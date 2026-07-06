"""Admin controls for Jobber lead capture."""

import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from google.cloud.firestore_v1 import DELETE_FIELD

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
        self.data = data
        self.updates = []

    def get(self):
        return _FakeSnapshot(self.doc_id, self.data)

    def update(self, updates: dict):
        if self.data is None:
            raise RuntimeError("missing document")
        self.updates.append(updates)
        self.data.update(updates)


class _FakeCollection:
    def __init__(self, docs: dict[str, _FakeDocRef]):
        self.docs = docs

    def document(self, doc_id: str):
        return self.docs.get(doc_id, _FakeDocRef(doc_id, None))


class _FakeFirestore:
    def __init__(self, docs: dict[str, _FakeDocRef]):
        self.docs = docs

    def collection(self, name: str):
        assert name == "contractors"
        return _FakeCollection(self.docs)


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
    doc = _FakeDocRef("contractor-1", {"jobber_access_token": "token"})
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
    doc = _FakeDocRef("contractor-1", {
        "jobber_access_token": "",
        "jobber_lead_capture_enabled": True,
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: _FakeFirestore({"contractor-1": doc}))

    response = await integrations.jobber_disconnect(
        contractor_id="contractor-1",
        request=_request(is_admin=True),
    )

    assert response == {"status": "disconnected"}
    assert doc.updates == [{
        "jobber_access_token": DELETE_FIELD,
        "jobber_refresh_token": DELETE_FIELD,
        "jobber_connected_at": DELETE_FIELD,
        "jobber_lead_capture_enabled": False,
    }]
