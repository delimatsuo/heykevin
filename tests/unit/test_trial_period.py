"""Unit tests for the effective trial window.

Production records disagree about how long a trial lasts: 93 accounts carry a
3-day `subscription_expires` and 5 carry 14 days. The product's trial is 14 days,
so the effective end is derived from `trial_start` rather than trusting whatever
`subscription_expires` happens to hold — which heals the short records without a
migration.

The derivation uses max(), so it can only ever extend a trial, never shorten one.
"""

import time

from app.services.subscription import (
    TRIAL_PERIOD_DAYS,
    evaluate_subscription_access,
    should_expire_trial,
    trial_expires_at,
)

DAY = 86400


def test_trial_period_is_fourteen_days():
    assert TRIAL_PERIOD_DAYS == 14


def test_short_stored_window_is_healed_from_trial_start():
    """The 3-day regression: 93 production records look like this."""
    now = time.time()
    started = now - 5 * DAY
    contractor = {"trial_start": started, "subscription_expires": started + 3 * DAY}
    assert trial_expires_at(contractor) == started + 14 * DAY


def test_longer_stored_window_is_respected():
    """A promo or manual extension must never be shortened by the derivation."""
    now = time.time()
    started = now - 2 * DAY
    contractor = {"trial_start": started, "subscription_expires": started + 30 * DAY}
    assert trial_expires_at(contractor) == started + 30 * DAY


def test_missing_trial_start_falls_back_to_stored_expiry():
    now = time.time()
    assert trial_expires_at({"subscription_expires": now + DAY}) == now + DAY


def test_no_usable_data_returns_zero():
    assert trial_expires_at({}) == 0.0
    assert trial_expires_at(None) == 0.0


def test_day_five_of_a_three_day_record_still_has_access():
    """The regression this fixes: expired on paper, inside the real trial."""
    now = time.time()
    started = now - 5 * DAY
    contractor = {
        "subscription_status": "trial",
        "trial_start": started,
        "subscription_expires": started + 3 * DAY,
    }
    allowed, reason = evaluate_subscription_access(contractor, now)
    assert allowed is True
    assert reason == "trial_active"
    assert should_expire_trial(contractor, now) is False


def test_day_fifteen_is_denied_and_swept():
    now = time.time()
    started = now - 15 * DAY
    contractor = {
        "subscription_status": "trial",
        "trial_start": started,
        "subscription_expires": started + 3 * DAY,
    }
    allowed, reason = evaluate_subscription_access(contractor, now)
    assert allowed is False
    assert reason == "trial_expired"
    assert should_expire_trial(contractor, now) is True


def test_the_three_production_accounts_inside_a_real_trial():
    """0rAe3YUEJW (day 4.2), gDWhIHQ3to (day 4.9), CoAZDCgjEe (day 13.2)."""
    now = time.time()
    for age in (4.2, 4.9, 13.2):
        started = now - age * DAY
        contractor = {
            "subscription_status": "trial",
            "trial_start": started,
            "subscription_expires": started + 3 * DAY,
        }
        assert evaluate_subscription_access(contractor, now)[0] is True, age
        assert should_expire_trial(contractor, now) is False, age


def test_malformed_trial_start_does_not_shorten_access():
    now = time.time()
    contractor = {
        "subscription_status": "trial",
        "trial_start": "yesterday",
        "subscription_expires": now + DAY,
    }
    assert evaluate_subscription_access(contractor, now)[0] is True
