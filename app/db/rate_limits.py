"""Persistent, cross-instance rate limiting backed by Firestore.

The original implementation kept a per-process `dict` of timestamps. With
Cloud Run autoscaling, that lets attackers multiply their effective budget by
the number of running instances, and a fresh deploy/cold start wipes history.

This module stores a rolling window per `(scope, key)` in Firestore so all
instances share state. The default scope is "sign_offer", but additional
callers can use any scope key.

Firestore data model:

    rate_limits/{scope}__{key}
        bucket: list[float]   # unix timestamps, capped to <= limit + 1 entries
        updated_at: float     # last write time

We use a Firestore transaction for compare-and-update semantics so two
concurrent calls cannot both squeeze in past the limit.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from app.utils.logging import get_logger

logger = get_logger(__name__)


COLLECTION = "rate_limits"


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int  # 0 when allowed
    count_in_window: int


def _doc_id(scope: str, key: str) -> str:
    safe_key = key.replace("/", "_")
    return f"{scope}__{safe_key}"


def _doc_ref(scope: str, key: str):
    from app.db.firestore_client import get_firestore_client
    db = get_firestore_client()
    return db.collection(COLLECTION).document(_doc_id(scope, key))


async def check_and_increment(
    *,
    scope: str,
    key: str,
    limit: int,
    window_seconds: int,
    now: float | None = None,
    document_ttl_seconds: int | None = None,
) -> RateLimitResult:
    """Atomically prune the rolling window, decide allow/deny, and on allow append.

    Always returns a RateLimitResult — never raises on Firestore errors. On a
    Firestore failure we *fail closed*: deny the request with retry_after = window
    so attackers cannot bypass the limit by triggering Firestore errors. Logged
    so operators can investigate.
    """
    if limit <= 0 or window_seconds <= 0:
        # Misconfiguration → deny everything.
        return RateLimitResult(False, 0, window_seconds, 0)

    if not key:
        return RateLimitResult(False, 0, window_seconds, 0)

    current_time = float(now) if now is not None else time.time()
    expires_at: datetime | None = None
    if document_ttl_seconds is not None:
        if (
            isinstance(document_ttl_seconds, bool)
            or not isinstance(document_ttl_seconds, (int, float))
            or not math.isfinite(float(document_ttl_seconds))
            or float(document_ttl_seconds) < float(window_seconds)
        ):
            return RateLimitResult(False, 0, window_seconds, limit)
        try:
            expires_at = datetime.fromtimestamp(
                current_time + float(document_ttl_seconds),
                tz=UTC,
            )
        except (OverflowError, OSError, ValueError):
            return RateLimitResult(False, 0, window_seconds, limit)

    from google.cloud import firestore as fs

    from app.db.firestore_client import get_firestore_client

    db = get_firestore_client()
    doc_ref = _doc_ref(scope, key)
    window_start = current_time - float(window_seconds)

    def _write_data(bucket: list[float]) -> dict:
        data = {"bucket": bucket, "updated_at": current_time}
        if expires_at is not None:
            # A Firestore TTL policy on this field is required for callers that
            # opt into bounded document retention. Existing rate-limit users do
            # not gain or depend on TTL behavior implicitly.
            data["expires_at"] = expires_at
        return data

    @fs.transactional
    def _txn(transaction) -> RateLimitResult:
        snapshot = doc_ref.get(transaction=transaction)
        bucket: list[float] = []
        if snapshot.exists:
            raw = (snapshot.to_dict() or {}).get("bucket", []) or []
            for ts in raw:
                try:
                    ts_f = float(ts)
                except (TypeError, ValueError):
                    continue
                if ts_f > window_start:
                    bucket.append(ts_f)

        if len(bucket) >= limit:
            # Compute retry-after as the time until the oldest entry exits the window.
            oldest = min(bucket)
            retry_after = max(1, int(oldest + window_seconds - current_time) + 1)
            transaction.set(
                doc_ref,
                _write_data(bucket),
                merge=True,
            )
            return RateLimitResult(False, 0, retry_after, len(bucket))

        bucket.append(current_time)
        # Cap the stored bucket so a misbehaving caller cannot inflate the document.
        if len(bucket) > limit + 1:
            bucket = sorted(bucket)[-(limit + 1):]

        transaction.set(
            doc_ref,
            _write_data(bucket),
            merge=True,
        )
        return RateLimitResult(True, max(0, limit - len(bucket)), 0, len(bucket))

    loop = asyncio.get_event_loop()
    transaction = db.transaction()
    try:
        return await loop.run_in_executor(None, lambda: _txn(transaction))
    except Exception as error:  # noqa: BLE001 - storage uncertainty must fail closed
        logger.error(
            "rate_limits: Firestore failure scope=%s exception_type=%s — failing closed",
            scope,
            type(error).__name__,
        )
        return RateLimitResult(False, 0, window_seconds, limit)
