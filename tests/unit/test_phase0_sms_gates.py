import os
from unittest.mock import Mock

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services import sms
from app.services.gated_actions import ActionKey, GateContext


class _Messages:
    def __init__(self, events: list[str] | None = None):
        self.created = []
        self.events = events

    def create(self, **kwargs):
        if self.events is not None:
            self.events.append("send")
        self.created.append(kwargs)
        return type("Message", (), {"sid": "SM123"})()


class _Client:
    def __init__(self, events: list[str] | None = None):
        self.messages = _Messages(events)


def _allowed_contractor(action: ActionKey) -> dict:
    return {
        "contractor_id": "c1",
        "gated_actions": {action.value: True},
        "sms_compliance_status": "approved",
    }


def _gate_context(idempotency_key: str = "msg-1") -> GateContext:
    return GateContext(source="ios", actor="owner", idempotency_key=idempotency_key, owner_confirmed=True)


@pytest.mark.asyncio
async def test_send_sms_with_disabled_gate_does_not_call_twilio(monkeypatch):
    client_constructor = Mock(side_effect=AssertionError("Twilio Client should not be constructed"))
    record_gate_decision = Mock()
    monkeypatch.setattr(sms, "Client", client_constructor)
    monkeypatch.setattr(sms, "record_gate_decision", record_gate_decision)

    result = await sms.send_sms(
        "+15551234567",
        "hello",
        from_number="+15557654321",
        contractor={"contractor_id": "c1"},
        action=ActionKey.CALLER_TEXT_REPLY,
        gate_context=_gate_context(),
    )

    assert result is False
    client_constructor.assert_not_called()
    record_gate_decision.assert_called_once()
    assert record_gate_decision.call_args.kwargs["action"] == ActionKey.CALLER_TEXT_REPLY
    assert record_gate_decision.call_args.kwargs["contractor_id"] == "c1"
    assert record_gate_decision.call_args.kwargs["source"] == "ios"
    assert record_gate_decision.call_args.kwargs["resource_id"] == "msg-1"
    assert record_gate_decision.call_args.kwargs["decision"].allowed is False


@pytest.mark.asyncio
async def test_send_sms_with_enabled_gate_calls_twilio_and_records_decision(monkeypatch):
    events = []
    client = _Client(events)
    record_gate_decision = Mock(side_effect=lambda **_kwargs: events.append("audit"))

    def client_constructor(*_args, **_kwargs):
        events.append("client")
        assert "audit" in events
        return client

    monkeypatch.setattr(sms, "Client", client_constructor)
    monkeypatch.setattr(sms, "record_gate_decision", record_gate_decision)

    result = await sms.send_sms(
        "+15551234567",
        "hello",
        from_number="+15557654321",
        contractor=_allowed_contractor(ActionKey.CALLER_TEXT_REPLY),
        action=ActionKey.CALLER_TEXT_REPLY,
        gate_context=_gate_context(),
    )

    assert result is True
    assert events == ["audit", "client", "send"]
    assert client.messages.created == [
        {
            "to": "+15551234567",
            "from_": "+15557654321",
            "body": "hello",
        }
    ]
    record_gate_decision.assert_called_once()
    assert record_gate_decision.call_args.kwargs["action"] == ActionKey.CALLER_TEXT_REPLY
    assert record_gate_decision.call_args.kwargs["contractor_id"] == "c1"
    assert record_gate_decision.call_args.kwargs["source"] == "ios"
    assert record_gate_decision.call_args.kwargs["resource_id"] == "msg-1"
    assert record_gate_decision.call_args.kwargs["decision"].allowed is True


@pytest.mark.asyncio
async def test_send_mms_with_disabled_gate_does_not_call_twilio(monkeypatch):
    client_constructor = Mock(side_effect=AssertionError("Twilio Client should not be constructed"))
    record_gate_decision = Mock()
    monkeypatch.setattr(sms, "Client", client_constructor)
    monkeypatch.setattr(sms, "record_gate_decision", record_gate_decision)

    result = await sms.send_mms(
        "+15551234567",
        "hello",
        "https://example.com/card.vcf",
        from_number="+15557654321",
        contractor={"contractor_id": "c1"},
        action=ActionKey.CALLER_CONFIRMATION_MMS,
        gate_context=_gate_context("mms-1"),
    )

    assert result is False
    client_constructor.assert_not_called()
    record_gate_decision.assert_called_once()
    assert record_gate_decision.call_args.kwargs["action"] == ActionKey.CALLER_CONFIRMATION_MMS
    assert record_gate_decision.call_args.kwargs["contractor_id"] == "c1"
    assert record_gate_decision.call_args.kwargs["source"] == "ios"
    assert record_gate_decision.call_args.kwargs["resource_id"] == "mms-1"
    assert record_gate_decision.call_args.kwargs["decision"].allowed is False


@pytest.mark.asyncio
async def test_send_mms_with_enabled_gate_calls_twilio_with_media_url_and_records_decision(monkeypatch):
    events = []
    client = _Client(events)
    record_gate_decision = Mock(side_effect=lambda **_kwargs: events.append("audit"))

    def client_constructor(*_args, **_kwargs):
        events.append("client")
        assert "audit" in events
        return client

    monkeypatch.setattr(sms, "Client", client_constructor)
    monkeypatch.setattr(sms, "record_gate_decision", record_gate_decision)

    result = await sms.send_mms(
        "+15551234567",
        "hello",
        "https://example.com/card.vcf",
        from_number="+15557654321",
        contractor=_allowed_contractor(ActionKey.CALLER_CONFIRMATION_MMS),
        action=ActionKey.CALLER_CONFIRMATION_MMS,
        gate_context=_gate_context("mms-1"),
    )

    assert result is True
    assert events == ["audit", "client", "send"]
    assert client.messages.created == [
        {
            "to": "+15551234567",
            "from_": "+15557654321",
            "body": "hello",
            "media_url": ["https://example.com/card.vcf"],
        }
    ]
    record_gate_decision.assert_called_once()
    assert record_gate_decision.call_args.kwargs["action"] == ActionKey.CALLER_CONFIRMATION_MMS
    assert record_gate_decision.call_args.kwargs["contractor_id"] == "c1"
    assert record_gate_decision.call_args.kwargs["source"] == "ios"
    assert record_gate_decision.call_args.kwargs["resource_id"] == "mms-1"
    assert record_gate_decision.call_args.kwargs["decision"].allowed is True


@pytest.mark.asyncio
async def test_send_sms_with_no_action_uses_legacy_ungated_twilio_payload(monkeypatch):
    client = _Client()
    record_gate_decision = Mock()
    monkeypatch.setattr(sms, "Client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(sms, "record_gate_decision", record_gate_decision)

    result = await sms.send_sms(
        "+15551234567",
        "hello",
        from_number="+15557654321",
        action=None,
    )

    assert result is True
    assert client.messages.created == [
        {
            "to": "+15551234567",
            "from_": "+15557654321",
            "body": "hello",
        }
    ]
    record_gate_decision.assert_not_called()


@pytest.mark.asyncio
async def test_send_mms_with_no_action_uses_legacy_ungated_twilio_payload(monkeypatch):
    client = _Client()
    record_gate_decision = Mock()
    monkeypatch.setattr(sms, "Client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(sms, "record_gate_decision", record_gate_decision)

    result = await sms.send_mms(
        "+15551234567",
        "hello",
        "https://example.com/card.vcf",
        from_number="+15557654321",
        action=None,
    )

    assert result is True
    assert client.messages.created == [
        {
            "to": "+15551234567",
            "from_": "+15557654321",
            "body": "hello",
            "media_url": ["https://example.com/card.vcf"],
        }
    ]
    record_gate_decision.assert_not_called()
