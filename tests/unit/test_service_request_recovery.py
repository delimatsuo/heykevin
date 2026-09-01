import asyncio
import base64
import os
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, RLock

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


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = str(self._body)

    def json(self):
        return self._body


class _InjectedTransactionFailure(RuntimeError):
    pass


class _OptimisticVersionConflict(RuntimeError):
    pass


class _CommitGate:
    def __init__(self, mode):
        self.mode = mode
        self.entered = Event()
        self.release = Event()
        self.finished = Event()

    def wait(self):
        self.entered.set()
        if not self.release.wait(timeout=5.0):
            raise AssertionError("transaction commit gate was not released")


class _VersionedStore(dict):
    def __init__(self):
        super().__init__()
        self.lock = RLock()
        self.versions = {}

    def publish(self, path, data):
        with self.lock:
            if data is _MISSING:
                dict.pop(self, path, None)
            elif path in self and isinstance(dict.__getitem__(self, path), dict) and isinstance(
                data, dict
            ):
                current = dict.__getitem__(self, path)
                current.clear()
                current.update(deepcopy(data))
            else:
                dict.__setitem__(self, path, deepcopy(data))
            self.versions[path] = self.versions.get(path, 0) + 1

    def __setitem__(self, path, data):
        self.publish(path, data)

    def __delitem__(self, path):
        self.publish(path, _MISSING)


_MISSING = object()


class _Snapshot:
    def __init__(self, path, store, *, data=_MISSING, version=None):
        self.id = path.rsplit("/", 1)[-1]
        self.reference = _Doc(path, store)
        if data is _MISSING:
            with store.lock:
                data = deepcopy(store.get(path))
                version = store.versions.get(path, 0)
        self._data = deepcopy(data)
        self.version = version
        self.exists = self._data is not None
        self.read_time = datetime.now(UTC)

    def to_dict(self):
        return deepcopy(self._data)


class _Doc:
    def __init__(self, path, store):
        self.path, self.store = path, store

    def collection(self, name):
        return _Query(f"{self.path}/{name}", self.store)

    def get(self, transaction=None):
        if transaction is not None:
            return transaction._read(self)
        return _Snapshot(self.path, self.store)

    def update(self, updates):
        with self.store.lock:
            current = deepcopy(self.store.get(self.path))
            updated = current if isinstance(current, dict) else {}
            _apply_updates(updated, updates)
            self.store.publish(self.path, updated)

    def set(self, data, merge=False):
        with self.store.lock:
            if merge and isinstance(self.store.get(self.path), dict):
                updated = deepcopy(self.store[self.path])
                updated.update(deepcopy(data))
            else:
                updated = deepcopy(data)
            self.store.publish(self.path, updated)

    def delete(self):
        self.store.publish(self.path, _MISSING)


def _is_delete_sentinel(value):
    return str(type(value).__name__) == "Sentinel" or "DELETE" in str(value)


def _apply_updates(document, updates):
    for key, value in updates.items():
        if _is_delete_sentinel(value):
            document.pop(key, None)
        else:
            document[key] = deepcopy(value)


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
        with self.store.lock:
            stored_rows = [(path, deepcopy(data)) for path, data in self.store.items()]
        for path, data in stored_rows:
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
    def __init__(self, store, client):
        self.store = store
        self.client = client
        self._read_versions = {}
        self._read_data = {}
        self._writes = []
        self._closed = False

    def get(self, ref):
        return self._read(ref)

    def _read(self, ref):
        if ref.path not in self._read_data:
            with self.store.lock:
                self._read_data[ref.path] = deepcopy(self.store.get(ref.path))
                self._read_versions[ref.path] = self.store.versions.get(ref.path, 0)
        return _Snapshot(
            ref.path,
            self.store,
            data=self._read_data[ref.path],
            version=self._read_versions[ref.path],
        )

    def set(self, ref, data, merge=False):
        self._writes.append(("set", ref.path, deepcopy(data), merge))

    def update(self, ref, updates):
        self._writes.append(("update", ref.path, deepcopy(updates), False))

    def delete(self, ref):
        self._writes.append(("delete", ref.path, None, False))

    def rollback(self):
        self._writes.clear()
        self._closed = True

    def commit(self):
        if self._closed:
            raise AssertionError("transaction already closed")
        gate = self.client._take_commit_gate()
        try:
            if gate is not None and gate.mode == "before_lock":
                gate.wait()
            with self.store.lock:
                for path, version in self._read_versions.items():
                    if self.store.versions.get(path, 0) != version:
                        self.client.optimistic_conflicts += 1
                        raise _OptimisticVersionConflict(path)

                working = {}
                for _operation, path, _payload, _merge in self._writes:
                    if path not in working:
                        working[path] = (
                            deepcopy(self.store[path]) if path in self.store else _MISSING
                        )
                for index, (operation, path, payload, merge) in enumerate(
                    self._writes,
                    start=1,
                ):
                    if self.client.fail_on_write_number == index:
                        raise _InjectedTransactionFailure(f"write-{index}")
                    if operation == "delete":
                        working[path] = _MISSING
                    elif operation == "set":
                        if merge and isinstance(working[path], dict):
                            working[path].update(deepcopy(payload))
                        else:
                            working[path] = deepcopy(payload)
                    else:
                        current = working[path]
                        if not isinstance(current, dict):
                            current = {}
                        _apply_updates(current, payload)
                        working[path] = current

                if self.client.fail_before_publish:
                    raise _InjectedTransactionFailure("commit-before-publish")
                if gate is not None and gate.mode == "locked_before_publish":
                    gate.wait()

                before = {path: deepcopy(self.store.get(path)) for path in working}
                for path, data in working.items():
                    self.store.publish(path, data)
                after = {path: deepcopy(self.store.get(path)) for path in working}
                if working:
                    self.client.commit_observations.append((before, after))
                self._closed = True
        except Exception:
            self.rollback()
            raise
        finally:
            if gate is not None:
                gate.finished.set()


class _Client:
    def __init__(self):
        self.store = _VersionedStore()
        self.fail_after_callback = False
        self.fail_on_write_number = None
        self.fail_before_publish = False
        self.commit_observations = []
        self.optimistic_conflicts = 0
        self.transaction_retries = 0
        self._commit_gate = None
        self._gate_lock = RLock()
        self._transaction_count = 0
        self._next_transaction_watchers = []

    def collection(self, name):
        return _Query(name, self.store)

    def collection_group(self, name):
        return _Query(name, self.store, group=True)

    def transaction(self):
        with self._gate_lock:
            self._transaction_count += 1
            watchers = self._next_transaction_watchers
            self._next_transaction_watchers = []
        for watcher in watchers:
            watcher.set()
        return _Transaction(self.store, self)

    def watch_next_transaction(self):
        watcher = Event()
        with self._gate_lock:
            self._next_transaction_watchers.append(watcher)
        return watcher

    def arm_commit_gate(self, mode):
        gate = _CommitGate(mode)
        with self._gate_lock:
            if self._commit_gate is not None:
                raise AssertionError("commit gate already armed")
            self._commit_gate = gate
        return gate

    def _take_commit_gate(self):
        with self._gate_lock:
            gate = self._commit_gate
            self._commit_gate = None
            return gate

    def external_update(self, path, updates):
        _Doc(path, self.store).update(updates)


def _staged_transactional(fn):
    def _run(transaction):
        current_transaction = transaction
        for attempt in range(2):
            try:
                result = fn(current_transaction)
            except Exception:
                current_transaction.rollback()
                raise
            if current_transaction.client.fail_after_callback:
                current_transaction.rollback()
                raise _InjectedTransactionFailure("callback-before-commit")
            try:
                current_transaction.commit()
            except _OptimisticVersionConflict:
                if attempt == 1:
                    raise
                current_transaction.client.transaction_retries += 1
                current_transaction = current_transaction.client.transaction()
                continue
            return result
        raise AssertionError("bounded transaction retry loop exhausted")

    return _run


def _setup_contractor_in_store(client, contractor_id="c1", **overrides):
    import app.services.integration_token_mutations as it_mutations

    enc_acc = it_mutations.encrypt_integration_token("access-token", contractor_id=contractor_id, provider="google_calendar", token_kind="access")
    enc_ref = it_mutations.encrypt_integration_token("refresh-token", contractor_id=contractor_id, provider="google_calendar", token_kind="refresh")
    contractor = {
        "contractor_id": contractor_id,
        "active": True,
        "google_calendar_access_token": enc_acc,
        "google_calendar_refresh_token": enc_ref,
        "google_calendar_token_envelope_required": True,
        "google_calendar_scope": it_mutations.CANONICAL_GOOGLE_CALENDAR_SCOPE,
        "google_calendar_token_expires_at": 1800000000.0,
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
    # Import/patch the mutations module before mutating the shared Firestore
    # module. Otherwise its first import can permanently capture this test
    # decorator and leak it into subsequently collected test modules.
    monkeypatch.setattr(
        "app.services.integration_token_mutations.transactional",
        _staged_transactional,
    )
    monkeypatch.setattr(
        "app.db.service_requests.firestore.transactional",
        _staged_transactional,
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
async def test_worker_rejects_mismatched_contractor_identity_before_adapter_or_provider():
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    await _pending_reschedule(repo)
    factory_calls = 0

    def factory(_config):
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("mismatched contractor must not construct provider adapter")

    async def load_contractor(_contractor_id):
        return {"contractor_id": "different-contractor"}

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=load_contractor,
        adapter_factory=factory,
        clock=lambda: NOW,
        owner="worker-contractor-identity",
        lease_seconds=15,
    )

    result = await worker.run_once()

    assert result.claimed == result.deferred == 1
    assert result.finalized == 0
    assert factory_calls == 0
    document = next(
        value
        for key, value in client.store.items()
        if key.endswith("/service_requests/request-1")
    )
    assert PENDING_PROVIDER_OPERATION_FIELD in document
    assert PROVIDER_RECOVERY_LEASE_FIELD not in document


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


def _reload_document(client, path):
    return client.collection(path.split("/", 1)[0]).document(
        path.split("/", 1)[1]
    ).get().to_dict()


def _raw_token_envelope(marker):
    return {
        "schema_version": 1,
        "key_version": 7,
        "algorithm": "AES-256-GCM",
        "nonce": base64.b64encode(bytes([marker]) * 12).decode("ascii"),
        "ciphertext": base64.b64encode(bytes([marker]) * 17).decode("ascii"),
    }


def _fenced_contractor(prepared, *, claim_id="claim-atomic-proof"):
    from app.services.integration_tokens import (
        CANONICAL_GOOGLE_CALENDAR_SCOPE,
        compute_raw_credentials_fingerprint,
        encrypt_integration_token,
    )

    access = encrypt_integration_token(
        "plain-access-token",
        contractor_id="c1",
        provider="google_calendar",
        token_kind="access",
    )
    refresh = encrypt_integration_token(
        "plain-refresh-token",
        contractor_id="c1",
        provider="google_calendar",
        token_kind="refresh",
    )
    contractor = {
        "contractor_id": "c1",
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_generation": 7,
        "google_calendar_lifecycle_epoch": 11,
        "google_calendar_access_token": access,
        "google_calendar_refresh_token": refresh,
        "google_calendar_token_envelope_required": True,
        "google_calendar_scope": CANONICAL_GOOGLE_CALENDAR_SCOPE,
        "google_calendar_token_expires_at": 1800000000.0,
        "google_calendar_operation_intent_id": claim_id,
        "google_calendar_operation_intent_kind": "business",
        "google_calendar_operation_intent_phase": "provider_outcome_uncertain",
        "google_calendar_operation_intent_expires_at": 100.0,
        "google_calendar_operation_intent_acquired_at": 50.0,
        "google_calendar_operation_intent_generation": 7,
        "google_calendar_operation_intent_lifecycle_epoch": 11,
        "google_calendar_operation_intent_credentials_fingerprint": (
            compute_raw_credentials_fingerprint(access, refresh)
        ),
        "google_calendar_operation_intent_bound_operation_id": (
            prepared.logical_operation_id
        ),
        "connection_counters": {"refreshes": 4, "nested": {"kept": True}},
        "unrelated": {"nested": {"list": ["preserve", {"exact": 3}]}},
    }
    return contractor


async def _claimed_fenced_reschedule(client, *, owner="worker-atomic-proof"):
    repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_reschedule(repo)
    request_path = f"contractors/c1/service_requests/{prepared.base_request.request_id}"
    contractor_path = "contractors/c1"
    client.store[contractor_path] = _fenced_contractor(prepared)
    leases = await repo.claim_due_provider_recoveries(
        owner=owner,
        now=NOW,
        lease_seconds=15,
    )
    assert len(leases) == 1
    client.commit_observations.clear()
    return repo, prepared, leases[0], request_path, contractor_path


async def _wait_for_thread_event(event, *, timeout=2.0):
    observed = await asyncio.wait_for(
        asyncio.to_thread(event.wait, timeout),
        timeout=timeout + 0.5,
    )
    assert observed


@pytest.mark.asyncio
async def test_recovery_reschedule_remote_desired_finalizes_exactly_once(monkeypatch):
    import base64
    import app.db.firestore_client as firestore_module
    import app.services.integration_token_mutations as mutations_module
    from app.config import settings
    from app.services.integration_tokens import (
        get_provider_operation_intent_keys,
        parse_provider_operation_intent,
    )

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
                    "id": "stable-event-id",
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

    # Verify durable provider intent transitioned to provider_outcome_uncertain bound to logical_operation_id
    contractor_post = client.store["contractors/c1"]
    status, intent, _ = parse_provider_operation_intent(contractor_post, "google_calendar")
    assert status == "started" or status == "uncertain" or intent is not None
    assert intent.get("phase") in ("provider_outcome_uncertain", "provider_request_started")
    assert intent.get("bound_operation_id") == prepared.logical_operation_id

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
                    "id": "stable-event-id",
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

    # Assert durable claim has been cleared after recovery finalization
    contractor_final = client.store["contractors/c1"]
    status_final, intent_final, _ = parse_provider_operation_intent(contractor_final, "google_calendar")
    assert status_final == "absent"
    assert intent_final is None
    for key in get_provider_operation_intent_keys("google_calendar"):
        assert key not in contractor_final


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
                    "id": "stable-event-id",
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
                    "id": "stable-event-id",
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
                    "id": "stable-event-id",
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
                    "id": "stable-event-id",
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
                "id": "stable-event-id",
                "etag": '"etag-malformed-missing-end"',
                "start": {"dateTime": "2026-09-10T10:00:00Z"},
                # no "end"
            },
            "missing_end",
        ),
        # Naive (non-aware) dateTime → _parse_aware_datetime returns None.
        (
            {
                "id": "stable-event-id",
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
                    "id": "stable-event-id",
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
                    "id": "stable-event-id",
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


@pytest.mark.asyncio
async def test_recovery_reconciliation_exception_never_falls_through_to_reschedule():
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_reschedule(repo)
    provider_calls = []
    contractor = {
        "contractor_id": "c1",
        "google_calendar_operation_intent_id": "fence-retained-on-error",
    }

    class _Adapter:
        async def reconcile_reschedule(self, **_kwargs):
            raise RuntimeError("injected reconciliation failure")

        async def reschedule(self, **kwargs):
            provider_calls.append(kwargs)
            return True

    async def _load_contractor(_contractor_id):
        return contractor

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=_load_contractor,
        adapter_factory=lambda _config: _Adapter(),
        clock=lambda: NOW,
        owner="worker-reconcile-error",
        lease_seconds=15,
    )

    result = await worker.run_once()

    assert result.claimed == result.deferred == 1
    assert result.finalized == 0
    assert provider_calls == []
    assert contractor["google_calendar_operation_intent_id"] == "fence-retained-on-error"
    document = client.store[
        f"contractors/c1/service_requests/{prepared.base_request.request_id}"
    ]
    assert PENDING_PROVIDER_OPERATION_FIELD in document
    assert document[PROVIDER_RECOVERY_STATE_FIELD] == "pending"


@pytest.mark.asyncio
async def test_recovery_composed_calendar_reconciliation_exception_never_patches(
    monkeypatch,
):
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_reschedule(repo)
    contractor = {"contractor_id": "c1"}
    reschedule_calls = []

    async def _raise_reconciliation(*_args, **_kwargs):
        raise RuntimeError("injected calendar reconciliation failure")

    async def _unexpected_reschedule(*_args, **kwargs):
        reschedule_calls.append(kwargs)
        return True

    monkeypatch.setattr(
        calendar,
        "reconcile_reschedule_appointment",
        _raise_reconciliation,
    )
    monkeypatch.setattr(calendar, "reschedule_appointment", _unexpected_reschedule)

    async def _load_contractor(_contractor_id):
        return contractor

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=_load_contractor,
        adapter_factory=lambda config: GoogleCalendarRequestProvider(config),
        clock=lambda: NOW,
        owner="worker-composed-reconcile-error",
        lease_seconds=15,
    )

    result = await worker.run_once()

    assert result.claimed == result.deferred == 1
    assert result.finalized == 0
    assert reschedule_calls == []
    document = client.store[
        f"contractors/c1/service_requests/{prepared.base_request.request_id}"
    ]
    assert PENDING_PROVIDER_OPERATION_FIELD in document


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim_id", "logical_operation_id"),
    [
        ("valid-claim", "f" * 64),
        ("claim id with spaces", None),
    ],
)
async def test_recovery_rejects_foreign_or_malformed_reconciliation_identity(
    claim_id,
    logical_operation_id,
):
    client = _Client()
    durable_repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_reschedule(durable_repo)
    finalize_calls = 0
    provider_calls = []

    class _CountingRepository:
        async def claim_due_provider_recoveries(self, **kwargs):
            return await durable_repo.claim_due_provider_recoveries(**kwargs)

        async def finalize_provider_recovery(self, lease):
            nonlocal finalize_calls
            finalize_calls += 1
            return await durable_repo.finalize_provider_recovery(lease)

        async def release_provider_recovery(self, lease, **kwargs):
            return await durable_repo.release_provider_recovery(lease, **kwargs)

    class _Adapter:
        async def reconcile_reschedule(self, **_kwargs):
            return calendar.CalendarReconciliationResult(
                has_matching_claim=True,
                confirmed=True,
                claim_id=claim_id,
                logical_operation_id=(
                    logical_operation_id
                    if logical_operation_id is not None
                    else prepared.logical_operation_id
                ),
                authorization_status="matching_claim",
            )

        async def reschedule(self, **kwargs):
            provider_calls.append(kwargs)
            return True

    async def _load_contractor(contractor_id):
        return {"contractor_id": contractor_id}

    worker = ServiceRequestRecoveryWorker(
        _CountingRepository(),
        contractor_loader=_load_contractor,
        adapter_factory=lambda _config: _Adapter(),
        clock=lambda: NOW,
        owner="worker-identity-fence",
        lease_seconds=15,
    )

    result = await worker.run_once()

    assert result.claimed == result.deferred == 1
    assert result.finalized == 0
    assert finalize_calls == 0
    assert provider_calls == []


@pytest.mark.asyncio
async def test_recovery_blocked_reconciliation_cannot_fall_through_to_provider():
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    await _pending_reschedule(repo)
    provider_calls = []

    class _Adapter:
        async def reconcile_reschedule(self, **_kwargs):
            return calendar.CalendarReconciliationResult(
                has_matching_claim=False,
                confirmed=False,
                authorization_status="blocked",
            )

        async def reschedule(self, **kwargs):
            provider_calls.append(kwargs)
            return True

    async def _load_contractor(contractor_id):
        return {"contractor_id": contractor_id}

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=_load_contractor,
        adapter_factory=lambda _config: _Adapter(),
        clock=lambda: NOW,
        owner="worker-blocked-reconciliation",
        lease_seconds=15,
    )

    result = await worker.run_once()

    assert result.claimed == result.deferred == 1
    assert result.finalized == 0
    assert provider_calls == []


@pytest.mark.asyncio
async def test_recovery_missing_reconciliation_capability_cannot_fall_through():
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    await _pending_reschedule(repo)
    provider_calls = []

    class _AdapterWithoutReconciliation:
        async def reschedule(self, **kwargs):
            provider_calls.append(kwargs)
            return True

    async def _load_contractor(contractor_id):
        return {"contractor_id": contractor_id}

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=_load_contractor,
        adapter_factory=lambda _config: _AdapterWithoutReconciliation(),
        clock=lambda: NOW,
        owner="worker-missing-reconciliation",
        lease_seconds=15,
    )

    result = await worker.run_once()

    assert result.claimed == result.deferred == 1
    assert result.finalized == 0
    assert provider_calls == []


@pytest.mark.asyncio
async def test_recovery_verified_absence_allows_one_standard_provider_attempt():
    client = _Client()
    repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_reschedule(repo)
    provider_calls = []

    class _Adapter:
        async def reconcile_reschedule(self, **_kwargs):
            return calendar.CalendarReconciliationResult(
                has_matching_claim=False,
                confirmed=False,
                authorization_status="verified_absent",
            )

        async def reschedule(self, **kwargs):
            provider_calls.append(kwargs)
            return True

    async def _load_contractor(contractor_id):
        return {"contractor_id": contractor_id}

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=_load_contractor,
        adapter_factory=lambda _config: _Adapter(),
        clock=lambda: NOW,
        owner="worker-verified-absence",
        lease_seconds=15,
    )

    result = await worker.run_once()

    assert result.claimed == result.finalized == 1
    assert result.deferred == 0
    assert len(provider_calls) == 1
    assert provider_calls[0]["idempotency_key"] == prepared.logical_operation_id


@pytest.mark.asyncio
async def test_recovery_atomically_finalizes_and_clears_exact_provider_fence():
    client = _Client()
    repo, prepared, lease, request_path, contractor_path = (
        await _claimed_fenced_reschedule(client)
    )
    request_before = deepcopy(_reload_document(client, request_path))
    contractor_before = deepcopy(_reload_document(client, contractor_path))
    gate = client.arm_commit_gate("before_lock")
    finalize_task = asyncio.create_task(
        repo.finalize_reconciled_provider_recovery(
            lease,
            claim_id=contractor_before["google_calendar_operation_intent_id"],
            bound_operation_id=prepared.logical_operation_id,
        )
    )

    try:
        await _wait_for_thread_event(gate.entered)
        assert _reload_document(client, request_path) == request_before
        assert _reload_document(client, contractor_path) == contractor_before
    finally:
        gate.release.set()
    assert await finalize_task is True
    await _wait_for_thread_event(gate.finished)

    request_after = _reload_document(client, request_path)
    contractor_after = _reload_document(client, contractor_path)
    assert len(client.commit_observations) == 1
    observed_before, observed_after = client.commit_observations[0]
    assert observed_before == {
        request_path: request_before,
        contractor_path: contractor_before,
    }
    assert observed_after == {
        request_path: request_after,
        contractor_path: contractor_after,
    }
    assert ServiceRequest.from_dict(request_before["aggregate"]).revision == 1
    assert ServiceRequest.from_dict(request_after["aggregate"]).revision == 2
    assert PENDING_PROVIDER_OPERATION_FIELD not in request_after

    from app.services.integration_tokens import OPERATION_INTENT_BASE_KEYS

    expected_contractor = deepcopy(contractor_before)
    for key in OPERATION_INTENT_BASE_KEYS:
        expected_contractor.pop(f"google_calendar_{key}")
    assert contractor_after == expected_contractor
    assert contractor_after["google_calendar_access_token"] == contractor_before[
        "google_calendar_access_token"
    ]
    assert contractor_after["google_calendar_refresh_token"] == contractor_before[
        "google_calendar_refresh_token"
    ]
    assert contractor_after["google_calendar_token_envelope_required"] is True
    assert contractor_after["google_calendar_connected"] is True
    assert contractor_after["google_calendar_generation"] == 7
    assert contractor_after["google_calendar_lifecycle_epoch"] == 11
    assert contractor_after["connection_counters"] == contractor_before[
        "connection_counters"
    ]
    assert contractor_after["unrelated"] == contractor_before["unrelated"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    ["callback", "second_write", "commit_before_publish"],
)
async def test_reconciled_finalization_failures_publish_neither_document(failure_mode):
    client = _Client()
    repo, prepared, lease, request_path, contractor_path = (
        await _claimed_fenced_reschedule(client)
    )
    request_before = deepcopy(_reload_document(client, request_path))
    contractor_before = deepcopy(_reload_document(client, contractor_path))
    if failure_mode == "callback":
        client.fail_after_callback = True
    elif failure_mode == "second_write":
        client.fail_on_write_number = 2
    else:
        client.fail_before_publish = True

    with pytest.raises(_InjectedTransactionFailure):
        await repo.finalize_reconciled_provider_recovery(
            lease,
            claim_id=contractor_before["google_calendar_operation_intent_id"],
            bound_operation_id=prepared.logical_operation_id,
        )

    assert _reload_document(client, request_path) == request_before
    assert _reload_document(client, contractor_path) == contractor_before
    assert client.commit_observations == []
    assert PENDING_PROVIDER_OPERATION_FIELD in request_before
    assert "google_calendar_operation_intent_id" in contractor_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "race",
    [
        "claim_id",
        "bound_operation_id",
        "allowed_phase",
        "generation",
        "lifecycle_epoch",
        "raw_credentials_fingerprint",
        "floor_false",
        "floor_malformed",
        "reduced_scope",
        "malformed_scope",
        "malformed_expiry_bool",
        "malformed_expiry_negative",
        "malformed_expiry_nan",
        "wrong_aad_contractor",
        "tampered_ciphertext",
    ],
)
async def test_reconciled_finalization_rejects_schema_valid_post_lease_races(race):
    from app.services.integration_tokens import encrypt_integration_token

    client = _Client()
    repo, prepared, lease, request_path, contractor_path = (
        await _claimed_fenced_reschedule(client)
    )
    original_claim_id = _reload_document(client, contractor_path)[
        "google_calendar_operation_intent_id"
    ]
    cur_access = _reload_document(client, contractor_path)["google_calendar_access_token"]
    updates = {
        "claim_id": {"google_calendar_operation_intent_id": "claim-raced"},
        "bound_operation_id": {
            "google_calendar_operation_intent_bound_operation_id": "0" * 64
        },
        "allowed_phase": {
            "google_calendar_operation_intent_phase": "reserved"
        },
        "generation": {"google_calendar_generation": 8},
        "lifecycle_epoch": {"google_calendar_lifecycle_epoch": 12},
        "raw_credentials_fingerprint": {
            "google_calendar_access_token": encrypt_integration_token(
                "plain-other", contractor_id="c1", provider="google_calendar", token_kind="access"
            )
        },
        "floor_false": {"google_calendar_token_envelope_required": False},
        "floor_malformed": {"google_calendar_token_envelope_required": "true"},
        "reduced_scope": {"google_calendar_scope": "https://www.googleapis.com/auth/userinfo.email"},
        "malformed_scope": {"google_calendar_scope": 12345},
        "malformed_expiry_bool": {"google_calendar_token_expires_at": True},
        "malformed_expiry_negative": {"google_calendar_token_expires_at": -100.0},
        "malformed_expiry_nan": {"google_calendar_token_expires_at": float("nan")},
        "wrong_aad_contractor": {
            "google_calendar_access_token": encrypt_integration_token(
                "plain-other", contractor_id="c2", provider="google_calendar", token_kind="access"
            )
        },
        "tampered_ciphertext": {
            "google_calendar_access_token": {**cur_access, "ciphertext": "dGFtcGVyZWQ="}
        },
    }[race]
    client.external_update(contractor_path, updates)
    request_before = deepcopy(_reload_document(client, request_path))
    contractor_before = deepcopy(_reload_document(client, contractor_path))
    client.commit_observations.clear()

    assert (
        await repo.finalize_reconciled_provider_recovery(
            lease,
            claim_id=original_claim_id,
            bound_operation_id=prepared.logical_operation_id,
        )
        is False
    )

    request_after = _reload_document(client, request_path)
    contractor_after = _reload_document(client, contractor_path)
    assert request_after == request_before
    assert contractor_after == contractor_before
    assert client.commit_observations == []
    assert ServiceRequest.from_dict(request_after["aggregate"]) == prepared.base_request
    assert PENDING_PROVIDER_OPERATION_FIELD in request_after
    assert "google_calendar_operation_intent_id" in contractor_after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_read_time",
    [
        None,
        datetime(2026, 8, 24, 18, 0, 0),
        1700000000.0,
        True,
        "2026-08-24T18:00:00Z",
    ],
)
async def test_reconciled_finalization_rejects_invalid_contractor_read_time(
    bad_read_time, monkeypatch
):
    client = _Client()
    repo, prepared, lease, request_path, contractor_path = (
        await _claimed_fenced_reschedule(client)
    )
    original_claim_id = _reload_document(client, contractor_path)[
        "google_calendar_operation_intent_id"
    ]
    request_before = deepcopy(_reload_document(client, request_path))
    contractor_before = deepcopy(_reload_document(client, contractor_path))
    client.commit_observations.clear()

    orig_read = _Transaction._read

    def _bad_read(self, ref):
        snap = orig_read(self, ref)
        if ref.path == contractor_path:
            snap.read_time = bad_read_time
        return snap

    monkeypatch.setattr(_Transaction, "_read", _bad_read)

    assert (
        await repo.finalize_reconciled_provider_recovery(
            lease,
            claim_id=original_claim_id,
            bound_operation_id=prepared.logical_operation_id,
        )
        is False
    )

    request_after = _reload_document(client, request_path)
    contractor_after = _reload_document(client, contractor_path)
    assert request_after == request_before
    assert contractor_after == contractor_before
    assert client.commit_observations == []
    assert PENDING_PROVIDER_OPERATION_FIELD in request_after
    assert "google_calendar_operation_intent_id" in contractor_after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "forgery",
    [
        "outer_customer_key",
        "outer_kind",
        "preparation_contractor_id",
        "preparation_customer_key",
        "preparation_request_id",
    ],
)
async def test_reconciled_finalization_rejects_forged_lease_identity(forgery):
    from app.services.service_request_repository import ServiceRequestRepositoryConflict

    client = _Client()
    repo, prepared, lease, request_path, contractor_path = (
        await _claimed_fenced_reschedule(client)
    )
    if forgery == "outer_customer_key":
        forged_lease = replace(
            lease,
            customer_key=customer_key_for_phone("+16175559999"),
        )
    elif forgery == "outer_kind":
        forged_lease = replace(lease, kind="create")
    else:
        preparation_updates = {
            "preparation_contractor_id": {"contractor_id": "c2"},
            "preparation_customer_key": {
                "customer_key": customer_key_for_phone("+16175559999")
            },
            "preparation_request_id": {"request_id": "request-forged"},
        }[forgery]
        forged_lease = replace(
            lease,
            preparation=replace(prepared, **preparation_updates),
        )
    request_before = deepcopy(_reload_document(client, request_path))
    contractor_before = deepcopy(_reload_document(client, contractor_path))

    with pytest.raises(ServiceRequestRepositoryConflict):
        await repo.finalize_reconciled_provider_recovery(
            forged_lease,
            claim_id=contractor_before["google_calendar_operation_intent_id"],
            bound_operation_id=prepared.logical_operation_id,
        )

    assert _reload_document(client, request_path) == request_before
    assert _reload_document(client, contractor_path) == contractor_before
    assert client.commit_observations == []
    assert PENDING_PROVIDER_OPERATION_FIELD in request_before
    assert "google_calendar_operation_intent_id" in contractor_before


@pytest.mark.asyncio
async def test_recovery_finalize_failure_never_clears_provider_claim():
    client = _Client()
    durable_repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_reschedule(durable_repo)
    atomic_calls = 0

    class _FinalizeFalseRepository:
        async def claim_due_provider_recoveries(self, **kwargs):
            return await durable_repo.claim_due_provider_recoveries(**kwargs)

        async def finalize_provider_recovery(self, _lease):
            return False

        async def finalize_reconciled_provider_recovery(self, _lease, **_kwargs):
            nonlocal atomic_calls
            atomic_calls += 1
            return False

        async def release_provider_recovery(self, lease, **kwargs):
            return await durable_repo.release_provider_recovery(lease, **kwargs)

    class _Adapter:
        async def reconcile_reschedule(self, **_kwargs):
            return calendar.CalendarReconciliationResult(
                has_matching_claim=True,
                confirmed=True,
                claim_id="claim-retained-finalize-false",
                logical_operation_id=prepared.logical_operation_id,
                authorization_status="matching_claim",
            )

    async def _load_contractor(contractor_id):
        return {"contractor_id": contractor_id}

    worker = ServiceRequestRecoveryWorker(
        _FinalizeFalseRepository(),
        contractor_loader=_load_contractor,
        adapter_factory=lambda _config: _Adapter(),
        clock=lambda: NOW,
        owner="worker-finalize-false",
        lease_seconds=15,
    )

    result = await worker.run_once()

    assert result.claimed == result.deferred == 1
    assert result.finalized == 0
    assert atomic_calls == 1
    document = client.store[
        f"contractors/c1/service_requests/{prepared.base_request.request_id}"
    ]
    assert PENDING_PROVIDER_OPERATION_FIELD in document


async def _timeout_recovery_case(monkeypatch, *, gate_mode):
    import app.db.firestore_client as firestore_module
    import app.db.service_requests as service_requests_module
    import app.services.integration_token_mutations as mutations_module
    from app.config import settings
    from app.services.integration_tokens import compute_raw_credentials_fingerprint

    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(
        settings,
        "integration_token_encryption_keys",
        f'{{"1": "{dummy_key}"}}',
    )
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    monkeypatch.setattr(settings, "google_calendar_client_id", "client-id")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "client-secret")
    monkeypatch.setattr(service_requests_module, "IO_TIMEOUT_SECONDS", 0.25)
    calendar._REFRESH_LOCKS.clear()

    client = _Client()
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: client)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: client)
    _setup_contractor_in_store(client, "c1")
    repo = FirestoreServiceRequestRepository(client)
    prepared = await _pending_reschedule(repo)
    request_path = f"contractors/c1/service_requests/{prepared.base_request.request_id}"
    contractor_path = "contractors/c1"
    contractor = _reload_document(client, contractor_path)
    claim_id = "claim-timeout-late-commit"
    client.external_update(
        contractor_path,
        {
            "google_calendar_operation_intent_id": claim_id,
            "google_calendar_operation_intent_kind": "business",
            "google_calendar_operation_intent_phase": "provider_outcome_uncertain",
            "google_calendar_operation_intent_expires_at": 100.0,
            "google_calendar_operation_intent_acquired_at": 50.0,
            "google_calendar_operation_intent_generation": 0,
            "google_calendar_operation_intent_lifecycle_epoch": 0,
            "google_calendar_operation_intent_credentials_fingerprint": (
                compute_raw_credentials_fingerprint(
                    contractor["google_calendar_access_token"],
                    contractor["google_calendar_refresh_token"],
                )
            ),
            "google_calendar_operation_intent_bound_operation_id": (
                prepared.logical_operation_id
            ),
        },
    )

    provider_calls = []
    gate_box = []
    gate_ready = asyncio.Event()
    desired_start_iso = (NOW + timedelta(days=2)).isoformat()
    desired_end_iso = (NOW + timedelta(days=2, hours=1)).isoformat()

    class _GetOnlyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            provider_calls.append(("GET", url, kwargs))
            if not gate_box:
                gate_box.append(client.arm_commit_gate(gate_mode))
                gate_ready.set()
            return _FakeResponse(
                200,
                {
                    "id": "stable-event-id",
                    "etag": '"etag-timeout-desired"',
                    "start": {"dateTime": desired_start_iso},
                    "end": {"dateTime": desired_end_iso},
                },
            )

        async def patch(self, *_args, **_kwargs):
            provider_calls.append(("PATCH", None, None))
            raise AssertionError("reconciliation must never replay PATCH")

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _GetOnlyClient)

    async def _load_contractor(contractor_id):
        assert contractor_id == "c1"
        return _reload_document(client, contractor_path)

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=_load_contractor,
        adapter_factory=lambda config: GoogleCalendarRequestProvider(config),
        clock=lambda: NOW,
        owner=f"worker-timeout-{gate_mode}",
        lease_seconds=15,
    )
    return (
        client,
        prepared,
        request_path,
        contractor_path,
        provider_calls,
        gate_box,
        gate_ready,
        worker,
    )


@pytest.mark.asyncio
async def test_recovery_timeout_allows_single_atomic_late_commit_without_replay(
    monkeypatch,
):
    (
        client,
        prepared,
        request_path,
        contractor_path,
        provider_calls,
        gate_box,
        gate_ready,
        worker,
    ) = await _timeout_recovery_case(monkeypatch, gate_mode="locked_before_publish")
    first_task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(gate_ready.wait(), timeout=1.0)
        gate = gate_box[0]
        await _wait_for_thread_event(gate.entered)
        release_started = client.watch_next_transaction()
        await _wait_for_thread_event(release_started)
        gate.release.set()
        first = await first_task
    finally:
        if gate_box:
            gate_box[0].release.set()
            await _wait_for_thread_event(gate_box[0].finished)
        if not first_task.done():
            await first_task

    assert first.claimed == first.deferred == 1
    assert first.finalized == 0
    request_after = _reload_document(client, request_path)
    contractor_after = _reload_document(client, contractor_path)
    assert PENDING_PROVIDER_OPERATION_FIELD not in request_after
    assert ServiceRequest.from_dict(request_after["aggregate"]).revision == 2
    assert "google_calendar_operation_intent_id" not in contractor_after
    assert client.optimistic_conflicts == 0

    worker._clock = lambda: NOW + timedelta(seconds=30)
    later = await worker.run_once()
    assert later.claimed == later.finalized == later.deferred == 0
    assert [method for method, *_rest in provider_calls] == ["GET"]
    assert prepared.result.request.revision == 2


@pytest.mark.asyncio
async def test_recovery_timeout_release_wins_and_stale_atomic_commit_conflicts(
    monkeypatch,
):
    (
        client,
        prepared,
        request_path,
        contractor_path,
        provider_calls,
        gate_box,
        gate_ready,
        worker,
    ) = await _timeout_recovery_case(monkeypatch, gate_mode="before_lock")
    first_task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(gate_ready.wait(), timeout=1.0)
        gate = gate_box[0]
        await _wait_for_thread_event(gate.entered)
        first = await first_task
        request_released = _reload_document(client, request_path)
        contractor_released = _reload_document(client, contractor_path)
        assert PENDING_PROVIDER_OPERATION_FIELD in request_released
        assert "google_calendar_operation_intent_id" in contractor_released
    finally:
        if gate_box:
            gate_box[0].release.set()
            await _wait_for_thread_event(gate_box[0].finished)
        if not first_task.done():
            await first_task

    assert first.claimed == first.deferred == 1
    assert first.finalized == 0
    assert client.optimistic_conflicts == 1
    assert client.transaction_retries == 1
    assert _reload_document(client, request_path) == request_released
    assert _reload_document(client, contractor_path) == contractor_released

    worker._clock = lambda: NOW + timedelta(seconds=30)
    later = await worker.run_once()
    assert later.claimed == later.finalized == 1
    assert later.deferred == 0
    request_after = _reload_document(client, request_path)
    contractor_after = _reload_document(client, contractor_path)
    assert PENDING_PROVIDER_OPERATION_FIELD not in request_after
    assert ServiceRequest.from_dict(request_after["aggregate"]).revision == 2
    assert "google_calendar_operation_intent_id" not in contractor_after
    assert [method for method, *_rest in provider_calls] == ["GET", "GET"]
    assert prepared.result.request.revision == 2


@pytest.mark.asyncio
async def test_recovery_transient_atomic_finalize_failure_retries_without_patch(
    monkeypatch,
):
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
    desired_start_iso = (NOW + timedelta(days=2)).isoformat()
    desired_end_iso = (NOW + timedelta(days=2, hours=1)).isoformat()

    # Create durable uncertain claim bound to logical_operation_id
    contractor = client.store["contractors/c1"]
    contractor["google_calendar_operation_intent_id"] = "claim-ord-1"
    contractor["google_calendar_operation_intent_kind"] = "business"
    contractor["google_calendar_operation_intent_phase"] = "provider_outcome_uncertain"
    contractor["google_calendar_operation_intent_expires_at"] = 100.0
    contractor["google_calendar_operation_intent_acquired_at"] = 50.0
    contractor["google_calendar_operation_intent_generation"] = 0
    contractor["google_calendar_operation_intent_lifecycle_epoch"] = 0
    contractor["google_calendar_operation_intent_bound_operation_id"] = prepared.logical_operation_id
    contractor["google_calendar_operation_intent_credentials_fingerprint"] = mutations_module.compute_raw_credentials_fingerprint(
        contractor["google_calendar_access_token"],
        contractor["google_calendar_refresh_token"],
    )

    reconcile_calls = []

    class _ReconcileClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            reconcile_calls.append(("GET", url))
            return _FakeResponse(
                200,
                {
                    "id": "stable-event-id",
                    "etag": '"etag-desired-ord"',
                    "start": {"dateTime": desired_start_iso},
                    "end": {"dateTime": desired_end_iso},
                },
            )

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _ReconcileClient)

    class _FailAtomicOnce:
        def __init__(self):
            self.failed = False
            self.atomic_calls = 0

        async def claim_due_provider_recoveries(self, **kwargs):
            return await repo.claim_due_provider_recoveries(**kwargs)

        async def finalize_provider_recovery(self, lease):
            return await repo.finalize_provider_recovery(lease)

        async def finalize_reconciled_provider_recovery(self, lease, **kwargs):
            self.atomic_calls += 1
            if not self.failed:
                self.failed = True
                raise RuntimeError("injected atomic finalize failure")
            return await repo.finalize_reconciled_provider_recovery(lease, **kwargs)

        async def release_provider_recovery(self, lease, **kwargs):
            return await repo.release_provider_recovery(lease, **kwargs)

    async def _load_contractor(contractor_id):
        return client.store.get(f"contractors/{contractor_id}")

    repository = _FailAtomicOnce()
    worker = ServiceRequestRecoveryWorker(
        repository,
        contractor_loader=_load_contractor,
        adapter_factory=lambda cfg: GoogleCalendarRequestProvider(cfg),
        clock=lambda: NOW,
        owner="worker-ord",
        lease_seconds=15,
    )

    first = await worker.run_once()
    assert first.claimed == first.deferred == 1
    assert first.finalized == 0
    doc = client.store[f"contractors/c1/service_requests/{prepared.base_request.request_id}"]
    assert PENDING_PROVIDER_OPERATION_FIELD in doc
    assert contractor.get("google_calendar_operation_intent_id") == "claim-ord-1"

    worker._clock = lambda: NOW + timedelta(seconds=30)
    second = await worker.run_once()
    assert second.claimed == second.finalized == 1
    assert second.deferred == 0
    assert repository.atomic_calls == 2
    assert [method for method, _url in reconcile_calls] == ["GET", "GET"]

    doc = client.store[f"contractors/c1/service_requests/{prepared.base_request.request_id}"]
    assert doc["status"] == "open"
    assert PENDING_PROVIDER_OPERATION_FIELD not in doc
    agg = ServiceRequest.from_dict(doc["aggregate"])
    assert agg.revision == 2
    assert "google_calendar_operation_intent_id" not in contractor


@pytest.mark.asyncio
async def test_recovery_reschedule_needs_review_retains_provider_fence(monkeypatch):
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

    # Create durable uncertain claim
    contractor = client.store["contractors/c1"]
    contractor["google_calendar_operation_intent_id"] = "claim-nr-1"
    contractor["google_calendar_operation_intent_kind"] = "business"
    contractor["google_calendar_operation_intent_phase"] = "provider_outcome_uncertain"
    contractor["google_calendar_operation_intent_expires_at"] = 100.0
    contractor["google_calendar_operation_intent_acquired_at"] = 50.0
    contractor["google_calendar_operation_intent_generation"] = 0
    contractor["google_calendar_operation_intent_lifecycle_epoch"] = 0
    contractor["google_calendar_operation_intent_bound_operation_id"] = prepared.logical_operation_id
    contractor["google_calendar_operation_intent_credentials_fingerprint"] = mutations_module.compute_raw_credentials_fingerprint(
        contractor["google_calendar_access_token"],
        contractor["google_calendar_refresh_token"],
    )

    class _BaseScheduleClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            return _FakeResponse(
                200,
                {
                    "id": "stable-event-id",
                    "etag": '"etag-base-nr"',
                    "start": {"dateTime": base_start_iso},
                    "end": {"dateTime": base_end_iso},
                },
            )

    monkeypatch.setattr(calendar.httpx, "AsyncClient", _BaseScheduleClient)

    async def _load_contractor(contractor_id):
        return client.store.get(f"contractors/{contractor_id}")

    worker = ServiceRequestRecoveryWorker(
        repo,
        contractor_loader=_load_contractor,
        adapter_factory=lambda cfg: GoogleCalendarRequestProvider(cfg),
        clock=lambda: NOW,
        owner="worker-nr",
        lease_seconds=15,
        max_attempts=1,  # Exhaust attempts in one run
    )

    result = await worker.run_once()
    assert result.claimed == 1
    assert result.finalized == 0
    assert result.deferred == 1

    # Request reaches needs_review
    doc = client.store[f"contractors/c1/service_requests/{prepared.base_request.request_id}"]
    assert doc[PROVIDER_RECOVERY_STATE_FIELD] == PROVIDER_RECOVERY_NEEDS_REVIEW

    # Provider fence is STILL retained in contractor doc
    contractor_nr = client.store["contractors/c1"]
    assert contractor_nr.get("google_calendar_operation_intent_id") == "claim-nr-1"
    assert contractor_nr.get("google_calendar_operation_intent_phase") == "provider_outcome_uncertain"
