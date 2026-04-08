"""Knowledge base service — used by Vapi tool calls to answer FAQs."""

from typing import Optional

from app.db.knowledge import search_kb
from app.utils.logging import get_logger

logger = get_logger(__name__)


async def check_knowledge_base(question: str, contractor_id: str = "") -> Optional[str]:
    """Search the knowledge base for an answer to the caller's question.

    Called by the Vapi tool handler when Claude triggers check_knowledge_base.
    Returns the answer string, or None if no match.

    Args:
        question: The caller's question text.
        contractor_id: Contractor whose KB to search. Required for tenant isolation.
    """
    if not contractor_id:
        logger.warning("check_knowledge_base called without contractor_id — KB search skipped")
        return None
    match = await search_kb(question, contractor_id=contractor_id)
    if match:
        answer = match.get("answer", "")
        logger.info(f"KB answer found for: {question[:50]}")
        return answer

    logger.info(f"KB no match for: {question[:50]}")
    return None
