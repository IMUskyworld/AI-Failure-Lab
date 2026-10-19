"""Pydantic models: the data contract between pipeline stages.

Schemas are exported to ``docs/schemas/`` by ``scripts/export_schemas.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from faillab.domain.enums import Backend, Confidence, IncidentStatus, ReproStatus, Severity

SCHEMA_VERSION = "1.0"


class Note(BaseModel):
    code: str
    message: str


class ToolCall(BaseModel):
    index: int
    name: str
    arguments: str | None = None
    result: str | None = None
    error: str | None = None


class Trace(BaseModel):
    """Normalized transcript (DEVELOPMENT.md §8.1)."""

    trace_id: str
    harness: str = "unknown"
    model: str = "unknown"
    task: str = ""
    prompt: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    error: str | None = None
    retries: int = 0
    final_result: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    cwd: str | None = None
    warnings: list[Note] = Field(default_factory=list)
    meta: dict[str, str] = Field(default_factory=dict)


class DetectionHit(BaseModel):
    rule_id: str
    detail: str


class ReproSpec(BaseModel):
    cwd: str | None = None
    command: str
    pattern: str | None = None
    expect_exit_nonzero: bool = True
    timeout_s: float | None = None


class Reproduction(BaseModel):
    status: ReproStatus = ReproStatus.UNVERIFIED
    last_run_at: str | None = None
    total_runs: int = 0
    reproduced_count: int = 0
    fixed_count: int = 0
    inconclusive_count: int = 0


class Evidence(BaseModel):
    kind: str
    detail: str


class Incident(BaseModel):
    """Failure record (DEVELOPMENT.md §8.2)."""

    public_id: str
    slug: str
    title: str
    task: str = ""
    failure: str = ""
    category: str = "unclassified"
    category_label: str = "未分类"
    reason: str = ""
    recovery: str = ""
    severity: Severity = Severity.MEDIUM
    confidence: Confidence = Confidence.LOW
    analyzed_by: Backend | None = None
    fallback_reason: str | None = None
    model: str = "unknown"
    harness: str = "unknown"
    signature: str = ""
    occurrences: int = 1
    duplicate_of: str | None = None
    status: IncidentStatus = IncidentStatus.OPEN
    evidence: list[Evidence] = Field(default_factory=list)
    reproduce: ReproSpec | None = None
    reproduction: Reproduction = Field(default_factory=Reproduction)
    traces: list[str] = Field(default_factory=list)
    source: str = "import"
    created_at: str
    updated_at: str


class ReproRun(BaseModel):
    incident_id: str
    started_at: str
    ended_at: str
    duration_s: float
    exit_code: int | None = None
    pattern_matched: bool | None = None
    exit_nonzero_matched: bool | None = None
    status: ReproStatus
    note: str = ""
    log_path: str | None = None


class TaxonomySignature(BaseModel):
    field: str  # error | final | tool_error | task
    pattern: str


class TaxonomyCategory(BaseModel):
    id: str
    label: str
    description: str = ""
    severity: Severity = Severity.MEDIUM
    recovery_template: str = ""
    signatures: list[TaxonomySignature] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    """Output of one analyze run (offline or LLM)."""

    category: str
    category_label: str
    reason: str
    recovery: str
    severity: Severity
    confidence: Confidence
    backend: Backend
    fallback_reason: str | None = None
    precedents: list[str] = Field(default_factory=list)


# --- report -------------------------------------------------------------------


class ToolInfo(BaseModel):
    name: str = "faillab"
    version: str


class ReportTotals(BaseModel):
    incidents: int = 0
    open: int = 0
    reproduced: int = 0
    fixed: int = 0
    wontfix: int = 0
    unverified: int = 0
    repro_rate: float = 0.0


class CategoryCount(BaseModel):
    category: str
    label: str
    count: int


class KeyCount(BaseModel):
    key: str
    count: int


class TopSignature(BaseModel):
    signature: str
    title: str
    public_id: str
    occurrences: int


class IncidentBrief(BaseModel):
    public_id: str
    title: str
    category: str
    category_label: str
    status: IncidentStatus
    repro_status: ReproStatus
    model: str
    harness: str
    occurrences: int


class Report(BaseModel):
    schema_version: str = SCHEMA_VERSION
    tool: ToolInfo
    generated_at: str
    totals: ReportTotals
    by_category: list[CategoryCount] = Field(default_factory=list)
    by_model: list[KeyCount] = Field(default_factory=list)
    by_harness: list[KeyCount] = Field(default_factory=list)
    top_signatures: list[TopSignature] = Field(default_factory=list)
    recent: list[IncidentBrief] = Field(default_factory=list)
