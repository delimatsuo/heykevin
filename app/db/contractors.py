"""Contractor profile management in Firestore."""

import asyncio
import math
import secrets
from contextvars import ContextVar
import time
import uuid as _uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger, redact_phone

logger = get_logger(__name__)

COLLECTION = "contractors"
NUMBER_RELEASE_CLAIM_FIELD = "number_release_claim"
NUMBER_RELEASE_CLAIM_RESERVED = "reserved"
NUMBER_RELEASE_CLAIM_DELETING = "deleting"
NUMBER_RELEASE_CLAIM_CANCELLED = "cancelled"
NUMBER_RELEASE_CLAIM_NEEDS_ATTENTION = "needs_attention"
NUMBER_RELEASE_CONFLICT_FIELD = "number_release_conflict"
# ``reserved`` has not crossed the irreversible provider DELETE. A process
# crash in that interval must not strand an otherwise safe number forever, so
# a fresh, still-eligible worker may replace an abandoned reservation after
# this short lease. The prior nonce can no longer cross the delete boundary
# after replacement.
NUMBER_RELEASE_RESERVATION_TTL_SECONDS = 5 * 60

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
    # A strict external release reserves this server-owned fence before its
    # irreversible provider boundary. Client writes must never create, clear,
    # or disguise an in-flight release claim.
    NUMBER_RELEASE_CLAIM_FIELD,
    NUMBER_RELEASE_CONFLICT_FIELD,
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
    "last_inbound_call_at",
    # Integrations — feature flags are enabled by backend/admin flows only.
    "jobber_lead_capture_enabled",
    "jobber_lead_capture_updated_at",
    # Integrations — credentials, lifecycle, claims, operation intents, generations, and scopes
    # are server-owned and mutated strictly through atomic CAS transactions.
    "jobber_connected",
    "jobber_operation_intent_context",
    "jobber_reauthorization_attempt_context",
    "jobber_app_id",
    "jobber_account_id",
    "jobber_accepted_disconnect_barrier",
    "jobber_disconnect_state",
    "jobber_disconnect_processed_barrier",
    "jobber_disconnect_pending",
    "jobber_disconnect_pending_barrier",
    "jobber_disconnect_pending_at",
    "jobber_disconnect_pending_receipt_id",
    "jobber_generation",
    "jobber_lifecycle_epoch",
    "jobber_access_token",
    "jobber_refresh_token",
    "jobber_connected_at",
    "jobber_disconnected_at",
    "jobber_token_refreshed_at",
    "jobber_token_expires_at",
    "jobber_operation_intent_id",
    "jobber_operation_intent_kind",
    "jobber_operation_intent_phase",
    "jobber_operation_intent_expires_at",
    "jobber_operation_intent_acquired_at",
    "jobber_operation_intent_generation",
    "jobber_operation_intent_lifecycle_epoch",
    "jobber_operation_intent_credentials_fingerprint",
    "jobber_operation_intent_bound_operation_id",
    "jobber_reauthorization_attempt_id",
    "jobber_reauthorization_attempt_kind",
    "jobber_reauthorization_attempt_phase",
    "jobber_reauthorization_attempt_expires_at",
    "jobber_reauthorization_attempt_acquired_at",
    "jobber_reauthorization_attempt_generation",
    "jobber_reauthorization_attempt_lifecycle_epoch",
    "jobber_reauthorization_attempt_credentials_fingerprint",
    "jobber_refresh_claim_id",
    "jobber_refresh_phase",
    "jobber_refresh_claim_phase",
    "jobber_refresh_claim_expires_at",
    "jobber_refresh_claim_generation",
    "jobber_refresh_lease_expires_at",
    "jobber_refresh_in_progress",
    "jobber_token_quarantine",
    "jobber_token_quarantine_reason",
    "jobber_unknown_outcome_claim_id",
    "jobber_refresh_outcome_unknown",
    "jobber_reauthorization_required",
    "jobber_token_envelope_required",
    "google_calendar_connected",
    "google_calendar_generation",
    "google_calendar_lifecycle_epoch",
    "google_calendar_access_token",
    "google_calendar_refresh_token",
    "google_calendar_scope",
    "google_calendar_connected_at",
    "google_calendar_disconnected_at",
    "google_calendar_token_refreshed_at",
    "google_calendar_token_expires_at",
    "google_calendar_operation_intent_id",
    "google_calendar_operation_intent_kind",
    "google_calendar_operation_intent_phase",
    "google_calendar_operation_intent_expires_at",
    "google_calendar_operation_intent_acquired_at",
    "google_calendar_operation_intent_generation",
    "google_calendar_operation_intent_lifecycle_epoch",
    "google_calendar_operation_intent_credentials_fingerprint",
    "google_calendar_operation_intent_bound_operation_id",
    "google_calendar_reauthorization_attempt_id",
    "google_calendar_reauthorization_attempt_kind",
    "google_calendar_reauthorization_attempt_phase",
    "google_calendar_reauthorization_attempt_expires_at",
    "google_calendar_reauthorization_attempt_acquired_at",
    "google_calendar_reauthorization_attempt_generation",
    "google_calendar_reauthorization_attempt_lifecycle_epoch",
    "google_calendar_reauthorization_attempt_credentials_fingerprint",
    "google_calendar_refresh_claim_id",
    "google_calendar_refresh_phase",
    "google_calendar_refresh_claim_phase",
    "google_calendar_refresh_claim_expires_at",
    "google_calendar_refresh_claim_generation",
    "google_calendar_refresh_lease_expires_at",
    "google_calendar_refresh_in_progress",
    "google_calendar_token_quarantine",
    "google_calendar_token_quarantine_reason",
    "google_calendar_unknown_outcome_claim_id",
    "google_calendar_refresh_outcome_unknown",
    "google_calendar_reauthorization_required",
    "google_calendar_token_envelope_required",
    # Customer-memory workflow writes — enabled only after backend/provider
    # qualification; callers and the iOS profile API cannot self-enable it.
    "service_request_mutations_enabled",
    # Durable caller profiling and spoken-name use are independently rolled
    # out. Absent values are false and clients cannot self-enable either one.
    "customer_memory_capture_enabled",
    "customer_memory_personalization_enabled",
    # Telephony service binding — written only during provider provisioning
    "provisioned_country_code",
    "number_provider",
    "number_type",
    "number_capabilities",
    "service_binding",
    # Identity bindings — written only at account creation / authenticated migration.
    # Allowing PATCH to overwrite these would let an attacker hijack another account
    # by claiming its phone number or Apple user ID. (Security audit F-04.)
    "owner_phone",
    "owner_phone_e164",
    "apple_user_id",
    # Owner SMS carrier/provider opt-out fields — server-owned, mutated strictly
    # through signed consent webhooks and status callbacks.
    "owner_sms_consent",
    "owner_sms_opted_out",
    "owner_sms_opt_out_revision",
    "owner_sms_opt_out_source",
    "owner_sms_opt_out_updated_at",
    # Acquisition and activation measurement — server-owned embedded map.
    "acquisition_measurement",
})

from app.services.country_policy import (
    RECOGNIZED_COUNTRIES,
    AVAILABLE_COUNTRIES,
    SUPPORTED_COUNTRIES,
    REGULATORY_COUNTRIES,
    COUNTRY_NAMES,
)


def detect_country_from_phone(phone: str) -> str:
    """Detect ISO 3166-1 alpha-2 country code from a phone number. Defaults to 'US'."""
    import phonenumbers
    try:
        parsed = phonenumbers.parse(phone, None)
        region = phonenumbers.region_code_for_number(parsed)
        if region and region in RECOGNIZED_COUNTRIES:
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


class PhoneDedupeAmbiguityError(Exception):
    """Raised when phone deduplication encounters unexamined overflow or multiple conflicting canonical records."""
    pass


DOC_QUERY_CAP = 5


async def get_contractor_by_owner_phone(
    owner_phone: str,
    *,
    country_code: str = "US",
) -> Optional[dict]:
    """Look up existing contractor by their personal phone number (unique ID)."""
    if not isinstance(owner_phone, str) or not owner_phone.strip():
        return None
    import phonenumbers
    from app.utils.phone import normalize_phone

    effective_country = (
        country_code.strip().upper()
        if isinstance(country_code, str) and country_code.strip().upper() in SUPPORTED_COUNTRIES
        else "US"
    )

    # Try parsing as E.164 first (no region needed), fall back to effective country
    canonical = normalize_phone(owner_phone, default_region=None)
    if not canonical:
        canonical = normalize_phone(owner_phone, default_region=effective_country)
    if not canonical:
        return None
    db = get_firestore_client()
    loop = asyncio.get_event_loop()

    def _query_field(field_name: str, value: str, limit: int = DOC_QUERY_CAP + 1):
        return list(
            db.collection(COLLECTION)
            .where(filter=FieldFilter(field_name, "==", value))
            .where(filter=FieldFilter("active", "==", True))
            .limit(limit)
            .stream()
        )

    verified_matches_by_id: dict[str, dict] = {}

    def _record_match(doc_id: str, data: dict):
        if doc_id not in verified_matches_by_id:
            if len(verified_matches_by_id) >= 1:
                raise PhoneDedupeAmbiguityError("Phone deduplication encountered multiple matching records")
            verified_matches_by_id[doc_id] = data

    # 1. Authoritative canonical companion lookup on owner_phone_e164
    docs = await loop.run_in_executor(None, lambda: _query_field("owner_phone_e164", canonical))
    if len(docs) > DOC_QUERY_CAP:
        raise PhoneDedupeAmbiguityError("Phone deduplication encountered ambiguous unexamined records")
    for doc in docs:
        data = doc.to_dict()
        doc_id = doc.id
        data["contractor_id"] = doc_id
        _record_match(doc_id, data)

    # 2. Canonical lookup on owner_phone
    docs = await loop.run_in_executor(None, lambda: _query_field("owner_phone", canonical))
    if len(docs) > DOC_QUERY_CAP:
        raise PhoneDedupeAmbiguityError("Phone deduplication encountered ambiguous unexamined records")
    for doc in docs:
        data = doc.to_dict()
        doc_id = doc.id
        data["contractor_id"] = doc_id
        _record_match(doc_id, data)

    # 3. Bounded deterministic legacy candidate representations for historical unnormalized records.
    # Note: Arbitrary non-standard historical spellings require a separately authorized
    # collision-aware backfill/migration; this provides bounded deterministic fallback
    # without live collection scans.
    raw_input = owner_phone.strip()
    candidates_to_add: list[str] = [raw_input]
    try:
        parsed = phonenumbers.parse(owner_phone, effective_country)
        if phonenumbers.is_valid_number(parsed):
            nat = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
            intl = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            digits_nat = str(parsed.national_number)
            nat_no_spaces = "".join(c for c in nat if c.isdigit())
            digits_cc = f"{parsed.country_code}{parsed.national_number}"
            candidates_to_add.extend([nat, intl, digits_nat, nat_no_spaces, digits_cc])
    except phonenumbers.NumberParseException:
        pass

    seen = {canonical}
    legacy_candidates = []
    for c in candidates_to_add:
        c_str = str(c).strip()
        if c_str and c_str not in seen:
            seen.add(c_str)
            legacy_candidates.append(c_str)

    # Enforce strict query ceiling (at most 6 legacy queries + 2 canonical queries = <= 8 total queries)
    for candidate in legacy_candidates[:6]:
        docs = await loop.run_in_executor(None, lambda c=candidate: _query_field("owner_phone", c))
        if len(docs) > DOC_QUERY_CAP:
            raise PhoneDedupeAmbiguityError("Phone deduplication encountered ambiguous unexamined records")

        for doc in docs:
            stored_data = doc.to_dict()
            doc_id = doc.id
            stored_raw_val = stored_data.get("owner_phone")
            stored_raw = str(stored_raw_val).strip() if isinstance(stored_raw_val, (str, int, float)) else ""

            stored_country_val = stored_data.get("country_code")
            stored_country = (
                stored_country_val.strip().upper()
                if isinstance(stored_country_val, str)
                else ""
            )

            # Re-normalize stored phone: E.164 first, otherwise require supported stored country
            stored_canonical = normalize_phone(stored_raw, default_region=None)
            if not stored_canonical:
                if stored_country in SUPPORTED_COUNTRIES:
                    stored_canonical = normalize_phone(stored_raw, default_region=stored_country)
                else:
                    stored_canonical = None

            # Only accept legacy hit if stored canonical exactly matches requested canonical
            if stored_canonical and stored_canonical == canonical:
                stored_data["contractor_id"] = doc_id
                _record_match(doc_id, stored_data)

    if len(verified_matches_by_id) == 1:
        return next(iter(verified_matches_by_id.values()))
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
    from app.services.country_policy import (
        validate_phone_and_region,
        is_country_available,
        is_recognized_country,
        CountryPhoneMismatchError,
        InvalidPhoneError,
    )

    raw_country_val = data.get("country_code")
    raw_country = (
        raw_country_val.strip().upper()
        if isinstance(raw_country_val, str)
        else ""
    )

    if raw_country_val is not None and str(raw_country_val).strip() != "" and not is_recognized_country(raw_country):
        raise ValueError(f"Unsupported country code: {raw_country_val}")

    owner_phone_raw = str(data.get("owner_phone") or "").strip()
    if owner_phone_raw:
        try:
            canonical, derived_region = validate_phone_and_region(
                owner_phone_raw, default_country=raw_country
            )
        except CountryPhoneMismatchError as e:
            raise ValueError(f"Phone number does not match specified country: {e}")
        except InvalidPhoneError as e:
            raise ValueError(f"Invalid owner phone number: {e}")

        effective_country = derived_region or raw_country or "UNKNOWN"
        data["owner_phone"] = canonical
        data["owner_phone_e164"] = canonical
    else:
        effective_country = raw_country if raw_country else "US"
        if "owner_phone" in data:
            data["owner_phone"] = ""
        data["owner_phone_e164"] = ""

    # Validate admitted country before get_firestore_client and trial defaults
    if not is_country_available(effective_country):
        raise ValueError(f"Country not available for new accounts: {effective_country}")

    data["country_code"] = effective_country

    db = get_firestore_client()
    data["created_at"] = time.time()
    data["active"] = True
    data.setdefault("mode", "kevin")
    data.setdefault("voice_engine", "elevenlabs")
    data.setdefault("business_address", "")
    data.setdefault("business_city", "")
    data.setdefault("business_country_name", "")
    data.setdefault("callback_sla_minutes", 15)
    data.setdefault("customer_memory_capture_enabled", False)
    data.setdefault("customer_memory_personalization_enabled", False)
    data.setdefault("service_request_mutations_enabled", False)
    data.setdefault("smart_interruption", True)
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
    data.setdefault("provisioned_country_code", None)
    data.setdefault("number_provider", None)
    data.setdefault("number_type", None)
    data.setdefault("number_capabilities", None)
    # Express consent can only be collected by the dedicated transaction.
    data.pop("owner_sms_consent", None)
    data.setdefault("owner_sms_enabled", effective_country != "CA")
    data.setdefault("owner_sms_opted_out", False)
    data.setdefault("owner_sms_opt_out_revision", 0)
    data.setdefault("owner_sms_opt_out_source", None)
    data.setdefault("owner_sms_opt_out_updated_at", None)

    # Acquisition and activation measurement cohort tracking (default off).
    # Declared onboarding intent is stripped from ordinary contractor persistence
    # and placed only inside the protected acquisition_measurement map on eligible accounts.
    raw_intent = data.pop("declared_onboarding_intent", None)
    from app.services.acquisition import is_acquisition_measurement_enabled
    if is_acquisition_measurement_enabled():
        intent = raw_intent if raw_intent in ("personal", "business") else "unknown"
        created_ts = data.get("created_at") or time.time()
        data.setdefault("acquisition_measurement", {
            "schema_version": 1,
            "cohort": created_ts,
            "created_at": created_ts,
            "account_country_at_signup": effective_country,
            "declared_onboarding_intent": intent,
            "attempts": 0,
            "last_attempt_at": None,
            "attribution_status": None,
            "attribution": None,
            "lease_attempt": None,
            "lease_expires_at": None,
        })
    loop = asyncio.get_event_loop()
    doc_ref = await loop.run_in_executor(
        None,
        lambda: db.collection(COLLECTION).add(data)
    )
    # doc_ref is a tuple (timestamp, DocumentReference)
    contractor_id = doc_ref[1].id
    logger.info(f"Contractor created: {contractor_id} ({data.get('business_name', '')})")
    return contractor_id


class ReleaseAwareMutationResult(str, Enum):
    """Result of a root-profile write while a number-release lock exists."""

    APPLIED = "applied"
    CANCELLED = "cancelled"
    HELD = "held"


_RELEASE_CONFLICT_DEFERRED_FIELDS = frozenset({
    # These are the safety signals an operator needs to reconcile a release
    # that crossed the provider boundary. Keep this deliberately narrow:
    # profile content, tokens, phone numbers, and knowledge must not be copied
    # into a conflict record.
    "active",
    "subscription_status",
    "subscription_tier",
    "subscription_expires",
    "subscription_original_transaction_id",
    "subscription_auto_renews",
    "subscription_renewal_status_signed_at_ms",
    "subscription_forwarded_from",
    "deleted_app_detected_at",
    "forwarding_last_seen_at",
    "last_inbound_call_at",
})

# A scalar deferred_updates field is useful for the operator's current view,
# but cannot safely establish order when App Store notifications arrive late.
# Keep a small immutable history of entitlement updates too, so a held EXPIRED
# event never erases evidence of a held DID_RENEW event.
_RELEASE_CONFLICT_ENTITLEMENT_FIELDS = frozenset({
    "subscription_status",
    "subscription_tier",
    "subscription_expires",
    "subscription_original_transaction_id",
    "subscription_auto_renews",
    "subscription_renewal_status_signed_at_ms",
    "subscription_forwarded_from",
})
_RELEASE_CONFLICT_EVENT_LIMIT = 8



def _release_conflict_payload(
    state: str,
    updates: dict,
    *,
    prior: dict | None = None,
) -> dict:
    """Build a non-sensitive durable record of a blocked profile mutation.

    The root profile must remain untouched once a delete is in progress, but
    losing an App Store renewal or a call-safety timestamp would make manual
    reconciliation guess at a real event. Preserve only the narrow,
    server-owned set of entitlement and safety values needed to resolve it.
    """
    payload = {
        "at": int(time.time()),
        "phase": state,
        "kind": "profile_mutation",
        "fields": sorted(str(key) for key in updates)[:32],
    }
    deferred_updates = {}
    if isinstance(prior, dict):
        previous_deferred = prior.get("deferred_updates")
        if isinstance(previous_deferred, dict):
            deferred_updates.update(previous_deferred)
    deferred_updates.update({
        field: updates[field]
        for field in _RELEASE_CONFLICT_DEFERRED_FIELDS
        if field in updates
    })
    if deferred_updates:
        payload["deferred_updates"] = deferred_updates

    entitlement_updates = {
        field: updates[field]
        for field in _RELEASE_CONFLICT_ENTITLEMENT_FIELDS
        if field in updates
    }
    events: list[dict] = []
    if isinstance(prior, dict):
        raw_events = prior.get("deferred_subscription_events")
        if isinstance(raw_events, list):
            for raw_event in raw_events[-_RELEASE_CONFLICT_EVENT_LIMIT:]:
                if not isinstance(raw_event, dict):
                    continue
                raw_updates = raw_event.get("updates")
                if not isinstance(raw_updates, dict):
                    continue
                safe_updates = {
                    field: raw_updates[field]
                    for field in _RELEASE_CONFLICT_ENTITLEMENT_FIELDS
                    if field in raw_updates
                }
                if not safe_updates:
                    continue
                raw_at = raw_event.get("at")
                events.append({
                    "at": raw_at if isinstance(raw_at, (int, float)) else None,
                    "updates": safe_updates,
                })
    if entitlement_updates:
        # Redeliveries commonly repeat the exact payload. Dedupe adjacent
        # identical events without rewriting an earlier observed timestamp.
        if not events or events[-1]["updates"] != entitlement_updates:
            events.append({"at": payload["at"], "updates": entitlement_updates})
    if events:
        payload["deferred_subscription_events"] = events[-_RELEASE_CONFLICT_EVENT_LIMIT:]
    return payload


def apply_release_aware_contractor_update(
    transaction,
    doc_ref,
    data: dict,
    updates: dict,
) -> ReleaseAwareMutationResult:
    """Apply a contractor update without letting it race an owned release.

    A safety-relevant write during ``reserved`` is real evidence, so it is
    committed together with cancellation of the pending release. Once the
    state machine reaches ``deleting``, the write is held and a durable
    conflict moves the release to ``needs_attention``.
    """
    if not isinstance(data, dict) or not isinstance(updates, dict):
        return ReleaseAwareMutationResult.HELD
    if NUMBER_RELEASE_CLAIM_FIELD in updates:
        return ReleaseAwareMutationResult.HELD

    raw_claim = data.get(NUMBER_RELEASE_CLAIM_FIELD)
    if raw_claim is None:
        transaction.update(doc_ref, updates)
        return ReleaseAwareMutationResult.APPLIED
    if not isinstance(raw_claim, dict):
        transaction.update(doc_ref, {
            NUMBER_RELEASE_CONFLICT_FIELD: _release_conflict_payload("malformed", updates),
        })
        return ReleaseAwareMutationResult.HELD

    state = raw_claim.get("state", NUMBER_RELEASE_CLAIM_RESERVED)
    if state == NUMBER_RELEASE_CLAIM_RESERVED:
        cancelled = dict(raw_claim)
        cancelled.update({
            "state": NUMBER_RELEASE_CLAIM_CANCELLED,
            "cancelled_at": int(time.time()),
        })
        transaction.update(doc_ref, {
            **updates,
            NUMBER_RELEASE_CLAIM_FIELD: cancelled,
        })
        return ReleaseAwareMutationResult.CANCELLED

    if state == NUMBER_RELEASE_CLAIM_CANCELLED:
        # Cancellation makes the reservation terminal. A later worker may
        # claim again after it has evaluated the new profile state.
        transaction.update(doc_ref, updates)
        return ReleaseAwareMutationResult.APPLIED

    held = dict(raw_claim)
    if state == NUMBER_RELEASE_CLAIM_DELETING:
        held.update({
            "state": NUMBER_RELEASE_CLAIM_NEEDS_ATTENTION,
            "held_at": int(time.time()),
        })
    transaction.update(doc_ref, {
        NUMBER_RELEASE_CLAIM_FIELD: held,
        NUMBER_RELEASE_CONFLICT_FIELD: _release_conflict_payload(
            state,
            updates,
            prior=data.get(NUMBER_RELEASE_CONFLICT_FIELD),
        ),
    })
    return ReleaseAwareMutationResult.HELD


async def record_inbound_call_evidence(
    contractor_id: str,
    seen_at: float,
    *,
    rpc_timeout: float = 0.75,
) -> bool:
    """Transactionally record a live inbound call with real Firestore deadlines.

    This narrow path is intentionally separate from the normal profile updater:
    a webhook has a strict response budget, while a number-release reservation
    needs the inbound event to cancel or hold its nonce in the same transaction.
    We use one Firestore transaction attempt with explicit read and commit
    deadlines. An aborted or deadline-exceeded attempt reports False so the
    webhook can use its controlled direct-ring fallback.
    """
    if (
        not isinstance(contractor_id, str)
        or not contractor_id
        or not isinstance(seen_at, (int, float))
        or isinstance(seen_at, bool)
        or not math.isfinite(seen_at)
        or not isinstance(rpc_timeout, (int, float))
        or isinstance(rpc_timeout, bool)
        or not math.isfinite(rpc_timeout)
        or rpc_timeout <= 0
    ):
        return False

    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc_ref = db.collection(COLLECTION).document(contractor_id)

    def _write() -> bool:
        # Avoid opening a Firestore transaction on the common hourly-throttled
        # path. A recent committed stamp already makes any quiet-number claim
        # fail its guard; if release state is present, proceed to the
        # transaction so this specific call can cancel or hold it.
        preflight = doc_ref.get(timeout=float(rpc_timeout))
        if not preflight.exists:
            return False
        preflight_data = preflight.to_dict() or {}
        previous = preflight_data.get("last_inbound_call_at") or 0
        if (
            preflight_data.get(NUMBER_RELEASE_CLAIM_FIELD) is None
            and isinstance(previous, (int, float))
            and seen_at - previous < 3600
        ):
            return True

        try:
            transaction = db.transaction(max_attempts=1)
        except TypeError:
            # Lightweight local fakes may not accept Firestore's optional
            # transaction setting; production always receives max_attempts=1.
            transaction = db.transaction()
        snapshot = doc_ref.get(transaction=transaction, timeout=float(rpc_timeout))
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        outcome = apply_release_aware_contractor_update(
            transaction,
            doc_ref,
            data,
            {"last_inbound_call_at": float(seen_at)},
        )
        transaction.commit(timeout=float(rpc_timeout))
        return outcome is not ReleaseAwareMutationResult.HELD

    try:
        return await loop.run_in_executor(None, _write)
    except Exception as e:
        logger.warning(
            "Inbound call safety transaction failed: contractor=%s error=%s",
            contractor_id,
            type(e).__name__,
        )
        return False


async def update_contractor(contractor_id: str, updates: dict) -> bool:
    """Update a contractor while respecting an in-flight number release.

    The common path covers inbound-call evidence, device registration,
    subscription webhooks, and API profile updates. A safety update made while
    a release is reserved is retained and cancels that release; a post-delete
    boundary mutation is durably held for reconciliation.
    """
    if (
        not isinstance(contractor_id, str)
        or not contractor_id
        or not isinstance(updates, dict)
        or NUMBER_RELEASE_CLAIM_FIELD in updates
    ):
        return False
    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    doc_ref = db.collection(COLLECTION).document(contractor_id)

    @firestore.transactional
    def _txn(transaction) -> ReleaseAwareMutationResult:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return ReleaseAwareMutationResult.HELD
        return apply_release_aware_contractor_update(
            transaction, doc_ref, snapshot.to_dict() or {}, updates,
        )

    outcome = await loop.run_in_executor(None, lambda: _txn(db.transaction()))
    if outcome is ReleaseAwareMutationResult.HELD:
        logger.warning("Contractor update held by strict number release: %s", contractor_id)
        return False
    if outcome is ReleaseAwareMutationResult.CANCELLED:
        logger.info("Contractor update cancelled pending number release: %s", contractor_id)
    else:
        logger.info("Contractor updated: %s", contractor_id)
    return True


@dataclass(frozen=True)
class StrictNumberReleaseClaim:
    """Opaque ownership token for one Twilio release attempt.

    ``update_time`` is audit metadata only. The durable nonce and phase are
    the release linearization contract; unrelated profile writes no longer
    turn a confirmed provider deletion into an avoidable stale-version hold.
    """

    nonce: str
    update_time: Any = None
    state: str = NUMBER_RELEASE_CLAIM_RESERVED


def _claim_nonce_matches(data: dict, nonce: str, *, state: str | None = None) -> bool:
    claim = data.get(NUMBER_RELEASE_CLAIM_FIELD)
    matches = (
        isinstance(claim, dict)
        and isinstance(claim.get("nonce"), str)
        and secrets.compare_digest(claim["nonce"], nonce)
    )
    if not matches:
        return False
    if state is None:
        return True
    return claim.get("state", NUMBER_RELEASE_CLAIM_RESERVED) == state


def _reserved_claim_is_reclaimable(claim: dict) -> bool:
    """Whether a provider-free reservation is old enough for safe replacement.

    ``deleting`` is intentionally never reclaimable: a crash there leaves the
    Twilio outcome unknown and requires reconciliation.  A malformed timestamp
    also fails closed rather than turning an unrecognized claim into a retry.
    """
    if claim.get("state", NUMBER_RELEASE_CLAIM_RESERVED) != NUMBER_RELEASE_CLAIM_RESERVED:
        return False
    claimed_at = claim.get("claimed_at")
    if (
        not isinstance(claimed_at, (int, float))
        or isinstance(claimed_at, bool)
        or not math.isfinite(claimed_at)
    ):
        return False
    return time.time() - float(claimed_at) >= NUMBER_RELEASE_RESERVATION_TTL_SECONDS


def _committed_claim_update_time(transaction) -> Any:
    """Return an optional post-commit version for diagnostics only."""
    write_results = getattr(transaction, "write_results", ())
    if len(write_results) != 1:
        return None
    update_time = getattr(write_results[0], "update_time", None)
    if update_time is None:
        return None
    try:
        return DatetimeWithNanoseconds.from_timestamp_pb(update_time)
    except (AttributeError, TypeError, ValueError):
        return None


def _cancel_release_claim(transaction, doc_ref, data: dict, *, kind: str) -> None:
    """Make a reserved release terminal when its observed state changed."""
    claim = dict(data[NUMBER_RELEASE_CLAIM_FIELD])
    claim.update({
        "state": NUMBER_RELEASE_CLAIM_CANCELLED,
        "cancelled_at": int(time.time()),
    })
    transaction.update(doc_ref, {
        NUMBER_RELEASE_CLAIM_FIELD: claim,
        NUMBER_RELEASE_CONFLICT_FIELD: {
            "at": int(time.time()),
            "phase": NUMBER_RELEASE_CLAIM_RESERVED,
            "kind": kind,
        },
    })


def _hold_release_claim(
    transaction,
    doc_ref,
    data: dict,
    *,
    kind: str,
    extra_updates: dict | None = None,
) -> None:
    """Put a nonce-owned release into a durable reconciliation state."""
    claim = dict(data[NUMBER_RELEASE_CLAIM_FIELD])
    claim.update({
        "state": NUMBER_RELEASE_CLAIM_NEEDS_ATTENTION,
        "held_at": int(time.time()),
    })
    updates = {
        NUMBER_RELEASE_CLAIM_FIELD: claim,
        NUMBER_RELEASE_CONFLICT_FIELD: {
            "at": int(time.time()),
            "phase": data[NUMBER_RELEASE_CLAIM_FIELD].get("state"),
            "kind": kind,
        },
    }
    if extra_updates:
        updates.update(extra_updates)
    transaction.update(doc_ref, updates)


async def claim_strict_number_release(
    contractor_id: str,
    *,
    strict_precondition: Callable[[dict], bool],
) -> Optional[StrictNumberReleaseClaim]:
    """Atomically reserve one release before any Twilio operation.

    A later profile write either cancels ``reserved`` while retaining its new
    safety evidence, or holds a ``deleting`` claim for reconciliation. A
    cancelled claim can be replaced only after a fresh eligibility evaluation.
    """
    if not isinstance(contractor_id, str) or not contractor_id or not callable(strict_precondition):
        return None

    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(contractor_id)
    nonce = secrets.token_urlsafe(24)
    loop = asyncio.get_running_loop()

    @firestore.transactional
    def claim(transaction) -> bool:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        if not isinstance(data, dict):
            return False
        existing = data.get(NUMBER_RELEASE_CLAIM_FIELD)
        if isinstance(existing, dict):
            existing_state = existing.get("state", NUMBER_RELEASE_CLAIM_RESERVED)
            if (
                existing_state != NUMBER_RELEASE_CLAIM_CANCELLED
                and not _reserved_claim_is_reclaimable(existing)
            ):
                return False
        elif existing is not None:
            return False
        expected_number = data.get("twilio_number")
        if not isinstance(expected_number, str) or not expected_number:
            return False
        try:
            eligible = strict_precondition(data) is True
        except Exception:
            logger.exception(
                "Strict Twilio release claim precondition failed for contractor %s",
                contractor_id,
            )
            return False
        if not eligible:
            return False
        transaction.update(doc_ref, {
            NUMBER_RELEASE_CLAIM_FIELD: {
                "nonce": nonce,
                "state": NUMBER_RELEASE_CLAIM_RESERVED,
                "claimed_at": int(time.time()),
                "expected_twilio_number": expected_number,
            },
        })
        return True

    transaction = db.transaction()

    def commit_claim() -> tuple[bool, Any]:
        claimed = claim(transaction)
        return claimed, _committed_claim_update_time(transaction) if claimed else None

    claimed, update_time = await loop.run_in_executor(None, commit_claim)
    if not claimed:
        return None
    return StrictNumberReleaseClaim(
        nonce=nonce,
        update_time=update_time,
        state=NUMBER_RELEASE_CLAIM_RESERVED,
    )


async def begin_strict_number_release_delete(
    contractor_id: str,
    claim: StrictNumberReleaseClaim,
    *,
    strict_precondition: Callable[[dict], bool],
) -> Optional[StrictNumberReleaseClaim]:
    """Atomically cross the local point of no return before Twilio DELETE.

    This is the release linearization point. It requires the same nonce,
    ``reserved`` phase, expected phone binding, and a fresh full safety
    predicate, then changes only the claim phase to ``deleting``.
    """
    if (
        not isinstance(contractor_id, str)
        or not contractor_id
        or not isinstance(claim, StrictNumberReleaseClaim)
        or claim.state != NUMBER_RELEASE_CLAIM_RESERVED
        or not callable(strict_precondition)
    ):
        return None
    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(contractor_id)
    loop = asyncio.get_running_loop()

    @firestore.transactional
    def begin(transaction) -> bool:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        if not _claim_nonce_matches(
            data, claim.nonce, state=NUMBER_RELEASE_CLAIM_RESERVED,
        ):
            return False
        stored_claim = data[NUMBER_RELEASE_CLAIM_FIELD]
        expected_number = stored_claim.get("expected_twilio_number")
        if (
            not isinstance(expected_number, str)
            or not expected_number
            or data.get("twilio_number") != expected_number
        ):
            _cancel_release_claim(transaction, doc_ref, data, kind="release_binding_changed")
            return False
        try:
            eligible = strict_precondition(data) is True
        except Exception:
            logger.exception(
                "Strict Twilio release delete precondition failed for contractor %s",
                contractor_id,
            )
            eligible = False
        if not eligible:
            _cancel_release_claim(transaction, doc_ref, data, kind="release_precondition_changed")
            return False
        deleting = dict(stored_claim)
        deleting.update({
            "state": NUMBER_RELEASE_CLAIM_DELETING,
            "deleting_at": int(time.time()),
        })
        transaction.update(doc_ref, {NUMBER_RELEASE_CLAIM_FIELD: deleting})
        return True

    transaction = db.transaction()

    def commit_begin() -> tuple[bool, Any]:
        began = begin(transaction)
        return began, _committed_claim_update_time(transaction) if began else None

    began, update_time = await loop.run_in_executor(None, commit_begin)
    if not began:
        return None
    return StrictNumberReleaseClaim(
        nonce=claim.nonce,
        update_time=update_time,
        state=NUMBER_RELEASE_CLAIM_DELETING,
    )


async def hold_strict_number_release(
    contractor_id: str,
    claim: StrictNumberReleaseClaim,
    *,
    anomaly: dict | None = None,
) -> bool:
    """Preserve an owned strict claim for operator reconciliation."""
    if (
        not isinstance(contractor_id, str)
        or not contractor_id
        or not isinstance(claim, StrictNumberReleaseClaim)
    ):
        return False
    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(contractor_id)
    loop = asyncio.get_running_loop()

    @firestore.transactional
    def hold(transaction) -> bool:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        if not _claim_nonce_matches(data, claim.nonce, state=claim.state):
            return False
        _hold_release_claim(
            transaction,
            doc_ref,
            data,
            kind="provider_outcome_uncertain",
            extra_updates={"number_release_anomaly": anomaly} if anomaly is not None else None,
        )
        return True

    return await loop.run_in_executor(None, lambda: hold(db.transaction()))


async def finalize_strict_number_release(
    contractor_id: str,
    claim: StrictNumberReleaseClaim,
    release_updates: dict,
    *,
    strict_precondition: Callable[[dict], bool],
    deactivate_profile: bool,
    deactivation_updates: dict | None = None,
) -> bool:
    """Conditionally commit a confirmed release and optional deactivation.

    Firestore cannot join Twilio's DELETE. The nonce-owned ``deleting`` phase
    is therefore retained until this transaction verifies the live safety
    predicate again and atomically commits the profile transition.
    """
    if (
        not isinstance(contractor_id, str)
        or not contractor_id
        or not isinstance(claim, StrictNumberReleaseClaim)
        or claim.state != NUMBER_RELEASE_CLAIM_DELETING
        or not isinstance(release_updates, dict)
        or not callable(strict_precondition)
        or not isinstance(deactivate_profile, bool)
        or (deactivation_updates is not None and not isinstance(deactivation_updates, dict))
    ):
        return False
    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(contractor_id)
    loop = asyncio.get_running_loop()

    @firestore.transactional
    def finalize(transaction) -> bool:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False
        data = snapshot.to_dict() or {}
        if not _claim_nonce_matches(
            data, claim.nonce, state=NUMBER_RELEASE_CLAIM_DELETING,
        ):
            return False
        stored_claim = data[NUMBER_RELEASE_CLAIM_FIELD]
        expected_number = stored_claim.get("expected_twilio_number")
        try:
            eligible = strict_precondition(data) is True
        except Exception:
            logger.exception(
                "Strict Twilio release finalization precondition failed for contractor %s",
                contractor_id,
            )
            eligible = False
        if (
            not isinstance(expected_number, str)
            or data.get("twilio_number") != expected_number
            or not eligible
        ):
            _hold_release_claim(transaction, doc_ref, data, kind="release_finalization_conflict")
            return False
        updates = {
            **release_updates,
            NUMBER_RELEASE_CLAIM_FIELD: None,
        }
        if deactivate_profile:
            updates.update({
                "active": False,
                "deactivated_at": int(time.time()),
            })
            if deactivation_updates:
                updates.update(deactivation_updates)
        transaction.update(doc_ref, updates)
        return True

    return await loop.run_in_executor(None, lambda: finalize(db.transaction()))


async def _create_regulatory_bundle(client, loop, country_code: str, business_name: str, address: str, city: str) -> str:
    """Create a Twilio regulatory bundle for EU/BR number provisioning.

    Returns the bundle SID. Raises if the bundle cannot be created or approved.
    """
    from app.config import settings

    country_name = COUNTRY_NAMES.get(country_code, "")

    # twilio 9.x requires a contact email on the bundle; without one the SDK
    # raises TypeError inside the executor. Refuse clearly before any call.
    contact_email = (settings.twilio_regulatory_contact_email or "").strip()
    if not contact_email:
        raise Exception(
            f"Regulatory contact email not configured for {country_name} number provisioning"
        )

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
            email=contact_email,
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
        try:
            from app.services.sms_sender_enrollment import ensure_sms_sender_membership
            await ensure_sms_sender_membership(contractor_id, existing_number)
        except Exception:
            logger.warning("SMS sender enrollment check failed on existing number return")
        if contractor.get("country_code") == "CA":
            from app.services.canada_sms_enrollment import ensure_canada_sms_sender_membership
            await ensure_canada_sms_sender_membership(contractor_id, existing_number)
        return existing_number

    from app.services.country_policy import (
        is_country_available,
        is_recognized_country,
        validate_phone_and_region,
        CountryPhoneMismatchError,
        InvalidPhoneError,
    )

    # Derive effective country from stored profile and owner phone
    req_country = (country_code or "").strip().upper()
    if req_country and not is_recognized_country(req_country):
        raise Exception(f"Unsupported country code: {country_code}")

    stored_country = (contractor.get("country_code") or "").strip().upper()
    if stored_country and not is_recognized_country(stored_country):
        raise Exception(f"Unsupported stored country code: {stored_country}")

    owner_phone = (contractor.get("owner_phone") or "").strip()

    if owner_phone:
        try:
            _, derived_region = validate_phone_and_region(owner_phone, default_country=stored_country)
        except (CountryPhoneMismatchError, InvalidPhoneError) as e:
            raise Exception(f"Contractor phone validation failed: {e}")
        effective_country = derived_region or stored_country or "UNKNOWN"
    else:
        effective_country = stored_country if stored_country else "US"

    if req_country and req_country != effective_country:
        raise Exception(f"Requested country {req_country} conflicts with contractor country {effective_country}")

    if not is_country_available(effective_country):
        raise Exception(f"Country {effective_country} is not available for number provisioning")

    from twilio.rest import Client
    from app.config import settings

    business_address = business_city = business_name = ""
    if effective_country in REGULATORY_COUNTRIES:
        business_address = contractor.get("business_address", "")
        business_city = contractor.get("business_city", "")
        business_name = contractor.get("business_name", "")
        if not business_address or not business_city:
            raise Exception("Business address and city required for number provisioning in this country")

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    loop = asyncio.get_event_loop()

    bundle_sid = None
    if effective_country in REGULATORY_COUNTRIES:
        bundle_sid = await _create_regulatory_bundle(
            client, loop, effective_country, business_name, business_address, business_city
        )

    # Search for available numbers (voice + SMS required for US/CA)
    search_params = {"voice_enabled": True, "sms_enabled": True}
    if area_code:
        search_params["area_code"] = area_code

    numbers = await loop.run_in_executor(
        None,
        lambda: client.available_phone_numbers(effective_country).local.list(**search_params, limit=1)
    )

    if not numbers and area_code:
        # Retry without area code
        search_params.pop("area_code", None)
        numbers = await loop.run_in_executor(
            None,
            lambda: client.available_phone_numbers(effective_country).local.list(**search_params, limit=1)
        )

    if not numbers:
        country_name = COUNTRY_NAMES.get(effective_country, effective_country)
        raise Exception(f"No phone numbers available in {country_name}")

    # Buy the number (bundle_sid goes here, NOT in search)
    callback_base_url = settings.cloud_run_url.rstrip('/')
    webhook_url = f"{callback_base_url}/webhooks/twilio/incoming"
    status_url = f"{callback_base_url}/webhooks/twilio/status"

    purchase_params = {
        "phone_number": numbers[0].phone_number,
        "voice_url": webhook_url,
        "voice_method": "POST",
        "status_callback": status_url,
        "status_callback_method": "POST",
        "sms_url": f"{callback_base_url}/webhooks/twilio/mms-incoming",
        "sms_method": "POST",
    }
    if bundle_sid:
        purchase_params["bundle_sid"] = bundle_sid

    purchased = await loop.run_in_executor(
        None,
        lambda: client.incoming_phone_numbers.create(**purchase_params)
    )

    # Derive capabilities from actual provider response where available (only actual bools, no default True)
    caps = None
    raw_caps = getattr(purchased, "capabilities", None)
    if isinstance(raw_caps, dict):
        extracted = {}
        for k in ("voice", "sms", "mms"):
            v = raw_caps.get(k)
            if isinstance(v, bool):
                extracted[k] = v
        caps = extracted if extracted else None
    elif raw_caps is not None:
        extracted = {}
        for k in ("voice", "sms", "mms"):
            v = getattr(raw_caps, k, None)
            if isinstance(v, bool):
                extracted[k] = v
        caps = extracted if extracted else None

    # Update contractor profile with the number and protected service binding metadata
    await update_contractor(
        contractor_id,
        {
            "twilio_number": purchased.phone_number,
            "provisioned_country_code": effective_country,
            "number_provider": "twilio",
            "number_type": "local",
            "number_capabilities": caps,
        },
    )

    try:
        from app.services.sms_sender_enrollment import ensure_sms_sender_membership
        await ensure_sms_sender_membership(contractor_id, purchased.phone_number)
    except Exception:
        logger.warning("SMS sender enrollment failed on new number provisioning")

    if effective_country == "CA":
        from app.services.canada_sms_enrollment import ensure_canada_sms_sender_membership
        await ensure_canada_sms_sender_membership(contractor_id, purchased.phone_number)

    logger.info(f"Provisioned {redact_phone(purchased.phone_number)} ({effective_country}) for contractor {contractor_id}")
    return purchased.phone_number


async def release_twilio_number(
    contractor_id: str,
    *,
    require_confirmed: bool = False,
    strict_precondition: Callable[[dict], bool] | None = None,
    strict_release_claim: StrictNumberReleaseClaim | None = None,
    deactivate_profile: bool = False,
    deactivation_updates: dict | None = None,
) -> bool:
    """Release a Twilio number through one nonce-owned state machine.

    Every provider DELETE, including manual and embedded cleanup paths, first
    reserves the contractor and atomically enters ``deleting``.  A release
    only becomes visible in the profile through conditional finalization.
    That prevents two paths from deleting the same number and keeps a live
    call or subscription update from being silently overwritten.
    """
    from twilio.rest import Client
    from app.config import settings

    if deactivation_updates is not None and not isinstance(deactivation_updates, dict):
        return False
    if strict_release_claim is not None and (
        not require_confirmed or strict_precondition is None
    ):
        logger.error(
            "Strict Twilio release claim lacks its confirmed precondition for contractor %s",
            contractor_id,
        )
        return False

    contractor = await get_contractor(contractor_id)
    if not contractor or not isinstance(contractor.get("twilio_number"), str) or not contractor.get("twilio_number"):
        logger.warning("No Twilio number to release for contractor %s", contractor_id)
        return False
    twilio_number = contractor["twilio_number"]

    def release_precondition(candidate: dict) -> bool:
        if not isinstance(candidate, dict) or candidate.get("twilio_number") != twilio_number:
            return False
        if strict_precondition is None:
            return True
        try:
            return strict_precondition(candidate) is True
        except Exception:
            logger.exception(
                "Strict Twilio release precondition failed for contractor %s; preserving profile",
                contractor_id,
            )
            return False

    if not release_precondition(contractor):
        logger.warning(
            "Twilio release precondition changed for contractor %s; preserving profile",
            contractor_id,
        )
        return False

    # Manual account deletion, phone-number release, embedded cleanup, and
    # the strict external worker all acquire this same ownership token. The
    # external worker supplies its already-claimed token; every other entry
    # point claims a current number binding here.
    release_claim = strict_release_claim
    if release_claim is None:
        release_claim = await claim_strict_number_release(
            contractor_id, strict_precondition=release_precondition,
        )
    if release_claim is None:
        logger.warning("Twilio release is already owned or held for contractor %s", contractor_id)
        return False

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    loop = asyncio.get_running_loop()
    anomaly = {"at": int(time.time()), "number": twilio_number}
    release_updates = {
        "twilio_number": "",
        "number_released_at": int(time.time()),
        "provisioned_country_code": None,
        "number_provider": None,
        "number_type": None,
        "number_capabilities": None,
    }

    try:
        numbers = await loop.run_in_executor(
            None,
            lambda: client.incoming_phone_numbers.list(phone_number=twilio_number, limit=1),
        )
    except Exception:
        # A reservation has already been made. Do not let a retry guess
        # whether Twilio processed a request that failed locally.
        await hold_strict_number_release(contractor_id, release_claim, anomaly=anomaly)
        raise

    if not numbers:
        logger.error(
            "Twilio number %s for contractor %s not found in Twilio account; recording release anomaly",
            redact_phone(twilio_number), contractor_id,
        )
        if require_confirmed:
            await hold_strict_number_release(contractor_id, release_claim, anomaly=anomaly)
            return False
        # Legacy callers historically clear a missing provider binding. Keep
        # that behavior, but cross the same local ownership boundary first.
        deleting_claim = await begin_strict_number_release_delete(
            contractor_id, release_claim, strict_precondition=release_precondition,
        )
        if deleting_claim is None:
            return False
        release_updates["number_release_anomaly"] = anomaly
    else:
        deleting_claim = await begin_strict_number_release_delete(
            contractor_id, release_claim, strict_precondition=release_precondition,
        )
        if deleting_claim is None:
            return False
        try:
            deleted = await loop.run_in_executor(None, numbers[0].delete)
        except Exception as error:
            from twilio.base.exceptions import TwilioRestException

            if isinstance(error, TwilioRestException) and error.status == 404:
                # Twilio confirms the desired provider state already holds.
                deleted = True
                logger.info(
                    "Twilio number %s for contractor %s was already released",
                    redact_phone(twilio_number), contractor_id,
                )
            else:
                await hold_strict_number_release(
                    contractor_id, deleting_claim, anomaly=anomaly,
                )
                raise
        if deleted is not True:
            logger.error(
                "Twilio release result unconfirmed for contractor %s; preserving profile for reconciliation",
                contractor_id,
            )
            await hold_strict_number_release(
                contractor_id, deleting_claim, anomaly=anomaly,
            )
            return False

    finalized = await finalize_strict_number_release(
        contractor_id,
        deleting_claim,
        release_updates,
        strict_precondition=release_precondition,
        deactivate_profile=deactivate_profile,
        deactivation_updates=deactivation_updates,
    )
    if not finalized:
        # If finalization itself observed a conflict it has already moved the
        # claim to needs_attention. This best-effort hold covers transaction
        # failures before that durable transition.
        await hold_strict_number_release(contractor_id, deleting_claim, anomaly=anomaly)
        return False

    logger.info(
        "Released Twilio number %s for contractor %s",
        redact_phone(twilio_number), contractor_id,
    )
    return True


async def deactivate_contractor(
    contractor_id: str,
    user_requested: bool = False,
    *,
    require_confirmed_number_release: bool = False,
    strict_number_release_precondition: Callable[[dict], bool] | None = None,
    strict_number_release_claim: StrictNumberReleaseClaim | None = None,
) -> bool:
    """Deactivate an account only after its number transition is settled."""
    contractor = await get_contractor(contractor_id)
    if contractor is None:
        return False

    deactivation_updates: dict[str, Any] = {}
    if user_requested:
        # This remains the sole account-erasure consent signal.
        deactivation_updates["deletion_requested_at"] = int(time.time())

    has_twilio_number = isinstance(contractor.get("twilio_number"), str) and bool(
        contractor.get("twilio_number"),
    )
    if has_twilio_number:
        release_kwargs: dict[str, Any] = {
            "require_confirmed": require_confirmed_number_release,
            "deactivate_profile": True,
            "deactivation_updates": deactivation_updates,
        }
        if strict_number_release_precondition is not None:
            release_kwargs["strict_precondition"] = strict_number_release_precondition
        if strict_number_release_claim is not None:
            release_kwargs["strict_release_claim"] = strict_number_release_claim
        released = await release_twilio_number(contractor_id, **release_kwargs)
        if not released:
            # A live route stays intact when Twilio or Firestore is uncertain.
            return False
    else:
        updates = {
            "active": False,
            "deactivated_at": int(time.time()),
            **deactivation_updates,
        }
        if not await update_contractor(contractor_id, updates):
            return False

    # Drop this instance's cached token mappings only after the final profile
    # transition succeeded. Other warm instances retain their cache until
    # recycle, the existing bounded residual.
    from app.middleware.auth import invalidate_contractor_tokens
    invalidate_contractor_tokens(contractor_id)
    logger.info("Contractor deactivated: %s", contractor_id)
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


class SubscriptionEntitlementActivationOutcome(str, Enum):
    """Outcome for a verified entitlement write at a release-state boundary."""

    APPLIED = "applied"
    RELEASE_CONFLICT = "release_conflict"
    REJECTED = "rejected"


_last_subscription_entitlement_activation: ContextVar[
    SubscriptionEntitlementActivationOutcome | None
] = ContextVar("last_subscription_entitlement_activation", default=None)


def take_subscription_entitlement_activation_outcome(
) -> SubscriptionEntitlementActivationOutcome | None:
    """Consume the typed result of the boolean compatibility wrapper.

    Existing callers use the boolean helper. The subscription verifier needs to
    distinguish its release-state hold from a malformed entitlement, without
    changing that public boolean contract.
    """
    outcome = _last_subscription_entitlement_activation.get()
    _last_subscription_entitlement_activation.set(None)
    return outcome


async def activate_subscription_entitlement_with_outcome(
    contractor_id: str,
    tier: str,
    expires_ts: float,
    original_transaction_id: str,
    expected_subscription_uuid: str,
) -> SubscriptionEntitlementActivationOutcome:
    """Apply a verified entitlement and distinguish a release hold from rejection.

    A verified StoreKit transaction that races the irreversible number-release
    boundary must remain unfinished on the device. Returning a typed conflict
    lets the API send a retryable response while the durable reconciliation
    record keeps the entitlement data for an operator.
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
        return SubscriptionEntitlementActivationOutcome.REJECTED

    from google.cloud import firestore as fs

    db = get_firestore_client()
    doc_ref = db.collection(COLLECTION).document(contractor_id)
    loop = asyncio.get_event_loop()

    @fs.transactional
    def _txn(transaction) -> SubscriptionEntitlementActivationOutcome:
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            return SubscriptionEntitlementActivationOutcome.REJECTED
        data = snapshot.to_dict() or {}
        if data.get("active") is not True:
            return SubscriptionEntitlementActivationOutcome.REJECTED
        if data.get("subscription_uuid") != expected_subscription_uuid:
            return SubscriptionEntitlementActivationOutcome.REJECTED

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

        mutation = apply_release_aware_contractor_update(
            transaction, doc_ref, data, updates,
        )
        if mutation is ReleaseAwareMutationResult.HELD:
            return SubscriptionEntitlementActivationOutcome.RELEASE_CONFLICT
        return SubscriptionEntitlementActivationOutcome.APPLIED

    transaction = db.transaction()
    return await loop.run_in_executor(None, lambda: _txn(transaction))


async def activate_subscription_entitlement(
    contractor_id: str,
    tier: str,
    expires_ts: float,
    original_transaction_id: str,
    expected_subscription_uuid: str,
) -> bool:
    """Backward-compatible boolean wrapper for entitlement callers."""
    outcome = await activate_subscription_entitlement_with_outcome(
        contractor_id,
        tier,
        expires_ts,
        original_transaction_id,
        expected_subscription_uuid,
    )
    _last_subscription_entitlement_activation.set(outcome)
    return outcome is SubscriptionEntitlementActivationOutcome.APPLIED


class BindingBackfillOutcome(str, Enum):
    REPAIRED = "repaired"
    IDEMPOTENT_SAME = "idempotent_same"
    SUPERSEDED = "superseded"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    UUID_MISMATCH = "uuid_mismatch"
    NOT_FOUND_OR_INACTIVE = "not_found_or_inactive"
    RELEASE_CONFLICT = "release_conflict"


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
            outcome = apply_release_aware_contractor_update(
                transaction,
                doc_ref,
                data,
                {
                    "subscription_original_transaction_id": clean_orig,
                    "subscription_auto_renews": None,
                    "subscription_renewal_status_signed_at_ms": None,
                },
            )
            return (
                BindingBackfillOutcome.REPAIRED
                if outcome is not ReleaseAwareMutationResult.HELD
                else BindingBackfillOutcome.RELEASE_CONFLICT
            )

        if current_original.strip() == clean_orig:
            if current_original == clean_orig:
                return BindingBackfillOutcome.IDEMPOTENT_SAME
            outcome = apply_release_aware_contractor_update(
                transaction,
                doc_ref,
                data,
                {
                    "subscription_original_transaction_id": clean_orig,
                    "subscription_auto_renews": None,
                    "subscription_renewal_status_signed_at_ms": None,
                },
            )
            return (
                BindingBackfillOutcome.REPAIRED
                if outcome is not ReleaseAwareMutationResult.HELD
                else BindingBackfillOutcome.RELEASE_CONFLICT
            )

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

        outcome = apply_release_aware_contractor_update(
            transaction,
            doc_ref,
            data,
            {
                "subscription_auto_renews": auto_renews,
                "subscription_renewal_status_signed_at_ms": signed_at_ms,
            },
        )
        return outcome is not ReleaseAwareMutationResult.HELD

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
