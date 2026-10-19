from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from faillab.domain.enums import IncidentStatus, ReproStatus
from faillab.domain.models import ReproSpec
from faillab.errors import IncidentError, StoreError
from faillab.store import Store
from support import make_incident, make_repro_run, make_trace


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "faillab.db")


def test_public_id_sequence(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        assert store.next_public_id() == "F-0001"
        store.insert_incident(make_incident("F-0001"))
        assert store.next_public_id() == "F-0002"
        store.insert_incident(make_incident("F-0002"))
        assert store.next_public_id() == "F-0003"


def test_insert_get_require_and_update(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        incident = make_incident()
        store.insert_incident(incident)
        fetched = store.get_incident("F-0001")
        assert fetched is not None and fetched.title == incident.title
        assert store.get_incident("F-9999") is None
        with pytest.raises(IncidentError) as excinfo:
            store.require_incident("F-9999")
        assert excinfo.value.code == "INCIDENT_NOT_FOUND"

        fetched.status = IncidentStatus.REPRODUCED
        fetched.reproduction.status = ReproStatus.REPRODUCED
        store.update_incident(fetched)
        again = store.require_incident("F-0001")
        assert again.status == IncidentStatus.REPRODUCED
        assert again.reproduction.status == ReproStatus.REPRODUCED

        with pytest.raises(IncidentError):
            store.update_incident(make_incident("F-0404"))


def test_find_by_signature(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.insert_incident(make_incident("F-0001", signature="sig-abc"))
        found = store.find_by_signature("sig-abc")
        assert found is not None and found.public_id == "F-0001"
        assert store.find_by_signature("sig-none") is None
        assert store.find_by_signature("") is None


def test_list_filters(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.insert_incident(
            make_incident("F-0001", category="retry_loop", model="m-a", harness="h-a", title="重试风暴")
        )
        store.insert_incident(
            make_incident(
                "F-0002",
                category="tool_misuse",
                model="m-b",
                harness="h-b",
                status=IncidentStatus.FIXED,
                repro_status=ReproStatus.FIXED,
                title="工具误用",
            )
        )
        assert [i.public_id for i in store.list_incidents()] == ["F-0001", "F-0002"]
        assert [i.public_id for i in store.list_incidents(category="retry_loop")] == ["F-0001"]
        assert [i.public_id for i in store.list_incidents(status="fixed")] == ["F-0002"]
        assert [i.public_id for i in store.list_incidents(repro="fixed")] == ["F-0002"]
        assert [i.public_id for i in store.list_incidents(model="m-a")] == ["F-0001"]
        assert [i.public_id for i in store.list_incidents(harness="h-b")] == ["F-0002"]
        assert [i.public_id for i in store.list_incidents(query="重试")] == ["F-0001"]
        assert len(store.list_incidents(limit=1)) == 1


def test_traces_roundtrip(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.insert_trace(make_trace("tr-1"), "F-0001")
        fetched = store.get_trace("tr-1")
        assert fetched is not None and fetched.error == "boom"
        assert store.get_trace("tr-none") is None


def test_repro_runs_history_order(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.insert_repro_run(make_repro_run(started_at="2026-09-22T10:00:00+08:00"))
        store.insert_repro_run(make_repro_run(started_at="2026-09-22T12:00:00+08:00"))
        runs = store.list_repro_runs("F-0001")
        assert [run.started_at for run in runs] == ["2026-09-22T12:00:00+08:00", "2026-09-22T10:00:00+08:00"]
        assert len(store.list_repro_runs("F-0001", limit=1)) == 1


def test_status_counts(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.insert_incident(make_incident("F-0001"))
        store.insert_incident(make_incident("F-0002", status=IncidentStatus.FIXED, repro_status=ReproStatus.FIXED))
        counts = store.status_counts()
        assert counts["open"] == 1
        assert counts["fixed"] == 1
        assert counts["unverified"] == 1


def test_bundle_roundtrip(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    target_dir = tmp_path / "dst"
    with _store(source_dir) as source:
        source.insert_incident(make_incident("F-0001", title="事故一"))
        source.insert_incident(make_incident("F-0002", title="事故二", updated_at="2026-09-23T00:00:00+08:00"))
        source.insert_trace(make_trace("tr-1"), "F-0001")
        source.insert_repro_run(make_repro_run())
        bundle = source.export_bundle()

    with _store(target_dir) as target:
        summary = target.import_bundle(bundle)
        assert summary.added == 2
        assert summary.traces_added == 1
        assert summary.repro_runs_added == 1
        assert target.count_incidents() == 2

        again = target.import_bundle(bundle)
        assert again.added == 0 and again.skipped == 2 and again.repro_runs_added == 0

        # 更新的文档会被合并
        bundle["incidents"][0]["title"] = "事故一（更新）"
        bundle["incidents"][0]["updated_at"] = "2026-10-01T00:00:00+08:00"
        merged = target.import_bundle(bundle)
        assert merged.merged == 1
        assert target.require_incident("F-0001").title == "事故一（更新）"


def test_bundle_restores_trace_owner_links(tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    target_dir = tmp_path / "dst"
    with _store(source_dir) as source:
        source.insert_incident(make_incident("F-0001"))
        source.insert_trace(make_trace("tr-1"), "F-0001")
        bundle = source.export_bundle()

    with _store(target_dir) as target:
        target.import_bundle(bundle)
        row = target._conn.execute("SELECT incident_id FROM traces WHERE trace_id = 'tr-1'").fetchone()
        assert row is not None and row["incident_id"] == "F-0001"


def test_bundle_export_order_is_stable(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.insert_incident(make_incident("F-0002"))
        store.insert_incident(make_incident("F-0001"))
        bundle = store.export_bundle()
    assert [item["public_id"] for item in bundle["incidents"]] == ["F-0001", "F-0002"]


def test_bundle_compares_timestamps_as_instants(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        store.insert_incident(
            make_incident("F-0001", title="旧", updated_at="2026-09-22T22:00:00+08:00")
        )
        incoming = make_incident("F-0001", title="新", updated_at="2026-09-22T15:00:00+00:00")
        bundle = {
            "schema_version": "1",
            "incidents": [incoming.model_dump(mode="json")],
            "traces": [],
            "repro_runs": [],
        }
        summary = store.import_bundle(bundle)
        assert summary.merged == 1
        assert store.require_incident("F-0001").title == "新"


def test_bundle_rejects_malformed_documents(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        with pytest.raises(StoreError) as excinfo:
            store.import_bundle(
                {
                    "schema_version": "1",
                    "incidents": [{"public_id": "F-0001"}],
                    "traces": [],
                    "repro_runs": [],
                }
            )
        assert excinfo.value.code == "BUNDLE_INVALID"

        with pytest.raises(StoreError) as excinfo:
            store.import_bundle(
                {
                    "schema_version": "1",
                    "incidents": {},
                    "traces": [],
                    "repro_runs": [],
                }  # type: ignore[dict-item]
            )
        assert excinfo.value.code == "BUNDLE_INVALID"


def test_bundle_requires_matching_schema(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        with pytest.raises(StoreError) as excinfo:
            store.import_bundle({"schema_version": "999", "incidents": []})
        assert excinfo.value.code == "STORE_SCHEMA_INCOMPATIBLE"


def test_schema_version_guard(tmp_path: Path) -> None:
    path = tmp_path / "faillab.db"
    Store(path).close()
    conn = sqlite3.connect(str(path))
    conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError) as excinfo:
        Store(path)
    assert excinfo.value.code == "STORE_SCHEMA_INCOMPATIBLE"


def test_repro_spec_roundtrip_in_incident(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        spec = ReproSpec(cwd="C:/proj", command="pytest -q", pattern="FAILED", timeout_s=120)
        store.insert_incident(make_incident(reproduce=spec))
        fetched = store.require_incident("F-0001")
        assert fetched.reproduce is not None
        assert fetched.reproduce.pattern == "FAILED"
        assert fetched.reproduce.expect_exit_nonzero is True
