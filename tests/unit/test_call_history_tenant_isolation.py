"""Tenant isolation for inbound trust-history lookups."""

from __future__ import annotations

import inspect
import os
import re

import pytest
from google.cloud.firestore_v1.base_query import FieldFilter

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.db import calls as calls_db
from app.services import lookup as lookup_service
from app.webhooks import twilio_incoming


class _Doc:
    def __init__(self, data: dict):
        self._data = data

    def to_dict(self) -> dict:
        return dict(self._data)


class _Query:
    def __init__(self, docs: list[dict]):
        self._docs = docs
        self.filters: list[tuple[str, str, object]] = []
        self.order: tuple[str, object] | None = None
        self.limit_n: int | None = None

    def where(self, *, filter=None):
        assert isinstance(filter, FieldFilter)
        self.filters.append((filter.field_path, filter.op_string, filter.value))
        return self

    def order_by(self, field, direction=None):
        self.order = (field, direction)
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def stream(self):
        rows = self._docs
        for field, op, value in self.filters:
            assert op == "=="
            rows = [row for row in rows if row.get(field) == value]
        return [_Doc(row) for row in rows]


class _Collection:
    def __init__(self, docs: list[dict]):
        self.query = _Query(docs)

    def where(self, *, filter=None):
        return self.query.where(filter=filter)


class _DB:
    def __init__(self, docs: list[dict]):
        self.docs = docs
        self.collection_names: list[str] = []
        self.collection_obj = _Collection(docs)

    def collection(self, name):
        self.collection_names.append(name)
        return self.collection_obj


def test_get_call_history_requires_keyword_contractor_id():
    params = inspect.signature(calls_db.get_call_history).parameters
    assert "contractor_id" in params
    assert params["contractor_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["contractor_id"].default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_get_call_history_does_not_query_when_contractor_id_blank(monkeypatch):
    db = _DB([])
    monkeypatch.setattr(calls_db, "get_firestore_client", lambda: db)

    rows = await calls_db.get_call_history("+15551230000", contractor_id="", limit=10)

    assert rows == []
    assert db.collection_names == []


@pytest.mark.asyncio
async def test_get_call_history_excludes_other_tenant_outcomes(monkeypatch):
    """Tenant A pickups must not become tenant B trust-history."""
    phone = "+15551230000"
    docs = [
        {
            "caller_phone": phone,
            "contractor_id": "tenant-a",
            "outcome": "picked_up",
            "timestamp": 200,
        },
        {
            "caller_phone": phone,
            "contractor_id": "tenant-b",
            "outcome": "ignored",
            "timestamp": 100,
        },
        {
            "caller_phone": "+15559990000",
            "contractor_id": "tenant-b",
            "outcome": "picked_up",
            "timestamp": 300,
        },
    ]
    db = _DB(docs)
    monkeypatch.setattr(calls_db, "get_firestore_client", lambda: db)

    rows = await calls_db.get_call_history(phone, contractor_id="tenant-b", limit=10)

    assert [row["outcome"] for row in rows] == ["ignored"]
    assert ("contractor_id", "==", "tenant-b") in db.collection_obj.query.filters
    assert ("caller_phone", "==", phone) in db.collection_obj.query.filters
    assert db.collection_obj.query.limit_n == 10


def test_incoming_webhook_passes_contractor_id_to_call_history():
    source = inspect.getsource(twilio_incoming)
    assert re.search(
        r"get_call_history\(\s*caller_phone\s*,\s*contractor_id\s*=\s*contractor_id",
        source,
    )
    assert re.search(r"get_call_history\(\s*caller_phone\s*,\s*limit\s*=", source) is None


@pytest.mark.asyncio
async def test_lookup_history_does_not_read_calls_without_contractor(monkeypatch):
    async def _should_not_run(*args, **kwargs):
        raise AssertionError("unscoped call history lookup")

    monkeypatch.setattr(lookup_service, "get_call_history", _should_not_run)

    assert await lookup_service._lookup_history("+15551230000") == {}
    assert await lookup_service._lookup_history("+15551230000", contractor_id="") == {}


@pytest.mark.asyncio
async def test_lookup_history_forwards_contractor_id(monkeypatch):
    captured: dict = {}

    async def fake_history(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return [{"outcome": "picked_up"}]

    monkeypatch.setattr(lookup_service, "get_call_history", fake_history)

    result = await lookup_service._lookup_history(
        "+15551230000",
        contractor_id="tenant-b",
    )

    assert captured["kwargs"]["contractor_id"] == "tenant-b"
    assert result["times_picked_up"] == 1
    assert result["times_ignored"] == 0
