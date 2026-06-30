import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.api import voip as voip_api
from app.services.gated_actions import ActionKey
from app.webhooks import telegram_callback


class _ActiveCall:
    contractor_id = "c1"
    caller_phone = "+15551234567"
    caller_name = "Caller"


@pytest.mark.asyncio
async def test_voip_text_reply_fails_closed_without_sms_gate(monkeypatch):
    sent = []

    async def fake_get_active_call(_sid):
        return _ActiveCall()

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "twilio_number": "+15559999999",
            "gated_actions": {ActionKey.CALLER_TEXT_REPLY.value: True},
        }

    async def fake_send_sms(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    monkeypatch.setattr("app.db.cache.get_active_call", fake_get_active_call)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.services.sms.send_sms", fake_send_sms)

    result = await voip_api._handle_text_reply("CA123", "hello", "c1")

    assert result["status"] == "error"
    assert result["message"] == "Texting is not enabled for this account."
    assert sent == []


@pytest.mark.asyncio
async def test_voip_text_reply_allowed_path_forwards_gate_metadata(monkeypatch):
    sent = []
    contractor = {
        "contractor_id": "c1",
        "twilio_number": "+15559999999",
        "sms_compliance_status": "approved",
        "gated_actions": {ActionKey.CALLER_TEXT_REPLY.value: True},
    }

    async def fake_get_active_call(_sid):
        return _ActiveCall()

    async def fake_get_contractor(_cid):
        return contractor

    async def fake_send_sms(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    monkeypatch.setattr("app.db.cache.get_active_call", fake_get_active_call)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.services.sms.send_sms", fake_send_sms)

    result = await voip_api._handle_text_reply("CA123", "hello", "c1")

    assert result == {"status": "ok"}
    assert len(sent) == 1
    _args, kwargs = sent[0]
    assert kwargs["contractor"] == contractor
    assert kwargs["action"] == ActionKey.CALLER_TEXT_REPLY
    assert kwargs["gate_context"].source == "ios"
    assert kwargs["gate_context"].idempotency_key == "CA123:text_reply"


@pytest.mark.asyncio
async def test_telegram_active_text_reply_denied_does_not_send_sms(monkeypatch):
    sent = []
    answered = []

    async def fake_get_active_call(_sid):
        return _ActiveCall()

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "gated_actions": {ActionKey.CALLER_TEXT_REPLY.value: True},
        }

    async def fake_send_text_reply(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    async def fake_answer_callback_query(callback_id, message):
        answered.append((callback_id, message))

    monkeypatch.setattr(telegram_callback, "get_active_call", fake_get_active_call)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.services.sms.send_text_reply", fake_send_text_reply)
    monkeypatch.setattr(telegram_callback, "answer_callback_query", fake_answer_callback_query)

    await telegram_callback._handle_text_reply("CA123", 123)

    assert sent == []
    assert answered == [("", "Texting is not enabled for this account.")]


@pytest.mark.asyncio
async def test_telegram_post_call_text_them_denied_does_not_send_sms(monkeypatch):
    sent = []
    answered = []

    async def fake_get_call(_sid):
        return {
            "contractor_id": "c1",
            "caller_phone": "+15551234567",
        }

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "gated_actions": {ActionKey.CALLER_AUTO_REPLY.value: True},
        }

    async def fake_send_followup_text(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    async def fake_answer_callback_query(callback_id, message):
        answered.append((callback_id, message))

    monkeypatch.setattr("app.db.calls.get_call", fake_get_call)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.services.sms.send_followup_text", fake_send_followup_text)
    monkeypatch.setattr(telegram_callback, "answer_callback_query", fake_answer_callback_query)

    await telegram_callback._handle_text_them("CA123", 123)

    assert sent == []
    assert answered == [("", "Texting is not enabled for this account.")]


@pytest.mark.asyncio
async def test_telegram_active_text_reply_missing_contractor_fails_closed_before_sms(monkeypatch):
    sent = []
    answered = []

    async def fake_get_active_call(_sid):
        return _ActiveCall()

    async def fake_get_contractor(_cid):
        return None

    async def fake_send_text_reply(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    async def fake_answer_callback_query(callback_id, message):
        answered.append((callback_id, message))

    monkeypatch.setattr(telegram_callback, "get_active_call", fake_get_active_call)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.services.sms.send_text_reply", fake_send_text_reply)
    monkeypatch.setattr(telegram_callback, "answer_callback_query", fake_answer_callback_query)

    await telegram_callback._handle_text_reply("CA123", 123)

    assert sent == []
    assert answered == [("", "No account owner was found for this action.")]
