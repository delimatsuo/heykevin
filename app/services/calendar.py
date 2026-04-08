"""Google Calendar client for contractors without Jobber.

Provides free/busy lookup and event creation via Google Calendar API.
Used as a fallback scheduling tool in the voice pipeline.
"""

import httpx
from datetime import datetime, timedelta, timezone

from app.utils.logging import get_logger

logger = get_logger(__name__)

FREEBUSY_URL = "https://www.googleapis.com/calendar/v3/freeBusy"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TOKEN_URL = "https://oauth2.googleapis.com/token"


async def refresh_access_token(refresh_token: str) -> str | None:
    """Exchange a refresh token for a new access token. Returns None on failure."""
    from app.config import settings

    if not refresh_token or not settings.google_calendar_client_id:
        return None

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": settings.google_calendar_client_id,
                    "client_secret": settings.google_calendar_client_secret,
                },
                timeout=10.0,
            )
        if resp.status_code == 200:
            return resp.json().get("access_token")
        logger.error(f"Google token refresh failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Google token refresh error: {e}")
    return None


async def get_available_slots(access_token: str, days_ahead: int = 7) -> list[dict]:
    """Query Google Calendar free/busy and return available 1-hour slots.

    Returns list of dicts: [{"date": "Mon Jan 6", "start": "9:00 AM", "end": "10:00 AM"}, ...]
    """
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=min(days_ahead, 14))

    body = {
        "timeMin": now.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": "primary"}],
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            FREEBUSY_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=8.0,
        )

    if resp.status_code != 200:
        logger.error(f"Google FreeBusy error: {resp.status_code} {resp.text[:200]}")
        return []

    data = resp.json()
    busy_periods = data.get("calendars", {}).get("primary", {}).get("busy", [])

    # Convert busy periods to datetime objects
    busy = []
    for period in busy_periods:
        busy.append((
            datetime.fromisoformat(period["start"].replace("Z", "+00:00")),
            datetime.fromisoformat(period["end"].replace("Z", "+00:00")),
        ))

    # Generate available 1-hour slots during business hours (9 AM - 5 PM local)
    # We use UTC but label as local time — contractor's timezone would improve this
    available = []
    day = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    for _ in range(min(days_ahead, 14)):
        for hour in range(9, 17):  # 9 AM to 5 PM
            slot_start = day.replace(hour=hour)
            slot_end = slot_start + timedelta(hours=1)

            # Check if slot overlaps any busy period
            is_busy = any(
                slot_start < b_end and slot_end > b_start
                for b_start, b_end in busy
            )

            if not is_busy:
                available.append({
                    "date": slot_start.strftime("%a %b %d"),
                    "start": slot_start.strftime("%-I:%M %p"),
                    "end": slot_end.strftime("%-I:%M %p"),
                    "start_iso": slot_start.isoformat(),
                    "end_iso": slot_end.isoformat(),
                })

        day += timedelta(days=1)

    # Cap at 20 slots to keep responses manageable
    return available[:20]


async def book_appointment(
    access_token: str,
    title: str,
    start_time: str,
    end_time: str,
    description: str = "",
) -> str | None:
    """Create a Google Calendar event. Returns event ID or None on failure.

    start_time / end_time should be ISO 8601 strings.
    """
    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            EVENTS_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=8.0,
        )

    if resp.status_code in (200, 201):
        event_id = resp.json().get("id", "")
        logger.info(f"Google Calendar event created: {event_id}")
        return event_id

    logger.error(f"Google Calendar create event error: {resp.status_code} {resp.text[:200]}")
    return None
