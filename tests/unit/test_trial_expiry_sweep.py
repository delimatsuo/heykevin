"""Unit tests for the lapsed-trial sweep selection logic."""

import time

from app.services.subscription import should_expire_trial

DAY = 86400


def _c(**kw) -> dict:
    base = {"subscription_status": "trial", "subscription_expires": 0}
    base.update(kw)
    return base


def test_lapsed_trial_is_selected():
    now = time.time()
    assert should_expire_trial(_c(subscription_expires=now - 67 * DAY), now) is True


def test_live_trial_is_not_selected():
    now = time.time()
    assert should_expire_trial(_c(subscription_expires=now + DAY), now) is False


def test_missing_expiry_is_not_selected():
    """Never expire an account on absent data — same fail-open rule as the gate."""
    now = time.time()
    assert should_expire_trial(_c(subscription_expires=0), now) is False
    assert should_expire_trial(_c(subscription_expires=None), now) is False


def test_active_subscription_is_never_selected():
    """`active` expiry is Apple-managed and ambiguous; the sweep must not touch it."""
    now = time.time()
    assert should_expire_trial(
        _c(subscription_status="active", subscription_expires=now - 52 * DAY), now
    ) is False


def test_already_expired_is_not_reselected():
    now = time.time()
    assert should_expire_trial(
        _c(subscription_status="expired", subscription_expires=now - DAY), now
    ) is False


def test_cancelled_is_not_selected():
    now = time.time()
    assert should_expire_trial(
        _c(subscription_status="cancelled", subscription_expires=now - DAY), now
    ) is False


def test_empty_contractor_is_not_selected():
    assert should_expire_trial({}, time.time()) is False
    assert should_expire_trial(None, time.time()) is False


def test_agrees_with_the_gate():
    """The sweep must only expire what the gate already denies."""
    from app.services.subscription import evaluate_subscription_access

    now = time.time()
    lapsed = _c(subscription_expires=now - 30 * DAY)
    assert should_expire_trial(lapsed, now) is True
    assert evaluate_subscription_access(lapsed, now)[0] is False
