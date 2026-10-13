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


async def collect_canada_owner_sms_consent(
    contractor_id: str, *, disclosure_version: str, owner_phone_e164: str,
    twilio_number: str, expected_revision: int,
) -> tuple[bool, dict]:
    """Bind express consent to fresh server identities without clearing STOP or revision."""
    from datetime import datetime, timezone
    from google.cloud.firestore_v1.base_query import FieldFilter
    from app.services.canada_sms_consent import (
        DISCLOSURE_VERSION, canadian_assignment_valid, valid_canada_consent,
    )

    if (not isinstance(contractor_id, str) or not contractor_id.strip()
            or disclosure_version != DISCLOSURE_VERSION
            or type(expected_revision) is not int or expected_revision < 0):
        return False, {"error": "invalid_consent_request"}
    db = get_firestore_client()
    ref = db.collection("contractors").document(contractor_id)

    def run():
        @firestore.transactional
        def apply(tx):
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False, {"error": "contractor_not_found"}
            data = snap.to_dict() or {}
            owner, sender = resolve_contractor_owner_sms_identities(data)
            if (owner_phone_e164 != owner or twilio_number != sender
                    or expected_revision != data.get("owner_sms_opt_out_revision")):
                return False, {"error": "stale_consent_snapshot"}
            if not canadian_assignment_valid(data, require_enabled=False):
                return False, {"error": "consent_not_eligible"}
            # Duplicate assignment protection is in the same serializable transaction.
            query = db.collection("contractors").where(
                filter=FieldFilter("twilio_number", "==", sender)
            ).limit(2)
            assignments = list(query.stream(transaction=tx))
            if len(assignments) != 1 or assignments[0].id != contractor_id:
                return False, {"error": "assignment_ambiguous"}
            data["contractor_id"] = contractor_id
            if valid_canada_consent(data):
                # A retry preserves the original express-consent facts and collection time.
                consent = dict(data["owner_sms_consent"])
            else:
                consent = {
                    "contractor_id": contractor_id,
                    "disclosure_version": DISCLOSURE_VERSION,
                    "owner_phone_e164": owner, "twilio_number": sender,
                    "source": "ios_settings", "collected_at": datetime.now(timezone.utc),
                    "opt_out_revision": expected_revision,
                }
            consent["delivery_state"] = "pending"
            tx.update(ref, {"owner_sms_enabled": True, "owner_sms_consent": consent})
            return True, {"status": "pending", "accepted_consent": dict(consent)}
        return apply(db.transaction())

    return await asyncio.get_running_loop().run_in_executor(None, run)


async def verify_canada_owner_sms_delivery(
    contractor_id: str, *, accepted_consent: dict, account_sid: str, messaging_service_sid: str,
) -> tuple[bool, dict]:
    """Promote only the exact accepted consent after independent enrollment readback.

    This function receives pinned enrollment configuration, and never contacts Twilio.
    A newer consent, STOP epoch, recipient, sender or configuration invalidates promotion.
    """
    from datetime import datetime, timezone
    from google.cloud.firestore_v1.base_query import FieldFilter
    from app.config import PRODUCTION_GCP_PROJECT_ID, settings
    from app.services.canada_sms_consent import (
        canada_configuration_valid, canadian_assignment_valid, valid_canada_consent,
    )

    def config_matches():
        return (canada_configuration_valid() and settings.twilio_account_sid == account_sid
                and settings.canada_messaging_service_sid == messaging_service_sid)

    if not config_matches() or not isinstance(accepted_consent, dict):
        return False, {"error": "consent_delivery_unavailable"}
    db = get_firestore_client()
    if db.project != PRODUCTION_GCP_PROJECT_ID:
        return False, {"error": "consent_delivery_unavailable"}
    ref = db.collection("contractors").document(contractor_id)

    def run():
        @firestore.transactional
        def apply(tx):
            if not config_matches():
                return False, {"error": "consent_delivery_unavailable"}
            snap = ref.get(transaction=tx)
            if not snap.exists:
                return False, {"error": "consent_state_changed"}
            data = snap.to_dict() or {}
            data["contractor_id"] = contractor_id
            if (data.get("owner_sms_consent") != accepted_consent
                    or accepted_consent.get("delivery_state") != "pending"
                    or not canadian_assignment_valid(data) or not valid_canada_consent(data)):
                return False, {"error": "consent_state_changed"}
            query = db.collection("contractors").where(filter=FieldFilter(
                "twilio_number", "==", accepted_consent["twilio_number"],
            )).limit(2)
            assignments = list(query.stream(transaction=tx))
            if len(assignments) != 1 or assignments[0].id != contractor_id:
                return False, {"error": "assignment_ambiguous"}
            consent = dict(accepted_consent)
            consent.update({"delivery_state": "verified", "account_sid": account_sid,
                "messaging_service_sid": messaging_service_sid, "verified_at": datetime.now(timezone.utc)})
            if not config_matches():
                return False, {"error": "consent_delivery_unavailable"}
            tx.update(ref, {"owner_sms_consent": consent})
            return True, {"status": "verified"}
        return apply(db.transaction())

    return await asyncio.get_running_loop().run_in_executor(None, run)
