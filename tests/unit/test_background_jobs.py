"""Offline transactional ledger tests, including independently instantiated stores."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.db.background_jobs import (
    BackgroundJobStore,
    BackgroundWorkerCutoverStore,
    BoundedTransaction,
    COLLECTION,
    CUTOVER_COLLECTION,
    CUTOVER_DOCUMENT,
    JOB_INTERVALS,
)


class Transaction:
    _max_attempts = 1
    _read_only = False
    _id = None

    def __init__(self, db):
        self.db = db
        self.pending = []
        self.locked = False

    def _clean_up(self):
        self.pending = []
        self._id = None

    def _begin(self, retry_id=None):
        self.db.lock.acquire()
        self.locked = True
        self._id = b"test"

    def _commit(self):
        for ref, data, replace in self.pending:
            if replace:
                self.db.data[ref.key] = deepcopy(data)
            else:
                self.db.data[ref.key].update(deepcopy(data))
        self._rollback()

    def _rollback(self):
        if self.locked:
            self.db.lock.release()
            self.locked = False
        self._clean_up()

    def set(self, ref, data):
        self.pending.append((ref, data, True))

    def update(self, ref, data):
        self.pending.append((ref, data, False))


class Reference:
    def __init__(self, db, collection, key):
        self.db = db
        self.key = key if collection == COLLECTION else (collection, key)

    def get(self, *, transaction=None, retry, timeout):
        assert retry is None and timeout == 2.0
        data = deepcopy(self.db.data.get(self.key))
        return SimpleNamespace(exists=data is not None, to_dict=lambda: data,
            read_time=datetime.fromtimestamp(self.db.now, timezone.utc))


class Database:
    def __init__(self):
        self.data, self.lock, self.now = {}, RLock(), 1000.0

    def collection(self, name):
        return SimpleNamespace(document=lambda key: Reference(self, name, key))

    def get_all(self, refs, **kwargs):
        return (ref.get(**kwargs) for ref in refs)

    def store(self, *, external_generation=None):
        return BackgroundJobStore(
            self,
            transaction_factory=lambda: Transaction(self),
            external_generation=external_generation,
        )

    def cutover(self):
        return BackgroundWorkerCutoverStore(self, transaction_factory=lambda: Transaction(self))


def test_competing_independent_stores_execute_only_one_admission():
    db = Database()
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: db.store().admit("post_calls"), range(8)))
    assert sum(c.status == "admitted" for c in claims) == 1
    assert sum(c.status == "in_progress" for c in claims) == 7
    assert set(db.data) == {"post_calls"}


def test_completed_due_and_nonce_fencing_survive_instance_restart():
    db = Database()
    store = db.store()
    first = store.admit("post_calls")
    assert store.finish("post_calls", first.nonce, completed=True, continuation={"pending": "b"})
    assert db.store().admit("post_calls").status == "not_due"
    db.now += 60
    second = db.store().admit("post_calls")
    assert second.continuation == {"pending": "b"}
    assert second.nonce != first.nonce
    assert not store.finish("post_calls", first.nonce, completed=True)
    assert db.data["post_calls"]["nonce"] == second.nonce
    assert store.finish("post_calls", second.nonce, completed=False)
    db.now += 10**9
    assert db.store().admit("post_calls").status == "needs_attention"


@pytest.mark.parametrize("status", ["running", "needs_attention", "corrupt"])
def test_no_automatic_uncertain_reclamation(status):
    db = Database()
    db.data["estimates"] = {"status": status, "started_at": -10**9, "nonce": "old"}
    before = deepcopy(db.data)
    assert db.store().admit("estimates").status == "needs_attention"
    assert db.data == before


@pytest.mark.parametrize("job,interval", list(JOB_INTERVALS.items()))
def test_original_job_cadence_and_constant_document_cardinality(job, interval):
    db = Database()
    claim = db.store().admit(job)
    db.now += 5
    assert db.store().finish(job, claim.nonce, completed=True)
    assert db.data[job]["next_due_at"] == (1000 // interval + 1) * interval
    with pytest.raises(ValueError):
        db.store().admit("caller_supplied")
    assert set(db.data) == {job}


def test_claim_write_failure_never_returns_admitted(monkeypatch):
    db = Database()
    monkeypatch.setattr(Transaction, "_commit", Mock(side_effect=RuntimeError("offline")))
    with pytest.raises(RuntimeError):
        db.store().admit("post_calls")
    assert not db.data


def test_empty_server_response_cannot_admit_from_local_time(monkeypatch):
    db = Database()
    monkeypatch.setattr(db, "get_all", lambda *args, **kwargs: iter(()))
    with pytest.raises(ValueError, match="authoritative"):
        db.store().admit("post_calls")
    assert not db.data


def test_empty_completion_read_does_not_clear_running_claim(monkeypatch):
    db = Database()
    claim = db.store().admit("post_calls")
    before = deepcopy(db.data)
    monkeypatch.setattr(db, "get_all", lambda *args, **kwargs: iter(()))
    with pytest.raises(ValueError, match="authoritative"):
        db.store().finish("post_calls", claim.nonce, completed=True)
    assert db.data == before


def test_real_sdk_empty_batch_response_is_rejected():
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore

    db = firestore.Client(project="fictional-worker", credentials=AnonymousCredentials())
    db._firestore_api_internal = SimpleNamespace(batch_get_documents=Mock(return_value=iter(())))
    store = BackgroundJobStore(db)
    with pytest.raises(ValueError, match="authoritative"):
        store._snapshot(store._ref("post_calls"), None)


def test_bounded_transaction_limits_all_sdk_rpcs():
    api = SimpleNamespace(begin_transaction=Mock(return_value=SimpleNamespace(transaction=b"x")),
        commit=Mock(return_value=SimpleNamespace(write_results=[], commit_time=None)), rollback=Mock())
    client = SimpleNamespace(_firestore_api=api, _database_string="projects/fictional/databases/(default)",
                             _rpc_metadata=[])
    transaction = BoundedTransaction(client, max_attempts=1)
    transaction._begin()
    transaction._commit()
    transaction._begin()
    transaction._rollback()
    for call in [*api.begin_transaction.call_args_list, *api.commit.call_args_list, *api.rollback.call_args_list]:
        assert call.kwargs["retry"] is None
        assert call.kwargs["timeout"] == 2.0


def test_calendar_slots_preserve_cron_cadence_after_execution_latency():
    db = Database()
    db.now = 1200.8
    claim = db.store().admit("orphan_calls")
    assert db.data["orphan_calls"]["next_due_at"] == 1500
    db.now += 3.5
    assert db.store().finish("orphan_calls", claim.nonce, completed=True)
    assert db.store().admit("orphan_calls").status == "not_due"
    db.now = 1500.2
    assert db.store().admit("orphan_calls").status == "admitted"


_GENERATION = "staging-cutover-20261013"
_CUTOVER_KEY = (CUTOVER_COLLECTION, CUTOVER_DOCUMENT)


def test_external_claims_wait_for_api_ack_and_drain_before_embedded_restarts():
    db = Database()
    cutover = db.cutover()
    external = db.store(external_generation=_GENERATION)

    assert cutover.begin_embedded()
    cutover.prepare_external(
        _GENERATION,
        lease_seconds=300,
        embedded_instances_drained=True,
        scheduler_delivery_verified=True,
    )
    assert external.admit("post_calls").status == "cutover_pending"
    assert "post_calls" not in db.data
    assert cutover.activate_external(_GENERATION)

    claim = external.admit("post_calls")
    assert claim.status == "admitted"
    assert not cutover.begin_embedded()
    assert db.data[_CUTOVER_KEY]["state"] == "external_draining"
    assert external.admit("estimates").status == "cutover_blocked"

    assert external.finish("post_calls", claim.nonce, completed=True)
    assert cutover.begin_embedded()
    assert db.data[_CUTOVER_KEY] == {"state": "embedded", "protocol_version": 1}
    assert external.admit("estimates").status == "cutover_blocked"


def test_expired_external_lease_never_admits_work_and_reverts_to_embedded():
    db = Database()
    cutover = db.cutover()
    external = db.store(external_generation=_GENERATION)
    assert cutover.begin_embedded()
    cutover.prepare_external(
        _GENERATION,
        lease_seconds=60,
        embedded_instances_drained=True,
        scheduler_delivery_verified=True,
    )
    assert cutover.activate_external(_GENERATION)
    db.now += 60

    assert not cutover.external_active(_GENERATION)
    assert external.admit("orphan_calls").status == "cutover_blocked"
    assert not cutover.begin_embedded()
    assert db.data[_CUTOVER_KEY]["state"] == "external_draining"
    assert cutover.begin_embedded()
    assert db.data[_CUTOVER_KEY] == {"state": "embedded", "protocol_version": 1}


def test_external_cutover_requires_a_verified_embedded_baseline():
    db = Database()
    cutover = db.cutover()

    with pytest.raises(ValueError, match="verified embedded baseline"):
        cutover.prepare_external(
            _GENERATION,
            lease_seconds=300,
            embedded_instances_drained=True,
            scheduler_delivery_verified=True,
        )

    assert cutover.begin_embedded()
    cutover.prepare_external(
        _GENERATION,
        lease_seconds=300,
        embedded_instances_drained=True,
        scheduler_delivery_verified=True,
    )
