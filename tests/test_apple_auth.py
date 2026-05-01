"""Tests for Apple Sign-In identity-token verification (F-01 / F-03)."""

from __future__ import annotations

import base64
import os
import time
from types import SimpleNamespace
from typing import Optional

# Required env vars for app.config to import cleanly.
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt as jose_jwt

from app.api import contractors as contractors_api
from app.services import apple_auth
from app.services.apple_auth import (
    AppleAuthError,
    DEFAULT_AUDIENCE,
    verify_apple_identity_token,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64u_int(i: int) -> str:
    raw = i.to_bytes((i.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class _SigningKey:
    """A locally-generated RSA key paired with its JWKS representation."""

    def __init__(self, kid: str = "test-kid"):
        self.kid = kid
        self._priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = self._priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub = self._priv.public_key().public_numbers()
        self.jwk = {
            "kty": "RSA",
            "kid": kid,
            "use": "sig",
            "alg": "RS256",
            "n": _b64u_int(pub.n),
            "e": _b64u_int(pub.e),
        }

    def jwks(self) -> dict:
        return {"keys": [self.jwk]}

    def sign(
        self,
        *,
        sub: str = "001234.deadbeef.5678",
        aud: str = DEFAULT_AUDIENCE,
        iss: str = "https://appleid.apple.com",
        exp: Optional[int] = None,
        iat: Optional[int] = None,
        email: Optional[str] = "user@privaterelay.appleid.com",
        algorithm: str = "RS256",
        headers: Optional[dict] = None,
    ) -> str:
        now = int(time.time())
        payload: dict = {
            "iss": iss,
            "aud": aud,
            "sub": sub,
            "exp": exp if exp is not None else now + 3600,
            "iat": iat if iat is not None else now,
        }
        if email is not None:
            payload["email"] = email
            payload["email_verified"] = "true"
        head = {"kid": self.kid}
        if headers:
            head.update(headers)
        return jose_jwt.encode(payload, self.pem, algorithm=algorithm, headers=head)


@pytest.fixture
def signing_key() -> _SigningKey:
    return _SigningKey()


@pytest.fixture(autouse=True)
def _reset_jwks_cache():
    apple_auth._reset_cache_for_tests()
    yield
    apple_auth._reset_cache_for_tests()


@pytest.fixture
def patch_jwks(monkeypatch):
    """Return a function that installs a fake _fetch_jwks returning the given JWKS."""

    def _install(jwks: dict):
        async def _fake(force_refresh: bool = False) -> dict:
            return jwks

        monkeypatch.setattr(apple_auth, "_fetch_jwks", _fake)

    return _install


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_happy_path_returns_claims(signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())
    token = signing_key.sign(sub="apple-user-1", email="caller@example.com")

    identity = await verify_apple_identity_token(token, "apple-user-1")

    assert identity.sub == "apple-user-1"
    assert identity.aud == DEFAULT_AUDIENCE
    assert identity.iss == "https://appleid.apple.com"
    assert identity.email == "caller@example.com"
    assert identity.email_verified is True


@pytest.mark.asyncio
async def test_wrong_audience_raises(signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())
    token = signing_key.sign(sub="apple-user-1", aud="com.example.other")

    with pytest.raises(AppleAuthError):
        await verify_apple_identity_token(token, "apple-user-1")


@pytest.mark.asyncio
async def test_wrong_issuer_raises(signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())
    token = signing_key.sign(sub="apple-user-1", iss="https://evil.example/")

    with pytest.raises(AppleAuthError):
        await verify_apple_identity_token(token, "apple-user-1")


@pytest.mark.asyncio
async def test_expired_token_raises(signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())
    # Expire well before any 60s leeway window.
    token = signing_key.sign(sub="apple-user-1", exp=int(time.time()) - 600)

    with pytest.raises(AppleAuthError):
        await verify_apple_identity_token(token, "apple-user-1")


@pytest.mark.asyncio
async def test_sub_mismatch_raises(signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())
    token = signing_key.sign(sub="apple-user-attacker")

    with pytest.raises(AppleAuthError):
        await verify_apple_identity_token(token, "apple-user-victim")


@pytest.mark.asyncio
async def test_bad_signature_raises(signing_key, patch_jwks):
    # JWKS we serve is for a different key than the one that actually signed.
    other = _SigningKey(kid=signing_key.kid)
    patch_jwks(signing_key.jwks())  # serve the *wrong* public key
    token = other.sign(sub="apple-user-1")

    with pytest.raises(AppleAuthError):
        await verify_apple_identity_token(token, "apple-user-1")


@pytest.mark.asyncio
async def test_empty_token_raises(signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())

    with pytest.raises(AppleAuthError):
        await verify_apple_identity_token("", "apple-user-1")


@pytest.mark.asyncio
async def test_empty_expected_user_id_raises(signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())
    token = signing_key.sign(sub="apple-user-1")

    with pytest.raises(AppleAuthError):
        await verify_apple_identity_token(token, "")


@pytest.mark.asyncio
async def test_unknown_kid_raises(signing_key, patch_jwks):
    # JWKS contains a key whose kid does not match the token header.
    other = _SigningKey(kid="rotated-kid")
    patch_jwks(other.jwks())
    token = signing_key.sign(sub="apple-user-1")  # signed with kid=test-kid

    with pytest.raises(AppleAuthError):
        await verify_apple_identity_token(token, "apple-user-1")


@pytest.mark.asyncio
async def test_unsupported_algorithm_raises(signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())
    # HS256 token (different algo). The module rejects non-RS256 in the header.
    hs_token = jose_jwt.encode(
        {
            "iss": "https://appleid.apple.com",
            "aud": DEFAULT_AUDIENCE,
            "sub": "apple-user-1",
            "exp": int(time.time()) + 3600,
        },
        "sharedsecret",
        algorithm="HS256",
        headers={"kid": signing_key.kid},
    )

    with pytest.raises(AppleAuthError):
        await verify_apple_identity_token(hs_token, "apple-user-1")


@pytest.mark.asyncio
async def test_aud_as_list_accepted(signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())
    token = signing_key.sign(sub="apple-user-1", aud=[DEFAULT_AUDIENCE, "com.other.app"])

    identity = await verify_apple_identity_token(token, "apple-user-1")

    assert identity.aud == DEFAULT_AUDIENCE


@pytest.mark.asyncio
async def test_jwks_cache_avoids_redundant_fetches(monkeypatch, signing_key):
    """The 1-hour cache should mean a second call does not refetch."""
    apple_auth._reset_cache_for_tests()
    fetch_count = {"n": 0}

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            fetch_count["n"] += 1
            return _FakeResponse(signing_key.jwks())

    monkeypatch.setattr(apple_auth.httpx, "AsyncClient", _FakeClient)

    token = signing_key.sign(sub="apple-user-1")
    await verify_apple_identity_token(token, "apple-user-1")
    await verify_apple_identity_token(token, "apple-user-1")

    assert fetch_count["n"] == 1


# ---------------------------------------------------------------------------
# Endpoint integration tests
# ---------------------------------------------------------------------------

def _non_admin_request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id=""))


def _admin_request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(is_admin=True, contractor_id=""))


@pytest.mark.asyncio
async def test_create_contractor_rejects_missing_token():
    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(
            contractors_api.ContractorCreate(
                business_name="Acme",
                owner_name="Alice",
                apple_user_id="apple-user-1",
                # apple_identity_token intentionally omitted
            ),
            _non_admin_request(),
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_create_contractor_rejects_invalid_token(monkeypatch, signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())

    # A real-shaped token but with a sub that does not match.
    bad_token = signing_key.sign(sub="apple-user-attacker")

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(
            contractors_api.ContractorCreate(
                business_name="Acme",
                owner_name="Alice",
                apple_user_id="apple-user-victim",
                apple_identity_token=bad_token,
            ),
            _non_admin_request(),
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_create_contractor_accepts_valid_token(monkeypatch, signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())

    captured: dict = {}

    async def fake_create_contractor(data):
        captured.update(data)
        return "contractor-1"

    async def fake_update_contractor(contractor_id, updates):
        return True

    async def fake_get_by_phone(phone):
        return None

    monkeypatch.setattr(contractors_api, "create_contractor", fake_create_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)
    # The dedup helper is imported inside the function — patch via module path.
    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_owner_phone",
        fake_get_by_phone,
    )

    token = signing_key.sign(sub="apple-user-1")

    response = await contractors_api.api_create_contractor(
        contractors_api.ContractorCreate(
            business_name="Acme",
            owner_name="Alice",
            apple_user_id="apple-user-1",
            apple_identity_token=token,
            mode="personal",
        ),
        _non_admin_request(),
    )

    assert response["status"] == "ok"
    assert response["contractor_id"] == "contractor-1"
    # The raw identity token must never be persisted.
    assert "apple_identity_token" not in captured


@pytest.mark.asyncio
async def test_create_contractor_admin_bypasses_apple_check(monkeypatch):
    """Admin (global bearer) callers don't need an Apple identity token."""
    captured: dict = {}

    async def fake_create_contractor(data):
        captured.update(data)
        return "contractor-1"

    async def fake_update_contractor(contractor_id, updates):
        return True

    monkeypatch.setattr(contractors_api, "create_contractor", fake_create_contractor)
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update_contractor)

    response = await contractors_api.api_create_contractor(
        contractors_api.ContractorCreate(
            business_name="Acme",
            owner_name="Alice",
            mode="personal",
        ),
        _admin_request(),
    )

    assert response["status"] == "ok"


@pytest.mark.asyncio
async def test_lookup_by_apple_id_rejects_missing_token():
    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_lookup_by_apple_id(
            _non_admin_request(),
            apple_user_id="apple-user-1",
            apple_identity_token="",
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_lookup_by_apple_id_rejects_invalid_token(signing_key, patch_jwks):
    patch_jwks(signing_key.jwks())
    bad_token = signing_key.sign(sub="apple-user-attacker")

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_lookup_by_apple_id(
            _non_admin_request(),
            apple_user_id="apple-user-victim",
            apple_identity_token=bad_token,
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_lookup_by_apple_id_rejects_missing_user_id():
    """Empty apple_user_id should also produce a generic 401, not 400."""
    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_lookup_by_apple_id(
            _non_admin_request(),
            apple_user_id="",
            apple_identity_token="anything",
        )

    assert exc_info.value.status_code == 401
