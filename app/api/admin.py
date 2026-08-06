"""Admin monitoring API endpoints.

Protected by global admin token only — not accessible via contractor tokens.
"""

import asyncio
import time
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.db import calls as call_db
from app.db import post_call_handoffs as handoff_db
from app.db.admin_audit import list_admin_audit_events, write_admin_audit_event
from app.db.firestore_client import get_firestore_client
from app.middleware.auth import verify_api_token
from app.services.admin_diagnostics import diagnose_contractor
from app.services.admin_numbers import fetch_twilio_incoming_numbers, reconcile_number_inventory
from app.utils.logging import get_logger, redact_phone

logger = get_logger(__name__)

router = APIRouter(prefix="/api/admin", dependencies=[Depends(verify_api_token)])


def _require_admin(request: Request):
    """Raise 403 if the caller is not the global admin."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


def _timestamp_sort_value(value) -> float:
    """Return a numeric timestamp for sorting mixed Firestore timestamp values."""
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    return 0.0


SECRET_CONTRACTOR_FIELDS = {
    "api_token_hash",
    "auth_token",
    "contractor_api_token",
    "contractor_token",
    "jobber_access_token",
    "jobber_refresh_token",
    "token_hash",
}

PostCallHandoffStatus = Literal[
    "pending",
    "in_progress",
    "completed",
    "needs_attention",
    "acknowledged",
]
PostCallResolution = Literal[
    "verified_complete",
    "customer_contacted_manually",
    "no_action_required",
    "false_alarm",
]


class AcknowledgePostCallHandoffRequest(BaseModel):
    resolution: PostCallResolution


def _contractor_list_item(doc) -> dict:
    d = doc.to_dict()
    return {
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
    }


def _matches_contractor_filters(
    item: dict,
    *,
    status: Optional[str],
    tier: Optional[str],
    has_twilio: Optional[bool],
    q: Optional[str],
) -> bool:
    if status and item.get("subscription_status") != status:
        return False
    if tier and item.get("subscription_tier") != tier:
        return False
    if has_twilio is True and not item.get("twilio_number"):
        return False
    if has_twilio is False and item.get("twilio_number"):
        return False
    if q:
        haystack = " ".join([
            item.get("contractor_id", ""),
            item.get("business_name", ""),
            item.get("owner_name", ""),
            item.get("twilio_number", ""),
        ]).lower()
        return q.lower() in haystack
    return True


def _sanitize_contractor_profile(contractor_id: str, data: dict) -> dict:
    sanitized = {
        key: value
        for key, value in data.items()
        if key not in SECRET_CONTRACTOR_FIELDS
    }
    sanitized["contractor_id"] = contractor_id
    return sanitized


def _jobber_summary(data: dict) -> dict:
    connected = bool(data.get("jobber_access_token"))
    return {
        "connected": connected,
        "connected_at": data.get("jobber_connected_at"),
        "lead_capture_enabled": connected and data.get("jobber_lead_capture_enabled") is True,
        "lead_capture_updated_at": data.get("jobber_lead_capture_updated_at"),
    }


def _device_summary(device_doc) -> dict:
    if not getattr(device_doc, "exists", False):
        return {
            "push_token_present": False,
            "voip_token_present": False,
            "updated_at": None,
        }

    data = device_doc.to_dict() or {}
    return {
        "push_token_present": bool(data.get("push_token")),
        "voip_token_present": bool(data.get("voip_token")),
        "updated_at": data.get("updated_at"),
    }


def _summary_from_job_card(data: dict) -> str:
    return (data.get("issue_description") or data.get("message") or "").strip()


def _job_cards_by_call_sid(db) -> dict[str, dict]:
    try:
        jobs = {}
        for doc in db.collection("jobs").stream():
            data = doc.to_dict() or {}
            call_sid = data.get("call_sid", "")
            if call_sid:
                jobs[call_sid] = data
        return jobs
    except Exception:
        return {}


def _call_list_item(doc, job_cards: Optional[dict[str, dict]] = None) -> dict:
    data = doc.to_dict() or {}
    call_sid = data.get("call_sid") or doc.id
    job_card = (job_cards or {}).get(call_sid, {})
    summary = data.get("summary") or _summary_from_job_card(job_card)
    transcript = data.get("transcript")
    item = {
        "call_sid": call_sid,
        "contractor_id": data.get("contractor_id", ""),
        "timestamp": data.get("timestamp"),
        "caller_phone": redact_phone(data.get("caller_phone", "")),
        "caller_name": data.get("caller_name", ""),
        "outcome": (
            data.get("outcome")
            or data.get("call_type")
            or job_card.get("call_type")
            or data.get("call_status", "")
        ),
        "route": data.get("route") or data.get("route_taken", ""),
        "duration_seconds": data.get("duration_seconds") or data.get("duration") or 0,
        "has_summary": bool(summary),
        "has_transcript": bool(transcript),
    }
    for key in (
        "jobber_sync_status",
        "jobber_request_id",
        "jobber_request_url",
        "jobber_synced_at",
        "jobber_sync_error",
    ):
        if key in data:
            item[key] = data.get(key)
    return item


def _contractor_inventory_item(doc) -> dict:
    data = doc.to_dict() or {}
    item = _contractor_list_item(doc)
    item["active"] = data.get("active", False)
    return item


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@router.get("/post-call-handoffs")
async def admin_list_post_call_handoffs(
    request: Request,
    status: PostCallHandoffStatus = "needs_attention",
    limit: int = 50,
):
    """Return a bounded payload-free operational queue."""
    _require_admin(request)
    bounded_limit = max(1, min(limit, 100))
    handoffs = await handoff_db.list_handoffs(status, limit=bounded_limit)
    return {
        "status": status,
        "count": len(handoffs),
        "limit": bounded_limit,
        "has_more": len(handoffs) >= bounded_limit,
        "handoffs": handoffs,
    }


@router.post("/post-call-handoffs/{call_sid}/acknowledge")
async def admin_acknowledge_post_call_handoff(
    call_sid: str,
    body: AcknowledgePostCallHandoffRequest,
    request: Request,
):
    """Acknowledge operator review without retrying external effects."""
    _require_admin(request)
    if not call_sid or len(call_sid) > 128:
        raise HTTPException(status_code=422, detail="Invalid call identifier")

    current = await handoff_db.get_handoff(call_sid)
    if current is None:
        raise HTTPException(status_code=404, detail="Post-call handoff not found")
    if current.get("status") != "needs_attention":
        raise HTTPException(
            status_code=409,
            detail="Only needs_attention handoffs can be acknowledged",
        )

    acknowledged = await handoff_db.acknowledge_handoff(
        call_sid,
        body.resolution,
    )
    if not acknowledged:
        raise HTTPException(
            status_code=409,
            detail="Post-call handoff state changed; reload before acknowledging",
        )

    summary = handoff_db.safe_handoff_summary(call_sid, current)
    acknowledged_at = time.time()
    call_status_mirrored = await call_db.save_call(
        call_sid,
        {
            "post_call_status": "acknowledged",
            "post_call_failure_code": summary["failure_code"],
            "post_call_completed_effects": summary["completed_effects"],
            "post_call_failed_effects": summary["failed_effects"],
            "post_call_resolution": body.resolution,
            "post_call_acknowledged_at": acknowledged_at,
        },
    )
    await write_admin_audit_event(
        request=request,
        action="acknowledge_post_call_handoff",
        target_type="post_call_handoff",
        target_id=call_sid,
        reason=body.resolution,
        before={
            "status": "needs_attention",
            "failure_code": summary["failure_code"],
        },
        after={
            "status": "acknowledged",
            "resolution": body.resolution,
        },
        metadata={"call_status_mirrored": call_status_mirrored},
    )
    return {
        "status": "acknowledged",
        "call_sid": call_sid,
        "resolution": body.resolution,
        "call_status_mirrored": call_status_mirrored,
    }

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
async def admin_list_contractors(
    request: Request,
    status: Optional[str] = None,
    tier: Optional[str] = None,
    has_twilio: Optional[bool] = None,
    q: Optional[str] = None,
    limit: int = 200,
):
    """Return all active contractors sorted by created_at desc."""
    _require_admin(request)

    loop = asyncio.get_event_loop()
    db = get_firestore_client()

    def _fetch():
        docs = list(
            db.collection("contractors")
            .where("active", "==", True)
            .stream()
        )
        results = []
        for doc in docs:
            item = _contractor_list_item(doc)
            if _matches_contractor_filters(
                item,
                status=status,
                tier=tier,
                has_twilio=has_twilio,
                q=q,
            ):
                results.append(item)
        results.sort(
            key=lambda item: _timestamp_sort_value(item.get("created_at")),
            reverse=True,
        )
        results = results[:limit]
        return results

    contractors = await loop.run_in_executor(None, _fetch)
    return {"contractors": contractors, "count": len(contractors)}


@router.get("/contractors/{contractor_id}")
async def admin_get_contractor_detail(contractor_id: str, request: Request):
    """Return one contractor profile with support diagnostics context."""
    _require_admin(request)

    loop = asyncio.get_event_loop()
    db = get_firestore_client()

    def _fetch():
        ref = db.collection("contractors").document(contractor_id)
        doc = ref.get()
        if not doc.exists:
            return None

        device_doc = ref.collection("devices").document("primary").get()
        contractor_data = doc.to_dict() or {}
        contractor = _sanitize_contractor_profile(contractor_id, contractor_data)
        device = _device_summary(device_doc)
        job_cards = _job_cards_by_call_sid(db)
        recent_calls = [
            _call_list_item(call_doc, job_cards)
            for call_doc in db.collection("calls").stream()
            if (call_doc.to_dict() or {}).get("contractor_id") == contractor_id
        ]
        recent_calls.sort(
            key=lambda item: _timestamp_sort_value(item.get("timestamp")),
            reverse=True,
        )
        recent_calls = recent_calls[:10]
        return {
            "contractor": contractor,
            "device": device,
            "jobber": _jobber_summary(contractor_data),
            "recent_calls": recent_calls,
            "diagnostics": diagnose_contractor(contractor, device, recent_calls),
            "audit_events": [],
        }

    result = await loop.run_in_executor(None, _fetch)
    if result is None:
        raise HTTPException(status_code=404, detail="Contractor not found")
    return result


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


@router.get("/calls")
async def admin_list_calls(request: Request, limit: int = 50):
    """Return recent calls without transcript or summary contents."""
    _require_admin(request)

    loop = asyncio.get_event_loop()
    db = get_firestore_client()

    def _fetch():
        docs = list(db.collection("calls").stream())
        job_cards = _job_cards_by_call_sid(db)
        calls = [_call_list_item(doc, job_cards) for doc in docs]
        calls.sort(
            key=lambda item: _timestamp_sort_value(item.get("timestamp")),
            reverse=True,
        )
        return calls[:limit]

    calls = await loop.run_in_executor(None, _fetch)
    return {"calls": calls, "count": len(calls)}


@router.get("/numbers")
async def admin_number_inventory(request: Request):
    """Return Twilio number inventory reconciled against active contractors."""
    _require_admin(request)

    loop = asyncio.get_event_loop()
    db = get_firestore_client()

    def _fetch_contractors():
        docs = list(
            db.collection("contractors")
            .where("active", "==", True)
            .stream()
        )
        return [_contractor_inventory_item(doc) for doc in docs]

    contractors, twilio_numbers = await asyncio.gather(
        loop.run_in_executor(None, _fetch_contractors),
        loop.run_in_executor(None, fetch_twilio_incoming_numbers),
    )
    return reconcile_number_inventory(contractors, twilio_numbers)


@router.get("/audit-events")
async def admin_audit_events(request: Request, limit: int = 100):
    """Return recent durable admin audit events."""
    _require_admin(request)
    return {"events": await list_admin_audit_events(limit=limit)}


@router.get("/contractors/{contractor_id}/calls")
async def admin_list_contractor_calls(
    contractor_id: str,
    request: Request,
    limit: int = 50,
):
    """Return recent calls for one contractor without transcript contents."""
    _require_admin(request)

    loop = asyncio.get_event_loop()
    db = get_firestore_client()

    def _fetch():
        docs = list(db.collection("calls").stream())
        job_cards = _job_cards_by_call_sid(db)
        calls = [
            _call_list_item(doc, job_cards)
            for doc in docs
            if (doc.to_dict() or {}).get("contractor_id") == contractor_id
        ]
        calls.sort(
            key=lambda item: _timestamp_sort_value(item.get("timestamp")),
            reverse=True,
        )
        return calls[:limit]

    calls = await loop.run_in_executor(None, _fetch)
    return {"calls": calls, "count": len(calls)}


# ---------------------------------------------------------------------------
# Extend trial
# ---------------------------------------------------------------------------

class ExtendTrialRequest(BaseModel):
    days: int = Field(..., ge=1, le=30)
    reason: str = Field(..., min_length=3, max_length=500)


class RevokeContractorRequest(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)
    mode: str = Field(default="expire_now", pattern="^(expire_now|disable_account)$")


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
        before = dict(d)
        current_expires = d.get("subscription_expires") or time.time()
        # If already expired, extend from now
        if current_expires < time.time():
            current_expires = time.time()

        new_expires = current_expires + body.days * 86400
        after = {
            "subscription_expires": new_expires,
            "subscription_status": "trial",
        }
        ref.update(after)
        return before, after, new_expires

    result = await loop.run_in_executor(None, _extend)
    if result is None:
        raise HTTPException(status_code=404, detail="Contractor not found")

    before, after, new_expires = result
    await write_admin_audit_event(
        request=request,
        action="extend_trial",
        target_type="contractor",
        target_id=contractor_id,
        reason=body.reason,
        before=before,
        after=after,
    )

    logger.info(f"Admin extended trial for {contractor_id} by {body.days} days")
    return {"status": "ok", "subscription_expires": new_expires}


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------

@router.post("/contractors/{contractor_id}/revoke")
async def admin_revoke_contractor(
    contractor_id: str,
    body: RevokeContractorRequest,
    request: Request,
):
    """Immediately set subscription_status to expired (abuse/admin action)."""
    _require_admin(request)

    loop = asyncio.get_event_loop()
    db = get_firestore_client()

    def _revoke():
        ref = db.collection("contractors").document(contractor_id)
        doc = ref.get()
        if not doc.exists:
            return None
        before = doc.to_dict()
        after = {"subscription_status": "expired"}
        if body.mode == "disable_account":
            after["active"] = False
        ref.update(after)
        return before, after

    result = await loop.run_in_executor(None, _revoke)
    if result is None:
        raise HTTPException(status_code=404, detail="Contractor not found")

    before, after = result
    await write_admin_audit_event(
        request=request,
        action="revoke_contractor",
        target_type="contractor",
        target_id=contractor_id,
        reason=body.reason,
        before=before,
        after=after,
        metadata={"mode": body.mode},
    )

    logger.info(f"Admin revoked subscription for {contractor_id}")
    return {"status": "ok", "contractor_id": contractor_id}
