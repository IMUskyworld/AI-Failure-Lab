"""Taxonomy loading and signature matching (DEVELOPMENT.md §10)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from faillab.domain.models import TaxonomyCategory, Trace
from faillab.errors import TAXONOMY_INVALID, FaillabError

DATA_DIR = Path(__file__).resolve().parent / "data"
TAXONOMY_PATH = DATA_DIR / "taxonomy.json"

UNCLASSIFIED_ID = "unclassified"
UNCLASSIFIED_LABEL = "未分类"


@lru_cache(maxsize=1)
def load_taxonomy() -> list[TaxonomyCategory]:
    try:
        payload = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
        raw_categories = payload["categories"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FaillabError(f"taxonomy 文件无效：{exc}", code=TAXONOMY_INVALID) from exc
    try:
        categories = [TaxonomyCategory.model_validate(item) for item in raw_categories]
    except ValidationError as exc:
        raise FaillabError(
            "taxonomy 内容不符合 schema",
            code=TAXONOMY_INVALID,
            details={"errors": exc.error_count()},
        ) from exc
    if not categories:
        raise FaillabError("taxonomy 为空", code=TAXONOMY_INVALID)
    for category in categories:
        for signature in category.signatures:
            if signature.field == "retries":
                try:
                    int(signature.pattern)
                except ValueError as exc:
                    raise FaillabError(
                        f"taxonomy {category.id} 的 retries 阈值无效：{signature.pattern}",
                        code=TAXONOMY_INVALID,
                    ) from exc
            else:
                try:
                    re.compile(signature.pattern)
                except re.error as exc:
                    raise FaillabError(
                        f"taxonomy {category.id} 的正则无效：{signature.pattern}",
                        code=TAXONOMY_INVALID,
                    ) from exc
    return categories


def category_label(category_id: str) -> str:
    for category in load_taxonomy():
        if category.id == category_id:
            return category.label
    return UNCLASSIFIED_LABEL


def _haystacks(trace: Trace) -> dict[str, str]:
    return {
        "error": trace.error or "",
        "final": trace.final_result or "",
        "tool_error": " ".join(call.error or "" for call in trace.tool_calls if call.error),
        "task": trace.task or "",
    }


@dataclass
class TaxonomyMatch:
    category: TaxonomyCategory
    field: str
    pattern: str


def match_category(trace: Trace, categories: list[TaxonomyCategory] | None = None) -> TaxonomyMatch | None:
    """First matching category wins (file order is intentional: specific → generic)."""
    haystacks = _haystacks(trace)
    for category in categories or load_taxonomy():
        for signature in category.signatures:
            if signature.field == "retries":
                threshold = int(signature.pattern)
                if trace.retries >= threshold:
                    return TaxonomyMatch(category=category, field="retries", pattern=signature.pattern)
                continue
            text = haystacks.get(signature.field, "")
            if text:
                try:
                    matched = bool(re.search(signature.pattern, text))
                except re.error as exc:
                    raise FaillabError(
                        f"taxonomy {category.id} 的正则无效：{signature.pattern}",
                        code=TAXONOMY_INVALID,
                    ) from exc
                if matched:
                    return TaxonomyMatch(category=category, field=signature.field, pattern=signature.pattern)
    return None
