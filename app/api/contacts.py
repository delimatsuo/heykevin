"""Contact management API — CRUD, whitelist, blacklist, bulk sync."""

import hashlib
import secrets
import time

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from typing import Optional

from app.middleware.auth import verify_api_token, require_contractor_access
from app.db.contacts import (
    bulk_sync_contacts,
    clear_open_contact_sync,
    get_contact,
    get_open_contact_sync,
    list_contacts,
    save_open_contact_sync,
    upsert_contact,
)
from app.utils.phone import normalize_phone
from app.utils.logging import get_logger, redact_phone

logger = get_logger(__name__)

router = APIRouter(prefix="/api/contacts", dependencies=[Depends(verify_api_token)])

# Rate limit: track last bulk sync per contractor
_last_sync: dict[str, float] = {}
SYNC_COOLDOWN = 300  # 5 minutes
# Installed iOS builds upload an address book of more than 200 numbers as
# back-to-back 200-number requests and send contacts_hash only on the last one.
# A request that arrives within this many seconds of the previous request of an
# unfinished sync continues it instead of starting a new one, so the cooldown
# does not drop the later requests. Equal to the cooldown, so a request that is
# too late to continue a sync is never also too early to start one.
SYNC_BATCH_WINDOW = SYNC_COOLDOWN
# Across every request of one sync. Installed builds stop reading contacts once
# past 5000 numbers but finish the contact that crossed it, so allow some slack.
MAX_CONTACTS_PER_SYNC = 6000


def _digits(phone: str) -> str:
    """A number as the iOS app hashes it: its raw string reduced to digits.

    From the raw string, not the normalized number: the app also hashes numbers
    the server rejects as invalid.
    """
    return "".join(ch for ch in phone if ch.isnumeric())


def _client_list_hash(digit_strings: list[str]) -> str:
    """contacts_hash as ``ContactSyncManager`` computes it: SHA-256 of the
    digits-only numbers, sorted and comma-joined."""
    return hashlib.sha256(",".join(sorted(digit_strings)).encode()).hexdigest()


class ContactCreate(BaseModel):
    phone: str
    name: str = ""
    trust_level: int = 50
    tags: list = []
    is_whitelisted: bool = False
    is_blacklisted: bool = False
    contractor_id: str = ""


class ContactUpdate(BaseModel):
    name: Optional[str] = None
    trust_level: Optional[int] = None
    tags: Optional[list] = None
    is_whitelisted: Optional[bool] = None
    is_blacklisted: Optional[bool] = None


class BulkSyncContact(BaseModel):
    name: str = ""
    phone: str


class BulkSyncRequest(BaseModel):
    contacts: list[BulkSyncContact] = Field(max_length=5000)
    contractor_id: str
    contacts_hash: str = ""  # SHA-256 of sorted phone list for diff check


@router.get("")
async def api_list_contacts(request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """List all contacts for a contractor."""
    require_contractor_access(request, contractor_id)
    contacts = await list_contacts(contractor_id=contractor_id)
    return {"contacts": contacts}


@router.post("")
async def api_create_contact(request: Request, body: ContactCreate):
    """Create or update a contact."""
    if not body.contractor_id:
        return {"error": "contractor_id required"}, 400
    require_contractor_access(request, body.contractor_id)

    phone = normalize_phone(body.phone)
    if not phone:
        return {"error": "Invalid phone number"}, 400

    data = body.dict(exclude={"contractor_id"})
    data["phone"] = phone
    await upsert_contact(phone, data, contractor_id=body.contractor_id)
    return {"status": "ok", "phone": phone}


@router.get("/{phone}")
async def api_get_contact(phone: str, request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """Get a contact by phone number."""
    require_contractor_access(request, contractor_id)
    normalized = normalize_phone(phone)
    if not normalized:
        return {"error": "Invalid phone number"}

    contact = await get_contact(normalized, contractor_id=contractor_id)
    if not contact:
        return {"error": "Not found"}, 404
    return contact


@router.post("/{phone}/whitelist")
async def api_whitelist(phone: str, request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """Add a number to the whitelist."""
    require_contractor_access(request, contractor_id)
    normalized = normalize_phone(phone)
    if not normalized:
        return {"error": "Invalid phone number"}

    await upsert_contact(normalized, {
        "is_whitelisted": True,
        "is_blacklisted": False,
        "trust_level": 100,
    }, contractor_id=contractor_id)
    logger.info(f"Whitelisted: {redact_phone(normalized)}")
    return {"status": "whitelisted", "phone": normalized}


@router.post("/{phone}/blacklist")
async def api_blacklist(phone: str, request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """Add a number to the blacklist."""
    require_contractor_access(request, contractor_id)
    normalized = normalize_phone(phone)
    if not normalized:
        return {"error": "Invalid phone number"}

    await upsert_contact(normalized, {
        "is_blacklisted": True,
        "is_whitelisted": False,
        "trust_level": 0,
    }, contractor_id=contractor_id)
    logger.info(f"Blacklisted: {redact_phone(normalized)}")
    return {"status": "blacklisted", "phone": normalized}


@router.post("/bulk-sync")
async def api_bulk_sync(request: Request, body: BulkSyncRequest):
    """Bulk sync iPhone contacts for a contractor. Marks all as whitelisted.

    A request that carries contacts_hash completes a sync; a request without it
    is one part of a larger sync that a later request completes. Contacts the
    sync did not carry are un-whitelisted only when it completes and the numbers
    the server received hash to contacts_hash, proving none of its requests was
    lost, rejected or failed.

    Optimized with server-side hash: if a sync arrives as one request with the
    same contacts_hash as what's stored, it is skipped (0 Firestore writes).
    """
    if not body.contractor_id:
        return {"error": "contractor_id required"}, 400
    require_contractor_access(request, body.contractor_id)

    # Rate limit: check last_sync_at from contractor doc (persists across deploys)
    from app.db.contractors import get_contractor, update_contractor
    contractor = await get_contractor(body.contractor_id) or {}
    now = time.time()
    completes_sync = bool(body.contacts_hash)

    try:
        open_sync = await get_open_contact_sync(body.contractor_id)
    except Exception as e:  # noqa: BLE001 - fail the request; the app uploads again later
        logger.error(f"Open contact sync read failed for {body.contractor_id}: {type(e).__name__}")
        return {"status": "error", "synced": 0, "removed": 0}

    sync_id = open_sync.get("sync_id") or ""
    received = list(open_sync.get("phones") or [])
    if not sync_id or now - (open_sync.get("last_request_at") or 0) > SYNC_BATCH_WINDOW:
        # This request starts a new sync.
        last_sync = contractor.get("last_sync_at", 0)
        if now - last_sync < SYNC_COOLDOWN:
            remaining = int(SYNC_COOLDOWN - (now - last_sync))
            logger.info(f"Bulk sync rate limited for {body.contractor_id}: retry in {remaining}s")
            return {"status": "rate_limited", "retry_after_seconds": remaining}

        # Hash check: skip sync if contacts haven't changed. Only a hash this code
        # verified counts; the contacts_hash field written before it was also
        # stored for lists the server never fully received.
        if completes_sync and body.contacts_hash == contractor.get("contacts_list_hash", ""):
            logger.info(f"Bulk sync skipped for {body.contractor_id}: hash match")
            return {"status": "skipped", "reason": "no_changes"}

        sync_id = secrets.token_hex(8)
        received = []

    if len(received) + len(body.contacts) > MAX_CONTACTS_PER_SYNC:
        logger.warning(f"Bulk sync rejected for {body.contractor_id}: over {MAX_CONTACTS_PER_SYNC} contacts")
        return {"status": "rejected", "reason": "too_many_contacts"}
    received += [_digits(c.phone) for c in body.contacts]
    complete = completes_sync and _client_list_hash(received) == body.contacts_hash

    contacts_data = [{"name": c.name, "phone": c.phone} for c in body.contacts]
    result = await bulk_sync_contacts(
        body.contractor_id,
        contacts_data,
        sync_id=sync_id,
        sweep=complete,
        default_region=(contractor.get("country_code") or "US").upper(),
    )
    failed = "error" in result

    updates = {"last_sync_at": now}
    if complete and not failed:
        updates["contacts_list_hash"] = body.contacts_hash
    await update_contractor(body.contractor_id, updates)

    # Record only numbers that were written: a request that failed leaves the
    # received list short, so this sync can no longer prove it is complete.
    try:
        if completes_sync:
            await clear_open_contact_sync(body.contractor_id)
        elif not failed:
            await save_open_contact_sync(body.contractor_id, {
                "sync_id": sync_id,
                "phones": received,
                "last_request_at": now,
            })
    except Exception as e:  # noqa: BLE001 - an unrecorded request only stops this sync's sweep
        logger.error(f"Open contact sync write failed for {body.contractor_id}: {type(e).__name__}")

    if failed:
        return {"status": "error", "synced": 0, "removed": 0}
    if completes_sync and not complete:
        logger.warning(
            f"Contact sync for {body.contractor_id} ended without its full list "
            f"({len(received)} numbers received); kept existing whitelist"
        )
    logger.info(f"Bulk sync request for {body.contractor_id} (completes sync: {completes_sync}): {result}")
    return {"status": "ok", **result}
