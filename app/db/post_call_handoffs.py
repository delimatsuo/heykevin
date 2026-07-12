"""Firestore outbox state for replay-safe post-call processing."""

import asyncio
import time

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "post_call_handoffs"
DEFAULT_LEASE_SECONDS = 180
TERMINAL_STATUSES = frozenset({"completed", "needs_attention"})


def _call_label(call_sid: str) -> str:
    return str(call_sid or "")[:8] or "unknown"


def _claim_transition(
    data: dict,
    *,
    now: float,
    lease_seconds: int,
) -> tuple[bool, dict]:
    """Return an atomic claim decision without replaying uncertain work."""
    status = data.get("status")
    if status in TERMINAL_STATUSES:
        return False, {}
    if status == "in_progress":
        lease_expires_at = data.get("lease_expires_at")
        if not isinstance(lease_expires_at, (int, float)) or lease_expires_at <= now:
            return False, {
                "status": "needs_attention",
                "finished_at": now,
                "lease_expires_at": 0,
                "failure_code": "lease_expired_uncertain",
            }
        return False, {}
    if status != "pending":
        return False, {}

    attempts = data.get("attempts", 0)
    if not isinstance(attempts, int) or attempts < 0:
        attempts = 0
    return True, {
        "status": "in_progress",
        "attempts": attempts + 1,
        "started_at": now,
        "lease_expires_at": now + lease_seconds,
    }


async def enqueue_handoff(
    *,
    call_sid: str,
    contractor_id: str = "",
    caller_language: str = "en",
) -> bool:
    """Create one durable pending handoff without resetting existing state."""
    if not call_sid:
        return False

    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(call_sid)
    payload = {
        "call_sid": call_sid,
        "contractor_id": contractor_id,
        "caller_language": caller_language or "en",
        "status": "pending",
        "attempts": 0,
        "created_at": time.time(),
    }

    def _create() -> bool:
        try:
            doc_ref.create(payload)
        except AlreadyExists:
            return True
        return True

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _create)
    except Exception as error:
        logger.error(
            "post_call_handoff enqueue failed call=%s exception_type=%s",
            _call_label(call_sid),
            type(error).__name__,
        )
        return False


async def claim_handoff(
    call_sid: str,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Claim pending work once; stale in-progress work becomes uncertain."""
    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(call_sid)

    def _claim() -> bool:
        transaction = db.transaction()

        @firestore.transactional
        def _transactional_claim(tx) -> bool:
            snapshot = doc_ref.get(transaction=tx)
            if not snapshot.exists:
                return False
            claimed, updates = _claim_transition(
                snapshot.to_dict() or {},
                now=time.time(),
                lease_seconds=lease_seconds,
            )
            if updates:
                tx.update(doc_ref, updates)
            return claimed

        return _transactional_claim(transaction)

    return await asyncio.get_running_loop().run_in_executor(None, _claim)


def terminal_outcome(result_status: str) -> tuple[str, str]:
    """Map a processing result to safe persisted terminal state."""
    if result_status == "complete":
        return "completed", ""
    if result_status == "partial":
        return "needs_attention", "partial_delivery"
    return "needs_attention", "processing_failed"


def _finish_transition(data: dict, updates: dict) -> tuple[bool, dict]:
    """Allow only an in-progress claim to publish its terminal result."""
    current_status = data.get("status")
    target_status = updates.get("status")
    if current_status == target_status and current_status in TERMINAL_STATUSES:
        return True, {}
    if current_status in TERMINAL_STATUSES or current_status != "in_progress":
        return False, {}
    return True, updates


def _attention_transition(data: dict, updates: dict) -> tuple[bool, dict]:
    """Quarantine non-terminal work without overwriting completed work."""
    current_status = data.get("status")
    if current_status == "needs_attention":
        return True, {}
    if current_status == "completed":
        return False, {}
    if current_status not in {"pending", "in_progress"}:
        return False, {}
    return True, updates


async def finish_handoff(call_sid: str, result) -> bool:
    """Persist a terminal aggregate outcome without caller content."""
    status, failure_code = terminal_outcome(result.status)
    updates = {
        "status": status,
        "finished_at": time.time(),
        "lease_expires_at": 0,
        "failure_code": failure_code,
        "completed_effects": list(result.completed_effects),
        "failed_effects": list(result.failed_effects),
    }
    try:
        db = get_firestore_client()
        doc_ref = db.collection(COLLECTION).document(call_sid)

        def _finish() -> bool:
            transaction = db.transaction()

            @firestore.transactional
            def _transactional_finish(tx) -> bool:
                snapshot = doc_ref.get(transaction=tx)
                if not snapshot.exists:
                    return False
                accepted, terminal_updates = _finish_transition(
                    snapshot.to_dict() or {},
                    updates,
                )
                if terminal_updates:
                    tx.update(doc_ref, terminal_updates)
                return accepted

            return _transactional_finish(transaction)

        return await asyncio.get_running_loop().run_in_executor(None, _finish)
    except Exception as error:
        logger.error(
            "post_call_handoff finish failed call=%s exception_type=%s",
            _call_label(call_sid),
            type(error).__name__,
        )
        return False


async def mark_needs_attention(call_sid: str, failure_code: str) -> bool:
    """Record a safe terminal failure code for operator follow-up."""
    updates = {
        "status": "needs_attention",
        "finished_at": time.time(),
        "lease_expires_at": 0,
        "failure_code": failure_code,
    }
    try:
        db = get_firestore_client()
        doc_ref = db.collection(COLLECTION).document(call_sid)

        def _mark() -> bool:
            transaction = db.transaction()

            @firestore.transactional
            def _transactional_mark(tx) -> bool:
                snapshot = doc_ref.get(transaction=tx)
                if not snapshot.exists:
                    return False
                accepted, terminal_updates = _attention_transition(
                    snapshot.to_dict() or {},
                    updates,
                )
                if terminal_updates:
                    tx.update(doc_ref, terminal_updates)
                return accepted

            return _transactional_mark(transaction)

        return await asyncio.get_running_loop().run_in_executor(None, _mark)
    except Exception as error:
        logger.error(
            "post_call_handoff status failed call=%s exception_type=%s",
            _call_label(call_sid),
            type(error).__name__,
        )
        return False


async def get_handoff(call_sid: str) -> dict | None:
    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(call_sid)
    snapshot = await asyncio.get_running_loop().run_in_executor(None, doc_ref.get)
    if not snapshot.exists:
        return None
    return snapshot.to_dict() or {}


async def list_handoff_ids(status: str, *, limit: int = 10) -> list[str]:
    """List bounded work IDs by status without returning payload data."""
    db = get_firestore_client()
    bounded_limit = max(1, min(int(limit), 100))

    def _query():
        return list(
            db.collection(COLLECTION)
            .where(filter=FieldFilter("status", "==", status))
            .limit(bounded_limit)
            .stream()
        )

    docs = await asyncio.get_running_loop().run_in_executor(None, _query)
    return [doc.id for doc in docs]
