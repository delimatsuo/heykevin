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

Availability is computed in the contractor's own timezone and business
hours (`timezone`, `business_hours_start`, `business_hours_end` on the
contractor document). This is a deliberate behaviour change: the previous
implementation generated slots at a hardcoded UTC 9-5 and *labelled* them
as local time, so a contractor in America/Los_Angeles had callers offered
"9:00 AM" for what was really 01:00 local. Most contractors do have these
fields set — `ContractorCreate` has defaulted them since 2026-04-08, and
device registration (app/api/voip.py) writes the handset's IANA timezone
on every app launch — so this corrects quoted appointment times rather
than being the no-op it would be if nothing populated them.

An explicitly-set but malformed value (bad IANA zone name, inverted
hours) fails closed rather than guessing; a value that's simply absent
falls back to the UTC 9-5 default rather than breaking availability for
a contractor who has never configured it.

`zoneinfo.ZoneInfo` needs an IANA timezone database to resolve zone
names, and Cloud Run's base image doesn't reliably ship a system one —
the `tzdata` PyPI package bundles it so this doesn't depend on the host.
"""

from __future__ import annotations

import asyncio
import base64
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

MAX_DAYS_AHEAD = 14
MAX_RETURNED_SLOTS = 20
# Bound slots per day BEFORE the overall cap. Without this, an open calendar
# fills MAX_RETURNED_SLOTS with the earliest ~2.5 days of hourly slots and
# later days never reach the model — on live call CAb4533d Kevin could not
# offer a Tuesday five days out because the tool never showed him one.
MAX_SLOTS_PER_DAY = 3
DEFAULT_TIMEZONE = "UTC"
DEFAULT_BUSINESS_HOURS_START = "09:00"
DEFAULT_BUSINESS_HOURS_END = "17:00"

_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch_now() -> float:
    return time.time()


class GoogleCalendarUnavailableError(Exception):
    """Raised when availability can't be determined reliably.

    Distinguishes "the integration is broken" from "no slots are free" —
    both used to surface identically as an empty list, which let a dead
    integration look like a fully-booked calendar to the caller.
    """


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
    return isinstance(expires_at, (int, float)) and expires_at <= _epoch_now() + leeway_seconds


def _refresh_lock_key(contractor: dict) -> str:
    contractor_id = str(contractor.get("contractor_id", ""))
    if contractor_id:
        return contractor_id
    # Fall back to a hash, not the raw refresh token, as the in-process dict key —
    # avoids a live OAuth secret sitting in something a future debug dump might print.
    refresh_token = str(contractor.get("google_calendar_refresh_token", ""))
    return hashlib.sha256(refresh_token.encode()).hexdigest()[:16]


async def refresh_access_token(contractor: dict, *, force: bool = False) -> str | None:
    """Refresh and persist Google Calendar OAuth tokens for a contractor.

    Mutates `contractor` in place with the refreshed values (mirrors
    app/services/jobber.py::refresh_access_token) so callers holding the
    same dict see the fresh token without re-reading Firestore.
    """
    from app.config import settings

    lock = _REFRESH_LOCKS.setdefault(_refresh_lock_key(contractor), asyncio.Lock())

    async with lock:
        stale_token = contractor.get("google_calendar_access_token", "")
        contractor_id = contractor.get("contractor_id", "")
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
                logger.error(f"Google token refresh failed: status_code={resp.status_code}")
                return None

            tokens = resp.json()
            access_token = tokens.get("access_token", "")
            if not access_token:
                logger.error("Google token refresh returned no access token")
                return None
            # Google typically omits refresh_token on refresh (the original stays valid).
            new_refresh_token = tokens.get("refresh_token") or refresh_token
            expires_in = tokens.get("expires_in", 3300)

            updates = {
                "google_calendar_access_token": access_token,
                "google_calendar_refresh_token": new_refresh_token,
                "google_calendar_token_expires_at": _epoch_now() + expires_in,
                "google_calendar_token_refreshed_at": _epoch_now(),
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


def _calendar_configuration(contractor: dict) -> tuple[ZoneInfo, dtime, dtime]:
    """Resolve the timezone + business hours to compute availability in.

    Falls back to UTC 9-5 only when nothing is configured, which is rare —
    see the module docstring for what populates these fields. Fails closed
    on a value that IS set and doesn't parse: guessing past a garbage
    timezone risks quoting slots in the wrong hours entirely, which is
    worse than refusing.
    """
    timezone_name = contractor.get("timezone") or DEFAULT_TIMEZONE
    start_value = contractor.get("business_hours_start") or DEFAULT_BUSINESS_HOURS_START
    end_value = contractor.get("business_hours_end") or DEFAULT_BUSINESS_HOURS_END

    local_timezone = ZoneInfo(timezone_name)
    business_start = dtime.fromisoformat(start_value)
    business_end = dtime.fromisoformat(end_value)
    if business_start >= business_end:
        raise ValueError("Business hours must be a same-day interval")
    return local_timezone, business_start, business_end


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
        logger.error(f"Google FreeBusy calendar error: error_count={error_count}")
        raise GoogleCalendarUnavailableError("Calendar provider rejected the calendar query")

    busy_periods = primary.get("busy")
    if not isinstance(busy_periods, list):
        raise ValueError("FreeBusy response is missing busy intervals")

    intervals = []
    for period in busy_periods:
        start = datetime.fromisoformat(period["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(period["end"].replace("Z", "+00:00"))
        intervals.append((start, end))
    return intervals


async def get_available_slots(contractor: dict, days_ahead: int = 7) -> list[dict]:
    """Query Google Calendar free/busy and return available 1-hour slots.

    `contractor` is the contractor's Firestore-backed config dict (must
    contain google_calendar_access_token / _refresh_token, and ideally
    contractor_id + google_calendar_token_expires_at). Refreshes the
    access token first if it's known to be expiring, and retries once
    more on a 401 for contractors whose expiry isn't tracked yet.

    Raises GoogleCalendarUnavailableError when availability genuinely
    can't be determined (bad config, provider failure/error, malformed
    response) — never returns [] to mean "the integration is broken",
    only to mean "no open slots in the window".
    """
    try:
        local_timezone, business_start, business_end = _calendar_configuration(contractor)
    except Exception as e:
        # Exception text intentionally omitted: an invalid timezone name (contractor
        # input) or ZoneInfoNotFoundError embeds the raw offending value in its message.
        logger.error(f"Google Calendar configuration invalid: {type(e).__name__}")
        raise GoogleCalendarUnavailableError("Calendar configuration is invalid") from e

    days = max(1, min(int(days_ahead), MAX_DAYS_AHEAD))
    now = _utc_now()
    local_now = now.astimezone(local_timezone)
    first_day = local_now.date() + timedelta(days=1)
    query_end = datetime.combine(first_day + timedelta(days=days), dtime.min, tzinfo=local_timezone)

    body = {
        "timeMin": now.isoformat(),
        "timeMax": query_end.isoformat(),
        "timeZone": local_timezone.key,
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
        raise GoogleCalendarUnavailableError("No valid Google Calendar access token")
    if resp.status_code != 200:
        # Status only — resp.text is the provider's raw response body and may
        # contain calendar/event details that don't belong in application logs.
        logger.error(f"Google FreeBusy error: status_code={resp.status_code}")
        raise GoogleCalendarUnavailableError("Calendar provider request failed")

    try:
        busy = _busy_intervals(resp.json())
    except GoogleCalendarUnavailableError:
        raise
    except Exception as e:
        logger.error(f"Google FreeBusy response invalid: {type(e).__name__}")
        raise GoogleCalendarUnavailableError("Calendar provider response is invalid") from e

    available = []
    for day_offset in range(days):
        local_day = first_day + timedelta(days=day_offset)
        slot_start = datetime.combine(local_day, business_start, tzinfo=local_timezone)
        closing_time = datetime.combine(local_day, business_end, tzinfo=local_timezone)

        day_slots = 0
        while (
            slot_start + timedelta(hours=1) <= closing_time
            and day_slots < MAX_SLOTS_PER_DAY
        ):
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
                day_slots += 1
            slot_start = slot_end

    return available[:MAX_RETURNED_SLOTS]


def _deterministic_event_id(
    contractor_id: str,
    call_sid: str,
    title: str,
    start_time: str,
    end_time: str,
) -> str:
    """Derive a stable Google event id for one intended appointment.

    Keyed on the appointment's content, not just the call: the voice gate's
    own idempotency key is `f"{call_sid}:{action}"`, constant for a whole
    call, so deriving the id from that alone would make a caller's second
    genuine booking collide with their first and vanish as a "duplicate".

    Google requires base32hex (digits 0-9 and lowercase a-v) and a length
    between 5 and 1024 characters.
    """
    seed = "|".join([contractor_id, call_sid, title, start_time, end_time]).encode()
    digest = hashlib.sha256(seed).digest()
    return base64.b32hexencode(digest).decode().lower().rstrip("=")[:32]


def _is_duplicate_conflict(resp) -> bool:
    """True only for Google's duplicate-identifier 409.

    A 409 can also mean a genuine scheduling conflict, which must stay a
    failure — laundering that into success would report a booking that
    was never made.
    """
    if resp.status_code != 409:
        return False
    try:
        errors = resp.json().get("error", {}).get("errors", [])
    except Exception:
        return False
    return any(
        isinstance(item, dict) and item.get("reason") == "duplicate" for item in errors
    )


async def book_appointment(
    contractor: dict,
    title: str,
    start_time: str,
    end_time: str,
    description: str = "",
    call_sid: str = "",
) -> str | None:
    """Create a Google Calendar event. Returns event ID or None on failure.

    `contractor` is the contractor's config dict (see get_available_slots).
    start_time / end_time should be ISO 8601 strings.

    When `call_sid` is supplied we send a deterministic event id so Google
    enforces uniqueness per calendar: a retried tool call answers 409
    /"duplicate" and resolves to the existing appointment instead of
    double-booking the contractor. Nothing else in this codebase persists
    a durable booking claim — `check_gated_action` only asserts that an
    idempotency key is non-empty, it never compares one.

    Google notes it "cannot guarantee that ID collisions will be detected
    at event creation time", so treat this as strong best-effort
    protection rather than an absolute guarantee.
    """
    body = {
        "summary": title,
        "description": description,
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
    }

    event_id = ""
    if call_sid:
        event_id = _deterministic_event_id(
            contractor.get("contractor_id", ""), call_sid, title, start_time, end_time
        )
        body["id"] = event_id

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
        created_id = resp.json().get("id", "")
        logger.info(f"Google Calendar event created: {created_id}")
        return created_id

    if _is_duplicate_conflict(resp):
        # Our own earlier insert already created this appointment. Reporting
        # failure here would have Kevin apologise for a booking that is in
        # fact on the calendar.
        logger.info(
            f"Google Calendar event already exists, treating as booked: {event_id}"
        )
        return event_id

    logger.error(
        f"Google Calendar create event error: operation=create_event status_code={resp.status_code}"
    )
    return None
