"""Recovery worker for stranded estimate video analyses."""

import asyncio
import time
from typing import Optional

from app.api.estimates import send_estimate_notifications
from app.db.contractors import get_contractor
from app.db.firestore_client import get_firestore_client
from app.services.ai_estimate import analyze_media
from app.services.estimate_media import make_watch_url, read_media
from app.utils.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "estimates"
MAX_ANALYSIS_ATTEMPTS = 3
WORKER_INTERVAL_SECONDS = 30.0


async def run_pending_estimates_once(now: Optional[float] = None) -> None:
    """Scan for processing estimates with expired leases, re-claim, and re-run analysis."""
    current_time = now if now is not None else time.time()
    db = get_firestore_client()
    query = db.collection(COLLECTION).where("status", "==", "processing")
    docs = list(query.stream())

    for doc in docs:
        data = doc.to_dict() or {}
        token_hash = doc.id
        lease_expires_at = data.get("lease_expires_at", 0)

        # Only sweep leases that have actually expired
        if lease_expires_at > current_time:
            continue

        attempts = data.get("attempts", 1)
        media_id = data.get("media_id", "")
        object_path = data.get("media_object_path", "")
        content_type = data.get("media_content_type", "")
        description = data.get("description", "")
        contractor_id = data.get("contractor_id", "")
        caller_phone = data.get("caller_phone", "")
        call_sid = data.get("call_sid", "")
        contractor = await get_contractor(contractor_id) if contractor_id else None

        if attempts >= MAX_ANALYSIS_ATTEMPTS:
            # Exceeded maximum analysis attempts — mark failed
            doc.reference.update({
                "status": "failed",
                "completed_at": current_time,
            })
            watch_url = make_watch_url(media_id) if (media_id and object_path) else ""
            if not data.get("notified_at"):
                await send_estimate_notifications(
                    caller_phone=caller_phone,
                    contractor=contractor,
                    call_sid=call_sid,
                    token_hash=token_hash,
                    is_failure=True,
                    watch_url=watch_url,
                )
                doc.reference.update({"notified_at": time.time()})
            continue

        # Re-claim with fresh lease and incremented attempts
        new_attempts = attempts + 1
        new_lease = current_time + 300.0
        doc.reference.update({
            "attempts": new_attempts,
            "lease_expires_at": new_lease,
        })

        if not object_path:
            doc.reference.update({
                "status": "failed",
                "completed_at": current_time,
            })
            if not data.get("notified_at"):
                await send_estimate_notifications(
                    caller_phone=caller_phone,
                    contractor=contractor,
                    call_sid=call_sid,
                    token_hash=token_hash,
                    is_failure=True,
                )
                doc.reference.update({"notified_at": time.time()})
            continue

        try:
            media_bytes = read_media(object_path)
            services = contractor.get("services", []) if contractor else []
            business_name = contractor.get("business_name", "") if contractor else ""
            result = await analyze_media(
                media_bytes=media_bytes,
                media_type=content_type,
                services_list=services,
                business_name=business_name,
                text_description=description,
            )
            doc.reference.update({
                "status": "complete",
                "result": result,
                "completed_at": time.time(),
            })
            watch_url = make_watch_url(media_id) if media_id else ""
            if not data.get("notified_at"):
                await send_estimate_notifications(
                    caller_phone=caller_phone,
                    contractor=contractor,
                    call_sid=call_sid,
                    token_hash=token_hash,
                    result=result,
                    watch_url=watch_url,
                )
                doc.reference.update({"notified_at": time.time()})
        except Exception as error:
            logger.error("Estimate recovery analysis failed for %s: %s", token_hash[:8], error)
            if new_attempts >= MAX_ANALYSIS_ATTEMPTS:
                doc.reference.update({
                    "status": "failed",
                    "completed_at": time.time(),
                })
                watch_url = make_watch_url(media_id) if (media_id and object_path) else ""
                if not data.get("notified_at"):
                    await send_estimate_notifications(
                        caller_phone=caller_phone,
                        contractor=contractor,
                        call_sid=call_sid,
                        token_hash=token_hash,
                        is_failure=True,
                        watch_url=watch_url,
                    )
                    doc.reference.update({"notified_at": time.time()})


async def estimate_worker_loop() -> None:
    """Continuously sweep and recover stranded estimate analyses."""
    while True:
        try:
            await run_pending_estimates_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error("estimate_worker_loop error: %s", error)
        await asyncio.sleep(WORKER_INTERVAL_SECONDS)
