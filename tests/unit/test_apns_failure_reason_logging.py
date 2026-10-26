"""APNs failure logs carry Apple's fixed reason code and never the raw body."""

import logging
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services import push_notification


DEVICE_TOKEN = "270dd7ecdeadbeef0000"


def _install_fake_apns(monkeypatch, response):
    posts = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, content, timeout):
            posts.append(url)
            return response

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(push_notification.settings, "apns_key_content", "test-key")
    monkeypatch.setattr(push_notification.settings, "apns_bundle_id", "com.kevin.callscreen")
    monkeypatch.setattr(push_notification, "_generate_apns_token", lambda: "jwt-token")
    monkeypatch.setattr(push_notification.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(push_notification.asyncio, "sleep", no_sleep)
    return posts


class JsonResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = repr(payload)

    def json(self):
        return self._payload


class NonJsonResponse:
    status_code = 400
    text = "<html>proxy error for caller +15551234567</html>"

    def json(self):
        raise ValueError("not json")


async def _send(kind):
    if kind == "voip":
        return await push_notification.send_voip_push(
            device_token=DEVICE_TOKEN,
            caller_phone="+15551234567",
            call_sid="CA123",
        )
    if kind == "regular":
        return await push_notification.send_regular_push(
            device_token=DEVICE_TOKEN,
            body="Kevin is screening a call.",
            call_sid="CA123",
        )
    return await push_notification.send_urgent_push(
        device_token=DEVICE_TOKEN,
        body="Urgent call.",
        call_sid="CA123",
    )


def _failure_messages(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if "APNs push failed" in record.getMessage()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["voip", "regular", "urgent"])
async def test_apns_400_logs_known_reason(monkeypatch, caplog, kind):
    _install_fake_apns(monkeypatch, JsonResponse(400, {"reason": "BadDeviceToken"}))
    caplog.set_level(logging.ERROR, logger="app.services.push_notification")

    assert await _send(kind) is False

    messages = _failure_messages(caplog)
    assert messages, "expected an APNs failure log"
    assert all("status=400 reason=BadDeviceToken" in message for message in messages)
    assert all(DEVICE_TOKEN[:8] not in message for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["voip", "regular", "urgent"])
async def test_apns_unrecognized_reason_is_not_echoed(monkeypatch, caplog, kind):
    private_reason = "caller Pat Customer +15551234567"
    _install_fake_apns(monkeypatch, JsonResponse(400, {"reason": private_reason}))
    caplog.set_level(logging.ERROR, logger="app.services.push_notification")

    assert await _send(kind) is False

    messages = _failure_messages(caplog)
    assert messages
    assert all("reason=unknown" in message for message in messages)
    assert all(private_reason not in message for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["voip", "regular", "urgent"])
async def test_apns_non_json_body_is_not_echoed(monkeypatch, caplog, kind):
    _install_fake_apns(monkeypatch, NonJsonResponse())
    caplog.set_level(logging.ERROR, logger="app.services.push_notification")

    assert await _send(kind) is False

    messages = _failure_messages(caplog)
    assert messages
    assert all("reason=unparsed" in message for message in messages)
    assert all("+15551234567" not in message for message in messages)


def test_reason_allowlist_covers_every_documented_400():
    documented_400s = {
        "BadCollapseId", "BadDeviceToken", "BadExpirationDate", "BadMessageId",
        "BadPriority", "BadTopic", "DeviceTokenNotForTopic", "DuplicateHeaders",
        "IdleTimeout", "InvalidPushType", "MissingDeviceToken", "MissingTopic",
        "PayloadEmpty", "TopicDisallowed",
    }
    assert documented_400s <= push_notification.APNS_ERROR_REASONS
