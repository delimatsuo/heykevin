"""Call history API."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.middleware.auth import verify_api_token, require_contractor_access
from app.db.calls import get_call, get_calls_for_contractor, cleanup_old_calls
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/calls", dependencies=[Depends(verify_api_token)])


@router.get("")
async def api_list_calls(request: Request, contractor_id: str = Query(..., description="Contractor ID to filter calls")):
    """List recent calls for a contractor (last 7 days)."""
    require_contractor_access(request, contractor_id)
    calls = await get_calls_for_contractor(contractor_id)
    return {"calls": calls, "count": len(calls)}


@router.get("/{call_sid}")
async def api_get_call(call_sid: str, request: Request):
    """Get a call record by SID."""
    call = await get_call(call_sid)
    if not call:
        return {"error": "Not found"}
    require_contractor_access(request, call.get("contractor_id", ""))
    return call


@router.post("/cleanup")
async def api_cleanup_old_calls(request: Request):
    """Delete call records older than retention period. Admin-only."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")
    count = await cleanup_old_calls()
    return {"status": "ok", "deleted": count}
