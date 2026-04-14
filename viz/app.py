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
        f"{trace['raw']['source_kind']} · "
        f"{analysis.get('language') or '—'}"
    )

    # 1. Raw text — context only
    with st.container(border=True):
        st.text(trace["raw"]["raw_text"])

    # 2. Hero verdict — the three things the operator needs at a glance
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**판정**")
        st.markdown(f"## `{packet['issue_kind']}`")
    with c2:
        st.markdown("**심각도**")
        st.markdown(
            f"## {SEVERITY_BADGE.get(packet['severity'], packet['severity'])}"
        )
    with c3:
        st.markdown("**라우트**")
        st.markdown(
            f"## {ROUTE_EMOJI.get(packet['route'], '⚫')} `{packet['route']}`"
        )

    # 3. LLM narration — the star of the simplified dashboard
    narration = analysis.get("narration") or ""
    if narration:
        st.info(narration, icon="💬")

    # 4. Gate upgrade note (if severity was pushed up by rules / critical path)
    upgrades = [u for u in trace.get("severity_upgrades", []) if "forced" in u or "upgraded" in u]
    if upgrades:
        for u in upgrades:
            st.caption(f"⬆️ 게이트 조정: {u}")

    # 5. Self-concerns — the model's own doubts as a review signal
    if analysis.get("self_concerns"):
        for concern in analysis["self_concerns"]:
            st.warning(concern, icon="🤔")

    # 6. Review status
    if packet["needs_human_review"]:
        st.error("🔴 사람 검토 필요")
    else:
        st.success("🟢 검토 불필요")

    # 7. Key flags and missing fields — minimal chips
    chip_row = []
    if packet["risk_flags"]:
        chip_row.append(
            "위험 플래그: " + " ".join(f"`{f}`" for f in packet["risk_flags"])
        )
    if packet["missing_fields"]:
        chip_row.append(
            "누락된 정보: "
            + " ".join(f"`{m}`" for m in packet["missing_fields"][:5])
        )
    for row in chip_row:
        st.markdown(row)

    # 8. Technical details — collapsed by default for engineers
    with st.expander("자세히 — 기술 세부사항", expanded=False):
        # 4-dim analyze breakdown
        st.markdown("**analyze — 4 dimensions**")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown("*severity*")
            st.markdown(
                f"{SEVERITY_BADGE.get(analysis['severity_call'], analysis['severity_call'])}"
            )
            st.caption(analysis["impact_summary"])
        with c2:
            st.markdown("*risk*")
            if analysis["detected_risks"]:
                st.markdown(" ".join(f"`{f}`" for f in analysis["detected_risks"]))
            else:
                st.markdown("*no flags*")
        with c3:
            st.markdown("*completeness*")
            suff = analysis["info_sufficiency"]
            badge = {"high": "🟢 high", "medium": "🟡 medium", "low": "🔴 low"}
            st.markdown(f"{badge.get(suff, suff)}")
        with c4:
            st.markdown("*agreement*")
            st.markdown(f"`{trace['agreement_score']:.2f}`")

        # Rationale lists
        if analysis["severity_rationale"]:
            st.markdown("**severity rationale**")
            for r in analysis["severity_rationale"]:
                st.markdown(f"- {r}")
        if analysis["risk_rationale"]:
            st.markdown("**risk rationale**")
            for r in analysis["risk_rationale"]:
                st.markdown(f"- {r}")

        # Field provenance
        if analysis.get("field_sources"):
            st.markdown("**field provenance**")
            for name, src in analysis["field_sources"].items():
                if src is None:
                    continue
                icon = STATUS_ICON.get(src["status"], "?")
                quote = f' — *{src["quote"]}*' if src.get("quote") else ""
                st.markdown(f"- {icon} `{name}`: {src['status']}{quote}")

        # Conflicts (rules↔LLM disagreements + self_concerns)
        if trace.get("conflicts"):
            st.markdown("**conflicts**")
            for c in trace["conflicts"]:
                st.markdown(f"- ⚠️ {c}")

        # Timings
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
