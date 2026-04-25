"""Vapi webhook event handler.

Handles:
- assistant-request: Dynamic routing — run lookups, decide if Kevin screens or forward
- transcript: Live transcript updates → Telegram
- status-update: Call state changes
- tool-calls: Knowledge base lookups
- end-of-call-report: Final transcript, cost
"""

import asyncio
import hashlib
import hmac
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings, get_dial_in_number
from app.db.cache import get_active_call, update_active_call, save_active_call
from app.db.contacts import get_contact
from app.services.state_machine import ActiveCall, CallState
from app.services.scoring import calculate_trust_score
from app.services.routing import determine_route, Route
from app.services.telegram_bot import update_transcript, send_call_notification
from app.utils.logging import get_logger, call_sid_var, redact_phone
from app.utils.phone import normalize_phone

logger = get_logger(__name__)

router = APIRouter()

# Throttle transcript updates to Telegram
_last_transcript_update: dict = {}
TRANSCRIPT_THROTTLE = 2.0


def _kevin_assistant_config(caller_phone: str, caller_name: str = "", contractor_id: str = "") -> dict:
    """Build the Kevin assistant configuration for Vapi."""
    caller_info = f"The caller's number is {caller_phone}."
    if caller_name:
        caller_info = f"The caller identifies as {caller_name} (number: {caller_phone})."

    return {
        "name": "Kevin",
        "firstMessage": f"Hi, this is Kevin, {settings.user_name}'s assistant. How can I help you?",
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-2",
        },
        "model": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "maxTokens": 150,
            "messages": [
                {
                    "role": "system",
                    "content": f"""You are Kevin, {settings.user_name}'s assistant. You answer the phone when {settings.user_name} is not available.

{caller_info}

YOUR ROLE: You are a gatekeeper, not a helper. Your ONLY job is to find out WHO is calling and WHAT it's about (in one line). Then hold the line while {settings.user_name} decides what to do. You do NOT try to help, solve problems, or ask detailed questions about their topic.

EXAMPLE OF A GOOD CALL:
- Kevin: "Hi, this is Kevin, {settings.user_name}'s assistant. How can I help you?"
- Caller: "Hi, I'm Brian, calling about a car {settings.user_name} wanted to buy."
- Kevin: "Got it, Brian. Let me see if {settings.user_name} is available. One moment."
- (hold the line, make small talk if needed: "How's your day going?" or "Shouldn't be long.")

EXAMPLE OF WHAT NOT TO DO:
- Caller: "I'm calling about a car."
- Kevin: "Which car? What model? When did they want to buy it?" ← WRONG. You don't need details. You just need to know it's about a car.

RULES:
- Get their name. Get a one-line reason. Stop asking questions about the topic.
- Once you have who + why, say "Let me check if they're available, one moment please." Then STOP TALKING. Say nothing more. Just wait in silence. Do NOT make small talk after this point. Do NOT ask "how's your day". Just be quiet until you're told to speak again.
- Keep responses to ONE short sentence.
- Sound natural and warm. Use "Got it", "Sure thing", "Absolutely".
- NEVER repeat or paraphrase what the caller just said.
- NEVER ask detailed follow-up questions about their topic.
- NEVER make small talk after saying you'll check availability.
- NEVER reveal you are AI.
- NEVER share {settings.user_name}'s personal info.
- If wrapping up: "I'll pass that along to {settings.user_name}. Have a good one!\"""",
                }
            ],
        },
        "voice": {
            "provider": "11labs",
            "voiceId": "bIHbv24MWmeRgasZH58o",
        },
        "silenceTimeoutSeconds": 120,
        "maxDurationSeconds": 300,
        "endCallFunctionEnabled": True,
        "forwardingPhoneNumber": get_dial_in_number("US"),  # Vapi is US-only legacy
        "serverUrl": f"{settings.cloud_run_url}/webhooks/vapi/events"
        + (f"?contractor_id={contractor_id}" if contractor_id else ""),
    }


@router.post("/webhooks/vapi/events")
async def handle_vapi_event(request: Request):
    """Handle events from Vapi's serverUrl webhook."""
    # Verify webhook signature if secret is configured
    if settings.vapi_webhook_secret:
        body = await request.body()
        signature = request.headers.get("X-Vapi-Signature", "")
        expected = hmac.new(
            settings.vapi_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            logger.warning("Vapi webhook signature mismatch — rejecting request")
            return JSONResponse(status_code=401, content={"error": "invalid signature"})

    try:
        data = await request.json()
        message = data.get("message", {})
        message_type = message.get("type", "")

        # Log every event type for debugging
        logger.info(f"Vapi event: {message_type}", extra={"action": message_type})

        if message_type == "assistant-request":
            return await _handle_assistant_request(data)
        elif message_type == "transcript":
            await _handle_transcript(data)
        elif message_type == "conversation-update":
            await _handle_conversation_update(data)
        elif message_type == "status-update":
            await _handle_status_update(data)
        elif message_type == "tool-calls":
            return await _handle_tool_calls(data)
        elif message_type == "end-of-call-report":
            await _handle_end_of_call(data)

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Vapi event handler error: {e}", exc_info=True)
        # For assistant-request, we MUST return something or the call fails
        return {"assistant": _kevin_assistant_config("unknown")}


async def _handle_assistant_request(data: dict) -> dict:
    """Handle assistant-request — this is our routing decision point.

    Vapi sends this BEFORE the assistant speaks. We have 7.5 seconds to respond.
    We run fast lookups, score the caller, and either:
    - Return a Kevin assistant config (AI screening)
    - Return a forwarding destination (whitelisted contacts)
    - Tell Vapi to handle it (spam could be rejected)
    """
    message = data.get("message", {})
    call_data = message.get("call", {})
    customer = call_data.get("customer", {})
    caller_phone = normalize_phone(customer.get("number", "")) or customer.get("number", "")
    call_id = call_data.get("id", "")  # Vapi's call ID

    logger.info(f"Assistant request for caller: {redact_phone(caller_phone)}")

    # Determine contractor_id from the Vapi phone number that received the call
    contractor_id = ""
    vapi_phone = call_data.get("phoneNumber", {}).get("number", "")
    if vapi_phone:
        try:
            from app.db.contractors import get_contractor_by_twilio_number
            contractor = await asyncio.wait_for(
                get_contractor_by_twilio_number(vapi_phone), timeout=2.0
            )
            if contractor:
                contractor_id = contractor.get("contractor_id", "")
        except Exception:
            pass

    # Fast lookups (< 2 seconds)
    contact = None
    try:
        contact = await asyncio.wait_for(
            get_contact(caller_phone, contractor_id=contractor_id), timeout=2.0
        )
    except Exception:
        pass

    lookups = {
        "contact": contact,
        "history": {},
        "twilio": {},
        "nomorobo": {},
    }

    trust_score, score_breakdown = calculate_trust_score(caller_phone, lookups)
    route = determine_route(trust_score)

    caller_name = contact.get("name", "") if contact else ""

    logger.info(
        f"Routing: {route.value} (score={trust_score})",
        extra={"caller_phone": caller_phone, "trust_score": trust_score, "route": route.value},
    )

    # Send Telegram notification and save state in background
    asyncio.create_task(_post_assistant_request_tasks(
        call_id=call_id,
        caller_phone=caller_phone,
        caller_name=caller_name,
        trust_score=trust_score,
        score_breakdown=score_breakdown,
        route=route,
    ))

    if route == Route.WHITELIST_FORWARD:
        # Forward to user — tell Vapi to transfer the call
        return {
            "assistant": {
                "name": "Transfer",
                "firstMessage": "Connecting you now.",
                "model": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-20250514",
                    "messages": [{"role": "system", "content": "Say 'Connecting you now' and nothing else."}],
                },
                "voice": {"provider": "11labs", "voiceId": "bIHbv24MWmeRgasZH58o"},
                "forwardingPhoneNumber": settings.user_phone,
            }
        }

    elif route == Route.SPAM_BLOCK:
        # For spam, use a minimal assistant that hangs up
        return {
            "assistant": {
                "name": "Block",
                "firstMessage": "This number is not accepting calls. Goodbye.",
                "model": {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-20250514",
                    "messages": [{"role": "system", "content": "Say 'This number is not accepting calls. Goodbye.' then end the call immediately."}],
                },
                "voice": {"provider": "11labs", "voiceId": "bIHbv24MWmeRgasZH58o"},
                "endCallMessage": "Goodbye.",
                "maxDurationSeconds": 10,
            }
        }

    else:
        # AI Screening (unknown + ring-then-screen) — Kevin answers
        return {
            "assistant": _kevin_assistant_config(caller_phone, caller_name, contractor_id),
        }


async def _post_assistant_request_tasks(
    call_id: str,
    caller_phone: str,
    caller_name: str,
    trust_score: int,
    score_breakdown: dict,
    route,
):
    """Background tasks after routing decision — Telegram notification, save state."""
    try:
        from app.db.calls import save_call

        # Save call record
        await save_call(call_id, {
            "caller_phone": caller_phone,
            "caller_name": caller_name,
            "timestamp": time.time(),
            "trust_score": trust_score,
            "score_breakdown": score_breakdown,
            "route_taken": route.value,
        })

        # Send Telegram notification for screened calls
        if route in (Route.AI_SCREENING, Route.RING_THEN_SCREEN):
            telegram_msg_id = await send_call_notification(
                call_sid=call_id,
                caller_phone=caller_phone,
                caller_name=caller_name,
                trust_score=trust_score,
            )

            # Save active call state
            active_call = ActiveCall(
                call_sid=call_id,
                caller_phone=caller_phone,
                state=CallState.SCREENING,
                trust_score=trust_score,
                caller_name=caller_name,
                telegram_message_id=telegram_msg_id or 0,
                vapi_call_id=call_id,
            )
            await save_active_call(active_call)

    except Exception as e:
        logger.error(f"Post assistant-request task error: {e}", exc_info=True)


async def _handle_transcript(data: dict):
    """Handle real-time transcript updates — push to Telegram."""
    message = data.get("message", {})
    transcript_type = message.get("transcriptType", "")
    role = message.get("role", "")
    content = message.get("transcript", "")

    if not content or transcript_type == "partial":
        return

    call_data = data.get("call", {})
    call_id = call_data.get("id", "")

    # Throttle
    now = time.time()
    if now - _last_transcript_update.get(call_id, 0) < TRANSCRIPT_THROTTLE:
        return
    _last_transcript_update[call_id] = now

    active_call = await get_active_call(call_id)
    if not active_call:
        return

    speaker = "Kevin" if role == "assistant" else "Caller"
    new_line = f"{speaker}: {content}"

    lines = active_call.transcript_buffer.split("\n") if active_call.transcript_buffer else []
    lines.append(new_line)
    transcript_text = "\n".join(lines[-5:])

    await update_active_call(call_id, {"transcript_buffer": transcript_text})

    if active_call.telegram_message_id:
        await update_transcript(
            message_id=active_call.telegram_message_id,
            call_sid=call_id,
            caller_phone=active_call.caller_phone,
            caller_name=active_call.caller_name,
            carrier=active_call.carrier,
            line_type=active_call.line_type,
            spam_score=active_call.spam_score,
            trust_score=active_call.trust_score,
            transcript=transcript_text,
        )

    logger.info(f"Transcript: {speaker}: {content[:80]}")


async def _handle_conversation_update(data: dict):
    """Handle conversation-update — Vapi sends full conversation history updates."""
    message = data.get("message", {})
    conversation = message.get("conversation", [])

    if not conversation:
        return

    # Vapi may put call info at different paths depending on event type
    call_data = data.get("call", {})
    if not call_data:
        call_data = message.get("call", {})
    call_id = call_data.get("id", "")

    # If no call_id found, try to extract from the data root
    if not call_id:
        call_id = data.get("call", {}).get("id", "") or data.get("id", "")

    logger.info(f"Conv update: call_id={call_id}, turns={len(conversation)}")

    # Throttle
    now = time.time()
    if now - _last_transcript_update.get(call_id, 0) < TRANSCRIPT_THROTTLE:
        return
    _last_transcript_update[call_id] = now

    active_call = await get_active_call(call_id)
    if not active_call:
        logger.warning(f"No active call found for call_id={call_id}")
        return

    # Build transcript from last few conversation turns
    lines = []
    for turn in conversation[-6:]:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if content and role in ("assistant", "user"):
            speaker = "Kevin" if role == "assistant" else "Caller"
            lines.append(f"{speaker}: {content}")

    transcript_text = "\n".join(lines[-5:])

    await update_active_call(call_id, {"transcript_buffer": transcript_text})

    if active_call.telegram_message_id:
        await update_transcript(
            message_id=active_call.telegram_message_id,
            call_sid=call_id,
            caller_phone=active_call.caller_phone,
            caller_name=active_call.caller_name,
            carrier=active_call.carrier,
            line_type=active_call.line_type,
            spam_score=active_call.spam_score,
            trust_score=active_call.trust_score,
            transcript=transcript_text,
        )

    logger.info(f"Conversation update: {len(conversation)} turns")


async def _handle_status_update(data: dict):
    message = data.get("message", {})
    status = message.get("status", "")
    logger.info(f"Vapi call status: {status}")


async def _handle_tool_calls(data: dict) -> dict:
    message = data.get("message", {})
    tool_calls = message.get("toolCalls", [])

    results = []
    for tool_call in tool_calls:
        tool_name = tool_call.get("function", {}).get("name", "")
        tool_call_id = tool_call.get("id", "")

        if tool_name == "check_knowledge_base":
            from app.services.knowledge_base import check_knowledge_base
            question = tool_call.get("function", {}).get("arguments", {}).get("question", "")
            answer = await check_knowledge_base(question)
            results.append({
                "toolCallId": tool_call_id,
                "result": answer or "I don't have that information right now.",
            })
        else:
            results.append({
                "toolCallId": tool_call_id,
                "result": "Done.",
            })

    return {"results": results}


async def _handle_end_of_call(data: dict):
    message = data.get("message", {})
    ended_reason = message.get("endedReason", "")
    logger.info(f"Vapi call ended: reason={ended_reason}")
