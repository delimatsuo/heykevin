import json
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.webhooks import media_stream
from app.webhooks import twilio_incoming
from app.services import post_call
from app.services import push_notification


def test_incoming_call_push_body_does_not_include_caller_identity():
    body = twilio_incoming._safe_incoming_call_push_body(
        caller_name="Pat Customer",
        caller_phone="+15551234567",
    )

    assert "Pat Customer" not in body
    assert "+15551234567" not in body
    assert body == "Kevin is screening a call. Open Kevin for details."


def test_urgent_push_body_does_not_include_raw_speech():
    body = media_stream._safe_urgent_push_body(
        caller_name="Pat Customer",
        caller_phone="+15551234567",
    )

    assert "Caller says:" not in body
    assert "+15551234567" not in body
    assert body == "Urgent call needs review. Open Kevin for details."


def test_media_stream_active_call_fallback_uses_authenticated_context():
    active_call = media_stream._active_call_fallback(
        "CA123",
        {
            "contractor_id": "contractor-1",
            "caller_phone": "+15551234567",
            "caller_name": "Pat Customer",
            "accepted": True,
        },
    )

    assert active_call is not None
    assert active_call.call_sid == "CA123"
    assert active_call.contractor_id == "contractor-1"
    assert active_call.caller_phone == "+15551234567"
    assert active_call.caller_name == "Pat Customer"
    assert active_call.accepted is True


def test_media_stream_active_call_fallback_requires_owner_context():
    assert media_stream._active_call_fallback("CA123", None) is None
    assert media_stream._active_call_fallback("CA123", {"caller_phone": "+15551234567"}) is None
    assert media_stream._active_call_fallback("CA123", {"contractor_id": "contractor-1"}) is None


def test_voip_push_body_does_not_include_arbitrary_reason():
    body = push_notification._safe_voip_push_body(
        reason="emergency from Pat Customer +15551234567"
    )

    assert body == "Incoming call. Open Kevin for details."
    assert "Pat Customer" not in body
    assert "+15551234567" not in body
    assert "emergency from" not in body


def test_summary_push_body_does_not_include_issue_details():
    body = post_call._safe_summary_push_body(
        caller_name="Pat Customer",
        call_type="service_request",
        urgency="emergency",
    )

    assert "Pat Customer" not in body
    assert "urgent" in body.lower()
    assert "emergency" not in body.lower()
    assert "Open Kevin" in body


def test_summary_push_body_does_not_interpolate_unknown_urgency():
    body = post_call._safe_summary_push_body(
        caller_name="Pat Customer",
        call_type="service_request",
        urgency="emergency from Pat Customer +15551234567 burst pipe",
    )

    assert "emergency from" not in body
    assert "Pat Customer" not in body
    assert "+15551234567" not in body
    assert "burst pipe" not in body
    assert body == "New service call summary. Open Kevin for details."


def test_summary_push_body_formats_same_day_urgency_safely():
    body = post_call._safe_summary_push_body(
        caller_name="Pat Customer",
        call_type="service_request",
        urgency="same_day",
    )

    assert "same_day" not in body
    assert "same-day" in body
    assert body == "New same-day call summary. Open Kevin for details."


def test_summary_push_body_uses_generic_service_copy_without_urgency():
    body = post_call._safe_summary_push_body(
        caller_name="Pat Customer",
        call_type="service_request",
        urgency="none",
    )

    assert "Pat Customer" not in body
    assert "leaking sink" not in body
    assert body == "New service call summary. Open Kevin for details."


@pytest.mark.asyncio
async def test_send_summary_push_sends_lock_screen_safe_body(monkeypatch):
    sent_pushes = []

    async def fake_get_device_token(*, contractor_id):
        assert contractor_id == "c1"
        return "push-token"

    async def fake_send_regular_push(**kwargs):
        sent_pushes.append(kwargs)
        return True

    monkeypatch.setattr("app.services.push_notification.get_device_token", fake_get_device_token)
    monkeypatch.setattr("app.services.push_notification.send_regular_push", fake_send_regular_push)

    await post_call._send_summary_push(
        {
            "caller_name": "Pat Customer",
            "caller_phone": "+15551234567",
            "call_sid": "CA123",
            "call_type": "service_request",
            "issue_description": "burst pipe at 123 Main Street",
            "urgency": "emergency",
        },
        {"contractor_id": "c1"},
    )

    assert len(sent_pushes) == 1
    assert sent_pushes[0]["body"] == "New urgent call summary. Open Kevin for details."
    assert "Pat Customer" not in sent_pushes[0]["body"]
    assert "+15551234567" not in sent_pushes[0]["body"]
    assert "burst pipe" not in sent_pushes[0]["body"]
    assert "123 Main Street" not in sent_pushes[0]["body"]


@pytest.mark.asyncio
async def test_send_voip_push_sends_lock_screen_safe_body(monkeypatch):
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
            sent_payloads.append(json.loads(content))
            return FakeResponse()

    monkeypatch.setattr(push_notification.settings, "apns_key_content", "test-key")
    monkeypatch.setattr(push_notification.settings, "apns_bundle_id", "com.kevin.callscreen")
    monkeypatch.setattr(push_notification, "_generate_apns_token", lambda: "jwt-token")
    monkeypatch.setattr(push_notification.httpx, "AsyncClient", FakeAsyncClient)

    sent = await push_notification.send_voip_push(
        device_token="voip-token",
        caller_phone="+15551234567",
        caller_name="URGENT: Pat Customer",
        reason="urgent_call",
        call_sid="CA123",
        conference_name="urgent-conf",
        access_token="twilio-access-token",
    )

    assert sent is True
    assert len(sent_payloads) == 1
    assert sent_payloads[0]["caller_phone"] == "+15551234567"
    assert sent_payloads[0]["caller_name"] == "URGENT: Pat Customer"
    assert sent_payloads[0]["reason"] == "urgent_call"
    assert sent_payloads[0]["call_sid"] == "CA123"
    assert sent_payloads[0]["conference_name"] == "urgent-conf"
    assert sent_payloads[0]["access_token"] == "twilio-access-token"

    body = sent_payloads[0]["aps"]["alert"]["body"]
    assert body == "Urgent call needs review. Open Kevin for details."
    assert "URGENT: Pat Customer" not in body
    assert "Pat Customer" not in body
    assert "+15551234567" not in body
    assert "emergency from" not in body
