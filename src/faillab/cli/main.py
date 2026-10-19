"""CLI wiring (DEVELOPMENT.md §4): commands, confirmation gate, exit codes, interrupts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console

from faillab import __version__
from faillab.analyze import analyze_incident, apply_analysis
from faillab.config import FaillabConfig, load_config
from faillab.domain.enums import Backend, IncidentStatus
from faillab.domain.models import Evidence, Incident, ReproSpec
from faillab.errors import (
    INCIDENT_NO_REPRO,
    INVALID_ARGUMENT,
    REPRO_SPEC_INVALID,
    FaillabError,
    IncidentError,
    describe,
)
from faillab.ingest import import_file
from faillab.ingest.incidents import incident_signature, slugify
from faillab.llm import LlmClient
from faillab.paths import WorkLayout
from faillab.report import build_report, dump_json, write_html_report, write_json_report
from faillab.report.console import (
    render_dashboard_brief,
    render_incident_detail,
    render_incident_list,
    render_repro_run,
)
from faillab.repro.runner import ReproRunner
from faillab.repro.signals import signals_configured, validate_pattern
from faillab.store import Store
from faillab.util import now_iso

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="faillab — 把 AI coding agent 的失败变成可检索、可分析、可复现的事故记录。",
)
console = Console()
err_console = Console(stderr=True)

EXIT_OK = 0
EXIT_REPRODUCED = 1
EXIT_NOT_PERFORMED = 2
EXIT_INTERNAL = 3
EXIT_INTERRUPTED = 130


@app.callback()
def _bootstrap() -> None:
    # Runs for every entry point: cp936 consoles cannot encode the Chinese output (§22).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _print_error(exc: BaseException) -> None:
    payload = describe(exc)
    err_console.print(f"[red][{payload['code']}][/] {payload['message']}")
    if payload.get("details"):
        err_console.print(f"[dim]{payload['details']}[/]")


def _fail(exc: BaseException) -> typer.Exit:
    _print_error(exc)
    return typer.Exit(EXIT_INTERNAL)


def _not_performed(exc: BaseException) -> typer.Exit:
    """Pre-execution problems are 'not performed' (exit 2), not internal errors."""
    _print_error(exc)
    return typer.Exit(EXIT_NOT_PERFORMED)


def _layout(
    workdir: str | None,
    config_path: Path | None,
    *,
    config: FaillabConfig | None = None,
) -> WorkLayout:
    active = config or load_config(str(config_path) if config_path else None)
    root = Path(workdir) if workdir else Path.cwd() / active.defaults.workdir
    return WorkLayout(root)


def _parse_status(value: str) -> IncidentStatus:
    try:
        return IncidentStatus(value)
    except ValueError as exc:
        raise FaillabError(
            f"--status 非法：{value}（open|reproduced|fixed|wontfix）",
            code=INVALID_ARGUMENT,
        ) from exc


def _validate_timeout(timeout_s: float | None) -> None:
    if timeout_s is not None and timeout_s <= 0:
        raise FaillabError("--timeout 必须大于 0", code=INVALID_ARGUMENT)


def _repro_spec(
    *,
    cmd: str | None,
    pattern: str | None,
    cwd: str | None,
    timeout_s: float | None,
    exit_nonzero: bool | None,
) -> ReproSpec | None:
    if cmd is None:
        if pattern or cwd or timeout_s is not None:
            raise FaillabError(
                "--pattern/--cwd/--timeout 需要与 --cmd 一起提供",
                code=INVALID_ARGUMENT,
            )
        return None
    if not cmd.strip():
        raise FaillabError("--cmd 不能为空", code=INVALID_ARGUMENT)
    _validate_timeout(timeout_s)
    validate_pattern(pattern)
    return ReproSpec(
        cwd=cwd,
        command=cmd,
        pattern=pattern,
        expect_exit_nonzero=True if exit_nonzero is None else exit_nonzero,
        timeout_s=timeout_s,
    )


def _confirm_gate(entries: list[tuple[Incident, ReproSpec]], *, yes: bool) -> tuple[bool, str]:
    if yes:
        return True, ""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False, "非交互终端且未提供 --yes，已跳过复现（补上 --yes 或在本机终端重跑）"
    console.print()
    console.print(f"[bold]即将执行 {len(entries)} 条复现命令[/]（在你的项目目录中运行，可能修改文件）")
    for incident, spec in entries[:5]:
        timeout_note = f"{spec.timeout_s:.0f}s" if spec.timeout_s else "默认"
        console.print(f"  {incident.public_id} [dim]cwd={spec.cwd or '（当前目录）'} timeout={timeout_note}[/]")
        console.print(f"    [bold]{spec.command}[/]")
    if len(entries) > 5:
        console.print(f"  [dim]… 其余 {len(entries) - 5} 条[/]")
    console.print("[yellow]⚠ 请确认命令无害且工作区已提交/备份；超时与 Ctrl+C 护栏已生效。[/]")
    answer = typer.prompt("继续？[y/N]", default="n", show_default=False)
    if answer.strip().lower() not in {"y", "yes"}:
        return False, "已按你的选择跳过复现"
    return True, ""


@app.command()
def version() -> None:
    """打印版本信息。"""
    console.print(f"faillab {__version__}")


@app.command(name="import")
def import_command(
    file: Path = typer.Argument(..., help="转录文件（.jsonl 事件流 或 .json 单对象）"),
    harness: str | None = typer.Option(None, "--harness", help="覆盖 harness 字段"),
    model: str | None = typer.Option(None, "--model", help="覆盖 model 字段"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只检测与预览，不写库"),
    new: bool = typer.Option(False, "--new", help="同签名时强制新建（默认合并）"),
    cmd: str | None = typer.Option(None, "--cmd", help="附带复现命令"),
    pattern: str | None = typer.Option(None, "--pattern", help="失败信号正则（配合 --cmd）"),
    cwd: str | None = typer.Option(None, "--cwd", help="复现命令工作目录"),
    timeout_s: float | None = typer.Option(None, "--timeout", help="复现超时（秒）"),
    exit_nonzero: bool | None = typer.Option(
        None, "--exit-nonzero/--no-exit-nonzero", help="失败信号：非零退出（默认是）"
    ),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """导入转录 → 检测失败 → 生成/合并事故记录。"""
    try:
        layout = _layout(workdir, None)
        reproduce = _repro_spec(cmd=cmd, pattern=pattern, cwd=cwd, timeout_s=timeout_s, exit_nonzero=exit_nonzero)
        store = Store(layout.db_path)
        with store:
            outcome = import_file(
                file,
                store=store,
                harness=harness,
                model=model,
                reproduce=reproduce,
                force_new=new,
                dry_run=dry_run,
            )
        trace = outcome.trace
        console.print(
            f"[dim]trace[/] {trace.trace_id} · {trace.harness} / {trace.model} · "
            f"工具调用 {len(trace.tool_calls)} · retries {trace.retries}"
        )
        for note in trace.warnings:
            console.print(f"[yellow]{note.code}[/] {note.message}")
        if dry_run:
            if outcome.skipped_existing_trace:
                console.print("[yellow]该转录已导入过（trace 已存在）——dry-run 无写入[/]")
            elif not outcome.is_failure:
                console.print("[dim]dry-run：未检测到失败，不会生成事故[/]")
            elif outcome.would_merge_into:
                console.print(f"[yellow]dry-run：签名与 {outcome.would_merge_into} 相同，导入将合并[/]")
            else:
                console.print(f"[green]dry-run：将生成新事故（命中规则 {len(outcome.hits)} 条）[/]")
            raise typer.Exit(EXIT_OK)
        if outcome.skipped_existing_trace:
            console.print("[yellow]该转录已导入过（幂等跳过）[/]")
        elif outcome.incident is None:
            console.print("[dim]未检测到失败，转录已归档（不生成事故）[/]")
        elif outcome.merged:
            console.print(
                f"[green]已合并到 {outcome.incident.public_id}[/]（occurrences={outcome.incident.occurrences}）"
            )
        else:
            incident = outcome.incident
            console.print(f"[bold green]生成事故 {incident.public_id}[/] {incident.title}")
            console.print(
                f"[dim]签名 {incident.signature} · 命中规则 {', '.join(hit.rule_id for hit in outcome.hits)}[/]"
            )
            if incident.reproduce is None:
                console.print("[dim]提示：还没有复现定义，可用 annotate 补充 --cmd/--pattern[/]")
    except FaillabError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command()
def record(
    title: str = typer.Option(..., "--title", help="事故标题"),
    failure: str = typer.Option(..., "--failure", help="失败描述"),
    task: str | None = typer.Option(None, "--task", help="原始任务"),
    model: str = typer.Option("unknown", "--model"),
    harness: str = typer.Option("unknown", "--harness"),
    cmd: str | None = typer.Option(None, "--cmd", help="复现命令"),
    pattern: str | None = typer.Option(None, "--pattern", help="失败信号正则"),
    cwd: str | None = typer.Option(None, "--cwd", help="复现工作目录"),
    timeout_s: float | None = typer.Option(None, "--timeout"),
    exit_nonzero: bool | None = typer.Option(None, "--exit-nonzero/--no-exit-nonzero"),
    status: str | None = typer.Option(None, "--status", help="open | reproduced | fixed | wontfix"),
    new: bool = typer.Option(False, "--new", help="同签名时强制新建"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """手动登记一条事故（转录之外的失败）。"""
    try:
        layout = _layout(workdir, None)
        reproduce = _repro_spec(cmd=cmd, pattern=pattern, cwd=cwd, timeout_s=timeout_s, exit_nonzero=exit_nonzero)
        status_value = _parse_status(status) if status else None
        signature = incident_signature(failure)
        with Store(layout.db_path) as store:
            existing = None if new else store.find_by_signature(signature)
            if existing is not None:
                existing.occurrences += 1
                existing.evidence.append(Evidence(kind="manual", detail=f"再次登记：{title}"))
                if reproduce is not None and existing.reproduce is None:
                    existing.reproduce = reproduce
                if status_value is not None:
                    existing.status = status_value
                existing.updated_at = now_iso()
                store.update_incident(existing)
                console.print(
                    f"[green]已合并到 {existing.public_id}[/]（签名相同，occurrences={existing.occurrences}）"
                )
                raise typer.Exit(EXIT_OK)
            incident = Incident(
                public_id=store.next_public_id(),
                slug=slugify(title),
                title=title,
                task=task or "",
                failure=failure,
                model=model,
                harness=harness,
                signature=signature,
                status=status_value or IncidentStatus.OPEN,
                evidence=[Evidence(kind="manual", detail="手动登记")],
                reproduce=reproduce,
                source="record",
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            store.insert_incident(incident)
        console.print(f"[bold green]生成事故 {incident.public_id}[/] {incident.title}")
        if incident.reproduce is None:
            console.print("[dim]提示：还没有复现定义，可用 annotate 补充 --cmd/--pattern[/]")
    except FaillabError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command(name="list")
def list_command(
    category: str | None = typer.Option(None, "--category"),
    model: str | None = typer.Option(None, "--model"),
    harness: str | None = typer.Option(None, "--harness"),
    status: str | None = typer.Option(None, "--status", help="open | reproduced | fixed | wontfix"),
    repro: str | None = typer.Option(None, "--repro", help="unverified | reproduced | fixed | inconclusive"),
    query: str | None = typer.Option(None, "--query", help="关键词（标题/失败/原因 模糊匹配）"),
    limit: int | None = typer.Option(None, "--limit"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """过滤查询事故记录。"""
    try:
        layout = _layout(workdir, None)
        with Store(layout.db_path) as store:
            incidents = store.list_incidents(
                category=category, model=model, harness=harness, status=status, repro=repro, query=query, limit=limit
            )
        render_incident_list(console, incidents)
    except FaillabError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command()
def show(
    public_id: str = typer.Argument(..., help="事故 ID，例如 F-0001"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """查看事故详情 + 证据 + 复现历史。"""
    try:
        layout = _layout(workdir, None)
        with Store(layout.db_path) as store:
            incident = store.require_incident(public_id)
            runs = store.list_repro_runs(public_id)
        render_incident_detail(console, incident, runs)
    except FaillabError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command()
def annotate(
    public_id: str = typer.Argument(...),
    cmd: str | None = typer.Option(None, "--cmd", help="复现命令"),
    pattern: str | None = typer.Option(None, "--pattern", help="失败信号正则"),
    cwd: str | None = typer.Option(None, "--cwd"),
    timeout_s: float | None = typer.Option(None, "--timeout"),
    exit_nonzero: bool | None = typer.Option(None, "--exit-nonzero/--no-exit-nonzero"),
    title: str | None = typer.Option(None, "--title"),
    recovery: str | None = typer.Option(None, "--recovery"),
    status: str | None = typer.Option(None, "--status", help="open | reproduced | fixed | wontfix"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """补充/修改事故的复现定义与人工字段。"""
    try:
        _validate_timeout(timeout_s)
        validate_pattern(pattern)
        layout = _layout(workdir, None)
        with Store(layout.db_path) as store:
            incident = store.require_incident(public_id)
            if any(value is not None for value in (cmd, pattern, cwd, timeout_s, exit_nonzero)):
                base = incident.reproduce
                command = cmd or (base.command if base else "")
                if not command:
                    raise FaillabError("需要 --cmd 提供复现命令", code=INVALID_ARGUMENT)
                incident.reproduce = ReproSpec(
                    cwd=cwd if cwd is not None else (base.cwd if base else None),
                    command=command,
                    pattern=pattern if pattern is not None else (base.pattern if base else None),
                    expect_exit_nonzero=(
                        exit_nonzero if exit_nonzero is not None else (base.expect_exit_nonzero if base else True)
                    ),
                    timeout_s=timeout_s if timeout_s is not None else (base.timeout_s if base else None),
                )
            if title:
                incident.title = title
                incident.slug = slugify(title)
            if recovery:
                incident.recovery = recovery
            if status:
                incident.status = _parse_status(status)
            incident.updated_at = now_iso()
            store.update_incident(incident)
        console.print(f"[green]已更新 {incident.public_id}[/]")
        if incident.reproduce is not None:
            spec = incident.reproduce
            console.print(f"  [dim]command[/] {spec.command}")
            signals = []
            if spec.pattern:
                signals.append(f"pattern={spec.pattern}")
            if spec.expect_exit_nonzero:
                signals.append("expect_exit_nonzero")
            console.print(f"  [dim]信号[/] {', '.join(signals) or '（无 —— 不可执行）'}")
    except FaillabError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


def _resolve_backend(kind: str, client: LlmClient) -> Backend:
    if kind not in {"auto", "llm", "offline"}:
        raise FaillabError(f"--backend 非法：{kind}（auto|llm|offline）", code=INVALID_ARGUMENT)
    ready, reason = client.ready()
    if kind == "offline":
        return Backend.OFFLINE
    if kind == "llm":
        if not ready:
            raise FaillabError(f"LLM 后端不可用：{reason}", code="LLM_DISABLED")
        return Backend.LLM
    if ready:
        return Backend.LLM
    console.print(f"[yellow]auto 降级为离线后端：{reason}[/]")
    return Backend.OFFLINE


@app.command(name="analyze")
def analyze_command(
    public_id: str | None = typer.Argument(None, help="事故 ID（或使用 --pending 批量）"),
    pending: bool = typer.Option(False, "--pending", help="分析所有未分析的事故"),
    backend: str = typer.Option("auto", "--backend", help="auto | llm | offline"),
    config_path: Path | None = typer.Option(None, "--config"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """分类 / 根因 / 恢复建议（离线 taxonomy 或 LLM）。"""
    try:
        if pending and public_id:
            raise FaillabError("--pending 与事故 ID 只能二选一", code=INVALID_ARGUMENT)
        config = load_config(str(config_path) if config_path else None)
        layout = _layout(workdir, config_path, config=config)
        client = LlmClient(config)
        backend_kind = _resolve_backend(backend, client)
        with Store(layout.db_path) as store:
            if pending:
                targets = [item for item in store.list_incidents() if item.analyzed_by is None]
                if not targets:
                    console.print("[dim]没有未分析的事故[/]")
                    raise typer.Exit(EXIT_OK)
            else:
                if not public_id:
                    raise FaillabError("需要事故 ID 或 --pending", code=INVALID_ARGUMENT)
                targets = [store.require_incident(public_id)]
            for incident in targets:
                trace = store.get_trace(incident.traces[0]) if incident.traces else None
                result = analyze_incident(incident, trace=trace, store=store, backend=backend_kind, client=client)
                apply_analysis(incident, result, store)
                fallback_note = f" [yellow]（{result.fallback_reason}）[/]" if result.fallback_reason else ""
                console.print(
                    f"[bold]{incident.public_id}[/] → [bold]{result.category_label}[/]"
                    f"（{result.category}/{result.severity.value}/{result.confidence.value}，{result.backend.value}）{fallback_note}"
                )
                console.print(f"  [dim]根因[/] {result.reason}")
                if result.recovery:
                    console.print(f"  [dim]建议[/] {result.recovery[:160]}")
                if result.precedents:
                    console.print(f"  [dim]相似先例[/] {', '.join(result.precedents)}")
    except FaillabError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command()
def repro(
    public_id: str | None = typer.Argument(None, help="事故 ID（或使用 --all）"),
    all_incidents: bool = typer.Option(False, "--all", help="复现所有带复现定义的事故"),
    yes: bool = typer.Option(False, "--yes", help="跳过交互确认"),
    timeout_s: float | None = typer.Option(None, "--timeout", help="单条复现超时（秒，覆盖记录值）"),
    cwd: str | None = typer.Option(None, "--cwd", help="覆盖记录的工作目录"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """重放复现命令并判定 reproduced / fixed / inconclusive。"""
    exit_code = EXIT_OK
    preflight_failed = 0
    statuses = []
    try:
        _validate_timeout(timeout_s)
        if all_incidents and public_id:
            raise FaillabError("--all 与事故 ID 只能二选一", code=INVALID_ARGUMENT)
        config = load_config()
        layout = _layout(workdir, None, config=config)
        with Store(layout.db_path) as store:
            if all_incidents:
                candidates = [item for item in store.list_incidents() if item.reproduce is not None]
                entries: list[tuple[Incident, ReproSpec]] = []
                skipped = 0
                for item in candidates:
                    spec = item.reproduce
                    if spec is None or not signals_configured(spec):
                        skipped += 1
                        continue
                    try:
                        validate_pattern(spec.pattern)
                        if not spec.command.strip():
                            raise FaillabError(f"{item.public_id} 的复现命令为空", code=REPRO_SPEC_INVALID)
                        if timeout_s is None and spec.timeout_s is not None and spec.timeout_s <= 0:
                            raise FaillabError(f"{item.public_id} 的复现超时必须大于 0", code=REPRO_SPEC_INVALID)
                    except FaillabError as exc:
                        console.print(f"[yellow]跳过 {item.public_id}：{exc.message}[/]")
                        preflight_failed += 1
                        continue
                    entries.append((item, spec))
                if skipped:
                    console.print(f"[dim]跳过 {skipped} 条没有失败信号的事故[/]")
                if not entries:
                    console.print("[yellow]没有任何可执行的复现定义（先 annotate 补充 --cmd/--pattern）[/]")
                    raise typer.Exit(EXIT_NOT_PERFORMED)
            else:
                if not public_id:
                    raise FaillabError("需要事故 ID 或 --all", code=INVALID_ARGUMENT)
                incident = store.require_incident(public_id)
                if incident.reproduce is None:
                    raise IncidentError(
                        f"{incident.public_id} 没有复现定义；可用 annotate --cmd/--pattern 补充",
                        code=INCIDENT_NO_REPRO,
                    )
                if not signals_configured(incident.reproduce):
                    raise IncidentError(
                        f"{incident.public_id} 的复现定义没有失败信号（--pattern 或 --exit-nonzero）",
                        code=REPRO_SPEC_INVALID,
                    )
                entries = [(incident, incident.reproduce)]

            proceed, note = _confirm_gate(entries, yes=yes)
            if not proceed:
                console.print(f"[yellow]{note}[/]")
                raise typer.Exit(EXIT_NOT_PERFORMED)

            runner = ReproRunner(
                layout=layout,
                store=store,
                timeout_s=config.defaults.repro_timeout_s,
                timeout_override=timeout_s,
                echo=console.print,
            )
            for incident, _spec in entries:
                try:
                    run = runner.run(store.require_incident(incident.public_id), cwd_override=cwd)
                except FaillabError as exc:
                    # One broken record must not block the rest of a --all run.
                    console.print(f"[yellow]{incident.public_id} 未执行：{exc.message}[/]")
                    preflight_failed += 1
                    continue
                render_repro_run(console, run)
                statuses.append(run.status)
        if any(status.value == "reproduced" for status in statuses):
            exit_code = EXIT_REPRODUCED
        elif preflight_failed or any(status.value in {"inconclusive", "interrupted"} for status in statuses):
            exit_code = EXIT_NOT_PERFORMED
        console.print(
            f"[dim]汇总：{sum(1 for s in statuses if s.value == 'reproduced')} reproduced · "
            f"{sum(1 for s in statuses if s.value == 'fixed')} fixed · "
            f"{sum(1 for s in statuses if s.value not in {'reproduced', 'fixed'})} inconclusive"
            + (f" · {preflight_failed} 未执行" if preflight_failed else "")
            + "[/]"
        )
    except KeyboardInterrupt:
        err_console.print("[yellow]已中断（Ctrl+C）：子进程已终止，历史保留。[/]")
        raise typer.Exit(EXIT_INTERRUPTED) from None
    except FaillabError as exc:
        if exc.code in {INCIDENT_NO_REPRO, REPRO_SPEC_INVALID}:
            raise _not_performed(exc) from None
        raise _fail(exc) from None
    raise typer.Exit(exit_code)


@app.command()
def status(
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """总览统计。"""
    try:
        layout = _layout(workdir, None)
        with Store(layout.db_path) as store:
            report = build_report(store)
        render_dashboard_brief(console, report)
    except FaillabError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command()
def report(
    html_out: Path | None = typer.Option(None, "--html", help="HTML 输出路径（默认 <workdir>/report.html）"),
    json_out: Path | None = typer.Option(None, "--json", help="JSON 输出路径（默认 <workdir>/report.json）"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """生成自包含 HTML 仪表盘 + report.json。"""
    try:
        layout = _layout(workdir, None)
        with Store(layout.db_path) as store:
            report_obj = build_report(store)
        html_path = html_out or (layout.root / "report.html")
        json_path = json_out or (layout.root / "report.json")
        write_html_report(report_obj, html_path)
        write_json_report(report_obj, json_path)
        render_dashboard_brief(console, report_obj)
        console.print(f"[dim]报告[/] {html_path}")
        console.print(f"[dim]JSON[/] {json_path}")
    except FaillabError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command()
def export(
    path: Path = typer.Argument(..., help="bundle 输出路径（.json）"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """导出 JSON bundle（事故 + traces + 复现历史）。"""
    try:
        layout = _layout(workdir, None)
        with Store(layout.db_path) as store:
            bundle = store.export_bundle()
        dump_json(bundle, path)
        console.print(
            f"[green]已导出[/] {path}（事故 {len(bundle['incidents'])} · traces {len(bundle['traces'])} · "
            f"复现历史 {len(bundle['repro_runs'])}）"
        )
    except FaillabError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


@app.command(name="import-bundle")
def import_bundle_command(
    path: Path = typer.Argument(..., help="bundle 路径"),
    workdir: str | None = typer.Option(None, "--workdir"),
) -> None:
    """合并 bundle 回库（已存在且更新的文档会被合并）。"""
    try:
        layout = _layout(workdir, None)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FaillabError(f"bundle 读取失败：{exc}", code="BUNDLE_INVALID") from exc
        with Store(layout.db_path) as store:
            summary = store.import_bundle(payload)
        console.print(
            f"[green]已合并[/] 新增 {summary.added} · 更新 {summary.merged} · 跳过 {summary.skipped} · "
            f"traces +{summary.traces_added} · 复现历史 +{summary.repro_runs_added}"
        )
    except FaillabError as exc:
        raise _fail(exc) from None
    raise typer.Exit(EXIT_OK)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        err_console.print("[yellow]已中断（Ctrl+C）[/]")
        raise SystemExit(EXIT_INTERRUPTED) from None


if __name__ == "__main__":  # pragma: no cover
    main()
