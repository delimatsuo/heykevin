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
URGENT_VOIP_REASONS = {"urgent", "urgent_call", "emergency", "emergency_call"}


def _get_apns_url() -> str:
    """Get the correct APNs URL based on apns_sandbox config.

    Must match the aps-environment entitlement in the iOS app.
    Set APNS_SANDBOX=true for dev-signed builds, false for App Store.
    """
    if settings.apns_sandbox:
        logger.debug("Using APNs sandbox endpoint")
        return APNS_SANDBOX
    return APNS_PRODUCTION


def _safe_voip_push_body(reason: str = "") -> str:
    """Return lock-screen-safe VoIP alert copy without caller identity."""
    normalized_reason = "_".join((reason or "").strip().lower().replace("-", " ").split())
    if normalized_reason in URGENT_VOIP_REASONS:
        return "Urgent call needs review. Open Kevin for details."
    return "Incoming call. Open Kevin for details."


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


async def _record_app_deletion(contractor_id: str):
    """Stamp deleted_app_detected_at after APNs reports the token is gone.

    APNs 410 means the token is no longer active for this topic. Uninstalling is
    the usual cause, but not the only one — token rotation and device restores can
    also produce it. So this is a strong signal, not proof, and it is deliberately
    only a *starting* condition: number release is separately gated on the number
    having gone silent (see `_expired_contractor_cleanup`), because releasing a
    number that still has live forwarding hands a user's calls to whoever Twilio
    assigns it to next.

    Before this existed, the only writer of this field was the inbound-call path,
    so a user who deleted the app and received no calls was never detected at all —
    and never got the SMS telling them how to turn forwarding off.
    """
    if not contractor_id:
        return
    try:
        from app.db.contractors import get_contractor, update_contractor
        import time

        contractor = await get_contractor(contractor_id)
        if not contractor or contractor.get("deleted_app_detected_at"):
            return
        await update_contractor(contractor_id, {"deleted_app_detected_at": time.time()})
        logger.info(f"App deletion detected via APNs 410: {contractor_id}")
    except Exception as e:
        logger.warning(f"Could not record app deletion: {type(e).__name__}")


async def _delete_expired_device_token(device_token: str, contractor_id: str = ""):
    """Remove an expired device token from Firestore and note the app deletion."""
    await _record_app_deletion(contractor_id)
    try:
        from app.db.firestore_client import get_firestore_client
        db = get_firestore_client()
        loop = asyncio.get_event_loop()
        if contractor_id:
            path = db.collection("contractors").document(contractor_id).collection("devices").document("primary")
        else:
            path = db.collection("devices").document("primary")
        doc = await loop.run_in_executor(None, lambda: path.get())
        if doc.exists:
            data = doc.to_dict()
            updates = {}
            if data.get("push_token") == device_token:
                updates["push_token"] = ""
            if data.get("voip_token") == device_token:
                updates["voip_token"] = ""
            if updates:
                await loop.run_in_executor(None, lambda: path.update(updates))
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
    contractor_id: str = "",
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
                "body": _safe_voip_push_body(reason=reason),
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
                    await _delete_expired_device_token(device_token, contractor_id)
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
    contractor_id: str = "",
    collapse_id: Optional[str] = None,
    category: Optional[str] = None,
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
    if category:
        payload["aps"]["category"] = category

    headers = {
        "apns-topic": topic,
        "apns-push-type": "alert",
        "apns-priority": "10",
        "apns-expiration": "0",
    }
    if collapse_id:
        headers["apns-collapse-id"] = collapse_id

    for attempt in range(2):
        try:
            token = _generate_apns_token()
            req_headers = dict(headers)
            req_headers["authorization"] = f"bearer {token}"

            async with httpx.AsyncClient(http2=True) as client:
                response = await client.post(
                    f"{apns_url}/3/device/{device_token}",
                    headers=req_headers,
                    content=json.dumps(payload),
                    timeout=10.0,
                )

                if response.status_code == 200:
                    logger.info(f"Push notification sent to {device_token[:8]}...")
                    return True
                elif response.status_code == 410:
                    logger.warning(f"Device token expired (410): {device_token[:8]}...")
                    await _delete_expired_device_token(device_token, contractor_id)
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


async def send_screening_summary_push(
    *,
    contractor_id: str,
    call_sid: str,
    caller_phone: str = "",
    caller_name: str = "",
    reason: str = "",
    collapse_id: Optional[str] = None,
) -> bool:
    """Send an in-place screening summary push replacing the incoming call banner.

    Uses `apns-collapse-id` so APNs collapses/updates the existing notification
    in place on the lock screen / Apple Watch without clutter.
    """
    if not contractor_id:
        return False

    device_token = await get_device_token(contractor_id=contractor_id)
    if not device_token:
        logger.warning(f"No push token for contractor {contractor_id} — screening summary not sent")
        return False

    title = caller_name or caller_phone or "Incoming Call"
    if reason:
        body = f"{reason} — Tap to answer"
    else:
        body = "Kevin is screening this call. Tap to answer."

    cid = collapse_id or (f"call_{call_sid}" if call_sid else None)

    return await send_regular_push(
        device_token=device_token,
        title=title,
        body=body,
        call_sid=call_sid,
        caller_phone=caller_phone,
        caller_name=caller_name,
        contractor_id=contractor_id,
        collapse_id=cid,
        category="SCREENING_CALL",
    )


async def send_urgent_push(
    device_token: str,
    title: str = "URGENT CALL",
    body: str = "",
    call_sid: str = "",
    caller_phone: str = "",
    caller_name: str = "",
    contractor_id: str = "",
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
                    await _delete_expired_device_token(device_token, contractor_id)
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
