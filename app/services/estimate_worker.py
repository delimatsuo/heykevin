"""Recovery worker for stranded estimate video analyses."""

import asyncio
import time
from typing import Optional

from google.cloud.firestore import transactional

from app.db.contractors import get_contractor
from app.db.firestore_client import get_firestore_client
from app.services.ai_estimate import analyze_media
from app.services.estimate_media import make_watch_url, read_media
from app.services.estimate_notifications import send_estimate_notifications
from app.utils.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "estimates"
MAX_ANALYSIS_ATTEMPTS = 3
WORKER_INTERVAL_SECONDS = 30.0
DEFAULT_LEASE_SECONDS = 300.0


def claim_reanalysis_or_fail(
    db,
    doc_ref,
    now: float,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> tuple[str, dict]:
    """Atomically check lease expiry and either re-claim or mark failed.

    Returns (action, data) where action is one of:
      - "reclaimed": lease expired, attempts < MAX, incremented attempts and set fresh lease
      - "failed_notify": attempts >= MAX, marked failed, claimed notification rights (notified_at set)
      - "failed_no_notify": attempts >= MAX, marked failed, notification was already sent
      - "noop": doc not exists, not processing, or lease not expired
    """
    tx = db.transaction()

    @transactional
    def _tx(transaction) -> tuple[str, dict]:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return "noop", {}
        data = snapshot.to_dict() or {}
        if data.get("status") != "processing":
            return "noop", data
        if now <= data.get("lease_expires_at", 0):
            return "noop", data

        attempts = data.get("attempts") or 0
        if attempts >= MAX_ANALYSIS_ATTEMPTS:
            should_notify = not bool(data.get("notified_at"))
            updates = {
                "status": "failed",
                "completed_at": now,
            }
            if should_notify:
                updates["notified_at"] = now
            transaction.update(doc_ref, updates)
            data.update(updates)
            return "failed_notify" if should_notify else "failed_no_notify", data

        attempts += 1
        updates = {
            "attempts": attempts,
            "lease_expires_at": now + lease_seconds,
        }
        transaction.update(doc_ref, updates)
        data.update(updates)
        return "reclaimed", data

    return _tx(tx)


def claim_notification_and_complete(
    db,
    doc_ref,
    media_id: str,
    result: dict,
    now: float,
) -> tuple[bool, bool, dict]:
    """Atomically complete processing and claim notification rights.

    Returns (accepted, should_notify, data).
    """
    tx = db.transaction()

    @transactional
    def _tx(transaction) -> tuple[bool, bool, dict]:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False, False, {}
        data = snapshot.to_dict() or {}
        if data.get("status") != "processing" or data.get("media_id") != media_id:
            return False, False, data

        should_notify = not bool(data.get("notified_at"))
        updates = {
            "status": "complete",
            "result": result,
            "completed_at": now,
        }
        if should_notify:
            updates["notified_at"] = now

        transaction.update(doc_ref, updates)
        data.update(updates)
        return True, should_notify, data

    return _tx(tx)


def claim_notification_and_fail(
    db,
    doc_ref,
    media_id: str,
    now: float,
) -> tuple[bool, bool, dict]:
    """Atomically fail processing and claim notification rights.

    Returns (accepted, should_notify, data).
    """
    tx = db.transaction()

    @transactional
    def _tx(transaction) -> tuple[bool, bool, dict]:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False, False, {}
        data = snapshot.to_dict() or {}
        if data.get("status") != "processing" or data.get("media_id") != media_id:
            return False, False, data

        should_notify = not bool(data.get("notified_at"))
        updates = {
            "status": "failed",
            "completed_at": now,
        }
        if should_notify:
            updates["notified_at"] = now

        transaction.update(doc_ref, updates)
        data.update(updates)
        return True, should_notify, data

    return _tx(tx)


async def run_pending_estimates_once(now: Optional[float] = None) -> None:
    """Scan for processing estimates with expired leases, re-claim, and re-run analysis."""
    current_time = now if now is not None else time.time()
    db = get_firestore_client()

    def _fetch_processing_docs():
        query = db.collection(COLLECTION).where("status", "==", "processing")
        return list(query.stream())

    try:
        docs = await asyncio.get_running_loop().run_in_executor(None, _fetch_processing_docs)
    except Exception as e:
        logger.error("Error checking pending estimates: %s", type(e).__name__)
        return

    for doc in docs:
        token_hash = doc.id
        doc_ref = doc.reference

        # Cheap pre-check before entering transaction
        data = doc.to_dict() or {}
        if data.get("lease_expires_at", 0) > current_time:
            continue

        try:
            action, claim_data = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: claim_reanalysis_or_fail(db, doc_ref, current_time),
            )
        except Exception as e:
            logger.error("Failed to execute claim transaction for %s: %s", token_hash[:8], type(e).__name__)
            continue

        if action == "noop":
            continue

        media_id = claim_data.get("media_id", "")
        object_path = claim_data.get("media_object_path", "")
        contractor_id = claim_data.get("contractor_id", "")
        caller_phone = claim_data.get("caller_phone", "")
        call_sid = claim_data.get("call_sid", "")
        contractor = await get_contractor(contractor_id) if contractor_id else None

        if action == "failed_notify":
            logger.error(
                "Estimate %s failed after max attempts (%s)",
                token_hash[:8],
                MAX_ANALYSIS_ATTEMPTS,
            )
            watch_url = make_watch_url(media_id) if (media_id and object_path) else ""
            await send_estimate_notifications(
                caller_phone=caller_phone,
                contractor=contractor,
                call_sid=call_sid,
                token_hash=token_hash,
                is_failure=True,
                watch_url=watch_url,
            )
            continue
        elif action == "failed_no_notify":
            continue

        # action == "reclaimed"
        logger.info(
            "Re-claimed expired estimate %s (attempt %s)",
            token_hash[:8],
            claim_data.get("attempts"),
        )

        content_type = claim_data.get("media_content_type", "")
        description = claim_data.get("description", "")

        if not object_path:
            now_fail = time.time()
            accepted, should_notify, _ = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: claim_notification_and_fail(db, doc_ref, media_id, now_fail),
            )
            if accepted and should_notify:
                await send_estimate_notifications(
                    caller_phone=caller_phone,
                    contractor=contractor,
                    call_sid=call_sid,
                    token_hash=token_hash,
                    is_failure=True,
                )
            continue

        try:
            media_bytes = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: read_media(object_path),
            )
            services = contractor.get("services", []) if contractor else []
            business_name = contractor.get("business_name", "") if contractor else ""
            result = await analyze_media(
                media_bytes=media_bytes,
                media_type=content_type,
                services_list=services,
                business_name=business_name,
                text_description=description,
            )

            now_finish = time.time()
            accepted, should_notify, _ = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: claim_notification_and_complete(db, doc_ref, media_id, result, now_finish),
            )
            if accepted and should_notify:
                watch_url = make_watch_url(media_id) if media_id else ""
                await send_estimate_notifications(
                    caller_phone=caller_phone,
                    contractor=contractor,
                    call_sid=call_sid,
                    token_hash=token_hash,
                    result=result,
                    watch_url=watch_url,
                )
        except Exception as error:
            logger.error(
                "Recovery failed for estimate %s: %s",
                token_hash[:8],
                type(error).__name__,
            )
            if (claim_data.get("attempts") or 0) >= MAX_ANALYSIS_ATTEMPTS:
                now_err = time.time()
                try:
                    accepted, should_notify, _ = await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda: claim_notification_and_fail(db, doc_ref, media_id, now_err),
                    )
                    if accepted and should_notify:
                        watch_url = make_watch_url(media_id) if (media_id and object_path) else ""
                        await send_estimate_notifications(
                            caller_phone=caller_phone,
                            contractor=contractor,
                            call_sid=call_sid,
                            token_hash=token_hash,
                            is_failure=True,
                            watch_url=watch_url,
                        )
                except Exception as e:
                    logger.error(
                        "Failed to mark estimate %s as failed after recovery error: %s",
                        token_hash[:8],
                        type(e).__name__,
                    )


async def estimate_worker_loop() -> None:
    """Continuously sweep and recover stranded estimate analyses."""
    while True:
        try:
            await run_pending_estimates_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error("estimate_worker_loop error: %s", type(error).__name__)
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)
