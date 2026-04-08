"""Firestore operations for contacts.

Contacts are stored as subcollections under each contractor:
  contractors/{contractor_id}/contacts/{phone_hash}

For backward compatibility, if no contractor_id is provided,
falls back to the legacy global 'contacts' collection.
"""

from typing import Optional

from app.db.firestore_client import get_firestore_client
from app.utils.phone import phone_hash
from app.utils.logging import get_logger

logger = get_logger(__name__)

LEGACY_COLLECTION = "contacts"


def _contacts_ref(db, contractor_id: str = ""):
    """Return the contacts collection reference, scoped to contractor if provided."""
    if contractor_id:
        return db.collection("contractors").document(contractor_id).collection("contacts")
    return db.collection(LEGACY_COLLECTION)


async def get_contact(e164_phone: str, contractor_id: str = "") -> Optional[dict]:
    """Look up a contact by phone number. Returns None if not found."""
    try:
        db = get_firestore_client()
        doc = _contacts_ref(db, contractor_id).document(phone_hash(e164_phone)).get()
        if doc.exists:
            return doc.to_dict()
        # Fallback: check legacy global collection if contractor-scoped not found
        if contractor_id:
            legacy_doc = db.collection(LEGACY_COLLECTION).document(phone_hash(e164_phone)).get()
            if legacy_doc.exists:
                return legacy_doc.to_dict()
    except Exception as e:
        logger.error(f"Firestore contact lookup failed: {e}", exc_info=True)
    return None


async def upsert_contact(e164_phone: str, data: dict, contractor_id: str = ""):
    """Create or update a contact."""
    try:
        db = get_firestore_client()
        doc_id = phone_hash(e164_phone)
        data["phone"] = e164_phone
        _contacts_ref(db, contractor_id).document(doc_id).set(data, merge=True)
    except Exception as e:
        logger.error(f"Firestore contact upsert failed: {e}", exc_info=True)


async def list_contacts(contractor_id: str = "", limit: int = 100) -> list[dict]:
    """List all contacts for a contractor."""
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
