"""Payload-safe audit events for gated side effects."""

from __future__ import annotations

from app.services.gated_actions import ActionKey, GateDecision
from app.utils.logging import get_logger

logger = get_logger(__name__)


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
            "contractor_id": contractor_id[:8] if contractor_id else "",
            "source": source,
            "resource_id": resource_id[:12] if resource_id else "",
            "allowed": decision.allowed,
            "reason": decision.reason.value,
        },
    )
