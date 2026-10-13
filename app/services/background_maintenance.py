"""Single-pass housekeeping, shared by legacy loops and the private worker."""

import asyncio
import time

from app.utils.logging import get_logger

logger = get_logger(__name__)


def _bounded_cursor(continuation: dict | None) -> str | None:
    cursor = (continuation or {}).get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ValueError("Invalid background-maintenance cursor")
    return cursor


def _delete_if_stale(current, now: float):
    """RTDB transactions retry against concurrent call-state refreshes."""
    if isinstance(current, dict):
        updated = current.get("state_updated_at", 0)
        if updated and now - updated > 7200:
            return None
    return current


async def orphan_calls_once(*, continuation: dict | None = None, page_size: int | None = None) -> dict:
    """Clean stale calls, using a bounded RTDB key page for external mode."""
    from app.db.cache import _init_firebase, ACTIVE_CALLS_PATH
    from firebase_admin import db as rtdb

    bounded = page_size is not None
    if bounded and (not isinstance(page_size, int) or not 1 <= page_size <= 25):
        raise ValueError("External orphan cleanup requires a page size of 1-25")
    cursor = _bounded_cursor(continuation) if bounded else None
    _init_firebase()
    loop = asyncio.get_running_loop()
    ref = rtdb.reference(ACTIVE_CALLS_PATH)
    if bounded:
        query = ref.order_by_key()
        if cursor:
            query = query.start_at(cursor)
        calls = await loop.run_in_executor(None, lambda: query.limit_to_first(page_size + 1).get())
    else:
        calls = await loop.run_in_executor(None, ref.get)
    if not isinstance(calls, dict):
        return {"cursor": None} if bounded else {}
    now = time.time()
    items = sorted(calls.items())
    if bounded and cursor:
        items = [(call_sid, data) for call_sid, data in items if call_sid != cursor]
    selected = items[:page_size] if bounded else items
    for call_sid, data in selected:
        if not isinstance(data, dict):
            continue
        updated = data.get("state_updated_at", 0)
        if updated and now - updated > 7200:
            ref = rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}")
            await loop.run_in_executor(None, lambda r=ref: r.transaction(
                lambda current: _delete_if_stale(current, now)))
    if not bounded:
        return {}
    if not selected:
        return {"cursor": None}
    # An exact full page may be the final page; retaining its final key for one
    # extra empty pass is safe and avoids losing a key at the query boundary.
    next_cursor = selected[-1][0] if len(calls) >= page_size else None
    return {"cursor": next_cursor}


async def call_retention_once(*, strict: bool = False):
    from app.db.calls import cleanup_old_calls

    for _ in range(4):
        if strict:
            deleted = await cleanup_old_calls(strict=True)
        else:
            deleted = await cleanup_old_calls()
        if deleted < 500:
            break


async def lapsed_trials_once(
    *, strict: bool = False, continuation: dict | None = None, page_size: int | None = None,
) -> dict:
    from google.cloud import firestore
    from app.db.contractors import (
        ReleaseAwareMutationResult,
        apply_release_aware_contractor_update,
    )
    from app.db.firestore_client import get_firestore_client
    from app.services.subscription import should_expire_trial

    bounded = page_size is not None
    if bounded and (not isinstance(page_size, int) or not 1 <= page_size <= 25):
        raise ValueError("External lapsed-trial sweep requires a page size of 1-25")
    cursor = _bounded_cursor(continuation) if bounded else None
    db = get_firestore_client()
    loop = asyncio.get_running_loop()
    now = time.time()

    def fetch_docs():
        query = db.collection("contractors").where("subscription_status", "==", "trial")
        if not bounded:
            return list(query.stream())
        query = query.order_by("__name__")
        if cursor:
            query = query.start_after({"__name__": db.collection("contractors").document(cursor)})
        docs = list(query.limit(page_size).stream())
        if not docs and cursor:
            docs = list(db.collection("contractors")
                .where("subscription_status", "==", "trial")
                .order_by("__name__").limit(page_size).stream())
        return docs

    docs = await loop.run_in_executor(None, fetch_docs)

    def expire(doc_id):
        ref = db.collection("contractors").document(doc_id)

        @firestore.transactional
        def transaction_body(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return False
            data = snapshot.to_dict() or {}
            if not should_expire_trial(data, now):
                return False
            outcome = apply_release_aware_contractor_update(
                transaction, ref, data, {"subscription_status": "expired"},
            )
            return outcome is not ReleaseAwareMutationResult.HELD

        return transaction_body(db.transaction())

    for doc in docs:
        if not should_expire_trial(doc.to_dict() or {}, now):
            continue
        try:
            await loop.run_in_executor(None, lambda d=doc: expire(d.id))
        except Exception as error:
            if strict:
                raise
            logger.warning("Lapsed trial sweep failed for one contractor: %s", type(error).__name__)
    if not bounded:
        return {}
    return {"cursor": docs[-1].id if len(docs) == page_size else None}


async def expired_contractors_once(
    *, continuation: dict | None = None, page_size: int | None = None,
) -> dict:
    from app.services.number_release import (
        run_bounded_expired_contractor_cleanup_once,
        run_expired_contractor_cleanup_once,
    )
    if page_size is None:
        await run_expired_contractor_cleanup_once()
        return {}
    return await run_bounded_expired_contractor_cleanup_once(
        continuation or {}, page_size=page_size,
    )


async def data_purge_once():
    from app.db.purge import purge_sweep
    await purge_sweep()
