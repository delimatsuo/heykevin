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
import time
from dataclasses import dataclass
from typing import Optional

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
    now: Optional[float] = None,
) -> RateLimitResult:
    """Atomically prune the rolling window, decide allow/deny, and on allow append.

    Always returns a RateLimitResult — never raises on Firestore errors. On a
    Firestore failure we *fail closed*: deny the request with retry_after = window
    so attackers cannot bypass the limit by triggering Firestore errors. Logged
    so operators can investigate.
    """
    if limit <= 0:
        # Misconfiguration → deny everything.
        return RateLimitResult(False, 0, window_seconds, 0)

    if not key:
        return RateLimitResult(False, 0, window_seconds, 0)

    from app.db.firestore_client import get_firestore_client
    from google.cloud import firestore as fs

    db = get_firestore_client()
    doc_ref = _doc_ref(scope, key)
    current_time = float(now) if now is not None else time.time()
    window_start = current_time - float(window_seconds)

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
                {"bucket": bucket, "updated_at": current_time},
                merge=True,
            )
            return RateLimitResult(False, 0, retry_after, len(bucket))

        bucket.append(current_time)
        # Cap the stored bucket so a misbehaving caller cannot inflate the document.
        if len(bucket) > limit + 1:
            bucket = sorted(bucket)[-(limit + 1):]

        transaction.set(
            doc_ref,
            {"bucket": bucket, "updated_at": current_time},
            merge=True,
        )
        return RateLimitResult(True, max(0, limit - len(bucket)), 0, len(bucket))

    loop = asyncio.get_event_loop()
    transaction = db.transaction()
    try:
        return await loop.run_in_executor(None, lambda: _txn(transaction))
    except Exception as e:  # noqa: BLE001 — fail closed, don't bypass limit
        logger.error(
            "rate_limits: Firestore failure scope=%s key=%s err=%s — failing closed",
            scope, key, e,
            exc_info=True,
        )
        return RateLimitResult(False, 0, window_seconds, limit)
