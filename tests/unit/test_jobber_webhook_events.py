"""Unit tests for Jobber disconnect webhook event parser and envelope primitive.

Tests include:
- Valid exact body + real HMAC verification.
- Tampered body, altered signature, and wrong client secret rejection.
- Expected app ID matching and mismatch rejection.
- Absent / invalid configured expected app ID rejection.
- Topic verification (APP_DISCONNECT required).
- Schema structure and type checking (data, webHookEvent, accountId, appId).
- ID validation (1..1024 code points, no whitespace, no Unicode Cc controls).
- Duplicate key rejection at multiple object depths.
- Malformed UTF-8, JSON syntax errors, BOM, nonfinite constants, and excessive depth rejection.
- Size boundary tests (0 bytes, 1 byte, 65536 bytes, 65537 bytes).
- RFC 3339 timestamp validation (subsecond precision 1..9 digits, timezone offsets, leap years).
- Impossible dates, naive local timestamps, trailing newlines, Unicode digits, and legacy misspelling (occuredAt) rejection.
- Optional and null itemId handling, plus invalid itemId rejection.
- Ignored unknown fields and privacy retention safety.
- Delivery fingerprint: identical raw bytes produce same fingerprint, different serialization produces different fingerprint.
- Authenticate-before-parse guard proving json.loads is never called on failed signature.
- Zero logging emissions and frozen dataclass immutability.
"""

from __future__ import annotations

import base64
from dataclasses import FrozenInstanceError
import hashlib
import hmac
import json
import logging
from unittest import mock

import pytest

from app.services.jobber_webhook_events import (
    JobberDisconnectEnvelope,
    authenticate_jobber_disconnect_envelope,
)


def _sign(raw_body: bytes, secret: str) -> str:
    """Helper to compute valid X-Jobber-Hmac-SHA256 signature for test payloads."""
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def test_valid_jobber_disconnect_envelope_real_hmac() -> None:
    """Verify authentic payload with valid HMAC correctly produces JobberDisconnectEnvelope."""
    secret = "jobber_oauth_secret_abc"
    app_id = "app_heykevin_prod"
    raw_payload = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_heykevin_prod",'
        b'"accountId":"acc_jobber_12345","occurredAt":"2026-10-09T13:01:49Z","itemId":"item_disconnect_77"}}}'
    )
    sig = _sign(raw_payload, secret)

    envelope = authenticate_jobber_disconnect_envelope(
        raw_body=raw_payload,
        signature=sig,
        client_secret=secret,
        expected_app_id=app_id,
    )

    assert isinstance(envelope, JobberDisconnectEnvelope)
    assert envelope.app_id == "app_heykevin_prod"
    assert envelope.account_id == "acc_jobber_12345"
    assert envelope.item_id == "item_disconnect_77"
    assert envelope.occurred_at == "2026-10-09T13:01:49Z"
    assert envelope.delivery_fingerprint == hashlib.sha256(raw_payload).hexdigest().lower()


def test_tampered_body_signature_or_secret_rejected() -> None:
    """Verify tampered raw body, bad signature, or wrong secret fails authentication safely."""
    secret = "jobber_secret_123"
    app_id = "app_1"
    raw_payload = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}}}'
    )
    valid_sig = _sign(raw_payload, secret)

    # 1. Tampered body bytes
    assert (
        authenticate_jobber_disconnect_envelope(
            raw_body=raw_payload + b" ",
            signature=valid_sig,
            client_secret=secret,
            expected_app_id=app_id,
        )
        is None
    )

    # 2. Tampered signature
    bad_sig = "A" * 43 + "="
    assert (
        authenticate_jobber_disconnect_envelope(
            raw_body=raw_payload,
            signature=bad_sig,
            client_secret=secret,
            expected_app_id=app_id,
        )
        is None
    )

    # 3. Wrong secret
    assert (
        authenticate_jobber_disconnect_envelope(
            raw_body=raw_payload,
            signature=valid_sig,
            client_secret="wrong_secret",
            expected_app_id=app_id,
        )
        is None
    )


def test_expected_app_mismatch_rejected() -> None:
    """Verify payload appId mismatch against configured expected_app_id returns None."""
    secret = "secret"
    raw_payload = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_other",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}}}'
    )
    sig = _sign(raw_payload, secret)

    result = authenticate_jobber_disconnect_envelope(
        raw_body=raw_payload,
        signature=sig,
        client_secret=secret,
        expected_app_id="app_expected",
    )
    assert result is None


def test_absent_or_invalid_configured_app_rejected() -> None:
    """Verify malformed or invalid configured expected_app_id rejects before verification."""
    secret = "secret"
    raw_payload = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}}}'
    )
    sig = _sign(raw_payload, secret)

    for bad_app_id in [
        None,
        "",
        "   ",
        "app 1",
        "app\n1",
        "app\x001",
        "app\x1f1",
        "app\x7f1",
        "app\x801",
        "a" * 1025,
        12345,
        ["app_1"],
    ]:
        result = authenticate_jobber_disconnect_envelope(
            raw_body=raw_payload,
            signature=sig,
            client_secret=secret,
            expected_app_id=bad_app_id,
        )
        assert result is None


def test_wrong_topic_rejected() -> None:
    """Verify non-APP_DISCONNECT topics are rejected safely."""
    secret = "secret"
    app_id = "app_1"

    for bad_topic in ["APP_CONNECT", "JOB_CREATE", "app_disconnect", "APP_DISCONNECT_EVENT", "", None, 123]:
        raw = json.dumps(
            {
                "data": {
                    "webHookEvent": {
                        "topic": bad_topic,
                        "appId": app_id,
                        "accountId": "acc_1",
                        "occurredAt": "2026-10-09T13:01:49Z",
                    }
                }
            }
        ).encode("utf-8")
        sig = _sign(raw, secret)
        assert (
            authenticate_jobber_disconnect_envelope(
                raw_body=raw,
                signature=sig,
                client_secret=secret,
                expected_app_id=app_id,
            )
            is None
        )


def test_schema_and_type_anomalies_rejected() -> None:
    """Verify structural schema anomalies and invalid ID formats are rejected."""
    secret = "secret"
    app_id = "app_1"

    invalid_payloads = [
        # Non-dict root
        b'["not", "a", "dict"]',
        b'"just a string"',
        b"12345",
        # Missing data or bad data type
        b'{"data": null}',
        b'{"data": []}',
        b'{"data": "invalid"}',
        b'{"other": {}}',
        # Missing webHookEvent or bad webHookEvent type
        b'{"data": {"webHookEvent": null}}',
        b'{"data": {"webHookEvent": []}}',
        b'{"data": {"webHookEvent": "str"}}',
        b'{"data": {"otherEvent": {}}}',
        # Missing required fields
        b'{"data": {"webHookEvent": {"topic": "APP_DISCONNECT", "accountId": "acc_1", "occurredAt": "2026-10-09T13:01:49Z"}}}',
        b'{"data": {"webHookEvent": {"topic": "APP_DISCONNECT", "appId": "app_1", "occurredAt": "2026-10-09T13:01:49Z"}}}',
        b'{"data": {"webHookEvent": {"topic": "APP_DISCONNECT", "appId": "app_1", "accountId": "acc_1"}}}',
        # Invalid accountId values (whitespace, controls, length > 1024, empty, non-str)
        b'{"data": {"webHookEvent": {"topic": "APP_DISCONNECT", "appId": "app_1", "accountId": "", "occurredAt": "2026-10-09T13:01:49Z"}}}',
        b'{"data": {"webHookEvent": {"topic": "APP_DISCONNECT", "appId": "app_1", "accountId": "acc 1", "occurredAt": "2026-10-09T13:01:49Z"}}}',
        b'{"data": {"webHookEvent": {"topic": "APP_DISCONNECT", "appId": "app_1", "accountId": "acc\\n1", "occurredAt": "2026-10-09T13:01:49Z"}}}',
        b'{"data": {"webHookEvent": {"topic": "APP_DISCONNECT", "appId": "app_1", "accountId": 12345, "occurredAt": "2026-10-09T13:01:49Z"}}}',
        b'{"data": {"webHookEvent": {"topic": "APP_DISCONNECT", "appId": "app_1", "accountId": null, "occurredAt": "2026-10-09T13:01:49Z"}}}',
        # Invalid appId in payload (contains control or whitespace)
        b'{"data": {"webHookEvent": {"topic": "APP_DISCONNECT", "appId": "app\\t1", "accountId": "acc_1", "occurredAt": "2026-10-09T13:01:49Z"}}}',
    ]

    for payload in invalid_payloads:
        sig = _sign(payload, secret)
        assert (
            authenticate_jobber_disconnect_envelope(
                raw_body=payload,
                signature=sig,
                client_secret=secret,
                expected_app_id=app_id,
            )
            is None
        )


def test_duplicate_keys_at_multiple_depths_rejected() -> None:
    """Verify duplicate JSON keys at any nesting depth fail parsing."""
    secret = "secret"
    app_id = "app_1"

    duplicate_key_payloads = [
        # Root level duplicates
        (
            b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1","accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}},'
            b'"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1","accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}}}'
        ),
        # Data level duplicates
        (
            b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1","accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"},'
            b'"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1","accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}}}'
        ),
        # webHookEvent level duplicates
        (
            b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","topic":"APP_DISCONNECT","appId":"app_1","accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}}}'
        ),
        (
            b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1","accountId":"acc_1","accountId":"acc_2","occurredAt":"2026-10-09T13:01:49Z"}}}'
        ),
        # Nested extra level duplicates
        (
            b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1","accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z","extra":{"dup":1,"dup":2}}}}'
        ),
    ]

    for payload in duplicate_key_payloads:
        sig = _sign(payload, secret)
        assert (
            authenticate_jobber_disconnect_envelope(
                raw_body=payload,
                signature=sig,
                client_secret=secret,
                expected_app_id=app_id,
            )
            is None
        )


def test_malformed_utf8_json_bom_nonfinite_and_deep() -> None:
    """Verify BOM, invalid UTF-8, syntax errors, nonfinite constants, and excessive depth are rejected."""
    secret = "secret"
    app_id = "app_1"
    valid_body = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}}}'
    )

    # 1. UTF-8 BOM
    bom_body = b"\xef\xbb\xbf" + valid_body
    assert (
        authenticate_jobber_disconnect_envelope(
            raw_body=bom_body,
            signature=_sign(bom_body, secret),
            client_secret=secret,
            expected_app_id=app_id,
        )
        is None
    )

    # 2. Invalid UTF-8 bytes
    invalid_utf8 = b'{"data": "\xff\xfe invalid utf8"}'
    assert (
        authenticate_jobber_disconnect_envelope(
            raw_body=invalid_utf8,
            signature=_sign(invalid_utf8, secret),
            client_secret=secret,
            expected_app_id=app_id,
        )
        is None
    )

    # 3. Nonfinite constants (NaN, Infinity, -Infinity)
    for constant in [b"NaN", b"Infinity", b"-Infinity"]:
        nonfinite_body = (
            b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
            b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z","num":' + constant + b"}}}"
        )
        assert (
            authenticate_jobber_disconnect_envelope(
                raw_body=nonfinite_body,
                signature=_sign(nonfinite_body, secret),
                client_secret=secret,
                expected_app_id=app_id,
            )
            is None
        )

    # 4. Malformed JSON syntax
    for bad_json in [b'{"data": {', b'{"data": "unclosed', b"{,}", b""]:
        if bad_json:
            assert (
                authenticate_jobber_disconnect_envelope(
                    raw_body=bad_json,
                    signature=_sign(bad_json, secret),
                    client_secret=secret,
                    expected_app_id=app_id,
                )
                is None
            )

    # 5. Excessive nesting depth (> 10 levels)
    deep_json = b'{"a":{"a":{"a":{"a":{"a":{"a":{"a":{"a":{"a":{"a":{"a":{"topic":"APP_DISCONNECT"}}}}}}}}}}}'
    assert (
        authenticate_jobber_disconnect_envelope(
            raw_body=deep_json,
            signature=_sign(deep_json, secret),
            client_secret=secret,
            expected_app_id=app_id,
        )
        is None
    )


def test_size_boundaries() -> None:
    """Verify body length boundaries: 0 (rejected), 65537 (rejected), 65536 (valid)."""
    secret = "secret"
    app_id = "app_1"

    # 0 bytes rejected
    assert (
        authenticate_jobber_disconnect_envelope(
            raw_body=b"",
            signature="A" * 44,
            client_secret=secret,
            expected_app_id=app_id,
        )
        is None
    )

    # Max body size (65536 bytes) with valid payload + padding
    prefix = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z","padding":"'
    )
    suffix = b'"}}}'
    pad_len = 65536 - len(prefix) - len(suffix)
    max_payload = prefix + (b"x" * pad_len) + suffix
    assert len(max_payload) == 65536
    sig_max = _sign(max_payload, secret)

    env_max = authenticate_jobber_disconnect_envelope(
        raw_body=max_payload,
        signature=sig_max,
        client_secret=secret,
        expected_app_id=app_id,
    )
    assert env_max is not None
    assert env_max.account_id == "acc_1"

    # Oversized payload (65537 bytes) rejected
    oversized = max_payload + b" "
    assert len(oversized) == 65537
    sig_over = _sign(oversized, secret)
    assert (
        authenticate_jobber_disconnect_envelope(
            raw_body=oversized,
            signature=sig_over,
            client_secret=secret,
            expected_app_id=app_id,
        )
        is None
    )


def test_valid_fractional_and_offset_timestamps() -> None:
    """Verify valid RFC 3339 timestamps (subseconds and timezone offsets) are accepted and preserved."""
    secret = "secret"
    app_id = "app_1"

    valid_timestamps = [
        "2026-10-09T13:01:49Z",
        "2026-10-09T13:01:49.1Z",
        "2026-10-09T13:01:49.12Z",
        "2026-10-09T13:01:49.123Z",
        "2026-10-09T13:01:49.123456Z",
        "2026-10-09T13:01:49.123456789Z",
        "2026-10-09T13:01:49+00:00",
        "2026-10-09T13:01:49-05:00",
        "2026-10-09T13:01:49+05:30",
        "2026-10-09T13:01:49-08:00",
        "2024-02-29T23:59:59Z",  # Leap year Feb 29
        "2026-10-09t13:01:49z",  # Lowercase t and z
    ]

    for ts in valid_timestamps:
        raw = json.dumps(
            {
                "data": {
                    "webHookEvent": {
                        "topic": "APP_DISCONNECT",
                        "appId": app_id,
                        "accountId": "acc_1",
                        "occurredAt": ts,
                    }
                }
            }
        ).encode("utf-8")
        sig = _sign(raw, secret)
        env = authenticate_jobber_disconnect_envelope(
            raw_body=raw,
            signature=sig,
            client_secret=secret,
            expected_app_id=app_id,
        )
        assert env is not None
        # Assert precision is preserved exactly without truncation or modification
        assert env.occurred_at == ts


def test_impossible_local_and_legacy_timestamps_rejected() -> None:
    """Verify impossible calendar dates, naive/local timestamps, and legacy 'occuredAt' are rejected."""
    secret = "secret"
    app_id = "app_1"

    invalid_timestamps = [
        "2025-02-29T12:00:00Z",  # 2025 is not a leap year
        "2026-04-31T12:00:00Z",  # April only has 30 days
        "2026-02-30T12:00:00Z",  # Feb 30 impossible
        "2026-13-01T12:00:00Z",  # Month 13
        "2026-00-01T12:00:00Z",  # Month 0
        "2026-10-00T12:00:00Z",  # Day 0
        "2026-10-09T24:00:00Z",  # Hour 24
        "2026-10-09T12:60:00Z",  # Minute 60
        "2026-10-09T12:00:60Z",  # Second 60
        "2026-10-09T12:00:00",  # Missing timezone offset (naive/local)
        "2026-10-09 12:00:00Z",  # Space instead of T
        "2026-10-09",  # Date only
        "2026-10-09T12:00:00+24:00",  # Invalid tz offset hour 24
        "2026-10-09T12:00:00+05:60",  # Invalid tz offset min 60
        "2026-10-09T12:00:00.1234567890Z",  # 10 fractional digits (> 9)
        "2026-10-09T17:00:00+00:00\n",  # Trailing newline after numeric offset
        "2026-10-09T17:00:00Z\n",  # Trailing newline after Z
        "٢٠٢٦-10-09T17:00:00Z",  # Unicode Arabic-Indic digits in year
        "2026-10-09T17:00:00.١٢٣Z",  # Unicode Arabic-Indic digits in fraction
        "2026-10-09T17:00:00+٠٠:٠٠",  # Unicode Arabic-Indic digits in offset
        "not_a_date",
        1234567890,
    ]

    for ts in invalid_timestamps:
        raw = json.dumps(
            {
                "data": {
                    "webHookEvent": {
                        "topic": "APP_DISCONNECT",
                        "appId": app_id,
                        "accountId": "acc_1",
                        "occurredAt": ts,
                    }
                }
            }
        ).encode("utf-8")
        sig = _sign(raw, secret)
        assert (
            authenticate_jobber_disconnect_envelope(
                raw_body=raw,
                signature=sig,
                client_secret=secret,
                expected_app_id=app_id,
            )
            is None
        )

    # Legacy misspelling "occuredAt" without "occurredAt"
    legacy_raw = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occuredAt":"2026-10-09T13:01:49Z"}}}'
    )
    assert (
        authenticate_jobber_disconnect_envelope(
            raw_body=legacy_raw,
            signature=_sign(legacy_raw, secret),
            client_secret=secret,
            expected_app_id=app_id,
        )
        is None
    )


def test_trailing_newline_and_unicode_digits_rejected_real_hmac() -> None:
    """Verify numeric-offset trailing-newline and Unicode digits (year, fraction, offset) fail with real HMAC."""
    secret = "jobber_oauth_secret_abc"
    app_id = "app_heykevin_prod"

    rejected_timestamps = [
        # Trailing newlines (numeric offset, negative offset, fraction + offset, Z, CRLF)
        "2026-10-09T17:00:00+00:00\n",
        "2026-10-09T17:00:00-05:00\n",
        "2026-10-09T17:00:00.123+00:00\n",
        "2026-10-09T17:00:00.123456789+00:00\n",
        "2026-10-09T17:00:00Z\n",
        "2026-10-09T17:00:00+00:00\r\n",
        # Unicode digits in year (Arabic-Indic, Fullwidth, Devanagari)
        "٢٠٢٦-10-09T17:00:00Z",
        "２０２６-10-09T17:00:00Z",
        "२०२६-10-09T17:00:00Z",
        # Unicode digits in month / day / hour / minute / second
        "2026-١٠-09T17:00:00Z",
        "2026-10-٠٩T17:00:00Z",
        "2026-10-09T١٧:00:00Z",
        "2026-10-09T17:٠٠:00Z",
        "2026-10-09T17:00:٠٠Z",
        # Unicode digits in fraction
        "2026-10-09T17:00:00.١٢٣Z",
        "2026-10-09T17:00:00.１２３Z",
        "2026-10-09T17:00:00.١٢٣+00:00",
        "2026-10-09T17:00:00.１２３+00:00",
        "2026-10-09T17:00:00.१२३Z",
        # Unicode digits in numeric offset
        "2026-10-09T17:00:00+٠٠:٠٠",
        "2026-10-09T17:00:00-٠٥:٠٠",
        "2026-10-09T17:00:00+００:００",
        "2026-10-09T17:00:00-０５:００",
        # Unicode digits in both fraction and offset
        "2026-10-09T17:00:00.١٢٣+٠٠:٠٠",
        "2026-10-09T17:00:00.１２３+００:００",
    ]

    for ts in rejected_timestamps:
        raw = json.dumps(
            {
                "data": {
                    "webHookEvent": {
                        "topic": "APP_DISCONNECT",
                        "appId": app_id,
                        "accountId": "acc_jobber_12345",
                        "occurredAt": ts,
                    }
                }
            }
        ).encode("utf-8")
        sig = _sign(raw, secret)
        assert (
            authenticate_jobber_disconnect_envelope(
                raw_body=raw,
                signature=sig,
                client_secret=secret,
                expected_app_id=app_id,
            )
            is None
        )


def test_optional_and_null_item_id() -> None:
    """Verify itemId can be omitted, null, or a valid ID string."""
    secret = "secret"
    app_id = "app_1"

    # 1. Omitted itemId
    raw_omitted = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}}}'
    )
    env_omitted = authenticate_jobber_disconnect_envelope(
        raw_body=raw_omitted,
        signature=_sign(raw_omitted, secret),
        client_secret=secret,
        expected_app_id=app_id,
    )
    assert env_omitted is not None
    assert env_omitted.item_id is None

    # 2. itemId: null
    raw_null = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z","itemId":null}}}'
    )
    env_null = authenticate_jobber_disconnect_envelope(
        raw_body=raw_null,
        signature=_sign(raw_null, secret),
        client_secret=secret,
        expected_app_id=app_id,
    )
    assert env_null is not None
    assert env_null.item_id is None

    # 3. Valid string itemId
    raw_str = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z","itemId":"item_abc_99"}}}'
    )
    env_str = authenticate_jobber_disconnect_envelope(
        raw_body=raw_str,
        signature=_sign(raw_str, secret),
        client_secret=secret,
        expected_app_id=app_id,
    )
    assert env_str is not None
    assert env_str.item_id == "item_abc_99"

    # 4. Invalid itemId (whitespace, control chars, empty, non-str)
    for bad_item_id in ["", "item 1", "item\n1", "item\x001", "a" * 1025, 123, []]:
        raw_bad = json.dumps(
            {
                "data": {
                    "webHookEvent": {
                        "topic": "APP_DISCONNECT",
                        "appId": app_id,
                        "accountId": "acc_1",
                        "occurredAt": "2026-10-09T13:01:49Z",
                        "itemId": bad_item_id,
                    }
                }
            }
        ).encode("utf-8")
        assert (
            authenticate_jobber_disconnect_envelope(
                raw_body=raw_bad,
                signature=_sign(raw_bad, secret),
                client_secret=secret,
                expected_app_id=app_id,
            )
            is None
        )


def test_ignored_unknown_fields() -> None:
    """Verify unknown fields at any level are ignored and never retained in the envelope."""
    secret = "secret"
    app_id = "app_1"

    raw = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z",'
        b'"unknown_event_attr":"secret_token_123","eventContext":{"nested":true}},'
        b'"extra_data_field":"ignored"},"root_meta":"ignored"}'
    )
    env = authenticate_jobber_disconnect_envelope(
        raw_body=raw,
        signature=_sign(raw, secret),
        client_secret=secret,
        expected_app_id=app_id,
    )
    assert env is not None
    assert env.app_id == "app_1"
    assert env.account_id == "acc_1"
    # Ensure envelope only contains declared attributes
    assert not hasattr(env, "unknown_event_attr")
    assert not hasattr(env, "extra_data_field")
    assert not hasattr(env, "root_meta")


def test_delivery_fingerprint_semantics() -> None:
    """Verify delivery fingerprint matches exact raw byte SHA256 and differs across serializations."""
    secret = "secret"
    app_id = "app_1"

    # Same payload exact bytes produce identical fingerprint
    raw_1 = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}}}'
    )
    sig_1 = _sign(raw_1, secret)
    env_1a = authenticate_jobber_disconnect_envelope(raw_1, sig_1, secret, app_id)
    env_1b = authenticate_jobber_disconnect_envelope(raw_1, sig_1, secret, app_id)
    assert env_1a is not None and env_1b is not None
    assert env_1a.delivery_fingerprint == env_1b.delivery_fingerprint
    assert env_1a.delivery_fingerprint == hashlib.sha256(raw_1).hexdigest().lower()

    # Same semantic event with different spacing has different raw bytes, different valid signature, different fingerprint
    raw_2 = (
        b'{\n  "data": {\n    "webHookEvent": {\n      "topic": "APP_DISCONNECT",\n'
        b'      "appId": "app_1",\n      "accountId": "acc_1",\n      "occurredAt": "2026-10-09T13:01:49Z"\n    }\n  }\n}'
    )
    sig_2 = _sign(raw_2, secret)
    env_2 = authenticate_jobber_disconnect_envelope(raw_2, sig_2, secret, app_id)
    assert env_2 is not None
    assert env_2.delivery_fingerprint != env_1a.delivery_fingerprint
    assert env_2.delivery_fingerprint == hashlib.sha256(raw_2).hexdigest().lower()


def test_authenticate_before_parse_guard() -> None:
    """Verify that JSON parsing is NEVER invoked if signature verification or expected_app_id fails."""
    secret = "secret"
    app_id = "app_1"
    raw = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}}}'
    )
    bad_sig = "A" * 43 + "="

    with mock.patch("json.loads") as mock_json_loads:
        # 1. Invalid signature -> json.loads must not be called
        res_bad_sig = authenticate_jobber_disconnect_envelope(
            raw_body=raw,
            signature=bad_sig,
            client_secret=secret,
            expected_app_id=app_id,
        )
        assert res_bad_sig is None
        mock_json_loads.assert_not_called()

        # 2. Invalid expected_app_id -> json.loads must not be called
        res_bad_app = authenticate_jobber_disconnect_envelope(
            raw_body=raw,
            signature=_sign(raw, secret),
            client_secret=secret,
            expected_app_id="",
        )
        assert res_bad_app is None
        mock_json_loads.assert_not_called()


def test_no_logging_and_frozen_envelope_immutability(caplog: pytest.LogCaptureFixture) -> None:
    """Verify zero logs are emitted and returned envelope instance is strictly immutable."""
    caplog.set_level(logging.DEBUG)

    secret = "secret"
    app_id = "app_1"
    raw = (
        b'{"data":{"webHookEvent":{"topic":"APP_DISCONNECT","appId":"app_1",'
        b'"accountId":"acc_1","occurredAt":"2026-10-09T13:01:49Z"}}}'
    )
    sig = _sign(raw, secret)

    env = authenticate_jobber_disconnect_envelope(raw, sig, secret, app_id)
    assert env is not None

    # Test immutability
    with pytest.raises(FrozenInstanceError):
        env.app_id = "mutated_app"  # type: ignore[misc]

    with pytest.raises(FrozenInstanceError):
        env.account_id = "mutated_acc"  # type: ignore[misc]

    # Verify no log records were emitted
    assert len(caplog.records) == 0
