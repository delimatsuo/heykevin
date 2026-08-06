"""Phone number normalization and validation."""

from __future__ import annotations

import hashlib
from typing import Optional

import phonenumbers


def normalize_phone(number: str, default_region: str = "US") -> Optional[str]:
    """Normalize a phone number to E.164 format. Returns None if invalid."""
    try:
        parsed = phonenumbers.parse(number, default_region)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return None


def phone_hash(e164_number: str) -> str:
    """SHA-256 hash of an E.164 phone number for use as Firestore doc ID."""
    return hashlib.sha256(e164_number.encode()).hexdigest()


def forwarding_confirms_owner(
    forwarded_from: Optional[str], owner_phone: Optional[str]
) -> bool:
    """True if Twilio's ForwardedFrom proves this call was diverted by the owner.

    iOS exposes no call-forwarding state, so this is the only positive proof
    available that a user's forward is actually live. Carriers send the field in
    inconsistent formats, so both sides are normalized to E.164 before comparing.

    Returns False on anything unparseable rather than falling back to string
    equality — matching raw strings would let values like "restricted" or
    "anonymous" confirm against each other.

    Note the asymmetry: True is conclusive, False is not. Twilio documents
    ForwardedFrom as carrier-dependent, so many working forwards never set it.
    """
    if not forwarded_from or not owner_phone:
        return False
    normalized_source = normalize_phone(str(forwarded_from))
    normalized_owner = normalize_phone(str(owner_phone))
    if not normalized_source or not normalized_owner:
        return False
    return normalized_source == normalized_owner
