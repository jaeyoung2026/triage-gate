"""intake_agent — LLM layer that turns raw unstructured reports into ExtractedReport.

Structured output is enforced via pydantic. The model never sees a dict[str, ...]
because OpenAI strict mode requires all object keys to be predefined, so
FieldSourceMap makes the per-field provenance shape explicit.

Source-role split (ProductContext → intake sees):
    scope_summary + known_limitations + domain_glossary   ✓
    critical_paths, precedent_cases                        ✗
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from triage_gate.llm import INTAKE_MODEL, get_client
from triage_gate.schema import (
    ExtractedFields,
    ExtractedReport,
    FieldSource,
    IssueKind,
    ProductContext,
    RawReport,
)


class FieldSourceMap(BaseModel):
    """Typed per-field provenance. Strict structured outputs need every key upfront."""

    title: Optional[FieldSource]
    body: Optional[FieldSource]
    reproduction_steps: Optional[FieldSource]
    observed_result: Optional[FieldSource]
    expected_result: Optional[FieldSource]
    stack_trace: Optional[FieldSource]
    affected_area: Optional[FieldSource]
    frequency_hint: Optional[FieldSource]
    user_impact_hint: Optional[FieldSource]


class IntakeOutput(BaseModel):
    """LLM call output. Merged with RawReport to form ExtractedReport."""

    language: Optional[str]
    preliminary_issue_kind: IssueKind
    fields: ExtractedFields
    field_sources: FieldSourceMap
    intake_notes: list[str]


SYSTEM_PROMPT = """You are the intake analyst for a bug-report triage system.

Your job: read a raw, unstructured report (GitHub issue, email, chat, OCR,
or short message in any language) and produce a structured extraction.

ABSOLUTE RULES
1. Never fabricate. If a field is not stated or reasonably inferable from raw
   text, set status="missing" and leave the field null/empty.
2. For each populated field, record how you got it:
   - stated   = reporter wrote it explicitly
   - inferred = you derived it from context (include the exact quote you used)
   - missing  = field not present in the raw text
3. Preserve original language. Do not translate.
4. Never guess reproduction_steps or expected_result — these must be stated
   or left missing. They are load-bearing for downstream severity decisions.
5. Use product context (scope + known_limitations + glossary) to set
   preliminary_issue_kind. If raw text matches a known_limitation, the report
   is a support_question, not a bug.

PRELIMINARY ISSUE KIND
- bug               : real defect in the product
- feature_request   : request for a capability that does not exist
- support_question  : usage question, or matches a known_limitation
- duplicate         : obviously the same as a prior report (bias against this)
- insufficient_info : too little raw text to act on responsibly

FIELDS TO EXTRACT (leave any field missing if not present in raw text)
title, body, reproduction_steps, observed_result, expected_result,
stack_trace, affected_area, frequency_hint, user_impact_hint.
"""


def _build_context_block(ctx: ProductContext) -> str:
    lines = [f"Product: {ctx.product_name}", f"Scope: {ctx.scope_summary}"]
    if ctx.known_limitations:
        lines.append("")
        lines.append("Known limitations (these are NOT bugs):")
        for item in ctx.known_limitations:
            lines.append(f"  - {item}")
    if ctx.domain_glossary:
        lines.append("")
        lines.append("Glossary:")
        for term, defn in ctx.domain_glossary.items():
            lines.append(f"  - {term}: {defn}")
    return "\n".join(lines)


def intake(
    raw: RawReport,
    ctx: ProductContext,
    *,
    model: str | None = None,
) -> ExtractedReport:
    """Run intake_agent on a raw report. Returns a fully-formed ExtractedReport."""
    client = get_client()
    context_block = _build_context_block(ctx)
    user_msg = (
        f"{context_block}\n\n"
        f"Report:\n---\n{raw.raw_text}\n---\n\n"
        "Produce the structured extraction now."
    )

    response = client.chat.completions.parse(
        model=model or INTAKE_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format=IntakeOutput,
    )
    out = response.choices[0].message.parsed
    if out is None:
        raise RuntimeError("intake_agent: no parsed output from LLM")

    # The LLM sets preliminary_issue_kind at the top level; push it into
    # ExtractedFields so synthesizer fast-paths can read it uniformly.
    out.fields.preliminary_issue_kind = out.preliminary_issue_kind

    field_sources_dict: dict[str, FieldSource] = {}
    for name in FieldSourceMap.model_fields:
        value = getattr(out.field_sources, name)
        if value is not None:
            field_sources_dict[name] = value

    return ExtractedReport(
        report_id=raw.report_id,
        raw_text=raw.raw_text,
        source_kind=raw.source_kind,
        language=out.language or raw.language,
        fields=out.fields,
        field_sources=field_sources_dict,
        intake_notes=out.intake_notes,
    )
