"""Payload-safe audit events for gated side effects."""

from __future__ import annotations

import re

from app.services.gated_actions import ActionKey, GateDecision
from app.utils.logging import get_logger

logger = get_logger(__name__)

_SAFE_TOKEN_PATTERN = re.compile(r"[^a-zA-Z0-9_.:-]+")
_LONG_DIGIT_PATTERN = re.compile(r"\d{7,}")


def _sanitize_token(value: str, *, max_length: int) -> str:
    token = str(value or "")[:max_length]
    token = _SAFE_TOKEN_PATTERN.sub("_", token).strip("_.:-")
    if _LONG_DIGIT_PATTERN.search(token):
        return ""
    return token[:max_length]


def _sanitize_source(source: str) -> str:
    first_token = str(source or "").strip().split(maxsplit=1)[0]
    sanitized = _sanitize_token(first_token.lower(), max_length=40)
    return sanitized or "unknown"


def record_gate_decision(
    *,
    action: ActionKey,
    contractor_id: str,
    source: str,
    resource_id: str = "",
    decision: GateDecision,
) -> None:
    """Log a gate decision without caller speech, message bodies, tokens, or payloads."""
    logger.info(
        "side_effect_gate_decision",
        extra={
            "action": action.value,
            "contractor_id": _sanitize_token(contractor_id, max_length=8),
            "source": _sanitize_source(source),
            "resource_id": _sanitize_token(resource_id, max_length=12),
            "allowed": decision.allowed,
            "reason": decision.reason.value,
        },
    )
