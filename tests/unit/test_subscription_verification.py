"""Subscription verification safety checks."""

import base64
import json

import pytest

from app.db import contractors as contractors_db
from app.services import subscription


def _unsigned_jws(payload: dict) -> str:
    header = {"alg": "ES256"}

    def encode(part: dict) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode(header)}.{encode(payload)}.signature"


def test_extract_transaction_info_decodes_signed_transaction():
    payload = {
        "bundleId": "com.kevin.callscreen",
        "productId": "com.kevin.callscreen.businesspro.monthly",
        "appAccountToken": "subscription-uuid",
        "expiresDate": 1770000000000,
    }

    decoded = subscription._extract_transaction_info({
        "signedTransactionInfo": _unsigned_jws(payload)
    })

    assert decoded == payload


def test_extract_transaction_info_rejects_bundle_mismatch():
    payload = {
        "bundleId": "com.example.other",
        "productId": "com.kevin.callscreen.businesspro.monthly",
        "appAccountToken": "subscription-uuid",
        "expiresDate": 1770000000000,
    }

    decoded = subscription._extract_transaction_info({
        "signedTransactionInfo": _unsigned_jws(payload)
    })

    assert decoded is None


@pytest.mark.asyncio
async def test_verify_transaction_falls_back_to_sandbox_on_production_not_found(monkeypatch):
    payload = {
        "bundleId": "com.kevin.callscreen",
        "productId": "com.kevin.callscreen.business.monthly",
        "appAccountToken": "subscription-uuid",
        "environment": "Sandbox",
    }
    responses = [
        _FakeResponse(404, {"errorCode": 4040010, "errorMessage": "Transaction id not found."}),
        _FakeResponse(200, {"signedTransactionInfo": _unsigned_jws(payload)}),
    ]
    calls = []

    monkeypatch.setattr(subscription.settings, "appstore_environment", "production")
    monkeypatch.setattr(subscription.settings, "appstore_key_id", "KEY")
    monkeypatch.setattr(subscription, "_get_appstore_jwt", lambda: "jwt")
    monkeypatch.setattr(subscription.httpx, "AsyncClient", lambda: _FakeAsyncClient(responses, calls))

    transaction = await subscription.verify_transaction("tx-1")

    assert transaction == payload
    assert calls == [
        "https://api.storekit.itunes.apple.com/inApps/v1/transactions/tx-1",
        "https://api.storekit-sandbox.itunes.apple.com/inApps/v1/transactions/tx-1",
    ]


@pytest.mark.asyncio
async def test_verify_transaction_does_not_fallback_for_non_not_found_errors(monkeypatch):
    responses = [_FakeResponse(401, {"errorCode": 4010000, "errorMessage": "Unauthorized"})]
    calls = []

    monkeypatch.setattr(subscription.settings, "appstore_environment", "production")
    monkeypatch.setattr(subscription.settings, "appstore_key_id", "KEY")
    monkeypatch.setattr(subscription, "_get_appstore_jwt", lambda: "jwt")
    monkeypatch.setattr(subscription.httpx, "AsyncClient", lambda: _FakeAsyncClient(responses, calls))

    transaction = await subscription.verify_transaction("tx-1")

    assert transaction is None
    assert calls == ["https://api.storekit.itunes.apple.com/inApps/v1/transactions/tx-1"]


@pytest.mark.asyncio
async def test_verify_transaction_uses_sandbox_only_outside_production(monkeypatch):
    payload = {
        "bundleId": "com.kevin.callscreen",
        "productId": "com.kevin.callscreen.personal.monthly",
        "appAccountToken": "subscription-uuid",
        "environment": "Sandbox",
    }
    responses = [_FakeResponse(200, {"signedTransactionInfo": _unsigned_jws(payload)})]
    calls = []

    monkeypatch.setattr(subscription.settings, "appstore_environment", "sandbox")
    monkeypatch.setattr(subscription.settings, "appstore_key_id", "KEY")
    monkeypatch.setattr(subscription, "_get_appstore_jwt", lambda: "jwt")
    monkeypatch.setattr(subscription.httpx, "AsyncClient", lambda: _FakeAsyncClient(responses, calls))

    transaction = await subscription.verify_transaction("tx-1")

    assert transaction == payload
    assert calls == ["https://api.storekit-sandbox.itunes.apple.com/inApps/v1/transactions/tx-1"]


@pytest.mark.asyncio
async def test_update_subscription_from_decoded_transaction(monkeypatch):
    updates = {}

    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "subscription_uuid": "subscription-uuid",
        }

    async def fake_update_contractor(contractor_id, body):
        updates["contractor_id"] = contractor_id
        updates["body"] = body
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update_contractor)

    updated = await subscription.update_subscription_from_transaction(
        "contractor-1",
        {
            "productId": "com.kevin.callscreen.businesspro.monthly",
            "appAccountToken": "subscription-uuid",
            "expiresDate": 1770000000000,
        },
    )

    assert updated is True
    assert updates == {
        "contractor_id": "contractor-1",
        "body": {
            "subscription_tier": "businessPro",
            "subscription_status": "active",
            "subscription_expires": 1770000000.0,
        },
    }


@pytest.mark.asyncio
async def test_update_subscription_rejects_app_account_token_mismatch(monkeypatch):
    async def fake_get_contractor(contractor_id):
        return {
            "contractor_id": contractor_id,
            "subscription_uuid": "expected-uuid",
        }

    async def fail_update(*args, **kwargs):
        raise AssertionError("mismatched transaction must not update subscription")

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fail_update)

    updated = await subscription.update_subscription_from_transaction(
        "contractor-1",
        {
            "productId": "com.kevin.callscreen.businesspro.monthly",
            "appAccountToken": "other-uuid",
            "expiresDate": 1770000000000,
        },
    )

    assert updated is False


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeResponse], calls: list[str]):
        self.responses = responses
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, headers: dict, timeout: float):
        self.calls.append(url)
        return self.responses.pop(0)
