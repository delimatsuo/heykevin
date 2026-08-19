"""Tenant-scoped Firestore repository for durable service-request aggregates."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.db.firestore_client import get_firestore_client
from app.services.service_request import (
    ServiceRequest,
    ServiceRequestCommandOutcome,
    ServiceRequestCommandResult,
    ServiceRequestOperation,
)
from app.services.service_request_repository import (
    ExecutionKind,
    PreparedProviderCreate,
    PreparedProviderOperation,
    ProviderBinding,
    ServiceRequestExecutionTarget,
    ServiceRequestMutation,
    ServiceRequestProviderBindingRequired,
    ServiceRequestRepositoryConflict,
    _normalize_provider_description,
    _normalize_provider_title,
    _prepare_provider_create,
    _prepare_provider_operation,
    _repository_identity,
    _required_customer_key,
    _required_identifier,
    _validate_prepared_provider_create,
    _validate_prepared_provider_operation,
)

SUBCOLLECTION = "service_requests"
RETENTION_DAYS = 90
IO_TIMEOUT_SECONDS = 5.0
PROVIDER_OPERATION_SCHEMA_VERSION = 2
PROVIDER_CREATE_SCHEMA_VERSION = 1
EXECUTION_KIND_FIELD = "execution_kind"
PROVIDER_BINDING_FIELD = "provider_binding"
PENDING_PROVIDER_OPERATION_FIELD = "pending_provider_operation"
PENDING_PROVIDER_CREATE_FIELD = "pending_provider_create"
PENDING_PROVIDER_CREATE_STATUS = "pending_provider_create"
FINALIZED_PROVIDER_CREATE_RECEIPT_FIELD = "finalized_provider_create_receipt"
PROVIDER_RECOVERY_STATE_FIELD = "provider_recovery_state"
NEXT_ATTEMPT_AT_FIELD = "next_attempt_at"
PROVIDER_RECOVERY_ATTEMPTS_FIELD = "provider_recovery_attempts"
PROVIDER_RECOVERY_LEASE_FIELD = "provider_recovery_lease"
PROVIDER_RECOVERY_PENDING = "pending"
PROVIDER_RECOVERY_NEEDS_REVIEW = "needs_review"
DEFAULT_RECOVERY_BATCH_SIZE = 10
MAX_RECOVERY_BATCH_SIZE = 25
MAX_RECOVERY_SCAN_SIZE = 100
DEFAULT_RECOVERY_LEASE_SECONDS = 10 * 60
MAX_PROVIDER_RECOVERY_ATTEMPTS = 8
RECOVERY_BACKOFF_BASE_SECONDS = 30
RECOVERY_BACKOFF_MAX_SECONDS = 6 * 3600


@dataclass(frozen=True, slots=True)
class LeasedProviderRecovery:
    """One strictly scoped provider proposal claimed by a recovery worker."""

    kind: Literal["create", "operation"]
    contractor_id: str
    customer_key: str
    request_id: str
    owner: str
    lease_id: str
    lease_expires_at: datetime
    attempt: int
    preparation: PreparedProviderCreate | PreparedProviderOperation


def _collection(db, contractor_id: str):
    if not contractor_id:
        raise ValueError("contractor_id is required")
    return db.collection("contractors").document(contractor_id).collection(SUBCOLLECTION)


def _decode(
    snapshot,
    *,
    customer_key: str,
    request_id: str | None = None,
) -> ServiceRequest | None:
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    if data.get("customer_key") != customer_key:
        return None
    expected_request_id = request_id or snapshot.id
    try:
        target = _execution_target_from_data(
            data,
            customer_key=customer_key,
            request_id=expected_request_id,
        )
    except (ServiceRequestRepositoryConflict, TypeError, ValueError):
        return None
    return target.request


def _binding_to_dict(binding: ProviderBinding) -> dict[str, str]:
    if not isinstance(binding, ProviderBinding):
        raise TypeError("binding must be a ProviderBinding")
    return {"kind": binding.kind, "resource_id": binding.resource_id}


def _binding_from_dict(value: object) -> ProviderBinding | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "resource_id"}:
        raise ServiceRequestRepositoryConflict("provider binding has an invalid schema")
    try:
        return ProviderBinding(kind=value["kind"], resource_id=value["resource_id"])
    except (TypeError, ValueError) as error:
        raise ServiceRequestRepositoryConflict("provider binding is invalid") from error


def _pending_operation_data(preparation: PreparedProviderOperation) -> dict[str, object]:
    _validate_prepared_provider_operation(preparation)
    return {
        "schema_version": PROVIDER_OPERATION_SCHEMA_VERSION,
        "logical_operation_id": preparation.logical_operation_id,
        "semantic_fingerprint": preparation.command_fingerprint,
        "base_revision": preparation.base_revision,
        "origin_idempotency_key": preparation.idempotency_key,
        "proposal": {
            "operation": preparation.operation.value,
            "arguments": dict(preparation.arguments),
            "result": {
                "request": preparation.result.request.to_dict(),
                "outcome": preparation.result.outcome.to_dict(),
            },
        },
    }


def _pending_operation_from_data(
    value: object,
    *,
    contractor_id: str,
    customer_key: str,
    request_id: str,
    binding: ProviderBinding,
    base_request: ServiceRequest,
) -> PreparedProviderOperation:
    expected_keys = {
        "schema_version",
        "logical_operation_id",
        "semantic_fingerprint",
        "base_revision",
        "origin_idempotency_key",
        "proposal",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ServiceRequestRepositoryConflict("pending provider operation has an invalid schema")
    if value.get("schema_version") != PROVIDER_OPERATION_SCHEMA_VERSION:
        raise ServiceRequestRepositoryConflict("pending provider operation schema is unsupported")
    proposal = value.get("proposal")
    if not isinstance(proposal, dict) or set(proposal) != {"operation", "arguments", "result"}:
        raise ServiceRequestRepositoryConflict("provider proposal has an invalid schema")
    arguments = proposal.get("arguments")
    result_data = proposal.get("result")
    if not isinstance(arguments, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in arguments.items()
    ):
        raise ServiceRequestRepositoryConflict("provider proposal arguments are invalid")
    if not isinstance(result_data, dict) or set(result_data) != {"request", "outcome"}:
        raise ServiceRequestRepositoryConflict("provider proposal result has an invalid schema")
    request_data = result_data.get("request")
    outcome_data = result_data.get("outcome")
    if not isinstance(request_data, dict) or not isinstance(outcome_data, dict):
        raise ServiceRequestRepositoryConflict("provider proposal result is invalid")
    try:
        result = ServiceRequestCommandResult(
            request=ServiceRequest.from_dict(request_data),
            outcome=ServiceRequestCommandOutcome.from_dict(outcome_data),
        )
        operation = ServiceRequestOperation(proposal.get("operation"))
        preparation = PreparedProviderOperation(
            logical_operation_id=value.get("logical_operation_id"),
            contractor_id=contractor_id,
            customer_key=customer_key,
            request_id=request_id,
            idempotency_key=value.get("origin_idempotency_key"),
            binding=binding,
            base_request=base_request,
            result=result,
            command_fingerprint=value.get("semantic_fingerprint"),
            operation=operation,
            arguments=tuple(sorted(arguments.items())),
            base_revision=value.get("base_revision"),
        )
        _validate_prepared_provider_operation(preparation)
    except (TypeError, ValueError) as error:
        raise ServiceRequestRepositoryConflict("provider proposal is invalid") from error
    return preparation


def _pending_create_data(preparation: PreparedProviderCreate) -> dict[str, object]:
    """Serialize the complete provider-create proposal needed after a restart."""

    _validate_prepared_provider_create(preparation)
    return {
        "schema_version": PROVIDER_CREATE_SCHEMA_VERSION,
        "logical_operation_id": preparation.logical_operation_id,
        "semantic_fingerprint": preparation.semantic_fingerprint,
        "command_fingerprint": preparation.command_fingerprint,
        "base_revision": preparation.base_revision,
        "origin_idempotency_key": preparation.idempotency_key,
        "binding": _binding_to_dict(preparation.binding),
        "title": preparation.title,
        "description": preparation.description,
        "result": {
            "request": preparation.result.request.to_dict(),
            "outcome": preparation.result.outcome.to_dict(),
        },
    }


def _pending_create_from_data(
    value: object,
    *,
    contractor_id: str,
    customer_key: str,
    request_id: str,
) -> PreparedProviderCreate:
    expected_keys = {
        "schema_version",
        "logical_operation_id",
        "semantic_fingerprint",
        "command_fingerprint",
        "base_revision",
        "origin_idempotency_key",
        "binding",
        "title",
        "description",
        "result",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ServiceRequestRepositoryConflict("pending provider create has an invalid schema")
    if value.get("schema_version") != PROVIDER_CREATE_SCHEMA_VERSION:
        raise ServiceRequestRepositoryConflict("pending provider create schema is unsupported")
    result_data = value.get("result")
    if not isinstance(result_data, dict) or set(result_data) != {"request", "outcome"}:
        raise ServiceRequestRepositoryConflict("provider create result has an invalid schema")
    request_data = result_data.get("request")
    outcome_data = result_data.get("outcome")
    if not isinstance(request_data, dict) or not isinstance(outcome_data, dict):
        raise ServiceRequestRepositoryConflict("provider create result is invalid")
    binding = _binding_from_dict(value.get("binding"))
    if binding is None:
        raise ServiceRequestRepositoryConflict("provider create binding is missing")
    try:
        preparation = PreparedProviderCreate(
            logical_operation_id=value.get("logical_operation_id"),
            contractor_id=contractor_id,
            customer_key=customer_key,
            request_id=request_id,
            idempotency_key=value.get("origin_idempotency_key"),
            binding=binding,
            result=ServiceRequestCommandResult(
                request=ServiceRequest.from_dict(request_data),
                outcome=ServiceRequestCommandOutcome.from_dict(outcome_data),
            ),
            command_fingerprint=value.get("command_fingerprint"),
            semantic_fingerprint=value.get("semantic_fingerprint"),
            title=value.get("title"),
            description=value.get("description"),
            base_revision=value.get("base_revision"),
        )
        _validate_prepared_provider_create(preparation)
    except (TypeError, ValueError) as error:
        raise ServiceRequestRepositoryConflict("provider create proposal is invalid") from error
    return preparation


def _pending_create_fields(
    preparation: PreparedProviderCreate,
    *,
    customer_key: str,
) -> dict[str, object]:
    request = preparation.result.request
    return {
        "customer_key": customer_key,
        "status": PENDING_PROVIDER_CREATE_STATUS,
        PROVIDER_RECOVERY_STATE_FIELD: PROVIDER_RECOVERY_PENDING,
        PROVIDER_RECOVERY_ATTEMPTS_FIELD: 0,
        NEXT_ATTEMPT_AT_FIELD: request.updated_at,
        "updated_at": request.updated_at,
        PENDING_PROVIDER_CREATE_FIELD: _pending_create_data(preparation),
    }


def _finalized_create_receipt_data(
    preparation: PreparedProviderCreate,
) -> dict[str, object]:
    return {
        "schema_version": PROVIDER_CREATE_SCHEMA_VERSION,
        "logical_operation_id": preparation.logical_operation_id,
        "semantic_fingerprint": preparation.semantic_fingerprint,
        "binding": _binding_to_dict(preparation.binding),
        "result": {
            "request": preparation.result.request.to_dict(),
            "outcome": preparation.result.outcome.to_dict(),
        },
    }


def _validate_finalized_create_receipt(
    value: object,
    *,
    preparation: PreparedProviderCreate,
) -> ServiceRequestCommandResult:
    expected_keys = {
        "schema_version",
        "logical_operation_id",
        "semantic_fingerprint",
        "binding",
        "result",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ServiceRequestRepositoryConflict("provider create receipt has an invalid schema")
    if (
        value.get("schema_version") != PROVIDER_CREATE_SCHEMA_VERSION
        or value.get("logical_operation_id") != preparation.logical_operation_id
        or value.get("semantic_fingerprint") != preparation.semantic_fingerprint
        or _binding_from_dict(value.get("binding")) != preparation.binding
    ):
        raise ServiceRequestRepositoryConflict("provider create receipt does not match")
    result_data = value.get("result")
    if not isinstance(result_data, dict) or set(result_data) != {"request", "outcome"}:
        raise ServiceRequestRepositoryConflict("provider create receipt result is invalid")
    request_data = result_data.get("request")
    outcome_data = result_data.get("outcome")
    if not isinstance(request_data, dict) or not isinstance(outcome_data, dict):
        raise ServiceRequestRepositoryConflict("provider create receipt result is invalid")
    try:
        result = ServiceRequestCommandResult(
            request=ServiceRequest.from_dict(request_data),
            outcome=ServiceRequestCommandOutcome.from_dict(outcome_data),
        )
    except (TypeError, ValueError) as error:
        raise ServiceRequestRepositoryConflict("provider create receipt is invalid") from error
    applied = _applied_result(
        result.request,
        command_fingerprint=preparation.command_fingerprint,
    )
    if (
        result.request.request_id != preparation.request_id
        or applied is None
        or applied.outcome != result.outcome
    ):
        raise ServiceRequestRepositoryConflict("provider create receipt result does not match")
    return result


def _load_pending_create(
    snapshot,
    *,
    contractor_id: str,
    customer_key: str,
    request_id: str,
) -> PreparedProviderCreate:
    if not snapshot.exists:
        raise ServiceRequestRepositoryConflict("pending provider create was not found")
    data = snapshot.to_dict() or {}
    expected_keys = {
        "customer_key",
        "status",
        PROVIDER_RECOVERY_STATE_FIELD,
        PROVIDER_RECOVERY_ATTEMPTS_FIELD,
        NEXT_ATTEMPT_AT_FIELD,
        "updated_at",
        PENDING_PROVIDER_CREATE_FIELD,
    }
    if set(data) not in (expected_keys, expected_keys | {PROVIDER_RECOVERY_LEASE_FIELD}):
        raise ServiceRequestRepositoryConflict(
            "pending provider create document has an invalid schema"
        )
    if data.get("customer_key") != customer_key:
        raise ServiceRequestRepositoryConflict("service request does not belong to this customer")
    if data.get("status") != PENDING_PROVIDER_CREATE_STATUS:
        raise ServiceRequestRepositoryConflict("pending provider create status is invalid")
    if data.get(PROVIDER_RECOVERY_STATE_FIELD) != PROVIDER_RECOVERY_PENDING:
        raise ServiceRequestRepositoryConflict("pending provider recovery state is invalid")
    attempts = data.get(PROVIDER_RECOVERY_ATTEMPTS_FIELD)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ServiceRequestRepositoryConflict("pending provider recovery attempts are invalid")
    preparation = _pending_create_from_data(
        data.get(PENDING_PROVIDER_CREATE_FIELD),
        contractor_id=contractor_id,
        customer_key=customer_key,
        request_id=request_id,
    )
    if data.get("updated_at") != preparation.result.request.updated_at:
        raise ServiceRequestRepositoryConflict("pending provider create timestamp is invalid")
    _required_aware_datetime(
        data.get(NEXT_ATTEMPT_AT_FIELD),
        "pending provider create retry time",
    )
    if PROVIDER_RECOVERY_LEASE_FIELD in data:
        _lease_from_dict(data.get(PROVIDER_RECOVERY_LEASE_FIELD))
    return preparation


def _required_aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ServiceRequestRepositoryConflict(f"{field_name} is invalid")
    return value.astimezone(UTC)


def _lease_to_dict(
    *,
    owner: str,
    lease_id: str,
    expires_at: datetime,
) -> dict[str, object]:
    return {
        "owner": _bounded_worker_identifier(owner, "recovery owner"),
        "lease_id": _bounded_worker_identifier(lease_id, "recovery lease_id"),
        "expires_at": _required_aware_datetime(expires_at, "recovery lease expiry"),
    }


def _lease_from_dict(value: object) -> tuple[str, str, datetime]:
    if not isinstance(value, dict) or set(value) != {"owner", "lease_id", "expires_at"}:
        raise ServiceRequestRepositoryConflict("provider recovery lease has an invalid schema")
    return (
        _bounded_worker_identifier(value.get("owner"), "recovery owner"),
        _bounded_worker_identifier(value.get("lease_id"), "recovery lease_id"),
        _required_aware_datetime(value.get("expires_at"), "recovery lease expiry"),
    )


def _bounded_worker_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ServiceRequestRepositoryConflict(f"{field_name} is invalid")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 128
        or any(ord(character) < 33 or ord(character) == 127 for character in normalized)
    ):
        raise ServiceRequestRepositoryConflict(f"{field_name} is invalid")
    return normalized


def _strict_service_request_path(path: object) -> tuple[str, str]:
    if not isinstance(path, str):
        raise ServiceRequestRepositoryConflict("provider recovery path is invalid")
    parts = path.split("/")
    if len(parts) != 4 or parts[0] != "contractors" or parts[2] != SUBCOLLECTION:
        raise ServiceRequestRepositoryConflict("provider recovery path is invalid")
    contractor_id = _required_identifier(parts[1], "contractor_id")
    request_id = _required_identifier(parts[3], "request_id")
    if contractor_id != parts[1] or request_id != parts[3] or "/" in contractor_id + request_id:
        raise ServiceRequestRepositoryConflict("provider recovery path is invalid")
    return contractor_id, request_id


def _recovery_backoff(attempt: int) -> timedelta:
    exponent = min(max(attempt - 1, 0), 20)
    seconds = min(
        RECOVERY_BACKOFF_BASE_SECONDS * (2**exponent),
        RECOVERY_BACKOFF_MAX_SECONDS,
    )
    return timedelta(seconds=seconds)


def _recovery_metadata(data: dict) -> tuple[int, datetime, tuple[str, str, datetime] | None]:
    if data.get(PROVIDER_RECOVERY_STATE_FIELD) != PROVIDER_RECOVERY_PENDING:
        raise ServiceRequestRepositoryConflict("provider recovery is not pending")
    attempts = data.get(PROVIDER_RECOVERY_ATTEMPTS_FIELD)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ServiceRequestRepositoryConflict("provider recovery attempts are invalid")
    next_attempt_at = _required_aware_datetime(
        data.get(NEXT_ATTEMPT_AT_FIELD),
        "provider recovery retry time",
    )
    lease_value = data.get(PROVIDER_RECOVERY_LEASE_FIELD)
    lease = _lease_from_dict(lease_value) if lease_value is not None else None
    if lease is not None and lease[2] != next_attempt_at:
        raise ServiceRequestRepositoryConflict(
            "provider recovery lease expiry does not match retry time"
        )
    return attempts, next_attempt_at, lease


def _recovery_preparation(
    snapshot,
    *,
    contractor_id: str,
    request_id: str,
) -> tuple[str, PreparedProviderCreate | PreparedProviderOperation]:
    if not snapshot.exists:
        raise ServiceRequestRepositoryConflict("provider recovery document was not found")
    data = snapshot.to_dict() or {}
    customer_key = _required_customer_key(data.get("customer_key"))
    has_create = PENDING_PROVIDER_CREATE_FIELD in data
    has_operation = PENDING_PROVIDER_OPERATION_FIELD in data
    if has_create == has_operation:
        raise ServiceRequestRepositoryConflict("provider recovery proposal is ambiguous")
    _recovery_metadata(data)
    if has_create:
        if isinstance(data.get("aggregate"), dict) or PROVIDER_BINDING_FIELD in data:
            raise ServiceRequestRepositoryConflict(
                "pending provider create exposes canonical state"
            )
        return "create", _load_pending_create(
            snapshot,
            contractor_id=contractor_id,
            customer_key=customer_key,
            request_id=request_id,
        )

    allowed = {
        "customer_key",
        EXECUTION_KIND_FIELD,
        "status",
        "updated_at",
        "aggregate",
        PROVIDER_BINDING_FIELD,
        PENDING_PROVIDER_OPERATION_FIELD,
        PROVIDER_RECOVERY_STATE_FIELD,
        PROVIDER_RECOVERY_ATTEMPTS_FIELD,
        NEXT_ATTEMPT_AT_FIELD,
    }
    optional = {FINALIZED_PROVIDER_CREATE_RECEIPT_FIELD, PROVIDER_RECOVERY_LEASE_FIELD}
    if not allowed.issubset(data) or set(data) - allowed - optional:
        raise ServiceRequestRepositoryConflict(
            "pending provider operation document has an invalid schema"
        )
    _data, current = _load_bound_request(
        snapshot,
        customer_key=customer_key,
        request_id=request_id,
    )
    target = _execution_target_from_data(
        data,
        customer_key=customer_key,
        request_id=request_id,
    )
    if target.execution_kind is not ExecutionKind.PROVIDER:
        raise ServiceRequestRepositoryConflict("provider recovery target is not provider-backed")
    if data.get("status") != current.status.value:
        raise ServiceRequestRepositoryConflict("provider recovery status is invalid")
    updated_at = _required_aware_datetime(data.get("updated_at"), "provider recovery update time")
    if updated_at != current.updated_at.astimezone(UTC):
        raise ServiceRequestRepositoryConflict("provider recovery aggregate timestamp is invalid")
    binding = target.provider_binding
    if binding is None:
        raise ServiceRequestRepositoryConflict("service request has no provider binding")
    return "operation", _pending_operation_from_data(
        data.get(PENDING_PROVIDER_OPERATION_FIELD),
        contractor_id=contractor_id,
        customer_key=customer_key,
        request_id=request_id,
        binding=binding,
        base_request=current,
    )


def _lease_matches(data: dict, lease: LeasedProviderRecovery) -> bool:
    try:
        owner, lease_id, expires_at = _lease_from_dict(data.get(PROVIDER_RECOVERY_LEASE_FIELD))
    except ServiceRequestRepositoryConflict:
        return False
    attempts = data.get(PROVIDER_RECOVERY_ATTEMPTS_FIELD)
    return (
        owner == lease.owner
        and lease_id == lease.lease_id
        and expires_at == lease.lease_expires_at.astimezone(UTC)
        and attempts == lease.attempt
    )


def _validate_recovery_lease(lease: LeasedProviderRecovery) -> None:
    if not isinstance(lease, LeasedProviderRecovery):
        raise TypeError("lease must be a LeasedProviderRecovery")
    contractor, customer, request = _repository_identity(
        lease.contractor_id,
        lease.customer_key,
        lease.request_id,
    )
    if (
        contractor != lease.contractor_id
        or customer != lease.customer_key
        or request != lease.request_id
    ):
        raise ServiceRequestRepositoryConflict("provider recovery lease identity is invalid")
    if lease.kind not in {"create", "operation"}:
        raise ServiceRequestRepositoryConflict("provider recovery lease kind is invalid")
    _bounded_worker_identifier(lease.owner, "recovery owner")
    _bounded_worker_identifier(lease.lease_id, "recovery lease_id")
    _required_aware_datetime(lease.lease_expires_at, "recovery lease expiry")
    if isinstance(lease.attempt, bool) or not isinstance(lease.attempt, int) or lease.attempt < 1:
        raise ServiceRequestRepositoryConflict("provider recovery lease attempt is invalid")


def _active_recovery_lease(data: dict, *, now: datetime | None = None) -> bool:
    value = data.get(PROVIDER_RECOVERY_LEASE_FIELD)
    if value is None:
        return False
    _owner, _lease_id, expires_at = _lease_from_dict(value)
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    return expires_at > checked_at


def _applied_result(
    request: ServiceRequest,
    *,
    command_fingerprint: str,
) -> ServiceRequestCommandResult | None:
    records = [
        record
        for record in request.idempotency_records
        if record.command_fingerprint == command_fingerprint
    ]
    if len(records) != 1:
        return None
    return ServiceRequestCommandResult(request=request, outcome=records[0].outcome)


def _request_fields(
    request: ServiceRequest,
    *,
    customer_key: str,
    execution_kind: ExecutionKind,
) -> dict[str, object]:
    if not isinstance(execution_kind, ExecutionKind):
        raise TypeError("execution_kind must be an ExecutionKind")
    expiry = max(request.scheduled_end, datetime.now(UTC)) + timedelta(days=RETENTION_DAYS)
    return {
        "customer_key": customer_key,
        EXECUTION_KIND_FIELD: execution_kind.value,
        "status": request.status.value,
        "updated_at": request.updated_at,
        "expires_at": expiry,
        "aggregate": request.to_dict(),
    }


def _load_bound_request(
    snapshot,
    *,
    customer_key: str,
    request_id: str,
) -> tuple[dict, ServiceRequest]:
    if not snapshot.exists:
        raise ServiceRequestRepositoryConflict("service request was not found")
    data = snapshot.to_dict() or {}
    if data.get("customer_key") != customer_key:
        raise ServiceRequestRepositoryConflict("service request does not belong to this customer")
    aggregate = data.get("aggregate")
    if not isinstance(aggregate, dict):
        raise ServiceRequestRepositoryConflict("service request aggregate is missing")
    request = ServiceRequest.from_dict(aggregate)
    if request.request_id != request_id:
        raise ServiceRequestRepositoryConflict("stored aggregate does not match its request path")
    return data, request


def _execution_target_from_data(
    data: dict,
    *,
    customer_key: str,
    request_id: str,
) -> ServiceRequestExecutionTarget:
    """Decode a canonical request only when its execution authority is coherent."""

    if data.get("customer_key") != customer_key:
        raise ServiceRequestRepositoryConflict("service request does not belong to this customer")
    aggregate = data.get("aggregate")
    if not isinstance(aggregate, dict):
        raise ServiceRequestRepositoryConflict("service request aggregate is missing")
    try:
        request = ServiceRequest.from_dict(aggregate)
    except (TypeError, ValueError) as error:
        raise ServiceRequestRepositoryConflict("service request aggregate is invalid") from error
    if request.request_id != request_id:
        raise ServiceRequestRepositoryConflict("stored aggregate does not match its request path")
    try:
        execution_kind = ExecutionKind(data.get(EXECUTION_KIND_FIELD))
    except (TypeError, ValueError) as error:
        raise ServiceRequestRepositoryConflict(
            "service request execution provenance is missing or invalid"
        ) from error

    if execution_kind is ExecutionKind.LOCAL:
        if PROVIDER_BINDING_FIELD in data or FINALIZED_PROVIDER_CREATE_RECEIPT_FIELD in data:
            raise ServiceRequestRepositoryConflict(
                "local execution provenance conflicts with provider state"
            )
        return ServiceRequestExecutionTarget(
            request=request,
            execution_kind=ExecutionKind.LOCAL,
        )

    binding = _binding_from_dict(data.get(PROVIDER_BINDING_FIELD))
    if binding is None:
        raise ServiceRequestRepositoryConflict("provider execution binding is missing")
    receipt = data.get(FINALIZED_PROVIDER_CREATE_RECEIPT_FIELD)
    expected_receipt_keys = {
        "schema_version",
        "logical_operation_id",
        "semantic_fingerprint",
        "binding",
        "result",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_receipt_keys:
        raise ServiceRequestRepositoryConflict("provider execution receipt is missing or invalid")
    if receipt.get("schema_version") != PROVIDER_CREATE_SCHEMA_VERSION:
        raise ServiceRequestRepositoryConflict("provider execution receipt schema is unsupported")
    try:
        _required_identifier(receipt.get("logical_operation_id"), "logical_operation_id")
        _required_identifier(receipt.get("semantic_fingerprint"), "semantic_fingerprint")
    except (TypeError, ValueError) as error:
        raise ServiceRequestRepositoryConflict("provider execution receipt is invalid") from error
    if _binding_from_dict(receipt.get("binding")) != binding:
        raise ServiceRequestRepositoryConflict("provider execution receipt binding does not match")
    result_data = receipt.get("result")
    if not isinstance(result_data, dict) or set(result_data) != {"request", "outcome"}:
        raise ServiceRequestRepositoryConflict("provider execution receipt result is invalid")
    try:
        create_result = ServiceRequestCommandResult(
            request=ServiceRequest.from_dict(result_data.get("request")),
            outcome=ServiceRequestCommandOutcome.from_dict(result_data.get("outcome")),
        )
    except (TypeError, ValueError) as error:
        raise ServiceRequestRepositoryConflict("provider execution receipt is invalid") from error
    records = create_result.request.idempotency_records
    if (
        create_result.request.request_id != request_id
        or len(records) != 1
        or records[0].outcome.operation is not ServiceRequestOperation.CREATE
        or records[0].outcome != create_result.outcome
    ):
        raise ServiceRequestRepositoryConflict("provider execution receipt result does not match")
    applied = _applied_result(request, command_fingerprint=records[0].command_fingerprint)
    if applied is None or applied.outcome != create_result.outcome:
        raise ServiceRequestRepositoryConflict(
            "provider execution receipt is not present in canonical state"
        )
    return ServiceRequestExecutionTarget(
        request=request,
        execution_kind=ExecutionKind.PROVIDER,
        provider_binding=binding,
    )


class FirestoreServiceRequestRepository:
    """Firestore adapter whose atomic mutation executes inside one transaction."""

    def __init__(self, client=None) -> None:
        self._client = client

    def _db(self):
        return self._client or get_firestore_client()

    async def get(self, *, contractor_id: str, customer_key: str, request_id: str):
        target = await self.get_execution_target(
            contractor_id=contractor_id,
            customer_key=customer_key,
            request_id=request_id,
        )
        return target.request if target is not None else None

    async def get_execution_target(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
    ) -> ServiceRequestExecutionTarget | None:
        contractor, customer, request = _repository_identity(
            contractor_id,
            customer_key,
            request_id,
        )
        ref = _collection(self._db(), contractor).document(request)
        snapshot = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, ref.get),
            timeout=IO_TIMEOUT_SECONDS,
        )
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if data.get("customer_key") != customer:
            return None
        if not isinstance(data.get("aggregate"), dict):
            if PENDING_PROVIDER_CREATE_FIELD not in data:
                raise ServiceRequestRepositoryConflict(
                    "service request has no canonical aggregate or provider-create proposal"
                )
            _load_pending_create(
                snapshot,
                contractor_id=contractor,
                customer_key=customer,
                request_id=request,
            )
            return None
        return _execution_target_from_data(
            data,
            customer_key=customer,
            request_id=request,
        )

    async def apply(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
        mutation: ServiceRequestMutation,
    ) -> ServiceRequestCommandResult:
        db = self._db()
        ref = _collection(db, contractor_id).document(request_id)

        def _write() -> ServiceRequestCommandResult:
            transaction = db.transaction()

            @firestore.transactional
            def _transaction(tx):
                snapshot = ref.get(transaction=tx)
                if snapshot.exists:
                    data, current = _load_bound_request(
                        snapshot,
                        customer_key=customer_key,
                        request_id=request_id,
                    )
                    target = _execution_target_from_data(
                        data,
                        customer_key=customer_key,
                        request_id=request_id,
                    )
                    if data.get(PENDING_PROVIDER_OPERATION_FIELD) is not None:
                        raise ServiceRequestRepositoryConflict(
                            "a provider mutation is pending for this service request"
                        )
                    if target.execution_kind is ExecutionKind.PROVIDER:
                        raise ServiceRequestProviderBindingRequired(
                            "provider requests require two-phase mutation"
                        )
                else:
                    current = None
                    target = None
                result = mutation(current)
                if result.request.request_id != request_id:
                    raise ServiceRequestRepositoryConflict(
                        "mutation returned a different request_id"
                    )
                if current is not result.request:
                    tx.set(
                        ref,
                        _request_fields(
                            result.request,
                            customer_key=customer_key,
                            execution_kind=(
                                target.execution_kind if target is not None else ExecutionKind.LOCAL
                            ),
                        ),
                    )
                return result

            return _transaction(transaction)

        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _write),
            timeout=IO_TIMEOUT_SECONDS,
        )

    async def list_actionable(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        limit: int = 5,
    ) -> tuple[ServiceRequest, ...]:
        bounded_limit = min(max(int(limit), 1), 5)
        query = (
            _collection(self._db(), contractor_id)
            .where(filter=FieldFilter("customer_key", "==", customer_key))
            .where(filter=FieldFilter("status", "==", "open"))
            .order_by("updated_at", direction=firestore.Query.DESCENDING)
            .limit(min(bounded_limit * 4, 20))
        )
        snapshots = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, lambda: list(query.stream())),
            timeout=IO_TIMEOUT_SECONDS,
        )
        requests: list[ServiceRequest] = []
        for snapshot in snapshots:
            data = snapshot.to_dict() or {}
            try:
                target = _execution_target_from_data(
                    data,
                    customer_key=customer_key,
                    request_id=snapshot.id,
                )
            except (ServiceRequestRepositoryConflict, TypeError, ValueError):
                continue
            requests.append(target.request)
        requests.sort(key=lambda request: request.updated_at, reverse=True)
        return tuple(requests[:bounded_limit])

    async def bind_provider(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
        binding: ProviderBinding,
    ) -> None:
        contractor, customer, request = _repository_identity(
            contractor_id,
            customer_key,
            request_id,
        )
        db = self._db()
        ref = _collection(db, contractor).document(request)

        def _write() -> None:
            transaction = db.transaction()

            @firestore.transactional
            def _transaction(tx) -> None:
                snapshot = ref.get(transaction=tx)
                data, _current = _load_bound_request(
                    snapshot,
                    customer_key=customer,
                    request_id=request,
                )
                target = _execution_target_from_data(
                    data,
                    customer_key=customer,
                    request_id=request,
                )
                if data.get(PENDING_PROVIDER_OPERATION_FIELD) is not None:
                    raise ServiceRequestRepositoryConflict(
                        "cannot replace a provider binding while a mutation is pending"
                    )
                if target.execution_kind is ExecutionKind.LOCAL:
                    raise ServiceRequestRepositoryConflict(
                        "local execution provenance cannot be upgraded to provider"
                    )
                existing = target.provider_binding
                if existing != binding:
                    raise ServiceRequestRepositoryConflict(
                        "service request already has a different provider binding"
                    )

            _transaction(transaction)

        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _write),
            timeout=IO_TIMEOUT_SECONDS,
        )

    async def get_provider_binding(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
    ) -> ProviderBinding | None:
        contractor, customer, request = _repository_identity(
            contractor_id,
            customer_key,
            request_id,
        )
        target = await self.get_execution_target(
            contractor_id=contractor,
            customer_key=customer,
            request_id=request,
        )
        return target.provider_binding if target is not None else None

    async def prepare_provider_operation(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
        idempotency_key: str,
        mutation: ServiceRequestMutation,
    ) -> PreparedProviderOperation:
        contractor, customer, request = _repository_identity(
            contractor_id,
            customer_key,
            request_id,
        )
        key = _required_identifier(idempotency_key, "idempotency_key")
        if not callable(mutation):
            raise TypeError("mutation must be callable")
        db = self._db()
        ref = _collection(db, contractor).document(request)

        def _write() -> PreparedProviderOperation:
            transaction = db.transaction()

            @firestore.transactional
            def _transaction(tx) -> PreparedProviderOperation:
                snapshot = ref.get(transaction=tx)
                data, current = _load_bound_request(
                    snapshot,
                    customer_key=customer,
                    request_id=request,
                )
                target = _execution_target_from_data(
                    data,
                    customer_key=customer,
                    request_id=request,
                )
                if target.execution_kind is not ExecutionKind.PROVIDER:
                    raise ServiceRequestRepositoryConflict("service request is not provider-backed")
                binding = target.provider_binding
                if binding is None:  # Defensive: target validation requires this.
                    raise ServiceRequestRepositoryConflict(
                        "service request has no provider binding"
                    )

                result = mutation(current)
                if not isinstance(result, ServiceRequestCommandResult):
                    raise TypeError("mutation must return ServiceRequestCommandResult")
                if result.request.request_id != request:
                    raise ServiceRequestRepositoryConflict(
                        "mutation returned a different request_id"
                    )
                preparation = _prepare_provider_operation(
                    contractor_id=contractor,
                    customer_key=customer,
                    request_id=request,
                    idempotency_key=key,
                    binding=binding,
                    base_request=current,
                    result=result,
                )
                pending = data.get(PENDING_PROVIDER_OPERATION_FIELD)
                if pending is not None:
                    _recovery_metadata(data)
                    recovered = _pending_operation_from_data(
                        pending,
                        contractor_id=contractor,
                        customer_key=customer,
                        request_id=request,
                        binding=binding,
                        base_request=current,
                    )
                    if (
                        recovered.command_fingerprint != preparation.command_fingerprint
                        or recovered.base_revision != preparation.base_revision
                        or recovered.operation is not preparation.operation
                        or recovered.arguments != preparation.arguments
                    ):
                        raise ServiceRequestRepositoryConflict(
                            "a different provider mutation is already pending"
                        )
                    return recovered
                if not preparation.already_applied:
                    pending_data = dict(data)
                    pending_data[PENDING_PROVIDER_OPERATION_FIELD] = _pending_operation_data(
                        preparation
                    )
                    pending_data[PROVIDER_RECOVERY_STATE_FIELD] = PROVIDER_RECOVERY_PENDING
                    pending_data[PROVIDER_RECOVERY_ATTEMPTS_FIELD] = 0
                    pending_data[NEXT_ATTEMPT_AT_FIELD] = preparation.result.request.updated_at
                    # Firestore TTL must never auto-delete an uncertain provider
                    # operation. Canonical finalization restores retention expiry.
                    pending_data.pop("expires_at", None)
                    tx.set(ref, pending_data)
                return preparation

            return _transaction(transaction)

        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _write),
            timeout=IO_TIMEOUT_SECONDS,
        )

    async def finalize_provider_operation(
        self,
        preparation: PreparedProviderOperation,
    ) -> ServiceRequestCommandResult:
        if not isinstance(preparation, PreparedProviderOperation):
            raise TypeError("preparation must be a PreparedProviderOperation")
        contractor, customer, request = _repository_identity(
            preparation.contractor_id,
            preparation.customer_key,
            preparation.request_id,
        )
        _validate_prepared_provider_operation(preparation)
        db = self._db()
        ref = _collection(db, contractor).document(request)

        def _write() -> ServiceRequestCommandResult:
            transaction = db.transaction()

            @firestore.transactional
            def _transaction(tx) -> ServiceRequestCommandResult:
                snapshot = ref.get(transaction=tx)
                data, current = _load_bound_request(
                    snapshot,
                    customer_key=customer,
                    request_id=request,
                )
                target = _execution_target_from_data(
                    data,
                    customer_key=customer,
                    request_id=request,
                )
                if target.execution_kind is not ExecutionKind.PROVIDER:
                    raise ServiceRequestRepositoryConflict("service request is not provider-backed")
                binding = target.provider_binding
                if preparation.already_applied:
                    applied = _applied_result(
                        current,
                        command_fingerprint=preparation.command_fingerprint,
                    )
                    if applied is None or preparation.binding != binding:
                        raise ServiceRequestRepositoryConflict(
                            "applied provider operation no longer matches canonical state"
                        )
                    return applied

                pending = data.get(PENDING_PROVIDER_OPERATION_FIELD)
                if pending is None:
                    applied = _applied_result(
                        current,
                        command_fingerprint=preparation.command_fingerprint,
                    )
                    if applied is not None and preparation.binding == binding:
                        return applied
                    raise ServiceRequestRepositoryConflict(
                        "provider operation is not the current prepared mutation"
                    )
                _recovery_metadata(data)
                if _active_recovery_lease(data):
                    raise ServiceRequestRepositoryConflict(
                        "provider operation is owned by a recovery worker"
                    )
                recovered = _pending_operation_from_data(
                    pending,
                    contractor_id=contractor,
                    customer_key=customer,
                    request_id=request,
                    binding=binding,
                    base_request=current,
                )
                if recovered.logical_operation_id != preparation.logical_operation_id:
                    raise ServiceRequestRepositoryConflict(
                        "provider operation is not the current prepared mutation"
                    )
                if binding != preparation.binding:
                    raise ServiceRequestRepositoryConflict(
                        "service request changed before provider finalization"
                    )

                updated_data = dict(data)
                updated_data.update(
                    _request_fields(
                        recovered.result.request,
                        customer_key=customer,
                        execution_kind=ExecutionKind.PROVIDER,
                    )
                )
                updated_data.pop(PENDING_PROVIDER_OPERATION_FIELD, None)
                updated_data.pop(PROVIDER_RECOVERY_STATE_FIELD, None)
                updated_data.pop(PROVIDER_RECOVERY_ATTEMPTS_FIELD, None)
                updated_data.pop(NEXT_ATTEMPT_AT_FIELD, None)
                updated_data.pop(PROVIDER_RECOVERY_LEASE_FIELD, None)
                tx.set(ref, updated_data)
                return recovered.result

            return _transaction(transaction)

        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _write),
            timeout=IO_TIMEOUT_SECONDS,
        )

    async def recover_provider_operation(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
    ) -> PreparedProviderOperation | None:
        contractor, customer, request = _repository_identity(
            contractor_id,
            customer_key,
            request_id,
        )
        ref = _collection(self._db(), contractor).document(request)
        snapshot = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, ref.get),
            timeout=IO_TIMEOUT_SECONDS,
        )
        if not snapshot.exists:
            return None
        data, current = _load_bound_request(
            snapshot,
            customer_key=customer,
            request_id=request,
        )
        target = _execution_target_from_data(
            data,
            customer_key=customer,
            request_id=request,
        )
        if target.execution_kind is not ExecutionKind.PROVIDER:
            raise ServiceRequestRepositoryConflict("service request is not provider-backed")
        pending = data.get(PENDING_PROVIDER_OPERATION_FIELD)
        if pending is None:
            return None
        _recovery_metadata(data)
        binding = target.provider_binding
        if binding is None:
            raise ServiceRequestRepositoryConflict("service request has no provider binding")
        return _pending_operation_from_data(
            pending,
            contractor_id=contractor,
            customer_key=customer,
            request_id=request,
            binding=binding,
            base_request=current,
        )

    async def prepare_provider_create(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
        idempotency_key: str,
        binding: ProviderBinding,
        title: str,
        description: str,
        mutation: ServiceRequestMutation,
    ) -> PreparedProviderCreate:
        contractor, customer, request = _repository_identity(
            contractor_id,
            customer_key,
            request_id,
        )
        key = _required_identifier(idempotency_key, "idempotency_key")
        if not isinstance(binding, ProviderBinding):
            raise TypeError("binding must be a ProviderBinding")
        if not callable(mutation):
            raise TypeError("mutation must be callable")
        normalized_title = _normalize_provider_title(title)
        normalized_description = _normalize_provider_description(description)

        # Evaluate exactly once before entering Firestore's retryable transaction.
        # The resulting immutable proposal is the only provider-create intent that
        # can occupy this tenant/request path.
        result = mutation(None)
        if not isinstance(result, ServiceRequestCommandResult):
            raise TypeError("mutation must return ServiceRequestCommandResult")
        if result.request.request_id != request:
            raise ServiceRequestRepositoryConflict("mutation returned a different request_id")
        preparation = _prepare_provider_create(
            contractor_id=contractor,
            customer_key=customer,
            request_id=request,
            idempotency_key=key,
            binding=binding,
            title=normalized_title,
            description=normalized_description,
            result=result,
        )

        db = self._db()
        ref = _collection(db, contractor).document(request)

        def _write() -> PreparedProviderCreate:
            transaction = db.transaction()

            @firestore.transactional
            def _transaction(tx) -> PreparedProviderCreate:
                snapshot = ref.get(transaction=tx)
                if snapshot.exists:
                    data = snapshot.to_dict() or {}
                    if data.get("customer_key") != customer:
                        raise ServiceRequestRepositoryConflict(
                            "service request does not belong to this customer"
                        )
                    if isinstance(data.get("aggregate"), dict):
                        target = _execution_target_from_data(
                            data,
                            customer_key=customer,
                            request_id=request,
                        )
                        if (
                            target.execution_kind is not ExecutionKind.PROVIDER
                            or target.provider_binding != preparation.binding
                        ):
                            raise ServiceRequestRepositoryConflict(
                                "service request execution provenance does not match"
                            )
                        receipt = data.get(FINALIZED_PROVIDER_CREATE_RECEIPT_FIELD)
                        if receipt is None:
                            raise ServiceRequestRepositoryConflict("service request already exists")
                        _validate_finalized_create_receipt(
                            receipt,
                            preparation=preparation,
                        )
                        return replace(preparation, already_applied=True)
                    recovered = _load_pending_create(
                        snapshot,
                        contractor_id=contractor,
                        customer_key=customer,
                        request_id=request,
                    )
                    if recovered.semantic_fingerprint != preparation.semantic_fingerprint:
                        raise ServiceRequestRepositoryConflict(
                            "a different provider create is already pending"
                        )
                    return recovered
                tx.set(ref, _pending_create_fields(preparation, customer_key=customer))
                return preparation

            return _transaction(transaction)

        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _write),
            timeout=IO_TIMEOUT_SECONDS,
        )

    async def finalize_provider_create(
        self,
        preparation: PreparedProviderCreate,
    ) -> ServiceRequestCommandResult:
        if not isinstance(preparation, PreparedProviderCreate):
            raise TypeError("preparation must be a PreparedProviderCreate")
        _validate_prepared_provider_create(preparation)
        contractor, customer, request = _repository_identity(
            preparation.contractor_id,
            preparation.customer_key,
            preparation.request_id,
        )
        db = self._db()
        ref = _collection(db, contractor).document(request)

        def _write() -> ServiceRequestCommandResult:
            transaction = db.transaction()

            @firestore.transactional
            def _transaction(tx) -> ServiceRequestCommandResult:
                snapshot = ref.get(transaction=tx)
                if not snapshot.exists:
                    raise ServiceRequestRepositoryConflict(
                        "provider create is not the current prepared operation"
                    )
                data = snapshot.to_dict() or {}
                if data.get("customer_key") != customer:
                    raise ServiceRequestRepositoryConflict(
                        "service request does not belong to this customer"
                    )

                # Finalization is replay-safe even after the pending envelope has
                # been atomically replaced by the canonical aggregate and binding.
                if isinstance(data.get("aggregate"), dict):
                    _data, current = _load_bound_request(
                        snapshot,
                        customer_key=customer,
                        request_id=request,
                    )
                    target = _execution_target_from_data(
                        data,
                        customer_key=customer,
                        request_id=request,
                    )
                    if target.execution_kind is not ExecutionKind.PROVIDER:
                        raise ServiceRequestRepositoryConflict(
                            "service request is not provider-backed"
                        )
                    binding = target.provider_binding
                    applied = _validate_finalized_create_receipt(
                        data.get(FINALIZED_PROVIDER_CREATE_RECEIPT_FIELD),
                        preparation=preparation,
                    )
                    canonical_create = _applied_result(
                        current,
                        command_fingerprint=preparation.command_fingerprint,
                    )
                    if (
                        binding != preparation.binding
                        or canonical_create is None
                        or canonical_create.outcome != applied.outcome
                    ):
                        raise ServiceRequestRepositoryConflict(
                            "finalized provider create no longer matches canonical state"
                        )
                    return applied

                recovered = _load_pending_create(
                    snapshot,
                    contractor_id=contractor,
                    customer_key=customer,
                    request_id=request,
                )
                if _active_recovery_lease(data):
                    raise ServiceRequestRepositoryConflict(
                        "provider create is owned by a recovery worker"
                    )
                if (
                    recovered.logical_operation_id != preparation.logical_operation_id
                    or recovered.semantic_fingerprint != preparation.semantic_fingerprint
                ):
                    raise ServiceRequestRepositoryConflict(
                        "provider create is not the current prepared operation"
                    )

                # One non-merge write is the visibility boundary: an OPEN provider
                # aggregate and its binding appear together, never independently.
                finalized = _request_fields(
                    recovered.result.request,
                    customer_key=customer,
                    execution_kind=ExecutionKind.PROVIDER,
                )
                finalized[PROVIDER_BINDING_FIELD] = _binding_to_dict(recovered.binding)
                finalized[FINALIZED_PROVIDER_CREATE_RECEIPT_FIELD] = _finalized_create_receipt_data(
                    recovered
                )
                tx.set(ref, finalized)
                return recovered.result

            return _transaction(transaction)

        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _write),
            timeout=IO_TIMEOUT_SECONDS,
        )

    async def recover_provider_create(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
    ) -> PreparedProviderCreate | None:
        contractor, customer, request = _repository_identity(
            contractor_id,
            customer_key,
            request_id,
        )
        ref = _collection(self._db(), contractor).document(request)
        snapshot = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, ref.get),
            timeout=IO_TIMEOUT_SECONDS,
        )
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if data.get("customer_key") != customer:
            return None
        if isinstance(data.get("aggregate"), dict):
            return None
        return _load_pending_create(
            snapshot,
            contractor_id=contractor,
            customer_key=customer,
            request_id=request,
        )

    async def claim_due_provider_recoveries(
        self,
        *,
        owner: str,
        now: datetime,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
        lease_seconds: int = DEFAULT_RECOVERY_LEASE_SECONDS,
        max_attempts: int = MAX_PROVIDER_RECOVERY_ATTEMPTS,
    ) -> tuple[LeasedProviderRecovery, ...]:
        """Lease a bounded collection-group batch of due provider proposals."""

        normalized_owner = _bounded_worker_identifier(owner, "recovery owner")
        checked_at = _required_aware_datetime(now, "provider recovery clock")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RECOVERY_BATCH_SIZE
        ):
            raise ValueError(f"limit must be between 1 and {MAX_RECOVERY_BATCH_SIZE}")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 15 <= lease_seconds <= 15 * 60
        ):
            raise ValueError("lease_seconds must be between 15 and 900")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 50
        ):
            raise ValueError("max_attempts must be between 1 and 50")

        db = self._db()
        query = (
            db.collection_group(SUBCOLLECTION)
            .where(
                filter=FieldFilter(
                    PROVIDER_RECOVERY_STATE_FIELD,
                    "==",
                    PROVIDER_RECOVERY_PENDING,
                )
            )
            .where(filter=FieldFilter(NEXT_ATTEMPT_AT_FIELD, "<=", checked_at))
            .order_by(NEXT_ATTEMPT_AT_FIELD, direction=firestore.Query.ASCENDING)
            # Scan a bounded superset so a malformed record or an active lease
            # observed through a stale query does not starve all healthy work.
            .limit(min(limit * 4, MAX_RECOVERY_SCAN_SIZE))
        )
        snapshots = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: list(query.stream()),
            ),
            timeout=IO_TIMEOUT_SECONDS,
        )

        claimed: list[LeasedProviderRecovery] = []
        for candidate in snapshots:
            if len(claimed) >= limit:
                break
            reference = getattr(candidate, "reference", None)
            path = getattr(reference, "path", None)
            try:
                contractor_id, request_id = _strict_service_request_path(path)
            except ServiceRequestRepositoryConflict:
                continue
            lease_id = secrets.token_hex(16)

            def _claim_one(
                reference=reference,
                contractor_id=contractor_id,
                request_id=request_id,
                lease_id=lease_id,
            ) -> LeasedProviderRecovery | None:
                transaction = db.transaction()

                @firestore.transactional
                def _transaction(tx) -> LeasedProviderRecovery | None:
                    snapshot = reference.get(transaction=tx)
                    if not snapshot.exists:
                        return None
                    data = snapshot.to_dict() or {}
                    if not isinstance(data, dict):
                        return None
                    try:
                        attempts, next_attempt_at, current_lease = _recovery_metadata(data)
                        kind, preparation = _recovery_preparation(
                            snapshot,
                            contractor_id=contractor_id,
                            request_id=request_id,
                        )
                    except (ServiceRequestRepositoryConflict, TypeError, ValueError):
                        # The exact product path is known, but its envelope cannot
                        # be replayed safely. Retain it for operator review rather
                        # than repeatedly starving valid due proposals.
                        reviewed = dict(data)
                        reviewed[PROVIDER_RECOVERY_STATE_FIELD] = PROVIDER_RECOVERY_NEEDS_REVIEW
                        reviewed.pop(PROVIDER_RECOVERY_LEASE_FIELD, None)
                        reviewed.pop("expires_at", None)
                        tx.set(reference, reviewed)
                        return None
                    if next_attempt_at > checked_at:
                        return None
                    if current_lease is not None and current_lease[2] > checked_at:
                        return None
                    if attempts >= max_attempts:
                        reviewed = dict(data)
                        reviewed[PROVIDER_RECOVERY_STATE_FIELD] = PROVIDER_RECOVERY_NEEDS_REVIEW
                        reviewed.pop(PROVIDER_RECOVERY_LEASE_FIELD, None)
                        reviewed.pop("expires_at", None)
                        tx.set(reference, reviewed)
                        return None

                    attempt = attempts + 1
                    lease_expires_at = checked_at + timedelta(seconds=lease_seconds)
                    leased = dict(data)
                    leased[PROVIDER_RECOVERY_ATTEMPTS_FIELD] = attempt
                    leased[NEXT_ATTEMPT_AT_FIELD] = lease_expires_at
                    leased[PROVIDER_RECOVERY_LEASE_FIELD] = _lease_to_dict(
                        owner=normalized_owner,
                        lease_id=lease_id,
                        expires_at=lease_expires_at,
                    )
                    tx.set(reference, leased)
                    return LeasedProviderRecovery(
                        kind=kind,
                        contractor_id=contractor_id,
                        customer_key=preparation.customer_key,
                        request_id=request_id,
                        owner=normalized_owner,
                        lease_id=lease_id,
                        lease_expires_at=lease_expires_at,
                        attempt=attempt,
                        preparation=preparation,
                    )

                return _transaction(transaction)

            lease = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, _claim_one),
                timeout=IO_TIMEOUT_SECONDS,
            )
            if lease is not None:
                claimed.append(lease)
        return tuple(claimed)

    async def finalize_provider_recovery(self, lease: LeasedProviderRecovery) -> bool:
        """CAS-finalize a provider-confirmed proposal owned by ``lease``."""

        _validate_recovery_lease(lease)
        db = self._db()
        ref = _collection(db, lease.contractor_id).document(lease.request_id)

        def _write() -> bool:
            transaction = db.transaction()

            @firestore.transactional
            def _transaction(tx) -> bool:
                snapshot = ref.get(transaction=tx)
                if not snapshot.exists:
                    return False
                data = snapshot.to_dict() or {}
                if not _lease_matches(data, lease):
                    return False
                kind, preparation = _recovery_preparation(
                    snapshot,
                    contractor_id=lease.contractor_id,
                    request_id=lease.request_id,
                )
                if kind != lease.kind or preparation.logical_operation_id != (
                    lease.preparation.logical_operation_id
                ):
                    raise ServiceRequestRepositoryConflict(
                        "provider recovery lease proposal changed"
                    )
                if kind == "create":
                    if not isinstance(preparation, PreparedProviderCreate):
                        raise ServiceRequestRepositoryConflict(
                            "provider create recovery type is invalid"
                        )
                    finalized = _request_fields(
                        preparation.result.request,
                        customer_key=lease.customer_key,
                        execution_kind=ExecutionKind.PROVIDER,
                    )
                    finalized[PROVIDER_BINDING_FIELD] = _binding_to_dict(preparation.binding)
                    finalized[FINALIZED_PROVIDER_CREATE_RECEIPT_FIELD] = (
                        _finalized_create_receipt_data(preparation)
                    )
                    tx.set(ref, finalized)
                    return True

                if not isinstance(preparation, PreparedProviderOperation):
                    raise ServiceRequestRepositoryConflict(
                        "provider operation recovery type is invalid"
                    )
                updated = dict(data)
                updated.update(
                    _request_fields(
                        preparation.result.request,
                        customer_key=lease.customer_key,
                        execution_kind=ExecutionKind.PROVIDER,
                    )
                )
                updated.pop(PENDING_PROVIDER_OPERATION_FIELD, None)
                updated.pop(PROVIDER_RECOVERY_STATE_FIELD, None)
                updated.pop(PROVIDER_RECOVERY_ATTEMPTS_FIELD, None)
                updated.pop(NEXT_ATTEMPT_AT_FIELD, None)
                updated.pop(PROVIDER_RECOVERY_LEASE_FIELD, None)
                tx.set(ref, updated)
                return True

            return _transaction(transaction)

        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _write),
            timeout=IO_TIMEOUT_SECONDS,
        )

    async def release_provider_recovery(
        self,
        lease: LeasedProviderRecovery,
        *,
        now: datetime,
        max_attempts: int = MAX_PROVIDER_RECOVERY_ATTEMPTS,
    ) -> bool:
        """Release one failed lease with backoff or route it to manual review."""

        _validate_recovery_lease(lease)
        checked_at = _required_aware_datetime(now, "provider recovery clock")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 50
        ):
            raise ValueError("max_attempts must be between 1 and 50")
        db = self._db()
        ref = _collection(db, lease.contractor_id).document(lease.request_id)

        def _write() -> bool:
            transaction = db.transaction()

            @firestore.transactional
            def _transaction(tx) -> bool:
                snapshot = ref.get(transaction=tx)
                if not snapshot.exists:
                    return False
                data = snapshot.to_dict() or {}
                if not _lease_matches(data, lease):
                    return False
                kind, preparation = _recovery_preparation(
                    snapshot,
                    contractor_id=lease.contractor_id,
                    request_id=lease.request_id,
                )
                if kind != lease.kind or preparation.logical_operation_id != (
                    lease.preparation.logical_operation_id
                ):
                    raise ServiceRequestRepositoryConflict(
                        "provider recovery lease proposal changed"
                    )
                released = dict(data)
                released.pop(PROVIDER_RECOVERY_LEASE_FIELD, None)
                released.pop("expires_at", None)
                if lease.attempt >= max_attempts:
                    released[PROVIDER_RECOVERY_STATE_FIELD] = PROVIDER_RECOVERY_NEEDS_REVIEW
                    released[NEXT_ATTEMPT_AT_FIELD] = checked_at
                else:
                    released[PROVIDER_RECOVERY_STATE_FIELD] = PROVIDER_RECOVERY_PENDING
                    released[NEXT_ATTEMPT_AT_FIELD] = checked_at + _recovery_backoff(lease.attempt)
                tx.set(ref, released)
                return True

            return _transaction(transaction)

        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _write),
            timeout=IO_TIMEOUT_SECONDS,
        )
