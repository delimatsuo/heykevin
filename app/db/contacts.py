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
from collections import Counter
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


CONTACT_SYNC_SUBCOLLECTION = "contact_sync"
_OPEN_CONTACT_SYNC_DOC = "open"


def _open_contact_sync_ref(db, contractor_id: str):
    if not contractor_id:
        raise ValueError("contractor_id is required for contact sync access")
    return (
        db.collection("contractors")
        .document(contractor_id)
        .collection(CONTACT_SYNC_SUBCOLLECTION)
        .document(_OPEN_CONTACT_SYNC_DOC)
    )


async def get_open_contact_sync(contractor_id: str) -> dict:
    """The contact sync this contractor's app is part-way through, or {}.

    Holds the sync's id, the digits of every number its requests delivered so
    far and the time of its last request. Raises on Firestore errors.
    """
    def _read() -> dict:
        doc = _open_contact_sync_ref(get_firestore_client(), contractor_id).get()
        return doc.to_dict() if doc.exists else {}

    return await asyncio.to_thread(_read)


async def save_open_contact_sync(contractor_id: str, state: dict) -> None:
    await asyncio.to_thread(
        lambda: _open_contact_sync_ref(get_firestore_client(), contractor_id).set(state)
    )


async def clear_open_contact_sync(contractor_id: str) -> None:
    await asyncio.to_thread(
        lambda: _open_contact_sync_ref(get_firestore_client(), contractor_id).delete()
    )


async def bulk_sync_contacts(
    contractor_id: str,
    contacts: list[dict],
    *,
    sync_id: str,
    sweep: bool,
    default_region: str = "US",
) -> dict:
    """Bulk sync iPhone contacts for a contractor.

    Marks all provided contacts as whitelisted (source: iphone_sync) and stamps
    them with ``sync_id``. One sync can arrive as several requests that share a
    ``sync_id``. Only the request that completes the sync passes ``sweep=True``,
    which un-whitelists previously synced contacts that this sync did not carry.
    Numbers without a country code are read as ``default_region`` numbers, the
    owner's country. Uses Firestore batch writes (max 500 per batch), off the
    event loop: a 5000-number upload would otherwise stall live calls.

    Returns {"synced": N, "removed": N}.
    """
    return await asyncio.to_thread(
        _bulk_sync_contacts_blocking, contractor_id, contacts, sync_id, sweep, default_region,
    )


def _bulk_sync_contacts_blocking(
    contractor_id: str,
    contacts: list[dict],
    sync_id: str,
    sweep: bool,
    default_region: str,
) -> dict:
    try:
        db = get_firestore_client()
        col_ref = _contacts_ref(db, contractor_id)

        from app.utils.phone import (
            AddressBook, country_of, describe_unreadable_numbers, normalize_phone, read_address_book,
        )
        phones = [c.get("phone", "") for c in contacts]
        try:
            book = read_address_book(phones, default_region)
        except Exception as e:  # noqa: BLE001 - whitelist what the plain reading can read
            logger.warning(f"Address book reading failed for {contractor_id}: {type(e).__name__}")
            book = AddressBook(readings=[
                [normalized] if (normalized := normalize_phone(phone, default_region)) else []
                for phone in phones
            ])
        valid_contacts = []
        unreadable = []
        readable_countries = Counter()
        for c, phone, readings in zip(contacts, phones, book.readings):
            if not readings:
                unreadable.append(phone)
            for reading in readings:
                valid_contacts.append({"name": c.get("name", ""), "phone": reading})
                readable_countries[country_of(reading)] += 1
        read = sum(1 for readings in book.readings if readings)
        if book.ninth_digit or book.as_second_country or book.both_ways:
            # Counts only, like the summary below.
            logger.info(
                f"Bulk sync for {contractor_id}: {book.ninth_digit} old Brazilian mobiles read "
                f"with their 9; {book.as_second_country} numbers unreadable as {default_region} "
                f"numbers read as {book.second_country} numbers; {book.both_ways} numbers read "
                f"both as {default_region} and as {book.second_country} numbers"
            )
        if unreadable:
            # Counts only: why these numbers were dropped, and which countries the
            # readable ones belong to, to tell how the unreadable ones were written.
            # A log line must never fail the sync.
            try:
                logger.info(
                    f"Bulk sync for {contractor_id}: {len(unreadable)} of {len(contacts)} numbers "
                    f"unreadable as {default_region} numbers: "
                    f"{describe_unreadable_numbers(unreadable, default_region)}; "
                    f"readable numbers by country: {dict(readable_countries.most_common(5))}"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Unreadable number summary failed for {contractor_id}: {type(e).__name__}")

        # Batch write new/updated contacts. A number read two ways is one contact
        # and two whitelist entries; synced counts contacts.
        synced_doc_ids = set()
        batch = db.batch()
        batch_count = 0

        for contact in valid_contacts:
            doc_id = phone_hash(contact["phone"])
            synced_doc_ids.add(doc_id)
            batch.set(col_ref.document(doc_id), {
                "phone": contact["phone"],
                "name": contact["name"],
                "is_whitelisted": True,
                "is_blacklisted": False,
                "source": "iphone_sync",
                "sync_id": sync_id,
            }, merge=True)
            batch_count += 1

            if batch_count >= 500:
                batch.commit()
                batch = db.batch()
                batch_count = 0

        # Un-whitelist stale contacts: synced before, but not carried by this sync.
        # This request's upserts may still be uncommitted, so the stream shows
        # their old sync_id; skip them by id rather than by sync_id.
        removed = 0
        if sweep:
            for doc in col_ref.where("source", "==", "iphone_sync").stream():
                doc_data = doc.to_dict()
                if (
                    doc.id in synced_doc_ids
                    or doc_data.get("sync_id") == sync_id
                    or not doc_data.get("is_whitelisted")
                ):
                    continue
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
