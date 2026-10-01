"""Unit tests for country admission, phone-country binding, and telephony service binding."""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException

from app.api import contractors as contractors_api
from app.api import settings as settings_api
from app.db import contractors as contractors_db
from app.services.country_policy import (
    resolve_service_binding,
    validate_phone_and_region,
    is_country_available,
    country_not_available_detail,
    get_markets_payload,
    get_country_status,
    InvalidPhoneError,
    CountryPhoneMismatchError,
    PINNED_MARKET_ORDER,
    QUALIFICATION_ORDER,
)


@pytest.fixture(autouse=True)
def _guard_no_real_firestore(monkeypatch):
    """Fail-closed guard ensuring no test inadvertently accesses real Firestore without an explicit mock."""
    def _fail_firestore(*args, **kwargs):
        pytest.fail("Unmocked call to get_firestore_client in test_country_admission")
    monkeypatch.setattr("app.db.firestore_client.get_firestore_client", _fail_firestore)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", _fail_firestore)
    monkeypatch.setattr("app.api.settings.get_firestore_client", _fail_firestore)


def _admin_request(contractor_id: str = "contractor-1"):
    return SimpleNamespace(state=SimpleNamespace(is_admin=True, contractor_id=contractor_id))


# ===========================================================================
# 1. Phone validation & region derivation
# ===========================================================================


def test_phone_validation_us_and_ca():
    canonical_us, region_us = validate_phone_and_region("(415) 555-1234", "US")
    assert canonical_us == "+14155551234"
    assert region_us == "US"

    canonical_ca, region_ca = validate_phone_and_region("+14165551234", "CA")
    assert canonical_ca == "+14165551234"
    assert region_ca == "CA"


def test_phone_validation_brazil_valid_and_invalid():
    # Valid 11-digit Brazilian mobile with DDD
    canonical_br, region_br = validate_phone_and_region("(11) 98765-4321", "BR")
    assert canonical_br == "+5511987654321"
    assert region_br == "BR"

    # Valid E.164 Brazil without explicit country
    canonical_e164, region_e164 = validate_phone_and_region("+5511987654321")
    assert canonical_e164 == "+5511987654321"
    assert region_e164 == "BR"

    # Missing DDD (9 digits only)
    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("98765-4321", "BR")

    # Fixed line in Brazil (10 digits starting with 3)
    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("(11) 3456-7890", "BR")

    # Mobile missing leading 9 (10 digits)
    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("(11) 8765-4321", "BR")

    # Carrier prefix (e.g. 015 11 98765-4321)
    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("015 11 98765-4321", "BR")

    # Trunk prefix (e.g. 0 11 98765-4321)
    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("0 11 98765-4321", "BR")

    # Invalid text
    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("not-a-number", "BR")


def test_phone_validation_character_level_rejection():
    # Vanity / letters
    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("1-800-FLOWERS", "US")

    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("800-CALL-KEVIN", "US")

    # Extensions
    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("415-555-1234 ext 101", "US")

    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("(415) 555-1234 x101", "US")

    # Multiple pluses
    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("++14155551234")

    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("+1+4155551234")

    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("415+5551234")

    # Unicode digits
    with pytest.raises(InvalidPhoneError):
        validate_phone_and_region("\uff14\uff11\uff15\uff15\uff15\uff15\uff11\uff12\uff13\uff14", "US")


def test_phone_validation_unknown_valid_e164_does_not_become_us():
    # Inmarsat / Global non-geographic code (+870) or unassigned global (+881)
    canonical, region = validate_phone_and_region("+870772001899")
    assert canonical == "+870772001899"
    assert region == "UNKNOWN"
    assert region != "US"


def test_phone_validation_country_mismatch_raises():
    # Brazilian number provided with explicit US country
    with pytest.raises(CountryPhoneMismatchError) as exc_info:
        validate_phone_and_region("+5511987654321", "US")
    assert exc_info.value.country_code == "US"
    assert exc_info.value.phone_region == "BR"


# ===========================================================================
# 2. Service binding resolution
# ===========================================================================


def test_resolve_service_binding_empty_when_no_number():
    assert resolve_service_binding(None) is None
    assert resolve_service_binding({}) is None
    assert resolve_service_binding({"twilio_number": ""}) is None
    assert resolve_service_binding({"twilio_number": "   "}) is None


def test_resolve_service_binding_with_provisioned_metadata():
    contractor = {
        "twilio_number": "+14155552671",
        "provisioned_country_code": "US",
        "number_provider": "twilio",
        "number_type": "local",
        "number_capabilities": {"voice": True, "sms": True},
    }
    binding = resolve_service_binding(contractor)
    assert binding == {
        "country_code": "US",
        "provider": "twilio",
        "number_type": "local",
        "capabilities": {"voice": True, "sms": True},
    }


def test_resolve_service_binding_legacy_number_derives_region():
    # Legacy UK number without provisioned metadata
    contractor = {
        "twilio_number": "+442071234567",
    }
    binding = resolve_service_binding(contractor)
    assert binding == {
        "country_code": "GB",
        "provider": None,
        "number_type": None,
        "capabilities": None,
    }


def test_resolve_service_binding_malformed_number_returns_null_country():
    contractor = {
        "twilio_number": "invalid-assigned-number",
    }
    binding = resolve_service_binding(contractor)
    assert binding == {
        "country_code": None,
        "provider": None,
        "number_type": None,
        "capabilities": None,
    }


# ===========================================================================
# 3. New Account Admission (POST /api/contractors)
# ===========================================================================


@pytest.mark.asyncio
async def test_api_create_contractor_allowed_for_us_and_ca(monkeypatch):
    created_docs = []

    async def fake_create(data):
        created_docs.append(data)
        return "c-new"

    async def fake_update(cid, updates):
        return True

    async def fake_enforce(request, apple_user_id, token):
        return None

    async def fake_by_apple(apple_user_id):
        return None

    async def fake_by_phone(phone, *, country_code="US"):
        return None

    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(contractors_api, "create_contractor", fake_create)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)
    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple
    )
    monkeypatch.setattr("app.db.contractors.get_contractor_by_owner_phone", fake_by_phone)

    # US creation
    body_us = contractors_api.ContractorCreate(
        business_name="US Co",
        owner_name="Alice",
        owner_phone="(415) 555-1234",
        country_code="US",
        apple_user_id="apple-us-1",
        apple_identity_token="tok",
    )
    res_us = await contractors_api.api_create_contractor(body_us, request=None)
    assert res_us["status"] == "ok"
    assert created_docs[-1]["country_code"] == "US"
    assert created_docs[-1]["owner_phone"] == "+14155551234"

    # CA creation
    body_ca = contractors_api.ContractorCreate(
        business_name="CA Co",
        owner_name="Bob",
        owner_phone="+14165551234",
        country_code="CA",
        apple_user_id="apple-ca-1",
        apple_identity_token="tok",
    )
    res_ca = await contractors_api.api_create_contractor(body_ca, request=None)
    assert res_ca["status"] == "ok"
    assert created_docs[-1]["country_code"] == "CA"
    assert created_docs[-1]["owner_phone"] == "+14165551234"


@pytest.mark.parametrize("unavailable_cc, expected_name", [
    ("BR", "Brazil"),
    ("GB", "United Kingdom"),
    ("DE", "Germany"),
    ("FR", "France"),
    ("IT", "Italy"),
    ("ES", "Spain"),
    ("PT", "Portugal"),
])
@pytest.mark.asyncio
async def test_api_create_contractor_rejected_for_unavailable_markets(
    unavailable_cc, expected_name, monkeypatch
):
    created_docs = []

    async def fake_create(data):
        created_docs.append(data)
        return "c-new"

    async def fake_enforce(request, apple_user_id, token):
        return None

    async def fake_by_apple(apple_user_id):
        return None

    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(contractors_api, "create_contractor", fake_create)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple)

    body = contractors_api.ContractorCreate(
        business_name="Test Co",
        owner_name="Owner",
        owner_phone="",
        country_code=unavailable_cc,
        apple_user_id="apple-new-user",
        apple_identity_token="tok",
    )

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(body, request=None)

    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert detail["code"] == "country_not_available"
    assert detail["country_code"] == unavailable_cc
    assert detail["message"] == f"Kevin is not yet available in {expected_name}."
    assert created_docs == [], "No contractor doc should be created on rejected admission"


@pytest.mark.asyncio
async def test_api_create_contractor_phone_country_mismatch_returns_400(monkeypatch):
    created_docs = []

    async def fake_create(data):
        created_docs.append(data)
        return "c-new"

    async def fake_enforce(request, apple_user_id, token):
        return None

    async def fake_by_apple(apple_user_id):
        return None

    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(contractors_api, "create_contractor", fake_create)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple)

    body = contractors_api.ContractorCreate(
        business_name="Mismatch Co",
        owner_name="Owner",
        owner_phone="+5511987654321",  # Brazil E.164
        country_code="US",              # Explicit US
        apple_user_id="apple-new-user",
        apple_identity_token="tok",
    )

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(body, request=None)

    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["code"] == "country_phone_mismatch"
    assert detail["country_code"] == "US"
    assert detail["phone_region"] == "BR"
    assert created_docs == [], "No contractor doc should be created on phone mismatch"


@pytest.mark.asyncio
async def test_api_create_contractor_unknown_valid_e164_rejected_with_409(monkeypatch):
    created_docs = []

    async def fake_create(data):
        created_docs.append(data)
        return "c-new"

    async def fake_enforce(request, apple_user_id, token):
        return None

    async def fake_by_apple(apple_user_id):
        return None

    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr(contractors_api, "create_contractor", fake_create)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple)

    body = contractors_api.ContractorCreate(
        business_name="Global Co",
        owner_name="Owner",
        owner_phone="+870772001899",
        apple_user_id="apple-new-user",
        apple_identity_token="tok",
    )

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(body, request=None)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "country_not_available"
    assert exc_info.value.detail["country_code"] == "UNKNOWN"
    assert created_docs == []


@pytest.mark.asyncio
async def test_api_create_contractor_authenticated_apple_restore_bypasses_new_admission_gate(monkeypatch):
    """Returning accounts in BR/GB must be restored without triggering country admission rejection."""
    async def fake_enforce(request, apple_user_id, token):
        return None

    async def fake_by_apple(apple_user_id):
        if apple_user_id == "apple-returning-br":
            return {
                "contractor_id": "c-returning-br",
                "apple_user_id": "apple-returning-br",
                "country_code": "BR",
                "owner_phone": "+5511987654321",
                "twilio_number": "+551199999999",
                "subscription_uuid": "00000000-0000-0000-0000-000000000001",
            }
        return None

    async def fake_update(cid, updates):
        return True

    async def fake_ensure_uuid(cid, existing):
        return "00000000-0000-0000-0000-000000000001"

    monkeypatch.setattr(contractors_api, "_enforce_apple_identity", fake_enforce)
    monkeypatch.setattr("app.db.contractors.get_contractor_by_apple_user_id", fake_by_apple)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)
    monkeypatch.setattr(contractors_api, "ensure_subscription_uuid", fake_ensure_uuid)

    body = contractors_api.ContractorCreate(
        business_name="Returning BR",
        owner_name="Carlos",
        owner_phone="(11) 98765-4321",
        country_code="BR",
        apple_user_id="apple-returning-br",
        apple_identity_token="tok",
    )

    res = await contractors_api.api_create_contractor(body, request=None)
    assert res["status"] == "ok"
    assert res["contractor_id"] == "c-returning-br"
    assert res["existing"] is True


# ===========================================================================
# 4. Direct DB create_contractor & provision_twilio_number defenses
# ===========================================================================


@pytest.mark.asyncio
async def test_db_create_contractor_rejects_unavailable_country(monkeypatch):
    with pytest.raises(ValueError, match="Country not available for new accounts: BR"):
        await contractors_db.create_contractor({
            "business_name": "Direct DB BR",
            "country_code": "BR",
        })


@pytest.mark.asyncio
async def test_db_create_contractor_rejects_unsupported_country(monkeypatch):
    with pytest.raises(ValueError, match="Unsupported country code: XX"):
        await contractors_db.create_contractor({
            "business_name": "Direct DB XX",
            "country_code": "XX",
        })


@pytest.mark.asyncio
async def test_db_provision_twilio_number_rejects_unavailable_country(monkeypatch):
    async def fake_get_contractor(cid):
        return {
            "contractor_id": cid,
            "twilio_number": "",
            "country_code": "BR",
            "owner_phone": "+5511987654321",
        }

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)

    with pytest.raises(Exception, match="Country BR is not available for number provisioning"):
        await contractors_db.provision_twilio_number("c-br", country_code="BR")


@pytest.mark.asyncio
async def test_db_provision_twilio_number_rejects_unsupported_country(monkeypatch):
    async def fake_get_contractor(cid):
        return {
            "contractor_id": cid,
            "twilio_number": "",
            "country_code": "XX",
            "owner_phone": "",
        }

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)

    with pytest.raises(Exception, match="Unsupported country code: XX"):
        await contractors_db.provision_twilio_number("c-xx", country_code="XX")


@pytest.mark.asyncio
async def test_api_provision_number_reuses_existing_assigned_legacy_number(monkeypatch):
    contractor_data = {
        "contractor_id": "c-legacy-gb",
        "twilio_number": "+442071234567",
        "country_code": "GB",
    }

    async def fake_get_contractor(cid):
        return dict(contractor_data)

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)

    res = await contractors_api.api_provision_number("c-legacy-gb", _admin_request("c-legacy-gb"))
    assert res["status"] == "ok"
    assert res["phone_number"] == "+442071234567"
    assert res["existing"] is True
    assert res["country_code"] == "GB"
    assert res["service_binding"] == {
        "country_code": "GB",
        "provider": None,
        "number_type": None,
        "capabilities": None,
    }


# ===========================================================================
# 5. Capabilities Recording During Provisioning
# ===========================================================================


@pytest.mark.asyncio
async def test_db_provision_capabilities_recording(monkeypatch):
    """Tests capabilities recording with missing, partial, false, and non-bool values."""
    contractor_doc = {
        "contractor_id": "c-us-test",
        "twilio_number": "",
        "country_code": "US",
        "owner_phone": "+14155552671",
        "business_name": "Test Co",
    }

    captured_updates = []

    async def fake_get_contractor(cid):
        return dict(contractor_doc)

    async def fake_update_contractor(cid, updates):
        captured_updates.append(dict(updates))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update_contractor)

    # Fake Twilio client
    class FakePurchasedNumber:
        def __init__(self, phone, capabilities):
            self.phone_number = phone
            self.capabilities = capabilities

    class FakeTwilioClient:
        def __init__(self, account_sid, auth_token):
            self.capabilities_response = None
            self.available_phone_numbers = lambda cc: SimpleNamespace(
                local=SimpleNamespace(
                    list=lambda **kwargs: [SimpleNamespace(phone_number="+14155551212")]
                )
            )
            self.incoming_phone_numbers = SimpleNamespace(
                create=lambda **kwargs: FakePurchasedNumber("+14155551212", self.capabilities_response)
            )

    import twilio.rest
    fake_client = FakeTwilioClient("ACtest", "tok")
    monkeypatch.setattr(twilio.rest, "Client", lambda sid, tok: fake_client)

    # 1. Missing whole response -> capabilities is None
    fake_client.capabilities_response = None
    await contractors_db.provision_twilio_number("c-us-test", country_code="US")
    assert captured_updates[-1]["number_capabilities"] is None

    # 2. Partial dict -> includes only actual bool fields
    fake_client.capabilities_response = {"voice": True}
    await contractors_db.provision_twilio_number("c-us-test", country_code="US")
    assert captured_updates[-1]["number_capabilities"] == {"voice": True}

    # 3. False capability -> records exact false bool, not coerced or defaulted to True
    fake_client.capabilities_response = {"voice": True, "sms": False}
    await contractors_db.provision_twilio_number("c-us-test", country_code="US")
    assert captured_updates[-1]["number_capabilities"] == {"voice": True, "sms": False}

    # 4. String "false" -> ignored as non-boolean
    fake_client.capabilities_response = {"voice": "false", "sms": True}
    await contractors_db.provision_twilio_number("c-us-test", country_code="US")
    assert captured_updates[-1]["number_capabilities"] == {"sms": True}


# ===========================================================================
# 6. Geographic Change Locks (PATCH contractor & PUT settings)
# ===========================================================================


@pytest.mark.asyncio
async def test_patch_contractor_locks_country_to_assigned_number(monkeypatch):
    contractor_data = {
        "contractor_id": "c-locked",
        "twilio_number": "+14155552671",
        "provisioned_country_code": "US",
        "country_code": "US",
    }

    async def fake_get_contractor(cid):
        return dict(contractor_data)

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)

    # Attempt to change to CA on an account with assigned US number
    body_conflict = contractors_api.ContractorUpdate(country_code="CA")
    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_update_contractor("c-locked", body_conflict, _admin_request("c-locked"))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "country_locked_to_number"
    assert exc_info.value.detail["country_code"] == "US"

    # Same-country update is allowed
    updated_calls = []

    async def fake_update(cid, updates):
        updated_calls.append(updates)
        return True

    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)

    body_same = contractors_api.ContractorUpdate(country_code="US")
    res_same = await contractors_api.api_update_contractor("c-locked", body_same, _admin_request("c-locked"))
    assert res_same["status"] == "ok"


@pytest.mark.asyncio
async def test_patch_contractor_unassigned_invalid_stored_phone_returns_400(monkeypatch):
    contractor_data = {
        "contractor_id": "c-bad-phone",
        "twilio_number": "",
        "country_code": "US",
        "owner_phone": "invalid-phone",
    }

    async def fake_get_contractor(cid):
        return dict(contractor_data)

    updated_calls = []

    async def fake_update(cid, updates):
        updated_calls.append(updates)
        return True

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)

    body = contractors_api.ContractorUpdate(country_code="CA")
    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_update_contractor("c-bad-phone", body, _admin_request("c-bad-phone"))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["code"] == "invalid_owner_phone"
    assert updated_calls == [], "No update should be performed when owner phone is invalid"


# ===========================================================================
# 7. Markets payload derivation
# ===========================================================================


def test_get_markets_payload_derivation():
    payload = get_markets_payload()
    assert payload["qualification_order"] == list(QUALIFICATION_ORDER)
    assert len(payload["markets"]) == len(PINNED_MARKET_ORDER)

    for market in payload["markets"]:
        cc = market["country_code"]
        expected_status = get_country_status(cc)
        assert market["status"] == expected_status
