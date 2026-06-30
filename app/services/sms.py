"""SMS service — Text Reply and Text Them flows."""

import asyncio

from twilio.rest import Client

from app.config import settings
from app.services.gated_actions import ActionKey, GateContext, check_gated_action
from app.services.side_effect_audit import record_gate_decision
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _gate_message_send(
    *,
    contractor: dict | None,
    action: ActionKey | None,
    gate_context: GateContext | None,
    channel: str,
) -> bool:
    if action is None:
        return True

    context = gate_context or GateContext(source="unknown", actor="system")
    decision = check_gated_action(contractor, action, context)
    record_gate_decision(
        action=action,
        contractor_id=(contractor or {}).get("contractor_id", ""),
        source=context.source,
        resource_id=context.idempotency_key,
        decision=decision,
    )
    if not decision.allowed:
        logger.info(
            f"{channel} blocked by gated action registry",
            extra={"action": action.value, "reason": decision.reason.value},
        )
        return False

    return True


async def send_sms(
    to: str,
    body: str,
    from_number: str = "",
    *,
    contractor: dict | None = None,
    action: ActionKey | None = None,
    gate_context: GateContext | None = None,
) -> bool:
    """Send an SMS via Twilio. Caller-facing sends can be gated fail-closed."""
    if not _gate_message_send(
        contractor=contractor,
        action=action,
        gate_context=gate_context,
        channel="SMS",
    ):
        return False

    try:
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        loop = asyncio.get_running_loop()
        message = await loop.run_in_executor(None, lambda: client.messages.create(
            to=to,
            from_=from_number or settings.twilio_phone_number,
            body=body,
        ))
        logger.info(f"SMS sent: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"SMS send failed: {e}", exc_info=True)
        return False


async def send_mms(
    to: str,
    body: str,
    media_url: str,
    from_number: str = "",
    *,
    contractor: dict | None = None,
    action: ActionKey | None = None,
    gate_context: GateContext | None = None,
) -> bool:
    """Send an MMS with a media attachment. Caller-facing sends can be gated fail-closed."""
    if not _gate_message_send(
        contractor=contractor,
        action=action,
        gate_context=gate_context,
        channel="MMS",
    ):
        return False

    try:
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        loop = asyncio.get_running_loop()
        message = await loop.run_in_executor(None, lambda: client.messages.create(
            to=to,
            from_=from_number or settings.twilio_phone_number,
            body=body,
            media_url=[media_url],
        ))
        logger.info(f"MMS sent: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"MMS send failed: {e}", exc_info=True)
        return False


async def send_text_reply(caller_phone: str, from_number: str = "") -> bool:
    """Send a quick text reply to the caller (like iPhone's 'Reply with Message')."""
    body = f"Can't talk right now. What's up? - {settings.user_name}"
    return await send_sms(caller_phone, body, from_number=from_number)


async def send_followup_text(caller_phone: str, from_number: str = "") -> bool:
    """Send a follow-up text after a missed/ended call."""
    body = f"Hi, I saw you called. What can I help with? - {settings.user_name}"
    return await send_sms(caller_phone, body, from_number=from_number)
