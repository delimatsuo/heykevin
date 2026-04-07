"""Firestore operations for contacts."""

from typing import Optional

from app.db.firestore_client import get_firestore_client
from app.utils.phone import phone_hash
from app.utils.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "contacts"


async def get_contact(e164_phone: str) -> Optional[dict]:
    """Look up a contact by phone number. Returns None if not found."""
    try:
        db = get_firestore_client()
        doc = db.collection(COLLECTION).document(phone_hash(e164_phone)).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        logger.error(f"Firestore contact lookup failed: {e}", exc_info=True)
    return None


async def upsert_contact(e164_phone: str, data: dict):
    """Create or update a contact."""
    try:
        db = get_firestore_client()
        doc_id = phone_hash(e164_phone)
        data["phone"] = e164_phone
        db.collection(COLLECTION).document(doc_id).set(data, merge=True)
    except Exception as e:
        logger.error(f"Firestore contact upsert failed: {e}", exc_info=True)


async def list_contacts(limit: int = 100) -> list[dict]:
    """List all contacts."""
    try:
        db = get_firestore_client()
        docs = db.collection(COLLECTION).limit(limit).stream()
        return [doc.to_dict() for doc in docs]
    except Exception as e:
        logger.error(f"Firestore contact list failed: {e}", exc_info=True)
        return []
