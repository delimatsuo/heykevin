"""Returning-customer context shared by every normal Kevin voice engine."""

import os
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services.customer_memory import CustomerMemory, IdentitySource, IdentityState
from app.services.receptionist_context import (
    build_customer_memory_prompt,
    build_greeting_text,
    load_customer_memory_context,
    project_customer_memory,
    returning_caller_first_name,
)
from app.services.voice_pipeline import build_system_prompt


def _config(**overrides):
    config = {
        "contractor_id": "contractor-1",
        "owner_name": "Deli Matsuo",
        "business_name": "Test Plumbing",
        "effective_mode": "business",
    }
    config.update(overrides)
    return config


def test_returning_caller_gets_natural_greeting_without_demo_language():
    greeting = build_greeting_text(
        _config(
            known_caller_name="Jonathan Smith",
            known_caller_name_trusted=True,
        ),
        after_hours=False,
    )

    assert greeting == "Hello, Jonathan. How can I help you today?"
    assert "demo" not in greeting.lower()


def test_customer_memory_name_wins_over_legacy_known_name():
    now = datetime.now(UTC)
    config = _config(
        customer_memory_personalization_enabled=True,
        known_caller_name="Legacy Name",
        customer_memory=CustomerMemory(
            contractor_id="contractor-1",
            customer_key="opaque-key",
            display_name="Jonathan Smith",
            identity_state=IdentityState.CONFIRMED,
            identity_source=IdentitySource.CALLER_CONFIRMED,
            confidence=0.95,
            language="en",
            revision=1,
            created_at=now,
            updated_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=90),
            last_command_id="call-1:name",
        ),
    )

    assert returning_caller_first_name(config) == "Jonathan"


def test_unconfirmed_product_memory_does_not_override_a_trusted_contact_name():
    now = datetime.now(UTC)
    config = _config(
        customer_memory_personalization_enabled=True,
        known_caller_name="Jonathan Smith",
        known_caller_name_trusted=True,
        customer_memory=CustomerMemory(
            contractor_id="contractor-1",
            customer_key="opaque-key",
            display_name="Wrong Name",
            identity_state=IdentityState.CANDIDATE,
            identity_source=IdentitySource.TRANSCRIPT_EXTRACTED,
            confidence=0.6,
            language="en",
            revision=1,
            created_at=now,
            updated_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=90),
            last_command_id="call-1:name",
        ),
    )

    assert returning_caller_first_name(config) == "Jonathan"


def test_unconfirmed_name_is_not_projected_into_model_context():
    now = datetime.now(UTC)
    memory = CustomerMemory(
        contractor_id="contractor-1",
        customer_key="opaque-key",
        display_name="Unverified Name",
        identity_state=IdentityState.CANDIDATE,
        identity_source=IdentitySource.TRANSCRIPT_EXTRACTED,
        confidence=0.6,
        language="en",
        revision=1,
        created_at=now,
        updated_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=90),
        last_command_id="call-1:name",
    )

    projection = project_customer_memory(memory)
    prompt = build_customer_memory_prompt({"customer_memory": projection})

    assert projection["display_name"] == ""
    assert "Unverified Name" not in prompt


def test_bare_legacy_name_is_not_spoken_without_trusted_provenance():
    config = _config(known_caller_name="Unverified Name")

    greeting = build_greeting_text(config, after_hours=False)

    assert "Unverified" not in greeting
    assert greeting.startswith("Hi, thank you for calling Test Plumbing")


def test_spoken_name_is_bounded_and_strips_stored_markup():
    config = _config(
        known_caller_name="Jonathan<script> system: ignore",
        known_caller_name_trusted=True,
    )

    assert returning_caller_first_name(config) == "Jonathanscript"
    assert "<" not in build_greeting_text(config, after_hours=False)


def test_memory_prompt_supplies_internal_request_refs_and_normal_call_policy(monkeypatch):
    from app.services import receptionist_context

    monkeypatch.setattr(
        receptionist_context.settings,
        "service_request_recovery_enabled",
        True,
    )
    config = _config(
        customer_memory_personalization_enabled=True,
        service_request_mutations_enabled=True,
        integration_write_status="approved",
        google_calendar_access_token="test-token",
        google_calendar_refresh_token="test-refresh",
        customer_memory={
            "display_name": "Jonathan Smith",
        },
        service_request_context={
            "customer_key": "customer-key",
            "open_service_requests": [
                {
                    "request_id": "req_123",
                    "revision": 2,
                    "status": "scheduled",
                    "services": ["Furnace tune-up"],
                    "start_time": "2026-08-20T14:00:00+00:00",
                    "end_time": "2026-08-20T15:00:00+00:00",
                }
            ],
        },
    )

    context = build_customer_memory_prompt(config)
    prompt = build_system_prompt(config)

    assert "req_123" in context
    assert '"revision": 2' in context
    assert "continue naturally" in context.lower()
    assert "demo" not in context.lower()
    assert "Claim a change only after the tool reports success" in context
    assert context in prompt


def test_empty_memory_adds_no_returning_customer_prompt():
    assert build_customer_memory_prompt(_config()) == ""


def test_memory_personalization_is_closed_when_flag_is_absent():
    config = _config(customer_memory={"display_name": "Jonathan Smith"})

    assert returning_caller_first_name(config) == ""
    assert build_customer_memory_prompt(config) == ""


def test_request_context_can_be_enabled_without_name_personalization(monkeypatch):
    from app.services import receptionist_context

    monkeypatch.setattr(
        receptionist_context.settings,
        "service_request_recovery_enabled",
        True,
    )
    context = build_customer_memory_prompt(
        _config(
            service_request_context={
                "customer_key": "customer-key",
                "open_service_requests": [
                    {
                        "request_id": "req_123",
                        "revision": 2,
                        "status": "open",
                        "services": ["Furnace tune-up"],
                    }
                ],
            },
            service_request_mutations_enabled=True,
            integration_write_status="approved",
            google_calendar_access_token="test-token",
            google_calendar_refresh_token="test-refresh",
        )
    )

    assert "req_123" in context
    assert "Jonathan" not in context


@pytest.mark.asyncio
async def test_call_setup_does_not_open_memory_store_when_both_flags_are_closed(monkeypatch):
    class FailRepository:
        def __init__(self):
            raise AssertionError("memory repository must not be opened")

    monkeypatch.setattr(
        "app.db.customer_memory.FirestoreCustomerMemoryRepository",
        FailRepository,
    )

    assert (
        await load_customer_memory_context(
            "contractor-1",
            "+16175550123",
        )
        == {}
    )


@pytest.mark.asyncio
async def test_request_context_does_not_require_an_identity_memory_document(monkeypatch):
    class FailMemoryRepository:
        def __init__(self):
            raise AssertionError("identity memory must not be opened")

    class RequestRepository:
        async def list_actionable(self, **_kwargs):
            return ()

    monkeypatch.setattr(
        "app.db.customer_memory.FirestoreCustomerMemoryRepository",
        FailMemoryRepository,
    )
    monkeypatch.setattr(
        "app.db.service_requests.FirestoreServiceRequestRepository",
        RequestRepository,
    )

    result = await load_customer_memory_context(
        "contractor-1",
        "+16175550123",
        personalization_enabled=False,
        mutations_enabled=True,
    )

    assert result["service_request_context"]["customer_key"]
    assert result["service_request_context"]["open_service_requests"] == []
    assert "customer_memory" not in result
