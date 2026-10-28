"""Phone number normalization and validation."""

from __future__ import annotations

import hashlib
from collections import Counter
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


# A national number is tried in every country, about 245 parses each, so a
# huge upload of unreadable numbers is described from its first numbers only.
SHAPE_SCAN_LIMIT = 300
SHAPE_TOP_COUNTRIES = 5


def _lead(number: str) -> str:
    """How a number starts, the part of its shape that says how it was written."""
    if number.startswith("+"):
        return "plus"
    if number[:1] in ("*", "#"):
        return "service"
    digits = "".join(ch for ch in number if ch.isdigit())
    if digits.startswith("00"):
        return "double-zero"
    if digits.startswith("0"):
        return "zero"
    return "national"


def describe_unreadable_numbers(numbers: list[str], default_region: str = "US") -> dict:
    """Counts that say why ``normalize_phone`` rejected these numbers, for logs.

    Nothing that could identify a number leaves this function. Each number is
    reduced to how it starts and its digit count ("national/11" is eleven
    digits with no "+" or leading zero), and to the countries where it would
    read as a valid number: for a "+" number the country its code names; for
    any other number every country other than ``default_region`` where it is
    valid. A short number is valid in many countries, so those counts overlap;
    "valid_nowhere" counts the numbers valid in none.
    """
    shapes: Counter = Counter()
    plus_countries: Counter = Counter()
    valid_in: Counter = Counter()
    nowhere = 0
    scanned = numbers[:SHAPE_SCAN_LIMIT]
    for raw in scanned:
        number = (raw or "").strip()
        lead = _lead(number)
        shapes[f"{lead}/{sum(ch.isdigit() for ch in number)}"] += 1
        if lead == "plus":
            try:
                code = phonenumbers.parse(number, None).country_code
                plus_countries[phonenumbers.region_code_for_country_code(code)] += 1
            except phonenumbers.NumberParseException:
                plus_countries["unparseable"] += 1
            continue
        countries = set()
        for region in phonenumbers.SUPPORTED_REGIONS:
            if region == default_region:
                continue
            try:
                parsed = phonenumbers.parse(number, region)
            except phonenumbers.NumberParseException:
                continue
            if phonenumbers.is_valid_number(parsed):
                countries.add(phonenumbers.region_code_for_number(parsed))
        valid_in.update(countries)
        if not countries:
            nowhere += 1
    return {
        "scanned": len(scanned),
        "shapes": dict(shapes.most_common()),
        "plus_countries": dict(plus_countries.most_common(SHAPE_TOP_COUNTRIES)),
        "valid_in": dict(valid_in.most_common(SHAPE_TOP_COUNTRIES)),
        "valid_nowhere": nowhere,
    }


def country_of(e164_number: str) -> Optional[str]:
    """The country of a number ``normalize_phone`` returned, such as "BR"."""
    try:
        return phonenumbers.region_code_for_number(phonenumbers.parse(e164_number, None))
    except phonenumbers.NumberParseException:
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
