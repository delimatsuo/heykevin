"""Personal-mode Kevin must not invent answers on the owner's behalf.

On call CAa5e0de (personal mode) the caller asked "Do you guys do toilet
replacement?" and Kevin answered "Yes, we do toilet replacement." There is
no business in personal mode — Kevin fabricated a service offering. The
prompt gave him no rule against it: personal mode is a message-taker, and
substantive questions about the owner's work belong in the message.
"""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services.voice_pipeline import build_system_prompt


def _personal_prompt() -> str:
    return build_system_prompt({
        "contractor_id": "c1",
        "owner_name": "Deli Matsuo",
        "effective_mode": "personal",
    })


def test_personal_prompt_forbids_answering_for_the_owner():
    lowered = _personal_prompt().lower()

    assert "must not guess" in lowered
    assert "pass the question along" in lowered


def test_personal_prompt_covers_the_do_you_guys_do_shape():
    """The exact question shape from the live call must be addressed."""
    lowered = _personal_prompt().lower()

    assert "do you" in lowered and "offer" in lowered


def test_business_prompt_is_unaffected():
    """Business Kevin has a knowledge base and legitimately answers these."""
    business = build_system_prompt({
        "contractor_id": "c1",
        "owner_name": "Deli Matsuo",
        "business_name": "Electus",
        "effective_mode": "business",
    })

    assert "must not guess" not in business.lower()


def test_personal_prompt_requires_name_and_reason_before_hold():
    """Prompt must instruct Kevin to get both name and reason before checking availability."""
    lowered = _personal_prompt().lower()
    assert "both" in lowered
    assert "name" in lowered
    assert "regarding" in lowered
    assert "stay completely silent" in lowered
    assert "does not use calendars" in lowered

