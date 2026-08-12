"""Repository contract and application service for service-request commands.

This module is deliberately provider- and database-neutral.  The in-memory repository
implements the same customer binding, optimistic concurrency, and idempotency behavior
required of durable adapters.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

from app.services.service_request import (
    ServiceRequest,
    ServiceRequestCommandResult,
    ServiceRequestConcurrencyError,
    ServiceRequestError,
    ServiceRequestIdempotencyConflict,
    ServiceRequestOperation,
)
from app.utils.phone import normalize_phone, phone_hash


class ServiceRequestRepositoryConflict(RuntimeError):
    """Raised when a repository precondition or customer binding does not match."""


class ServiceRequestProviderBindingRequired(ServiceRequestRepositoryConflict):
    """Raised when a provider-bound request bypasses two-phase mutation."""


class ExecutionKind(str, Enum):
    """Immutable authority for how a canonical service request is executed."""

    LOCAL = "local"
    PROVIDER = "provider"


class ServiceRequestCommandStatus(str, Enum):
    APPLIED = "applied"
    PENDING_PROVIDER = "pending_provider"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    FAILED = "failed"


@dataclass(frozen=True)
class ProviderBinding:
    """Opaque link to a request owned by an external scheduling provider."""

    kind: str
    resource_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _required_identifier(self.kind, "provider kind"))
        object.__setattr__(
            self,
            "resource_id",
            _required_identifier(self.resource_id, "provider resource_id"),
        )
        if len(self.kind) > 40:
            raise ValueError("provider kind exceeds 40 characters")
        if len(self.resource_id) > 512:
            raise ValueError("provider resource_id exceeds 512 characters")


class ProviderMutationAdapter(Protocol):
    """Idempotent external mutations; ``True`` means provider-confirmed."""

    def create(
        self,
        *,
        binding: ProviderBinding,
        request: ServiceRequest,
        title: str,
        description: str,
        idempotency_key: str,
    ) -> Awaitable[bool]: ...

    def cancel(
        self,
        *,
        binding: ProviderBinding,
        request: ServiceRequest,
        idempotency_key: str,
    ) -> Awaitable[bool]: ...

    def reschedule(
        self,
        *,
        binding: ProviderBinding,
        request: ServiceRequest,
        scheduled_start: datetime,
        scheduled_end: datetime,
        idempotency_key: str,
    ) -> Awaitable[bool]: ...

    def add_service(
        self,
        *,
        binding: ProviderBinding,
        request: ServiceRequest,
        service: str,
        idempotency_key: str,
    ) -> Awaitable[bool]: ...


@dataclass(frozen=True)
class StoredServiceRequest:
    contractor_id: str
    customer_key: str
    request: ServiceRequest
    execution_kind: ExecutionKind


@dataclass(frozen=True, slots=True)
class ServiceRequestExecutionTarget:
    """One atomic routing snapshot for an existing canonical request."""

    request: ServiceRequest
    execution_kind: ExecutionKind
    provider_binding: ProviderBinding | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, ServiceRequest):
            raise TypeError("request must be a ServiceRequest")
        if not isinstance(self.execution_kind, ExecutionKind):
            raise TypeError("execution_kind must be an ExecutionKind")
        if self.execution_kind is ExecutionKind.LOCAL:
            if self.provider_binding is not None:
                raise ValueError("local execution cannot have a provider binding")
        elif not isinstance(self.provider_binding, ProviderBinding):
            raise ValueError("provider execution requires a provider binding")


@dataclass(frozen=True)
class ServiceRequestCommandResponse:
    """Closed result consumed by ``ReceptionistToolExecutor``."""

    status: ServiceRequestCommandStatus
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class PreparedProviderOperation:
    """Repository-issued proposal that has not changed canonical request state."""

    logical_operation_id: str
    contractor_id: str
    customer_key: str
    request_id: str
    idempotency_key: str
    binding: ProviderBinding
    base_request: ServiceRequest
    result: ServiceRequestCommandResult
    command_fingerprint: str
    operation: ServiceRequestOperation
    arguments: tuple[tuple[str, str], ...]
    base_revision: int
    already_applied: bool = False

    @property
    def token(self) -> str:
        """Compatibility alias for callers that treated the old token as opaque."""

        return self.logical_operation_id


@dataclass(frozen=True, slots=True)
class PreparedProviderCreate:
    """Durable provider-create proposal with no canonical aggregate yet.

    ``idempotency_key`` is retained only because the proposed domain aggregate
    records the first transport attempt.  Provider idempotency always uses
    ``logical_operation_id``, which is derived solely from normalized semantics.
    """

    logical_operation_id: str
    contractor_id: str
    customer_key: str
    request_id: str
    idempotency_key: str
    binding: ProviderBinding
    result: ServiceRequestCommandResult
    command_fingerprint: str
    semantic_fingerprint: str
    title: str
    description: str
    base_revision: int = 0
    already_applied: bool = False

    @property
    def token(self) -> str:
        """Opaque compatibility token for durable repository adapters."""

        return self.logical_operation_id


@dataclass(frozen=True, slots=True)
class FinalizedProviderCreateReceipt:
    """Durable proof that a provider create was atomically finalized."""

    logical_operation_id: str
    contractor_id: str
    customer_key: str
    request_id: str
    semantic_fingerprint: str
    binding: ProviderBinding
    result: ServiceRequestCommandResult


ServiceRequestMutation = Callable[[ServiceRequest | None], ServiceRequestCommandResult]
ProviderInvocation = Callable[[ProviderMutationAdapter, PreparedProviderOperation], Awaitable[bool]]


class ServiceRequestRepository(Protocol):
    """Atomic store for tenant- and customer-scoped service requests."""

    async def get(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
    ) -> ServiceRequest | None: ...

    async def apply(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
        mutation: ServiceRequestMutation,
    ) -> ServiceRequestCommandResult: ...

    async def list_actionable(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        limit: int = 5,
    ) -> tuple[ServiceRequest, ...]: ...

    async def get_execution_target(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
    ) -> ServiceRequestExecutionTarget | None: ...

    async def bind_provider(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
        binding: ProviderBinding,
    ) -> None: ...

    async def get_provider_binding(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
    ) -> ProviderBinding | None: ...

    async def prepare_provider_operation(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
        idempotency_key: str,
        mutation: ServiceRequestMutation,
    ) -> PreparedProviderOperation: ...

    async def finalize_provider_operation(
        self,
        preparation: PreparedProviderOperation,
    ) -> ServiceRequestCommandResult: ...

    async def recover_provider_operation(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
    ) -> PreparedProviderOperation | None: ...

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
    ) -> PreparedProviderCreate: ...

    async def finalize_provider_create(
        self,
        preparation: PreparedProviderCreate,
    ) -> ServiceRequestCommandResult: ...

    async def recover_provider_create(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
    ) -> PreparedProviderCreate | None: ...


class InMemoryServiceRequestRepository:
    """Concurrency-safe reference repository for tests and sandbox composition."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], StoredServiceRequest] = {}
        self._provider_bindings: dict[tuple[str, str], ProviderBinding] = {}
        self._pending_provider_operations: dict[tuple[str, str], PreparedProviderOperation] = {}
        self._pending_provider_creates: dict[tuple[str, str], PreparedProviderCreate] = {}
        self._finalized_provider_creates: dict[tuple[str, str], FinalizedProviderCreateReceipt] = {}
        self._lock = asyncio.Lock()

    async def get(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
    ) -> ServiceRequest | None:
        contractor = _required_identifier(contractor_id, "contractor_id")
        customer = _required_customer_key(customer_key)
        request = _required_identifier(request_id, "request_id")
        async with self._lock:
            target = self._execution_target_locked(
                storage_key=(contractor, request),
                customer_key=customer,
            )
            if target is None:
                return None
            return target.request

    async def apply(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
        mutation: ServiceRequestMutation,
    ) -> ServiceRequestCommandResult:
        contractor = _required_identifier(contractor_id, "contractor_id")
        customer = _required_customer_key(customer_key)
        request = _required_identifier(request_id, "request_id")
        if not callable(mutation):
            raise TypeError("mutation must be callable")

        async with self._lock:
            storage_key = (contractor, request)
            stored = self._records.get(storage_key)
            if stored is not None and stored.customer_key != customer:
                raise ServiceRequestRepositoryConflict(
                    "service request does not belong to this customer"
                )
            current = stored.request if stored is not None else None
            if storage_key in self._pending_provider_creates:
                raise ServiceRequestRepositoryConflict(
                    "a provider create is pending for this service request"
                )
            if storage_key in self._pending_provider_operations:
                raise ServiceRequestRepositoryConflict(
                    "a provider mutation is pending for this service request"
                )
            if current is not None:
                target = self._execution_target_locked(
                    storage_key=storage_key,
                    customer_key=customer,
                )
                if target is None:
                    raise ServiceRequestRepositoryConflict("service request was not found")
                if target.execution_kind is ExecutionKind.PROVIDER:
                    raise ServiceRequestProviderBindingRequired(
                        "provider requests require two-phase mutation"
                    )
            result = mutation(current)
            if not isinstance(result, ServiceRequestCommandResult):
                raise TypeError("mutation must return ServiceRequestCommandResult")
            if result.request.request_id != request:
                raise ServiceRequestRepositoryConflict("mutation returned a different request_id")
            # A replay after later commands returns the current aggregate together with
            # the earlier outcome. Persist the current aggregate, never the old outcome.
            self._records[storage_key] = StoredServiceRequest(
                contractor_id=contractor,
                customer_key=customer,
                request=result.request,
                execution_kind=ExecutionKind.LOCAL,
            )
            return result

    async def list_actionable(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        limit: int = 5,
    ) -> tuple[ServiceRequest, ...]:
        contractor = _required_identifier(contractor_id, "contractor_id")
        customer = _required_customer_key(customer_key)
        bounded_limit = _bounded_limit(limit)
        async with self._lock:
            requests: list[ServiceRequest] = []
            for storage_key, stored in self._records.items():
                if stored.contractor_id != contractor or stored.customer_key != customer:
                    continue
                try:
                    target = self._execution_target_locked(
                        storage_key=storage_key,
                        customer_key=customer,
                    )
                except ServiceRequestRepositoryConflict:
                    continue
                if target is not None and target.request.status.value == "open":
                    requests.append(target.request)
            requests.sort(
                key=lambda request: (request.updated_at, request.request_id),
                reverse=True,
            )
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
        if not isinstance(binding, ProviderBinding):
            raise TypeError("binding must be a ProviderBinding")
        async with self._lock:
            storage_key = (contractor, request)
            stored = self._records.get(storage_key)
            if stored is None or stored.customer_key != customer:
                raise ServiceRequestRepositoryConflict("service request was not found")
            if storage_key in self._pending_provider_operations:
                raise ServiceRequestRepositoryConflict(
                    "cannot replace a provider binding while a mutation is pending"
                )
            target = self._execution_target_locked(
                storage_key=storage_key,
                customer_key=customer,
            )
            if target is None:
                raise ServiceRequestRepositoryConflict("service request was not found")
            if target.execution_kind is ExecutionKind.LOCAL:
                raise ServiceRequestRepositoryConflict(
                    "local execution provenance cannot be upgraded to provider"
                )
            if target.provider_binding != binding:
                raise ServiceRequestRepositoryConflict(
                    "service request already has a different provider binding"
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
        async with self._lock:
            storage_key = (contractor, request)
            target = self._execution_target_locked(
                storage_key=storage_key,
                customer_key=customer,
            )
            if target is None:
                return None
            return target.provider_binding

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
        async with self._lock:
            return self._execution_target_locked(
                storage_key=(contractor, request),
                customer_key=customer,
            )

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

        async with self._lock:
            storage_key = (contractor, request)
            stored = self._records.get(storage_key)
            if stored is None or stored.customer_key != customer:
                raise ServiceRequestRepositoryConflict("service request was not found")
            target = self._execution_target_locked(
                storage_key=storage_key,
                customer_key=customer,
            )
            if target is None or target.execution_kind is not ExecutionKind.PROVIDER:
                raise ServiceRequestRepositoryConflict("service request is not provider-backed")
            binding = target.provider_binding
            if binding is None:  # Defensive: the target constructor already enforces this.
                raise ServiceRequestRepositoryConflict("service request has no provider binding")

            # Evaluate the domain command against canonical state while holding the
            # repository lock. This validates expected revision and idempotency before
            # any provider side effect is attempted.
            result = mutation(stored.request)
            if not isinstance(result, ServiceRequestCommandResult):
                raise TypeError("mutation must return ServiceRequestCommandResult")
            if result.request.request_id != request:
                raise ServiceRequestRepositoryConflict("mutation returned a different request_id")
            preparation = _prepare_provider_operation(
                contractor_id=contractor,
                customer_key=customer,
                request_id=request,
                idempotency_key=key,
                binding=binding,
                base_request=stored.request,
                result=result,
            )

            existing = self._pending_provider_operations.get(storage_key)
            if existing is not None:
                if (
                    existing.command_fingerprint != preparation.command_fingerprint
                    or existing.base_revision != preparation.base_revision
                    or existing.operation is not preparation.operation
                    or existing.arguments != preparation.arguments
                ):
                    raise ServiceRequestRepositoryConflict(
                        "a different provider mutation is already pending"
                    )
                return existing

            if not preparation.already_applied:
                self._pending_provider_operations[storage_key] = preparation
            return preparation

    async def finalize_provider_operation(
        self,
        preparation: PreparedProviderOperation,
    ) -> ServiceRequestCommandResult:
        if not isinstance(preparation, PreparedProviderOperation):
            raise TypeError("preparation must be a PreparedProviderOperation")
        _validate_prepared_provider_operation(preparation)
        async with self._lock:
            storage_key = (preparation.contractor_id, preparation.request_id)
            stored = self._records.get(storage_key)
            if stored is None or stored.customer_key != preparation.customer_key:
                raise ServiceRequestRepositoryConflict("service request was not found")
            if preparation.already_applied:
                target = self._execution_target_locked(
                    storage_key=storage_key,
                    customer_key=preparation.customer_key,
                )
                applied = _applied_result_by_fingerprint(
                    stored.request,
                    preparation.command_fingerprint,
                )
                if target is None or target.execution_kind is not ExecutionKind.PROVIDER:
                    raise ServiceRequestRepositoryConflict("service request is not provider-backed")
                if applied is None:
                    raise ServiceRequestRepositoryConflict(
                        "applied provider operation no longer matches canonical state"
                    )
                return applied
            pending = self._pending_provider_operations.get(storage_key)
            if pending is None:
                applied = _applied_result_by_fingerprint(
                    stored.request,
                    preparation.command_fingerprint,
                )
                if applied is not None:
                    return applied
                raise ServiceRequestRepositoryConflict(
                    "provider operation is not the current prepared mutation"
                )
            if pending.logical_operation_id != preparation.logical_operation_id:
                raise ServiceRequestRepositoryConflict(
                    "provider operation is not the current prepared mutation"
                )
            if (
                stored.request != pending.base_request
                or (
                    target := self._execution_target_locked(
                        storage_key=storage_key,
                        customer_key=preparation.customer_key,
                    )
                )
                is None
                or target.execution_kind is not ExecutionKind.PROVIDER
                or target.provider_binding != pending.binding
            ):
                raise ServiceRequestRepositoryConflict(
                    "service request changed before provider finalization"
                )
            self._records[storage_key] = StoredServiceRequest(
                contractor_id=pending.contractor_id,
                customer_key=pending.customer_key,
                request=pending.result.request,
                execution_kind=ExecutionKind.PROVIDER,
            )
            del self._pending_provider_operations[storage_key]
            return pending.result

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
        async with self._lock:
            storage_key = (contractor, request)
            stored = self._records.get(storage_key)
            if stored is None or stored.customer_key != customer:
                return None
            pending = self._pending_provider_operations.get(storage_key)
            if pending is None:
                return None
            target = self._execution_target_locked(
                storage_key=storage_key,
                customer_key=customer,
            )
            if (
                pending.base_request != stored.request
                or target is None
                or target.execution_kind is not ExecutionKind.PROVIDER
                or pending.binding != target.provider_binding
            ):
                raise ServiceRequestRepositoryConflict(
                    "pending provider operation no longer matches canonical state"
                )
            return pending

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

        # Validate and canonicalize the complete create command before taking the
        # repository lock. The result remains a proposal until provider confirmation.
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

        async with self._lock:
            storage_key = (contractor, request)
            stored = self._records.get(storage_key)
            if stored is not None:
                if stored.customer_key != customer:
                    raise ServiceRequestRepositoryConflict(
                        "service request does not belong to this customer"
                    )
                target = self._execution_target_locked(
                    storage_key=storage_key,
                    customer_key=customer,
                )
                receipt = self._finalized_provider_creates.get(storage_key)
                if receipt is None:
                    raise ServiceRequestRepositoryConflict("service request already exists")
                if (
                    target is None
                    or target.execution_kind is not ExecutionKind.PROVIDER
                    or receipt.semantic_fingerprint != preparation.semantic_fingerprint
                    or receipt.binding != preparation.binding
                    or self._provider_bindings.get(storage_key) != receipt.binding
                    or not _receipt_is_present_in_request(receipt, stored.request)
                ):
                    raise ServiceRequestRepositoryConflict(
                        "service request was finalized with different create semantics"
                    )
                return PreparedProviderCreate(
                    logical_operation_id=receipt.logical_operation_id,
                    contractor_id=preparation.contractor_id,
                    customer_key=preparation.customer_key,
                    request_id=preparation.request_id,
                    # Keep the original receipt result and transport key together so
                    # its idempotency record remains internally consistent.
                    idempotency_key=receipt.result.request.idempotency_records[0].idempotency_key,
                    binding=receipt.binding,
                    result=receipt.result,
                    command_fingerprint=receipt.result.request.idempotency_records[
                        0
                    ].command_fingerprint,
                    semantic_fingerprint=receipt.semantic_fingerprint,
                    title=preparation.title,
                    description=preparation.description,
                    already_applied=True,
                )
            if storage_key in self._pending_provider_operations:
                raise ServiceRequestRepositoryConflict(
                    "a provider mutation is pending for this service request"
                )
            existing = self._pending_provider_creates.get(storage_key)
            if existing is not None:
                if existing.semantic_fingerprint != preparation.semantic_fingerprint:
                    raise ServiceRequestRepositoryConflict(
                        "a different provider create is already pending"
                    )
                return existing
            self._pending_provider_creates[storage_key] = preparation
            return preparation

    async def finalize_provider_create(
        self,
        preparation: PreparedProviderCreate,
    ) -> ServiceRequestCommandResult:
        if not isinstance(preparation, PreparedProviderCreate):
            raise TypeError("preparation must be a PreparedProviderCreate")
        _validate_prepared_provider_create(preparation)
        async with self._lock:
            storage_key = (preparation.contractor_id, preparation.request_id)
            stored = self._records.get(storage_key)
            if stored is not None:
                target = self._execution_target_locked(
                    storage_key=storage_key,
                    customer_key=preparation.customer_key,
                )
                receipt = self._finalized_provider_creates.get(storage_key)
                if (
                    target is not None
                    and target.execution_kind is ExecutionKind.PROVIDER
                    and receipt is not None
                    and receipt.logical_operation_id == preparation.logical_operation_id
                    and self._provider_bindings.get(storage_key) == receipt.binding
                    and _receipt_is_present_in_request(receipt, stored.request)
                ):
                    return receipt.result
                raise ServiceRequestRepositoryConflict("service request already exists")
            pending = self._pending_provider_creates.get(storage_key)
            if pending is None or pending.logical_operation_id != preparation.logical_operation_id:
                raise ServiceRequestRepositoryConflict(
                    "provider create is not the current prepared operation"
                )
            if storage_key in self._provider_bindings:
                raise ServiceRequestRepositoryConflict(
                    "provider binding appeared before create finalization"
                )
            # The aggregate and binding become visible together under one lock.
            self._records[storage_key] = StoredServiceRequest(
                contractor_id=pending.contractor_id,
                customer_key=pending.customer_key,
                request=pending.result.request,
                execution_kind=ExecutionKind.PROVIDER,
            )
            self._provider_bindings[storage_key] = pending.binding
            self._finalized_provider_creates[storage_key] = FinalizedProviderCreateReceipt(
                logical_operation_id=pending.logical_operation_id,
                contractor_id=pending.contractor_id,
                customer_key=pending.customer_key,
                request_id=pending.request_id,
                semantic_fingerprint=pending.semantic_fingerprint,
                binding=pending.binding,
                result=pending.result,
            )
            del self._pending_provider_creates[storage_key]
            return pending.result

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
        async with self._lock:
            storage_key = (contractor, request)
            stored = self._records.get(storage_key)
            if stored is not None:
                if stored.customer_key != customer:
                    return None
                return None
            pending = self._pending_provider_creates.get(storage_key)
            if pending is None or pending.customer_key != customer:
                return None
            if storage_key in self._provider_bindings:
                raise ServiceRequestRepositoryConflict(
                    "pending provider create already has a visible binding"
                )
            _validate_prepared_provider_create(pending)
            return pending

    def _execution_target_locked(
        self,
        *,
        storage_key: tuple[str, str],
        customer_key: str,
    ) -> ServiceRequestExecutionTarget | None:
        """Validate immutable provenance while the repository lock is held."""

        stored = self._records.get(storage_key)
        if stored is None or stored.customer_key != customer_key:
            return None
        kind = stored.execution_kind
        binding = self._provider_bindings.get(storage_key)
        receipt = self._finalized_provider_creates.get(storage_key)
        if kind is ExecutionKind.LOCAL:
            if binding is not None or receipt is not None:
                raise ServiceRequestRepositoryConflict(
                    "local execution provenance conflicts with provider state"
                )
            return ServiceRequestExecutionTarget(
                request=stored.request,
                execution_kind=ExecutionKind.LOCAL,
            )
        if kind is not ExecutionKind.PROVIDER:
            raise ServiceRequestRepositoryConflict(
                "service request execution provenance is invalid"
            )
        if (
            binding is None
            or receipt is None
            or receipt.binding != binding
            or not _receipt_is_present_in_request(receipt, stored.request)
        ):
            raise ServiceRequestRepositoryConflict(
                "provider execution provenance is incomplete or corrupt"
            )
        return ServiceRequestExecutionTarget(
            request=stored.request,
            execution_kind=ExecutionKind.PROVIDER,
            provider_binding=binding,
        )


class ServiceRequestCommandService:
    """Application facade matching ``ReceptionistToolExecutor`` method names."""

    def __init__(
        self,
        repository: ServiceRequestRepository,
        *,
        provider_adapter: ProviderMutationAdapter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._provider_adapter = provider_adapter
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create_service_request(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
        services: Sequence[str],
        scheduled_start: datetime | str,
        scheduled_end: datetime | str,
        expected_revision: int,
        idempotency_key: str,
    ) -> ServiceRequestCommandResponse:
        return await self._execute(
            contractor_id=contractor_id,
            caller_phone=caller_phone,
            request_id=request_id,
            mutation=lambda current: ServiceRequest.create(
                request_id=request_id,
                services=services,
                scheduled_start=_parse_datetime(scheduled_start, "scheduled_start"),
                scheduled_end=_parse_datetime(scheduled_end, "scheduled_end"),
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                occurred_at=self._now(),
                existing=current,
            ),
        )

    async def create_provider_service_request(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
        services: Sequence[str],
        scheduled_start: datetime | str,
        scheduled_end: datetime | str,
        expected_revision: int,
        idempotency_key: str,
        binding: ProviderBinding,
        title: str,
        description: str,
    ) -> ServiceRequestCommandResponse:
        """Create externally first, exposing aggregate and binding only after confirmation."""

        customer_key = _try_customer_key(caller_phone)
        if customer_key is None:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        try:
            preparation = await self._repository.prepare_provider_create(
                contractor_id=contractor_id,
                customer_key=customer_key,
                request_id=request_id,
                idempotency_key=idempotency_key,
                binding=binding,
                title=title,
                description=description,
                mutation=lambda current: ServiceRequest.create(
                    request_id=request_id,
                    services=services,
                    scheduled_start=_parse_datetime(scheduled_start, "scheduled_start"),
                    scheduled_end=_parse_datetime(scheduled_end, "scheduled_end"),
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                    occurred_at=self._now(),
                    existing=current,
                ),
            )
        except (ServiceRequestConcurrencyError, ServiceRequestIdempotencyConflict):
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.CONFLICT)
        except ServiceRequestRepositoryConflict:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.CONFLICT)
        except ServiceRequestError:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        except Exception:  # noqa: BLE001 - application boundary returns a closed status
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        if preparation.already_applied:
            return _applied_response(preparation.result)
        return await self._confirm_prepared_provider_create(preparation)

    async def cancel_service_request(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> ServiceRequestCommandResponse:
        return await self._execute_existing(
            contractor_id=contractor_id,
            caller_phone=caller_phone,
            request_id=request_id,
            idempotency_key=idempotency_key,
            mutation=lambda current: current.cancel(
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                occurred_at=self._now(),
            ),
            provider_invocation=lambda adapter, preparation: adapter.cancel(
                binding=preparation.binding,
                request=preparation.base_request,
                idempotency_key=preparation.logical_operation_id,
            ),
        )

    async def reschedule_service_request(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
        expected_revision: int,
        idempotency_key: str,
        scheduled_start: datetime | str,
        scheduled_end: datetime | str,
    ) -> ServiceRequestCommandResponse:
        return await self._execute_existing(
            contractor_id=contractor_id,
            caller_phone=caller_phone,
            request_id=request_id,
            idempotency_key=idempotency_key,
            mutation=lambda current: current.reschedule(
                scheduled_start=_parse_datetime(scheduled_start, "scheduled_start"),
                scheduled_end=_parse_datetime(scheduled_end, "scheduled_end"),
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                occurred_at=self._now(),
            ),
            provider_invocation=lambda adapter, preparation: adapter.reschedule(
                binding=preparation.binding,
                request=preparation.base_request,
                scheduled_start=preparation.result.request.scheduled_start,
                scheduled_end=preparation.result.request.scheduled_end,
                idempotency_key=preparation.logical_operation_id,
            ),
        )

    async def add_service_to_request(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
        expected_revision: int,
        idempotency_key: str,
        service: str,
    ) -> ServiceRequestCommandResponse:
        return await self._execute_existing(
            contractor_id=contractor_id,
            caller_phone=caller_phone,
            request_id=request_id,
            idempotency_key=idempotency_key,
            mutation=lambda current: current.add_service(
                service=service,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                occurred_at=self._now(),
            ),
            provider_invocation=lambda adapter, preparation: adapter.add_service(
                binding=preparation.binding,
                request=preparation.base_request,
                service=preparation.result.request.services[-1],
                idempotency_key=preparation.logical_operation_id,
            ),
        )

    async def recover_provider_operation(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
    ) -> ServiceRequestCommandResponse:
        """Replay one durable proposal after a restart, then CAS-finalize it."""

        customer_key = _try_customer_key(caller_phone)
        if customer_key is None:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        try:
            preparation = await self._repository.recover_provider_operation(
                contractor_id=contractor_id,
                customer_key=customer_key,
                request_id=request_id,
            )
        except ServiceRequestRepositoryConflict:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.CONFLICT)
        except Exception:  # noqa: BLE001 - recovery boundary returns a closed status
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        if preparation is None:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.NOT_FOUND)
        return await self._confirm_prepared_provider_operation(preparation)

    async def recover_provider_create(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
    ) -> ServiceRequestCommandResponse:
        """Replay one durable provider-create proposal after a restart."""

        customer_key = _try_customer_key(caller_phone)
        if customer_key is None:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        try:
            preparation = await self._repository.recover_provider_create(
                contractor_id=contractor_id,
                customer_key=customer_key,
                request_id=request_id,
            )
        except ServiceRequestRepositoryConflict:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.CONFLICT)
        except Exception:  # noqa: BLE001 - recovery boundary returns a closed status
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        if preparation is None:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.NOT_FOUND)
        return await self._confirm_prepared_provider_create(preparation)

    async def list_actionable(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        limit: int = 5,
    ) -> tuple[ServiceRequest, ...]:
        try:
            customer_key = customer_key_for_phone(caller_phone)
            return await self._repository.list_actionable(
                contractor_id=contractor_id,
                customer_key=customer_key,
                limit=limit,
            )
        except Exception:  # noqa: BLE001 - reads fail closed without leaking identity
            return ()

    async def _execute_existing(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
        idempotency_key: str,
        mutation: Callable[[ServiceRequest], ServiceRequestCommandResult],
        provider_invocation: ProviderInvocation,
    ) -> ServiceRequestCommandResponse:
        customer_key = _try_customer_key(caller_phone)
        if customer_key is None:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        try:
            target = await self._repository.get_execution_target(
                contractor_id=contractor_id,
                customer_key=customer_key,
                request_id=request_id,
            )
        except Exception:  # noqa: BLE001 - repository details are not caller-safe
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        if target is None:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.NOT_FOUND)
        if target.execution_kind is ExecutionKind.PROVIDER:
            return await self._execute_provider_operation(
                contractor_id=contractor_id,
                customer_key=customer_key,
                request_id=request_id,
                idempotency_key=idempotency_key,
                mutation=lambda latest: mutation(_require_present(latest)),
                provider_invocation=provider_invocation,
            )
        if target.execution_kind is not ExecutionKind.LOCAL:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        return await self._execute(
            contractor_id=contractor_id,
            caller_phone=caller_phone,
            request_id=request_id,
            mutation=lambda _latest: mutation(_require_present(_latest)),
        )

    async def _execute_provider_operation(
        self,
        *,
        contractor_id: str,
        customer_key: str,
        request_id: str,
        idempotency_key: str,
        mutation: ServiceRequestMutation,
        provider_invocation: ProviderInvocation,
    ) -> ServiceRequestCommandResponse:
        try:
            preparation = await self._repository.prepare_provider_operation(
                contractor_id=contractor_id,
                customer_key=customer_key,
                request_id=request_id,
                idempotency_key=idempotency_key,
                mutation=mutation,
            )
        except (ServiceRequestConcurrencyError, ServiceRequestIdempotencyConflict):
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.CONFLICT)
        except ServiceRequestRepositoryConflict:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.CONFLICT)
        except ServiceRequestError:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        except Exception:  # noqa: BLE001 - application boundary returns a closed status
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)

        if preparation.already_applied:
            return _applied_response(preparation.result)
        return await self._confirm_prepared_provider_operation(
            preparation,
            provider_invocation=provider_invocation,
        )

    async def _confirm_prepared_provider_operation(
        self,
        preparation: PreparedProviderOperation,
        *,
        provider_invocation: ProviderInvocation | None = None,
    ) -> ServiceRequestCommandResponse:
        adapter = self._provider_adapter
        if adapter is None:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.PENDING_PROVIDER)
        try:
            if provider_invocation is None:
                confirmed = await _invoke_prepared_provider_operation(adapter, preparation)
            else:
                confirmed = await provider_invocation(adapter, preparation)
        except Exception:  # noqa: BLE001 - provider details are not caller-safe
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.PENDING_PROVIDER)
        if confirmed is not True:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.PENDING_PROVIDER)
        try:
            result = await self._repository.finalize_provider_operation(preparation)
        except Exception:  # noqa: BLE001 - do not claim an uncommitted canonical change
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        return _applied_response(result)

    async def _confirm_prepared_provider_create(
        self,
        preparation: PreparedProviderCreate,
    ) -> ServiceRequestCommandResponse:
        adapter = self._provider_adapter
        if adapter is None:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.PENDING_PROVIDER)
        try:
            confirmed = await adapter.create(
                binding=preparation.binding,
                request=preparation.result.request,
                title=preparation.title,
                description=preparation.description,
                idempotency_key=preparation.logical_operation_id,
            )
        except Exception:  # noqa: BLE001 - provider details are not caller-safe
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.PENDING_PROVIDER)
        if confirmed is not True:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.PENDING_PROVIDER)
        try:
            result = await self._repository.finalize_provider_create(preparation)
        except Exception:  # noqa: BLE001 - do not claim an uncommitted canonical create
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        return _applied_response(result)

    async def _execute(
        self,
        *,
        contractor_id: str,
        caller_phone: str,
        request_id: str,
        mutation: ServiceRequestMutation,
    ) -> ServiceRequestCommandResponse:
        customer_key = _try_customer_key(caller_phone)
        if customer_key is None:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        try:
            result = await self._repository.apply(
                contractor_id=contractor_id,
                customer_key=customer_key,
                request_id=request_id,
                mutation=mutation,
            )
        except (ServiceRequestConcurrencyError, ServiceRequestIdempotencyConflict):
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.CONFLICT)
        except ServiceRequestProviderBindingRequired:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.CONFLICT)
        except ServiceRequestRepositoryConflict:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.NOT_FOUND)
        except ServiceRequestError:
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        except Exception:  # noqa: BLE001 - application boundary returns a closed status
            return ServiceRequestCommandResponse(ServiceRequestCommandStatus.FAILED)
        return _applied_response(result)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return a datetime")
        return value


def _applied_response(result: ServiceRequestCommandResult) -> ServiceRequestCommandResponse:
    return ServiceRequestCommandResponse(
        ServiceRequestCommandStatus.APPLIED,
        # The outcome revision is stable across retries even if later commands
        # have advanced the aggregate before an old response is retried.
        revision=result.outcome.revision,
    )


def customer_key_for_phone(caller_phone: str) -> str:
    """Return the SHA-256 key of a normalized E.164 phone number."""

    if not isinstance(caller_phone, str):
        raise TypeError("caller_phone must be a string")
    normalized = normalize_phone(caller_phone)
    if not normalized:
        raise ValueError("caller_phone must normalize to E.164")
    return phone_hash(normalized)


def _try_customer_key(caller_phone: str) -> str | None:
    try:
        return customer_key_for_phone(caller_phone)
    except (TypeError, ValueError):
        return None


def _required_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_customer_key(value: str) -> str:
    normalized = _required_identifier(value, "customer_key")
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("customer_key must be a lowercase SHA-256 digest")
    return normalized


def _repository_identity(
    contractor_id: str,
    customer_key: str,
    request_id: str,
) -> tuple[str, str, str]:
    return (
        _required_identifier(contractor_id, "contractor_id"),
        _required_customer_key(customer_key),
        _required_identifier(request_id, "request_id"),
    )


def _result_fingerprint(result: ServiceRequestCommandResult, idempotency_key: str) -> str:
    records = [
        record
        for record in result.request.idempotency_records
        if record.idempotency_key == idempotency_key
    ]
    if len(records) != 1:
        raise ServiceRequestRepositoryConflict(
            "prepared mutation did not contain the requested idempotency record"
        )
    return records[0].command_fingerprint


def _provider_operation_arguments(
    result: ServiceRequestCommandResult,
) -> tuple[tuple[str, str], ...]:
    operation = result.outcome.operation
    if operation is ServiceRequestOperation.CANCEL:
        return ()
    if operation is ServiceRequestOperation.RESCHEDULE:
        return (
            ("scheduled_end", _provider_datetime(result.outcome.scheduled_end)),
            ("scheduled_start", _provider_datetime(result.outcome.scheduled_start)),
        )
    if operation is ServiceRequestOperation.ADD_SERVICE:
        if not result.outcome.services:
            raise ServiceRequestRepositoryConflict("add-service proposal has no service")
        return (("service", result.outcome.services[-1]),)
    raise ServiceRequestRepositoryConflict("provider proposal operation is unsupported")


def _provider_datetime(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ServiceRequestRepositoryConflict("provider proposal timestamp must be aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _logical_provider_operation_id(
    contractor_id: str,
    customer_key: str,
    request_id: str,
    base_revision: int,
    command_fingerprint: str,
) -> str:
    material = (
        f"service-request-provider-operation-v2\x00{contractor_id}\x00"
        f"{customer_key}\x00{request_id}\x00{base_revision}\x00{command_fingerprint}"
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _prepare_provider_operation(
    *,
    contractor_id: str,
    customer_key: str,
    request_id: str,
    idempotency_key: str,
    binding: ProviderBinding,
    base_request: ServiceRequest,
    result: ServiceRequestCommandResult,
) -> PreparedProviderOperation:
    fingerprint = _result_fingerprint(result, idempotency_key)
    base_revision = result.outcome.revision - 1
    if base_revision < 1:
        raise ServiceRequestRepositoryConflict("provider mutation must follow request creation")
    already_applied = _applied_result_by_fingerprint(base_request, fingerprint) is not None
    preparation = PreparedProviderOperation(
        logical_operation_id=_logical_provider_operation_id(
            contractor_id,
            customer_key,
            request_id,
            base_revision,
            fingerprint,
        ),
        contractor_id=contractor_id,
        customer_key=customer_key,
        request_id=request_id,
        idempotency_key=idempotency_key,
        binding=binding,
        base_request=base_request,
        result=result,
        command_fingerprint=fingerprint,
        operation=result.outcome.operation,
        arguments=_provider_operation_arguments(result),
        base_revision=base_revision,
        already_applied=already_applied,
    )
    _validate_prepared_provider_operation(preparation)
    return preparation


def _prepare_provider_create(
    *,
    contractor_id: str,
    customer_key: str,
    request_id: str,
    idempotency_key: str,
    binding: ProviderBinding,
    title: str,
    description: str,
    result: ServiceRequestCommandResult,
) -> PreparedProviderCreate:
    if result.outcome.operation is not ServiceRequestOperation.CREATE:
        raise ServiceRequestRepositoryConflict("provider create requires a create command")
    if result.outcome.revision != 1 or result.request.revision != 1:
        raise ServiceRequestRepositoryConflict("provider create must start at revision one")
    command_fingerprint = _result_fingerprint(result, idempotency_key)
    semantic_fingerprint = _provider_create_semantic_fingerprint(
        command_fingerprint=command_fingerprint,
        binding=binding,
        title=title,
        description=description,
    )
    preparation = PreparedProviderCreate(
        logical_operation_id=_logical_provider_create_id(
            contractor_id,
            customer_key,
            request_id,
            semantic_fingerprint,
        ),
        contractor_id=contractor_id,
        customer_key=customer_key,
        request_id=request_id,
        idempotency_key=idempotency_key,
        binding=binding,
        result=result,
        command_fingerprint=command_fingerprint,
        semantic_fingerprint=semantic_fingerprint,
        title=title,
        description=description,
    )
    _validate_prepared_provider_create(preparation)
    return preparation


def _validate_prepared_provider_create(preparation: PreparedProviderCreate) -> None:
    contractor, customer, request = _repository_identity(
        preparation.contractor_id,
        preparation.customer_key,
        preparation.request_id,
    )
    key = _required_identifier(preparation.idempotency_key, "idempotency_key")
    if not isinstance(preparation.binding, ProviderBinding):
        raise ServiceRequestRepositoryConflict("prepared provider binding is invalid")
    if preparation.result.request.request_id != request:
        raise ServiceRequestRepositoryConflict("prepared aggregate does not match its request path")
    if preparation.result.outcome.operation is not ServiceRequestOperation.CREATE:
        raise ServiceRequestRepositoryConflict("prepared provider create has a non-create result")
    if (
        preparation.base_revision != 0
        or preparation.result.outcome.revision != 1
        or preparation.result.request.revision != 1
    ):
        raise ServiceRequestRepositoryConflict("prepared provider create revision is invalid")
    command_fingerprint = _result_fingerprint(preparation.result, key)
    if preparation.command_fingerprint != command_fingerprint:
        raise ServiceRequestRepositoryConflict(
            "prepared create does not match its command fingerprint"
        )
    title = _normalize_provider_title(preparation.title)
    description = _normalize_provider_description(preparation.description)
    if preparation.title != title or preparation.description != description:
        raise ServiceRequestRepositoryConflict("prepared provider arguments are not normalized")
    semantic_fingerprint = _provider_create_semantic_fingerprint(
        command_fingerprint=command_fingerprint,
        binding=preparation.binding,
        title=title,
        description=description,
    )
    if preparation.semantic_fingerprint != semantic_fingerprint:
        raise ServiceRequestRepositoryConflict("prepared create semantic fingerprint is invalid")
    logical_id = _logical_provider_create_id(
        contractor,
        customer,
        request,
        semantic_fingerprint,
    )
    if preparation.logical_operation_id != logical_id:
        raise ServiceRequestRepositoryConflict("provider create logical operation id is invalid")


def _provider_create_semantic_fingerprint(
    *,
    command_fingerprint: str,
    binding: ProviderBinding,
    title: str,
    description: str,
) -> str:
    material = json.dumps(
        {
            "binding": {"kind": binding.kind, "resource_id": binding.resource_id},
            "command_fingerprint": command_fingerprint,
            "description": description,
            "title": title,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _logical_provider_create_id(
    contractor_id: str,
    customer_key: str,
    request_id: str,
    semantic_fingerprint: str,
) -> str:
    material = (
        f"service-request-provider-create-v1\x00{contractor_id}\x00"
        f"{customer_key}\x00{request_id}\x000\x00{semantic_fingerprint}"
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _normalize_provider_title(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("provider title must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ValueError("provider title must be non-empty")
    if len(normalized) > 200:
        raise ValueError("provider title exceeds 200 characters")
    return normalized


def _normalize_provider_description(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("provider description must be a string")
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(" ".join(line.split()) for line in normalized.split("\n")).strip()
    if len(normalized) > 1500:
        raise ValueError("provider description exceeds 1500 characters")
    return normalized


def _validate_prepared_provider_operation(preparation: PreparedProviderOperation) -> None:
    contractor, customer, request = _repository_identity(
        preparation.contractor_id,
        preparation.customer_key,
        preparation.request_id,
    )
    key = _required_identifier(preparation.idempotency_key, "idempotency_key")
    if not isinstance(preparation.binding, ProviderBinding):
        raise ServiceRequestRepositoryConflict("prepared provider binding is invalid")
    if (
        preparation.base_request.request_id != request
        or preparation.result.request.request_id != request
    ):
        raise ServiceRequestRepositoryConflict("prepared aggregate does not match its request path")
    fingerprint = _result_fingerprint(preparation.result, key)
    if fingerprint != preparation.command_fingerprint:
        raise ServiceRequestRepositoryConflict(
            "prepared result does not match its command fingerprint"
        )
    base_revision = preparation.result.outcome.revision - 1
    if preparation.base_revision != base_revision:
        raise ServiceRequestRepositoryConflict("prepared base revision is invalid")
    if preparation.operation is not preparation.result.outcome.operation:
        raise ServiceRequestRepositoryConflict("prepared operation does not match its result")
    if preparation.arguments != _provider_operation_arguments(preparation.result):
        raise ServiceRequestRepositoryConflict("prepared arguments do not match its result")
    logical_id = _logical_provider_operation_id(
        contractor,
        customer,
        request,
        base_revision,
        fingerprint,
    )
    if preparation.logical_operation_id != logical_id:
        raise ServiceRequestRepositoryConflict("provider logical operation id is invalid")
    if not preparation.already_applied and preparation.base_request.revision != base_revision:
        raise ServiceRequestRepositoryConflict("proposal base does not match canonical revision")


def _applied_result_by_fingerprint(
    request: ServiceRequest,
    command_fingerprint: str,
) -> ServiceRequestCommandResult | None:
    records = [
        record
        for record in request.idempotency_records
        if record.command_fingerprint == command_fingerprint
    ]
    if not records:
        return None
    if len(records) != 1:
        raise ServiceRequestRepositoryConflict("command fingerprint is not unique")
    return ServiceRequestCommandResult(request=request, outcome=records[0].outcome)


def _receipt_is_present_in_request(
    receipt: FinalizedProviderCreateReceipt,
    request: ServiceRequest,
) -> bool:
    if not isinstance(receipt, FinalizedProviderCreateReceipt):
        return False
    records = receipt.result.request.idempotency_records
    if len(records) != 1 or records[0].outcome.operation is not ServiceRequestOperation.CREATE:
        return False
    applied = _applied_result_by_fingerprint(request, records[0].command_fingerprint)
    return applied is not None and applied.outcome == receipt.result.outcome


async def _invoke_prepared_provider_operation(
    adapter: ProviderMutationAdapter,
    preparation: PreparedProviderOperation,
) -> bool:
    arguments = dict(preparation.arguments)
    common = {
        "binding": preparation.binding,
        "request": preparation.base_request,
        "idempotency_key": preparation.logical_operation_id,
    }
    if preparation.operation is ServiceRequestOperation.CANCEL:
        return await adapter.cancel(**common)
    if preparation.operation is ServiceRequestOperation.RESCHEDULE:
        return await adapter.reschedule(
            **common,
            scheduled_start=_parse_datetime(arguments["scheduled_start"], "scheduled_start"),
            scheduled_end=_parse_datetime(arguments["scheduled_end"], "scheduled_end"),
        )
    if preparation.operation is ServiceRequestOperation.ADD_SERVICE:
        return await adapter.add_service(**common, service=arguments["service"])
    raise ServiceRequestRepositoryConflict("provider proposal operation is unsupported")


def _bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("limit must be a positive integer")
    return min(value, 5)


def _parse_datetime(value: datetime | str, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a datetime or ISO-8601 string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid ISO-8601 datetime") from error


def _require_present(value: ServiceRequest | None) -> ServiceRequest:
    if value is None:
        raise ServiceRequestRepositoryConflict("service request disappeared")
    return value


__all__ = [
    "ExecutionKind",
    "FinalizedProviderCreateReceipt",
    "InMemoryServiceRequestRepository",
    "PreparedProviderCreate",
    "PreparedProviderOperation",
    "ProviderBinding",
    "ProviderMutationAdapter",
    "ServiceRequestCommandResponse",
    "ServiceRequestCommandService",
    "ServiceRequestCommandStatus",
    "ServiceRequestExecutionTarget",
    "ServiceRequestProviderBindingRequired",
    "ServiceRequestRepository",
    "ServiceRequestRepositoryConflict",
    "StoredServiceRequest",
    "customer_key_for_phone",
]
