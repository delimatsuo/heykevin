import os
from typing import Optional

import pytest
from google.api_core.exceptions import FailedPrecondition

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.db import calls as calls_db


class _Doc:
    def __init__(self, data: dict):
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _Query:
    def __init__(
        self,
        docs: list[_Doc],
        *,
        caller_phone: str = "",
        ordered: bool = False,
        limit_value: Optional[int] = None,
        fail_ordered_stream: bool = False,
    ):
        self._docs = docs
        self._caller_phone = caller_phone
        self._ordered = ordered
        self._limit_value = limit_value
        self._fail_ordered_stream = fail_ordered_stream

    def where(self, *args, **kwargs):
        filter_arg = kwargs.get("filter")
        if filter_arg is not None:
            assert filter_arg.field_path == "caller_phone"
            assert filter_arg.op_string == "=="
            caller_phone = filter_arg.value
        else:
            field_path, op_string, caller_phone = args
            assert field_path == "caller_phone"
            assert op_string == "=="
        return _Query(
            self._docs,
            caller_phone=caller_phone,
            ordered=self._ordered,
            limit_value=self._limit_value,
            fail_ordered_stream=self._fail_ordered_stream,
        )

    def order_by(self, field_path, direction=None):
        assert field_path == "timestamp"
        return _Query(
            self._docs,
            caller_phone=self._caller_phone,
            ordered=True,
            limit_value=self._limit_value,
            fail_ordered_stream=self._fail_ordered_stream,
        )

    def limit(self, value: int):
        return _Query(
            self._docs,
            caller_phone=self._caller_phone,
            ordered=self._ordered,
            limit_value=value,
            fail_ordered_stream=self._fail_ordered_stream,
        )

    def stream(self):
        if self._ordered and self._fail_ordered_stream:
            raise FailedPrecondition("missing composite index")
        docs = self._docs
        if self._caller_phone:
            docs = [
                doc for doc in docs
                if doc.to_dict().get("caller_phone") == self._caller_phone
            ]
        if self._ordered:
            docs = sorted(docs, key=lambda doc: doc.to_dict().get("timestamp", 0), reverse=True)
        if self._limit_value is not None:
            docs = docs[: self._limit_value]
        return docs


class _DB:
    def __init__(self, docs: list[_Doc]):
        self._docs = docs

    def collection(self, name: str):
        assert name == "calls"
        return _Query(self._docs, fail_ordered_stream=True)


@pytest.mark.asyncio
async def test_get_call_history_falls_back_when_composite_index_is_missing(monkeypatch):
    docs = [
        _Doc({"call_sid": "old", "caller_phone": "+15550001111", "timestamp": 100}),
        _Doc({"call_sid": "other", "caller_phone": "+15550002222", "timestamp": 300}),
        _Doc({"call_sid": "new", "caller_phone": "+15550001111", "timestamp": 200}),
    ]
    monkeypatch.setattr(calls_db, "get_firestore_client", lambda: _DB(docs))

    calls = await calls_db.get_call_history("+15550001111", limit=10)

    assert [call["call_sid"] for call in calls] == ["new", "old"]
