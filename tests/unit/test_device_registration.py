"""Exercise actual registration HTTP acknowledgements without provider access."""
import os
from unittest.mock import AsyncMock, Mock

# Synthetic defaults are installed after tests/conftest.py snapshots the pristine
# environment; no agent/test command exports provider configuration at startup.
for _key, _value in {
    "TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "test-token",
    "TWILIO_PHONE_NUMBER": "+15005550006", "TELEGRAM_BOT_TOKEN": "test-token",
    "USER_PHONE": "+15555550123",
}.items():
    os.environ.setdefault(_key, _value)

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from app.api import voip
from app.db import contractors, firestore_client


@pytest_asyncio.fixture
async def registration(monkeypatch):
    documents = {}
    doc = Mock()
    def save(data, merge):
        assert merge is True
        documents.update(data)
    doc.set.side_effect = save
    database = Mock()
    database.document.return_value = doc
    monkeypatch.setattr(firestore_client, "get_firestore_client", lambda: database)
    get_account = AsyncMock(return_value={"deleted_app_detected_at": 123.0})
    update_account = AsyncMock(return_value=True)
    monkeypatch.setattr(contractors, "get_contractor", get_account)
    monkeypatch.setattr(contractors, "update_contractor", update_account)
    api = FastAPI()
    async def authenticate(request: Request):
        if request.headers.get("authorization") != "Bearer synthetic-owner":
            raise HTTPException(status_code=401)
        request.state.contractor_id = "owner"
        request.state.is_admin = False
    api.dependency_overrides[voip.verify_api_token] = authenticate
    api.include_router(voip.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://registration.test",
                                 headers={"Authorization": "Bearer synthetic-owner"}) as client:
        yield client, documents, database, doc, update_account


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"push_token": "push"}, {"contractor_id": "owner"}])
async def test_invalid_registration_is_http_400(registration, body):
    client, _, database, _, _ = registration
    response = await client.post("/api/register-device", json=body)
    assert response.status_code == 400
    database.document.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [("push_token", "voip_token"), ("voip_token", "push_token")])
async def test_both_callback_orders_keep_other_channel_and_replace_current_token(registration, first, second):
    client, document, database, _, update = registration
    for channel in (first, second, first):
        value = channel + ("-new" if len(document) else "-initial")
        response = await client.post("/api/register-device", json={"contractor_id": "owner", channel: value})
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert document[channel] == value
    assert first in document and second in document
    assert document[first].endswith("-new")
    assert document["platform"] == "ios"
    database.document.assert_called_with("contractors/owner/devices/primary")
    update.assert_awaited_with("owner", {"deleted_app_detected_at": None})


@pytest.mark.asyncio
async def test_profile_updates_and_deletion_signal_clear(registration):
    client, document, _, _, update = registration
    response = await client.post("/api/register-device", json={"contractor_id": "owner", "push_token": "push",
        "voip_token": "voip", "timezone": "America/New_York", "language": "en", "urgent_handoff_v1": True})
    assert response.status_code == 200
    assert document["urgent_handoff_v1"] is True
    update.assert_awaited_once_with("owner", {"deleted_app_detected_at": None,
        "timezone": "America/New_York", "user_language": "en"})


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["device", "profile", "profile_unacknowledged"])
async def test_write_failure_is_http_500_not_success(registration, phase):
    client, _, _, doc, update = registration
    if phase == "profile_unacknowledged":
        update.return_value = False
    else:
        failing_write = doc.set if phase == "device" else update
        failing_write.side_effect = RuntimeError("synthetic write failure")
    response = await client.post("/api/register-device", json={"contractor_id": "owner", "push_token": "push"})
    assert response.status_code == 500
    assert response.json().get("status") != "ok"
    if phase == "device":
        update.assert_not_awaited()


@pytest.mark.asyncio
async def test_deletion_signal_clear_does_not_depend_on_profile_read(registration, monkeypatch):
    client, _, _, _, update = registration
    failing_read = AsyncMock(side_effect=RuntimeError("synthetic read failure"))
    monkeypatch.setattr(contractors, "get_contractor", failing_read)
    response = await client.post("/api/register-device", json={"contractor_id": "owner", "push_token": "push"})
    assert response.status_code == 200
    failing_read.assert_not_awaited()
    update.assert_awaited_once_with("owner", {"deleted_app_detected_at": None})


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized,owner,expected", [(False, "owner", 401), (True, "another-account", 403)])
async def test_auth_and_ownership_remain_enforced(registration, authorized, owner, expected):
    client, _, database, _, _ = registration
    headers = {"Authorization": "Bearer synthetic-owner" if authorized else "Bearer invalid"}
    response = await client.post("/api/register-device", headers=headers,
                                 json={"contractor_id": owner, "push_token": "push"})
    assert response.status_code == expected
    database.document.assert_not_called()
