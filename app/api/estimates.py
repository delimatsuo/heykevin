"""AI estimate endpoints — token creation, upload, analysis, results."""

import hashlib
import secrets
import time

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from typing import Optional

from app.config import settings
from app.middleware.auth import verify_api_token, require_contractor_access
from app.db.firestore_client import get_firestore_client
from app.db.contractors import get_contractor
from app.services.ai_estimate import analyze_media
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


@router.post("/{token}/upload")
async def upload_and_analyze(token: str, request=None):
    """Receive media upload and trigger Gemini analysis.

    For MVP: direct upload. Production should use GCS signed URLs.
    """
    from fastapi import Request
    if request is None:
        return {"error": "No request"}, 400

    estimate = await _get_estimate_doc(token)
    if not estimate:
        return {"error": "Invalid or expired token"}, 404

    # Read the request body
    body = await request.body()
    content_type = request.headers.get("content-type", "application/octet-stream")

    # Validate size
    max_size = MAX_VIDEO_SIZE if content_type.startswith("video/") else MAX_IMAGE_SIZE
    if len(body) > max_size:
        return {"error": f"File too large. Max: {max_size // (1024*1024)}MB"}, 413

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
        await send_sms(caller_phone, customer_msg, from_number=twilio_number)

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
