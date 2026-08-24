"""Admin controls for Jobber lead capture."""

import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.api import integrations


class _FakeSnapshot:
    def __init__(self, doc_id: str, data: dict | None):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, doc_id: str, data: dict | None):
        self.doc_id = doc_id
        self.data = dict(data) if data is not None else None
        self.updates = []
        self.deleted = False

    def get(self, *args, transaction=None, **kwargs):
        return _FakeSnapshot(self.doc_id, self.data)

    def update(self, updates: dict, *args, **kwargs):
        if self.data is None:
            self.data = {}
        self.updates.append(dict(updates))
        for k, v in updates.items():
            if str(type(v).__name__) == "Sentinel" or "DELETE" in str(v):
                self.data.pop(k, None)
            else:
                self.data[k] = v

    def set(self, data: dict, *args, **kwargs):
        self.data = dict(data)
        self.deleted = False

    def delete(self, *args, **kwargs):
        self.deleted = True
        self.data = None


class _FakeCollection:
    def __init__(self, docs: dict[str, _FakeDocRef]):
        self.docs = docs

    def document(self, doc_id: str):
        return self.docs.setdefault(doc_id, _FakeDocRef(doc_id, {}))


class _FakeFirestore:
    def __init__(self, docs: dict[str, _FakeDocRef]):
        self.docs = docs
        self._other_colls = {}

    def collection(self, name: str):
        if name == "contractors":
            return _FakeCollection(self.docs)
        return _FakeCollection(self._other_colls.setdefault(name, {}))

    def transaction(self):
        class _Tx:
            def __init__(self):
                self._staged_updates = []
                self._staged_sets = []
                self._staged_deletes = []
                self.committed = False
                self._read_only = False
                self._id = b"fake-tx-id"
                self._max_attempts = 5
                self.in_progress = True

            def get(self, doc_ref):
                return doc_ref.get()

            def update(self, doc_ref, updates):
                self._staged_updates.append((doc_ref, dict(updates)))

            def delete(self, doc_ref):
                self._staged_deletes.append(doc_ref)

            def set(self, doc_ref, data):
                self._staged_sets.append((doc_ref, dict(data)))

            def _begin(self, *args, **kwargs):
                pass

            def _clean_up(self):
                pass

            def _rollback(self):
                self._staged_updates.clear()
                self._staged_sets.clear()
                self._staged_deletes.clear()

            def _commit(self):
                self.commit()
                return []

            def commit(self):
                for doc_ref, data in self._staged_sets:
                    doc_ref.set(data)
                for doc_ref, updates in self._staged_updates:
                    doc_ref.update(updates)
                for doc_ref in self._staged_deletes:
                    doc_ref.delete()
                self.committed = True

        return _Tx()


def _request(*, is_admin: bool):
    return SimpleNamespace(
        state=SimpleNamespace(is_admin=is_admin, contractor_id="contractor-1"),
        headers={"user-agent": "pytest"},
        client=SimpleNamespace(host="127.0.0.1"),
    )


@pytest.mark.asyncio
async def test_jobber_status_includes_lead_capture_flag(monkeypatch):
    doc = _FakeDocRef("contractor-1", {
        "jobber_access_token": "token",
        "jobber_refresh_token": "refresh-token",
        "jobber_connected_at": 123.0,
        "jobber_lead_capture_enabled": True,
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: _FakeFirestore({"contractor-1": doc}))

    response = await integrations.jobber_status(
        contractor_id="contractor-1",
        request=_request(is_admin=True),
    )

    assert response == {
        "connected": True,
        "connected_at": 123.0,
        "lead_capture_enabled": True,
    }


@pytest.mark.asyncio
async def test_jobber_status_hides_enabled_flag_when_disconnected(monkeypatch):
    doc = _FakeDocRef("contractor-1", {"jobber_lead_capture_enabled": True})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: _FakeFirestore({"contractor-1": doc}))

    response = await integrations.jobber_status(
        contractor_id="contractor-1",
        request=_request(is_admin=True),
    )

    assert response["connected"] is False
    assert response["lead_capture_enabled"] is False


@pytest.mark.asyncio
async def test_admin_can_enable_jobber_lead_capture_for_connected_contractor(monkeypatch):
    audit_events = []
    doc = _FakeDocRef("contractor-1", {
        "jobber_access_token": "token",
        "jobber_refresh_token": "refresh-token",
        "jobber_lead_capture_enabled": False,
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: _FakeFirestore({"contractor-1": doc}))

    async def fake_audit(**kwargs):
        audit_events.append(kwargs)

    monkeypatch.setattr(integrations, "write_admin_audit_event", fake_audit)
    monkeypatch.setattr(integrations.time, "time", lambda: 12345.0)

    response = await integrations.jobber_update_lead_capture(
        integrations.JobberLeadCaptureUpdate(enabled=True, reason="staging validation"),
        contractor_id="contractor-1",
        request=_request(is_admin=True),
    )

    assert response == {
        "status": "ok",
        "contractor_id": "contractor-1",
        "connected": True,
        "lead_capture_enabled": True,
        "updated_at": 12345.0,
    }
    assert doc.updates == [{
        "jobber_lead_capture_enabled": True,
        "jobber_lead_capture_updated_at": 12345.0,
    }]
    assert audit_events[0]["action"] == "jobber_lead_capture_update"
    assert audit_events[0]["reason"] == "staging validation"
    assert audit_events[0]["before"] == {"jobber_lead_capture_enabled": False}
    assert audit_events[0]["after"] == {"jobber_lead_capture_enabled": True}


@pytest.mark.asyncio
async def test_contractor_token_cannot_toggle_jobber_lead_capture(monkeypatch):
    doc = _FakeDocRef("contractor-1", {
        "jobber_access_token": "token",
        "jobber_refresh_token": "refresh-token",
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: _FakeFirestore({"contractor-1": doc}))

    with pytest.raises(HTTPException) as exc:
        await integrations.jobber_update_lead_capture(
            integrations.JobberLeadCaptureUpdate(enabled=True),
            contractor_id="contractor-1",
            request=_request(is_admin=False),
        )

    assert exc.value.status_code == 403
    assert doc.updates == []


@pytest.mark.asyncio
async def test_admin_cannot_enable_jobber_lead_capture_until_connected(monkeypatch):
    doc = _FakeDocRef("contractor-1", {"jobber_lead_capture_enabled": False})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: _FakeFirestore({"contractor-1": doc}))

    with pytest.raises(HTTPException) as exc:
        await integrations.jobber_update_lead_capture(
            integrations.JobberLeadCaptureUpdate(enabled=True),
            contractor_id="contractor-1",
            request=_request(is_admin=True),
        )

    assert exc.value.status_code == 409
    assert doc.updates == []


@pytest.mark.asyncio
async def test_jobber_disconnect_disables_lead_capture(monkeypatch):
    import app.services.integration_token_mutations as mutations_module
    import base64
    from app.config import settings

    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(
        settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}'
    )
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")

    doc = _FakeDocRef("contractor-1", {
        "contractor_id": "contractor-1",
        "jobber_access_token": "old-token",
        "jobber_refresh_token": "refresh-token",
        "jobber_generation": 0,
        "jobber_connected": True,
        "jobber_lead_capture_enabled": True,
    })
    db = _FakeFirestore({"contractor-1": doc})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    response = await integrations.jobber_disconnect(
        contractor_id="contractor-1",
        request=_request(is_admin=True),
    )

    assert response["status"] == "disconnected"
    assert doc.data["jobber_lead_capture_enabled"] is False
    assert doc.data["jobber_connected"] is False
    assert "jobber_access_token" not in doc.data
    assert doc.data["jobber_generation"] == 1
