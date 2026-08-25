"""A bad appointment date must never leave the call.

`slot_is_plausible` already guarded the owner's Confirm tap, but that fires at
the very end. On 2026-08-19 a caller asked for "Friday, August 21st", Kevin
accepted it out loud, the owner got an SMS for it, and only Confirm caught that
the stored year was 2020 — by which point the wrong date had reached both the
caller's ears and the owner's phone.

The prompt now states today's date, but a stated date is guidance, not a
guarantee. This rejects at the tool boundary so the model has to re-ask rather
than the system quietly recording fiction.
"""

import json
import os

import pytest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services.voice_pipeline import VoicePipeline


class _Pipe:
    """Borrow the real method without constructing a full pipeline.

    Mirrors the `__new__` idiom the pipelines already use to share
    `_execute_tool` across engines.
    """

    def __init__(self, contractor=None):
        self._contractor_config = contractor or {"timezone": "America/New_York"}
        self._call_sid = "CAtest"

    _reject_implausible_slot = VoicePipeline._reject_implausible_slot


def _iso(days_from_now: int, hour: int = 10) -> str:
    when = datetime.now(timezone(timedelta(hours=-4))) + timedelta(days=days_from_now)
    return when.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def test_the_original_defect_year_2020_is_rejected():
    result = _Pipe()._reject_implausible_slot(
        {"start_time": "2020-08-21T15:00:00-04:00"}
    )

    assert result is not None
    payload = json.loads(result)
    assert payload["booked"] is False
    assert payload["status"] == "date_not_understood"


def test_a_normal_upcoming_slot_passes_through():
    assert _Pipe()._reject_implausible_slot({"start_time": _iso(4)}) is None


def test_rejection_tells_the_model_not_to_retry_the_same_value():
    """A bare error reads as a transient fault — that is the PR #156 bug."""
    payload = json.loads(
        _Pipe()._reject_implausible_slot({"start_time": "2020-08-21T15:00:00-04:00"})
    )

    message = payload["message"]
    assert "Do not call book_appointment again with the same value" in message
    # It must say what to do instead, not merely what not to do.
    assert "Ask the caller" in message


def test_rejection_carries_no_error_key():
    """An "error" key would never reach the model on the legacy pipeline.

    _handle_caller_speech treats any tool result with a truthy `error` as a
    hard failure: it speaks "I can't check the schedule right now", takes a
    message, and returns without another model turn — so the instruction would
    be discarded exactly where it is needed.
    """
    payload = json.loads(
        _Pipe()._reject_implausible_slot({"start_time": "2020-08-21T15:00:00-04:00"})
    )

    assert "error" not in payload


def test_a_year_out_in_either_direction_is_rejected():
    """The likeliest model error, and the lenient Confirm predicate misses it.

    slot_is_plausible allows ±400 days, so both of these pass it. The live
    booking path needs the tighter window or the guard is theatre.
    """
    from app.services.appointment_time import slot_is_plausible

    contractor = {"timezone": "America/New_York"}
    for value in (_iso(-365), _iso(366)):
        assert slot_is_plausible(value, contractor) is True, "precondition"
        assert _Pipe()._reject_implausible_slot({"start_time": value}) is not None


def test_a_booking_a_few_months_out_is_still_allowed():
    """The window must not be so tight it rejects a real advance booking."""
    assert _Pipe()._reject_implausible_slot({"start_time": _iso(120)}) is None


def test_missing_start_time_is_rejected_rather_than_stored_empty():
    assert _Pipe()._reject_implausible_slot({}) is not None
    assert _Pipe()._reject_implausible_slot({"start_time": ""}) is not None


def test_unparseable_start_time_is_rejected():
    assert _Pipe()._reject_implausible_slot({"start_time": "next friday-ish"}) is not None


def test_far_future_is_rejected():
    assert _Pipe()._reject_implausible_slot({"start_time": _iso(900)}) is not None


def test_the_past_is_rejected_for_a_new_booking():
    """A booking agreed on the call is not retrospective.

    Distinct from Confirm, where a slot from last week is a legitimate leftover
    and slot_is_plausible still accepts it.
    """
    from app.services.appointment_time import slot_is_plausible

    contractor = {"timezone": "America/New_York"}
    assert slot_is_plausible(_iso(-3), contractor) is True, "Confirm still allows it"
    assert _Pipe()._reject_implausible_slot({"start_time": _iso(-3)}) is not None


# --- wiring -----------------------------------------------------------------
# The tests above call the method directly, so they would all still pass if the
# call were deleted from the book_appointment handler. These drive the real tool
# dispatcher instead, and assert nothing was written.


def _noop(*_args, **_kwargs):
    return None


def _live_pipeline(call_sid="CAwire"):
    return VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        on_call_complete=_noop,
        call_sid=call_sid,
        # Calendar connected but unauthorized to write — the shape that
        # reaches the request path, matching test_appointment_requests.
        contractor_config={
            "contractor_id": "c1",
            "google_calendar_access_token": "gcal-token",
            "google_calendar_refresh_token": "gcal-refresh",
            "timezone": "America/New_York",
        },
    )


@pytest.mark.asyncio
async def test_tool_call_with_a_2020_date_records_nothing(monkeypatch):
    saved = []

    async def fake_save_call(sid, payload):
        saved.append((sid, payload))
        return True

    monkeypatch.setattr("app.db.calls.save_call", fake_save_call)

    raw = await _live_pipeline()._execute_tool(
        "book_appointment",
        {
            "title": "Toilet replacement",
            "start_time": "2020-08-21T15:00:00-04:00",
            "end_time": "2020-08-21T16:00:00-04:00",
        },
    )

    payload = json.loads(raw)
    assert payload["status"] == "date_not_understood"
    # The whole point: nothing about this date reaches the call record.
    assert saved == []


@pytest.mark.asyncio
async def test_tool_call_with_a_good_date_still_records_the_request(monkeypatch):
    """The guard must not swallow the ordinary path."""
    saved = []

    async def fake_save_call(sid, payload):
        saved.append((sid, payload))
        return True

    monkeypatch.setattr("app.db.calls.save_call", fake_save_call)

    raw = await _live_pipeline()._execute_tool(
        "book_appointment",
        {"title": "Toilet replacement", "start_time": _iso(3), "end_time": _iso(3, 11)},
    )

    payload = json.loads(raw)
    assert payload["status"] == "request_recorded"
    assert len(saved) == 1


def test_grace_means_earlier_today_not_a_rolling_day():
    """At 3pm, yesterday 4pm is 23 hours back — inside a duration grace.

    A duration-based window would accept a date the caller cannot be booking.
    "Already past" is a calendar question.
    """
    from app.services.appointment_time import slot_is_bookable

    contractor = {"timezone": "America/New_York"}
    ny = ZoneInfo("America/New_York")
    now = datetime(2026, 8, 20, 15, 0, tzinfo=ny)

    yesterday_4pm = datetime(2026, 8, 19, 16, 0, tzinfo=ny).isoformat()
    assert slot_is_bookable(yesterday_4pm, contractor, now=now) is False

    # Earlier the same day is still fine — that is what the grace was for.
    earlier_today = datetime(2026, 8, 20, 9, 0, tzinfo=ny).isoformat()
    assert slot_is_bookable(earlier_today, contractor, now=now) is True


def test_recovery_wording_survives_a_year_boundary():
    """"Use the current year" sends a December caller asking for January backwards.

    On Dec 30 2026, "January 2" means 2027. Telling the model to use the
    current year produces a past date that fails this same guard again, looping
    the caller instead of recovering.
    """
    payload = json.loads(
        _Pipe()._reject_implausible_slot({"start_time": "2020-08-21T15:00:00-04:00"})
    )

    assert "current year" not in payload["message"]
    assert "next occurrence" in payload["message"]


def test_prompt_date_rule_survives_a_year_boundary():
    from app.services.voice_pipeline import build_system_prompt

    prompt = build_system_prompt({
        "owner_name": "Deli",
        "business_name": "Electus USA",
        "effective_mode": "business",
        "timezone": "America/New_York",
    })
    anchor = prompt[prompt.index("TODAY'S DATE:"):]
    assert "next occurrence" in anchor
    assert "always use the current year" not in anchor
