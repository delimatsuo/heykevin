"""Google Calendar availability reliability, timezone, and privacy behavior.

Reconciles two independent proposals for hardening app/services/calendar.py
(PR #86 and PR #141) into one implementation. #86 added per-contractor
timezone/business-hours support and defensive FreeBusy response parsing
that #141 lacked; #141 had the correct OAuth-scope math and extended the
proactive/reactive refresh to book_appointment, which #86 never touched.

One deliberate divergence from #86: #86 failed closed (raised) whenever a
contractor's timezone/business-hours fields were simply absent, not just
invalid. Since nothing in this codebase sets those fields yet, every
existing contractor lacks them — shipping that as written would have
silently zeroed out Google Calendar availability for 100% of current
users. Here, absent config falls back to the pre-existing UTC 9-5
default (unchanged behavior for everyone today); an explicitly-set but
unparseable value still fails closed, since guessing past garbage input
is worse than refusing.
"""

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.services import calendar


def test_packaged_tzdata_resolves_iana_zone_without_system_database():
    """ZoneInfo needs an IANA database; Cloud Run's base image doesn't
    reliably ship a system one. The `tzdata` package bundles it — this
    guards against ever losing that guarantee. PYTHONTZPATH="" forces
    zoneinfo to use only its importable-package data sources, not
    whatever the host happens to have installed.
    """
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


@pytest.fixture(autouse=True)
def _reset_locks():
    calendar._REFRESH_LOCKS.clear()
    yield
    calendar._REFRESH_LOCKS.clear()


def _noop_async(*_args, **_kwargs):
    async def _inner():
        return None
    return _inner()


@pytest.mark.asyncio
async def test_availability_uses_contractor_timezone_hours_and_dst_offset(monkeypatch):
    fixed_now = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(calendar, "_utc_now", lambda: fixed_now)
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "calendars": {
                        "primary": {
                            "busy": [
                                {"start": "2026-03-08T13:30:00Z", "end": "2026-03-08T14:30:00Z"}
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

    # 2026-03-08 is the DST-spring-forward date in America/New_York (UTC-5 -> UTC-4).
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
async def test_missing_schedule_falls_back_to_default_utc_business_hours(monkeypatch):
    """The pre-existing UTC 9-5 default, unchanged for the ~100% of
    contractors who have no timezone/business-hours configured today —
    the deliberate divergence from #86 (see module docstring).
    """
    fixed_now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(calendar, "_utc_now", lambda: fixed_now)
    calls = _patch_client(monkeypatch, [_FakeResponse(200, {"calendars": {"primary": {"busy": []}}})])
    contractor = {"google_calendar_access_token": "access-token"}

    slots = await calendar.get_available_slots(contractor, days_ahead=1)

    assert len(calls) == 1  # did NOT fail closed
    assert slots[0]["start_iso"] == "2026-06-02T09:00:00+00:00"
    assert slots[-1]["end_iso"] == "2026-06-02T17:00:00+00:00"
    assert calls[0][1]["json"]["timeZone"] == "UTC"


@pytest.mark.asyncio
async def test_partial_schedule_also_falls_back_to_defaults(monkeypatch):
    """A contractor with SOME but not all fields set (e.g. mid-migration)
    still gets safe UTC 9-5 defaults, not a hard failure.
    """
    calls = _patch_client(monkeypatch, [_FakeResponse(200, {"calendars": {"primary": {"busy": []}}})])
    contractor = {
        "google_calendar_access_token": "access-token",
        "timezone": "America/New_York",
        # business_hours_start / business_hours_end intentionally absent
    }

    await calendar.get_available_slots(contractor, days_ahead=1)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_expiring_access_token_refreshes_and_persists_before_freebusy(monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(config.settings, "google_calendar_client_secret", "client-secret")
    monkeypatch.setattr(calendar, "_epoch_now", lambda: 1_000.0)
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

    monkeypatch.setattr(calendar, "_read_google_calendar_tokens", fake_read)
    monkeypatch.setattr(calendar, "_write_google_calendar_tokens", fake_write)
    contractor = {
        "contractor_id": "contractor-1",
        "google_calendar_access_token": "old-access",
        "google_calendar_refresh_token": "refresh-token",
        "google_calendar_token_expires_at": 1_030.0,
    }

    await calendar.get_available_slots(contractor, days_ahead=1)

    assert [call[0] for call in calls] == [calendar.TOKEN_URL, calendar.FREEBUSY_URL]
    assert calls[0][1]["data"]["refresh_token"] == "refresh-token"
    assert calls[1][1]["headers"]["Authorization"] == "Bearer new-access"
    assert contractor["google_calendar_access_token"] == "new-access"
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
    import asyncio

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

    monkeypatch.setattr(calendar, "_read_google_calendar_tokens", fake_read)
    monkeypatch.setattr(calendar, "_write_google_calendar_tokens", fake_write)
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
    monkeypatch.setattr(calendar, "_epoch_now", lambda: 2_000.0)
    calls = _patch_client(
        monkeypatch,
        [
            _FakeResponse(401, {}, text=sensitive_payload),
            _FakeResponse(200, {"access_token": "new-access", "expires_in": 3600}),
            _FakeResponse(200, {"calendars": {"primary": {"busy": []}}}),
        ],
    )
    monkeypatch.setattr(calendar, "_read_google_calendar_tokens", lambda cid: _noop_async())
    monkeypatch.setattr(calendar, "_write_google_calendar_tokens", lambda cid, updates: _noop_async())
    contractor = {
        "contractor_id": "contractor-2",
        "google_calendar_access_token": "old-access",
        "google_calendar_refresh_token": "refresh-token",
        "google_calendar_token_expires_at": 9_999.0,
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
    contractor = {"google_calendar_access_token": "access-token"}

    with caplog.at_level(logging.ERROR):
        with pytest.raises(calendar.GoogleCalendarUnavailableError):
            await calendar.get_available_slots(contractor, days_ahead=1)

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
    contractor = {"google_calendar_access_token": "access-token"}

    with caplog.at_level(logging.ERROR):
        with pytest.raises(calendar.GoogleCalendarUnavailableError):
            await calendar.get_available_slots(contractor, days_ahead=1)

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
async def test_inverted_business_hours_fails_closed(monkeypatch):
    """An explicitly-set but nonsensical hours range (end before start)
    is a configuration error, not a 24-hour availability window.
    """
    calls = _patch_client(monkeypatch, [_FakeResponse(200, {"calendars": {"primary": {"busy": []}}})])
    contractor = {
        "google_calendar_access_token": "access-token",
        "timezone": "UTC",
        "business_hours_start": "17:00",
        "business_hours_end": "09:00",
    }

    with pytest.raises(calendar.GoogleCalendarUnavailableError):
        await calendar.get_available_slots(contractor, days_ahead=1)

    assert calls == []


@pytest.mark.asyncio
async def test_token_refresh_failure_logging_uses_status_only(monkeypatch, caplog):
    """The refresh path must not log provider-controlled response bodies.

    Every other failure path in this module logs status or exception type
    only. `refresh_access_token` had no callers before this change, so its
    log line goes live on the recovery path for the first time here — and
    a failed refresh is exactly when Google returns an error body.
    """
    from app import config

    sensitive_payload = "refresh-error-detail-do-not-log"
    monkeypatch.setattr(config.settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(config.settings, "google_calendar_client_secret", "client-secret")
    _patch_client(monkeypatch, [_FakeResponse(400, {}, text=sensitive_payload)])
    monkeypatch.setattr(calendar, "_read_google_calendar_tokens", lambda cid: _noop_async())
    monkeypatch.setattr(calendar, "_write_google_calendar_tokens", lambda cid, updates: _noop_async())
    contractor = {
        "contractor_id": "contractor-1",
        "google_calendar_refresh_token": "refresh-token",
    }

    with caplog.at_level(logging.ERROR):
        result = await calendar.refresh_access_token(contractor)

    assert result is None
    assert "Google token refresh failed" in caplog.text
    assert "status_code=400" in caplog.text
    assert sensitive_payload not in caplog.text
