"""Offline, synchronized create-only receipts and real Firebase SDK references."""

from copy import deepcopy
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

from google.api_core.exceptions import AlreadyExists

from tests.fixtures.fake_rtdb import use_fake_rtdb


def registration_stores(monkeypatch):
    from app.services import call_registration

    documents = {}
    lock = threading.RLock()
    receipts_read = threading.Event()

    class Document:
        def __init__(self, sid):
            self.sid = sid

        def create(self, value, *, retry, timeout):
            assert retry is None and 0 < timeout <= 0.5
            with lock:
                if self.sid in documents:
                    raise AlreadyExists("synthetic duplicate")
                documents[self.sid] = deepcopy(value)

        def get(self, **kwargs):
            with lock:
                value = deepcopy(documents.get(self.sid))
            receipts_read.set()
            return SimpleNamespace(exists=value is not None, to_dict=lambda: value)

        def set(self, value, merge=False):
            with lock:
                documents[self.sid] = {**(documents.get(self.sid, {}) if merge else {}), **deepcopy(value)}

    class Database:
        def collection(self, name):
            assert name == "calls"
            return SimpleNamespace(document=Document)

    database = Database()
    monkeypatch.setattr(call_registration, "get_firestore_client", lambda: database)
    client = use_fake_rtdb(monkeypatch, active_calls={})["active_calls"]
    # Firebase callbacks run in executor threads. Serialize individual HTTP
    # operations, not whole transactions: stale ETags still exercise SDK retry.
    for name in ("body", "headers_and_body", "headers", "request"):
        original = getattr(client, name)

        def guarded(*args, _method=original, **kwargs):
            with lock:
                return _method(*args, **kwargs)

        monkeypatch.setattr(client, name, guarded)
    conference = AsyncMock()
    monkeypatch.setattr("app.services.conference_registry.register_conference", conference)
    return SimpleNamespace(
        documents=documents, client=client, lock=lock, database=database,
        receipts_read=receipts_read, conference=conference,
    )
