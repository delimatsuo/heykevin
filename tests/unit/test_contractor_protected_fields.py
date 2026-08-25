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
        "jobber_lifecycle_epoch",
        "jobber_connected_at",
        "jobber_disconnected_at",
        "jobber_token_refreshed_at",
        "jobber_token_expires_at",
        "jobber_refresh_claim_id",
        "jobber_refresh_claim_expires_at",
        "jobber_refresh_claim_generation",
        "jobber_refresh_phase",
        "jobber_refresh_claim_phase",
        "jobber_refresh_outcome_unknown",
        "jobber_reauthorization_required",
        "jobber_lead_capture_enabled",
        "jobber_lead_capture_updated_at",
        "jobber_token_envelope_required",
        "jobber_operation_intent_id",
        "jobber_operation_intent_kind",
        "jobber_operation_intent_phase",
        "jobber_operation_intent_expires_at",
        "jobber_operation_intent_acquired_at",
        "jobber_operation_intent_generation",
        "jobber_operation_intent_lifecycle_epoch",
        "jobber_operation_intent_credentials_fingerprint",
        # Google Calendar credentials & lifecycle
        "google_calendar_access_token",
        "google_calendar_refresh_token",
        "google_calendar_connected",
        "google_calendar_generation",
        "google_calendar_lifecycle_epoch",
        "google_calendar_scope",
        "google_calendar_connected_at",
        "google_calendar_disconnected_at",
        "google_calendar_token_refreshed_at",
        "google_calendar_token_expires_at",
        "google_calendar_refresh_claim_id",
        "google_calendar_refresh_claim_expires_at",
        "google_calendar_refresh_claim_generation",
        "google_calendar_refresh_phase",
        "google_calendar_refresh_claim_phase",
        "google_calendar_refresh_outcome_unknown",
        "google_calendar_reauthorization_required",
        "google_calendar_token_envelope_required",
        "google_calendar_operation_intent_id",
        "google_calendar_operation_intent_kind",
        "google_calendar_operation_intent_phase",
        "google_calendar_operation_intent_expires_at",
        "google_calendar_operation_intent_acquired_at",
        "google_calendar_operation_intent_generation",
        "google_calendar_operation_intent_lifecycle_epoch",
        "google_calendar_operation_intent_credentials_fingerprint",
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


def test_18j_dynamic_operation_intent_keys_subset_of_protected_fields():
    """Dynamically assert get_provider_operation_intent_keys(provider) is a subset of PROTECTED_FIELDS for jobber and google_calendar, covering aliases without a hand-maintained duplicate list."""
    from app.services.integration_tokens import get_provider_operation_intent_keys, VALID_PROVIDERS

    for provider in ["jobber", "google_calendar"]:
        intent_keys = get_provider_operation_intent_keys(provider)
        missing = intent_keys - PROTECTED_FIELDS
        assert not missing, f"Operation intent keys for provider '{provider}' missing from PROTECTED_FIELDS: {missing}"

    for provider in VALID_PROVIDERS:
        intent_keys = get_provider_operation_intent_keys(provider)
        assert intent_keys.issubset(PROTECTED_FIELDS), f"Provider '{provider}' intent keys dynamically violate PROTECTED_FIELDS contract"
