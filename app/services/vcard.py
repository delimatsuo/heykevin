"""vCard generation for contractor contact sharing.

The HMAC secret used to sign vCard download URLs is loaded from the dedicated
``VCARD_HMAC_SECRET`` environment variable (F-08). It is intentionally
decoupled from ``API_BEARER_TOKEN``: rotating the admin bearer token must not
silently invalidate already-shared vCard links, and a leak of the bearer token
must not be sufficient to forge vCard URLs offline.

For backwards-compatibility on environments that have not yet set the new var
we fall back to a value derived from ``api_bearer_token`` and emit a clear
warning at first use. In production the absence of ``VCARD_HMAC_SECRET`` is
also logged at WARNING level on the very first signing call so it surfaces in
operational dashboards.
"""

import hashlib
import hmac
import time

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

VCARD_EXPIRY_SECONDS = 30 * 86400  # 30 days

_FALLBACK_DEFAULT = b"kevin-vcard-secret"

# Module-level memo so the warning fires once per process, not on every call.
_warned_about_missing_secret = False


def _resolve_vcard_secret() -> bytes:
    """Return the HMAC key for signing vCard URLs.

    Order of preference:
      1. ``VCARD_HMAC_SECRET`` (the documented, dedicated secret).
      2. Derived value from ``API_BEARER_TOKEN`` (legacy behaviour).
      3. A constant fallback string. This last branch is only meaningful in
         test environments — it is logged loudly when reached.
    """
    global _warned_about_missing_secret
    secret = (settings.vcard_hmac_secret or "").strip()
    if secret:
        return secret.encode()

    bearer = (settings.api_bearer_token or "").strip()
    if bearer:
        if not _warned_about_missing_secret:
            env = (settings.environment or "").strip().lower()
            level = "ERROR" if env == "production" else "WARNING"
            logger.warning(
                "VCARD_HMAC_SECRET is not set; falling back to a key derived "
                "from API_BEARER_TOKEN. Configure VCARD_HMAC_SECRET on Cloud Run "
                "to decouple vCard signing from the admin bearer token. "
                "(severity=%s, environment=%s)",
                level,
                env or "unknown",
            )
            _warned_about_missing_secret = True
        # Hash the bearer token so we don't reuse the raw secret material as
        # an HMAC key — adds a small amount of separation when both are set.
        return hashlib.sha256(b"vcard|" + bearer.encode()).digest()

    if not _warned_about_missing_secret:
        logger.warning(
            "VCARD_HMAC_SECRET and API_BEARER_TOKEN are both unset; "
            "vCard URLs are signed with a constant fallback. This must never "
            "happen in production."
        )
        _warned_about_missing_secret = True
    return _FALLBACK_DEFAULT


def generate_vcard(contractor: dict) -> str:
    """Generate a vCard 3.0 string for a contractor."""
    name = contractor.get("owner_name", "")
    business = contractor.get("business_name", "")
    phone = contractor.get("twilio_number", "")
    service = contractor.get("service_type", "")

    meaningful_service = service and service not in {"general", "personal", "business", "kevin"}
    label = f"{name} - {service}".strip(" -") if meaningful_service else name

    # Escape special vCard characters
    label = label.replace(",", "\\,").replace(";", "\\;")
    business = business.replace(",", "\\,").replace(";", "\\;")

    return (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        f"FN:{label}\r\n"
        f"ORG:{business}\r\n"
        f"TEL;TYPE=WORK:{phone}\r\n"
        "END:VCARD\r\n"
    )


def generate_signed_vcard_url(contractor_id: str) -> str:
    """Generate an HMAC-signed URL for downloading a contractor's vCard."""
    expires = int(time.time()) + VCARD_EXPIRY_SECONDS
    payload = f"{contractor_id}:{expires}"
    sig = hmac.new(_resolve_vcard_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{settings.cloud_run_url}/api/vcard/{contractor_id}.vcf?expires={expires}&sig={sig}"


def verify_vcard_signature(contractor_id: str, expires: int, sig: str) -> bool:
    """Verify an HMAC-signed vCard URL."""
    if time.time() > expires:
        return False
    payload = f"{contractor_id}:{expires}"
    expected = hmac.new(_resolve_vcard_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)
