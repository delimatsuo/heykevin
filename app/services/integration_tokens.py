"""Versioned, context-bound AES-256-GCM encryption envelope for integration tokens.

Protects persisted Jobber and Google Calendar OAuth access and refresh tokens
at rest in Firestore on top of cloud storage encryption.

Single Compatibility Release Contract:
- Strict JSON keyring parsing for INTEGRATION_TOKEN_ENCRYPTION_KEYS
- Active key version enforcement for INTEGRATION_TOKEN_ACTIVE_KEY_VERSION
- Canonical AAD binding (schema_version, key_version, algorithm, contractor_id, provider, token_kind)
- v1 Firestore map envelope decryption and validation
- Strict scalar and dictionary key type validation (rejects bool, subclasses, malformed payloads)
- Token string hygiene and opacity (never strips; rejects control chars, edge whitespace, invalid UTF-8)
- Centralized write format policy (determine_write_format) enforcing monotonic representation
- Zero database or mutation dependencies in this pure crypto/reader/policy module
"""

from __future__ import annotations

import base64
import json
import math
import re
import secrets
from typing import Any, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SCHEMA_VERSION = 1
ALGORITHM = "AES-256-GCM"

VALID_PROVIDERS = frozenset({"jobber", "google_calendar"})
VALID_TOKEN_KINDS = frozenset({"access", "refresh"})

MIN_KEY_VERSION = 1
MAX_KEY_VERSION = 2147483647  # 31-bit signed integer max

MIN_CIPHERTEXT_BYTES = 17  # 1 byte plaintext + 16 bytes tag
MAX_CIPHERTEXT_BYTES = 16400  # 16384 bytes plaintext + 16 bytes tag
MAX_PLAINTEXT_BYTES = 16384
MAX_CONTRACTOR_ID_BYTES = 1500

ENVELOPE_KEYS = frozenset({"schema_version", "key_version", "algorithm", "nonce", "ciphertext"})
_CANONICAL_VERSION_REGEX = re.compile(r"^[1-9][0-9]*$")


class IntegrationTokenError(Exception):
    """Base exception for all integration token operations."""


class IntegrationTokenConfigError(IntegrationTokenError):
    """Raised when integration token key configuration is missing or invalid."""


class IntegrationTokenEnvelopeError(IntegrationTokenError):
    """Raised when an encrypted token envelope is malformed or has invalid fields."""


class IntegrationTokenDecryptionError(IntegrationTokenError):
    """Raised when decryption fails, authentication fails, or a required key is absent."""


class IntegrationTokenCASConflict(IntegrationTokenError):
    """Raised when a durable generation or credential state CAS precondition fails."""


def validate_token_string(
    val: Any,
    *,
    name: str = "token",
    allow_none: bool = False,
) -> Optional[str]:
    """Validate that a token is an exact, non-empty, well-formed str without stripping or modifying it.

    Rejects:
    - Non-str types (including bool, int, dict, list, bytes)
    - String subclasses
    - Empty strings
    - Leading or trailing Unicode whitespace
    - C0 and C1 control characters (e.g. \x00-\x1f, \x7f, \x80-\x9f)
    - Byte length outside 1..MAX_PLAINTEXT_BYTES
    """
    if val is None:
        if allow_none:
            return None
        raise IntegrationTokenEnvelopeError(f"{name} is required")

    if type(val) is not str:
        raise IntegrationTokenEnvelopeError(f"Invalid {name}: must be an exact str")

    if not val:
        raise IntegrationTokenEnvelopeError(f"Invalid {name}: token cannot be empty")

    # Reject leading/trailing whitespace without stripping
    if val != val.strip():
        raise IntegrationTokenEnvelopeError(f"Invalid {name}: token contains leading or trailing whitespace")

    # Reject C0 and C1 control characters
    for ch in val:
        code = ord(ch)
        if code < 32 or (127 <= code <= 159):
            raise IntegrationTokenEnvelopeError(f"Invalid {name}: token contains disallowed control characters")

    try:
        raw_bytes = val.encode("utf-8")
    except UnicodeEncodeError:
        raise IntegrationTokenEnvelopeError(f"Invalid {name}: token cannot be encoded as UTF-8") from None

    if not (1 <= len(raw_bytes) <= MAX_PLAINTEXT_BYTES):
        raise IntegrationTokenEnvelopeError(
            f"Invalid {name}: byte length outside range 1..{MAX_PLAINTEXT_BYTES}"
        )

    return val


def validate_token_expires_in(val: Any) -> Optional[float]:
    """Validate that token expires_in duration is an exact built-in int or float in range 1..31536000."""
    if val is None:
        return None
    if type(val) not in (int, float) or type(val) is bool:
        raise IntegrationTokenEnvelopeError("Invalid expires_in duration")
    val_f = float(val)
    if not math.isfinite(val_f) or not (1.0 <= val_f <= 31536000.0):
        raise IntegrationTokenEnvelopeError("Invalid expires_in duration")
    return val_f


def validate_token_expires_at(val: Any) -> Optional[float]:
    """Validate that token expires_at timestamp is an exact built-in int or float in absolute range."""
    if val is None:
        return None
    if type(val) not in (int, float) or type(val) is bool:
        raise IntegrationTokenEnvelopeError("Invalid expires_at timestamp")
    val_f = float(val)
    if not math.isfinite(val_f) or not (1.0 <= val_f <= 2147483647000.0):
        raise IntegrationTokenEnvelopeError("Invalid expires_at timestamp")
    return val_f


def _exact_raw_credential_equal(a: Any, b: Any) -> bool:
    """Exact-type structural equality comparison for raw stored token credentials."""
    if a is None and b is None:
        return True
    if type(a) is str and type(b) is str:
        return a == b
    if type(a) is dict and type(b) is dict:
        for k in a.keys():
            if type(k) is not str:
                return False
        for k in b.keys():
            if type(k) is not str:
                return False
        if set(a.keys()) != set(b.keys()):
            return False
        for k, v_a in a.items():
            v_b = b[k]
            if type(v_a) is not type(v_b):
                return False
            if (type(v_a) is bool) != (type(v_b) is bool):
                return False
            if v_a != v_b:
                return False
        return True
    return False


def _reject_json_constants(val: str) -> None:
    raise IntegrationTokenConfigError("Disallowed JSON constant (NaN or Infinity)")


def _validate_keyring_dict(keyring: Any) -> None:
    """Validate that an in-memory keyring dict meets key version, byte length, and uniqueness rules."""
    if type(keyring) is not dict:
        raise IntegrationTokenConfigError("Keyring must be an exact dict")
    seen_bytes = set()
    for k, v in keyring.items():
        if type(k) is not int or type(k) is bool or not (MIN_KEY_VERSION <= k <= MAX_KEY_VERSION):
            raise IntegrationTokenConfigError(
                "Keyring version key must be an exact int in range 1..2147483647"
            )
        if type(v) is not bytes or len(v) != 32:
            raise IntegrationTokenConfigError(
                "Keyring key value must be exact bytes of length 32"
            )
        if v in seen_bytes:
            raise IntegrationTokenConfigError(
                "Duplicate key material across different key versions in keyring"
            )
        seen_bytes.add(v)


def parse_keyring(raw_json: str) -> dict[int, bytes]:
    """Parse and strictly validate a raw JSON object string mapping version strings to base64 keys."""
    if type(raw_json) is not str or not raw_json.strip():
        raise IntegrationTokenConfigError("Raw keyring JSON must be a non-empty str")

    def _strict_pairs_hook(pairs: list[tuple[Any, Any]]) -> dict[Any, Any]:
        d: dict[Any, Any] = {}
        for k, v in pairs:
            if k in d:
                raise IntegrationTokenConfigError("Duplicate key in keyring JSON")
            d[k] = v
        return d

    try:
        parsed = json.loads(
            raw_json,
            parse_constant=_reject_json_constants,
            object_pairs_hook=_strict_pairs_hook,
        )
    except IntegrationTokenConfigError:
        raise
    except Exception:
        raise IntegrationTokenConfigError("Keyring JSON failed to parse") from None

    if type(parsed) is not dict or not parsed:
        raise IntegrationTokenConfigError("Keyring JSON must be a non-empty object")

    keyring: dict[int, bytes] = {}
    for k, v in parsed.items():
        if type(k) is not str or not _CANONICAL_VERSION_REGEX.match(k):
            raise IntegrationTokenConfigError(
                "Keyring key must be a canonical positive integer string with no leading zeroes"
            )
        k_int = int(k)
        if not (MIN_KEY_VERSION <= k_int <= MAX_KEY_VERSION):
            raise IntegrationTokenConfigError(
                "Keyring version key must be in range 1..2147483647"
            )
        if type(v) is not str:
            raise IntegrationTokenConfigError("Keyring key value must be a base64 string")

        try:
            key_bytes = base64.b64decode(v, validate=True)
        except Exception:
            raise IntegrationTokenConfigError("Invalid base64 in keyring key material") from None

        if len(key_bytes) != 32:
            raise IntegrationTokenConfigError(
                "Keyring key material must decode to exactly 32 bytes (256 bits)"
            )

        keyring[k_int] = key_bytes

    _validate_keyring_dict(keyring)
    return keyring


def parse_active_key_version(val: Any) -> Optional[int]:
    """Parse and validate active key version setting."""
    if val is None or val == "":
        return None
    if type(val) is bool:
        raise IntegrationTokenConfigError("Active key version must not be a boolean")
    if type(val) is int:
        if not (MIN_KEY_VERSION <= val <= MAX_KEY_VERSION):
            raise IntegrationTokenConfigError(
                "Active key version int must be in range 1..2147483647"
            )
        return val
    if type(val) is str:
        if not _CANONICAL_VERSION_REGEX.match(val):
            raise IntegrationTokenConfigError(
                "Active key version string must be canonical positive integer with no leading zeroes"
            )
        v_int = int(val)
        if not (MIN_KEY_VERSION <= v_int <= MAX_KEY_VERSION):
            raise IntegrationTokenConfigError(
                "Active key version string must be in range 1..2147483647"
            )
        return v_int
    raise IntegrationTokenConfigError("Active key version must be an int or str")


def compute_aad(
    *,
    contractor_id: str,
    provider: str,
    token_kind: str,
    schema_version: int = SCHEMA_VERSION,
    key_version: int,
    algorithm: str = ALGORITHM,
) -> bytes:
    """Compute canonical deterministic JSON Additional Authenticated Data (AAD) for AES-GCM."""
    valid_cid = validate_token_string(contractor_id, name="contractor_id")
    assert valid_cid is not None
    if len(valid_cid.encode("utf-8")) > MAX_CONTRACTOR_ID_BYTES:
        raise IntegrationTokenEnvelopeError("contractor_id exceeds maximum byte length")

    if type(provider) is not str or provider not in VALID_PROVIDERS:
        raise IntegrationTokenEnvelopeError(
            f"Invalid provider: must be one of {sorted(VALID_PROVIDERS)}"
        )

    if type(token_kind) is not str or token_kind not in VALID_TOKEN_KINDS:
        raise IntegrationTokenEnvelopeError(
            f"Invalid token_kind: must be one of {sorted(VALID_TOKEN_KINDS)}"
        )

    if type(schema_version) is not int or type(schema_version) is bool or schema_version != SCHEMA_VERSION:
        raise IntegrationTokenEnvelopeError(f"schema_version must be exact int {SCHEMA_VERSION}")

    if type(key_version) is not int or type(key_version) is bool or not (MIN_KEY_VERSION <= key_version <= MAX_KEY_VERSION):
        raise IntegrationTokenEnvelopeError("key_version must be an exact int in range 1..2147483647")

    if type(algorithm) is not str or algorithm != ALGORITHM:
        raise IntegrationTokenEnvelopeError(f"algorithm must be exact str '{ALGORITHM}'")

    aad_dict = {
        "algorithm": algorithm,
        "contractor_id": valid_cid,
        "key_version": key_version,
        "provider": provider,
        "schema_version": schema_version,
        "token_kind": token_kind,
    }
    return json.dumps(aad_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def is_envelope_map(val: Any) -> bool:
    """Return True if val has the shape of an integration token envelope map."""
    if type(val) is not dict:
        return False
    return "schema_version" in val or "ciphertext" in val or "nonce" in val


def get_configured_keyring() -> dict[int, bytes]:
    """Load and return the parsed keyring from application settings."""
    from app.config import settings

    raw = getattr(settings, "integration_token_encryption_keys", "")
    if raw is None or (type(raw) is str and not raw.strip()):
        return {}
    return parse_keyring(raw)


def get_configured_active_key_version() -> Optional[int]:
    """Load and return the active key version from application settings."""
    from app.config import settings

    raw = getattr(settings, "integration_token_active_key_version", None)
    return parse_active_key_version(raw)


def is_encryption_configured() -> bool:
    """Return True if a valid keyring is loaded and active_key_version is present in the keyring."""
    try:
        keyring = get_configured_keyring()
        if not keyring:
            return False
        active_ver = get_configured_active_key_version()
        if active_ver is None:
            return False
        return active_ver in keyring
    except Exception:
        return False


def encrypt_integration_token(
    token: str,
    *,
    contractor_id: str,
    provider: str,
    token_kind: str,
    keyring: Optional[dict[int, bytes]] = None,
    active_key_version: Optional[int] = None,
    active_version: Optional[int] = None,
) -> dict[str, Any]:
    """Encrypt a plaintext integration token into a versioned AES-256-GCM envelope dictionary."""
    valid_token = validate_token_string(token, name="token")
    assert valid_token is not None

    if keyring is None:
        keyring = get_configured_keyring()
    else:
        _validate_keyring_dict(keyring)

    if not keyring:
        raise IntegrationTokenConfigError("Keyring is empty; cannot encrypt token")

    effective_active_ver = active_key_version if active_key_version is not None else active_version
    if effective_active_ver is None:
        effective_active_ver = get_configured_active_key_version()
    else:
        effective_active_ver = parse_active_key_version(effective_active_ver)

    if effective_active_ver is None:
        raise IntegrationTokenConfigError("No active key version configured for integration token encryption")

    if effective_active_ver not in keyring:
        raise IntegrationTokenConfigError(
            f"Active key version {effective_active_ver} not found in configured keyring"
        )

    key_bytes = keyring[effective_active_ver]
    nonce = secrets.token_bytes(12)
    aad = compute_aad(
        contractor_id=contractor_id,
        provider=provider,
        token_kind=token_kind,
        schema_version=SCHEMA_VERSION,
        key_version=effective_active_ver,
        algorithm=ALGORITHM,
    )

    pt_bytes = valid_token.encode("utf-8")
    aesgcm = AESGCM(key_bytes)
    ct_bytes = aesgcm.encrypt(nonce, pt_bytes, aad)

    return {
        "schema_version": SCHEMA_VERSION,
        "key_version": effective_active_ver,
        "algorithm": ALGORITHM,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ct_bytes).decode("ascii"),
    }


def decrypt_integration_token(
    envelope: Any,
    *,
    contractor_id: str,
    provider: str,
    token_kind: str,
    keyring: Optional[dict[int, bytes]] = None,
) -> str:
    """Decrypt an encrypted envelope dictionary or return a validated legacy plaintext token string."""
    if type(envelope) is str:
        if not envelope:
            return ""
        return validate_token_string(envelope, name="legacy_token")

    if type(envelope) is not dict:
        raise IntegrationTokenEnvelopeError("Envelope must be an exact dict or plaintext string")

    for k in envelope.keys():
        if type(k) is not str:
            raise IntegrationTokenEnvelopeError("Envelope keys must be exact str")

    if set(envelope.keys()) != ENVELOPE_KEYS:
        raise IntegrationTokenEnvelopeError(
            f"Envelope keys must exactly match {sorted(ENVELOPE_KEYS)}"
        )

    sv = envelope["schema_version"]
    if type(sv) is not int or type(sv) is bool or sv != SCHEMA_VERSION:
        raise IntegrationTokenEnvelopeError(
            f"Unsupported envelope schema_version: expected {SCHEMA_VERSION}"
        )

    kv = envelope["key_version"]
    if type(kv) is not int or type(kv) is bool or not (MIN_KEY_VERSION <= kv <= MAX_KEY_VERSION):
        raise IntegrationTokenEnvelopeError(
            "Envelope key_version must be an exact int in range 1..2147483647"
        )

    alg = envelope["algorithm"]
    if type(alg) is not str or alg != ALGORITHM:
        raise IntegrationTokenEnvelopeError(
            f"Unsupported envelope algorithm: expected '{ALGORITHM}'"
        )

    nonce_str = envelope["nonce"]
    if type(nonce_str) is not str or not nonce_str.strip():
        raise IntegrationTokenEnvelopeError("Envelope nonce must be a non-empty base64 str")

    try:
        nonce_bytes = base64.b64decode(nonce_str, validate=True)
    except Exception:
        raise IntegrationTokenEnvelopeError("Invalid base64 encoding in envelope nonce") from None

    if len(nonce_bytes) != 12:
        raise IntegrationTokenEnvelopeError("Envelope nonce must decode to exactly 12 bytes")

    ct_str = envelope["ciphertext"]
    if type(ct_str) is not str or not ct_str.strip():
        raise IntegrationTokenEnvelopeError("Envelope ciphertext must be a non-empty base64 str")

    try:
        ct_bytes = base64.b64decode(ct_str, validate=True)
    except Exception:
        raise IntegrationTokenEnvelopeError("Invalid base64 encoding in envelope ciphertext") from None

    if not (MIN_CIPHERTEXT_BYTES <= len(ct_bytes) <= MAX_CIPHERTEXT_BYTES):
        raise IntegrationTokenEnvelopeError(
            f"Envelope ciphertext length {len(ct_bytes)} outside valid range {MIN_CIPHERTEXT_BYTES}..{MAX_CIPHERTEXT_BYTES}"
        )

    if keyring is None:
        keyring = get_configured_keyring()
    else:
        _validate_keyring_dict(keyring)

    if kv not in keyring:
        raise IntegrationTokenDecryptionError(
            f"Key version {kv} required by envelope is not present in configured keyring"
        )

    key_bytes = keyring[kv]
    aad = compute_aad(
        contractor_id=contractor_id,
        provider=provider,
        token_kind=token_kind,
        schema_version=sv,
        key_version=kv,
        algorithm=alg,
    )

    aesgcm = AESGCM(key_bytes)
    try:
        pt_bytes = aesgcm.decrypt(nonce_bytes, ct_bytes, aad)
    except Exception:
        raise IntegrationTokenDecryptionError(
            "AEAD decryption/authentication failed (corrupted ciphertext, altered AAD, or wrong key)"
        ) from None

    if not (1 <= len(pt_bytes) <= MAX_PLAINTEXT_BYTES):
        raise IntegrationTokenDecryptionError(
            "Decrypted plaintext length outside range 1..16384"
        )

    try:
        pt_str = pt_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise IntegrationTokenDecryptionError("Decrypted token is not valid UTF-8") from None

    # Validate hygiene of decrypted token without stripping
    validate_token_string(pt_str, name="decrypted_token")

    return pt_str


def safe_decrypt_integration_token(
    stored: Any,
    *,
    contractor_id: str,
    provider: str,
    token_kind: str,
    keyring: Optional[dict[int, bytes]] = None,
) -> Optional[str]:
    """Attempt decryption of stored token; returns None if decryption or configuration fails."""
    try:
        val = decrypt_integration_token(
            stored,
            contractor_id=contractor_id,
            provider=provider,
            token_kind=token_kind,
            keyring=keyring,
        )
        return val if (type(val) is str and val) else None
    except Exception:
        return None


def resolve_usable_token_pair(
    contractor: dict | None,
    provider: str,
    *,
    contractor_id: str | None = None,
) -> tuple[str, str] | tuple[None, None]:
    """Resolve, authenticate, and safely decrypt a usable (access, refresh) token pair from contractor config.

    Both tokens must be simultaneously valid:
    - connected must be True (or absent for legacy records); explicit False or non-bool fails closed.
    - contractor_id must be valid non-empty str (either explicit or embedded).
    - BOTH access and refresh tokens must be exact valid plaintext strings,
      OR BOTH must be valid encrypted envelope dictionaries bound to the contractor and provider.
    - Absent, one-sided, mixed (str/dict), malformed, unknown-key, or tampered pairs return (None, None).
    """
    if type(contractor) is not dict:
        return None, None

    if type(provider) is not str or provider not in VALID_PROVIDERS:
        return None, None

    if contractor_id is not None:
        try:
            effective_id = validate_token_string(contractor_id, name="contractor_id")
        except Exception:
            return None, None
        if effective_id is None:
            return None, None
    else:
        if "contractor_id" in contractor:
            raw_id = contractor.get("contractor_id")
        elif "id" in contractor:
            raw_id = contractor.get("id")
        else:
            raw_id = None

        if raw_id is None:
            return None, None
        try:
            effective_id = validate_token_string(raw_id, name="contractor_id")
        except Exception:
            return None, None
        if effective_id is None:
            return None, None

    if f"{provider}_connected" in contractor:
        connected = contractor.get(f"{provider}_connected")
        if type(connected) is not bool or connected is False:
            return None, None

    env_req_key = f"{provider}_token_envelope_required"
    if env_req_key in contractor:
        env_req = contractor.get(env_req_key)
        if type(env_req) is not bool:
            return None, None
    else:
        env_req = None

    raw_access = contractor.get(f"{provider}_access_token")
    raw_refresh = contractor.get(f"{provider}_refresh_token")
    if raw_access is None or raw_refresh is None or raw_access == "" or raw_refresh == "":
        return None, None

    # Case A: Plaintext pair
    if type(raw_access) is str and type(raw_refresh) is str:
        if env_req is True:
            # Envelope required floor is active; plaintext pair is rejected
            return None, None
        try:
            valid_access = validate_token_string(raw_access, name=f"{provider}_access_token")
            valid_refresh = validate_token_string(raw_refresh, name=f"{provider}_refresh_token")
            if valid_access and valid_refresh:
                return valid_access, valid_refresh
        except Exception:
            return None, None
        return None, None

    # Case B: Envelope pair
    if type(raw_access) is dict and type(raw_refresh) is dict:
        val_access = safe_decrypt_integration_token(
            raw_access,
            contractor_id=effective_id,
            provider=provider,
            token_kind="access",
        )
        val_refresh = safe_decrypt_integration_token(
            raw_refresh,
            contractor_id=effective_id,
            provider=provider,
            token_kind="refresh",
        )
        if type(val_access) is str and val_access and type(val_refresh) is str and val_refresh:
            return val_access, val_refresh
        return None, None

    # Mixed, one-sided, or non-str/non-dict types
    return None, None


def resolve_usable_token(
    contractor: dict | None,
    provider: str,
    token_kind: str = "access",
    *,
    contractor_id: str | None = None,
) -> str | None:
    """Resolve and safely decrypt a usable integration token from a contractor config."""
    if type(token_kind) is not str or token_kind not in VALID_TOKEN_KINDS:
        return None
    acc, ref = resolve_usable_token_pair(
        contractor,
        provider=provider,
        contractor_id=contractor_id,
    )
    if token_kind == "access":
        return acc
    elif token_kind == "refresh":
        return ref
    return None


def has_usable_token(
    contractor: dict | None,
    provider: str,
    token_kind: str = "access",
    *,
    contractor_id: str | None = None,
) -> bool:
    """Return True if the contractor has a usable integration token for provider/kind."""
    return bool(
        resolve_usable_token(
            contractor,
            provider=provider,
            token_kind=token_kind,
            contractor_id=contractor_id,
        )
    )


def determine_write_format(
    *,
    contractor_id: str,
    provider: str,
    stored_access: Any,
    stored_refresh: Any,
    envelope_required: Optional[bool] = None,
    keyring: Optional[dict[int, bytes]] = None,
    active_key_version: Optional[int] = None,
    encrypted_writes_enabled: Optional[bool] = None,
) -> str:
    """Classify durable credentials and determine target write format ('plaintext' or 'envelope').

    Enforces monotonic representation and strict safety policies:
    - If either stored credential is an envelope dict, BOTH must be valid decryptable envelopes,
      and the target format MUST remain 'envelope' regardless of the encrypted_writes_enabled flag.
    - If envelope_required is True (durable monotonic floor established):
      - Absent credentials (None, None) write 'envelope' even when encrypted_writes_enabled is False.
      - Plaintext credentials (str, str) fail closed with a conflicted downgrade error.
      - Valid envelope credentials write 'envelope'.
    - If envelope_required is not True:
      - If stored credentials are both absent (None) or both plaintext strings:
        - If encrypted_writes_enabled is True: requires valid keyring and active key version -> 'envelope'
        - If encrypted_writes_enabled is False: target format is 'plaintext'
    - One-sided, mixed (str/dict), malformed, or non-(None/str/dict) types fail closed immediately.
    - Falsy or malformed contractor IDs fail closed without fallback or stripping.
    """
    from app.config import settings

    valid_cid = validate_token_string(contractor_id, name="contractor_id")
    assert valid_cid is not None

    if type(provider) is not str or provider not in VALID_PROVIDERS:
        raise IntegrationTokenEnvelopeError(f"Invalid provider: {provider}")

    if envelope_required is not None:
        if type(envelope_required) is not bool:
            raise IntegrationTokenEnvelopeError("envelope_required must be an exact bool")

    if encrypted_writes_enabled is not None:
        if type(encrypted_writes_enabled) is not bool:
            raise IntegrationTokenEnvelopeError("encrypted_writes_enabled must be an exact bool")
        effective_writes_enabled = encrypted_writes_enabled
    else:
        raw_flag = getattr(settings, "integration_token_encrypted_writes_enabled", False)
        if type(raw_flag) is not bool:
            raise IntegrationTokenEnvelopeError("settings.integration_token_encrypted_writes_enabled must be an exact bool")
        effective_writes_enabled = raw_flag

    if keyring is not None:
        _validate_keyring_dict(keyring)

    if active_key_version is not None:
        effective_active_ver = parse_active_key_version(active_key_version)
    else:
        effective_active_ver = get_configured_active_key_version()

    has_access_dict = type(stored_access) is dict
    has_refresh_dict = type(stored_refresh) is dict

    if has_access_dict or has_refresh_dict:
        if not (has_access_dict and has_refresh_dict):
            raise IntegrationTokenEnvelopeError(
                "One-sided or mixed envelope representation: both access and refresh tokens must be valid envelopes"
            )

        if keyring is None:
            keyring = get_configured_keyring()
        if not keyring:
            raise IntegrationTokenConfigError("Keyring is empty or unconfigured for envelope decryption")

        # Decrypt both to verify validity, authenticity, and historical key presence
        decrypt_integration_token(
            stored_access,
            contractor_id=valid_cid,
            provider=provider,
            token_kind="access",
            keyring=keyring,
        )
        decrypt_integration_token(
            stored_refresh,
            contractor_id=valid_cid,
            provider=provider,
            token_kind="refresh",
            keyring=keyring,
        )

        # Verify active key version is present in keyring for writing new envelopes
        if effective_active_ver is None or effective_active_ver not in keyring:
            raise IntegrationTokenConfigError("Active key version not present in keyring for envelope write")

        return "envelope"

    if envelope_required is True:
        if stored_access is None and stored_refresh is None:
            if keyring is None:
                keyring = get_configured_keyring()
            if not keyring or effective_active_ver is None or effective_active_ver not in keyring:
                raise IntegrationTokenConfigError("Encryption configuration invalid for required envelope writes")
            return "envelope"

        if type(stored_access) is str and type(stored_refresh) is str:
            raise IntegrationTokenEnvelopeError(
                "Conflicted credential downgrade attempt: provider requires encrypted envelope representation"
            )

    if stored_access is None and stored_refresh is None:
        if effective_writes_enabled:
            if keyring is None:
                keyring = get_configured_keyring()
            if not keyring or effective_active_ver is None or effective_active_ver not in keyring:
                raise IntegrationTokenConfigError("Encryption configuration invalid for encrypted writes")
            return "envelope"
        return "plaintext"

    if type(stored_access) is str and type(stored_refresh) is str:
        validate_token_string(stored_access, name=f"{provider}_access_token")
        validate_token_string(stored_refresh, name=f"{provider}_refresh_token")
        if effective_writes_enabled:
            if keyring is None:
                keyring = get_configured_keyring()
            if not keyring or effective_active_ver is None or effective_active_ver not in keyring:
                raise IntegrationTokenConfigError("Encryption configuration invalid for encrypted writes")
            return "envelope"
        return "plaintext"

    raise IntegrationTokenEnvelopeError("Invalid, one-sided, or mixed stored credentials representation")
