from __future__ import annotations

from pathlib import Path

import pytest

from faillab.domain.enums import IncidentStatus, ReproStatus
from faillab.domain.models import ReproSpec
from faillab.errors import FaillabError, IncidentError
from faillab.paths import WorkLayout
from faillab.repro.runner import ReproRunner
from faillab.repro.signals import evaluate, signals_configured, validate_pattern
from faillab.store import Store
from support import make_incident


def _spec(pattern: str | None = "FAILED", expect_nonzero: bool = True, cwd: str | None = None) -> ReproSpec:
    return ReproSpec(cwd=cwd, command="pytest -q", pattern=pattern, expect_exit_nonzero=expect_nonzero, timeout_s=30)


# --- signal evaluation --------------------------------------------------------


def test_evaluate_reproduced() -> None:
    status, pattern, nonzero, note = evaluate(
        exit_code=1, timed_out=False, started=True, output="FAILED test_oauth", spec=_spec()
    )
    assert status == ReproStatus.REPRODUCED
    assert pattern is True and nonzero is True
    assert "全部命中" in note


def test_evaluate_fixed() -> None:
    status, pattern, nonzero, note = evaluate(
        exit_code=0, timed_out=False, started=True, output="2 passed", spec=_spec()
    )
    assert status == ReproStatus.FIXED
    assert pattern is False and nonzero is False
    assert "未出现" in note


def test_evaluate_pattern_only() -> None:
    spec = _spec(expect_nonzero=False)
    status, pattern, nonzero, _ = evaluate(exit_code=0, timed_out=False, started=True, output="FAILED x", spec=spec)
    assert status == ReproStatus.REPRODUCED
    assert pattern is True and nonzero is None


def test_evaluate_exit_only() -> None:
    spec = _spec(pattern=None)
    status, pattern, nonzero, _ = evaluate(exit_code=2, timed_out=False, started=True, output="oops", spec=spec)
    assert status == ReproStatus.REPRODUCED
    assert pattern is None and nonzero is True

    fixed_spec = _spec(pattern=None)
    status_fixed, _, nonzero_fixed, _ = evaluate(
        exit_code=0, timed_out=False, started=True, output="fine", spec=fixed_spec
    )
    assert status_fixed == ReproStatus.FIXED
    assert nonzero_fixed is False


def test_evaluate_nonzero_without_signal_is_inconclusive() -> None:
    """真实事故 F-0002 的教训：命令非零退出且失败信号未出现时，不能判 fixed。"""
    spec = _spec(expect_nonzero=False)
    status, pattern, nonzero, note = evaluate(
        exit_code=1, timed_out=False, started=True, output="不是内部或外部命令", spec=spec
    )
    assert status == ReproStatus.INCONCLUSIVE
    assert pattern is False and nonzero is None
    assert "非零退出" in note


def test_evaluate_partial_hit_is_inconclusive() -> None:
    status, pattern, nonzero, note = evaluate(
        exit_code=0, timed_out=False, started=True, output="FAILED in output", spec=_spec()
    )
    assert status == ReproStatus.INCONCLUSIVE
    assert pattern is True and nonzero is False  # pattern 命中、退码为 0
    assert "部分命中" in note


def test_evaluate_timeout_and_start_failure_and_no_signals() -> None:
    status, *_ = evaluate(exit_code=None, timed_out=True, started=True, output="", spec=_spec())
    assert status == ReproStatus.INCONCLUSIVE

    status_start, *_ = evaluate(exit_code=None, timed_out=False, started=False, output="", spec=_spec())
    assert status_start == ReproStatus.INCONCLUSIVE

    bare = ReproSpec(command="echo hi", pattern=None, expect_exit_nonzero=False)
    status_none, *_ = evaluate(exit_code=0, timed_out=False, started=True, output="hi", spec=bare)
    assert status_none == ReproStatus.INCONCLUSIVE


def test_signals_configured() -> None:
    assert signals_configured(_spec())
    assert signals_configured(_spec(pattern=None))
    assert not signals_configured(ReproSpec(command="x", pattern=None, expect_exit_nonzero=False))


def test_invalid_pattern_is_rejected_before_execution() -> None:
    with pytest.raises(FaillabError) as excinfo:
        validate_pattern("[unclosed")
    assert excinfo.value.code == "REPRO_SPEC_INVALID"


def test_invalid_pattern_never_crashes_evaluation() -> None:
    """Legacy/bundle data: a bad regex must be inconclusive, not an uncaught re.error."""
    status, pattern, nonzero, note = evaluate(
        exit_code=1, timed_out=False, started=True, output="anything", spec=_spec(pattern="[bad")
    )
    assert status == ReproStatus.INCONCLUSIVE
    assert pattern is None and nonzero is None
    assert "正则无效" in note


# --- runner with injected spawner ---------------------------------------------


class FakeSpawner:
    def __init__(
        self,
        *,
        exit_code: int | None = 1,
        output: str = "FAILED x",
        timed_out: bool = False,
        started: bool = True,
        interrupt: bool = False,
    ) -> None:
        self.exit_code = exit_code
        self.output = output
        self.timed_out = timed_out
        self.started = started
        self.interrupt = interrupt
        self.calls: list[tuple[str, Path, float]] = []

    def __call__(self, command, *, cwd, timeout_s):
        self.calls.append((command, cwd, timeout_s))
        if self.interrupt:
            raise KeyboardInterrupt
        return self.exit_code, self.output, self.timed_out, self.started


def _runner(tmp_path: Path, spawner: FakeSpawner) -> tuple[ReproRunner, Store, WorkLayout]:
    layout = WorkLayout(tmp_path / ".faillab")
    store = Store(layout.db_path)
    return ReproRunner(layout=layout, store=store, spawner=spawner), store, layout


def test_runner_reproduced_updates_status_and_persists(tmp_path: Path) -> None:
    runner, store, _layout = _runner(tmp_path, FakeSpawner(exit_code=1, output="FAILED test_oauth"))
    with store:
        incident = make_incident(reproduce=_spec(cwd=str(tmp_path)))
        store.insert_incident(incident)
        run = runner.run(store.require_incident("F-0001"))
        assert run.status == ReproStatus.REPRODUCED
        assert run.pattern_matched is True
        assert run.exit_code == 1
        assert run.log_path and Path(run.log_path).is_file()
        assert "FAILED test_oauth" in Path(run.log_path).read_text(encoding="utf-8")

        updated = store.require_incident("F-0001")
        assert updated.status == IncidentStatus.REPRODUCED
        assert updated.reproduction.status == ReproStatus.REPRODUCED
        assert updated.reproduction.total_runs == 1
        assert updated.reproduction.reproduced_count == 1
        assert len(store.list_repro_runs("F-0001")) == 1


def test_runner_fixed_updates_status(tmp_path: Path) -> None:
    runner, store, _layout = _runner(tmp_path, FakeSpawner(exit_code=0, output="2 passed"))
    with store:
        store.insert_incident(make_incident(reproduce=_spec(cwd=str(tmp_path))))
        run = runner.run(store.require_incident("F-0001"))
        assert run.status == ReproStatus.FIXED
        updated = store.require_incident("F-0001")
        assert updated.status == IncidentStatus.FIXED
        assert updated.reproduction.fixed_count == 1


def test_runner_inconclusive_keeps_status(tmp_path: Path) -> None:
    runner, store, _layout = _runner(tmp_path, FakeSpawner(exit_code=0, output="FAILED x"))  # 部分命中
    with store:
        store.insert_incident(make_incident(reproduce=_spec(cwd=str(tmp_path))))
        run = runner.run(store.require_incident("F-0001"))
        assert run.status == ReproStatus.INCONCLUSIVE
        updated = store.require_incident("F-0001")
        assert updated.status == IncidentStatus.OPEN  # 不复现也不判 fixed
        assert updated.reproduction.inconclusive_count == 1


def test_runner_requires_reproduce_spec(tmp_path: Path) -> None:
    runner, store, _layout = _runner(tmp_path, FakeSpawner())
    with store:
        store.insert_incident(make_incident())
        with pytest.raises(IncidentError) as excinfo:
            runner.run(store.require_incident("F-0001"))
        assert excinfo.value.code == "INCIDENT_NO_REPRO"


def test_runner_requires_signals(tmp_path: Path) -> None:
    runner, store, _layout = _runner(tmp_path, FakeSpawner())
    with store:
        bare = ReproSpec(command="echo hi", pattern=None, expect_exit_nonzero=False, cwd=str(tmp_path))
        store.insert_incident(make_incident(reproduce=bare))
        with pytest.raises(IncidentError) as excinfo:
            runner.run(store.require_incident("F-0001"))
        assert excinfo.value.code == "REPRO_SPEC_INVALID"


def test_runner_timeout_override_wins_over_recorded_value(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    layout = WorkLayout(tmp_path / ".faillab")
    store = Store(layout.db_path)
    runner = ReproRunner(layout=layout, store=store, spawner=spawner, timeout_override=7)
    with store:
        store.insert_incident(make_incident(reproduce=_spec(cwd=str(tmp_path))))  # spec 里是 30s
        runner.run(store.require_incident("F-0001"))
    assert spawner.calls[0][2] == 7


def test_runner_rejects_invalid_spec_before_spawning(tmp_path: Path) -> None:
    spawner = FakeSpawner()
    runner, store, _layout = _runner(tmp_path, spawner)
    with store:
        store.insert_incident(
            make_incident(reproduce=ReproSpec(command="echo x", pattern="[bad", cwd=str(tmp_path)))
        )
        with pytest.raises(FaillabError) as excinfo:
            runner.run(store.require_incident("F-0001"))
        assert excinfo.value.code == "REPRO_SPEC_INVALID"
        assert spawner.calls == []

        store.insert_incident(
            make_incident(
                "F-0002",
                reproduce=ReproSpec(command="   ", pattern="FAILED", cwd=str(tmp_path)),
            )
        )
        with pytest.raises(IncidentError):
            runner.run(store.require_incident("F-0002"))
        assert spawner.calls == []

        store.insert_incident(
            make_incident(
                "F-0003",
                reproduce=ReproSpec(command="echo x", pattern="FAILED", timeout_s=0, cwd=str(tmp_path)),
            )
        )
        with pytest.raises(IncidentError):
            runner.run(store.require_incident("F-0003"))
        assert spawner.calls == []


def test_runner_cwd_override_and_missing_cwd(tmp_path: Path) -> None:
    runner, store, _layout = _runner(tmp_path, FakeSpawner())
    with store:
        spec = _spec(cwd=str(tmp_path / "does-not-exist"))
        store.insert_incident(make_incident(reproduce=spec))
        with pytest.raises(IncidentError):
            runner.run(store.require_incident("F-0001"))

        run = runner.run(store.require_incident("F-0001"), cwd_override=str(tmp_path))
        assert run.status == ReproStatus.REPRODUCED


def test_runner_interrupt_records_interrupted(tmp_path: Path) -> None:
    runner, store, _layout = _runner(tmp_path, FakeSpawner(interrupt=True))
    with store:
        store.insert_incident(make_incident(reproduce=_spec(cwd=str(tmp_path))))
        with pytest.raises(KeyboardInterrupt):
            runner.run(store.require_incident("F-0001"))
        runs = store.list_repro_runs("F-0001")
        assert runs and runs[0].status == ReproStatus.INTERRUPTED
        assert store.require_incident("F-0001").status == IncidentStatus.OPEN


def test_default_cwd_when_spec_has_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner, store, _layout = _runner(tmp_path, FakeSpawner())
    with store:
        store.insert_incident(make_incident(reproduce=_spec(cwd=None)))
        run = runner.run(store.require_incident("F-0001"))
        assert run.status == ReproStatus.REPRODUCED
