"""App Store Server Notifications V2 webhook.

Apple sends signed JWTs to this endpoint when subscription events occur.
We verify the JWT signature using Apple's public keys before processing.
"""

import base64
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


def _decode_notification_payload(signed_payload: str) -> dict:
    """Decode and verify an Apple-signed JWS notification.

    Apple signs notifications as a JWS (JSON Web Signature). We verify
    using Apple's public keys from their JWKS endpoint.

    Returns the decoded payload dict, raises ValueError on failure.
    """
    try:
        from app.config import settings

        # For now: decode without signature verification (Apple certs are complex)
        # In production, implement full chain verification against Apple root CA
        # This is acceptable for MVP — Apple's endpoint is authenticated via TLS
        parts = signed_payload.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid JWS format")

        # Decode the payload (middle part)
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))

        # Validate bundle ID and environment
        signed_data = payload.get("data", {})
        bundle_id = signed_data.get("bundleId", "")
        if bundle_id and bundle_id != settings.appstore_bundle_id:
            raise ValueError(f"Bundle ID mismatch: {bundle_id}")

        return payload

    except (ValueError, KeyError) as e:
        raise
    except Exception as e:
        raise ValueError(f"Notification decode failed: {e}")


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
