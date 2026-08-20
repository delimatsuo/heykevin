"""Estimate media persistence in GCS and HMAC-signed watch URLs."""

import datetime
import hashlib
import hmac
import time
from typing import Callable, Optional

from google.cloud import storage

from app.config import settings
from app.services.vcard import _resolve_vcard_secret
from app.utils.logging import get_logger

logger = get_logger(__name__)

MEDIA_WATCH_EXPIRY_SECONDS = 90 * 86400  # 90 days retention matching call records

_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}


def _extension_for_content_type(content_type: str) -> str:
    cleaned = (content_type or "").split(";")[0].strip().lower()
    if cleaned in _CONTENT_TYPE_EXTENSIONS:
        return _CONTENT_TYPE_EXTENSIONS[cleaned]
    if cleaned.startswith("video/"):
        return ".mp4"
    if cleaned.startswith("image/"):
        return ".jpg"
    return ".bin"


def archive_media(
    token_hash: str,
    media_id: str,
    media_bytes: bytes,
    content_type: str,
    client_factory: Optional[Callable[[], storage.Client]] = None,
) -> Optional[str]:
    """Archive media to GCS bucket.

    Writes to gs://{ESTIMATE_MEDIA_BUCKET}/{token_hash}/{media_id}.{ext}.
    Returns the object path ({token_hash}/{media_id}.{ext}), or None when
    ESTIMATE_MEDIA_BUCKET is unset (degraded mode: analysis runs without archive).
    """
    bucket_name = (settings.estimate_media_bucket or "").strip()
    if not bucket_name:
        return None

    ext = _extension_for_content_type(content_type)
    object_path = f"{token_hash}/{media_id}{ext}"

    client = client_factory() if client_factory is not None else storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_path)
    blob.upload_from_string(media_bytes, content_type=content_type)
    return object_path


def make_watch_url(media_id: str) -> str:
    """Generate an HMAC-signed watch URL for the owner with 90-day expiry."""
    expires = int(time.time()) + MEDIA_WATCH_EXPIRY_SECONDS
    payload = f"estimate-media:{media_id}:{expires}"
    sig = hmac.new(_resolve_vcard_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{settings.cloud_run_url}/api/estimates/media/{media_id}?e={expires}&s={sig}"


def verify_watch_sig(media_id: str, expires: int, sig: str) -> bool:
    """Verify HMAC signature and expiry for a watch URL."""
    try:
        expires_int = int(expires)
    except (ValueError, TypeError):
        return False

    if time.time() > expires_int:
        return False

    payload = f"estimate-media:{media_id}:{expires_int}"
    expected = hmac.new(_resolve_vcard_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig or "", expected)


def gcs_redirect_url(
    object_path: str,
    client_factory: Optional[Callable[[], storage.Client]] = None,
) -> str:
    """Generate a V4 signed GET URL with 1-hour expiry for GCS media playback."""
    bucket_name = (settings.estimate_media_bucket or "").strip()
    if not bucket_name or not object_path:
        return ""

    client = client_factory() if client_factory is not None else storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_path)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(hours=1),
        method="GET",
    )


def read_media(
    object_path: str,
    client_factory: Optional[Callable[[], storage.Client]] = None,
) -> bytes:
    """Download archived media bytes from GCS."""
    bucket_name = (settings.estimate_media_bucket or "").strip()
    if not bucket_name:
        raise RuntimeError("ESTIMATE_MEDIA_BUCKET is unset")

    client = client_factory() if client_factory is not None else storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_path)
    return blob.download_as_bytes()
