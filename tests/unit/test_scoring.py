"""Unit tests for trust score engine."""

from app.services.scoring import calculate_trust_score


def test_whitelisted_contact():
    lookups = {"contact": {"is_whitelisted": True}, "history": {}, "twilio": {}, "nomorobo": {}}
    score, breakdown = calculate_trust_score("+15551234567", lookups)
    assert score == 100
    assert "whitelist" in breakdown


def test_blacklisted_contact():
    lookups = {"contact": {"is_blacklisted": True}, "history": {}, "twilio": {}, "nomorobo": {}}
    score, breakdown = calculate_trust_score("+15551234567", lookups)
    assert score == 0
    assert "blacklist" in breakdown


def test_unknown_caller_baseline():
    lookups = {"contact": None, "history": {}, "twilio": {}, "nomorobo": {}}
    score, breakdown = calculate_trust_score("+15551234567", lookups)
    assert score == 50  # baseline


def test_high_spam_score():
    lookups = {"contact": None, "history": {}, "twilio": {}, "nomorobo": {"spam_score": 0.9}}
    score, _ = calculate_trust_score("+15551234567", lookups)
    assert score < 30  # should be in spam range


def test_voip_penalty():
    lookups = {"contact": None, "history": {}, "twilio": {"line_type": "voip"}, "nomorobo": {}}
    score, breakdown = calculate_trust_score("+15551234567", lookups)
    assert score < 50
    assert "line_type_voip" in breakdown


def test_repeated_pickups_increase_trust():
    lookups = {
        "contact": None,
        "history": {"times_picked_up": 5, "times_ignored": 0},
        "twilio": {},
        "nomorobo": {},
    }
    score, _ = calculate_trust_score("+15551234567", lookups)
    assert score > 60  # should be elevated


def test_repeated_ignores_decrease_trust():
    lookups = {
        "contact": None,
        "history": {"times_picked_up": 0, "times_ignored": 5},
        "twilio": {},
        "nomorobo": {},
    }
    score, _ = calculate_trust_score("+15551234567", lookups)
    assert score < 50  # should be below baseline


def test_score_clamped_to_0_100():
    # Extreme spam + VoIP + ignored — should not go below 0
    lookups = {
        "contact": None,
        "history": {"times_picked_up": 0, "times_ignored": 100},
        "twilio": {"line_type": "voip"},
        "nomorobo": {"spam_score": 0.99},
    }
    score, _ = calculate_trust_score("+15551234567", lookups)
    assert 0 <= score <= 100


def test_landline_bonus():
    lookups = {"contact": None, "history": {}, "twilio": {"line_type": "landline"}, "nomorobo": {}}
    score, breakdown = calculate_trust_score("+15551234567", lookups)
    assert score > 50
    assert "line_type_landline" in breakdown


def test_carrier_bonus():
    lookups = {"contact": None, "history": {}, "twilio": {"carrier": "T-Mobile"}, "nomorobo": {}}
    score, breakdown = calculate_trust_score("+15551234567", lookups)
    assert "has_carrier" in breakdown
