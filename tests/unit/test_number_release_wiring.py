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


def test_cleanup_loop_delegates_to_the_tested_sweep():
    src = inspect.getsource(app_main._expired_contractor_cleanup)
    assert "run_expired_contractor_cleanup_once" in src
    assert "asyncio.sleep(6 * 3600)" in src


def test_startup_schedules_the_cleanup_loop():
    src = inspect.getsource(app_main.startup)
    assert "_expired_contractor_cleanup()" in src


def test_incoming_call_stamps_inbound_evidence_before_the_subscription_gate():
    """Calls on expired accounts must be stamped, so the stamp has to precede the gate."""
    src = inspect.getsource(twilio_incoming.handle_incoming_call)
    stamp = src.index("_record_inbound_call_evidence(contractor_id, time.time())")
    gate = src.index("evaluate_subscription_access(contractor, now)")
    assert stamp < gate


def test_inbound_stamp_helper_is_a_module_level_coroutine():
    tree = ast.parse(inspect.getsource(twilio_incoming))
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert "_record_inbound_call_evidence" in names


@pytest.mark.asyncio
async def test_inbound_stamp_writes_and_throttles(monkeypatch):
    now = time.time()
    doc = {"last_inbound_call_at": None}
    writes: list[dict] = []

    async def fake_get(cid):
        return dict(doc)

    async def fake_update(cid, updates):
        writes.append(updates)
        doc.update(updates)
        return True

    import app.db.contractors as contractors
    monkeypatch.setattr(contractors, "get_contractor", fake_get)
    monkeypatch.setattr(contractors, "update_contractor", fake_update)

    await twilio_incoming._record_inbound_call_evidence("c1", now)
    await twilio_incoming._record_inbound_call_evidence("c1", now + 60)  # inside the hourly throttle
    await twilio_incoming._record_inbound_call_evidence("c1", now + 3601)

    assert writes == [{"last_inbound_call_at": now}, {"last_inbound_call_at": now + 3601}]


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
