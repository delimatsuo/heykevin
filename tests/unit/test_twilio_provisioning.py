"""Twilio number provisioning safety checks."""

import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

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

    async def fake_get_contractor_by_apple_user_id(_apple_user_id):
        return None

    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce_apple_identity)
    monkeypatch.setattr(contractors_api, "create_contractor", fake_create_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    monkeypatch.setattr(
        contractors_db, "get_contractor_by_apple_user_id", fake_get_contractor_by_apple_user_id
    )

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

    assert response == {
        "status": "ok",
        "phone_number": "+16505551212",
        "country_code": "US",
        "service_binding": {
            "country_code": "US",
            "provider": None,
            "number_type": None,
            "capabilities": None,
        },
    }
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
        "country_code": "US",
        "service_binding": {
            "country_code": "US",
            "provider": None,
            "number_type": None,
            "capabilities": None,
        },
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
    assert "service_request_mutations_enabled" in contractors_db.PROTECTED_FIELDS
    assert "customer_memory_capture_enabled" in contractors_db.PROTECTED_FIELDS
    assert "customer_memory_personalization_enabled" in contractors_db.PROTECTED_FIELDS


@pytest.mark.parametrize("country,owner_phone,provisioned_number", [
    ("US", "+14155552671", "+16505551212"),
    ("CA", "+14165551234", "+16045551212"),
])
@pytest.mark.asyncio
async def test_provision_number_endpoint_returns_the_country_it_resolved_for_valid_markets(
    monkeypatch, country, owner_phone, provisioned_number
):
    """The client keys its call-forwarding codes on the account country, and
    provisioning is where that country is finally resolved from the phone — so
    the response must carry it with service_binding."""

    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "twilio_number": "",
            "country_code": "",
            "owner_phone": owner_phone,
        }

    async def fake_update_contractor(contractor_id, updates):
        return True

    async def fake_provision_twilio_number(contractor_id, country_code="US"):
        return provisioned_number

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    monkeypatch.setattr(contractors_db, "provision_twilio_number", fake_provision_twilio_number)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("contractor-1", request)

    assert response["status"] == "ok"
    assert response["country_code"] == country
    assert response["phone_number"] == provisioned_number
    assert response["service_binding"] == {
        "country_code": country,
        "provider": None,
        "number_type": None,
        "capabilities": None,
    }


@pytest.mark.asyncio
async def test_provision_number_endpoint_rejects_new_gb_provision_with_409(monkeypatch):
    """New GB provisioning attempts must fail with HTTP 409 country_not_available and call no Twilio."""
    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "twilio_number": "",
            "country_code": "GB",
            "owner_phone": "",
        }

    def fail_twilio(*args, **kwargs):
        pytest.fail("Twilio provisioning must not be called for unavailable GB market")

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "provision_twilio_number", fail_twilio)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_provision_number("contractor-1", request)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "country_not_available"
    assert exc_info.value.detail["country_code"] == "GB"


@pytest.mark.asyncio
async def test_provision_number_endpoint_reports_country_for_an_existing_number(monkeypatch):
    """Unknown/invalid assigned number maps country to None with explicit all-unknown service_binding."""
    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "twilio_number": "+441onexisting",
            "country_code": "gb",
            "owner_phone": "+447700900123",
        }

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("contractor-1", request)

    assert response == {
        "status": "ok",
        "phone_number": "+441onexisting",
        "existing": True,
        "country_code": None,
        "service_binding": {
            "country_code": None,
            "provider": None,
            "number_type": None,
            "capabilities": None,
        },
    }


@pytest.mark.parametrize("country,assigned_number", [
    ("GB", "+442079460958"),
    ("BR", "+5511987654321"),
])
@pytest.mark.asyncio
async def test_provision_number_endpoint_genuine_existing_international_number_not_blocked(
    monkeypatch, country, assigned_number
):
    """Existing genuine GB/BR numbers are not blocked and resolve their authoritative country."""
    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "twilio_number": assigned_number,
            "country_code": country,
            "provisioned_country_code": country,
            "number_provider": "twilio",
            "number_type": "local",
            "number_capabilities": {"voice": True, "SMS": True},
        }

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("contractor-1", request)

    assert response == {
        "status": "ok",
        "phone_number": assigned_number,
        "existing": True,
        "country_code": country,
        "service_binding": {
            "country_code": country,
            "provider": "twilio",
            "number_type": "local",
            "capabilities": {"voice": True, "SMS": True},
        },
    }


@pytest.mark.parametrize("stored", ["XX", 55, None, "  "])
@pytest.mark.asyncio
async def test_provision_number_endpoint_existing_number_reports_us_for_unusable_stored_country(
    monkeypatch, stored
):
    """Mirror GET /api/settings: an unsupported, non-string, or blank stored
    country on a valid US assigned number derives 'US' from the number."""

    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "twilio_number": "+16505551212",
            "country_code": stored,
            "owner_phone": "+16505551212",
        }

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("contractor-1", request)

    assert response["status"] == "ok"
    assert response["country_code"] == "US"
    assert response["service_binding"] == {
        "country_code": "US",
        "provider": None,
        "number_type": None,
        "capabilities": None,
    }


@pytest.mark.asyncio
async def test_provision_number_endpoint_unmatched_fresh_profile_does_not_reuse_stale_metadata(
    monkeypatch,
):
    """If fresh profile read has an unmatched assigned number and stale metadata, fallback must not reuse stale fields."""
    call_count = 0

    async def fake_get_contractor(contractor_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "contractor_id": contractor_id,
                "twilio_number": "",
                "country_code": "US",
                "owner_phone": "+14155552671",
            }
        return {
            "contractor_id": contractor_id,
            "twilio_number": "+5511987654321",
            "country_code": "BR",
            "provisioned_country_code": "BR",
            "number_provider": "foreign",
            "number_type": "mobile",
            "number_capabilities": {"voice": True, "SMS": True},
        }

    async def fake_update_contractor(contractor_id, updates):
        return True

    async def fake_provision_twilio_number(contractor_id, country_code="US"):
        return "+14155552671"

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    monkeypatch.setattr(contractors_db, "provision_twilio_number", fake_provision_twilio_number)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("contractor-1", request)

    assert response == {
        "status": "ok",
        "phone_number": "+14155552671",
        "country_code": "US",
        "service_binding": {
            "country_code": "US",
            "provider": None,
            "number_type": None,
            "capabilities": None,
        },
    }


@pytest.mark.asyncio
async def test_provision_number_endpoint_missing_fresh_profile_falls_back_to_clean_binding(
    monkeypatch,
):
    """If fresh profile read returns None after provisioning, fallback returns known country with None fields."""
    call_count = 0

    async def fake_get_contractor(contractor_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "contractor_id": contractor_id,
                "twilio_number": "",
                "country_code": "US",
                "owner_phone": "+14155552671",
            }
        return None

    async def fake_update_contractor(contractor_id, updates):
        return True

    async def fake_provision_twilio_number(contractor_id, country_code="US"):
        return "+14155552671"

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    monkeypatch.setattr(contractors_db, "provision_twilio_number", fake_provision_twilio_number)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("contractor-1", request)

    assert response == {
        "status": "ok",
        "phone_number": "+14155552671",
        "country_code": "US",
        "service_binding": {
            "country_code": "US",
            "provider": None,
            "number_type": None,
            "capabilities": None,
        },
    }


@pytest.mark.asyncio
async def test_provision_number_endpoint_matching_fresh_profile_retains_recorded_capabilities(
    monkeypatch,
):
    """When fresh profile matches the newly provisioned number, resolve_service_binding returns recorded capabilities."""
    call_count = 0

    async def fake_get_contractor(contractor_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "contractor_id": contractor_id,
                "twilio_number": "",
                "country_code": "US",
                "owner_phone": "+14155552671",
            }
        return {
            "contractor_id": contractor_id,
            "twilio_number": "+14155552671",
            "country_code": "US",
            "provisioned_country_code": "US",
            "number_provider": "twilio",
            "number_type": "local",
            "number_capabilities": {"voice": True, "SMS": True},
        }

    async def fake_update_contractor(contractor_id, updates):
        return True

    async def fake_provision_twilio_number(contractor_id, country_code="US"):
        return "+14155552671"

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    monkeypatch.setattr(contractors_db, "provision_twilio_number", fake_provision_twilio_number)
    request = SimpleNamespace(state=SimpleNamespace(is_admin=True))

    response = await contractors_api.api_provision_number("contractor-1", request)

    assert response == {
        "status": "ok",
        "phone_number": "+14155552671",
        "country_code": "US",
        "service_binding": {
            "country_code": "US",
            "provider": "twilio",
            "number_type": "local",
            "capabilities": {"voice": True, "SMS": True},
        },
    }
