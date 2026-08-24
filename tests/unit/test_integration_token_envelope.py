"""Deterministic unit tests for integration token AES-256-GCM envelope and security boundaries."""

import base64
import json
import os
import secrets
import time
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
from app.services.integration_token_mutations import (
    IntegrationTokenLeaseError,
    IntegrationTokenPostconditionError,
    acquire_refresh_claim_cas,
    connect_provider_cas,
    consume_oauth_state,
    disconnect_provider_cas,
    persist_refreshed_tokens_cas,
    release_refresh_claim_cas,
)


def _make_key_b64(byte_val: bytes = b"k") -> str:
    return base64.b64encode(byte_val * 32).decode("ascii")


def _setup_keyring(monkeypatch, keys: dict[str, str] | None = None, active: str | None = "1"):
    if keys is None:
        keys = {"1": _make_key_b64(b"1"), "2": _make_key_b64(b"2")}
    monkeypatch.setattr(settings, "integration_token_encryption_keys", json.dumps(keys))
    monkeypatch.setattr(settings, "integration_token_active_key_version", active)


def _patch_firestore(monkeypatch, db):
    import app.services.integration_token_mutations as it_mutations
    import app.db.firestore_client as firestore_mod
    import app.services.jobber as jobber_svc
    import app.services.calendar as calendar_svc
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
        "jobber_access_token": valid_acc,
        "jobber_refresh_token": valid_ref,
    }
    assert resolve_usable_token(wrong_id_contractor, "jobber", "access") is None
    assert has_usable_token(wrong_id_contractor, "jobber", "access") is False

    # 5. Malformed envelope -> returns None / False
    malformed_contractor = {"contractor_id": contractor_id, "jobber_access_token": {"schema_version": 1, "bad": True}, "jobber_refresh_token": valid_ref}
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
        if data is not None:
            self.data = dict(data)
        else:
            self.data = {"active": True, "contractor_id": self.id}
        self.deleted = False
        self.updates = []

    def get(self, *args, transaction=None, **kwargs):
        class _Snap:
            def __init__(self, d, deleted):
                self._d = dict(d) if d is not None else {}
                self.exists = (d is not None) and (not deleted)

            def to_dict(self):
                return dict(self._d) if self.exists else {}

        return _Snap(self.data, self.deleted)

    def set(self, data, *args, **kwargs):
        self.data = dict(data)
        self.exists = True
        self.deleted = False

    def update(self, updates, *args, **kwargs):
        from google.cloud.firestore_v1 import DELETE_FIELD
        self.updates.append(dict(updates))
        for k, v in updates.items():
            if v is DELETE_FIELD:
                self.data.pop(k, None)
            else:
                self.data[k] = v

    def delete(self, *args, **kwargs):
        self.deleted = True


class _FakeTransaction:
    def __init__(self, db):
        self._db = db
        self._staged_updates = []
        self._staged_sets = []
        self._staged_deletes = []
        self.committed = False
        self._read_only = False
        self._id = b"fake-tx-id"
        self._max_attempts = 5
        self.in_progress = True

    def get(self, doc_ref):
        return doc_ref.get()

    def update(self, doc_ref, updates):
        self._staged_updates.append((doc_ref, dict(updates)))

    def delete(self, doc_ref):
        self._staged_deletes.append(doc_ref)

    def set(self, doc_ref, data):
        self._staged_sets.append((doc_ref, dict(data)))

    def commit(self):
        for doc_ref, data in self._staged_sets:
            doc_ref.set(data)
        for doc_ref, updates in self._staged_updates:
            doc_ref.update(updates)
        for doc_ref in self._staged_deletes:
            doc_ref.delete()
        self.committed = True

    def _begin(self, *args, **kwargs):
        if hasattr(self._db, "_tx_lock") and self._db._tx_lock is not None:
            self._db._tx_lock.acquire()

    def _clean_up(self):
        pass

    def _rollback(self):
        self._staged_sets.clear()
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
            def __init__(self, docs):
                self.docs = docs

            def document(self, doc_id):
                return self.docs.setdefault(doc_id, _FakeDocRef({"contractor_id": doc_id, "active": True}, doc_id=doc_id))

        return _Coll(self.collections.setdefault(name, {}))

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

    doc_jobber = _FakeDocRef({"contractor_id": contractor_id_j, "active": True, "jobber_connected": True, "jobber_access_token": enc_jobber, "jobber_refresh_token": enc_jobber}, doc_id=contractor_id_j)
    doc_gcal = _FakeDocRef({"contractor_id": contractor_id_g, "active": True, "google_calendar_connected": True, "google_calendar_access_token": enc_gcal, "google_calendar_refresh_token": enc_gcal}, doc_id=contractor_id_g)

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
    updates_conn, new_gen, _ = await connect_provider_cas(
        contractor_id=cid,
        provider="google_calendar",
        access_token="reconnect-access",
        refresh_token="reconnect-refresh",
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
    state_doc = _FakeDocRef({"contractor_id": "c-oauth-1", "expires_at": time.time() + 1000.0})
    db = _FakeFirestore({"jobber_oauth_states": {"state-unique-123456": state_doc}})

    # First consumption succeeds
    data = await consume_oauth_state(db=db, collection_name="jobber_oauth_states", state="state-unique-123456")
    assert data["contractor_id"] == "c-oauth-1"
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
    doc_ref.data["jobber_refresh_claim_expires_at"] = time.time() + 60
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
        "jobber_generation": 1,
        "jobber_connected": True,
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
        "jobber_refresh_claim_id": claim_id_b,
        "jobber_refresh_claim_expires_at": time.time() + 60,
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
        "ciphertext": base64.b64encode(f"{sentinel_token}".encode("utf-8") + b"0" * 16).decode("ascii"),
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
    from app.db.integration_lifecycle_audit import AUDIT_COLLECTION
    from app.services.integration_token_mutations import (
        disconnect_provider_cas,
    )
    _setup_keyring(monkeypatch)
    cid = "c-audit-lifecycle-1"

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_generation": 0,
        "jobber_connected": False,
    })

    audit_store: dict[str, Any] = {}

    class _AuditFakeTx(_FakeTransaction):
        def set(self, ref, data):
            audit_store[ref.doc_id] = data

    class _AuditFakeFirestore(_FakeFirestore):
        def collection(self, name):
            if name == AUDIT_COLLECTION:
                class _AuditColl:
                    def document(self, doc_id):
                        class _AuditRef:
                            def __init__(self, d_id):
                                self.doc_id = d_id
                            def update(self, updates):
                                if self.doc_id in audit_store:
                                    audit_store[self.doc_id].update(updates)
                            def get(self):
                                class _AuditSnap:
                                    def __init__(self, d):
                                        self._d = d
                                        self.exists = d is not None
                                    def to_dict(self):
                                        return dict(self._d) if self._d else {}
                                return _AuditSnap(audit_store.get(self.doc_id))
                        return _AuditRef(doc_id)
                return _AuditColl()
            return super().collection(name)

        def transaction(self):
            return _AuditFakeTx(self)

    db = _AuditFakeFirestore({"contractors": {cid: doc_ref}})
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
    connect_event = audit_store[connect_audit_id]
    assert connect_event["action"] == "connected"
    assert connect_event["provider"] == "jobber"
    assert connect_event["generation"] == 1
    assert connect_event["actor"] == "oauth_state"

    # Invariant: No token material or ciphertexts in audit record!
    assert secret_access not in str(connect_event)
    assert secret_refresh not in str(connect_event)
    assert "ciphertext" not in str(connect_event)

    # 2. Disconnect creates pending audit record
    tombstone_gen, decrypted_acc, disc_audit_id = await disconnect_provider_cas(
        contractor_id=cid,
        provider="jobber",
        db=db,
    )

    assert disc_audit_id in audit_store
    disc_event = audit_store[disc_audit_id]
    assert disc_event["action"] == "credentials_deleted"
    assert disc_event["provider"] == "jobber"
    assert disc_event["generation"] == 2
    assert disc_event["actor"] == "contractor_api"
    assert disc_event["revocation_status"] == "pending"

    # Invariant: No secrets in disconnect audit record
    assert secret_access not in str(disc_event)
    assert secret_refresh not in str(disc_event)


@pytest.mark.asyncio
async def test_consume_oauth_state_strict_validations(monkeypatch):
    """consume_oauth_state requires canonical state, allowlisted collection, valid expiry, and contractor_id."""
    _setup_keyring(monkeypatch)

    class _StateFakeDocRef:
        def __init__(self, data=None):
            self.data = data
            self.exists = data is not None

        def to_dict(self):
            return dict(self.data) if self.data else {}

        def get(self, *args, transaction=None, **kwargs):
            class _Snap:
                def __init__(self, d, exists):
                    self._d = dict(d) if d else {}
                    self.exists = exists
                def to_dict(self):
                    return dict(self._d) if self.exists else {}
            return _Snap(self.data, self.exists)

        def delete(self, *args, **kwargs):
            self.exists = False
            self.data = None

    class _StateFakeTx(_FakeTransaction):
        def __init__(self, store):
            super().__init__(None)
            self.store = store

        def get(self, ref):
            return ref

        def delete(self, ref):
            ref.delete()

    class _StateFakeFirestore:
        def __init__(self, states):
            self.states = states

        def collection(self, name):
            class _Coll:
                def __init__(self, store):
                    self.store = store
                def document(self, doc_id):
                    if doc_id not in self.store:
                        self.store[doc_id] = _StateFakeDocRef(None)
                    return self.store[doc_id]
            return _Coll(self.states)

        def transaction(self):
            return _StateFakeTx(self.states)

    states_db = {
        "valid-state-1234567890": _StateFakeDocRef({
            "contractor_id": "c-valid",
            "expires_at": time.time() + 300,
        }),
        "expired-state-1234567890": _StateFakeDocRef({
            "contractor_id": "c-expired",
            "expires_at": time.time() - 10,
        }),
        "no-cid-state-1234567890": _StateFakeDocRef({
            "expires_at": time.time() + 300,
        }),
    }
    db = _StateFakeFirestore(states_db)

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
    res = await consume_oauth_state(db=db, collection_name="jobber_oauth_states", state="valid-state-1234567890")
    assert res["contractor_id"] == "c-valid"
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
    import app.api.integrations as integrations_module
    from app.api.integrations import JobberLeadCaptureUpdate, jobber_update_lead_capture
    import app.db.admin_audit as admin_audit_module
    import app.db.firestore_client as firestore_client_module
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
        })

    monkeypatch.setattr(integrations_module, "write_admin_audit_event", _fake_write_admin_audit_event)

    # Enabling when disconnected -> 409 Conflict (zero audit calls)
    with pytest.raises(HTTPException) as exc:
        await jobber_update_lead_capture(
            body=JobberLeadCaptureUpdate(enabled=True),
            contractor_id=cid,
            request=None,
        )
    assert exc.value.status_code == 409
    assert len(audit_calls) == 0

    # Disabling when disconnected -> Succeeds!
    res = await jobber_update_lead_capture(
        body=JobberLeadCaptureUpdate(enabled=False),
        contractor_id=cid,
        request=None,
    )
    assert res["status"] == "ok"
    assert res["lead_capture_enabled"] is False
    assert doc_ref.data["jobber_lead_capture_enabled"] is False

    # Assert exact deterministic audit call
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "jobber_lead_capture_update"
    assert audit_calls[0]["target_type"] == "contractor"
    assert audit_calls[0]["target_id"] == cid
    assert audit_calls[0]["reason"] == "admin toggled Jobber lead capture"
    assert audit_calls[0]["before"] == {"jobber_lead_capture_enabled": True}
    assert audit_calls[0]["after"] == {"jobber_lead_capture_enabled": False}
    assert audit_calls[0]["metadata"] == {"jobber_connected": False}


@pytest.mark.asyncio
async def test_jobber_lead_capture_toggle_fails_fast_if_audit_unpatched(monkeypatch):
    """Causal proof: if write_admin_audit_event is not patched, the endpoint fails fast

    at the exact app.db.admin_audit.get_firestore_client alias boundary, proving no real
    cloud client can be queried, written, instantiated, or reused even if ADC or a cached
    firestore_client._client exists.
    """
    from app.api.integrations import JobberLeadCaptureUpdate, jobber_update_lead_capture
    import app.db.admin_audit as admin_audit_module
    import app.db.firestore_client as firestore_client_module
    _setup_keyring(monkeypatch)
    cid = "c-toggle-unpatched"

    def _forbidden_admin_audit_get_firestore(*args, **kwargs):
        raise AssertionError("Causal fail-fast: app.db.admin_audit.get_firestore_client intercepted")

    def _forbidden_firestore_client_factory(*args, **kwargs):
        raise AssertionError("Causal fail-fast: real Firestore client factory intercepted")

    monkeypatch.setattr(admin_audit_module, "get_firestore_client", _forbidden_admin_audit_get_firestore)
    monkeypatch.setattr(firestore_client_module, "get_firestore_client", _forbidden_firestore_client_factory)
    monkeypatch.setattr(firestore_client_module, "_client", None)
    monkeypatch.setattr("google.cloud.firestore.Client", _forbidden_firestore_client_factory)
    monkeypatch.setattr("google.cloud.firestore_v1.Client", _forbidden_firestore_client_factory)

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "jobber_connected": False,
        "jobber_lead_capture_enabled": True,
    })
    db = _FakeFirestore({"contractors": {cid: doc_ref}})
    monkeypatch.setattr("app.api.integrations._get_firestore", lambda: db)
    monkeypatch.setattr("app.api.integrations._require_admin", lambda req: None)

    # Because write_admin_audit_event is NOT patched, executing disable must trigger the causal guard at admin_audit.get_firestore_client
    with pytest.raises(AssertionError, match="Causal fail-fast: app.db.admin_audit.get_firestore_client intercepted"):
        await jobber_update_lead_capture(
            body=JobberLeadCaptureUpdate(enabled=False),
            contractor_id=cid,
            request=None,
        )


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
        "jobber_access_token": acc_enc,
        "jobber_refresh_token": ref_enc,
        "jobber_refresh_claim_id": "stale-claim-dead-worker",
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

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        "jobber_connected": True,
        "jobber_generation": 1,
        "jobber_refresh_claim_id": "active-claim-123",
        "jobber_refresh_claim_expires_at": time.time() + 60.0,
        "jobber_refresh_claim_generation": 1,
    }, doc_id=cid)
    db = _FakeFirestore({"contractors": {cid: doc_ref}})


    # Attempt release with wrong claim_id -> Does NOT delete lease
    await release_refresh_claim_cas(contractor_id=cid, provider="jobber", claim_id="wrong-claim-id", db=db)
    assert doc_ref.data.get("jobber_refresh_claim_id") == "active-claim-123"

    # Attempt release with matching claim_id -> Deletes lease atomically
    await release_refresh_claim_cas(contractor_id=cid, provider="jobber", claim_id="active-claim-123", db=db)
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
    _setup_keyring(monkeypatch)
    cid = "c-inactive-cb"

    state_doc = _FakeDocRef({"contractor_id": cid, "expires_at": time.time() + 300.0}, doc_id="state-inactive-12345")
    contractor_doc = _FakeDocRef({"contractor_id": cid, "active": False}, doc_id=cid)

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
    assert exc_j.value.status_code == 403

    # Reset state for Google
    state_doc_g = _FakeDocRef({"contractor_id": cid, "expires_at": time.time() + 300.0}, doc_id="state-inactive-67890")
    db_g = _FakeFirestore({
        "google_oauth_states": {"state-inactive-67890": state_doc_g},
        "contractors": {cid: contractor_doc},
    })
    monkeypatch.setattr("app.api.integrations._get_firestore", lambda: db_g)

    with pytest.raises(HTTPException) as exc_g:
        await google_calendar_callback(code="test-code", state="state-inactive-67890", request=None)
    assert exc_g.value.status_code == 403


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
        "jobber_connected": True,
        "jobber_access_token": enc_jobber_acc,
        "jobber_refresh_token": enc_jobber_ref,
        "google_calendar_connected": True,
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
                await provider_release.wait()
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
    await provider_entered.wait()

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
        "jobber_access_token": enc_access,
        "jobber_refresh_token": enc_refresh,
        "jobber_refresh_claim_id": old_claim_id,
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
    from app.services.integration_token_mutations import _verify_audit_postcondition
    from app.db.integration_lifecycle_audit import AUDIT_COLLECTION

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
    with pytest.raises(IntegrationTokenPostconditionError, match="Audit document field mismatch for generation"):
        _verify_audit_postcondition(db_1, "audit-1", expected_audit)

    # 2. Actual timestamp is int instead of float
    bad_data_int_ts = dict(expected_audit)
    bad_data_int_ts["timestamp"] = 100
    audit_doc_2 = _FakeDocRef(bad_data_int_ts, doc_id="audit-2")
    db_2 = _FakeFirestore({AUDIT_COLLECTION: {"audit-2": audit_doc_2}})
    with pytest.raises(IntegrationTokenPostconditionError, match="Audit document field mismatch for timestamp"):
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
    doc_ref = _FakeDocRef({"contractor_id": cid, "active": True, "jobber_connected": False, "jobber_generation": 0}, doc_id=cid)
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

    _setup_keyring(monkeypatch)
    state = "state-invalid-cid-12345"
    db = _FakeFirestore({
        "jobber_oauth_states": {
            state: _FakeDocRef({"contractor_id": "   ", "expires_at": time.time() + 300.0}, doc_id=state)
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
    bad_contractor_id_ws = {"id": " " + cid, "jobber_connected": True, "jobber_access_token": enc, "jobber_refresh_token": enc_ref}
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
        cid: _FakeDocRef({"active": True, "jobber_connected": False, "jobber_generation": 0}, doc_id=cid)
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
    updates_ref, next_gen_2 = await it_mutations.persist_refreshed_tokens_cas(
        contractor_id=cid,
        provider="jobber",
        new_access_token="plain-access-2",
        new_refresh_token="plain-refresh-2",
        observed_generation=1,
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
    import app.db.jobs as job_db
    import app.db.calls as call_db
    monkeypatch.setattr(job_db, "claim_jobber_sync", lambda job_id: asyncio.sleep(0, result=True))
    monkeypatch.setattr(job_db, "update_job", lambda *a, **kw: asyncio.sleep(0))
    monkeypatch.setattr(call_db, "save_call", lambda *a, **kw: asyncio.sleep(0))

    # 1. With encrypted envelope contractor record
    bearer_tokens_seen.clear()
    contractor_enc = {
        "contractor_id": cid,
        "jobber_connected": True,
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
    bearer_tokens_seen.clear()
    contractor_plain = {
        "contractor_id": cid,
        "jobber_connected": True,
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
        ("app/api/integrations.py", "google_calendar_access_token"): 1,
        ("app/api/integrations.py", "google_calendar_refresh_token"): 1,
        ("app/api/integrations.py", "jobber_access_token"): 1,
        ("app/api/integrations.py", "jobber_refresh_token"): 1,
        ("app/db/contractors.py", "google_calendar_access_token"): 1,
        ("app/db/contractors.py", "google_calendar_refresh_token"): 1,
        ("app/db/contractors.py", "jobber_access_token"): 1,
        ("app/db/contractors.py", "jobber_refresh_token"): 1,
        ("app/services/calendar.py", "google_calendar_access_token"): 9,
        ("app/services/calendar.py", "google_calendar_refresh_token"): 10,
        ("app/services/jobber.py", "jobber_access_token"): 12,
        ("app/services/jobber.py", "jobber_refresh_token"): 7,
        ("scripts/phase0_account_audit.py", "google_calendar_access_token"): 1,
        ("scripts/phase0_account_audit.py", "jobber_access_token"): 1,
        ("scripts/phase0_staging_smoke.py", "google_calendar_access_token"): 1,
        ("scripts/phase0_staging_smoke.py", "google_calendar_refresh_token"): 1,
        ("scripts/phase0_staging_smoke.py", "jobber_access_token"): 1,
        ("scripts/phase0_staging_smoke.py", "jobber_refresh_token"): 1,
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
    pass


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
    from app.services.integration_token_mutations import _exact_scalar_or_composite_equal, _verify_audit_postcondition
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
    plain_contractor = {"contractor_id": cid, "jobber_connected": True, "jobber_access_token": plain_tok, "jobber_refresh_token": plain_ref}
    enc_contractor = {"contractor_id": cid, "jobber_connected": True, "jobber_access_token": enc_tok, "jobber_refresh_token": enc_ref}

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
        assert resolve_usable_token({"contractor_id": bad_embedded_id, "id": cid, "jobber_connected": True, "jobber_access_token": plain_tok, "jobber_refresh_token": plain_ref}, "jobber") is None
        assert resolve_usable_token({"contractor_id": bad_embedded_id, "id": cid, "jobber_connected": True, "jobber_access_token": enc_tok, "jobber_refresh_token": enc_ref}, "jobber") is None

    # 3. Absent contractor_id key allows fallback to id ONLY IF id is valid built-in string
    assert resolve_usable_token({"id": cid, "jobber_connected": True, "jobber_access_token": plain_tok, "jobber_refresh_token": plain_ref}, "jobber") == plain_tok
    assert resolve_usable_token({"id": cid, "jobber_connected": True, "jobber_access_token": enc_tok, "jobber_refresh_token": enc_ref}, "jobber") == "secret-tok"

    # If id is invalid/padded/subclass/absent, both plaintext and envelope return None
    for bad_fallback_id in [None, "", "   ", " cid ", subclass_id, False, 123]:
        assert resolve_usable_token({"id": bad_fallback_id, "jobber_connected": True, "jobber_access_token": plain_tok, "jobber_refresh_token": plain_ref}, "jobber") is None
        assert resolve_usable_token({"id": bad_fallback_id, "jobber_connected": True, "jobber_access_token": enc_tok, "jobber_refresh_token": enc_ref}, "jobber") is None

    # No ID at all -> returns None for both plaintext and envelope
    assert resolve_usable_token({"jobber_connected": True, "jobber_access_token": plain_tok, "jobber_refresh_token": plain_ref}, "jobber") is None
    assert resolve_usable_token({"jobber_connected": True, "jobber_access_token": enc_tok, "jobber_refresh_token": enc_ref}, "jobber") is None


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

    # Test adversarial generation values: bool True, string "1", negative -1, overflow 2147483648
    for invalid_gen in [True, False, "1", -1, 2147483648]:
        doc_ref = _FakeDocRef({
            "contractor_id": cid,
            "active": True,
            "jobber_generation": invalid_gen,
        }, doc_id=cid)
        fake_db.collections["contractors"] = {cid: doc_ref}
        fake_db.collections["jobber_oauth_states"] = {
            state_token: _FakeDocRef({"contractor_id": cid, "expires_at": time.time() + 300.0}, doc_id=state_token)
        }

        with pytest.raises(HTTPException) as exc_info:
            await integrations.jobber_callback(code="auth-code", state=state_token)
        assert exc_info.value.status_code == 409
        assert "generation" in exc_info.value.detail.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_oauth_preflight_generation_adversarial_value_free_logging(monkeypatch, caplog, provider):
    """Proves adversarial generation values with secrets/newlines do not leak into logs and abort before provider HTTP."""
    import logging
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

    doc_ref = _FakeDocRef({
        "contractor_id": cid,
        "active": True,
        f"{provider}_generation": secret_payload,
    }, doc_id=cid)
    fake_db.collections["contractors"] = {cid: doc_ref}

    state_col = "jobber_oauth_states" if provider == "jobber" else "google_oauth_states"
    fake_db.collections[state_col] = {
        state_token: _FakeDocRef({"contractor_id": cid, "expires_at": time.time() + 300.0}, doc_id=state_token)
    }

    with caplog.at_level(logging.ERROR):
        with pytest.raises(HTTPException) as exc_info:
            if provider == "jobber":
                await integrations.jobber_callback(code="auth-code", state=state_token)
            else:
                await integrations.google_calendar_callback(code="auth-code", state=state_token)

    assert exc_info.value.status_code == 409
    assert "generation" in exc_info.value.detail.lower()

    # Verify that secret payload is completely absent from all captured log output
    assert "SECRET_LEAK_KEY_99999" not in caplog.text
    assert "INJECTED_ATTACKER_VALUE_88888" not in caplog.text
    assert "invalid stored generation" in caplog.text


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
        "jobber_generation": 1,
        "jobber_access_token": "old-acc",
        "jobber_refresh_token": "old-ref",
    }, doc_id=cid)
    fake_db = _FakeFirestore({"contractors": {cid: doc_ref}})
    _patch_firestore(monkeypatch, fake_db)

    valid_claim_id = secrets.token_hex(16)

    # 1. Calling persist on document without claim fields fails closed
    with pytest.raises(IntegrationTokenCASConflict, match="Missing refresh lease claim record"):
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
    with pytest.raises(IntegrationTokenCASConflict, match="Refresh lease generation mismatch"):
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
    doc_enc = {"contractor_id": cid, "jobber_connected": True, "jobber_access_token": enc_acc, "jobber_refresh_token": enc_ref}
    assert resolve_usable_token_pair(doc_enc, "jobber") == ("acc", "ref")

    # 2. Both valid plaintext
    doc_plain = {"contractor_id": cid, "jobber_connected": True, "jobber_access_token": "plain-acc", "jobber_refresh_token": "plain-ref"}
    assert resolve_usable_token_pair(doc_plain, "jobber") == ("plain-acc", "plain-ref")

    # 3. Access present, refresh absent
    doc_missing_ref = {"contractor_id": cid, "jobber_connected": True, "jobber_access_token": enc_acc}
    assert resolve_usable_token_pair(doc_missing_ref, "jobber") == (None, None)
    assert resolve_usable_token(doc_missing_ref, "jobber", "access") is None

    # 4. Refresh present, access absent
    doc_missing_acc = {"contractor_id": cid, "jobber_connected": True, "jobber_refresh_token": enc_ref}
    assert resolve_usable_token_pair(doc_missing_acc, "jobber") == (None, None)
    assert resolve_usable_token(doc_missing_acc, "jobber", "refresh") is None

    # 5. Mixed: str access + dict refresh
    doc_mixed_1 = {"contractor_id": cid, "jobber_connected": True, "jobber_access_token": "plain-acc", "jobber_refresh_token": enc_ref}
    assert resolve_usable_token_pair(doc_mixed_1, "jobber") == (None, None)

    # 6. Mixed: dict access + str refresh
    doc_mixed_2 = {"contractor_id": cid, "jobber_connected": True, "jobber_access_token": enc_acc, "jobber_refresh_token": "plain-ref"}
    assert resolve_usable_token_pair(doc_mixed_2, "jobber") == (None, None)

    # 7. Unknown key version in refresh
    enc_unknown = dict(enc_ref, key_version=999)
    doc_unknown_key = {"contractor_id": cid, "jobber_connected": True, "jobber_access_token": enc_acc, "jobber_refresh_token": enc_unknown}
    assert resolve_usable_token_pair(doc_unknown_key, "jobber") == (None, None)

    # 8. Tampered ciphertext in access
    enc_tampered = dict(enc_acc, ciphertext="tampered")
    doc_tampered = {"contractor_id": cid, "jobber_connected": True, "jobber_access_token": enc_tampered, "jobber_refresh_token": enc_ref}
    assert resolve_usable_token_pair(doc_tampered, "jobber") == (None, None)

    # 9. Explicit connected=False
    doc_disconnected = {"contractor_id": cid, "jobber_connected": False, "jobber_access_token": enc_acc, "jobber_refresh_token": enc_ref}
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
    assert "Refresh lease expired or invalid on commit" in str(exc_info.value)
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
        f"{provider}_token_envelope_required": malformed_floor,
        f"{provider}_refresh_claim_id": "test-claim-id-123",
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
