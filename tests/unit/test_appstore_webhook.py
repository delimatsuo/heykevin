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


# ---------------------------------------------------------------------------
# Real signature verification (no mocking of _decode_notification_payload).
#
# Apple signs App Store Server Notifications V2 as JWS with alg=ES256. Per
# RFC 7518 §3.4 the JWS signature value is the raw 64-octet concatenation
# R || S, *not* the DER Dss-Sig-Value that `cryptography`'s verify() expects.
# Production rejected every Apple delivery for weeks with
# "JWS signature verification failed" because the raw bytes were handed
# straight to verify(). These tests pin the spec format end-to-end with a
# synthetic three-cert chain anchored to a fake root that is trusted via
# monkeypatch.
# ---------------------------------------------------------------------------

import base64
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

from app.config import settings


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _make_cert(
    common_name: str,
    issuer: x509.Name,
    issuer_key,
    subject_key,
    *,
    is_ca: bool,
    oids: tuple[str, ...] = (),
):
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(issuer)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
    )
    for oid in oids:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier(oid), b"\x05\x00"),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _fake_apple_chain(
    *,
    leaf_oids: tuple[str, ...] | None = None,
    intermediate_oids: tuple[str, ...] | None = None,
):
    """Return (leaf_private_key, [leaf, intermediate, root]) — the x5c order.

    By default the leaf and intermediate carry Apple's marker OIDs (as real
    App Store Server Notifications certs do). Pass overrides to build
    malformed chains for negative tests.
    """
    if leaf_oids is None:
        leaf_oids = (appstore_webhook.APPLE_LEAF_OID,)
    if intermediate_oids is None:
        intermediate_oids = (appstore_webhook.APPLE_INTERMEDIATE_OID,)
    root_key = ec.generate_private_key(ec.SECP256R1())
    root = _make_cert("Fake Apple Root CA", _name("Fake Apple Root CA"), root_key, root_key, is_ca=True)
    inter_key = ec.generate_private_key(ec.SECP256R1())
    inter = _make_cert(
        "Fake WWDR CA", root.subject, root_key, inter_key, is_ca=True, oids=intermediate_oids,
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = _make_cert(
        "Fake App Store Signer", inter.subject, inter_key, leaf_key, is_ca=False, oids=leaf_oids,
    )
    return leaf_key, [leaf, inter, root]


def _trust_root(monkeypatch, root: x509.Certificate) -> None:
    der = root.public_bytes(serialization.Encoding.DER)
    monkeypatch.setattr(
        appstore_webhook,
        "_TRUSTED_APPLE_ROOT_FINGERPRINTS",
        {hashlib.sha256(der).hexdigest()},
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_jws(payload: dict, leaf_key, chain, *, raw_signature: bool = True) -> str:
    header = {
        "alg": "ES256",
        "x5c": [
            base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode("ascii")
            for c in chain
        ],
    }
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(payload).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    der_sig = leaf_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    if raw_signature:
        r, s = decode_dss_signature(der_sig)
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")  # RFC 7518 §3.4
    else:
        sig = der_sig
    return f"{h}.{p}.{_b64url(sig)}"


def test_es256_raw_signature_per_rfc7518_is_accepted(monkeypatch):
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    # bundleId is required under the 2026-09-03 fail-closed rule; it was
    # previously optional here (the old check was `if bundle_id and ...`),
    # but this test's intent — that a correctly-formed raw ES256 signature
    # per RFC 7518 is accepted — is unaffected by adding it.
    payload = {
        "notificationType": "DID_RENEW",
        "data": {"bundleId": settings.appstore_bundle_id, "environment": "Production"},
    }

    decoded = appstore_webhook._decode_notification_payload(
        _sign_jws(payload, leaf_key, chain, raw_signature=True)
    )

    assert decoded == payload


def test_tampered_payload_fails_signature_check(monkeypatch):
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    token = _sign_jws({"notificationType": "DID_RENEW", "data": {}}, leaf_key, chain)
    h, _p, s = token.split(".")
    forged = f"{h}.{_b64url(json.dumps({'notificationType': 'REFUND', 'data': {}}).encode())}.{s}"

    with pytest.raises(ValueError, match="JWS signature verification failed"):
        appstore_webhook._decode_notification_payload(forged)


def test_der_encoded_signature_is_rejected_as_non_spec(monkeypatch):
    """A DER Dss-Sig-Value is not a valid JWS ES256 signature (wrong length)."""
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    token = _sign_jws({"notificationType": "DID_RENEW", "data": {}}, leaf_key, chain, raw_signature=False)

    with pytest.raises(ValueError, match="signature length"):
        appstore_webhook._decode_notification_payload(token)


def test_chain_not_anchored_to_trusted_root_is_rejected():
    leaf_key, chain = _fake_apple_chain()  # fake root deliberately NOT trusted
    token = _sign_jws({"notificationType": "DID_RENEW", "data": {}}, leaf_key, chain)

    with pytest.raises(ValueError, match="trusted Apple Root CA"):
        appstore_webhook._decode_notification_payload(token)


def test_bundle_id_mismatch_is_rejected_after_valid_signature(monkeypatch):
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    token = _sign_jws(
        {"notificationType": "DID_RENEW", "data": {"bundleId": "com.example.not-kevin"}},
        leaf_key,
        chain,
    )

    with pytest.raises(ValueError, match="Bundle ID mismatch"):
        appstore_webhook._decode_notification_payload(token)


# ---------------------------------------------------------------------------
# 2026-09-03 follow-up: exactly-3 certificate chain, Apple marker OIDs on
# leaf/intermediate, bundleId fail-closed, environment mismatch is log-only.
# ---------------------------------------------------------------------------


def test_two_cert_chain_is_rejected(monkeypatch):
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    leaf, _inter, root = chain
    token = _sign_jws({"notificationType": "DID_RENEW", "data": {}}, leaf_key, [leaf, root])

    with pytest.raises(ValueError, match="exactly 3"):
        appstore_webhook._decode_notification_payload(token)


def test_four_cert_chain_is_rejected(monkeypatch):
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    leaf, inter, root = chain
    token = _sign_jws({"notificationType": "DID_RENEW", "data": {}}, leaf_key, [leaf, inter, root, root])

    with pytest.raises(ValueError, match="exactly 3"):
        appstore_webhook._decode_notification_payload(token)


def test_leaf_without_marker_oid_is_rejected(monkeypatch):
    leaf_key, chain = _fake_apple_chain(leaf_oids=())
    _trust_root(monkeypatch, chain[-1])
    token = _sign_jws({"notificationType": "DID_RENEW", "data": {}}, leaf_key, chain)

    with pytest.raises(ValueError, match="Leaf certificate lacks"):
        appstore_webhook._decode_notification_payload(token)


def test_intermediate_without_marker_oid_is_rejected(monkeypatch):
    leaf_key, chain = _fake_apple_chain(intermediate_oids=())
    _trust_root(monkeypatch, chain[-1])
    token = _sign_jws({"notificationType": "DID_RENEW", "data": {}}, leaf_key, chain)

    with pytest.raises(ValueError, match="Intermediate certificate lacks"):
        appstore_webhook._decode_notification_payload(token)


def test_swapped_marker_oids_are_rejected(monkeypatch):
    leaf_key, chain = _fake_apple_chain(
        leaf_oids=(appstore_webhook.APPLE_INTERMEDIATE_OID,),
        intermediate_oids=(appstore_webhook.APPLE_LEAF_OID,),
    )
    _trust_root(monkeypatch, chain[-1])
    token = _sign_jws({"notificationType": "DID_RENEW", "data": {}}, leaf_key, chain)

    with pytest.raises(ValueError, match="Leaf certificate lacks"):
        appstore_webhook._decode_notification_payload(token)


def test_missing_bundle_id_is_rejected(monkeypatch):
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    token = _sign_jws({"notificationType": "TEST", "data": {}}, leaf_key, chain)

    with pytest.raises(ValueError, match="Missing bundleId"):
        appstore_webhook._decode_notification_payload(token)


def test_bundle_id_from_summary_section_is_accepted(monkeypatch):
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    payload = {
        "notificationType": "RENEWAL_EXTENSION",
        "summary": {"bundleId": settings.appstore_bundle_id},
    }
    token = _sign_jws(payload, leaf_key, chain)

    decoded = appstore_webhook._decode_notification_payload(token)

    assert decoded == payload


def test_bundle_id_from_external_purchase_token_section_is_accepted(monkeypatch):
    """Fix round 1 finding #1: externalPurchaseToken is one of Apple's four
    mutually exclusive bundleId sections and must not be rejected outright."""
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    payload = {
        "notificationType": "EXTERNAL_PURCHASE_TOKEN",
        "externalPurchaseToken": {"bundleId": settings.appstore_bundle_id},
    }
    token = _sign_jws(payload, leaf_key, chain)

    decoded = appstore_webhook._decode_notification_payload(token)

    assert decoded == payload


def test_bundle_id_from_app_data_section_is_accepted(monkeypatch):
    """Fix round 1 finding #1: appData is one of Apple's four mutually
    exclusive bundleId sections and must not be rejected outright."""
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    payload = {
        "notificationType": "DID_RENEW",
        "appData": {"bundleId": settings.appstore_bundle_id},
    }
    token = _sign_jws(payload, leaf_key, chain)

    decoded = appstore_webhook._decode_notification_payload(token)

    assert decoded == payload


def test_app_data_bundle_id_mismatch_is_rejected(monkeypatch):
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    token = _sign_jws(
        {"notificationType": "DID_RENEW", "appData": {"bundleId": "com.example.not-kevin"}},
        leaf_key,
        chain,
    )

    with pytest.raises(ValueError, match="Bundle ID mismatch"):
        appstore_webhook._decode_notification_payload(token)


def test_environment_mismatch_is_logged_not_rejected(monkeypatch, caplog):
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    monkeypatch.setattr(settings, "appstore_environment", "production")
    payload = {
        "notificationType": "DID_RENEW",
        "data": {"bundleId": settings.appstore_bundle_id, "environment": "Sandbox"},
    }
    token = _sign_jws(payload, leaf_key, chain)

    with caplog.at_level(logging.WARNING, logger="app.webhooks.appstore"):
        decoded = appstore_webhook._decode_notification_payload(token)

    assert decoded == payload
    assert "differs from configured" in caplog.text


def test_environment_case_only_difference_is_not_logged(monkeypatch, caplog):
    """Fix round 1 finding #3: pin case-insensitivity specifically — a
    same-value, different-case environment must not warn or reject."""
    leaf_key, chain = _fake_apple_chain()
    _trust_root(monkeypatch, chain[-1])
    monkeypatch.setattr(settings, "appstore_environment", "production")
    payload = {
        "notificationType": "DID_RENEW",
        "data": {"bundleId": settings.appstore_bundle_id, "environment": "Production"},
    }
    token = _sign_jws(payload, leaf_key, chain)

    with caplog.at_level(logging.WARNING, logger="app.webhooks.appstore"):
        decoded = appstore_webhook._decode_notification_payload(token)

    assert decoded == payload
    assert "differs from configured" not in caplog.text


def test_x5c_with_non_string_entry_is_rejected():
    header = {"alg": "ES256", "x5c": [12345]}
    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps({"notificationType": "DID_RENEW", "data": {}}).encode())
    token = f"{h}.{p}.{_b64url(b'sig')}"

    with pytest.raises(ValueError, match="Malformed x5c"):
        appstore_webhook._decode_notification_payload(token)
