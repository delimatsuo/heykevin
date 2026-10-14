"""Wiring and data-access tests the sweep refactor must not regress.

Review of the first cut found that moving the sweep out of app/main.py had
silently dropped the @app.on_event("startup") decorator, which would have
stopped every background worker in production while the hash guard was
re-pinned on the broken file. These tests make that class of mistake loud.
"""

from __future__ import annotations

import ast
import inspect
import os
import time

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest

from app import main as app_main
from app.db import calls as calls_module
from app.webhooks import twilio_incoming


def test_startup_and_shutdown_handlers_are_registered():
    names = [h.__name__ for h in app_main.app.router.on_startup]
    assert "startup" in names
    assert [h.__name__ for h in app_main.app.router.on_shutdown] == ["shutdown"]


@pytest.mark.asyncio
async def test_cleanup_loop_delegates_to_the_tested_sweep(monkeypatch):
    from unittest.mock import AsyncMock
    from app.services import background_maintenance, number_release
    run = AsyncMock()
    monkeypatch.setattr(number_release, "run_expired_contractor_cleanup_once", run)
    await background_maintenance.expired_contractors_once()
    run.assert_awaited_once_with()
    delegated = AsyncMock()
    monkeypatch.setattr(app_main, "_periodic_maintenance", delegated)
    await app_main._expired_contractor_cleanup()
    delegated.assert_awaited_once_with("expired_contractors_once", 6 * 3600)


@pytest.mark.asyncio
async def test_startup_schedules_the_cleanup_loop(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(app_main.settings, "background_worker_mode", "embedded")
    monkeypatch.setattr(app_main, "validate_runtime_safety", lambda: None)
    for field in ("twilio_account_sid", "twilio_auth_token", "anthropic_api_key",
                  "deepgram_api_key", "elevenlabs_api_key", "api_bearer_token"):
        monkeypatch.setattr(app_main.settings, field, "fictional")
    monkeypatch.setattr(app_main, "_housekeeping_tasks", {})
    cleanup = AsyncMock()
    cleanup.__name__ = "_expired_contractor_cleanup"
    monkeypatch.setattr(app_main, "_expired_contractor_cleanup", cleanup)
    try:
        await app_main.startup()
        assert "_expired_contractor_cleanup" in app_main._housekeeping_tasks
    finally:
        await app_main.shutdown()


def test_incoming_call_awaits_deadline_bounded_evidence_before_the_subscription_gate():
    """A release race needs a real transaction, while routing keeps its deadline."""
    src = inspect.getsource(twilio_incoming.handle_incoming_call)
    stamp = src.index("_record_inbound_call_evidence(")
    gate = src.index("evaluate_subscription_access(contractor, now)")
    assert stamp < gate
    assert "asyncio.wait_for(" in src
    assert "INBOUND_EVIDENCE_RPC_TIMEOUT_SECONDS" in inspect.getsource(twilio_incoming)
    assert "asyncio.create_task(_record_inbound_call_evidence" not in src
    assert "direct ring fallback" in src


def test_inbound_stamp_helper_is_a_module_level_coroutine():
    tree = ast.parse(inspect.getsource(twilio_incoming))
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert "_record_inbound_call_evidence" in names


@pytest.mark.asyncio
async def test_inbound_stamp_uses_the_deadline_aware_database_helper(monkeypatch):
    now = time.time()
    calls = []

    async def fake_record(contractor_id, seen_at, *, rpc_timeout):
        calls.append((contractor_id, seen_at, rpc_timeout))
        return True

    import app.db.contractors as contractors
    monkeypatch.setattr(contractors, "record_inbound_call_evidence", fake_record)

    assert await twilio_incoming._record_inbound_call_evidence("c1", now)
    assert calls == [(
        "c1",
        now,
        twilio_incoming.INBOUND_EVIDENCE_RPC_TIMEOUT_SECONDS,
    )]


@pytest.mark.asyncio
async def test_inbound_stamp_timeout_returns_controlled_direct_ring(monkeypatch):
    class _Request:
        async def form(self):
            return {
                "CallSid": "CA-stamp-timeout",
                "From": "+15555550100",
                "To": "+15555550199",
            }

    async def fake_lookup(_number):
        return {
            "contractor_id": "c1",
            "owner_name": "Owner",
            "owner_phone": "+15555550123",
            "business_name": "Test Business",
        }

    async def slow_record(*_args, **_kwargs):
        await __import__("asyncio").sleep(0.1)
        return True

    import app.db.contractors as contractors
    monkeypatch.setattr(contractors, "get_contractor_by_twilio_number", fake_lookup)
    monkeypatch.setattr(contractors, "record_inbound_call_evidence", slow_record)
    monkeypatch.setattr(twilio_incoming, "INBOUND_EVIDENCE_TIMEOUT_SECONDS", 0.01)

    started = time.monotonic()
    response = await twilio_incoming.handle_incoming_call(_Request(), None)
    elapsed = time.monotonic() - started

    assert elapsed < 0.08
    assert response.status_code == 200
    assert b"<Dial" in response.body
    assert b"+15555550123" in response.body


# --- latest_call_timestamp -------------------------------------------------


class _Snap:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class _Query:
    def __init__(self, docs, raise_error=None):
        self._docs = docs
        self._raise = raise_error
        self.calls: list[tuple] = []

    def where(self, *a, **k):
        self.calls.append(("where", k.get("filter")))
        return self

    def order_by(self, *a, **k):
        self.calls.append(("order_by", a, k))
        return self

    def limit(self, n):
        self.calls.append(("limit", n))
        return self

    def stream(self):
        if self._raise:
            raise self._raise
        return iter(self._docs)


class _Db:
    def __init__(self, query):
        self._query = query

    def collection(self, name):
        assert name == "calls"
        return self._query


@pytest.mark.asyncio
async def test_latest_call_timestamp_returns_none_when_no_calls(monkeypatch):
    q = _Query([])
    monkeypatch.setattr(calls_module, "get_firestore_client", lambda: _Db(q))
    assert await calls_module.latest_call_timestamp("c1") is None
    assert ("limit", 1) in q.calls and any(c[0] == "order_by" for c in q.calls)
    assert sum(1 for c in q.calls if c[0] == "where") == 2


@pytest.mark.asyncio
async def test_latest_call_timestamp_returns_the_newest_stored_value(monkeypatch):
    q = _Query([_Snap({"timestamp": 1700000000.0}), _Snap({"timestamp": 1600000000.0})])
    monkeypatch.setattr(calls_module, "get_firestore_client", lambda: _Db(q))
    assert await calls_module.latest_call_timestamp("c1") == 1700000000.0


@pytest.mark.asyncio
async def test_latest_call_timestamp_raises_instead_of_pretending_silence(monkeypatch):
    """A failed lookup must not look like 'no calls'; the sweep relies on this to fail closed."""
    q = _Query([], raise_error=RuntimeError("index missing"))
    monkeypatch.setattr(calls_module, "get_firestore_client", lambda: _Db(q))
    with pytest.raises(RuntimeError):
        await calls_module.latest_call_timestamp("c1")
