"""APNs VoIP push notification sender.

Sends VoIP push notifications to the iOS app via Apple Push Notification Service.
Uses HTTP/2 with token-based authentication (.p8 key).
"""

import asyncio
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
APNS_SANDBOX = "https://api.sandbox.push.apple.com"


def _get_apns_url() -> str:
    """Get the correct APNs URL based on apns_sandbox config.

    Must match the aps-environment entitlement in the iOS app.
    Set APNS_SANDBOX=true for dev-signed builds, false for App Store.
    """
    if settings.apns_sandbox:
        logger.debug("Using APNs sandbox endpoint")
        return APNS_SANDBOX
    return APNS_PRODUCTION


_cached_apns_token = None
_cached_apns_token_expiry = 0


def _generate_apns_token() -> str:
    """Generate a JWT token for APNs authentication.

    Uses the .p8 key file from Apple Developer Portal.
    Token is valid for 1 hour; cached for 50 minutes.
    """
    global _cached_apns_token, _cached_apns_token_expiry

    now = int(time.time())

    if _cached_apns_token and now < _cached_apns_token_expiry:
        return _cached_apns_token

    payload = {
        "iss": settings.apns_team_id,
        "iat": now,
    }
    headers = {
        "alg": "ES256",
        "kid": settings.apns_key_id,
    }

    # Handle different encoding formats for the key content
    key_content = settings.apns_key_content
    if "|" in key_content:
        key_content = key_content.replace("|", "\n")
    elif "\\n" in key_content:
        key_content = key_content.replace("\\n", "\n")

    token = jwt.encode(
        payload,
        key_content,
        algorithm="ES256",
        headers=headers,
    )

    _cached_apns_token = token
    _cached_apns_token_expiry = now + 3000  # 50 minutes

    return token


async def _delete_expired_device_token(device_token: str):
    """Remove an expired device token from Firestore."""
    try:
        from app.db.firestore_client import get_firestore_client
        db = get_firestore_client()
        loop = asyncio.get_event_loop()
        doc = await loop.run_in_executor(
            None, lambda: db.collection("devices").document("primary").get()
        )
        if doc.exists:
            data = doc.to_dict()
            updates = {}
            if data.get("push_token") == device_token:
                updates["push_token"] = ""
            if data.get("voip_token") == device_token:
                updates["voip_token"] = ""
            if updates:
                await loop.run_in_executor(
                    None, lambda: db.collection("devices").document("primary").update(updates)
                )
                logger.info(f"Deleted expired device token {device_token[:8]}... from Firestore")
    except Exception as e:
        logger.error(f"Failed to delete expired device token: {e}")


async def send_voip_push(
    device_token: str,
    caller_phone: str,
    caller_name: str = "",
    reason: str = "",
    call_sid: str = "",
    conference_name: str = "",
    access_token: str = "",
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
        "aps": {
            "alert": {
                "title": "Incoming Call",
                "body": caller_name or caller_phone,
            },
        },
        "call_sid": call_sid,
        "caller_phone": caller_phone,
        "caller_name": caller_name,
        "reason": reason,
        "access_token": access_token,
        "conference_name": conference_name,
    }

    for attempt in range(2):
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
                    logger.warning(f"Device token expired (410): {device_token[:8]}...")
                    await _delete_expired_device_token(device_token)
                    return False
                else:
                    logger.error(f"APNs push failed: {response.status_code} {response.text}")
                    if attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    return False

        except Exception as e:
            logger.error(f"APNs push error: {e}", exc_info=True)
            if attempt == 0:
                await asyncio.sleep(2)
                continue
            return False

    return False


async def send_regular_push(
    device_token: str,
    title: str = "Kevin",
    body: str = "",
    call_sid: str = "",
    caller_phone: str = "",
    caller_name: str = "",
) -> bool:
    """Send a regular APNs push notification (banner, not CallKit)."""
    if not device_token or not settings.apns_key_content:
        logger.warning("APNs not configured — cannot send push")
        return False

    apns_url = _get_apns_url()
    topic = settings.apns_bundle_id  # Regular push uses bundle ID, not .voip

    payload = {
        "aps": {
            "alert": {
                "title": title,
                "body": body,
            },
            "sound": "default",
            "content-available": 1,
        },
        "call_sid": call_sid,
        "caller_phone": caller_phone,
        "caller_name": caller_name,
    }

    for attempt in range(2):
        try:
            token = _generate_apns_token()

            async with httpx.AsyncClient(http2=True) as client:
                response = await client.post(
                    f"{apns_url}/3/device/{device_token}",
                    headers={
                        "authorization": f"bearer {token}",
                        "apns-topic": topic,
                        "apns-push-type": "alert",
                        "apns-priority": "10",
                        "apns-expiration": "0",
                    },
                    content=json.dumps(payload),
                    timeout=10.0,
                )

                if response.status_code == 200:
                    logger.info(f"Push notification sent to {device_token[:8]}...")
                    return True
                elif response.status_code == 410:
                    logger.warning(f"Device token expired (410): {device_token[:8]}...")
                    await _delete_expired_device_token(device_token)
                    return False
                else:
                    logger.error(f"APNs push failed: {response.status_code} {response.text}")
                    if attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    return False

        except Exception as e:
            logger.error(f"APNs push error: {e}", exc_info=True)
            if attempt == 0:
                await asyncio.sleep(2)
                continue
            return False

    return False


async def send_urgent_push(
    device_token: str,
    title: str = "URGENT CALL",
    body: str = "",
    call_sid: str = "",
    caller_phone: str = "",
    caller_name: str = "",
) -> bool:
    """Send a critical-priority APNs push for urgent/emergency calls.

    Uses interruption-level: critical to break through Do Not Disturb.
    """
    if not device_token or not settings.apns_key_content:
        return False

    apns_url = _get_apns_url()
    topic = settings.apns_bundle_id

    payload = {
        "aps": {
            "alert": {
                "title": title,
                "body": body,
            },
            "sound": {"critical": 1, "name": "default", "volume": 1.0},
            "interruption-level": "critical",
            "content-available": 1,
        },
        "call_sid": call_sid,
        "caller_phone": caller_phone,
        "caller_name": caller_name,
        "urgent": True,
    }

    for attempt in range(2):
        try:
            token = _generate_apns_token()

            async with httpx.AsyncClient(http2=True) as client:
                response = await client.post(
                    f"{apns_url}/3/device/{device_token}",
                    headers={
                        "authorization": f"bearer {token}",
                        "apns-topic": topic,
                        "apns-push-type": "alert",
                        "apns-priority": "10",
                        "apns-expiration": "0",
                    },
                    content=json.dumps(payload),
                    timeout=10.0,
                )

                if response.status_code == 200:
                    logger.info(f"URGENT push sent to {device_token[:8]}...")
                    return True
                elif response.status_code == 410:
                    logger.warning(f"Device token expired (410): {device_token[:8]}...")
                    await _delete_expired_device_token(device_token)
                    return False
                else:
                    logger.error(f"Urgent APNs push failed: {response.status_code} {response.text}")
                    if attempt == 0:
                        await asyncio.sleep(2)
                        continue
                    return False

        except Exception as e:
            logger.error(f"Urgent APNs push error: {e}", exc_info=True)
            if attempt == 0:
                await asyncio.sleep(2)
                continue
            return False

    return False


async def get_device_token(token_type: str = "push", contractor_id: str = "") -> Optional[str]:
    """Get the device token (push or voip) from Firestore.

    Args:
        token_type: "push" for regular push notifications, "voip" for VoIP push.
        contractor_id: If provided, look up per-contractor tokens from
            contractors/{contractor_id}/devices/primary instead of global devices/primary.
    """
    try:
        from app.db.firestore_client import get_firestore_client
        import asyncio
        db = get_firestore_client()
        loop = asyncio.get_event_loop()

        if contractor_id:
            path = f"contractors/{contractor_id}/devices/primary"
            doc = await loop.run_in_executor(
                None, lambda: db.document(path).get()
            )
        else:
            doc = await loop.run_in_executor(
                None, lambda: db.collection("devices").document("primary").get()
            )

        if doc.exists:
            data = doc.to_dict()
            if token_type == "voip":
                return data.get("voip_token", "")
            return data.get("push_token", "")
    except Exception as e:
        logger.error(f"Failed to get device token: {e}", exc_info=True)
    return None
