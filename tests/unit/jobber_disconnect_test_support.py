"""Offline datastore with real transaction lifecycle, atomic commits and RPC bounds."""
from __future__ import annotations
import copy
import datetime as dt
import os
import threading
import uuid

import pytest
from google.api_core.exceptions import Aborted
from google.cloud.firestore_v1 import DELETE_FIELD
from google.cloud.firestore_v1.base_query import FieldFilter


def seed_required_environment():
    # Called by test modules after tests/conftest.py snapshots the original names.
    for name, value in {
        "TWILIO_ACCOUNT_SID": "AC00000000000000000000000000000000",
        "TWILIO_AUTH_TOKEN": "fictional-auth-token",
        "TWILIO_PHONE_NUMBER": "+15005550006",
        "TELEGRAM_BOT_TOKEN": "fictional-telegram-token",
        "USER_PHONE": "+15005550001",
    }.items():
        os.environ.setdefault(name, value)


@pytest.fixture(autouse=True)
def jobber_settings(monkeypatch):
    from app.config import settings
    for key, value in {
        "jobber_client_id": "oauth-client-distinct-from-app",
        "jobber_client_secret": "fictional-oauth-secret",
        "jobber_disconnect_expected_app_id": "app",
        "jobber_disconnect_receiver_enabled": True,
        "jobber_disconnect_recovery_enabled": False,
        "jobber_disconnect_enforce": False,
        "jobber_disconnect_enforcement_mode": "off",
    }.items():
        monkeypatch.setattr(settings, key, value)
    return settings


def clone(value):
    return copy.deepcopy(value, {id(DELETE_FIELD): DELETE_FIELD})


class FakeDocumentSnapshot:
    def __init__(self, doc_id, data, exists=True, read_time=None):
        self.id = doc_id
        self._data = clone(data)
        self.exists = exists and data is not None
        self.read_time = read_time

    def to_dict(self):
        return clone(self._data)


class FakeDocumentReference:
    def __init__(self, doc_id, collection):
        if type(doc_id) is not str or not doc_id or "/" in doc_id:
            raise ValueError("Invalid fake document path")
        self.id = doc_id
        self.collection = collection
        self.path = collection.name + "/" + doc_id

    def get(self, transaction=None, *, retry=None, timeout=2.0):
        self.collection.db.record_rpc("get", retry, timeout)
        if transaction is not None:
            return next(transaction.get(self, retry=retry, timeout=timeout))
        return FakeDocumentSnapshot(self.id, self.collection.db._data.get(self.path),
                                    read_time=self.collection.db.get_server_time())

    def set(self, data, merge=False, **kwargs):
        with self.collection.db.lock:
            target = clone(self.collection.db._data)
            _apply(target, "set", self.path, clone(data), merge)
            self.collection.db._data = target
            self.collection.db.version += 1

    def update(self, data, **kwargs):
        with self.collection.db.lock:
            target = clone(self.collection.db._data)
            _apply(target, "update", self.path, clone(data), False)
            self.collection.db._data = target
            self.collection.db.version += 1

    def delete(self, **kwargs):
        with self.collection.db.lock:
            self.collection.db._data.pop(self.path, None)
            self.collection.db.version += 1


def _apply(target, op, path, payload, merge):
    if op == "delete":
        target.pop(path, None)
        return
    if op == "create" and path in target:
        raise ValueError("Create collision")
    if op == "update" and path not in target:
        raise ValueError("Update missing document")
    if op == "set" and not merge or op == "create":
        target[path] = {}
    dest = target.setdefault(path, {})
    for key, value in payload.items():
        if value is DELETE_FIELD:
            dest.pop(key, None)
        else:
            dest[key] = clone(value)


class FakeQuery:
    def __init__(self, collection, filters=(), order=None, cursor=None, count=None):
        self.collection, self.filters, self.order, self.cursor, self.count = collection, filters, order, cursor, count

    def where(self, *args, filter=None):
        filt = filter or FieldFilter(*args)
        return FakeQuery(self.collection, self.filters + (filt,), self.order, self.cursor, self.count)

    def order_by(self, field):
        return FakeQuery(self.collection, self.filters, field, self.cursor, self.count)

    def limit(self, count):
        return FakeQuery(self.collection, self.filters, self.order, self.cursor, count)

    def start_after(self, cursor):
        if type(cursor) is not dict or set(cursor) != {"__name__"} or not isinstance(cursor["__name__"], FakeDocumentReference):
            raise ValueError("Cursor must be an actual supported map")
        return FakeQuery(self.collection, self.filters, self.order, cursor["__name__"].id, self.count)

    def stream(self, *, retry=None, timeout=2.0):
        db = self.collection.db
        db.record_rpc("stream", retry, timeout)
        prefix = self.collection.name + "/"
        rows = []
        for path, data in clone(db._data).items():
            if not path.startswith(prefix) or "/" in path[len(prefix):]:
                continue
            if any((f.op_string == "==" and data.get(f.field_path) != f.value)
                   or (f.op_string == "in" and data.get(f.field_path) not in f.value) for f in self.filters):
                continue
            doc_id = path[len(prefix):]
            if self.cursor is not None and doc_id <= self.cursor:
                continue
            rows.append((doc_id, data))
        rows.sort(key=lambda item: item[0])
        for doc_id, data in rows[:self.count]:
            yield FakeDocumentSnapshot(doc_id, data, read_time=db.get_server_time())


class FakeCollectionReference:
    def __init__(self, name, db):
        self.name, self.db = name, db

    def document(self, doc_id):
        return FakeDocumentReference(doc_id, self)

    def where(self, *args, **kwargs):
        return FakeQuery(self).where(*args, **kwargs)

    def order_by(self, field):
        return FakeQuery(self).order_by(field)

    def limit(self, count):
        return FakeQuery(self).limit(count)

    def stream(self, **kwargs):
        return FakeQuery(self).stream(**kwargs)


class FakeTransaction:
    def __init__(self, db, *, deadline=None, read_only=False, max_attempts=3):
        self.db = db
        self.jobber_deadline = deadline
        self._read_only, self._max_attempts = read_only, max_attempts
        self._id = None
        self.writes = []
        self.write_results = []
        self.commit_time = None

    @property
    def id(self):
        return self._id

    @property
    def in_progress(self):
        return self._id is not None

    def _clean_up(self):
        self._id = None
        self.writes = []

    def _begin(self, retry_id=None):
        if self.in_progress:
            raise ValueError("Transaction already begun")
        self.db.record_rpc("begin", None, self.jobber_deadline.timeout() if self.jobber_deadline else 2.0)
        with self.db.lock:
            self.snapshot = clone(self.db._data)
            self.version = self.db.version
            self.read_time = self.db.get_server_time()
        self._id = uuid.uuid4().bytes

    def get(self, ref, *, retry=None, timeout=2.0):
        self.db.record_rpc("get", retry, timeout)
        if not self.in_progress:
            raise ValueError("Transaction must be begun")
        if self.writes:
            raise ValueError("Read after write")
        return iter([FakeDocumentSnapshot(ref.id, self.snapshot.get(ref.path), read_time=self.read_time)])

    def _write(self, op, ref, data=None, merge=False):
        if not self.in_progress or self._read_only:
            raise ValueError("No writable transaction")
        self.writes.append((op, ref.path, clone(data), merge))

    def set(self, ref, data, merge=False):
        self._write("set", ref, data, merge)

    def update(self, ref, data):
        self._write("update", ref, data)

    def create(self, ref, data):
        self._write("create", ref, data)

    def delete(self, ref):
        self._write("delete", ref)

    def _commit(self):
        if not self.in_progress:
            raise ValueError("Transaction not begun")
        self.db.record_rpc("commit", None, self.jobber_deadline.timeout() if self.jobber_deadline else 2.0)
        hook = self.db.before_commit
        if hook is not None:
            self.db.before_commit = None
            hook(self)
        with self.db.lock:
            if self.db.version != self.version:
                raise Aborted("Concurrent fake transaction")
            target = clone(self.db._data)
            for op, path, data, merge in self.writes:
                if path == self.db.fail_write_path:
                    raise RuntimeError("Injected midcommit failure")
                _apply(target, op, path, data, merge)
            had_writes = bool(self.writes)
            self.db._data = target
            if had_writes:
                self.db.version += 1
            self.db.commits.append((self._read_only, had_writes))
        self._clean_up()
        if had_writes and self.db.commit_then_raise:
            self.db.commit_then_raise = False
            raise RuntimeError("Commit response lost")
        return []

    def _rollback(self):
        self.db.record_rpc("rollback", None, 0.25)
        self._clean_up()


class FakeFirestoreDb:
    def __init__(self, fixed_server_time=None):
        self._data = {}
        self.version = 0
        self.lock = threading.RLock()
        self._fixed_server_time = fixed_server_time or dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc)
        self.before_commit = None
        self.fail_write_path = None
        self.commit_then_raise = False
        self.rpc_calls = []
        self.commits = []

    def record_rpc(self, name, retry, timeout):
        assert retry is None and type(timeout) in (int, float) and 0 < timeout <= 2.0
        self.rpc_calls.append((name, retry, timeout))

    def get_server_time(self):
        return self._fixed_server_time

    def set_server_time(self, value):
        self._fixed_server_time = value

    def collection(self, name):
        return FakeCollectionReference(name, self)

    def transaction(self, max_attempts=3, read_only=False):
        return FakeTransaction(self, max_attempts=max_attempts, read_only=read_only)

    def jobber_transaction_factory(self, *, deadline, read_only):
        return FakeTransaction(self, deadline=deadline, read_only=read_only)


def active_db():
    db = FakeFirestoreDb()
    db.collection("jobber_disconnect_control").document("current").set({
        "schema_version": 1, "expected_app_id": "app", "receipt_counter": 0,
        "enforcement_active": True, "updated_at": db.get_server_time().timestamp(),
    })
    return db


def admit(db, account="account", admission="adm_1"):
    from app.db.jobber_disconnect_receipts import record_disconnect_admission_atomic
    return record_disconnect_admission_atomic(db, admission, "a" * 64, "app", account,
                                              "2026-10-10T00:00:00Z")


def binding(db, cid="contractor", account="account", accepted=0):
    from app.services.integration_tokens import encrypt_integration_token
    data = {"active": True, "jobber_app_id": "app", "jobber_account_id": account,
            "jobber_generation": 2, "jobber_lifecycle_epoch": 3, "jobber_connected": True,
            "jobber_accepted_disconnect_barrier": accepted, "jobber_lead_capture_enabled": True,
            "jobber_access_token": encrypt_integration_token("fictional-access", contractor_id=cid, provider="jobber", token_kind="access"),
            "jobber_refresh_token": encrypt_integration_token("fictional-refresh", contractor_id=cid, provider="jobber", token_kind="refresh"),
            "jobber_token_envelope_required": True}
    db.collection("contractors").document(cid).set(data)
    return clone(data)
