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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import httpx

from app.services.subscription import _get_appstore_jwt
from app.webhooks.appstore import _decode_notification_payload
from app.services.subscription import handle_appstore_notification

NOTIFICATION_HISTORY_PATH = "/inApps/v1/notifications/history"

# At most six entries per Apple's documented response shape.
_MAX_SEND_ATTEMPTS = 6


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
) -> list[HistoryItem]:
    """Page through Apple's Get Notification History endpoint.

    POSTs to ``{base_url}/inApps/v1/notifications/history`` with
    ``startDate``/``endDate`` (integer ms since epoch) and ``onlyFailures``,
    carrying ``paginationToken`` as a query parameter on every page after the
    first. Raises RuntimeError on any non-200 response; the caller receives
    no partial result in that case (nothing is returned until every page
    has succeeded).
    """
    owns_client = client is None
    http_client = client or httpx.AsyncClient()
    items: list[HistoryItem] = []
    try:
        pagination_token: Optional[str] = None
        while True:
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
                        send_attempts=list(record.get("sendAttempts") or [])[: _MAX_SEND_ATTEMPTS],
                    )
                )
            if not data.get("hasMore"):
                break
            pagination_token = data.get("paginationToken")
            if not pagination_token:
                break
        return items
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


def _original_transaction_suffix(data: dict) -> str:
    """Best-effort last-6-chars of originalTransactionId, never the full id.

    originalTransactionId is nested inside the (separately signed)
    signedTransactionInfo/signedRenewalInfo JWS carried in the notification's
    ``data`` object, not at the top level. This only unwraps the payload
    section (no signature check needed here — the outer envelope is already
    verified, and this is a read-only summary, never fed to the handler).
    """
    from app.services.subscription import _decode_jws_payload

    if not isinstance(data, dict):
        return ""
    for key in ("signedTransactionInfo", "signedRenewalInfo"):
        signed = data.get(key)
        if not isinstance(signed, str) or not signed:
            continue
        decoded = _decode_jws_payload(signed)
        if not isinstance(decoded, dict):
            continue
        original_id = decoded.get("originalTransactionId")
        if isinstance(original_id, str) and original_id:
            return original_id[-6:]
    return ""


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
    dry_run: int = 0  # would apply (dry-run mode only)
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
    emit: Callable[[str], None] = print,
) -> ReplayReport:
    """Verify, sort, summarize, and (only when apply=True) apply each item.

    Every item is verified first; a ValueError from ``verify`` counts as
    ``rejected`` and the item is skipped (never applied). Surviving payloads
    are sorted ascending by signedDate (see ``_sort_key``), then one summary
    line is emitted per item via ``emit``. When ``apply`` is True, ``handler``
    is awaited for each verified payload in that order: True increments
    ``applied``, False increments ``handler_false``, and a raised exception
    increments ``handler_error`` (only its type is logged) and the loop
    continues to the next item.
    """
    report = ReplayReport(fetched=len(items))

    verified: list[tuple[int, dict, list[dict]]] = []
    for idx, item in enumerate(items):
        try:
            payload = verify(item.signed_payload)
        except ValueError:
            report.rejected += 1
            continue
        verified.append((idx, payload, item.send_attempts))

    verified.sort(key=_sort_key)

    for _idx, payload, attempts in verified:
        summary = summarize(payload, attempts)
        notification_type = summary["type"] or "UNKNOWN"
        report.by_type[notification_type] = report.by_type.get(notification_type, 0) + 1
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
