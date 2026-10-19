"""Transcript parsers (DEVELOPMENT.md §9): jsonl-events + json-object → normalized Trace.

Missing or malformed fields are tolerated and recorded as warnings — the source
information is never silently dropped.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from faillab.domain.enums import TraceFormat
from faillab.domain.models import Note, ToolCall, Trace
from faillab.errors import TRACE_EMPTY, TRACE_FILE_NOT_FOUND, TRACE_PARSE_FAILED, TraceError
from faillab.util import decode_bytes, now_iso

OBJECT_ALIASES: dict[str, tuple[str, ...]] = {
    "model": ("model", "model_name", "agent_model"),
    "harness": ("harness", "tool", "client", "agent"),
    "task": ("task", "question", "instruction", "title"),
    "prompt": ("prompt", "system_prompt", "instructions"),
    "tool_calls": ("tool_calls", "steps", "actions"),
    "error": ("error", "exception", "failure"),
    "retries": ("retries", "attempts", "retry_count"),
    "final_result": ("final_result", "final", "result", "outcome"),
    "cwd": ("cwd", "workdir", "directory"),
    "started_at": ("started_at", "start_time", "started"),
    "ended_at": ("ended_at", "end_time", "finished"),
}

TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "tool", "action"),
    "arguments": ("arguments", "args", "input"),
    "result": ("result", "output"),
    "error": ("error", "exception"),
}


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _first(payload: dict[str, Any], aliases: tuple[str, ...]) -> tuple[str | None, bool]:
    """Returns (value, found)."""
    for alias in aliases:
        if alias in payload and payload[alias] is not None:
            return _as_text(payload[alias]), True
    return None, False


def trace_id_for(path: Path, content: bytes) -> str:
    """Deterministic id: re-importing the same file is idempotent (§9.4 note)."""
    digest = hashlib.sha1(content).hexdigest()[:8]
    stem = "".join(character if character.isalnum() else "-" for character in path.stem).strip("-") or "trace"
    return f"tr-{stem[:40]}-{digest}"


def detect_format(path: Path, text: str) -> TraceFormat:
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        return TraceFormat.JSONL_EVENTS
    stripped = text.lstrip()
    if path.suffix.lower() == ".json":
        return TraceFormat.JSON_OBJECT
    first_line = stripped.splitlines()[0] if stripped else ""
    if first_line.strip().startswith("{"):
        try:
            json.loads(first_line)
            return TraceFormat.JSONL_EVENTS
        except json.JSONDecodeError:
            return TraceFormat.JSON_OBJECT
    return TraceFormat.JSON_OBJECT


def parse_trace_file(path: Path, *, harness: str | None = None, model: str | None = None) -> Trace:
    if not path.is_file():
        raise TraceError(f"转录文件不存在：{path}", code=TRACE_FILE_NOT_FOUND, details={"path": str(path)})
    content = path.read_bytes()
    if not content.strip():
        raise TraceError(f"转录文件为空：{path}", code=TRACE_EMPTY, details={"path": str(path)})
    # Transcripts are not guaranteed to be UTF-8 (same lesson as child-process output).
    text = decode_bytes(content)
    trace_format = detect_format(path, text)
    trace_id = trace_id_for(path, content)
    if trace_format == TraceFormat.JSONL_EVENTS:
        trace = parse_jsonl_events(text, trace_id=trace_id)
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TraceError(
                f"JSON 解析失败：{path.name}",
                code=TRACE_PARSE_FAILED,
                details={"path": str(path), "reason": str(exc)},
            ) from exc
        if not isinstance(payload, dict):
            raise TraceError(
                f"单对象格式要求顶层是 JSON object：{path.name}",
                code=TRACE_PARSE_FAILED,
                details={"path": str(path)},
            )
        trace = parse_json_object(payload, trace_id=trace_id)
    if harness:
        trace.harness = harness
    if model:
        trace.model = model
    if trace.harness == "unknown" and not harness:
        trace.warnings.append(Note(code="MISSING_FIELD", message="缺少 harness，已置为 unknown（可用 --harness 覆盖）"))
    if trace.model == "unknown" and not model:
        trace.warnings.append(Note(code="MISSING_FIELD", message="缺少 model，已置为 unknown（可用 --model 覆盖）"))
    return trace


def parse_jsonl_events(text: str, *, trace_id: str) -> Trace:
    trace = Trace(trace_id=trace_id)
    prompt_set = False
    errors: list[str] = []
    last_tool_index: int | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            trace.warnings.append(Note(code="BAD_LINE", message=f"第 {line_number} 行不是合法 JSON，已跳过"))
            continue
        if not isinstance(event, dict):
            trace.warnings.append(Note(code="BAD_EVENT", message=f"第 {line_number} 行不是 JSON object，已跳过"))
            continue
        kind = str(event.get("kind") or event.get("type") or "").lower()
        if kind in {"meta", "task"}:
            trace.task = trace.task or (
                _as_text(event.get("task") or event.get("question") or event.get("title")) or ""
            )
            _, has_model = _first(event, OBJECT_ALIASES["model"])
            if has_model and trace.model == "unknown":
                trace.model = _first(event, OBJECT_ALIASES["model"])[0] or "unknown"
            _, has_harness = _first(event, OBJECT_ALIASES["harness"])
            if has_harness and trace.harness == "unknown":
                trace.harness = _first(event, OBJECT_ALIASES["harness"])[0] or "unknown"
            trace.cwd = trace.cwd or _first(event, OBJECT_ALIASES["cwd"])[0]
            trace.started_at = trace.started_at or _first(event, OBJECT_ALIASES["started_at"])[0]
            trace.ended_at = trace.ended_at or _first(event, OBJECT_ALIASES["ended_at"])[0]
        elif kind == "prompt":
            if not prompt_set:
                trace.prompt = _as_text(event.get("text") or event.get("prompt") or "") or ""
                prompt_set = True
        elif kind == "tool_call":
            trace.tool_calls.append(
                ToolCall(
                    index=len(trace.tool_calls),
                    name=_as_text(event.get("name") or event.get("tool") or "unknown") or "unknown",
                    arguments=_as_text(event.get("arguments") or event.get("args") or event.get("input")),
                )
            )
            last_tool_index = len(trace.tool_calls) - 1
        elif kind == "tool_result":
            if last_tool_index is None:
                trace.warnings.append(
                    Note(code="ORPHAN_TOOL_RESULT", message=f"第 {line_number} 行 tool_result 没有对应 tool_call")
                )
                continue
            call = trace.tool_calls[last_tool_index]
            call.result = _as_text(event.get("result") or event.get("output")) or call.result
            call.error = _as_text(event.get("error")) or call.error
            last_tool_index = None
        elif kind == "error":
            message = _as_text(event.get("message") or event.get("error") or "")
            if message:
                errors.append(message)
        elif kind == "retry":
            attempt = event.get("attempt")
            if isinstance(attempt, int) and attempt > trace.retries:
                trace.retries = attempt
            else:
                trace.retries += 1
        elif kind == "final":
            result_text = _as_text(event.get("result") or event.get("status") or "") or ""
            ok = event.get("ok")
            trace.final_result = result_text if ok is not False else f"[failed] {result_text}".strip()
        elif kind == "note":
            trace.meta.setdefault(f"note-{line_number}", _as_text(event.get("text")) or "")
        elif kind:
            trace.warnings.append(
                Note(code="UNKNOWN_EVENT", message=f"未知事件类型「{kind}」，已忽略（第 {line_number} 行）")
            )
    if errors:
        trace.error = "；".join(dict.fromkeys(errors))[:500]
    return trace


def parse_json_object(payload: dict[str, Any], *, trace_id: str) -> Trace:
    trace = Trace(trace_id=trace_id)
    model, has_model = _first(payload, OBJECT_ALIASES["model"])
    harness, has_harness = _first(payload, OBJECT_ALIASES["harness"])
    trace.model = model or "unknown"
    trace.harness = harness or "unknown"
    if not has_model:
        trace.warnings.append(Note(code="MISSING_FIELD", message="缺少 model 字段"))
    if not has_harness:
        trace.warnings.append(Note(code="MISSING_FIELD", message="缺少 harness 字段"))
    trace.task = _first(payload, OBJECT_ALIASES["task"])[0] or ""
    trace.prompt = _first(payload, OBJECT_ALIASES["prompt"])[0] or ""
    trace.cwd = _first(payload, OBJECT_ALIASES["cwd"])[0]
    trace.started_at = _first(payload, OBJECT_ALIASES["started_at"])[0]
    trace.ended_at = _first(payload, OBJECT_ALIASES["ended_at"])[0]

    error_value, has_error = _first(payload, OBJECT_ALIASES["error"])
    trace.error = error_value if has_error else None

    retries_value, has_retries = _first(payload, OBJECT_ALIASES["retries"])
    if has_retries:
        try:
            trace.retries = int(float(retries_value or 0))
        except (TypeError, ValueError):
            trace.warnings.append(Note(code="BAD_FIELD", message=f"retries 不是数字：{retries_value}，已置 0"))

    raw_final = None
    for alias in OBJECT_ALIASES["final_result"]:
        if alias in payload and payload[alias] is not None:
            raw_final = payload[alias]
            break
    if isinstance(raw_final, dict):
        ok = raw_final.get("ok", raw_final.get("success"))
        text = _as_text(raw_final.get("result") or raw_final.get("output") or raw_final.get("status") or "") or ""
        trace.final_result = text if ok is not False else f"[failed] {text}".strip()
    elif raw_final is not None:
        trace.final_result = _as_text(raw_final)

    raw_calls = None
    for alias in OBJECT_ALIASES["tool_calls"]:
        if alias in payload and isinstance(payload[alias], list):
            raw_calls = payload[alias]
            break
    for index, item in enumerate(raw_calls or []):
        if not isinstance(item, dict):
            trace.warnings.append(Note(code="BAD_TOOL_CALL", message=f"第 {index + 1} 个工具调用不是 object，已跳过"))
            continue
        name = None
        arguments = None
        result = None
        error = None
        for target, aliases in TOOL_ALIASES.items():
            value, _found = _first(item, aliases)
            if target == "name":
                name = value
            elif target == "arguments":
                arguments = value
            elif target == "result":
                result = value
            elif target == "error":
                error = value
        trace.tool_calls.append(
            ToolCall(index=index, name=name or "unknown", arguments=arguments, result=result, error=error)
        )
    if not trace.task and not trace.prompt:
        trace.warnings.append(Note(code="MISSING_FIELD", message="task 与 prompt 均为空"))
    trace.meta.setdefault("imported_at", now_iso())
    return trace
