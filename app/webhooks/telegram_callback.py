"""Telegram callback handler — processes button taps from call notifications.

ACTIVE CALL buttons:  Pick Up | Text Reply | Voicemail | Ignore
POST-CALL buttons:    Call Back | Text Them
"""

import asyncio

from fastapi import APIRouter, Depends, Request

from app.config import settings
from app.middleware.telegram_verify import verify_telegram_secret
from app.services.telegram_bot import answer_callback_query, update_call_ended
from app.db.cache import get_active_call, transition_state
from app.services.state_machine import CallState
from app.utils.logging import get_logger, call_sid_var

logger = get_logger(__name__)

router = APIRouter()

# In-memory dedup for callback queries (prevents double-tap)
_processed_callbacks: set = set()
MAX_PROCESSED = 1000


@router.post("/webhooks/telegram/callback")
async def handle_telegram_callback(request: Request, _=Depends(verify_telegram_secret)):
    """Handle Telegram inline button presses."""
    try:
        data = await request.json()
        callback_query = data.get("callback_query")

        if not callback_query:
            return {"status": "ok"}

        callback_id = callback_query.get("id", "")
        callback_data = callback_query.get("data", "")

        # Idempotency: reject duplicate callbacks
        if callback_id in _processed_callbacks:
            await answer_callback_query(callback_id, "Already processed")
            return {"status": "duplicate"}

        _processed_callbacks.add(callback_id)
        if len(_processed_callbacks) > MAX_PROCESSED:
            _processed_callbacks.clear()

        if ":" not in callback_data:
            await answer_callback_query(callback_id, "Invalid action")
            return {"status": "invalid"}

        action, call_sid = callback_data.split(":", 1)
        call_sid_var.set(call_sid)
        logger.info(f"Telegram action: {action}", extra={"action": action})

        message = callback_query.get("message", {})
        message_id = message.get("message_id")

        # Acknowledge immediately
        await answer_callback_query(callback_id, "Processing...")

        # --- ACTIVE CALL ACTIONS ---

        if action == "pickup":
            await _handle_pickup(call_sid, message_id)

        elif action == "textreply":
            await _handle_text_reply(call_sid, message_id)

        elif action == "voicemail":
            await _handle_voicemail(call_sid, message_id)

        elif action == "ignore":
            await _handle_ignore(call_sid, message_id)

        # --- POST-CALL ACTIONS ---

        elif action == "callback":
            await _handle_callback(call_sid, message_id)

        elif action == "text":
            await _handle_text_them(call_sid, message_id)

        else:
            logger.warning(f"Unknown action: {action}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Telegram callback error: {e}", exc_info=True)
        return {"status": "error"}


async def _handle_pickup(call_sid: str, message_id: int):
    """Pick Up — warm transfer via conference bridge."""
    from app.services.warm_transfer import execute_pickup

    active_call = await get_active_call(call_sid)
    if not active_call:
        logger.warning("Pick up: call not found in RTDB")
        return

    success = await execute_pickup(call_sid)

    if success:
        await update_call_ended(
            message_id=message_id,
            call_sid=call_sid,
            caller_phone=active_call.caller_phone,
            caller_name=active_call.caller_name,
            outcome="Connected",
            show_followup_buttons=False,
        )
    else:
        await answer_callback_query("", "Failed to connect — Kevin is still screening")


async def _handle_text_reply(call_sid: str, message_id: int):
    """Text Reply — send SMS to caller while Kevin keeps them engaged."""
    from app.services.sms import send_text_reply

    active_call = await get_active_call(call_sid)
    if not active_call:
        logger.warning("Text reply: call not found")
        return

    # Send the SMS
    sent = send_text_reply(active_call.caller_phone)

    # Transition state (can continue screening after text)
    await transition_state(call_sid, CallState.TEXT_REPLIED)

    status = "Text sent" if sent else "Failed to send text"
    await update_call_ended(
        message_id=message_id,
        call_sid=call_sid,
        caller_phone=active_call.caller_phone,
        caller_name=active_call.caller_name,
        outcome=f"Text Reply: \"{settings.user_name}: Can't talk right now. What's up?\"",
        show_followup_buttons=True,
    )


async def _handle_voicemail(call_sid: str, message_id: int):
    """Voicemail — tell Kevin to ask caller to leave a message."""
    active_call = await get_active_call(call_sid)
    if not active_call:
        return

    await transition_state(call_sid, CallState.VOICEMAIL_RECORDING)

    # TODO: Signal Vapi to transition to voicemail mode
    # For now, update the UI
    await update_call_ended(
        message_id=message_id,
        call_sid=call_sid,
        caller_phone=active_call.caller_phone,
        caller_name=active_call.caller_name,
        outcome="Taking voicemail...",
        show_followup_buttons=True,
    )


async def _handle_ignore(call_sid: str, message_id: int):
    """Ignore — Kevin wraps up politely."""
    active_call = await get_active_call(call_sid)
    if not active_call:
        return

    await transition_state(call_sid, CallState.IGNORED)

    # End the Vapi call (Kevin wraps up)
    if active_call.vapi_call_id:
        from app.services.vapi_agent import end_vapi_call
        asyncio.create_task(end_vapi_call(active_call.vapi_call_id))

    await update_call_ended(
        message_id=message_id,
        call_sid=call_sid,
        caller_phone=active_call.caller_phone,
        caller_name=active_call.caller_name,
        outcome="Ignored — Kevin wrapped up",
        show_followup_buttons=True,
    )


async def _handle_callback(call_sid: str, message_id: int):
    """Call Back — initiate a callback to the caller (post-call action)."""
    from app.services.sms import send_sms
    from app.db.calls import get_call

    call = await get_call(call_sid)
    if not call:
        return

    caller_phone = call.get("caller_phone", "")
    if not caller_phone:
        return

    # Bridge a callback: call user first, then connect to caller
    # For MVP: just initiate a call from Twilio
    try:
        from twilio.rest import Client
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

        # Call the user first
        twiml = f"""<Response><Dial callerId="{settings.twilio_phone_number}"><Number>{caller_phone}</Number></Dial></Response>"""
        client.calls.create(
            to=settings.user_phone,
            from_=settings.twilio_phone_number,
            twiml=twiml,
        )
        logger.info(f"Callback initiated to {caller_phone}")
    except Exception as e:
        logger.error(f"Callback failed: {e}", exc_info=True)


async def _handle_text_them(call_sid: str, message_id: int):
    """Text Them — send follow-up SMS after call ended."""
    from app.services.sms import send_followup_text
    from app.db.calls import get_call

    call = await get_call(call_sid)
    if not call:
        return

    caller_phone = call.get("caller_phone", "")
    if caller_phone:
        send_followup_text(caller_phone)
        logger.info(f"Follow-up text sent to {caller_phone}")
