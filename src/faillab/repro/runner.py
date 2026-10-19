"""Reproduction runner (DEVELOPMENT.md §11): execute the recorded command, judge, persist."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from faillab.domain.enums import IncidentStatus, ReproStatus
from faillab.domain.models import Incident, ReproRun
from faillab.errors import INCIDENT_NO_REPRO, REPRO_SPEC_INVALID, FaillabError, IncidentError
from faillab.paths import WorkLayout
from faillab.repro.signals import evaluate, signals_configured, validate_pattern
from faillab.store import Store
from faillab.util import decode_bytes, now_iso

TERMINATE_GRACE_S = 5.0
DEFAULT_TIMEOUT_S = 300.0
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _signal_tree(proc: subprocess.Popen[bytes], *, force: bool) -> None:
    """Best-effort stop of the shell *and its children*.

    ``shell=True`` means the direct child is cmd.exe/sh; terminating only that PID can
    leave the actual test process running (a runaway timeout). Windows uses
    ``taskkill /T``; POSIX uses the process group created by ``start_new_session``.
    """
    if os.name == "nt":
        # Windows has no graceful console signal here; /F is what actually stops the
        # tree. Without /T, the cmd.exe shell dies but the real test process survives.
        args = ["taskkill", "/F", "/T", "/PID", str(proc.pid)]
        try:
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if proc.poll() is None:
            if force:
                proc.kill()
            else:
                proc.terminate()
        return
    killpg = getattr(os, "killpg", None)
    sig = getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM
    if killpg is not None:
        try:
            killpg(proc.pid, sig)
            return
        except OSError:
            pass
    if force:
        proc.kill()
    else:
        proc.terminate()


def spawn(
    command: str,
    *,
    cwd: Path,
    timeout_s: float,
) -> tuple[int | None, str, bool, bool]:
    """Run one command. Returns (exit_code, decoded_output, timed_out, started).

    ``shell=True`` is deliberate: on Windows, passing ``["cmd", "/c", command]`` makes
    Python escape the command's own quotes, so ``".venv\\Scripts\\python.exe" ...``
    reaches cmd as ``\\"...\\"`` and fails (real incident F-0004). Passing the raw
    command string to the shell keeps the user's quoting intact on every platform.
    The command is explicitly user-recorded and the confirmation gate is the boundary.

    Timeout: terminate the whole tree → 5s grace → force kill. Ctrl+C: same, then re-raise.
    """
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        return None, f"无法启动命令：{type(exc).__name__}: {exc}", False, False
    timed_out = False
    output: bytes | None = None
    try:
        output, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _signal_tree(proc, force=False)
        try:
            output, _ = proc.communicate(timeout=TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            _signal_tree(proc, force=True)
            proc.kill()
            output, _ = proc.communicate()
        if os.name != "nt":
            # The shell may have exited while a stubborn child ignored SIGTERM; the
            # process group still exists. A second, forced signal closes that gap.
            _signal_tree(proc, force=True)
    except KeyboardInterrupt:
        _signal_tree(proc, force=False)
        try:
            proc.wait(timeout=TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            _signal_tree(proc, force=True)
            proc.kill()
        if os.name != "nt":
            _signal_tree(proc, force=True)
        raise
    return proc.returncode, decode_bytes(output or b""), timed_out, True


class ReproRunner:
    def __init__(
        self,
        *,
        layout: WorkLayout,
        store: Store,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        timeout_override: float | None = None,
        spawner: Callable[..., tuple[int | None, str, bool, bool]] = spawn,
        echo: Callable[[str], None] | None = None,
    ) -> None:
        self._layout = layout
        self._store = store
        self._timeout_s = timeout_s
        self._timeout_override = timeout_override
        self._spawner = spawner
        self._echo = echo or (lambda _message: None)

    def _resolve_cwd(self, incident: Incident, cwd_override: str | None) -> Path:
        assert incident.reproduce is not None
        raw = cwd_override or incident.reproduce.cwd
        return Path(raw).expanduser().resolve() if raw else Path.cwd()

    def run(self, incident: Incident, *, cwd_override: str | None = None) -> ReproRun:
        spec = incident.reproduce
        if spec is None:
            raise IncidentError(
                f"{incident.public_id} 没有复现定义（reproduce）；可用 annotate 补充 --cmd/--pattern",
                code=INCIDENT_NO_REPRO,
                details={"public_id": incident.public_id},
            )
        if not signals_configured(spec):
            raise IncidentError(
                f"{incident.public_id} 的复现定义没有任何失败信号（pattern / --exit-nonzero）",
                code=REPRO_SPEC_INVALID,
                details={"public_id": incident.public_id},
            )
        if not spec.command.strip():
            raise IncidentError(
                f"{incident.public_id} 的复现命令为空",
                code=REPRO_SPEC_INVALID,
                details={"public_id": incident.public_id},
            )
        try:
            validate_pattern(spec.pattern)
        except FaillabError as exc:
            raise FaillabError(
                f"{incident.public_id}：{exc.message}",
                code=exc.code,
                component="repro",
                details=exc.details,
            ) from exc
        timeout_s = (
            self._timeout_override
            if self._timeout_override is not None
            else (spec.timeout_s if spec.timeout_s is not None else self._timeout_s)
        )
        if timeout_s <= 0:
            raise IncidentError(
                f"{incident.public_id} 的复现超时必须大于 0（当前 {timeout_s}s）",
                code=REPRO_SPEC_INVALID,
                details={"timeout_s": timeout_s},
            )
        cwd = self._resolve_cwd(incident, cwd_override)
        if not cwd.is_dir():
            raise IncidentError(
                f"复现目录不存在：{cwd}（可用 --cwd 覆盖）",
                code=REPRO_SPEC_INVALID,
                details={"cwd": str(cwd)},
            )
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        log_path = self._layout.repro_log_path(incident.public_id, stamp)
        started_at = now_iso()
        started_clock = time.perf_counter()
        self._echo(f"▶ 复现 {incident.public_id}：{spec.command}（cwd={cwd}）")
        try:
            exit_code, output, timed_out, started = self._spawner(spec.command, cwd=cwd, timeout_s=timeout_s)
        except KeyboardInterrupt:
            log_path.write_text("[用户中断]\n", encoding="utf-8", newline="\n")
            run = ReproRun(
                incident_id=incident.public_id,
                started_at=started_at,
                ended_at=now_iso(),
                duration_s=time.perf_counter() - started_clock,
                status=ReproStatus.INTERRUPTED,
                note="用户中断（Ctrl+C）",
                log_path=str(log_path),
            )
            self._store.insert_repro_run(run)
            raise
        duration_s = time.perf_counter() - started_clock
        log_path.write_text(output if output.endswith("\n") else output + "\n", encoding="utf-8", newline="\n")
        status, pattern_matched, exit_matched, note = evaluate(
            exit_code=exit_code,
            timed_out=timed_out,
            started=started,
            output=output,
            spec=spec,
        )
        run = ReproRun(
            incident_id=incident.public_id,
            started_at=started_at,
            ended_at=now_iso(),
            duration_s=duration_s,
            exit_code=exit_code,
            pattern_matched=pattern_matched,
            exit_nonzero_matched=exit_matched,
            status=status,
            note=note,
            log_path=str(log_path),
        )
        self._store.insert_repro_run(run)
        self._apply(incident, status)
        return run

    def _apply(self, incident: Incident, status: ReproStatus) -> None:
        reproduction = incident.reproduction
        reproduction.status = status
        reproduction.last_run_at = now_iso()
        reproduction.total_runs += 1
        if status == ReproStatus.REPRODUCED:
            reproduction.reproduced_count += 1
            incident.status = IncidentStatus.REPRODUCED
        elif status == ReproStatus.FIXED:
            reproduction.fixed_count += 1
            incident.status = IncidentStatus.FIXED
        elif status == ReproStatus.INCONCLUSIVE:
            reproduction.inconclusive_count += 1
        incident.updated_at = now_iso()
        self._store.update_incident(incident)
