"""Failure detection (DEVELOPMENT.md §9.3): data-driven rules over the normalized trace."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from faillab.domain.models import DetectionHit, Trace
from faillab.errors import DETECTION_RULES_INVALID, FaillabError

DATA_DIR = Path(__file__).resolve().parent / "data"
RULES_PATH = DATA_DIR / "detection.json"


@lru_cache(maxsize=1)
def load_rules() -> list[dict]:
    try:
        payload = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        rules = payload["rules"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FaillabError(f"检测规则文件无效：{exc}", code=DETECTION_RULES_INVALID) from exc
    if not isinstance(rules, list) or not rules:
        raise FaillabError("检测规则为空", code=DETECTION_RULES_INVALID)
    if any(not isinstance(rule, dict) for rule in rules):
        raise FaillabError("检测规则条目必须是 object", code=DETECTION_RULES_INVALID)
    return list(rules)


def detect(trace: Trace, rules: list[dict] | None = None) -> list[DetectionHit]:
    active_rules = load_rules() if rules is None else rules
    hits: list[DetectionHit] = []
    for rule in active_rules:
        rule_id = str(rule.get("id", "unknown"))
        when = str(rule.get("when", ""))
        detail = str(rule.get("detail", rule_id))
        if when == "error" and trace.error:
            hits.append(DetectionHit(rule_id=rule_id, detail=detail))
        elif when == "final_result" and trace.final_result:
            pattern = str(rule.get("pattern", ""))
            if pattern:
                try:
                    matched = bool(re.search(pattern, trace.final_result))
                except re.error as exc:
                    raise FaillabError(
                        f"检测规则 {rule_id} 的正则无效：{pattern}",
                        code=DETECTION_RULES_INVALID,
                    ) from exc
                if matched:
                    hits.append(
                        DetectionHit(rule_id=rule_id, detail=f"{detail}（匹配输出：{trace.final_result[:60]}）")
                    )
        elif when == "retries":
            try:
                minimum = int(rule.get("min", 3))
            except (TypeError, ValueError) as exc:
                raise FaillabError(f"检测规则 {rule_id} 的 min 无效", code=DETECTION_RULES_INVALID) from exc
            if trace.retries >= minimum:
                hits.append(DetectionHit(rule_id=rule_id, detail=f"{detail}（retries={trace.retries}）"))
        elif when == "tool_call_errors":
            try:
                minimum = int(rule.get("min", 1))
            except (TypeError, ValueError) as exc:
                raise FaillabError(f"检测规则 {rule_id} 的 min 无效", code=DETECTION_RULES_INVALID) from exc
            error_count = sum(1 for call in trace.tool_calls if call.error)
            if error_count >= minimum:
                hits.append(DetectionHit(rule_id=rule_id, detail=f"{detail}（{error_count} 个工具调用报错）"))
    return hits


def is_failure(trace: Trace, rules: list[dict] | None = None) -> bool:
    return bool(detect(trace, rules))
