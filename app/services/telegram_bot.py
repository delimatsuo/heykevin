"""Telegram bot service — notifications with inline action buttons and live transcript."""

import asyncio
from typing import Optional

import httpx
import phonenumbers

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


def _format_phone(e164: str) -> str:
    """Format E.164 phone number for display: +15551234567 → (555) 123-4567."""
    try:
        parsed = phonenumbers.parse(e164, "US")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.NATIONAL)
    except Exception:
        return e164


def _spam_label(spam_score: float) -> str:
    if spam_score > 0.7:
        return "High"
    elif spam_score > 0.3:
        return "Medium"
    return "Low"


def _trust_label(trust_score: int) -> str:
    if trust_score >= 90:
        return "VIP"
    elif trust_score >= 70:
        return "Likely Known"
    elif trust_score >= 30:
        return "Unknown"
    return "Likely Spam"


def _build_active_call_text(
    caller_phone: str,
    caller_name: str = "",
    carrier: str = "",
    line_type: str = "",
    spam_score: float = 0,
    trust_score: int = 50,
    transcript: str = "",
) -> str:
    """Build notification for an active call being screened by Kevin."""
    display_phone = _format_phone(caller_phone)

    lines = [
        "\U0001f4de Incoming Call",
        "\u2501" * 18,
        f"From: {display_phone}",
    ]
    if caller_name:
        lines.append(f"ID: {caller_name}")

    # Type and carrier on one line, compact
    parts = []
    if line_type:
        parts.append(line_type.title())
    if carrier:
        parts.append(carrier)
    if parts:
        sep = " \u00b7 "
        type_str = sep.join(parts)
        lines.append(f"Type: {type_str}")

    lines.append(f"Spam: {_spam_label(spam_score)} \u00b7 Trust: {_trust_label(trust_score)}")

    if transcript:
        lines.append("")
        lines.append("Live transcript:")
        lines.append(transcript)

    return "\n".join(lines)


def _build_active_call_keyboard(call_sid: str) -> dict:
    """Buttons shown while Kevin is screening the call."""
    return {
        "inline_keyboard": [
            [
                {"text": "\u260e\ufe0f  Pick Up", "callback_data": f"pickup:{call_sid}"},
            ],
            [
                {"text": "\U0001f4ac  Text Reply", "callback_data": f"textreply:{call_sid}"},
                {"text": "\U0001f4e8  Voicemail", "callback_data": f"voicemail:{call_sid}"},
            ],
            [
                {"text": "Ignore", "callback_data": f"ignore:{call_sid}"},
            ],
        ]
    }


def _build_dial_in_keyboard(conference_number: str, pin: str = "") -> dict:
    """Button shown after user taps Pick Up — tap to dial into the conference."""
    tel_uri = f"tel:{conference_number}"
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f4de  Tap to Join Call", "url": tel_uri},
            ],
        ]
    }


def _build_ended_call_text(
    caller_phone: str,
    caller_name: str = "",
    outcome: str = "",
    voicemail_transcript: str = "",
    duration: str = "",
) -> str:
    """Build notification after a call has ended."""
    display_phone = _format_phone(caller_phone)

    lines = [
        "\u260e\ufe0f Call Ended",
        "\u2501" * 18,
        f"From: {display_phone}",
    ]
    if caller_name:
        lines.append(f"ID: {caller_name}")
    if outcome:
        lines.append(f"Outcome: {outcome}")
    if duration:
        lines.append(f"Duration: {duration}")

    if voicemail_transcript:
        lines.append("")
        lines.append("Voicemail:")
        lines.append(f"\u201c{voicemail_transcript}\u201d")

    return "\n".join(lines)


def _build_ended_call_keyboard(call_sid: str) -> dict:
    """Buttons shown after a call ends — follow-up actions."""
    return {
        "inline_keyboard": [
            [
                {"text": "\U0001f4de  Call Back", "callback_data": f"callback:{call_sid}"},
                {"text": "\U0001f4ac  Text Them", "callback_data": f"text:{call_sid}"},
            ],
        ]
    }


async def _send(payload: dict) -> Optional[dict]:
    """Send a Telegram API request. Returns response data or None."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{TELEGRAM_API}/sendMessage",
                json=payload,
                timeout=10.0,
            )
            if response.status_code == 200:
                return response.json().get("result")
            else:
                logger.error(f"Telegram send failed: {response.status_code} {response.text}")
    except Exception as e:
        logger.error(f"Telegram send error: {e}", exc_info=True)
    return None


async def _edit(payload: dict) -> bool:
    """Edit a Telegram message. Returns True on success."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{TELEGRAM_API}/editMessageText",
                json=payload,
                timeout=10.0,
            )
            return response.status_code == 200
    except Exception as e:
        logger.error(f"Telegram edit error: {e}", exc_info=True)
        return False


# --- Public API ---


async def send_call_notification(
    call_sid: str,
    caller_phone: str,
    caller_name: str = "",
    carrier: str = "",
    line_type: str = "",
    spam_score: float = 0,
    trust_score: int = 50,
) -> Optional[int]:
    """Send a Telegram notification for an active call. Returns message_id or None."""
    chat_id = settings.telegram_chat_id
    if not chat_id:
        logger.warning("No TELEGRAM_CHAT_ID configured — skipping notification")
        return None

    text = _build_active_call_text(
        caller_phone=caller_phone,
        caller_name=caller_name,
        carrier=carrier,
        line_type=line_type,
        spam_score=spam_score,
        trust_score=trust_score,
    )

    result = await _send({
        "chat_id": chat_id,
        "text": text,
        "reply_markup": _build_active_call_keyboard(call_sid),
    })

    if result:
        msg_id = result.get("message_id")
        logger.info(f"Telegram notification sent, message_id={msg_id}")
        return msg_id
    return None


async def update_transcript(
    message_id: int,
    call_sid: str,
    caller_phone: str,
    caller_name: str = "",
    carrier: str = "",
    line_type: str = "",
    spam_score: float = 0,
    trust_score: int = 50,
    transcript: str = "",
) -> bool:
    """Update an existing notification with new transcript lines (keeps active buttons)."""
    chat_id = settings.telegram_chat_id
    if not chat_id or not message_id:
        return False

    text = _build_active_call_text(
        caller_phone=caller_phone,
        caller_name=caller_name,
        carrier=carrier,
        line_type=line_type,
        spam_score=spam_score,
        trust_score=trust_score,
        transcript=transcript,
    )

    return await _edit({
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": _build_active_call_keyboard(call_sid),
    })


async def update_call_ended(
    message_id: int,
    call_sid: str,
    caller_phone: str,
    caller_name: str = "",
    outcome: str = "",
    voicemail_transcript: str = "",
    duration: str = "",
    show_followup_buttons: bool = True,
) -> bool:
    """Update notification to show call ended with optional follow-up buttons."""
    chat_id = settings.telegram_chat_id
    if not chat_id or not message_id:
        return False

    text = _build_ended_call_text(
        caller_phone=caller_phone,
        caller_name=caller_name,
        outcome=outcome,
        voicemail_transcript=voicemail_transcript,
        duration=duration,
    )

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }

    # Show Call Back / Text Them buttons after call ends
    if show_followup_buttons:
        payload["reply_markup"] = _build_ended_call_keyboard(call_sid)

    return await _edit(payload)


async def send_dial_in_message(
    chat_id: str,
    conference_number: str,
    pin: str,
    caller_phone: str,
    caller_name: str = "",
) -> Optional[int]:
    """Send a message with dial-in number and PIN for pickup."""
    if not chat_id:
        return None

    display_caller = _format_phone(caller_phone)
    display_dialin = _format_phone(conference_number)
    name_str = f" ({caller_name})" if caller_name else ""

    text = (
        f"\u260e\ufe0f Pick Up: {display_caller}{name_str}\n"
        f"\n"
        f"Call now: {display_dialin}"
    )

    result = await _send({
        "chat_id": chat_id,
        "text": text,
    })

    if result:
        return result.get("message_id")
    return None


async def answer_callback_query(callback_query_id: str, text: str = "Processing..."):
    """Acknowledge a Telegram callback query (required within 30s)."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={
                    "callback_query_id": callback_query_id,
                    "text": text,
                },
                timeout=5.0,
            )
    except Exception as e:
        logger.error(f"Telegram answer callback error: {e}", exc_info=True)
