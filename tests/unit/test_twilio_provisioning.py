"""Twilio number provisioning safety checks."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.api import contractors as contractors_api
from app.db import contractors as contractors_db


@pytest.mark.asyncio
async def test_create_contractor_defaults_blank_country_to_us(monkeypatch):
    created = {}

    async def fake_enforce_apple_identity(*_args, **_kwargs):
        return None

    async def fake_create_contractor(data):
        created.update(data)
        return "contractor-1"

    async def fake_update_contractor(*_args, **_kwargs):
        return True

    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce_apple_identity)
    monkeypatch.setattr(contractors_api, "create_contractor", fake_create_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)

    response = await contractors_api.api_create_contractor(
        contractors_api.ContractorCreate(
            owner_name="New User",
            business_name="New User's phone",
            mode="personal",
            owner_phone="",
            apple_user_id="apple-user-1",
            apple_identity_token="identity-token",
        ),
        SimpleNamespace(state=SimpleNamespace(is_admin=False)),
    )

    assert response["status"] == "ok"
    assert created["country_code"] == "US"


@pytest.mark.asyncio
async def test_provision_number_endpoint_defaults_blank_country_to_us(monkeypatch):
    seen = {}

    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "twilio_number": "",
            "country_code": "",
            "owner_phone": "",
        }

    async def fake_update_contractor(contractor_id, updates):
        seen["updated"] = {"contractor_id": contractor_id, "updates": updates}
        return True

    async def fake_provision_twilio_number(contractor_id, country_code="US"):
        seen["provisioned"] = {"contractor_id": contractor_id, "country_code": country_code}
        return "+16505551212"

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    monkeypatch.setattr(contractors_db, "provision_twilio_number", fake_provision_twilio_number)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("contractor-1", request)

    assert response == {"status": "ok", "phone_number": "+16505551212"}
    assert seen["provisioned"] == {"contractor_id": "contractor-1", "country_code": "US"}
    assert seen["updated"] == {
        "contractor_id": "contractor-1",
        "updates": {"country_code": "US"},
    }


@pytest.mark.asyncio
async def test_provision_twilio_number_reuses_existing_number(monkeypatch):
    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "twilio_number": "+16505551212",
        }

    async def fail_update(*args, **kwargs):
        raise AssertionError("existing-number provisioning must not update Firestore")

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fail_update)

    number = await contractors_db.provision_twilio_number("contractor-1")

    assert number == "+16505551212"


@pytest.mark.asyncio
async def test_provision_number_endpoint_reuses_existing_number(monkeypatch):
    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "twilio_number": "+16505551212",
            "country_code": "US",
        }

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("contractor-1", request)

    assert response == {
        "status": "ok",
        "phone_number": "+16505551212",
        "existing": True,
    }


@pytest.mark.asyncio
async def test_contractor_patch_cannot_change_twilio_number(monkeypatch):
    updates_seen = {}

    async def fake_update_contractor(contractor_id, updates):
        updates_seen["contractor_id"] = contractor_id
        updates_seen["updates"] = updates
        return True

    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_update_contractor(
        "contractor-1",
        contractors_api.ContractorUpdate(
            mode="personal",
            twilio_number="+16505559999",
        ),
        request,
    )

    assert response == {"status": "ok"}
    assert updates_seen == {
        "contractor_id": "contractor-1",
        "updates": {"mode": "personal"},
    }


def test_subscription_uuid_is_server_protected():
    assert "subscription_uuid" in contractors_db.PROTECTED_FIELDS


@pytest.mark.asyncio
async def test_contractor_patch_cannot_enable_jobber_lead_capture(monkeypatch):
    updates_seen = {}

    async def fake_update_contractor(contractor_id, updates):
        updates_seen["contractor_id"] = contractor_id
        updates_seen["updates"] = updates
        return True

    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_update_contractor(
        "contractor-1",
        contractors_api.ContractorUpdate(
            mode="personal",
            jobber_lead_capture_enabled=True,
        ),
        request,
    )

    assert response == {"status": "ok"}
    assert updates_seen == {
        "contractor_id": "contractor-1",
        "updates": {"mode": "personal"},
    }


def test_jobber_lead_capture_flag_is_server_protected():
    assert "jobber_lead_capture_enabled" in contractors_db.PROTECTED_FIELDS
