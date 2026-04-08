"""VoIP API — device registration, Twilio access tokens, call actions."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from typing import Optional

from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import VoiceResponse, Dial

from app.config import settings
from app.middleware.auth import verify_api_token, require_contractor_access
from app.utils.logging import get_logger, redact_phone

logger = get_logger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_token)])


@router.get("/active-call")
async def check_active_call(request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """Check if there's a call currently being screened for this contractor."""
    require_contractor_access(request, contractor_id)
    try:
        from app.db.cache import _init_firebase, ACTIVE_CALLS_PATH, STALE_THRESHOLD
        from firebase_admin import db as rtdb
        import time

        import asyncio
        _init_firebase()
        ref = rtdb.reference(ACTIVE_CALLS_PATH)
        loop = asyncio.get_event_loop()
        all_calls = await loop.run_in_executor(None, ref.get)
        if not all_calls:
            return {"active": False}

        # Find the most recent active call for this contractor
        latest = None
        latest_time = 0
        latest_sid = ""
        for call_sid, data in all_calls.items():
            if not isinstance(data, dict):
                continue
            # Filter by contractor
            if data.get("contractor_id", "") != contractor_id:
                continue
            state = data.get("state", "")
            updated_at = data.get("state_updated_at", 0)
            if state in ("screening", "pending") and updated_at > latest_time:
                if time.time() - updated_at < STALE_THRESHOLD:
                    latest = data
                    latest_time = updated_at
                    latest_sid = call_sid

        if latest:
            return {
                "active": True,
                "call_sid": latest.get("call_sid", latest_sid),
                "caller_phone": latest.get("caller_phone", ""),
                "caller_name": latest.get("caller_name", ""),
                "transcript": latest.get("transcript_buffer", ""),
            }

        return {"active": False}
    except Exception as e:
        logger.error(f"Active call check failed: {e}")
        return {"active": False}


@router.get("/transcript/{call_sid}")
async def get_transcript(call_sid: str, request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """Get the live transcript for an active call from RTDB."""
    require_contractor_access(request, contractor_id)
    try:
        from app.db.cache import get_active_call
        active_call = await get_active_call(call_sid)
        if active_call:
            # Verify call belongs to this contractor
            if active_call.contractor_id != contractor_id:
                return {"transcript": ""}
            return {"transcript": active_call.transcript_buffer or ""}
        return {"transcript": ""}
    except Exception as e:
        logger.error(f"Transcript fetch failed: {e}")
        return {"transcript": ""}


@router.get("/jobs")
async def api_list_jobs(request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """List recent job cards for a contractor."""
    require_contractor_access(request, contractor_id)
    from app.db.jobs import list_jobs
    jobs = await list_jobs(limit=20, contractor_id=contractor_id)
    return {"jobs": jobs}


@router.get("/jobs/{job_id}")
async def api_get_job(job_id: str, request: Request, contractor_id: str = Query(..., description="Contractor ID")):
    """Get a specific job card."""
    require_contractor_access(request, contractor_id)
    from app.db.jobs import get_job
    job = await get_job(job_id, contractor_id=contractor_id)
    if not job:
        return {"error": "Not found"}
    return job


class DeviceRegister(BaseModel):
    push_token: str = ""
    voip_token: str = ""  # VoIP push token for CallKit
    platform: str = "ios"
    contractor_id: str = ""  # Required — store tokens per-contractor
    timezone: str = ""  # IANA timezone from device (e.g., "America/New_York")
    language: str = ""  # ISO language code from device (e.g., "en", "es", "fr")


class VoIPTokenRequest(BaseModel):
    call_sid: str
    conference_name: str


class CallAction(BaseModel):
    call_sid: str
    action: str  # accept, decline, voicemail, text_reply
    message: str = ""  # Custom message for text_reply


@router.post("/register-device")
async def register_device(request: Request, body: DeviceRegister):
    """Register an iOS device's push and/or VoIP token."""
    if not body.contractor_id:
        return {"status": "error", "message": "contractor_id required"}
    require_contractor_access(request, body.contractor_id)
    try:
        from app.db.firestore_client import get_firestore_client
        db = get_firestore_client()

        import time
        data = {
            "platform": body.platform,
            "registered_at": time.time(),
        }
        if body.push_token:
            data["push_token"] = body.push_token
        if body.voip_token:
            data["voip_token"] = body.voip_token

        # Per-contractor device tokens: contractors/{id}/devices/primary
        db.document(f"contractors/{body.contractor_id}/devices/primary").set(data, merge=True)

        # Save timezone and language to contractor doc (updates on every app launch)
        device_updates = {}
        if body.timezone:
            device_updates["timezone"] = body.timezone
        if body.language:
            device_updates["user_language"] = body.language
        if device_updates:
            from app.db.contractors import update_contractor
            await update_contractor(body.contractor_id, device_updates)

        token_preview = (body.push_token or body.voip_token)[:8] if (body.push_token or body.voip_token) else "none"
        logger.info(f"Device registered: {token_preview}... ({body.platform}) contractor={body.contractor_id} tz={body.timezone or 'not set'} lang={body.language or 'not set'}")
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Device registration failed: {e}", exc_info=True)
        return {"status": "error", "message": "Internal error"}


@router.post("/voip-token")
async def get_voip_token(request: Request, body: VoIPTokenRequest, contractor_id: str = Query(..., description="Contractor ID")):
    """Generate a Twilio access token for the iOS app to join a conference.

    The token includes a Voice Grant that allows the app to make an
    outgoing call to our TwiML App, which returns Conference TwiML.
    """
    require_contractor_access(request, contractor_id)
    try:
        # Create an access token
        token = AccessToken(
            settings.twilio_account_sid,
            settings.twilio_api_key_sid,
            settings.twilio_api_key_secret,
            identity="kevin-user",
            ttl=600,  # 10 minutes
        )

        # Add Voice Grant with our TwiML App SID
        voice_grant = VoiceGrant(
            outgoing_application_sid=settings.twilio_twiml_app_sid,
            incoming_allow=False,
        )
        token.add_grant(voice_grant)

        logger.info(f"VoIP token generated for call {body.call_sid}")

        return {
            "token": token.to_jwt(),
            "call_sid": body.call_sid,
            "conference_name": body.conference_name,
        }

    except Exception as e:
        logger.error(f"VoIP token generation failed: {e}", exc_info=True)
        return {"status": "error", "message": "Internal error"}


@router.post("/call-action")
async def handle_call_action(request: Request, body: CallAction, contractor_id: str = Query(..., description="Contractor ID")):
    """Handle an action from the iOS app (accept, decline, voicemail, text_reply)."""
    require_contractor_access(request, contractor_id)

    # Verify the call belongs to this contractor
    try:
        from app.db.cache import get_active_call
        active_call = await get_active_call(body.call_sid)
        if active_call and active_call.contractor_id != contractor_id:
            logger.warning(f"Call action denied: call {body.call_sid} does not belong to contractor {contractor_id}")
            return {"status": "error", "message": "Access denied"}
    except Exception as e:
        logger.error(f"Call ownership check failed: {e}", exc_info=True)
        return {"status": "error", "message": "Internal error"}

    logger.info(f"Call action: {body.action} for {body.call_sid}")

    try:
        if body.action == "accept":
            return await _handle_accept(body.call_sid)
        elif body.action == "decline":
            return await _handle_decline(body.call_sid)
        elif body.action == "voicemail":
            return await _handle_voicemail(body.call_sid)
        elif body.action == "text_reply":
            return await _handle_text_reply(body.call_sid, body.message, contractor_id)
        else:
            return {"status": "error", "message": f"Unknown action: {body.action}"}

    except Exception as e:
        logger.error(f"Call action failed: {e}", exc_info=True)
        return {"status": "error", "message": "Internal error"}


def _generate_access_token() -> str:
    """Generate a Twilio Voice access token for the iOS SDK."""
    token = AccessToken(
        settings.twilio_account_sid,
        settings.twilio_api_key_sid,
        settings.twilio_api_key_secret,
        identity="kevin-contractor",
        ttl=120,  # 2 minutes
    )

    voice_grant = VoiceGrant(
        outgoing_application_sid=settings.twilio_twiml_app_sid,
        incoming_allow=True,
    )
    token.add_grant(voice_grant)

    jwt_token = token.to_jwt()
    # Ensure string (some Twilio SDK versions return bytes)
    return jwt_token if isinstance(jwt_token, str) else jwt_token.decode("utf-8")


async def _handle_accept(call_sid: str) -> dict:
    """Accept a call — move caller to conference, return token for direct SDK connection.

    The iOS app connects directly via Twilio Voice SDK using the returned
    access_token and conference_name. No VoIP push needed — the user already
    tapped 'Pick Up' in the app.
    """
    import secrets
    from twilio.rest import Client
    import asyncio

    conference_name = f"pickup_{secrets.token_urlsafe(8)}"

    # Redirect the caller's Twilio call from <Stream> to <Conference>
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    response = VoiceResponse()
    response.say("One moment please.", voice="Polly.Matthew")
    dial = Dial(time_limit=5400)  # 90 min max — Twilio enforces server-side
    dial.conference(
        conference_name,
        start_conference_on_enter=True,
        end_conference_on_exit=True,
        beep=False,
        wait_url="http://twimlets.com/holdmusic?Bucket=com.twilio.music.soft-rock",
    )
    response.append(dial)
    conf_twiml = str(response)

    # Mark call as accepted in RTDB BEFORE redirecting — the media stream checks this
    from app.db.cache import update_active_call
    await update_active_call(call_sid, {"accepted": True, "conference_name": conference_name})

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: client.calls(call_sid).update(twiml=conf_twiml))
    logger.info(f"Caller redirected to conference: {conference_name}")

    # Return access token so the iOS app can connect directly via SDK
    access_token = _generate_access_token()

    return {
        "status": "ok",
        "conference_name": conference_name,
        "access_token": access_token,
    }


async def _handle_decline(call_sid: str) -> dict:
    """Write take_message command to RTDB for the voice pipeline to pick up."""
    from app.db.cache import _init_firebase
    from firebase_admin import db as rtdb
    import asyncio

    _init_firebase()
    ref = rtdb.reference(f"/call_commands/{call_sid}")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, ref.set, {"type": "take_message"})
    logger.info(f"Take-message command queued for {call_sid}")
    return {"status": "ok"}


async def _handle_voicemail(call_sid: str) -> dict:
    """Route to voicemail."""
    from twilio.rest import Client

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    voicemail_twiml = """<Response>
<Say voice="Polly.Joey">Please leave a message after the beep.</Say>
<Record maxLength="120" playBeep="true" transcribe="true"/>
<Say>Thank you. Goodbye.</Say>
</Response>"""

    client.calls(call_sid).update(twiml=voicemail_twiml)
    logger.info("Call redirected to voicemail")
    return {"status": "ok"}


async def _handle_text_reply(call_sid: str, message: str = "", contractor_id: str = "") -> dict:
    """Send text reply SMS to the caller."""
    from app.db.cache import get_active_call
    from app.services.sms import send_sms

    active_call = await get_active_call(call_sid)
    if not active_call or not active_call.caller_phone:
        return {"status": "error", "message": "No active call or caller phone"}

    # Use contractor's Twilio number if available
    from_number = ""
    if contractor_id:
        from app.db.contractors import get_contractor
        contractor = await get_contractor(contractor_id)
        if contractor:
            from_number = contractor.get("twilio_number", "")

    body = message.strip() if message.strip() else "Can't talk right now. What's up?"
    success = await send_sms(active_call.caller_phone, body, from_number=from_number)
    if success:
        logger.info(f"Text reply sent to {redact_phone(active_call.caller_phone)}")
        return {"status": "ok"}
    return {"status": "error", "message": "SMS send failed"}
