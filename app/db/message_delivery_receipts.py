"""Durable state for outbound message delivery receipts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
import time

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger


logger = get_logger(__name__)


COLLECTION = "message_delivery_receipts"
CALLS_COLLECTION = "calls"
RETENTION_DAYS = 90
RECONCILIATION_DELAY_SECONDS = 12 * 60 * 60
RECONCILIATION_INTERVAL_SECONDS = 60 * 60
RECONCILIATION_LEASE_SECONDS = 60.0
PROJECTION_RETRY_SECONDS = 60.0

TRACKED_EFFECTS = frozenset(
    {
        "caller_auto_reply",
        "caller_confirmation",
        "caller_vcard",
        "owner_sms",
    }
)
VALID_CHANNELS = frozenset({"sms", "mms"})
VALID_RECEIPT_STATUSES = frozenset(
    {"pending", "delivered", "failed", "acknowledged"}
)
PROVIDER_STATUSES = frozenset(
    {
        "unknown",
        "accepted",
        "scheduled",
        "queued",
        "sending",
        "sent",
        "delivered",
        "read",
        "failed",
        "undelivered",
        "canceled",
    }
)
PROVIDER_STATUS_RANK = {
    "unknown": 0,
    "accepted": 1,
    "scheduled": 1,
    "queued": 2,
    "sending": 3,
    "sent": 4,
}
DELIVERED_PROVIDER_STATUSES = frozenset({"delivered", "read"})
FAILED_PROVIDER_STATUSES = frozenset({"failed", "undelivered", "canceled"})
FAILURE_CODES = frozenset(
    {
        "",
        "conflicting_terminal_status",
        "provider_delivery_failed",
        "reconciliation_missing_provider_id",
        "submission_failed",
    }
)
ACKNOWLEDGEMENT_RESOLUTIONS = frozenset(
    {
        "customer_contacted_manually",
        "no_action_required",
        "provider_confirmed_delivery",
    }
)

_SAFE_IDENTIFIER_PATTERN = re.compile(r"[^a-zA-Z0-9_.:-]+")
_MESSAGE_SID_PATTERN = re.compile(r"^(?:SM|MM)[A-Za-z0-9]{32}$")


@dataclass(frozen=True)
class ReceiptUpdate:
    outcome: str
    summary: dict | None = None


@dataclass(frozen=True)
class ReconciliationCandidate:
    receipt_id: str
    provider_message_sid: str
    lease_token: str = ""


def _bounded_identifier(value: object, *, limit: int = 128) -> str:
    return _SAFE_IDENTIFIER_PATTERN.sub("_", str(value or "")[:limit]).strip(
        "_.:-"
    )


def _safe_timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _safe_positive_timestamp(value: object) -> float | None:
    timestamp = _safe_timestamp(value)
    return timestamp if timestamp is not None and timestamp > 0 else None


def _safe_provider_error_code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        code = int(value)
    except (TypeError, ValueError):
        return None
    return code if 0 <= code <= 99999 else None


def _retention_deadline(now: float) -> datetime:
    return datetime.fromtimestamp(now, tz=timezone.utc) + timedelta(
        days=RETENTION_DAYS
    )


def _reconciliation_schedule(now: float) -> dict:
    return {
        "last_reconciled_at": now,
        "next_reconcile_at": now + RECONCILIATION_INTERVAL_SECONDS,
        "reconcile_lease_token": "",
        "reconcile_lease_expires_at": 0.0,
        "updated_at": now,
    }


def _projection_updates(data: dict, *, now: float) -> dict:
    current_version = data.get("call_projection_version", 0)
    if (
        isinstance(current_version, bool)
        or not isinstance(current_version, int)
        or current_version < 0
    ):
        current_version = 0
    return {
        "call_projection_pending": True,
        "call_projection_version": current_version + 1,
        "call_projection_next_at": now,
    }


def _projection_defer_transition(data: dict, *, now: float) -> tuple[bool, dict]:
    if data.get("call_projection_pending") is not True:
        return False, {}
    return True, {
        "call_projection_next_at": now + PROJECTION_RETRY_SECONDS,
        "call_projection_last_attempt_at": now,
    }


def _clear_reconciliation_lease() -> dict:
    return {
        "reconcile_lease_token": "",
        "reconcile_lease_expires_at": 0.0,
    }


def _reconciliation_claim_transition(
    data: dict,
    *,
    lease_token: str,
    now: float,
) -> tuple[bool, dict]:
    next_reconcile_at = _safe_timestamp(data.get("next_reconcile_at"))
    lease_expires_at = _safe_timestamp(data.get("reconcile_lease_expires_at"))
    active_lease = bool(data.get("reconcile_lease_token")) and (
        lease_expires_at is not None and lease_expires_at > now
    )
    if (
        data.get("status") != "pending"
        or not lease_token
        or next_reconcile_at is None
        or next_reconcile_at > now
        or active_lease
    ):
        return False, {}
    lease_deadline = now + RECONCILIATION_LEASE_SECONDS
    return True, {
        "reconcile_lease_token": lease_token,
        "reconcile_lease_expires_at": lease_deadline,
        "next_reconcile_at": lease_deadline,
        "updated_at": now,
    }


def _reconciliation_completion_updates(
    data: dict,
    *,
    lease_token: str,
    now: float,
) -> dict | None:
    stored_lease_token = str(data.get("reconcile_lease_token") or "")
    if data.get("status") != "pending" or stored_lease_token != lease_token:
        return None
    return _reconciliation_schedule(now)


def _provider_message_hash(provider_message_sid: str) -> str:
    if not isinstance(provider_message_sid, str) or not _MESSAGE_SID_PATTERN.fullmatch(
        provider_message_sid
    ):
        return ""
    return hashlib.sha256(provider_message_sid.encode("ascii")).hexdigest()


def safe_receipt_summary(receipt_id: str, data: dict) -> dict:
    """Return an allowlisted operational summary without message payload data."""
    effect = data.get("effect")
    channel = data.get("channel")
    status = data.get("status")
    provider_status = data.get("provider_status")
    failure_code = data.get("failure_code", "")
    resolution = data.get("resolution", "")
    return {
        "receipt_id": _bounded_identifier(receipt_id),
        "call_sid": _bounded_identifier(data.get("call_sid")),
        "effect": (
            effect
            if isinstance(effect, str) and effect in TRACKED_EFFECTS
            else "unknown"
        ),
        "channel": (
            channel
            if isinstance(channel, str) and channel in VALID_CHANNELS
            else "unknown"
        ),
        "status": (
            status
            if isinstance(status, str) and status in VALID_RECEIPT_STATUSES
            else "unknown"
        ),
        "provider_status": (
            provider_status
            if isinstance(provider_status, str)
            and provider_status in PROVIDER_STATUSES
            else "unknown"
        ),
        "provider_error_code": _safe_provider_error_code(
            data.get("provider_error_code")
        ),
        "failure_code": (
            failure_code
            if isinstance(failure_code, str) and failure_code in FAILURE_CODES
            else "unknown"
        ),
        "created_at": _safe_timestamp(data.get("created_at")),
        "updated_at": _safe_timestamp(data.get("updated_at")),
        "next_reconcile_at": _safe_timestamp(data.get("next_reconcile_at")),
        "finished_at": _safe_timestamp(data.get("finished_at")),
        "acknowledged_at": _safe_timestamp(data.get("acknowledged_at")),
        "last_reconciled_at": _safe_timestamp(data.get("last_reconciled_at")),
        "call_projection_pending": data.get("call_projection_pending") is True,
        "call_projection_version": (
            data.get("call_projection_version")
            if isinstance(data.get("call_projection_version"), int)
            and not isinstance(data.get("call_projection_version"), bool)
            and data.get("call_projection_version") >= 0
            else 0
        ),
        "call_projected_at": _safe_timestamp(data.get("call_projected_at")),
        "call_projection_next_at": _safe_positive_timestamp(
            data.get("call_projection_next_at")
        ),
        "call_projection_last_attempt_at": _safe_timestamp(
            data.get("call_projection_last_attempt_at")
        ),
        "resolution": (
            resolution
            if isinstance(resolution, str)
            and resolution in ACKNOWLEDGEMENT_RESOLUTIONS
            else ""
        ),
    }


def call_record_delivery_updates(
    summary: dict,
    *,
    resolution: str = "",
) -> dict:
    """Return allowlisted effect-scoped call fields that cannot clobber peers."""
    effect = summary.get("effect")
    if not isinstance(effect, str) or effect not in TRACKED_EFFECTS:
        return {}
    prefix = f"post_call_delivery_{effect}"
    failure_code = summary.get("failure_code", "")
    if not isinstance(failure_code, str) or failure_code not in FAILURE_CODES:
        failure_code = "unknown"
    if resolution:
        if (
            not isinstance(resolution, str)
            or resolution not in ACKNOWLEDGEMENT_RESOLUTIONS
        ):
            return {}
        return {
            f"{prefix}_status": "acknowledged",
            f"{prefix}_failure_code": failure_code,
            f"{prefix}_resolution": resolution,
        }

    status = summary.get("status")
    provider_status = summary.get("provider_status")
    updates = {
        f"{prefix}_status": (
            status
            if isinstance(status, str) and status in VALID_RECEIPT_STATUSES
            else "unknown"
        ),
        f"{prefix}_provider_status": (
            provider_status
            if isinstance(provider_status, str)
            and provider_status in PROVIDER_STATUSES
            else "unknown"
        ),
        f"{prefix}_failure_code": failure_code,
        f"{prefix}_error_code": _safe_provider_error_code(
            summary.get("provider_error_code")
        ),
        f"{prefix}_updated_at": _safe_timestamp(summary.get("updated_at")),
    }
    return updates


def _provider_transition(
    data: dict,
    *,
    provider_status: str,
    provider_message_sid: str,
    provider_error_code: object,
    now: float,
) -> tuple[str, dict]:
    """Return a monotonic provider update without trusting callback payloads."""
    normalized_status = str(provider_status or "").lower()
    provider_hash = _provider_message_hash(provider_message_sid)
    if normalized_status not in PROVIDER_STATUSES or not provider_hash:
        return "invalid", {}

    stored_hash = str(data.get("provider_message_hash") or "")
    if stored_hash and stored_hash != provider_hash:
        return "invalid", {}

    current_status = data.get("status")
    current_provider_status = data.get("provider_status", "unknown")
    if (
        not isinstance(current_status, str)
        or current_status not in VALID_RECEIPT_STATUSES
    ):
        return "invalid", {}
    if (
        not isinstance(current_provider_status, str)
        or current_provider_status not in PROVIDER_STATUSES
    ):
        current_provider_status = "unknown"
    if current_status == "acknowledged":
        return "ignored", {}

    identity_updates = {
        "provider_message_sid": provider_message_sid,
        "provider_message_hash": provider_hash,
    }
    target_status = (
        "delivered"
        if normalized_status in DELIVERED_PROVIDER_STATUSES
        else "failed"
        if normalized_status in FAILED_PROVIDER_STATUSES
        else "pending"
    )

    if current_status in {"delivered", "failed"}:
        if current_status == target_status:
            if (
                current_status == "delivered"
                and normalized_status == "read"
                and current_provider_status != "read"
            ):
                return "updated", {
                    **identity_updates,
                    "provider_status": "read",
                    "updated_at": now,
                    **_projection_updates(data, now=now),
                }
            return "ignored", {}
        if target_status == "pending":
            return "ignored", {}
        return "conflict", {
            **identity_updates,
            "status": "failed",
            "failure_code": "conflicting_terminal_status",
            "updated_at": now,
            "finished_at": now,
            "expires_at": _retention_deadline(now),
            **_clear_reconciliation_lease(),
            **_projection_updates(data, now=now),
        }

    if target_status == "pending":
        current_rank = PROVIDER_STATUS_RANK.get(str(current_provider_status), 0)
        target_rank = PROVIDER_STATUS_RANK.get(normalized_status, 0)
        identity_missing = not stored_hash
        if target_rank < current_rank or (
            target_rank == current_rank and not identity_missing
        ):
            return "ignored", {}
        return "updated", {
            **identity_updates,
            "status": "pending",
            "provider_status": normalized_status,
            "updated_at": now,
            **_projection_updates(data, now=now),
        }

    updates = {
        **identity_updates,
        "status": target_status,
        "provider_status": normalized_status,
        "updated_at": now,
        "finished_at": now,
        "expires_at": _retention_deadline(now),
        **_clear_reconciliation_lease(),
        **_projection_updates(data, now=now),
    }
    if target_status == "failed":
        updates["failure_code"] = "provider_delivery_failed"
        error_code = _safe_provider_error_code(provider_error_code)
        if error_code is not None:
            updates["provider_error_code"] = error_code
    return "updated", updates


def _submission_failure_transition(data: dict, *, now: float) -> tuple[bool, dict]:
    if data.get("status") == "failed" and data.get("failure_code") == "submission_failed":
        return True, {}
    if data.get("status") != "pending":
        return False, {}
    return True, {
        "status": "failed",
        "provider_status": "failed",
        "failure_code": "submission_failed",
        "updated_at": now,
        "finished_at": now,
        "expires_at": _retention_deadline(now),
        **_clear_reconciliation_lease(),
        **_projection_updates(data, now=now),
    }


def _acknowledge_transition(
    data: dict,
    *,
    resolution: str,
    now: float,
) -> tuple[bool, dict]:
    if (
        not isinstance(resolution, str)
        or resolution not in ACKNOWLEDGEMENT_RESOLUTIONS
    ):
        return False, {}
    if data.get("status") != "failed":
        return False, {}
    return True, {
        "status": "acknowledged",
        "resolution": resolution,
        "acknowledged_at": now,
        "updated_at": now,
        "expires_at": _retention_deadline(now),
        **_clear_reconciliation_lease(),
        **_projection_updates(data, now=now),
    }


def _missing_provider_id_transition(
    data: dict,
    *,
    now: float,
) -> tuple[bool, dict]:
    if data.get("status") != "pending" or _provider_message_hash(
        data.get("provider_message_sid", "")
    ):
        return False, {}
    return True, {
        "status": "failed",
        "failure_code": "reconciliation_missing_provider_id",
        "updated_at": now,
        "finished_at": now,
        "last_reconciled_at": now,
        "expires_at": _retention_deadline(now),
        **_clear_reconciliation_lease(),
        **_projection_updates(data, now=now),
    }


async def create_receipt(
    *,
    call_sid: str,
    effect: str,
    channel: str,
) -> str:
    """Pre-register an opaque callback target before any provider send."""
    if (
        not isinstance(call_sid, str)
        or not call_sid
        or len(call_sid) > 128
        or not isinstance(effect, str)
        or effect not in TRACKED_EFFECTS
        or not isinstance(channel, str)
        or channel not in VALID_CHANNELS
    ):
        return ""

    created_at = time.time()
    try:
        db = get_firestore_client()
    except Exception as error:
        logger.error(
            "message receipt create failed exception_type=%s",
            type(error).__name__,
        )
        return ""

    def _create() -> str:
        for _ in range(3):
            receipt_id = secrets.token_urlsafe(24)
            payload = {
                "receipt_id": receipt_id,
                "call_sid": call_sid,
                "effect": effect,
                "channel": channel,
                "status": "pending",
                "provider_status": "unknown",
                "provider_message_sid": "",
                "provider_message_hash": "",
                "failure_code": "",
                "created_at": created_at,
                "updated_at": created_at,
                "next_reconcile_at": (
                    created_at + RECONCILIATION_DELAY_SECONDS
                ),
                "reconcile_lease_token": "",
                "reconcile_lease_expires_at": 0.0,
                "call_projection_pending": False,
                "call_projection_version": 0,
                "call_projection_next_at": 0.0,
            }
            try:
                db.collection(COLLECTION).document(receipt_id).create(payload)
            except AlreadyExists:
                continue
            return receipt_id
        return ""

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _create)
    except Exception as error:
        logger.error(
            "message receipt create failed exception_type=%s",
            type(error).__name__,
        )
        return ""


async def record_provider_update(
    receipt_id: str,
    *,
    provider_status: str,
    provider_message_sid: str,
    provider_error_code: object = "",
) -> ReceiptUpdate:
    """Apply one authenticated callback or reconciliation result transactionally."""
    if not receipt_id or len(receipt_id) > 128:
        return ReceiptUpdate("invalid")
    try:
        db = get_firestore_client()
        doc_ref = db.collection(COLLECTION).document(receipt_id)
    except Exception as error:
        logger.error(
            "message receipt update failed receipt=%s exception_type=%s",
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        return ReceiptUpdate("error")

    def _record() -> ReceiptUpdate:
        transaction = db.transaction()

        @firestore.transactional
        def _transactional_record(tx) -> ReceiptUpdate:
            snapshot = doc_ref.get(transaction=tx)
            if not snapshot.exists:
                return ReceiptUpdate("not_found")
            data = snapshot.to_dict() or {}
            outcome, updates = _provider_transition(
                data,
                provider_status=provider_status,
                provider_message_sid=provider_message_sid,
                provider_error_code=provider_error_code,
                now=time.time(),
            )
            if updates:
                tx.update(doc_ref, updates)
            return ReceiptUpdate(
                outcome,
                safe_receipt_summary(receipt_id, {**data, **updates}),
            )

        return _transactional_record(transaction)

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _record)
    except Exception as error:
        logger.error(
            "message receipt update failed receipt=%s exception_type=%s",
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        return ReceiptUpdate("error")


async def mark_submission_failed(receipt_id: str) -> bool:
    return await _apply_boolean_transition(
        receipt_id,
        lambda data, now: _submission_failure_transition(data, now=now),
        event="submission_failure",
    )


async def mark_missing_provider_id(
    receipt_id: str,
    *,
    lease_token: str = "",
) -> bool:
    return await _apply_boolean_transition(
        receipt_id,
        lambda data, now: _missing_provider_id_transition(data, now=now),
        event="missing_provider_id",
        required_lease_token=lease_token,
    )


async def _apply_boolean_transition(
    receipt_id: str,
    transition,
    *,
    event: str,
    required_lease_token: str = "",
) -> bool:
    if not receipt_id or len(receipt_id) > 128:
        return False
    try:
        db = get_firestore_client()
        doc_ref = db.collection(COLLECTION).document(receipt_id)
    except Exception as error:
        logger.error(
            "message receipt transition failed event=%s receipt=%s exception_type=%s",
            event,
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        return False

    def _apply() -> bool:
        transaction = db.transaction()

        @firestore.transactional
        def _transactional_apply(tx) -> bool:
            snapshot = doc_ref.get(transaction=tx)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if required_lease_token and data.get("reconcile_lease_token") != (
                required_lease_token
            ):
                return False
            accepted, updates = transition(data, time.time())
            if updates:
                tx.update(doc_ref, updates)
            return accepted

        return _transactional_apply(transaction)

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _apply)
    except Exception as error:
        logger.error(
            "message receipt transition failed event=%s receipt=%s exception_type=%s",
            event,
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        return False


async def get_receipt(receipt_id: str) -> dict | None:
    try:
        db = get_firestore_client()
        doc_ref = db.collection(COLLECTION).document(receipt_id)
    except Exception as error:
        logger.error(
            "message receipt get failed receipt=%s exception_type=%s",
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        raise
    try:
        snapshot = await asyncio.get_running_loop().run_in_executor(None, doc_ref.get)
    except Exception as error:
        logger.error(
            "message receipt get failed receipt=%s exception_type=%s",
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        raise
    if not snapshot.exists:
        return None
    return snapshot.to_dict() or {}


async def list_receipts(status: str, *, limit: int = 50) -> list[dict]:
    """Return bounded payload-free operational summaries."""
    if status not in VALID_RECEIPT_STATUSES:
        raise ValueError("unsupported message delivery receipt status")
    bounded_limit = max(1, min(int(limit), 101))
    db = get_firestore_client()

    def _query():
        return list(
            db.collection(COLLECTION)
            .where(filter=FieldFilter("status", "==", status))
            .order_by("created_at", direction=firestore.Query.ASCENDING)
            .limit(bounded_limit)
            .stream()
        )

    docs = await asyncio.get_running_loop().run_in_executor(None, _query)
    return [safe_receipt_summary(doc.id, doc.to_dict() or {}) for doc in docs]


async def acknowledge_receipt(receipt_id: str, resolution: str) -> bool:
    return await _apply_boolean_transition(
        receipt_id,
        lambda data, now: _acknowledge_transition(
            data,
            resolution=resolution,
            now=now,
        ),
        event="acknowledge",
    )


async def list_reconciliation_candidates(
    *,
    now: float | None = None,
    limit: int = 20,
) -> list[ReconciliationCandidate]:
    """List old pending receipts that are due for provider reconciliation."""
    current_time = time.time() if now is None else now
    bounded_limit = max(1, min(int(limit), 100))
    db = get_firestore_client()

    def _query():
        return list(
            db.collection(COLLECTION)
            .where(filter=FieldFilter("status", "==", "pending"))
            .where(filter=FieldFilter("next_reconcile_at", "<=", current_time))
            .order_by("next_reconcile_at", direction=firestore.Query.ASCENDING)
            .limit(bounded_limit)
            .stream()
        )

    docs = await asyncio.get_running_loop().run_in_executor(None, _query)
    candidates = []
    for doc in docs:
        data = doc.to_dict() or {}
        provider_message_sid = str(data.get("provider_message_sid") or "")
        candidates.append(
            ReconciliationCandidate(
                receipt_id=doc.id,
                provider_message_sid=(
                    provider_message_sid
                    if _provider_message_hash(provider_message_sid)
                    else ""
                ),
            )
        )
    return candidates


async def claim_reconciliation(
    receipt_id: str,
    *,
    now: float | None = None,
) -> ReconciliationCandidate | None:
    """Lease one due receipt transactionally before any provider fetch."""
    if not receipt_id or len(receipt_id) > 128:
        return None
    claimed_at = time.time() if now is None else now
    lease_token = secrets.token_urlsafe(18)
    try:
        db = get_firestore_client()
        doc_ref = db.collection(COLLECTION).document(receipt_id)
    except Exception as error:
        logger.error(
            "message receipt claim failed receipt=%s exception_type=%s",
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        return None

    def _claim() -> ReconciliationCandidate | None:
        transaction = db.transaction()

        @firestore.transactional
        def _transactional_claim(tx) -> ReconciliationCandidate | None:
            snapshot = doc_ref.get(transaction=tx)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict() or {}
            accepted, updates = _reconciliation_claim_transition(
                data,
                lease_token=lease_token,
                now=claimed_at,
            )
            if not accepted:
                return None
            tx.update(doc_ref, updates)
            provider_message_sid = str(data.get("provider_message_sid") or "")
            return ReconciliationCandidate(
                receipt_id=receipt_id,
                provider_message_sid=(
                    provider_message_sid
                    if _provider_message_hash(provider_message_sid)
                    else ""
                ),
                lease_token=lease_token,
            )

        return _transactional_claim(transaction)

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _claim)
    except Exception as error:
        logger.error(
            "message receipt claim failed receipt=%s exception_type=%s",
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        return None


async def mark_reconciled(
    receipt_id: str,
    *,
    lease_token: str = "",
    now: float | None = None,
) -> bool:
    if not receipt_id or len(receipt_id) > 128:
        return False
    try:
        db = get_firestore_client()
        doc_ref = db.collection(COLLECTION).document(receipt_id)
    except Exception as error:
        logger.error(
            "message receipt reconcile mark failed receipt=%s exception_type=%s",
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        return False
    reconciled_at = time.time() if now is None else now

    def _mark() -> bool:
        transaction = db.transaction()

        @firestore.transactional
        def _transactional_mark(tx) -> bool:
            snapshot = doc_ref.get(transaction=tx)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            updates = _reconciliation_completion_updates(
                data,
                lease_token=lease_token,
                now=reconciled_at,
            )
            if updates is None:
                return False
            tx.update(
                doc_ref,
                updates,
            )
            return True

        return _transactional_mark(transaction)

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _mark)
    except Exception as error:
        logger.error(
            "message receipt reconcile mark failed receipt=%s exception_type=%s",
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        return False


async def list_pending_projection_ids(
    *,
    now: float | None = None,
    limit: int = 20,
) -> list[str]:
    current_time = time.time() if now is None else now
    bounded_limit = max(1, min(int(limit), 100))
    db = get_firestore_client()

    def _query():
        return list(
            db.collection(COLLECTION)
            .where(filter=FieldFilter("call_projection_pending", "==", True))
            .where(filter=FieldFilter("call_projection_next_at", "<=", current_time))
            .order_by("call_projection_next_at", direction=firestore.Query.ASCENDING)
            .limit(bounded_limit)
            .stream()
        )

    docs = await asyncio.get_running_loop().run_in_executor(None, _query)
    return [doc.id for doc in docs if doc.id and len(doc.id) <= 128]


async def defer_projection(receipt_id: str) -> bool:
    """Move one failed projection behind the current repair backlog."""
    return await _apply_boolean_transition(
        receipt_id,
        lambda data, now: _projection_defer_transition(data, now=now),
        event="defer_projection",
    )


async def project_receipt_to_call(receipt_id: str) -> bool:
    """Atomically project the latest receipt state into its call record."""
    if not receipt_id or len(receipt_id) > 128:
        return False
    try:
        db = get_firestore_client()
        receipt_ref = db.collection(COLLECTION).document(receipt_id)
    except Exception as error:
        logger.error(
            "message receipt projection failed receipt=%s exception_type=%s",
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        return False

    def _project() -> bool:
        transaction = db.transaction()

        @firestore.transactional
        def _transactional_project(tx) -> bool:
            snapshot = receipt_ref.get(transaction=tx)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if data.get("call_projection_pending") is not True:
                return True

            raw_call_sid = data.get("call_sid")
            effect = data.get("effect")
            version = data.get("call_projection_version")
            if (
                not isinstance(raw_call_sid, str)
                or not raw_call_sid
                or len(raw_call_sid) > 128
                or "/" in raw_call_sid
                or not isinstance(effect, str)
                or effect not in TRACKED_EFFECTS
                or isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
            ):
                return False

            summary = safe_receipt_summary(receipt_id, data)
            resolution = (
                summary["resolution"]
                if summary.get("status") == "acknowledged"
                else ""
            )
            call_updates = call_record_delivery_updates(
                summary,
                resolution=resolution,
            )
            if not call_updates:
                return False
            prefix = f"post_call_delivery_{effect}"
            call_updates.update(
                {
                    "call_sid": raw_call_sid,
                    f"{prefix}_receipt_id": _bounded_identifier(receipt_id),
                    f"{prefix}_version": version,
                }
            )
            call_ref = db.collection(CALLS_COLLECTION).document(raw_call_sid)
            tx.set(call_ref, call_updates, merge=True)
            projected_at = time.time()
            tx.update(
                receipt_ref,
                {
                    "call_projection_pending": False,
                    "call_projection_next_at": 0.0,
                    "call_projected_version": version,
                    "call_projected_at": projected_at,
                    "updated_at": max(
                        projected_at,
                        _safe_timestamp(data.get("updated_at")) or 0.0,
                    ),
                },
            )
            return True

        return _transactional_project(transaction)

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _project)
    except Exception as error:
        logger.error(
            "message receipt projection failed receipt=%s exception_type=%s",
            _bounded_identifier(receipt_id)[:8] or "unknown",
            type(error).__name__,
        )
        return False
