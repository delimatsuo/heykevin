"""Firestore operations for call records."""

import time
from typing import Optional

from google.cloud import firestore

from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "calls"

# Call records older than 90 days are eligible for cleanup
RETENTION_DAYS = 90


async def save_call(call_sid: str, data: dict):
    """Save or update a call record."""
    try:
        db = get_firestore_client()
        data["call_sid"] = call_sid
        db.collection(COLLECTION).document(call_sid).set(data, merge=True)
    except Exception as e:
        logger.error(f"Firestore call save failed: {e}", exc_info=True)


async def get_call(call_sid: str) -> Optional[dict]:
    """Get a call record by SID."""
    try:
        db = get_firestore_client()
        doc = db.collection(COLLECTION).document(call_sid).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        logger.error(f"Firestore call get failed: {e}", exc_info=True)
    return None


async def get_call_history(e164_phone: str, limit: int = 10) -> list[dict]:
    """Get recent call history for a phone number."""
    try:
        db = get_firestore_client()
        docs = (
            db.collection(COLLECTION)
            .where("caller_phone", "==", e164_phone)
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return [doc.to_dict() for doc in docs]
    except Exception as e:
        logger.error(f"Firestore call history failed: {e}", exc_info=True)
        return []


async def get_calls_for_contractor(contractor_id: str, limit: int = 100) -> list[dict]:
    """Get recent calls for a specific contractor, within retention window."""
    try:
        db = get_firestore_client()
        cutoff = time.time() - (RETENTION_DAYS * 86400)
        docs = (
            db.collection(COLLECTION)
            .where("contractor_id", "==", contractor_id)
            .where("timestamp", ">=", cutoff)
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return [doc.to_dict() for doc in docs]
    except Exception as e:
        logger.error(f"Firestore contractor calls failed: {e}", exc_info=True)
        return []


async def cleanup_old_calls() -> int:
    """Delete call records older than RETENTION_DAYS. Returns count deleted."""
    try:
        db = get_firestore_client()
        cutoff = time.time() - (RETENTION_DAYS * 86400)
        docs = (
            db.collection(COLLECTION)
            .where("timestamp", "<", cutoff)
            .limit(500)
            .stream()
        )
        count = 0
        for doc in docs:
            doc.reference.delete()
            count += 1
        if count:
            logger.info(f"Cleaned up {count} call records older than {RETENTION_DAYS} days")
        return count
    except Exception as e:
        logger.error(f"Call cleanup failed: {e}", exc_info=True)
        return 0
