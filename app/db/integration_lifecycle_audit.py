"""Durable integration lifecycle audit events and revocation outbox.

Records payload-safe audit records and revocation outbox records for integration
connect, reconnect, and disconnect lifecycle transitions directly into Firestore.
Contains zero credentials, tokens, ciphertexts, authorization codes, secrets,
or provider response payloads.
"""

from __future__ import annotations

import math
import time
from typing import Any, Optional

from app.services.integration_tokens import MAX_KEY_VERSION

AUDIT_COLLECTION = "integration_lifecycle_audit"
REVOCATION_OUTBOX_COLLECTION = "integration_revocation_outbox"

VALID_ACTIONS = frozenset({"connected", "reconnected", "credentials_deleted"})

# Canonical exact revocation status constants
REVOCATION_STATUS_REQUEST_STARTED = "provider_request_started"
REVOCATION_STATUS_CONFIRMED = "provider_confirmed"
REVOCATION_STATUS_REJECTED = "provider_rejected"
REVOCATION_STATUS_TRANSPORT_ERROR = "transport_error_unknown"
REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE = "not_attempted_unavailable_token"

ALL_REVOCATION_STATUSES = frozenset({
    REVOCATION_STATUS_REQUEST_STARTED,
    REVOCATION_STATUS_CONFIRMED,
    REVOCATION_STATUS_REJECTED,
    REVOCATION_STATUS_TRANSPORT_ERROR,
    REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE,
})

TERMINAL_REVOCATION_STATUSES = frozenset({
    REVOCATION_STATUS_CONFIRMED,
    REVOCATION_STATUS_REJECTED,
    REVOCATION_STATUS_TRANSPORT_ERROR,
    REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE,
})

# Canonical exact credential deletion dispositions for durable storage
DISPOSITION_EXECUTED = "executed"
DISPOSITION_PARTIAL_RECONCILED = "partial_reconciled"
DISPOSITION_LEGACY_RECONCILED = "legacy_reconciled"

VALID_DISPOSITIONS = frozenset({
    DISPOSITION_EXECUTED,
    DISPOSITION_PARTIAL_RECONCILED,
    DISPOSITION_LEGACY_RECONCILED,
})

# Backward-compatibility superset for older audit assertions
VALID_REVOCATION_STATUSES = frozenset({
    "pending",
    "succeeded",
    "provider_rejected",
    "transport_error",
    "not_attempted_unavailable_token",
    "revoked_provider_confirmed",
    "revocation_rejected_provider",
    "revocation_network_error",
    REVOCATION_STATUS_REQUEST_STARTED,
    REVOCATION_STATUS_CONFIRMED,
    REVOCATION_STATUS_TRANSPORT_ERROR,
})

# Closed sets for disconnect audit actor and reason contracts
VALID_DISCONNECT_ACTORS = frozenset({
    "contractor_api",
    "admin_api",
    "system_reconciliation",
})

VALID_DISCONNECT_REASONS = frozenset({
    "contractor_initiated_disconnect",
    "legacy_reconciliation",
    "admin_initiated_disconnect",
    "user_requested_disconnect",
})

EXPECTED_OUTBOX_KEYS = frozenset({
    "schema_version",
    "contractor_id",
    "provider",
    "generation",
    "lifecycle_epoch",
    "status",
    "claim_id",
    "audit_finalized",
    "audit_finalized_at",
    "created_at",
    "updated_at",
    "credential_deletion_disposition",
})

EXPECTED_DISCONNECT_AUDIT_KEYS = frozenset({
    "schema_version",
    "contractor_id",
    "provider",
    "generation",
    "lifecycle_epoch",
    "action",
    "actor",
    "reason",
    "credential_deletion_disposition",
    "revocation_status",
    "revocation_completed_at",
    "created_at",
    "timestamp",
})

FORBIDDEN_OUTBOX_KEYS = frozenset({
    "access_token",
    "refresh_token",
    "token",
    "client_secret",
    "secret",
    "ciphertext",
    "auth_code",
    "authorization_code",
    "code",
    "iv",
    "tag",
    "encrypted_token",
    "raw",
    "key_version",
    "scope",
    "google_calendar_scope",
    "customer_data",
})


def format_audit_doc_id(
    *,
    contractor_id: str,
    provider: str,
    generation: int,
    action: str = "credentials_deleted",
) -> str:
    """Deterministic document ID for an integration lifecycle audit event."""
    return f"{contractor_id}_{provider}_{generation}_{action}"


def format_outbox_doc_id(
    *,
    contractor_id: str,
    provider: str,
    generation: int,
) -> str:
    """Deterministic document ID for a revocation outbox record (matches credentials_deleted audit ID)."""
    return format_audit_doc_id(contractor_id=contractor_id, provider=provider, generation=generation, action="credentials_deleted")


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
    lifecycle_epoch: int = 1,
    actor: str = "contractor_api",
    reason: str = "contractor_initiated_disconnect",
    credential_deletion_disposition: str = DISPOSITION_EXECUTED,
    revocation_status: str = REVOCATION_STATUS_REQUEST_STARTED,
    revocation_completed_at: Optional[float] = None,
    timestamp: Optional[float] = None,
) -> dict[str, Any]:
    """Build a closed payload-safe audit record for a provider disconnect event."""
    now_ts = timestamp if timestamp is not None else time.time()
    return {
        "schema_version": 1,
        "contractor_id": contractor_id,
        "provider": provider,
        "generation": generation,
        "lifecycle_epoch": lifecycle_epoch,
        "action": "credentials_deleted",
        "actor": actor,
        "reason": reason,
        "credential_deletion_disposition": credential_deletion_disposition,
        "revocation_status": revocation_status,
        "revocation_completed_at": revocation_completed_at,
        "created_at": now_ts,
        "timestamp": now_ts,
    }


def build_disconnect_outbox_record(
    *,
    contractor_id: str,
    provider: str,
    generation: int,
    lifecycle_epoch: int,
    status: str = REVOCATION_STATUS_REQUEST_STARTED,
    claim_id: Optional[str] = None,
    audit_finalized: bool = False,
    audit_finalized_at: Optional[float] = None,
    created_at: float,
    updated_at: float,
    credential_deletion_disposition: str = DISPOSITION_EXECUTED,
) -> dict[str, Any]:
    """Build a closed payload-safe revocation outbox record."""
    return {
        "schema_version": 1,
        "contractor_id": contractor_id,
        "provider": provider,
        "generation": generation,
        "lifecycle_epoch": lifecycle_epoch,
        "status": status,
        "claim_id": claim_id,
        "audit_finalized": audit_finalized,
        "audit_finalized_at": audit_finalized_at,
        "created_at": created_at,
        "updated_at": updated_at,
        "credential_deletion_disposition": credential_deletion_disposition,
    }


def validate_outbox_record(
    data: Any,
    *,
    expected_contractor_id: Optional[str] = None,
    expected_provider: Optional[str] = None,
    expected_generation: Optional[int] = None,
    expected_lifecycle_epoch: Optional[int] = None,
    expected_outbox_id: Optional[str] = None,
) -> dict[str, Any]:
    """Strict hostile validation of a revocation outbox document."""
    if type(data) is not dict:
        raise ValueError("Outbox record is not an exact dict")
    for k in data.keys():
        if type(k) is not str:
            raise ValueError("Outbox record contains non-string key")
        if k in FORBIDDEN_OUTBOX_KEYS:
            raise ValueError("Outbox record contains forbidden secret key")

    actual_keys = frozenset(data.keys())
    if actual_keys != EXPECTED_OUTBOX_KEYS:
        missing = EXPECTED_OUTBOX_KEYS - actual_keys
        extra = actual_keys - EXPECTED_OUTBOX_KEYS
        raise ValueError("Outbox record key mismatch")

    # schema_version (exact int 1, not bool)
    schema_ver = data["schema_version"]
    if type(schema_ver) is not int or type(schema_ver) is bool or schema_ver != 1:
        raise ValueError("Invalid schema_version")

    # contractor_id
    cid = data["contractor_id"]
    if type(cid) is not str or len(cid) == 0:
        raise ValueError("contractor_id must be non-empty str")
    if expected_contractor_id is not None:
        if type(expected_contractor_id) is not str or len(expected_contractor_id) == 0:
            raise ValueError("expected_contractor_id must be non-empty str")
        if cid != expected_contractor_id:
            raise ValueError("contractor_id mismatch")

    # provider
    prov = data["provider"]
    if type(prov) is not str or prov not in {"jobber", "google_calendar"}:
        raise ValueError("Invalid provider")
    if expected_provider is not None:
        if type(expected_provider) is not str or expected_provider not in {"jobber", "google_calendar"}:
            raise ValueError("Invalid expected_provider")
        if prov != expected_provider:
            raise ValueError("provider mismatch")

    # generation (exact int, not bool, 0 <= gen <= MAX_KEY_VERSION)
    gen = data["generation"]
    if type(gen) is not int or type(gen) is bool or not (0 <= gen <= MAX_KEY_VERSION):
        raise ValueError("Invalid generation")
    if expected_generation is not None:
        if type(expected_generation) is not int or type(expected_generation) is bool or not (0 <= expected_generation <= MAX_KEY_VERSION):
            raise ValueError("Invalid expected_generation")
        if gen != expected_generation:
            raise ValueError("generation mismatch")

    computed_doc_id = format_outbox_doc_id(contractor_id=cid, provider=prov, generation=gen)
    if expected_outbox_id is not None:
        if type(expected_outbox_id) is not str or len(expected_outbox_id) == 0:
            raise ValueError("expected_outbox_id must be non-empty str")
        if computed_doc_id != expected_outbox_id:
            raise ValueError("outbox_id mismatch")

    # lifecycle_epoch (exact int, not bool, 0 <= epoch <= MAX_KEY_VERSION)
    epoch = data["lifecycle_epoch"]
    if type(epoch) is not int or type(epoch) is bool or not (0 <= epoch <= MAX_KEY_VERSION):
        raise ValueError("Invalid lifecycle_epoch")
    if expected_lifecycle_epoch is not None:
        if type(expected_lifecycle_epoch) is not int or type(expected_lifecycle_epoch) is bool or not (0 <= expected_lifecycle_epoch <= MAX_KEY_VERSION):
            raise ValueError("Invalid expected_lifecycle_epoch")
        if epoch != expected_lifecycle_epoch:
            raise ValueError("lifecycle_epoch mismatch")

    # status
    status = data["status"]
    if type(status) is not str or status not in ALL_REVOCATION_STATUSES:
        raise ValueError("Invalid revocation status")

    # credential_deletion_disposition
    disp = data["credential_deletion_disposition"]
    if type(disp) is not str or disp not in VALID_DISPOSITIONS:
        raise ValueError("Invalid credential_deletion_disposition")

    # claim_id & status-specific invariants
    claim_id = data["claim_id"]
    finalized = data["audit_finalized"]
    if type(finalized) is not bool:
        raise ValueError("audit_finalized must be exact bool")
    finalized_at = data["audit_finalized_at"]

    # created_at & updated_at
    created_at = data["created_at"]
    if type(created_at) is not float or not math.isfinite(created_at) or created_at <= 0.0:
        raise ValueError("created_at must be finite positive float")

    updated_at = data["updated_at"]
    if type(updated_at) is not float or not math.isfinite(updated_at) or updated_at <= 0.0:
        raise ValueError("updated_at must be finite positive float")
    if updated_at < created_at:
        raise ValueError("updated_at cannot be before created_at")

    if status == REVOCATION_STATUS_REQUEST_STARTED:
        if type(claim_id) is not str or len(claim_id) == 0:
            raise ValueError("claim_id must be non-empty str when status is provider_request_started")
        if finalized is not False:
            raise ValueError("audit_finalized must be False when status is provider_request_started")
        if finalized_at is not None:
            raise ValueError("audit_finalized_at must be None when not finalized")
        if updated_at != created_at:
            raise ValueError("updated_at must exactly equal created_at when status is provider_request_started")
    elif status == REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE:
        if claim_id is not None:
            raise ValueError("claim_id must be None when status is not_attempted_unavailable_token")
        if finalized is True:
            if type(finalized_at) is not float or not math.isfinite(finalized_at) or finalized_at <= 0.0:
                raise ValueError("audit_finalized_at must be finite positive float when finalized")
            if finalized_at < updated_at:
                raise ValueError("audit_finalized_at cannot be before updated_at")
        else:
            if finalized_at is not None:
                raise ValueError("audit_finalized_at must be None when not finalized")
    else:
        # provider_confirmed, provider_rejected, transport_error_unknown
        if type(claim_id) is not str or len(claim_id) == 0:
            raise ValueError("claim_id must be non-empty str for status")
        if finalized is True:
            if type(finalized_at) is not float or not math.isfinite(finalized_at) or finalized_at <= 0.0:
                raise ValueError("audit_finalized_at must be finite positive float when finalized")
            if finalized_at < updated_at:
                raise ValueError("audit_finalized_at cannot be before updated_at")
        else:
            if finalized_at is not None:
                raise ValueError("audit_finalized_at must be None when not finalized")

    return dict(data)


def validate_disconnect_audit_record(
    data: Any,
    *,
    expected_contractor_id: Optional[str] = None,
    expected_provider: Optional[str] = None,
    expected_generation: Optional[int] = None,
    expected_lifecycle_epoch: Optional[int] = None,
    expected_audit_id: Optional[str] = None,
) -> dict[str, Any]:
    """Strict hostile validation of a closed disconnect lifecycle audit record."""
    if type(data) is not dict:
        raise ValueError("Audit record is not an exact dict")
    for k in data.keys():
        if type(k) is not str:
            raise ValueError("Audit record contains non-string key")
        if k in FORBIDDEN_OUTBOX_KEYS:
            raise ValueError("Audit record contains forbidden secret key")

    actual_keys = frozenset(data.keys())
    if actual_keys != EXPECTED_DISCONNECT_AUDIT_KEYS:
        missing = EXPECTED_DISCONNECT_AUDIT_KEYS - actual_keys
        extra = actual_keys - EXPECTED_DISCONNECT_AUDIT_KEYS
        raise ValueError("Disconnect audit record key mismatch")

    # schema_version
    schema_ver = data["schema_version"]
    if type(schema_ver) is not int or type(schema_ver) is bool or schema_ver != 1:
        raise ValueError("Invalid schema_version")

    # contractor_id
    cid = data["contractor_id"]
    if type(cid) is not str or len(cid) == 0:
        raise ValueError("contractor_id must be non-empty str")
    if expected_contractor_id is not None:
        if type(expected_contractor_id) is not str or len(expected_contractor_id) == 0:
            raise ValueError("expected_contractor_id must be non-empty str")
        if cid != expected_contractor_id:
            raise ValueError("contractor_id mismatch")

    # provider
    prov = data["provider"]
    if type(prov) is not str or prov not in {"jobber", "google_calendar"}:
        raise ValueError("Invalid provider")
    if expected_provider is not None:
        if type(expected_provider) is not str or expected_provider not in {"jobber", "google_calendar"}:
            raise ValueError("Invalid expected_provider")
        if prov != expected_provider:
            raise ValueError("provider mismatch")

    # generation (exact int, not bool, 0 <= gen <= MAX_KEY_VERSION)
    gen = data["generation"]
    if type(gen) is not int or type(gen) is bool or not (0 <= gen <= MAX_KEY_VERSION):
        raise ValueError("Invalid generation")
    if expected_generation is not None:
        if type(expected_generation) is not int or type(expected_generation) is bool or not (0 <= expected_generation <= MAX_KEY_VERSION):
            raise ValueError("Invalid expected_generation")
        if gen != expected_generation:
            raise ValueError("generation mismatch")

    computed_doc_id = format_audit_doc_id(contractor_id=cid, provider=prov, generation=gen, action="credentials_deleted")
    if expected_audit_id is not None:
        if type(expected_audit_id) is not str or len(expected_audit_id) == 0:
            raise ValueError("expected_audit_id must be non-empty str")
        if computed_doc_id != expected_audit_id:
            raise ValueError("audit_id mismatch")

    # lifecycle_epoch (exact int, not bool, 0 <= epoch <= MAX_KEY_VERSION)
    epoch = data["lifecycle_epoch"]
    if type(epoch) is not int or type(epoch) is bool or not (0 <= epoch <= MAX_KEY_VERSION):
        raise ValueError("Invalid lifecycle_epoch")
    if expected_lifecycle_epoch is not None:
        if type(expected_lifecycle_epoch) is not int or type(expected_lifecycle_epoch) is bool or not (0 <= expected_lifecycle_epoch <= MAX_KEY_VERSION):
            raise ValueError("Invalid expected_lifecycle_epoch")
        if epoch != expected_lifecycle_epoch:
            raise ValueError("lifecycle_epoch mismatch")

    # action
    action = data["action"]
    if type(action) is not str or action != "credentials_deleted":
        raise ValueError("Invalid action in disconnect audit record")

    # actor & reason (exact closed contracts)
    actor = data["actor"]
    if type(actor) is not str or actor not in VALID_DISCONNECT_ACTORS:
        raise ValueError("Invalid actor in disconnect audit record")
    reason = data["reason"]
    if type(reason) is not str or reason not in VALID_DISCONNECT_REASONS:
        raise ValueError("Invalid reason in disconnect audit record")

    # credential_deletion_disposition
    disp = data["credential_deletion_disposition"]
    if type(disp) is not str or disp not in VALID_DISPOSITIONS:
        raise ValueError("Invalid credential_deletion_disposition in audit record")

    # revocation_status
    rev_status = data["revocation_status"]
    if type(rev_status) is not str or rev_status not in ALL_REVOCATION_STATUSES:
        raise ValueError("Invalid revocation_status in audit record")

    # created_at & timestamp (exact equality)
    created_at = data["created_at"]
    if type(created_at) is not float or not math.isfinite(created_at) or created_at <= 0.0:
        raise ValueError("created_at must be finite positive float in audit record")

    ts = data["timestamp"]
    if type(ts) is not float or not math.isfinite(ts) or ts <= 0.0:
        raise ValueError("timestamp must be finite positive float in audit record")
    if ts != created_at:
        raise ValueError("timestamp must exactly equal created_at in audit record")

    # revocation_completed_at
    rev_ts = data["revocation_completed_at"]
    if rev_status == REVOCATION_STATUS_REQUEST_STARTED:
        if rev_ts is not None:
            raise ValueError("revocation_completed_at must be None when revocation_status is provider_request_started")
    else:
        if rev_ts is None:
            raise ValueError("revocation_completed_at cannot be None when revocation_status is terminal")
        if type(rev_ts) is not float or not math.isfinite(rev_ts) or rev_ts <= 0.0:
            raise ValueError("revocation_completed_at must be finite positive float")
        if rev_ts < created_at:
            raise ValueError("revocation_completed_at cannot be before created_at")

    return dict(data)


def validate_disconnect_lifecycle_pair(
    audit_data: Any,
    outbox_data: Any,
    *,
    expected_contractor_id: Optional[str] = None,
    expected_provider: Optional[str] = None,
    expected_generation: Optional[int] = None,
    expected_lifecycle_epoch: Optional[int] = None,
    expected_audit_id: Optional[str] = None,
    expected_outbox_id: Optional[str] = None,
    expected_doc_id: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pure exact audit/outbox PAIR validator.

    Proves that a disconnect audit event and a revocation outbox record are
    mutually coherent, share identical context/timestamps/dispositions, and
    represent a valid atomic lifecycle state.
    """
    exp_audit = expected_audit_id
    exp_outbox = expected_outbox_id if expected_outbox_id is not None else expected_doc_id
    valid_audit = validate_disconnect_audit_record(
        audit_data,
        expected_contractor_id=expected_contractor_id,
        expected_provider=expected_provider,
        expected_generation=expected_generation,
        expected_lifecycle_epoch=expected_lifecycle_epoch,
        expected_audit_id=exp_audit,
    )
    valid_outbox = validate_outbox_record(
        outbox_data,
        expected_contractor_id=expected_contractor_id,
        expected_provider=expected_provider,
        expected_generation=expected_generation,
        expected_lifecycle_epoch=expected_lifecycle_epoch,
        expected_outbox_id=exp_outbox,
    )

    # 1. Exact context match
    if valid_audit["contractor_id"] != valid_outbox["contractor_id"]:
        raise ValueError("Contractor ID mismatch between audit and outbox")
    if valid_audit["provider"] != valid_outbox["provider"]:
        raise ValueError("Provider mismatch between audit and outbox")
    if valid_audit["generation"] != valid_outbox["generation"]:
        raise ValueError("Generation mismatch between audit and outbox")
    if valid_audit["lifecycle_epoch"] != valid_outbox["lifecycle_epoch"]:
        raise ValueError("Lifecycle epoch mismatch between audit and outbox")
    if valid_audit["credential_deletion_disposition"] != valid_outbox["credential_deletion_disposition"]:
        raise ValueError("Disposition mismatch between audit and outbox")
    if valid_audit["created_at"] != valid_outbox["created_at"]:
        raise ValueError("created_at mismatch between audit and outbox")
    if valid_audit["schema_version"] != valid_outbox["schema_version"]:
        raise ValueError("schema_version mismatch between audit and outbox")

    # 2. State & finalization coherence
    outbox_status = valid_outbox["status"]
    outbox_finalized = valid_outbox["audit_finalized"]
    audit_status = valid_audit["revocation_status"]
    audit_completed_at = valid_audit["revocation_completed_at"]

    if outbox_status == REVOCATION_STATUS_REQUEST_STARTED:
        if outbox_finalized is not False:
            raise ValueError("Outbox with status provider_request_started must have audit_finalized=False")
        if audit_status != REVOCATION_STATUS_REQUEST_STARTED:
            raise ValueError("Audit status must be provider_request_started when outbox is started")
        if audit_completed_at is not None:
            raise ValueError("Audit revocation_completed_at must be None when outbox is started")
    elif outbox_status in TERMINAL_REVOCATION_STATUSES:
        if outbox_finalized is False:
            # Atomic finalizer has not run yet: audit must still be in started state
            if audit_status != REVOCATION_STATUS_REQUEST_STARTED:
                raise ValueError("Unfinalized outbox with terminal status must pair with started audit")
            if audit_completed_at is not None:
                raise ValueError("Unfinalized audit revocation_completed_at must be None")
        else:
            # Atomic finalizer has run: audit must match terminal outbox status and exact outbox.updated_at completion time
            if audit_status != outbox_status:
                raise ValueError("Finalized audit status does not match outbox status")
            if audit_completed_at != valid_outbox["updated_at"]:
                raise ValueError("Finalized audit revocation_completed_at must match outbox updated_at")
            if valid_outbox["audit_finalized_at"] is None or valid_outbox["audit_finalized_at"] < valid_outbox["updated_at"]:
                raise ValueError("Outbox audit_finalized_at cannot be before updated_at")
    else:
        raise ValueError("Invalid outbox status in pair")

    return valid_audit, valid_outbox


def validate_lifecycle_audit_record(
    data: Any,
    *,
    expected_contractor_id: Optional[str] = None,
    expected_provider: Optional[str] = None,
    expected_generation: Optional[int] = None,
    expected_lifecycle_epoch: Optional[int] = None,
) -> dict[str, Any]:
    """Hostile validation of an integration lifecycle audit record (generic for connect/disconnect)."""
    if type(data) is not dict:
        raise ValueError("Audit record is not an exact dict")
    for k in data.keys():
        if type(k) is not str:
            raise ValueError("Audit record contains non-string key")
        if k in FORBIDDEN_OUTBOX_KEYS:
            raise ValueError("Audit record contains forbidden secret key")

    # contractor_id
    cid = data.get("contractor_id")
    if type(cid) is not str or len(cid) == 0:
        raise ValueError("contractor_id must be non-empty str in audit record")
    if expected_contractor_id is not None and cid != expected_contractor_id:
        raise ValueError("contractor_id mismatch")

    # provider
    prov = data.get("provider")
    if type(prov) is not str or prov not in {"jobber", "google_calendar"}:
        raise ValueError("Invalid provider in audit record")
    if expected_provider is not None and prov != expected_provider:
        raise ValueError("provider mismatch")

    # generation
    gen = data.get("generation")
    if type(gen) is not int or type(gen) is bool or gen < 0:
        raise ValueError("Invalid generation in audit record")
    if expected_generation is not None and gen != expected_generation:
        raise ValueError("generation mismatch")

    # lifecycle_epoch if present
    epoch = data.get("lifecycle_epoch")
    if epoch is not None:
        if type(epoch) is not int or type(epoch) is bool or epoch < 0:
            raise ValueError("Invalid lifecycle_epoch in audit record")
        if expected_lifecycle_epoch is not None and epoch != expected_lifecycle_epoch:
            raise ValueError("lifecycle_epoch mismatch")

    # action
    action = data.get("action")
    if type(action) is not str or action not in VALID_ACTIONS:
        raise ValueError("Invalid action in audit record")

    # revocation_status if present
    rev_status = data.get("revocation_status")
    if rev_status is not None:
        if type(rev_status) is not str or rev_status not in VALID_REVOCATION_STATUSES:
            raise ValueError("Invalid revocation_status in audit record")

    # revocation_completed_at if present
    rev_ts = data.get("revocation_completed_at")
    if rev_ts is not None:
        if type(rev_ts) is not float or not math.isfinite(rev_ts) or rev_ts <= 0.0:
            raise ValueError("Invalid revocation_completed_at in audit record")

    # created_at
    created_at = data.get("created_at")
    if type(created_at) is not float or not math.isfinite(created_at) or created_at <= 0.0:
        raise ValueError("Invalid created_at in audit record")

    return data
