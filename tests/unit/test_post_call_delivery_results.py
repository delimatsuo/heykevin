"""Structured post-call delivery outcomes honor provider return values."""

import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services import post_call


@pytest.mark.asyncio
async def test_personal_sms_false_return_produces_partial_result(monkeypatch):
    async def extract(*_args, **_kwargs):
        return {
            "caller_name": "Test Caller",
            "caller_phone": "test-caller-number",
            "issue_description": "Routine request",
            "call_type": "unknown",
        }

    async def save_call(*_args, **_kwargs):
        return True

    async def send_sms(*_args, **_kwargs):
        return False

    async def skip_push(*_args, **_kwargs):
        return None

    monkeypatch.setattr(post_call, "extract_job_card", extract)
    monkeypatch.setattr(post_call.call_db, "save_call", save_call)
    monkeypatch.setattr(post_call, "send_sms", send_sms)
    monkeypatch.setattr(post_call, "_send_summary_push", skip_push)

    result = await post_call.process_post_call(
        transcript_lines=["Caller: routine request"],
        caller_phone="test-caller-number",
        call_sid="CA_test",
        contractor_phone="test-owner-number",
        twilio_number="test-twilio-number",
        contractor={"effective_mode": "personal"},
    )

    assert result.status == "partial"
    assert result.completed_effects == ("call_record",)
    assert result.failed_effects == ("owner_sms",)


@pytest.mark.asyncio
async def test_missing_tenant_delivery_config_never_uses_process_fallback(monkeypatch):
    async def extract(*_args, **_kwargs):
        return {
            "caller_name": "Test Caller",
            "caller_phone": "test-caller-number",
            "issue_description": "Routine request",
            "call_type": "unknown",
        }

    async def save_call(*_args, **_kwargs):
        return True

    async def unexpected_send(*_args, **_kwargs):
        pytest.fail("post-call delivery must not use a process-wide fallback number")

    async def skip_effect(*_args, **_kwargs):
        return None

    monkeypatch.setattr(post_call, "extract_job_card", extract)
    monkeypatch.setattr(post_call.call_db, "save_call", save_call)
    monkeypatch.setattr(post_call, "send_sms", unexpected_send)
    monkeypatch.setattr(post_call, "_send_summary_push", skip_effect)
    monkeypatch.setattr(post_call, "_update_caller_contact", skip_effect)

    result = await post_call.process_post_call(
        transcript_lines=["Caller: routine request"],
        caller_phone="test-caller-number",
        call_sid="CA_test",
        contractor={
            "contractor_id": "contractor-test",
            "effective_mode": "personal",
        },
    )

    assert result.status == "partial"
    assert result.failed_effects == ("owner_sms",)


@pytest.mark.asyncio
async def test_failed_auto_reply_does_not_advance_rate_limit(monkeypatch):
    writes = []

    class Snapshot:
        exists = False

    class Document:
        def get(self):
            return Snapshot()

        def set(self, data):
            writes.append(data)

    class Collection:
        def document(self, _key):
            return Document()

    class Firestore:
        def collection(self, _name):
            return Collection()

    async def send_sms(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        "app.db.firestore_client.get_firestore_client",
        lambda: Firestore(),
    )
    monkeypatch.setattr(post_call, "send_sms", send_sms)

    sent = await post_call._send_auto_reply(
        "15555550123",
        {"business_name": "Test Business", "owner_name": "Test Owner"},
        "test-twilio-number",
        call_sid="CA_test",
    )

    assert sent is False
    assert writes == []


@pytest.mark.asyncio
async def test_caller_contact_history_replaces_same_call_instead_of_duplicating(
    monkeypatch,
):
    from app.db import contacts as contact_db

    writes = []

    async def get_contact(_contractor_id, _caller_phone):
        return {
            "caller_name": "Test Caller",
            "call_history": [
                {"call_sid": "CA_old", "summary": "Older request"},
                {"call_sid": "CA_test", "summary": "Stale request"},
                "malformed-entry",
            ],
        }

    async def upsert_contact(contractor_id, caller_phone, updates, *, merge):
        writes.append((contractor_id, caller_phone, updates, merge))
        return True

    monkeypatch.setattr(contact_db, "get_caller_contact", get_contact)
    monkeypatch.setattr(contact_db, "upsert_caller_contact", upsert_contact)
    monkeypatch.setattr(post_call.time, "time", lambda: 200.0)

    saved = await post_call._update_caller_contact(
        {
            "caller_name": "Test Caller",
            "caller_phone": "test-caller-number",
            "issue_description": "Current routine request",
        },
        "contractor-test",
        "CA_test",
    )

    assert saved is True
    assert len(writes) == 1
    history = writes[0][2]["call_history"]
    assert [item["call_sid"] for item in history] == ["CA_old", "CA_test"]
    assert history[-1]["summary"] == "Current routine request"
