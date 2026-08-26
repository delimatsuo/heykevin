"""Focused contract tests for the in-memory service-request application layer."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.services.service_request import ServiceRequest
from app.services.service_request_repository import (
    ExecutionKind,
    InMemoryServiceRequestRepository,
    ProviderBinding,
    ServiceRequestCommandService,
    ServiceRequestCommandStatus,
    ServiceRequestRepositoryConflict,
    customer_key_for_phone,
)
from app.utils.phone import phone_hash

NOW = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
START = datetime(2026, 8, 13, 13, 0, tzinfo=UTC)
END = datetime(2026, 8, 13, 14, 0, tzinfo=UTC)


@pytest.fixture
def repository():
    return InMemoryServiceRequestRepository()


@pytest.fixture
def service(repository):
    return ServiceRequestCommandService(repository, clock=lambda: NOW)


async def _create(service, **overrides):
    values = {
        "contractor_id": "contractor-1",
        "caller_phone": "+16175550123",
        "request_id": "request-1",
        "services": ["Toilet repair"],
        "scheduled_start": START,
        "scheduled_end": END,
        "expected_revision": 0,
        "idempotency_key": "create-1",
    }
    values.update(overrides)
    return await service.create_service_request(**values)


class _ProviderAdapter:
    def __init__(self, outcomes=True):
        self._outcomes = iter(outcomes if isinstance(outcomes, list) else [outcomes])
        self.calls = []

    async def _confirm(self, operation, **values):
        self.calls.append((operation, values))
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def cancel(self, **values):
        return await self._confirm("cancel", **values)

    async def create(self, **values):
        return await self._confirm("create", **values)

    async def reschedule(self, **values):
        return await self._confirm("reschedule", **values)

    async def add_service(self, **values):
        return await self._confirm("add_service", **values)


class _LoseFirstCreateFinalizeReplyRepository:
    """Simulate commit success followed by process/network loss before its reply."""

    def __init__(self, repository):
        self._repository = repository
        self._lost_reply = False

    def __getattr__(self, name):
        return getattr(self._repository, name)

    async def finalize_provider_create(self, preparation):
        result = await self._repository.finalize_provider_create(preparation)
        if not self._lost_reply:
            self._lost_reply = True
            raise RuntimeError("reply lost after committed finalize")
        return result


async def _create_provider_backed(repository):
    prepared = await repository.prepare_provider_create(
        contractor_id="contractor-1",
        customer_key=customer_key_for_phone("+16175550123"),
        request_id="request-1",
        idempotency_key="create-1",
        binding=ProviderBinding(kind="google_calendar", resource_id="event-1"),
        title="Toilet repair",
        description="Caller requested service.",
        mutation=lambda current: ServiceRequest.create(
            request_id="request-1",
            services=["Toilet repair"],
            scheduled_start=START,
            scheduled_end=END,
            expected_revision=0,
            idempotency_key="create-1",
            occurred_at=NOW,
            existing=current,
        ),
    )
    return await repository.finalize_provider_create(prepared)


async def _provider_create(service, **overrides):
    values = {
        "contractor_id": "contractor-1",
        "caller_phone": "+16175550123",
        "request_id": "request-provider-create-1",
        "services": ["Drain repair"],
        "scheduled_start": START,
        "scheduled_end": END,
        "expected_revision": 0,
        "idempotency_key": "transport-create-1",
        "binding": ProviderBinding(kind="google_calendar", resource_id="event-create-1"),
        "title": "Drain repair",
        "description": "Kitchen sink is leaking.",
    }
    values.update(overrides)
    return await service.create_provider_service_request(**values)


def test_customer_key_normalizes_before_hashing_and_rejects_invalid_phone():
    assert customer_key_for_phone("(617) 555-0123") == phone_hash("+16175550123")

    with pytest.raises(ValueError):
        customer_key_for_phone("anonymous")


@pytest.mark.asyncio
async def test_local_create_records_immutable_local_execution_provenance(repository, service):
    await _create(service)
    customer_key = customer_key_for_phone("+16175550123")

    target = await repository.get_execution_target(
        contractor_id="contractor-1",
        customer_key=customer_key,
        request_id="request-1",
    )

    assert target is not None
    assert target.execution_kind is ExecutionKind.LOCAL
    assert target.provider_binding is None
    with pytest.raises(ServiceRequestRepositoryConflict):
        await repository.bind_provider(
            contractor_id="contractor-1",
            customer_key=customer_key,
            request_id="request-1",
            binding=ProviderBinding(kind="google_calendar", resource_id="event-1"),
        )


@pytest.mark.asyncio
async def test_corrupt_or_incomplete_execution_provenance_fails_closed(repository, service):
    await _create_provider_backed(repository)
    storage_key = ("contractor-1", "request-1")
    repository._finalized_provider_creates.pop(storage_key)
    adapter = _ProviderAdapter(True)
    command_service = ServiceRequestCommandService(
        repository,
        provider_adapter=adapter,
        clock=lambda: NOW,
    )

    assert (
        await command_service.list_actionable(
            contractor_id="contractor-1",
            caller_phone="+16175550123",
        )
        == ()
    )
    failed = await command_service.cancel_service_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="cancel-corrupt-provider",
    )
    assert failed.status is ServiceRequestCommandStatus.FAILED
    assert adapter.calls == []

    stored = repository._records[storage_key]
    repository._records[storage_key] = replace(stored, execution_kind="legacy")
    repository._provider_bindings.pop(storage_key)
    assert (
        await command_service.list_actionable(
            contractor_id="contractor-1",
            caller_phone="+16175550123",
        )
        == ()
    )


@pytest.mark.asyncio
async def test_create_and_identical_retry_return_applied_without_revision_growth(
    service,
    repository,
):
    first = await _create(service)
    retry = await _create(service, caller_phone="617-555-0123")

    assert first.status is ServiceRequestCommandStatus.APPLIED
    assert first.revision == 1
    assert retry == first
    stored = await repository.get(
        contractor_id="contractor-1",
        customer_key=customer_key_for_phone("+16175550123"),
        request_id="request-1",
    )
    assert stored is not None
    assert stored.revision == 1


@pytest.mark.asyncio
async def test_conflicting_idempotency_reuse_and_stale_revision_are_conflicts(service):
    await _create(service)

    reused = await service.add_service_to_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="create-1",
        service="Drain clearing",
    )
    stale = await service.add_service_to_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=99,
        idempotency_key="add-stale",
        service="Drain clearing",
    )

    assert reused.status is ServiceRequestCommandStatus.CONFLICT
    assert stale.status is ServiceRequestCommandStatus.CONFLICT


@pytest.mark.asyncio
async def test_retry_after_later_command_returns_the_original_command_response(service):
    await _create(service)
    original = await service.add_service_to_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="add-1",
        service="Drain clearing",
    )
    await service.reschedule_service_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=2,
        idempotency_key="reschedule-1",
        scheduled_start=START + timedelta(days=1),
        scheduled_end=END + timedelta(days=1),
    )

    retry = await service.add_service_to_request(
        contractor_id="contractor-1",
        caller_phone="617-555-0123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="add-1",
        service="Drain clearing",
    )

    assert retry == original
    assert retry.revision == 2


@pytest.mark.asyncio
async def test_executor_method_names_apply_cancel_reschedule_and_add_service(service):
    await _create(service)
    added = await service.add_service_to_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="add-1",
        service="Drain clearing",
    )
    rescheduled = await service.reschedule_service_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=2,
        idempotency_key="reschedule-1",
        scheduled_start=(START + timedelta(days=1)).isoformat(),
        scheduled_end=(END + timedelta(days=1)).isoformat(),
    )
    cancelled = await service.cancel_service_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=3,
        idempotency_key="cancel-1",
    )

    assert (added.status, added.revision) == (ServiceRequestCommandStatus.APPLIED, 2)
    assert (rescheduled.status, rescheduled.revision) == (
        ServiceRequestCommandStatus.APPLIED,
        3,
    )
    assert (cancelled.status, cancelled.revision) == (
        ServiceRequestCommandStatus.APPLIED,
        4,
    )


@pytest.mark.asyncio
async def test_missing_or_cross_customer_request_is_not_found(service):
    await _create(service)

    missing = await service.cancel_service_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="missing",
        expected_revision=1,
        idempotency_key="cancel-missing",
    )
    wrong_customer = await service.cancel_service_request(
        contractor_id="contractor-1",
        caller_phone="+16175550124",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="cancel-wrong-customer",
    )
    wrong_tenant = await service.cancel_service_request(
        contractor_id="contractor-2",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="cancel-wrong-tenant",
    )

    assert missing.status is ServiceRequestCommandStatus.NOT_FOUND
    assert wrong_customer.status is ServiceRequestCommandStatus.NOT_FOUND
    assert wrong_tenant.status is ServiceRequestCommandStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_invalid_identity_or_domain_input_returns_failed(service):
    invalid_phone = await _create(service, caller_phone="anonymous")
    invalid_schedule = await _create(service, scheduled_start="not-a-date")

    assert invalid_phone.status is ServiceRequestCommandStatus.FAILED
    assert invalid_schedule.status is ServiceRequestCommandStatus.FAILED


@pytest.mark.asyncio
async def test_list_actionable_is_customer_scoped_open_only_newest_first_and_bounded(
    service,
):
    for index in range(7):
        result = await _create(
            service,
            request_id=f"request-{index}",
            idempotency_key=f"create-{index}",
        )
        assert result.status is ServiceRequestCommandStatus.APPLIED

    await service.cancel_service_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-6",
        expected_revision=1,
        idempotency_key="cancel-6",
    )
    await _create(
        service,
        contractor_id="contractor-2",
        request_id="other-tenant",
        idempotency_key="other-tenant-create",
    )
    await _create(
        service,
        caller_phone="+16175550124",
        request_id="other-customer",
        idempotency_key="other-customer-create",
    )

    actionable = await service.list_actionable(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        limit=99,
    )

    assert len(actionable) == 5
    assert all(request.status.value == "open" for request in actionable)
    assert {request.request_id for request in actionable}.isdisjoint(
        {"request-6", "other-tenant", "other-customer"}
    )


@pytest.mark.asyncio
async def test_repository_apply_is_atomic_for_competing_expected_revisions(service):
    await _create(service)

    first, second = await asyncio.gather(
        service.add_service_to_request(
            contractor_id="contractor-1",
            caller_phone="+16175550123",
            request_id="request-1",
            expected_revision=1,
            idempotency_key="add-a",
            service="Drain clearing",
        ),
        service.add_service_to_request(
            contractor_id="contractor-1",
            caller_phone="+16175550123",
            request_id="request-1",
            expected_revision=1,
            idempotency_key="add-b",
            service="Faucet repair",
        ),
    )

    assert sorted((first.status.value, second.status.value)) == ["applied", "conflict"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_outcome", [False, RuntimeError("provider unavailable")])
async def test_provider_failure_is_pending_and_does_not_change_canonical_request(
    repository,
    service,
    provider_outcome,
):
    await _create_provider_backed(repository)
    adapter = _ProviderAdapter(provider_outcome)
    provider_service = ServiceRequestCommandService(
        repository,
        provider_adapter=adapter,
        clock=lambda: NOW,
    )

    response = await provider_service.add_service_to_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="provider-add-1",
        service="Drain clearing",
    )

    assert response.status is ServiceRequestCommandStatus.PENDING_PROVIDER
    assert [call[0] for call in adapter.calls] == ["add_service"]
    stored = await repository.get(
        contractor_id="contractor-1",
        customer_key=customer_key_for_phone("+16175550123"),
        request_id="request-1",
    )
    assert stored is not None
    assert stored.revision == 1
    assert stored.services == ("Toilet repair",)


@pytest.mark.asyncio
async def test_pending_provider_retry_finalizes_once_and_applied_retry_skips_provider(
    repository,
    service,
):
    await _create_provider_backed(repository)
    pending_adapter = _ProviderAdapter(False)
    pending_service = ServiceRequestCommandService(
        repository,
        provider_adapter=pending_adapter,
        clock=lambda: NOW,
    )
    command = {
        "contractor_id": "contractor-1",
        "caller_phone": "+16175550123",
        "request_id": "request-1",
        "expected_revision": 1,
        "idempotency_key": "provider-add-1",
        "service": "Drain clearing",
    }

    pending = await pending_service.add_service_to_request(**command)
    confirmed_adapter = _ProviderAdapter(True)
    confirmed_service = ServiceRequestCommandService(
        repository,
        provider_adapter=confirmed_adapter,
        clock=lambda: NOW + timedelta(minutes=5),
    )
    applied = await confirmed_service.add_service_to_request(**command)
    replay_adapter = _ProviderAdapter(RuntimeError("must not be called"))
    replay_service = ServiceRequestCommandService(
        repository,
        provider_adapter=replay_adapter,
        clock=lambda: NOW + timedelta(minutes=10),
    )
    replay = await replay_service.add_service_to_request(**command)

    assert pending.status is ServiceRequestCommandStatus.PENDING_PROVIDER
    assert (applied.status, applied.revision) == (ServiceRequestCommandStatus.APPLIED, 2)
    assert replay == applied
    assert len(confirmed_adapter.calls) == 1
    assert replay_adapter.calls == []
    stored = await repository.get(
        contractor_id="contractor-1",
        customer_key=customer_key_for_phone("+16175550123"),
        request_id="request-1",
    )
    assert stored is not None
    assert stored.revision == 2
    assert stored.services == ("Toilet repair", "Drain clearing")


@pytest.mark.asyncio
async def test_pending_semantics_attach_across_transport_keys_with_one_logical_id(
    repository,
    service,
):
    await _create_provider_backed(repository)
    pending_adapter = _ProviderAdapter(False)
    pending_service = ServiceRequestCommandService(
        repository,
        provider_adapter=pending_adapter,
        clock=lambda: NOW,
    )
    first = await pending_service.add_service_to_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="transport-attempt-1",
        service="Drain clearing",
    )
    confirmed_adapter = _ProviderAdapter(True)
    retry_service = ServiceRequestCommandService(
        repository,
        provider_adapter=confirmed_adapter,
        clock=lambda: NOW + timedelta(minutes=5),
    )
    applied = await retry_service.add_service_to_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="transport-attempt-2",
        service="Drain clearing",
    )

    assert first.status is ServiceRequestCommandStatus.PENDING_PROVIDER
    assert applied.status is ServiceRequestCommandStatus.APPLIED
    assert (
        pending_adapter.calls[0][1]["idempotency_key"]
        == confirmed_adapter.calls[0][1]["idempotency_key"]
    )
    assert pending_adapter.calls[0][1]["idempotency_key"] != "transport-attempt-1"


@pytest.mark.asyncio
async def test_recovery_replays_durable_semantic_proposal_and_finalizes(repository, service):
    await _create_provider_backed(repository)
    pending_service = ServiceRequestCommandService(
        repository,
        provider_adapter=_ProviderAdapter(False),
        clock=lambda: NOW,
    )
    pending = await pending_service.reschedule_service_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="transport-before-crash",
        scheduled_start=START + timedelta(days=1),
        scheduled_end=END + timedelta(days=1),
    )
    recovery_adapter = _ProviderAdapter(True)
    restarted_service = ServiceRequestCommandService(
        repository,
        provider_adapter=recovery_adapter,
        clock=lambda: NOW + timedelta(hours=1),
    )

    recovered = await restarted_service.recover_provider_operation(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
    )

    assert pending.status is ServiceRequestCommandStatus.PENDING_PROVIDER
    assert (recovered.status, recovered.revision) == (
        ServiceRequestCommandStatus.APPLIED,
        2,
    )
    operation, values = recovery_adapter.calls[0]
    assert operation == "reschedule"
    assert values["request"].scheduled_start == START
    assert values["request"].scheduled_end == END
    assert values["scheduled_start"] == START + timedelta(days=1)
    assert values["scheduled_end"] == END + timedelta(days=1)
    assert values["idempotency_key"] != "transport-before-crash"


@pytest.mark.asyncio
async def test_provider_preconditions_are_checked_before_external_call(repository, service):
    await _create_provider_backed(repository)
    adapter = _ProviderAdapter(True)
    provider_service = ServiceRequestCommandService(
        repository,
        provider_adapter=adapter,
        clock=lambda: NOW,
    )

    stale = await provider_service.cancel_service_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=99,
        idempotency_key="provider-cancel-stale",
    )

    assert stale.status is ServiceRequestCommandStatus.CONFLICT
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_different_command_cannot_overtake_pending_provider_operation(
    repository,
    service,
):
    await _create_provider_backed(repository)
    adapter = _ProviderAdapter([False, True])
    provider_service = ServiceRequestCommandService(
        repository,
        provider_adapter=adapter,
        clock=lambda: NOW,
    )

    pending = await provider_service.add_service_to_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="provider-add-1",
        service="Drain clearing",
    )
    competing = await provider_service.reschedule_service_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-1",
        expected_revision=1,
        idempotency_key="provider-reschedule-1",
        scheduled_start=START + timedelta(days=1),
        scheduled_end=END + timedelta(days=1),
    )

    assert pending.status is ServiceRequestCommandStatus.PENDING_PROVIDER
    assert competing.status is ServiceRequestCommandStatus.CONFLICT
    assert [call[0] for call in adapter.calls] == ["add_service"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "reschedule", "add_service"])
async def test_provider_confirmed_mutations_finalize_all_supported_commands(
    repository,
    service,
    operation,
):
    await _create_provider_backed(repository)
    adapter = _ProviderAdapter(True)
    provider_service = ServiceRequestCommandService(
        repository,
        provider_adapter=adapter,
        clock=lambda: NOW,
    )
    common = {
        "contractor_id": "contractor-1",
        "caller_phone": "+16175550123",
        "request_id": "request-1",
        "expected_revision": 1,
        "idempotency_key": f"provider-{operation}-1",
    }

    if operation == "cancel":
        response = await provider_service.cancel_service_request(**common)
    elif operation == "reschedule":
        response = await provider_service.reschedule_service_request(
            **common,
            scheduled_start=START + timedelta(days=1),
            scheduled_end=END + timedelta(days=1),
        )
    else:
        response = await provider_service.add_service_to_request(
            **common,
            service="Drain clearing",
        )

    assert (response.status, response.revision) == (
        ServiceRequestCommandStatus.APPLIED,
        2,
    )
    assert [call[0] for call in adapter.calls] == [operation]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_outcome", [False, RuntimeError("provider unavailable")])
async def test_provider_create_failure_remains_pending_without_exposing_request_or_binding(
    repository,
    provider_outcome,
):
    adapter = _ProviderAdapter(provider_outcome)
    provider_service = ServiceRequestCommandService(
        repository,
        provider_adapter=adapter,
        clock=lambda: NOW,
    )

    response = await _provider_create(
        provider_service,
        title="  Drain   repair ",
        description="Kitchen sink is leaking.  \r\nBring a wrench.  ",
    )

    customer_key = customer_key_for_phone("+16175550123")
    assert response.status is ServiceRequestCommandStatus.PENDING_PROVIDER
    assert (
        await repository.get(
            contractor_id="contractor-1",
            customer_key=customer_key,
            request_id="request-provider-create-1",
        )
        is None
    )
    assert (
        await repository.get_provider_binding(
            contractor_id="contractor-1",
            customer_key=customer_key,
            request_id="request-provider-create-1",
        )
        is None
    )
    assert (
        await provider_service.list_actionable(
            contractor_id="contractor-1",
            caller_phone="+16175550123",
        )
        == ()
    )
    operation, values = adapter.calls[0]
    assert operation == "create"
    assert values["title"] == "Drain repair"
    assert values["description"] == "Kitchen sink is leaking.\nBring a wrench."
    assert values["idempotency_key"] != "transport-create-1"


@pytest.mark.asyncio
async def test_provider_create_confirmation_atomically_exposes_aggregate_and_binding(repository):
    adapter = _ProviderAdapter(True)
    provider_service = ServiceRequestCommandService(
        repository,
        provider_adapter=adapter,
        clock=lambda: NOW,
    )

    response = await _provider_create(provider_service)

    customer_key = customer_key_for_phone("+16175550123")
    stored = await repository.get(
        contractor_id="contractor-1",
        customer_key=customer_key,
        request_id="request-provider-create-1",
    )
    binding = await repository.get_provider_binding(
        contractor_id="contractor-1",
        customer_key=customer_key,
        request_id="request-provider-create-1",
    )
    actionable = await provider_service.list_actionable(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
    )

    assert (response.status, response.revision) == (
        ServiceRequestCommandStatus.APPLIED,
        1,
    )
    assert stored is not None
    assert stored.services == ("Drain repair",)
    assert binding == ProviderBinding(kind="google_calendar", resource_id="event-create-1")
    assert [request.request_id for request in actionable] == ["request-provider-create-1"]


@pytest.mark.asyncio
async def test_provider_create_retry_after_finalize_reply_loss_is_applied_without_provider(
    repository,
):
    lossy_repository = _LoseFirstCreateFinalizeReplyRepository(repository)
    first_adapter = _ProviderAdapter(True)
    first_service = ServiceRequestCommandService(
        lossy_repository,
        provider_adapter=first_adapter,
        clock=lambda: NOW,
    )

    lost_reply = await _provider_create(first_service)
    retry_adapter = _ProviderAdapter(RuntimeError("provider must not be called"))
    restarted_service = ServiceRequestCommandService(
        repository,
        provider_adapter=retry_adapter,
        clock=lambda: NOW + timedelta(minutes=5),
    )
    retried = await _provider_create(
        restarted_service,
        idempotency_key="transport-after-finalize-reply-loss",
    )

    assert lost_reply.status is ServiceRequestCommandStatus.FAILED
    assert (retried.status, retried.revision) == (
        ServiceRequestCommandStatus.APPLIED,
        1,
    )
    assert [call[0] for call in first_adapter.calls] == ["create"]
    assert retry_adapter.calls == []


@pytest.mark.asyncio
async def test_exact_provider_create_finalize_retry_returns_original_result(repository):
    customer_key = customer_key_for_phone("+16175550123")
    preparation = await repository.prepare_provider_create(
        contractor_id="contractor-1",
        customer_key=customer_key,
        request_id="request-provider-create-1",
        idempotency_key="transport-create-1",
        binding=ProviderBinding(kind="google_calendar", resource_id="event-create-1"),
        title="Drain repair",
        description="Kitchen sink is leaking.",
        mutation=lambda current: ServiceRequest.create(
            request_id="request-provider-create-1",
            services=["Drain repair"],
            scheduled_start=START,
            scheduled_end=END,
            expected_revision=0,
            idempotency_key="transport-create-1",
            occurred_at=NOW,
            existing=current,
        ),
    )

    first = await repository.finalize_provider_create(preparation)
    mutation_service = ServiceRequestCommandService(
        repository,
        provider_adapter=_ProviderAdapter(True),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    mutated = await mutation_service.add_service_to_request(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-provider-create-1",
        expected_revision=1,
        idempotency_key="add-after-create",
        service="Pipe inspection",
    )
    retried = await repository.finalize_provider_create(preparation)

    assert mutated.status is ServiceRequestCommandStatus.APPLIED
    assert retried == first


@pytest.mark.asyncio
async def test_changed_provider_create_semantics_conflict_after_finalize_without_provider(
    repository,
):
    first_adapter = _ProviderAdapter(True)
    first_service = ServiceRequestCommandService(
        repository,
        provider_adapter=first_adapter,
        clock=lambda: NOW,
    )
    applied = await _provider_create(first_service)
    retry_adapter = _ProviderAdapter(RuntimeError("provider must not be called"))
    restarted_service = ServiceRequestCommandService(
        repository,
        provider_adapter=retry_adapter,
        clock=lambda: NOW + timedelta(minutes=5),
    )

    changed = await _provider_create(
        restarted_service,
        idempotency_key="new-transport",
        description="A materially different problem.",
    )

    assert applied.status is ServiceRequestCommandStatus.APPLIED
    assert changed.status is ServiceRequestCommandStatus.CONFLICT
    assert retry_adapter.calls == []


@pytest.mark.asyncio
async def test_pending_provider_create_attaches_new_transport_to_same_logical_operation(
    repository,
):
    first_adapter = _ProviderAdapter(False)
    first_service = ServiceRequestCommandService(
        repository,
        provider_adapter=first_adapter,
        clock=lambda: NOW,
    )
    first = await _provider_create(first_service)
    retry_adapter = _ProviderAdapter(True)
    restarted_service = ServiceRequestCommandService(
        repository,
        provider_adapter=retry_adapter,
        clock=lambda: NOW + timedelta(minutes=5),
    )

    retried = await _provider_create(
        restarted_service,
        idempotency_key="transport-create-after-restart",
    )

    assert first.status is ServiceRequestCommandStatus.PENDING_PROVIDER
    assert (retried.status, retried.revision) == (
        ServiceRequestCommandStatus.APPLIED,
        1,
    )
    first_key = first_adapter.calls[0][1]["idempotency_key"]
    retry_key = retry_adapter.calls[0][1]["idempotency_key"]
    assert first_key == retry_key
    assert retry_key not in {"transport-create-1", "transport-create-after-restart"}


@pytest.mark.asyncio
async def test_recover_provider_create_replays_proposal_after_restart(repository):
    pending_service = ServiceRequestCommandService(
        repository,
        provider_adapter=_ProviderAdapter(False),
        clock=lambda: NOW,
    )
    pending = await _provider_create(pending_service)
    recovery_adapter = _ProviderAdapter(True)
    restarted_service = ServiceRequestCommandService(
        repository,
        provider_adapter=recovery_adapter,
        clock=lambda: NOW + timedelta(hours=1),
    )

    recovered = await restarted_service.recover_provider_create(
        contractor_id="contractor-1",
        caller_phone="+16175550123",
        request_id="request-provider-create-1",
    )

    assert pending.status is ServiceRequestCommandStatus.PENDING_PROVIDER
    assert (recovered.status, recovered.revision) == (
        ServiceRequestCommandStatus.APPLIED,
        1,
    )
    assert recovery_adapter.calls[0][0] == "create"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("services", ["Water heater repair"]),
        ("title", "Water heater repair"),
        ("description", "A different problem."),
        (
            "binding",
            ProviderBinding(kind="google_calendar", resource_id="event-create-2"),
        ),
    ],
)
async def test_changed_provider_create_semantics_conflict_without_second_provider_call(
    repository,
    changed_field,
    changed_value,
):
    adapter = _ProviderAdapter([False, True])
    provider_service = ServiceRequestCommandService(
        repository,
        provider_adapter=adapter,
        clock=lambda: NOW,
    )
    pending = await _provider_create(provider_service)

    changed = await _provider_create(
        provider_service,
        idempotency_key="new-transport",
        **{changed_field: changed_value},
    )

    assert pending.status is ServiceRequestCommandStatus.PENDING_PROVIDER
    assert changed.status is ServiceRequestCommandStatus.CONFLICT
    assert [call[0] for call in adapter.calls] == ["create"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_field", "invalid_value", "expected_status"),
    [
        ("caller_phone", "anonymous", ServiceRequestCommandStatus.FAILED),
        ("scheduled_start", "not-a-date", ServiceRequestCommandStatus.FAILED),
        ("expected_revision", 1, ServiceRequestCommandStatus.CONFLICT),
        ("title", "   ", ServiceRequestCommandStatus.FAILED),
    ],
)
async def test_invalid_provider_create_never_calls_provider(
    repository,
    invalid_field,
    invalid_value,
    expected_status,
):
    adapter = _ProviderAdapter(True)
    provider_service = ServiceRequestCommandService(
        repository,
        provider_adapter=adapter,
        clock=lambda: NOW,
    )

    response = await _provider_create(
        provider_service,
        **{invalid_field: invalid_value},
    )

    assert response.status is expected_status
    assert adapter.calls == []
