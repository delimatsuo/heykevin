import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

import pytest

from app.api import voip as voip_api
from app.services.gated_actions import ActionKey


class _ActiveCall:
    contractor_id = "c1"
    caller_phone = "+15551234567"


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
