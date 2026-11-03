"""Quiet hours — Kevin answers non-whitelisted callers during an account's window."""

from datetime import datetime, time as dtime
from typing import Optional

from app.utils.logging import get_logger

logger = get_logger(__name__)

# Clock defaults used only when a caller does not pass a window. Routing does
# not use these: it uses the account settings below.
DEFAULT_QUIET_START = dtime(22, 0)  # 10 PM
DEFAULT_QUIET_END = dtime(7, 0)  # 7 AM
# Business-hours fallback when a contractor has no timezone. Quiet-hours
# routing does not use this; an unset quiet-hours zone uses the settings default.
DEFAULT_TIMEZONE = "UTC"

# Same values as app.api.settings._DEFAULT_SETTINGS. Quiet hours stay off
# until the account turns them on.
ACCOUNT_QUIET_HOURS_DEFAULTS = {
    "quiet_hours_enabled": False,
    "quiet_hours_start": "22:00",
    "quiet_hours_end": "07:00",
    "quiet_hours_tz": "America/Los_Angeles",
}


def _configured_clock(value: object, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def quiet_hours_routing_args(preferences: Optional[dict]) -> dict:
    """Map a settings document onto routing arguments.

    A missing document matches GET /api/settings: quiet hours are off, and
    the stored window is 22:00–07:00 in America/Los_Angeles only if enabled.
    """
    stored = preferences if isinstance(preferences, dict) else {}
    defaults = ACCOUNT_QUIET_HOURS_DEFAULTS
    return {
        "enabled": stored.get("quiet_hours_enabled") is True,
        "quiet_start": _configured_clock(
            stored.get("quiet_hours_start"), defaults["quiet_hours_start"]
        ),
        "quiet_end": _configured_clock(stored.get("quiet_hours_end"), defaults["quiet_hours_end"]),
        "timezone": _configured_clock(stored.get("quiet_hours_tz"), defaults["quiet_hours_tz"]),
    }


def _wall_time(timezone: str, now: Optional[datetime]) -> dtime:
    """Current wall-clock time in `timezone`. Aware `now` is converted first."""
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(timezone)
        if now is None:
            current = datetime.now(zone)
        elif now.tzinfo is None:
            current = now.replace(tzinfo=zone)
        else:
            current = now.astimezone(zone)
        return current.time()
    except ImportError:
        current = now if now is not None else datetime.now()
        return current.time()


def is_quiet_hours(
    quiet_start: Optional[str] = None,
    quiet_end: Optional[str] = None,
    timezone: str = DEFAULT_TIMEZONE,
    now: Optional[datetime] = None,
) -> bool:
    """Check if `now` (default: the current time) is inside the quiet-hours window."""
    try:
        start = dtime.fromisoformat(quiet_start) if quiet_start else DEFAULT_QUIET_START
        end = dtime.fromisoformat(quiet_end) if quiet_end else DEFAULT_QUIET_END
        current = _wall_time(timezone, now)

        # Overnight ranges (e.g., 22:00 - 07:00) cross midnight.
        if start > end:
            return current >= start or current <= end
        return start <= current <= end

    except Exception as e:
        logger.error(f"Quiet hours check failed: {e}")
        return False


def is_business_hours(contractor: dict) -> bool:
    """Check if the current time is within the contractor's business hours.

    This is SEPARATE from quiet hours. Business hours control greeting text
    (e.g., 8AM-5PM). Quiet hours control routing overrides (e.g., 10PM-7AM).
    """
    try:
        start_str = contractor.get("business_hours_start", "")
        end_str = contractor.get("business_hours_end", "")

        if not start_str or not end_str:
            return True  # No hours configured = always business hours

        start = dtime.fromisoformat(start_str)
        end = dtime.fromisoformat(end_str)
        timezone = contractor.get("timezone", DEFAULT_TIMEZONE)

        try:
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo(timezone)).time()
        except ImportError:
            now = datetime.now().time()

        # Normal range (e.g., 08:00 - 17:00)
        if start <= end:
            return start <= now <= end
        # Overnight range (shouldn't be typical for business hours, but handle it)
        return now >= start or now <= end

    except Exception as e:
        logger.error(f"Business hours check failed: {e}")
        return True  # Default to business hours on error


def get_quiet_hours_routing_override(
    trust_score: int,
    *,
    enabled: bool = False,
    quiet_start: Optional[str] = None,
    quiet_end: Optional[str] = None,
    timezone: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Screen non-whitelisted callers during this account's quiet hours.

    Returns ``ai_screening``, or None when the account has not enabled quiet
    hours, the call is outside that account's window, or the caller is
    whitelisted (trust score 100). There is no shared UTC window.
    """
    if enabled is not True:
        return None

    zone = (
        timezone.strip()
        if isinstance(timezone, str) and timezone.strip()
        else (ACCOUNT_QUIET_HOURS_DEFAULTS["quiet_hours_tz"])
    )
    if not is_quiet_hours(quiet_start, quiet_end, zone, now=now):
        return None

    # Whitelisted contacts still ring through. Every other score is screened,
    # including VIPs that are not explicitly whitelisted.
    if trust_score >= 100:
        return None

    logger.info("Quiet hours active — routing to AI screening")
    return "ai_screening"


async def load_quiet_hours_preferences(contractor_id: str) -> dict:
    """Read contractors/{id}/settings/preferences. Failures return {} (quiet hours off)."""
    if not contractor_id:
        return {}
    try:
        from app.db.firestore_client import get_firestore_client

        snap = (
            get_firestore_client()
            .collection("contractors")
            .document(contractor_id)
            .collection("settings")
            .document("preferences")
            .get()
        )
        if getattr(snap, "exists", False):
            data = snap.to_dict() or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        logger.error(
            "Quiet hours settings read failed for %s",
            contractor_id,
            exc_info=True,
        )
    return {}
