"""MVP acceptance audit for DEVELOPMENT.md §20.

Each check produces executable evidence; the only local real-subprocess checks are
the repro plumbing ones (echo-level commands, explicitly labelled).

Usage:
    python scripts/verify_mvp.py                # includes pytest (slow but complete)
    python scripts/verify_mvp.py --skip-tests   # when the caller already ran pytest

Exit code 0 only when nothing failed.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# cp936 consoles cannot encode the Chinese output; keep the whole audit log UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from faillab.analyze import analyze_incident, analyze_offline, apply_analysis  # noqa: E402
from faillab.analyze.taxonomy import match_category  # noqa: E402
from faillab.config import FaillabConfig  # noqa: E402
from faillab.domain.enums import Backend, IncidentStatus, ReproStatus  # noqa: E402
from faillab.domain.models import ReproSpec, ToolCall, Trace  # noqa: E402
from faillab.llm import LlmClient  # noqa: E402
from faillab.paths import WorkLayout  # noqa: E402
from faillab.repro.runner import ReproRunner  # noqa: E402
from faillab.repro.signals import evaluate  # noqa: E402
from faillab.store import Store  # noqa: E402

DEV = ROOT / "scripts" / "faillab_dev.py"
TRACES = ROOT / "tests" / "fixtures" / "traces"
EXTERNAL_REF_RE = re.compile(r'(?:src|href)\s*=\s*"https?://')

RESULTS: list[tuple[str, str, str]] = []

FAKE_ANALYZE = {
    "category": "env_permission",
    "reason": "假服务器：命令不存在",
    "recovery": "安装缺失命令",
    "severity": "medium",
    "confidence": "high",
}


def record(status: str, name: str, detail: str) -> None:
    RESULTS.append((status, name, detail))
    print(f"{status:<4} {name:<36} {detail}")


def run_cli(
    *args: str, cwd: Path, env_extra: dict[str, str] | None = None, strip_llm_env: bool = False
) -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if strip_llm_env:
        for key in list(env):
            if key.startswith("FAILLAB_LLM_"):
                env.pop(key)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(DEV), *args],
        cwd=str(cwd),
        capture_output=True,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    output = proc.stdout.decode("utf-8", errors="replace") + proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, output


def workdir(cwd: Path) -> str:
    return str(cwd / ".faillab")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class _FakeLlmHandler(http.server.BaseHTTPRequestHandler):
    mode = "ok"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        text = json.dumps(body, ensure_ascii=False)
        content = json.dumps(FAKE_ANALYZE, ensure_ascii=False) if "失败分析助手" in text else "{}"
        if type(self).mode == "garbage":
            content = "这不是 JSON"
        data = json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


class _FakeLlm:
    def __enter__(self) -> str:
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeLlmHandler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        _FakeLlmHandler.mode = "ok"
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __exit__(self, *exc_info: object) -> None:
        self._server.shutdown()
        self._server.server_close()


def llm_env(base_url: str) -> dict[str, str]:
    return {
        "FAILLAB_LLM_BASE_URL": base_url,
        "FAILLAB_LLM_MODEL": "fake-qwen",
        "FAILLAB_LLM_API_KEY": "test-key",
    }


def _trace(**overrides) -> Trace:
    base = Trace(trace_id="tr-audit", task="audit 任务")
    return base.model_copy(update=overrides)


# --- checks -------------------------------------------------------------------


def check_20_01_import_jsonl(tmp: Path) -> tuple[str, str]:
    code, output = run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp), cwd=tmp)
    if code != 0:
        return "FAIL", f"exit={code}；{output.strip()[-160:]}"
    with Store(Path(workdir(tmp)) / "faillab.db") as store:
        incident = store.require_incident("F-0001")
    if not (incident.task and incident.failure and incident.model == "claude-sonnet-4.5"):
        return "FAIL", f"字段不完整：{incident.model_dump()}"
    rule_ids = {item.detail.split()[1] for item in incident.evidence if item.kind == "detection"}
    if rule_ids != {"error_present", "final_failed", "tool_errors"}:
        return "FAIL", f"检测规则记录不符：{rule_ids}"
    return "PASS", f"F-0001 生成，字段齐全，命中规则 {', '.join(sorted(rule_ids))}"


def check_20_02_import_object_aliases(tmp: Path) -> tuple[str, str]:
    code, output = run_cli("import", str(TRACES / "result.json"), "--workdir", workdir(tmp), cwd=tmp)
    if code != 0:
        return "FAIL", f"exit={code}"
    with Store(Path(workdir(tmp)) / "faillab.db") as store:
        incident = store.require_incident("F-0001")
        trace = store.get_trace(incident.traces[0])
    if incident.model != "gpt-5.2" or incident.harness != "qoder":
        return "FAIL", f"别名映射失败：{incident.model}/{incident.harness}"
    if trace is None or not trace.final_result or "[failed]" not in trace.final_result:
        return "FAIL", "final.ok=false 未标记为失败态"

    minimal = tmp / "minimal.jsonl"
    minimal.write_text('{"kind": "final", "result": "x", "ok": false}\n', encoding="utf-8")
    code, output = run_cli("import", str(minimal), "--workdir", workdir(tmp), cwd=tmp)
    if code != 0 or "MISSING_FIELD" not in output:
        return "FAIL", f"缺字段未宽容处理（warning 缺失）：exit={code}"
    return "PASS", "别名映射正确；缺字段宽容 + warning 记录"


def check_20_03_dedup(tmp: Path) -> tuple[str, str]:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp), cwd=tmp)
    code, output = run_cli("import", str(TRACES / "dup.jsonl"), "--workdir", workdir(tmp), cwd=tmp)
    if code != 0 or "已合并到 F-0001" not in output:
        return "FAIL", f"同签名未合并：exit={code}"
    with Store(Path(workdir(tmp)) / "faillab.db") as store:
        incident = store.require_incident("F-0001")
        total = store.count_incidents()
    if incident.occurrences != 2 or total != 1 or len(incident.traces) != 2:
        return (
            "FAIL",
            f"合并结果不符：occurrences={incident.occurrences}，库内 {total} 条，traces={len(incident.traces)}",
        )
    return "PASS", "同签名二次导入 → 合并（occurrences=2，trace 关联，不新增记录）"


def check_20_04_record_and_merge(tmp: Path) -> tuple[str, str]:
    code, output = run_cli(
        "record",
        "--title",
        "手动事故",
        "--failure",
        "Bash: command not found: yolo",
        "--workdir",
        workdir(tmp),
        cwd=tmp,
    )
    if code != 0 or "生成事故 F-0001" not in output:
        return "FAIL", f"record 失败：exit={code}"
    code, output = run_cli(
        "record",
        "--title",
        "再次遇到",
        "--failure",
        "Bash: command not found: yolo",
        "--workdir",
        workdir(tmp),
        cwd=tmp,
    )
    if "已合并到 F-0001" not in output:
        return "FAIL", "撞库未提示既有事故 ID"
    code, output = run_cli(
        "record",
        "--title",
        "强制新建",
        "--failure",
        "Bash: command not found: yolo",
        "--new",
        "--workdir",
        workdir(tmp),
        cwd=tmp,
    )
    if "生成事故 F-0002" not in output:
        return "FAIL", "--new 未强制新建"
    return "PASS", "手动登记 + 撞库合并提示 + --new 强制新建"


def check_20_05_analyze_offline(tmp: Path) -> tuple[str, str]:
    del tmp
    cases = [
        ("retry_loop", _trace(retries=3)),
        ("hallucinated_api", _trace(error="ModuleNotFoundError: No module named 'oauthlib'")),
        ("tool_misuse", _trace(error="invalid argument: missing 'old_text'")),
        ("env_permission", _trace(error="command not found: yolo")),
        ("env_permission", _trace(error="SecurityError: UnauthorizedAccess - 禁止运行脚本")),
        ("env_permission", _trace(error="NotADirectoryError: [WinError 267] 目录名称无效")),
        ("encoding_locale", _trace(error="UnicodeDecodeError: 'utf-8' codec can't decode byte 0xcf")),
        ("wrong_file_edit", _trace(error="string not found in config.py")),
        ("context_misidentification", _trace(error="expected 'a' but found 'b'")),
        (
            "test_failure_ignored",
            _trace(
                tool_calls=[ToolCall(index=0, name="Bash", error="pytest: 2 tests failed")], final_result="[failed] x"
            ),
        ),
        ("premature_completion", _trace(final_result="implemented OAuth login")),
        ("regression_introduced", _trace(error="existing tests failed after the change")),
    ]
    for expected, trace in cases:
        match = match_category(trace)
        if match is None or match.category.id != expected:
            return "FAIL", f"{expected} 未命中（实际 {match.category.id if match else None}）"

    audit_dir = Path(tempfile.mkdtemp(prefix="faillab-audit-"))
    try:
        with Store(audit_dir / "faillab.db") as store:
            from faillab.domain.models import Incident

            first = Incident(
                public_id="F-0001",
                slug="s",
                title="先例",
                category="hallucinated_api",
                category_label="幻觉 API/符号",
                created_at="t",
                updated_at="t",
            )
            store.insert_incident(first)
            second = Incident(public_id="F-0002", slug="s", title="待分析", created_at="t", updated_at="t")
            store.insert_incident(second)
            result = analyze_offline(second, _trace(error="AttributeError: has no attribute 'load'"), store)
            if result.precedents != ["F-0001"]:
                return "FAIL", f"相似先例未填充：{result.precedents}"
            unclassified = analyze_offline(second, _trace(final_result="[failed] oops"), store)
            if unclassified.category != "unclassified":
                return "FAIL", "无匹配时未落 unclassified"
            apply_analysis(second, result, store)
            if store.require_incident("F-0002").category != "hallucinated_api":
                return "FAIL", "分析结果未写回事故"
    finally:
        shutil.rmtree(audit_dir, ignore_errors=True)
    categories = len({expected for expected, _ in cases})
    return "PASS", f"taxonomy {len(cases)} 条正例全命中（{categories} 类）+ 兜底 unclassified + 相似先例 + 写回"


def check_20_06_analyze_llm(tmp: Path) -> tuple[str, str]:
    with _FakeLlm() as base_url:
        code, output = run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp), cwd=tmp)
        code, output = run_cli(
            "analyze",
            "F-0001",
            "--backend",
            "llm",
            "--workdir",
            workdir(tmp),
            cwd=tmp,
            env_extra=llm_env(base_url),
        )
        if code != 0 or "环境/权限问题" not in output:
            return "FAIL", f"LLM 分析未生效：exit={code}"
        with Store(Path(workdir(tmp)) / "faillab.db") as store:
            incident = store.require_incident("F-0001")
            if incident.analyzed_by is None or incident.analyzed_by.value != "llm":
                return "FAIL", "analyzed_by 未标记 llm"

        _FakeLlmHandler.mode = "garbage"
        code, output = run_cli(
            "analyze",
            "F-0001",
            "--backend",
            "llm",
            "--workdir",
            workdir(tmp),
            cwd=tmp,
            env_extra=llm_env(base_url),
        )
        if code != 0 or "LLM 降级" not in output:
            return "FAIL", "垃圾输出未触发降级"
        with Store(Path(workdir(tmp)) / "faillab.db") as store:
            incident = store.require_incident("F-0001")
            if incident.category != "wrong_file_edit" or not incident.fallback_reason:
                return "FAIL", "降级未用离线兜底或未记录原因"

    config = FaillabConfig()
    config.llm.enabled = True
    config.llm.base_url = "https://llm.example/v1"
    config.llm.model = "fake"
    os.environ["FAILLAB_LLM_API_KEY"] = "test-key"
    try:
        import httpx

        payload = dict(FAKE_ANALYZE, category="made_up")
        client = LlmClient(
            config,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})
            ),
        )
        with Store(tmp / "out-of-band.db") as store:
            from faillab.domain.models import Incident

            incident = Incident(public_id="F-0001", slug="s", title="t", created_at="t", updated_at="t")
            store.insert_incident(incident)
            result = analyze_incident(
                incident, trace=_trace(error="command not found: yolo"), store=store, backend=Backend.LLM, client=client
            )
            if result.fallback_reason is None or "白名单" not in result.fallback_reason:
                return "FAIL", "越界类别未触发白名单校验降级"
    finally:
        os.environ.pop("FAILLAB_LLM_API_KEY", None)
    return "PASS", "真 httpx 路径命中假服务器；垃圾/越界 → 降级 + 原因（含白名单校验）"


def check_20_07_repro_three_states(tmp: Path) -> tuple[str, str]:
    del tmp
    spec = ReproSpec(command="x", pattern="FAILED", expect_exit_nonzero=True)
    reproduced, *_ = evaluate(exit_code=1, timed_out=False, started=True, output="FAILED", spec=spec)
    fixed, *_ = evaluate(exit_code=0, timed_out=False, started=True, output="passed", spec=spec)
    inconclusive, *_ = evaluate(exit_code=0, timed_out=False, started=True, output="FAILED", spec=spec)
    timeout_case, *_ = evaluate(exit_code=None, timed_out=True, started=True, output="", spec=spec)
    if {reproduced, fixed, inconclusive, timeout_case} != {
        ReproStatus.REPRODUCED,
        ReproStatus.FIXED,
        ReproStatus.INCONCLUSIVE,
    }:
        return "FAIL", "三态判定不符"

    audit_dir = Path(tempfile.mkdtemp(prefix="faillab-audit-repro-"))
    layout = WorkLayout(audit_dir / ".faillab")

    class _FakeSpawner:
        def __init__(self, *, exit_code, output):
            self.exit_code = exit_code
            self.output = output

        def __call__(self, command, *, cwd, timeout_s):
            return self.exit_code, self.output, False, True

    try:
        with Store(layout.db_path) as store:
            from faillab.domain.models import Incident

            for public_id, fake in (("F-0001", _FakeSpawner(exit_code=1, output="FAILED x")),):
                incident = Incident(
                    public_id=public_id,
                    slug="s",
                    title="t",
                    created_at="t",
                    updated_at="t",
                    reproduce=ReproSpec(
                        command="pytest", pattern="FAILED", expect_exit_nonzero=False, cwd=str(audit_dir)
                    ),
                )
                store.insert_incident(incident)
                run = ReproRunner(layout=layout, store=store, spawner=fake).run(store.require_incident(public_id))
                if run.status != ReproStatus.REPRODUCED:
                    return "FAIL", f"假执行层未判 reproduced：{run.status}"
    finally:
        shutil.rmtree(audit_dir, ignore_errors=True)
    return "PASS", "假执行层覆盖 reproduced/fixed/inconclusive/超时；plumbing 见 20-8"


def check_20_08_repro_cli_exit_codes(tmp: Path) -> tuple[str, str]:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp), cwd=tmp)
    run_cli(
        "annotate",
        "F-0001",
        "--cmd",
        "echo FAILED test_oauth",
        "--pattern",
        "FAILED",
        "--no-exit-nonzero",
        "--workdir",
        workdir(tmp),
        cwd=tmp,
    )
    code, output = run_cli("repro", "F-0001", "--workdir", workdir(tmp), cwd=tmp)
    if code != 2 or "非交互终端" not in output:
        return "FAIL", f"非交互无 --yes 应零执行（exit 2）：exit={code}"
    code, output = run_cli("repro", "F-0001", "--yes", "--workdir", workdir(tmp), cwd=tmp)
    if code != 1 or "reproduced" not in output:
        return "FAIL", f"reproduced 应 exit 1：exit={code}"

    run_cli(
        "annotate",
        "F-0001",
        "--cmd",
        "echo all green",
        "--pattern",
        "FAILED",
        "--no-exit-nonzero",
        "--workdir",
        workdir(tmp),
        cwd=tmp,
    )
    code, output = run_cli("repro", "F-0001", "--yes", "--workdir", workdir(tmp), cwd=tmp)
    if code != 0 or "fixed" not in output:
        return "FAIL", f"fixed 应 exit 0：exit={code}"

    run_cli(
        "annotate",
        "F-0001",
        "--cmd",
        "echo FAILED x",
        "--pattern",
        "FAILED",
        "--exit-nonzero",
        "--workdir",
        workdir(tmp),
        cwd=tmp,
    )
    code, output = run_cli("repro", "F-0001", "--yes", "--workdir", workdir(tmp), cwd=tmp)
    if code != 2 or "inconclusive" not in output:
        return "FAIL", f"inconclusive 应 exit 2：exit={code}"
    return "PASS", "退出码契约 0=fixed / 1=reproduced / 2=inconclusive 或未执行（真实子进程 echo 级）"


def check_20_09_repro_history_and_status(tmp: Path) -> tuple[str, str]:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp), cwd=tmp)
    run_cli(
        "annotate",
        "F-0001",
        "--cmd",
        "echo FAILED x",
        "--pattern",
        "FAILED",
        "--no-exit-nonzero",
        "--workdir",
        workdir(tmp),
        cwd=tmp,
    )
    layout = WorkLayout(tmp / ".faillab")

    class _FakeSpawner:
        def __init__(self, *, output: str) -> None:
            self.output = output

        def __call__(self, command, *, cwd, timeout_s):
            return 0, self.output, False, True

    with Store(layout.db_path) as store:
        runner_broken = ReproRunner(layout=layout, store=store, spawner=_FakeSpawner(output="FAILED x"))
        runner_fixed = ReproRunner(layout=layout, store=store, spawner=_FakeSpawner(output="all green"))
        runner_broken.run(store.require_incident("F-0001"))
        runner_fixed.run(store.require_incident("F-0001"))
        incident = store.require_incident("F-0001")
        runs = store.list_repro_runs("F-0001")
    if len(runs) != 2:
        return "FAIL", f"复现历史条数不符：{len(runs)}"
    if incident.status != IncidentStatus.FIXED:
        return "FAIL", f"状态未随最新判定演进：{incident.status}"
    if incident.reproduction.reproduced_count != 1 or incident.reproduction.fixed_count != 1:
        return "FAIL", f"计数不符：{incident.reproduction.model_dump()}"
    _code, output = run_cli("show", "F-0001", "--workdir", workdir(tmp), cwd=tmp)
    if "复现历史" not in output:
        return "FAIL", "show 未展示复现历史"
    return "PASS", "2 次历史落库；状态随判定演进 reproduced → fixed；计数 1/1"


def check_20_10_report_and_list(tmp: Path) -> tuple[str, str]:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp), cwd=tmp)
    run_cli("import", str(TRACES / "result.json"), "--workdir", workdir(tmp), cwd=tmp)
    code, output = run_cli("report", "--workdir", workdir(tmp), cwd=tmp)
    if code != 0:
        return "FAIL", f"report 失败：exit={code}"
    html = (tmp / ".faillab" / "report.html").read_text(encoding="utf-8")
    if EXTERNAL_REF_RE.search(html):
        return "FAIL", "仪表盘存在外部资源引用"
    if "<svg" not in html:
        return "FAIL", "仪表盘缺少 SVG"
    payload = read_json(tmp / ".faillab" / "report.json")
    if payload["totals"]["incidents"] != 2 or payload["schema_version"] != "1.0":
        return "FAIL", f"report.json 不符：{payload['totals']}"

    code, output = run_cli("list", "--model", "gpt-5.2", "--workdir", workdir(tmp), cwd=tmp)
    if "F-0002" not in output or "F-0001" in output:
        return "FAIL", "list 过滤不正确"
    return "PASS", "HTML 自包含 + SVG；report.json 往返；list 过滤正确"


def check_20_11_bundle_roundtrip(tmp: Path) -> tuple[str, str]:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp), cwd=tmp)
    run_cli("import", str(TRACES / "result.json"), "--workdir", workdir(tmp), cwd=tmp)
    bundle = tmp / "bundle.json"
    code, output = run_cli("export", str(bundle), "--workdir", workdir(tmp), cwd=tmp)
    if code != 0:
        return "FAIL", f"export 失败：exit={code}"
    other = tmp / "other"
    other.mkdir(exist_ok=True)
    code, output = run_cli("import-bundle", str(bundle), "--workdir", workdir(other), cwd=other)
    if code != 0 or "新增 2" not in output:
        return "FAIL", f"bundle 导入失败：exit={code}；{output.strip()[-120:]}"
    with Store(Path(workdir(tmp)) / "faillab.db") as source, Store(Path(workdir(other)) / "faillab.db") as target:
        if source.count_incidents() != target.count_incidents():
            return "FAIL", "往返后数量不一致"
        for public_id in ("F-0001", "F-0002"):
            if source.require_incident(public_id).signature != target.require_incident(public_id).signature:
                return "FAIL", f"{public_id} 关键字段不一致"
    return "PASS", "export → import-bundle 往返（数量与签名一致）"


def check_20_12_quality_gate(_tmp: Path, *, skip_tests: bool) -> tuple[str, str]:
    steps = [
        ("ruff", [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"]),
        ("ruff format", [sys.executable, "-m", "ruff", "format", "--check", "src"]),
        ("pyright", [sys.executable, "-m", "pyright"]),
    ]
    if not skip_tests:
        steps.append(("pytest", [sys.executable, "-m", "pytest", "-q"]))
    failures: list[str] = []
    for name, argv in steps:
        proc = subprocess.run(argv, cwd=str(ROOT), capture_output=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        if proc.returncode != 0:
            tail = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()[-3:]
            failures.append(f"{name}: {' | '.join(tail)}")
    if failures:
        return "FAIL", "；".join(failures)
    names = " + ".join(name for name, _ in steps)
    return "PASS", f"{names} 全绿" + ("（pytest 由调用方执行）" if skip_tests else "")


def main() -> int:
    skip_tests = "--skip-tests" in sys.argv[1:]
    tmp_root = Path(tempfile.mkdtemp(prefix="faillab-mvp-"))
    checks = [
        ("20-1", check_20_01_import_jsonl),
        ("20-2", check_20_02_import_object_aliases),
        ("20-3", check_20_03_dedup),
        ("20-4", check_20_04_record_and_merge),
        ("20-5", check_20_05_analyze_offline),
        ("20-6", check_20_06_analyze_llm),
        ("20-7", check_20_07_repro_three_states),
        ("20-8", check_20_08_repro_cli_exit_codes),
        ("20-9", check_20_09_repro_history_and_status),
        ("20-10", check_20_10_report_and_list),
        ("20-11", check_20_11_bundle_roundtrip),
    ]
    try:
        for number, check in checks:
            try:
                check_dir = tmp_root / number
                check_dir.mkdir(parents=True, exist_ok=True)
                status, detail = check(check_dir)
            except Exception as exc:
                status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
            record(status, f"{number} {check.__name__.removeprefix('check_20_')}", detail)
        try:
            gate_dir = tmp_root / "20-12"
            gate_dir.mkdir(parents=True, exist_ok=True)
            status, detail = check_20_12_quality_gate(gate_dir, skip_tests=skip_tests)
        except Exception as exc:
            status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
        record(status, "20-12 quality_gate", detail)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    passed = sum(1 for status, _, _ in RESULTS if status == "PASS")
    skipped = sum(1 for status, _, _ in RESULTS if status == "SKIP")
    failed = sum(1 for status, _, _ in RESULTS if status == "FAIL")
    print(f"\n{passed} PASS / {skipped} SKIP / {failed} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
