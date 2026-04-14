"""gate — programmatic safety gate that turns Analysis into TriagePacket.

Pure function, no LLM. Applies:

    1. Fast path for non-bug issue kinds (feature_request → pm, etc.)
    2. Severity upgrades (rules danger flags, critical_path floors).
       Severity can only go UP here, never DOWN — the LLM's call is a floor,
       rules and product context are ceilings pushing it higher.
    3. Route matrix (severity × danger × info_sufficiency).
    4. Hard invariants (S0/S1 always human, danger blocks auto_fix).

Also records:
    - `severity_upgrades`    : each upgrade fired, in order
    - `conflicts`            : rules↔LLM disagreements and self_concerns
    - `agreement_score`      : a single 0..1 confidence derived from rules
                                overlap and self_concern penalty
"""

from __future__ import annotations

from triage_gate.rules import detect_critical_paths, detect_risk_flags_on_raw
from triage_gate.schema import (
    Analysis,
    InfoSufficiency,
    ProductContext,
    RawReport,
    RiskFlag,
    Route,
    Severity,
    TriagePacket,
)

DANGER_FLAGS: set[RiskFlag] = {
    "auth",
    "payment",
    "data_loss",
    "security",
    "outage",
}

SEVERITY_RANK: dict[Severity, int] = {
    "unknown": 0,
    "S3": 1,
    "S2": 2,
    "S1": 3,
    "S0": 4,
}


class GateResult:
    """Structured output of gate(). Carries fields Trace needs."""

    def __init__(
        self,
        packet: TriagePacket,
        rule_flags_raw: list[RiskFlag],
        severity_upgrades: list[str],
        conflicts: list[str],
        agreement_score: float,
    ):
        self.packet = packet
        self.rule_flags_raw = rule_flags_raw
        self.severity_upgrades = severity_upgrades
        self.conflicts = conflicts
        self.agreement_score = agreement_score


def gate(
    raw: RawReport,
    analysis: Analysis,
    ctx: ProductContext,
) -> GateResult:
    """Apply safety gates and build the final packet."""
    upgrades: list[str] = []
    conflicts: list[str] = []

    rule_flags_raw = detect_risk_flags_on_raw(raw.raw_text)

    # Log rules↔LLM flag disagreement as a conflict (evolve_agent supervision).
    llm_only = sorted(set(analysis.detected_risks) - set(rule_flags_raw))
    rule_only = sorted(set(rule_flags_raw) - set(analysis.detected_risks))
    if llm_only:
        conflicts.append(f"LLM raised flags rules did not: {llm_only}")
    if rule_only:
        conflicts.append(f"Rules raised flags LLM did not: {rule_only}")

    # Self-concerns become conflicts → feed into needs_human_review
    for concern in analysis.self_concerns:
        conflicts.append(f"self_concern: {concern}")

    # ── Fast path: non-bug ────────────────────────────────────────────────
    if analysis.preliminary_issue_kind != "bug":
        return _non_bug_result(raw, analysis, rule_flags_raw, conflicts)

    # ── Start from LLM severity ───────────────────────────────────────────
    severity = analysis.severity_call

    # ── Severity upgrade 1: danger flag floor (rules ∪ LLM) ──────────────
    all_danger = sorted(
        (set(analysis.detected_risks) | set(rule_flags_raw)) & DANGER_FLAGS
    )
    if all_danger and _rank(severity) < _rank("S2"):
        prev = severity
        severity = "S2"
        upgrades.append(
            f"danger flags {all_danger} upgraded severity {prev}→S2"
        )

    # ── Severity upgrade 2: critical_path floor (product context) ────────
    matched_paths = detect_critical_paths(raw.raw_text, ctx.critical_paths)
    if matched_paths:
        floor = _max_severity([p.default_severity_floor for p in matched_paths])
        path_names = [p.name for p in matched_paths]
        if _rank(severity) < _rank(floor):
            prev = severity
            severity = floor
            upgrades.append(
                f"critical path {path_names} forced floor {floor} (was {prev})"
            )
        else:
            upgrades.append(
                f"critical path {path_names} matched (severity already ≥ floor)"
            )

    # ── Route matrix ──────────────────────────────────────────────────────
    route = _route_for(
        severity,
        has_danger=bool(all_danger),
        info=analysis.info_sufficiency,
    )

    # ── Hard invariants (defense in depth) ───────────────────────────────
    if all_danger and route == "auto_fix":
        route = "human_engineer"
    if severity in ("S0", "S1") and route != "human_engineer":
        route = "human_engineer"

    # ── Agreement score + needs_human_review ─────────────────────────────
    agreement = _agreement(analysis, rule_flags_raw)

    needs_review = bool(
        analysis.self_concerns
        or all_danger
        or severity in ("S0", "S1", "unknown")
        or agreement < 0.6
    )

    rationale = _build_rationale(analysis, upgrades)
    bug_conf = round(agreement, 3)

    # Packet risk_flags = union of LLM and rules. Downstream consumers must
    # see the full risk landscape, not just one source.
    all_risk_flags = sorted(set(analysis.detected_risks) | set(rule_flags_raw))

    packet = TriagePacket(
        report_id=raw.report_id,
        issue_kind="bug",
        bug_confidence=bug_conf,
        severity=severity,
        route=route,
        rationale=rationale,
        missing_fields=list(analysis.missing_fields),
        risk_flags=all_risk_flags,
        needs_human_review=needs_review,
    )
    return GateResult(
        packet=packet,
        rule_flags_raw=rule_flags_raw,
        severity_upgrades=upgrades,
        conflicts=conflicts,
        agreement_score=agreement,
    )


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────


def _non_bug_result(
    raw: RawReport,
    analysis: Analysis,
    rule_flags_raw: list[RiskFlag],
    conflicts: list[str],
) -> GateResult:
    kind = analysis.preliminary_issue_kind
    route: Route
    if kind == "feature_request":
        route = "pm"
    elif kind == "support_question":
        route = "support"
    elif kind == "duplicate":
        route = "human_engineer"
    else:  # insufficient_info
        route = "needs_more_info"

    # Pick a short natural-language rationale from whatever the LLM wrote.
    rationale = (
        list(analysis.severity_rationale[:2])
        or list(analysis.risk_rationale[:2])
        or [f"intake classified as {kind}"]
    )[:4]

    packet = TriagePacket(
        report_id=raw.report_id,
        issue_kind=kind,
        bug_confidence=0.2,
        severity="unknown",
        route=route,
        rationale=rationale,
        missing_fields=list(analysis.missing_fields),
        risk_flags=[],  # non-bug paths never propagate risk flags downstream
        needs_human_review=True,
    )
    return GateResult(
        packet=packet,
        rule_flags_raw=rule_flags_raw,
        severity_upgrades=[],
        conflicts=conflicts,
        agreement_score=1.0,
    )


def _rank(s: Severity) -> int:
    return SEVERITY_RANK[s]


def _max_severity(severities: list[Severity]) -> Severity:
    return max(severities, key=lambda s: SEVERITY_RANK[s])


def _route_for(
    severity: Severity,
    *,
    has_danger: bool,
    info: InfoSufficiency,
) -> Route:
    if severity == "unknown":
        return "needs_more_info"
    if severity in ("S0", "S1"):
        return "human_engineer"
    if has_danger:
        return "human_engineer"
    if info == "low":
        return "needs_more_info"
    if severity == "S2":
        return "auto_fix" if info == "high" else "human_engineer"
    # S3 + medium/high
    return "auto_fix"


def _agreement(analysis: Analysis, rule_flags_raw: list[RiskFlag]) -> float:
    """Single-LLM confidence — Jaccard with rules minus self-concern penalty."""
    llm = set(analysis.detected_risks)
    rules = set(rule_flags_raw)
    union = llm | rules
    jaccard = 1.0 if not union else len(llm & rules) / len(union)
    concern_penalty = min(len(analysis.self_concerns) * 0.15, 0.5)
    return round(max(0.0, jaccard - concern_penalty), 3)


def _build_rationale(
    analysis: Analysis,
    upgrades: list[str],
) -> list[str]:
    r = list(analysis.severity_rationale[:2])
    for u in upgrades:
        if "forced" in u or "upgraded" in u:
            r.append(u)
    return r[:4] if r else ["(no rationale)"]
