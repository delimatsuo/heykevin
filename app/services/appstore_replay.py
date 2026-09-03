"""Owner-run replay of missed App Store Server Notifications.

Every Apple notification was answered HTTP 400 for 30+ days (fixed by PR #215
on 2026-09-02). Apple retries a failed delivery for 72 hours only, so any
renewal, expiration, cancellation or refund that fired before the fix never
reached Firestore. Apple's Get Notification History endpoint keeps 180 days
of history regardless of delivery outcome, which lets us recover from that
window after the fact.

All the logic lives here so it is unit-testable; scripts/replay_appstore_notifications.py
is a thin CLI wrapper around ``fetch_notification_history`` and ``replay``.

Nothing here holds an original_transaction_id, appAccountToken, email, or
phone number in a summary — see ``summarize()``.

IMPORTANT — applying is NOT a notification-level dedupe. ``claim_transaction``
(inside the handler) is an ownership *binding*: once a contractor already
owns a given original_transaction_id, a repeat notification for it is a
same-contractor no-op for the binding itself, but the handler still runs its
full state-transition logic every time it is called — including an
unconditional ``update_contractor(status="expired")`` and a re-sent "your
subscription has ended" push for EXPIRED/DID_FAIL_TO_RENEW/REFUND/REVOKE.
Replaying an old deactivating notification for a customer who has since
renewed would otherwise flip them back to expired. ``replay()`` guards
against exactly that with a stale-deactivation check (see
``_is_stale_deactivation``) — but only for that one regression. Applying is
still not safe to run repeatedly as a matter of course: run --apply once per
incident, review the totals, and stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from app.db.contractors import get_contractor_by_subscription_uuid
from app.services.subscription import _get_appstore_jwt
from app.webhooks.appstore import _decode_notification_payload
from app.services.subscription import handle_appstore_notification
from app.utils.logging import get_logger

logger = get_logger(__name__)

NOTIFICATION_HISTORY_PATH = "/inApps/v1/notifications/history"

# At most six entries per Apple's documented response shape.
_MAX_SEND_ATTEMPTS = 6

# Default cap on pages fetched per fetch_notification_history() call. Apple
# returns up to 20 records per page, so 200 pages covers up to 4,000
# notifications in one run before we refuse to keep paging silently.
_DEFAULT_MAX_PAGES = 200

# Notification types whose effect is to deactivate a subscription. Only
# these are ever eligible for the stale-deactivation guard in replay().
# REFUND_REVERSED is deliberately excluded -- it re-activates, not
# deactivates, so replaying an old one is not the regression this guards
# against.
_DEACTIVATING_NOTIFICATION_TYPES = frozenset(
    {"EXPIRED", "DID_FAIL_TO_RENEW", "GRACE_PERIOD_EXPIRED", "REFUND", "REVOKE"}
)


@dataclass
class HistoryItem:
    """One record from Apple's Get Notification History response."""

    signed_payload: str
    send_attempts: list[dict]


async def fetch_notification_history(
    *,
    base_url: str,
    start_ms: int,
    end_ms: int,
    only_failures: bool,
    token_factory: Callable[[], str] = _get_appstore_jwt,
    client: Optional[httpx.AsyncClient] = None,
    max_pages: int = _DEFAULT_MAX_PAGES,
) -> list[HistoryItem]:
    """Page through Apple's Get Notification History endpoint.

    POSTs to ``{base_url}/inApps/v1/notifications/history`` with
    ``startDate``/``endDate`` (integer ms since epoch) and ``onlyFailures``,
    carrying ``paginationToken`` as a query parameter on every page after the
    first. Raises RuntimeError on any non-200 response; the caller receives
    no partial result in that case (nothing is returned until every page
    has succeeded).

    Bounded to ``max_pages`` pages (default 200): raises RuntimeError if
    Apple keeps reporting ``hasMore`` past that, and stops (returning what
    was collected so far) if a ``paginationToken`` repeats, rather than
    paging forever.
    """
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=30.0)
    items: list[HistoryItem] = []
    try:
        pagination_token: Optional[str] = None
        seen_tokens: set[str] = set()
        for _page in range(max_pages):
            body: dict[str, Any] = {
                "startDate": start_ms,
                "endDate": end_ms,
                "onlyFailures": only_failures,
            }
            params = {"paginationToken": pagination_token} if pagination_token else None
            token = token_factory()
            response = await http_client.post(
                f"{base_url}{NOTIFICATION_HISTORY_PATH}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Get Notification History failed: HTTP {response.status_code} "
                    f"{response.text[:200]}"
                )
            data = response.json()
            for record in data.get("notificationHistory", []) or []:
                items.append(
                    HistoryItem(
                        signed_payload=str(record.get("signedPayload", "")),
                        # Keep the most recent attempts, not the earliest ones.
                        send_attempts=list(record.get("sendAttempts") or [])[-_MAX_SEND_ATTEMPTS:],
                    )
                )
            if not data.get("hasMore"):
                return items
            next_token = data.get("paginationToken")
            if not next_token or next_token in seen_tokens:
                # No usable token to continue with, or Apple repeated one --
                # stop rather than loop forever.
                return items
            seen_tokens.add(next_token)
            pagination_token = next_token
        raise RuntimeError(
            f"Get Notification History exceeded max_pages={max_pages}; aborting pagination"
        )
    finally:
        if owns_client:
            await http_client.aclose()


def _signed_date_iso(ms: Any) -> str:
    """Render Apple's signedDate (ms since epoch) as ISO-8601 UTC, or "" if unusable."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return ""
    try:
        return (
            datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _decode_data_signed_field(data: dict, key: str) -> Optional[dict]:
    """Best-effort payload-only unwrap of one signed field under ``data``.

    No signature check here (the outer envelope is already verified by
    ``verify()`` before this ever runs) — this only base64-decodes the
    payload segment via ``_decode_jws_payload``. Returns None if the field
    is absent, not a string, or fails to decode.
    """
    from app.services.subscription import _decode_jws_payload

    if not isinstance(data, dict):
        return None
    signed = data.get(key)
    if not isinstance(signed, str) or not signed:
        return None
    decoded = _decode_jws_payload(signed)
    return decoded if isinstance(decoded, dict) else None


def _original_transaction_suffix(data: dict) -> str:
    """Best-effort last-6-chars of originalTransactionId, never the full id.

    originalTransactionId is nested inside the (separately signed)
    signedTransactionInfo/signedRenewalInfo JWS carried in the notification's
    ``data`` object, not at the top level.
    """
    for key in ("signedTransactionInfo", "signedRenewalInfo"):
        decoded = _decode_data_signed_field(data, key)
        if decoded is None:
            continue
        original_id = decoded.get("originalTransactionId")
        if isinstance(original_id, str) and original_id:
            return original_id[-6:]
    return ""


async def _is_stale_deactivation(
    payload: dict,
    *,
    lookup: Callable[[str], Any],
) -> bool:
    """True iff a deactivating notification's term is already superseded.

    Only ever evaluated for ``_DEACTIVATING_NOTIFICATION_TYPES``. Every
    ambiguous or unreadable case — wrong notification type, no usable
    ``data.signedTransactionInfo``, missing/invalid ``appAccountToken`` or
    ``expiresDate``, a raising or None-returning ``lookup``, or a
    non-numeric/bool ``subscription_expires`` on the looked-up contractor —
    returns False. This guard only ever *prevents* an apply; it never forces
    one, so every ambiguous case defers to the handler exactly as before
    this guard existed.

    Stale means: the contractor's current ``subscription_expires`` (unix
    seconds) already ends *after* the term this notification is about
    (``expiresDate`` ms / 1000, +1s of slack for rounding) — i.e. a later
    renewal is already on file, so this old deactivation must not be
    replayed on top of it.
    """
    if payload.get("notificationType") not in _DEACTIVATING_NOTIFICATION_TYPES:
        return False

    data = payload.get("data")
    transaction_info = _decode_data_signed_field(data, "signedTransactionInfo")
    if transaction_info is None:
        return False

    app_account_token = transaction_info.get("appAccountToken")
    expires_ms = transaction_info.get("expiresDate")
    if not isinstance(app_account_token, str) or not app_account_token:
        return False
    if isinstance(expires_ms, bool) or not isinstance(expires_ms, (int, float)):
        return False

    try:
        contractor = await lookup(app_account_token)
    except Exception as exc:  # noqa: BLE001 - ambiguous outcome, never blocks on this
        logger.warning(
            "appstore_replay stale-check lookup failed (%s) -- not skipping",
            type(exc).__name__,
        )
        return False
    if not isinstance(contractor, dict):
        return False

    stored_expires = contractor.get("subscription_expires")
    if isinstance(stored_expires, bool) or not isinstance(stored_expires, (int, float)):
        return False

    return float(stored_expires) > (float(expires_ms) / 1000.0 + 1.0)


def summarize(payload: dict, attempts: list[dict]) -> dict:
    """PII-free one-line summary of a verified notification.

    Fields: type, subtype, signed_date_iso, environment,
    original_transaction_suffix (last 6 chars only), attempts (count),
    last_result. Never includes the full transaction id, appAccountToken,
    or any email/phone.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}

    last_result = None
    if attempts:
        last_result = attempts[-1].get("sendAttemptResult")

    return {
        "type": str(payload.get("notificationType") or ""),
        "subtype": str(payload.get("subtype") or ""),
        "signed_date_iso": _signed_date_iso(payload.get("signedDate")),
        "environment": str(data.get("environment") or ""),
        "original_transaction_suffix": _original_transaction_suffix(data),
        "attempts": len(attempts),
        "last_result": last_result,
    }


def format_summary_line(summary: dict) -> str:
    """Render one summary dict as a single printable line. PII-free."""
    return (
        f"{summary['signed_date_iso'] or '(no signedDate)':<25} "
        f"{summary['type']:<24} {summary['subtype']:<22} "
        f"env={summary['environment']:<10} "
        f"orig=...{summary['original_transaction_suffix']:<6} "
        f"attempts={summary['attempts']} last={summary['last_result']}"
    )


@dataclass
class ReplayReport:
    """Aggregate outcome of a replay run. No per-item PII, counts only."""

    fetched: int = 0
    rejected: int = 0  # verifier refused the signedPayload
    stale_skipped: int = 0  # deactivation superseded by a later paid term on file
    dry_run: int = 0  # would apply (dry-run mode only; excludes stale_skipped)
    applied: int = 0  # handler returned True
    handler_false: int = 0  # handler returned False
    handler_error: int = 0  # handler raised
    by_type: dict[str, int] = field(default_factory=dict)


def _sort_key(entry: tuple[int, dict, list[dict]]) -> tuple[int, float, int]:
    """Ascending by signedDate; missing/invalid signedDate sorts first, stable in input order."""
    idx, payload, _attempts = entry
    signed_date = payload.get("signedDate")
    if isinstance(signed_date, (int, float)) and not isinstance(signed_date, bool):
        return (1, float(signed_date), idx)
    return (0, 0.0, idx)


async def replay(
    items: list[HistoryItem],
    *,
    apply: bool,
    verify: Callable[[str], dict] = _decode_notification_payload,
    handler: Callable[[dict], Any] = handle_appstore_notification,
    lookup: Callable[[str], Any] = get_contractor_by_subscription_uuid,
    emit: Callable[[str], None] = print,
) -> ReplayReport:
    """Verify, sort, summarize, and (only when apply=True) apply each item.

    Every item is verified first; any exception from ``verify`` (not just
    ValueError -- a malformed payload can raise other things too) counts as
    ``rejected`` and the item is skipped (never applied, never passed to the
    stale check). Surviving payloads are sorted ascending by signedDate (see
    ``_sort_key``), then for each one:

    1. A stale-deactivation check runs (see ``_is_stale_deactivation``),
       *regardless of apply*. A stale item increments ``stale_skipped``,
       emits a "STALE" line, and is never handed to ``handler`` -- not even
       in dry-run mode, so ``dry_run`` excludes stale items too.
    2. Otherwise one summary line is emitted via ``emit``. When ``apply`` is
       True, ``handler`` is awaited: True increments ``applied``, False
       increments ``handler_false``, and a raised exception increments
       ``handler_error`` (only its type is logged) and the loop continues
       to the next item.
    """
    report = ReplayReport(fetched=len(items))

    verified: list[tuple[int, dict, list[dict]]] = []
    for idx, item in enumerate(items):
        try:
            payload = verify(item.signed_payload)
        except Exception as exc:  # noqa: BLE001 - any verifier failure is a rejection
            report.rejected += 1
            logger.warning("appstore_replay verify rejected item: %s", type(exc).__name__)
            continue
        verified.append((idx, payload, item.send_attempts))

    verified.sort(key=_sort_key)

    for _idx, payload, attempts in verified:
        summary = summarize(payload, attempts)
        notification_type = summary["type"] or "UNKNOWN"
        report.by_type[notification_type] = report.by_type.get(notification_type, 0) + 1

        if await _is_stale_deactivation(payload, lookup=lookup):
            report.stale_skipped += 1
            emit(format_summary_line(summary) + "  STALE (account term ends later) — skipped")
            continue

        emit(format_summary_line(summary))

        if not apply:
            report.dry_run += 1
            continue

        try:
            handled = await handler(payload)
        except Exception as exc:  # noqa: BLE001 - counted and logged by type only
            report.handler_error += 1
            emit(f"  -> handler_error: {type(exc).__name__}")
            continue

        if handled:
            report.applied += 1
        else:
            report.handler_false += 1

    return report
