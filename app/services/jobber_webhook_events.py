"""Authenticated Jobber disconnect webhook event parser and envelope primitive.

Primary source:
https://developer.getjobber.com/docs/using_jobbers_api/setting_up_webhooks/

Security and architectural invariants:
- Unmounted envelope primitive: authenticates and extracts Jobber APP_DISCONNECT
  webhook payloads into an immutable, frozen data structure.
- Caller responsibility:
  1. The caller (HTTP transport layer) MUST enforce request body size limits
     (1..65536 bytes) while streaming network input BEFORE buffering into memory.
  2. The caller MUST perform separate account routing, grant applicability
     verification, durable transaction/reauth revocation lifecycle management,
     and Marketplace uninstallation workflows.
- Authenticate-before-parse: verifies cryptographic HMAC-SHA256 signature against
  the configured OAuth client secret before any JSON parsing occurs.
- Rejection safety: all malformed inputs, signature mismatches, schema errors,
  unsupported topics, duplicate keys, nonfinite constants, or timestamp anomalies
  return None safely with zero logging and zero exception leakage.
- Strict RFC 3339 timestamp validation: preserves the original validated timestamp
  string as `occurred_at` without loss of sub-second precision.
- Delivery fingerprint: SHA-256 digest of exact raw body bytes identifying identical
  raw wire deliveries. It is NOT a semantic deduplication proof or provider event ID.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any
import unicodedata

from app.services.jobber_webhook_auth import verify_jobber_webhook_signature

MAX_ID_CODE_POINTS: int = 1024
MIN_ID_CODE_POINTS: int = 1
MAX_JSON_DEPTH: int = 10
SUPPORTED_TOPIC: str = "APP_DISCONNECT"

# RFC 3339: 4-digit year, 2-digit month, 2-digit day, T, 2-digit hour, 2-digit minute, 2-digit second,
# optional 1..9 fractional digits, and Z or +/-HH:MM timezone offset.
_RFC3339_REGEX = re.compile(
    r"\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])[Tt]"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class JobberDisconnectEnvelope:
    """Authenticated and validated Jobber APP_DISCONNECT webhook envelope."""

    app_id: str
    account_id: str
    item_id: str | None
    occurred_at: str
    delivery_fingerprint: str


def _is_valid_id(value: Any) -> bool:
    """Validate opaque ID string.

    Must be exact str, 1..1024 Unicode code points, containing no whitespace
    or Unicode control category Cc characters.
    """
    if type(value) is not str:
        return False
    if not (MIN_ID_CODE_POINTS <= len(value) <= MAX_ID_CODE_POINTS):
        return False
    for c in value:
        if c.isspace() or unicodedata.category(c) == "Cc":
            return False
    return True


def _reject_nonfinite(constant: str) -> Any:
    """Reject nonfinite JSON constants (NaN, Infinity, -Infinity)."""
    raise ValueError(f"Forbidden nonfinite JSON constant: {constant}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys at any object depth."""
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"Duplicate JSON key: {key}")
        obj[key] = value
    return obj


def _check_depth(obj: Any, current_depth: int = 1) -> bool:
    """Ensure JSON object nesting does not exceed MAX_JSON_DEPTH."""
    if current_depth > MAX_JSON_DEPTH:
        return False
    if type(obj) is dict:
        for v in obj.values():
            if not _check_depth(v, current_depth + 1):
                return False
    elif type(obj) is list:
        for item in obj:
            if not _check_depth(item, current_depth + 1):
                return False
    return True


def _validate_rfc3339_timestamp(value: Any) -> str | None:
    """Validate RFC 3339 timestamp format and calendar validity.

    Requires 4-digit year, date, T, hour:minute:second, optional 1..9 fractional
    digits, and explicit Z or +/-HH:MM timezone offset. Rejects absent timezone
    and impossible dates (e.g. Feb 29 on non-leap years).
    Returns the original string unchanged on success, or None on failure.
    """
    if type(value) is not str:
        return None
    if not _RFC3339_REGEX.fullmatch(value):
        return None

    try:
        from datetime import datetime, timedelta, timezone

        year = int(value[0:4])
        month = int(value[5:7])
        day = int(value[8:10])
        hour = int(value[11:13])
        minute = int(value[14:16])
        second = int(value[17:19])

        # datetime validates calendar bounds (leap years, month day ranges)
        datetime(year, month, day, hour, minute, second)

        # Validate numeric timezone offset if present
        tz_part = value[19:]
        idx = max(tz_part.rfind("+"), tz_part.rfind("-"))
        if idx != -1:
            tz_str = tz_part[idx:]
            sign = 1 if tz_str[0] == "+" else -1
            tz_hour = int(tz_str[1:3])
            tz_min = int(tz_str[4:6])
            if not (0 <= tz_hour <= 23 and 0 <= tz_min <= 59):
                return None
            timezone(sign * timedelta(hours=tz_hour, minutes=tz_min))
        elif not (value.endswith("Z") or value.endswith("z")):
            return None
    except (ValueError, OverflowError):
        return None

    return value


def authenticate_jobber_disconnect_envelope(
    raw_body: Any,
    signature: Any,
    client_secret: Any,
    expected_app_id: Any,
) -> JobberDisconnectEnvelope | None:
    """Authenticate and parse a Jobber APP_DISCONNECT webhook event.

    Args:
        raw_body: Exact raw request payload bytes.
        signature: The X-Jobber-Hmac-SHA256 header string.
        client_secret: Configured OAuth client secret string.
        expected_app_id: Configured Jobber application ID.

    Returns:
        JobberDisconnectEnvelope on valid authentication and schema match;
        None on any authentication failure, schema mismatch, or malformed input.
    """
    try:
        # 1. Require a valid nonempty configured expected_app_id first
        if not _is_valid_id(expected_app_id):
            return None

        # 2. Authenticate exact raw bytes with HMAC primitive before any JSON parsing
        if not verify_jobber_webhook_signature(
            raw_body=raw_body,
            signature=signature,
            client_secret=client_secret,
        ):
            return None

        # 3. Reject BOM and malformed UTF-8
        if raw_body.startswith(b"\xef\xbb\xbf"):
            return None

        try:
            decoded_body = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            return None

        if decoded_body.startswith("\ufeff"):
            return None

        # 4. Parse JSON with strict duplicate-key rejection and nonfinite constant rejection
        try:
            parsed = json.loads(
                decoded_body,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except (ValueError, TypeError, RecursionError):
            return None

        # 5. Check object nesting depth
        if not _check_depth(parsed, 1):
            return None

        # 6. Validate JSON payload structure: data.webHookEvent
        if type(parsed) is not dict:
            return None

        data = parsed.get("data")
        if type(data) is not dict:
            return None

        webhook_event = data.get("webHookEvent")
        if type(webhook_event) is not dict:
            return None

        # 7. Topic must be exact string APP_DISCONNECT
        topic = webhook_event.get("topic")
        if type(topic) is not str or topic != SUPPORTED_TOPIC:
            return None

        # 8. appId must be valid ID and equal to configured expected_app_id
        app_id = webhook_event.get("appId")
        if type(app_id) is not str or not _is_valid_id(app_id) or app_id != expected_app_id:
            return None

        # 9. accountId must be valid opaque ID
        account_id = webhook_event.get("accountId")
        if type(account_id) is not str or not _is_valid_id(account_id):
            return None

        # 10. itemId is optional, null, or a valid ID
        if "itemId" in webhook_event:
            raw_item_id = webhook_event["itemId"]
            if raw_item_id is None:
                item_id = None
            elif type(raw_item_id) is str and _is_valid_id(raw_item_id):
                item_id = raw_item_id
            else:
                return None
        else:
            item_id = None

        # 11. occurredAt must be an exact valid RFC3339 string (reject legacy occuredAt)
        occurred_at_raw = webhook_event.get("occurredAt")
        if type(occurred_at_raw) is not str:
            return None

        occurred_at = _validate_rfc3339_timestamp(occurred_at_raw)
        if occurred_at is None:
            return None

        # 12. Return envelope with delivery fingerprint
        return JobberDisconnectEnvelope(
            app_id=app_id,
            account_id=account_id,
            item_id=item_id,
            occurred_at=occurred_at,
            delivery_fingerprint=hashlib.sha256(raw_body).hexdigest().lower(),
        )
    except Exception:
        return None
