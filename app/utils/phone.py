"""Phone number normalization and validation."""

from __future__ import annotations

import hashlib
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
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


# Brazil put a 9 in front of every mobile number between 2012 and 2016. A mobile
# saved before then has 8 digits after its 2-digit area code, starting with 6-9,
# and libphonenumber now rejects it.
_BRAZIL = 55
_OLD_BRAZIL_MOBILE_STARTS = "6789"
# Another country becomes an address book's second country once at least this
# many of its numbers are written with a country code.
SECOND_COUNTRY_MIN = 3


def _brazil_ninth_digit(parsed: phonenumbers.PhoneNumber) -> Optional[phonenumbers.PhoneNumber]:
    """An old 8-digit Brazilian mobile with the 9 it was given; None for any other number."""
    national = str(parsed.national_number)
    if (
        parsed.country_code != _BRAZIL
        or len(national) != 10
        or national[2] not in _OLD_BRAZIL_MOBILE_STARTS
    ):
        return None
    upgraded = phonenumbers.PhoneNumber(
        country_code=_BRAZIL, national_number=int(national[:2] + "9" + national[2:]),
    )
    if (
        phonenumbers.is_valid_number(upgraded)
        and phonenumbers.number_type(upgraded) == phonenumbers.PhoneNumberType.MOBILE
    ):
        return upgraded
    return None


def _read(number: str, region: str) -> tuple[Optional[str], bool]:
    """The number in E.164, read as a ``region`` number unless it carries a country
    code, and whether it is an old Brazilian mobile that took its 9."""
    try:
        parsed = phonenumbers.parse(number, region)
    except phonenumbers.NumberParseException:
        return None, False
    if phonenumbers.is_valid_number(parsed):
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), False
    upgraded = _brazil_ninth_digit(parsed)
    if upgraded is None:
        return None, False
    return phonenumbers.format_number(upgraded, phonenumbers.PhoneNumberFormat.E164), True


@dataclass
class AddressBook:
    """What ``read_address_book`` read, entry by entry, and counts for the log."""

    readings: list[list[str]]  # per entry: no number, one, or two in E.164
    second_country: Optional[str] = None
    ninth_digit: int = 0  # entries read as old Brazilian mobiles, given their 9
    as_second_country: int = 0  # entries unreadable at home, read as second-country numbers
    both_ways: int = 0  # entries read both as home and as second-country numbers


def read_address_book(numbers: list[str], home_region: str = "US") -> AddressBook:
    """Read an owner's address-book numbers for the whitelist.

    A number written with "+" is read as its country's number; any other as a
    ``home_region`` number. Old 8-digit Brazilian mobiles are read with the 9
    every Brazilian mobile was given.

    An address book can be mostly another country's. Its second country is the
    one, other than ``home_region``, whose code the most numbers are written
    with: at least ``SECOND_COUNTRY_MIN``, ties to the alphabetically first. A
    number without "+" that is not valid at home is read as a second-country
    number, as it was most likely saved there, as "0 21 11 8765-4321" is.

    When more numbers are written with the second country's code than are read
    as home numbers at all, as for an owner in the US whose contacts are mostly
    in Brazil, a number without "+" that is valid in both countries is read both
    ways: "(21) 2345-6789" is a New York number and a Rio number, and only one is
    the contact. Whitelisting both lets the contact ring through; the stranger
    with the other number rings through too, which costs far less than
    screening a contact.
    """
    cleaned = [_clean(number) for number in numbers]
    explicit = [_lead(number) == "plus" for number in cleaned]
    home = [_read(number, home_region) for number in cleaned]

    home_count = 0
    coded: Counter = Counter()
    for (reading, _), is_explicit in zip(home, explicit):
        if reading is None:
            continue
        country = country_of(reading)
        if country == home_region:
            home_count += 1
        elif is_explicit and country in phonenumbers.SUPPORTED_REGIONS:
            coded[country] += 1
    ranked = sorted(coded.items(), key=lambda item: (-item[1], item[0]))
    second = ranked[0][0] if ranked and ranked[0][1] >= SECOND_COUNTRY_MIN else None
    second_dominates = second is not None and ranked[0][1] > home_count

    book = AddressBook(readings=[], second_country=second)
    for number, is_explicit, (reading, took_nine) in zip(cleaned, explicit, home):
        entry = [reading] if reading else []
        wants_second = second is not None and not is_explicit and (
            reading is None or (second_dominates and country_of(reading) == home_region)
        )
        if wants_second:
            alternative, alternative_took_nine = _read(number, second)
            if alternative and alternative not in entry:
                entry.append(alternative)
                took_nine = took_nine or alternative_took_nine
                if reading is None:
                    book.as_second_country += 1
                else:
                    book.both_ways += 1
        book.ninth_digit += took_nine
        book.readings.append(entry)
    return book


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
