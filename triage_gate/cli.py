"""Command-line entry point for triage-gate.

Usage:
    python -m triage_gate run <report.json>
    python -m triage_gate run-all <reports_dir>

Each run produces a Trace JSON at traces/<report_id>.json and prints a short
summary to stdout. Traces are the input to the Streamlit viz.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

from triage_gate.decide import decide
from triage_gate.intake import intake
from triage_gate.rules import detect_risk_flags_on_raw
from triage_gate.schema import ProductContext, RawReport, Trace
from triage_gate.specialists import (
    completeness_agent,
    risk_agent,
    severity_agent,
)
from triage_gate.synthesize import synthesize

DEFAULT_CONTEXT = "data/product_context.json"
DEFAULT_TRACES_DIR = "traces"


def run_one(
    report_path: Path,
    ctx: ProductContext,
    traces_dir: Path,
) -> Trace:
    raw_data = json.loads(report_path.read_text(encoding="utf-8"))
    raw = RawReport(**raw_data)

    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    extracted = intake(raw, ctx)
    timings["intake_ms"] = (time.perf_counter() - t0) * 1000

    rule_flags_raw = detect_risk_flags_on_raw(raw.raw_text)

    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        f_sev = pool.submit(severity_agent, extracted, ctx)
        f_risk = pool.submit(risk_agent, extracted, ctx)
        f_comp = pool.submit(completeness_agent, extracted, ctx)
        sev_op = f_sev.result()
        risk_op = f_risk.result()
        comp_op = f_comp.result()
    timings["specialists_parallel_ms"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    synth = synthesize(extracted, sev_op, risk_op, comp_op, rule_flags_raw, ctx)
    packet, overrides = decide(synth, sev_op, risk_op, comp_op, extracted)
    timings["synth_decide_ms"] = (time.perf_counter() - t0) * 1000

    trace = Trace(
        report_id=raw.report_id,
        raw=raw,
        extracted=extracted,
        product_context_version=ctx.version,
        rule_flags_raw=rule_flags_raw,
        rule_flags_extracted=[],
        specialist_opinions=[sev_op, risk_op, comp_op],
        synthesizer_decision=synth,
        overrides_applied=overrides,
        final_packet=packet,
        timings_ms=timings,
    )

    traces_dir.mkdir(parents=True, exist_ok=True)
    out_path = traces_dir / f"{raw.report_id}.json"
    out_path.write_text(trace.model_dump_json(indent=2), encoding="utf-8")
    return trace


def print_summary(trace: Trace) -> None:
    p = trace.final_packet
    s = trace.synthesizer_decision
    print(f"── {trace.report_id} ──")
    print(f"  issue_kind:         {p.issue_kind}")
    print(f"  severity:           {p.severity}")
    print(f"  route:              {p.route}")
    print(f"  needs_human_review: {p.needs_human_review}")
    print(f"  bug_confidence:     {p.bug_confidence}")
    print(f"  risk_flags:         {p.risk_flags}")
    print(f"  missing_fields:     {p.missing_fields}")
    print(f"  agreement_score:    {s.agreement_score}")
    if s.conflicts:
        print("  conflicts:")
        for c in s.conflicts:
            print(f"    • {c}")
    if trace.overrides_applied:
        print("  hard overrides:")
        for o in trace.overrides_applied:
            print(f"    • {o}")
    print("  timings:")
    for k, v in trace.timings_ms.items():
        print(f"    {k}: {v:.0f} ms")


def cmd_run(args) -> int:
    ctx = ProductContext.load(args.product_context)
    report_path = Path(args.report)
    if not report_path.exists():
        print(f"error: report not found: {report_path}", file=sys.stderr)
        return 1
    trace = run_one(report_path, ctx, Path(args.traces_dir))
    print_summary(trace)
    return 0


def cmd_evolve(args) -> int:
    from triage_gate.evolve import analyze, load_traces

    traces_dir = Path(args.traces_dir)
    if not traces_dir.exists():
        print(f"error: traces dir not found: {traces_dir}", file=sys.stderr)
        return 1
    traces = load_traces(traces_dir)
    if not traces:
        print(f"error: no traces in {traces_dir}", file=sys.stderr)
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


def cmd_run_all(args) -> int:
    ctx = ProductContext.load(args.product_context)
    reports_dir = Path(args.reports_dir)
    if not reports_dir.exists():
        print(f"error: reports dir not found: {reports_dir}", file=sys.stderr)
        return 1
    report_paths = sorted(reports_dir.glob("*.json"))
    if not report_paths:
        print(f"error: no *.json reports found in {reports_dir}", file=sys.stderr)
        return 1
    failures = 0
    for path in report_paths:
        try:
            trace = run_one(path, ctx, Path(args.traces_dir))
            print_summary(trace)
            print()
        except Exception as exc:
            failures += 1
            print(f"error on {path.name}: {exc}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="triage-gate",
        description="Bug report triage gate — multi-agent panel + synthesizer.",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    p_run = subs.add_parser("run", help="Triage one report")
    p_run.add_argument("report", help="Path to report JSON file")
    p_run.add_argument("--product-context", default=DEFAULT_CONTEXT)
    p_run.add_argument("--traces-dir", default=DEFAULT_TRACES_DIR)
    p_run.set_defaults(func=cmd_run)

    p_all = subs.add_parser("run-all", help="Triage every report in a directory")
    p_all.add_argument("reports_dir", help="Directory of report JSON files")
    p_all.add_argument("--product-context", default=DEFAULT_CONTEXT)
    p_all.add_argument("--traces-dir", default=DEFAULT_TRACES_DIR)
    p_all.set_defaults(func=cmd_run_all)

    p_evolve = subs.add_parser(
        "evolve",
        help="Analyze traces and print improvement suggestions",
    )
    p_evolve.add_argument("--traces-dir", default=DEFAULT_TRACES_DIR)
    p_evolve.set_defaults(func=cmd_evolve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
