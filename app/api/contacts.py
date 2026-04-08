"""Contact management API — CRUD, whitelist, blacklist, bulk sync."""

import hashlib
import time

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from typing import Optional

from app.middleware.auth import verify_api_token, require_contractor_access
from app.db.contacts import get_contact, upsert_contact, list_contacts, bulk_sync_contacts
from app.utils.phone import normalize_phone
from app.utils.logging import get_logger, redact_phone

logger = get_logger(__name__)

router = APIRouter(prefix="/api/contacts", dependencies=[Depends(verify_api_token)])

# Rate limit: track last bulk sync per contractor
_last_sync: dict[str, float] = {}
SYNC_COOLDOWN = 300  # 5 minutes


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

    Optimized with server-side hash: if the client sends the same contacts_hash
    as what's stored, the sync is skipped entirely (0 Firestore writes).
    """
    if not body.contractor_id:
        return {"error": "contractor_id required"}, 400
    require_contractor_access(request, body.contractor_id)

    # Rate limit: check last_sync_at from contractor doc (persists across deploys)
    from app.db.contractors import get_contractor, update_contractor
    contractor = await get_contractor(body.contractor_id)
    if contractor:
        last_sync = contractor.get("last_sync_at", 0)
        if time.time() - last_sync < SYNC_COOLDOWN:
            remaining = int(SYNC_COOLDOWN - (time.time() - last_sync))
            return {"status": "rate_limited", "retry_after_seconds": remaining}

        # Hash check: skip sync if contacts haven't changed
        if body.contacts_hash and body.contacts_hash == contractor.get("contacts_hash", ""):
            logger.info(f"Bulk sync skipped for {body.contractor_id}: hash match")
            return {"status": "skipped", "reason": "no_changes"}

    contacts_data = [{"name": c.name, "phone": c.phone} for c in body.contacts]
    result = await bulk_sync_contacts(body.contractor_id, contacts_data)

    # Persist hash and sync timestamp in contractor doc
    await update_contractor(body.contractor_id, {
        "contacts_hash": body.contacts_hash,
        "last_sync_at": time.time(),
    })

    logger.info(f"Bulk sync for {body.contractor_id}: {result}")
    return {"status": "ok", **result}
