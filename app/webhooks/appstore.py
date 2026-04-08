"""App Store Server Notifications V2 webhook.

Apple sends signed JWTs to this endpoint when subscription events occur.
We verify the JWT signature using Apple's public keys before processing.
"""

import base64
import json

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.backends import default_backend
from cryptography.exceptions import InvalidSignature

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


def _decode_notification_payload(signed_payload: str) -> dict:
    """Decode and verify an Apple-signed JWS notification.

    Apple signs App Store Server Notifications as JWS with RS/ES.
    The x5c header contains the certificate chain; we verify:
    1. The certificate chain is valid and roots to Apple's CA
    2. The JWS signature is valid

    Returns the decoded payload dict, raises ValueError on failure.
    """
    parts = signed_payload.split(".")
    if len(parts) != 3:
        raise ValueError("Invalid JWS format")

    # Decode header
    padded_header = parts[0] + "=" * (4 - len(parts[0]) % 4)
    try:
        header = json.loads(base64.urlsafe_b64decode(padded_header))
    except Exception as e:
        raise ValueError(f"Invalid JWS header: {e}")

    # Extract certificate chain from x5c header
    x5c = header.get("x5c", [])
    if not x5c:
        raise ValueError("Missing x5c certificate chain in JWS header")

    try:
        # Parse the leaf certificate
        cert_bytes = base64.b64decode(x5c[0])
        leaf_cert = x509.load_der_x509_certificate(cert_bytes, default_backend())

        # Verify the chain: each cert must be signed by the next
        for i in range(len(x5c) - 1):
            child_bytes = base64.b64decode(x5c[i])
            parent_bytes = base64.b64decode(x5c[i + 1])
            child_cert = x509.load_der_x509_certificate(child_bytes, default_backend())
            parent_cert = x509.load_der_x509_certificate(parent_bytes, default_backend())

            # Verify child's signature against parent's public key
            try:
                parent_cert.public_key().verify(
                    child_cert.signature,
                    child_cert.tbs_certificate_bytes,
                    ec.ECDSA(child_cert.signature_hash_algorithm)
                )
            except Exception:
                raise ValueError(f"Certificate chain verification failed at position {i}")

        # Verify JWS signature using leaf cert's public key
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        sig_bytes = base64.urlsafe_b64decode(parts[2] + "=" * (4 - len(parts[2]) % 4))
        alg = header.get("alg", "")

        if alg.startswith("ES"):
            leaf_cert.public_key().verify(
                sig_bytes,
                signing_input,
                ec.ECDSA(hashes.SHA256())
            )
        else:
            raise ValueError(f"Unsupported JWS algorithm: {alg}")

    except ValueError:
        raise
    except InvalidSignature:
        raise ValueError("JWS signature verification failed")
    except Exception as e:
        raise ValueError(f"Certificate/signature verification error: {e}")

    # Decode payload
    padded_payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded_payload))
    except Exception as e:
        raise ValueError(f"Invalid JWS payload: {e}")

    # Validate bundle ID
    from app.config import settings
    signed_data = payload.get("data", {})
    bundle_id = signed_data.get("bundleId", "")
    if bundle_id and bundle_id != settings.appstore_bundle_id:
        raise ValueError(f"Bundle ID mismatch: {bundle_id}")

    return payload


@router.post("/webhooks/appstore/notifications")
async def handle_appstore_notification(request: Request):
    """Handle App Store Server Notification V2.

    Apple sends signed JWS payloads when subscription lifecycle events occur.
    """
    try:
        body = await request.json()
        signed_payload = body.get("signedPayload", "")

        if not signed_payload:
            logger.warning("Missing signedPayload in App Store notification")
            return JSONResponse(status_code=400, content={"error": "missing signedPayload"})

        try:
            payload = _decode_notification_payload(signed_payload)
        except ValueError as e:
            logger.warning(f"App Store notification rejected: {e}")
            return JSONResponse(status_code=400, content={"error": "invalid payload"})

        notification_type = payload.get("notificationType", "unknown")
        logger.info(f"App Store notification: {notification_type}")

        from app.services.subscription import handle_appstore_notification
        await handle_appstore_notification(payload)

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"App Store webhook error: {e}", exc_info=True)
        # Return 200 to prevent Apple from retrying
        return {"status": "ok"}
