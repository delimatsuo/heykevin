"""Firestore state for owner SMS preferences and consent tracking."""

import asyncio
import hashlib
import re
import time
import uuid
from typing import Optional

from google.cloud import firestore

from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger
from app.utils.phone import normalize_phone

logger = get_logger(__name__)

_MESSAGESID_PATTERN = re.compile(r"^(SM|MM)[0-9a-fA-F]{32}$")


def _is_valid_uuid(val: str) -> bool:
    try:
        uuid.UUID(str(val))
        return True
    except (TypeError, ValueError):
        return False


def _validate_and_hash_dedupe_key(dedupe_key: str) -> Optional[str]:
    """Validate dedupe_key format and return deterministic SHA-256 hex string.

    Allowed key formats:
    - inbound:<MessageSid> (MessageSid: SM/MM + 32 hex digits)
    - delivery:<MessageSid> (MessageSid: SM/MM + 32 hex digits)
    - sync:<UUID> or raw UUID
    """
    key = str(dedupe_key or "").strip()
    if not key:
        return None

    if key.startswith("inbound:"):
        sid = key[len("inbound:"):]
        if not _MESSAGESID_PATTERN.match(sid):
            return None
    elif key.startswith("delivery:"):
        sid = key[len("delivery:"):]
        if not _MESSAGESID_PATTERN.match(sid):
            return None
    elif key.startswith("sync:"):
        raw_uuid = key[len("sync:"):]
        if not _is_valid_uuid(raw_uuid):
            return None
    elif _is_valid_uuid(key):
        pass
    else:
        return None

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def resolve_owner_sms_enabled(data: dict) -> bool:
    """Resolve owner_sms_enabled flag: absent means true; malformed fails closed (false)."""
    if "owner_sms_enabled" not in data:
        return True
    val = data["owner_sms_enabled"]
    if isinstance(val, bool):
        return val
    return False


def resolve_owner_sms_opted_out(data: dict) -> bool:
    """Resolve owner_sms_opted_out flag: absent means false; malformed fails closed (blocked / true)."""
    if "owner_sms_opted_out" not in data:
        return False
    val = data["owner_sms_opted_out"]
    if isinstance(val, bool):
        return val
    return True


def resolve_owner_sms_opt_out_revision(data: dict) -> int:
    """Resolve owner_sms_opt_out_revision integer."""
    val = data.get("owner_sms_opt_out_revision")
    if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
        return val
    return 0


def resolve_contractor_owner_sms_identities(data: dict) -> tuple[Optional[str], Optional[str]]:
    """Resolve (owner_phone_e164, twilio_number_e164) for owner SMS delivery and consent.

    - Prefer a nonempty valid owner_phone_e164.
    - If owner_phone_e164 is present but malformed/empty/invalid -> fails closed (None).
    - If owner_phone_e164 is absent -> normalize owner_phone using country_code (absent defaults 'US').
    - Normalize twilio_number.
    - Never fall back to raw string equality.
    """
    owner_e164 = None
    if "owner_phone_e164" in data:
        raw_companion = data.get("owner_phone_e164")
        if isinstance(raw_companion, str) and raw_companion.strip():
            norm_companion = normalize_phone(raw_companion.strip())
            if norm_companion:
                owner_e164 = norm_companion
            else:
                return None, None
        else:
            return None, None
    else:
        raw_owner = data.get("owner_phone")
        if isinstance(raw_owner, str) and raw_owner.strip():
            raw_country = data.get("country_code")
            country = str(raw_country).strip().upper() if raw_country and isinstance(raw_country, str) else "US"
            norm_owner = normalize_phone(raw_owner.strip(), default_region=country)
            if norm_owner:
                owner_e164 = norm_owner

    twilio_e164 = None
    raw_twilio = data.get("twilio_number")
    if isinstance(raw_twilio, str) and raw_twilio.strip():
        norm_twilio = normalize_phone(raw_twilio.strip())
        if norm_twilio:
            twilio_e164 = norm_twilio

    return owner_e164, twilio_e164


async def apply_owner_sms_consent_transition(
    contractor_id: str,
    *,
    opt_out: bool,
    source: str,
    dedupe_key: str,
    expected_owner_phone: str,
    expected_twilio_number: str,
    expected_revision: Optional[int] = None,
) -> tuple[bool, dict]:
    """Atomically record an owner STOP/START or provider 21610 transition.

    - Re-reads contractor doc inside the transaction.
    - Validates matching identities against resolved canonical contractor phones.
    - Prevents duplicate transition replay using deterministic SHA-256 event doc ID.
    - Checks expected_revision to prevent stale callbacks from overwriting newer consent.
    - Increments revision on every new valid transition (even if opt_out bool is unchanged).
    - Never touches client-controlled app switch (owner_sms_enabled).
    - Contains zero PII or message payloads in event records.
    """
    if not contractor_id or not isinstance(contractor_id, str) or not contractor_id.strip():
        return False, {"error": "invalid_contractor_id"}

    safe_event_id = _validate_and_hash_dedupe_key(dedupe_key)
    if not safe_event_id:
        return False, {"error": "invalid_dedupe_key"}

    norm_expected_owner = normalize_phone(str(expected_owner_phone or "").strip())
    norm_expected_twilio = normalize_phone(str(expected_twilio_number or "").strip())
    if not norm_expected_owner or not norm_expected_twilio:
        return False, {"error": "invalid_expected_identities"}

    clean_cid = contractor_id.strip()
    db = get_firestore_client()
    contractor_ref = db.collection("contractors").document(clean_cid)
    event_ref = contractor_ref.collection("owner_sms_events").document(safe_event_id)
    loop = asyncio.get_running_loop()

    def _txn() -> tuple[bool, dict]:
        transaction = db.transaction()

        @firestore.transactional
        def _apply(tx) -> tuple[bool, dict]:
            contractor_snap = contractor_ref.get(transaction=tx)
            if not contractor_snap.exists:
                return False, {"error": "contractor_not_found"}
            data = contractor_snap.to_dict() or {}

            stored_owner, stored_twilio = resolve_contractor_owner_sms_identities(data)
            if not stored_owner or not stored_twilio:
                return False, {"error": "stored_identities_invalid"}

            if stored_owner != norm_expected_owner or stored_twilio != norm_expected_twilio:
                return False, {"error": "identity_mismatch"}

            event_snap = event_ref.get(transaction=tx)
            current_opted_out = resolve_owner_sms_opted_out(data)
            current_rev = resolve_owner_sms_opt_out_revision(data)

            if event_snap.exists:
                return True, {
                    "outcome": "deduplicated",
                    "current_opted_out": current_opted_out,
                    "revision": current_rev,
                }

            if expected_revision is not None and current_rev != expected_revision:
                tx.set(
                    event_ref,
                    {
                        "event_id": safe_event_id,
                        "transition": "superseded",
                        "source": source,
                        "revision_applied": current_rev,
                        "created_at": time.time(),
                    },
                )
                return True, {
                    "outcome": "superseded",
                    "current_opted_out": current_opted_out,
                    "revision": current_rev,
                }

            new_rev = current_rev + 1
            now = time.time()
            tx.update(
                contractor_ref,
                {
                    "owner_sms_opted_out": opt_out,
                    "owner_sms_opt_out_revision": new_rev,
                    "owner_sms_opt_out_source": source,
                    "owner_sms_opt_out_updated_at": now,
                },
            )
            tx.set(
                event_ref,
                {
                    "event_id": safe_event_id,
                    "transition": "opt_out" if opt_out else "opt_in",
                    "source": source,
                    "revision_applied": new_rev,
                    "created_at": now,
                },
            )
            return True, {
                "outcome": "applied",
                "current_opted_out": opt_out,
                "revision": new_rev,
            }

        return _apply(transaction)

    return await loop.run_in_executor(None, _txn)
