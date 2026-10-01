"""Unit tests for GET /api/forwarding-instructions.

The endpoint is the single source of truth for per-country carrier dial codes
the iOS app should present during onboarding and in Settings.

NANP (US/CA) rows reflect CDMA/Verizon-style forwarding (*71/*72/*73)
and deliberately carry only the legacy ``disable``. Granular and standard GSM
forwarding codes (e.g. GSM MMI ``*61*``/``##61#`` on T-Mobile/AT&T) differ across
carriers, so the server table does not assert a single universal code for NANP.

All non-US/CA countries (such as BR, GB, DE, FR, IT, ES, PT) return
``supported: False`` with status ``qualification_required`` (for BR, GB) or
``unsupported`` (for DE, FR, IT, ES, PT) and do not provide activation/deactivation
templates.
"""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

import httpx
import pytest
from fastapi import FastAPI

from app.api import forwarding as forwarding_api
from app.api.forwarding import FORWARDING_CODES, get_forwarding_instructions

QUALIFICATION_REQUIRED_COUNTRIES = ["BR", "GB"]
UNSUPPORTED_GSM_COUNTRIES = ["DE", "FR", "IT", "ES", "PT"]
ALL_NON_NANP_COUNTRIES = QUALIFICATION_REQUIRED_COUNTRIES + UNSUPPORTED_GSM_COUNTRIES
NANP_COUNTRIES = ["US", "CA"]
GRANULAR_DISABLE_KEYS = ("disable_all", "disable_unanswered", "disable_everything")


def test_table_covers_only_nanp_countries():
    assert set(FORWARDING_CODES) == set(NANP_COUNTRIES)


@pytest.mark.parametrize("country", QUALIFICATION_REQUIRED_COUNTRIES)
async def test_qualification_required_countries_return_unsupported_status(country):
    body = await get_forwarding_instructions(country_code=country)
    assert body["supported"] is False
    assert body["status"] == "qualification_required"
    assert body["country_code"] == country
    assert "message" in body
    assert "fallback_message" in body
    assert "forward_all" not in body
    assert "forward_unanswered" not in body
    assert "disable" not in body


@pytest.mark.parametrize("country", UNSUPPORTED_GSM_COUNTRIES)
async def test_unsupported_countries_return_unsupported_status(country):
    body = await get_forwarding_instructions(country_code=country)
    assert body["supported"] is False
    assert body["status"] == "unsupported"
    assert body["country_code"] == country
    assert "message" in body
    assert "fallback_message" in body
    assert "forward_all" not in body
    assert "forward_unanswered" not in body
    assert "disable" not in body


@pytest.mark.parametrize("country", NANP_COUNTRIES)
async def test_nanp_rows_keep_only_the_legacy_disable_code(country):
    """US/CA assert nothing beyond ``*73``: the granular cancel codes differ
    per carrier (T-Mobile US uses GSM MMI, AT&T documents ``*93``), so the
    server must not claim them and a client must not read them."""
    body = await get_forwarding_instructions(country_code=country)
    assert body["supported"] is True
    assert body["forward_unanswered"] == "*71{number}"
    assert body["forward_all"] == "*72{number}"
    assert body["disable"] == "*73"
    for key in GRANULAR_DISABLE_KEYS:
        assert key not in body, f"{country} must not assert {key}"


@pytest.mark.parametrize("country", sorted(FORWARDING_CODES))
async def test_forward_templates_carry_placeholder_and_disable_codes_do_not(country):
    body = await get_forwarding_instructions(country_code=country)
    assert "{number}" in body["forward_all"]
    assert "{number}" in body["forward_unanswered"]
    for key in ("disable", *GRANULAR_DISABLE_KEYS):
        if key in body:
            assert "{number}" not in body[key]


async def test_country_code_is_case_insensitive_nanp():
    body = await get_forwarding_instructions(country_code="ca")
    assert body["supported"] is True
    assert body["country_code"] == "CA"


async def test_country_code_is_case_insensitive_non_nanp():
    body = await get_forwarding_instructions(country_code="br")
    assert body["supported"] is False
    assert body["status"] == "qualification_required"
    assert body["country_code"] == "BR"


async def test_unsupported_country_fails_closed_with_fallback():
    body = await get_forwarding_instructions(country_code="ZZ")
    assert body["supported"] is False
    assert body["status"] == "unsupported"
    assert body["country_code"] == "ZZ"
    assert "message" in body
    assert "forward_unanswered" not in body


async def test_unsupported_country_message_matches_the_uppercased_field():
    body = await get_forwarding_instructions(country_code="zz")
    assert body["country_code"] == "ZZ"
    assert body["status"] == "unsupported"
    assert "not available for ZZ." in body["message"]


# ---------------------------------------------------------------------------
# HTTP surface: the exact JSON shape a client parses, behind the auth dependency.
# ---------------------------------------------------------------------------


def _app_with_auth_bypassed() -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(forwarding_api.router)
    test_app.dependency_overrides[forwarding_api.verify_api_token] = lambda: None
    return test_app


async def test_route_serves_nanp_shape_over_http():
    transport = httpx.ASGITransport(app=_app_with_auth_bypassed())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/forwarding-instructions", params={"country_code": "us"})

    assert response.status_code == 200
    body = response.json()
    assert body["supported"] is True
    assert body["country_code"] == "US"
    assert set(body) == {
        "supported",
        "country_code",
        "forward_all",
        "forward_unanswered",
        "disable",
        "notes",
        "recommended",
        "fallback_message",
    }


async def test_route_serves_non_nanp_qualification_required_shape_over_http():
    transport = httpx.ASGITransport(app=_app_with_auth_bypassed())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/forwarding-instructions", params={"country_code": "br"})

    assert response.status_code == 200
    body = response.json()
    assert body["supported"] is False
    assert body["status"] == "qualification_required"
    assert body["country_code"] == "BR"
    assert "forward_all" not in body
    assert "forward_unanswered" not in body


async def test_route_rejects_country_code_longer_than_two_characters():
    transport = httpx.ASGITransport(app=_app_with_auth_bypassed())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/forwarding-instructions", params={"country_code": "BRA"})

    assert response.status_code == 422
