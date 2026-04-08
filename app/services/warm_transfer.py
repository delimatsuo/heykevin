"""Warm transfer (Pick Up) flow for v2 (Media Streams architecture).

When the caller is connected via <Connect><Stream>, we own the Twilio call.
To pick up:
1. User taps Pick Up → we generate a PIN and conference name
2. We redirect the caller's Twilio call from <Stream> into a <Conference>
   (this disconnects the Gemini stream but keeps the caller on the line)
3. User calls the dial-in number, enters PIN → joins the same conference
4. Caller and user are talking.
"""

import asyncio
import secrets

from twilio.rest import Client

from app.config import settings, get_dial_in_number
from app.db.cache import get_active_call, transition_state, update_active_call
from app.services.state_machine import CallState
from app.services.telegram_bot import send_dial_in_message
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _get_twilio_client() -> Client:
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


async def execute_pickup(call_sid: str) -> bool:
    """Execute Pick Up — redirect caller to conference, send user dial-in PIN."""

    active_call = await transition_state(call_sid, CallState.PICKUP_RINGING)
    if not active_call:
        logger.warning("Cannot pick up — invalid state transition")
        return False

    try:
        twilio = _get_twilio_client()
        conference_name = secrets.token_urlsafe(16)
        pin = ''.join(secrets.choice('0123456789') for _ in range(4))

        # Save pickup state
        await update_active_call(call_sid, {
            "conference_name": conference_name,
            "pickup_pin": pin,
            "pickup_active": True,
        })

        # Redirect the caller's Twilio call from <Stream> into a <Conference>
        # This terminates the Gemini WebSocket (stream closes) but keeps
        # the caller connected — they hear "Connecting you now" + brief hold music
        conf_twiml = f"""<Response>
<Say voice="Polly.Joey">Connecting you now. One moment please.</Say>
<Dial>
<Conference startConferenceOnEnter="true" endConferenceOnExit="true" beep="false" waitUrl="http://twimlets.com/holdmusic?Bucket=com.twilio.music.classical">{conference_name}</Conference>
</Dial>
</Response>"""

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: twilio.calls(call_sid).update(twiml=conf_twiml)
        )
        logger.info(f"Caller redirected to pickup conference: {conference_name}")

        # Look up contractor's country for regional dial-in
        from app.db.contractors import get_contractor
        _contractor = await get_contractor(active_call.contractor_id) if active_call.contractor_id else None
        _country = _contractor.get("country_code", "US") if _contractor else "US"

        # Send user the dial-in number + PIN via Telegram
        await send_dial_in_message(
            chat_id=settings.telegram_chat_id,
            conference_number=get_dial_in_number(_country),
            pin=pin,
            caller_phone=active_call.caller_phone,
            caller_name=active_call.caller_name,
        )

        logger.info(f"Pickup initiated — PIN: {pin}")
        return True

    except Exception as e:
        logger.error(f"Pickup failed: {e}", exc_info=True)
        await transition_state(call_sid, CallState.SCREENING)
        return False
