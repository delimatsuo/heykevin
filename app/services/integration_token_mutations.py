"""Durable, transactional CAS mutations and multi-instance lease coordination for integration tokens.

Mutation / Writer Architecture:
- Atomic CAS token persistence, connect, disconnect, and OAuth state consumption
- Multi-instance cross-process refresh lease claims before provider HTTP calls
- Strict Firestore 2.21 transaction semantics (doc_ref.get(transaction=transaction) only)
- Post-transaction durable-read verification with exact envelope, timestamp, and flag comparison
- Atomic integration lifecycle audit trail writes without token secrets
"""

from __future__ import annotations

import asyncio
import math
import re
import secrets
import time
from typing import Any, Optional

from fastapi import HTTPException
from google.cloud.firestore import transactional
from google.cloud.firestore_v1 import DELETE_FIELD

from app.db.firestore_client import get_firestore_client
from app.db.integration_lifecycle_audit import (
    AUDIT_COLLECTION,
    build_connect_audit_event,
    build_disconnect_audit_event,
    format_audit_doc_id,
)
from app.services.integration_tokens import (
    MAX_KEY_VERSION,
    VALID_PROVIDERS,
    IntegrationTokenCASConflict,
    IntegrationTokenConfigError,
    IntegrationTokenDecryptionError,
    IntegrationTokenEnvelopeError,
    IntegrationTokenError,
    _exact_raw_credential_equal,
    determine_write_format,
    encrypt_integration_token,
    safe_decrypt_integration_token,
    validate_token_expires_at,
    validate_token_expires_in,
    validate_token_string,
)

VALID_OAUTH_COLLECTIONS = frozenset({
    "jobber_oauth_states",
    "google_oauth_states",
    "google_calendar_oauth_states",
})

ALLOWED_EXTRA_UPDATES = {
    "jobber": frozenset({"jobber_lead_capture_enabled"}),
    "google_calendar": frozenset({"google_calendar_scope"}),
}

_CANONICAL_STATE_REGEX = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
LEASE_DURATION_SECONDS = 60.0


class IntegrationTokenLeaseError(IntegrationTokenError):
    """Raised when a concurrent worker actively holds the refresh lease for a contractor."""


class IntegrationTokenPostconditionError(IntegrationTokenEnvelopeError):
    """Raised when post-transaction durable-read verification fails against expected state."""


def _get_doc_snapshot_in_txn(doc_ref: Any, transaction: Any) -> Any:
    """Read a DocumentSnapshot strictly through doc_ref.get(transaction=transaction).

    Per Firestore 2.21+ API contract:
    - Rejects generator objects and non-snapshot returns
    - Fails closed if doc_ref.get does not return a DocumentSnapshot
    """
    doc_snap = doc_ref.get(transaction=transaction)
    if hasattr(doc_snap, "__next__") or hasattr(doc_snap, "__iter__") and not hasattr(doc_snap, "to_dict"):
        raise IntegrationTokenEnvelopeError("doc_ref.get returned a generator instead of DocumentSnapshot")

    return doc_snap


def _exact_scalar_or_composite_equal(actual: Any, expected: Any) -> bool:
    """Strict recursive equality distinguishing bool/int/float, finite numbers, and exact containers."""
    if type(actual) is not type(expected):
        return False
    if type(actual) is dict:
        if type(expected) is not dict:
            return False
        for k in actual.keys():
            if type(k) is not str:
                return False
        for k in expected.keys():
            if type(k) is not str:
                return False
        if set(actual.keys()) != set(expected.keys()):
            return False
        for k in expected:
            if not _exact_scalar_or_composite_equal(actual[k], expected[k]):
                return False
        return True
    if type(actual) in (list, tuple):
        if type(actual) is not type(expected) or len(actual) != len(expected):
            return False
        for a_elem, e_elem in zip(actual, expected):
            if not _exact_scalar_or_composite_equal(a_elem, e_elem):
                return False
        return True
    if isinstance(actual, float):
        if not math.isfinite(actual) or not math.isfinite(expected):
            return False
        return actual == expected
    if isinstance(actual, int):
        return actual == expected
    if isinstance(actual, bool):
        return actual is expected
    return actual == expected


def _verify_mutation_postcondition(
    doc_ref: Any,
    *,
    expected_generation: int,
    expected_connected: bool,
    provider: str,
    expected_access_envelope: Optional[dict[str, Any]] = None,
    expected_refresh_envelope: Optional[dict[str, Any]] = None,
    expected_token_refreshed_at: Optional[float] = None,
    expected_connected_at: Optional[float] = None,
    expected_disconnected_at: Optional[float] = None,
    expected_expires_at: Optional[float] = None,
    expected_extra_fields: Optional[dict[str, Any]] = None,
    expected_envelope_required: Optional[bool] = None,
    deleted_fields: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Execute an independent durable read and verify complete closed exact-key, value, and type postconditions."""
    doc_snap = doc_ref.get()
    if not getattr(doc_snap, "exists", False):
        raise IntegrationTokenPostconditionError("Document does not exist after transaction")

    data = doc_snap.to_dict()
    if type(data) is not dict:
        raise IntegrationTokenPostconditionError("Durable document snapshot is not an exact dict")

    # Verify generation (exact int, not bool)
    actual_gen = data.get(f"{provider}_generation")
    if type(actual_gen) is not int or type(actual_gen) is bool or actual_gen != expected_generation:
        raise IntegrationTokenPostconditionError("Postcondition generation mismatch")

    # Verify connected flag (exact bool)
    actual_connected = data.get(f"{provider}_connected")
    if type(actual_connected) is not bool or actual_connected is not expected_connected:
        raise IntegrationTokenPostconditionError("Postcondition connected flag mismatch")

    # Verify envelope required floor
    if expected_envelope_required is not None:
        actual_req = data.get(f"{provider}_token_envelope_required")
        if actual_req is not expected_envelope_required:
            raise IntegrationTokenPostconditionError("Postcondition token_envelope_required mismatch")

    # Verify deleted fields are truly ABSENT (not None, not DELETE_FIELD)
    if deleted_fields:
        for field in deleted_fields:
            if field in data:
                raise IntegrationTokenPostconditionError(f"Deleted field {field} remains present in document")

    # Verify exact encrypted access envelope
    if expected_access_envelope is not None:
        actual_access = data.get(f"{provider}_access_token")
        if not _exact_raw_credential_equal(actual_access, expected_access_envelope):
            raise IntegrationTokenPostconditionError("Postcondition access token envelope mismatch")

    # Verify exact encrypted refresh envelope
    if expected_refresh_envelope is not None:
        actual_refresh = data.get(f"{provider}_refresh_token")
        if not _exact_raw_credential_equal(actual_refresh, expected_refresh_envelope):
            raise IntegrationTokenPostconditionError("Postcondition refresh token envelope mismatch")

    # Verify exact timestamps (exact float/int, not bool, finite)
    if expected_token_refreshed_at is not None:
        actual_ts = data.get(f"{provider}_token_refreshed_at")
        if (
            type(actual_ts) not in (int, float)
            or type(actual_ts) is bool
            or not math.isfinite(actual_ts)
            or actual_ts != expected_token_refreshed_at
        ):
            raise IntegrationTokenPostconditionError("Postcondition token_refreshed_at timestamp mismatch")

    if expected_connected_at is not None:
        actual_ts = data.get(f"{provider}_connected_at")
        if (
            type(actual_ts) not in (int, float)
            or type(actual_ts) is bool
            or not math.isfinite(actual_ts)
            or actual_ts != expected_connected_at
        ):
            raise IntegrationTokenPostconditionError("Postcondition connected_at timestamp mismatch")

    if expected_disconnected_at is not None:
        actual_ts = data.get(f"{provider}_disconnected_at")
        if (
            type(actual_ts) not in (int, float)
            or type(actual_ts) is bool
            or not math.isfinite(actual_ts)
            or actual_ts != expected_disconnected_at
        ):
            raise IntegrationTokenPostconditionError("Postcondition disconnected_at timestamp mismatch")

    # Verify exact expires_at
    if expected_expires_at is not None:
        actual_exp = data.get(f"{provider}_token_expires_at")
        if (
            type(actual_exp) not in (int, float)
            or type(actual_exp) is bool
            or not math.isfinite(actual_exp)
            or actual_exp != expected_expires_at
        ):
            raise IntegrationTokenPostconditionError("Postcondition token_expires_at timestamp mismatch")

    # Verify extra fields with exact scalar/composite equality
    if expected_extra_fields:
        for k, v in expected_extra_fields.items():
            if not _exact_scalar_or_composite_equal(data.get(k), v):
                raise IntegrationTokenPostconditionError(f"Postcondition extra field mismatch for {k}")

    return data


def _verify_audit_postcondition(db: Any, audit_id: str, expected_data: dict[str, Any]) -> None:
    """Verify that an audit document was committed to Firestore with exact expected fields and exact types."""
    if not audit_id or type(audit_id) is not str:
        raise IntegrationTokenPostconditionError("Audit document ID is empty or invalid")
    if type(expected_data) is not dict:
        raise IntegrationTokenPostconditionError("Expected audit data is not an exact dict")
    for k in expected_data.keys():
        if type(k) is not str:
            raise IntegrationTokenPostconditionError("Expected audit keys must be exact str")
    audit_snap = db.collection(AUDIT_COLLECTION).document(audit_id).get()
    if not getattr(audit_snap, "exists", False):
        raise IntegrationTokenPostconditionError("Audit document was not committed to Firestore")
    actual_data = audit_snap.to_dict()
    if type(actual_data) is not dict:
        raise IntegrationTokenPostconditionError("Audit document snapshot is not an exact dict")
    for k in actual_data.keys():
        if type(k) is not str:
            raise IntegrationTokenPostconditionError("Actual audit keys must be exact str")
    if set(actual_data.keys()) != set(expected_data.keys()):
        raise IntegrationTokenPostconditionError("Audit document keys do not match expected exact key set")
    for k, expected_v in expected_data.items():
        actual_v = actual_data[k]
        if not _exact_scalar_or_composite_equal(actual_v, expected_v):
            raise IntegrationTokenPostconditionError(f"Audit document field mismatch for {k}")


# ═══════════════════════════════════════════════════════════════════════
# Multi-Instance Durable Refresh Lease Coordination
# ═══════════════════════════════════════════════════════════════════════

async def acquire_refresh_claim_cas(
    *,
    contractor_id: str,
    provider: str,
    observed_generation: int,
    observed_access_raw: Any,
    observed_refresh_raw: Any,
    lease_duration: float = LEASE_DURATION_SECONDS,
    db: Any = None,
) -> tuple[str, float]:
    """Acquire a cross-process multi-instance refresh lease claim in Firestore before invoking provider HTTP endpoints.

    Returns (claim_id, claim_expires_at).
    Raises IntegrationTokenLeaseError if an active lease is held by another process.
    Raises IntegrationTokenCASConflict if observed generation or raw credentials do not match.
    """
    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            raise IntegrationTokenEnvelopeError("Database unavailable") from None

    if db is None:
        raise IntegrationTokenEnvelopeError("Database unavailable")

    valid_cid = validate_token_string(contractor_id, name="contractor_id")
    assert valid_cid is not None

    if type(provider) is not str or provider not in VALID_PROVIDERS:
        raise IntegrationTokenEnvelopeError("Invalid provider")

    if type(observed_generation) is not int or type(observed_generation) is bool or not (0 <= observed_generation <= MAX_KEY_VERSION):
        raise IntegrationTokenEnvelopeError("Invalid observed_generation")

    if (
        type(lease_duration) not in (int, float)
        or type(lease_duration) is bool
        or not math.isfinite(lease_duration)
        or not (1.0 <= lease_duration <= 3600.0)
    ):
        raise IntegrationTokenEnvelopeError("Invalid lease_duration: must be a finite float between 1.0 and 3600.0")

    claim_id = secrets.token_hex(16)
    final_expires_at_box: list[float] = [0.0]

    doc_ref = db.collection("contractors").document(valid_cid)

    @transactional
    def _acquire_txn(transaction):
        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            raise IntegrationTokenCASConflict("Contractor document not found")

        d_data = doc_snap.to_dict() or {}
        if d_data.get("active") is not True:
            raise IntegrationTokenCASConflict("Contractor document is not active")

        conn_val = d_data.get(f"{provider}_connected")
        if f"{provider}_connected" in d_data:
            if type(conn_val) is not bool:
                raise IntegrationTokenCASConflict("Provider connected flag is malformed")
            if conn_val is False:
                raise IntegrationTokenCASConflict("Provider is not connected")
        else:
            # Legacy record without provider_connected: accept only if both stored credentials form a valid durable pair
            stored_access_check = d_data.get(f"{provider}_access_token")
            stored_refresh_check = d_data.get(f"{provider}_refresh_token")
            if stored_access_check is None or stored_refresh_check is None:
                raise IntegrationTokenCASConflict("Provider is not connected (missing legacy credentials)")

        current_gen = d_data.get(f"{provider}_generation", 0)
        if current_gen is None:
            current_gen = 0
        if type(current_gen) is not int or type(current_gen) is bool or not (0 <= current_gen <= MAX_KEY_VERSION):
            raise IntegrationTokenCASConflict("Invalid generation on document")

        if current_gen != observed_generation:
            raise IntegrationTokenCASConflict("Generation conflict")

        if not _exact_raw_credential_equal(d_data.get(f"{provider}_access_token"), observed_access_raw):
            raise IntegrationTokenCASConflict("Stored access token credential mismatch")

        if not _exact_raw_credential_equal(d_data.get(f"{provider}_refresh_token"), observed_refresh_raw):
            raise IntegrationTokenCASConflict("Stored refresh token credential mismatch")

        env_req = d_data.get(f"{provider}_token_envelope_required")
        if f"{provider}_token_envelope_required" in d_data and type(env_req) is not bool:
            raise IntegrationTokenCASConflict("Malformed token_envelope_required flag on document")

        stored_access = d_data.get(f"{provider}_access_token")
        stored_refresh = d_data.get(f"{provider}_refresh_token")
        try:
            determine_write_format(
                contractor_id=valid_cid,
                provider=provider,
                stored_access=stored_access,
                stored_refresh=stored_refresh,
                envelope_required=env_req,
            )
        except (IntegrationTokenConfigError, IntegrationTokenDecryptionError, IntegrationTokenEnvelopeError) as exc:
            raise IntegrationTokenCASConflict(f"Write format preflight failed under CAS: {exc}") from exc

        attempt_now = time.time()
        attempt_expires_at = attempt_now + lease_duration

        k_claim_id = f"{provider}_refresh_claim_id"
        k_claim_exp = f"{provider}_refresh_claim_expires_at"
        k_claim_gen = f"{provider}_refresh_claim_generation"

        has_claim_id = k_claim_id in d_data
        has_claim_exp = k_claim_exp in d_data
        has_claim_gen = k_claim_gen in d_data

        if has_claim_id or has_claim_exp or has_claim_gen:
            if not (has_claim_id and has_claim_exp and has_claim_gen):
                raise IntegrationTokenCASConflict("Malformed existing refresh claim record: incomplete claim fields")

            existing_claim_id = d_data[k_claim_id]
            existing_exp = d_data[k_claim_exp]
            existing_claim_gen = d_data[k_claim_gen]

            if (
                type(existing_claim_id) is not str
                or type(existing_claim_id) is bool
                or not _CANONICAL_STATE_REGEX.match(existing_claim_id)
            ):
                raise IntegrationTokenCASConflict("Malformed existing refresh claim record: invalid claim_id")
            try:
                validate_token_string(existing_claim_id, name="refresh_claim_id")
            except Exception:
                raise IntegrationTokenCASConflict("Malformed existing refresh claim record: invalid claim_id") from None

            if (
                type(existing_exp) not in (int, float)
                or type(existing_exp) is bool
                or not math.isfinite(existing_exp)
                or existing_exp < 0.0
            ):
                raise IntegrationTokenCASConflict("Malformed existing refresh claim record: invalid expires_at")

            if (
                type(existing_claim_gen) is not int
                or type(existing_claim_gen) is bool
                or not (0 <= existing_claim_gen <= MAX_KEY_VERSION)
                or existing_claim_gen != observed_generation
            ):
                raise IntegrationTokenCASConflict("Malformed existing refresh claim record: invalid generation")

            if existing_exp > attempt_now:
                raise IntegrationTokenLeaseError("Refresh lease actively held by another process")

        transaction.update(doc_ref, {
            f"{provider}_refresh_claim_id": claim_id,
            f"{provider}_refresh_claim_expires_at": attempt_expires_at,
            f"{provider}_refresh_claim_generation": observed_generation,
        })
        final_expires_at_box[0] = attempt_expires_at

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _acquire_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenLeaseError):
        raise
    except Exception:
        raise IntegrationTokenEnvelopeError("Failed to acquire refresh lease") from None

    # Post-verify acquired claim in durable read
    snap = doc_ref.get()
    if not getattr(snap, "exists", False):
        raise IntegrationTokenLeaseError("Failed to verify acquired refresh lease: document missing")
    s_data = snap.to_dict() or {}
    actual_claim_id = s_data.get(f"{provider}_refresh_claim_id")
    actual_expires_at = s_data.get(f"{provider}_refresh_claim_expires_at")
    actual_claim_gen = s_data.get(f"{provider}_refresh_claim_generation")
    if actual_claim_id != claim_id or type(actual_claim_id) is not str:
        raise IntegrationTokenLeaseError("Failed to verify acquired refresh lease claim_id in durable store")
    if actual_expires_at != final_expires_at_box[0] or type(actual_expires_at) not in (int, float) or type(actual_expires_at) is bool:
        raise IntegrationTokenLeaseError("Failed to verify acquired refresh lease expiry in durable store")
    if actual_claim_gen != observed_generation or type(actual_claim_gen) is not int or type(actual_claim_gen) is bool:
        raise IntegrationTokenLeaseError("Failed to verify acquired refresh lease generation in durable store")

    return claim_id, final_expires_at_box[0]


async def release_refresh_claim_cas(
    *,
    contractor_id: str,
    provider: str,
    claim_id: str,
    db: Any = None,
) -> None:
    """Release a refresh lease claim in Firestore if it matches the current claim_id."""
    if type(provider) is not str or provider not in VALID_PROVIDERS:
        return
    if (
        type(claim_id) is not str
        or type(claim_id) is bool
        or not _CANONICAL_STATE_REGEX.match(claim_id)
    ):
        return
    try:
        valid_cid = validate_token_string(contractor_id, name="contractor_id")
    except Exception:
        return
    if valid_cid is None:
        return

    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            return
    if db is None:
        return

    doc_ref = db.collection("contractors").document(valid_cid)

    @transactional
    def _release_txn(transaction):
        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            return
        d_data = doc_snap.to_dict() or {}
        held_id = d_data.get(f"{provider}_refresh_claim_id")
        if type(held_id) is str and held_id == claim_id:
            transaction.update(doc_ref, {
                f"{provider}_refresh_claim_id": DELETE_FIELD,
                f"{provider}_refresh_claim_expires_at": DELETE_FIELD,
                f"{provider}_refresh_claim_generation": DELETE_FIELD,
            })

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _release_txn(transaction))
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════
# Durable CAS Lifecycle Operations (Refresh, Connect, Disconnect, State)
# ═══════════════════════════════════════════════════════════════════════

async def persist_refreshed_tokens_cas(
    *,
    contractor_id: str,
    provider: str,
    new_access_token: str,
    new_refresh_token: str,
    observed_generation: int,
    observed_access_raw: Any,
    observed_refresh_raw: Any,
    claim_id: str,
    expires_at: Optional[float] = None,
    extra_updates: Optional[dict[str, Any]] = None,
    db: Any = None,
) -> tuple[dict[str, Any], int]:
    """Atomically persist refreshed integration tokens under a strict CAS precondition and postverify."""
    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            raise IntegrationTokenEnvelopeError("Database unavailable") from None

    if db is None:
        raise IntegrationTokenEnvelopeError("Database unavailable")

    valid_cid = validate_token_string(contractor_id, name="contractor_id")
    assert valid_cid is not None

    if type(provider) is not str or provider not in VALID_PROVIDERS:
        raise IntegrationTokenEnvelopeError("Invalid provider")

    valid_access = validate_token_string(new_access_token, name="access_token")
    assert valid_access is not None
    valid_refresh = validate_token_string(new_refresh_token, name="refresh_token")
    assert valid_refresh is not None

    if (
        type(claim_id) is not str
        or type(claim_id) is bool
        or not _CANONICAL_STATE_REGEX.match(claim_id)
    ):
        raise IntegrationTokenEnvelopeError("Invalid claim_id")
    try:
        validate_token_string(claim_id, name="claim_id")
    except Exception:
        raise IntegrationTokenEnvelopeError("Invalid claim_id") from None

    if (
        type(observed_generation) is not int
        or type(observed_generation) is bool
        or not (0 <= observed_generation <= MAX_KEY_VERSION)
    ):
        raise IntegrationTokenEnvelopeError("Invalid observed_generation")

    next_gen = observed_generation + 1
    if next_gen > MAX_KEY_VERSION:
        raise IntegrationTokenEnvelopeError("Generation overflow")

    if extra_updates is not None:
        if type(extra_updates) is not dict:
            raise IntegrationTokenEnvelopeError("extra_updates must be an exact dict")
        allowed = ALLOWED_EXTRA_UPDATES.get(provider, frozenset())
        for k, v in extra_updates.items():
            if k not in allowed:
                raise IntegrationTokenEnvelopeError("Disallowed field in extra_updates")
            if k == "jobber_lead_capture_enabled" and type(v) is not bool:
                raise IntegrationTokenEnvelopeError("Invalid value for jobber_lead_capture_enabled")
            if k == "google_calendar_scope" and (type(v) is not str or not (1 <= len(v) <= 1024)):
                raise IntegrationTokenEnvelopeError("Invalid value for google_calendar_scope")

    valid_exp = validate_token_expires_at(expires_at)
    updates_box: list[dict[str, Any]] = [{}]
    final_refreshed_at_box: list[float] = [0.0]
    expected_env_req_box: list[Optional[bool]] = [None]
    doc_ref = db.collection("contractors").document(valid_cid)

    @transactional
    def _refresh_txn(transaction):
        attempt_now = time.time()
        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            raise IntegrationTokenCASConflict("Contractor document not found")

        d_data = doc_snap.to_dict() or {}
        if d_data.get("active") is not True:
            raise IntegrationTokenCASConflict("Contractor document is not active")

        conn_val = d_data.get(f"{provider}_connected")
        if f"{provider}_connected" in d_data:
            if type(conn_val) is not bool:
                raise IntegrationTokenCASConflict("Provider connected flag is malformed")
            if conn_val is False:
                raise IntegrationTokenCASConflict("Provider is not connected")
        else:
            # Legacy record without provider_connected: accept only if both stored credentials form a valid durable pair
            stored_access_check = d_data.get(f"{provider}_access_token")
            stored_refresh_check = d_data.get(f"{provider}_refresh_token")
            if stored_access_check is None or stored_refresh_check is None:
                raise IntegrationTokenCASConflict("Provider is not connected (missing legacy credentials)")

        current_gen = d_data.get(f"{provider}_generation", 0)
        if current_gen is None:
            current_gen = 0
        if type(current_gen) is not int or type(current_gen) is bool or not (0 <= current_gen <= MAX_KEY_VERSION):
            raise IntegrationTokenCASConflict("Invalid generation on document")

        if current_gen != observed_generation:
            raise IntegrationTokenCASConflict("Generation conflict")

        if not _exact_raw_credential_equal(d_data.get(f"{provider}_access_token"), observed_access_raw):
            raise IntegrationTokenCASConflict("Stored access token credential mismatch")

        if not _exact_raw_credential_equal(d_data.get(f"{provider}_refresh_token"), observed_refresh_raw):
            raise IntegrationTokenCASConflict("Stored refresh token credential mismatch")

        k_claim_id = f"{provider}_refresh_claim_id"
        k_claim_exp = f"{provider}_refresh_claim_expires_at"
        k_claim_gen = f"{provider}_refresh_claim_generation"

        if not (k_claim_id in d_data and k_claim_exp in d_data and k_claim_gen in d_data):
            raise IntegrationTokenCASConflict("Missing refresh lease claim record on contractor document")

        held_claim_id = d_data[k_claim_id]
        held_claim_exp = d_data[k_claim_exp]
        held_claim_gen = d_data[k_claim_gen]

        if type(held_claim_id) is not str or held_claim_id != claim_id:
            raise IntegrationTokenCASConflict("Refresh lease claim ID mismatch on commit")
        if (
            type(held_claim_exp) not in (int, float)
            or type(held_claim_exp) is bool
            or not math.isfinite(held_claim_exp)
            or held_claim_exp <= attempt_now
        ):
            raise IntegrationTokenCASConflict("Refresh lease expired or invalid on commit")
        if (
            type(held_claim_gen) is not int
            or type(held_claim_gen) is bool
            or held_claim_gen != observed_generation
        ):
            raise IntegrationTokenCASConflict("Refresh lease generation mismatch on commit")

        env_req = d_data.get(f"{provider}_token_envelope_required")
        if f"{provider}_token_envelope_required" in d_data and type(env_req) is not bool:
            raise IntegrationTokenCASConflict("Malformed token_envelope_required flag on document")

        # Classify durable stored credentials to determine write format under CAS
        stored_access = d_data.get(f"{provider}_access_token")
        stored_refresh = d_data.get(f"{provider}_refresh_token")
        try:
            write_format = determine_write_format(
                contractor_id=valid_cid,
                provider=provider,
                stored_access=stored_access,
                stored_refresh=stored_refresh,
                envelope_required=env_req,
            )
        except (IntegrationTokenConfigError, IntegrationTokenDecryptionError, IntegrationTokenEnvelopeError) as exc:
            raise IntegrationTokenCASConflict(f"Write format policy failed under CAS: {exc}") from exc

        if write_format == "envelope":
            final_access = encrypt_integration_token(
                valid_access,
                contractor_id=valid_cid,
                provider=provider,
                token_kind="access",
            )
            final_refresh = encrypt_integration_token(
                valid_refresh,
                contractor_id=valid_cid,
                provider=provider,
                token_kind="refresh",
            )
        else:
            final_access = valid_access
            final_refresh = valid_refresh

        updates: dict[str, Any] = {
            f"{provider}_access_token": final_access,
            f"{provider}_refresh_token": final_refresh,
            f"{provider}_generation": next_gen,
            f"{provider}_connected": True,
            f"{provider}_token_refreshed_at": attempt_now,
            f"{provider}_refresh_claim_id": DELETE_FIELD,
            f"{provider}_refresh_claim_expires_at": DELETE_FIELD,
            f"{provider}_refresh_claim_generation": DELETE_FIELD,
        }
        if write_format == "envelope":
            updates[f"{provider}_token_envelope_required"] = True
            expected_env_req_box[0] = True
        elif env_req is True:
            expected_env_req_box[0] = True

        if valid_exp is not None:
            updates[f"{provider}_token_expires_at"] = valid_exp
        else:
            updates[f"{provider}_token_expires_at"] = DELETE_FIELD

        if extra_updates:
            for k, v in extra_updates.items():
                if k not in updates:
                    updates[k] = v

        updates_box[0] = updates
        final_refreshed_at_box[0] = attempt_now
        transaction.update(doc_ref, updates)

    deleted_claim_keys = {
        f"{provider}_refresh_claim_id",
        f"{provider}_refresh_claim_expires_at",
        f"{provider}_refresh_claim_generation",
    }
    if valid_exp is None:
        deleted_claim_keys.add(f"{provider}_token_expires_at")

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _refresh_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenEnvelopeError):
        raise
    except Exception:
        # Ambiguous commit recovery check using complete shared postcondition
        try:
            expected_payload = {
                f"{provider}_access_token": updates_box[0].get(f"{provider}_access_token"),
                f"{provider}_refresh_token": updates_box[0].get(f"{provider}_refresh_token"),
                f"{provider}_generation": next_gen,
                f"{provider}_connected": True,
                f"{provider}_token_refreshed_at": final_refreshed_at_box[0],
            }
            if valid_exp is not None:
                expected_payload[f"{provider}_token_expires_at"] = valid_exp
            if extra_updates:
                for k, v in extra_updates.items():
                    expected_payload[k] = v
            _verify_mutation_postcondition(
                doc_ref,
                expected_generation=next_gen,
                expected_connected=True,
                provider=provider,
                expected_access_envelope=updates_box[0].get(f"{provider}_access_token"),
                expected_refresh_envelope=updates_box[0].get(f"{provider}_refresh_token"),
                expected_token_refreshed_at=final_refreshed_at_box[0],
                expected_expires_at=valid_exp,
                expected_extra_fields=extra_updates,
                expected_envelope_required=expected_env_req_box[0],
                deleted_fields=deleted_claim_keys,
            )
            return updates_box[0], next_gen
        except Exception:
            pass
        raise IntegrationTokenCASConflict("Transaction commit failed with ambiguous state") from None

    # Verification: independent durable read
    _verify_mutation_postcondition(
        doc_ref,
        expected_generation=next_gen,
        expected_connected=True,
        provider=provider,
        expected_access_envelope=updates_box[0].get(f"{provider}_access_token"),
        expected_refresh_envelope=updates_box[0].get(f"{provider}_refresh_token"),
        expected_token_refreshed_at=final_refreshed_at_box[0],
        expected_expires_at=valid_exp,
        expected_extra_fields=extra_updates,
        expected_envelope_required=expected_env_req_box[0],
        deleted_fields=deleted_claim_keys,
    )

    return updates_box[0], next_gen

    return updates_box[0], next_gen


async def disconnect_provider_cas(
    *,
    contractor_id: str,
    provider: str,
    actor: str = "contractor_api",
    reason: Optional[str] = None,
    db: Any = None,
) -> tuple[int, Optional[str], str]:
    """Atomically disconnect a provider: advances generation, tombstones credentials, records audit event."""
    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            raise IntegrationTokenEnvelopeError("Database unavailable") from None

    if db is None:
        raise IntegrationTokenEnvelopeError("Database unavailable")

    valid_cid = validate_token_string(contractor_id, name="contractor_id")
    assert valid_cid is not None

    if type(provider) is not str or provider not in VALID_PROVIDERS:
        raise IntegrationTokenEnvelopeError("Invalid provider")

    doc_ref = db.collection("contractors").document(valid_cid)
    tombstone_gen_box = [0]
    final_disconnected_at_box: list[float] = [0.0]
    expected_env_req_box: list[Optional[bool]] = [None]
    access_token_for_revoke_box: list[Optional[str]] = [None]
    audit_event_id_box: list[str] = [""]
    audit_event_box: list[dict[str, Any]] = [{}]

    @transactional
    def _disconnect_txn(transaction):
        attempt_now = time.time()
        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            raise IntegrationTokenCASConflict("Contractor document not found")

        d_data = doc_snap.to_dict() or {}
        current_gen = d_data.get(f"{provider}_generation", 0)
        if current_gen is None:
            current_gen = 0
        if type(current_gen) is not int or type(current_gen) is bool or not (0 <= current_gen <= MAX_KEY_VERSION):
            raise IntegrationTokenEnvelopeError("Invalid generation on document")

        tombstone_gen = current_gen + 1
        if tombstone_gen > MAX_KEY_VERSION:
            raise IntegrationTokenEnvelopeError("Generation overflow")

        tombstone_gen_box[0] = tombstone_gen

        raw_access = d_data.get(f"{provider}_access_token")
        if raw_access:
            access_token_for_revoke_box[0] = safe_decrypt_integration_token(
                raw_access,
                contractor_id=valid_cid,
                provider=provider,
                token_kind="access",
            )

        updates = {
            f"{provider}_connected": False,
            f"{provider}_generation": tombstone_gen,
            f"{provider}_disconnected_at": attempt_now,
            f"{provider}_connected_at": DELETE_FIELD,
            f"{provider}_token_refreshed_at": DELETE_FIELD,
            f"{provider}_access_token": DELETE_FIELD,
            f"{provider}_refresh_token": DELETE_FIELD,
            f"{provider}_token_expires_at": DELETE_FIELD,
            f"{provider}_refresh_claim_id": DELETE_FIELD,
            f"{provider}_refresh_claim_expires_at": DELETE_FIELD,
            f"{provider}_refresh_claim_generation": DELETE_FIELD,
        }

        floor_key = f"{provider}_token_envelope_required"
        floor_present = floor_key in d_data
        floor_val = d_data.get(floor_key)
        has_access_dict = type(d_data.get(f"{provider}_access_token")) is dict
        has_refresh_dict = type(d_data.get(f"{provider}_refresh_token")) is dict
        has_dict_cred = has_access_dict or has_refresh_dict

        # Disconnect conservatively normalizes any non-exact-bool floor marker to exact True,
        # and establishes/preserves True if the prior value was True or any dict credentials exist.
        should_require_floor = False
        if floor_present:
            if type(floor_val) is not bool:
                # Present but not exact bool -> conservatively normalize to exact True
                should_require_floor = True
            elif floor_val is True:
                # Prior exact value is True -> preserve True
                should_require_floor = True
            elif has_dict_cred:
                # Prior exact value is False but dict credentials exist -> upgrade to True
                should_require_floor = True
        elif has_dict_cred:
            # Absent floor marker but dict credentials exist -> establish True
            should_require_floor = True

        if should_require_floor:
            updates[floor_key] = True
            expected_env_req_box[0] = True
        else:
            if floor_present and floor_val is False:
                expected_env_req_box[0] = False
            else:
                expected_env_req_box[0] = None

        if provider == "jobber":
            updates["jobber_lead_capture_enabled"] = False
        elif provider == "google_calendar":
            updates["google_calendar_scope"] = DELETE_FIELD

        final_disconnected_at_box[0] = attempt_now
        transaction.update(doc_ref, updates)

        audit_doc_id = format_audit_doc_id(
            contractor_id=valid_cid,
            provider=provider,
            generation=tombstone_gen,
            action="credentials_deleted",
        )
        audit_event_id_box[0] = audit_doc_id

        audit_data = build_disconnect_audit_event(
            contractor_id=valid_cid,
            provider=provider,
            generation=tombstone_gen,
            actor=actor,
            reason=reason or "contractor_initiated_disconnect",
            revocation_status="pending",
            timestamp=attempt_now,
        )
        audit_event_box[0] = audit_data
        audit_ref = db.collection(AUDIT_COLLECTION).document(audit_doc_id)
        transaction.set(audit_ref, audit_data)

    deleted_token_fields = {
        f"{provider}_access_token",
        f"{provider}_refresh_token",
        f"{provider}_token_expires_at",
        f"{provider}_connected_at",
        f"{provider}_token_refreshed_at",
        f"{provider}_refresh_claim_id",
        f"{provider}_refresh_claim_expires_at",
        f"{provider}_refresh_claim_generation",
    }
    if provider == "google_calendar":
        deleted_token_fields.add("google_calendar_scope")

    extra_post_fields = {}
    if provider == "jobber":
        extra_post_fields["jobber_lead_capture_enabled"] = False

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _disconnect_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenEnvelopeError):
        raise
    except Exception:
        try:
            _verify_mutation_postcondition(
                doc_ref,
                expected_generation=tombstone_gen_box[0],
                expected_connected=False,
                provider=provider,
                expected_disconnected_at=final_disconnected_at_box[0],
                expected_extra_fields=extra_post_fields,
                expected_envelope_required=expected_env_req_box[0],
                deleted_fields=deleted_token_fields,
            )
            _verify_audit_postcondition(db, audit_event_id_box[0], audit_event_box[0])
            return tombstone_gen_box[0], access_token_for_revoke_box[0], audit_event_id_box[0]
        except Exception:
            pass
        raise IntegrationTokenCASConflict("Disconnect transaction failed with ambiguous state") from None

    # Postcondition verification: deleted fields must be truly ABSENT
    _verify_mutation_postcondition(
        doc_ref,
        expected_generation=tombstone_gen_box[0],
        expected_connected=False,
        provider=provider,
        expected_disconnected_at=final_disconnected_at_box[0],
        expected_extra_fields=extra_post_fields,
        expected_envelope_required=expected_env_req_box[0],
        deleted_fields=deleted_token_fields,
    )
    _verify_audit_postcondition(db, audit_event_id_box[0], audit_event_box[0])

    return tombstone_gen_box[0], access_token_for_revoke_box[0], audit_event_id_box[0]


async def connect_provider_cas(
    *,
    contractor_id: str,
    provider: str,
    access_token: str,
    refresh_token: str,
    expires_in: Optional[float] = None,
    expires_at: Optional[float] = None,
    scope: Optional[str] = None,
    observed_generation: Optional[int] = None,
    observed_access_raw: Any = None,
    observed_refresh_raw: Any = None,
    actor: str = "oauth_state",
    extra_updates: Optional[dict[str, Any]] = None,
    db: Any = None,
) -> tuple[dict[str, Any], int, str]:
    """Atomically connect or reconnect a provider: advances generation, installs encrypted credentials, records audit event."""
    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            raise IntegrationTokenEnvelopeError("Database unavailable") from None

    if db is None:
        raise IntegrationTokenEnvelopeError("Database unavailable")

    valid_cid = validate_token_string(contractor_id, name="contractor_id")
    assert valid_cid is not None

    if type(provider) is not str or provider not in VALID_PROVIDERS:
        raise IntegrationTokenEnvelopeError("Invalid provider")

    valid_access = validate_token_string(access_token, name="access_token")
    assert valid_access is not None

    valid_refresh = validate_token_string(refresh_token, name="refresh_token", allow_none=False)
    assert valid_refresh is not None

    if observed_generation is not None:
        if (
            type(observed_generation) is not int
            or type(observed_generation) is bool
            or not (0 <= observed_generation <= MAX_KEY_VERSION)
        ):
            raise IntegrationTokenEnvelopeError("Invalid observed_generation")

    if scope is not None:
        if provider == "google_calendar":
            if type(scope) is not str or not (1 <= len(scope) <= 1024):
                raise IntegrationTokenEnvelopeError("Invalid google_calendar_scope")
        else:
            raise IntegrationTokenEnvelopeError("scope is not allowed for provider")

    if extra_updates is not None:
        if type(extra_updates) is not dict:
            raise IntegrationTokenEnvelopeError("extra_updates must be an exact dict")
        allowed = ALLOWED_EXTRA_UPDATES.get(provider, frozenset())
        for k, v in extra_updates.items():
            if k not in allowed:
                raise IntegrationTokenEnvelopeError("Disallowed field in extra_updates")
            if k == "jobber_lead_capture_enabled" and type(v) is not bool:
                raise IntegrationTokenEnvelopeError("Invalid value for jobber_lead_capture_enabled")
            if k == "google_calendar_scope" and (type(v) is not str or not (1 <= len(v) <= 1024)):
                raise IntegrationTokenEnvelopeError("Invalid value for google_calendar_scope")

    now = time.time()
    effective_expires_at: Optional[float] = None
    if expires_at is not None:
        effective_expires_at = validate_token_expires_at(expires_at)
    elif expires_in is not None:
        valid_in = validate_token_expires_in(expires_in)
        if valid_in is not None:
            effective_expires_at = now + valid_in

    doc_ref = db.collection("contractors").document(valid_cid)
    next_gen_box = [0]
    final_connected_at_box: list[float] = [0.0]
    expected_env_req_box: list[Optional[bool]] = [None]
    updates_box: list[dict[str, Any]] = [{}]
    audit_event_id_box: list[str] = [""]
    audit_event_box: list[dict[str, Any]] = [{}]

    @transactional
    def _connect_txn(transaction):
        attempt_now = time.time()
        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            raise IntegrationTokenCASConflict("Contractor document not found")

        d_data = doc_snap.to_dict() or {}
        if d_data.get("active") is not True:
            raise IntegrationTokenCASConflict("Contractor document is not active")

        current_gen = d_data.get(f"{provider}_generation", 0)
        if current_gen is None:
            current_gen = 0
        if type(current_gen) is not int or type(current_gen) is bool or not (0 <= current_gen <= MAX_KEY_VERSION):
            raise IntegrationTokenEnvelopeError("Invalid generation on document")

        if observed_generation is not None:
            if current_gen != observed_generation:
                raise IntegrationTokenCASConflict("Generation conflict")
            if not _exact_raw_credential_equal(d_data.get(f"{provider}_access_token"), observed_access_raw):
                raise IntegrationTokenCASConflict("Stored access token credential mismatch")
            if not _exact_raw_credential_equal(d_data.get(f"{provider}_refresh_token"), observed_refresh_raw):
                raise IntegrationTokenCASConflict("Stored refresh token credential mismatch")

        next_gen = current_gen + 1
        if next_gen > MAX_KEY_VERSION:
            raise IntegrationTokenEnvelopeError("Generation overflow")

        next_gen_box[0] = next_gen

        env_req = d_data.get(f"{provider}_token_envelope_required")
        if f"{provider}_token_envelope_required" in d_data and type(env_req) is not bool:
            raise IntegrationTokenCASConflict("Malformed token_envelope_required flag on document")

        # Classify durable stored credentials to determine write format under CAS
        stored_access = d_data.get(f"{provider}_access_token")
        stored_refresh = d_data.get(f"{provider}_refresh_token")
        try:
            write_format = determine_write_format(
                contractor_id=valid_cid,
                provider=provider,
                stored_access=stored_access,
                stored_refresh=stored_refresh,
                envelope_required=env_req,
            )
        except (IntegrationTokenConfigError, IntegrationTokenDecryptionError, IntegrationTokenEnvelopeError) as exc:
            raise IntegrationTokenCASConflict(f"Write format policy failed under CAS: {exc}") from exc

        if write_format == "envelope":
            final_access = encrypt_integration_token(
                valid_access,
                contractor_id=valid_cid,
                provider=provider,
                token_kind="access",
            )
            final_refresh = encrypt_integration_token(
                valid_refresh,
                contractor_id=valid_cid,
                provider=provider,
                token_kind="refresh",
            )
        else:
            final_access = valid_access
            final_refresh = valid_refresh

        updates = {
            f"{provider}_connected": True,
            f"{provider}_generation": next_gen,
            f"{provider}_connected_at": attempt_now,
            f"{provider}_disconnected_at": DELETE_FIELD,
            f"{provider}_token_refreshed_at": DELETE_FIELD,
            f"{provider}_access_token": final_access,
            f"{provider}_refresh_token": final_refresh,
            f"{provider}_refresh_claim_id": DELETE_FIELD,
            f"{provider}_refresh_claim_expires_at": DELETE_FIELD,
            f"{provider}_refresh_claim_generation": DELETE_FIELD,
        }
        if write_format == "envelope":
            updates[f"{provider}_token_envelope_required"] = True
            expected_env_req_box[0] = True
        elif env_req is True:
            expected_env_req_box[0] = True

        if effective_expires_at is not None:
            updates[f"{provider}_token_expires_at"] = effective_expires_at
        else:
            updates[f"{provider}_token_expires_at"] = DELETE_FIELD

        if scope is not None:
            updates[f"{provider}_scope"] = scope
        if extra_updates:
            for k, v in extra_updates.items():
                if k not in updates:
                    updates[k] = v

        updates_box[0] = updates
        final_connected_at_box[0] = attempt_now
        transaction.update(doc_ref, updates)

        has_existing_credentials = stored_access is not None and stored_refresh is not None
        action_name = "reconnected" if (current_gen > 0 or has_existing_credentials) else "connected"
        audit_doc_id = format_audit_doc_id(
            contractor_id=valid_cid,
            provider=provider,
            generation=next_gen,
            action=action_name,
        )
        audit_event_id_box[0] = audit_doc_id

        audit_data = build_connect_audit_event(
            contractor_id=valid_cid,
            provider=provider,
            generation=next_gen,
            actor=actor,
            action=action_name,
            timestamp=attempt_now,
        )
        audit_event_box[0] = audit_data
        audit_ref = db.collection(AUDIT_COLLECTION).document(audit_doc_id)
        transaction.set(audit_ref, audit_data)

    deleted_claim_keys = {
        f"{provider}_disconnected_at",
        f"{provider}_token_refreshed_at",
        f"{provider}_refresh_claim_id",
        f"{provider}_refresh_claim_expires_at",
        f"{provider}_refresh_claim_generation",
    }
    if effective_expires_at is None:
        deleted_claim_keys.add(f"{provider}_token_expires_at")

    extra_post_fields = dict(extra_updates) if extra_updates else {}
    if scope is not None:
        extra_post_fields[f"{provider}_scope"] = scope

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _connect_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenEnvelopeError):
        raise
    except Exception:
        try:
            _verify_mutation_postcondition(
                doc_ref,
                expected_generation=next_gen_box[0],
                expected_connected=True,
                provider=provider,
                expected_access_envelope=updates_box[0].get(f"{provider}_access_token"),
                expected_refresh_envelope=updates_box[0].get(f"{provider}_refresh_token"),
                expected_connected_at=final_connected_at_box[0],
                expected_expires_at=effective_expires_at,
                expected_extra_fields=extra_post_fields,
                expected_envelope_required=expected_env_req_box[0],
                deleted_fields=deleted_claim_keys,
            )
            _verify_audit_postcondition(db, audit_event_id_box[0], audit_event_box[0])
            return updates_box[0], next_gen_box[0], audit_event_id_box[0]
        except Exception:
            pass
        raise IntegrationTokenCASConflict("Connect transaction failed with ambiguous state") from None

    # Postcondition verification
    _verify_mutation_postcondition(
        doc_ref,
        expected_generation=next_gen_box[0],
        expected_connected=True,
        provider=provider,
        expected_access_envelope=updates_box[0].get(f"{provider}_access_token"),
        expected_refresh_envelope=updates_box[0].get(f"{provider}_refresh_token"),
        expected_connected_at=final_connected_at_box[0],
        expected_expires_at=effective_expires_at,
        expected_extra_fields=extra_post_fields,
        expected_envelope_required=expected_env_req_box[0],
        deleted_fields=deleted_claim_keys,
    )
    _verify_audit_postcondition(db, audit_event_id_box[0], audit_event_box[0])

    return updates_box[0], next_gen_box[0], audit_event_id_box[0]


async def consume_oauth_state(
    *,
    db: Any,
    collection_name: str,
    state: str,
) -> dict[str, Any]:
    """Atomically verify and consume an OAuth state document in a transaction.

    Deletes any existent state document in Firestore (even if malformed or expired)
    and validates durable absence before raising any HTTPException outside the transaction.
    """
    if type(collection_name) is not str or collection_name not in VALID_OAUTH_COLLECTIONS:
        raise HTTPException(status_code=400, detail="Invalid OAuth state collection")

    if type(state) is not str or not _CANONICAL_STATE_REGEX.match(state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state identifier")

    if db is None:
        raise HTTPException(status_code=500, detail="Database unavailable for state validation")

    state_ref = db.collection(collection_name).document(state)
    outcome_box: list[tuple[str, dict[str, Any]]] = [("uninitialized", {})]

    @transactional
    def _consume_txn(transaction):
        state_snap = _get_doc_snapshot_in_txn(state_ref, transaction)
        if not getattr(state_snap, "exists", False):
            outcome_box[0] = ("not_found", {})
            return

        # Document exists: always stage delete to prevent replay or dead state retention
        transaction.delete(state_ref)

        data = state_snap.to_dict() or {}
        if type(data) is not dict:
            outcome_box[0] = ("malformed", {})
            return

        cid = data.get("contractor_id")
        try:
            valid_cid = validate_token_string(cid, name="contractor_id")
            if valid_cid is None:
                outcome_box[0] = ("invalid_contractor", {})
                return
        except Exception:
            outcome_box[0] = ("invalid_contractor", {})
            return

        exp = data.get("expires_at")
        if type(exp) not in (int, float) or type(exp) is bool:
            outcome_box[0] = ("invalid_expiration", {})
            return

        exp_f = float(exp)
        now = time.time()
        import math
        if not math.isfinite(exp_f) or exp_f <= now:
            outcome_box[0] = ("expired", {})
            return

        outcome_box[0] = ("valid", data)

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _consume_txn(transaction))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to consume OAuth state") from None

    status, state_data = outcome_box[0]

    # If document existed, verify durable absence after commit
    if status != "not_found":
        post_snap = state_ref.get()
        if getattr(post_snap, "exists", False):
            raise HTTPException(status_code=500, detail="Failed to delete OAuth state document")

    # Raise appropriate client error outside transaction after successful deletion commit
    if status == "not_found":
        raise HTTPException(status_code=400, detail="OAuth state expired or invalid")
    elif status == "malformed":
        raise HTTPException(status_code=400, detail="Malformed OAuth state payload")
    elif status == "invalid_contractor":
        raise HTTPException(status_code=400, detail="Invalid contractor in OAuth state")
    elif status == "invalid_expiration":
        raise HTTPException(status_code=400, detail="Invalid expiration in OAuth state")
    elif status == "expired":
        raise HTTPException(status_code=400, detail="OAuth state has expired")

    return state_data
