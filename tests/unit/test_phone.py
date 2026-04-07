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


def test_phone_hash_consistent():
    h1 = phone_hash("+16504222677")
    h2 = phone_hash("+16504222677")
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_phone_hash_different():
    h1 = phone_hash("+16504222677")
    h2 = phone_hash("+16504228667")
    assert h1 != h2
