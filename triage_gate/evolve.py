"""evolve_agent — pattern-based improvement suggestions from trace history.

Reads every Trace in traces/ and surfaces four kinds of patterns:

    1. critical_path keyword false positives  (product_context.json tuning)
    2. low agreement_score                    (rules ↔ LLM disagreement too high)
    3. self_concerns surface                   (model's own doubts as signal)
    4. rules ↔ LLM risk-flag disagreement     (rules.py keyword tuning)

Output is a markdown report printed to stdout. Every suggestion names the
file that should change (product_context.json vs rules.py vs analyze prompt),
so a human reviewer can act without re-deriving the context.
"""

from __future__ import annotations

import json
from pathlib import Path

from triage_gate.schema import Trace

DANGER_FLAGS = {"auth", "payment", "data_loss", "security", "outage"}


def load_traces(traces_dir: Path) -> list[Trace]:
    out: list[Trace] = []
    for path in sorted(traces_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append(Trace.model_validate(data))
    return out


def analyze_traces(traces: list[Trace]) -> list[str]:
    """Produce a list of markdown-formatted suggestions from trace history."""
    suggestions: list[str] = []

    # ── Pattern 1: critical_path false-positive candidates ────────────────
    # A critical_path floor fired, but the LLM's own severity call was S3
    # and no danger flags were detected. Likely shape of an over-broad keyword.
    cp_fp: list[tuple[str, list[str]]] = []
    for t in traces:
        a = t.analysis
        has_danger = bool(set(a.detected_risks) & DANGER_FLAGS)
        had_cp_upgrade = any(
            "critical path" in u and "forced floor" in u
            for u in t.severity_upgrades
        )
        if had_cp_upgrade and a.severity_call == "S3" and not has_danger:
            cp_fp.append((t.report_id, t.severity_upgrades))

    if cp_fp:
        lines = [
            f"### ⚠ critical_path keyword false-positive candidates ({len(cp_fp)})",
            "",
            "A critical_path severity floor fired, but the LLM called S3 and no "
            "danger flags were detected. The keyword probably matched text that "
            "is not actually on the critical path.",
            "",
        ]
        for rid, upgrades in cp_fp:
            lines.append(f"- **{rid}**: {'; '.join(upgrades)}")
        lines.append("")
        lines.append(
            "**Target file**: `data/product_context.json` — narrow the `keywords` "
            "list on the triggered critical_path, or raise its "
            "`default_severity_floor`."
        )
        suggestions.append("\n".join(lines))

    # ── Pattern 2: low agreement_score ────────────────────────────────────
    low_agree = [t for t in traces if t.agreement_score < 0.7]
    if low_agree:
        lines = [
            f"### ⚠ low agreement_score ({len(low_agree)})",
            "",
            "Reports where rules and LLM disagreed heavily on risk flags, or "
            "the model flagged multiple self_concerns.",
            "",
        ]
        for t in low_agree:
            lines.append(
                f"- **{t.report_id}**: agreement=`{t.agreement_score}`, "
                f"conflicts={t.conflicts[:3]}"
            )
        lines.append("")
        lines.append(
            "**Target**: `triage_gate/analyze.py` SYSTEM_PROMPT — clarify the "
            "dimension the model is wobbling on, or `triage_gate/rules.py` "
            "keywords if the disagreement is on risk flags."
        )
        suggestions.append("\n".join(lines))

    # ── Pattern 3: self_concerns surface ──────────────────────────────────
    with_concerns = [(t, t.analysis.self_concerns) for t in traces if t.analysis.self_concerns]
    if with_concerns:
        lines = [
            f"### self_concerns raised by the model ({len(with_concerns)})",
            "",
            "The model flagged uncertainty about its own answer on these reports. "
            "Recurring concern patterns point to prompt or rubric gaps.",
            "",
        ]
        for t, concerns in with_concerns:
            for c in concerns:
                lines.append(f"- **{t.report_id}**: {c}")
        lines.append("")
        lines.append(
            "**Target**: `triage_gate/analyze.py` SYSTEM_PROMPT. If multiple "
            "reports have similar concerns, extend the relevant STEP with a "
            "clarifying rule."
        )
        suggestions.append("\n".join(lines))

    # ── Pattern 4: rules ↔ LLM risk-flag disagreement ─────────────────────
    rule_miss: list[tuple[str, list[str], list[str]]] = []
    for t in traces:
        llm_flags = set(t.analysis.detected_risks)
        rule_flags = set(t.rule_flags_raw)
        llm_only = sorted(llm_flags - rule_flags)
        rule_only = sorted(rule_flags - llm_flags)
        if llm_only or rule_only:
            rule_miss.append((t.report_id, llm_only, rule_only))

    if rule_miss:
        lines = [
            f"### rules ↔ LLM risk-flag disagreement ({len(rule_miss)})",
            "",
            "Cases where the keyword rules and the LLM's adversarial scan "
            "disagreed. LLM-only flags suggest missing keywords in rules.py; "
            "rules-only flags suggest over-eager keyword matching.",
            "",
        ]
        for rid, llm_only, rule_only in rule_miss:
            lines.append(
                f"- **{rid}**: LLM-only={llm_only or '∅'}, rules-only={rule_only or '∅'}"
            )
        lines.append("")
        lines.append(
            "**Target**: `triage_gate/rules.py` `RISK_KEYWORDS` dict. "
            "Tighten or extend keyword lists based on which direction dominates."
        )
        suggestions.append("\n".join(lines))

    return suggestions


def main() -> int:
    import sys

    traces_dir = Path("traces")
    traces = load_traces(traces_dir)
    if not traces:
        print(f"No traces found in {traces_dir}", file=sys.stderr)
        return 1

    print("# evolve_agent report")
    print()
    print(f"*{len(traces)} traces analyzed*")
    print()

    suggestions = analyze_traces(traces)
    if not suggestions:
        print("No improvement patterns detected in this batch.")
        return 0

    for s in suggestions:
        print(s)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
