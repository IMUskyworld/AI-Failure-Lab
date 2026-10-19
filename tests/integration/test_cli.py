"""CLI end-to-end: import/list/show/analyze/report/export chains and exit codes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from faillab.store import Store

ROOT = Path(__file__).resolve().parents[2]
DEV = ROOT / "scripts" / "faillab_dev.py"
TRACES = ROOT / "tests" / "fixtures" / "traces"
EXTERNAL_REF_RE = re.compile(r'(?:src|href)\s*=\s*"https?://')


def run_cli(
    *args: str,
    cwd: Path,
    env_extra: dict[str, str] | None = None,
    strip_llm_env: bool = False,
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


def workdir(tmp_path: Path) -> str:
    return str(tmp_path / ".faillab")


def test_cli_version(tmp_path: Path) -> None:
    code, output = run_cli("version", cwd=tmp_path)
    assert code == 0
    assert "faillab" in output


def test_import_creates_and_lists(tmp_path: Path) -> None:
    code, output = run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0, output
    assert "生成事故 F-0001" in output
    assert "error_present" in output  # 命中规则展示

    code, output = run_cli("list", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0
    assert "F-0001" in output and "未分类" in output

    code, output = run_cli("show", "F-0001", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0
    assert "证据" in output
    assert "没有复现定义" in output


def test_import_dedup_and_idempotency(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    code, output = run_cli("import", str(TRACES / "dup.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0
    assert "已合并到 F-0001" in output
    assert "occurrences=2" in output

    code, output = run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0
    assert "幂等跳过" in output


def test_import_clean_and_dry_run(tmp_path: Path) -> None:
    code, output = run_cli("import", str(TRACES / "clean.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0
    assert "未检测到失败" in output

    code, output = run_cli(
        "import", str(TRACES / "result.json"), "--dry-run", "--workdir", workdir(tmp_path), cwd=tmp_path
    )
    assert code == 0
    assert "将生成新事故" in output
    with Store(Path(workdir(tmp_path)) / "faillab.db") as store:
        assert store.count_incidents() == 0  # dry-run 无写入


def test_record_and_merge(tmp_path: Path) -> None:
    code, output = run_cli(
        "record",
        "--title",
        "手动事故",
        "--failure",
        "Bash: command not found: yolo",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
    )
    assert code == 0
    assert "生成事故 F-0001" in output

    code, output = run_cli(
        "record",
        "--title",
        "手动事故（再次遇到）",
        "--failure",
        "Bash: command not found: yolo",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
    )
    assert code == 0
    assert "已合并到 F-0001" in output


def test_annotate_sets_repro_and_status(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    code, output = run_cli(
        "annotate",
        "F-0001",
        "--cmd",
        "echo FAILED x",
        "--pattern",
        "FAILED",
        "--no-exit-nonzero",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
    )
    assert code == 0
    assert "pattern=FAILED" in output

    code, output = run_cli("show", "F-0001", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert "复现定义" in output and "echo FAILED x" in output


def test_analyze_pending_offline(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    code, output = run_cli("analyze", "--pending", "--backend", "offline", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0, output
    assert "F-0001" in output
    assert "改错文件/位置" in output  # taxonomy 命中
    assert "根因" in output

    code, output = run_cli("show", "F-0001", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert "改错文件/位置" in output


def test_repro_gate_non_interactive(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    run_cli(
        "annotate",
        "F-0001",
        "--cmd",
        "echo FAILED x",
        "--pattern",
        "FAILED",
        "--no-exit-nonzero",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
    )
    code, output = run_cli("repro", "F-0001", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 2
    assert "非交互终端" in output


def test_repro_without_spec_is_structured(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    code, output = run_cli("repro", "F-0001", "--yes", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 2  # 未执行，符合 §4 退出码契约
    assert "INCIDENT_NO_REPRO" in output


def test_repro_without_signals_exits_2(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    run_cli(
        "annotate",
        "F-0001",
        "--cmd",
        "echo hi",
        "--no-exit-nonzero",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
    )
    code, output = run_cli("repro", "F-0001", "--yes", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 2
    assert "REPRO_SPEC_INVALID" in output


def test_invalid_pattern_is_rejected_at_write_time(tmp_path: Path) -> None:
    code, output = run_cli(
        "record",
        "--title",
        "坏正则",
        "--failure",
        "boom",
        "--cmd",
        "echo x",
        "--pattern",
        "[unclosed",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
    )
    assert code == 3
    assert "REPRO_SPEC_INVALID" in output
    with Store(Path(workdir(tmp_path)) / "faillab.db") as store:
        assert store.count_incidents() == 0  # 未写库


def test_record_status_is_supported(tmp_path: Path) -> None:
    code, output = run_cli(
        "record",
        "--title",
        "人工标记",
        "--failure",
        "已知不修",
        "--status",
        "wontfix",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
    )
    assert code == 0, output
    with Store(Path(workdir(tmp_path)) / "faillab.db") as store:
        assert store.require_incident("F-0001").status.value == "wontfix"


def test_invalid_backend_is_rejected(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    code, output = run_cli(
        "analyze", "F-0001", "--backend", "bogus", "--workdir", workdir(tmp_path), cwd=tmp_path
    )
    assert code == 3
    assert "INVALID_ARGUMENT" in output


def test_status_and_report(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    code, output = run_cli("status", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0
    assert "总览" in output

    code, output = run_cli("report", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0, output
    html = (tmp_path / ".faillab" / "report.html").read_text(encoding="utf-8")
    assert EXTERNAL_REF_RE.search(html) is None
    assert "<svg" in html
    payload = json.loads((tmp_path / ".faillab" / "report.json").read_text(encoding="utf-8"))
    assert payload["totals"]["incidents"] == 1


def test_export_and_import_bundle_roundtrip(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    run_cli("import", str(TRACES / "result.json"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    bundle = tmp_path / "bundle.json"
    code, output = run_cli("export", str(bundle), "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0
    assert "已导出" in output

    other = tmp_path / "other"
    other.mkdir()
    code, output = run_cli("import-bundle", str(bundle), "--workdir", workdir(other), cwd=other)
    assert code == 0, output
    assert "新增 2" in output
    with Store(Path(workdir(other)) / "faillab.db") as store:
        assert store.count_incidents() == 2

    code, output = run_cli("import-bundle", str(bundle), "--workdir", workdir(other), cwd=other)
    assert "新增 0" in output and "跳过 2" in output


def test_show_missing_incident_is_structured(tmp_path: Path) -> None:
    code, output = run_cli("show", "F-9999", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 3
    assert "INCIDENT_NOT_FOUND" in output


def test_list_filters(tmp_path: Path) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    run_cli("import", str(TRACES / "result.json"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    run_cli("analyze", "F-0001", "--backend", "offline", "--workdir", workdir(tmp_path), cwd=tmp_path)
    run_cli("analyze", "F-0002", "--backend", "offline", "--workdir", workdir(tmp_path), cwd=tmp_path)

    code, output = run_cli("list", "--query", "OAuth", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0
    assert "F-0001" in output and "F-0002" not in output

    code, output = run_cli("list", "--model", "gpt-5.2", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert "F-0002" in output and "F-0001" not in output

    code, output = run_cli("list", "--repro", "unverified", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert "F-0001" in output and "F-0002" in output
