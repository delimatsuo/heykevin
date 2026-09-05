import json
import os
import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services import push_notification
from app.services import screening_summary


@pytest.mark.asyncio
async def test_send_regular_push_includes_collapse_id_and_category(monkeypatch):
    sent_headers = []
    sent_payloads = []

    class FakeResponse:
        status_code = 200
        text = "OK"

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, content, timeout):
            sent_headers.append(headers)
            sent_payloads.append(json.loads(content))
            return FakeResponse()

    monkeypatch.setattr(push_notification.settings, "apns_key_content", "test-key")
    monkeypatch.setattr(push_notification.settings, "apns_bundle_id", "com.kevin.callscreen")
    monkeypatch.setattr(push_notification, "_generate_apns_token", lambda: "jwt-token")
    monkeypatch.setattr(push_notification.httpx, "AsyncClient", FakeAsyncClient)

    sent = await push_notification.send_regular_push(
        device_token="device-token-123",
        title="Jonathan from Geico",
        body="Wants to talk about insurance renewal — Tap to answer",
        call_sid="CA12345",
        caller_phone="+15551234567",
        caller_name="Jonathan from Geico",
        contractor_id="c1",
        collapse_id="call_CA12345",
        category="SCREENING_CALL",
    )

    assert sent is True
    assert len(sent_headers) == 1
    assert sent_headers[0]["apns-collapse-id"] == "call_CA12345"
    assert sent_headers[0]["apns-topic"] == "com.kevin.callscreen"

    assert len(sent_payloads) == 1
    assert sent_payloads[0]["aps"]["alert"]["title"] == "Jonathan from Geico"
    assert sent_payloads[0]["aps"]["alert"]["body"] == "Wants to talk about insurance renewal — Tap to answer"
    assert sent_payloads[0]["aps"]["category"] == "SCREENING_CALL"
    assert sent_payloads[0]["call_sid"] == "CA12345"


@pytest.mark.asyncio
async def test_send_screening_summary_push(monkeypatch):
    captured_args = {}

    async def fake_get_device_token(*, contractor_id):
        assert contractor_id == "c1"
        return "push-token-456"

    async def fake_send_regular_push(**kwargs):
        captured_args.update(kwargs)
        return True

    monkeypatch.setattr(push_notification, "get_device_token", fake_get_device_token)
    monkeypatch.setattr(push_notification, "send_regular_push", fake_send_regular_push)

    sent = await push_notification.send_screening_summary_push(
        contractor_id="c1",
        call_sid="CA999",
        caller_phone="+15559876543",
        caller_name="Jonathan from Geico",
        reason="Calling regarding insurance policy renewal",
    )

    assert sent is True
    assert captured_args["device_token"] == "push-token-456"
    assert captured_args["title"] == "Jonathan from Geico"
    assert captured_args["body"] == "Calling regarding insurance policy renewal — Tap to answer"
    assert captured_args["collapse_id"] == "call_CA999"
    assert captured_args["category"] == "SCREENING_CALL"


@pytest.mark.asyncio
async def test_extract_screening_summary_fallback():
    transcript = "Kevin: Hi, who's calling?\nCaller: Hi, this is Jonathan from Geico. I'm calling about car insurance renewal."
    result = screening_summary._fallback_extraction(transcript)
    assert "Jonathan from Geico" in result["caller_name"]
    assert "insurance renewal" in result["reason"]


@pytest.mark.asyncio
async def test_extract_screening_summary_llm(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "content": [
                    {
                        "type": "text",
                        "text": '{"caller_name": "Jonathan from Geico", "reason": "Wants to talk about insurance renewal"}',
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, *, headers, json, timeout):
            return FakeResponse()

    monkeypatch.setattr(screening_summary.settings, "anthropic_api_key", "fake-key")
    monkeypatch.setattr(screening_summary.httpx, "AsyncClient", FakeAsyncClient)

    res = await screening_summary.extract_screening_summary(
        transcript="Kevin: Who is this?\nCaller: Jonathan from Geico.",
        caller_phone="+15551234567",
    )
    assert res["caller_name"] == "Jonathan from Geico"
    assert res["reason"] == "Wants to talk about insurance renewal"
