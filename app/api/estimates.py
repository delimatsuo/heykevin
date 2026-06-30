"""AI estimate endpoints — token creation, upload, analysis, results."""

import hashlib
import os
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from app.config import settings
from app.middleware.auth import verify_api_token, require_contractor_access
from app.db.firestore_client import get_firestore_client
from app.db.contractors import get_contractor
from app.services.ai_estimate import analyze_media
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

# Hard absolute cap on any upload, regardless of content type. Defends against
# DoS via memory exhaustion described in SECURITY_AUDIT.md F-10
# (line 37 — `app/api/estimates.py:138-159`). Configurable via env var
# (default 50 MiB to match the largest legitimate video upload — operators
# can lower it to 10 MiB or similar if videos are not required).
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


# --- Authenticated endpoint: create token (called by post-call processing) ---

class CreateTokenRequest(BaseModel):
    contractor_id: str
    caller_phone: str
    call_sid: str = ""


@router.post("/create-token", dependencies=[Depends(verify_api_token)])
async def create_estimate_token(body: CreateTokenRequest, request: Request = None):
    """Create an estimate token for a caller after a service request call."""
    require_contractor_access(request, body.contractor_id)
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
        return {"error": "Invalid or expired token"}, 404

    # Don't expose internal fields
    return {
        "status": estimate.get("status", "pending"),
        "result": estimate.get("result"),
    }


class UploadUrlRequest(BaseModel):
    content_type: str


@router.post("/{token}/upload-url")
async def get_upload_url(token: str, body: UploadUrlRequest):
    """Get a signed GCS upload URL. Public — token is the auth."""
    estimate = await _get_estimate_doc(token)
    if not estimate:
        return {"error": "Invalid or expired token"}, 404

    if estimate.get("upload_count", 0) >= MAX_UPLOADS_PER_TOKEN:
        return {"error": "Upload limit reached"}, 429

    if body.content_type not in ALLOWED_CONTENT_TYPES:
        return {"error": f"File type not allowed. Accepted: {', '.join(ALLOWED_CONTENT_TYPES)}"}, 400

    max_size = MAX_VIDEO_SIZE if body.content_type.startswith("video/") else MAX_IMAGE_SIZE

    # For MVP: accept direct upload to our endpoint instead of GCS
    # TODO: Switch to GCS signed URLs for production scale
    token_hash = _hash_token(token)
    upload_url = f"{settings.cloud_run_url}/api/estimates/{token}/upload"

    # Increment upload count
    db = get_firestore_client()
    db.collection(COLLECTION).document(token_hash).update({
        "upload_count": estimate.get("upload_count", 0) + 1,
    })

    return {
        "upload_url": upload_url,
        "max_size": max_size,
        "content_type": body.content_type,
    }


async def _read_request_with_cap(request, max_bytes: int) -> bytes:
    """Stream the request body and abort early past `max_bytes`.

    F-10 (SECURITY_AUDIT.md line 37): the previous implementation called
    `await request.body()` which buffers the entire payload before any size
    check, letting a malicious client OOM the worker by streaming a multi-GB
    body. We now check `Content-Length` up front (cheap rejection) and also
    accumulate chunks from `request.stream()`, raising 413 the moment the
    running total crosses `max_bytes`.
    """
    # Cheap up-front rejection: trust Content-Length when the client sets it.
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

    # Stream chunks; abort the moment we exceed the cap.
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            # Discard buffered data so it cannot continue to consume RAM.
            chunks.clear()
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max: {max_bytes // (1024 * 1024)}MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/{token}/upload")
async def upload_and_analyze(token: str, request=None):
    """Receive media upload and trigger Gemini analysis.

    For MVP: direct upload. Production should use GCS signed URLs.

    Size handling: rejects oversized uploads early via streamed-chunk
    accumulation, never buffering more than `MAX_UPLOAD_BYTES` in memory
    (SECURITY_AUDIT.md F-10).
    """
    if request is None:
        return {"error": "No request"}, 400

    estimate = await _get_estimate_doc(token)
    if not estimate:
        return {"error": "Invalid or expired token"}, 404

    content_type = request.headers.get("content-type", "application/octet-stream")

    # Per-content-type cap, but never exceeding the absolute MAX_UPLOAD_BYTES.
    type_max = MAX_VIDEO_SIZE if content_type.startswith("video/") else MAX_IMAGE_SIZE
    effective_max = min(type_max, MAX_UPLOAD_BYTES)

    # Stream the body with the cap; raises HTTPException(413) on overflow.
    body = await _read_request_with_cap(request, effective_max)

    # Update status
    token_hash = _hash_token(token)
    db = get_firestore_client()
    db.collection(COLLECTION).document(token_hash).update({"status": "processing"})

    # Get contractor's service list
    contractor = await get_contractor(estimate["contractor_id"])
    services = contractor.get("services", []) if contractor else []
    business_name = contractor.get("business_name", "") if contractor else ""

    # Run Gemini analysis
    result = await analyze_media(
        media_bytes=body,
        media_type=content_type,
        services_list=services,
        business_name=business_name,
    )

    # Store result
    db.collection(COLLECTION).document(token_hash).update({
        "status": "complete",
        "result": result,
        "completed_at": time.time(),
    })

    # Send SMS to customer
    caller_phone = estimate.get("caller_phone", "")
    twilio_number = contractor.get("twilio_number", "") if contractor else ""

    if caller_phone:
        if result.get("requires_manual_investigation"):
            customer_msg = (
                f"Thanks for your upload. This issue will require {business_name}'s "
                f"technician to manually investigate. We are unable to provide an "
                f"AI estimate at this time.\n\n"
                f"Call {business_name}: {twilio_number}"
            )
        else:
            diagnosis = result.get("diagnosis", "")
            est_min = result.get("estimate_min", 0)
            est_max = result.get("estimate_max", 0)
            customer_msg = (
                f"AI Diagnosis: {diagnosis}\n\n"
                f"Estimated Cost: ${est_min}-${est_max}\n\n"
                f"⚠️ This is an AI-generated estimate. The actual cost may differ "
                f"based on the technician's hands-on diagnosis.\n\n"
                f"Call {business_name}: {twilio_number}"
            )
        context = GateContext(
            source="estimate",
            actor="system",
            idempotency_key=f"{token}:caller_sms",
        )
        decision = check_gated_action(contractor, ActionKey.ESTIMATE_RESULT_SMS, context)
        record_gate_decision(
            action=ActionKey.ESTIMATE_RESULT_SMS,
            contractor_id=(contractor or {}).get("contractor_id") or estimate.get("contractor_id", ""),
            source="estimate",
            resource_id=token_hash[:12],
            decision=decision,
        )
        if decision.allowed:
            await send_sms(
                caller_phone,
                customer_msg,
                from_number=twilio_number,
                contractor=contractor,
                action=ActionKey.ESTIMATE_RESULT_SMS,
                gate_context=context,
            )
        else:
            logger.info(
                "Estimate caller SMS blocked by gate",
                extra={"action": ActionKey.ESTIMATE_RESULT_SMS.value, "reason": decision.reason.value},
            )

    # Send SMS to contractor
    contractor_phone = contractor.get("owner_phone", "") if contractor else ""
    if contractor_phone:
        if result.get("requires_manual_investigation"):
            contractor_msg = (
                f"📋 AI ESTIMATE REQUEST\n"
                f"From: {caller_phone}\n"
                f"Result: Requires manual investigation\n"
                f"The AI could not confidently diagnose the issue."
            )
        else:
            diagnosis = result.get("diagnosis", "")
            est_min = result.get("estimate_min", 0)
            est_max = result.get("estimate_max", 0)
            matched = ", ".join(s.get("name", "") for s in result.get("matched_services", []))
            contractor_msg = (
                f"📋 AI ESTIMATE SENT\n"
                f"To: {caller_phone}\n"
                f"Diagnosis: {diagnosis}\n"
                f"Services: {matched}\n"
                f"Estimate: ${est_min}-${est_max}\n"
                f"Confidence: {result.get('confidence', 'unknown')}"
            )
        await send_sms(contractor_phone, contractor_msg, from_number=twilio_number)

    logger.info(f"Estimate complete for {redact_phone(caller_phone)}: {result.get('confidence', 'unknown')}")
    return {"status": "ok", "result": result}
