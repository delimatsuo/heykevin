"""Bounded durable Jobber disconnect fan-out; never calls the provider."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
import uuid

from google.cloud.firestore_v1 import DELETE_FIELD
from google.cloud.firestore_v1.base_query import FieldFilter

from app.db.firestore_client import get_firestore_client
from app.db.jobber_disconnect_receipts import (
    CONTROL_COLLECTION, CONTROL_DOC_ID, BARRIERS_COLLECTION, WORK_COLLECTION,
    SCAN_CURSOR_COLLECTION, SCAN_CURSOR_DOC_ID, SUSPENSION_AUDIT_COLLECTION,
    MAX_KEY_VERSION, MAX_RECEIPT_COUNTER, JobberDisconnectError,
    JobberDisconnectWorkError, JobberLifecycleOverflowError, JobberSuspensionAuditError,
    compute_account_barrier_key, compute_work_doc_id, compute_suspension_audit_doc_id,
    extract_snapshot_time, is_safe_document_id, is_firestore_document_id, validate_control_record,
    validate_work_record, validate_scan_cursor_record, validate_account_records,
    validate_suspension_audit_record,
)
from app.db.jobber_disconnect_transactions import (
    Deadline, JobberSnapshotTimeError, read_document, run_transaction, stream_query, run_bounded_async,
)
from app.services.integration_tokens import (
    get_provider_operation_intent_keys, parse_durable_lifecycle_counters,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)
MAX_WORK_ATTEMPTS = 5
DEFAULT_LEASE_DURATION_SECONDS = 60.0
DEFAULT_PAGE_SIZE = 50
CANDIDATE_SCAN_LIMIT = 10
REVIEW_COLLECTION = "jobber_disconnect_work_review"


def _data(snap):
    return snap.to_dict() if snap.exists else None


def _control_ref(db):
    return db.collection(CONTROL_COLLECTION).document(CONTROL_DOC_ID)


def _scan(txn, db):
    cs = read_document(_control_ref(db), txn)
    control = validate_control_record(_data(cs))
    now = extract_snapshot_time(cs)
    ref = db.collection(SCAN_CURSOR_COLLECTION).document(SCAN_CURSOR_DOC_ID)
    snap = read_document(ref, txn)
    scan = validate_scan_cursor_record(snap.to_dict()) if snap.exists else None
    return control, now, ref, scan


def _cursor(scan):
    return scan["cursor_work_id"] if scan else None


def _scan_payload(cursor, now):
    payload = {"schema_version": 1, "cursor_work_id": cursor, "updated_at": now}
    validate_scan_cursor_record(payload)
    return payload


def _review_ref(db, work_id):
    key = hashlib.sha256(json.dumps(["jobber_work_review", 1, work_id],
                                    separators=(",", ":")).encode()).hexdigest()
    return db.collection(REVIEW_COLLECTION).document(key)


def _review_payload(work_id, now):
    return {"schema_version": 1, "work_document_digest": _review_digest(work_id),
            "reason": "invalid_work_record", "status": "needs_review", "created_at": now}


def _review_digest(work_id):
    return hashlib.sha256(work_id.encode()).hexdigest()


def _validate_review(data, work_id):
    if (type(data) is not dict or set(data) != {"schema_version", "work_document_digest", "reason", "status", "created_at"}
            or type(data["schema_version"]) is not int or data["schema_version"] != 1
            or data["work_document_digest"] != _review_digest(work_id)
            or data["reason"] != "invalid_work_record" or data["status"] != "needs_review"
            or type(data["created_at"]) is not float or not 0 < data["created_at"] < float("inf")):
        raise JobberDisconnectWorkError("Invalid poison review marker")


def acquire_work_lease_atomic(db: Any, worker_id: str,
                              lease_duration_seconds: float = DEFAULT_LEASE_DURATION_SECONDS,
                              now_ts: float | None = None, *, deadline: Deadline | None = None) -> dict | None:
    if not is_safe_document_id(worker_id) or now_ts is not None:
        raise JobberDisconnectWorkError("Invalid acquisition context")
    if type(lease_duration_seconds) not in (float, int) or not 1 <= lease_duration_seconds <= 60:
        raise JobberDisconnectWorkError("Invalid lease duration")
    deadline = deadline or Deadline(20)
    _, _, _, scan = run_transaction(db, lambda t: _scan(t, db), deadline=deadline, read_only=True)
    expected_cursor = _cursor(scan)
    query = db.collection(WORK_COLLECTION).order_by("__name__").limit(CANDIDATE_SCAN_LIMIT)
    if expected_cursor:
        query = query.start_after({"__name__": db.collection(WORK_COLLECTION).document(expected_cursor)})
    candidates = stream_query(query, deadline)
    if not candidates and expected_cursor is not None:
        candidates = stream_query(db.collection(WORK_COLLECTION).order_by("__name__").limit(CANDIDATE_SCAN_LIMIT), deadline)
    # A new nonce for each acquisition, including reuse of a caller's worker label.
    lease_nonce = "lease_" + uuid.uuid4().hex
    for candidate in candidates:
        deadline.timeout()
        candidate_id = candidate.id
        if not is_firestore_document_id(candidate_id):
            raise JobberDisconnectWorkError("Unsafe work document ID")

        def acquire(txn):
            control, now, scan_ref, current_scan = _scan(txn, db)
            if _cursor(current_scan) != expected_cursor:
                return "cursor_changed", None
            work_ref = db.collection(WORK_COLLECTION).document(candidate_id)
            work_snap = read_document(work_ref, txn)
            if not work_snap.exists:
                txn.set(scan_ref, _scan_payload(candidate_id, now))
                return "advanced", None
            review_ref = _review_ref(db, candidate_id)
            review_snap = read_document(review_ref, txn)
            repair_review = False
            if review_snap.exists:
                try:
                    _validate_review(review_snap.to_dict(), candidate_id)
                except JobberDisconnectWorkError:
                    # Diagnostic corruption must never turn a poison row into a queue lock.
                    repair_review = True
            invalid = False
            try:
                work = validate_work_record(work_snap.to_dict())
                if work["work_id"] != candidate_id:
                    raise JobberDisconnectWorkError("Work path mismatch")
                barrier_ref = db.collection(BARRIERS_COLLECTION).document(
                    compute_account_barrier_key(work["app_id"], work["account_id"]))
                barrier_snap = read_document(barrier_ref, txn)
                validate_account_records(control, _data(barrier_snap), work,
                                         app_id=work["app_id"], account_id=work["account_id"])
            except JobberSnapshotTimeError:
                raise
            except JobberDisconnectError:
                invalid = True
            # All authoritative reads precede these atomic marker/cursor writes.
            if repair_review:
                txn.set(review_ref, _review_payload(candidate_id, now))
            if invalid:
                if not review_snap.exists:
                    txn.create(review_ref, _review_payload(candidate_id, now))
                txn.set(scan_ref, _scan_payload(candidate_id, now))
                return "advanced", None
            eligible = (work["status"] == "pending" or
                        (work["status"] == "processing" and work["lease_expires_at"] <= now))
            if not eligible or work["next_attempt_at"] > now:
                txn.set(scan_ref, _scan_payload(candidate_id, now))
                return "advanced", None
            attempts = work["attempt_count"] + int(work["status"] == "processing")
            if attempts >= MAX_WORK_ATTEMPTS:
                updates = {"status": "needs_review", "lease_id": None, "lease_expires_at": None,
                           "attempt_count": MAX_WORK_ATTEMPTS, "updated_at": now}
                validate_work_record({**work, **updates})
                txn.update(work_ref, updates)
                txn.set(scan_ref, _scan_payload(candidate_id, now))
                return "advanced", None
            updates = {"status": "processing", "lease_id": lease_nonce,
                       "lease_expires_at": now + float(lease_duration_seconds), "updated_at": now,
                       "attempt_count": attempts}
            result = {**work, **updates}
            validate_work_record(result)
            txn.update(work_ref, updates)
            txn.set(scan_ref, _scan_payload(candidate_id, now))
            return "acquired", result

        disposition, work = run_transaction(db, acquire, deadline=deadline)
        if disposition == "cursor_changed":
            return None
        expected_cursor = candidate_id
        if work is not None:
            return work
    return None


def _leased_context(txn, db, work_id, lease_id, expected_barrier, expected_app_id, expected_account_id):
    cs = read_document(_control_ref(db), txn)
    barrier_ref = db.collection(BARRIERS_COLLECTION).document(
        compute_account_barrier_key(expected_app_id, expected_account_id))
    bs = read_document(barrier_ref, txn)
    work_ref = db.collection(WORK_COLLECTION).document(work_id)
    ws = read_document(work_ref, txn)
    c, b, w = validate_account_records(_data(cs), _data(bs), _data(ws),
                                      app_id=expected_app_id, account_id=expected_account_id)
    now = extract_snapshot_time(cs)
    if (work_id != compute_work_doc_id(expected_app_id, expected_account_id)
            or w["barrier"] != expected_barrier or w["status"] != "processing"
            or w["lease_id"] != lease_id or w["lease_expires_at"] <= now):
        raise JobberDisconnectWorkError("Lease stale, expired or superseded")
    return work_ref, w, now


def _bounded(value):
    return type(value) is int and 0 <= value <= MAX_RECEIPT_COUNTER


def _binding_state(data, expected_barrier):
    ok, gen, epoch, present, _ = parse_durable_lifecycle_counters(data, "jobber")
    if not ok or not present:
        raise JobberDisconnectWorkError("Malformed binding lifecycle")
    accepted = data.get("jobber_accepted_disconnect_barrier")
    if not _bounded(accepted) or accepted > expected_barrier:
        raise JobberDisconnectWorkError("Malformed accepted barrier")
    state_present = "jobber_disconnect_state" in data
    processed_present = "jobber_disconnect_processed_barrier" in data
    if state_present != processed_present:
        raise JobberDisconnectWorkError("Partial suspension metadata")
    if state_present:
        processed = data["jobber_disconnect_processed_barrier"]
        if (data["jobber_disconnect_state"] != "reauthorization_required"
                or not _bounded(processed) or not 1 <= processed <= expected_barrier
                or processed <= accepted or data["jobber_connected"] is not False
                or data.get("jobber_lead_capture_enabled") is not False):
            raise JobberDisconnectWorkError("Malformed suspended binding")
        if processed == expected_barrier:
            if any(key in data for key in get_provider_operation_intent_keys("jobber")):
                raise JobberDisconnectWorkError("Suspended binding retains stale claims")
            return "already_processed", gen, epoch
    elif accepted == expected_barrier:
        return "fresh_reconnected", gen, epoch
    return "stale", gen, epoch


def _audit_payload(cid, app, account, barrier, gen, epoch, now):
    audit_id = compute_suspension_audit_doc_id(cid, app, account, barrier, gen, epoch)
    return {"schema_version": 1, "audit_id": audit_id, "contractor_id": cid,
            "app_id": app, "account_id": account, "barrier": barrier, "generation": gen,
            "lifecycle_epoch": epoch, "binding_suspended": True, "credentials_retained": True,
            "capture_disabled": True, "provider_revocation_attempted": False,
            "created_at": now, "timestamp": now}


def advance_contractor_disconnect_in_txn(txn, db, contractor_id, work_id, expected_worker_id,
                                        expected_barrier, expected_app_id, expected_account_id):
    _, _, now = _leased_context(txn, db, work_id, expected_worker_id, expected_barrier,
                                expected_app_id, expected_account_id)
    ref = db.collection("contractors").document(contractor_id)
    snap = read_document(ref, txn)
    if not snap.exists:
        return False, "membership_changed"
    data = snap.to_dict()
    if type(data) is not dict:
        raise JobberDisconnectWorkError("Malformed contractor")
    if data.get("jobber_app_id") != expected_app_id or data.get("jobber_account_id") != expected_account_id:
        return False, "membership_changed"
    state, gen, epoch = _binding_state(data, expected_barrier)
    if state == "fresh_reconnected":
        return False, state
    if state == "already_processed":
        expected = _audit_payload(contractor_id, expected_app_id, expected_account_id, expected_barrier, gen, epoch, now)
        audit = read_document(db.collection(SUSPENSION_AUDIT_COLLECTION).document(expected["audit_id"]), txn)
        actual = validate_suspension_audit_record(_data(audit), expected["audit_id"])
        if any(actual[k] != expected[k] for k in expected if k not in ("created_at", "timestamp")):
            raise JobberSuspensionAuditError("Suspension evidence mismatch")
        return False, state
    if gen >= MAX_KEY_VERSION or epoch >= MAX_KEY_VERSION:
        raise JobberLifecycleOverflowError("Lifecycle overflow")
    payload = _audit_payload(contractor_id, expected_app_id, expected_account_id,
                             expected_barrier, gen + 1, epoch + 1, now)
    audit_ref = db.collection(SUSPENSION_AUDIT_COLLECTION).document(payload["audit_id"])
    audit_snap = read_document(audit_ref, txn)
    if audit_snap.exists:
        validate_suspension_audit_record(audit_snap.to_dict(), payload["audit_id"])
        # No audit may preexist without its atomic contractor transition.
        raise JobberSuspensionAuditError("Conflicting suspension audit")
    updates = {key: DELETE_FIELD for key in get_provider_operation_intent_keys("jobber")}
    updates.update({"jobber_generation": gen + 1, "jobber_lifecycle_epoch": epoch + 1,
                    "jobber_connected": False, "jobber_lead_capture_enabled": False,
                    "jobber_disconnect_state": "reauthorization_required",
                    "jobber_disconnect_processed_barrier": expected_barrier,
                    "jobber_disconnected_at": now, "jobber_disconnect_pending": False})
    validate_suspension_audit_record(payload, payload["audit_id"])
    txn.create(audit_ref, payload)
    txn.update(ref, updates)
    return True, "advanced"


def _readback_binding(txn, db, cid, work, retained=None):
    # Same barrier may have been superseded; readback must never overwrite it.
    cs = read_document(_control_ref(db), txn)
    bs = read_document(db.collection(BARRIERS_COLLECTION).document(
        compute_account_barrier_key(work["app_id"], work["account_id"])), txn)
    ws = read_document(db.collection(WORK_COLLECTION).document(work["work_id"]), txn)
    validate_account_records(_data(cs), _data(bs), _data(ws), app_id=work["app_id"], account_id=work["account_id"])
    snap = read_document(db.collection("contractors").document(cid), txn)
    if not snap.exists:
        return
    data = snap.to_dict()
    if data.get("jobber_app_id") != work["app_id"] or data.get("jobber_account_id") != work["account_id"]:
        return
    current_barrier = bs.to_dict()["barrier"]
    state, gen, epoch = _binding_state(data, current_barrier)
    if state == "fresh_reconnected":
        return
    processed = data.get("jobber_disconnect_processed_barrier")
    if not _bounded(processed) or processed < work["barrier"]:
        raise JobberDisconnectWorkError("Suspension postcondition missing")
    expected = _audit_payload(cid, work["app_id"], work["account_id"], processed, gen, epoch, extract_snapshot_time(cs))
    audit = read_document(db.collection(SUSPENSION_AUDIT_COLLECTION).document(expected["audit_id"]), txn)
    actual = validate_suspension_audit_record(_data(audit), expected["audit_id"])
    if any(actual[k] != expected[k] for k in expected if k not in ("created_at", "timestamp")):
        raise JobberSuspensionAuditError("Suspension postcondition mismatch")
    if retained is not None and gen == retained["generation"] and epoch == retained["epoch"]:
        for key, (was_present, value) in retained["fields"].items():
            if (key in data) != was_present or (was_present and (type(data[key]) is not type(value) or data[key] != value)):
                raise JobberDisconnectWorkError("Credential retention postcondition mismatch")


def process_work_batch(db, work, worker_id, page_size=DEFAULT_PAGE_SIZE, *, deadline=None):
    if type(page_size) is not int or not 1 <= page_size <= DEFAULT_PAGE_SIZE:
        raise JobberDisconnectWorkError("Invalid page bound")
    deadline = deadline or Deadline(20)
    validate_work_record(work)
    query = (db.collection("contractors").where(filter=FieldFilter("jobber_account_id", "==", work["account_id"]))
             .order_by("__name__").limit(page_size))
    if work["cursor"]:
        query = query.start_after({"__name__": db.collection("contractors").document(work["cursor"])})
    snapshots = stream_query(query, deadline)
    processed = 0
    for snap in snapshots:
        deadline.timeout()
        retained = {}
        def advance(txn):
            retained.clear()
            before = read_document(db.collection("contractors").document(snap.id), txn)
            before_data = before.to_dict() if before.exists else {}
            result = advance_contractor_disconnect_in_txn(txn, db, snap.id, work["work_id"],
                worker_id, work["barrier"], work["app_id"], work["account_id"])
            if result[0]:
                retained.update({"generation": before_data["jobber_generation"] + 1,
                                 "epoch": before_data["jobber_lifecycle_epoch"] + 1,
                                 "fields": {key: (key in before_data, before_data.get(key)) for key in
                                     ("jobber_access_token", "jobber_refresh_token", "jobber_token_envelope_required")}})
            return result
        try:
            advanced, _ = run_transaction(db, advance, deadline=deadline)
        except JobberDisconnectError:
            raise
        except Exception:
            run_transaction(db, lambda t: _readback_binding(t, db, snap.id, work, retained or None), deadline=deadline, read_only=True)
            advanced = False
        else:
            run_transaction(db, lambda t: _readback_binding(t, db, snap.id, work, retained or None), deadline=deadline, read_only=True)
        processed += int(advanced)
    return processed, len(snapshots) == page_size, snapshots[-1].id if snapshots else None


def finalize_work_lease_atomic(db, work_id, worker_id, has_more, last_cursor, *,
                               expected_work: dict, deadline=None):
    deadline = deadline or Deadline()
    def finalize(txn):
        ref, work, now = _leased_context(txn, db, work_id, worker_id, expected_work["barrier"],
                                        expected_work["app_id"], expected_work["account_id"])
        updates = {"cursor": last_cursor if has_more else None, "lease_id": None,
                   "lease_expires_at": None, "status": "pending" if has_more else "completed",
                   "attempt_count": 0, "next_attempt_at": now, "updated_at": now}
        validate_work_record({**work, **updates})
        txn.update(ref, updates)
    run_transaction(db, finalize, deadline=deadline)


def release_work_lease_on_failure_atomic(db, work_id, worker_id, error_msg=None, *, expected_work: dict, deadline=None):
    deadline = deadline or Deadline()
    def release(txn):
        ref, work, now = _leased_context(txn, db, work_id, worker_id, expected_work["barrier"],
                                        expected_work["app_id"], expected_work["account_id"])
        attempts = work["attempt_count"] + 1
        updates = {"status": "needs_review" if attempts >= MAX_WORK_ATTEMPTS else "pending",
                   "lease_id": None, "lease_expires_at": None, "attempt_count": attempts,
                   "next_attempt_at": now + min(300.0, 5.0 * 2 ** attempts), "updated_at": now}
        validate_work_record({**work, **updates})
        txn.update(ref, updates)
    run_transaction(db, release, deadline=deadline)


def _sync_run_recovery_once(db, worker_id, deadline):
    work = acquire_work_lease_atomic(db, worker_id, deadline=deadline)
    if work is None:
        return 0
    lease_id = work["lease_id"]
    try:
        processed, more, cursor = process_work_batch(db, work, lease_id, deadline=deadline)
        finalize_work_lease_atomic(db, work["work_id"], lease_id, more, cursor,
                                  expected_work=work, deadline=deadline)
        return processed
    except Exception:
        try:
            release_work_lease_on_failure_atomic(db, work["work_id"], lease_id,
                                                expected_work=work, deadline=deadline)
        except Exception:
            # Durable lease expiry permits later recovery; never log payloads.
            logger.warning("Jobber recovery deferred")
        return 0


async def run_jobber_disconnect_recovery_once(db=None, clock=None):
    if clock is not None:
        raise ValueError("Inject aware server snapshot time through the datastore")
    worker_id = "worker_" + uuid.uuid4().hex
    return await run_bounded_async(lambda deadline: _sync_run_recovery_once(
        db if db is not None else get_firestore_client(), worker_id, deadline), seconds=20.0)


async def jobber_disconnect_recovery_worker_loop(db=None, poll_interval=10.0, clock=None):
    while True:
        try:
            await run_jobber_disconnect_recovery_once(db, clock)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Jobber recovery pass deferred")
        await asyncio.sleep(poll_interval)
