"""Google Calendar client for contractors without Jobber.

Provides free/busy lookup and event creation via Google Calendar API.
Used as a fallback scheduling tool in the voice pipeline.

Google access tokens are opaque bearer strings (unlike Jobber's JWTs), so
we can't decode an expiry locally — we track it ourselves in Firestore
(`google_calendar_token_expires_at`, stamped at connect and at each
refresh). Contractors who connected before that field existed have no
stored expiry; for them the proactive check is a no-op and the 401-retry
in _with_token_refresh() is the real backstop. Mirrors the refresh/retry
shape in app/services/jobber.py.
"""

import asyncio
import time
import httpx
from datetime import datetime, timedelta, timezone

from app.utils.logging import get_logger

logger = get_logger(__name__)

FREEBUSY_URL = "https://www.googleapis.com/calendar/v3/freeBusy"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TOKEN_URL = "https://oauth2.googleapis.com/token"

_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


async def _write_google_calendar_tokens(contractor_id: str, updates: dict):
    """Persist refreshed Google Calendar tokens on the contractor document."""
    if not contractor_id:
        return
    from app.db.firestore_client import get_firestore_client

    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: db.collection("contractors").document(contractor_id).update(updates),
    )


async def _read_google_calendar_tokens(contractor_id: str) -> dict:
    """Read latest stored Google Calendar tokens to avoid racing concurrent refreshes."""
    if not contractor_id:
        return {}
    from app.db.firestore_client import get_firestore_client

    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc = await loop.run_in_executor(
        None,
        lambda: db.collection("contractors").document(contractor_id).get(),
    )
    if not doc.exists:
        return {}
    data = doc.to_dict() or {}
    return {
        "google_calendar_access_token": data.get("google_calendar_access_token", ""),
        "google_calendar_refresh_token": data.get("google_calendar_refresh_token", ""),
        "google_calendar_token_expires_at": data.get("google_calendar_token_expires_at"),
    }


def _token_expires_soon(contractor: dict, leeway_seconds: int = 120) -> bool:
    """Return True only when we KNOW the stored token is expired/expiring.

    Contractors connected before this field existed have no stored expiry;
    treat that as "unknown", not "expiring" — forcing a refresh on every
    call for them would add latency with no evidence it's needed. The
    401-retry path is what actually protects those contractors.
    """
    expires_at = contractor.get("google_calendar_token_expires_at")
    return isinstance(expires_at, (int, float)) and expires_at <= time.time() + leeway_seconds


async def refresh_access_token(contractor: dict, *, force: bool = False) -> str | None:
    """Refresh and persist Google Calendar OAuth tokens for a contractor.

    Mutates `contractor` in place with the refreshed values (mirrors
    app/services/jobber.py::refresh_access_token) so callers holding the
    same dict see the fresh token without re-reading Firestore.
    """
    from app.config import settings

    contractor_id = contractor.get("contractor_id", "")
    lock_key = contractor_id or contractor.get("google_calendar_refresh_token", "")
    lock = _REFRESH_LOCKS.setdefault(lock_key, asyncio.Lock())

    async with lock:
        stale_token = contractor.get("google_calendar_access_token", "")
        latest = await _read_google_calendar_tokens(contractor_id)
        if latest:
            contractor.update({k: v for k, v in latest.items() if v})

        current_token = contractor.get("google_calendar_access_token", "")
        if current_token and current_token != stale_token and not _token_expires_soon(contractor):
            return current_token
        if current_token and not force and not _token_expires_soon(contractor):
            return current_token

        refresh_token = contractor.get("google_calendar_refresh_token", "")
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
            if resp.status_code != 200:
                logger.error(f"Google token refresh failed: {resp.status_code} {resp.text[:200]}")
                return None

            tokens = resp.json()
            access_token = tokens.get("access_token", "")
            if not access_token:
                logger.error("Google token refresh returned no access token")
                return None
            # Google typically omits refresh_token on refresh (the original stays valid).
            new_refresh_token = tokens.get("refresh_token", refresh_token)
            expires_in = tokens.get("expires_in", 3300)

            updates = {
                "google_calendar_access_token": access_token,
                "google_calendar_refresh_token": new_refresh_token,
                "google_calendar_token_expires_at": time.time() + expires_in,
                "google_calendar_token_refreshed_at": time.time(),
            }

            contractor.update(updates)
            try:
                await _write_google_calendar_tokens(contractor_id, updates)
            except Exception as e:
                logger.error(f"Google Calendar token persistence failed after refresh: {e}")
            logger.info(f"Google Calendar token refreshed for contractor {contractor_id[:8] or 'unknown'}")
            return access_token
        except Exception as e:
            logger.error(f"Google token refresh error: {e}")
            return None


async def _resolve_access_token(contractor: dict) -> str:
    access_token = contractor.get("google_calendar_access_token", "")
    if access_token and _token_expires_soon(contractor):
        refreshed = await refresh_access_token(contractor)
        if refreshed:
            return refreshed
    return access_token


async def _with_token_refresh(contractor: dict, call):
    """Resolve a (possibly proactively-refreshed) token, call `call(token)`,
    and on a 401 do one forced refresh + retry. Mirrors
    app/services/jobber.py::_graphql_request_with_refresh.
    """
    access_token = await _resolve_access_token(contractor)
    if not access_token:
        return None

    resp = await call(access_token)
    if resp.status_code != 401:
        return resp

    refreshed = await refresh_access_token(contractor, force=True)
    if not refreshed:
        return resp

    return await call(refreshed)


async def get_available_slots(contractor: dict, days_ahead: int = 7) -> list[dict]:
    """Query Google Calendar free/busy and return available 1-hour slots.

    `contractor` is the contractor's Firestore-backed config dict (must
    contain google_calendar_access_token / _refresh_token, and ideally
    contractor_id + google_calendar_token_expires_at). Refreshes the
    access token first if it's known to be expiring, and retries once
    more on a 401 for contractors whose expiry isn't tracked yet.

    Returns list of dicts: [{"date": "Mon Jan 6", "start": "9:00 AM", "end": "10:00 AM"}, ...]
    """
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=min(days_ahead, 14))

    body = {
        "timeMin": now.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": "primary"}],
    }

    async def _call(token: str):
        async with httpx.AsyncClient() as client:
            return await client.post(
                FREEBUSY_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=8.0,
            )

    resp = await _with_token_refresh(contractor, _call)
    if resp is None:
        logger.error("Google FreeBusy error: no valid access token")
        return []

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
    contractor: dict,
    title: str,
    start_time: str,
    end_time: str,
    description: str = "",
) -> str | None:
    """Create a Google Calendar event. Returns event ID or None on failure.

    `contractor` is the contractor's config dict (see get_available_slots).
    start_time / end_time should be ISO 8601 strings.
    """
    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
    }

    async def _call(token: str):
        async with httpx.AsyncClient() as client:
            return await client.post(
                EVENTS_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=8.0,
            )

    resp = await _with_token_refresh(contractor, _call)
    if resp is None:
        logger.error("Google Calendar create event error: no valid access token")
        return None

    if resp.status_code in (200, 201):
        event_id = resp.json().get("id", "")
        logger.info(f"Google Calendar event created: {event_id}")
        return event_id

    logger.error(
        f"Google Calendar create event error: operation=create_event status_code={resp.status_code}"
    )
    return None
