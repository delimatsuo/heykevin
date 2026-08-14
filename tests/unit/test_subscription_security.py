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
from typing import Any, Dict, Optional

import pytest
from fastapi import HTTPException
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
    def __init__(self, fs: _InMemoryFirestore, name: str):
        self._fs = fs
        self._name = name

    def document(self, doc_id: str):
        return _DocRef(self._fs, f"{self._name}/{doc_id}")


class _Snapshot:
    def __init__(self, exists: bool, data: Optional[dict]):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return dict(self._data)


class _DocRef:
    def __init__(self, fs: _InMemoryFirestore, path: str):
        self._fs = fs
        self._path = path

    def get(self, transaction=None):
        data = self._fs._docs.get(self._path)
        return _Snapshot(data is not None, data)

    def set(self, value: dict, merge: bool = False):
        if merge and self._path in self._fs._docs:
            current = self._fs._docs[self._path]
            current.update(value)
        else:
            self._fs._docs[self._path] = dict(value)


class _FakeTransaction:
    """Mimics google.cloud.firestore.Transaction for our two helpers."""

    def __init__(self, fs: _InMemoryFirestore):
        self._fs = fs

    def set(self, doc_ref: _DocRef, value: dict, merge: bool = False):
        doc_ref.set(value, merge=merge)


def _install_fake_firestore(monkeypatch) -> _InMemoryFirestore:
    """Patch get_firestore_client + the @fs.transactional decorator."""
    fake = _InMemoryFirestore()

    monkeypatch.setattr(
        "app.db.firestore_client.get_firestore_client",
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

    async def fail_get_binding(_tx):
        raise AssertionError("duplicate transaction should not check global binding")

    async def fail_strict(_tx):
        raise AssertionError("duplicate transaction should not call Apple")

    monkeypatch.setattr(sub_service, "get_processed_transaction", fake_get_processed)
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
