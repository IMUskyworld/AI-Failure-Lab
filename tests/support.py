"""Shared builders for tests (importable thanks to pytest pythonpath=["tests"])."""

from __future__ import annotations

from faillab.domain.enums import Confidence, IncidentStatus, ReproStatus, Severity
from faillab.domain.models import Evidence, Incident, Reproduction, ReproRun, ReproSpec, ToolCall, Trace

TIMESTAMP = "2026-09-22T22:00:00+08:00"


def make_incident(
    public_id: str = "F-0001",
    *,
    signature: str = "sig1",
    title: str = "示例事故",
    category: str = "unclassified",
    category_label: str = "未分类",
    model: str = "model-a",
    harness: str = "harness-a",
    status: IncidentStatus = IncidentStatus.OPEN,
    repro_status: ReproStatus = ReproStatus.UNVERIFIED,
    occurrences: int = 1,
    updated_at: str = TIMESTAMP,
    reproduce: ReproSpec | None = None,
) -> Incident:
    return Incident(
        public_id=public_id,
        slug=f"slug-{public_id.lower()}",
        title=title,
        task="任务",
        failure="失败描述",
        category=category,
        category_label=category_label,
        reason="原因",
        recovery="恢复建议",
        severity=Severity.MEDIUM,
        confidence=Confidence.MEDIUM,
        model=model,
        harness=harness,
        signature=signature,
        occurrences=occurrences,
        status=status,
        evidence=[Evidence(kind="detection", detail="rule hit")],
        reproduce=reproduce,
        reproduction=Reproduction(status=repro_status),
        traces=["tr-1"],
        created_at=TIMESTAMP,
        updated_at=updated_at,
    )


def make_trace(trace_id: str = "tr-1", *, error: str | None = "boom", retries: int = 0) -> Trace:
    return Trace(
        trace_id=trace_id,
        harness="harness-a",
        model="model-a",
        task="任务",
        prompt="提示",
        tool_calls=[ToolCall(index=0, name="Edit", arguments="file=a.py", result="ok")],
        error=error,
        retries=retries,
        final_result="failed" if error else "ok",
    )


def make_repro_run(
    incident_id: str = "F-0001", *, started_at: str = TIMESTAMP, status: ReproStatus = ReproStatus.REPRODUCED
) -> ReproRun:
    return ReproRun(
        incident_id=incident_id,
        started_at=started_at,
        ended_at=TIMESTAMP,
        duration_s=1.0,
        exit_code=1,
        pattern_matched=True,
        exit_nonzero_matched=True,
        status=status,
        note="note",
    )
