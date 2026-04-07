"""VoIP API — device registration, Twilio access tokens, call actions."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant

from app.config import settings
from app.middleware.auth import verify_api_token
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api")


class DeviceRegister(BaseModel):
    voip_token: str
    platform: str = "ios"


class VoIPTokenRequest(BaseModel):
    call_sid: str
    conference_name: str


class CallAction(BaseModel):
    call_sid: str
    action: str  # accept, decline, voicemail, text_reply


@router.post("/register-device")
async def register_device(body: DeviceRegister):
    """Register an iOS device's VoIP push token."""
    try:
        from app.db.firestore_client import get_firestore_client
        db = get_firestore_client()

        import time
        db.collection("devices").document("primary").set({
            "voip_token": body.voip_token,
            "platform": body.platform,
            "registered_at": time.time(),
        })

        logger.info(f"Device registered: {body.voip_token[:8]}... ({body.platform})")
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Device registration failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/voip-token")
async def get_voip_token(body: VoIPTokenRequest):
    """Generate a Twilio access token for the iOS app to join a conference.

    The token includes a Voice Grant that allows the app to make an
    outgoing call to our TwiML App, which returns Conference TwiML.
    """
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
        return {"status": "error", "message": str(e)}


@router.post("/call-action")
async def handle_call_action(body: CallAction):
    """Handle an action from the iOS app (accept, decline, voicemail, text_reply)."""
    logger.info(f"Call action: {body.action} for {body.call_sid}")

    try:
        if body.action == "accept":
            return await _handle_accept(body.call_sid)
        elif body.action == "decline":
            return await _handle_decline(body.call_sid)
        elif body.action == "voicemail":
            return await _handle_voicemail(body.call_sid)
        elif body.action == "text_reply":
            return await _handle_text_reply(body.call_sid)
        else:
            return {"status": "error", "message": f"Unknown action: {body.action}"}

    except Exception as e:
        logger.error(f"Call action failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


async def _handle_accept(call_sid: str) -> dict:
    """Accept a call — redirect caller to conference, return conference name."""
    import secrets
    from twilio.rest import Client

    conference_name = secrets.token_urlsafe(16)

    # Redirect the caller's Twilio call from <Stream> to <Conference>
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    conf_twiml = f"""<Response>
<Say voice="Polly.Joey">Connecting you now.</Say>
<Dial><Conference startConferenceOnEnter="true" endConferenceOnExit="true" beep="false">{conference_name}</Conference></Dial>
</Response>"""

    client.calls(call_sid).update(twiml=conf_twiml)
    logger.info(f"Caller redirected to conference: {conference_name}")

    return {
        "status": "ok",
        "conference_name": conference_name,
    }


async def _handle_decline(call_sid: str) -> dict:
    """Decline — tell Kevin to take a message (call continues with Gemini)."""
    # The Gemini session is still active on <Connect><Stream>
    # We don't need to do anything — Kevin will naturally offer to take a message
    # after the timeout (per the system prompt)
    logger.info("Call declined — Kevin will take message")
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


async def _handle_text_reply(call_sid: str) -> dict:
    """Send text reply SMS to the caller."""
    from app.db.cache import get_active_call
    from app.services.sms import send_text_reply

    active_call = await get_active_call(call_sid)
    if active_call and active_call.caller_phone:
        send_text_reply(active_call.caller_phone)
        logger.info(f"Text reply sent to {active_call.caller_phone}")

    return {"status": "ok"}
