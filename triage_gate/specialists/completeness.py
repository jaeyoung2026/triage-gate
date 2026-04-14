"""completeness_agent — judges whether there's enough info to act.

Sees:      extracted (fields + field_sources) + ProductContext.scope_summary
Does not:  raw text primarily (uses extracted), other ProductContext fields

Special behavior: treats "inferred" fields as weaker than "stated". An inferred
value for reproduction_steps or expected_result may still count as missing
when rigor is required.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from triage_gate.llm import COMPLETENESS_MODEL, get_client
from triage_gate.schema import (
    CompletenessOpinion,
    ExtractedReport,
    ProductContext,
)


class CompletenessOutput(BaseModel):
    """LLM-facing output. No validation constraints."""

    missing_fields: list[str]
    inferred_fields: list[str]
    info_sufficiency: Literal["low", "medium", "high"]
    rationale: list[str]
    confidence: float


SYSTEM_PROMPT = """You are the completeness specialist in a bug-triage panel.

Your ONLY job: judge whether the report has enough information to act on.
You do NOT decide severity or route. You only rate info_sufficiency.

Load-bearing fields (matter most for acting on a bug):
- reproduction_steps
- observed_result
- expected_result

If any of these three is missing or only inferred, info_sufficiency drops.

Sufficiency levels:
- high   : all three load-bearing fields are STATED; anyone could act on this
- medium : some stated, some missing or inferred
- low    : most load-bearing fields missing; downstream cannot act safely

Rules:
- Be strict about reproduction_steps and expected_result. An "inferred" status
  there should usually count as missing, not as present.
- List missing_fields and inferred_fields separately, using the same field
  names that appear in the extracted report's field_sources.
- A title alone is not enough for high sufficiency.
- Write 2-3 short rationale sentences citing which fields are problematic.
- confidence is 0..1 — how sure you are in the sufficiency call.
"""


def _field_sources_block(extracted: ExtractedReport) -> str:
    if not extracted.field_sources:
        return "(no field_sources recorded)"
    lines = []
    for name, src in extracted.field_sources.items():
        quote = f' — "{src.quote[:60]}"' if src.quote else ""
        lines.append(f"  - {name}: {src.status} (conf={src.confidence}){quote}")
    return "\n".join(lines)


def completeness_agent(
    extracted: ExtractedReport,
    ctx: ProductContext,
    *,
    model: str | None = None,
) -> CompletenessOpinion:
    client = get_client()
    sources_block = _field_sources_block(extracted)
    body_preview = (extracted.fields.body or "")[:200]

    user_msg = f"""Product scope: {ctx.scope_summary}

Field provenance (status per field):
{sources_block}

Extracted values:
  title:              {extracted.fields.title}
  body (preview):     {body_preview}
  observed_result:    {extracted.fields.observed_result}
  expected_result:    {extracted.fields.expected_result}
  reproduction_steps: {extracted.fields.reproduction_steps}
  stack_trace:        {'(present)' if extracted.fields.stack_trace else '(absent)'}

Call completeness now."""

    response = client.chat.completions.parse(
        model=model or COMPLETENESS_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format=CompletenessOutput,
    )
    out = response.choices[0].message.parsed
    if out is None:
        raise RuntimeError("completeness_agent: no parsed output")

    rationale = out.rationale[:4] if out.rationale else ["(no rationale given)"]
    return CompletenessOpinion(
        missing_fields=out.missing_fields,
        inferred_fields=out.inferred_fields,
        info_sufficiency=out.info_sufficiency,
        rationale=rationale,
        confidence=max(0.0, min(1.0, out.confidence)),
    )
