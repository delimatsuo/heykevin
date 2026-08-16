"""Unit tests for the guard on releasing a contractor's Twilio number.

Releasing is irreversible and dangerous. Twilio reassigns released numbers to
other customers after the FCC's 45-day aging period, so releasing a number that
still has live call forwarding pointed at it routes a user's missed calls — and
their voicemails — to a stranger's phone system.

Holding a number costs about a dollar a month. The asymmetry is the whole reason
this guard exists: when in doubt, keep paying.
"""

import time

from app.services.subscription import is_safe_to_release_number

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
