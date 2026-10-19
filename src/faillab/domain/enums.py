"""Enums shared across ingest / analyze / repro / report."""

from __future__ import annotations

from enum import Enum


class IncidentStatus(str, Enum):
    OPEN = "open"
    REPRODUCED = "reproduced"
    FIXED = "fixed"
    WONTFIX = "wontfix"


class ReproStatus(str, Enum):
    UNVERIFIED = "unverified"
    REPRODUCED = "reproduced"
    FIXED = "fixed"
    INCONCLUSIVE = "inconclusive"
    INTERRUPTED = "interrupted"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Backend(str, Enum):
    LLM = "llm"
    OFFLINE = "offline"


class TraceFormat(str, Enum):
    JSONL_EVENTS = "jsonl-events"
    JSON_OBJECT = "json-object"
