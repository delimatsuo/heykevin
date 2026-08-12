"""Subscription management API endpoints."""

import os
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.middleware.auth import require_contractor_access, verify_api_token
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/subscription", dependencies=[Depends(verify_api_token)])

# Legacy in-memory rate limiting for the lighter-weight verify/promo-eligible
# endpoints. The promotional-offer signing path (`sign-offer`) uses a persistent
# Firestore-backed limiter — see `_persistent_rate_limit_check` below.
_rate_limits: dict = defaultdict(list)

VERIFY_RATE_LIMIT = 5    # requests per minute (per instance — best-effort)
PROMO_RATE_LIMIT = 3     # requests per minute (per instance — best-effort)


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


# ---------------------------------------------------------------------------
# Persistent (cross-instance) rate limit for /sign-offer (F-12).
#
# Defaults: 5 requests / 15 minutes per contractor. Tunable via env vars so we
# can tighten in response to abuse without a code change.
# ---------------------------------------------------------------------------

def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


SIGN_OFFER_LIMIT = _int_env("SIGN_OFFER_RATE_LIMIT", 5)
SIGN_OFFER_WINDOW_SECONDS = _int_env("SIGN_OFFER_RATE_WINDOW_SECONDS", 900)


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
    """Verify an App Store transaction and update subscription status.

    Fails *closed* (F-05): when Apple's API is unreachable or returns an
    unexpected shape, we return HTTP 502 + Retry-After so the iOS client
    retries with backoff. We never claim verification succeeded unless Apple
    explicitly confirmed the receipt.
    """
    require_contractor_access(request, body.contractor_id)

    from app.db.apple_transactions import get_transaction_binding
    from app.services.subscription import (
        CrossContractorReceiptError,
        is_transaction_seen,
        mark_transaction_seen,
        update_subscription_from_transaction,
        verify_transaction_strict,
    )

    # Per-contractor dedup (cheap idempotency for retries from this same client).
    # This must happen before the rate limit so legitimate StoreKit retries do
    # not turn a successful purchase into noisy 429s.
    if await is_transaction_seen(body.contractor_id, body.transaction_id):
        logger.info(f"Duplicate transaction ignored: {body.transaction_id}")
        return {"status": "ok", "message": "already_processed"}

    # Global receipt-replay defense (F-06): even before calling Apple, if this
    # transaction_id is bound to a *different* contractor, reject. We also do
    # the authoritative bind inside update_subscription_from_transaction, but
    # the early check avoids spurious Apple round-trips and gives a clearer
    # 409 response.
    pre_binding = await get_transaction_binding(body.transaction_id)
    if pre_binding and pre_binding.get("contractor_id") and pre_binding["contractor_id"] != body.contractor_id:
        logger.error(
            "verify_subscription cross-contractor reject: tx=%s requested_by=%s owned_by=%s",
            body.transaction_id, body.contractor_id, pre_binding["contractor_id"],
        )
        raise HTTPException(
            status_code=409,
            detail="receipt_already_bound",
        )

    if not _check_rate_limit(body.contractor_id, VERIFY_RATE_LIMIT, ":verify"):
        raise HTTPException(status_code=429, detail="Too many verification requests")

    result = await verify_transaction_strict(body.transaction_id)

    if result.unreachable:
        # Apple is unreachable / unexpected shape → fail closed. Do NOT claim ok.
        logger.warning(
            "Apple verification unreachable for %s reason=%s — failing closed",
            body.transaction_id, result.reason,
        )
        return JSONResponse(
            status_code=502,
            headers={"Retry-After": "30"},
            content={
                "status": "verification_failed",
                "reason": result.reason or "apple_unreachable",
                "retry_after_seconds": 30,
            },
        )

    if not result.ok or not result.transaction:
        # Apple replied authoritatively that this receipt is invalid /
        # not found / bundle mismatch. Do not claim success.
        logger.warning(
            "Apple verification authoritatively rejected %s reason=%s",
            body.transaction_id, result.reason,
        )
        raise HTTPException(
            status_code=400,
            detail=f"verification_rejected:{result.reason or 'invalid'}",
        )

    try:
        updated = await update_subscription_from_transaction(body.contractor_id, result.transaction)
    except CrossContractorReceiptError as e:
        logger.error("verify_subscription cross-contractor reject (atomic): %s", e)
        raise HTTPException(status_code=409, detail="receipt_already_bound")

    if updated:
        await mark_transaction_seen(body.contractor_id, body.transaction_id)
        return {"status": "ok", "message": "updated"}

    return {"status": "error", "message": "update_failed"}


@router.get("/promo-eligible")
async def get_promo_eligible(contractor_id: str, request: Request):
    """Check if promo offer is available (boolean only — no count exposed)."""
    require_contractor_access(request, contractor_id)

    # Existing iOS builds attach a promotional offer whenever this endpoint
    # returns true for an expired server-side trial. A server trial is not
    # evidence that the Apple ID has ever subscribed, but Apple only permits
    # promotional offers for current or former App Store subscribers. Fail
    # closed so already-installed builds immediately use the regular StoreKit
    # purchase path instead of presenting an offer Apple will reject.
    if not settings.subscription_promotional_offers_enabled:
        return {"eligible": False}

    if not _check_rate_limit(contractor_id, PROMO_RATE_LIMIT, ":promo"):
        raise HTTPException(status_code=429, detail="Too many requests")

    from app.services.subscription import check_promo_eligible
    eligible = await check_promo_eligible()
    return {"eligible": eligible}


@router.post("/sign-offer")
async def sign_offer(body: SignOfferRequest, request: Request):
    """Sign a StoreKit promotional offer. Atomically claims a promo slot.

    Rate limited via a Firestore-backed rolling-window counter so the budget is
    shared across all Cloud Run instances (F-12). Defaults to 5 requests per
    15 minutes per contractor; tunable via SIGN_OFFER_RATE_LIMIT and
    SIGN_OFFER_RATE_WINDOW_SECONDS environment variables.
    """
    require_contractor_access(request, body.contractor_id)

    # Deliberately do not apply the eligibility kill switch here. Released
    # clients silently fall back to a regular-price purchase when signing does
    # not return a signature, even if their already-open paywall still displays
    # the promotional price. Returning false from /promo-eligible is the safe
    # compatibility boundary; it takes effect when the paywall is reloaded.

    from app.db.rate_limits import check_and_increment

    rl = await check_and_increment(
        scope="sign_offer",
        key=body.contractor_id,
        limit=SIGN_OFFER_LIMIT,
        window_seconds=SIGN_OFFER_WINDOW_SECONDS,
    )
    if not rl.allowed:
        retry_after = max(1, int(rl.retry_after_seconds))
        logger.warning(
            "sign-offer rate limit exceeded: contractor=%s window_count=%s retry_after=%ss",
            body.contractor_id, rl.count_in_window, retry_after,
        )
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={
                "status": "rate_limited",
                "retry_after_seconds": retry_after,
            },
        )

    from app.services.subscription import claim_promo_slot, sign_promotional_offer

    # 1. Try signing first (pure local operation)
    signature_data = sign_promotional_offer(
        product_id=body.product_id,
        offer_id=body.offer_id,
        application_username=body.application_username,
    )
    if not signature_data:
        return {"status": "error", "message": "signing_failed"}

    # 2. Only claim slot if signing succeeded
    claimed = await claim_promo_slot()
    if not claimed:
        return {"status": "ineligible", "message": "promo_limit_reached"}

    return {"status": "ok", "signature": signature_data}
