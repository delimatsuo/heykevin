"""Phone number normalization and validation."""

from __future__ import annotations

import hashlib
import time
import unicodedata
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


# Each number without a "+" is parsed once per country, about 245 parses and a
# few milliseconds of CPU, so the country scan stops at this many numbers or
# this much time per request, whichever comes first. Shapes cover every number.
SHAPE_SCAN_LIMIT = 300
SHAPE_SCAN_SECONDS = 0.25
SHAPE_TOP_COUNTRIES = 8
# The longest number valid anywhere: 15 digits (E.164) plus a 2-digit dialing
# prefix such as "00". A longer one is valid nowhere, so it is not scanned.
SHAPE_MAX_DIGITS = 17
_PLUS_SIGNS = ("+", "\N{FULLWIDTH PLUS SIGN}")  # libphonenumber reads both


def _clean(raw: str) -> str:
    """The number without invisible format marks, such as the bidi marks iOS
    can put around a number, which hide how it starts."""
    return "".join(ch for ch in (raw or "") if unicodedata.category(ch) != "Cf").strip()


def _lead(number: str) -> str:
    """How a number starts, the part of its shape that says how it was written."""
    if number.startswith(_PLUS_SIGNS):
        return "plus"
    if number[:1] in ("*", "#"):
        return "service"
    digits = "".join(ch for ch in number if ch.isdigit())
    if digits.startswith("00"):
        return "double-zero"
    if digits.startswith("0"):
        return "zero"
    return "national"


def _top(counts: Counter) -> dict:
    """The largest counts, ties in alphabetical order so the log is stable."""
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return dict(ranked[:SHAPE_TOP_COUNTRIES])


def describe_unreadable_numbers(numbers: list[str], default_region: str = "US") -> dict:
    """Counts that say why ``normalize_phone`` rejected these numbers, for logs.

    Nothing that could identify a number leaves this function. Each number is
    reduced to how it starts and its digit count ("national/11" is eleven
    digits with no "+" or leading zero), and to the countries where it would
    read as a valid number. For a "+" number that is the country its code
    names. Any other number is parsed as a number of every country but
    ``default_region``, where it was already invalid, and counted under each
    country it then belongs to. One dialed with an international prefix, such
    as "00 55 ...", can land in ``default_region`` itself. A short number is
    valid in many countries, so those counts overlap; "valid_nowhere" counts
    the scanned numbers valid in none.
    """
    shapes: Counter = Counter()
    plus_countries: Counter = Counter()
    valid_in: Counter = Counter()
    nowhere = 0
    scanned = 0
    deadline = time.monotonic() + SHAPE_SCAN_SECONDS
    for raw in numbers:
        number = _clean(raw)
        lead = _lead(number)
        digit_count = sum(ch.isdigit() for ch in number)
        shapes[f"{lead}/{digit_count}"] += 1
        if lead == "plus":
            try:
                code = phonenumbers.parse(number, None).country_code
                plus_countries[phonenumbers.region_code_for_country_code(code)] += 1
            except phonenumbers.NumberParseException:
                plus_countries["unparseable"] += 1
            continue
        if scanned >= SHAPE_SCAN_LIMIT or time.monotonic() >= deadline:
            continue
        scanned += 1
        countries = set()
        if digit_count <= SHAPE_MAX_DIGITS:
            for region in sorted(phonenumbers.SUPPORTED_REGIONS):
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
        "scanned": scanned,
        "shapes": dict(shapes.most_common()),
        "plus_countries": _top(plus_countries),
        "valid_in": _top(valid_in),
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
