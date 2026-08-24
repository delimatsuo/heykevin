"""Google Calendar OAuth connect flow remains durable and least-privileged."""

import logging
import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.api import integrations


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, data=None, doc_id=None):
        self.data = dict(data) if data is not None else None
        self.deleted = False
        self.updates = []
        self.id = doc_id

    def get(self, *args, transaction=None, **kwargs):
        return _FakeSnapshot(self.data)

    def update(self, updates, *args, **kwargs):
        if self.data is None:
            self.data = {}
        self.updates.append(dict(updates))
        for k, v in updates.items():
            if str(type(v).__name__) == "Sentinel" or "DELETE" in str(v):
                self.data.pop(k, None)
            else:
                self.data[k] = v

    def delete(self, *args, **kwargs):
        self.deleted = True
        self.data = None

    def set(self, data, *args, **kwargs):
        self.data = dict(data)
        self.deleted = False


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def document(self, doc_id):
        if doc_id in self.docs:
            doc = self.docs[doc_id]
            doc.id = doc_id
            return doc
        doc = _FakeDocRef(doc_id=doc_id)
        self.docs[doc_id] = doc
        return doc


class _FakeFirestore:
    def __init__(self, collections):
        self.collections = collections

    def collection(self, name):
        return _FakeCollection(self.collections.setdefault(name, {}))

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


class _FakeResponse:
    def __init__(self, status_code, body, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body


class _InvalidJsonResponse(_FakeResponse):
    def json(self):
        raise ValueError("private-token-response-do-not-log")


class _FakeAsyncClient:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *_args, **_kwargs):
        return self.response


def _oauth_firestore(*, refresh_token="existing-refresh", access_token="existing-access"):
    state = _FakeDocRef({"contractor_id": "contractor-1", "expires_at": 2_000.0}, doc_id="opaque-state-12345678")
    c_data = {
        "contractor_id": "contractor-1",
        "active": True,
        "google_calendar_connected": bool(refresh_token),
        "google_calendar_generation": 0,
    }
    if refresh_token:
        c_data["google_calendar_refresh_token"] = refresh_token
        c_data["google_calendar_access_token"] = access_token
    contractor = _FakeDocRef(c_data, doc_id="contractor-1")
    db = _FakeFirestore(
        {
            "google_oauth_states": {"opaque-state-12345678": state},
            "contractors": {"contractor-1": contractor},
        }
    )
    return db, state, contractor


def _configure_keys(monkeypatch):
    import base64

    from app.config import settings

    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(
        settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}'
    )
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", True)


def test_google_calendar_scope_covers_both_availability_and_booking():
    """calendar.freebusy alone is read-only (confirmed against Google's own
    scope tables for freebusy.query vs. events.insert) and would silently
    break book_appointment for anyone connecting under it. calendar.events
    + calendar.freebusy is the narrowest pair that covers both operations
    Kevin actually performs — narrower than a blanket `calendar` grant.
    """
    scopes = integrations.GOOGLE_CALENDAR_SCOPE.split()
    assert "https://www.googleapis.com/auth/calendar.events" in scopes
    assert "https://www.googleapis.com/auth/calendar.freebusy" in scopes
    assert "https://www.googleapis.com/auth/calendar.readonly" not in scopes
    assert "https://www.googleapis.com/auth/calendar.freebusy" != integrations.GOOGLE_CALENDAR_SCOPE


@pytest.mark.asyncio
async def test_google_calendar_connect_returns_authorize_url(monkeypatch):
    """GET /api/integrations/google-calendar/connect generates authorize_url with least privilege."""
    from app.config import settings

    monkeypatch.setattr(settings, "google_calendar_client_id", "test-client-id")
    db = _FakeFirestore({"google_oauth_states": {}, "contractors": {}})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)

    from types import SimpleNamespace
    req = SimpleNamespace(state=SimpleNamespace(is_admin=True, contractor_id="contractor-1"))
    resp = await integrations.google_calendar_connect(contractor_id="contractor-1", request=req)
    assert "authorize_url" in resp
    assert "url" not in resp
    assert "state" not in resp
    url = resp["authorize_url"]
    assert "accounts.google.com" in url
    assert "calendar.events" in url
    assert "calendar.freebusy" in url
    assert "calendar.readonly" not in url


@pytest.mark.asyncio
async def test_callback_preserves_existing_refresh_token_and_records_expiry(monkeypatch):
    _configure_keys(monkeypatch)
    import app.services.integration_token_mutations as mutations_module
    from app.services.integration_tokens import decrypt_integration_token

    db, state, contractor = _oauth_firestore()
    response = _FakeResponse(
        200,
        {
            "access_token": "new-access",
            "expires_in": 3600,
            "scope": integrations.GOOGLE_CALENDAR_SCOPE,
        },
    )
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr(integrations.time, "time", lambda: 1_000.0)

    await integrations.google_calendar_callback(code="opaque-code", state="opaque-state-12345678")

    assert state.deleted is True
    assert contractor.data["google_calendar_access_token"]["schema_version"] == 1
    assert (
        decrypt_integration_token(
            contractor.data["google_calendar_access_token"],
            contractor_id="contractor-1",
            provider="google_calendar",
            token_kind="access",
        )
        == "new-access"
    )
    assert (
        decrypt_integration_token(
            contractor.data["google_calendar_refresh_token"],
            contractor_id="contractor-1",
            provider="google_calendar",
            token_kind="refresh",
        )
        == "existing-refresh"
    )
    assert contractor.data["google_calendar_token_expires_at"] == 4_600.0
    assert contractor.data["google_calendar_scope"] == integrations.GOOGLE_CALENDAR_SCOPE


@pytest.mark.asyncio
async def test_callback_rejects_non_durable_connection_without_any_refresh_token(monkeypatch):
    db, _state, contractor = _oauth_firestore(refresh_token="")
    response = _FakeResponse(200, {"access_token": "new-access", "expires_in": 3600})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr(integrations.time, "time", lambda: 1_000.0)

    with pytest.raises(HTTPException) as exc:
        await integrations.google_calendar_callback(code="opaque-code", state="opaque-state-12345678")

    assert exc.value.status_code == 502
    assert contractor.updates == []


@pytest.mark.asyncio
async def test_callback_error_logging_omits_provider_response(monkeypatch, caplog):
    sensitive_payload = "private-provider-callback-detail-do-not-log"
    db, _state, _contractor = _oauth_firestore()
    response = _FakeResponse(400, {}, text=sensitive_payload)
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr(integrations.time, "time", lambda: 1_000.0)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(HTTPException):
            await integrations.google_calendar_callback(code="opaque-code", state="opaque-state-12345678")

    assert "Google token exchange failed" in caplog.text
    assert "status_code=400" in caplog.text
    assert sensitive_payload not in caplog.text


@pytest.mark.asyncio
async def test_callback_invalid_json_returns_sanitized_bad_gateway(monkeypatch, caplog):
    sensitive_payload = "private-token-response-do-not-log"
    db, _state, _contractor = _oauth_firestore()
    response = _InvalidJsonResponse(200, {})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr(integrations.time, "time", lambda: 1_000.0)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(HTTPException) as exc:
            await integrations.google_calendar_callback(code="opaque-code", state="opaque-state-12345678")

    assert exc.value.status_code == 502
    assert "Google token response invalid" in caplog.text
    assert "result=invalid_json" in caplog.text
    assert sensitive_payload not in caplog.text


@pytest.mark.asyncio
async def test_callback_rejects_non_object_token_payload(monkeypatch, caplog):
    db, _state, _contractor = _oauth_firestore()
    response = _FakeResponse(200, ["unexpected"])
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr(integrations.time, "time", lambda: 1_000.0)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(HTTPException) as exc:
            await integrations.google_calendar_callback(code="opaque-code", state="opaque-state-12345678")

    assert exc.value.status_code == 502
    assert "Google token response invalid" in caplog.text
    assert "result=invalid_type" in caplog.text
