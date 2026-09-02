"""Unit tests for GET /api/forwarding-instructions.

The endpoint is the single source of truth for per-country carrier dial codes
the iOS app should present during onboarding and in Settings. The invariant
that matters most: the ``disable`` code must cancel the *recommended*
forwarding mode. On GSM networks unconditional forwarding is supplementary
service code 21 and no-reply forwarding is service code 61 — erasing one does
not erase the other — so a table that recommends ``**61*{number}#`` but
disables with ``##21#`` leaves the user unable to turn Kevin off.
"""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

import pytest

from app.api.forwarding import FORWARDING_CODES, get_forwarding_instructions

GSM_COUNTRIES = ["BR", "GB", "DE", "FR", "IT", "ES", "PT"]
NANP_COUNTRIES = ["US", "CA"]


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


@pytest.mark.parametrize("country", NANP_COUNTRIES)
async def test_nanp_every_disable_code_is_star_73(country):
    body = await get_forwarding_instructions(country_code=country)
    assert body["forward_unanswered"] == "*71{number}"
    assert body["forward_all"] == "*72{number}"
    assert body["disable_unanswered"] == "*73"
    assert body["disable_all"] == "*73"
    assert body["disable_everything"] == "*73"


@pytest.mark.parametrize("country", sorted(FORWARDING_CODES))
async def test_disable_cancels_the_recommended_mode(country):
    """The generic ``disable`` must undo whatever ``recommended`` sets up."""
    body = await get_forwarding_instructions(country_code=country)
    recommended = body["recommended"]
    assert recommended in {"forward_all", "forward_unanswered"}
    mode = recommended.removeprefix("forward_")
    assert body["disable"] == body[f"disable_{mode}"]


@pytest.mark.parametrize("country", sorted(FORWARDING_CODES))
async def test_every_forward_template_has_number_placeholder(country):
    body = await get_forwarding_instructions(country_code=country)
    assert "{number}" in body["forward_all"]
    assert "{number}" in body["forward_unanswered"]
    for key in ("disable", "disable_all", "disable_unanswered", "disable_everything"):
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
