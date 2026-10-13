"""Single-pass housekeeping, shared by legacy loops and the private worker."""

import asyncio
import time

from app.utils.logging import get_logger

logger = get_logger(__name__)


def _delete_if_stale(current, now: float):
    """RTDB transactions retry against concurrent call-state refreshes."""
    if isinstance(current, dict):
        updated = current.get("state_updated_at", 0)
        if updated and now - updated > 7200:
            return None
    return current


async def orphan_calls_once():
    from app.db.cache import _init_firebase, ACTIVE_CALLS_PATH
    from firebase_admin import db as rtdb

    _init_firebase()
    loop = asyncio.get_running_loop()
    calls = await loop.run_in_executor(None, rtdb.reference(ACTIVE_CALLS_PATH).get)
    if not isinstance(calls, dict):
        return
    now = time.time()
    for call_sid, data in calls.items():
        if not isinstance(data, dict):
            continue
        updated = data.get("state_updated_at", 0)
        if updated and now - updated > 7200:
            ref = rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}")
            await loop.run_in_executor(None, lambda r=ref: r.transaction(
                lambda current: _delete_if_stale(current, now)))


async def call_retention_once():
    from app.db.calls import cleanup_old_calls

    for _ in range(4):
        if await cleanup_old_calls() < 500:
            break


async def lapsed_trials_once(*, strict: bool = False):
    from google.cloud import firestore
    from app.db.firestore_client import get_firestore_client
    from app.services.subscription import should_expire_trial

    db = get_firestore_client()
    loop = asyncio.get_running_loop()
    now = time.time()
    docs = await loop.run_in_executor(None, lambda: list(db.collection("contractors")
        .where("subscription_status", "==", "trial").stream()))

    def expire(doc_id):
        ref = db.collection("contractors").document(doc_id)

        @firestore.transactional
        def transaction_body(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists or not should_expire_trial(snapshot.to_dict() or {}, now):
                return False
            transaction.update(ref, {"subscription_status": "expired"})
            return True

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


async def expired_contractors_once():
    from app.services.number_release import run_expired_contractor_cleanup_once
    await run_expired_contractor_cleanup_once()


async def data_purge_once():
    from app.db.purge import purge_sweep
    await purge_sweep()
