"""Release sweep for Twilio numbers held by accounts that no longer need them.

Two independent triggers, both checked against the same quiet-number guard
(no forwarded traffic, no inbound calls) so a still-forwarded number is never
handed back to Twilio:

- deleted app: the APNs 410 signal is at least NUMBER_RELEASE_QUIET_DAYS old
  (is_safe_to_release_number). Always on.
- lapsed account: `expired` for at least LAPSED_NUMBER_RELEASE_DAYS with a
  quiet number (is_safe_to_release_lapsed_number). Gated by
  LAPSED_NUMBER_RELEASE_ENABLED, capped per run, and further protected by:

  * an observation window. "No inbound call stamped" only proves silence
    once the stamp has been live for a full window, so the first run records
    `system/number_release.observation_started_at` and lapsed releases wait
    until that is LAPSED_NUMBER_RELEASE_DAYS old. The marker is written
    whether or not the flag is on, so the window elapses while the flag is
    still off.
  * an Apple re-check. `expired` is written on DID_FAIL_TO_RENEW, i.e. at the
    start of Apple's billing retry, and the webhook that keeps it current was
    broken for weeks (PR #215). An account that ever bound an Apple receipt
    is asked about live, and anything Apple still considers active, in
    billing retry, or in grace holds its number. Any failure to ask holds.

The sweep body lived inline in app/main.py until 2026-09-03 with no tests; it
moved here so it can be driven by tests and so main.py stays a wiring file.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from app.config import settings
from app.db.calls import latest_call_timestamp
from app.db.contractors import deactivate_contractor
from app.db.firestore_client import get_firestore_client
from app.services.sms import send_sms
from app.services.subscription import (
    LAPSED_NUMBER_RELEASE_DAYS,
    NUMBER_RELEASE_QUIET_DAYS,
    _get_appstore_jwt,
    _get_appstore_url,
    is_safe_to_release_lapsed_number,
    is_safe_to_release_number,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Brake against a bad rule or bad data emptying the fleet in one pass. The
# sweep runs every 6 hours, so a backlog still drains within days.
LAPSED_RELEASE_MAX_PER_RUN = 5

OBSERVATION_DOC = ("system", "number_release")

# Apple subscription status codes that mean "still a customer".
_APPLE_HOLD_STATUSES = frozenset({1, 3, 4})  # ACTIVE, IN_BILLING_RETRY, IN_GRACE_PERIOD

DELETED_APP_NOTICE = (
    f"Kevin AI: Your Kevin number has been released after {NUMBER_RELEASE_QUIET_DAYS} days. "
    "To stop forwarding calls to the old number, dial ##61# "
    "(Verizon: dial *73). To reactivate, reinstall Kevin AI."
)

LAPSED_NOTICE = (
    f"Kevin AI: Your Kevin number was released after {LAPSED_NUMBER_RELEASE_DAYS} days "
    "without an active subscription. To stop forwarding calls to it, dial ##61# "
    "(Verizon: dial *73). To get a new number, resubscribe in the Kevin AI app."
)


async def _apple_subscription_statuses(original_transaction_id: str) -> list[int]:
    """Ask the App Store Server API for every status on this receipt. Raises on any failure."""
    url = f"{_get_appstore_url()}/inApps/v1/subscriptions/{original_transaction_id}"
    token = _get_appstore_jwt()
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    if response.status_code != 200:
        raise RuntimeError(f"App Store status HTTP {response.status_code}")
    statuses: list[int] = []
    for group in response.json().get("data", []):
        for item in group.get("lastTransactions", []):
            code = item.get("status")
            if isinstance(code, int) and not isinstance(code, bool):
                statuses.append(code)
    return statuses


async def apple_blocks_release(contractor: dict) -> bool:
    """True if Apple still treats this account's receipt as live, or if we cannot tell."""
    original_id = str(contractor.get("subscription_original_transaction_id") or "").strip()
    if not original_id:
        return False  # never bound a receipt: a lapsed trial, nothing to ask Apple
    try:
        statuses = await _apple_subscription_statuses(original_id)
    except Exception as e:
        logger.warning("Lapsed release: App Store check failed (%s); holding number", type(e).__name__)
        return True
    return any(code in _APPLE_HOLD_STATUSES for code in statuses)


async def _release_reason(data: dict, now: float, contractor_id: str, allow_lapsed: bool) -> Optional[str]:
    """Return "deleted_app", "lapsed", or None. Any lookup failure means None."""
    if is_safe_to_release_number(data, now):
        return "deleted_app"
    if not allow_lapsed:
        return None
    try:
        last_call_at = await latest_call_timestamp(contractor_id)
    except Exception as e:
        # Cannot prove the number is quiet, so it is not.
        logger.warning(
            "Lapsed release: call lookup failed for %s (%s); holding number",
            contractor_id, type(e).__name__,
        )
        return None
    if not is_safe_to_release_lapsed_number(data, now, last_call_at):
        return None
    if await apple_blocks_release(data):
        logger.info("Lapsed release: Apple still reports a live subscription for %s; holding", contractor_id)
        return None
    return "lapsed"


async def _observation_window_elapsed(db, loop, now: float) -> bool:
    """Record the first run and report whether a full window has passed since."""
    ref = db.collection(OBSERVATION_DOC[0]).document(OBSERVATION_DOC[1])
    snap = await loop.run_in_executor(None, ref.get)
    data = (snap.to_dict() or {}) if getattr(snap, "exists", False) else {}
    started = data.get("observation_started_at")
    if not isinstance(started, (int, float)) or isinstance(started, bool) or started <= 0:
        await loop.run_in_executor(None, lambda: ref.set({"observation_started_at": now}, merge=True))
        logger.info("Number release: inbound-call observation window started")
        return False
    remaining = LAPSED_NUMBER_RELEASE_DAYS * 86400 - (now - started)
    if remaining > 0:
        logger.info("Number release: observation window has %.1f days left", remaining / 86400)
        return False
    return True


async def run_expired_contractor_cleanup_once(now: Optional[float] = None) -> dict:
    """One pass over expired, active contractors. Returns release counts."""
    if now is None:
        now = time.time()
    db = get_firestore_client()
    loop = asyncio.get_running_loop()
    counts = {"deleted_app_released": 0, "lapsed_released": 0, "skipped": 0}

    lapsed_enabled = await _observation_window_elapsed(db, loop, now) and bool(
        settings.lapsed_number_release_enabled
    )

    docs = await loop.run_in_executor(
        None,
        lambda: list(
            db.collection("contractors")
            .where("subscription_status", "==", "expired")
            .where("active", "==", True)
            .stream()
        ),
    )

    for doc in docs:
        contractor_id = doc.id
        data = doc.to_dict() or {}
        allow_lapsed = lapsed_enabled and counts["lapsed_released"] < LAPSED_RELEASE_MAX_PER_RUN

        reason = await _release_reason(data, now, contractor_id, allow_lapsed)
        if reason is None:
            continue

        # Re-validate on a fresh read immediately before acting. The query
        # snapshot ages as this loop progresses, and a forwarded call or a
        # device re-registration arriving in that window writes exactly the
        # evidence that must block release (review finding on PR #143).
        fresh = await loop.run_in_executor(
            None,
            lambda cid=contractor_id: db.collection("contractors").document(cid).get(),
        )
        data = fresh.to_dict() or {}
        reason = None if not data.get("active") else await _release_reason(
            data, time.time(), contractor_id, allow_lapsed
        )
        if reason is None:
            logger.info(f"Number release: skipping {contractor_id} — state changed since snapshot")
            counts["skipped"] += 1
            continue

        owner_phone = data.get("owner_phone", "")
        twilio_number = data.get("twilio_number", "")
        logger.info(f"Number release ({reason}): deactivating {contractor_id}")

        # Notice goes out before the number is gone so it is sent from it.
        if owner_phone:
            notice = LAPSED_NOTICE if reason == "lapsed" else DELETED_APP_NOTICE
            await send_sms(owner_phone, notice, from_number=twilio_number)

        try:
            await deactivate_contractor(contractor_id)
        except Exception as e:
            logger.error(f"Deactivate failed for {contractor_id}: {e}")
            continue

        counts[f"{reason}_released"] += 1

    if counts["deleted_app_released"] or counts["lapsed_released"]:
        logger.info(
            "Number release sweep: %d deleted-app, %d lapsed, %d skipped",
            counts["deleted_app_released"], counts["lapsed_released"], counts["skipped"],
        )
    return counts
