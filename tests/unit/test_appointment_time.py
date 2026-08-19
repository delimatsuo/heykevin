"""Appointment slot localization and bookability checks."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.appointment_time import format_wall_clock, slot_is_plausible


NY = {"timezone": "America/New_York"}
NOW = datetime(2026, 8, 19, 11, 0, tzinfo=ZoneInfo("America/New_York"))


def test_format_wall_clock_keeps_spoken_eastern_noon():
    assert (
        format_wall_clock("2026-08-21T12:00:00Z", NY)
        == "Fri, Aug 21 at 12:00 PM"
    )


def test_slot_is_plausible_for_this_week():
    assert slot_is_plausible("2026-08-21T12:00:00-04:00", NY, now=NOW) is True


def test_slot_rejects_year_2020():
    assert slot_is_plausible("2020-08-21T12:00:00Z", NY, now=NOW) is False
