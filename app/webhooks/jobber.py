"""Authenticated HTTP webhook receiver for Jobber provider events.

Invariants:
- Default-off: disabled or unconfigured receiver returns 503 Service Unavailable without database or provider effects.
- Exactly one X-Jobber-Hmac-SHA256 signature header is required.
- Cumulative streaming body cap (65536 bytes) enforced before memory buffering.
- Cryptographic HMAC authentication and envelope extraction performed before any JSON parsing or persistence.
- Random admission ID generated outside transaction.
- Atomic control increment, receipt, barrier, and durable work recording executed off asyncio event loop.
- Verified durable acceptance returned as HTTP 200 {"status": "accepted"}.
- No background-only receipt, provider HTTP or fanout before acknowledgement.
- Safe logging only: zero raw bodies, tokens, secret keys, or raw exception strings logged.
"""

from __future__ import annotations

import asyncio
from app.db.jobber_disconnect_transactions import run_bounded_async
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.db.firestore_client import get_firestore_client
from app.db.jobber_disconnect_receipts import (
    JobberDisconnectControlError,
    JobberDisconnectError,
    record_disconnect_admission_atomic,
)
from app.services.jobber_webhook_auth import (
    MAX_JOBBER_WEBHOOK_BODY_BYTES,
    MIN_JOBBER_WEBHOOK_BODY_BYTES,
)
from app.services.jobber_webhook_events import authenticate_jobber_disconnect_envelope
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/jobber")
async def jobber_webhook_receiver(request: Request):
    """Receive and durably record authenticated Jobber APP_DISCONNECT webhook events."""
    # 1. Receiver configuration and enablement gate
    if not getattr(settings, "jobber_disconnect_receiver_enabled", False):
        raise HTTPException(
            status_code=503,
            detail="Jobber webhook receiver is disabled",
        )

    client_secret = settings.jobber_client_secret
    if not client_secret:
        logger.error("Jobber webhook receiver is enabled but client secret is unconfigured")
        raise HTTPException(
            status_code=503,
            detail="Jobber webhook receiver is not configured",
        )

    expected_app_id = settings.jobber_disconnect_expected_app_id
    from app.db.jobber_disconnect_receipts import is_valid_opaque_id
    if not is_valid_opaque_id(expected_app_id):
        logger.error("Jobber webhook receiver is enabled but expected app ID is unconfigured")
        raise HTTPException(
            status_code=503,
            detail="Jobber webhook receiver is not configured",
        )

    # 2. Signature header validation (must have exactly one signature header)
    sig_headers = request.headers.getlist("x-jobber-hmac-sha256")
    if len(sig_headers) != 1:
        raise HTTPException(
            status_code=401,
            detail="Missing or duplicate signature header",
        )
    signature = sig_headers[0]

    # 3. Stream body with cumulative byte cap before memory buffering
    body_chunks: list[bytes] = []
    total_bytes = 0

    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > MAX_JOBBER_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Webhook payload exceeds maximum size limit",
            )
        body_chunks.append(chunk)

    raw_body = b"".join(body_chunks)
    if len(raw_body) < MIN_JOBBER_WEBHOOK_BODY_BYTES:
        raise HTTPException(
            status_code=400,
            detail="Empty or missing webhook payload",
        )

    # 4. Authenticate envelope before JSON parsing
    envelope = authenticate_jobber_disconnect_envelope(
        raw_body=raw_body,
        signature=signature,
        client_secret=client_secret,
        expected_app_id=expected_app_id,
    )
    if envelope is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid webhook signature, schema, or topic",
        )

    # 5. Atomic admission persistence off asyncio event loop
    admission_id = f"adm_{uuid.uuid4().hex}"
    try:
        def persist(deadline):
            return record_disconnect_admission_atomic(
                db=get_firestore_client(), admission_id=admission_id,
                delivery_fingerprint=envelope.delivery_fingerprint,
                app_id=envelope.app_id, account_id=envelope.account_id,
                occurred_at=envelope.occurred_at, enforce_control_exists=True,
                deadline=deadline,
            )
        await run_bounded_async(persist, seconds=6.0)
    except asyncio.TimeoutError:
        logger.error("Jobber disconnect admission persistence timed out")
        raise HTTPException(
            status_code=503,
            detail="Admission persistence timed out",
        ) from None
    except JobberDisconnectControlError:
        logger.error("Jobber disconnect control unavailable or inactive")
        raise HTTPException(
            status_code=503,
            detail="Jobber disconnect control record unavailable",
        ) from None
    except JobberDisconnectError:
        logger.error("Jobber disconnect persistence rejected")
        raise HTTPException(
            status_code=503,
            detail="Disconnect admission rejected",
        ) from None
    except Exception:
        logger.error("Jobber disconnect admission failed unexpectedly")
        raise HTTPException(
            status_code=503,
            detail="Internal admission persistence error",
        ) from None

    logger.info("Jobber disconnect admission accepted and durably persisted")
    return JSONResponse(status_code=200, content={"status": "accepted"})
