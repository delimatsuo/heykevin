"""Firestore operations for knowledge base.

Knowledge entries are stored per-contractor under:
  contractors/{contractor_id}/knowledge_base/{kb_id}
"""

from typing import Optional

from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "knowledge_base"


def _kb_collection(contractor_id: str):
    """Return the knowledge_base subcollection for a contractor."""
    db = get_firestore_client()
    return db.collection("contractors").document(contractor_id).collection(COLLECTION)


async def add_kb_entry(data: dict, contractor_id: str) -> str:
    """Add a knowledge base entry. Returns doc ID."""
    try:
        col = _kb_collection(contractor_id)
        doc_ref = col.document()
        data["id"] = doc_ref.id
        data["contractor_id"] = contractor_id
        doc_ref.set(data)
        return doc_ref.id
    except Exception as e:
        logger.error(f"KB add failed: {e}", exc_info=True)
        return ""


async def get_kb_entry(kb_id: str, contractor_id: str) -> Optional[dict]:
    """Get a knowledge base entry by ID."""
    try:
        doc = _kb_collection(contractor_id).document(kb_id).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        logger.error(f"KB get failed: {e}", exc_info=True)
    return None


async def update_kb_entry(kb_id: str, data: dict, contractor_id: str):
    """Update a knowledge base entry."""
    try:
        _kb_collection(contractor_id).document(kb_id).set(data, merge=True)
    except Exception as e:
        logger.error(f"KB update failed: {e}", exc_info=True)


async def delete_kb_entry(kb_id: str, contractor_id: str):
    """Delete a knowledge base entry."""
    try:
        _kb_collection(contractor_id).document(kb_id).delete()
    except Exception as e:
        logger.error(f"KB delete failed: {e}", exc_info=True)


async def list_kb_entries(contractor_id: str) -> list:
    """List all enabled knowledge base entries for a contractor."""
    try:
        docs = _kb_collection(contractor_id).where("enabled", "==", True).stream()
        return [doc.to_dict() for doc in docs]
    except Exception as e:
        logger.error(f"KB list failed: {e}", exc_info=True)
        return []


async def search_kb(query: str, contractor_id: str) -> Optional[dict]:
    """Search knowledge base by keyword matching. Returns best match or None."""
    try:
        entries = await list_kb_entries(contractor_id)
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
