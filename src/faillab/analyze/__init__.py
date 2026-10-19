"""Analysis orchestration (DEVELOPMENT.md §10): offline taxonomy + LLM with whitelist/fallback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from faillab.analyze.taxonomy import (
    UNCLASSIFIED_ID,
    UNCLASSIFIED_LABEL,
    category_label,
    load_taxonomy,
    match_category,
)
from faillab.domain.enums import Backend, Confidence, Severity
from faillab.domain.models import AnalysisResult, Incident, Trace
from faillab.errors import LLM_DISABLED, LLM_OUTPUT_INVALID, LlmError
from faillab.llm import LlmClient, redact
from faillab.store import Store
from faillab.util import now_iso

ANALYZE_SYSTEM = (
    "你是 AI coding agent 的失败分析助手。给定一次失败会话的证据（任务、错误、工具调用、检测规则命中），"
    "输出严格 JSON（不要任何额外文字）：\n"
    '{"category": str, "reason": str, "recovery": str, "severity": "low"|"medium"|"high"|"critical", '
    '"confidence": "high"|"medium"|"low"}\n'
    "约束：category 必须从给定候选列表中选择（没有把握就选 unclassified）；reason 必须引用给定证据中的具体文本；"
    "recovery 给出可操作的一两步建议；不要发明证据之外的事实。"
)

MAX_PRECEDENTS = 3
MAX_LLM_ATTEMPTS = 2  # initial attempt + 1 retry (DEVELOPMENT.md §10)


@dataclass
class _Digest:
    task: str
    failure: str
    error: str
    tool_summary: list[str]
    evidence: list[str]
    final_result: str


def _digest(incident: Incident, trace: Trace | None) -> _Digest:
    tool_summary: list[str] = []
    evidence: list[str] = []
    if trace is not None:
        for call in trace.tool_calls:
            line = f"#{call.index} {call.name}"
            if call.error:
                line += f" → error: {call.error[:120]}"
            tool_summary.append(line)
        evidence = [f"{item.kind}: {item.detail}" for item in incident.evidence][:8]
    return _Digest(
        task=incident.task,
        failure=incident.failure,
        error=(trace.error if trace and trace.error else incident.failure),
        tool_summary=tool_summary[:12],
        evidence=evidence,
        final_result=(trace.final_result if trace and trace.final_result else ""),
    )


def _precedents(store: Store, incident: Incident, category_id: str) -> list[str]:
    others = [
        item for item in store.list_incidents() if item.public_id != incident.public_id and item.category == category_id
    ]
    others.sort(key=lambda item: item.occurrences, reverse=True)
    return [item.public_id for item in others[:MAX_PRECEDENTS]]


def analyze_offline(incident: Incident, trace: Trace | None, store: Store) -> AnalysisResult:
    if trace is None:
        # Manual `record` incidents have no trace; classify what we do have instead
        # of unconditionally returning unclassified.
        trace = Trace(trace_id=f"incident-{incident.public_id}", task=incident.task, error=incident.failure or None)
    match = match_category(trace)
    if match is None:
        return AnalysisResult(
            category=UNCLASSIFIED_ID,
            category_label=UNCLASSIFIED_LABEL,
            reason="无匹配签名（可用 --backend llm 或人工分类）",
            recovery="",
            severity=Severity.MEDIUM,
            confidence=Confidence.LOW,
            backend=Backend.OFFLINE,
            precedents=_precedents(store, incident, UNCLASSIFIED_ID),
        )
    confidence = Confidence.HIGH if match.field in {"error", "tool_error"} else Confidence.MEDIUM
    snippet = ""
    if trace is not None:
        snippet = (trace.error or trace.final_result or "")[:80]
    reason = f"匹配签名 {match.field}:{match.pattern}" + (f"（{snippet}）" if snippet else "")
    return AnalysisResult(
        category=match.category.id,
        category_label=match.category.label,
        reason=reason,
        recovery=match.category.recovery_template,
        severity=match.category.severity,
        confidence=confidence,
        backend=Backend.OFFLINE,
        precedents=_precedents(store, incident, match.category.id),
    )


def _build_prompt(incident: Incident, digest: _Digest) -> str:
    allowed = [category.id for category in load_taxonomy()] + [UNCLASSIFIED_ID]
    lines = [
        f"任务：{redact(digest.task)[:200]}",
        f"失败描述：{redact(digest.failure)[:300]}",
        f"错误：{redact(digest.error)[:300]}",
    ]
    if digest.final_result:
        lines.append(f"最终结果：{redact(digest.final_result)[:200]}")
    if digest.tool_summary:
        lines.append("工具调用：")
        lines.extend(f"- {redact(item)}" for item in digest.tool_summary)
    if digest.evidence:
        lines.append("检测证据：")
        lines.extend(f"- {redact(item)}" for item in digest.evidence)
    lines.append(f"候选 category：{', '.join(allowed)}")
    return "\n".join(lines)


def _parse_llm_payload(payload: dict[str, Any]) -> tuple[str, str, str, Severity, Confidence]:
    allowed = {category.id for category in load_taxonomy()} | {UNCLASSIFIED_ID}
    category = str(payload.get("category", "")).strip()
    if category not in allowed:
        raise LlmError(
            f"LLM 给出的 category 不在白名单：{category or '（空）'}",
            code=LLM_OUTPUT_INVALID,
        )
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        raise LlmError("LLM 输出缺少 reason", code=LLM_OUTPUT_INVALID)
    recovery = str(payload.get("recovery", "")).strip()
    severity_text = str(payload.get("severity", "medium")).lower()
    confidence_text = str(payload.get("confidence", "medium")).lower()
    if severity_text not in {item.value for item in Severity}:
        raise LlmError(f"severity 非法：{severity_text}", code=LLM_OUTPUT_INVALID)
    if confidence_text not in {item.value for item in Confidence}:
        raise LlmError(f"confidence 非法：{confidence_text}", code=LLM_OUTPUT_INVALID)
    return category, reason, recovery, Severity(severity_text), Confidence(confidence_text)


def analyze_llm(incident: Incident, trace: Trace | None, store: Store, client: LlmClient) -> AnalysisResult:
    digest = _digest(incident, trace)
    prompt = _build_prompt(incident, digest)
    last_error: LlmError | None = None
    attempts = 0
    for attempt in range(1, MAX_LLM_ATTEMPTS + 1):
        attempts = attempt
        try:
            payload = client.chat_json(system=ANALYZE_SYSTEM, user=prompt)
            category, reason, recovery, severity, confidence = _parse_llm_payload(payload)
            return AnalysisResult(
                category=category,
                category_label=category_label(category),
                reason=reason,
                recovery=recovery,
                severity=severity,
                confidence=confidence,
                backend=Backend.LLM,
                precedents=_precedents(store, incident, category),
            )
        except LlmError as exc:
            last_error = exc
            # A missing endpoint/key cannot improve on retry; output/request failures can.
            if exc.code == LLM_DISABLED:
                break
    assert last_error is not None
    if attempts > 1 and last_error.code != LLM_DISABLED:
        raise LlmError(
            f"{last_error.message}（已尝试 {attempts} 次）",
            code=last_error.code,
            details=last_error.details,
        ) from last_error
    raise last_error


def analyze_incident(
    incident: Incident,
    *,
    trace: Trace | None,
    store: Store,
    backend: Backend,
    client: LlmClient | None = None,
) -> AnalysisResult:
    """LLM backend falls back to offline rules on failure (reason recorded, never silent)."""
    if backend == Backend.OFFLINE:
        return analyze_offline(incident, trace, store)
    assert client is not None
    try:
        return analyze_llm(incident, trace, store, client)
    except LlmError as exc:
        result = analyze_offline(incident, trace, store)
        result.fallback_reason = f"LLM 降级：{exc.message}"
        return result


def apply_analysis(incident: Incident, result: AnalysisResult, store: Store) -> Incident:
    incident.category = result.category
    incident.category_label = result.category_label
    incident.reason = result.reason
    incident.recovery = result.recovery
    incident.severity = result.severity
    incident.confidence = result.confidence
    incident.analyzed_by = result.backend
    incident.fallback_reason = result.fallback_reason
    incident.updated_at = now_iso()
    store.update_incident(incident)
    return incident
