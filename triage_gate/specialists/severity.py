"""severity_agent — judges user and service impact magnitude.

Sees:      extracted + ProductContext.scope_summary + ProductContext.critical_paths
Does not:  known_limitations, domain_glossary, precedent_cases

Does NOT decide issue_kind or route. Only severity + impact summary.
"""

from __future__ import annotations

from pydantic import BaseModel

from triage_gate.llm import SEVERITY_MODEL, get_client
from triage_gate.schema import (
    CriticalPath,
    ExtractedReport,
    ProductContext,
    Severity,
    SeverityOpinion,
)


class SeverityOutput(BaseModel):
    """LLM-facing output model. No validation constraints (strict mode friendly)."""

    severity: Severity
    impact_summary: str
    rationale: list[str]
    confidence: float


SYSTEM_PROMPT = """You are the severity specialist in a bug-triage panel.

Your ONLY job is to judge the user and service impact of this report.
You do NOT decide whether it is a bug at all — assume the intake layer already
filtered non-bugs. You do NOT decide the route — that is the synthesizer's job.

Severity levels:
- S0       : service-wide outage, data loss, security breach, payment or auth
             total failure, or many users immediately harmed
- S1       : core user flow blocked, many users affected, no workaround,
             revenue or activation path cut
- S2       : real functional defect but scoped to one area, partial impact or
             workaround exists
- S3       : cosmetic, low-impact edge case, minor UI glitch
- unknown  : cannot judge from the info given

Rules:
- Be CONSERVATIVE on S0 and S1. False negatives there are the worst outcome.
- If the report touches a critical path listed below, respect its severity
  floor. The synthesizer enforces floors later, but you should already honor
  them so agreement stays high.
- Write 2-3 short rationale sentences that cite specific facts, not feelings.
- confidence is 0..1 — how sure you are in THIS severity call specifically.
"""


def _critical_paths_block(paths: list[CriticalPath]) -> str:
    if not paths:
        return "(no critical paths defined)"
    lines = []
    for p in paths:
        lines.append(
            f"  - {p.name} (floor={p.default_severity_floor}): {p.description}"
        )
    return "\n".join(lines)


def severity_agent(
    extracted: ExtractedReport,
    ctx: ProductContext,
    *,
    model: str | None = None,
) -> SeverityOpinion:
    client = get_client()
    paths = _critical_paths_block(ctx.critical_paths)

    user_msg = f"""Product: {ctx.product_name}
Scope: {ctx.scope_summary}

Critical paths (respect these floors):
{paths}

Extracted report:
  title:              {extracted.fields.title}
  observed_result:    {extracted.fields.observed_result}
  expected_result:    {extracted.fields.expected_result}
  reproduction_steps: {extracted.fields.reproduction_steps}
  affected_area:      {extracted.fields.affected_area}
  frequency_hint:     {extracted.fields.frequency_hint}
  user_impact_hint:   {extracted.fields.user_impact_hint}

Raw text:
---
{extracted.raw_text}
---

Call severity now."""

    response = client.chat.completions.parse(
        model=model or SEVERITY_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format=SeverityOutput,
    )
    out = response.choices[0].message.parsed
    if out is None:
        raise RuntimeError("severity_agent: no parsed output")

    rationale = out.rationale[:4] if out.rationale else ["(no rationale given)"]
    return SeverityOpinion(
        severity=out.severity,
        impact_summary=out.impact_summary,
        rationale=rationale,
        confidence=max(0.0, min(1.0, out.confidence)),
    )
