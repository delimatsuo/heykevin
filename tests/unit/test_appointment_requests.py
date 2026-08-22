"""Appointment requests: what happens when Kevin cannot book automatically.

Auto-booking is off by default (`GOOGLE_CREATE_EVENT` is a gated write). That
is a product decision, not a failure, so a denied booking has to come back to
the model as a *recorded request* — otherwise the model reads a bare error,
retries the tool, and improvises a reassurance the caller hears as "reserved".
"""

import json
from datetime import datetime, timedelta, timezone
import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.services import post_call
from app.services.gated_actions import ActionKey
from app.services.voice_pipeline import VoicePipeline, build_system_prompt


async def _noop(*_args, **_kwargs):
    return None


def _pipeline(config, call_sid="CA123"):
    return VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        on_call_complete=_noop,
        call_sid=call_sid,
        contractor_config=config,
    )


def _soon(hour: int) -> str:
    """A slot a few days out, relative to now.

    Frozen dates expire: book_appointment now rejects a start_time outside the
    plausibility window, so a hardcoded date would silently become an
    implausible one and fail this suite on a calendar date rather than a code
    change. Nothing here asserts the literal value.
    """
    when = datetime.now(timezone(timedelta(hours=-4))) + timedelta(days=3)
    return when.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


BOOKING_ARGS = {
    "title": "Faucet repair - John Smith",
    "start_time": _soon(10),
    "end_time": _soon(11),
    "description": "Kitchen sink leaking",
}


def _unauthorized_contractor():
    """Google Calendar connected, but no authorization to write to it."""
    return {
        "contractor_id": "c1",
        "google_calendar_access_token": "gcal-token",
    }


@pytest.fixture
def no_calendar_write(monkeypatch):
    """Fail loudly if a denied booking ever reaches Google Calendar."""
    created = []

    async def fake_book_appointment(*args, **kwargs):
        created.append((args, kwargs))
        return "event-1"

    monkeypatch.setattr("app.services.calendar.book_appointment", fake_book_appointment)
    return created


@pytest.fixture
def saved_calls(monkeypatch):
    saved = []

    async def fake_save_call(call_sid, data):
        saved.append((call_sid, data))
        return True

    monkeypatch.setattr("app.db.calls.save_call", fake_save_call)
    return saved


# --- tool layer -----------------------------------------------------------


@pytest.mark.asyncio
async def test_unauthorized_booking_is_recorded_as_a_request(no_calendar_write, saved_calls):
    pipeline = _pipeline(_unauthorized_contractor())

    result = json.loads(await pipeline._execute_tool("book_appointment", BOOKING_ARGS))

    assert result["status"] == "request_recorded"
    assert result["booked"] is False
    assert no_calendar_write == []


@pytest.mark.asyncio
async def test_recorded_request_tells_the_model_not_to_claim_a_booking(
    no_calendar_write, saved_calls
):
    pipeline = _pipeline(_unauthorized_contractor())

    result = json.loads(await pipeline._execute_tool("book_appointment", BOOKING_ARGS))
    message = result["message"].lower()

    assert "not booked" in message
    assert "confirm" in message
    assert "book_appointment" in message  # explicit "do not retry" instruction


@pytest.mark.asyncio
async def test_recorded_request_is_persisted_to_the_call_record(
    no_calendar_write, saved_calls
):
    pipeline = _pipeline(_unauthorized_contractor(), call_sid="CAbooking")

    await pipeline._execute_tool("book_appointment", BOOKING_ARGS)

    assert len(saved_calls) == 1
    call_sid, data = saved_calls[0]
    assert call_sid == "CAbooking"
    request = data["appointment_request"]
    assert request["start_time"] == BOOKING_ARGS["start_time"]
    assert request["end_time"] == BOOKING_ARGS["end_time"]
    assert request["title"] == BOOKING_ARGS["title"]
    assert request["status"] == "pending_owner_confirmation"


@pytest.mark.asyncio
async def test_owner_confirmation_gate_also_produces_a_request(no_calendar_write, saved_calls):
    """Flag on and integration approved, but automation not approved."""
    pipeline = _pipeline({
        "contractor_id": "c1",
        "google_calendar_access_token": "gcal-token",
        "integration_write_status": "approved",
        "gated_actions": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
    })

    result = json.loads(await pipeline._execute_tool("book_appointment", BOOKING_ARGS))

    assert result["status"] == "request_recorded"
    assert no_calendar_write == []


@pytest.mark.asyncio
async def test_genuine_gate_failures_stay_errors(no_calendar_write, saved_calls):
    """A missing contractor is a bug, not a booking request — do not paper over it."""
    pipeline = _pipeline({"google_calendar_access_token": "gcal-token"})

    result = json.loads(await pipeline._execute_tool("book_appointment", BOOKING_ARGS))

    assert result["success"] is False
    assert "status" not in result
    assert saved_calls == []


@pytest.mark.asyncio
async def test_persistence_failure_does_not_break_the_call(no_calendar_write, monkeypatch):
    """Firestore is best-effort here; the contractor still gets the call SMS."""

    async def failing_save(*_args, **_kwargs):
        raise RuntimeError("firestore down")

    monkeypatch.setattr("app.db.calls.save_call", failing_save)
    pipeline = _pipeline(_unauthorized_contractor())

    result = json.loads(await pipeline._execute_tool("book_appointment", BOOKING_ARGS))

    assert result["status"] == "request_recorded"


# --- contractor SMS -------------------------------------------------------


def _job_data(**overrides):
    data = {
        "call_type": "service_request",
        "urgency": "none",
        "caller_name": "John Smith",
        "caller_phone": "+15551234567",
        "issue_description": "Leaky kitchen faucet",
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_contractor_sms_leads_with_the_appointment_request():
    job_data = _job_data(appointment_request={
        "start_time": "2026-08-11T10:00:00-04:00",
        "status": "pending_owner_confirmation",
    })

    sms = await post_call._format_contractor_sms(
        job_data, "job-1", contractor={"timezone": "America/New_York"}
    )

    lines = sms.splitlines()
    assert "APPOINTMENT REQUEST" in lines[1]
    assert "Tue, Aug 11 at 10:00 AM" in sms
    assert "not confirmed" in sms.lower()


@pytest.mark.asyncio
async def test_contractor_sms_keeps_spoken_noon_when_the_model_stamps_utc():
    """Gemini often writes Friday 12pm as 12:00Z. That is local wall clock, not UTC.

    Converting 12:00 UTC into America/New_York in August yields 8:00 AM, which
    is what the owner SMS showed today while the summary still said 12:00 PM.
    """
    job_data = _job_data(appointment_request={"start_time": "2026-08-21T12:00:00Z"})

    sms = await post_call._format_contractor_sms(
        job_data, "job-1", contractor={"timezone": "America/New_York"}
    )

    assert "Fri, Aug 21 at 12:00 PM" in sms
    assert "8:00 AM" not in sms


@pytest.mark.asyncio
async def test_contractor_sms_converts_a_real_non_utc_offset_into_contractor_time():
    """A Pacific-offset slot is an actual instant and should shift to Eastern."""
    job_data = _job_data(appointment_request={"start_time": "2026-08-11T07:00:00-07:00"})

    sms = await post_call._format_contractor_sms(
        job_data, "job-1", contractor={"timezone": "America/New_York"}
    )

    assert "Tue, Aug 11 at 10:00 AM" in sms


@pytest.mark.asyncio
async def test_utc_booking_args_are_stored_as_contractor_local_iso(
    no_calendar_write, saved_calls
):
    contractor = {
        **_unauthorized_contractor(),
        "timezone": "America/New_York",
    }
    pipeline = _pipeline(contractor, call_sid="CAzulu")

    # Dynamic future date: this call goes through slot_is_bookable, which
    # (correctly) refuses past slots — a hardcoded date detonated here the
    # day after it was written. The behavior under test: a Z-stamped time is
    # local wall clock, kept verbatim and re-stamped with the contractor's
    # own offset for that date (computed, so DST changes can't rot it).
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    future_day = (datetime.now(tz) + timedelta(days=3)).date()
    offset = datetime(future_day.year, future_day.month, future_day.day,
                      12, tzinfo=tz).strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}"

    await pipeline._execute_tool(
        "book_appointment",
        {
            **BOOKING_ARGS,
            "start_time": f"{future_day}T12:00:00Z",
            "end_time": f"{future_day}T13:00:00Z",
        },
    )

    request = saved_calls[0][1]["appointment_request"]
    assert request["start_time"].startswith(f"{future_day}T12:00:00")
    assert request["start_time"].endswith(offset)
    assert request["end_time"].endswith(offset)


@pytest.mark.asyncio
async def test_contractor_sms_survives_an_unparseable_requested_time():
    job_data = _job_data(appointment_request={"start_time": "whenever works"})

    sms = await post_call._format_contractor_sms(job_data, "job-1", contractor={})

    assert "APPOINTMENT REQUEST" in sms
    assert "whenever works" in sms


# --- prompt guidance ------------------------------------------------------


def test_business_prompt_asks_kevin_to_close_warmly():
    """Call CA40a9f4 ended on "Deli will reach out directly to you" and hung up.

    The scheduling rule told Kevin what to convey but nothing about closing,
    so he delivered the fact and stopped. The sign-off also carries the
    goodbye phrase the teardown listens for.
    """
    prompt = build_system_prompt({"contractor_id": "c1", "effective_mode": "business"})

    lowered = prompt.lower()
    assert "thank" in lowered
    assert "goodbye" in lowered


def test_business_prompt_forbids_claiming_an_unconfirmed_appointment():
    """The tool result says this too, but only after Kevin has already spoken."""
    prompt = build_system_prompt({"contractor_id": "c1", "effective_mode": "business"})

    assert "SCHEDULING" in prompt
    lowered = prompt.lower()
    assert "booked" in lowered
    assert "request" in lowered
    # Contractors with no calendar connected get this rule too, so it must not
    # read as permission to invent times.
    assert "check_availability" in prompt


@pytest.mark.asyncio
async def test_contractor_sms_without_a_request_is_unchanged():
    sms = await post_call._format_contractor_sms(
        _job_data(), "job-1", contractor={"timezone": "America/New_York"}
    )

    assert "APPOINTMENT REQUEST" not in sms
    assert sms.splitlines()[1] == "From: John Smith"
