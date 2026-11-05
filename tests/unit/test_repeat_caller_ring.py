"""Callers who ring the owner get Kevin as a fallback when nobody answers."""

from __future__ import annotations

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest  # noqa: E402

from app.webhooks import twilio_incoming  # noqa: E402
from tests.unit import test_quiet_hours_routing as harness  # noqa: E402

NO_QUIET_HOURS = {"quiet_hours_enabled": False}


@pytest.fixture
def ring(monkeypatch):
    state = {"prepared": True, "rings": []}

    async def prepare(*_args):
        return state["prepared"]

    async def ring_contractor(**kwargs):
        state["rings"].append(kwargs)

    monkeypatch.setattr(twilio_incoming, "_prepare_direct_call", prepare)
    monkeypatch.setattr(twilio_incoming, "_ring_contractor", ring_contractor)
    return state


async def _call(monkeypatch, contact):
    return await harness._incoming(monkeypatch, contact, NO_QUIET_HOURS, harness.UTC_EVENING)


def _score(monkeypatch, score):
    monkeypatch.setattr(
        "app.services.scoring.calculate_trust_score",
        lambda _phone, _lookups: (score, {}),
    )


@pytest.mark.asyncio
async def test_repeat_caller_rings_the_app_with_kevin_as_fallback(monkeypatch, ring):
    _score(monkeypatch, 80)

    twiml = await _call(monkeypatch, None)

    assert "<Conference" in twiml
    assert "<Number" not in twiml
    assert len(ring["rings"]) == 1
    assert ring["rings"][0]["contractor_id"] == harness.TENANT_ID
    assert ring["rings"][0]["conference_name"] == "opaque_conference"


@pytest.mark.asyncio
async def test_saved_contact_name_is_shown_on_the_ring(monkeypatch, ring):
    await _call(monkeypatch, {"name": "Sam", "trust_level": 80})

    assert ring["rings"][0]["caller_name"] == "Sam"


@pytest.mark.asyncio
async def test_name_the_caller_gave_kevin_is_not_shown_as_identity(monkeypatch, ring):
    _score(monkeypatch, 80)

    async def caller_contact(_contractor_id, _phone):
        return {"caller_name": "Your Bank"}

    monkeypatch.setattr("app.db.contacts.get_caller_contact", caller_contact)

    await _call(monkeypatch, None)

    assert ring["rings"][0]["caller_name"] == ""


@pytest.mark.asyncio
async def test_high_score_without_whitelist_also_gets_the_fallback(monkeypatch, ring):
    twiml = await _call(monkeypatch, {"name": "Sam", "trust_level": 95})

    assert "<Conference" in twiml
    assert "<Number" not in twiml
    assert len(ring["rings"]) == 1


@pytest.mark.asyncio
async def test_whitelisted_contact_still_rings_the_app(monkeypatch, ring):
    twiml = await _call(monkeypatch, {"name": "Sam", "is_whitelisted": True})

    assert "<Conference" in twiml
    assert len(ring["rings"]) == 1


@pytest.mark.asyncio
async def test_ring_through_off_sends_repeat_callers_to_kevin(monkeypatch, ring):
    _score(monkeypatch, 80)
    monkeypatch.setattr(
        harness,
        "_contractor",
        lambda: {
            "contractor_id": harness.TENANT_ID,
            "owner_phone": "+15550000001",
            "ring_through_contacts": False,
            "subscription_status": "active",
        },
    )

    twiml = await _call(monkeypatch, None)

    assert "<Stream" in twiml
    assert "<Number" not in twiml
    assert ring["rings"] == []


@pytest.mark.asyncio
async def test_failed_call_setup_still_reaches_the_owner(monkeypatch, ring):
    _score(monkeypatch, 80)
    ring["prepared"] = False

    twiml = await _call(monkeypatch, None)

    assert "<Number" in twiml
    assert ring["rings"] == []
