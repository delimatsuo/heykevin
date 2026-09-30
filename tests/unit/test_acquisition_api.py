"""Unit tests for /api/acquisition routes and transactional attribution engine."""

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import asyncio
import json
import logging
import math
import threading
import time
from typing import Any, Dict, Optional
import pytest
from fastapi import FastAPI
import httpx
from httpx import AsyncClient, ASGITransport

from app.api.acquisition import router as acquisition_router
from app.config import settings
from app.middleware import auth as auth_module
from app.services import acquisition


# Minimal FastAPI app mounting the acquisition router (renamed route_app to avoid collection warning)
route_app = FastAPI()
route_app.include_router(acquisition_router)


@pytest.fixture(autouse=True)
def outbound_tripwire(monkeypatch):
    """Prevent real network and Firestore client construction during unit tests."""
    def _block_httpx(*args, **kwargs):
        raise RuntimeError("Real httpx network transport is forbidden in unit tests")

    def _block_requests(*args, **kwargs):
        raise RuntimeError("Real requests network call is forbidden in unit tests")

    def _block_firestore(*args, **kwargs):
        raise RuntimeError("Real google.cloud.firestore.Client construction is forbidden in unit tests")

    monkeypatch.setattr("httpx.AsyncHTTPTransport.handle_async_request", _block_httpx)
    import requests.sessions
    monkeypatch.setattr(requests.sessions.Session, "request", _block_requests)
    import google.cloud.firestore
    monkeypatch.setattr(google.cloud.firestore.Client, "__init__", _block_firestore)


class _InMemoryFirestore:
    def __init__(self):
        self._docs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def collection(self, name: str):
        return _Collection(self, name)

    def transaction(self):
        return _FakeTransaction(self)


class _Collection:
    def __init__(self, fs: _InMemoryFirestore, name: str):
        self._fs = fs
        self._name = name

    def document(self, doc_id: str):
        return _DocRef(self._fs, f"{self._name}/{doc_id}", doc_id)


class _Snapshot:
    def __init__(self, exists: bool, data: Optional[dict], doc_id: str = ""):
        self.exists = exists
        self._data = data or {}
        self.id = doc_id

    def to_dict(self):
        return json.loads(json.dumps(self._data))


class _DocRef:
    def __init__(self, fs: _InMemoryFirestore, path: str, doc_id: str):
        self._fs = fs
        self._path = path
        self.id = doc_id

    def get(self, transaction=None):
        with self._fs._lock:
            data = self._fs._docs.get(self._path)
            return _Snapshot(data is not None, data, doc_id=self.id)

    def update(self, value: dict):
        with self._fs._lock:
            if self._path not in self._fs._docs:
                raise KeyError(f"Document {self._path} does not exist")
            target = self._fs._docs[self._path]
            for k, v in value.items():
                if "." in k:
                    parts = k.split(".")
                    curr = target
                    for p in parts[:-1]:
                        if p not in curr or not isinstance(curr[p], dict):
                            curr[p] = {}
                        curr = curr[p]
                    curr[parts[-1]] = v
                else:
                    target[k] = v


class _FakeTransaction:
    def __init__(self, fs: _InMemoryFirestore):
        self._fs = fs

    def update(self, doc_ref: _DocRef, value: dict):
        doc_ref.update(value)


@pytest.fixture
def fake_db(monkeypatch):
    db = _InMemoryFirestore()
    monkeypatch.setattr(acquisition, "get_firestore_client", lambda: db)
    # Wrap firestore.transactional to execute with the fake db's lock
    def _fake_transactional(fn):
        def _wrapped(transaction, *args, **kwargs):
            with db._lock:
                return fn(transaction, *args, **kwargs)
        return _wrapped
    monkeypatch.setattr("google.cloud.firestore.transactional", _fake_transactional)
    return db


@pytest.fixture(autouse=True)
def clear_token_cache(monkeypatch):
    monkeypatch.setattr(settings, "api_bearer_token", "dummy_configured_admin_token")
    auth_module._token_cache.clear()
    yield
    auth_module._token_cache.clear()


@pytest.mark.asyncio
async def test_acquisition_status_unauthenticated():
    """Verify GET /api/acquisition/status rejects unauthenticated requests with 401."""
    async with AsyncClient(transport=ASGITransport(app=route_app), base_url="http://test") as client:
        response = await client.get("/api/acquisition/status")
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_acquisition_status_admin_returns_not_eligible(monkeypatch):
    """Verify GET /api/acquisition/status returns eligible=False for admin token."""
    monkeypatch.setattr(settings, "api_bearer_token", "admin-secret-token")
    headers = {"Authorization": "Bearer admin-secret-token"}
    async with AsyncClient(transport=ASGITransport(app=route_app), base_url="http://test") as client:
        response = await client.get("/api/acquisition/status", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"eligible": False}


@pytest.mark.asyncio
async def test_acquisition_apple_ads_refuses_admin_token(monkeypatch):
    """Verify POST /api/acquisition/apple-ads refuses admin token with 403."""
    monkeypatch.setattr(settings, "api_bearer_token", "admin-secret-token")
    headers = {"Authorization": "Bearer admin-secret-token"}
    async with AsyncClient(transport=ASGITransport(app=route_app), base_url="http://test") as client:
        response = await client.post("/api/acquisition/apple-ads", json={"token": "test-token"}, headers=headers)
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_acquisition_contractor_auth_success(fake_db, monkeypatch):
    """Verify contractor token auth resolves contractor ID and binds status/apple-ads."""
    monkeypatch.setattr(settings, "api_bearer_token", "admin-secret-token")
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960000.0  # Sept 2026
    contractor_id = "cnt_auth_test"
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": now,
            "created_at": now,
            "declared_onboarding_intent": "personal",
            "attempts": 0,
            "last_attempt_at": None,
            "attribution_status": None,
            "lease_attempt": None,
            "lease_expires_at": None,
        }
    }

    async def fake_get_contractor_by_token(token_hash):
        return {"contractor_id": contractor_id, "active": True}

    async def fake_get_contractor(cid):
        return fake_db._docs.get(f"contractors/{cid}")

    monkeypatch.setattr("app.db.contractors.get_contractor_by_api_token", fake_get_contractor_by_token)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)

    headers = {"Authorization": "Bearer contractor-api-key-123"}
    async with AsyncClient(transport=ASGITransport(app=route_app), base_url="http://test") as client:
        # GET /status
        resp_status = await client.get("/api/acquisition/status", headers=headers)
        assert resp_status.status_code == 200
        assert resp_status.json() == {"eligible": True}


@pytest.mark.asyncio
async def test_acquisition_contractor_post_identity_binding_and_rejection_of_extra_contractor_id(fake_db, monkeypatch):
    """Verify contractor POST binds authenticated contractor identity and rejects extra contractor_id body field."""
    monkeypatch.setattr(settings, "api_bearer_token", "admin-secret-token")
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960000.0
    contractor_id = "cnt_post_auth_test"
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": now,
            "created_at": now,
            "declared_onboarding_intent": "personal",
            "attempts": 0,
            "last_attempt_at": None,
            "attribution_status": None,
            "lease_attempt": None,
            "lease_expires_at": None,
        }
    }

    async def fake_get_contractor_by_token(token_hash):
        return {"contractor_id": contractor_id, "active": True}

    async def fake_get_contractor(cid):
        return fake_db._docs.get(f"contractors/{cid}")

    monkeypatch.setattr("app.db.contractors.get_contractor_by_api_token", fake_get_contractor_by_token)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)

    called_with_contractor_id = []
    async def fake_process(cid, token):
        called_with_contractor_id.append((cid, token))
        return {"status": "recorded"}

    # Patch the router's directly imported binding in app.api.acquisition
    monkeypatch.setattr("app.api.acquisition.process_apple_ads_attribution", fake_process)

    headers = {"Authorization": "Bearer contractor-key-xyz"}
    async with AsyncClient(transport=ASGITransport(app=route_app), base_url="http://test") as client:
        # 1. Successful POST binds authenticated contractor identity
        resp_post = await client.post(
            "/api/acquisition/apple-ads",
            json={"token": "valid-token-123"},
            headers=headers,
        )
        assert resp_post.status_code == 200
        assert resp_post.json() == {"status": "recorded"}
        assert called_with_contractor_id == [(contractor_id, "valid-token-123")]

        # 2. Rejection of extra contractor_id in request body
        resp_extra = await client.post(
            "/api/acquisition/apple-ads",
            json={"token": "valid-token-123", "contractor_id": "cnt_other"},
            headers=headers,
        )
        assert resp_extra.status_code == 422
        # Assert no extra call was made to process_apple_ads_attribution
        assert len(called_with_contractor_id) == 1


@pytest.mark.asyncio
async def test_acquisition_apple_ads_rejects_extra_fields(monkeypatch):
    """Verify POST /api/acquisition/apple-ads rejects extra body fields with 422."""
    monkeypatch.setattr(settings, "api_bearer_token", "admin-secret-token")
    headers = {"Authorization": "Bearer admin-secret-token"}
    async with AsyncClient(transport=ASGITransport(app=route_app), base_url="http://test") as client:
        response = await client.post(
            "/api/acquisition/apple-ads",
            json={"token": "test-token", "extra_field": "disallowed", "contractor_id": "cnt_hacked"},
            headers=headers,
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_acquisition_apple_ads_rejects_empty_token(monkeypatch):
    """Verify POST /api/acquisition/apple-ads rejects empty token string with 422."""
    monkeypatch.setattr(settings, "api_bearer_token", "admin-secret-token")
    headers = {"Authorization": "Bearer admin-secret-token"}
    async with AsyncClient(transport=ASGITransport(app=route_app), base_url="http://test") as client:
        response = await client.post(
            "/api/acquisition/apple-ads",
            json={"token": ""},
            headers=headers,
        )
        assert response.status_code == 422


@pytest.mark.parametrize("corrupt_acq", [
    {"schema_version": 2, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": 0},  # wrong schema_version
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960100.0, "attempts": 0},  # created != cohort
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": True},  # attempts bool
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": 4},  # attempts > 3
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": 1, "last_attempt_at": None},  # attempts>0 missing last_attempt
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": 1, "last_attempt_at": 1788950000.0},  # last_attempt < created
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": 1, "last_attempt_at": float("nan")},  # NaN
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": 1, "last_attempt_at": 1788960000.0, "lease_attempt": 1},  # unpaired lease
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": 2, "last_attempt_at": 1788960000.0, "lease_attempt": 1, "lease_expires_at": 1788960010.0},  # lease_attempt != attempts
    {"schema_version": 1, "cohort": 1788960000.0, "created_at": 1788960000.0, "attempts": 1, "last_attempt_at": 1788960000.0, "lease_attempt": 1, "lease_expires_at": 1788960020.0},  # expiry > last_attempt + 15
])
@pytest.mark.asyncio
async def test_corrupted_budget_and_lease_rejected_without_writes(fake_db, monkeypatch, corrupt_acq):
    """Verify corrupt state fails validation and returns ineligible without mutating storage."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    contractor_id = "cnt_corrupt"
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": dict(corrupt_acq),
    }

    async def fake_get_contractor(cid):
        return fake_db._docs.get(f"contractors/{cid}")

    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    assert await acquisition.check_contractor_acquisition_eligibility(contractor_id) is False

    # Lease attempt returns ineligible
    res = await acquisition.lease_attribution_attempt(contractor_id)
    assert res["status"] == "ineligible"

    # Terminal attribution returns ineligible
    res_term = await acquisition.record_terminal_attribution(contractor_id, {"attribution": True}, 1)
    assert res_term == "ineligible"

    # Failed attempt finalizer returns ineligible
    res_fail = await acquisition.finalize_failed_attempt(contractor_id, 1)
    assert res_fail == "ineligible"

    # Verify no writes occurred
    assert fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"] == corrupt_acq


@pytest.mark.asyncio
async def test_duplicate_request_while_attempt3_in_flight_blocks_exhaustion(fake_db, monkeypatch):
    """Verify valid third outstanding lease blocks duplicate exhaustion during in-flight network call."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960000.0
    contractor_id = "cnt_concurrent_3"
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": now - 100,
            "created_at": now - 100,
            "declared_onboarding_intent": "personal",
            "attempts": 2,
            "last_attempt_at": now - 20,
            "attribution_status": None,
            "lease_attempt": None,
            "lease_expires_at": None,
        }
    }

    network_entered = asyncio.Event()
    network_release = asyncio.Event()

    apple_response = {
        "attribution": True,
        "orgId": 987654,
        "campaignId": 111,
        "adGroupId": 222,
    }

    class _HeldStreamResponse:
        def __init__(self):
            self.status_code = 200

        async def aiter_bytes(self):
            network_entered.set()
            await network_release.wait()
            yield json.dumps(apple_response).encode("utf-8")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class _ControlledAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            return _HeldStreamResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(acquisition.httpx, "AsyncClient", _ControlledAsyncClient)

    current_time = now
    monkeypatch.setattr(acquisition.time, "time", lambda: current_time)

    # Start first request (reserves attempt 3)
    task1 = asyncio.create_task(acquisition.process_apple_ads_attribution(contractor_id, "token_123"))

    try:
        # Wait until task1 enters streaming network exchange
        await asyncio.wait_for(network_entered.wait(), timeout=1)

        # Document now has attempts=3 and active lease
        doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
        assert doc["attempts"] == 3
        assert doc["lease_attempt"] == 3
        assert doc["attribution_status"] is None  # NOT exhausted!

        # Advance fake clock by 6.0s (after 5s spacing, before 15s lease expiry)
        current_time += 6.0

        # Concurrent request arrives while task1 is in-flight
        lease_dup = await acquisition.lease_attribution_attempt(contractor_id)
        assert lease_dup["status"] == "retryable"
        assert 5 <= lease_dup["retry_after_seconds"] <= 15
        # Document still has attempts=3 and NOT exhausted
        doc_after_dup = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
        assert doc_after_dup["attempts"] == 3
        assert doc_after_dup["attribution_status"] is None

        # Release network response
        network_release.set()
        res1 = await task1
        assert res1["status"] == "recorded"

        # Terminal attribution recorded once, lease cleared
        doc_final = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
        assert doc_final["attribution_status"] == "recorded"
        assert doc_final["attempts"] == 3
        assert doc_final["lease_attempt"] is None
        assert doc_final["attribution"] is True
    finally:
        network_release.set()
        if not task1.done():
            task1.cancel()
            try:
                await task1
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_stale_finalizer_returns_retryable_and_does_not_disturb_newer_lease(fake_db, monkeypatch):
    """Verify stale finalizer from attempt 1 does not clear newer lease 2 or overwrite terminal states."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960000.0
    contractor_id = "cnt_stale_fin"
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": now - 100,
            "created_at": now - 100,
            "declared_onboarding_intent": "personal",
            "attempts": 2,
            "last_attempt_at": now,
            "attribution_status": None,
            "lease_attempt": 2,
            "lease_expires_at": now + 15.0,
        }
    }

    # Stale attempt 1 finalization arrives (captured_lease_attempt=1 != active lease 2)
    stale_term = await acquisition.record_terminal_attribution(
        contractor_id,
        {"attribution": True, "org_id": 987654, "campaign_id": 111, "ad_group_id": 222},
        captured_lease_attempt=1,
    )
    assert stale_term == "retryable"

    # Stale failed attempt 1 arrives
    stale_fail = await acquisition.finalize_failed_attempt(contractor_id, captured_lease_attempt=1)
    assert stale_fail == "retryable"

    # Active lease 2 was NOT disturbed
    doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    assert doc["attempts"] == 2
    assert doc["lease_attempt"] == 2
    assert doc["lease_expires_at"] == now + 15.0
    assert doc["attribution_status"] is None


@pytest.mark.parametrize("scenario", ["deactivated", "deleted", "disabled"])
@pytest.mark.asyncio
async def test_deactivated_deleted_or_disabled_during_network_response(fake_db, monkeypatch, scenario):
    """Verify account deactivation, deletion, or system disable mid-flight returns ineligible/disabled without crash or writes."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960000.0
    contractor_id = f"cnt_{scenario}"
    initial_acq = {
        "schema_version": 1,
        "cohort": now - 100,
        "created_at": now - 100,
        "declared_onboarding_intent": "personal",
        "attempts": 0,
        "last_attempt_at": None,
        "attribution_status": None,
        "lease_attempt": None,
        "lease_expires_at": None,
    }
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": dict(initial_acq),
    }

    # Lease attempt 1
    lease = await acquisition.lease_attribution_attempt(contractor_id)
    assert lease["status"] == "leased"

    # Mid-flight mutation before finalization
    if scenario == "deactivated":
        fake_db._docs[f"contractors/{contractor_id}"]["active"] = False
    elif scenario == "deleted":
        fake_db._docs.pop(f"contractors/{contractor_id}", None)
    elif scenario == "disabled":
        monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", False)

    res = await acquisition.record_terminal_attribution(
        contractor_id,
        {"attribution": True, "org_id": 987654, "campaign_id": 111, "ad_group_id": 222},
        captured_lease_attempt=1,
    )

    if scenario in ("deactivated", "deleted"):
        assert res == "ineligible"
    elif scenario == "disabled":
        assert res == "disabled"

    if scenario == "deactivated":
        # Ensure terminal attribution was NOT written
        doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
        assert doc.get("attribution_status") is None
        assert doc.get("attribution") is None
    elif scenario == "deleted":
        assert f"contractors/{contractor_id}" not in fake_db._docs


@pytest.mark.asyncio
async def test_disable_after_entry_before_transaction_cancels_write(fake_db, monkeypatch):
    """Verify disabling feature after record_terminal_attribution entry but before transaction executes returns disabled and aborts write."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960000.0
    contractor_id = "cnt_toggle_mid"
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": now - 100,
            "created_at": now - 100,
            "declared_onboarding_intent": "personal",
            "attempts": 1,
            "last_attempt_at": now,
            "attribution_status": None,
            "lease_attempt": 1,
            "lease_expires_at": now + 15.0,
        }
    }

    # Intercept transaction execution to flip enabled flag before transaction body runs
    def _toggle_and_run_transactional(fn):
        def _wrapped(transaction, *args, **kwargs):
            acquisition.settings.acquisition_measurement_enabled = False
            with fake_db._lock:
                return fn(transaction, *args, **kwargs)
        return _wrapped

    monkeypatch.setattr("google.cloud.firestore.transactional", _toggle_and_run_transactional)

    res = await acquisition.record_terminal_attribution(
        contractor_id,
        {"attribution": True, "org_id": 987654, "campaign_id": 111, "ad_group_id": 222},
        captured_lease_attempt=1,
    )
    assert res == "disabled"

    # Assert no write occurred on the document
    doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    assert doc["attribution_status"] is None
    assert doc.get("attribution") is None


@pytest.mark.asyncio
async def test_disabled_helper_never_acquires_db_client(monkeypatch):
    """Verify all collection helpers return immediately without obtaining DB client when disabled."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", False)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 0)

    db_called = False
    def boom():
        nonlocal db_called
        db_called = True
        raise AssertionError("get_firestore_client must not be called when disabled")

    monkeypatch.setattr(acquisition, "get_firestore_client", boom)

    assert await acquisition.check_contractor_acquisition_eligibility("cnt_123") is False
    assert (await acquisition.lease_attribution_attempt("cnt_123")) == {"status": "disabled"}
    assert (await acquisition.record_terminal_attribution("cnt_123", {}, 1)) == "disabled"
    assert (await acquisition.finalize_failed_attempt("cnt_123", 1)) == "disabled"
    assert (await acquisition.process_apple_ads_attribution("cnt_123", "token")) == {"status": "disabled"}

    await acquisition.record_inbound_call_measurement("cnt_123", 1788960000.0)
    await acquisition.record_forwarded_call_measurement("cnt_123", 1788960000.0)
    await acquisition.record_payment_measurement("cnt_123", "personal", {})

    assert db_called is False


@pytest.mark.asyncio
async def test_streaming_fake_assertions_and_16kib_cutoff(fake_db, monkeypatch, caplog):
    """Verify streaming fake asserts URL, POST, text/plain, redirect=False, timeout, and cuts off >16KiB without requesting sentinel chunk."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960000.0
    contractor_id = "cnt_streaming_assert"
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": now - 100,
            "created_at": now - 100,
            "declared_onboarding_intent": "personal",
            "attempts": 0,
            "last_attempt_at": None,
            "attribution_status": None,
            "lease_attempt": None,
            "lease_expires_at": None,
        }
    }

    sentinel_chunk_requested = False
    stream_closed = False
    recorded_calls = []

    class _StreamingCutoffResponse:
        def __init__(self):
            self.status_code = 200

        async def aiter_bytes(self):
            # Chunk 1: exactly 16 KiB (16384 bytes)
            yield b"A" * (16 * 1024)
            # Chunk 2: 1 byte (total = 16385 > 16384 bytes cutoff)
            yield b"B"
            # Chunk 3: Sentinel chunk that must NOT be requested / consumed
            nonlocal sentinel_chunk_requested
            sentinel_chunk_requested = True
            yield b"UNCONSUMED_SENTINEL_CHUNK_PII"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            nonlocal stream_closed
            stream_closed = True

    class _AssertingAsyncClient:
        def __init__(self, timeout=None, follow_redirects=None, *args, **kwargs):
            self.timeout = timeout
            self.follow_redirects = follow_redirects

        def stream(self, method, url, content=None, headers=None, *args, **kwargs):
            recorded_calls.append({
                "method": method,
                "url": url,
                "content": content,
                "headers": headers,
                "timeout": self.timeout,
                "follow_redirects": self.follow_redirects,
            })
            return _StreamingCutoffResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(acquisition.httpx, "AsyncClient", _AssertingAsyncClient)

    with caplog.at_level(logging.DEBUG):
        res = await acquisition.process_apple_ads_attribution(contractor_id, "token_stream_test")
        # Exceeding body size triggers failed attempt finalizer -> retryable (attempt 1)
        assert res["status"] == "retryable"
        assert res["retry_after_seconds"] == 5

    # Assert exact request contract
    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api-adservices.apple.com/api/v1/"
    assert call["headers"] == {"Content-Type": "text/plain"}
    assert call["content"] == b"token_stream_test"
    assert call["follow_redirects"] is False
    assert call["timeout"] is not None
    assert call["timeout"].read == 5.0
    assert call["timeout"].connect == 3.0

    # Assert stream closed and sentinel was NEVER requested or logged
    assert stream_closed is True
    assert sentinel_chunk_requested is False
    for record in caplog.records:
        assert "UNCONSUMED_SENTINEL_CHUNK_PII" not in record.message


@pytest.mark.parametrize("error_scenario", ["http_404", "http_500", "invalid_json"])
@pytest.mark.asyncio
async def test_apple_ads_bounded_retries_on_http_errors_and_invalid_json(fake_db, monkeypatch, error_scenario):
    """Verify HTTP 404, HTTP 500, and invalid JSON responses result in bounded retries and eventual exhaustion."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    now = 1788960000.0
    contractor_id = f"cnt_err_{error_scenario}"
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": now - 100,
            "created_at": now - 100,
            "declared_onboarding_intent": "personal",
            "attempts": 2,  # Already at attempt 2; next attempt will be 3 (final)
            "last_attempt_at": now - 20,
            "attribution_status": None,
            "lease_attempt": None,
            "lease_expires_at": None,
        }
    }

    class _ErrorStreamResponse:
        def __init__(self):
            if error_scenario == "http_404":
                self.status_code = 404
            elif error_scenario == "http_500":
                self.status_code = 500
            else:
                self.status_code = 200

        async def aiter_bytes(self):
            if error_scenario == "invalid_json":
                yield b"NOT_VALID_JSON{{"
            elif error_scenario == "http_404":
                yield b"Not Found"
            else:
                yield b"Internal Server Error"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class _ErrorAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            return _ErrorStreamResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(acquisition.httpx, "AsyncClient", _ErrorAsyncClient)

    # Attempt 3 fails -> finalizer exhausts attempts (attempts >= 3)
    res = await acquisition.process_apple_ads_attribution(contractor_id, "test_token")
    assert res["status"] == "exhausted"

    doc = fake_db._docs[f"contractors/{contractor_id}"]["acquisition_measurement"]
    assert doc["attempts"] == 3
    assert doc["attribution_status"] == "exhausted"
    assert doc["lease_attempt"] is None


@pytest.mark.asyncio
async def test_streaming_transport_deadline_and_logging_privacy(fake_db, monkeypatch, caplog):
    """Verify HTTP timeout returns retryable and logs never contain synthetic token/PII sentinels."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)
    monkeypatch.setattr(acquisition, "HTTP_DEADLINE_SECONDS", 0.05)

    now = 1788960000.0
    contractor_id = "cnt_timeout"
    fake_db._docs[f"contractors/{contractor_id}"] = {
        "active": True,
        "acquisition_measurement": {
            "schema_version": 1,
            "cohort": now - 100,
            "created_at": now - 100,
            "declared_onboarding_intent": "personal",
            "attempts": 0,
            "last_attempt_at": None,
            "attribution_status": None,
            "lease_attempt": None,
            "lease_expires_at": None,
        }
    }

    class _HangingStreamResponse:
        def __init__(self):
            self.status_code = 200

        async def aiter_bytes(self):
            await asyncio.sleep(1.0)
            yield b"{}"

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class _HangingClient:
        def __init__(self, *args, **kwargs):
            pass

        def stream(self, *args, **kwargs):
            return _HangingStreamResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    monkeypatch.setattr(acquisition.httpx, "AsyncClient", _HangingClient)

    token_sentinel = "SECRET_TOKEN_SENTINEL_XYZ_999"
    with caplog.at_level(logging.DEBUG):
        res = await acquisition.process_apple_ads_attribution(contractor_id, token_sentinel)
        assert res["status"] == "retryable"
        assert res["retry_after_seconds"] == 5

    # Log check: token sentinel must never appear in logs
    for record in caplog.records:
        assert token_sentinel not in record.message


@pytest.mark.asyncio
async def test_multibyte_token_8192_boundary(fake_db, monkeypatch):
    """Verify token byte length validation correctly bounds multibyte UTF-8 input."""
    monkeypatch.setattr(acquisition.settings, "acquisition_measurement_enabled", True)
    monkeypatch.setattr(acquisition.settings, "apple_ads_expected_org_id", 987654)

    contractor_id = "cnt_multibyte"

    # Multi-byte UTF-8 character (e.g. '€' is 3 bytes)
    # 2730 * 3 = 8190 bytes (<= 8192)
    valid_multibyte = "€" * 2730
    assert len(valid_multibyte.encode("utf-8")) == 8190

    # 2731 * 3 = 8193 bytes (> 8192)
    invalid_multibyte = "€" * 2731
    assert len(invalid_multibyte.encode("utf-8")) == 8193

    # Invalid multibyte length is rejected as ineligible before leasing
    res = await acquisition.process_apple_ads_attribution(contractor_id, invalid_multibyte)
    assert res["status"] == "ineligible"
