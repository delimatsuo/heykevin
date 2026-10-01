"""Country policy, market availability, and service binding resolution.

Single source of truth for recognized markets, new account admission, and
telephony service binding.
"""

from __future__ import annotations

from typing import Any, Optional
import phonenumbers


# Recognized countries for Hey Kevin
RECOGNIZED_COUNTRIES = frozenset({"US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"})

# Compatibility alias
SUPPORTED_COUNTRIES = RECOGNIZED_COUNTRIES

# Markets available for new account admission and new number provisioning
AVAILABLE_COUNTRIES = frozenset({"US", "CA"})

# Markets requiring carrier/provider qualification before launch
QUALIFICATION_REQUIRED_COUNTRIES = frozenset({"BR", "GB"})

# Unsupported markets
UNSUPPORTED_COUNTRIES = frozenset({"DE", "FR", "IT", "ES", "PT"})

# Priority qualification order for markets
QUALIFICATION_ORDER = ["BR", "CA", "GB"]

# Stable pinned market order for public availability discovery
PINNED_MARKET_ORDER = ["US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"]

# Countries that require Twilio regulatory bundles for number provisioning (dormant helper data)
REGULATORY_COUNTRIES = {"DE", "FR", "IT", "ES", "PT", "BR"}

# Country code to English name mapping
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


class PhoneAdmissionError(Exception):
    """Base exception for phone admission validation errors."""
    pass


class InvalidPhoneError(PhoneAdmissionError):
    """Raised when an owner phone number cannot be parsed or is invalid E.164."""
    pass


class CountryPhoneMismatchError(PhoneAdmissionError):
    """Raised when phone number region conflicts with explicit country code."""

    def __init__(self, country_code: str, phone_region: str):
        self.country_code = country_code
        self.phone_region = phone_region
        super().__init__(
            f"Phone number region '{phone_region}' does not match country '{country_code}'"
        )


def is_recognized_country(country_code: Optional[str]) -> bool:
    """Return True if country_code is in recognized markets."""
    if not country_code or not isinstance(country_code, str):
        return False
    return country_code.strip().upper() in RECOGNIZED_COUNTRIES


def is_country_available(country_code: Optional[str]) -> bool:
    """Return True if country_code is currently open for new account/number admission."""
    if not country_code or not isinstance(country_code, str):
        return False
    return country_code.strip().upper() in AVAILABLE_COUNTRIES


def get_country_status(country_code: Optional[str]) -> str:
    """Return market availability status string: available, qualification_required, or unsupported."""
    if not country_code or not isinstance(country_code, str):
        return "unsupported"
    code = country_code.strip().upper()
    if code in AVAILABLE_COUNTRIES:
        return "available"
    if code in QUALIFICATION_REQUIRED_COUNTRIES:
        return "qualification_required"
    return "unsupported"


def get_country_name(country_code: Optional[str]) -> str:
    """Return full English country name for recognized country code, or code if unmapped."""
    if not country_code or not isinstance(country_code, str):
        return ""
    code = country_code.strip().upper()
    return COUNTRY_NAMES.get(code, code)


def get_markets_payload() -> dict[str, Any]:
    """Return public market availability payload for GET /api/markets."""
    return {
        "markets": [
            {"country_code": cc, "status": get_country_status(cc)}
            for cc in PINNED_MARKET_ORDER
        ],
        "qualification_order": list(QUALIFICATION_ORDER),
    }


def country_not_available_detail(country_code: str) -> dict[str, str]:
    """Construct standard HTTP 409 detail dictionary for unavailable market admission."""
    code = (country_code or "").strip().upper()
    name = get_country_name(code)
    message = f"Kevin is not yet available in {name}." if name else f"Kevin is not yet available in {code}."
    return {
        "code": "country_not_available",
        "country_code": code,
        "message": message,
    }


def _validate_raw_phone_chars(raw: str) -> None:
    """Validate phone string characters before phonenumbers parsing.

    Only allows ASCII digits, spaces, parentheses, dash, dot, and at most one
    leading plus at index 0. Rejects letters, vanity numbers, extensions, unicode
    digits, and multiple or non-leading pluses.
    """
    if not raw:
        return
    has_leading_plus = raw.startswith("+")
    body = raw[1:] if has_leading_plus else raw

    ALLOWED_BODY_CHARS = set("0123456789 ()-.")
    for char in body:
        if char not in ALLOWED_BODY_CHARS:
            raise InvalidPhoneError(f"Invalid character in phone number: {char!r}")

    if not any(c in "0123456789" for c in body):
        raise InvalidPhoneError("Phone number must contain at least one ASCII digit")


def validate_phone_and_region(
    owner_phone: str,
    default_country: str = "",
) -> tuple[str, str]:
    """Parse, validate and derive region from an owner phone number.

    Returns:
        tuple of (canonical_e164_phone, derived_region_code)

    Raises:
        InvalidPhoneError: If phone is not a valid phone number or fails format constraints.
        CountryPhoneMismatchError: If explicit country conflicts with derived region.
    """
    raw = (owner_phone or "").strip()
    if not raw:
        return "", ""

    # Validate characters before any phonenumbers parse
    _validate_raw_phone_chars(raw)

    exp_cc = (default_country or "").strip().upper()

    # Brazilian national format validation
    if exp_cc == "BR" and not raw.startswith("+"):
        raw_digits = "".join(c for c in raw if c.isdigit())
        # Require 11 digits: 2-digit DDD + 9-digit mobile number starting with 9
        if len(raw_digits) != 11:
            raise InvalidPhoneError(
                "Brazilian national phone number must have exactly 11 digits (2-digit DDD + 9-digit mobile)"
            )
        if raw_digits[2] != "9":
            raise InvalidPhoneError("Brazilian mobile number must start with 9 after DDD")

    parsed = None
    if raw.startswith("+"):
        try:
            parsed = phonenumbers.parse(raw, None)
        except phonenumbers.NumberParseException:
            raise InvalidPhoneError("Invalid owner phone number")
    elif exp_cc and exp_cc in RECOGNIZED_COUNTRIES:
        try:
            parsed = phonenumbers.parse(raw, exp_cc)
        except phonenumbers.NumberParseException:
            raise InvalidPhoneError("Invalid owner phone number")
    else:
        try:
            parsed = phonenumbers.parse(raw, "US")
        except phonenumbers.NumberParseException:
            raise InvalidPhoneError("Invalid owner phone number")

    if not parsed or not phonenumbers.is_valid_number(parsed):
        raise InvalidPhoneError("Invalid owner phone number")

    # Brazilian E.164 or parsed BR constraints: require 11 national digits with mobile 9, reject fixed-line
    if parsed.country_code == 55:
        national_str = str(parsed.national_number)
        if len(national_str) != 11 or national_str[2] != "9":
            raise InvalidPhoneError(
                "Brazilian phone number must be an 11-digit mobile starting with 9 after DDD"
            )
        num_type = phonenumbers.number_type(parsed)
        if num_type == phonenumbers.PhoneNumberType.FIXED_LINE:
            raise InvalidPhoneError("Fixed-line Brazilian numbers cannot be used as personal mobile")

    canonical = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    region = phonenumbers.region_code_for_number(parsed)

    # Unknown valid E.164 (non-geographic e.g. 001 or unassigned region) must not become US
    if not region or region == "001" or not region.isalpha():
        derived_region = "UNKNOWN"
    else:
        derived_region = region.upper()

    if exp_cc and derived_region and derived_region != exp_cc:
        raise CountryPhoneMismatchError(country_code=exp_cc, phone_region=derived_region)

    return canonical, derived_region


def resolve_service_binding(contractor: Optional[dict]) -> Optional[dict[str, Any]]:
    """Derive read-only service_binding dictionary for a contractor profile.

    Returns None if no number is assigned.
    Returns dictionary with country_code, provider, number_type, capabilities.
    """
    if not contractor or not isinstance(contractor, dict):
        return None

    twilio_number = contractor.get("twilio_number")
    if not twilio_number or not isinstance(twilio_number, str) or not twilio_number.strip():
        return None

    raw_number = twilio_number.strip()

    # Prefer stored protected provisioned_country_code if present and valid
    prov_cc = contractor.get("provisioned_country_code")
    country_code: Optional[str] = None
    if isinstance(prov_cc, str) and prov_cc.strip().upper() in RECOGNIZED_COUNTRIES:
        country_code = prov_cc.strip().upper()
    else:
        # For legacy records without provisioned_country_code, derive from valid assigned E.164
        try:
            parsed = phonenumbers.parse(raw_number, None)
            if phonenumbers.is_valid_number(parsed):
                region = phonenumbers.region_code_for_number(parsed)
                if region and region != "001" and region.isalpha():
                    country_code = region.upper()
        except Exception:
            pass

    raw_provider = contractor.get("number_provider")
    raw_number_type = contractor.get("number_type")
    raw_capabilities = contractor.get("number_capabilities")

    return {
        "country_code": country_code,
        "provider": str(raw_provider).strip() if isinstance(raw_provider, str) and raw_provider.strip() else None,
        "number_type": str(raw_number_type).strip() if isinstance(raw_number_type, str) and raw_number_type.strip() else None,
        "capabilities": dict(raw_capabilities) if isinstance(raw_capabilities, dict) else None,
    }
