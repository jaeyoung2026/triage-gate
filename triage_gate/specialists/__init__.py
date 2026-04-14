"""Three LLM specialists running in parallel on one extracted report.

Each specialist has a narrow job and a tailored slice of product context.
Disagreement between specialists is itself a signal — synthesize.py measures it.
"""

from triage_gate.specialists.completeness import completeness_agent
from triage_gate.specialists.risk import risk_agent
from triage_gate.specialists.severity import severity_agent

__all__ = ["severity_agent", "risk_agent", "completeness_agent"]
