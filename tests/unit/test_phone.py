"""Unit tests for phone number utilities."""

from app.utils import phone as phone_utils
from app.utils.phone import (
    describe_unreadable_numbers,
    normalize_phone,
    phone_hash,
    read_address_book,
)


def test_normalize_us_number():
    assert normalize_phone("650-422-2677") == "+16504222677"
    assert normalize_phone("(650) 422-2677") == "+16504222677"
    assert normalize_phone("+16504222677") == "+16504222677"
    assert normalize_phone("16504222677") == "+16504222677"


def test_normalize_invalid():
    assert normalize_phone("123") is None
    assert normalize_phone("not-a-number") is None
    assert normalize_phone("") is None


def test_normalize_international_with_region():
    # Brazil
    assert normalize_phone("(11) 98765-4321", default_region="BR") == "+5511987654321"
    assert normalize_phone("+5511987654321") == "+5511987654321"

    # Canada
    assert normalize_phone("(416) 555-1234", default_region="CA") == "+14165551234"
    assert normalize_phone("+14165551234") == "+14165551234"

    # United Kingdom
    assert normalize_phone("020 7946 0958", default_region="GB") == "+442079460958"
    assert normalize_phone("+442079460958") == "+442079460958"



def test_phone_hash_consistent():
    h1 = phone_hash("+16504222677")
    h2 = phone_hash("+16504222677")
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_phone_hash_different():
    h1 = phone_hash("+16504222677")
    h2 = phone_hash("+16504228667")
    assert h1 != h2


# --- describe_unreadable_numbers: why a sync dropped numbers, in counts only ---
# Every number below is made up.


def test_brazilian_numbers_saved_without_country_code_point_to_brazil():
    summary = describe_unreadable_numbers(
        ["(11) 98765-4321", "11 97654-3210", "(31) 99876-5432"], "US",
    )

    assert summary["shapes"] == {"national/11": 3}
    assert summary["valid_in"]["BR"] == 3
    assert summary["valid_nowhere"] == 0
    assert summary["plus_countries"] == {}


def test_a_plus_number_is_counted_under_the_country_its_code_names():
    # An old 8-digit Brazilian mobile: the country code is there, the number is not valid.
    summary = describe_unreadable_numbers(["+55 11 8765-4321"], "US")

    assert summary["shapes"] == {"plus/12": 1}
    assert summary["plus_countries"] == {"BR": 1}
    assert summary["valid_in"] == {}
    assert summary["valid_nowhere"] == 0
    assert summary["scanned"] == 0  # a "+" number is never tried as other countries


def test_a_plus_hidden_behind_spaces_or_format_marks_is_still_a_plus():
    numbers = [
        "  +55 11 8765-4321",
        "\N{LEFT-TO-RIGHT EMBEDDING}+55 11 8765-4321\N{POP DIRECTIONAL FORMATTING}",
        "\N{FULLWIDTH PLUS SIGN}55 11 8765-4321",
    ]

    summary = describe_unreadable_numbers(numbers, "US")

    assert summary["shapes"] == {"plus/12": 3}
    assert summary["plus_countries"] == {"BR": 3}


def test_a_number_dialed_with_00_counts_under_the_country_it_dials():
    assert describe_unreadable_numbers(["0055 11 98765 4321"], "US")["valid_in"] == {"BR": 1}
    # Dialed this way it can land in the owner's own country.
    assert describe_unreadable_numbers(["0055 11 98765 4321"], "BR")["valid_in"] == {"BR": 1}


def test_country_counts_keep_only_the_largest_in_a_stable_order():
    # An 8-digit national number is valid in many countries.
    first = describe_unreadable_numbers(["98765-4321"], "US")["valid_in"]

    assert len(first) == phone_utils.SHAPE_TOP_COUNTRIES
    assert list(first) == sorted(first)  # all tied at 1, so alphabetical


def test_a_number_too_long_to_be_valid_is_not_tried_as_other_countries(monkeypatch):
    calls = []
    real_parse = phone_utils.phonenumbers.parse
    monkeypatch.setattr(
        phone_utils.phonenumbers, "parse",
        lambda *args, **kwargs: calls.append(args) or real_parse(*args, **kwargs),
    )

    summary = describe_unreadable_numbers(["1" * 40], "US")

    assert calls == []
    assert summary["shapes"] == {"national/40": 1}
    assert summary["valid_nowhere"] == 1


def test_the_country_scan_stops_when_its_time_is_up(monkeypatch):
    monkeypatch.setattr(phone_utils, "SHAPE_SCAN_SECONDS", 0)

    summary = describe_unreadable_numbers(["11 98765-4321", "+55 11 8765-4321"], "US")

    assert summary["scanned"] == 0
    assert summary["valid_in"] == {}
    assert summary["shapes"] == {"national/11": 1, "plus/12": 1}
    assert summary["plus_countries"] == {"BR": 1}


def test_shapes_say_how_each_number_starts():
    summary = describe_unreadable_numbers(
        ["0 21 11 98765 4321", "0055 11 98765 4321", "*67 415 555 0123", "12", "not-a-number"],
        "US",
    )

    assert summary["shapes"] == {
        "zero/14": 1, "double-zero/15": 1, "service/12": 1, "national/2": 1, "national/0": 1,
    }
    assert summary["valid_nowhere"] >= 2  # "12" and "not-a-number" are numbers nowhere


def test_a_number_is_not_parsed_again_as_the_owners_country():
    summary = describe_unreadable_numbers(["11 98765-4321"], "BR")

    assert "BR" not in summary["valid_in"]


def test_the_summary_carries_no_digits_of_any_number():
    numbers = ["(11) 98765-4321", "+55 11 8765-4321", "0 21 11 97531-8642", "555-0193"]

    text = repr(describe_unreadable_numbers(numbers, "US"))

    for number in numbers:
        digits = "".join(ch for ch in number if ch.isdigit())
        for start in range(len(digits) - 3):
            assert digits[start:start + 4] not in text


def test_a_huge_upload_scans_its_first_numbers_but_shapes_them_all(monkeypatch):
    monkeypatch.setattr(phone_utils, "SHAPE_SCAN_LIMIT", 3)

    summary = describe_unreadable_numbers(["11 98765-4321"] * 5, "US")

    assert summary["scanned"] == 3
    assert summary["valid_in"]["BR"] == 3
    assert summary["shapes"] == {"national/11": 5}


# --- read_address_book: an owner's address book, read for the whitelist ---
# Every number below is made up. Three valid Brazilian mobiles written with +55
# make Brazil an address book's second country.

BRAZIL_CODED = ["+55 11 98765-1111", "+55 21 98765-2222", "+55 31 98765-3333"]
BRAZIL_CODED_E164 = [["+5511987651111"], ["+5521987652222"], ["+5531987653333"]]


def test_an_old_brazilian_mobile_is_read_with_its_nine():
    book = read_address_book(["+55 11 8765-4321"], "US")

    assert book.readings == [["+5511987654321"]]
    assert book.ninth_digit == 1


def test_only_brazilian_numbers_are_given_a_nine():
    # Shaped like an old Brazilian mobile after its country code, but American.
    assert read_address_book(["+1 118 765 4321"], "US").readings == [[]]


def test_a_short_brazilian_number_stays_unread():
    assert read_address_book(["+55 11"], "US").readings == [[]]


def test_a_number_dialed_abroad_from_home_gets_one_reading():
    # "011" dials out of the US: a St Helena number, though after a trunk 0 the
    # same digits are a São Paulo landline. Only home numbers are read both ways.
    book = read_address_book(BRAZIL_CODED + ["011 290 22345", "011 55 11 98765-4321"], "US")

    assert book.readings[-2:] == [["+29022345"], ["+5511987654321"]]
    assert book.both_ways == 0


def test_a_number_dialed_abroad_does_not_make_a_second_country():
    book = read_address_book(BRAZIL_CODED[:2] + ["011 55 11 98765-4321", "0 11 2345-6789"], "US")

    assert book.second_country is None
    assert book.readings[-1] == []


def test_a_brazilian_landline_is_read_as_it_is():
    book = read_address_book(["+55 21 2345-6789"], "US")

    assert book.readings == [["+552123456789"]]
    assert book.ninth_digit == 0


def test_numbers_unreadable_at_home_are_read_as_the_second_countrys():
    numbers = BRAZIL_CODED + [
        "0 21 11 8765-4321",  # trunk 0, carrier code, old 8-digit mobile
        "0 11 2345-6789",  # trunk 0, landline
        "55 11 8765 4321",  # country code without "+", old mobile
        "11 98765-4321",  # area code and mobile, no country code
    ]

    book = read_address_book(numbers, "US")

    assert book.second_country == "BR"
    assert book.readings == BRAZIL_CODED_E164 + [
        ["+5511987654321"], ["+551123456789"], ["+5511987654321"], ["+5511987654321"],
    ]
    assert book.as_second_country == 4
    assert book.ninth_digit == 2
    assert book.both_ways == 0


def test_too_few_coded_numbers_make_no_second_country():
    book = read_address_book(BRAZIL_CODED[:2] + ["0 11 2345-6789"], "US")

    assert book.second_country is None
    assert book.readings[-1] == []
    assert book.as_second_country == 0


def test_a_number_valid_in_both_countries_is_read_both_ways_when_the_second_dominates():
    # Three coded Brazilian numbers against two numbers read as US numbers.
    book = read_address_book(BRAZIL_CODED + ["(21) 2345-6789", "(21) 8765-4321"], "US")

    assert book.readings[3:] == [
        ["+12123456789", "+552123456789"],  # a New York number and a Rio landline
        ["+12187654321", "+5521987654321"],  # a Minnesota number and a Rio old mobile
    ]
    assert book.both_ways == 2
    assert book.ninth_digit == 1


def test_home_numbers_keep_one_reading_when_the_second_country_does_not_dominate():
    numbers = BRAZIL_CODED + ["+1 415 555 0101", "+1 415 555 0102", "(21) 2345-6789", "0 11 2345-6789"]

    book = read_address_book(numbers, "US")

    assert book.readings[5:] == [["+12123456789"], ["+551123456789"]]
    assert book.both_ways == 0
    assert book.as_second_country == 1  # unreadable at home is still read as Brazilian


def test_a_number_written_with_a_country_code_gets_one_reading():
    book = read_address_book(BRAZIL_CODED + ["+1 212 345 6789"], "US")

    assert book.readings[-1] == ["+12123456789"]
    assert book.both_ways == 0


def test_local_numbers_without_an_area_code_stay_unread():
    book = read_address_book(BRAZIL_CODED + ["8765-4321", "98765-4321"], "US")

    assert book.readings[-2:] == [[], []]


def test_a_tie_for_second_country_goes_to_the_alphabetically_first():
    british = ["+44 20 7946 0958", "+44 20 7946 0959", "+44 20 7946 0960"]

    assert read_address_book(british + BRAZIL_CODED, "US").second_country == "BR"
    assert read_address_book(BRAZIL_CODED + british, "US").second_country == "BR"


def test_a_non_geographic_code_is_never_the_second_country():
    book = read_address_book(["+800 1234 5678"] * 3 + ["0 11 2345-6789"], "US")

    assert book.second_country is None
    assert book.readings[-1] == []


def test_the_owners_own_country_is_never_the_second_country():
    book = read_address_book(["+1 415 555 0101", "+1 415 555 0102", "+1 415 555 0103"], "US")

    assert book.second_country is None
