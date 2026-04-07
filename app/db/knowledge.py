"""Firestore operations for knowledge base."""

from typing import Optional

from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "knowledge_base"


async def add_kb_entry(data: dict) -> str:
    """Add a knowledge base entry. Returns doc ID."""
    try:
        db = get_firestore_client()
        doc_ref = db.collection(COLLECTION).document()
        data["id"] = doc_ref.id
        doc_ref.set(data)
        return doc_ref.id
    except Exception as e:
        logger.error(f"KB add failed: {e}", exc_info=True)
        return ""


async def get_kb_entry(kb_id: str) -> Optional[dict]:
    """Get a knowledge base entry by ID."""
    try:
        db = get_firestore_client()
        doc = db.collection(COLLECTION).document(kb_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        logger.error(f"KB get failed: {e}", exc_info=True)
    return None


async def update_kb_entry(kb_id: str, data: dict):
    """Update a knowledge base entry."""
    try:
        db = get_firestore_client()
        db.collection(COLLECTION).document(kb_id).set(data, merge=True)
    except Exception as e:
        logger.error(f"KB update failed: {e}", exc_info=True)


async def delete_kb_entry(kb_id: str):
    """Delete a knowledge base entry."""
    try:
        db = get_firestore_client()
        db.collection(COLLECTION).document(kb_id).delete()
    except Exception as e:
        logger.error(f"KB delete failed: {e}", exc_info=True)


async def list_kb_entries() -> list:
    """List all knowledge base entries."""
    try:
        db = get_firestore_client()
        docs = db.collection(COLLECTION).where("enabled", "==", True).stream()
        return [doc.to_dict() for doc in docs]
    except Exception as e:
        logger.error(f"KB list failed: {e}", exc_info=True)
        return []


async def search_kb(query: str) -> Optional[dict]:
    """Search knowledge base by keyword matching. Returns best match or None."""
    try:
        entries = await list_kb_entries()
        query_lower = query.lower()

        best_match = None
        best_score = 0

        for entry in entries:
            keywords = entry.get("keywords", [])
            question = entry.get("question", "").lower()

            # Score by keyword matches
            score = 0
            for kw in keywords:
                if kw.lower() in query_lower:
                    score += 2
            # Partial match on question
            for word in query_lower.split():
                if word in question:
                    score += 1

            if score > best_score:
                best_score = score
                best_match = entry

        if best_match and best_score > 0:
            logger.info(f"KB match found: {best_match.get('question', '')[:50]}")
            return best_match

    except Exception as e:
        logger.error(f"KB search failed: {e}", exc_info=True)
    return None
