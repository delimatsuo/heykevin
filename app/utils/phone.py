"""Phone number normalization and validation."""

from __future__ import annotations

import hashlib
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
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
# saved before then has 8 digits after its 2-digit area code. One starting with 6,
# 8 or 9 is no longer valid. One starting with 7 still is, as one of the 8-digit
# radio numbers Nextel sold; but most were mobiles that took the 9, and the radio
# service is gone, so such a number is also read with its 9.
_BRAZIL = 55
_OLD_BRAZIL_MOBILE_STARTS = "6789"
_OLD_BRAZIL_RADIO_START = "7"
# Another country becomes an address book's second country once at least this
# many of its numbers are written with a country code.
SECOND_COUNTRY_MIN = 3


def _brazil_ninth_digit(parsed: phonenumbers.PhoneNumber) -> Optional[phonenumbers.PhoneNumber]:
    """A Brazilian area code and 8-digit mobile with the 9 every mobile was given;
    None for any other number, or when that is not valid."""
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
    return upgraded if phonenumbers.is_valid_number(upgraded) else None


def _e164(parsed: phonenumbers.PhoneNumber) -> str:
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _read(number: str, region: str) -> tuple[list[str], bool]:
    """The number in E.164, read as a ``region`` number unless it carries a country
    code, and whether a 9 was added: an old Brazilian mobile is read with its 9,
    and an old radio number both as it is and with its 9."""
    try:
        parsed = phonenumbers.parse(number, region)
    except phonenumbers.NumberParseException:
        return [], False
    upgraded = _brazil_ninth_digit(parsed)
    if phonenumbers.is_valid_number(parsed):
        if upgraded is not None and str(parsed.national_number)[2] == _OLD_BRAZIL_RADIO_START:
            return [_e164(parsed), _e164(upgraded)], True
        return [_e164(parsed)], False
    if upgraded is None:
        return [], False
    return [_e164(upgraded)], True


def _prior_counts(prior) -> tuple[Counter, int]:
    """Counts an earlier request of the same sync left, as stored; empty if malformed."""
    if not isinstance(prior, dict):
        return Counter(), 0
    coded = prior.get("coded")
    coded = Counter({
        country: count for country, count in (coded.items() if isinstance(coded, dict) else ())
        if isinstance(country, str) and isinstance(count, int) and not isinstance(count, bool) and count > 0
    })
    home = prior.get("home")
    if not isinstance(home, int) or isinstance(home, bool) or home < 0:
        home = 0
    return coded, home


@dataclass
class AddressBook:
    """What ``read_address_book`` read, entry by entry, and counts for the log."""

    readings: list[list[str]]  # per entry: no number, or one or more in E.164
    second_country: Optional[str] = None
    ninth_digit: int = 0  # entries with a reading that took a Brazilian mobile's 9
    as_second_country: int = 0  # entries unreadable at home, read as second-country numbers
    both_ways: int = 0  # entries read both as home and as second-country numbers
    # Running counts the next request of the same sync passes back as ``prior``.
    counts: dict = field(default_factory=dict)


def read_address_book(numbers: list[str], home_region: str = "US", prior=None) -> AddressBook:
    """Read an owner's address-book numbers for the whitelist.

    A number written with "+" is read as its country's number; any other as a
    ``home_region`` number. Old 8-digit Brazilian mobiles are read with the 9
    every Brazilian mobile was given. A home number is one with the home calling
    code: for an owner in the US, Canadian numbers too.

    An address book can be mostly another country's. Its second country is the
    one, without the home calling code, whose code the most numbers are written
    with: at least ``SECOND_COUNTRY_MIN``, ties to the alphabetically first. A
    number without "+" that is not valid at home is read as a second-country
    number, as it was most likely saved there, as "0 21 11 8765-4321" is. So is
    one that is valid at home only with a 9 added, which makes it a guess.

    When more numbers are written with the second country's code than are read
    as home numbers at all, as for an owner in the US whose contacts are mostly
    in Brazil, a number without "+" that is valid in both countries is read both
    ways: "(21) 2345-6789" is a New York number and a Rio number, and only one is
    the contact. Whitelisting both lets the contact ring through; the stranger
    with the other number rings through too, which costs far less than
    screening a contact.

    The app can send one sync as several requests. ``prior`` is the ``counts``
    the earlier requests returned, so later requests decide from everything the
    sync has sent so far, not from a short last request alone.
    """
    cleaned = [_clean(number) for number in numbers]
    explicit = [_lead(number) == "plus" for number in cleaned]
    home = [_read(number, home_region) for number in cleaned]
    home_prefix = f"+{phonenumbers.country_code_for_region(home_region)}"

    coded, home_count = _prior_counts(prior)
    for (readings, _), is_explicit in zip(home, explicit):
        if not readings:
            continue
        if readings[0].startswith(home_prefix):  # calling codes are prefix-free
            home_count += 1
        elif is_explicit:
            country = country_of(readings[0])
            if country in phonenumbers.SUPPORTED_REGIONS:
                coded[country] += 1
    ranked = sorted(coded.items(), key=lambda item: (-item[1], item[0]))
    second = ranked[0][0] if ranked and ranked[0][1] >= SECOND_COUNTRY_MIN else None
    second_dominates = second is not None and ranked[0][1] > home_count

    book = AddressBook(
        readings=[], second_country=second, counts={"coded": dict(coded), "home": home_count},
    )
    for number, is_explicit, (readings, took_nine) in zip(cleaned, explicit, home):
        entry = list(readings)
        wants_second = second is not None and not is_explicit and (
            not readings
            or took_nine
            or (second_dominates and readings[0].startswith(home_prefix))
        )
        if wants_second:
            alternatives, alternatives_took_nine = _read(number, second)
            added = [reading for reading in alternatives if reading not in entry]
            if added:
                entry += added
                took_nine = took_nine or alternatives_took_nine
                if readings:
                    book.both_ways += 1
                else:
                    book.as_second_country += 1
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
