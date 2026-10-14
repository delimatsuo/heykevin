"""Housekeeping races and batch bounds without datastore/provider access."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest

from app.services import background_maintenance as maintenance


def test_orphan_transaction_preserves_concurrently_refreshed_call():
    old = {"state_updated_at": 1000, "status": "screening"}
    fresh = {"state_updated_at": 9999, "status": "screening"}
    assert maintenance._delete_if_stale(old, 10000) is None
    assert maintenance._delete_if_stale(fresh, 10000) is fresh
    assert maintenance._delete_if_stale(None, 10000) is None
    assert maintenance._delete_if_stale({"state_updated_at": 0}, 10000) == {"state_updated_at": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("counts,expected", [([500]*4, 4), ([500, 2], 2), ([0], 1)])
async def test_retention_preserves_four_batch_cap_and_short_batch_stop(monkeypatch, counts, expected):
    from app.db import calls
    cleanup = AsyncMock(side_effect=counts)
    monkeypatch.setattr(calls, "cleanup_old_calls", cleanup)
    await maintenance.call_retention_once()
    assert cleanup.await_count == expected


@pytest.mark.asyncio
async def test_external_retention_propagates_datastore_uncertainty(monkeypatch):
    """A private worker must hold its ledger instead of treating this as zero rows."""
    from app.db import calls
    cleanup = AsyncMock(side_effect=RuntimeError("fictional firestore outage"))
    monkeypatch.setattr(calls, "cleanup_old_calls", cleanup)

    with pytest.raises(RuntimeError, match="fictional firestore outage"):
        await maintenance.call_retention_once(strict=True)

    cleanup.assert_awaited_once_with(strict=True)


@pytest.mark.asyncio
async def test_strict_call_cleanup_reraises_a_datastore_error(monkeypatch):
    from app.db import calls

    def unavailable():
        raise RuntimeError("fictional firestore outage")

    monkeypatch.setattr(calls, "get_firestore_client", unavailable)

    with pytest.raises(RuntimeError, match="fictional firestore outage"):
        await calls.cleanup_old_calls(strict=True)
    assert await calls.cleanup_old_calls() == 0


@pytest.mark.asyncio
async def test_trial_transaction_does_not_clobber_paid_activation(monkeypatch):
    from google.cloud import firestore
    from app.db import firestore_client
    from app.services import subscription
    selected = {"subscription_status": "trial", "trial_start": 1}
    activated = {"subscription_status": "active", "trial_start": 1}
    updates = []
    ref = SimpleNamespace(get=lambda **kwargs: SimpleNamespace(exists=True, to_dict=lambda: activated))
    collection = SimpleNamespace(document=lambda _: ref)
    collection.where = lambda *args: SimpleNamespace(stream=lambda: [SimpleNamespace(id="c", to_dict=lambda: selected)])
    db = SimpleNamespace(collection=lambda _: collection, transaction=lambda: SimpleNamespace(
        update=lambda *args: updates.append(args)))
    monkeypatch.setattr(firestore_client, "get_firestore_client", lambda: db)
    monkeypatch.setattr(firestore, "transactional", lambda function: function)
    monkeypatch.setattr(subscription, "should_expire_trial", lambda data, now: data["subscription_status"] == "trial")
    await maintenance.lapsed_trials_once(strict=True)
    assert updates == []


@pytest.mark.asyncio
async def test_external_orphan_cleanup_uses_a_key_cursor_and_never_reads_more_than_one_page(monkeypatch):
    from app.db import cache
    from firebase_admin import db as rtdb

    rows = {key: {"state_updated_at": 9999} for key in ("a", "b", "c", "d", "e")}
    query_limits = []

    class Query:
        def __init__(self):
            self.cursor = None
            self.limit = None
        def order_by_key(self): return self
        def start_at(self, value): self.cursor = value; return self
        def limit_to_first(self, value): self.limit = value; query_limits.append(value); return self
        def get(self):
            keys = [key for key in sorted(rows) if self.cursor is None or key >= self.cursor]
            return {key: rows[key] for key in keys[:self.limit]}

    class Ref:
        def order_by_key(self): return Query()
        def get(self): return dict(rows)
        def transaction(self, callback): return callback(None)

    monkeypatch.setattr(cache, "_init_firebase", lambda: None)
    monkeypatch.setattr(rtdb, "reference", lambda _path: Ref())

    first = await maintenance.orphan_calls_once(continuation={}, page_size=2)
    second = await maintenance.orphan_calls_once(continuation=first, page_size=2)

    assert first == {"cursor": "b"}
    assert second == {"cursor": "d"}
    assert query_limits == [3, 3]


@pytest.mark.asyncio
async def test_external_lapsed_trial_sweep_pages_by_document_id(monkeypatch):
    from app.db import firestore_client
    from app.services import subscription

    records = {key: {"subscription_status": "trial", "trial_start": 9999} for key in ("a", "b", "c", "d")}
    query_limits = []

    class Snap:
        def __init__(self, doc_id): self.id = doc_id
        def to_dict(self): return dict(records[self.id])

    class Query:
        def __init__(self): self.cursor, self.count = "", None
        def where(self, *args): return self
        def order_by(self, *args): return self
        def start_after(self, value): self.cursor = value["__name__"]; return self
        def limit(self, count): self.count = count; query_limits.append(count); return self
        def stream(self):
            ids = [key for key in sorted(records) if key > self.cursor]
            return [Snap(key) for key in ids[:self.count]]

    class Collection:
        def where(self, *args): return Query()
        def document(self, doc_id): return doc_id

    monkeypatch.setattr(firestore_client, "get_firestore_client", lambda: SimpleNamespace(collection=lambda _: Collection()))
    monkeypatch.setattr(subscription, "should_expire_trial", lambda data, now: False)

    first = await maintenance.lapsed_trials_once(strict=True, continuation={}, page_size=2)
    second = await maintenance.lapsed_trials_once(strict=True, continuation=first, page_size=2)

    assert first == {"cursor": "b"}
    assert second == {"cursor": "d"}
    assert query_limits == [2, 2]
