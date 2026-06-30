import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services import sms
from app.services.gated_actions import ActionKey, GateContext


class _Messages:
    def __init__(self):
        self.created = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return type("Message", (), {"sid": "SM123"})()


class _Client:
    messages = _Messages()


@pytest.mark.asyncio
async def test_send_sms_with_disabled_gate_does_not_call_twilio(monkeypatch):
    client = _Client()
    monkeypatch.setattr(sms, "Client", lambda *_args, **_kwargs: client)

    result = await sms.send_sms(
        "+15551234567",
        "hello",
        from_number="+15557654321",
        contractor={"contractor_id": "c1"},
        action=ActionKey.CALLER_TEXT_REPLY,
        gate_context=GateContext(source="ios", actor="owner", idempotency_key="msg-1", owner_confirmed=True),
    )

    assert result is False
    assert client.messages.created == []


@pytest.mark.asyncio
async def test_send_sms_with_enabled_gate_calls_twilio(monkeypatch):
    client = _Client()
    monkeypatch.setattr(sms, "Client", lambda *_args, **_kwargs: client)

    result = await sms.send_sms(
        "+15551234567",
        "hello",
        from_number="+15557654321",
        contractor={
            "contractor_id": "c1",
            "gated_actions": {ActionKey.CALLER_TEXT_REPLY.value: True},
            "sms_compliance_status": "approved",
        },
        action=ActionKey.CALLER_TEXT_REPLY,
        gate_context=GateContext(source="ios", actor="owner", idempotency_key="msg-1", owner_confirmed=True),
    )

    assert result is True
    assert client.messages.created[0]["to"] == "+15551234567"
