"""Conference name registry.

The Twilio incoming webhook used to derive the conference friendly name from
the inbound `CallSid`. That value is partially observable (Twilio status
callbacks expose it) and was the basis for F-07's defense-in-depth weakness:
an attacker who knew or guessed a `CallSid` could join the conference by
replaying the same name.

This module provides:

  * ``new_conference_name()`` — opaque, cryptographically-random conference
    name generated with ``secrets.token_urlsafe(16)``.
  * ``register_conference()`` — persists a binding
    ``{conference_name -> {contractor_id, call_sid, created_at}}`` in
    Firestore so subsequent webhooks (notably ``/webhooks/twilio/ios-voice``
    and ``/api/calls/{call_sid}/action``) can resolve the name back to the
    owning contractor and enforce ownership.
  * ``get_conference_binding()`` — read-only lookup used at the iOS-voice
    webhook and the call-action endpoint (F-13).

The collection ``conference_bindings`` is keyed by the conference name. Entries
expire 1 hour after creation; we don't actively delete them (Firestore TTL or a
background sweep handles long-term cleanup), but readers treat anything older
than the TTL as missing so a stolen-and-stale conference name can't be reused.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Optional, TypedDict

from app.utils.logging import get_logger

logger = get_logger(__name__)

# How long a conference binding is considered valid for ownership checks.
# Calls have a 90-minute hard cap; we add a margin.
CONFERENCE_BINDING_TTL_SECONDS = 2 * 60 * 60  # 2 hours

_COLLECTION = "conference_bindings"


class ConferenceBinding(TypedDict, total=False):
    contractor_id: str
    call_sid: str
    created_at: float


def new_conference_name(prefix: str = "") -> str:
    """Generate an opaque, unguessable conference friendly-name.

    A short prefix (``"direct"``, ``"expired"`` etc.) is preserved purely for
    log readability; the security-relevant entropy is in the suffix. Twilio's
    conference friendly name allows alphanumerics + ``_`` + ``-`` and the
    iOS-voice webhook validates against ``^[a-zA-Z0-9_-]+$``.
    """
    suffix = secrets.token_urlsafe(16)
    if prefix:
        return f"{prefix}_{suffix}"
    return suffix


async def register_conference(
    conference_name: str,
    contractor_id: str,
    call_sid: str,
) -> None:
    """Persist a ``conference_name -> {contractor_id, call_sid, created_at}`` row."""
    if not conference_name or not contractor_id:
        logger.warning("register_conference called with empty conference_name or contractor_id")
        return
    try:
        from app.db.firestore_client import get_firestore_client

        db = get_firestore_client()
        loop = asyncio.get_event_loop()
        doc = db.collection(_COLLECTION).document(conference_name)
        payload: ConferenceBinding = {
            "contractor_id": contractor_id,
            "call_sid": call_sid,
            "created_at": time.time(),
        }
        await loop.run_in_executor(None, lambda: doc.set(dict(payload)))
    except Exception as e:  # pragma: no cover — logged, never raised
        logger.error(f"Failed to register conference binding: {e}", exc_info=True)


async def get_conference_binding(conference_name: str) -> Optional[ConferenceBinding]:
    """Look up the binding for ``conference_name``; returns None if missing or expired."""
    if not conference_name:
        return None
    try:
        from app.db.firestore_client import get_firestore_client

        db = get_firestore_client()
        loop = asyncio.get_event_loop()
        doc = db.collection(_COLLECTION).document(conference_name)
        snap = await loop.run_in_executor(None, doc.get)
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        created_at = float(data.get("created_at", 0))
        if created_at and time.time() - created_at > CONFERENCE_BINDING_TTL_SECONDS:
            return None
        return ConferenceBinding(
            contractor_id=str(data.get("contractor_id", "")),
            call_sid=str(data.get("call_sid", "")),
            created_at=created_at,
        )
    except Exception as e:
        logger.error(f"Failed to get conference binding: {e}", exc_info=True)
        return None


async def resolve_conference_owner(conference_name: str) -> Optional[str]:
    """Return the contractor_id that owns ``conference_name``, or None."""
    binding = await get_conference_binding(conference_name)
    if not binding:
        return None
    return binding.get("contractor_id") or None
