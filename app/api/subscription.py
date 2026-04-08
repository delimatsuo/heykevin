"""Subscription management API endpoints."""

import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.middleware.auth import verify_api_token, require_contractor_access
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/subscription", dependencies=[Depends(verify_api_token)])

# Simple in-memory rate limiting (per contractor, resets on restart)
# {contractor_id: [(timestamp), ...]}
_rate_limits: dict = defaultdict(list)

VERIFY_RATE_LIMIT = 5    # requests per minute
PROMO_RATE_LIMIT = 3     # requests per minute


def _check_rate_limit(contractor_id: str, limit: int, key_suffix: str = "") -> bool:
    """Return True if under rate limit, False if exceeded."""
    key = f"{contractor_id}{key_suffix}"
    now = time.time()
    window_start = now - 60
    calls = [t for t in _rate_limits[key] if t > window_start]
    _rate_limits[key] = calls
    if len(calls) >= limit:
        return False
    _rate_limits[key].append(now)
    return True


class VerifyRequest(BaseModel):
    transaction_id: str
    contractor_id: str


class SignOfferRequest(BaseModel):
    contractor_id: str
    product_id: str
    offer_id: str
    application_username: str


@router.post("/verify")
async def verify_subscription(body: VerifyRequest, request: Request):
    """Verify an App Store transaction and update subscription status."""
    require_contractor_access(request, body.contractor_id)

    if not _check_rate_limit(body.contractor_id, VERIFY_RATE_LIMIT, ":verify"):
        raise HTTPException(status_code=429, detail="Too many verification requests")

    from app.services.subscription import (
        verify_transaction, is_transaction_seen, mark_transaction_seen,
        update_subscription_from_transaction,
    )

    # Deduplication
    if await is_transaction_seen(body.contractor_id, body.transaction_id):
        logger.info(f"Duplicate transaction ignored: {body.transaction_id}")
        return {"status": "ok", "message": "already_processed"}

    transaction_info = await verify_transaction(body.transaction_id)
    if not transaction_info:
        # Fail open — don't break paying users when Apple API is slow
        logger.warning(f"Apple API verification failed for {body.transaction_id} — failing open")
        return {"status": "ok", "message": "verification_skipped"}

    updated = await update_subscription_from_transaction(body.contractor_id, transaction_info)
    if updated:
        await mark_transaction_seen(body.contractor_id, body.transaction_id)
        return {"status": "ok", "message": "updated"}

    return {"status": "error", "message": "update_failed"}


@router.get("/promo-eligible")
async def get_promo_eligible(contractor_id: str, request: Request):
    """Check if promo offer is available (boolean only — no count exposed)."""
    require_contractor_access(request, contractor_id)

    if not _check_rate_limit(contractor_id, PROMO_RATE_LIMIT, ":promo"):
        raise HTTPException(status_code=429, detail="Too many requests")

    from app.services.subscription import check_promo_eligible
    eligible = await check_promo_eligible()
    return {"eligible": eligible}


@router.post("/sign-offer")
async def sign_offer(body: SignOfferRequest, request: Request):
    """Sign a StoreKit promotional offer. Atomically claims a promo slot."""
    require_contractor_access(request, body.contractor_id)

    if not _check_rate_limit(body.contractor_id, PROMO_RATE_LIMIT, ":sign"):
        raise HTTPException(status_code=429, detail="Too many requests")

    from app.services.subscription import claim_promo_slot, sign_promotional_offer

    # Atomically claim the slot
    claimed = await claim_promo_slot()
    if not claimed:
        return {"status": "ineligible", "message": "promo_limit_reached"}

    signature_data = sign_promotional_offer(
        product_id=body.product_id,
        offer_id=body.offer_id,
        application_username=body.application_username,
    )
    if not signature_data:
        return {"status": "error", "message": "signing_failed"}

    return {"status": "ok", "signature": signature_data}
