"""Tracked Twilio sends pre-register payload-free delivery receipts."""

import logging
import os
import time

import pytest


os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

from app.db.message_delivery_receipts import ReceiptUpdate
from app.services import sms
from app.services.sms import MessageDeliveryContext


MESSAGE_SID = "SM" + ("a" * 32)


class _Messages:
    def __init__(self, events: list[str], *, error: Exception | None = None):
        self.events = events
        self.error = error
        self.created = []

    def create(self, **kwargs):
        self.events.append("send")
        self.created.append(kwargs)
        if self.error:
            raise self.error
        return type(
            "Message",
            (),
            {"sid": MESSAGE_SID, "status": "queued"},
        )()


class _Client:
    def __init__(self, events: list[str], *, error: Exception | None = None):
        self.messages = _Messages(events, error=error)


@pytest.mark.asyncio
async def test_tracked_sms_registers_before_send_and_persists_provider_identity(
    monkeypatch,
    caplog,
):
    events = []
    client = _Client(events)

    async def create_receipt(**kwargs):
        events.append("register")
        assert kwargs == {
            "call_sid": "CA_test",
            "effect": "owner_sms",
            "channel": "sms",
        }
        return "receipt-test"

    async def record_provider_update(receipt_id, **kwargs):
        events.append("persist")
        assert receipt_id == "receipt-test"
        assert kwargs == {
            "provider_status": "queued",
            "provider_message_sid": MESSAGE_SID,
        }
        return ReceiptUpdate("updated", {"status": "pending"})

    def client_constructor(*_args, **_kwargs):
        events.append("client")
        assert _kwargs["http_client"].timeout == sms.TWILIO_HTTP_TIMEOUT_SECONDS
        return client

    monkeypatch.setattr(sms.receipt_db, "create_receipt", create_receipt)
    monkeypatch.setattr(
        sms.receipt_db,
        "record_provider_update",
        record_provider_update,
    )
    monkeypatch.setattr(sms, "Client", client_constructor)
    monkeypatch.setattr(sms.settings, "cloud_run_url", "https://service.example")
    caplog.set_level(logging.INFO, logger="app.services.sms")

    result = await sms.send_sms(
        "+15555550102",
        "private message body",
        from_number="+15555550103",
        delivery_context=MessageDeliveryContext("CA_test", "owner_sms"),
    )

    assert result is True
    assert events == ["register", "client", "send", "persist"]
    assert client.messages.created == [
        {
            "to": "+15555550102",
            "from_": "+15555550103",
            "body": "private message body",
            "status_callback": (
                "https://service.example/webhooks/twilio/message-status/receipt-test"
            ),
        }
    ]
    assert MESSAGE_SID not in caplog.text
    assert "private message body" not in caplog.text


@pytest.mark.asyncio
async def test_tracked_send_fails_closed_when_receipt_registration_fails(monkeypatch):
    async def create_receipt(**_kwargs):
        return ""

    monkeypatch.setattr(sms.receipt_db, "create_receipt", create_receipt)
    monkeypatch.setattr(
        sms,
        "Client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Twilio must not be called")
        ),
    )

    result = await sms.send_sms(
        "+15555550102",
        "private message body",
        delivery_context=MessageDeliveryContext("CA_test", "owner_sms"),
    )

    assert result is False


@pytest.mark.asyncio
async def test_tracked_send_fails_closed_when_receipt_registration_raises(
    monkeypatch,
):
    async def create_receipt(**_kwargs):
        raise RuntimeError("private storage failure")

    monkeypatch.setattr(sms.receipt_db, "create_receipt", create_receipt)
    monkeypatch.setattr(
        sms,
        "Client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Twilio must not be called")
        ),
    )

    result = await sms.send_sms(
        "+15555550102",
        "private message body",
        delivery_context=MessageDeliveryContext("CA_test", "owner_sms"),
    )

    assert result is False


@pytest.mark.asyncio
async def test_tracked_provider_rejection_marks_submission_failed_without_payload(
    monkeypatch,
    caplog,
):
    private_error = "private destination and message provider response"
    marked = []

    async def create_receipt(**_kwargs):
        return "receipt-test"

    async def record_submission_failure(receipt_id):
        marked.append(receipt_id)
        return True

    monkeypatch.setattr(sms.receipt_db, "create_receipt", create_receipt)
    monkeypatch.setattr(
        sms.message_delivery,
        "record_submission_failure",
        record_submission_failure,
    )
    monkeypatch.setattr(
        sms,
        "Client",
        lambda *_args, **_kwargs: _Client([], error=RuntimeError(private_error)),
    )
    caplog.set_level(logging.ERROR, logger="app.services.sms")

    result = await sms.send_sms(
        "+15555550102",
        "private message body",
        delivery_context=MessageDeliveryContext("CA_test", "owner_sms"),
    )

    assert result is False
    assert marked == ["receipt-test"]
    assert "exception_type=RuntimeError" in caplog.text
    assert private_error not in caplog.text
    assert "private message body" not in caplog.text


@pytest.mark.asyncio
async def test_tracked_provider_timeout_is_bounded_and_marked_failed(
    monkeypatch,
    caplog,
):
    marked = []

    async def create_receipt(**_kwargs):
        return "receipt-test"

    async def record_submission_failure(receipt_id):
        marked.append(receipt_id)
        return True

    class SlowMessages(_Messages):
        def create(self, **kwargs):
            time.sleep(0.05)
            return super().create(**kwargs)

    class SlowClient:
        messages = SlowMessages([])

    monkeypatch.setattr(sms.receipt_db, "create_receipt", create_receipt)
    monkeypatch.setattr(
        sms.message_delivery,
        "record_submission_failure",
        record_submission_failure,
    )
    monkeypatch.setattr(sms, "Client", lambda *_args, **_kwargs: SlowClient())
    monkeypatch.setattr(sms.settings, "cloud_run_url", "https://service.example")
    monkeypatch.setattr(sms, "TWILIO_SEND_TIMEOUT_SECONDS", 0.001)
    caplog.set_level(logging.ERROR, logger="app.services.sms")

    result = await sms.send_sms(
        "+15555550102",
        "private message body",
        delivery_context=MessageDeliveryContext("CA_test", "owner_sms"),
    )

    assert result is False
    assert marked == ["receipt-test"]
    assert "exception_type=TimeoutError" in caplog.text
    assert "private message body" not in caplog.text


@pytest.mark.asyncio
async def test_tracked_mms_registers_mms_channel(monkeypatch):
    client = _Client([])
    registered = []

    async def create_receipt(**kwargs):
        registered.append(kwargs)
        return "receipt-test"

    async def record_provider_update(*_args, **_kwargs):
        return ReceiptUpdate("updated", {"status": "pending"})

    monkeypatch.setattr(sms.receipt_db, "create_receipt", create_receipt)
    monkeypatch.setattr(
        sms.receipt_db,
        "record_provider_update",
        record_provider_update,
    )
    monkeypatch.setattr(sms, "Client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(sms.settings, "cloud_run_url", "https://service.example")

    result = await sms.send_mms(
        "+15555550102",
        "private message body",
        "https://example.com/card.vcf",
        from_number="+15555550103",
        delivery_context=MessageDeliveryContext("CA_test", "caller_vcard"),
    )

    assert result is True
    assert registered == [
        {
            "call_sid": "CA_test",
            "effect": "caller_vcard",
            "channel": "mms",
        }
    ]
    assert client.messages.created[0]["status_callback"].endswith(
        "/webhooks/twilio/message-status/receipt-test"
    )


@pytest.mark.asyncio
async def test_submission_persistence_error_does_not_duplicate_external_send(
    monkeypatch,
    caplog,
):
    client = _Client([])

    async def create_receipt(**_kwargs):
        return "receipt-test"

    async def record_provider_update(*_args, **_kwargs):
        return ReceiptUpdate("error")

    monkeypatch.setattr(sms.receipt_db, "create_receipt", create_receipt)
    monkeypatch.setattr(
        sms.receipt_db,
        "record_provider_update",
        record_provider_update,
    )
    monkeypatch.setattr(sms, "Client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(sms.settings, "cloud_run_url", "https://service.example")
    caplog.set_level(logging.ERROR, logger="app.services.sms")

    result = await sms.send_sms(
        "+15555550102",
        "private message body",
        delivery_context=MessageDeliveryContext("CA_test", "owner_sms"),
    )

    assert result is True
    assert len(client.messages.created) == 1
    assert "message_delivery event=submission_persist_failed" in caplog.text


@pytest.mark.asyncio
async def test_submission_persistence_exception_after_acceptance_is_not_send_failure(
    monkeypatch,
    caplog,
):
    client = _Client([])
    submission_failures = []

    async def create_receipt(**_kwargs):
        return "receipt-test"

    async def record_provider_update(*_args, **_kwargs):
        raise RuntimeError("private persistence failure")

    async def record_submission_failure(receipt_id):
        submission_failures.append(receipt_id)
        return True

    monkeypatch.setattr(sms.receipt_db, "create_receipt", create_receipt)
    monkeypatch.setattr(
        sms.receipt_db,
        "record_provider_update",
        record_provider_update,
    )
    monkeypatch.setattr(
        sms.message_delivery,
        "record_submission_failure",
        record_submission_failure,
    )
    monkeypatch.setattr(sms, "Client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(sms.settings, "cloud_run_url", "https://service.example")
    caplog.set_level(logging.ERROR, logger="app.services.sms")

    result = await sms.send_sms(
        "+15555550102",
        "private message body",
        delivery_context=MessageDeliveryContext("CA_test", "owner_sms"),
    )

    assert result is True
    assert len(client.messages.created) == 1
    assert submission_failures == []
    assert "message_delivery event=submission_persist_failed" in caplog.text
    assert "private persistence failure" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_base",
    ["http://service.example", "https://["],
)
async def test_tracked_send_rejects_invalid_callback_configuration(
    monkeypatch,
    callback_base,
):
    async def unexpected_create_receipt(**_kwargs):
        pytest.fail("invalid callback configuration must fail before registration")

    monkeypatch.setattr(sms.receipt_db, "create_receipt", unexpected_create_receipt)
    monkeypatch.setattr(sms.settings, "cloud_run_url", callback_base)
    monkeypatch.setattr(
        sms,
        "Client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Twilio must not be called")
        ),
    )

    result = await sms.send_sms(
        "+15555550102",
        "private message body",
        delivery_context=MessageDeliveryContext("CA_test", "owner_sms"),
    )

    assert result is False
