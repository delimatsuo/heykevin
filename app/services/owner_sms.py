"""Owner SMS service: single boundary for sending SMS to account owners.

Enforces app opt-out (owner_sms_enabled), carrier/provider opt-out (owner_sms_opted_out),
STOP disclosure appending, status callback configuration, and 21610 provider opt-out handling.
"""

import asyncio
import urllib.parse
import uuid
from typing import Optional

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from app.config import settings
from app.db import contractors as contractor_db
from app.db import owner_sms as owner_sms_db
from app.services.sms import _message_create_kwargs
from app.utils.logging import get_logger, redact_phone

logger = get_logger(__name__)

DISCLOSURE_TEXT = "Reply STOP to stop texts."


async def send_owner_sms(contractor_id: str, body: str) -> Optional[bool]:
    """Send an SMS to an account owner with authoritative preference enforcement.

    Returns:
        - True: successfully accepted by provider
        - None: intentionally suppressed (app preference disabled or carrier/provider blocked)
        - False: actual error / missing account / invalid configuration / read failure
    """
    if not contractor_id or not isinstance(contractor_id, str) or not contractor_id.strip():
        logger.error("send_owner_sms: missing contractor_id")
        return False

    clean_cid = contractor_id.strip()

    # Re-read contractor immediately before provider send
    try:
        contractor = await contractor_db.get_contractor(clean_cid)
    except Exception as e:
        logger.error("send_owner_sms: contractor read failed for %s: %s", clean_cid, type(e).__name__)
        return False

    if not contractor or contractor.get("contractor_id") != clean_cid:
        logger.error("send_owner_sms: contractor %s not found or id mismatch", clean_cid)
        return False

    owner_phone, twilio_number = owner_sms_db.resolve_contractor_owner_sms_identities(contractor)

    if not owner_phone or not twilio_number:
        logger.error(
            "send_owner_sms: invalid or unresolvable phone numbers for %s (owner=%s, twilio=%s)",
            clean_cid,
            redact_phone(str(contractor.get("owner_phone") or "")),
            redact_phone(str(contractor.get("twilio_number") or "")),
        )
        return False

    enabled = owner_sms_db.resolve_owner_sms_enabled(contractor)
    opted_out = owner_sms_db.resolve_owner_sms_opted_out(contractor)

    if not enabled or opted_out:
        logger.info(
            "send_owner_sms: suppressed for %s (enabled=%s, opted_out=%s)",
            clean_cid,
            enabled,
            opted_out,
        )
        return None

    # Append disclosure once outside translated/generated content
    if DISCLOSURE_TEXT not in body:
        full_body = f"{body.rstrip()}\n\n{DISCLOSURE_TEXT}"
    else:
        full_body = body

    base_url = (getattr(settings, "cloud_run_url", "") or "").rstrip("/")
    try:
        parsed_base = urllib.parse.urlsplit(base_url)
        valid_base = (
            parsed_base.scheme == "https"
            and bool(parsed_base.hostname)
            and not parsed_base.username
            and not parsed_base.password
            and not parsed_base.query
            and not parsed_base.fragment
            and not any(char.isspace() for char in base_url)
        )
        _ = parsed_base.port
    except ValueError:
        valid_base = False
    if not valid_base:
        logger.error("send_owner_sms: invalid base url for callback")
        return False

    revision = owner_sms_db.resolve_owner_sms_opt_out_revision(contractor)
    query = urllib.parse.urlencode({"contractor_id": clean_cid, "revision": str(revision)})
    status_callback_url = f"{base_url}/webhooks/twilio/owner-sms-status?{query}"

    create_kwargs = _message_create_kwargs(
        to=owner_phone,
        body=full_body,
        from_number=twilio_number,
    )
    create_kwargs["status_callback"] = status_callback_url

    loop = asyncio.get_running_loop()

    try:
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        msg = await loop.run_in_executor(
            None,
            lambda: client.messages.create(**create_kwargs),
        )
        logger.info(
            "send_owner_sms: message accepted for %s (sid=%s)",
            clean_cid,
            getattr(msg, "sid", "unknown"),
        )
        return True
    except TwilioRestException as e:
        code = str(getattr(e, "code", "") or "")
        if code == "21610":
            logger.warning("Owner SMS provider error code=21610 type=TwilioRestException")
            dedupe_key = f"sync:{uuid.uuid4()}"
            try:
                await owner_sms_db.apply_owner_sms_consent_transition(
                    clean_cid,
                    opt_out=True,
                    source="provider_21610_sync",
                    dedupe_key=dedupe_key,
                    expected_revision=revision,
                    expected_owner_phone=owner_phone,
                    expected_twilio_number=twilio_number,
                )
            except Exception as sync_err:
                logger.error("Failed to persist sync 21610 opt-out: %s", type(sync_err).__name__)
            return None
        logger.warning("Owner SMS provider error code=%s type=%s", code, type(e).__name__)
        return False
    except Exception as e:
        logger.error("send_owner_sms: exception sending message: %s", type(e).__name__)
        return False
