"""Contractor profile management in Firestore."""

import asyncio
import math
import secrets
import time
import uuid as _uuid
from enum import Enum
from typing import Any, Optional
from google.cloud.firestore_v1.base_query import FieldFilter
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
    "subscription_uuid",
    "subscription_original_transaction_id",
    "subscription_auto_renews",
    "subscription_renewal_status_signed_at_ms",
    "subscription_forwarded_from",
    "twilio_number",
    # Public-demo identity is a server-owned routing boundary. A client must
    # never be able to turn an ordinary tenant into a public ingress target.
    "public_demo",
    "demo_enabled_until",
    "demo_profile_version",
    # App lifecycle — written only by backend
    "deleted_app_detected_at",
    "deactivated_at",
    # Consent signal for the data purge; a client that could forge it could
    # trigger erasure of an account that never asked for it.
    "deletion_requested_at",
    "number_released_at",
    # Reconciliation signal for a number Twilio no longer lists but we
    # believed we owned; a client that could clear it could hide a billing
    # anomaly.
    "number_release_anomaly",
    # App Store notifications that arrived for a deactivated account — an
    # ex-customer may still be being charged. Server-only reconciliation
    # record; a client that could clear it could hide the charges.
    "post_deletion_billing",
    # Forwarding truth — derived from carrier signalling, never client-asserted.
    # A client that could write this would be able to fake an activated forward.
    "forwarding_last_seen_at",
    # Integrations — feature flags are enabled by backend/admin flows only.
    "jobber_lead_capture_enabled",
    # Customer-memory workflow writes — enabled only after backend/provider
    # qualification; callers and the iOS profile API cannot self-enable it.
    "service_request_mutations_enabled",
    # Durable caller profiling and spoken-name use are independently rolled
    # out. Absent values are false and clients cannot self-enable either one.
    "customer_memory_capture_enabled",
    "customer_memory_personalization_enabled",
    # Identity bindings — written only at account creation / authenticated migration.
    # Allowing PATCH to overwrite these would let an attacker hijack another account
    # by claiming its phone number or Apple user ID. (Security audit F-04.)
    "owner_phone",
    "apple_user_id",
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
        lambda: list(db.collection(COLLECTION).where(filter=FieldFilter("twilio_number", "==", twilio_number)).where(filter=FieldFilter("active", "==", True)).limit(1).stream())
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
        lambda: list(db.collection(COLLECTION).where(filter=FieldFilter("apple_user_id", "==", apple_user_id)).where(filter=FieldFilter("active", "==", True)).limit(1).stream())
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
        lambda: list(db.collection(COLLECTION).where(filter=FieldFilter("api_token_hash", "==", token_hash)).where(filter=FieldFilter("active", "==", True)).limit(1).stream())
    )
    if docs:
        data = docs[0].to_dict()
        data["contractor_id"] = docs[0].id
        return data
    return None


async def get_contractor_by_subscription_uuid(subscription_uuid: str, include_inactive: bool = False) -> Optional[dict]:
    """Look up contractor by subscription_uuid (StoreKit appAccountToken).

    By default only active contractors match. include_inactive=True is the
    reconciliation path: App Store notifications for deactivated (deleted)
    accounts must still be attributable, or an ex-customer's renewals are
    silently dropped while Apple keeps charging them.
    """
    if not subscription_uuid:
        return None
    db = get_firestore_client()
    loop = asyncio.get_event_loop()

    def _query():
        q = db.collection(COLLECTION).where(
            filter=FieldFilter("subscription_uuid", "==", subscription_uuid)
        )
        if not include_inactive:
            q = q.where(filter=FieldFilter("active", "==", True))
        return list(q.limit(1).stream())

    docs = await loop.run_in_executor(None, _query)
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

    def _query(value: str):
        return list(
            db.collection(COLLECTION)
            .where(filter=FieldFilter("owner_phone", "==", value))
            .where(filter=FieldFilter("active", "==", True))
            .limit(1)
            .stream()
        )

    # Firestore compares strings exactly. `owner_phone` was historically stored
    # unnormalized — production holds "(415) 555-1234" and bare digits alongside
    # E.164 — so a normalized-only query silently missed those records and the
    # caller created a duplicate account. New writes are normalized; this second
    # lookup covers the records written before that.
    candidates = [normalized]
    raw = owner_phone.strip()
    if raw and raw != normalized:
        candidates.append(raw)

    for candidate in candidates:
        docs = await loop.run_in_executor(None, lambda c=candidate: _query(c))
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


def _is_valid_uuid(value: str) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


async def ensure_subscription_uuid(contractor_id: str, contractor: Optional[dict] = None) -> Optional[str]:
    """Ensure legacy contractor documents have a StoreKit appAccountToken UUID."""
    existing = (contractor or {}).get("subscription_uuid", "")
    if _is_valid_uuid(existing):
        return existing

    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc_ref = db.collection(COLLECTION).document(contractor_id)

    def _ensure() -> Optional[str]:
        doc = doc_ref.get()
        if not doc.exists:
            return None

        data = doc.to_dict() or {}
        current = data.get("subscription_uuid", "")
        if _is_valid_uuid(current):
            return current

        generated = str(_uuid.uuid4())
        doc_ref.update({"subscription_uuid": generated})
        return generated

    ensured = await loop.run_in_executor(None, _ensure)
    if ensured:
        logger.info(f"Backfilled subscription_uuid for contractor {contractor_id}")
    return ensured


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
    # Store owner_phone canonically. Firestore matches strings exactly, so
    # writing raw formats here is what broke dedupe on the next signup.
    if data.get("owner_phone"):
        from app.utils.phone import normalize_phone
        canonical = normalize_phone(str(data["owner_phone"]))
        if canonical:
            data["owner_phone"] = canonical
    data["created_at"] = time.time()
    data["active"] = True
    data.setdefault("mode", "kevin")
    data.setdefault("voice_engine", "elevenlabs")
    data.setdefault("country_code", "US")
    data.setdefault("business_address", "")
    data.setdefault("business_city", "")
    data.setdefault("business_country_name", "")
    data.setdefault("callback_sla_minutes", 15)
    data.setdefault("customer_memory_capture_enabled", False)
    data.setdefault("customer_memory_personalization_enabled", False)
    data.setdefault("service_request_mutations_enabled", False)
    # Generate a random 6-digit dial-in PIN
    data.setdefault("dial_in_pin", f"{secrets.randbelow(1000000):06d}")
    trial_start = data.setdefault("trial_start", time.time())
    data.setdefault("subscription_status", "trial")
    data.setdefault("subscription_tier", "none")
    # 14-day free trial. This previously wrote a 3-day window, which left 93
    # accounts expiring 11 days early; the gate now derives trial end from
    # trial_start (see subscription.trial_expires_at) so those records heal too.
    from app.services.subscription import TRIAL_PERIOD_DAYS
    data.setdefault("subscription_expires", trial_start + TRIAL_PERIOD_DAYS * 86400)
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
    contractor = await get_contractor(contractor_id)
    if not contractor:
        raise Exception("Contractor not found")

    existing_number = contractor.get("twilio_number", "")
    if existing_number:
        logger.info(
            "Contractor %s already has Twilio number %s; skipping provisioning",
            contractor_id,
            redact_phone(existing_number),
        )
        return existing_number

    from twilio.rest import Client
    from app.config import settings

    if country_code not in COUNTRY_NAMES:
        raise Exception(f"Unsupported country: {country_code}")

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    loop = asyncio.get_event_loop()

    # For regulatory countries, create a bundle first
    bundle_sid = None
    if country_code in REGULATORY_COUNTRIES:
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

    from twilio.base.exceptions import TwilioRestException

    updates = {"twilio_number": "", "number_released_at": int(time.time())}
    if numbers:
        try:
            await loop.run_in_executor(
                None,
                lambda: numbers[0].delete()
            )
            logger.info(f"Released Twilio number {redact_phone(twilio_number)} for contractor {contractor_id}")
        except TwilioRestException as e:
            if e.status != 404:
                raise
            # A concurrent release (second device, retry racing a slow first
            # request) already deleted it between our list and delete. The
            # desired end state holds; this is not an anomaly.
            logger.info(
                f"Twilio number {redact_phone(twilio_number)} for contractor {contractor_id} "
                f"already released by a concurrent request"
            )
    else:
        # A number we believe we own but Twilio does not list is an anomaly,
        # not a success: it may be a format mismatch on a number that still
        # bills. Record it on the document so reconciliation can find it.
        logger.error(
            f"Twilio number {redact_phone(twilio_number)} for contractor {contractor_id} "
            f"not found in Twilio account; clearing profile and recording anomaly"
        )
        # The doc clears the number and the log redacts it, so the anomaly
        # record itself must carry the number or reconciliation has no key.
        # Server-only field (PROTECTED_FIELDS), same sensitivity as the
        # twilio_number field it replaces.
        updates["number_release_anomaly"] = {"at": int(time.time()), "number": twilio_number}

    # Clear the number from the contractor profile
    await update_contractor(contractor_id, updates)
    return True


async def deactivate_contractor(contractor_id: str, user_requested: bool = False) -> bool:
    """Deactivate a contractor account and release their Twilio number.

    user_requested=True is set ONLY by the user's own DELETE endpoint and
    stamps deletion_requested_at — the sole consent signal the data purge
    accepts. System deactivations (deleted-app cleanup) must never set it.
    """
    await release_twilio_number(contractor_id)
    updates = {"active": False, "deactivated_at": int(time.time())}
    if user_requested:
        updates["deletion_requested_at"] = int(time.time())
    await update_contractor(contractor_id, updates)
    # Drop this instance's cached token mappings so the deleted account's
    # token stops authenticating here immediately. Other warm instances keep
    # their entry until recycle — a bounded, documented residual.
    from app.middleware.auth import invalidate_contractor_tokens
    invalidate_contractor_tokens(contractor_id)
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


async def activate_subscription_entitlement(
    contractor_id: str,
    tier: str,
    expires_ts: float,
    original_transaction_id: str,
    expected_subscription_uuid: str,
) -> bool:
    """Atomically set direct verified entitlement on an active contractor.

    Inside the transaction:
    - re-read the contractor document;
    - require active is True and exact expected subscription_uuid;
    - atomically set tier/status/expiry, the current subscription_original_transaction_id,
      and clear stale subscription_forwarded_from provenance;
    - if the live current original ID differs from the new original ID, reset
      subscription_auto_renews and subscription_renewal_status_signed_at_ms to None;
    - if it is the same receipt chain, preserve its renewal observation and clock.
    """
    import math
    if (
        not contractor_id
        or not isinstance(contractor_id, str)
        or not expected_subscription_uuid
        or not isinstance(expected_subscription_uuid, str)
        or not original_transaction_id
        or not isinstance(original_transaction_id, str)
        or not tier
        or not isinstance(tier, str)
        or not isinstance(expires_ts, (int, float))
        or isinstance(expires_ts, bool)
        or not math.isfinite(expires_ts)
        or expires_ts <= 0
    ):
        return False

    from google.cloud import firestore as fs

    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(contractor_id)
    loop = asyncio.get_event_loop()

    @fs.transactional
    def _txn(transaction) -> bool:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        if data.get("active") is not True:
            return False
        if data.get("subscription_uuid") != expected_subscription_uuid:
            return False

        current_original_id = data.get("subscription_original_transaction_id")
        updates = {
            "subscription_tier": tier,
            "subscription_status": "active",
            "subscription_expires": float(expires_ts),
            "subscription_original_transaction_id": original_transaction_id,
            "subscription_forwarded_from": None,
        }
        if current_original_id != original_transaction_id:
            updates["subscription_auto_renews"] = None
            updates["subscription_renewal_status_signed_at_ms"] = None

        transaction.update(doc_ref, updates)
        return True

    transaction = db.transaction()
    return await loop.run_in_executor(None, lambda: _txn(transaction))


class BindingBackfillOutcome(str, Enum):
    REPAIRED = "repaired"
    IDEMPOTENT_SAME = "idempotent_same"
    SUPERSEDED = "superseded"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    UUID_MISMATCH = "uuid_mismatch"
    NOT_FOUND_OR_INACTIVE = "not_found_or_inactive"


def is_missing_or_malformed_binding(binding: Any) -> bool:
    """Return True if binding is missing, None, whitespace-only, padded, or not a string."""
    if not isinstance(binding, str):
        return True
    if not binding.strip():
        return True
    if binding != binding.strip():
        return True
    return False


async def conditionally_backfill_subscription_binding(
    contractor_id: str,
    expected_subscription_uuid: str,
    original_transaction_id: str,
    expected_tier: str,
    expected_expires_ts: float,
) -> BindingBackfillOutcome:
    """Atomically backfill subscription_original_transaction_id for an active contractor.

    Inside the transaction:
    - re-read the contractor document;
    - require document exists, active is True, subscription_status == 'active';
    - require exact subscription_uuid matches expected_subscription_uuid;
    - require exact subscription_tier matches expected_tier;
    - require subscription_expires matches expected_expires_ts (within 1.0s tolerance
      for float/int ms normalization);
    - if live subscription_original_transaction_id is missing, non-string, empty, or whitespace-only:
        atomically writes ONLY canonical subscription_original_transaction_id,
        and sets subscription_auto_renews=None, subscription_renewal_status_signed_at_ms=None;
        returns BindingBackfillOutcome.REPAIRED;
    - if live subscription_original_transaction_id is a padded string whose stripped value equals verified ID:
        atomically rewrites to canonical form with renewal fields reset to None;
        returns BindingBackfillOutcome.REPAIRED;
    - if live subscription_original_transaction_id is canonical and equals original_transaction_id:
        idempotent success with zero writes;
        returns BindingBackfillOutcome.IDEMPOTENT_SAME;
    - if live subscription_original_transaction_id (padded or canonical) has a different stripped identity:
        superseded safe no-op with zero writes (never overwritten);
        returns BindingBackfillOutcome.SUPERSEDED;
    - never modifies status, tier, expires, subscription_forwarded_from, uuid, active state,
      or unrelated fields.
    """
    if (
        not contractor_id
        or not isinstance(contractor_id, str)
        or not expected_subscription_uuid
        or not isinstance(expected_subscription_uuid, str)
        or not original_transaction_id
        or not isinstance(original_transaction_id, str)
        or not original_transaction_id.strip()
        or not expected_tier
        or not isinstance(expected_tier, str)
        or not isinstance(expected_expires_ts, (int, float))
        or isinstance(expected_expires_ts, bool)
        or not math.isfinite(expected_expires_ts)
        or expected_expires_ts <= 0
    ):
        return BindingBackfillOutcome.FINGERPRINT_MISMATCH

    from google.cloud import firestore as fs

    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(contractor_id)
    loop = asyncio.get_event_loop()

    @fs.transactional
    def _txn(transaction) -> BindingBackfillOutcome:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return BindingBackfillOutcome.NOT_FOUND_OR_INACTIVE
        data = snapshot.to_dict() or {}
        if data.get("active") is not True:
            return BindingBackfillOutcome.NOT_FOUND_OR_INACTIVE
        if data.get("subscription_status") != "active":
            return BindingBackfillOutcome.FINGERPRINT_MISMATCH
        if data.get("subscription_uuid") != expected_subscription_uuid:
            return BindingBackfillOutcome.UUID_MISMATCH
        if data.get("subscription_tier") != expected_tier:
            return BindingBackfillOutcome.FINGERPRINT_MISMATCH

        stored_expires = data.get("subscription_expires")
        if (
            stored_expires is None
            or isinstance(stored_expires, bool)
            or not isinstance(stored_expires, (int, float))
            or not math.isfinite(stored_expires)
            or abs(float(stored_expires) - float(expected_expires_ts)) >= 1.0
        ):
            return BindingBackfillOutcome.FINGERPRINT_MISMATCH

        current_original = data.get("subscription_original_transaction_id")
        clean_orig = original_transaction_id.strip()

        if not isinstance(current_original, str) or not current_original.strip():
            transaction.update(
                doc_ref,
                {
                    "subscription_original_transaction_id": clean_orig,
                    "subscription_auto_renews": None,
                    "subscription_renewal_status_signed_at_ms": None,
                },
            )
            return BindingBackfillOutcome.REPAIRED

        if current_original.strip() == clean_orig:
            if current_original == clean_orig:
                return BindingBackfillOutcome.IDEMPOTENT_SAME
            transaction.update(
                doc_ref,
                {
                    "subscription_original_transaction_id": clean_orig,
                    "subscription_auto_renews": None,
                    "subscription_renewal_status_signed_at_ms": None,
                },
            )
            return BindingBackfillOutcome.REPAIRED

        return BindingBackfillOutcome.SUPERSEDED

    transaction = db.transaction()
    return await loop.run_in_executor(None, lambda: _txn(transaction))


async def record_active_renewal_status(
    contractor_id: str,
    expected_subscription_uuid: str,
    original_transaction_id: str,
    auto_renews: bool,
    signed_at_ms: int,
) -> bool:
    """Atomically record renewal observation for an active contractor.

    Inside the transaction:
    - require doc exists, active is True, expected subscription_uuid;
    - require protected current subscription_original_transaction_id equals original_transaction_id;
    - equal or older signed_at_ms for that same receipt chain is an atomic no-op;
    - malformed stored clock is treated as safe no-op;
    - updates subscription_auto_renews and subscription_renewal_status_signed_at_ms without touching entitlement.
    """
    if (
        not contractor_id
        or not isinstance(contractor_id, str)
        or not expected_subscription_uuid
        or not isinstance(expected_subscription_uuid, str)
        or not original_transaction_id
        or not isinstance(original_transaction_id, str)
        or type(auto_renews) is not bool
        or type(signed_at_ms) is not int
        or isinstance(signed_at_ms, bool)
        or signed_at_ms <= 0
    ):
        return False

    from google.cloud import firestore as fs

    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(contractor_id)
    loop = asyncio.get_event_loop()

    @fs.transactional
    def _txn(transaction) -> bool:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        if data.get("active") is not True:
            return False
        if data.get("subscription_uuid") != expected_subscription_uuid:
            return False

        current_original = data.get("subscription_original_transaction_id")
        if not current_original or not isinstance(current_original, str) or current_original != original_transaction_id:
            return False

        stored_clock = data.get("subscription_renewal_status_signed_at_ms")
        if stored_clock is not None:
            if type(stored_clock) is not int or isinstance(stored_clock, bool) or stored_clock <= 0:
                return False
            if signed_at_ms <= stored_clock:
                return False

        transaction.update(doc_ref, {
            "subscription_auto_renews": auto_renews,
            "subscription_renewal_status_signed_at_ms": signed_at_ms,
        })
        return True

    transaction = db.transaction()
    return await loop.run_in_executor(None, lambda: _txn(transaction))


async def record_inactive_notification(
    contractor_id: str,
    expected_subscription_uuid: str,
    notification_type: str,
    subtype: Optional[str] = None,
    transaction_id: str = "",
    purchase_date_ms: Optional[int] = None,
    rebound_contractor_id: Optional[str] = None,
    renewal_observation: Optional[dict] = None,
) -> dict:
    """Atomically record notification evidence on an inactive contractor.

    Guards:
    - requires doc exists, active is False, expected subscription_uuid matches;
    - performs monotonic renewal clock updates only within the matching receipt chain;
    - handles same-chain duplicate delivery vs distinct renewal events;
    - preserves existing post-deletion evidence across sequential notifications.
    """
    if (
        not contractor_id
        or not isinstance(contractor_id, str)
        or not expected_subscription_uuid
        or not isinstance(expected_subscription_uuid, str)
        or not notification_type
        or not isinstance(notification_type, str)
    ):
        return {"outcome": "invalid_input"}

    import math
    from google.cloud import firestore as fs

    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(contractor_id)
    loop = asyncio.get_event_loop()

    @fs.transactional
    def _txn(transaction) -> dict:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return {"outcome": "not_found"}
        data = snapshot.to_dict() or {}
        if data.get("active") is not False:
            return {"outcome": "not_inactive"}
        if data.get("subscription_uuid") != expected_subscription_uuid:
            return {"outcome": "uuid_mismatch"}

        prior = data.get("post_deletion_billing") or {}
        if not isinstance(prior, dict):
            prior = {}

        norm_tx_id = str(transaction_id or "")
        norm_type = str(notification_type or "")
        norm_subtype = str(subtype or "")

        prior_tx_id = str(prior.get("last_transaction_id") or "")
        prior_type = str(prior.get("last_type") or "")
        prior_subtype = str(prior.get("last_subtype") or "")

        is_dup = bool(
            norm_tx_id
            and prior_tx_id == norm_tx_id
            and prior_type == norm_type
            and prior_subtype == norm_subtype
        )

        valid_renewal = False
        renewal_to_apply = None
        if isinstance(renewal_observation, dict):
            event_orig = renewal_observation.get("original_transaction_id")
            event_auto = renewal_observation.get("auto_renews")
            event_signed = renewal_observation.get("signed_at_ms")
            if (
                isinstance(event_orig, str)
                and event_orig.strip()
                and type(event_auto) is bool
                and type(event_signed) is int
                and not isinstance(event_signed, bool)
                and event_signed > 0
            ):
                event_orig_str = event_orig.strip()
                top_orig = data.get("subscription_original_transaction_id")
                top_orig_str = str(top_orig).strip() if (isinstance(top_orig, str) and top_orig.strip()) else ""
                nested_orig = prior.get("renewal_original_transaction_id")
                nested_orig_str = str(nested_orig).strip() if (isinstance(nested_orig, str) and nested_orig.strip()) else ""

                matches_nested = bool(nested_orig_str and event_orig_str == nested_orig_str)
                matches_top = bool(top_orig_str and event_orig_str == top_orig_str)

                if matches_nested:
                    # Same receipt chain as existing nested observation: strictly compare against nested clock
                    prior_signed_at = prior.get("renewal_status_signed_at_ms")
                    if prior_signed_at is None:
                        valid_renewal = True
                        renewal_to_apply = {
                            "renewal_original_transaction_id": event_orig_str,
                            "renewal_auto_renews": event_auto,
                            "renewal_status_signed_at_ms": event_signed,
                        }
                    elif (
                        type(prior_signed_at) is int
                        and not isinstance(prior_signed_at, bool)
                        and prior_signed_at > 0
                        and event_signed > prior_signed_at
                    ):
                        valid_renewal = True
                        renewal_to_apply = {
                            "renewal_original_transaction_id": event_orig_str,
                            "renewal_auto_renews": event_auto,
                            "renewal_status_signed_at_ms": event_signed,
                        }
                elif matches_top:
                    # Matches top-level current receipt but nested belongs to a different receipt (or unset).
                    # Treat this as first observation for top-level receipt and atomically replace.
                    valid_renewal = True
                    renewal_to_apply = {
                        "renewal_original_transaction_id": event_orig_str,
                        "renewal_auto_renews": event_auto,
                        "renewal_status_signed_at_ms": event_signed,
                    }

        if is_dup:
            if valid_renewal and renewal_to_apply:
                merged = dict(prior)
                merged.update(renewal_to_apply)
                transaction.update(doc_ref, {"post_deletion_billing": merged})
                return {
                    "outcome": "renewal_update",
                    "post_deletion_billing": merged,
                    "count": prior.get("count", 0),
                    "charges": prior.get("charges", 0),
                    "charged_after_deletion": False,
                }
            return {
                "outcome": "duplicate",
                "post_deletion_billing": prior,
                "count": prior.get("count", 0),
                "charges": prior.get("charges", 0),
                "charged_after_deletion": False,
            }

        def _safe_int(v):
            try:
                if isinstance(v, (int, float)) and not math.isfinite(v):
                    return 0
                return int(v or 0)
            except (TypeError, ValueError, OverflowError):
                return 0

        is_charge = notification_type in ("DID_RENEW", "SUBSCRIBED")
        charged_after_deletion = is_charge

        raw_deact = data.get("deactivated_at")
        deactivated_ts = None
        if isinstance(raw_deact, (int, float)) and not isinstance(raw_deact, bool):
            if math.isfinite(raw_deact) and raw_deact > 0:
                deactivated_ts = float(raw_deact)

        purchase_ms_val = None
        if isinstance(purchase_date_ms, (int, float)) and not isinstance(purchase_date_ms, bool):
            if math.isfinite(purchase_date_ms) and purchase_date_ms > 0:
                purchase_ms_val = float(purchase_date_ms)

        if is_charge and deactivated_ts is not None and purchase_ms_val is not None:
            charged_after_deletion = (purchase_ms_val / 1000.0) > deactivated_ts

        new_count = _safe_int(prior.get("count")) + 1
        new_charges = _safe_int(prior.get("charges")) + (1 if charged_after_deletion else 0)

        merged = dict(prior)
        merged["count"] = new_count
        merged["charges"] = new_charges
        merged["last_type"] = notification_type
        merged["last_subtype"] = norm_subtype
        merged["last_at"] = int(time.time())
        merged["last_transaction_id"] = norm_tx_id
        if rebound_contractor_id:
            merged["rebound_contractor_id"] = rebound_contractor_id

        if valid_renewal and renewal_to_apply:
            merged.update(renewal_to_apply)

        transaction.update(doc_ref, {"post_deletion_billing": merged})
        return {
            "outcome": "recorded",
            "post_deletion_billing": merged,
            "count": new_count,
            "charges": new_charges,
            "charged_after_deletion": charged_after_deletion,
        }

    transaction = db.transaction()
    return await loop.run_in_executor(None, lambda: _txn(transaction))
