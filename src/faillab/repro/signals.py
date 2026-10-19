"""Reproduction signal evaluation (DEVELOPMENT.md §11.2) — deterministic three-state judgment."""

from __future__ import annotations

import re

from faillab.domain.enums import ReproStatus
from faillab.domain.models import ReproSpec
from faillab.errors import REPRO_SPEC_INVALID, FaillabError


def signals_configured(spec: ReproSpec) -> bool:
    return bool(spec.pattern) or bool(spec.expect_exit_nonzero)


def validate_pattern(pattern: str | None) -> None:
    """Reject an uncompilable pattern *before* a command is executed.

    Without this check an invalid regex escapes ``re.search`` as ``re.error`` during
    judging, which crashed the CLI with exit code 1 -- the same code that means
    ``reproduced`` in the public contract.
    """
    if pattern is None:
        return
    try:
        re.compile(pattern)
    except re.error as exc:
        raise FaillabError(
            f"失败信号正则无效：{pattern}（{exc.msg}）",
            code=REPRO_SPEC_INVALID,
            component="repro",
            details={"pattern": pattern},
        ) from exc


def evaluate(
    *,
    exit_code: int | None,
    timed_out: bool,
    started: bool,
    output: str,
    spec: ReproSpec,
) -> tuple[ReproStatus, bool | None, bool | None, str]:
    """Returns (status, pattern_matched, exit_nonzero_matched, note)."""
    if not started:
        return ReproStatus.INCONCLUSIVE, None, None, "命令未能启动"
    if timed_out:
        return ReproStatus.INCONCLUSIVE, None, None, "命令超时"
    if not signals_configured(spec):
        return ReproStatus.INCONCLUSIVE, None, None, "未配置任何失败信号（pattern / expect_exit_nonzero）"
    pattern_matched: bool | None = None
    if spec.pattern:
        try:
            pattern_matched = bool(re.search(spec.pattern, output))
        except re.error as exc:
            # Defensive: legacy/bundle data may predate CLI validation. Never let a
            # regex error turn into a crash (or an accidental exit code 1).
            return (
                ReproStatus.INCONCLUSIVE,
                None,
                None,
                f"失败信号正则无效：{spec.pattern}（{exc.msg}）——未做判定",
            )
    exit_nonzero_matched: bool | None = None
    if spec.expect_exit_nonzero:
        exit_nonzero_matched = exit_code not in (0, None)
    checks = [value for value in (pattern_matched, exit_nonzero_matched) if value is not None]
    if all(checks):
        return ReproStatus.REPRODUCED, pattern_matched, exit_nonzero_matched, "失败信号全部命中"
    if not any(checks):
        if exit_code not in (0, None):
            return (
                ReproStatus.INCONCLUSIVE,
                pattern_matched,
                exit_nonzero_matched,
                f"失败信号未出现，但命令以非零退出（exit={exit_code}）："
                "无法区分「已修复」与「命令本身出错」（真实事故 F-0002 的教训）",
            )
        return ReproStatus.FIXED, pattern_matched, exit_nonzero_matched, "命令正常结束，失败信号未出现"
    matched = []
    if pattern_matched:
        matched.append("pattern")
    if exit_nonzero_matched:
        matched.append("非零退出")
    return (
        ReproStatus.INCONCLUSIVE,
        pattern_matched,
        exit_nonzero_matched,
        f"信号部分命中（{', '.join(matched)}）",
    )
