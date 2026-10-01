"""Unit tests for phone number utilities."""

from app.utils.phone import normalize_phone, phone_hash


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
