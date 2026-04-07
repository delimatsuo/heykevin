"""Firestore operations for call records."""

from typing import Optional

from google.cloud import firestore

from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "calls"


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
