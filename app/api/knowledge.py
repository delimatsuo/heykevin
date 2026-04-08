"""Knowledge base API — CRUD for FAQ entries Kevin can answer."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from typing import Optional

from app.middleware.auth import verify_api_token, require_contractor_access
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
async def api_list_kb(request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """List all knowledge base entries for a contractor."""
    require_contractor_access(request, contractor_id)
    entries = await list_kb_entries(contractor_id=contractor_id)
    return {"entries": entries, "count": len(entries)}


@router.post("")
async def api_create_kb(request: Request, body: KBCreate, contractor_id: str = Query(..., description="Contractor ID")):
    """Create a knowledge base entry."""
    require_contractor_access(request, contractor_id)
    kb_id = await add_kb_entry(body.dict(), contractor_id=contractor_id)
    return {"status": "ok", "id": kb_id}


@router.get("/{kb_id}")
async def api_get_kb(kb_id: str, request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """Get a knowledge base entry."""
    require_contractor_access(request, contractor_id)
    entry = await get_kb_entry(kb_id, contractor_id=contractor_id)
    if not entry:
        return {"error": "Not found"}
    return entry


@router.put("/{kb_id}")
async def api_update_kb(kb_id: str, request: Request, body: KBUpdate, contractor_id: str = Query(..., description="Contractor ID")):
    """Update a knowledge base entry."""
    require_contractor_access(request, contractor_id)
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if updates:
        await update_kb_entry(kb_id, updates, contractor_id=contractor_id)
    return {"status": "ok"}


@router.delete("/{kb_id}")
async def api_delete_kb(kb_id: str, request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """Delete a knowledge base entry."""
    require_contractor_access(request, contractor_id)
    await delete_kb_entry(kb_id, contractor_id=contractor_id)
    return {"status": "ok"}
