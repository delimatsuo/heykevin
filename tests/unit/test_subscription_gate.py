"""Unit tests for the call-time subscription gate.

The gate decides whether an inbound call gets paid features (AI screening) or
the expired-user path (VoIP ring-through / voicemail). Getting it wrong in one
direction gives service away; getting it wrong in the other direction silences a
paying customer's phone. The asymmetry is why `active` and `trial` are treated
differently — see `evaluate_subscription_access` for the reasoning.
"""

import time

from app.services.subscription import evaluate_subscription_access

DAY = 86400


def _c(**kw) -> dict:
    base = {"subscription_status": "trial", "subscription_expires": 0}
    base.update(kw)
    return base


# ---- Fail-open cases (CLAUDE.md design decision #1) -------------------------


def test_missing_contractor_is_allowed():
    allowed, _ = evaluate_subscription_access(None, time.time())
    assert allowed is True


def test_missing_expiry_is_allowed():
    """No expiry timestamp is unknown state, not expired state."""
    now = time.time()
    allowed, _ = evaluate_subscription_access(_c(subscription_status="trial", subscription_expires=0), now)
    assert allowed is True
    allowed, _ = evaluate_subscription_access(_c(subscription_status="active", subscription_expires=None), now)
    assert allowed is True


def test_unrecognized_status_is_allowed():
    now = time.time()
    allowed, _ = evaluate_subscription_access(_c(subscription_status="banana", subscription_expires=now - DAY), now)
    assert allowed is True


# ---- Trial: expiry IS enforced ---------------------------------------------


def test_trial_within_window_is_allowed():
    now = time.time()
    allowed, reason = evaluate_subscription_access(
        _c(subscription_status="trial", subscription_expires=now + 3 * DAY), now
    )
    assert allowed is True
    assert reason == "trial_active"


def test_trial_past_expiry_is_denied():
    """The bug this fixes: 89 accounts sat in `trial` with a long-past expiry."""
    now = time.time()
    allowed, reason = evaluate_subscription_access(
        _c(subscription_status="trial", subscription_expires=now - 67 * DAY), now
    )
    assert allowed is False
    assert reason == "trial_expired"


def test_trial_expiring_exactly_now_is_denied():
    now = time.time()
    allowed, _ = evaluate_subscription_access(
        _c(subscription_status="trial", subscription_expires=now), now
    )
    assert allowed is False


# ---- Active: expiry is NOT enforced, but is flagged ------------------------


def test_active_within_window_is_allowed():
    now = time.time()
    allowed, reason = evaluate_subscription_access(
        _c(subscription_status="active", subscription_expires=now + 10 * DAY), now
    )
    assert allowed is True
    assert reason == "subscription_active"


def test_active_past_expiry_is_still_allowed_but_flagged():
    """A stale `active` expiry is ambiguous.

    `subscription_expires` is advanced by App Store DID_RENEW notifications. A
    past timestamp means either the subscription lapsed OR the notification never
    arrived. Denying on that ambiguity would cut off a customer who is still being
    billed by Apple, so we allow and flag for reconciliation instead.
    """
    now = time.time()
    allowed, reason = evaluate_subscription_access(
        _c(subscription_status="active", subscription_expires=now - 52 * DAY), now
    )
    assert allowed is True
    assert reason == "active_stale_expiry_needs_reconciliation"


# ---- Explicit expired status ------------------------------------------------


def test_expired_status_past_expiry_is_denied():
    now = time.time()
    allowed, reason = evaluate_subscription_access(
        _c(subscription_status="expired", subscription_expires=now - DAY), now
    )
    assert allowed is False
    assert reason == "expired"


def test_expired_status_with_future_expiry_is_allowed():
    """Grace period: status flipped to expired but paid time remains."""
    now = time.time()
    allowed, reason = evaluate_subscription_access(
        _c(subscription_status="expired", subscription_expires=now + 5 * DAY), now
    )
    assert allowed is True
    assert reason == "expired_grace_period"


def test_cancelled_status_with_remaining_time_is_allowed():
    """Cancelled-but-not-yet-lapsed must keep working until the paid term ends."""
    now = time.time()
    allowed, _ = evaluate_subscription_access(
        _c(subscription_status="cancelled", subscription_expires=now + 5 * DAY), now
    )
    assert allowed is True


def test_cancelled_status_past_expiry_is_denied():
    now = time.time()
    allowed, reason = evaluate_subscription_access(
        _c(subscription_status="cancelled", subscription_expires=now - DAY), now
    )
    assert allowed is False
    assert reason == "expired"


# ---- Regression: the exact production records that motivated this ----------


def test_production_record_shapes():
    now = time.time()
    # ggZH2bhqkj: personal, active, expiry 18d past -> allowed, flagged
    allowed, reason = evaluate_subscription_access(
        _c(subscription_status="active", subscription_expires=now - 18 * DAY), now
    )
    assert (allowed, reason) == (True, "active_stale_expiry_needs_reconciliation")

    # lJsm529LGW: trial, expiry 67d past -> denied
    allowed, reason = evaluate_subscription_access(
        _c(subscription_status="trial", subscription_expires=now - 67 * DAY), now
    )
    assert (allowed, reason) == (False, "trial_expired")


# ---- Bugbot findings on PR #143 --------------------------------------------


def test_expired_status_with_missing_expiry_is_denied():
    """Fail-open covers unknown state, not explicitly terminal state.

    Returning early on a missing timestamp let an `expired` account regain
    full access purely because its expiry was absent.
    """
    now = time.time()
    allowed, reason = evaluate_subscription_access(
        _c(subscription_status="expired", subscription_expires=0), now
    )
    assert allowed is False
    assert reason == "expired"


def test_cancelled_status_with_missing_expiry_is_denied():
    now = time.time()
    allowed, _ = evaluate_subscription_access(
        _c(subscription_status="cancelled", subscription_expires=None), now
    )
    assert allowed is False


def test_trial_with_missing_expiry_still_fails_open():
    """Only terminal statuses lose the benefit of the doubt."""
    now = time.time()
    allowed, _ = evaluate_subscription_access(
        _c(subscription_status="trial", subscription_expires=0), now
    )
    assert allowed is True


def test_active_with_missing_expiry_still_fails_open():
    now = time.time()
    allowed, _ = evaluate_subscription_access(
        _c(subscription_status="active", subscription_expires=0), now
    )
    assert allowed is True
