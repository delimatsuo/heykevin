"""Pure, fail-closed identity and express-consent policy for Canadian owner SMS."""
from datetime import datetime, timezone

import re

import phonenumbers

from app.config import PRODUCTION_GCP_PROJECT_ID, PRODUCTION_CLOUD_RUN_URL, settings

from app.db.owner_sms import resolve_contractor_owner_sms_identities

DISCLOSURE_VERSION = "ca-owner-sms-v1"
LIFECYCLE_HOLDS = (
    "deletion_requested_at", "deactivated_at", "deleted_app_detected_at", "number_released_at",
)


def is_canadian_phone(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("+"):
        return False
    try:
        parsed = phonenumbers.parse(value, None)
        return (
            phonenumbers.is_valid_number(parsed)
            and phonenumbers.region_code_for_number(parsed) == "CA"
            and phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164) == value
        )
    except phonenumbers.NumberParseException:
        return False


def canadian_assignment_valid(data: dict, *, require_enabled: bool = True) -> bool:
    owner, sender = resolve_contractor_owner_sms_identities(data)
    caps = data.get("number_capabilities")
    revision = data.get("owner_sms_opt_out_revision")
    return (
        data.get("country_code") == "CA" and data.get("provisioned_country_code") == "CA"
        and data.get("active") is True and data.get("number_provider") == "twilio"
        and data.get("number_type") == "local"
        and isinstance(caps, dict) and caps.get("voice") is True and caps.get("sms") is True
        and is_canadian_phone(owner) and is_canadian_phone(sender)
        and (not require_enabled or data.get("owner_sms_enabled") is True)
        and data.get("owner_sms_opted_out") is False
        and isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0
        and all(data.get(key) is None for key in LIFECYCLE_HOLDS)
    )


def valid_canada_consent(data: dict) -> bool:
    consent = data.get("owner_sms_consent")
    if not isinstance(consent, dict):
        return False
    owner, sender = resolve_contractor_owner_sms_identities(data)
    collected = consent.get("collected_at")
    revision = data.get("owner_sms_opt_out_revision")
    return (
        data.get("country_code") == "CA" and data.get("provisioned_country_code") == "CA"
        and is_canadian_phone(owner) and is_canadian_phone(sender)
        and isinstance(data.get("contractor_id"), str) and bool(data["contractor_id"])
        and consent.get("contractor_id") == data["contractor_id"]
        and consent.get("owner_phone_e164") == owner and consent.get("twilio_number") == sender
        and consent.get("disclosure_version") == DISCLOSURE_VERSION
        and consent.get("source") == "ios_settings"
        and isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0
        and type(consent.get("opt_out_revision")) is int
        and consent["opt_out_revision"] == revision
        and isinstance(collected, datetime) and collected.tzinfo is not None
        and collected.utcoffset() == timezone.utc.utcoffset(collected)
        and collected.timestamp() > 0 and collected <= datetime.now(timezone.utc)
    )


def canada_configuration_valid() -> bool:
    return (
        settings.canada_sms_enabled is True and settings.environment == "production"
        and settings.firestore_project_id == PRODUCTION_GCP_PROJECT_ID
        and settings.cloud_run_url.rstrip("/") == PRODUCTION_CLOUD_RUN_URL
        and re.fullmatch(r"AC[0-9a-fA-F]{32}", settings.twilio_account_sid or "") is not None
        and settings.twilio_account_sid == settings.production_twilio_account_sid
        and re.fullmatch(r"MG[0-9a-fA-F]{32}", settings.canada_messaging_service_sid or "") is not None
        and settings.canada_messaging_service_sid != settings.twilio_messaging_service_sid
    )


def valid_canada_delivery_binding(
    data: dict, *, account_sid: str, messaging_service_sid: str, configuration_enabled: bool,
) -> bool:
    """Pure readiness check; app preference and lifecycle remain separate send guards."""
    if configuration_enabled is not True or not valid_canada_consent(data):
        return False
    consent = data["owner_sms_consent"]
    verified = consent.get("verified_at")
    return (
        consent.get("delivery_state") == "verified"
        and consent.get("account_sid") == account_sid
        and consent.get("messaging_service_sid") == messaging_service_sid
        and isinstance(verified, datetime) and verified.tzinfo is not None
        and verified.utcoffset() == timezone.utc.utcoffset(verified)
        and consent["collected_at"] <= verified <= datetime.now(timezone.utc)
    )


def canada_delivery_ready(data: dict) -> bool:
    return valid_canada_delivery_binding(
        data, account_sid=settings.twilio_account_sid,
        messaging_service_sid=settings.canada_messaging_service_sid,
        configuration_enabled=canada_configuration_valid(),
    )


def canada_consent_required(data: dict) -> bool:
    """Only explicit Canadian assignments use the Canadian review/retry flow."""
    if data.get("country_code") != "CA" or data.get("provisioned_country_code") != "CA":
        return False
    return not canada_delivery_ready(data)
