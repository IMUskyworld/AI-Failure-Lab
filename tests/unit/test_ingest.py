from __future__ import annotations

from pathlib import Path

import pytest

from faillab.domain.enums import TraceFormat
from faillab.domain.models import ToolCall, Trace
from faillab.errors import FaillabError, TraceError
from faillab.ingest import import_file
from faillab.ingest.detection import detect, is_failure
from faillab.ingest.formats import detect_format, parse_jsonl_events, parse_trace_file
from faillab.ingest.incidents import build_incident, normalize_error, signature_for, slugify
from faillab.store import Store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "traces"


# --- formats ------------------------------------------------------------------


def test_parse_jsonl_events_session() -> None:
    trace = parse_trace_file(FIXTURES / "session.jsonl")
    assert trace.harness == "claude-code"
    assert trace.model == "claude-sonnet-4.5"
    assert trace.task == "Add OAuth login to the API"
    assert trace.cwd == "C:/projects/api"
    assert len(trace.tool_calls) == 3
    edit_call = trace.tool_calls[1]
    assert edit_call.name == "Edit" and edit_call.error == "string not found in config.py"
    assert trace.retries == 1
    assert trace.error == "Edit failed: string not found in config.py"
    assert trace.final_result == "[failed] implemented OAuth"


def test_parse_json_object_aliases() -> None:
    trace = parse_trace_file(FIXTURES / "result.json")
    assert trace.model == "gpt-5.2"
    assert trace.harness == "qoder"
    assert trace.task == "Add rate limiting to the endpoint"
    assert trace.cwd == "C:/projects/svc"
    assert trace.retries == 2
    assert trace.error == "ToolError: invalid argument missing old_text"
    assert trace.final_result == "[failed] gave up after two attempts"
    assert trace.tool_calls[1].error is not None


def test_parse_object_agent_alias_maps_to_harness(tmp_path: Path) -> None:
    payload = tmp_path / "agent.json"
    payload.write_text(
        '{"agent": "claude-code", "model_name": "claude-sonnet-4.5", "task": "x", "error": "boom"}',
        encoding="utf-8",
    )
    trace = parse_trace_file(payload)
    assert trace.harness == "claude-code"
    assert trace.model == "claude-sonnet-4.5"


def test_parse_object_missing_fields_warns() -> None:
    trace = parse_trace_file(FIXTURES / "clean.jsonl")
    assert trace.harness == "claude-code"  # meta 行提供
    minimal = parse_jsonl_events('{"kind": "prompt", "text": "hi"}\n', trace_id="tr-x")
    assert minimal.harness == "unknown"
    assert minimal.warnings == []  # 文件级解析器才补 warning；此处为纯解析


def test_format_detection(tmp_path: Path) -> None:
    assert detect_format(Path("a.jsonl"), "x") == TraceFormat.JSONL_EVENTS
    assert detect_format(Path("a.json"), "{}") == TraceFormat.JSON_OBJECT
    assert detect_format(Path("a.txt"), '{"kind": "task"}\n') == TraceFormat.JSONL_EVENTS
    assert detect_format(Path("a.txt"), "not json") == TraceFormat.JSON_OBJECT


def test_parse_errors(tmp_path: Path) -> None:
    with pytest.raises(TraceError) as missing:
        parse_trace_file(tmp_path / "nope.jsonl")
    assert missing.value.code == "TRACE_FILE_NOT_FOUND"

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(TraceError) as empty_exc:
        parse_trace_file(empty)
    assert empty_exc.value.code == "TRACE_EMPTY"

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(TraceError) as bad_exc:
        parse_trace_file(bad)
    assert bad_exc.value.code == "TRACE_PARSE_FAILED"


def test_gbk_transcript_is_decoded(tmp_path: Path) -> None:
    path = tmp_path / "gbk.jsonl"
    path.write_bytes(
        '{"kind": "task", "task": "修复编码问题"}\n{"kind": "error", "message": "UnicodeDecodeError"}\n'.encode("gbk")
    )
    trace = parse_trace_file(path)
    assert trace.task == "修复编码问题"
    assert trace.error == "UnicodeDecodeError"


def test_bad_lines_become_warnings(tmp_path: Path) -> None:
    path = tmp_path / "mixed.jsonl"
    path.write_text('not json\n{"kind": "task", "task": "x"}\n', encoding="utf-8")
    trace = parse_trace_file(path)
    assert any(note.code == "BAD_LINE" for note in trace.warnings)
    assert trace.task == "x"


# --- detection ----------------------------------------------------------------


def test_detection_rules_on_session() -> None:
    trace = parse_trace_file(FIXTURES / "session.jsonl")
    hits = detect(trace)
    rule_ids = {hit.rule_id for hit in hits}
    assert rule_ids == {"error_present", "final_failed", "tool_errors"}
    assert is_failure(trace) is True


def test_detection_retry_storm_threshold() -> None:
    trace = Trace(trace_id="tr-x", retries=3, final_result="done")
    hits = detect(trace)
    assert any(hit.rule_id == "retry_storm" for hit in hits)

    calm = Trace(trace_id="tr-y", retries=1, final_result="done")
    assert not any(hit.rule_id == "retry_storm" for hit in detect(calm))


def test_detection_clean_trace_is_not_failure() -> None:
    trace = parse_trace_file(FIXTURES / "clean.jsonl")
    assert is_failure(trace) is False


def test_detection_object_fixture() -> None:
    trace = parse_trace_file(FIXTURES / "result.json")
    rule_ids = {hit.rule_id for hit in detect(trace)}
    assert {"error_present", "final_failed", "tool_errors"} <= rule_ids


def test_detection_rejects_invalid_custom_rule() -> None:
    trace = Trace(trace_id="tr-x", final_result="boom")
    with pytest.raises(FaillabError) as excinfo:
        detect(trace, [{"id": "bad", "when": "final_result", "pattern": "[unclosed"}])
    assert excinfo.value.code == "DETECTION_RULES_INVALID"


# --- signature / normalize ----------------------------------------------------


def test_normalize_error_masks_variance() -> None:
    text = r"Failed at C:\Users\world\proj\app.py:42 after 3 retries (deadbeef1234)"
    normalized = normalize_error(text)
    assert "world" not in normalized
    assert "42" not in normalized
    assert "deadbeef1234" not in normalized
    assert "path" in normalized
    assert len(normalize_error("x" * 500)) == 200


def test_signature_stable_and_discriminating() -> None:
    session = parse_trace_file(FIXTURES / "session.jsonl")
    dup = parse_trace_file(FIXTURES / "dup.jsonl")
    assert signature_for(session) == signature_for(dup)

    other = parse_trace_file(FIXTURES / "result.json")
    assert signature_for(other) != signature_for(session)


def test_slugify_cjk_fallback() -> None:
    assert slugify("Add OAuth login：Edit failed").startswith("add-oauth")
    assert slugify("中文标题只有汉字").startswith("failure-")


# --- import service + dedup ---------------------------------------------------


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "faillab.db")


def test_import_creates_incident(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        outcome = import_file(FIXTURES / "session.jsonl", store=store)
        assert outcome.incident is not None and not outcome.merged
        incident = outcome.incident
        assert incident.public_id == "F-0001"
        assert incident.occurrences == 1
        assert incident.harness == "claude-code"
        assert incident.category == "unclassified"
        assert any(evidence.kind == "detection" for evidence in incident.evidence)
        assert store.get_trace(outcome.trace.trace_id) is not None


def test_import_dedup_merges_same_signature(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        first = import_file(FIXTURES / "session.jsonl", store=store)
        second = import_file(FIXTURES / "dup.jsonl", store=store)
        assert second.merged is True
        assert second.incident is not None
        assert second.incident.public_id == first.incident.public_id  # type: ignore[union-attr]
        assert second.incident.occurrences == 2
        assert len(second.incident.traces) == 2
        assert store.count_incidents() == 1


def test_import_same_file_is_idempotent(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        import_file(FIXTURES / "session.jsonl", store=store)
        again = import_file(FIXTURES / "session.jsonl", store=store)
        assert again.skipped_existing_trace is True
        assert again.incident is None
        assert store.count_incidents() == 1
        assert store.require_incident("F-0001").occurrences == 1


def test_import_clean_trace_archives_without_incident(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        outcome = import_file(FIXTURES / "clean.jsonl", store=store)
        assert outcome.incident is None
        assert outcome.is_failure is False
        assert store.count_incidents() == 0
        assert store.get_trace(outcome.trace.trace_id) is not None


def test_import_force_new(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        import_file(FIXTURES / "session.jsonl", store=store)
        forced = import_file(FIXTURES / "dup.jsonl", store=store, force_new=True)
        assert forced.incident is not None
        assert forced.incident.public_id == "F-0002"
        assert forced.merged is False


def test_import_dry_run_writes_nothing(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        outcome = import_file(FIXTURES / "session.jsonl", store=store, dry_run=True)
        assert outcome.dry_run is True
        assert outcome.incident is None
        assert store.count_incidents() == 0

        import_file(FIXTURES / "session.jsonl", store=store)
        merge_preview = import_file(FIXTURES / "dup.jsonl", store=store, dry_run=True)
        assert merge_preview.would_merge_into == "F-0001"
        assert store.count_incidents() == 1


def test_import_overrides_harness_and_model(tmp_path: Path) -> None:
    with _store(tmp_path) as store:
        outcome = import_file(FIXTURES / "session.jsonl", store=store, harness="custom-harness", model="custom-model")
        assert outcome.trace.harness == "custom-harness"
        assert outcome.trace.model == "custom-model"
        assert outcome.incident is not None
        assert outcome.incident.harness == "custom-harness"


def test_build_incident_title_and_evidence() -> None:
    trace = parse_trace_file(FIXTURES / "session.jsonl")
    hits = detect(trace)
    incident = build_incident(trace, public_id="F-0001", hits=hits)
    assert incident.title.startswith("Add OAuth login to the API")
    assert "Edit failed" in incident.title or "Edit failed" in incident.failure
    kinds = {evidence.kind for evidence in incident.evidence}
    assert {"detection", "error", "tool_call"} <= kinds
    assert incident.status.value == "open"
    assert incident.reproduction.status.value == "unverified"


def test_tool_call_error_records_evidence() -> None:
    trace = Trace(
        trace_id="tr-synth",
        task="任务",
        tool_calls=[ToolCall(index=0, name="Bash", result=None, error="command not found: yolo")],
        final_result="[failed] gave up",
    )
    hits = detect(trace)
    incident = build_incident(trace, public_id="F-0001", hits=hits)
    assert any("Bash" in evidence.detail for evidence in incident.evidence)
