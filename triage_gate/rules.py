"""Keyword-based detectors — the deterministic floor under every LLM layer.

Rules run on raw text (robust to intake extraction failure) and on product-
context critical paths. If every LLM call goes down, rules still surface
high-risk signals. Rules never downgrade — only upgrade or flag.
"""

from __future__ import annotations

from typing import Iterable

from triage_gate.schema import CriticalPath, RiskFlag

# High-risk keyword dictionary. Keep short and explicit — false positives here
# only cause extra human review, false negatives can let danger bypass the gate.
RISK_KEYWORDS: dict[RiskFlag, list[str]] = {
    "auth": [
        "login", "sign in", "signin", "sign up", "signup",
        "password", "token", "session", "oauth", "sso",
        "로그인", "인증", "비밀번호",
    ],
    "payment": [
        "payment", "billing", "checkout", "subscription",
        "invoice", "charge", "refund",
        "결제", "청구", "환불", "구독",
    ],
    "data_loss": [
        "deleted", "missing data", "lost data", "disappeared",
        "rollback", "wiped", "gone",
        "삭제", "사라", "날아", "유실",
    ],
    "security": [
        "unauthorized", "access another user", "other user's",
        "permission denied", "exploit", "bypass",
        "권한", "보안 취약",
    ],
    "outage": [
        "down", "cannot load", "everything broken", "all users",
        "entire app", "complete outage",
        "장애", "먹통", "전체 안",
    ],
}

# Hints for intake_agent only. Not authoritative — intake makes the final call.
NON_BUG_HINTS: list[str] = [
    "feature request", "enhancement", "please add",
    "would be nice", "can you support", "support for",
    "기능 추가", "지원해주세요", "추가해주세요",
]


def detect_risk_flags_on_raw(text: str) -> list[RiskFlag]:
    """Scan raw text for danger keywords. Returns a deduped list."""
    lower = text.lower()
    hits: list[RiskFlag] = []
    for flag, kws in RISK_KEYWORDS.items():
        if any(kw.lower() in lower for kw in kws):
            hits.append(flag)
    return hits


def detect_critical_paths(
    text: str, paths: Iterable[CriticalPath]
) -> list[CriticalPath]:
    """Match product-specific critical paths by keyword. Product context enters here."""
    lower = text.lower()
    hits: list[CriticalPath] = []
    for path in paths:
        if any(kw.lower() in lower for kw in path.keywords):
            hits.append(path)
    return hits


def smells_like_non_bug(text: str) -> bool:
    """Weak hint for intake_agent only. Never routes on its own."""
    lower = text.lower()
    return any(kw.lower() in lower for kw in NON_BUG_HINTS)
