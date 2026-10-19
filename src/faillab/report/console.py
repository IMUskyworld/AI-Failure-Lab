"""Console rendering (DEVELOPMENT.md §12)."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from faillab.domain.enums import ReproStatus
from faillab.domain.models import Incident, Report, ReproRun

STATUS_DISPLAY: dict[ReproStatus, tuple[str, str]] = {
    ReproStatus.UNVERIFIED: ("–", "dim"),
    ReproStatus.REPRODUCED: ("✗", "red"),
    ReproStatus.FIXED: ("✓", "green"),
    ReproStatus.INCONCLUSIVE: ("?", "yellow"),
    ReproStatus.INTERRUPTED: ("!", "yellow"),
}

STATUS_ASCII: dict[ReproStatus, str] = {
    ReproStatus.UNVERIFIED: "---",
    ReproStatus.REPRODUCED: "REP",
    ReproStatus.FIXED: "FIX",
    ReproStatus.INCONCLUSIVE: "INC",
    ReproStatus.INTERRUPTED: "INTR",
}


def _unicode_ok(console: Console) -> bool:
    encoding = getattr(getattr(console, "file", None), "encoding", None) or "utf-8"
    try:
        "✓✗→".encode(encoding)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


def _repro_cell(status: ReproStatus, unicode_ok: bool) -> str:
    glyph, style = STATUS_DISPLAY[status]
    if not unicode_ok:
        glyph = STATUS_ASCII[status]
    return f"[{style}]{glyph}[/]"


def render_incident_list(console: Console, incidents: list[Incident]) -> None:
    unicode_ok = _unicode_ok(console)
    table = Table(title=f"事故列表（{len(incidents)} 条）", title_justify="left", show_edge=False)
    table.add_column("ID", no_wrap=True)
    table.add_column("标题", overflow="fold")
    table.add_column("类别", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("复现", no_wrap=True)
    table.add_column("模型 / Harness", no_wrap=True)
    table.add_column("出现", justify="right", no_wrap=True)
    for incident in incidents:
        table.add_row(
            incident.public_id,
            incident.title,
            incident.category_label,
            incident.status.value,
            _repro_cell(incident.reproduction.status, unicode_ok),
            f"{incident.model} / {incident.harness}",
            str(incident.occurrences),
        )
    console.print(table)


def render_incident_detail(console: Console, incident: Incident, runs: list[ReproRun]) -> None:
    console.print(f"[bold]{incident.public_id}[/] {incident.title}")
    console.print(
        f"[dim]类别[/] {incident.category_label}（{incident.category}） · [dim]严重度[/] {incident.severity.value} · "
        f"[dim]置信度[/] {incident.confidence.value} · [dim]状态[/] {incident.status.value} · "
        f"[dim]复现[/] {incident.reproduction.status.value}"
    )
    console.print(
        f"[dim]模型 / Harness[/] {incident.model} / {incident.harness} · [dim]出现[/] {incident.occurrences} 次"
    )
    if incident.task:
        console.print(f"[dim]任务[/] {incident.task}")
    console.print(f"[dim]失败[/] {incident.failure}")
    if incident.reason:
        console.print(f"[dim]根因[/] {incident.reason}")
    if incident.recovery:
        console.print(f"[dim]恢复建议[/] {incident.recovery}")
    if incident.fallback_reason:
        console.print(f"[yellow]{incident.fallback_reason}[/]")
    if incident.evidence:
        console.print("[bold]证据[/]")
        for item in incident.evidence:
            console.print(f"  [dim]·[/] [dim][{item.kind}][/] {item.detail}")
    if incident.reproduce is not None:
        spec = incident.reproduce
        console.print("[bold]复现定义[/]")
        console.print(f"  [dim]cwd[/] {spec.cwd or '（当前目录）'}")
        console.print(f"  [dim]command[/] [bold]{spec.command}[/]")
        signals = []
        if spec.pattern:
            signals.append(f"pattern={spec.pattern}")
        if spec.expect_exit_nonzero:
            signals.append("expect_exit_nonzero")
        console.print(f"  [dim]信号[/] {', '.join(signals) or '（无 —— 不可执行）'}")
    else:
        console.print("[yellow]没有复现定义（未验证）：可用 annotate 补充 --cmd/--pattern[/]")
    if runs:
        console.print("[bold]复现历史[/]")
        unicode_ok = _unicode_ok(console)
        for run in runs[:5]:
            console.print(
                f"  {_repro_cell(run.status, unicode_ok)} {run.started_at} · {run.duration_s:.1f}s · "
                f"exit={run.exit_code} · {run.note}"
            )
    if incident.traces:
        console.print(f"[dim]traces[/] {', '.join(incident.traces)}")


def render_repro_run(console: Console, run: ReproRun) -> None:
    style = {"reproduced": "red", "fixed": "green", "inconclusive": "yellow", "interrupted": "yellow"}[run.status.value]
    console.print(
        f"[{style}]{run.status.value}[/] · {run.incident_id} · {run.duration_s:.1f}s · "
        f"exit={run.exit_code} · pattern={run.pattern_matched} · exit≠0={run.exit_nonzero_matched} · {run.note}"
    )


def render_dashboard_brief(console: Console, report: Report) -> None:
    totals = report.totals
    console.print(
        f"[bold]faillab 总览[/] 事故 {totals.incidents} · 仍可复现 {totals.reproduced} · 已修复 {totals.fixed} · "
        f"未解决 {totals.open} · 未验证 {totals.unverified} · 复现率 {totals.repro_rate * 100:.0f}%"
    )
    for item in report.by_category[:5]:
        console.print(f"  [dim]{item.label}[/] {item.count}")
