"""Unit tests for the guard on releasing a contractor's Twilio number.

Releasing is irreversible and dangerous. Twilio reassigns released numbers to
other customers after the FCC's 45-day aging period, so releasing a number that
still has live call forwarding pointed at it routes a user's missed calls — and
their voicemails — to a stranger's phone system.

Holding a number costs about a dollar a month. The asymmetry is the whole reason
this guard exists: when in doubt, keep paying.
"""

import os
import time

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

from app.services.subscription import is_safe_to_release_number  # noqa: E402

DAY = 86400


def _c(**kw) -> dict:
    base = {"deleted_app_detected_at": None, "forwarding_last_seen_at": None}
    base.update(kw)
    return base


def test_released_after_quiet_period_since_deletion():
    now = time.time()
    assert is_safe_to_release_number(_c(deleted_app_detected_at=now - 20 * DAY), now) is True


def test_not_released_before_14_days():
    now = time.time()
    assert is_safe_to_release_number(_c(deleted_app_detected_at=now - 3 * DAY), now) is False


def test_not_released_without_deletion_signal():
    now = time.time()
    assert is_safe_to_release_number(_c(), now) is False


def test_not_released_while_forwarding_is_still_live():
    """The core guard: recent forwarded traffic proves the forward still points here.

    Even long past the deletion window, releasing now would hand this user's calls
    to whoever Twilio assigns the number to next.
    """
    now = time.time()
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 60 * DAY, forwarding_last_seen_at=now - 2 * DAY), now
    ) is False


def test_released_once_forwarding_has_gone_quiet():
    now = time.time()
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 60 * DAY, forwarding_last_seen_at=now - 45 * DAY), now
    ) is True


def test_old_forwarding_evidence_does_not_block_forever():
    """Evidence older than the quiet window is stale and must not pin a number."""
    now = time.time()
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 30 * DAY, forwarding_last_seen_at=now - 31 * DAY), now
    ) is True


def test_forwarding_seen_today_blocks_even_with_ancient_deletion():
    now = time.time()
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 365 * DAY, forwarding_last_seen_at=now - 60), now
    ) is False


def test_malformed_values_never_release():
    """Fail closed. An unreadable record is not permission to release."""
    now = time.time()
    assert is_safe_to_release_number(_c(deleted_app_detected_at="yesterday"), now) is False
    assert is_safe_to_release_number(None, now) is False
    assert is_safe_to_release_number({}, now) is False


def test_garbage_forwarding_timestamp_blocks_release():
    """If we cannot tell when forwarding was last seen, do not release."""
    now = time.time()
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 60 * DAY, forwarding_last_seen_at="recently"), now
    ) is False


# ---------------------------------------------------------------------------
# 30-day rule for lapsed accounts (owner decision 2026-09-03): a former user's
# number is not worth holding past 30 days. Same asymmetry as above, so the
# quiet-number guard still applies — an expired account whose number still
# receives forwarded calls keeps it.
# ---------------------------------------------------------------------------

from app.services.subscription import (  # noqa: E402
    LAPSED_NUMBER_RELEASE_DAYS,
    is_safe_to_release_lapsed_number,
)


def _lapsed(now: float, *, expired_days_ago: float = 40, **kw) -> dict:
    base = {
        "active": True,
        "subscription_status": "expired",
        "twilio_number": "+15555550100",
        "subscription_expires": now - expired_days_ago * DAY,
        "trial_start": now - (expired_days_ago + 14) * DAY,
        "forwarding_last_seen_at": None,
    }
    base.update(kw)
    return base


def test_lapsed_window_is_thirty_days():
    assert LAPSED_NUMBER_RELEASE_DAYS == 30


def test_lapsed_released_after_thirty_quiet_days():
    now = time.time()
    assert is_safe_to_release_lapsed_number(_lapsed(now, expired_days_ago=30), now, None) is True


def test_lapsed_not_released_at_twenty_nine_days():
    now = time.time()
    assert is_safe_to_release_lapsed_number(_lapsed(now, expired_days_ago=29), now, None) is False


def test_lapsed_active_subscription_is_never_a_candidate():
    """A stale `active` expiry may just be a missed Apple notification."""
    now = time.time()
    c = _lapsed(now, expired_days_ago=100, subscription_status="active")
    assert is_safe_to_release_lapsed_number(c, now, None) is False


def test_lapsed_trial_status_is_never_a_candidate():
    """Trials become `expired` via the lapsed-trial sweep first; this rule never jumps ahead."""
    now = time.time()
    c = _lapsed(now, expired_days_ago=100, subscription_status="trial")
    assert is_safe_to_release_lapsed_number(c, now, None) is False


def test_lapsed_without_a_number_is_not_a_candidate():
    now = time.time()
    assert is_safe_to_release_lapsed_number(_lapsed(now, twilio_number=""), now, None) is False


def test_lapsed_unknown_term_end_never_releases():
    now = time.time()
    c = _lapsed(now, subscription_expires=None, trial_start=None)
    assert is_safe_to_release_lapsed_number(c, now, None) is False


def test_lapsed_uses_the_repaired_fourteen_day_trial_window():
    """Legacy records store a 3-day trial expiry; the effective end is trial_start + 14d."""
    now = time.time()
    start = now - 43 * DAY  # effective end = now - 29d -> hold
    c = _lapsed(now, trial_start=start, subscription_expires=start + 3 * DAY)
    assert is_safe_to_release_lapsed_number(c, now, None) is False
    start = now - 44 * DAY  # effective end = now - 30d -> release
    c = _lapsed(now, trial_start=start, subscription_expires=start + 3 * DAY)
    assert is_safe_to_release_lapsed_number(c, now, None) is True


def test_lapsed_recent_forwarding_evidence_blocks():
    now = time.time()
    c = _lapsed(now, expired_days_ago=90, forwarding_last_seen_at=now - 2 * DAY)
    assert is_safe_to_release_lapsed_number(c, now, None) is False


def test_lapsed_old_forwarding_evidence_does_not_block():
    now = time.time()
    c = _lapsed(now, expired_days_ago=90, forwarding_last_seen_at=now - 31 * DAY)
    assert is_safe_to_release_lapsed_number(c, now, None) is True


def test_lapsed_recent_inbound_call_blocks():
    """Calls still arrive, so someone's forward still points here. Keep paying."""
    now = time.time()
    assert is_safe_to_release_lapsed_number(_lapsed(now, expired_days_ago=90), now, now - 10 * DAY) is False


def test_lapsed_old_inbound_call_does_not_block():
    now = time.time()
    assert is_safe_to_release_lapsed_number(_lapsed(now, expired_days_ago=90), now, now - 31 * DAY) is True


def test_lapsed_unreadable_call_timestamp_blocks():
    now = time.time()
    c = _lapsed(now, expired_days_ago=90)
    assert is_safe_to_release_lapsed_number(c, now, "recently") is False
    assert is_safe_to_release_lapsed_number(c, now, True) is False


def test_lapsed_garbage_forwarding_timestamp_blocks():
    now = time.time()
    c = _lapsed(now, expired_days_ago=90, forwarding_last_seen_at="recently")
    assert is_safe_to_release_lapsed_number(c, now, None) is False


def test_lapsed_malformed_records_never_release():
    now = time.time()
    assert is_safe_to_release_lapsed_number(None, now, None) is False
    assert is_safe_to_release_lapsed_number({}, now, None) is False


def test_lapsed_recent_inbound_stamp_blocks():
    """The incoming webhook stamps every call; a recent stamp proves the number is not quiet."""
    now = time.time()
    c = _lapsed(now, expired_days_ago=90, last_inbound_call_at=now - 5 * DAY)
    assert is_safe_to_release_lapsed_number(c, now, None) is False


def test_lapsed_old_inbound_stamp_does_not_block():
    now = time.time()
    c = _lapsed(now, expired_days_ago=90, last_inbound_call_at=now - 31 * DAY)
    assert is_safe_to_release_lapsed_number(c, now, None) is True


def test_lapsed_unreadable_inbound_stamp_blocks():
    now = time.time()
    for bad in ("recently", True, -1, 0):
        c = _lapsed(now, expired_days_ago=90, last_inbound_call_at=bad)
        assert is_safe_to_release_lapsed_number(c, now, None) is False, bad


# ---------------------------------------------------------------------------
# The always-on deleted-app guard must honour the same inbound-call stamp the
# lapsed guard already does (F: the incoming webhook writes no call record for
# an expired account, so last_inbound_call_at is the only evidence a call
# still landed).
# ---------------------------------------------------------------------------


def test_recent_inbound_stamp_blocks_deleted_app_release():
    now = time.time()
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 30 * DAY, last_inbound_call_at=now - 2 * DAY), now
    ) is False


def test_old_inbound_stamp_does_not_block_deleted_app_release():
    now = time.time()
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 30 * DAY, last_inbound_call_at=now - 20 * DAY), now
    ) is True


def test_unreadable_inbound_stamp_blocks_deleted_app_release():
    now = time.time()
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 30 * DAY, last_inbound_call_at="yesterday"), now
    ) is False
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 30 * DAY, last_inbound_call_at=True), now
    ) is False


def test_missing_inbound_stamp_does_not_change_deleted_app_release():
    now = time.time()
    assert is_safe_to_release_number(_c(deleted_app_detected_at=now - 30 * DAY), now) is True
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 30 * DAY, last_inbound_call_at=None), now
    ) is True


def test_negative_and_bool_timestamps_never_release_on_either_path():
    now = time.time()
    assert is_safe_to_release_number(_c(deleted_app_detected_at=True), now) is False
    assert is_safe_to_release_number(_c(deleted_app_detected_at=now - 60 * DAY, forwarding_last_seen_at=-1), now) is False
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 60 * DAY, last_inbound_call_at=-1), now
    ) is False
    assert is_safe_to_release_number(
        _c(deleted_app_detected_at=now - 60 * DAY, last_inbound_call_at=True), now
    ) is False
    assert is_safe_to_release_lapsed_number(_lapsed(now, expired_days_ago=90, forwarding_last_seen_at=-1), now, None) is False
