"""Contractor profile management in Firestore."""

import asyncio
import secrets
import time
import uuid as _uuid
from typing import Optional
from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger, redact_phone

logger = get_logger(__name__)

COLLECTION = "contractors"

PROTECTED_FIELDS = frozenset({
    # Subscription billing — written only by App Store webhook / subscription service
    "subscription_tier",
    "subscription_status",
    "subscription_expires",
    "trial_start",
    # App lifecycle — written only by backend
    "deleted_app_detected_at",
})

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
    # Try parsing as E.164 first (no region needed), fall back to US
    normalized = normalize_phone(owner_phone, default_region=None)
    if not normalized:
        normalized = normalize_phone(owner_phone, default_region="US")
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
    trial_start = data.setdefault("trial_start", time.time())
    data.setdefault("subscription_status", "trial")
    data.setdefault("subscription_tier", "none")
    data.setdefault("subscription_expires", trial_start + 3 * 86400)  # 3-day grace; real trial is Apple's 2-week intro offer
    data.setdefault("deleted_app_detected_at", None)
    data.setdefault("subscription_uuid", str(_uuid.uuid4()))
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


async def _create_regulatory_bundle(client, loop, country_code: str, business_name: str, address: str, city: str) -> str:
    """Create a Twilio regulatory bundle for EU/BR number provisioning.

    Returns the bundle SID. Raises if the bundle cannot be created or approved.
    """
    country_name = COUNTRY_NAMES.get(country_code, "")

    # Look up the regulation SID for this country + number type
    regulations = await loop.run_in_executor(
        None,
        lambda: client.numbers.v2.regulatory_compliance.regulations.list(
            iso_country=country_code, number_type="local", limit=1
        )
    )
    if not regulations:
        raise Exception(f"No Twilio regulations found for {country_name} local numbers")
    regulation_sid = regulations[0].sid

    # Create an address in Twilio
    twilio_address = await loop.run_in_executor(
        None,
        lambda: client.addresses.create(
            friendly_name=f"{business_name} - {city}",
            street=address,
            city=city,
            region="",
            postal_code="",
            iso_country=country_code,
            customer_name=business_name,
        )
    )

    # Create a regulatory bundle
    bundle = await loop.run_in_executor(
        None,
        lambda: client.numbers.v2.regulatory_compliance.bundles.create(
            friendly_name=f"{business_name} - {country_name} number",
            regulation_sid=regulation_sid,
            iso_country=country_code,
            number_type="local",
        )
    )

    # Attach the address as a supporting document
    await loop.run_in_executor(
        None,
        lambda: client.numbers.v2.regulatory_compliance.bundles(bundle.sid)
        .item_assignments.create(object_sid=twilio_address.sid)
    )

    # Submit the bundle for review
    await loop.run_in_executor(
        None,
        lambda: client.numbers.v2.regulatory_compliance.bundles(bundle.sid)
        .update(status="pending-review")
    )

    # Poll for approval (usually instant, max 30 seconds)
    for _ in range(15):
        await asyncio.sleep(2)
        updated = await loop.run_in_executor(
            None,
            lambda: client.numbers.v2.regulatory_compliance.bundles(bundle.sid).fetch()
        )
        if updated.status == "twilio-approved":
            logger.info(f"Regulatory bundle approved: {bundle.sid} ({country_code})")
            return bundle.sid
        if updated.status == "provisionally-approved":
            logger.info(f"Regulatory bundle provisionally approved: {bundle.sid}")
            return bundle.sid
        if updated.status == "twilio-rejected":
            raise Exception(f"Regulatory bundle rejected for {country_name}. Please verify your business address.")

    # Bundle still pending — try to provision anyway (Twilio may accept provisionally)
    logger.info(f"Regulatory bundle pending after 30s: {bundle.sid} ({country_code})")
    return bundle.sid


async def provision_twilio_number(contractor_id: str, country_code: str = "US", area_code: str = "") -> str:
    """Buy a Twilio phone number in the contractor's country and assign it.

    For EU/BR countries, creates a regulatory bundle first using the contractor's
    business address. Returns the provisioned phone number (E.164 format).
    """
    from twilio.rest import Client
    from app.config import settings

    if country_code not in COUNTRY_NAMES:
        raise Exception(f"Unsupported country: {country_code}")

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    loop = asyncio.get_event_loop()

    # For regulatory countries, create a bundle first
    bundle_sid = None
    if country_code in REGULATORY_COUNTRIES:
        contractor = await get_contractor(contractor_id)
        if not contractor:
            raise Exception("Contractor not found")
        business_address = contractor.get("business_address", "")
        business_city = contractor.get("business_city", "")
        business_name = contractor.get("business_name", "")
        if not business_address or not business_city:
            raise Exception("Business address and city required for number provisioning in this country")

        bundle_sid = await _create_regulatory_bundle(
            client, loop, country_code, business_name, business_address, business_city
        )

    # Search for available numbers
    # Note: sms_enabled only for US/CA — EU/BR local numbers often don't support SMS
    search_params = {"voice_enabled": True}
    if country_code in ("US", "CA"):
        search_params["sms_enabled"] = True
    if area_code:
        search_params["area_code"] = area_code

    numbers = await loop.run_in_executor(
        None,
        lambda: client.available_phone_numbers(country_code).local.list(**search_params, limit=1)
    )

    if not numbers and area_code:
        # Retry without area code
        search_params.pop("area_code", None)
        numbers = await loop.run_in_executor(
            None,
            lambda: client.available_phone_numbers(country_code).local.list(**search_params, limit=1)
        )

    if not numbers:
        raise Exception(f"No phone numbers available in {COUNTRY_NAMES.get(country_code, country_code)}")

    # Buy the number (bundle_sid goes here, NOT in search)
    webhook_url = f"{settings.cloud_run_url}/webhooks/twilio/incoming"
    status_url = f"{settings.cloud_run_url}/webhooks/twilio/status"

    purchase_params = {
        "phone_number": numbers[0].phone_number,
        "voice_url": webhook_url,
        "voice_method": "POST",
        "status_callback": status_url,
        "status_callback_method": "POST",
        "sms_url": f"{settings.cloud_run_url}/webhooks/twilio/mms-incoming",
        "sms_method": "POST",
    }
    if bundle_sid:
        purchase_params["bundle_sid"] = bundle_sid

    purchased = await loop.run_in_executor(
        None,
        lambda: client.incoming_phone_numbers.create(**purchase_params)
    )

    # Update contractor profile with the number
    await update_contractor(contractor_id, {"twilio_number": purchased.phone_number})

    logger.info(f"Provisioned {redact_phone(purchased.phone_number)} ({country_code}) for contractor {contractor_id}")
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
