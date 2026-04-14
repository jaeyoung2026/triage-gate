"""evolve_agent v1 — pattern-based improvement suggestions from trace history.

Reads every Trace in traces/ and surfaces four kinds of patterns:

    1. critical_path keyword false positives  (product_context.json tuning)
    2. low specialist agreement               (specialist prompt review)
    3. hard override activity                 (synthesizer matrix gaps)
    4. rules ↔ LLM risk-flag disagreement     (rules.py keyword tuning)

Output is a markdown report printed to stdout. Every suggestion names the
file that should change (product_context.json vs rules.py vs a prompt),
so a human reviewer can act on it without re-deriving the context.

This is the v1 stub. A later version will:
    - consume real OutcomeRecord feedback (not just same-batch conflicts)
    - use an LLM to draft the actual config/rules diffs
    - open PRs instead of printing markdown
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from triage_gate.schema import RiskFlag, Trace

DANGER_FLAGS: set[RiskFlag] = {
    "auth",
    "payment",
    "data_loss",
    "security",
    "outage",
}


def load_traces(traces_dir: Path) -> list[Trace]:
    out: list[Trace] = []
    for path in sorted(traces_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append(Trace.model_validate(data))
    return out


def _get_opinion(trace: Trace, name: str):
    for op in trace.specialist_opinions:
        if op.specialist == name:
            return op
    return None


def analyze(traces: list[Trace]) -> list[str]:
    """Produce a list of markdown-formatted suggestions."""
    suggestions: list[str] = []

    # ── Pattern 1: critical_path false-positive candidates ────────────────
    # A critical_path upgrade fired, but the severity specialist called S3
    # and risk_agent found no danger flags. That's the shape of a keyword
    # over-match in product_context.json — the keyword hit but nothing else
    # agreed that this is load-bearing.
    cp_fp: list[tuple[str, list[str]]] = []
    for t in traces:
        sev_op = _get_opinion(t, "severity")
        risk_op = _get_opinion(t, "risk")
        if sev_op is None or risk_op is None:
            continue
        has_danger = bool(set(risk_op.risk_flags) & DANGER_FLAGS)
        had_cp_upgrade = any(
            "critical path" in c and "forces floor" in c
            for c in t.synthesizer_decision.conflicts
        )
        if had_cp_upgrade and sev_op.severity == "S3" and not has_danger:
            cp_fp.append((t.report_id, t.synthesizer_decision.conflicts))

    if cp_fp:
        lines = [
            f"### ⚠ critical_path keyword false-positive candidates ({len(cp_fp)})",
            "",
            "A critical_path severity floor fired, but severity_agent called S3 "
            "and risk_agent found no danger flags. The keyword probably matched "
            "text that is not actually on the critical path.",
            "",
        ]
        for rid, conflicts in cp_fp:
            lines.append(f"- **{rid}**: {'; '.join(conflicts)}")
        lines.append("")
        lines.append(
            "**Target file**: `data/product_context.json` — narrow the `keywords` "
            "list on the triggered critical_path, or raise its "
            "`default_severity_floor`."
        )
        suggestions.append("\n".join(lines))

    # ── Pattern 2: low specialist agreement ───────────────────────────────
    low_agree = [
        t for t in traces if t.synthesizer_decision.agreement_score < 0.7
    ]
    if low_agree:
        lines = [
            f"### ⚠ low specialist agreement ({len(low_agree)})",
            "",
            "Reports where the three specialists disagreed. This is the most "
            "direct supervision signal the evolve loop gets.",
            "",
        ]
        for t in low_agree:
            s = t.synthesizer_decision
            lines.append(
                f"- **{t.report_id}**: agreement=`{s.agreement_score}`, "
                f"conflicts={s.conflicts}"
            )
        lines.append("")
        lines.append(
            "**Target**: specialist prompt for the disagreeing dimension. "
            "Check `triage_gate/specialists/{severity,risk,completeness}.py`."
        )
        suggestions.append("\n".join(lines))

    # ── Pattern 3: hard override activity ─────────────────────────────────
    override_kinds: Counter[str] = Counter()
    for t in traces:
        for o in t.overrides_applied:
            # Bucket by the rule tag at the front of each override message.
            if ":" in o:
                prefix = o.split(":", 1)[1].strip().split(" ")[0:3]
                key = " ".join(prefix)
            else:
                key = o
            override_kinds[key] += 1

    if override_kinds:
        total = sum(override_kinds.values())
        lines = [
            f"### decide-layer override activity ({total} fired across {len(traces)} traces)",
            "",
            "If the same override fires repeatedly, its rule is doing real work "
            "and should be promoted into the synthesizer matrix so it is visible "
            "in the decision trail instead of kicking in at the last moment.",
            "",
        ]
        for key, n in override_kinds.most_common():
            lines.append(f"- `{key}`: {n}×")
        lines.append("")
        lines.append(
            "**Target**: `triage_gate/synthesize.py` matrix layers, or "
            "`triage_gate/decide.py` if the rule belongs as a safety gate."
        )
        suggestions.append("\n".join(lines))

    # ── Pattern 4: rules ↔ LLM risk-flag disagreement ─────────────────────
    rule_miss: list[tuple[str, list[str], list[str]]] = []
    for t in traces:
        risk_op = _get_opinion(t, "risk")
        if risk_op is None:
            continue
        llm_flags = set(risk_op.risk_flags)
        rule_flags = set(t.rule_flags_raw)
        llm_only = sorted(llm_flags - rule_flags)
        rule_only = sorted(rule_flags - llm_flags)
        if llm_only or rule_only:
            rule_miss.append((t.report_id, llm_only, rule_only))

    if rule_miss:
        lines = [
            f"### rules ↔ LLM risk-flag disagreement ({len(rule_miss)})",
            "",
            "Cases where the keyword rules and the LLM risk_agent disagreed on "
            "which flags should fire. LLM-only flags suggest missing keywords "
            "in rules.py; rules-only flags suggest over-eager keyword matching.",
            "",
        ]
        for rid, llm_only, rule_only in rule_miss:
            lines.append(
                f"- **{rid}**: LLM-only={llm_only or '∅'}, rules-only={rule_only or '∅'}"
            )
        lines.append("")
        lines.append(
            "**Target**: `triage_gate/rules.py` `RISK_KEYWORDS` dict. "
            "Tighten or extend the keyword lists based on which direction dominates."
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

    suggestions = analyze(traces)
    if not suggestions:
        print("No improvement patterns detected in this batch.")
        return 0

    for s in suggestions:
        print(s)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
