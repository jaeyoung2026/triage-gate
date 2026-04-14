"""Triage-gate schemas — the team contract.

Six pydantic models cover the full pipeline:

    RawReport          unstructured input as it arrives
    ExtractedReport    intake_agent output (fields + field_sources + raw preserved)
    SpecialistOpinion  one specialist's judgment (severity / risk / completeness)
    TriagePacket       final decision handed to downstream (fix / human / support / pm)
    Trace              full reasoning trail — rules, opinions, overrides
    OutcomeRecord      downstream feedback — what actually happened to the packet

Specialists write to SpecialistOpinion subtypes; the synthesizer merges them into
a TriagePacket, and the decide layer applies hard rule overrides on top. Trace
preserves every intermediate artifact so the evolve loop can learn from both
agreement and disagreement.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# enums as Literal types — easier for LLM structured output than Enum classes
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

SpecialistName = Literal["severity", "risk", "completeness"]

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
    """A bug report in whatever shape it arrived.

    `raw_text` is the single source of truth. Everything else is metadata the
    intake layer may or may not have. Rules on raw run directly against
    `raw_text` so they remain robust to intake extraction failures.
    """

    report_id: str
    source_kind: SourceKind = "unknown"
    raw_text: str
    language: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    received_at: Optional[datetime] = None


# ─────────────────────────────────────────────────────────────────────────────
# 2. ExtractedReport — intake_agent output
# ─────────────────────────────────────────────────────────────────────────────


class ExtractedFields(BaseModel):
    """Fields the intake agent tries to populate. Any of these may be missing.

    `preliminary_issue_kind` is set by intake_agent after reading the raw text
    plus product_context (scope + limitations + glossary). It drives the
    synthesizer's fast-path routing for non-bug reports.
    """

    preliminary_issue_kind: IssueKind = "bug"
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
    """Provenance for one extracted field.

    `stated` means the reporter wrote it explicitly. `inferred` means intake
    guessed from context — downstream specialists should treat this as weaker
    evidence than stated, and completeness_agent may count inferred fields
    toward `missing_fields` when rigor is needed.
    """

    status: FieldStatus
    quote: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class ExtractedReport(BaseModel):
    """Intake_agent output. `raw_text` is preserved so specialists can re-read."""

    report_id: str
    raw_text: str
    source_kind: SourceKind
    language: Optional[str] = None
    fields: ExtractedFields
    field_sources: dict[str, FieldSource] = Field(default_factory=dict)
    intake_notes: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# 2b. ProductContext — product-specific facts loaded once at startup
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
    """Product facts that every specialist reads a tailored subset of.

    Source-role split so tokens stay bounded and specialists don't cross-interpret:
        intake       → scope_summary + known_limitations + domain_glossary
        severity     → critical_paths + scope_summary
        risk         → critical_paths (risk_flags) + known_limitations
        completeness → scope_summary only
        evolve       → everything + rules.py + outcome_log
    """

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
# 3. SpecialistOpinion — one specialist's output
# ─────────────────────────────────────────────────────────────────────────────


class _SpecialistBase(BaseModel):
    """Common fields every specialist must produce."""

    specialist: SpecialistName
    rationale: list[str] = Field(min_length=1, max_length=4)
    confidence: float = Field(ge=0.0, le=1.0)


class SeverityOpinion(_SpecialistBase):
    specialist: Literal["severity"] = "severity"
    severity: Severity
    impact_summary: str


class RiskOpinion(_SpecialistBase):
    specialist: Literal["risk"] = "risk"
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    escalation_reason: Optional[str] = None


class CompletenessOpinion(_SpecialistBase):
    specialist: Literal["completeness"] = "completeness"
    missing_fields: list[str] = Field(default_factory=list)
    inferred_fields: list[str] = Field(default_factory=list)
    info_sufficiency: Literal["low", "medium", "high"]


SpecialistOpinion = Annotated[
    Union[SeverityOpinion, RiskOpinion, CompletenessOpinion],
    Field(discriminator="specialist"),
]


# ─────────────────────────────────────────────────────────────────────────────
# 4. TriagePacket — final decision contract
# ─────────────────────────────────────────────────────────────────────────────


class TriagePacket(BaseModel):
    """The contract handed to downstream systems.

    Anything consumed by fix/human/support/pm lanes must come from this packet.
    Free-form reasoning belongs in `Trace`, not here.
    """

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
# 5. Trace — full reasoning trail
# ─────────────────────────────────────────────────────────────────────────────


class SynthesizerDecision(BaseModel):
    """Programmatic synthesizer output — merges specialist opinions.

    `agreement_score` and `conflicts` are the signal the evolve loop watches
    most closely: disagreement between specialists often predicts downstream
    relabeling.
    """

    agreement_score: float = Field(ge=0.0, le=1.0)
    conflicts: list[str] = Field(default_factory=list)
    chosen_severity: Severity
    chosen_issue_kind: IssueKind
    chosen_route: Route
    reasoning_notes: list[str] = Field(default_factory=list)


class Trace(BaseModel):
    """Every intermediate artifact produced while triaging one report."""

    report_id: str
    raw: RawReport
    extracted: ExtractedReport
    product_context_version: str
    rule_flags_raw: list[RiskFlag] = Field(default_factory=list)
    rule_flags_extracted: list[str] = Field(default_factory=list)
    specialist_opinions: list[SpecialistOpinion] = Field(default_factory=list)
    synthesizer_decision: SynthesizerDecision
    overrides_applied: list[str] = Field(default_factory=list)
    final_packet: TriagePacket
    timings_ms: dict[str, float] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.now)


# ─────────────────────────────────────────────────────────────────────────────
# 6. OutcomeRecord — downstream feedback for the evolve loop
# ─────────────────────────────────────────────────────────────────────────────


class OutcomeRecord(BaseModel):
    """What actually happened to a triage packet downstream.

    The evolve_agent reads a stream of these alongside the matching Trace to
    propose rule/prompt diffs. `delta_from_original` makes supervision signals
    cheap to compute: any non-empty delta is a disagreement worth studying.
    """

    report_id: str
    original_packet: TriagePacket
    trace_ref: Optional[str] = None  # path or id of the matching Trace artifact
    human_decision_by: Optional[str] = None
    final_issue_kind: Optional[IssueKind] = None
    final_severity: Optional[Severity] = None
    final_route: Optional[Route] = None
    downstream_outcome: DownstreamOutcome = "in_progress"
    delta_from_original: dict[str, tuple] = Field(default_factory=dict)
    notes: Optional[str] = None
    recorded_at: datetime = Field(default_factory=datetime.now)
