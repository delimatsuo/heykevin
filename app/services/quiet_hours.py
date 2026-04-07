"""Quiet hours — Kevin answers everything during configured hours, only escalates emergencies."""

from datetime import datetime, time as dtime
from typing import Optional

from app.utils.logging import get_logger

logger = get_logger(__name__)

# Default quiet hours (configured per user in settings)
DEFAULT_QUIET_START = dtime(22, 0)  # 10 PM
DEFAULT_QUIET_END = dtime(7, 0)     # 7 AM
DEFAULT_TIMEZONE = "America/Los_Angeles"


def is_quiet_hours(
    quiet_start: Optional[str] = None,
    quiet_end: Optional[str] = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> bool:
    """Check if the current time is within quiet hours."""
    try:
        # Parse configured times or use defaults
        if quiet_start:
            start = dtime.fromisoformat(quiet_start)
        else:
            start = DEFAULT_QUIET_START

        if quiet_end:
            end = dtime.fromisoformat(quiet_end)
        else:
            end = DEFAULT_QUIET_END

        # Get current time in user's timezone
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(timezone)).time()
        except ImportError:
            now = datetime.now().time()

        # Handle overnight ranges (e.g., 22:00 - 07:00)
        if start > end:
            return now >= start or now <= end
        else:
            return start <= now <= end

    except Exception as e:
        logger.error(f"Quiet hours check failed: {e}")
        return False


def get_quiet_hours_routing_override(trust_score: int) -> Optional[str]:
    """During quiet hours, override routing to screen everything except emergencies.

    Returns the override route name, or None if no override.
    """
    if not is_quiet_hours():
        return None

    # During quiet hours, even VIP calls get screened (Kevin answers)
    # Only exception: explicitly whitelisted contacts still ring through
    if trust_score >= 100:  # Whitelisted — always ring through
        return None

    # Everything else gets screened by Kevin during quiet hours
    logger.info("Quiet hours active — routing to AI screening")
    return "ai_screening"
