"""Admin Twilio number inventory tests."""

from app.services import admin_numbers


def test_reconcile_numbers_flags_orphan_and_missing_numbers():
    contractors = [
        {"contractor_id": "has-number", "active": True, "twilio_number": "+15550001111"},
        {"contractor_id": "missing-number", "active": True, "twilio_number": ""},
    ]
    twilio_numbers = [
        {
            "phone_number": "+15550001111",
            "sid": "PN1",
            "voice_url": "https://prod/webhooks/twilio/incoming",
        },
        {
            "phone_number": "+15550002222",
            "sid": "PN2",
            "voice_url": "https://prod/webhooks/twilio/incoming",
        },
    ]

    result = admin_numbers.reconcile_number_inventory(contractors, twilio_numbers)

    assert result["summary"]["assigned_numbers"] == 1
    assert result["summary"]["contractors_missing_numbers"] == 1
    assert result["summary"]["orphan_numbers"] == 1
    assert result["orphan_numbers"][0]["phone_number"] == "+15550002222"
