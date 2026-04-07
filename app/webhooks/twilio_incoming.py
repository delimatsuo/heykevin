"""Twilio incoming call webhook handler.

This is the most critical endpoint. Every incoming call hits this.

Strategy: Run fast lookups (contacts DB, call history) synchronously within
the webhook, compute trust score, and return the correct TwiML directly.
This avoids the "call not in-progress" issue with async updates.

Twilio gives us 15 seconds to respond. Our target: respond in < 5 seconds.
"""

import asyncio
import time

from fastapi import APIRouter, Depends, Request

from app.config import settings
from app.middleware.twilio_verify import verify_twilio_signature
from app.utils.error_handlers import twiml_response, fallback_twiml_response
from app.utils.logging import get_logger, call_sid_var
from app.utils.phone import normalize_phone

logger = get_logger(__name__)

router = APIRouter()


def _forward_twiml(phone: str) -> str:
    """TwiML to forward call directly to a number."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{settings.twilio_phone_number}">
        <Number>{phone}</Number>
    </Dial>
</Response>"""


def _screening_twiml(call_sid: str) -> str:
    """TwiML to connect caller directly to Kevin via Twilio Media Streams + Gemini.

    <Connect><Stream> opens a WebSocket to our server, which bridges
    the audio to Gemini Live API. The caller talks to Kevin directly
    on this same call — no conference, no transfer, no disconnection.
    """
    ws_url = settings.cloud_run_url.replace("https://", "wss://")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}/media-stream/{call_sid}"/>
    </Connect>
</Response>"""


def _reject_twiml() -> str:
    """TwiML to reject a spam call."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Reject reason="busy"/>
</Response>"""


def _voicemail_twiml() -> str:
    """TwiML for voicemail (circuit breaker mode)."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Hi, I can't take your call right now. Please leave a message after the beep.</Say>
    <Record maxLength="120" transcribe="true" playBeep="true"/>
    <Say>Thank you. Goodbye.</Say>
</Response>"""


@router.post("/webhooks/twilio/incoming")
async def handle_incoming_call(request: Request, _=Depends(verify_twilio_signature)):
    """Handle an incoming call from Twilio.

    Runs lookups and scoring synchronously, then returns the correct TwiML
    directly in the response. No async update needed.
    """
    start = time.monotonic()

    try:
        form_data = await request.form()
        call_sid = form_data.get("CallSid", "unknown")
        caller_phone = normalize_phone(form_data.get("From", "")) or form_data.get("From", "")

        call_sid_var.set(call_sid)
        logger.info("Incoming call received", extra={"caller_phone": caller_phone})

        # Circuit breaker check
        from app.services.circuit_breaker import is_circuit_open
        if is_circuit_open():
            logger.warning("Circuit breaker open — voicemail")
            return twiml_response(_voicemail_twiml())

        # Fast lookups — contacts and call history only (Firestore, ~100ms each)
        # Skip Twilio Lookup API (too slow at 3s) for the synchronous path
        from app.db.contacts import get_contact
        from app.db.calls import get_call_history
        from app.services.scoring import calculate_trust_score
        from app.services.routing import determine_route, Route

        contact = None
        history = {}
        try:
            contact = await asyncio.wait_for(get_contact(caller_phone), timeout=2.0)
        except Exception:
            pass

        try:
            calls = await asyncio.wait_for(get_call_history(caller_phone, limit=10), timeout=2.0)
            if calls:
                history = {
                    "times_picked_up": sum(1 for c in calls if c.get("outcome") == "picked_up"),
                    "times_ignored": sum(1 for c in calls if c.get("outcome") == "ignored"),
                }
        except Exception:
            pass

        lookups = {
            "contact": contact,
            "history": history,
            "twilio": {},      # Skip Twilio Lookup in sync path
            "nomorobo": {},    # Skip Nomorobo in sync path
        }

        trust_score, score_breakdown = calculate_trust_score(caller_phone, lookups)
        route = determine_route(trust_score)

        # Quiet hours override
        from app.services.quiet_hours import get_quiet_hours_routing_override
        override = get_quiet_hours_routing_override(trust_score)
        if override and route != Route.SPAM_BLOCK:
            route = Route.AI_SCREENING

        caller_name = contact.get("name", "") if contact else ""
        conference_name = f"call_{call_sid}"

        # Save call record and send notifications in background (don't block response)
        asyncio.create_task(_post_routing_tasks(
            call_sid=call_sid,
            caller_phone=caller_phone,
            caller_name=caller_name,
            trust_score=trust_score,
            score_breakdown=score_breakdown,
            route=route,
            lookups=lookups,
            conference_name=conference_name,
        ))

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            f"Routing decided in {duration_ms}ms",
            extra={"route": route.value, "trust_score": trust_score, "duration_ms": duration_ms},
        )

        # Return the correct TwiML directly — no async update needed
        if route == Route.WHITELIST_FORWARD:
            return twiml_response(_forward_twiml(settings.user_phone))

        elif route == Route.RING_THEN_SCREEN:
            # For now, forward to user
            return twiml_response(_forward_twiml(settings.user_phone))

        elif route == Route.AI_SCREENING:
            # Connect caller directly to Kevin via Media Streams + Gemini
            return twiml_response(_screening_twiml(call_sid))

        elif route == Route.SPAM_BLOCK:
            return twiml_response(_reject_twiml())

        # Default fallback
        return twiml_response(_forward_twiml(settings.user_phone))

    except Exception as e:
        logger.error(f"Error handling incoming call: {e}", exc_info=True)
        from app.services.circuit_breaker import record_error
        record_error()
        return fallback_twiml_response()


async def _post_routing_tasks(
    call_sid: str,
    caller_phone: str,
    caller_name: str,
    trust_score: int,
    score_breakdown: dict,
    route,
    lookups: dict,
    conference_name: str,
):
    """Background tasks after routing — save call record, send Telegram, save RTDB state."""
    call_sid_var.set(call_sid)

    try:
        from app.db.calls import save_call
        from app.services.telegram_bot import send_call_notification
        from app.services.routing import Route

        carrier = lookups.get("twilio", {}).get("carrier", "")
        line_type = lookups.get("twilio", {}).get("line_type", "")
        spam_score = lookups.get("nomorobo", {}).get("spam_score", 0)

        # Save call record
        await save_call(call_sid, {
            "caller_phone": caller_phone,
            "caller_name": caller_name,
            "timestamp": time.time(),
            "trust_score": trust_score,
            "score_breakdown": score_breakdown,
            "route_taken": route.value,
            "lookup_data": {"carrier": carrier, "line_type": line_type, "spam_score": spam_score},
        })

        # Notify user for screened calls
        if route in (Route.AI_SCREENING, Route.RING_THEN_SCREEN):
            # Send Telegram notification (keep as fallback)
            telegram_msg_id = await send_call_notification(
                call_sid=call_sid,
                caller_phone=caller_phone,
                caller_name=caller_name,
                carrier=carrier,
                line_type=line_type,
                spam_score=spam_score,
                trust_score=trust_score,
            )

            # Send VoIP push to iOS app (triggers CallKit)
            from app.services.push_notification import send_voip_push, get_device_token
            device_token = await get_device_token()
            if device_token:
                await send_voip_push(
                    device_token=device_token,
                    caller_phone=caller_phone,
                    caller_name=caller_name,
                    call_sid=call_sid,
                    conference_name=f"call_{call_sid}",
                )

            # Save active call state to RTDB
            if route == Route.AI_SCREENING:
                from app.db.cache import save_active_call
                from app.services.state_machine import ActiveCall, CallState

                active_call = ActiveCall(
                    call_sid=call_sid,
                    caller_phone=caller_phone,
                    state=CallState.SCREENING,
                    conference_name=f"call_{call_sid}",
                    trust_score=trust_score,
                    caller_name=caller_name,
                    carrier=carrier,
                    line_type=line_type,
                    spam_score=spam_score,
                    telegram_message_id=telegram_msg_id or 0,
                )
                await save_active_call(active_call)

    except Exception as e:
        logger.error(f"Post-routing task error: {e}", exc_info=True)


@router.post("/webhooks/twilio/ios-voice")
async def handle_ios_voice(request: Request):
    """TwiML App webhook for iOS Twilio Voice SDK.

    When the iOS app accepts a call, the Twilio Voice SDK makes an outgoing
    call to this TwiML App. We return Conference TwiML so the app joins
    the same conference as the caller.
    """
    try:
        form_data = await request.form()
        conference_name = form_data.get("conference_name", "")

        logger.info(f"iOS Voice SDK connecting to conference: {conference_name}")

        if not conference_name:
            return twiml_response("""<?xml version="1.0" encoding="UTF-8"?>
<Response><Say>No conference specified.</Say></Response>""")

        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Conference startConferenceOnEnter="true"
                    endConferenceOnExit="true"
                    beep="false">
            {conference_name}
        </Conference>
    </Dial>
</Response>"""

        return twiml_response(twiml)

    except Exception as e:
        logger.error(f"iOS voice webhook error: {e}", exc_info=True)
        return twiml_response("""<?xml version="1.0" encoding="UTF-8"?>
<Response><Say>Something went wrong.</Say></Response>""")


@router.post("/webhooks/twilio/dial-in")
async def handle_dial_in(request: Request):
    """Handle user dialing in to pick up a screened call.

    The caller has already been redirected into a conference by warm_transfer.
    The user calls the dial-in number, enters a PIN, and joins the same conference.
    """
    try:
        form_data = await request.form()
        caller = form_data.get("From", "")
        digits = form_data.get("Digits", "")  # PIN from <Gather>

        logger.info(f"Dial-in from {caller}, digits={digits}")

        # If no digits yet, prompt for PIN
        if not digits:
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather numDigits="4" action="{settings.cloud_run_url}/webhooks/twilio/dial-in" method="POST">
        <Say voice="Polly.Joey">Enter your four digit PIN.</Say>
    </Gather>
    <Say>No PIN entered. Goodbye.</Say>
</Response>"""
            return twiml_response(twiml)

        # Validate PIN against RTDB
        from app.db.cache import _init_firebase, ACTIVE_CALLS_PATH
        _init_firebase()
        from firebase_admin import db as rtdb

        ref = rtdb.reference(ACTIVE_CALLS_PATH)
        all_calls = ref.get() or {}

        conference_name = None
        active_call_id = None

        for cid, cdata in all_calls.items():
            if cdata.get("pickup_active") and cdata.get("pickup_pin") == digits:
                conference_name = cdata["conference_name"]
                active_call_id = cid
                logger.info(f"PIN validated! Conference: {conference_name}")
                rtdb.reference(f"{ACTIVE_CALLS_PATH}/{cid}/pickup_active").set(False)
                break

        if not conference_name:
            logger.warning(f"Invalid PIN: {digits}")
            return twiml_response("""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Invalid PIN. Goodbye.</Say>
</Response>""")

        # Join the user to the conference where the caller is waiting
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joey">Connecting you now.</Say>
    <Dial>
        <Conference startConferenceOnEnter="true"
                    endConferenceOnExit="true"
                    beep="false">
            {conference_name}
        </Conference>
    </Dial>
</Response>"""

        # Update state
        if active_call_id:
            from app.db.cache import transition_state
            from app.services.state_machine import CallState
            asyncio.create_task(transition_state(active_call_id, CallState.CONNECTED))

        return twiml_response(twiml)

    except Exception as e:
        logger.error(f"Dial-in error: {e}", exc_info=True)
        return twiml_response("""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Something went wrong. Please try again.</Say>
</Response>""")


@router.post("/webhooks/twilio/fallback")
async def handle_fallback(request: Request):
    """Emergency fallback. Zero dependencies. Just forward."""
    logger.error("Twilio fallback URL hit — primary handler failed")
    return fallback_twiml_response()


@router.post("/webhooks/twilio/status")
async def handle_status(request: Request, _=Depends(verify_twilio_signature)):
    """Handle call status updates from Twilio."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    status = form_data.get("CallStatus", "")
    call_sid_var.set(call_sid)
    logger.info(f"Call status update: {status}")
    return {"status": "ok"}
