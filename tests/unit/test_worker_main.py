"""Private route, OIDC and request-lifetime contracts with offline doubles."""

import asyncio
import base64
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import worker_main as worker
from app.db.background_jobs import Admission
from app.middleware import worker_auth as auth


@pytest.fixture
def valid_worker(monkeypatch):
    overrides = {
        "environment": "staging", "background_worker_mode": "external",
        "background_worker_scheduler_audience": "https://private-worker.example.com",
        "background_worker_scheduler_service_account_email": "scheduler@kevin-staging.iam.gserviceaccount.com",
        "allow_production_resources_in_non_production": False,
        "firestore_project_id": "kevin-staging", "firebase_database_url": "https://kevin-staging.firebaseio.com",
        "cloud_run_url": "https://kevin-staging.example.com", "production_twilio_account_sid": "ACproduction",
        "twilio_account_sid": "ACstaging", "apns_sandbox": True, "appstore_environment": "sandbox",
        "transcript_encryption_key": base64.b64encode(b"x"*32).decode(),
    }
    for key, value in overrides.items():
        monkeypatch.setattr(auth.settings, key, value)
    return overrides


def claims():
    return {"iss": "https://accounts.google.com", "aud": auth.settings.background_worker_scheduler_audience,
            "email": auth.settings.background_worker_scheduler_service_account_email,
            "email_verified": True, "sub": "12345678", "iat": int(time.time()) - 1,
            "exp": int(time.time()) + 300}


def request(headers):
    return Request({"type": "http", "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()]})


def test_worker_only_exposes_health_and_fixed_task_route():
    assert {(tuple(sorted(route.methods)), route.path) for route in worker.app.routes} == {
        (("GET",), "/health"), (("POST",), "/tasks/{job}")}


@pytest.mark.parametrize("field,value", [("environment", "production"), ("environment", "development"),
    ("background_worker_mode", "embedded"), ("background_worker_scheduler_audience", "http://worker.example.com"),
    ("background_worker_scheduler_audience", "https://worker.example.com/tasks"),
    ("background_worker_scheduler_service_account_email", "caller@example.com"),
    ("allow_production_resources_in_non_production", True), ("apns_sandbox", False),
    ("firestore_project_id", "kevin-491315"), ("twilio_account_sid", "ACproduction")])
def test_worker_startup_refuses_unsafe_or_malformed_configuration(valid_worker, monkeypatch, field, value):
    monkeypatch.setattr(auth.settings, field, value)
    with pytest.raises(RuntimeError):
        auth.validate_worker_configuration()


def test_valid_private_staging_worker_config(valid_worker):
    auth.validate_worker_configuration()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("iss", "https://evil.example"), ("aud", "other"),
    ("email", "other@kevin-staging.iam.gserviceaccount.com"), ("email_verified", False),
    ("email_verified", "true"), ("sub", ""), ("exp", 1), ("exp", float("nan")), ("exp", True)])
async def test_wrong_identity_or_expiry_rejected(valid_worker, monkeypatch, field, value):
    payload = claims()
    payload[field] = value
    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", lambda *args, **kwargs: payload)
    with pytest.raises(HTTPException) as error:
        await auth.verify_worker_identity(request({"Authorization": "Bearer token"}))
    assert error.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"X-CloudScheduler": "true", "X-CloudScheduler-JobName": "trusted"},
    {"Authorization": "Bearer contractor-token"}, {"Authorization": "Bearer unsigned.jwt.value"}])
async def test_anonymous_forged_scheduler_and_bearer_tokens_rejected(valid_worker, monkeypatch, headers):
    monkeypatch.setattr(auth.id_token, "verify_oauth2_token", Mock(side_effect=ValueError("invalid signature")))
    with pytest.raises(HTTPException) as error:
        await auth.verify_worker_identity(request(headers))
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_real_google_verifier_accepts_signature_and_rejects_tampering(valid_worker, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.auth import crypt, jwt
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(auth.id_token, "_fetch_certs", lambda *args, **kwargs: {"test-key": public.decode()})
    token = jwt.encode(crypt.RSASigner.from_string(pem, key_id="test-key"), claims()).decode()
    await auth.verify_worker_identity(request({"Authorization": f"Bearer {token}"}))
    parts = token.split(".")
    parts[1] = parts[1][:-2] + "ab"
    with pytest.raises(HTTPException):
        await auth.verify_worker_identity(request({"Authorization": "Bearer " + ".".join(parts)}))


def test_oidc_certificate_transport_has_bounded_timeout(monkeypatch):
    original = Mock(return_value="response")
    monkeypatch.setattr(auth.GoogleRequest, "__call__", original)
    assert auth.BoundedGoogleRequest()("https://example.com", timeout=999) == "response"
    assert original.call_args.kwargs["timeout"] == 5.0


@pytest.fixture
def route_access(monkeypatch):
    worker.app.dependency_overrides[auth.verify_worker_identity] = lambda: None
    yield
    worker.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_unknown_and_disabled_jobs_cannot_touch_datastore_or_provider(route_access, monkeypatch):
    store = Mock(side_effect=AssertionError("unexpected datastore access"))
    monkeypatch.setattr(worker, "get_job_store", store)
    monkeypatch.setattr(auth.settings, "purge_enabled", False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="https://worker") as client:
        assert (await client.post("/tasks/arbitrary")).status_code == 404
        assert (await client.post("/tasks/data_purge")).json() == {"status": "disabled"}
    store.assert_not_called()


@pytest.mark.asyncio
async def test_http_response_waits_for_work_and_durable_completion(route_access, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    finished = []
    store = SimpleNamespace(admit=lambda job: Admission("admitted", "nonce", {}),
        finish=lambda *args, **kwargs: finished.append(kwargs) or True)
    monkeypatch.setattr(worker, "get_job_store", lambda: store)
    async def work(job, cursor):
        started.set()
        await release.wait()
        return {}
    monkeypatch.setattr(worker, "run_child", work)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="https://worker") as client:
        response = asyncio.create_task(client.post("/tasks/orphan_calls", json={"continuation": "untrusted"}))
        await started.wait()
        assert not response.done() and not finished
        release.set()
        assert (await response).json() == {"status": "completed"}
    assert finished == [{"completed": True, "continuation": {}}]


@pytest.mark.asyncio
@pytest.mark.parametrize("admission,expected", [("needs_attention", 409), ("unexpected", 503),
    ("not_due", 200), ("in_progress", 200)])
async def test_non_admitted_requests_never_execute(route_access, monkeypatch, admission, expected):
    monkeypatch.setattr(worker, "get_job_store", lambda: SimpleNamespace(admit=lambda _: Admission(admission)))
    run = AsyncMock(side_effect=AssertionError("unexpected work"))
    monkeypatch.setattr(worker, "run_child", run)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="https://worker") as client:
        assert (await client.post("/tasks/orphan_calls")).status_code == expected
    run.assert_not_called()


@pytest.mark.asyncio
async def test_failed_child_marks_ledger_attention(route_access, monkeypatch):
    finish = Mock(return_value=True)
    monkeypatch.setattr(worker, "get_job_store", lambda: SimpleNamespace(
        admit=lambda _: Admission("admitted", "nonce", {}), finish=finish))
    monkeypatch.setattr(worker, "run_child", AsyncMock(side_effect=asyncio.TimeoutError()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="https://worker") as client:
        assert (await client.post("/tasks/post_calls")).status_code == 503
    finish.assert_called_once_with("post_calls", "nonce", completed=False)


@pytest.mark.asyncio
async def test_cancellation_during_admission_settles_claim_then_marks_attention(monkeypatch):
    from threading import Event
    entered, release = Event(), Event()
    finish = Mock(return_value=True)
    def admit(job):
        entered.set()
        assert release.wait(2)
        return Admission("admitted", "nonce", {})
    monkeypatch.setattr(worker, "get_job_store", lambda: SimpleNamespace(admit=admit, finish=finish))
    run = AsyncMock()
    monkeypatch.setattr(worker, "run_child", run)
    task = asyncio.create_task(worker.task("post_calls"))
    try:
        while not entered.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.001)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    run.assert_not_called()
    finish.assert_called_once_with("post_calls", "nonce", completed=False)


@pytest.mark.asyncio
async def test_failed_completion_commit_never_reports_success(route_access, monkeypatch):
    finish = Mock(side_effect=[RuntimeError("commit uncertain"), True])
    monkeypatch.setattr(worker, "get_job_store", lambda: SimpleNamespace(
        admit=lambda _: Admission("admitted", "nonce", {}), finish=finish))
    monkeypatch.setattr(worker, "run_child", AsyncMock(return_value={}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="https://worker") as client:
        assert (await client.post("/tasks/post_calls")).status_code == 503
    assert finish.call_count == 2
    assert finish.call_args.kwargs == {"completed": False}


_PARENT_DIAGNOSTIC_SECRET = "FAKE_TOKEN_contractor_private_firestore_payload_15555550199"


def _worker_diagnostics(caplog):
    return [record for record in caplog.records if record.name == "app.background_worker"]


@pytest.mark.asyncio
async def test_store_bootstrap_preserves_500_with_sanitized_exception_chain(route_access, monkeypatch, caplog):
    def failed_store():
        raise ValueError(_PARENT_DIAGNOSTIC_SECRET)
    monkeypatch.setattr(worker, "get_job_store", failed_store)
    with pytest.raises(RuntimeError) as error:
        await worker.task("estimates")
    assert str(error.value) == "Worker datastore unavailable"
    assert error.value.__suppress_context__ is True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app, raise_app_exceptions=False),
                                 base_url="https://worker") as client:
        response = await client.post("/tasks/estimates")
    assert response.status_code == 500
    assert _PARENT_DIAGNOSTIC_SECRET not in response.text + caplog.text
    assert "phase=admission code=store_unavailable" in caplog.text
    assert all(record.exc_info is None and not record.stack_info for record in _worker_diagnostics(caplog))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site,expected_phase,expected_code", [
    ("admission", "admission", "admission_failed"),
    ("child", "child_execute", "dispatch_failed"),
    ("completion", "completion", "completion_write_failed"),
    ("fenced", "completion", "completion_fenced"),
    ("attention", "attention", "attention_write_failed"),
])
async def test_parent_failure_phases_are_distinguishable_without_exception_data(
    route_access, monkeypatch, caplog, failure_site, expected_phase, expected_code,
):
    class SecretException(RuntimeError):
        def __str__(self):
            raise AssertionError("Exception string must never be read")
    def admit(job):
        if failure_site == "admission":
            raise SecretException(_PARENT_DIAGNOSTIC_SECRET)
        return Admission("admitted", "nonce-private", {})
    finishes = []
    def finish(*args, **kwargs):
        finishes.append(kwargs)
        if kwargs["completed"] and failure_site == "completion":
            raise SecretException(_PARENT_DIAGNOSTIC_SECRET)
        if kwargs["completed"] and failure_site == "fenced":
            return False
        if not kwargs["completed"] and failure_site == "attention":
            raise SecretException(_PARENT_DIAGNOSTIC_SECRET)
        return True
    monkeypatch.setattr(worker, "get_job_store", lambda: SimpleNamespace(admit=admit, finish=finish))
    run = AsyncMock(side_effect=SecretException(_PARENT_DIAGNOSTIC_SECRET)) if failure_site in {"child", "attention"} else AsyncMock(return_value={})
    monkeypatch.setattr(worker, "run_child", run)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="https://worker") as client:
        response = await client.post("/tasks/estimates")
    assert response.status_code == 503
    assert f"phase={expected_phase} code={expected_code}" in caplog.text
    assert _PARENT_DIAGNOSTIC_SECRET not in caplog.text + response.text
    assert "SecretException" not in caplog.text and "nonce-private" not in caplog.text
    assert all(record.exc_info is None and not record.stack_info for record in _worker_diagnostics(caplog))
    if failure_site != "admission":
        assert finishes[-1] == {"completed": False}


@pytest.mark.asyncio
async def test_typed_child_failure_is_distinct_from_completion_failure(route_access, monkeypatch, caplog):
    from app.services.background_dispatch import WorkerChildError
    finish = Mock(return_value=True)
    monkeypatch.setattr(worker, "get_job_store", lambda: SimpleNamespace(
        admit=lambda _: Admission("admitted", "nonce", {}), finish=finish))
    monkeypatch.setattr(worker, "run_child", AsyncMock(side_effect=WorkerChildError("child_bootstrap", "import_failed")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="https://worker") as client:
        assert (await client.post("/tasks/estimates")).status_code == 503
    assert "phase=child_bootstrap code=import_failed" in caplog.text
    assert "phase=completion" not in caplog.text
    finish.assert_called_once_with("estimates", "nonce", completed=False)


@pytest.mark.asyncio
async def test_completion_provider_permission_code_is_static_and_private(route_access, monkeypatch, caplog):
    from google.api_core import exceptions as google_errors
    def finish(*args, **kwargs):
        if kwargs["completed"]:
            raise google_errors.PermissionDenied(_PARENT_DIAGNOSTIC_SECRET)
        return True
    monkeypatch.setattr(worker, "get_job_store", lambda: SimpleNamespace(
        admit=lambda _: Admission("admitted", "nonce", {}), finish=finish))
    monkeypatch.setattr(worker, "run_child", AsyncMock(return_value={}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="https://worker") as client:
        assert (await client.post("/tasks/estimates")).status_code == 503
    assert "phase=completion code=permission_denied" in caplog.text
    assert _PARENT_DIAGNOSTIC_SECRET not in caplog.text
    assert "PermissionDenied" not in caplog.text


@pytest.mark.asyncio
async def test_worker_startup_configuration_diagnostics_do_not_serialize_failure(monkeypatch, caplog):
    def invalid_config():
        raise RuntimeError(_PARENT_DIAGNOSTIC_SECRET)
    monkeypatch.setattr(worker, "validate_worker_configuration", invalid_config)
    with pytest.raises(RuntimeError) as error:
        async with worker.lifespan(worker.app):
            pytest.fail("unsafe worker must not start")
    assert str(error.value) == "Worker configuration invalid"
    assert error.value.__suppress_context__ is True
    assert "job=unknown phase=config code=config_invalid" in caplog.text
    assert _PARENT_DIAGNOSTIC_SECRET not in caplog.text
    assert all(record.exc_info is None for record in _worker_diagnostics(caplog))
