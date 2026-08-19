"""Focused tests for the Google Calendar service-request provider adapter."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.services import google_calendar_request_provider as provider_module
from app.services.google_calendar_request_provider import (
    GOOGLE_CALENDAR_PROVIDER_KIND,
    MAX_PROVIDER_METADATA_VALUE_BYTES,
    GoogleCalendarRequestProvider,
)

START = datetime(2026, 8, 14, 13, 0, tzinfo=UTC)
END = START + timedelta(hours=1)


@dataclass(frozen=True)
class _Binding:
    kind: str = GOOGLE_CALENDAR_PROVIDER_KIND
    resource_id: str = "provider/event id"


@dataclass(frozen=True)
class _Request:
    scheduled_start: datetime | str = START
    scheduled_end: datetime | str = END
    services: tuple[str, ...] = ("Furnace tune-up",)


@pytest.mark.asyncio
async def test_create_uses_bound_id_proposed_schedule_and_logical_marker(monkeypatch):
    contractor = {"contractor_id": "contractor-1"}
    calls = []

    async def fake_create(received_contractor, **kwargs):
        calls.append((received_contractor, kwargs))
        return True

    monkeypatch.setattr(
        provider_module.calendar,
        "create_managed_appointment",
        fake_create,
    )
    operation_id = "a" * 64

    created = await GoogleCalendarRequestProvider(contractor).create(
        binding=_Binding(),
        request=_Request(),
        title=" Furnace\n tune-up ",
        description=" Caller\t notes ",
        idempotency_key=operation_id,
    )

    assert created is True
    assert calls == [
        (
            contractor,
            {
                "event_id": "provider/event id",
                "title": "Furnace tune-up",
                "start_time": START.isoformat(),
                "end_time": END.isoformat(),
                "description": "Caller notes",
                "logical_operation_id": operation_id,
            },
        )
    ]


@pytest.mark.asyncio
async def test_create_rejects_transport_id_before_provider_call(monkeypatch):
    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(
        provider_module.calendar,
        "create_managed_appointment",
        unexpected_call,
    )

    created = await GoogleCalendarRequestProvider({}).create(
        binding=_Binding(),
        request=_Request(),
        title="Furnace tune-up",
        description="",
        idempotency_key="ephemeral-tool-call-id",
    )

    assert created is False


@pytest.mark.asyncio
async def test_cancel_uses_injected_contractor_and_bound_resource(monkeypatch):
    contractor = {"contractor_id": "contractor-1", "google_calendar_access_token": "token"}
    calls = []

    async def fake_cancel(received_contractor, event_id):
        calls.append((received_contractor, event_id))
        received_contractor["google_calendar_access_token"] = "refreshed-token"
        return True

    monkeypatch.setattr(provider_module.calendar, "cancel_appointment", fake_cancel)
    adapter = GoogleCalendarRequestProvider(contractor)

    assert (
        await adapter.cancel(
            binding=_Binding(),
            request=_Request(),
            idempotency_key="cancel-1",
        )
        is True
    )
    assert calls == [(contractor, "provider/event id")]
    assert contractor["google_calendar_access_token"] == "refreshed-token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding",
    [
        _Binding(kind="jobber"),
        _Binding(resource_id=""),
        _Binding(resource_id="event\nprivate"),
    ],
)
async def test_wrong_or_invalid_binding_fails_without_provider_call(monkeypatch, binding):
    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(provider_module.calendar, "cancel_appointment", unexpected_call)

    result = await GoogleCalendarRequestProvider({}).cancel(
        binding=binding,
        request=_Request(),
        idempotency_key="cancel-1",
    )

    assert result is False


@pytest.mark.asyncio
async def test_reschedule_sends_normalized_schedule_without_overwriting_metadata(monkeypatch):
    calls = []

    async def fake_update(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(provider_module.calendar, "update_appointment", fake_update)
    contractor = {"contractor_id": "contractor-1"}
    adapter = GoogleCalendarRequestProvider(contractor)

    result = await adapter.reschedule(
        binding=_Binding(),
        request=_Request(),
        scheduled_start="2026-08-20T09:00:00-04:00",
        scheduled_end="2026-08-20T10:30:00-04:00",
        idempotency_key="reschedule-1",
    )

    assert result is True
    assert calls == [
        (
            (
                contractor,
                "provider/event id",
                "2026-08-20T09:00:00-04:00",
                "2026-08-20T10:30:00-04:00",
            ),
            {},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("not-a-date", "2026-08-20T10:30:00-04:00"),
        ("2026-08-20T09:00:00", "2026-08-20T10:30:00"),
        ("2026-08-20T11:00:00-04:00", "2026-08-20T10:30:00-04:00"),
    ],
)
async def test_reschedule_rejects_invalid_schedule_without_provider_call(
    monkeypatch,
    start,
    end,
):
    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(provider_module.calendar, "update_appointment", unexpected_call)

    result = await GoogleCalendarRequestProvider({}).reschedule(
        binding=_Binding(),
        request=_Request(),
        scheduled_start=start,
        scheduled_end=end,
        idempotency_key="reschedule-1",
    )

    assert result is False


@pytest.mark.asyncio
async def test_add_service_updates_only_bounded_private_metadata(monkeypatch):
    calls = []

    async def fake_update(*args):
        assert len(args) == 3
        calls.append(args)
        return True

    monkeypatch.setattr(
        provider_module.calendar,
        "set_appointment_service_metadata",
        fake_update,
    )
    contractor = {"contractor_id": "contractor-1"}
    request = _Request(
        services=(
            "Furnace tune-up",
            "  Filter\nreplacement  ",
            "Drain cleaning",
        )
    )

    result = await GoogleCalendarRequestProvider(contractor).add_service(
        binding=_Binding(),
        request=request,
        service="Drain cleaning",
        idempotency_key="add-service-1",
    )

    assert result is True
    assert len(calls) == 1
    args = calls[0]
    assert args[:2] == (contractor, "provider/event id")
    assert json.loads(args[2]) == {
        "v": 1,
        "services": [
            "Furnace tune-up",
            "Filter replacement",
            "Drain cleaning",
        ],
    }


@pytest.mark.asyncio
async def test_add_service_metadata_is_bounded_and_always_includes_addition(monkeypatch):
    metadata_values = []

    async def fake_update(_contractor, _resource_id, metadata_value):
        metadata_values.append(metadata_value)
        return True

    monkeypatch.setattr(
        provider_module.calendar,
        "set_appointment_service_metadata",
        fake_update,
    )
    request = _Request(
        services=tuple(f"Existing service {index} " + "x" * 120 for index in range(30))
    )

    result = await GoogleCalendarRequestProvider({}).add_service(
        binding=_Binding(),
        request=request,
        service="Caller confirmed addition",
        idempotency_key="add-service-1",
    )

    assert result is True
    assert len(metadata_values[0].encode("utf-8")) <= MAX_PROVIDER_METADATA_VALUE_BYTES
    assert json.loads(metadata_values[0])["services"][-1] == "Caller confirmed addition"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_result", [False, None, "truthy-but-not-boolean"])
async def test_only_literal_true_is_reported_as_success(monkeypatch, provider_result):
    async def fake_cancel(*_args, **_kwargs):
        return provider_result

    monkeypatch.setattr(provider_module.calendar, "cancel_appointment", fake_cancel)

    result = await GoogleCalendarRequestProvider({}).cancel(
        binding=_Binding(),
        request=_Request(),
        idempotency_key="cancel-1",
    )

    assert result is False


@pytest.mark.asyncio
async def test_provider_exception_returns_false_without_logging_ids_or_payloads(
    monkeypatch,
    caplog,
):
    private_data = "provider/event id Jonathan at 123 Secret Lane"

    async def failing_cancel(*_args, **_kwargs):
        raise RuntimeError(private_data)

    monkeypatch.setattr(provider_module.calendar, "cancel_appointment", failing_cancel)

    result = await GoogleCalendarRequestProvider({}).cancel(
        binding=_Binding(),
        request=_Request(),
        idempotency_key="cancel-1",
    )

    assert result is False
    assert "exception_type=RuntimeError" in caplog.text
    assert private_data not in caplog.text
    assert "provider/event id" not in caplog.text


def test_contractor_config_must_be_injected_as_a_dictionary():
    with pytest.raises(TypeError, match="contractor_config"):
        GoogleCalendarRequestProvider(None)
