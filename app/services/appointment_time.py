"""Normalize appointment datetimes the voice model hands us.

Callers speak wall-clock times in the contractor's timezone. Gemini often
emits that clock with a Z / UTC suffix (`2026-08-21T12:00:00Z` for noon).
Treating that as a real UTC instant shifts Eastern August noon to 8:00 AM.
Availability slots already include the contractor offset (`-04:00`); those
stay exact instants.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


def contractor_zone(contractor: dict[str, Any] | None) -> ZoneInfo | None:
    name = (contractor or {}).get("timezone") or ""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def parse_iso_datetime(value: str) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def localize_spoken_slot(value: str, contractor: dict[str, Any] | None) -> str:
    """Return ISO 8601 in the contractor zone, preserving spoken UTC/Z clocks."""
    parsed = parse_iso_datetime(value)
    if parsed is None:
        return value

    zone = contractor_zone(contractor)
    if zone is None:
        return parsed.isoformat()

    if parsed.tzinfo is None:
        localized = parsed.replace(tzinfo=zone)
    elif parsed.utcoffset() == timedelta(0):
        localized = parsed.replace(tzinfo=zone)
    else:
        localized = parsed.astimezone(zone)
    return localized.isoformat()


def format_wall_clock(start_time: str, contractor: dict[str, Any] | None) -> str:
    """Render a slot as the contractor's wall clock, e.g. Fri, Aug 21 at 12:00 PM."""
    localized = localize_spoken_slot(start_time, contractor)
    parsed = parse_iso_datetime(localized)
    if parsed is None:
        return start_time
    hour = parsed.hour % 12 or 12
    meridiem = "AM" if parsed.hour < 12 else "PM"
    return f"{parsed:%a}, {parsed:%b} {parsed.day} at {hour}:{parsed:%M} {meridiem}"


def _slot_offset(
    value: str,
    contractor: dict[str, Any] | None,
    now: datetime | None = None,
) -> timedelta | None:
    """How far a spoken slot sits from now, or None when it will not parse."""
    localized = localize_spoken_slot(value, contractor)
    parsed = parse_iso_datetime(localized)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        zone = contractor_zone(contractor)
        parsed = parsed.replace(tzinfo=zone or ZoneInfo("UTC"))
    clock = now or datetime.now(parsed.tzinfo)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=parsed.tzinfo)
    else:
        clock = clock.astimezone(parsed.tzinfo)
    return parsed - clock


def slot_is_plausible(
    value: str,
    contractor: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Reject absurd years (e.g. 2020) while allowing last week's leftover slot.

    Deliberately lenient: this guards the owner's Confirm tap, where a request
    taken a week ago is still legitimately bookable. Do not reuse it to vet a
    slot during a live call — see `slot_is_bookable`.
    """
    offset = _slot_offset(value, contractor, now)
    if offset is None:
        return False
    return timedelta(days=-400) <= offset <= timedelta(days=400)


# A slot being agreed during a call is a different question from one the owner
# is confirming later. `slot_is_plausible`'s ±400 days keeps a leftover request
# confirmable, but that window also swallows the likeliest model error — a year
# out either way. On 2026-08-20 both 2025-08-21 and 2027-08-21 sit inside it.
#
# check_availability only ever offers 14 days ahead, so a horizon of six months
# leaves ample room for a caller asking well in advance while still catching a
# year-sized mistake. The past is barely allowed at all: a new booking is not
# retrospective, and a day of grace covers a slot earlier today.
BOOKABLE_GRACE = timedelta(days=1)
BOOKABLE_HORIZON = timedelta(days=180)


def slot_is_bookable(
    value: str,
    contractor: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether a slot is sane to accept while still on the call with the caller."""
    offset = _slot_offset(value, contractor, now)
    if offset is None:
        return False
    return -BOOKABLE_GRACE <= offset <= BOOKABLE_HORIZON
