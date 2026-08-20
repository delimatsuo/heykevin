"""AI estimate endpoints — token creation, upload, analysis, results."""

import asyncio
import hashlib
import os
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from google.cloud.firestore import transactional
from pydantic import BaseModel

from app.config import settings
from app.db.contractors import get_contractor
from app.db.firestore_client import get_firestore_client
from app.middleware.auth import require_contractor_access, verify_api_token
from app.services.ai_estimate import analyze_media
from app.services.estimate_media import (
    archive_media,
    gcs_redirect_url,
    make_watch_url,
    read_media,
    verify_watch_sig,
)
from app.services.gated_actions import ActionKey, GateContext, check_gated_action
from app.services.side_effect_audit import record_gate_decision
from app.services.sms import send_sms
from app.utils.logging import get_logger, redact_phone

logger = get_logger(__name__)

router = APIRouter(prefix="/api/estimates")

COLLECTION = "estimates"
TOKEN_EXPIRY_SECONDS = 48 * 3600  # 48 hours
MAX_UPLOADS_PER_TOKEN = 3

ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/heic",
    "video/mp4", "video/quicktime",
}
MAX_IMAGE_SIZE = 10 * 1024 * 1024   # 10MB
MAX_VIDEO_SIZE = 50 * 1024 * 1024   # 50MB


async def send_estimate_notifications(
    caller_phone: str,
    contractor: Optional[dict],
    call_sid: str,
    token_hash: str,
    result: Optional[dict] = None,
    is_failure: bool = False,
    watch_url: str = "",
) -> None:
    """Send customer and contractor notification SMS for an estimate outcome."""
    business_name = (contractor or {}).get("business_name", "the business")
    twilio_number = (contractor or {}).get("twilio_number", "")
    contractor_phone = (contractor or {}).get("owner_phone", "")

    # Customer SMS
    if caller_phone:
        if is_failure:
            customer_msg = (
                f"Thanks for your upload. We couldn't process this media. "
                f"Please call {business_name} directly at {twilio_number}."
                if twilio_number
                else f"Thanks for your upload. We couldn't process this media. "
                f"Please call {business_name} directly."
            )
        elif result and result.get("requires_manual_investigation"):
            customer_msg = (
                f"Thanks for your upload. This issue will require {business_name}'s "
                f"technician to manually investigate. We are unable to provide an "
                f"AI estimate at this time.\n\n"
                f"Call {business_name}: {twilio_number}"
            )
        else:
            diagnosis = (result or {}).get("diagnosis", "")
            est_min = (result or {}).get("estimate_min", 0)
            est_max = (result or {}).get("estimate_max", 0)
            customer_msg = (
                f"AI Diagnosis: {diagnosis}\n\n"
                f"Estimated Cost: ${est_min}-${est_max}\n\n"
                f"⚠️ This is an AI-generated estimate. The actual cost may differ "
                f"based on the technician's hands-on diagnosis.\n\n"
                f"Call {business_name}: {twilio_number}"
            )

        try:
            context = GateContext(
                source="estimate",
                actor="system",
                idempotency_key=f"{token_hash[:12]}:result",
            )
            await send_sms(
                caller_phone,
                customer_msg,
                from_number=twilio_number,
                contractor=contractor,
                action=ActionKey.ESTIMATE_RESULT_SMS,
                gate_context=context,
            )
        except Exception as e:
            logger.error("Failed to send customer estimate SMS for %s: %s", token_hash[:8], e)

    # Contractor SMS
    if contractor_phone:
        if is_failure:
            contractor_msg = (
                f"📋 AI ESTIMATE FAILED\n"
                f"From: {caller_phone}\n"
                f"Result: We couldn't process the caller's media."
            )
        elif result and result.get("requires_manual_investigation"):
            contractor_msg = (
                f"📋 AI ESTIMATE REQUEST\n"
                f"From: {caller_phone}\n"
                f"Result: Requires manual investigation\n"
                f"The AI could not confidently diagnose the issue."
            )
        else:
            diagnosis = (result or {}).get("diagnosis", "")
            est_min = (result or {}).get("estimate_min", 0)
            est_max = (result or {}).get("estimate_max", 0)
            matched = ", ".join(s.get("name", "") for s in (result or {}).get("matched_services", []))
            contractor_msg = (
                f"📋 AI ESTIMATE SENT\n"
                f"To: {caller_phone}\n"
                f"Diagnosis: {diagnosis}\n"
                f"Services: {matched}\n"
                f"Estimate: ${est_min}-${est_max}\n"
                f"Confidence: {(result or {}).get('confidence', 'unknown')}"
            )

        if watch_url:
            contractor_msg += f"\n\nWatch the caller's video: {watch_url}"

        try:
            await send_sms(contractor_phone, contractor_msg, from_number=twilio_number)
        except Exception as e:
            logger.error("Failed to send contractor estimate SMS for %s: %s", token_hash[:8], e)


def _max_upload_bytes_default() -> int:
    raw = os.environ.get("MAX_UPLOAD_BYTES", "").strip()
    if not raw:
        return 50 * 1024 * 1024
    try:
        return max(0, int(raw))
    except ValueError:
        return 50 * 1024 * 1024


MAX_UPLOAD_BYTES = _max_upload_bytes_default()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _gate_denied_response(decision):
    raise HTTPException(status_code=403, detail=decision.to_response())


# --- Authenticated endpoint: create token (called by post-call processing) ---

class CreateTokenRequest(BaseModel):
    contractor_id: str
    caller_phone: str
    call_sid: str = ""


@router.post("/create-token", dependencies=[Depends(verify_api_token)])
async def create_estimate_token(body: CreateTokenRequest, request: Request = None):
    """Create an estimate token for a caller after a service request call."""
    require_contractor_access(request, body.contractor_id)

    contractor = await get_contractor(body.contractor_id)
    context = GateContext(
        source="estimate",
        actor="system",
        idempotency_key=f"{body.call_sid}:estimate_token" if body.call_sid else f"{body.contractor_id}:estimate_token",
    )
    decision = check_gated_action(contractor, ActionKey.ESTIMATE_TOKEN_CREATE, context)
    record_gate_decision(
        action=ActionKey.ESTIMATE_TOKEN_CREATE,
        contractor_id=(contractor or {}).get("contractor_id") or body.contractor_id,
        source="estimate",
        resource_id=body.call_sid or body.contractor_id[:12],
        decision=decision,
    )
    if not decision.allowed:
        logger.info(
            "Estimate token creation blocked by gate",
            extra={"action": ActionKey.ESTIMATE_TOKEN_CREATE.value, "reason": decision.reason.value},
        )
        _gate_denied_response(decision)

    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)

    db = get_firestore_client()
    db.collection(COLLECTION).document(token_hash).set({
        "token_hash": token_hash,
        "contractor_id": body.contractor_id,
        "caller_phone": body.caller_phone,
        "call_sid": body.call_sid,
        "created_at": time.time(),
        "expires_at": time.time() + TOKEN_EXPIRY_SECONDS,
        "status": "pending",
        "upload_count": 0,
        "result": None,
        "media_object_path": None,
        "media_id": None,
        "media_content_type": None,
        "description": None,
        "attempts": 0,
        "lease_expires_at": 0,
        "notified_at": None,
    })

    estimate_url = f"https://heykevin.one/estimate/{token}"
    logger.info(f"Estimate token created for {redact_phone(body.caller_phone)}")
    return {"status": "ok", "token": token, "url": estimate_url}


# --- Public endpoints: token is the auth ---

async def _get_estimate_doc(token: str) -> Optional[dict]:
    """Look up an estimate by token. Returns None if invalid/expired."""
    token_hash = _hash_token(token)
    db = get_firestore_client()
    doc = db.collection(COLLECTION).document(token_hash).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    if time.time() > data.get("expires_at", 0):
        return None
    return data


@router.get("/{token}")
async def get_estimate(token: str):
    """Get estimate status/result. Public — token is the auth."""
    estimate = await _get_estimate_doc(token)
    if not estimate:
        raise HTTPException(status_code=404, detail="Invalid or expired token")

    return {
        "status": estimate.get("status", "pending"),
        "result": estimate.get("result"),
    }


class UploadUrlRequest(BaseModel):
    content_type: str


@router.post("/{token}/upload-url")
async def get_upload_url(token: str, body: UploadUrlRequest):
    """Get upload URL. Public — token is the auth."""
    estimate = await _get_estimate_doc(token)
    if not estimate:
        raise HTTPException(status_code=404, detail="Invalid or expired token")

    if estimate.get("upload_count", 0) >= MAX_UPLOADS_PER_TOKEN:
        raise HTTPException(status_code=429, detail="Upload limit reached")

    if body.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"File type not allowed. Accepted: {', '.join(ALLOWED_CONTENT_TYPES)}",
        )

    max_size = MAX_VIDEO_SIZE if body.content_type.startswith("video/") else MAX_IMAGE_SIZE
    token_hash = _hash_token(token)
    upload_url = f"{settings.cloud_run_url}/api/estimates/{token}/upload"

    db = get_firestore_client()
    db.collection(COLLECTION).document(token_hash).update({
        "upload_count": estimate.get("upload_count", 0) + 1,
    })

    return {
        "upload_url": upload_url,
        "max_size": max_size,
        "content_type": body.content_type,
    }


async def _read_request_with_cap(request: Request, max_bytes: int) -> bytes:
    """Stream the request body and abort early past `max_bytes`."""
    cl_header = request.headers.get("content-length", "").strip()
    if cl_header:
        try:
            cl_int = int(cl_header)
        except ValueError:
            cl_int = -1
        if cl_int > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max: {max_bytes // (1024 * 1024)}MB",
            )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            chunks.clear()
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max: {max_bytes // (1024 * 1024)}MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _run_async_estimate_analysis(
    token_hash: str,
    media_id: str,
    media_bytes: Optional[bytes],
    media_content_type: str,
    description: str,
    contractor: Optional[dict],
    caller_phone: str,
    call_sid: str,
    object_path: Optional[str] = None,
) -> None:
    """Background task for Gemini video analysis."""
    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(token_hash)
    services = contractor.get("services", []) if contractor else []
    business_name = contractor.get("business_name", "") if contractor else ""

    try:
        if media_bytes is None and object_path:
            media_bytes = read_media(object_path)

        result = await analyze_media(
            media_bytes=media_bytes,
            media_type=media_content_type,
            services_list=services,
            business_name=business_name,
            text_description=description,
        )

        doc_snap = doc_ref.get()
        if doc_snap.exists and doc_snap.to_dict().get("media_id") == media_id:
            now = time.time()
            doc_ref.update({
                "status": "complete",
                "result": result,
                "completed_at": now,
            })
            watch_url = make_watch_url(media_id) if object_path else ""
            doc_data = doc_snap.to_dict() or {}
            if not doc_data.get("notified_at"):
                await send_estimate_notifications(
                    caller_phone=caller_phone,
                    contractor=contractor,
                    call_sid=call_sid,
                    token_hash=token_hash,
                    result=result,
                    is_failure=False,
                    watch_url=watch_url,
                )
                doc_ref.update({"notified_at": time.time()})
    except Exception as error:
        logger.error("Async estimate analysis failed for %s: %s", token_hash[:8], error)
        doc_snap = doc_ref.get()
        if doc_snap.exists and doc_snap.to_dict().get("media_id") == media_id:
            now = time.time()
            doc_ref.update({
                "status": "failed",
                "completed_at": now,
            })
            watch_url = make_watch_url(media_id) if object_path else ""
            doc_data = doc_snap.to_dict() or {}
            if not doc_data.get("notified_at"):
                await send_estimate_notifications(
                    caller_phone=caller_phone,
                    contractor=contractor,
                    call_sid=call_sid,
                    token_hash=token_hash,
                    is_failure=True,
                    watch_url=watch_url,
                )
                doc_ref.update({"notified_at": time.time()})


@router.post("/{token}/upload")
async def upload_and_analyze(
    token: str,
    request: Request,
    description: str = Query(default="", max_length=500),
):
    """Receive media upload and trigger Gemini analysis."""
    estimate = await _get_estimate_doc(token)
    if not estimate:
        raise HTTPException(status_code=404, detail="Invalid or expired token")

    token_hash = _hash_token(token)
    contractor = await get_contractor(estimate["contractor_id"])
    context = GateContext(
        source="estimate",
        actor="system",
        idempotency_key=f"{token_hash[:12]}:result",
    )
    decision = check_gated_action(contractor, ActionKey.ESTIMATE_RESULT_SMS, context)
    record_gate_decision(
        action=ActionKey.ESTIMATE_RESULT_SMS,
        contractor_id=(contractor or {}).get("contractor_id") or estimate.get("contractor_id", ""),
        source="estimate",
        resource_id=token_hash[:12],
        decision=decision,
    )
    if not decision.allowed:
        logger.info(
            "Estimate upload blocked by gate",
            extra={"action": ActionKey.ESTIMATE_RESULT_SMS.value, "reason": decision.reason.value},
        )
        _gate_denied_response(decision)

    content_type = request.headers.get("content-type", "application/octet-stream")
    type_max = MAX_VIDEO_SIZE if content_type.startswith("video/") else MAX_IMAGE_SIZE
    effective_max = min(type_max, MAX_UPLOAD_BYTES)

    body = await _read_request_with_cap(request, effective_max)
    media_id = secrets.token_urlsafe(16)
    is_video = content_type.startswith("video/")
    caller_description = ""
    if isinstance(description, str):
        caller_description = description.strip()[:500]
    elif hasattr(request, "query_params"):
        caller_description = request.query_params.get("description", "").strip()[:500]

    if is_video:
        # 1. Archive to GCS
        try:
            object_path = archive_media(token_hash, media_id, body, content_type)
        except Exception as e:
            logger.error("Failed to archive estimate media for %s: %s", token_hash[:8], e)
            raise HTTPException(status_code=503, detail="Storage unavailable")

        # 2. Atomic claim in Firestore transaction
        db = get_firestore_client()
        doc_ref = db.collection(COLLECTION).document(token_hash)

        def _claim_tx() -> tuple[bool, dict]:
            tx = db.transaction()

            @transactional
            def _transaction_fn(transaction) -> tuple[bool, dict]:
                snapshot = doc_ref.get(transaction=transaction)
                if not snapshot.exists:
                    return False, {}
                data = snapshot.to_dict() or {}
                if time.time() > data.get("expires_at", 0):
                    return False, {}
                if data.get("status") == "processing":
                    return False, data

                attempts = (data.get("attempts") or 0) + 1
                now = time.time()
                updates = {
                    "status": "processing",
                    "attempts": attempts,
                    "lease_expires_at": now + 300.0,
                    "media_id": media_id,
                    "media_object_path": object_path,
                    "media_content_type": content_type,
                    "description": caller_description,
                    "notified_at": None,
                    "result": None,
                }
                transaction.update(doc_ref, updates)
                data.update(updates)
                return True, data

            return _transaction_fn(tx)

        claimed, claim_data = await asyncio.get_running_loop().run_in_executor(None, _claim_tx)
        if not claimed:
            if claim_data.get("status") == "processing":
                return JSONResponse(status_code=409, content={"status": "processing"})
            raise HTTPException(status_code=404, detail="Invalid or expired token")

        # 3. Schedule async background task
        asyncio.create_task(
            _run_async_estimate_analysis(
                token_hash=token_hash,
                media_id=media_id,
                media_bytes=body,
                media_content_type=content_type,
                description=caller_description,
                contractor=contractor,
                caller_phone=estimate.get("caller_phone", ""),
                call_sid=estimate.get("call_sid", ""),
                object_path=object_path,
            )
        )

        return JSONResponse(status_code=202, content={"status": "processing"})

    else:
        # Photo path — synchronous inline execution
        object_path = archive_media(token_hash, media_id, body, content_type)

        now = time.time()
        attempts = (estimate.get("attempts") or 0) + 1
        db = get_firestore_client()
        db.collection(COLLECTION).document(token_hash).update({
            "status": "processing",
            "attempts": attempts,
            "lease_expires_at": now + 300.0,
            "media_id": media_id,
            "media_object_path": object_path,
            "media_content_type": content_type,
            "description": caller_description,
            "notified_at": None,
            "result": None,
        })

        services = contractor.get("services", []) if contractor else []
        business_name = contractor.get("business_name", "") if contractor else ""

        result = await analyze_media(
            media_bytes=body,
            media_type=content_type,
            services_list=services,
            business_name=business_name,
            text_description=caller_description,
        )

        completed_time = time.time()
        db.collection(COLLECTION).document(token_hash).update({
            "status": "complete",
            "result": result,
            "completed_at": completed_time,
            "notified_at": completed_time,
        })

        caller_phone = estimate.get("caller_phone", "")
        call_sid = estimate.get("call_sid", "")
        watch_url = make_watch_url(media_id) if object_path else ""

        await send_estimate_notifications(
            caller_phone=caller_phone,
            contractor=contractor,
            call_sid=call_sid,
            token_hash=token_hash,
            result=result,
            is_failure=False,
            watch_url=watch_url,
        )

        logger.info(
            f"Estimate complete for {redact_phone(caller_phone)}: {result.get('confidence', 'unknown')}"
        )
        return {"status": "complete", "result": result}


# --- Media watch redirect endpoint ---

@router.get("/media/{media_id}")
async def get_estimate_media_redirect(
    media_id: str,
    e: Optional[int] = Query(default=None),
    s: Optional[str] = Query(default=None),
    expires: Optional[int] = Query(default=None),
    sig: Optional[str] = Query(default=None),
):
    """Serve signed GCS redirect URL for contractor to watch customer video."""
    expiry_val = e if e is not None else (expires if expires is not None else 0)
    sig_val = s if s is not None else (sig if sig is not None else "")

    if not verify_watch_sig(media_id, expiry_val, sig_val):
        raise HTTPException(status_code=403, detail="Forbidden")

    db = get_firestore_client()
    docs = list(db.collection(COLLECTION).where("media_id", "==", media_id).limit(1).stream())
    if not docs:
        raise HTTPException(status_code=404, detail="Media not found")

    data = docs[0].to_dict() or {}
    object_path = data.get("media_object_path", "")
    if not object_path:
        raise HTTPException(status_code=404, detail="Media not found")

    redirect_url = gcs_redirect_url(object_path)
    if not redirect_url:
        raise HTTPException(status_code=404, detail="Media unavailable")

    return RedirectResponse(url=redirect_url, status_code=302)
