"""Contractor profile management in Firestore."""

import asyncio
import secrets
from typing import Optional
from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger, redact_phone

logger = get_logger(__name__)

COLLECTION = "contractors"

# Supported countries for Kevin AI
SUPPORTED_COUNTRIES = {"US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"}

# Countries that require Twilio regulatory bundles for number provisioning
REGULATORY_COUNTRIES = {"DE", "FR", "IT", "ES", "PT", "BR"}

# Country code to full name mapping
COUNTRY_NAMES = {
    "US": "United States",
    "CA": "Canada",
    "BR": "Brazil",
    "GB": "United Kingdom",
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "PT": "Portugal",
}


def detect_country_from_phone(phone: str) -> str:
    """Detect ISO 3166-1 alpha-2 country code from a phone number. Defaults to 'US'."""
    import phonenumbers
    try:
        parsed = phonenumbers.parse(phone, None)
        region = phonenumbers.region_code_for_number(parsed)
        if region and region in SUPPORTED_COUNTRIES:
            return region
    except phonenumbers.NumberParseException:
        pass
    return "US"


async def get_contractor_by_twilio_number(twilio_number: str) -> Optional[dict]:
    """Look up contractor profile by their Kevin Twilio number."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    docs = await loop.run_in_executor(
        None,
        lambda: list(db.collection(COLLECTION).where("twilio_number", "==", twilio_number).where("active", "==", True).limit(1).stream())
    )
    if docs:
        data = docs[0].to_dict()
        data["contractor_id"] = docs[0].id
        return data
    return None


async def get_contractor_by_apple_user_id(apple_user_id: str) -> Optional[dict]:
    """Look up contractor by Apple User ID (iOS account restore)."""
    if not apple_user_id:
        return None
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    docs = await loop.run_in_executor(
        None,
        lambda: list(db.collection(COLLECTION).where("apple_user_id", "==", apple_user_id).where("active", "==", True).limit(1).stream())
    )
    if docs:
        data = docs[0].to_dict()
        data["contractor_id"] = docs[0].id
        return data
    return None


async def get_contractor_by_api_token(token_hash: str) -> Optional[dict]:
    """Look up contractor by their hashed API token."""
    if not token_hash:
        return None
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    docs = await loop.run_in_executor(
        None,
        lambda: list(db.collection(COLLECTION).where("api_token_hash", "==", token_hash).where("active", "==", True).limit(1).stream())
    )
    if docs:
        data = docs[0].to_dict()
        data["contractor_id"] = docs[0].id
        return data
    return None


async def get_contractor_by_owner_phone(owner_phone: str) -> Optional[dict]:
    """Look up existing contractor by their personal phone number (unique ID)."""
    if not owner_phone:
        return None
    from app.utils.phone import normalize_phone
    normalized = normalize_phone(owner_phone)
    if not normalized:
        return None
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    docs = await loop.run_in_executor(
        None,
        lambda: list(db.collection(COLLECTION).where("owner_phone", "==", normalized).where("active", "==", True).limit(1).stream())
    )
    if docs:
        data = docs[0].to_dict()
        data["contractor_id"] = docs[0].id
        return data
    return None


async def get_contractor(contractor_id: str) -> Optional[dict]:
    """Get contractor profile by ID."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc = await loop.run_in_executor(
        None,
        lambda: db.collection(COLLECTION).document(contractor_id).get()
    )
    if doc.exists:
        data = doc.to_dict()
        data["contractor_id"] = doc.id
        return data
    return None


async def get_contractor_by_pin(pin: str) -> Optional[dict]:
    """Look up an active contractor by their dial-in PIN."""
    if not pin:
        return None
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    docs = await loop.run_in_executor(
        None,
        lambda: list(db.collection(COLLECTION).where("dial_in_pin", "==", pin).where("active", "==", True).limit(1).stream())
    )
    if docs:
        data = docs[0].to_dict()
        data["contractor_id"] = docs[0].id
        return data
    return None


async def create_contractor(data: dict) -> str:
    """Create a new contractor profile. Returns the contractor_id."""
    db = get_firestore_client()
    import time
    data["created_at"] = time.time()
    data["active"] = True
    data.setdefault("mode", "kevin")
    data.setdefault("voice_engine", "elevenlabs")
    data.setdefault("country_code", "US")
    data.setdefault("business_address", "")
    data.setdefault("business_city", "")
    data.setdefault("business_country_name", "")
    data.setdefault("callback_sla_minutes", 15)
    # Generate a random 6-digit dial-in PIN
    data.setdefault("dial_in_pin", f"{secrets.randbelow(1000000):06d}")
    loop = asyncio.get_event_loop()
    doc_ref = await loop.run_in_executor(
        None,
        lambda: db.collection(COLLECTION).add(data)
    )
    # doc_ref is a tuple (timestamp, DocumentReference)
    contractor_id = doc_ref[1].id
    logger.info(f"Contractor created: {contractor_id} ({data.get('business_name', '')})")
    return contractor_id


async def update_contractor(contractor_id: str, updates: dict) -> bool:
    """Update contractor profile fields."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: db.collection(COLLECTION).document(contractor_id).update(updates)
    )
    logger.info(f"Contractor updated: {contractor_id}")
    return True


async def provision_twilio_number(contractor_id: str, area_code: str = "") -> str:
    """Buy a Twilio phone number and assign it to a contractor.
    Returns the provisioned phone number (E.164 format).
    """
    from twilio.rest import Client
    from app.config import settings

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    loop = asyncio.get_event_loop()

    # Search for available numbers
    search_params = {"voice_enabled": True, "sms_enabled": True}
    if area_code:
        search_params["area_code"] = area_code

    numbers = await loop.run_in_executor(
        None,
        lambda: client.available_phone_numbers("US").local.list(**search_params, limit=1)
    )

    if not numbers:
        # Fallback: try without area code
        numbers = await loop.run_in_executor(
            None,
            lambda: client.available_phone_numbers("US").local.list(voice_enabled=True, sms_enabled=True, limit=1)
        )

    if not numbers:
        raise Exception("No phone numbers available")

    # Buy the number
    webhook_url = f"{settings.cloud_run_url}/webhooks/twilio/incoming"
    status_url = f"{settings.cloud_run_url}/webhooks/twilio/status"

    purchased = await loop.run_in_executor(
        None,
        lambda: client.incoming_phone_numbers.create(
            phone_number=numbers[0].phone_number,
            voice_url=webhook_url,
            voice_method="POST",
            status_callback=status_url,
            status_callback_method="POST",
            sms_url=f"{settings.cloud_run_url}/webhooks/twilio/mms-incoming",
            sms_method="POST",
        )
    )

    # Update contractor profile with the number
    await update_contractor(contractor_id, {"twilio_number": purchased.phone_number})

    logger.info(f"Provisioned {redact_phone(purchased.phone_number)} for contractor {contractor_id}")
    return purchased.phone_number


async def release_twilio_number(contractor_id: str) -> bool:
    """Release a contractor's Twilio phone number and clear it from their profile."""
    from twilio.rest import Client
    from app.config import settings

    contractor = await get_contractor(contractor_id)
    if not contractor or not contractor.get("twilio_number"):
        logger.warning(f"No Twilio number to release for contractor {contractor_id}")
        return False

    twilio_number = contractor["twilio_number"]
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    loop = asyncio.get_event_loop()

    # Find the number SID
    numbers = await loop.run_in_executor(
        None,
        lambda: client.incoming_phone_numbers.list(phone_number=twilio_number, limit=1)
    )

    if numbers:
        await loop.run_in_executor(
            None,
            lambda: numbers[0].delete()
        )
        logger.info(f"Released Twilio number {redact_phone(twilio_number)} for contractor {contractor_id}")
    else:
        logger.warning(f"Twilio number {redact_phone(twilio_number)} not found in account")

    # Clear the number from the contractor profile
    await update_contractor(contractor_id, {"twilio_number": ""})
    return True


async def deactivate_contractor(contractor_id: str) -> bool:
    """Deactivate a contractor account and release their Twilio number."""
    await release_twilio_number(contractor_id)
    await update_contractor(contractor_id, {"active": False})
    logger.info(f"Contractor deactivated: {contractor_id}")
    return True


async def list_contractors() -> list:
    """List all active contractors."""
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    docs = await loop.run_in_executor(
        None,
        lambda: list(db.collection(COLLECTION).where("active", "==", True).stream())
    )
    return [{"contractor_id": d.id, **d.to_dict()} for d in docs]
