"""Post-call Jobber lead capture behavior."""

import time

import pytest

from app.db import jobs


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakeDoc:
    def __init__(self, data):
        self.data = data
        self.updates = []

    def get(self, transaction=None):
        return _FakeSnapshot(self.data)


class _FakeCollection:
    def __init__(self, doc):
        self.doc = doc

    def document(self, job_id):
        assert job_id == "job-1"
        return self.doc


class _FakeTransaction:
    def __init__(self):
        self.updates = []

    def update(self, ref, updates):
        self.updates.append((ref, updates))
        ref.updates.append(updates)


class _FakeDb:
    def __init__(self, doc):
        self.doc = doc
        self.tx = _FakeTransaction()

    def collection(self, name):
        assert name == jobs.COLLECTION
        return _FakeCollection(self.doc)

    def transaction(self):
        return self.tx


@pytest.mark.asyncio
async def test_claim_jobber_sync_skips_existing_request(monkeypatch):
    fake_doc = _FakeDoc({"jobber_request_id": "request-1"})
    fake_db = _FakeDb(fake_doc)
    monkeypatch.setattr(jobs, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(jobs.firestore_module, "transactional", lambda fn: fn)

    claimed = await jobs.claim_jobber_sync("job-1")

    assert claimed is False
    assert fake_doc.updates == []


@pytest.mark.asyncio
async def test_claim_jobber_sync_marks_in_progress(monkeypatch):
    fake_doc = _FakeDoc({"call_sid": "CA123"})
    fake_db = _FakeDb(fake_doc)
    monkeypatch.setattr(jobs, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(jobs.firestore_module, "transactional", lambda fn: fn)
    monkeypatch.setattr(time, "time", lambda: 12345.0)

    claimed = await jobs.claim_jobber_sync("job-1")

    assert claimed is True
    assert fake_doc.updates == [
        {"jobber_sync_status": "in_progress", "jobber_sync_started_at": 12345.0}
    ]
