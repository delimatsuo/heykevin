"""Pure crypto, envelope parsing, and token reader unit tests for integration tokens.

Crypto / Reader Isolation Contract:
This module verifies that integration_tokens.py is completely decoupled from database mutations,
audit logs, and API routes. It imports NEITHER integration_token_mutations NOR integration_lifecycle_audit
NOR app.api.integrations.
"""

from __future__ import annotations

import ast
import base64
import json
import os
from pathlib import Path

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

import pytest
from app.config import settings

from app.services.integration_tokens import (
    VALID_PROVIDERS,
    VALID_TOKEN_KINDS,
    IntegrationTokenConfigError,
    IntegrationTokenDecryptionError,
    IntegrationTokenEnvelopeError,
    compute_aad,
    decrypt_integration_token,
    encrypt_integration_token,
    has_usable_token,
    is_encryption_configured,
    is_envelope_map,
    parse_active_key_version,
    parse_keyring,
    resolve_usable_token,
    resolve_usable_token_pair,
    safe_decrypt_integration_token,
    validate_token_expires_at,
    validate_token_expires_in,
    validate_token_string,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_key_b64(byte_val: bytes = b"k") -> str:
    return base64.b64encode(byte_val * 32).decode("ascii")


def _setup_keyring(monkeypatch, active_version: int = 1):
    k1 = _make_key_b64(b"1")
    k2 = _make_key_b64(b"2")
    raw_keys = json.dumps({"1": k1, "2": k2})
    monkeypatch.setattr(settings, "integration_token_encryption_keys", raw_keys)
    monkeypatch.setattr(settings, "integration_token_active_key_version", str(active_version))


# ---------------------------------------------------------------------------
# 1. Architecture & Isolation Contract
# ---------------------------------------------------------------------------

def test_reader_source_imports_neither_mutations_nor_audit_nor_api():
    """Verify that neither integration_tokens.py nor test_integration_token_reader.py imports mutations, audit, or API."""
    forbidden_modules = {
        "app.services.integration_token_mutations",
        "app.db.integration_lifecycle_audit",
        "app.api.integrations",
        "integration_token_mutations",
        "integration_lifecycle_audit",
    }

    files_to_check = [
        Path("app/services/integration_tokens.py"),
        Path("tests/unit/test_integration_token_reader.py"),
    ]

    for file_path in files_to_check:
        assert file_path.exists(), f"File {file_path} must exist"
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in forbidden_modules:
                        assert not alias.name.startswith(forbidden), (
                            f"{file_path} forbidden import of {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    for forbidden in forbidden_modules:
                        assert not node.module.startswith(forbidden), (
                            f"{file_path} forbidden from-import of {node.module}"
                        )


# ---------------------------------------------------------------------------
# 2. Strict Keyring & Config Parsing
# ---------------------------------------------------------------------------

def test_parse_keyring_valid_single_and_multi_key():
    k1 = _make_key_b64(b"1")
    k2 = _make_key_b64(b"2")
    keyring = parse_keyring(json.dumps({"1": k1, "2": k2}))
    assert len(keyring) == 2
    assert keyring[1] == b"1" * 32
    assert keyring[2] == b"2" * 32


def test_parse_keyring_rejects_duplicate_json_keys():
    k1 = _make_key_b64(b"1")
    k2 = _make_key_b64(b"2")
    raw_dup = f'{{"1": "{k1}", "1": "{k2}"}}'
    with pytest.raises(IntegrationTokenConfigError, match="Duplicate key"):
        parse_keyring(raw_dup)


def test_parse_keyring_rejects_duplicate_key_material():
    k1 = _make_key_b64(b"1")
    raw = json.dumps({"1": k1, "2": k1})
    with pytest.raises(IntegrationTokenConfigError, match="Duplicate key material"):
        parse_keyring(raw)


def test_parse_keyring_rejects_invalid_inputs():
    with pytest.raises(IntegrationTokenConfigError):
        parse_keyring("")
    with pytest.raises(IntegrationTokenConfigError):
        parse_keyring("   ")
    with pytest.raises(IntegrationTokenConfigError):
        parse_keyring(123)  # type: ignore
    with pytest.raises(IntegrationTokenConfigError):
        parse_keyring("[]")
    with pytest.raises(IntegrationTokenConfigError):
        parse_keyring('{"01": "' + _make_key_b64(b"1") + '"}')  # leading zero
    with pytest.raises(IntegrationTokenConfigError):
        parse_keyring('{"0": "' + _make_key_b64(b"1") + '"}')  # 0 outside min 1
    with pytest.raises(IntegrationTokenConfigError):
        parse_keyring('{"1": "short"}')  # invalid length


def test_parse_active_key_version():
    assert parse_active_key_version(None) is None
    assert parse_active_key_version("") is None
    assert parse_active_key_version(1) == 1
    assert parse_active_key_version("1") == 1
    assert parse_active_key_version("42") == 42
    with pytest.raises(IntegrationTokenConfigError):
        parse_active_key_version(" ")
    with pytest.raises(IntegrationTokenConfigError):
        parse_active_key_version(" 1 ")
    with pytest.raises(IntegrationTokenConfigError):
        parse_active_key_version("1 ")
    with pytest.raises(IntegrationTokenConfigError):
        parse_active_key_version(" 1")
    with pytest.raises(IntegrationTokenConfigError):
        parse_active_key_version(True)
    with pytest.raises(IntegrationTokenConfigError):
        parse_active_key_version("01")
    with pytest.raises(IntegrationTokenConfigError):
        parse_active_key_version(0)
    with pytest.raises(IntegrationTokenConfigError):
        parse_active_key_version(-1)


# ---------------------------------------------------------------------------
# 3. Token Hygiene & Scalar Validation
# ---------------------------------------------------------------------------

def test_validate_token_string_hygiene():
    assert validate_token_string("exact_token_123") == "exact_token_123"
    assert validate_token_string(None, allow_none=True) is None

    with pytest.raises(IntegrationTokenEnvelopeError):
        validate_token_string(None, allow_none=False)
    with pytest.raises(IntegrationTokenEnvelopeError):
        validate_token_string("")
    with pytest.raises(IntegrationTokenEnvelopeError, match="leading or trailing whitespace"):
        validate_token_string(" token")
    with pytest.raises(IntegrationTokenEnvelopeError, match="leading or trailing whitespace"):
        validate_token_string("token ")
    with pytest.raises(IntegrationTokenEnvelopeError, match="control characters"):
        validate_token_string("tok\x00en")
    with pytest.raises(IntegrationTokenEnvelopeError, match="must be an exact str"):
        validate_token_string(123)
    with pytest.raises(IntegrationTokenEnvelopeError, match="must be an exact str"):
        validate_token_string(True)


def test_validate_token_expires_in_and_at():
    assert validate_token_expires_in(3600) == 3600.0
    assert validate_token_expires_in(3600.5) == 3600.5
    assert validate_token_expires_in(None) is None
    with pytest.raises(IntegrationTokenEnvelopeError):
        validate_token_expires_in(True)
    with pytest.raises(IntegrationTokenEnvelopeError):
        validate_token_expires_in(0)
    with pytest.raises(IntegrationTokenEnvelopeError):
        validate_token_expires_in(float("inf"))

    assert validate_token_expires_at(1700000000.0) == 1700000000.0
    assert validate_token_expires_at(None) is None
    with pytest.raises(IntegrationTokenEnvelopeError):
        validate_token_expires_at(True)
    with pytest.raises(IntegrationTokenEnvelopeError):
        validate_token_expires_at(float("inf"))


# ---------------------------------------------------------------------------
# 4. AES-256-GCM & Canonical AAD Round Trips
# ---------------------------------------------------------------------------

def test_compute_aad_canonical_json():
    aad = compute_aad(
        contractor_id="c-123",
        provider="jobber",
        token_kind="access",
        key_version=1,
    )
    parsed = json.loads(aad.decode("utf-8"))
    assert parsed == {
        "algorithm": "AES-256-GCM",
        "contractor_id": "c-123",
        "key_version": 1,
        "provider": "jobber",
        "schema_version": 1,
        "token_kind": "access",
    }
    assert list(parsed.keys()) == sorted(parsed.keys())


def test_compute_aad_fails_closed_on_invalid_contractor_id():
    bad_cids = ["  c1", "c1  ", "\tc1", "c1\n", "\u2003c1", "\u3000c1"]
    for bad in bad_cids:
        with pytest.raises(IntegrationTokenEnvelopeError, match="leading or trailing whitespace"):
            compute_aad(contractor_id=bad, provider="jobber", token_kind="access", key_version=1)

    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id="", provider="jobber", token_kind="access", key_version=1)
    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id=123, provider="jobber", token_kind="access", key_version=1)  # type: ignore
    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id=True, provider="jobber", token_kind="access", key_version=1)  # type: ignore


def test_encrypt_decrypt_round_trip(monkeypatch):
    _setup_keyring(monkeypatch, active_version=1)
    cid = "contractor-rt-1"
    token = "secret-access-token-xyz-123"

    for provider in VALID_PROVIDERS:
        for kind in VALID_TOKEN_KINDS:
            enc = encrypt_integration_token(
                token,
                contractor_id=cid,
                provider=provider,
                token_kind=kind,
            )
            assert is_envelope_map(enc)
            assert enc["schema_version"] == 1
            assert enc["key_version"] == 1
            assert enc["algorithm"] == "AES-256-GCM"

            dec = decrypt_integration_token(
                enc,
                contractor_id=cid,
                provider=provider,
                token_kind=kind,
            )
            assert dec == token


# ---------------------------------------------------------------------------
# 5. Adversarial Envelope & Tampering Defense
# ---------------------------------------------------------------------------

def test_tampered_ciphertext_fails_decryption(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-tamper"
    enc = encrypt_integration_token("secret", contractor_id=cid, provider="jobber", token_kind="access")

    # Corrupt ciphertext byte
    raw_ct = bytearray(base64.b64decode(enc["ciphertext"]))
    raw_ct[0] ^= 0xFF
    enc["ciphertext"] = base64.b64encode(raw_ct).decode("ascii")

    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(enc, contractor_id=cid, provider="jobber", token_kind="access")


def test_tampered_nonce_fails_decryption(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-tamper-nonce"
    enc = encrypt_integration_token("secret", contractor_id=cid, provider="jobber", token_kind="access")

    raw_nonce = bytearray(base64.b64decode(enc["nonce"]))
    raw_nonce[0] ^= 0xFF
    enc["nonce"] = base64.b64encode(raw_nonce).decode("ascii")

    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(enc, contractor_id=cid, provider="jobber", token_kind="access")


def test_altered_context_fails_decryption(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-context-1"
    enc = encrypt_integration_token("secret", contractor_id=cid, provider="jobber", token_kind="access")

    # Wrong contractor_id
    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(enc, contractor_id="c-context-2", provider="jobber", token_kind="access")

    # Wrong provider
    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(enc, contractor_id=cid, provider="google_calendar", token_kind="access")

    # Wrong token_kind
    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(enc, contractor_id=cid, provider="jobber", token_kind="refresh")


# ---------------------------------------------------------------------------
# 6. Legacy Plaintext & Safe Fallbacks
# ---------------------------------------------------------------------------

def test_legacy_plaintext_read_compatibility():
    legacy_token = "legacy-plaintext-token-abc"
    assert decrypt_integration_token(
        legacy_token,
        contractor_id="c1",
        provider="jobber",
        token_kind="access",
    ) == legacy_token


def test_safe_decrypt_returns_none_on_failure(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-safe-dec"
    enc = encrypt_integration_token("secret", contractor_id=cid, provider="jobber", token_kind="access")

    # Corrupt
    enc["ciphertext"] = "invalid-base64!"
    assert safe_decrypt_integration_token(enc, contractor_id=cid, provider="jobber", token_kind="access") is None


# ---------------------------------------------------------------------------
# 7. Key Rotation & Unknown Key
# ---------------------------------------------------------------------------

def test_key_rotation_multi_version_decryption(monkeypatch):
    k1 = _make_key_b64(b"1")
    k2 = _make_key_b64(b"2")
    raw_keys = json.dumps({"1": k1, "2": k2})
    from app.config import settings
    monkeypatch.setattr(settings, "integration_token_encryption_keys", raw_keys)
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")

    cid = "c-rot"
    enc_v1 = encrypt_integration_token("token-v1", contractor_id=cid, provider="jobber", token_kind="access")
    assert enc_v1["key_version"] == 1

    # Rotate active version to 2
    monkeypatch.setattr(settings, "integration_token_active_key_version", "2")
    enc_v2 = encrypt_integration_token("token-v2", contractor_id=cid, provider="jobber", token_kind="access")
    assert enc_v2["key_version"] == 2

    # Both decrypt cleanly
    assert decrypt_integration_token(enc_v1, contractor_id=cid, provider="jobber", token_kind="access") == "token-v1"
    assert decrypt_integration_token(enc_v2, contractor_id=cid, provider="jobber", token_kind="access") == "token-v2"


def test_unknown_key_version_fails_decryption(monkeypatch):
    _setup_keyring(monkeypatch, active_version=1)
    cid = "c-unknown-key"
    enc = encrypt_integration_token("secret", contractor_id=cid, provider="jobber", token_kind="access")
    enc["key_version"] = 99  # not in keyring

    with pytest.raises(IntegrationTokenDecryptionError, match="not present in configured keyring"):
        decrypt_integration_token(enc, contractor_id=cid, provider="jobber", token_kind="access")


# ---------------------------------------------------------------------------
# 8. Consumer Gates & resolve_usable_token
# ---------------------------------------------------------------------------

def test_resolve_usable_token_with_encrypted_and_legacy(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-consumer"
    enc_acc = encrypt_integration_token("enc-access", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("enc-refresh", contractor_id=cid, provider="jobber", token_kind="refresh")

    # 1. Encrypted envelope pair
    contractor_enc = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
    }
    assert resolve_usable_token(contractor_enc, "jobber", "access") == "enc-access"
    assert resolve_usable_token(contractor_enc, "jobber", "refresh") == "enc-refresh"
    assert resolve_usable_token_pair(contractor_enc, "jobber") == ("enc-access", "enc-refresh")
    assert has_usable_token(contractor_enc, "jobber", "access") is True

    # 2. Legacy plaintext string pair
    contractor_leg = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "legacy-access",
        "jobber_refresh_token": "legacy-refresh",
    }
    assert resolve_usable_token(contractor_leg, "jobber", "access") == "legacy-access"
    assert resolve_usable_token(contractor_leg, "jobber", "refresh") == "legacy-refresh"
    assert resolve_usable_token_pair(contractor_leg, "jobber") == ("legacy-access", "legacy-refresh")
    assert has_usable_token(contractor_leg, "jobber", "access") is True

    # 3. Disconnected provider returns None
    contractor_disc = dict(contractor_enc, jobber_connected=False)
    assert resolve_usable_token(contractor_disc, "jobber", "access") is None
    assert resolve_usable_token_pair(contractor_disc, "jobber") == (None, None)
    assert has_usable_token(contractor_disc, "jobber", "access") is False

    # 4. Explicit contractor_id overrides
    assert resolve_usable_token(contractor_enc, "jobber", "access", contractor_id=cid) == "enc-access"
    assert resolve_usable_token_pair(contractor_enc, "jobber", contractor_id=cid) == ("enc-access", "enc-refresh")
    assert resolve_usable_token(contractor_enc, "jobber", "access", contractor_id="wrong-cid") is None
    assert resolve_usable_token_pair(contractor_enc, "jobber", contractor_id="wrong-cid") == (None, None)
    assert resolve_usable_token(contractor_enc, "jobber", "access", contractor_id="") is None
    assert resolve_usable_token_pair(contractor_enc, "jobber", contractor_id="") == (None, None)


def test_representative_downstream_consumer_gates(monkeypatch):
    """Proves representative downstream consumer gate behaviors across all services and providers."""
    _setup_keyring(monkeypatch)
    cid = "c-representative-consumer"

    g_acc = encrypt_integration_token("google-tok", contractor_id=cid, provider="google_calendar", token_kind="access")
    g_ref = encrypt_integration_token("google-ref", contractor_id=cid, provider="google_calendar", token_kind="refresh")
    j_acc = encrypt_integration_token("jobber-acc", contractor_id=cid, provider="jobber", token_kind="access")
    j_ref = encrypt_integration_token("jobber-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    full_contractor = {
        "contractor_id": cid,
        "google_calendar_connected": True,
        "google_calendar_access_token": g_acc,
        "google_calendar_refresh_token": g_ref,
        "jobber_connected": True,
        "jobber_access_token": j_acc,
        "jobber_refresh_token": j_ref,
    }

    # Gemini pipeline / Voice pipeline / Receptionist context calendar gates
    assert has_usable_token(full_contractor, "google_calendar") is True
    assert resolve_usable_token(full_contractor, "google_calendar") == "google-tok"
    assert resolve_usable_token_pair(full_contractor, "google_calendar") == ("google-tok", "google-ref")

    # Post-call / Admin Jobber access & refresh token gates
    assert has_usable_token(full_contractor, "jobber", "access") is True
    assert resolve_usable_token(full_contractor, "jobber", "access") == "jobber-acc"
    assert has_usable_token(full_contractor, "jobber", "refresh") is True
    assert resolve_usable_token(full_contractor, "jobber", "refresh") == "jobber-ref"
    assert resolve_usable_token_pair(full_contractor, "jobber") == ("jobber-acc", "jobber-ref")

    # Disconnected Google Calendar
    no_cal = dict(full_contractor, google_calendar_connected=False)
    assert has_usable_token(no_cal, "google_calendar") is False
    assert resolve_usable_token(no_cal, "google_calendar") is None
    assert resolve_usable_token_pair(no_cal, "google_calendar") == (None, None)
    # Jobber still works
    assert has_usable_token(no_cal, "jobber", "access") is True

    # Encryption configuration probe
    assert is_encryption_configured() is True


def test_resolve_usable_token_pair_boundary_fail_closed(monkeypatch):
    """Proves that resolve_usable_token_pair fails closed with (None, None) for any unpaired or invalid credentials."""
    _setup_keyring(monkeypatch)
    cid = "c-pair-test"

    enc_acc = encrypt_integration_token("acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    # 1. Missing refresh token -> (None, None)
    missing_ref = {"contractor_id": cid, "jobber_connected": True, "jobber_access_token": enc_acc}
    assert resolve_usable_token_pair(missing_ref, "jobber") == (None, None)
    assert resolve_usable_token(missing_ref, "jobber", "access") is None

    # 2. Missing access token -> (None, None)
    missing_acc = {"contractor_id": cid, "jobber_connected": True, "jobber_refresh_token": enc_ref}
    assert resolve_usable_token_pair(missing_acc, "jobber") == (None, None)
    assert resolve_usable_token(missing_acc, "jobber", "refresh") is None

    # 3. Mixed types: str access + dict refresh -> (None, None)
    mixed_str_dict = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "plain-acc",
        "jobber_refresh_token": enc_ref,
    }
    assert resolve_usable_token_pair(mixed_str_dict, "jobber") == (None, None)
    assert resolve_usable_token(mixed_str_dict, "jobber", "access") is None

    # 4. Mixed types: dict access + str refresh -> (None, None)
    mixed_dict_str = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": "plain-ref",
    }
    assert resolve_usable_token_pair(mixed_dict_str, "jobber") == (None, None)
    assert resolve_usable_token(mixed_dict_str, "jobber", "refresh") is None

    # 5. Tampered envelope in refresh -> (None, None)
    tampered_ref = dict(enc_ref, ciphertext="tampered_data")
    tampered_doc = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": tampered_ref,
    }
    assert resolve_usable_token_pair(tampered_doc, "jobber") == (None, None)
    assert resolve_usable_token(tampered_doc, "jobber", "access") is None

    # 6. Legacy doc without connected boolean but with valid plaintext pair -> ("plain-acc", "plain-ref")
    legacy_doc = {
        "contractor_id": cid,
        "jobber_access_token": "plain-acc",
        "jobber_refresh_token": "plain-ref",
    }
    assert resolve_usable_token_pair(legacy_doc, "jobber") == ("plain-acc", "plain-ref")
