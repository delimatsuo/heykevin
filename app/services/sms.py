"""SMS service — Text Reply and Text Them flows."""

import asyncio
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from twilio.rest import Client
from twilio.http.http_client import TwilioHttpClient

from app.config import settings
from app.db import message_delivery_receipts as receipt_db
from app.services import message_delivery
from app.services.gated_actions import ActionKey, GateContext, check_gated_action
from app.services.side_effect_audit import record_gate_decision
from app.utils.logging import get_logger

logger = get_logger(__name__)


_SAFE_LABEL_PATTERN = re.compile(r"[^a-zA-Z0-9_.:-]+")
TWILIO_HTTP_TIMEOUT_SECONDS = 8.0
TWILIO_SEND_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class MessageDeliveryContext:
    call_sid: str
    effect: str


def _call_label(call_sid: str) -> str:
    label = _SAFE_LABEL_PATTERN.sub("_", str(call_sid or "")[:128]).strip("_.:-")
    return label[:8] or "unknown"


def _safe_effect(effect: str) -> str:
    return (
        effect
        if isinstance(effect, str) and effect in receipt_db.TRACKED_EFFECTS
        else "unknown"
    )


def _callback_base_url() -> str:
    callback_base = str(settings.cloud_run_url or "").strip().rstrip("/")
    try:
        parsed = urlsplit(callback_base)
        parsed.port
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return callback_base


async def _prepare_delivery_receipt(
    context: MessageDeliveryContext | None,
    *,
    channel: str,
) -> tuple[str, str]:
    if context is None:
        return "", ""
    callback_base = _callback_base_url()
    if not callback_base:
        logger.error(
            "message_delivery event=callback_config_invalid call=%s effect=%s channel=%s",
            _call_label(context.call_sid),
            _safe_effect(context.effect),
            channel,
        )
        return "", ""
    try:
        receipt_id = await receipt_db.create_receipt(
            call_sid=context.call_sid,
            effect=context.effect,
            channel=channel,
        )
    except Exception as error:
        logger.error(
            "message_delivery event=receipt_registration_error call=%s effect=%s channel=%s exception_type=%s",
            _call_label(context.call_sid),
            _safe_effect(context.effect),
            channel,
            type(error).__name__,
        )
        return "", ""
    if not receipt_id:
        logger.error(
            "message_delivery event=receipt_registration_failed call=%s effect=%s channel=%s",
            _call_label(context.call_sid),
            _safe_effect(context.effect),
            channel,
        )
        return "", ""
    callback_url = f"{callback_base}/webhooks/twilio/message-status/{receipt_id}"
    return receipt_id, callback_url


async def _send_twilio_message(
    *,
    channel: str,
    to: str,
    body: str,
    from_number: str,
    media_url: str = "",
    delivery_context: MessageDeliveryContext | None = None,
) -> bool:
    receipt_id, callback_url = await _prepare_delivery_receipt(
        delivery_context,
        channel=channel,
    )
    if delivery_context is not None and not receipt_id:
        return False

    try:
        client = Client(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            http_client=TwilioHttpClient(timeout=TWILIO_HTTP_TIMEOUT_SECONDS),
        )
        create_kwargs = {
            "to": to,
            "from_": from_number or settings.twilio_phone_number,
            "body": body,
        }
        if media_url:
            create_kwargs["media_url"] = [media_url]
        if callback_url:
            create_kwargs["status_callback"] = callback_url

        loop = asyncio.get_running_loop()
        message = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: client.messages.create(**create_kwargs),
            ),
            timeout=TWILIO_SEND_TIMEOUT_SECONDS,
        )
    except Exception as error:
        if receipt_id:
            await message_delivery.record_submission_failure(receipt_id)
        logger.error(
            "message_delivery event=send_failed channel=%s tracked=%s exception_type=%s",
            channel,
            bool(receipt_id),
            type(error).__name__,
        )
        return False

    if receipt_id:
        try:
            provider_status = str(
                getattr(message, "status", "unknown") or "unknown"
            ).lower()
            if provider_status not in receipt_db.PROVIDER_STATUSES:
                provider_status = "unknown"
            update = await receipt_db.record_provider_update(
                receipt_id,
                provider_status=provider_status,
                provider_message_sid=str(getattr(message, "sid", "") or ""),
            )
            if update.outcome not in {"updated", "ignored"}:
                logger.error(
                    "message_delivery event=submission_persist_failed call=%s effect=%s channel=%s outcome=%s",
                    _call_label(delivery_context.call_sid),
                    _safe_effect(delivery_context.effect),
                    channel,
                    update.outcome,
                )
        except Exception as error:
            logger.error(
                "message_delivery event=submission_persist_failed call=%s effect=%s channel=%s outcome=exception exception_type=%s",
                _call_label(delivery_context.call_sid),
                _safe_effect(delivery_context.effect),
                channel,
                type(error).__name__,
            )
    logger.info(
        "message_delivery event=submitted channel=%s tracked=%s",
        channel,
        bool(receipt_id),
    )
    return True


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
    delivery_context: MessageDeliveryContext | None = None,
) -> bool:
    """Send an SMS via Twilio. Caller-facing sends can be gated fail-closed."""
    if not _gate_message_send(
        contractor=contractor,
        action=action,
        gate_context=gate_context,
        channel="SMS",
    ):
        return False

    return await _send_twilio_message(
        channel="sms",
        to=to,
        body=body,
        from_number=from_number,
        delivery_context=delivery_context,
    )


async def send_mms(
    to: str,
    body: str,
    media_url: str,
    from_number: str = "",
    *,
    contractor: dict | None = None,
    action: ActionKey | None = None,
    gate_context: GateContext | None = None,
    delivery_context: MessageDeliveryContext | None = None,
) -> bool:
    """Send an MMS with a media attachment. Caller-facing sends can be gated fail-closed."""
    if not _gate_message_send(
        contractor=contractor,
        action=action,
        gate_context=gate_context,
        channel="MMS",
    ):
        return False

    return await _send_twilio_message(
        channel="mms",
        to=to,
        body=body,
        from_number=from_number,
        media_url=media_url,
        delivery_context=delivery_context,
    )


async def send_text_reply(caller_phone: str, from_number: str = "") -> bool:
    """Send a quick text reply to the caller (like iPhone's 'Reply with Message')."""
    body = f"Can't talk right now. What's up? - {settings.user_name}"
    return await send_sms(caller_phone, body, from_number=from_number)


async def send_followup_text(caller_phone: str, from_number: str = "") -> bool:
    """Send a follow-up text after a missed/ended call."""
    body = f"Hi, I saw you called. What can I help with? - {settings.user_name}"
    return await send_sms(caller_phone, body, from_number=from_number)
