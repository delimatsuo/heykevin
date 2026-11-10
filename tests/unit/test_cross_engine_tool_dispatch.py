"""Characterize actual shared tool execution without provider or database I/O.

Only the external boundaries are replaced. In particular, these tests leave
VoicePipeline._execute_tool, write gates, and ReceptionistToolExecutor intact.
Transport differences are observations to preserve during extraction, not new
requirements for accepting malformed model calls.
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services import voice_pipeline
from app.services.gemini_pipeline import GeminiPipeline
from app.services.receptionist_tools import CANCEL_SERVICE_REQUEST
from app.services.relay_pipeline import RelayPipeline
from app.services.service_request_repository import (
    InMemoryServiceRequestRepository,
    ServiceRequestCommandService,
)


async def _noop(*_args, **_kwargs):
    return None


async def _unexpected_io(*_args, **_kwargs):
    pytest.fail("An unstubbed provider or persistence boundary was reached")


@pytest.fixture(autouse=True)
def _offline_boundaries(monkeypatch):
    monkeypatch.setattr(
        "app.services.gemini_pipeline.staging_native_live_safety_controls_enabled",
        lambda: False,
    )
    monkeypatch.setattr(voice_pipeline.settings, "service_request_recovery_enabled", True)
    for target in (
        "app.services.calendar.get_available_slots",
        "app.services.calendar.book_appointment",
        "app.services.calendar.create_managed_appointment",
        "app.services.jobber.lookup_customer",
        "app.db.calls.save_call",
    ):
        monkeypatch.setattr(target, _unexpected_io)


@pytest.fixture(params=["gemini", "relay"])
def engine(request):
    return request.param


def _contractor(tenant="tenant-a", **overrides):
    config = {
        "contractor_id": tenant,
        "effective_mode": "business",
        "business_name": "Synthetic Plumbing",
        "timezone": "UTC",
        "google_calendar_access_token": f"synthetic-access-{tenant}",
        "google_calendar_refresh_token": f"synthetic-refresh-{tenant}",
        "service_request_mutations_enabled": True,
        "integration_write_status": "approved",
        "service_request_context": {
            "customer_key": f"synthetic-customer-{tenant}",
            "open_service_requests": [{"request_id": "request-1", "revision": 1}],
        },
    }
    config.update(overrides)
    return config


def _session(engine, config, *, call_sid="CA-synthetic-a", caller_phone="+16175550101"):
    if engine == "gemini":
        pipeline = GeminiPipeline(
            on_audio_out=_noop,
            on_transcript=_noop,
            contractor_config=config,
            call_sid=call_sid,
            caller_phone=caller_phone,
        )
        send = AsyncMock()
        pipeline._ws = SimpleNamespace(send=send)

        async def dispatch(name, arguments, operation_id="operation-1"):
            send.reset_mock()
            await pipeline._handle_tool_calls([
                {"name": name, "args": arguments, "id": operation_id},
            ])
            send.assert_awaited_once()
            payload = json.loads(send.await_args.args[0])
            responses = payload["tool_response"]["function_responses"]
            assert len(responses) == 1
            assert responses[0]["id"] == operation_id
            assert responses[0]["name"] == name
            return responses[0]["response"]

    else:
        pipeline = RelayPipeline(
            contractor_config=config,
            call_sid=call_sid,
            caller_phone=caller_phone,
            send_to_twilio=_noop,
        )

        async def dispatch(name, arguments, operation_id="operation-1"):
            return await pipeline._execute_tool(
                {"name": name, "args": arguments, "id": operation_id},
            )

    return dispatch


@pytest.mark.asyncio
async def test_calendar_receives_each_tenant_config_and_keeps_refresh_updates(engine, monkeypatch):
    configs = [_contractor("tenant-a"), _contractor("tenant-b")]
    seen = []

    async def slots(config, days):
        seen.append((config, config["google_calendar_access_token"], days))
        config["google_calendar_access_token"] = f"refreshed-{config['contractor_id']}"
        return [{"synthetic_slot": config["contractor_id"]}]

    monkeypatch.setattr("app.services.calendar.get_available_slots", slots)
    sessions = [_session(engine, config) for config in configs]

    for index in (0, 1, 0):
        result = await sessions[index]("check_availability", {"days_ahead": 3})
        assert result == {
            "available_slots": [{"synthetic_slot": configs[index]["contractor_id"]}],
            "days_checked": 3,
        }

    assert [entry[1] for entry in seen] == [
        "synthetic-access-tenant-a", "synthetic-access-tenant-b", "refreshed-tenant-a",
    ]
    assert all(entry[0] is configs[index] for entry, index in zip(seen, (0, 1, 0), strict=True))
    assert [config["google_calendar_access_token"] for config in configs] == [
        "refreshed-tenant-a", "refreshed-tenant-b",
    ]


@pytest.mark.asyncio
async def test_request_commands_bind_call_identity_and_scope_operation_ids(engine):
    cancel = AsyncMock(return_value={"status": "applied", "revision": 2})
    service = SimpleNamespace(cancel_service_request=cancel)
    sessions = [
        _session(
            engine,
            _contractor(tenant, service_request_command_service=service),
            call_sid=call_sid,
            caller_phone=phone,
        )
        for tenant, call_sid, phone in (
            ("tenant-a", "CA-synthetic-a", "+16175550101"),
            ("tenant-b", "CA-synthetic-b", "+16175550102"),
        )
    ]
    arguments = {
        "request_id": "request-1",
        "expected_revision": 1,
        "contractor_id": "untrusted-tenant",
        "caller_phone": "+16175550199",
        "idempotency_key": "untrusted-operation",
    }

    for dispatch in sessions:
        result = await dispatch(CANCEL_SERVICE_REQUEST, arguments, "same-operation")
        assert result["success"] is True
        assert result["confirmed"] is True
        assert result["revision"] == 2

    assert [call.kwargs for call in cancel.await_args_list] == [
        {
            "contractor_id": tenant,
            "caller_phone": phone,
            "request_id": "request-1",
            "expected_revision": 1,
            "idempotency_key": f"{call_sid}:same-operation",
        }
        for tenant, call_sid, phone in (
            ("tenant-a", "CA-synthetic-a", "+16175550101"),
            ("tenant-b", "CA-synthetic-b", "+16175550102"),
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action_flag,automation_approval,tenant_flag,recovery_ready,write_status,books",
    [
        (True, True, True, True, "approved", True),
        (1, True, True, True, "approved", False),
        (True, 1, True, True, "approved", False),
        (True, True, 1, True, "approved", False),
        (True, True, True, 1, "approved", False),
        (True, True, True, True, "revoked", False),
    ],
    ids=["enabled", "nonliteral-action", "nonliteral-approval", "nonliteral-tenant",
         "nonliteral-recovery", "revoked-write"],
)
async def test_booking_gates_produce_requests_without_provider_writes(
    engine, monkeypatch, action_flag, automation_approval, tenant_flag,
    recovery_ready, write_status, books,
):
    provider = SimpleNamespace(create=AsyncMock(return_value=True))
    service = ServiceRequestCommandService(
        InMemoryServiceRequestRepository(), provider_adapter=provider,
    )
    save_call = AsyncMock(return_value=True)
    monkeypatch.setattr("app.db.calls.save_call", save_call)
    monkeypatch.setattr(voice_pipeline.settings, "service_request_recovery_enabled", recovery_ready)
    config = _contractor(
        service_request_command_service=service,
        gated_actions={"google_create_event": action_flag},
        automation_approvals={"google_create_event": automation_approval},
        service_request_mutations_enabled=tenant_flag,
        integration_write_status=write_status,
    )
    start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
    arguments = {
        "title": "Synthetic estimate",
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=1)).isoformat(),
    }

    result = await _session(engine, config)("book_appointment", arguments)

    if books:
        assert result == {"success": True, "revision": 1}
        provider.create.assert_awaited_once()
        save_call.assert_not_awaited()
    else:
        assert result["status"] == "request_recorded"
        assert result["booked"] is False
        provider.create.assert_not_awaited()
        save_call.assert_awaited_once()
        assert save_call.await_args.args[0] == "CA-synthetic-a"
        request = save_call.await_args.args[1]["appointment_request"]
        assert request["status"] == "pending_owner_confirmation"
        assert request["start_time"] == start.isoformat()


@pytest.mark.asyncio
async def test_null_calendar_arguments_preserve_transport_normalization(engine, monkeypatch):
    slots = AsyncMock(return_value=[])
    monkeypatch.setattr("app.services.calendar.get_available_slots", slots)

    result = await _session(engine, _contractor())("check_availability", None)

    if engine == "relay":
        assert result == {"available_slots": [], "days_checked": 7}
        slots.assert_awaited_once()
    else:
        assert result["success"] is False
        assert result["error"] == "unavailable"
        assert "Do not retry" in result["instruction"]
        slots.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_operation_id_preserves_transport_normalization(engine):
    cancel = AsyncMock(return_value={"status": "applied", "revision": 2})
    config = _contractor(
        service_request_command_service=SimpleNamespace(cancel_service_request=cancel),
    )

    result = await _session(engine, config)(
        CANCEL_SERVICE_REQUEST, {"request_id": "request-1", "expected_revision": 1}, 0,
    )

    if engine == "relay":
        assert result["confirmed"] is True
        cancel.assert_awaited_once()
        assert cancel.await_args.kwargs["idempotency_key"] == "CA-synthetic-a:0"
    else:
        assert result["confirmed"] is False
        assert result["status"] == "invalid_operation"
        cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_calendar_timeout_cancels_provider_work_before_returning(engine, monkeypatch):
    cancelled = asyncio.Event()

    async def blocked_slots(*_args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("app.services.calendar.get_available_slots", blocked_slots)
    monkeypatch.setattr(voice_pipeline, "GOOGLE_CALENDAR_TOOL_TIMEOUT_SECONDS", 0.01)

    result = await _session(engine, _contractor())("check_availability", {})

    assert result == {"error": "Request timed out"}
    assert cancelled.is_set()
