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
async def test_release_number_refused_by_gate_makes_no_changes(monkeypatch):
    """If the gate refuses, the endpoint returns 403 and releases nothing."""
    calls = []

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_release(cid):
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
    monkeypatch.setattr(contractors_api, "release_twilio_number", fake_release)
    monkeypatch.setattr(contractors_api, "check_gated_action", refusing_gate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_release_number("c1", _owner_request())
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


# ---------------------------------------------------------------------------
# Review-round fixes (PR #192 findings)
# ---------------------------------------------------------------------------

import hashlib
import importlib.util
from pathlib import Path

from twilio.base.exceptions import TwilioRestException

from app.db.contractors import PROTECTED_FIELDS
from app.middleware import auth as auth_middleware
from app.services.gated_actions import GATE_POLICIES, GatePolicy


class _FakeNumberGone:
    def delete(self):
        raise TwilioRestException(404, "/IncomingPhoneNumbers/PNxxx", "not found", 20404, "DELETE")


@pytest.mark.asyncio
async def test_release_tolerates_number_already_gone_at_twilio(monkeypatch, fake_twilio):
    """A 20404 on delete() means another request already released it: success,
    no anomaly — the concurrent-delete race must not surface as a 500."""
    updates = []
    fake_twilio.lookup_result = [_FakeNumberGone()]

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
    assert "number_release_anomaly" not in fields


@pytest.mark.asyncio
async def test_release_anomaly_records_which_number(monkeypatch, fake_twilio):
    """The anomaly record must carry the number itself — the doc clears it and
    the log redacts it, so without this reconciliation has no key."""
    updates = []
    fake_twilio.lookup_result = []

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    await contractors_db.release_twilio_number("c1")

    _cid, fields = updates[0]
    anomaly = fields["number_release_anomaly"]
    assert anomaly["number"] == "+15559999999"
    assert anomaly["at"]


@pytest.mark.asyncio
async def test_delete_missing_contractor_returns_404(monkeypatch):
    """A doc that does not exist is 404 'already gone', never a 403 dead-end."""

    async def fake_get_contractor(cid):
        return None

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)

    admin_request = SimpleNamespace(state=SimpleNamespace(is_admin=True, contractor_id=""))
    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor("gone", admin_request)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_gate_context_reflects_the_real_actor(monkeypatch):
    """Admin-token deletions must not be audited as owner-confirmed iOS taps."""
    contexts = []
    real_check = contractors_api.check_gated_action

    def spying_check(contractor, action, context):
        contexts.append(context)
        return real_check(contractor, action, context)

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_deactivate(cid):
        return True

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)
    monkeypatch.setattr(contractors_api, "check_gated_action", spying_check)

    await contractors_api.api_delete_contractor("c1", _owner_request())
    admin_request = SimpleNamespace(state=SimpleNamespace(is_admin=True, contractor_id=""))
    await contractors_api.api_delete_contractor("c1", admin_request)

    assert contexts[0].actor == "owner"
    assert contexts[0].source == "ios"
    assert contexts[1].actor == "admin"
    assert contexts[1].source == "api"


@pytest.mark.asyncio
async def test_endpoint_preserves_http_exceptions_from_callees(monkeypatch):
    """A deliberate 4xx raised inside the deletion path must not be rewrapped
    as a generic 500."""

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_deactivate(cid):
        raise HTTPException(status_code=409, detail="forwarding still live")

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor("c1", _owner_request())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_deactivate_invalidates_token_cache(monkeypatch):
    """A deleted account's token must stop authenticating on this instance."""
    token_hash = hashlib.sha256(b"kv_ct_c1_secret").hexdigest()
    auth_middleware._token_cache[token_hash] = "c1"
    auth_middleware._token_cache["other-hash"] = "c2"

    async def fake_release(cid):
        return True

    async def fake_update(cid, fields):
        return True

    monkeypatch.setattr(contractors_db, "release_twilio_number", fake_release)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    try:
        await contractors_db.deactivate_contractor("c1")
        assert token_hash not in auth_middleware._token_cache
        assert auth_middleware._token_cache.get("other-hash") == "c2"
    finally:
        auth_middleware._token_cache.pop("other-hash", None)
        auth_middleware._token_cache.pop(token_hash, None)


@pytest.mark.asyncio
async def test_gate_refusal_detail_carries_the_reason(monkeypatch):
    """Denial payloads must match the estimates endpoints' shape so clients
    can distinguish refusal reasons."""

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    def refusing_gate(contractor, action, context):
        return GateDecision(
            allowed=False,
            action=action,
            reason=GateReason.OWNER_CONFIRMATION_REQUIRED,
            message="Owner confirmation is required for this action.",
        )

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "check_gated_action", refusing_gate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor("c1", _owner_request())
    assert exc.value.detail["reason"] == GateReason.OWNER_CONFIRMATION_REQUIRED.value


def test_new_lifecycle_stamps_are_protected_fields():
    assert {"number_released_at", "number_release_anomaly", "deactivated_at"} <= set(PROTECTED_FIELDS)


@pytest.mark.parametrize(
    "action", [ActionKey.ACCOUNT_DELETE, ActionKey.TWILIO_NUMBER_RELEASE]
)
def test_gate_policy_shape_is_pinned(action):
    assert GATE_POLICIES[action] == GatePolicy(
        requires_flag=False,
        requires_owner_confirmation=True,
        requires_idempotency=True,
    )


def test_kill_switch_scripts_do_not_offer_flagless_actions():
    """gated_actions flags are never consulted for requires_flag=False actions;
    offering them in the operator writer or counting them in the audit script
    fabricates a kill switch that does not exist."""
    root = Path(__file__).resolve().parents[2]

    spec = importlib.util.spec_from_file_location(
        "set_gated_action_killswitch", root / "scripts" / "set_gated_action.py"
    )
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    assert "account_delete" not in writer.KNOWN_ACTIONS
    assert "twilio_number_release" not in writer.KNOWN_ACTIONS

    spec = importlib.util.spec_from_file_location(
        "phase0_account_audit_killswitch", root / "scripts" / "phase0_account_audit.py"
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    flags = audit.KNOWN_ACTION_KEYS
    assert "account_delete" not in flags
    assert "twilio_number_release" not in flags
