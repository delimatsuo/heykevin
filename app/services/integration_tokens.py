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
import hashlib
import json
import math
import re
import secrets
from typing import Any

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
_CANONICAL_VERSION_REGEX = re.compile(r"^[1-9][0-9]*\Z")

OPERATION_INTENT_BASE_KEYS = frozenset({
    "operation_intent_id",
    "operation_intent_kind",
    "operation_intent_phase",
    "operation_intent_expires_at",
    "operation_intent_acquired_at",
    "operation_intent_generation",
    "operation_intent_lifecycle_epoch",
    "operation_intent_credentials_fingerprint",
})

LEGACY_CLAIM_BASE_KEYS = frozenset({
    "refresh_claim_id",
    "refresh_claim_phase",
    "refresh_claim_expires_at",
    "refresh_claim_generation",
    "refresh_phase",
    "refresh_lease_expires_at",
    "refresh_in_progress",
    "token_quarantine",
    "token_quarantine_reason",
    "unknown_outcome_claim_id",
})

REAUTHORIZATION_ATTEMPT_BASE_KEYS = frozenset({
    "reauthorization_attempt_id",
    "reauthorization_attempt_kind",
    "reauthorization_attempt_phase",
    "reauthorization_attempt_expires_at",
    "reauthorization_attempt_acquired_at",
    "reauthorization_attempt_generation",
    "reauthorization_attempt_lifecycle_epoch",
    "reauthorization_attempt_credentials_fingerprint",
})

QUARANTINE_BASE_KEYS = frozenset({
    "refresh_outcome_unknown",
    "reauthorization_required",
})

VALID_INTENT_KINDS = frozenset({"business", "refresh", "connect", "reconnect"})
VALID_INTENT_PHASES = frozenset({"reserved", "provider_request_started"})
_CANONICAL_STATE_REGEX = re.compile(r"^[0-9a-zA-Z_-]{1,128}\Z")


def get_provider_operation_intent_keys(provider: str) -> frozenset[str]:
    """Return all operation-intent, legacy claim, quarantine, and reauthorization attempt field names for a provider."""
    keys = {f"{provider}_{k}" for k in OPERATION_INTENT_BASE_KEYS}
    keys.update({f"{provider}_{k}" for k in LEGACY_CLAIM_BASE_KEYS})
    keys.update({f"{provider}_{k}" for k in QUARANTINE_BASE_KEYS})
    keys.update({f"{provider}_{k}" for k in REAUTHORIZATION_ATTEMPT_BASE_KEYS})
    return frozenset(keys)


def get_provider_reauthorization_attempt_keys(provider: str) -> frozenset[str]:
    """Return all reauthorization attempt field names for a provider."""
    return frozenset({f"{provider}_{k}" for k in REAUTHORIZATION_ATTEMPT_BASE_KEYS})


def parse_provider_reauthorization_attempt(
    data: Any,
    provider: str,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Exact closed validator and parser for provider reauthorization attempt metadata.

    Returns:
    - ("absent", None, None): All reauthorization attempt keys are absent.
    - ("valid", parsed_dict, None): Exact valid reauthorization attempt schema.
    - ("malformed", None, error_msg): Partial fields, invalid types, hostile values, or invalid schema.
    """
    if type(data) is not dict or type(provider) is not str or provider not in VALID_PROVIDERS:
        return "malformed", None, "Invalid data dictionary or provider"

    all_attempt_keys = {f"{provider}_{k}" for k in REAUTHORIZATION_ATTEMPT_BASE_KEYS}
    present_attempt_keys = {k for k in all_attempt_keys if k in data}

    if not present_attempt_keys:
        return "absent", None, None

    if present_attempt_keys != all_attempt_keys:
        return "malformed", None, "Incomplete reauthorization attempt schema"

    k_id = f"{provider}_reauthorization_attempt_id"
    k_kind = f"{provider}_reauthorization_attempt_kind"
    k_phase = f"{provider}_reauthorization_attempt_phase"
    k_exp = f"{provider}_reauthorization_attempt_expires_at"
    k_acq = f"{provider}_reauthorization_attempt_acquired_at"
    k_gen = f"{provider}_reauthorization_attempt_generation"
    k_epoch = f"{provider}_reauthorization_attempt_lifecycle_epoch"
    k_fp = f"{provider}_reauthorization_attempt_credentials_fingerprint"

    raw_id = data[k_id]
    if type(raw_id) is not str or type(raw_id) is bool or not _CANONICAL_STATE_REGEX.fullmatch(raw_id):
        return "malformed", None, "Invalid reauthorization_attempt_id"

    raw_kind = data[k_kind]
    if type(raw_kind) is not str or raw_kind != "reconnect":
        return "malformed", None, "Invalid reauthorization_attempt_kind"

    raw_phase = data[k_phase]
    if type(raw_phase) is not str or raw_phase not in VALID_INTENT_PHASES:
        return "malformed", None, "Invalid reauthorization_attempt_phase"

    raw_exp = data[k_exp]
    if type(raw_exp) is not float or not math.isfinite(raw_exp) or raw_exp <= 0.0:
        return "malformed", None, "Invalid reauthorization_attempt_expires_at"

    raw_acq = data[k_acq]
    if type(raw_acq) is not float or not math.isfinite(raw_acq) or raw_acq <= 0.0 or raw_acq > raw_exp:
        return "malformed", None, "Invalid reauthorization_attempt_acquired_at"

    raw_gen = data[k_gen]
    if type(raw_gen) is not int or type(raw_gen) is bool or not (0 <= raw_gen <= MAX_KEY_VERSION):
        return "malformed", None, "Invalid reauthorization_attempt_generation"

    raw_epoch = data[k_epoch]
    if type(raw_epoch) is not int or type(raw_epoch) is bool or not (0 <= raw_epoch <= MAX_KEY_VERSION):
        return "malformed", None, "Invalid reauthorization_attempt_lifecycle_epoch"

    raw_fp = data[k_fp]
    if type(raw_fp) is not str or type(raw_fp) is bool or not re.fullmatch(r"^[0-9a-f]{64}$", raw_fp):
        return "malformed", None, "Invalid reauthorization_attempt_credentials_fingerprint"

    parsed = {
        "id": raw_id,
        "kind": raw_kind,
        "phase": raw_phase,
        "expires_at": raw_exp,
        "acquired_at": raw_acq,
        "generation": raw_gen,
        "lifecycle_epoch": raw_epoch,
        "credentials_fingerprint": raw_fp,
        "is_reauthorization_attempt": True,
    }
    return "valid", parsed, None


def parse_provider_operation_intent(
    data: Any,
    provider: str,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Exact closed validator and parser for provider operation intent metadata.

    Returns:
    - ("absent", None, None): All canonical, legacy, quarantine, and attempt intent keys are absent.
    - ("valid", parsed_dict, None): Exact valid canonical or legacy intent schema.
    - ("quarantined", quarantine_dict, None): Durable unknown-outcome quarantine record present (clean quarantine).
    - ("quarantined_reauthorizing", attempt_dict, None): Durable quarantine record present with valid reauthorization attempt.
    - ("malformed", None, error_msg): Partial fields, invalid types, hostile values, or alias mismatches.
    """
    if type(data) is not dict or type(provider) is not str or provider not in VALID_PROVIDERS:
        return "malformed", None, "Invalid data dictionary or provider"

    # Collect present keys across all categories
    all_intent_keys = {f"{provider}_{k}" for k in OPERATION_INTENT_BASE_KEYS}
    all_legacy_keys = {f"{provider}_{k}" for k in LEGACY_CLAIM_BASE_KEYS}
    all_attempt_keys = {f"{provider}_{k}" for k in REAUTHORIZATION_ATTEMPT_BASE_KEYS}

    present_intent_keys = {k for k in all_intent_keys if k in data}
    present_legacy_keys = {k for k in all_legacy_keys if k in data}
    present_attempt_keys = {k for k in all_attempt_keys if k in data}

    # 1. Check quarantine keys
    k_outcome = f"{provider}_refresh_outcome_unknown"
    k_reauth = f"{provider}_reauthorization_required"
    outcome_present = k_outcome in data
    reauth_present = k_reauth in data

    if outcome_present or reauth_present:
        # Both quarantine fields must be present together
        if not (outcome_present and reauth_present):
            return "malformed", None, "Partial quarantine fields: both outcome_unknown and reauthorization_required must be present"

        outcome_val = data[k_outcome]
        reauth_val = data[k_reauth]

        if type(outcome_val) is not bool or type(reauth_val) is not bool:
            return "malformed", None, "Quarantine fields must be exact built-in booleans"

        if outcome_val is not True or reauth_val is not True:
            return "malformed", None, "Quarantine boolean fields must both be exact True"

        if present_legacy_keys:
            return "malformed", None, "Quarantine fields cannot coexist with legacy claim fields"

        if present_intent_keys:
            return "malformed", None, "Ordinary intent kinds cannot coexist with quarantine"

        if present_attempt_keys:
            att_st, att_parsed, att_err = parse_provider_reauthorization_attempt(data, provider)
            if att_st == "valid":
                return "quarantined_reauthorizing", att_parsed, None
            return "malformed", None, "Malformed reauthorization attempt under quarantine"

        return "quarantined", {
            "outcome_unknown": True,
            "reauthorization_required": True,
        }, None

    if present_attempt_keys:
        return "malformed", None, "Reauthorization attempt fields present without quarantine"

    if not present_intent_keys and not present_legacy_keys:
        return "absent", None, None

    k_id = f"{provider}_operation_intent_id"
    k_kind = f"{provider}_operation_intent_kind"
    k_phase = f"{provider}_operation_intent_phase"
    k_exp = f"{provider}_operation_intent_expires_at"
    k_acq = f"{provider}_operation_intent_acquired_at"
    k_gen = f"{provider}_operation_intent_generation"
    k_epoch = f"{provider}_operation_intent_lifecycle_epoch"
    k_fp = f"{provider}_operation_intent_credentials_fingerprint"

    k_leg_id = f"{provider}_refresh_claim_id"
    k_leg_phase = f"{provider}_refresh_claim_phase"
    k_leg_exp = f"{provider}_refresh_claim_expires_at"
    k_leg_gen = f"{provider}_refresh_claim_generation"

    # Disallow unmanaged legacy fields
    disallowed_legacy = present_legacy_keys - {k_leg_id, k_leg_phase, k_leg_exp, k_leg_gen}
    if disallowed_legacy:
        return "malformed", None, "Disallowed legacy intent keys present"

    # Case A: Canonical intent schema present
    if k_id in present_intent_keys:
        required_canonical = {k_id, k_kind, k_phase, k_exp, k_acq, k_gen, k_epoch, k_fp}
        if not required_canonical.issubset(present_intent_keys):
            return "malformed", None, "Missing required canonical intent fields"

        raw_id = data[k_id]
        if type(raw_id) is not str or type(raw_id) is bool or not _CANONICAL_STATE_REGEX.fullmatch(raw_id):
            return "malformed", None, "Invalid operation_intent_id"

        raw_kind = data[k_kind]
        if type(raw_kind) is not str or raw_kind not in VALID_INTENT_KINDS:
            return "malformed", None, "Invalid operation_intent_kind"

        raw_phase = data[k_phase]
        if type(raw_phase) is not str or raw_phase not in VALID_INTENT_PHASES:
            return "malformed", None, "Invalid operation_intent_phase"

        raw_exp = data[k_exp]
        if type(raw_exp) is not float or not math.isfinite(raw_exp) or raw_exp <= 0.0:
            return "malformed", None, "Invalid operation_intent_expires_at: must be finite positive float"

        raw_acq = data[k_acq]
        if type(raw_acq) is not float or not math.isfinite(raw_acq) or raw_acq <= 0.0 or raw_acq > raw_exp:
            return "malformed", None, "Invalid operation_intent_acquired_at: must be finite positive float <= expires_at"

        raw_gen = data[k_gen]
        if type(raw_gen) is not int or type(raw_gen) is bool or not (0 <= raw_gen <= MAX_KEY_VERSION):
            return "malformed", None, "Invalid operation_intent_generation"

        raw_epoch = data[k_epoch]
        if type(raw_epoch) is not int or type(raw_epoch) is bool or not (0 <= raw_epoch <= MAX_KEY_VERSION):
            return "malformed", None, "Invalid operation_intent_lifecycle_epoch"

        raw_fp = data[k_fp]
        if type(raw_fp) is not str or type(raw_fp) is bool or not re.fullmatch(r"^[0-9a-f]{64}$", raw_fp):
            return "malformed", None, "Invalid operation_intent_credentials_fingerprint"

        # Check paired legacy aliases if any are present
        if present_legacy_keys:
            leg_set = {k_leg_id, k_leg_phase, k_leg_exp, k_leg_gen}
            if present_legacy_keys != leg_set:
                return "malformed", None, "Incomplete paired legacy refresh claim fields"
            if raw_kind != "refresh":
                return "malformed", None, "Legacy refresh claim fields present on non-refresh intent"
            leg_id_val = data[k_leg_id]
            if type(leg_id_val) is not str or type(leg_id_val) is bool or leg_id_val != raw_id:
                return "malformed", None, "Legacy refresh claim id mismatch or non-str"
            leg_phase_val = data[k_leg_phase]
            if type(leg_phase_val) is not str or type(leg_phase_val) is bool or leg_phase_val != raw_phase:
                return "malformed", None, "Legacy refresh claim phase mismatch or non-str"
            leg_exp_val = data[k_leg_exp]
            if type(leg_exp_val) is not float or not math.isfinite(leg_exp_val) or leg_exp_val != raw_exp:
                return "malformed", None, "Legacy refresh claim expires_at mismatch or non-float"
            leg_gen_val = data[k_leg_gen]
            if type(leg_gen_val) is not int or type(leg_gen_val) is bool or leg_gen_val != raw_gen:
                return "malformed", None, "Legacy refresh claim generation mismatch or non-int"

        parsed = {
            "id": raw_id,
            "kind": raw_kind,
            "phase": raw_phase,
            "expires_at": raw_exp,
            "acquired_at": raw_acq,
            "generation": raw_gen,
            "lifecycle_epoch": raw_epoch,
            "credentials_fingerprint": raw_fp,
            "is_legacy": False,
        }
        return "valid", parsed, None

    # Case B: Pure legacy refresh claim (no canonical operation_intent_id)
    if present_legacy_keys:
        leg_set = {k_leg_id, k_leg_phase, k_leg_exp, k_leg_gen}
        if present_legacy_keys != leg_set or present_intent_keys:
            return "malformed", None, "Incomplete or mixed legacy refresh claim schema"

        raw_id = data[k_leg_id]
        if type(raw_id) is not str or type(raw_id) is bool or not _CANONICAL_STATE_REGEX.fullmatch(raw_id):
            return "malformed", None, "Invalid legacy refresh_claim_id"

        raw_phase = data[k_leg_phase]
        if type(raw_phase) is not str or raw_phase not in VALID_INTENT_PHASES:
            return "malformed", None, "Invalid legacy refresh_claim_phase"

        raw_exp = data[k_leg_exp]
        if type(raw_exp) is not float or not math.isfinite(raw_exp) or raw_exp <= 0.0:
            return "malformed", None, "Invalid legacy refresh_claim_expires_at"

        raw_gen = data[k_leg_gen]
        if type(raw_gen) is not int or type(raw_gen) is bool or not (0 <= raw_gen <= MAX_KEY_VERSION):
            return "malformed", None, "Invalid legacy refresh_claim_generation"

        parsed = {
            "id": raw_id,
            "kind": "refresh",
            "phase": raw_phase,
            "expires_at": raw_exp,
            "acquired_at": raw_exp - 60.0,
            "generation": raw_gen,
            "lifecycle_epoch": 0,
            "credentials_fingerprint": None,
            "is_legacy": True,
        }
        return "valid", parsed, None

    return "malformed", None, "Partial intent fields present"


def parse_bounded_counter(
    data: Any,
    key: str,
    *,
    default: int | None = None,
    allow_absent: bool = True,
    min_val: int = 0,
    max_val: int = MAX_KEY_VERSION,
) -> int | None:
    """Parse a presence-aware exact bounded counter from dictionary.

    - If data is not a dict or key is absent:
        - returns default if allow_absent is True
        - returns None if allow_absent is False
    - If key is present:
        - returns int if exact int (not bool) and min_val <= val <= max_val
        - otherwise (None, bool, float, str, negative, overflow, object) returns None (invalid).
    """
    if type(data) is not dict or key not in data:
        return default if allow_absent else None
    val = data[key]
    if type(val) is not int or type(val) is bool:
        return None
    if not (min_val <= val <= max_val):
        return None
    return val


def parse_durable_lifecycle_counters(
    data: Any,
    provider: str,
) -> tuple[bool, int, int, bool, str | None]:
    """Exact parser for provider lifecycle presence triples (connected, generation, lifecycle_epoch).

    Invariants:
    - (all 3 absent): returns (True, 0, 0, False, None) [valid legacy unnormalized]
    - (all 3 present): returns (True, gen, epoch, True, None) [if connected is bool, gen/epoch are exact bounded ints]
    - (any other combination of presence, non-bool connected, non-int gen/epoch, negative, overflow):
      returns (False, 0, 0, False, error_detail) [invalid partial/malformed lifecycle metadata]
    """
    if type(data) is not dict or type(provider) is not str or provider not in VALID_PROVIDERS:
        return False, 0, 0, False, "Invalid data dictionary or provider"

    k_conn = f"{provider}_connected"
    k_gen = f"{provider}_generation"
    k_epoch = f"{provider}_lifecycle_epoch"

    has_conn = k_conn in data
    has_gen = k_gen in data
    has_epoch = k_epoch in data

    lifecycle_count = int(has_conn) + int(has_gen) + int(has_epoch)
    if lifecycle_count == 0:
        return True, 0, 0, False, None

    if lifecycle_count == 3:
        v_conn = data[k_conn]
        if type(v_conn) is not bool:
            return False, 0, 0, False, "Malformed connected field: must be exact bool"

        v_gen = data[k_gen]
        if type(v_gen) is not int or type(v_gen) is bool or not (0 <= v_gen <= MAX_KEY_VERSION):
            return False, 0, 0, False, "Malformed generation field: must be exact non-negative bounded int"

        v_epoch = data[k_epoch]
        if type(v_epoch) is not int or type(v_epoch) is bool or not (0 <= v_epoch <= MAX_KEY_VERSION):
            return False, 0, 0, False, "Malformed lifecycle epoch field: must be exact non-negative bounded int"

        return True, v_gen, v_epoch, True, None

    return False, 0, 0, False, "Invalid partial lifecycle metadata"



def _validate_canonical_base64(
    raw: Any,
    *,
    name: str,
    expected_len: int | None = None,
    min_len: int | None = None,
    max_len: int | None = None,
) -> bytes:
    """Validate that raw is an exact str, non-empty, strictly canonical standard base64."""
    if type(raw) is not str:
        raise IntegrationTokenEnvelopeError(f"{name} must be an exact base64 str")
    if len(raw) == 0:
        raise IntegrationTokenEnvelopeError(f"{name} cannot be empty")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        raise IntegrationTokenEnvelopeError(f"Invalid base64 encoding in {name}") from None
    if base64.b64encode(decoded).decode("ascii") != raw:
        raise IntegrationTokenEnvelopeError(f"Non-canonical base64 encoding in {name}")
    if expected_len is not None and len(decoded) != expected_len:
        raise IntegrationTokenEnvelopeError(f"{name} must decode to exactly {expected_len} bytes")
    if min_len is not None and max_len is not None and not (min_len <= len(decoded) <= max_len):
        raise IntegrationTokenEnvelopeError(
            f"{name} length {len(decoded)} outside valid range {min_len}..{max_len}"
        )
    return decoded


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


class IntegrationTokenContractorNotFound(IntegrationTokenCASConflict):
    """Raised when contractor document is absent during a CAS mutation."""


def validate_token_string(
    val: Any,
    *,
    name: str = "token",
    allow_none: bool = False,
) -> str | None:
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


def validate_token_expires_in(val: Any) -> float | None:
    """Validate that token expires_in duration is an exact built-in int or float in range 1..31536000."""
    if val is None:
        return None
    if type(val) not in (int, float) or type(val) is bool:
        raise IntegrationTokenEnvelopeError("Invalid expires_in duration")
    val_f = float(val)
    if not math.isfinite(val_f) or not (1.0 <= val_f <= 31536000.0):
        raise IntegrationTokenEnvelopeError("Invalid expires_in duration")
    return val_f


def validate_token_expires_at(val: Any) -> float | None:
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
            if type(v_a) is bool:
                if v_a is not v_b:
                    return False
            elif type(v_a) is int or type(v_a) is str or type(v_a) is bytes:
                if v_a != v_b:
                    return False
            elif type(v_a) is float:
                if not math.isfinite(v_a) or not math.isfinite(v_b) or v_a != v_b:
                    return False
            else:
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
    if type(raw_json) is not str or len(raw_json.strip()) == 0:
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

    if type(parsed) is not dict or len(parsed) == 0:
        raise IntegrationTokenConfigError("Keyring JSON must be a non-empty object")

    keyring: dict[int, bytes] = {}
    for k, v in parsed.items():
        if type(k) is not str or not _CANONICAL_VERSION_REGEX.fullmatch(k):
            raise IntegrationTokenConfigError(
                "Keyring key must be a canonical positive integer string with no leading zeroes"
            )
        k_int = int(k)
        if not (MIN_KEY_VERSION <= k_int <= MAX_KEY_VERSION):
            raise IntegrationTokenConfigError(
                "Keyring version key must be in range 1..2147483647"
            )
        if k_int in keyring:
            raise IntegrationTokenConfigError("Duplicate key version in keyring")
        if type(v) is not str or len(v) == 0:
            raise IntegrationTokenConfigError("Keyring key value must be a non-empty base64 string")

        try:
            key_bytes = _validate_canonical_base64(v, name="Keyring key material", expected_len=32)
        except IntegrationTokenEnvelopeError as exc:
            raise IntegrationTokenConfigError(str(exc)) from exc

        keyring[k_int] = key_bytes

    _validate_keyring_dict(keyring)
    return keyring


def parse_active_key_version(val: Any) -> int | None:
    """Parse and validate active key version setting."""
    if val is None:
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
        if len(val) == 0:
            return None
        if not _CANONICAL_VERSION_REGEX.fullmatch(val):
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


def get_configured_active_key_version() -> int | None:
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
    keyring: dict[int, bytes] | None = None,
    active_key_version: int | None = None,
    active_version: int | None = None,
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
    keyring: dict[int, bytes] | None = None,
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
    nonce_bytes = _validate_canonical_base64(nonce_str, name="Envelope nonce", expected_len=12)

    ct_str = envelope["ciphertext"]
    ct_bytes = _validate_canonical_base64(
        ct_str,
        name="Envelope ciphertext",
        min_len=MIN_CIPHERTEXT_BYTES,
        max_len=MAX_CIPHERTEXT_BYTES,
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
    keyring: dict[int, bytes] | None = None,
) -> str | None:
    """Attempt decryption of stored token; returns None if decryption or configuration fails."""
    try:
        val = decrypt_integration_token(
            stored,
            contractor_id=contractor_id,
            provider=provider,
            token_kind=token_kind,
            keyring=keyring,
        )
        return val if (type(val) is str and len(val) > 0) else None
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

    # Lifecycle triple check: all 3 absent (legacy) OR all 3 exact valid present
    lifecycle_ok, gen_val, epoch_val, lifecycle_present, _ = parse_durable_lifecycle_counters(contractor, provider)
    if not lifecycle_ok:
        return None, None

    if f"{provider}_connected" in contractor:
        if contractor[f"{provider}_connected"] is not True:
            return None, None

    # Intent / quarantine check: must be strictly absent (no valid intent, no quarantine, no malformed fields)
    intent_status, _, _ = parse_provider_operation_intent(contractor, provider)
    if intent_status != "absent":
        return None, None

    env_req_key = f"{provider}_token_envelope_required"
    if env_req_key in contractor:
        env_req = contractor[env_req_key]
        if type(env_req) is not bool:
            return None, None
    else:
        env_req = None

    if f"{provider}_access_token" not in contractor or f"{provider}_refresh_token" not in contractor:
        return None, None

    raw_access = contractor[f"{provider}_access_token"]
    raw_refresh = contractor[f"{provider}_refresh_token"]

    # Case A: Plaintext pair
    if type(raw_access) is str and type(raw_refresh) is str:
        if env_req is True:
            # Envelope required floor is active; plaintext pair is rejected
            return None, None
        try:
            valid_access = validate_token_string(raw_access, name=f"{provider}_access_token")
            valid_refresh = validate_token_string(raw_refresh, name=f"{provider}_refresh_token")
            if type(valid_access) is str and len(valid_access) > 0 and type(valid_refresh) is str and len(valid_refresh) > 0:
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
        if type(val_access) is str and len(val_access) > 0 and type(val_refresh) is str and len(val_refresh) > 0:
            return val_access, val_refresh
        return None, None

    # Mixed, one-sided, or non-str/non-dict types
    return None, None


def validate_envelope_structure(envelope: Any) -> None:
    """Strictly validate the structure, keys, types, and canonical base64 encodings of an envelope dictionary."""
    if type(envelope) is not dict:
        raise IntegrationTokenEnvelopeError("Envelope must be an exact dict")
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
    _validate_canonical_base64(nonce_str, name="Envelope nonce", expected_len=12)

    ct_str = envelope["ciphertext"]
    _validate_canonical_base64(
        ct_str,
        name="Envelope ciphertext",
        min_len=MIN_CIPHERTEXT_BYTES,
        max_len=MAX_CIPHERTEXT_BYTES,
    )


_validate_raw_envelope_dict = validate_envelope_structure


REQUIRED_GOOGLE_CALENDAR_SCOPES: frozenset[str] = frozenset({
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
})
CANONICAL_GOOGLE_CALENDAR_SCOPE: str = (
    "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.freebusy"
)


def validate_and_normalize_google_calendar_scope(
    scope_val: Any,
    *,
    allow_none: bool = True,
) -> tuple[bool, str | None]:
    """Pure validator/normalizer for Google Calendar OAuth response scope and stored scope.

    Required exact scopes:
    - https://www.googleapis.com/auth/calendar.events
    - https://www.googleapis.com/auth/calendar.freebusy

    A present response scope must:
    - Be an exact built-in str (type(scope_val) is str, not bool or subclass).
    - Not be empty, have no leading or trailing whitespace.
    - Use strict ASCII-space (' ', 0x20) token separation with no empty tokens (no consecutive spaces).
    - Every character in each token must be a visible ASCII character (0x21 <= ord(c) <= 0x7E),
      rejecting control characters, non-ASCII characters, tabs, newlines, and unicode whitespace.
    - Contain BOTH required scopes. Extra valid visible ASCII scopes are preserved.

    Returns (True, canonical_or_normalized_scope_str) on success.
    Returns (False, None) on invalid, reduced, or malformed scope.
    If scope_val is None:
    - If allow_none is True, returns (True, CANONICAL_GOOGLE_CALENDAR_SCOPE).
    - If allow_none is False, returns (False, None).
    """
    if scope_val is None:
        if allow_none:
            return True, CANONICAL_GOOGLE_CALENDAR_SCOPE
        return False, None

    if type(scope_val) is not str:
        return False, None

    if len(scope_val) == 0:
        return False, None

    tokens = scope_val.split(" ")
    for token in tokens:
        if type(token) is not str or len(token) == 0:
            return False, None
        for c in token:
            if not (33 <= ord(c) <= 126):
                return False, None

    token_set = set(tokens)
    if not REQUIRED_GOOGLE_CALENDAR_SCOPES.issubset(token_set):
        return False, None

    return True, scope_val


def compute_raw_credentials_fingerprint(
    raw_access: Any,
    raw_refresh: Any,
) -> str:
    """Compute deterministic SHA-256 fingerprint of raw stored credential pair.

    Exact-type-aware and safe for absent (None, None), plaintext (str, str), or
    exact envelope (dict, dict) raw representations.
    Malformed representations fail closed with IntegrationTokenEnvelopeError.
    """
    if raw_access is None and raw_refresh is None:
        canonical: dict[str, Any] = {"access": None, "refresh": None}
    elif type(raw_access) is str and type(raw_refresh) is str:
        valid_acc = validate_token_string(raw_access, name="access_token")
        valid_ref = validate_token_string(raw_refresh, name="refresh_token")
        canonical = {"access": valid_acc, "refresh": valid_ref}
    elif type(raw_access) is dict and type(raw_refresh) is dict:
        _validate_raw_envelope_dict(raw_access)
        _validate_raw_envelope_dict(raw_refresh)
        canonical = {"access": raw_access, "refresh": raw_refresh}
    else:
        raise IntegrationTokenEnvelopeError("Invalid or mixed raw credentials representation")

    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


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
    envelope_required: bool | None = None,
    keyring: dict[int, bytes] | None = None,
    active_key_version: int | None = None,
    encrypted_writes_enabled: bool | None = None,
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
