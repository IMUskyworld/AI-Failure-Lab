"""SQLite store (DEVELOPMENT.md §7/§21): single source of truth + JSON bundle channel."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from faillab.domain.enums import IncidentStatus, ReproStatus
from faillab.domain.models import Incident, ReproRun, Trace
from faillab.errors import BUNDLE_INVALID, INCIDENT_NOT_FOUND, STORE_SCHEMA_INCOMPATIBLE, IncidentError, StoreError
from faillab.util import now_iso

SCHEMA_VERSION = "1"


def _is_newer(incoming: str, existing: str) -> bool:
    """Compare ISO timestamps as instants, not strings.

    ``2026-09-22T15:00:00+00:00`` is newer than ``2026-09-22T22:00:00+08:00``
    (same instant plus an hour) even though the lexical order says otherwise.
    """
    try:
        return datetime.fromisoformat(incoming) > datetime.fromisoformat(existing)
    except (TypeError, ValueError):
        return incoming > existing


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (
    public_id TEXT PRIMARY KEY,
    signature TEXT,
    status TEXT NOT NULL,
    repro_status TEXT NOT NULL,
    category TEXT NOT NULL,
    model TEXT NOT NULL,
    harness TEXT NOT NULL,
    severity TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    document TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_signature ON incidents(signature);
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    incident_id TEXT,
    imported_at TEXT NOT NULL,
    document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS repro_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL,
    document TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_repro_runs_incident ON repro_runs(incident_id, started_at);
"""


@dataclass
class ImportSummary:
    added: int = 0
    merged: int = 0  # incidents whose document was updated (bundle import)
    skipped: int = 0
    traces_added: int = 0
    repro_runs_added: int = 0
    notes: list[str] = field(default_factory=list)


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._check_schema()
        self._conn.commit()

    def _check_schema(self) -> None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
            return
        if row["value"] != SCHEMA_VERSION:
            raise StoreError(
                f"数据库 schema 版本不兼容：{row['value']}（当前支持 {SCHEMA_VERSION}）",
                code=STORE_SCHEMA_INCOMPATIBLE,
                details={"db": str(self._path), "found": row["value"], "expected": SCHEMA_VERSION},
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- incidents ---------------------------------------------------------

    def next_public_id(self) -> str:
        row = self._conn.execute("SELECT public_id FROM incidents ORDER BY public_id DESC LIMIT 1").fetchone()
        number = 1
        if row is not None:
            number = int(str(row["public_id"]).removeprefix("F-")) + 1
        return f"F-{number:04d}"

    def insert_incident(self, incident: Incident) -> None:
        self._conn.execute(
            """
            INSERT INTO incidents(public_id, signature, status, repro_status, category, model, harness,
                                  severity, updated_at, document)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                incident.public_id,
                incident.signature,
                incident.status.value,
                incident.reproduction.status.value,
                incident.category,
                incident.model,
                incident.harness,
                incident.severity.value,
                incident.updated_at,
                incident.model_dump_json(),
            ),
        )
        self._conn.commit()

    def update_incident(self, incident: Incident) -> None:
        cursor = self._conn.execute(
            """
            UPDATE incidents SET signature = ?, status = ?, repro_status = ?, category = ?, model = ?,
                                 harness = ?, severity = ?, updated_at = ?, document = ?
            WHERE public_id = ?
            """,
            (
                incident.signature,
                incident.status.value,
                incident.reproduction.status.value,
                incident.category,
                incident.model,
                incident.harness,
                incident.severity.value,
                incident.updated_at,
                incident.model_dump_json(),
                incident.public_id,
            ),
        )
        if cursor.rowcount == 0:
            raise IncidentError(f"事故不存在：{incident.public_id}", code=INCIDENT_NOT_FOUND)
        self._conn.commit()

    def get_incident(self, public_id: str) -> Incident | None:
        row = self._conn.execute("SELECT document FROM incidents WHERE public_id = ?", (public_id,)).fetchone()
        if row is None:
            return None
        return Incident.model_validate_json(row["document"])

    def require_incident(self, public_id: str) -> Incident:
        incident = self.get_incident(public_id)
        if incident is None:
            raise IncidentError(f"事故不存在：{public_id}", code=INCIDENT_NOT_FOUND, details={"public_id": public_id})
        return incident

    def find_by_signature(self, signature: str) -> Incident | None:
        if not signature:
            return None
        row = self._conn.execute(
            "SELECT document FROM incidents WHERE signature = ? ORDER BY updated_at DESC LIMIT 1",
            (signature,),
        ).fetchone()
        if row is None:
            return None
        return Incident.model_validate_json(row["document"])

    def list_incidents(
        self,
        *,
        category: str | None = None,
        model: str | None = None,
        harness: str | None = None,
        status: str | None = None,
        repro: str | None = None,
        query: str | None = None,
        limit: int | None = None,
    ) -> list[Incident]:
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (("category", category), ("model", model), ("harness", harness), ("status", status)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if repro:
            clauses.append("repro_status = ?")
            params.append(repro)
        if query:
            clauses.append("document LIKE ?")
            params.append(f"%{query}%")
        sql = "SELECT document FROM incidents"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY public_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [Incident.model_validate_json(row["document"]) for row in rows]

    def count_incidents(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM incidents").fetchone()
        return int(row["n"])

    # -- traces ------------------------------------------------------------

    def insert_trace(self, trace: Trace, incident_id: str | None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO traces(trace_id, incident_id, imported_at, document) VALUES(?, ?, ?, ?)",
            (trace.trace_id, incident_id, now_iso(), trace.model_dump_json()),
        )
        self._conn.commit()

    def get_trace(self, trace_id: str) -> Trace | None:
        row = self._conn.execute("SELECT document FROM traces WHERE trace_id = ?", (trace_id,)).fetchone()
        if row is None:
            return None
        return Trace.model_validate_json(row["document"])

    # -- repro runs --------------------------------------------------------

    def insert_repro_run(self, run: ReproRun) -> None:
        self._conn.execute(
            "INSERT INTO repro_runs(incident_id, started_at, status, document) VALUES(?, ?, ?, ?)",
            (run.incident_id, run.started_at, run.status.value, run.model_dump_json()),
        )
        self._conn.commit()

    def list_repro_runs(self, incident_id: str, *, limit: int | None = None) -> list[ReproRun]:
        # id DESC keeps same-second runs deterministic (started_at has second precision).
        sql = "SELECT document FROM repro_runs WHERE incident_id = ? ORDER BY started_at DESC, id DESC"
        params: list[object] = [incident_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [ReproRun.model_validate_json(row["document"]) for row in rows]

    # -- bundle ------------------------------------------------------------

    def export_bundle(self) -> dict:
        # ORDER BY keeps bundles byte-stable (git-friendly) regardless of insertion order.
        incidents = [
            json.loads(row["document"])
            for row in self._conn.execute("SELECT document FROM incidents ORDER BY public_id")
        ]
        traces = [
            json.loads(row["document"]) for row in self._conn.execute("SELECT document FROM traces ORDER BY trace_id")
        ]
        repro_runs = [
            json.loads(row["document"])
            for row in self._conn.execute("SELECT document FROM repro_runs ORDER BY incident_id, started_at, id")
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": "faillab",
            "exported_at": now_iso(),
            "incidents": incidents,
            "traces": traces,
            "repro_runs": repro_runs,
        }

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for status in IncidentStatus:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM incidents WHERE status = ?", (status.value,)).fetchone()
            counts[status.value] = int(row["n"])
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM incidents WHERE repro_status = ?", (ReproStatus.UNVERIFIED.value,)
        ).fetchone()
        counts["unverified"] = int(row["n"])
        return counts

    def import_bundle(self, payload: dict) -> ImportSummary:
        summary = ImportSummary()
        if not isinstance(payload, dict):
            raise StoreError("bundle 顶层必须是 JSON object", code=BUNDLE_INVALID)
        if str(payload.get("schema_version")) != SCHEMA_VERSION:
            raise StoreError(
                f"bundle schema 版本不兼容：{payload.get('schema_version')}",
                code=STORE_SCHEMA_INCOMPATIBLE,
                details={"found": payload.get("schema_version"), "expected": SCHEMA_VERSION},
            )
        collections: dict[str, list] = {}
        for key in ("incidents", "traces", "repro_runs"):
            value = payload.get(key, [])
            if value is None:
                value = []
            if not isinstance(value, list):
                raise StoreError(
                    f"bundle 字段 {key} 必须是数组",
                    code=BUNDLE_INVALID,
                    details={"field": key, "type": type(value).__name__},
                )
            collections[key] = value
        # Validate every document before writing anything: a malformed bundle must not
        # leave a half-imported database behind.
        try:
            incidents = [Incident.model_validate(document) for document in collections["incidents"]]
            traces = [Trace.model_validate(document) for document in collections["traces"]]
            repro_runs = [ReproRun.model_validate(document) for document in collections["repro_runs"]]
        except ValidationError as exc:
            errors = exc.errors(include_url=False)
            first = errors[0] if errors else {}
            raise StoreError(
                f"bundle 内容无效：{len(errors)} 处不符合 schema",
                code=BUNDLE_INVALID,
                details={
                    "errors": len(errors),
                    "first": {"loc": list(first.get("loc", [])), "msg": first.get("msg", "")},
                },
            ) from exc

        existing_runs = {
            (row["incident_id"], row["started_at"])
            for row in self._conn.execute("SELECT incident_id, started_at FROM repro_runs")
        }
        trace_owners: dict[str, str] = {}
        for incident in incidents:
            for trace_id in incident.traces:
                trace_owners.setdefault(trace_id, incident.public_id)

        for incident in incidents:
            existing = self.get_incident(incident.public_id)
            if existing is None:
                self.insert_incident(incident)
                summary.added += 1
            elif _is_newer(incident.updated_at, existing.updated_at):
                self.update_incident(incident)
                summary.merged += 1
            else:
                summary.skipped += 1
        for trace in traces:
            row = self._conn.execute("SELECT 1 FROM traces WHERE trace_id = ?", (trace.trace_id,)).fetchone()
            owner = trace_owners.get(trace.trace_id)
            if row is None:
                self._conn.execute(
                    "INSERT INTO traces(trace_id, incident_id, imported_at, document) VALUES(?, ?, ?, ?)",
                    (trace.trace_id, owner, now_iso(), trace.model_dump_json()),
                )
                summary.traces_added += 1
            elif owner is not None:
                # The incidents table is authoritative; only fill a missing link.
                self._conn.execute(
                    "UPDATE traces SET incident_id = ? WHERE trace_id = ? AND incident_id IS NULL",
                    (owner, trace.trace_id),
                )
        for run in repro_runs:
            key = (run.incident_id, run.started_at)
            if key in existing_runs:
                continue
            existing_runs.add(key)
            self.insert_repro_run(run)
            summary.repro_runs_added += 1
        self._conn.commit()
        return summary
