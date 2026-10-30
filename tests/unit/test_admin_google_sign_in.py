"""Google sign-in for the admin page.

The page sends a Google ID token as its bearer token. It must verify with
Google, carry a verified email on ADMIN_EMAILS, and only reach the endpoints
the admin page uses. Off unless ADMIN_GOOGLE_CLIENT_ID and ADMIN_EMAILS are
both set; the global bearer token keeps working either way.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.config import settings  # noqa: E402
from app.middleware import admin_google  # noqa: E402
from app.middleware.auth import verify_api_token  # noqa: E402

GOOGLE_TOKEN = "header.payload.signature"
CLIENT_ID = "1234567890-abc.apps.googleusercontent.com"


@pytest.fixture
def google(monkeypatch):
    """Google sign-in configured; Google's verifier faked."""
    state = {"claims": {"email": "Owner@Example.com", "email_verified": True, "hd": "example.com",
                        "exp": time.time() + 3600},
             "calls": 0, "error": None}

    def fake_verify(token):
        state["calls"] += 1
        if state["error"]:
            raise state["error"]
        assert token == GOOGLE_TOKEN
        return dict(state["claims"])

    monkeypatch.setattr(settings, "admin_google_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "admin_emails", "owner@example.com, second@example.com")
    monkeypatch.setattr(settings, "api_bearer_token", "global-admin-token")
    monkeypatch.setattr(admin_google, "_verify_claims", fake_verify)
    monkeypatch.setattr(admin_google, "_verified", {})
    return state


def _request(path, token=None, method="GET"):
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "method": method, "path": path, "headers": headers, "query_string": b""})


# --- verification -----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_allowlisted_google_account_is_admitted(google):
    email = await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/overview")

    assert email == "owner@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("client_id,emails", [("", "owner@example.com"), (CLIENT_ID, ""), (CLIENT_ID, " , ")])
async def test_google_sign_in_is_off_until_both_settings_are_set(google, monkeypatch, client_id, emails):
    monkeypatch.setattr(settings, "admin_google_client_id", client_id)
    monkeypatch.setattr(settings, "admin_emails", emails)

    assert admin_google.google_sign_in_enabled() is False
    assert await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/overview") is None
    assert google["calls"] == 0


@pytest.mark.asyncio
async def test_an_account_not_on_the_list_is_refused(google):
    google["claims"]["email"] = "stranger@example.com"

    assert await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/overview") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [False, None, "true"])
async def test_an_unverified_email_is_refused(google, verified):
    google["claims"]["email_verified"] = verified

    assert await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/overview") is None


@pytest.mark.asyncio
async def test_a_consumer_google_account_on_a_non_gmail_address_is_refused(google):
    # Such an account proved the mailbox only once, when it was created.
    del google["claims"]["hd"]

    assert await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/overview") is None


@pytest.mark.asyncio
async def test_a_gmail_account_needs_no_workspace_domain(google, monkeypatch):
    del google["claims"]["hd"]
    google["claims"]["email"] = "owner@gmail.com"
    monkeypatch.setattr(settings, "admin_emails", "owner@gmail.com")

    assert await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/overview") == "owner@gmail.com"


@pytest.mark.asyncio
async def test_a_token_google_rejects_is_refused(google):
    google["error"] = ValueError("Token expired")

    assert await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/overview") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/contractors/c1", "/api/calls", "/api/adminx", "/admin"])
async def test_a_google_session_only_reaches_the_admin_page_endpoints(google, path):
    assert await admin_google.admin_email_for_token(GOOGLE_TOKEN, path) is None
    assert google["calls"] == 0


@pytest.mark.asyncio
async def test_the_jobber_toggle_on_the_admin_page_is_reachable(google):
    email = await admin_google.admin_email_for_token(
        GOOGLE_TOKEN, "/api/integrations/jobber/lead-capture"
    )

    assert email == "owner@example.com"


@pytest.mark.asyncio
async def test_a_token_that_is_not_a_jwt_is_never_sent_to_google(google):
    assert await admin_google.admin_email_for_token("kv_ct_abcd1234_secret", "/api/admin/overview") is None
    assert google["calls"] == 0


@pytest.mark.asyncio
async def test_a_verified_token_is_cached_until_it_expires(google):
    await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/overview")
    await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/calls")
    assert google["calls"] == 1

    key = next(iter(admin_google._verified))
    admin_google._verified[key] = ("owner@example.com", time.time() - 1)
    await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/overview")
    assert google["calls"] == 2


@pytest.mark.asyncio
async def test_removing_an_account_from_the_list_ends_its_cached_session(google, monkeypatch):
    assert await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/overview")

    monkeypatch.setattr(settings, "admin_emails", "second@example.com")

    assert await admin_google.admin_email_for_token(GOOGLE_TOKEN, "/api/admin/overview") is None


# --- the real verifier and Google's certificates -----------------------------


class _CertsResponse:
    def __init__(self, keys, cache_control):
        self._keys = keys
        self.headers = {"Cache-Control": cache_control}

    def raise_for_status(self):
        pass

    def json(self):
        return dict(self._keys)


class _CertsSession:
    def __init__(self, keys, cache_control="public, max-age=19000, must-revalidate"):
        self.keys, self.cache_control, self.gets = keys, cache_control, []

    def get(self, url, timeout=None):
        self.gets.append((url, timeout))
        return _CertsResponse(self.keys, self.cache_control)


@pytest.fixture
def verifier(monkeypatch):
    """The real _verify_claims, with Google's certificate endpoint and jwt.decode faked."""
    from google.auth import jwt

    state = {"session": _CertsSession({"kid-1": "cert-1"}), "kid": "kid-1",
             "claims": {"iss": "https://accounts.google.com", "email": "owner@example.com"}, "decoded": []}

    def fake_decode(token, certs=None, audience=None, clock_skew_in_seconds=0):
        state["decoded"].append({"token": token, "certs": dict(certs), "audience": audience,
                                 "skew": clock_skew_in_seconds})
        return dict(state["claims"])

    monkeypatch.setattr(jwt, "decode", fake_decode)
    monkeypatch.setattr(jwt, "decode_header", lambda _token: {"kid": state["kid"], "alg": "RS256"})
    monkeypatch.setattr(admin_google, "_session", state["session"])
    monkeypatch.setattr(admin_google, "_certs", {"keys": None, "expires": 0.0, "fetched": 0.0})
    monkeypatch.setattr(settings, "admin_google_client_id", f"  {CLIENT_ID} ")
    return state


def test_the_real_verifier_checks_the_audience_with_little_clock_skew(verifier):
    claims = admin_google._verify_claims(GOOGLE_TOKEN)

    assert claims["email"] == "owner@example.com"
    assert verifier["decoded"] == [{"token": GOOGLE_TOKEN, "certs": {"kid-1": "cert-1"},
                                    "audience": CLIENT_ID, "skew": 10}]


@pytest.mark.parametrize("issuer", ["https://evil.example.com", "", None])
def test_a_token_from_another_issuer_is_refused(verifier, issuer):
    verifier["claims"]["iss"] = issuer

    with pytest.raises(ValueError):
        admin_google._verify_claims(GOOGLE_TOKEN)


def test_googles_certificates_are_fetched_once_and_cached_for_their_max_age(verifier):
    for _ in range(5):
        admin_google._verify_claims(GOOGLE_TOKEN)

    assert verifier["session"].gets == [("https://www.googleapis.com/oauth2/v1/certs", 5)]
    assert admin_google._certs["expires"] == pytest.approx(time.time() + 19000, abs=5)


def test_stale_certificates_are_fetched_again(verifier):
    admin_google._verify_claims(GOOGLE_TOKEN)
    admin_google._certs["expires"] = time.time() - 1

    admin_google._verify_claims(GOOGLE_TOKEN)

    assert len(verifier["session"].gets) == 2


def test_certificates_without_a_max_age_last_an_hour(verifier):
    verifier["session"].cache_control = "no-transform"

    admin_google._verify_claims(GOOGLE_TOKEN)

    assert admin_google._certs["expires"] == pytest.approx(time.time() + 3600, abs=5)


def test_an_unknown_key_refetches_at_most_every_five_minutes(verifier):
    admin_google._verify_claims(GOOGLE_TOKEN)
    verifier["kid"] = "forged-kid"

    # Just fetched: a token naming an unknown key doesn't force another fetch.
    admin_google._verify_claims(GOOGLE_TOKEN)
    assert len(verifier["session"].gets) == 1

    # Five minutes on, one forced refresh picks up a rotated key.
    admin_google._certs["fetched"] = time.time() - 301
    verifier["session"].keys = {"kid-1": "cert-1", "forged-kid": "cert-2"}
    admin_google._verify_claims(GOOGLE_TOKEN)
    assert len(verifier["session"].gets) == 2
    assert verifier["decoded"][-1]["certs"] == {"kid-1": "cert-1", "forged-kid": "cert-2"}


def _rsa_signer_and_cert(kid):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timedelta, timezone
    from google.auth import crypt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    signer = crypt.RSASigner.from_string(private_pem, key_id=kid)
    return signer, cert.public_bytes(serialization.Encoding.PEM).decode()


def test_real_signatures_are_checked_end_to_end(monkeypatch):
    """No fakes in the crypto: a token signed by a listed key passes; tampering,
    an unlisted key, or another audience fails."""
    from google.auth import jwt

    signer, cert = _rsa_signer_and_cert("kid-1")
    other_signer, _other_cert = _rsa_signer_and_cert("kid-1")
    monkeypatch.setattr(admin_google, "_session", _CertsSession({"kid-1": cert}))
    monkeypatch.setattr(admin_google, "_certs", {"keys": None, "expires": 0.0, "fetched": 0.0})
    monkeypatch.setattr(settings, "admin_google_client_id", CLIENT_ID)
    now = int(time.time())
    payload = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "iat": now, "exp": now + 600,
               "email": "owner@example.com", "email_verified": True, "hd": "example.com"}

    good = jwt.encode(signer, payload).decode()
    assert admin_google._verify_claims(good)["email"] == "owner@example.com"

    header, body, signature = good.split(".")
    forged_body = jwt.encode(signer, {**payload, "email": "attacker@example.com"}).decode().split(".")[1]
    with pytest.raises(Exception):
        admin_google._verify_claims(f"{header}.{forged_body}.{signature}")

    with pytest.raises(Exception):
        admin_google._verify_claims(jwt.encode(other_signer, payload).decode())

    with pytest.raises(Exception):
        admin_google._verify_claims(jwt.encode(signer, {**payload, "aud": "another-client"}).decode())


# --- the middleware ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_middleware_admits_a_google_admin_and_names_them(google):
    request = _request("/api/admin/overview", GOOGLE_TOKEN)

    await verify_api_token(request)

    assert request.state.is_admin is True
    assert request.state.contractor_id == ""
    assert request.state.admin_actor == "google:owner@example.com"


@pytest.mark.asyncio
async def test_the_middleware_refuses_a_google_session_outside_the_admin_page(google, monkeypatch):
    async def no_contractor(_token_hash):
        return None

    monkeypatch.setattr("app.db.contractors.get_contractor_by_api_token", no_contractor)

    with pytest.raises(HTTPException) as exc_info:
        await verify_api_token(_request("/api/contractors/c1", GOOGLE_TOKEN))

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_the_global_token_still_works_and_is_not_named(google):
    request = _request("/api/admin/overview", "global-admin-token")

    await verify_api_token(request)

    assert request.state.is_admin is True
    assert not hasattr(request.state, "admin_actor")
    assert google["calls"] == 0


@pytest.mark.asyncio
async def test_the_sign_in_settings_are_public_but_nothing_else_is(google):
    await verify_api_token(_request("/api/admin/auth-config"))  # no token: allowed

    with pytest.raises(HTTPException) as exc_info:
        await verify_api_token(_request("/api/admin/auth-config", method="POST"))
    assert exc_info.value.status_code == 401

    with pytest.raises(HTTPException):
        await verify_api_token(_request("/api/admin/overview"))


# --- the page's settings and the audit trail --------------------------------


@pytest.mark.asyncio
async def test_the_page_is_told_the_client_id_only_when_sign_in_is_on(google, monkeypatch):
    from app.api import admin as admin_api

    assert await admin_api.admin_auth_config() == {"google_client_id": CLIENT_ID}

    monkeypatch.setattr(settings, "admin_emails", "")
    assert await admin_api.admin_auth_config() == {"google_client_id": None}


@pytest.mark.asyncio
async def test_the_settings_never_reveal_who_is_allowed(google):
    from app.api import admin as admin_api

    reply = await admin_api.admin_auth_config()

    assert "owner@example.com" not in str(reply)


@pytest.mark.asyncio
async def test_audit_events_name_the_google_admin(monkeypatch):
    from app.db import admin_audit

    written = {}

    class _Doc:
        def set(self, data):
            written.update(data)

    class _Collection:
        def document(self):
            return _Doc()

    class _Db:
        def collection(self, _name):
            return _Collection()

    request = SimpleNamespace(
        state=SimpleNamespace(is_admin=True, admin_actor="google:owner@example.com"),
        headers={"user-agent": "pytest"},
        client=SimpleNamespace(host="127.0.0.1"),
    )

    await admin_audit.write_admin_audit_event(
        request=request, action="set_test_account", target_type="contractor",
        target_id="c1", reason="internal account", db=_Db(),
    )

    assert written["actor_type"] == "google:owner@example.com"
