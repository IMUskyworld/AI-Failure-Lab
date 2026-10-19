from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from faillab.analyze import analyze_incident, analyze_offline, apply_analysis
from faillab.analyze.taxonomy import UNCLASSIFIED_ID, load_taxonomy, match_category
from faillab.config import FaillabConfig
from faillab.domain.enums import Backend, Confidence, Severity
from faillab.domain.models import TaxonomyCategory, TaxonomySignature, ToolCall, Trace
from faillab.errors import LLM_REQUEST_FAILED, FaillabError, LlmError
from faillab.llm import LlmClient
from faillab.store import Store
from support import make_incident

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "traces"


def _trace(**overrides) -> Trace:
    base = Trace(trace_id="tr-x", task="任务")
    return base.model_copy(update=overrides)


def _trace_with_tool_error(error: str) -> Trace:
    return Trace(
        trace_id="tr-x",
        task="任务",
        tool_calls=[ToolCall(index=0, name="Edit", error=error)],
        final_result="[failed] done",
    )


CATEGORY_CASES = [
    ("retry_loop", _trace(retries=3)),
    ("hallucinated_api", _trace(error="ModuleNotFoundError: No module named 'oauthlib'")),
    ("tool_misuse", _trace(error="invalid argument: missing 'old_text'")),
    ("env_permission", _trace(error="command not found: yolo")),
    ("env_permission", _trace(error="SecurityError: UnauthorizedAccess - 禁止运行脚本")),
    ("env_permission", _trace(error="NotADirectoryError: [WinError 267] 目录名称无效")),
    ("encoding_locale", _trace(error="UnicodeDecodeError: 'utf-8' codec can't decode byte 0xcf")),
    ("wrong_file_edit", _trace(error="string not found in config.py")),
    ("context_misidentification", _trace(error="expected 'a' but found 'b'")),
    ("test_failure_ignored", _trace_with_tool_error("pytest: 2 tests failed")),
    ("premature_completion", _trace(final_result="implemented OAuth login")),
    ("regression_introduced", _trace(error="existing tests failed after the change")),
]


@pytest.mark.parametrize("expected_id,trace", CATEGORY_CASES, ids=[case[0] for case in CATEGORY_CASES])
def test_every_category_has_a_positive_case(expected_id: str, trace: Trace) -> None:
    match = match_category(trace)
    assert match is not None, f"{expected_id} 没有匹配到任何类别"
    assert match.category.id == expected_id


def test_taxonomy_order_prefers_specific_category() -> None:
    trace = Trace(
        trace_id="tr-x",
        task="Add OAuth",
        error="Edit failed: string not found in config.py",
        tool_calls=[ToolCall(index=0, name="Edit", error="string not found in config.py")],
        final_result="[failed] implemented OAuth",
        retries=1,
    )
    match = match_category(trace)
    assert match is not None
    assert match.category.id == "wrong_file_edit"  # 而不是 final 类的 premature/test_failure


def test_unclassified_when_nothing_matches() -> None:
    match = match_category(_trace(final_result="[failed] oops"))
    assert match is None


def test_match_category_rejects_invalid_custom_pattern() -> None:
    category = TaxonomyCategory(
        id="broken", label="坏类别", signatures=[TaxonomySignature(field="error", pattern="[unclosed")]
    )
    with pytest.raises(FaillabError) as excinfo:
        match_category(_trace(error="boom"), [category])
    assert excinfo.value.code == "TAXONOMY_INVALID"


def test_offline_analyze_matched_and_unclassified(tmp_path: Path) -> None:
    with Store(tmp_path / "faillab.db") as store:
        incident = make_incident("F-0001")
        store.insert_incident(incident)
        matched = analyze_offline(incident, _trace(error="ModuleNotFoundError: No module named 'x'"), store)
        assert matched.category == "hallucinated_api"
        assert matched.backend == Backend.OFFLINE
        assert matched.confidence == Confidence.HIGH
        assert matched.recovery

        unclassified = analyze_offline(incident, _trace(final_result="[failed] oops"), store)
        assert unclassified.category == UNCLASSIFIED_ID
        assert unclassified.confidence == Confidence.LOW
        assert "无匹配签名" in unclassified.reason


def test_offline_analyze_manual_incident_without_trace(tmp_path: Path) -> None:
    with Store(tmp_path / "faillab.db") as store:
        incident = make_incident("F-0001")
        incident.failure = "Bash: command not found: yolo"
        store.insert_incident(incident)
        result = analyze_offline(incident, None, store)
    assert result.category == "env_permission"
    assert result.backend == Backend.OFFLINE


def test_offline_analyze_precedents(tmp_path: Path) -> None:
    with Store(tmp_path / "faillab.db") as store:
        first = make_incident("F-0001", category="hallucinated_api", category_label="幻觉 API/符号", occurrences=3)
        store.insert_incident(first)
        second = make_incident("F-0002")
        store.insert_incident(second)
        result = analyze_offline(second, _trace(error="AttributeError: has no attribute 'load'"), store)
        assert result.category == "hallucinated_api"
        assert result.precedents == ["F-0001"]


def test_apply_analysis_persists(tmp_path: Path) -> None:
    with Store(tmp_path / "faillab.db") as store:
        incident = make_incident("F-0001")
        store.insert_incident(incident)
        result = analyze_offline(incident, _trace(error="command not found: yolo"), store)
        apply_analysis(incident, result, store)
        stored = store.require_incident("F-0001")
        assert stored.category == "env_permission"
        assert stored.recovery
        assert stored.analyzed_by == Backend.OFFLINE


# --- LLM backend --------------------------------------------------------------


def _client(handler) -> tuple[LlmClient, FaillabConfig]:
    config = FaillabConfig()
    config.llm.enabled = True
    config.llm.base_url = "https://llm.example/v1"
    config.llm.model = "fake"
    return LlmClient(config, transport=httpx.MockTransport(handler)), config


LLM_PAYLOAD = {
    "category": "env_permission",
    "reason": "错误文本为 command not found，属于环境缺失",
    "recovery": "先安装 yolo 并确认 PATH",
    "severity": "medium",
    "confidence": "high",
}


def test_llm_analyze_valid(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FAILLAB_LLM_API_KEY", "test-key")
    client, _config = _client(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(LLM_PAYLOAD)}}]})
    )
    with Store(tmp_path / "faillab.db") as store:
        incident = make_incident("F-0001")
        store.insert_incident(incident)
        result = analyze_incident(
            incident, trace=_trace(error="command not found: yolo"), store=store, backend=Backend.LLM, client=client
        )
        assert result.backend == Backend.LLM
        assert result.category == "env_permission"
        assert result.fallback_reason is None


def test_llm_analyze_out_of_whitelist_falls_back(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FAILLAB_LLM_API_KEY", "test-key")
    payload = dict(LLM_PAYLOAD, category="made_up_category")
    client, _config = _client(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})
    )
    with Store(tmp_path / "faillab.db") as store:
        incident = make_incident("F-0001")
        store.insert_incident(incident)
        result = analyze_incident(
            incident,
            trace=_trace(error="command not found: yolo"),
            store=store,
            backend=Backend.LLM,
            client=client,
        )
        assert result.backend == Backend.OFFLINE
        assert result.category == "env_permission"  # 离线规则兜底
        assert result.fallback_reason is not None and "白名单" in result.fallback_reason


def test_llm_analyze_garbage_falls_back(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FAILLAB_LLM_API_KEY", "test-key")
    client, _config = _client(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "这不是 JSON"}}]})
    )
    with Store(tmp_path / "faillab.db") as store:
        incident = make_incident("F-0001")
        store.insert_incident(incident)
        result = analyze_incident(
            incident,
            trace=_trace(error="string not found in config.py"),
            store=store,
            backend=Backend.LLM,
            client=client,
        )
        assert result.fallback_reason is not None and "LLM 降级" in result.fallback_reason
        assert result.category == "wrong_file_edit"


def test_llm_analyze_http_error_falls_back(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FAILLAB_LLM_API_KEY", "test-key")
    client, _config = _client(lambda request: httpx.Response(500, text="boom"))
    with Store(tmp_path / "faillab.db") as store:
        incident = make_incident("F-0001")
        store.insert_incident(incident)
        result = analyze_incident(incident, trace=None, store=store, backend=Backend.LLM, client=client)
        assert result.fallback_reason is not None


class _FlakyClient:
    def __init__(self, *, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def chat_json(self, *, system: str, user: str, max_tokens: int = 900) -> dict:
        del system, user, max_tokens
        self.calls += 1
        if self.calls <= self.failures:
            raise LlmError("transient boom", code=LLM_REQUEST_FAILED)
        return LLM_PAYLOAD


def test_llm_retries_once_then_succeeds(tmp_path: Path) -> None:
    client = _FlakyClient(failures=1)
    with Store(tmp_path / "faillab.db") as store:
        incident = make_incident("F-0001")
        store.insert_incident(incident)
        result = analyze_incident(
            incident,
            trace=_trace(error="command not found: yolo"),
            store=store,
            backend=Backend.LLM,
            client=client,  # type: ignore[arg-type]
        )
    assert client.calls == 2
    assert result.backend == Backend.LLM
    assert result.fallback_reason is None


def test_llm_gives_up_after_one_retry_and_falls_back(tmp_path: Path) -> None:
    client = _FlakyClient(failures=2)
    with Store(tmp_path / "faillab.db") as store:
        incident = make_incident("F-0001")
        store.insert_incident(incident)
        result = analyze_incident(
            incident,
            trace=_trace(error="command not found: yolo"),
            store=store,
            backend=Backend.LLM,
            client=client,  # type: ignore[arg-type]
        )
    assert client.calls == 2
    assert result.backend == Backend.OFFLINE
    assert result.fallback_reason is not None and "已尝试 2 次" in result.fallback_reason


def test_llm_unclassified_allowed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FAILLAB_LLM_API_KEY", "test-key")
    payload = dict(LLM_PAYLOAD, category="unclassified", confidence="low")
    client, _config = _client(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})
    )
    with Store(tmp_path / "faillab.db") as store:
        incident = make_incident("F-0001")
        store.insert_incident(incident)
        result = analyze_incident(incident, trace=None, store=store, backend=Backend.LLM, client=client)
        assert result.category == UNCLASSIFIED_ID
        assert result.category_label == "未分类"


def test_taxonomy_file_loads() -> None:
    categories = load_taxonomy()
    ids = [category.id for category in categories]
    assert len(ids) == len(set(ids)) >= 9
    for category in categories:
        assert category.signatures, f"{category.id} 没有签名"
        assert category.recovery_template, f"{category.id} 没有恢复模板"


def test_severity_enum_values_align() -> None:
    assert {item.value for item in Severity} == {"low", "medium", "high", "critical"}
