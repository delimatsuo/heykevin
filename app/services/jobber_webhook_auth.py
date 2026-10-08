"""Jobber webhook signature verification primitive.

Primary contract: https://developer.getjobber.com/docs/using_jobbers_api/setting_up_webhooks/
Validates X-Jobber-Hmac-SHA256 signatures over raw HTTP request bodies.

Security and architecture invariants:
- Unmounted authentication primitive: verifying a signature validates cryptographic
  authenticity against the shared OAuth client secret; it does NOT prove payload schema
  validity, grant applicability, replay protection, or durable acceptance.
- Caller responsibility:
  1. The caller MUST enforce request body size limits (e.g. 64 KiB) while streaming network
     input before passing the complete raw bytes to this verifier.
  2. The caller MUST perform separate schema validation, event parsing, account routing,
     app/grant applicability checks, durable receipt deduplication and separately established
     grant-applicability decisions, and durable transaction lifecycle handling.
- No JSON normalization or body modification: HMAC-SHA256 is computed over exact raw body
  bytes as received on the wire.
- Constant-time comparison: uses hmac.compare_digest to prevent timing side channels.
- Pure module with zero runtime receiver, endpoint activation, logging, or exception leaks.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from typing import Any

MAX_JOBBER_WEBHOOK_BODY_BYTES: int = 65536
MIN_JOBBER_WEBHOOK_BODY_BYTES: int = 1
MAX_JOBBER_WEBHOOK_SECRET_BYTES: int = 4096
MIN_JOBBER_WEBHOOK_SECRET_BYTES: int = 1
JOBBER_SIGNATURE_BASE64_LENGTH: int = 44
JOBBER_SIGNATURE_DIGEST_BYTES: int = 32


def verify_jobber_webhook_signature(
    raw_body: Any,
    signature: Any,
    client_secret: Any,
) -> bool:
    """Verify the X-Jobber-Hmac-SHA256 signature of a Jobber webhook raw body.

    Args:
        raw_body: Exact raw request payload bytes (1..65536 bytes).
        signature: The X-Jobber-Hmac-SHA256 header string (canonical standard Base64, len 44).
        client_secret: OAuth client secret string (1..4096 UTF-8 bytes).

    Returns:
        bool: True if signature matches HMAC-SHA256 of raw_body with client_secret;
              False on any mismatch, malformed input, invalid type, size violation, or error.
    """
    try:
        # Require exact bytes body of length 1..65536 inclusive
        if type(raw_body) is not bytes:
            return False
        if not (MIN_JOBBER_WEBHOOK_BODY_BYTES <= len(raw_body) <= MAX_JOBBER_WEBHOOK_BODY_BYTES):
            return False

        # Require exact str signature of canonical SHA-256 standard base64 length 44
        if type(signature) is not str or len(signature) != JOBBER_SIGNATURE_BASE64_LENGTH:
            return False

        # Require exact nonempty str secret whose UTF-8 representation is 1..4096 bytes
        if type(client_secret) is not str:
            return False
        secret_bytes = client_secret.encode("utf-8")
        if not (MIN_JOBBER_WEBHOOK_SECRET_BYTES <= len(secret_bytes) <= MAX_JOBBER_WEBHOOK_SECRET_BYTES):
            return False

        # Strict ASCII standard base64 decoding
        sig_ascii = signature.encode("ascii")
        decoded_sig = base64.b64decode(sig_ascii, validate=True)
        if len(decoded_sig) != JOBBER_SIGNATURE_DIGEST_BYTES:
            return False

        # Canonical standard base64 re-encoding verification (rejects non-canonical padding, unused bits, URL-safe)
        if base64.b64encode(decoded_sig).decode("ascii") != signature:
            return False

        # Compute HMAC-SHA256 over exact raw body bytes
        expected_digest = hmac.new(secret_bytes, raw_body, hashlib.sha256).digest()

        # Constant-time comparison
        return hmac.compare_digest(decoded_sig, expected_digest)
    except (binascii.Error, UnicodeError, ValueError, TypeError, AttributeError):
        return False
