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


STALE_CALL_SECONDS = 7200


def _is_stale(record, now: float) -> bool:
    if not isinstance(record, dict):
        return False
    updated = record.get("state_updated_at", 0)
    return (isinstance(updated, (int, float)) and not isinstance(updated, bool)
            and updated > 0 and now - updated > STALE_CALL_SECONDS)


def _delete_if_still_stale(ref, now: float) -> bool:
    """Delete one active call only if it is still stale and unchanged since this read.

    A live call that refreshes the record in between makes the conditional delete
    fail (412), and the next pass then sees it fresh.
    """
    from app.db.cache import delete_if_unchanged

    current, etag = ref.get(etag=True)
    if not _is_stale(current, now):
        return False
    return delete_if_unchanged(ref, etag)


def _call_is_over(record, now: float) -> bool:
    """Legacy /call_commands entries carry no timestamp, so their call's active record dates them.

    publish_message_intent writes a command only while that record is live, and the
    record goes only with the call's terminal status callback or as stale. Nothing
    consumes a command once its call is over: adoption requires a live record.
    """
    return record is None or _is_stale(record, now)


def _delete_command_if_call_over(command_ref, call_ref, now: float) -> bool:
    """Delete one legacy command only if its call is over and it is unchanged since this read.

    The command is read before its call, so a call that starts and writes a command
    mid-pass is found live. A replaced command fails the conditional delete (412).
    """
    from app.db.cache import delete_if_unchanged

    current, etag = command_ref.get(etag=True)
    if current is None or not _call_is_over(call_ref.get(), now):
        return False
    return delete_if_unchanged(command_ref, etag)


async def _sweep_legacy_commands():
    """Remove legacy commands left by ended calls, e.g. those written before #311, whose
    conditional delete always failed."""
    from app.db.cache import ACTIVE_CALLS_PATH
    from app.services.legacy_call_commands import CALL_COMMANDS_PATH
    from firebase_admin import db as rtdb

    loop = asyncio.get_running_loop()
    commands = await loop.run_in_executor(None, rtdb.reference(CALL_COMMANDS_PATH).get)
    if not isinstance(commands, dict):
        return
    now = time.time()
    removed = 0
    for call_sid in sorted(commands):
        removed += await loop.run_in_executor(
            None, _delete_command_if_call_over,
            rtdb.reference(f"{CALL_COMMANDS_PATH}/{call_sid}"),
            rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}"), now,
        )
    if removed:
        logger.info("Removed %d legacy call command(s) of ended calls", removed)


async def orphan_calls_once(*, continuation: dict | None = None, page_size: int | None = None) -> dict:
    """Clean stale calls, using a bounded RTDB key page for external mode.

    The unbounded (embedded) pass then sweeps legacy commands of ended calls.
    """
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
        calls = {}  # no active calls still leaves commands to sweep
    now = time.time()
    items = sorted(calls.items())
    if bounded and cursor:
        items = [(call_sid, data) for call_sid, data in items if call_sid != cursor]
    selected = items[:page_size] if bounded else items
    removed = 0
    for call_sid, data in selected:
        if not _is_stale(data, now):
            continue
        call_ref = rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}")
        removed += await loop.run_in_executor(None, _delete_if_still_stale, call_ref, now)
    if removed:
        logger.info("Removed %d stale active call(s)", removed)
    if not bounded:
        await _sweep_legacy_commands()
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
