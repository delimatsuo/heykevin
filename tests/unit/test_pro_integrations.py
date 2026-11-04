"""Jobber and Google Calendar need Business Pro once the cutoff is set.

Business accounts connected before the cutoff keep their connection. The switch
stays off until the app that explains the limit is out.
"""

from __future__ import annotations

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.api import integrations  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import entitlements  # noqa: E402

CUTOFF = 2_000_000_000.0
BEFORE = CUTOFF - 1000.0
AFTER = CUTOFF + 1000.0


def _account(tier="business", status="active", **fields) -> dict:
    return {"subscription_tier": tier, "subscription_status": status, **fields}


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(settings, "pro_integrations_cutoff", CUTOFF)


def test_switch_off_changes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "pro_integrations_cutoff", 0.0)
    assert entitlements.can_use_integration(_account("personal"), "jobber") is True


@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
def test_business_pro_can_use_both(gate_on, provider):
    assert entitlements.can_use_integration(_account("businessPro"), provider) is True


@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
def test_new_business_connection_needs_pro(gate_on, provider):
    assert entitlements.can_use_integration(_account(), provider) is False


def test_business_connected_before_cutoff_keeps_that_connection(gate_on):
    account = _account(jobber_connected_at=BEFORE)
    assert entitlements.can_use_integration(account, "jobber") is True
    assert entitlements.can_use_integration(account, "google_calendar") is False


def test_reauthorizing_a_kept_connection_keeps_it(gate_on):
    account = _account(jobber_connected_at=AFTER, jobber_first_connected_at=BEFORE)
    assert entitlements.can_use_integration(account, "jobber") is True


def test_downgrade_from_pro_after_cutoff_loses_access(gate_on):
    account = _account(jobber_connected_at=AFTER, jobber_first_connected_at=AFTER)
    assert entitlements.can_use_integration(account, "jobber") is False


def test_kept_connection_needs_an_active_business_plan(gate_on):
    expired = _account(status="expired", jobber_connected_at=BEFORE)
    personal = _account("personal", jobber_connected_at=BEFORE)
    assert entitlements.can_use_integration(expired, "jobber") is False
    assert entitlements.can_use_integration(personal, "jobber") is False


def test_config_without_subscription_fields_stays_allowed(gate_on):
    assert entitlements.can_use_integration({"jobber_connected": True}, "jobber") is True


def test_token_check_respects_the_plan(gate_on, monkeypatch):
    monkeypatch.setattr("app.services.integration_tokens.has_usable_token", lambda *_a, **_k: True)
    assert entitlements.has_entitled_token(_account(), "jobber") is False
    assert entitlements.has_entitled_token(_account("businessPro"), "jobber") is True


def test_app_sees_availability(gate_on):
    flags = entitlements.with_entitlement_flags(_account(jobber_connected_at=BEFORE))
    assert flags["jobber_available"] is True
    assert flags["google_calendar_available"] is False


@pytest.mark.asyncio
async def test_connect_is_refused_without_the_plan(gate_on, monkeypatch):
    async def get_contractor(_id):
        return _account()

    monkeypatch.setattr("app.db.contractors.get_contractor", get_contractor)
    with pytest.raises(HTTPException) as refused:
        await integrations._require_integration_plan("c-1", "jobber")
    assert refused.value.status_code == 403
    assert refused.value.detail == "business_pro_required"


@pytest.mark.asyncio
async def test_connect_is_allowed_with_pro(gate_on, monkeypatch):
    async def get_contractor(_id):
        return _account("businessPro")

    monkeypatch.setattr("app.db.contractors.get_contractor", get_contractor)
    await integrations._require_integration_plan("c-1", "google_calendar")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_reauthorization_keeps_the_first_connection_date(monkeypatch, provider):
    from app.services.integration_token_mutations import connect_provider_cas
    from tests.unit.test_integration_token_envelope import (
        _FakeDocRef,
        _FakeFirestore,
        _patch_firestore,
        _setup_keyring,
    )
    from tests.unit.test_jobber_disconnect_integration import BoundedLegacyTestTransaction

    class Firestore(_FakeFirestore):
        def jobber_transaction_factory(self, *, deadline, read_only):
            return BoundedLegacyTestTransaction(self, deadline=deadline, read_only=read_only)

    _setup_keyring(monkeypatch)
    doc_ref = _FakeDocRef({
        "contractor_id": "c-reauth",
        "active": True,
        f"{provider}_lifecycle_epoch": 0,
        f"{provider}_connected": False,
        f"{provider}_generation": 0,
        f"{provider}_connected_at": BEFORE,
    })
    db = Firestore({"contractors": {"c-reauth": doc_ref}})
    _patch_firestore(monkeypatch, db)

    extra = {"jobber_account_id": "jobber-acc-1"} if provider == "jobber" else {}
    await connect_provider_cas(
        contractor_id="c-reauth",
        provider=provider,
        access_token="acc-1",
        refresh_token="ref-1",
        db=db,
        **extra,
    )

    assert doc_ref.data[f"{provider}_first_connected_at"] == BEFORE
    assert doc_ref.data[f"{provider}_connected_at"] != BEFORE
