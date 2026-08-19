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
