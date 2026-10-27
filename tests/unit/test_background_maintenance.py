"""Housekeeping races and batch bounds without datastore/provider access."""

import copy
import hashlib
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest

from app.services import background_maintenance as maintenance


def test_orphan_staleness_needs_a_timestamp_over_two_hours_old():
    assert maintenance._is_stale({"state_updated_at": 1000, "status": "screening"}, 10000)
    assert not maintenance._is_stale({"state_updated_at": 9999, "status": "screening"}, 10000)
    assert not maintenance._is_stale({"state_updated_at": 2800}, 10000)  # exactly 7200 s
    assert not maintenance._is_stale(None, 10000)
    assert not maintenance._is_stale({"state_updated_at": 0}, 10000)
    assert not maintenance._is_stale({"state_updated_at": "1000"}, 10000)
    assert not maintenance._is_stale({"state_updated_at": True}, 10000)


class _FakeRtdb:
    """RTDB's REST surface under firebase-admin's real Reference.

    ETags change on every write and a PUT or DELETE carrying a stale if-match
    fails with 412. As on the real server, one without if-match is applied
    unconditionally (requests drops a None header), so the race tests catch an
    unconditional delete.
    """

    def __init__(self, records):
        self.records = copy.deepcopy(records)
        self.record_reads = []
        self.send_etag = True
        # e.g. a live call refreshing its record mid-cleanup
        self.before_read = None
        self.before_write = None

    @staticmethod
    def _key(url):
        return url.removeprefix("/active_calls").removesuffix(".json").strip("/")

    def _etag(self, key):
        value = self.records if not key else self.records.get(key)
        return '"' + hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest() + '"'

    def _precondition(self, key, headers):
        from firebase_admin.exceptions import FailedPreconditionError
        if self.before_write:
            hook, self.before_write = self.before_write, None
            hook(self.records)
        expected = (headers or {}).get("if-match")
        if expected is not None and expected != self._etag(key):
            response = SimpleNamespace(headers={"ETag": self._etag(key)}, json=lambda: self.records.get(key))
            raise FailedPreconditionError("412 Precondition Failed", http_response=response)

    def body(self, method, url, **kwargs):  # list reads (plain and ordered-by-key pages)
        assert method == "get"
        key = self._key(url)
        return copy.deepcopy(self.records if not key else self.records.get(key))

    def headers_and_body(self, method, url, headers=None, **kwargs):  # get(etag=True)
        assert method == "get" and headers == {"X-Firebase-ETag": "true"}
        if self.before_read:
            hook, self.before_read = self.before_read, None
            hook(self.records)
        key = self._key(url)
        self.record_reads.append(key)
        response_headers = {"ETag": self._etag(key)} if self.send_etag else {}
        return response_headers, copy.deepcopy(self.records.get(key))

    def headers(self, method, url, json=None, headers=None, **kwargs):  # set_if_unchanged (transactions)
        assert method == "put"
        key = self._key(url)
        self._precondition(key, headers)
        self.records[key] = json
        return {"ETag": self._etag(key)}

    def request(self, method, url, headers=None, **kwargs):
        assert method == "delete", method
        key = self._key(url)
        self._precondition(key, headers)
        self.records.pop(key, None)


def _use_fake_rtdb(monkeypatch, records):
    from firebase_admin import db as rtdb

    from app.db import cache

    fake = _FakeRtdb(records)
    monkeypatch.setattr(cache, "_init_firebase", lambda: None)
    monkeypatch.setattr(rtdb, "reference", lambda path: rtdb.Reference(path=path, client=fake))
    return fake


def _calls(now):
    return {
        "CA_stale": {"state_updated_at": now - 3 * 3600, "status": "screening"},
        "CA_stale_too": {"state_updated_at": now - 26 * 3600, "status": "ringing"},
        "CA_live": {"state_updated_at": now - 60, "status": "screening"},
        "CA_untimed": {"status": "screening"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [None, 25])
async def test_orphan_cleanup_removes_stale_calls_and_keeps_live_ones(monkeypatch, page_size):
    fake = _use_fake_rtdb(monkeypatch, _calls(time.time()))

    if page_size is None:
        await maintenance.orphan_calls_once()
    else:
        await maintenance.orphan_calls_once(continuation={}, page_size=page_size)

    assert sorted(fake.records) == ["CA_live", "CA_untimed"]
    # Only stale candidates cost a second read; live calls are judged from the list read.
    assert sorted(fake.record_reads) == ["CA_stale", "CA_stale_too"]


@pytest.mark.asyncio
async def test_orphan_cleanup_keeps_a_call_refreshed_between_read_and_delete(monkeypatch):
    now = time.time()
    fake = _use_fake_rtdb(monkeypatch, _calls(now))
    fake.before_write = lambda records: records["CA_stale"].update(state_updated_at=now)

    await maintenance.orphan_calls_once()

    assert fake.records["CA_stale"]["state_updated_at"] == now
    assert "CA_stale_too" not in fake.records, "a 412 on one call must not stop the pass"


@pytest.mark.asyncio
async def test_orphan_cleanup_never_deletes_without_an_etag(monkeypatch):
    fake = _use_fake_rtdb(monkeypatch, _calls(time.time()))
    fake.send_etag = False

    await maintenance.orphan_calls_once()

    assert sorted(fake.records) == sorted(_calls(0))


def test_conditional_delete_through_the_real_firebase_client():
    """Pins the firebase-admin internals the cleanup relies on: the if-match header reaches
    the HTTP request, and RTDB's 412 surfaces as FailedPreconditionError."""
    import requests
    from firebase_admin import db as rtdb
    from requests.adapters import BaseAdapter

    now = time.time()
    record = {"state_updated_at": now - 3 * 3600, "status": "screening"}
    sent = []

    class Rtdb(BaseAdapter):
        def __init__(self, delete_status):
            super().__init__()
            self.delete_status = delete_status

        def send(self, request, **kwargs):
            sent.append((request.method, request.headers.get("if-match")))
            response = requests.Response()
            response.status_code = 200 if request.method == "GET" else self.delete_status
            body = None if request.method == "DELETE" and self.delete_status == 200 else record
            response._content = json.dumps(body).encode()
            response.headers.update({"ETag": '"v1"', "Content-Type": "application/json"})
            response.request, response.url = request, request.url
            return response

        def close(self):
            pass

    for delete_status, deleted in ((200, True), (412, False)):
        client = rtdb._Client(None, "https://kevin-test.firebaseio.com", 5)
        client.session.mount("https://", Rtdb(delete_status))
        ref = rtdb.Reference(path="/active_calls/CA_stale", client=client)
        assert maintenance._delete_if_still_stale(ref, now) is deleted
    assert sent == [("GET", None), ("DELETE", '"v1"')] * 2


@pytest.mark.asyncio
async def test_orphan_cleanup_rechecks_a_call_refreshed_after_the_list_read(monkeypatch):
    now = time.time()
    fake = _use_fake_rtdb(monkeypatch, _calls(now))
    fake.before_read = lambda records: records["CA_stale"].update(state_updated_at=now)

    await maintenance.orphan_calls_once()

    assert fake.records["CA_stale"]["state_updated_at"] == now


@pytest.mark.asyncio
async def test_orphan_cleanup_tolerates_a_call_that_ended_mid_pass(monkeypatch):
    fake = _use_fake_rtdb(monkeypatch, _calls(time.time()))
    fake.before_write = lambda records: records.pop("CA_stale")

    await maintenance.orphan_calls_once()

    assert "CA_stale" not in fake.records


def test_fake_rtdb_refuses_the_old_delete_by_transaction(monkeypatch):
    """firebase-admin's transaction() cannot delete; the fake must reproduce that."""
    from firebase_admin import db as rtdb
    fake = _FakeRtdb(_calls(time.time()))
    ref = rtdb.Reference(path="/active_calls/CA_stale", client=fake)
    with pytest.raises(ValueError, match="none"):
        ref.transaction(lambda current: None)


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
        def get(self, etag=False): return (None, '"gone"') if etag else dict(rows)

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
