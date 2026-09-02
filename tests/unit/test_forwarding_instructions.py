"""Unit tests for GET /api/forwarding-instructions.

The endpoint is the single source of truth for per-country carrier dial codes
the iOS app should present during onboarding and in Settings. The invariant
that matters most: the ``disable`` code must cancel the *recommended*
forwarding mode. On GSM networks unconditional forwarding is supplementary
service code 21 and no-reply forwarding is service code 61 — erasing one does
not erase the other — so a table that recommends ``**61*{number}#`` but
disables with ``##21#`` leaves the user unable to turn Kevin off.

NANP (US/CA) rows deliberately carry only the legacy ``disable``: the granular
cancel codes are not uniform across the carriers those rows name, so the
server does not assert them and clients must not consume them.
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

GSM_COUNTRIES = ["BR", "GB", "DE", "FR", "IT", "ES", "PT"]
NANP_COUNTRIES = ["US", "CA"]
GRANULAR_DISABLE_KEYS = ("disable_all", "disable_unanswered", "disable_everything")


def test_table_covers_exactly_the_expected_countries():
    assert set(FORWARDING_CODES) == set(GSM_COUNTRIES) | set(NANP_COUNTRIES)


@pytest.mark.parametrize("country", GSM_COUNTRIES)
async def test_gsm_disable_unanswered_targets_service_code_61(country):
    body = await get_forwarding_instructions(country_code=country)
    assert body["supported"] is True
    assert body["forward_unanswered"] == "**61*{number}#"
    assert body["disable_unanswered"] == "##61#"


@pytest.mark.parametrize("country", GSM_COUNTRIES)
async def test_gsm_disable_all_targets_service_code_21(country):
    body = await get_forwarding_instructions(country_code=country)
    assert body["forward_all"] == "**21*{number}#"
    assert body["disable_all"] == "##21#"


@pytest.mark.parametrize("country", GSM_COUNTRIES)
async def test_gsm_disable_everything_is_002(country):
    body = await get_forwarding_instructions(country_code=country)
    assert body["disable_everything"] == "##002#"


@pytest.mark.parametrize("country", GSM_COUNTRIES)
async def test_gsm_disable_cancels_the_recommended_mode(country):
    """The generic ``disable`` must undo whatever ``recommended`` sets up.

    This is the assertion that fails on the pre-fix table, where ``disable``
    was ``##21#`` next to ``recommended: forward_unanswered``.
    """
    body = await get_forwarding_instructions(country_code=country)
    assert body["recommended"] == "forward_unanswered"
    assert body["disable"] == body["disable_unanswered"] == "##61#"


@pytest.mark.parametrize("country", NANP_COUNTRIES)
async def test_nanp_rows_keep_only_the_legacy_disable_code(country):
    """US/CA assert nothing beyond ``*73``: the granular cancel codes differ
    per carrier (T-Mobile US uses GSM MMI, AT&T documents ``*93``), so the
    server must not claim them and a client must not read them."""
    body = await get_forwarding_instructions(country_code=country)
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


async def test_country_code_is_case_insensitive():
    body = await get_forwarding_instructions(country_code="br")
    assert body["supported"] is True
    assert body["country_code"] == "BR"


async def test_unsupported_country_fails_closed_with_fallback():
    body = await get_forwarding_instructions(country_code="ZZ")
    assert body["supported"] is False
    assert body["country_code"] == "ZZ"
    assert "message" in body
    assert "forward_unanswered" not in body


async def test_unsupported_country_message_matches_the_uppercased_field():
    body = await get_forwarding_instructions(country_code="zz")
    assert body["country_code"] == "ZZ"
    assert "not available for ZZ." in body["message"]


# ---------------------------------------------------------------------------
# HTTP surface: the exact JSON shape a client parses, behind the auth dependency.
# ---------------------------------------------------------------------------


def _app_with_auth_bypassed() -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(forwarding_api.router)
    test_app.dependency_overrides[forwarding_api.verify_api_token] = lambda: None
    return test_app


async def test_route_serves_full_gsm_shape_over_http():
    transport = httpx.ASGITransport(app=_app_with_auth_bypassed())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/forwarding-instructions", params={"country_code": "br"})

    assert response.status_code == 200
    body = response.json()
    assert body["supported"] is True
    assert body["country_code"] == "BR"
    assert set(body) == {
        "supported",
        "country_code",
        "forward_all",
        "forward_unanswered",
        "disable",
        "disable_all",
        "disable_unanswered",
        "disable_everything",
        "notes",
        "recommended",
        "fallback_message",
    }
    assert body["disable"] == body["disable_unanswered"] == "##61#"


async def test_route_rejects_country_code_longer_than_two_characters():
    transport = httpx.ASGITransport(app=_app_with_auth_bypassed())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/forwarding-instructions", params={"country_code": "BRA"})

    assert response.status_code == 422
