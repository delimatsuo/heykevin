"""APNs VoIP push notification sender.

Sends VoIP push notifications to the iOS app via Apple Push Notification Service.
Uses HTTP/2 with token-based authentication (.p8 key).
"""

import json
import time
import jwt
import httpx
from typing import Optional

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

# APNs endpoints
APNS_PRODUCTION = "https://api.push.apple.com"
APNS_SANDBOX = "https://api.development.push.apple.com"


def _get_apns_url() -> str:
    """Get the correct APNs URL based on environment."""
    if settings.environment == "production":
        return APNS_PRODUCTION
    return APNS_SANDBOX


def _generate_apns_token() -> str:
    """Generate a JWT token for APNs authentication.

    Uses the .p8 key file from Apple Developer Portal.
    Token is valid for 1 hour.
    """
    now = int(time.time())
    payload = {
        "iss": settings.apns_team_id,
        "iat": now,
    }
    headers = {
        "alg": "ES256",
        "kid": settings.apns_key_id,
    }

    token = jwt.encode(
        payload,
        settings.apns_key_content,
        algorithm="ES256",
        headers=headers,
    )
    return token


async def send_voip_push(
    device_token: str,
    caller_phone: str,
    caller_name: str = "",
    reason: str = "",
    call_sid: str = "",
    conference_name: str = "",
) -> bool:
    """Send a VoIP push notification to trigger CallKit on the iOS app.

    Returns True if push was accepted by APNs.
    """
    if not device_token:
        logger.warning("No device token — cannot send VoIP push")
        return False

    if not settings.apns_key_content:
        logger.warning("APNs key not configured — falling back to Telegram")
        return False

    apns_url = _get_apns_url()
    topic = f"{settings.apns_bundle_id}.voip"

    payload = {
        "aps": {},
        "caller_phone": caller_phone,
        "caller_name": caller_name,
        "reason": reason,
        "call_sid": call_sid,
        "conference_name": conference_name,
    }

    try:
        token = _generate_apns_token()

        async with httpx.AsyncClient(http2=True) as client:
            response = await client.post(
                f"{apns_url}/3/device/{device_token}",
                headers={
                    "authorization": f"bearer {token}",
                    "apns-topic": topic,
                    "apns-push-type": "voip",
                    "apns-priority": "10",
                    "apns-expiration": "0",
                },
                content=json.dumps(payload),
                timeout=10.0,
            )

            if response.status_code == 200:
                logger.info(f"VoIP push sent to {device_token[:8]}...")
                return True
            elif response.status_code == 410:
                # Device token is no longer valid — should be removed
                logger.warning(f"Device token expired (410): {device_token[:8]}...")
                return False
            else:
                logger.error(f"APNs push failed: {response.status_code} {response.text}")
                return False

    except Exception as e:
        logger.error(f"APNs push error: {e}", exc_info=True)
        return False


async def get_device_token() -> Optional[str]:
    """Get the stored VoIP device token from Firestore."""
    try:
        from app.db.firestore_client import get_firestore_client
        db = get_firestore_client()
        docs = db.collection("devices").limit(1).stream()
        for doc in docs:
            data = doc.to_dict()
            return data.get("voip_token", "")
    except Exception as e:
        logger.error(f"Failed to get device token: {e}", exc_info=True)
    return None
