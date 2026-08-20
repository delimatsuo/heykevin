"""Kevin must know what day it is before resolving a caller's date.

Live call on 2026-08-19: the caller asked for "Friday, August 21st at 3:00 PM"
and the request was stored as **2020**-08-21T15:00:00. Aug 21 2020 was also a
Friday, which is exactly why it looked plausible enough to persist.

Nothing in the prompt told the model the current date, so a relative date had
no anchor and it supplied a year from its own priors. The year only came out
right when the model copied `start_iso` back from `check_availability`; a
caller who named a time directly got fiction.

Downstream this was invisible until Confirm: `slot_is_plausible` rejected the
year with a 422, which the app rendered as "Couldn't add this to Google
Calendar. Try again." — a retry prompt for something retrying can never fix.
"""

import os
import re
from datetime import datetime

from zoneinfo import ZoneInfo

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services.voice_pipeline import build_system_prompt


def _business_prompt(**overrides) -> str:
    config = {
        "contractor_id": "c1",
        "owner_name": "Deli Matsuo",
        "business_name": "Electus USA",
        "effective_mode": "business",
        "timezone": "America/New_York",
    }
    config.update(overrides)
    return build_system_prompt(config)


def test_prompt_states_todays_date():
    prompt = _business_prompt()
    assert "TODAY'S DATE:" in prompt


def test_prompt_carries_the_actual_current_year():
    """The specific defect: a past year reaching book_appointment."""
    prompt = _business_prompt()
    match = re.search(r"TODAY'S DATE: It is ([^.]+)\.", prompt)
    assert match, "date anchor missing from prompt"

    stated = match.group(1)
    expected_year = str(datetime.now(ZoneInfo("America/New_York")).year)
    assert expected_year in stated, f"expected {expected_year} in {stated!r}"
    assert "2020" not in stated


def test_date_anchor_uses_the_contractor_timezone_not_utc():
    """"Today" belongs to the contractor. Near midnight UTC the two differ.

    A Pacific contractor at 5pm local is already tomorrow in UTC, so anchoring
    on UTC would hand Kevin the wrong day for part of every evening.
    """
    prompt = _business_prompt(timezone="Pacific/Kiritimati")
    match = re.search(r"TODAY'S DATE: It is [A-Za-z]+, ([A-Za-z]+ \d+, \d{4})\.", prompt)
    assert match, "date anchor missing or malformed"

    stated = datetime.strptime(match.group(1), "%B %d, %Y").date()
    local_today = datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    assert stated == local_today


def test_prompt_tells_the_model_to_resolve_relative_dates():
    prompt = _business_prompt()
    anchor = prompt[prompt.index("TODAY'S DATE:"):]
    assert "current year" in anchor
    assert "past year" in anchor


def test_missing_timezone_still_produces_a_date_anchor():
    """A contractor without a timezone must not lose the anchor entirely.

    Falling back to server-local is imperfect, but a prompt with no date at all
    is what caused the 2020 booking.
    """
    prompt = _business_prompt(timezone="")
    assert "TODAY'S DATE:" in prompt
    assert str(datetime.now().year) in prompt
