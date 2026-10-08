"""Unit tests for Jobber webhook raw-body signature verifier.

Tests include:
- RFC 4231 independently pinned known-answer HMAC-SHA256 fixtures (Test Case 2, 1, 4).
- Exact raw body byte matching with whitespace/body tampering and wrong key rejection.
- Absence of JSON normalization (reordering keys or adding spaces fails signature check).
- Strict canonical standard Base64 validation (rejecting non-canonical bits, URL-safe chars,
  whitespace, bad length, non-ASCII chars).
- Type safety and boundary validation for body (1..65536 bytes) and secret (1..4096 UTF-8 bytes).
- Raw binary and multilingual Unicode payloads.
- Zero logging and zero JSON parser invocation verification.
- Constant-time comparison spy proving `hmac.compare_digest` is called with 32-byte digests.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
from unittest import mock

import pytest

from app.services.jobber_webhook_auth import (
    JOBBER_SIGNATURE_BASE64_LENGTH,
    JOBBER_SIGNATURE_DIGEST_BYTES,
    MAX_JOBBER_WEBHOOK_BODY_BYTES,
    MAX_JOBBER_WEBHOOK_SECRET_BYTES,
    MIN_JOBBER_WEBHOOK_BODY_BYTES,
    MIN_JOBBER_WEBHOOK_SECRET_BYTES,
    verify_jobber_webhook_signature,
)


def test_rfc4231_test_case_2_independently_pinned_vector() -> None:
    """Verify RFC 4231 Test Case 2 known-answer vector against independently pinned constants."""
    # RFC 4231 Test Case 2:
    # Key: "Jefe"
    # Data: "what do ya want for nothing?"
    # Digest: 5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843
    key = "Jefe"
    message = b"what do ya want for nothing?"
    expected_hex = "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    expected_b64 = "W9zBRr9gdU5qBCQmCJV1x1oAPwidJzmDnexYuWTsOEM="

    # Sanity check pinned constants match RFC 4231
    assert binascii.hexlify(base64.b64decode(expected_b64)).decode("ascii") == expected_hex
    assert len(expected_b64) == JOBBER_SIGNATURE_BASE64_LENGTH

    # Positive verification
    assert verify_jobber_webhook_signature(message, expected_b64, key) is True

    # Rejection on wrong key
    assert verify_jobber_webhook_signature(message, expected_b64, "jefe") is False
    assert verify_jobber_webhook_signature(message, expected_b64, "Jefe ") is False
    assert verify_jobber_webhook_signature(message, expected_b64, "wrong_secret") is False

    # Rejection on modified message
    assert verify_jobber_webhook_signature(b"what do ya want for nothing!", expected_b64, key) is False
    assert verify_jobber_webhook_signature(message + b" ", expected_b64, key) is False


def test_additional_rfc4231_pinned_vectors() -> None:
    """Verify additional RFC 4231 test vectors (Test Case 1 and Test Case 4)."""
    # RFC 4231 Test Case 1:
    # Key: 20 bytes of 0x0b
    # Data: "Hi There"
    # Digest: b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7
    key_tc1 = "\x0b" * 20
    data_tc1 = b"Hi There"
    sig_tc1 = "sDRMYdjbOFNcqK/OrwvxK4gdwgDJgz2nJuk3bC4yz/c="
    assert binascii.hexlify(base64.b64decode(sig_tc1)).decode("ascii") == "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
    assert verify_jobber_webhook_signature(data_tc1, sig_tc1, key_tc1) is True

    # RFC 4231 Test Case 4:
    # Key: 25 bytes (0x01..0x19)
    # Data: 50 bytes of 0xcd
    # Digest: 82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b
    key_tc4 = "".join(chr(i) for i in range(1, 26))
    data_tc4 = bytes([0xCD] * 50)
    sig_tc4 = "glWKOJpEPA6kzIGYmfIIOoXw+qPlePgHei4/9GcpZls="
    assert binascii.hexlify(base64.b64decode(sig_tc4)).decode("ascii") == "82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b"
    assert verify_jobber_webhook_signature(data_tc4, sig_tc4, key_tc4) is True


def test_whitespace_and_body_tampering_rejection() -> None:
    """Verify that body tampering, whitespace modification, or JSON reformatting fails verification."""
    secret = "jobber_oauth_client_secret_xyz"
    raw_payload = b'{"account_id":"acc_123","event":"job_create"}'
    # Pinned signature for (secret, raw_payload)
    # HMAC-SHA256 digest hex: 3219283a7e8673eb60752ebaceba72600aa88f268a93d04d49b1ea02e15c6fc9
    valid_sig = "MhkoOn6Gc+tgdS66zrpyYAqojyaKk9BNSbHqAuFcb8k="

    # Positive baseline
    assert verify_jobber_webhook_signature(raw_payload, valid_sig, secret) is True

    # Whitespace changes around body
    assert verify_jobber_webhook_signature(raw_payload + b" ", valid_sig, secret) is False
    assert verify_jobber_webhook_signature(b" " + raw_payload, valid_sig, secret) is False
    assert verify_jobber_webhook_signature(raw_payload + b"\n", valid_sig, secret) is False
    assert verify_jobber_webhook_signature(raw_payload + b"\r\n", valid_sig, secret) is False

    # Bit flip and byte changes
    tampered_bytes = bytearray(raw_payload)
    tampered_bytes[10] ^= 0x01
    assert verify_jobber_webhook_signature(bytes(tampered_bytes), valid_sig, secret) is False

    # Proves no JSON normalization before verification:
    # 1. Added inner whitespace
    assert (
        verify_jobber_webhook_signature(
            b'{"account_id": "acc_123", "event": "job_create"}',
            valid_sig,
            secret,
        )
        is False
    )
    # 2. Reordered keys
    assert (
        verify_jobber_webhook_signature(
            b'{"event":"job_create","account_id":"acc_123"}',
            valid_sig,
            secret,
        )
        is False
    )
    # 3. Pretty-printed JSON
    assert (
        verify_jobber_webhook_signature(
            b'{\n  "account_id": "acc_123",\n  "event": "job_create"\n}',
            valid_sig,
            secret,
        )
        is False
    )


def test_signature_format_and_canonical_base64_validation() -> None:
    """Verify strict standard Base64 validation and rejection of malformed/non-canonical signatures."""
    secret = "Jefe"
    message = b"what do ya want for nothing?"
    valid_sig = "W9zBRr9gdU5qBCQmCJV1x1oAPwidJzmDnexYuWTsOEM="

    # Empty / wrong length signatures
    assert verify_jobber_webhook_signature(message, "", secret) is False
    assert verify_jobber_webhook_signature(message, valid_sig[:43], secret) is False
    assert verify_jobber_webhook_signature(message, valid_sig + "=", secret) is False
    assert verify_jobber_webhook_signature(message, valid_sig + "A", secret) is False
    assert verify_jobber_webhook_signature(message, "A" * 32, secret) is False
    assert verify_jobber_webhook_signature(message, "A" * 64, secret) is False

    # Whitespace in signature
    assert verify_jobber_webhook_signature(message, " " + valid_sig[1:], secret) is False
    assert verify_jobber_webhook_signature(message, valid_sig[:-1] + " ", secret) is False
    assert verify_jobber_webhook_signature(message, f" {valid_sig}", secret) is False
    assert verify_jobber_webhook_signature(message, f"{valid_sig}\n", secret) is False

    # Non-base64 characters
    assert verify_jobber_webhook_signature(message, valid_sig[:-2] + "!=", secret) is False
    assert verify_jobber_webhook_signature(message, valid_sig[:-2] + "@=", secret) is False

    # URL-safe base64 rejection (e.g. '-' or '_' substituted for '+' or '/')
    std_sig = "+//++//++//++//++//++//++//++//++//++//+AAA="
    url_sig = "-__--__--__--__--__--__--__--__--__--__-AAA="
    assert len(std_sig) == 44 and len(url_sig) == 44
    assert verify_jobber_webhook_signature(message, url_sig, secret) is False

    # Non-canonical base64 unused bits rejection
    # The pinned valid RFC signature ends with 'OEM=' (M = 001100). Changing M to N (001101) preserves
    # the decoded 32 digest bytes while mutating the unused padding bits.
    non_canonical_sig = valid_sig[:-2] + "N="
    assert base64.b64decode(non_canonical_sig, validate=True) == base64.b64decode(valid_sig, validate=True)
    assert verify_jobber_webhook_signature(message, non_canonical_sig, secret) is False

    # Non-ASCII Unicode string in signature
    unicode_sig = "W9zBRr9gdU5qBCQmCJV1x1oAPwidJzmDnexYuWTsOE\u00e9="
    assert verify_jobber_webhook_signature(message, unicode_sig, secret) is False


def test_invalid_types_rejected_safely() -> None:
    """Verify non-bytes body, non-str signature, and non-str secret safely return False."""
    valid_body = b"what do ya want for nothing?"
    valid_sig = "W9zBRr9gdU5qBCQmCJV1x1oAPwidJzmDnexYuWTsOEM="
    valid_secret = "Jefe"

    # Invalid body types
    for bad_body in [None, "string_body", 12345, [valid_body], {"body": valid_body}, bytearray(valid_body)]:
        assert verify_jobber_webhook_signature(bad_body, valid_sig, valid_secret) is False

    # Invalid signature types
    for bad_sig in [None, 12345, valid_sig.encode("ascii"), [valid_sig], {"sig": valid_sig}]:
        assert verify_jobber_webhook_signature(valid_body, bad_sig, valid_secret) is False

    # Invalid secret types
    for bad_secret in [None, 12345, valid_secret.encode("utf-8"), [valid_secret], {"secret": valid_secret}]:
        assert verify_jobber_webhook_signature(valid_body, valid_sig, bad_secret) is False


def test_body_size_limits_boundaries() -> None:
    """Verify body length boundaries: 0 (rejected), 1 (valid), 65536 (valid), 65537 (rejected)."""
    secret = "test_secret"

    # Boundary 0 bytes (empty body rejected)
    assert verify_jobber_webhook_signature(b"", "A" * 44, secret) is False

    # Boundary 1 byte (min limit accepted with valid signature)
    body_1 = b"a"
    sig_1 = "oUiov7J8qju9hIk4iQ2tkiNqUQtbbV9fPDTiEOhUF4U="
    assert len(body_1) == MIN_JOBBER_WEBHOOK_BODY_BYTES
    assert verify_jobber_webhook_signature(body_1, sig_1, secret) is True
    assert verify_jobber_webhook_signature(body_1, sig_1, "wrong_secret") is False

    # Boundary 65536 bytes (max limit accepted with valid signature)
    body_max = b"x" * MAX_JOBBER_WEBHOOK_BODY_BYTES
    sig_max = "Y1UPELLPEYOMydNYlqgt6E8OoAI+/HsTZm5FYmy82LA="
    assert len(body_max) == 65536
    assert verify_jobber_webhook_signature(body_max, sig_max, secret) is True

    # Boundary 65537 bytes (oversized body rejected safely)
    body_oversized = b"x" * (MAX_JOBBER_WEBHOOK_BODY_BYTES + 1)
    assert len(body_oversized) == 65537
    assert verify_jobber_webhook_signature(body_oversized, sig_max, secret) is False


def test_secret_size_limits_and_unicode_boundaries() -> None:
    """Verify secret length boundaries: 0 (rejected), 1 byte (valid), 4096 bytes (valid), 4097 bytes (rejected)."""
    body = b"hello world"

    # Boundary 0 chars (empty secret rejected)
    assert verify_jobber_webhook_signature(body, "A" * 44, "") is False

    # Boundary 1 byte UTF-8 secret
    secret_1 = "k"
    sig_secret_1 = "Z+7cXVCFKqzQVcyUC1Lt3onrppsVkCsqmoJIPqtw0S0="
    assert len(secret_1.encode("utf-8")) == MIN_JOBBER_WEBHOOK_SECRET_BYTES
    assert verify_jobber_webhook_signature(body, sig_secret_1, secret_1) is True

    # Boundary 4096 bytes UTF-8 secret (ASCII 1 byte per char)
    secret_4096 = "s" * MAX_JOBBER_WEBHOOK_SECRET_BYTES
    sig_secret_4096 = "aNrv04sxLnhxAcDKn/XQ6m2xf625sbSWj3xNg3WJ8qE="
    assert len(secret_4096.encode("utf-8")) == 4096
    assert verify_jobber_webhook_signature(body, sig_secret_4096, secret_4096) is True

    # Boundary 4097 bytes UTF-8 secret (rejected)
    secret_4097 = "s" * (MAX_JOBBER_WEBHOOK_SECRET_BYTES + 1)
    assert verify_jobber_webhook_signature(body, sig_secret_4096, secret_4097) is False

    # Boundary 4096 bytes multi-byte UTF-8 secret (2048 chars * 2 bytes = 4096 bytes)
    secret_mb_4096 = "\u00e9" * 2048
    assert len(secret_mb_4096.encode("utf-8")) == 4096
    sig_mb_4096 = "ycBGxi/fSjpMm0TZRcSCoV+AmZd0El61nvd6CulSx2k="
    assert verify_jobber_webhook_signature(body, sig_mb_4096, secret_mb_4096) is True

    # Boundary 4098 bytes multi-byte UTF-8 secret (2049 chars * 2 bytes = 4098 bytes -> rejected)
    secret_mb_4098 = "\u00e9" * 2049
    assert len(secret_mb_4098.encode("utf-8")) == 4098
    assert verify_jobber_webhook_signature(body, sig_mb_4096, secret_mb_4098) is False

    # Malformed Unicode surrogate in secret (cannot be encoded to UTF-8)
    surrogate_secret = "\ud800"
    assert verify_jobber_webhook_signature(body, sig_secret_1, surrogate_secret) is False


def test_raw_unicode_and_binary_payloads() -> None:
    """Verify raw body bytes are processed verbatim for UTF-8 and arbitrary binary payloads."""
    secret = "secret_key_123"

    # Multilingual UTF-8 payload with emoji and quotes
    unicode_payload = '{"caller": "José \U0001f527", "action": "quote_request"}'.encode("utf-8")
    sig_unicode = "3jiXUXHGwTMcfKcQXucoFhUQNG/zFU8CvNXEjE1pMvY="
    assert verify_jobber_webhook_signature(unicode_payload, sig_unicode, secret) is True

    # Raw binary payload spanning byte range 0..255
    bin_secret = "binary_secret"
    bin_payload = bytes(range(256))
    sig_bin = "idlZpcIa5s6IiE1Luy9OH89xGzTssNTJDTTA1c51syU="
    assert verify_jobber_webhook_signature(bin_payload, sig_bin, bin_secret) is True


def test_no_logging_and_no_json_dependency(caplog: pytest.LogCaptureFixture) -> None:
    """Verify no log messages are emitted and json module functions are not invoked."""
    caplog.set_level(logging.DEBUG)

    with mock.patch("json.loads", side_effect=AssertionError("json.loads must not be called")), mock.patch(
        "json.dumps", side_effect=AssertionError("json.dumps must not be called")
    ):
        # Run across valid and invalid calls
        res_valid = verify_jobber_webhook_signature(
            b"what do ya want for nothing?",
            "W9zBRr9gdU5qBCQmCJV1x1oAPwidJzmDnexYuWTsOEM=",
            "Jefe",
        )
        res_invalid = verify_jobber_webhook_signature(
            b"tampered",
            "W9zBRr9gdU5qBCQmCJV1x1oAPwidJzmDnexYuWTsOEM=",
            "Jefe",
        )
        res_malformed = verify_jobber_webhook_signature(
            None,
            "bad_sig",
            123,
        )

    assert res_valid is True
    assert res_invalid is False
    assert res_malformed is False
    # Verify no log records were emitted
    assert len(caplog.records) == 0


def test_compare_digest_spy_proves_constant_time_primitive_used() -> None:
    """Verify that hmac.compare_digest is called with 32-byte digests when inputs are structurally valid."""
    secret = "Jefe"
    message = b"what do ya want for nothing?"
    valid_sig = "W9zBRr9gdU5qBCQmCJV1x1oAPwidJzmDnexYuWTsOEM="

    with mock.patch("hmac.compare_digest", wraps=hmac.compare_digest) as spy_compare:
        # Case 1: Matching signature -> compare_digest called with two 32-byte digests
        result = verify_jobber_webhook_signature(message, valid_sig, secret)
        assert result is True
        assert spy_compare.call_count == 1
        arg1, arg2 = spy_compare.call_args[0]
        assert isinstance(arg1, bytes) and len(arg1) == JOBBER_SIGNATURE_DIGEST_BYTES
        assert isinstance(arg2, bytes) and len(arg2) == JOBBER_SIGNATURE_DIGEST_BYTES

        spy_compare.reset_mock()

        # Case 2: Mismatched valid base64 signature -> compare_digest called and returns False
        mismatched_sig = "A" * 43 + "="
        result_mismatch = verify_jobber_webhook_signature(message, mismatched_sig, secret)
        assert result_mismatch is False
        assert spy_compare.call_count == 1

        spy_compare.reset_mock()

        # Case 3: Structurally invalid inputs -> pre-validation rejects before compare_digest
        result_invalid = verify_jobber_webhook_signature(b"msg", "short_sig", secret)
        assert result_invalid is False
        assert spy_compare.call_count == 0
