"""Contractor mode persistence and subscription guardrails."""

import os
from types import SimpleNamespace

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import pytest
from fastapi import HTTPException

from app.api import contractors as contractors_api
from app.services.entitlements import effective_mode, has_business_entitlement


def _admin_request():
    return SimpleNamespace(state=SimpleNamespace(is_admin=True))


@pytest.mark.asyncio
async def test_create_contractor_persists_selected_mode(monkeypatch):
    captured = {}

    async def fake_create_contractor(data):
        captured.update(data)
        return "contractor-1"

    async def fake_update_contractor(contractor_id, updates):
        return True

    monkeypatch.setattr(contractors_api, "create_contractor", fake_create_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)

    response = await contractors_api.api_create_contractor(
        contractors_api.ContractorCreate(
            business_name="Deli Plumbing",
            owner_name="Deli",
            service_type="plumbing",
            mode="personal",
        ),
        _admin_request(),
    )

    assert response["status"] == "ok"
    assert response["contractor_id"] == "contractor-1"
    assert captured["mode"] == "personal"


@pytest.mark.asyncio
async def test_create_contractor_does_not_activate_business_without_entitlement(monkeypatch):
    captured = {}

    async def fake_create_contractor(data):
        captured.update(data)
        return "contractor-1"

    async def fake_update_contractor(contractor_id, updates):
        return True

    monkeypatch.setattr(contractors_api, "create_contractor", fake_create_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)

    response = await contractors_api.api_create_contractor(
        contractors_api.ContractorCreate(
            business_name="Deli Plumbing",
            owner_name="Deli",
            service_type="plumbing",
            mode="business",
        ),
        _admin_request(),
    )

    assert response["status"] == "ok"
    assert captured["mode"] == "personal"


@pytest.mark.asyncio
async def test_get_contractor_backfills_missing_subscription_uuid(monkeypatch):
    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "owner_name": "Deli",
            "subscription_status": "trial",
            "subscription_tier": "none",
        }

    async def fake_ensure_subscription_uuid(contractor_id, contractor):
        assert contractor_id == "contractor-1"
        assert "subscription_uuid" not in contractor
        return "11111111-1111-4111-8111-111111111111"

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "ensure_subscription_uuid", fake_ensure_subscription_uuid)

    response = await contractors_api.api_get_contractor("contractor-1", _admin_request())

    assert response["subscription_uuid"] == "11111111-1111-4111-8111-111111111111"


@pytest.mark.asyncio
async def test_trial_user_without_business_tier_cannot_switch_to_business_mode(monkeypatch):
    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "subscription_status": "trial",
            "subscription_tier": "none",
        }

    async def fail_update_contractor(*args, **kwargs):
        raise AssertionError("non-business trial users must not be switched to business")

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fail_update_contractor)

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_update_contractor(
            "contractor-1",
            contractors_api.ContractorUpdate(mode="business"),
            _admin_request(),
        )

    assert exc_info.value.status_code == 403


def test_business_runtime_falls_back_to_personal_without_business_tier():
    contractor = {
        "mode": "business",
        "subscription_status": "trial",
        "subscription_tier": "none",
    }

    assert not has_business_entitlement(contractor)
    assert effective_mode(contractor) == "personal"


def test_business_runtime_allowed_with_business_tier():
    contractor = {
        "mode": "business",
        "subscription_status": "active",
        "subscription_tier": "business",
    }

    assert has_business_entitlement(contractor)
    assert effective_mode(contractor) == "business"


@pytest.mark.asyncio
async def test_trial_user_with_business_tier_can_switch_to_business_mode(monkeypatch):
    updates_seen = {}

    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "subscription_status": "trial",
            "subscription_tier": "business",
        }

    async def fake_update_contractor(contractor_id, updates):
        updates_seen["contractor_id"] = contractor_id
        updates_seen["updates"] = updates
        return True

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)

    response = await contractors_api.api_update_contractor(
        "contractor-1",
        contractors_api.ContractorUpdate(mode="business"),
        _admin_request(),
    )

    assert response == {"status": "ok"}
    assert updates_seen == {
        "contractor_id": "contractor-1",
        "updates": {"mode": "business"},
    }


@pytest.mark.asyncio
async def test_active_personal_subscriber_cannot_switch_to_business_mode(monkeypatch):
    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "subscription_status": "active",
            "subscription_tier": "personal",
        }

    async def fail_update_contractor(*args, **kwargs):
        raise AssertionError("personal subscribers must not be switched to business")

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fail_update_contractor)

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_update_contractor(
            "contractor-1",
            contractors_api.ContractorUpdate(mode="business"),
            _admin_request(),
        )

    assert exc_info.value.status_code == 403
