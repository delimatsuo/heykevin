"""Firestore operations for contacts.

Contacts are stored as subcollections under each contractor:
  contractors/{contractor_id}/contacts/{phone_hash}

Legacy global ``contacts`` and ``caller_contacts`` documents are quarantined:
runtime reads require a contractor and never fall back across tenant boundaries.

Caller-contact records (auto-extracted name/business/issue summary from a
prior screening transcript) live in a separate collection. F-14 moves these
from the global ``caller_contacts/{phone}`` layout to a per-contractor
subcollection ``contractors/{contractor_id}/caller_contacts/{phone_key}`` so
one tenant's extracted call data cannot bleed into another tenant's caller
display. Helpers below are the single source of truth for read/write so
webhook code does not have to assemble the path.
"""

import asyncio
from typing import Optional

from app.db.firestore_client import get_firestore_client
from app.utils.phone import phone_hash
from app.utils.logging import get_logger

logger = get_logger(__name__)

CALLER_CONTACTS_SUBCOLLECTION = "caller_contacts"
CALLER_CONTACT_PROVENANCE_SCHEMA = 1
CALLER_CONTACT_PROVENANCE_SOURCE = "tenant_post_call"


def caller_contact_key(e164_or_raw_phone: str) -> str:
    """Deterministic document key for a caller phone.

    Mirrors the historical ``phone.replace("+","").replace("-","").replace(" ","")``
    convention so the migration script and live readers/writers agree on doc
    IDs. We deliberately keep the digits-only form rather than a SHA hash to
    keep migrations introspectable; the data is per-contractor scoped now so
    the doc id is no longer global PII material on its own.
    """
    return (
        (e164_or_raw_phone or "")
        .replace("+", "")
        .replace("-", "")
        .replace(" ", "")
        .replace("(", "")
        .replace(")", "")
    )


def _contacts_ref(db, contractor_id: str):
    """Return one contractor's contacts collection.

    The top-level legacy collection is deliberately not reachable through this
    helper. A tenant miss must remain a miss; otherwise one owner's saved name,
    whitelist, or blacklist can affect another owner's call.
    """
    if not contractor_id:
        raise ValueError("contractor_id is required for contact access")
    return db.collection("contractors").document(contractor_id).collection("contacts")


async def get_contact(e164_phone: str, contractor_id: str = "") -> Optional[dict]:
    """Look up a tenant-scoped contact. Returns None if unbound or not found."""
    if not contractor_id or not e164_phone:
        return None
    try:
        db = get_firestore_client()
        doc = _contacts_ref(db, contractor_id).document(phone_hash(e164_phone)).get()
        if doc.exists:
            return doc.to_dict()
    except Exception as e:
        logger.error(f"Firestore contact lookup failed: {e}", exc_info=True)
    return None


async def upsert_contact(e164_phone: str, data: dict, contractor_id: str = ""):
    """Create or update a tenant-scoped contact."""
    if not contractor_id or not e164_phone:
        logger.warning("Refusing unscoped contact write")
        return
    try:
        db = get_firestore_client()
        doc_id = phone_hash(e164_phone)
        data["phone"] = e164_phone
        _contacts_ref(db, contractor_id).document(doc_id).set(data, merge=True)
    except Exception as e:
        logger.error(f"Firestore contact upsert failed: {e}", exc_info=True)


async def list_contacts(contractor_id: str = "", limit: int = 100) -> list[dict]:
    """List contacts for one contractor; unscoped reads fail closed."""
    if not contractor_id:
        return []
    try:
        db = get_firestore_client()
        docs = _contacts_ref(db, contractor_id).limit(limit).stream()
        return [doc.to_dict() for doc in docs]
    except Exception as e:
        logger.error(f"Firestore contact list failed: {e}", exc_info=True)
        return []


async def bulk_sync_contacts(
    contractor_id: str,
    contacts: list[dict],
) -> dict:
    """Bulk sync iPhone contacts for a contractor.

    Marks all provided contacts as whitelisted (source: iphone_sync).
    Un-whitelists previously synced contacts that are no longer in the list.
    Uses Firestore batch writes (max 500 per batch).

    Returns {"synced": N, "removed": N}.
    """
    try:
        db = get_firestore_client()
        col_ref = _contacts_ref(db, contractor_id)

        # Get existing iphone_sync contacts
        existing_docs = list(col_ref.where("source", "==", "iphone_sync").stream())
        existing_phones = {doc.to_dict().get("phone", "") for doc in existing_docs}

        # Build set of new phones
        from app.utils.phone import normalize_phone
        new_phones = set()
        valid_contacts = []
        for c in contacts:
            normalized = normalize_phone(c.get("phone", ""))
            if normalized:
                new_phones.add(normalized)
                valid_contacts.append({"name": c.get("name", ""), "phone": normalized})

        # Batch write new/updated contacts
        synced = 0
        batch = db.batch()
        batch_count = 0

        for contact in valid_contacts:
            doc_ref = col_ref.document(phone_hash(contact["phone"]))
            batch.set(doc_ref, {
                "phone": contact["phone"],
                "name": contact["name"],
                "is_whitelisted": True,
                "is_blacklisted": False,
                "source": "iphone_sync",
            }, merge=True)
            batch_count += 1
            synced += 1

            if batch_count >= 500:
                batch.commit()
                batch = db.batch()
                batch_count = 0

        # Un-whitelist stale contacts (were synced before but no longer in phone)
        removed = 0
        stale_phones = existing_phones - new_phones
        for doc in existing_docs:
            doc_data = doc.to_dict()
            if doc_data.get("phone", "") in stale_phones:
                batch.update(doc.reference, {"is_whitelisted": False})
                batch_count += 1
                removed += 1

                if batch_count >= 500:
                    batch.commit()
                    batch = db.batch()
                    batch_count = 0

        # Commit remaining
        if batch_count > 0:
            batch.commit()

        logger.info(f"Bulk sync for {contractor_id}: synced={synced}, removed={removed}")
        return {"synced": synced, "removed": removed}

    except Exception as e:
        logger.error(f"Bulk contact sync failed: {e}", exc_info=True)
        return {"synced": 0, "removed": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# Caller-contact (auto-extracted) helpers — F-14 per-tenant scope.
# ---------------------------------------------------------------------------


def _caller_contact_doc(db, contractor_id: str, phone_key: str):
    """Return the per-contractor caller_contacts doc reference."""
    return (
        db.collection("contractors")
        .document(contractor_id)
        .collection(CALLER_CONTACTS_SUBCOLLECTION)
        .document(phone_key)
    )


def _caller_contact_has_tenant_provenance(data: object, contractor_id: str) -> bool:
    """Return whether a caller-contact row was freshly bound by this tenant."""
    return (
        isinstance(data, dict)
        and type(data.get("provenance_schema")) is int
        and data.get("provenance_schema") == CALLER_CONTACT_PROVENANCE_SCHEMA
        and data.get("provenance_source") == CALLER_CONTACT_PROVENANCE_SOURCE
        and data.get("provenance_contractor_id") == contractor_id
    )


async def get_caller_contact(contractor_id: str, phone: str) -> Optional[dict]:
    """Read a caller-contact record scoped to a contractor.

    Legacy global records are intentionally not used. Returns None on miss or
    on Firestore failure (caller treats missing data as "unknown caller").
    """
    if not contractor_id or not phone:
        return None
    try:
        db = get_firestore_client()
        phone_key = caller_contact_key(phone)
        loop = asyncio.get_event_loop()
        doc = await loop.run_in_executor(
            None, lambda: _caller_contact_doc(db, contractor_id, phone_key).get()
        )
        if doc.exists:
            data = doc.to_dict() or {}
            if _caller_contact_has_tenant_provenance(data, contractor_id):
                return data
    except Exception as e:  # noqa: BLE001 — caller handles "unknown caller" sentinel.
        logger.warning("caller_contacts: lookup failed contractor=%s err=%s", contractor_id, e)
    return None


async def upsert_caller_contact(
    contractor_id: str,
    phone: str,
    data: dict,
    *,
    merge: bool = True,
) -> bool:
    """Write a caller-contact record under the contractor's subcollection.

    Returns True on success, False on misconfiguration or Firestore failure.
    """
    if not contractor_id or not phone:
        logger.warning(
            "caller_contacts: refusing to write without contractor_id+phone "
            "(F-14 per-tenant scope)"
        )
        return False
    try:
        db = get_firestore_client()
        phone_key = caller_contact_key(phone)
        loop = asyncio.get_event_loop()
        ref = _caller_contact_doc(db, contractor_id, phone_key)
        payload = dict(data)
        payload.update(
            {
                "provenance_schema": CALLER_CONTACT_PROVENANCE_SCHEMA,
                "provenance_source": CALLER_CONTACT_PROVENANCE_SOURCE,
                "provenance_contractor_id": contractor_id,
            }
        )

        def _write():
            # Never bless an unproven/mismatched row by merging provenance into
            # it. Replace it with only the current tenant-bound observation.
            existing = ref.get() if merge else None
            safe_merge = bool(
                existing
                and existing.exists
                and _caller_contact_has_tenant_provenance(
                    existing.to_dict() or {},
                    contractor_id,
                )
            )
            ref.set(payload, merge=safe_merge)

        await loop.run_in_executor(None, _write)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(
            "caller_contacts: upsert failed contractor=%s err=%s",
            contractor_id,
            e,
            exc_info=True,
        )
        return False
