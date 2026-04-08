"""vCard generation for contractor contact sharing."""

import hashlib
import hmac
import time

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

# HMAC secret for signing vCard URLs (derived from bearer token)
_VCARD_SECRET = (settings.api_bearer_token or "kevin-vcard-secret").encode()
VCARD_EXPIRY_SECONDS = 30 * 86400  # 30 days


def generate_vcard(contractor: dict) -> str:
    """Generate a vCard 3.0 string for a contractor."""
    name = contractor.get("owner_name", "")
    business = contractor.get("business_name", "")
    phone = contractor.get("twilio_number", "")
    service = contractor.get("service_type", "")

    label = f"{name} - {service}".strip(" -") if service and service != "general" else name

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
    sig = hmac.new(_VCARD_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{settings.cloud_run_url}/api/vcard/{contractor_id}.vcf?expires={expires}&sig={sig}"


def verify_vcard_signature(contractor_id: str, expires: int, sig: str) -> bool:
    """Verify an HMAC-signed vCard URL."""
    if time.time() > expires:
        return False
    payload = f"{contractor_id}:{expires}"
    expected = hmac.new(_VCARD_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)
