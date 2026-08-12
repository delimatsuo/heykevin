import os
from datetime import UTC, datetime, timedelta

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.db.customer_memory import FirestoreCustomerMemoryRepository
from app.services.customer_memory import (
    CustomerMemoryConflict,
    IdentitySource,
    IdentityState,
)

NOW = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)


class _Snapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _Doc:
    def __init__(self, path, store):
        self.path = path
        self._store = store

    def collection(self, name):
        return _Collection(f"{self.path}/{name}", self._store)

    def get(self, transaction=None):
        return _Snapshot(self._store.get(self.path))

    def delete(self):
        self._store.pop(self.path, None)


class _Collection:
    def __init__(self, path, store):
        self.path = path
        self._store = store

    def document(self, doc_id):
        return _Doc(f"{self.path}/{doc_id}", self._store)


class _Transaction:
    def __init__(self, store):
        self._store = store

    def set(self, ref, data):
        self._store[ref.path] = data

    def delete(self, ref):
        self._store.pop(ref.path, None)


class _Client:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return _Collection(name, self.store)

    def transaction(self):
        return _Transaction(self.store)


@pytest.mark.asyncio
async def test_firestore_adapter_uses_only_tenant_subcollection(monkeypatch):
    monkeypatch.setattr(
        "app.db.customer_memory.firestore.transactional",
        lambda fn: lambda transaction: fn(transaction),
    )
    client = _Client()
    repo = FirestoreCustomerMemoryRepository(client)

    saved = await repo.remember(
        "contractor-1",
        "+16175550123",
        display_name="Jonathan Smith",
        identity_state=IdentityState.CONFIRMED,
        identity_source=IdentitySource.CALLER_CONFIRMED,
        confidence=0.95,
        language="en",
        expected_revision=0,
        command_id="call-1:name",
        occurred_at=NOW,
    )

    memory_path = f"contractors/contractor-1/customer_memory/{saved.customer_key}"
    assert memory_path in client.store
    assert all(path.startswith(f"{memory_path}/") or path == memory_path for path in client.store)
    assert await repo.lookup("contractor-1", "+16175550123", NOW) == saved
    assert await repo.lookup("contractor-2", "+16175550123", NOW) is None

    assert (
        await repo.forget(
            "contractor-1",
            "+16175550123",
            expected_revision=saved.revision,
            command_id="forget-1",
        )
        is True
    )
    assert memory_path not in client.store
    assert all(path.startswith(f"{memory_path}/command_receipts/") for path in client.store)
    assert all(
        "display_name" not in data and "language" not in data for data in client.store.values()
    )


@pytest.mark.asyncio
async def test_firestore_receipts_make_old_remember_and_forget_retries_safe(monkeypatch):
    monkeypatch.setattr(
        "app.db.customer_memory.firestore.transactional",
        lambda fn: lambda transaction: fn(transaction),
    )
    client = _Client()
    repo = FirestoreCustomerMemoryRepository(client)
    first = await repo.remember(
        "contractor-1",
        "+16175550123",
        display_name="Jonathan Smith",
        identity_state=IdentityState.CONFIRMED,
        identity_source=IdentitySource.CALLER_CONFIRMED,
        confidence=0.95,
        language="en",
        expected_revision=0,
        command_id="call-1:name",
        occurred_at=NOW,
    )
    current = await repo.remember(
        "contractor-1",
        "+16175550123",
        display_name="Jon Smith",
        identity_state=IdentityState.CONFIRMED,
        identity_source=IdentitySource.CALLER_CONFIRMED,
        confidence=0.98,
        language="en",
        expected_revision=first.revision,
        command_id="call-2:name",
        occurred_at=NOW + timedelta(days=1),
    )

    old_retry = await repo.remember(
        "contractor-1",
        "+16175550123",
        display_name="Jonathan Smith",
        identity_state=IdentityState.CONFIRMED,
        identity_source=IdentitySource.CALLER_CONFIRMED,
        confidence=0.95,
        language="en",
        expected_revision=0,
        command_id="call-1:name",
        occurred_at=NOW + timedelta(days=2),
    )
    assert old_retry == current

    with pytest.raises(CustomerMemoryConflict):
        await repo.remember(
            "contractor-1",
            "+16175550123",
            display_name="Different Person",
            identity_state=IdentityState.CONFIRMED,
            identity_source=IdentitySource.CALLER_CONFIRMED,
            confidence=0.95,
            language="en",
            expected_revision=0,
            command_id="call-1:name",
            occurred_at=NOW + timedelta(days=2),
        )

    assert (
        await repo.forget(
            "contractor-1",
            "+16175550123",
            expected_revision=current.revision,
            command_id="forget-1",
        )
        is True
    )
    assert (
        await repo.forget(
            "contractor-1",
            "+16175550123",
            expected_revision=current.revision,
            command_id="forget-1",
        )
        is True
    )

    receipts = [data for path, data in client.store.items() if "/command_receipts/" in path]
    assert len(receipts) == 3
    assert all(
        not ({"display_name", "language", "customer_key", "contractor_id"} & data.keys())
        for data in receipts
    )
    assert all(isinstance(data["expires_at"], datetime) for data in receipts)
