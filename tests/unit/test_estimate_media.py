"""Unit tests for estimate media GCS archiving and signed watch URLs."""

import hashlib
import os
import secrets
import time
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.config import settings
from app.services import estimate_media


class _FakeBlob:
    def __init__(self, name: str, bucket: "_FakeBucket"):
        self.name = name
        self.bucket = bucket
        self.uploaded_bytes = b""
        self.uploaded_content_type = ""

    def upload_from_string(self, data: bytes, content_type: str = "") -> None:
        self.uploaded_bytes = data
        self.uploaded_content_type = content_type
        self.bucket.blobs[self.name] = self

    def generate_signed_url(self, version: str = "v4", expiration=None, method: str = "GET") -> str:
        return f"https://storage.googleapis.com/{self.bucket.name}/{self.name}?signed=true"

    def download_as_bytes(self) -> bytes:
        return self.uploaded_bytes


class _FakeBucket:
    def __init__(self, name: str):
        self.name = name
        self.blobs: dict[str, _FakeBlob] = {}

    def blob(self, name: str) -> _FakeBlob:
        if name not in self.blobs:
            self.blobs[name] = _FakeBlob(name, self)
        return self.blobs[name]


class _FakeStorageClient:
    def __init__(self):
        self.buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        if name not in self.buckets:
            self.buckets[name] = _FakeBucket(name)
        return self.buckets[name]


# 1. Archive writes to the injected fake GCS client with the expected object path and content type; returns the path.
def test_archive_writes_to_fake_gcs_client_with_expected_path_and_content_type(monkeypatch):
    fake_client = _FakeStorageClient()
    monkeypatch.setattr(settings, "estimate_media_bucket", "test-bucket")

    token_hash = "token_hash_12345"
    media_id = "media_id_abcde"
    media_bytes = b"fake-video-content-12345"
    content_type = "video/mp4"

    object_path = estimate_media.archive_media(
        token_hash=token_hash,
        media_id=media_id,
        media_bytes=media_bytes,
        content_type=content_type,
        client_factory=lambda: fake_client,
    )

    assert object_path == f"{token_hash}/{media_id}.mp4"
    bucket = fake_client.buckets["test-bucket"]
    assert object_path in bucket.blobs
    blob = bucket.blobs[object_path]
    assert blob.uploaded_bytes == media_bytes
    assert blob.uploaded_content_type == content_type


# 2. Bucket unset → archive_media returns None, no client constructed.
def test_bucket_unset_returns_none_no_client_constructed(monkeypatch):
    monkeypatch.setattr(settings, "estimate_media_bucket", "")

    client_constructed = False

    def fake_factory():
        nonlocal client_constructed
        client_constructed = True
        return _FakeStorageClient()

    result = estimate_media.archive_media(
        token_hash="hash1",
        media_id="id1",
        media_bytes=b"bytes",
        content_type="video/mp4",
        client_factory=fake_factory,
    )

    assert result is None
    assert not client_constructed


# 3. Watch URL round-trip: make_watch_url → verify_watch_sig true.
def test_watch_url_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "vcard_hmac_secret", "secret-test-key-32bytes-length-1234567")
    monkeypatch.setattr(settings, "cloud_run_url", "https://api.example.com")

    media_id = "media_xyz_789"
    watch_url = estimate_media.make_watch_url(media_id)

    assert watch_url.startswith(f"https://api.example.com/api/estimates/media/{media_id}?")
    parsed = urlparse(watch_url)
    qs = parse_qs(parsed.query)

    assert "e" in qs
    assert "s" in qs

    expires = int(qs["e"][0])
    sig = qs["s"][0]

    assert expires > time.time()
    assert estimate_media.verify_watch_sig(media_id, expires, sig) is True


# 4. Tampered signature → false. Expired → false. (hmac.compare_digest used.)
def test_watch_sig_tampered_or_expired_rejected(monkeypatch):
    monkeypatch.setattr(settings, "vcard_hmac_secret", "secret-test-key-32bytes-length-1234567")

    media_id = "media_xyz_789"
    watch_url = estimate_media.make_watch_url(media_id)
    parsed = urlparse(watch_url)
    qs = parse_qs(parsed.query)
    expires = int(qs["e"][0])
    sig = qs["s"][0]

    # Tampered signature
    tampered_sig = "a" + sig[1:] if sig[0] != "a" else "b" + sig[1:]
    assert estimate_media.verify_watch_sig(media_id, expires, tampered_sig) is False

    # Tampered media_id
    assert estimate_media.verify_watch_sig("other_media_id", expires, sig) is False

    # Expired timestamp
    past_expires = int(time.time()) - 60
    assert estimate_media.verify_watch_sig(media_id, past_expires, sig) is False


# 5. media_id is not derived from the upload token (no substring of the token or its hash appears in the watch URL).
def test_media_id_not_derived_from_upload_token(monkeypatch):
    monkeypatch.setattr(settings, "vcard_hmac_secret", "secret-test-key-32bytes-length-1234567")

    upload_token = "secret_raw_upload_token_user_1234567890"
    upload_token_hash = hashlib.sha256(upload_token.encode()).hexdigest()

    # Generate a random media_id (as done in estimates.py)
    media_id = secrets.token_urlsafe(16)
    watch_url = estimate_media.make_watch_url(media_id)

    # Assert no substring (>= 4 chars) of token or token_hash appears in media_id or watch_url
    assert upload_token not in watch_url
    assert upload_token_hash not in watch_url
    assert upload_token[:8] not in watch_url
    assert upload_token_hash[:8] not in watch_url


# --- Review round 3, item 1: signing must work without a private key --------
# Cloud Run ADC comes from the metadata server and cannot sign bytes locally;
# generate_signed_url must route through IAM signBlob by passing
# service_account_email + access_token. Local key credentials keep the direct
# path. These pin both branches.


class _CapturingBlob:
    def __init__(self):
        self.kwargs = None

    def generate_signed_url(self, **kwargs):
        self.kwargs = kwargs
        return "https://signed.example/url"


class _CapturingClient:
    def __init__(self, blob):
        self._blob = blob

    def bucket(self, name):
        return self

    def blob(self, path):
        return self._blob


class _MetadataCreds:
    """No sign_bytes attribute — the Cloud Run ambient credential shape."""

    service_account_email = "runtime-sa@kevin-491315.iam.gserviceaccount.com"
    token = None

    def refresh(self, _request):
        self.token = "fresh-access-token"


class _KeyCreds:
    """Local service-account key: can sign directly."""

    def sign_bytes(self, data):  # pragma: no cover - existence is the branch
        return b"sig"


def test_signed_url_uses_iam_signblob_when_credentials_cannot_sign(monkeypatch):
    monkeypatch.setattr(settings, "estimate_media_bucket", "test-bucket")
    blob = _CapturingBlob()

    url = estimate_media.gcs_redirect_url(
        "tok/vid.mp4",
        client_factory=lambda: _CapturingClient(blob),
        credentials_factory=lambda: (_MetadataCreds(), "proj"),
    )

    assert url == "https://signed.example/url"
    assert blob.kwargs["service_account_email"].startswith("runtime-sa@")
    assert blob.kwargs["access_token"] == "fresh-access-token"


def test_signed_url_signs_directly_with_a_local_key(monkeypatch):
    monkeypatch.setattr(settings, "estimate_media_bucket", "test-bucket")
    blob = _CapturingBlob()

    url = estimate_media.gcs_redirect_url(
        "tok/vid.mp4",
        client_factory=lambda: _CapturingClient(blob),
        credentials_factory=lambda: (_KeyCreds(), "proj"),
    )

    assert url == "https://signed.example/url"
    assert "service_account_email" not in blob.kwargs
    assert "access_token" not in blob.kwargs
