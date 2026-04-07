"""SMS service — Text Reply and Text Them flows."""

from twilio.rest import Client

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


def send_sms(to: str, body: str) -> bool:
    """Send an SMS via Twilio."""
    try:
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        message = client.messages.create(
            to=to,
            from_=settings.twilio_phone_number,
            body=body,
        )
        logger.info(f"SMS sent: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"SMS send failed: {e}", exc_info=True)
        return False


def send_text_reply(caller_phone: str) -> bool:
    """Send a quick text reply to the caller (like iPhone's 'Reply with Message')."""
    body = f"Can't talk right now. What's up? - {settings.user_name}"
    return send_sms(caller_phone, body)


def send_followup_text(caller_phone: str) -> bool:
    """Send a follow-up text after a missed/ended call."""
    body = f"Hi, I saw you called. What can I help with? - {settings.user_name}"
    return send_sms(caller_phone, body)
