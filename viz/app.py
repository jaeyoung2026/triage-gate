"""Streamlit viz for triage-gate traces (post-simplification).

Run:
    streamlit run viz/app.py

Reads Trace JSON files from ../traces/ and renders:
  1. Single trace view — extraction + 4-dimension analysis + severity upgrade trail
  2. Bucket overview — how reports distributed across routes

Trace shape is the new one from schema.Trace (single `analysis` block, not
three specialist opinions).
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
    traces_dir = Path(trace_dir_str)
    out: list[dict] = []
    for path in sorted(traces_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            Trace.model_validate(data)
            out.append(data)
        except (ValidationError, json.JSONDecodeError):
            continue
    return out


def render_single_trace(trace: dict) -> None:
    packet = trace["final_packet"]
    analysis = trace["analysis"]

    st.markdown(f"## {trace['report_id']}")
    st.caption(
        f"source: {trace['raw']['source_kind']} · "
        f"language: {analysis.get('language') or '—'} · "
        f"product_context: `{trace['product_context_version']}`"
    )

    with st.container(border=True):
        st.markdown("**raw text**")
        st.text(trace["raw"]["raw_text"])

    # Intake (extraction) block
    with st.expander("intake extraction", expanded=False):
        c1, c2 = st.columns([1, 2])
        with c1:
            st.markdown(
                f"**preliminary_issue_kind**: `{analysis['preliminary_issue_kind']}`"
            )
            st.markdown(f"**language**: `{analysis.get('language') or '—'}`")
        with c2:
            st.markdown("**field provenance**")
            for name, src in analysis.get("field_sources", {}).items():
                if src is None:
                    continue
                icon = STATUS_ICON.get(src["status"], "?")
                quote = f' — *{src["quote"]}*' if src.get("quote") else ""
                st.markdown(f"- {icon} `{name}`: {src['status']}{quote}")

    # Four dimensions of the analyze() output
    st.markdown("### analyze — 4 dimensions from one LLM call")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        with st.container(border=True):
            st.markdown("#### severity")
            st.markdown(
                f"### {SEVERITY_BADGE.get(analysis['severity_call'], analysis['severity_call'])}"
            )
            st.caption(f"*{analysis['impact_summary']}*")
            for r in analysis["severity_rationale"]:
                st.markdown(f"- {r}")
    with c2:
        with st.container(border=True):
            st.markdown("#### risk")
            if analysis["detected_risks"]:
                chips = " ".join(f"`{f}`" for f in analysis["detected_risks"])
                st.markdown(f"### {chips}")
            else:
                st.markdown("### *no flags*")
            for r in analysis["risk_rationale"]:
                st.markdown(f"- {r}")
    with c3:
        with st.container(border=True):
            st.markdown("#### completeness")
            suff = analysis["info_sufficiency"]
            badge = {"high": "🟢 high", "medium": "🟡 medium", "low": "🔴 low"}
            st.markdown(f"### {badge.get(suff, suff)}")
            if analysis["missing_fields"]:
                st.markdown(
                    "**missing**: "
                    + ", ".join(f"`{m}`" for m in analysis["missing_fields"])
                )
    with c4:
        with st.container(border=True):
            st.markdown("#### self_concerns")
            if analysis["self_concerns"]:
                for c in analysis["self_concerns"]:
                    st.warning(c, icon="🤔")
            else:
                st.success("no concerns", icon="✅")

    # Gate layer — what the programmatic safety did on top of the LLM call
    st.markdown("### gate — programmatic safety layer")
    c1, c2 = st.columns([1, 3])
    with c1:
        score = trace["agreement_score"]
        st.metric("agreement", f"{score:.2f}")
        if score < 0.6:
            st.error("low")
        elif score < 0.85:
            st.warning("partial")
        else:
            st.success("high")
    with c2:
        if trace["severity_upgrades"]:
            st.markdown("**severity upgrades**")
            for u in trace["severity_upgrades"]:
                st.info(u, icon="⬆️")
        if trace["conflicts"]:
            st.markdown("**conflicts / concerns**")
            for c in trace["conflicts"]:
                st.warning(c, icon="⚠️")
        if not trace["severity_upgrades"] and not trace["conflicts"]:
            st.success("no upgrades, no conflicts", icon="✅")

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
        st.markdown(f"## {ROUTE_EMOJI.get(packet['route'], '⚫')} `{packet['route']}`")

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
        rows.append(
            {
                "report_id": t["report_id"],
                "issue_kind": p["issue_kind"],
                "severity": p["severity"],
                "route": p["route"],
                "risk_flags": ", ".join(p["risk_flags"]),
                "agreement": t["agreement_score"],
                "upgrades": len(t["severity_upgrades"]),
                "conflicts": len(t["conflicts"]),
                "human_review": "✓" if p["needs_human_review"] else "",
            }
        )
    df = pd.DataFrame(rows)
    st.dataframe(df, width="stretch", hide_index=True)

    flagged = [t for t in traces if t["conflicts"] or t["severity_upgrades"]]
    if flagged:
        st.markdown("## reports with upgrades or conflicts")
        st.caption("these are the reports the evolve_agent will study first")
        for t in flagged:
            with st.container(border=True):
                st.markdown(f"**{t['report_id']}** — {t['final_packet']['route']}")
                for u in t["severity_upgrades"]:
                    st.markdown(f"- ⬆️ {u}")
                for c in t["conflicts"]:
                    st.markdown(f"- ⚠️ {c}")


def main() -> None:
    st.set_page_config(page_title="triage-gate", layout="wide")
    st.title("triage-gate")
    st.caption("one LLM analyze + programmatic safety gate · codex hackathon demo")

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
