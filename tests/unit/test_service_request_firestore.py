import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.db.service_requests import FirestoreServiceRequestRepository
from app.services.service_request import ServiceRequest
from app.services.service_request_repository import (
    ExecutionKind,
    ProviderBinding,
    ServiceRequestProviderBindingRequired,
    ServiceRequestRepositoryConflict,
    customer_key_for_phone,
)

NOW = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)


class _Snapshot:
    def __init__(self, path, data=None):
        self.id = path.rsplit("/", 1)[-1]
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _Doc:
    def __init__(self, path, store):
        self.path, self.store = path, store

    def collection(self, name):
        return _Collection(f"{self.path}/{name}", self.store)

    def get(self, transaction=None):
        return _Snapshot(self.path, self.store.get(self.path))


class _Query:
    def __init__(self, path, store):
        self.path, self.store, self.filters, self.cap = path, store, [], 5
        self.order_field = None
        self.order_direction = None

    def where(self, filter):
        self.filters.append((filter.field_path, filter.value))
        return self

    def limit(self, value):
        self.cap = value
        return self

    def order_by(self, field, direction):
        self.order_field = field
        self.order_direction = direction
        return self

    def stream(self):
        prefix = f"{self.path}/"
        rows = []
        for path, data in self.store.items():
            if path.startswith(prefix) and all(data.get(k) == v for k, v in self.filters):
                rows.append(_Snapshot(path, data))
        if self.order_field:
            rows.sort(
                key=lambda snapshot: snapshot.to_dict()[self.order_field],
                reverse=self.order_direction == "DESCENDING",
            )
        return rows[: self.cap]


class _Collection(_Query):
    def document(self, doc_id):
        return _Doc(f"{self.path}/{doc_id}", self.store)


class _Transaction:
    def __init__(self, store):
        self.store = store

    def set(self, ref, data, merge=False):
        if merge and ref.path in self.store:
            self.store[ref.path] = {**self.store[ref.path], **data}
        else:
            self.store[ref.path] = data


class _Client:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return _Collection(name, self.store)

    def transaction(self):
        return _Transaction(self.store)


async def _create_provider_backed(
    repo,
    *,
    customer_key,
    binding=None,
):
    binding = binding or ProviderBinding(
        kind="google_calendar",
        resource_id="provider-event-1",
    )
    prepared = await repo.prepare_provider_create(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
        idempotency_key="create-1",
        binding=binding,
        title="Furnace tune-up",
        description="Caller requested annual maintenance.",
        mutation=lambda current: ServiceRequest.create(
            request_id="request-1",
            services=["Furnace tune-up"],
            scheduled_start=NOW + timedelta(days=1),
            scheduled_end=NOW + timedelta(days=1, hours=1),
            expected_revision=0,
            idempotency_key="create-1",
            occurred_at=NOW,
            existing=current,
        ),
    )
    return await repo.finalize_provider_create(prepared)


@pytest.mark.asyncio
async def test_firestore_request_is_tenant_customer_scoped_and_actionable(monkeypatch):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    customer_key = customer_key_for_phone("+16175550123")
    result = await repo.apply(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
        mutation=lambda current: ServiceRequest.create(
            request_id="request-1",
            services=["Furnace tune-up"],
            scheduled_start=NOW + timedelta(days=1),
            scheduled_end=NOW + timedelta(days=1, hours=1),
            expected_revision=0,
            idempotency_key="create-1",
            occurred_at=NOW,
            existing=current,
        ),
    )

    assert result.request.revision == 1
    assert next(iter(client.store)).startswith("contractors/c1/service_requests/")
    document = next(iter(client.store.values()))
    assert document["execution_kind"] == "local"
    target = await repo.get_execution_target(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
    )
    assert target is not None
    assert target.execution_kind is ExecutionKind.LOCAL
    with pytest.raises(ServiceRequestRepositoryConflict):
        await repo.bind_provider(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-1",
            binding=ProviderBinding(kind="google_calendar", resource_id="provider-event-1"),
        )
    assert (
        await repo.get(contractor_id="c1", customer_key=customer_key, request_id="request-1")
        == result.request
    )
    assert (
        await repo.get(contractor_id="c2", customer_key=customer_key, request_id="request-1")
        is None
    )
    assert await repo.list_actionable(contractor_id="c1", customer_key=customer_key, limit=5) == (
        result.request,
    )


@pytest.mark.asyncio
async def test_firestore_actionable_limit_selects_newest_before_truncating(monkeypatch):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    customer_key = customer_key_for_phone("+16175550123")

    for index in range(7):
        request_id = f"request-{index}"
        occurred_at = NOW + timedelta(minutes=index)
        await repo.apply(
            contractor_id="c1",
            customer_key=customer_key,
            request_id=request_id,
            mutation=lambda current, request_id=request_id, occurred_at=occurred_at: (
                ServiceRequest.create(
                    request_id=request_id,
                    services=["Furnace tune-up"],
                    scheduled_start=NOW + timedelta(days=1),
                    scheduled_end=NOW + timedelta(days=1, hours=1),
                    expected_revision=0,
                    idempotency_key=f"create:{request_id}",
                    occurred_at=occurred_at,
                    existing=current,
                )
            ),
        )

    actionable = await repo.list_actionable(
        contractor_id="c1",
        customer_key=customer_key,
        limit=5,
    )

    assert [request.request_id for request in actionable] == [
        "request-6",
        "request-5",
        "request-4",
        "request-3",
        "request-2",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["missing_kind", "unknown_kind", "provider_without_proof", "local_with_binding"],
)
async def test_firestore_corrupt_or_legacy_provenance_is_not_actionable(monkeypatch, corruption):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    customer_key = customer_key_for_phone("+16175550123")
    await repo.apply(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
        mutation=lambda current: ServiceRequest.create(
            request_id="request-1",
            services=["Furnace tune-up"],
            scheduled_start=NOW + timedelta(days=1),
            scheduled_end=NOW + timedelta(days=1, hours=1),
            expected_revision=0,
            idempotency_key="create-1",
            occurred_at=NOW,
            existing=current,
        ),
    )
    document = next(iter(client.store.values()))
    if corruption == "missing_kind":
        document.pop("execution_kind")
    elif corruption == "unknown_kind":
        document["execution_kind"] = "legacy"
    elif corruption == "provider_without_proof":
        document["execution_kind"] = "provider"
    else:
        document["provider_binding"] = {
            "kind": "google_calendar",
            "resource_id": "provider-event-1",
        }

    assert (
        await repo.list_actionable(
            contractor_id="c1",
            customer_key=customer_key,
        )
        == ()
    )
    with pytest.raises(ServiceRequestRepositoryConflict):
        await repo.get_execution_target(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-1",
        )


@pytest.mark.asyncio
async def test_firestore_provider_operation_only_canonicalizes_after_finalize(monkeypatch):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    customer_key = customer_key_for_phone("+16175550123")
    binding = ProviderBinding(kind="google_calendar", resource_id="provider-event-1")
    created = await _create_provider_backed(repo, customer_key=customer_key, binding=binding)

    assert (
        await repo.get_provider_binding(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-1",
        )
        == binding
    )
    with pytest.raises(ServiceRequestProviderBindingRequired):
        await repo.apply(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-1",
            mutation=lambda current: current.cancel(
                expected_revision=created.request.revision,
                idempotency_key="bypass-cancel",
                occurred_at=NOW + timedelta(minutes=1),
            ),
        )

    prepared = await repo.prepare_provider_operation(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
        idempotency_key="provider-cancel-1",
        mutation=lambda current: current.cancel(
            expected_revision=created.request.revision,
            idempotency_key="provider-cancel-1",
            occurred_at=NOW + timedelta(minutes=2),
        ),
    )

    assert prepared.base_request == created.request
    assert prepared.result.request.status.value == "cancelled"
    assert (
        await repo.get(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-1",
        )
        == created.request
    )
    document = next(iter(client.store.values()))
    assert document["status"] == "open"
    assert ServiceRequest.from_dict(document["aggregate"]) == created.request
    assert set(document["pending_provider_operation"]) == {
        "schema_version",
        "logical_operation_id",
        "semantic_fingerprint",
        "base_revision",
        "origin_idempotency_key",
        "proposal",
    }
    assert document["provider_recovery_state"] == "pending"
    assert document["provider_recovery_attempts"] == 0
    assert document["next_attempt_at"] == NOW + timedelta(minutes=2)
    assert "expires_at" not in document
    proposal = document["pending_provider_operation"]["proposal"]
    assert proposal["operation"] == "cancel"
    assert proposal["arguments"] == {}
    assert ServiceRequest.from_dict(proposal["result"]["request"]) == prepared.result.request
    concurrent_retry = await repo.prepare_provider_operation(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
        idempotency_key="provider-cancel-1",
        mutation=lambda current: current.cancel(
            expected_revision=created.request.revision,
            idempotency_key="provider-cancel-1",
            occurred_at=NOW + timedelta(minutes=3),
        ),
    )

    finalized = await repo.finalize_provider_operation(prepared)

    assert finalized == prepared.result
    assert (
        await repo.get(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-1",
        )
        == prepared.result.request
    )
    document = next(iter(client.store.values()))
    assert document["status"] == "cancelled"
    assert document["execution_kind"] == "provider"
    assert "pending_provider_operation" not in document
    assert "provider_recovery_state" not in document
    assert "provider_recovery_attempts" not in document
    assert "next_attempt_at" not in document
    assert document["expires_at"] > document["updated_at"]
    assert document["provider_binding"] == {
        "kind": "google_calendar",
        "resource_id": "provider-event-1",
    }
    assert await repo.finalize_provider_operation(prepared) == prepared.result
    assert await repo.finalize_provider_operation(concurrent_retry) == prepared.result


@pytest.mark.asyncio
async def test_firestore_finalize_rejects_forged_prepared_result(monkeypatch):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    customer_key = customer_key_for_phone("+16175550123")
    created = await _create_provider_backed(repo, customer_key=customer_key)
    prepared = await repo.prepare_provider_operation(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
        idempotency_key="provider-change-1",
        mutation=lambda current: current.cancel(
            expected_revision=created.request.revision,
            idempotency_key="provider-change-1",
            occurred_at=NOW + timedelta(minutes=1),
        ),
    )
    forged_result = created.request.add_service(
        service="Drain clearing",
        expected_revision=created.request.revision,
        idempotency_key="provider-change-1",
        occurred_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(ServiceRequestRepositoryConflict):
        await repo.finalize_provider_operation(replace(prepared, result=forged_result))

    assert (
        await repo.get(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-1",
        )
        == created.request
    )


@pytest.mark.asyncio
async def test_firestore_provider_prepare_is_replay_safe_and_exclusive(monkeypatch):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    customer_key = customer_key_for_phone("+16175550123")
    other_customer_key = customer_key_for_phone("+16175550124")
    binding = ProviderBinding(kind="google_calendar", resource_id="provider-event-1")
    created = await _create_provider_backed(repo, customer_key=customer_key, binding=binding)

    def cancel(current, *, key="provider-cancel-1", minute=1):
        return current.cancel(
            expected_revision=created.request.revision,
            idempotency_key=key,
            occurred_at=NOW + timedelta(minutes=minute),
        )

    first = await repo.prepare_provider_operation(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
        idempotency_key="provider-cancel-1",
        mutation=cancel,
    )
    retry = await repo.prepare_provider_operation(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
        idempotency_key="provider-cancel-1",
        mutation=lambda current: cancel(current, minute=2),
    )

    assert retry.token == first.token
    assert retry.command_fingerprint == first.command_fingerprint
    assert retry.base_request == first.base_request
    new_transport_retry = await repo.prepare_provider_operation(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
        idempotency_key="different-transport-key",
        mutation=lambda current: cancel(current, key="different-transport-key", minute=3),
    )
    assert new_transport_retry.logical_operation_id == first.logical_operation_id
    assert new_transport_retry.idempotency_key == first.idempotency_key
    with pytest.raises(ServiceRequestRepositoryConflict):
        await repo.prepare_provider_operation(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-1",
            idempotency_key="different-operation",
            mutation=lambda current: current.add_service(
                service="Drain clearing",
                expected_revision=created.request.revision,
                idempotency_key="different-operation",
                occurred_at=NOW + timedelta(minutes=3),
            ),
        )
    with pytest.raises(ServiceRequestRepositoryConflict):
        await repo.bind_provider(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-1",
            binding=ProviderBinding(
                kind="google_calendar",
                resource_id="different-provider-event",
            ),
        )
    assert (
        await repo.get_provider_binding(
            contractor_id="c2",
            customer_key=customer_key,
            request_id="request-1",
        )
        is None
    )
    assert (
        await repo.get_provider_binding(
            contractor_id="c1",
            customer_key=other_customer_key,
            request_id="request-1",
        )
        is None
    )
    with pytest.raises(ServiceRequestRepositoryConflict):
        await repo.prepare_provider_operation(
            contractor_id="c1",
            customer_key=other_customer_key,
            request_id="request-1",
            idempotency_key="provider-cancel-1",
            mutation=cancel,
        )


@pytest.mark.asyncio
async def test_firestore_restart_recovers_persisted_proposal(monkeypatch):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    customer_key = customer_key_for_phone("+16175550123")
    created = await _create_provider_backed(repo, customer_key=customer_key)
    prepared = await repo.prepare_provider_operation(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
        idempotency_key="transport-before-crash",
        mutation=lambda current: current.add_service(
            service="Drain clearing",
            expected_revision=created.request.revision,
            idempotency_key="transport-before-crash",
            occurred_at=NOW + timedelta(minutes=1),
        ),
    )

    restarted_repo = FirestoreServiceRequestRepository(client)
    recovered = await restarted_repo.recover_provider_operation(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-1",
    )

    assert recovered is not None
    assert recovered.logical_operation_id == prepared.logical_operation_id
    assert recovered.operation.value == "add_service"
    assert recovered.arguments == (("service", "Drain clearing"),)
    assert recovered.result.request.services == ("Furnace tune-up", "Drain clearing")
    finalized = await restarted_repo.finalize_provider_operation(recovered)
    assert finalized == recovered.result
    assert (
        await restarted_repo.get(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-1",
        )
        == recovered.result.request
    )


def _provider_create_mutation(*, request_id: str, idempotency_key: str, minute: int = 0):
    return lambda current: ServiceRequest.create(
        request_id=request_id,
        services=["Furnace tune-up"],
        scheduled_start=NOW + timedelta(days=1),
        scheduled_end=NOW + timedelta(days=1, hours=1),
        expected_revision=0,
        idempotency_key=idempotency_key,
        occurred_at=NOW + timedelta(minutes=minute),
        existing=current,
    )


@pytest.mark.asyncio
async def test_firestore_provider_create_is_hidden_recoverable_and_semantic(monkeypatch):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    customer_key = customer_key_for_phone("+16175550123")
    binding = ProviderBinding(kind="google_calendar", resource_id="stable-event-id")
    prepared = await repo.prepare_provider_create(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-create-1",
        idempotency_key="transport-1",
        binding=binding,
        title="Furnace tune-up",
        description="Caller requested annual maintenance.",
        mutation=_provider_create_mutation(
            request_id="request-create-1",
            idempotency_key="transport-1",
        ),
    )

    document = next(iter(client.store.values()))
    assert document["status"] == "pending_provider_create"
    assert document["provider_recovery_state"] == "pending"
    assert document["provider_recovery_attempts"] == 0
    assert document["next_attempt_at"] == NOW
    assert "expires_at" not in document
    assert "aggregate" not in document
    assert "provider_binding" not in document
    assert (
        await repo.get(contractor_id="c1", customer_key=customer_key, request_id="request-create-1")
        is None
    )
    assert (
        await repo.get_provider_binding(
            contractor_id="c1", customer_key=customer_key, request_id="request-create-1"
        )
        is None
    )
    assert await repo.list_actionable(contractor_id="c1", customer_key=customer_key) == ()

    restarted = FirestoreServiceRequestRepository(client)
    recovered = await restarted.recover_provider_create(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-create-1",
    )
    assert recovered == prepared
    retry = await restarted.prepare_provider_create(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-create-1",
        idempotency_key="new-transport-after-restart",
        binding=binding,
        title="  Furnace   tune-up  ",
        description="Caller requested annual maintenance.\r\n",
        mutation=_provider_create_mutation(
            request_id="request-create-1",
            idempotency_key="new-transport-after-restart",
            minute=5,
        ),
    )
    assert retry == prepared

    with pytest.raises(ServiceRequestRepositoryConflict):
        await restarted.prepare_provider_create(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-create-1",
            idempotency_key="different-semantic-create",
            binding=binding,
            title="Emergency furnace repair",
            description="Caller requested annual maintenance.",
            mutation=_provider_create_mutation(
                request_id="request-create-1",
                idempotency_key="different-semantic-create",
            ),
        )


@pytest.mark.asyncio
async def test_firestore_provider_create_finalizes_aggregate_binding_and_receipt_atomically(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    customer_key = customer_key_for_phone("+16175550123")
    binding = ProviderBinding(kind="google_calendar", resource_id="stable-event-id")
    prepared = await repo.prepare_provider_create(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-create-1",
        idempotency_key="transport-1",
        binding=binding,
        title="Furnace tune-up",
        description="Original notes",
        mutation=_provider_create_mutation(
            request_id="request-create-1",
            idempotency_key="transport-1",
        ),
    )

    finalized = await repo.finalize_provider_create(prepared)
    document = next(iter(client.store.values()))
    assert finalized == prepared.result
    assert document["status"] == "open"
    assert ServiceRequest.from_dict(document["aggregate"]) == prepared.result.request
    assert document["execution_kind"] == "provider"
    assert document["provider_binding"] == {
        "kind": "google_calendar",
        "resource_id": "stable-event-id",
    }
    assert "pending_provider_create" not in document
    assert "provider_recovery_state" not in document
    assert "provider_recovery_attempts" not in document
    assert "next_attempt_at" not in document
    assert document["expires_at"] > document["updated_at"]
    assert set(document["finalized_provider_create_receipt"]) == {
        "schema_version",
        "logical_operation_id",
        "semantic_fingerprint",
        "binding",
        "result",
    }

    # Crash after commit but before reply: both finalize and a later equivalent
    # prepare recover the receipt without issuing another provider create.
    restarted = FirestoreServiceRequestRepository(client)
    assert await restarted.finalize_provider_create(prepared) == prepared.result
    replay = await restarted.prepare_provider_create(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-create-1",
        idempotency_key="transport-after-lost-reply",
        binding=binding,
        title="Furnace tune-up",
        description="Original notes",
        mutation=_provider_create_mutation(
            request_id="request-create-1",
            idempotency_key="transport-after-lost-reply",
            minute=10,
        ),
    )
    assert replay.already_applied is True
    assert replay.logical_operation_id == prepared.logical_operation_id
    assert await restarted.finalize_provider_create(replay) == prepared.result

    # Later canonical commands may advance the aggregate, but the original
    # create receipt must remain replayable without rolling state backward.
    later = prepared.result.request.add_service(
        service="Drain clearing",
        expected_revision=1,
        idempotency_key="later-command",
        occurred_at=NOW + timedelta(hours=1),
    ).request
    document["aggregate"] = later.to_dict()
    document["updated_at"] = later.updated_at
    assert await restarted.finalize_provider_create(prepared) == prepared.result
    assert ServiceRequest.from_dict(document["aggregate"]) == later


@pytest.mark.asyncio
async def test_firestore_provider_create_rejects_cross_customer_and_forged_state(monkeypatch):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    customer_key = customer_key_for_phone("+16175550123")
    other_customer = customer_key_for_phone("+16175550124")
    prepared = await repo.prepare_provider_create(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-create-1",
        idempotency_key="transport-1",
        binding=ProviderBinding(kind="google_calendar", resource_id="stable-event-id"),
        title="Furnace tune-up",
        description="Original notes",
        mutation=_provider_create_mutation(
            request_id="request-create-1",
            idempotency_key="transport-1",
        ),
    )
    assert (
        await repo.recover_provider_create(
            contractor_id="c1",
            customer_key=other_customer,
            request_id="request-create-1",
        )
        is None
    )
    assert (
        await repo.recover_provider_create(
            contractor_id="c2",
            customer_key=customer_key,
            request_id="request-create-1",
        )
        is None
    )

    path = next(iter(client.store))
    client.store[path]["provider_binding"] = {
        "kind": "google_calendar",
        "resource_id": "forged-visible-binding",
    }
    with pytest.raises(ServiceRequestRepositoryConflict):
        await repo.recover_provider_create(
            contractor_id="c1",
            customer_key=customer_key,
            request_id="request-create-1",
        )
    with pytest.raises(ServiceRequestRepositoryConflict):
        await repo.finalize_provider_create(prepared)


@pytest.mark.asyncio
async def test_firestore_provider_create_finalize_failure_leaves_only_hidden_intent(monkeypatch):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    customer_key = customer_key_for_phone("+16175550123")
    prepared = await repo.prepare_provider_create(
        contractor_id="c1",
        customer_key=customer_key,
        request_id="request-create-1",
        idempotency_key="transport-1",
        binding=ProviderBinding(kind="google_calendar", resource_id="stable-event-id"),
        title="Furnace tune-up",
        description="Original notes",
        mutation=_provider_create_mutation(
            request_id="request-create-1",
            idempotency_key="transport-1",
        ),
    )

    class _FailBeforeFinalize(_Transaction):
        def set(self, ref, data, merge=False):
            if "aggregate" in data and "provider_binding" in data:
                raise RuntimeError("injected finalization failure")
            return super().set(ref, data, merge=merge)

    client.transaction = lambda: _FailBeforeFinalize(client.store)
    with pytest.raises(RuntimeError, match="injected finalization failure"):
        await repo.finalize_provider_create(prepared)

    document = next(iter(client.store.values()))
    assert document["status"] == "pending_provider_create"
    assert "aggregate" not in document
    assert "provider_binding" not in document
    assert await repo.list_actionable(contractor_id="c1", customer_key=customer_key) == ()

    client.transaction = lambda: _Transaction(client.store)
    assert await repo.finalize_provider_create(prepared) == prepared.result
    document = next(iter(client.store.values()))
    assert document["status"] == "open"
    assert "aggregate" in document and "provider_binding" in document
