"""Unit tests for phone number utilities."""

from app.utils import phone as phone_utils
from app.utils.phone import describe_unreadable_numbers, normalize_phone, phone_hash


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


def test_shapes_say_how_each_number_starts():
    summary = describe_unreadable_numbers(
        ["0 21 11 98765 4321", "0055 11 98765 4321", "*67 415 555 0123", "12", "not-a-number"],
        "US",
    )

    assert summary["shapes"] == {
        "zero/14": 1, "double-zero/15": 1, "service/12": 1, "national/2": 1, "national/0": 1,
    }
    assert summary["valid_nowhere"] >= 2  # "12" and "not-a-number" are numbers nowhere


def test_the_owners_own_country_is_never_counted():
    summary = describe_unreadable_numbers(["11 98765-4321"], "BR")

    assert "BR" not in summary["valid_in"]


def test_the_summary_carries_no_digits_of_any_number():
    numbers = ["(11) 98765-4321", "+55 11 8765-4321", "0 21 11 97531-8642", "555-0193"]

    text = repr(describe_unreadable_numbers(numbers, "US"))

    for number in numbers:
        digits = "".join(ch for ch in number if ch.isdigit())
        for start in range(len(digits) - 3):
            assert digits[start:start + 4] not in text


def test_a_huge_upload_is_described_from_its_first_numbers(monkeypatch):
    monkeypatch.setattr(phone_utils, "SHAPE_SCAN_LIMIT", 3)

    summary = describe_unreadable_numbers(["11 98765-4321"] * 5, "US")

    assert summary["scanned"] == 3
    assert summary["shapes"] == {"national/11": 3}
