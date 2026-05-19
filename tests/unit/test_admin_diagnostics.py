"""Admin support diagnostics tests."""

from app.services import admin_diagnostics


def test_diagnose_contractor_flags_missing_number_expired_subscription_and_missing_voip():
    findings = admin_diagnostics.diagnose_contractor(
        {"subscription_status": "expired", "twilio_number": ""},
        {"push_token_present": True, "voip_token_present": False},
        [],
    )

    assert {finding["code"] for finding in findings} == {
        "missing_twilio_number",
        "subscription_expired",
        "missing_voip_token",
        "no_recent_calls",
    }
