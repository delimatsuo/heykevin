"""Jobber-specific durable admission, lifecycle claims and authenticated recovery.

Every authority decision runs in a started bounded transaction. No provider I/O
belongs here. OAuth proofs originate from one consumed state and are internal
Python values, never API payloads. Google continues using its existing schemas.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import secrets
from typing import Any

from fastapi import HTTPException
from google.cloud.firestore_v1 import DELETE_FIELD

from app.config import settings
from app.db.firestore_client import get_firestore_client
from app.db.jobber_disconnect_transactions import read_document, run_transaction, run_bounded_async
from app.db.jobber_disconnect_receipts import (
    CONTROL_COLLECTION, CONTROL_DOC_ID, BARRIERS_COLLECTION, compute_account_barrier_key,
    validate_control_record, validate_barrier_record, extract_snapshot_time,
    is_valid_opaque_id, MAX_RECEIPT_COUNTER,
)
from app.services.jobber_disconnect_barriers import ENFORCEMENT_FIELDS, is_jobber_disconnect_enforced_in_config, JobberEnforcementConfigurationError
from app.services.integration_tokens import (
    JobberAdmissionContext, parse_jobber_admission_context, MAX_KEY_VERSION,
    IntegrationTokenCASConflict, IntegrationTokenContractorNotFound,
    IntegrationTokenEnvelopeError, compute_raw_credentials_fingerprint,
    parse_durable_lifecycle_counters, parse_provider_operation_intent,
    get_provider_operation_intent_keys, get_provider_reauthorization_attempt_keys,
    resolve_usable_token_pair, validate_token_string, determine_write_format,
    encrypt_integration_token, validate_token_expires_at, validate_token_expires_in,
)

BINDING_KEYS = ENFORCEMENT_FIELDS | {"jobber_account_id"}
_UNOBSERVED_FLOOR = object()
ORDINARY_CLAIM_KEYS = frozenset(k for k in get_provider_operation_intent_keys("jobber")
    if "reauthorization_attempt" not in k and k not in
    ("jobber_reauthorization_required", "jobber_refresh_outcome_unknown"))


def _mutations():
    from app.services import integration_token_mutations
    return integration_token_mutations


def _database(db):
    if db is None:
        try:
            db = _mutations().get_firestore_client()
        except Exception:
            raise IntegrationTokenEnvelopeError("Database unavailable") from None
    if db is None:
        raise IntegrationTokenEnvelopeError("Database unavailable")
    return db


def _read(ref, txn):
    snap = read_document(ref, txn)
    extract_snapshot_time(snap)
    return snap


def _contractor(db, txn, cid):
    validate_token_string(cid, name="contractor_id")
    ref = db.collection("contractors").document(cid)
    snap = _read(ref, txn)
    if not snap.exists:
        raise IntegrationTokenContractorNotFound("Contractor document not found")
    data = snap.to_dict()
    if type(data) is not dict or any(type(k) is not str for k in data):
        raise IntegrationTokenCASConflict("Malformed contractor")
    return ref, data, extract_snapshot_time(snap)


def _life(data):
    ok, gen, epoch, present, _ = parse_durable_lifecycle_counters(data, "jobber")
    if not ok:
        raise IntegrationTokenCASConflict("Invalid Jobber lifecycle")
    return gen, epoch, present


def _next(value):
    if type(value) is not int or not 0 <= value < MAX_KEY_VERSION:
        raise IntegrationTokenCASConflict("Jobber lifecycle exhausted")
    return value + 1


def _fingerprint(data):
    return compute_raw_credentials_fingerprint(data.get("jobber_access_token"), data.get("jobber_refresh_token"))


def _authority(db, txn, data):
    snap = _read(db.collection(CONTROL_COLLECTION).document(CONTROL_DOC_ID), txn)
    if not snap.exists:
        if is_jobber_disconnect_enforced_in_config() or ENFORCEMENT_FIELDS.intersection(data):
            raise IntegrationTokenCASConflict("Jobber enforcement unavailable")
        return None
    control = validate_control_record(snap.to_dict())
    configured = settings.jobber_disconnect_expected_app_id
    if configured and configured != control["expected_app_id"]:
        raise IntegrationTokenCASConflict("Jobber app mismatch")
    # Validate mode even when the sticky record is authoritative.
    is_jobber_disconnect_enforced_in_config()
    return control


def _barrier(db, txn, control, account):
    if not is_valid_opaque_id(account):
        raise IntegrationTokenCASConflict("Invalid Jobber account")
    app = control["expected_app_id"]
    snap = _read(db.collection(BARRIERS_COLLECTION).document(compute_account_barrier_key(app, account)), txn)
    if not snap.exists:
        return 0
    record = validate_barrier_record(snap.to_dict(), expected_app_id=app, expected_account_id=account)
    value = record["barrier"]
    if value > control["receipt_counter"]:
        raise IntegrationTokenCASConflict("Jobber barrier exceeds control")
    return value


def _source(data):
    if "jobber_account_id" not in data:
        return None
    value = data["jobber_account_id"]
    if not is_valid_opaque_id(value):
        raise IntegrationTokenCASConflict("Malformed Jobber source account")
    return value


def _validate_optional_expiries(data):
    # Past access expiry still permits refresh. Malformed metadata never does.
    if "jobber_token_expires_at" in data:
        value = data["jobber_token_expires_at"]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise IntegrationTokenCASConflict("Malformed Jobber credential expiry")


def _ordinary(db, txn, data, *, observed=None, claim=False, credentials=True):
    control = _authority(db, txn, data)
    account = _source(data)
    if credentials and data.get("active") is not True:
        raise IntegrationTokenCASConflict("Inactive contractor")
    gen, epoch, _ = _life(data)
    if any(k in data for k in ENFORCEMENT_FIELDS - {"jobber_app_id", "jobber_accepted_disconnect_barrier"}):
        raise IntegrationTokenCASConflict("Jobber connection paused")
    if control is None:
        context = JobberAdmissionContext("ordinary", "preactivation", None, account, 0)
    else:
        if data.get("jobber_app_id") != control["expected_app_id"] or account is None:
            raise IntegrationTokenCASConflict("Jobber binding needs reauthorization")
        barrier = _barrier(db, txn, control, account)
        accepted = data.get("jobber_accepted_disconnect_barrier")
        if type(accepted) is not int or accepted != barrier:
            raise IntegrationTokenCASConflict("Jobber receipt requires reauthorization")
        context = JobberAdmissionContext("ordinary", "enforced", control["expected_app_id"], account, barrier)
    if observed is not None:
        if parse_jobber_admission_context(observed, purpose="ordinary") != context:
            raise IntegrationTokenCASConflict("Jobber admission changed")
    if credentials:
        _validate_optional_expiries(data)
        candidate = dict(data)
        if claim:
            for key in ORDINARY_CLAIM_KEYS:
                candidate.pop(key, None)
        access, refresh = resolve_usable_token_pair(candidate, "jobber", contractor_id=data.get("_admission_cid"))
        if access is None or refresh is None:
            raise IntegrationTokenCASConflict("Jobber credentials unavailable")
    return context


def _oauth_authority(db, txn, data, context, *, destination=None):
    context = parse_jobber_admission_context(context, purpose="oauth")
    control = _authority(db, txn, data)
    source = _source(data)
    if context.account_id != source:
        raise IntegrationTokenCASConflict("OAuth source identity changed")
    if control is None:
        if context.mode != "preactivation":
            raise IntegrationTokenCASConflict("OAuth enforcement changed")
        return 0
    if context.mode != "enforced" or context.app_id != control["expected_app_id"]:
        raise IntegrationTokenCASConflict("OAuth activation changed")
    if context.counter > control["receipt_counter"]:
        raise IntegrationTokenCASConflict("OAuth start counter exceeds control")
    if source is not None and _barrier(db, txn, control, source) > context.counter:
        raise IntegrationTokenCASConflict("OAuth source receipt advanced")
    if destination is not None:
        accepted = _barrier(db, txn, control, destination)
        if accepted > context.counter:
            raise IntegrationTokenCASConflict("OAuth destination receipt advanced")
        return accepted
    return 0


def _with_cid(data, cid):
    return {**data, "_admission_cid": cid}


def _observation(data, options):
    gen, epoch, _ = _life(data)
    for name, current in (("observed_generation", gen), ("observed_lifecycle_epoch", epoch)):
        value = options.get(name)
        if value is not None and (type(value) is not int or value != current):
            raise IntegrationTokenCASConflict("Generation conflict" if name == "observed_generation" else "Lifecycle epoch conflict")
    for name, key in (("observed_access_raw", "jobber_access_token"), ("observed_refresh_raw", "jobber_refresh_token")):
        if options.get("observed_generation") is not None or options.get(name) is not None:
            if not _mutations()._exact_raw_credential_equal(data.get(key), options.get(name)):
                raise IntegrationTokenCASConflict("Jobber credential mismatch")
    return gen, epoch


def _claim(data, claim_id, *, phase=None, kind=None, now=None):
    if type(claim_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", claim_id) or (kind is not None and type(kind) is not str):
        raise IntegrationTokenCASConflict("Invalid Jobber claim identity")
    status, parsed, _ = parse_provider_operation_intent(data, "jobber")
    gen, epoch, _ = _life(data)
    if status not in ("valid", "quarantined_reauthorizing") or parsed is None:
        raise IntegrationTokenCASConflict("Malformed Jobber claim")
    if parsed["id"] != claim_id:
        raise IntegrationTokenCASConflict("Jobber claim ID mismatch")
    if phase is not None and parsed["phase"] != phase:
        raise IntegrationTokenCASConflict("Jobber claim must be provider_request_started" if phase == "provider_request_started" else "Jobber claim phase changed")
    if ((kind is not None and parsed["kind"] != kind)
            or parsed["generation"] != gen or parsed["lifecycle_epoch"] != epoch):
        raise IntegrationTokenCASConflict("Jobber claim lifecycle changed")
    if not parsed.get("is_legacy") and parsed["credentials_fingerprint"] != _fingerprint(data):
        raise IntegrationTokenCASConflict("Jobber credentials fingerprint mismatch")
    if now is not None and parsed["expires_at"] <= now:
        raise IntegrationTokenCASConflict("Jobber claim changed or expired")
    return parsed


def _claim_authority(db, txn, data, parsed):
    context = parsed.get("jobber_context")
    if parsed["kind"] in ("connect", "reconnect"):
        if context is None:
            # Predeployment OAuth states never produce an active callback proof.
            raise IntegrationTokenCASConflict("OAuth claim context missing")
        _oauth_authority(db, txn, data, context)
    else:
        current = _ordinary(db, txn, data, claim=True)
        if context is None:
            if current.mode != "preactivation":
                raise IntegrationTokenCASConflict("Legacy claim invalid after activation")
        elif parse_jobber_admission_context(context, purpose="ordinary") != current:
            raise IntegrationTokenCASConflict("Jobber claim admission changed")
    return context


class _SnapshotReference:
    """Supply an already authoritative snapshot to the existing exact validator."""
    def __init__(self, snapshot):
        self.snapshot = snapshot
    def get(self):
        return self.snapshot


def _verify_updates(db, txn, cid, updates, *, authority=None, audit=None, expected_floor=_UNOBSERVED_FLOOR):
    ref = db.collection("contractors").document(cid)
    snap = _read(ref, txn)
    if not snap.exists:
        raise IntegrationTokenCASConflict("Missing Jobber mutation readback")
    data = snap.to_dict()
    if authority is not None:
        authority(txn, _with_cid(data, cid))
    gen, epoch, _ = _life(data)
    deleted = {k for k, v in updates.items() if v is DELETE_FIELD}
    expected = {k: v for k, v in updates.items() if v is not DELETE_FIELD}
    if "jobber_generation" in data:
        _mutations()._verify_mutation_postcondition(
            _SnapshotReference(snap), provider="jobber",
            expected_generation=expected.get("jobber_generation", gen),
            expected_lifecycle_epoch=expected.get("jobber_lifecycle_epoch", epoch),
            expected_connected=expected.get("jobber_connected", data.get("jobber_connected")),
            expected_extra_fields=expected, deleted_fields=deleted,
            expected_envelope_required=(_mutations()._UNCHECKED if expected_floor is _UNOBSERVED_FLOOR else expected_floor))
    else:
        # Legacy absence is itself checked; exact token writes always establish
        # the lifecycle triple and use the shared complete postcondition above.
        if any(k in data for k in deleted) or any(not _mutations()._exact_scalar_or_composite_equal(data.get(k), v) for k, v in expected.items()):
            raise IntegrationTokenCASConflict("Legacy Jobber postcondition mismatch")
    if audit:
        audit_ref, expected_audit = audit
        audit_snap = _read(audit_ref, txn)
        if not audit_snap.exists or not _mutations()._exact_scalar_or_composite_equal(audit_snap.to_dict(), expected_audit):
            raise _mutations().IntegrationTokenPostconditionError("Admin audit event missing or mismatched after lead capture mutation")
    return data


async def _run(db, body, verify=None):
    """Normal and ambiguous commits use exactly the same authoritative postread."""
    from google.api_core.exceptions import AlreadyExists
    db = _database(db)
    def work(deadline):
        prepared = {}
        def operation(txn):
            prepared.clear()
            result = body(txn, prepared)
            prepared["result"] = result
            return result
        try:
            result = run_transaction(db, operation, deadline=deadline)
        except AlreadyExists:
            raise IntegrationTokenCASConflict("Jobber create-only document already exists") from None
        except (IntegrationTokenCASConflict, IntegrationTokenContractorNotFound, IntegrationTokenEnvelopeError, HTTPException, _mutations().IntegrationTokenLeaseError):
            raise
        except Exception:
            if "result" not in prepared or verify is None:
                raise IntegrationTokenCASConflict("Jobber transaction failed") from None
            return run_transaction(db, lambda txn: verify(txn, prepared), deadline=deadline, read_only=True)
        if verify:
            return run_transaction(db, lambda txn: verify(txn, prepared), deadline=deadline, read_only=True)
        return result
    return await run_bounded_async(work, seconds=12.0)


async def load_snapshot(contractor_id, db=None):
    try:
        validate_token_string(contractor_id, name="contractor_id")
        db = _database(db)
    except Exception:
        return None
    def body(txn, box):
        ref, data, _ = _contractor(db, txn, contractor_id)
        context = _ordinary(db, txn, _with_cid(data, contractor_id))
        status, snapshot, info = _mutations()._classify_durable_provider_record(data, "jobber", contractor_id)
        updates = {}
        if status in ("valid_legacy_unnormalized", "needs_floor_promotion") and info:
            if info.get("needs_lifecycle_normalization"):
                updates.update(jobber_connected=True, jobber_generation=0, jobber_lifecycle_epoch=0)
            if info.get("needs_floor_promotion"):
                updates["jobber_token_envelope_required"] = True
        elif status != "valid_normalized":
            raise IntegrationTokenCASConflict("Jobber snapshot unavailable")
        box.update(context=context, updates=updates, fingerprint=_fingerprint(data), life=_life(data)[:2])
        if updates:
            txn.update(ref, updates)
    def verify(txn, box):
        _, data, _ = _contractor(db, txn, contractor_id)
        context = _ordinary(db, txn, _with_cid(data, contractor_id), observed=box["context"])
        if _fingerprint(data) != box["fingerprint"] or _life(data)[:2] != box["life"]:
            raise IntegrationTokenCASConflict("Jobber snapshot changed")
        for key, value in box["updates"].items():
            if type(data.get(key)) is not type(value) or data.get(key) != value:
                raise IntegrationTokenCASConflict("Jobber normalization not durable")
        status, snapshot, _ = _mutations()._classify_durable_provider_record(data, "jobber", contractor_id)
        if status != "valid_normalized":
            raise IntegrationTokenCASConflict("Jobber snapshot unavailable")
        snapshot["jobber_context"] = context
        return snapshot
    try:
        return await _run(db, body, verify)
    except Exception:
        return None


async def preflight(*, contractor_id, db=None, **unused):
    try:
        validate_token_string(contractor_id, name="contractor_id")
        db = _database(db)
    except Exception:
        return "blocked", "jobber_admission_unavailable"
    def body(txn, box):
        ref, data, now = _contractor(db, txn, contractor_id)
        data = _with_cid(data, contractor_id)
        context = _ordinary(db, txn, data, claim=True)
        status, parsed, _ = parse_provider_operation_intent(data, "jobber")
        box.update(context=context, updates={})
        if status == "absent":
            return ("proceed", None)
        if status != "valid" or parsed is None:
            return ("blocked", "jobber_claim_unavailable")
        _claim_authority(db, txn, data, parsed)
        if parsed["expires_at"] > now:
            return ("blocked", "active_intent_in_progress")
        updates = {k: DELETE_FIELD for k in ORDINARY_CLAIM_KEYS}
        if parsed["phase"] == "provider_request_started":
            updates.update(jobber_refresh_outcome_unknown=True, jobber_reauthorization_required=True)
            outcome = ("quarantined", "expired_started_quarantined")
        elif parsed["phase"] == "reserved":
            outcome = ("proceed", None)
        else:
            return ("blocked", "jobber_claim_unavailable")
        box["updates"] = updates
        txn.update(ref, updates)
        return outcome
    def verify(txn, box):
        outcome = box["result"]
        # Quarantine is expected here; validate the prior ordinary binding sans its
        # local quarantine, while still observing current account/control authority.
        def authority(tx, data):
            candidate = dict(data)
            if outcome[0] == "quarantined":
                candidate.pop("jobber_refresh_outcome_unknown", None)
                candidate.pop("jobber_reauthorization_required", None)
            _ordinary(db, tx, candidate, observed=box["context"], claim=True)
        _verify_updates(db, txn, contractor_id, box["updates"], authority=authority)
        return outcome
    try:
        return await _run(db, body, verify)
    except Exception:
        return "blocked", "jobber_admission_unavailable"


def _lease(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 3600:
        raise IntegrationTokenEnvelopeError("Invalid lease duration")
    return float(value)


def _claim_updates(data, claim_id, kind, now, duration, context, *, attempt=False):
    prefix = "jobber_reauthorization_attempt_" if attempt else "jobber_operation_intent_"
    gen, epoch, _ = _life(data)
    values = dict(id=claim_id, kind=kind, phase="reserved", acquired_at=now,
                  expires_at=now + duration, generation=gen, lifecycle_epoch=epoch,
                  credentials_fingerprint=_fingerprint(data), context=context.as_dict())
    updates = {prefix + k: v for k, v in values.items()}
    if kind == "refresh":
        updates.update(jobber_refresh_claim_id=claim_id, jobber_refresh_claim_phase="reserved",
                       jobber_refresh_claim_generation=gen, jobber_refresh_claim_expires_at=now + duration)
    return updates


async def acquire(*, contractor_id, kind, db=None, lease_duration=60.0, observed_jobber_context=None, **options):
    if kind not in ("business", "refresh") or options.get("bound_operation_id") is not None:
        raise IntegrationTokenEnvelopeError("Jobber OAuth claims require consumed state")
    duration = _lease(lease_duration)
    db = _database(db)
    claim_id = secrets.token_hex(16)
    def body(txn, box):
        ref, data, now = _contractor(db, txn, contractor_id)
        context = _ordinary(db, txn, _with_cid(data, contractor_id), observed=observed_jobber_context, claim=True)
        if observed_jobber_context is None and context.mode != "preactivation":
            raise IntegrationTokenCASConflict("Observed Jobber context required")
        _observation(data, options)
        status, parsed, _ = parse_provider_operation_intent(data, "jobber")
        cleanup = {}
        if status != "absent":
            if status != "valid" or parsed is None:
                raise IntegrationTokenCASConflict("Malformed or quarantined Jobber operation")
            _claim_authority(db, txn, _with_cid(data, contractor_id), parsed)
            if parsed["expires_at"] > now:
                raise _mutations().IntegrationTokenLeaseError("Jobber operation already held")
            cleanup = {key: DELETE_FIELD for key in ORDINARY_CLAIM_KEYS}
            if parsed["phase"] == "provider_request_started":
                # An expired started request has an unknown provider outcome.
                # Commit quarantine before reporting the conflict; never steal it.
                updates = {**cleanup, "jobber_refresh_outcome_unknown": True,
                           "jobber_reauthorization_required": True}
                box.update(updates=updates, quarantined=True, context=context)
                txn.update(ref, updates)
                return None
            if parsed["phase"] != "reserved":
                raise IntegrationTokenCASConflict("Jobber operation outcome unknown")
        updates = {**cleanup, **_claim_updates(data, claim_id, kind, now, duration, context)}
        box.update(updates=updates)
        txn.update(ref, updates)
        return claim_id, now + duration
    def verify(txn, box):
        def authority(tx, data):
            if box.get("quarantined"):
                candidate = dict(data)
                candidate.pop("jobber_refresh_outcome_unknown", None)
                candidate.pop("jobber_reauthorization_required", None)
                _ordinary(db, tx, candidate, observed=box["context"])
            else:
                parsed = _claim(data, claim_id, phase="reserved", kind=kind)
                _claim_authority(db, tx, data, parsed)
        _verify_updates(db, txn, contractor_id, box["updates"], authority=authority)
        return box["result"]
    result = await _run(db, body, verify)
    if result is None:
        raise IntegrationTokenCASConflict("Refresh outcome unknown; reauthorization required")
    return result


async def start(*, contractor_id, claim_id, db=None, lease_duration=60.0, kind=None, **options):
    duration = _lease(lease_duration)
    db = _database(db)
    def body(txn, box):
        ref, data, now = _contractor(db, txn, contractor_id)
        data = _with_cid(data, contractor_id)
        _observation(data, options)
        parsed = _claim(data, claim_id, phase="reserved", kind=kind, now=now)
        _claim_authority(db, txn, data, parsed)
        prefix = "jobber_reauthorization_attempt_" if parsed.get("is_reauthorization_attempt") else "jobber_operation_intent_"
        updates = {prefix + "phase": "provider_request_started", prefix + "expires_at": now + duration}
        if "jobber_refresh_claim_id" in data:
            updates.update(jobber_refresh_claim_phase="provider_request_started", jobber_refresh_claim_expires_at=now + duration)
        box["updates"] = updates
        txn.update(ref, updates)
        return claim_id, now + duration
    def verify(txn, box):
        def authority(tx, data):
            parsed = _claim(data, claim_id, phase="provider_request_started", kind=kind)
            _claim_authority(db, tx, data, parsed)
        _verify_updates(db, txn, contractor_id, box["updates"], authority=authority)
        return box["result"]
    return await _run(db, body, verify)


async def clear_claim(*, contractor_id, claim_id, db=None, kind=None, terminal=False, attempt=False, **unused):
    try:
        validate_token_string(contractor_id, name="contractor_id")
        validate_token_string(claim_id, name="claim_id")
        db = _database(db)
    except Exception:
        return False
    def body(txn, box):
        ref, data, now = _contractor(db, txn, contractor_id)
        _authority(db, txn, data)  # cleanup never authorizes token use
        parsed = _claim(data, claim_id, kind=kind)
        if attempt != bool(parsed.get("is_reauthorization_attempt")):
            raise IntegrationTokenCASConflict("Jobber claim owner differs")
        if not terminal and parsed["phase"] != "reserved":
            raise IntegrationTokenCASConflict("Started Jobber claim cannot be released")
        keys = get_provider_reauthorization_attempt_keys("jobber") if attempt else ORDINARY_CLAIM_KEYS
        updates = {key: DELETE_FIELD for key in keys}
        box.update(updates=updates, life=_life(data)[:2], fingerprint=_fingerprint(data))
        txn.update(ref, updates)
        return True
    def verify(txn, box):
        snap = _read(db.collection("contractors").document(contractor_id), txn)
        if not snap.exists:
            _authority(db, txn, {})
            return True  # deletion confirms the old owner is gone, never token use
        data, now = snap.to_dict(), extract_snapshot_time(snap)
        if type(data) is not dict or any(type(key) is not str for key in data):
            raise IntegrationTokenCASConflict("Malformed cleanup readback")
        _authority(db, txn, data)  # confirmation grants no provider authority
        if _life(data)[:2] != box["life"] or _fingerprint(data) != box["fingerprint"]:
            raise IntegrationTokenCASConflict("Jobber cleanup superseded")
        if any(key in data for key in box["updates"]):
            # A terminal completion may observe a newer valid owner. Never clear
            # that owner or mistake reappearance of our own claim for completion.
            if not terminal:
                raise IntegrationTokenCASConflict("Jobber claim remains")
            status, replacement, _ = parse_provider_operation_intent(data, "jobber")
            if status not in ("valid", "quarantined_reauthorizing") or replacement is None or replacement["id"] == claim_id:
                raise IntegrationTokenCASConflict("Jobber cleanup claim readback invalid")
            _claim(data, replacement["id"], now=now)
            _claim_authority(db, txn, _with_cid(data, contractor_id), replacement)
        return True
    try:
        return await _run(db, body, verify)
    except Exception:
        return False


async def quarantine(*, contractor_id, claim_id, db=None, **options):
    try:
        validate_token_string(contractor_id, name="contractor_id")
        validate_token_string(claim_id, name="claim_id")
        db = _database(db)
    except Exception:
        return False
    def body(txn, box):
        ref, data, now = _contractor(db, txn, contractor_id)
        data = _with_cid(data, contractor_id)
        _observation(data, options)
        parsed = _claim(data, claim_id, phase="provider_request_started", kind="refresh")
        _claim_authority(db, txn, data, parsed)
        context = _ordinary(db, txn, data, claim=True)
        updates = {key: DELETE_FIELD for key in ORDINARY_CLAIM_KEYS}
        updates.update(jobber_refresh_outcome_unknown=True, jobber_reauthorization_required=True)
        box.update(updates=updates, context=context, life=_life(data)[:2], fingerprint=_fingerprint(data))
        txn.update(ref, updates)
        return True
    def verify(txn, box):
        def authority(tx, data):
            data = dict(data)
            data.pop("jobber_refresh_outcome_unknown", None)
            data.pop("jobber_reauthorization_required", None)
            _ordinary(db, tx, data, observed=box["context"])
        data = _verify_updates(db, txn, contractor_id, box["updates"], authority=authority)
        if _life(data)[:2] != box["life"] or _fingerprint(data) != box["fingerprint"]:
            raise IntegrationTokenCASConflict("Jobber quarantine superseded")
        return True
    try:
        return await _run(db, body, verify)
    except Exception:
        return False


def _token_updates(data, cid, access, refresh, *, now, expires_at=None, expires_in=None):
    if expires_at is not None:
        validate_token_expires_at(expires_at)
    if expires_in is not None:
        validate_token_expires_in(expires_in)
    access = validate_token_string(access, name="access_token")
    refresh = validate_token_string(refresh, name="refresh_token")
    floor = data.get("jobber_token_envelope_required")
    if "jobber_token_envelope_required" in data and type(floor) is not bool:
        raise IntegrationTokenCASConflict("Invalid envelope floor")
    form = determine_write_format(contractor_id=cid, provider="jobber",
        stored_access=data.get("jobber_access_token"), stored_refresh=data.get("jobber_refresh_token"),
        envelope_required=floor)
    if form == "envelope":
        access = encrypt_integration_token(access, contractor_id=cid, provider="jobber", token_kind="access")
        refresh = encrypt_integration_token(refresh, contractor_id=cid, provider="jobber", token_kind="refresh")
    expires = None
    if expires_in is not None:
        expires = now + validate_token_expires_in(expires_in)
    elif expires_at is not None:
        expires = validate_token_expires_at(expires_at)
    updates = {key: DELETE_FIELD for key in get_provider_operation_intent_keys("jobber")}
    updates.update(jobber_access_token=access, jobber_refresh_token=refresh, jobber_connected=True,
                   jobber_token_expires_at=expires if expires is not None else DELETE_FIELD)
    if form == "envelope":
        updates["jobber_token_envelope_required"] = True
    return updates


async def persist(*, contractor_id, claim_id, new_access_token, new_refresh_token,
                  db=None, expires_at=None, expires_in=None, extra_updates=None, **options):
    db = _database(db)
    if extra_updates is not None and (type(extra_updates) is not dict
            or any(type(k) is not str for k in extra_updates) or set(extra_updates) - {"jobber_lead_capture_enabled"}
            or any(type(v) is not bool for v in extra_updates.values())):
        raise IntegrationTokenEnvelopeError("Invalid Jobber extra updates")
    def body(txn, box):
        ref, data, now = _contractor(db, txn, contractor_id)
        data = _with_cid(data, contractor_id)
        gen, epoch = _observation(data, options)
        parsed = _claim(data, claim_id, phase="provider_request_started", kind="refresh", now=now)
        _claim_authority(db, txn, data, parsed)
        context = _ordinary(db, txn, data, claim=True)
        updates = _token_updates(data, contractor_id, new_access_token, new_refresh_token,
                                now=now, expires_at=expires_at, expires_in=expires_in)
        updates.update(jobber_generation=_next(gen), jobber_lifecycle_epoch=epoch, jobber_token_refreshed_at=now)
        updates.update(extra_updates or {})
        box.update(updates=updates, context=context, floor=updates.get("jobber_token_envelope_required", data.get("jobber_token_envelope_required")))
        txn.update(ref, updates)
        return updates, gen + 1
    def verify(txn, box):
        _verify_updates(db, txn, contractor_id, box["updates"],
            authority=lambda tx, d: _ordinary(db, tx, d, observed=box["context"]), expected_floor=box["floor"])
        return box["result"]
    return await _run(db, body, verify)


@dataclass(frozen=True)
class JobberConsumedStateProof:
    state_id: str
    contractor_id: str
    claim_id: str
    generation: int
    lifecycle_epoch: int
    credentials_fingerprint: str
    context: JobberAdmissionContext


@dataclass(frozen=True)
class JobberBindingProof:
    consumed: JobberConsumedStateProof
    verified_account_id: str
    candidate_fingerprint: str


def _validate_consumed(proof):
    if type(proof) is not JobberConsumedStateProof:
        raise IntegrationTokenCASConflict("Authenticated Jobber state proof required")
    if (type(proof.state_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", proof.state_id)
            or type(proof.claim_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", proof.claim_id)
            or type(proof.generation) is not int or not 0 <= proof.generation <= MAX_KEY_VERSION
            or type(proof.lifecycle_epoch) is not int or not 0 <= proof.lifecycle_epoch <= MAX_KEY_VERSION
            or type(proof.credentials_fingerprint) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", proof.credentials_fingerprint)):
        raise IntegrationTokenCASConflict("Invalid authenticated Jobber proof")
    validate_token_string(proof.contractor_id, name="contractor_id")
    if type(proof.context) is not JobberAdmissionContext:
        raise IntegrationTokenCASConflict("Invalid authenticated Jobber context")
    parse_jobber_admission_context(proof.context, purpose="oauth")
    return proof


def finalize_jobber_binding_proof(consumed_proof, verified_account_id, access_token, refresh_token):
    proof = _validate_consumed(consumed_proof)
    if not is_valid_opaque_id(verified_account_id):
        raise IntegrationTokenCASConflict("Invalid verified Jobber account")
    validate_token_string(access_token, name="access_token")
    validate_token_string(refresh_token, name="refresh_token")
    return JobberBindingProof(proof, verified_account_id,
        compute_raw_credentials_fingerprint(access_token, refresh_token))


JOBBER_STATE_KEYS = frozenset({"schema_version", "contractor_id", "provider", "generation",
    "lifecycle_epoch", "credentials_fingerprint", "created_at", "expires_at", "pkce_method",
    "pkce_code_verifier", "receipt_counter_at_start", "enforcement_context", "source_account_id"})


def _status_in_txn(db, txn, data, cid):
    """Malformed authority is a paused connection; transport exceptions propagate."""
    from app.db.jobber_disconnect_receipts import JobberDisconnectError
    try:
        control = _authority(db, txn, data)
        gen, epoch, present = _life(data)
        if _mutations().is_durable_provider_tombstone(data, "jobber", cid):
            return "disconnected"
        token_keys = {"jobber_access_token", "jobber_refresh_token"}
        claim_keys = get_provider_operation_intent_keys("jobber")
        # A clean tombstone/empty account is safe to display as disconnected even
        # when active enforcement requires a new proof for a future connection.
        if (not token_keys.intersection(data) and not BINDING_KEYS.intersection(data)
                and not claim_keys.intersection(data)
                and (not present or data.get("jobber_connected") is False)):
            return "disconnected"
        if data.get("jobber_disconnect_state") == "reauthorization_required":
            return "reauthorization_required"
        if control is not None:
            account = _source(data)
            if data.get("jobber_app_id") != control["expected_app_id"] or account is None:
                return "reauthorization_required"
            barrier = _barrier(db, txn, control, account)
            accepted = data.get("jobber_accepted_disconnect_barrier")
            if type(accepted) is not int or not 0 <= accepted <= barrier:
                return "reauthorization_required"
            if accepted < barrier:
                return "disconnect_pending"
        _ordinary(db, txn, _with_cid(data, cid), claim=True)
        status, parsed, _ = parse_provider_operation_intent(data, "jobber")
        if status == "valid" and parsed and parsed["kind"] in ("business", "refresh"):
            _claim_authority(db, txn, _with_cid(data, cid), parsed)
        elif status != "absent":
            return "reauthorization_required"
        return "connected"
    except (IntegrationTokenCASConflict, IntegrationTokenEnvelopeError, JobberDisconnectError, JobberEnforcementConfigurationError, ValueError):
        return "reauthorization_required"


@dataclass(frozen=True)
class JobberConnectionStatusSnapshot:
    connection_status: str
    connected: bool
    lead_capture_enabled: bool
    connected_at: float | None
    lead_capture_updated_at: float | None = None


async def load_jobber_connection_status(contractor_id, *, db):
    validate_token_string(contractor_id, name="contractor_id")
    def body(txn, box):
        _, data, _ = _contractor(db, txn, contractor_id)
        status = _status_in_txn(db, txn, data, contractor_id)
        connected = status == "connected"
        historical_update = data.get("jobber_lead_capture_updated_at")
        try:
            updated_at = float(historical_update) if type(historical_update) in (int, float) else None
        except OverflowError:
            updated_at = None
        if updated_at is not None and (not math.isfinite(updated_at) or updated_at <= 0):
            updated_at = None
        return JobberConnectionStatusSnapshot(status, connected,
            connected and data.get("jobber_lead_capture_enabled") is True,
            _mutations().extract_safe_connected_at(data, "jobber") if connected else None, updated_at)
    return await _run(db, body)


async def create_state(*, db, collection_name, state, contractor_id, ttl_seconds=600.0, **unused):
    from app.services.oauth_pkce import generate_pkce_code_verifier
    validate_token_string(contractor_id, name="contractor_id")
    if type(collection_name) is not str or collection_name != "jobber_oauth_states" or type(state) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", state):
        raise HTTPException(400, "Invalid Jobber OAuth state")
    if (type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
            or not 1.0 <= ttl_seconds <= 600.0):
        raise HTTPException(400, "Invalid ttl_seconds: must be finite between 1 and 600 seconds")
    duration = float(ttl_seconds)
    if db is None:
        raise HTTPException(500, "Database unavailable")
    verifier = generate_pkce_code_verifier()
    state_ref = db.collection(collection_name).document(state)
    def body(txn, box):
        ref, data, now = _contractor(db, txn, contractor_id)
        if data.get("active") is not True:
            raise IntegrationTokenCASConflict("Inactive contractor")
        control = _authority(db, txn, data)
        source = _source(data)
        try:
            gen, epoch, _ = _life(data)
        except IntegrationTokenCASConflict:
            raise HTTPException(400, "Invalid contractor lifecycle metadata") from None
        status = _status_in_txn(db, txn, data, contractor_id)
        # Only provider authority permits preempting an in-flight operation.
        provider_suspended = control is not None and status in ("disconnect_pending", "reauthorization_required")
        # A healthy ordinary claim is not suspension. Classification treats it as
        # connected; a legacy unknown refresh outcome retains its explicit attempt.
        if control is not None:
            binding_current = (data.get("jobber_app_id") == control["expected_app_id"]
                and source is not None and type(data.get("jobber_accepted_disconnect_barrier")) is int
                and data["jobber_accepted_disconnect_barrier"] == _barrier(db, txn, control, source))
            provider_suspended = not binding_current or bool(ENFORCEMENT_FIELDS.intersection(data) - {"jobber_app_id", "jobber_accepted_disconnect_barrier"})
            if not any(k in data for k in ("jobber_access_token", "jobber_refresh_token")) and not BINDING_KEYS.intersection(data):
                provider_suspended = False
        if _read(state_ref, txn).exists:
            raise IntegrationTokenCASConflict("OAuth state collision")
        updates = {}
        if provider_suspended:
            gen, epoch = _next(gen), _next(epoch)
            updates = {k: DELETE_FIELD for k in get_provider_operation_intent_keys("jobber")}
            updates.update(jobber_generation=gen, jobber_lifecycle_epoch=epoch,
                           jobber_connected=False, jobber_lead_capture_enabled=False)
        context = JobberAdmissionContext("oauth", "enforced" if control else "preactivation",
            control["expected_app_id"] if control else None, source, control["receipt_counter"] if control else 0)
        payload = dict(schema_version=1, contractor_id=contractor_id, provider="jobber", generation=gen,
            lifecycle_epoch=epoch, credentials_fingerprint=_fingerprint(data), created_at=now,
            expires_at=now + duration, pkce_method="S256", pkce_code_verifier=verifier,
            receipt_counter_at_start=context.counter, enforcement_context=context.as_dict(), source_account_id=source)
        box.update(payload=payload, updates=updates, context=context)
        if updates:
            txn.update(ref, updates)
        txn.create(state_ref, payload)
        return payload
    def verify(txn, box):
        _, data, _ = _contractor(db, txn, contractor_id)
        _oauth_authority(db, txn, data, box["context"])
        snap = _read(state_ref, txn)
        if not snap.exists or not _mutations()._exact_scalar_or_composite_equal(snap.to_dict(), box["payload"]):
            raise IntegrationTokenCASConflict("OAuth state readback mismatch")
        if _life(data)[:2] != (box["payload"]["generation"], box["payload"]["lifecycle_epoch"]) or _fingerprint(data) != box["payload"]["credentials_fingerprint"]:
            raise IntegrationTokenCASConflict("OAuth state lifecycle changed")
        for k, v in box["updates"].items():
            if (v is DELETE_FIELD and k in data) or (v is not DELETE_FIELD and not _mutations()._exact_scalar_or_composite_equal(data.get(k), v)):
                raise IntegrationTokenCASConflict("OAuth recovery advance not durable")
        return box["payload"]
    return await _run(db, body, verify)


def _parse_state(data):
    from app.services.oauth_pkce import validate_pkce_code_verifier
    if type(data) is not dict or any(type(k) is not str for k in data) or set(data) != JOBBER_STATE_KEYS or type(data["schema_version"]) is not int or data["schema_version"] != 1 or type(data["provider"]) is not str or data["provider"] != "jobber":
        raise HTTPException(400, "Restart Jobber authorization")
    validate_token_string(data["contractor_id"], name="contractor_id")
    if any(type(data[k]) is not int or not 0 <= data[k] <= MAX_KEY_VERSION for k in ("generation", "lifecycle_epoch", "receipt_counter_at_start")):
        raise HTTPException(400, "Invalid Jobber OAuth counters")
    if any(type(data[k]) is not float or not math.isfinite(data[k]) or data[k] <= 0 for k in ("created_at", "expires_at")) or data["expires_at"] <= data["created_at"] or data["expires_at"] - data["created_at"] > 605.0:
        raise HTTPException(400, "Invalid Jobber OAuth time")
    if type(data["credentials_fingerprint"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", data["credentials_fingerprint"]):
        raise HTTPException(400, "Invalid Jobber OAuth fingerprint")
    if type(data["pkce_method"]) is not str or data["pkce_method"] != "S256":
        raise HTTPException(400, "Invalid Jobber PKCE method")
    validate_pkce_code_verifier(data["pkce_code_verifier"])
    context = parse_jobber_admission_context(data["enforcement_context"], purpose="oauth")
    if (data["source_account_id"] is not None and not is_valid_opaque_id(data["source_account_id"])) or context.counter != data["receipt_counter_at_start"] or context.account_id != data["source_account_id"]:
        raise HTTPException(400, "Jobber OAuth context mismatch")
    return context


async def consume_jobber_oauth_state(*, db, state):
    if db is None:
        raise HTTPException(500, "Database unavailable for state validation")
    if type(state) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", state):
        raise HTTPException(400, "Invalid Jobber OAuth state")
    state_ref = db.collection("jobber_oauth_states").document(state)
    claim_id = secrets.token_hex(16)
    def consume_body(txn, box):
        state_snap = _read(state_ref, txn)
        if not state_snap.exists:
            raise HTTPException(400, "OAuth state expired or invalid")
        payload = state_snap.to_dict()
        context = _parse_state(payload)
        cid = payload["contractor_id"]
        ref, data, now = _contractor(db, txn, cid)
        if data.get("active") is not True or payload["expires_at"] <= now or payload["created_at"] > now:
            raise HTTPException(400, "OAuth state expired or invalid")
        _oauth_authority(db, txn, data, context)
        gen, epoch, _ = _life(data)
        if (gen, epoch) != (payload["generation"], payload["lifecycle_epoch"]) or _fingerprint(data) != payload["credentials_fingerprint"]:
            raise HTTPException(400, "OAuth state lifecycle changed")
        status, parsed, _ = parse_provider_operation_intent(data, "jobber")
        is_quarantined = status == "quarantined"
        if status not in ("absent", "quarantined"):
            raise HTTPException(409, "Another Jobber operation is in progress")
        updates = _claim_updates(data, claim_id, "reconnect" if is_quarantined else "connect", now, 60.0, context, attempt=is_quarantined)
        proof = JobberConsumedStateProof(state, cid, claim_id, gen, epoch, _fingerprint(data), context)
        observation = dict(contractor_id=cid, provider="jobber", generation=gen, lifecycle_epoch=epoch,
            observed_access_raw=data.get("jobber_access_token"), observed_refresh_raw=data.get("jobber_refresh_token"),
            envelope_required=data.get("jobber_token_envelope_required"), claim_id=claim_id,
            is_quarantined=is_quarantined, jobber_context=context)
        box.update(updates=updates, proof=proof, observation=observation, payload=payload)
        txn.update(ref, updates)
        txn.delete(state_ref)
        return payload, observation, proof
    def body(txn, box):
        from app.services.oauth_pkce import PkceError
        try:
            return consume_body(txn, box)
        except (HTTPException, IntegrationTokenCASConflict, IntegrationTokenEnvelopeError, PkceError) as error:
            # No mutations precede successful validation in consume_body.
            box["invalid"] = error if isinstance(error, HTTPException) else HTTPException(400, "Restart Jobber authorization")
            txn.delete(state_ref)
            return None
    def verify(txn, box):
        if "invalid" in box:
            if _read(state_ref, txn).exists:
                raise IntegrationTokenCASConflict("Invalid OAuth state not consumed")
            return box["invalid"]
        proof = box["proof"]
        if _read(state_ref, txn).exists:
            raise IntegrationTokenCASConflict("OAuth state not consumed")
        def authority(tx, data):
            parsed = _claim(data, proof.claim_id, phase="reserved")
            if parsed.get("jobber_context") != proof.context:
                raise IntegrationTokenCASConflict("OAuth proof claim changed")
            _oauth_authority(db, tx, data, proof.context)
        _verify_updates(db, txn, proof.contractor_id, box["updates"], authority=authority)
        return box["result"]
    result = await _run(db, body, verify)
    if isinstance(result, HTTPException):
        raise result
    return result


async def connect(*, contractor_id, access_token, refresh_token, jobber_account_id=None,
                  jobber_binding_proof=None, receipt_counter_at_start=None, claim_id=None,
                  expires_at=None, expires_in=None, extra_updates=None, actor="oauth_state", db=None, **options):
    from app.db.integration_lifecycle_audit import AUDIT_COLLECTION, format_audit_doc_id, build_connect_audit_event
    db = _database(db)
    if not is_valid_opaque_id(jobber_account_id):
        raise IntegrationTokenEnvelopeError("Verified Jobber account required")
    if options.get("scope") is not None:
        raise IntegrationTokenEnvelopeError("Jobber scope unsupported")
    if extra_updates is not None and (type(extra_updates) is not dict or any(type(k) is not str for k in extra_updates) or set(extra_updates) - {"jobber_lead_capture_enabled"} or any(type(v) is not bool for v in extra_updates.values())):
        raise IntegrationTokenEnvelopeError("Invalid Jobber extra updates")
    if receipt_counter_at_start is not None and (type(receipt_counter_at_start) is not int or not 0 <= receipt_counter_at_start <= MAX_RECEIPT_COUNTER):
        raise IntegrationTokenEnvelopeError("Invalid OAuth start counter")
    proof = None
    if jobber_binding_proof is not None:
        if type(jobber_binding_proof) is not JobberBindingProof:
            raise IntegrationTokenCASConflict("Invalid Jobber binding proof")
        if (not is_valid_opaque_id(jobber_binding_proof.verified_account_id)
                or type(jobber_binding_proof.candidate_fingerprint) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", jobber_binding_proof.candidate_fingerprint)):
            raise IntegrationTokenCASConflict("Invalid Jobber candidate proof")
        proof = _validate_consumed(jobber_binding_proof.consumed)
        if (proof.contractor_id != contractor_id or proof.claim_id != claim_id
                or jobber_binding_proof.verified_account_id != jobber_account_id
                or jobber_binding_proof.candidate_fingerprint != compute_raw_credentials_fingerprint(access_token, refresh_token)
                or (receipt_counter_at_start is not None and receipt_counter_at_start != proof.context.counter)):
            raise IntegrationTokenCASConflict("Jobber binding proof mismatch")
    def body(txn, box):
        ref, data, now = _contractor(db, txn, contractor_id)
        if data.get("active") is not True:
            raise IntegrationTokenCASConflict("Inactive contractor")
        control = _authority(db, txn, data)
        gen, epoch = _observation(data, options)
        accepted = 0
        if proof is None:
            if control is not None or claim_id is not None:
                raise IntegrationTokenCASConflict("Consumed Jobber proof required")
            claim_status = parse_provider_operation_intent(data, "jobber")[0]
            if claim_status == "malformed":
                raise IntegrationTokenCASConflict("Malformed Jobber claim")
            if claim_status != "absent":
                raise IntegrationTokenCASConflict("Jobber claim held")
        else:
            parsed = _claim(data, claim_id, phase="provider_request_started", now=now)
            if parsed["kind"] not in ("connect", "reconnect") or parsed.get("jobber_context") != proof.context or (gen, epoch, _fingerprint(data)) != (proof.generation, proof.lifecycle_epoch, proof.credentials_fingerprint):
                raise IntegrationTokenCASConflict("Jobber proof no longer owns binding")
            accepted = _oauth_authority(db, txn, data, proof.context, destination=jobber_account_id)
        next_gen, next_epoch = _next(gen), _next(epoch)
        action = "reconnected" if gen > 0 or data.get("jobber_access_token") is not None else "connected"
        audit_id = format_audit_doc_id(contractor_id=contractor_id, provider="jobber", generation=next_gen, action=action)
        audit_ref = db.collection(AUDIT_COLLECTION).document(audit_id)
        if _read(audit_ref, txn).exists:
            raise IntegrationTokenCASConflict("Jobber connect audit already exists")
        audit = build_connect_audit_event(contractor_id=contractor_id, provider="jobber", generation=next_gen, actor=actor, action=action, timestamp=now)
        audit["lifecycle_epoch"] = next_epoch
        updates = _token_updates(data, contractor_id, access_token, refresh_token, now=now, expires_at=expires_at, expires_in=expires_in)
        updates.update({key: DELETE_FIELD for key in ENFORCEMENT_FIELDS})
        updates.update(jobber_generation=next_gen, jobber_lifecycle_epoch=next_epoch,
            jobber_connected_at=now, jobber_disconnected_at=DELETE_FIELD, jobber_token_refreshed_at=DELETE_FIELD,
            jobber_account_id=jobber_account_id)
        # Provider suspension preemption already forced capture false at state
        # creation; additionally require a currently accepted source binding.
        was_suspended = bool((ENFORCEMENT_FIELDS - {"jobber_app_id", "jobber_accepted_disconnect_barrier"}).intersection(data))
        if control is not None:
            source = _source(data)
            was_suspended = was_suspended or source is None or data.get("jobber_app_id") != control["expected_app_id"] or data.get("jobber_accepted_disconnect_barrier") != _barrier(db, txn, control, source)
            updates.update(jobber_app_id=control["expected_app_id"], jobber_accepted_disconnect_barrier=accepted)
        updates["jobber_lead_capture_enabled"] = (not was_suspended and data.get("jobber_lead_capture_enabled") is True
            and data.get("jobber_account_id") == jobber_account_id
            and (extra_updates or {}).get("jobber_lead_capture_enabled") is not False)
        context = JobberAdmissionContext("ordinary", "enforced" if control else "preactivation",
            control["expected_app_id"] if control else None, jobber_account_id, accepted)
        box.update(updates=updates, audit=(audit_ref, audit), context=context, floor=updates.get("jobber_token_envelope_required", data.get("jobber_token_envelope_required")))
        txn.create(audit_ref, audit)
        txn.update(ref, updates)
        return updates, next_gen, audit_id
    def verify(txn, box):
        _verify_updates(db, txn, contractor_id, box["updates"], audit=box["audit"],
            authority=lambda tx, d: _ordinary(db, tx, d, observed=box["context"]), expected_floor=box["floor"])
        return box["result"]
    return await _run(db, body, verify)


async def update_capture(*, contractor_id, enabled, actor="contractor_api", reason="admin_lead_capture_toggle", request_metadata=None, db=None):
    from app.db.admin_audit import build_lead_capture_admin_audit_event
    validate_token_string(contractor_id, name="contractor_id")
    if type(enabled) is not bool:
        raise IntegrationTokenEnvelopeError("Capture must be boolean")
    db = _database(db)
    audit_ref = db.collection("admin_audit_events").document(f"{contractor_id}_jobber_lead_capture_{secrets.token_hex(8)}")
    def body(txn, box):
        ref, data, now = _contractor(db, txn, contractor_id)
        gen, epoch, present = _life(data)
        status = _status_in_txn(db, txn, data, contractor_id)
        context = _ordinary(db, txn, _with_cid(data, contractor_id)) if enabled else None
        if enabled and status != "connected":
            raise IntegrationTokenCASConflict("Connect Jobber before enabling capture")
        previous = data.get("jobber_lead_capture_enabled") is True
        updates, audit = {}, None
        stamp = data.get("jobber_lead_capture_updated_at", 0.0)
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
            stamp = 0.0
        if previous != enabled:
            if _read(audit_ref, txn).exists:
                raise IntegrationTokenCASConflict("Capture audit collision")
            stamp = now
            updates = {"jobber_lead_capture_enabled": enabled, "jobber_lead_capture_updated_at": stamp}
            meta = dict(request_metadata or {})
            meta["jobber_connected"] = status == "connected"
            audit = build_lead_capture_admin_audit_event(contractor_id=contractor_id,
                enabled=enabled, previous_enabled=previous, generation=gen, lifecycle_epoch=epoch,
                timestamp=stamp, actor_type=actor, reason=reason, request_metadata=meta)
            txn.create(audit_ref, audit)
            txn.update(ref, updates)
        box.update(updates=updates, context=context, audit=(audit_ref, audit) if audit else None,
                   previous=previous, stamp=float(stamp), life=(gen, epoch))
    def verify(txn, box):
        def authority(tx, data):
            if enabled:
                _ordinary(db, tx, data, observed=box["context"])
            else:
                # Status is authoritative but a local disable remains possible
                # even if the binding needs reauthorization.
                _status_in_txn(db, tx, data, contractor_id)
        data = _verify_updates(db, txn, contractor_id, box["updates"], authority=authority, audit=box["audit"])
        if _life(data)[:2] != box["life"] or (data.get("jobber_lead_capture_enabled") is True) != enabled:
            raise IntegrationTokenCASConflict("Capture mutation superseded")
        status = _status_in_txn(db, txn, data, contractor_id)
        return _mutations().JobberLeadCaptureMutationResult(contractor_id, box["previous"], enabled,
            status == "connected", *box["life"], box["stamp"])
    return await _run(db, body, verify)


def provider_binding_blocked(db, txn, data):
    """Provider-level suspension, distinct from an ordinary refresh quarantine."""
    from app.db.jobber_disconnect_receipts import JobberDisconnectError
    from app.services.jobber_disconnect_barriers import JobberEnforcementConfigurationError
    try:
        control = _authority(db, txn, data)
        if _mutations().is_durable_provider_tombstone(data, "jobber", data.get("contractor_id", "")):
            return False
        if (ENFORCEMENT_FIELDS - {"jobber_app_id", "jobber_accepted_disconnect_barrier"}).intersection(data):
            return True
        if control is None:
            return False
        if not ({"jobber_access_token", "jobber_refresh_token"} | BINDING_KEYS).intersection(data):
            return not _mutations().is_durable_provider_tombstone(data, "jobber", data.get("contractor_id", ""))
        account = _source(data)
        if account is None or data.get("jobber_app_id") != control["expected_app_id"]:
            return True
        accepted = data.get("jobber_accepted_disconnect_barrier")
        return type(accepted) is not int or accepted != _barrier(db, txn, control, account)
    except (IntegrationTokenCASConflict, IntegrationTokenEnvelopeError, JobberDisconnectError, JobberEnforcementConfigurationError):
        return True


async def admit_revocation(result, *, db):
    """Last dispatch fence for a user-initiated normal app-side disconnect."""
    context = result.jobber_context
    if type(context) is not JobberAdmissionContext:
        return False
    def body(txn, box):
        _, data, _ = _contractor(db, txn, result.contractor_id)
        if not _mutations().is_durable_provider_tombstone(data, "jobber", result.contractor_id) or _life(data)[:2] != (result.generation, result.lifecycle_epoch):
            return False
        control = _authority(db, txn, data)
        if control is None:
            return context.mode == "preactivation"
        return (context.mode == "enforced" and context.app_id == control["expected_app_id"]
                and _barrier(db, txn, control, context.account_id) == context.counter)
    try:
        return await _run(db, body)
    except Exception:
        return False


def _denied_revocation_tombstone(db, txn, result):
    try:
        return _prove_denied_revocation_tombstone(db, txn, result)
    except IntegrationTokenCASConflict:
        raise
    except Exception:
        # Unavailable or malformed authority is not evidence of a cancelled
        # dispatch, and diagnostics must not expose stored record contents.
        raise IntegrationTokenCASConflict("Jobber cancellation authority unconfirmed") from None


def _prove_denied_revocation_tombstone(db, txn, result):
    """Prove a current authority change, never infer it from a failed read."""
    context = parse_jobber_admission_context(result.jobber_context, purpose="ordinary")
    ref = db.collection("contractors").document(result.contractor_id)
    snap = _read(ref, txn)
    _mutations()._verify_complete_disconnect_postcondition(
        _SnapshotReference(snap), contractor_id=result.contractor_id, provider="jobber",
        expected_generation=result.generation, expected_lifecycle_epoch=result.lifecycle_epoch,
        expected_disconnected_at=result.expected_disconnected_at, expected_floor=result.expected_floor)
    data = snap.to_dict()
    if data.get("jobber_lead_capture_enabled") is not False or _source(data) != context.account_id:
        raise IntegrationTokenCASConflict("Jobber cancellation tombstone changed")
    control = _authority(db, txn, data)
    if control is None:
        raise IntegrationTokenCASConflict("Jobber dispatch denial is unproven")
    # Activation itself invalidates a preactivation dispatch. Validate the known
    # source account too; an accountless legacy binding has no account to probe.
    barrier = _barrier(db, txn, control, context.account_id) if context.account_id is not None else None
    if context.mode == "enforced" and (
        context.app_id != control["expected_app_id"] or barrier is None
        or context.counter >= barrier
    ):
        raise IntegrationTokenCASConflict("Jobber dispatch denial is unproven")
    return snap


async def cancel_denied_revocation(result, *, db):
    """Only the original, still-local dispatch owner can certify zero HTTP.

    The canonical unavailable-token status has no claim. This is deliberately a
    separate atomic audit/outbox transition from outcomes of attempted HTTP.
    """
    from app.db.integration_lifecycle_audit import (
        AUDIT_COLLECTION, REVOCATION_OUTBOX_COLLECTION, validate_disconnect_lifecycle_pair,
        REVOCATION_STATUS_REQUEST_STARTED, REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE,
    )
    if (type(result) is not _mutations().DisconnectProviderResult or result.provider != "jobber"
            or result.local_only or result.revocation_status != REVOCATION_STATUS_REQUEST_STARTED
            or result.audit_finalized is not False or type(result.claim_id) is not str or not result.claim_id
            or type(result.access_token_for_revocation) is not str or not result.access_token_for_revocation
            or type(result.jobber_context) is not JobberAdmissionContext):
        raise IntegrationTokenCASConflict("Jobber cancellation requires original dispatch owner")
    audit_ref = db.collection(AUDIT_COLLECTION).document(result.audit_id)
    outbox_ref = db.collection(REVOCATION_OUTBOX_COLLECTION).document(result.outbox_id)

    def read_pair(txn):
        a, o = _read(audit_ref, txn), _read(outbox_ref, txn)
        if not a.exists or not o.exists:
            raise IntegrationTokenCASConflict("Jobber cancellation lifecycle missing")
        try:
            pair = validate_disconnect_lifecycle_pair(a.to_dict(), o.to_dict(),
                expected_contractor_id=result.contractor_id, expected_provider="jobber",
                expected_generation=result.generation, expected_lifecycle_epoch=result.lifecycle_epoch,
                expected_audit_id=result.audit_id, expected_outbox_id=result.outbox_id)
        except Exception:
            raise IntegrationTokenCASConflict("Jobber cancellation lifecycle invalid") from None
        return pair

    def body(txn, box):
        snap = _denied_revocation_tombstone(db, txn, result)
        a, o = read_pair(txn)
        if (o["status"] != REVOCATION_STATUS_REQUEST_STARTED or o["claim_id"] != result.claim_id
                or not _mutations()._exact_scalar_or_composite_equal(o["created_at"], result.created_at)
                or not _mutations()._exact_scalar_or_composite_equal(o["updated_at"], result.updated_at)
                or o["created_at"] != result.expected_disconnected_at
                or o["credential_deletion_disposition"] != result.credential_deletion):
            raise IntegrationTokenCASConflict("Jobber cancellation owner changed")
        now = max(extract_snapshot_time(snap), o["updated_at"])
        expected_o = {**o, "status": REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE,
                      "claim_id": None, "updated_at": now,
                      "audit_finalized": True, "audit_finalized_at": now}
        expected_a = {**a, "revocation_status": REVOCATION_STATUS_NOT_ATTEMPTED_UNAVAILABLE,
                      "revocation_completed_at": now}
        validate_disconnect_lifecycle_pair(expected_a, expected_o)
        box.update(audit=expected_a, outbox=expected_o)
        txn.set(audit_ref, expected_a)
        txn.set(outbox_ref, expected_o)
        return expected_a, expected_o

    def verify(txn, box):
        _denied_revocation_tombstone(db, txn, result)
        a, o = read_pair(txn)
        if (not _mutations()._exact_scalar_or_composite_equal(a, box["audit"])
                or not _mutations()._exact_scalar_or_composite_equal(o, box["outbox"])):
            raise IntegrationTokenCASConflict("Jobber cancellation persistence unconfirmed")
        return a, o
    return await _run(db, body, verify)


class _TransactionReadDB:
    """Read-only adapter for existing exact audit validators inside one snapshot."""
    def __init__(self, db, txn):
        self.db, self.txn = db, txn
    def collection(self, name):
        owner = self
        class Collection:
            def document(self, identifier):
                ref = owner.db.collection(name).document(identifier)
                class Reference:
                    id = ref.id
                    def get(self):
                        return _read(ref, owner.txn)
                return Reference()
        return Collection()


def _revocation_authority(db, txn, data, context):
    control = _authority(db, txn, data)
    if context is None:
        return  # idempotent tombstone without any dispatch token
    context = parse_jobber_admission_context(context, purpose="ordinary")
    if control is None:
        if context.mode != "preactivation":
            raise IntegrationTokenCASConflict("Jobber disconnect admission changed")
    elif context.mode != "enforced" or context.app_id != control["expected_app_id"] or _barrier(db, txn, control, context.account_id) != context.counter:
        raise IntegrationTokenCASConflict("Jobber disconnect receipt advanced")


async def run_normal_disconnect(*, db, callback, result_factory, verification):
    def body(txn, box):
        callback(txn)
        return result_factory()
    def verify(txn, box):
        result = box["result"]
        reader = _TransactionReadDB(db, txn)
        snap = reader.collection("contractors").document(result.contractor_id).get()
        if not snap.exists:
            raise IntegrationTokenCASConflict("Jobber disconnect readback missing")
        _revocation_authority(db, txn, snap.to_dict(), result.jobber_context)
        _mutations()._verify_complete_disconnect_postcondition(
            _SnapshotReference(snap), db=reader, **verification())
        return result
    return await _run(db, body, verify)


async def run_audit_mutation(*, db, contractor_id, callback, outbox_id, expected_outbox, expected_audit=None, audit_id=None):
    """Bound historical audit writes; these never reauthorize credential use."""
    def body(txn, box):
        _, data, _ = _contractor(db, txn, contractor_id)
        _authority(db, txn, data)
        callback(txn)
        return expected_outbox()
    def verify(txn, box):
        _, data, _ = _contractor(db, txn, contractor_id)
        _authority(db, txn, data)
        reader = _TransactionReadDB(db, txn)
        _mutations()._verify_outbox_postcondition(reader, outbox_id, expected_outbox())
        if expected_audit is not None:
            _mutations()._verify_audit_postcondition(reader, audit_id, expected_audit())
        return expected_outbox()
    return await _run(db, body, verify)


async def read_disconnect_lifecycle(*, db, result, cancelled_dispatch=None):
    from app.db.integration_lifecycle_audit import AUDIT_COLLECTION, REVOCATION_OUTBOX_COLLECTION
    def body(txn, box):
        c = (_denied_revocation_tombstone(db, txn, result) if cancelled_dispatch is not None else
             _read(db.collection("contractors").document(result.contractor_id), txn))
        if not c.exists:
            raise IntegrationTokenContractorNotFound("Contractor missing")
        _authority(db, txn, c.to_dict())
        a = _read(db.collection(AUDIT_COLLECTION).document(result.audit_id), txn)
        o = _read(db.collection(REVOCATION_OUTBOX_COLLECTION).document(result.outbox_id), txn)
        if cancelled_dispatch is not None:
            expected_a, expected_o = cancelled_dispatch
            if (not a.exists or not o.exists
                    or not _mutations()._exact_scalar_or_composite_equal(a.to_dict(), expected_a)
                    or not _mutations()._exact_scalar_or_composite_equal(o.to_dict(), expected_o)):
                raise IntegrationTokenCASConflict("Jobber cancellation lifecycle changed after verification")
        return c, a, o
    return await _run(db, body)
