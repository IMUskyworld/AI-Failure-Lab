"""Plumbing tests: the repro lab really spawns subprocesses and judges with real signals.

This is NOT an agent experiment — it is the one place where the real subprocess path
is exercised locally (DEVELOPMENT.md §17).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from test_cli import TRACES, run_cli, workdir

from faillab.domain.enums import IncidentStatus, ReproStatus
from faillab.domain.models import ReproSpec
from faillab.paths import WorkLayout
from faillab.repro.runner import ReproRunner
from faillab.store import Store
from support import make_incident

pytestmark = pytest.mark.slow


def test_real_repro_reproduced_then_fixed(tmp_path: Path) -> None:
    layout = WorkLayout(tmp_path / ".faillab")
    with Store(layout.db_path) as store:
        still_broken = ReproSpec(
            cwd=str(tmp_path),
            command="echo FAILED test_oauth",
            pattern="FAILED",
            expect_exit_nonzero=False,
            timeout_s=30,
        )
        store.insert_incident(make_incident("F-0001", reproduce=still_broken))
        runner = ReproRunner(layout=layout, store=store)

        run = runner.run(store.require_incident("F-0001"))
        assert run.status == ReproStatus.REPRODUCED
        assert run.pattern_matched is True
        assert run.log_path and "FAILED test_oauth" in Path(run.log_path).read_text(encoding="utf-8")
        assert store.require_incident("F-0001").status == IncidentStatus.REPRODUCED

        repaired = ReproSpec(
            cwd=str(tmp_path),
            command="echo all green",
            pattern="FAILED",
            expect_exit_nonzero=False,
            timeout_s=30,
        )
        store.insert_incident(make_incident("F-0002", reproduce=repaired))
        fixed_run = runner.run(store.require_incident("F-0002"))
        assert fixed_run.status == ReproStatus.FIXED
        assert store.require_incident("F-0002").status == IncidentStatus.FIXED


def test_real_repro_quoted_executable_command(tmp_path: Path) -> None:
    """Real incident F-0004: ``cmd /c`` used to mangle a command starting with a quote."""
    command = f'"{sys.executable}" -c "print(\'FAILED quoted\')"'
    layout = WorkLayout(tmp_path / ".faillab")
    with Store(layout.db_path) as store:
        spec = ReproSpec(
            cwd=str(tmp_path),
            command=command,
            pattern="FAILED",
            expect_exit_nonzero=False,
            timeout_s=30,
        )
        store.insert_incident(make_incident("F-0001", reproduce=spec))
        run = ReproRunner(layout=layout, store=store).run(store.require_incident("F-0001"))
        assert run.status == ReproStatus.REPRODUCED
        assert run.exit_code == 0
        assert run.pattern_matched is True
        assert run.log_path and "FAILED quoted" in Path(run.log_path).read_text(encoding="utf-8")


def test_real_repro_timeout_kills_process_tree(tmp_path: Path) -> None:
    """A timed-out shell command must not leave its child process running."""
    started = tmp_path / "started.txt"
    survived = tmp_path / "survived.txt"
    code = (
        "import pathlib, time; "
        f"pathlib.Path(r'{started}').write_text('started'); "
        "time.sleep(3.0); "
        f"pathlib.Path(r'{survived}').write_text('survived')"
    )
    layout = WorkLayout(tmp_path / ".faillab")
    with Store(layout.db_path) as store:
        spec = ReproSpec(
            cwd=str(tmp_path),
            command=f'"{sys.executable}" -c "{code}"',
            pattern="NEVER",
            expect_exit_nonzero=False,
            timeout_s=2.0,
        )
        store.insert_incident(make_incident("F-0001", reproduce=spec))
        run = ReproRunner(layout=layout, store=store).run(store.require_incident("F-0001"))
        assert run.status == ReproStatus.INCONCLUSIVE
        assert "超时" in run.note
        assert started.is_file()  # the child really started before the timeout
    time.sleep(3.2)
    assert not survived.exists()  # ...and the whole tree was stopped


def _annotate(tmp_path: Path, command: str, *, pattern: str = "FAILED", exit_nonzero: bool = False) -> None:
    run_cli("import", str(TRACES / "session.jsonl"), "--workdir", workdir(tmp_path), cwd=tmp_path)
    args = ["annotate", "F-0001", "--cmd", command, "--pattern", pattern, "--workdir", workdir(tmp_path)]
    if not exit_nonzero:
        args.append("--no-exit-nonzero")
    code, output = run_cli(*args, cwd=tmp_path)
    assert code == 0, output


def test_cli_repro_exit_codes(tmp_path: Path) -> None:
    # reproduced → exit 1
    _annotate(tmp_path, "echo FAILED test_oauth")
    code, output = run_cli("repro", "F-0001", "--yes", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 1, output
    assert "reproduced" in output

    # 修复后重放 → fixed → exit 0（同一事故，历史追加）
    code, output = run_cli(
        "annotate",
        "F-0001",
        "--cmd",
        "echo all green",
        "--pattern",
        "FAILED",
        "--no-exit-nonzero",
        "--workdir",
        workdir(tmp_path),
        cwd=tmp_path,
    )
    assert code == 0
    code, output = run_cli("repro", "F-0001", "--yes", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 0, output
    assert "fixed" in output
    assert "1 fixed" in output  # 本次调用的汇总（按次统计，不累计历史）

    code, output = run_cli("show", "F-0001", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert "复现历史" in output
    assert "失败信号全部命中" in output  # 第一条 run：reproduced 的说明
    assert "命令正常结束" in output  # 第二条 run：fixed 的说明


def test_cli_repro_inconclusive_exit_code(tmp_path: Path) -> None:
    # pattern 命中但退码为 0 且要求非零 → 部分命中 → inconclusive → exit 2
    _annotate(tmp_path, "echo FAILED x", exit_nonzero=True)
    code, output = run_cli("repro", "F-0001", "--yes", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 2, output
    assert "inconclusive" in output


def test_cli_repro_all_contract(tmp_path: Path) -> None:
    _annotate(tmp_path, "echo FAILED test_oauth")
    code, output = run_cli("repro", "--all", "--yes", "--workdir", workdir(tmp_path), cwd=tmp_path)
    assert code == 1, output  # 存在 reproduced
    assert "汇总" in output


def test_cli_repro_all_skips_broken_records(tmp_path: Path) -> None:
    """One missing cwd must not block the remaining incidents in a --all run."""
    target = workdir(tmp_path)

    def record(title: str, failure: str, cwd: Path) -> tuple[int, str]:
        return run_cli(
            "record",
            "--title",
            title,
            "--failure",
            failure,
            "--cmd",
            "echo all green",
            "--pattern",
            "FAILED",
            "--no-exit-nonzero",
            "--cwd",
            str(cwd),
            "--workdir",
            target,
            cwd=tmp_path,
        )

    code, output = record("good", "good failure", tmp_path)
    assert code == 0, output
    code, output = record("bad", "bad failure", tmp_path / "missing")
    assert code == 0, output

    code, output = run_cli("repro", "--all", "--yes", "--workdir", target, cwd=tmp_path)
    assert code == 2, output
    flat = output.replace("\r", "").replace("\n", "")
    assert "F-0002 未执行" in flat
    assert "1 fixed" in flat
    assert "1 未执行" in flat
