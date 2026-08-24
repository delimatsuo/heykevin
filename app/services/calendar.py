
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
import json
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import Optional
from urllib.parse import quote
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

# Google Calendar private extended-property limits are intentionally enforced
# here as bytes, not Python characters.  The key is owned by Hey Kevin and the
# value is a versioned, bounded JSON payload assembled by the provider adapter.
HEY_KEVIN_SERVICES_PRIVATE_KEY = "hey_kevin_services_v1"
HEY_KEVIN_OPERATION_PRIVATE_KEY = "hey_kevin_operation_id"
MAX_PRIVATE_PROPERTY_VALUE_BYTES = 1_024
MAX_SERVICE_METADATA_ATTEMPTS = 2

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


async def _read_google_calendar_tokens(contractor_id: str) -> Optional[dict]:
    """Read latest stored Google Calendar tokens, decrypting envelopes to memory strings.

    Returns None if reading fails, document is missing, provider is disconnected,
    generation is malformed, or stored refresh token is corrupt/missing.
    """
    from app.services.integration_tokens import (
        MAX_KEY_VERSION,
        determine_write_format,
        resolve_usable_token_pair,
        validate_token_expires_at,
        validate_token_string,
    )

    try:
        valid_cid = validate_token_string(contractor_id, name="contractor_id")
    except Exception:
        return None
    if valid_cid is None:
        return None

    try:
        from app.db.firestore_client import get_firestore_client

        db = get_firestore_client()
        if db is None:
            return None
        loop = asyncio.get_event_loop()
        doc = await loop.run_in_executor(
            None,
            lambda: db.collection("contractors").document(valid_cid).get(),
        )
        if not getattr(doc, "exists", False):
            return None
        data = doc.to_dict() or {}
    except Exception:
        return None

    cur_gen_raw = data.get("google_calendar_generation")
    if cur_gen_raw is None:
        cur_gen = 0
    elif type(cur_gen_raw) is int and type(cur_gen_raw) is not bool and 0 <= cur_gen_raw <= MAX_KEY_VERSION:
        cur_gen = cur_gen_raw
    else:
        return None

    access_token, refresh_token = resolve_usable_token_pair(
        data,
        provider="google_calendar",
        contractor_id=valid_cid,
    )
    if not access_token or not refresh_token:
        return None

    raw_access = data.get("google_calendar_access_token")
    raw_refresh = data.get("google_calendar_refresh_token")

    try:
        determine_write_format(
            contractor_id=valid_cid,
            provider="google_calendar",
            stored_access=raw_access,
            stored_refresh=raw_refresh,
            envelope_required=data.get("google_calendar_token_envelope_required"),
        )
    except Exception:
        return None

    exp = data.get("google_calendar_token_expires_at")
    if exp is not None:
        try:
            validate_token_expires_at(exp)
        except Exception:
            return None

    return {
        "generation": cur_gen,
        "access_token_raw": raw_access,
        "refresh_token_raw": raw_refresh,
        "google_calendar_access_token": access_token,
        "google_calendar_refresh_token": refresh_token,
        "google_calendar_token_expires_at": exp,
        "connected": True,
    }


def _token_expires_soon(contractor: dict, leeway_seconds: int = 120) -> bool:
    """Return True only when we KNOW the stored token is expired/expiring.

    Contractors connected before this field existed have no stored expiry;
    treat that as "unknown", not "expiring" — forcing a refresh on every
    call for them would add latency with no evidence it's needed. The
    401-retry path is what actually protects those contractors.
    """
    expires_at = contractor.get("google_calendar_token_expires_at")
    return (type(expires_at) in (int, float) and type(expires_at) is not bool) and expires_at <= _epoch_now() + leeway_seconds


def _refresh_lock_key(contractor: dict) -> str:
    contractor_id = str(contractor.get("contractor_id", ""))
    if contractor_id:
        return contractor_id
    # Fall back to a hash, not the raw refresh token, as the in-process dict key —
    # avoids a live OAuth secret sitting in something a future debug dump might print.
    refresh_token = str(contractor.get("google_calendar_refresh_token", ""))
    return hashlib.sha256(refresh_token.encode()).hexdigest()[:16]


async def refresh_access_token(contractor: dict, *, force: bool = False) -> str | None:
    """Refresh and persist Google Calendar OAuth tokens for a contractor with durable CAS."""
    from app.config import settings
    from app.services.integration_token_mutations import (
        IntegrationTokenLeaseError,
        acquire_refresh_claim_cas,
        persist_refreshed_tokens_cas,
        release_refresh_claim_cas,
    )
    from app.services.integration_tokens import (
        validate_token_expires_at,
        validate_token_expires_in,
        validate_token_string,
    )

    if type(contractor) is not dict:
        return None

    if "contractor_id" in contractor:
        raw_id = contractor.get("contractor_id")
    elif "id" in contractor:
        raw_id = contractor.get("id")
    else:
        raw_id = None

    try:
        valid_cid = validate_token_string(raw_id, name="contractor_id")
    except Exception:
        return None
    if valid_cid is None:
        return None

    if not settings.google_calendar_client_id:
        return None

    lock = _REFRESH_LOCKS.setdefault(valid_cid, asyncio.Lock())

    async with lock:
        snapshot = await _read_google_calendar_tokens(valid_cid)
        if snapshot is None:
            logger.error(
                "Google Calendar token refresh aborted: durable snapshot invalid or unavailable"
            )
            return None

        current_token = snapshot.get("google_calendar_access_token")
        snapshot_expires_at = snapshot.get("google_calendar_token_expires_at")

        # If durable snapshot is already fresh (a concurrent winner refreshed and committed),
        # hydrate contractor and return the fresh token without making a redundant provider call.
        if (
            current_token
            and not force
            and type(snapshot_expires_at) in (int, float)
            and snapshot_expires_at > _epoch_now() + 120
        ):
            contractor["google_calendar_access_token"] = snapshot.get("access_token_raw")
            contractor["google_calendar_refresh_token"] = snapshot.get("refresh_token_raw")
            contractor["google_calendar_generation"] = snapshot.get("generation", 0)
            if snapshot_expires_at is not None:
                contractor["google_calendar_token_expires_at"] = snapshot_expires_at
            else:
                contractor.pop("google_calendar_token_expires_at", None)
            return current_token

        token_is_expiring = (
            (type(snapshot_expires_at) in (int, float) and snapshot_expires_at <= _epoch_now() + 120)
            or _token_expires_soon(contractor)
        )
        if current_token and not force and not token_is_expiring:
            # Verified winner already committed; hydrate contractor and return
            contractor["google_calendar_access_token"] = snapshot.get("access_token_raw")
            contractor["google_calendar_refresh_token"] = snapshot.get("refresh_token_raw")
            contractor["google_calendar_generation"] = snapshot["generation"]
            if snapshot_expires_at is not None:
                contractor["google_calendar_token_expires_at"] = snapshot_expires_at
            else:
                contractor.pop("google_calendar_token_expires_at", None)
            return current_token

        # Acquire multi-instance cross-process refresh lease before provider HTTP call
        claim_id: Optional[str] = None
        try:
            claim_id, _ = await acquire_refresh_claim_cas(
                contractor_id=valid_cid,
                provider="google_calendar",
                observed_generation=snapshot["generation"],
                observed_access_raw=snapshot["access_token_raw"],
                observed_refresh_raw=snapshot["refresh_token_raw"],
            )
        except IntegrationTokenLeaseError:
            # Contender instance: another worker is actively refreshing this contractor.
            # Re-read durable store: if winner already advanced generation, reload winner's tokens!
            latest_snap = await _read_google_calendar_tokens(valid_cid)
            if latest_snap and latest_snap["generation"] > snapshot["generation"]:
                winner_token = latest_snap.get("google_calendar_access_token")
                if winner_token:
                    contractor["google_calendar_access_token"] = latest_snap.get("access_token_raw")
                    contractor["google_calendar_refresh_token"] = latest_snap.get("refresh_token_raw")
                    contractor["google_calendar_generation"] = latest_snap["generation"]
                    if latest_snap.get("google_calendar_token_expires_at") is not None:
                        contractor["google_calendar_token_expires_at"] = latest_snap["google_calendar_token_expires_at"]
                    else:
                        contractor.pop("google_calendar_token_expires_at", None)
                    return winner_token
            logger.warning("Google Calendar token refresh actively locked by concurrent lease")
            return None
        except Exception:
            logger.error("Failed to acquire Google Calendar refresh lease")
            return None

        try:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        TOKEN_URL,
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": snapshot["google_calendar_refresh_token"],
                            "client_id": settings.google_calendar_client_id,
                            "client_secret": settings.google_calendar_client_secret,
                        },
                        timeout=10.0,
                    )
            except Exception:
                logger.error(
                    "Google token refresh failed: provider=google_calendar operation=refresh result=error"
                )
                return None

            if resp.status_code != 200:
                logger.error(
                    "Google token refresh failed: provider=google_calendar operation=refresh status_code=%s",
                    resp.status_code,
                )
                return None

            try:
                tokens = resp.json()
            except Exception:
                logger.error(
                    "Google token refresh invalid payload: provider=google_calendar operation=refresh result=invalid_json"
                )
                return None

            if type(tokens) is not dict:
                logger.error(
                    "Google token refresh invalid payload: provider=google_calendar operation=refresh result=invalid_type"
                )
                return None

            access_token = tokens.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                logger.error(
                    "Google token refresh returned no access token: provider=google_calendar operation=refresh"
                )
                return None

            try:
                validate_token_string(access_token, name="access_token")
            except Exception:
                logger.error("Google token refresh returned invalid access token")
                return None

            if "refresh_token" in tokens:
                new_refresh_token = tokens["refresh_token"]
                try:
                    validate_token_string(new_refresh_token, name="refresh_token")
                    effective_refresh_token = new_refresh_token
                except Exception:
                    logger.error("Google token refresh returned malformed refresh_token")
                    return None
            else:
                effective_refresh_token = snapshot["google_calendar_refresh_token"]

            token_expires_at = None
            if "expires_in" in tokens and tokens["expires_in"] is not None:
                try:
                    exp_duration = validate_token_expires_in(tokens["expires_in"])
                    if exp_duration is not None:
                        token_expires_at = _epoch_now() + exp_duration
                except Exception:
                    logger.error("Google token refresh invalid expires_in: provider=google_calendar")
                    return None
            elif "expires_at" in tokens and tokens["expires_at"] is not None:
                try:
                    token_expires_at = validate_token_expires_at(tokens["expires_at"])
                except Exception:
                    logger.error("Google token refresh invalid expires_at: provider=google_calendar")
                    return None

            try:
                updates, next_gen = await persist_refreshed_tokens_cas(
                    contractor_id=valid_cid,
                    provider="google_calendar",
                    new_access_token=access_token,
                    new_refresh_token=effective_refresh_token,
                    observed_generation=snapshot["generation"],
                    observed_access_raw=snapshot["access_token_raw"],
                    observed_refresh_raw=snapshot["refresh_token_raw"],
                    expires_at=token_expires_at,
                    claim_id=claim_id,
                )
                claim_id = None  # Cleared by atomic CAS commit!
            except Exception:
                logger.error(
                    "Google Calendar token persistence failed after refresh: provider=google_calendar operation=persist result=error"
                )
                return None

            # Mutate in-memory contractor only on verified durable success
            contractor["google_calendar_access_token"] = updates["google_calendar_access_token"]
            contractor["google_calendar_refresh_token"] = updates["google_calendar_refresh_token"]
            contractor["google_calendar_generation"] = next_gen
            from google.cloud.firestore_v1 import DELETE_FIELD
            if "google_calendar_token_expires_at" in updates and updates["google_calendar_token_expires_at"] is not DELETE_FIELD:
                contractor["google_calendar_token_expires_at"] = updates["google_calendar_token_expires_at"]
            else:
                contractor.pop("google_calendar_token_expires_at", None)

            logger.info("Google Calendar token refreshed for contractor %s", valid_cid[:8] or "unknown")
            return access_token
        finally:
            if claim_id is not None:
                await release_refresh_claim_cas(
                    contractor_id=valid_cid,
                    provider="google_calendar",
                    claim_id=claim_id,
                )


async def _resolve_access_token(contractor: dict) -> str:
    if isinstance(contractor, dict):
        from app.services.integration_tokens import resolve_usable_token

        access_token = resolve_usable_token(
            contractor,
            provider="google_calendar",
            token_kind="access",
        ) or ""

        if access_token and _token_expires_soon(contractor):
            refreshed = await refresh_access_token(contractor)
            if refreshed:
                return refreshed
        return access_token
    return ""


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
        logger.info("Google Calendar event created")
        return created_id

    if _is_duplicate_conflict(resp):
        # Our own earlier insert already created this appointment. Reporting
        # failure here would have Kevin apologise for a booking that is in
        # fact on the calendar.
        logger.info("Google Calendar event already exists, treating as booked")
        return event_id

    logger.error(
        f"Google Calendar create event error: operation=create_event status_code={resp.status_code}"
    )
    return None


async def create_managed_appointment(
    contractor: dict,
    *,
    event_id: str,
    title: str,
    start_time: str,
    end_time: str,
    description: str,
    logical_operation_id: str,
) -> bool:
    """Create, or verify, one provider-saga-owned calendar event.

    The caller supplies the deterministic Google event ID that was durably
    persisted before this function runs. A duplicate is success only when a
    follow-up GET proves it carries our exact logical operation marker and
    schedule; an unrelated 409 never becomes a false booking confirmation.
    """
    event_url = _event_resource_url(event_id)
    if not event_url or not _valid_managed_operation_id(logical_operation_id):
        logger.error("Google Calendar managed create error: invalid operation")
        return False
    if not all(isinstance(value, str) for value in (title, start_time, end_time, description)):
        logger.error("Google Calendar managed create error: invalid event fields")
        return False

    body = {
        "id": event_id,
        "summary": title,
        "description": description,
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
        "extendedProperties": {
            "private": {HEY_KEVIN_OPERATION_PRIVATE_KEY: logical_operation_id}
        },
    }

    async def _insert(token: str):
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

    try:
        response = await _with_token_refresh(contractor, _insert)
    except Exception as error:  # noqa: BLE001 - private provider data stays out of logs
        logger.error(
            "Google Calendar managed create error: operation=insert_event "
            f"exception_type={type(error).__name__}"
        )
        return False
    if response is None:
        logger.error(
            "Google Calendar managed create error: operation=insert_event "
            "reason=no_valid_access_token"
        )
        return False
    if 200 <= response.status_code < 300:
        try:
            returned_id = response.json().get("id")
        except Exception:  # noqa: BLE001 - response body is intentionally never logged
            returned_id = None
        if returned_id != event_id:
            logger.error(
                "Google Calendar managed create error: operation=insert_event "
                "reason=unexpected_resource"
            )
            return False
        logger.info("Google Calendar managed event created")
        return True
    if not _is_duplicate_conflict(response):
        logger.error(
            "Google Calendar managed create error: operation=insert_event "
            f"status_code={response.status_code}"
        )
        return False

    return await _verify_managed_appointment(
        contractor,
        event_url=event_url,
        logical_operation_id=logical_operation_id,
        start_time=start_time,
        end_time=end_time,
    )


def _valid_managed_operation_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


async def _verify_managed_appointment(
    contractor: dict,
    *,
    event_url: str,
    logical_operation_id: str,
    start_time: str,
    end_time: str,
) -> bool:
    async def _get(token: str):
        async with httpx.AsyncClient() as client:
            return await client.get(
                event_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=8.0,
            )

    try:
        response = await _with_token_refresh(contractor, _get)
    except Exception as error:  # noqa: BLE001
        logger.error(
            "Google Calendar managed create error: operation=verify_event "
            f"exception_type={type(error).__name__}"
        )
        return False
    if response is None or not 200 <= response.status_code < 300:
        status = getattr(response, "status_code", "unavailable")
        logger.error(
            "Google Calendar managed create error: operation=verify_event "
            f"status_code={status}"
        )
        return False
    try:
        event = response.json()
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    private = _event_private_properties(event)
    start = event.get("start") if isinstance(event, dict) else None
    end = event.get("end") if isinstance(event, dict) else None
    verified = (
        private is not None
        and private.get(HEY_KEVIN_OPERATION_PRIVATE_KEY) == logical_operation_id
        and isinstance(start, dict)
        and start.get("dateTime") == start_time
        and isinstance(end, dict)
        and end.get("dateTime") == end_time
    )
    if verified:
        logger.info("Google Calendar managed event already exists and was verified")
    else:
        logger.error(
            "Google Calendar managed create error: operation=verify_event "
            "reason=identity_mismatch"
        )
    return verified


def _event_resource_url(event_id: str) -> str:
    """Return the primary-calendar URL for one opaque Google event ID."""
    if not isinstance(event_id, str) or not event_id:
        return ""
    return f"{EVENTS_URL}/{quote(event_id, safe='')}"


async def update_appointment(
    contractor: dict,
    event_id: str,
    start_time: str,
    end_time: str,
    title: str | None = None,
    description: str | None = None,
) -> bool:
    """Update an existing Google Calendar appointment.

    Returns True only after Google accepts the PATCH. Optional title and
    description values are omitted when None, allowing callers to change the
    time without erasing existing event metadata. Empty strings are sent
    deliberately so a caller can clear either field.
    """
    event_url = _event_resource_url(event_id)
    if not event_url:
        logger.error("Google Calendar update event error: invalid event id")
        return False

    body = {
        "start": {"dateTime": start_time},
        "end": {"dateTime": end_time},
    }
    if title is not None:
        body["summary"] = title
    if description is not None:
        body["description"] = description

    async def _call(token: str):
        async with httpx.AsyncClient() as client:
            return await client.patch(
                event_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=8.0,
            )

    try:
        resp = await _with_token_refresh(contractor, _call)
    except Exception as error:  # noqa: BLE001
        logger.error(
            "Google Calendar update event error: operation=update_event "
            f"exception_type={type(error).__name__}"
        )
        return False

    if resp is None:
        logger.error(
            "Google Calendar update event error: operation=update_event "
            "reason=no_valid_access_token"
        )
        return False
    if 200 <= resp.status_code < 300:
        logger.info("Google Calendar event updated")
        return True

    # Status only: provider bodies can contain private event details.
    logger.error(
        "Google Calendar update event error: operation=update_event "
        f"status_code={resp.status_code}"
    )
    return False


def _event_private_properties(event: object) -> dict[str, str] | None:
    """Return a copy of an event's private properties without coercing data."""
    if not isinstance(event, dict):
        return None
    extended_properties = event.get("extendedProperties", {})
    if not isinstance(extended_properties, dict):
        return None
    private_properties = extended_properties.get("private", {})
    if not isinstance(private_properties, dict):
        return None
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in private_properties.items()
    ):
        return None
    return dict(private_properties)


def _event_etag(event: object) -> str | None:
    if not isinstance(event, dict):
        return None
    etag = event.get("etag")
    if (
        not isinstance(etag, str)
        or not etag
        or len(etag) > 1_024
        or any(ord(character) < 32 or ord(character) == 127 for character in etag)
    ):
        return None
    return etag


async def set_appointment_service_metadata(
    contractor: dict,
    event_id: str,
    metadata_value: str,
) -> bool:
    """Merge Hey Kevin's owned service metadata into an existing event.

    The current event is fetched before every conditional PATCH. Existing
    private properties are copied into the PATCH body so Kevin cannot erase
    another integration's metadata. No schedule, title, description, or other
    event field is sent. A concurrent edit (HTTP 412) gets one bounded
    refetch/merge retry. An exact semantic replay succeeds after the GET
    without another write.
    """
    event_url = _event_resource_url(event_id)
    if not event_url:
        logger.error("Google Calendar service metadata error: invalid event id")
        return False
    if (
        not isinstance(metadata_value, str)
        or not metadata_value
        or len(metadata_value.encode("utf-8")) > MAX_PRIVATE_PROPERTY_VALUE_BYTES
        or any(ord(character) < 32 for character in metadata_value)
    ):
        logger.error("Google Calendar service metadata error: invalid metadata value")
        return False

    async def _get(token: str):
        async with httpx.AsyncClient() as client:
            return await client.get(
                event_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=8.0,
            )

    for attempt in range(MAX_SERVICE_METADATA_ATTEMPTS):
        try:
            get_response = await _with_token_refresh(contractor, _get)
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Google Calendar service metadata error: operation=get_event "
                f"exception_type={type(error).__name__}"
            )
            return False

        if get_response is None:
            logger.error(
                "Google Calendar service metadata error: operation=get_event "
                "reason=no_valid_access_token"
            )
            return False
        if not 200 <= get_response.status_code < 300:
            logger.error(
                "Google Calendar service metadata error: operation=get_event "
                f"status_code={get_response.status_code}"
            )
            return False

        try:
            event = get_response.json()
        except Exception:  # noqa: BLE001 - response body is intentionally never logged
            logger.error(
                "Google Calendar service metadata error: operation=get_event "
                "reason=invalid_response"
            )
            return False
        private_properties = _event_private_properties(event)
        etag = _event_etag(event)
        if private_properties is None or etag is None:
            logger.error(
                "Google Calendar service metadata error: operation=get_event "
                "reason=invalid_response"
            )
            return False

        if private_properties.get(HEY_KEVIN_SERVICES_PRIVATE_KEY) == metadata_value:
            logger.info("Google Calendar service metadata already current")
            return True

        private_properties[HEY_KEVIN_SERVICES_PRIVATE_KEY] = metadata_value
        body = {"extendedProperties": {"private": private_properties}}

        async def _patch(token: str):
            async with httpx.AsyncClient() as client:
                return await client.patch(
                    event_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "If-Match": etag,
                    },
                    json=body,
                    timeout=8.0,
                )

        try:
            patch_response = await _with_token_refresh(contractor, _patch)
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Google Calendar service metadata error: operation=patch_event "
                f"exception_type={type(error).__name__}"
            )
            return False
        if patch_response is None:
            logger.error(
                "Google Calendar service metadata error: operation=patch_event "
                "reason=no_valid_access_token"
            )
            return False
        if 200 <= patch_response.status_code < 300:
            logger.info("Google Calendar service metadata updated")
            return True
        if patch_response.status_code == 412 and attempt + 1 < MAX_SERVICE_METADATA_ATTEMPTS:
            continue

        logger.error(
            "Google Calendar service metadata error: operation=patch_event "
            f"status_code={patch_response.status_code}"
        )
        return False

    return False


async def cancel_appointment(contractor: dict, event_id: str) -> bool:
    """Delete an existing Google Calendar appointment.

    Returns True only after Google confirms the deletion with a successful
    response. The opaque event ID is URL-encoded and never written to logs.
    """
    event_url = _event_resource_url(event_id)
    if not event_url:
        logger.error("Google Calendar cancel event error: invalid event id")
        return False

    async def _call(token: str):
        async with httpx.AsyncClient() as client:
            return await client.delete(
                event_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=8.0,
            )

    try:
        resp = await _with_token_refresh(contractor, _call)
    except Exception as error:  # noqa: BLE001
        logger.error(
            "Google Calendar cancel event error: operation=cancel_event "
            f"exception_type={type(error).__name__}"
        )
        return False

    if resp is None:
        logger.error(
            "Google Calendar cancel event error: operation=cancel_event "
            "reason=no_valid_access_token"
        )
        return False
    if 200 <= resp.status_code < 300:
        logger.info("Google Calendar event cancelled")
        return True
    if resp.status_code == 410:
        # Google documents 410/deleted for an event that has already been
        # deleted. Treat that as an idempotent success so a retry after a lost
        # response can complete our pending canonical operation.
        logger.info("Google Calendar event was already cancelled")
        return True

    # Status only: provider bodies can contain private event details.
    logger.error(
        "Google Calendar cancel event error: operation=cancel_event "
        f"status_code={resp.status_code}"
    )
    return False
