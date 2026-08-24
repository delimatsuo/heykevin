"""Durable integration lifecycle audit events.

Records payload-safe audit records for integration connect, reconnect, and disconnect lifecycle transitions
directly into Firestore. Contains zero credentials, tokens, ciphertexts, authorization codes,
or provider response payloads.
"""

from __future__ import annotations

import time
from typing import Any, Optional

AUDIT_COLLECTION = "integration_lifecycle_audit"

VALID_ACTIONS = frozenset({"connected", "reconnected", "credentials_deleted"})
VALID_REVOCATION_STATUSES = frozenset({
    "pending",
    "succeeded",
    "provider_rejected",
    "transport_error",
    "not_attempted_unavailable_token",
    "revoked_provider_confirmed",
    "revocation_rejected_provider",
    "revocation_network_error",
})


def format_audit_doc_id(
    contractor_id: str,
    provider: str,
    generation: int,
    action: str,
) -> str:
    """Deterministic document ID for an integration lifecycle audit event."""
    return f"{contractor_id}_{provider}_{generation}_{action}"


def build_connect_audit_event(
    *,
    contractor_id: str,
    provider: str,
    generation: int,
    actor: str = "oauth_state",
    action: str = "connected",
    timestamp: Optional[float] = None,
) -> dict[str, Any]:
    """Build a payload-safe audit payload for a provider connect/reconnect event."""
    return {
        "contractor_id": contractor_id,
        "provider": provider,
        "action": action,
        "generation": generation,
        "actor": actor,
        "created_at": timestamp if timestamp is not None else time.time(),
    }


def build_disconnect_audit_event(
    *,
    contractor_id: str,
    provider: str,
    generation: int,
    actor: str = "contractor_api",
    reason: Optional[str] = None,
    revocation_status: str = "pending",
    timestamp: Optional[float] = None,
) -> dict[str, Any]:
    """Build a payload-safe audit payload for a provider disconnect/deletion event."""
    event: dict[str, Any] = {
        "contractor_id": contractor_id,
        "provider": provider,
        "action": "credentials_deleted",
        "generation": generation,
        "actor": actor,
        "created_at": timestamp if timestamp is not None else time.time(),
        "revocation_status": revocation_status,
    }
    if reason is not None:
        event["reason"] = reason
    return event
