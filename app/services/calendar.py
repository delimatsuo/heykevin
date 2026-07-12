"""Google Calendar client for contractors without Jobber.

Provides free/busy lookup and event creation via Google Calendar API.
Used as a fallback scheduling tool in the voice pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app.utils.logging import get_logger

logger = get_logger(__name__)

FREEBUSY_URL = "https://www.googleapis.com/calendar/v3/freeBusy"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKEN_EXPIRY_LEEWAY_SECONDS = 120
GOOGLE_HTTP_TIMEOUT_SECONDS = 2.5
MAX_DAYS_AHEAD = 14
MAX_RETURNED_SLOTS = 20
DEFAULT_TIMEZONE = "UTC"
DEFAULT_BUSINESS_HOURS_START = "09:00"
DEFAULT_BUSINESS_HOURS_END = "17:00"
_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


class GoogleCalendarAuthError(Exception):
    """Raised when Google rejects the current Calendar access token."""


class GoogleCalendarUnavailableError(Exception):
    """Raised when Calendar availability cannot be determined reliably."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch_now() -> float:
    return time.time()


def _token_expires_soon(contractor: dict, leeway_seconds: int = TOKEN_EXPIRY_LEEWAY_SECONDS) -> bool:
    expires_at = contractor.get("google_calendar_token_expires_at")
    try:
        return expires_at is not None and float(expires_at) <= _epoch_now() + leeway_seconds
    except (TypeError, ValueError):
        return True


async def _write_google_tokens(contractor_id: str, updates: dict) -> None:
    if not contractor_id:
        return
    from app.db.firestore_client import get_firestore_client

    db = get_firestore_client()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: db.collection("contractors").document(contractor_id).update(updates),
    )


async def _read_google_tokens(contractor_id: str) -> dict:
    if not contractor_id:
        return {}
    from app.db.firestore_client import get_firestore_client

    db = get_firestore_client()
    loop = asyncio.get_running_loop()
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


def _refresh_lock_key(contractor: dict) -> str:
    contractor_id = str(contractor.get("contractor_id", ""))
    if contractor_id:
        return contractor_id
    refresh_token = str(contractor.get("google_calendar_refresh_token", ""))
    return hashlib.sha256(refresh_token.encode()).hexdigest()[:16]


async def refresh_access_token(contractor: dict, *, force: bool = False) -> str | None:
    """Refresh and persist Google Calendar credentials for a contractor."""
    from app.config import settings

    lock = _REFRESH_LOCKS.setdefault(_refresh_lock_key(contractor), asyncio.Lock())
    async with lock:
        stale_token = contractor.get("google_calendar_access_token", "")
        contractor_id = contractor.get("contractor_id", "")

        try:
            latest = await _read_google_tokens(contractor_id)
        except Exception as error:
            logger.error(
                "Google token state read failed: exception_type=%s",
                type(error).__name__,
            )
            latest = {}
        contractor.update({key: value for key, value in latest.items() if value not in (None, "")})

        current_token = contractor.get("google_calendar_access_token", "")
        if current_token and current_token != stale_token and not _token_expires_soon(contractor):
            return current_token
        if current_token and not force and not _token_expires_soon(contractor):
            return current_token

        refresh_token = contractor.get("google_calendar_refresh_token", "")
        if not refresh_token or not settings.google_calendar_client_id or not settings.google_calendar_client_secret:
            return None

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": settings.google_calendar_client_id,
                        "client_secret": settings.google_calendar_client_secret,
                    },
                    timeout=GOOGLE_HTTP_TIMEOUT_SECONDS,
                )
            if response.status_code != 200:
                logger.error(
                    "Google token refresh failed: status_code=%s",
                    response.status_code,
                )
                return None

            tokens = response.json()
            access_token = tokens.get("access_token", "")
            if not access_token:
                logger.error("Google token refresh returned no access token")
                return None

            now = _epoch_now()
            new_refresh_token = tokens.get("refresh_token") or refresh_token
            updates = {
                "google_calendar_access_token": access_token,
                "google_calendar_refresh_token": new_refresh_token,
                "google_calendar_token_refreshed_at": now,
            }
            try:
                expires_in = float(tokens["expires_in"])
            except (KeyError, TypeError, ValueError):
                pass
            else:
                updates["google_calendar_token_expires_at"] = now + max(0.0, expires_in)

            contractor.update(updates)
            try:
                await _write_google_tokens(contractor_id, updates)
            except Exception as error:
                logger.error(
                    "Google token persistence failed after refresh: exception_type=%s",
                    type(error).__name__,
                )
            logger.info("Google Calendar token refreshed for contractor=%s", contractor_id[:8] or "unknown")
            return access_token
        except Exception as error:
            logger.error(
                "Google token refresh error: exception_type=%s",
                type(error).__name__,
            )
            return None


async def _resolve_access_token(auth: str | dict) -> str:
    if not isinstance(auth, dict):
        return auth

    access_token = auth.get("google_calendar_access_token", "")
    if not access_token or _token_expires_soon(auth):
        refreshed = await refresh_access_token(auth)
        if refreshed:
            return refreshed
    return access_token


async def _freebusy_request(access_token: str, body: dict) -> dict | None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            FREEBUSY_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=GOOGLE_HTTP_TIMEOUT_SECONDS,
        )

    if response.status_code == 401:
        raise GoogleCalendarAuthError("Google Calendar access token rejected")
    if response.status_code != 200:
        logger.error("Google FreeBusy error: status_code=%s", response.status_code)
        return None
    return response.json()


async def _freebusy_with_refresh(auth: str | dict, body: dict) -> dict | None:
    access_token = await _resolve_access_token(auth)
    if not access_token:
        logger.error("Google FreeBusy unavailable: missing access token")
        return None

    try:
        return await _freebusy_request(access_token, body)
    except GoogleCalendarAuthError:
        if not isinstance(auth, dict):
            logger.error("Google FreeBusy error: status_code=401")
            return None

    refreshed = await refresh_access_token(auth, force=True)
    if not refreshed:
        logger.error("Google FreeBusy authorization refresh failed")
        return None

    try:
        return await _freebusy_request(refreshed, body)
    except GoogleCalendarAuthError:
        logger.error("Google FreeBusy error after refresh: status_code=401")
        return None


def _calendar_configuration(auth: str | dict) -> tuple[ZoneInfo, dtime, dtime]:
    if isinstance(auth, dict):
        timezone_name = auth.get("timezone")
        start_value = auth.get("business_hours_start")
        end_value = auth.get("business_hours_end")
        if not timezone_name or not start_value or not end_value:
            raise ValueError("Contractor timezone and business hours are required")
    else:
        timezone_name = DEFAULT_TIMEZONE
        start_value = DEFAULT_BUSINESS_HOURS_START
        end_value = DEFAULT_BUSINESS_HOURS_END

    local_timezone = ZoneInfo(timezone_name)
    business_start = dtime.fromisoformat(start_value)
    business_end = dtime.fromisoformat(end_value)
    if business_start >= business_end:
        raise ValueError("Business hours must be a same-day interval")
    return local_timezone, business_start, business_end


def _parse_provider_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Provider datetime is missing an offset")
    return parsed


def _busy_intervals(data: dict) -> list[tuple[datetime, datetime]]:
    if not isinstance(data, dict):
        raise ValueError("FreeBusy response must be an object")
    calendars = data.get("calendars")
    if not isinstance(calendars, dict):
        raise ValueError("FreeBusy response is missing calendars")
    primary = calendars.get("primary")
    if not isinstance(primary, dict):
        raise ValueError("FreeBusy response is missing the primary calendar")

    errors = primary.get("errors") or []
    if errors:
        error_count = len(errors) if isinstance(errors, list) else 1
        logger.error("Google FreeBusy calendar error: error_count=%s", error_count)
        raise GoogleCalendarUnavailableError("Calendar provider rejected the calendar query")

    busy_periods = primary.get("busy")
    if not isinstance(busy_periods, list):
        raise ValueError("FreeBusy response is missing busy intervals")

    intervals = []
    for period in busy_periods:
        if not isinstance(period, dict):
            raise ValueError("FreeBusy interval must be an object")
        start = _parse_provider_datetime(period["start"])
        end = _parse_provider_datetime(period["end"])
        if end <= start:
            raise ValueError("FreeBusy interval must have positive duration")
        intervals.append((start, end))
    return intervals


async def get_available_slots(auth: str | dict, days_ahead: int = 7) -> list[dict]:
    """Query Google Calendar free/busy and return available 1-hour slots.

    Returns list of dicts: [{"date": "Mon Jan 6", "start": "9:00 AM", "end": "10:00 AM"}, ...]
    """
    try:
        local_timezone, business_start, business_end = _calendar_configuration(auth)
        days = max(1, min(int(days_ahead), MAX_DAYS_AHEAD))
    except Exception as error:
        logger.error(
            "Google Calendar configuration invalid: exception_type=%s",
            type(error).__name__,
        )
        raise GoogleCalendarUnavailableError("Calendar configuration is invalid") from error

    now = _utc_now()
    local_now = now.astimezone(local_timezone)
    first_day = local_now.date() + timedelta(days=1)
    end_day = first_day + timedelta(days=days)
    query_end = datetime.combine(end_day, dtime.min, tzinfo=local_timezone)

    body = {
        "timeMin": now.isoformat(),
        "timeMax": query_end.isoformat(),
        "timeZone": local_timezone.key,
        "items": [{"id": "primary"}],
    }

    try:
        data = await _freebusy_with_refresh(auth, body)
    except Exception as error:
        logger.error(
            "Google FreeBusy request failed: exception_type=%s",
            type(error).__name__,
        )
        raise GoogleCalendarUnavailableError("Calendar provider request failed") from error
    if data is None:
        raise GoogleCalendarUnavailableError("Calendar provider is unavailable")

    try:
        busy = _busy_intervals(data)
    except GoogleCalendarUnavailableError:
        raise
    except Exception as error:
        logger.error(
            "Google FreeBusy response invalid: exception_type=%s",
            type(error).__name__,
        )
        raise GoogleCalendarUnavailableError("Calendar provider response is invalid") from error

    available = []
    for day_offset in range(days):
        local_day = first_day + timedelta(days=day_offset)
        slot_start = datetime.combine(local_day, business_start, tzinfo=local_timezone)
        closing_time = datetime.combine(local_day, business_end, tzinfo=local_timezone)

        while slot_start + timedelta(hours=1) <= closing_time:
            slot_end = slot_start + timedelta(hours=1)
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
            slot_start = slot_end

    return available[:MAX_RETURNED_SLOTS]


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

    logger.error(
        f"Google Calendar create event error: operation=create_event status_code={resp.status_code}"
    )
    return None
