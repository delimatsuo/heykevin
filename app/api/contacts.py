"""Contact management API — CRUD, whitelist, blacklist."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.middleware.auth import verify_api_token
from app.db.contacts import get_contact, upsert_contact, list_contacts
from app.utils.phone import normalize_phone, phone_hash
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/contacts", dependencies=[Depends(verify_api_token)])


class ContactCreate(BaseModel):
    phone: str
    name: str = ""
    trust_level: int = 50
    tags: list = []
    is_whitelisted: bool = False
    is_blacklisted: bool = False


class ContactUpdate(BaseModel):
    name: Optional[str] = None
    trust_level: Optional[int] = None
    tags: Optional[list] = None
    is_whitelisted: Optional[bool] = None
    is_blacklisted: Optional[bool] = None


@router.get("")
async def api_list_contacts():
    """List all contacts."""
    contacts = await list_contacts()
    return {"contacts": contacts}


@router.post("")
async def api_create_contact(body: ContactCreate):
    """Create or update a contact."""
    phone = normalize_phone(body.phone)
    if not phone:
        return {"error": "Invalid phone number"}, 400

    data = body.dict()
    data["phone"] = phone
    await upsert_contact(phone, data)
    return {"status": "ok", "phone": phone}


@router.get("/{phone}")
async def api_get_contact(phone: str):
    """Get a contact by phone number."""
    normalized = normalize_phone(phone)
    if not normalized:
        return {"error": "Invalid phone number"}

    contact = await get_contact(normalized)
    if not contact:
        return {"error": "Not found"}, 404
    return contact


@router.post("/{phone}/whitelist")
async def api_whitelist(phone: str):
    """Add a number to the whitelist."""
    normalized = normalize_phone(phone)
    if not normalized:
        return {"error": "Invalid phone number"}

    await upsert_contact(normalized, {
        "is_whitelisted": True,
        "is_blacklisted": False,
        "trust_level": 100,
    })
    logger.info(f"Whitelisted: {normalized}")
    return {"status": "whitelisted", "phone": normalized}


@router.post("/{phone}/blacklist")
async def api_blacklist(phone: str):
    """Add a number to the blacklist."""
    normalized = normalize_phone(phone)
    if not normalized:
        return {"error": "Invalid phone number"}

    await upsert_contact(normalized, {
        "is_blacklisted": True,
        "is_whitelisted": False,
        "trust_level": 0,
    })
    logger.info(f"Blacklisted: {normalized}")
    return {"status": "blacklisted", "phone": normalized}
