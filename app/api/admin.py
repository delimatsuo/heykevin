"""Admin monitoring API endpoints.

Protected by global admin token only — not accessible via contractor tokens.
"""

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.firestore_client import get_firestore_client
from app.middleware.auth import verify_api_token
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(verify_api_token)])


def _require_admin(request: Request):
    """Raise 403 if the caller is not the global admin."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@router.get("/overview")
async def admin_overview(request: Request):
    """Return high-level subscription metrics."""
    _require_admin(request)

    loop = asyncio.get_event_loop()
    db = get_firestore_client()

    def _fetch():
        docs = list(
            db.collection("contractors")
            .where("active", "==", True)
            .stream()
        )
        total = len(docs)
        trials_active = 0
        paid_subscribers = 0
        expired = 0
        trials_expiring_7d = 0
        now = time.time()
        cutoff_7d = now + 7 * 86400

        for doc in docs:
            d = doc.to_dict()
            status = d.get("subscription_status", "")
            expires = d.get("subscription_expires")

            if status == "trial":
                trials_active += 1
                if expires and expires < cutoff_7d:
                    trials_expiring_7d += 1
            elif status == "active":
                paid_subscribers += 1
            elif status == "expired":
                expired += 1

        return {
            "total_contractors": total,
            "trials_active": trials_active,
            "paid_subscribers": paid_subscribers,
            "expired": expired,
            "trials_expiring_7d": trials_expiring_7d,
        }

    def _fetch_promo():
        doc = db.collection("subscription").document("promo_counter").get()
        if doc.exists:
            return doc.to_dict().get("count", 0)
        return 0

    stats, promo_count = await asyncio.gather(
        loop.run_in_executor(None, _fetch),
        loop.run_in_executor(None, _fetch_promo),
    )

    stats["promo_slots_used"] = promo_count
    return stats


# ---------------------------------------------------------------------------
# Contractor list
# ---------------------------------------------------------------------------

@router.get("/contractors")
async def admin_list_contractors(request: Request):
    """Return all active contractors sorted by created_at desc."""
    _require_admin(request)

    loop = asyncio.get_event_loop()
    db = get_firestore_client()

    def _fetch():
        docs = list(
            db.collection("contractors")
            .where("active", "==", True)
            .order_by("created_at", direction="DESCENDING")
            .limit(200)
            .stream()
        )
        results = []
        for doc in docs:
            d = doc.to_dict()
            results.append({
                "contractor_id": doc.id,
                "business_name": d.get("business_name", ""),
                "owner_name": d.get("owner_name", ""),
                "subscription_status": d.get("subscription_status", ""),
                "subscription_tier": d.get("subscription_tier", ""),
                "subscription_expires": d.get("subscription_expires"),
                "trial_start": d.get("trial_start"),
                "created_at": d.get("created_at"),
                "deleted_app_detected_at": d.get("deleted_app_detected_at"),
                "twilio_number": d.get("twilio_number", ""),
            })
        return results

    contractors = await loop.run_in_executor(None, _fetch)
    return {"contractors": contractors, "count": len(contractors)}


# ---------------------------------------------------------------------------
# Call stats
# ---------------------------------------------------------------------------

@router.get("/calls/stats")
async def admin_call_stats(request: Request):
    """Return global call counts for last 24h / 7d / 30d."""
    _require_admin(request)

    loop = asyncio.get_event_loop()
    db = get_firestore_client()
    now = time.time()

    def _count_since(cutoff: float) -> int:
        docs = list(
            db.collection("calls")
            .where("timestamp", ">=", cutoff)
            .stream()
        )
        return len(docs)

    calls_today, calls_7d, calls_30d = await asyncio.gather(
        loop.run_in_executor(None, lambda: _count_since(now - 86400)),
        loop.run_in_executor(None, lambda: _count_since(now - 7 * 86400)),
        loop.run_in_executor(None, lambda: _count_since(now - 30 * 86400)),
    )

    return {
        "calls_today": calls_today,
        "calls_7d": calls_7d,
        "calls_30d": calls_30d,
    }


# ---------------------------------------------------------------------------
# Extend trial
# ---------------------------------------------------------------------------

class ExtendTrialRequest(BaseModel):
    days: int = Field(..., ge=1, le=30)


@router.post("/contractors/{contractor_id}/extend-trial")
async def admin_extend_trial(contractor_id: str, body: ExtendTrialRequest, request: Request):
    """Extend a contractor's trial by N days."""
    _require_admin(request)

    loop = asyncio.get_event_loop()
    db = get_firestore_client()

    def _extend():
        ref = db.collection("contractors").document(contractor_id)
        doc = ref.get()
        if not doc.exists:
            return None

        d = doc.to_dict()
        current_expires = d.get("subscription_expires") or time.time()
        # If already expired, extend from now
        if current_expires < time.time():
            current_expires = time.time()

        new_expires = current_expires + body.days * 86400
        ref.update({
            "subscription_expires": new_expires,
            "subscription_status": "trial",
        })
        return new_expires

    new_expires = await loop.run_in_executor(None, _extend)
    if new_expires is None:
        raise HTTPException(status_code=404, detail="Contractor not found")

    logger.info(f"Admin extended trial for {contractor_id} by {body.days} days")
    return {"status": "ok", "subscription_expires": new_expires}


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------

@router.post("/contractors/{contractor_id}/revoke")
async def admin_revoke_contractor(contractor_id: str, request: Request):
    """Immediately set subscription_status to expired (abuse/admin action)."""
    _require_admin(request)

    loop = asyncio.get_event_loop()
    db = get_firestore_client()

    def _revoke():
        ref = db.collection("contractors").document(contractor_id)
        doc = ref.get()
        if not doc.exists:
            return False
        ref.update({"subscription_status": "expired"})
        return True

    ok = await loop.run_in_executor(None, _revoke)
    if not ok:
        raise HTTPException(status_code=404, detail="Contractor not found")

    logger.info(f"Admin revoked subscription for {contractor_id}")
    return {"status": "ok", "contractor_id": contractor_id}
