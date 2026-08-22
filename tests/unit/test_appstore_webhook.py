"""Tests for App Store Server Notifications webhook (app/webhooks/appstore.py).

Verifies error handling behavior:
- Missing signedPayload -> HTTP 400
- Invalid/forged payload -> HTTP 400
- Invalid JSON in request -> HTTP 400
- Successful handling -> HTTP 200
- Unexpected infrastructure/processing exception -> HTTP 500 so Apple's
  retry mechanism re-delivers the notification instead of silently dropping it.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest
from fastapi.responses import JSONResponse

from app.webhooks import appstore as appstore_webhook


class _FakeJsonRequest:
    def __init__(self, data: dict | None = None, raise_error: Exception | None = None):
        self._data = data
        self._raise_error = raise_error

    async def json(self):
        if self._raise_error:
            raise self._raise_error
        return self._data


@pytest.mark.asyncio
async def test_missing_signed_payload_returns_400():
    req = _FakeJsonRequest(data={"other_key": "val"})
    response = await appstore_webhook.handle_appstore_notification(req)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "missing signedPayload"}


@pytest.mark.asyncio
async def test_non_string_signed_payload_returns_400():
    req = _FakeJsonRequest(data={"signedPayload": 12345})
    response = await appstore_webhook.handle_appstore_notification(req)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "missing signedPayload"}


@pytest.mark.asyncio
async def test_invalid_jws_payload_returns_400(monkeypatch):
    req = _FakeJsonRequest(data={"signedPayload": "invalid.jws.token"})
    monkeypatch.setattr(
        appstore_webhook,
        "_decode_notification_payload",
        lambda payload: (_ for _ in ()).throw(ValueError("Certificate chain invalid")),
    )
    response = await appstore_webhook.handle_appstore_notification(req)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "invalid payload"}


@pytest.mark.asyncio
async def test_invalid_json_body_returns_400():
    req = _FakeJsonRequest(raise_error=json.JSONDecodeError("Expecting value", "doc", 0))
    response = await appstore_webhook.handle_appstore_notification(req)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "invalid json"}


@pytest.mark.asyncio
async def test_valid_notification_calls_service_and_returns_200(monkeypatch):
    mock_payload = {
        "notificationType": "DID_RENEW",
        "data": {"bundleId": "com.kevin.callscreen"},
    }
    monkeypatch.setattr(
        appstore_webhook,
        "_decode_notification_payload",
        lambda payload: mock_payload,
    )
    mock_handle = AsyncMock()
    with patch("app.services.subscription.handle_appstore_notification", mock_handle):
        req = _FakeJsonRequest(data={"signedPayload": "valid.mock.token"})
        response = await appstore_webhook.handle_appstore_notification(req)
        assert response == {"status": "ok"}
        mock_handle.assert_awaited_once_with(mock_payload)


@pytest.mark.asyncio
async def test_unexpected_service_exception_returns_500(monkeypatch):
    mock_payload = {
        "notificationType": "DID_RENEW",
        "data": {"bundleId": "com.kevin.callscreen"},
    }
    monkeypatch.setattr(
        appstore_webhook,
        "_decode_notification_payload",
        lambda payload: mock_payload,
    )
    mock_handle = AsyncMock(side_effect=RuntimeError("Firestore connection timeout"))
    with patch("app.services.subscription.handle_appstore_notification", mock_handle):
        req = _FakeJsonRequest(data={"signedPayload": "valid.mock.token"})
        response = await appstore_webhook.handle_appstore_notification(req)
        assert isinstance(response, JSONResponse)
        assert response.status_code == 500
        assert json.loads(response.body) == {"error": "internal processing error"}
