"""Comprehensive unit and security tests for Jobber OAuth 2.0 PKCE and Marketplace handoff."""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import logging
import math
import os
import re
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

import app.api.integrations as integrations
import app.services.integration_token_mutations as mutations_module
import app.services.oauth_pkce as oauth_pkce
from app.config import settings
from app.services.integration_tokens import compute_raw_credentials_fingerprint

# Capture original HTTPX classes before any test-level monkeypatching
_CapturedAsyncClient = httpx.AsyncClient
_CapturedASGITransport = httpx.ASGITransport


# ─────────────────────────────────────────────────────────────────────────────
# Test Fakes & App Fixtures
# ─────────────────────────────────────────────────────────────────────────────

class _FakeDocRef:
    def __init__(self, data=None, doc_id=None):
        self.id = doc_id
        self.data = dict(data) if data is not None else None
        self.deleted = False
        self.updates = []

    @property
    def exists(self) -> bool:
        return (self.data is not None) and (not self.deleted)

    def get(self, *args, transaction=None, **kwargs):
        class _Snap:
            def __init__(self, d, deleted):
                self._d = dict(d) if d is not None else None
                self.exists = (d is not None) and (not deleted)
                self.read_time = datetime.datetime.fromtimestamp(time.time(), datetime.UTC)

            def to_dict(self):
                return dict(self._d) if self.exists else {}

        return _Snap(self.data, self.deleted)

    def update(self, updates, *args, **kwargs):
        if self.data is None:
            self.data = {}
        self.updates.append(dict(updates))
        for k, v in updates.items():
            if str(type(v).__name__) == "Sentinel" or "DELETE" in str(v):
                self.data.pop(k, None)
            else:
                self.data[k] = v

    def set(self, data, *args, **kwargs):
        self.data = dict(data)
        self.deleted = False

    def delete(self, *args, **kwargs):
        self.deleted = True
        self.data = None


class _FakeTransaction:
    def __init__(self, db):
        self._db = db
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
            raise RuntimeError("Firestore transaction read-after-write violation: all reads must occur before writes")
        return doc_ref.get()

    def update(self, doc_ref, updates):
        self._staged_updates.append((doc_ref, dict(updates)))

    def set(self, doc_ref, data):
        self._staged_sets.append((doc_ref, dict(data)))

    def create(self, doc_ref, data):
        if doc_ref.exists:
            raise RuntimeError("Document already exists")
        self._staged_sets.append((doc_ref, dict(data)))

    def delete(self, doc_ref):
        self._staged_deletes.append(doc_ref)

    def commit(self):
        for doc_ref, data in self._staged_sets:
            doc_ref.set(data)
        for doc_ref, updates in self._staged_updates:
            doc_ref.update(updates)
        for doc_ref in self._staged_deletes:
            doc_ref.delete()
        self.committed = True

    def _begin(self, *args, **kwargs): pass
    def _clean_up(self): pass
    def _rollback(self):
        self._staged_sets.clear()
        self._staged_updates.clear()
        self._staged_deletes.clear()
    def _commit(self):
        self.commit()
        return []


class _FakeFirestore:
    def __init__(self, collections=None):
        self.collections = collections or {}
        self.last_transaction = None

    def collection(self, name):
        class _Coll:
            def __init__(self, docs):
                self.docs = docs

            def document(self, doc_id):
                if doc_id not in self.docs:
                    self.docs[doc_id] = _FakeDocRef(None, doc_id=doc_id)
                return self.docs[doc_id]

        return _Coll(self.collections.setdefault(name, {}))

    def transaction(self):
        tx = _FakeTransaction(self)
        self.last_transaction = tx
        return tx


def _make_key_b64(byte_val: bytes = b"k") -> str:
    return base64.b64encode(byte_val * 32).decode("ascii")


def _setup_test_env(monkeypatch):
    dummy_key = _make_key_b64(b"1")
    monkeypatch.setattr(settings, "integration_token_encryption_keys", json.dumps({"1": dummy_key}))
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", True)
    monkeypatch.setattr(settings, "jobber_client_id", "test-jobber-client-id")
    monkeypatch.setattr(settings, "jobber_client_secret", "test-jobber-client-secret")
    monkeypatch.setattr(settings, "google_calendar_client_id", "test-gcal-client-id")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "test-gcal-client-secret")
    monkeypatch.setattr(settings, "api_bearer_token", "test-api-bearer-secret")


def _make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(integrations.router)
    return app


@pytest.fixture
def test_app() -> FastAPI:
    return _make_test_app()


@pytest.fixture
async def asgi_client(test_app: FastAPI):
    transport = _CapturedASGITransport(app=test_app)
    async with _CapturedAsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _make_jobber_provider_mock_transport(
    *,
    token_status: int = 200,
    token_json: dict | None = None,
    account_status: int = 200,
    account_json: dict | None = None,
    on_token_request=None,
    on_account_request=None,
    redirect_location: str | None = None,
):
    recorded_requests: list[httpx.Request] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        url_str = str(request.url)

        if redirect_location and "oauth/token" in url_str:
            if on_token_request:
                if asyncio.iscoroutinefunction(on_token_request):
                    await on_token_request(request)
                else:
                    on_token_request(request)
            return httpx.Response(307, headers={"Location": redirect_location})

        if "oauth/token" in url_str:
            if on_token_request:
                if asyncio.iscoroutinefunction(on_token_request):
                    await on_token_request(request)
                else:
                    on_token_request(request)
            if token_status != 200:
                return httpx.Response(token_status, json=token_json or {"error": "invalid_grant"})
            default_token_body = {
                "access_token": "fresh_jobber_access_tok",
                "refresh_token": "fresh_jobber_refresh_tok",
                "expires_in": 3600,
            }
            return httpx.Response(200, json=token_json if token_json is not None else default_token_body)

        if "graphql" in url_str or "api.getjobber.com" in url_str:
            if on_account_request:
                if asyncio.iscoroutinefunction(on_account_request):
                    await on_account_request(request)
                else:
                    on_account_request(request)
            if account_status != 200:
                return httpx.Response(account_status, json=account_json or {"error": "identity_failed"})
            default_acc_body = {
                "data": {
                    "account": {
                        "id": "acc_verified_123"
                    }
                }
            }
            return httpx.Response(200, json=account_json if account_json is not None else default_acc_body)

        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(_handler)
    return transport, recorded_requests


# ─────────────────────────────────────────────────────────────────────────────
# 1. RFC 7636 and PKCE Helper Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_rfc7636_appendix_b_known_test_vector():
    """Prove RFC 7636 Appendix B test vector yields exact expected S256 challenge."""
    rfc_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected_challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    validated = oauth_pkce.validate_pkce_code_verifier(rfc_verifier)
    assert validated == rfc_verifier

    derived = oauth_pkce.derive_pkce_code_challenge(rfc_verifier)
    assert derived == expected_challenge
    assert "=" not in derived  # Unpadded base64url


def test_pkce_verifier_generation_entropy_and_uniqueness():
    """Prove generate_pkce_code_verifier produces distinct 86-character unreserved strings."""
    verifiers = set()
    for _ in range(100):
        v = oauth_pkce.generate_pkce_code_verifier()
        assert isinstance(v, str)
        assert len(v) == 86
        assert oauth_pkce.validate_pkce_code_verifier(v) == v
        verifiers.add(v)
    assert len(verifiers) == 100


def test_pkce_verifier_exact_length_boundaries():
    """Prove strict 43..128 length boundaries on code verifiers."""
    # 42 chars -> too short
    too_short = "a" * 42
    with pytest.raises(oauth_pkce.PkceError):
        oauth_pkce.validate_pkce_code_verifier(too_short)

    # 43 chars -> minimum allowed length
    min_len = "a" * 43
    assert oauth_pkce.validate_pkce_code_verifier(min_len) == min_len

    # 128 chars -> maximum allowed length
    max_len = "a" * 128
    assert oauth_pkce.validate_pkce_code_verifier(max_len) == max_len

    # 129 chars -> too long
    too_long = "a" * 129
    with pytest.raises(oauth_pkce.PkceError):
        oauth_pkce.validate_pkce_code_verifier(too_long)


def test_pkce_verifier_character_set_strictness():
    """Prove only RFC 7636 unreserved characters (ALPHA / DIGIT / '-' / '.' / '_' / '~') are accepted."""
    # All allowed characters
    allowed_sample = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
    assert len(allowed_sample) >= 43
    assert oauth_pkce.validate_pkce_code_verifier(allowed_sample) == allowed_sample

    # Disallowed characters fail closed
    disallowed_cases = [
        "a" * 42 + " ",      # space
        "a" * 42 + "+",      # base64 plus
        "a" * 42 + "/",      # base64 slash
        "a" * 42 + "=",      # base64 pad
        "a" * 42 + "%",      # urlencode marker
        "a" * 42 + "&",      # query separator
        "a" * 42 + "?",      # query marker
        "a" * 42 + "\n",     # newline
        "a" * 42 + "\t",     # tab
        "a" * 42 + "\x00",   # null byte
        "a" * 42 + "\x7f",   # DEL
        "a" * 42 + "é",      # non-ASCII unicode
        "a" * 42 + "🔥",     # emoji
        " " + "a" * 43,      # leading whitespace
        "a" * 43 + " ",      # trailing whitespace
    ]
    for bad in disallowed_cases:
        with pytest.raises(oauth_pkce.PkceError):
            oauth_pkce.validate_pkce_code_verifier(bad)


def test_pkce_verifier_type_safety():
    """Prove non-str types and str subclasses fail closed without leaking values."""
    class _StrSub(str):
        pass

    invalid_types = [
        None,
        12345,
        True,
        False,
        b"a" * 43,
        ["a" * 43],
        {"verifier": "a" * 43},
        _StrSub("a" * 43),
    ]
    for val in invalid_types:
        with pytest.raises(oauth_pkce.PkceError):
            oauth_pkce.validate_pkce_code_verifier(val)


def test_pkce_value_free_error_messages():
    """Prove PKCE errors never expose input verifiers or secrets."""
    secret_sentinel = "SECRET_SENSITIVE_BEARER_PAYLOAD_99999"
    with pytest.raises(oauth_pkce.PkceError) as exc_info:
        oauth_pkce.validate_pkce_code_verifier(secret_sentinel + "!")
    assert secret_sentinel not in str(exc_info.value)

    with pytest.raises(oauth_pkce.PkceError) as exc_info_derive:
        oauth_pkce.derive_pkce_code_challenge(secret_sentinel + "!")
    assert secret_sentinel not in str(exc_info_derive.value)

# ─────────────────────────────────────────────────────────────────────────────
# 2. Authenticated Connect Orchestration
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_jobber_connect_persists_pkce_and_matches_authorize_url_challenge(
    monkeypatch,
    test_app,
    asgi_client,
    caplog,
):
    """Prove authenticated ASGI connect persists verifier atomically and builds matching S256 challenge in URL.
    
    Excludes raw verifier, client secret, and contractor ID from response body, headers, and logs.
    Also verifies unauthenticated (no-bearer) returns 401/403 and other-contractor returns 403 before storage.
    """
    _setup_test_env(monkeypatch)
    cid = "cid_connect_pkce_test"

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    # 1. Establish fictional authorized request.state via dependency override only
    async def _auth_override(request: Request):
        request.state.contractor_id = cid
        request.state.is_admin = False

    test_app.dependency_overrides[integrations.verify_api_token] = _auth_override

    with caplog.at_level(logging.DEBUG):
        resp = await asgi_client.get(f"/api/integrations/jobber/connect?contractor_id={cid}")

    assert resp.status_code == 200
    resp_data = resp.json()
    assert "authorize_url" in resp_data
    auth_url = resp_data["authorize_url"]
    parsed_url = urlparse(auth_url)
    params = parse_qs(parsed_url.query)

    assert "state" in params
    state = params["state"][0]
    assert "code_challenge" in params
    url_challenge = params["code_challenge"][0]
    assert params["code_challenge_method"] == ["S256"]
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["test-jobber-client-id"]

    # Verify Firestore state persistence
    state_doc = db.collection("jobber_oauth_states").document(state).get().to_dict()
    assert state_doc is not None
    assert set(state_doc.keys()) == mutations_module.JOBBER_OAUTH_STATE_KEYS
    assert state_doc["contractor_id"] == cid
    assert state_doc["provider"] == "jobber"
    assert state_doc["pkce_method"] == "S256"

    persisted_verifier = state_doc["pkce_code_verifier"]
    assert len(persisted_verifier) == 86
    # Mathematical proof: URL challenge is the exact S256 hash of the persisted verifier
    expected_challenge = oauth_pkce.derive_pkce_code_challenge(persisted_verifier)
    assert url_challenge == expected_challenge

    # Strict exclusions: raw verifier, client secret, and contractor ID must NOT be in URL query, response body/headers, or logs
    assert cid not in auth_url
    assert "code_verifier" not in params
    assert persisted_verifier not in resp.text
    assert persisted_verifier not in str(resp.headers)
    assert settings.jobber_client_secret not in resp.text
    assert settings.jobber_client_secret not in str(resp.headers)
    assert cid not in resp.text
    assert cid not in str(resp.headers)

    app_logs = [rec for rec in caplog.records if not rec.name.startswith("httpx")]
    for rec in app_logs:
        assert persisted_verifier not in rec.message
        assert settings.jobber_client_secret not in rec.message
        assert cid not in rec.message

    # 2. Actual no-bearer ASGI request returns 401/403 before storage
    test_app.dependency_overrides.clear()
    monkeypatch.setattr(integrations, "_get_firestore", lambda: (_ for _ in ()).throw(AssertionError("DB called on unauthenticated request!")))
    resp_unauth = await asgi_client.get(f"/api/integrations/jobber/connect?contractor_id={cid}")
    assert resp_unauth.status_code in (401, 403)

    # 3. Authenticated other-contractor request returns 403 before storage
    async def _auth_other_contractor(request: Request):
        request.state.contractor_id = "other_contractor_999"
        request.state.is_admin = False

    test_app.dependency_overrides[integrations.verify_api_token] = _auth_other_contractor
    resp_forbidden = await asgi_client.get(f"/api/integrations/jobber/connect?contractor_id={cid}")
    assert resp_forbidden.status_code == 403


@pytest.mark.asyncio
async def test_jobber_connect_attempts_use_independent_verifiers_and_challenges(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove consecutive jobber_connect calls use independent verifiers and challenges."""
    _setup_test_env(monkeypatch)
    cid = "cid_connect_unique_test"

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    async def _auth_override(request: Request):
        request.state.contractor_id = cid
        request.state.is_admin = False

    test_app.dependency_overrides[integrations.verify_api_token] = _auth_override

    resp1 = await asgi_client.get(f"/api/integrations/jobber/connect?contractor_id={cid}")
    resp2 = await asgi_client.get(f"/api/integrations/jobber/connect?contractor_id={cid}")

    params1 = parse_qs(urlparse(resp1.json()["authorize_url"]).query)
    params2 = parse_qs(urlparse(resp2.json()["authorize_url"]).query)

    state1 = params1["state"][0]
    state2 = params2["state"][0]
    assert state1 != state2

    chal1 = params1["code_challenge"][0]
    chal2 = params2["code_challenge"][0]
    assert chal1 != chal2

    doc1 = db.collection("jobber_oauth_states").document(state1).get().to_dict()
    doc2 = db.collection("jobber_oauth_states").document(state2).get().to_dict()
    assert doc1["pkce_code_verifier"] != doc2["pkce_code_verifier"]


@pytest.mark.asyncio
async def test_jobber_connect_requires_authenticated_caller(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove jobber_connect enforces require_contractor_access via ASGI."""
    _setup_test_env(monkeypatch)

    async def _auth_override(request: Request):
        request.state.contractor_id = "other_contractor"
        request.state.is_admin = False

    test_app.dependency_overrides[integrations.verify_api_token] = _auth_override

    resp = await asgi_client.get("/api/integrations/jobber/connect?contractor_id=cid_target")
    assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 3. Callback Transport, PKCE Exchange & Security Headers
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_jobber_callback_sends_consumed_verifier_and_server_secrets(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove jobber_callback sends consumed PKCE verifier, client secret, 10s timeout, and returns security headers."""
    _setup_test_env(monkeypatch)
    cid = "cid_callback_success"
    state_id = "state_cb_success_" + "1" * 16
    test_verifier = oauth_pkce.generate_pkce_code_verifier()

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)
    s_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": compute_raw_credentials_fingerprint(None, None),
        "created_at": time.time(),
        "expires_at": time.time() + 600.0,
        "pkce_method": "S256",
        "pkce_code_verifier": test_verifier,
    }, doc_id=state_id)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {state_id: s_doc},
        "integration_lifecycle_audit": {},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    transport, recorded_requests = _make_jobber_provider_mock_transport()
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda **kw: _CapturedAsyncClient(transport=transport, **kw))
    monkeypatch.setattr("app.services.jobber.httpx.AsyncClient", lambda **kw: _CapturedAsyncClient(transport=transport, **kw))

    resp = await asgi_client.get(f"/api/integrations/jobber/callback?code=auth_code_123&state={state_id}")

    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"

    # Verify state document was consumed and deleted
    assert s_doc.deleted is True

    # Verify token exchange payload
    assert len(recorded_requests) == 2
    token_req = recorded_requests[0]
    assert token_req.url == integrations.JOBBER_TOKEN_URL
    token_form = parse_qs(token_req.content.decode("utf-8"))
    assert token_form["grant_type"] == ["authorization_code"]
    assert token_form["code"] == ["auth_code_123"]
    assert token_form["client_id"] == ["test-jobber-client-id"]
    assert token_form["client_secret"] == ["test-jobber-client-secret"]
    assert token_form["redirect_uri"] == [integrations.JOBBER_REDIRECT_URI]
    assert token_form["code_verifier"] == [test_verifier]

    # Verify contractor document was connected with encrypted credentials
    durable_contractor = c_doc.get().to_dict()
    assert durable_contractor["jobber_connected"] is True
    assert durable_contractor["jobber_generation"] == 1
    assert durable_contractor["jobber_lifecycle_epoch"] == 1
    assert durable_contractor["jobber_account_id"] == "acc_verified_123"


@pytest.mark.asyncio
async def test_jobber_callback_overlapping_requests_fenced_and_single_exchange(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove overlapping ASGI callbacks on one state fence properly with exactly one token exchange and identity lookup."""
    _setup_test_env(monkeypatch)
    cid = "cid_overlap_fence"
    state_id = "state_overlap_" + "1" * 16
    test_verifier = oauth_pkce.generate_pkce_code_verifier()

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)
    s_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": compute_raw_credentials_fingerprint(None, None),
        "created_at": time.time(),
        "expires_at": time.time() + 600.0,
        "pkce_method": "S256",
        "pkce_code_verifier": test_verifier,
    }, doc_id=state_id)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {state_id: s_doc},
        "integration_lifecycle_audit": {},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    req1_at_token = asyncio.Event()
    req1_continue = asyncio.Event()

    token_exchange_count = 0
    account_lookup_count = 0

    async def _on_token(request):
        nonlocal token_exchange_count
        token_exchange_count += 1
        req1_at_token.set()
        await asyncio.wait_for(req1_continue.wait(), timeout=5.0)

    async def _on_account(request):
        nonlocal account_lookup_count
        account_lookup_count += 1

    transport, recorded_requests = _make_jobber_provider_mock_transport(
        on_token_request=_on_token,
        on_account_request=_on_account,
    )
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda **kw: _CapturedAsyncClient(transport=transport, **kw))
    monkeypatch.setattr("app.services.jobber.httpx.AsyncClient", lambda **kw: _CapturedAsyncClient(transport=transport, **kw))

    task1 = None
    try:
        # 1. Launch callback 1
        task1 = asyncio.create_task(
            asgi_client.get(f"/api/integrations/jobber/callback?code=code_first&state={state_id}")
        )

        # 2. Wait for callback 1 to consume state and reach token endpoint
        await asyncio.wait_for(req1_at_token.wait(), timeout=5.0)
        assert token_exchange_count == 1
        assert s_doc.deleted is True  # State was durably consumed

        # 3. While callback 1 is waiting at token endpoint, dispatch callback 2 with same state
        resp2 = await asgi_client.get(f"/api/integrations/jobber/callback?code=code_second&state={state_id}")
        assert resp2.status_code == 400
        assert token_exchange_count == 1  # No new provider calls!
        assert account_lookup_count == 0

        # 4. Release callback 1
        req1_continue.set()

        # 5. Await callback 1 success
        resp1 = await asyncio.wait_for(task1, timeout=5.0)
        assert resp1.status_code == 200

        # 6. Verify total provider interactions
        assert token_exchange_count == 1
        assert account_lookup_count == 1

        # 7. Verify contractor document updated exactly once
        durable_c = c_doc.get().to_dict()
        assert durable_c["jobber_connected"] is True
        assert durable_c["jobber_generation"] == 1
        assert durable_c["jobber_lifecycle_epoch"] == 1
        assert durable_c["jobber_account_id"] == "acc_verified_123"

    finally:
        req1_continue.set()
        if task1 and not task1.done():
            task1.cancel()
            try:
                await task1
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "durable_mutation",
    [
        {"jobber_generation": 1},
        {"jobber_lifecycle_epoch": 1},
        {
            "jobber_generation": 1,
            "jobber_lifecycle_epoch": 1,
            "jobber_access_token": "replacement_access_token",
            "jobber_refresh_token": "replacement_refresh_token",
            "jobber_account_id": "acc_replacement_999",
            "jobber_connected": True,
        },
    ],
)
async def test_jobber_callback_cas_conflict_on_concurrent_lifecycle_mutation(
    monkeypatch,
    test_app,
    asgi_client,
    durable_mutation,
):
    """Prove CAS aborts and preserves committed replacement fields when generation or epoch changes during exchange."""
    _setup_test_env(monkeypatch)
    cid = "cid_cas_conflict_test"
    state_id = "state_cas_conflict_" + "1" * 16
    test_verifier = oauth_pkce.generate_pkce_code_verifier()

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)
    s_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": compute_raw_credentials_fingerprint(None, None),
        "created_at": time.time(),
        "expires_at": time.time() + 600.0,
        "pkce_method": "S256",
        "pkce_code_verifier": test_verifier,
    }, doc_id=state_id)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {state_id: s_doc},
        "integration_lifecycle_audit": {},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    req_at_token = asyncio.Event()
    req_continue = asyncio.Event()

    async def _on_token(request):
        req_at_token.set()
        await asyncio.wait_for(req_continue.wait(), timeout=5.0)

    transport, _ = _make_jobber_provider_mock_transport(
        on_token_request=_on_token,
    )
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda **kw: _CapturedAsyncClient(transport=transport, **kw))
    monkeypatch.setattr("app.services.jobber.httpx.AsyncClient", lambda **kw: _CapturedAsyncClient(transport=transport, **kw))

    task = None
    try:
        # 1. Start callback request
        task = asyncio.create_task(
            asgi_client.get(f"/api/integrations/jobber/callback?code=valid_code&state={state_id}")
        )

        # 2. Wait until callback reaches token exchange
        await asyncio.wait_for(req_at_token.wait(), timeout=5.0)

        # 3. Mutate only fictional durable fields on contractor to simulate a committed replacement
        c_doc.data.update(durable_mutation)
        expected_snapshot_after_mutation = dict(c_doc.data)

        # 4. Release callback request to attempt CAS commit
        req_continue.set()

        # 5. Callback must fail due to CAS conflict
        resp = await asyncio.wait_for(task, timeout=5.0)
        assert resp.status_code == 500

        # 6. Verify contractor document was NOT overwritten with callback's credentials
        durable_after = c_doc.get().to_dict()
        for k, expected_v in expected_snapshot_after_mutation.items():
            assert durable_after.get(k) == expected_v
        assert durable_after.get("jobber_access_token") != "fresh_jobber_access_tok"

    finally:
        req_continue.set()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_jobber_callback_token_endpoint_redirect_rejected_with_502(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove 307 redirect from token endpoint fails closed with 502, no redirected request, and verifies form fields."""
    _setup_test_env(monkeypatch)
    cid = "cid_redirect_test"
    state_id = "state_redirect_" + "1" * 16
    test_verifier = oauth_pkce.generate_pkce_code_verifier()

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)
    s_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": compute_raw_credentials_fingerprint(None, None),
        "created_at": time.time(),
        "expires_at": time.time() + 600.0,
        "pkce_method": "S256",
        "pkce_code_verifier": test_verifier,
    }, doc_id=state_id)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {state_id: s_doc},
        "integration_lifecycle_audit": {},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    sentinel_redirect_origin = "https://evil.sentinel.origin/steal-tokens"
    transport, recorded_requests = _make_jobber_provider_mock_transport(
        redirect_location=sentinel_redirect_origin,
    )

    # Configure client factory with follow_redirects=True by default to test explicit follow_redirects=False override
    def _client_factory(**kwargs):
        fr = kwargs.get("follow_redirects", True)
        return _CapturedAsyncClient(transport=transport, follow_redirects=fr)

    monkeypatch.setattr(integrations.httpx, "AsyncClient", _client_factory)
    monkeypatch.setattr("app.services.jobber.httpx.AsyncClient", _client_factory)

    resp = await asgi_client.get(f"/api/integrations/jobber/callback?code=auth_code_123&state={state_id}")

    # 1. Generic 502 returned
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Failed to exchange code with Jobber"

    # 2. Exactly one transport request made (zero requests to redirected origin)
    assert len(recorded_requests) == 1
    req = recorded_requests[0]
    assert req.url == integrations.JOBBER_TOKEN_URL
    assert "evil.sentinel.origin" not in str(req.url)

    # 3. Started intent is retained on contractor (provider ambiguity on non-400 failure)
    durable_c = c_doc.get().to_dict()
    assert durable_c.get("jobber_connected") is False
    assert durable_c.get("jobber_operation_intent_phase") == "provider_request_started"

    # 4. Verify submitted form content
    body_str = req.content.decode("utf-8")
    form_params = parse_qs(body_str)
    assert form_params["grant_type"] == ["authorization_code"]
    assert form_params["code"] == ["auth_code_123"]
    assert form_params["client_id"] == ["test-jobber-client-id"]
    assert form_params["client_secret"] == ["test-jobber-client-secret"]
    assert form_params["redirect_uri"] == [integrations.JOBBER_REDIRECT_URI]
    assert form_params["code_verifier"] == [test_verifier]

    # 5. Client secret and verifier must NOT be in URL query or headers
    assert "test-jobber-client-secret" not in str(req.url)
    assert test_verifier not in str(req.url)
    for _, h_val in req.headers.items():
        assert "test-jobber-client-secret" not in h_val
        assert test_verifier not in h_val


# ─────────────────────────────────────────────────────────────────────────────
# 4. Schema Gating & Zero-HTTP Fences on Malformed / Legacy States
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_jobber_callback_rejects_pre_pkce_seven_field_schema_with_zero_http(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove old 7-field Jobber states fail closed with HTTP 400, deleted from Firestore, zero HTTP, zero claim."""
    _setup_test_env(monkeypatch)
    cid = "cid_legacy_schema_rejection"
    state_id = "state_legacy_7field_" + "1" * 16

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)
    # Old 7-field state (missing pkce_method and pkce_code_verifier)
    s_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": compute_raw_credentials_fingerprint(None, None),
        "created_at": time.time(),
        "expires_at": time.time() + 600.0,
    }, doc_id=state_id)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {state_id: s_doc},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    def _must_not_call_provider(*args, **kwargs):
        raise AssertionError("HTTP must not be called for legacy state!")

    monkeypatch.setattr(integrations.httpx, "AsyncClient", _must_not_call_provider)
    monkeypatch.setattr("app.services.jobber.httpx.AsyncClient", _must_not_call_provider)

    resp = await asgi_client.get(f"/api/integrations/jobber/callback?code=valid_code&state={state_id}")

    assert resp.status_code == 400
    assert "Location" not in resp.headers
    # Proves state document is deleted on failure to prevent replay
    assert s_doc.deleted is True
    # Proves contractor doc has zero intent / claim reserved
    assert "jobber_operation_intent_id" not in c_doc.get().to_dict()
    assert "jobber_operation_intent_phase" not in c_doc.get().to_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_state_fields",
    [
        # Invalid pkce_method
        {"pkce_method": "plain", "pkce_code_verifier": "a" * 43},
        {"pkce_method": "s256", "pkce_code_verifier": "a" * 43},  # lowercase
        {"pkce_method": None, "pkce_code_verifier": "a" * 43},
        {"pkce_method": 123, "pkce_code_verifier": "a" * 43},
        # Malformed pkce_code_verifier
        {"pkce_method": "S256", "pkce_code_verifier": "a" * 42},   # 42 chars (too short)
        {"pkce_method": "S256", "pkce_code_verifier": "a" * 129},  # 129 chars (too long)
        {"pkce_method": "S256", "pkce_code_verifier": "a" * 42 + " "},  # whitespace
        {"pkce_method": "S256", "pkce_code_verifier": "a" * 42 + "+"},  # invalid char
        {"pkce_method": "S256", "pkce_code_verifier": None},
        {"pkce_method": "S256", "pkce_code_verifier": 12345},
        # Missing pkce_code_verifier
        {"pkce_method": "S256"},
        # Extra unexpected key
        {"pkce_method": "S256", "pkce_code_verifier": "a" * 43, "extra_key": "injected"},
    ],
)
async def test_jobber_callback_rejects_malformed_pkce_schema_with_zero_http(
    monkeypatch,
    test_app,
    asgi_client,
    bad_state_fields,
):
    """Prove any Jobber state schema deviation fails closed with HTTP 400, deletes state, and makes zero HTTP."""
    _setup_test_env(monkeypatch)
    cid = "cid_malformed_pkce"
    state_id = "state_malformed_" + "1" * 16

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)

    state_dict = {
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": compute_raw_credentials_fingerprint(None, None),
        "created_at": time.time(),
        "expires_at": time.time() + 600.0,
    }
    state_dict.update(bad_state_fields)

    s_doc = _FakeDocRef(state_dict, doc_id=state_id)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {state_id: s_doc},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    def _must_not_call_provider(*args, **kwargs):
        raise AssertionError("HTTP called on malformed state!")

    monkeypatch.setattr(integrations.httpx, "AsyncClient", _must_not_call_provider)
    monkeypatch.setattr("app.services.jobber.httpx.AsyncClient", _must_not_call_provider)

    resp = await asgi_client.get(f"/api/integrations/jobber/callback?code=valid_code&state={state_id}")

    assert resp.status_code == 400
    assert "Location" not in resp.headers
    assert s_doc.deleted is True
    assert "jobber_operation_intent_id" not in c_doc.get().to_dict()


@pytest.mark.asyncio
async def test_jobber_callback_canonical_nonexistent_state_fails_closed_400(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove canonical nonexistent state fails closed with HTTP 400, never redirects, and makes zero HTTP."""
    _setup_test_env(monkeypatch)
    cid = "cid_nonexistent_state"
    state_id = "state_nonexistent_" + "1" * 16

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    def _must_not_call_provider(*args, **kwargs):
        raise AssertionError("HTTP called on nonexistent state!")

    monkeypatch.setattr(integrations.httpx, "AsyncClient", _must_not_call_provider)
    monkeypatch.setattr("app.services.jobber.httpx.AsyncClient", _must_not_call_provider)

    resp = await asgi_client.get(f"/api/integrations/jobber/callback?code=valid_code&state={state_id}")

    assert resp.status_code == 400
    assert "Location" not in resp.headers
    assert "jobber_operation_intent_id" not in c_doc.get().to_dict()


@pytest.mark.asyncio
async def test_jobber_callback_expired_valid_schema_state_deletes_row_and_fails_closed_400(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove expired valid-schema state is deleted from DB, fails closed with HTTP 400, and makes zero HTTP."""
    _setup_test_env(monkeypatch)
    cid = "cid_expired_state"
    state_id = "state_expired_" + "1" * 16
    test_verifier = oauth_pkce.generate_pkce_code_verifier()

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)
    s_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": compute_raw_credentials_fingerprint(None, None),
        "created_at": time.time() - 700.0,
        "expires_at": time.time() - 100.0,
        "pkce_method": "S256",
        "pkce_code_verifier": test_verifier,
    }, doc_id=state_id)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {state_id: s_doc},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    def _must_not_call_provider(*args, **kwargs):
        raise AssertionError("HTTP called on expired state!")

    monkeypatch.setattr(integrations.httpx, "AsyncClient", _must_not_call_provider)
    monkeypatch.setattr("app.services.jobber.httpx.AsyncClient", _must_not_call_provider)

    resp = await asgi_client.get(f"/api/integrations/jobber/callback?code=valid_code&state={state_id}")

    assert resp.status_code == 400
    assert "Location" not in resp.headers
    assert s_doc.deleted is True
    assert "jobber_operation_intent_id" not in c_doc.get().to_dict()


@pytest.mark.asyncio
async def test_jobber_state_replay_fails_closed_and_makes_zero_http(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove consuming an already-deleted or replayed Jobber state fails with HTTP 400 and exactly one exchange overall."""
    _setup_test_env(monkeypatch)
    cid = "cid_replay_test"
    state_id = "state_replay_" + "1" * 16

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)
    s_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": compute_raw_credentials_fingerprint(None, None),
        "created_at": time.time(),
        "expires_at": time.time() + 600.0,
        "pkce_method": "S256",
        "pkce_code_verifier": "a" * 43,
    }, doc_id=state_id)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {state_id: s_doc},
        "integration_lifecycle_audit": {},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    transport, recorded_requests = _make_jobber_provider_mock_transport()
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda **kw: _CapturedAsyncClient(transport=transport, **kw))
    monkeypatch.setattr("app.services.jobber.httpx.AsyncClient", lambda **kw: _CapturedAsyncClient(transport=transport, **kw))

    # 1. First execution succeeds
    resp1 = await asgi_client.get(f"/api/integrations/jobber/callback?code=auth_code_1&state={state_id}")
    assert resp1.status_code == 200
    assert len(recorded_requests) == 2

    # 2. Replay attempt with same state fails closed
    resp2 = await asgi_client.get(f"/api/integrations/jobber/callback?code=auth_code_2&state={state_id}")
    assert resp2.status_code == 400
    assert "Location" not in resp2.headers
    assert len(recorded_requests) == 2  # Zero further HTTP calls


# ─────────────────────────────────────────────────────────────────────────────
# 5. Safe Stateless Entry (Marketplace Handoff) & Setup Page
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_jobber_callback_absent_state_returns_hardcoded_303_to_setup_before_db_or_crypto(
    monkeypatch,
    test_app,
    asgi_client,
    caplog,
):
    """Prove unsolicited Marketplace entry missing state returns hard-coded 303 to /jobber/setup with zero DB/crypto effects.
    
    Validates static setup route HTML/CSP and ensures hostile sentinels are not reflected in headers, body, or application logs.
    """
    _setup_test_env(monkeypatch)
    assert settings.integration_token_encrypted_writes_enabled is True

    def _must_not_call_db(*args, **kwargs):
        raise AssertionError("Database entrypoint called on stateless entry!")

    def _must_not_call_is_enc():
        raise AssertionError("is_encryption_configured called on stateless entry!")

    def _must_not_call_write_format(*args, **kwargs):
        raise AssertionError("determine_write_format called on stateless entry!")

    def _must_not_call_http(*args, **kwargs):
        raise AssertionError("Provider AsyncClient called on stateless entry!")

    monkeypatch.setattr(integrations, "_get_firestore", _must_not_call_db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", _must_not_call_db)
    monkeypatch.setattr("app.services.integration_tokens.is_encryption_configured", _must_not_call_is_enc)
    monkeypatch.setattr("app.services.integration_tokens.determine_write_format", _must_not_call_write_format)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", _must_not_call_http)

    hostile_sentinel = "SECRET_SENTINEL_XSS_<script>alert(1)</script>_CRLF_\r\nLocation:evil.test"

    with caplog.at_level(logging.DEBUG):
        resp = await asgi_client.get(
            "/api/integrations/jobber/callback",
            params={"code": hostile_sentinel},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["Location"] == "/api/integrations/jobber/setup"
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "set-cookie" not in resp.headers
    assert len(resp.cookies) == 0
    assert hostile_sentinel not in resp.headers["Location"]

    # GET the clean setup route and assert static HTML/CSP and no sentinel reflection
    setup_resp = await asgi_client.get("/api/integrations/jobber/setup")
    assert setup_resp.status_code == 200
    assert setup_resp.headers["Cache-Control"] == "no-store"
    assert setup_resp.headers["Referrer-Policy"] == "no-referrer"
    assert setup_resp.headers["X-Content-Type-Options"] == "nosniff"

    csp = setup_resp.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "style-src 'unsafe-inline'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'none'" in csp

    body = setup_resp.text
    assert "https://apps.apple.com/app/id6761427495" in body
    assert "Hey Kevin" in body
    assert "Settings" in body
    assert "Integrations" in body
    assert "Jobber" in body
    assert "Connect" in body
    assert "<script" not in body.lower()
    assert '<link rel="stylesheet"' not in body.lower()
    assert "<img" not in body.lower()
    assert hostile_sentinel not in body

    # Exclude test client HTTPX access logs and verify application log hygiene
    app_records = [rec for rec in caplog.records if not rec.name.startswith("httpx")]
    for rec in app_records:
        assert hostile_sentinel not in rec.message
        assert hostile_sentinel not in str(rec.__dict__)


@pytest.mark.asyncio
async def test_jobber_callback_supplied_blank_or_whitespace_state_fails_closed_400(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove supplied blank or whitespace state fails closed with 400 and never redirects 303."""
    _setup_test_env(monkeypatch)

    def _must_not_call_db(*args, **kwargs):
        raise AssertionError("Database accessed on blank state!")

    monkeypatch.setattr(integrations, "_get_firestore", _must_not_call_db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", _must_not_call_db)

    blank_cases = [
        "code=some_code&state=",
        "code=some_code&state=%20%20",
        "code=some_code&state=%09",
    ]
    for qs in blank_cases:
        resp = await asgi_client.get(f"/api/integrations/jobber/callback?{qs}", follow_redirects=False)
        assert resp.status_code == 400
        assert "Location" not in resp.headers
        assert "set-cookie" not in resp.headers
        assert len(resp.cookies) == 0


@pytest.mark.asyncio
async def test_jobber_callback_duplicate_query_parameters_rejected_before_any_effects(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove duplicate state or code query parameters are rejected with HTTP 400 before DB/crypto/exchange."""
    _setup_test_env(monkeypatch)

    def _must_not_call_db(*args, **kwargs):
        raise AssertionError("Firestore accessed on duplicate params!")

    monkeypatch.setattr(integrations, "_get_firestore", _must_not_call_db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", _must_not_call_db)

    # 1. Duplicate state parameters
    resp_dup_s = await asgi_client.get("/api/integrations/jobber/callback?code=c1&state=s1&state=s2", follow_redirects=False)
    assert resp_dup_s.status_code == 400
    assert "Location" not in resp_dup_s.headers
    assert "set-cookie" not in resp_dup_s.headers
    assert len(resp_dup_s.cookies) == 0
    assert "Duplicate query parameters" in resp_dup_s.json()["detail"]

    # 2. Duplicate code parameters
    resp_dup_c = await asgi_client.get("/api/integrations/jobber/callback?code=c1&code=c2&state=s1", follow_redirects=False)
    assert resp_dup_c.status_code == 400
    assert "Location" not in resp_dup_c.headers
    assert "set-cookie" not in resp_dup_c.headers
    assert len(resp_dup_c.cookies) == 0
    assert "Duplicate query parameters" in resp_dup_c.json()["detail"]

    # 3. Duplicate code parameters without state
    resp_dup_c_ns = await asgi_client.get("/api/integrations/jobber/callback?code=c1&code=c2", follow_redirects=False)
    assert resp_dup_c_ns.status_code == 400
    assert "Location" not in resp_dup_c_ns.headers
    assert "set-cookie" not in resp_dup_c_ns.headers
    assert len(resp_dup_c_ns.cookies) == 0
    assert "Duplicate query parameters" in resp_dup_c_ns.json()["detail"]


@pytest.mark.asyncio
async def test_jobber_setup_page_has_honest_instructions_and_strict_csp(
    test_app,
    asgi_client,
):
    """Prove public setup page serves static honest instructions and restrictive security headers."""
    resp = await asgi_client.get("/api/integrations/jobber/setup")

    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"

    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "style-src 'unsafe-inline'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'none'" in csp

    body = resp.text
    assert "https://apps.apple.com/app/id6761427495" in body
    assert "Hey Kevin" in body
    assert "Settings" in body
    assert "Integrations" in body
    assert "Jobber" in body
    assert "Connect" in body

    # Forbidden items: zero script tags, zero external style links, zero external img sources
    assert "<script" not in body.lower()
    assert '<link rel="stylesheet"' not in body.lower()
    assert "<img" not in body.lower()


@pytest.mark.asyncio
async def test_hostile_input_sentinels_not_reflected_in_location_setup_or_logs(
    monkeypatch,
    test_app,
    asgi_client,
    caplog,
):
    """Prove hostile incoming code/state/token sentinels cannot appear in Location header, setup HTML, or logs."""
    _setup_test_env(monkeypatch)
    hostile_sentinel = "SECRET_SENTINEL_XSS_<script>alert(1)</script>_CRLF_\r\nLocation:evil.test"

    with caplog.at_level(logging.DEBUG):
        resp = await asgi_client.get(
            "/api/integrations/jobber/callback",
            params={"code": hostile_sentinel},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["Location"] == "/api/integrations/jobber/setup"
    assert hostile_sentinel not in resp.headers["Location"]

    # Check setup response
    setup_resp = await asgi_client.get("/api/integrations/jobber/setup")
    assert hostile_sentinel not in setup_resp.text

    # Check application logs (excluding HTTPX test client access logs)
    app_records = [rec for rec in caplog.records if not rec.name.startswith("httpx")]
    for rec in app_records:
        assert hostile_sentinel not in rec.message
        assert hostile_sentinel not in str(rec.__dict__)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Google Calendar Schema and Callback Non-Regression
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_google_calendar_oauth_state_schema_remains_exact_seven_fields(monkeypatch):
    """Prove Google Calendar state creation uses exact 7-field schema without PKCE fields."""
    _setup_test_env(monkeypatch)
    cid = "cid_gcal_schema_test"
    state_id = "state_gcal_7field_" + "1" * 16

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "google_calendar_connected": False,
        "google_calendar_generation": 0,
        "google_calendar_lifecycle_epoch": 0,
    }, doc_id=cid)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "google_oauth_states": {},
    })

    created_payload = await mutations_module.create_oauth_state(
        db=db,
        collection_name="google_oauth_states",
        state=state_id,
        contractor_id=cid,
        provider="google_calendar",
    )

    # Google payload must match OAUTH_STATE_KEYS exactly (no pkce fields)
    assert set(created_payload.keys()) == mutations_module.OAUTH_STATE_KEYS
    assert "pkce_method" not in created_payload
    assert "pkce_code_verifier" not in created_payload

    # Consuming Google state succeeds
    consumed, obs = await mutations_module.consume_oauth_state(
        db=db,
        collection_name="google_oauth_states",
        state=state_id,
    )
    assert consumed["contractor_id"] == cid
    assert obs["contractor_id"] == cid


@pytest.mark.asyncio
async def test_google_calendar_callback_exchanges_without_pkce(
    monkeypatch,
    test_app,
    asgi_client,
):
    """Prove Google Calendar callback exchanges authorization code without code_verifier."""
    _setup_test_env(monkeypatch)
    cid = "cid_gcal_callback"
    state_id = "state_gcal_cb_" + "1" * 16

    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "google_calendar_connected": False,
        "google_calendar_generation": 0,
        "google_calendar_lifecycle_epoch": 0,
    }, doc_id=cid)
    s_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "google_calendar",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": compute_raw_credentials_fingerprint(None, None),
        "created_at": time.time(),
        "expires_at": time.time() + 600.0,
    }, doc_id=state_id)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "google_oauth_states": {state_id: s_doc},
        "integration_lifecycle_audit": {},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(mutations_module, "get_firestore_client", lambda: db)

    recorded_requests = []

    async def _gcal_handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        return httpx.Response(200, json={
            "access_token": "gcal_access_token_123",
            "refresh_token": "gcal_refresh_token_123",
            "expires_in": 3600,
        })

    transport = httpx.MockTransport(_gcal_handler)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda **kw: _CapturedAsyncClient(transport=transport, **kw))

    resp = await asgi_client.get(f"/api/integrations/google-calendar/callback?code=gcal_auth_code&state={state_id}")
    assert resp.status_code == 200

    # Verify Google token exchange body has NO code_verifier
    assert len(recorded_requests) == 1
    req = recorded_requests[0]
    assert req.url == integrations.GOOGLE_TOKEN_URL
    form_params = parse_qs(req.content.decode("utf-8"))
    assert "code_verifier" not in form_params
    assert form_params["grant_type"] == ["authorization_code"]
    assert form_params["code"] == ["gcal_auth_code"]
