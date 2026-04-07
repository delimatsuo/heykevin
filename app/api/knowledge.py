"""Knowledge base API — CRUD for FAQ entries Kevin can answer."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.middleware.auth import verify_api_token
from app.db.knowledge import add_kb_entry, get_kb_entry, update_kb_entry, delete_kb_entry, list_kb_entries
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/knowledge", dependencies=[Depends(verify_api_token)])


class KBCreate(BaseModel):
    category: str = "general"
    question: str
    answer: str
    keywords: list = []
    enabled: bool = True


class KBUpdate(BaseModel):
    category: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    keywords: Optional[list] = None
    enabled: Optional[bool] = None


@router.get("")
async def api_list_kb():
    """List all knowledge base entries."""
    entries = await list_kb_entries()
    return {"entries": entries, "count": len(entries)}


@router.post("")
async def api_create_kb(body: KBCreate):
    """Create a knowledge base entry."""
    kb_id = await add_kb_entry(body.dict())
    return {"status": "ok", "id": kb_id}


@router.get("/{kb_id}")
async def api_get_kb(kb_id: str):
    """Get a knowledge base entry."""
    entry = await get_kb_entry(kb_id)
    if not entry:
        return {"error": "Not found"}
    return entry


@router.put("/{kb_id}")
async def api_update_kb(kb_id: str, body: KBUpdate):
    """Update a knowledge base entry."""
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if updates:
        await update_kb_entry(kb_id, updates)
    return {"status": "ok"}


@router.delete("/{kb_id}")
async def api_delete_kb(kb_id: str):
    """Delete a knowledge base entry."""
    await delete_kb_entry(kb_id)
    return {"status": "ok"}
