"""Admin audit event persistence tests."""

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

from app.db import admin_audit


class _FakeDocRef:
    def __init__(self):
        self.written = None

    def set(self, data):
        self.written = data


class _FakeAuditDoc:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeCollection:
    def __init__(self, docs: list[_FakeAuditDoc] | None = None):
        self.doc_ref = _FakeDocRef()
        self._docs = docs or []

    def document(self):
        return self.doc_ref

    def order_by(self, field_path, direction=None):
        reverse = direction == "DESCENDING"
        return _FakeCollection(
            sorted(
                self._docs,
                key=lambda doc: doc.to_dict().get(field_path, 0),
                reverse=reverse,
            )
        )

    def limit(self, value: int):
        return _FakeCollection(self._docs[:value])

    def stream(self):
        return list(self._docs)


class _FakeFirestore:
    def __init__(self, docs: list[_FakeAuditDoc] | None = None):
        self.collections = {"admin_audit_events": _FakeCollection(docs)}

    def collection(self, name):
        return self.collections[name]


def _request():
    return SimpleNamespace(
        state=SimpleNamespace(is_admin=True),
        headers={"user-agent": "pytest"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


@pytest.mark.asyncio
async def test_write_admin_audit_event_persists_actor_action_reason(monkeypatch):
    fake_db = _FakeFirestore()
    monkeypatch.setattr(admin_audit, "get_firestore_client", lambda: fake_db)

    await admin_audit.write_admin_audit_event(
        request=_request(),
        action="extend_trial",
        target_type="contractor",
        target_id="contractor-1",
        reason="customer support request",
        before={"subscription_status": "expired"},
        after={"subscription_status": "trial"},
    )

    written = fake_db.collections["admin_audit_events"].doc_ref.written
    assert written["action"] == "extend_trial"
    assert written["target_id"] == "contractor-1"
    assert written["reason"] == "customer support request"
    assert written["before"] == {"subscription_status": "expired"}
    assert written["after"] == {"subscription_status": "trial"}
    assert written["actor_type"] == "global_admin_token"
    assert written["user_agent"] == "pytest"
    assert "created_at" in written


@pytest.mark.asyncio
async def test_list_admin_audit_events_returns_newest_first(monkeypatch):
    fake_db = _FakeFirestore([
        _FakeAuditDoc("old", {"created_at": 100, "action": "old_action"}),
        _FakeAuditDoc("new", {"created_at": 200, "action": "new_action"}),
    ])
    monkeypatch.setattr(admin_audit, "get_firestore_client", lambda: fake_db)

    events = await admin_audit.list_admin_audit_events(limit=1)

    assert events == [{"id": "new", "created_at": 200, "action": "new_action"}]
