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

import datetime
import time

from app.api import integrations


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None
        self.read_time = datetime.datetime.fromtimestamp(time.time(), datetime.UTC)

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, data=None, doc_id=None):
        self.data = dict(data) if data is not None else None
        self.deleted = False
        self.updates = []
        self.id = doc_id

    @property
    def exists(self) -> bool:
        return (self.data is not None) and (not self.deleted)

    def get(self, *args, transaction=None, **kwargs):
        return _FakeSnapshot(self.data if not self.deleted else None)

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
    from app.services.integration_tokens import compute_raw_credentials_fingerprint

    state_data = {
        "contractor_id": "contractor-1",
        "provider": "google_calendar",
        "created_at": 1000.0,
        "expires_at": 1600.0,
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": compute_raw_credentials_fingerprint(access_token if refresh_token else None, refresh_token if refresh_token else None),
    }
    state = _FakeDocRef(state_data, doc_id="opaque-state-12345678")
    c_data = {
        "contractor_id": "contractor-1",
        "active": True,
        "google_calendar_connected": bool(refresh_token),
        "google_calendar_generation": 0,
        "google_calendar_lifecycle_epoch": 0,
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
    monkeypatch.setattr(settings, "google_calendar_client_id", "test-gcal-client-id")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "test-gcal-client-secret")


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
    db = _FakeFirestore({
        "google_oauth_states": {},
        "contractors": {"contractor-1": _FakeDocRef({"contractor_id": "contractor-1", "active": True})},
    })
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
    monkeypatch.setattr("time.time", lambda: 1_000.0)

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
    monkeypatch.setattr("time.time", lambda: 1_000.0)

    with pytest.raises(HTTPException) as exc:
        await integrations.google_calendar_callback(code="opaque-code", state="opaque-state-12345678")

    assert exc.value.status_code == 502
    assert contractor.data.get("google_calendar_access_token") != "new-access"


@pytest.mark.asyncio
async def test_callback_error_logging_omits_provider_response(monkeypatch, caplog):
    sensitive_payload = "private-provider-callback-detail-do-not-log"
    db, _state, _contractor = _oauth_firestore()
    response = _FakeResponse(400, {}, text=sensitive_payload)
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr("time.time", lambda: 1_000.0)

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
    monkeypatch.setattr("time.time", lambda: 1_000.0)

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
    monkeypatch.setattr("time.time", lambda: 1_000.0)

    with pytest.raises(HTTPException) as exc:
        await integrations.google_calendar_callback(code="opaque-code", state="opaque-state-12345678")

    assert exc.value.status_code == 502
    assert "Google token response invalid" in caplog.text
    assert "result=invalid_type" in caplog.text


@pytest.mark.asyncio
async def test_callback_rejects_reduced_or_malformed_scope(monkeypatch):
    """Callback rejects reduced scope before credential commit and returns HTTP 400."""
    _configure_keys(monkeypatch)
    import app.services.integration_token_mutations as mutations_module

    db, state, contractor = _oauth_firestore()
    # Provider returns reduced scope (only freebusy, missing events)
    response = _FakeResponse(
        200,
        {
            "access_token": "new-access",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/calendar.freebusy",
        },
    )
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr("time.time", lambda: 1_000.0)

    with pytest.raises(HTTPException) as exc:
        await integrations.google_calendar_callback(code="opaque-code", state="opaque-state-12345678")

    assert exc.value.status_code == 400
    assert "scope" in str(exc.value.detail).lower()
    # State document is consumed to prevent reuse
    assert state.deleted is True
    # But contractor credentials are NOT updated
    assert contractor.data.get("google_calendar_access_token") != "new-access"


@pytest.mark.asyncio
async def test_callback_accepts_omitted_scope_using_canonical_default(monkeypatch):
    """Callback accepts omitted scope from provider, defaulting to required scope set."""
    _configure_keys(monkeypatch)
    import app.services.integration_token_mutations as mutations_module

    db, state, contractor = _oauth_firestore()
    # Provider response omits scope field completely
    response = _FakeResponse(
        200,
        {
            "access_token": "new-access",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr("time.time", lambda: 1_000.0)

    await integrations.google_calendar_callback(code="opaque-code", state="opaque-state-12345678")

    assert state.deleted is True
    assert contractor.data["google_calendar_scope"] == integrations.GOOGLE_CALENDAR_SCOPE


class _StrSubclass(str):
    pass


class _HostileObj:
    def __eq__(self, other):
        raise AssertionError("Hostile equality called!")

    def __bool__(self):
        raise AssertionError("Hostile bool called!")

    def __len__(self):
        raise AssertionError("Hostile len called!")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_scope",
    [
        None,
        False,
        0,
        "",
        "   ",
        [],
        ["https://www.googleapis.com/auth/calendar.events"],
        "https://www.googleapis.com/auth/calendar.events\thttps://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.events\nhttps://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.events  https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
        _StrSubclass("https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy"),
    ],
)
async def test_callback_rejects_present_invalid_scope_parameterized(monkeypatch, bad_scope):
    """Callback rejects any present invalid/reduced scope with HTTP 400 and makes zero credential commit."""
    _configure_keys(monkeypatch)
    import app.services.integration_token_mutations as mutations_module

    db, state, contractor = _oauth_firestore()
    response = _FakeResponse(
        200,
        {
            "access_token": "new-access",
            "expires_in": 3600,
            "scope": bad_scope,
        },
    )
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr("time.time", lambda: 1_000.0)

    with pytest.raises(HTTPException) as exc:
        await integrations.google_calendar_callback(code="opaque-code", state="opaque-state-12345678")

    assert exc.value.status_code == 400
    assert "scope" in str(exc.value.detail).lower()
    assert state.deleted is True
    assert contractor.data.get("google_calendar_access_token") != "new-access"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_access",
    [None, 123, "", _HostileObj(), _StrSubclass("valid-looking-token")],
)
async def test_callback_rejects_malformed_access_token(monkeypatch, bad_access):
    """Callback rejects malformed access_token from Google with HTTP 502 and commits zero credentials."""
    _configure_keys(monkeypatch)
    db, state, contractor = _oauth_firestore()
    response = _FakeResponse(
        200,
        {
            "access_token": bad_access,
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))
    monkeypatch.setattr("time.time", lambda: 1_000.0)

    with pytest.raises(HTTPException) as exc:
        await integrations.google_calendar_callback(code="opaque-code", state="opaque-state-12345678")

    assert exc.value.status_code == 502
    assert contractor.data.get("google_calendar_access_token") != "new-access"


@pytest.mark.asyncio
async def test_18j_google_oauth_callback_causal_timeout_and_invalid_json_fencing(monkeypatch, caplog):
    """Causal proof: when Google OAuth token exchange times out or returns invalid JSON after setting
    provider_request_started, the durable started fence remains on the contractor document (HTTP 502).
    For each retry, creating a fresh valid OAuth state for the same contractor is rejected by the
    durable started fence with exact status HTTP 400 before making any second provider HTTP call."""
    import secrets

    import httpx
    _configure_keys(monkeypatch)
    import app.services.integration_token_mutations as mutations_module

    secret_marker_code1 = "secret-code-marker-1111"

    # Part A: Test Timeout failure and retry
    cid1 = "c-18j-gcal-timeout"
    contractor1 = _FakeDocRef({
        "contractor_id": cid1,
        "active": True,
        "google_calendar_connected": False,
        "google_calendar_generation": 0,
        "google_calendar_lifecycle_epoch": 0,
    }, doc_id=cid1)
    db1 = _FakeFirestore({
        "google_oauth_states": {},
        "contractors": {cid1: contractor1},
    })

    state1 = secrets.token_urlsafe(32)
    await mutations_module.create_oauth_state(
        db=db1,
        collection_name="google_oauth_states",
        state=state1,
        contractor_id=cid1,
        provider="google_calendar",
    )

    http_call_count1 = [0]
    class _TimingOutClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            http_call_count1[0] += 1
            raise httpx.TimeoutException("Google OAuth token exchange connection timeout")

    monkeypatch.setattr(integrations, "_get_firestore", lambda: db1)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db1)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _TimingOutClient())

    with pytest.raises(HTTPException) as exc1:
        await integrations.google_calendar_callback(code=secret_marker_code1, state=state1)

    assert exc1.value.status_code == 502
    assert http_call_count1[0] == 1
    assert contractor1.data.get("google_calendar_operation_intent_phase") == "provider_request_started"

    # Create fresh valid OAuth state for the same contractor after state1 was consumed
    state1_retry = secrets.token_urlsafe(32)
    await mutations_module.create_oauth_state(
        db=db1,
        collection_name="google_oauth_states",
        state=state1_retry,
        contractor_id=cid1,
        provider="google_calendar",
    )

    # Fresh callback retry against contractor with started intent is blocked by the fence with exact status 400 before provider HTTP
    with pytest.raises(HTTPException) as exc1_retry:
        await integrations.google_calendar_callback(code="secret-code-retry1", state=state1_retry)

    assert exc1_retry.value.status_code == 400
    assert http_call_count1[0] == 1  # ZERO additional HTTP call!

    # Assert secret markers are not leaked in exception details or log text
    assert secret_marker_code1 not in str(exc1.value.detail)
    assert secret_marker_code1 not in caplog.text
    assert state1 not in str(exc1.value.detail)
    assert state1_retry not in str(exc1_retry.value.detail)

    # Part B: Test Invalid JSON failure and retry
    secret_marker_code2 = "secret-code-marker-2222"
    cid2 = "c-18j-gcal-invalid-json"
    contractor2 = _FakeDocRef({
        "contractor_id": cid2,
        "active": True,
        "google_calendar_connected": False,
        "google_calendar_generation": 0,
        "google_calendar_lifecycle_epoch": 0,
    }, doc_id=cid2)
    db2 = _FakeFirestore({
        "google_oauth_states": {},
        "contractors": {cid2: contractor2},
    })
    state2 = secrets.token_urlsafe(32)
    await mutations_module.create_oauth_state(
        db=db2,
        collection_name="google_oauth_states",
        state=state2,
        contractor_id=cid2,
        provider="google_calendar",
    )

    http_call_count2 = [0]
    class _InvalidJsonClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            http_call_count2[0] += 1
            class _Resp:
                status_code = 200
                def json(self): raise ValueError("Non-JSON HTML error page from Google")
            return _Resp()

    monkeypatch.setattr(integrations, "_get_firestore", lambda: db2)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db2)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _InvalidJsonClient())

    with pytest.raises(HTTPException) as exc2:
        await integrations.google_calendar_callback(code=secret_marker_code2, state=state2)

    assert exc2.value.status_code == 502
    assert http_call_count2[0] == 1
    assert contractor2.data.get("google_calendar_operation_intent_phase") == "provider_request_started"

    # Create fresh valid OAuth state for cid2
    state2_retry = secrets.token_urlsafe(32)
    await mutations_module.create_oauth_state(
        db=db2,
        collection_name="google_oauth_states",
        state=state2_retry,
        contractor_id=cid2,
        provider="google_calendar",
    )

    with pytest.raises(HTTPException) as exc2_retry:
        await integrations.google_calendar_callback(code="secret-code-retry2", state=state2_retry)

    assert exc2_retry.value.status_code == 400
    assert http_call_count2[0] == 1  # ZERO additional HTTP call!

    assert secret_marker_code2 not in str(exc2.value.detail)
    assert secret_marker_code2 not in caplog.text
    assert state2 not in str(exc2.value.detail)
    assert state2_retry not in str(exc2_retry.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_type",
    [
        "success",
        "missing_fresh_refresh_token",
        "pre_dispatch_fail",
        "terminal_400",
        "timeout",
        "http_429",
        "invalid_json",
        "non_dict_json",
        "persistence_fail",
    ],
)
async def test_18q_google_calendar_callback_quarantine_reauth_matrix(monkeypatch, case_type):
    """Causal proof for google_calendar_callback under True/True quarantine reauthorization:
    - success: 1 HTTP, fresh credentials installed, gen+epoch advance, quarantine+attempt removed.
    - missing_fresh_refresh_token: missing fresh refresh token during quarantine recovery raises HTTP 502 with zero credential fallback.
    - pre_dispatch_fail / terminal_400: attempt terminalized, quarantine retained.
    - ambiguity (timeout, 429, invalid/non-dict JSON, persist fail): attempt retained in provider_request_started, quarantine retained, retry blocks before second HTTP.
    """
    import httpx
    from fastapi import HTTPException
    import app.api.integrations as integrations
    import app.services.integration_token_mutations as mutations_module
    from app.services.integration_tokens import compute_raw_credentials_fingerprint, encrypt_integration_token, IntegrationTokenConfigError
    from app.services.integration_token_mutations import IntegrationTokenCASConflict

    _configure_keys(monkeypatch)
    cid = f"c-gcal-cb-qreauth-{case_type}"
    enc_acc = encrypt_integration_token("old-acc", contractor_id=cid, provider="google_calendar", token_kind="access")
    enc_ref = encrypt_integration_token("old-ref", contractor_id=cid, provider="google_calendar", token_kind="refresh")
    state_id = "s" * 32
    fp = compute_raw_credentials_fingerprint(enc_acc, enc_ref)

    c_doc = _FakeDocRef(
        {
            "contractor_id": cid,
            "active": True,
            "google_calendar_connected": True,
            "google_calendar_access_token": enc_acc,
            "google_calendar_refresh_token": enc_ref,
            "google_calendar_generation": 1,
            "google_calendar_lifecycle_epoch": 1,
            "google_calendar_token_envelope_required": True,
            "google_calendar_reauthorization_required": True,
            "google_calendar_refresh_outcome_unknown": True,
        },
        doc_id=cid,
    )
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "google_oauth_states": {},
        "integration_lifecycle_audit": {},
    })
    await mutations_module.create_oauth_state(
        db=db,
        collection_name="google_oauth_states",
        state=state_id,
        contractor_id=cid,
        provider="google_calendar",
    )
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    http_count = [0]

    class _MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            http_count[0] += 1
            if case_type == "success":
                return _FakeResponse(200, {"access_token": "new-acc-tok", "refresh_token": "new-ref-tok", "expires_in": 3600})
            elif case_type == "missing_fresh_refresh_token":
                return _FakeResponse(200, {"access_token": "new-acc-tok", "expires_in": 3600})
            elif case_type == "terminal_400":
                return _FakeResponse(400, {"error": "invalid_grant"})
            elif case_type == "timeout":
                raise httpx.TimeoutException("Timeout")
            elif case_type == "http_429":
                return _FakeResponse(429, {"error": "rate limited"})
            elif case_type == "invalid_json":
                class _BadJsonResp:
                    status_code = 200
                    def json(self): raise ValueError("Not JSON")
                return _BadJsonResp()
            elif case_type == "non_dict_json":
                class _NonDictResp:
                    status_code = 200
                    def json(self): return ["array"]
                return _NonDictResp()
            elif case_type == "persistence_fail":
                return _FakeResponse(200, {"access_token": "new-acc-tok", "refresh_token": "new-ref-tok"})

    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _MockClient())

    if case_type == "pre_dispatch_fail":
        monkeypatch.setattr("app.services.integration_tokens.determine_write_format", lambda **kwargs: (_ for _ in ()).throw(IntegrationTokenConfigError("Unconfigured")))

    if case_type == "persistence_fail":
        async def _fail_connect(*args, **kwargs):
            raise IntegrationTokenCASConflict("Simulated persistence failure")
        monkeypatch.setattr(mutations_module, "connect_provider_cas", _fail_connect)

    if case_type == "success":
        res = await integrations.google_calendar_callback(code="gcal-code", state=state_id)
        assert res.status_code == 200
        assert http_count[0] == 1
        durable = c_doc.data
        assert durable.get("google_calendar_generation") == 2
        assert durable.get("google_calendar_lifecycle_epoch") == 2
        assert "google_calendar_reauthorization_required" not in durable
        assert "google_calendar_refresh_outcome_unknown" not in durable
        assert "google_calendar_reauthorization_attempt_id" not in durable
    elif case_type in ("pre_dispatch_fail", "terminal_400"):
        with pytest.raises(HTTPException):
            await integrations.google_calendar_callback(code="gcal-code", state=state_id)
        durable = c_doc.data
        assert durable.get("google_calendar_reauthorization_required") is True
        assert durable.get("google_calendar_refresh_outcome_unknown") is True
        assert "google_calendar_reauthorization_attempt_id" not in durable
    elif case_type == "missing_fresh_refresh_token":
        with pytest.raises(HTTPException) as exc_info:
            await integrations.google_calendar_callback(code="gcal-code", state=state_id)
        assert exc_info.value.status_code == 502
        assert "Missing fresh refresh token" in exc_info.value.detail
        durable = c_doc.data
        assert durable.get("google_calendar_reauthorization_required") is True
        assert durable.get("google_calendar_refresh_outcome_unknown") is True
    else:
        with pytest.raises(HTTPException):
            await integrations.google_calendar_callback(code="gcal-code", state=state_id)
        durable = c_doc.data
        assert durable.get("google_calendar_reauthorization_required") is True
        assert durable.get("google_calendar_refresh_outcome_unknown") is True
        assert durable.get("google_calendar_reauthorization_attempt_phase") == "provider_request_started"


@pytest.mark.asyncio
async def test_18qc_quarantined_callback_retry_matrix_asserts_zero_second_http(monkeypatch):
    """Prove that after an ambiguous callback outcome (HTTP 500, timeout, bad JSON), retry from started attempt issues 0 second HTTP call."""
    import base64
    import app.api.integrations as integrations
    import app.services.integration_token_mutations as mutations_module
    import app.services.integration_token_mutations as it_mutations
    from app.config import settings
    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}')
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    cid = "test_quarantined_callback_retry_zero_http"
    state_id = "state_retry_" + "c" * 20
    fp = it_mutations.compute_raw_credentials_fingerprint("acc_old", "ref_old")

    # Durable contractor in quarantined_reauthorizing state with started attempt
    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_access_token": "acc_old",
        "google_calendar_refresh_token": "ref_old",
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 1,
        "google_calendar_reauthorization_required": True,
        "google_calendar_refresh_outcome_unknown": True,
        "google_calendar_reauthorization_attempt_id": "attempt-1",
        "google_calendar_reauthorization_attempt_kind": "reconnect",
        "google_calendar_reauthorization_attempt_phase": "provider_request_started",
        "google_calendar_reauthorization_attempt_expires_at": time.time() + 300.0,
        "google_calendar_reauthorization_attempt_acquired_at": time.time(),
        "google_calendar_reauthorization_attempt_generation": 1,
        "google_calendar_reauthorization_attempt_lifecycle_epoch": 1,
        "google_calendar_reauthorization_attempt_credentials_fingerprint": fp,
    }, doc_id=cid)
    s_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "google_calendar",
        "lifecycle_epoch": 1,
        "generation": 1,
        "credentials_fingerprint": fp,
        "created_at": time.time(),
        "expires_at": time.time() + 600.0,
    }, doc_id=state_id)
    db = _FakeFirestore({"contractors": {cid: c_doc}, "google_oauth_states": {state_id: s_doc}})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    http_count = [0]
    class _FailingRetryClient:
        async def post(self, *args, **kwargs):
            http_count[0] += 1
            raise AssertionError("HTTP call strictly forbidden for retry under started attempt!")

    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda: _FailingRetryClient())

    # Callback attempt under provider_request_started phase must block with 409 and make ZERO HTTP calls
    with pytest.raises(HTTPException) as exc_info:
        await integrations.google_calendar_callback(code="gcal-code", state=state_id)

    assert exc_info.value.status_code in (400, 409)
    assert http_count[0] == 0


@pytest.mark.asyncio
async def test_18qc_google_refresh_ambiguity_matrix(monkeypatch):
    """Prove Google Calendar token refresh ambiguous HTTP responses (408, 425, 429, 599, non-dict) transition to True/True quarantine with 0 retry HTTP."""
    import base64
    import app.api.integrations as integrations
    import app.services.calendar as calendar_service
    import app.services.integration_token_mutations as mutations_module
    import app.services.integration_token_mutations as it_mutations
    from app.config import settings
    dummy_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setattr(settings, "integration_token_encryption_keys", f'{{"1": "{dummy_key}"}}')
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    cid = "test_google_refresh_ambiguity"
    stored_acc = "acc_old_gcal"
    stored_ref = "ref_old_gcal"
    fp = it_mutations.compute_raw_credentials_fingerprint(stored_acc, stored_ref)

    for status_code in (408, 425, 429, 599, "non_dict_response"):
        c_doc = _FakeDocRef({
            "contractor_id": cid,
            "active": True,
            "google_calendar_connected": True,
            "google_calendar_access_token": stored_acc,
            "google_calendar_refresh_token": stored_ref,
            "google_calendar_generation": 1,
            "google_calendar_lifecycle_epoch": 1,
        }, doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})
        monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)
        monkeypatch.setattr(settings, "google_calendar_client_id", "fake-cid")
        monkeypatch.setattr(settings, "google_calendar_client_secret", "fake-sec")

        http_calls = [0]
        class _AmbiguousResp:
            def __init__(self):
                if status_code == "non_dict_response":
                    self.status_code = 200
                else:
                    self.status_code = status_code
            def json(self):
                if status_code == "non_dict_response":
                    return "not a dict string response"
                return {"error": "ambiguous"}

        class _AmbiguousClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, *args, **kwargs):
                http_calls[0] += 1
                return _AmbiguousResp()

        monkeypatch.setattr("httpx.AsyncClient", _AmbiguousClient)

        contractor = dict(c_doc.data)
        res = await calendar_service.refresh_access_token(contractor, force=True)
        assert res is None
        assert http_calls[0] == 1

        durable = c_doc.data
        assert durable.get("google_calendar_reauthorization_required") is True
        assert durable.get("google_calendar_refresh_outcome_unknown") is True
