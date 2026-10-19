"""Trace import service (DEVELOPMENT.md §9): parse → detect → dedup → persist."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from faillab.domain.models import DetectionHit, Incident, ReproSpec, Trace
from faillab.ingest.detection import detect
from faillab.ingest.formats import parse_trace_file
from faillab.ingest.incidents import signature_for, upsert_incident
from faillab.store import Store


@dataclass
class ImportOutcome:
    trace: Trace
    hits: list[DetectionHit] = field(default_factory=list)
    incident: Incident | None = None
    merged: bool = False
    skipped_existing_trace: bool = False
    dry_run: bool = False
    would_merge_into: str | None = None

    @property
    def is_failure(self) -> bool:
        return bool(self.hits)


def import_file(
    path: Path,
    *,
    store: Store | None,
    harness: str | None = None,
    model: str | None = None,
    reproduce: ReproSpec | None = None,
    force_new: bool = False,
    dry_run: bool = False,
) -> ImportOutcome:
    trace: Trace = parse_trace_file(path, harness=harness, model=model)
    hits = detect(trace)
    if dry_run:
        existing_trace = store.get_trace(trace.trace_id) if store is not None else None
        would_merge_into = None
        if store is not None and existing_trace is None and hits:
            found = store.find_by_signature(signature_for(trace))
            would_merge_into = found.public_id if found is not None else None
        return ImportOutcome(
            trace=trace,
            hits=hits,
            dry_run=True,
            skipped_existing_trace=existing_trace is not None,
            would_merge_into=would_merge_into,
        )
    if store is None:
        raise ValueError("非 dry-run 导入需要 store")  # pragma: no cover - guarded by CLI
    if store.get_trace(trace.trace_id) is not None:
        return ImportOutcome(trace=trace, hits=hits, skipped_existing_trace=True)
    if not hits:
        store.insert_trace(trace, None)  # 成功会话也留档，不带事故
        return ImportOutcome(trace=trace, hits=hits)
    merge_outcome = upsert_incident(
        store,
        trace,
        hits=hits,
        reproduce=reproduce,
        force_new=force_new,
    )
    return ImportOutcome(trace=trace, hits=hits, incident=merge_outcome.incident, merged=merge_outcome.merged)
