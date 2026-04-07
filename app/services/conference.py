"""Twilio Conference Bridge management — participant add/remove for warm transfer."""

from typing import Optional

from twilio.rest import Client

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _get_client() -> Client:
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


def find_conference_sid(conference_name: str) -> Optional[str]:
    """Find the SID of an active conference by name."""
    try:
        client = _get_client()
        conferences = client.conferences.list(
            friendly_name=conference_name,
            status="in-progress",
        )
        if conferences:
            return conferences[0].sid
    except Exception as e:
        logger.error(f"Conference lookup failed: {e}", exc_info=True)
    return None


def add_participant(conference_sid: str, phone: str) -> Optional[str]:
    """Add a phone number as a participant to a conference. Returns call SID."""
    try:
        client = _get_client()
        participant = client.conferences(conference_sid).participants.create(
            from_=settings.twilio_phone_number,
            to=phone,
            beep="false",
            early_media=True,
        )
        logger.info(f"Added participant to conference: {participant.call_sid}")
        return participant.call_sid
    except Exception as e:
        logger.error(f"Add participant failed: {e}", exc_info=True)
        return None


def remove_participant(conference_sid: str, call_sid: str) -> bool:
    """Remove a participant from a conference."""
    try:
        client = _get_client()
        client.conferences(conference_sid).participants(call_sid).delete()
        logger.info(f"Removed participant: {call_sid}")
        return True
    except Exception as e:
        logger.error(f"Remove participant failed: {e}", exc_info=True)
        return False


def end_conference(conference_sid: str) -> bool:
    """End a conference entirely."""
    try:
        client = _get_client()
        client.conferences(conference_sid).update(status="completed")
        logger.info("Conference ended")
        return True
    except Exception as e:
        logger.error(f"End conference failed: {e}", exc_info=True)
        return False
