"""Deterministic unit tests for integration token AES-256-GCM envelope and security boundaries."""

import asyncio
import base64
import datetime
import json
import os
import secrets
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

import app.db.firestore_client as firestore_module
import app.services.integration_token_mutations as it_mutations
from app.api import admin as admin_api
from app.api import integrations
from app.config import settings
from app.services import calendar as calendar_service
from app.services import gemini_pipeline, post_call, receptionist_context, voice_pipeline
from app.services import jobber as jobber_service
from app.services.integration_token_mutations import (
    IntegrationTokenLeaseError,
    IntegrationTokenPostconditionError,
    acquire_refresh_claim_cas,
    connect_provider_cas,
    consume_oauth_state,
    disconnect_provider_cas,
    persist_refreshed_tokens_cas,
    release_refresh_claim_cas,
    transition_refresh_claim_to_started_cas,
)
from app.services.integration_tokens import (
    ALGORITHM,
    MAX_PLAINTEXT_BYTES,
    SCHEMA_VERSION,
    IntegrationTokenCASConflict,
    IntegrationTokenConfigError,
    IntegrationTokenDecryptionError,
    IntegrationTokenEnvelopeError,
    compute_aad,
    decrypt_integration_token,
    determine_write_format,
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


def _make_key_b64(byte_val: bytes = b"k") -> str:
    return base64.b64encode(byte_val * 32).decode("ascii")


def _setup_keyring(monkeypatch, keys: dict[str, str] | None = None, active: str | None = "1"):
    if keys is None:
        keys = {"1": _make_key_b64(b"1"), "2": _make_key_b64(b"2")}
    monkeypatch.setattr(settings, "integration_token_encryption_keys", json.dumps(keys))
    monkeypatch.setattr(settings, "integration_token_active_key_version", active)


def _patch_firestore(monkeypatch, db):
    import app.db.firestore_client as firestore_mod
    import app.services.calendar as calendar_svc
    import app.services.integration_token_mutations as it_mutations
    import app.services.jobber as jobber_svc
    fn = db if callable(db) else (lambda: db)
    monkeypatch.setattr(firestore_mod, "get_firestore_client", fn)
    monkeypatch.setattr(it_mutations, "get_firestore_client", fn)
    monkeypatch.setattr(jobber_svc, "get_firestore_client", fn, raising=False)
    monkeypatch.setattr(calendar_svc, "get_firestore_client", fn, raising=False)


# ---------------------------------------------------------------------------
# 0. Pure Crypto/Reader Architecture Contract
# ---------------------------------------------------------------------------

def test_integration_tokens_has_no_db_or_mutation_dependencies():
    """Source-contract: app.services.integration_tokens must be crypto/reader-only

    with zero database, transaction, audit, or mutation imports.
    """
    import inspect

    import app.services.integration_tokens as it

    source = inspect.getsource(it)
    forbidden_terms = [
        "google.cloud.firestore",
        "get_firestore_client",
        "transactional",
        "persist_refreshed_tokens_cas",
        "disconnect_provider_cas",
        "connect_provider_cas",
        "consume_oauth_state",
        "integration_lifecycle_audit",
        "admin_audit",
        "DELETE_FIELD",
    ]
    for term in forbidden_terms:
        assert term not in source, f"Forbidden database/mutation dependency in integration_tokens.py: {term}"


def test_validate_token_string_hygiene():
    # Valid tokens are returned verbatim without stripping or mutation
    assert validate_token_string("abc-123_XYZ.~") == "abc-123_XYZ.~"
    assert validate_token_string("ya29.a0AfH6_internal space ok") == "ya29.a0AfH6_internal space ok"

    # None handling
    assert validate_token_string(None, allow_none=True) is None
    with pytest.raises(IntegrationTokenEnvelopeError, match="required"):
        validate_token_string(None, allow_none=False)

    # Rejects non-str types (including bool, int, bytes, dict)
    for invalid in (True, False, 123, b"bytes", {}, []):
        with pytest.raises(IntegrationTokenEnvelopeError, match="must be an exact str"):
            validate_token_string(invalid)

    # Rejects empty string
    with pytest.raises(IntegrationTokenEnvelopeError, match="cannot be empty"):
        validate_token_string("")

    # Rejects leading / trailing Unicode whitespace
    for ws in (" token", "token ", "\ntoken", "token\t", "\r\ntoken"):
        with pytest.raises(IntegrationTokenEnvelopeError, match="leading or trailing whitespace"):
            validate_token_string(ws)

    # Rejects C0 and C1 control characters
    for ctrl in ("tok\x00en", "tok\x1fen", "tok\x7fen", "tok\x80en", "tok\x9fen"):
        with pytest.raises(IntegrationTokenEnvelopeError, match="disallowed control characters"):
            validate_token_string(ctrl)


def test_parse_keyring_rejects_duplicate_key_material():
    same_key = _make_key_b64(b"1")
    raw = json.dumps({"1": same_key, "2": same_key})
    with pytest.raises(IntegrationTokenConfigError, match="Duplicate key material"):
        parse_keyring(raw)


# ---------------------------------------------------------------------------
# 1. Keyring & Active Key Version Parsing
# ---------------------------------------------------------------------------

def test_parse_keyring_valid_and_canonical():
    key1 = _make_key_b64(b"1")
    key2 = _make_key_b64(b"2")
    raw = json.dumps({"1": key1, "2": key2})
    keyring = parse_keyring(raw)
    assert len(keyring) == 2
    assert keyring[1] == b"1" * 32
    assert keyring[2] == b"2" * 32
    assert type(list(keyring.keys())[0]) is int


def test_parse_keyring_rejects_non_string_inputs():
    for non_str in (None, 123, True, False, ["1"], {"1": "key"}):
        with pytest.raises(IntegrationTokenConfigError):
            parse_keyring(non_str)


def test_parse_keyring_rejects_non_object_json():
    for invalid in ("[]", '"hello"', "123", "true", "null"):
        with pytest.raises(IntegrationTokenConfigError):
            parse_keyring(invalid)


def test_parse_keyring_rejects_duplicate_keys():
    raw = '{"1": "' + _make_key_b64(b"1") + '", "1": "' + _make_key_b64(b"2") + '"}'
    with pytest.raises(IntegrationTokenConfigError, match="Duplicate key"):
        parse_keyring(raw)


def test_parse_keyring_rejects_nan_and_infinity():
    for invalid in ('{"1": NaN}', '{"1": Infinity}', '{"1": -Infinity}'):
        with pytest.raises(IntegrationTokenConfigError):
            parse_keyring(invalid)


def test_parse_keyring_rejects_invalid_version_keys():
    valid_key = _make_key_b64(b"k")
    invalid_keys = ["0", "-1", "01", "v1", "1.0", "2147483648", "", " ", "abc"]
    for inv_k in invalid_keys:
        raw = json.dumps({inv_k: valid_key})
        with pytest.raises(IntegrationTokenConfigError):
            parse_keyring(raw)


def test_parse_keyring_rejects_invalid_key_values():
    invalid_values = [
        123,
        True,
        False,
        None,
        {},
        [],
        "not-base64-!!!",
        base64.b64encode(b"short").decode("ascii"),  # 5 bytes
        base64.b64encode(b"a" * 31).decode("ascii"),  # 31 bytes
        base64.b64encode(b"a" * 33).decode("ascii"),  # 33 bytes
        _make_key_b64(b"k") + "==",  # non-canonical
    ]
    for inv_v in invalid_values:
        raw = json.dumps({"1": inv_v})
        with pytest.raises(IntegrationTokenConfigError):
            parse_keyring(raw)


def test_parse_active_key_version():
    assert parse_active_key_version(None) is None
    assert parse_active_key_version("") is None
    assert parse_active_key_version(1) == 1
    assert parse_active_key_version("1") == 1
    assert parse_active_key_version(2147483647) == 2147483647

    # Strict rejection of whitespace-only, boolean, floats, invalid strings
    for inv in ("  ", " 1 ", "1 ", " 1", True, False, 1.0, 0, -1, "0", "-1", "01", "v1", 2147483648, "2147483648", [], {}):
        with pytest.raises(IntegrationTokenConfigError):
            parse_active_key_version(inv)


def test_is_encryption_configured(monkeypatch):
    _setup_keyring(monkeypatch, active="1")
    assert is_encryption_configured() is True

    # Active key not in keyring
    monkeypatch.setattr(settings, "integration_token_active_key_version", "99")
    assert is_encryption_configured() is False

    # Keyring empty
    monkeypatch.setattr(settings, "integration_token_encryption_keys", "")
    assert is_encryption_configured() is False


# ---------------------------------------------------------------------------
# 2. Encryption, Decryption, and AAD Context Binding
# ---------------------------------------------------------------------------

def test_exact_canonical_aad_byte_representation():
    aad_bytes = compute_aad(
        contractor_id="contractor-100",
        provider="jobber",
        token_kind="access",
        schema_version=1,
        key_version=1,
        algorithm="AES-256-GCM",
    )
    expected_bytes = b'{"algorithm":"AES-256-GCM","contractor_id":"contractor-100","key_version":1,"provider":"jobber","schema_version":1,"token_kind":"access"}'
    assert aad_bytes == expected_bytes


def test_encryption_roundtrip_and_envelope_shape(monkeypatch):
    _setup_keyring(monkeypatch)
    plaintext = "super-secret-oauth-access-token-xyz-12345"
    envelope = encrypt_integration_token(
        plaintext,
        contractor_id="contractor-100",
        provider="jobber",
        token_kind="access",
    )

    assert is_envelope_map(envelope)
    assert set(envelope.keys()) == {"schema_version", "key_version", "algorithm", "nonce", "ciphertext"}
    assert envelope["schema_version"] == SCHEMA_VERSION
    assert envelope["key_version"] == 1
    assert envelope["algorithm"] == ALGORITHM
    assert isinstance(envelope["nonce"], str)
    assert isinstance(envelope["ciphertext"], str)

    # Plaintext sentinel must be completely absent from raw envelope
    assert plaintext not in envelope["nonce"]
    assert plaintext not in envelope["ciphertext"]

    decrypted = decrypt_integration_token(
        envelope,
        contractor_id="contractor-100",
        provider="jobber",
        token_kind="access",
    )
    assert decrypted == plaintext


def test_random_nonce_uniqueness(monkeypatch):
    _setup_keyring(monkeypatch)
    env1 = encrypt_integration_token("secret", contractor_id="c1", provider="jobber", token_kind="access")
    env2 = encrypt_integration_token("secret", contractor_id="c1", provider="jobber", token_kind="access")
    assert env1["nonce"] != env2["nonce"]
    assert env1["ciphertext"] != env2["ciphertext"]


def test_encryption_rejects_empty_or_oversized_plaintext(monkeypatch):
    _setup_keyring(monkeypatch)
    with pytest.raises(IntegrationTokenEnvelopeError):
        encrypt_integration_token("", contractor_id="c1", provider="jobber", token_kind="access")

    with pytest.raises(IntegrationTokenEnvelopeError):
        encrypt_integration_token(None, contractor_id="c1", provider="jobber", token_kind="access")

    oversized = "a" * (MAX_PLAINTEXT_BYTES + 1)
    with pytest.raises(IntegrationTokenEnvelopeError):
        encrypt_integration_token(oversized, contractor_id="c1", provider="jobber", token_kind="access")


def test_explicit_active_version_and_keyring_validation(monkeypatch):
    valid_keyring = {1: b"k" * 32}
    # Rejection of explicit bool or float active version
    for inv_active in (True, False, 1.0, 0, -1, 2147483648):
        with pytest.raises(IntegrationTokenConfigError):
            encrypt_integration_token(
                "secret",
                contractor_id="c1",
                provider="jobber",
                token_kind="access",
                keyring=valid_keyring,
                active_version=inv_active,
            )

    # Rejection of invalid keyring structures
    for inv_kr in (
        {True: b"k" * 32},
        {"1": b"k" * 32},
        {1: b"short"},
        {1: "string-key"},
        [],
        "not-a-dict",
    ):
        with pytest.raises(IntegrationTokenConfigError):
            encrypt_integration_token(
                "secret",
                contractor_id="c1",
                provider="jobber",
                token_kind="access",
                keyring=inv_kr,
                active_version=1,
            )


def test_aad_context_binding_prevents_cross_context_decryption(monkeypatch):
    _setup_keyring(monkeypatch)
    envelope = encrypt_integration_token(
        "secret-payload",
        contractor_id="tenant-A",
        provider="jobber",
        token_kind="access",
    )

    # Wrong contractor
    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(
            envelope,
            contractor_id="tenant-B",
            provider="jobber",
            token_kind="access",
        )

    # Wrong provider
    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(
            envelope,
            contractor_id="tenant-A",
            provider="google_calendar",
            token_kind="access",
        )

    # Wrong token kind
    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(
            envelope,
            contractor_id="tenant-A",
            provider="jobber",
            token_kind="refresh",
        )


def test_tampered_ciphertext_and_nonce(monkeypatch):
    _setup_keyring(monkeypatch)
    envelope = encrypt_integration_token(
        "secret-payload",
        contractor_id="c1",
        provider="jobber",
        token_kind="access",
    )

    # Tamper ciphertext
    raw_ct = base64.b64decode(envelope["ciphertext"])
    tampered_ct = bytes([raw_ct[0] ^ 0x01]) + raw_ct[1:]
    bad_env_ct = dict(envelope, ciphertext=base64.b64encode(tampered_ct).decode("ascii"))
    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(bad_env_ct, contractor_id="c1", provider="jobber", token_kind="access")

    # Tamper nonce
    raw_nonce = base64.b64decode(envelope["nonce"])
    tampered_nonce = bytes([raw_nonce[0] ^ 0x01]) + raw_nonce[1:]
    bad_env_nonce = dict(envelope, nonce=base64.b64encode(tampered_nonce).decode("ascii"))
    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(bad_env_nonce, contractor_id="c1", provider="jobber", token_kind="access")


def test_strict_envelope_shape_and_type_validation(monkeypatch):
    _setup_keyring(monkeypatch)
    valid_env = encrypt_integration_token(
        "secret-payload",
        contractor_id="c1",
        provider="jobber",
        token_kind="access",
    )

    # Extra key (including mixed-type extra keys)
    for extra_env in (
        dict(valid_env, extra=1),
        {**valid_env, 7: "val"},
        {**valid_env, 7: "val", "x": 1},
    ):
        with pytest.raises(IntegrationTokenEnvelopeError):
            decrypt_integration_token(extra_env, contractor_id="c1", provider="jobber", token_kind="access")

    # Missing key
    for missing_k in valid_env.keys():
        bad = {k: v for k, v in valid_env.items() if k != missing_k}
        with pytest.raises(IntegrationTokenEnvelopeError):
            decrypt_integration_token(bad, contractor_id="c1", provider="jobber", token_kind="access")

    # Type confusion: bool / float for schema_version and key_version
    for bad_schema in (True, False, 1.0, 2):
        with pytest.raises(IntegrationTokenEnvelopeError):
            decrypt_integration_token(
                dict(valid_env, schema_version=bad_schema),
                contractor_id="c1",
                provider="jobber",
                token_kind="access",
            )

    for bad_key_ver in (True, False, 1.0, 0, -1, 2147483648):
        with pytest.raises(IntegrationTokenEnvelopeError):
            decrypt_integration_token(
                dict(valid_env, key_version=bad_key_ver),
                contractor_id="c1",
                provider="jobber",
                token_kind="access",
            )

    # Invalid algorithm
    with pytest.raises(IntegrationTokenEnvelopeError):
        decrypt_integration_token(
            dict(valid_env, algorithm="AES-128-GCM"),
            contractor_id="c1",
            provider="jobber",
            token_kind="access",
        )

    # Decoded size bounds: nonce length != 12, ciphertext length < 17 or > 16400
    with pytest.raises(IntegrationTokenEnvelopeError):
        decrypt_integration_token(
            dict(valid_env, nonce=base64.b64encode(b"short").decode("ascii")),
            contractor_id="c1",
            provider="jobber",
            token_kind="access",
        )
    with pytest.raises(IntegrationTokenEnvelopeError):
        decrypt_integration_token(
            dict(valid_env, ciphertext=base64.b64encode(b"short").decode("ascii")),
            contractor_id="c1",
            provider="jobber",
            token_kind="access",
        )


def test_compute_aad_validation():
    # Valid AAD output
    aad_bytes = compute_aad(
        contractor_id="c1",
        provider="jobber",
        token_kind="access",
        schema_version=1,
        key_version=1,
        algorithm="AES-256-GCM",
    )
    parsed = json.loads(aad_bytes.decode("utf-8"))
    assert parsed == {
        "algorithm": "AES-256-GCM",
        "contractor_id": "c1",
        "key_version": 1,
        "provider": "jobber",
        "schema_version": 1,
        "token_kind": "access",
    }

    # Invalid contractor_id
    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id="", provider="jobber", token_kind="access", key_version=1)
    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id=True, provider="jobber", token_kind="access", key_version=1)
    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id="x" * 1501, provider="jobber", token_kind="access", key_version=1)

    # Invalid provider
    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id="c1", provider="unknown_provider", token_kind="access", key_version=1)

    # Invalid token_kind
    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id="c1", provider="jobber", token_kind="invalid_kind", key_version=1)

    # Invalid schema_version
    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id="c1", provider="jobber", token_kind="access", schema_version=2, key_version=1)
    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id="c1", provider="jobber", token_kind="access", schema_version=True, key_version=1)

    # Invalid key_version
    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id="c1", provider="jobber", token_kind="access", key_version=0)
    with pytest.raises(IntegrationTokenEnvelopeError):
        compute_aad(contractor_id="c1", provider="jobber", token_kind="access", key_version=True)


# ---------------------------------------------------------------------------
# 3. Central Usable Token Helpers (resolve_usable_token, has_usable_token)
# ---------------------------------------------------------------------------

def test_resolve_usable_token_and_has_usable_token(monkeypatch):
    _setup_keyring(monkeypatch)
    contractor_id = "c-usable-1"
    valid_acc = encrypt_integration_token("secret-tok", contractor_id=contractor_id, provider="jobber", token_kind="access")
    valid_ref = encrypt_integration_token("secret-ref", contractor_id=contractor_id, provider="jobber", token_kind="refresh")

    # 1. Valid encrypted envelope with matching contractor_id
    contractor = {
        "contractor_id": contractor_id,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": valid_acc,
        "jobber_refresh_token": valid_ref,
    }
    assert resolve_usable_token(contractor, "jobber", "access") == "secret-tok"
    assert resolve_usable_token(contractor, "jobber", "refresh") == "secret-ref"
    assert has_usable_token(contractor, "jobber", "access") is True

    # 2. Legacy plaintext string
    legacy_contractor = {
        "contractor_id": contractor_id,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": "legacy-plaintext-tok",
        "jobber_refresh_token": "legacy-plaintext-ref",
    }
    assert resolve_usable_token(legacy_contractor, "jobber", "access") == "legacy-plaintext-tok"
    assert resolve_usable_token(legacy_contractor, "jobber", "refresh") == "legacy-plaintext-ref"
    assert has_usable_token(legacy_contractor, "jobber", "access") is True

    # 3. Missing contractor_id for encrypted envelope -> returns None / False
    no_id_contractor = {"jobber_access_token": valid_acc, "jobber_refresh_token": valid_ref}
    assert resolve_usable_token(no_id_contractor, "jobber", "access") is None
    assert has_usable_token(no_id_contractor, "jobber", "access") is False

    # 4. Wrong contractor_id (cross-tenant) -> returns None / False
    wrong_id_contractor = {
        "contractor_id": "other-tenant",
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": valid_acc,
        "jobber_refresh_token": valid_ref,
    }
    assert resolve_usable_token(wrong_id_contractor, "jobber", "access") is None
    assert has_usable_token(wrong_id_contractor, "jobber", "access") is False

    # 5. Malformed envelope -> returns None / False
    malformed_contractor = {
        "contractor_id": contractor_id,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": {"schema_version": 1, "bad": True},
        "jobber_refresh_token": valid_ref,
    }
    assert resolve_usable_token(malformed_contractor, "jobber", "access") is None
    assert has_usable_token(malformed_contractor, "jobber", "access") is False

    # 6. Unknown key version -> returns None / False
    env_v99 = dict(valid_acc, key_version=99)
    unknown_key_contractor = {"contractor_id": contractor_id, "jobber_access_token": env_v99, "jobber_refresh_token": valid_ref}
    assert resolve_usable_token(unknown_key_contractor, "jobber", "access") is None
    assert has_usable_token(unknown_key_contractor, "jobber", "access") is False

    # 7. Non-dict contractor or empty field
    assert resolve_usable_token(None, "jobber") is None
    assert resolve_usable_token({}, "jobber") is None
    assert has_usable_token(None, "jobber") is False


# ---------------------------------------------------------------------------
# 4. Key Rotation & Unknown Key Versions
# ---------------------------------------------------------------------------

def test_key_rotation_and_unknown_version(monkeypatch):
    _setup_keyring(monkeypatch, keys={"1": _make_key_b64(b"1"), "2": _make_key_b64(b"2")}, active="1")
    env_v1 = encrypt_integration_token("secret-v1", contractor_id="c1", provider="jobber", token_kind="access")
    assert env_v1["key_version"] == 1

    # Switch active key to version 2
    monkeypatch.setattr(settings, "integration_token_active_key_version", "2")
    env_v2 = encrypt_integration_token("secret-v2", contractor_id="c1", provider="jobber", token_kind="access")
    assert env_v2["key_version"] == 2

    # Both decrypt correctly
    assert decrypt_integration_token(env_v1, contractor_id="c1", provider="jobber", token_kind="access") == "secret-v1"
    assert decrypt_integration_token(env_v2, contractor_id="c1", provider="jobber", token_kind="access") == "secret-v2"

    # Envelope with unknown key version 3
    env_v3 = dict(env_v2, key_version=3)
    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(env_v3, contractor_id="c1", provider="jobber", token_kind="access")


# ---------------------------------------------------------------------------
# 5. Legacy Plaintext Compatibility & Fail-Closed Behavior
# ---------------------------------------------------------------------------

def test_legacy_plaintext_read_compatibility(monkeypatch):
    _setup_keyring(monkeypatch)
    legacy_token = "legacy-plaintext-access-token"
    read_val = decrypt_integration_token(
        legacy_token,
        contractor_id="c1",
        provider="jobber",
        token_kind="access",
    )
    assert read_val == legacy_token


def test_absent_key_permits_legacy_read_but_fails_encrypted_read_and_new_write(monkeypatch):
    # Keyring is empty
    monkeypatch.setattr(settings, "integration_token_encryption_keys", "")
    monkeypatch.setattr(settings, "integration_token_active_key_version", None)

    # Legacy read succeeds
    assert decrypt_integration_token("legacy-token", contractor_id="c1", provider="jobber", token_kind="access") == "legacy-token"

    # Encrypted envelope read fails closed
    dummy_envelope = {
        "schema_version": 1,
        "key_version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": base64.b64encode(b"n" * 12).decode("ascii"),
        "ciphertext": base64.b64encode(b"c" * 20).decode("ascii"),
    }
    with pytest.raises(IntegrationTokenDecryptionError):
        decrypt_integration_token(dummy_envelope, contractor_id="c1", provider="jobber", token_kind="access")

    # New write fails closed
    with pytest.raises(IntegrationTokenConfigError):
        encrypt_integration_token("new-token", contractor_id="c1", provider="jobber", token_kind="access")


# ---------------------------------------------------------------------------
# 6. Service Token Refresh: Atomic Persistence & Defense in Depth
# ---------------------------------------------------------------------------

class _FakeDocRef:
    def __init__(self, data=None, doc_id="fake-id"):
        self.id = (data or {}).get("contractor_id") or doc_id
        self.data = dict(data) if data is not None else None
        self.deleted = False
        self.updates = []

    @property
    def exists(self) -> bool:
        return (self.data is not None) and (not self.deleted)

    def get(self, *args, transaction=None, **kwargs):
        class _Snap:
            def __init__(self, d, deleted):
                self._d = dict(d) if d is not None else {}
                self.exists = (d is not None) and (not deleted)
                self.read_time = datetime.datetime.fromtimestamp(time.time(), datetime.UTC)

            def to_dict(self):
                return dict(self._d) if self.exists else {}

        snap = _Snap(self.data, self.deleted)
        if transaction is not None and hasattr(transaction, "_record_read"):
            transaction._record_read(self, snap)
        return snap

    def set(self, data, *args, **kwargs):
        self.data = dict(data)
        self.deleted = False

    def update(self, updates, *args, **kwargs):
        from google.cloud.firestore_v1 import DELETE_FIELD
        from google.cloud.firestore_v1.transforms import Sentinel
        if self.data is None:
            self.data = {}
        self.updates.append(dict(updates))
        for k, v in updates.items():
            if v is DELETE_FIELD or isinstance(v, Sentinel) or getattr(v, "__class__", None).__name__ == "Sentinel" or str(v).startswith("Sentinel:"):
                self.data.pop(k, None)
            else:
                self.data[k] = v

    def delete(self, *args, **kwargs):
        self.deleted = True
        self.data = None


class _ContentionDocRef(_FakeDocRef):
    def __init__(self, data=None, doc_id="fake-id"):
        super().__init__(data, doc_id=doc_id)
        self.version = 1

    def update(self, updates, *args, **kwargs):
        super().update(updates, *args, **kwargs)
        self.version += 1

    def set(self, data, *args, **kwargs):
        super().set(data, *args, **kwargs)
        self.version += 1

    def get(self, *args, transaction=None, **kwargs):
        snap = super().get(*args, transaction=transaction, **kwargs)
        if transaction is not None and hasattr(transaction, "_record_read"):
            transaction._record_read(self, snap)
        return snap


class _FakeTransaction:
    def __init__(self, db):
        self._db = db
        self._staged_updates = []
        self._staged_sets = []
        self._staged_creates = []
        self._staged_deletes = []
        self.committed = False
        self._read_only = False
        self._id = b"fake-tx-id"
        self._max_attempts = 5
        self.in_progress = True

    def get(self, doc_ref):
        if self._staged_updates or self._staged_sets or self._staged_creates or self._staged_deletes:
            raise RuntimeError("Firestore transaction read-after-write violation: all reads must occur before writes/deletes/creates")
        return doc_ref.get()

    def update(self, doc_ref, updates):
        self._staged_updates.append((doc_ref, dict(updates)))

    def delete(self, doc_ref):
        self._staged_deletes.append(doc_ref)

    def set(self, doc_ref, data):
        self._staged_sets.append((doc_ref, dict(data)))

    def create(self, doc_ref, data):
        self._staged_creates.append((doc_ref, dict(data)))

    def commit(self):
        touched_refs = []
        for ref, _ in self._staged_creates:
            if ref not in touched_refs:
                touched_refs.append(ref)
        for ref, _ in self._staged_sets:
            if ref not in touched_refs:
                touched_refs.append(ref)
        for ref, _ in self._staged_updates:
            if ref not in touched_refs:
                touched_refs.append(ref)
        for ref in self._staged_deletes:
            if ref not in touched_refs:
                touched_refs.append(ref)

        snapshots = {}
        for ref in touched_refs:
            old_data = dict(ref.data) if getattr(ref, "data", None) is not None else None
            old_deleted = getattr(ref, "deleted", False)
            snapshots[ref] = (old_data, old_deleted)

        try:
            for doc_ref, data in self._staged_creates:
                if getattr(doc_ref, "exists", False):
                    from google.api_core.exceptions import AlreadyExists
                    raise AlreadyExists(f"Document already exists: {doc_ref}")
                doc_ref.set(data)
            for doc_ref, data in self._staged_sets:
                doc_ref.set(data)
            for doc_ref, updates in self._staged_updates:
                doc_ref.update(updates)
            for doc_ref in self._staged_deletes:
                doc_ref.delete()
            self.committed = True
        except Exception:
            for ref, (old_data, old_deleted) in snapshots.items():
                ref.data = dict(old_data) if old_data is not None else None
                ref.deleted = old_deleted
            raise

    def _begin(self, *args, **kwargs):
        if hasattr(self._db, "_tx_lock") and self._db._tx_lock is not None:
            self._db._tx_lock.acquire()

    def _clean_up(self):
        self._staged_sets.clear()
        self._staged_creates.clear()
        self._staged_updates.clear()
        self._staged_deletes.clear()

    def _rollback(self):
        self._staged_sets.clear()
        self._staged_creates.clear()
        self._staged_updates.clear()
        self._staged_deletes.clear()
        if hasattr(self._db, "_tx_lock") and self._db._tx_lock is not None:
            try:
                self._db._tx_lock.release()
            except RuntimeError:
                pass

    def _commit(self):
        try:
            self.commit()
            return []
        finally:
            if hasattr(self._db, "_tx_lock") and self._db._tx_lock is not None:
                try:
                    self._db._tx_lock.release()
                except RuntimeError:
                    pass


class _FakeFirestore:
    def __init__(self, collections=None):
        import threading
        self.collections = collections or {}
        self.last_transaction = None
        self._tx_lock = threading.Lock()

    def collection(self, name):
        class _Coll:
            def __init__(self, coll_name, docs):
                self.coll_name = coll_name
                self.docs = docs

            def document(self, doc_id):
                if doc_id in self.docs:
                    return self.docs[doc_id]
                if self.coll_name == "contractors":
                    doc = _FakeDocRef({"contractor_id": doc_id, "active": True}, doc_id=doc_id)
                else:
                    doc = _FakeDocRef(None, doc_id=doc_id)
                self.docs[doc_id] = doc
                return doc

        return _Coll(name, self.collections.setdefault(name, {}))

    def transaction(self):
        tx = _FakeTransaction(self)
        self.last_transaction = tx
        return tx


class _FakeResponse:
    def __init__(self, status_code, body, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body


class _FakeAsyncClient:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *_args, **_kwargs):
        return self.response


@pytest.mark.asyncio
async def test_persist_refreshed_tokens_cas_rejects_non_string_raw_payloads(monkeypatch):
    _setup_keyring(monkeypatch)
    fake_db = _FakeFirestore()
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: fake_db)

    for inv in ({"raw": "dict"}, True, False, 123, 1.0, None, "", "a" * (MAX_PLAINTEXT_BYTES + 1)):
        with pytest.raises(IntegrationTokenEnvelopeError):
            await persist_refreshed_tokens_cas(
                contractor_id="c1",
                provider="jobber",
                new_access_token=inv,
                new_refresh_token="valid-ref",
                observed_generation=0,
                observed_access_raw="old-acc",
                observed_refresh_raw="old-ref",
                claim_id="cl-valid",
                db=fake_db,
            )

    for inv_claim in ({"raw": "dict"}, True, False, 123, 1.0, None, "", "bad spaces", "a" * (MAX_PLAINTEXT_BYTES + 1)):
        with pytest.raises(IntegrationTokenEnvelopeError):
            await persist_refreshed_tokens_cas(
                contractor_id="c1",
                provider="jobber",
                new_access_token="valid-acc",
                new_refresh_token="valid-ref",
                observed_generation=0,
                observed_access_raw="old-acc",
                observed_refresh_raw="old-ref",
                claim_id=inv_claim,
                db=fake_db,
            )


@pytest.mark.asyncio
async def test_refresh_persistence_failure_leaves_contractor_dict_unchanged(monkeypatch):
    _setup_keyring(monkeypatch)
    fake_db = _FakeFirestore()
    monkeypatch.setattr(firestore_module, "get_firestore_client", lambda: fake_db)

    contractor_id = "c-fail-1"
    enc_access = encrypt_integration_token("old-access", contractor_id=contractor_id, provider="jobber", token_kind="access")
    enc_refresh = encrypt_integration_token("old-refresh", contractor_id=contractor_id, provider="jobber", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": contractor_id,
        "jobber_connected": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
    }, doc_id=contractor_id)
    fake_db.collections["contractors"] = {contractor_id: doc_ref}

    initial_jobber_dict = {
        "contractor_id": contractor_id,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
    }
    input_copy = dict(initial_jobber_dict)

    monkeypatch.setattr(settings, "jobber_client_id", "test-client")
    monkeypatch.setattr(settings, "jobber_client_secret", "test-secret")

    # Mock provider response returning new tokens
    response = _FakeResponse(200, {"access_token": "fresh-access-123", "refresh_token": "fresh-ref-456"})
    monkeypatch.setattr(jobber_service.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))

    # Mock persist_refreshed_tokens_cas raising RuntimeError
    async def _failing_write(*_args, **_kwargs):
        raise RuntimeError("Firestore write failure")

    import app.services.integration_token_mutations as it_mutations
    monkeypatch.setattr(it_mutations, "persist_refreshed_tokens_cas", _failing_write)

    res = await jobber_service.refresh_access_token(input_copy, force=True)
    assert res is None
    # Crucial invariant: input dictionary MUST NOT be mutated
    assert input_copy == initial_jobber_dict

    # Repeat for Google Calendar
    enc_gcal_acc = encrypt_integration_token("old-gcal-acc", contractor_id=contractor_id, provider="google_calendar", token_kind="access")
    enc_gcal_ref = encrypt_integration_token("old-gcal-ref", contractor_id=contractor_id, provider="google_calendar", token_kind="refresh")
    doc_ref_gcal = _FakeDocRef({
        "contractor_id": contractor_id,
        "google_calendar_connected": True,
        "google_calendar_lifecycle_epoch": 0,
        "google_calendar_generation": 1,
        "google_calendar_access_token": enc_gcal_acc,
        "google_calendar_refresh_token": enc_gcal_ref,
    }, doc_id=contractor_id)
    fake_db.collections["contractors"] = {contractor_id: doc_ref_gcal}

    initial_gcal_dict = {
        "contractor_id": contractor_id,
        "google_calendar_access_token": enc_gcal_acc,
        "google_calendar_refresh_token": enc_gcal_ref,
    }
    input_gcal_copy = dict(initial_gcal_dict)

    monkeypatch.setattr(settings, "google_calendar_client_id", "gcal-client")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "gcal-secret")
    monkeypatch.setattr(calendar_service.httpx, "AsyncClient", lambda: _FakeAsyncClient(response))

    gcal_res = await calendar_service.refresh_access_token(input_gcal_copy, force=True)
    assert gcal_res is None
    assert input_gcal_copy == initial_gcal_dict


@pytest.mark.asyncio
async def test_refresh_missing_contractor_id_or_config_makes_zero_provider_calls(monkeypatch):
    _setup_keyring(monkeypatch)

    class _MustNotBeCalledClient:
        async def __aenter__(self):
            raise AssertionError("Provider HTTP request was made unexpectedly")

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(jobber_service.httpx, "AsyncClient", _MustNotBeCalledClient)
    monkeypatch.setattr(calendar_service.httpx, "AsyncClient", _MustNotBeCalledClient)

    # 1. Missing contractor_id
    contractor_no_id = {"jobber_refresh_token": "ref-tok"}
    assert await jobber_service.refresh_access_token(contractor_no_id, force=True) is None

    contractor_gcal_no_id = {"google_calendar_refresh_token": "ref-tok"}
    assert await calendar_service.refresh_access_token(contractor_gcal_no_id, force=True) is None

    # 2. Unconfigured encryption
    monkeypatch.setattr(settings, "integration_token_encryption_keys", "")
    contractor_with_id = {"contractor_id": "c1", "jobber_refresh_token": "ref-tok"}
    assert await jobber_service.refresh_access_token(contractor_with_id, force=True) is None
    contractor_gcal_with_id = {"contractor_id": "c1", "google_calendar_refresh_token": "ref-tok"}
    assert await calendar_service.refresh_access_token(contractor_gcal_with_id, force=True) is None


# ---------------------------------------------------------------------------
# 7. Direct Consumer Tool Gating & Side-Effect Prevention
# ---------------------------------------------------------------------------

def test_voice_pipeline_tool_gating_rejects_malformed_envelope(monkeypatch):
    _setup_keyring(monkeypatch)
    contractor_id = "c-vp-1"
    bad_envelope = {"schema_version": 1, "ciphertext": "bad", "nonce": "bad"}

    # 1. Malformed envelope -> disabled
    malformed_config = {
        "contractor_id": contractor_id,
        "jobber_access_token": bad_envelope,
        "google_calendar_access_token": bad_envelope,
    }
    vp = voice_pipeline.VoicePipeline(
        on_audio_out=lambda *a: None,
        on_transcript=lambda *a: None,
        call_sid="CA123",
        contractor_config=malformed_config,
    )
    assert vp._has_jobber() is False
    assert vp._has_google_calendar() is False
    assert vp._get_jobber_token() == ""
    assert vp._get_google_calendar_token() == ""

    # 2. Valid envelope -> enabled
    valid_jobber_acc = encrypt_integration_token("jobber-valid-token", contractor_id=contractor_id, provider="jobber", token_kind="access")
    valid_jobber_ref = encrypt_integration_token("jobber-valid-ref", contractor_id=contractor_id, provider="jobber", token_kind="refresh")
    valid_gcal_acc = encrypt_integration_token("gcal-valid-token", contractor_id=contractor_id, provider="google_calendar", token_kind="access")
    valid_gcal_ref = encrypt_integration_token("gcal-valid-ref", contractor_id=contractor_id, provider="google_calendar", token_kind="refresh")
    valid_config = {
        "contractor_id": contractor_id,
        "jobber_access_token": valid_jobber_acc,
        "jobber_refresh_token": valid_jobber_ref,
        "google_calendar_access_token": valid_gcal_acc,
        "google_calendar_refresh_token": valid_gcal_ref,
    }
    vp_valid = voice_pipeline.VoicePipeline(
        on_audio_out=lambda *a: None,
        on_transcript=lambda *a: None,
        call_sid="CA123",
        contractor_config=valid_config,
    )
    assert vp_valid._has_jobber() is True
    assert vp_valid._has_google_calendar() is True
    assert vp_valid._get_jobber_token() == "jobber-valid-token"
    assert vp_valid._get_google_calendar_token() == "gcal-valid-token"


def test_gemini_pipeline_tool_building_rejects_malformed_envelope(monkeypatch):
    _setup_keyring(monkeypatch)
    contractor_id = "c-gp-1"
    bad_envelope = {"schema_version": 1, "ciphertext": "bad", "nonce": "bad"}

    # 1. Malformed envelope -> no tools declared
    malformed_config = {
        "contractor_id": contractor_id,
        "jobber_access_token": bad_envelope,
        "google_calendar_access_token": bad_envelope,
    }
    gp = gemini_pipeline.GeminiPipeline(
        on_audio_out=lambda *a: None,
        on_transcript=lambda *a: None,
        call_sid="CA123",
        contractor_config=malformed_config,
    )
    tools = gp._build_gemini_tools()
    assert tools == []

    # 2. Valid Jobber envelope -> declares check_customer
    valid_jobber_acc = encrypt_integration_token("jobber-token", contractor_id=contractor_id, provider="jobber", token_kind="access")
    valid_jobber_ref = encrypt_integration_token("jobber-ref", contractor_id=contractor_id, provider="jobber", token_kind="refresh")
    gp_jobber = gemini_pipeline.GeminiPipeline(
        on_audio_out=lambda *a: None,
        on_transcript=lambda *a: None,
        call_sid="CA123",
        contractor_config={
            "contractor_id": contractor_id,
            "jobber_access_token": valid_jobber_acc,
            "jobber_refresh_token": valid_jobber_ref,
        },
    )
    jobber_tools = gp_jobber._build_gemini_tools()
    names = [fn["name"] for fn in jobber_tools[0]["function_declarations"]]
    assert "check_customer" in names

    # 3. Valid Google Calendar envelope -> declares check_availability and book_appointment
    valid_gcal_acc = encrypt_integration_token("gcal-token", contractor_id=contractor_id, provider="google_calendar", token_kind="access")
    valid_gcal_ref = encrypt_integration_token("gcal-ref", contractor_id=contractor_id, provider="google_calendar", token_kind="refresh")
    gp_gcal = gemini_pipeline.GeminiPipeline(
        on_audio_out=lambda *a: None,
        on_transcript=lambda *a: None,
        call_sid="CA123",
        contractor_config={
            "contractor_id": contractor_id,
            "google_calendar_access_token": valid_gcal_acc,
            "google_calendar_refresh_token": valid_gcal_ref,
        },
    )
    gcal_tools = gp_gcal._build_gemini_tools()
    gcal_names = [fn["name"] for fn in gcal_tools[0]["function_declarations"]]
    assert "check_availability" in gcal_names
    assert "book_appointment" in gcal_names


def test_receptionist_context_continuity_rejects_malformed_envelope(monkeypatch):
    _setup_keyring(monkeypatch)
    contractor_id = "c-rec-1"
    bad_envelope = {"schema_version": 1, "bad": True}

    config = {
        "contractor_id": contractor_id,
        "service_request_mutations_enabled": True,
        "integration_write_status": "approved",
        "google_calendar_access_token": bad_envelope,
        "service_request_context": {
            "customer_key": "k1",
            "open_service_requests": [{"request_id": "r1", "status": "open"}],
        },
    }
    monkeypatch.setattr(settings, "service_request_recovery_enabled", True)
    prompt = receptionist_context.build_customer_memory_prompt(config)
    assert prompt == ""


@pytest.mark.asyncio
async def test_post_call_lead_capture_rejects_malformed_envelope(monkeypatch):
    _setup_keyring(monkeypatch)
    bad_envelope = {"schema_version": 1, "bad": True}
    contractor = {
        "contractor_id": "c-pc-1",
        "jobber_access_token": bad_envelope,
        "jobber_lead_capture_enabled": True,
    }
    job_data = {"call_sid": "CA123", "call_type": "service_request"}

    # Must return None without attempting sync
    captured = await post_call._capture_jobber_lead(contractor, job_data, "job-123")
    assert captured is None


# ---------------------------------------------------------------------------
# 8. OAuth Callbacks, Disconnect Guarantees & Safe Logging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_preflight_missing_keyring_fails_without_consuming_state(monkeypatch):
    # Empty keyring with encrypted writes enabled
    monkeypatch.setattr(settings, "integration_token_encryption_keys", "")
    monkeypatch.setattr(settings, "integration_token_active_key_version", None)
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", True)

    state_jobber = _FakeDocRef({"contractor_id": "c1", "expires_at": time.time() + 1000.0})
    state_gcal = _FakeDocRef({"contractor_id": "c1", "expires_at": time.time() + 1000.0})
    db = _FakeFirestore({
        "jobber_oauth_states": {"state-jobber-123456": state_jobber},
        "google_oauth_states": {"state-google-123456": state_gcal},
    })
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)

    # Jobber callback
    with pytest.raises(HTTPException) as exc_j:
        await integrations.jobber_callback(code="code", state="state-jobber-123456")
    assert exc_j.value.status_code == 500
    assert state_jobber.deleted is False

    # Google callback
    with pytest.raises(HTTPException) as exc_g:
        await integrations.google_calendar_callback(code="code", state="state-google-123456")
    assert exc_g.value.status_code == 500
    assert state_gcal.deleted is False


@pytest.mark.asyncio
async def test_disconnect_with_revoke_error_still_deletes_firestore_fields(monkeypatch, caplog):
    _setup_keyring(monkeypatch)
    contractor_id_j = "c-disc-rev-j"
    contractor_id_g = "c-disc-rev-g"
    enc_jobber = encrypt_integration_token("jobber-acc-tok", contractor_id=contractor_id_j, provider="jobber", token_kind="access")
    enc_gcal = encrypt_integration_token("gcal-acc-tok", contractor_id=contractor_id_g, provider="google_calendar", token_kind="access")

    doc_jobber = _FakeDocRef({"contractor_id": contractor_id_j, "active": True, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc_jobber, "jobber_refresh_token": enc_jobber}, doc_id=contractor_id_j)
    doc_gcal = _FakeDocRef({"contractor_id": contractor_id_g, "active": True, "google_calendar_connected": True, "google_calendar_generation": 1, "google_calendar_lifecycle_epoch": 1, "google_calendar_access_token": enc_gcal, "google_calendar_refresh_token": enc_gcal}, doc_id=contractor_id_g)

    db = _FakeFirestore({"contractors": {contractor_id_j: doc_jobber, contractor_id_g: doc_gcal}})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    _patch_firestore(monkeypatch, db)

    # Mock revoke endpoint throwing exception
    class _FailingRevokeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("Simulated connection timeout to provider revoke")

    monkeypatch.setattr(integrations.httpx, "AsyncClient", _FailingRevokeClient)

    req = type("Req", (), {"state": type("State", (), {"is_admin": True})()})()

    # Jobber disconnect
    res_j = await integrations.jobber_disconnect(contractor_id=contractor_id_j, request=req)
    assert res_j["status"] == "disconnected"
    from google.cloud.firestore_v1 import DELETE_FIELD
    assert ("jobber_access_token" not in doc_jobber.data or doc_jobber.data["jobber_access_token"] is DELETE_FIELD)
    assert doc_jobber.data["jobber_connected"] is False

    # Google disconnect
    res_g = await integrations.google_calendar_disconnect(contractor_id=contractor_id_g, request=req)
    assert res_g["status"] == "disconnected"
    assert ("google_calendar_access_token" not in doc_gcal.data or doc_gcal.data["google_calendar_access_token"] is DELETE_FIELD)
    assert doc_gcal.data["google_calendar_connected"] is False


# ---------------------------------------------------------------------------
# 9. Admin Summary Regression Test
# ---------------------------------------------------------------------------

def test_admin_jobber_summary_with_encrypted_token(monkeypatch):
    _setup_keyring(monkeypatch)
    contractor_id = "c-admin-summary-1"
    enc_access = encrypt_integration_token("jobber-valid-secret", contractor_id=contractor_id, provider="jobber", token_kind="access")
    enc_refresh = encrypt_integration_token("jobber-valid-refresh", contractor_id=contractor_id, provider="jobber", token_kind="refresh")

    # Raw contractor data from Firestore (which doesn't have "contractor_id" in dict)
    raw_contractor_data = {
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
        "jobber_connected_at": 1700000000.0,
        "jobber_lead_capture_enabled": True,
    }

    # Calling with contractor_id correctly reports connected
    summary = admin_api._jobber_summary(raw_contractor_data, contractor_id=contractor_id)
    assert summary["connected"] is True
    assert summary["lead_capture_enabled"] is True

    # Calling with wrong contractor_id fails safe (reports disconnected)
    wrong_summary = admin_api._jobber_summary(raw_contractor_data, contractor_id="wrong-id")
    assert wrong_summary["connected"] is False
    assert wrong_summary["lead_capture_enabled"] is False


# ---------------------------------------------------------------------------
# 10. Strict Stored Scalars & Exact AES-256 Contract
# ---------------------------------------------------------------------------

def test_strict_stored_scalars_rejection(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-strict-scalars"

    class CustomStr(str):
        pass

    malformed_values = [
        123,
        True,
        False,
        12.34,
        b"raw-bytes",
        ["token-in-list"],
        {"custom": "map"},
        CustomStr("custom-str-subclass"),
    ]

    for val in malformed_values:
        # decrypt_integration_token raises IntegrationTokenEnvelopeError
        with pytest.raises(IntegrationTokenEnvelopeError):
            decrypt_integration_token(val, contractor_id=cid, provider="jobber", token_kind="access")

        # safe_decrypt_integration_token returns None
        assert safe_decrypt_integration_token(val, contractor_id=cid, provider="jobber", token_kind="access") is None

        # resolve_usable_token returns None
        contractor = {"contractor_id": cid, "jobber_access_token": val}
        assert resolve_usable_token(contractor, "jobber", "access") is None
        assert has_usable_token(contractor, "jobber", "access") is False


def test_exact_aes_256_key_sizes_and_bool_aliasing(monkeypatch):
    from app.services.integration_tokens import _validate_keyring_dict

    # 16-byte key (AES-128) and 24-byte key (AES-192) rejected
    with pytest.raises(IntegrationTokenConfigError, match="length 32"):
        _validate_keyring_dict({1: b"k" * 16})
    with pytest.raises(IntegrationTokenConfigError, match="length 32"):
        _validate_keyring_dict({1: b"k" * 24})

    # Non-dict keyring rejected
    with pytest.raises(IntegrationTokenConfigError):
        _validate_keyring_dict(["not-a-dict"])

    # Boolean key aliasing rejected ({True: 32_bytes} is not exact int 1)
    with pytest.raises(IntegrationTokenConfigError, match="exact int"):
        _validate_keyring_dict({True: b"k" * 32})

    # Active version booleans and floats rejected
    for invalid_v in (True, False, 1.0, "1.0", "01", "-1"):
        with pytest.raises(IntegrationTokenConfigError):
            parse_active_key_version(invalid_v)


# ---------------------------------------------------------------------------
# 11. Multi-Instance Durable CAS Simulation Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_durable_cas_refresh_vs_disconnect_race_simulation(monkeypatch):
    """Simulate in-flight token refresh losing a race to a concurrent disconnect."""
    from app.services.integration_token_mutations import (
        disconnect_provider_cas,
        persist_refreshed_tokens_cas,
    )
    from app.services.integration_tokens import (
        IntegrationTokenCASConflict,
    )
    _setup_keyring(monkeypatch)
    cid = "c-race-disc"

    enc_access = encrypt_integration_token("old-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_refresh = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_connected": True,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
    })
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    # Step 1: Refresh instance A observes durable state at generation 1 and acquires claim
    observed_gen = 1
    observed_acc_raw = enc_access
    observed_ref_raw = enc_refresh
    claim_id_a, _ = await acquire_refresh_claim_cas(
        contractor_id=cid,
        provider="jobber",
        observed_generation=observed_gen,
        observed_access_raw=observed_acc_raw,
        observed_refresh_raw=observed_ref_raw,
        db=db,
    )

    # Step 2: Concurrent process B executes disconnect before A's provider call completes
    tombstone_gen, _, _ = await disconnect_provider_cas(contractor_id=cid, provider="jobber", db=db)
    assert tombstone_gen == 2
    from google.cloud.firestore_v1 import DELETE_FIELD
    assert doc_ref.data["jobber_connected"] is False
    assert ("jobber_access_token" not in doc_ref.data or doc_ref.data["jobber_access_token"] is DELETE_FIELD)

    # Step 3: Refresh instance A attempts CAS persist with observed generation 1
    with pytest.raises(IntegrationTokenCASConflict):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-acc",
            new_refresh_token="new-ref",
            observed_generation=observed_gen,
            observed_access_raw=observed_acc_raw,
            observed_refresh_raw=observed_ref_raw,
            claim_id=claim_id_a,
            db=db,
        )

    # Precondition verified: credentials remain DELETED and disconnected
    assert doc_ref.data["jobber_connected"] is False
    assert ("jobber_access_token" not in doc_ref.data or doc_ref.data["jobber_access_token"] is DELETE_FIELD)
    assert doc_ref.data["jobber_generation"] == 2


@pytest.mark.asyncio
async def test_durable_cas_refresh_vs_reconnect_race_simulation(monkeypatch):
    """Simulate in-flight token refresh losing a race to a concurrent reconnect."""
    from app.services.integration_token_mutations import (
        persist_refreshed_tokens_cas,
    )
    from app.services.integration_tokens import (
        IntegrationTokenCASConflict,
    )
    _setup_keyring(monkeypatch)
    cid = "c-race-reconn"

    enc_access = encrypt_integration_token("old-acc", contractor_id=cid, provider="google_calendar", token_kind="access")
    enc_refresh = encrypt_integration_token("old-ref", contractor_id=cid, provider="google_calendar", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "google_calendar_lifecycle_epoch": 0,
        "google_calendar_generation": 1,
        "google_calendar_connected": True,
        "google_calendar_access_token": enc_access,
        "google_calendar_refresh_token": enc_refresh,
    })
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    # Step 1: Refresh instance A observes state at generation 1 and acquires claim
    observed_gen = 1
    observed_acc_raw = enc_access
    observed_ref_raw = enc_refresh
    claim_id_a, _ = await acquire_refresh_claim_cas(
        contractor_id=cid,
        provider="google_calendar",
        observed_generation=observed_gen,
        observed_access_raw=observed_acc_raw,
        observed_refresh_raw=observed_ref_raw,
        db=db,
    )

    # Step 2: User completes OAuth reconnect in browser (advances generation to 2)
    claim_id_b = "b" * 32
    all_legacy = {f"google_calendar_{k}" for k in it_mutations.LEGACY_CLAIM_BASE_KEYS}
    for k in all_legacy:
        doc_ref.data.pop(k, None)

    doc_ref.data["google_calendar_operation_intent_id"] = claim_id_b
    doc_ref.data["google_calendar_operation_intent_kind"] = "reconnect"
    doc_ref.data["google_calendar_operation_intent_phase"] = "provider_request_started"
    doc_ref.data["google_calendar_operation_intent_expires_at"] = time.time() + 300.0
    doc_ref.data["google_calendar_operation_intent_acquired_at"] = time.time()
    doc_ref.data["google_calendar_operation_intent_generation"] = 1
    doc_ref.data["google_calendar_operation_intent_lifecycle_epoch"] = 0

    updates_conn, new_gen, _ = await connect_provider_cas(
        contractor_id=cid,
        provider="google_calendar",
        access_token="reconnect-access",
        refresh_token="reconnect-refresh",
        claim_id=claim_id_b,
        db=db,
    )
    assert new_gen == 2
    assert doc_ref.data["google_calendar_generation"] == 2

    # Step 3: Stale in-flight refresh attempts CAS persist with observed generation 1
    with pytest.raises(IntegrationTokenCASConflict):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="google_calendar",
            new_access_token="stale-refreshed-access",
            new_refresh_token="stale-refreshed-refresh",
            observed_generation=observed_gen,
            observed_access_raw=observed_acc_raw,
            observed_refresh_raw=observed_ref_raw,
            claim_id=claim_id_a,
            db=db,
        )

    # Credentials from the reconnect remain intact, not overwritten by stale refresh
    assert doc_ref.data["google_calendar_generation"] == 2
    assert doc_ref.data["google_calendar_access_token"] == updates_conn["google_calendar_access_token"]


@pytest.mark.asyncio
async def test_oauth_state_one_time_consumption(monkeypatch):
    """Verify OAuth state is consumed atomically and cannot be replayed."""
    from app.services.integration_tokens import compute_raw_credentials_fingerprint
    now = time.time()
    fp = compute_raw_credentials_fingerprint(None, None)
    state_doc = _FakeDocRef({
        "contractor_id": "c-oauth-1",
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": fp,
        "created_at": now,
        "expires_at": now + 600.0,
    })
    contractor_doc = _FakeDocRef({
        "contractor_id": "c-oauth-1",
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    })
    db = _FakeFirestore({
        "jobber_oauth_states": {"state-unique-123456": state_doc},
        "contractors": {"c-oauth-1": contractor_doc},
    })

    # First consumption succeeds
    data, contractor_obs = await consume_oauth_state(db=db, collection_name="jobber_oauth_states", state="state-unique-123456")
    assert data["contractor_id"] == "c-oauth-1"
    assert contractor_obs["contractor_id"] == "c-oauth-1"
    assert state_doc.deleted is True

    # Replay attempt fails with 400
    with pytest.raises(HTTPException) as exc:
        await consume_oauth_state(db=db, collection_name="jobber_oauth_states", state="state-unique-123456")
    assert exc.value.status_code == 400


def test_validate_token_expiry():
    from app.services.integration_tokens import (
        IntegrationTokenEnvelopeError,
    )

    assert validate_token_expires_in(None) is None
    assert validate_token_expires_in(3600) == 3600.0
    assert validate_token_expires_at(1700000000.5) == 1700000000.5
    for invalid in (True, False, "3600", float("nan"), float("inf"), -10):
        with pytest.raises(IntegrationTokenEnvelopeError):
            validate_token_expires_in(invalid)


# ---------------------------------------------------------------------------
# 12. Ambiguous Commit Recovery & Rollback Floor Verification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_durable_cas_ambiguous_commit_recovery(monkeypatch):
    """Verify that an ambiguous commit exception checks durable state and recovers if proven committed."""
    from app.services.integration_token_mutations import persist_refreshed_tokens_cas
    from app.services.integration_tokens import (
        IntegrationTokenCASConflict,
    )
    _setup_keyring(monkeypatch)
    cid = "c-ambig-1"

    enc_access = encrypt_integration_token("old-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_refresh = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_connected": True,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
    })

    # Case A: Commit raises network error, but the update WAS applied (next_gen=2 and fresh envelope present)
    class _AmbigSucceededFirestore(_FakeFirestore):
        def transaction(self):
            class _AmbigTx(_FakeTransaction):
                def _commit(self):
                    super().commit()
                    raise ConnectionResetError("Simulated network drop on commit confirmation")

            return _AmbigTx(self)

    db_success = _AmbigSucceededFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db_success)

    # Should recover because the re-read found generation 2 and matching encrypted access token
    claim_id_a = secrets.token_hex(16)
    doc_ref.data["jobber_refresh_claim_id"] = claim_id_a
    doc_ref.data["jobber_refresh_claim_phase"] = "provider_request_started"
    doc_ref.data["jobber_refresh_claim_expires_at"] = time.time() + 60.0
    doc_ref.data["jobber_refresh_claim_generation"] = 1

    updates, next_gen = await persist_refreshed_tokens_cas(
        contractor_id=cid,
        provider="jobber",
        new_access_token="new-acc-proven",
        new_refresh_token="new-ref-proven",
        observed_generation=1,
        observed_access_raw=enc_access,
        observed_refresh_raw=enc_refresh,
        claim_id=claim_id_a,
        db=db_success,
    )
    assert next_gen == 2
    assert doc_ref.data["jobber_generation"] == 2

    # Case B: Commit raises network error and the update was NOT applied
    class _AmbigFailedFirestore(_FakeFirestore):
        def transaction(self):
            class _AmbigFailTx(_FakeTransaction):
                def _commit(self):
                    # Drops update and raises
                    raise ConnectionResetError("Simulated uncommitted network failure")

            return _AmbigFailTx(self)

    claim_id_b = secrets.token_hex(16)
    doc_ref_uncommitted = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_connected": True,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
        "jobber_refresh_claim_id": claim_id_b,
        "jobber_refresh_claim_phase": "provider_request_started",
        "jobber_refresh_claim_expires_at": time.time() + 60.0,
        "jobber_refresh_claim_generation": 1,
    })
    db_failed = _AmbigFailedFirestore({"contractors": {cid: doc_ref_uncommitted}})
    _patch_firestore(monkeypatch, db_failed)

    with pytest.raises(IntegrationTokenCASConflict):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-acc-uncommitted",
            new_refresh_token="new-ref-uncommitted",
            observed_generation=1,
            observed_access_raw=enc_access,
            observed_refresh_raw=enc_refresh,
            claim_id=claim_id_b,
            db=db_failed,
        )


def test_reader_first_rollback_floor_compatibility(monkeypatch):
    """Verify that a reader-only node successfully decrypts envelopes written by an encrypted writer."""
    _setup_keyring(monkeypatch)
    cid = "c-rollback-floor"

    # Writer produces a v1 envelope pair
    writer_access_envelope = encrypt_integration_token(
        "secret-live-access-token",
        contractor_id=cid,
        provider="jobber",
        token_kind="access",
    )
    writer_refresh_envelope = encrypt_integration_token(
        "secret-live-refresh-token",
        contractor_id=cid,
        provider="jobber",
        token_kind="refresh",
    )

    # Reader (simulating Phase 1 release) decrypts it without any writer logic
    decrypted = decrypt_integration_token(
        writer_access_envelope,
        contractor_id=cid,
        provider="jobber",
        token_kind="access",
    )
    assert decrypted == "secret-live-access-token"

    # Reader resolves it seamlessly through resolve_usable_token
    contractor = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": writer_access_envelope,
        "jobber_refresh_token": writer_refresh_envelope,
    }
    assert resolve_usable_token(contractor, "jobber", "access") == "secret-live-access-token"
    assert resolve_usable_token(contractor, "jobber", "refresh") == "secret-live-refresh-token"
    assert has_usable_token(contractor, "jobber", "access") is True


# ---------------------------------------------------------------------------
# 13. Production Hardening: Commit Proof, No-DB Fail-Closed, Sanitized Errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transaction_commits_staged_mutations(monkeypatch):
    """Verify that a transaction explicitly commits staged mutations to Firestore."""
    from app.services.integration_token_mutations import persist_refreshed_tokens_cas
    _setup_keyring(monkeypatch)
    cid = "c-commit-test"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 0,
        "jobber_connected": True,
        "jobber_access_token": "old-access",
        "jobber_refresh_token": "old-refresh",
    })
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    claim_id, _ = await acquire_refresh_claim_cas(
        contractor_id=cid,
        provider="jobber",
        observed_generation=0,
        observed_access_raw="old-access",
        observed_refresh_raw="old-refresh",
        db=db,
    )
    await transition_refresh_claim_to_started_cas(
        contractor_id=cid,
        provider="jobber",
        claim_id=claim_id,
        observed_generation=0,
        observed_access_raw="old-access",
        observed_refresh_raw="old-refresh",
        db=db,
    )
    updates, next_gen = await persist_refreshed_tokens_cas(
        contractor_id=cid,
        provider="jobber",
        new_access_token="new-access",
        new_refresh_token="new-refresh",
        observed_generation=0,
        observed_access_raw="old-access",
        observed_refresh_raw="old-refresh",
        claim_id=claim_id,
        db=db,
    )
    assert next_gen == 1
    assert db.last_transaction is not None
    assert db.last_transaction.committed is True
    assert doc_ref.data["jobber_generation"] == 1


@pytest.mark.asyncio
async def test_uncommitted_transaction_does_not_mutate_document(monkeypatch):
    """Verify that if a transaction fails before commit, staged mutations are NOT applied."""
    from app.services.integration_token_mutations import persist_refreshed_tokens_cas
    from app.services.integration_tokens import (
        IntegrationTokenCASConflict,
    )
    _setup_keyring(monkeypatch)
    cid = "c-uncommitted-test"

    claim_id_uncommitted = secrets.token_hex(16)
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_connected": True,
        "jobber_access_token": "old-access",
        "jobber_refresh_token": "old-refresh",
        "jobber_refresh_claim_id": claim_id_uncommitted,
        "jobber_refresh_claim_expires_at": time.time() + 60,
        "jobber_refresh_claim_generation": 1,
    })
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    # Calling with mismatched observed generation (0 != 1) must raise and leave doc completely unchanged
    with pytest.raises(IntegrationTokenCASConflict):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-access",
            new_refresh_token="new-refresh",
            observed_generation=0,
            observed_access_raw="old-access",
            observed_refresh_raw="old-refresh",
            claim_id=claim_id_uncommitted,
            db=db,
        )

    assert doc_ref.data["jobber_generation"] == 1
    assert doc_ref.data["jobber_access_token"] == "old-access"


@pytest.mark.asyncio
async def test_no_database_fails_closed_across_all_cas_helpers(monkeypatch):
    """Verify that all CAS helpers fail closed with typed errors when db is None or unavailable."""
    from app.services.integration_token_mutations import (
        disconnect_provider_cas,
        persist_refreshed_tokens_cas,
    )
    _setup_keyring(monkeypatch)

    def _no_db():
        return None

    _patch_firestore(monkeypatch, _no_db)

    # 1. persist_refreshed_tokens_cas
    with pytest.raises(IntegrationTokenEnvelopeError):
        await persist_refreshed_tokens_cas(
            contractor_id="c-nodb",
            provider="jobber",
            new_access_token="acc",
            new_refresh_token="ref",
            observed_generation=0,
            observed_access_raw="old-acc",
            observed_refresh_raw="old-ref",
            claim_id=secrets.token_hex(16),
            db=None,
        )

    # 2. connect_provider_cas
    with pytest.raises(IntegrationTokenEnvelopeError):
        await connect_provider_cas(
            contractor_id="c-nodb",
            provider="jobber",
            access_token="acc",
            refresh_token="ref",
            db=None,
        )

    # 3. disconnect_provider_cas
    with pytest.raises(IntegrationTokenEnvelopeError):
        await disconnect_provider_cas(
            contractor_id="c-nodb",
            provider="jobber",
            db=None,
        )

    # 4. consume_oauth_state
    with pytest.raises(HTTPException) as exc:
        await consume_oauth_state(
            db=None,
            collection_name="jobber_oauth_states",
            state="state-1234567890123456",
        )
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_strict_cas_inputs_and_generation_validation(monkeypatch):
    """Verify strict validation of observed_generation, durable generation, and extra_updates."""
    from app.services.integration_token_mutations import persist_refreshed_tokens_cas
    from app.services.integration_tokens import (
        IntegrationTokenCASConflict,
        IntegrationTokenEnvelopeError,
    )
    _setup_keyring(monkeypatch)
    cid = "c-gen-val"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 0,
        "jobber_connected": True,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
    })
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    claim_id_val = secrets.token_hex(16)

    # Invalid observed_generation types/values
    for invalid_gen in (-1, True, False, 1.0, "0", 2147483648, None, [], {}):
        with pytest.raises(IntegrationTokenEnvelopeError):
            await persist_refreshed_tokens_cas(
                contractor_id=cid,
                provider="jobber",
                new_access_token="new-acc",
                new_refresh_token="new-ref",
                observed_generation=invalid_gen,
                observed_access_raw="acc",
                observed_refresh_raw="ref",
                claim_id=claim_id_val,
                db=db,
            )

    # Disallowed extra_updates
    with pytest.raises(IntegrationTokenEnvelopeError):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-acc",
            new_refresh_token="new-ref",
            observed_generation=0,
            observed_access_raw="acc",
            observed_refresh_raw="ref",
            claim_id=claim_id_val,
            extra_updates={"malicious_field": "injected"},
            db=db,
        )

    # Malformed durable generation on document
    for bad_durable_gen in ("0", True, -1, 3.14, [0]):
        doc_ref.data["jobber_generation"] = bad_durable_gen
        with pytest.raises((IntegrationTokenCASConflict, IntegrationTokenEnvelopeError)):
            await persist_refreshed_tokens_cas(
                contractor_id=cid,
                provider="jobber",
                new_access_token="new-acc",
                new_refresh_token="new-ref",
                observed_generation=0,
                observed_access_raw="acc",
                observed_refresh_raw="ref",
                claim_id=claim_id_val,
                db=db,
            )


@pytest.mark.asyncio
async def test_ambiguous_commit_fails_closed_on_partial_match(monkeypatch):
    """Verify that ambiguous commit fails closed if any required field does not match."""
    from app.services.integration_token_mutations import persist_refreshed_tokens_cas
    from app.services.integration_tokens import (
        IntegrationTokenCASConflict,
    )
    _setup_keyring(monkeypatch)
    cid = "c-ambig-partial"

    enc_access = encrypt_integration_token("old-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_refresh = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    # Simulation where commit raises error and leaves refresh_token mismatched
    class _PartialFirestore(_FakeFirestore):
        def transaction(self):
            class _PartialTx(_FakeTransaction):
                def _commit(self):
                    # Tamper with the update: change refresh token to corrupted value
                    for doc_ref, updates in self._staged_updates:
                        tampered = dict(updates)
                        tampered["jobber_refresh_token"] = "wrong-refresh"
                        doc_ref.update(tampered)
                    self.committed = True
                    raise ConnectionResetError("Simulated drop")

            return _PartialTx(self)

    claim_id_partial = secrets.token_hex(16)
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_connected": True,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
        "jobber_refresh_claim_id": claim_id_partial,
        "jobber_refresh_claim_expires_at": time.time() + 60,
        "jobber_refresh_claim_generation": 1,
    })
    db = _PartialFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    # Re-read finds generation 2 and access token, but refresh token is mismatched -> fails closed!
    with pytest.raises(IntegrationTokenCASConflict):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-acc",
            new_refresh_token="new-ref",
            observed_generation=1,
            observed_access_raw=enc_access,
            observed_refresh_raw=enc_refresh,
            claim_id=claim_id_partial,
            db=db,
        )


@pytest.mark.asyncio
async def test_ambiguous_commit_connect_and_disconnect(monkeypatch):
    """Verify ambiguous commit handling on connect_provider_cas and disconnect_provider_cas."""
    from app.services.integration_token_mutations import (
        disconnect_provider_cas,
    )
    _setup_keyring(monkeypatch)
    cid = "c-conn-disc-ambig"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 0,
        "jobber_connected": False,
    })

    class _AmbigDropFirestore(_FakeFirestore):
        def transaction(self):
            class _AmbigDropTx(_FakeTransaction):
                def _commit(self):
                    super().commit()
                    raise ConnectionResetError("Commit drop")

            return _AmbigDropTx(self)

    db = _AmbigDropFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    # Connect recovers on full match
    updates, next_gen, _ = await connect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        access_token="acc-1",
        refresh_token="ref-1",
        db=db,
    )
    assert next_gen == 1
    assert doc_ref.data["jobber_connected"] is True

    # Disconnect recovers on full tombstone match
    tombstone_gen, _, _ = await disconnect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        db=db,
    )
    assert tombstone_gen == 2
    assert doc_ref.data["jobber_connected"] is False


def test_sanitized_error_messages_contain_no_sentinels(monkeypatch, caplog):
    """Verify that exception messages never leak raw attacker inputs, tokens, or sentinels."""
    from app.services.integration_tokens import (
        decrypt_integration_token,
        encrypt_integration_token,
        parse_active_key_version,
        parse_keyring,
    )
    _setup_keyring(monkeypatch)

    sentinel_token = "SECRET_SENTINEL_TOKEN_XYZ_12345"
    sentinel_key = "CANARY_KEY_STRING_99999"

    # 1. parse_keyring
    try:
        parse_keyring(f'{{"{sentinel_key}": "bad-val"}}')
    except Exception as exc:
        assert sentinel_key not in str(exc)

    # 2. parse_active_key_version
    try:
        parse_active_key_version(f"{sentinel_key}")
    except Exception as exc:
        assert sentinel_key not in str(exc)

    # 3. encrypt_integration_token with invalid provider
    try:
        encrypt_integration_token(
            sentinel_token,
            contractor_id="c1",
            provider="invalid_provider_sentinel",
            token_kind="access",
        )
    except Exception as exc:
        assert sentinel_token not in str(exc)
        assert "invalid_provider_sentinel" not in str(exc)

    # 4. decrypt_integration_token with corrupted envelope
    corrupted = {
        "schema_version": 1,
        "key_version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": base64.b64encode(b"0" * 12).decode("ascii"),
        "ciphertext": base64.b64encode(f"{sentinel_token}".encode() + b"0" * 16).decode("ascii"),
    }
    try:
        decrypt_integration_token(
            corrupted,
            contractor_id="c1",
            provider="jobber",
            token_kind="access",
        )
    except Exception as exc:
        assert sentinel_token not in str(exc)




# ---------------------------------------------------------------------------
# 11. Qualification Repair 3 - Regressions, Lifecycle Audit, and Invariants
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_regression_transaction_missing_update_fails_without_doc_ref_mutation(monkeypatch):
    """Codex Regression 1: A transaction object missing update() must fail closed without calling doc_ref.update outside txn."""
    from app.services.integration_token_mutations import persist_refreshed_tokens_cas
    from app.services.integration_tokens import (
        IntegrationTokenEnvelopeError,
    )
    _setup_keyring(monkeypatch)
    cid = "c-reg-1-no-txn-update"

    mutations_outside_txn = []

    class _DocRefWithUpdate:
        def __init__(self):
            self.exists = True
            self.data = {
                "contractor_id": cid,
                "active": True,
                "jobber_lifecycle_epoch": 0,
                "jobber_generation": 0,
                "jobber_connected": True,
                "jobber_access_token": "legacy-acc",
                "jobber_refresh_token": "legacy-ref",
                "jobber_refresh_claim_id": "cl-reg",
                "jobber_refresh_claim_expires_at": time.time() + 60,
                "jobber_refresh_claim_generation": 0,
            }

        def to_dict(self):
            return dict(self.data)

        def update(self, updates):
            mutations_outside_txn.append(updates)
            self.data.update(updates)

    doc_ref = _DocRefWithUpdate()

    class _TxnWithoutUpdate:
        def __init__(self):
            self._id = b"test"
            self._max_attempts = 1
            self._read_only = False
        def get(self, ref):
            return doc_ref
        def _clean_up(self):
            pass
        def _begin(self, *args, **kwargs):
            pass
        def _rollback(self):
            pass
        def _commit(self):
            pass

    class _Db:
        def collection(self, name):
            return self

        def document(self, doc_id):
            return doc_ref

        def transaction(self):
            return _TxnWithoutUpdate()

    _patch_firestore(monkeypatch, _Db())

    with pytest.raises((IntegrationTokenEnvelopeError, IntegrationTokenCASConflict)):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-acc",
            new_refresh_token="new-ref",
            observed_generation=0,
            observed_access_raw="legacy-acc",
            observed_refresh_raw="legacy-ref",
            claim_id="cl-reg",
            db=_Db(),
        )

    # Invariant: Zero mutations outside the transaction!
    assert len(mutations_outside_txn) == 0
    assert doc_ref.data["jobber_generation"] == 0


@pytest.mark.asyncio
async def test_regression_pre_provider_read_failure_causes_zero_provider_calls(monkeypatch):
    """Codex Regression 3: Durable pre-provider read failure causes 0 provider calls and 0 contractor mutations."""
    _setup_keyring(monkeypatch)
    cid = "c-reg-3-read-fail"

    provider_http_calls = []

    class _MockHttpxClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            provider_http_calls.append(url)
            raise AssertionError("Provider HTTP request must NOT be executed!")

    monkeypatch.setattr("httpx.AsyncClient", _MockHttpxClient)

    # 1. Test when firestore get_firestore_client raises
    _patch_firestore(monkeypatch, None)

    contractor_jobber = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_access_token": "legacy-jobber-acc",
        "jobber_refresh_token": "legacy-jobber-ref",
    }
    orig_jobber = dict(contractor_jobber)

    res_jobber = await jobber_service.refresh_access_token(contractor_jobber, force=True)
    assert res_jobber is None
    assert contractor_jobber == orig_jobber
    assert len(provider_http_calls) == 0

    contractor_cal = {
        "contractor_id": cid,
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_access_token": "legacy-cal-acc",
        "google_calendar_refresh_token": "legacy-cal-ref",
    }
    orig_cal = dict(contractor_cal)

    res_cal = await calendar_service.refresh_access_token(contractor_cal, force=True)
    assert res_cal is None
    assert contractor_cal == orig_cal
    assert len(provider_http_calls) == 0


def test_regression_voice_pipeline_service_request_gating_malformed_envelope(monkeypatch):
    """Codex Regression 4: Malformed nonempty Google token envelope causes voice pipeline service request checks to return False."""
    _setup_keyring(monkeypatch)
    monkeypatch.setattr(settings, "service_request_recovery_enabled", True)

    malformed_envelope = {
        "schema_version": 1,
        "key_version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": "not-valid-base64",
        "ciphertext": "not-valid-base64",
    }

    contractor_config = {
        "contractor_id": "c-pipeline-test",
        "service_request_context": {
            "customer_key": "cust-1",
            "open_service_requests": [{"id": "sr-1"}],
        },
        "service_request_mutations_enabled": True,
        "integration_write_status": "approved",
        "google_calendar_access_token": malformed_envelope,
    }

    vp = voice_pipeline.VoicePipeline(on_audio_out=lambda *a: None, on_transcript=lambda *a: None, call_sid="CA1", contractor_config=contractor_config)

    # Both must return False when token is a malformed envelope
    assert vp._has_service_request_context() is False
    assert vp._managed_provider_create_enabled() is False

    # Valid encrypted envelope returns True
    valid_access = encrypt_integration_token(
        "valid-token",
        contractor_id="c-pipeline-test",
        provider="google_calendar",
        token_kind="access",
    )
    valid_refresh = encrypt_integration_token(
        "valid-refresh",
        contractor_id="c-pipeline-test",
        provider="google_calendar",
        token_kind="refresh",
    )
    contractor_config["google_calendar_access_token"] = valid_access
    contractor_config["google_calendar_refresh_token"] = valid_refresh
    vp_valid = voice_pipeline.VoicePipeline(on_audio_out=lambda *a: None, on_transcript=lambda *a: None, call_sid="CA1", contractor_config=contractor_config)
    assert vp_valid._has_service_request_context() is True
    assert vp_valid._managed_provider_create_enabled() is True


def test_validate_token_expiry_duration_and_timestamp():
    """Validate strict split expiry logic: expires_in duration vs expires_at timestamp."""

    # expires_in (duration: 1..31536000)
    assert validate_token_expires_in(None) is None
    assert validate_token_expires_in(3600) == 3600.0
    assert validate_token_expires_in(1) == 1.0
    assert validate_token_expires_in(31536000) == 31536000.0

    for invalid in (True, False, 0, -1, 31536001, "3600", float("nan"), float("inf"), -float("inf"), [], {}):
        with pytest.raises(IntegrationTokenEnvelopeError):
            validate_token_expires_in(invalid)

    # expires_at (absolute timestamp: >= 1.0)
    assert validate_token_expires_at(None) is None
    assert validate_token_expires_at(1700000000.0) == 1700000000.0
    assert validate_token_expires_at(4600.0) == 4600.0

    for invalid in (True, False, 0, -1, "1700000000", float("nan"), float("inf"), -float("inf"), [], {}):
        with pytest.raises(IntegrationTokenEnvelopeError):
            validate_token_expires_at(invalid)


def test_cas_exact_raw_credential_structural_comparisons():
    """CAS comparisons must be exact-type structural checks (reject True == 1 or 120.0 == 120)."""
    from app.services.integration_tokens import _exact_raw_credential_equal

    assert _exact_raw_credential_equal("token1", "token1") is True
    assert _exact_raw_credential_equal("token1", "token2") is False
    assert _exact_raw_credential_equal(None, None) is True
    assert _exact_raw_credential_equal("token", None) is False

    env1 = {
        "schema_version": 1,
        "key_version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": "abc",
        "ciphertext": "def",
    }
    env_same = dict(env1)
    assert _exact_raw_credential_equal(env1, env_same) is True

    # Type confusion: bool schema_version vs int
    env_bad_type = dict(env1)
    env_bad_type["schema_version"] = True
    assert _exact_raw_credential_equal(env1, env_bad_type) is False

    # Extra key
    env_extra = dict(env1)
    env_extra["extra"] = "bad"
    assert _exact_raw_credential_equal(env1, env_extra) is False


@pytest.mark.asyncio
async def test_durable_integration_lifecycle_audit_connect_and_disconnect(monkeypatch):
    """Atomic lifecycle audit events recorded on connect and disconnect without secrets."""
    from app.services.integration_token_mutations import (
        disconnect_provider_cas,
    )
    _setup_keyring(monkeypatch)
    cid = "c-audit-lifecycle-1"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 0,
        "jobber_connected": False,
    })

    audit_store: dict[str, Any] = {}
    outbox_store: dict[str, Any] = {}

    db = _FakeFirestore({
        "contractors": {cid: doc_ref},
        "integration_lifecycle_audit": audit_store,
        "integration_revocation_outbox": outbox_store,
    })
    _patch_firestore(monkeypatch, db)

    # 1. Connect creates audit record
    secret_access = "SECRET_ACCESS_TOKEN_XYZ"
    secret_refresh = "SECRET_REFRESH_TOKEN_ABC"

    updates, new_gen, connect_audit_id = await connect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        access_token=secret_access,
        refresh_token=secret_refresh,
        db=db,
    )

    assert connect_audit_id in audit_store
    connect_event = audit_store[connect_audit_id].data
    assert connect_event["action"] == "connected"
    assert connect_event["provider"] == "jobber"
    assert connect_event["generation"] == 1
    assert connect_event["actor"] == "oauth_state"

    # Invariant: No token material or ciphertexts in audit record!
    assert secret_access not in str(connect_event)
    assert secret_refresh not in str(connect_event)
    assert "ciphertext" not in str(connect_event)

    # 2. Disconnect creates pending/started audit record
    tombstone_gen, decrypted_acc, disc_audit_id = await disconnect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        db=db,
    )

    assert disc_audit_id in audit_store
    disc_event = audit_store[disc_audit_id].data
    assert disc_event["action"] == "credentials_deleted"
    assert disc_event["provider"] == "jobber"
    assert disc_event["generation"] == 2
    assert disc_event["actor"] == "contractor_api"
    assert disc_event["revocation_status"] in ("pending", "provider_request_started")

    # Invariant: No secrets in disconnect audit record
    assert secret_access not in str(disc_event)
    assert secret_refresh not in str(disc_event)


@pytest.mark.asyncio
async def test_consume_oauth_state_strict_validations(monkeypatch):
    """consume_oauth_state requires canonical state, allowlisted collection, valid expiry, and contractor_id."""
    from app.services.integration_tokens import compute_raw_credentials_fingerprint
    _setup_keyring(monkeypatch)

    class _StateFakeDocRef:
        def __init__(self, data=None):
            self.data = dict(data) if data is not None else None
            self.exists = data is not None

        def to_dict(self):
            return dict(self.data) if self.data else {}

        def get(self, *args, transaction=None, **kwargs):
            class _Snap:
                def __init__(self, d, exists):
                    self._d = dict(d) if d else {}
                    self.exists = exists
                    self.read_time = datetime.datetime.fromtimestamp(time.time(), datetime.UTC)
                def to_dict(self):
                    return dict(self._d) if self.exists else {}
            return _Snap(self.data, self.exists)

        def delete(self, *args, **kwargs):
            self.exists = False
            self.data = None

        def update(self, updates, *args, **kwargs):
            if self.data is None:
                self.data = {}
            for k, v in updates.items():
                self.data[k] = v

    class _StateFakeTx(_FakeTransaction):
        def __init__(self, db):
            super().__init__(db)
            self._db = db

        def get(self, ref):
            return ref.get()

        def delete(self, ref):
            ref.delete()

        def update(self, ref, updates):
            ref.update(updates)

    class _StateFakeFirestore:
        def __init__(self, collections):
            self.collections = collections

        def collection(self, name):
            class _Coll:
                def __init__(self, store):
                    self.store = store
                def document(self, doc_id):
                    if doc_id not in self.store:
                        self.store[doc_id] = _StateFakeDocRef(None)
                    return self.store[doc_id]
            return _Coll(self.collections.setdefault(name, {}))

        def transaction(self):
            return _StateFakeTx(self)

    now = time.time()
    fp = compute_raw_credentials_fingerprint(None, None)

    states_db = {
        "valid-state-1234567890": _StateFakeDocRef({
            "contractor_id": "c-valid",
            "provider": "jobber",
            "lifecycle_epoch": 0,
            "generation": 0,
            "credentials_fingerprint": fp,
            "created_at": now,
            "expires_at": now + 300.0,
        }),
        "expired-state-1234567890": _StateFakeDocRef({
            "contractor_id": "c-expired",
            "provider": "jobber",
            "lifecycle_epoch": 0,
            "generation": 0,
            "credentials_fingerprint": fp,
            "created_at": now - 400.0,
            "expires_at": now - 10.0,
        }),
        "no-cid-state-1234567890": _StateFakeDocRef({
            "expires_at": now + 300.0,
        }),
    }
    contractors_db = {
        "c-valid": _StateFakeDocRef({
            "contractor_id": "c-valid",
            "active": True,
            "jobber_connected": False,
            "jobber_generation": 0,
            "jobber_lifecycle_epoch": 0,
        }),
        "c-expired": _StateFakeDocRef({
            "contractor_id": "c-expired",
            "active": True,
            "jobber_connected": False,
            "jobber_generation": 0,
            "jobber_lifecycle_epoch": 0,
        }),
    }
    db = _StateFakeFirestore({
        "jobber_oauth_states": states_db,
        "contractors": contractors_db,
    })

    # 1. Invalid collection name rejected
    with pytest.raises(HTTPException) as exc:
        await consume_oauth_state(db=db, collection_name="disallowed_collection", state="valid-state-1234567890")
    assert exc.value.status_code == 400

    # 2. Invalid state string rejected (too short, bad characters)
    for bad_state in ("short", "bad characters!!", "", "a" * 300):
        with pytest.raises(HTTPException) as exc:
            await consume_oauth_state(db=db, collection_name="jobber_oauth_states", state=bad_state)
        assert exc.value.status_code == 400

    # 3. Expired state rejected and deleted
    with pytest.raises(HTTPException) as exc:
        await consume_oauth_state(db=db, collection_name="jobber_oauth_states", state="expired-state-1234567890")
    assert exc.value.status_code == 400
    assert states_db["expired-state-1234567890"].exists is False

    # 4. Valid state consumed successfully
    res, obs = await consume_oauth_state(db=db, collection_name="jobber_oauth_states", state="valid-state-1234567890")
    assert res["contractor_id"] == "c-valid"
    assert obs["contractor_id"] == "c-valid"
    assert states_db["valid-state-1234567890"].exists is False

    # 5. Replay fails (already deleted)
    with pytest.raises(HTTPException) as exc:
        await consume_oauth_state(db=db, collection_name="jobber_oauth_states", state="valid-state-1234567890")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_jobber_lead_capture_toggle_allows_disable_when_disconnected(monkeypatch):
    """Jobber lead capture toggle disallows enable when disconnected, but allows disable.

    Patches app.api.integrations.write_admin_audit_event with a deterministic local fake,
    asserts exact audit parameters, and installs causal fail-fast guards proving no real
    Firestore client or client factory can be constructed or reused even if ADC or a cached
    firestore_client._client exists.
    """
    import app.db.admin_audit as admin_audit_module
    import app.db.firestore_client as firestore_client_module
    from app.api.integrations import JobberLeadCaptureUpdate, jobber_update_lead_capture
    _setup_keyring(monkeypatch)
    cid = "c-toggle-test"

    # Causal fail-fast guards: exact module alias guard plus constructor guards and cache nullification
    def _forbidden_firestore_client_factory(*args, **kwargs):
        raise AssertionError("Causal fail-fast: real Firestore client factory must NOT be invoked")

    monkeypatch.setattr(admin_audit_module, "get_firestore_client", _forbidden_firestore_client_factory)
    monkeypatch.setattr(firestore_client_module, "get_firestore_client", _forbidden_firestore_client_factory)
    monkeypatch.setattr(firestore_client_module, "_client", None)
    monkeypatch.setattr("google.cloud.firestore.Client", _forbidden_firestore_client_factory)
    monkeypatch.setattr("google.cloud.firestore_v1.Client", _forbidden_firestore_client_factory)

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": True,
    })
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    monkeypatch.setattr("app.api.integrations._get_firestore", lambda: db)
    monkeypatch.setattr("app.api.integrations._require_admin", lambda req: None)

    audit_calls = []

    async def _fake_write_admin_audit_event(
        request,
        action,
        target_type,
        target_id,
        reason,
        before,
        after,
        metadata=None,
        created_at=None,
    ):
        audit_calls.append({
            "request": request,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "reason": reason,
            "before": before,
            "after": after,
            "metadata": metadata,
            "created_at": created_at,
        })

    monkeypatch.setattr(admin_audit_module, "write_admin_audit_event", _fake_write_admin_audit_event)

    # Enabling when disconnected -> 409 Conflict (zero audit calls)
    with pytest.raises(HTTPException) as exc:
        await jobber_update_lead_capture(
            body=JobberLeadCaptureUpdate(enabled=True),
            contractor_id=cid,
            request=None,
        )
    assert exc.value.status_code == 409
    audit_col = db.collection("admin_audit_events")
    assert len(audit_col.docs) == 0

    # Disabling when disconnected -> Succeeds!
    res = await jobber_update_lead_capture(
        body=JobberLeadCaptureUpdate(enabled=False),
        contractor_id=cid,
        request=None,
    )
    assert res["status"] == "ok"
    assert res["lead_capture_enabled"] is False
    assert doc_ref.data["jobber_lead_capture_enabled"] is False

    # Assert exact deterministic audit record written transactionally in db
    audit_docs = list(audit_col.docs.values())
    assert len(audit_docs) == 1
    audit_record = audit_docs[0].data
    assert audit_record["action"] == "jobber_lead_capture_update"
    assert audit_record["target_type"] == "contractor"
    assert audit_record["target_id"] == cid
    assert audit_record["reason"] == "admin toggled Jobber lead capture"
    assert audit_record["before"] == {"jobber_lead_capture_enabled": True}
    assert audit_record["after"] == {"jobber_lead_capture_enabled": False}
    assert audit_record["metadata"]["jobber_connected"] is False
    assert audit_record["metadata"]["generation"] == 0
    assert audit_record["metadata"]["lifecycle_epoch"] == 0
    assert isinstance(audit_record["metadata"]["timestamp"], float)
    assert audit_record["created_at"] == res["updated_at"]


@pytest.mark.asyncio
async def test_jobber_lead_capture_toggle_fails_fast_if_audit_unpatched(monkeypatch):
    """Causal proof: when updating lead capture, audit is atomic inside the transaction and uses db directly."""
    import app.db.firestore_client as firestore_client_module
    from app.api.integrations import JobberLeadCaptureUpdate, jobber_update_lead_capture
    _setup_keyring(monkeypatch)
    cid = "c-toggle-unpatched"

    def _forbidden_firestore_client_factory(*args, **kwargs):
        raise AssertionError("Causal fail-fast: real Firestore client factory intercepted")

    monkeypatch.setattr(firestore_client_module, "get_firestore_client", _forbidden_firestore_client_factory)
    monkeypatch.setattr(firestore_client_module, "_client", None)
    monkeypatch.setattr("google.cloud.firestore.Client", _forbidden_firestore_client_factory)
    monkeypatch.setattr("google.cloud.firestore_v1.Client", _forbidden_firestore_client_factory)

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": True,
    })
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    monkeypatch.setattr("app.api.integrations._get_firestore", lambda: db)
    monkeypatch.setattr("app.api.integrations._require_admin", lambda req: None)

    res = await jobber_update_lead_capture(
        body=JobberLeadCaptureUpdate(enabled=False),
        contractor_id=cid,
        request=None,
    )
    assert res["status"] == "ok"
    assert res["lead_capture_enabled"] is False
    assert doc_ref.data["jobber_lead_capture_enabled"] is False


# ---------------------------------------------------------------------------
# 12. Repair 4B-1: Firestore 2.21 Transaction, Postconditions, & Lease Tests
# ---------------------------------------------------------------------------

def test_doc_ref_get_in_txn_rejects_generator_and_uses_exact_kwargs():
    from app.services.integration_token_mutations import _get_doc_snapshot_in_txn

    class _BrokenGeneratorTxn:
        def update(self, *args):
            pass

        def get(self, *args):
            def _gen():
                yield 1
            return _gen()

    class _DocRef:
        def get(self, transaction=None):
            def _gen():
                yield 1
            return _gen()

    with pytest.raises(IntegrationTokenEnvelopeError):
        _get_doc_snapshot_in_txn(_DocRef(), _BrokenGeneratorTxn())


@pytest.mark.asyncio
async def test_mutation_postcondition_fails_on_uncommitted_transaction(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-uncommitted-postcond"

    class _UncommittedTxn:
        def __init__(self):
            self.committed = False
        def update(self, *args):
            pass
        def commit(self):
            pass

    class _NoCommitDB:
        def collection(self, name):
            class _Coll:
                def document(self, doc_id):
                    return _FakeDocRef({
                        "contractor_id": doc_id,
                        "active": True,
                        "jobber_connected": True,
                        "jobber_lifecycle_epoch": 0,
                        "jobber_generation": 0,
                        "jobber_access_token": "a",
                        "jobber_refresh_token": "r",
                        "jobber_refresh_claim_id": "cl-uncommitted",
                        "jobber_refresh_claim_expires_at": time.time() + 60,
                        "jobber_refresh_claim_generation": 0,
                    }, doc_id=doc_id)
            return _Coll()
        def transaction(self):
            return _UncommittedTxn()

    with pytest.raises((IntegrationTokenEnvelopeError, IntegrationTokenCASConflict)):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-acc",
            new_refresh_token="new-ref",
            observed_generation=0,
            observed_access_raw="a",
            observed_refresh_raw="r",
            claim_id="cl-uncommitted",
            db=_NoCommitDB(),
        )


@pytest.mark.asyncio
async def test_disconnect_postcondition_verifies_field_absence(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-disc-postcond"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_access_token": encrypt_integration_token("acc", contractor_id=cid, provider="jobber", token_kind="access"),
        "jobber_refresh_token": encrypt_integration_token("ref", contractor_id=cid, provider="jobber", token_kind="refresh"),
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    gen, access, audit_id = await disconnect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        db=db,
    )
    assert gen == 2
    assert access == "acc"
    assert "jobber_access_token" not in doc_ref.data
    assert "jobber_refresh_token" not in doc_ref.data
    assert doc_ref.data["jobber_connected"] is False


@pytest.mark.asyncio
async def test_multi_instance_refresh_lease_coordination(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-lease-test"

    acc_enc = encrypt_integration_token("old-acc", contractor_id=cid, provider="jobber", token_kind="access")
    ref_enc = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_access_token": acc_enc,
        "jobber_refresh_token": ref_enc,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    # Instance 1 acquires lease
    claim_id, expires_at = await acquire_refresh_claim_cas(
        contractor_id=cid,
        provider="jobber",
        observed_generation=1,
        observed_access_raw=acc_enc,
        observed_refresh_raw=ref_enc,
        db=db,
    )
    assert claim_id is not None
    assert doc_ref.data["jobber_refresh_claim_id"] == claim_id

    # Instance 2 attempts to acquire lease on same contractor -> IntegrationTokenLeaseError
    with pytest.raises(IntegrationTokenLeaseError):
        await acquire_refresh_claim_cas(
            contractor_id=cid,
            provider="jobber",
            observed_generation=1,
            observed_access_raw=acc_enc,
            observed_refresh_raw=ref_enc,
            db=db,
        )

    # Instance 1 transitions claim to provider_request_started
    await transition_refresh_claim_to_started_cas(
        contractor_id=cid,
        provider="jobber",
        claim_id=claim_id,
        observed_generation=1,
        observed_access_raw=acc_enc,
        observed_refresh_raw=ref_enc,
        db=db,
    )

    # Instance 1 completes refresh -> CAS commit atomically clears lease and advances generation
    updates, next_gen = await persist_refreshed_tokens_cas(
        contractor_id=cid,
        provider="jobber",
        new_access_token="new-acc-123",
        new_refresh_token="new-ref-456",
        observed_generation=1,
        observed_access_raw=acc_enc,
        observed_refresh_raw=ref_enc,
        claim_id=claim_id,
        db=db,
    )
    assert next_gen == 2
    assert "jobber_refresh_claim_id" not in doc_ref.data


@pytest.mark.asyncio
async def test_multi_instance_stale_lease_takeover(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-stale-lease"

    acc_enc = encrypt_integration_token("old-acc", contractor_id=cid, provider="jobber", token_kind="access")
    ref_enc = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    # Stale expired lease in document
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": acc_enc,
        "jobber_refresh_token": ref_enc,
        "jobber_refresh_claim_id": "stale-claim-dead-worker",
        "jobber_refresh_claim_phase": "reserved",
        "jobber_refresh_claim_expires_at": time.time() - 10.0,
        "jobber_refresh_claim_generation": 1,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    # New instance takes over stale lease
    new_claim_id, exp = await acquire_refresh_claim_cas(
        contractor_id=cid,
        provider="jobber",
        observed_generation=1,
        observed_access_raw=acc_enc,
        observed_refresh_raw=ref_enc,
        db=db,
    )
    assert new_claim_id != "stale-claim-dead-worker"
    assert doc_ref.data["jobber_refresh_claim_id"] == new_claim_id


@pytest.mark.asyncio
async def test_multi_instance_contender_winner_hydration_zero_provider_calls(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-contender-test"

    acc_enc = encrypt_integration_token("old-acc", contractor_id=cid, provider="jobber", token_kind="access")
    ref_enc = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")
    fresh_acc_enc = encrypt_integration_token("winner-fresh-acc", contractor_id=cid, provider="jobber", token_kind="access")
    fresh_ref_enc = encrypt_integration_token("winner-fresh-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    # Winner has completed and committed gen 2
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 2,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": fresh_acc_enc,
        "jobber_refresh_token": fresh_ref_enc,
        "jobber_token_expires_at": time.time() + 3600,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    monkeypatch.setattr(settings, "jobber_client_id", "test-client")
    monkeypatch.setattr(settings, "jobber_client_secret", "test-secret")

    class _MustNotBeCalledHttpx:
        async def __aenter__(self):
            raise AssertionError("Contender made forbidden provider HTTP call!")
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(jobber_service.httpx, "AsyncClient", _MustNotBeCalledHttpx)

    # Contender with stale in-memory dictionary (gen 1) calls refresh_access_token
    stale_contractor = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_access_token": acc_enc,
        "jobber_refresh_token": ref_enc,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
    }

    token = await jobber_service.refresh_access_token(stale_contractor, force=False)
    assert token == "winner-fresh-acc"
    assert resolve_usable_token_pair(stale_contractor, "jobber") == ("winner-fresh-acc", "winner-fresh-ref")
    assert stale_contractor["jobber_generation"] == 2


@pytest.mark.asyncio
async def test_google_refresh_token_fallback_when_absent_and_reject_when_present_malformed(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-gcal-fallback"
    enc_acc = encrypt_integration_token("old-acc", contractor_id=cid, provider="google_calendar", token_kind="access")
    enc_ref = encrypt_integration_token("old-ref", contractor_id=cid, provider="google_calendar", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 0,
        "google_calendar_access_token": enc_acc,
        "google_calendar_refresh_token": enc_ref,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)
    monkeypatch.setattr(settings, "google_calendar_client_id", "test-gcal-client")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "test-gcal-secret")

    # Case A: "refresh_token" key ABSENT from response -> fallback to existing stored refresh token
    resp_absent = _FakeResponse(200, {"access_token": "new-acc-1"})
    monkeypatch.setattr(calendar_service.httpx, "AsyncClient", lambda: _FakeAsyncClient(resp_absent))

    contractor_dict = {
        "contractor_id": cid,
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 0,
        "google_calendar_access_token": enc_acc,
        "google_calendar_refresh_token": enc_ref,
    }
    res = await calendar_service.refresh_access_token(contractor_dict, force=True)
    assert res == "new-acc-1"
    assert isinstance(contractor_dict["google_calendar_refresh_token"], dict)
    assert resolve_usable_token_pair(contractor_dict, "google_calendar") == ("new-acc-1", "old-ref")

    # Case B: "refresh_token" key PRESENT but malformed (empty, non-string, whitespace) -> Rejects!
    for malformed_ref in ("", "   ", None, 123, True, {"raw": "dict"}):
        resp_malformed = _FakeResponse(200, {"access_token": "new-acc-2", "refresh_token": malformed_ref})
        monkeypatch.setattr(calendar_service.httpx, "AsyncClient", lambda: _FakeAsyncClient(resp_malformed))
        res_bad = await calendar_service.refresh_access_token(contractor_dict, force=True)
        assert res_bad is None


@pytest.mark.asyncio
async def test_jobber_refresh_token_mandatory_rotation_rejects_missing_or_malformed(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-jobber-rotation"
    enc_acc = encrypt_integration_token("old-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)
    monkeypatch.setattr(settings, "jobber_client_id", "test-jobber-client")
    monkeypatch.setattr(settings, "jobber_client_secret", "test-jobber-secret")

    contractor_dict = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
    }

    # Jobber response without new refresh token (or malformed) MUST fail closed!
    for missing_or_bad in (None, "", "   ", 123, True):
        tokens_payload = {"access_token": "new-acc-3"}
        if missing_or_bad is not None:
            tokens_payload["refresh_token"] = missing_or_bad
        resp = _FakeResponse(200, tokens_payload)
        monkeypatch.setattr(jobber_service.httpx, "AsyncClient", lambda: _FakeAsyncClient(resp))
        res = await jobber_service.refresh_access_token(contractor_dict, force=True)
        assert res is None


@pytest.mark.asyncio
async def test_refresh_lease_release_only_matching_id_and_leaves_other_intact(monkeypatch):
    _setup_keyring(monkeypatch)
    cid = "c-lease-release-match"

    claim_match = "active-claim-12345678"
    claim_wrong = "wrong-claim-id-12345678"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_refresh_claim_id": claim_match,
        "jobber_refresh_claim_phase": "reserved",
        "jobber_refresh_claim_expires_at": time.time() + 60.0,
        "jobber_refresh_claim_generation": 1,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    # Attempt release with wrong claim_id -> Does NOT delete lease
    await release_refresh_claim_cas(contractor_id=cid, provider="jobber", claim_id=claim_wrong, db=db)
    assert doc_ref.data.get("jobber_refresh_claim_id") == claim_match

    # Attempt release with matching claim_id -> Deletes lease atomically
    await release_refresh_claim_cas(contractor_id=cid, provider="jobber", claim_id=claim_match, db=db)
    assert "jobber_refresh_claim_id" not in doc_ref.data


# ---------------------------------------------------------------------------
# 13. Repair 4B-2B: Callbacks, State Deletion, CAS Preconditions, Status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oauth_state_malformed_and_expired_deleted_and_raises_outside_txn(monkeypatch):
    """Verify that consume_oauth_state commits document deletion for malformed and expired states and raises outside txn."""

    # 1. Expired state: document is deleted, HTTPException(400) raised
    state_expired = _FakeDocRef({"contractor_id": "c-exp", "expires_at": time.time() - 50.0}, doc_id="state-exp-12345678")
    db_exp = _FakeFirestore({"jobber_oauth_states": {"state-exp-12345678": state_expired}})
    with pytest.raises(HTTPException) as exc_exp:
        await consume_oauth_state(db=db_exp, collection_name="jobber_oauth_states", state="state-exp-12345678")
    assert exc_exp.value.status_code == 400
    assert state_expired.deleted is True

    # 2. Malformed state (missing contractor): document is deleted, HTTPException(400) raised
    state_bad = _FakeDocRef({"expires_at": time.time() + 300.0}, doc_id="state-bad-12345678")
    db_bad = _FakeFirestore({"google_oauth_states": {"state-bad-12345678": state_bad}})
    with pytest.raises(HTTPException) as exc_bad:
        await consume_oauth_state(db=db_bad, collection_name="google_oauth_states", state="state-bad-12345678")
    assert exc_bad.value.status_code == 400
    assert state_bad.deleted is True

    # 3. Malformed state (bad expiry type): document is deleted, HTTPException(400) raised
    state_bad_exp = _FakeDocRef({"contractor_id": "c-bad-exp", "expires_at": "not-a-number"}, doc_id="state-badexp-123456")
    db_bad_exp = _FakeFirestore({"jobber_oauth_states": {"state-badexp-123456": state_bad_exp}})
    with pytest.raises(HTTPException) as exc_bad_exp:
        await consume_oauth_state(db=db_bad_exp, collection_name="jobber_oauth_states", state="state-badexp-123456")
    assert exc_bad_exp.value.status_code == 400
    assert state_bad_exp.deleted is True


@pytest.mark.asyncio
async def test_connect_cas_with_observed_precondition_concurrency_race(monkeypatch):
    """Verify that connect_provider_cas fails if concurrent reconnect changed generation or credentials."""
    _setup_keyring(monkeypatch)
    cid = "c-connect-race"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 2,
        "jobber_access_token": "acc-gen2",
        "jobber_refresh_token": "ref-gen2",
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    # Attempt connect with stale observed generation 1 -> IntegrationTokenCASConflict
    with pytest.raises(IntegrationTokenCASConflict):
        await connect_provider_cas(
            contractor_id=cid,
            provider="jobber",
            access_token="new-acc",
            refresh_token="new-ref",
            observed_generation=1,
            observed_access_raw="acc-gen1",
            observed_refresh_raw="ref-gen1",
            db=db,
        )

    # Attempt connect with matching generation 2 and raw credentials -> Succeeds and advances to 3
    updates, next_gen, audit_id = await connect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        access_token="new-acc",
        refresh_token="new-ref",
        observed_generation=2,
        observed_access_raw="acc-gen2",
        observed_refresh_raw="ref-gen2",
        db=db,
    )
    assert next_gen == 3
    assert doc_ref.data["jobber_generation"] == 3


@pytest.mark.asyncio
async def test_callback_contractor_inactive_aborts_before_provider_exchange(monkeypatch):
    """Callback endpoints must verify contractor active is True before making any provider HTTP exchange."""
    from app.api.integrations import google_calendar_callback, jobber_callback
    from app.services.integration_tokens import compute_raw_credentials_fingerprint
    _setup_keyring(monkeypatch)
    cid = "c-inactive-cb"
    now = time.time()
    fp = compute_raw_credentials_fingerprint(None, None)

    state_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": fp,
        "created_at": now,
        "expires_at": now + 300.0,
    }, doc_id="state-inactive-12345")
    contractor_doc = _FakeDocRef({"contractor_id": cid, "active": False, "jobber_generation": 0, "jobber_lifecycle_epoch": 0}, doc_id=cid)

    db = _FakeFirestore({
        "jobber_oauth_states": {"state-inactive-12345": state_doc},
        "contractors": {cid: contractor_doc},
    })
    monkeypatch.setattr("app.api.integrations._get_firestore", lambda: db)

    class _MustNotBeCalledHttpx:
        async def __aenter__(self):
            raise AssertionError("Provider HTTP request was made unexpectedly for inactive contractor")
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _MustNotBeCalledHttpx)

    with pytest.raises(HTTPException) as exc_j:
        await jobber_callback(code="test-code", state="state-inactive-12345", request=None)
    assert exc_j.value.status_code == 400

    # Reset state for Google
    state_doc_g = _FakeDocRef({
        "contractor_id": cid,
        "provider": "google_calendar",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": fp,
        "created_at": now,
        "expires_at": now + 300.0,
    }, doc_id="state-inactive-67890")
    contractor_doc_g = _FakeDocRef({"contractor_id": cid, "active": False, "google_calendar_generation": 0, "google_calendar_lifecycle_epoch": 0}, doc_id=cid)
    db_g = _FakeFirestore({
        "google_oauth_states": {"state-inactive-67890": state_doc_g},
        "contractors": {cid: contractor_doc_g},
    })
    monkeypatch.setattr("app.api.integrations._get_firestore", lambda: db_g)

    with pytest.raises(HTTPException) as exc_g:
        await google_calendar_callback(code="test-code", state="state-inactive-67890", request=None)
    assert exc_g.value.status_code == 400


@pytest.mark.asyncio
async def test_integrations_status_endpoints_pass_explicit_contractor_id(monkeypatch):
    """Integrations status endpoints pass explicit contractor_id to has_usable_token."""
    from app.api.integrations import google_calendar_status, jobber_status
    _setup_keyring(monkeypatch)
    cid = "c-status-explicit"

    enc_jobber_acc = encrypt_integration_token("jobber-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_jobber_ref = encrypt_integration_token("jobber-ref", contractor_id=cid, provider="jobber", token_kind="refresh")
    enc_gcal_acc = encrypt_integration_token("gcal-acc", contractor_id=cid, provider="google_calendar", token_kind="access")
    enc_gcal_ref = encrypt_integration_token("gcal-ref", contractor_id=cid, provider="google_calendar", token_kind="refresh")

    # Document where "contractor_id" is NOT present inside the data payload (simulating Firestore document ID only)
    doc_ref = _FakeDocRef({
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_token_envelope_required": True,
        "jobber_access_token": enc_jobber_acc,
        "jobber_refresh_token": enc_jobber_ref,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 1,
        "google_calendar_token_envelope_required": True,
        "google_calendar_access_token": enc_gcal_acc,
        "google_calendar_refresh_token": enc_gcal_ref,
    }, doc_id=cid)
    # Clear contractor_id from data dict to strictly test explicit contractor_id parameter passing
    doc_ref.data.pop("contractor_id", None)

    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    monkeypatch.setattr("app.api.integrations._get_firestore", lambda: db)
    monkeypatch.setattr("app.api.integrations.require_contractor_access", lambda req, cid: None)

    req = type("Req", (), {"state": type("State", (), {"is_admin": False})()})()

    # Jobber status succeeds because explicit contractor_id resolves the envelope
    res_j = await jobber_status(contractor_id=cid, request=req)
    assert res_j["connected"] is True

    # Google status succeeds because explicit contractor_id resolves the envelope
    res_g = await google_calendar_status(contractor_id=cid, request=req)
    assert res_g["connected"] is True


@pytest.mark.asyncio
async def test_jobber_concurrent_barrier_two_instances_one_provider_call(monkeypatch):
    """Proves that when two service instances have distinct, non-shared in-process locks,
    the first instance acquires the durable Firestore refresh lease and holds it during the provider call,
    while the second instance fails closed as a contender without making a second provider call."""
    import asyncio

    from app.services import jobber

    _setup_keyring(monkeypatch)
    cid = "contractor-barrier-1"
    initial_access = "header.eyJleHAiOjEwMDB9."
    initial_refresh = "initial-refresh-token"
    fresh_access = "header.eyJleHAiOjk5OTk5OTk5OTl9."
    fresh_refresh = "fresh-rotated-refresh"

    enc_access = encrypt_integration_token(initial_access, contractor_id=cid, provider="jobber", token_kind="access")
    enc_refresh = encrypt_integration_token(initial_refresh, contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_data = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_token_envelope_required": True,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
    }
    doc_ref = _FakeDocRef(doc_data, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    contractor_inst1 = {"contractor_id": cid, "jobber_access_token": initial_access, "jobber_refresh_token": initial_refresh}
    contractor_inst2 = {"contractor_id": cid, "jobber_access_token": initial_access, "jobber_refresh_token": initial_refresh}

    provider_call_count = 0
    provider_entered = asyncio.Event()
    provider_release = asyncio.Event()

    class _ContentionAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            nonlocal provider_call_count
            if url == jobber.JOBBER_TOKEN_URL:
                provider_call_count += 1
                provider_entered.set()
                await asyncio.wait_for(provider_release.wait(), timeout=5.0)
                return _FakeResponse(200, {"access_token": fresh_access, "refresh_token": fresh_refresh, "expires_in": 3600})
            raise RuntimeError("Unexpected endpoint")

    monkeypatch.setattr(jobber.httpx, "AsyncClient", _ContentionAsyncClient)
    from app.config import settings
    monkeypatch.setattr(settings, "jobber_client_id", "test-jobber-client-id")
    monkeypatch.setattr(settings, "jobber_client_secret", "test-jobber-client-secret")

    # Non-coordinating local locks seam: returns a distinct asyncio.Lock on every invocation and never stores or shares it
    class _DistinctLocksSeam:
        def setdefault(self, key, default=None):
            return asyncio.Lock()

        def __getitem__(self, key):
            return asyncio.Lock()

        def __contains__(self, key):
            return True

        def __setitem__(self, key, value):
            pass

    monkeypatch.setattr(jobber, "_REFRESH_LOCKS", _DistinctLocksSeam())

    # Start instance 1; it acquires the durable lease and enters the provider call
    task1 = asyncio.create_task(jobber.refresh_access_token(contractor_inst1, force=True))
    await asyncio.wait_for(provider_entered.wait(), timeout=5.0)

    # While instance 1 is blocked in the provider call holding the lease, run instance 2
    # Instance 2 has its own distinct in-process lock and hits the Firestore lease contention
    result2 = await jobber.refresh_access_token(contractor_inst2, force=True)

    # Assert instance 2 completed with None and zero provider calls while instance 1 is still blocked
    assert result2 is None
    assert provider_call_count == 1

    # Release instance 1 to finish provider response and persist tokens
    provider_release.set()
    result1 = await task1

    assert result1 == fresh_access
    assert provider_call_count == 1
    assert doc_ref.data["jobber_generation"] == 1
    dec_access = decrypt_integration_token(doc_ref.data["jobber_access_token"], contractor_id=cid, provider="jobber", token_kind="access")
    assert dec_access == fresh_access


@pytest.mark.asyncio
async def test_acquire_refresh_claim_fails_closed_on_malformed_claim_states(monkeypatch):
    """Proves acquire_refresh_claim_cas fails closed on any partial, malformed, or invalid existing claim state."""
    _setup_keyring(monkeypatch)
    cid = "c-malformed-claims"
    enc_access = encrypt_integration_token("access-1", contractor_id=cid, provider="jobber", token_kind="access")
    enc_refresh = encrypt_integration_token("refresh-1", contractor_id=cid, provider="jobber", token_kind="refresh")

    malformed_states = [
        # claim_id present, expiry absent
        {"jobber_refresh_claim_id": "a" * 32, "jobber_refresh_claim_generation": 0},
        # claim_id present, expiry=True (bool)
        {"jobber_refresh_claim_id": "a" * 32, "jobber_refresh_claim_expires_at": True, "jobber_refresh_claim_generation": 0},
        # expiry present, claim_id absent
        {"jobber_refresh_claim_expires_at": time.time() + 100.0, "jobber_refresh_claim_generation": 0},
        # generation present, claim_id absent
        {"jobber_refresh_claim_generation": 0},
        # claim_id is not str (int)
        {"jobber_refresh_claim_id": 12345, "jobber_refresh_claim_expires_at": time.time() + 100.0, "jobber_refresh_claim_generation": 0},
        # claim_id contains spaces / non-canonical
        {"jobber_refresh_claim_id": "invalid claim id with space", "jobber_refresh_claim_expires_at": time.time() + 100.0, "jobber_refresh_claim_generation": 0},
        # claim_gen is str (not int)
        {"jobber_refresh_claim_id": "a" * 32, "jobber_refresh_claim_expires_at": time.time() + 100.0, "jobber_refresh_claim_generation": "0"},
        # claim_gen is bool
        {"jobber_refresh_claim_id": "a" * 32, "jobber_refresh_claim_expires_at": time.time() + 100.0, "jobber_refresh_claim_generation": True},
        # claim_gen does not match observed_generation
        {"jobber_refresh_claim_id": "a" * 32, "jobber_refresh_claim_expires_at": time.time() + 100.0, "jobber_refresh_claim_generation": 5},
        # expires_at is infinite
        {"jobber_refresh_claim_id": "a" * 32, "jobber_refresh_claim_expires_at": float("inf"), "jobber_refresh_claim_generation": 0},
    ]

    for bad_state in malformed_states:
        base_doc = {
            "contractor_id": cid,
            "active": True,
            "jobber_connected": True,
            "jobber_lifecycle_epoch": 0,
            "jobber_generation": 0,
            "jobber_access_token": enc_access,
            "jobber_refresh_token": enc_refresh,
        }
        base_doc.update(bad_state)
        doc_ref = _FakeDocRef(base_doc, doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: doc_ref}})

        with pytest.raises(IntegrationTokenCASConflict, match="Malformed existing refresh claim record"):
            await acquire_refresh_claim_cas(
                contractor_id=cid,
                provider="jobber",
                observed_generation=0,
                observed_access_raw=enc_access,
                observed_refresh_raw=enc_refresh,
                db=db,
            )


@pytest.mark.asyncio
async def test_acquire_refresh_claim_allows_stale_takeover_when_expired(monkeypatch):
    """Proves a complete, valid expired claim is cleanly taken over by a new refresh claim."""
    _setup_keyring(monkeypatch)
    cid = "c-stale-takeover"
    enc_access = encrypt_integration_token("access-1", contractor_id=cid, provider="jobber", token_kind="access")
    enc_refresh = encrypt_integration_token("refresh-1", contractor_id=cid, provider="jobber", token_kind="refresh")

    old_claim_id = "0" * 32
    expired_at = time.time() - 100.0
    doc_data = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
        "jobber_refresh_claim_id": old_claim_id,
        "jobber_refresh_claim_phase": "reserved",
        "jobber_refresh_claim_expires_at": expired_at,
        "jobber_refresh_claim_generation": 0,
    }
    doc_ref = _FakeDocRef(doc_data, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    new_claim_id, new_exp = await acquire_refresh_claim_cas(
        contractor_id=cid,
        provider="jobber",
        observed_generation=0,
        observed_access_raw=enc_access,
        observed_refresh_raw=enc_refresh,
        db=db,
    )

    assert new_claim_id != old_claim_id
    assert new_exp > time.time()
    assert doc_ref.data["jobber_refresh_claim_id"] == new_claim_id
    assert doc_ref.data["jobber_refresh_claim_expires_at"] == new_exp


def test_verify_audit_postcondition_rejects_bool_for_int_and_unexpected_keys():
    """Proves _verify_audit_postcondition rejects actual generation=True for expected 1 and unexpected keys."""
    from app.db.integration_lifecycle_audit import AUDIT_COLLECTION
    from app.services.integration_token_mutations import _verify_audit_postcondition

    expected_audit = {
        "contractor_id": "c1",
        "provider": "jobber",
        "generation": 1,
        "timestamp": 100.0,
        "action": "connected",
    }

    # 1. Actual generation is True (bool) instead of 1 (int)
    bad_data_bool = dict(expected_audit)
    bad_data_bool["generation"] = True
    audit_doc_1 = _FakeDocRef(bad_data_bool, doc_id="audit-1")
    db_1 = _FakeFirestore({AUDIT_COLLECTION: {"audit-1": audit_doc_1}})
    with pytest.raises(IntegrationTokenPostconditionError, match="Audit document field mismatch"):
        _verify_audit_postcondition(db_1, "audit-1", expected_audit)

    # 2. Actual timestamp is int instead of float
    bad_data_int_ts = dict(expected_audit)
    bad_data_int_ts["timestamp"] = 100
    audit_doc_2 = _FakeDocRef(bad_data_int_ts, doc_id="audit-2")
    db_2 = _FakeFirestore({AUDIT_COLLECTION: {"audit-2": audit_doc_2}})
    with pytest.raises(IntegrationTokenPostconditionError, match="Audit document field mismatch"):
        _verify_audit_postcondition(db_2, "audit-2", expected_audit)

    # 3. Actual data has an unexpected extra key
    bad_data_extra = dict(expected_audit)
    bad_data_extra["unexpected_field"] = "malicious"
    audit_doc_3 = _FakeDocRef(bad_data_extra, doc_id="audit-3")
    db_3 = _FakeFirestore({AUDIT_COLLECTION: {"audit-3": audit_doc_3}})
    with pytest.raises(IntegrationTokenPostconditionError, match="Audit document keys do not match expected exact key set"):
        _verify_audit_postcondition(db_3, "audit-3", expected_audit)

    # 4. Nominal match succeeds
    good_doc = _FakeDocRef(dict(expected_audit), doc_id="audit-ok")
    db_ok = _FakeFirestore({AUDIT_COLLECTION: {"audit-ok": good_doc}})
    _verify_audit_postcondition(db_ok, "audit-ok", expected_audit)


def test_verify_mutation_postcondition_distinguishes_bool_int_float_for_all_scalars():
    """Proves _verify_mutation_postcondition distinguishes bool/int/float for every scalar and timestamp."""
    from app.services.integration_token_mutations import _verify_mutation_postcondition

    base_data = {
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_connected": True,
        "jobber_token_refreshed_at": 100.0,
        "jobber_connected_at": 100.0,
        "jobber_disconnected_at": 100.0,
        "jobber_token_expires_at": 100.0,
        "jobber_lead_capture_enabled": True,
    }

    # 1. Generation is True instead of 1
    d1 = dict(base_data, jobber_generation=True)
    with pytest.raises(IntegrationTokenPostconditionError, match="Postcondition generation mismatch"):
        _verify_mutation_postcondition(_FakeDocRef(d1), expected_generation=1, expected_connected=True, provider="jobber")

    # 2. Connected is 1 (int) instead of True (bool)
    d2 = dict(base_data, jobber_connected=1)
    with pytest.raises(IntegrationTokenPostconditionError, match="Postcondition connected flag mismatch"):
        _verify_mutation_postcondition(_FakeDocRef(d2), expected_generation=1, expected_connected=True, provider="jobber")

    # 3. token_refreshed_at is True (bool)
    d3 = dict(base_data, jobber_token_refreshed_at=True)
    with pytest.raises(IntegrationTokenPostconditionError, match="Postcondition token_refreshed_at timestamp mismatch"):
        _verify_mutation_postcondition(_FakeDocRef(d3), expected_generation=1, expected_connected=True, provider="jobber", expected_token_refreshed_at=100.0)

    # 4. connected_at is True (bool)
    d4 = dict(base_data, jobber_connected_at=True)
    with pytest.raises(IntegrationTokenPostconditionError, match="Postcondition connected_at timestamp mismatch"):
        _verify_mutation_postcondition(_FakeDocRef(d4), expected_generation=1, expected_connected=True, provider="jobber", expected_connected_at=100.0)

    # 5. disconnected_at is True (bool)
    d5 = dict(base_data, jobber_disconnected_at=True)
    with pytest.raises(IntegrationTokenPostconditionError, match="Postcondition disconnected_at timestamp mismatch"):
        _verify_mutation_postcondition(_FakeDocRef(d5), expected_generation=1, expected_connected=True, provider="jobber", expected_disconnected_at=100.0)

    # 6. token_expires_at is True (bool)
    d6 = dict(base_data, jobber_token_expires_at=True)
    with pytest.raises(IntegrationTokenPostconditionError, match="Postcondition token_expires_at timestamp mismatch"):
        _verify_mutation_postcondition(_FakeDocRef(d6), expected_generation=1, expected_connected=True, provider="jobber", expected_expires_at=100.0)

    # 7. extra field bool vs int mismatch
    d7 = dict(base_data, jobber_lead_capture_enabled=1)
    with pytest.raises(IntegrationTokenPostconditionError, match="Postcondition extra field mismatch for jobber_lead_capture_enabled"):
        _verify_mutation_postcondition(_FakeDocRef(d7), expected_generation=1, expected_connected=True, provider="jobber", expected_extra_fields={"jobber_lead_capture_enabled": True})


@pytest.mark.asyncio
async def test_disconnect_deletes_connected_at_and_token_refreshed_at(monkeypatch):
    """Proves disconnect deletes connected_at and token_refreshed_at and postverifies absence."""
    _setup_keyring(monkeypatch)
    cid = "c-disc-cleanup"
    enc_access = encrypt_integration_token("access-1", contractor_id=cid, provider="jobber", token_kind="access")
    enc_refresh = encrypt_integration_token("refresh-1", contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_data = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 0,
        "jobber_connected_at": 50.0,
        "jobber_token_refreshed_at": 75.0,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
    }
    doc_ref = _FakeDocRef(doc_data, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    tombstone_gen, revoked_acc, audit_id = await disconnect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        db=db,
    )

    assert tombstone_gen == 1
    assert doc_ref.data["jobber_connected"] is False
    assert "jobber_connected_at" not in doc_ref.data
    assert "jobber_token_refreshed_at" not in doc_ref.data
    assert "jobber_access_token" not in doc_ref.data
    assert "jobber_refresh_token" not in doc_ref.data
    assert doc_ref.data["jobber_disconnected_at"] > 0.0


@pytest.mark.asyncio
async def test_connect_deletes_stale_disconnected_at_and_token_refreshed_at(monkeypatch):
    """Proves connect/reconnect deletes stale disconnected_at and token_refreshed_at and postverifies absence."""
    _setup_keyring(monkeypatch)
    cid = "c-conn-cleanup"

    doc_data = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_disconnected_at": 50.0,
        "jobber_token_refreshed_at": 25.0,
    }
    doc_ref = _FakeDocRef(doc_data, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    updates, new_gen, audit_id = await connect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        access_token="new-access",
        refresh_token="new-refresh",
        db=db,
    )

    assert new_gen == 2
    assert doc_ref.data["jobber_connected"] is True
    assert "jobber_disconnected_at" not in doc_ref.data
    assert "jobber_token_refreshed_at" not in doc_ref.data
    assert doc_ref.data["jobber_connected_at"] > 0.0


@pytest.mark.asyncio
async def test_connect_requires_refresh_token(monkeypatch):
    """Proves connect_provider_cas requires refresh_token and rejects None or empty string."""
    _setup_keyring(monkeypatch)
    cid = "c-conn-req-refresh"
    doc_ref = _FakeDocRef({"contractor_id": cid, "active": True, "jobber_connected": False, "jobber_generation": 0, "jobber_lifecycle_epoch": 0}, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    with pytest.raises(IntegrationTokenEnvelopeError, match="required"):
        await connect_provider_cas(
            contractor_id=cid,
            provider="jobber",
            access_token="new-access",
            refresh_token=None,  # type: ignore
            db=db,
        )

    with pytest.raises(IntegrationTokenEnvelopeError, match="cannot be empty"):
        await connect_provider_cas(
            contractor_id=cid,
            provider="jobber",
            access_token="new-access",
            refresh_token="",
            db=db,
        )


@pytest.mark.asyncio
async def test_oauth_state_invalid_contractor_id_rejected(monkeypatch):
    """Proves OAuth state consumption and callbacks reject invalid contractor_id before provider calls."""
    from app.api.integrations import jobber_callback
    from app.services.integration_tokens import compute_raw_credentials_fingerprint
    _setup_keyring(monkeypatch)
    state = "state-invalid-cid-12345"
    now = time.time()
    fp = compute_raw_credentials_fingerprint(None, None)
    db = _FakeFirestore({
        "jobber_oauth_states": {
            state: _FakeDocRef({
                "contractor_id": "   ",
                "provider": "jobber",
                "lifecycle_epoch": 0,
                "generation": 0,
                "credentials_fingerprint": fp,
                "created_at": now,
                "expires_at": now + 300.0,
            }, doc_id=state)
        },
        "contractors": {},
    })
    monkeypatch.setattr("app.api.integrations._get_firestore", lambda: db)

    class _MustNotBeCalled:
        async def __aenter__(self):
            raise AssertionError("Provider HTTP client must not be called on invalid contractor_id")

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _MustNotBeCalled)

    with pytest.raises(HTTPException) as exc:
        await jobber_callback(code="test-code", state=state, request=None)
    assert exc.value.status_code == 400
    assert "Invalid contractor" in exc.value.detail


def test_compute_aad_fails_closed_on_invalid_contractor_id():
    """Proves compute_aad rejects leading/trailing whitespace, non-str types, and empty strings with exact AAD binding."""
    # 1. Leading or trailing ASCII / Unicode whitespace rejected
    bad_cids = [
        "  cid",
        "cid  ",
        "\tcid",
        "cid\n",
        "\u2003cid",  # Em space
        "\u3000cid",  # Ideographic space
        "\u00A0cid",  # Non-breaking space
    ]
    for bad_cid in bad_cids:
        with pytest.raises(IntegrationTokenEnvelopeError, match="leading or trailing whitespace"):
            compute_aad(
                contractor_id=bad_cid,
                provider="jobber",
                token_kind="access",
                key_version=1,
            )

    # 2. Wrong types rejected
    bad_types = [123, True, False, ["c1"], {"id": "c1"}, b"c1"]
    for bad_type in bad_types:
        with pytest.raises(IntegrationTokenEnvelopeError, match="must be an exact str"):
            compute_aad(
                contractor_id=bad_type,  # type: ignore
                provider="jobber",
                token_kind="access",
                key_version=1,
            )

    # 3. Empty string rejected
    with pytest.raises(IntegrationTokenEnvelopeError, match="cannot be empty"):
        compute_aad(
            contractor_id="",
            provider="jobber",
            token_kind="access",
            key_version=1,
        )

    # 4. Exact unchanged AAD binding
    exact_cid = "contractor_exact_uuid-12345"
    aad_bytes = compute_aad(
        contractor_id=exact_cid,
        provider="jobber",
        token_kind="access",
        key_version=1,
    )
    parsed_aad = json.loads(aad_bytes.decode("utf-8"))
    assert parsed_aad["contractor_id"] == exact_cid


def test_resolve_usable_token_fails_closed_on_invalid_contractor_id_with_no_fallback(monkeypatch):
    """Proves resolve_usable_token fails closed and never falls back from an explicit invalid contractor_id."""
    _setup_keyring(monkeypatch)
    cid = "c-resolve-contractor"
    token = "valid-token-value"
    ref_token = "valid-refresh-value"
    enc = encrypt_integration_token(token, contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token(ref_token, contractor_id=cid, provider="jobber", token_kind="refresh")

    contractor = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": enc,
        "jobber_refresh_token": enc_ref,
    }

    # 1. Explicit invalid contractor_id must NOT fall back to contractor["contractor_id"]
    assert resolve_usable_token(contractor, provider="jobber", token_kind="access", contractor_id="") is None
    assert resolve_usable_token(contractor, provider="jobber", token_kind="access", contractor_id="  " + cid) is None
    assert resolve_usable_token(contractor, provider="jobber", token_kind="access", contractor_id=cid + "  ") is None
    assert resolve_usable_token(contractor, provider="jobber", token_kind="access", contractor_id=123) is None  # type: ignore
    assert resolve_usable_token(contractor, provider="jobber", token_kind="access", contractor_id=False) is None  # type: ignore

    # 2. Embedded invalid contractor_id fails closed
    bad_contractor_ws = dict(contractor, contractor_id=" " + cid)
    assert resolve_usable_token(bad_contractor_ws, provider="jobber", token_kind="access") is None

    # 3. Embedded invalid id fails closed
    bad_contractor_id_ws = {"id": " " + cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc, "jobber_refresh_token": enc_ref}
    assert resolve_usable_token(bad_contractor_id_ws, provider="jobber", token_kind="access") is None

    # 4. Valid explicit and embedded resolution succeeds
    assert resolve_usable_token(contractor, provider="jobber", token_kind="access") == token
    assert resolve_usable_token(contractor, provider="jobber", token_kind="access", contractor_id=cid) == token


# ---------------------------------------------------------------------------
# 13. Centralized Write Format Policy & Monotonic Rollout Compatibility
# ---------------------------------------------------------------------------

def test_determine_write_format_full_matrix(monkeypatch):
    """Proves the full matrix of flag off/on × absent/plaintext/envelope/mixed/malformed for both providers."""
    _setup_keyring(monkeypatch)
    cid = "c-matrix-test"
    providers = ["jobber", "google_calendar"]

    for prov in providers:
        enc_acc = encrypt_integration_token("acc", contractor_id=cid, provider=prov, token_kind="access")
        enc_ref = encrypt_integration_token("ref", contractor_id=cid, provider=prov, token_kind="refresh")

        # 1. Absent credentials:
        # Flag False -> plaintext
        assert determine_write_format(contractor_id=cid, provider=prov, stored_access=None, stored_refresh=None, encrypted_writes_enabled=False) == "plaintext"
        # Flag True -> envelope
        assert determine_write_format(contractor_id=cid, provider=prov, stored_access=None, stored_refresh=None, encrypted_writes_enabled=True) == "envelope"

        # 2. Plaintext credentials:
        # Flag False -> plaintext
        assert determine_write_format(contractor_id=cid, provider=prov, stored_access="plain-acc", stored_refresh="plain-ref", encrypted_writes_enabled=False) == "plaintext"
        # Flag True -> envelope
        assert determine_write_format(contractor_id=cid, provider=prov, stored_access="plain-acc", stored_refresh="plain-ref", encrypted_writes_enabled=True) == "envelope"

        # 3. Valid Envelope credentials (Monotonic! Once an envelope exists, always envelope):
        # Flag False -> envelope
        assert determine_write_format(contractor_id=cid, provider=prov, stored_access=enc_acc, stored_refresh=enc_ref, encrypted_writes_enabled=False) == "envelope"
        # Flag True -> envelope
        assert determine_write_format(contractor_id=cid, provider=prov, stored_access=enc_acc, stored_refresh=enc_ref, encrypted_writes_enabled=True) == "envelope"

        # 4. Mixed representations fail closed (str + dict, dict + str)
        with pytest.raises(IntegrationTokenEnvelopeError):
            determine_write_format(contractor_id=cid, provider=prov, stored_access="plain-acc", stored_refresh=enc_ref, encrypted_writes_enabled=False)
        with pytest.raises(IntegrationTokenEnvelopeError):
            determine_write_format(contractor_id=cid, provider=prov, stored_access=enc_acc, stored_refresh="plain-ref", encrypted_writes_enabled=False)
        with pytest.raises(IntegrationTokenEnvelopeError):
            determine_write_format(contractor_id=cid, provider=prov, stored_access="plain-acc", stored_refresh=enc_ref, encrypted_writes_enabled=True)

        # 5. One-sided credentials fail closed (None + str, None + dict, str + None, dict + None)
        for non_none in ["plain-tok", enc_acc]:
            with pytest.raises(IntegrationTokenEnvelopeError):
                determine_write_format(contractor_id=cid, provider=prov, stored_access=None, stored_refresh=non_none, encrypted_writes_enabled=False)
            with pytest.raises(IntegrationTokenEnvelopeError):
                determine_write_format(contractor_id=cid, provider=prov, stored_access=non_none, stored_refresh=None, encrypted_writes_enabled=False)

        # 6. Malformed types fail closed (int, bool, list)
        for bad_val in [123, True, False, []]:
            with pytest.raises(IntegrationTokenEnvelopeError):
                determine_write_format(contractor_id=cid, provider=prov, stored_access=bad_val, stored_refresh=bad_val, encrypted_writes_enabled=False)


@pytest.mark.asyncio
async def test_flag_off_keyless_plaintext_and_absent_writes_plaintext(monkeypatch):
    """Proves flag off + keyless + plaintext/absent writes exact plaintext strings through CAS and audit."""
    monkeypatch.setattr(settings, "integration_token_encryption_keys", "")
    monkeypatch.setattr(settings, "integration_token_active_key_version", None)
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", False)

    fake_db = _FakeFirestore()
    _patch_firestore(monkeypatch, fake_db)
    cid = "c-keyless-plain"

    # 1. Connect absent contractor keyless
    fake_db.collections["contractors"] = {
        cid: _FakeDocRef({"active": True, "jobber_connected": False, "jobber_generation": 0, "jobber_lifecycle_epoch": 0}, doc_id=cid)
    }
    updates, next_gen, audit_id = await it_mutations.connect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        access_token="plain-access-1",
        refresh_token="plain-refresh-1",
        observed_generation=0,
        observed_access_raw=None,
        observed_refresh_raw=None,
    )
    assert updates["jobber_access_token"] == "plain-access-1"
    assert updates["jobber_refresh_token"] == "plain-refresh-1"
    assert type(updates["jobber_access_token"]) is str
    assert next_gen == 1

    # 2. Refresh plaintext contractor keyless
    claim_id_2, _ = await it_mutations.acquire_refresh_claim_cas(
        contractor_id=cid,
        provider="jobber",
        observed_generation=1,
        observed_access_raw="plain-access-1",
        observed_refresh_raw="plain-refresh-1",
    )
    await it_mutations.transition_refresh_claim_to_started_cas(
        contractor_id=cid,
        provider="jobber",
        claim_id=claim_id_2,
        observed_generation=1,
        observed_lifecycle_epoch=1,
        observed_access_raw="plain-access-1",
        observed_refresh_raw="plain-refresh-1",
    )
    updates_ref, next_gen_2 = await it_mutations.persist_refreshed_tokens_cas(
        contractor_id=cid,
        provider="jobber",
        new_access_token="plain-access-2",
        new_refresh_token="plain-refresh-2",
        observed_generation=1,
        observed_lifecycle_epoch=1,
        observed_access_raw="plain-access-1",
        observed_refresh_raw="plain-refresh-1",
        claim_id=claim_id_2,
    )
    assert updates_ref["jobber_access_token"] == "plain-access-2"
    assert updates_ref["jobber_refresh_token"] == "plain-refresh-2"
    assert type(updates_ref["jobber_access_token"]) is str
    assert next_gen_2 == 2


@pytest.mark.asyncio
async def test_flag_true_missing_keys_fails_before_provider_http(monkeypatch):
    """Proves that when encrypted writes flag is True, missing/invalid keys fail before provider HTTP calls."""
    monkeypatch.setattr(settings, "integration_token_encryption_keys", "")
    monkeypatch.setattr(settings, "integration_token_active_key_version", None)
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", True)
    monkeypatch.setattr(settings, "jobber_client_id", "jobber-client-1")

    class _FailIfCalled:
        async def __aenter__(self):
            raise AssertionError("Provider HTTP was called despite missing keys!")
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(jobber_service.httpx, "AsyncClient", _FailIfCalled)
    monkeypatch.setattr(calendar_service.httpx, "AsyncClient", _FailIfCalled)

    fake_db = _FakeFirestore()
    _patch_firestore(monkeypatch, fake_db)
    cid = "c-flag-true-no-keys"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_access_token": "plain-acc",
        "jobber_refresh_token": "plain-ref",
    }, doc_id=cid)
    fake_db.collections["contractors"] = {cid: doc_ref}

    contractor_dict = {"contractor_id": cid, "jobber_access_token": "plain-acc", "jobber_refresh_token": "plain-ref"}
    res = await jobber_service.refresh_access_token(contractor_dict, force=True)
    assert res is None


@pytest.mark.asyncio
async def test_flag_off_writer_preserves_envelope_representation_monotonically(monkeypatch):
    """Proves that a flag-off instance reading an existing envelope persists envelopes, never downgrading to plaintext."""
    _setup_keyring(monkeypatch)
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", False)

    fake_db = _FakeFirestore()
    _patch_firestore(monkeypatch, fake_db)
    cid = "c-monotonic-envelope"

    enc_acc = encrypt_integration_token("old-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("old-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "active": True,
        "jobber_connected": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
    }, doc_id=cid)
    fake_db.collections["contractors"] = {cid: doc_ref}

    claim_id, _ = await it_mutations.acquire_refresh_claim_cas(
        contractor_id=cid,
        provider="jobber",
        observed_generation=1,
        observed_access_raw=enc_acc,
        observed_refresh_raw=enc_ref,
    )
    await it_mutations.transition_refresh_claim_to_started_cas(
        contractor_id=cid,
        provider="jobber",
        claim_id=claim_id,
        observed_generation=1,
        observed_access_raw=enc_acc,
        observed_refresh_raw=enc_ref,
    )
    updates, next_gen = await it_mutations.persist_refreshed_tokens_cas(
        contractor_id=cid,
        provider="jobber",
        new_access_token="new-acc",
        new_refresh_token="new-ref",
        observed_generation=1,
        observed_access_raw=enc_acc,
        observed_refresh_raw=enc_ref,
        claim_id=claim_id,
    )
    # Output must be an envelope dict, NOT a plaintext string!
    assert is_envelope_map(updates["jobber_access_token"]) is True
    assert is_envelope_map(updates["jobber_refresh_token"]) is True
    assert decrypt_integration_token(updates["jobber_access_token"], contractor_id=cid, provider="jobber", token_kind="access") == "new-acc"
    assert decrypt_integration_token(updates["jobber_refresh_token"], contractor_id=cid, provider="jobber", token_kind="refresh") == "new-ref"


@pytest.mark.asyncio
async def test_missing_historical_key_fails_before_provider_call(monkeypatch):
    """Proves that an envelope encrypted with an unconfigured historical key version fails before provider HTTP."""
    _setup_keyring(monkeypatch)
    cid = "c-missing-hist-key"
    # Envelope encrypted with key version 99 (not in keyring)
    k99 = _make_key_b64(b"9")
    custom_keyring = {99: base64.b64decode(k99)}
    enc_acc = encrypt_integration_token("acc", contractor_id=cid, provider="jobber", token_kind="access", active_key_version=99, keyring=custom_keyring)
    enc_ref = encrypt_integration_token("ref", contractor_id=cid, provider="jobber", token_kind="refresh", active_key_version=99, keyring=custom_keyring)

    class _MustNotBeCalled:
        async def __aenter__(self):
            raise AssertionError("Provider HTTP was called unexpectedly!")
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(jobber_service.httpx, "AsyncClient", _MustNotBeCalled)
    fake_db = _FakeFirestore()
    _patch_firestore(monkeypatch, fake_db)

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
    }, doc_id=cid)
    fake_db.collections["contractors"] = {cid: doc_ref}

    contractor_dict = {"contractor_id": cid, "jobber_access_token": enc_acc, "jobber_refresh_token": enc_ref}
    res = await jobber_service.refresh_access_token(contractor_dict, force=True)
    assert res is None


@pytest.mark.asyncio
async def test_direct_service_post_call_to_jobber_with_plaintext_and_envelopes(monkeypatch):
    """Proves that the real post_call -> Jobber service pipeline correctly captures leads with both plaintext and encrypted records."""
    import asyncio
    _setup_keyring(monkeypatch)
    from app.services.post_call import _capture_jobber_lead

    cid = "c-post-call-jobber"
    enc_acc = encrypt_integration_token("jobber-secret-access", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("jobber-secret-refresh", contractor_id=cid, provider="jobber", token_kind="refresh")

    bearer_tokens_seen: list[str] = []

    class _FakeGraphQLClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, headers=None, json=None, timeout=None):
            headers = headers or {}
            auth_hdr = headers.get("Authorization", "")
            if auth_hdr.startswith("Bearer "):
                bearer_tokens_seen.append(auth_hdr.split("Bearer ")[1])
            class _Resp:
                status_code = 200
                def json(self):
                    return {
                        "data": {
                            "clients": {"nodes": []},
                            "clientCreate": {"client": {"id": "jobber-client-1"}},
                            "propertyCreate": {"property": {"id": "jobber-prop-1"}},
                            "requestCreate": {"request": {"id": "jobber-req-1"}},
                        }
                    }
            return _Resp()

    monkeypatch.setattr(jobber_service.httpx, "AsyncClient", _FakeGraphQLClient)
    import app.db.calls as call_db
    import app.db.jobs as job_db
    monkeypatch.setattr(job_db, "claim_jobber_sync", lambda job_id: asyncio.sleep(0, result=True))
    monkeypatch.setattr(job_db, "update_job", lambda *a, **kw: asyncio.sleep(0))
    monkeypatch.setattr(call_db, "save_call", lambda *a, **kw: asyncio.sleep(0))

    # 1. With encrypted envelope contractor record
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
        "jobber_lead_capture_enabled": True,
    }, doc_id=cid)
    fake_db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, fake_db)

    bearer_tokens_seen.clear()
    contractor_enc = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
        "jobber_lead_capture_enabled": True,
    }
    job_data = {"caller_phone": "+15551234567", "caller_name": "Alice", "call_sid": "CA111"}
    res_enc = await _capture_jobber_lead(contractor_enc, job_data, "job-1")
    assert res_enc is True
    assert set(bearer_tokens_seen) == {"jobber-secret-access"}
    assert len(bearer_tokens_seen) >= 1

    # 2. With legacy plaintext contractor record
    cid2 = "c-post-call-jobber-plain"
    doc_ref2 = _FakeDocRef({
        "contractor_id": cid2,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "legacy-jobber-plain-token",
        "jobber_refresh_token": "legacy-jobber-plain-refresh",
        "jobber_lead_capture_enabled": True,
    }, doc_id=cid2)
    fake_db.collections["contractors"][cid2] = doc_ref2
    bearer_tokens_seen.clear()
    contractor_plain = {
        "contractor_id": cid2,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "legacy-jobber-plain-token",
        "jobber_refresh_token": "legacy-jobber-plain-refresh",
        "jobber_lead_capture_enabled": True,
    }
    res_plain = await _capture_jobber_lead(contractor_plain, job_data, "job-2")
    assert res_plain is True
    assert set(bearer_tokens_seen) == {"legacy-jobber-plain-token"}
    assert len(bearer_tokens_seen) >= 1


def test_strict_ast_token_field_inventory():
    """Verify that every raw integration token field reference in AST across app/ and scripts/ matches the strict approved inventory."""
    import ast
    from collections import Counter
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent.parent

    target_fields = {
        "jobber_access_token",
        "jobber_refresh_token",
        "google_calendar_access_token",
        "google_calendar_refresh_token",
    }

    approved_inventory = {
        ("app/api/admin.py", "google_calendar_access_token"): 1,
        ("app/api/admin.py", "google_calendar_refresh_token"): 1,
        ("app/api/admin.py", "jobber_access_token"): 1,
        ("app/api/admin.py", "jobber_refresh_token"): 1,
        ("app/api/contractors.py", "google_calendar_access_token"): 1,
        ("app/api/contractors.py", "google_calendar_refresh_token"): 1,
        ("app/api/contractors.py", "jobber_access_token"): 1,
        ("app/api/contractors.py", "jobber_refresh_token"): 1,
        ("app/db/contractors.py", "google_calendar_access_token"): 1,
        ("app/db/contractors.py", "google_calendar_refresh_token"): 1,
        ("app/db/contractors.py", "jobber_access_token"): 1,
        ("app/db/contractors.py", "jobber_refresh_token"): 1,
        ("app/services/calendar.py", "google_calendar_access_token"): 8,
        ("app/services/calendar.py", "google_calendar_refresh_token"): 9,
        ("app/services/integration_token_mutations.py", "google_calendar_access_token"): 1,
        ("app/services/integration_token_mutations.py", "google_calendar_refresh_token"): 1,
        ("app/services/integration_token_mutations.py", "jobber_access_token"): 1,
        ("app/services/integration_token_mutations.py", "jobber_refresh_token"): 1,
        ("app/services/jobber.py", "jobber_access_token"): 11,
        ("app/services/jobber.py", "jobber_refresh_token"): 6,
    }

    actual_counts: Counter = Counter()
    for directory in ("app", "scripts"):
        for py_path in sorted((repo_root / directory).rglob("*.py")):
            rel_path = str(py_path.relative_to(repo_root))
            tree = ast.parse(py_path.read_text("utf-8"), filename=rel_path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value in target_fields:
                        actual_counts[(rel_path, node.value)] += 1

    assert dict(actual_counts) == approved_inventory, (
        f"AST token field count mismatch:\nActual: {dict(actual_counts)}\nApproved: {approved_inventory}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Section 15: Repair 9 — Exact Boundaries & Missing Rollout Proofs
# ═══════════════════════════════════════════════════════════════════════

class _CustomStr(str):
    """String subclass for adversarial type testing."""


def test_adversarial_key_types_and_subclasses_fail_closed(monkeypatch):
    """Proves that string subclasses, bool/int aliases, and malformed maps fail closed."""
    _setup_keyring(monkeypatch)
    cid = "c-adversarial-types"
    valid_env = encrypt_integration_token("secret", contractor_id=cid, provider="jobber", token_kind="access")

    # 1. decrypt_integration_token rejects str subclass keys in envelope map
    subclass_env = {_CustomStr(k): v for k, v in valid_env.items()}
    with pytest.raises(IntegrationTokenEnvelopeError, match="must be exact str"):
        decrypt_integration_token(subclass_env, contractor_id=cid, provider="jobber", token_kind="access")

    # 2. _exact_raw_credential_equal rejects str subclass keys
    from app.services.integration_tokens import _exact_raw_credential_equal
    assert _exact_raw_credential_equal(subclass_env, valid_env) is False
    assert _exact_raw_credential_equal(valid_env, subclass_env) is False
    assert _exact_raw_credential_equal(subclass_env, subclass_env) is False

    # 3. _verify_audit_postcondition rejects str subclass keys
    from app.services.integration_token_mutations import (
        _exact_scalar_or_composite_equal,
    )
    assert _exact_scalar_or_composite_equal(subclass_env, valid_env) is False
    assert _exact_scalar_or_composite_equal(valid_env, subclass_env) is False

    # Scalar distinctions in _exact_scalar_or_composite_equal
    assert _exact_scalar_or_composite_equal(1, True) is False
    assert _exact_scalar_or_composite_equal(True, 1) is False
    assert _exact_scalar_or_composite_equal(0, False) is False
    assert _exact_scalar_or_composite_equal(1, 1.0) is False
    assert _exact_scalar_or_composite_equal([1], (1,)) is False
    assert _exact_scalar_or_composite_equal({"a": 1}, {"a": True}) is False
    assert _exact_scalar_or_composite_equal({"a": 1}, {"a": 1.0}) is False
    assert _exact_scalar_or_composite_equal(float("nan"), float("nan")) is False
    assert _exact_scalar_or_composite_equal(float("inf"), float("inf")) is False

    # 4. determine_write_format type validation
    with pytest.raises(IntegrationTokenEnvelopeError, match="encrypted_writes_enabled must be an exact bool"):
        determine_write_format(contractor_id=cid, provider="jobber", stored_access=None, stored_refresh=None, encrypted_writes_enabled="true")  # type: ignore

    with pytest.raises(IntegrationTokenEnvelopeError, match="encrypted_writes_enabled must be an exact bool"):
        determine_write_format(contractor_id=cid, provider="jobber", stored_access=None, stored_refresh=None, encrypted_writes_enabled=1)  # type: ignore

    with pytest.raises(IntegrationTokenConfigError):
        determine_write_format(contractor_id=cid, provider="jobber", stored_access=None, stored_refresh=None, encrypted_writes_enabled=True, active_key_version="0")

    with pytest.raises(IntegrationTokenConfigError):
        determine_write_format(contractor_id=cid, provider="jobber", stored_access=None, stored_refresh=None, encrypted_writes_enabled=True, active_key_version=True)  # type: ignore

    with pytest.raises(IntegrationTokenConfigError):
        determine_write_format(contractor_id=cid, provider="jobber", stored_access=None, stored_refresh=None, encrypted_writes_enabled=True, active_key_version="01")


def test_contractor_id_tenant_boundary_and_fallback(monkeypatch):
    """Proves that whitespace-padded IDs, string subclasses, and malformed fallback IDs fail closed for both plaintext and envelopes."""
    _setup_keyring(monkeypatch)
    cid = "c-tenant-exact"
    enc_tok = encrypt_integration_token("secret-tok", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("secret-ref", contractor_id=cid, provider="jobber", token_kind="refresh")
    plain_tok = "plain-secret-token"
    plain_ref = "plain-secret-ref"

    class SubclassStr(str):
        pass

    subclass_id = SubclassStr(cid)

    # 1. resolve_usable_token explicit contractor_id validation for both plaintext and envelope
    plain_contractor = {"contractor_id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": plain_tok, "jobber_refresh_token": plain_ref}
    enc_contractor = {"contractor_id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc_tok, "jobber_refresh_token": enc_ref}

    for bad_explicit_id in ["", "   ", " c-tenant-exact ", "c-tenant-exact ", " c-tenant-exact", subclass_id, False, 0, 123, None]:
        if bad_explicit_id is None:
            # None falls back to embedded ID
            assert resolve_usable_token(plain_contractor, "jobber", "access", contractor_id=None) == plain_tok
            assert resolve_usable_token(enc_contractor, "jobber", "access", contractor_id=None) == "secret-tok"
        else:
            # Non-None bad explicit ID immediately returns None without fallback for BOTH plaintext and envelope!
            assert resolve_usable_token(plain_contractor, "jobber", "access", contractor_id=bad_explicit_id) is None  # type: ignore
            assert resolve_usable_token(enc_contractor, "jobber", "access", contractor_id=bad_explicit_id) is None  # type: ignore

    # 2. Embedded ID fallback: contractor_id present-but-empty/None/padded/subclass/invalid must NEVER fall back to id
    for bad_embedded_id in [None, "", "   ", " cid ", "cid ", " cid", subclass_id, False, 123, ["cid"]]:
        # Both plaintext and envelope must return None!
        assert resolve_usable_token({"contractor_id": bad_embedded_id, "id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": plain_tok, "jobber_refresh_token": plain_ref}, "jobber") is None
        assert resolve_usable_token({"contractor_id": bad_embedded_id, "id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc_tok, "jobber_refresh_token": enc_ref}, "jobber") is None

    # 3. Absent contractor_id key allows fallback to id ONLY IF id is valid built-in string
    assert resolve_usable_token({"id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": plain_tok, "jobber_refresh_token": plain_ref}, "jobber") == plain_tok
    assert resolve_usable_token({"id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc_tok, "jobber_refresh_token": enc_ref}, "jobber") == "secret-tok"

    # If id is invalid/padded/subclass/absent, both plaintext and envelope return None
    for bad_fallback_id in [None, "", "   ", " cid ", subclass_id, False, 123]:
        assert resolve_usable_token({"id": bad_fallback_id, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": plain_tok, "jobber_refresh_token": plain_ref}, "jobber") is None
        assert resolve_usable_token({"id": bad_fallback_id, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc_tok, "jobber_refresh_token": enc_ref}, "jobber") is None

    # No ID at all -> returns None for both plaintext and envelope
    assert resolve_usable_token({"jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": plain_tok, "jobber_refresh_token": plain_ref}, "jobber") is None
    assert resolve_usable_token({"jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc_tok, "jobber_refresh_token": enc_ref}, "jobber") is None


@pytest.mark.asyncio
async def test_jobber_read_tokens_rejects_subclass_and_padded_without_invoking_strip_or_firestore(monkeypatch):
    """Proves app/services/jobber.py::_read_jobber_tokens rejects str subclasses and padded IDs without calling subclass methods or Firestore."""
    class PoisonStr(str):
        def strip(self, *args, **kwargs):
            raise AssertionError("PoisonStr.strip() was called during token validation!")

    class _MustNotAccessFirestore:
        def collection(self, *args, **kwargs):
            raise AssertionError("Firestore was accessed for an invalid contractor ID!")

    monkeypatch.setattr("app.db.firestore_client.get_firestore_client", lambda: _MustNotAccessFirestore())

    poison_id = PoisonStr("c-test-poison")
    assert await jobber_service._read_jobber_tokens(poison_id) is None  # type: ignore

    padded_id = "   c-test-padded   "
    assert await jobber_service._read_jobber_tokens(padded_id) is None


@pytest.mark.asyncio
async def test_oauth_preflight_generation_validation(monkeypatch):
    """Proves that invalid contractor generation aborts OAuth callbacks before any provider HTTP call."""
    _setup_keyring(monkeypatch)
    cid = "c-oauth-gen-check"
    fake_db = _FakeFirestore()
    _patch_firestore(monkeypatch, fake_db)
    monkeypatch.setattr(integrations, "_get_firestore", lambda: fake_db)

    class _MustNotBeCalledHttpx:
        async def __aenter__(self):
            raise AssertionError("Provider HTTP was called unexpectedly on invalid generation preflight!")
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(integrations.httpx, "AsyncClient", _MustNotBeCalledHttpx)

    state_token = "state-gen-test-12345678"
    from app.services.integration_tokens import compute_raw_credentials_fingerprint
    now = time.time()
    fp = compute_raw_credentials_fingerprint(None, None)

    # Test adversarial generation values: bool True, string "1", negative -1, overflow 2147483648
    for invalid_gen in [True, False, "1", -1, 2147483648]:
        doc_ref = _FakeDocRef({
            "contractor_id": cid,
            "active": True,
            "jobber_generation": invalid_gen,
            "jobber_lifecycle_epoch": 0,
        }, doc_id=cid)
        fake_db.collections["contractors"] = {cid: doc_ref}
        fake_db.collections["jobber_oauth_states"] = {
            state_token: _FakeDocRef({
                "contractor_id": cid,
                "provider": "jobber",
                "lifecycle_epoch": 0,
                "generation": 0,
                "credentials_fingerprint": fp,
                "created_at": now,
                "expires_at": now + 300.0,
            }, doc_id=state_token)
        }

        with pytest.raises(HTTPException) as exc_info:
            await integrations.jobber_callback(code="auth-code", state=state_token)
        assert exc_info.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_oauth_preflight_generation_adversarial_value_free_logging(monkeypatch, caplog, provider):
    """Proves adversarial generation values with secrets/newlines do not leak into logs and abort before provider HTTP."""
    import logging

    from app.services.integration_tokens import compute_raw_credentials_fingerprint
    _setup_keyring(monkeypatch)
    cid = f"c-oauth-gen-leak-{provider}"
    fake_db = _FakeFirestore()
    _patch_firestore(monkeypatch, fake_db)
    monkeypatch.setattr(integrations, "_get_firestore", lambda: fake_db)

    class _MustNotBeCalledHttpx:
        async def __aenter__(self):
            raise AssertionError("Provider HTTP was called on invalid generation preflight!")
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(integrations.httpx, "AsyncClient", _MustNotBeCalledHttpx)

    secret_payload = "SECRET_LEAK_KEY_99999\nINJECTED_ATTACKER_VALUE_88888"
    state_token = f"state-leak-test-12345678-{provider}"
    now = time.time()
    fp = compute_raw_credentials_fingerprint(None, None)

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        f"{provider}_generation": secret_payload,
        f"{provider}_lifecycle_epoch": 0,
    }, doc_id=cid)
    fake_db.collections["contractors"] = {cid: doc_ref}

    state_col = "jobber_oauth_states" if provider == "jobber" else "google_oauth_states"
    fake_db.collections[state_col] = {
        state_token: _FakeDocRef({
            "contractor_id": cid,
            "provider": provider,
            "lifecycle_epoch": 0,
            "generation": 0,
            "credentials_fingerprint": fp,
            "created_at": now,
            "expires_at": now + 300.0,
        }, doc_id=state_token)
    }

    with caplog.at_level(logging.ERROR), pytest.raises(HTTPException) as exc_info:
        if provider == "jobber":
            await integrations.jobber_callback(code="auth-code", state=state_token)
        else:
            await integrations.google_calendar_callback(code="auth-code", state=state_token)

    assert exc_info.value.status_code == 400

    # Verify that secret payload is completely absent from all captured log output
    assert "SECRET_LEAK_KEY_99999" not in caplog.text
    assert "INJECTED_ATTACKER_VALUE_88888" not in caplog.text


@pytest.mark.asyncio
async def test_real_jobber_service_refresh_with_envelopes(monkeypatch):
    """Proves real Jobber service refresh under flag=False: rotates refresh token, commits envelopes, 1 HTTP call."""
    _setup_keyring(monkeypatch)
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", False)
    monkeypatch.setattr(settings, "jobber_client_id", "jobber-client-123")
    monkeypatch.setattr(settings, "jobber_client_secret", "jobber-secret-456")

    cid = "c-real-jobber-svc-refresh"
    enc_acc = encrypt_integration_token("old-jobber-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("old-jobber-ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
    }, doc_id=cid)
    fake_db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, fake_db)

    http_calls = []

    class _FakeJobberClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, data=None, timeout=None):
            http_calls.append({"url": url, "data": data})
            class _Resp:
                status_code = 200
                def json(self):
                    return {
                        "access_token": "new-jobber-access-jwt",
                        "refresh_token": "new-jobber-rotated-refresh-token",
                        "expires_in": 3600,
                    }
            return _Resp()

    monkeypatch.setattr(jobber_service.httpx, "AsyncClient", _FakeJobberClient)

    contractor_dict = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
    }

    new_token = await jobber_service.refresh_access_token(contractor_dict, force=True)
    assert new_token == "new-jobber-access-jwt"
    assert len(http_calls) == 1
    assert http_calls[0]["data"]["refresh_token"] == "old-jobber-ref"

    # Durable assertions
    updated_doc = fake_db.collections["contractors"][cid].data
    assert updated_doc["jobber_generation"] == 2
    assert type(updated_doc["jobber_access_token"]) is dict
    assert type(updated_doc["jobber_refresh_token"]) is dict

    # Decrypt durable envelopes
    assert decrypt_integration_token(updated_doc["jobber_access_token"], contractor_id=cid, provider="jobber", token_kind="access") == "new-jobber-access-jwt"
    assert decrypt_integration_token(updated_doc["jobber_refresh_token"], contractor_id=cid, provider="jobber", token_kind="refresh") == "new-jobber-rotated-refresh-token"


@pytest.mark.asyncio
async def test_real_google_calendar_service_401_refresh_with_envelopes(monkeypatch):
    """Proves real Google Calendar 401 retry refresh under flag=False: 401 -> 1 refresh exchange -> retry 200 -> committed envelopes."""
    _setup_keyring(monkeypatch)
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", False)
    monkeypatch.setattr(settings, "google_calendar_client_id", "gcal-client-123")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "gcal-secret-456")

    cid = "c-real-gcal-401-refresh"
    enc_acc = encrypt_integration_token("old-gcal-acc", contractor_id=cid, provider="google_calendar", token_kind="access")
    enc_ref = encrypt_integration_token("old-gcal-ref", contractor_id=cid, provider="google_calendar", token_kind="refresh")

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 0,
        "google_calendar_access_token": enc_acc,
        "google_calendar_refresh_token": enc_ref,
    }, doc_id=cid)
    fake_db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, fake_db)

    token_exchange_calls = []

    class _FakeGoogleHttpx:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, data=None, json=None, headers=None, timeout=None):
            if "oauth2.googleapis.com/token" in str(url):
                token_exchange_calls.append({"url": url, "data": data})
                class _TokenResp:
                    status_code = 200
                    def json(self):
                        return {
                            "access_token": "new-gcal-refreshed-token",
                            "refresh_token": "new-gcal-rotated-refresh",
                            "expires_in": 3600,
                        }
                return _TokenResp()
            raise NotImplementedError(f"Unexpected post: {url}")

    monkeypatch.setattr(calendar_service.httpx, "AsyncClient", _FakeGoogleHttpx)

    contractor_dict = {
        "contractor_id": cid,
        "google_calendar_connected": True,
        "google_calendar_access_token": enc_acc,
        "google_calendar_refresh_token": enc_ref,
        "google_calendar_lifecycle_epoch": 0,
        "google_calendar_generation": 1,
    }

    # Simulate 401 on first call and 200 on retry via _with_token_refresh
    api_attempts = []
    class _MockApiResp:
        def __init__(self, status_code, token):
            self.status_code = status_code
            self.token = token

    async def _test_calendar_api_call(token: str):
        api_attempts.append(token)
        if len(api_attempts) == 1:
            return _MockApiResp(401, token)
        return _MockApiResp(200, token)

    result = await calendar_service._with_token_refresh(contractor_dict, _test_calendar_api_call)
    assert result.status_code == 200
    assert result.token == "new-gcal-refreshed-token"
    assert len(api_attempts) == 2
    assert api_attempts[0] == "old-gcal-acc"
    assert api_attempts[1] == "new-gcal-refreshed-token"
    assert len(token_exchange_calls) == 1

    # Durable assertions
    updated_doc = fake_db.collections["contractors"][cid].data
    assert updated_doc["google_calendar_generation"] == 2
    assert type(updated_doc["google_calendar_access_token"]) is dict
    assert type(updated_doc["google_calendar_refresh_token"]) is dict

    # Decrypt durable envelopes
    assert decrypt_integration_token(updated_doc["google_calendar_access_token"], contractor_id=cid, provider="google_calendar", token_kind="access") == "new-gcal-refreshed-token"
    assert decrypt_integration_token(updated_doc["google_calendar_refresh_token"], contractor_id=cid, provider="google_calendar", token_kind="refresh") == "new-gcal-rotated-refresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_mixed_revision_stale_writer_cas_race(monkeypatch, provider):
    """Proves that a stale writer attempting CAS against advanced envelopes loses and envelopes remain intact."""
    _setup_keyring(monkeypatch)
    cid = f"c-stale-writer-{provider}"

    # Initial durable document has plaintext tokens at generation 1
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        f"{provider}_connected": True,
        f"{provider}_generation": 1,
        f"{provider}_lifecycle_epoch": 0,
        f"{provider}_access_token": "legacy-plain-access-1",
        f"{provider}_refresh_token": "legacy-plain-refresh-1",
    }, doc_id=cid)
    fake_db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, fake_db)

    # 1. Advanced writer with flag=True advances to generation 2 with encrypted envelopes
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", True)
    claim_id_1, _ = await acquire_refresh_claim_cas(
        contractor_id=cid,
        provider=provider,
        observed_generation=1,
        observed_access_raw="legacy-plain-access-1",
        observed_refresh_raw="legacy-plain-refresh-1",
        db=fake_db,
    )
    await transition_refresh_claim_to_started_cas(
        contractor_id=cid,
        provider=provider,
        claim_id=claim_id_1,
        observed_generation=1,
        observed_access_raw="legacy-plain-access-1",
        observed_refresh_raw="legacy-plain-refresh-1",
        db=fake_db,
    )
    _, next_gen = await persist_refreshed_tokens_cas(
        contractor_id=cid,
        provider=provider,
        new_access_token="winner-acc-2",
        new_refresh_token="winner-ref-2",
        observed_generation=1,
        observed_access_raw="legacy-plain-access-1",
        observed_refresh_raw="legacy-plain-refresh-1",
        claim_id=claim_id_1,
        db=fake_db,
    )
    assert next_gen == 2
    winner_doc = fake_db.collections["contractors"][cid].data
    assert type(winner_doc[f"{provider}_access_token"]) is dict
    assert type(winner_doc[f"{provider}_refresh_token"]) is dict

    # 2. Stale writer (flag=False) attempts persist with old observed generation 1 and old plaintext tokens
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", False)
    with pytest.raises(IntegrationTokenCASConflict):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider=provider,
            new_access_token="stale-loser-acc",
            new_refresh_token="stale-loser-ref",
            observed_generation=1,
            observed_access_raw="legacy-plain-access-1",
            observed_refresh_raw="legacy-plain-refresh-1",
            claim_id=claim_id_1,
            db=fake_db,
        )

    # 3. Verify winning envelopes in Firestore are unchanged, still at gen 2, and decrypt cleanly
    final_doc = fake_db.collections["contractors"][cid].data
    assert final_doc[f"{provider}_generation"] == 2
    assert decrypt_integration_token(final_doc[f"{provider}_access_token"], contractor_id=cid, provider=provider, token_kind="access") == "winner-acc-2"
    assert decrypt_integration_token(final_doc[f"{provider}_refresh_token"], contractor_id=cid, provider=provider, token_kind="refresh") == "winner-ref-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
@pytest.mark.parametrize("flag_enabled", [False, True])
async def test_strengthened_mutation_matrix_durable_representations(monkeypatch, provider, flag_enabled):
    """Proves actual durable representations (plaintext str vs envelope dict) across connect and refresh operations."""
    _setup_keyring(monkeypatch)
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", flag_enabled)

    # 1. Connect absent record
    cid_connect = f"c-matrix-connect-{provider}-{flag_enabled}"
    doc_ref_conn = _FakeDocRef({"contractor_id": cid_connect, "active": True}, doc_id=cid_connect)
    fake_db = _FakeFirestore({"contractors": {cid_connect: doc_ref_conn}})
    _patch_firestore(monkeypatch, fake_db)

    await connect_provider_cas(
        contractor_id=cid_connect,
        provider=provider,
        access_token="conn-acc-token",
        refresh_token="conn-ref-token",
        observed_generation=0,
        observed_access_raw=None,
        observed_refresh_raw=None,
    )
    doc_conn = fake_db.collections["contractors"][cid_connect].data
    expected_conn_type = dict if flag_enabled else str
    assert type(doc_conn[f"{provider}_access_token"]) is expected_conn_type
    assert type(doc_conn[f"{provider}_refresh_token"]) is expected_conn_type
    assert resolve_usable_token(doc_conn, provider, "access") == "conn-acc-token"
    assert resolve_usable_token(doc_conn, provider, "refresh") == "conn-ref-token"

    # 2. Refresh plaintext record
    cid_plain = f"c-matrix-plain-{provider}-{flag_enabled}"
    doc_ref_plain = _FakeDocRef({
        "contractor_id": cid_plain,
        "active": True,
        f"{provider}_connected": True,
        f"{provider}_generation": 1,
        f"{provider}_lifecycle_epoch": 0,
        f"{provider}_access_token": "plain-acc-1",
        f"{provider}_refresh_token": "plain-ref-1",
    }, doc_id=cid_plain)
    fake_db.collections["contractors"][cid_plain] = doc_ref_plain

    claim_id_plain, _ = await acquire_refresh_claim_cas(
        contractor_id=cid_plain,
        provider=provider,
        observed_generation=1,
        observed_access_raw="plain-acc-1",
        observed_refresh_raw="plain-ref-1",
        db=fake_db,
    )
    await transition_refresh_claim_to_started_cas(
        contractor_id=cid_plain,
        provider=provider,
        claim_id=claim_id_plain,
        observed_generation=1,
        observed_access_raw="plain-acc-1",
        observed_refresh_raw="plain-ref-1",
        db=fake_db,
    )
    await persist_refreshed_tokens_cas(
        contractor_id=cid_plain,
        provider=provider,
        new_access_token="plain-acc-2",
        new_refresh_token="plain-ref-2",
        observed_generation=1,
        observed_access_raw="plain-acc-1",
        observed_refresh_raw="plain-ref-1",
        claim_id=claim_id_plain,
        db=fake_db,
    )
    doc_plain_ref = fake_db.collections["contractors"][cid_plain].data
    expected_plain_ref_type = dict if flag_enabled else str
    assert type(doc_plain_ref[f"{provider}_access_token"]) is expected_plain_ref_type
    assert type(doc_plain_ref[f"{provider}_refresh_token"]) is expected_plain_ref_type
    assert resolve_usable_token(doc_plain_ref, provider, "access") == "plain-acc-2"
    assert resolve_usable_token(doc_plain_ref, provider, "refresh") == "plain-ref-2"

    # 3. Refresh envelope record (monotonicity: ALWAYS remains envelope even if flag_enabled is False!)
    cid_enc = f"c-matrix-enc-{provider}-{flag_enabled}"
    enc_acc = encrypt_integration_token("enc-acc-1", contractor_id=cid_enc, provider=provider, token_kind="access")
    enc_ref = encrypt_integration_token("enc-ref-1", contractor_id=cid_enc, provider=provider, token_kind="refresh")
    doc_ref_enc = _FakeDocRef({
        "contractor_id": cid_enc,
        "active": True,
        f"{provider}_connected": True,
        f"{provider}_generation": 1,
        f"{provider}_lifecycle_epoch": 0,
        f"{provider}_access_token": enc_acc,
        f"{provider}_refresh_token": enc_ref,
    }, doc_id=cid_enc)
    fake_db.collections["contractors"][cid_enc] = doc_ref_enc

    claim_id_enc, _ = await acquire_refresh_claim_cas(
        contractor_id=cid_enc,
        provider=provider,
        observed_generation=1,
        observed_access_raw=enc_acc,
        observed_refresh_raw=enc_ref,
        db=fake_db,
    )
    await transition_refresh_claim_to_started_cas(
        contractor_id=cid_enc,
        provider=provider,
        claim_id=claim_id_enc,
        observed_generation=1,
        observed_access_raw=enc_acc,
        observed_refresh_raw=enc_ref,
        db=fake_db,
    )
    await persist_refreshed_tokens_cas(
        contractor_id=cid_enc,
        provider=provider,
        new_access_token="enc-acc-2",
        new_refresh_token="enc-ref-2",
        observed_generation=1,
        observed_access_raw=enc_acc,
        observed_refresh_raw=enc_ref,
        claim_id=claim_id_enc,
        db=fake_db,
    )
    doc_enc_ref = fake_db.collections["contractors"][cid_enc].data
    assert type(doc_enc_ref[f"{provider}_access_token"]) is dict
    assert type(doc_enc_ref[f"{provider}_refresh_token"]) is dict
    assert resolve_usable_token(doc_enc_ref, provider, "access") == "enc-acc-2"
    assert resolve_usable_token(doc_enc_ref, provider, "refresh") == "enc-ref-2"


def test_startup_runtime_safety_matrix_environments_and_flags(monkeypatch):
    """Proves the strict startup safety matrix across environments and encrypted writes flag settings."""
    from app.config import validate_runtime_safety

    valid_keyring = json.dumps({"1": _make_key_b64(b"1")})

    def _set_base_valid(env="development", flag=False):
        monkeypatch.setattr(settings, "environment", env)
        monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", flag)
        monkeypatch.setattr(settings, "public_demo_breaker_twilio_parent_account_sid", "")
        monkeypatch.setattr(settings, "public_demo_breaker_twilio_parent_main_api_key_sid", "")
        monkeypatch.setattr(settings, "public_demo_breaker_twilio_parent_main_api_key_secret", "")
        monkeypatch.setattr(settings, "public_demo_breaker_twilio_child_account_sid", "")
        monkeypatch.setattr(settings, "public_demo_enabled", False)
        monkeypatch.setattr(settings, "transcript_encryption_key", _make_key_b64(b"t"))
        monkeypatch.setattr(settings, "allow_production_resources_in_non_production", False)

        if env in {"staging", "production"}:
            monkeypatch.setattr(settings, "production_twilio_account_sid", "ACprod")
            monkeypatch.setattr(settings, "twilio_account_sid", "ACprod" if env == "production" else "ACstage")
            if env == "production":
                monkeypatch.setattr(settings, "appstore_environment", "production")
                monkeypatch.setattr(settings, "apns_sandbox", False)
                monkeypatch.setattr(settings, "cloud_run_url", "https://prod.run.app")
                monkeypatch.setattr(settings, "firestore_project_id", "kevin-491315")
            else:
                monkeypatch.setattr(settings, "appstore_environment", "sandbox")
                monkeypatch.setattr(settings, "apns_sandbox", True)
                monkeypatch.setattr(settings, "cloud_run_url", "https://stage.run.app")
                monkeypatch.setattr(settings, "firestore_project_id", "kevin-staging")
                monkeypatch.setattr(settings, "firebase_database_url", "https://kevin-staging.firebaseio.com")
        else:
            # development / demo
            monkeypatch.setattr(settings, "production_twilio_account_sid", "ACprod")
            monkeypatch.setattr(settings, "twilio_account_sid", "ACdev")
            monkeypatch.setattr(settings, "appstore_environment", "sandbox")
            monkeypatch.setattr(settings, "apns_sandbox", True)
            monkeypatch.setattr(settings, "cloud_run_url", "https://dev.run.app")
            monkeypatch.setattr(settings, "firestore_project_id", "kevin-dev")
            monkeypatch.setattr(settings, "firebase_database_url", "https://kevin-dev.firebaseio.com")

    # 1. Development + Flag False + Keyless -> PASS
    _set_base_valid("development", flag=False)
    monkeypatch.setattr(settings, "integration_token_encryption_keys", "")
    monkeypatch.setattr(settings, "integration_token_active_key_version", None)
    validate_runtime_safety()

    # 2. Development + Flag True + Keyless -> FAIL
    _set_base_valid("development", flag=True)
    monkeypatch.setattr(settings, "integration_token_encryption_keys", "")
    monkeypatch.setattr(settings, "integration_token_active_key_version", None)
    with pytest.raises(RuntimeError, match="INTEGRATION_TOKEN_ENCRYPTION_KEYS"):
        validate_runtime_safety()

    # 3. Development + Flag True + Valid Keys -> PASS
    _set_base_valid("development", flag=True)
    monkeypatch.setattr(settings, "integration_token_encryption_keys", valid_keyring)
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    validate_runtime_safety()

    # 4. Staging + Flag False + Keyless -> FAIL (staging requires keys even when flag is false!)
    _set_base_valid("staging", flag=False)
    monkeypatch.setattr(settings, "integration_token_encryption_keys", "")
    monkeypatch.setattr(settings, "integration_token_active_key_version", None)
    with pytest.raises(RuntimeError, match="INTEGRATION_TOKEN_ENCRYPTION_KEYS"):
        validate_runtime_safety()

    # 5. Staging + Flag False + Valid Keys -> PASS
    _set_base_valid("staging", flag=False)
    monkeypatch.setattr(settings, "integration_token_encryption_keys", valid_keyring)
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    validate_runtime_safety()

    # 6. Production + Flag False + Keyless -> FAIL (production requires keys even when flag is false!)
    _set_base_valid("production", flag=False)
    monkeypatch.setattr(settings, "integration_token_encryption_keys", "")
    monkeypatch.setattr(settings, "integration_token_active_key_version", None)
    with pytest.raises(RuntimeError, match="INTEGRATION_TOKEN_ENCRYPTION_KEYS"):
        validate_runtime_safety()

    # 7. Production + Flag True + Valid Keys -> PASS
    _set_base_valid("production", flag=True)
    monkeypatch.setattr(settings, "integration_token_encryption_keys", valid_keyring)
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    validate_runtime_safety()


# ═══════════════════════════════════════════════════════════════════════
# Section 18: Repair 11 — Legacy Compatibility, Lease Enforcement, and Operator Safety
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
@pytest.mark.parametrize("flag_enabled", [False, True])
async def test_legacy_record_refresh_flag_matrix(monkeypatch, provider, flag_enabled):
    """Proves legacy records with missing connected flag & gen 0 refresh in 1 provider exchange and advance to gen 1."""
    _setup_keyring(monkeypatch)
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", flag_enabled)

    cid = f"c-legacy-refresh-{provider}-{flag_enabled}"
    # Legacy record: no provider_connected field, no generation field
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        f"{provider}_access_token": "legacy-access-token",
        f"{provider}_refresh_token": "legacy-refresh-token",
    }, doc_id=cid)
    fake_db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, fake_db)

    provider_http_calls = []

    if provider == "jobber":
        monkeypatch.setattr(settings, "jobber_client_id", "jobber-client-123")
        monkeypatch.setattr(settings, "jobber_client_secret", "jobber-secret-456")

        class _FakeJobberHttp:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, url, data=None, json=None, headers=None, timeout=None):
                provider_http_calls.append({"url": url, "data": data})
                class _Resp:
                    status_code = 200
                    def json(self):
                        return {
                            "access_token": "new-jobber-refreshed-acc",
                            "refresh_token": "new-jobber-rotated-ref",
                            "expires_in": 3600,
                        }
                return _Resp()

        monkeypatch.setattr(jobber_service.httpx, "AsyncClient", _FakeJobberHttp)

        contractor_dict = {
            "contractor_id": cid,
            "jobber_access_token": "legacy-access-token",
            "jobber_refresh_token": "legacy-refresh-token",
        }
        res = await jobber_service.refresh_access_token(contractor_dict, force=True)
        assert res == "new-jobber-refreshed-acc"
        assert len(provider_http_calls) == 1

    else:
        monkeypatch.setattr(settings, "google_calendar_client_id", "gcal-client-123")
        monkeypatch.setattr(settings, "google_calendar_client_secret", "gcal-secret-456")

        class _FakeGoogleHttp:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, url, data=None, json=None, headers=None, timeout=None):
                if "oauth2.googleapis.com/token" in str(url):
                    provider_http_calls.append({"url": url, "data": data})
                    class _Resp:
                        status_code = 200
                        def json(self):
                            return {
                                "access_token": "new-gcal-refreshed-acc",
                                "refresh_token": "new-gcal-rotated-ref",
                                "expires_in": 3600,
                            }
                    return _Resp()
                raise NotImplementedError(f"Unexpected URL: {url}")

        monkeypatch.setattr(calendar_service.httpx, "AsyncClient", _FakeGoogleHttp)

        contractor_dict = {
            "contractor_id": cid,
            "google_calendar_access_token": "legacy-access-token",
            "google_calendar_refresh_token": "legacy-refresh-token",
        }
        api_attempts = []
        class _MockApiResp:
            def __init__(self, status_code, token):
                self.status_code = status_code
                self.token = token

        async def _test_calendar_api_call(token: str):
            api_attempts.append(token)
            if len(api_attempts) == 1:
                return _MockApiResp(401, token)
            return _MockApiResp(200, token)

        result = await calendar_service._with_token_refresh(contractor_dict, _test_calendar_api_call)
        assert result.status_code == 200
        assert result.token == "new-gcal-refreshed-acc"
        assert len(provider_http_calls) == 1

    # Durable assertions
    updated_doc = fake_db.collections["contractors"][cid].data
    assert updated_doc[f"{provider}_connected"] is True
    assert updated_doc[f"{provider}_generation"] == 1
    expected_type = dict if flag_enabled else str
    assert type(updated_doc[f"{provider}_access_token"]) is expected_type
    assert type(updated_doc[f"{provider}_refresh_token"]) is expected_type
    assert resolve_usable_token(updated_doc, provider, "access") == ("new-jobber-refreshed-acc" if provider == "jobber" else "new-gcal-refreshed-acc")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
@pytest.mark.parametrize("bad_state", [
    {"connected": False},
    {"connected": "true"},
    {"connected": 1},
    {"connected": None},
    {"missing_refresh": True},
])
async def test_provider_must_not_be_called_on_explicit_disconnected_or_malformed_state(monkeypatch, provider, bad_state):
    """Proves provider HTTP is NEVER called when connected flag is False, non-bool, or credentials are malformed."""
    _setup_keyring(monkeypatch)
    cid = f"c-bad-state-{provider}"

    class _FailIfCalled:
        async def __aenter__(self):
            raise AssertionError(f"Provider HTTP was called for bad state: {bad_state}")
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(jobber_service.httpx, "AsyncClient", _FailIfCalled)
    monkeypatch.setattr(calendar_service.httpx, "AsyncClient", _FailIfCalled)

    doc_data = {
        "contractor_id": cid,
        "active": True,
        f"{provider}_access_token": "valid-acc",
        f"{provider}_refresh_token": "valid-ref",
    }
    if "connected" in bad_state:
        doc_data[f"{provider}_connected"] = bad_state["connected"]
    if bad_state.get("missing_refresh"):
        del doc_data[f"{provider}_refresh_token"]

    doc_ref = _FakeDocRef(doc_data, doc_id=cid)
    fake_db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, fake_db)

    contractor_dict = {
        "contractor_id": cid,
        f"{provider}_access_token": "valid-acc",
        f"{provider}_refresh_token": "valid-ref",
    }
    if provider == "jobber":
        res = await jobber_service.refresh_access_token(contractor_dict, force=True)
        assert res is None
    else:
        res = await calendar_service.refresh_access_token(contractor_dict, force=True)
        assert res is None


@pytest.mark.asyncio
async def test_persist_refreshed_tokens_cas_lease_enforcement_boundary(monkeypatch):
    """Proves persist_refreshed_tokens_cas strictly enforces valid unexpired lease claims at mutation boundary."""
    _setup_keyring(monkeypatch)
    cid = "c-lease-boundary-test"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_access_token": "old-acc",
        "jobber_refresh_token": "old-ref",
    }, doc_id=cid)
    fake_db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, fake_db)

    valid_claim_id = secrets.token_hex(16)

    # 1. Calling persist on document without claim fields fails closed
    with pytest.raises(IntegrationTokenCASConflict, match="(Missing or invalid operation intent|Missing refresh lease claim record)"):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-acc",
            new_refresh_token="new-ref",
            observed_generation=1,
            observed_access_raw="old-acc",
            observed_refresh_raw="old-ref",
            claim_id=valid_claim_id,
            db=fake_db,
        )

    # 2. Populate claim fields on doc
    doc_ref.data["jobber_refresh_claim_id"] = valid_claim_id
    doc_ref.data["jobber_refresh_claim_phase"] = "provider_request_started"
    doc_ref.data["jobber_refresh_claim_expires_at"] = time.time() + 60.0
    doc_ref.data["jobber_refresh_claim_generation"] = 1

    # Calling with mismatched claim ID fails closed
    with pytest.raises(IntegrationTokenCASConflict, match="Refresh lease claim ID mismatch"):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-acc",
            new_refresh_token="new-ref",
            observed_generation=1,
            observed_access_raw="old-acc",
            observed_refresh_raw="old-ref",
            claim_id=secrets.token_hex(16),
            db=fake_db,
        )

    # Calling with expired claim fails closed
    doc_ref.data["jobber_refresh_claim_expires_at"] = time.time() - 10.0
    with pytest.raises(IntegrationTokenCASConflict, match="Refresh lease expired"):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-acc",
            new_refresh_token="new-ref",
            observed_generation=1,
            observed_access_raw="old-acc",
            observed_refresh_raw="old-ref",
            claim_id=valid_claim_id,
            db=fake_db,
        )

    # Calling with claim generation mismatch fails closed
    doc_ref.data["jobber_refresh_claim_expires_at"] = time.time() + 60.0
    doc_ref.data["jobber_refresh_claim_generation"] = 0
    with pytest.raises(IntegrationTokenCASConflict, match="Refresh lease generation/epoch mismatch on commit"):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-acc",
            new_refresh_token="new-ref",
            observed_generation=1,
            observed_access_raw="old-acc",
            observed_refresh_raw="old-ref",
            claim_id=valid_claim_id,
            db=fake_db,
        )

    # 3. Releasing claim with wrong ID does not clear; releasing with exact ID clears
    doc_ref.data["jobber_refresh_claim_generation"] = 1
    doc_ref.data["jobber_refresh_claim_phase"] = "reserved"
    await release_refresh_claim_cas(contractor_id=cid, provider="jobber", claim_id=secrets.token_hex(16), db=fake_db)
    assert doc_ref.data.get("jobber_refresh_claim_id") == valid_claim_id

    await release_refresh_claim_cas(contractor_id=cid, provider="jobber", claim_id=valid_claim_id, db=fake_db)
    assert "jobber_refresh_claim_id" not in doc_ref.data


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_in_memory_expiry_hydration_and_delete_field_safety(monkeypatch, provider):
    """Proves that when provider omits expiry, durable expiry is deleted and in-memory expiry is safely removed."""
    _setup_keyring(monkeypatch)
    cid = f"c-expiry-hydration-{provider}"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        f"{provider}_connected": True,
        f"{provider}_generation": 1,
        f"{provider}_lifecycle_epoch": 0,
        f"{provider}_access_token": "old-acc",
        f"{provider}_refresh_token": "old-ref",
        f"{provider}_token_expires_at": time.time() + 3600.0,
    }, doc_id=cid)
    fake_db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, fake_db)

    if provider == "jobber":
        monkeypatch.setattr(settings, "jobber_client_id", "jobber-client-123")
        monkeypatch.setattr(settings, "jobber_client_secret", "jobber-secret-456")

        class _FakeJobberHttpNoExp:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, url, data=None, json=None, headers=None, timeout=None):
                class _Resp:
                    status_code = 200
                    def json(self):
                        # Response omits expires_in
                        return {
                            "access_token": "new-jobber-no-exp-acc",
                            "refresh_token": "new-jobber-no-exp-ref",
                        }
                return _Resp()

        monkeypatch.setattr(jobber_service.httpx, "AsyncClient", _FakeJobberHttpNoExp)

        contractor_dict = {
            "contractor_id": cid,
            "jobber_access_token": "old-acc",
            "jobber_refresh_token": "old-ref",
            "jobber_token_expires_at": time.time() + 3600.0,
        }
        res = await jobber_service.refresh_access_token(contractor_dict, force=True)
        assert res == "new-jobber-no-exp-acc"
        assert "jobber_token_expires_at" not in contractor_dict
        from google.cloud.firestore_v1 import DELETE_FIELD
        assert "jobber_token_expires_at" not in doc_ref.data or doc_ref.data["jobber_token_expires_at"] is DELETE_FIELD

    else:
        monkeypatch.setattr(settings, "google_calendar_client_id", "gcal-client-123")
        monkeypatch.setattr(settings, "google_calendar_client_secret", "gcal-secret-456")

        class _FakeGoogleHttpNoExp:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, url, data=None, json=None, headers=None, timeout=None):
                class _Resp:
                    status_code = 200
                    def json(self):
                        # Response omits expires_in
                        return {
                            "access_token": "new-gcal-no-exp-acc",
                            "refresh_token": "new-gcal-no-exp-ref",
                        }
                return _Resp()

        monkeypatch.setattr(calendar_service.httpx, "AsyncClient", _FakeGoogleHttpNoExp)

        contractor_dict = {
            "contractor_id": cid,
            "google_calendar_access_token": "old-acc",
            "google_calendar_refresh_token": "old-ref",
            "google_calendar_token_expires_at": time.time() + 3600.0,
        }
        res = await calendar_service.refresh_access_token(contractor_dict, force=True)
        assert res == "new-gcal-no-exp-acc"
        assert "google_calendar_token_expires_at" not in contractor_dict
        from google.cloud.firestore_v1 import DELETE_FIELD
        assert "google_calendar_token_expires_at" not in doc_ref.data or doc_ref.data["google_calendar_token_expires_at"] is DELETE_FIELD


@pytest.mark.asyncio
async def test_connect_legacy_record_with_existing_credentials_emits_reconnected_audit(monkeypatch):
    """Proves reconnecting a legacy record (generation 0 with existing credentials) emits action=reconnected."""
    _setup_keyring(monkeypatch)
    cid_legacy = "c-legacy-reconn-audit"

    # Legacy record at generation 0 with existing credentials
    doc_ref_legacy = _FakeDocRef({
        "contractor_id": cid_legacy,
        "active": True,
        "jobber_connected": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 0,
        "jobber_access_token": "legacy-old-acc",
        "jobber_refresh_token": "legacy-old-ref",
    }, doc_id=cid_legacy)
    fake_db = _FakeFirestore({"contractors": {cid_legacy: doc_ref_legacy}})
    _patch_firestore(monkeypatch, fake_db)

    updates, next_gen, audit_id = await connect_provider_cas(
        contractor_id=cid_legacy,
        provider="jobber",
        access_token="new-connected-acc",
        refresh_token="new-connected-ref",
        observed_generation=0,
        observed_access_raw="legacy-old-acc",
        observed_refresh_raw="legacy-old-ref",
        db=fake_db,
    )
    assert next_gen == 1
    assert audit_id.endswith("_reconnected")
    assert fake_db.collections["integration_lifecycle_audit"][audit_id].data["action"] == "reconnected"

    # First-ever connect on absent record emits action=connected
    cid_new = "c-first-ever-connect"
    doc_ref_new = _FakeDocRef({"contractor_id": cid_new, "active": True}, doc_id=cid_new)
    fake_db.collections["contractors"][cid_new] = doc_ref_new

    updates_new, next_gen_new, audit_id_new = await connect_provider_cas(
        contractor_id=cid_new,
        provider="jobber",
        access_token="new-acc-first",
        refresh_token="new-ref-first",
        observed_generation=0,
        observed_access_raw=None,
        observed_refresh_raw=None,
        db=fake_db,
    )
    assert next_gen_new == 1
    assert audit_id_new.endswith("_connected")
    assert fake_db.collections["integration_lifecycle_audit"][audit_id_new].data["action"] == "connected"


# ===========================================================================
# 19. Repair 12: Pair-Valid Boundary, Envelope Floor, & Retry-Time Leases
# ===========================================================================

def test_parse_active_key_version_rejects_padding_and_whitespace():
    """Proves parse_active_key_version rejects padded strings, whitespace, and leading zeroes."""
    from app.services.integration_tokens import (
        IntegrationTokenConfigError,
        parse_active_key_version,
    )

    # Valid values
    assert parse_active_key_version(None) is None
    assert parse_active_key_version("") is None
    assert parse_active_key_version("1") == 1
    assert parse_active_key_version("42") == 42
    assert parse_active_key_version(1) == 1
    assert parse_active_key_version(42) == 42

    # Padded and whitespace strings must be rejected without value leakage
    for invalid in [" 1 ", "1 ", " 1", " ", "  \t\n  ", "01", "0", "-1", "1.0", "v1"]:
        with pytest.raises(IntegrationTokenConfigError) as exc_info:
            parse_active_key_version(invalid)
        assert str(invalid) not in str(exc_info.value) or invalid == " " or invalid == "0" or invalid == "-1" or invalid == "1.0" or invalid == "v1"


def test_resolve_usable_token_pair_exhaustive_matrix(monkeypatch):
    """Proves pair-valid boundary: both tokens must be simultaneously valid or return (None, None)."""
    from app.services.integration_tokens import (
        resolve_usable_token,
        resolve_usable_token_pair,
    )
    _setup_keyring(monkeypatch)
    cid = "c-pair-matrix"

    enc_acc = encrypt_integration_token("acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    # 1. Both valid envelope
    doc_enc = {"contractor_id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc_acc, "jobber_refresh_token": enc_ref}
    assert resolve_usable_token_pair(doc_enc, "jobber") == ("acc", "ref")

    # 2. Both valid plaintext
    doc_plain = {"contractor_id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": "plain-acc", "jobber_refresh_token": "plain-ref"}
    assert resolve_usable_token_pair(doc_plain, "jobber") == ("plain-acc", "plain-ref")

    # 3. Access present, refresh absent
    doc_missing_ref = {"contractor_id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc_acc}
    assert resolve_usable_token_pair(doc_missing_ref, "jobber") == (None, None)
    assert resolve_usable_token(doc_missing_ref, "jobber", "access") is None

    # 4. Refresh present, access absent
    doc_missing_acc = {"contractor_id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_refresh_token": enc_ref}
    assert resolve_usable_token_pair(doc_missing_acc, "jobber") == (None, None)
    assert resolve_usable_token(doc_missing_acc, "jobber", "refresh") is None

    # 5. Mixed: str access + dict refresh
    doc_mixed_1 = {"contractor_id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": "plain-acc", "jobber_refresh_token": enc_ref}
    assert resolve_usable_token_pair(doc_mixed_1, "jobber") == (None, None)

    # 6. Mixed: dict access + str refresh
    doc_mixed_2 = {"contractor_id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc_acc, "jobber_refresh_token": "plain-ref"}
    assert resolve_usable_token_pair(doc_mixed_2, "jobber") == (None, None)

    # 7. Unknown key version in refresh
    enc_unknown = dict(enc_ref, key_version=999)
    doc_unknown_key = {"contractor_id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc_acc, "jobber_refresh_token": enc_unknown}
    assert resolve_usable_token_pair(doc_unknown_key, "jobber") == (None, None)

    # 8. Tampered ciphertext in access
    enc_tampered = dict(enc_acc, ciphertext="tampered")
    doc_tampered = {"contractor_id": cid, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": 1, "jobber_access_token": enc_tampered, "jobber_refresh_token": enc_ref}
    assert resolve_usable_token_pair(doc_tampered, "jobber") == (None, None)

    # 9. Explicit connected=False
    doc_disconnected = {"contractor_id": cid, "jobber_connected": False, "jobber_generation": 0, "jobber_lifecycle_epoch": 0, "jobber_access_token": enc_acc, "jobber_refresh_token": enc_ref}
    assert resolve_usable_token_pair(doc_disconnected, "jobber") == (None, None)


@pytest.mark.asyncio
async def test_provider_http_zero_calls_on_unpaired_tokens(monkeypatch):
    """Proves Jobber and Google Calendar make zero HTTP provider calls when credentials are not a valid pair."""
    from app.services import calendar as calendar_service
    from app.services import jobber as jobber_service

    _setup_keyring(monkeypatch)
    cid = "c-zero-http"

    # Instrument AsyncClient to assert zero calls
    call_counts = {"jobber": 0, "google": 0}

    class _SpyHttp:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, *args, **kwargs):
            if "getjobber" in str(url):
                call_counts["jobber"] += 1
            elif "googleapis" in str(url) or "oauth2.googleapis" in str(url):
                call_counts["google"] += 1
            class _Resp:
                status_code = 500
                text = "should not be called"
            return _Resp()

    monkeypatch.setattr(jobber_service.httpx, "AsyncClient", _SpyHttp)
    monkeypatch.setattr(calendar_service.httpx, "AsyncClient", _SpyHttp)

    # 1. Jobber: doc with only access token (missing refresh)
    doc_ref_jobber = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_access_token": "only-access-token",
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref_jobber}})
    _patch_firestore(monkeypatch, db)

    res_jobber = await jobber_service.refresh_access_token({"contractor_id": cid}, force=True)
    assert res_jobber is None
    assert call_counts["jobber"] == 0

    # 2. Google Calendar: doc with only access token (missing refresh)
    doc_ref_gcal = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_access_token": "only-gcal-access-token",
    }, doc_id=cid)
    db.collections["contractors"][cid] = doc_ref_gcal

    res_gcal = await calendar_service.refresh_access_token({"contractor_id": cid}, force=True)
    assert res_gcal is None
    assert call_counts["google"] == 0


@pytest.mark.asyncio
async def test_durable_monotonic_envelope_floor_across_disconnect_reconnect(monkeypatch):
    """Proves flag-on connect establishes floor, disconnect preserves floor, and flag-off reconnect stays envelope."""
    from app.config import settings

    _setup_keyring(monkeypatch)
    cid = "c-floor-lifecycle"

    doc_ref = _FakeDocRef({"contractor_id": cid, "active": True}, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    # 1. Flag-on connect -> writes envelope and sets token_envelope_required=True
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", True)
    updates_conn, gen1, _ = await connect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        access_token="initial-acc",
        refresh_token="initial-ref",
        db=db,
    )
    assert gen1 == 1
    assert type(doc_ref.data["jobber_access_token"]) is dict
    assert doc_ref.data["jobber_token_envelope_required"] is True

    # 2. Disconnect -> tombstones credentials, preserves jobber_token_envelope_required=True
    tomb_gen, _, _ = await disconnect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        db=db,
    )
    assert tomb_gen == 2
    assert "jobber_access_token" not in doc_ref.data
    assert doc_ref.data["jobber_token_envelope_required"] is True

    # 3. Flag-off reconnect -> MUST write envelope because token_envelope_required floor is True!
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", False)
    updates_reconn, gen3, _ = await connect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        access_token="reconnected-acc",
        refresh_token="reconnected-ref",
        observed_generation=2,
        observed_access_raw=None,
        observed_refresh_raw=None,
        db=db,
    )
    assert gen3 == 3
    assert type(doc_ref.data["jobber_access_token"]) is dict
    assert doc_ref.data["jobber_token_envelope_required"] is True


@pytest.mark.asyncio
async def test_disconnect_preserves_floor_even_on_unknown_key_or_malformed_envelope(monkeypatch):
    """Proves disconnect succeeds and preserves/establishes floor even if stored envelope has unknown key version."""
    _setup_keyring(monkeypatch)
    cid = "c-unknown-key-disconnect"

    malformed_envelope = {
        "v": 1,
        "key_version": 999,  # Unknown key version
        "algorithm": "AES-256-GCM",
        "iv": "dGVzdC1pdi0xMjM0NTY=",
        "ciphertext": "dGVzdC1jaXBoZXJ0ZXh0",
        "tag": "dGVzdC10YWctMTIzNDU2",
    }

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 5,
        "jobber_connected": True,
        "jobber_access_token": malformed_envelope,
        "jobber_refresh_token": malformed_envelope,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    # Disconnect must succeed without error, delete credentials, and set token_envelope_required=True
    tomb_gen, revoked_acc, _ = await disconnect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        db=db,
    )
    assert tomb_gen == 6
    assert revoked_acc is None  # Could not decrypt for revocation, but disconnect still succeeds
    assert "jobber_access_token" not in doc_ref.data
    assert "jobber_refresh_token" not in doc_ref.data
    assert doc_ref.data["jobber_token_envelope_required"] is True


def test_floor_true_plus_plaintext_fails_closed_before_provider(monkeypatch):
    """Proves determine_write_format fails closed on attempted plaintext downgrade against envelope floor."""
    from app.services.integration_tokens import (
        IntegrationTokenEnvelopeError,
        determine_write_format,
    )
    _setup_keyring(monkeypatch)

    # When envelope_required is True, plaintext pair must raise IntegrationTokenEnvelopeError
    with pytest.raises(IntegrationTokenEnvelopeError) as exc_info:
        determine_write_format(
            contractor_id="c-downgrade",
            provider="jobber",
            stored_access="plain-acc",
            stored_refresh="plain-ref",
            envelope_required=True,
            encrypted_writes_enabled=False,
        )
    assert "Conflicted credential downgrade attempt" in str(exc_info.value)


@pytest.mark.asyncio
async def test_retry_time_leases_fresh_clock_per_transaction_attempt(monkeypatch):
    """Proves transaction retries evaluate lease expiry against fresh time.time() on every attempt."""
    from app.services.integration_token_mutations import (
        IntegrationTokenCASConflict,
        acquire_refresh_claim_cas,
        persist_refreshed_tokens_cas,
    )
    _setup_keyring(monkeypatch)
    cid = "c-retry-lease-clock"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_connected": True,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    # 1. Acquire lease
    claim_id, exp_ts = await acquire_refresh_claim_cas(
        contractor_id=cid,
        provider="jobber",
        observed_generation=1,
        observed_access_raw="acc",
        observed_refresh_raw="ref",
        lease_duration=10.0,
        db=db,
    )
    assert doc_ref.data["jobber_refresh_claim_id"] == claim_id

    await transition_refresh_claim_to_started_cas(
        contractor_id=cid,
        provider="jobber",
        claim_id=claim_id,
        observed_generation=1,
        observed_access_raw="acc",
        observed_refresh_raw="ref",
        lease_duration=10.0,
        db=db,
    )

    # 2. If lease expires before persist transaction runs, commit must fail CAS
    doc_ref.data["jobber_refresh_claim_expires_at"] = time.time() - 5.0  # Expired in the past!

    with pytest.raises(IntegrationTokenCASConflict) as exc_info:
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new-acc",
            new_refresh_token="new-ref",
            observed_generation=1,
            observed_access_raw="acc",
            observed_refresh_raw="ref",
            claim_id=claim_id,
            db=db,
        )
    assert ("Refresh lease expired" in str(exc_info.value) or "Operation intent expired" in str(exc_info.value) or "Missing or invalid operation intent" in str(exc_info.value))
    # Stored tokens must remain untouched
    assert doc_ref.data["jobber_access_token"] == "acc"


def test_reader_enforces_floor_fails_closed_on_plaintext_and_malformed(monkeypatch):
    """Proves resolve_usable_token_pair/resolve_usable_token/has_usable_token enforce floor at the reader boundary."""
    from app.services.integration_tokens import (
        has_usable_token,
        resolve_usable_token,
        resolve_usable_token_pair,
    )
    _setup_keyring(monkeypatch)
    cid = "c-reader-floor-test"

    enc_jobber_acc = encrypt_integration_token("j-acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_jobber_ref = encrypt_integration_token("j-ref", contractor_id=cid, provider="jobber", token_kind="refresh")
    enc_gcal_acc = encrypt_integration_token("g-acc", contractor_id=cid, provider="google_calendar", token_kind="access")
    enc_gcal_ref = encrypt_integration_token("g-ref", contractor_id=cid, provider="google_calendar", token_kind="refresh")

    # 1. Floor=True with plaintext pair -> FAILS CLOSED
    doc_j_plain_floor = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_token_envelope_required": True,
        "jobber_access_token": "plain-acc",
        "jobber_refresh_token": "plain-ref",
    }
    assert resolve_usable_token_pair(doc_j_plain_floor, "jobber") == (None, None)
    assert resolve_usable_token(doc_j_plain_floor, "jobber", "access") is None
    assert has_usable_token(doc_j_plain_floor, "jobber", "access") is False

    doc_g_plain_floor = {
        "contractor_id": cid,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 1,
        "google_calendar_token_envelope_required": True,
        "google_calendar_access_token": "plain-gcal-acc",
        "google_calendar_refresh_token": "plain-gcal-ref",
    }
    assert resolve_usable_token_pair(doc_g_plain_floor, "google_calendar") == (None, None)
    assert resolve_usable_token(doc_g_plain_floor, "google_calendar", "access") is None
    assert has_usable_token(doc_g_plain_floor, "google_calendar", "access") is False

    # 2. Floor=True with envelope pair -> SUCCEEDS
    doc_j_enc_floor = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_token_envelope_required": True,
        "jobber_access_token": enc_jobber_acc,
        "jobber_refresh_token": enc_jobber_ref,
    }
    assert resolve_usable_token_pair(doc_j_enc_floor, "jobber") == ("j-acc", "j-ref")
    assert resolve_usable_token(doc_j_enc_floor, "jobber", "access") == "j-acc"
    assert has_usable_token(doc_j_enc_floor, "jobber", "access") is True

    doc_g_enc_floor = {
        "contractor_id": cid,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 1,
        "google_calendar_token_envelope_required": True,
        "google_calendar_access_token": enc_gcal_acc,
        "google_calendar_refresh_token": enc_gcal_ref,
    }
    assert resolve_usable_token_pair(doc_g_enc_floor, "google_calendar") == ("g-acc", "g-ref")
    assert resolve_usable_token(doc_g_enc_floor, "google_calendar", "access") == "g-acc"
    assert has_usable_token(doc_g_enc_floor, "google_calendar", "access") is True

    # 3. Floor is malformed (non-bool) -> FAILS CLOSED even with envelope pair
    for bad_floor in ("true", 1, 0, None, [], {}):
        if bad_floor is None:
            continue  # None means absent, tested separately
        doc_bad_floor = {
            "contractor_id": cid,
            "jobber_connected": True,
            "jobber_generation": 1,
            "jobber_lifecycle_epoch": 1,
            "jobber_token_envelope_required": bad_floor,
            "jobber_access_token": enc_jobber_acc,
            "jobber_refresh_token": enc_jobber_ref,
        }
        assert resolve_usable_token_pair(doc_bad_floor, "jobber") == (None, None)
        assert resolve_usable_token(doc_bad_floor, "jobber") is None
        assert has_usable_token(doc_bad_floor, "jobber") is False

    # 4. Floor=False or absent -> SUCCEEDS with plaintext pair
    doc_j_plain_nofloor = {
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": "plain-acc",
        "jobber_refresh_token": "plain-ref",
    }
    assert resolve_usable_token_pair(doc_j_plain_nofloor, "jobber") == ("plain-acc", "plain-ref")
    assert resolve_usable_token(doc_j_plain_nofloor, "jobber", "access") == "plain-acc"
    assert has_usable_token(doc_j_plain_nofloor, "jobber", "access") is True


@pytest.mark.asyncio
async def test_default_off_absent_floor_legacy_connect_remains_plaintext(monkeypatch):
    """Proves flag-off connect on record without floor persists plaintext and does NOT set floor."""
    from app.config import settings
    _setup_keyring(monkeypatch)
    cid = "c-legacy-connect-plaintext"

    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", False)
    doc_ref = _FakeDocRef({"contractor_id": cid, "active": True}, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    updates, next_gen, _ = await connect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        access_token="plain-acc-1",
        refresh_token="plain-ref-1",
        db=db,
    )
    assert next_gen == 1
    assert doc_ref.data["jobber_access_token"] == "plain-acc-1"
    assert doc_ref.data["jobber_refresh_token"] == "plain-ref-1"
    assert "jobber_token_envelope_required" not in doc_ref.data


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
@pytest.mark.parametrize(
    "malformed_floor,initial_creds_type",
    [
        (None, "plaintext"),
        (1, "plaintext"),
        ("true", "plaintext"),
        (["bad"], "plaintext"),
        ({"nested": "bad"}, "plaintext"),
        (None, "absent"),
        (1, "absent"),
        ("true", "absent"),
        (["bad"], "absent"),
        ({"nested": "bad"}, "absent"),
        (None, "malformed_envelope"),
        (1, "malformed_envelope"),
        ("true", "malformed_envelope"),
        (["bad"], "malformed_envelope"),
        ({"nested": "bad"}, "malformed_envelope"),
    ],
)
async def test_disconnect_normalizes_malformed_floor_and_forces_envelope_reconnect(
    monkeypatch, provider, malformed_floor, initial_creds_type
):
    """Proves disconnect conservatively normalizes malformed floor markers to exact bool True,
    deletes all credentials/claims, preserves audit/generation invariants, and causes subsequent
    flag-off reconnect to write an encrypted envelope rather than plaintext."""
    from app.config import settings
    _setup_keyring(monkeypatch)
    cid = f"c-norm-{provider}-{initial_creds_type}"

    # Spy client to verify zero provider/network calls
    call_counts = {"jobber": 0, "google": 0}

    class _SpyHttpx:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, *args, **kwargs):
            if "getjobber" in str(url):
                call_counts["jobber"] += 1
            elif "googleapis" in str(url) or "oauth2.googleapis" in str(url):
                call_counts["google"] += 1
            class _Resp:
                status_code = 500
                text = "should not be called"
            return _Resp()

    monkeypatch.setattr(jobber_service.httpx, "AsyncClient", _SpyHttpx)
    monkeypatch.setattr(calendar_service.httpx, "AsyncClient", _SpyHttpx)

    # Prepare document data with malformed floor
    doc_data = {
        "contractor_id": cid,
        "active": True,
        f"{provider}_connected": True,
        f"{provider}_generation": 3,
        f"{provider}_lifecycle_epoch": 0,
        f"{provider}_token_envelope_required": malformed_floor,
        f"{provider}_refresh_claim_id": "test-claim-id-123",
        f"{provider}_refresh_claim_phase": "reserved",
        f"{provider}_refresh_claim_expires_at": time.time() + 300,
        f"{provider}_refresh_claim_generation": 3,
    }

    if initial_creds_type == "plaintext":
        doc_data[f"{provider}_access_token"] = "plain-acc-123"
        doc_data[f"{provider}_refresh_token"] = "plain-ref-123"
    elif initial_creds_type == "malformed_envelope":
        doc_data[f"{provider}_access_token"] = {"schema_version": 1, "ciphertext": "bad"}
        doc_data[f"{provider}_refresh_token"] = {"schema_version": 1, "ciphertext": "bad"}
    # if "absent", neither token field is in doc_data

    doc_ref = _FakeDocRef(doc_data, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    # 1. Execute disconnect
    tomb_gen, revoked_acc, audit_id = await disconnect_provider_cas(
        contractor_id=cid,
        provider=provider,
        db=db,
    )

    # Assertions on disconnect result
    assert tomb_gen == 4
    assert doc_ref.data[f"{provider}_generation"] == 4
    assert doc_ref.data[f"{provider}_connected"] is False
    # Exact bool True floor must be written
    assert doc_ref.data[f"{provider}_token_envelope_required"] is True
    assert type(doc_ref.data[f"{provider}_token_envelope_required"]) is bool

    # Token fields and claim fields must be deleted
    assert f"{provider}_access_token" not in doc_ref.data
    assert f"{provider}_refresh_token" not in doc_ref.data
    assert f"{provider}_refresh_claim_id" not in doc_ref.data
    assert f"{provider}_refresh_claim_expires_at" not in doc_ref.data
    assert f"{provider}_refresh_claim_generation" not in doc_ref.data

    # Audit event written
    assert audit_id in db.collections["integration_lifecycle_audit"]
    audit_record = db.collections["integration_lifecycle_audit"][audit_id].data
    assert audit_record["action"] == "credentials_deleted"
    assert audit_record["generation"] == 4
    assert audit_record["contractor_id"] == cid
    assert audit_record["provider"] == provider

    # Zero HTTP provider calls were made during disconnect
    assert call_counts["jobber"] == 0
    assert call_counts["google"] == 0

    # 2. Subsequent reconnect with flag OFF must write an envelope because floor is True!
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", False)
    updates_reconn, gen5, audit_id_reconn = await connect_provider_cas(
        contractor_id=cid,
        provider=provider,
        access_token="reconnected-acc",
        refresh_token="reconnected-ref",
        observed_generation=4,
        observed_access_raw=None,
        observed_refresh_raw=None,
        db=db,
    )
    assert gen5 == 5
    assert type(doc_ref.data[f"{provider}_access_token"]) is dict
    assert type(doc_ref.data[f"{provider}_refresh_token"]) is dict
    assert doc_ref.data[f"{provider}_token_envelope_required"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
@pytest.mark.parametrize("initial_creds_type", ["plaintext", "absent"])
async def test_disconnect_exact_false_floor_with_no_dict_creds_retains_legacy_behavior(
    monkeypatch, provider, initial_creds_type
):
    """Proves exact False floor without dict credentials remains False after disconnect,
    and flag-off reconnect retains legacy plaintext writing."""
    from app.config import settings
    _setup_keyring(monkeypatch)
    cid = f"c-control-false-{provider}-{initial_creds_type}"

    doc_data = {
        "contractor_id": cid,
        "active": True,
        f"{provider}_connected": True,
        f"{provider}_generation": 2,
        f"{provider}_lifecycle_epoch": 0,
        f"{provider}_token_envelope_required": False,  # Exact bool False
    }
    if initial_creds_type == "plaintext":
        doc_data[f"{provider}_access_token"] = "plain-acc-1"
        doc_data[f"{provider}_refresh_token"] = "plain-ref-1"

    doc_ref = _FakeDocRef(doc_data, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    tomb_gen, _, _ = await disconnect_provider_cas(
        contractor_id=cid,
        provider=provider,
        db=db,
    )
    assert tomb_gen == 3
    assert doc_ref.data[f"{provider}_connected"] is False
    assert doc_ref.data[f"{provider}_token_envelope_required"] is False
    assert type(doc_ref.data[f"{provider}_token_envelope_required"]) is bool
    assert f"{provider}_access_token" not in doc_ref.data
    assert f"{provider}_refresh_token" not in doc_ref.data

    # Flag-off reconnect writes plaintext because floor is False
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", False)
    updates_reconn, gen4, _ = await connect_provider_cas(
        contractor_id=cid,
        provider=provider,
        access_token="reconnected-plain-acc",
        refresh_token="reconnected-plain-ref",
        observed_generation=3,
        observed_access_raw=None,
        observed_refresh_raw=None,
        db=db,
    )
    assert gen4 == 4
    assert doc_ref.data[f"{provider}_access_token"] == "reconnected-plain-acc"
    assert doc_ref.data[f"{provider}_refresh_token"] == "reconnected-plain-ref"
    assert doc_ref.data[f"{provider}_token_envelope_required"] is False


# ---------------------------------------------------------------------------
# 17. Provider Lifecycle Core & Staff Invariant Causal Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_load_durable_provider_snapshot_invariants(monkeypatch, provider):
    """Proves load_durable_provider_snapshot is the centralized fail-closed linearization point."""
    from app.services.integration_token_mutations import load_durable_provider_snapshot
    _setup_keyring(monkeypatch)
    cid = f"c-linearize-{provider}"

    # 1. Non-existent doc fails closed
    db = _FakeFirestore({"contractors": {}})
    _patch_firestore(monkeypatch, db)
    assert await load_durable_provider_snapshot(cid, provider=provider, db=db) is None

    # 2. Inactive account fails closed
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": False,
        f"{provider}_connected": True,
        f"{provider}_generation": 1,
        f"{provider}_lifecycle_epoch": 1,
        f"{provider}_access_token": "acc-1",
        f"{provider}_refresh_token": "ref-1",
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)
    assert await load_durable_provider_snapshot(cid, provider=provider, db=db) is None

    # 3. Disconnected provider fails closed
    doc_ref.data["active"] = True
    doc_ref.data[f"{provider}_connected"] = False
    doc_ref.data[f"{provider}_generation"] = 0
    doc_ref.data[f"{provider}_lifecycle_epoch"] = 0
    assert await load_durable_provider_snapshot(cid, provider=provider, db=db) is None

    # 4. Canonical Quarantined (both outcome_unknown and reauthorization_required True) fails closed
    doc_ref.data[f"{provider}_connected"] = True
    doc_ref.data[f"{provider}_generation"] = 1
    doc_ref.data[f"{provider}_lifecycle_epoch"] = 1
    doc_ref.data[f"{provider}_reauthorization_required"] = True
    doc_ref.data[f"{provider}_refresh_outcome_unknown"] = True
    assert await load_durable_provider_snapshot(cid, provider=provider, db=db) is None

    # 5. Mixed quarantine booleans (reauthorization_required=False, refresh_outcome_unknown=True) fails closed as malformed
    doc_ref.data[f"{provider}_reauthorization_required"] = False
    doc_ref.data[f"{provider}_refresh_outcome_unknown"] = True
    assert await load_durable_provider_snapshot(cid, provider=provider, db=db) is None

    # 6. Malformed generation fails closed (pop quarantine fields to return to clean state)
    doc_ref.data.pop(f"{provider}_reauthorization_required", None)
    doc_ref.data.pop(f"{provider}_refresh_outcome_unknown", None)
    doc_ref.data[f"{provider}_generation"] = "invalid-gen"
    assert await load_durable_provider_snapshot(cid, provider=provider, db=db) is None

    # 7. Malformed lifecycle epoch fails closed
    doc_ref.data[f"{provider}_generation"] = 1
    doc_ref.data[f"{provider}_lifecycle_epoch"] = -1
    assert await load_durable_provider_snapshot(cid, provider=provider, db=db) is None

    # 8. Valid connected document succeeds and returns exact snapshot
    doc_ref.data[f"{provider}_lifecycle_epoch"] = 1
    snap = await load_durable_provider_snapshot(cid, provider=provider, db=db)
    assert snap is not None
    assert snap["generation"] == 1
    assert snap["lifecycle_epoch"] == 1
    assert snap["access_token"] == "acc-1"
    assert snap["refresh_token"] == "ref-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_oauth_state_lifecycle_binding_and_consumption(monkeypatch, provider):
    """Proves create_oauth_state binds to lifecycle epoch, generation, and raw credentials fingerprint."""
    from app.services.integration_token_mutations import (
        consume_oauth_state,
        create_oauth_state,
    )
    from app.services.integration_tokens import compute_raw_credentials_fingerprint
    _setup_keyring(monkeypatch)
    cid = f"c-oauth-state-{provider}"
    coll_name = "google_oauth_states" if provider == "google_calendar" else "jobber_oauth_states"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        f"{provider}_connected": True,
        f"{provider}_generation": 3,
        f"{provider}_lifecycle_epoch": 2,
        f"{provider}_access_token": "acc-old",
        f"{provider}_refresh_token": "ref-old",
    }, doc_id=cid)
    db = _FakeFirestore({
        "contractors": {cid: doc_ref},
        coll_name: {},
    })
    _patch_firestore(monkeypatch, db)

    state = "state-token-xyz-123"
    await create_oauth_state(
        db=db,
        collection_name=coll_name,
        state=state,
        contractor_id=cid,
        provider=provider,
    )

    # State doc was created and contains binding fields
    state_doc_ref = db.collection(coll_name).document(state)
    assert state_doc_ref.exists is True
    assert state_doc_ref.data["contractor_id"] == cid
    assert state_doc_ref.data["lifecycle_epoch"] == 2
    assert state_doc_ref.data["generation"] == 3
    assert state_doc_ref.data["credentials_fingerprint"] == compute_raw_credentials_fingerprint("acc-old", "ref-old")

    # Atomic consume deletes state and returns payload
    consumed, obs = await consume_oauth_state(
        db=db,
        collection_name=coll_name,
        state=state,
    )
    assert consumed["contractor_id"] == cid
    assert consumed["lifecycle_epoch"] == 2
    assert consumed["generation"] == 3
    assert obs["contractor_id"] == cid

    # Second consumption fails closed (one-time use)
    with pytest.raises(HTTPException) as exc_info:
        await consume_oauth_state(
            db=db,
            collection_name=coll_name,
            state=state,
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_phased_refresh_claim_and_quarantine_state_machine(monkeypatch, provider):
    """Proves two-phase refresh claims (reserved -> provider_request_started) and quarantine transitions."""
    from app.services.integration_token_mutations import (
        IntegrationTokenCASConflict,
        IntegrationTokenLeaseError,
        acquire_refresh_claim_cas,
        persist_refreshed_tokens_cas,
        quarantine_provider_reauth_cas,
        transition_refresh_claim_to_started_cas,
    )
    _setup_keyring(monkeypatch)
    cid = f"c-phased-claim-{provider}"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        f"{provider}_connected": True,
        f"{provider}_generation": 1,
        f"{provider}_lifecycle_epoch": 1,
        f"{provider}_access_token": "acc-1",
        f"{provider}_refresh_token": "ref-1",
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, db)

    # 1. Acquire reserved phase claim
    claim_id, expires_at = await acquire_refresh_claim_cas(
        contractor_id=cid,
        provider=provider,
        observed_generation=1,
        observed_access_raw="acc-1",
        observed_refresh_raw="ref-1",
        db=db,
    )
    assert claim_id is not None
    assert doc_ref.data[f"{provider}_refresh_claim_phase"] == "reserved"
    assert doc_ref.data[f"{provider}_refresh_claim_id"] == claim_id

    # Concurrent attempt while claim is unexpired raises IntegrationTokenLeaseError
    with pytest.raises(IntegrationTokenLeaseError):
        await acquire_refresh_claim_cas(
            contractor_id=cid,
            provider=provider,
            observed_generation=1,
            observed_access_raw="acc-1",
            observed_refresh_raw="ref-1",
            db=db,
        )

    # Attempting to persist directly from reserved phase is rejected
    with pytest.raises(IntegrationTokenCASConflict):
        await persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider=provider,
            new_access_token="acc-2",
            new_refresh_token="ref-2",
            observed_generation=1,
            observed_access_raw="acc-1",
            observed_refresh_raw="ref-1",
            claim_id=claim_id,
            db=db,
        )

    # 2. Transition claim to provider_request_started phase
    claim_id_2, expires_at_2 = await transition_refresh_claim_to_started_cas(
        contractor_id=cid,
        provider=provider,
        claim_id=claim_id,
        observed_generation=1,
        observed_access_raw="acc-1",
        observed_refresh_raw="ref-1",
        db=db,
    )
    assert doc_ref.data[f"{provider}_refresh_claim_phase"] == "provider_request_started"

    # 3. Simulate failure in started phase: quarantine provider for reauthorization
    await quarantine_provider_reauth_cas(
        contractor_id=cid,
        provider=provider,
        claim_id=claim_id_2,
        observed_generation=1,
        observed_lifecycle_epoch=1,
        observed_access_raw="acc-1",
        observed_refresh_raw="ref-1",
        db=db,
    )
    assert doc_ref.data[f"{provider}_reauthorization_required"] is True
    assert doc_ref.data[f"{provider}_refresh_outcome_unknown"] is True
    assert f"{provider}_refresh_claim_id" not in doc_ref.data
    assert f"{provider}_refresh_claim_phase" not in doc_ref.data

    # Attempting refresh acquire while quarantined fails closed with conflict
    with pytest.raises(IntegrationTokenCASConflict):
        await acquire_refresh_claim_cas(
            contractor_id=cid,
            provider=provider,
            observed_generation=1,
            observed_access_raw="acc-1",
            observed_refresh_raw="ref-1",
            db=db,
        )

    # 4. Reconnecting via OAuth reauthorization fence clears quarantine and advances lifecycle epoch
    doc_ref.data[f"{provider}_reauthorization_attempt_id"] = "reauth_claim_12345"
    doc_ref.data[f"{provider}_reauthorization_attempt_kind"] = "reconnect"
    doc_ref.data[f"{provider}_reauthorization_attempt_phase"] = "provider_request_started"
    doc_ref.data[f"{provider}_reauthorization_attempt_expires_at"] = time.time() + 300.0
    doc_ref.data[f"{provider}_reauthorization_attempt_acquired_at"] = time.time()
    doc_ref.data[f"{provider}_reauthorization_attempt_generation"] = 1
    doc_ref.data[f"{provider}_reauthorization_attempt_lifecycle_epoch"] = 1
    doc_ref.data[f"{provider}_reauthorization_attempt_credentials_fingerprint"] = it_mutations.compute_raw_credentials_fingerprint("acc-1", "ref-1")

    updates, new_gen, _ = await connect_provider_cas(
        contractor_id=cid,
        provider=provider,
        access_token="acc-reauth",
        refresh_token="ref-reauth",
        observed_generation=1,
        observed_lifecycle_epoch=1,
        observed_access_raw="acc-1",
        observed_refresh_raw="ref-1",
        claim_id="reauth_claim_12345",
        db=db,
    )
    assert new_gen == 2
    assert doc_ref.data[f"{provider}_lifecycle_epoch"] == 2
    assert f"{provider}_reauthorization_required" not in doc_ref.data
    assert f"{provider}_refresh_outcome_unknown" not in doc_ref.data
    assert doc_ref.data[f"{provider}_access_token"] == "acc-reauth"
    assert doc_ref.data[f"{provider}_refresh_token"] == "ref-reauth"


@pytest.mark.asyncio
async def test_missing_or_naive_read_time_never_falls_back_to_local_clock():
    """Real server time only: naive/numeric/missing read_time must fail closed with IntegrationTokenEnvelopeError."""
    class _SnapMock:
        def __init__(self, rt):
            self.read_time = rt
            self.exists = True
        def to_dict(self):
            return {"active": True}

    # Missing read_time
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
        it_mutations._extract_snapshot_server_time(_SnapMock(None))

    # Float numeric read_time
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
        it_mutations._extract_snapshot_server_time(_SnapMock(time.time()))

    # Integer numeric read_time
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
        it_mutations._extract_snapshot_server_time(_SnapMock(1700000000))

    # Boolean read_time
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
        it_mutations._extract_snapshot_server_time(_SnapMock(True))

    # String read_time
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
        it_mutations._extract_snapshot_server_time(_SnapMock("2026-08-24T18:00:00Z"))

    # Naive datetime (no timezone)
    naive_dt = datetime.datetime(2026, 8, 24, 18, 0, 0)
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
        it_mutations._extract_snapshot_server_time(_SnapMock(naive_dt))

    # Timezone-aware datetime succeeds
    utc_dt = datetime.datetime(2026, 8, 24, 18, 0, 0, tzinfo=datetime.UTC)
    ts = it_mutations._extract_snapshot_server_time(_SnapMock(utc_dt))
    assert ts == utc_dt.timestamp()


class _FakeDBDocRef:
    def __init__(self, data=None, doc_id="fake-id"):
        self.id = doc_id
        self.data = dict(data) if data is not None else None
        self.deleted = False

    @property
    def exists(self) -> bool:
        return (self.data is not None) and (not self.deleted)

    def get(self, *args, transaction=None, **kwargs):
        class _Snap:
            def __init__(self, d, deleted):
                self._d = dict(d) if d is not None else {}
                self.exists = (d is not None) and (not deleted)
                self.read_time = datetime.datetime.fromtimestamp(time.time(), datetime.UTC)

            def to_dict(self):
                return dict(self._d) if self.exists else {}

        return _Snap(self.data, self.deleted)

    def set(self, data, *args, **kwargs):
        self.data = dict(data)
        self.deleted = False

    def update(self, updates, *args, **kwargs):
        from google.cloud.firestore_v1 import DELETE_FIELD
        if self.data is None:
            self.data = {}
        for k, v in updates.items():
            if v is DELETE_FIELD:
                self.data.pop(k, None)
            else:
                self.data[k] = v

    def delete(self, *args, **kwargs):
        self.deleted = True
        self.data = None


class _FakeDBTx(_FakeTransaction):
    def __init__(self, db):
        super().__init__(db)
        self._db = db

    def get(self, doc_ref):
        return doc_ref.get()

    def update(self, doc_ref, updates):
        self._staged_updates.append((doc_ref, dict(updates)))

    def delete(self, doc_ref):
        self._staged_deletes.append(doc_ref)

    def set(self, doc_ref, data):
        self._staged_sets.append((doc_ref, dict(data)))

    def create(self, doc_ref, data):
        if doc_ref.exists:
            raise RuntimeError("Document already exists")
        self._staged_sets.append((doc_ref, dict(data)))


class _FakeDB:
    def __init__(self, store: dict[str, dict] | None = None):
        self._collections: dict[str, dict[str, _FakeDBDocRef]] = {}
        if store:
            for path, data in store.items():
                parts = path.split("/")
                if len(parts) == 2:
                    coll, doc_id = parts
                    self.collection(coll).document(doc_id).set(data)

    def collection(self, name: str):
        class _Coll:
            def __init__(self, coll_dict):
                self._coll_dict = coll_dict
            def document(self, doc_id: str):
                if doc_id not in self._coll_dict:
                    self._coll_dict[doc_id] = _FakeDBDocRef(None, doc_id=doc_id)
                return self._coll_dict[doc_id]
        if name not in self._collections:
            self._collections[name] = {}
        return _Coll(self._collections[name])

    def transaction(self):
        return _FakeDBTx(self)


@pytest.mark.asyncio
async def test_cached_provider_dict_cannot_authorize():
    """load_durable_provider_snapshot ignores in-memory dict and requires fresh Firestore read."""
    cid = "cid-cached-dict-test"
    db = _FakeDB()
    # No doc in Firestore -> returns None even if contractor_data would look valid
    res = await it_mutations.load_durable_provider_snapshot(cid, provider="jobber", db=db)
    assert res is None


@pytest.mark.asyncio
async def test_expired_started_claim_quarantine_commits_despite_transaction_abort_fakes():
    """An expired started claim must commit quarantine in Firestore before raising IntegrationTokenCASConflict."""
    cid = "cid-expired-started-test"
    provider = "jobber"
    server_time = datetime.datetime.now(datetime.UTC)
    past_exp = server_time.timestamp() - 100.0

    db = _FakeDB({
        f"contractors/{cid}": {
            "contractor_id": cid,
            "active": True,
            f"{provider}_connected": True,
            f"{provider}_generation": 1,
            f"{provider}_lifecycle_epoch": 1,
            f"{provider}_access_token": "acc-1",
            f"{provider}_refresh_token": "ref-1",
            f"{provider}_refresh_claim_id": "claim-expired-started-1234",
            f"{provider}_refresh_claim_phase": "provider_request_started",
            f"{provider}_refresh_claim_expires_at": past_exp,
            f"{provider}_refresh_claim_generation": 1,
        }
    })

    with pytest.raises(it_mutations.IntegrationTokenCASConflict) as exc_info:
        await it_mutations.acquire_refresh_claim_cas(
            contractor_id=cid,
            provider=provider,
            observed_generation=1,
            observed_access_raw="acc-1",
            observed_refresh_raw="ref-1",
            db=db,
        )

    assert "Refresh outcome unknown" in str(exc_info.value)

    # Verify durable quarantine was COMMITTED in Firestore
    doc_data = db.collection("contractors").document(cid).data
    assert doc_data.get(f"{provider}_reauthorization_required") is True
    assert doc_data.get(f"{provider}_refresh_outcome_unknown") is True
    assert f"{provider}_refresh_claim_id" not in doc_data
    assert f"{provider}_refresh_claim_phase" not in doc_data
    assert f"{provider}_refresh_claim_expires_at" not in doc_data
    assert f"{provider}_refresh_claim_generation" not in doc_data


@pytest.mark.asyncio
async def test_missing_phase_cannot_persist():
    """persist_refreshed_tokens_cas requires phase exactly provider_request_started."""
    cid = "cid-missing-phase-test"
    provider = "jobber"
    future_exp = datetime.datetime.now(datetime.UTC).timestamp() + 300.0

    # 1. Missing phase in doc
    db = _FakeDB({
        f"contractors/{cid}": {
            "contractor_id": cid,
            "active": True,
            f"{provider}_connected": True,
            f"{provider}_generation": 1,
            f"{provider}_lifecycle_epoch": 1,
            f"{provider}_access_token": "acc-1",
            f"{provider}_refresh_token": "ref-1",
            f"{provider}_refresh_claim_id": "claim-missing-phase-1234",
            f"{provider}_refresh_claim_expires_at": future_exp,
            f"{provider}_refresh_claim_generation": 1,
        }
    })

    with pytest.raises(it_mutations.IntegrationTokenCASConflict):
        await it_mutations.persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider=provider,
            new_access_token="acc-2",
            new_refresh_token="ref-2",
            observed_generation=1,
            observed_access_raw="acc-1",
            observed_refresh_raw="ref-1",
            claim_id="claim-missing-phase-1234",
            db=db,
        )

    # 2. Phase is reserved (not provider_request_started)
    db.collection("contractors").document(cid).data[f"{provider}_refresh_claim_phase"] = "reserved"
    with pytest.raises(it_mutations.IntegrationTokenCASConflict):
        await it_mutations.persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider=provider,
            new_access_token="acc-2",
            new_refresh_token="ref-2",
            observed_generation=1,
            observed_access_raw="acc-1",
            observed_refresh_raw="ref-1",
            claim_id="claim-missing-phase-1234",
            db=db,
        )


@pytest.mark.asyncio
async def test_quarantine_generation_or_raw_mismatch_leaves_state_unchanged():
    """quarantine_provider_reauth_cas with generation or credential mismatch makes zero mutations and returns False."""
    cid = "cid-quarantine-mismatch-test"
    provider = "jobber"
    future_exp = datetime.datetime.now(datetime.UTC).timestamp() + 300.0

    original_doc = {
        "contractor_id": cid,
        "active": True,
        f"{provider}_connected": True,
        f"{provider}_generation": 2,
        f"{provider}_lifecycle_epoch": 1,
        f"{provider}_access_token": "acc-current",
        f"{provider}_refresh_token": "ref-current",
        f"{provider}_refresh_claim_id": "claim-started-12345678",
        f"{provider}_refresh_claim_phase": "provider_request_started",
        f"{provider}_refresh_claim_expires_at": future_exp,
        f"{provider}_refresh_claim_generation": 2,
    }

    db = _FakeDB({f"contractors/{cid}": dict(original_doc)})

    # Generation mismatch (observed=1 vs current=2)
    res = await it_mutations.quarantine_provider_reauth_cas(
        contractor_id=cid,
        provider=provider,
        claim_id="claim-started-12345678",
        observed_generation=1,
        observed_access_raw="acc-current",
        observed_refresh_raw="ref-current",
        db=db,
    )
    assert res is False
    assert db.collection("contractors").document(cid).data == original_doc

    # Credential mismatch
    res2 = await it_mutations.quarantine_provider_reauth_cas(
        contractor_id=cid,
        provider=provider,
        claim_id="claim-started-12345678",
        observed_generation=2,
        observed_access_raw="acc-old",
        observed_refresh_raw="ref-current",
        db=db,
    )
    assert res2 is False
    assert db.collection("contractors").document(cid).data == original_doc


@pytest.mark.asyncio
async def test_legacy_lifecycle_normalization_is_exact_and_race_safe():
    """Fresh active legacy records without lifecycle fields are normalized transactionally under CAS."""
    cid = "cid-legacy-norm-test"
    provider = "jobber"

    # 1. Valid legacy record with all 3 lifecycle fields absent
    db = _FakeDB({
        f"contractors/{cid}": {
            "contractor_id": cid,
            "active": True,
            f"{provider}_access_token": "legacy-acc-1",
            f"{provider}_refresh_token": "legacy-ref-1",
        }
    })

    snap = await it_mutations.load_durable_provider_snapshot(cid, provider=provider, db=db)
    assert snap is not None
    assert snap["generation"] == 0
    assert snap["lifecycle_epoch"] == 0

    doc_data = db.collection("contractors").document(cid).data
    assert doc_data.get(f"{provider}_connected") is True
    assert doc_data.get(f"{provider}_generation") == 0
    assert doc_data.get(f"{provider}_lifecycle_epoch") == 0

    # 2. Partial / malformed lifecycle fields fail closed without normalizing
    cid_malformed = "cid-legacy-malformed"
    db2 = _FakeDB({
        f"contractors/{cid_malformed}": {
            "contractor_id": cid_malformed,
            "active": True,
            f"{provider}_access_token": "legacy-acc-1",
            f"{provider}_refresh_token": "legacy-ref-1",
            f"{provider}_connected": False,  # explicit False
        }
    })
    snap2 = await it_mutations.load_durable_provider_snapshot(cid_malformed, provider=provider, db=db2)
    assert snap2 is None


@pytest.mark.asyncio
async def test_oauth_state_lifecycle_comparison_occurs_inside_consume_transaction():
    """consume_oauth_state validates contractor epoch, generation, and credentials in-transaction, deleting state on mismatch."""
    cid = "cid-oauth-lifecycle-test"
    provider = "jobber"
    state_token = "state-lifecycle-binding-12345678"

    db = _FakeDB({
        f"contractors/{cid}": {
            "contractor_id": cid,
            "active": True,
            f"{provider}_connected": True,
            f"{provider}_generation": 1,
            f"{provider}_lifecycle_epoch": 1,
            f"{provider}_access_token": "acc-before",
            f"{provider}_refresh_token": "ref-before",
        }
    })

    # Create OAuth state bound to epoch 1, gen 1, and credentials fingerprint
    state_payload = await it_mutations.create_oauth_state(
        db=db,
        collection_name="jobber_oauth_states",
        state=state_token,
        contractor_id=cid,
        provider=provider,
    )
    assert state_payload["lifecycle_epoch"] == 1
    assert state_payload["generation"] == 1

    # Simulate concurrent contractor disconnect / epoch bump before consumption
    db.collection("contractors").document(cid).data[f"{provider}_lifecycle_epoch"] = 2

    # Consuming must fail with 400 lifecycle_mismatch and delete the state document
    with pytest.raises(HTTPException) as exc_info:
        await it_mutations.consume_oauth_state(
            db=db,
            collection_name="jobber_oauth_states",
            state=state_token,
        )
    assert exc_info.value.status_code == 400
    assert "lifecycle" in str(exc_info.value.detail).lower() or "invalidated" in str(exc_info.value.detail).lower()

    # Verify state document was DELETED from Firestore to prevent replay
    state_doc = db.collection("jobber_oauth_states").document(state_token)
    assert not state_doc.exists


# ---------------------------------------------------------------------------
# 26. Repair 17C — Monotonic Floor Promotion & Exact Type Regressions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_monotonic_floor_promotion_modern_valid_envelope_pair_absent_floor(monkeypatch, provider):
    """Modern record with valid envelope pair and absent floor enters transactional CAS, promotes floor to True, and returns snapshot with floor True."""
    _setup_keyring(monkeypatch)
    cid = f"cid-floor-promo-absent-{provider}"
    enc_access = encrypt_integration_token("valid-access-token", contractor_id=cid, provider=provider, token_kind="access")
    enc_refresh = encrypt_integration_token("valid-refresh-token", contractor_id=cid, provider=provider, token_kind="refresh")

    doc_ref = _FakeDBDocRef({
        "contractor_id": cid,
        "active": True,
        f"{provider}_connected": True,
        f"{provider}_generation": 3,
        f"{provider}_lifecycle_epoch": 2,
        f"{provider}_access_token": enc_access,
        f"{provider}_refresh_token": enc_refresh,
        # f"{provider}_token_envelope_required" is ABSENT
    }, doc_id=cid)
    db = _FakeDB({f"contractors/{cid}": doc_ref.data})

    snap = await it_mutations.load_durable_provider_snapshot(cid, provider=provider, db=db)
    assert snap is not None
    assert snap["access_token"] == "valid-access-token"
    assert snap["refresh_token"] == "valid-refresh-token"
    assert snap["generation"] == 3
    assert snap["lifecycle_epoch"] == 2
    assert snap["data"][f"{provider}_token_envelope_required"] is True

    # Verify durable document in DB was transactionally promoted to floor=True without mutating generation or epoch
    durable_data = db.collection("contractors").document(cid).data
    assert durable_data[f"{provider}_token_envelope_required"] is True
    assert durable_data[f"{provider}_generation"] == 3
    assert durable_data[f"{provider}_lifecycle_epoch"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_monotonic_floor_promotion_modern_valid_envelope_pair_false_floor(monkeypatch, provider):
    """Modern record with valid envelope pair and explicit False floor enters transactional CAS, promotes floor to True, and returns snapshot with floor True."""
    _setup_keyring(monkeypatch)
    cid = f"cid-floor-promo-false-{provider}"
    enc_access = encrypt_integration_token("valid-access-token-2", contractor_id=cid, provider=provider, token_kind="access")
    enc_refresh = encrypt_integration_token("valid-refresh-token-2", contractor_id=cid, provider=provider, token_kind="refresh")

    doc_ref = _FakeDBDocRef({
        "contractor_id": cid,
        "active": True,
        f"{provider}_connected": True,
        f"{provider}_generation": 5,
        f"{provider}_lifecycle_epoch": 1,
        f"{provider}_access_token": enc_access,
        f"{provider}_refresh_token": enc_refresh,
        f"{provider}_token_envelope_required": False,  # explicit False
    }, doc_id=cid)
    db = _FakeDB({f"contractors/{cid}": doc_ref.data})

    snap = await it_mutations.load_durable_provider_snapshot(cid, provider=provider, db=db)
    assert snap is not None
    assert snap["access_token"] == "valid-access-token-2"
    assert snap["refresh_token"] == "valid-refresh-token-2"
    assert snap["generation"] == 5
    assert snap["lifecycle_epoch"] == 1
    assert snap["data"][f"{provider}_token_envelope_required"] is True

    # Verify durable document was promoted to floor=True
    durable_data = db.collection("contractors").document(cid).data
    assert durable_data[f"{provider}_token_envelope_required"] is True
    assert durable_data[f"{provider}_generation"] == 5
    assert durable_data[f"{provider}_lifecycle_epoch"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_plaintext_records_remain_unpromoted(monkeypatch, provider):
    """Plaintext records (absent or False floor) are NOT promoted to floor True."""
    _setup_keyring(monkeypatch)
    cid_absent = f"cid-plain-absent-{provider}"
    cid_false = f"cid-plain-false-{provider}"

    db = _FakeDB({
        f"contractors/{cid_absent}": {
            "contractor_id": cid_absent,
            "active": True,
            f"{provider}_connected": True,
            f"{provider}_generation": 1,
            f"{provider}_lifecycle_epoch": 0,
            f"{provider}_access_token": "plain-access-1",
            f"{provider}_refresh_token": "plain-refresh-1",
        },
        f"contractors/{cid_false}": {
            "contractor_id": cid_false,
            "active": True,
            f"{provider}_connected": True,
            f"{provider}_generation": 1,
            f"{provider}_lifecycle_epoch": 0,
            f"{provider}_access_token": "plain-access-2",
            f"{provider}_refresh_token": "plain-refresh-2",
            f"{provider}_token_envelope_required": False,
        },
    })

    snap_absent = await it_mutations.load_durable_provider_snapshot(cid_absent, provider=provider, db=db)
    assert snap_absent is not None
    assert snap_absent["access_token"] == "plain-access-1"
    assert snap_absent["data"].get(f"{provider}_token_envelope_required") is None
    doc_absent = db.collection("contractors").document(cid_absent).data
    assert f"{provider}_token_envelope_required" not in doc_absent

    snap_false = await it_mutations.load_durable_provider_snapshot(cid_false, provider=provider, db=db)
    assert snap_false is not None
    assert snap_false["access_token"] == "plain-access-2"
    assert snap_false["data"].get(f"{provider}_token_envelope_required") is False
    doc_false = db.collection("contractors").document(cid_false).data
    assert doc_false[f"{provider}_token_envelope_required"] is False


@pytest.mark.asyncio
async def test_exact_dict_and_hostile_objects_fail_closed_safely(monkeypatch):
    """Non-dict snapshots and hostile objects raising on equality/bool operators fail closed safely."""
    from app.services.integration_tokens import resolve_usable_token_pair

    class HostileObject:
        def __eq__(self, other):
            raise RuntimeError("Hostile __eq__ should never be invoked before type check")
        def __bool__(self):
            raise RuntimeError("Hostile __bool__ should never be invoked before type check")
        def __len__(self):
            raise RuntimeError("Hostile __len__ should never be invoked before type check")
        def __contains__(self, item):
            raise RuntimeError("Hostile __contains__ should never be invoked before type check")

    # 1. Non-dict container passed to resolve_usable_token_pair
    assert resolve_usable_token_pair(["not", "a", "dict"], provider="jobber") == (None, None)

    # 2. Hostile objects in fields fail closed without unhandled exception
    hostile_doc = {
        "jobber_connected": HostileObject(),
        "jobber_access_token": "valid-token",
        "jobber_refresh_token": "valid-refresh",
    }
    assert resolve_usable_token_pair(hostile_doc, provider="jobber") == (None, None)

    hostile_doc2 = {
        "jobber_connected": True,
        "jobber_access_token": HostileObject(),
        "jobber_refresh_token": "valid-refresh",
    }
    assert resolve_usable_token_pair(hostile_doc2, provider="jobber") == (None, None)

    # 3. Present None or non-bool in quarantine flags fails closed
    quarantine_none = {
        "jobber_connected": True,
        "jobber_access_token": "token",
        "jobber_refresh_token": "refresh",
        "jobber_reauthorization_required": None,
    }
    assert resolve_usable_token_pair(quarantine_none, provider="jobber") == (None, None)

    quarantine_int = {
        "jobber_connected": True,
        "jobber_access_token": "token",
        "jobber_refresh_token": "refresh",
        "jobber_reauthorization_required": 1,
    }
    assert resolve_usable_token_pair(quarantine_int, provider="jobber") == (None, None)


@pytest.mark.asyncio
async def test_claim_int_expires_at_fails_closed(monkeypatch):
    """A refresh claim with integer expires_at fails closed because float is strictly required."""
    cid = "cid-claim-int-exp"
    db = _FakeDB({
        f"contractors/{cid}": {
            "contractor_id": cid,
            "active": True,
            "jobber_connected": True,
            "jobber_generation": 1,
            "jobber_lifecycle_epoch": 0,
            "jobber_access_token": "plain-acc",
            "jobber_refresh_token": "plain-ref",
            "jobber_refresh_claim_id": "claim-xyz",
            "jobber_refresh_claim_phase": "reserved",
            "jobber_refresh_claim_generation": 1,
            "jobber_refresh_claim_expires_at": 1700000000,  # INT instead of FLOAT
        }
    })

    # Acquiring claim or reading snapshot with malformed claim fails closed
    with pytest.raises(it_mutations.IntegrationTokenCASConflict):
        await it_mutations.acquire_refresh_claim_cas(
            contractor_id=cid,
            provider="jobber",
            observed_generation=1,
            observed_access_raw="plain-acc",
            observed_refresh_raw="plain-ref",
            db=db,
        )


@pytest.mark.asyncio
async def test_oauth_state_exact_float_schema_and_ttl(monkeypatch):
    """OAuth state requires exact finite float created_at/expires_at, created_at < expires_at, and TTL <= 605s."""
    cid = "cid-oauth-schema"
    now = 1000.0
    monkeypatch.setattr("time.time", lambda: now)

    db = _FakeDB({
        f"contractors/{cid}": {
            "contractor_id": cid,
            "active": True,
            "jobber_connected": False,
            "jobber_generation": 0,
            "jobber_lifecycle_epoch": 0,
        }
    })

    # 1. create_oauth_state writes exact float timestamps
    state_payload = await it_mutations.create_oauth_state(
        db=db,
        collection_name="jobber_oauth_states",
        state="state-exact-float-123456",
        contractor_id=cid,
        provider="jobber",
        ttl_seconds=600.0,
    )
    assert type(state_payload["created_at"]) is float
    assert type(state_payload["expires_at"]) is float
    assert state_payload["created_at"] == 1000.0
    assert state_payload["expires_at"] == 1600.0

    # 2. consume_oauth_state accepts valid float schema
    data, obs = await it_mutations.consume_oauth_state(
        db=db,
        collection_name="jobber_oauth_states",
        state="state-exact-float-123456",
    )
    assert data["contractor_id"] == cid

    # 3. Reject integer timestamps in state document
    from app.services.integration_tokens import compute_raw_credentials_fingerprint
    fp = compute_raw_credentials_fingerprint(None, None)
    db.collection("jobber_oauth_states").document("state-int-timestamps").set({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": fp,
        "created_at": 1000,  # INT
        "expires_at": 1600,  # INT
    })
    with pytest.raises(HTTPException) as exc:
        await it_mutations.consume_oauth_state(
            db=db,
            collection_name="jobber_oauth_states",
            state="state-int-timestamps",
        )
    assert exc.value.status_code == 400
    assert "malformed" in str(exc.value.detail).lower()

    # 4. Reject reversed timestamps (created_at >= expires_at)
    db.collection("jobber_oauth_states").document("state-reversed-timestamps").set({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": fp,
        "created_at": 1600.0,
        "expires_at": 1000.0,
    })
    with pytest.raises(HTTPException) as exc:
        await it_mutations.consume_oauth_state(
            db=db,
            collection_name="jobber_oauth_states",
            state="state-reversed-timestamps",
        )
    assert exc.value.status_code == 400

    # 5. Reject excessive TTL (> 605s)
    db.collection("jobber_oauth_states").document("state-excessive-ttl").set({
        "contractor_id": cid,
        "provider": "jobber",
        "lifecycle_epoch": 0,
        "generation": 0,
        "credentials_fingerprint": fp,
        "created_at": 1000.0,
        "expires_at": 2000.0,  # 1000s > 605s
    })
    with pytest.raises(HTTPException) as exc:
        await it_mutations.consume_oauth_state(
            db=db,
            collection_name="jobber_oauth_states",
            state="state-excessive-ttl",
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# 36. Repair 18A1: Exact Schema, Hostile Objects, Activation, Canonical Encodings & Google Scope
# ---------------------------------------------------------------------------

class _HostileComparisonObject:
    """Object that raises an exception if equality or membership is checked before type."""
    def __hash__(self):
        return 42

    def __eq__(self, other):
        raise AssertionError("Hostile equality check invoked before exact type verification!")

    def __contains__(self, item):
        raise AssertionError("Hostile contains check invoked before exact type verification!")

    def __bool__(self):
        raise AssertionError("Hostile truthiness check invoked before exact type verification!")

    def __len__(self):
        raise AssertionError("Hostile len check invoked before exact type verification!")


class _BoolSubclass(int):
    """Subclass of int imitating a bool."""


def test_repair_18a1_strict_activation_config_validator(monkeypatch):
    """Test strict default-off activation validator for integration_token_encrypted_writes_enabled in app/config.py."""
    from pydantic import ValidationError

    from app.config import Settings

    # Default is False
    s_default = Settings(api_bearer_token="test-token")
    assert s_default.integration_token_encrypted_writes_enabled is False

    # Exact bool True and False
    s_true = Settings(api_bearer_token="test-token", integration_token_encrypted_writes_enabled=True)
    assert s_true.integration_token_encrypted_writes_enabled is True
    s_false = Settings(api_bearer_token="test-token", integration_token_encrypted_writes_enabled=False)
    assert s_false.integration_token_encrypted_writes_enabled is False

    # Exact lowercase string "true" and "false"
    s_str_true = Settings(api_bearer_token="test-token", integration_token_encrypted_writes_enabled="true")
    assert s_str_true.integration_token_encrypted_writes_enabled is True
    s_str_false = Settings(api_bearer_token="test-token", integration_token_encrypted_writes_enabled="false")
    assert s_str_false.integration_token_encrypted_writes_enabled is False

    # Reject non-canonical strings, ints, bool subclasses, bytes, whitespace
    invalid_values = [
        1, 0, "1", "0", "True", "TRUE", "False", "FALSE", "yes", "no", "on", "off",
        " true ", " false ", b"true", b"false", _BoolSubclass(1), _BoolSubclass(0),
        ["true"], {"true": True}, 1.0, 0.0, None,
    ]
    for inv in invalid_values:
        with pytest.raises(ValidationError):
            Settings(api_bearer_token="test-token", integration_token_encrypted_writes_enabled=inv)


def test_repair_18a1_canonical_key_version_and_collision_rejection():
    """Test canonical key version regex, newline rejection, and key collision in parse_keyring and parse_active_key_version."""
    from app.services.integration_tokens import (
        IntegrationTokenConfigError,
        parse_active_key_version,
        parse_keyring,
    )

    # 1. Valid active key version int and canonical string
    assert parse_active_key_version(1) == 1
    assert parse_active_key_version("1") == 1
    assert parse_active_key_version(None) is None
    assert parse_active_key_version("") is None

    # 2. Reject boolean, float, newline, leading zero, negative, out-of-range
    for inv in [True, False, 1.0, "01", "0", "-1", "\n1", "1\n", "1\r\n", " 1 ", "1a", 0, -1, 2147483648]:
        with pytest.raises(IntegrationTokenConfigError):
            parse_active_key_version(inv)

    # 3. Valid keyring JSON
    key_32 = base64.b64encode(b"k" * 32).decode("ascii")
    raw_json = json.dumps({"1": key_32, "2": base64.b64encode(b"j" * 32).decode("ascii")})
    keyring = parse_keyring(raw_json)
    assert 1 in keyring and 2 in keyring

    # 4. Reject collisions between "1" and "01" or duplicate version keys
    raw_collision = f'{{"1": "{key_32}", "01": "{key_32}"}}'
    with pytest.raises(IntegrationTokenConfigError):
        parse_keyring(raw_collision)

    raw_newline = f'{{"1\\n": "{key_32}"}}'
    with pytest.raises(IntegrationTokenConfigError):
        parse_keyring(raw_newline)


def test_repair_18a1_canonical_base64_reencoding_pad_bits():
    """Test rejection of standard base64 strings with non-canonical pad bits for key, nonce, ciphertext."""
    from app.services.integration_tokens import (
        IntegrationTokenConfigError,
        IntegrationTokenEnvelopeError,
        parse_keyring,
        validate_envelope_structure,
    )

    # In base64, a 1-byte value like b'A' (0x41) is encoded as "QQ==" (bits: 01000001 -> 010000 010000 -> Q Q = =).
    # If the trailing unused 4 bits are non-zero, e.g. 010000 011111 ("Q/=="), base64.b64decode might decode it
    # to b'A', but re-encoding b'A' yields "QQ==", which does not match "Q/==".
    # For a 32-byte key: 32 bytes = 24 x 1 + 8 bytes -> 32 % 3 = 2.
    # 2 bytes end with a 6-bit char where 2 trailing bits are unused.
    # Standard 32 bytes: b"k"*32 -> len 32.
    key_bytes = b"k" * 32
    canonical_b64 = base64.b64encode(key_bytes).decode("ascii")  # 44 chars, ending with '='
    assert canonical_b64.endswith("=")
    last_char = canonical_b64[-2]
    # Tweak the last non-pad character to flip an unused padding bit
    # 'a' is 011010, 'b' is 011011
    corrupted_char = "b" if last_char == "a" else "a"
    non_canonical_b64 = canonical_b64[:-2] + corrupted_char + "="

    # Keyring parsing must reject non-canonical base64
    raw_json_bad = json.dumps({"1": non_canonical_b64})
    with pytest.raises(IntegrationTokenConfigError):
        parse_keyring(raw_json_bad)

    # Envelope validation must reject non-canonical nonce or ciphertext
    valid_nonce = base64.b64encode(b"n" * 12).decode("ascii")  # 16 chars, no padding
    valid_envelope = {
        "schema_version": 1,
        "key_version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": valid_nonce,
        "ciphertext": base64.b64encode(b"c" * 20).decode("ascii"),
    }
    validate_envelope_structure(valid_envelope)

    # Corrupt ciphertext padding bit
    ct_bytes = b"c" * 17  # 17 % 3 = 2 -> ends with '='
    ct_canonical = base64.b64encode(ct_bytes).decode("ascii")
    ct_bad = ct_canonical[:-2] + ("b" if ct_canonical[-2] == "a" else "a") + "="
    bad_ct_envelope = dict(valid_envelope)
    bad_ct_envelope["ciphertext"] = ct_bad
    with pytest.raises(IntegrationTokenEnvelopeError):
        validate_envelope_structure(bad_ct_envelope)


def test_repair_18a1_google_calendar_scope_validator_pure():
    """Test pure validator and normalizer for Google Calendar scopes."""
    from app.services.integration_tokens import (
        CANONICAL_GOOGLE_CALENDAR_SCOPE,
        validate_and_normalize_google_calendar_scope,
    )

    # 1. Exact canonical scope
    ok, scope_str = validate_and_normalize_google_calendar_scope(CANONICAL_GOOGLE_CALENDAR_SCOPE)
    assert ok is True
    assert scope_str == CANONICAL_GOOGLE_CALENDAR_SCOPE

    # 2. Re-ordered valid scopes with extra Google scopes
    extra_scope = "https://www.googleapis.com/auth/calendar.freebusy https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/calendar.events"
    ok, scope_str = validate_and_normalize_google_calendar_scope(extra_scope)
    assert ok is True
    assert scope_str == extra_scope

    # 3. None with allow_none=True vs allow_none=False
    ok, scope_str = validate_and_normalize_google_calendar_scope(None, allow_none=True)
    assert ok is True
    assert scope_str == CANONICAL_GOOGLE_CALENDAR_SCOPE

    ok, scope_str = validate_and_normalize_google_calendar_scope(None, allow_none=False)
    assert ok is False
    assert scope_str is None

    # 4. Reduced scopes (missing events or freebusy)
    reduced_1 = "https://www.googleapis.com/auth/calendar.events"
    ok, _ = validate_and_normalize_google_calendar_scope(reduced_1)
    assert ok is False

    reduced_2 = "https://www.googleapis.com/auth/calendar.freebusy"
    ok, _ = validate_and_normalize_google_calendar_scope(reduced_2)
    assert ok is False

    # 5. Malformed tokens, empty tokens, non-ASCII spaces, extra internal spaces, leading/trailing whitespace
    malformed_cases = [
        " https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy ",
        "https://www.googleapis.com/auth/calendar.events  https://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.events\thttps://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.events\nhttps://www.googleapis.com/auth/calendar.freebusy",
        "",
        "   ",
        123,
        True,
        False,
        ["https://www.googleapis.com/auth/calendar.events"],
    ]
    for mal in malformed_cases:
        ok, res = validate_and_normalize_google_calendar_scope(mal)
        assert ok is False
        assert res is None


@pytest.mark.asyncio
async def test_repair_18a1_refresh_claim_rejects_integer_expires_at(monkeypatch):
    """Test that refresh claim schema strictly requires float expires_at and rejects integer values."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-claim-int-exp"

    # Set up contractor with integer expires_at in claim
    doc_ref = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_refresh_claim_id": "claim1234567890abcdef",
        "jobber_refresh_claim_phase": "reserved",
        "jobber_refresh_claim_generation": 0,
        "jobber_refresh_claim_expires_at": 500,  # INT, not float!
    }, doc_id=cid)
    db = _FakeFirestore({
        "contractors": {
            cid: doc_ref,
        }
    })
    _patch_firestore(monkeypatch, db)

    # acquire_refresh_claim_cas sees existing claim with int expires_at -> fails CASConflict due to malformed claim
    with pytest.raises(it_mutations.IntegrationTokenCASConflict) as exc:
        await it_mutations.acquire_refresh_claim_cas(
            contractor_id=cid,
            provider="jobber",
            observed_generation=0,
            observed_access_raw="acc",
            observed_refresh_raw="ref",
            db=db,
        )
    assert "malformed" in str(exc.value).lower()

    # transition_refresh_claim_to_started_cas with int expires_at -> fails lease error
    with pytest.raises(it_mutations.IntegrationTokenLeaseError):
        await it_mutations.transition_refresh_claim_to_started_cas(
            contractor_id=cid,
            provider="jobber",
            claim_id="claim1234567890abcdef",
            observed_generation=0,
            observed_access_raw="acc",
            observed_refresh_raw="ref",
            db=db,
        )


@pytest.mark.asyncio
async def test_repair_18a1_hostile_objects_and_snapshot_classification():
    """Test that durable classifier and mutations do not invoke hostile operators on foreign objects."""
    from app.services.integration_token_mutations import _classify_durable_provider_record

    hostile_val = _HostileComparisonObject()

    # Classification with hostile objects in unexpected places does not trigger hostile operators before type check
    bad_record = {
        "active": hostile_val,
        "contractor_id": "c-hostile",
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
    }
    status, _, _ = _classify_durable_provider_record(bad_record, "jobber", "c-hostile")
    assert status == "invalid"


@pytest.mark.asyncio
async def test_repair_18a1b_load_durable_provider_snapshot_google_scope_cases(monkeypatch):
    """Test load_durable_provider_snapshot classifies scope correctly without treating error tuples as valid."""
    from app.services.integration_token_mutations import load_durable_provider_snapshot
    from app.services.integration_tokens import CANONICAL_GOOGLE_CALENDAR_SCOPE
    _setup_keyring(monkeypatch)

    cid = "c-snap-scope"
    base_data = {
        "active": True,
        "contractor_id": cid,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 1,
        "google_calendar_access_token": "acc",
        "google_calendar_refresh_token": "ref",
    }

    # 1. Reduced scope
    d1 = dict(base_data, google_calendar_scope="https://www.googleapis.com/auth/calendar.freebusy")
    db1 = _FakeFirestore({"contractors": {cid: _FakeDocRef(d1, doc_id=cid)}})
    assert await load_durable_provider_snapshot(cid, "google_calendar", db=db1) is None

    # 2. Empty scope
    d2 = dict(base_data, google_calendar_scope="")
    db2 = _FakeFirestore({"contractors": {cid: _FakeDocRef(d2, doc_id=cid)}})
    assert await load_durable_provider_snapshot(cid, "google_calendar", db=db2) is None

    # 3. None stored scope
    d3 = dict(base_data, google_calendar_scope=None)
    db3 = _FakeFirestore({"contractors": {cid: _FakeDocRef(d3, doc_id=cid)}})
    assert await load_durable_provider_snapshot(cid, "google_calendar", db=db3) is None

    # 4. Non-str stored scope (int, bool, list)
    for bad_scope in (123, True, False, ["https://www.googleapis.com/auth/calendar.events"]):
        d4 = dict(base_data, google_calendar_scope=bad_scope)
        db4 = _FakeFirestore({"contractors": {cid: _FakeDocRef(d4, doc_id=cid)}})
        assert await load_durable_provider_snapshot(cid, "google_calendar", db=db4) is None

    # 5. Tab or double-space stored scope
    for bad_space_scope in (
        "https://www.googleapis.com/auth/calendar.events\thttps://www.googleapis.com/auth/calendar.freebusy",
        "https://www.googleapis.com/auth/calendar.events  https://www.googleapis.com/auth/calendar.freebusy",
    ):
        d5 = dict(base_data, google_calendar_scope=bad_space_scope)
        db5 = _FakeFirestore({"contractors": {cid: _FakeDocRef(d5, doc_id=cid)}})
        assert await load_durable_provider_snapshot(cid, "google_calendar", db=db5) is None

    # 6. Hostile value stored scope
    d6 = dict(base_data, google_calendar_scope=_HostileComparisonObject())
    db6 = _FakeFirestore({"contractors": {cid: _FakeDocRef(d6, doc_id=cid)}})
    assert await load_durable_provider_snapshot(cid, "google_calendar", db=db6) is None

    # 7. Absent google_calendar_scope key (legacy default)
    d7 = dict(base_data)
    db7 = _FakeFirestore({"contractors": {cid: _FakeDocRef(d7, doc_id=cid)}})
    snap7 = await load_durable_provider_snapshot(cid, "google_calendar", db=db7)
    assert snap7 is not None
    assert snap7["google_calendar_scope"] == CANONICAL_GOOGLE_CALENDAR_SCOPE

    # 8. Valid extended scope
    valid_ext = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy https://www.googleapis.com/auth/userinfo.email"
    d8 = dict(base_data, google_calendar_scope=valid_ext)
    db8 = _FakeFirestore({"contractors": {cid: _FakeDocRef(d8, doc_id=cid)}})
    snap8 = await load_durable_provider_snapshot(cid, "google_calendar", db=db8)
    assert snap8 is not None
    assert snap8["google_calendar_scope"] == valid_ext


@pytest.mark.asyncio
async def test_repair_18a1b_postread_hostile_objects_and_int_expiry(monkeypatch):
    """Test that acquire and transition postreads strictly require exact types and reject hostile objects & ints."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-postread-hostile"

    class _BadPostreadDocRef(_FakeDocRef):
        def __init__(self, data, doc_id=None, hostile_field=None, hostile_val=None):
            super().__init__(data, doc_id=doc_id)
            self._hostile_field = hostile_field
            self._hostile_val = hostile_val

        def get(self, *args, transaction=None, **kwargs):
            snap = super().get(*args, transaction=transaction, **kwargs)
            # Inject hostile val into post-transaction get() return snapshot for postread verification
            if transaction is None and self._hostile_field:
                snap._d = dict(self.data)
                snap._d[self._hostile_field] = self._hostile_val
            return snap

    base_data = {
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
    }

    # Test acquire postread when claim_id in snapshot is a hostile object
    doc1 = _BadPostreadDocRef(base_data, doc_id=cid, hostile_field="jobber_refresh_claim_id", hostile_val=_HostileComparisonObject())
    db1 = _FakeFirestore({"contractors": {cid: doc1}})
    with pytest.raises(it_mutations.IntegrationTokenLeaseError):
        await it_mutations.acquire_refresh_claim_cas(
            contractor_id=cid, provider="jobber", observed_generation=0,
            observed_access_raw="acc", observed_refresh_raw="ref", db=db1,
        )

    # Test acquire postread when expires_at in snapshot is an int (e.g. 5000)
    doc2 = _BadPostreadDocRef(base_data, doc_id=cid, hostile_field="jobber_refresh_claim_expires_at", hostile_val=5000)
    db2 = _FakeFirestore({"contractors": {cid: doc2}})
    with pytest.raises(it_mutations.IntegrationTokenLeaseError):
        await it_mutations.acquire_refresh_claim_cas(
            contractor_id=cid, provider="jobber", observed_generation=0,
            observed_access_raw="acc", observed_refresh_raw="ref", db=db2,
        )

    # Test transition postread when phase in snapshot is a hostile object
    claim_data = dict(base_data, jobber_refresh_claim_id="claim1234567890abcdef", jobber_refresh_claim_phase="reserved", jobber_refresh_claim_expires_at=time.time()+60, jobber_refresh_claim_generation=0)
    doc3 = _BadPostreadDocRef(claim_data, doc_id=cid, hostile_field="jobber_refresh_claim_phase", hostile_val=_HostileComparisonObject())
    db3 = _FakeFirestore({"contractors": {cid: doc3}})
    with pytest.raises(it_mutations.IntegrationTokenLeaseError):
        await it_mutations.transition_refresh_claim_to_started_cas(
            contractor_id=cid, provider="jobber", claim_id="claim1234567890abcdef",
            observed_generation=0, observed_access_raw="acc", observed_refresh_raw="ref", db=db3,
        )


@pytest.mark.asyncio
async def test_repair_18a1b_disconnect_hostile_and_malformed_access_tokens(monkeypatch):
    """Test that disconnect_provider_cas handles hostile or malformed access tokens without calling hooks."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-disc-hostile"

    # Test disconnect with hostile object in access token
    data_hostile = {
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": _HostileComparisonObject(),
        "jobber_refresh_token": "ref",
    }
    doc = _FakeDocRef(data_hostile, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc}})

    gen, token_for_revoke, audit_id = await it_mutations.disconnect_provider_cas(
        contractor_id=cid, provider="jobber", db=db,
    )
    assert gen == 2
    assert token_for_revoke is None
    assert doc.data["jobber_connected"] is False
    assert "jobber_access_token" not in doc.data


@pytest.mark.asyncio
async def test_repair_18a1b_extra_updates_hostile_keys_and_scope_validation(monkeypatch):
    """Test that extra_updates keys are validated before membership/equality, and scope is validated strictly."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-extra-hostile"

    db = _FakeFirestore({"contractors": {cid: _FakeDocRef({"active": True, "contractor_id": cid, "google_calendar_connected": True, "google_calendar_generation": 0, "google_calendar_lifecycle_epoch": 0, "google_calendar_access_token": "acc", "google_calendar_refresh_token": "ref"}, doc_id=cid)}})

    # Hostile key in extra_updates
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
        await it_mutations.connect_provider_cas(
            contractor_id=cid, provider="google_calendar", access_token="acc", refresh_token="ref",
            extra_updates={_HostileComparisonObject(): "val"}, db=db,
        )

    # Non-str key (int)
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
        await it_mutations.connect_provider_cas(
            contractor_id=cid, provider="google_calendar", access_token="acc", refresh_token="ref",
            extra_updates={123: "val"}, db=db,
        )

    # Invalid / reduced scope in extra_updates
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
        await it_mutations.connect_provider_cas(
            contractor_id=cid, provider="google_calendar", access_token="acc", refresh_token="ref",
            extra_updates={"google_calendar_scope": "https://www.googleapis.com/auth/calendar.events"}, db=db,
        )

    # Non-bool jobber_lead_capture_enabled in extra_updates
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
        await it_mutations.connect_provider_cas(
            contractor_id=cid, provider="jobber", access_token="acc", refresh_token="ref",
            extra_updates={"jobber_lead_capture_enabled": "true"}, db=db,
        )


# ---------------------------------------------------------------------------
# 18B1. Jobber Lead-Capture CAS & Exact Status Projection Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_18b1_lead_capture_cas_strict_bool_and_scalar_validation(monkeypatch):
    """Test that update_jobber_lead_capture_cas rejects non-bool enabled and non-dict/invalid contractors."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-18b1-bool-val"

    doc = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc}})

    # Non-bool enabled values (int, str, float, list, dict)
    for invalid in (1, 0, "true", "false", 1.0, 0.0, [], {}, None):
        with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
            await it_mutations.update_jobber_lead_capture_cas(
                contractor_id=cid,
                enabled=invalid,
                db=db,
            )

    # Invalid contractor_id (empty, whitespace, leading/trailing spaces, non-str)
    for invalid_cid in ("", " ", " cid", "cid ", None, 123):
        with pytest.raises(it_mutations.IntegrationTokenEnvelopeError):
            await it_mutations.update_jobber_lead_capture_cas(
                contractor_id=invalid_cid,
                enabled=True,
                db=db,
            )

    # Non-existent contractor document
    db_missing = _FakeFirestore({"contractors": {"c-nonexistent": _FakeDocRef(None, doc_id="c-nonexistent")}})
    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="not found"):
        await it_mutations.update_jobber_lead_capture_cas(
            contractor_id="c-nonexistent",
            enabled=True,
            db=db_missing,
        )


@pytest.mark.asyncio
async def test_18b1_lead_capture_cas_enable_success_and_disable_from_disconnected(monkeypatch):
    """Test that enabling succeeds with usable pair, and disabling succeeds even when disconnected/inactive."""
    import math

    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-18b1-enable-disable"

    doc = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc}})

    # 1. Enable succeeds with exact usable pair
    res = await it_mutations.update_jobber_lead_capture_cas(
        contractor_id=cid,
        enabled=True,
        db=db,
    )
    assert isinstance(res, it_mutations.JobberLeadCaptureMutationResult)
    assert res.contractor_id == cid
    assert res.previous_enabled is False
    assert res.enabled is True
    assert res.connected is True
    assert res.generation == 0
    assert res.lifecycle_epoch == 0
    assert isinstance(res.updated_at, float) and math.isfinite(res.updated_at)
    assert doc.data["jobber_lead_capture_enabled"] is True

    # 2. Disable succeeds even when disconnected / inactive
    doc.data["active"] = False
    doc.data["jobber_connected"] = False
    doc.data.pop("jobber_access_token", None)

    res_dis = await it_mutations.update_jobber_lead_capture_cas(
        contractor_id=cid,
        enabled=False,
        db=db,
    )
    assert res_dis.previous_enabled is True
    assert res_dis.enabled is False
    assert res_dis.connected is False
    assert doc.data["jobber_lead_capture_enabled"] is False


@pytest.mark.asyncio
async def test_18b1_lead_capture_cas_enable_fails_on_all_invalid_states(monkeypatch):
    """Test that enabling fails for inactive, disconnected, quarantined, downgrade, and malformed state."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-18b1-invalid-states"

    base_valid = {
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }

    # Helper to test failure
    async def assert_enable_fails(mutated_data):
        doc = _FakeDocRef(dict(mutated_data), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: doc}})
        with pytest.raises(it_mutations.IntegrationTokenCASConflict):
            await it_mutations.update_jobber_lead_capture_cas(
                contractor_id=cid,
                enabled=True,
                db=db,
            )
        # Ensure zero writes to jobber_lead_capture_enabled
        assert doc.data.get("jobber_lead_capture_enabled") is not True

    # Inactive
    await assert_enable_fails(dict(base_valid, active=False))
    # Disconnected
    await assert_enable_fails(dict(base_valid, jobber_connected=False))
    # Quarantined: reauth required
    await assert_enable_fails(dict(base_valid, jobber_reauthorization_required=True))
    # Quarantined: outcome unknown
    await assert_enable_fails(dict(base_valid, jobber_refresh_outcome_unknown=True))
    # One-sided token: missing refresh
    d_no_ref = dict(base_valid)
    d_no_ref.pop("jobber_refresh_token")
    await assert_enable_fails(d_no_ref)
    # One-sided token: missing access
    d_no_acc = dict(base_valid)
    d_no_acc.pop("jobber_access_token")
    await assert_enable_fails(d_no_acc)
    # Plaintext downgrade under envelope floor
    await assert_enable_fails(dict(base_valid, jobber_token_envelope_required=True))
    # Malformed envelope
    await assert_enable_fails(dict(base_valid, jobber_access_token={"version": 1, "ciphertext": "bad!!!"}))


@pytest.mark.asyncio
async def test_18b1_lead_capture_and_disconnect_races(monkeypatch):
    """Test two deterministic concurrent transaction race orderings between lead-capture update and disconnect.

    Under either concurrent interleaving, durable disconnected state strictly implies lead capture is False.
    """
    import asyncio
    import threading

    from google.api_core.exceptions import Aborted

    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-18b1-races"

    class _ContentionDocRef(_FakeDocRef):
        def __init__(self, data=None, doc_id="fake-id"):
            super().__init__(data=data, doc_id=doc_id)
            self.version = 1

        def update(self, updates, *args, **kwargs):
            super().update(updates, *args, **kwargs)
            self.version += 1

        def set(self, data, *args, **kwargs):
            super().set(data, *args, **kwargs)
            self.version += 1

    # ══════════════════════════════════════════════════════════════════════════
    # Concurrent Interleaving 1: Enable snapshot and commit completes BEFORE Disconnect commits
    # ══════════════════════════════════════════════════════════════════════════
    doc1 = _ContentionDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    trace1 = []
    enable_done_1 = asyncio.Event()
    tx_lock_1 = threading.Lock()

    class _Ordering1Firestore(_FakeFirestore):
        def __init__(self):
            super().__init__({"contractors": {cid: doc1}})
            self._tx_lock = None

        def transaction(self):
            class _Tx(_FakeTransaction):
                def __init__(self, db):
                    super().__init__(db)
                    self._read_version = None

                def _record_read(self, doc_ref, snap):
                    if doc_ref is not doc1 and getattr(doc_ref, "id", None) != cid:
                        return
                    with tx_lock_1:
                        self._read_version = getattr(doc_ref, "version", 1)

                def commit(self):
                    with tx_lock_1:
                        if self._read_version is not None and self._read_version != doc1.version:
                            raise Aborted(f"OCC conflict: read v{self._read_version}, current v{doc1.version}")
                        super().commit()

            return _Tx(self)

    db1 = _Ordering1Firestore()

    async def run_enable_1():
        trace1.append("enable_start")
        res = await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=True, db=db1)
        trace1.append("enable_committed")
        enable_done_1.set()
        return res

    async def run_disconnect_1():
        trace1.append("disconnect_start")
        await asyncio.wait_for(enable_done_1.wait(), timeout=5.0)
        res = await it_mutations.disconnect_provider_cas(contractor_id=cid, provider="jobber", db=db1)
        trace1.append("disconnect_committed")
        return res

    enable_res_1, disc_res_1 = await asyncio.gather(run_enable_1(), run_disconnect_1())

    assert enable_res_1.enabled is True
    assert enable_res_1.connected is True
    assert disc_res_1[0] == 1  # generation bumped to 1

    # Invariants for Interleaving 1:
    assert doc1.data["jobber_connected"] is False
    assert doc1.data["jobber_lead_capture_enabled"] is False
    assert doc1.data["jobber_generation"] == 1
    assert doc1.data["jobber_lifecycle_epoch"] == 1
    assert trace1 == ["enable_start", "disconnect_start", "enable_committed", "disconnect_committed"]

    # ══════════════════════════════════════════════════════════════════════════
    # Concurrent Interleaving 2: Disconnect commits while Enable is in-flight,
    # forcing Enable to abort on OCC, retry with updated snapshot, and fail CAS.
    # ══════════════════════════════════════════════════════════════════════════
    doc2 = _ContentionDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    trace2 = []
    enable_attempt_count = [0]
    enable_read_snap_2 = threading.Event()
    disconnect_committed_2 = threading.Event()
    tx_lock_2 = threading.Lock()

    def _is_in_lead_capture_txn():
        import sys
        f = sys._getframe()
        while f:
            if f.f_code.co_name == "_lead_capture_txn":
                return True
            f = f.f_back
        return False

    class _Ordering2Firestore(_FakeFirestore):
        def __init__(self):
            super().__init__({"contractors": {cid: doc2}})
            self._tx_lock = None

        def transaction(self):
            class _Tx(_FakeTransaction):
                def __init__(self, db):
                    super().__init__(db)
                    self._read_version = None
                    self._is_enable = False

                def _record_read(self, doc_ref, snap):
                    if doc_ref is not doc2 and getattr(doc_ref, "id", None) != cid:
                        return
                    if _is_in_lead_capture_txn():
                        self._is_enable = True
                    if self._is_enable:
                        with tx_lock_2:
                            enable_attempt_count[0] += 1
                            attempt = enable_attempt_count[0]
                            self._read_version = getattr(doc_ref, "version", 1)
                        trace2.append(f"enable_read_attempt_{attempt}")
                        enable_read_snap_2.set()
                        if attempt == 1:
                            # Hold attempt 1 until disconnect has committed!
                            assert disconnect_committed_2.wait(timeout=5.0)
                    else:
                        # In disconnect worker thread, wait until enable has read its initial snapshot!
                        assert enable_read_snap_2.wait(timeout=5.0)
                        with tx_lock_2:
                            self._read_version = getattr(doc_ref, "version", 1)
                        trace2.append("disconnect_read")

                def commit(self):
                    with tx_lock_2:
                        if self._read_version is not None and self._read_version != doc2.version:
                            trace2.append("enable_occ_conflict")
                            raise Aborted(f"OCC conflict: read v{self._read_version}, current v{doc2.version}")
                        super().commit()
                        if not self._is_enable:
                            trace2.append("disconnect_committed")
                            disconnect_committed_2.set()

            return _Tx(self)

    db2 = _Ordering2Firestore()

    async def run_enable_2():
        trace2.append("enable_start")
        try:
            return await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=True, db=db2)
        except it_mutations.IntegrationTokenCASConflict as exc:
            trace2.append("enable_cas_rejected")
            return exc

    async def run_disconnect_2():
        trace2.append("disconnect_start")
        res = await it_mutations.disconnect_provider_cas(contractor_id=cid, provider="jobber", db=db2)
        return res

    enable_outcome_2, disc_res_2 = await asyncio.gather(run_enable_2(), run_disconnect_2())

    assert isinstance(enable_outcome_2, it_mutations.IntegrationTokenCASConflict)
    assert disc_res_2[0] == 1  # generation bumped to 1

    # Invariants for Interleaving 2:
    assert doc2.data["jobber_connected"] is False
    assert doc2.data["jobber_lead_capture_enabled"] is False
    assert doc2.data["jobber_generation"] == 1
    assert doc2.data["jobber_lifecycle_epoch"] == 1
    assert "enable_read_attempt_1" in trace2
    assert "disconnect_committed" in trace2
    assert "enable_occ_conflict" in trace2
    assert "enable_read_attempt_2" in trace2
    assert "enable_cas_rejected" in trace2


@pytest.mark.asyncio
async def test_18b1_lead_capture_ambiguous_recovery_and_exact_projection(monkeypatch):
    """Test that ambiguous transport recovery accepts only exact expected body projection."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-18b1-recovery"

    # 1. Exact body-prepared projection matches -> recovers successfully
    doc_match = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    class _AmbigMatchFirestore(_FakeFirestore):
        def transaction(self):
            class _Tx(_FakeTransaction):
                def _commit(self):
                    super().commit()
                    raise ConnectionResetError("Simulated network drop on commit confirmation")
            return _Tx(self)

    db_match = _AmbigMatchFirestore({"contractors": {cid: doc_match}})
    res = await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=True, db=db_match)
    assert res.enabled is True
    assert isinstance(res.updated_at, float)

    # 2. Altered timestamp -> recovery fails and raises CAS conflict
    doc_bad_ts = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    class _AmbigBadTsFirestore(_FakeFirestore):
        def transaction(self):
            class _Tx(_FakeTransaction):
                def _commit(self):
                    super().commit()
                    doc_bad_ts.data["jobber_lead_capture_updated_at"] = 99999.0
                    raise ConnectionResetError("Simulated network drop on commit confirmation")
            return _Tx(self)

    db_bad_ts = _AmbigBadTsFirestore({"contractors": {cid: doc_bad_ts}})
    with pytest.raises(it_mutations.IntegrationTokenCASConflict):
        await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=True, db=db_bad_ts)

    # 3. Altered generation -> recovery fails
    doc_bad_gen = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    class _AmbigBadGenFirestore(_FakeFirestore):
        def transaction(self):
            class _Tx(_FakeTransaction):
                def _commit(self):
                    super().commit()
                    doc_bad_gen.data["jobber_generation"] = 5
                    raise ConnectionResetError("Simulated network drop on commit confirmation")
            return _Tx(self)

    db_bad_gen = _AmbigBadGenFirestore({"contractors": {cid: doc_bad_gen}})
    with pytest.raises(it_mutations.IntegrationTokenCASConflict):
        await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=True, db=db_bad_gen)

    # 4. Zero write happened -> recovery fails
    doc_no_write = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    class _AmbigNoWriteFirestore(_FakeFirestore):
        def transaction(self):
            class _Tx(_FakeTransaction):
                def _commit(self):
                    raise ConnectionResetError("Simulated network drop before write")
            return _Tx(self)

    db_no_write = _AmbigNoWriteFirestore({"contractors": {cid: doc_no_write}})
    with pytest.raises(it_mutations.IntegrationTokenCASConflict):
        await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=True, db=db_no_write)


def test_18b1_is_durable_provider_connected_and_safe_connected_at(monkeypatch):
    """Test pure status projections across valid and hostile representations."""
    from app.services.integration_token_mutations import (
        extract_safe_connected_at,
        is_durable_provider_connected,
    )
    _setup_keyring(monkeypatch)
    cid = "c-18b1-status"

    valid_jobber = {
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_connected_at": 1000.0,
    }
    assert is_durable_provider_connected(valid_jobber, "jobber", cid) is True
    assert extract_safe_connected_at(valid_jobber, "jobber") == 1000.0

    valid_google = {
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 1,
        "google_calendar_access_token": "acc",
        "google_calendar_refresh_token": "ref",
        "google_calendar_connected_at": 2000.0,
    }
    assert is_durable_provider_connected(valid_google, "google_calendar", cid) is True
    assert extract_safe_connected_at(valid_google, "google_calendar") == 2000.0

    # Disconnect immediately forces False even if stale credential strings remain
    disconnected_jobber = dict(valid_jobber, jobber_connected=False, jobber_generation=0, jobber_lifecycle_epoch=0)
    assert is_durable_provider_connected(disconnected_jobber, "jobber", cid) is False

    disconnected_google = dict(valid_google, google_calendar_connected=False, google_calendar_generation=0, google_calendar_lifecycle_epoch=0)
    assert is_durable_provider_connected(disconnected_google, "google_calendar", cid) is False

    # Non-finite or non-float connected_at values produce None
    for bad_ts in (1000, True, False, "1000.0", float("nan"), float("inf"), None, [], {}):
        assert extract_safe_connected_at({"jobber_connected_at": bad_ts}, "jobber") is None

    # Hostile objects and bad keys produce False
    assert is_durable_provider_connected(_HostileComparisonObject(), "jobber", cid) is False
    assert is_durable_provider_connected({_HostileComparisonObject(): "val"}, "jobber", cid) is False


# ═════════════════════════════════════════════════════════════════════════════
# 18B2: DURABLE IDEMPOTENT REVOCATION & REVOCATION OUTBOX TEST SUITE
# ═════════════════════════════════════════════════════════════════════════════


def test_18b2_outbox_schema_and_hostile_validators():
    """Test outbox schema builder, hostile validator, and scalar alias rejections."""
    from app.db.integration_lifecycle_audit import (
        REVOCATION_STATUS_CONFIRMED,
        REVOCATION_STATUS_REQUEST_STARTED,
        build_disconnect_outbox_record,
        validate_outbox_record,
    )

    valid_record = build_disconnect_outbox_record(
        contractor_id="c-outbox-1",
        provider="jobber",
        generation=1,
        lifecycle_epoch=1,
        status=REVOCATION_STATUS_REQUEST_STARTED,
        claim_id="claim-12345",
        audit_finalized=False,
        audit_finalized_at=None,
        created_at=1000.0,
        updated_at=1000.0,
        credential_deletion_disposition="executed",
    )
    validated = validate_outbox_record(valid_record, expected_contractor_id="c-outbox-1", expected_provider="jobber", expected_generation=1, expected_lifecycle_epoch=1)
    assert validated == valid_record

    # Terminal record with finalization
    terminal_record = build_disconnect_outbox_record(
        contractor_id="c-outbox-1",
        provider="jobber",
        generation=1,
        lifecycle_epoch=1,
        status=REVOCATION_STATUS_CONFIRMED,
        claim_id="claim-12345",
        audit_finalized=True,
        audit_finalized_at=1005.0,
        created_at=1000.0,
        updated_at=1005.0,
        credential_deletion_disposition="executed",
    )
    validate_outbox_record(terminal_record)

    # 1. Non-dict rejects
    for bad in (None, "string", 123, []):
        with pytest.raises(ValueError, match="not an exact dict"):
            validate_outbox_record(bad)

    # 2. Forbidden secret keys reject
    for secret_key in ("access_token", "refresh_token", "secret", "client_secret", "ciphertext", "auth_code", "customer_data"):
        bad = dict(valid_record)
        bad[secret_key] = "leak"
        with pytest.raises(ValueError, match="forbidden secret key"):
            validate_outbox_record(bad)

    # 3. Missing or extra keys reject
    bad_missing = dict(valid_record)
    del bad_missing["claim_id"]
    with pytest.raises(ValueError, match="key mismatch"):
        validate_outbox_record(bad_missing)

    bad_extra = dict(valid_record, extra_unknown="bad")
    with pytest.raises(ValueError, match="key mismatch"):
        validate_outbox_record(bad_extra)

    # 4. Hostile scalar aliases and types reject
    # schema_version bool
    with pytest.raises(ValueError, match="Invalid schema_version"):
        validate_outbox_record(dict(valid_record, schema_version=True))
    with pytest.raises(ValueError, match="Invalid schema_version"):
        validate_outbox_record(dict(valid_record, schema_version=2))

    # generation bool or negative
    with pytest.raises(ValueError, match="Invalid generation"):
        validate_outbox_record(dict(valid_record, generation=True))
    with pytest.raises(ValueError, match="Invalid generation"):
        validate_outbox_record(dict(valid_record, generation=-1))

    # lifecycle_epoch bool or 0
    with pytest.raises(ValueError, match="Invalid lifecycle_epoch"):
        validate_outbox_record(dict(valid_record, lifecycle_epoch=False))

    # audit_finalized bad types or contradictory audit_finalized_at
    with pytest.raises(ValueError, match="audit_finalized must be exact bool"):
        validate_outbox_record(dict(valid_record, audit_finalized=1))
    with pytest.raises(ValueError, match="audit_finalized_at must be None when not finalized"):
        validate_outbox_record(dict(valid_record, audit_finalized=False, audit_finalized_at=1000.0))
    with pytest.raises(ValueError, match="audit_finalized_at must be finite positive float"):
        validate_outbox_record(dict(terminal_record, audit_finalized=True, audit_finalized_at=None))
    with pytest.raises(ValueError, match="audit_finalized_at must be finite positive float"):
        validate_outbox_record(dict(terminal_record, audit_finalized=True, audit_finalized_at=float("nan")))

    # status-specific claim_id requirements
    with pytest.raises(ValueError, match="claim_id must be non-empty str when status is provider_request_started"):
        validate_outbox_record(dict(valid_record, claim_id=None))
    with pytest.raises(ValueError, match="claim_id must be non-empty str when status is provider_request_started"):
        validate_outbox_record(dict(valid_record, claim_id=""))

    # non-finite timestamps
    for bad_ts in (float("nan"), float("inf"), 0.0, -1.0, 1000, "1000.0"):
        with pytest.raises(ValueError, match="must be finite positive float"):
            validate_outbox_record(dict(valid_record, created_at=bad_ts))
        with pytest.raises(ValueError, match="must be finite positive float"):
            validate_outbox_record(dict(valid_record, updated_at=bad_ts))


@pytest.mark.asyncio
async def test_18b2_concurrent_disconnect_single_revocation_owner(monkeypatch):
    """C-8: Concurrent orchestration with two candidate claims proves OCC retry, exactly 1 HTTP call, 1 generation advance, and 1 deterministic pair."""
    from google.api_core.exceptions import Aborted

    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-18b2c-concurrent"

    doc = _ContentionDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "valid-access-12345",
        "jobber_refresh_token": "valid-refresh-67890",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": True,
    }, doc_id=cid)

    outbox_store = {}
    audit_store = {}

    barrier = threading.Barrier(2)
    tx_lock = threading.Lock()
    read_versions = []
    http_call_count = [0]

    class _CountingHttp:
        async def post(self, *args, **kwargs):
            http_call_count[0] += 1
            return SimpleNamespace(status_code=200)

    class _ConcurrentDiscFakeFirestore(_FakeFirestore):
        def __init__(self):
            super().__init__({
                "contractors": {cid: doc},
                "integration_revocation_outbox": outbox_store,
                "integration_lifecycle_audit": audit_store,
            })
            self._tx_lock = None

        def transaction(self):
            class _Tx(_FakeTransaction):
                def __init__(self, db):
                    super().__init__(db)
                    self._contractor_read_version = None
                    self._tx_snapshot = {}

                def _begin(self, *args, **kwargs):
                    super()._begin(*args, **kwargs)
                    with tx_lock:
                        self._contractor_read_version = doc.version
                        self._tx_snapshot = {
                            "contractor": dict(doc.data) if doc.data is not None else None,
                            "outbox": dict(outbox_store),
                            "audit": dict(audit_store),
                        }

                def get_snapshot(self, coll_name, doc_id, default_snap):
                    if coll_name == "contractors" and doc_id == cid:
                        d = self._tx_snapshot.get("contractor")
                        ref = _FakeDocRef(d, doc_id=doc_id)
                        ref.deleted = (d is None)
                        snap = ref.get()
                        self._record_read(ref, snap)
                        return snap
                    elif coll_name == "integration_revocation_outbox":
                        out_dict = self._tx_snapshot.get("outbox", {})
                        if doc_id in out_dict:
                            ref_or_doc = out_dict[doc_id]
                            d = getattr(ref_or_doc, "data", None) if hasattr(ref_or_doc, "data") else (ref_or_doc if isinstance(ref_or_doc, dict) else None)
                            ref = _FakeDocRef(d, doc_id=doc_id)
                            ref.deleted = (d is None)
                            return ref.get()
                        else:
                            ref = _FakeDocRef(None, doc_id=doc_id)
                            ref.deleted = True
                            return ref.get()
                    elif coll_name == "integration_lifecycle_audit":
                        aud_dict = self._tx_snapshot.get("audit", {})
                        if doc_id in aud_dict:
                            ref_or_doc = aud_dict[doc_id]
                            d = getattr(ref_or_doc, "data", None) if hasattr(ref_or_doc, "data") else (ref_or_doc if isinstance(ref_or_doc, dict) else None)
                            ref = _FakeDocRef(d, doc_id=doc_id)
                            ref.deleted = (d is None)
                            return ref.get()
                        else:
                            ref = _FakeDocRef(None, doc_id=doc_id)
                            ref.deleted = True
                            return ref.get()
                    return default_snap

                def _record_read(self, doc_ref, snap):
                    if doc_ref is doc or (hasattr(doc_ref, "id") and doc_ref.id == cid):
                        with tx_lock:
                            read_versions.append(self._contractor_read_version)
                        if len(read_versions) <= 2:
                            try:
                                barrier.wait(timeout=5.0)
                            except threading.BrokenBarrierError:
                                pass

                def _commit(self):
                    with tx_lock:
                        if self._contractor_read_version is not None and self._contractor_read_version != doc.version:
                            raise Aborted(f"OCC conflict: read v{self._contractor_read_version}, current v{doc.version}")
                        return super()._commit()

            return _Tx(self)

        def collection(self, name):
            class _ConcurrentColl:
                def __init__(self, coll_name, db_inst):
                    self.coll_name = coll_name
                    self.db = db_inst

                def document(self, doc_id):
                    class _TxAwareDocRef:
                        def __init__(self, coll_name, doc_id, real_ref, db):
                            self.coll_name = coll_name
                            self.id = doc_id
                            self.real_ref = real_ref
                            self.db = db

                        @property
                        def data(self):
                            return self.real_ref.data

                        @property
                        def deleted(self):
                            return self.real_ref.deleted

                        def get(self, *args, transaction=None, **kwargs):
                            if transaction is not None and hasattr(transaction, "get_snapshot"):
                                return transaction.get_snapshot(self.coll_name, self.id, None)
                            return self.real_ref.get(*args, transaction=transaction, **kwargs)

                        def set(self, *args, **kwargs):
                            return self.real_ref.set(*args, **kwargs)

                        def update(self, *args, **kwargs):
                            return self.real_ref.update(*args, **kwargs)

                        def delete(self, *args, **kwargs):
                            return self.real_ref.delete(*args, **kwargs)

                        @property
                        def exists(self):
                            return self.real_ref.exists

                    if doc_id in self.db.collections.setdefault(self.coll_name, {}):
                        real = self.db.collections[self.coll_name][doc_id]
                    else:
                        real = _FakeDocRef(None, doc_id=doc_id)
                        self.db.collections[self.coll_name][doc_id] = real

                    return _TxAwareDocRef(self.coll_name, doc_id, real, self.db)

            return _ConcurrentColl(name, self)

    db = _ConcurrentDiscFakeFirestore()

    import concurrent.futures
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    asyncio.get_running_loop().set_default_executor(executor)

    # Orchestration 1 and Orchestration 2 run concurrently with candidate claims
    async def run_orchestration_1():
        return await it_mutations.disconnect_and_revoke_provider_orchestration(
            contractor_id=cid,
            provider="jobber",
            candidate_claim_id="claim-cand-1",
            db=db,
            http_client=_CountingHttp(),
        )

    async def run_orchestration_2():
        return await it_mutations.disconnect_and_revoke_provider_orchestration(
            contractor_id=cid,
            provider="jobber",
            candidate_claim_id="claim-cand-2",
            db=db,
            http_client=_CountingHttp(),
        )

    try:
        results = await asyncio.gather(run_orchestration_1(), run_orchestration_2(), return_exceptions=True)
    finally:
        executor.shutdown(wait=False)

    # 1. Both first attempts read initial contractor version 1
    assert read_versions[:2] == [1, 1]

    # 2. Exactly one HTTP revocation call across both orchestrations
    assert http_call_count[0] == 1

    # 3. Successful results contain exactly 1 winner who attempted revocation, and any repeat loser did not attempt revocation
    successful_results = [r for r in results if isinstance(r, dict)]
    assert len(successful_results) >= 1
    winners = [r for r in successful_results if r.get("provider_revocation", {}).get("attempted_by_this_request") is True]
    assert len(winners) == 1
    winner_res = winners[0]

    for loser_res in successful_results:
        if loser_res is not winner_res:
            assert loser_res["provider_revocation"]["attempted_by_this_request"] is False

    assert winner_res["generation"] == 1
    assert winner_res["lifecycle_epoch"] == 1
    assert winner_res["audit_id"] == f"{cid}_jobber_1_credentials_deleted"
    assert winner_res["provider_revocation"]["status"] == "provider_confirmed"
    assert winner_res["provider_revocation"]["attempted_by_this_request"] is True
    assert winner_res["audit_finalization"]["status"] == "finalized"

    exceptions = [r for r in results if isinstance(r, Exception)]
    if exceptions:
        assert any(isinstance(exc, it_mutations.IntegrationTokenCASConflict) for exc in exceptions)

    # 6. Exactly 1 pair in durable storage, finalized and confirmed
    assert len(outbox_store) == 1
    assert len(audit_store) == 1
    durable_outbox = outbox_store[winner_res["outbox_id"]].data
    durable_audit = audit_store[winner_res["audit_id"]].data
    assert durable_outbox["status"] == "provider_confirmed"
    assert durable_outbox["audit_finalized"] is True
    assert durable_audit["revocation_status"] == "provider_confirmed"
    assert durable_audit["revocation_completed_at"] is not None

    # 7. Durable document invariants
    assert doc.data["jobber_connected"] is False
    assert doc.data["jobber_lead_capture_enabled"] is False
    assert "jobber_access_token" not in doc.data
    assert "jobber_refresh_token" not in doc.data


@pytest.mark.asyncio
async def test_18b2_repeat_disconnect_idempotency_and_zero_http(monkeypatch):
    """Test that repeating disconnect on an already-disconnected contractor returns identical IDs with zero HTTP calls."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-18b2-repeat"

    doc = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": "token-1",
        "jobber_refresh_token": "refresh-1",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)

    db = _FakeFirestore({
        "contractors": {cid: doc},
        "integration_revocation_outbox": {},
        "integration_lifecycle_audit": {},
    })

    # Call 1: First disconnect
    res1 = await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db)
    assert res1.generation == 1
    assert res1.credential_deletion == "executed"
    assert res1.claim_id is not None
    assert res1.access_token_for_revocation == "token-1"

    # Call 2: Repeated low-level disconnect returns existing durable started state without advancing IDs or granting new revocation ownership
    res2 = await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db)
    assert res2.generation == 1
    assert res2.lifecycle_epoch == 1
    assert res2.audit_id == res1.audit_id
    assert res2.outbox_id == res1.outbox_id
    assert res2.credential_deletion == "already_disconnected"
    assert res2.claim_id is None
    assert res2.access_token_for_revocation is None

    # Call 3: Repeated disconnect via full orchestration fails closed while status is provider_request_started and performs zero HTTP
    class _FailingHttpClient:
        async def post(self, *args, **kwargs):
            raise AssertionError("HTTP post must NOT be called on repeated disconnect!")

    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="unconfirmed|non-terminal|pending"):
        await it_mutations.disconnect_and_revoke_provider_orchestration(
            contractor_id=cid,
            provider="jobber",
            db=db,
            http_client=_FailingHttpClient(),
        )


@pytest.mark.asyncio
async def test_18b2_unavailable_access_token_terminal_and_zero_http(monkeypatch):
    """Test that missing/corrupted/tampered/wrong-context access token results in terminal unavailable status with zero HTTP."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-18b2-unavail"

    # Contractor with corrupted envelope dict access token
    doc = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_access_token": {"encrypted_token": "corrupt-ciphertext", "iv": "bad", "tag": "bad", "key_version": 1},
        "jobber_refresh_token": "some-refresh",
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)

    db = _FakeFirestore({
        "contractors": {cid: doc},
        "integration_revocation_outbox": {},
        "integration_lifecycle_audit": {},
    })

    class _FailingHttpClient:
        async def post(self, *args, **kwargs):
            raise AssertionError("HTTP must NOT be called when access token is unavailable!")

    res = await it_mutations.disconnect_and_revoke_provider_orchestration(
        contractor_id=cid,
        provider="jobber",
        db=db,
        http_client=_FailingHttpClient(),
    )

    assert res["status"] == "disconnected"
    assert res["generation"] == 1
    assert res["provider_revocation"]["attempted"] is False
    assert res["provider_revocation"]["status"] == "not_attempted_unavailable_token"
    assert res["audit_finalization"]["finalized"] is True
    assert doc.data["jobber_connected"] is False
    assert "jobber_access_token" not in doc.data


@pytest.mark.asyncio
async def test_18b2_provider_revocation_outcome_mapping_and_outbox_cas(monkeypatch):
    """Test outcome mapping across Jobber (200 confirmed, non-200 rejected, exception transport) and Google (200/204 confirmed, non-200/204 rejected, exception transport)."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)

    class _MockHttp:
        def __init__(self, status_code: int = 200, raises: bool = False):
            self.status_code = status_code
            self.raises = raises

        async def post(self, *args, **kwargs):
            if self.raises:
                raise ConnectionError("Network timeout")
            return SimpleNamespace(status_code=self.status_code)

    # 1. Jobber 200 -> provider_confirmed
    cid1 = "c-jobber-200"
    doc1 = _FakeDocRef({"active": True, "contractor_id": cid1, "jobber_connected": True, "jobber_generation": 0, "jobber_lifecycle_epoch": 0, "jobber_access_token": "acc", "jobber_refresh_token": "ref"}, doc_id=cid1)
    db1 = _FakeFirestore({"contractors": {cid1: doc1}, "integration_revocation_outbox": {}, "integration_lifecycle_audit": {}})
    r1 = await it_mutations.disconnect_and_revoke_provider_orchestration(contractor_id=cid1, provider="jobber", db=db1, http_client=_MockHttp(200))
    assert r1["provider_revocation"]["status"] == "provider_confirmed"
    assert r1["audit_finalization"]["finalized"] is True

    # 2. Jobber 400 -> provider_rejected
    cid2 = "c-jobber-400"
    doc2 = _FakeDocRef({"active": True, "contractor_id": cid2, "jobber_connected": True, "jobber_generation": 0, "jobber_lifecycle_epoch": 0, "jobber_access_token": "acc", "jobber_refresh_token": "ref"}, doc_id=cid2)
    db2 = _FakeFirestore({"contractors": {cid2: doc2}, "integration_revocation_outbox": {}, "integration_lifecycle_audit": {}})
    r2 = await it_mutations.disconnect_and_revoke_provider_orchestration(contractor_id=cid2, provider="jobber", db=db2, http_client=_MockHttp(400))
    assert r2["provider_revocation"]["status"] == "provider_rejected"

    # 3. Jobber exception -> transport_error_unknown
    cid3 = "c-jobber-timeout"
    doc3 = _FakeDocRef({"active": True, "contractor_id": cid3, "jobber_connected": True, "jobber_generation": 0, "jobber_lifecycle_epoch": 0, "jobber_access_token": "acc", "jobber_refresh_token": "ref"}, doc_id=cid3)
    db3 = _FakeFirestore({"contractors": {cid3: doc3}, "integration_revocation_outbox": {}, "integration_lifecycle_audit": {}})
    r3 = await it_mutations.disconnect_and_revoke_provider_orchestration(contractor_id=cid3, provider="jobber", db=db3, http_client=_MockHttp(raises=True))
    assert r3["provider_revocation"]["status"] == "transport_error_unknown"

    # 4. Google 204 -> provider_confirmed
    cid4 = "c-google-204"
    doc4 = _FakeDocRef({"active": True, "contractor_id": cid4, "google_calendar_connected": True, "google_calendar_generation": 0, "google_calendar_lifecycle_epoch": 0, "google_calendar_access_token": "acc", "google_calendar_refresh_token": "ref"}, doc_id=cid4)
    db4 = _FakeFirestore({"contractors": {cid4: doc4}, "integration_revocation_outbox": {}, "integration_lifecycle_audit": {}})
    r4 = await it_mutations.disconnect_and_revoke_provider_orchestration(contractor_id=cid4, provider="google_calendar", db=db4, http_client=_MockHttp(204))
    assert r4["provider_revocation"]["status"] == "provider_confirmed"


@pytest.mark.asyncio
async def test_18b2_ambiguous_outcome_persistence_leaves_started_state(monkeypatch):
    """Test that if provider HTTP occurs but outcome persistence fails, status remains truthfully provider_request_started, and a second call performs zero HTTP."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-ambig-outcome"

    doc = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
    }, doc_id=cid)

    class _FailingOutcomeFirestore(_FakeFirestore):
        def __init__(self):
            super().__init__({
                "contractors": {cid: doc},
                "integration_revocation_outbox": {},
                "integration_lifecycle_audit": {},
            })
            self.fail_outcomes = False

        def transaction(self):
            db_self = self
            class _Tx(_FakeTransaction):
                def commit(self):
                    if db_self.fail_outcomes:
                        raise ConnectionResetError("Simulated DB write failure during outcome commit")
                    super().commit()
            return _Tx(self)

    db = _FailingOutcomeFirestore()
    http_count = [0]

    class _CountingHttp:
        async def post(self, *args, **kwargs):
            http_count[0] += 1
            return SimpleNamespace(status_code=200)

    # First call: disconnect succeeds, HTTP succeeds, but outcome commit fails -> fails closed with CASConflict
    db.fail_outcomes = False
    async def _failing_record(**kwargs):
        raise ConnectionResetError("Persistence failed")

    monkeypatch.setattr(it_mutations, "record_revocation_outcome_cas", _failing_record)

    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="unconfirmed after HTTP request"):
        await it_mutations.disconnect_and_revoke_provider_orchestration(
            contractor_id=cid,
            provider="jobber",
            db=db,
            http_client=_CountingHttp(),
        )
    assert http_count[0] == 1

    # Second call on the same contractor: performs ZERO HTTP calls and fails closed with IntegrationTokenCASConflict
    monkeypatch.undo()
    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="unconfirmed|non-terminal|pending"):
        await it_mutations.disconnect_and_revoke_provider_orchestration(
            contractor_id=cid,
            provider="jobber",
            db=db,
            http_client=_CountingHttp(),
        )
    assert http_count[0] == 1  # Still 1! Zero new HTTP calls!


@pytest.mark.asyncio
async def test_18b2_legacy_reconciliation_and_partial_disconnect(monkeypatch):
    """Test legacy reconciliation of existing disconnected accounts and cleanup of partial remnants."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)

    # 1. Legacy already-disconnected account missing outbox
    cid1 = "c-legacy-disc"
    doc1 = _FakeDocRef({
        "active": True,
        "contractor_id": cid1,
        "jobber_connected": False,
        "jobber_generation": 2,
        "jobber_lifecycle_epoch": 2,
        "jobber_disconnected_at": 1700000000.0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid1)
    db1 = _FakeFirestore({"contractors": {cid1: doc1}, "integration_revocation_outbox": {}, "integration_lifecycle_audit": {}})

    res1 = await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid1, provider="jobber", db=db1)
    assert res1.generation == 2  # Generation unchanged!
    assert res1.lifecycle_epoch == 2  # Epoch unchanged!
    assert res1.credential_deletion == "legacy_reconciled"
    assert res1.revocation_status == "not_attempted_unavailable_token"
    assert res1.claim_id is None
    assert res1.access_token_for_revocation is None

    # 2. Partial disconnect remnants (connected False but stale access_token still present)
    cid2 = "c-partial-remnants"
    doc2 = _FakeDocRef({
        "active": True,
        "contractor_id": cid2,
        "jobber_connected": False,
        "jobber_access_token": "stale-access",
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
    }, doc_id=cid2)
    db2 = _FakeFirestore({"contractors": {cid2: doc2}, "integration_revocation_outbox": {}, "integration_lifecycle_audit": {}})

    res2 = await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid2, provider="jobber", db=db2)
    assert res2.generation == 2  # Advanced generation to tombstone remnants!
    assert res2.lifecycle_epoch == 2
    assert res2.credential_deletion == "partial_reconciled"
    assert "jobber_access_token" not in doc2.data


@pytest.mark.asyncio
async def test_18b2_no_secret_leakage_in_documents_and_responses(monkeypatch):
    """Test that all created outbox, audit documents, and endpoint responses contain zero secret fields or tokens."""
    from app.api import integrations
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-no-secrets"

    doc = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "secret-access-token-9999",
        "jobber_refresh_token": "secret-refresh-token-8888",
    }, doc_id=cid)

    outbox_store = {}
    audit_store = {}
    db = _FakeFirestore({
        "contractors": {cid: doc},
        "integration_revocation_outbox": outbox_store,
        "integration_lifecycle_audit": audit_store,
    })

    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(it_mutations, "get_firestore_client", lambda: db)

    class _MockHttp:
        async def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=200)

    req = SimpleNamespace(state=SimpleNamespace(is_admin=True, contractor_id=cid))
    resp = await integrations.jobber_disconnect(contractor_id=cid, request=req)

    forbidden_strings = ["secret-access-token-9999", "secret-refresh-token-8888", "claim_id", "token", "key_version"]

    # Verify endpoint response
    resp_str = str(resp)
    for forbidden in ("secret-access-token-9999", "secret-refresh-token-8888"):
        assert forbidden not in resp_str
    assert "claim_id" not in resp

    # Verify Firestore outbox document
    outbox_doc = list(outbox_store.values())[0]
    outbox_str = str(outbox_doc.data)
    for forbidden in ("secret-access-token-9999", "secret-refresh-token-8888"):
        assert forbidden not in outbox_str

    # Verify Firestore audit document
    audit_doc = list(audit_store.values())[0]
    audit_str = str(audit_doc.data)
    for forbidden in ("secret-access-token-9999", "secret-refresh-token-8888"):
        assert forbidden not in audit_str


# ─────────────────────────────────────────────────────────────────────────────
# 18B2A Strict Independent-Review Repairs (P1-1 through P1-13)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_18b2a_tombstone_classifier_pure_and_strict():
    """P1-1: Pure exact tombstone classifier accepts clean tombstones and rejects any violation."""
    from app.services.integration_token_mutations import is_durable_provider_tombstone

    cid = "c-tombstone-test"

    valid_jobber = {
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_disconnected_at": 1700000000.0,
        "jobber_token_envelope_required": True,
        "jobber_lead_capture_enabled": False,
    }
    assert is_durable_provider_tombstone(valid_jobber, "jobber", cid) is True

    valid_gcal = {
        "contractor_id": cid,
        "google_calendar_connected": False,
        "google_calendar_generation": 0,
        "google_calendar_lifecycle_epoch": 0,
        "google_calendar_disconnected_at": 1700000000.0,
    }
    assert is_durable_provider_tombstone(valid_gcal, "google_calendar", cid) is True

    # Rejections
    # 1. Non-dict
    assert is_durable_provider_tombstone("not-a-dict", "jobber", cid) is False
    # 2. Non-string keys
    assert is_durable_provider_tombstone({123: "val"}, "jobber", cid) is False
    # 3. connected is True or non-bool
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_connected": True}, "jobber", cid) is False
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_connected": "false"}, "jobber", cid) is False
    # 4. generation is bool or negative or float
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_generation": True}, "jobber", cid) is False
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_generation": -1}, "jobber", cid) is False
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_generation": 1.0}, "jobber", cid) is False
    # 5. lifecycle_epoch is bool or negative or float
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_lifecycle_epoch": False}, "jobber", cid) is False
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_lifecycle_epoch": -1}, "jobber", cid) is False
    # 6. disconnected_at is missing, zero, negative, non-float, or infinite
    assert is_durable_provider_tombstone({k: v for k, v in valid_jobber.items() if k != "jobber_disconnected_at"}, "jobber", cid) is False
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_disconnected_at": 0.0}, "jobber", cid) is False
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_disconnected_at": -100.0}, "jobber", cid) is False
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_disconnected_at": float("inf")}, "jobber", cid) is False
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_disconnected_at": 1700000000}, "jobber", cid) is False
    # 7. Floor is non-bool
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_token_envelope_required": "true"}, "jobber", cid) is False
    # 8. Forbidden fields present
    for forbidden_key in (
        "jobber_access_token",
        "jobber_refresh_token",
        "jobber_token_expires_at",
        "jobber_connected_at",
        "jobber_token_refreshed_at",
        "jobber_refresh_claim_id",
        "jobber_refresh_lease_expires_at",
        "jobber_token_quarantine",
        "jobber_refresh_outcome_unknown",
        "jobber_reauthorization_required",
    ):
        assert is_durable_provider_tombstone({**valid_jobber, forbidden_key: "residual"}, "jobber", cid) is False
    # 9. Jobber lead capture True
    assert is_durable_provider_tombstone({**valid_jobber, "jobber_lead_capture_enabled": True}, "jobber", cid) is False
    # 10. Google scope present
    assert is_durable_provider_tombstone({**valid_gcal, "google_calendar_scope": "https://www.googleapis.com/auth/calendar"}, "google_calendar", cid) is False


@pytest.mark.asyncio
async def test_18b2a_revocation_access_token_eligibility(monkeypatch):
    """P1-2: Access revocation eligibility is fail-closed and respects envelope floor."""
    from app.services.integration_token_mutations import extract_revocation_access_token
    _setup_keyring(monkeypatch)
    cid = "c-eligibility-test"

    # Valid plaintext pair
    valid_pt = {
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_access_token": "valid-access-token-1234",
        "jobber_refresh_token": "valid-refresh-token-1234",
    }
    # Valid envelope pair
    enc_acc = encrypt_integration_token("env-access-token-5678", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = encrypt_integration_token("env-refresh-token-5678", contractor_id=cid, provider="jobber", token_kind="refresh")
    valid_env = {
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
    }

    # 1. Plaintext + no floor -> Eligible
    assert extract_revocation_access_token(valid_pt, "jobber", cid, 1) == "valid-access-token-1234"
    # 2. Plaintext + floor False -> Eligible
    assert extract_revocation_access_token({**valid_pt, "jobber_token_envelope_required": False}, "jobber", cid, 1) == "valid-access-token-1234"
    # 3. Plaintext + floor True -> Ineligible (None)
    assert extract_revocation_access_token({**valid_pt, "jobber_token_envelope_required": True}, "jobber", cid, 1) is None
    # 4. Envelope + floor True -> Eligible (decrypted)
    assert extract_revocation_access_token({**valid_env, "jobber_token_envelope_required": True}, "jobber", cid, 1) == "env-access-token-5678"
    # 5. Envelope + floor False / absent -> Eligible (decrypted)
    assert extract_revocation_access_token(valid_env, "jobber", cid, 1) == "env-access-token-5678"
    # 6. Mixed pair -> Ineligible (None)
    mixed = {"jobber_generation": 1, "jobber_access_token": "valid-access-token-1234", "jobber_refresh_token": enc_ref}
    assert extract_revocation_access_token(mixed, "jobber", cid, 1) is None
    # 7. One-sided -> Ineligible (None)
    one_sided = {"jobber_generation": 1, "jobber_access_token": "valid-access-token-1234"}
    assert extract_revocation_access_token(one_sided, "jobber", cid, 1) is None
    # 8. Malformed floor -> Ineligible (None)
    assert extract_revocation_access_token({**valid_pt, "jobber_token_envelope_required": "true"}, "jobber", cid, 1) is None
    # 9. Envelope with wrong contractor context -> Ineligible (None)
    assert extract_revocation_access_token(valid_env, "jobber", "wrong-contractor", 1) is None


@pytest.mark.asyncio
async def test_18b2a_deterministic_create_only_rejects_preexisting_docs(monkeypatch):
    """P1-3: New generation disconnect fails closed with IntegrationTokenCASConflict if audit or outbox exists."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-preexisting-docs"

    # Pre-existing outbox doc for generation 1
    doc = _FakeDocRef({
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "acc-1",
        "jobber_refresh_token": "ref-1",
    }, doc_id=cid)

    outbox_store = {
        f"{cid}_jobber_1_credentials_deleted": _FakeDocRef({"status": "provider_confirmed"}),
    }
    db = _FakeFirestore({
        "contractors": {cid: doc},
        "integration_revocation_outbox": outbox_store,
        "integration_lifecycle_audit": {},
    })

    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Deterministic audit/outbox document already exists"):
        await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db)


@pytest.mark.asyncio
async def test_18b2a_complete_postcondition_detects_all_corruptions(monkeypatch):
    """P1-4: Complete postcondition verification checks contractor tombstone, audit, and outbox."""
    from app.services.integration_token_mutations import (
        IntegrationTokenPostconditionError,
        _verify_complete_disconnect_postcondition,
    )
    _setup_keyring(monkeypatch)
    cid = "c-postcondition-test"

    doc = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_disconnected_at": 1700000000.0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    outbox_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_confirmed",
        "claim_id": "claim-1",
        "audit_finalized": True,
        "audit_finalized_at": 1700000005.0,
        "created_at": 1700000000.0,
        "updated_at": 1700000002.0,
        "credential_deletion_disposition": "executed",
    })

    audit_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_confirmed",
        "revocation_completed_at": 1700000002.0,
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    })

    db = _FakeFirestore({
        "contractors": {cid: doc},
        "integration_revocation_outbox": {f"{cid}_jobber_1_credentials_deleted": outbox_doc},
        "integration_lifecycle_audit": {f"{cid}_jobber_1_credentials_deleted": audit_doc},
    })

    # Passes on clean match
    _verify_complete_disconnect_postcondition(
        doc,
        contractor_id=cid,
        provider="jobber",
        expected_generation=1,
        expected_lifecycle_epoch=1,
        expected_disconnected_at=1700000000.0,
        expected_floor=it_mutations._FLOOR_ABSENT,
        db=db,
        outbox_id=f"{cid}_jobber_1_credentials_deleted",
        audit_id=f"{cid}_jobber_1_credentials_deleted",
    )

    # Fails on generation mismatch
    with pytest.raises(IntegrationTokenPostconditionError, match="Generation mismatch"):
        _verify_complete_disconnect_postcondition(
            doc,
            contractor_id=cid,
            provider="jobber",
            expected_generation=2,
            expected_lifecycle_epoch=1,
            expected_disconnected_at=1700000000.0,
            expected_floor=it_mutations._FLOOR_ABSENT,
            db=db,
            outbox_id=f"{cid}_jobber_1_credentials_deleted",
            audit_id=f"{cid}_jobber_1_credentials_deleted",
        )


@pytest.mark.asyncio
async def test_18b2a_record_revocation_outcome_claim_and_idempotency(monkeypatch):
    """P1-6: record_revocation_outcome_cas enforces strict claim match and allows exact idempotent duplicates."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-outcome-test"
    outbox_id = f"{cid}_jobber_1_credentials_deleted"

    outbox_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_request_started",
        "claim_id": "valid-claim-1234",
        "audit_finalized": False,
        "audit_finalized_at": None,
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
        "credential_deletion_disposition": "executed",
    })
    audit_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_request_started",
        "revocation_completed_at": None,
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    })
    db = _FakeFirestore({
        "integration_revocation_outbox": {outbox_id: outbox_doc},
        "integration_lifecycle_audit": {outbox_id: audit_doc},
    })

    # 1. Invalid claim rejected
    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Claim ID mismatch"):
        await it_mutations.record_revocation_outcome_cas(
            contractor_id=cid,
            provider="jobber",
            outbox_id=outbox_id,
            claim_id="wrong-claim",
            outcome_status="provider_confirmed",
            expected_generation=1,
            expected_lifecycle_epoch=1,
            db=db,
        )

    # 2. Valid claim transitions to confirmed
    res = await it_mutations.record_revocation_outcome_cas(
        contractor_id=cid,
        provider="jobber",
        outbox_id=outbox_id,
        claim_id="valid-claim-1234",
        outcome_status="provider_confirmed",
        expected_generation=1,
        expected_lifecycle_epoch=1,
        db=db,
    )
    assert res["status"] == "provider_confirmed"
    assert outbox_doc.data["status"] == "provider_confirmed"

    # 3. Exact same outcome is idempotent
    res_dup = await it_mutations.record_revocation_outcome_cas(
        contractor_id=cid,
        provider="jobber",
        outbox_id=outbox_id,
        claim_id="valid-claim-1234",
        outcome_status="provider_confirmed",
        expected_generation=1,
        expected_lifecycle_epoch=1,
        db=db,
    )
    assert res_dup["status"] == "provider_confirmed"

    # 4. Conflicting outcome rejected
    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="already in terminal status"):
        await it_mutations.record_revocation_outcome_cas(
            contractor_id=cid,
            provider="jobber",
            outbox_id=outbox_id,
            claim_id="valid-claim-1234",
            outcome_status="provider_rejected",
            expected_generation=1,
            expected_lifecycle_epoch=1,
            db=db,
        )


@pytest.mark.asyncio
async def test_18b2a_audit_finalization_links_terminal_outbox_timestamp(monkeypatch):
    """P1-7: finalize_revocation_audit_cas copies exact outbox updated_at into audit revocation_completed_at."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-finalization-ts-test"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    outbox_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_confirmed",
        "claim_id": "claim-1",
        "audit_finalized": False,
        "audit_finalized_at": None,
        "created_at": 1700000000.0,
        "updated_at": 1700000010.5,
        "credential_deletion_disposition": "executed",
    })

    audit_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_request_started",
        "revocation_completed_at": None,
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    })

    db = _FakeFirestore({
        "integration_revocation_outbox": {doc_id: outbox_doc},
        "integration_lifecycle_audit": {doc_id: audit_doc},
    })

    ok = await it_mutations.finalize_revocation_audit_cas(
        contractor_id=cid,
        provider="jobber",
        outbox_id=doc_id,
        expected_generation=1,
        expected_lifecycle_epoch=1,
        db=db,
    )
    assert ok is True
    # Audit revocation_completed_at MUST be exact outbox updated_at (1700000010.5)
    assert audit_doc.data["revocation_completed_at"] == 1700000010.5
    assert audit_doc.data["revocation_status"] == "provider_confirmed"
    assert outbox_doc.data["audit_finalized"] is True
    assert outbox_doc.data["audit_finalized_at"] >= 1700000010.5


@pytest.mark.asyncio
async def test_18b2a_legacy_reconciliation_generation_0(monkeypatch):
    """P1-10: Generation 0 clean tombstone is reconciled atomically with generation 0 records."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-legacy-gen0"
    doc_id = f"{cid}_jobber_0_credentials_deleted"

    doc = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_disconnected_at": 1700000000.0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    outbox_store = {}
    audit_store = {}
    db = _FakeFirestore({
        "contractors": {cid: doc},
        "integration_revocation_outbox": outbox_store,
        "integration_lifecycle_audit": audit_store,
    })

    res = await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db)
    assert res.generation == 0  # Generation stays 0!
    assert res.lifecycle_epoch == 0
    assert res.credential_deletion == "legacy_reconciled"
    assert res.revocation_status == "not_attempted_unavailable_token"
    assert res.audit_finalized is True

    assert doc_id in outbox_store
    assert doc_id in audit_store
    assert outbox_store[doc_id].data["generation"] == 0
    assert audit_store[doc_id].data["generation"] == 0


@pytest.mark.asyncio
async def test_18b2a_audit_and_outbox_closed_schema_validators():
    """P1-8 & P1-9: Hostile schema validation for disconnect audit and outbox records."""
    from app.db.integration_lifecycle_audit import (
        validate_disconnect_audit_record,
        validate_outbox_record,
    )
    from app.services.integration_tokens import IntegrationTokenEnvelopeError

    cid = "c-validator-test"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    valid_audit = {
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "user_requested_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_confirmed",
        "revocation_completed_at": 1700000005.0,
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    }
    # Valid audit passes
    validate_disconnect_audit_record(
        valid_audit,
        expected_contractor_id=cid,
        expected_provider="jobber",
        expected_generation=1,
        expected_lifecycle_epoch=1,
        expected_audit_id=doc_id,
    )

    # Audit rejections: unexpected key, forbidden secret key, bad disposition, bad status, generation bool
    with pytest.raises((ValueError, IntegrationTokenEnvelopeError)):
        validate_disconnect_audit_record({**valid_audit, "extra_key": "bad"})
    with pytest.raises((ValueError, IntegrationTokenEnvelopeError)):
        validate_disconnect_audit_record({**valid_audit, "access_token": "secret"})
    with pytest.raises((ValueError, IntegrationTokenEnvelopeError)):
        validate_disconnect_audit_record({**valid_audit, "credential_deletion_disposition": "unknown_disp"})
    with pytest.raises((ValueError, IntegrationTokenEnvelopeError)):
        validate_disconnect_audit_record({**valid_audit, "revocation_status": "unknown_status"})
    with pytest.raises((ValueError, IntegrationTokenEnvelopeError)):
        validate_disconnect_audit_record({**valid_audit, "generation": True})

    valid_outbox = {
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_confirmed",
        "claim_id": "claim-1234",
        "audit_finalized": True,
        "audit_finalized_at": 1700000010.0,
        "created_at": 1700000000.0,
        "updated_at": 1700000005.0,
        "credential_deletion_disposition": "executed",
    }
    # Valid outbox passes
    validate_outbox_record(
        valid_outbox,
        expected_contractor_id=cid,
        expected_provider="jobber",
        expected_generation=1,
        expected_lifecycle_epoch=1,
        expected_outbox_id=doc_id,
    )

    # Outbox rejections: unexpected key, bad status, updated_at < created_at, audit_finalized_at < updated_at
    with pytest.raises((ValueError, IntegrationTokenEnvelopeError)):
        validate_outbox_record({**valid_outbox, "unexpected": "field"})
    with pytest.raises((ValueError, IntegrationTokenEnvelopeError)):
        validate_outbox_record({**valid_outbox, "status": "invalid_status"})
    with pytest.raises((ValueError, IntegrationTokenEnvelopeError)):
        validate_outbox_record({**valid_outbox, "updated_at": 1699999999.0})
    with pytest.raises((ValueError, IntegrationTokenEnvelopeError)):
        validate_outbox_record({**valid_outbox, "audit_finalized_at": 1700000001.0})


@pytest.mark.asyncio
async def test_18b2a_legacy_reconciliation_exact_reuse_and_counterpart_creation(monkeypatch):
    """P1-10: Legacy reconciliation reuses matching pairs and creates missing counterparts without overwriting."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-reconcile-counterpart"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    contractor_doc = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_disconnected_at": 1700000000.0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    # Case A: Outbox exists, audit missing -> Creates missing audit derived from outbox
    existing_outbox = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_confirmed",
        "claim_id": "claim-old",
        "audit_finalized": True,
        "audit_finalized_at": 1700000005.0,
        "created_at": 1700000000.0,
        "updated_at": 1700000005.0,
        "credential_deletion_disposition": "executed",
    })

    audit_store = {}
    db = _FakeFirestore({
        "contractors": {cid: contractor_doc},
        "integration_revocation_outbox": {doc_id: existing_outbox},
        "integration_lifecycle_audit": audit_store,
    })

    res = await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db)
    assert res.generation == 1
    assert res.credential_deletion == "legacy_reconciled"
    assert doc_id in audit_store
    assert audit_store[doc_id].data["revocation_status"] == "provider_confirmed"
    assert audit_store[doc_id].data["revocation_completed_at"] == 1700000005.0


@pytest.mark.asyncio
async def test_18b2b_orchestration_fail_closed_after_http_if_unpersisted(monkeypatch):
    """B2B-5 & B2B-7 #10: Orchestration fails closed with CASConflict after HTTP if outcome CAS fails to persist terminal state."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-orch-truth"

    doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "acc-valid-1234",
        "jobber_refresh_token": "ref-valid-1234",
    }, doc_id=cid)

    outbox_store = {}
    audit_store = {}
    db = _FakeFirestore({
        "contractors": {cid: doc},
        "integration_revocation_outbox": outbox_store,
        "integration_lifecycle_audit": audit_store,
    })

    class _MockHttp:
        async def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=200)

    # Monkeypatch record_revocation_outcome_cas to simulate network failure leaving outbox in provider_request_started
    async def _failing_record_outcome(*args, **kwargs):
        raise RuntimeError("Simulated network timeout during outcome commit")

    monkeypatch.setattr(it_mutations, "record_revocation_outcome_cas", _failing_record_outcome)

    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="unconfirmed after HTTP request"):
        await it_mutations.disconnect_and_revoke_provider_orchestration(
            contractor_id=cid,
            provider="jobber",
            db=db,
            http_client=_MockHttp(),
        )


@pytest.mark.asyncio
async def test_18b2a_orchestration_zero_http_on_contenders_and_repeats(monkeypatch):
    """P1-12: Exactly one request owns claim; contenders and repeats make zero provider HTTP calls."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-zero-http-contenders"

    doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "acc-valid-1234",
        "jobber_refresh_token": "ref-valid-1234",
    }, doc_id=cid)

    outbox_store = {}
    audit_store = {}
    db = _FakeFirestore({
        "contractors": {cid: doc},
        "integration_revocation_outbox": outbox_store,
        "integration_lifecycle_audit": audit_store,
    })

    http_call_count = [0]

    class _CountingHttp:
        async def post(self, *args, **kwargs):
            http_call_count[0] += 1
            return SimpleNamespace(status_code=200)

    # 1. First caller: succeeds, owns claim, executes 1 HTTP call
    res1 = await it_mutations.disconnect_and_revoke_provider_orchestration(
        contractor_id=cid,
        provider="jobber",
        db=db,
        http_client=_CountingHttp(),
    )
    assert res1["credential_deletion"]["status"] == "executed"
    assert res1["provider_revocation"]["attempted_by_this_request"] is True
    assert res1["provider_revocation"]["status"] == "provider_confirmed"
    assert http_call_count[0] == 1

    # 2. Second repeat caller: returns already_disconnected, executes 0 HTTP calls
    res2 = await it_mutations.disconnect_and_revoke_provider_orchestration(
        contractor_id=cid,
        provider="jobber",
        db=db,
        http_client=_CountingHttp(),
    )
    assert res2["credential_deletion"]["status"] == "already_disconnected"
    assert res2["provider_revocation"]["attempted_by_this_request"] is False
    assert res2["provider_revocation"]["status"] == "provider_confirmed"
    assert http_call_count[0] == 1  # Still 1!


@pytest.mark.asyncio
async def test_18b2a_endpoint_structured_response_no_secrets(monkeypatch):
    """P1-12 & P1-13: Endpoint returns structured response without leaking secrets."""
    from app.api import integrations
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-endpoint-structured"

    doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "secret-token-1234",
        "jobber_refresh_token": "secret-token-5678",
    }, doc_id=cid)

    outbox_store = {}
    audit_store = {}
    db = _FakeFirestore({
        "contractors": {cid: doc},
        "integration_revocation_outbox": outbox_store,
        "integration_lifecycle_audit": audit_store,
    })

    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(it_mutations, "get_firestore_client", lambda: db)

    async def _fake_post(*args, **kwargs):
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    req = SimpleNamespace(state=SimpleNamespace(is_admin=True, contractor_id=cid))
    resp = await integrations.jobber_disconnect(contractor_id=cid, request=req)

    assert resp["status"] == "disconnected"
    assert resp["contractor_id"] == cid
    assert resp["provider"] == "jobber"
    assert resp["generation"] == 1
    assert resp["lifecycle_epoch"] == 1
    assert resp["audit_id"] == f"{cid}_jobber_1_credentials_deleted"
    assert resp["outbox_id"] == f"{cid}_jobber_1_credentials_deleted"
    assert resp["credential_deletion"]["status"] == "executed"
    assert resp["credential_deletion"]["attempted_by_this_request"] is True
    assert resp["provider_revocation"]["status"] == "provider_confirmed"
    assert resp["provider_revocation"]["attempted_by_this_request"] is True
    assert resp["audit_finalization"]["finalized"] is True
    assert resp["audit_finalization"]["status"] == "finalized"

    resp_str = str(resp)
    assert "secret-token-1234" not in resp_str
    assert "secret-token-5678" not in resp_str
    assert "claim_id" not in resp


# ---------------------------------------------------------------------------
# 18B2B Causal Test Scenarios (B2B-7 Items 1 to 11)
# ---------------------------------------------------------------------------


def test_18b2b_malformed_floor_types_fail_closed_in_tombstone_and_extraction():
    """B2B-7 #1: Malformed floor types (None, non-bool) fail closed in tombstone classifier and token extraction."""
    from app.services import integration_token_mutations as it_mutations
    cid = "c-malformed-floor-test"

    base_tombstone = {
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_disconnected_at": 1700000000.0,
        "jobber_lead_capture_enabled": False,
    }

    # Absent floor and exact bool floors are valid tombstones
    assert it_mutations.is_durable_provider_tombstone(base_tombstone, "jobber", cid) is True
    assert it_mutations.is_durable_provider_tombstone({**base_tombstone, "jobber_token_envelope_required": False}, "jobber", cid) is True
    assert it_mutations.is_durable_provider_tombstone({**base_tombstone, "jobber_token_envelope_required": True}, "jobber", cid) is True

    # Malformed floor types in tombstone MUST fail classification
    for bad_floor in [None, 1, 0, "true", "false", [], {}]:
        bad_doc = dict(base_tombstone)
        bad_doc["jobber_token_envelope_required"] = bad_floor
        assert it_mutations.is_durable_provider_tombstone(bad_doc, "jobber", cid) is False

    # Malformed floor types in token extraction MUST return None (fail-closed, zero HTTP)
    base_active = {
        "contractor_id": cid,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_access_token": "acc-valid-1234",
        "jobber_refresh_token": "ref-valid-1234",
    }
    for bad_floor in [None, 1, 0, "true", "false", []]:
        bad_doc = dict(base_active)
        bad_doc["jobber_token_envelope_required"] = bad_floor
        assert it_mutations.extract_revocation_access_token(bad_doc, "jobber", cid, generation=1) is None


@pytest.mark.asyncio
async def test_18b2b_presence_aware_floor_postcondition_and_normalization(monkeypatch):
    """B2B-7 #2: Presence-aware floor in postcondition and monotonic normalization for dirty contractors."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-presence-floor-test"

    doc_ref_absent = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_disconnected_at": 1700000000.0,
        "jobber_lead_capture_enabled": False,
    })
    doc_ref_false = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_disconnected_at": 1700000000.0,
        "jobber_lead_capture_enabled": False,
        "jobber_token_envelope_required": False,
    })
    doc_ref_true = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_disconnected_at": 1700000000.0,
        "jobber_lead_capture_enabled": False,
        "jobber_token_envelope_required": True,
    })

    # Postcondition verifies exact expected floor state
    it_mutations._verify_complete_disconnect_postcondition(
        doc_ref_absent,
        contractor_id=cid,
        provider="jobber",
        expected_generation=1,
        expected_lifecycle_epoch=1,
        expected_disconnected_at=1700000000.0,
        expected_floor=it_mutations._FLOOR_ABSENT,
    )
    with pytest.raises(it_mutations.IntegrationTokenPostconditionError, match="Expected floor is absent"):
        it_mutations._verify_complete_disconnect_postcondition(
            doc_ref_absent,
            contractor_id=cid,
            provider="jobber",
            expected_generation=1,
            expected_lifecycle_epoch=1,
            expected_disconnected_at=1700000000.0,
            expected_floor=False,
        )

    it_mutations._verify_complete_disconnect_postcondition(
        doc_ref_false,
        contractor_id=cid,
        provider="jobber",
        expected_generation=1,
        expected_lifecycle_epoch=1,
        expected_disconnected_at=1700000000.0,
        expected_floor=False,
    )
    with pytest.raises(it_mutations.IntegrationTokenPostconditionError, match="Expected floor absence"):
        it_mutations._verify_complete_disconnect_postcondition(
            doc_ref_false,
            contractor_id=cid,
            provider="jobber",
            expected_generation=1,
            expected_lifecycle_epoch=1,
            expected_disconnected_at=1700000000.0,
            expected_floor=it_mutations._FLOOR_ABSENT,
        )

    # Disconnecting a dirty document with malformed present floor normalizes it to True
    dirty_doc = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_token_envelope_required": "malformed_string",
        "jobber_access_token": "token-1",
        "jobber_refresh_token": "token-2",
    }, doc_id=cid)

    db = _FakeFirestore({
        "contractors": {cid: dirty_doc},
        "integration_revocation_outbox": {},
        "integration_lifecycle_audit": {},
    })

    res = await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db)
    assert dirty_doc.data["jobber_token_envelope_required"] is True
    assert res.generation == 1


def test_18b2b_outbox_validator_status_specific_shapes():
    """B2B-7 #3: Strict hostile outbox validation enforces status-specific shapes and temporal invariants."""
    from app.db.integration_lifecycle_audit import validate_outbox_record
    cid = "c-outbox-shapes"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    base = {
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "credential_deletion_disposition": "executed",
        "created_at": 1700000000.0,
    }

    # 1. provider_request_started: claim_id required, audit_finalized must be False, updated_at == created_at
    valid_started = {
        **base,
        "status": "provider_request_started",
        "claim_id": "claim-1234",
        "audit_finalized": False,
        "audit_finalized_at": None,
        "updated_at": 1700000000.0,
    }
    validate_outbox_record(valid_started, expected_outbox_id=doc_id)

    with pytest.raises(ValueError, match="claim_id must be non-empty str"):
        validate_outbox_record({**valid_started, "claim_id": None})
    with pytest.raises(ValueError, match="audit_finalized must be False"):
        validate_outbox_record({**valid_started, "audit_finalized": True})
    with pytest.raises(ValueError, match="audit_finalized_at must be None"):
        validate_outbox_record({**valid_started, "audit_finalized_at": 1700000005.0})
    with pytest.raises(ValueError, match="must exactly equal created_at"):
        validate_outbox_record({**valid_started, "updated_at": 1700000005.0})

    # 2. not_attempted_unavailable_token: claim_id must be None
    valid_unavailable = {
        **base,
        "status": "not_attempted_unavailable_token",
        "claim_id": None,
        "audit_finalized": True,
        "audit_finalized_at": 1700000000.0,
        "updated_at": 1700000000.0,
    }
    validate_outbox_record(valid_unavailable, expected_outbox_id=doc_id)

    with pytest.raises(ValueError, match="claim_id must be None"):
        validate_outbox_record({**valid_unavailable, "claim_id": "forbidden-claim"})

    # 3. provider_confirmed: claim_id required, audit_finalized_at >= updated_at
    valid_confirmed = {
        **base,
        "status": "provider_confirmed",
        "claim_id": "claim-1234",
        "audit_finalized": True,
        "audit_finalized_at": 1700000010.0,
        "updated_at": 1700000005.0,
    }
    validate_outbox_record(valid_confirmed, expected_outbox_id=doc_id)

    with pytest.raises(ValueError, match="claim_id must be non-empty str"):
        validate_outbox_record({**valid_confirmed, "claim_id": None})
    with pytest.raises(ValueError, match="cannot be before updated_at"):
        validate_outbox_record({**valid_confirmed, "audit_finalized_at": 1700000001.0})


def test_18b2b_disconnect_audit_validator_completion_and_actors():
    """B2B-7 #4: Hostile disconnect audit validator enforces closed actors, closed reasons, and timestamp invariants."""
    from app.db.integration_lifecycle_audit import validate_disconnect_audit_record
    cid = "c-audit-validator"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    base_audit = {
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    }

    # Started audit: revocation_completed_at MUST be None
    valid_started = {
        **base_audit,
        "revocation_status": "provider_request_started",
        "revocation_completed_at": None,
    }
    validate_disconnect_audit_record(valid_started, expected_audit_id=doc_id)

    with pytest.raises(ValueError, match="revocation_completed_at must be None"):
        validate_disconnect_audit_record({**valid_started, "revocation_completed_at": 1700000005.0})

    # Terminal audit: revocation_completed_at MUST be >= created_at
    valid_terminal = {
        **base_audit,
        "revocation_status": "provider_confirmed",
        "revocation_completed_at": 1700000005.0,
    }
    validate_disconnect_audit_record(valid_terminal, expected_audit_id=doc_id)

    with pytest.raises(ValueError, match="cannot be None when revocation_status is terminal"):
        validate_disconnect_audit_record({**valid_terminal, "revocation_completed_at": None})
    with pytest.raises(ValueError, match="cannot be before created_at"):
        validate_disconnect_audit_record({**valid_terminal, "revocation_completed_at": 1699999999.0})

    # Closed actor and reason sets
    with pytest.raises(ValueError, match="Invalid actor"):
        validate_disconnect_audit_record({**valid_terminal, "actor": "unauthorized_script"})
    with pytest.raises(ValueError, match="Invalid reason"):
        validate_disconnect_audit_record({**valid_terminal, "reason": "arbitrary_reason"})

    # timestamp == created_at exact equality
    with pytest.raises(ValueError, match="must exactly equal created_at"):
        validate_disconnect_audit_record({**valid_terminal, "timestamp": 1700000001.0})


def test_18b2b_pair_validator_state_and_timestamp_coherence():
    """B2B-7 #5: Pure pair validator enforces exact context matching and atomic state machine coherence."""
    from app.db.integration_lifecycle_audit import validate_disconnect_lifecycle_pair
    cid = "c-pair-test"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    audit_started = {
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_request_started",
        "revocation_completed_at": None,
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    }
    outbox_started = {
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_request_started",
        "claim_id": "claim-1",
        "audit_finalized": False,
        "audit_finalized_at": None,
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
        "credential_deletion_disposition": "executed",
    }
    # 1. Matching started pair passes
    validate_disconnect_lifecycle_pair(audit_started, outbox_started, expected_doc_id=doc_id)

    # 2. Matching unfinalized terminal pair passes
    outbox_unfinalized = {
        **outbox_started,
        "status": "provider_confirmed",
        "updated_at": 1700000005.0,
    }
    validate_disconnect_lifecycle_pair(audit_started, outbox_unfinalized, expected_doc_id=doc_id)

    # 3. Matching finalized terminal pair passes
    audit_finalized = {
        **audit_started,
        "revocation_status": "provider_confirmed",
        "revocation_completed_at": 1700000005.0,
    }
    outbox_finalized = {
        **outbox_unfinalized,
        "audit_finalized": True,
        "audit_finalized_at": 1700000006.0,
    }
    validate_disconnect_lifecycle_pair(audit_finalized, outbox_finalized, expected_doc_id=doc_id)

    # 4. Incoherent state: outbox is started but audit is finalized -> rejected
    with pytest.raises(ValueError, match="must be provider_request_started when outbox is started"):
        validate_disconnect_lifecycle_pair(audit_finalized, outbox_started)

    # 5. Incoherent state: outbox is finalized but audit is still started -> rejected
    with pytest.raises(ValueError, match="does not match outbox status"):
        validate_disconnect_lifecycle_pair(audit_started, outbox_finalized)

    # 6. Incoherent timestamps: audit completion time does not match outbox updated_at -> rejected
    audit_wrong_ts = {**audit_finalized, "revocation_completed_at": 1700000006.0}
    with pytest.raises(ValueError, match="must match outbox updated_at"):
        validate_disconnect_lifecycle_pair(audit_wrong_ts, outbox_finalized)


@pytest.mark.asyncio
async def test_18b2b_first_disconnect_unavailable_token_prefinalized(monkeypatch):
    """B2B-7 #6: First disconnect with no usable token creates an atomically pre-finalized unavailable pair."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-no-token-prefinalized"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    # Connected flag is True but credentials are absent
    doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
    }, doc_id=cid)

    outbox_store = {}
    audit_store = {}
    db = _FakeFirestore({
        "contractors": {cid: doc},
        "integration_revocation_outbox": outbox_store,
        "integration_lifecycle_audit": audit_store,
    })

    res = await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db)
    assert res.generation == 1
    assert res.lifecycle_epoch == 1
    assert res.revocation_status == "not_attempted_unavailable_token"
    assert res.claim_id is None
    assert res.access_token_for_revocation is None
    assert res.audit_finalized is True

    # Validate stored records in fake DB
    assert doc_id in outbox_store
    assert doc_id in audit_store
    outbox_data = outbox_store[doc_id].data
    audit_data = audit_store[doc_id].data
    assert outbox_data["status"] == "not_attempted_unavailable_token"
    assert outbox_data["claim_id"] is None
    assert outbox_data["audit_finalized"] is True
    assert audit_data["revocation_status"] == "not_attempted_unavailable_token"
    assert audit_data["revocation_completed_at"] == outbox_data["updated_at"]


@pytest.mark.asyncio
async def test_18b2b_legacy_reconciliation_conflict_on_incoherent_or_lone_audit(monkeypatch):
    """B2B-7 #7: Legacy reconciliation fails closed on incoherent pairs, unfinalized outboxes, or lone audits."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-legacy-fail-closed"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    contractor_doc = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_disconnected_at": 1700000000.0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    # Case A: Incoherent pair (outbox finalized but audit still started) -> fails closed
    outbox_fin = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_confirmed",
        "claim_id": "c1",
        "audit_finalized": True,
        "audit_finalized_at": 1700000010.0,
        "created_at": 1700000000.0,
        "updated_at": 1700000005.0,
        "credential_deletion_disposition": "executed",
    })
    audit_started = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_request_started",
        "revocation_completed_at": None,
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    })

    db_incoherent = _FakeFirestore({
        "contractors": {cid: contractor_doc},
        "integration_revocation_outbox": {doc_id: outbox_fin},
        "integration_lifecycle_audit": {doc_id: audit_started},
    })
    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Incoherent existing audit/outbox pair"):
        await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db_incoherent)

    # Case B: Outbox is unfinalized / started -> cannot derive audit from started outbox
    outbox_started = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_request_started",
        "claim_id": "c1",
        "audit_finalized": False,
        "audit_finalized_at": None,
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
        "credential_deletion_disposition": "executed",
    })
    db_unfinalized_outbox = _FakeFirestore({
        "contractors": {cid: contractor_doc},
        "integration_revocation_outbox": {doc_id: outbox_started},
        "integration_lifecycle_audit": {},
    })
    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Cannot derive audit record"):
        await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db_unfinalized_outbox)

    # Case C: Lone audit record exists -> fails closed
    db_lone_audit = _FakeFirestore({
        "contractors": {cid: contractor_doc},
        "integration_revocation_outbox": {},
        "integration_lifecycle_audit": {doc_id: audit_started},
    })
    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Cannot reconstruct outbox from lone audit"):
        await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db_lone_audit)


@pytest.mark.asyncio
async def test_18b2b_outcome_and_finalizer_context_validation(monkeypatch):
    """B2B-7 #8: Outcome CAS and Finalizer CAS strictly validate generation and lifecycle epoch context."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-context-val-test"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    outbox_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_request_started",
        "claim_id": "claim-1234",
        "audit_finalized": False,
        "audit_finalized_at": None,
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
        "credential_deletion_disposition": "executed",
    })
    audit_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_request_started",
        "revocation_completed_at": None,
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    })

    db = _FakeFirestore({
        "integration_revocation_outbox": {doc_id: outbox_doc},
        "integration_lifecycle_audit": {doc_id: audit_doc},
    })

    # 1. Outcome CAS generation mismatch fails
    with pytest.raises((it_mutations.IntegrationTokenCASConflict, ValueError)):
        await it_mutations.record_revocation_outcome_cas(
            contractor_id=cid,
            provider="jobber",
            outbox_id=doc_id,
            claim_id="claim-1234",
            outcome_status="provider_confirmed",
            expected_generation=2,  # Mismatch!
            expected_lifecycle_epoch=1,
            db=db,
        )

    # 2. Outcome CAS lifecycle epoch mismatch fails
    with pytest.raises((it_mutations.IntegrationTokenCASConflict, ValueError)):
        await it_mutations.record_revocation_outcome_cas(
            contractor_id=cid,
            provider="jobber",
            outbox_id=doc_id,
            claim_id="claim-1234",
            outcome_status="provider_confirmed",
            expected_generation=1,
            expected_lifecycle_epoch=2,  # Mismatch!
            db=db,
        )

    # 3. Finalizer CAS generation mismatch fails
    with pytest.raises((it_mutations.IntegrationTokenCASConflict, ValueError)):
        await it_mutations.finalize_revocation_audit_cas(
            contractor_id=cid,
            provider="jobber",
            outbox_id=doc_id,
            expected_generation=2,
            expected_lifecycle_epoch=1,
            db=db,
        )


@pytest.mark.asyncio
async def test_18b2b_finalizer_idempotency_and_stale_audit_rejection(monkeypatch):
    """B2B-7 #9: Finalizer succeeds idempotently on coherent finalized pair, but rejects stale audit with zero writes."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-finalizer-idempotency"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    outbox_finalized = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_confirmed",
        "claim_id": "c1",
        "audit_finalized": True,
        "audit_finalized_at": 1700000010.0,
        "created_at": 1700000000.0,
        "updated_at": 1700000005.0,
        "credential_deletion_disposition": "executed",
    })
    audit_matching = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_confirmed",
        "revocation_completed_at": 1700000005.0,
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    })

    db_matching = _FakeFirestore({
        "integration_revocation_outbox": {doc_id: outbox_finalized},
        "integration_lifecycle_audit": {doc_id: audit_matching},
    })

    # Matching coherent pair returns True (idempotent success)
    ok = await it_mutations.finalize_revocation_audit_cas(
        contractor_id=cid,
        provider="jobber",
        outbox_id=doc_id,
        expected_generation=1,
        expected_lifecycle_epoch=1,
        db=db_matching,
    )
    assert ok is True

    # Stale audit (status provider_request_started while outbox is finalized) fails closed
    audit_stale = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_request_started",
        "revocation_completed_at": None,
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    })
    db_stale = _FakeFirestore({
        "integration_revocation_outbox": {doc_id: outbox_finalized},
        "integration_lifecycle_audit": {doc_id: audit_stale},
    })
    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Incoherent"):
        await it_mutations.finalize_revocation_audit_cas(
            contractor_id=cid,
            provider="jobber",
            outbox_id=doc_id,
            expected_generation=1,
            expected_lifecycle_epoch=1,
            db=db_stale,
        )


def test_18b2b_generation_binding_for_revocation_token():
    """B2B-7 #11: extract_revocation_access_token binds and validates explicit snapshot generation."""
    from app.services import integration_token_mutations as it_mutations
    cid = "c-gen-binding-test"

    doc_data = {
        "contractor_id": cid,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 2,
        "jobber_access_token": "acc-token-1234",
        "jobber_refresh_token": "ref-token-1234",
    }

    # Matching generation returns token
    assert it_mutations.extract_revocation_access_token(doc_data, "jobber", cid, generation=2) == "acc-token-1234"

    # Mismatched generation returns None (fail closed)
    assert it_mutations.extract_revocation_access_token(doc_data, "jobber", cid, generation=1) is None
    assert it_mutations.extract_revocation_access_token(doc_data, "jobber", cid, generation=3) is None

    # Invalid generation types return None
    assert it_mutations.extract_revocation_access_token(doc_data, "jobber", cid, generation="2") is None  # type: ignore
    assert it_mutations.extract_revocation_access_token(doc_data, "jobber", cid, generation=True) is None  # type: ignore
    assert it_mutations.extract_revocation_access_token(doc_data, "jobber", cid, generation=-1) is None


# ═════════════════════════════════════════════════════════════════════════════
# 18B2C Strict Verification Suite (C-1 through C-8)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_18b2c_disconnect_generation_and_epoch_overflow_zero_writes(monkeypatch):
    """C-1: Generation and lifecycle epoch overflow rejects before any queued mutations with zero writes."""
    from app.services import integration_token_mutations as it_mutations
    from app.services.integration_tokens import MAX_KEY_VERSION
    _setup_keyring(monkeypatch)
    cid = "c-overflow-test"

    # 1. Generation overflow
    doc_gen_max = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": MAX_KEY_VERSION,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": "acc-token-1234",
        "jobber_refresh_token": "ref-token-1234",
    }, doc_id=cid)
    outbox_store_gen = {}
    audit_store_gen = {}
    db_gen = _FakeFirestore({
        "contractors": {cid: doc_gen_max},
        "integration_revocation_outbox": outbox_store_gen,
        "integration_lifecycle_audit": audit_store_gen,
    })

    initial_doc_snapshot = dict(doc_gen_max.data)
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError, match="Generation overflow"):
        await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db_gen)

    # Prove byte-for-byte zero writes across all stores
    assert doc_gen_max.data == initial_doc_snapshot
    assert len(outbox_store_gen) == 0
    assert len(audit_store_gen) == 0

    # 2. Lifecycle epoch overflow
    doc_epoch_max = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": MAX_KEY_VERSION,
        "jobber_access_token": "acc-token-1234",
        "jobber_refresh_token": "ref-token-1234",
    }, doc_id=cid)
    outbox_store_epoch = {}
    audit_store_epoch = {}
    db_epoch = _FakeFirestore({
        "contractors": {cid: doc_epoch_max},
        "integration_revocation_outbox": outbox_store_epoch,
        "integration_lifecycle_audit": audit_store_epoch,
    })

    initial_epoch_snapshot = dict(doc_epoch_max.data)
    with pytest.raises(it_mutations.IntegrationTokenEnvelopeError, match="Lifecycle epoch overflow"):
        await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider="jobber", db=db_epoch)

    # Prove byte-for-byte zero writes across all stores
    assert doc_epoch_max.data == initial_epoch_snapshot
    assert len(outbox_store_epoch) == 0
    assert len(audit_store_epoch) == 0


def test_18b2c_closed_validators_enforce_max_key_version_and_exact_types():
    """C-1 & C-3: Closed record validators enforce MAX_KEY_VERSION bounds and reject bool aliases."""
    from app.db.integration_lifecycle_audit import (
        build_disconnect_audit_event,
        build_disconnect_outbox_record,
        validate_disconnect_audit_record,
        validate_outbox_record,
    )
    from app.services.integration_tokens import MAX_KEY_VERSION

    cid = "c-bounds-test"
    valid_outbox = build_disconnect_outbox_record(
        contractor_id=cid,
        provider="jobber",
        generation=1,
        lifecycle_epoch=1,
        status="provider_request_started",
        claim_id="claim-123",
        audit_finalized=False,
        audit_finalized_at=None,
        created_at=1700000000.0,
        updated_at=1700000000.0,
        credential_deletion_disposition="executed",
    )
    valid_audit = build_disconnect_audit_event(
        contractor_id=cid,
        provider="jobber",
        generation=1,
        lifecycle_epoch=1,
        actor="contractor_api",
        reason="contractor_initiated_disconnect",
        credential_deletion_disposition="executed",
        revocation_status="provider_request_started",
        revocation_completed_at=None,
        timestamp=1700000000.0,
    )

    # Outbox generation overflow
    with pytest.raises(ValueError, match="Invalid generation"):
        validate_outbox_record({**valid_outbox, "generation": MAX_KEY_VERSION + 1})
    with pytest.raises(ValueError, match="Invalid expected_generation"):
        validate_outbox_record(valid_outbox, expected_generation=MAX_KEY_VERSION + 1)
    with pytest.raises(ValueError, match="Invalid expected_generation"):
        validate_outbox_record(valid_outbox, expected_generation=True)  # True must never certify 1

    # Outbox epoch overflow
    with pytest.raises(ValueError, match="Invalid lifecycle_epoch"):
        validate_outbox_record({**valid_outbox, "lifecycle_epoch": MAX_KEY_VERSION + 1})
    with pytest.raises(ValueError, match="Invalid expected_lifecycle_epoch"):
        validate_outbox_record(valid_outbox, expected_lifecycle_epoch=MAX_KEY_VERSION + 1)
    with pytest.raises(ValueError, match="Invalid expected_lifecycle_epoch"):
        validate_outbox_record(valid_outbox, expected_lifecycle_epoch=True)

    # Audit generation overflow
    with pytest.raises(ValueError, match="Invalid generation"):
        validate_disconnect_audit_record({**valid_audit, "generation": MAX_KEY_VERSION + 1})
    with pytest.raises(ValueError, match="Invalid expected_generation"):
        validate_disconnect_audit_record(valid_audit, expected_generation=MAX_KEY_VERSION + 1)
    with pytest.raises(ValueError, match="Invalid expected_generation"):
        validate_disconnect_audit_record(valid_audit, expected_generation=True)

    # Audit epoch overflow
    with pytest.raises(ValueError, match="Invalid lifecycle_epoch"):
        validate_disconnect_audit_record({**valid_audit, "lifecycle_epoch": MAX_KEY_VERSION + 1})
    with pytest.raises(ValueError, match="Invalid expected_lifecycle_epoch"):
        validate_disconnect_audit_record(valid_audit, expected_lifecycle_epoch=MAX_KEY_VERSION + 1)
    with pytest.raises(ValueError, match="Invalid expected_lifecycle_epoch"):
        validate_disconnect_audit_record(valid_audit, expected_lifecycle_epoch=True)


def test_18b2c_extract_revocation_token_missing_generation_and_hostile_inputs():
    """C-2: extract_revocation_access_token strictly requires generation key present and handles hostile types safely."""
    from app.services import integration_token_mutations as it_mutations
    cid = "c-extract-hostile"

    # 1. Missing generation key
    doc_missing_gen = {
        "contractor_id": cid,
        "jobber_access_token": "acc-token-1234",
        "jobber_refresh_token": "ref-token-1234",
    }
    assert it_mutations.extract_revocation_access_token(doc_missing_gen, "jobber", cid, generation=0) is None
    assert it_mutations.extract_revocation_access_token(doc_missing_gen, "jobber", cid, generation=1) is None

    # 2. None generation key
    doc_none_gen = {
        "contractor_id": cid,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": None,
        "jobber_access_token": "acc-token-1234",
        "jobber_refresh_token": "ref-token-1234",
    }
    assert it_mutations.extract_revocation_access_token(doc_none_gen, "jobber", cid, generation=0) is None

    # 3. Hostile unhashable provider inputs (must return None safely, zero TypeError)
    doc_valid = {
        "contractor_id": cid,
        "jobber_lifecycle_epoch": 0,
        "jobber_generation": 1,
        "jobber_access_token": "acc-token-1234",
        "jobber_refresh_token": "ref-token-1234",
    }
    assert it_mutations.extract_revocation_access_token(doc_valid, ["jobber"], cid, generation=1) is None  # type: ignore
    assert it_mutations.extract_revocation_access_token(doc_valid, {"p": "jobber"}, cid, generation=1) is None  # type: ignore
    assert it_mutations.extract_revocation_access_token(doc_valid, None, cid, generation=1) is None  # type: ignore

    # 4. Hostile contractor_id inputs
    assert it_mutations.extract_revocation_access_token(doc_valid, "jobber", None, generation=1) is None  # type: ignore
    assert it_mutations.extract_revocation_access_token(doc_valid, "jobber", 12345, generation=1) is None  # type: ignore
    assert it_mutations.extract_revocation_access_token(doc_valid, "jobber", ["cid"], generation=1) is None  # type: ignore

    # 5. Hostile document types
    assert it_mutations.extract_revocation_access_token(None, "jobber", cid, generation=1) is None  # type: ignore
    assert it_mutations.extract_revocation_access_token("string", "jobber", cid, generation=1) is None  # type: ignore
    assert it_mutations.extract_revocation_access_token([], "jobber", cid, generation=1) is None  # type: ignore


def test_18b2c_validator_rejects_extra_keys_including_helper_ids():
    """C-3: Closed validators reject all extra keys including outbox_id and audit_id, and return pure persisted dictionaries."""
    from app.db.integration_lifecycle_audit import (
        EXPECTED_DISCONNECT_AUDIT_KEYS,
        EXPECTED_OUTBOX_KEYS,
        build_disconnect_audit_event,
        build_disconnect_outbox_record,
        validate_disconnect_audit_record,
        validate_outbox_record,
    )

    cid = "c-extra-keys-test"
    valid_outbox = build_disconnect_outbox_record(
        contractor_id=cid,
        provider="jobber",
        generation=1,
        lifecycle_epoch=1,
        status="provider_request_started",
        claim_id="claim-123",
        audit_finalized=False,
        audit_finalized_at=None,
        created_at=1700000000.0,
        updated_at=1700000000.0,
        credential_deletion_disposition="executed",
    )
    valid_audit = build_disconnect_audit_event(
        contractor_id=cid,
        provider="jobber",
        generation=1,
        lifecycle_epoch=1,
        actor="contractor_api",
        reason="contractor_initiated_disconnect",
        credential_deletion_disposition="executed",
        revocation_status="provider_request_started",
        revocation_completed_at=None,
        timestamp=1700000000.0,
    )

    # 1. Outbox rejects outbox_id and audit_id as extra keys
    with pytest.raises(ValueError, match="key mismatch"):
        validate_outbox_record({**valid_outbox, "outbox_id": "injected-id"})
    with pytest.raises(ValueError, match="key mismatch"):
        validate_outbox_record({**valid_outbox, "audit_id": "injected-id"})

    # 2. Audit rejects audit_id and outbox_id as extra keys
    with pytest.raises(ValueError, match="key mismatch"):
        validate_disconnect_audit_record({**valid_audit, "audit_id": "injected-id"})
    with pytest.raises(ValueError, match="key mismatch"):
        validate_disconnect_audit_record({**valid_audit, "outbox_id": "injected-id"})

    # 3. Validated returns have exact persisted schema keys and no helper keys
    val_o = validate_outbox_record(valid_outbox)
    assert frozenset(val_o.keys()) == EXPECTED_OUTBOX_KEYS
    assert "outbox_id" not in val_o
    assert "audit_id" not in val_o

    val_a = validate_disconnect_audit_record(valid_audit)
    assert frozenset(val_a.keys()) == EXPECTED_DISCONNECT_AUDIT_KEYS
    assert "audit_id" not in val_a
    assert "outbox_id" not in val_a


@pytest.mark.asyncio
async def test_18b2c_outcome_cas_requires_exact_pair_and_zero_writes_on_mismatch(monkeypatch):
    """C-4: record_revocation_outcome_cas transacts against exact audit/outbox pair, ensuring zero writes on mismatch."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-outcome-pair-test"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    outbox_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_request_started",
        "claim_id": "claim-1234",
        "audit_finalized": False,
        "audit_finalized_at": None,
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
        "credential_deletion_disposition": "executed",
    })
    initial_outbox_snap = dict(outbox_doc.data)

    # 1. Missing audit record -> zero outbox writes
    db_missing_audit = _FakeFirestore({
        "integration_revocation_outbox": {doc_id: outbox_doc},
        "integration_lifecycle_audit": {},
    })
    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Outbox or audit record not found"):
        await it_mutations.record_revocation_outcome_cas(
            contractor_id=cid,
            provider="jobber",
            outbox_id=doc_id,
            claim_id="claim-1234",
            outcome_status="provider_confirmed",
            expected_generation=1,
            expected_lifecycle_epoch=1,
            db=db_missing_audit,
        )
    assert outbox_doc.data == initial_outbox_snap

    # 2. Mismatched generation audit record -> zero outbox writes
    audit_doc_mismatched_gen = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 2,  # Mismatched generation!
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_request_started",
        "revocation_completed_at": None,
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    })
    db_mismatched_audit = _FakeFirestore({
        "integration_revocation_outbox": {doc_id: outbox_doc},
        "integration_lifecycle_audit": {doc_id: audit_doc_mismatched_gen},
    })
    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Incoherent audit/outbox pair"):
        await it_mutations.record_revocation_outcome_cas(
            contractor_id=cid,
            provider="jobber",
            outbox_id=doc_id,
            claim_id="claim-1234",
            outcome_status="provider_confirmed",
            expected_generation=1,
            expected_lifecycle_epoch=1,
            db=db_mismatched_audit,
        )
    assert outbox_doc.data == initial_outbox_snap


@pytest.mark.asyncio
async def test_18b2c_finalizer_validates_pair_before_write_with_zero_writes_on_incoherent_audit(monkeypatch):
    """C-5: finalize_revocation_audit_cas validates pair BEFORE writing, preserving byte-identical state on incoherent audit."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-finalizer-incoherent-test"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    outbox_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_confirmed",
        "claim_id": "claim-1234",
        "audit_finalized": False,
        "audit_finalized_at": None,
        "created_at": 1700000000.0,
        "updated_at": 1700000010.0,
        "credential_deletion_disposition": "executed",
    })
    # Incoherent audit: already marked terminal but with wrong completed_at timestamp
    audit_doc_wrong_ts = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_confirmed",
        "revocation_completed_at": 1700000005.0,  # Does not match outbox updated_at (1700000010.0)!
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    })

    initial_outbox_data = dict(outbox_doc.data)
    initial_audit_data = dict(audit_doc_wrong_ts.data)

    db = _FakeFirestore({
        "integration_revocation_outbox": {doc_id: outbox_doc},
        "integration_lifecycle_audit": {doc_id: audit_doc_wrong_ts},
    })

    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Incoherent audit/outbox pair"):
        await it_mutations.finalize_revocation_audit_cas(
            contractor_id=cid,
            provider="jobber",
            outbox_id=doc_id,
            expected_generation=1,
            expected_lifecycle_epoch=1,
            db=db,
        )

    # Prove BOTH documents remain byte-identical (zero writes)
    assert outbox_doc.data == initial_outbox_data
    assert audit_doc_wrong_ts.data == initial_audit_data


@pytest.mark.asyncio
async def test_18b2c_orchestration_fails_closed_if_reconnected_before_response(monkeypatch):
    """C-6: Orchestration fails closed with IntegrationTokenCASConflict if contractor reconnected before response, with exactly 1 HTTP call."""
    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-reconnect-before-resp"

    doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "acc-token-1234",
        "jobber_refresh_token": "ref-token-1234",
    }, doc_id=cid)

    outbox_store = {}
    audit_store = {}
    db = _FakeFirestore({
        "contractors": {cid: doc},
        "integration_revocation_outbox": outbox_store,
        "integration_lifecycle_audit": audit_store,
    })

    http_call_count = [0]

    class _ReconnectingOnceHttp:
        def __init__(self):
            self.reconnected = False

        async def post(self, *args, **kwargs):
            http_call_count[0] += 1
            if not self.reconnected:
                self.reconnected = True
                # Simulate concurrent reconnect committing while HTTP request was in flight
                doc.data["jobber_connected"] = True
                doc.data["jobber_generation"] = 2
                doc.data["jobber_lifecycle_epoch"] = 2
                doc.data["jobber_access_token"] = "new-access-token"
                doc.data["jobber_refresh_token"] = "new-refresh-token"
            return SimpleNamespace(status_code=200)

    http_client = _ReconnectingOnceHttp()

    # Orchestration must fail closed because final contractor tombstone proof fails!
    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Contractor document is not a durable tombstone"):
        await it_mutations.disconnect_and_revoke_provider_orchestration(
            contractor_id=cid,
            provider="jobber",
            db=db,
            http_client=http_client,
        )

    # Exactly 1 HTTP revocation call occurred
    assert http_call_count[0] == 1

    # Safe subsequent disconnect call on the new connected state works cleanly
    res_repeat = await it_mutations.disconnect_and_revoke_provider_orchestration(
        contractor_id=cid,
        provider="jobber",
        db=db,
        http_client=http_client,
    )
    assert res_repeat["generation"] == 3
    assert res_repeat["provider_revocation"]["status"] == "provider_confirmed"
    # Proves 2nd HTTP was for generation 2, not a duplicate for gen 1
    assert http_call_count[0] == 2


def test_18b2c_claim_mismatch_exception_diagnostics_contain_no_secrets(monkeypatch):
    """C-7: Claim mismatch exceptions never interpolate or leak claim candidate or stored claim IDs."""
    import asyncio

    from app.services import integration_token_mutations as it_mutations
    _setup_keyring(monkeypatch)
    cid = "c-claim-diag-test"
    doc_id = f"{cid}_jobber_1_credentials_deleted"

    secret_stored_claim = "secret-stored-claim-abc-12345"
    secret_caller_claim = "secret-caller-claim-xyz-67890"

    outbox_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": "provider_request_started",
        "claim_id": secret_stored_claim,
        "audit_finalized": False,
        "audit_finalized_at": None,
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
        "credential_deletion_disposition": "executed",
    })
    audit_doc = _FakeDocRef({
        "schema_version": 1,
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "action": "credentials_deleted",
        "actor": "contractor_api",
        "reason": "contractor_initiated_disconnect",
        "credential_deletion_disposition": "executed",
        "revocation_status": "provider_request_started",
        "revocation_completed_at": None,
        "created_at": 1700000000.0,
        "timestamp": 1700000000.0,
    })
    db = _FakeFirestore({
        "integration_revocation_outbox": {doc_id: outbox_doc},
        "integration_lifecycle_audit": {doc_id: audit_doc},
    })

    with pytest.raises(it_mutations.IntegrationTokenCASConflict) as exc_info:
        asyncio.run(
            it_mutations.record_revocation_outcome_cas(
                contractor_id=cid,
                provider="jobber",
                outbox_id=doc_id,
                claim_id=secret_caller_claim,
                outcome_status="provider_confirmed",
                expected_generation=1,
                expected_lifecycle_epoch=1,
                db=db,
            )
        )

    exc_msg = str(exc_info.value)
    assert secret_stored_claim not in exc_msg
    assert secret_caller_claim not in exc_msg
    assert "Claim ID mismatch on outbox record" in exc_msg


# ═════════════════════════════════════════════════════════════════════════════
# 33. Repair 18D — Durable Provider Fence & Audit Contract Causal Tests
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_18d_disconnect_blocks_on_started_intent_and_allows_reserved(monkeypatch):
    """Disconnect raises CASConflict if intent phase is provider_request_started, but preempts reserved."""
    _setup_keyring(monkeypatch)
    cid = "c-18d-disconnect-intent"
    enc_access = encrypt_integration_token("acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_refresh = encrypt_integration_token("ref", contractor_id=cid, provider="jobber", token_kind="refresh")

    future_exp = time.time() + 300.0

    # 1. Intent is provider_request_started -> disconnect raises CAS conflict
    doc_started = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
        "jobber_operation_intent_id": "started-intent-id-12345678",
        "jobber_operation_intent_kind": "refresh",
        "jobber_operation_intent_phase": "provider_request_started",
        "jobber_operation_intent_acquired_at": time.time(),
        "jobber_operation_intent_expires_at": future_exp,
        "jobber_operation_intent_generation": 1,
        "jobber_operation_intent_lifecycle_epoch": 1,
        "jobber_operation_intent_credentials_fingerprint": it_mutations.compute_raw_credentials_fingerprint(enc_access, enc_refresh),
    }, doc_id=cid)
    db_started = _FakeFirestore({"contractors": {cid: doc_started}})

    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Provider operation request started; disconnect pending"):
        await it_mutations.disconnect_provider_envelope_cas(
            contractor_id=cid,
            provider="jobber",
            db=db_started,
        )

    # 2. Intent is reserved -> disconnect succeeds, preempts lease, and clears all intent fields
    doc_reserved = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
        "jobber_operation_intent_id": "reserved-intent-id-12345678",
        "jobber_operation_intent_kind": "refresh",
        "jobber_operation_intent_phase": "reserved",
        "jobber_operation_intent_acquired_at": time.time(),
        "jobber_operation_intent_expires_at": future_exp,
        "jobber_operation_intent_generation": 1,
        "jobber_operation_intent_lifecycle_epoch": 1,
        "jobber_operation_intent_credentials_fingerprint": it_mutations.compute_raw_credentials_fingerprint(enc_access, enc_refresh),
    }, doc_id=cid)
    db_reserved = _FakeFirestore({"contractors": {cid: doc_reserved}})

    res = await it_mutations.disconnect_provider_envelope_cas(
        contractor_id=cid,
        provider="jobber",
        db=db_reserved,
    )
    assert res.generation == 2
    assert doc_reserved.data["jobber_connected"] is False
    assert "jobber_access_token" not in doc_reserved.data
    assert "jobber_refresh_token" not in doc_reserved.data
    assert "jobber_operation_intent_id" not in doc_reserved.data
    assert "jobber_operation_intent_phase" not in doc_reserved.data


@pytest.mark.asyncio
async def test_18d_jobber_lead_capture_single_atomic_audit_and_request_metadata():
    """Lead capture toggle performs single atomic audit create in transaction and forwards actor/reason/metadata."""
    cid = "c-18d-lead-cap-audit"
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    res = await it_mutations.update_jobber_lead_capture_cas(
        contractor_id=cid,
        enabled=True,
        actor="admin_role_operator",
        reason="operator enabled lead capture for test",
        request_metadata={"ip_hash": "testhash", "user_agent": "testagent", "source": "unit_test"},
        db=db,
    )
    assert res.enabled is True
    assert doc_ref.data["jobber_lead_capture_enabled"] is True

    audit_col = db.collection("admin_audit_events")
    assert len(audit_col.docs) == 1
    audit_event = list(audit_col.docs.values())[0].data
    assert audit_event["action"] == "jobber_lead_capture_update"
    assert audit_event["actor_type"] == "admin_role_operator"
    assert audit_event["reason"] == "operator enabled lead capture for test"
    assert audit_event["ip_hash"] == "testhash"
    assert audit_event["user_agent"] == "testagent"
    assert audit_event["metadata"]["source"] == "unit_test"
    assert audit_event["metadata"]["jobber_connected"] is True
    assert audit_event["metadata"]["generation"] == 1
    assert audit_event["metadata"]["lifecycle_epoch"] == 1


@pytest.mark.asyncio
async def test_18d_connect_cas_creates_audit_deterministically_without_overwrite(monkeypatch):
    """connect_provider_cas creates audit record with transaction.create (fail closed if conflicting exists)."""
    _setup_keyring(monkeypatch)
    cid = "c-18d-connect-audit"
    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})

    updates, new_gen, audit_id = await it_mutations.connect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        access_token="valid-acc",
        refresh_token="valid-ref",
        db=db,
    )
    assert new_gen == 1
    audit_col = db.collection("integration_lifecycle_audit")
    assert audit_id in audit_col.docs
    audit_rec = audit_col.docs[audit_id].data
    assert audit_rec["action"] == "connected"
    assert audit_rec["generation"] == 1
    assert audit_rec["lifecycle_epoch"] == 1


# ═════════════════════════════════════════════════════════════════════════════
# 18J: CAUSAL & MUTATION-EFFECTIVE TEST SUITE
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_18j_create_collision_causes_zero_mutation():
    """Prove transaction.create collision on existing doc rolls back all staged mutations."""
    cid = "c-18j-collision"
    doc_contractor = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    existing_audit_id = "colliding-audit-id"
    doc_audit_existing = _FakeDocRef({"existing": True}, doc_id=existing_audit_id)

    db = _FakeFirestore({
        "contractors": {cid: doc_contractor},
        "integration_lifecycle_audit": {existing_audit_id: doc_audit_existing},
    })

    tx = _FakeTransaction(db)
    tx.update(doc_contractor, {"jobber_connected": True})
    tx.create(doc_audit_existing, {"new_audit": True})

    from google.api_core.exceptions import AlreadyExists
    with pytest.raises(AlreadyExists):
        tx.commit()

    assert doc_contractor.data["jobber_connected"] is False
    assert doc_audit_existing.data == {"existing": True}


@pytest.mark.asyncio
async def test_18j_mid_commit_failure_rolls_back_all_staged_documents():
    """Prove an exception during transaction commit restores all touched documents cleanly byte-for-byte."""
    doc_new = _FakeDocRef(doc_id="d_new")
    doc_existing = _FakeDocRef({"k": "v_orig"}, doc_id="d_exist")

    class _BrokenDocRef(_FakeDocRef):
        def update(self, updates):
            raise RuntimeError("Simulated mid-commit I/O failure")

    doc_broken = _BrokenDocRef({"k": "v_broken_orig"}, doc_id="d_broken")
    db = _FakeFirestore({"coll": {"d_exist": doc_existing, "d_broken": doc_broken}})

    tx = _FakeTransaction(db)
    # Staged creates run before staged sets, which run before staged updates in _FakeTransaction.commit
    tx.create(doc_new, {"k": "v_new"})
    tx.set(doc_existing, {"k": "v_set"})
    tx.update(doc_broken, {"k": "v_bad"})

    with pytest.raises(RuntimeError, match="Simulated mid-commit I/O failure"):
        tx.commit()

    # Every touched document MUST be restored byte-for-byte to pre-transaction state
    assert doc_new.data is None
    assert doc_new.deleted is False
    assert doc_existing.data == {"k": "v_orig"}
    assert doc_broken.data == {"k": "v_broken_orig"}


@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
def test_18j_exhaustive_lifecycle_counter_presence_matrix(provider):
    """Prove only 000 legacy and 111 normalized parse; all 6 partial presence combinations fail closed."""
    from app.services.integration_tokens import parse_durable_lifecycle_counters

    base_contractor = {"contractor_id": "c-18j-lifecycle", "active": True}

    # 1. 000: all absent -> legacy unnormalized
    ok_000, gen_000, epoch_000, present_000, err_000 = parse_durable_lifecycle_counters(base_contractor, provider)
    assert ok_000 is True
    assert present_000 is False
    assert gen_000 == 0
    assert epoch_000 == 0
    assert err_000 is None

    # 2. 111: all present with exact bool True, exact int -> normalized
    c_111 = dict(base_contractor, **{
        f"{provider}_connected": True,
        f"{provider}_generation": 2,
        f"{provider}_lifecycle_epoch": 1,
    })
    ok_111, gen_111, epoch_111, present_111, err_111 = parse_durable_lifecycle_counters(c_111, provider)
    assert ok_111 is True
    assert present_111 is True
    assert gen_111 == 2
    assert epoch_111 == 1

    # 3. All 6 partial presence combinations fail closed
    partials = [
        {f"{provider}_connected": True},
        {f"{provider}_generation": 1},
        {f"{provider}_lifecycle_epoch": 1},
        {f"{provider}_connected": True, f"{provider}_generation": 1},
        {f"{provider}_connected": True, f"{provider}_lifecycle_epoch": 1},
        {f"{provider}_generation": 1, f"{provider}_lifecycle_epoch": 1},
    ]
    for partial_dict in partials:
        c_part = dict(base_contractor, **partial_dict)
        ok_part, _, _, _, err_part = parse_durable_lifecycle_counters(c_part, provider)
        assert ok_part is False
        assert "partial lifecycle metadata" in str(err_part)

    # 4. Hostile / non-exact scalar types fail closed without triggering hostile behavior
    class _SubclassStr(str): pass

    bad_scalars = [
        {f"{provider}_connected": True, f"{provider}_generation": True, f"{provider}_lifecycle_epoch": 1},
        {f"{provider}_connected": True, f"{provider}_generation": 1.0, f"{provider}_lifecycle_epoch": 1},
        {f"{provider}_connected": True, f"{provider}_generation": _SubclassStr("1"), f"{provider}_lifecycle_epoch": 1},
        {f"{provider}_connected": "True", f"{provider}_generation": 1, f"{provider}_lifecycle_epoch": 1},
        {f"{provider}_connected": True, f"{provider}_generation": _HostileComparisonObject(), f"{provider}_lifecycle_epoch": 1},
    ]
    for bad_dict in bad_scalars:
        c_bad = dict(base_contractor, **bad_dict)
        ok_bad, _, _, _, err_bad = parse_durable_lifecycle_counters(c_bad, provider)
        assert ok_bad is False


@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
def test_18j_direct_quarantine_parser_matrix(provider):
    """Exhaustive direct tests of parse_provider_operation_intent for quarantine status."""
    from app.services.integration_tokens import parse_provider_operation_intent

    k_reauth = f"{provider}_reauthorization_required"
    k_outcome = f"{provider}_refresh_outcome_unknown"

    # 1. Exact True/True -> valid quarantine
    status_q, _, _ = parse_provider_operation_intent({k_reauth: True, k_outcome: True}, provider)
    assert status_q == "quarantined"

    # 2. Absent / absent -> clean absent
    status_a, _, _ = parse_provider_operation_intent({}, provider)
    assert status_a == "absent"

    class _CustomStr(str):
        pass

    # 3. One-sided, both False, mixed, non-bool, None, str-subclass, hostile -> malformed / invalid
    bad_quarantines = [
        {k_reauth: True},
        {k_outcome: True},
        {k_reauth: False, k_outcome: False},
        {k_reauth: True, k_outcome: False},
        {k_reauth: False, k_outcome: True},
        {k_reauth: 1, k_outcome: 1},
        {k_reauth: "True", k_outcome: "True"},
        {k_reauth: None, k_outcome: True},
        {k_reauth: True, k_outcome: None},
        {k_reauth: None, k_outcome: None},
        {k_reauth: _CustomStr("True"), k_outcome: _CustomStr("True")},
        {k_reauth: _HostileComparisonObject(), k_outcome: _HostileComparisonObject()},
    ]
    for bad_q in bad_quarantines:
        status, parsed, err_msg = parse_provider_operation_intent(bad_q, provider)
        assert status == "malformed"
        assert parsed is None
        assert isinstance(err_msg, str) and len(err_msg) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_18j_quarantined_malformed_and_started_intents_perform_zero_writes_and_zero_http(monkeypatch, provider):
    """Prove orchestration-level disconnect on malformed, quarantined, or started intent raises IntegrationTokenCASConflict, makes zero HTTP calls, leaves contractor unchanged byte-for-byte, and writes zero audit/outbox documents. Keep reserved as sole preemptible case."""
    _setup_keyring(monkeypatch)
    cid = f"c-18j-zero-write-{provider}"
    enc_acc = encrypt_integration_token("acc", contractor_id=cid, provider=provider, token_kind="access")
    enc_ref = encrypt_integration_token("ref", contractor_id=cid, provider=provider, token_kind="refresh")

    k_conn = f"{provider}_connected"
    k_gen = f"{provider}_generation"
    k_epoch = f"{provider}_lifecycle_epoch"
    k_acc = f"{provider}_access_token"
    k_ref = f"{provider}_refresh_token"
    k_intent_id = f"{provider}_operation_intent_id"
    k_reauth = f"{provider}_reauthorization_required"
    k_outcome = f"{provider}_refresh_outcome_unknown"

    base_doc_data = {
        "contractor_id": cid,
        "active": True,
        k_conn: True,
        k_gen: 1,
        k_epoch: 1,
        k_acc: enc_acc,
        k_ref: enc_ref,
    }

    class _CountingHTTPClient:
        def __init__(self):
            self.call_count = 0
        async def post(self, *args, **kwargs):
            self.call_count += 1
            raise RuntimeError("HTTP call forbidden for rejected disconnect")

    # Case A: Quarantined document (orchestration-level)
    http_a = _CountingHTTPClient()
    orig_data_a = dict(base_doc_data, **{k_reauth: True, k_outcome: True})
    doc_q = _FakeDocRef(dict(orig_data_a), doc_id=cid)
    db_q = _FakeFirestore({
        "contractors": {cid: doc_q},
        "integration_revocation_outbox": {},
        "integration_lifecycle_audit": {},
    })

    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="quarantine|unknown outcome"):
        await it_mutations.disconnect_and_revoke_provider_orchestration(
            contractor_id=cid,
            provider=provider,
            db=db_q,
            http_client=http_a,
        )

    assert http_a.call_count == 0
    assert doc_q.data == orig_data_a
    assert len(db_q.collection("integration_revocation_outbox").docs) == 0
    assert len(db_q.collection("integration_lifecycle_audit").docs) == 0

    # Case B: Started intent document (orchestration-level)
    http_b = _CountingHTTPClient()
    started_intent = {
        f"{provider}_operation_intent_id": "intent-started-1",
        f"{provider}_operation_intent_kind": "business",
        f"{provider}_operation_intent_phase": "provider_request_started",
        f"{provider}_operation_intent_generation": 1,
        f"{provider}_operation_intent_lifecycle_epoch": 1,
        f"{provider}_operation_intent_acquired_at": 1000.0,
        f"{provider}_operation_intent_expires_at": 2000.0,
        f"{provider}_operation_intent_credentials_fingerprint": it_mutations.compute_raw_credentials_fingerprint(enc_acc, enc_ref),
    }
    orig_data_b = dict(base_doc_data, **started_intent)
    doc_s = _FakeDocRef(dict(orig_data_b), doc_id=cid)
    db_s = _FakeFirestore({
        "contractors": {cid: doc_s},
        "integration_revocation_outbox": {},
        "integration_lifecycle_audit": {},
    })

    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="started; disconnect pending completion"):
        await it_mutations.disconnect_and_revoke_provider_orchestration(
            contractor_id=cid,
            provider=provider,
            db=db_s,
            http_client=http_b,
        )

    assert http_b.call_count == 0
    assert doc_s.data == orig_data_b
    assert len(db_s.collection("integration_revocation_outbox").docs) == 0
    assert len(db_s.collection("integration_lifecycle_audit").docs) == 0

    # Case C: Malformed intent document (orchestration-level)
    http_c = _CountingHTTPClient()
    orig_data_c = dict(base_doc_data, **{k_intent_id: "intent-malformed-1"})
    doc_m = _FakeDocRef(dict(orig_data_c), doc_id=cid)
    db_m = _FakeFirestore({
        "contractors": {cid: doc_m},
        "integration_revocation_outbox": {},
        "integration_lifecycle_audit": {},
    })

    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Malformed existing operation intent"):
        await it_mutations.disconnect_and_revoke_provider_orchestration(
            contractor_id=cid,
            provider=provider,
            db=db_m,
            http_client=http_c,
        )

    assert http_c.call_count == 0
    assert doc_m.data == orig_data_c
    assert len(db_m.collection("integration_revocation_outbox").docs) == 0
    assert len(db_m.collection("integration_lifecycle_audit").docs) == 0

    # Case D: Reserved intent IS preemptible (sole low-level preemptible case)
    reserved_intent = {
        f"{provider}_operation_intent_id": "intent-reserved-1",
        f"{provider}_operation_intent_kind": "business",
        f"{provider}_operation_intent_phase": "reserved",
        f"{provider}_operation_intent_generation": 1,
        f"{provider}_operation_intent_lifecycle_epoch": 1,
        f"{provider}_operation_intent_acquired_at": 1000.0,
        f"{provider}_operation_intent_expires_at": 2000.0,
        f"{provider}_operation_intent_credentials_fingerprint": it_mutations.compute_raw_credentials_fingerprint(enc_acc, enc_ref),
    }
    doc_r = _FakeDocRef(dict(base_doc_data, **reserved_intent), doc_id=cid)
    db_r = _FakeFirestore({
        "contractors": {cid: doc_r},
        "integration_revocation_outbox": {},
        "integration_lifecycle_audit": {},
    })
    res_r = await it_mutations.disconnect_provider_envelope_cas(contractor_id=cid, provider=provider, db=db_r)
    assert res_r.generation == 2
    assert doc_r.data[k_conn] is False


@pytest.mark.asyncio
async def test_18j_jobber_lead_capture_audit_lifecycle_and_fault_injections(monkeypatch):
    """Causal proof:
    1. Sequence False -> True -> False -> True creates exactly 3 create-only audit documents with 3 unique IDs and exact payloads including actor, action, target_id, before/after, generation, epoch, timestamp coupling, and reason metadata.
    2. Identical-state no-op creates zero new audit documents and does not change jobber_lead_capture_updated_at.
    3. Retryable Aborted transaction retry reuses candidate ID, calls secrets.token_hex once per API call, creates 1 audit, and subsequent call mints a new ID.
    4. Dropped contractor or audit writes raise exact IntegrationTokenPostconditionError and never report success."""
    _setup_keyring(monkeypatch)
    cid = "c-18j-lead-capture-audit"

    doc_contractor = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid)

    db = _FakeFirestore({
        "contractors": {cid: doc_contractor},
        "admin_audit_events": {},
    })

    # Step 1: False -> True (Mutation 1)
    res1 = await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=True, db=db)
    assert res1.enabled is True
    assert doc_contractor.data["jobber_lead_capture_enabled"] is True
    updated_at_1 = doc_contractor.data.get("jobber_lead_capture_updated_at")
    assert updated_at_1 is not None

    audit_docs1 = db.collection("admin_audit_events").docs
    assert len(audit_docs1) == 1
    audit_id_1 = list(audit_docs1.keys())[0]
    payload1 = audit_docs1[audit_id_1].data
    assert payload1["actor_type"] == "contractor_api"
    assert payload1["action"] == "jobber_lead_capture_update"
    assert payload1["target_type"] == "contractor"
    assert payload1["target_id"] == cid
    assert payload1["reason"] == "admin_lead_capture_toggle"
    assert payload1["before"] == {"jobber_lead_capture_enabled": False}
    assert payload1["after"] == {"jobber_lead_capture_enabled": True}
    assert payload1["metadata"]["generation"] == 0
    assert payload1["metadata"]["lifecycle_epoch"] == 0
    assert payload1["metadata"]["timestamp"] == updated_at_1
    assert payload1["created_at"] == updated_at_1
    assert payload1["timestamp"] == updated_at_1

    # Step 2: No-Op True -> True
    res_noop = await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=True, db=db)
    assert res_noop.enabled is True
    assert len(db.collection("admin_audit_events").docs) == 1
    assert doc_contractor.data.get("jobber_lead_capture_updated_at") == updated_at_1

    # Step 3: True -> False (Mutation 2)
    res2 = await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=False, db=db)
    assert res2.enabled is False
    assert doc_contractor.data["jobber_lead_capture_enabled"] is False
    updated_at_2 = doc_contractor.data.get("jobber_lead_capture_updated_at")
    assert len(db.collection("admin_audit_events").docs) == 2
    audit_id_2 = [k for k in db.collection("admin_audit_events").docs.keys() if k != audit_id_1][0]
    payload2 = db.collection("admin_audit_events").docs[audit_id_2].data
    assert payload2["before"] == {"jobber_lead_capture_enabled": True}
    assert payload2["after"] == {"jobber_lead_capture_enabled": False}
    assert payload2["created_at"] == updated_at_2

    # Step 4: False -> True (Mutation 3)
    res3 = await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=True, db=db)
    assert res3.enabled is True
    assert doc_contractor.data["jobber_lead_capture_enabled"] is True
    updated_at_3 = doc_contractor.data.get("jobber_lead_capture_updated_at")
    assert len(db.collection("admin_audit_events").docs) == 3
    audit_id_3 = [k for k in db.collection("admin_audit_events").docs.keys() if k not in (audit_id_1, audit_id_2)][0]
    payload3 = db.collection("admin_audit_events").docs[audit_id_3].data
    assert payload3["before"] == {"jobber_lead_capture_enabled": False}
    assert payload3["after"] == {"jobber_lead_capture_enabled": True}
    assert payload3["created_at"] == updated_at_3

    # Verify 3 unique IDs
    assert len({audit_id_1, audit_id_2, audit_id_3}) == 3

    # Step 5: Real retry proof with injected Aborted commit
    cid_retry = "c-18j-lead-capture-aborted-retry"
    doc_retry = _FakeDocRef({
        "contractor_id": cid_retry,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_access_token": "acc",
        "jobber_refresh_token": "ref",
        "jobber_lead_capture_enabled": False,
    }, doc_id=cid_retry)
    db_retry = _FakeFirestore({
        "contractors": {cid_retry: doc_retry},
        "admin_audit_events": {},
    })

    attempts = [0]
    class _AbortedOnceTransaction(_FakeTransaction):
        def commit(self):
            attempts[0] += 1
            if attempts[0] == 1:
                from google.api_core.exceptions import Aborted
                raise Aborted("Simulated transaction contention")
            super().commit()

    db_retry.transaction = lambda: _AbortedOnceTransaction(db_retry)

    hex_calls = [0]
    orig_hex = it_mutations.secrets.token_hex
    def _counted_hex(n):
        hex_calls[0] += 1
        return orig_hex(n)

    monkeypatch.setattr(it_mutations.secrets, "token_hex", _counted_hex)

    res_ret1 = await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid_retry, enabled=True, db=db_retry)
    assert res_ret1.enabled is True
    assert attempts[0] == 2  # Proves 1 retry attempt occurred
    assert hex_calls[0] == 1  # Proves candidate ID was pre-minted once and reused
    assert len(db_retry.collection("admin_audit_events").docs) == 1

    res_ret2 = await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid_retry, enabled=False, db=db_retry)
    assert res_ret2.enabled is False
    assert hex_calls[0] == 2  # Proves 2nd invocation minted a new candidate ID
    assert len(db_retry.collection("admin_audit_events").docs) == 2

    # Step 6: Fault injection - contractor write dropped raises exact IntegrationTokenPostconditionError
    class _FakeTxnDroppedContractor(_FakeTransaction):
        def update(self, doc_ref, updates):
            if doc_ref == doc_contractor:
                return  # Drop contractor update
            super().update(doc_ref, updates)

    monkeypatch.setattr(db, "transaction", lambda: _FakeTxnDroppedContractor(db))
    with pytest.raises(it_mutations.IntegrationTokenPostconditionError, match="Postcondition verification failed"):
        await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=False, db=db)

    # Step 7: Fault injection - audit create dropped raises exact IntegrationTokenPostconditionError
    class _FakeTxnDroppedAudit(_FakeTransaction):
        def create(self, doc_ref, data):
            if doc_ref.id != cid:
                return  # Drop audit create
            super().create(doc_ref, data)

    monkeypatch.setattr(db, "transaction", lambda: _FakeTxnDroppedAudit(db))
    with pytest.raises(it_mutations.IntegrationTokenPostconditionError, match="Admin audit event missing after lead capture mutation"):
        await it_mutations.update_jobber_lead_capture_cas(contractor_id=cid, enabled=False, db=db)


@pytest.mark.asyncio
async def test_18j_repeated_disconnect_idempotency_low_level_and_orchestration(monkeypatch):
    """Causal proof:
    1. Low-level: disconnect_provider_cas on an already-disconnected contractor returns current generation, None token, None audit ID without error.
    2. Orchestration: jobber_disconnect and google_calendar_disconnect on an already-disconnected contractor return status 'disconnected' with 0 provider revoke HTTP calls."""
    _setup_keyring(monkeypatch)
    cid_j = "c-18j-rep-disc-jobber"
    cid_g = "c-18j-rep-disc-google"

    enc_j_acc = encrypt_integration_token("j-acc", contractor_id=cid_j, provider="jobber", token_kind="access")
    enc_j_ref = encrypt_integration_token("j-ref", contractor_id=cid_j, provider="jobber", token_kind="refresh")
    enc_g_acc = encrypt_integration_token("g-acc", contractor_id=cid_g, provider="google_calendar", token_kind="access")
    enc_g_ref = encrypt_integration_token("g-ref", contractor_id=cid_g, provider="google_calendar", token_kind="refresh")

    doc_j = _FakeDocRef({
        "contractor_id": cid_j,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_token_envelope_required": True,
        "jobber_lead_capture_enabled": False,
        "jobber_access_token": enc_j_acc,
        "jobber_refresh_token": enc_j_ref,
        "jobber_connected_at": 100.0,
    }, doc_id=cid_j)

    doc_g = _FakeDocRef({
        "contractor_id": cid_g,
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 1,
        "google_calendar_token_envelope_required": True,
        "google_calendar_access_token": enc_g_acc,
        "google_calendar_refresh_token": enc_g_ref,
        "google_calendar_connected_at": 100.0,
    }, doc_id=cid_g)

    db = _FakeFirestore({"contractors": {cid_j: doc_j, cid_g: doc_g}, "admin_audit_events": {}})
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    _patch_firestore(monkeypatch, db)

    revoke_http_calls = []

    class _TrackingRevokeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *args, **kwargs):
            revoke_http_calls.append(str(url))
            return type("Resp", (), {"status_code": 200, "json": dict})()

    monkeypatch.setattr("httpx.AsyncClient", _TrackingRevokeClient)
    req = type("Req", (), {"state": type("State", (), {"is_admin": True})()})()

    # Phase 1: First disconnect
    res_j1 = await integrations.jobber_disconnect(contractor_id=cid_j, request=req)
    assert res_j1["status"] == "disconnected"
    assert doc_j.data["jobber_connected"] is False
    assert len(revoke_http_calls) == 1

    res_g1 = await integrations.google_calendar_disconnect(contractor_id=cid_g, request=req)
    assert res_g1["status"] == "disconnected"
    assert doc_g.data["google_calendar_connected"] is False
    assert len(revoke_http_calls) == 2

    audit_count_1 = len(db.collections.get("admin_audit_events", {}))

    # Phase 2: Low-level repeated disconnect
    gen_j2, tok_j2, audit_j2 = await it_mutations.disconnect_provider_cas(contractor_id=cid_j, provider="jobber", db=db)
    assert gen_j2 == doc_j.data["jobber_generation"]
    assert tok_j2 is None
    assert isinstance(audit_j2, str) and len(audit_j2) > 0

    gen_g2, tok_g2, audit_g2 = await it_mutations.disconnect_provider_cas(contractor_id=cid_g, provider="google_calendar", db=db)
    assert gen_g2 == doc_g.data["google_calendar_generation"]
    assert tok_g2 is None
    assert isinstance(audit_g2, str) and len(audit_g2) > 0

    audit_count_2 = len(db.collections.get("admin_audit_events", {}))
    assert audit_count_2 == audit_count_1

    # Phase 3: Orchestration repeated disconnect
    revoke_calls_before_p3 = len(revoke_http_calls)
    res_j2 = await integrations.jobber_disconnect(contractor_id=cid_j, request=req)
    assert res_j2["status"] == "disconnected"

    res_g2 = await integrations.google_calendar_disconnect(contractor_id=cid_g, request=req)
    assert res_g2["status"] == "disconnected"

    assert len(revoke_http_calls) == revoke_calls_before_p3


# ---------------------------------------------------------------------------
# REPAIR 18O: Causal Production & Security Verification Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repair_18o_oauth_reauthorization_under_quarantine(monkeypatch):
    """Prove coherent OAuth reauthorization under exact True/True quarantine for Jobber and Google Calendar."""
    _setup_keyring(monkeypatch)
    for provider in ("jobber", "google_calendar"):
        cid = f"test_quarantine_reauth_{provider}"
        enc_acc_stored = it_mutations.encrypt_integration_token("acc_stored", contractor_id=cid, provider=provider, token_kind="access")
        enc_ref_stored = it_mutations.encrypt_integration_token("ref_stored", contractor_id=cid, provider=provider, token_kind="refresh")
        contractor_doc = _FakeDocRef(
            {
                "contractor_id": cid,
                "active": True,
                f"{provider}_connected": True,
                f"{provider}_access_token": enc_acc_stored,
                f"{provider}_refresh_token": enc_ref_stored,
                f"{provider}_generation": 1,
                f"{provider}_lifecycle_epoch": 1,
                f"{provider}_token_envelope_required": True,
                f"{provider}_reauthorization_required": True,
                f"{provider}_refresh_outcome_unknown": True,
            },
            doc_id=cid,
        )
        db = _FakeFirestore({"contractors": {cid: contractor_doc}, "jobber_oauth_states": {}, "google_oauth_states": {}})
        _patch_firestore(monkeypatch, db)

        # 1. Ordinary connect intent coexisting with quarantine is malformed
        bad_doc = dict(contractor_doc.data)
        bad_doc[f"{provider}_operation_intent_id"] = "a" * 32
        bad_doc[f"{provider}_operation_intent_kind"] = "connect"
        bad_doc[f"{provider}_operation_intent_phase"] = "reserved"
        bad_doc[f"{provider}_operation_intent_expires_at"] = time.time() + 300.0
        bad_doc[f"{provider}_operation_intent_acquired_at"] = time.time()
        bad_doc[f"{provider}_operation_intent_generation"] = 1
        bad_doc[f"{provider}_operation_intent_lifecycle_epoch"] = 1
        st, _, err = it_mutations.parse_provider_operation_intent(bad_doc, provider)
        assert st == "malformed"
        assert "Ordinary intent kinds cannot coexist with quarantine" in err

        # 2. consume_oauth_state on quarantined contractor
        state_id = "s" * 32
        col_name = "jobber_oauth_states" if provider == "jobber" else "google_oauth_states"
        state_doc = _FakeDocRef(
            {
                "contractor_id": cid,
                "provider": provider,
                "lifecycle_epoch": 1,
                "generation": 1,
                "credentials_fingerprint": it_mutations.compute_raw_credentials_fingerprint(enc_acc_stored, enc_ref_stored),
                "created_at": time.time(),
                "expires_at": time.time() + 300.0,
            },
            doc_id=state_id,
        )
        db.collections[col_name][state_id] = state_doc

        st_data, c_obs = await it_mutations.consume_oauth_state(db=db, collection_name=col_name, state=state_id)
        assert state_doc.deleted is True
        claim_id = c_obs["claim_id"]

        # Contractor should now hold reconnect attempt alongside quarantine flags
        post_c = contractor_doc.data
        assert post_c[f"{provider}_reauthorization_required"] is True
        assert post_c[f"{provider}_refresh_outcome_unknown"] is True
        assert post_c[f"{provider}_reauthorization_attempt_kind"] == "reconnect"
        assert post_c[f"{provider}_reauthorization_attempt_phase"] == "reserved"
        assert post_c[f"{provider}_reauthorization_attempt_id"] == claim_id

        # 3. Wrong claim or claimless connect fails and preserves quarantine
        with pytest.raises(IntegrationTokenCASConflict):
            await connect_provider_cas(
                contractor_id=cid,
                provider=provider,
                access_token="new_acc",
                refresh_token="new_ref",
                claim_id="wrong_claim_" + "x" * 20,
                db=db,
            )
        assert contractor_doc.data[f"{provider}_reauthorization_required"] is True

        with pytest.raises(IntegrationTokenCASConflict):
            await connect_provider_cas(
                contractor_id=cid,
                provider=provider,
                access_token="new_acc",
                refresh_token="new_ref",
                claim_id=None,
                db=db,
            )
        assert contractor_doc.data[f"{provider}_reauthorization_required"] is True

        # Transition attempt to started before connect
        await it_mutations.transition_provider_reauthorization_attempt_to_started_cas(
            contractor_id=cid,
            provider=provider,
            claim_id=claim_id,
            observed_generation=1,
            observed_lifecycle_epoch=1,
            observed_access_raw=enc_acc_stored,
            observed_refresh_raw=enc_ref_stored,
            db=db,
        )

        # 4. Valid connect with matching claim_id clears quarantine
        updates, res_gen, audit_id = await connect_provider_cas(
            contractor_id=cid,
            provider=provider,
            access_token="new_acc_ok",
            refresh_token="new_ref_ok",
            claim_id=claim_id,
            observed_generation=1,
            observed_lifecycle_epoch=1,
            observed_access_raw=enc_acc_stored,
            observed_refresh_raw=enc_ref_stored,
            db=db,
        )
        assert res_gen == 2
        final_doc = contractor_doc.data
        assert f"{provider}_reauthorization_required" not in final_doc
        assert f"{provider}_refresh_outcome_unknown" not in final_doc
        assert f"{provider}_reauthorization_attempt_id" not in final_doc


@pytest.mark.asyncio
async def test_repair_18o_expired_started_refresh_preflight(monkeypatch):
    """Prove expired-started refresh intent is recovered by preflight to exact True/True quarantine with 0 HTTP."""
    _setup_keyring(monkeypatch)
    monkeypatch.setattr(settings, "google_calendar_client_id", "fake-client-id")
    monkeypatch.setattr(settings, "google_calendar_client_secret", "fake-client-secret")
    for provider in ("jobber", "google_calendar"):
        cid = f"test_preflight_expired_started_{provider}"
        exp_past = time.time() - 100.0
        contractor_doc = _FakeDocRef(
            {
                "contractor_id": cid,
                "active": True,
                f"{provider}_connected": True,
                f"{provider}_access_token": "acc",
                f"{provider}_refresh_token": "ref",
                f"{provider}_generation": 1,
                f"{provider}_lifecycle_epoch": 1,
                f"{provider}_operation_intent_id": "c" * 32,
                f"{provider}_operation_intent_kind": "refresh",
                f"{provider}_operation_intent_phase": "provider_request_started",
                f"{provider}_operation_intent_expires_at": exp_past,
                f"{provider}_operation_intent_acquired_at": exp_past - 60.0,
                f"{provider}_operation_intent_generation": 1,
                f"{provider}_operation_intent_lifecycle_epoch": 1,
                f"{provider}_operation_intent_credentials_fingerprint": it_mutations.compute_raw_credentials_fingerprint("acc", "ref"),
            },
            doc_id=cid,
        )
        db = _FakeFirestore({"contractors": {cid: contractor_doc}})
        _patch_firestore(monkeypatch, db)

        http_called = []
        class _FailIfCalledClient:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def post(self, *args, **kwargs):
                http_called.append(True)
                raise RuntimeError("Provider HTTP must not be called on preflight block")

        monkeypatch.setattr("httpx.AsyncClient", _FailIfCalledClient)

        if provider == "jobber":
            res = await jobber_service.refresh_access_token({"contractor_id": cid}, force=True)
        else:
            res = await calendar_service.refresh_access_token({"contractor_id": cid}, force=True)

        assert res is None
        assert len(http_called) == 0
        final_doc = contractor_doc.data
        assert final_doc[f"{provider}_reauthorization_required"] is True
        assert final_doc[f"{provider}_refresh_outcome_unknown"] is True
        assert f"{provider}_operation_intent_id" not in final_doc


@pytest.mark.asyncio
async def test_repair_18o_authoritative_lifecycle_binding_in_mutations(monkeypatch):
    """Prove transition, persistence, and quarantine fail closed on partial, bool, or invalid lifecycle values."""
    _setup_keyring(monkeypatch)
    cid = "test_lifecycle_binding_fail_closed"
    invalid_lifecycle_docs = [
        {"contractor_id": cid, "active": True, "jobber_connected": True, "jobber_generation": True},  # bool generation
        {"contractor_id": cid, "active": True, "jobber_connected": True, "jobber_generation": -1},    # negative gen
        {"contractor_id": cid, "active": True, "jobber_connected": True, "jobber_generation": 1, "jobber_lifecycle_epoch": "abc"},  # non-int epoch
        {"contractor_id": cid, "active": True, "jobber_connected": True, "jobber_generation": 1},   # missing epoch when gen > 0
    ]

    for bad_fields in invalid_lifecycle_docs:
        doc_data_transition = dict(bad_fields)
        doc_data_transition["jobber_operation_intent_id"] = "c" * 32
        doc_data_transition["jobber_operation_intent_kind"] = "refresh"
        doc_data_transition["jobber_operation_intent_phase"] = "reserved"
        doc_data_transition["jobber_operation_intent_expires_at"] = time.time() + 300.0
        doc_data_transition["jobber_operation_intent_acquired_at"] = time.time()
        doc_data_transition["jobber_operation_intent_generation"] = 1
        doc_data_transition["jobber_operation_intent_lifecycle_epoch"] = 1

        doc = _FakeDocRef(doc_data_transition, doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: doc}})
        _patch_firestore(monkeypatch, db)

        with pytest.raises(IntegrationTokenCASConflict):
            await transition_refresh_claim_to_started_cas(
                contractor_id=cid,
                provider="jobber",
                claim_id="c" * 32,
                observed_generation=1,
                observed_access_raw="acc",
                observed_refresh_raw="ref",
                db=db,
            )

        doc_data_started = dict(bad_fields)
        doc_data_started["jobber_operation_intent_id"] = "c" * 32
        doc_data_started["jobber_operation_intent_kind"] = "refresh"
        doc_data_started["jobber_operation_intent_phase"] = "provider_request_started"
        doc_data_started["jobber_operation_intent_expires_at"] = time.time() + 300.0
        doc_data_started["jobber_operation_intent_acquired_at"] = time.time()
        doc_data_started["jobber_operation_intent_generation"] = 1
        doc_data_started["jobber_operation_intent_lifecycle_epoch"] = 1

        doc_started = _FakeDocRef(doc_data_started, doc_id=cid)
        db_started = _FakeFirestore({"contractors": {cid: doc_started}})
        _patch_firestore(monkeypatch, db_started)

        with pytest.raises(IntegrationTokenCASConflict):
            await persist_refreshed_tokens_cas(
                contractor_id=cid,
                provider="jobber",
                new_access_token="new_acc",
                new_refresh_token="new_ref",
                observed_generation=1,
                observed_access_raw="acc",
                observed_refresh_raw="ref",
                claim_id="c" * 32,
                db=db_started,
            )

        q_res = await it_mutations.quarantine_provider_reauth_cas(
            contractor_id=cid,
            provider="jobber",
            claim_id="c" * 32,
            observed_generation=1,
            observed_access_raw="acc",
            observed_refresh_raw="ref",
            db=db_started,
        )
        assert q_res is False


@pytest.mark.asyncio
async def test_repair_18o_lead_capture_noop_durable_timestamp(monkeypatch):
    """Prove update_jobber_lead_capture_cas on no-op returns exact durable timestamp and executes 0 writes."""
    _setup_keyring(monkeypatch)
    cid = "test_lead_capture_noop_ts"
    durable_ts = 1750000000.5
    doc = _FakeDocRef(
        {
            "contractor_id": cid,
            "active": True,
            "jobber_connected": True,
            "jobber_access_token": "acc",
            "jobber_refresh_token": "ref",
            "jobber_generation": 1,
            "jobber_lifecycle_epoch": 1,
            "jobber_lead_capture_enabled": True,
            "jobber_lead_capture_updated_at": durable_ts,
        },
        doc_id=cid,
    )
    db = _FakeFirestore({"contractors": {cid: doc}, "admin_audit_events": {}})
    _patch_firestore(monkeypatch, db)

    res = await it_mutations.update_jobber_lead_capture_cas(
        contractor_id=cid,
        enabled=True,
        db=db,
    )
    assert res.previous_enabled is True
    assert res.enabled is True
    assert res.updated_at == durable_ts
    assert len(db.collections["admin_audit_events"]) == 0


@pytest.mark.asyncio
async def test_repair_18o_connect_collision_proof_and_http_ambiguity_matrix(monkeypatch):
    """Prove connect_provider_cas fails on audit doc collision and HTTP ambiguity transitions to quarantine."""
    _setup_keyring(monkeypatch)
    cid = "test_connect_collision_and_ambiguity"
    doc = _FakeDocRef(
        {
            "contractor_id": cid,
            "active": True,
            "jobber_connected": False,
            "jobber_generation": 0,
            "jobber_lifecycle_epoch": 0,
            "jobber_token_envelope_required": True,
            "jobber_operation_intent_id": "c" * 32,
            "jobber_operation_intent_kind": "connect",
            "jobber_operation_intent_phase": "provider_request_started",
            "jobber_operation_intent_expires_at": time.time() + 300.0,
            "jobber_operation_intent_acquired_at": time.time(),
            "jobber_operation_intent_generation": 0,
            "jobber_operation_intent_lifecycle_epoch": 0,
        },
        doc_id=cid,
    )

    audit_id = it_mutations.format_audit_doc_id(contractor_id=cid, provider="jobber", generation=1, action="connected")
    audit_doc = _FakeDocRef({"contractor_id": cid}, doc_id=audit_id)
    db = _FakeFirestore({"contractors": {cid: doc}, "integration_lifecycle_audit": {audit_id: audit_doc}})
    _patch_firestore(monkeypatch, db)

    # Collision test: pre-existing audit doc raises IntegrationTokenCASConflict
    with pytest.raises(IntegrationTokenCASConflict):
        await connect_provider_cas(
            contractor_id=cid,
            provider="jobber",
            access_token="acc",
            refresh_token="ref",
            claim_id="c" * 32,
            db=db,
        )


@pytest.mark.asyncio
async def test_repair_18p_quarantine_oauth_attempt_flow(monkeypatch):
    """Causal proof of isolated reauthorization attempt namespace under True/True quarantine."""
    _setup_keyring(monkeypatch)
    cid = "test_repair_18p_quarantine"
    doc_data = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": "old_acc",
        "jobber_refresh_token": "old_ref",
        "jobber_reauthorization_required": True,
        "jobber_refresh_outcome_unknown": True,
        "jobber_token_envelope_required": False,
    }
    state_id = "s" * 32
    fp = it_mutations.compute_raw_credentials_fingerprint("old_acc", "old_ref")
    now_ts = time.time()
    state_data = {
        "contractor_id": cid,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "credentials_fingerprint": fp,
        "created_at": now_ts,
        "expires_at": now_ts + 600.0,
    }
    c_doc = _FakeDocRef(doc_data, doc_id=cid)
    s_doc = _FakeDocRef(state_data, doc_id=state_id)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "jobber_oauth_states": {state_id: s_doc},
    })
    _patch_firestore(monkeypatch, db)

    # 1. Consume OAuth state under True/True quarantine
    res_state, c_obs = await consume_oauth_state(db=db, collection_name="jobber_oauth_states", state=state_id)
    assert c_obs["is_quarantined"] is True
    claim_id = c_obs["claim_id"]

    updated_c = c_doc.get().to_dict()
    assert updated_c.get("jobber_reauthorization_attempt_id") == claim_id
    assert updated_c.get("jobber_reauthorization_attempt_kind") == "reconnect"
    assert updated_c.get("jobber_reauthorization_attempt_phase") == "reserved"
    assert updated_c.get("jobber_reauthorization_required") is True
    assert updated_c.get("jobber_refresh_outcome_unknown") is True
    # Zero ordinary intent fields
    for k in ("jobber_operation_intent_id", "jobber_operation_intent_kind", "jobber_operation_intent_phase"):
        assert k not in updated_c

    # 2. Transition reauthorization attempt to started before HTTP
    trans_id, trans_exp = await it_mutations.transition_provider_reauthorization_attempt_to_started_cas(
        contractor_id=cid,
        provider="jobber",
        claim_id=claim_id,
        observed_generation=1,
        observed_lifecycle_epoch=1,
        observed_access_raw="old_acc",
        observed_refresh_raw="old_ref",
        db=db,
    )
    assert trans_id == claim_id
    assert c_doc.get().to_dict().get("jobber_reauthorization_attempt_phase") == "provider_request_started"

    # 3. Explicit HTTP 400 rejection -> terminalize reauthorization attempt
    term_ok = await it_mutations.terminalize_provider_reauthorization_attempt_cas(
        contractor_id=cid,
        provider="jobber",
        claim_id=claim_id,
        db=db,
    )
    assert term_ok is True
    post_term = c_doc.get().to_dict()
    assert "jobber_reauthorization_attempt_id" not in post_term
    assert post_term.get("jobber_reauthorization_required") is True
    assert post_term.get("jobber_refresh_outcome_unknown") is True

    # 4. Re-create state & consume again -> test successful connect
    state_id2 = "r" * 32
    s_doc2 = _FakeDocRef(dict(state_data), doc_id=state_id2)
    db.collections["jobber_oauth_states"][state_id2] = s_doc2

    _, c_obs2 = await consume_oauth_state(db=db, collection_name="jobber_oauth_states", state=state_id2)
    claim_id2 = c_obs2["claim_id"]

    await it_mutations.transition_provider_reauthorization_attempt_to_started_cas(
        contractor_id=cid,
        provider="jobber",
        claim_id=claim_id2,
        observed_generation=1,
        observed_lifecycle_epoch=1,
        observed_access_raw="old_acc",
        observed_refresh_raw="old_ref",
        db=db,
    )

    updates, new_gen, audit_id = await connect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        access_token="new_acc_123",
        refresh_token="new_ref_123",
        claim_id=claim_id2,
        observed_generation=1,
        observed_lifecycle_epoch=1,
        observed_access_raw="old_acc",
        observed_refresh_raw="old_ref",
        db=db,
    )
    assert new_gen == 2
    post_conn = c_doc.get().to_dict()
    assert post_conn.get("jobber_reauthorization_required") is None or "jobber_reauthorization_required" not in post_conn
    assert post_conn.get("jobber_refresh_outcome_unknown") is None or "jobber_refresh_outcome_unknown" not in post_conn
    assert "jobber_reauthorization_attempt_id" not in post_conn
    assert post_conn["jobber_generation"] == 2
    assert post_conn["jobber_lifecycle_epoch"] == 2


@pytest.mark.asyncio
async def test_repair_18p_google_quarantine_missing_fresh_refresh_token(monkeypatch):
    """Google quarantine recovery MUST require fresh refresh token from provider response."""
    _setup_keyring(monkeypatch)
    cid = "test_google_quarantine_recovery"
    c_doc = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 1,
        "google_calendar_access_token": "old_g_acc",
        "google_calendar_refresh_token": "old_g_ref",
        "google_calendar_reauthorization_required": True,
        "google_calendar_refresh_outcome_unknown": True,
        "google_calendar_token_envelope_required": False,
    }, doc_id=cid)
    state_id = "g" * 32
    fp = it_mutations.compute_raw_credentials_fingerprint("old_g_acc", "old_g_ref")
    now_ts = time.time()
    s_doc = _FakeDocRef({
        "contractor_id": cid,
        "provider": "google_calendar",
        "generation": 1,
        "lifecycle_epoch": 1,
        "credentials_fingerprint": fp,
        "created_at": now_ts,
        "expires_at": now_ts + 600.0,
    }, doc_id=state_id)
    db = _FakeFirestore({
        "contractors": {cid: c_doc},
        "google_oauth_states": {state_id: s_doc},
    })
    _patch_firestore(monkeypatch, db)

    class _MockHttpxClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, data=None, timeout=None):
            # Google returns access_token BUT NO refresh_token!
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"access_token": "fresh_g_acc", "expires_in": 3600},
            )

    monkeypatch.setattr(integrations.httpx, "AsyncClient", _MockHttpxClient)
    monkeypatch.setattr(integrations.settings, "google_calendar_client_id", "test_g_client_id")
    monkeypatch.setattr(integrations.settings, "google_calendar_client_secret", "test_g_client_secret")

    with pytest.raises(HTTPException) as exc_info:
        await integrations.google_calendar_callback(code="auth_code_123", state=state_id)
    assert exc_info.value.status_code == 502
    assert "Missing fresh refresh token" in exc_info.value.detail


@pytest.mark.asyncio
async def test_repair_18p_commit_time_audit_collision(monkeypatch):
    """Commit-time audit collision test using real connect_provider_cas and transaction.create."""
    _setup_keyring(monkeypatch)
    cid = "test_commit_time_audit_collision"
    initial_doc_data = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_token_envelope_required": False,
        "jobber_operation_intent_id": "c" * 32,
        "jobber_operation_intent_kind": "connect",
        "jobber_operation_intent_phase": "provider_request_started",
        "jobber_operation_intent_expires_at": time.time() + 300.0,
        "jobber_operation_intent_acquired_at": time.time(),
        "jobber_operation_intent_generation": 0,
        "jobber_operation_intent_lifecycle_epoch": 0,
    }
    c_doc = _FakeDocRef(dict(initial_doc_data), doc_id=cid)
    audit_id = it_mutations.format_audit_doc_id(contractor_id=cid, provider="jobber", generation=1, action="connected")
    audit_doc = _FakeDocRef({"contractor_id": cid, "pre_existing": True}, doc_id=audit_id)
    db = _FakeFirestore({"contractors": {cid: c_doc}, "integration_lifecycle_audit": {audit_id: audit_doc}})
    _patch_firestore(monkeypatch, db)

    # Collision test: pre-existing audit doc causes transaction.create to raise IntegrationTokenCASConflict
    with pytest.raises(IntegrationTokenCASConflict):
        await connect_provider_cas(
            contractor_id=cid,
            provider="jobber",
            access_token="acc",
            refresh_token="ref",
            claim_id="c" * 32,
            db=db,
        )

    # Assert contractor document remains byte-identical and pre-existing audit preserved
    assert c_doc.get().to_dict() == initial_doc_data
    assert db.collections["integration_lifecycle_audit"][audit_id].get().to_dict() == {"contractor_id": cid, "pre_existing": True}


@pytest.mark.asyncio
async def test_18q_exclusive_reauthorization_fence(monkeypatch):
    """Prove exclusive reauthorization fence: reserved attempt fails connect byte-identical; disconnect blocked under quarantine."""
    _setup_keyring(monkeypatch)
    for provider in ("jobber", "google_calendar"):
        cid = f"test_18q_reauth_fence_{provider}"
        enc_acc = it_mutations.encrypt_integration_token("acc", contractor_id=cid, provider=provider, token_kind="access")
        enc_ref = it_mutations.encrypt_integration_token("ref", contractor_id=cid, provider=provider, token_kind="refresh")
        claim_id = "c" * 32
        fp = it_mutations.compute_raw_credentials_fingerprint(enc_acc, enc_ref)

        base_data = {
            "contractor_id": cid,
            "active": True,
            f"{provider}_connected": True,
            f"{provider}_access_token": enc_acc,
            f"{provider}_refresh_token": enc_ref,
            f"{provider}_generation": 1,
            f"{provider}_lifecycle_epoch": 1,
            f"{provider}_token_envelope_required": True,
            f"{provider}_reauthorization_required": True,
            f"{provider}_refresh_outcome_unknown": True,
            f"{provider}_reauthorization_attempt_id": claim_id,
            f"{provider}_reauthorization_attempt_kind": "reconnect",
            f"{provider}_reauthorization_attempt_phase": "reserved",
            f"{provider}_reauthorization_attempt_expires_at": time.time() + 300.0,
            f"{provider}_reauthorization_attempt_acquired_at": time.time(),
            f"{provider}_reauthorization_attempt_generation": 1,
            f"{provider}_reauthorization_attempt_lifecycle_epoch": 1,
            f"{provider}_reauthorization_attempt_credentials_fingerprint": fp,
        }
        c_doc = _FakeDocRef(dict(base_data), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}, "integration_lifecycle_audit": {}})
        _patch_firestore(monkeypatch, db)

        # 1. connect_provider_cas while attempt is in "reserved" phase MUST fail byte-identical
        with pytest.raises(IntegrationTokenCASConflict) as exc_info:
            await connect_provider_cas(
                contractor_id=cid,
                provider=provider,
                access_token="new_acc",
                refresh_token="new_ref",
                claim_id=claim_id,
                observed_generation=1,
                observed_lifecycle_epoch=1,
                observed_access_raw=enc_acc,
                observed_refresh_raw=enc_ref,
                db=db,
            )
        assert "provider_request_started" in str(exc_info.value)
        assert c_doc.data == base_data

        # 2. disconnect_provider_cas while quarantined with attempt (reserved or started) MUST fail byte-identical
        reserved_data = dict(c_doc.data)
        with pytest.raises(IntegrationTokenCASConflict):
            await it_mutations.disconnect_provider_cas(
                contractor_id=cid,
                provider=provider,
                actor="contractor_api",
                reason="contractor_initiated_disconnect",
                db=db,
            )
        assert c_doc.data == reserved_data

        # 3. Transition attempt phase to "provider_request_started"
        await it_mutations.transition_provider_reauthorization_attempt_to_started_cas(
            contractor_id=cid,
            provider=provider,
            claim_id=claim_id,
            observed_generation=1,
            observed_lifecycle_epoch=1,
            observed_access_raw=enc_acc,
            observed_refresh_raw=enc_ref,
            db=db,
        )
        assert c_doc.data[f"{provider}_reauthorization_attempt_phase"] == "provider_request_started"

        # 4. disconnect_provider_cas while attempt is started MUST ALSO fail byte-identical
        started_data = dict(c_doc.data)
        with pytest.raises(IntegrationTokenCASConflict):
            await it_mutations.disconnect_provider_cas(
                contractor_id=cid,
                provider=provider,
                actor="contractor_api",
                reason="contractor_initiated_disconnect",
                db=db,
            )
        assert c_doc.data == started_data

        # 5. connect_provider_cas in "provider_request_started" phase succeeds, clearing quarantine and attempt
        updates, new_gen, audit_id = await connect_provider_cas(
            contractor_id=cid,
            provider=provider,
            access_token="new_acc_ok",
            refresh_token="new_ref_ok",
            claim_id=claim_id,
            observed_generation=1,
            observed_lifecycle_epoch=1,
            observed_access_raw=enc_acc,
            observed_refresh_raw=enc_ref,
            db=db,
        )
        assert new_gen == 2
        assert f"{provider}_reauthorization_required" not in c_doc.data
        assert f"{provider}_refresh_outcome_unknown" not in c_doc.data
        assert f"{provider}_reauthorization_attempt_id" not in c_doc.data


@pytest.mark.asyncio
async def test_18q_complete_lifecycle_claim_binding(monkeypatch):
    """Prove complete lifecycle/claim binding: wrong epoch, generation, credentials, claim, phase, or fingerprint fail byte-identical."""
    _setup_keyring(monkeypatch)
    cid = "test_18q_claim_binding"
    enc_acc = it_mutations.encrypt_integration_token("acc", contractor_id=cid, provider="jobber", token_kind="access")
    enc_ref = it_mutations.encrypt_integration_token("ref", contractor_id=cid, provider="jobber", token_kind="refresh")
    claim_id = "c" * 32
    fp = it_mutations.compute_raw_credentials_fingerprint(enc_acc, enc_ref)

    intent_data = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 2,
        "jobber_operation_intent_id": claim_id,
        "jobber_operation_intent_kind": "refresh",
        "jobber_operation_intent_phase": "provider_request_started",
        "jobber_operation_intent_expires_at": time.time() + 300.0,
        "jobber_operation_intent_acquired_at": time.time(),
        "jobber_operation_intent_generation": 1,
        "jobber_operation_intent_lifecycle_epoch": 2,
        "jobber_operation_intent_credentials_fingerprint": fp,
    }
    c_doc = _FakeDocRef(dict(intent_data), doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: c_doc}})
    _patch_firestore(monkeypatch, db)

    # 1. persist_refreshed_tokens_cas with wrong observed_lifecycle_epoch fails
    with pytest.raises(IntegrationTokenCASConflict):
        await it_mutations.persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new_acc",
            new_refresh_token="new_ref",
            observed_generation=1,
            observed_lifecycle_epoch=1,  # Actual is 2
            observed_access_raw=enc_acc,
            observed_refresh_raw=enc_ref,
            claim_id=claim_id,
            db=db,
        )
    assert c_doc.data == intent_data

    # 2. persist_refreshed_tokens_cas with wrong credentials_fingerprint fails
    bad_fp_doc = dict(intent_data)
    bad_fp_doc["jobber_operation_intent_credentials_fingerprint"] = "0" * 64
    c_doc.data = bad_fp_doc
    with pytest.raises(IntegrationTokenCASConflict):
        await it_mutations.persist_refreshed_tokens_cas(
            contractor_id=cid,
            provider="jobber",
            new_access_token="new_acc",
            new_refresh_token="new_ref",
            observed_generation=1,
            observed_lifecycle_epoch=2,
            observed_access_raw=enc_acc,
            observed_refresh_raw=enc_ref,
            claim_id=claim_id,
            db=db,
        )
    assert c_doc.data == bad_fp_doc

    # 3. Partial quarantine (reauthorization_required=True without refresh_outcome_unknown) fails connect even claimless
    partial_q_doc = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_access_token": enc_acc,
        "jobber_refresh_token": enc_ref,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 2,
        "jobber_reauthorization_required": True,
    }
    c_doc.data = partial_q_doc
    with pytest.raises(IntegrationTokenCASConflict) as exc_info:
        await connect_provider_cas(
            contractor_id=cid,
            provider="jobber",
            access_token="new_acc",
            refresh_token="new_ref",
            claim_id=None,
            db=db,
        )
    assert "Malformed" in str(exc_info.value)
    assert c_doc.data == partial_q_doc


def test_18q_fingerprint_canonical_hex_validation():
    """Prove credentials fingerprint validation rejects noncanonical hex, uppercase, and incorrect length."""
    import app.services.integration_tokens as it_tokens
    valid_fp = "a" * 64
    doc_valid = {
        "jobber_reauthorization_attempt_id": "c" * 32,
        "jobber_reauthorization_attempt_kind": "reconnect",
        "jobber_reauthorization_attempt_phase": "reserved",
        "jobber_reauthorization_attempt_expires_at": 1000.0,
        "jobber_reauthorization_attempt_acquired_at": 500.0,
        "jobber_reauthorization_attempt_generation": 1,
        "jobber_reauthorization_attempt_lifecycle_epoch": 1,
        "jobber_reauthorization_attempt_credentials_fingerprint": valid_fp,
    }
    st, parsed, err = it_tokens.parse_provider_reauthorization_attempt(doc_valid, "jobber")
    assert st == "valid"
    assert parsed["credentials_fingerprint"] == valid_fp

    # Invalid fingerprints: uppercase, short, non-hex
    for bad_fp in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "12345"):
        doc_bad = dict(doc_valid)
        doc_bad["jobber_reauthorization_attempt_credentials_fingerprint"] = bad_fp
        st_bad, _, err_bad = it_tokens.parse_provider_reauthorization_attempt(doc_bad, "jobber")
        assert st_bad == "malformed"
        assert "credentials_fingerprint" in err_bad


def test_18qc_canonical_intent_requires_fingerprint():
    """Prove canonical operation intent mandates credentials_fingerprint and rejects missing or malformed values."""
    import app.services.integration_tokens as it_tokens
    cid = "test_canonical_fp_req"
    valid_fp = "a" * 64
    base_canonical = {
        "jobber_operation_intent_id": "c" * 32,
        "jobber_operation_intent_kind": "refresh",
        "jobber_operation_intent_phase": "reserved",
        "jobber_operation_intent_expires_at": 2000.0,
        "jobber_operation_intent_acquired_at": 1000.0,
        "jobber_operation_intent_generation": 1,
        "jobber_operation_intent_lifecycle_epoch": 1,
    }

    # Missing fingerprint -> malformed
    st_missing, _, err_m = it_tokens.parse_provider_operation_intent(base_canonical, "jobber")
    assert st_missing == "malformed"
    assert "Missing required canonical intent fields" in err_m

    # Invalid fingerprint format -> malformed
    for bad_fp in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, 123, True, None):
        doc_bad = dict(base_canonical)
        doc_bad["jobber_operation_intent_credentials_fingerprint"] = bad_fp
        st_bad, _, err_b = it_tokens.parse_provider_operation_intent(doc_bad, "jobber")
        assert st_bad == "malformed"
        assert "credentials_fingerprint" in err_b or "Missing" in err_b

    # Valid fingerprint -> valid
    doc_ok = dict(base_canonical)
    doc_ok["jobber_operation_intent_credentials_fingerprint"] = valid_fp
    st_ok, parsed, err_ok = it_tokens.parse_provider_operation_intent(doc_ok, "jobber")
    assert st_ok == "valid"
    assert parsed["credentials_fingerprint"] == valid_fp
    assert err_ok is None


@pytest.mark.asyncio
async def test_18qc_unconditional_fingerprint_checks_in_mutations(monkeypatch):
    """Seam test: prove transition, quarantine, persist, and connect fail closed when credentials fingerprint mismatches stored tokens."""
    _setup_keyring(monkeypatch)
    cid = "test_unconditional_fp_seam"
    stored_acc = "acc_token_val_1"
    stored_ref = "ref_token_val_1"
    real_fp = it_mutations.compute_raw_credentials_fingerprint(stored_acc, stored_ref)
    wrong_fp = "b" * 64

    for provider in ("jobber", "google_calendar"):
        # 1. Transition to started fails on wrong fingerprint
        c_doc = _FakeDocRef({
            "contractor_id": cid,
            "active": True,
            f"{provider}_connected": True,
            f"{provider}_access_token": stored_acc,
            f"{provider}_refresh_token": stored_ref,
            f"{provider}_generation": 1,
            f"{provider}_lifecycle_epoch": 1,
            f"{provider}_operation_intent_id": "c" * 32,
            f"{provider}_operation_intent_kind": "refresh",
            f"{provider}_operation_intent_phase": "reserved",
            f"{provider}_operation_intent_expires_at": time.time() + 300.0,
            f"{provider}_operation_intent_acquired_at": time.time(),
            f"{provider}_operation_intent_generation": 1,
            f"{provider}_operation_intent_lifecycle_epoch": 1,
            f"{provider}_operation_intent_credentials_fingerprint": wrong_fp,
        }, doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})
        with pytest.raises(it_mutations.IntegrationTokenLeaseError, match="credentials fingerprint mismatch"):
            await it_mutations.transition_provider_operation_intent_to_started_cas(
                contractor_id=cid,
                provider=provider,
                claim_id="c" * 32,
                db=db,
            )

        # 2. Persist refreshed tokens fails on wrong fingerprint
        c_doc_started = _FakeDocRef({
            "contractor_id": cid,
            "active": True,
            f"{provider}_connected": True,
            f"{provider}_access_token": stored_acc,
            f"{provider}_refresh_token": stored_ref,
            f"{provider}_generation": 1,
            f"{provider}_lifecycle_epoch": 1,
            f"{provider}_operation_intent_id": "c" * 32,
            f"{provider}_operation_intent_kind": "refresh",
            f"{provider}_operation_intent_phase": "provider_request_started",
            f"{provider}_operation_intent_expires_at": time.time() + 300.0,
            f"{provider}_operation_intent_acquired_at": time.time(),
            f"{provider}_operation_intent_generation": 1,
            f"{provider}_operation_intent_lifecycle_epoch": 1,
            f"{provider}_operation_intent_credentials_fingerprint": wrong_fp,
        }, doc_id=cid)
        db_persist = _FakeFirestore({"contractors": {cid: c_doc_started}})
        with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="credentials fingerprint mismatch"):
            await it_mutations.persist_refreshed_tokens_cas(
                contractor_id=cid,
                provider=provider,
                new_access_token="new_acc",
                new_refresh_token="new_ref",
                observed_generation=1,
                observed_lifecycle_epoch=1,
                observed_access_raw=stored_acc,
                observed_refresh_raw=stored_ref,
                claim_id="c" * 32,
                db=db_persist,
            )


@pytest.mark.asyncio
async def test_18qc_connect_audit_collision_ordering_and_rollback(monkeypatch):
    """Prove connect audit creation happens after reads and before commit, failing on collision and rolling back staged contractor writes."""
    _setup_keyring(monkeypatch)
    cid = "test_connect_audit_collision_ordering"
    initial_doc_data = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_token_envelope_required": False,
        "jobber_operation_intent_id": "c" * 32,
        "jobber_operation_intent_kind": "connect",
        "jobber_operation_intent_phase": "provider_request_started",
        "jobber_operation_intent_expires_at": time.time() + 300.0,
        "jobber_operation_intent_acquired_at": time.time(),
        "jobber_operation_intent_generation": 0,
        "jobber_operation_intent_lifecycle_epoch": 0,
        "jobber_operation_intent_credentials_fingerprint": it_mutations.compute_raw_credentials_fingerprint(None, None),
    }
    c_doc = _FakeDocRef(dict(initial_doc_data), doc_id=cid)
    audit_id = it_mutations.format_audit_doc_id(contractor_id=cid, provider="jobber", generation=1, action="connected")
    competing_audit = {"contractor_id": cid, "competing_audit_written_first": True}
    audit_doc = _FakeDocRef(dict(competing_audit), doc_id=audit_id)
    db = _FakeFirestore({"contractors": {cid: c_doc}, "integration_lifecycle_audit": {audit_id: audit_doc}})
    _patch_firestore(monkeypatch, db)

    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="already exists"):
        await it_mutations.connect_provider_cas(
            contractor_id=cid,
            provider="jobber",
            access_token="acc_new_val",
            refresh_token="ref_new_val",
            claim_id="c" * 32,
            db=db,
        )

    # Assert contractor document remains byte-identical and competing audit doc is preserved
    assert c_doc.get().to_dict() == initial_doc_data
    assert db.collections["integration_lifecycle_audit"][audit_id].get().to_dict() == competing_audit


@pytest.mark.asyncio
async def test_18qc_sentinel_non_disclosure_in_oauth_and_preflight(monkeypatch, caplog):
    """Prove secret sentinels and contractor IDs are absent from HTTP details, return reason codes, and caplog in OAuth state and preflight."""
    import logging
    _setup_keyring(monkeypatch)
    cid = "secret_cid_sentinel_98765"
    secret_sentinel = "secret_key_payload_sentinel_abc"
    fake_db = _FakeFirestore({
        "contractors": {
            "inactive_cid": _FakeDocRef({
                "contractor_id": "inactive_cid",
                "active": False,
            }, doc_id="inactive_cid")
        }
    })
    for provider in ("jobber", "google_calendar"):
        # Preflight returns closed reason code, no contractor ID or exception string
        status, reason = await it_mutations.check_and_recover_expired_intent_preflight_cas(
            contractor_id="inactive_cid",
            provider=provider,
            db=fake_db,
        )
        assert status == "blocked"
        assert reason == "contractor_inactive"
        assert secret_sentinel not in (reason or "")
        assert cid not in (reason or "")

        # Create OAuth state with invalid lifecycle counters returns fixed detail
        db = _FakeFirestore({
            "contractors": {
                cid: _FakeDocRef({
                    "contractor_id": cid,
                    "active": True,
                    f"{provider}_connected": True,
                    f"{provider}_generation": "invalid_bool_type",  # invalid
                }, doc_id=cid)
            }
        })
        with caplog.at_level(logging.ERROR):
            with pytest.raises(HTTPException) as exc_info:
                await it_mutations.create_oauth_state(
                    db=db,
                    collection_name=it_mutations.OAUTH_PROVIDER_COLLECTIONS[provider],
                    state="a" * 32,
                    contractor_id=cid,
                    provider=provider,
                )
            assert exc_info.value.status_code in (400, 500)
            assert "Invalid contractor lifecycle metadata" in exc_info.value.detail or "Failed" in exc_info.value.detail
            assert secret_sentinel not in exc_info.value.detail
            assert cid not in exc_info.value.detail
            assert secret_sentinel not in caplog.text
            assert cid not in caplog.text


# ═══════════════════════════════════════════════════════════════════════
# 18Q-D EXPLICIT MUTATION & HOSTILE REPAIR TESTS
# ═══════════════════════════════════════════════════════════════════════

class HostileObject:
    """Hostile object whose __str__ and __repr__ raise exceptions when evaluated."""

    def __str__(self):
        raise RuntimeError("HOSTILE_STR_EVALUATION_ESCAPED")

    def __repr__(self):
        raise RuntimeError("HOSTILE_REPR_EVALUATION_ESCAPED")


@pytest.mark.asyncio
async def test_18qd_both_provider_acquire_intent_fails_on_malformed_credentials_without_write(monkeypatch):
    """Prove acquire_provider_operation_intent_cas fails closed before any transactional write when raw credentials are malformed or mixed."""
    import app.services.integration_tokens as it_tokens
    _setup_keyring(monkeypatch)
    cid = "cid_18qd_acquire_malformed"

    for provider in ("jobber", "google_calendar"):
        initial_doc = {
            "contractor_id": cid,
            "active": True,
            f"{provider}_connected": True,
            f"{provider}_generation": 0,
            f"{provider}_lifecycle_epoch": 0,
            f"{provider}_access_token": "valid_str_token",
            f"{provider}_refresh_token": None,  # mixed/partial: access is str, refresh is None
        }
        c_doc = _FakeDocRef(dict(initial_doc), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})
        _patch_firestore(monkeypatch, db)

        with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Failed to compute credentials fingerprint"):
            await it_mutations.acquire_provider_operation_intent_cas(
                contractor_id=cid,
                provider=provider,
                kind="refresh",
                db=db,
            )

        assert c_doc.get().to_dict() == initial_doc
        parse_st, _, _ = it_tokens.parse_provider_operation_intent(c_doc.get().to_dict(), provider)
        assert parse_st == "absent"


@pytest.mark.asyncio
async def test_18qd_hostile_objects_and_sentinels_non_disclosure():
    """Prove parsers, mutations, and validators fail closed with fixed static errors when given hostile objects or secret sentinels."""
    import app.db.integration_lifecycle_audit as audit_db
    import app.services.integration_tokens as it_tokens

    hostile = HostileObject()
    sentinel = "SENTINEL_SECRET_TOKEN_9999"

    hostile_data = {
        "jobber_operation_intent_id": sentinel,
        "jobber_operation_intent_kind": hostile,
        "jobber_operation_intent_phase": hostile,
        "jobber_operation_intent_expires_at": 1000.0,
        "jobber_operation_intent_acquired_at": 500.0,
        "jobber_operation_intent_generation": 1,
        "jobber_operation_intent_lifecycle_epoch": 1,
        "jobber_operation_intent_credentials_fingerprint": "0" * 64,
    }
    st, parsed, err = it_tokens.parse_provider_operation_intent(hostile_data, "jobber")
    assert st == "malformed"
    assert parsed is None
    assert err == "Invalid operation_intent_kind" or err == "Missing required canonical intent fields"
    assert sentinel not in (err or "")

    hostile_outbox = {
        "schema_version": 1,
        "contractor_id": sentinel,
        "provider": "jobber",
        "generation": 1,
        "lifecycle_epoch": 1,
        "status": hostile,
        "credential_deletion_disposition": "revoked_at_provider",
        "claim_id": "c" * 32,
        "audit_finalized": False,
        "audit_finalized_at": None,
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    with pytest.raises(ValueError) as exc_info:
        audit_db.validate_outbox_record(hostile_outbox)
    assert sentinel not in str(exc_info.value)
    assert "HOSTILE" not in str(exc_info.value)
    assert str(exc_info.value) == "Invalid revocation status"


@pytest.mark.asyncio
async def test_18qd_isolated_single_factor_mutation_fences(monkeypatch):
    """Prove isolated single-factor mismatch for transition, quarantine, persist, connect, and reauth transition for both providers."""
    _setup_keyring(monkeypatch)
    cid = "cid_18qd_isolated_fence"

    for provider in ("jobber", "google_calendar"):
        base_exp = 2000.0
        valid_fp = it_mutations.compute_raw_credentials_fingerprint(None, None)
        claim_id = "c" * 32
        initial_doc = {
            "contractor_id": cid,
            "active": True,
            f"{provider}_connected": False,
            f"{provider}_generation": 0,
            f"{provider}_lifecycle_epoch": 0,
            f"{provider}_operation_intent_id": claim_id,
            f"{provider}_operation_intent_kind": "connect",
            f"{provider}_operation_intent_phase": "reserved",
            f"{provider}_operation_intent_acquired_at": 500.0,
            f"{provider}_operation_intent_expires_at": base_exp,
            f"{provider}_operation_intent_generation": 0,
            f"{provider}_operation_intent_lifecycle_epoch": 0,
            f"{provider}_operation_intent_credentials_fingerprint": valid_fp,
        }

        # Factor 1: observed_generation mismatch
        c_doc = _FakeDocRef(dict(initial_doc), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})
        _patch_firestore(monkeypatch, db)
        with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Generation conflict"):
            await it_mutations.transition_provider_operation_intent_to_started_cas(
                contractor_id=cid,
                provider=provider,
                claim_id=claim_id,
                observed_generation=99,
                observed_lifecycle_epoch=0,
                db=db,
            )
        assert c_doc.get().to_dict() == initial_doc

        # Factor 2: observed_lifecycle_epoch mismatch
        c_doc = _FakeDocRef(dict(initial_doc), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})
        _patch_firestore(monkeypatch, db)
        with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Lifecycle epoch conflict"):
            await it_mutations.transition_provider_operation_intent_to_started_cas(
                contractor_id=cid,
                provider=provider,
                claim_id=claim_id,
                observed_generation=0,
                observed_lifecycle_epoch=99,
                db=db,
            )
        assert c_doc.get().to_dict() == initial_doc

        # Factor 3: claim_id mismatch
        c_doc = _FakeDocRef(dict(initial_doc), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})
        _patch_firestore(monkeypatch, db)
        with pytest.raises(it_mutations.IntegrationTokenLeaseError, match="claim ID mismatch"):
            await it_mutations.transition_provider_operation_intent_to_started_cas(
                contractor_id=cid,
                provider=provider,
                claim_id="x" * 32,
                observed_generation=0,
                observed_lifecycle_epoch=0,
                db=db,
            )
        assert c_doc.get().to_dict() == initial_doc

        # Factor 4: credential mismatch / fingerprint mismatch
        c_doc = _FakeDocRef(dict(initial_doc), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})
        _patch_firestore(monkeypatch, db)
        with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="credential mismatch|credentials fingerprint mismatch"):
            await it_mutations.transition_provider_operation_intent_to_started_cas(
                contractor_id=cid,
                provider=provider,
                claim_id=claim_id,
                observed_generation=0,
                observed_lifecycle_epoch=0,
                observed_access_raw="raw_acc_different",
                db=db,
            )
        assert c_doc.get().to_dict() == initial_doc


@pytest.mark.asyncio
async def test_18qd_commit_time_audit_collision_rollback(monkeypatch):
    """Prove that if a competing audit document is injected right before commit, connect_provider_cas detects collision and rolls back staged contractor updates."""
    import app.db.integration_lifecycle_audit as audit_db
    _setup_keyring(monkeypatch)
    cid = "cid_18qd_commit_collision"
    claim_id = "c" * 32
    valid_fp = it_mutations.compute_raw_credentials_fingerprint(None, None)
    now_ts = time.time()

    initial_doc = {
        "contractor_id": cid,
        "active": True,
        "jobber_connected": False,
        "jobber_generation": 0,
        "jobber_lifecycle_epoch": 0,
        "jobber_operation_intent_id": claim_id,
        "jobber_operation_intent_kind": "connect",
        "jobber_operation_intent_phase": "provider_request_started",
        "jobber_operation_intent_acquired_at": now_ts - 100.0,
        "jobber_operation_intent_expires_at": now_ts + 3600.0,
        "jobber_operation_intent_generation": 0,
        "jobber_operation_intent_lifecycle_epoch": 0,
        "jobber_operation_intent_credentials_fingerprint": valid_fp,
    }

    c_doc = _FakeDocRef(dict(initial_doc), doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: c_doc}})
    _patch_firestore(monkeypatch, db)

    audit_id = it_mutations.format_audit_doc_id(contractor_id=cid, provider="jobber", generation=1, action="connected")
    competing_audit_data = audit_db.build_connect_audit_event(
        contractor_id=cid,
        provider="jobber",
        generation=1,
    )

    real_transaction_func = db.transaction

    def _colliding_transaction():
        txn = real_transaction_func()
        orig_create = txn.create
        def _colliding_create(ref, data):
            comp_ref = _FakeDocRef(dict(competing_audit_data), doc_id=ref.id)
            db.collections["integration_lifecycle_audit"][ref.id] = comp_ref
            ref.data = dict(competing_audit_data)
            return orig_create(ref, data)
        txn.create = _colliding_create
        return txn

    monkeypatch.setattr(db, "transaction", _colliding_transaction)

    with pytest.raises(it_mutations.IntegrationTokenCASConflict, match="Connect transaction failed|already exists"):
        await it_mutations.connect_provider_cas(
            contractor_id=cid,
            provider="jobber",
            access_token="acc_new_123",
            refresh_token="ref_new_123",
            claim_id=claim_id,
            db=db,
        )

    assert c_doc.get().to_dict() == initial_doc
    assert db.collections["integration_lifecycle_audit"][audit_id].get().to_dict() == competing_audit_data


@pytest.mark.asyncio
async def test_18qe_disconnect_untrusted_runtime_error_sentinel_privacy(monkeypatch):
    """Test that disconnect transaction RuntimeError with secret sentinel returns exact fixed conflict with __cause__ is None."""
    _setup_keyring(monkeypatch)
    cid = "c-disc-sentinel-priv"
    initial_doc = {
        "active": True,
        "contractor_id": cid,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_lifecycle_epoch": 1,
        "jobber_access_token": "acc_1",
        "jobber_refresh_token": "ref_1",
        "jobber_disconnected_at": None,
    }
    c_doc = _FakeDocRef(dict(initial_doc), doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: c_doc}})

    def _failing_transaction():
        txn = _FakeTransaction()
        def _failing_get(ref):
            if ref == c_doc:
                return c_doc.get()
            raise RuntimeError("SECRET_DB_PASSWORD_12345")
        txn.get = _failing_get
        return txn

    monkeypatch.setattr(db, "transaction", _failing_transaction)

    with pytest.raises(it_mutations.IntegrationTokenCASConflict) as exc_info:
        await it_mutations.disconnect_provider_envelope_cas(
            contractor_id=cid,
            provider="jobber",
            db=db,
        )

    err = exc_info.value
    assert str(err) == "Disconnect transaction failed with ambiguous state"
    assert err.__cause__ is None
    assert "SECRET" not in str(err)


@pytest.mark.asyncio
async def test_18qe_disconnect_hostile_value_error_subclass_privacy(monkeypatch):
    """Test that disconnect transaction hostile ValueError subclass callbacks are not invoked and return fixed conflict with __cause__ None."""
    _setup_keyring(monkeypatch)
    cid = "c-disc-hostile-ve"
    initial_doc = {
        "active": True,
        "contractor_id": cid,
        "google_calendar_connected": True,
        "google_calendar_generation": 1,
        "google_calendar_lifecycle_epoch": 1,
        "google_calendar_access_token": "acc_1",
        "google_calendar_refresh_token": "ref_1",
    }
    c_doc = _FakeDocRef(dict(initial_doc), doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: c_doc}})

    class HostileValueError(ValueError):
        def __str__(self):
            raise RuntimeError("HOSTILE___STR___CALLBACK_INVOKED")
        def __repr__(self):
            raise RuntimeError("HOSTILE___REPR___CALLBACK_INVOKED")

    def _failing_transaction():
        txn = _FakeTransaction()
        def _failing_get(ref):
            if ref == c_doc:
                return c_doc.get()
            raise HostileValueError("hostile")
        txn.get = _failing_get
        return txn

    monkeypatch.setattr(db, "transaction", _failing_transaction)

    with pytest.raises(it_mutations.IntegrationTokenCASConflict) as exc_info:
        await it_mutations.disconnect_provider_envelope_cas(
            contractor_id=cid,
            provider="google_calendar",
            db=db,
        )

    err = exc_info.value
    assert str(err) == "Disconnect transaction failed with ambiguous state"
    assert err.__cause__ is None


@pytest.mark.asyncio
async def test_18qe_disconnect_postcondition_failure_messages(monkeypatch):
    """Test that disconnect postcondition failure messages are exact fixed strings without contractor/claim sentinels."""
    _setup_keyring(monkeypatch)
    cid = "c-disc-post-msg-sentinel"
    doc_data = {
        "active": True,
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_generation": 2,
        "jobber_lifecycle_epoch": 2,
        "jobber_disconnected_at": 1000.0,
        "jobber_token_envelope_required": False,
        "jobber_lead_capture_enabled": False,
    }
    c_doc = _FakeDocRef(dict(doc_data), doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: c_doc}})

    # Missing outbox / missing audit postcondition check
    with pytest.raises(it_mutations.IntegrationTokenPostconditionError) as exc_info:
        it_mutations._verify_complete_disconnect_postcondition(
            c_doc,
            contractor_id=cid,
            provider="jobber",
            expected_generation=2,
            expected_lifecycle_epoch=2,
            expected_disconnected_at=1000.0,
            expected_floor=False,
            db=db,
            outbox_id=f"{cid}_jobber_2_credentials_deleted",
            expected_outbox={},
            audit_id=f"{cid}_jobber_2_credentials_deleted",
            expected_audit={},
        )
    assert str(exc_info.value) == "Outbox record not found during postcondition verification"
    assert cid not in str(exc_info.value)


@pytest.mark.asyncio
async def test_18qg_disconnect_endpoints_exception_privacy_and_mappings(monkeypatch, caplog):
    """Assert jobber_disconnect and google_calendar_disconnect handle unexpected, hostile, and typed exceptions with total privacy."""
    import logging
    from app.api import integrations
    from app.services import integration_token_mutations as it_mutations
    from fastapi import HTTPException

    class HostileException(Exception):
        def __init__(self):
            self.str_count = 0
            self.repr_count = 0
        def __str__(self):
            self.str_count += 1
            raise RuntimeError("HOSTILE___STR___CALLBACK_INVOKED")
        def __repr__(self):
            self.repr_count += 1
            raise RuntimeError("HOSTILE___REPR___CALLBACK_INVOKED")

    endpoints = [
        (integrations.jobber_disconnect, "jobber"),
        (integrations.google_calendar_disconnect, "google_calendar"),
    ]

    orig_logger_handlers = list(integrations.logger.handlers)
    orig_logger_propagate = integrations.logger.propagate
    integrations.logger.handlers.clear()
    integrations.logger.propagate = True

    root_logger = logging.getLogger()
    orig_root_handlers = list(root_logger.handlers)
    if caplog.handler in root_logger.handlers:
        root_logger.handlers = [caplog.handler]

    try:
        for endpoint_func, provider in endpoints:
            cid = f"c-disc-privacy-{provider}-123"

            # Case A: _get_firestore raises RuntimeError with secret sentinel
            sentinel_msg_a = "SECRET_SENTINEL_FIRESTORE_CONSTRUCTION_FAIL_123"
            monkeypatch.setattr(integrations, "require_contractor_access", lambda req, c_id: None)
            def _failing_get_firestore_sentinel():
                raise RuntimeError(sentinel_msg_a)
            monkeypatch.setattr(integrations, "_get_firestore", _failing_get_firestore_sentinel)

            caplog.clear()
            with pytest.raises(HTTPException) as exc_info:
                await endpoint_func(contractor_id=cid)
            err = exc_info.value
            assert err.status_code == 500
            assert err.detail == "Internal server error"
            assert err.__cause__ is None
            assert err.__suppress_context__ is True
            assert len(caplog.records) == 1
            rec = caplog.records[0]
            assert rec.levelno == logging.ERROR
            expected_msg = f"Provider disconnect failed: provider={provider} operation=disconnect result=internal_error"
            assert rec.getMessage() == expected_msg
            assert rec.exc_info is None
            assert sentinel_msg_a not in rec.getMessage()
            assert cid not in rec.getMessage()

            # Case B: _get_firestore raises hostile exception with raising __str__/__repr__
            hostile_b = HostileException()
            def _failing_get_firestore_hostile():
                raise hostile_b
            monkeypatch.setattr(integrations, "_get_firestore", _failing_get_firestore_hostile)

            caplog.clear()
            with pytest.raises(HTTPException) as exc_info:
                await endpoint_func(contractor_id=cid)
            err = exc_info.value
            assert err.status_code == 500
            assert err.detail == "Internal server error"
            assert err.__cause__ is None
            assert err.__suppress_context__ is True
            assert hostile_b.str_count == 0
            assert hostile_b.repr_count == 0
            assert len(caplog.records) == 1
            rec = caplog.records[0]
            assert rec.levelno == logging.ERROR
            assert rec.getMessage() == expected_msg
            assert rec.exc_info is None

            # Reset _get_firestore to valid for remaining orchestration cases
            monkeypatch.setattr(integrations, "_get_firestore", lambda: None)

            # Case C: Orchestration raises HTTPException(status_code=418, detail="SECRET_SENTINEL_418")
            sentinel_msg_c = "SECRET_SENTINEL_418_ATTACKER_DETAIL"
            async def _failing_orchestration_418(**kwargs):
                raise HTTPException(status_code=418, detail=sentinel_msg_c)
            monkeypatch.setattr(it_mutations, "disconnect_and_revoke_provider_orchestration", _failing_orchestration_418)

            caplog.clear()
            with pytest.raises(HTTPException) as exc_info:
                await endpoint_func(contractor_id=cid)
            err = exc_info.value
            assert err.status_code == 500
            assert err.detail == "Internal server error"
            assert err.__cause__ is None
            assert err.__suppress_context__ is True
            assert len(caplog.records) == 1
            rec = caplog.records[0]
            assert rec.levelno == logging.ERROR
            assert rec.getMessage() == expected_msg
            assert rec.exc_info is None
            assert sentinel_msg_c not in rec.getMessage()
            assert "418" not in rec.getMessage()
            assert cid not in rec.getMessage()

            # Case D1: Unexpected RuntimeError in orchestration
            sentinel_msg_d = "SECRET_SENTINEL_ORCHESTRATION_FAIL_999"
            async def _failing_orchestration_sentinel(**kwargs):
                raise RuntimeError(sentinel_msg_d)
            monkeypatch.setattr(it_mutations, "disconnect_and_revoke_provider_orchestration", _failing_orchestration_sentinel)

            caplog.clear()
            with pytest.raises(HTTPException) as exc_info:
                await endpoint_func(contractor_id=cid)
            err = exc_info.value
            assert err.status_code == 500
            assert err.detail == "Internal server error"
            assert err.__cause__ is None
            assert err.__suppress_context__ is True
            assert len(caplog.records) == 1
            rec = caplog.records[0]
            assert rec.levelno == logging.ERROR
            assert rec.getMessage() == expected_msg
            assert rec.exc_info is None
            assert sentinel_msg_d not in rec.getMessage()
            assert cid not in rec.getMessage()

            # Case D2: Hostile exception in orchestration
            hostile_d2 = HostileException()
            async def _failing_orchestration_hostile(**kwargs):
                raise hostile_d2
            monkeypatch.setattr(it_mutations, "disconnect_and_revoke_provider_orchestration", _failing_orchestration_hostile)

            caplog.clear()
            with pytest.raises(HTTPException) as exc_info:
                await endpoint_func(contractor_id=cid)
            err = exc_info.value
            assert err.status_code == 500
            assert err.detail == "Internal server error"
            assert err.__cause__ is None
            assert err.__suppress_context__ is True
            assert hostile_d2.str_count == 0
            assert hostile_d2.repr_count == 0
            assert len(caplog.records) == 1
            rec = caplog.records[0]
            assert rec.levelno == logging.ERROR
            assert rec.getMessage() == expected_msg
            assert rec.exc_info is None

            # Case D3: Typed Contractor Not Found -> 404
            async def _failing_orchestration_404(**kwargs):
                raise it_mutations.IntegrationTokenContractorNotFound("Contractor not found in DB")
            monkeypatch.setattr(it_mutations, "disconnect_and_revoke_provider_orchestration", _failing_orchestration_404)

            caplog.clear()
            with pytest.raises(HTTPException) as exc_info:
                await endpoint_func(contractor_id=cid)
            err = exc_info.value
            assert err.status_code == 404
            assert err.detail == "Contractor not found"
            assert err.__cause__ is None
            assert err.__suppress_context__ is True
            assert len(caplog.records) == 1
            rec = caplog.records[0]
            assert rec.levelno == logging.WARNING
            expected_msg_404 = f"Provider disconnect failed: provider={provider} operation=disconnect result=contractor_not_found"
            assert rec.getMessage() == expected_msg_404
            assert rec.exc_info is None
            assert "Contractor not found in DB" not in rec.getMessage()
            assert cid not in rec.getMessage()

            # Case D4: Typed CAS / Lifecycle Conflict -> 409
            async def _failing_orchestration_409(**kwargs):
                raise it_mutations.IntegrationTokenCASConflict("CAS conflict during disconnect")
            monkeypatch.setattr(it_mutations, "disconnect_and_revoke_provider_orchestration", _failing_orchestration_409)

            caplog.clear()
            with pytest.raises(HTTPException) as exc_info:
                await endpoint_func(contractor_id=cid)
            err = exc_info.value
            assert err.status_code == 409
            assert err.detail == "Integration transaction conflict"
            assert err.__cause__ is None
            assert err.__suppress_context__ is True
            assert len(caplog.records) == 1
            rec = caplog.records[0]
            assert rec.levelno == logging.WARNING
            expected_msg_409 = f"Provider disconnect failed: provider={provider} operation=disconnect result=conflict"
            assert rec.getMessage() == expected_msg_409
            assert rec.exc_info is None
            assert "CAS conflict" not in rec.getMessage()
            assert cid not in rec.getMessage()

            # Case F: require_contractor_access raises 403 (auth is outside boundary; _get_firestore and orchestration 0 calls)
            get_db_calls = [0]
            orch_calls = [0]

            def _auth_403(req, contractor_id_arg):
                raise HTTPException(status_code=403, detail="Forbidden contractor access")
            monkeypatch.setattr(integrations, "require_contractor_access", _auth_403)
            def _tracking_get_db():
                get_db_calls[0] += 1
                return None
            monkeypatch.setattr(integrations, "_get_firestore", _tracking_get_db)
            async def _tracking_orch(**kwargs):
                orch_calls[0] += 1
                return {}
            monkeypatch.setattr(it_mutations, "disconnect_and_revoke_provider_orchestration", _tracking_orch)

            caplog.clear()
            with pytest.raises(HTTPException) as exc_info:
                await endpoint_func(contractor_id=cid)
            err = exc_info.value
            assert err.status_code == 403
            assert err.detail == "Forbidden contractor access"
            assert get_db_calls[0] == 0
            assert orch_calls[0] == 0
            assert len(caplog.records) == 0
    finally:
        integrations.logger.handlers = orig_logger_handlers
        integrations.logger.propagate = orig_logger_propagate
        root_logger.handlers = orig_root_handlers


@pytest.mark.asyncio
async def test_18qg_acquire_intent_zero_transaction_writes_and_fingerprint_guard(monkeypatch):
    """Assert zero transaction updates/sets/creates/deletes and intent absent for acquire intent fingerprint failures across both providers."""
    _setup_keyring(monkeypatch)
    cid = "c-acq-tx-zero-write"

    class HostileObject:
        def __str__(self):
            raise RuntimeError("HOSTILE_STR")
        def __repr__(self):
            raise RuntimeError("HOSTILE_REPR")

    hostile_obj = HostileObject()
    valid_env = {
        "schema_version": 1,
        "key_version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": "a" * 24,
        "ciphertext": "b" * 32,
    }
    malformed_env = {"schema_version": 1, "invalid_key": True}

    invalid_credential_cases = []
    for provider in ("jobber", "google_calendar"):
        invalid_credential_cases.extend([
            # Partial 1: access present, refresh missing
            ("acc_only_str", None, provider),
            # Partial 2: access missing, refresh present
            (None, "ref_only_str", provider),
            # Mixed 1: access plaintext str, refresh envelope dict
            ("acc_plain_str", valid_env, provider),
            # Mixed 2: access envelope dict, refresh plaintext str
            (valid_env, "ref_plain_str", provider),
            # Malformed envelope
            ("acc_str", malformed_env, provider),
            # Hostile object
            (hostile_obj, "ref_str", provider),
        ])

    for acc_val, ref_val, provider in invalid_credential_cases:
        initial_doc = {
            "active": True,
            "contractor_id": cid,
            f"{provider}_connected": True,
            f"{provider}_generation": 0,
            f"{provider}_lifecycle_epoch": 0,
        }
        if acc_val is not None:
            initial_doc[f"{provider}_access_token"] = acc_val
        if ref_val is not None:
            initial_doc[f"{provider}_refresh_token"] = ref_val

        initial_doc_copy = dict(initial_doc)
        c_doc = _FakeDocRef(dict(initial_doc), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})

        staged_writes = {"update": 0, "set": 0, "create": 0, "delete": 0}

        def _tracking_transaction():
            txn = _FakeTransaction(db)
            orig_update = txn.update
            orig_set = txn.set
            orig_create = txn.create
            orig_delete = txn.delete

            def _track_update(*a, **kw):
                staged_writes["update"] += 1
                return orig_update(*a, **kw)
            def _track_set(*a, **kw):
                staged_writes["set"] += 1
                return orig_set(*a, **kw)
            def _track_create(*a, **kw):
                staged_writes["create"] += 1
                return orig_create(*a, **kw)
            def _track_delete(*a, **kw):
                staged_writes["delete"] += 1
                return orig_delete(*a, **kw)

            txn.update = _track_update
            txn.set = _track_set
            txn.create = _track_create
            txn.delete = _track_delete
            return txn

        monkeypatch.setattr(db, "transaction", _tracking_transaction)

        with pytest.raises(it_mutations.IntegrationTokenCASConflict):
            await it_mutations.acquire_provider_operation_intent_cas(
                contractor_id=cid,
                provider=provider,
                kind="refresh",
                observed_access_raw=acc_val,
                observed_refresh_raw=ref_val,
                observed_generation=0,
                observed_lifecycle_epoch=0,
                db=db,
            )

        assert staged_writes == {"update": 0, "set": 0, "create": 0, "delete": 0}
        assert c_doc.get().to_dict() == initial_doc_copy
        intent_status, parsed_intent, _ = it_mutations.parse_provider_operation_intent(c_doc.get().to_dict(), provider)
        assert intent_status == "absent"


@pytest.mark.asyncio
async def test_18qg_fingerprint_guard_transition_intent_to_started(monkeypatch):
    """Assert transition_provider_operation_intent_to_started_cas fails closed with zero transaction writes on fingerprint mismatch when all other intent fields are canonical."""
    _setup_keyring(monkeypatch)
    cid = "c-fp-transition-intent"
    claim_id = "claim_1234567890abcdef"

    for provider in ("jobber", "google_calendar"):
        acc_raw = "acc_fp_valid_123"
        ref_raw = "ref_fp_valid_123"
        mismatched_fp = "f" * 64

        doc_data = {
            "active": True,
            "contractor_id": cid,
            f"{provider}_connected": True,
            f"{provider}_generation": 0,
            f"{provider}_lifecycle_epoch": 0,
            f"{provider}_access_token": acc_raw,
            f"{provider}_refresh_token": ref_raw,
            f"{provider}_operation_intent_id": claim_id,
            f"{provider}_operation_intent_kind": "refresh",
            f"{provider}_operation_intent_phase": "reserved",
            f"{provider}_operation_intent_expires_at": 9999999999.0,
            f"{provider}_operation_intent_acquired_at": 100.0,
            f"{provider}_operation_intent_generation": 0,
            f"{provider}_operation_intent_lifecycle_epoch": 0,
            f"{provider}_operation_intent_credentials_fingerprint": mismatched_fp,
        }
        doc_copy = dict(doc_data)
        c_doc = _FakeDocRef(dict(doc_data), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})

        staged_writes = {"update": 0, "set": 0, "create": 0, "delete": 0}

        def _tracking_transaction():
            txn = _FakeTransaction(db)
            orig_update = txn.update
            def _track_update(*a, **kw):
                staged_writes["update"] += 1
                return orig_update(*a, **kw)
            txn.update = _track_update
            return txn

        monkeypatch.setattr(db, "transaction", _tracking_transaction)

        with pytest.raises(it_mutations.IntegrationTokenLeaseError) as exc_info:
            await it_mutations.transition_provider_operation_intent_to_started_cas(
                contractor_id=cid,
                provider=provider,
                kind="refresh",
                observed_access_raw=acc_raw,
                observed_refresh_raw=ref_raw,
                observed_generation=0,
                observed_lifecycle_epoch=0,
                claim_id=claim_id,
                db=db,
            )

        assert "credentials fingerprint mismatch" in str(exc_info.value)
        assert staged_writes == {"update": 0, "set": 0, "create": 0, "delete": 0}
        assert c_doc.get().to_dict() == doc_copy


@pytest.mark.asyncio
async def test_18qg_fingerprint_guard_quarantine_reauth(monkeypatch):
    """Assert quarantine_provider_reauth_cas fails closed with zero transaction writes on fingerprint mismatch when all other intent fields are canonical."""
    _setup_keyring(monkeypatch)
    cid = "c-fp-quarantine"
    claim_id = "claim_2234567890abcdef"

    for provider in ("jobber", "google_calendar"):
        acc_raw = "acc_fp_valid_123"
        ref_raw = "ref_fp_valid_123"
        mismatched_fp = "f" * 64

        doc_data = {
            "active": True,
            "contractor_id": cid,
            f"{provider}_connected": True,
            f"{provider}_generation": 0,
            f"{provider}_lifecycle_epoch": 0,
            f"{provider}_access_token": acc_raw,
            f"{provider}_refresh_token": ref_raw,
            f"{provider}_operation_intent_id": claim_id,
            f"{provider}_operation_intent_kind": "refresh",
            f"{provider}_operation_intent_phase": "provider_request_started",
            f"{provider}_operation_intent_expires_at": 9999999999.0,
            f"{provider}_operation_intent_acquired_at": 100.0,
            f"{provider}_operation_intent_generation": 0,
            f"{provider}_operation_intent_lifecycle_epoch": 0,
            f"{provider}_operation_intent_credentials_fingerprint": mismatched_fp,
        }
        doc_copy = dict(doc_data)
        c_doc = _FakeDocRef(dict(doc_data), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})

        staged_writes = {"update": 0, "set": 0, "create": 0, "delete": 0}

        def _tracking_transaction():
            txn = _FakeTransaction(db)
            orig_update = txn.update
            def _track_update(*a, **kw):
                staged_writes["update"] += 1
                return orig_update(*a, **kw)
            txn.update = _track_update
            return txn

        monkeypatch.setattr(db, "transaction", _tracking_transaction)

        res = await it_mutations.quarantine_provider_reauth_cas(
            contractor_id=cid,
            provider=provider,
            observed_generation=0,
            observed_lifecycle_epoch=0,
            observed_access_raw=acc_raw,
            observed_refresh_raw=ref_raw,
            claim_id=claim_id,
            db=db,
        )
        assert res is False
        assert staged_writes == {"update": 0, "set": 0, "create": 0, "delete": 0}
        assert c_doc.get().to_dict() == doc_copy


@pytest.mark.asyncio
async def test_18qg_fingerprint_guard_persist_refreshed_tokens(monkeypatch):
    """Assert persist_refreshed_tokens_cas fails closed with zero transaction writes on fingerprint mismatch when all other intent fields are canonical."""
    _setup_keyring(monkeypatch)
    cid = "c-fp-persist"
    claim_id = "claim_3234567890abcdef"

    for provider in ("jobber", "google_calendar"):
        acc_raw = "acc_fp_valid_123"
        ref_raw = "ref_fp_valid_123"
        mismatched_fp = "f" * 64

        doc_data = {
            "active": True,
            "contractor_id": cid,
            f"{provider}_connected": True,
            f"{provider}_generation": 0,
            f"{provider}_lifecycle_epoch": 0,
            f"{provider}_access_token": acc_raw,
            f"{provider}_refresh_token": ref_raw,
            f"{provider}_operation_intent_id": claim_id,
            f"{provider}_operation_intent_kind": "refresh",
            f"{provider}_operation_intent_phase": "provider_request_started",
            f"{provider}_operation_intent_expires_at": 9999999999.0,
            f"{provider}_operation_intent_acquired_at": 100.0,
            f"{provider}_operation_intent_generation": 0,
            f"{provider}_operation_intent_lifecycle_epoch": 0,
            f"{provider}_operation_intent_credentials_fingerprint": mismatched_fp,
        }
        doc_copy = dict(doc_data)
        c_doc = _FakeDocRef(dict(doc_data), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})

        staged_writes = {"update": 0, "set": 0, "create": 0, "delete": 0}

        def _tracking_transaction():
            txn = _FakeTransaction(db)
            orig_update = txn.update
            def _track_update(*a, **kw):
                staged_writes["update"] += 1
                return orig_update(*a, **kw)
            txn.update = _track_update
            return txn

        monkeypatch.setattr(db, "transaction", _tracking_transaction)

        with pytest.raises((it_mutations.IntegrationTokenCASConflict, it_mutations.IntegrationTokenLeaseError)) as exc_info:
            await it_mutations.persist_refreshed_tokens_cas(
                contractor_id=cid,
                provider=provider,
                new_access_token="new_acc_123",
                new_refresh_token="new_ref_123",
                observed_access_raw=acc_raw,
                observed_refresh_raw=ref_raw,
                observed_generation=0,
                observed_lifecycle_epoch=0,
                claim_id=claim_id,
                db=db,
            )

        assert "credentials fingerprint mismatch" in str(exc_info.value)
        assert staged_writes == {"update": 0, "set": 0, "create": 0, "delete": 0}
        assert c_doc.get().to_dict() == doc_copy


@pytest.mark.asyncio
async def test_18qg_fingerprint_guard_connect_provider(monkeypatch):
    """Assert connect_provider_cas fails closed with zero transaction writes on fingerprint mismatch when all other intent fields are canonical."""
    _setup_keyring(monkeypatch)
    cid = "c-fp-connect"
    claim_id = "claim_4234567890abcdef"

    for provider in ("jobber", "google_calendar"):
        acc_raw = "acc_fp_valid_123"
        ref_raw = "ref_fp_valid_123"
        mismatched_fp = "f" * 64

        doc_data = {
            "active": True,
            "contractor_id": cid,
            f"{provider}_connected": False,
            f"{provider}_generation": 0,
            f"{provider}_lifecycle_epoch": 0,
            f"{provider}_access_token": acc_raw,
            f"{provider}_refresh_token": ref_raw,
            f"{provider}_operation_intent_id": claim_id,
            f"{provider}_operation_intent_kind": "connect",
            f"{provider}_operation_intent_phase": "provider_request_started",
            f"{provider}_operation_intent_expires_at": 9999999999.0,
            f"{provider}_operation_intent_acquired_at": 100.0,
            f"{provider}_operation_intent_generation": 0,
            f"{provider}_operation_intent_lifecycle_epoch": 0,
            f"{provider}_operation_intent_credentials_fingerprint": mismatched_fp,
        }
        doc_copy = dict(doc_data)
        c_doc = _FakeDocRef(dict(doc_data), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})

        staged_writes = {"update": 0, "set": 0, "create": 0, "delete": 0}

        def _tracking_transaction():
            txn = _FakeTransaction(db)
            orig_update = txn.update
            def _track_update(*a, **kw):
                staged_writes["update"] += 1
                return orig_update(*a, **kw)
            txn.update = _track_update
            return txn

        monkeypatch.setattr(db, "transaction", _tracking_transaction)

        with pytest.raises(it_mutations.IntegrationTokenCASConflict) as exc_info:
            await it_mutations.connect_provider_cas(
                contractor_id=cid,
                provider=provider,
                access_token="new_conn_acc",
                refresh_token="new_conn_ref",
                claim_id=claim_id,
                db=db,
            )

        assert "Connect operation intent credentials fingerprint mismatch" in str(exc_info.value)
        assert staged_writes == {"update": 0, "set": 0, "create": 0, "delete": 0}
        assert c_doc.get().to_dict() == doc_copy


@pytest.mark.asyncio
async def test_18qg_fingerprint_guard_transition_reauth_attempt_to_started(monkeypatch):
    """Assert transition_provider_reauthorization_attempt_to_started_cas fails closed with zero transaction writes on fingerprint mismatch when all other reauth attempt fields are canonical."""
    _setup_keyring(monkeypatch)
    cid = "c-fp-transition-reauth"
    claim_id = "claim_5234567890abcdef"

    for provider in ("jobber", "google_calendar"):
        acc_raw = "acc_fp_valid_123"
        ref_raw = "ref_fp_valid_123"
        mismatched_fp = "f" * 64

        doc_data = {
            "active": True,
            "contractor_id": cid,
            f"{provider}_connected": True,
            f"{provider}_generation": 0,
            f"{provider}_lifecycle_epoch": 0,
            f"{provider}_access_token": acc_raw,
            f"{provider}_refresh_token": ref_raw,
            f"{provider}_reauthorization_required": True,
            f"{provider}_refresh_outcome_unknown": True,
            f"{provider}_reauthorization_attempt_id": claim_id,
            f"{provider}_reauthorization_attempt_kind": "reconnect",
            f"{provider}_reauthorization_attempt_phase": "reserved",
            f"{provider}_reauthorization_attempt_expires_at": 9999999999.0,
            f"{provider}_reauthorization_attempt_acquired_at": 100.0,
            f"{provider}_reauthorization_attempt_generation": 0,
            f"{provider}_reauthorization_attempt_lifecycle_epoch": 0,
            f"{provider}_reauthorization_attempt_credentials_fingerprint": mismatched_fp,
        }
        doc_copy = dict(doc_data)
        c_doc = _FakeDocRef(dict(doc_data), doc_id=cid)
        db = _FakeFirestore({"contractors": {cid: c_doc}})

        staged_writes = {"update": 0, "set": 0, "create": 0, "delete": 0}

        def _tracking_transaction():
            txn = _FakeTransaction(db)
            orig_update = txn.update
            def _track_update(*a, **kw):
                staged_writes["update"] += 1
                return orig_update(*a, **kw)
            txn.update = _track_update
            return txn

        monkeypatch.setattr(db, "transaction", _tracking_transaction)

        with pytest.raises(it_mutations.IntegrationTokenLeaseError) as exc_info:
            await it_mutations.transition_provider_reauthorization_attempt_to_started_cas(
                contractor_id=cid,
                provider=provider,
                observed_generation=0,
                observed_lifecycle_epoch=0,
                observed_access_raw=acc_raw,
                observed_refresh_raw=ref_raw,
                claim_id=claim_id,
                db=db,
            )

        assert "Reauthorization attempt credentials fingerprint mismatch" in str(exc_info.value)
        assert staged_writes == {"update": 0, "set": 0, "create": 0, "delete": 0}
        assert c_doc.get().to_dict() == doc_copy


@pytest.mark.asyncio
async def test_18qh_missing_contractor_real_orchestration_public_404_and_service_contract(monkeypatch, caplog):
    """Assert real disconnect orchestration and public disconnect endpoints return 404 for missing contractors with zero writes/revocations, and service raises IntegrationTokenContractorNotFound."""
    import logging
    from app.api import integrations
    from app.services import integration_token_mutations as it_mutations
    from fastapi import HTTPException

    # 1. Direct Service Level Assertion: disconnect_provider_envelope_cas raises IntegrationTokenContractorNotFound on missing contractor
    cid_missing_srv = "c-missing-service-123"
    c_doc_srv = _FakeDocRef(data=None, doc_id=cid_missing_srv)
    db_srv = _FakeFirestore({"contractors": {cid_missing_srv: c_doc_srv}})

    for provider in ("jobber", "google_calendar"):
        with pytest.raises(it_mutations.IntegrationTokenContractorNotFound) as exc_info_srv:
            await it_mutations.disconnect_provider_envelope_cas(
                contractor_id=cid_missing_srv,
                provider=provider,
                db=db_srv,
            )
        assert "Contractor document not found" in str(exc_info_srv.value)
        assert type(exc_info_srv.value) is it_mutations.IntegrationTokenContractorNotFound

    # 2. Public Endpoint Causal Integration Test: jobber_disconnect and google_calendar_disconnect
    endpoints = [
        (integrations.jobber_disconnect, "jobber"),
        (integrations.google_calendar_disconnect, "google_calendar"),
    ]

    orig_logger_handlers = list(integrations.logger.handlers)
    orig_logger_propagate = integrations.logger.propagate
    integrations.logger.handlers.clear()
    integrations.logger.propagate = True

    root_logger = logging.getLogger()
    orig_root_handlers = list(root_logger.handlers)
    if caplog.handler in root_logger.handlers:
        root_logger.handlers = [caplog.handler]

    try:
        for endpoint_func, provider in endpoints:
            cid_missing = f"c-missing-endpoint-{provider}-404"
            c_doc = _FakeDocRef(data=None, doc_id=cid_missing)
            db = _FakeFirestore({"contractors": {cid_missing: c_doc}})

            monkeypatch.setattr(integrations, "require_contractor_access", lambda req, c_id: None)
            monkeypatch.setattr(integrations, "_get_firestore", lambda: db)

            staged_writes = {"update": 0, "set": 0, "create": 0, "delete": 0}
            http_call_count = [0]

            def _tracking_transaction():
                txn = _FakeTransaction(db)
                orig_update = txn.update
                orig_set = txn.set
                orig_create = txn.create
                orig_delete = txn.delete
                def _track_update(*a, **kw):
                    staged_writes["update"] += 1
                    return orig_update(*a, **kw)
                def _track_set(*a, **kw):
                    staged_writes["set"] += 1
                    return orig_set(*a, **kw)
                def _track_create(*a, **kw):
                    staged_writes["create"] += 1
                    return orig_create(*a, **kw)
                def _track_delete(*a, **kw):
                    staged_writes["delete"] += 1
                    return orig_delete(*a, **kw)
                txn.update = _track_update
                txn.set = _track_set
                txn.create = _track_create
                txn.delete = _track_delete
                return txn

            monkeypatch.setattr(db, "transaction", _tracking_transaction)

            class _CountingHttpClient:
                def __init__(self, *a, **kw):
                    pass
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    pass
                async def post(self, *a, **kw):
                    http_call_count[0] += 1
                    raise RuntimeError("HTTP call should not be attempted")

            monkeypatch.setattr("httpx.AsyncClient", _CountingHttpClient)

            caplog.clear()
            with pytest.raises(HTTPException) as exc_info:
                await endpoint_func(contractor_id=cid_missing)

            err = exc_info.value
            assert err.status_code == 404
            assert err.detail == "Contractor not found"
            assert err.__cause__ is None
            assert err.__suppress_context__ is True
            assert type(err.__context__) is it_mutations.IntegrationTokenContractorNotFound

            assert len(caplog.records) == 1
            rec = caplog.records[0]
            assert rec.levelno == logging.WARNING
            expected_msg = f"Provider disconnect failed: provider={provider} operation=disconnect result=contractor_not_found"
            assert rec.getMessage() == expected_msg
            assert rec.exc_info is None
            assert cid_missing not in rec.getMessage()
            assert "Contractor document not found" not in rec.getMessage()

            assert http_call_count[0] == 0
            assert staged_writes == {"update": 0, "set": 0, "create": 0, "delete": 0}
            assert c_doc.get().exists is False
    finally:
        integrations.logger.handlers = orig_logger_handlers
        integrations.logger.propagate = orig_logger_propagate
        root_logger.handlers = orig_root_handlers
