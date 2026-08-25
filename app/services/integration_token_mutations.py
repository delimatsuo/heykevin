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
import datetime
import math
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from google.cloud.firestore import transactional
from google.cloud.firestore_v1 import DELETE_FIELD

from app.db.admin_audit import build_lead_capture_admin_audit_event
from app.db.firestore_client import get_firestore_client
from app.db.integration_lifecycle_audit import (
    AUDIT_COLLECTION,
    DISPOSITION_EXECUTED,
    DISPOSITION_LEGACY_RECONCILED,
    DISPOSITION_PARTIAL_RECONCILED,
    REVOCATION_OUTBOX_COLLECTION,
    REVOCATION_STATUS_CONFIRMED,
    REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE,
    REVOCATION_STATUS_REJECTED,
    REVOCATION_STATUS_REQUEST_STARTED,
    REVOCATION_STATUS_TRANSPORT_ERROR,
    TERMINAL_REVOCATION_STATUSES,
    build_connect_audit_event,
    build_disconnect_audit_event,
    build_disconnect_outbox_record,
    format_audit_doc_id,
    format_outbox_doc_id,
    validate_disconnect_audit_record,
    validate_disconnect_lifecycle_pair,
    validate_outbox_record,
)
from app.services.calendar import (
    CANONICAL_GOOGLE_CALENDAR_SCOPE,
    validate_and_normalize_google_calendar_scope,
)
from app.services.integration_tokens import (
    LEGACY_CLAIM_BASE_KEYS,
    MAX_KEY_VERSION,
    OPERATION_INTENT_BASE_KEYS,
    REAUTHORIZATION_ATTEMPT_BASE_KEYS,
    VALID_PROVIDERS,
    IntegrationTokenCASConflict,
    IntegrationTokenContractorNotFound,
    IntegrationTokenConfigError,
    IntegrationTokenDecryptionError,
    IntegrationTokenEnvelopeError,
    IntegrationTokenError,
    _exact_raw_credential_equal,
    compute_raw_credentials_fingerprint,
    determine_write_format,
    encrypt_integration_token,
    get_provider_operation_intent_keys,
    get_provider_reauthorization_attempt_keys,
    is_envelope_map,
    parse_bounded_counter,
    parse_durable_lifecycle_counters,
    parse_provider_operation_intent,
    parse_provider_reauthorization_attempt,
    resolve_usable_token_pair,
    safe_decrypt_integration_token,
    validate_envelope_structure,
    validate_token_expires_at,
    validate_token_expires_in,
    validate_token_string,
)


@dataclass(frozen=True)
class DisconnectProviderResult:
    contractor_id: str
    provider: str
    generation: int
    lifecycle_epoch: int
    audit_id: str
    outbox_id: str
    credential_deletion: str
    revocation_status: str
    claim_id: str | None = field(default=None, repr=False)
    access_token_for_revocation: str | None = field(default=None, repr=False)
    audit_finalized: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    expected_disconnected_at: float = 0.0
    expected_floor: Any = None


OAUTH_PROVIDER_COLLECTIONS = {
    "jobber": "jobber_oauth_states",
    "google_calendar": "google_oauth_states",
}
VALID_OAUTH_COLLECTIONS = frozenset(OAUTH_PROVIDER_COLLECTIONS.values())

OAUTH_STATE_KEYS = frozenset({
    "contractor_id",
    "provider",
    "lifecycle_epoch",
    "generation",
    "credentials_fingerprint",
    "created_at",
    "expires_at",
})

ALLOWED_EXTRA_UPDATES = {
    "jobber": frozenset({"jobber_lead_capture_enabled"}),
    "google_calendar": frozenset({"google_calendar_scope"}),
}

_CANONICAL_STATE_REGEX = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
LEASE_DURATION_SECONDS = 60.0
_UNCHECKED = object()


def _build_deleted_credential_keys(provider: str) -> set[str]:
    keys = {
        f"{provider}_access_token",
        f"{provider}_refresh_token",
        f"{provider}_token_expires_at",
        f"{provider}_connected_at",
        f"{provider}_token_refreshed_at",
        f"{provider}_refresh_outcome_unknown",
        f"{provider}_reauthorization_required",
    }
    keys.update(get_provider_operation_intent_keys(provider))
    if provider == "google_calendar":
        keys.add("google_calendar_scope")
    return keys


def _build_deleted_claim_keys(provider: str, expires_at: float | None = None) -> set[str]:
    keys = {
        f"{provider}_disconnected_at",
        f"{provider}_token_refreshed_at",
        f"{provider}_refresh_outcome_unknown",
        f"{provider}_reauthorization_required",
    }
    keys.update(get_provider_operation_intent_keys(provider))
    if expires_at is None:
        keys.add(f"{provider}_token_expires_at")
    return keys



class IntegrationTokenLeaseError(IntegrationTokenError):
    """Raised when a concurrent worker actively holds the refresh lease for a contractor."""


class IntegrationTokenPostconditionError(IntegrationTokenEnvelopeError):
    """Raised when post-transaction durable-read verification fails against expected state."""


def _extract_snapshot_server_time(doc_snap: Any) -> float:
    """Extract Firestore server read_time as a finite float Unix timestamp from DocumentSnapshot.

    Accepts ONLY isinstance(read_time, datetime.datetime) (including Firestore DatetimeWithNanoseconds).
    Rejects all other objects even if they expose timestamp/tzinfo.
    Missing, naive, numeric, bool, malformed, update_time-only, create_time-only,
    or local clock fallbacks fail closed with IntegrationTokenEnvelopeError.
    """
    read_time = getattr(doc_snap, "read_time", None)
    if read_time is None or type(read_time) is bool:
        raise IntegrationTokenEnvelopeError("Invalid or missing snapshot read_time server timestamp")

    if not isinstance(read_time, datetime.datetime):
        raise IntegrationTokenEnvelopeError("Snapshot read_time is not an instance of datetime.datetime")

    if read_time.tzinfo is None or read_time.utcoffset() is None:
        raise IntegrationTokenEnvelopeError("Snapshot read_time is naive (missing timezone)")

    try:
        ts = read_time.timestamp()
    except Exception as exc:
        raise IntegrationTokenEnvelopeError(f"Failed to convert read_time to timestamp: {exc}") from exc

    if type(ts) not in (int, float) or type(ts) is bool:
        raise IntegrationTokenEnvelopeError("Invalid timestamp type from read_time")

    ts_f = float(ts)
    if not math.isfinite(ts_f) or ts_f <= 0.0:
        raise IntegrationTokenEnvelopeError("Non-finite or non-positive server read_time")

    return ts_f


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
    if actual is None and expected is None:
        return True
    if type(actual) is not type(expected):
        return False
    if type(actual) is dict:
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
        if len(actual) != len(expected):
            return False
        for a_elem, e_elem in zip(actual, expected):
            if not _exact_scalar_or_composite_equal(a_elem, e_elem):
                return False
        return True
    if type(actual) is bool:
        return actual is expected
    if type(actual) is int:
        return actual == expected
    if type(actual) is float:
        if not math.isfinite(actual) or not math.isfinite(expected):
            return False
        return actual == expected
    if type(actual) is str:
        return actual == expected
    if type(actual) is bytes:
        return actual == expected
    return False


def _verify_mutation_postcondition(
    doc_ref: Any,
    *,
    expected_generation: int,
    expected_connected: bool,
    provider: str,
    expected_lifecycle_epoch: int | None = None,
    expected_access_envelope: dict[str, Any] | None = None,
    expected_refresh_envelope: dict[str, Any] | None = None,
    expected_token_refreshed_at: float | None = None,
    expected_connected_at: float | None = None,
    expected_disconnected_at: float | None = None,
    expected_expires_at: float | None = None,
    expected_extra_fields: dict[str, Any] | None = None,
    expected_envelope_required: Any = _UNCHECKED,
    expected_claim_id: str | None = None,
    expected_claim_phase: str | None = None,
    expected_claim_expires_at: float | None = None,
    expected_claim_generation: int | None = None,
    deleted_fields: set[str] | None = None,
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

    # Verify lifecycle epoch if specified
    if expected_lifecycle_epoch is not None:
        actual_epoch = data.get(f"{provider}_lifecycle_epoch")
        if type(actual_epoch) is not int or type(actual_epoch) is bool or actual_epoch != expected_lifecycle_epoch:
            raise IntegrationTokenPostconditionError("Postcondition lifecycle_epoch mismatch")

    # Verify connected flag (exact bool)
    actual_connected = data.get(f"{provider}_connected")
    if type(actual_connected) is not bool or actual_connected is not expected_connected:
        raise IntegrationTokenPostconditionError("Postcondition connected flag mismatch")

    # Verify envelope required floor: _UNCHECKED vs None (absent) vs bool (exact True/False)
    if expected_envelope_required is not _UNCHECKED:
        floor_key = f"{provider}_token_envelope_required"
        if expected_envelope_required is None:
            if floor_key in data:
                raise IntegrationTokenPostconditionError("Postcondition token_envelope_required expected absent")
        elif type(expected_envelope_required) is bool:
            actual_req = data.get(floor_key)
            if type(actual_req) is not bool or actual_req is not expected_envelope_required:
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

    # Verify exact timestamps (exact float, not bool, finite)
    if expected_token_refreshed_at is not None:
        actual_ts = data.get(f"{provider}_token_refreshed_at")
        if (
            type(actual_ts) is not float
            or not math.isfinite(actual_ts)
            or actual_ts != expected_token_refreshed_at
        ):
            raise IntegrationTokenPostconditionError("Postcondition token_refreshed_at timestamp mismatch")

    if expected_connected_at is not None:
        actual_ts = data.get(f"{provider}_connected_at")
        if (
            type(actual_ts) is not float
            or not math.isfinite(actual_ts)
            or actual_ts != expected_connected_at
        ):
            raise IntegrationTokenPostconditionError("Postcondition connected_at timestamp mismatch")

    if expected_disconnected_at is not None:
        actual_ts = data.get(f"{provider}_disconnected_at")
        if (
            type(actual_ts) is not float
            or not math.isfinite(actual_ts)
            or actual_ts != expected_disconnected_at
        ):
            raise IntegrationTokenPostconditionError("Postcondition disconnected_at timestamp mismatch")

    # Verify exact expires_at (exact float, finite)
    if expected_expires_at is not None:
        actual_exp = data.get(f"{provider}_token_expires_at")
        if (
            type(actual_exp) is not float
            or not math.isfinite(actual_exp)
            or actual_exp != expected_expires_at
        ):
            raise IntegrationTokenPostconditionError("Postcondition token_expires_at timestamp mismatch")

    # Verify claim postconditions if specified
    if expected_claim_id is not None:
        actual_cid = data.get(f"{provider}_refresh_claim_id")
        if type(actual_cid) is not str or actual_cid != expected_claim_id:
            raise IntegrationTokenPostconditionError("Postcondition claim_id mismatch")

    if expected_claim_phase is not None:
        actual_cphase = data.get(f"{provider}_refresh_claim_phase")
        if type(actual_cphase) is not str or actual_cphase != expected_claim_phase:
            raise IntegrationTokenPostconditionError("Postcondition claim_phase mismatch")

    if expected_claim_expires_at is not None:
        actual_cexp = data.get(f"{provider}_refresh_claim_expires_at")
        if (
            type(actual_cexp) is not float
            or not math.isfinite(actual_cexp)
            or actual_cexp != expected_claim_expires_at
        ):
            raise IntegrationTokenPostconditionError("Postcondition claim_expires_at mismatch")

    if expected_claim_generation is not None:
        actual_cgen = data.get(f"{provider}_refresh_claim_generation")
        if type(actual_cgen) is not int or type(actual_cgen) is bool or actual_cgen != expected_claim_generation:
            raise IntegrationTokenPostconditionError("Postcondition claim_generation mismatch")

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
    for k in expected_data:
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
            raise IntegrationTokenPostconditionError("Audit document field mismatch")


def _verify_outbox_postcondition(db: Any, outbox_id: str, expected_data: dict[str, Any]) -> None:
    """Verify that an outbox document was committed to Firestore with exact expected fields and exact types."""
    if not outbox_id or type(outbox_id) is not str:
        raise IntegrationTokenPostconditionError("Outbox document ID is empty or invalid")
    if type(expected_data) is not dict:
        raise IntegrationTokenPostconditionError("Expected outbox data is not an exact dict")
    for k in expected_data:
        if type(k) is not str:
            raise IntegrationTokenPostconditionError("Expected outbox keys must be exact str")
    outbox_snap = db.collection(REVOCATION_OUTBOX_COLLECTION).document(outbox_id).get()
    if not getattr(outbox_snap, "exists", False):
        raise IntegrationTokenPostconditionError("Outbox document was not committed to Firestore")
    actual_data = outbox_snap.to_dict()
    if type(actual_data) is not dict:
        raise IntegrationTokenPostconditionError("Outbox document snapshot is not an exact dict")
    for k in actual_data.keys():
        if type(k) is not str:
            raise IntegrationTokenPostconditionError("Actual outbox keys must be exact str")
    if set(actual_data.keys()) != set(expected_data.keys()):
        raise IntegrationTokenPostconditionError("Outbox document keys do not match expected exact key set")
    for k, expected_v in expected_data.items():
        actual_v = actual_data[k]
        if not _exact_scalar_or_composite_equal(actual_v, expected_v):
            raise IntegrationTokenPostconditionError("Outbox document field mismatch")


def _classify_durable_provider_record(
    data: Any,
    provider: str,
    contractor_id: str,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """Pure exact classifier for durable contractor integration records.

    Used both before and after legacy metadata normalization.
    Returns (status, snapshot_dict_or_none, legacy_info_dict_or_none).
    status: 'valid_normalized', 'valid_legacy_unnormalized', or 'invalid'.
    """
    if type(data) is not dict:
        return "invalid", None, None

    if data.get("active") is not True:
        return "invalid", None, None

    intent_status, _, _ = parse_provider_operation_intent(data, provider)
    if intent_status != "absent":
        return "invalid", None, None

    k_reauth = f"{provider}_reauthorization_required"
    if k_reauth in data:
        v_reauth = data[k_reauth]
        if type(v_reauth) is not bool or v_reauth is not False:
            return "invalid", None, None

    k_outcome = f"{provider}_refresh_outcome_unknown"
    if k_outcome in data:
        v_outcome = data[k_outcome]
        if type(v_outcome) is not bool or v_outcome is not False:
            return "invalid", None, None

    # Floor presence & value check (never use .get to conflate absent with present None)
    k_floor = f"{provider}_token_envelope_required"
    if k_floor in data:
        v_floor = data[k_floor]
        if type(v_floor) is not bool:
            return "invalid", None, None
        floor_val = v_floor
    else:
        floor_val = None

    k_access = f"{provider}_access_token"
    k_refresh = f"{provider}_refresh_token"
    if k_access not in data or k_refresh not in data:
        return "invalid", None, None

    raw_access = data[k_access]
    raw_refresh = data[k_refresh]

    acc, ref = resolve_usable_token_pair(
        data,
        provider=provider,
        contractor_id=contractor_id,
    )
    if type(acc) is not str or not acc or type(ref) is not str or not ref:
        return "invalid", None, None

    # Floor compliance
    is_env_acc = is_envelope_map(raw_access)
    is_env_ref = is_envelope_map(raw_refresh)
    if is_env_acc and is_env_ref:
        needs_floor_promotion = (floor_val is not True)
    elif type(raw_access) is str and type(raw_refresh) is str:
        if floor_val is True:
            # Plaintext credentials strictly forbidden under floor True
            return "invalid", None, None
        needs_floor_promotion = False
    else:
        return "invalid", None, None

    # Expiration check
    k_exp = f"{provider}_token_expires_at"
    if k_exp in data:
        v_exp = data[k_exp]
        if v_exp is None or type(v_exp) not in (int, float) or type(v_exp) is bool or not math.isfinite(v_exp) or v_exp <= 0.0:
            return "invalid", None, None
        exp_val = float(v_exp)
    else:
        exp_val = None

    # Google Calendar scope validation & canonical normalization
    if provider == "google_calendar":
        if "google_calendar_scope" in data:
            raw_scope = data["google_calendar_scope"]
            ok_scope, norm_scope = validate_and_normalize_google_calendar_scope(raw_scope, allow_none=False)
            if not ok_scope or norm_scope is None or type(norm_scope) is not str:
                return "invalid", None, None
            scope_val = norm_scope
        else:
            scope_val = CANONICAL_GOOGLE_CALENDAR_SCOPE
    else:
        scope_val = None

    # Lifecycle fields
    k_conn = f"{provider}_connected"
    # Enforce exact lifecycle triple invariants
    lifecycle_ok, v_gen, v_epoch, lifecycle_present, _ = parse_durable_lifecycle_counters(data, provider)
    if not lifecycle_ok:
        return "invalid", None, None

    if lifecycle_present:
        v_conn = data[k_conn]
        if v_conn is not True:
            return "invalid", None, None

        if needs_floor_promotion:
            promotion_info = {
                "raw_access": raw_access,
                "raw_refresh": raw_refresh,
                "floor_val": floor_val,
                "needs_floor_promotion": True,
                "needs_lifecycle_normalization": False,
                "observed_generation": v_gen,
                "observed_lifecycle_epoch": v_epoch,
                "observed_connected": v_conn,
                "google_calendar_scope": scope_val if provider == "google_calendar" else None,
            }
            return "needs_floor_promotion", None, promotion_info

        snapshot = {
            "contractor_id": contractor_id,
            "provider": provider,
            "generation": v_gen,
            "lifecycle_epoch": v_epoch,
            "jobber_access_token": acc if provider == "jobber" else None,
            "jobber_refresh_token": ref if provider == "jobber" else None,
            "google_calendar_access_token": acc if provider == "google_calendar" else None,
            "google_calendar_refresh_token": ref if provider == "google_calendar" else None,
            "google_calendar_scope": scope_val if provider == "google_calendar" else None,
            "scope": scope_val,
            "access_token": acc,
            "refresh_token": ref,
            "access_token_raw": raw_access,
            "refresh_token_raw": raw_refresh,
            "expires_at": exp_val,
            f"{provider}_token_expires_at": exp_val,
            "connected": True,
            "data": data,
        }
        return "valid_normalized", snapshot, None

    # Legacy unnormalized path: all three lifecycle keys are strictly absent
    legacy_info = {
        "raw_access": raw_access,
        "raw_refresh": raw_refresh,
        "floor_val": floor_val,
        "needs_floor_promotion": needs_floor_promotion,
        "needs_lifecycle_normalization": True,
        "observed_generation": 0,
        "observed_generation_present": False,
        "observed_lifecycle_epoch": 0,
        "observed_epoch_present": False,
        "observed_connected": None,
        "google_calendar_scope": scope_val if provider == "google_calendar" else None,
    }
    return "valid_legacy_unnormalized", None, legacy_info


async def check_and_recover_expired_intent_preflight_cas(
    *,
    contractor_id: str,
    provider: str,
    db: Any = None,
) -> tuple[str, str | None]:
    """Preflight transactional check to recover expired intents before snapshot authorization.

    Returns:
    - ("proceed", None): Clean contractor or expired reserved cleared.
    - ("quarantined", msg): Expired started intent transitioned to exact True/True quarantine.
    - ("blocked", msg): Active intent, active quarantine, or malformed state blocks refresh.
    """
    valid_cid = validate_token_string(contractor_id, name="contractor_id")
    if valid_cid is None:
        return "blocked", "invalid_contractor_id"
    if type(provider) is not str or provider not in VALID_PROVIDERS:
        return "blocked", "invalid_provider"

    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            db = None
    if db is None:
        return "blocked", "database_unavailable"

    contractor_ref = db.collection("contractors").document(valid_cid)
    preflight_box: list[tuple[str, str | None]] = [("blocked", "preflight_failed")]

    @transactional
    def _preflight_txn(transaction):
        doc_snap = _get_doc_snapshot_in_txn(contractor_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            preflight_box[0] = ("blocked", "contractor_not_found")
            return
        d_data = doc_snap.to_dict()
        if type(d_data) is not dict or d_data.get("active") is not True:
            preflight_box[0] = ("blocked", "contractor_inactive")
            return

        lifecycle_ok, current_gen, current_epoch, lifecycle_present, lifecycle_err = parse_durable_lifecycle_counters(d_data, provider)
        if not lifecycle_ok:
            preflight_box[0] = ("blocked", "invalid_lifecycle_metadata")
            return

        intent_status, parsed_intent, error_detail = parse_provider_operation_intent(d_data, provider)
        if intent_status == "absent":
            preflight_box[0] = ("proceed", None)
            return
        elif intent_status == "quarantined":
            preflight_box[0] = ("blocked", "quarantined")
            return
        elif intent_status == "malformed":
            preflight_box[0] = ("blocked", "malformed_intent")
            return
        elif intent_status == "valid" and parsed_intent is not None:
            server_now = _extract_snapshot_server_time(doc_snap)
            held_exp = parsed_intent["expires_at"]
            held_phase = parsed_intent["phase"]
            if held_exp > server_now:
                preflight_box[0] = ("blocked", "active_intent_in_progress")
                return
            else:
                # Expired intent recovery
                updates = {}
                all_intent_keys = {f"{provider}_{k}" for k in OPERATION_INTENT_BASE_KEYS}
                all_legacy_keys = {f"{provider}_{k}" for k in LEGACY_CLAIM_BASE_KEYS}
                for k in (all_intent_keys | all_legacy_keys):
                    if k in d_data:
                        updates[k] = DELETE_FIELD

                if held_phase == "reserved":
                    transaction.update(contractor_ref, updates)
                    preflight_box[0] = ("proceed", None)
                    return
                elif held_phase == "provider_request_started":
                    updates[f"{provider}_reauthorization_required"] = True
                    updates[f"{provider}_refresh_outcome_unknown"] = True
                    transaction.update(contractor_ref, updates)
                    preflight_box[0] = ("quarantined", "expired_started_quarantined")
                    return
        preflight_box[0] = ("blocked", "preflight_failed")

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _preflight_txn(transaction))
    except Exception:
        return "blocked", "preflight_failed"

    return preflight_box[0]


async def load_durable_provider_snapshot(
    contractor_id: str,
    provider: str,
    db: Any = None,
) -> dict[str, Any] | None:
    """Load fresh durable provider snapshot directly from Firestore immediately before provider call."""
    if type(provider) is not str or provider not in VALID_PROVIDERS:
        return None

    try:
        valid_cid = validate_token_string(contractor_id, name="contractor_id")
    except Exception:
        return None
    if valid_cid is None:
        return None

    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            db = None
    if db is None:
        return None

    doc_ref = db.collection("contractors").document(valid_cid)
    try:
        loop = asyncio.get_running_loop()
        doc_snap = await loop.run_in_executor(
            None,
            lambda: doc_ref.get(),
        )
        if not getattr(doc_snap, "exists", False):
            return None
        data = doc_snap.to_dict()
        if type(data) is not dict:
            return None
        _extract_snapshot_server_time(doc_snap)
    except Exception:
        return None

    status, snap_dict, mutation_info = _classify_durable_provider_record(data, provider, valid_cid)
    if status == "valid_normalized":
        return snap_dict
    if status not in ("valid_legacy_unnormalized", "needs_floor_promotion") or mutation_info is None:
        return None

    # Transactional normalization / floor promotion
    k_access = f"{provider}_access_token"
    k_refresh = f"{provider}_refresh_token"
    k_floor = f"{provider}_token_envelope_required"
    k_conn = f"{provider}_connected"
    k_gen = f"{provider}_generation"
    k_epoch = f"{provider}_lifecycle_epoch"

    try:
        @transactional
        def _normalize_or_promote_txn(transaction):
            snap_txn = _get_doc_snapshot_in_txn(doc_ref, transaction)
            if not getattr(snap_txn, "exists", False):
                raise IntegrationTokenCASConflict("Contractor missing during normalization/promotion")
            d_data = snap_txn.to_dict()
            if type(d_data) is not dict:
                raise IntegrationTokenCASConflict("Document snapshot is not an exact dict")
            _extract_snapshot_server_time(snap_txn)

            cur_status, _, cur_info = _classify_durable_provider_record(d_data, provider, valid_cid)
            if cur_status != status or cur_info is None:
                raise IntegrationTokenCASConflict("Record not eligible for normalization/promotion")

            # Exact raw credentials check
            if not _exact_raw_credential_equal(d_data.get(k_access), mutation_info["raw_access"]):
                raise IntegrationTokenCASConflict("Access token modified concurrently")
            if not _exact_raw_credential_equal(d_data.get(k_refresh), mutation_info["raw_refresh"]):
                raise IntegrationTokenCASConflict("Refresh token modified concurrently")

            # Exact floor check
            if (k_floor in d_data) != (mutation_info["floor_val"] is not None):
                raise IntegrationTokenCASConflict("Floor presence changed concurrently")
            if k_floor in d_data and d_data[k_floor] is not mutation_info["floor_val"]:
                raise IntegrationTokenCASConflict("Floor value changed concurrently")

            # Exact lifecycle check
            if mutation_info["needs_lifecycle_normalization"]:
                if (k_conn in d_data) != (mutation_info["observed_connected"] is not None):
                    raise IntegrationTokenCASConflict("Lifecycle connected modified concurrently")
                if (k_gen in d_data) != mutation_info["observed_generation_present"]:
                    raise IntegrationTokenCASConflict("Lifecycle generation modified concurrently")
                if (k_epoch in d_data) != mutation_info["observed_epoch_present"]:
                    raise IntegrationTokenCASConflict("Lifecycle epoch modified concurrently")
                if k_conn in d_data and d_data[k_conn] is not mutation_info["observed_connected"]:
                    raise IntegrationTokenCASConflict("Lifecycle connected value modified concurrently")
                if k_gen in d_data and d_data[k_gen] != mutation_info["observed_generation"]:
                    raise IntegrationTokenCASConflict("Lifecycle generation value modified concurrently")
                if k_epoch in d_data and d_data[k_epoch] != mutation_info["observed_lifecycle_epoch"]:
                    raise IntegrationTokenCASConflict("Lifecycle epoch value modified concurrently")
            else:
                act_conn = d_data.get(k_conn)
                act_gen = d_data.get(k_gen)
                act_epoch = d_data.get(k_epoch)
                if type(act_conn) is not bool or act_conn is not mutation_info["observed_connected"]:
                    raise IntegrationTokenCASConflict("Connected flag modified concurrently")
                if type(act_gen) is not int or type(act_gen) is bool or act_gen != mutation_info["observed_generation"]:
                    raise IntegrationTokenCASConflict("Generation modified concurrently")
                if type(act_epoch) is not int or type(act_epoch) is bool or act_epoch != mutation_info["observed_lifecycle_epoch"]:
                    raise IntegrationTokenCASConflict("Lifecycle epoch modified concurrently")

            update_payload: dict[str, Any] = {}
            if mutation_info["needs_lifecycle_normalization"]:
                if k_conn not in d_data:
                    update_payload[k_conn] = True
                if k_gen not in d_data:
                    update_payload[k_gen] = 0
                if k_epoch not in d_data:
                    update_payload[k_epoch] = 0
            if mutation_info["needs_floor_promotion"]:
                update_payload[k_floor] = True

            if not update_payload:
                raise IntegrationTokenCASConflict("No-op update payload")

            transaction.update(doc_ref, update_payload)

        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _normalize_or_promote_txn(transaction))
    except Exception:
        return None

    # Post-normalization fresh durable read with valid server read_time
    try:
        fresh_snap = await loop.run_in_executor(None, lambda: doc_ref.get())
        if not getattr(fresh_snap, "exists", False):
            return None
        fresh_data = fresh_snap.to_dict()
        if type(fresh_data) is not dict:
            return None
        _extract_snapshot_server_time(fresh_snap)
    except Exception:
        return None

    # Re-run pure classifier on fresh durable data
    fresh_status, fresh_snap_dict, _ = _classify_durable_provider_record(fresh_data, provider, valid_cid)
    if fresh_status != "valid_normalized":
        return None

    if mutation_info["needs_floor_promotion"] and fresh_data.get(k_floor) is not True:
        return None

    return fresh_snap_dict


# ═══════════════════════════════════════════════════════════════════════
# Multi-Instance Durable Refresh Lease Coordination
# ═══════════════════════════════════════════════════════════════════════

async def acquire_provider_operation_intent_cas(
    *,
    contractor_id: str,
    provider: str,
    kind: str,
    observed_generation: int | None = None,
    observed_lifecycle_epoch: int | None = None,
    observed_access_raw: Any = None,
    observed_refresh_raw: Any = None,
    lease_duration: float = LEASE_DURATION_SECONDS,
    db: Any = None,
) -> tuple[str, float]:
    """Acquire a lifecycle-bound durable provider operation intent in Firestore.

    Kinds supported: 'business', 'refresh', 'connect'.
    Starts in phase 'reserved'.
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

    if type(kind) is not str or kind not in ("business", "refresh", "connect"):
        raise IntegrationTokenEnvelopeError(f"Invalid operation intent kind: {kind}")

    if observed_generation is not None:
        if type(observed_generation) is not int or type(observed_generation) is bool or not (0 <= observed_generation <= MAX_KEY_VERSION):
            raise IntegrationTokenEnvelopeError("Invalid observed_generation")

    if observed_lifecycle_epoch is not None:
        if type(observed_lifecycle_epoch) is not int or type(observed_lifecycle_epoch) is bool or not (0 <= observed_lifecycle_epoch <= MAX_KEY_VERSION):
            raise IntegrationTokenEnvelopeError("Invalid observed_lifecycle_epoch")

    if (
        type(lease_duration) not in (int, float)
        or type(lease_duration) is bool
        or not math.isfinite(lease_duration)
        or not (1.0 <= float(lease_duration) <= 3600.0)
    ):
        raise IntegrationTokenEnvelopeError("Invalid lease_duration: must be a finite float between 1.0 and 3600.0")

    claim_id = secrets.token_hex(16)
    final_expires_at_box: list[float] = [0.0]
    outcome_box: list[str] = ["acquired"]
    current_gen_box: list[int] = [0]
    current_epoch_box: list[int] = [0]

    doc_ref = db.collection("contractors").document(valid_cid)

    @transactional
    def _acquire_txn(transaction):
        final_expires_at_box[0] = 0.0
        outcome_box[0] = "acquired"
        current_gen_box[0] = 0
        current_epoch_box[0] = 0

        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            raise IntegrationTokenCASConflict("Contractor document not found")

        d_data = doc_snap.to_dict()
        if type(d_data) is not dict or d_data.get("active") is not True:
            raise IntegrationTokenCASConflict("Contractor document is not active")

        server_now = _extract_snapshot_server_time(doc_snap)

        # Quarantine check
        reauth = d_data.get(f"{provider}_reauthorization_required")
        if reauth is True or (reauth is not None and reauth is not False):
            raise IntegrationTokenCASConflict("Provider is quarantined for reauthorization")
        outcome_unknown = d_data.get(f"{provider}_refresh_outcome_unknown")
        if outcome_unknown is True or (outcome_unknown is not None and outcome_unknown is not False):
            raise IntegrationTokenCASConflict("Provider is quarantined due to unknown refresh outcome")

        if kind in ("business", "refresh"):
            conn_val = d_data.get(f"{provider}_connected")
            if f"{provider}_connected" in d_data:
                if type(conn_val) is not bool or conn_val is not True:
                    raise IntegrationTokenCASConflict("Provider is not connected")
            else:
                stored_access_check = d_data.get(f"{provider}_access_token")
                stored_refresh_check = d_data.get(f"{provider}_refresh_token")
                if stored_access_check is None or stored_refresh_check is None:
                    raise IntegrationTokenCASConflict("Provider is not connected (missing legacy credentials)")

        lifecycle_ok, current_gen, current_epoch, lifecycle_present, lifecycle_err = parse_durable_lifecycle_counters(d_data, provider)
        if not lifecycle_ok:
            raise IntegrationTokenCASConflict(f"Invalid lifecycle metadata on contractor document: {lifecycle_err}")

        if observed_generation is not None and current_gen != observed_generation:
            raise IntegrationTokenCASConflict("Generation conflict")

        if observed_lifecycle_epoch is not None and current_epoch != observed_lifecycle_epoch:
            raise IntegrationTokenCASConflict("Lifecycle epoch conflict")

        if observed_access_raw is not None and not _exact_raw_credential_equal(d_data.get(f"{provider}_access_token"), observed_access_raw):
            raise IntegrationTokenCASConflict("Stored access token credential mismatch")

        if observed_refresh_raw is not None and not _exact_raw_credential_equal(d_data.get(f"{provider}_refresh_token"), observed_refresh_raw):
            raise IntegrationTokenCASConflict("Stored refresh token credential mismatch")

        stored_access = d_data.get(f"{provider}_access_token")
        stored_refresh = d_data.get(f"{provider}_refresh_token")
        try:
            fp = compute_raw_credentials_fingerprint(stored_access, stored_refresh)
        except Exception:
            raise IntegrationTokenCASConflict("Failed to compute credentials fingerprint") from None

        attempt_expires_at = server_now + float(lease_duration)

        intent_status, parsed_intent, error_detail = parse_provider_operation_intent(d_data, provider)
        if intent_status == "quarantined":
            raise IntegrationTokenCASConflict("Provider is quarantined for reauthorization / unknown outcome")
        elif intent_status == "malformed":
            raise IntegrationTokenCASConflict("Malformed existing refresh claim record / operation intent")
        elif intent_status == "valid":
            assert parsed_intent is not None
            if parsed_intent["expires_at"] > server_now:
                raise IntegrationTokenLeaseError("Provider operation intent actively held by another process")

            if parsed_intent["phase"] == "provider_request_started":
                quarantine_updates: dict[str, Any] = {}
                for f in (OPERATION_INTENT_BASE_KEYS | LEGACY_CLAIM_BASE_KEYS):
                    quarantine_updates[f"{provider}_{f}"] = DELETE_FIELD
                quarantine_updates[f"{provider}_refresh_outcome_unknown"] = True
                quarantine_updates[f"{provider}_reauthorization_required"] = True
                transaction.update(doc_ref, quarantine_updates)
                outcome_box[0] = "quarantined"
                return

        intent_updates: dict[str, Any] = {
            f"{provider}_operation_intent_id": claim_id,
            f"{provider}_operation_intent_kind": kind,
            f"{provider}_operation_intent_phase": "reserved",
            f"{provider}_operation_intent_acquired_at": server_now,
            f"{provider}_operation_intent_expires_at": attempt_expires_at,
            f"{provider}_operation_intent_generation": current_gen,
            f"{provider}_operation_intent_lifecycle_epoch": current_epoch,
            f"{provider}_operation_intent_credentials_fingerprint": fp,
        }

        if kind == "refresh":
            intent_updates[f"{provider}_refresh_claim_id"] = claim_id
            intent_updates[f"{provider}_refresh_claim_phase"] = "reserved"
            intent_updates[f"{provider}_refresh_claim_expires_at"] = attempt_expires_at
            intent_updates[f"{provider}_refresh_claim_generation"] = current_gen

        transaction.update(doc_ref, intent_updates)
        final_expires_at_box[0] = attempt_expires_at
        current_gen_box[0] = current_gen
        current_epoch_box[0] = current_epoch

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _acquire_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenLeaseError):
        raise
    except Exception:
        raise IntegrationTokenEnvelopeError("Failed to acquire provider operation intent") from None

    if outcome_box[0] == "quarantined":
        raise IntegrationTokenCASConflict(
            "Refresh outcome unknown: provider request expired in started phase, account quarantined for reauthorization"
        )

    # Postread verification
    post_snap = doc_ref.get()
    if not getattr(post_snap, "exists", False):
        raise IntegrationTokenLeaseError("Contractor document missing after intent acquisition")
    post_data = post_snap.to_dict()
    if type(post_data) is not dict:
        raise IntegrationTokenLeaseError("Contractor document is not an exact dict after intent acquisition")

    post_status, post_intent, post_err = parse_provider_operation_intent(post_data, provider)
    if post_status != "valid" or post_intent is None:
        raise IntegrationTokenLeaseError(f"Postread failed intent validation: {post_err}")

    if post_intent["id"] != claim_id:
        raise IntegrationTokenLeaseError("Intent ID mismatch on postread")
    if post_intent["phase"] != "reserved":
        raise IntegrationTokenLeaseError("Intent phase mismatch on postread")
    if post_intent["expires_at"] != final_expires_at_box[0]:
        raise IntegrationTokenLeaseError("Intent expires_at mismatch on postread")
    if post_intent["kind"] != kind:
        raise IntegrationTokenLeaseError("Intent kind mismatch on postread")
    if post_intent["generation"] != current_gen_box[0]:
        raise IntegrationTokenLeaseError("Intent generation mismatch on postread")
    if post_intent["lifecycle_epoch"] != current_epoch_box[0]:
        raise IntegrationTokenLeaseError("Intent lifecycle epoch mismatch on postread")

    return claim_id, final_expires_at_box[0]


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
    """Acquire a cross-process multi-instance refresh lease claim in Firestore."""
    return await acquire_provider_operation_intent_cas(
        contractor_id=contractor_id,
        provider=provider,
        kind="refresh",
        observed_generation=observed_generation,
        observed_access_raw=observed_access_raw,
        observed_refresh_raw=observed_refresh_raw,
        lease_duration=lease_duration,
        db=db,
    )


async def transition_provider_operation_intent_to_started_cas(
    *,
    contractor_id: str,
    provider: str,
    claim_id: str,
    kind: str | None = None,
    observed_generation: int | None = None,
    observed_lifecycle_epoch: int | None = None,
    observed_access_raw: Any = None,
    observed_refresh_raw: Any = None,
    lease_duration: float = LEASE_DURATION_SECONDS,
    db: Any = None,
) -> tuple[str, float]:
    """Atomically transition a live reserved provider operation intent to provider_request_started before HTTP."""
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

    if (
        type(claim_id) is not str
        or type(claim_id) is bool
        or not _CANONICAL_STATE_REGEX.fullmatch(claim_id)
    ):
        raise IntegrationTokenEnvelopeError("Invalid claim_id")

    doc_ref = db.collection("contractors").document(valid_cid)
    final_expires_at_box: list[float] = [0.0]

    @transactional
    def _transition_txn(transaction):
        final_expires_at_box[0] = 0.0

        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            raise IntegrationTokenCASConflict("Contractor document not found")

        d_data = doc_snap.to_dict()
        if type(d_data) is not dict or d_data.get("active") is not True:
            raise IntegrationTokenCASConflict("Contractor document is not active")

        lifecycle_ok, current_gen, current_epoch, _, lifecycle_err = parse_durable_lifecycle_counters(d_data, provider)
        if not lifecycle_ok:
            raise IntegrationTokenCASConflict("Invalid lifecycle metadata on contractor document")

        if observed_generation is not None and current_gen != observed_generation:
            raise IntegrationTokenCASConflict("Generation conflict")

        if observed_lifecycle_epoch is not None and current_epoch != observed_lifecycle_epoch:
            raise IntegrationTokenCASConflict("Lifecycle epoch conflict")

        stored_access = d_data.get(f"{provider}_access_token")
        stored_refresh = d_data.get(f"{provider}_refresh_token")
        if observed_access_raw is not None and not _exact_raw_credential_equal(stored_access, observed_access_raw):
            raise IntegrationTokenCASConflict("Stored access token credential mismatch")

        if observed_refresh_raw is not None and not _exact_raw_credential_equal(stored_refresh, observed_refresh_raw):
            raise IntegrationTokenCASConflict("Stored refresh token credential mismatch")

        server_now = _extract_snapshot_server_time(doc_snap)

        intent_status, parsed_intent, error_detail = parse_provider_operation_intent(d_data, provider)
        if intent_status != "valid" or parsed_intent is None:
            raise IntegrationTokenLeaseError("Provider operation intent not valid on transition")

        if parsed_intent["id"] != claim_id:
            raise IntegrationTokenLeaseError("Provider operation intent claim ID mismatch on transition to started")

        if parsed_intent["phase"] != "reserved":
            raise IntegrationTokenLeaseError("Provider operation intent is in invalid phase for transition to started")

        if kind is not None and parsed_intent["kind"] != kind:
            raise IntegrationTokenLeaseError("Provider operation intent kind mismatch")

        if parsed_intent["generation"] != current_gen or parsed_intent["lifecycle_epoch"] != current_epoch:
            raise IntegrationTokenLeaseError("Provider operation intent generation/epoch mismatch")

        computed_fp = compute_raw_credentials_fingerprint(stored_access, stored_refresh)
        if not parsed_intent.get("is_legacy") and parsed_intent["credentials_fingerprint"] != computed_fp:
            raise IntegrationTokenLeaseError("Provider operation intent credentials fingerprint mismatch")

        if parsed_intent["expires_at"] <= server_now:
            raise IntegrationTokenLeaseError("Provider operation intent expired before transition to started phase")

        new_expires_at = server_now + float(lease_duration)
        updates = {
            f"{provider}_operation_intent_phase": "provider_request_started",
            f"{provider}_operation_intent_expires_at": new_expires_at,
        }
        if f"{provider}_refresh_claim_id" in d_data:
            updates[f"{provider}_refresh_claim_phase"] = "provider_request_started"
            updates[f"{provider}_refresh_claim_expires_at"] = new_expires_at

        transaction.update(doc_ref, updates)
        final_expires_at_box[0] = new_expires_at

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _transition_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenLeaseError):
        raise
    except Exception:
        raise IntegrationTokenEnvelopeError("Failed to transition provider operation intent to started") from None

    # Postread verification
    post_snap = doc_ref.get()
    if not getattr(post_snap, "exists", False):
        raise IntegrationTokenLeaseError("Contractor document missing after transition to started")
    post_data = post_snap.to_dict()
    if type(post_data) is not dict:
        raise IntegrationTokenLeaseError("Contractor document is not an exact dict after transition to started")

    post_status, post_intent, post_err = parse_provider_operation_intent(post_data, provider)
    if post_status != "valid" or post_intent is None:
        raise IntegrationTokenLeaseError(f"Postread failed intent validation after transition: {post_err}")

    if post_intent["id"] != claim_id:
        raise IntegrationTokenLeaseError("Intent ID mismatch on postread")
    if post_intent["phase"] != "provider_request_started":
        raise IntegrationTokenLeaseError("Intent phase mismatch on postread")
    if post_intent["expires_at"] != final_expires_at_box[0]:
        raise IntegrationTokenLeaseError("Intent expires_at mismatch on postread")
    if kind is not None and post_intent["kind"] != kind:
        raise IntegrationTokenLeaseError("Intent kind mismatch on postread")

    return claim_id, final_expires_at_box[0]


async def transition_refresh_claim_to_started_cas(
    *,
    contractor_id: str,
    provider: str,
    claim_id: str,
    observed_generation: int | None = None,
    observed_lifecycle_epoch: int | None = None,
    observed_access_raw: Any = None,
    observed_refresh_raw: Any = None,
    lease_duration: float = LEASE_DURATION_SECONDS,
    db: Any = None,
) -> tuple[str, float]:
    """Atomically transition a live reserved refresh lease claim to provider_request_started."""
    return await transition_provider_operation_intent_to_started_cas(
        contractor_id=contractor_id,
        provider=provider,
        claim_id=claim_id,
        kind="refresh",
        observed_generation=observed_generation,
        observed_lifecycle_epoch=observed_lifecycle_epoch,
        observed_access_raw=observed_access_raw,
        observed_refresh_raw=observed_refresh_raw,
        lease_duration=lease_duration,
        db=db,
    )


async def release_provider_operation_intent_cas(
    *,
    contractor_id: str,
    provider: str,
    claim_id: str,
    kind: str | None = None,
    db: Any = None,
) -> bool:
    """Atomically release an unexpired reserved provider operation intent."""
    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            return False
    if db is None:
        return False

    try:
        valid_cid = validate_token_string(contractor_id, name="contractor_id")
    except Exception:
        return False
    if valid_cid is None or provider not in VALID_PROVIDERS:
        return False

    doc_ref = db.collection("contractors").document(valid_cid)
    mutated_box = [False]

    @transactional
    def _release_txn(transaction):
        mutated_box[0] = False
        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            return
        d_data = doc_snap.to_dict()
        if type(d_data) is not dict:
            return
        intent_status, parsed_intent, _ = parse_provider_operation_intent(d_data, provider)
        if intent_status != "valid" or parsed_intent is None:
            return
        if parsed_intent["id"] != claim_id or parsed_intent["phase"] != "reserved":
            return
        if kind is not None and parsed_intent["kind"] != kind:
            return
        updates = {f: DELETE_FIELD for f in get_provider_operation_intent_keys(provider)}
        transaction.update(doc_ref, updates)
        mutated_box[0] = True

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _release_txn(transaction))
    except Exception:
        return False

    if not mutated_box[0]:
        return False

    post_snap = doc_ref.get()
    post_data = post_snap.to_dict() if getattr(post_snap, "exists", False) else None
    if post_data is not None:
        p_status, _, _ = parse_provider_operation_intent(post_data, provider)
        if p_status != "absent":
            return False
    return True


async def release_refresh_claim_cas(
    *,
    contractor_id: str,
    provider: str,
    claim_id: str,
    db: Any = None,
) -> bool:
    """Release a reserved refresh lease claim in Firestore if it matches the current claim_id."""
    return await release_provider_operation_intent_cas(
        contractor_id=contractor_id,
        provider=provider,
        claim_id=claim_id,
        kind="refresh",
        db=db,
    )


async def terminalize_provider_operation_intent_cas(
    *,
    contractor_id: str,
    provider: str,
    claim_id: str,
    kind: str | None = None,
    db: Any = None,
) -> bool:
    """Atomically terminalize (clear) a provider operation intent in any phase."""
    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            return False
    if db is None:
        return False

    try:
        valid_cid = validate_token_string(contractor_id, name="contractor_id")
    except Exception:
        return False
    if valid_cid is None or provider not in VALID_PROVIDERS:
        return False

    doc_ref = db.collection("contractors").document(valid_cid)
    mutated_box = [False]

    @transactional
    def _term_txn(transaction):
        mutated_box[0] = False
        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            return
        d_data = doc_snap.to_dict()
        if type(d_data) is not dict:
            return
        intent_status, parsed_intent, _ = parse_provider_operation_intent(d_data, provider)
        if intent_status != "valid" or parsed_intent is None:
            return
        if parsed_intent["id"] != claim_id:
            return
        if kind is not None and parsed_intent["kind"] != kind:
            return
        updates = {f: DELETE_FIELD for f in get_provider_operation_intent_keys(provider)}
        transaction.update(doc_ref, updates)
        mutated_box[0] = True

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _term_txn(transaction))
    except Exception:
        return False

    if not mutated_box[0]:
        return False

    post_snap = doc_ref.get()
    post_data = post_snap.to_dict() if getattr(post_snap, "exists", False) else None
    if post_data is not None:
        p_status, _, _ = parse_provider_operation_intent(post_data, provider)
        if p_status != "absent":
            return False
    return True


async def transition_provider_reauthorization_attempt_to_started_cas(
    *,
    contractor_id: str,
    provider: str,
    claim_id: str,
    observed_generation: int,
    observed_lifecycle_epoch: int,
    observed_access_raw: Any,
    observed_refresh_raw: Any,
    lease_duration: float = LEASE_DURATION_SECONDS,
    db: Any = None,
) -> tuple[str, float]:
    """Transition a reserved reauthorization attempt to started before provider HTTP exchange.

    Requires:
    - Contractor active True and in exact True/True quarantine
    - Current lifecycle tuple equals observed_generation and observed_lifecycle_epoch
    - Stored raw credentials equal observed_access_raw and observed_refresh_raw
    - Reauthorization attempt valid, matching claim_id, phase='reserved', generation=current_gen, epoch=current_epoch
    - Reauthorization attempt credentials fingerprint equals recomputation of current stored raw credentials
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

    if (
        type(claim_id) is not str
        or type(claim_id) is bool
        or not _CANONICAL_STATE_REGEX.fullmatch(claim_id)
    ):
        raise IntegrationTokenEnvelopeError("Invalid claim_id")

    if (
        type(observed_generation) is not int
        or type(observed_generation) is bool
        or not (0 <= observed_generation <= MAX_KEY_VERSION)
    ):
        raise IntegrationTokenEnvelopeError("Invalid observed_generation")

    if (
        type(observed_lifecycle_epoch) is not int
        or type(observed_lifecycle_epoch) is bool
        or not (0 <= observed_lifecycle_epoch <= MAX_KEY_VERSION)
    ):
        raise IntegrationTokenEnvelopeError("Invalid observed_lifecycle_epoch")

    doc_ref = db.collection("contractors").document(valid_cid)
    final_expires_at_box: list[float] = [0.0]

    @transactional
    def _transition_reauth_txn(transaction):
        final_expires_at_box[0] = 0.0

        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            raise IntegrationTokenCASConflict("Contractor document not found")

        d_data = doc_snap.to_dict()
        if type(d_data) is not dict or d_data.get("active") is not True:
            raise IntegrationTokenCASConflict("Contractor document invalid or not active")

        lifecycle_ok, current_gen, current_epoch, _, lifecycle_err = parse_durable_lifecycle_counters(d_data, provider)
        if not lifecycle_ok or current_gen != observed_generation or current_epoch != observed_lifecycle_epoch:
            raise IntegrationTokenCASConflict("Lifecycle epoch/generation mismatch")

        stored_access = d_data.get(f"{provider}_access_token")
        stored_refresh = d_data.get(f"{provider}_refresh_token")
        if not _exact_raw_credential_equal(stored_access, observed_access_raw) or not _exact_raw_credential_equal(stored_refresh, observed_refresh_raw):
            raise IntegrationTokenCASConflict("Stored raw credentials mismatch")

        # Require exact True/True quarantine
        if d_data.get(f"{provider}_reauthorization_required") is not True or d_data.get(f"{provider}_refresh_outcome_unknown") is not True:
            raise IntegrationTokenCASConflict("Contractor is not in quarantine")

        att_st, parsed_att, att_err = parse_provider_reauthorization_attempt(d_data, provider)
        if att_st != "valid" or parsed_att is None:
            raise IntegrationTokenLeaseError("Invalid reauthorization attempt on commit")

        if parsed_att["id"] != claim_id:
            raise IntegrationTokenLeaseError("Reauthorization attempt claim_id mismatch")

        if parsed_att["phase"] != "reserved":
            raise IntegrationTokenLeaseError("Reauthorization attempt not in reserved phase")

        if parsed_att["generation"] != current_gen or parsed_att["lifecycle_epoch"] != current_epoch:
            raise IntegrationTokenLeaseError("Reauthorization attempt generation/epoch mismatch")

        computed_fp = compute_raw_credentials_fingerprint(stored_access, stored_refresh)
        if parsed_att["credentials_fingerprint"] != computed_fp:
            raise IntegrationTokenLeaseError("Reauthorization attempt credentials fingerprint mismatch")

        server_now = _extract_snapshot_server_time(doc_snap)
        if parsed_att["expires_at"] <= server_now:
            raise IntegrationTokenLeaseError("Reauthorization attempt has expired")

        new_expires_at = server_now + float(lease_duration)
        attempt_updates = {
            f"{provider}_reauthorization_attempt_phase": "provider_request_started",
            f"{provider}_reauthorization_attempt_acquired_at": server_now,
            f"{provider}_reauthorization_attempt_expires_at": new_expires_at,
        }
        transaction.update(doc_ref, attempt_updates)
        final_expires_at_box[0] = new_expires_at

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _transition_reauth_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenLeaseError):
        raise
    except Exception:
        raise IntegrationTokenEnvelopeError("Failed to transition reauthorization attempt to started") from None

    # Postread verification
    post_snap = doc_ref.get()
    if not getattr(post_snap, "exists", False):
        raise IntegrationTokenLeaseError("Contractor document missing after transition to started")
    post_data = post_snap.to_dict()
    if type(post_data) is not dict:
        raise IntegrationTokenLeaseError("Contractor document is not an exact dict after transition to started")

    att_st, post_att, att_err = parse_provider_reauthorization_attempt(post_data, provider)
    if att_st != "valid" or post_att is None:
        raise IntegrationTokenLeaseError(f"Postread failed reauthorization attempt validation: {att_err}")

    if post_att["id"] != claim_id or post_att["phase"] != "provider_request_started":
        raise IntegrationTokenLeaseError("Reauthorization attempt phase/ID mismatch on postread")

    return claim_id, final_expires_at_box[0]


async def terminalize_provider_reauthorization_attempt_cas(
    *,
    contractor_id: str,
    provider: str,
    claim_id: str,
    db: Any = None,
) -> bool:
    """Clear reauthorization attempt fields on contractor upon deterministic pre-dispatch failure or explicit terminal rejection.

    RETAINS exact True/True quarantine.
    """
    if type(contractor_id) is not str or type(provider) is not str or provider not in VALID_PROVIDERS:
        return False

    if type(claim_id) is not str or not _CANONICAL_STATE_REGEX.fullmatch(claim_id):
        return False

    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            return False
    if db is None:
        return False

    try:
        valid_cid = validate_token_string(contractor_id, name="contractor_id")
    except Exception:
        return False
    if valid_cid is None:
        return False

    doc_ref = db.collection("contractors").document(valid_cid)
    mutated_box = [False]

    @transactional
    def _term_attempt_txn(transaction):
        mutated_box[0] = False
        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            return

        d_data = doc_snap.to_dict()
        if type(d_data) is not dict or d_data.get("active") is not True:
            return

        att_st, parsed_att, _ = parse_provider_reauthorization_attempt(d_data, provider)
        if att_st != "valid" or parsed_att is None or parsed_att["id"] != claim_id:
            return

        term_updates = {}
        for f in get_provider_reauthorization_attempt_keys(provider):
            if f in d_data:
                term_updates[f] = DELETE_FIELD

        # Retain True/True quarantine
        term_updates[f"{provider}_reauthorization_required"] = True
        term_updates[f"{provider}_refresh_outcome_unknown"] = True

        transaction.update(doc_ref, term_updates)
        mutated_box[0] = True

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _term_attempt_txn(transaction))
    except Exception:
        return False

    return mutated_box[0]


async def quarantine_provider_reauth_cas(
    *,
    contractor_id: str,
    provider: str,
    claim_id: str,
    observed_generation: int,
    observed_lifecycle_epoch: int = 0,
    observed_access_raw: Any,
    observed_refresh_raw: Any,
    reason: str = "refresh_failed_in_started_phase",
    db: Any = None,
) -> bool:
    """Atomically establish durable unknown-outcome quarantine and clear active claim after started failure."""
    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            return False
    if db is None:
        return False

    try:
        valid_cid = validate_token_string(contractor_id, name="contractor_id")
    except Exception:
        return False
    if valid_cid is None:
        return False

    if type(provider) is not str or provider not in VALID_PROVIDERS:
        return False

    if (
        type(claim_id) is not str
        or type(claim_id) is bool
        or not _CANONICAL_STATE_REGEX.fullmatch(claim_id)
    ):
        return False

    if type(observed_generation) is not int or type(observed_generation) is bool or not (0 <= observed_generation <= MAX_KEY_VERSION):
        return False

    if type(observed_lifecycle_epoch) is not int or type(observed_lifecycle_epoch) is bool or not (0 <= observed_lifecycle_epoch <= MAX_KEY_VERSION):
        return False

    doc_ref = db.collection("contractors").document(valid_cid)
    outcome_box: list[str] = ["uncommitted"]

    @transactional
    def _quarantine_txn(transaction):
        outcome_box[0] = "uncommitted"

        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            outcome_box[0] = "not_found"
            return
        d_data = doc_snap.to_dict()
        if type(d_data) is not dict:
            outcome_box[0] = "malformed"
            return
        if d_data.get("active") is not True:
            outcome_box[0] = "inactive"
            return

        lifecycle_ok, current_gen, current_epoch, lifecycle_present, err_msg = parse_durable_lifecycle_counters(d_data, provider)
        if not lifecycle_ok:
            outcome_box[0] = "malformed"
            return

        if current_gen != observed_generation or current_epoch != observed_lifecycle_epoch:
            outcome_box[0] = "generation_mismatch"
            return

        stored_access = d_data.get(f"{provider}_access_token")
        stored_refresh = d_data.get(f"{provider}_refresh_token")
        if not _exact_raw_credential_equal(stored_access, observed_access_raw):
            outcome_box[0] = "access_mismatch"
            return
        if not _exact_raw_credential_equal(stored_refresh, observed_refresh_raw):
            outcome_box[0] = "refresh_mismatch"
            return

        intent_status, parsed_intent, error_detail = parse_provider_operation_intent(d_data, provider)
        if intent_status != "valid" or parsed_intent is None:
            outcome_box[0] = "claim_mismatch"
            return

        if (
            parsed_intent["id"] != claim_id
            or parsed_intent["phase"] != "provider_request_started"
            or parsed_intent["kind"] != "refresh"
            or parsed_intent["generation"] != current_gen
            or parsed_intent["lifecycle_epoch"] != current_epoch
        ):
            outcome_box[0] = "claim_mismatch"
            return

        computed_fp = compute_raw_credentials_fingerprint(stored_access, stored_refresh)
        if not parsed_intent.get("is_legacy") and parsed_intent["credentials_fingerprint"] != computed_fp:
            outcome_box[0] = "fingerprint_mismatch"
            return

        updates: dict[str, Any] = {}
        for f in (OPERATION_INTENT_BASE_KEYS | LEGACY_CLAIM_BASE_KEYS):
            updates[f"{provider}_{f}"] = DELETE_FIELD
        updates[f"{provider}_refresh_outcome_unknown"] = True
        updates[f"{provider}_reauthorization_required"] = True

        transaction.update(doc_ref, updates)
        outcome_box[0] = "quarantined"

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _quarantine_txn(transaction))
    except Exception:
        return False

    return outcome_box[0] == "quarantined"


async def persist_refreshed_tokens_cas(
    *,
    contractor_id: str,
    provider: str,
    new_access_token: str,
    new_refresh_token: str,
    observed_generation: int,
    observed_lifecycle_epoch: int = 0,
    observed_access_raw: Any,
    observed_refresh_raw: Any,
    claim_id: str,
    expires_at: float | None = None,
    expires_in: float | None = None,
    extra_updates: dict[str, Any] | None = None,
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
        or not _CANONICAL_STATE_REGEX.fullmatch(claim_id)
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

    if (
        type(observed_lifecycle_epoch) is not int
        or type(observed_lifecycle_epoch) is bool
        or not (0 <= observed_lifecycle_epoch <= MAX_KEY_VERSION)
    ):
        raise IntegrationTokenEnvelopeError("Invalid observed_lifecycle_epoch")

    next_gen = observed_generation + 1
    if next_gen > MAX_KEY_VERSION:
        raise IntegrationTokenEnvelopeError("Generation overflow")

    if extra_updates is not None:
        if type(extra_updates) is not dict:
            raise IntegrationTokenEnvelopeError("extra_updates must be an exact dict")
        allowed = ALLOWED_EXTRA_UPDATES.get(provider, frozenset())
        for k, v in extra_updates.items():
            if type(k) is not str:
                raise IntegrationTokenEnvelopeError("extra_updates keys must be exact str")
            if k not in allowed:
                raise IntegrationTokenEnvelopeError("Disallowed field in extra_updates")
            if k == "jobber_lead_capture_enabled":
                if type(v) is not bool:
                    raise IntegrationTokenEnvelopeError("Invalid value for jobber_lead_capture_enabled")
            elif k == "google_calendar_scope":
                ok_scope, norm_scope = validate_and_normalize_google_calendar_scope(v, allow_none=False)
                if not ok_scope or norm_scope is None or type(norm_scope) is not str:
                    raise IntegrationTokenEnvelopeError("Invalid value for google_calendar_scope")

    effective_expires_at_box: list[float | None] = [None]
    updates_box: list[dict[str, Any]] = [{}]
    final_refreshed_at_box: list[float] = [0.0]
    expected_env_req_box: list[Any] = [_UNCHECKED]
    body_prepared_box = [False]
    doc_ref = db.collection("contractors").document(valid_cid)

    @transactional
    def _refresh_txn(transaction):
        updates_box[0] = {}
        final_refreshed_at_box[0] = 0.0
        expected_env_req_box[0] = _UNCHECKED
        effective_expires_at_box[0] = None
        body_prepared_box[0] = False

        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            raise IntegrationTokenCASConflict("Contractor document not found")

        d_data = doc_snap.to_dict()
        if type(d_data) is not dict:
            raise IntegrationTokenCASConflict("Contractor document is not an exact dict")
        if d_data.get("active") is not True:
            raise IntegrationTokenCASConflict("Contractor document is not active")

        server_now = _extract_snapshot_server_time(doc_snap)

        lifecycle_ok, current_gen, current_epoch, lifecycle_present, err_msg = parse_durable_lifecycle_counters(d_data, provider)
        if not lifecycle_ok:
            raise IntegrationTokenCASConflict(f"Invalid lifecycle metadata: {err_msg}")

        if current_gen != observed_generation:
            raise IntegrationTokenCASConflict("Generation conflict")

        if current_epoch != observed_lifecycle_epoch:
            raise IntegrationTokenCASConflict("Lifecycle epoch conflict")

        stored_access = d_data.get(f"{provider}_access_token")
        stored_refresh = d_data.get(f"{provider}_refresh_token")

        if not _exact_raw_credential_equal(stored_access, observed_access_raw):
            raise IntegrationTokenCASConflict("Stored access token credential mismatch")

        if not _exact_raw_credential_equal(stored_refresh, observed_refresh_raw):
            raise IntegrationTokenCASConflict("Stored refresh token credential mismatch")

        intent_status, parsed_intent, error_detail = parse_provider_operation_intent(d_data, provider)
        if intent_status != "valid" or parsed_intent is None:
            raise IntegrationTokenCASConflict("Missing or invalid operation intent/claim record on commit")

        if parsed_intent["id"] != claim_id:
            raise IntegrationTokenCASConflict("Refresh lease claim ID mismatch on commit")

        if parsed_intent["kind"] != "refresh":
            raise IntegrationTokenCASConflict("Refresh lease kind mismatch on commit")

        if parsed_intent["phase"] != "provider_request_started":
            raise IntegrationTokenCASConflict("Refresh lease was not in provider_request_started phase on commit")

        if parsed_intent["expires_at"] <= server_now:
            raise IntegrationTokenCASConflict("Refresh lease expired or invalid on commit")

        if parsed_intent["generation"] != current_gen or parsed_intent["lifecycle_epoch"] != current_epoch:
            raise IntegrationTokenCASConflict("Refresh lease generation/epoch mismatch on commit")

        computed_fp = compute_raw_credentials_fingerprint(stored_access, stored_refresh)
        if not parsed_intent.get("is_legacy") and parsed_intent["credentials_fingerprint"] != computed_fp:
            raise IntegrationTokenCASConflict("Refresh lease credentials fingerprint mismatch on commit")

        env_req = d_data.get(f"{provider}_token_envelope_required")
        if f"{provider}_token_envelope_required" in d_data and type(env_req) is not bool:
            raise IntegrationTokenCASConflict("Malformed token_envelope_required flag on document")

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

        calculated_exp = None
        if expires_in is not None:
            valid_in = validate_token_expires_in(expires_in)
            if valid_in is not None:
                calculated_exp = server_now + valid_in
        elif expires_at is not None:
            calculated_exp = validate_token_expires_at(expires_at)
        effective_expires_at_box[0] = calculated_exp

        updates: dict[str, Any] = {
            f"{provider}_access_token": final_access,
            f"{provider}_refresh_token": final_refresh,
            f"{provider}_generation": next_gen,
            f"{provider}_connected": True,
            f"{provider}_token_refreshed_at": server_now,
            f"{provider}_refresh_outcome_unknown": DELETE_FIELD,
            f"{provider}_reauthorization_required": DELETE_FIELD,
        }
        for f in get_provider_operation_intent_keys(provider):
            updates[f] = DELETE_FIELD

        if write_format == "envelope" or env_req is True:
            updates[f"{provider}_token_envelope_required"] = True
            expected_env_req_box[0] = True
        elif env_req is False:
            updates[f"{provider}_token_envelope_required"] = False
            expected_env_req_box[0] = False
        else:
            expected_env_req_box[0] = None

        if calculated_exp is not None:
            updates[f"{provider}_token_expires_at"] = calculated_exp
        else:
            updates[f"{provider}_token_expires_at"] = DELETE_FIELD

        if extra_updates:
            for k, v in extra_updates.items():
                if k not in updates:
                    updates[k] = v

        updates_box[0] = updates
        final_refreshed_at_box[0] = server_now
        body_prepared_box[0] = True
        transaction.update(doc_ref, updates)

    deleted_claim_keys = {
        f"{provider}_refresh_claim_id",
        f"{provider}_refresh_claim_phase",
        f"{provider}_refresh_claim_expires_at",
        f"{provider}_refresh_claim_generation",
        f"{provider}_refresh_outcome_unknown",
        f"{provider}_reauthorization_required",
    }
    deleted_claim_keys.update(get_provider_operation_intent_keys(provider))

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _refresh_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenEnvelopeError):
        raise
    except Exception:
        if body_prepared_box[0]:
            recovery_deleted = set(deleted_claim_keys)
            if effective_expires_at_box[0] is None:
                recovery_deleted.add(f"{provider}_token_expires_at")
            # Ambiguous commit recovery check using complete shared postcondition
            try:
                _verify_mutation_postcondition(
                    doc_ref,
                    expected_generation=next_gen,
                    expected_connected=True,
                    provider=provider,
                    expected_access_envelope=updates_box[0].get(f"{provider}_access_token"),
                    expected_refresh_envelope=updates_box[0].get(f"{provider}_refresh_token"),
                    expected_token_refreshed_at=final_refreshed_at_box[0],
                    expected_expires_at=effective_expires_at_box[0],
                    expected_extra_fields=extra_updates,
                    expected_envelope_required=expected_env_req_box[0],
                    deleted_fields=recovery_deleted,
                )
                return updates_box[0], next_gen
            except Exception:
                pass
        raise IntegrationTokenCASConflict("Transaction commit failed with ambiguous state") from None

    # Verification: independent durable read
    if effective_expires_at_box[0] is None:
        deleted_claim_keys.add(f"{provider}_token_expires_at")

    _verify_mutation_postcondition(
        doc_ref,
        expected_generation=next_gen,
        expected_connected=True,
        provider=provider,
        expected_access_envelope=updates_box[0].get(f"{provider}_access_token"),
        expected_refresh_envelope=updates_box[0].get(f"{provider}_refresh_token"),
        expected_token_refreshed_at=final_refreshed_at_box[0],
        expected_expires_at=effective_expires_at_box[0],
        expected_extra_fields=extra_updates,
        expected_envelope_required=expected_env_req_box[0],
        deleted_fields=deleted_claim_keys,
    )

    return updates_box[0], next_gen


_FLOOR_UNSET = object()
_FLOOR_ABSENT = object()

def _build_tombstone_forbidden_fields(provider: str) -> frozenset[str]:
    keys = {
        f"{provider}_access_token",
        f"{provider}_refresh_token",
        f"{provider}_token_expires_at",
        f"{provider}_expires_at",
        f"{provider}_connected_at",
        f"{provider}_token_refreshed_at",
        f"{provider}_token_quarantine",
        f"{provider}_token_quarantine_reason",
        f"{provider}_unknown_outcome_claim_id",
        f"{provider}_refresh_outcome_unknown",
        f"{provider}_reauthorization_required",
    }
    keys.update(get_provider_operation_intent_keys(provider))
    if provider == "google_calendar":
        keys.add("google_calendar_scope")
    return frozenset(keys)


def is_durable_provider_tombstone(data: Any, provider: str, contractor_id: str) -> bool:
    """Classify whether contractor data is an exact, clean durable tombstone.

    Requires:
    - data is exact dict with all string keys
    - provider_connected is exact bool False
    - generation is exact non-negative int (0 <= gen <= MAX_KEY_VERSION)
    - lifecycle_epoch is exact non-negative int (0 <= epoch <= MAX_KEY_VERSION)
    - disconnected_at is exact finite positive float
    - monotonic envelope floor is absent or exact bool (present None or non-bool is malformed -> False)
    - all enumerated credential, expiry, claim, lease, quarantine, and connection fields are absent
    - Google scope is absent if provider is google_calendar
    - Jobber lead_capture_enabled is exact bool False if provider is jobber
    """
    if type(data) is not dict:
        return False
    for k in data.keys():
        if type(k) is not str:
            return False

    if provider not in VALID_PROVIDERS:
        return False

    # 1. provider_connected is exact bool False
    conn_val = data.get(f"{provider}_connected")
    if type(conn_val) is not bool or conn_val is not False:
        return False

    # 2. generation is exact non-negative int (required)
    gen = parse_bounded_counter(data, f"{provider}_generation", allow_absent=False)
    if gen is None:
        return False

    # 3. lifecycle_epoch is exact non-negative int (required)
    epoch = parse_bounded_counter(data, f"{provider}_lifecycle_epoch", allow_absent=False)
    if epoch is None:
        return False


    # 4. disconnected_at is exact finite positive float
    disc_at = data.get(f"{provider}_disconnected_at")
    if disc_at is None or type(disc_at) is not float or not math.isfinite(disc_at) or disc_at <= 0.0:
        return False

    # 5. monotonic envelope floor is absent or exact bool (presence-aware)
    floor_key = f"{provider}_token_envelope_required"
    if floor_key in data:
        floor_val = data[floor_key]
        if type(floor_val) is not bool:
            return False

    # 6. Forbidden credential/claim/lease/quarantine fields must be absent
    forbidden = _build_tombstone_forbidden_fields(provider)
    for f in forbidden:
        if f in data:
            return False

    # 7. Jobber lead-capture-enabled must be exact bool False
    if provider == "jobber":
        lc_val = data.get("jobber_lead_capture_enabled")
        if type(lc_val) is not bool or lc_val is not False:
            return False

    return True


def extract_revocation_access_token(
    d_data: Any,
    provider: str,
    contractor_id: str,
    generation: int,
) -> str | None:
    """Pure, presence-aware, and fail-closed revocation token extractor.

    Rules:
    - Validates contractor_id and provider inputs.
    - Explicit generation binding: snapshot generation key MUST be present, exact int (0 <= gen <= MAX_KEY_VERSION), and match generation argument exactly. Missing or None => returns None.
    - Hostile/unhashable/invalid provider or contractor returns None safely.
    - Floor is respected: malformed floor fails closed.
    - Plaintext access is eligible ONLY when floor is absent or exact False AND both access/refresh are valid plaintext strings.
    - Floor exact True REQUIRES envelopes.
    - Envelope access is eligible ONLY when both access/refresh are structurally valid envelopes, canonical, and decrypt in context.
    - Mixed representation, one-sided pairs, malformed values, or wrong context return None (zero HTTP).
    """
    try:
        if type(d_data) is not dict:
            return None
        if type(provider) is not str or provider not in VALID_PROVIDERS:
            return None
        if not isinstance(contractor_id, str) or not contractor_id:
            return None
        valid_cid = validate_token_string(contractor_id, name="contractor_id")
        if not valid_cid:
            return None

        if type(generation) is not int or type(generation) is bool or not (0 <= generation <= MAX_KEY_VERSION):
            return None

        gen_key = f"{provider}_generation"
        if gen_key not in d_data:
            return None
        snap_gen = d_data[gen_key]
        if type(snap_gen) is not int or type(snap_gen) is bool or not (0 <= snap_gen <= MAX_KEY_VERSION) or snap_gen != generation:
            return None

        floor_key = f"{provider}_token_envelope_required"
        floor_val = None
        if floor_key in d_data:
            floor_val = d_data[floor_key]
            if type(floor_val) is not bool:
                return None

        raw_access = d_data.get(f"{provider}_access_token")
        raw_refresh = d_data.get(f"{provider}_refresh_token")

        if raw_access is None or raw_refresh is None:
            return None

        # Case A: Plaintext pair
        if type(raw_access) is str and type(raw_refresh) is str:
            if floor_val is True:
                return None  # Plaintext forbidden when floor is True
            try:
                val_acc = validate_token_string(raw_access, name="access_token", allow_none=False)
                val_ref = validate_token_string(raw_refresh, name="refresh_token", allow_none=False)
                if val_acc and val_ref:
                    return val_acc
            except Exception:
                return None
            return None

        # Case B: Envelope pair
        if is_envelope_map(raw_access) and is_envelope_map(raw_refresh):
            try:
                validate_envelope_structure(raw_access)
                validate_envelope_structure(raw_refresh)
                dec_acc = safe_decrypt_integration_token(
                    raw_access,
                    contractor_id=valid_cid,
                    provider=provider,
                    token_kind="access",
                )
                dec_ref = safe_decrypt_integration_token(
                    raw_refresh,
                    contractor_id=valid_cid,
                    provider=provider,
                    token_kind="refresh",
                )
                if dec_acc and dec_ref:
                    return dec_acc
            except Exception:
                return None
            return None

        return None
    except Exception:
        return None


def is_durable_provider_connected(data: Any, provider: str, contractor_id: str) -> bool:
    """Classify whether contractor data represents a durably connected provider."""
    if type(data) is not dict or type(provider) is not str or type(contractor_id) is not str:
        return False
    for k in data.keys():
        if type(k) is not str:
            return False
    if provider not in VALID_PROVIDERS:
        return False
    if data.get(f"{provider}_connected") is not True:
        return False
    status, snap_dict, _ = _classify_durable_provider_record(data, provider, contractor_id)
    return status in ("valid_normalized", "valid_legacy_unnormalized")


def extract_safe_connected_at(data: Any, provider: str) -> float | None:
    """Extract finite float connected_at timestamp from contractor data if valid."""
    if type(data) is not dict or type(provider) is not str:
        return None
    val = data.get(f"{provider}_connected_at")
    if type(val) is float and math.isfinite(val) and val > 0:
        return val
    return None


def _verify_complete_disconnect_postcondition(
    doc_ref: Any,
    *,
    contractor_id: str,
    provider: str,
    expected_generation: int,
    expected_lifecycle_epoch: int,
    expected_disconnected_at: float,
    expected_floor: Any,
    db: Any = None,
    outbox_id: str | None = None,
    expected_outbox: dict[str, Any] | None = None,
    audit_id: str | None = None,
    expected_audit: dict[str, Any] | None = None,
) -> None:
    """Complete independent durable-read verification of a disconnect operation."""
    doc_snap = doc_ref.get()
    if not getattr(doc_snap, "exists", False):
        raise IntegrationTokenPostconditionError("Contractor document not found during disconnect postcondition verification")

    d_data = doc_snap.to_dict()
    if not is_durable_provider_tombstone(d_data, provider, contractor_id):
        raise IntegrationTokenPostconditionError("Contractor document failed durable tombstone verification")

    # Exact generation & epoch
    gen = d_data.get(f"{provider}_generation")
    if type(gen) is not int or type(gen) is bool or gen != expected_generation:
        raise IntegrationTokenPostconditionError(f"Generation mismatch on postcondition read (expected {expected_generation}, observed {gen})")

    epoch = d_data.get(f"{provider}_lifecycle_epoch")
    if type(epoch) is not int or type(epoch) is bool or epoch != expected_lifecycle_epoch:
        raise IntegrationTokenPostconditionError(f"Lifecycle epoch mismatch on postcondition read (expected {expected_lifecycle_epoch}, observed {epoch})")

    # Exact disconnected_at (required on ALL paths)
    if type(expected_disconnected_at) is not float or not math.isfinite(expected_disconnected_at) or expected_disconnected_at <= 0.0:
        raise IntegrationTokenPostconditionError(f"Invalid expected_disconnected_at {expected_disconnected_at}")
    disc_at = d_data.get(f"{provider}_disconnected_at")
    if type(disc_at) is not float or not math.isfinite(disc_at) or disc_at != expected_disconnected_at:
        raise IntegrationTokenPostconditionError(f"disconnected_at mismatch on postcondition read (expected {expected_disconnected_at}, observed {disc_at})")

    # Exact floor (presence-aware)
    floor_key = f"{provider}_token_envelope_required"
    if expected_floor is _FLOOR_ABSENT:
        if floor_key in d_data:
            raise IntegrationTokenPostconditionError("Expected floor absence")
    elif expected_floor is True or expected_floor is False:
        if floor_key not in d_data:
            raise IntegrationTokenPostconditionError("Expected floor is absent")
        actual_floor = d_data[floor_key]
        if type(actual_floor) is not bool or actual_floor is not expected_floor:
            raise IntegrationTokenPostconditionError("Floor mismatch on postcondition read")
    else:
        raise IntegrationTokenPostconditionError("expected_floor must be exact bool or _FLOOR_ABSENT")

    outbox_data = None
    audit_data = None

    # Verify Outbox Document
    if outbox_id is not None and db is not None:
        outbox_snap = db.collection(REVOCATION_OUTBOX_COLLECTION).document(outbox_id).get()
        if not getattr(outbox_snap, "exists", False):
            raise IntegrationTokenPostconditionError("Outbox record not found during postcondition verification")
        outbox_data = outbox_snap.to_dict()
        validate_outbox_record(
            outbox_data,
            expected_contractor_id=contractor_id,
            expected_provider=provider,
            expected_generation=expected_generation,
            expected_lifecycle_epoch=expected_lifecycle_epoch,
            expected_outbox_id=outbox_id,
        )
        if expected_outbox is not None:
            for k, v in expected_outbox.items():
                if k in ("outbox_id", "audit_id"):
                    continue
                if outbox_data.get(k) != v or type(outbox_data.get(k)) is not type(v):
                    if k in ("status", "audit_finalized", "audit_finalized_at", "updated_at") and outbox_data.get("status") in TERMINAL_REVOCATION_STATUSES:
                        continue
                    raise IntegrationTokenPostconditionError("Outbox field mismatch on postcondition read")

    # Verify Audit Document
    if audit_id is not None and db is not None:
        audit_snap = db.collection(AUDIT_COLLECTION).document(audit_id).get()
        if not getattr(audit_snap, "exists", False):
            raise IntegrationTokenPostconditionError("Audit record not found during postcondition verification")
        audit_data = audit_snap.to_dict()
        validate_disconnect_audit_record(
            audit_data,
            expected_contractor_id=contractor_id,
            expected_provider=provider,
            expected_generation=expected_generation,
            expected_lifecycle_epoch=expected_lifecycle_epoch,
            expected_audit_id=audit_id,
        )
        if expected_audit is not None:
            for k, v in expected_audit.items():
                if k in ("outbox_id", "audit_id"):
                    continue
                if audit_data.get(k) != v or type(audit_data.get(k)) is not type(v):
                    if k in ("revocation_status", "revocation_completed_at") and audit_data.get("revocation_status") in TERMINAL_REVOCATION_STATUSES:
                        continue
                    raise IntegrationTokenPostconditionError("Audit field mismatch on postcondition read")

    # Verify Exact Pair Coherence
    if outbox_data is not None and audit_data is not None:
        try:
            validate_disconnect_lifecycle_pair(
                audit_data,
                outbox_data,
                expected_contractor_id=contractor_id,
                expected_provider=provider,
                expected_generation=expected_generation,
                expected_lifecycle_epoch=expected_lifecycle_epoch,
                expected_audit_id=audit_id,
                expected_outbox_id=outbox_id,
            )
        except Exception:
            raise IntegrationTokenPostconditionError("Lifecycle pair coherence check failed") from None


async def disconnect_provider_envelope_cas(
    *,
    contractor_id: str,
    provider: str,
    actor: str = "contractor_api",
    reason: str | None = None,
    candidate_claim_id: str | None = None,
    db: Any = None,
) -> DisconnectProviderResult:
    """Atomically disconnect a provider: advances generation and lifecycle epoch, tombstones credentials, records audit event and revocation outbox."""
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

    # Mint a cryptographically random claim candidate before the transaction
    if candidate_claim_id is not None:
        if type(candidate_claim_id) is not str or len(candidate_claim_id) == 0:
            raise IntegrationTokenEnvelopeError("Invalid candidate_claim_id")
        claim_candidate = candidate_claim_id
    else:
        claim_candidate = secrets.token_urlsafe(32)

    doc_ref = db.collection("contractors").document(valid_cid)

    disposition_box: list[str] = [""]
    tombstone_gen_box: list[int] = [0]
    next_epoch_box: list[int] = [0]
    final_disconnected_at_box: list[float] = [0.0]
    expected_env_req_box: list[Any] = [_UNCHECKED]
    access_token_for_revoke_box: list[str | None] = [None]
    audit_event_id_box: list[str] = [""]
    outbox_id_box: list[str] = [""]
    revocation_status_box: list[str] = [""]
    claim_id_box: list[str | None] = [None]
    audit_finalized_box: list[bool] = [False]
    created_at_box: list[float] = [0.0]
    updated_at_box: list[float] = [0.0]
    audit_event_box: list[dict[str, Any]] = [{}]
    outbox_event_box: list[dict[str, Any]] = [{}]
    body_prepared_box: list[bool] = [False]

    @transactional
    def _disconnect_txn(transaction):
        # Reset all mutable result boxes on every attempt
        disposition_box[0] = ""
        tombstone_gen_box[0] = 0
        next_epoch_box[0] = 0
        final_disconnected_at_box[0] = 0.0
        expected_env_req_box[0] = _UNCHECKED
        access_token_for_revoke_box[0] = None
        audit_event_id_box[0] = ""
        outbox_id_box[0] = ""
        revocation_status_box[0] = ""
        claim_id_box[0] = None
        audit_finalized_box[0] = False
        created_at_box[0] = 0.0
        updated_at_box[0] = 0.0
        audit_event_box[0] = {}
        outbox_event_box[0] = {}
        body_prepared_box[0] = False

        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            raise IntegrationTokenContractorNotFound("Contractor document not found")

        d_data = doc_snap.to_dict()
        if type(d_data) is not dict:
            raise IntegrationTokenCASConflict("Contractor document is not an exact dict")
        for k in d_data.keys():
            if type(k) is not str:
                raise IntegrationTokenCASConflict("Contractor document contains non-string keys")

        # Check operation intent on contractor: disconnect may preempt ONLY a valid reserved intent or absent intent
        intent_status, parsed_intent, err_msg = parse_provider_operation_intent(d_data, provider)
        if intent_status in ("quarantined", "quarantined_reauthorizing"):
            raise IntegrationTokenCASConflict("Provider is in durable quarantine / unknown outcome state; disconnect rejected")
        elif intent_status == "malformed":
            raise IntegrationTokenCASConflict("Malformed existing operation intent/quarantine state")
        elif intent_status == "valid" and parsed_intent is not None:
            if parsed_intent["phase"] == "provider_request_started":
                raise IntegrationTokenCASConflict("Provider operation request started; disconnect pending completion of in-flight operation")

        server_now = _extract_snapshot_server_time(doc_snap)

        lifecycle_ok, current_gen, current_epoch, lifecycle_present, lifecycle_err = parse_durable_lifecycle_counters(d_data, provider)
        if not lifecycle_ok:
            raise IntegrationTokenCASConflict("Invalid lifecycle metadata on contractor document")

        is_connected = is_durable_provider_connected(d_data, provider, contractor_id=valid_cid)
        is_clean_tombstone = is_durable_provider_tombstone(d_data, provider, valid_cid)
        needs_mutation = not is_clean_tombstone

        if needs_mutation:
            token_for_revoke = extract_revocation_access_token(
                d_data,
                provider=provider,
                contractor_id=valid_cid,
                generation=current_gen,
            )

            if token_for_revoke is not None:
                initial_status = REVOCATION_STATUS_REQUEST_STARTED
                claim_to_set = claim_candidate
                token_to_grant = token_for_revoke
                is_finalized = False
                finalized_at = None
                rev_completed_at = None
            else:
                initial_status = REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE
                claim_to_set = None
                token_to_grant = None
                is_finalized = True
                finalized_at = server_now
                rev_completed_at = server_now

            tombstone_gen = current_gen + 1
            next_epoch = current_epoch + 1
            if tombstone_gen > MAX_KEY_VERSION:
                raise IntegrationTokenEnvelopeError("Generation overflow: exceeded MAX_KEY_VERSION")
            if next_epoch > MAX_KEY_VERSION:
                raise IntegrationTokenEnvelopeError("Lifecycle epoch overflow: exceeded MAX_KEY_VERSION")
            tombstone_gen_box[0] = tombstone_gen
            next_epoch_box[0] = next_epoch

            updates = {
                f"{provider}_connected": False,
                f"{provider}_generation": tombstone_gen,
                f"{provider}_lifecycle_epoch": next_epoch,
                f"{provider}_disconnected_at": server_now,
            }

            # Delete all forbidden fields
            forbidden = _build_tombstone_forbidden_fields(provider)
            for f in forbidden:
                updates[f] = DELETE_FIELD

            floor_key = f"{provider}_token_envelope_required"
            floor_present = floor_key in d_data
            floor_val = d_data.get(floor_key)
            has_access_dict = type(d_data.get(f"{provider}_access_token")) is dict
            has_refresh_dict = type(d_data.get(f"{provider}_refresh_token")) is dict
            has_dict_cred = has_access_dict or has_refresh_dict

            should_require_floor = False
            if floor_present:
                if type(floor_val) is not bool or floor_val is True or has_dict_cred:
                    should_require_floor = True
            elif has_dict_cred:
                should_require_floor = True

            if should_require_floor:
                updates[floor_key] = True
                expected_env_req_box[0] = True
            else:
                if floor_present and floor_val is False:
                    expected_env_req_box[0] = False
                else:
                    expected_env_req_box[0] = _FLOOR_ABSENT

            if provider == "jobber":
                updates["jobber_lead_capture_enabled"] = False
            elif provider == "google_calendar":
                updates["google_calendar_scope"] = DELETE_FIELD

            final_disconnected_at_box[0] = server_now

            audit_doc_id = format_audit_doc_id(
                contractor_id=valid_cid,
                provider=provider,
                generation=tombstone_gen,
                action="credentials_deleted",
            )
            outbox_doc_id = format_outbox_doc_id(
                contractor_id=valid_cid,
                provider=provider,
                generation=tombstone_gen,
            )

            # Pre-read deterministic documents: must NOT already exist!
            audit_ref = db.collection(AUDIT_COLLECTION).document(audit_doc_id)
            outbox_ref = db.collection(REVOCATION_OUTBOX_COLLECTION).document(outbox_doc_id)
            audit_snap = _get_doc_snapshot_in_txn(audit_ref, transaction)
            outbox_snap = _get_doc_snapshot_in_txn(outbox_ref, transaction)
            if getattr(audit_snap, "exists", False) or getattr(outbox_snap, "exists", False):
                from google.api_core.exceptions import Aborted
                raise Aborted("Deterministic audit/outbox document already exists for new generation")

            transaction.update(doc_ref, updates)

            disp = DISPOSITION_EXECUTED if (is_connected or d_data.get(f"{provider}_connected") is True) else DISPOSITION_PARTIAL_RECONCILED

            audit_data = build_disconnect_audit_event(
                contractor_id=valid_cid,
                provider=provider,
                generation=tombstone_gen,
                lifecycle_epoch=next_epoch,
                actor=actor,
                reason=reason or "contractor_initiated_disconnect",
                credential_deletion_disposition=disp,
                revocation_status=initial_status,
                revocation_completed_at=rev_completed_at,
                timestamp=server_now,
            )
            outbox_data = build_disconnect_outbox_record(
                contractor_id=valid_cid,
                provider=provider,
                generation=tombstone_gen,
                lifecycle_epoch=next_epoch,
                status=initial_status,
                claim_id=claim_to_set,
                audit_finalized=is_finalized,
                audit_finalized_at=finalized_at,
                created_at=server_now,
                updated_at=server_now,
                credential_deletion_disposition=disp,
            )
            validate_disconnect_lifecycle_pair(audit_data, outbox_data)
            transaction.create(audit_ref, audit_data)
            transaction.create(outbox_ref, outbox_data)

            disposition_box[0] = disp
            revocation_status_box[0] = initial_status
            claim_id_box[0] = claim_to_set
            access_token_for_revoke_box[0] = token_to_grant
            audit_finalized_box[0] = is_finalized
            created_at_box[0] = server_now
            updated_at_box[0] = server_now
            audit_event_id_box[0] = audit_doc_id
            outbox_id_box[0] = outbox_doc_id
            audit_event_box[0] = audit_data
            outbox_event_box[0] = outbox_data
            body_prepared_box[0] = True

        else:
            # Already disconnected clean tombstone!
            gen = current_gen
            epoch = current_epoch
            audit_doc_id = format_audit_doc_id(contractor_id=valid_cid, provider=provider, generation=gen, action="credentials_deleted")
            outbox_doc_id = format_outbox_doc_id(contractor_id=valid_cid, provider=provider, generation=gen)

            outbox_ref = db.collection(REVOCATION_OUTBOX_COLLECTION).document(outbox_doc_id)
            audit_ref = db.collection(AUDIT_COLLECTION).document(audit_doc_id)
            outbox_snap = _get_doc_snapshot_in_txn(outbox_ref, transaction)
            audit_snap = _get_doc_snapshot_in_txn(audit_ref, transaction)
            outbox_exists = getattr(outbox_snap, "exists", False)
            audit_exists = getattr(audit_snap, "exists", False)

            floor_key = f"{provider}_token_envelope_required"
            floor_val = d_data.get(floor_key) if (floor_key in d_data and type(d_data[floor_key]) is bool) else (_FLOOR_ABSENT if floor_key not in d_data else True)
            disc_at_val = d_data[f"{provider}_disconnected_at"]

            if outbox_exists and audit_exists:
                outbox_data = outbox_snap.to_dict()
                audit_data = audit_snap.to_dict()
                try:
                    val_a, val_o = validate_disconnect_lifecycle_pair(
                        audit_data,
                        outbox_data,
                        expected_contractor_id=valid_cid,
                        expected_provider=provider,
                        expected_generation=gen,
                        expected_lifecycle_epoch=epoch,
                        expected_audit_id=audit_doc_id,
                        expected_outbox_id=outbox_doc_id,
                    )
                except Exception:
                    raise IntegrationTokenCASConflict("Incoherent existing audit/outbox pair for already disconnected contractor") from None

                disposition_box[0] = "already_disconnected"
                tombstone_gen_box[0] = gen
                next_epoch_box[0] = epoch
                final_disconnected_at_box[0] = disc_at_val
                expected_env_req_box[0] = floor_val
                audit_event_id_box[0] = audit_doc_id
                outbox_id_box[0] = outbox_doc_id
                revocation_status_box[0] = val_o["status"]
                claim_id_box[0] = None  # Contender / repeat gets ZERO claim ownership!
                access_token_for_revoke_box[0] = None  # ZERO token!
                audit_finalized_box[0] = val_o["audit_finalized"]
                created_at_box[0] = val_o["created_at"]
                updated_at_box[0] = val_o["updated_at"]
                audit_event_box[0] = val_a
                outbox_event_box[0] = val_o
                body_prepared_box[0] = True

            elif (not outbox_exists) and (not audit_exists):
                # Legacy reconciliation: atomically create both
                outbox_data = build_disconnect_outbox_record(
                    contractor_id=valid_cid,
                    provider=provider,
                    generation=gen,
                    lifecycle_epoch=epoch,
                    status=REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE,
                    claim_id=None,
                    audit_finalized=True,
                    audit_finalized_at=server_now,
                    created_at=server_now,
                    updated_at=server_now,
                    credential_deletion_disposition=DISPOSITION_LEGACY_RECONCILED,
                )
                audit_data = build_disconnect_audit_event(
                    contractor_id=valid_cid,
                    provider=provider,
                    generation=gen,
                    lifecycle_epoch=epoch,
                    actor="system_reconciliation" if actor == "contractor_api" else actor,
                    reason=reason or "legacy_reconciliation",
                    credential_deletion_disposition=DISPOSITION_LEGACY_RECONCILED,
                    revocation_status=REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE,
                    revocation_completed_at=server_now,
                    timestamp=server_now,
                )
                validate_disconnect_lifecycle_pair(audit_data, outbox_data)
                transaction.create(outbox_ref, outbox_data)
                transaction.create(audit_ref, audit_data)

                disposition_box[0] = DISPOSITION_LEGACY_RECONCILED
                tombstone_gen_box[0] = gen
                next_epoch_box[0] = epoch
                final_disconnected_at_box[0] = disc_at_val
                expected_env_req_box[0] = floor_val
                audit_event_id_box[0] = audit_doc_id
                outbox_id_box[0] = outbox_doc_id
                revocation_status_box[0] = REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE
                claim_id_box[0] = None
                access_token_for_revoke_box[0] = None
                audit_finalized_box[0] = True
                created_at_box[0] = server_now
                updated_at_box[0] = server_now
                audit_event_box[0] = audit_data
                outbox_event_box[0] = outbox_data
                body_prepared_box[0] = True

            elif outbox_exists and not audit_exists:
                outbox_data = outbox_snap.to_dict()
                try:
                    val_o = validate_outbox_record(
                        outbox_data,
                        expected_contractor_id=valid_cid,
                        expected_provider=provider,
                        expected_generation=gen,
                        expected_lifecycle_epoch=epoch,
                        expected_outbox_id=outbox_doc_id,
                    )
                except Exception as exc:
                    raise IntegrationTokenCASConflict(f"Existing outbox record invalid: {exc}") from exc

                # Safe audit synthesis is permitted ONLY for exact finalized terminal outbox
                if val_o["audit_finalized"] is not True or val_o["status"] not in TERMINAL_REVOCATION_STATUSES:
                    raise IntegrationTokenCASConflict("Cannot derive audit record from unfinalized or started outbox record")

                audit_data = build_disconnect_audit_event(
                    contractor_id=valid_cid,
                    provider=provider,
                    generation=gen,
                    lifecycle_epoch=epoch,
                    actor="system_reconciliation" if actor == "contractor_api" else actor,
                    reason=reason or "legacy_reconciliation",
                    credential_deletion_disposition=val_o.get("credential_deletion_disposition", DISPOSITION_LEGACY_RECONCILED),
                    revocation_status=val_o["status"],
                    revocation_completed_at=val_o["updated_at"],
                    timestamp=val_o["created_at"],
                )
                validate_disconnect_lifecycle_pair(audit_data, val_o)
                transaction.create(audit_ref, audit_data)

                disposition_box[0] = DISPOSITION_LEGACY_RECONCILED
                tombstone_gen_box[0] = gen
                next_epoch_box[0] = epoch
                final_disconnected_at_box[0] = disc_at_val
                expected_env_req_box[0] = floor_val
                audit_event_id_box[0] = audit_doc_id
                outbox_id_box[0] = outbox_doc_id
                revocation_status_box[0] = val_o["status"]
                claim_id_box[0] = None
                access_token_for_revoke_box[0] = None
                audit_finalized_box[0] = True
                created_at_box[0] = val_o["created_at"]
                updated_at_box[0] = val_o["updated_at"]
                audit_event_box[0] = audit_data
                outbox_event_box[0] = val_o
                body_prepared_box[0] = True

            else:
                # Lone audit record exists without outbox: impossible to reconstruct outbox unambiguously -> FAIL CLOSED
                raise IntegrationTokenCASConflict("Cannot reconstruct outbox from lone audit record")

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _disconnect_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenEnvelopeError):
        raise
    except Exception as exc:
        if body_prepared_box[0]:
            try:
                # Ambiguous commit recovery check using strict complete postcondition
                _verify_complete_disconnect_postcondition(
                    doc_ref,
                    contractor_id=valid_cid,
                    provider=provider,
                    expected_generation=tombstone_gen_box[0],
                    expected_lifecycle_epoch=next_epoch_box[0],
                    expected_disconnected_at=final_disconnected_at_box[0],
                    expected_floor=expected_env_req_box[0],
                    db=db,
                    outbox_id=outbox_id_box[0],
                    expected_outbox=outbox_event_box[0],
                    audit_id=audit_event_id_box[0],
                    expected_audit=audit_event_box[0],
                )
                return DisconnectProviderResult(
                    contractor_id=valid_cid,
                    provider=provider,
                    generation=tombstone_gen_box[0],
                    lifecycle_epoch=next_epoch_box[0],
                    audit_id=audit_event_id_box[0],
                    outbox_id=outbox_id_box[0],
                    credential_deletion=disposition_box[0],
                    revocation_status=revocation_status_box[0],
                    claim_id=claim_id_box[0],
                    access_token_for_revocation=access_token_for_revoke_box[0],
                    audit_finalized=audit_finalized_box[0],
                    created_at=created_at_box[0],
                    updated_at=updated_at_box[0],
                    expected_disconnected_at=final_disconnected_at_box[0],
                    expected_floor=expected_env_req_box[0],
                )
            except Exception:
                pass
        from google.api_core.exceptions import Aborted, AlreadyExists
        if isinstance(exc, (AlreadyExists, Aborted)):
            raise IntegrationTokenCASConflict("Deterministic audit/outbox document already exists for new generation") from None
        raise IntegrationTokenCASConflict("Disconnect transaction failed with ambiguous state") from None

    # Postcondition verification for all successful returns
    _verify_complete_disconnect_postcondition(
        doc_ref,
        contractor_id=valid_cid,
        provider=provider,
        expected_generation=tombstone_gen_box[0],
        expected_lifecycle_epoch=next_epoch_box[0],
        expected_disconnected_at=final_disconnected_at_box[0],
        expected_floor=expected_env_req_box[0],
        db=db,
        outbox_id=outbox_id_box[0],
        expected_outbox=outbox_event_box[0],
        audit_id=audit_event_id_box[0],
        expected_audit=audit_event_box[0],
    )

    return DisconnectProviderResult(
        contractor_id=valid_cid,
        provider=provider,
        generation=tombstone_gen_box[0],
        lifecycle_epoch=next_epoch_box[0],
        audit_id=audit_event_id_box[0],
        outbox_id=outbox_id_box[0],
        credential_deletion=disposition_box[0],
        revocation_status=revocation_status_box[0],
        claim_id=claim_id_box[0],
        access_token_for_revocation=access_token_for_revoke_box[0],
        audit_finalized=audit_finalized_box[0],
        created_at=created_at_box[0],
        updated_at=updated_at_box[0],
        expected_disconnected_at=final_disconnected_at_box[0],
        expected_floor=expected_env_req_box[0],
    )


async def record_revocation_outcome_cas(
    *,
    contractor_id: str,
    provider: str,
    outbox_id: str,
    claim_id: str,
    outcome_status: str,
    expected_generation: int,
    expected_lifecycle_epoch: int,
    db: Any = None,
) -> dict[str, Any]:
    """Claim-bound CAS transitioning provider_request_started outbox to a terminal outcome."""
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

    if type(outbox_id) is not str or len(outbox_id) == 0:
        raise IntegrationTokenEnvelopeError("Invalid outbox_id")

    if type(claim_id) is not str or len(claim_id) == 0:
        raise IntegrationTokenEnvelopeError("Invalid claim_id")

    if type(outcome_status) is not str or outcome_status not in TERMINAL_REVOCATION_STATUSES:
        raise IntegrationTokenEnvelopeError(f"Invalid outcome_status: {outcome_status}")

    if type(expected_generation) is not int or type(expected_generation) is bool or not (0 <= expected_generation <= MAX_KEY_VERSION):
        raise IntegrationTokenEnvelopeError(f"Invalid expected_generation: {expected_generation}")
    if type(expected_lifecycle_epoch) is not int or type(expected_lifecycle_epoch) is bool or not (0 <= expected_lifecycle_epoch <= MAX_KEY_VERSION):
        raise IntegrationTokenEnvelopeError(f"Invalid expected_lifecycle_epoch: {expected_lifecycle_epoch}")

    audit_id = format_audit_doc_id(contractor_id=valid_cid, provider=provider, generation=expected_generation, action="credentials_deleted")
    outbox_ref = db.collection(REVOCATION_OUTBOX_COLLECTION).document(outbox_id)
    audit_ref = db.collection(AUDIT_COLLECTION).document(audit_id)
    updated_outbox_box: list[dict[str, Any]] = [{}]

    @transactional
    def _outcome_txn(transaction):
        updated_outbox_box[0] = {}
        outbox_snap = _get_doc_snapshot_in_txn(outbox_ref, transaction)
        audit_snap = _get_doc_snapshot_in_txn(audit_ref, transaction)
        if not getattr(outbox_snap, "exists", False) or not getattr(audit_snap, "exists", False):
            raise IntegrationTokenCASConflict("Outbox or audit record not found")

        try:
            val_a, val_o = validate_disconnect_lifecycle_pair(
                audit_snap.to_dict(),
                outbox_snap.to_dict(),
                expected_contractor_id=valid_cid,
                expected_provider=provider,
                expected_generation=expected_generation,
                expected_lifecycle_epoch=expected_lifecycle_epoch,
                expected_audit_id=audit_id,
                expected_outbox_id=outbox_id,
            )
        except Exception as exc:
            raise IntegrationTokenCASConflict(f"Incoherent audit/outbox pair before outcome recording: {exc}") from exc

        # Claim MUST match durable claim (constant non-secret diagnostic)
        if val_o.get("claim_id") != claim_id:
            raise IntegrationTokenCASConflict("Claim ID mismatch on outbox record")

        # Idempotent match for the exact same outcome
        if val_o["status"] == outcome_status:
            updated_outbox_box[0] = dict(val_o)
            return

        if val_o["status"] != REVOCATION_STATUS_REQUEST_STARTED:
            raise IntegrationTokenCASConflict(f"Outbox already in terminal status {val_o['status']}")

        server_now = _extract_snapshot_server_time(outbox_snap)
        server_now = max(server_now, val_o["created_at"])

        updates = {
            "status": outcome_status,
            "updated_at": server_now,
        }
        transaction.update(outbox_ref, updates)
        new_data = dict(val_o)
        new_data.update(updates)
        updated_outbox_box[0] = new_data

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _outcome_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenEnvelopeError):
        raise
    except Exception:
        # Ambiguous commit recovery check
        try:
            o_snap = outbox_ref.get()
            a_snap = audit_ref.get()
            if getattr(o_snap, "exists", False) and getattr(a_snap, "exists", False):
                val_a, val_o = validate_disconnect_lifecycle_pair(
                    a_snap.to_dict(),
                    o_snap.to_dict(),
                    expected_contractor_id=valid_cid,
                    expected_provider=provider,
                    expected_generation=expected_generation,
                    expected_lifecycle_epoch=expected_lifecycle_epoch,
                    expected_doc_id=outbox_id,
                )
                if val_o.get("claim_id") == claim_id and val_o.get("status") == outcome_status:
                    return val_o
        except Exception:
            pass
        raise IntegrationTokenCASConflict("Outcome transaction failed with ambiguous state") from None

    _verify_outbox_postcondition(db, outbox_id, updated_outbox_box[0])
    return updated_outbox_box[0]


async def finalize_revocation_audit_cas(
    *,
    contractor_id: str,
    provider: str,
    outbox_id: str,
    expected_generation: int,
    expected_lifecycle_epoch: int,
    db: Any = None,
) -> bool:
    """Audit-finalization CAS copying terminal outbox status to lifecycle audit and marking outbox finalized."""
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

    if type(outbox_id) is not str or len(outbox_id) == 0:
        raise IntegrationTokenEnvelopeError("Invalid outbox_id")

    if type(expected_generation) is not int or type(expected_generation) is bool or not (0 <= expected_generation <= MAX_KEY_VERSION):
        raise IntegrationTokenEnvelopeError(f"Invalid expected_generation: {expected_generation}")
    if type(expected_lifecycle_epoch) is not int or type(expected_lifecycle_epoch) is bool or not (0 <= expected_lifecycle_epoch <= MAX_KEY_VERSION):
        raise IntegrationTokenEnvelopeError(f"Invalid expected_lifecycle_epoch: {expected_lifecycle_epoch}")

    audit_id = format_audit_doc_id(contractor_id=valid_cid, provider=provider, generation=expected_generation, action="credentials_deleted")
    outbox_ref = db.collection(REVOCATION_OUTBOX_COLLECTION).document(outbox_id)
    audit_ref = db.collection(AUDIT_COLLECTION).document(audit_id)
    expected_outbox_box: list[dict[str, Any]] = [{}]
    expected_audit_box: list[dict[str, Any]] = [{}]

    @transactional
    def _finalize_txn(transaction):
        expected_outbox_box[0] = {}
        expected_audit_box[0] = {}

        outbox_snap = _get_doc_snapshot_in_txn(outbox_ref, transaction)
        audit_snap = _get_doc_snapshot_in_txn(audit_ref, transaction)
        if not getattr(outbox_snap, "exists", False) or not getattr(audit_snap, "exists", False):
            raise IntegrationTokenCASConflict("Outbox or audit record not found for finalization")

        try:
            val_a, val_o = validate_disconnect_lifecycle_pair(
                audit_snap.to_dict(),
                outbox_snap.to_dict(),
                expected_contractor_id=valid_cid,
                expected_provider=provider,
                expected_generation=expected_generation,
                expected_lifecycle_epoch=expected_lifecycle_epoch,
                expected_audit_id=audit_id,
                expected_outbox_id=outbox_id,
            )
        except Exception as exc:
            raise IntegrationTokenCASConflict(f"Incoherent audit/outbox pair before finalization: {exc}") from exc

        if val_o["status"] not in TERMINAL_REVOCATION_STATUSES:
            raise IntegrationTokenCASConflict(f"Cannot finalize non-terminal outbox status {val_o['status']}")

        if val_o.get("audit_finalized") is True:
            expected_outbox_box[0] = dict(val_o)
            expected_audit_box[0] = dict(val_a)
            return

        outcome_ts = val_o["updated_at"]  # Canonical terminal outbox timestamp!
        server_now = _extract_snapshot_server_time(outbox_snap)
        server_now = max(server_now, outcome_ts)

        terminal_status = val_o["status"]

        audit_updates = {
            "revocation_status": terminal_status,
            "revocation_completed_at": outcome_ts,
        }
        transaction.update(audit_ref, audit_updates)
        new_audit = dict(val_a)
        new_audit.update(audit_updates)
        expected_audit_box[0] = new_audit

        outbox_updates = {
            "audit_finalized": True,
            "audit_finalized_at": server_now,
        }
        transaction.update(outbox_ref, outbox_updates)
        new_outbox = dict(val_o)
        new_outbox.update(outbox_updates)
        expected_outbox_box[0] = new_outbox

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _finalize_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenEnvelopeError):
        raise
    except Exception:
        # Ambiguous commit recovery check
        try:
            outbox_snap = outbox_ref.get()
            audit_snap = audit_ref.get()
            if getattr(outbox_snap, "exists", False) and getattr(audit_snap, "exists", False):
                o_data = outbox_snap.to_dict()
                a_data = audit_snap.to_dict()
                validate_disconnect_lifecycle_pair(
                    a_data,
                    o_data,
                    expected_contractor_id=valid_cid,
                    expected_provider=provider,
                    expected_generation=expected_generation,
                    expected_lifecycle_epoch=expected_lifecycle_epoch,
                    expected_doc_id=outbox_id,
                )
                if o_data.get("audit_finalized") is True:
                    return True
        except Exception:
            pass
        raise IntegrationTokenCASConflict("Finalization transaction failed with ambiguous state") from None

    _verify_audit_postcondition(db, audit_id, expected_audit_box[0])
    _verify_outbox_postcondition(db, outbox_id, expected_outbox_box[0])
    return True


async def disconnect_and_revoke_provider_orchestration(
    *,
    contractor_id: str,
    provider: str,
    actor: str = "contractor_api",
    reason: str | None = None,
    candidate_claim_id: str | None = None,
    db: Any = None,
    http_client: Any | None = None,
) -> dict[str, Any]:
    """Orchestrate disconnect CAS, at-most-once HTTP revocation, and audit finalization."""
    import httpx

    from app.config import settings

    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            raise IntegrationTokenEnvelopeError("Database unavailable") from None

    if db is None:
        raise IntegrationTokenEnvelopeError("Database unavailable")

    disc_res = await disconnect_provider_envelope_cas(
        contractor_id=contractor_id,
        provider=provider,
        actor=actor,
        reason=reason,
        candidate_claim_id=candidate_claim_id,
        db=db,
    )

    attempted_http = False
    durable_status = disc_res.revocation_status
    is_finalized = disc_res.audit_finalized
    finalization_attempted = False

    if disc_res.claim_id and disc_res.access_token_for_revocation:
        attempted_http = True
        outcome_status = REVOCATION_STATUS_TRANSPORT_ERROR
        try:
            if provider == "jobber":
                if http_client is not None:
                    resp = await http_client.post(
                        "https://api.getjobber.com/api/oauth/revoke",
                        data={
                            "token": disc_res.access_token_for_revocation,
                            "client_id": settings.jobber_client_id,
                            "client_secret": settings.jobber_client_secret,
                        },
                        timeout=5.0,
                    )
                else:
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            "https://api.getjobber.com/api/oauth/revoke",
                            data={
                                "token": disc_res.access_token_for_revocation,
                                "client_id": settings.jobber_client_id,
                                "client_secret": settings.jobber_client_secret,
                            },
                            timeout=5.0,
                        )
                if resp.status_code == 200:
                    outcome_status = REVOCATION_STATUS_CONFIRMED
                else:
                    outcome_status = REVOCATION_STATUS_REJECTED

            elif provider == "google_calendar":
                if http_client is not None:
                    resp = await http_client.post(
                        "https://oauth2.googleapis.com/revoke",
                        params={"token": disc_res.access_token_for_revocation},
                        timeout=5.0,
                    )
                else:
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            "https://oauth2.googleapis.com/revoke",
                            params={"token": disc_res.access_token_for_revocation},
                            timeout=5.0,
                        )
                if resp.status_code in (200, 204):
                    outcome_status = REVOCATION_STATUS_CONFIRMED
                else:
                    outcome_status = REVOCATION_STATUS_REJECTED
        except Exception:
            outcome_status = REVOCATION_STATUS_TRANSPORT_ERROR

        try:
            outbox_data = await record_revocation_outcome_cas(
                contractor_id=disc_res.contractor_id,
                provider=provider,
                outbox_id=disc_res.outbox_id,
                claim_id=disc_res.claim_id,
                outcome_status=outcome_status,
                expected_generation=disc_res.generation,
                expected_lifecycle_epoch=disc_res.lifecycle_epoch,
                db=db,
            )
            durable_status = outbox_data.get("status", outcome_status)
        except Exception:
            # Independent reread of BOTH outbox and audit records: fail closed unless durable terminal truth is confirmed
            o_snap = db.collection(REVOCATION_OUTBOX_COLLECTION).document(disc_res.outbox_id).get()
            a_snap = db.collection(AUDIT_COLLECTION).document(disc_res.audit_id).get()
            if not getattr(o_snap, "exists", False) or not getattr(a_snap, "exists", False):
                raise IntegrationTokenCASConflict("Revocation outcome persistence unconfirmed after HTTP request") from None
            try:
                val_a, val_o = validate_disconnect_lifecycle_pair(
                    a_snap.to_dict(),
                    o_snap.to_dict(),
                    expected_contractor_id=disc_res.contractor_id,
                    expected_provider=provider,
                    expected_generation=disc_res.generation,
                    expected_lifecycle_epoch=disc_res.lifecycle_epoch,
                    expected_audit_id=disc_res.audit_id,
                    expected_outbox_id=disc_res.outbox_id,
                )
            except Exception:
                raise IntegrationTokenCASConflict("Revocation lifecycle pair invalid after HTTP request") from None

            if val_o.get("status") in TERMINAL_REVOCATION_STATUSES and val_o.get("claim_id") == disc_res.claim_id:
                durable_status = val_o["status"]
            else:
                raise IntegrationTokenCASConflict("Revocation outcome persistence unconfirmed after HTTP request") from None

        if durable_status in TERMINAL_REVOCATION_STATUSES:
            try:
                finalization_attempted = True
                await finalize_revocation_audit_cas(
                    contractor_id=disc_res.contractor_id,
                    provider=provider,
                    outbox_id=disc_res.outbox_id,
                    expected_generation=disc_res.generation,
                    expected_lifecycle_epoch=disc_res.lifecycle_epoch,
                    db=db,
                )
            except Exception:
                pass

    else:
        # Zero provider HTTP
        if durable_status in TERMINAL_REVOCATION_STATUSES and not is_finalized:
            try:
                finalization_attempted = True
                await finalize_revocation_audit_cas(
                    contractor_id=disc_res.contractor_id,
                    provider=provider,
                    outbox_id=disc_res.outbox_id,
                    expected_generation=disc_res.generation,
                    expected_lifecycle_epoch=disc_res.lifecycle_epoch,
                    db=db,
                )
            except Exception:
                pass

    # Final independent proof of contractor tombstone state
    c_snap = db.collection("contractors").document(disc_res.contractor_id).get()
    if not getattr(c_snap, "exists", False):
        raise IntegrationTokenCASConflict("Contractor document not found during final orchestration verification")
    c_data = c_snap.to_dict()
    if not is_durable_provider_tombstone(c_data, provider, disc_res.contractor_id):
        raise IntegrationTokenCASConflict("Contractor document is not a durable tombstone during final verification")
    if c_data.get(f"{provider}_generation") != disc_res.generation:
        raise IntegrationTokenCASConflict("Contractor generation mismatch during final verification")
    if c_data.get(f"{provider}_lifecycle_epoch") != disc_res.lifecycle_epoch:
        raise IntegrationTokenCASConflict("Contractor lifecycle epoch mismatch during final verification")
    if c_data.get(f"{provider}_disconnected_at") != disc_res.expected_disconnected_at:
        raise IntegrationTokenCASConflict("Contractor disconnected_at mismatch during final verification")
    floor_key = f"{provider}_token_envelope_required"
    if disc_res.expected_floor is _FLOOR_ABSENT:
        if floor_key in c_data:
            raise IntegrationTokenCASConflict(f"Contractor expected floor absence, found {c_data[floor_key]!r}")
    elif disc_res.expected_floor is True or disc_res.expected_floor is False:
        if floor_key not in c_data or c_data[floor_key] is not disc_res.expected_floor:
            raise IntegrationTokenCASConflict("Contractor floor state mismatch during final verification")
    if provider == "jobber" and c_data.get("jobber_lead_capture_enabled") is not False:
        raise IntegrationTokenCASConflict("Jobber lead capture not disabled on contractor tombstone")
    if provider == "google_calendar" and c_data.get("google_calendar_scope"):
        raise IntegrationTokenCASConflict("Google calendar scope not removed on contractor tombstone")

    # Final independent verification of durable lifecycle pair
    a_snap = db.collection(AUDIT_COLLECTION).document(disc_res.audit_id).get()
    o_snap = db.collection(REVOCATION_OUTBOX_COLLECTION).document(disc_res.outbox_id).get()
    if not getattr(a_snap, "exists", False) or not getattr(o_snap, "exists", False):
        raise IntegrationTokenCASConflict("Lifecycle audit/outbox records unreadable during final verification")
    val_a, val_o = validate_disconnect_lifecycle_pair(
        a_snap.to_dict(),
        o_snap.to_dict(),
        expected_contractor_id=disc_res.contractor_id,
        expected_provider=provider,
        expected_generation=disc_res.generation,
        expected_lifecycle_epoch=disc_res.lifecycle_epoch,
        expected_audit_id=disc_res.audit_id,
        expected_outbox_id=disc_res.outbox_id,
    )
    is_finalized = val_o["audit_finalized"]
    durable_status = val_o["status"]
    if durable_status not in TERMINAL_REVOCATION_STATUSES:
        raise IntegrationTokenCASConflict(f"Revocation status {durable_status} is non-terminal during final verification")

    return {
        "status": "disconnected",
        "contractor_id": disc_res.contractor_id,
        "provider": provider,
        "generation": disc_res.generation,
        "lifecycle_epoch": disc_res.lifecycle_epoch,
        "audit_id": disc_res.audit_id,
        "outbox_id": disc_res.outbox_id,
        "credential_deletion": {
            "status": disc_res.credential_deletion,
            "attempted_by_this_request": disc_res.credential_deletion in (DISPOSITION_EXECUTED, DISPOSITION_PARTIAL_RECONCILED),
        },
        "provider_revocation": {
            "status": durable_status,
            "attempted": attempted_http,
            "attempted_by_this_request": attempted_http,
        },
        "audit_finalization": {
            "status": "finalized" if is_finalized else "pending",
            "finalized": is_finalized,
            "attempted_by_this_request": finalization_attempted,
        },
        "revocation_status": durable_status,
    }


async def disconnect_provider_cas(
    *,
    contractor_id: str,
    provider: str,
    actor: str = "contractor_api",
    reason: str | None = None,
    db: Any = None,
) -> tuple[int, str | None, str]:
    """Atomically disconnect a provider: advances generation and lifecycle epoch, tombstones credentials, records audit event."""
    res = await disconnect_provider_envelope_cas(
        contractor_id=contractor_id,
        provider=provider,
        actor=actor,
        reason=reason,
        db=db,
    )
    return res.generation, res.access_token_for_revocation, res.audit_id


async def connect_provider_cas(
    *,
    contractor_id: str,
    provider: str,
    access_token: str,
    refresh_token: str,
    observed_generation: int | None = None,
    observed_lifecycle_epoch: int | None = None,
    observed_access_raw: Any = None,
    observed_refresh_raw: Any = None,
    claim_id: str | None = None,
    expires_at: float | None = None,
    expires_in: float | None = None,
    scope: str | None = None,
    extra_updates: dict[str, Any] | None = None,
    actor: str = "oauth_state",
    db: Any = None,
) -> tuple[dict[str, Any], int, str]:
    """Atomically connect/reconnect a provider: advances generation and lifecycle epoch, persists encrypted credentials, writes audit event."""
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

    if claim_id is not None:
        if type(claim_id) is not str or not _CANONICAL_STATE_REGEX.fullmatch(claim_id):
            raise IntegrationTokenEnvelopeError("Invalid claim_id")

    if observed_generation is not None:
        if (
            type(observed_generation) is not int
            or type(observed_generation) is bool
            or not (0 <= observed_generation <= MAX_KEY_VERSION)
        ):
            raise IntegrationTokenEnvelopeError("Invalid observed_generation")

    if observed_lifecycle_epoch is not None:
        if (
            type(observed_lifecycle_epoch) is not int
            or type(observed_lifecycle_epoch) is bool
            or not (0 <= observed_lifecycle_epoch <= MAX_KEY_VERSION)
        ):
            raise IntegrationTokenEnvelopeError("Invalid observed_lifecycle_epoch")

    if scope is not None:
        if provider == "google_calendar":
            ok_scope, norm_scope = validate_and_normalize_google_calendar_scope(scope, allow_none=False)
            if not ok_scope or norm_scope is None or type(norm_scope) is not str:
                raise IntegrationTokenEnvelopeError("Invalid google_calendar_scope")
        else:
            raise IntegrationTokenEnvelopeError("scope is not allowed for provider")

    if extra_updates is not None:
        if type(extra_updates) is not dict:
            raise IntegrationTokenEnvelopeError("extra_updates must be an exact dict")
        allowed = ALLOWED_EXTRA_UPDATES.get(provider, frozenset())
        for k, v in extra_updates.items():
            if type(k) is not str:
                raise IntegrationTokenEnvelopeError("extra_updates keys must be exact str")
            if k not in allowed:
                raise IntegrationTokenEnvelopeError("Disallowed field in extra_updates")
            if k == "jobber_lead_capture_enabled":
                if type(v) is not bool:
                    raise IntegrationTokenEnvelopeError("Invalid value for jobber_lead_capture_enabled")
            elif k == "google_calendar_scope":
                ok_scope, norm_scope = validate_and_normalize_google_calendar_scope(v, allow_none=False)
                if not ok_scope or norm_scope is None or type(norm_scope) is not str:
                    raise IntegrationTokenEnvelopeError("Invalid value for google_calendar_scope")

    doc_ref = db.collection("contractors").document(valid_cid)
    next_gen_box = [0]
    next_epoch_box = [0]
    final_connected_at_box: list[float] = [0.0]
    effective_expires_at_box: list[float | None] = [None]
    expected_env_req_box: list[Any] = [_UNCHECKED]
    updates_box: list[dict[str, Any]] = [{}]
    audit_event_id_box: list[str] = [""]
    audit_event_box: list[dict[str, Any]] = [{}]
    body_prepared_box = [False]

    @transactional
    def _connect_txn(transaction):
        next_gen_box[0] = 0
        next_epoch_box[0] = 0
        final_connected_at_box[0] = 0.0
        effective_expires_at_box[0] = None
        expected_env_req_box[0] = _UNCHECKED
        updates_box[0] = {}
        audit_event_id_box[0] = ""
        audit_event_box[0] = {}
        body_prepared_box[0] = False

        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            raise IntegrationTokenCASConflict("Contractor document not found")

        d_data = doc_snap.to_dict()
        if type(d_data) is not dict:
            raise IntegrationTokenCASConflict("Contractor document is not an exact dict")
        if d_data.get("active") is not True:
            raise IntegrationTokenCASConflict("Contractor document is not active")

        server_now = _extract_snapshot_server_time(doc_snap)

        lifecycle_ok, current_gen, current_epoch, lifecycle_present, lifecycle_err = parse_durable_lifecycle_counters(d_data, provider)
        if not lifecycle_ok:
            raise IntegrationTokenCASConflict("Invalid lifecycle metadata on contractor document")

        if observed_generation is not None:
            if current_gen != observed_generation:
                raise IntegrationTokenCASConflict("Generation conflict")
            if not _exact_raw_credential_equal(d_data.get(f"{provider}_access_token"), observed_access_raw):
                raise IntegrationTokenCASConflict("Stored access token credential mismatch")
            if not _exact_raw_credential_equal(d_data.get(f"{provider}_refresh_token"), observed_refresh_raw):
                raise IntegrationTokenCASConflict("Stored refresh token credential mismatch")

        if observed_lifecycle_epoch is not None:
            if current_epoch != observed_lifecycle_epoch:
                raise IntegrationTokenCASConflict("Lifecycle epoch conflict")

        intent_status, parsed_state, state_err = parse_provider_operation_intent(d_data, provider)
        if intent_status == "malformed":
            raise IntegrationTokenCASConflict(f"Malformed operation intent or quarantine state: {state_err}")

        stored_access = d_data.get(f"{provider}_access_token")
        stored_refresh = d_data.get(f"{provider}_refresh_token")

        if intent_status == "quarantined_reauthorizing":
            if claim_id is None:
                raise IntegrationTokenCASConflict("Quarantined provider record requires explicit claim_id for reauthorization")
            if parsed_state is None:
                raise IntegrationTokenCASConflict("Missing parsed reauthorization attempt")
            if parsed_state["id"] != claim_id:
                raise IntegrationTokenCASConflict("Reauthorization attempt claim_id mismatch")
            if parsed_state["phase"] != "provider_request_started":
                raise IntegrationTokenCASConflict(f"Reauthorization attempt in invalid phase {parsed_state['phase']} (must be provider_request_started)")
            if parsed_state["kind"] != "reconnect":
                raise IntegrationTokenCASConflict("Reauthorization attempt kind mismatch")
            if parsed_state["expires_at"] <= server_now:
                raise IntegrationTokenCASConflict("Reauthorization attempt expired")
            if parsed_state["generation"] != current_gen or parsed_state["lifecycle_epoch"] != current_epoch:
                raise IntegrationTokenCASConflict("Reauthorization attempt generation/epoch mismatch")
            computed_fp = compute_raw_credentials_fingerprint(stored_access, stored_refresh)
            if parsed_state["credentials_fingerprint"] != computed_fp:
                raise IntegrationTokenCASConflict("Reauthorization attempt credentials fingerprint mismatch")
        elif intent_status == "quarantined":
            raise IntegrationTokenCASConflict("Quarantined provider record requires active started reauthorization attempt")
        elif intent_status == "valid":
            if claim_id is None:
                raise IntegrationTokenCASConflict("Claimless connect rejected while operation intent is held")
            if parsed_state is None:
                raise IntegrationTokenCASConflict("Missing parsed operation intent")
            if parsed_state["id"] != claim_id:
                raise IntegrationTokenCASConflict("Connect operation intent ID mismatch on commit")
            if parsed_state["phase"] != "provider_request_started":
                raise IntegrationTokenCASConflict(f"Connect operation intent in invalid phase {parsed_state['phase']} on commit")
            if parsed_state["kind"] not in ("connect", "reconnect"):
                raise IntegrationTokenCASConflict(f"Connect operation intent kind mismatch (held {parsed_state['kind']})")
            if parsed_state["expires_at"] <= server_now:
                raise IntegrationTokenCASConflict("Connect operation intent expired on commit")
            if parsed_state["generation"] != current_gen or parsed_state["lifecycle_epoch"] != current_epoch:
                raise IntegrationTokenCASConflict("Connect operation intent generation/epoch mismatch")
            computed_fp = compute_raw_credentials_fingerprint(stored_access, stored_refresh)
            if not parsed_state.get("is_legacy") and parsed_state["credentials_fingerprint"] != computed_fp:
                raise IntegrationTokenCASConflict("Connect operation intent credentials fingerprint mismatch")
        elif intent_status == "absent":
            if claim_id is not None:
                raise IntegrationTokenCASConflict("Claimed connect provided but no operation intent is held on contractor document")

        next_gen = current_gen + 1
        if next_gen > MAX_KEY_VERSION:
            raise IntegrationTokenEnvelopeError("Generation overflow")
        next_gen_box[0] = next_gen

        next_epoch = current_epoch + 1
        if next_epoch > MAX_KEY_VERSION:
            raise IntegrationTokenEnvelopeError("Lifecycle epoch overflow")
        next_epoch_box[0] = next_epoch

        stored_access = d_data.get(f"{provider}_access_token")
        stored_refresh = d_data.get(f"{provider}_refresh_token")
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
            timestamp=server_now,
        )
        audit_data["lifecycle_epoch"] = next_epoch
        audit_event_box[0] = audit_data

        audit_ref = db.collection(AUDIT_COLLECTION).document(audit_doc_id)
        audit_snap = _get_doc_snapshot_in_txn(audit_ref, transaction)
        audit_exists = getattr(audit_snap, "exists", False)
        if audit_exists:
            raise IntegrationTokenCASConflict(f"Deterministic connect audit document already exists for new generation: {audit_doc_id}")

        env_req = d_data.get(f"{provider}_token_envelope_required")
        if f"{provider}_token_envelope_required" in d_data and type(env_req) is not bool:
            raise IntegrationTokenCASConflict("Malformed token_envelope_required flag on document")

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

        calculated_exp = None
        if expires_in is not None:
            valid_in = validate_token_expires_in(expires_in)
            if valid_in is not None:
                calculated_exp = server_now + valid_in
        elif expires_at is not None:
            calculated_exp = validate_token_expires_at(expires_at)
        effective_expires_at_box[0] = calculated_exp

        updates = {
            f"{provider}_connected": True,
            f"{provider}_generation": next_gen,
            f"{provider}_lifecycle_epoch": next_epoch,
            f"{provider}_connected_at": server_now,
            f"{provider}_disconnected_at": DELETE_FIELD,
            f"{provider}_token_refreshed_at": DELETE_FIELD,
            f"{provider}_access_token": final_access,
            f"{provider}_refresh_token": final_refresh,
            f"{provider}_refresh_outcome_unknown": DELETE_FIELD,
            f"{provider}_reauthorization_required": DELETE_FIELD,
        }
        for f in get_provider_operation_intent_keys(provider):
            updates[f] = DELETE_FIELD

        if calculated_exp is not None:
            updates[f"{provider}_token_expires_at"] = calculated_exp
        else:
            updates[f"{provider}_token_expires_at"] = DELETE_FIELD

        if write_format == "envelope":
            updates[f"{provider}_token_envelope_required"] = True
            expected_env_req_box[0] = True
        else:
            expected_env_req_box[0] = env_req

        if extra_updates:
            updates.update(extra_updates)

        if provider == "google_calendar":
            if scope is not None:
                ok_scope, norm_scope = validate_and_normalize_google_calendar_scope(scope, allow_none=False)
                if not ok_scope or norm_scope is None:
                    raise IntegrationTokenEnvelopeError("Invalid or reduced google_calendar_scope")
                updates["google_calendar_scope"] = norm_scope
            else:
                updates["google_calendar_scope"] = CANONICAL_GOOGLE_CALENDAR_SCOPE

        final_connected_at_box[0] = server_now
        updates_box[0] = updates
        body_prepared_box[0] = True

        transaction.create(audit_ref, audit_data)
        transaction.update(doc_ref, updates)

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _connect_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenEnvelopeError):
        raise
    except Exception:
        if body_prepared_box[0]:
            try:
                extra_post_fields = dict(extra_updates or {})
                if provider == "google_calendar":
                    extra_post_fields["google_calendar_scope"] = updates_box[0].get("google_calendar_scope")
                _verify_mutation_postcondition(
                    doc_ref,
                    expected_generation=next_gen_box[0],
                    expected_lifecycle_epoch=next_epoch_box[0],
                    expected_connected=True,
                    provider=provider,
                    expected_access_envelope=updates_box[0].get(f"{provider}_access_token"),
                    expected_refresh_envelope=updates_box[0].get(f"{provider}_refresh_token"),
                    expected_connected_at=final_connected_at_box[0],
                    expected_expires_at=effective_expires_at_box[0],
                    expected_extra_fields=extra_post_fields,
                    expected_envelope_required=expected_env_req_box[0],
                    deleted_fields=_build_deleted_claim_keys(provider, effective_expires_at_box[0]),
                )
                _verify_audit_postcondition(db, audit_event_id_box[0], audit_event_box[0])
                return updates_box[0], next_gen_box[0], audit_event_id_box[0]
            except Exception:
                pass
        raise IntegrationTokenCASConflict("Connect transaction failed with ambiguous state") from None

    # Postcondition verification
    extra_post_fields = dict(extra_updates or {})
    if provider == "google_calendar":
        extra_post_fields["google_calendar_scope"] = updates_box[0].get("google_calendar_scope")
    _verify_mutation_postcondition(
        doc_ref,
        expected_generation=next_gen_box[0],
        expected_lifecycle_epoch=next_epoch_box[0],
        expected_connected=True,
        provider=provider,
        expected_access_envelope=updates_box[0].get(f"{provider}_access_token"),
        expected_refresh_envelope=updates_box[0].get(f"{provider}_refresh_token"),
        expected_connected_at=final_connected_at_box[0],
        expected_expires_at=effective_expires_at_box[0],
        expected_extra_fields=extra_post_fields,
        expected_envelope_required=expected_env_req_box[0],
        deleted_fields=_build_deleted_claim_keys(provider, effective_expires_at_box[0]),
    )
    _verify_audit_postcondition(db, audit_event_id_box[0], audit_event_box[0])

    return updates_box[0], next_gen_box[0], audit_event_id_box[0]


async def create_oauth_state(
    *,
    db: Any,
    collection_name: str,
    state: str,
    contractor_id: str,
    provider: str,
    ttl_seconds: float = 600.0,
) -> dict[str, Any]:
    """Atomically generate and store an OAuth state document bound to contractor lifecycle and credentials."""
    if type(provider) is not str or provider not in OAUTH_PROVIDER_COLLECTIONS:
        raise HTTPException(status_code=400, detail="Invalid provider")

    if type(collection_name) is not str or collection_name != OAUTH_PROVIDER_COLLECTIONS[provider]:
        raise HTTPException(status_code=400, detail="Invalid OAuth state collection for provider")

    if type(state) is not str or not _CANONICAL_STATE_REGEX.fullmatch(state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state identifier")

    if (
        type(ttl_seconds) not in (int, float)
        or type(ttl_seconds) is bool
        or not math.isfinite(ttl_seconds)
        or not (1.0 <= float(ttl_seconds) <= 3600.0)
    ):
        raise HTTPException(status_code=400, detail="Invalid ttl_seconds: must be a finite float between 1.0 and 3600.0")

    valid_cid = validate_token_string(contractor_id, name="contractor_id")
    assert valid_cid is not None

    if db is None:
        raise HTTPException(status_code=500, detail="Database unavailable")

    contractor_ref = db.collection("contractors").document(valid_cid)
    state_ref = db.collection(collection_name).document(state)
    state_payload_box: list[dict[str, Any]] = [{}]

    @transactional
    def _create_state_txn(transaction):
        state_payload_box[0] = {}
        contractor_snap = _get_doc_snapshot_in_txn(contractor_ref, transaction)
        if not getattr(contractor_snap, "exists", False):
            raise HTTPException(status_code=404, detail="Contractor not found")
        c_data = contractor_snap.to_dict()
        if type(c_data) is not dict:
            raise HTTPException(status_code=500, detail="Contractor document is not an exact dict")
        if c_data.get("active") is not True:
            raise HTTPException(status_code=403, detail="Contractor is inactive")

        state_snap = _get_doc_snapshot_in_txn(state_ref, transaction)
        if getattr(state_snap, "exists", False):
            raise HTTPException(status_code=409, detail="OAuth state conflict: state already exists")

        server_now = _extract_snapshot_server_time(contractor_snap)

        lifecycle_ok, cur_gen, cur_epoch, lifecycle_present, lifecycle_err = parse_durable_lifecycle_counters(c_data, provider)
        if not lifecycle_ok:
            raise HTTPException(status_code=400, detail="Invalid contractor lifecycle metadata")

        raw_access = c_data.get(f"{provider}_access_token")
        raw_refresh = c_data.get(f"{provider}_refresh_token")
        try:
            fp = compute_raw_credentials_fingerprint(raw_access, raw_refresh)
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to compute credentials fingerprint") from None

        now_f = float(server_now)
        exp_f = float(server_now + float(ttl_seconds))
        payload = {
            "contractor_id": valid_cid,
            "provider": provider,
            "lifecycle_epoch": cur_epoch,
            "generation": cur_gen,
            "credentials_fingerprint": fp,
            "created_at": now_f,
            "expires_at": exp_f,
        }
        state_payload_box[0] = payload
        transaction.create(state_ref, payload)

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _create_state_txn(transaction))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to create OAuth state") from None

    # Postverify state document
    post_snap = state_ref.get()
    if not getattr(post_snap, "exists", False):
        raise HTTPException(status_code=500, detail="Failed to post-verify created OAuth state document")
    post_data = post_snap.to_dict()
    if type(post_data) is not dict:
        raise HTTPException(status_code=500, detail="Created OAuth state snapshot is not an exact dict")
    if not _exact_scalar_or_composite_equal(post_data, state_payload_box[0]):
        raise HTTPException(status_code=500, detail="Created OAuth state postcondition mismatch")

    return state_payload_box[0]


async def consume_oauth_state(
    *,
    db: Any,
    collection_name: str,
    state: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Atomically verify and consume an OAuth state document in a transaction.

    Performs ALL reads before any staged mutations. Atomically reserves connect intent
    on contractor before deleting state.
    """
    if type(collection_name) is not str or collection_name not in VALID_OAUTH_COLLECTIONS:
        raise HTTPException(status_code=400, detail="Invalid OAuth state collection")

    if type(state) is not str or not _CANONICAL_STATE_REGEX.fullmatch(state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state identifier")

    if db is None:
        raise HTTPException(status_code=500, detail="Database unavailable for state validation")

    state_ref = db.collection(collection_name).document(state)
    outcome_box: list[tuple[str, dict[str, Any], dict[str, Any]]] = [("uninitialized", {}, {})]

    @transactional
    def _consume_txn(transaction):
        outcome_box[0] = ("uninitialized", {}, {})

        # READ PHASE (all reads before any writes/deletes)
        state_snap = _get_doc_snapshot_in_txn(state_ref, transaction)
        if not getattr(state_snap, "exists", False):
            outcome_box[0] = ("not_found", {}, {})
            return

        data = state_snap.to_dict()
        cid = None
        c_snap = None
        c_ref = None
        if type(data) is dict:
            raw_cid = data.get("contractor_id")
            if type(raw_cid) is str:
                try:
                    valid_cid = validate_token_string(raw_cid, name="contractor_id")
                    if valid_cid is not None:
                        cid = valid_cid
                        c_ref = db.collection("contractors").document(valid_cid)
                        c_snap = _get_doc_snapshot_in_txn(c_ref, transaction)
                except Exception:
                    pass

        # ALL READS ARE NOW COMPLETED FOR THIS TRANSACTION.

        # VALIDATION AND STAGED WRITE PHASE
        if type(data) is not dict or not all(type(k) is str for k in data.keys()) or set(data.keys()) != OAUTH_STATE_KEYS:
            transaction.delete(state_ref)
            outcome_box[0] = ("malformed", {}, {})
            return

        provider = data.get("provider")
        if (
            type(provider) is not str
            or provider not in OAUTH_PROVIDER_COLLECTIONS
            or collection_name != OAUTH_PROVIDER_COLLECTIONS[provider]
        ):
            transaction.delete(state_ref)
            outcome_box[0] = ("malformed", {}, {})
            return

        epoch = data.get("lifecycle_epoch")
        if type(epoch) is not int or type(epoch) is bool or not (0 <= epoch <= MAX_KEY_VERSION):
            transaction.delete(state_ref)
            outcome_box[0] = ("malformed", {}, {})
            return

        gen = data.get("generation")
        if type(gen) is not int or type(gen) is bool or not (0 <= gen <= MAX_KEY_VERSION):
            transaction.delete(state_ref)
            outcome_box[0] = ("malformed", {}, {})
            return

        fp = data.get("credentials_fingerprint")
        if type(fp) is not str or not re.fullmatch(r"^[0-9a-f]{64}$", fp):
            transaction.delete(state_ref)
            outcome_box[0] = ("malformed", {}, {})
            return

        created_at = data.get("created_at")
        if type(created_at) is not float or not math.isfinite(created_at) or created_at <= 0.0:
            transaction.delete(state_ref)
            outcome_box[0] = ("malformed", {}, {})
            return

        exp = data.get("expires_at")
        if type(exp) is not float or not math.isfinite(exp) or exp <= 0.0:
            transaction.delete(state_ref)
            outcome_box[0] = ("invalid_expiration", {}, {})
            return

        if created_at >= exp or (exp - created_at) > 605.0:
            transaction.delete(state_ref)
            outcome_box[0] = ("malformed", {}, {})
            return

        try:
            server_now = _extract_snapshot_server_time(state_snap)
        except Exception:
            transaction.delete(state_ref)
            outcome_box[0] = ("malformed", {}, {})
            return

        if exp <= server_now:
            transaction.delete(state_ref)
            outcome_box[0] = ("expired", {}, {})
            return

        if c_snap is None or not getattr(c_snap, "exists", False) or c_ref is None or cid is None:
            transaction.delete(state_ref)
            outcome_box[0] = ("invalid_contractor", {}, {})
            return

        c_data = c_snap.to_dict()
        if type(c_data) is not dict or c_data.get("active") is not True:
            transaction.delete(state_ref)
            outcome_box[0] = ("invalid_contractor", {}, {})
            return

        c_lifecycle_ok, c_gen, c_epoch, _, c_lifecycle_err = parse_durable_lifecycle_counters(c_data, provider)
        if not c_lifecycle_ok or c_epoch != epoch or c_gen != gen:
            transaction.delete(state_ref)
            outcome_box[0] = ("lifecycle_mismatch", {}, {})
            return

        c_access = c_data.get(f"{provider}_access_token")
        c_refresh = c_data.get(f"{provider}_refresh_token")
        try:
            c_fp = compute_raw_credentials_fingerprint(c_access, c_refresh)
        except Exception:
            transaction.delete(state_ref)
            outcome_box[0] = ("lifecycle_mismatch", {}, {})
            return

        if c_fp != fp:
            transaction.delete(state_ref)
            outcome_box[0] = ("lifecycle_mismatch", {}, {})
            return

        # Check existing operation intent or quarantine on contractor using parse_provider_operation_intent
        intent_status, parsed_intent, intent_err = parse_provider_operation_intent(c_data, provider)
        if intent_status == "malformed":
            transaction.delete(state_ref)
            outcome_box[0] = ("lifecycle_mismatch", {}, {})
            return

        is_quarantined = (intent_status in ("quarantined", "quarantined_reauthorizing")) or (
            c_data.get(f"{provider}_reauthorization_required") is True
            and c_data.get(f"{provider}_refresh_outcome_unknown") is True
        )

        if is_quarantined:
            if intent_status == "quarantined_reauthorizing" and parsed_intent is not None:
                held_exp = parsed_intent["expires_at"]
                held_phase = parsed_intent["phase"]
                if held_phase == "provider_request_started" and held_exp > server_now:
                    # Active started reauthorization attempt blocks with zero HTTP
                    transaction.delete(state_ref)
                    outcome_box[0] = ("lifecycle_mismatch", {}, {})
                    return
        else:
            if intent_status == "valid" and parsed_intent is not None:
                held_exp = parsed_intent["expires_at"]
                held_phase = parsed_intent["phase"]
                if held_exp > server_now:
                    # Active intent of ANY kind blocks with zero HTTP
                    transaction.delete(state_ref)
                    outcome_box[0] = ("lifecycle_mismatch", {}, {})
                    return
                else:
                    # Expired intent
                    if held_phase == "reserved":
                        # Expired reserved may preempt safely
                        pass
                    elif held_phase == "provider_request_started":
                        # Expired provider_request_started becomes durable exact True/True quarantine, safely terminalizes state, zero HTTP
                        quarantine_updates = {
                            f"{provider}_reauthorization_required": True,
                            f"{provider}_refresh_outcome_unknown": True,
                        }
                        all_intent_keys = {f"{provider}_{k}" for k in OPERATION_INTENT_BASE_KEYS}
                        all_legacy_keys = {f"{provider}_{k}" for k in LEGACY_CLAIM_BASE_KEYS}
                        all_att_keys = {f"{provider}_{k}" for k in REAUTHORIZATION_ATTEMPT_BASE_KEYS}
                        for k in (all_intent_keys | all_legacy_keys | all_att_keys):
                            if k in c_data:
                                quarantine_updates[k] = DELETE_FIELD
                        transaction.update(c_ref, quarantine_updates)
                        transaction.delete(state_ref)
                        outcome_box[0] = ("lifecycle_mismatch", {}, {})
                        return

        c_floor = c_data.get(f"{provider}_token_envelope_required")
        if f"{provider}_token_envelope_required" in c_data and type(c_floor) is not bool:
            transaction.delete(state_ref)
            outcome_box[0] = ("malformed", {}, {})
            return

        claim_id = secrets.token_hex(16)
        attempt_expires_at = server_now + LEASE_DURATION_SECONDS

        if is_quarantined:
            intent_updates = {
                f"{provider}_reauthorization_attempt_id": claim_id,
                f"{provider}_reauthorization_attempt_kind": "reconnect",
                f"{provider}_reauthorization_attempt_phase": "reserved",
                f"{provider}_reauthorization_attempt_acquired_at": server_now,
                f"{provider}_reauthorization_attempt_expires_at": attempt_expires_at,
                f"{provider}_reauthorization_attempt_generation": c_gen,
                f"{provider}_reauthorization_attempt_lifecycle_epoch": c_epoch,
                f"{provider}_reauthorization_attempt_credentials_fingerprint": c_fp,
                f"{provider}_reauthorization_required": True,
                f"{provider}_refresh_outcome_unknown": True,
            }
            # Clean up any leftover ordinary intent or legacy keys
            all_intent_keys = {f"{provider}_{k}" for k in OPERATION_INTENT_BASE_KEYS}
            all_legacy_keys = {f"{provider}_{k}" for k in LEGACY_CLAIM_BASE_KEYS}
            for k in (all_intent_keys | all_legacy_keys):
                if k in c_data:
                    intent_updates[k] = DELETE_FIELD
        else:
            intent_updates = {
                f"{provider}_operation_intent_id": claim_id,
                f"{provider}_operation_intent_kind": "connect",
                f"{provider}_operation_intent_phase": "reserved",
                f"{provider}_operation_intent_acquired_at": server_now,
                f"{provider}_operation_intent_expires_at": attempt_expires_at,
                f"{provider}_operation_intent_generation": c_gen,
                f"{provider}_operation_intent_lifecycle_epoch": c_epoch,
            }
            if c_fp:
                intent_updates[f"{provider}_operation_intent_credentials_fingerprint"] = c_fp

            # Clean up any leftover attempt keys
            all_att_keys = {f"{provider}_{k}" for k in REAUTHORIZATION_ATTEMPT_BASE_KEYS}
            for k in all_att_keys:
                if k in c_data:
                    intent_updates[k] = DELETE_FIELD

        transaction.update(c_ref, intent_updates)
        transaction.delete(state_ref)

        contractor_observation = {
            "contractor_id": cid,
            "provider": provider,
            "generation": c_gen,
            "lifecycle_epoch": c_epoch,
            "observed_access_raw": c_access,
            "observed_refresh_raw": c_refresh,
            "envelope_required": c_floor,
            "claim_id": claim_id,
            "is_quarantined": is_quarantined,
        }
        outcome_box[0] = ("valid", data, contractor_observation)

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _consume_txn(transaction))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to consume OAuth state") from None

    status, state_data, contractor_obs = outcome_box[0]

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
    elif status == "lifecycle_mismatch":
        raise HTTPException(status_code=400, detail="OAuth state invalidated by concurrent contractor lifecycle change")

    return state_data, contractor_obs


@dataclass(frozen=True)
class JobberLeadCaptureMutationResult:
    contractor_id: str
    previous_enabled: bool
    enabled: bool
    connected: bool
    generation: int
    lifecycle_epoch: int
    updated_at: float


async def update_jobber_lead_capture_cas(
    *,
    contractor_id: str,
    enabled: bool,
    actor: str = "contractor_api",
    reason: str = "admin_lead_capture_toggle",
    request_metadata: dict[str, Any] | None = None,
    db: Any = None,
) -> JobberLeadCaptureMutationResult:
    """Transactionally update Jobber lead capture setting and admin audit event with CAS verification."""
    if type(enabled) is not bool:
        raise IntegrationTokenEnvelopeError("enabled must be an exact boolean")

    if db is None:
        try:
            db = get_firestore_client()
        except Exception:
            raise IntegrationTokenEnvelopeError("Database unavailable") from None

    if db is None:
        raise IntegrationTokenEnvelopeError("Database unavailable")

    valid_cid = validate_token_string(contractor_id, name="contractor_id")
    assert valid_cid is not None

    doc_ref = db.collection("contractors").document(valid_cid)
    audit_candidate_id = f"{valid_cid}_jobber_lead_capture_{secrets.token_hex(8)}"

    previous_enabled_box = [False]
    connected_box = [False]
    generation_box = [0]
    lifecycle_epoch_box = [0]
    updated_at_box = [0.0]
    audit_doc_id_box = [""]
    audit_data_box: list[dict[str, Any]] = [{}]
    body_prepared_box = [False]

    @transactional
    def _lead_capture_txn(transaction):
        previous_enabled_box[0] = False
        connected_box[0] = False
        generation_box[0] = 0
        lifecycle_epoch_box[0] = 0
        updated_at_box[0] = 0.0
        audit_doc_id_box[0] = ""
        audit_data_box[0] = {}
        body_prepared_box[0] = False

        # READ 1: Contractor document
        doc_snap = _get_doc_snapshot_in_txn(doc_ref, transaction)
        if not getattr(doc_snap, "exists", False):
            raise IntegrationTokenContractorNotFound("Contractor document not found")

        d_data = doc_snap.to_dict()
        if type(d_data) is not dict:
            raise IntegrationTokenCASConflict("Contractor document is not an exact dict")
        for k in d_data.keys():
            if type(k) is not str:
                raise IntegrationTokenCASConflict("Contractor document contains non-string keys")

        server_now = _extract_snapshot_server_time(doc_snap)

        lifecycle_ok, current_gen, current_epoch, lifecycle_present, lifecycle_err = parse_durable_lifecycle_counters(d_data, "jobber")
        if not lifecycle_ok:
            raise IntegrationTokenCASConflict("Invalid lifecycle metadata on contractor document")

        raw_prev = d_data.get("jobber_lead_capture_enabled")
        previous_enabled = (type(raw_prev) is bool and raw_prev is True)

        is_connected = is_durable_provider_connected(d_data, "jobber", contractor_id=valid_cid)
        if enabled is True and not is_connected:
            raise IntegrationTokenCASConflict("Connect Jobber before enabling lead capture")

        previous_enabled_box[0] = previous_enabled
        connected_box[0] = is_connected
        generation_box[0] = current_gen
        lifecycle_epoch_box[0] = current_epoch
        updated_at_box[0] = server_now

        # If no-op (enabled state is identical), do not mutate or write new audit event
        if previous_enabled is enabled:
            audit_doc_id_box[0] = ""
            audit_data_box[0] = {}
            body_prepared_box[0] = True
            raw_ts = d_data.get("jobber_lead_capture_updated_at")
            if type(raw_ts) in (int, float) and type(raw_ts) is not bool and math.isfinite(float(raw_ts)) and float(raw_ts) > 0.0:
                updated_at_box[0] = float(raw_ts)
            else:
                updated_at_box[0] = 0.0
            return

        # State-changing transition: use stable pre-minted audit candidate ID
        audit_doc_id = audit_candidate_id
        audit_ref = db.collection("admin_audit_events").document(audit_doc_id)

        # READ 2: Admin audit document (must be done before any writes)
        audit_snap = _get_doc_snapshot_in_txn(audit_ref, transaction)
        audit_exists = getattr(audit_snap, "exists", False)
        if audit_exists:
            raise IntegrationTokenCASConflict("Admin audit candidate document ID collision")

        audit_meta = dict(request_metadata or {})
        audit_meta["jobber_connected"] = is_connected

        audit_data = build_lead_capture_admin_audit_event(
            contractor_id=valid_cid,
            enabled=enabled,
            previous_enabled=previous_enabled,
            generation=current_gen,
            lifecycle_epoch=current_epoch,
            timestamp=server_now,
            actor_type=actor,
            reason=reason,
            request_metadata=audit_meta,
        )

        updates = {
            "jobber_lead_capture_enabled": enabled,
            "jobber_lead_capture_updated_at": server_now,
        }

        audit_doc_id_box[0] = audit_doc_id
        audit_data_box[0] = audit_data
        body_prepared_box[0] = True

        transaction.create(audit_ref, audit_data)
        transaction.update(doc_ref, updates)

    loop = asyncio.get_running_loop()
    try:
        transaction = db.transaction()
        await loop.run_in_executor(None, lambda: _lead_capture_txn(transaction))
    except (IntegrationTokenCASConflict, IntegrationTokenEnvelopeError):
        raise
    except Exception:
        if body_prepared_box[0]:
            try:
                # If no-op, verify contractor document matches
                if previous_enabled_box[0] is enabled:
                    recov_snap = doc_ref.get()
                    if getattr(recov_snap, "exists", False):
                        recov_data = recov_snap.to_dict()
                        if type(recov_data) is dict:
                            act_enabled = recov_data.get("jobber_lead_capture_enabled")
                            if (type(act_enabled) is bool and act_enabled is enabled) or (act_enabled is None and enabled is False):
                                return JobberLeadCaptureMutationResult(
                                    contractor_id=valid_cid,
                                    previous_enabled=previous_enabled_box[0],
                                    enabled=enabled,
                                    connected=connected_box[0],
                                    generation=generation_box[0],
                                    lifecycle_epoch=lifecycle_epoch_box[0],
                                    updated_at=updated_at_box[0],
                                )
                else:
                    # State changing: BOTH contractor document AND audit document must be verified
                    recov_snap = doc_ref.get()
                    audit_ref = db.collection("admin_audit_events").document(audit_doc_id_box[0])
                    recov_audit_snap = audit_ref.get()
                    if getattr(recov_snap, "exists", False) and getattr(recov_audit_snap, "exists", False):
                        recov_data = recov_snap.to_dict()
                        recov_audit_data = recov_audit_snap.to_dict()
                        if (
                            type(recov_data) is dict and all(type(k) is str for k in recov_data.keys())
                            and type(recov_audit_data) is dict and all(type(k) is str for k in recov_audit_data.keys())
                        ):
                            act_enabled = recov_data.get("jobber_lead_capture_enabled")
                            act_ts = recov_data.get("jobber_lead_capture_updated_at")
                            act_gen = parse_bounded_counter(recov_data, "jobber_generation", default=0, allow_absent=True)
                            act_epoch = parse_bounded_counter(recov_data, "jobber_lifecycle_epoch", default=0, allow_absent=True)

                            if (
                                type(act_enabled) is bool
                                and act_enabled is enabled
                                and type(act_ts) is float
                                and math.isfinite(act_ts)
                                and act_ts == updated_at_box[0]
                                and act_gen == generation_box[0]
                                and act_epoch == lifecycle_epoch_box[0]
                                and _exact_scalar_or_composite_equal(recov_audit_data, audit_data_box[0])
                            ):
                                return JobberLeadCaptureMutationResult(
                                    contractor_id=valid_cid,
                                    previous_enabled=previous_enabled_box[0],
                                    enabled=enabled,
                                    connected=connected_box[0],
                                    generation=generation_box[0],
                                    lifecycle_epoch=lifecycle_epoch_box[0],
                                    updated_at=updated_at_box[0],
                                )
            except Exception:
                pass
        raise IntegrationTokenCASConflict("Jobber lead capture update failed with ambiguous state") from None

    # Postcondition verification
    post_snap = doc_ref.get()
    if not getattr(post_snap, "exists", False):
        raise IntegrationTokenPostconditionError("Contractor document missing after lead capture mutation")
    post_data = post_snap.to_dict()
    if type(post_data) is not dict or not all(type(k) is str for k in post_data.keys()):
        raise IntegrationTokenPostconditionError("Contractor document is not an exact dict after mutation")

    if previous_enabled_box[0] is not enabled:
        # State-changing: verify contractor document update AND exact audit event write
        act_enabled = post_data.get("jobber_lead_capture_enabled")
        act_ts = post_data.get("jobber_lead_capture_updated_at")
        act_gen = parse_bounded_counter(post_data, "jobber_generation", default=0, allow_absent=True)
        act_epoch = parse_bounded_counter(post_data, "jobber_lifecycle_epoch", default=0, allow_absent=True)

        if (
            type(act_enabled) is not bool
            or act_enabled is not enabled
            or type(act_ts) is not float
            or not math.isfinite(act_ts)
            or act_ts != updated_at_box[0]
            or act_gen != generation_box[0]
            or act_epoch != lifecycle_epoch_box[0]
        ):
            raise IntegrationTokenPostconditionError("Postcondition verification failed for Jobber lead capture contractor mutation")

        audit_ref = db.collection("admin_audit_events").document(audit_doc_id_box[0])
        post_audit_snap = audit_ref.get()
        if not getattr(post_audit_snap, "exists", False):
            raise IntegrationTokenPostconditionError("Admin audit event missing after lead capture mutation")
        post_audit_data = post_audit_snap.to_dict()
        if not _exact_scalar_or_composite_equal(post_audit_data, audit_data_box[0]):
            raise IntegrationTokenPostconditionError("Admin audit event payload mismatch after lead capture mutation")

    return JobberLeadCaptureMutationResult(
        contractor_id=valid_cid,
        previous_enabled=previous_enabled_box[0],
        enabled=enabled,
        connected=connected_box[0],
        generation=generation_box[0],
        lifecycle_epoch=lifecycle_epoch_box[0],
        updated_at=updated_at_box[0],
    )
