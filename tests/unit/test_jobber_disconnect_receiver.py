"""Receiver authentication and bounded asynchronous persistence, with no network."""
from tests.unit.jobber_disconnect_test_support import seed_required_environment
seed_required_environment()

import asyncio
import base64
import hashlib
import hmac
import json
from threading import Event

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tests.unit.jobber_disconnect_test_support import jobber_settings, active_db
from app.webhooks import jobber as receiver
from app.db.jobber_disconnect_transactions import run_bounded_async, drain_jobber_operations, JobberDeadlineError


def body(app="app", topic="APP_DISCONNECT"):
    return json.dumps({"data": {"webHookEvent": {"topic": topic, "appId": app,
        "accountId": "account", "itemId": "item", "occurredAt": "2026-10-10T00:00:00Z"}}}).encode()


def request(raw=None, signatures=None):
    raw = body() if raw is None else raw
    signature = base64.b64encode(hmac.new(b"fictional-oauth-secret", raw, hashlib.sha256).digest())
    headers = [(b"x-jobber-hmac-sha256", value) for value in (signatures if signatures is not None else [signature])]
    parts = [raw[index:index + 16000] for index in range(0, len(raw), 16000)] or [b""]
    async def receive():
        chunk = parts.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(parts)}
    return Request({"type": "http", "method": "POST", "path": "/webhooks/jobber", "headers": headers}, receive)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,signatures,status", [
    (b"", None, 400), (b"a" * 65537, None, 413), (body(), [], 401),
    (body(), [b"one", b"two"], 401), (body(), [b"invalid"], 400),
    (body("wrong-app"), None, 400), (body(topic="OTHER"), None, 400),
    (b"not-json", None, 400),
])
async def test_rejects_without_datastore(monkeypatch, raw, signatures, status):
    monkeypatch.setattr(receiver, "get_firestore_client", lambda: pytest.fail("Unexpected datastore"))
    with pytest.raises(HTTPException) as error:
        await receiver.jobber_webhook_receiver(request(raw, signatures))
    assert error.value.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [("jobber_disconnect_receiver_enabled", False),
    ("jobber_client_secret", ""), ("jobber_disconnect_expected_app_id", "")])
async def test_disabled_or_missing_explicit_configuration_no_db(monkeypatch, jobber_settings, key, value):
    monkeypatch.setattr(jobber_settings, key, value)
    monkeypatch.setattr(receiver, "get_firestore_client", lambda: pytest.fail("Unexpected datastore"))
    with pytest.raises(HTTPException) as error:
        await receiver.jobber_webhook_receiver(request())
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_real_persistence_ack_and_independent_delivery(monkeypatch):
    db = active_db()
    monkeypatch.setattr(receiver, "get_firestore_client", lambda: db)
    assert (await receiver.jobber_webhook_receiver(request())).status_code == 200
    assert (await receiver.jobber_webhook_receiver(request())).status_code == 200
    assert db._data["jobber_disconnect_control/current"]["receipt_counter"] == 2
    assert len([key for key in db._data if key.startswith("jobber_disconnect_receipts/")]) == 2


@pytest.mark.asyncio
async def test_persistence_failure_503(monkeypatch):
    db = active_db()
    db.collection("jobber_disconnect_control").document("current").delete()
    monkeypatch.setattr(receiver, "get_firestore_client", lambda: db)
    with pytest.raises(HTTPException) as error:
        await receiver.jobber_webhook_receiver(request())
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_datastore_thread_does_not_block_event_loop(monkeypatch):
    entered, release = Event(), Event()
    monkeypatch.setattr(receiver, "get_firestore_client", lambda: object())
    def blocked(**kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return {}
    monkeypatch.setattr(receiver, "record_disconnect_admission_atomic", blocked)
    task = asyncio.create_task(receiver.jobber_webhook_receiver(request()))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set() and not task.done()
        # This coroutine is running while the datastore function is blocked.
        release.set()
        assert (await task).status_code == 200
    finally:
        release.set()
        await drain_jobber_operations()


@pytest.mark.asyncio
async def test_cancelled_waits_retain_capacity_until_threads_finish():
    entered1, entered2, release = Event(), Event(), Event()
    def blocked(marker):
        def invoke(deadline):
            marker.set()
            release.wait(timeout=2)
        return invoke
    tasks = [asyncio.create_task(run_bounded_async(blocked(event), seconds=3)) for event in (entered1, entered2)]
    try:
        for _ in range(100):
            if entered1.is_set() and entered2.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered1.is_set() and entered2.is_set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with pytest.raises(JobberDeadlineError):
            await run_bounded_async(lambda deadline: None, seconds=1)
    finally:
        release.set()
        await drain_jobber_operations()


@pytest.mark.asyncio
async def test_actual_startup_shutdown_track_single_recovery_task(monkeypatch):
    # Execute the actual two lifecycle function definitions with inert dependencies;
    # importing the full application would activate unrelated fixture requirements.
    import ast
    from pathlib import Path
    import sys
    from types import ModuleType, SimpleNamespace
    from contextlib import suppress
    source = Path(__file__).resolve().parents[2] / "app" / "main.py"
    # tests/unit -> repository root is parents[2].
    tree = ast.parse(source.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in ("startup", "shutdown")]
    for node in functions:
        node.decorator_list = []
    tasks = []
    async def idle():
        await asyncio.Event().wait()
    async def recovery():
        await asyncio.Event().wait()
    def create(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    for module_name, attribute, fn in [
        ("app.services.post_call_handoff", "post_call_worker_loop", idle),
        ("app.services.estimate_worker", "estimate_worker_loop", idle),
        ("app.services.service_request_recovery", "service_request_recovery_worker_loop", idle),
        ("app.services.jobber_disconnect_recovery", "jobber_disconnect_recovery_worker_loop", recovery),
    ]:
        module = ModuleType(module_name)
        setattr(module, attribute, fn)
        monkeypatch.setitem(sys.modules, module_name, module)
    config = SimpleNamespace(**{key: "fictional" for key in [
        "twilio_account_sid", "twilio_auth_token", "anthropic_api_key", "deepgram_api_key", "elevenlabs_api_key", "api_bearer_token"]},
        vapi_webhook_secret="", environment="development", service_request_recovery_enabled=False,
        jobber_disconnect_recovery_enabled=True, background_worker_mode="embedded")
    namespace = {"asyncio": SimpleNamespace(create_task=create, CancelledError=asyncio.CancelledError, gather=asyncio.gather),
        "settings": config, "validate_runtime_safety": lambda: None,
        "logger": SimpleNamespace(info=lambda *args, **kwargs: None), "suppress": suppress}
    for name in ["_post_call_worker_task", "_service_request_recovery_task", "_estimate_worker_task", "_jobber_disconnect_recovery_task"]:
        namespace[name] = None
    namespace["_housekeeping_tasks"] = {}
    for name in ["_orphan_call_cleanup", "_call_retention_sweep", "_lapsed_trial_sweep", "_expired_contractor_cleanup", "_data_purge_sweep"]:
        async def housekeeping():
            await idle()
        housekeeping.__name__ = name
        namespace[name] = housekeeping
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    try:
        await namespace["startup"]()
        first = namespace["_jobber_disconnect_recovery_task"]
        await namespace["startup"]()
        assert namespace["_jobber_disconnect_recovery_task"] is first
        await namespace["shutdown"]()
        assert first.cancelled()
        assert namespace["_jobber_disconnect_recovery_task"] is None
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_receiver_and_recovery_queue_together_and_ack_only_same_durable_admission(monkeypatch):
    from app.db import jobber_disconnect_transactions as pool
    from app.services import jobber_disconnect_recovery as recovery
    from app.db.jobber_disconnect_receipts import RECEIPTS_COLLECTION
    db = active_db()
    release, persist_entered, allow_persist = Event(), Event(), Event()
    holders_entered = [Event(), Event()]
    admission_ids = []
    real_persist = receiver.record_disconnect_admission_atomic
    real_recovery = recovery._sync_run_recovery_once
    recovered = []
    def holder(i):
        def run(deadline):
            holders_entered[i].set()
            assert release.wait(3)
        return run
    def persist(**kwargs):
        admission_ids.append(kwargs["admission_id"])
        persist_entered.set()
        assert allow_persist.wait(3)
        return real_persist(**kwargs)
    def recover(*args):
        recovered.append(True)
        return real_recovery(*args)
    monkeypatch.setattr(receiver, "get_firestore_client", lambda: db)
    monkeypatch.setattr(receiver, "record_disconnect_admission_atomic", persist)
    monkeypatch.setattr(recovery, "_sync_run_recovery_once", recover)
    async def until(predicate):
        for _ in range(1000):
            if predicate(): return
            await asyncio.sleep(.002)
        pytest.fail("shared pool condition did not become true")
    def waiting_count():
        with pool._OUTSTANDING_LOCK: return len(pool._WAITING)
    tasks = [asyncio.create_task(run_bounded_async(holder(i), seconds=4)) for i in range(2)]
    try:
        await until(lambda: all(e.is_set() for e in holders_entered))
        received = asyncio.create_task(receiver.jobber_webhook_receiver(request()))
        tasks.append(received)
        await until(lambda: waiting_count() == 1)
        recovered_task = asyncio.create_task(recovery.run_jobber_disconnect_recovery_once(db=db))
        tasks.append(recovered_task)
        await until(lambda: waiting_count() == 2)
        assert not admission_ids and not recovered and not received.done()
        release.set()
        await until(persist_entered.is_set)
        assert len(admission_ids) == 1 and not received.done()
        assert not list(db.collection(RECEIPTS_COLLECTION).stream())
        # Ambiguous write acknowledgement must read back this admission, not
        # mint another one or acknowledge a background-only receipt.
        await recovered_task
        db.commit_then_raise = True
        allow_persist.set()
        assert (await received).status_code == 200
        assert len(admission_ids) == 1
        assert db.collection(RECEIPTS_COLLECTION).document(admission_ids[0]).get().exists
        assert db._data["jobber_disconnect_control/current"]["receipt_counter"] == 1
        await recovered_task
        assert recovered == [True]
    finally:
        release.set(); allow_persist.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await drain_jobber_operations()
