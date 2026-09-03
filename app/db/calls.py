"""Firestore operations for call records.

Call transcripts are sensitive (legal/medical/financial discussion in
residential-services calls). They are encrypted at rest with AES-256-GCM
on top of Firestore's default at-rest encryption (SECURITY_AUDIT.md F-11).
The encryption key is loaded from `TRANSCRIPT_ENCRYPTION_KEY` at process
start. Staging and production fail closed when encryption is unavailable;
development and tests retain legacy plaintext compatibility. Reads remain
backwards compatible with both formats.
"""

import asyncio
import base64
import os
import secrets as _secrets
import time
from typing import Any, Optional

from google.api_core.exceptions import FailedPrecondition
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.config import decode_transcript_encryption_key, settings
from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "calls"

# Call records older than 90 days are eligible for cleanup
RETENTION_DAYS = 90


# ---- Transcript encryption helpers (F-11) ---------------------------------

# Marker version that distinguishes ciphertext records from legacy plaintext.
# Reads check `version` first; if missing the value is treated as plaintext.
_TRANSCRIPT_VERSION = 1


class TranscriptEncryptionUnavailableError(RuntimeError):
    """Raised before a protected environment can persist plaintext speech."""


def _load_transcript_key() -> Optional[bytes]:
    """Return the 32-byte AES-256-GCM key, or None if encryption is disabled.

    The key is read from `settings.transcript_encryption_key` (env var
    TRANSCRIPT_ENCRYPTION_KEY) as a base64-encoded string. Invalid or
    short keys are rejected so we never silently fall back to a weak key.
    """
    raw = (settings.transcript_encryption_key or os.environ.get("TRANSCRIPT_ENCRYPTION_KEY", "") or "").strip()
    if not raw:
        return None
    key = decode_transcript_encryption_key(raw)
    if key is None:
        logger.error("Transcript encryption key is unavailable or invalid")
        return None
    return key


def _transcript_encryption_required() -> bool:
    return (settings.environment or "").strip().lower() in {"staging", "production"}


def _is_valid_encrypted_transcript(value: object) -> bool:
    if not isinstance(value, dict) or value.get("version") != _TRANSCRIPT_VERSION:
        return False
    try:
        ciphertext = base64.b64decode(value.get("ciphertext", ""), validate=True)
        nonce = base64.b64decode(value.get("nonce", ""), validate=True)
    except Exception:
        return False
    return len(ciphertext) >= 16 and len(nonce) == 12


def _encrypt_transcript(plaintext: str) -> Optional[dict]:
    """Encrypt `plaintext` with AES-256-GCM. Returns the on-disk dict.

    Returns None if encryption is disabled or fails. The caller decides
    whether plaintext fallback is legal for the current environment.
    """
    if plaintext is None or plaintext == "":
        return None
    key = _load_transcript_key()
    if key is None:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        aesgcm = AESGCM(key)
        nonce = _secrets.token_bytes(12)  # 96-bit nonce for GCM
        ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
        return {
            "version": _TRANSCRIPT_VERSION,
            "ciphertext": base64.b64encode(ct).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
        }
    except Exception as exc:  # pragma: no cover — defensive
        logger.error(
            "Transcript encryption failed: exception_type=%s",
            type(exc).__name__,
        )
        return None


def _decrypt_transcript(value: Any) -> str:
    """Inverse of `_encrypt_transcript`. Plaintext passes through.

    Backwards compat: legacy records without `version` are treated as
    plaintext strings (or empty when missing/non-string).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("version") == _TRANSCRIPT_VERSION:
        key = _load_transcript_key()
        if key is None:
            logger.warning(
                "Encrypted transcript present but TRANSCRIPT_ENCRYPTION_KEY is unset — returning empty",
            )
            return ""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            aesgcm = AESGCM(key)
            nonce = base64.b64decode(value["nonce"])
            ct = base64.b64decode(value["ciphertext"])
            return aesgcm.decrypt(nonce, ct, associated_data=None).decode("utf-8")
        except Exception as exc:
            logger.error(
                "Transcript decryption failed: exception_type=%s",
                type(exc).__name__,
            )
            return ""
    # Unknown shape — log and return empty so we never accidentally surface
    # encrypted bytes to a caller.
    if isinstance(value, dict):
        logger.warning("Unknown transcript record shape: keys=%s", list(value.keys()))
    return ""


def _maybe_encrypt_call_data(data: dict) -> dict:
    """Return a copy of `data` with the transcript field encrypted if present."""
    if not isinstance(data, dict) or "transcript" not in data:
        return data
    plaintext = data.get("transcript")
    if plaintext is None or plaintext == "":
        return data
    if isinstance(plaintext, dict):
        if _is_valid_encrypted_transcript(plaintext):
            return data
    if not isinstance(plaintext, str):
        if _transcript_encryption_required():
            raise TranscriptEncryptionUnavailableError(
                "Transcript has an unsupported protected-storage shape"
            )
        return data
    encrypted = _encrypt_transcript(plaintext)
    if encrypted is None:
        if _transcript_encryption_required():
            raise TranscriptEncryptionUnavailableError(
                "Transcript encryption is required in this environment"
            )
        return data
    out = dict(data)
    out["transcript"] = encrypted
    return out


def _maybe_decrypt_call_doc(doc: Optional[dict]) -> Optional[dict]:
    """Decrypt the transcript field in a call doc, returning a new dict."""
    if not isinstance(doc, dict):
        return doc
    if "transcript" not in doc:
        return doc
    out = dict(doc)
    out["transcript"] = _decrypt_transcript(doc.get("transcript"))
    return out


async def save_call(call_sid: str, data: dict) -> bool:
    """Save or update a call record. Transcripts are encrypted at rest (F-11)."""
    try:
        db = get_firestore_client()
        data = dict(data)
        data["call_sid"] = call_sid
        data = _maybe_encrypt_call_data(data)
        doc_ref = db.collection(COLLECTION).document(call_sid)
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: doc_ref.set(data, merge=True),
        )
        return True
    except TranscriptEncryptionUnavailableError as error:
        logger.error(
            "Firestore call save failed: exception_type=%s",
            type(error).__name__,
        )
        raise
    except Exception as e:
        logger.error(
            "Firestore call save failed: exception_type=%s",
            type(e).__name__,
        )
        return False


async def get_call(call_sid: str) -> Optional[dict]:
    """Get a call record by SID, decrypting the transcript if needed (F-11)."""
    try:
        db = get_firestore_client()
        doc_ref = db.collection(COLLECTION).document(call_sid)
        doc = await asyncio.get_running_loop().run_in_executor(None, doc_ref.get)
        if doc.exists:
            return _maybe_decrypt_call_doc(doc.to_dict())
    except Exception as e:
        logger.error(
            "Firestore call get failed: exception_type=%s",
            type(e).__name__,
        )
    return None


async def get_call_history(
    e164_phone: str,
    *,
    contractor_id: str,
    limit: int = 10,
) -> list[dict]:
    """Get recent call history for a phone number owned by one contractor.

    Both ``contractor_id`` and ``caller_phone`` are query filters so tenant A's
    outcomes cannot influence tenant B's trust score. A missing contractor
    returns no rows and does not query. Prefers
    ``calls(contractor_id, caller_phone, timestamp DESC)``; a missing composite
    index falls back to the same two equality filters without ``order_by`` and
    sorts in memory. Other Firestore errors fail closed to ``[]``.
    """
    if not contractor_id or not e164_phone:
        return []
    try:
        db = get_firestore_client()
    except Exception as e:
        logger.error(
            "Firestore call history failed: exception_type=%s",
            type(e).__name__,
        )
        return []

    def _tenant_phone_query(*, ordered: bool, fetch_limit: int):
        query = (
            db.collection(COLLECTION)
            .where(filter=FieldFilter("contractor_id", "==", contractor_id))
            .where(filter=FieldFilter("caller_phone", "==", e164_phone))
        )
        if ordered:
            query = query.order_by("timestamp", direction=firestore.Query.DESCENDING)
        return list(query.limit(fetch_limit).stream())

    try:
        docs = _tenant_phone_query(ordered=True, fetch_limit=limit)
    except FailedPrecondition as error:
        logger.warning(
            "Firestore call history index missing; using unordered tenant fallback: %s",
            error,
        )
        try:
            docs = _tenant_phone_query(ordered=False, fetch_limit=max(limit * 5, 50))
            docs.sort(
                key=lambda doc: (doc.to_dict() or {}).get("timestamp", 0),
                reverse=True,
            )
            docs = docs[:limit]
        except Exception as fallback_error:
            logger.error(
                "Firestore call history fallback failed: exception_type=%s",
                type(fallback_error).__name__,
            )
            return []
    except Exception as e:
        logger.error(
            "Firestore call history failed: exception_type=%s",
            type(e).__name__,
        )
        return []
    return [_maybe_decrypt_call_doc(doc.to_dict()) for doc in docs]


async def get_calls_for_contractor(contractor_id: str, limit: int = 100) -> list[dict]:
    """Get recent calls for a specific contractor (transcripts decrypted)."""
    try:
        db = get_firestore_client()
        cutoff = time.time() - (RETENTION_DAYS * 86400)
        docs = (
            db.collection(COLLECTION)
            .where(filter=FieldFilter("contractor_id", "==", contractor_id))
            .where(filter=FieldFilter("timestamp", ">=", cutoff))
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return [_maybe_decrypt_call_doc(doc.to_dict()) for doc in docs]
    except Exception as e:
        logger.error(f"Firestore contractor calls failed: {e}", exc_info=True)
        return []


async def latest_call_timestamp(contractor_id: str) -> Optional[Any]:
    """Newest call timestamp on record for a contractor, or None if there is none.

    Looks back over the full retention window, so None means "no call in the
    last RETENTION_DAYS days". Unlike get_calls_for_contractor this does NOT
    swallow errors: a failed query raises, so that a release decision can
    fail closed instead of mistaking an outage for silence. The raw stored
    value is returned unconverted; callers treat anything non-numeric as
    unreadable.
    """
    db = get_firestore_client()
    cutoff = time.time() - (RETENTION_DAYS * 86400)

    def _query():
        return list(
            db.collection(COLLECTION)
            .where(filter=FieldFilter("contractor_id", "==", contractor_id))
            .where(filter=FieldFilter("timestamp", ">=", cutoff))
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(1)
            .stream()
        )

    docs = await asyncio.get_running_loop().run_in_executor(None, _query)
    if not docs:
        return None
    return (docs[0].to_dict() or {}).get("timestamp")


async def cleanup_old_calls() -> int:
    """Delete call records older than RETENTION_DAYS. Returns count deleted."""
    try:
        db = get_firestore_client()
        cutoff = time.time() - (RETENTION_DAYS * 86400)

        def _sweep_batch() -> int:
            # Synchronous Firestore stream + deletes. Runs in the executor so a
            # 500-document batch cannot stall the event loop that is serving
            # Twilio webhooks (review finding on PR #143).
            docs = (
                db.collection(COLLECTION)
                .where(filter=FieldFilter("timestamp", "<", cutoff))
                .limit(500)
                .stream()
            )
            deleted = 0
            for doc in docs:
                doc.reference.delete()
                deleted += 1
            return deleted

        loop = asyncio.get_event_loop()
        count = await loop.run_in_executor(None, _sweep_batch)
        if count:
            logger.info(f"Cleaned up {count} call records older than {RETENTION_DAYS} days")
        return count
    except Exception as e:
        logger.error(f"Call cleanup failed: {e}", exc_info=True)
        return 0
