"""Model-neutral receptionist tools for changing existing service requests.

The tool surface deliberately excludes tenant and caller identity.  Those values are
bound to :class:`ReceptionistToolExecutor` by trusted call setup code and injected into
every command invocation server-side.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

MAX_REQUEST_ID_LENGTH = 160
MAX_IDEMPOTENCY_KEY_LENGTH = 200
MAX_SERVICE_LENGTH = 120

CANCEL_SERVICE_REQUEST = "cancel_service_request"
RESCHEDULE_SERVICE_REQUEST = "reschedule_service_request"
ADD_SERVICE_TO_REQUEST = "add_service_to_request"
RECEPTIONIST_TOOL_NAMES = frozenset(
    {
        CANCEL_SERVICE_REQUEST,
        RESCHEDULE_SERVICE_REQUEST,
        ADD_SERVICE_TO_REQUEST,
    }
)

_RESULT_STATUSES = frozenset({"applied", "pending_provider", "conflict", "not_found", "failed"})


class ServiceRequestCommandService(Protocol):
    """Structural interface supplied by the service-request application layer."""

    def cancel_service_request(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> Awaitable[Any]: ...

    def reschedule_service_request(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
        expected_revision: int,
        idempotency_key: str,
        scheduled_start: str,
        scheduled_end: str,
    ) -> Awaitable[Any]: ...

    def add_service_to_request(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
        expected_revision: int,
        idempotency_key: str,
        service: str,
    ) -> Awaitable[Any]: ...


@dataclass(frozen=True)
class ToolContract:
    """One canonical tool contract rendered for multiple model providers."""

    name: str
    description: str
    input_schema: dict[str, Any]


def _base_properties() -> dict[str, Any]:
    return {
        "request_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_REQUEST_ID_LENGTH,
            "description": "ID of the caller's current service request. Do not guess it.",
        },
        "expected_revision": {
            "type": "integer",
            "minimum": 1,
            "description": "Current request revision from trusted customer context.",
        },
    }


def _object_schema(
    extra_properties: Mapping[str, Any] | None = None,
    *,
    extra_required: tuple[str, ...] = (),
) -> dict[str, Any]:
    properties = _base_properties()
    if extra_properties:
        properties.update(deepcopy(dict(extra_properties)))
    return {
        "type": "object",
        "properties": properties,
        "required": [
            "request_id",
            "expected_revision",
            *extra_required,
        ],
        "additionalProperties": False,
    }


RECEPTIONIST_TOOL_CONTRACTS: tuple[ToolContract, ...] = (
    ToolContract(
        name=CANCEL_SERVICE_REQUEST,
        description=(
            "Cancel an existing service request after the caller clearly asks to cancel it. "
            "Report success only when the tool result says the change was applied."
        ),
        input_schema=_object_schema(),
    ),
    ToolContract(
        name=RESCHEDULE_SERVICE_REQUEST,
        description=(
            "Move an existing service request to a caller-confirmed date and time. "
            "Report success only when the tool result says the change was applied."
        ),
        input_schema=_object_schema(
            {
                "scheduled_start": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Caller-confirmed new start time in ISO 8601 format.",
                },
                "scheduled_end": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Caller-confirmed new end time in ISO 8601 format.",
                },
            },
            extra_required=("scheduled_start", "scheduled_end"),
        ),
    ),
    ToolContract(
        name=ADD_SERVICE_TO_REQUEST,
        description=(
            "Add one caller-requested service to an existing service request. "
            "Report success only when the tool result says the change was applied."
        ),
        input_schema=_object_schema(
            {
                "service": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_SERVICE_LENGTH,
                    "description": "The additional service requested by the caller.",
                }
            },
            extra_required=("service",),
        ),
    ),
)


def anthropic_tool_declarations() -> list[dict[str, Any]]:
    """Render canonical contracts in Anthropic's tool declaration format."""

    return [
        {
            "name": contract.name,
            "description": contract.description,
            "input_schema": deepcopy(contract.input_schema),
        }
        for contract in RECEPTIONIST_TOOL_CONTRACTS
    ]


def gemini_tool_declarations() -> list[dict[str, Any]]:
    """Render canonical contracts in Gemini's function-declaration format."""

    return [
        {
            "name": contract.name,
            "description": contract.description,
            "parameters": deepcopy(contract.input_schema),
        }
        for contract in RECEPTIONIST_TOOL_CONTRACTS
    ]


class ReceptionistToolExecutor:
    """Execute receptionist tools under a server-bound tenant and caller identity."""

    def __init__(
        self,
        command_service: ServiceRequestCommandService,
        *,
        contractor_id: str,
        caller_phone: str,
    ) -> None:
        self._command_service = command_service
        self._contractor_id = _require_bound_identity(contractor_id, "contractor_id")
        self._caller_phone = _require_bound_identity(caller_phone, "caller_phone")

    async def execute(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        *,
        operation_id: str = "",
    ) -> dict[str, Any]:
        """Validate and dispatch a model tool call, returning caller-safe truth."""

        if tool_name not in RECEPTIONIST_TOOL_NAMES:
            return _tool_error(
                "unsupported_tool",
                "That request change is not available. Ask the caller what they would like to do.",
            )
        if not isinstance(arguments, Mapping):
            return _tool_error(
                "invalid_arguments",
                "I need the current request details before making that change. Ask the caller "
                "which request they mean.",
            )

        validated = _validate_common_arguments(arguments)
        if isinstance(validated, dict) and "status" in validated:
            return validated
        request_id, expected_revision = validated
        idempotency_key = _required_text(
            operation_id,
            max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
        )
        if idempotency_key is None:
            return _tool_error(
                "invalid_operation",
                "I couldn't safely identify that change. Ask the caller to try again.",
            )

        kwargs: dict[str, Any] = {
            # Trusted values override and never read model-supplied identity fields.
            "contractor_id": self._contractor_id,
            "caller_phone": self._caller_phone,
            "request_id": request_id,
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
        }

        if tool_name == RESCHEDULE_SERVICE_REQUEST:
            scheduled_start = _required_text(arguments.get("scheduled_start"), max_length=80)
            scheduled_end = _required_text(arguments.get("scheduled_end"), max_length=80)
            if scheduled_start is None or scheduled_end is None:
                return _tool_error(
                    "invalid_arguments",
                    "I need a valid new start and end time. Ask the caller to confirm the date "
                    "and time.",
                )
            kwargs.update(
                scheduled_start=scheduled_start,
                scheduled_end=scheduled_end,
            )
        elif tool_name == ADD_SERVICE_TO_REQUEST:
            service = _required_text(arguments.get("service"), max_length=MAX_SERVICE_LENGTH)
            if service is None:
                return _tool_error(
                    "invalid_arguments",
                    "I need the service to add. Ask the caller which additional service they want.",
                )
            kwargs["service"] = service

        method = getattr(self._command_service, tool_name, None)
        if not callable(method):
            return _result_message(tool_name, "failed", request_id=request_id)

        try:
            result = await method(**kwargs)
        except Exception:  # noqa: BLE001 - the model receives a closed, detail-free failure
            return _result_message(tool_name, "failed", request_id=request_id)
        return _map_command_result(tool_name, result, request_id=request_id)


def _require_bound_identity(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty server-bound string")
    return value.strip()


def _required_text(value: Any, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        return None
    return normalized


def _validate_common_arguments(
    arguments: Mapping[str, Any],
) -> tuple[str, int] | dict[str, Any]:
    request_id = _required_text(
        arguments.get("request_id"),
        max_length=MAX_REQUEST_ID_LENGTH,
    )
    if request_id is None:
        return _tool_error(
            "invalid_arguments",
            "I need a valid current request. Ask the caller which request they mean.",
        )

    revision = arguments.get("expected_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        return _tool_error(
            "invalid_arguments",
            "The request changed or its current version is unavailable. Refresh the current "
            "request before trying again.",
        )

    return request_id, revision


def _result_status(result: Any) -> str:
    if isinstance(result, Mapping):
        raw_status = result.get("status")
    else:
        raw_status = getattr(result, "status", None)
    if isinstance(raw_status, Enum):
        raw_status = raw_status.value
    if not isinstance(raw_status, str):
        return "failed"
    normalized = raw_status.strip().lower()
    return normalized if normalized in _RESULT_STATUSES else "failed"


def _result_revision(result: Any) -> int | None:
    if isinstance(result, Mapping):
        revision = result.get("revision")
    else:
        revision = getattr(result, "revision", None)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        return None
    return revision


def _map_command_result(
    tool_name: str,
    result: Any,
    *,
    request_id: str,
) -> dict[str, Any]:
    return _result_message(
        tool_name,
        _result_status(result),
        request_id=request_id,
        revision=_result_revision(result),
    )


def _result_message(
    tool_name: str,
    status: str,
    *,
    request_id: str,
    revision: int | None = None,
) -> dict[str, Any]:
    action = {
        CANCEL_SERVICE_REQUEST: "cancelled",
        RESCHEDULE_SERVICE_REQUEST: "rescheduled",
        ADD_SERVICE_TO_REQUEST: "updated with the additional service",
    }[tool_name]
    messages = {
        "applied": f"The service request was {action} and the change is confirmed.",
        "pending_provider": (
            "The change was recorded, but the scheduling provider has not confirmed it yet. "
            "Tell the caller it is not yet confirmed."
        ),
        "conflict": (
            "The service request changed before this update could be applied. Nothing was "
            "changed; refresh it and ask the caller to confirm the latest details."
        ),
        "not_found": (
            "I couldn't find that service request for this caller. Ask which appointment or "
            "request they mean."
        ),
        "failed": (
            "I couldn't complete or confirm that change. Apologize and ask whether the caller "
            "wants to try again or have the business follow up."
        ),
    }
    safe_status = status if status in _RESULT_STATUSES else "failed"
    response: dict[str, Any] = {
        "status": safe_status,
        "success": safe_status == "applied",
        "confirmed": safe_status == "applied",
        "message": messages[safe_status],
        "request_id": request_id,
    }
    if revision is not None:
        response["revision"] = revision
    return response


def _tool_error(status: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "success": False,
        "confirmed": False,
        "message": message,
    }
