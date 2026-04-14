"""Synthesizer — merge three specialist opinions into a single decision.

Pure programmatic, no LLM calls. Applies the decision matrix:

    Layer A     issue_kind fast paths (non-bug routes out immediately)
    Layer B     severity from severity_agent
    Layer B'    danger-flag severity upgrade (rules ∪ risk_agent)
    Layer B''   critical-path severity floor (product context)
    Layer B'''  info-sufficiency downgrade (severity → unknown)
    Layer C     route matrix
    Layer D     agreement score
    Layer E     conflict list
    Layer G     reasoning trail

decide.py applies hard safety overrides on top of what synthesize produces.
"""

from __future__ import annotations

from typing import Literal

from triage_gate.rules import detect_critical_paths
from triage_gate.schema import (
    CompletenessOpinion,
    ExtractedReport,
    IssueKind,
    ProductContext,
    RiskFlag,
    RiskOpinion,
    Route,
    Severity,
    SeverityOpinion,
    SynthesizerDecision,
)

DANGER_FLAGS: set[RiskFlag] = {"auth", "payment", "data_loss", "security", "outage"}

SEVERITY_RANK: dict[Severity, int] = {
    "unknown": 0,
    "S3": 1,
    "S2": 2,
    "S1": 3,
    "S0": 4,
}

CRITICAL_FIELDS = {"reproduction_steps", "observed_result", "expected_result"}


def synthesize(
    extracted: ExtractedReport,
    severity_op: SeverityOpinion,
    risk_op: RiskOpinion,
    completeness_op: CompletenessOpinion,
    rule_flags_raw: list[RiskFlag],
    product_context: ProductContext,
) -> SynthesizerDecision:
    # ── Layer A: issue_kind fast paths from intake preliminary ──
    prelim = extracted.fields.preliminary_issue_kind
    if prelim == "feature_request":
        return _fast_path(
            issue_kind="feature_request",
            severity="unknown",
            route="pm",
            note="intake flagged feature_request → route=pm",
        )
    if prelim == "support_question":
        return _fast_path(
            issue_kind="support_question",
            severity="unknown",
            route="support",
            note="intake flagged support_question → route=support",
        )
    if prelim == "duplicate":
        return _fast_path(
            issue_kind="duplicate",
            severity="unknown",
            route="human_engineer",
            note="intake flagged duplicate → human_engineer (merge)",
        )
    if prelim == "insufficient_info":
        return _fast_path(
            issue_kind="insufficient_info",
            severity="unknown",
            route="needs_more_info",
            note="intake flagged insufficient_info → needs_more_info",
        )

    # ── Bug path below ──
    reasoning: list[str] = []
    conflicts: list[str] = []

    chosen_severity: Severity = severity_op.severity
    reasoning.append(f"severity_agent proposed {chosen_severity}")

    # Layer B': danger-flag upgrade (rules ∪ risk_agent)
    agent_danger = set(risk_op.risk_flags) & DANGER_FLAGS
    rule_danger = set(rule_flags_raw) & DANGER_FLAGS
    all_danger = agent_danger | rule_danger

    if all_danger and chosen_severity in ("S3", "unknown"):
        prev = chosen_severity
        chosen_severity = "S2"
        conflicts.append(
            f"severity={prev} but danger flags={sorted(all_danger)} → upgraded to S2"
        )
        reasoning.append(
            f"danger flags {sorted(all_danger)} upgraded severity {prev}→S2"
        )

    # Layer B'': critical-path floor (product context)
    detected_paths = detect_critical_paths(
        extracted.raw_text, product_context.critical_paths
    )
    if detected_paths:
        floor = _max_severity([p.default_severity_floor for p in detected_paths])
        path_names = [p.name for p in detected_paths]
        if _is_less_severe(chosen_severity, floor):
            prev = chosen_severity
            chosen_severity = floor
            conflicts.append(
                f"critical path {path_names} forces floor {floor} (was {prev})"
            )
            reasoning.append(
                f"critical path '{','.join(path_names)}' enforces severity floor {floor}"
            )
        else:
            reasoning.append(
                f"critical path '{','.join(path_names)}' matched (no upgrade needed)"
            )

    # Layer B''': info-sufficiency downgrade is intentionally NOT applied here.
    # The route matrix below already reroutes low-info reports to needs_more_info
    # without touching severity. Demoting severity to 'unknown' would hide danger
    # cases (e.g. a payment outage with no formal repro steps should still read
    # as S0/S1 so downstream reviewers see the real urgency).

    # Layer C: route matrix
    chosen_route = _route_matrix(
        chosen_severity,
        has_danger=bool(all_danger),
        info_sufficiency=completeness_op.info_sufficiency,
    )
    reasoning.append(
        f"route matrix: severity={chosen_severity}, danger={bool(all_danger)}, "
        f"info={completeness_op.info_sufficiency} → {chosen_route}"
    )

    # Layer D: agreement
    agreement = _compute_agreement(
        severity_op, risk_op, completeness_op, rule_flags_raw, extracted
    )
    if agreement < 0.6:
        conflicts.append(f"low specialist agreement: {agreement:.2f}")

    return SynthesizerDecision(
        agreement_score=agreement,
        conflicts=conflicts,
        chosen_severity=chosen_severity,
        chosen_issue_kind="bug",
        chosen_route=chosen_route,
        reasoning_notes=reasoning,
    )


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────


def _fast_path(
    *,
    issue_kind: IssueKind,
    severity: Severity,
    route: Route,
    note: str,
) -> SynthesizerDecision:
    return SynthesizerDecision(
        agreement_score=1.0,
        conflicts=[],
        chosen_severity=severity,
        chosen_issue_kind=issue_kind,
        chosen_route=route,
        reasoning_notes=[note],
    )


def _is_less_severe(a: Severity, b: Severity) -> bool:
    return SEVERITY_RANK[a] < SEVERITY_RANK[b]


def _max_severity(severities: list[Severity]) -> Severity:
    return max(severities, key=lambda s: SEVERITY_RANK[s])


def _critical_fields_missing(extracted: ExtractedReport) -> bool:
    for name in CRITICAL_FIELDS:
        src = extracted.field_sources.get(name)
        if src is None or src.status == "missing":
            return True
    return False


def _route_matrix(
    severity: Severity,
    *,
    has_danger: bool,
    info_sufficiency: Literal["low", "medium", "high"],
) -> Route:
    if severity == "unknown":
        return "needs_more_info"
    if severity in ("S0", "S1"):
        return "human_engineer"
    if has_danger:
        return "human_engineer"
    # S2/S3 without danger flag
    if info_sufficiency == "low":
        return "needs_more_info"
    if severity == "S2":
        return "auto_fix" if info_sufficiency == "high" else "human_engineer"
    # S3 + high/medium
    return "auto_fix"


def _compute_agreement(
    severity_op: SeverityOpinion,
    risk_op: RiskOpinion,
    completeness_op: CompletenessOpinion,
    rule_flags_raw: list[RiskFlag],
    extracted: ExtractedReport,
) -> float:
    # 1. severity × risk coherence
    has_danger = bool(set(risk_op.risk_flags) & DANGER_FLAGS)
    sev_rank = SEVERITY_RANK[severity_op.severity]
    if has_danger and sev_rank < 2:
        sev_score = 0.3  # saw danger but called it low severity
    elif not has_danger and sev_rank >= 3:
        sev_score = 0.6  # S0/S1 with no danger flag — possible but unusual
    else:
        sev_score = 1.0

    # 2. risk_agent vs rules overlap (Jaccard)
    llm_flags = set(risk_op.risk_flags)
    rule_flags = set(rule_flags_raw)
    union = llm_flags | rule_flags
    risk_score = 1.0 if not union else len(llm_flags & rule_flags) / len(union)

    # 3. completeness vs intake missing-count coherence
    intake_missing = sum(
        1 for s in extracted.field_sources.values() if s.status == "missing"
    )
    claimed_missing = len(completeness_op.missing_fields)
    diff = abs(intake_missing - claimed_missing)
    comp_score = max(0.0, 1.0 - diff * 0.25)

    return round(0.5 * sev_score + 0.3 * risk_score + 0.2 * comp_score, 3)
