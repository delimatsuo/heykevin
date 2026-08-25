import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.services.gemini_pipeline import GeminiPipeline
from app.services.receptionist_tools import (
    ADD_SERVICE_TO_REQUEST,
    CANCEL_SERVICE_REQUEST,
    RESCHEDULE_SERVICE_REQUEST,
    ReceptionistToolExecutor,
)
from app.services.service_request_repository import (
    InMemoryServiceRequestRepository,
    ProviderBinding,
    ServiceRequestCommandService,
    customer_key_for_phone,
)
from app.services.voice_pipeline import VoicePipeline


async def _noop(*_args, **_kwargs):
    return None


@pytest.fixture(autouse=True)
def _provider_recovery_ready(monkeypatch):
    from app.services import voice_pipeline

    monkeypatch.setattr(
        voice_pipeline.settings,
        "service_request_recovery_enabled",
        True,
    )


def _request_context(customer_key: str) -> dict:
    return {
        "schema_version": 1,
        "customer_key": customer_key,
        "display_name": "Jonathan Smith",
        "open_service_requests": [
            {
                "request_id": "request-1",
                "revision": 1,
                "status": "open",
                "services": ["Furnace tune-up"],
                "start_time": "2026-08-20T14:00:00+00:00",
                "end_time": "2026-08-20T15:00:00+00:00",
            }
        ],
    }


@pytest.mark.asyncio
async def test_normal_voice_pipeline_applies_returning_customer_changes():
    now = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
    repository = InMemoryServiceRequestRepository()
    service = ServiceRequestCommandService(repository, clock=lambda: now)
    created = await service.create_service_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        services=["Furnace tune-up"],
        scheduled_start=now + timedelta(days=1),
        scheduled_end=now + timedelta(days=1, hours=1),
        expected_revision=0,
        idempotency_key="create-1",
    )
    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="CA-returning",
        caller_phone="+16175550123",
        contractor_config={
            "contractor_id": "contractor-1",
            "effective_mode": "business",
            "service_request_context": _request_context("unused-in-this-in-memory-test"),
            "service_request_mutations_enabled": True,
            "integration_write_status": "approved",
            "google_calendar_access_token": "test-token",
            "google_calendar_refresh_token": "test-refresh",
        },
    )
    await pipeline._http_client.aclose()
    pipeline._receptionist_tool_executor = ReceptionistToolExecutor(
        service,
        contractor_id="contractor-1",
        caller_phone="+16175550123",
    )

    added = json.loads(
        await pipeline._execute_tool(
            ADD_SERVICE_TO_REQUEST,
            {
                "request_id": "request-1",
                "expected_revision": created.revision,
                "service": "Filter replacement",
            },
            operation_id="add-1",
        )
    )
    moved = json.loads(
        await pipeline._execute_tool(
            RESCHEDULE_SERVICE_REQUEST,
            {
                "request_id": "request-1",
                "expected_revision": added["revision"],
                "scheduled_start": (now + timedelta(days=2)).isoformat(),
                "scheduled_end": (now + timedelta(days=2, hours=1)).isoformat(),
            },
            operation_id="move-1",
        )
    )
    cancelled = json.loads(
        await pipeline._execute_tool(
            CANCEL_SERVICE_REQUEST,
            {
                "request_id": "request-1",
                "expected_revision": moved["revision"],
            },
            operation_id="cancel-1",
        )
    )

    assert added["success"] is True
    assert moved["success"] is True
    assert cancelled == {
        "status": "applied",
        "success": True,
        "confirmed": True,
        "message": "The service request was cancelled and the change is confirmed.",
        "request_id": "request-1",
        "revision": 4,
    }


def test_gemini_declarations_include_shared_request_tools_for_loaded_context(monkeypatch):
    monkeypatch.setattr(
        "app.services.gemini_pipeline.staging_native_live_safety_controls_enabled",
        lambda: False,
    )
    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        contractor_config={
            "contractor_id": "contractor-1",
            "effective_mode": "business",
            "service_request_context": _request_context("customer-key"),
            "service_request_mutations_enabled": True,
            "integration_write_status": "approved",
            "google_calendar_access_token": "test-token",
            "google_calendar_refresh_token": "test-refresh",
        },
        caller_phone="+16175550123",
    )

    names = {
        declaration["name"]
        for declaration in pipeline._build_gemini_tools()[0]["function_declarations"]
    }

    assert {
        CANCEL_SERVICE_REQUEST,
        RESCHEDULE_SERVICE_REQUEST,
        ADD_SERVICE_TO_REQUEST,
    } <= names


def test_request_mutation_tools_fail_closed_when_integration_write_is_revoked(monkeypatch):
    monkeypatch.setattr(
        "app.services.gemini_pipeline.staging_native_live_safety_controls_enabled",
        lambda: False,
    )
    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        contractor_config={
            "contractor_id": "contractor-1",
            "service_request_context": _request_context("customer-key"),
            "service_request_mutations_enabled": True,
            "integration_write_status": "revoked",
            "google_calendar_access_token": "test-token",
            "google_calendar_refresh_token": "test-refresh",
        },
        caller_phone="+16175550123",
    )

    declarations = pipeline._build_gemini_tools()[0]["function_declarations"]

    assert not (
        {item["name"] for item in declarations}
        & {
            CANCEL_SERVICE_REQUEST,
            RESCHEDULE_SERVICE_REQUEST,
            ADD_SERVICE_TO_REQUEST,
        }
    )


@pytest.mark.asyncio
async def test_undeclared_request_mutation_still_fails_closed_when_write_is_revoked():
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._contractor_config = {
        "contractor_id": "contractor-1",
        "service_request_context": _request_context("customer-key"),
        "service_request_mutations_enabled": True,
        "integration_write_status": "revoked",
        "google_calendar_access_token": "test-token",
        "google_calendar_refresh_token": "test-refresh",
    }
    pipeline._caller_phone = "+16175550123"
    pipeline._call_sid = "CA-revoked"

    result = json.loads(
        await pipeline._execute_tool(
            CANCEL_SERVICE_REQUEST,
            {"request_id": "request-1", "expected_revision": 1},
            operation_id="cancel-1",
        )
    )

    assert result == {
        "status": "failed",
        "success": False,
        "confirmed": False,
        "message": "That request change is not enabled for this account.",
    }


@pytest.mark.asyncio
async def test_confirmed_google_booking_atomically_creates_bound_service_request():
    repository = InMemoryServiceRequestRepository()

    class _Provider:
        def __init__(self):
            self.creates = []

        async def create(self, **kwargs):
            self.creates.append(kwargs)
            return True

    provider = _Provider()
    service = ServiceRequestCommandService(
        repository,
        provider_adapter=provider,
        clock=lambda: datetime(2026, 8, 12, 16, 0, tzinfo=UTC),
    )
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._contractor_config = {
        "contractor_id": "contractor-1",
        "service_request_command_service": service,
        "service_request_mutations_enabled": True,
        "integration_write_status": "approved",
        "google_calendar_access_token": "test-token",
        "google_calendar_refresh_token": "test-refresh",
    }
    pipeline._caller_phone = "+16175550123"
    pipeline._call_sid = "CA-booking"

    result = await pipeline._create_managed_google_booking(
        tool_input={
            "title": "Furnace tune-up",
            "start_time": "2026-08-20T14:00:00+00:00",
            "end_time": "2026-08-20T15:00:00+00:00",
        },
        operation_id="tool-book-1",
    )

    from app.services.voice_pipeline import _managed_booking_identity

    customer_key = customer_key_for_phone("+16175550123")
    request_id, event_id, *_ = _managed_booking_identity(
        "contractor-1",
        "+16175550123",
        "Furnace tune-up",
        "2026-08-20T14:00:00+00:00",
        "2026-08-20T15:00:00+00:00",
    )
    stored = await repository.get(
        contractor_id="contractor-1",
        customer_key=customer_key,
        request_id=request_id,
    )
    binding = await repository.get_provider_binding(
        contractor_id="contractor-1",
        customer_key=customer_key,
        request_id=request_id,
    )

    assert result == {"success": True, "revision": 1}
    assert len(provider.creates) == 1
    assert stored is not None
    assert stored.services == ("Furnace tune-up",)
    assert binding == ProviderBinding(
        kind="google_calendar",
        resource_id=event_id,
    )


@pytest.mark.asyncio
async def test_managed_google_booking_rechecks_tenant_enrollment():
    class _CommandService:
        create_provider_service_request = AsyncMock()

    command_service = _CommandService()
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._contractor_config = {
        "contractor_id": "contractor-1",
        "service_request_command_service": command_service,
        "integration_write_status": "approved",
        "google_calendar_access_token": "test-token",
        "google_calendar_refresh_token": "test-refresh",
        # service_request_mutations_enabled deliberately absent.
    }
    pipeline._caller_phone = "+16175550123"
    pipeline._call_sid = "CA-booking"

    result = await pipeline._create_managed_google_booking(
        tool_input={
            "title": "Furnace tune-up",
            "start_time": "2026-08-20T14:00:00+00:00",
            "end_time": "2026-08-20T15:00:00+00:00",
        },
        operation_id="tool-book-1",
    )

    assert result == {
        "success": False,
        "error": "Managed appointment creation is not enabled for this account.",
    }
    command_service.create_provider_service_request.assert_not_awaited()
