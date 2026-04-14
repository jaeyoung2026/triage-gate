"""Final safety gate. Hard rule overrides + TriagePacket assembly.

decide.py exists as defense in depth: even if the synthesizer matrix has a bug,
these rules enforce non-negotiable safety invariants from §13.3 of the triage
design (rule-first safety). Returns the final packet plus the list of overrides
that fired, which Trace preserves for the evolve loop.
"""

from __future__ import annotations

from triage_gate.schema import (
    CompletenessOpinion,
    ExtractedReport,
    RiskOpinion,
    Route,
    Severity,
    SeverityOpinion,
    SynthesizerDecision,
    TriagePacket,
)
from triage_gate.synthesize import DANGER_FLAGS


def decide(
    synth: SynthesizerDecision,
    severity_op: SeverityOpinion,
    risk_op: RiskOpinion,
    completeness_op: CompletenessOpinion,
    extracted: ExtractedReport,
) -> tuple[TriagePacket, list[str]]:
    overrides: list[str] = []

    severity: Severity = synth.chosen_severity
    route: Route = synth.chosen_route
    risk_flags = list(risk_op.risk_flags)
    danger_hit = sorted(set(risk_flags) & DANGER_FLAGS)
    has_danger = bool(danger_hit)

    # Hard rule 1: any danger flag → auto_fix is never allowed
    if has_danger and route == "auto_fix":
        route = "human_engineer"
        overrides.append(
            f"decide: danger flag {danger_hit} blocks auto_fix → human_engineer"
        )

    # Hard rule 2: S0/S1 → human_engineer always
    if severity in ("S0", "S1") and route != "human_engineer":
        prev = route
        route = "human_engineer"
        overrides.append(
            f"decide: severity {severity} forces human_engineer (was {prev})"
        )

    # Hard rule 3: bug with unknown severity + low info → needs_more_info
    # Only applies when issue_kind=bug. Non-bug fast paths (feature_request →
    # pm, support_question → support, duplicate → human_engineer) must not be
    # rerouted just because completeness is low — completeness doesn't matter
    # for those kinds.
    if (
        synth.chosen_issue_kind == "bug"
        and severity == "unknown"
        and completeness_op.info_sufficiency == "low"
        and route != "needs_more_info"
    ):
        prev = route
        route = "needs_more_info"
        overrides.append(
            f"decide: bug + unknown severity + low info → needs_more_info (was {prev})"
        )

    needs_human_review = _needs_human_review(synth, severity, has_danger)
    rationale = _build_rationale(severity_op, synth)
    bug_confidence = _bug_confidence(synth, severity_op)

    packet = TriagePacket(
        report_id=extracted.report_id,
        issue_kind=synth.chosen_issue_kind,
        bug_confidence=bug_confidence,
        severity=severity,
        route=route,
        rationale=rationale,
        missing_fields=list(completeness_op.missing_fields),
        risk_flags=risk_flags,
        needs_human_review=needs_human_review,
    )
    return packet, overrides


def _needs_human_review(
    synth: SynthesizerDecision,
    severity: Severity,
    has_danger: bool,
) -> bool:
    if synth.agreement_score < 0.6:
        return True
    if synth.conflicts:
        return True
    if has_danger:
        return True
    if severity in ("S0", "S1", "unknown"):
        return True
    return False


def _build_rationale(
    severity_op: SeverityOpinion,
    synth: SynthesizerDecision,
) -> list[str]:
    # Lead with natural-language rationale from severity specialist.
    rationale: list[str] = list(severity_op.rationale[:2])
    # Append notable synth decisions (upgrades, floors, conflicts).
    for note in synth.reasoning_notes:
        if any(k in note for k in ("upgraded", "floor", "forces", "unknown")):
            rationale.append(note)
    # TriagePacket schema caps rationale at 4 items.
    return rationale[:4]


def _bug_confidence(
    synth: SynthesizerDecision, severity_op: SeverityOpinion
) -> float:
    if synth.chosen_issue_kind != "bug":
        return 0.2
    # Attenuate severity_agent confidence by specialist agreement.
    return round(severity_op.confidence * synth.agreement_score, 3)
