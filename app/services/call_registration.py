"""Bounded, create-once incoming screening authority.

The retained history document is a receipt, not a second stream authority. It
contains only a token digest; the bearer token stays in RTDB. Missing RTDB state
after a receipt already exists is never recreated by a webhook retry. Receipts
share history's 90-day retention; replay after that retention is not covered.
If receipt creation fails, only the two-hour RTDB fence protects retries.

This is an AI registration barrier, not arbitration for all incoming routes.
Existing subscription/direct/spam paths still make their own decisions. An
unacknowledged SDK write can commit after caller-preserving outage forwarding;
neither cancellation nor an unacknowledged fence proves that write was revoked.
Old revisions with unconditional set/delete writers are also outside this
protocol; a deployment transition must account for their in-flight requests.
"""

import asyncio
from dataclasses import dataclass
import hashlib
import secrets
import threading
import time

from google.api_core.exceptions import AlreadyExists

from app.db import cache
from app.db.firestore_client import get_firestore_client
from app.services.owner_call_actions import live_record, validate_call_sid
from app.services.state_machine import ActiveCall
from app.utils.logging import get_logger

logger = get_logger(__name__)
REGISTRATION_TIMEOUT_SECONDS = 2.0
REGISTRATION_FENCE_TIMEOUT_SECONDS = 0.5
RECEIPT_RPC_TIMEOUT_SECONDS = 0.5
RECEIPT_FIELD = "_screening_registration"


@dataclass(frozen=True)
class RegisteredCall:
    record: dict
    created: bool


class RegistrationUnavailable(Exception):
    """Storage could not confirm authority within the response budget."""


class RegistrationRejected(RegistrationUnavailable):
    """Known terminal, superseded or mismatched authority must not be replayed."""


class _Rejected(Exception):
    pass


class _Existing(Exception):
    def __init__(self, record):
        self.record = record


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _forward_disposition(current, call: ActiveCall) -> bool:
    return (
        isinstance(current, dict)
        and current.get("call_sid") == call.call_sid
        and current.get("contractor_id") == call.contractor_id
        and current.get("registration_disposition") == "forward"
    )


def _matches_registration(current, call: ActiveCall, receipt) -> bool:
    if not isinstance(receipt, dict):
        return False
    token = current.get("ws_token") if isinstance(current, dict) else None
    return (
        live_record(current, call.contractor_id)
        and current.get("call_sid") == call.call_sid
        and current.get("caller_phone") == call.caller_phone
        and current.get("registration_nonce") == receipt.get("nonce")
        and isinstance(token, str) and bool(token)
        and _digest(token) == receipt.get("token_digest")
        and receipt.get("voice_engine") in ("elevenlabs", "gemini", "relay")
        and current.get("registration_voice_engine") == receipt["voice_engine"]
        and current.get("state") == "screening"
        and not current.get("accepted") and not current.get("owner_action")
        and not current.get("kevin_redirect_nonce")
    )


async def register_screening_call(
    call: ActiveCall, *, voice_engine: str = "elevenlabs",
) -> RegisteredCall | None:
    """Publish essential authority before returning any streaming instructions.

One request owns creation. Concurrent/repeated requests may read the original
authority, but never rotate its token, refresh its age or replay side effects.
An outer timeout bounds the caller, not an SDK request already in flight.
"""
    validate_call_sid(call.call_sid)
    if not call.contractor_id or not call.ws_token:
        return None
    cancelled = threading.Event()
    deadline = time.monotonic() + REGISTRATION_TIMEOUT_SECONDS
    nonce = secrets.token_urlsafe(24)
    record = call.to_dict()
    record["registration_nonce"] = nonce
    engine = voice_engine if voice_engine in ("gemini", "relay") else "elevenlabs"
    record["registration_voice_engine"] = engine
    receipt = {"nonce": nonce, "token_digest": _digest(call.ws_token), "voice_engine": engine}
    original = None
    # A receipt-only row remains an honest, useful call-history row even when
    # subsequent enrichment fails. No auth token or phone-derived identity key.
    history = {
        "call_sid": call.call_sid, "contractor_id": call.contractor_id,
        "caller_phone": call.caller_phone, "caller_name": call.caller_name,
        "timestamp": record["state_updated_at"], "route_taken": "ai_screening",
        "trust_score": call.trust_score, RECEIPT_FIELD: receipt,
    }

    def check_budget():
        if cancelled.is_set() or time.monotonic() >= deadline:
            raise _Rejected()

    def reserve():
        check_budget()
        ref = get_firestore_client().collection("calls").document(call.call_sid)
        try:
            ref.create(history, retry=None, timeout=RECEIPT_RPC_TIMEOUT_SECONDS)
            return history
        except AlreadyExists:
            check_budget()
            snap = ref.get(retry=None, timeout=RECEIPT_RPC_TIMEOUT_SECONDS)
            return snap.to_dict() if snap.exists else None

    try:
        async with asyncio.timeout(REGISTRATION_TIMEOUT_SECONDS):
            stored = await asyncio.to_thread(reserve)
            if (not isinstance(stored, dict)
                    or stored.get("contractor_id") != call.contractor_id
                    or stored.get("caller_phone") != call.caller_phone
                    or stored.get("ended_at") or stored.get("call_status") in {
                        "completed", "busy", "no-answer", "canceled", "failed"}):
                # This is affirmative evidence, not an outage. In particular a
                # terminal history row must never be turned into new forwarding.
                raise RegistrationRejected()
            check_budget()
            original = stored.get(RECEIPT_FIELD)
            if not isinstance(original, dict) or not original.get("nonce") or not original.get("token_digest"):
                raise RegistrationRejected()
            created = original["nonce"] == nonce
            if created:
                from app.services.conference_registry import register_conference
                await register_conference(call.conference_name, call.contractor_id, call.call_sid)
                check_budget()

            initialization_attempted = False

            def initialize(current):
                nonlocal initialization_attempted
                if current is not None:
                    if (_matches_registration(current, call, original)
                            or _forward_disposition(current, call)):
                        check_budget()
                        # Read-only duplicates must not PUT an old snapshot.
                        raise _Existing(current)
                    # Positive conflict evidence must survive a later storage
                    # failure. Do not ask the outage resolver to re-prove it.
                    raise RegistrationRejected()
                check_budget()  # SDK retries must not revive cancelled work.
                if created and not initialization_attempted:
                    initialization_attempted = True
                    return record
                raise _Rejected()

            ref = cache._get_ref(call.call_sid)
            if created:
                try:
                    committed = await asyncio.to_thread(cache.run_rtdb_transaction, ref, initialize)
                except _Existing as existing:
                    committed = existing.record
            else:
                # The winning request may still be publishing. Wait only within
                # this request's total budget; never initialize on its behalf.
                while True:
                    check_budget()
                    committed = await asyncio.to_thread(ref.get)
                    if committed is not None:
                        break
                    await asyncio.sleep(0.05)
            if _matches_registration(committed, call, original):
                check_budget()
                return RegisteredCall(committed, created)
            if _forward_disposition(committed, call):
                check_budget()
                return None
            raise RegistrationRejected()
    except RegistrationRejected:
        raise
    except asyncio.CancelledError:
        cancelled.set()
        # Cancellation cannot revoke a STREAM disposition another request read.
        try:
            await _resolve_failed_registration(call, original, nonce)
        except RegistrationUnavailable:
            pass
        raise
    except Exception as error:
        cancelled.set()
        logger.warning("Screening registration unavailable: %s", type(error).__name__)
        return await _resolve_failed_registration(call, original, nonce)
    finally:
        cancelled.set()


async def _resolve_failed_registration(call: ActiveCall, receipt, creator_nonce: str) -> RegisteredCall | None:
    """Choose FORWARD only from missing authority; never revoke published STREAM.

    A failed/late CAS is ambiguous. An acknowledged competing CAS either fences
    that creation or observes and adopts it. If neither is acknowledged, the
    caller may preserve availability by forwarding, but a late write/retry can
    still choose streaming. Do not report an unknown disposition as fenced.
    """
    marker = {
        "call_sid": call.call_sid, "state": "ended", "state_updated_at": time.time(),
        "registration_disposition": "forward", "contractor_id": call.contractor_id,
    }

    def resolve(current):
        if current is None:
            return marker
        raise _Existing(current)

    try:
        async with asyncio.timeout(REGISTRATION_FENCE_TIMEOUT_SECONDS):
            try:
                current = await asyncio.to_thread(cache.run_rtdb_transaction, cache._get_ref(call.call_sid), resolve)
            except _Existing as existing:
                current = existing.record
        if (isinstance(current, dict) and current.get("call_sid") == call.call_sid
                and current.get("contractor_id") == call.contractor_id):
            if _forward_disposition(current, call):
                return None
            if _matches_registration(current, call, receipt):
                return RegisteredCall(current, receipt["nonce"] == creator_nonce)
            if (receipt is None and live_record(current, call.contractor_id)
                    and current.get("caller_phone") == call.caller_phone
                    and current.get("state") == "screening"
                    and not current.get("accepted") and not current.get("owner_action")
                    and not current.get("kevin_redirect_nonce")):
                # A receipt read outage cannot prove a token mismatch or a
                # completed call. Preserve the caller's outage fallback without
                # exposing or changing this unverified existing stream token.
                raise RegistrationUnavailable()
        if current is not None:
            raise RegistrationRejected()
    except RegistrationRejected:
        raise
    except Exception as error:
        logger.warning("Registration disposition unconfirmed: %s", type(error).__name__)
    raise RegistrationUnavailable()


async def close_call_registration(call_sid: str) -> bool:
    """Fence in-flight initial writes before waiting for terminal history.

The token-free tombstone uses the existing two-hour orphan retention. Its ETag
blocks a previously read missing-state write; subsequent webhook retries are
also fenced by the retained receipt. No terminal marker contains caller PII.
"""
    validate_call_sid(call_sid)
    marker = {"call_sid": call_sid, "state": "ended", "state_updated_at": time.time()}
    try:
        async with asyncio.timeout(REGISTRATION_TIMEOUT_SECONDS):
            ref = cache._get_ref(call_sid)
            await asyncio.to_thread(cache.run_rtdb_transaction, ref, lambda _current: marker)
        return True
    except Exception as error:
        logger.warning("Terminal authority fence failed: %s", type(error).__name__)
        return False


async def enrich_registered_caller(call_sid: str, contractor_id: str, ws_token: str, name: str):
    """CNAM cannot recreate a missing call or mutate replacement authority."""
    validate_call_sid(call_sid)

    def update(current):
        if (not live_record(current, contractor_id)
                or current.get("call_sid") != call_sid
                or current.get("ws_token") != ws_token
                or current.get("accepted") or current.get("owner_action")):
            raise _Rejected()
        return {**current, "caller_name": name}

    try:
        await asyncio.to_thread(cache.run_rtdb_transaction, cache._get_ref(call_sid), update)
    except Exception as error:
        logger.warning("Caller enrichment unavailable: %s", type(error).__name__)
