"""risk_agent — adversarial scan for danger flags.

Sees:      extracted + ProductContext.critical_paths(risk_flags) + known_limitations
Does not:  scope_summary, domain_glossary, precedent_cases

Bias: pessimistic. When in doubt, raise the flag. False positives here cause
extra human review; false negatives let danger bypass the gate.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from triage_gate.llm import RISK_MODEL, get_client
from triage_gate.schema import (
    ExtractedReport,
    ProductContext,
    RiskFlag,
    RiskOpinion,
)


class RiskOutput(BaseModel):
    """LLM-facing output. No validation constraints."""

    risk_flags: list[RiskFlag]
    escalation_reason: Optional[str]
    rationale: list[str]
    confidence: float


SYSTEM_PROMPT = """You are the risk specialist in a bug-triage panel.

Your ONLY job: scan the report adversarially for danger signals. You do NOT
decide severity or route. You only surface risk_flags and the reason to
escalate if any danger is present.

Risk flags and what they mean:
- auth               : authentication, session, or login failure
- payment            : billing, checkout, invoicing, subscription
- data_loss          : data missing, deleted, corrupted, or unrecoverable
- security           : unauthorized access, permission bypass, cross-user leak
- outage             : multi-user or system-wide unavailability
- backend_validation : server rejects what client thought was valid
- insufficient_repro : cannot verify the claim without more info
- unclear_scope      : affected surface is not clear from the report

The FIRST FIVE (auth, payment, data_loss, security, outage) are DANGER FLAGS.
They block auto_fix downstream. They are the most load-bearing outputs you
produce — bias toward flagging them when there is reasonable evidence.

Rules:
- If the report matches a known_limitation listed below, it is NOT a bug and
  most risk flags should NOT fire.
- Write 2-3 short rationale sentences citing specific phrasing from the report.
- confidence is 0..1 — how sure you are in the flags you raised.
"""


def _known_limitations_block(items: list[str]) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"  - {item}" for item in items)


def _critical_path_hints_block(ctx: ProductContext) -> str:
    lines = []
    for p in ctx.critical_paths:
        if p.default_risk_flags:
            lines.append(
                f"  - {p.name} → typical flags: {p.default_risk_flags}"
            )
    return "\n".join(lines) if lines else "(none)"


def risk_agent(
    extracted: ExtractedReport,
    ctx: ProductContext,
    *,
    model: str | None = None,
) -> RiskOpinion:
    client = get_client()
    limitations = _known_limitations_block(ctx.known_limitations)
    path_hints = _critical_path_hints_block(ctx)

    user_msg = f"""Known limitations (NOT bugs — do not flag these):
{limitations}

Product-specific critical path hints:
{path_hints}

Extracted report:
  title:              {extracted.fields.title}
  observed_result:    {extracted.fields.observed_result}
  affected_area:      {extracted.fields.affected_area}
  user_impact_hint:   {extracted.fields.user_impact_hint}

Raw text:
---
{extracted.raw_text}
---

Call risk flags now."""

    response = client.chat.completions.parse(
        model=model or RISK_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format=RiskOutput,
    )
    out = response.choices[0].message.parsed
    if out is None:
        raise RuntimeError("risk_agent: no parsed output")

    rationale = out.rationale[:4] if out.rationale else ["(no rationale given)"]
    return RiskOpinion(
        risk_flags=out.risk_flags,
        escalation_reason=out.escalation_reason,
        rationale=rationale,
        confidence=max(0.0, min(1.0, out.confidence)),
    )
