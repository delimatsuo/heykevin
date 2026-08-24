import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

import pytest
from types import SimpleNamespace
from app.db.contractors import PROTECTED_FIELDS
from app.api import contractors as contractors_api


def _admin_request():
    return SimpleNamespace(state=SimpleNamespace(is_admin=True))


def test_integration_fields_in_protected_fields():
    """Proves all server-owned integration credential and lifecycle fields are in PROTECTED_FIELDS."""
    required_fields = {
        # Jobber credentials & lifecycle
        "jobber_access_token",
        "jobber_refresh_token",
        "jobber_connected",
        "jobber_generation",
        "jobber_connected_at",
        "jobber_disconnected_at",
        "jobber_token_refreshed_at",
        "jobber_token_expires_at",
        "jobber_refresh_claim_id",
        "jobber_refresh_claim_expires_at",
        "jobber_refresh_claim_generation",
        "jobber_lead_capture_enabled",
        "jobber_lead_capture_updated_at",
        "jobber_token_envelope_required",
        # Google Calendar credentials & lifecycle
        "google_calendar_access_token",
        "google_calendar_refresh_token",
        "google_calendar_connected",
        "google_calendar_generation",
        "google_calendar_scope",
        "google_calendar_connected_at",
        "google_calendar_disconnected_at",
        "google_calendar_token_refreshed_at",
        "google_calendar_token_expires_at",
        "google_calendar_refresh_claim_id",
        "google_calendar_refresh_claim_expires_at",
        "google_calendar_refresh_claim_generation",
        "google_calendar_token_envelope_required",
    }
    missing = required_fields - PROTECTED_FIELDS
    assert not missing, f"Missing integration fields in PROTECTED_FIELDS: {missing}"


@pytest.mark.asyncio
async def test_patch_contractor_strips_protected_integration_fields(monkeypatch):
    """Proves that PATCH /api/contractors/{id} strips all protected integration fields."""
    captured_updates = {}

    async def fake_update_contractor(contractor_id, updates):
        captured_updates.update(updates)
        return True

    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)

    # Simulate a permissive/expanded client model dictionary containing protected fields
    class PermissiveUpdate(contractors_api.ContractorUpdate):
        class Config:
            extra = "allow"

    payload = PermissiveUpdate(
        business_name="Acme Corp",
        jobber_access_token="malicious_token",
        jobber_connected=True,
        jobber_generation=99,
        jobber_token_envelope_required=False,
        google_calendar_access_token="stolen_token",
        google_calendar_scope="https://www.googleapis.com/auth/calendar",
        google_calendar_token_envelope_required=False,
        jobber_lead_capture_enabled=True,
    )

    resp = await contractors_api.api_update_contractor(
        "contractor-1",
        payload,
        _admin_request(),
    )
    assert resp == {"status": "ok"}
    assert captured_updates == {"business_name": "Acme Corp"}
    assert "jobber_access_token" not in captured_updates
    assert "jobber_connected" not in captured_updates
    assert "jobber_generation" not in captured_updates
    assert "jobber_token_envelope_required" not in captured_updates
    assert "google_calendar_access_token" not in captured_updates
    assert "google_calendar_scope" not in captured_updates
    assert "google_calendar_token_envelope_required" not in captured_updates
    assert "jobber_lead_capture_enabled" not in captured_updates
