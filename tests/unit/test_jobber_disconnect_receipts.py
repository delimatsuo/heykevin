"""Offline receipt atomicity, hostile metadata and real SDK transaction transport."""
from tests.unit.jobber_disconnect_test_support import seed_required_environment
seed_required_environment()

import datetime as dt
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.api_core.exceptions import Aborted
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client, DELETE_FIELD
from google.cloud.firestore_v1.transaction import transactional

from tests.unit.jobber_disconnect_test_support import (
    jobber_settings, active_db, admit, clone, FakeFirestoreDb, FakeDocumentSnapshot,
)
from app.db.jobber_disconnect_receipts import (
    JobberDisconnectError, MAX_RECEIPT_COUNTER, compute_account_barrier_key,
    compute_work_doc_id, extract_snapshot_time, record_disconnect_admission_atomic,
    validate_control_record, validate_work_record,
)
from app.db.jobber_disconnect_transactions import Deadline, BoundedTransaction, run_transaction, read_document


def test_unambiguous_hash_identity():
    assert compute_account_barrier_key("a:b", "c") != compute_account_barrier_key("a", "b:c")
    assert compute_work_doc_id("a:b", "c") != compute_work_doc_id("a", "b:c")
    assert compute_account_barrier_key("a", "b") != compute_work_doc_id("a", "b")


@pytest.mark.parametrize("value", [1.0, 1, None, True, dt.datetime(2026, 1, 1),
                                    SimpleNamespace(timestamp=lambda: 100.0)])
def test_authority_rejects_nonserver_clock(value):
    with pytest.raises(JobberDisconnectError):
        extract_snapshot_time(FakeDocumentSnapshot("id", {}, read_time=value))


def test_receipt_retry_and_replay_with_real_readback():
    db = active_db()
    first = admit(db)
    assert admit(db) == first
    assert db._data["jobber_disconnect_control/current"]["receipt_counter"] == 1
    assert admit(db, admission="adm_2")["receipt_counter"] == 2
    assert sum(read_only for read_only, _ in db.commits) == 3
    assert all(retry is None and timeout <= 2 for _, retry, timeout in db.rpc_calls)


def test_atomic_failure_and_commit_response_loss():
    db = active_db()
    before = clone(db._data)
    db.fail_write_path = "jobber_disconnect_work/" + compute_work_doc_id("app", "account")
    with pytest.raises(Exception):
        admit(db)
    assert db._data == before
    db.fail_write_path = None
    db.commit_then_raise = True
    assert admit(db)["receipt_counter"] == 1
    assert db._data["jobber_disconnect_control/current"]["receipt_counter"] == 1


@pytest.mark.parametrize("changes", [
    {"enforcement_active": False}, {"schema_version": True}, {"receipt_counter": True},
    {"receipt_counter": MAX_RECEIPT_COUNTER}, {"receipt_counter": -1}, {"extra": "unrecognized"},
])
def test_invalid_control_does_not_mutate(changes):
    db = active_db()
    db.collection("jobber_disconnect_control").document("current").update(changes)
    before = clone(db._data)
    with pytest.raises(JobberDisconnectError):
        admit(db)
    assert db._data == before


@pytest.mark.parametrize("field,value", [
    ("app_id", "other"), ("account_id", "other"), ("work_id", "f" * 64),
    ("barrier", 2), ("last_receipt_id", "another"), ("status", "processing"),
])
def test_cross_record_corruption_rejects_both_same_id_and_new(field, value):
    db = active_db()
    admit(db)
    ref = db.collection("jobber_disconnect_work").document(compute_work_doc_id("app", "account"))
    ref.update({field: value})
    before = clone(db._data)
    for admission in ("adm_1", "adm_2"):
        with pytest.raises(JobberDisconnectError):
            admit(db, admission=admission)
        assert db._data == before


def test_same_id_cannot_change_input():
    db = active_db()
    admit(db)
    with pytest.raises(JobberDisconnectError):
        record_disconnect_admission_atomic(db, "adm_1", "b" * 64, "app", "account", "2026-10-10T00:00:00Z")


def test_fake_matches_sdk_decorator_and_atomic_delete_semantics():
    db = FakeFirestoreDb()
    ref = db.collection("x").document("a")
    ref.set({"keep": 1, "remove": 2})
    @transactional
    def change(txn):
        ref.get(transaction=txn)
        txn.update(ref, {"remove": DELETE_FIELD})
    change(db.transaction())
    assert ref.get().to_dict() == {"keep": 1}
    def invalid(txn):
        txn.update(ref, {"keep": 3})
        read_document(ref, txn)
    with pytest.raises(ValueError):
        run_transaction(db, invalid)
    assert ref.get().to_dict() == {"keep": 1}


def test_actual_sdk_transaction_rpc_ids_deadlines_and_read_only():
    client = Client(project="fictional-project", credentials=AnonymousCredentials())
    rpc = SimpleNamespace(
        begin_transaction=Mock(return_value=SimpleNamespace(transaction=b"txn-id")),
        commit=Mock(return_value=SimpleNamespace(write_results=[], commit_time=None)),
        rollback=Mock(return_value=None),
    )
    client._firestore_api_internal = rpc
    txn = BoundedTransaction(client, deadline=Deadline(), read_only=True)
    txn._begin()
    assert txn.id == b"txn-id"
    txn._commit()
    assert rpc.commit.call_args.kwargs["request"]["transaction"] == b"txn-id"
    assert rpc.begin_transaction.call_args.kwargs["request"]["options"].read_only is not None
    txn = BoundedTransaction(client, deadline=Deadline())
    txn._begin()
    txn._rollback()
    assert rpc.rollback.call_args.kwargs["request"]["transaction"] == b"txn-id"
    for method in (rpc.begin_transaction, rpc.commit, rpc.rollback):
        for call in method.call_args_list:
            assert call.kwargs["retry"] is None
            assert 0 < call.kwargs["timeout"] <= 2


def test_actual_sdk_runner_caps_aborted_commits_at_three():
    client = Client(project="fictional-project", credentials=AnonymousCredentials())
    rpc = SimpleNamespace(
        begin_transaction=Mock(return_value=SimpleNamespace(transaction=b"transaction-id")),
        commit=Mock(side_effect=Aborted("fictional conflict")), rollback=Mock(),
    )
    client._firestore_api_internal = rpc
    with pytest.raises(Aborted):
        run_transaction(client, lambda txn: None)
    assert rpc.begin_transaction.call_count == 3
    assert rpc.commit.call_count == 3
    assert rpc.rollback.call_count == 3


def test_real_sdk_empty_batch_get_never_synthesizes_authoritative_time():
    client = Client(project="fictional-project", credentials=AnonymousCredentials())
    rpc = SimpleNamespace(
        begin_transaction=Mock(return_value=SimpleNamespace(transaction=b"txn-id")),
        batch_get_documents=Mock(return_value=iter([])), rollback=Mock(),
    )
    client._firestore_api_internal = rpc
    with pytest.raises(ValueError, match="exactly one"):
        run_transaction(client, lambda txn: read_document(client.collection("x").document("missing"), txn), read_only=True)
    call = rpc.batch_get_documents.call_args.kwargs
    assert call["request"]["transaction"] == b"txn-id"
    assert call["retry"] is None and 0 < call["timeout"] <= 2


def test_retry_restarts_with_new_server_snapshot():
    db = active_db()
    ref = db.collection("x").document("a")
    ref.set({"value": 0})
    seen = []
    def action(txn):
        snap = read_document(ref, txn)
        seen.append(snap.read_time)
        txn.update(ref, {"value": snap.to_dict()["value"] + 1})
    def conflict(txn):
        db.set_server_time(db.get_server_time() + dt.timedelta(seconds=10))
        ref.update({"value": 3})
    db.before_commit = conflict
    run_transaction(db, action)
    assert len(seen) == 2 and seen[1] > seen[0]
    assert ref.get().to_dict()["value"] == 4


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.parametrize("clock", [None, 100.0, dt.datetime(2026, 1, 1)])
def test_bounded_read_validates_clock_on_every_actual_or_missing_snapshot(exists, clock):
    db = FakeFirestoreDb()
    ref = db.collection("authority").document("record")
    if exists:
        ref.set({"value": 1})
    db.set_server_time(clock)
    with pytest.raises(JobberDisconnectError):
        run_transaction(db, lambda txn: read_document(ref, txn), read_only=True)


def test_aborted_retry_with_missing_server_clock_cannot_commit():
    db = active_db()
    ref = db.collection("authority").document("record")
    ref.set({"value": 0})
    def action(txn):
        snap = read_document(ref, txn)
        txn.update(ref, {"value": snap.to_dict()["value"] + 1})
    def conflict(txn):
        ref.update({"value": 3})
        db.set_server_time(None)
    db.before_commit = conflict
    with pytest.raises(JobberDisconnectError):
        run_transaction(db, action)
    assert db._data[ref.path] == {"value": 3}
