import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.db.service_requests import (
    NEXT_ATTEMPT_AT_FIELD,
    PENDING_PROVIDER_CREATE_FIELD,
    PENDING_PROVIDER_OPERATION_FIELD,
    PROVIDER_RECOVERY_ATTEMPTS_FIELD,
    PROVIDER_RECOVERY_LEASE_FIELD,
    PROVIDER_RECOVERY_NEEDS_REVIEW,
    PROVIDER_RECOVERY_STATE_FIELD,
    FirestoreServiceRequestRepository,
)
from app.services import service_request_recovery as recovery_module
from app.services.service_request import ServiceRequest
from app.services.service_request_recovery import (
    ServiceRequestRecoveryWorker,
    service_request_recovery_worker_loop,
)
from app.services.service_request_repository import ProviderBinding, customer_key_for_phone

NOW = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
CUSTOMER_KEY = customer_key_for_phone("+16175550123")


class _Snapshot:
    def __init__(self, path, store):
        self.id = path.rsplit("/", 1)[-1]
        self.reference = _Doc(path, store)
        self._data = store.get(path)
        self.exists = self._data is not None

    def to_dict(self):
        return self._data


class _Doc:
    def __init__(self, path, store):
        self.path, self.store = path, store

    def collection(self, name):
        return _Query(f"{self.path}/{name}", self.store)

    def get(self, transaction=None):
        return _Snapshot(self.path, self.store)


class _Query:
    def __init__(self, path, store, *, group=False):
        self.path = path
        self.store = store
        self.group = group
        self.filters = []
        self.cap = 100
        self.order_field = None
        self.order_direction = None

    def document(self, doc_id):
        return _Doc(f"{self.path}/{doc_id}", self.store)

    def where(self, filter):
        self.filters.append((filter.field_path, filter.op_string, filter.value))
        return self

    def limit(self, value):
        self.cap = value
        return self

    def order_by(self, field, direction):
        self.order_field = field
        self.order_direction = direction
        return self

    def stream(self):
        rows = []
        prefix = f"{self.path}/"
        for path, data in self.store.items():
            parts = path.split("/")
            selected = (
                len(parts) >= 2 and parts[-2] == self.path
                if self.group
                else path.startswith(prefix)
            )
            if not selected or not self._matches(data):
                continue
            rows.append(_Snapshot(path, self.store))
        if self.order_field:
            rows.sort(
                key=lambda snapshot: snapshot.to_dict()[self.order_field],
                reverse=self.order_direction == "DESCENDING",
            )
        return rows[: self.cap]

    def _matches(self, data):
        for field, operator, expected in self.filters:
            actual = data.get(field)
            if operator == "==" and actual != expected:
                return False
            if operator == "<=" and (actual is None or actual > expected):
                return False
        return True


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
        return _Query(name, self.store)

    def collection_group(self, name):
        return _Query(name, self.store, group=True)

    def transaction(self):
        return _Transaction(self.store)


def _create_mutation(request_id="request-create-1", key="transport-1"):
    return lambda current: ServiceRequest.create(
        request_id=request_id,
        services=["Furnace tune-up"],
        scheduled_start=NOW + timedelta(days=1),
        scheduled_end=NOW + timedelta(days=1, hours=1),
        expected_revision=0,
        idempotency_key=key,
        occurred_at=NOW,
        existing=current,
    )


async def _pending_create(repo, *, binding_kind="google_calendar"):
    return await repo.prepare_provider_create(
        contractor_id="c1",
        customer_key=CUSTOMER_KEY,
        request_id="request-create-1",
        idempotency_key="transport-1",
        binding=ProviderBinding(kind=binding_kind, resource_id="stable-event-id"),
        title="Furnace tune-up",
        description="Original notes",
        mutation=_create_mutation(),
    )


async def _pending_cancel(repo):
    provider_create = await repo.prepare_provider_create(
        contractor_id="c1",
        customer_key=CUSTOMER_KEY,
        request_id="request-1",
        idempotency_key="create-1",
        binding=ProviderBinding(kind="google_calendar", resource_id="stable-event-id"),
        title="Furnace tune-up",
        description="Original notes",
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
    created = await repo.finalize_provider_create(provider_create)
    return await repo.prepare_provider_operation(
        contractor_id="c1",
        customer_key=CUSTOMER_KEY,
        request_id="request-1",
        idempotency_key="cancel-transport",
        mutation=lambda current: current.cancel(
            expected_revision=created.request.revision,
            idempotency_key="cancel-transport",
            occurred_at=NOW,
        ),
    )


@pytest.fixture(autouse=True)
def _transaction_decorator(monkeypatch):
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        lambda fn: lambda tx: fn(tx),
    )


@pytest.mark.asyncio
async def test_dual_workers_lease_once_and_stale_owner_cannot_finalize():
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_create(repo)

    first = await repo.claim_due_provider_recoveries(owner="worker-one", now=NOW, lease_seconds=15)
    simultaneous = await repo.claim_due_provider_recoveries(
        owner="worker-two", now=NOW, lease_seconds=15
    )

    assert len(first) == 1
    assert simultaneous == ()
    reclaimed = await repo.claim_due_provider_recoveries(
        owner="worker-two", now=NOW + timedelta(seconds=16), lease_seconds=15
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].attempt == 2
    assert reclaimed[0].preparation.logical_operation_id == prepared.logical_operation_id
    assert await repo.finalize_provider_recovery(first[0]) is False
    assert await repo.finalize_provider_recovery(reclaimed[0]) is True
    document = next(iter(client.store.values()))
    assert document["status"] == "open"
    assert document["execution_kind"] == "provider"
    assert "provider_binding" in document
    assert PROVIDER_RECOVERY_LEASE_FIELD not in document


@pytest.mark.asyncio
async def test_failures_back_off_then_retain_needs_review_proposal():
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    await _pending_cancel(repo)

    first = (
        await repo.claim_due_provider_recoveries(
            owner="worker-one", now=NOW, lease_seconds=15, max_attempts=2
        )
    )[0]
    assert await repo.release_provider_recovery(first, now=NOW, max_attempts=2)
    document = next(iter(client.store.values()))
    assert document[PROVIDER_RECOVERY_ATTEMPTS_FIELD] == 1
    assert document[NEXT_ATTEMPT_AT_FIELD] == NOW + timedelta(seconds=30)
    assert PENDING_PROVIDER_OPERATION_FIELD in document
    assert "expires_at" not in document

    assert (
        await repo.claim_due_provider_recoveries(
            owner="worker-two", now=NOW + timedelta(seconds=29), max_attempts=2
        )
        == ()
    )
    second = (
        await repo.claim_due_provider_recoveries(
            owner="worker-two", now=NOW + timedelta(seconds=30), max_attempts=2
        )
    )[0]
    assert await repo.release_provider_recovery(
        second,
        now=NOW + timedelta(seconds=30),
        max_attempts=2,
    )
    document = next(iter(client.store.values()))
    assert document[PROVIDER_RECOVERY_STATE_FIELD] == PROVIDER_RECOVERY_NEEDS_REVIEW
    assert document[PROVIDER_RECOVERY_ATTEMPTS_FIELD] == 2
    assert PENDING_PROVIDER_OPERATION_FIELD in document
    assert PROVIDER_RECOVERY_LEASE_FIELD not in document
    assert "expires_at" not in document


@pytest.mark.asyncio
async def test_worker_replays_stable_create_even_when_current_opt_in_is_false():
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_create(repo)
    seen = []

    class _Adapter:
        async def create(self, **kwargs):
            seen.append(kwargs)
            return True

    async def load_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "service_request_mutations_enabled": False,
        }

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=load_contractor,
        adapter_factory=lambda _config: _Adapter(),
        clock=lambda: NOW,
        owner="worker-one",
        lease_seconds=15,
    )
    result = await worker.run_once()

    assert result.claimed == result.finalized == 1
    assert result.deferred == 0
    assert seen[0]["idempotency_key"] == prepared.logical_operation_id
    assert seen[0]["binding"] == prepared.binding


@pytest.mark.asyncio
async def test_finalize_crash_retries_same_desired_state_and_logical_id():
    client = _Client()
    durable_repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_cancel(durable_repo)
    provider_ids = []

    class _Adapter:
        async def cancel(self, **kwargs):
            provider_ids.append(kwargs["idempotency_key"])
            return True

    class _FailFinalizeOnce:
        def __init__(self):
            self.failed = False

        async def claim_due_provider_recoveries(self, **kwargs):
            return await durable_repo.claim_due_provider_recoveries(**kwargs)

        async def finalize_provider_recovery(self, lease):
            if not self.failed:
                self.failed = True
                raise RuntimeError("injected crash before finalize")
            return await durable_repo.finalize_provider_recovery(lease)

        async def release_provider_recovery(self, lease, **kwargs):
            return await durable_repo.release_provider_recovery(lease, **kwargs)

    async def load_contractor(contractor_id):
        return {"contractor_id": contractor_id}

    clock_value = NOW
    repository = _FailFinalizeOnce()
    first_worker = ServiceRequestRecoveryWorker(
        repository,
        contractor_loader=load_contractor,
        adapter_factory=lambda _config: _Adapter(),
        clock=lambda: clock_value,
        owner="worker-before-crash",
        lease_seconds=15,
    )
    first = await first_worker.run_once()
    assert first.claimed == first.deferred == 1
    assert first.finalized == 0

    clock_value = NOW + timedelta(seconds=30)
    restarted_worker = ServiceRequestRecoveryWorker(
        repository,
        contractor_loader=load_contractor,
        adapter_factory=lambda _config: _Adapter(),
        clock=lambda: clock_value,
        owner="worker-after-crash",
        lease_seconds=15,
    )
    second = await restarted_worker.run_once()

    assert second.claimed == second.finalized == 1
    assert provider_ids == [
        prepared.logical_operation_id,
        prepared.logical_operation_id,
    ]
    document = next(iter(client.store.values()))
    assert document["status"] == "cancelled"
    assert document["execution_kind"] == "provider"
    assert PENDING_PROVIDER_OPERATION_FIELD not in document
    assert document["expires_at"] > document["updated_at"]


@pytest.mark.asyncio
async def test_worker_rejects_non_google_binding_without_constructing_adapter():
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    await _pending_create(repo, binding_kind="other_calendar")
    factory_called = False

    def factory(_config):
        nonlocal factory_called
        factory_called = True
        raise AssertionError("unsupported provider must not be constructed")

    async def load_contractor(contractor_id):
        return {"contractor_id": contractor_id}

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=load_contractor,
        adapter_factory=factory,
        clock=lambda: NOW,
        owner="worker-one",
        lease_seconds=15,
    )
    result = await worker.run_once()

    assert result == replace(result, claimed=1, finalized=0, deferred=1)
    assert factory_called is False
    document = next(iter(client.store.values()))
    assert document[PROVIDER_RECOVERY_STATE_FIELD] == "pending"
    assert PENDING_PROVIDER_CREATE_FIELD in document


@pytest.mark.asyncio
async def test_worker_bounds_contractor_lookup_timeout(monkeypatch):
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    await _pending_create(repo)
    never_returns = asyncio.Event()

    async def load_contractor(_contractor_id):
        await never_returns.wait()

    original_wait_for = recovery_module.asyncio.wait_for

    async def immediate_wait_for(awaitable, *, timeout):
        if timeout == recovery_module.CONTRACTOR_LOAD_TIMEOUT_SECONDS and asyncio.iscoroutine(
            awaitable
        ):
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(recovery_module.asyncio, "wait_for", immediate_wait_for)
    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=load_contractor,
        clock=lambda: NOW,
        owner="worker-one",
        lease_seconds=15,
    )

    result = await worker.run_once()

    assert result.claimed == result.deferred == 1
    document = next(iter(client.store.values()))
    assert document[PROVIDER_RECOVERY_STATE_FIELD] == "pending"
    assert PROVIDER_RECOVERY_LEASE_FIELD not in document


@pytest.mark.asyncio
async def test_worker_loop_cancels_cleanly():
    entered = asyncio.Event()

    class _BlockingWorker:
        async def run_once(self):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(service_request_recovery_worker_loop(worker=_BlockingWorker()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
