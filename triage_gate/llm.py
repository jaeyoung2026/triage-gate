"""LLM client factory and per-specialist model defaults.

Model sizing follows the source-role split from the design:
    intake, completeness  → small model (extraction / structural)
    severity, risk        → larger model (judgment / adversarial)

On import, loads `triage-gate/.env` if present (shell-set vars win). The parser
is intentionally tiny so the project has no dotenv dependency.

Override any model via env var. API key is read from OPENAI_API_KEY by the SDK.
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Shell-exported values always win over .env.
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(_PROJECT_ROOT / ".env")


@cache
def get_client():
    from openai import OpenAI

    return OpenAI()


INTAKE_MODEL = os.environ.get("TRIAGE_INTAKE_MODEL", "gpt-4o-mini")
SEVERITY_MODEL = os.environ.get("TRIAGE_SEVERITY_MODEL", "gpt-4o")
RISK_MODEL = os.environ.get("TRIAGE_RISK_MODEL", "gpt-4o")
COMPLETENESS_MODEL = os.environ.get("TRIAGE_COMPLETENESS_MODEL", "gpt-4o-mini")
