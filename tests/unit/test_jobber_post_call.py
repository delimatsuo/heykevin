"""Post-call Jobber lead capture behavior."""

import inspect
import os
import time

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.db import jobs
from app.services import post_call


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
        self.get_transactions = []

    def get(self, transaction=None):
        self.get_transactions.append(transaction)
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
    assert fake_doc.get_transactions == [fake_db.tx]


@pytest.mark.asyncio
async def test_claim_jobber_sync_skips_missing_doc(monkeypatch):
    fake_doc = _FakeDoc(None)
    fake_db = _FakeDb(fake_doc)
    monkeypatch.setattr(jobs, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(jobs.firestore_module, "transactional", lambda fn: fn)

    claimed = await jobs.claim_jobber_sync("job-1")

    assert claimed is False
    assert fake_doc.updates == []
    assert fake_doc.get_transactions == [fake_db.tx]


@pytest.mark.asyncio
async def test_claim_jobber_sync_skips_in_progress(monkeypatch):
    fake_doc = _FakeDoc({"jobber_sync_status": "in_progress", "jobber_sync_started_at": 12300.0})
    fake_db = _FakeDb(fake_doc)
    monkeypatch.setattr(jobs, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(jobs.firestore_module, "transactional", lambda fn: fn)
    monkeypatch.setattr(time, "time", lambda: 12345.0)

    claimed = await jobs.claim_jobber_sync("job-1")

    assert claimed is False
    assert fake_doc.updates == []
    assert fake_doc.get_transactions == [fake_db.tx]


@pytest.mark.asyncio
async def test_claim_jobber_sync_reclaims_stale_in_progress(monkeypatch):
    fake_doc = _FakeDoc({"jobber_sync_status": "in_progress", "jobber_sync_started_at": 10000.0})
    fake_db = _FakeDb(fake_doc)
    monkeypatch.setattr(jobs, "get_firestore_client", lambda: fake_db)
    monkeypatch.setattr(jobs.firestore_module, "transactional", lambda fn: fn)
    monkeypatch.setattr(time, "time", lambda: 12345.0)

    claimed = await jobs.claim_jobber_sync("job-1")

    assert claimed is True
    assert fake_doc.updates == [
        {"jobber_sync_status": "in_progress", "jobber_sync_started_at": 12345.0}
    ]
    assert fake_doc.get_transactions == [fake_db.tx]


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
    assert fake_doc.get_transactions == [fake_db.tx]


def _lead_job_data(**overrides):
    data = {
        "call_sid": "CA123",
        "call_type": "service_request",
        "caller_name": "Maya Patel",
        "caller_phone": "+15551234567",
        "callback_number": "+15557654321",
        "address": "123 Main St",
        "urgency": "same_day",
        "issue_description": "Kitchen sink is leaking",
        "message": "Water is pooling under the cabinet.",
        "transcript": "Caller: My kitchen sink is leaking.\nKevin: I can pass that along.",
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_process_business_schedules_jobber_lead_capture_when_enabled(monkeypatch):
    captured = {}

    async def fake_extract_job_card(transcript_text, caller_phone, contractor=None):
        return _lead_job_data(call_sid="", caller_phone=caller_phone)

    async def fake_capture_jobber_lead(contractor, job_data, job_id):
        captured["contractor"] = contractor
        captured["job_data"] = dict(job_data)
        captured["job_id"] = job_id

    def fake_create_task(coro):
        assert inspect.iscoroutine(coro)
        captured["task"] = coro
        return coro

    async def fake_save_call(*args, **kwargs):
        return None

    async def fake_send_sms(*args, **kwargs):
        return True

    monkeypatch.setattr(post_call, "extract_job_card", fake_extract_job_card)
    monkeypatch.setattr(post_call.call_db, "save_call", fake_save_call)
    monkeypatch.setattr(post_call.job_db, "get_job_by_call_sid", lambda call_sid: _async_return(None))
    monkeypatch.setattr(post_call.job_db, "save_job", lambda job_data: _async_return("job-1"))
    monkeypatch.setattr(post_call, "_send_summary_push", lambda *args, **kwargs: _async_return(None))
    monkeypatch.setattr(post_call, "send_sms", fake_send_sms)
    monkeypatch.setattr(post_call, "_capture_jobber_lead", fake_capture_jobber_lead)
    monkeypatch.setattr(post_call.asyncio, "create_task", fake_create_task)

    contractor = {
        "contractor_id": "contractor-1",
        "jobber_access_token": "jobber-token",
        "jobber_lead_capture_enabled": True,
    }

    await post_call._process_business(
        "transcript",
        "+15551234567",
        "CA123",
        "",
        "+15550000000",
        contractor,
    )

    assert inspect.iscoroutine(captured["task"])
    await captured["task"]
    assert captured["contractor"] is contractor
    assert captured["job_id"] == "job-1"
    assert captured["job_data"]["call_sid"] == "CA123"
    assert captured["job_data"]["transcript"] == "transcript"


@pytest.mark.asyncio
async def test_capture_jobber_lead_success_existing_customer(monkeypatch):
    job_updates = []
    call_updates = []
    requests = []
    notes = []

    monkeypatch.setattr(post_call.time, "time", lambda: 12345.0)
    monkeypatch.setattr(post_call.job_db, "claim_jobber_sync", lambda job_id: _async_return(True))
    monkeypatch.setattr(post_call.job_db, "update_job", lambda job_id, updates: _record_async(job_updates, job_id, updates))
    monkeypatch.setattr(post_call.call_db, "save_call", lambda call_sid, updates: _record_async(call_updates, call_sid, updates))
    monkeypatch.setattr(
        post_call.jobber_service,
        "lookup_customer",
        lambda contractor, phone: _async_return({
            "id": "client-1",
            "clientProperties": {"nodes": [{"id": "property-1"}]},
        }),
    )
    monkeypatch.setattr(
        post_call.jobber_service,
        "create_client",
        lambda *args, **kwargs: pytest.fail("existing customers should not create a Jobber client"),
    )

    async def fake_create_request(contractor, request_data):
        requests.append(request_data)
        return {
            "id": "request-1",
            "jobberWebUri": "https://secure.getjobber.com/requests/request-1",
        }

    async def fake_create_request_note(contractor, request_id, note):
        notes.append((request_id, note))
        return "note-1"

    monkeypatch.setattr(post_call.jobber_service, "create_request", fake_create_request)
    monkeypatch.setattr(post_call.jobber_service, "create_request_note", fake_create_request_note)

    await post_call._capture_jobber_lead(
        {"jobber_access_token": "jobber-token", "jobber_lead_capture_enabled": True},
        _lead_job_data(),
        "job-1",
    )

    assert requests == [{
        "client_id": "client-1",
        "property_id": "property-1",
        "title": "Kitchen sink is leaking",
    }]
    assert notes[0][0] == "request-1"
    assert "Source: Hey Kevin" in notes[0][1]
    assert "Caller: Maya Patel" in notes[0][1]
    assert "Transcript:" in notes[0][1]
    assert job_updates == [("job-1", {
        "jobber_sync_status": "succeeded",
        "jobber_request_id": "request-1",
        "jobber_request_url": "https://secure.getjobber.com/requests/request-1",
        "jobber_client_id": "client-1",
        "jobber_note_id": "note-1",
        "jobber_synced_at": 12345.0,
    })]
    assert call_updates == [("CA123", job_updates[0][1])]


@pytest.mark.asyncio
async def test_capture_jobber_lead_success_creates_customer_when_lookup_misses(monkeypatch):
    requests = []
    job_updates = []

    monkeypatch.setattr(post_call.time, "time", lambda: 22222.0)
    monkeypatch.setattr(post_call.job_db, "claim_jobber_sync", lambda job_id: _async_return(True))
    monkeypatch.setattr(post_call.job_db, "update_job", lambda job_id, updates: _record_async(job_updates, job_id, updates))
    monkeypatch.setattr(post_call.call_db, "save_call", lambda *args, **kwargs: _async_return(None))
    monkeypatch.setattr(post_call.jobber_service, "lookup_customer", lambda *args, **kwargs: _async_return(None))
    monkeypatch.setattr(
        post_call.jobber_service,
        "create_client",
        lambda contractor, job_data: _async_return({"id": "client-new", "property_id": "property-new"}),
    )
    monkeypatch.setattr(
        post_call.jobber_service,
        "create_request",
        lambda contractor, request_data: _record_and_return_async(
            requests,
            request_data,
            {"id": "request-new", "jobberWebUri": ""},
        ),
    )
    monkeypatch.setattr(post_call.jobber_service, "create_request_note", lambda *args, **kwargs: _async_return(None))

    await post_call._capture_jobber_lead(
        {"jobber_access_token": "jobber-token", "jobber_lead_capture_enabled": True},
        _lead_job_data(),
        "job-1",
    )

    assert requests == [{
        "client_id": "client-new",
        "property_id": "property-new",
        "title": "Kitchen sink is leaking",
    }]
    assert job_updates == [("job-1", {
        "jobber_sync_status": "succeeded",
        "jobber_request_id": "request-new",
        "jobber_client_id": "client-new",
        "jobber_synced_at": 22222.0,
    })]


@pytest.mark.asyncio
async def test_capture_jobber_lead_call_mirror_failure_does_not_mark_failed(monkeypatch):
    job_updates = []

    async def fail_save_call(*args, **kwargs):
        raise RuntimeError("call mirror unavailable")

    monkeypatch.setattr(post_call.time, "time", lambda: 44444.0)
    monkeypatch.setattr(post_call.job_db, "claim_jobber_sync", lambda job_id: _async_return(True))
    monkeypatch.setattr(post_call.job_db, "update_job", lambda job_id, updates: _record_async(job_updates, job_id, updates))
    monkeypatch.setattr(post_call.call_db, "save_call", fail_save_call)
    monkeypatch.setattr(
        post_call.jobber_service,
        "lookup_customer",
        lambda *args, **kwargs: _async_return({"id": "client-1"}),
    )
    monkeypatch.setattr(
        post_call.jobber_service,
        "create_request",
        lambda *args, **kwargs: _async_return({"id": "request-1"}),
    )
    monkeypatch.setattr(post_call.jobber_service, "create_request_note", lambda *args, **kwargs: _async_return(None))

    await post_call._capture_jobber_lead(
        {"jobber_access_token": "jobber-token", "jobber_lead_capture_enabled": True},
        _lead_job_data(),
        "job-1",
    )

    assert job_updates == [("job-1", {
        "jobber_sync_status": "succeeded",
        "jobber_request_id": "request-1",
        "jobber_client_id": "client-1",
        "jobber_synced_at": 44444.0,
    })]


@pytest.mark.asyncio
async def test_capture_jobber_lead_disabled_flag_does_not_claim_or_sync(monkeypatch):
    monkeypatch.setattr(
        post_call.job_db,
        "claim_jobber_sync",
        lambda *args, **kwargs: pytest.fail("disabled flag must not claim sync"),
    )
    monkeypatch.setattr(
        post_call.jobber_service,
        "lookup_customer",
        lambda *args, **kwargs: pytest.fail("disabled flag must not call Jobber"),
    )

    await post_call._capture_jobber_lead(
        {"jobber_access_token": "jobber-token"},
        _lead_job_data(),
        "job-1",
    )


@pytest.mark.asyncio
async def test_capture_jobber_lead_claim_false_skips_jobber_api(monkeypatch):
    updates = []

    monkeypatch.setattr(post_call.job_db, "claim_jobber_sync", lambda job_id: _async_return(False))
    monkeypatch.setattr(post_call.job_db, "update_job", lambda job_id, data: _record_async(updates, job_id, data))
    monkeypatch.setattr(
        post_call.jobber_service,
        "lookup_customer",
        lambda *args, **kwargs: pytest.fail("claim false must not call Jobber"),
    )

    await post_call._capture_jobber_lead(
        {"jobber_access_token": "jobber-token", "jobber_lead_capture_enabled": True},
        _lead_job_data(),
        "job-1",
    )

    assert updates == []


@pytest.mark.asyncio
async def test_capture_jobber_lead_failure_after_claim_marks_job_failed(monkeypatch):
    job_updates = []
    call_updates = []

    monkeypatch.setattr(post_call.time, "time", lambda: 33333.0)
    monkeypatch.setattr(post_call.job_db, "claim_jobber_sync", lambda job_id: _async_return(True))
    monkeypatch.setattr(post_call.job_db, "update_job", lambda job_id, updates: _record_async(job_updates, job_id, updates))
    monkeypatch.setattr(post_call.call_db, "save_call", lambda call_sid, updates: _record_async(call_updates, call_sid, updates))
    monkeypatch.setattr(post_call.jobber_service, "lookup_customer", lambda *args, **kwargs: _async_return(None))
    monkeypatch.setattr(post_call.jobber_service, "create_client", lambda *args, **kwargs: _async_return(None))

    await post_call._capture_jobber_lead(
        {"jobber_access_token": "jobber-token", "jobber_lead_capture_enabled": True},
        _lead_job_data(),
        "job-1",
    )

    assert job_updates == [("job-1", {
        "jobber_sync_status": "failed",
        "jobber_sync_error": "client_missing",
        "jobber_sync_finished_at": 33333.0,
    })]
    assert call_updates == [("CA123", job_updates[0][1])]


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner()


def _record_async(records, key, value):
    async def _inner():
        records.append((key, value))

    return _inner()


def _record_and_return_async(records, value, result):
    async def _inner():
        records.append(value)
        return result

    return _inner()
