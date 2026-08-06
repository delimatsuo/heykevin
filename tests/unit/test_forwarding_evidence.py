"""Unit tests for detecting live call forwarding from Twilio's ForwardedFrom.

Twilio populates ForwardedFrom on a forwarded leg with the number that diverted
the call. When that number is the contractor's own phone, forwarding is provably
live — the only positive proof available, since iOS exposes no forwarding state.

Absence proves nothing: Twilio documents ForwardedFrom as carrier-dependent, so
plenty of working forwards will never populate it.
"""

from app.utils.phone import forwarding_confirms_owner


def test_matching_e164_confirms():
    assert forwarding_confirms_owner("+14155551234", "+14155551234") is True


def test_differing_formats_still_match():
    """Carriers send this field in wildly inconsistent formats."""
    assert forwarding_confirms_owner("4155551234", "+14155551234") is True
    assert forwarding_confirms_owner("(415) 555-1234", "+14155551234") is True
    assert forwarding_confirms_owner("+1 415-555-1234", "+14155551234") is True
    assert forwarding_confirms_owner("14155551234", "+14155551234") is True


def test_different_number_does_not_confirm():
    assert forwarding_confirms_owner("+14155559999", "+14155551234") is False


def test_absent_forwarded_from_does_not_confirm():
    """Carrier-dependent field. Absence is not evidence of anything."""
    assert forwarding_confirms_owner("", "+14155551234") is False
    assert forwarding_confirms_owner(None, "+14155551234") is False


def test_missing_owner_phone_does_not_confirm():
    assert forwarding_confirms_owner("+14155551234", "") is False
    assert forwarding_confirms_owner("+14155551234", None) is False


def test_garbage_does_not_confirm_or_raise():
    assert forwarding_confirms_owner("anonymous", "+14155551234") is False
    assert forwarding_confirms_owner("+14155551234", "not-a-phone") is False
    assert forwarding_confirms_owner("...", "...") is False


def test_unparseable_but_identical_strings_do_not_confirm():
    """Never confirm on raw string equality — that would let junk match junk."""
    assert forwarding_confirms_owner("restricted", "restricted") is False
