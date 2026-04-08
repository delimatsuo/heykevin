"""Job record management in Firestore."""

import asyncio
import time
from typing import Optional

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

    def _query():
        query = db.collection(COLLECTION)
        if contractor_id:
            query = query.where("contractor_id", "==", contractor_id)
        query = query.order_by("created_at", direction=firestore_module.Query.DESCENDING)
        return list(query.limit(limit).stream())

    docs = await loop.run_in_executor(None, _query)
    return [{"job_id": d.id, **d.to_dict()} for d in docs]


async def update_job(job_id: str, updates: dict):
    """Update a job record."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: db.collection(COLLECTION).document(job_id).update(updates)
    )
