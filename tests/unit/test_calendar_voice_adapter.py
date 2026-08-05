"""Voice pipeline <-> Google Calendar adapter: contractor context and typed failures."""

import json
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services.voice_pipeline import VoicePipeline


async def _noop(*_args, **_kwargs):
    return None


def _pipeline(config):
    return VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        on_call_complete=_noop,
        call_sid="test-call",
        contractor_config=config,
    )


@pytest.mark.asyncio
async def test_google_check_availability_passes_contractor_context(monkeypatch):
    seen = []

    async def fake_get_available_slots(contractor, days):
        seen.append((contractor, days))
        return []

    monkeypatch.setattr("app.services.calendar.get_available_slots", fake_get_available_slots)
    config = {
        "contractor_id": "contractor-1",
        "google_calendar_access_token": "access-token",
        "google_calendar_refresh_token": "refresh-token",
        "timezone": "America/New_York",
        "business_hours_start": "08:30",
        "business_hours_end": "17:30",
    }
    pipeline = _pipeline(config)

    result = json.loads(await pipeline._execute_tool("check_availability", {"days_ahead": 5}))

    assert result == {"available_slots": [], "days_checked": 5}
    assert seen == [(config, 5)]


@pytest.mark.asyncio
async def test_google_check_availability_distinguishes_provider_failure_from_no_slots(monkeypatch):
    from app.services.calendar import GoogleCalendarUnavailableError

    async def fake_get_available_slots(_contractor, _days):
        raise GoogleCalendarUnavailableError("provider unavailable")

    monkeypatch.setattr("app.services.calendar.get_available_slots", fake_get_available_slots)
    pipeline = _pipeline({
        "contractor_id": "contractor-1",
        "google_calendar_access_token": "access-token",
    })

    result = json.loads(await pipeline._execute_tool("check_availability", {"days_ahead": 5}))

    assert result == {"error": "Calendar availability is temporarily unavailable."}
