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
from app.services import calendar
from app.services.google_calendar_request_provider import GoogleCalendarRequestProvider
from app.services.service_request_repository import ProviderBinding, customer_key_for_phone

NOW = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
CUSTOMER_KEY = customer_key_for_phone("+16175550123")


class _Snapshot:
    def __init__(self, path, store):
        self.id = path.rsplit("/", 1)[-1]
        self.reference = _Doc(path, store)
        data = store.get(path)
        self._data = dict(data) if isinstance(data, dict) else data
        self.exists = self._data is not None
        self.read_time = datetime.now(UTC)

    def to_dict(self):
        return dict(self._data) if isinstance(self._data, dict) else self._data


class _Doc:
    def __init__(self, path, store):
        self.path, self.store = path, store

    def collection(self, name):
        return _Query(f"{self.path}/{name}", self.store)

    def get(self, transaction=None):
        return _Snapshot(self.path, self.store)

    def update(self, updates):
        if self.path not in self.store or not isinstance(self.store[self.path], dict):
            self.store[self.path] = {}
        for k, v in updates.items():
            if str(type(v).__name__) == "Sentinel" or "DELETE" in str(v):
                self.store[self.path].pop(k, None)
            else:
                self.store[self.path][k] = v

    def set(self, data, merge=False):
        if merge and self.path in self.store and isinstance(self.store[self.path], dict):
            self.store[self.path].update(data)
        else:
            self.store[self.path] = dict(data) if isinstance(data, dict) else data

    def delete(self):
        self.store.pop(self.path, None)


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

    def get(self, ref):
        return ref.get(transaction=self)

    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)

    def update(self, ref, updates):
        ref.update(updates)

    def delete(self, ref):
        ref.delete()


class _Client:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return _Query(name, self.store)

    def collection_group(self, name):
        return _Query(name, self.store, group=True)

    def transaction(self):
        return _Transaction(self.store)


def _setup_contractor_in_store(client, contractor_id="c1", **overrides):
    contractor = {
        "contractor_id": contractor_id,
        "active": True,
        "google_calendar_access_token": "access-token",
        "google_calendar_refresh_token": "refresh-token",
        "google_calendar_generation": 0,
        "google_calendar_lifecycle_epoch": 0,
        "google_calendar_connected": True,
    }
    contractor.update(overrides)
    client.store[f"contractors/{contractor_id}"] = dict(contractor)
    return contractor


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
    monkeypatch.setattr(
        "app.services.integration_token_mutations.transactional",
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


async def _pending_reschedule(repo):
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
        idempotency_key="reschedule-transport",
        mutation=lambda current: current.reschedule(
            scheduled_start=NOW + timedelta(days=2),
            scheduled_end=NOW + timedelta(days=2, hours=1),
            expected_revision=created.request.revision,
            idempotency_key="reschedule-transport",
            occurred_at=NOW,
        ),
    )


@pytest.mark.asyncio
async def test_recovery_reschedule_remote_desired_finalizes_exactly_once(monkeypatch):
    import base64
    import app.db.firestore_client as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app.config import settings
    from app.services.integration_tokens import parse_provider_operation_intent

    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}')
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    monkeypatch.setattr(settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "client-secret")
    calendar._REFRESH_LOCKS.clear()

    client = _Client()
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: client)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: client)

    _setup_contractor_in_store(client, "c1")
    repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_reschedule(repo)

    base_start_iso = (NOW + timedelta(days=1)).isoformat()
    base_end_iso = (NOW + timedelta(days=1, hours=1)).isoformat()
    desired_start_iso = (NOW + timedelta(days=2)).isoformat()
    desired_end_iso = (NOW + timedelta(days=2, hours=1)).isoformat()

    # Step 1: First attempt - GET returns base schedule, PATCH simulates timeout after server records write
    calls = []

    class _FakeResponse:
        def __init__(self, status_code: int, body: dict | None = None):
            self.status_code = status_code
            self._body = body or {}
            self.text = str(self._body)

        def json(self):
            return self._body

    class _FirstAttemptClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            return _FakeResponse(
                200,
                {
                    "etag": '"etag-base-1"',
                    "start": {"dateTime": base_start_iso},
                    "end": {"dateTime": base_end_iso},
                },
            )

        async def patch(self, url, **kwargs):
            calls.append(("PATCH", url, kwargs))
            # Server records the PATCH, but client transport times out before receiving reply
            raise TimeoutError("Simulated transport timeout after PATCH dispatch")

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _FirstAttemptClient)

    contractor_config = client.store["contractors/c1"]
    adapter = GoogleCalendarRequestProvider(contractor_config)

    # First attempt fails due to timeout
    first_attempt_ok = await adapter.reschedule(
        binding=ProviderBinding(kind="google_calendar", resource_id="stable-event-id"),
        request=prepared.base_request,
        scheduled_start=NOW + timedelta(days=2),
        scheduled_end=NOW + timedelta(days=2, hours=1),
        idempotency_key=prepared.logical_operation_id,
    )
    assert first_attempt_ok is False
    assert len(calls) == 2
    assert calls[0][0] == "GET"
    assert calls[1][0] == "PATCH"

    # Verify durable provider intent is NO LONGER stuck in started phase
    contractor_post = client.store["contractors/c1"]
    status, intent, _ = parse_provider_operation_intent(contractor_post, "google_calendar")
    assert status == "absent" or intent is None

    # Assert canonical base and pending operation are intact before recovery
    doc_before_recovery = client.store[f"contractors/c1/service_requests/{prepared.base_request.request_id}"]
    assert doc_before_recovery["status"] == "open"
    assert PENDING_PROVIDER_OPERATION_FIELD in doc_before_recovery
    base_agg = ServiceRequest.from_dict(doc_before_recovery["aggregate"])
    assert base_agg.scheduled_start == NOW + timedelta(days=1)
    assert base_agg.scheduled_end == NOW + timedelta(days=1, hours=1)
    assert base_agg.revision == 1

    # Step 2: Recovery attempt using production GoogleCalendarRequestProvider
    # Remote state is now desired schedule (since PATCH reached provider before timeout)
    recovery_calls = []

    class _RecoveryClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            recovery_calls.append(("GET", url, kwargs))
            return _FakeResponse(
                200,
                {
                    "etag": '"etag-desired-2"',
                    "start": {"dateTime": desired_start_iso},
                    "end": {"dateTime": desired_end_iso},
                },
            )

        async def patch(self, url, **kwargs):
            recovery_calls.append(("PATCH", url, kwargs))
            raise AssertionError("Zero PATCH calls must be emitted on GET-only desired reconciliation")

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _RecoveryClient)

    async def load_contractor(contractor_id):
        return client.store.get(f"contractors/{contractor_id}")

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=load_contractor,
        adapter_factory=lambda cfg: GoogleCalendarRequestProvider(cfg),
        clock=lambda: NOW,
        owner="worker-one",
        lease_seconds=15,
    )

    result = await worker.run_once()

    assert result.claimed == 1
    assert result.finalized == 1
    assert result.deferred == 0

    # Production GoogleCalendarRequestProvider fresh-GETs remote desired and emits ZERO second PATCH
    assert len(recovery_calls) == 1
    assert recovery_calls[0][0] == "GET"
    assert recovery_calls[0][1] == f"{calendar.EVENTS_URL}/stable-event-id"

    # Assert ServiceRequest was finalized exactly once to desired schedule
    document = client.store[f"contractors/c1/service_requests/{prepared.base_request.request_id}"]
    assert document["status"] == "open"
    assert document["execution_kind"] == "provider"
    assert PENDING_PROVIDER_OPERATION_FIELD not in document
    aggregate = ServiceRequest.from_dict(document["aggregate"])
    assert aggregate.scheduled_start == NOW + timedelta(days=2)
    assert aggregate.scheduled_end == NOW + timedelta(days=2, hours=1)
    assert aggregate.revision == 2


@pytest.mark.asyncio
async def test_recovery_reschedule_third_schedule_never_patches_and_reaches_needs_review(monkeypatch):
    import base64
    import app.db.firestore_client as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app.config import settings

    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}')
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    monkeypatch.setattr(settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "client-secret")
    calendar._REFRESH_LOCKS.clear()

    client = _Client()
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: client)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: client)

    _setup_contractor_in_store(client, "c1")
    repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_reschedule(repo)

    third_start_iso = (NOW + timedelta(days=5)).isoformat()
    third_end_iso = (NOW + timedelta(days=5, hours=1)).isoformat()

    http_calls = []

    class _FakeResponse:
        def __init__(self, status_code: int, body: dict | None = None):
            self.status_code = status_code
            self._body = body or {}
            self.text = str(self._body)

        def json(self):
            return self._body

    class _ThirdScheduleClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            http_calls.append(("GET", url, kwargs))
            return _FakeResponse(
                200,
                {
                    "etag": '"etag-third-1"',
                    "start": {"dateTime": third_start_iso},
                    "end": {"dateTime": third_end_iso},
                },
            )

        async def patch(self, url, **kwargs):
            http_calls.append(("PATCH", url, kwargs))
            raise AssertionError("Zero PATCH calls must be emitted on third schedule conflict")

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _ThirdScheduleClient)

    async def load_contractor(contractor_id):
        return client.store.get(f"contractors/{contractor_id}")

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=load_contractor,
        adapter_factory=lambda cfg: GoogleCalendarRequestProvider(cfg),
        clock=lambda: NOW,
        owner="worker-one",
        lease_seconds=15,
        max_attempts=2,
    )

    # First attempt: GET returns third schedule -> adapter returns False (zero PATCH) -> deferred
    first_result = await worker.run_once()
    assert first_result.claimed == 1
    assert first_result.finalized == 0
    assert first_result.deferred == 1
    assert len(http_calls) == 1
    assert http_calls[0][0] == "GET"

    # Second attempt after backoff -> reaches needs_review (zero PATCH)
    worker._clock = lambda: NOW + timedelta(seconds=30)
    second_result = await worker.run_once()
    assert second_result.claimed == 1
    assert second_result.finalized == 0
    assert second_result.deferred == 1
    assert len(http_calls) == 2
    assert http_calls[1][0] == "GET"

    # Total HTTP calls across both recovery attempts are exactly 2 GETs and 0 PATCHes!
    assert all(method == "GET" for method, _, _ in http_calls)

    document = client.store[f"contractors/c1/service_requests/{prepared.base_request.request_id}"]
    assert document[PROVIDER_RECOVERY_STATE_FIELD] == PROVIDER_RECOVERY_NEEDS_REVIEW
    assert document[PROVIDER_RECOVERY_ATTEMPTS_FIELD] == 2
    assert PENDING_PROVIDER_OPERATION_FIELD in document
    assert "expires_at" not in document
    # Canonical base aggregate is preserved intact!
    aggregate = ServiceRequest.from_dict(document["aggregate"])
    assert aggregate.scheduled_start == NOW + timedelta(days=1)
    assert aggregate.scheduled_end == NOW + timedelta(days=1, hours=1)
    assert aggregate.revision == 1


@pytest.mark.asyncio
async def test_recovery_reschedule_malformed_patch_response_retains_base_and_pending(monkeypatch):
    import base64
    import app.db.firestore_client as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app.config import settings

    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}')
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    monkeypatch.setattr(settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "client-secret")
    calendar._REFRESH_LOCKS.clear()

    client = _Client()
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: client)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: client)

    _setup_contractor_in_store(client, "c1")
    repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_reschedule(repo)

    base_start_iso = (NOW + timedelta(days=1)).isoformat()
    base_end_iso = (NOW + timedelta(days=1, hours=1)).isoformat()
    desired_start_iso = (NOW + timedelta(days=2)).isoformat()
    desired_end_iso = (NOW + timedelta(days=2, hours=1)).isoformat()

    class _FakeResponse:
        def __init__(self, status_code: int, body: dict | None = None):
            self.status_code = status_code
            self._body = body or {}
            self.text = str(self._body)

        def json(self):
            return self._body

    # 1. Recovery attempt where PATCH returns 200 with mismatched/malformed schedule body
    attempt_calls = []

    class _MalformedPatchClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            attempt_calls.append(("GET", url, kwargs))
            return _FakeResponse(
                200,
                {
                    "etag": '"etag-base-1"',
                    "start": {"dateTime": base_start_iso},
                    "end": {"dateTime": base_end_iso},
                },
            )

        async def patch(self, url, **kwargs):
            attempt_calls.append(("PATCH", url, kwargs))
            # 200 OK but response body has non-desired schedule
            return _FakeResponse(
                200,
                {
                    "etag": '"etag-mismatched"',
                    "start": {"dateTime": (NOW + timedelta(days=9)).isoformat()},
                    "end": {"dateTime": (NOW + timedelta(days=9, hours=1)).isoformat()},
                },
            )

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _MalformedPatchClient)

    async def load_contractor(contractor_id):
        return client.store.get(f"contractors/{contractor_id}")

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=load_contractor,
        adapter_factory=lambda cfg: GoogleCalendarRequestProvider(cfg),
        clock=lambda: NOW,
        owner="worker-one",
        lease_seconds=15,
        max_attempts=4,
    )

    res1 = await worker.run_once()
    assert res1.claimed == 1
    assert res1.finalized == 0
    assert res1.deferred == 1

    # Base schedule and pending operation are preserved!
    doc = client.store[f"contractors/c1/service_requests/{prepared.base_request.request_id}"]
    assert doc["status"] == "open"
    assert PENDING_PROVIDER_OPERATION_FIELD in doc
    agg = ServiceRequest.from_dict(doc["aggregate"])
    assert agg.scheduled_start == NOW + timedelta(days=1)
    assert agg.scheduled_end == NOW + timedelta(days=1, hours=1)
    assert agg.revision == 1

    # 2. Subsequent recovery attempt where remote returns valid desired schedule -> finalizes cleanly
    reconcile_calls = []

    class _ValidReconcileClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            reconcile_calls.append(("GET", url, kwargs))
            return _FakeResponse(
                200,
                {
                    "etag": '"etag-desired-valid"',
                    "start": {"dateTime": desired_start_iso},
                    "end": {"dateTime": desired_end_iso},
                },
            )

        async def patch(self, url, **kwargs):
            reconcile_calls.append(("PATCH", url, kwargs))
            raise AssertionError("Zero PATCH expected on GET-only desired reconciliation")

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _ValidReconcileClient)
    worker._clock = lambda: NOW + timedelta(seconds=30)

    res2 = await worker.run_once()
    assert res2.claimed == 1
    assert res2.finalized == 1
    assert res2.deferred == 0
    assert len(reconcile_calls) == 1
    assert reconcile_calls[0][0] == "GET"

    # Finalized aggregate has desired schedule and revision 2
    final_doc = client.store[f"contractors/c1/service_requests/{prepared.base_request.request_id}"]
    assert final_doc["status"] == "open"
    assert PENDING_PROVIDER_OPERATION_FIELD not in final_doc
    final_agg = ServiceRequest.from_dict(final_doc["aggregate"])
    assert final_agg.scheduled_start == NOW + timedelta(days=2)
    assert final_agg.scheduled_end == NOW + timedelta(days=2, hours=1)
    assert final_agg.revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("malformed_body", "label"),
    [
        # Missing "end" key entirely → _validate_non_recurring_timed_event returns None.
        (
            {
                "etag": '"etag-malformed-missing-end"',
                "start": {"dateTime": "2026-09-10T10:00:00Z"},
                # no "end"
            },
            "missing_end",
        ),
        # Naive (non-aware) dateTime → _parse_aware_datetime returns None.
        (
            {
                "etag": '"etag-malformed-naive-dt"',
                "start": {"dateTime": "2026-09-10T10:00:00"},   # no timezone offset
                "end":   {"dateTime": "2026-09-10T11:00:00"},   # no timezone offset
            },
            "naive_datetime",
        ),
    ],
)
async def test_recovery_reschedule_truly_malformed_patch_2xx_retains_base_and_pending(
    monkeypatch, malformed_body, label
):
    """P2 composed malformed-response proof.

    A PATCH that returns 200 with a truly malformed body (missing end or naive dateTime)
    must leave base schedule and pending operation intact with zero finalization.
    A later valid desired GET must then finalize exactly once with no second PATCH.
    """
    import base64

    import app.db.firestore_client as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app.config import settings


    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}')
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    monkeypatch.setattr(settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "client-secret")
    calendar._REFRESH_LOCKS.clear()

    client = _Client()
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: client)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: client)

    _setup_contractor_in_store(client, "c1")
    repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_reschedule(repo)

    base_start_iso = (NOW + timedelta(days=1)).isoformat()
    base_end_iso = (NOW + timedelta(days=1, hours=1)).isoformat()
    desired_start_iso = (NOW + timedelta(days=2)).isoformat()
    desired_end_iso = (NOW + timedelta(days=2, hours=1)).isoformat()

    class _FakeResponse:
        def __init__(self, status_code: int, body: dict | None = None):
            self.status_code = status_code
            self._body = body or {}
            self.text = str(self._body)

        def json(self):
            return self._body

    # Phase 1: PATCH returns 200 with a truly malformed body.
    attempt_calls = []

    class _TrulyMalformedPatchClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            attempt_calls.append(("GET", url))
            return _FakeResponse(
                200,
                {
                    "etag": '"etag-base-valid"',
                    "start": {"dateTime": base_start_iso},
                    "end": {"dateTime": base_end_iso},
                },
            )

        async def patch(self, url, **kwargs):
            attempt_calls.append(("PATCH", url))
            # 200 OK but body is truly malformed (fails _validate_non_recurring_timed_event).
            return _FakeResponse(200, malformed_body)

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _TrulyMalformedPatchClient)

    async def load_contractor(contractor_id):
        return client.store.get(f"contractors/{contractor_id}")

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=load_contractor,
        adapter_factory=lambda cfg: GoogleCalendarRequestProvider(cfg),
        clock=lambda: NOW,
        owner="worker-malformed",
        lease_seconds=15,
        max_attempts=4,
    )

    res1 = await worker.run_once()
    # Claimed one request; zero finalized (malformed body → reschedule returns False).
    assert res1.claimed == 1, f"{label}: expected 1 claimed, got {res1.claimed}"
    assert res1.finalized == 0, f"{label}: expected 0 finalized, got {res1.finalized}"
    assert res1.deferred == 1, f"{label}: expected 1 deferred, got {res1.deferred}"

    # GET + PATCH were both issued.
    assert len(attempt_calls) == 2, f"{label}: expected 2 HTTP calls, got {attempt_calls}"
    assert attempt_calls[0][0] == "GET"
    assert attempt_calls[1][0] == "PATCH"

    # Base schedule and pending operation are preserved.
    doc = client.store[
        f"contractors/c1/service_requests/{prepared.base_request.request_id}"
    ]
    assert doc["status"] == "open", f"{label}: status must remain open"
    assert PENDING_PROVIDER_OPERATION_FIELD in doc, f"{label}: pending op must be retained"
    agg = ServiceRequest.from_dict(doc["aggregate"])
    assert agg.scheduled_start == NOW + timedelta(days=1), f"{label}: base start must be retained"
    assert agg.scheduled_end == NOW + timedelta(days=1, hours=1), (
        f"{label}: base end must be retained"
    )
    assert agg.revision == 1, f"{label}: revision must not advance"

    # Phase 2: Valid desired GET → finalize exactly once, zero second PATCH.
    reconcile_calls = []

    class _ValidDesiredClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            reconcile_calls.append(("GET", url))
            return _FakeResponse(
                200,
                {
                    "etag": '"etag-desired-valid"',
                    "start": {"dateTime": desired_start_iso},
                    "end": {"dateTime": desired_end_iso},
                },
            )

        async def patch(self, url, **kwargs):
            reconcile_calls.append(("PATCH", url))
            raise AssertionError(f"{label}: zero PATCH expected on GET-only desired reconciliation")

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _ValidDesiredClient)
    worker._clock = lambda: NOW + timedelta(seconds=30)

    res2 = await worker.run_once()
    assert res2.claimed == 1, f"{label}: expected 1 claimed on reconcile, got {res2.claimed}"
    assert res2.finalized == 1, f"{label}: expected 1 finalized, got {res2.finalized}"
    assert res2.deferred == 0, f"{label}: expected 0 deferred, got {res2.deferred}"
    assert len(reconcile_calls) == 1, f"{label}: expected exactly 1 GET, got {reconcile_calls}"
    assert reconcile_calls[0][0] == "GET"

    # Finalized aggregate has desired schedule and advanced revision.
    final_doc = client.store[
        f"contractors/c1/service_requests/{prepared.base_request.request_id}"
    ]
    assert final_doc["status"] == "open"
    assert PENDING_PROVIDER_OPERATION_FIELD not in final_doc, f"{label}: pending op must be cleared"
    final_agg = ServiceRequest.from_dict(final_doc["aggregate"])
    assert final_agg.scheduled_start == NOW + timedelta(days=2), (
        f"{label}: desired start must be set"
    )
    assert final_agg.scheduled_end == NOW + timedelta(days=2, hours=1), (
        f"{label}: desired end must be set"
    )
    assert final_agg.revision == 2, f"{label}: revision must advance to 2"
