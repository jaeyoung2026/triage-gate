"""Streamlit viz for triage-gate traces.

Run:
    streamlit run viz/app.py

Reads Trace JSON files from ../traces/ and renders:
  1. Single trace view — the three-specialist panel + synthesizer decision trail
  2. Bucket overview — how reports distributed across routes

Design principle: every visual element must be able to change a reviewer's
decision. Agreement gauge, conflict chips, field provenance icons, and the
decision trail all map to specific levers the reviewer can pull.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

# Make the package importable when running `streamlit run viz/app.py`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from triage_gate.schema import Trace

TRACES_DIR = ROOT / "traces"

ROUTE_EMOJI = {
    "auto_fix": "🟢",
    "human_engineer": "🔴",
    "support": "🔵",
    "pm": "🟣",
    "needs_more_info": "🟡",
}

SEVERITY_BADGE = {
    "S0": "🔴 S0",
    "S1": "🟠 S1",
    "S2": "🟡 S2",
    "S3": "🟢 S3",
    "unknown": "⚪ unknown",
}

STATUS_ICON = {
    "stated": "●",
    "inferred": "◐",
    "missing": "○",
}


@st.cache_data
def load_traces(trace_dir_str: str) -> list[dict]:
    """Load raw trace dicts so Streamlit's cache can pickle them safely."""
    traces_dir = Path(trace_dir_str)
    out: list[dict] = []
    for path in sorted(traces_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            Trace.model_validate(data)  # validate only; we pass dicts forward
            out.append(data)
        except (ValidationError, json.JSONDecodeError):
            continue
    return out


def _specialist(opinions: list[dict], name: str) -> dict | None:
    for op in opinions:
        if op.get("specialist") == name:
            return op
    return None


def render_single_trace(trace: dict) -> None:
    packet = trace["final_packet"]
    synth = trace["synthesizer_decision"]
    extracted = trace["extracted"]

    st.markdown(f"## {trace['report_id']}")
    st.caption(
        f"source: {trace['raw']['source_kind']} · "
        f"language: {extracted.get('language') or '—'} · "
        f"product_context: `{trace['product_context_version']}`"
    )

    # Raw text — always visible
    with st.container(border=True):
        st.markdown("**raw text**")
        st.text(trace["raw"]["raw_text"])

    # Intake panel
    with st.expander("intake extraction", expanded=False):
        c1, c2 = st.columns([1, 2])
        with c1:
            st.markdown(
                f"**preliminary_issue_kind**: `{extracted['fields']['preliminary_issue_kind']}`"
            )
            st.markdown(f"**language**: `{extracted.get('language') or '—'}`")
        with c2:
            st.markdown("**field provenance**")
            for name, src in extracted.get("field_sources", {}).items():
                icon = STATUS_ICON.get(src["status"], "?")
                quote = f' — *{src["quote"]}*' if src.get("quote") else ""
                st.markdown(f"- {icon} `{name}`: {src['status']}{quote}")

    # Three specialist panel
    st.markdown("### 3-specialist panel")
    sev = _specialist(trace["specialist_opinions"], "severity")
    risk = _specialist(trace["specialist_opinions"], "risk")
    comp = _specialist(trace["specialist_opinions"], "completeness")

    c1, c2, c3 = st.columns(3)
    with c1:
        with st.container(border=True):
            st.markdown("#### severity")
            if sev:
                st.markdown(
                    f"### {SEVERITY_BADGE.get(sev['severity'], sev['severity'])}"
                )
                st.caption(f"confidence: {sev['confidence']}")
                st.markdown(f"*{sev['impact_summary']}*")
                for r in sev["rationale"]:
                    st.markdown(f"- {r}")
    with c2:
        with st.container(border=True):
            st.markdown("#### risk")
            if risk:
                if risk["risk_flags"]:
                    chips = " ".join(f"`{f}`" for f in risk["risk_flags"])
                    st.markdown(f"### {chips}")
                else:
                    st.markdown("### *no flags*")
                st.caption(f"confidence: {risk['confidence']}")
                if risk.get("escalation_reason"):
                    st.markdown(f"*{risk['escalation_reason']}*")
                for r in risk["rationale"]:
                    st.markdown(f"- {r}")
    with c3:
        with st.container(border=True):
            st.markdown("#### completeness")
            if comp:
                suff = comp["info_sufficiency"]
                badge = {"high": "🟢 high", "medium": "🟡 medium", "low": "🔴 low"}
                st.markdown(f"### {badge.get(suff, suff)}")
                st.caption(f"confidence: {comp['confidence']}")
                if comp.get("missing_fields"):
                    st.markdown(
                        "**missing**: "
                        + ", ".join(f"`{m}`" for m in comp["missing_fields"])
                    )
                if comp.get("inferred_fields"):
                    st.markdown(
                        "**inferred**: "
                        + ", ".join(f"`{f}`" for f in comp["inferred_fields"])
                    )
                for r in comp["rationale"]:
                    st.markdown(f"- {r}")

    # Synthesizer
    st.markdown("### synthesizer")
    c1, c2 = st.columns([1, 3])
    with c1:
        score = synth["agreement_score"]
        st.metric("agreement", f"{score:.2f}")
        if score < 0.6:
            st.error("low")
        elif score < 0.85:
            st.warning("partial")
        else:
            st.success("high")
    with c2:
        if synth["conflicts"]:
            st.markdown("**conflicts**")
            for c in synth["conflicts"]:
                st.warning(c, icon="⚠️")
        else:
            st.success("no conflicts", icon="✅")

    st.markdown("**decision trail**")
    for note in synth["reasoning_notes"]:
        st.markdown(f"- {note}")

    if trace["overrides_applied"]:
        st.markdown("**hard overrides (decide layer)**")
        for o in trace["overrides_applied"]:
            st.error(o)

    # Final packet
    st.markdown("### → final packet")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**issue_kind**")
        st.markdown(f"## `{packet['issue_kind']}`")
    with c2:
        st.markdown("**severity**")
        st.markdown(f"## {SEVERITY_BADGE.get(packet['severity'], packet['severity'])}")
    with c3:
        st.markdown("**route**")
        st.markdown(
            f"## {ROUTE_EMOJI.get(packet['route'], '⚫')} `{packet['route']}`"
        )

    c1, c2 = st.columns(2)
    with c1:
        review = "✓ required" if packet["needs_human_review"] else "not required"
        st.markdown(f"**needs_human_review**: {review}")
        st.markdown(f"**bug_confidence**: `{packet['bug_confidence']}`")
    with c2:
        if packet["risk_flags"]:
            st.markdown(
                "**risk_flags**: "
                + ", ".join(f"`{f}`" for f in packet["risk_flags"])
            )
        if packet["missing_fields"]:
            st.markdown(
                "**missing**: "
                + ", ".join(f"`{m}`" for m in packet["missing_fields"])
            )

    with st.expander("packet rationale"):
        for r in packet["rationale"]:
            st.markdown(f"- {r}")

    # Timings (small, bottom)
    timings = trace.get("timings_ms", {})
    if timings:
        parts = [f"{k}: {v:.0f}ms" for k, v in timings.items()]
        st.caption(" · ".join(parts))


def render_buckets(traces: list[dict]) -> None:
    st.markdown("## routing buckets")

    by_route = Counter(t["final_packet"]["route"] for t in traces)
    by_issue = Counter(t["final_packet"]["issue_kind"] for t in traces)
    by_severity = Counter(t["final_packet"]["severity"] for t in traces)

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**by route**")
        for route, n in by_route.most_common():
            st.markdown(f"- {ROUTE_EMOJI.get(route, '⚫')} `{route}`: **{n}**")
        # Gate-health signal
        auto_fix_ratio = by_route.get("auto_fix", 0) / max(len(traces), 1)
        if auto_fix_ratio > 0.3:
            st.error(
                f"⚠️ {auto_fix_ratio:.0%} routed to auto_fix — gate may be too loose"
            )
    with c2:
        st.markdown("**by issue_kind**")
        for kind, n in by_issue.most_common():
            st.markdown(f"- `{kind}`: **{n}**")
    with c3:
        st.markdown("**by severity**")
        for sev, n in by_severity.most_common():
            st.markdown(f"- {SEVERITY_BADGE.get(sev, sev)}: **{n}**")

    st.markdown("## all reports")
    rows = []
    for t in traces:
        p = t["final_packet"]
        s = t["synthesizer_decision"]
        rows.append(
            {
                "report_id": t["report_id"],
                "issue_kind": p["issue_kind"],
                "severity": p["severity"],
                "route": p["route"],
                "risk_flags": ", ".join(p["risk_flags"]),
                "agreement": s["agreement_score"],
                "conflicts": len(s["conflicts"]),
                "human_review": "✓" if p["needs_human_review"] else "",
            }
        )
    df = pd.DataFrame(rows)
    st.dataframe(df, width="stretch", hide_index=True)

    # Conflict surface — anything worth investigating
    flagged = [t for t in traces if t["synthesizer_decision"]["conflicts"]]
    if flagged:
        st.markdown("## reports with conflicts")
        st.caption("these are the reports the evolve_agent will study first")
        for t in flagged:
            with st.container(border=True):
                st.markdown(f"**{t['report_id']}** — {t['final_packet']['route']}")
                for c in t["synthesizer_decision"]["conflicts"]:
                    st.markdown(f"- ⚠️ {c}")


def main() -> None:
    st.set_page_config(page_title="triage-gate", layout="wide")
    st.title("triage-gate")
    st.caption("multi-agent bug triage panel · codex hackathon demo")

    traces = load_traces(str(TRACES_DIR))
    if not traces:
        st.error(
            f"No traces found in {TRACES_DIR}. "
            "Run `python -m triage_gate run-all data/reports` first."
        )
        return

    with st.sidebar:
        st.header("view")
        view = st.radio(
            "mode",
            ["single trace", "bucket overview"],
            key="view",
            label_visibility="collapsed",
        )
        if view == "single trace":
            ids = [t["report_id"] for t in traces]
            selected = st.selectbox("pick a report", ids, key="selected")
        else:
            selected = None

        st.divider()
        st.caption(f"{len(traces)} traces loaded")
        st.caption(f"from `{TRACES_DIR.relative_to(ROOT)}`")

    if view == "single trace":
        trace = next(t for t in traces if t["report_id"] == selected)
        render_single_trace(trace)
    else:
        render_buckets(traces)


if __name__ == "__main__":
    main()
