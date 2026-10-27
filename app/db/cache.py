"""Firebase RTDB wrapper for active call state.

Uses firebase_admin for server-side access (bypasses security rules).
Provides atomic state transitions via RTDB transactions.
"""

import asyncio
import time
from typing import Optional

import firebase_admin
from firebase_admin import credentials, db as rtdb

from app.config import settings
from app.services.state_machine import ActiveCall, CallState, can_transition
from app.utils.logging import get_logger

logger = get_logger(__name__)

ACTIVE_CALLS_PATH = "/active_calls"
STALE_THRESHOLD = 600  # 10 minutes — auto-cleanup

# Initialize Firebase Admin SDK (uses default credentials on Cloud Run)
_app = None


def _init_firebase():
    global _app
    if _app is not None:
        return
    try:
        _app = firebase_admin.get_app()
    except ValueError:
        _app = firebase_admin.initialize_app(None, {
            "databaseURL": settings.firebase_database_url,
        })


def _get_ref(call_sid: str):
    """Get RTDB reference for an active call."""
    _init_firebase()
    return rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}")


class _NothingToWrite(Exception):
    def __init__(self, current):
        self.current = current


def run_rtdb_transaction(ref, update):
    """ref.transaction(update), where returning None for a missing record writes nothing.

    firebase-admin writes the callback's result with set_if_unchanged, which refuses
    None (ValueError). Callbacks here return the current value to mean "no change",
    which is None once the record has been deleted, e.g. after the call ended. Returns
    the committed value, or None when the record is gone. Deleting a record that
    exists takes delete_if_unchanged.
    """
    def guarded(current):
        new = update(current)
        if new is None:
            raise _NothingToWrite(current)
        return new

    try:
        return ref.transaction(guarded)
    except _NothingToWrite as result:
        if result.current is not None:
            raise ValueError("RTDB transaction callback returned None for an existing record; "
                             "use delete_if_unchanged to delete it") from None
        return None


def delete_if_unchanged(ref, etag) -> bool:
    """Delete ref's location only if its ETag still matches; False on a 412 or without an ETag.

    firebase-admin (pinned) has no public conditional delete, so this sends RTDB's
    documented REST call: DELETE with if-match.
    """
    from firebase_admin import exceptions

    if not isinstance(etag, str) or not etag:
        return False  # requests drops a None header, which would make the DELETE unconditional
    try:
        ref._client.request("delete", ref._add_suffix(), headers={"if-match": etag})
    except exceptions.FailedPreconditionError:
        return False
    return True


async def save_active_call(call: ActiveCall):
    """Save active call state to RTDB."""
    try:
        ref = _get_ref(call.call_sid)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, ref.set, call.to_dict())
        logger.info(f"Active call saved: state={call.state.value}")
    except Exception as e:
        logger.error(f"RTDB save failed: {e}", exc_info=True)


async def get_active_call(call_sid: str) -> Optional[ActiveCall]:
    """Get active call state from RTDB. Returns None if not found."""
    try:
        ref = _get_ref(call_sid)
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, ref.get)
        if data:
            return ActiveCall.from_dict(data)
    except Exception as e:
        logger.error(f"RTDB get failed: {e}", exc_info=True)
    return None


async def transition_state(call_sid: str, to_state: CallState) -> Optional[ActiveCall]:
    """Atomically transition call state. Returns updated call or None if transition invalid.

    Uses RTDB transaction for compare-and-swap semantics.
    """
    try:
        ref = _get_ref(call_sid)

        result = [None]

        def _transaction(current_data):
            result[0] = None  # a retry must not report an earlier attempt's transition
            if current_data is None:
                logger.warning(f"Call {call_sid} not found in RTDB")
                return current_data

            current_state = CallState(current_data.get("state", "pending"))

            if not can_transition(current_state, to_state):
                logger.warning(
                    f"Invalid transition: {current_state.value} → {to_state.value}",
                )
                return current_data  # No change

            current_data["state"] = to_state.value
            current_data["state_updated_at"] = time.time()
            result[0] = current_data
            return current_data

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_rtdb_transaction, ref, _transaction)

        if result[0] and result[0].get("state") == to_state.value:
            logger.info(f"State transition: → {to_state.value}")
            return ActiveCall.from_dict(result[0])

        return None

    except Exception as e:
        logger.error(f"RTDB transition failed: {e}", exc_info=True)
        return None


async def update_active_call(call_sid: str, updates: dict):
    """Partial update of active call state (non-transactional, for transcript etc.)."""
    try:
        ref = _get_ref(call_sid)
        updates["state_updated_at"] = time.time()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, ref.update, updates)
    except Exception as e:
        logger.error(f"RTDB update failed: {e}", exc_info=True)


async def delete_active_call(call_sid: str):
    """Remove active call state from RTDB (cleanup after call ends)."""
    try:
        ref = _get_ref(call_sid)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, ref.delete)
        logger.info("Active call cleaned up from RTDB")
    except Exception as e:
        logger.error(f"RTDB delete failed: {e}", exc_info=True)
