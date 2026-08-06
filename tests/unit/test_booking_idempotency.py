"""Duplicate-booking protection for Google Calendar event creation.

`check_gated_action` only asserts that an idempotency key is non-empty
(app/services/gated_actions.py:151) — it never stores or compares one. So
before this change, two `book_appointment` tool calls in a single phone
call both passed the gate and created two real customer appointments.

The fix sends a deterministic, content-derived event id to Google, which
enforces uniqueness per calendar and answers a repeat insert with
409 / reason "duplicate". A retry therefore collapses onto the existing
event instead of creating a second one.

Google does not guarantee collision detection at creation time ("Due to
the globally distributed nature of the system, we cannot guarantee that
ID collisions will be detected at event creation time"), so this is
strong best-effort protection rather than an absolute guarantee.
"""

import logging
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services import calendar

BASE32HEX_ALPHABET = set("0123456789abcdefghijklmnopqrstuv")


class _FakeResponse:
    def __init__(self, status_code: int, body: dict, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body


class _FakeAsyncClient:
    def __init__(self, calls: list, responses: list):
        self.calls = calls
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        resp = self.responses.pop(0)
        # Google honours a client-supplied event id and echoes it back, so a
        # successful insert never returns some other id. Modelling that is
        # what makes "a retry resolves to the same appointment" meaningful.
        sent_id = (kwargs.get("json") or {}).get("id")
        if sent_id and resp.status_code in (200, 201):
            resp._body = {**resp._body, "id": sent_id}
        return resp


def _patch_client(monkeypatch, responses):
    calls = []
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))
    return calls


@pytest.fixture(autouse=True)
def _reset_locks():
    calendar._REFRESH_LOCKS.clear()
    yield
    calendar._REFRESH_LOCKS.clear()


def _contractor():
    return {
        "contractor_id": "contractor-1",
        "google_calendar_access_token": "access-token",
        "google_calendar_refresh_token": "refresh-token",
    }


def _booking(**overrides):
    booking = {
        "title": "Estimate",
        "start_time": "2026-08-06T09:00:00Z",
        "end_time": "2026-08-06T10:00:00Z",
        "description": "Leaking water heater",
        "call_sid": "CA_TEST_CALL",
    }
    booking.update(overrides)
    return booking


@pytest.mark.asyncio
async def test_booking_sends_deterministic_event_id_to_google(monkeypatch):
    calls = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])

    await calendar.book_appointment(_contractor(), **_booking())

    sent_id = calls[0][1]["json"]["id"]
    assert sent_id, "an explicit event id must be sent so Google can enforce uniqueness"
    assert 5 <= len(sent_id) <= 1024, "Google requires ids between 5 and 1024 characters"
    assert set(sent_id) <= BASE32HEX_ALPHABET, "Google requires base32hex (0-9, a-v)"


@pytest.mark.asyncio
async def test_identical_booking_requests_produce_the_same_event_id(monkeypatch):
    first = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])
    await calendar.book_appointment(_contractor(), **_booking())

    second = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])
    await calendar.book_appointment(_contractor(), **_booking())

    assert first[0][1]["json"]["id"] == second[0][1]["json"]["id"]


@pytest.mark.asyncio
async def test_duplicate_insert_is_reported_as_success_not_failure(monkeypatch, caplog):
    """A 409 means our own earlier insert won — the appointment exists.

    Returning None here would tell the caller booking failed, and Kevin
    would apologise for a booking that is actually on the calendar.
    """
    _patch_client(
        monkeypatch,
        [_FakeResponse(409, {"error": {"errors": [{"reason": "duplicate"}]}})],
    )

    with caplog.at_level(logging.INFO):
        result = await calendar.book_appointment(_contractor(), **_booking())

    assert result, "a duplicate insert must resolve to the existing event, not a failure"


@pytest.mark.asyncio
async def test_retry_of_the_same_booking_does_not_create_a_second_event(monkeypatch):
    """The scenario this exists for: one tool call retried after a timeout."""
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(200, {"id": "server-assigned"}),
            _FakeResponse(409, {"error": {"errors": [{"reason": "duplicate"}]}}),
        ],
    )

    first = await calendar.book_appointment(_contractor(), **_booking())
    second = await calendar.book_appointment(_contractor(), **_booking())

    assert first and second
    assert first == second, "a retry must resolve to the same appointment"
    assert calls[0][1]["json"]["id"] == calls[1][1]["json"]["id"]


@pytest.mark.asyncio
async def test_two_different_appointments_in_one_call_are_both_bookable(monkeypatch):
    """Idempotency must key on the appointment, not merely on the call.

    The gate's own key is `f"{call_sid}:{action}"`, constant for the whole
    call. Deriving the event id from that alone would make the second
    genuine booking of a call collide with the first and be silently
    swallowed as a duplicate.
    """
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(200, {"id": "server-assigned-1"}),
            _FakeResponse(200, {"id": "server-assigned-2"}),
        ],
    )

    await calendar.book_appointment(_contractor(), **_booking())
    await calendar.book_appointment(
        _contractor(),
        **_booking(
            title="Follow-up",
            start_time="2026-08-08T14:00:00Z",
            end_time="2026-08-08T15:00:00Z",
        ),
    )

    assert calls[0][1]["json"]["id"] != calls[1][1]["json"]["id"]


@pytest.mark.asyncio
async def test_bookings_from_different_calls_never_share_an_event_id(monkeypatch):
    """Two callers requesting the same slot must not collapse into one event."""
    first = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])
    await calendar.book_appointment(_contractor(), **_booking(call_sid="CA_CALLER_ONE"))

    second = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])
    await calendar.book_appointment(_contractor(), **_booking(call_sid="CA_CALLER_TWO"))

    assert first[0][1]["json"]["id"] != second[0][1]["json"]["id"]


@pytest.mark.asyncio
async def test_booking_without_a_call_sid_still_succeeds(monkeypatch):
    """Absent a call sid there is nothing stable to dedupe on.

    Fail open rather than closed: refusing to book would break the tool
    outright, which is worse than the duplicate risk it guards against.
    """
    calls = _patch_client(monkeypatch, [_FakeResponse(200, {"id": "server-assigned"})])

    result = await calendar.book_appointment(_contractor(), **_booking(call_sid=""))

    assert result == "server-assigned"
    assert "id" not in calls[0][1]["json"], "let Google assign an id when we cannot derive one"


@pytest.mark.asyncio
async def test_voice_pipeline_passes_call_sid_so_protection_is_not_inert(monkeypatch):
    """The dedup only works if the live tool path actually supplies a call sid.

    Without this, `book_appointment` falls back to letting Google assign an
    id and every duplicate-protection test above still passes while the
    running system is unprotected.
    """
    import json as _json

    from app.services.gated_actions import ActionKey
    from app.services.voice_pipeline import VoicePipeline

    seen = {}

    async def fake_book(contractor, **kwargs):
        seen.update(kwargs)
        return "event-1"

    monkeypatch.setattr("app.services.calendar.book_appointment", fake_book)

    async def _noop(*_args, **_kwargs):
        return None

    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        on_call_complete=_noop,
        call_sid="CA_LIVE_CALL",
        contractor_config={
            "contractor_id": "contractor-1",
            "google_calendar_access_token": "access-token",
            "gated_actions": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
            "automation_approvals": {ActionKey.GOOGLE_CREATE_EVENT.value: True},
            "integration_write_status": "approved",
        },
    )

    result = _json.loads(
        await pipeline._execute_tool(
            "book_appointment",
            {
                "title": "Estimate",
                "start_time": "2026-08-06T09:00:00Z",
                "end_time": "2026-08-06T10:00:00Z",
            },
        )
    )

    assert result["success"] is True
    assert seen.get("call_sid") == "CA_LIVE_CALL"


@pytest.mark.asyncio
async def test_non_duplicate_conflict_is_still_a_failure(monkeypatch, caplog):
    """A 409 that isn't our duplicate must not be laundered into success."""
    _patch_client(
        monkeypatch,
        [_FakeResponse(409, {"error": {"errors": [{"reason": "conflict"}]}})],
    )

    with caplog.at_level(logging.ERROR):
        result = await calendar.book_appointment(_contractor(), **_booking())

    assert result is None
