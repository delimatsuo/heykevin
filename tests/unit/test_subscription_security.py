"""Security-audit fixes for subscription flow (F-05, F-06, F-12).

These tests use stubs/mocks for Firestore, Apple's Server API, and the
contractor DB. They exercise:

  F-05  /api/subscription/verify fails closed when Apple is unreachable.
  F-06  Cross-contractor receipt reuse is rejected globally.
  F-12  /api/subscription/sign-offer rate limit is enforced via the persistent
        Firestore-backed limiter (across the boundary the in-memory dict can
        no longer be bypassed by hitting a fresh Cloud Run instance).
"""

from __future__ import annotations

import asyncio
import copy
import os
from typing import Any, Dict, Optional

# Required env vars for app.config to import cleanly.
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.api import subscription as sub_api
from app.db import apple_transactions as apple_tx_db
from app.db import rate_limits as rl_db
from app.services import subscription as sub_service

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, contractor_id: str, is_admin: bool = False):
        class _State:
            pass
        self.state = _State()
        self.state.contractor_id = contractor_id
        self.state.is_admin = is_admin


class _InMemoryFirestore:
    """Tiny Firestore stand-in supporting just what our two helpers use.

    Supports document().get(), document().set(merge=...), and the @transactional
    decorator path with snapshot read + transaction.set().
    """

    def __init__(self):
        self._docs: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # --- public API used by app.db.firestore_client.get_firestore_client ---
    def collection(self, name: str):
        return _Collection(self, name)

    def document(self, path: str):
        return _DocRef(self, path)

    def transaction(self):
        return _FakeTransaction(self)


class _Collection:
    def __init__(self, fs: _InMemoryFirestore, name: str, filters: list = None, limit_n: Optional[int] = None):
        self._fs = fs
        self._name = name
        self._filters = filters or []
        self._limit_n = limit_n

    def document(self, doc_id: str):
        return _DocRef(self._fs, f"{self._name}/{doc_id}")

    def where(self, field_or_filter=None, op=None, value=None, *, filter=None):
        new_filters = list(self._filters)
        if filter is not None:
            field = getattr(filter, "field_path", getattr(filter, "field_name", ""))
            new_filters.append((field, getattr(filter, "op_string", "=="), getattr(filter, "value", None)))
        elif field_or_filter is not None:
            if hasattr(field_or_filter, "field_path") or hasattr(field_or_filter, "field_name"):
                field = getattr(field_or_filter, "field_path", getattr(field_or_filter, "field_name", ""))
                new_filters.append((field, getattr(field_or_filter, "op_string", "=="), getattr(field_or_filter, "value", None)))
            else:
                new_filters.append((field_or_filter, op, value))
        return _Collection(self._fs, self._name, new_filters, self._limit_n)

    def limit(self, count: int):
        return _Collection(self._fs, self._name, self._filters, count)

    def stream(self, transaction=None):
        prefix = f"{self._name}/"
        results = []
        for path, data in list(self._fs._docs.items()):
            if path.startswith(prefix) and "/" not in path[len(prefix):]:
                match = True
                for field, op, val in self._filters:
                    doc_val = data.get(field)
                    if op == "==" and doc_val != val:
                        match = False
                        break
                if match:
                    doc_id = path[len(prefix):]
                    results.append(_Snapshot(True, data, doc_id=doc_id))
                    if self._limit_n is not None and len(results) >= self._limit_n:
                        break
        return iter(results)


class _Snapshot:
    def __init__(self, exists: bool, data: Optional[dict], doc_id: str = ""):
        self.exists = exists
        self._data = data or {}
        self.id = doc_id

    def to_dict(self):
        return dict(self._data)


class _DocRef:
    def __init__(self, fs: _InMemoryFirestore, path: str):
        self._fs = fs
        self._path = path

    def get(self, transaction=None):
        data = self._fs._docs.get(self._path)
        doc_id = self._path.split("/")[-1]
        return _Snapshot(data is not None, data, doc_id=doc_id)

    def set(self, value: dict, merge: bool = False):
        if merge and self._path in self._fs._docs:
            current = self._fs._docs[self._path]
            current.update(value)
        else:
            self._fs._docs[self._path] = dict(value)

    def update(self, value: dict):
        if self._path in self._fs._docs:
            self._fs._docs[self._path].update(value)
        else:
            raise KeyError(f"Document {self._path} does not exist (update requires existing document)")


class _FakeTransaction:
    """Mimics google.cloud.firestore.Transaction for our two helpers."""

    def __init__(self, fs: _InMemoryFirestore):
        self._fs = fs

    def set(self, doc_ref: _DocRef, value: dict, merge: bool = False):
        doc_ref.set(value, merge=merge)

    def update(self, doc_ref: _DocRef, value: dict):
        doc_ref.update(value)


def _install_fake_firestore(monkeypatch) -> _InMemoryFirestore:
    """Patch get_firestore_client + the @fs.transactional decorator."""
    fake = _InMemoryFirestore()

    monkeypatch.setattr(
        "app.db.firestore_client.get_firestore_client",
        lambda: fake,
    )
    monkeypatch.setattr(
        "app.db.contractors.get_firestore_client",
        lambda: fake,
    )

    # Replace google.cloud.firestore.transactional with a no-op decorator —
    # our _FakeTransaction does the work directly without retry semantics.
    import google.cloud.firestore as fs_mod
    monkeypatch.setattr(fs_mod, "transactional", lambda fn: fn)
    return fake


# ---------------------------------------------------------------------------
# F-05: verify_transaction_strict / /api/subscription/verify fail closed
# ---------------------------------------------------------------------------


def test_verify_request_rejects_transaction_path_injection():
    with pytest.raises(ValidationError):
        sub_api.VerifyRequest(transaction_id="../other/document", contractor_id="c1")

class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, url, headers, timeout):
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_verify_transaction_strict_returns_unreachable_on_5xx(monkeypatch):
    monkeypatch.setattr(sub_service.settings, "appstore_environment", "sandbox")
    monkeypatch.setattr(sub_service.settings, "appstore_key_id", "KEY")
    monkeypatch.setattr(sub_service, "_get_appstore_jwt", lambda: "jwt")
    monkeypatch.setattr(
        sub_service.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient([_FakeResponse(503, {"errorMessage": "down"})]),
    )

    result = await sub_service.verify_transaction_strict("tx-1")
    assert result.ok is False
    assert result.unreachable is True
    assert result.reason == "server_error"


@pytest.mark.asyncio
async def test_verify_transaction_strict_returns_authoritative_not_found(monkeypatch):
    monkeypatch.setattr(sub_service.settings, "appstore_environment", "sandbox")
    monkeypatch.setattr(sub_service.settings, "appstore_key_id", "KEY")
    monkeypatch.setattr(sub_service, "_get_appstore_jwt", lambda: "jwt")
    monkeypatch.setattr(
        sub_service.httpx,
        "AsyncClient",
        lambda: _FakeAsyncClient(
            [_FakeResponse(404, {"errorCode": 4040010, "errorMessage": "Transaction id not found."})]
        ),
    )

    result = await sub_service.verify_transaction_strict("tx-1")
    assert result.ok is False
    assert result.unreachable is False  # Apple authoritatively said "no"
    assert result.reason == "not_found"


@pytest.mark.asyncio
async def test_verify_transaction_strict_returns_unreachable_on_transport_error(monkeypatch):
    import httpx

    class _ExplodingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url, headers, timeout):
            raise httpx.ConnectError("dns failure")

    monkeypatch.setattr(sub_service.settings, "appstore_environment", "sandbox")
    monkeypatch.setattr(sub_service.settings, "appstore_key_id", "KEY")
    monkeypatch.setattr(sub_service, "_get_appstore_jwt", lambda: "jwt")
    monkeypatch.setattr(sub_service.httpx, "AsyncClient", lambda: _ExplodingClient())

    result = await sub_service.verify_transaction_strict("tx-1")
    assert result.ok is False
    assert result.unreachable is True
    assert result.reason == "transport_error"


@pytest.mark.asyncio
async def test_verify_endpoint_fails_closed_when_apple_unreachable(monkeypatch):
    """End-to-end: /api/subscription/verify must return 502 (not 200/ok) when
    Apple cannot be reached, so the iOS client never confuses it with success."""

    async def fake_get_processed(_cid, _tx):
        return None

    async def fake_get_binding(_tx):
        return None

    async def fake_strict(_tx):
        return sub_service.VerificationResult(ok=False, unreachable=True, reason="server_error")

    monkeypatch.setattr(sub_service, "get_processed_transaction", fake_get_processed)
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", fake_get_binding)
    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)

    request = _FakeRequest("c1")
    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, request)

    # Should be a JSONResponse with 502, not a dict claiming ok.
    assert getattr(response, "status_code", None) == 502
    assert response.headers.get("Retry-After") == "30"
    import json as _json
    payload = _json.loads(bytes(response.body).decode())
    assert payload["status"] == "verification_failed"


@pytest.mark.asyncio
async def test_verify_endpoint_duplicate_transaction_bypasses_rate_limit(monkeypatch):
    """Idempotent StoreKit retries must not be rejected by the verify limiter."""

    async def fake_get_processed(_cid, _tx):
        return {"outcome": "active"}

    async def fake_get_contractor(_cid):
        return {
            "contractor_id": "c1",
            "subscription_uuid": "uuid-1",
            "subscription_status": "active",
            "subscription_tier": "business",
            "subscription_original_transaction_id": "orig-1",
            "active": True,
        }

    async def fail_get_binding(_tx):
        raise AssertionError("duplicate transaction should not check global binding")

    async def fail_strict(_tx):
        raise AssertionError("duplicate transaction should not call Apple")

    from app.db import contractors as contractors_db
    monkeypatch.setattr(sub_service, "get_processed_transaction", fake_get_processed)
    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", fail_get_binding)
    monkeypatch.setattr(sub_service, "verify_transaction_strict", fail_strict)
    monkeypatch.setattr(
        sub_api,
        "_check_rate_limit_with_retry",
        lambda *_args, **_kwargs: (False, 60),
    )

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "already_processed",
        "outcome": "active",
        "entitlement_active": True,
    }


@pytest.mark.asyncio
async def test_verify_endpoint_unknown_processed_outcome_fails_closed(monkeypatch):
    """Corrupt/manual records must never become an inactive acknowledgement."""

    async def fake_get_processed(_cid, _tx):
        return {"outcome": "future_or_corrupt"}

    async def boom(*_args, **_kwargs):
        raise AssertionError("invalid processed outcome must fail before downstream work")

    monkeypatch.setattr(sub_service, "get_processed_transaction", fake_get_processed)
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", boom)
    monkeypatch.setattr(sub_service, "verify_transaction_strict", boom)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", boom)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    with pytest.raises(HTTPException) as exc:
        await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert exc.value.status_code == 500
    assert exc.value.detail == "invalid_processed_transaction_outcome"


@pytest.mark.asyncio
async def test_verify_endpoint_returns_400_when_apple_authoritatively_invalid(monkeypatch):
    async def fake_get_processed(*_):
        return None

    async def fake_get_binding(_tx):
        return None

    async def fake_strict(_tx):
        return sub_service.VerificationResult(ok=False, unreachable=False, reason="not_found")

    monkeypatch.setattr(sub_service, "get_processed_transaction", fake_get_processed)
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", fake_get_binding)
    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    with pytest.raises(HTTPException) as exc:
        await sub_api.verify_subscription(body, _FakeRequest("c1"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_verify_endpoint_acknowledges_and_records_inactive_transaction(monkeypatch):
    """A verified owned-but-expired transaction is safe to drain, not activate."""

    async def fake_get_processed(*_):
        return None

    async def fake_get_binding(_tx):
        return None

    async def fake_strict(_tx):
        return sub_service.VerificationResult(ok=True, transaction={"transactionId": "tx-1"})

    async def fake_update(*_):
        return sub_service.SubscriptionUpdateResult(
            sub_service.SubscriptionUpdateOutcome.INACTIVE,
            reason="expired",
        )

    marked = []

    async def fake_mark(contractor_id, transaction_id, outcome):
        marked.append((contractor_id, transaction_id, outcome))

    monkeypatch.setattr(sub_service, "get_processed_transaction", fake_get_processed)
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", fake_get_binding)
    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service, "update_subscription_from_transaction", fake_update)
    monkeypatch.setattr(sub_service, "mark_transaction_seen", fake_mark)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "terminal_processed",
        "outcome": "inactive",
        "entitlement_active": False,
    }
    assert marked == [
        ("c1", "tx-1", sub_service.SubscriptionUpdateOutcome.INACTIVE)
    ]


@pytest.mark.asyncio
async def test_verify_endpoint_duplicate_inactive_bypasses_apple_and_limiter(monkeypatch):
    async def fake_get_processed(*_):
        return {"outcome": "inactive", "processed_at": 123}

    async def boom(*_args, **_kwargs):
        raise AssertionError("processed inactive transaction must bypass downstream work")

    monkeypatch.setattr(sub_service, "get_processed_transaction", fake_get_processed)
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", boom)
    monkeypatch.setattr(sub_service, "verify_transaction_strict", boom)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", boom)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "already_processed",
        "outcome": "inactive",
        "entitlement_active": False,
    }


@pytest.mark.asyncio
async def test_verify_endpoint_ownership_mismatch_is_409_and_not_recorded(monkeypatch):
    async def fake_get_processed(*_):
        return None

    async def fake_get_binding(_tx):
        return None

    async def fake_strict(_tx):
        return sub_service.VerificationResult(ok=True, transaction={"transactionId": "tx-1"})

    async def fake_update(*_):
        return sub_service.SubscriptionUpdateResult(
            sub_service.SubscriptionUpdateOutcome.OWNERSHIP_MISMATCH,
            reason="app_account_token_mismatch",
        )

    async def boom_mark(*_):
        raise AssertionError("ownership mismatch must not be marked processed")

    monkeypatch.setattr(sub_service, "get_processed_transaction", fake_get_processed)
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", fake_get_binding)
    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service, "update_subscription_from_transaction", fake_update)
    monkeypatch.setattr(sub_service, "mark_transaction_seen", boom_mark)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    with pytest.raises(HTTPException) as exc:
        await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert exc.value.status_code == 409
    assert exc.value.detail == "app_account_token_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (sub_service.SubscriptionUpdateOutcome.UNKNOWN_PRODUCT, "unknown_product"),
        (
            sub_service.SubscriptionUpdateOutcome.MALFORMED_TRANSACTION,
            "missing_or_invalid_expiry",
        ),
    ],
)
async def test_verify_endpoint_rejects_unsupported_or_malformed_as_422(
    monkeypatch, outcome, reason
):
    async def fake_get_processed(*_):
        return None

    async def fake_get_binding(_tx):
        return None

    async def fake_strict(_tx):
        return sub_service.VerificationResult(ok=True, transaction={"transactionId": "tx-1"})

    async def fake_update(*_):
        return sub_service.SubscriptionUpdateResult(outcome, reason=reason)

    monkeypatch.setattr(sub_service, "get_processed_transaction", fake_get_processed)
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", fake_get_binding)
    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service, "update_subscription_from_transaction", fake_update)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    with pytest.raises(HTTPException) as exc:
        await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert exc.value.status_code == 422
    assert exc.value.detail == reason


@pytest.mark.asyncio
async def test_processed_transaction_preserves_outcome_and_legacy_defaults(monkeypatch):
    fake = _install_fake_firestore(monkeypatch)

    await sub_service.mark_transaction_seen(
        "c1", "tx-inactive", sub_service.SubscriptionUpdateOutcome.INACTIVE
    )
    inactive = await sub_service.get_processed_transaction("c1", "tx-inactive")
    assert inactive is not None
    assert inactive["outcome"] == "inactive"

    fake._docs["contractors/c1/transactions/tx-legacy"] = {"processed_at": 1}
    legacy = await sub_service.get_processed_transaction("c1", "tx-legacy")
    assert legacy is not None
    assert legacy["outcome"] == "active"


def test_verify_rate_limit_default_and_backlog_headroom(monkeypatch):
    assert sub_api.VERIFY_RATE_LIMIT == 30
    sub_api._rate_limits.clear()
    monkeypatch.setattr(sub_api.time, "time", lambda: 1_000.0)

    for _ in range(15):
        allowed, retry_after = sub_api._check_rate_limit_with_retry(
            "c1", sub_api.VERIFY_RATE_LIMIT, ":verify", 60
        )
        assert allowed is True
        assert retry_after == 0


def test_verify_rate_limit_computes_retry_after(monkeypatch):
    sub_api._rate_limits.clear()
    monkeypatch.setattr(sub_api.time, "time", lambda: 1_000.0)
    for _ in range(sub_api.VERIFY_RATE_LIMIT):
        assert sub_api._check_rate_limit_with_retry(
            "c1", sub_api.VERIFY_RATE_LIMIT, ":verify", 60
        ) == (True, 0)

    assert sub_api._check_rate_limit_with_retry(
        "c1", sub_api.VERIFY_RATE_LIMIT, ":verify", 60
    ) == (False, 60)


@pytest.mark.asyncio
async def test_verify_endpoint_rate_limit_has_retry_after(monkeypatch):
    async def fake_get_processed(*_):
        return None

    async def fake_get_binding(_tx):
        return None

    monkeypatch.setattr(sub_service, "get_processed_transaction", fake_get_processed)
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", fake_get_binding)
    monkeypatch.setattr(
        sub_api,
        "_check_rate_limit_with_retry",
        lambda *_args, **_kwargs: (False, 42),
    )

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"
    import json as _json
    payload = _json.loads(bytes(response.body).decode())
    assert payload == {
        "status": "retryable",
        "reason": "rate_limited",
        "retry_after_seconds": 42,
    }


# ---------------------------------------------------------------------------
# F-06: cross-contractor receipt reuse rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_transaction_binds_first_caller(monkeypatch):
    _install_fake_firestore(monkeypatch)

    ok, owner = await apple_tx_db.claim_transaction(
        original_transaction_id="orig-1",
        contractor_id="c1",
        transaction_id="tx-1",
        product_id="com.kevin.callscreen.business.monthly",
        environment="Sandbox",
    )
    assert ok is True
    assert owner == "c1"

    binding = await apple_tx_db.get_transaction_binding("orig-1")
    assert binding is not None
    assert binding["contractor_id"] == "c1"


@pytest.mark.asyncio
async def test_claim_transaction_rejects_cross_contractor(monkeypatch):
    _install_fake_firestore(monkeypatch)

    await apple_tx_db.claim_transaction(
        original_transaction_id="orig-1",
        contractor_id="c1",
    )
    ok, owner = await apple_tx_db.claim_transaction(
        original_transaction_id="orig-1",
        contractor_id="c2",
    )
    assert ok is False
    assert owner == "c1"


@pytest.mark.asyncio
async def test_claim_transaction_idempotent_for_same_owner(monkeypatch):
    _install_fake_firestore(monkeypatch)

    await apple_tx_db.claim_transaction(
        original_transaction_id="orig-1",
        contractor_id="c1",
    )
    ok, owner = await apple_tx_db.claim_transaction(
        original_transaction_id="orig-1",
        contractor_id="c1",
    )
    assert ok is True
    assert owner == "c1"


@pytest.mark.asyncio
async def test_update_subscription_rejects_receipt_bound_to_other_contractor(monkeypatch):
    _install_fake_firestore(monkeypatch)

    # Pre-bind to c1.
    await apple_tx_db.claim_transaction(
        original_transaction_id="orig-1",
        contractor_id="c1",
    )

    async def fake_get_contractor(_cid):
        return {"contractor_id": "c2", "subscription_uuid": "uuid-c2"}

    async def fail_update(*args, **kwargs):
        raise AssertionError("must not update subscription on cross-contractor receipt")

    from app.db import contractors as contractors_db
    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fail_update)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)

    transaction_info = {
        "productId": "com.kevin.callscreen.business.monthly",
        "appAccountToken": "uuid-c2",
        "expiresDate": 1770000000000,
        "originalTransactionId": "orig-1",
        "transactionId": "tx-1",
    }

    with pytest.raises(sub_service.CrossContractorReceiptError):
        await sub_service.update_subscription_from_transaction("c2", transaction_info)


@pytest.mark.asyncio
async def test_verify_endpoint_returns_409_on_pre_bound_receipt(monkeypatch):
    async def fake_get_processed(*_):
        return None

    async def fake_get_binding(_tx):
        return {"contractor_id": "other", "original_transaction_id": _tx}

    monkeypatch.setattr(sub_service, "get_processed_transaction", fake_get_processed)
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", fake_get_binding)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    with pytest.raises(HTTPException) as exc:
        await sub_api.verify_subscription(body, _FakeRequest("c1"))
    assert exc.value.status_code == 409
    assert exc.value.detail == "receipt_already_bound"


# ---------------------------------------------------------------------------
# F-12: persistent rate limit on /sign-offer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persistent_rate_limit_blocks_after_limit(monkeypatch):
    _install_fake_firestore(monkeypatch)

    # First N must pass.
    for _ in range(5):
        result = await rl_db.check_and_increment(
            scope="sign_offer", key="c1", limit=5, window_seconds=900,
        )
        assert result.allowed is True

    # 6th must fail with retry-after > 0.
    blocked = await rl_db.check_and_increment(
        scope="sign_offer", key="c1", limit=5, window_seconds=900,
    )
    assert blocked.allowed is False
    assert blocked.retry_after_seconds > 0


@pytest.mark.asyncio
async def test_persistent_rate_limit_separates_keys(monkeypatch):
    _install_fake_firestore(monkeypatch)

    for _ in range(5):
        await rl_db.check_and_increment(
            scope="sign_offer", key="c1", limit=5, window_seconds=900,
        )
    # c2 still has a fresh budget.
    result = await rl_db.check_and_increment(
        scope="sign_offer", key="c2", limit=5, window_seconds=900,
    )
    assert result.allowed is True


@pytest.mark.asyncio
async def test_persistent_rate_limit_window_expires(monkeypatch):
    _install_fake_firestore(monkeypatch)

    base = 1_000_000.0
    for i in range(5):
        await rl_db.check_and_increment(
            scope="sign_offer", key="c1", limit=5, window_seconds=900, now=base + i,
        )
    # Just past the window — old entries pruned.
    later = base + 901
    result = await rl_db.check_and_increment(
        scope="sign_offer", key="c1", limit=5, window_seconds=900, now=later,
    )
    assert result.allowed is True


@pytest.mark.asyncio
async def test_sign_offer_endpoint_returns_429_when_rate_limited(monkeypatch):
    """End-to-end: /sign-offer returns 429 + Retry-After once the persistent
    counter is exhausted, even if signing would have succeeded."""

    async def fake_check_and_increment(**kwargs):
        return rl_db.RateLimitResult(allowed=False, remaining=0, retry_after_seconds=42, count_in_window=5)

    monkeypatch.setattr(rl_db, "check_and_increment", fake_check_and_increment)

    # If signing or claim_promo_slot were called, the test is wrong.
    def boom_sign(*a, **kw):
        raise AssertionError("sign_promotional_offer should not be called when rate-limited")

    async def boom_claim():
        raise AssertionError("claim_promo_slot should not be called when rate-limited")

    monkeypatch.setattr(sub_service, "sign_promotional_offer", boom_sign)
    monkeypatch.setattr(sub_service, "claim_promo_slot", boom_claim)

    body = sub_api.SignOfferRequest(
        contractor_id="c1",
        product_id="com.kevin.callscreen.business.monthly",
        offer_id="founding_member_75off_business",
        application_username="uuid-c1",
    )
    response = await sub_api.sign_offer(body, _FakeRequest("c1"))
    assert getattr(response, "status_code", None) == 429
    assert response.headers.get("Retry-After") == "42"


@pytest.mark.asyncio
async def test_promo_eligible_fails_closed_when_offers_disabled(monkeypatch):
    """Expired server trials must use the regular StoreKit purchase path."""
    monkeypatch.setattr(sub_api.settings, "subscription_promotional_offers_enabled", False)

    async def should_not_check_counter():
        raise AssertionError("disabled offers must not consult or expose the promo budget")

    monkeypatch.setattr(sub_service, "check_promo_eligible", should_not_check_counter)

    result = await sub_api.get_promo_eligible("c1", _FakeRequest("c1"))
    assert result == {"eligible": False}


def test_promotional_offers_default_to_disabled():
    field = sub_api.settings.__class__.model_fields[
        "subscription_promotional_offers_enabled"
    ]
    assert field.default is False


# ---------------------------------------------------------------------------
# Lazy repair for processed active records with missing/malformed binding
# ---------------------------------------------------------------------------

_OMITTED = object()


@pytest.mark.asyncio
async def test_verify_endpoint_legacy_processed_active_repairs_missing_binding(monkeypatch):
    """Legacy processed-active with missing binding repairs binding and enables renewal persistence."""
    fake = _install_fake_firestore(monkeypatch)

    # 1. Pre-seed contractor with missing binding and extra unrelated fields
    fake._docs["contractors/c1"] = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "businessPro",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "subscription_auto_renews": None,
        "subscription_renewal_status_signed_at_ms": None,
        "subscription_forwarded_from": "old-c",
        "business_name": "Acme Pro Plumbing",
        "created_at": 12345,
        "active": True,
    }

    # 2. Mark seen as active
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    apple_calls = []

    async def fake_strict(tx_id):
        apple_calls.append(tx_id)
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-1",
                "originalTransactionId": "orig-1",
                "productId": "com.kevin.callscreen.businesspro.monthly",
                "expiresDate": 1770000000000,
                "appAccountToken": "uuid-c1",
                "environment": "Production",
            },
        )

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)

    # 3. Call verify
    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "updated",
        "outcome": "active",
        "entitlement_active": True,
    }
    assert apple_calls == ["tx-1"]

    # 4. Verify contractor doc mutations: ONLY binding written, renewal fields reset to None, unrelated fields preserved
    doc = fake._docs["contractors/c1"]
    assert doc["subscription_original_transaction_id"] == "orig-1"
    assert doc["subscription_auto_renews"] is None
    assert doc["subscription_renewal_status_signed_at_ms"] is None
    assert doc["subscription_tier"] == "businessPro"
    assert doc["subscription_status"] == "active"
    assert doc["subscription_expires"] == 1770000000.0
    assert doc["subscription_forwarded_from"] == "old-c"
    assert doc["business_name"] == "Acme Pro Plumbing"
    assert doc["created_at"] == 12345
    assert doc["active"] is True

    # 5. Verify global receipt claim was recorded
    binding = await apple_tx_db.get_transaction_binding("orig-1")
    assert binding is not None
    assert binding["contractor_id"] == "c1"

    # 6. Verify that a subsequent renewal webhook for this receipt chain successfully persists renewal status
    import base64 as _b64
    import json as _json

    def _unsigned_jws(payload: dict) -> str:
        header = {"alg": "ES256"}
        def enc(part: dict) -> str:
            raw = _json.dumps(part, separators=(",", ":")).encode()
            return _b64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"{enc(header)}.{enc(payload)}.signature"

    rn_jws = _unsigned_jws({
        "appAccountToken": "uuid-c1",
        "originalTransactionId": "orig-1",
        "autoRenewStatus": 0,
        "signedDate": 1780000000000,
        "productId": "com.kevin.callscreen.businesspro.monthly",
    })
    tx_jws = _unsigned_jws({
        "appAccountToken": "uuid-c1",
        "originalTransactionId": "orig-1",
        "transactionId": "tx-1",
        "productId": "com.kevin.callscreen.businesspro.monthly",
        "expiresDate": 1770000000000,
    })
    notification_payload = {
        "notificationType": "DID_CHANGE_RENEWAL_STATUS",
        "subtype": "AUTO_RENEW_DISABLED",
        "data": {
            "signedTransactionInfo": tx_jws,
            "signedRenewalInfo": rn_jws,
        },
    }

    ok_webhook = await sub_service.handle_appstore_notification(notification_payload)
    assert ok_webhook is True
    doc_after = fake._docs["contractors/c1"]
    assert doc_after["subscription_auto_renews"] is False
    assert doc_after["subscription_renewal_status_signed_at_ms"] == 1780000000000


@pytest.mark.asyncio
async def test_verify_endpoint_processed_active_with_canonical_binding_fast_path(monkeypatch):
    """Processed active with canonical non-empty string binding preserves zero-Apple zero-write fast path."""
    fake = _install_fake_firestore(monkeypatch)

    fake._docs["contractors/c1"] = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": "orig-canonical",
        "active": True,
    }
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }
    snapshot_before = copy.deepcopy(fake._docs)

    def forbidden(name):
        def _fail(*_a, **_kw):
            raise AssertionError(f"canonical active fast path must not call {name}")
        return _fail

    def forbidden_write(*_a, **_kw):
        raise AssertionError("canonical active fast path must not write to Firestore")

    monkeypatch.setattr(_DocRef, "set", forbidden_write)
    monkeypatch.setattr(_DocRef, "update", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "set", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "update", forbidden_write)

    monkeypatch.setattr(sub_service, "verify_transaction_strict", forbidden("verify_transaction_strict"))
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", forbidden("_check_rate_limit_with_retry"))
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", forbidden("claim_transaction"))
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", forbidden("get_transaction_binding"))
    monkeypatch.setattr(sub_service, "update_subscription_from_transaction", forbidden("update_subscription_from_transaction"))
    from app.db import contractors as contractors_db
    monkeypatch.setattr(contractors_db, "conditionally_backfill_subscription_binding", forbidden("conditionally_backfill_subscription_binding"))
    monkeypatch.setattr(contractors_db, "activate_subscription_entitlement", forbidden("activate_subscription_entitlement"))
    monkeypatch.setattr(contractors_db, "update_contractor", forbidden("update_contractor"))
    monkeypatch.setattr(sub_service, "mark_transaction_seen", forbidden("mark_transaction_seen"))
    monkeypatch.setattr(sub_service, "update_processed_transaction_outcome", forbidden("update_processed_transaction_outcome"))

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "already_processed",
        "outcome": "active",
        "entitlement_active": True,
    }
    assert fake._docs == snapshot_before


@pytest.mark.asyncio
async def test_verify_endpoint_processed_inactive_fast_path(monkeypatch):
    """Processed inactive preserves zero-Apple zero-write zero-limiter fast path (does not read contractor)."""
    fake = _install_fake_firestore(monkeypatch)

    fake._docs["contractors/c1"] = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": "orig-canonical",
        "active": True,
    }
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "inactive",
    }
    snapshot_before = copy.deepcopy(fake._docs)

    def forbidden(name):
        def _fail(*_a, **_kw):
            raise AssertionError(f"processed inactive fast path must not call {name}")
        return _fail

    def forbidden_write(*_a, **_kw):
        raise AssertionError("processed inactive fast path must not write to Firestore")

    monkeypatch.setattr(_DocRef, "set", forbidden_write)
    monkeypatch.setattr(_DocRef, "update", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "set", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "update", forbidden_write)

    from app.db import contractors as contractors_db
    monkeypatch.setattr(contractors_db, "get_contractor", forbidden("get_contractor"))
    monkeypatch.setattr(sub_service, "verify_transaction_strict", forbidden("verify_transaction_strict"))
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", forbidden("_check_rate_limit_with_retry"))
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", forbidden("claim_transaction"))
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", forbidden("get_transaction_binding"))
    monkeypatch.setattr(sub_service, "update_subscription_from_transaction", forbidden("update_subscription_from_transaction"))
    monkeypatch.setattr(contractors_db, "conditionally_backfill_subscription_binding", forbidden("conditionally_backfill_subscription_binding"))
    monkeypatch.setattr(contractors_db, "activate_subscription_entitlement", forbidden("activate_subscription_entitlement"))
    monkeypatch.setattr(contractors_db, "update_contractor", forbidden("update_contractor"))
    monkeypatch.setattr(sub_service, "mark_transaction_seen", forbidden("mark_transaction_seen"))
    monkeypatch.setattr(sub_service, "update_processed_transaction_outcome", forbidden("update_processed_transaction_outcome"))

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "already_processed",
        "outcome": "inactive",
        "entitlement_active": False,
    }
    assert fake._docs == snapshot_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_binding",
    [
        None,
        "",
        "   ",
        True,
        False,
        123,
        123.45,
        ["orig-1"],
        {"orig": "1"},
        _OMITTED,
    ],
)
async def test_verify_endpoint_missing_or_malformed_binding_parameter_matrix(monkeypatch, malformed_binding):
    """Missing, None, empty, whitespace-only, bool, number, list, or dict binding enters repair."""
    fake = _install_fake_firestore(monkeypatch)

    contractor_data = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "active": True,
    }
    if malformed_binding is not _OMITTED:
        contractor_data["subscription_original_transaction_id"] = malformed_binding

    fake._docs["contractors/c1"] = contractor_data
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    called_apple = []

    async def fake_strict(tx_id):
        called_apple.append(tx_id)
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-1",
                "originalTransactionId": "orig-matrix",
                "productId": "com.kevin.callscreen.personal.monthly",
                "expiresDate": 1770000000000,
                "appAccountToken": "uuid-c1",
            },
        )

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response["status"] == "ok"
    assert response["message"] == "updated"
    assert response["outcome"] == "active"
    assert called_apple == ["tx-1"]
    assert fake._docs["contractors/c1"]["subscription_original_transaction_id"] == "orig-matrix"


@pytest.mark.asyncio
async def test_verify_endpoint_repair_rate_limited_returns_429(monkeypatch):
    """Processed active with missing binding returns 429 when rate limited with zero downstream calls or writes."""
    fake = _install_fake_firestore(monkeypatch)

    fake._docs["contractors/c1"] = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }
    snapshot_before = copy.deepcopy(fake._docs)

    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (False, 45))

    def forbidden(name):
        def _fail(*_a, **_kw):
            raise AssertionError(f"rate limited repair must not call {name}")
        return _fail

    def forbidden_write(*_a, **_kw):
        raise AssertionError("rate limited repair must not write to Firestore")

    monkeypatch.setattr(_DocRef, "set", forbidden_write)
    monkeypatch.setattr(_DocRef, "update", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "set", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "update", forbidden_write)

    monkeypatch.setattr(sub_service, "verify_transaction_strict", forbidden("verify_transaction_strict"))
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", forbidden("claim_transaction"))
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", forbidden("get_transaction_binding"))
    monkeypatch.setattr(sub_service, "update_subscription_from_transaction", forbidden("update_subscription_from_transaction"))
    from app.db import contractors as contractors_db
    monkeypatch.setattr(contractors_db, "conditionally_backfill_subscription_binding", forbidden("conditionally_backfill_subscription_binding"))
    monkeypatch.setattr(contractors_db, "activate_subscription_entitlement", forbidden("activate_subscription_entitlement"))
    monkeypatch.setattr(contractors_db, "update_contractor", forbidden("update_contractor"))
    monkeypatch.setattr(sub_service, "mark_transaction_seen", forbidden("mark_transaction_seen"))
    monkeypatch.setattr(sub_service, "update_processed_transaction_outcome", forbidden("update_processed_transaction_outcome"))

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert getattr(response, "status_code", None) == 429
    assert response.headers.get("Retry-After") == "45"
    import json as _json
    content = _json.loads(bytes(response.body).decode())
    assert content == {
        "status": "retryable",
        "reason": "rate_limited",
        "retry_after_seconds": 45,
    }
    assert fake._docs == snapshot_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "verification_res", "expected_status", "expected_detail_or_reason"),
    [
        (
            "apple_unreachable",
            sub_service.VerificationResult(ok=False, unreachable=True, reason="apple_timeout"),
            502,
            "apple_timeout",
        ),
        (
            "apple_rejected",
            sub_service.VerificationResult(ok=False, unreachable=False, reason="not_found"),
            400,
            "verification_rejected:not_found",
        ),
        (
            "tx_id_mismatch",
            sub_service.VerificationResult(
                ok=True,
                transaction={
                    "transactionId": "tx-different",
                    "originalTransactionId": "orig-1",
                    "productId": "com.kevin.callscreen.personal.monthly",
                    "expiresDate": 1770000000000,
                    "appAccountToken": "uuid-c1",
                },
            ),
            502,
            "transaction_id_mismatch",
        ),
        (
            "token_mismatch",
            sub_service.VerificationResult(
                ok=True,
                transaction={
                    "transactionId": "tx-1",
                    "originalTransactionId": "orig-1",
                    "productId": "com.kevin.callscreen.personal.monthly",
                    "expiresDate": 1770000000000,
                    "appAccountToken": "uuid-wrong",
                },
            ),
            409,
            "app_account_token_mismatch",
        ),
        (
            "unknown_product",
            sub_service.VerificationResult(
                ok=True,
                transaction={
                    "transactionId": "tx-1",
                    "originalTransactionId": "orig-1",
                    "productId": "com.unknown.product",
                    "expiresDate": 1770000000000,
                    "appAccountToken": "uuid-c1",
                },
            ),
            422,
            "unknown_product",
        ),
        (
            "missing_expiry",
            sub_service.VerificationResult(
                ok=True,
                transaction={
                    "transactionId": "tx-1",
                    "originalTransactionId": "orig-1",
                    "productId": "com.kevin.callscreen.personal.monthly",
                    "appAccountToken": "uuid-c1",
                },
            ),
            422,
            "missing_or_invalid_expiry",
        ),
        (
            "missing_original_tx",
            sub_service.VerificationResult(
                ok=True,
                transaction={
                    "transactionId": "tx-1",
                    "productId": "com.kevin.callscreen.personal.monthly",
                    "expiresDate": 1770000000000,
                    "appAccountToken": "uuid-c1",
                },
            ),
            422,
            "missing_transaction_id",
        ),
        (
            "conflicting_original_tx_aliases",
            sub_service.VerificationResult(
                ok=True,
                transaction={
                    "transactionId": "tx-1",
                    "originalTransactionId": "orig-canon",
                    "original_transaction_id": "orig-alias",
                    "productId": "com.kevin.callscreen.personal.monthly",
                    "expiresDate": 1770000000000,
                    "appAccountToken": "uuid-c1",
                },
            ),
            422,
            "missing_transaction_id",
        ),
    ],
)
async def test_verify_endpoint_repair_error_mappings_and_forbidden_writes(
    monkeypatch, scenario, verification_res, expected_status, expected_detail_or_reason
):
    """Repair error mappings and assert zero contractor writes upon failure."""
    fake = _install_fake_firestore(monkeypatch)

    initial_contractor = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1"] = dict(initial_contractor)
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    async def fake_strict(_tx):
        return verification_res

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")

    if expected_status in (400, 409, 422):
        with pytest.raises(HTTPException) as exc:
            await sub_api.verify_subscription(body, _FakeRequest("c1"))
        assert exc.value.status_code == expected_status
        assert expected_detail_or_reason in exc.value.detail
    elif expected_status == 502:
        response = await sub_api.verify_subscription(body, _FakeRequest("c1"))
        assert getattr(response, "status_code", None) == 502
        assert response.headers.get("Retry-After") == "30"
        import json as _json
        content = _json.loads(bytes(response.body).decode())
        assert content["reason"] == expected_detail_or_reason

    # Forbidden write assertion: contractor document was NOT mutated
    assert fake._docs["contractors/c1"] == initial_contractor


@pytest.mark.asyncio
async def test_verify_endpoint_repair_cross_contractor_claim_rejected(monkeypatch):
    """Cross-contractor receipt claim during repair returns 409 and never mutates contractor."""
    fake = _install_fake_firestore(monkeypatch)

    # Pre-bind orig-1 to c2
    await apple_tx_db.claim_transaction(
        original_transaction_id="orig-1",
        contractor_id="c2",
    )

    initial_contractor = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1"] = dict(initial_contractor)
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    async def fake_strict(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-1",
                "originalTransactionId": "orig-1",
                "productId": "com.kevin.callscreen.personal.monthly",
                "expiresDate": 1770000000000,
                "appAccountToken": "uuid-c1",
            },
        )

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    with pytest.raises(HTTPException) as exc:
        await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert exc.value.status_code == 409
    assert exc.value.detail == "receipt_already_bound"
    assert fake._docs["contractors/c1"] == initial_contractor


@pytest.mark.asyncio
async def test_verify_endpoint_repair_real_transaction_update_failure_propagates(monkeypatch):
    """Underlying transaction.update failure propagates from real backfill helper and yields ASGI HTTP 500."""
    fake = _install_fake_firestore(monkeypatch)

    initial_contractor = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1"] = dict(initial_contractor)
    initial_processed = {
        "processed_at": 1000,
        "outcome": "active",
    }
    fake._docs["contractors/c1/transactions/tx-1"] = dict(initial_processed)

    async def fake_strict(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-1",
                "originalTransactionId": "orig-1",
                "productId": "com.kevin.callscreen.personal.monthly",
                "expiresDate": 1770000000000,
                "appAccountToken": "uuid-c1",
            },
        )

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (True, 0))

    def fail_update(self, doc_ref, value):
        raise RuntimeError("firestore_transaction_update_crashed")

    monkeypatch.setattr(_FakeTransaction, "update", fail_update)

    from app.api.subscription import router as sub_router
    from app.middleware.auth import verify_api_token
    from starlette.middleware.base import BaseHTTPMiddleware

    class _AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.contractor_id = "c1"
            request.state.is_admin = False
            return await call_next(request)

    test_app = FastAPI()
    test_app.add_middleware(_AuthMiddleware)
    test_app.dependency_overrides[verify_api_token] = lambda: None
    test_app.include_router(sub_router)

    transport = httpx.ASGITransport(app=test_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/subscription/verify",
            json={"transaction_id": "tx-1", "contractor_id": "c1"},
        )
        assert response.status_code == 500
    assert "ok" not in response.text
    assert fake._docs["contractors/c1"] == initial_contractor
    assert fake._docs["contractors/c1/transactions/tx-1"] == initial_processed
    # Preceding same-owner global registry claim is documented permitted residual
    claim_rec = await apple_tx_db.get_transaction_binding("orig-1")
    assert claim_rec is not None
    assert claim_rec["contractor_id"] == "c1"


@pytest.mark.asyncio
async def test_verify_endpoint_repair_storage_failure_propagates_500(monkeypatch):
    """Storage failure during repair propagates and does not corrupt state."""
    fake = _install_fake_firestore(monkeypatch)

    fake._docs["contractors/c1"] = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    async def fake_strict(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-1",
                "originalTransactionId": "orig-1",
                "productId": "com.kevin.callscreen.personal.monthly",
                "expiresDate": 1770000000000,
                "appAccountToken": "uuid-c1",
            },
        )

    async def fail_backfill(**_kwargs):
        raise RuntimeError("firestore_connection_lost")

    from app.db import contractors as contractors_db
    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(contractors_db, "conditionally_backfill_subscription_binding", fail_backfill)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    with pytest.raises(RuntimeError, match="firestore_connection_lost"):
        await sub_api.verify_subscription(body, _FakeRequest("c1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_or_expired", ["revoked", "expired"])
async def test_verify_endpoint_repair_revocation_and_past_expiry_terminal_handling(
    monkeypatch, revoked_or_expired
):
    """Revoked or past expired transaction records terminal inactive without updating live binding."""
    fake = _install_fake_firestore(monkeypatch)

    initial_contractor = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1"] = dict(initial_contractor)
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    tx_dict = {
        "transactionId": "tx-1",
        "originalTransactionId": "orig-1",
        "productId": "com.kevin.callscreen.personal.monthly",
        "appAccountToken": "uuid-c1",
    }
    if revoked_or_expired == "revoked":
        tx_dict["expiresDate"] = 1770000000000
        tx_dict["revocationDate"] = 1650000000000
    else:
        tx_dict["expiresDate"] = 1600000000000  # past expiry vs now=1700000000

    async def fake_strict(_tx):
        return sub_service.VerificationResult(ok=True, transaction=tx_dict)

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "terminal_processed",
        "outcome": "inactive",
        "entitlement_active": False,
    }
    # Transaction recorded as inactive
    tx_doc = fake._docs["contractors/c1/transactions/tx-1"]
    assert tx_doc["outcome"] == "inactive"
    # Live contractor was NOT modified
    assert fake._docs["contractors/c1"] == initial_contractor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "tx_product", "tx_exp_ms"),
    [
        ("tier_mismatch", "com.kevin.callscreen.personal.monthly", 1770000000000),
        ("expiry_mismatch", "com.kevin.callscreen.businesspro.monthly", 1710000000000),
    ],
)
async def test_verify_endpoint_repair_old_chain_leaves_global_registry_untouched(
    monkeypatch, scenario, tx_product, tx_exp_ms
):
    """Old chain receipt with different tier or expiry returns 200 already_processed with zero global registry claim."""
    fake = _install_fake_firestore(monkeypatch)
    initial_contractor = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "businessPro",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1"] = dict(initial_contractor)
    fake._docs["contractors/c1/transactions/tx-old"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    async def fake_strict(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-old",
                "originalTransactionId": "orig-old-unclaimed",
                "productId": tx_product,
                "expiresDate": tx_exp_ms,
                "appAccountToken": "uuid-c1",
            },
        )

    def forbidden_claim(*_a, **_kw):
        raise AssertionError("claim_transaction MUST NOT be called when initial fingerprint mismatches")

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (True, 0))
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", forbidden_claim)

    body = sub_api.VerifyRequest(transaction_id="tx-old", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "already_processed",
        "outcome": "active",
        "entitlement_active": True,
    }
    assert fake._docs["contractors/c1"] == initial_contractor
    assert fake._docs["contractors/c1/transactions/tx-old"]["outcome"] == "active"
    assert await apple_tx_db.get_transaction_binding("orig-old-unclaimed") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("diff_type", "tx_product", "tx_exp_ms"),
    [
        ("different_tier", "com.kevin.callscreen.personal.monthly", 1770000000000),
        ("different_expiry", "com.kevin.callscreen.businesspro.monthly", 1710000000000),
    ],
)
async def test_verify_endpoint_repair_old_processed_chain_safe_noop_200(
    monkeypatch, diff_type, tx_product, tx_exp_ms
):
    """Historical processed transaction with different tier or expiry returns safe 200 already_processed and never mutates."""
    fake = _install_fake_firestore(monkeypatch)

    initial_contractor = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "businessPro",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1"] = dict(initial_contractor)
    fake._docs["contractors/c1/transactions/tx-old"] = {
        "processed_at": 1000,
        "outcome": "active",
    }
    snapshot_before = copy.deepcopy(fake._docs)

    async def fake_strict(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-old",
                "originalTransactionId": "orig-old",
                "productId": tx_product,
                "expiresDate": tx_exp_ms,
                "appAccountToken": "uuid-c1",
            },
        )

    def forbidden_claim(*_a, **_kw):
        raise AssertionError("claim_transaction MUST NOT be called for old processed chain safe no-op")

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", forbidden_claim)

    body = sub_api.VerifyRequest(transaction_id="tx-old", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "already_processed",
        "outcome": "active",
        "entitlement_active": True,
    }
    # Zero binding or entitlement mutation
    assert fake._docs == snapshot_before
    assert await apple_tx_db.get_transaction_binding("orig-old") is None


@pytest.mark.asyncio
async def test_verify_endpoint_repair_concurrent_different_binding_not_overwritten(monkeypatch):
    """Concurrent valid receipt B cannot be overwritten by repair A."""
    fake = _install_fake_firestore(monkeypatch)

    fake._docs["contractors/c1"] = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": "orig-concurrent-B",
        "active": True,
    }
    fake._docs["contractors/c1/transactions/tx-A"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    async def fake_strict(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-A",
                "originalTransactionId": "orig-A",
                "productId": "com.kevin.callscreen.personal.monthly",
                "expiresDate": 1770000000000,
                "appAccountToken": "uuid-c1",
            },
        )

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (True, 0))

    body = sub_api.VerifyRequest(transaction_id="tx-A", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "already_processed",
        "outcome": "active",
        "entitlement_active": True,
    }
    assert fake._docs["contractors/c1"]["subscription_original_transaction_id"] == "orig-concurrent-B"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["idempotent_same", "superseded_different"])
async def test_verify_endpoint_repair_idempotent_and_superseded_preserves_renewal_fields_and_unrelated(
    monkeypatch, scenario
):
    """Idempotent same binding and superseded different binding never reset renewal fields or touch unrelated fields."""
    fake = _install_fake_firestore(monkeypatch)

    initial_binding = "orig-1" if scenario == "idempotent_same" else "orig-concurrent-B"
    contractor = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": initial_binding,
        "subscription_auto_renews": False,
        "subscription_renewal_status_signed_at_ms": 1750000000000,
        "business_name": "Acme Plumbing",
        "subscription_forwarded_from": "old-acc",
        "active": True,
    }
    fake._docs["contractors/c1"] = dict(contractor)
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    # Simulate stale initial read with None binding so it enters backfill helper
    stale_initial = dict(contractor)
    stale_initial["subscription_original_transaction_id"] = None
    from app.db import contractors as contractors_db
    async def fake_get_contractor_stale(_cid):
        return dict(stale_initial)
    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor_stale)

    async def fake_strict(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-1",
                "originalTransactionId": "orig-1",
                "productId": "com.kevin.callscreen.personal.monthly",
                "expiresDate": 1770000000000,
                "appAccountToken": "uuid-c1",
            },
        )

    def forbidden_update(*_a, **_kw):
        raise AssertionError("transaction.update MUST NOT be called for idempotent or superseded outcomes")

    monkeypatch.setattr(_FakeTransaction, "update", forbidden_update)

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (True, 0))

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "already_processed",
        "outcome": "active",
        "entitlement_active": True,
    }

    doc = fake._docs["contractors/c1"]
    assert doc["subscription_original_transaction_id"] == initial_binding
    assert doc["subscription_auto_renews"] is False
    assert doc["subscription_renewal_status_signed_at_ms"] == 1750000000000
    assert doc["business_name"] == "Acme Plumbing"
    assert doc["subscription_forwarded_from"] == "old-acc"
    assert doc["subscription_status"] == "active"
    assert doc["subscription_tier"] == "personal"
    assert doc["subscription_expires"] == 1770000000.0


@pytest.mark.asyncio
async def test_verify_endpoint_repair_padded_binding_canonicalization_and_superseded(monkeypatch):
    """Padded live binding canonicalizes on same ID, is protected on different ID, and handles terminal."""
    fake = _install_fake_firestore(monkeypatch)

    # 1. Padded matching binding " orig-1 " canonicalizes to "orig-1" with renewal fields reset,
    # and subsequent record_active_renewal_status successfully updates and persists new renewal facts.
    fake._docs["contractors/c1"] = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": " orig-1 ",
        "subscription_auto_renews": True,
        "subscription_renewal_status_signed_at_ms": 1740000000000,
        "business_name": "Padded Co",
        "subscription_forwarded_from": "prev-acc",
        "active": True,
    }
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    async def fake_strict(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-1",
                "originalTransactionId": "orig-1",
                "productId": "com.kevin.callscreen.personal.monthly",
                "expiresDate": 1770000000000,
                "appAccountToken": "uuid-c1",
            },
        )

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (True, 0))

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    res1 = await sub_api.verify_subscription(body, _FakeRequest("c1"))
    assert res1 == {
        "status": "ok",
        "message": "updated",
        "outcome": "active",
        "entitlement_active": True,
    }
    doc1 = fake._docs["contractors/c1"]
    assert doc1["subscription_original_transaction_id"] == "orig-1"
    assert doc1["subscription_auto_renews"] is None
    assert doc1["subscription_renewal_status_signed_at_ms"] is None
    assert doc1["business_name"] == "Padded Co"
    assert doc1["subscription_forwarded_from"] == "prev-acc"

    # Now verify that subsequent record_active_renewal_status succeeds with canonical binding
    from app.db.contractors import record_active_renewal_status
    ok_renew = await record_active_renewal_status(
        contractor_id="c1",
        expected_subscription_uuid="uuid-c1",
        original_transaction_id="orig-1",
        auto_renews=False,
        signed_at_ms=1780000000000,
    )
    assert ok_renew is True
    doc1_after_renew = fake._docs["contractors/c1"]
    assert doc1_after_renew["subscription_auto_renews"] is False
    assert doc1_after_renew["subscription_renewal_status_signed_at_ms"] == 1780000000000
    assert doc1_after_renew["subscription_original_transaction_id"] == "orig-1"
    assert doc1_after_renew["subscription_status"] == "active"
    assert doc1_after_renew["subscription_tier"] == "personal"
    assert doc1_after_renew["subscription_expires"] == 1770000000.0
    assert doc1_after_renew["business_name"] == "Padded Co"
    assert doc1_after_renew["subscription_forwarded_from"] == "prev-acc"

    # 2. Padded DIFFERENT binding " orig-other " is superseded: never claimed or overwritten, renewal fields preserved
    initial_c2 = {
        "contractor_id": "c2",
        "subscription_uuid": "uuid-c2",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": " orig-other ",
        "subscription_auto_renews": True,
        "subscription_renewal_status_signed_at_ms": 1740000000000,
        "business_name": "Different Co",
        "active": True,
    }
    fake._docs["contractors/c2"] = dict(initial_c2)
    fake._docs["contractors/c2/transactions/tx-2"] = {
        "processed_at": 1000,
        "outcome": "active",
    }
    snapshot_c2 = copy.deepcopy(fake._docs)

    async def fake_strict_2(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-2",
                "originalTransactionId": "orig-2",
                "productId": "com.kevin.callscreen.personal.monthly",
                "expiresDate": 1770000000000,
                "appAccountToken": "uuid-c2",
            },
        )

    real_claim = apple_tx_db.claim_transaction

    def forbidden_claim_c2(*_a, **_kw):
        raise AssertionError("claim_transaction MUST NOT be called for padded different binding")

    def forbidden_update_c2(*_a, **_kw):
        raise AssertionError("transaction.update MUST NOT be called for padded different binding")

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict_2)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", forbidden_claim_c2)
    monkeypatch.setattr(_FakeTransaction, "update", forbidden_update_c2)

    body2 = sub_api.VerifyRequest(transaction_id="tx-2", contractor_id="c2")
    res2 = await sub_api.verify_subscription(body2, _FakeRequest("c2"))
    assert res2 == {
        "status": "ok",
        "message": "already_processed",
        "outcome": "active",
        "entitlement_active": True,
    }
    assert fake._docs == snapshot_c2
    assert await apple_tx_db.get_transaction_binding("orig-2") is None

    # 3. Paired terminal case: padded-different current identity + strictly verified expired transaction
    # claims verified receipt, marks processed tx doc inactive, and leaves contractor document exact.
    initial_c3 = {
        "contractor_id": "c3",
        "subscription_uuid": "uuid-c3",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": " orig-other ",
        "subscription_auto_renews": True,
        "subscription_renewal_status_signed_at_ms": 1740000000000,
        "business_name": "Terminal Co",
        "active": True,
    }
    fake2 = _install_fake_firestore(monkeypatch)
    fake2._docs["contractors/c3"] = dict(initial_c3)
    fake2._docs["contractors/c3/transactions/tx-3"] = {
        "processed_at": 1000,
        "outcome": "active",
        "custom_sentinel": "preserve_me_exact",
    }

    async def fake_strict_3(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-3",
                "originalTransactionId": "orig-term",
                "productId": "com.kevin.callscreen.personal.monthly",
                "expiresDate": 1600000000000,  # expired
                "appAccountToken": "uuid-c3",
            },
        )

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict_3)
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", real_claim)
    monkeypatch.setattr(_FakeTransaction, "update", _FakeTransaction.update)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (True, 0))

    body3 = sub_api.VerifyRequest(transaction_id="tx-3", contractor_id="c3")
    res3 = await sub_api.verify_subscription(body3, _FakeRequest("c3"))
    assert res3 == {
        "status": "ok",
        "message": "terminal_processed",
        "outcome": "inactive",
        "entitlement_active": False,
    }
    assert fake2._docs["contractors/c3"] == initial_c3
    assert fake2._docs["contractors/c3/transactions/tx-3"] == {
        "processed_at": 1000,
        "outcome": "inactive",
        "custom_sentinel": "preserve_me_exact",
    }
    binding = await apple_tx_db.get_transaction_binding("orig-term")
    assert binding is not None
    assert binding["contractor_id"] == "c3"
    assert binding["last_transaction_id"] == "tx-3"
    assert fake2._docs["contractors/c3"]["subscription_original_transaction_id"] == " orig-other "
    assert fake2._docs["contractors/c3"]["subscription_status"] == "active"


@pytest.mark.asyncio
async def test_mark_transaction_seen_creates_new_record_with_timestamp_when_absent(monkeypatch):
    """Creating a new processed transaction sets processed_at and outcome."""
    fake = _install_fake_firestore(monkeypatch)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000.0)
    await sub_service.mark_transaction_seen(
        contractor_id="c1",
        transaction_id="tx-new",
        outcome=sub_service.SubscriptionUpdateOutcome.ACTIVE,
    )
    assert fake._docs["contractors/c1/transactions/tx-new"] == {
        "processed_at": 1700000000.0,
        "outcome": "active",
    }


@pytest.mark.asyncio
async def test_update_processed_transaction_outcome_helper_seam(monkeypatch):
    """update_processed_transaction_outcome helper updates only outcome on existing records and preserves all other fields."""
    fake = _install_fake_firestore(monkeypatch)
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000.0,
        "outcome": "active",
        "custom_sentinel": "preserve_val",
    }
    await sub_service.update_processed_transaction_outcome(
        contractor_id="c1",
        transaction_id="tx-1",
        outcome=sub_service.SubscriptionUpdateOutcome.INACTIVE,
    )
    assert fake._docs["contractors/c1/transactions/tx-1"] == {
        "processed_at": 1000.0,
        "outcome": "inactive",
        "custom_sentinel": "preserve_val",
    }


@pytest.mark.asyncio
async def test_update_processed_transaction_outcome_propagates_missing_document_failure(monkeypatch):
    """update_processed_transaction_outcome raises on missing document and never creates a record silently."""
    fake = _install_fake_firestore(monkeypatch)
    with pytest.raises(KeyError):
        await sub_service.update_processed_transaction_outcome(
            contractor_id="c1",
            transaction_id="tx-nonexistent",
            outcome=sub_service.SubscriptionUpdateOutcome.INACTIVE,
        )
    assert "contractors/c1/transactions/tx-nonexistent" not in fake._docs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "live_mutation", "expected_outcome", "expected_msg", "contractor_exists"),
    [
        (
            "became_inactive",
            {"active": False},
            "active",
            "already_processed",
            True,
        ),
        (
            "status_expired",
            {"subscription_status": "expired"},
            "active",
            "already_processed",
            True,
        ),
        (
            "uuid_changed",
            {"subscription_uuid": "uuid-rebound"},
            "409_ownership",
            "",
            True,
        ),
        (
            "tier_upgraded",
            {"subscription_tier": "business"},
            "active",
            "already_processed",
            True,
        ),
        (
            "expiry_drifted",
            {"subscription_expires": 1790000000.0},
            "active",
            "already_processed",
            True,
        ),
        (
            "contractor_deleted",
            {},
            "active",
            "already_processed",
            False,
        ),
    ],
)
async def test_verify_endpoint_repair_transactional_drift_races_using_real_helper(
    monkeypatch, scenario, live_mutation, expected_outcome, expected_msg, contractor_exists
):
    """Races between initial read and Firestore transaction return safe 200 no-op or 409 on UUID mismatch."""
    fake = _install_fake_firestore(monkeypatch)

    initial_contractor = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1"] = dict(initial_contractor)
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    # Inject live document drift into fake._docs right before transactional backfill executes
    from app.db import contractors as contractors_db
    real_cond_backfill = contractors_db.conditionally_backfill_subscription_binding

    async def hook_backfill(**kwargs):
        if not contractor_exists:
            fake._docs.pop("contractors/c1", None)
        else:
            fake._docs["contractors/c1"].update(live_mutation)
        return await real_cond_backfill(**kwargs)

    monkeypatch.setattr(contractors_db, "conditionally_backfill_subscription_binding", hook_backfill)

    async def fake_strict(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": "tx-1",
                "originalTransactionId": "orig-1",
                "productId": "com.kevin.callscreen.personal.monthly",
                "expiresDate": 1770000000000,
                "appAccountToken": "uuid-c1",
            },
        )

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (True, 0))

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")

    if expected_outcome == "409_ownership":
        with pytest.raises(HTTPException) as exc:
            await sub_api.verify_subscription(body, _FakeRequest("c1"))
        assert exc.value.status_code == 409
        assert exc.value.detail == "ownership_mismatch"
    else:
        res = await sub_api.verify_subscription(body, _FakeRequest("c1"))
        assert res == {
            "status": "ok",
            "message": expected_msg,
            "outcome": expected_outcome,
            "entitlement_active": True,
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "tx_patch", "expected_status", "expected_detail_or_reason"),
    [
        (
            "unreachable_500",
            {},
            502,
            "apple_500",
        ),
        (
            "authoritative_404",
            {},
            400,
            "invalid",
        ),
        (
            "transaction_id_mismatch",
            {"transactionId": "tx-other"},
            502,
            "transaction_id_mismatch",
        ),
        (
            "missing_original_id",
            {"originalTransactionId": ""},
            422,
            "missing_transaction_id",
        ),
        (
            "unknown_product",
            {"productId": "com.kevin.callscreen.invalid"},
            422,
            "unknown_product",
        ),
        (
            "app_account_token_mismatch",
            {"appAccountToken": "uuid-other"},
            409,
            "app_account_token_mismatch",
        ),
    ],
)
async def test_verify_endpoint_repair_expired_or_revoked_with_identity_failures_rejects_and_zero_processed_mutation(
    monkeypatch, scenario, tx_patch, expected_status, expected_detail_or_reason
):
    """Expired/revoked Apple transaction with identity/token/product/network failure NEVER marks processed tx inactive."""
    fake = _install_fake_firestore(monkeypatch)

    initial_contractor = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1"] = dict(initial_contractor)
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }

    base_tx = {
        "transactionId": "tx-1",
        "originalTransactionId": "orig-1",
        "productId": "com.kevin.callscreen.personal.monthly",
        "expiresDate": 1600000000000,  # Expired
        "appAccountToken": "uuid-c1",
    }
    base_tx.update(tx_patch)

    if scenario == "unreachable_500":
        verification_res = sub_service.VerificationResult(
            ok=False,
            unreachable=True,
            reason="apple_500",
        )
    elif scenario == "authoritative_404":
        verification_res = sub_service.VerificationResult(
            ok=False,
            unreachable=False,
            reason="invalid",
        )
    else:
        verification_res = sub_service.VerificationResult(
            ok=True,
            transaction=base_tx,
        )

    async def fake_strict(_tx):
        return verification_res

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (True, 0))

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")

    if expected_status in (400, 409, 422):
        with pytest.raises(HTTPException) as exc:
            await sub_api.verify_subscription(body, _FakeRequest("c1"))
        assert exc.value.status_code == expected_status
        assert expected_detail_or_reason in exc.value.detail
    elif expected_status == 502:
        response = await sub_api.verify_subscription(body, _FakeRequest("c1"))
        assert getattr(response, "status_code", None) == 502
        assert response.headers.get("Retry-After") == "30"
        import json as _json
        content = _json.loads(bytes(response.body).decode())
        assert content["reason"] == expected_detail_or_reason

    # Strict Zero-Mutation Check: processed record must NOT be changed to inactive
    assert fake._docs["contractors/c1/transactions/tx-1"]["outcome"] == "active"
    assert fake._docs["contractors/c1"] == initial_contractor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "tx_patch", "expected_status", "expected_outcome", "is_terminal"),
    [
        ("absent", {}, 200, "updated", False),
        ("valid_canon", {"revocationDate": 1650000000000}, 200, "terminal_processed", True),
        ("valid_alias", {"revokedDate": 1650000000000}, 200, "terminal_processed", True),
        ("both_equal_int", {"revocationDate": 1650000000000, "revokedDate": 1650000000000}, 200, "terminal_processed", True),
        ("both_equal_float", {"revocationDate": 1650000000000, "revokedDate": 1650000000000.0}, 200, "terminal_processed", True),
        ("both_disagree_1ms", {"revocationDate": 1650000000001, "revokedDate": 1650000000000}, 422, "conflicting_revocation_date", False),
        ("both_disagree_large", {"revocationDate": 1650000000000, "revokedDate": 1660000000000}, 422, "conflicting_revocation_date", False),
        ("malformed_canon_str", {"revocationDate": "bad"}, 422, "malformed_revocation_date", False),
        ("malformed_alias_str", {"revokedDate": "bad"}, 422, "malformed_revocation_date", False),
        ("malformed_none", {"revocationDate": None}, 422, "malformed_revocation_date", False),
        ("malformed_true", {"revocationDate": True}, 422, "malformed_revocation_date", False),
        ("malformed_false", {"revocationDate": False}, 422, "malformed_revocation_date", False),
        ("malformed_zero", {"revocationDate": 0}, 422, "malformed_revocation_date", False),
        ("malformed_neg", {"revocationDate": -100}, 422, "malformed_revocation_date", False),
        ("malformed_nan", {"revocationDate": float("nan")}, 422, "malformed_revocation_date", False),
        ("malformed_inf", {"revocationDate": float("inf")}, 422, "malformed_revocation_date", False),
        ("malformed_list", {"revocationDate": [123]}, 422, "malformed_revocation_date", False),
        ("malformed_dict", {"revocationDate": {"date": 123}}, 422, "malformed_revocation_date", False),
        ("malformed_empty_str", {"revocationDate": ""}, 422, "malformed_revocation_date", False),
        ("malformed_empty_list", {"revocationDate": []}, 422, "malformed_revocation_date", False),
        ("malformed_empty_dict", {"revocationDate": {}}, 422, "malformed_revocation_date", False),
    ],
)
async def test_verify_endpoint_repair_revocation_aliases_and_malformed_matrix(
    monkeypatch, scenario, tx_patch, expected_status, expected_outcome, is_terminal
):
    """Strict revocation parsing across aliases and malformed types with forbidden claim sentinel on error."""
    fake = _install_fake_firestore(monkeypatch)

    initial_contractor = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1"] = dict(initial_contractor)
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }
    snapshot_before = copy.deepcopy(fake._docs)

    base_tx = {
        "transactionId": "tx-1",
        "originalTransactionId": "orig-1",
        "productId": "com.kevin.callscreen.personal.monthly",
        "expiresDate": 1770000000000,
        "appAccountToken": "uuid-c1",
    }
    base_tx.update(tx_patch)

    async def fake_strict(_tx):
        return sub_service.VerificationResult(ok=True, transaction=dict(base_tx))

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (True, 0))

    if expected_status == 422:
        def forbidden_fn(name):
            def _fail(*_a, **_kw):
                raise AssertionError(f"malformed revocation must not call {name}")
            return _fail

        def forbidden_write(*_a, **_kw):
            raise AssertionError("malformed revocation must not write to Firestore")

        monkeypatch.setattr(_DocRef, "set", forbidden_write)
        monkeypatch.setattr(_DocRef, "update", forbidden_write)
        monkeypatch.setattr(_FakeTransaction, "set", forbidden_write)
        monkeypatch.setattr(_FakeTransaction, "update", forbidden_write)

        monkeypatch.setattr("app.db.apple_transactions.claim_transaction", forbidden_fn("claim_transaction"))
        monkeypatch.setattr(sub_service, "update_subscription_from_transaction", forbidden_fn("update_subscription_from_transaction"))
        monkeypatch.setattr(sub_service, "mark_transaction_seen", forbidden_fn("mark_transaction_seen"))
        monkeypatch.setattr(sub_service, "update_processed_transaction_outcome", forbidden_fn("update_processed_transaction_outcome"))
        from app.db import contractors as contractors_db
        monkeypatch.setattr(contractors_db, "conditionally_backfill_subscription_binding", forbidden_fn("conditionally_backfill_subscription_binding"))
        monkeypatch.setattr(contractors_db, "activate_subscription_entitlement", forbidden_fn("activate_subscription_entitlement"))
        monkeypatch.setattr(contractors_db, "update_contractor", forbidden_fn("update_contractor"))

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")

    if expected_status == 422:
        with pytest.raises(HTTPException) as exc:
            await sub_api.verify_subscription(body, _FakeRequest("c1"))
        assert exc.value.status_code == 422
        assert exc.value.detail == expected_outcome
        assert fake._docs == snapshot_before
        assert await apple_tx_db.get_transaction_binding("orig-1") is None
    elif is_terminal:
        res = await sub_api.verify_subscription(body, _FakeRequest("c1"))
        assert res == {
            "status": "ok",
            "message": "terminal_processed",
            "outcome": "inactive",
            "entitlement_active": False,
        }
        assert fake._docs["contractors/c1/transactions/tx-1"]["outcome"] == "inactive"
        assert fake._docs["contractors/c1"] == initial_contractor
    else:
        res = await sub_api.verify_subscription(body, _FakeRequest("c1"))
        assert res == {
            "status": "ok",
            "message": "updated",
            "outcome": "active",
            "entitlement_active": True,
        }
        assert fake._docs["contractors/c1"]["subscription_original_transaction_id"] == "orig-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_rev",
    [
        True,
        False,
        0,
        -1,
        "2026",
        float("nan"),
        float("inf"),
        ["123"],
        {"date": 123},
    ],
)
async def test_update_subscription_from_transaction_malformed_revocation_before_claim(monkeypatch, bad_rev):
    """Direct update_subscription_from_transaction rejects malformed revocation before claiming."""
    fake = _install_fake_firestore(monkeypatch)
    fake._docs["contractors/c1"] = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "active": True,
    }
    snapshot_before = copy.deepcopy(fake._docs)

    def forbidden(name):
        def _fail(*_a, **_kw):
            raise AssertionError(f"direct update malformed revocation must not call {name}")
        return _fail

    def forbidden_write(*_a, **_kw):
        raise AssertionError("direct update malformed revocation must not write to Firestore")

    monkeypatch.setattr(_DocRef, "set", forbidden_write)
    monkeypatch.setattr(_DocRef, "update", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "set", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "update", forbidden_write)

    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", forbidden("claim_transaction"))
    from app.db import contractors as contractors_db
    monkeypatch.setattr(contractors_db, "activate_subscription_entitlement", forbidden("activate_subscription_entitlement"))
    monkeypatch.setattr(contractors_db, "update_contractor", forbidden("update_contractor"))

    tx_dict = {
        "transactionId": "tx-1",
        "originalTransactionId": "orig-1",
        "productId": "com.kevin.callscreen.personal.monthly",
        "expiresDate": 1770000000000,
        "appAccountToken": "uuid-c1",
        "revocationDate": bad_rev,
    }
    res = await sub_service.update_subscription_from_transaction("c1", tx_dict)
    assert res.outcome is sub_service.SubscriptionUpdateOutcome.MALFORMED_TRANSACTION
    assert res.reason == "malformed_revocation_date"
    assert fake._docs == snapshot_before
    assert await apple_tx_db.get_transaction_binding("orig-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_product",
    [
        None,
        "",
        "   ",
        True,
        False,
        123,
        12.34,
        ["com.kevin.callscreen.personal.monthly"],
        {"product": "personal"},
    ],
)
async def test_verify_endpoint_repair_product_id_wrong_types_and_blank_matrix(monkeypatch, bad_product):
    """Invalid product ID types and blanks raise 422 before claim with zero writes."""
    fake = _install_fake_firestore(monkeypatch)
    fake._docs["contractors/c1"] = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }
    snapshot_before = copy.deepcopy(fake._docs)

    tx_dict = {
        "transactionId": "tx-1",
        "originalTransactionId": "orig-1",
        "productId": bad_product,
        "expiresDate": 1770000000000,
        "appAccountToken": "uuid-c1",
    }

    async def fake_strict(_tx):
        return sub_service.VerificationResult(ok=True, transaction=tx_dict)

    class _ForbiddenLookupDict(dict):
        def get(self, *args, **kwargs):
            raise AssertionError("PRODUCT_TO_TIER.get MUST NOT be called when product_id is non-string or blank")
        def __getitem__(self, item):
            raise AssertionError("PRODUCT_TO_TIER lookup MUST NOT be called when product_id is non-string or blank")

    forbidden_product_dict = _ForbiddenLookupDict()

    def forbidden_claim(*_a, **_kw):
        raise AssertionError("claim_transaction MUST NOT be called on bad product ID")

    def forbidden_write(*_a, **_kw):
        raise AssertionError("bad product ID must not write to Firestore")

    monkeypatch.setattr(_DocRef, "set", forbidden_write)
    monkeypatch.setattr(_DocRef, "update", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "set", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "update", forbidden_write)

    monkeypatch.setattr(sub_service, "PRODUCT_TO_TIER", forbidden_product_dict)
    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (True, 0))
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", forbidden_claim)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    with pytest.raises(HTTPException) as exc:
        await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert exc.value.status_code == 422
    assert exc.value.detail == "unknown_product"
    assert fake._docs == snapshot_before
    assert await apple_tx_db.get_transaction_binding("orig-1") is None


@pytest.mark.asyncio
async def test_verify_endpoint_repair_contractor_disappearance_returns_200_safe_noop(monkeypatch):
    """Contractor disappearance between processed lookup and live read returns 200 already_processed with zero claim/write."""
    fake = _install_fake_firestore(monkeypatch)

    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }
    # Notice: "contractors/c1" does NOT exist in fake._docs -> get_contractor("c1") returns None
    snapshot_before = copy.deepcopy(fake._docs)

    def forbidden(name):
        def _fail(*_a, **_kw):
            raise AssertionError(f"contractor disappearance must not call {name}")
        return _fail

    def forbidden_write(*_a, **_kw):
        raise AssertionError("contractor disappearance must not write to Firestore")

    monkeypatch.setattr(_DocRef, "set", forbidden_write)
    monkeypatch.setattr(_DocRef, "update", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "set", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "update", forbidden_write)

    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", forbidden("claim_transaction"))
    monkeypatch.setattr(apple_tx_db, "get_transaction_binding", forbidden("get_transaction_binding"))
    monkeypatch.setattr(sub_service, "verify_transaction_strict", forbidden("verify_transaction_strict"))
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", forbidden("_check_rate_limit_with_retry"))
    monkeypatch.setattr(sub_service, "update_subscription_from_transaction", forbidden("update_subscription_from_transaction"))
    from app.db import contractors as contractors_db
    monkeypatch.setattr(contractors_db, "conditionally_backfill_subscription_binding", forbidden("conditionally_backfill_subscription_binding"))
    monkeypatch.setattr(contractors_db, "activate_subscription_entitlement", forbidden("activate_subscription_entitlement"))
    monkeypatch.setattr(contractors_db, "update_contractor", forbidden("update_contractor"))
    monkeypatch.setattr(sub_service, "mark_transaction_seen", forbidden("mark_transaction_seen"))
    monkeypatch.setattr(sub_service, "update_processed_transaction_outcome", forbidden("update_processed_transaction_outcome"))

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert response == {
        "status": "ok",
        "message": "already_processed",
        "outcome": "active",
        "entitlement_active": True,
    }
    assert fake._docs == snapshot_before
    assert "contractors/c1" not in fake._docs
    assert fake._docs["contractors/c1/transactions/tx-1"]["outcome"] == "active"


@pytest.mark.asyncio
async def test_verify_endpoint_repair_padded_apple_transaction_id_rejected_502(monkeypatch):
    """Apple verified transactionId with whitespace padding (' tx-1 ') is rejected with 502 and zero claims/writes."""
    fake = _install_fake_firestore(monkeypatch)

    initial_contractor = {
        "contractor_id": "c1",
        "subscription_uuid": "uuid-c1",
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1770000000.0,
        "subscription_original_transaction_id": None,
        "active": True,
    }
    fake._docs["contractors/c1"] = dict(initial_contractor)
    fake._docs["contractors/c1/transactions/tx-1"] = {
        "processed_at": 1000,
        "outcome": "active",
    }
    snapshot_before = copy.deepcopy(fake._docs)

    async def fake_strict(_tx):
        return sub_service.VerificationResult(
            ok=True,
            transaction={
                "transactionId": " tx-1 ",
                "originalTransactionId": "orig-1",
                "productId": "com.kevin.callscreen.personal.monthly",
                "expiresDate": 1770000000000,
                "appAccountToken": "uuid-c1",
            },
        )

    def forbidden_claim(*_a, **_kw):
        raise AssertionError("claim_transaction MUST NOT be called when verified transactionId has whitespace padding")

    def forbidden_write(*_a, **_kw):
        raise AssertionError("padded transactionId must not write to Firestore")

    monkeypatch.setattr(_DocRef, "set", forbidden_write)
    monkeypatch.setattr(_DocRef, "update", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "set", forbidden_write)
    monkeypatch.setattr(_FakeTransaction, "update", forbidden_write)

    monkeypatch.setattr(sub_service, "verify_transaction_strict", fake_strict)
    monkeypatch.setattr(sub_service.time, "time", lambda: 1700000000)
    monkeypatch.setattr(sub_api, "_check_rate_limit_with_retry", lambda *a, **kw: (True, 0))
    monkeypatch.setattr("app.db.apple_transactions.claim_transaction", forbidden_claim)

    body = sub_api.VerifyRequest(transaction_id="tx-1", contractor_id="c1")
    response = await sub_api.verify_subscription(body, _FakeRequest("c1"))

    assert getattr(response, "status_code", None) == 502
    assert response.headers.get("Retry-After") == "30"
    import json as _json
    content = _json.loads(bytes(response.body).decode())
    assert content == {
        "status": "retryable",
        "reason": "transaction_id_mismatch",
        "retry_after_seconds": 30,
    }
    assert fake._docs == snapshot_before
    assert await apple_tx_db.get_transaction_binding("orig-1") is None
