"""Irreversible account operations: deletion and Twilio number release.

The Phase 0 side-effect matrix classifies app/api/contractors.py as
"irreversible" and requires confirmation, idempotency, partial-failure
handling, and audit for deletion and number release. These tests pin that
contract:

- The ACCOUNT_DELETE / TWILIO_NUMBER_RELEASE gates allow an authenticated
  owner's own request without any per-contractor flag (account deletion is an
  App Store requirement — it must work for every user), while still refusing
  unconfirmed or key-less invocations.
- The endpoints report failure as HTTP 5xx, never 200-with-an-error-body.
  A 200 on failure is what strands users: iOS clears local state while the
  account stays active and the number keeps billing.
- release_twilio_number never *silently* succeeds: a number missing from the
  Twilio account is recorded as an anomaly, and a Twilio failure propagates
  without touching the profile so a retry can heal.
"""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import contractors as contractors_api
from app.db import contractors as contractors_db
from app.services.gated_actions import (
    ActionKey,
    GateContext,
    GateDecision,
    GateReason,
    check_gated_action,
)


def _owner_request(contractor_id: str = "c1"):
    return SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id=contractor_id))


def _contractor(**overrides):
    doc = {
        "contractor_id": "c1",
        "twilio_number": "+15559999999",
        "active": True,
        # Deliberately no gated_actions / automation_approvals: 0 of 113
        # production contractors have them, and deletion must still work.
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# Gate policy layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action", [ActionKey.ACCOUNT_DELETE, ActionKey.TWILIO_NUMBER_RELEASE]
)
def test_owner_confirmed_request_is_allowed_without_contractor_flag(action):
    """An authenticated owner's own tap must pass with no gated_actions set."""
    decision = check_gated_action(
        _contractor(),
        action,
        GateContext(
            source="ios",
            actor="owner",
            owner_confirmed=True,
            idempotency_key="c1:test",
            environment="production",
        ),
    )
    assert decision.allowed, decision.reason


@pytest.mark.parametrize(
    "action", [ActionKey.ACCOUNT_DELETE, ActionKey.TWILIO_NUMBER_RELEASE]
)
def test_unconfirmed_request_is_refused_for_owner_confirmation(action):
    """Without owner confirmation the gate refuses — for that reason, not a flag."""
    decision = check_gated_action(
        _contractor(),
        action,
        GateContext(
            source="ios",
            actor="owner",
            owner_confirmed=False,
            idempotency_key="c1:test",
            environment="production",
        ),
    )
    assert not decision.allowed
    assert decision.reason == GateReason.OWNER_CONFIRMATION_REQUIRED


@pytest.mark.parametrize(
    "action", [ActionKey.ACCOUNT_DELETE, ActionKey.TWILIO_NUMBER_RELEASE]
)
def test_missing_idempotency_key_is_refused(action):
    decision = check_gated_action(
        _contractor(),
        action,
        GateContext(
            source="ios",
            actor="owner",
            owner_confirmed=True,
            idempotency_key="",
            environment="production",
        ),
    )
    assert not decision.allowed
    assert decision.reason == GateReason.IDEMPOTENCY_REQUIRED


# ---------------------------------------------------------------------------
# Endpoint layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_contractor_success_returns_ok(monkeypatch):
    calls = []

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_deactivate(cid):
        calls.append(cid)
        return True

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)

    result = await contractors_api.api_delete_contractor("c1", _owner_request())

    assert result == {"status": "ok"}
    assert calls == ["c1"]


@pytest.mark.asyncio
async def test_delete_contractor_failure_returns_http_500(monkeypatch):
    """A failed deletion must be a 5xx, never 200 {"status": "error"}."""

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_deactivate(cid):
        raise RuntimeError("twilio down")

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor("c1", _owner_request())
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_delete_contractor_refused_by_gate_makes_no_changes(monkeypatch):
    """If the gate refuses, the endpoint returns 403 and touches nothing."""
    calls = []

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_deactivate(cid):
        calls.append(cid)
        return True

    def refusing_gate(contractor, action, context):
        return GateDecision(
            allowed=False,
            action=action,
            reason=GateReason.OWNER_CONFIRMATION_REQUIRED,
            message="Owner confirmation is required for this action.",
        )

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)
    monkeypatch.setattr(contractors_api, "check_gated_action", refusing_gate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor("c1", _owner_request())
    assert exc.value.status_code == 403
    assert calls == []


@pytest.mark.asyncio
async def test_release_number_failure_returns_http_500(monkeypatch):
    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_release(cid):
        raise RuntimeError("twilio down")

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "release_twilio_number", fake_release)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_release_number("c1", _owner_request())
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_release_number_with_no_number_is_idempotent_ok(monkeypatch):
    """No number on file means the desired end state already holds: 200, not error.

    This is what lets a retry after a partial failure heal instead of erroring
    forever.
    """

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid, twilio_number="")

    async def fake_release(cid):
        return False

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "release_twilio_number", fake_release)

    result = await contractors_api.api_release_number("c1", _owner_request())

    assert result["status"] == "ok"
    assert result["released"] is False


@pytest.mark.asyncio
async def test_cross_tenant_delete_is_refused_before_any_work(monkeypatch):
    calls = []

    async def fake_deactivate(cid):
        calls.append(cid)
        return True

    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor(
            "victim", _owner_request(contractor_id="attacker")
        )
    assert exc.value.status_code == 403
    assert calls == []


# ---------------------------------------------------------------------------
# DB layer: release_twilio_number / deactivate_contractor
# ---------------------------------------------------------------------------


class _FakeNumber:
    def __init__(self, log):
        self._log = log

    def delete(self):
        self._log.append("deleted")


class _FakeTwilioClient:
    """Stands in for twilio.rest.Client; behavior injected per-test."""

    lookup_result = None  # set per test
    log = None

    def __init__(self, *args, **kwargs):
        pass

    @property
    def incoming_phone_numbers(self):
        outer = type(self)

        class _Numbers:
            def list(self, **kwargs):
                if isinstance(outer.lookup_result, Exception):
                    raise outer.lookup_result
                return outer.lookup_result

        return _Numbers()


@pytest.fixture
def fake_twilio(monkeypatch):
    _FakeTwilioClient.log = []
    _FakeTwilioClient.lookup_result = []
    monkeypatch.setattr("twilio.rest.Client", _FakeTwilioClient)
    return _FakeTwilioClient


@pytest.mark.asyncio
async def test_release_found_number_clears_profile_and_stamps_release(
    monkeypatch, fake_twilio
):
    updates = []
    fake_twilio.lookup_result = [_FakeNumber(fake_twilio.log)]

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    released = await contractors_db.release_twilio_number("c1")

    assert released is True
    assert fake_twilio.log == ["deleted"]
    assert len(updates) == 1
    cid, fields = updates[0]
    assert cid == "c1"
    assert fields["twilio_number"] == ""
    assert fields.get("number_released_at"), "release must be stamped for audit"


@pytest.mark.asyncio
async def test_release_missing_number_records_anomaly(monkeypatch, fake_twilio):
    """A number we believe we own but Twilio doesn't list is an anomaly, not a
    silent success — it may be a format mismatch on a number that still bills."""
    updates = []
    fake_twilio.lookup_result = []  # Twilio says: no such number

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    released = await contractors_db.release_twilio_number("c1")

    assert released is True
    assert len(updates) == 1
    _cid, fields = updates[0]
    assert fields["twilio_number"] == ""
    assert fields.get("number_release_anomaly"), (
        "a Twilio-side miss must be recorded on the document for reconciliation"
    )


@pytest.mark.asyncio
async def test_release_twilio_failure_propagates_without_touching_profile(
    monkeypatch, fake_twilio
):
    updates = []
    fake_twilio.lookup_result = RuntimeError("twilio 5xx")

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    with pytest.raises(RuntimeError):
        await contractors_db.release_twilio_number("c1")
    assert updates == [], "profile must stay intact so a retry can heal"


@pytest.mark.asyncio
async def test_deactivate_stamps_deactivated_at(monkeypatch):
    updates = []

    async def fake_release(cid):
        return True

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "release_twilio_number", fake_release)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    ok = await contractors_db.deactivate_contractor("c1")

    assert ok is True
    assert len(updates) == 1
    _cid, fields = updates[0]
    assert fields["active"] is False
    assert fields.get("deactivated_at"), "deactivation must be stamped for audit"
