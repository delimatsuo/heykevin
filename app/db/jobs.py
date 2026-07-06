"""Job record management in Firestore."""

import asyncio
import time
from typing import Optional

from google.api_core.exceptions import FailedPrecondition
from google.cloud import firestore as firestore_module

from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "jobs"


async def save_job(job_data: dict) -> str:
    """Save a job card to Firestore. Returns the job_id."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()

    job_data["created_at"] = time.time()
    job_data.setdefault("status", "new")

    doc_ref = await loop.run_in_executor(
        None,
        lambda: db.collection(COLLECTION).add(job_data)
    )
    job_id = doc_ref[1].id
    logger.info(f"Job saved: {job_id}")
    return job_id


async def get_job_by_call_sid(call_sid: str) -> Optional[dict]:
    """Check if a job with the given call_sid already exists."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    docs = await loop.run_in_executor(
        None,
        lambda: list(
            db.collection(COLLECTION)
            .where("call_sid", "==", call_sid)
            .limit(1)
            .stream()
        )
    )
    if docs:
        data = docs[0].to_dict()
        data["job_id"] = docs[0].id
        return data
    return None


async def get_job(job_id: str, contractor_id: str = "") -> Optional[dict]:
    """Get a job by ID. If contractor_id is provided, verify ownership."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc = await loop.run_in_executor(
        None,
        lambda: db.collection(COLLECTION).document(job_id).get()
    )
    if doc.exists:
        data = doc.to_dict()
        data["job_id"] = doc.id
        if contractor_id and data.get("contractor_id", "") != contractor_id:
            return None
        return data
    return None


async def list_jobs(limit: int = 20, contractor_id: str = "") -> list:
    """List recent jobs, optionally filtered by contractor."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()

    def _ordered_query():
        query = db.collection(COLLECTION)
        if contractor_id:
            query = query.where("contractor_id", "==", contractor_id)
        query = query.order_by("created_at", direction=firestore_module.Query.DESCENDING)
        return list(query.limit(limit).stream())

    def _fallback_contractor_query():
        query = db.collection(COLLECTION).where("contractor_id", "==", contractor_id)
        docs = list(query.limit(max(limit * 5, 100)).stream())
        docs.sort(key=lambda doc: doc.to_dict().get("created_at", 0), reverse=True)
        return docs[:limit]

    try:
        docs = await loop.run_in_executor(None, _ordered_query)
    except FailedPrecondition as e:
        if not contractor_id:
            logger.error(f"Firestore job list query requires an index: {e}")
            return []
        logger.warning("Firestore job index missing; using contractor-only fallback")
        try:
            docs = await loop.run_in_executor(None, _fallback_contractor_query)
        except Exception as fallback_error:
            logger.error(f"Firestore job fallback failed: {fallback_error}", exc_info=True)
            return []
    except Exception as e:
        logger.error(f"Firestore job list failed: {e}", exc_info=True)
        return []
    return [{"job_id": d.id, **d.to_dict()} for d in docs]


async def update_job(job_id: str, updates: dict):
    """Update a job record."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: db.collection(COLLECTION).document(job_id).update(updates)
    )


async def claim_jobber_sync(job_id: str) -> bool:
    """Claim one Jobber sync attempt for a local job record."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()

    def _claim() -> bool:
        transaction = db.transaction()
        doc_ref = db.collection(COLLECTION).document(job_id)

        @firestore_module.transactional
        def _transactional_claim(tx) -> bool:
            snapshot = doc_ref.get(transaction=tx)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if data.get("jobber_request_id"):
                return False
            if data.get("jobber_sync_status") == "in_progress":
                return False
            tx.update(doc_ref, {
                "jobber_sync_status": "in_progress",
                "jobber_sync_started_at": time.time(),
            })
            return True

        return _transactional_claim(transaction)

    return await loop.run_in_executor(None, _claim)
