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
            "databaseURL": "https://kevin-491315-rtdb.firebaseio.com",
        })


def _get_ref(call_sid: str):
    """Get RTDB reference for an active call."""
    _init_firebase()
    return rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}")


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
        await loop.run_in_executor(None, lambda: ref.transaction(_transaction))

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
