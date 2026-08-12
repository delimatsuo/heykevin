"""Focused tests for shared receptionist tool contracts and execution."""

from dataclasses import dataclass
from enum import Enum

import pytest

from app.services.receptionist_tools import (
    ADD_SERVICE_TO_REQUEST,
    CANCEL_SERVICE_REQUEST,
    RECEPTIONIST_TOOL_CONTRACTS,
    RESCHEDULE_SERVICE_REQUEST,
    ReceptionistToolExecutor,
    anthropic_tool_declarations,
    gemini_tool_declarations,
)


class _Status(str, Enum):
    APPLIED = "applied"


@dataclass
class _ObjectResult:
    status: _Status
    revision: int


class _CommandService:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result or {"status": "applied", "revision": 4}
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def _record(self, name: str, kwargs: dict):
        self.calls.append((name, kwargs))
        if self.error:
            raise self.error
        return self.result

    async def cancel_service_request(self, **kwargs):
        return await self._record(CANCEL_SERVICE_REQUEST, kwargs)

    async def reschedule_service_request(self, **kwargs):
        return await self._record(RESCHEDULE_SERVICE_REQUEST, kwargs)

    async def add_service_to_request(self, **kwargs):
        return await self._record(ADD_SERVICE_TO_REQUEST, kwargs)


def _executor(service: _CommandService) -> ReceptionistToolExecutor:
    return ReceptionistToolExecutor(
        service,
        contractor_id="contractor-trusted",
        caller_phone="+16175550123",
    )


def _common_arguments(**overrides):
    arguments = {
        "request_id": "request-1",
        "expected_revision": 3,
    }
    arguments.update(overrides)
    return arguments


def test_provider_declarations_derive_from_one_contract_without_identity_fields():
    gemini = gemini_tool_declarations()
    anthropic = anthropic_tool_declarations()

    assert [tool["name"] for tool in gemini] == [
        contract.name for contract in RECEPTIONIST_TOOL_CONTRACTS
    ]
    assert [tool["name"] for tool in anthropic] == [tool["name"] for tool in gemini]
    for gemini_tool, anthropic_tool in zip(gemini, anthropic, strict=True):
        assert gemini_tool["parameters"] == anthropic_tool["input_schema"]
        properties = gemini_tool["parameters"]["properties"]
        assert "contractor_id" not in properties
        assert "caller_phone" not in properties
        assert "idempotency_key" not in properties
        assert gemini_tool["parameters"]["additionalProperties"] is False

    gemini[0]["parameters"]["properties"].clear()
    assert anthropic_tool_declarations()[0]["input_schema"]["properties"]


@pytest.mark.asyncio
async def test_cancel_binds_trusted_identity_and_ignores_model_identity_overrides():
    service = _CommandService(result=_ObjectResult(_Status.APPLIED, revision=4))

    result = await _executor(service).execute(
        CANCEL_SERVICE_REQUEST,
        _common_arguments(
            contractor_id="attacker-tenant",
            caller_phone="+19999999999",
        ),
        operation_id="call-1:cancel:1",
    )

    assert service.calls == [
        (
            CANCEL_SERVICE_REQUEST,
            {
                "contractor_id": "contractor-trusted",
                "caller_phone": "+16175550123",
                "request_id": "request-1",
                "expected_revision": 3,
                "idempotency_key": "call-1:cancel:1",
            },
        )
    ]
    assert result == {
        "status": "applied",
        "success": True,
        "confirmed": True,
        "message": "The service request was cancelled and the change is confirmed.",
        "request_id": "request-1",
        "revision": 4,
    }


@pytest.mark.asyncio
async def test_reschedule_passes_only_validated_action_fields():
    service = _CommandService()

    result = await _executor(service).execute(
        RESCHEDULE_SERVICE_REQUEST,
        _common_arguments(
            scheduled_start="2026-08-14T09:00:00-04:00",
            scheduled_end="2026-08-14T10:00:00-04:00",
            untrusted_extra="do not forward",
        ),
        operation_id="call-1:reschedule:1",
    )

    assert result["success"] is True
    assert service.calls[0][1] == {
        "contractor_id": "contractor-trusted",
        "caller_phone": "+16175550123",
        "request_id": "request-1",
        "expected_revision": 3,
        "idempotency_key": "call-1:reschedule:1",
        "scheduled_start": "2026-08-14T09:00:00-04:00",
        "scheduled_end": "2026-08-14T10:00:00-04:00",
    }


@pytest.mark.asyncio
async def test_add_service_passes_only_validated_action_fields():
    service = _CommandService()

    result = await _executor(service).execute(
        ADD_SERVICE_TO_REQUEST,
        _common_arguments(service=" Drain cleaning "),
        operation_id="call-1:add:1",
    )

    assert result["success"] is True
    assert service.calls[0][1]["service"] == "Drain cleaning"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message_fragment"),
    [
        ("pending_provider", "not yet confirmed"),
        ("conflict", "Nothing was changed"),
        ("not_found", "which appointment or request"),
        ("failed", "try again or have the business follow up"),
        ("provider_leaked_private_error", "try again or have the business follow up"),
    ],
)
async def test_only_applied_is_success_and_failure_messages_are_truthful(
    status,
    message_fragment,
):
    service = _CommandService(result={"status": status, "revision": 4})

    result = await _executor(service).execute(
        CANCEL_SERVICE_REQUEST,
        _common_arguments(),
        operation_id="call-1:status:1",
    )

    assert result["success"] is False
    assert result["confirmed"] is False
    assert message_fragment in result["message"]
    assert "provider_leaked_private_error" not in str(result)
    if status == "provider_leaked_private_error":
        assert result["status"] == "failed"
    else:
        assert result["status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"request_id": ""},
        {"expected_revision": 0},
        {"expected_revision": True},
        {"expected_revision": "3"},
    ],
)
async def test_common_command_fields_are_validated_before_dispatch(overrides):
    service = _CommandService()

    result = await _executor(service).execute(
        CANCEL_SERVICE_REQUEST,
        _common_arguments(**overrides),
        operation_id="call-1:invalid:1",
    )

    assert result["status"] == "invalid_arguments"
    assert result["success"] is False
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            RESCHEDULE_SERVICE_REQUEST,
            _common_arguments(scheduled_start="", scheduled_end="2026-08-14T10:00:00Z"),
        ),
        (ADD_SERVICE_TO_REQUEST, _common_arguments(service="  ")),
    ],
)
async def test_action_specific_fields_are_validated_before_dispatch(tool_name, arguments):
    service = _CommandService()

    result = await _executor(service).execute(
        tool_name,
        arguments,
        operation_id="call-1:invalid-action:1",
    )

    assert result["status"] == "invalid_arguments"
    assert service.calls == []


@pytest.mark.asyncio
async def test_command_exception_becomes_closed_safe_failure():
    private_error = "provider body: Jonathan at 123 Secret Lane"
    service = _CommandService(error=RuntimeError(private_error))

    result = await _executor(service).execute(
        CANCEL_SERVICE_REQUEST,
        _common_arguments(),
        operation_id="call-1:error:1",
    )

    assert result["status"] == "failed"
    assert result["success"] is False
    assert private_error not in str(result)


@pytest.mark.asyncio
async def test_unknown_tool_is_not_dispatched():
    service = _CommandService()

    result = await _executor(service).execute("delete_everything", _common_arguments())

    assert result["status"] == "unsupported_tool"
    assert service.calls == []


@pytest.mark.asyncio
async def test_model_idempotency_is_ignored_and_transport_operation_is_required():
    service = _CommandService()

    result = await _executor(service).execute(
        CANCEL_SERVICE_REQUEST,
        _common_arguments(idempotency_key="model-controlled"),
    )

    assert result["status"] == "invalid_operation"
    assert service.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("contractor_id", ""), ("caller_phone", "  ")],
)
def test_executor_requires_server_bound_identity(field, value):
    kwargs = {
        "contractor_id": "contractor-trusted",
        "caller_phone": "+16175550123",
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=field):
        ReceptionistToolExecutor(_CommandService(), **kwargs)
