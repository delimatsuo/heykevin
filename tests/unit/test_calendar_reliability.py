"""Google Calendar read reliability, timezone, and privacy behavior."""

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services import calendar


def test_packaged_tzdata_resolves_iana_zone_without_system_database():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from datetime import datetime; "
                "from zoneinfo import ZoneInfo; "
                "print(int(datetime(2026, 3, 8, 8, 30, "
                "tzinfo=ZoneInfo('America/New_York')).utcoffset().total_seconds()))"
            ),
        ],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONTZPATH": ""},
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "-14400"


class _FakeResponse:
    def __init__(self, status_code: int, body: dict, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text

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


def _patch_client(monkeypatch, responses):
    calls = []
    monkeypatch.setattr(calendar.httpx, "AsyncClient", lambda: _FakeAsyncClient(calls, responses))
    return calls


@pytest.mark.asyncio
async def test_availability_uses_contractor_timezone_hours_and_dst_offset(monkeypatch):
    fixed_now = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(calendar, "_utc_now", lambda: fixed_now, raising=False)
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "calendars": {
                        "primary": {
                            "busy": [
                                {
                                    "start": "2026-03-08T13:30:00Z",
                                    "end": "2026-03-08T14:30:00Z",
                                }
                            ]
                        }
                    }
                },
            )
        ],
    )
    contractor = {
        "google_calendar_access_token": "access-token",
        "timezone": "America/New_York",
        "business_hours_start": "08:30",
        "business_hours_end": "11:30",
    }

    slots = await calendar.get_available_slots(contractor, days_ahead=1)

    assert slots == [
        {
            "date": "Sun Mar 08",
            "start": "8:30 AM",
            "end": "9:30 AM",
            "start_iso": "2026-03-08T08:30:00-04:00",
            "end_iso": "2026-03-08T09:30:00-04:00",
        },
        {
            "date": "Sun Mar 08",
            "start": "10:30 AM",
            "end": "11:30 AM",
            "start_iso": "2026-03-08T10:30:00-04:00",
            "end_iso": "2026-03-08T11:30:00-04:00",
        },
    ]
    request = calls[0][1]
    assert request["headers"]["Authorization"] == "Bearer access-token"
    assert request["json"]["timeZone"] == "America/New_York"


@pytest.mark.asyncio
async def test_expiring_access_token_refreshes_and_persists_before_freebusy(monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(config.settings, "google_calendar_client_secret", "client-secret")
    monkeypatch.setattr(calendar, "_epoch_now", lambda: 1_000.0, raising=False)
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(200, {"access_token": "new-access", "expires_in": 3600}),
            _FakeResponse(200, {"calendars": {"primary": {"busy": []}}}),
        ],
    )
    persisted = []

    async def fake_read(_contractor_id):
        return {}

    async def fake_write(contractor_id, updates):
        persisted.append((contractor_id, updates))

    monkeypatch.setattr(calendar, "_read_google_tokens", fake_read, raising=False)
    monkeypatch.setattr(calendar, "_write_google_tokens", fake_write, raising=False)
    contractor = {
        "contractor_id": "contractor-1",
        "google_calendar_access_token": "old-access",
        "google_calendar_refresh_token": "refresh-token",
        "google_calendar_token_expires_at": 1_030.0,
        "timezone": "UTC",
        "business_hours_start": "09:00",
        "business_hours_end": "17:00",
    }

    await calendar.get_available_slots(contractor, days_ahead=1)

    assert [call[0] for call in calls] == [calendar.TOKEN_URL, calendar.FREEBUSY_URL]
    assert calls[0][1]["data"]["refresh_token"] == "refresh-token"
    assert calls[1][1]["headers"]["Authorization"] == "Bearer new-access"
    assert contractor["google_calendar_access_token"] == "new-access"
    assert contractor["google_calendar_refresh_token"] == "refresh-token"
    assert contractor["google_calendar_token_expires_at"] == 4_600.0
    assert persisted == [
        (
            "contractor-1",
            {
                "google_calendar_access_token": "new-access",
                "google_calendar_refresh_token": "refresh-token",
                "google_calendar_token_expires_at": 4_600.0,
                "google_calendar_token_refreshed_at": 1_000.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_concurrent_expiry_refreshes_once_per_contractor(monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(config.settings, "google_calendar_client_secret", "client-secret")
    monkeypatch.setattr(calendar, "_epoch_now", lambda: 1_000.0)
    calls = _patch_client(
        monkeypatch,
        [_FakeResponse(200, {"access_token": "new-access", "expires_in": 3600})],
    )
    stored = {
        "google_calendar_access_token": "old-access",
        "google_calendar_refresh_token": "refresh-token",
        "google_calendar_token_expires_at": 1_030.0,
    }

    async def fake_read(_contractor_id):
        return dict(stored)

    async def fake_write(_contractor_id, updates):
        stored.update(updates)

    monkeypatch.setattr(calendar, "_read_google_tokens", fake_read)
    monkeypatch.setattr(calendar, "_write_google_tokens", fake_write)
    first = {"contractor_id": "contractor-concurrent", **stored}
    second = {"contractor_id": "contractor-concurrent", **stored}

    results = await asyncio.gather(
        calendar.refresh_access_token(first),
        calendar.refresh_access_token(second),
    )

    assert results == ["new-access", "new-access"]
    assert [call[0] for call in calls] == [calendar.TOKEN_URL]


@pytest.mark.asyncio
async def test_freebusy_401_refreshes_once_and_omits_provider_payload_from_logs(monkeypatch, caplog):
    from app import config

    sensitive_payload = "private-provider-detail-do-not-log"
    monkeypatch.setattr(config.settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(config.settings, "google_calendar_client_secret", "client-secret")
    monkeypatch.setattr(calendar, "_epoch_now", lambda: 2_000.0, raising=False)
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(401, {}, text=sensitive_payload),
            _FakeResponse(200, {"access_token": "new-access", "expires_in": 3600}),
            _FakeResponse(200, {"calendars": {"primary": {"busy": []}}}),
        ],
    )

    async def fake_read(_contractor_id):
        return {}

    async def fake_write(_contractor_id, _updates):
        return None

    monkeypatch.setattr(calendar, "_read_google_tokens", fake_read, raising=False)
    monkeypatch.setattr(calendar, "_write_google_tokens", fake_write, raising=False)
    contractor = {
        "contractor_id": "contractor-2",
        "google_calendar_access_token": "old-access",
        "google_calendar_refresh_token": "refresh-token",
        "google_calendar_token_expires_at": 9_999.0,
        "timezone": "UTC",
        "business_hours_start": "09:00",
        "business_hours_end": "17:00",
    }

    with caplog.at_level(logging.ERROR):
        await calendar.get_available_slots(contractor, days_ahead=1)

    assert [call[0] for call in calls] == [calendar.FREEBUSY_URL, calendar.TOKEN_URL, calendar.FREEBUSY_URL]
    assert calls[2][1]["headers"]["Authorization"] == "Bearer new-access"
    assert sensitive_payload not in caplog.text


@pytest.mark.asyncio
async def test_freebusy_error_logging_uses_status_only(monkeypatch, caplog):
    sensitive_payload = "private-calendar-response-do-not-log"
    _patch_client(monkeypatch, [_FakeResponse(403, {}, text=sensitive_payload)])

    with caplog.at_level(logging.ERROR):
        with pytest.raises(calendar.GoogleCalendarUnavailableError):
            await calendar.get_available_slots("access-token", days_ahead=1)

    assert "Google FreeBusy error" in caplog.text
    assert "status_code=403" in caplog.text
    assert sensitive_payload not in caplog.text


@pytest.mark.asyncio
async def test_freebusy_calendar_error_fails_closed_without_logging_payload(monkeypatch, caplog):
    sensitive_payload = "private-calendar-error-detail-do-not-log"
    _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "calendars": {
                        "primary": {
                            "busy": [],
                            "errors": [{"reason": "forbidden", "message": sensitive_payload}],
                        }
                    }
                },
            )
        ],
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(calendar.GoogleCalendarUnavailableError):
            await calendar.get_available_slots("access-token", days_ahead=1)

    assert "Google FreeBusy calendar error" in caplog.text
    assert "error_count=1" in caplog.text
    assert sensitive_payload not in caplog.text


@pytest.mark.asyncio
async def test_invalid_timezone_fails_closed_before_provider_request(monkeypatch, caplog):
    calls = _patch_client(monkeypatch, [_FakeResponse(200, {"calendars": {"primary": {"busy": []}}})])
    contractor = {
        "google_calendar_access_token": "access-token",
        "timezone": "Not/A-Timezone",
        "business_hours_start": "08:00",
        "business_hours_end": "17:00",
    }

    with caplog.at_level(logging.ERROR):
        with pytest.raises(calendar.GoogleCalendarUnavailableError):
            await calendar.get_available_slots(contractor, days_ahead=1)

    assert calls == []
    assert "Google Calendar configuration invalid" in caplog.text
    assert "Not/A-Timezone" not in caplog.text


@pytest.mark.asyncio
async def test_missing_contractor_schedule_fails_closed_before_provider_request(monkeypatch):
    calls = _patch_client(monkeypatch, [_FakeResponse(200, {"calendars": {"primary": {"busy": []}}})])
    contractor = {
        "google_calendar_access_token": "access-token",
        "timezone": "America/New_York",
    }

    with pytest.raises(calendar.GoogleCalendarUnavailableError):
        await calendar.get_available_slots(contractor, days_ahead=1)

    assert calls == []
