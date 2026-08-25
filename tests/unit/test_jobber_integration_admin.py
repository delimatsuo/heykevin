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
        import datetime
        import time

        self.id = doc_id
        self._data = data
        self.exists = data is not None
        self.read_time = datetime.datetime.fromtimestamp(time.time(), datetime.UTC)

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, doc_id: str, data: dict | None):
        self.doc_id = doc_id
        self.data = dict(data) if data is not None else None
        self.updates = []
        self.deleted = False

    @property
    def exists(self):
        return self.data is not None

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
        return self.docs.setdefault(doc_id, _FakeDocRef(doc_id, None))


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
                if self._staged_updates or self._staged_sets or self._staged_deletes:
                    raise RuntimeError("Firestore transaction read-after-write violation: all reads must occur before writes/deletes/creates")
                return doc_ref.get()

            def update(self, doc_ref, updates):
                self._staged_updates.append((doc_ref, dict(updates)))

            def delete(self, doc_ref):
                self._staged_deletes.append(doc_ref)

            def set(self, doc_ref, data):
                self._staged_sets.append((doc_ref, dict(data)))

            def create(self, doc_ref, data):
                if doc_ref.exists:
                    raise RuntimeError("Document already exists")
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
async def test_jobber_lead_capture_model_strict_bool_validation():
    from pydantic import ValidationError

    # Exact booleans succeed
    update_true = integrations.JobberLeadCaptureUpdate(enabled=True)
    assert update_true.enabled is True
    update_false = integrations.JobberLeadCaptureUpdate(enabled=False)
    assert update_false.enabled is False

    # Coercible aliases MUST fail validation
    for invalid in (1, 0, "true", "false", "True", "False", 1.0, 0.0, [], {}, None):
        with pytest.raises(ValidationError):
            integrations.JobberLeadCaptureUpdate.model_validate({"enabled": invalid})


@pytest.mark.asyncio
async def test_jobber_status_includes_lead_capture_flag(monkeypatch):
    doc = _FakeDocRef("contractor-1", {
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
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
    doc = _FakeDocRef("contractor-1", {
        "active": True,
        "jobber_connected": False,
        "jobber_lead_capture_enabled": True,
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: _FakeFirestore({"contractor-1": doc}))

    response = await integrations.jobber_status(
        contractor_id="contractor-1",
        request=_request(is_admin=True),
    )

    assert response["connected"] is False
    assert response["connected_at"] is None
    assert response["lead_capture_enabled"] is False


@pytest.mark.asyncio
async def test_admin_can_enable_jobber_lead_capture_for_connected_contractor(monkeypatch):
    audit_events = []
    doc = _FakeDocRef("contractor-1", {
        "active": True,
        "jobber_connected": True,
        "jobber_access_token": "token",
        "jobber_refresh_token": "refresh-token",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": False,
    })
    db = _FakeFirestore({"contractor-1": doc})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)

    response = await integrations.jobber_update_lead_capture(
        integrations.JobberLeadCaptureUpdate(enabled=True, reason="staging validation"),
        contractor_id="contractor-1",
        request=_request(is_admin=True),
    )

    assert response["status"] == "ok"
    assert response["contractor_id"] == "contractor-1"
    assert response["connected"] is True
    assert response["lead_capture_enabled"] is True
    assert isinstance(response["updated_at"], float)
    assert doc.data["jobber_lead_capture_enabled"] is True
    assert doc.data["jobber_lead_capture_updated_at"] == response["updated_at"]

    audit_col = db.collection("admin_audit_events")
    audit_docs = list(audit_col.docs.values())
    assert len(audit_docs) == 1
    audit_data = audit_docs[0].data
    assert audit_data["action"] == "jobber_lead_capture_update"
    assert audit_data["reason"] == "staging validation"
    assert audit_data["before"] == {"jobber_lead_capture_enabled": False}
    assert audit_data["after"] == {"jobber_lead_capture_enabled": True}
    assert audit_data["metadata"]["jobber_connected"] is True
    assert audit_data["metadata"]["timestamp"] == response["updated_at"]
    assert audit_data["created_at"] == response["updated_at"]


@pytest.mark.asyncio
async def test_admin_can_disable_jobber_lead_capture_even_when_disconnected(monkeypatch):
    doc = _FakeDocRef("contractor-1", {
        "active": False,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": True,
    })
    db = _FakeFirestore({"contractor-1": doc})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)

    response = await integrations.jobber_update_lead_capture(
        integrations.JobberLeadCaptureUpdate(enabled=False, reason="disable inactive"),
        contractor_id="contractor-1",
        request=_request(is_admin=True),
    )

    assert response["status"] == "ok"
    assert response["contractor_id"] == "contractor-1"
    assert response["connected"] is False
    assert response["lead_capture_enabled"] is False
    assert isinstance(response["updated_at"], float)
    assert doc.data["jobber_lead_capture_enabled"] is False
    assert doc.data["jobber_lead_capture_updated_at"] == response["updated_at"]

    audit_col = db.collection("admin_audit_events")
    audit_docs = list(audit_col.docs.values())
    assert len(audit_docs) == 1
    audit_data = audit_docs[0].data
    assert audit_data["before"] == {"jobber_lead_capture_enabled": True}
    assert audit_data["after"] == {"jobber_lead_capture_enabled": False}
    assert audit_data["created_at"] == response["updated_at"]


@pytest.mark.asyncio
async def test_contractor_token_cannot_toggle_jobber_lead_capture(monkeypatch):
    doc = _FakeDocRef("contractor-1", {
        "active": True,
        "jobber_connected": True,
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
    audit_events = []
    doc = _FakeDocRef("contractor-1", {
        "active": True,
        "jobber_connected": False,
        "jobber_lead_capture_enabled": False,
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: _FakeFirestore({"contractor-1": doc}))

    async def fake_audit(**kwargs):
        audit_events.append(kwargs)

    from app.db import admin_audit
    monkeypatch.setattr(admin_audit, "write_admin_audit_event", fake_audit)

    with pytest.raises(HTTPException) as exc:
        await integrations.jobber_update_lead_capture(
            integrations.JobberLeadCaptureUpdate(enabled=True),
            contractor_id="contractor-1",
            request=_request(is_admin=True),
        )

    assert exc.value.status_code == 409
    assert doc.updates == []
    assert len(audit_events) == 0


@pytest.mark.asyncio
async def test_jobber_disconnect_disables_lead_capture(monkeypatch):
    import base64

    import app.services.integration_token_mutations as mutations_module
    from app.config import settings

    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(
        settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}'
    )
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")

    doc = _FakeDocRef("contractor-1", {
        "contractor_id": "contractor-1",
        "active": True,
        "jobber_access_token": "old-token",
        "jobber_refresh_token": "refresh-token",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
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


@pytest.mark.asyncio
async def test_write_admin_audit_event_created_at_validation_and_preservation(monkeypatch):
    import app.db.admin_audit as admin_audit_module
    from app.db.admin_audit import write_admin_audit_event

    written_docs = []

    class _FakeCollection:
        def document(self):
            class _Doc:
                def set(self, data):
                    written_docs.append(data)
            return _Doc()

    class _FakeAuditDB:
        def collection(self, name):
            assert name == "admin_audit_events"
            return _FakeCollection()

    fake_db = _FakeAuditDB()
    monkeypatch.setattr(admin_audit_module, "get_firestore_client", lambda: fake_db)

    # 1. Valid exact positive float is strictly preserved
    await write_admin_audit_event(
        request=_request(is_admin=True),
        action="jobber_lead_capture_update",
        target_type="contractor",
        target_id="contractor-1",
        reason="testing",
        created_at=12345.678,
    )
    assert len(written_docs) == 1
    assert written_docs[0]["created_at"] == 12345.678
    assert type(written_docs[0]["created_at"]) is float

    # 2. Omitted created_at defaults to time.time()
    monkeypatch.setattr(admin_audit_module.time, "time", lambda: 9999.0)
    await write_admin_audit_event(
        request=_request(is_admin=True),
        action="jobber_lead_capture_update",
        target_type="contractor",
        target_id="contractor-1",
        reason="testing default",
    )
    assert len(written_docs) == 2
    assert written_docs[1]["created_at"] == 9999.0

    # 3. Invalid created_at values fail closed before DB access with zero writes
    def _forbidden_db_access():
        raise AssertionError("DB access must not occur when created_at is invalid")

    monkeypatch.setattr(admin_audit_module, "get_firestore_client", _forbidden_db_access)

    class CustomFloat(float):
        pass

    invalid_values = [
        12345,
        0,
        1,
        True,
        False,
        "12345.0",
        0.0,
        -0.0,
        -1.0,
        -12345.678,
        float("nan"),
        float("inf"),
        float("-inf"),
        [],
        {},
        CustomFloat(12345.0),
    ]

    for invalid in invalid_values:
        with pytest.raises(ValueError, match="created_at must be an exact finite positive float"):
            await write_admin_audit_event(
                request=_request(is_admin=True),
                action="jobber_lead_capture_update",
                target_type="contractor",
                target_id="contractor-1",
                reason="testing invalid",
                created_at=invalid,
            )

    # Exactly 2 docs written from the first two valid calls
    assert len(written_docs) == 2


@pytest.mark.asyncio
async def test_jobber_lead_capture_sentinel_non_disclosure_in_http_details(monkeypatch, caplog):
    """Prove secret sentinels and contractor IDs are absent from 409/500 HTTP details and caplog in Jobber lead capture."""
    import logging
    cid = "secret_cid_sentinel_12345"
    secret_sentinel = "secret_payload_sentinel_xyz"

    doc = _FakeDocRef(cid, {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_lead_capture_enabled": False,
    })
    db = _FakeFirestore({cid: doc})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)

    # 1. Conflict (409) HTTP detail is safe fixed detail, no str(exc) or contractor ID
    with caplog.at_level(logging.ERROR):
        with pytest.raises(HTTPException) as exc_info:
            await integrations.jobber_update_lead_capture(
                integrations.JobberLeadCaptureUpdate(enabled=True),
                contractor_id=cid,
                request=_request(is_admin=True),
            )
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "Jobber lead capture update conflict"
        assert secret_sentinel not in exc_info.value.detail
        assert cid not in exc_info.value.detail

    # 2. Generic failure (500) log and detail are safe fixed strings
    async def _failing_mutation(*args, **kwargs):
        raise RuntimeError(f"Internal breakdown: {secret_sentinel} for {cid}")

    monkeypatch.setattr("app.services.integration_token_mutations.update_jobber_lead_capture_cas", _failing_mutation)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(HTTPException) as exc_info500:
            await integrations.jobber_update_lead_capture(
                integrations.JobberLeadCaptureUpdate(enabled=True),
                contractor_id=cid,
                request=_request(is_admin=True),
            )
        assert exc_info500.value.status_code == 500
        assert exc_info500.value.detail == "Failed to update Jobber lead capture"
        assert secret_sentinel not in exc_info500.value.detail
        assert cid not in exc_info500.value.detail
        assert secret_sentinel not in caplog.text
        assert cid not in caplog.text
