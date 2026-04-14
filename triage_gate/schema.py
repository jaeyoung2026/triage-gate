"""Triage-gate schemas.

Four primary models:

    RawReport        unstructured input as it arrives
    ProductContext   product-specific facts (critical_paths, known_limitations, ...)
    Analysis         one LLM call output — extraction + all judgment dimensions
    TriagePacket     final downstream contract (issue_kind / severity / route)

Plus Trace (audit trail) and OutcomeRecord (reserved for feedback).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# enums as Literal types
# ─────────────────────────────────────────────────────────────────────────────

SourceKind = Literal["github_issue", "email", "chat", "ocr", "slack", "unknown"]

FieldStatus = Literal["stated", "inferred", "missing"]

IssueKind = Literal[
    "bug",
    "feature_request",
    "support_question",
    "duplicate",
    "insufficient_info",
]

Severity = Literal["S0", "S1", "S2", "S3", "unknown"]

Route = Literal[
    "auto_fix",
    "human_engineer",
    "support",
    "pm",
    "needs_more_info",
]

RiskFlag = Literal[
    "auth",
    "payment",
    "data_loss",
    "security",
    "outage",
    "backend_validation",
    "insufficient_repro",
    "unclear_scope",
]

InfoSufficiency = Literal["low", "medium", "high"]

DownstreamOutcome = Literal[
    "fixed_merged",
    "reverted",
    "rejected_not_a_bug",
    "escalated_later",
    "closed_stale",
    "in_progress",
]


# ─────────────────────────────────────────────────────────────────────────────
# 1. RawReport — unstructured input
# ─────────────────────────────────────────────────────────────────────────────


class RawReport(BaseModel):
    """A bug report in whatever shape it arrived. `raw_text` is the source of truth."""

    report_id: str
    source_kind: SourceKind = "unknown"
    raw_text: str
    language: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    received_at: Optional[datetime] = None


# ─────────────────────────────────────────────────────────────────────────────
# 2. ProductContext — product-specific facts
# ─────────────────────────────────────────────────────────────────────────────


class CriticalPath(BaseModel):
    """A product flow that enforces a severity floor when mentioned."""

    name: str
    keywords: list[str] = Field(default_factory=list)
    description: str
    default_severity_floor: Severity
    default_risk_flags: list[RiskFlag] = Field(default_factory=list)


class PrecedentCase(BaseModel):
    """A labeled historical case. Seeded by hand, grown by the evolve loop."""

    raw_excerpt: str
    verdict: IssueKind
    severity: Severity
    why: str


class ProductContext(BaseModel):
    """Product facts — loaded once at startup, fed to analyze() via prompt."""

    product_name: str
    version: str
    mission: str
    scope_summary: str
    critical_paths: list[CriticalPath] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    domain_glossary: dict[str, str] = Field(default_factory=dict)
    precedent_cases: list[PrecedentCase] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "ProductContext":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# 3. Analysis — single LLM call output
# ─────────────────────────────────────────────────────────────────────────────


class ExtractedFields(BaseModel):
    """Structured fields extracted from raw text. Any may be missing."""

    title: Optional[str] = None
    body: Optional[str] = None
    reproduction_steps: list[str] = Field(default_factory=list)
    observed_result: Optional[str] = None
    expected_result: Optional[str] = None
    stack_trace: Optional[str] = None
    affected_area: Optional[str] = None
    frequency_hint: Optional[str] = None
    user_impact_hint: Optional[str] = None


class FieldSource(BaseModel):
    """Provenance for one extracted field: stated / inferred / missing."""

    status: FieldStatus
    quote: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class FieldSourceMap(BaseModel):
    """Typed per-field provenance. Strict structured outputs need every key upfront."""

    title: Optional[FieldSource]
    body: Optional[FieldSource]
    reproduction_steps: Optional[FieldSource]
    observed_result: Optional[FieldSource]
    expected_result: Optional[FieldSource]
    stack_trace: Optional[FieldSource]
    affected_area: Optional[FieldSource]
    frequency_hint: Optional[FieldSource]
    user_impact_hint: Optional[FieldSource]


class Analysis(BaseModel):
    """One LLM call produces this. Replaces intake + 3 specialists + synthesizer.

    The gate() function in gate.py converts this to a TriagePacket by applying
    programmatic safety floors (rules keywords + critical_path floors).
    """

    language: Optional[str]
    preliminary_issue_kind: IssueKind

    # Extraction
    fields: ExtractedFields
    field_sources: FieldSourceMap
    intake_notes: list[str]

    # Severity judgment
    severity_call: Severity
    severity_rationale: list[str]
    impact_summary: str

    # Adversarial risk scan
    detected_risks: list[RiskFlag]
    risk_rationale: list[str]

    # Completeness
    info_sufficiency: InfoSufficiency
    missing_fields: list[str]

    # The model's own doubts — becomes the human-review signal.
    self_concerns: list[str]


# ─────────────────────────────────────────────────────────────────────────────
# 4. TriagePacket — downstream contract
# ─────────────────────────────────────────────────────────────────────────────


class TriagePacket(BaseModel):
    """The final output handed to downstream systems (fix / human / support / pm)."""

    report_id: str
    issue_kind: IssueKind
    bug_confidence: float = Field(ge=0.0, le=1.0)
    severity: Severity
    route: Route
    rationale: list[str] = Field(min_length=1, max_length=4)
    missing_fields: list[str] = Field(default_factory=list)
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    needs_human_review: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# 5. Trace — audit trail for viz + evolve loop
# ─────────────────────────────────────────────────────────────────────────────


class Trace(BaseModel):
    """Everything produced while triaging one report — feeds Streamlit and evolve."""

    report_id: str
    raw: RawReport
    product_context_version: str
    analysis: Analysis
    rule_flags_raw: list[RiskFlag] = Field(default_factory=list)
    severity_upgrades: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    agreement_score: float = Field(ge=0.0, le=1.0)
    final_packet: TriagePacket
    timings_ms: dict[str, float] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


# ─────────────────────────────────────────────────────────────────────────────
# 6. OutcomeRecord — downstream feedback for the future evolve loop
# ─────────────────────────────────────────────────────────────────────────────


class OutcomeRecord(BaseModel):
    """What actually happened to a triage packet downstream. Unused in v1."""

    report_id: str
    original_packet: TriagePacket
    trace_ref: Optional[str] = None
    human_decision_by: Optional[str] = None
    final_issue_kind: Optional[IssueKind] = None
    final_severity: Optional[Severity] = None
    final_route: Optional[Route] = None
    downstream_outcome: DownstreamOutcome = "in_progress"
    delta_from_original: dict[str, tuple] = Field(default_factory=dict)
    notes: Optional[str] = None
    recorded_at: datetime = Field(default_factory=datetime.now)
