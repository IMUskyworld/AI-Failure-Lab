"""Structured error model: stable codes, no secrets in details."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# --- error codes (stable public identifiers, do not rename) -------------------
TRACE_FILE_NOT_FOUND = "TRACE_FILE_NOT_FOUND"
TRACE_PARSE_FAILED = "TRACE_PARSE_FAILED"
TRACE_EMPTY = "TRACE_EMPTY"
INCIDENT_NOT_FOUND = "INCIDENT_NOT_FOUND"
INCIDENT_NO_REPRO = "INCIDENT_NO_REPRO"
INCIDENT_AMBIGUOUS = "INCIDENT_AMBIGUOUS"
STORE_ERROR = "STORE_ERROR"
STORE_SCHEMA_INCOMPATIBLE = "STORE_SCHEMA_INCOMPATIBLE"
BUNDLE_INVALID = "BUNDLE_INVALID"
TAXONOMY_INVALID = "TAXONOMY_INVALID"
DETECTION_RULES_INVALID = "DETECTION_RULES_INVALID"
REPRO_SPEC_INVALID = "REPRO_SPEC_INVALID"
CONFIG_INVALID = "CONFIG_INVALID"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
LLM_DISABLED = "LLM_DISABLED"
LLM_REQUEST_FAILED = "LLM_REQUEST_FAILED"
LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
REPORT_GENERATION_FAILED = "REPORT_GENERATION_FAILED"
UNKNOWN_ERROR = "UNKNOWN_ERROR"


class FaillabError(Exception):
    """An error with a machine-readable code.

    ``details`` must never contain secrets or raw transcript content: it is
    rendered to terminals and may be embedded in reports.
    """

    code: str = UNKNOWN_ERROR
    component: str = "core"
    recoverable: bool = True

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        component: str | None = None,
        recoverable: bool | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if component is not None:
            self.component = component
        if recoverable is not None:
            self.recoverable = recoverable
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "component": self.component,
            "recoverable": self.recoverable,
            "details": self.details,
        }

    def __str__(self) -> str:  # pragma: no cover - convenience for tracebacks
        return f"[{self.code}] {self.message}"


class TraceError(FaillabError):
    code = TRACE_PARSE_FAILED
    component = "ingest"


class IncidentError(FaillabError):
    code = INCIDENT_NOT_FOUND
    component = "store"


class StoreError(FaillabError):
    code = STORE_ERROR
    component = "store"


class ConfigError(FaillabError):
    code = CONFIG_INVALID
    component = "config"
    recoverable = False


class LlmError(FaillabError):
    code = LLM_REQUEST_FAILED
    component = "llm"


def describe(exc: BaseException, *, fallback_component: str = "core") -> dict[str, Any]:
    """Normalize any exception into the structured error shape used in JSON output."""
    if isinstance(exc, FaillabError):
        return exc.to_dict()
    return {
        "code": getattr(exc, "code", UNKNOWN_ERROR) if exc else UNKNOWN_ERROR,
        "message": f"{type(exc).__name__}: {exc}",
        "component": fallback_component,
        "recoverable": True,
        "details": {"exception_type": type(exc).__name__},
    }
