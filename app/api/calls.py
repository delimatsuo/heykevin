"""Call history API."""

from fastapi import APIRouter, Depends

from app.middleware.auth import verify_api_token
from app.db.calls import get_call, get_call_history
from app.utils.phone import normalize_phone
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/calls", dependencies=[Depends(verify_api_token)])


@router.get("/{call_sid}")
async def api_get_call(call_sid: str):
    """Get a call record by SID."""
    call = await get_call(call_sid)
    if not call:
        return {"error": "Not found"}
    return call


@router.get("/history/{phone}")
async def api_call_history(phone: str, limit: int = 20):
    """Get call history for a phone number."""
    normalized = normalize_phone(phone)
    if not normalized:
        return {"error": "Invalid phone number"}

    calls = await get_call_history(normalized, limit=limit)
    return {"calls": calls, "count": len(calls)}
