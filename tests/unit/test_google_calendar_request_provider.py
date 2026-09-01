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
async def test_reschedule_sends_base_and_desired_schedules(monkeypatch):
    calls = []

    async def fake_reschedule(
        contractor, event_id, *, base_start, base_end, desired_start, desired_end, logical_operation_id=None
    ):
        calls.append(
            (contractor, event_id, base_start, base_end, desired_start, desired_end, logical_operation_id)
        )
        return True

    monkeypatch.setattr(
        provider_module.calendar, "reschedule_appointment", fake_reschedule
    )
    contractor = {"contractor_id": "contractor-1"}
    adapter = GoogleCalendarRequestProvider(contractor)
    op_id = "a" * 64

    result = await adapter.reschedule(
        binding=_Binding(),
        request=_Request(),
        scheduled_start="2026-08-20T09:00:00-04:00",
        scheduled_end="2026-08-20T10:30:00-04:00",
        idempotency_key=op_id,
    )

    assert result is True
    assert calls == [
        (
            contractor,
            "provider/event id",
            START.isoformat(),
            END.isoformat(),
            "2026-08-20T09:00:00-04:00",
            "2026-08-20T10:30:00-04:00",
            op_id,
        )
    ]


@pytest.mark.asyncio
async def test_reschedule_rejects_invalid_logical_operation_id_without_provider_call(monkeypatch):
    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(
        provider_module.calendar, "reschedule_appointment", unexpected_call
    )

    result = await GoogleCalendarRequestProvider({}).reschedule(
        binding=_Binding(),
        request=_Request(),
        scheduled_start="2026-08-20T09:00:00-04:00",
        scheduled_end="2026-08-20T10:30:00-04:00",
        idempotency_key="non-64-hex-id",
    )

    assert result is False


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

    monkeypatch.setattr(
        provider_module.calendar, "reschedule_appointment", unexpected_call
    )

    result = await GoogleCalendarRequestProvider({}).reschedule(
        binding=_Binding(),
        request=_Request(),
        scheduled_start=start,
        scheduled_end=end,
        idempotency_key="b" * 64,
    )

    assert result is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_start", "base_end"),
    [
        ("not-a-date", "2026-08-14T14:00:00Z"),
        ("2026-08-14T13:00:00", "2026-08-14T14:00:00"),
        ("2026-08-14T15:00:00Z", "2026-08-14T14:00:00Z"),
    ],
)
async def test_reschedule_rejects_invalid_base_schedule_without_provider_call(
    monkeypatch,
    base_start,
    base_end,
):
    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(
        provider_module.calendar, "reschedule_appointment", unexpected_call
    )

    result = await GoogleCalendarRequestProvider({}).reschedule(
        binding=_Binding(),
        request=_Request(scheduled_start=base_start, scheduled_end=base_end),
        scheduled_start="2026-08-20T09:00:00-04:00",
        scheduled_end="2026-08-20T10:30:00-04:00",
        idempotency_key="c" * 64,
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


@pytest.mark.asyncio
async def test_reschedule_adapter_does_not_log_any_private_sentinel(monkeypatch, caplog):
    """B1 adapter privacy: every asserted sentinel flows through the executed path.

    Sentinels that flow as forwarded arguments (mutation-effective via spy validation):
      access token, refresh token, contractor ID, event ID, base schedule (start/end),
      desired schedule (start/end), idempotency key.

    Sentinels that flow through the exception message raised by the spy after argument
    validation (causal for log-message leakage — would appear if adapter logged the
    exception message or request object):
      full event URL, ETag, customer ID/key, request ID, summary, description,
      provider body, exception text.

    Every sentinel must be individually absent from logs while coarse
    operation=reschedule and exception_type=RuntimeError are present.
    """
    from dataclasses import dataclass as _dc
    from app.services import calendar as _calendar_module

    # --- Unique sentinels -------------------------------------------------------
    _s_access_token  = "SENTINEL_ACCESS_TOKEN_RESCHEDULE_7710"
    _s_refresh_token = "SENTINEL_REFRESH_TOKEN_RESCHEDULE_7720"
    _s_contractor_id = "sentinel-contractor-id-7730"
    _s_customer_id   = "SENTINEL_CUSTOMER_ID_7740"
    _s_customer_key  = "SENTINEL_CUSTOMER_KEY_7750"
    _s_request_id    = "SENTINEL_REQUEST_ID_7760"
    _s_idempotency_id = "f" * 64
    _s_event_id      = "sentinel-event-id-7780"
    _s_event_url     = f"{_calendar_module.EVENTS_URL}/{_s_event_id}"
    _s_etag          = '"SENTINEL_ETAG_VALUE_7790"'
    # Unique ISO strings for base schedule — distinct from shared START/END constants.
    _s_base_start    = "2026-09-01T09:17:00-04:00"
    _s_base_end      = "2026-09-01T10:17:00-04:00"
    _s_desired_start = "2026-09-02T11:44:00-04:00"
    _s_desired_end   = "2026-09-02T12:44:00-04:00"
    _s_summary       = "SENTINEL_EVENT_SUMMARY_7800"
    _s_description   = "SENTINEL_EVENT_DESCRIPTION_7810"
    _s_provider_body = "SENTINEL_PROVIDER_RESPONSE_BODY_7820"
    _s_exc_text      = "SENTINEL_EXCEPTION_MESSAGE_7830"

    # --- Request carries unique base-schedule sentinels as ISO strings -----------
    # _request_schedule() calls _aware_datetime() which accepts ISO string input,
    # so the adapter extracts these values and forwards them to calendar.reschedule_appointment.
    @_dc(frozen=True)
    class _SentinelRequest:
        scheduled_start: str = _s_base_start
        scheduled_end: str = _s_base_end
        services: tuple[str, ...] = ("Furnace tune-up",)
        # Extra fields the adapter never reads — present so logging the whole object leaks data.
        customer_id: str = _s_customer_id
        customer_key: str = _s_customer_key
        request_id: str = _s_request_id

    # --- Binding with sentinel event ID -----------------------------------------
    @_dc(frozen=True)
    class _SentinelBinding:
        kind: str = GOOGLE_CALENDAR_PROVIDER_KIND
        resource_id: str = _s_event_id

    # --- Contractor config with sentinel tokens ---------------------------------
    contractor_cfg = {
        "contractor_id": _s_contractor_id,
        "google_calendar_access_token": _s_access_token,
        "google_calendar_refresh_token": _s_refresh_token,
    }

    # --- Spy: validate real forwarded args, then raise with all remaining sentinels
    received: list[dict] = []

    async def _spy_reschedule(
        contractor,
        event_id,
        *,
        base_start,
        base_end,
        desired_start,
        desired_end,
        logical_operation_id=None,
    ):
        received.append(
            dict(
                contractor=contractor,
                event_id=event_id,
                base_start=base_start,
                base_end=base_end,
                desired_start=desired_start,
                desired_end=desired_end,
                logical_operation_id=logical_operation_id,
            )
        )
        # After all arguments are validated, raise an exception whose message contains
        # every remaining sentinel (event URL, ETag, customer ID/key, request ID,
        # summary, description, provider body, exc text).  The adapter catches Exception
        # and logs only type(error).__name__ — so if it ever leaked the message all these
        # would appear in logs, making each absent-from-log assertion causal.
        raise RuntimeError(
            f"{_s_event_url} etag={_s_etag} cid={_s_customer_id} "
            f"ckey={_s_customer_key} rid={_s_request_id} "
            f"summary={_s_summary} desc={_s_description} "
            f"body={_s_provider_body} {_s_exc_text}"
        )

    monkeypatch.setattr(provider_module.calendar, "reschedule_appointment", _spy_reschedule)

    adapter = GoogleCalendarRequestProvider(contractor_cfg)

    with caplog.at_level("DEBUG"):
        result = await adapter.reschedule(
            binding=_SentinelBinding(),
            request=_SentinelRequest(),
            scheduled_start=_s_desired_start,
            scheduled_end=_s_desired_end,
            idempotency_key=_s_idempotency_id,
        )

    # Adapter must return False (underlying call raised).
    assert result is False

    # Spy must have been called exactly once with the correct forwarded arguments.
    assert len(received) == 1
    assert received[0]["event_id"] == _s_event_id
    assert received[0]["contractor"] is contractor_cfg
    # Base schedule: adapter reads _SentinelRequest.scheduled_start/end and forwards them.
    assert received[0]["base_start"] == _s_base_start
    assert received[0]["base_end"] == _s_base_end
    # Desired schedule: passed directly via adapter.reschedule(scheduled_start/end=...).
    assert received[0]["desired_start"] == _s_desired_start
    assert received[0]["desired_end"] == _s_desired_end
    assert received[0]["logical_operation_id"] == _s_idempotency_id

    # Coarse operation label and exception_type must be present.
    assert "operation=reschedule" in caplog.text
    assert "exception_type=RuntimeError" in caplog.text

    # Every private sentinel must be individually absent from all logs.
    _all_sentinels = [
        _s_access_token,
        _s_refresh_token,
        _s_contractor_id,
        _s_customer_id,
        _s_customer_key,
        _s_request_id,
        _s_idempotency_id,
        _s_event_id,
        _s_event_url,
        _s_etag,
        _s_base_start,
        _s_base_end,
        _s_desired_start,
        _s_desired_end,
        _s_summary,
        _s_description,
        _s_provider_body,
        _s_exc_text,
    ]
    for sentinel in _all_sentinels:
        assert sentinel not in caplog.text, f"Private sentinel leaked in logs: {sentinel!r}"


@pytest.mark.asyncio
async def test_reconcile_reschedule_forwards_to_calendar_and_handles_result(monkeypatch):
    contractor = {"contractor_id": "c1"}
    adapter = GoogleCalendarRequestProvider(contractor)
    op_id = "d" * 64

    async def fake_reconcile(
        contractor_arg,
        event_id,
        *,
        desired_start,
        desired_end,
        logical_operation_id,
    ):
        assert contractor_arg is contractor
        assert event_id == "provider/event id"
        assert logical_operation_id == op_id
        return provider_module.calendar.CalendarReconciliationResult(
            has_matching_claim=True,
            confirmed=True,
            claim_id="claim-123",
            logical_operation_id=op_id,
            authorization_status="matching_claim",
        )

    monkeypatch.setattr(provider_module.calendar, "reconcile_reschedule_appointment", fake_reconcile)

    res = await adapter.reconcile_reschedule(
        binding=_Binding(),
        request=_Request(),
        scheduled_start=START,
        scheduled_end=END,
        logical_operation_id=op_id,
    )
    assert res.has_matching_claim is True
    assert res.confirmed is True
    assert res.claim_id == "claim-123"
    assert res.logical_operation_id == op_id
    assert res.authorization_status == "matching_claim"


@pytest.mark.asyncio
async def test_clear_reconciled_claim_forwards_to_calendar(monkeypatch):
    contractor = {"contractor_id": "c1"}
    adapter = GoogleCalendarRequestProvider(contractor)
    op_id = "e" * 64

    async def fake_clear(contractor_arg, *, claim_id, logical_operation_id):
        assert contractor_arg is contractor
        assert claim_id == "claim-123"
        assert logical_operation_id == op_id
        return True

    monkeypatch.setattr(provider_module.calendar, "clear_reconciled_reschedule_claim", fake_clear)

    assert await adapter.clear_reconciled_claim(claim_id="claim-123", logical_operation_id=op_id) is True
    assert await adapter.clear_reconciled_claim(claim_id="claim-123", logical_operation_id="invalid") is False
