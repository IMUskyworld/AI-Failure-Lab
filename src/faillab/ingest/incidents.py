"""Incident creation + signature dedup (DEVELOPMENT.md §9.4)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from faillab.domain.models import DetectionHit, Evidence, Incident, ReproSpec, Trace
from faillab.store import Store
from faillab.util import now_iso

_HEX_RE = re.compile(r"\b[0-9a-f]{7,}\b")
_NUMBER_RE = re.compile(r"\d+")
_PATH_RE = re.compile(r"(?:[a-zA-Z]:)?[\\/][^\s\"'|]+")
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_error(text: str) -> str:
    """Lowercase, mask hex/numbers/paths, collapse whitespace, cap at 200 chars (§9.4)."""
    lowered = text.lower()
    lowered = _HEX_RE.sub("h", lowered)
    lowered = _NUMBER_RE.sub("n", lowered)
    lowered = _PATH_RE.sub("path", lowered)
    lowered = _WS_RE.sub(" ", lowered).strip()
    return lowered[:200]


def incident_signature(failure_text: str, *, tools: list[str] | None = None) -> str:
    basis = normalize_error(failure_text or "unknown")
    raw = f"{basis}|{','.join(sorted(tools or []))}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def signature_for(trace: Trace) -> str:
    failed_tools = sorted({call.name.lower() for call in trace.tool_calls if call.error})
    return incident_signature(trace.error or trace.final_result or trace.task or "unknown", tools=failed_tools)


def slugify(title: str) -> str:
    candidate = "-".join(_WORD_RE.findall(title.lower())[:6])
    if len(candidate) < 3:
        candidate = f"failure-{hashlib.sha1(title.encode('utf-8')).hexdigest()[:8]}"
    return candidate


def _short(text: str, limit: int) -> str:
    one_line = _WS_RE.sub(" ", text).strip()
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def build_incident(
    trace: Trace,
    *,
    public_id: str,
    hits: list[DetectionHit],
    reproduce: ReproSpec | None = None,
    source: str = "import",
) -> Incident:
    task = trace.task or _short(trace.prompt, 60) or "未命名任务"
    failure_text = trace.error or trace.final_result or "检测到失败但无显式错误信息"
    title = f"{_short(task, 60)}：{_short(failure_text, 40)}"
    evidence = [Evidence(kind="detection", detail=f"规则 {hit.rule_id} 命中：{hit.detail}") for hit in hits]
    if trace.error:
        evidence.append(Evidence(kind="error", detail=_short(trace.error, 200)))
    error_calls = [call for call in trace.tool_calls if call.error]
    for call in error_calls[:5]:
        evidence.append(
            Evidence(kind="tool_call", detail=f"#{call.index} {call.name} → {_short(call.error or '', 120)}")
        )
    now = now_iso()
    return Incident(
        public_id=public_id,
        slug=slugify(title),
        title=title,
        task=task,
        failure=failure_text[:400],
        model=trace.model,
        harness=trace.harness,
        signature=signature_for(trace),
        evidence=evidence[:12],
        reproduce=reproduce,
        traces=[trace.trace_id],
        source=source,
        created_at=now,
        updated_at=now,
    )


@dataclass
class MergeOutcome:
    incident: Incident
    merged: bool


def upsert_incident(
    store: Store,
    trace: Trace,
    *,
    hits: list[DetectionHit],
    reproduce: ReproSpec | None = None,
    force_new: bool = False,
    source: str = "import",
) -> MergeOutcome:
    """Same signature → merge (occurrences++, trace linked); otherwise create a new incident."""
    signature = signature_for(trace)
    existing = None if force_new else store.find_by_signature(signature)
    if existing is not None:
        existing.occurrences += 1
        if trace.trace_id not in existing.traces:
            existing.traces.append(trace.trace_id)
        if reproduce is not None and existing.reproduce is None:
            existing.reproduce = reproduce
        existing.updated_at = now_iso()
        store.update_incident(existing)
        store.insert_trace(trace, existing.public_id)
        return MergeOutcome(incident=existing, merged=True)
    incident = build_incident(
        trace,
        public_id=store.next_public_id(),
        hits=hits,
        reproduce=reproduce,
        source=source,
    )
    store.insert_incident(incident)
    store.insert_trace(trace, incident.public_id)
    return MergeOutcome(incident=incident, merged=False)
