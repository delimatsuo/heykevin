"""Guarded SMS sender enrollment and recovery service.

Enrolls or confirms only the single freshly verified US number already assigned
to the requesting active account in the explicitly pinned Kevin Messaging Service
campaign. Default OFF.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from enum import Enum
from typing import Any, Optional

import phonenumbers
from google.cloud.firestore_v1.base_query import FieldFilter
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from app.config import (
    PRODUCTION_GCP_PROJECT_ID,
    settings,
)
from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

COLLECTION = "contractors"

_ACCOUNT_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}\Z")
_MESSAGING_SERVICE_SID_RE = re.compile(r"^MG[0-9a-fA-F]{32}\Z")
_PHONE_NUMBER_SID_RE = re.compile(r"^PN[0-9a-fA-F]{32}\Z")
_CAMPAIGN_SID_RE = re.compile(r"^QE[0-9a-fA-F]{32}\Z")
_SHA256_LOWER_RE = re.compile(r"^[0-9a-f]{64}\Z")
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}\Z")

PROJECTED_FIELDS = [
    "twilio_number",
    "active",
    "country_code",
    "provisioned_country_code",
    "number_provider",
    "number_type",
    "number_capabilities",
    "owner_sms_enabled",
    "owner_sms_opted_out",
    "deletion_requested_at",
    "deactivated_at",
    "deleted_app_detected_at",
    "number_released_at",
]

MAX_BUDGET_SECONDS = 15.0

# Dedicated silent transport logger that drops all HTTP client request/response logs
# without mutating global logger configuration.
_SILENT_TRANSPORT_LOGGER = logging.getLogger("app.services.sms_sender_enrollment.twilio_transport")
_SILENT_TRANSPORT_LOGGER.handlers = [logging.NullHandler()]
_SILENT_TRANSPORT_LOGGER.propagate = False
_SILENT_TRANSPORT_LOGGER.setLevel(logging.CRITICAL + 1)


class _BudgetExceededError(Exception):
    """Raised when monotonic operation budget is exceeded."""
    pass


class _OperationCancelledError(Exception):
    """Raised when cancellation signal is observed."""
    pass


class EnrollmentStatus(str, Enum):
    DISABLED = "disabled"
    ALREADY_MEMBER = "already_member"
    ENROLLED = "enrolled"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


class EnrollmentReason(str, Enum):
    FEATURE_DISABLED = "feature_disabled"
    INVALID_CONFIGURATION = "invalid_configuration"
    NON_PRODUCTION_ENVIRONMENT = "non_production_environment"
    CLIENT_IDENTITY_MISMATCH = "client_identity_mismatch"
    INVALID_INPUT = "invalid_input"
    NON_US_PHONE_REGION = "non_us_phone_region"
    ASSIGNMENT_NOT_FOUND = "assignment_not_found"
    ASSIGNMENT_AMBIGUOUS = "assignment_ambiguous"
    CONTRACTOR_ID_MISMATCH = "contractor_id_mismatch"
    ASSIGNMENT_INACTIVE = "assignment_inactive"
    COUNTRY_NOT_US = "country_not_us"
    PROVIDER_NOT_TWILIO = "provider_not_twilio"
    NUMBER_TYPE_NOT_LOCAL = "number_type_not_local"
    CAPABILITIES_INSUFFICIENT = "capabilities_insufficient"
    OWNER_SMS_NOT_ENABLED = "owner_sms_not_enabled"
    OWNER_SMS_OPTED_OUT = "owner_sms_opted_out"
    LIFECYCLE_HOLD_ACTIVE = "lifecycle_hold_active"
    SERVICE_VERIFICATION_FAILED = "service_verification_failed"
    CAMPAIGN_VERIFICATION_FAILED = "campaign_verification_failed"
    CAMPAIGN_DIGEST_MISMATCH = "campaign_digest_mismatch"
    INCOMING_NUMBER_NOT_FOUND = "incoming_number_not_found"
    INCOMING_NUMBER_AMBIGUOUS = "incoming_number_ambiguous"
    INCOMING_NUMBER_ACCOUNT_MISMATCH = "incoming_number_account_mismatch"
    INCOMING_NUMBER_ROUTING_MISMATCH = "incoming_number_routing_mismatch"
    MEMBERSHIP_PRESENT_VALID = "membership_present_valid"
    MEMBERSHIP_MALFORMED = "membership_malformed"
    OTHER_SERVICE_CONFLICT = "other_service_conflict"
    PREWRITE_DRIFT_DETECTED = "prewrite_drift_detected"
    POSTWRITE_DRIFT_DETECTED = "postwrite_drift_detected"
    BUDGET_EXCEEDED = "budget_exceeded"
    OPERATION_CANCELLED = "operation_cancelled"
    CREATE_FAILED_UNCERTAIN = "create_failed_uncertain"
    READBACK_FAILED_ABSENT = "readback_failed_absent"
    READBACK_FAILED_UNCERTAIN = "readback_failed_uncertain"
    READBACK_VERIFIED = "readback_verified"
    UNEXPECTED_ERROR = "unexpected_error"


class SmsSenderEnrollmentResult:
    """Typed outcome of sender enrollment. Contains no tenant IDs or phone numbers."""

    def __init__(
        self,
        status: EnrollmentStatus,
        reason: EnrollmentReason,
        enrolled: bool = False,
        mutated: Optional[bool] = False,
    ):
        self.status = status
        self.reason = reason
        self.enrolled = enrolled
        self.mutated = mutated

    def __repr__(self) -> str:
        return (
            f"SmsSenderEnrollmentResult(status={self.status.value!r}, "
            f"reason={self.reason.value!r}, enrolled={self.enrolled!r}, mutated={self.mutated!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason.value,
            "enrolled": self.enrolled,
            "mutated": self.mutated,
        }


class FrozenRoutingSnapshot:
    """Immutable snapshot of frozen routing attributes for an IncomingPhoneNumber."""

    def __init__(
        self,
        sid: str,
        phone_number: str,
        account_sid: str,
        origin: str,
        number_type: str,
        voice_url: str,
        voice_method: str,
        voice_fallback_url: Optional[str],
        voice_fallback_method: Optional[str],
        status_callback: str,
        status_callback_method: str,
        sms_url: str,
        sms_method: str,
        sms_fallback_url: Optional[str],
        sms_fallback_method: Optional[str],
        voice_application_sid: Optional[str],
        sms_application_sid: Optional[str],
        trunk_sid: Optional[str],
        capabilities: dict[str, bool],
    ):
        self.sid = sid
        self.phone_number = phone_number
        self.account_sid = account_sid
        self.origin = origin
        self.number_type = number_type
        self.voice_url = voice_url
        self.voice_method = voice_method
        self.voice_fallback_url = voice_fallback_url
        self.voice_fallback_method = voice_fallback_method
        self.status_callback = status_callback
        self.status_callback_method = status_callback_method
        self.sms_url = sms_url
        self.sms_method = sms_method
        self.sms_fallback_url = sms_fallback_url
        self.sms_fallback_method = sms_fallback_method
        self.voice_application_sid = voice_application_sid
        self.sms_application_sid = sms_application_sid
        self.trunk_sid = trunk_sid
        self.capabilities = dict(capabilities)

    def matches(self, other_incoming: Any) -> bool:
        """Compare another IncomingPhoneNumber resource against this frozen snapshot."""
        if getattr(other_incoming, "sid", None) != self.sid:
            return False
        if getattr(other_incoming, "phone_number", None) != self.phone_number:
            return False
        if getattr(other_incoming, "account_sid", None) != self.account_sid:
            return False
        if getattr(other_incoming, "origin", None) != self.origin:
            return False

        other_type = getattr(other_incoming, "type", None) or getattr(
            other_incoming, "phone_number_type", None
        )
        if other_type != self.number_type:
            return False

        if getattr(other_incoming, "voice_url", "") != self.voice_url:
            return False
        if getattr(other_incoming, "voice_method", "") != self.voice_method:
            return False
        if getattr(other_incoming, "voice_fallback_url", None) != self.voice_fallback_url:
            return False
        if getattr(other_incoming, "voice_fallback_method", None) != self.voice_fallback_method:
            return False

        if getattr(other_incoming, "status_callback", "") != self.status_callback:
            return False
        if getattr(other_incoming, "status_callback_method", "") != self.status_callback_method:
            return False

        if getattr(other_incoming, "sms_url", "") != self.sms_url:
            return False
        if getattr(other_incoming, "sms_method", "") != self.sms_method:
            return False
        if getattr(other_incoming, "sms_fallback_url", None) != self.sms_fallback_url:
            return False
        if getattr(other_incoming, "sms_fallback_method", None) != self.sms_fallback_method:
            return False

        if getattr(other_incoming, "voice_application_sid", None) != self.voice_application_sid:
            return False
        if getattr(other_incoming, "sms_application_sid", None) != self.sms_application_sid:
            return False
        if getattr(other_incoming, "trunk_sid", None) != self.trunk_sid:
            return False

        other_caps = _extract_capabilities(getattr(other_incoming, "capabilities", None))
        if other_caps != self.capabilities:
            return False

        return True


def _extract_capabilities(raw_caps: Any) -> dict[str, bool]:
    """Extract actual boolean voice and sms capabilities from a dict or object."""
    caps: dict[str, bool] = {}
    if isinstance(raw_caps, dict):
        for k in ("voice", "sms", "mms"):
            v = raw_caps.get(k)
            if isinstance(v, bool):
                caps[k] = v
    elif raw_caps is not None and not isinstance(raw_caps, (list, tuple, set, str)):
        for k in ("voice", "sms", "mms"):
            v = getattr(raw_caps, k, None)
            if isinstance(v, bool):
                caps[k] = v
    return caps


def _validate_membership_capabilities(raw_caps: Any) -> bool:
    """Validate Twilio Messaging Service PhoneNumber capabilities array.

    Twilio Service PhoneNumber capabilities is an array like ["Voice", "SMS", "MMS"].
    Must be a non-empty sequence/set of non-empty strings containing both 'voice' and 'sms'.
    Fails closed on missing or malformed values.
    """
    if not isinstance(raw_caps, (list, tuple, set)):
        return False
    if not raw_caps:
        return False
    normalized: set[str] = set()
    for item in raw_caps:
        if not isinstance(item, str) or not item.strip():
            return False
        normalized.add(item.strip().lower())
    return "voice" in normalized and "sms" in normalized


def _compute_campaign_scope_digest(
    description: Any,
    message_flow: Any,
    usecase: Any,
) -> str | None:
    """Compute deterministic lowercase SHA-256 of campaign scope JSON."""
    if not isinstance(description, str) or not description.strip():
        return None
    if not isinstance(message_flow, str) or not message_flow.strip():
        return None
    payload = {
        "description": description,
        "message_flow": message_flow,
        "us_app_to_person_usecase": usecase,
    }
    canonical_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest().lower()


def _validate_assignment_record(
    doc: Any,
    contractor_id: str,
    canonical_number: str,
) -> tuple[bool, EnrollmentReason]:
    """Validate projected assignment document invariants."""
    if getattr(doc, "id", None) != contractor_id:
        return False, EnrollmentReason.CONTRACTOR_ID_MISMATCH

    data = doc.to_dict() if hasattr(doc, "to_dict") else {}
    if not isinstance(data, dict):
        return False, EnrollmentReason.ASSIGNMENT_NOT_FOUND

    if data.get("twilio_number") != canonical_number:
        return False, EnrollmentReason.ASSIGNMENT_NOT_FOUND

    if data.get("active") is not True:
        return False, EnrollmentReason.ASSIGNMENT_INACTIVE

    if data.get("country_code") != "US" or data.get("provisioned_country_code") != "US":
        return False, EnrollmentReason.COUNTRY_NOT_US

    if data.get("number_provider") != "twilio":
        return False, EnrollmentReason.PROVIDER_NOT_TWILIO

    if data.get("number_type") != "local":
        return False, EnrollmentReason.NUMBER_TYPE_NOT_LOCAL

    caps = _extract_capabilities(data.get("number_capabilities"))
    if caps.get("voice") is not True or caps.get("sms") is not True:
        return False, EnrollmentReason.CAPABILITIES_INSUFFICIENT

    if data.get("owner_sms_enabled") is not True:
        return False, EnrollmentReason.OWNER_SMS_NOT_ENABLED

    if data.get("owner_sms_opted_out") is not False:
        return False, EnrollmentReason.OWNER_SMS_OPTED_OUT

    for hold in (
        "deletion_requested_at",
        "deactivated_at",
        "deleted_app_detected_at",
        "number_released_at",
    ):
        if data.get(hold) is not None:
            return False, EnrollmentReason.LIFECYCLE_HOLD_ACTIVE

    return True, EnrollmentReason.READBACK_VERIFIED


def _validate_incoming_resource(
    incoming: Any,
    canonical_number: str,
    account_sid: str,
) -> tuple[bool, EnrollmentReason, Optional[FrozenRoutingSnapshot]]:
    """Validate IncomingPhoneNumber resource invariants and routing."""
    if getattr(incoming, "phone_number", None) != canonical_number:
        return False, EnrollmentReason.INCOMING_NUMBER_NOT_FOUND, None

    if getattr(incoming, "account_sid", None) != account_sid:
        return False, EnrollmentReason.INCOMING_NUMBER_ACCOUNT_MISMATCH, None

    pn_sid = getattr(incoming, "sid", "")
    if not isinstance(pn_sid, str) or not _PHONE_NUMBER_SID_RE.fullmatch(pn_sid):
        return False, EnrollmentReason.INCOMING_NUMBER_ROUTING_MISMATCH, None

    origin = getattr(incoming, "origin", None)
    if origin != "twilio":
        return False, EnrollmentReason.INCOMING_NUMBER_ROUTING_MISMATCH, None

    number_type = getattr(incoming, "type", None) or getattr(incoming, "phone_number_type", None)
    if number_type != "local":
        return False, EnrollmentReason.NUMBER_TYPE_NOT_LOCAL, None

    caps = _extract_capabilities(getattr(incoming, "capabilities", None))
    if caps.get("voice") is not True or caps.get("sms") is not True:
        return False, EnrollmentReason.CAPABILITIES_INSUFFICIENT, None

    base_url = settings.cloud_run_url.rstrip("/")
    exp_voice = f"{base_url}/webhooks/twilio/incoming"
    exp_status = f"{base_url}/webhooks/twilio/status"
    exp_sms = f"{base_url}/webhooks/twilio/mms-incoming"

    voice_url = getattr(incoming, "voice_url", "")
    voice_method = getattr(incoming, "voice_method", "")
    status_callback = getattr(incoming, "status_callback", "")
    status_callback_method = getattr(incoming, "status_callback_method", "")
    sms_url = getattr(incoming, "sms_url", "")
    sms_method = getattr(incoming, "sms_method", "")

    if (
        voice_url != exp_voice
        or voice_method != "POST"
        or status_callback != exp_status
        or status_callback_method != "POST"
        or sms_url != exp_sms
        or sms_method != "POST"
    ):
        return False, EnrollmentReason.INCOMING_NUMBER_ROUTING_MISMATCH, None

    voice_app_sid = getattr(incoming, "voice_application_sid", None)
    sms_app_sid = getattr(incoming, "sms_application_sid", None)
    trunk_sid = getattr(incoming, "trunk_sid", None)

    if voice_app_sid or sms_app_sid or trunk_sid:
        return False, EnrollmentReason.INCOMING_NUMBER_ROUTING_MISMATCH, None

    voice_fallback_url = getattr(incoming, "voice_fallback_url", None)
    voice_fallback_method = getattr(incoming, "voice_fallback_method", None)
    sms_fallback_url = getattr(incoming, "sms_fallback_url", None)
    sms_fallback_method = getattr(incoming, "sms_fallback_method", None)

    if voice_fallback_url or sms_fallback_url:
        return False, EnrollmentReason.INCOMING_NUMBER_ROUTING_MISMATCH, None

    snapshot = FrozenRoutingSnapshot(
        sid=pn_sid,
        phone_number=canonical_number,
        account_sid=account_sid,
        origin=origin,
        number_type=number_type,
        voice_url=voice_url,
        voice_method=voice_method,
        voice_fallback_url=voice_fallback_url,
        voice_fallback_method=voice_fallback_method,
        status_callback=status_callback,
        status_callback_method=status_callback_method,
        sms_url=sms_url,
        sms_method=sms_method,
        sms_fallback_url=sms_fallback_url,
        sms_fallback_method=sms_fallback_method,
        voice_application_sid=voice_app_sid,
        sms_application_sid=sms_app_sid,
        trunk_sid=trunk_sid,
        capabilities=caps,
    )

    return True, EnrollmentReason.READBACK_VERIFIED, snapshot


def _validate_membership_resource(
    membership: Any,
    pn_sid: str,
    account_sid: str,
    service_sid: str,
    canonical_number: str,
) -> tuple[bool, EnrollmentReason]:
    """Validate PhoneNumber membership resource within Messaging Service."""
    if getattr(membership, "sid", None) != pn_sid:
        return False, EnrollmentReason.MEMBERSHIP_MALFORMED

    if getattr(membership, "account_sid", None) != account_sid:
        return False, EnrollmentReason.MEMBERSHIP_MALFORMED

    if getattr(membership, "service_sid", None) != service_sid:
        return False, EnrollmentReason.MEMBERSHIP_MALFORMED

    if getattr(membership, "phone_number", None) != canonical_number:
        return False, EnrollmentReason.MEMBERSHIP_MALFORMED

    if getattr(membership, "country_code", None) != "US":
        return False, EnrollmentReason.MEMBERSHIP_MALFORMED

    raw_caps = getattr(membership, "capabilities", None)
    if not _validate_membership_capabilities(raw_caps):
        return False, EnrollmentReason.MEMBERSHIP_MALFORMED

    return True, EnrollmentReason.READBACK_VERIFIED


def _log_outcome(result: SmsSenderEnrollmentResult) -> None:
    """Log outcome with static message and allowlisted enum values; never log IDs/PII."""
    if result.status == EnrollmentStatus.DISABLED:
        return
    if result.status in (EnrollmentStatus.ALREADY_MEMBER, EnrollmentStatus.ENROLLED):
        logger.info(
            "SMS sender membership confirmed",
            extra={"status": result.status.value, "reason": result.reason.value},
        )
    else:
        logger.warning(
            "SMS sender enrollment not completed",
            extra={"status": result.status.value, "reason": result.reason.value},
        )


async def ensure_sms_sender_membership(
    contractor_id: str,
    number: str,
    *,
    cancel_event: Optional[threading.Event | asyncio.Event] = None,
) -> SmsSenderEnrollmentResult:
    """Ensure a US sender number is enrolled in the pinned Messaging Service campaign.

    Non-throwing: catches all enrollment exceptions and returns a typed outcome.
    Preserves existing number, preferences, and routing on every outcome.
    """
    # 1. Gate check: default OFF produces zero I/O and zero client construction
    if not settings.sms_sender_enrollment_enabled:
        return SmsSenderEnrollmentResult(
            status=EnrollmentStatus.DISABLED,
            reason=EnrollmentReason.FEATURE_DISABLED,
            enrolled=False,
            mutated=False,
        )

    # 2. Gate check: settings validation before client construction
    env = (settings.environment or "").strip().lower()
    if env != "production":
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.REJECTED,
            reason=EnrollmentReason.NON_PRODUCTION_ENVIRONMENT,
            enrolled=False,
            mutated=False,
        )
        _log_outcome(res)
        return res

    if settings.firestore_project_id != PRODUCTION_GCP_PROJECT_ID:
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.REJECTED,
            reason=EnrollmentReason.INVALID_CONFIGURATION,
            enrolled=False,
            mutated=False,
        )
        _log_outcome(res)
        return res

    account_sid = (settings.twilio_account_sid or "").strip()
    prod_account_sid = (settings.production_twilio_account_sid or "").strip()
    if (
        not account_sid
        or account_sid != prod_account_sid
        or not _ACCOUNT_SID_RE.fullmatch(account_sid)
    ):
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.REJECTED,
            reason=EnrollmentReason.INVALID_CONFIGURATION,
            enrolled=False,
            mutated=False,
        )
        _log_outcome(res)
        return res

    service_sid = (settings.twilio_messaging_service_sid or "").strip()
    if not _MESSAGING_SERVICE_SID_RE.fullmatch(service_sid):
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.REJECTED,
            reason=EnrollmentReason.INVALID_CONFIGURATION,
            enrolled=False,
            mutated=False,
        )
        _log_outcome(res)
        return res

    campaign_sid = (settings.sms_sender_enrollment_campaign_sid or "").strip()
    if not _CAMPAIGN_SID_RE.fullmatch(campaign_sid):
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.REJECTED,
            reason=EnrollmentReason.INVALID_CONFIGURATION,
            enrolled=False,
            mutated=False,
        )
        _log_outcome(res)
        return res

    raw_campaign_sha256 = settings.sms_sender_enrollment_campaign_sha256
    if (
        not isinstance(raw_campaign_sha256, str)
        or not _SHA256_LOWER_RE.fullmatch(raw_campaign_sha256.strip())
    ):
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.REJECTED,
            reason=EnrollmentReason.INVALID_CONFIGURATION,
            enrolled=False,
            mutated=False,
        )
        _log_outcome(res)
        return res

    expected_sha256 = raw_campaign_sha256.strip()

    if not isinstance(contractor_id, str) or not contractor_id.strip():
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.SKIPPED,
            reason=EnrollmentReason.INVALID_INPUT,
            enrolled=False,
            mutated=False,
        )
        _log_outcome(res)
        return res

    if not isinstance(number, str) or not _E164_RE.fullmatch(number.strip()):
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.SKIPPED,
            reason=EnrollmentReason.INVALID_INPUT,
            enrolled=False,
            mutated=False,
        )
        _log_outcome(res)
        return res

    canonical_number = number.strip()

    # 3. Phone region validation: must be US region, not merely +1 prefix
    try:
        parsed = phonenumbers.parse(canonical_number, None)
        if (
            not phonenumbers.is_valid_number(parsed)
            or phonenumbers.region_code_for_number(parsed) != "US"
        ):
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.SKIPPED,
                reason=EnrollmentReason.NON_US_PHONE_REGION,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res
    except Exception:
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.SKIPPED,
            reason=EnrollmentReason.NON_US_PHONE_REGION,
            enrolled=False,
            mutated=False,
        )
        _log_outcome(res)
        return res

    # 4. Monotonic budget and cancellation tracking
    internal_cancel_event = threading.Event()
    start_time = time.monotonic()

    def _is_cancelled() -> bool:
        if internal_cancel_event.is_set():
            return True
        if cancel_event is not None:
            if isinstance(cancel_event, threading.Event) and cancel_event.is_set():
                return True
            if isinstance(cancel_event, asyncio.Event) and cancel_event.is_set():
                return True
        return False

    def _check_sync_guard() -> None:
        if _is_cancelled():
            raise _OperationCancelledError()
        if time.monotonic() - start_time > MAX_BUDGET_SECONDS:
            raise _BudgetExceededError()

    def _check_async_guard(mutated: Optional[bool] = False) -> Optional[SmsSenderEnrollmentResult]:
        if _is_cancelled():
            return SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.OPERATION_CANCELLED,
                enrolled=False,
                mutated=mutated,
            )
        if time.monotonic() - start_time > MAX_BUDGET_SECONDS:
            return SmsSenderEnrollmentResult(
                status=EnrollmentStatus.UNCERTAIN,
                reason=EnrollmentReason.BUDGET_EXCEEDED,
                enrolled=False,
                mutated=mutated,
            )
        return None

    guard = _check_async_guard(mutated=False)
    if guard:
        _log_outcome(guard)
        return guard

    # 5. Client construction and identity binding verification
    try:
        http_client = TwilioHttpClient(
            timeout=2,
            max_retries=0,
            logger=_SILENT_TRANSPORT_LOGGER,
        )
        twilio_client = Client(
            account_sid,
            settings.twilio_auth_token,
            http_client=http_client,
        )
        client_user = getattr(twilio_client, "username", None) or getattr(
            twilio_client, "account_sid", None
        )
        if client_user != account_sid:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.CLIENT_IDENTITY_MISMATCH,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        db = get_firestore_client()
        db_project = getattr(db, "project", None)
        if db_project != PRODUCTION_GCP_PROJECT_ID:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.CLIENT_IDENTITY_MISMATCH,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res
    except Exception:
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.REJECTED,
            reason=EnrollmentReason.INVALID_CONFIGURATION,
            enrolled=False,
            mutated=False,
        )
        _log_outcome(res)
        return res

    loop = asyncio.get_event_loop()
    create_attempted = False
    mutated_state: Optional[bool] = False

    try:
        # 6. Fetch and verify Messaging Service & Campaign
        guard = _check_async_guard(mutated=False)
        if guard:
            _log_outcome(guard)
            return guard

        def _fetch_service():
            _check_sync_guard()
            return twilio_client.messaging.v1.services(service_sid).fetch()

        def _fetch_campaign():
            _check_sync_guard()
            return twilio_client.messaging.v1.services(service_sid).us_app_to_person(
                campaign_sid
            ).fetch()

        service_res = await loop.run_in_executor(None, _fetch_service)

        guard = _check_async_guard(mutated=False)
        if guard:
            _log_outcome(guard)
            return guard

        campaign_res = await loop.run_in_executor(None, _fetch_campaign)

        if (
            getattr(service_res, "sid", None) != service_sid
            or getattr(service_res, "account_sid", None) != account_sid
            or getattr(service_res, "us_app_to_person_registered", None) is not True
            or getattr(service_res, "use_inbound_webhook_on_number", None) is not True
        ):
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.SERVICE_VERIFICATION_FAILED,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        if (
            getattr(campaign_res, "sid", None) != campaign_sid
            or getattr(campaign_res, "account_sid", None) != account_sid
            or getattr(campaign_res, "messaging_service_sid", None) != service_sid
            or getattr(campaign_res, "campaign_status", None) != "VERIFIED"
            or getattr(campaign_res, "us_app_to_person_usecase", None) != "LOW_VOLUME"
        ):
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.CAMPAIGN_VERIFICATION_FAILED,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        computed_digest = _compute_campaign_scope_digest(
            getattr(campaign_res, "description", None),
            getattr(campaign_res, "message_flow", None),
            getattr(campaign_res, "us_app_to_person_usecase", None),
        )
        if not computed_digest or computed_digest != expected_sha256:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.CAMPAIGN_DIGEST_MISMATCH,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        # 7. Query Firestore assignment
        guard = _check_async_guard(mutated=False)
        if guard:
            _log_outcome(guard)
            return guard

        def _query_assignment():
            _check_sync_guard()
            q = (
                db.collection(COLLECTION)
                .where(filter=FieldFilter("twilio_number", "==", canonical_number))
                .select(PROJECTED_FIELDS)
                .limit(2)
            )
            return list(q.stream(timeout=2, retry=None))

        assignment_docs = await loop.run_in_executor(None, _query_assignment)
        if len(assignment_docs) == 0:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.SKIPPED,
                reason=EnrollmentReason.ASSIGNMENT_NOT_FOUND,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res
        if len(assignment_docs) > 1:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.SKIPPED,
                reason=EnrollmentReason.ASSIGNMENT_AMBIGUOUS,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        valid_assignment, reason_assignment = _validate_assignment_record(
            assignment_docs[0], contractor_id, canonical_number
        )
        if not valid_assignment:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.SKIPPED,
                reason=reason_assignment,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        # 8. Query IncomingPhoneNumbers and freeze routing
        guard = _check_async_guard(mutated=False)
        if guard:
            _log_outcome(guard)
            return guard

        def _query_incoming():
            _check_sync_guard()
            return twilio_client.incoming_phone_numbers.list(
                phone_number=canonical_number, limit=2
            )

        incoming_list = await loop.run_in_executor(None, _query_incoming)
        if len(incoming_list) == 0:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.SKIPPED,
                reason=EnrollmentReason.INCOMING_NUMBER_NOT_FOUND,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res
        if len(incoming_list) > 1:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.SKIPPED,
                reason=EnrollmentReason.INCOMING_NUMBER_AMBIGUOUS,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        incoming_res = incoming_list[0]
        valid_incoming, reason_incoming, frozen_routing = _validate_incoming_resource(
            incoming_res, canonical_number, account_sid
        )
        if not valid_incoming or frozen_routing is None:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.SKIPPED,
                reason=reason_incoming,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        pn_sid = getattr(incoming_res, "sid", "")

        # 9. Fetch membership by exact service / PN SID
        guard = _check_async_guard(mutated=False)
        if guard:
            _log_outcome(guard)
            return guard

        def _fetch_membership():
            _check_sync_guard()
            return (
                twilio_client.messaging.v1.services(service_sid)
                .phone_numbers(pn_sid)
                .fetch()
            )

        is_absent = False
        membership_res = None
        try:
            membership_res = await loop.run_in_executor(None, _fetch_membership)
        except TwilioRestException as exc:
            if exc.status == 404 and exc.code == 20404:
                is_absent = True
            else:
                res = SmsSenderEnrollmentResult(
                    status=EnrollmentStatus.UNCERTAIN,
                    reason=EnrollmentReason.CREATE_FAILED_UNCERTAIN,
                    enrolled=False,
                    mutated=False,
                )
                _log_outcome(res)
                return res
        except (_BudgetExceededError, _OperationCancelledError):
            raise
        except Exception:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.UNCERTAIN,
                reason=EnrollmentReason.CREATE_FAILED_UNCERTAIN,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        if not is_absent:
            valid_membership, reason_membership = _validate_membership_resource(
                membership_res,
                pn_sid,
                account_sid,
                service_sid,
                canonical_number,
            )
            if valid_membership:
                res = SmsSenderEnrollmentResult(
                    status=EnrollmentStatus.ALREADY_MEMBER,
                    reason=EnrollmentReason.MEMBERSHIP_PRESENT_VALID,
                    enrolled=True,
                    mutated=False,
                )
                _log_outcome(res)
                return res
            else:
                res = SmsSenderEnrollmentResult(
                    status=EnrollmentStatus.REJECTED,
                    reason=reason_membership,
                    enrolled=False,
                    mutated=False,
                )
                _log_outcome(res)
                return res

        # 10. Pre-write fresh read and invariant recheck
        guard = _check_async_guard(mutated=False)
        if guard:
            _log_outcome(guard)
            return guard

        # Re-read Firestore assignment
        fresh_assignments = await loop.run_in_executor(None, _query_assignment)
        if len(fresh_assignments) != 1:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.PREWRITE_DRIFT_DETECTED,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res
        valid_re_assign, _ = _validate_assignment_record(
            fresh_assignments[0], contractor_id, canonical_number
        )
        if not valid_re_assign:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.PREWRITE_DRIFT_DETECTED,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        # Re-read IncomingPhoneNumbers and compare against frozen snapshot
        fresh_incoming_list = await loop.run_in_executor(None, _query_incoming)
        if len(fresh_incoming_list) != 1:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.PREWRITE_DRIFT_DETECTED,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res
        fresh_incoming = fresh_incoming_list[0]
        if not frozen_routing.matches(fresh_incoming):
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.PREWRITE_DRIFT_DETECTED,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        # Re-read Service & Campaign
        fresh_svc = await loop.run_in_executor(None, _fetch_service)
        fresh_cmp = await loop.run_in_executor(None, _fetch_campaign)
        if (
            getattr(fresh_svc, "sid", None) != service_sid
            or getattr(fresh_svc, "account_sid", None) != account_sid
            or getattr(fresh_svc, "us_app_to_person_registered", None) is not True
            or getattr(fresh_svc, "use_inbound_webhook_on_number", None) is not True
            or getattr(fresh_cmp, "sid", None) != campaign_sid
            or getattr(fresh_cmp, "account_sid", None) != account_sid
            or getattr(fresh_cmp, "messaging_service_sid", None) != service_sid
            or getattr(fresh_cmp, "campaign_status", None) != "VERIFIED"
            or getattr(fresh_cmp, "us_app_to_person_usecase", None) != "LOW_VOLUME"
        ):
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.PREWRITE_DRIFT_DETECTED,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res
        re_digest = _compute_campaign_scope_digest(
            getattr(fresh_cmp, "description", None),
            getattr(fresh_cmp, "message_flow", None),
            getattr(fresh_cmp, "us_app_to_person_usecase", None),
        )
        if not re_digest or re_digest != expected_sha256:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.REJECTED,
                reason=EnrollmentReason.PREWRITE_DRIFT_DETECTED,
                enrolled=False,
                mutated=False,
            )
            _log_outcome(res)
            return res

        # 11. Single create call (never retry create within invocation)
        guard = _check_async_guard(mutated=False)
        if guard:
            _log_outcome(guard)
            return guard

        def _create_membership():
            _check_sync_guard()
            return (
                twilio_client.messaging.v1.services(service_sid)
                .phone_numbers.create(phone_number_sid=pn_sid)
            )

        create_attempted = True
        create_succeeded_cleanly = False
        try:
            await loop.run_in_executor(None, _create_membership)
            create_succeeded_cleanly = True
            mutated_state = True
        except TwilioRestException as exc:
            if exc.code == 21712:
                # Explicit other-service conflict proves no mutation
                mutated_state = False
                res = SmsSenderEnrollmentResult(
                    status=EnrollmentStatus.REJECTED,
                    reason=EnrollmentReason.OTHER_SERVICE_CONFLICT,
                    enrolled=False,
                    mutated=False,
                )
                _log_outcome(res)
                return res
            # Duplicate code (e.g. 21618) or provider exception: mutation state is unknown
            mutated_state = None
        except (_BudgetExceededError, _OperationCancelledError):
            raise
        except Exception:
            mutated_state = None

        # 12. Post-create independent readback and re-verification
        guard = _check_async_guard(mutated=mutated_state)
        if guard:
            _log_outcome(guard)
            return guard

        readback_res = None
        readback_is_404 = False
        try:
            readback_res = await loop.run_in_executor(None, _fetch_membership)
        except TwilioRestException as exc:
            if exc.status == 404 and exc.code == 20404:
                readback_is_404 = True
        except (_BudgetExceededError, _OperationCancelledError):
            raise
        except Exception:
            readback_res = None

        if readback_res is None:
            fail_reason = (
                EnrollmentReason.READBACK_FAILED_ABSENT
                if readback_is_404
                else EnrollmentReason.READBACK_FAILED_UNCERTAIN
            )
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.UNCERTAIN,
                reason=fail_reason,
                enrolled=False,
                mutated=mutated_state,
            )
            _log_outcome(res)
            return res

        valid_readback, reason_readback = _validate_membership_resource(
            readback_res,
            pn_sid,
            account_sid,
            service_sid,
            canonical_number,
        )
        if not valid_readback:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.UNCERTAIN,
                reason=reason_readback,
                enrolled=False,
                mutated=mutated_state,
            )
            _log_outcome(res)
            return res

        # Re-verify assignment and incoming routing post-create
        post_assignments = await loop.run_in_executor(None, _query_assignment)
        if len(post_assignments) != 1 or not _validate_assignment_record(
            post_assignments[0], contractor_id, canonical_number
        )[0]:
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.UNCERTAIN,
                reason=EnrollmentReason.POSTWRITE_DRIFT_DETECTED,
                enrolled=False,
                mutated=mutated_state,
            )
            _log_outcome(res)
            return res

        post_incoming_list = await loop.run_in_executor(None, _query_incoming)
        if len(post_incoming_list) != 1 or not frozen_routing.matches(post_incoming_list[0]):
            res = SmsSenderEnrollmentResult(
                status=EnrollmentStatus.UNCERTAIN,
                reason=EnrollmentReason.POSTWRITE_DRIFT_DETECTED,
                enrolled=False,
                mutated=mutated_state,
            )
            _log_outcome(res)
            return res

        final_mutated = True if create_succeeded_cleanly else None
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.ENROLLED,
            reason=EnrollmentReason.READBACK_VERIFIED,
            enrolled=True,
            mutated=final_mutated,
        )
        _log_outcome(res)
        return res

    except _BudgetExceededError:
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.UNCERTAIN,
            reason=EnrollmentReason.BUDGET_EXCEEDED,
            enrolled=False,
            mutated=mutated_state if create_attempted else False,
        )
        _log_outcome(res)
        return res
    except _OperationCancelledError:
        internal_cancel_event.set()
        if cancel_event is not None and hasattr(cancel_event, "set"):
            cancel_event.set()
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.REJECTED,
            reason=EnrollmentReason.OPERATION_CANCELLED,
            enrolled=False,
            mutated=mutated_state if create_attempted else False,
        )
        _log_outcome(res)
        return res
    except asyncio.CancelledError:
        internal_cancel_event.set()
        if cancel_event is not None and hasattr(cancel_event, "set"):
            cancel_event.set()
        raise
    except Exception:
        res = SmsSenderEnrollmentResult(
            status=EnrollmentStatus.UNCERTAIN,
            reason=EnrollmentReason.UNEXPECTED_ERROR,
            enrolled=False,
            mutated=mutated_state if create_attempted else False,
        )
        _log_outcome(res)
        return res
