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
from app.services.estimate_notifications import send_estimate_notifications
from app.services.estimate_worker import (
    claim_notification_and_complete,
    claim_notification_and_fail,
)
from app.services.gated_actions import ActionKey, GateContext, check_gated_action
from app.services.side_effect_audit import record_gate_decision
from app.services import sms as sms_module
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
    now = time.time()

    doc_data = {
        "token_hash": token_hash,
        "contractor_id": body.contractor_id,
        "caller_phone": body.caller_phone,
        "call_sid": body.call_sid,
        "created_at": now,
        "expires_at": now + TOKEN_EXPIRY_SECONDS,
        "status": "pending",
        "upload_count": 0,
        "result": None,
        "completed_at": None,
    }

    db = get_firestore_client()
    db.collection(COLLECTION).document(token_hash).set(doc_data)

    logger.info(f"Created estimate token for {redact_phone(body.caller_phone)} ({body.contractor_id})")
    estimate_url = f"https://heykevin.one/estimate/{token}"
    return {
        "status": "ok",
        "token": token,
        "url": estimate_url,
    }


# --- Public endpoints (token in URL path) ---

async def _get_estimate_doc(token: str) -> Optional[dict]:
    """Look up an estimate by raw token (hashes token first)."""
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
    """Public endpoint: token is the auth. Pollable: returns status + result."""
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
    """Get a signed upload URL. Public — token is the auth."""
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
    """Background task to run Gemini analysis for an estimate."""
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

        now = time.time()
        accepted, should_notify, _ = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: claim_notification_and_complete(db, doc_ref, media_id, result, now),
        )
        if accepted and should_notify:
            watch_url = make_watch_url(media_id) if object_path else ""
            sms_fn = send_sms if send_sms is not sms_module.send_sms else None
            await send_estimate_notifications(
                caller_phone=caller_phone,
                contractor=contractor,
                call_sid=call_sid,
                token_hash=token_hash,
                result=result,
                is_failure=False,
                watch_url=watch_url,
                send_sms_fn=sms_fn,
            )
    except Exception as error:
        logger.error(
            "Async estimate analysis failed for %s: %s",
            token_hash[:8],
            type(error).__name__,
        )
        now = time.time()
        try:
            accepted, should_notify, _ = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: claim_notification_and_fail(db, doc_ref, media_id, now),
            )
            if accepted and should_notify:
                watch_url = make_watch_url(media_id) if object_path else ""
                sms_fn = send_sms if send_sms is not sms_module.send_sms else None
                await send_estimate_notifications(
                    caller_phone=caller_phone,
                    contractor=contractor,
                    call_sid=call_sid,
                    token_hash=token_hash,
                    is_failure=True,
                    watch_url=watch_url,
                    send_sms_fn=sms_fn,
                )
        except Exception as e:
            logger.error(
                "Failed to mark estimate %s as failed: %s",
                token_hash[:8],
                type(e).__name__,
            )


def _claim_upload(
    *,
    media_id: str,
    object_path: Optional[str],
    content_type: str,
    caller_description: str,
    token_hash: str,
) -> tuple[bool, dict]:
    """Atomically claim the estimate for one upload attempt.

    Shared by the video and photo paths: only pending or a terminal state may
    transition to processing, so a photo submitted while a video attempt is
    running is rejected instead of clobbering that attempt's media identity.

    Every claim also appends the attempt to media_ids/media_paths. A re-upload
    replaces the *current* media_id, but a watch link texted for an earlier
    attempt was signed for 90 days — the history keeps it resolvable.
    """
    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(token_hash)
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
        media_ids = list(data.get("media_ids") or [])
        media_paths = dict(data.get("media_paths") or {})
        if object_path:
            media_ids.append(media_id)
            media_paths[media_id] = object_path
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
            "media_ids": media_ids,
            "media_paths": media_paths,
        }
        transaction.update(doc_ref, updates)
        data.update(updates)
        return True, data

    return _transaction_fn(tx)


@router.post("/{token}/upload")
async def upload_and_analyze(
    token: str,
    request: Request,
    description: Optional[str] = Query(default="", max_length=500),
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
    # Not dead code: when tests (or any caller) invoke this handler directly
    # rather than through FastAPI, `description` is the Query FieldInfo object,
    # and stringifying it stores framework repr garbage as the caller's words.
    # An earlier review wrongly removed this guard; it is load-bearing.
    caller_description = description.strip()[:500] if isinstance(description, str) else ""

    # Cheap pre-check before any GCS write: a repeated POST while an attempt
    # is already processing must not archive another object. The handler-start
    # snapshot may be slightly stale; the claim transaction below remains the
    # authority — this only stops the storage spend on the obvious case, since
    # the public upload endpoint does not enforce upload_count.
    if estimate.get("status") == "processing":
        return JSONResponse(status_code=409, content={"status": "processing"})

    if is_video:
        # 1. Archive to GCS
        try:
            object_path = archive_media(token_hash, media_id, body, content_type)
        except Exception as e:
            logger.error("Failed to archive estimate media for %s: %s", token_hash[:8], type(e).__name__)
            raise HTTPException(status_code=503, detail="Storage unavailable")

        # 2. Atomic claim in Firestore transaction
        claimed, claim_data = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _claim_upload(
                media_id=media_id,
                object_path=object_path,
                content_type=content_type,
                caller_description=caller_description,
                token_hash=token_hash,
            ),
        )
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
        # Photo path — synchronous inline execution with resilient GCS archiving
        object_path = None
        try:
            object_path = archive_media(token_hash, media_id, body, content_type)
        except Exception as e:
            logger.error("Failed to archive photo media for %s: %s", token_hash[:8], type(e).__name__)

        # Same atomic claim as video: a photo submitted while a video attempt
        # is processing must not replace that attempt's media identity.
        claimed, claim_data = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _claim_upload(
                media_id=media_id,
                object_path=object_path,
                content_type=content_type,
                caller_description=caller_description,
                token_hash=token_hash,
            ),
        )
        if not claimed:
            if claim_data.get("status") == "processing":
                return JSONResponse(status_code=409, content={"status": "processing"})
            raise HTTPException(status_code=404, detail="Invalid or expired token")

        db = get_firestore_client()

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

        sms_fn = send_sms if send_sms is not sms_module.send_sms else None
        await send_estimate_notifications(
            caller_phone=caller_phone,
            contractor=contractor,
            call_sid=call_sid,
            token_hash=token_hash,
            result=result,
            is_failure=False,
            watch_url=watch_url,
            send_sms_fn=sms_fn,
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
    # media_ids keeps every attempt, so a watch link texted before a re-upload
    # (signed for 90 days) still resolves after media_id moves on. The
    # equality fallback covers documents written before the history existed.
    docs = list(
        db.collection(COLLECTION).where("media_ids", "array_contains", media_id).limit(1).stream()
    )
    if not docs:
        docs = list(db.collection(COLLECTION).where("media_id", "==", media_id).limit(1).stream())
    if not docs:
        raise HTTPException(status_code=404, detail="Media not found")

    data = docs[0].to_dict() or {}
    object_path = (data.get("media_paths") or {}).get(media_id) or (
        data.get("media_object_path", "") if data.get("media_id") == media_id else ""
    )
    if not object_path:
        raise HTTPException(status_code=404, detail="Media not found")

    redirect_url = gcs_redirect_url(object_path)
    if not redirect_url:
        raise HTTPException(status_code=404, detail="Media unavailable")

    return RedirectResponse(url=redirect_url, status_code=302)
