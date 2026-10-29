"""Re-check stale paid subscriptions against the App Store.

An account's `active`/`expired` status only moves when Apple sends a
notification. One that never arrives leaves the account `active` with a past
expiry for good: the webhook rejected every genuine notification until
2026-09-02, and Apple stops retrying after 72 hours. evaluate_subscription_access
keeps serving such an account rather than cut off a customer Apple may still be
billing, and asks callers to reconcile it against the App Store instead.

This is that reconciliation. When a call reaches an `active` account whose
expiry passed STALE_GRACE_SECONDS ago, it asks Apple, in the background, for the
status of the receipt bound to that account, and applies only an explicit
answer: ACTIVE writes the renewed tier and expiry; EXPIRED or REVOKED marks the
account expired and sends the subscription-ended push. Billing retry, grace
period, no bound receipt, a receipt that doesn't match the account, or any
failure to reach Apple changes nothing.

Default OFF (APPSTORE_STALE_RECHECK_ENABLED): enabling it is an owner action.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from app.config import settings
from app.services.subscription import (
    APPSTORE_PRODUCTION_URL,
    PRODUCT_TO_TIER,
    _active_subscription_expires_ts,
    _decode_jws_payload,
    _get_appstore_jwt,
    _get_transaction_lookup_urls,
    _is_transaction_not_found,
    notify_subscription_ended,
    parse_revocation_date,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Leave Apple's own renewal and notification lag alone before asking.
STALE_GRACE_SECONDS = 3 * 86400
# Per instance: a busy line asks Apple about an account at most once a window.
RECHECK_INTERVAL_SECONDS = 6 * 3600

# Apple's status codes for auto-renewable subscriptions that this acts on.
APPLE_ACTIVE = 1
APPLE_EXPIRED = 2
APPLE_REVOKED = 5

_last_attempt: dict[str, float] = {}
_tasks: set[asyncio.Task] = set()


def _stale_expiry(contractor: dict, now: float) -> Optional[float]:
    """The account's expiry if it is `active` and expired long enough to ask Apple."""
    if contractor.get("subscription_status") != "active":
        return None
    expires = contractor.get("subscription_expires")
    if not isinstance(expires, (int, float)) or isinstance(expires, bool) or expires <= 0:
        return None
    if now - expires < STALE_GRACE_SECONDS:
        return None
    return expires


def schedule_stale_subscription_recheck(contractor: dict, now: float) -> bool:
    """Start a background re-check if this account is due one. Never raises."""
    try:
        if not settings.appstore_stale_recheck_enabled:
            return False
        contractor_id = contractor.get("contractor_id")
        if not isinstance(contractor_id, str) or not contractor_id:
            return False
        if _stale_expiry(contractor, now) is None:
            return False
        last = _last_attempt.get(contractor_id)
        if last is not None and now - last < RECHECK_INTERVAL_SECONDS:
            return False
        _last_attempt[contractor_id] = now
        task = asyncio.create_task(_recheck_and_log(contractor_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("App Store re-check not scheduled: %s", type(e).__name__)
        return False


async def _recheck_and_log(contractor_id: str) -> None:
    try:
        outcome = await recheck_stale_subscription(contractor_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "App Store re-check failed for %s (%s); no change", contractor_id, type(e).__name__,
        )
        return
    logger.info("App Store re-check for %s: %s", contractor_id, outcome)


async def _apple_last_transaction(original_transaction_id: str) -> Optional[dict]:
    """Apple's latest transaction for this receipt, or None if no environment this
    service may see knows it. Raises when Apple can't be asked."""
    token = _get_appstore_jwt()
    async with httpx.AsyncClient(timeout=20.0) as client:
        for base in _get_transaction_lookup_urls():
            url = f"{base}/inApps/v1/subscriptions/{original_transaction_id}"
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 200:
                for group in response.json().get("data", []):
                    for item in group.get("lastTransactions", []):
                        if (
                            isinstance(item, dict)
                            and str(item.get("originalTransactionId", "")) == original_transaction_id
                        ):
                            return item
                return None
            if _is_transaction_not_found(response):
                continue
            env = "production" if base == APPSTORE_PRODUCTION_URL else "sandbox"
            raise RuntimeError(f"App Store status HTTP {response.status_code} ({env})")
    return None


def _receipt_matches(transaction: object, original_transaction_id: str, contractor: dict) -> bool:
    """True if Apple's transaction is this app's, this receipt's, and this account's."""
    if not isinstance(transaction, dict):
        return False
    if transaction.get("bundleId") != settings.appstore_bundle_id:
        return False
    if str(transaction.get("originalTransactionId", "")) != original_transaction_id:
        return False
    token = transaction.get("appAccountToken")
    if token:
        expected = str(contractor.get("subscription_uuid") or "")
        if str(token).lower() != expected.lower():
            return False
    return True


def _expired_by(transaction: dict, now: float) -> bool:
    """True if the transaction's paid term ended before `now`."""
    try:
        expires_ms = float(transaction.get("expiresDate") or 0)
    except (TypeError, ValueError):
        return False
    return 0 < expires_ms / 1000.0 <= now


async def recheck_stale_subscription(contractor_id: str, now: Optional[float] = None) -> str:
    """Ask Apple about one stale `active` account and apply an explicit answer.

    Returns the outcome, for the log line and for tests.
    """
    from app.db.apple_transactions import get_transaction_binding
    from app.db.contractors import (
        StaleSubscriptionUpdate,
        get_contractor,
        update_stale_subscription,
    )

    now = time.time() if now is None else now
    if not settings.appstore_key_id:
        return "not_configured"

    contractor = await get_contractor(contractor_id)
    if not contractor or contractor.get("active") is not True:
        return "not_stale"
    stale_expires = _stale_expiry(contractor, now)
    if stale_expires is None:
        return "not_stale"

    original_id = str(contractor.get("subscription_original_transaction_id") or "").strip()
    if not original_id:
        return "no_receipt"
    binding = await get_transaction_binding(original_id)
    if not binding or binding.get("contractor_id") != contractor_id:
        return "receipt_not_bound_here"

    item = await _apple_last_transaction(original_id)
    if item is None:
        return "apple_has_no_record"
    signed = item.get("signedTransactionInfo")
    transaction = _decode_jws_payload(signed) if isinstance(signed, str) else None
    if not _receipt_matches(transaction, original_id, contractor):
        return "receipt_mismatch"

    status = item.get("status")
    if status == APPLE_ACTIVE:
        tier = PRODUCT_TO_TIER.get(str(transaction.get("productId", "")), "")
        expires_ts = _active_subscription_expires_ts(transaction)
        if not tier or not expires_ts:
            return "no_change"
        updates = {
            "subscription_tier": tier,
            "subscription_status": "active",
            "subscription_expires": expires_ts,
        }
        verdict = "renewed"
    elif status == APPLE_EXPIRED and _expired_by(transaction, now):
        updates = {"subscription_status": "expired"}
        verdict = "expired"
    elif status == APPLE_REVOKED and parse_revocation_date(transaction)[0]:
        updates = {"subscription_status": "expired"}
        verdict = "revoked"
    else:
        return f"no_change(apple_status={status})"

    result = await update_stale_subscription(contractor_id, stale_expires, updates)
    if result is StaleSubscriptionUpdate.MOVED_ON:
        return "moved_on"
    if result is StaleSubscriptionUpdate.HELD:
        return "held_for_number_release"
    if verdict != "renewed":
        await notify_subscription_ended(contractor_id)
    return verdict
