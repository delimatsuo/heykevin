"""Tests for the F-3 and F-4 fixes from the pre-launch paywall audit.

F-3: app/webhooks/appstore.py must reject JWS notifications whose chain does
not anchor to a known Apple Root CA. Previously the chain was only checked
for internal consistency; an attacker could mint a self-consistent chain
[attacker_leaf, attacker_intermediate, attacker_root] and forge any
notification payload (e.g. flip a victim's subscription_status to expired).

F-4: app/api/contractors.py POST /api/contractors must not return an existing
contractor's api_token + subscription_uuid when the caller's verified Apple
identity does not match the existing record's apple_user_id. Previously,
any valid Apple identity token + a victim's owner_phone yielded a fresh
api_token bound to the victim's contractor_id (a complete account takeover).
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# app.config requires these to import cleanly.
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import pytest
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from app.api import contractors as contractors_api
from app.db import contractors as contractors_db
from app.services import apple_auth
from app.webhooks import appstore as appstore_webhook


# ---------------------------------------------------------------------------
# F-3 helpers: synthesise a self-consistent attacker chain
# ---------------------------------------------------------------------------

def _make_self_consistent_chain():
    """Mint root, intermediate, and leaf EC P-256 certs that all chain
    together and validate as a self-consistent x5c chain — but use an
    attacker-controlled root, not Apple's. Returns (leaf_priv, x5c_b64_list).
    """
    now = datetime.now(timezone.utc)

    def _make_keypair():
        return ec.generate_private_key(ec.SECP256R1(), default_backend())

    def _build_cert(subject_cn, issuer_cn, subject_priv, issuer_priv, *, is_ca=False):
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
        issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)])
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(subject_priv.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(hours=1))
            .not_valid_after(now + timedelta(days=365))
        )
        if is_ca:
            builder = builder.add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
        return builder.sign(issuer_priv, hashes.SHA256(), default_backend())

    root_priv = _make_keypair()
    int_priv = _make_keypair()
    leaf_priv = _make_keypair()

    root_cert = _build_cert("AttackerRoot", "AttackerRoot", root_priv, root_priv, is_ca=True)
    int_cert = _build_cert("AttackerInt", "AttackerRoot", int_priv, root_priv, is_ca=True)
    leaf_cert = _build_cert("AttackerLeaf", "AttackerInt", leaf_priv, int_priv)

    x5c = [
        base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode()
        for c in (leaf_cert, int_cert, root_cert)
    ]
    return leaf_priv, x5c


def _build_forged_jws(payload: dict, leaf_priv, x5c: list[str]) -> str:
    """Build a JWS that's internally consistent (signed by the supplied leaf
    key). The point of these tests is whether the chain anchor check catches
    forgeries; the JWS itself is well-formed.
    """
    header = {"alg": "ES256", "x5c": x5c}

    def _b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    header_b64 = _b64u(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()

    der_sig = leaf_priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    # Convert ASN.1 DER signature to raw R||S as JWS expects.
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    sig_b64 = _b64u(raw_sig)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


# ---------------------------------------------------------------------------
# F-3: Apple Root CA pinning
# ---------------------------------------------------------------------------

def test_trusted_apple_root_fingerprints_loaded():
    """At module import time we should have at least one Apple root pinned.
    Empty set means the bundled cert files are missing — fail closed.
    """
    fingerprints = appstore_webhook._TRUSTED_APPLE_ROOT_FINGERPRINTS
    assert isinstance(fingerprints, set)
    assert len(fingerprints) >= 1
    # Apple Root CA G3 SHA-256 fingerprint, lowercase hex without colons.
    assert (
        "63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179"
        in fingerprints
    )


def test_attacker_chain_rejected_even_when_internally_valid():
    """Build an attacker root + intermediate + leaf, sign a JWS with the
    leaf, and confirm the webhook rejects it because the root is not Apple.
    """
    leaf_priv, x5c = _make_self_consistent_chain()
    forged = _build_forged_jws(
        {
            "data": {"bundleId": "com.kevin.callscreen"},
            "notificationType": "EXPIRED",
        },
        leaf_priv,
        x5c,
    )

    with pytest.raises(ValueError) as exc:
        appstore_webhook._decode_notification_payload(forged)
    assert "trusted Apple Root CA" in str(exc.value)


def test_empty_chain_rejected():
    """A JWS with no x5c header still gets rejected (defence in depth)."""
    header_b64 = base64.urlsafe_b64encode(b'{"alg":"ES256"}').decode().rstrip("=")
    payload_b64 = base64.urlsafe_b64encode(b'{}').decode().rstrip("=")
    forged = f"{header_b64}.{payload_b64}.AAAA"
    with pytest.raises(ValueError) as exc:
        appstore_webhook._decode_notification_payload(forged)
    assert "x5c" in str(exc.value)


# ---------------------------------------------------------------------------
# F-4 helpers: minimal Apple identity-token mocking
# ---------------------------------------------------------------------------

def _non_admin_request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id=""))


async def _fake_no_apple_id_match(apple_user_id: str):
    """No contractor is already bound to the caller's Apple ID.

    `api_create_contractor` does a call-time
    `from app.db.contractors import get_contractor_by_apple_user_id` dedupe
    lookup before it ever reaches the owner_phone-based hijack check these
    tests exercise. Left unmocked, that lookup reaches real Firestore and
    hangs under a network-denied sandbox (PR #229 fixed the identical
    pattern in tests/unit/test_twilio_provisioning.py). Every test below
    passes a nonblank apple_user_id that must NOT already match an existing
    record, so returning None here reproduces the intended scenario as well
    as unblocking it offline. Assert the value received is non-empty (like
    the neighbouring `stub_apple_identity` fake) so a future refactor that
    passes the wrong value into this dedupe lookup fails loudly instead of
    silently returning None for the wrong reason.
    """
    assert apple_user_id, "get_contractor_by_apple_user_id called with an empty apple_user_id"
    return None


@pytest.fixture
def stub_apple_identity(monkeypatch):
    """Bypass the real Apple JWKS round-trip; assert sub == expected_user_id
    and otherwise return a static AppleIdentity. The test passes the same
    apple_user_id as both `expected_user_id` and the body field so this
    mirrors the real success path.
    """

    async def _fake(identity_token: str, expected_user_id: str, **kwargs):
        if not expected_user_id:
            raise apple_auth.AppleAuthError("missing user id")
        return SimpleNamespace(
            sub=expected_user_id,
            aud="com.kevin.callscreen",
            iss="https://appleid.apple.com",
            email="",
            email_verified=False,
        )

    # contractors.py imports the symbol with `from ... import`, so the
    # module-local rebind is what gets called.
    monkeypatch.setattr(contractors_api, "verify_apple_identity_token", _fake)


# ---------------------------------------------------------------------------
# F-4: phone-lookup hijack rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phone_lookup_rejects_apple_id_mismatch(stub_apple_identity, monkeypatch):
    """Existing contractor has apple_user_id=A. Caller signs in as
    apple_user_id=B and presents the victim's owner_phone. The endpoint
    must return 409 Conflict, NOT issue a token to the attacker.
    """
    victim_contractor = {
        "contractor_id": "victim-1",
        "apple_user_id": "apple-user-victim",
        "owner_phone": "+14155551234",
        "subscription_uuid": "uuid-victim",
    }

    async def fake_get_by_phone(phone, *, country_code="US"):
        return victim_contractor

    async def fake_update(contractor_id, updates):
        # Should never be called when we reject.
        pytest.fail(
            f"update_contractor must not run on rejected hijack; called with {updates!r}"
        )
        return False

    async def fake_ensure_uuid(contractor_id, contractor):
        pytest.fail("ensure_subscription_uuid must not run on rejected hijack")
        return ""

    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_owner_phone", fake_get_by_phone
    )
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)
    monkeypatch.setattr(contractors_api, "ensure_subscription_uuid", fake_ensure_uuid)
    monkeypatch.setattr(contractors_db, "get_contractor_by_apple_user_id", _fake_no_apple_id_match)

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(
            contractors_api.ContractorCreate(
                business_name="Acme",
                owner_name="Alice",
                owner_phone=victim_contractor["owner_phone"],
                apple_user_id="apple-user-attacker",
                apple_identity_token="opaque-token-bypassed-by-stub",
            ),
            _non_admin_request(),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_phone_lookup_allows_apple_id_match(stub_apple_identity, monkeypatch):
    """Existing contractor has apple_user_id=X. Caller signs in as the same
    apple_user_id=X and presents the same owner_phone. This is the legitimate
    restore-on-new-device path — should return the existing contractor.
    """
    contractor = {
        "contractor_id": "owner-1",
        "apple_user_id": "apple-user-owner",
        "owner_phone": "+14155551234",
        "subscription_uuid": "uuid-owner",
    }

    async def fake_get_by_phone(phone, *, country_code="US"):
        return contractor

    captured_updates: list = []

    async def fake_update(contractor_id, updates):
        captured_updates.append((contractor_id, dict(updates)))
        return True

    async def fake_ensure_uuid(contractor_id, existing):
        return existing.get("subscription_uuid", "")

    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_owner_phone", fake_get_by_phone
    )
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)
    monkeypatch.setattr(contractors_api, "ensure_subscription_uuid", fake_ensure_uuid)
    monkeypatch.setattr(contractors_db, "get_contractor_by_apple_user_id", _fake_no_apple_id_match)

    response = await contractors_api.api_create_contractor(
        contractors_api.ContractorCreate(
            business_name="Acme",
            owner_name="Alice",
            owner_phone=contractor["owner_phone"],
            apple_user_id="apple-user-owner",
            apple_identity_token="opaque-token-bypassed-by-stub",
        ),
        _non_admin_request(),
    )

    assert response["status"] == "ok"
    assert response["contractor_id"] == "owner-1"
    assert response["existing"] is True
    assert response["subscription_uuid"] == "uuid-owner"
    # The matching-Apple-ID path should NOT rewrite apple_user_id; only the
    # api_token_hash rotation update should occur.
    assert all(
        "apple_user_id" not in updates for _, updates in captured_updates
    ), "apple_user_id must not be rewritten when it already matches"


@pytest.mark.asyncio
async def test_phone_lookup_rejects_legacy_record_with_blank_apple_id(
    stub_apple_identity, monkeypatch
):
    """Legacy contractor (created before Apple Sign-In was required) has no
    apple_user_id. The endpoint must fail closed: knowing the phone number
    does NOT grant account recovery/takeover without verified ownership.
    Must return 409, with zero updates and zero token issuance.
    """
    legacy = {
        "contractor_id": "legacy-1",
        "apple_user_id": "",
        "owner_phone": "+14155559999",
        "subscription_uuid": "uuid-legacy",
    }

    async def fake_get_by_phone(phone, *, country_code="US"):
        return legacy

    async def fail_update(contractor_id, updates):
        pytest.fail(f"update_contractor must not be called on legacy rejection: {updates}")

    async def fail_create(data):
        pytest.fail(f"create_contractor must not be called on legacy rejection: {data}")

    async def fail_ensure_uuid(contractor_id, existing):
        pytest.fail("ensure_subscription_uuid must not be called on legacy rejection")

    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_owner_phone", fake_get_by_phone
    )
    monkeypatch.setattr(contractors_api, "update_contractor", fail_update)
    monkeypatch.setattr(contractors_api, "create_contractor", fail_create)
    monkeypatch.setattr(contractors_api, "ensure_subscription_uuid", fail_ensure_uuid)
    monkeypatch.setattr(contractors_db, "get_contractor_by_apple_user_id", _fake_no_apple_id_match)

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(
            contractors_api.ContractorCreate(
                business_name="Acme",
                owner_name="Alice",
                owner_phone=legacy["owner_phone"],
                apple_user_id="apple-user-firstsignin",
                apple_identity_token="opaque-token-bypassed-by-stub",
            ),
            _non_admin_request(),
        )

    assert exc_info.value.status_code == 409
    assert "already exists" in exc_info.value.detail


@pytest.mark.asyncio
async def test_international_phone_hijack_rejection(stub_apple_identity, monkeypatch):
    """Victim has UK E.164 phone +442079460958 and apple_user_id=apple-user-victim.
    Attacker signs in with different Apple ID and national-format phone 020 7946 0958
    and country_code=GB. Must be rejected with 409 Conflict.
    """
    victim_contractor = {
        "contractor_id": "victim-uk-1",
        "apple_user_id": "apple-user-victim",
        "owner_phone": "+442079460958",
        "country_code": "GB",
        "subscription_uuid": "uuid-victim-uk",
    }

    seen_dedupe_args: dict = {}

    async def fake_get_by_phone(phone, *, country_code="US"):
        seen_dedupe_args["phone"] = phone
        seen_dedupe_args["country_code"] = country_code
        return victim_contractor

    async def fake_update(contractor_id, updates):
        pytest.fail("update_contractor must not run on rejected hijack")
        return False

    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_owner_phone", fake_get_by_phone
    )
    monkeypatch.setattr(contractors_api, "update_contractor", fake_update)
    monkeypatch.setattr(contractors_db, "get_contractor_by_apple_user_id", _fake_no_apple_id_match)

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(
            contractors_api.ContractorCreate(
                business_name="UK Services",
                owner_name="Victim",
                owner_phone="020 7946 0958",
                country_code="GB",
                apple_user_id="apple-user-attacker",
                apple_identity_token="opaque-token-bypassed-by-stub",
            ),
            _non_admin_request(),
        )

    assert exc_info.value.status_code == 409
    assert seen_dedupe_args["country_code"] == "GB"


@pytest.mark.asyncio
async def test_international_phone_rejects_legacy_record_with_blank_apple_id(
    stub_apple_identity, monkeypatch
):
    """Legacy UK record with no apple_user_id must fail closed when looked up
    by national-format phone 020 7946 0958. Zero updates, zero token issuance.
    """
    legacy = {
        "contractor_id": "legacy-uk-1",
        "apple_user_id": "",
        "owner_phone": "+442079460958",
        "country_code": "GB",
        "subscription_uuid": "uuid-legacy-uk",
    }

    async def fake_get_by_phone(phone, *, country_code="US"):
        return legacy

    async def fail_update(contractor_id, updates):
        pytest.fail("update_contractor must not run on legacy fail-closed rejection")

    async def fail_create(data):
        pytest.fail("create_contractor must not run on legacy fail-closed rejection")

    async def fail_ensure_uuid(contractor_id, existing):
        pytest.fail("ensure_subscription_uuid must not run on legacy fail-closed rejection")

    monkeypatch.setattr(
        "app.db.contractors.get_contractor_by_owner_phone", fake_get_by_phone
    )
    monkeypatch.setattr(contractors_api, "update_contractor", fail_update)
    monkeypatch.setattr(contractors_api, "create_contractor", fail_create)
    monkeypatch.setattr(contractors_api, "ensure_subscription_uuid", fail_ensure_uuid)
    monkeypatch.setattr(contractors_db, "get_contractor_by_apple_user_id", _fake_no_apple_id_match)

    with pytest.raises(HTTPException) as exc_info:
        await contractors_api.api_create_contractor(
            contractors_api.ContractorCreate(
                business_name="UK Services",
                owner_name="Alice",
                owner_phone="020 7946 0958",
                country_code="GB",
                apple_user_id="apple-user-firstsignin",
                apple_identity_token="opaque-token-bypassed-by-stub",
            ),
            _non_admin_request(),
        )

    assert exc_info.value.status_code == 409
    assert "already exists" in exc_info.value.detail
