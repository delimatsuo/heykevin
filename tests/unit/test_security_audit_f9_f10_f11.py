"""Tests for the medium-severity security findings F-09, F-10, and F-11.

  F-09  `_validate_external_url` (and `_resolve_and_validate_url`) reject
        SSRF targets including the GCP metadata IP and DNS-rebinding
        scenarios, and the pinned resolver freezes the IP for the actual
        connect.
  F-10  `/api/estimates/{token}/upload` aborts past `MAX_UPLOAD_BYTES`
        without buffering the entire body, returning HTTP 413.
  F-11  Call transcripts encrypt + decrypt through `app.db.calls`, and
        legacy plaintext rows still read back as plaintext.
"""

from __future__ import annotations

import base64
import os
import socket
from typing import Dict, List, Optional

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")


# ---------------------------------------------------------------------------
# F-09: SSRF DNS-rebinding TOCTOU
# ---------------------------------------------------------------------------

def test_ssrf_blocks_gcp_metadata_ip():
    from app.api import contractors as ct

    assert ct._resolve_and_validate_url("http://169.254.169.254/computeMetadata/v1/") is None
    assert ct._validate_external_url("http://169.254.169.254/") is False


def test_ssrf_blocks_loopback_and_private():
    from app.api import contractors as ct

    assert ct._validate_external_url("http://127.0.0.1/") is False
    assert ct._validate_external_url("http://10.0.0.5/") is False
    assert ct._validate_external_url("http://192.168.1.1/") is False
    assert ct._validate_external_url("http://172.16.5.5/") is False
    # CGN range — RFC 6598
    assert ct._validate_external_url("http://100.64.1.2/") is False
    # Link-local
    assert ct._validate_external_url("http://169.254.1.2/") is False


def test_ssrf_blocks_metadata_hostname():
    from app.api import contractors as ct

    assert ct._validate_external_url("http://metadata.google.internal/") is False
    assert ct._validate_external_url("http://localhost/") is False


def test_ssrf_blocks_non_http_schemes():
    from app.api import contractors as ct

    assert ct._validate_external_url("file:///etc/passwd") is False
    assert ct._validate_external_url("gopher://example.com/") is False
    assert ct._validate_external_url("javascript:alert(1)") is False


def test_ssrf_dns_rebinding_rejected_when_resolver_returns_internal_ip(monkeypatch):
    """If the attacker's DNS returns an internal IP, validation must fail."""
    from app.api import contractors as ct

    def fake_getaddrinfo(host, port, family=0, socktype=0, proto=0, flags=0):
        # Pretend the attacker's DNS server points evil.example to GCP metadata.
        return [(family or socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert ct._validate_external_url("http://evil.example/") is False


def test_ssrf_dns_returns_public_ip_passes(monkeypatch):
    from app.api import contractors as ct

    def fake_getaddrinfo(host, port, family=0, socktype=0, proto=0, flags=0):
        return [(family or socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    resolved = ct._resolve_and_validate_url("http://example.com/")
    assert resolved is not None
    hostname, ip, port, scheme = resolved
    assert hostname == "example.com"
    assert ip == "93.184.216.34"
    assert scheme == "http"


def test_pinned_resolver_swaps_target_hostname(monkeypatch):
    """Inside the pinned-resolver context, the validated IP is returned for
    the validated hostname, even if the underlying DNS would now return
    an internal IP (DNS rebinding scenario)."""
    from app.api import contractors as ct

    rebind_calls: List[str] = []

    def evil_getaddrinfo(host, port, *args, **kwargs):
        rebind_calls.append(host)
        # Attacker rebinds to internal IP — but inside the pinned resolver
        # the IP we already validated should be returned instead.
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(socket, "getaddrinfo", evil_getaddrinfo)

    with ct._PinnedResolver("example.com", "93.184.216.34", 443):
        infos = socket.getaddrinfo("example.com", 443)
        assert len(infos) == 1
        family, socktype, proto, canon, sockaddr = infos[0]
        assert sockaddr[0] == "93.184.216.34"  # pinned, NOT 169.254...
        # Other hostnames still resolve via the (evil) underlying resolver,
        # but unrelated hostnames are not the attack target.
        unrelated = socket.getaddrinfo("other.example", 80)
        assert unrelated[0][4][0] == "169.254.169.254"

    # After exiting the context, the original (or "evil" patched) resolver returns.
    restored = socket.getaddrinfo("example.com", 443)
    assert restored[0][4][0] == "169.254.169.254"


# ---------------------------------------------------------------------------
# F-10: streaming upload size cap
# ---------------------------------------------------------------------------

class _FakeRequest:
    """Mimic enough of starlette.Request for the upload handler to run."""

    def __init__(self, chunks: List[bytes], headers: Optional[Dict[str, str]] = None):
        self._chunks = list(chunks)
        self.headers = headers or {}

    async def stream(self):
        for c in self._chunks:
            yield c

    async def body(self):
        return b"".join(self._chunks)


@pytest.mark.asyncio
async def test_upload_streaming_aborts_past_cap():
    from app.api import estimates
    from fastapi import HTTPException

    # 3 chunks of 4 MiB each = 12 MiB; cap at 5 MiB → must abort on chunk 2.
    chunk = b"x" * (4 * 1024 * 1024)
    req = _FakeRequest([chunk, chunk, chunk], headers={"content-type": "video/mp4"})

    with pytest.raises(HTTPException) as exc:
        await estimates._read_request_with_cap(req, 5 * 1024 * 1024)
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_upload_rejects_oversized_content_length_header():
    from app.api import estimates
    from fastapi import HTTPException

    req = _FakeRequest([b""], headers={"content-length": str(100 * 1024 * 1024)})
    with pytest.raises(HTTPException) as exc:
        await estimates._read_request_with_cap(req, 10 * 1024 * 1024)
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_upload_happy_path_accepts_under_cap():
    from app.api import estimates

    payload = b"image-bytes-here"
    req = _FakeRequest([payload], headers={"content-length": str(len(payload))})
    body = await estimates._read_request_with_cap(req, 1024 * 1024)
    assert body == payload


# ---------------------------------------------------------------------------
# F-11: transcript encryption round-trip + legacy compat
# ---------------------------------------------------------------------------

def _set_transcript_key(monkeypatch, key_bytes: Optional[bytes] = None):
    if key_bytes is None:
        key_bytes = os.urandom(32)
    encoded = base64.b64encode(key_bytes).decode("ascii")
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "transcript_encryption_key", encoded, raising=False)
    monkeypatch.setenv("TRANSCRIPT_ENCRYPTION_KEY", encoded)
    return key_bytes


def test_transcript_encrypt_decrypt_round_trip(monkeypatch):
    from app.db import calls

    _set_transcript_key(monkeypatch)
    plaintext = "Caller: I have a leak in the kitchen.\nKevin: Where exactly?"
    blob = calls._encrypt_transcript(plaintext)
    assert blob is not None
    assert blob["version"] == 1
    assert "ciphertext" in blob and "nonce" in blob
    # Plaintext must not appear in the encoded ciphertext.
    assert "leak" not in blob["ciphertext"]
    assert "leak" not in blob["nonce"]

    decrypted = calls._decrypt_transcript(blob)
    assert decrypted == plaintext


def test_transcript_encrypt_disabled_returns_none(monkeypatch):
    """No env var set → encryption is a no-op, caller falls back to plaintext."""
    from app.config import settings as _settings
    from app.db import calls

    monkeypatch.setattr(_settings, "transcript_encryption_key", "", raising=False)
    monkeypatch.delenv("TRANSCRIPT_ENCRYPTION_KEY", raising=False)
    assert calls._encrypt_transcript("hello") is None
    # Reading a plaintext value passes through unchanged.
    assert calls._decrypt_transcript("hello world") == "hello world"


def test_transcript_legacy_plaintext_read_through(monkeypatch):
    """Records written before this feature have no `version` field — they
    must still read back as plaintext."""
    from app.db import calls

    _set_transcript_key(monkeypatch)
    legacy_doc = {
        "call_sid": "CA1",
        "transcript": "old plaintext transcript content",
        "contractor_id": "c1",
    }
    out = calls._maybe_decrypt_call_doc(legacy_doc)
    assert out is not None
    assert out["transcript"] == "old plaintext transcript content"


def test_transcript_save_load_payload_shape(monkeypatch):
    """`_maybe_encrypt_call_data` produces a dict shape ready for Firestore,
    and `_maybe_decrypt_call_doc` reverses it."""
    from app.db import calls

    _set_transcript_key(monkeypatch)
    raw = {"call_sid": "CA1", "transcript": "secret"}
    encrypted = calls._maybe_encrypt_call_data(raw)
    assert isinstance(encrypted["transcript"], dict)
    assert encrypted["transcript"]["version"] == 1
    # Round-trip
    back = calls._maybe_decrypt_call_doc(encrypted)
    assert back["transcript"] == "secret"


def test_transcript_invalid_key_logged_not_used(monkeypatch):
    """An invalid base64 / wrong-length key must not silently encrypt with
    a weak fallback — encryption is skipped instead."""
    from app.config import settings as _settings
    from app.db import calls

    monkeypatch.setattr(_settings, "transcript_encryption_key", "not-base64-!!!", raising=False)
    monkeypatch.setenv("TRANSCRIPT_ENCRYPTION_KEY", "not-base64-!!!")
    assert calls._encrypt_transcript("data") is None


def test_transcript_short_key_rejected(monkeypatch):
    """A key whose decoded length is not 32 bytes is rejected."""
    from app.config import settings as _settings
    from app.db import calls

    short = base64.b64encode(b"\x01" * 16).decode("ascii")
    monkeypatch.setattr(_settings, "transcript_encryption_key", short, raising=False)
    monkeypatch.setenv("TRANSCRIPT_ENCRYPTION_KEY", short)
    assert calls._encrypt_transcript("data") is None


def test_protected_environment_refuses_plaintext_transcript_fallback(monkeypatch):
    from app.config import settings as _settings
    from app.db import calls

    monkeypatch.setattr(_settings, "environment", "staging")
    monkeypatch.setattr(_settings, "transcript_encryption_key", "", raising=False)
    monkeypatch.delenv("TRANSCRIPT_ENCRYPTION_KEY", raising=False)

    with pytest.raises(calls.TranscriptEncryptionUnavailableError):
        calls._maybe_encrypt_call_data({"transcript": "private transcript"})


def test_development_preserves_legacy_plaintext_fallback(monkeypatch):
    from app.config import settings as _settings
    from app.db import calls

    monkeypatch.setattr(_settings, "environment", "development")
    monkeypatch.setattr(_settings, "transcript_encryption_key", "", raising=False)
    monkeypatch.delenv("TRANSCRIPT_ENCRYPTION_KEY", raising=False)
    raw = {"transcript": "local development transcript"}

    assert calls._maybe_encrypt_call_data(raw) == raw


def test_protected_environment_rejects_unknown_transcript_shape(monkeypatch):
    from app.config import settings as _settings
    from app.db import calls

    monkeypatch.setattr(_settings, "environment", "production")
    unknown = {"transcript": {"raw": "unexpected transcript shape"}}

    with pytest.raises(calls.TranscriptEncryptionUnavailableError):
        calls._maybe_encrypt_call_data(unknown)

    malformed_encrypted = {
        "transcript": {
            "version": 1,
            "ciphertext": "not-valid-base64",
            "nonce": "not-valid-base64",
        }
    }
    with pytest.raises(calls.TranscriptEncryptionUnavailableError):
        calls._maybe_encrypt_call_data(malformed_encrypted)

    encrypted = {
        "transcript": {
            "version": 1,
            "ciphertext": base64.b64encode(b"c" * 16).decode("ascii"),
            "nonce": base64.b64encode(b"n" * 12).decode("ascii"),
        }
    }
    assert calls._maybe_encrypt_call_data(encrypted) == encrypted
    assert calls._maybe_encrypt_call_data({"transcript": ""}) == {"transcript": ""}


@pytest.mark.asyncio
async def test_protected_save_never_writes_plaintext_when_encryption_unavailable(
    monkeypatch,
    caplog,
):
    from app.config import settings as _settings
    from app.db import calls

    writes = []

    class FakeDocument:
        def set(self, data, merge=False):
            writes.append((data, merge))

    class FakeCollection:
        def document(self, _document_id):
            return FakeDocument()

    class FakeDatabase:
        def collection(self, _collection_name):
            return FakeCollection()

    monkeypatch.setattr(_settings, "environment", "production")
    monkeypatch.setattr(_settings, "transcript_encryption_key", "", raising=False)
    monkeypatch.delenv("TRANSCRIPT_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(calls, "get_firestore_client", lambda: FakeDatabase())

    with caplog.at_level("ERROR"):
        with pytest.raises(calls.TranscriptEncryptionUnavailableError):
            await calls.save_call("CA_redacted", {"transcript": "private transcript"})

    assert writes == []
    assert "exception_type=TranscriptEncryptionUnavailableError" in caplog.text
    assert "private transcript" not in caplog.text


def test_transcript_decrypt_with_wrong_key_returns_empty(monkeypatch):
    """Tampered or wrong-key ciphertext must surface as empty rather than
    leaking raw bytes to API consumers."""
    from app.db import calls

    _set_transcript_key(monkeypatch, key_bytes=os.urandom(32))
    blob = calls._encrypt_transcript("hello")
    assert blob is not None
    # Rotate the key — the previous ciphertext can no longer be decrypted.
    _set_transcript_key(monkeypatch, key_bytes=os.urandom(32))
    out = calls._decrypt_transcript(blob)
    assert out == ""
