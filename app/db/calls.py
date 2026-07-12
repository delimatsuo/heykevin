"""Firestore operations for call records.

Call transcripts are sensitive (legal/medical/financial discussion in
residential-services calls). They are encrypted at rest with AES-256-GCM
on top of Firestore's default at-rest encryption (SECURITY_AUDIT.md F-11).
The encryption key is loaded from `TRANSCRIPT_ENCRYPTION_KEY` at process
start; if unset, transcripts are written in plaintext (legacy behaviour)
and reads remain backwards compatible with both formats.
"""

import asyncio
import base64
import os
import secrets as _secrets
import time
from typing import Any, Optional

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.config import settings
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


def _load_transcript_key() -> Optional[bytes]:
    """Return the 32-byte AES-256-GCM key, or None if encryption is disabled.

    The key is read from `settings.transcript_encryption_key` (env var
    TRANSCRIPT_ENCRYPTION_KEY) as a base64-encoded string. Invalid or
    short keys are rejected so we never silently fall back to a weak key.
    """
    raw = (settings.transcript_encryption_key or os.environ.get("TRANSCRIPT_ENCRYPTION_KEY", "") or "").strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:
        logger.error("TRANSCRIPT_ENCRYPTION_KEY is not valid base64 — refusing to encrypt")
        return None
    if len(key) != 32:
        logger.error(
            "TRANSCRIPT_ENCRYPTION_KEY must decode to 32 bytes (got %d) — refusing to encrypt",
            len(key),
        )
        return None
    return key


def _encrypt_transcript(plaintext: str) -> Optional[dict]:
    """Encrypt `plaintext` with AES-256-GCM. Returns the on-disk dict.

    Returns None if encryption is disabled or fails (caller falls back to
    storing plaintext, preserving legacy behaviour).
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
        logger.error(f"Transcript encryption failed: {exc}", exc_info=True)
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
            logger.error(f"Transcript decryption failed: {exc}")
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
    if not plaintext or not isinstance(plaintext, str):
        return data
    encrypted = _encrypt_transcript(plaintext)
    if encrypted is None:
        return data  # encryption disabled or failed — fall back to plaintext
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
    except Exception as e:
        logger.error(
            "Firestore call save failed exception_type=%s",
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
        logger.error(f"Firestore call get failed: {e}", exc_info=True)
    return None


async def get_call_history(e164_phone: str, limit: int = 10) -> list[dict]:
    """Get recent call history for a phone number (transcripts decrypted)."""
    try:
        db = get_firestore_client()
        docs = (
            db.collection(COLLECTION)
            .where(filter=FieldFilter("caller_phone", "==", e164_phone))
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return [_maybe_decrypt_call_doc(doc.to_dict()) for doc in docs]
    except Exception as e:
        logger.error(f"Firestore call history failed: {e}", exc_info=True)
        return []


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


async def cleanup_old_calls() -> int:
    """Delete call records older than RETENTION_DAYS. Returns count deleted."""
    try:
        db = get_firestore_client()
        cutoff = time.time() - (RETENTION_DAYS * 86400)
        docs = (
            db.collection(COLLECTION)
            .where(filter=FieldFilter("timestamp", "<", cutoff))
            .limit(500)
            .stream()
        )
        count = 0
        for doc in docs:
            doc.reference.delete()
            count += 1
        if count:
            logger.info(f"Cleaned up {count} call records older than {RETENTION_DAYS} days")
        return count
    except Exception as e:
        logger.error(f"Call cleanup failed: {e}", exc_info=True)
        return 0
