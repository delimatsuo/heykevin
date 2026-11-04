"""Quiet-hours routing follows each account's settings.

The previous override ignored those settings and screened every
non-whitelisted caller from 22:00 to 07:00 UTC.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, time as dtime

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from zoneinfo import ZoneInfo

from app.api.settings import _DEFAULT_SETTINGS
from app.services.quiet_hours import (
    get_quiet_hours_routing_override,
    load_quiet_hours_preferences,
    quiet_hours_routing_args,
)
from app.webhooks import twilio_incoming
from tests.unit.test_security_audit_medium import _install_fake_firestore

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
# 00:30 UTC is 19:30 Eastern in November: inside the old shared UTC window,
# outside a 22:00–07:00 Eastern window.
UTC_EVENING = datetime(2026, 11, 3, 0, 30, tzinfo=UTC)
# 04:00 UTC is 23:00 Eastern. 06:30 UTC is 22:30 Pacific.
EASTERN_NIGHT = datetime(2026, 11, 3, 4, 0, tzinfo=UTC)
PACIFIC_NIGHT = datetime(2026, 11, 3, 6, 30, tzinfo=UTC)
TENANT_ID = "tenant-quiet"
KEVIN_NUMBER = "+15550009999"
CALLER = "+15551234567"


def _override(
    score: int,
    *,
    enabled: bool,
    now: datetime,
    timezone: str = "America/New_York",
    start: str = "22:00",
    end: str = "07:00",
):
    return get_quiet_hours_routing_override(
        score,
        enabled=enabled,
        quiet_start=start,
        quiet_end=end,
        timezone=timezone,
        now=now,
    )


def test_clock_anchors_match_the_windows_under_test():
    assert UTC_EVENING.astimezone(NY).time() == dtime(19, 30)
    assert EASTERN_NIGHT.astimezone(NY).time() == dtime(23, 0)
    assert PACIFIC_NIGHT.astimezone(ZoneInfo("America/Los_Angeles")).time() == dtime(22, 30)
    assert dtime(22, 0) <= UTC_EVENING.time() or UTC_EVENING.time() <= dtime(7, 0)


def test_disabled_account_is_not_screened_inside_the_old_utc_window():
    assert _override(50, enabled=False, now=UTC_EVENING) is None
    assert get_quiet_hours_routing_override(50, now=PACIFIC_NIGHT) is None


def test_enabled_account_uses_its_own_zone_not_utc():
    assert _override(50, enabled=True, now=UTC_EVENING) is None
    assert _override(50, enabled=True, now=EASTERN_NIGHT) == "ai_screening"


def test_window_edges_are_inclusive_and_same_day_windows_work():
    start = datetime(2026, 11, 2, 22, 0, tzinfo=NY)
    end = datetime(2026, 11, 3, 7, 0, tzinfo=NY)
    before = datetime(2026, 11, 2, 21, 59, tzinfo=NY)
    after = datetime(2026, 11, 3, 7, 0, 1, tzinfo=NY)
    assert _override(50, enabled=True, now=start) == "ai_screening"
    assert _override(50, enabled=True, now=end) == "ai_screening"
    assert _override(50, enabled=True, now=before) is None
    assert _override(50, enabled=True, now=after) is None

    midday = datetime(2026, 11, 2, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
    closing = datetime(2026, 11, 2, 17, 1, tzinfo=ZoneInfo("America/Chicago"))
    assert (
        _override(
            50, enabled=True, now=midday, timezone="America/Chicago", start="09:00", end="17:00"
        )
        == "ai_screening"
    )
    assert (
        _override(
            50, enabled=True, now=closing, timezone="America/Chicago", start="09:00", end="17:00"
        )
        is None
    )


def test_whitelist_rings_through_and_other_scores_are_screened():
    assert _override(100, enabled=True, now=EASTERN_NIGHT) is None
    assert _override(99, enabled=True, now=EASTERN_NIGHT) == "ai_screening"
    assert _override(0, enabled=True, now=EASTERN_NIGHT) == "ai_screening"


def test_bad_zone_or_clock_does_not_apply_a_fallback_window():
    assert _override(50, enabled=True, now=EASTERN_NIGHT, timezone="Not/AZone") is None
    assert _override(50, enabled=True, now=EASTERN_NIGHT, start="night") is None
    assert _override(50, enabled=True, now=UTC_EVENING, timezone=" ") is None


def test_routing_args_match_the_settings_defaults_and_ignore_non_bools():
    missing = quiet_hours_routing_args(None)
    assert missing == {
        "enabled": _DEFAULT_SETTINGS["quiet_hours_enabled"],
        "quiet_start": _DEFAULT_SETTINGS["quiet_hours_start"],
        "quiet_end": _DEFAULT_SETTINGS["quiet_hours_end"],
        "timezone": _DEFAULT_SETTINGS["quiet_hours_tz"],
    }
    assert quiet_hours_routing_args({"quiet_hours_enabled": "true"})["enabled"] is False
    assert quiet_hours_routing_args({"quiet_hours_enabled": 1})["enabled"] is False
    stored = quiet_hours_routing_args(
        {
            "quiet_hours_enabled": True,
            "quiet_hours_start": " 21:00 ",
            "quiet_hours_end": "06:30",
            "quiet_hours_tz": " America/Chicago ",
        }
    )
    assert stored == {
        "enabled": True,
        "quiet_start": "21:00",
        "quiet_end": "06:30",
        "timezone": "America/Chicago",
    }


@pytest.mark.asyncio
async def test_preferences_loader_fails_closed(monkeypatch):
    assert await load_quiet_hours_preferences("") == {}

    class _Broken:
        def collection(self, _name):
            raise RuntimeError("firestore down")

    monkeypatch.setattr("app.db.firestore_client.get_firestore_client", lambda: _Broken())
    assert await load_quiet_hours_preferences(TENANT_ID) == {}

    fake = _install_fake_firestore(monkeypatch)
    assert await load_quiet_hours_preferences(TENANT_ID) == {}

    fake._docs[f"contractors/{TENANT_ID}/settings/preferences"] = {
        "quiet_hours_enabled": True,
        "quiet_hours_tz": "America/New_York",
    }
    loaded = await load_quiet_hours_preferences(TENANT_ID)
    assert loaded["quiet_hours_enabled"] is True
    assert loaded["quiet_hours_tz"] == "America/New_York"


class _FakeFormRequest:
    async def form(self) -> dict[str, str]:
        return {"CallSid": "CA_quiet_hours", "From": CALLER, "To": KEVIN_NUMBER}


def _contractor() -> dict:
    return {
        "contractor_id": TENANT_ID,
        "owner_name": "Owner",
        "owner_phone": "+15550000001",
        "business_name": "Tenant Quiet Plumbing",
        "ring_through_contacts": True,
        "subscription_status": "active",
        "timezone": "America/Los_Angeles",
    }


def _freeze(monkeypatch, instant: datetime) -> None:
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.replace(tzinfo=None)
            return instant.astimezone(tz)

    monkeypatch.setattr("app.services.quiet_hours.datetime", _Frozen)


async def _incoming(
    monkeypatch, contact: dict | None, preferences: dict | None, instant: datetime
) -> str:
    fake = _install_fake_firestore(monkeypatch)
    if preferences is not None:
        fake._docs[f"contractors/{TENANT_ID}/settings/preferences"] = preferences
    _freeze(monkeypatch, instant)

    async def get_contractor_by_twilio_number(_number: str) -> dict:
        return _contractor()

    async def get_contact(_phone: str, contractor_id: str = "") -> dict | None:
        assert contractor_id == TENANT_ID
        return contact

    async def get_call_history(_phone: str, limit: int = 10) -> list:
        return []

    async def record_inbound_call_evidence(*_args, **_kwargs) -> bool:
        return True

    async def record_measurement(*_args, **_kwargs) -> None:
        return None

    async def register_conference(*_args, **_kwargs) -> None:
        return None

    recorded = asyncio.Event()

    async def record_routing(**_kwargs) -> None:
        recorded.set()

    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_twilio_number",
        get_contractor_by_twilio_number,
    )
    monkeypatch.setattr(
        "app.db.contractors.record_inbound_call_evidence",
        record_inbound_call_evidence,
    )
    monkeypatch.setattr(twilio_incoming, "_record_inbound_call_measurement", record_measurement)
    monkeypatch.setattr("app.db.contacts.get_contact", get_contact)
    monkeypatch.setattr("app.db.calls.get_call_history", get_call_history)
    monkeypatch.setattr("app.services.circuit_breaker.is_circuit_open", lambda: False)
    monkeypatch.setattr(
        "app.services.conference_registry.new_conference_name",
        lambda _prefix: "opaque_conference",
    )
    monkeypatch.setattr(
        "app.services.conference_registry.register_conference",
        register_conference,
    )
    monkeypatch.setattr(twilio_incoming, "_post_routing_tasks", record_routing)

    response = await twilio_incoming.handle_incoming_call(_FakeFormRequest())
    await asyncio.wait_for(recorded.wait(), timeout=1)
    return bytes(response.body).decode()


@pytest.mark.asyncio
async def test_incoming_call_screens_only_inside_the_account_window(monkeypatch):
    known = {"name": "Sam", "trust_level": 80}

    async def prepare(*_args):
        return True

    async def ring(**_kwargs):
        return None

    monkeypatch.setattr(twilio_incoming, "_prepare_direct_call", prepare)
    monkeypatch.setattr(twilio_incoming, "_ring_contractor", ring)
    enabled = {
        "quiet_hours_enabled": True,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
        "quiet_hours_tz": "America/New_York",
    }

    during = await _incoming(monkeypatch, known, enabled, EASTERN_NIGHT)
    assert "<Stream" in during
    assert "<Conference" not in during

    outside_utc_window = await _incoming(monkeypatch, known, enabled, UTC_EVENING)
    assert "<Conference" in outside_utc_window
    assert "<Stream" not in outside_utc_window

    disabled = await _incoming(
        monkeypatch,
        known,
        {"quiet_hours_enabled": False, "quiet_hours_tz": "America/New_York"},
        EASTERN_NIGHT,
    )
    assert "<Conference" in disabled
    assert "<Stream" not in disabled


@pytest.mark.asyncio
async def test_incoming_call_keeps_spam_blocked_during_quiet_hours(monkeypatch):
    twiml = await _incoming(
        monkeypatch,
        {"is_blacklisted": True},
        {
            "quiet_hours_enabled": True,
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "07:00",
            "quiet_hours_tz": "America/New_York",
        },
        EASTERN_NIGHT,
    )
    assert "<Reject" in twiml
    assert "<Stream" not in twiml
