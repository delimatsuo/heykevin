"""Google Calendar OAuth token refresh behavior.

Google Calendar access tokens are opaque bearer strings (unlike Jobber's,
which are JWTs we can decode locally) and expire in ~1 hour. Before this
module existed, `refresh_access_token` in app/services/calendar.py had zero
callers: get_available_slots() and book_appointment() were invoked directly
with whatever access_token sat in Firestore at connect time, so every
Google Calendar integration silently broke ~1 hour after a contractor
connected. These tests pin the fix: proactive refresh when we know a token
is expiring, and a reactive refresh-and-retry-once when we don't (e.g. for
contractors who connected before this fix and have no stored expiry).
"""

import json
import os
import time

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.services import calendar


def _noop_async(*_args, **_kwargs):
    async def _inner():
        return None
    return _inner()


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _FakeAsyncClient:
    def __init__(self, calls: list, responses: list[_FakeResponse]):
        self.calls = calls
        self.responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _freebusy_ok(busy=None):
    return _FakeResponse(200, {"calendars": {"primary": {"busy": busy or []}}})


@pytest.fixture(autouse=True)
def _reset_locks():
    calendar._REFRESH_LOCKS.clear()
    yield
    calendar._REFRESH_LOCKS.clear()


@pytest.fixture(autouse=True)
def _google_creds(monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(config.settings, "google_calendar_client_secret", "client-secret")


@pytest.mark.asyncio
async def test_refreshes_expiring_token_before_check_availability(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(200, {"access_token": "new-token", "expires_in": 3600}),
        _freebusy_ok(),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))
    monkeypatch.setattr(calendar, "_write_google_calendar_tokens", lambda cid, updates: _noop_async())
    monkeypatch.setattr(calendar, "_read_google_calendar_tokens", lambda cid: _noop_async())

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,  # already expired
    }

    slots = await calendar.get_available_slots(contractor)

    assert len(slots) > 0  # empty busy periods -> the whole window is available
    assert calls[0][0] == calendar.TOKEN_URL
    assert calls[1][0] == calendar.FREEBUSY_URL
    assert calls[1][1]["headers"]["Authorization"] == "Bearer new-token"


@pytest.mark.asyncio
async def test_reuses_fresh_token_without_refresh_call(monkeypatch):
    calls = []
    responses = [_freebusy_ok()]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))
    monkeypatch.setattr(calendar, "_write_google_calendar_tokens", lambda cid, updates: _noop_async())

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "still-good",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() + 3000,
    }

    await calendar.get_available_slots(contractor)

    assert len(calls) == 1
    assert calls[0][0] == calendar.FREEBUSY_URL
    assert calls[0][1]["headers"]["Authorization"] == "Bearer still-good"


@pytest.mark.asyncio
async def test_retries_once_after_401_when_no_expiry_known(monkeypatch):
    """Legacy contractors connected before this fix have no stored expiry.

    The proactive check can't help them (we don't know their token is
    stale), so the reactive 401-retry is the real backstop.
    """
    calls = []
    responses = [
        _FakeResponse(401, {"error": "invalid_token"}),
        _FakeResponse(200, {"access_token": "refreshed-token", "expires_in": 3600}),
        _freebusy_ok(),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))
    monkeypatch.setattr(calendar, "_write_google_calendar_tokens", lambda cid, updates: _noop_async())
    monkeypatch.setattr(calendar, "_read_google_calendar_tokens", lambda cid: _noop_async())

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "unknown-freshness-token",
        "google_calendar_refresh_token": "refresh-1",
        # no google_calendar_token_expires_at
    }

    slots = await calendar.get_available_slots(contractor)

    assert len(slots) > 0  # empty busy periods -> the whole window is available
    assert calls[0][0] == calendar.FREEBUSY_URL
    assert calls[0][1]["headers"]["Authorization"] == "Bearer unknown-freshness-token"
    assert calls[1][0] == calendar.TOKEN_URL
    assert calls[2][0] == calendar.FREEBUSY_URL
    assert calls[2][1]["headers"]["Authorization"] == "Bearer refreshed-token"


@pytest.mark.asyncio
async def test_persists_refreshed_access_token_and_new_expiry(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(200, {"access_token": "new-token", "expires_in": 1800}),
        _freebusy_ok(),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    persisted = {}

    async def _capture_write(contractor_id, updates):
        persisted["contractor_id"] = contractor_id
        persisted["updates"] = updates

    monkeypatch.setattr(calendar, "_write_google_calendar_tokens", _capture_write)
    monkeypatch.setattr(calendar, "_read_google_calendar_tokens", lambda cid: _noop_async())

    before = time.time()
    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    await calendar.get_available_slots(contractor)

    assert persisted["contractor_id"] == "c1"
    assert persisted["updates"]["google_calendar_access_token"] == "new-token"
    # Google omitted refresh_token in this response -> original must be preserved, not wiped.
    assert persisted["updates"]["google_calendar_refresh_token"] == "refresh-1"
    assert persisted["updates"]["google_calendar_token_expires_at"] >= before + 1800 - 5


@pytest.mark.asyncio
async def test_rotated_refresh_token_is_persisted(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(200, {"access_token": "new-token", "refresh_token": "rotated-refresh", "expires_in": 3600}),
        _freebusy_ok(),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    persisted = {}

    async def _capture_write(contractor_id, updates):
        persisted.update(updates)

    monkeypatch.setattr(calendar, "_write_google_calendar_tokens", _capture_write)
    monkeypatch.setattr(calendar, "_read_google_calendar_tokens", lambda cid: _noop_async())

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    await calendar.get_available_slots(contractor)

    assert persisted["google_calendar_refresh_token"] == "rotated-refresh"


@pytest.mark.asyncio
async def test_book_appointment_also_refreshes_expiring_token(monkeypatch):
    calls = []
    responses = [
        _FakeResponse(200, {"access_token": "new-token", "expires_in": 3600}),
        _FakeResponse(201, {"id": "evt-1"}),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))
    monkeypatch.setattr(calendar, "_write_google_calendar_tokens", lambda cid, updates: _noop_async())
    monkeypatch.setattr(calendar, "_read_google_calendar_tokens", lambda cid: _noop_async())

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    event_id = await calendar.book_appointment(
        contractor,
        title="Estimate",
        start_time="2026-08-05T09:00:00-04:00",
        end_time="2026-08-05T10:00:00-04:00",
    )

    assert event_id == "evt-1"
    assert calls[0][0] == calendar.TOKEN_URL
    assert calls[1][0] == calendar.EVENTS_URL
    assert calls[1][1]["headers"]["Authorization"] == "Bearer new-token"


@pytest.mark.asyncio
async def test_concurrent_refresh_calls_hit_google_once(monkeypatch):
    """Two calendar tool calls racing on the same contractor should not
    both refresh — the second should see the first refresh's fresh token.
    """
    import asyncio

    calls = []
    responses = [
        _FakeResponse(200, {"access_token": "new-token", "expires_in": 3600}),
        _freebusy_ok(),
        _freebusy_ok(),
    ]
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))

    write_count = 0

    async def _capture_write(contractor_id, updates):
        nonlocal write_count
        write_count += 1

    monkeypatch.setattr(calendar, "_write_google_calendar_tokens", _capture_write)
    monkeypatch.setattr(calendar, "_read_google_calendar_tokens", lambda cid: _noop_async())

    contractor = {
        "contractor_id": "c1",
        "google_calendar_access_token": "stale-token",
        "google_calendar_refresh_token": "refresh-1",
        "google_calendar_token_expires_at": time.time() - 10,
    }

    await asyncio.gather(
        calendar.get_available_slots(contractor),
        calendar.get_available_slots(contractor),
    )

    token_url_calls = [c for c in calls if c[0] == calendar.TOKEN_URL]
    assert len(token_url_calls) == 1
    assert write_count == 1
