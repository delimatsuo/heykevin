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
    def __init__(self, data=None):
        self.data = data
        self.deleted = False
        self.updates = []

    def get(self):
        return _FakeSnapshot(self.data)

    def update(self, updates):
        self.updates.append(dict(updates))
        self.data.update(updates)

    def delete(self):
        self.deleted = True


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def document(self, doc_id):
        return self.docs.setdefault(doc_id, _FakeDocRef())


class _FakeFirestore:
    def __init__(self, collections):
        self.collections = collections

    def collection(self, name):
        return _FakeCollection(self.collections.setdefault(name, {}))


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


def _oauth_firestore(*, refresh_token="existing-refresh"):
    state = _FakeDocRef({"contractor_id": "contractor-1", "expires_at": 2_000.0})
    contractor = _FakeDocRef({"google_calendar_refresh_token": refresh_token})
    db = _FakeFirestore(
        {
            "google_oauth_states": {"opaque-state": state},
            "contractors": {"contractor-1": contractor},
        }
    )
    return db, state, contractor


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
    assert "https://www.googleapis.com/auth/calendar.freebusy" != integrations.GOOGLE_CALENDAR_SCOPE


@pytest.mark.asyncio
async def test_callback_preserves_existing_refresh_token_and_records_expiry(monkeypatch):
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
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr(integrations.time, "time", lambda: 1_000.0)

    await integrations.google_calendar_callback(code="opaque-code", state="opaque-state")

    assert state.deleted is True
    assert contractor.data["google_calendar_access_token"] == "new-access"
    assert contractor.data["google_calendar_refresh_token"] == "existing-refresh"
    assert contractor.data["google_calendar_token_expires_at"] == 4_600.0
    assert contractor.data["google_calendar_scope"] == integrations.GOOGLE_CALENDAR_SCOPE
    assert "google_calendar_refresh_token" not in contractor.updates[0]


@pytest.mark.asyncio
async def test_callback_rejects_non_durable_connection_without_any_refresh_token(monkeypatch):
    db, _state, contractor = _oauth_firestore(refresh_token="")
    response = _FakeResponse(200, {"access_token": "new-access", "expires_in": 3600})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr(integrations.time, "time", lambda: 1_000.0)

    with pytest.raises(HTTPException) as exc:
        await integrations.google_calendar_callback(code="opaque-code", state="opaque-state")

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
            await integrations.google_calendar_callback(code="opaque-code", state="opaque-state")

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
            await integrations.google_calendar_callback(code="opaque-code", state="opaque-state")

    assert exc.value.status_code == 502
    assert "Google token response invalid" in caplog.text
    assert "exception_type=ValueError" in caplog.text
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
            await integrations.google_calendar_callback(code="opaque-code", state="opaque-state")

    assert exc.value.status_code == 502
    assert "Google token response invalid" in caplog.text
    assert "response_type=list" in caplog.text
