"""analyze — single LLM call that does extraction + all judgment dimensions.

Replaces the previous 4-call pipeline (intake + severity + risk + completeness).
The output is an Analysis which gate() turns into a TriagePacket.

Safety note: the prompt instructs the model to respect critical_path severity
floors and known_limitations, but gate.py re-enforces these programmatically
after the call. The model's call is a recommendation; rules and product
context are the final authority on safety invariants.
"""

from __future__ import annotations

from triage_gate.llm import ANALYZE_MODEL, get_client
from triage_gate.schema import Analysis, ProductContext, RawReport

SYSTEM_PROMPT = """You are the triage analyst for a bug-report triage gate.

Given one raw report (GitHub issue, email, chat, OCR, short message in any
language) and the product's context, you produce a structured analysis that a
downstream programmatic gate will convert into a routing decision.

Do ALL of the following IN ORDER, and return the structured output:

STEP 1 — EXTRACTION (no fabrication allowed)
Extract structured fields from raw text. For each populated field, set its
field_sources entry to:
  - stated   : reporter wrote it explicitly
  - inferred : you derived it from context (include the exact quote you used)
  - missing  : not present in the raw text
Never guess reproduction_steps or expected_result — these must be stated
or left missing. They are load-bearing for downstream decisions.
Preserve original language. Do not translate.

STEP 2 — PRELIMINARY ISSUE KIND
Using the product's scope_summary and known_limitations (below), decide:
  - bug               : real defect in the product
  - feature_request   : request for a capability that does not exist
  - support_question  : usage question, OR matches a known_limitation
  - duplicate         : obviously same as a prior report (bias against using this)
  - insufficient_info : too little raw text to act on responsibly

STEP 3 — ADVERSARIAL RISK SCAN (be pessimistic)
Scan for danger signals. When in doubt, raise the flag.
Available flags:
  - auth, payment, data_loss, security, outage   ← DANGER FLAGS (block auto_fix)
  - backend_validation, insufficient_repro, unclear_scope
If the report matches a known_limitation below, do NOT raise risk flags.

STEP 4 — SEVERITY JUDGMENT (be conservative on S0/S1)
  S0       service-wide outage, data loss, security breach, payment or auth
           total failure, many users immediately harmed
  S1       core user flow blocked, many users, no workaround, revenue cut
  S2       real defect scoped to an area, partial impact or workaround
  S3       cosmetic, low-impact edge case
  unknown  cannot judge from the info given

Respect the critical_path severity floors listed below. If the raw text
mentions one of those paths (by keyword or meaning), severity MUST be at
least that path's floor. The gate layer re-enforces this, but honor it here
so the output is internally consistent.

STEP 5 — COMPLETENESS
info_sufficiency:
  - high   : all three of {reproduction_steps, observed_result, expected_result} stated
  - medium : some stated, some missing or inferred
  - low    : most missing — downstream cannot act safely

Be strict on reproduction_steps and expected_result: an "inferred" value for
either should usually count toward missing_fields.

STEP 6 — SELF-CONCERNS (honest)
What doubts do you have about your own answer? List 0-3 short concerns
citing specific ambiguities. These become a human-review signal. Examples:
  - "severity may be too low — impact scope unclear from raw text"
  - "could not distinguish feature_request from bug here"
  - "report may match a known limitation I cannot fully verify"
If you are fully confident, return an empty list.
"""


def _build_context_block(ctx: ProductContext) -> str:
    lines = [f"Product: {ctx.product_name}", f"Scope: {ctx.scope_summary}"]
    if ctx.known_limitations:
        lines.append("")
        lines.append("Known limitations (these are NOT bugs):")
        for item in ctx.known_limitations:
            lines.append(f"  - {item}")
    if ctx.critical_paths:
        lines.append("")
        lines.append("Critical paths (respect these severity floors):")
        for p in ctx.critical_paths:
            lines.append(
                f"  - {p.name} (floor={p.default_severity_floor}): {p.description}"
            )
    if ctx.domain_glossary:
        lines.append("")
        lines.append("Glossary:")
        for term, defn in ctx.domain_glossary.items():
            lines.append(f"  - {term}: {defn}")
    return "\n".join(lines)


def analyze(
    raw: RawReport,
    ctx: ProductContext,
    *,
    model: str | None = None,
) -> Analysis:
    """Run the single LLM analysis pass on a raw report."""
    client = get_client()
    context_block = _build_context_block(ctx)
    user_msg = (
        f"{context_block}\n\n"
        f"Report:\n---\n{raw.raw_text}\n---\n\n"
        "Produce the structured analysis now."
    )
    response = client.chat.completions.parse(
        model=model or ANALYZE_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format=Analysis,
    )
    out = response.choices[0].message.parsed
    if out is None:
        raise RuntimeError("analyze: no parsed output from LLM")
    return out
