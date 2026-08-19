"""Voice pipeline <-> Google Calendar adapter: contractor context and typed failures."""

import json
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services.gated_actions import ActionKey
from app.services.voice_pipeline import VoicePipeline


async def _noop(*_args, **_kwargs):
    return None


@pytest.fixture(autouse=True)
def _provider_recovery_ready(monkeypatch):
    from app.services import voice_pipeline

    monkeypatch.setattr(
        voice_pipeline.settings,
        "service_request_recovery_enabled",
        True,
    )


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,tool_input,helper,expected",
    [
        (
            "check_availability",
            {"days_ahead": 5},
            "app.services.calendar.get_available_slots",
            {"available_slots": [], "days_checked": 5},
        ),
        (
            "book_appointment",
            {"title": "Estimate", "start_time": "2026-08-06T09:00:00Z", "end_time": "2026-08-06T10:00:00Z"},
            "managed_create",
            {"success": True, "revision": 1},
        ),
    ],
)
async def test_google_calendar_budget_covers_token_refresh_round_trips(
    monkeypatch, tool_name, tool_input, helper, expected
):
    """A calendar call that needs the 401-refresh-retry path must not time out.

    That path is three sequential Google round-trips (request -> refresh ->
    retry), so the old 3s budget could abort the exact recovery this module
    exists to perform and surface it to the caller as a tool failure.
    """
    import asyncio

    async def slow_helper(*_args, **_kwargs):
        await asyncio.sleep(3.2)
        return (
            []
            if tool_name == "check_availability"
            else {"success": True, "revision": 1}
        )

    pipeline = _pipeline({
        "contractor_id": "contractor-1",
        "google_calendar_access_token": "access-token",
        "google_calendar_refresh_token": "refresh-token",
        "gated_actions": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
        "automation_approvals": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
        "integration_write_status": "approved",
        "service_request_mutations_enabled": True,
    })
    if helper == "managed_create":
        monkeypatch.setattr(
            pipeline,
            "_create_managed_google_booking",
            slow_helper,
        )
    else:
        monkeypatch.setattr(helper, slow_helper)

    result = json.loads(await pipeline._execute_tool(tool_name, tool_input))

    assert result == expected


def test_gemini_tool_dispatch_budget_exceeds_calendar_tool_budget():
    """GeminiPipeline's outer tool budget must not cap the calendar budget.

    GeminiPipeline delegates to VoicePipeline._execute_tool inside its own
    asyncio.wait_for. Gemini is the primary voice engine, so if that outer
    budget is the smaller of the two it wins, and the Google Calendar
    401-refresh-retry recovery gets aborted with a generic "Tool execution
    timed out" before the calendar path's own budget is ever reached —
    making the wider inner budget dead code on the path that actually runs.
    """
    from app.services.gemini_pipeline import GeminiPipeline
    from app.services.voice_pipeline import GOOGLE_CALENDAR_TOOL_TIMEOUT_SECONDS

    assert GeminiPipeline.TOOL_DISPATCH_TIMEOUT_SECONDS > GOOGLE_CALENDAR_TOOL_TIMEOUT_SECONDS


def test_relay_tool_dispatch_budget_exceeds_calendar_tool_budget():
    """ConversationRelay must also leave the managed saga time to finalize."""
    from app.services.relay_pipeline import TOOL_DISPATCH_TIMEOUT_SECONDS
    from app.services.voice_pipeline import GOOGLE_CALENDAR_TOOL_TIMEOUT_SECONDS

    assert TOOL_DISPATCH_TIMEOUT_SECONDS > GOOGLE_CALENDAR_TOOL_TIMEOUT_SECONDS
