import os

import pytest
from google.api_core.exceptions import FailedPrecondition

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.db import jobs as jobs_db


class _Doc:
    def __init__(self, doc_id: str, data: dict):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _Query:
    def __init__(
        self,
        docs: list[_Doc],
        *,
        contractor_id: str = "",
        ordered: bool = False,
        limit_value: int | None = None,
        fail_ordered_stream: bool = False,
    ):
        self._docs = docs
        self._contractor_id = contractor_id
        self._ordered = ordered
        self._limit_value = limit_value
        self._fail_ordered_stream = fail_ordered_stream

    def where(self, field_path, op_string=None, value=None):
        assert field_path == "contractor_id"
        assert op_string == "=="
        return _Query(
            self._docs,
            contractor_id=value,
            ordered=self._ordered,
            limit_value=self._limit_value,
            fail_ordered_stream=self._fail_ordered_stream,
        )

    def order_by(self, field_path, direction=None):
        assert field_path == "created_at"
        return _Query(
            self._docs,
            contractor_id=self._contractor_id,
            ordered=True,
            limit_value=self._limit_value,
            fail_ordered_stream=self._fail_ordered_stream,
        )

    def limit(self, value):
        return _Query(
            self._docs,
            contractor_id=self._contractor_id,
            ordered=self._ordered,
            limit_value=value,
            fail_ordered_stream=self._fail_ordered_stream,
        )

    def stream(self):
        if self._ordered and self._fail_ordered_stream:
            raise FailedPrecondition("missing composite index")
        docs = self._docs
        if self._contractor_id:
            docs = [doc for doc in docs if doc.to_dict().get("contractor_id") == self._contractor_id]
        if self._ordered:
            docs = sorted(docs, key=lambda doc: doc.to_dict().get("created_at", 0), reverse=True)
        if self._limit_value is not None:
            docs = docs[: self._limit_value]
        return docs


class _DB:
    def __init__(self, docs: list[_Doc]):
        self._docs = docs

    def collection(self, name):
        assert name == "jobs"
        return _Query(self._docs, fail_ordered_stream=True)


@pytest.mark.asyncio
async def test_list_jobs_falls_back_when_composite_index_is_missing(monkeypatch):
    docs = [
        _Doc("old", {"contractor_id": "c1", "created_at": 100, "status": "new"}),
        _Doc("other", {"contractor_id": "c2", "created_at": 300, "status": "new"}),
        _Doc("new", {"contractor_id": "c1", "created_at": 200, "status": "new"}),
    ]
    monkeypatch.setattr(jobs_db, "get_firestore_client", lambda: _DB(docs))

    jobs = await jobs_db.list_jobs(limit=20, contractor_id="c1")

    assert [job["job_id"] for job in jobs] == ["new", "old"]
