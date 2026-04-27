"""Post-call processing: extract job card, save to Firestore, send SMS.

Supports two modes:
- "personal": simple missed-call notification, no job card
- "business" (default): full job card extraction + estimate link + vCard
"""

import asyncio
import time

from app.services.job_card import extract_job_card
from app.services.entitlements import effective_mode
from app.services.sms import send_sms, send_mms
from app.db.jobs import save_job
from app.config import settings
from app.utils.logging import get_logger, redact_phone

logger = get_logger(__name__)

# Auto-reply rate limit moved to Firestore (auto_reply_timestamps collection)

# Urgency emoji mapping
URGENCY_ICONS = {
    "emergency": "\U0001f6a8",
    "same_day": "\u26a1",
    "routine": "\U0001f527",
    "quote": "\U0001f4ac",
    "none": "\U0001f4de",
}

# Call type headers
CALL_TYPE_HEADERS = {
    "service_request": "NEW LEAD",
    "personal": "PERSONAL CALL",
    "business": "BUSINESS CALL",
    "spam": "SPAM",
    "unknown": "MISSED CALL",
}


async def process_post_call(
    transcript_lines: list,
    caller_phone: str,
    call_sid: str,
    contractor_phone: str = "",
    twilio_number: str = "",
    contractor: dict = None,
    caller_language: str = "en",
):
    """Full post-call pipeline: extract -> save -> notify contractor + caller."""
    contractor = contractor or {}
    mode = contractor.get("effective_mode") or effective_mode(contractor)

    # Treat legacy "kevin" mode as "business"
    if mode not in ("personal",):
        mode = "business"

    try:
        transcript_text = "\n".join(transcript_lines)

        if mode == "personal":
            await _process_personal(transcript_text, caller_phone, call_sid,
                                    contractor_phone, twilio_number,
                                    user_language=contractor.get("user_language", "en"))
        else:
            await _process_business(transcript_text, caller_phone, call_sid,
                                    contractor_phone, twilio_number, contractor,
                                    caller_language=caller_language)

    except Exception as e:
        logger.error(f"Post-call processing failed: {e}", exc_info=True)


async def _process_personal(
    transcript_text: str,
    caller_phone: str,
    call_sid: str,
    contractor_phone: str,
    twilio_number: str = "",
    user_language: str = "en",
):
    """Personal mode: simple notification, no job card extraction."""
    # Simple extraction: just get name and reason via Claude (with retry)
    from app.services.job_card import extract_job_card
    job_data = None
    for attempt in range(2):
        try:
            job_data = await extract_job_card(transcript_text, caller_phone)
            break
        except Exception as e:
            if attempt == 0:
                logger.warning(f"Job card extraction failed, retrying: {e}")
                await asyncio.sleep(1)
            else:
                logger.error(f"Job card extraction failed permanently: {e}")
                job_data = {"caller_phone": caller_phone, "call_type": "unknown"}

    name = job_data.get("caller_name", "") or "Unknown caller"
    reason = job_data.get("issue_description", "") or job_data.get("message", "") or "No details"
    callback = job_data.get("callback_number", "") or caller_phone

    # Save callback number and caller name to call record
    from app.db.calls import save_call
    call_updates = {"caller_name": name}
    if job_data.get("callback_number"):
        call_updates["callback_number"] = job_data["callback_number"]
    await save_call(call_sid, call_updates)

    # Send simple SMS to owner (in their language)
    owner_phone = contractor_phone or getattr(settings, "user_phone", "")
    if owner_phone:
        sms = (
            f"Missed call from {name}\n"
            f"Re: {reason}\n"
            f"\U0001f4de {callback}"
        )
        if user_language and user_language != "en":
            try:
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
                resp = await client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=200,
                    messages=[{"role": "user", "content": (
                        f"Translate this missed call notification to language code '{user_language}'. "
                        f"Keep phone numbers and names exactly as-is. Keep emojis. "
                        f"Return ONLY the translated message:\n\n{sms}"
                    )}],
                )
                sms = resp.content[0].text.strip()
            except Exception as e:
                logger.warning(f"Personal SMS translation failed: {e}")
        try:
            await send_sms(owner_phone, sms, from_number=twilio_number)
            logger.info(f"Personal mode SMS sent to: {redact_phone(owner_phone)}")
        except Exception as e:
            logger.error(f"Personal mode SMS failed to {redact_phone(owner_phone)}: {e}")

    # Send call summary push for personal mode
    await _send_summary_push(job_data, {})


async def _process_business(
    transcript_text: str,
    caller_phone: str,
    call_sid: str,
    contractor_phone: str,
    twilio_number: str,
    contractor: dict,
    caller_language: str = "en",
):
    """Business mode: full job card + estimate link + vCard."""
    # 1. Extract structured job card from transcript (with retry)
    job_data = None
    for attempt in range(2):
        try:
            job_data = await extract_job_card(transcript_text, caller_phone)
            break
        except Exception as e:
            if attempt == 0:
                logger.warning(f"Job card extraction failed, retrying: {e}")
                await asyncio.sleep(1)
            else:
                logger.error(f"Job card extraction failed permanently: {e}")
                job_data = {"caller_phone": caller_phone, "call_type": "unknown"}
    job_data["call_sid"] = call_sid

    # Save callback number and caller name to call record
    from app.db.calls import save_call
    call_updates = {"caller_name": job_data.get("caller_name", "")}
    if job_data.get("callback_number"):
        call_updates["callback_number"] = job_data["callback_number"]
    await save_call(call_sid, call_updates)

    # 2. Save to Firestore (with idempotency check on call_sid)
    job_data["transcript"] = transcript_text
    from app.db.jobs import get_job_by_call_sid
    existing_job = await get_job_by_call_sid(call_sid)
    if existing_job:
        job_id = existing_job["job_id"]
        logger.info(f"Job already exists for call_sid {call_sid}, skipping creation: {job_id}")
    else:
        job_id = await save_job(job_data)

    # 2b. Auto-create job in Jobber for service requests
    if contractor.get("jobber_access_token") and job_data.get("call_type") == "service_request":
        asyncio.create_task(_create_jobber_job(contractor, job_data))

    # 3. Send SMS to contractor (in their language)
    user_language = contractor.get("user_language", "en")
    if contractor_phone:
        contractor_sms = await _format_contractor_sms(job_data, job_id, user_language=user_language)
        try:
            await send_sms(contractor_phone, contractor_sms, from_number=twilio_number)
            logger.info(f"Job card SMS sent to contractor: {redact_phone(contractor_phone)}")
        except Exception as e:
            logger.error(f"Job card SMS to contractor failed: {e}")
    else:
        owner_phone = getattr(settings, "user_phone", "")
        if owner_phone:
            contractor_sms = await _format_contractor_sms(job_data, job_id, user_language=user_language)
            try:
                await send_sms(owner_phone, contractor_sms, from_number=twilio_number)
                logger.info(f"Job card SMS sent to owner: {redact_phone(owner_phone)}")
            except Exception as e:
                logger.error(f"Job card SMS to owner failed: {e}")

    # 4. Send confirmation SMS to caller (service requests only)
    call_type = job_data.get("call_type", "unknown")
    if caller_phone and caller_phone != contractor_phone and call_type == "service_request":
        caller_sms = await _format_caller_sms_with_estimate(
            job_data, job_id, contractor, twilio_number
        )

        # Attach vCard if available
        vcard_url = _get_vcard_url(contractor)
        try:
            if vcard_url:
                await send_mms(caller_phone, caller_sms, media_url=vcard_url, from_number=twilio_number)
            else:
                await send_sms(caller_phone, caller_sms, from_number=twilio_number)
            logger.info(f"Confirmation SMS sent to caller: {redact_phone(caller_phone)}")
        except Exception as e:
            logger.error(f"Confirmation SMS to caller failed: {e}")

    # 5. For non-service calls, still send vCard if we have one
    elif caller_phone and caller_phone != contractor_phone and call_type not in ("spam",):
        vcard_url = _get_vcard_url(contractor)
        if vcard_url:
            business_name = contractor.get("business_name", "us")
            msg = f"Thanks for calling {business_name}! Save our contact info:"
            try:
                await send_mms(caller_phone, msg, media_url=vcard_url, from_number=twilio_number)
            except Exception as e:
                logger.error(f"vCard MMS to caller failed: {e}")

        # 5b. Auto-reply SMS for non-service calls (opt-in)
        if contractor.get("auto_reply_sms", False):
            await _send_auto_reply(caller_phone, contractor, twilio_number, transcript_text, caller_language=caller_language)

    # 6. Send call summary push notification
    await _send_summary_push(job_data, contractor)

    logger.info(f"Post-call processing complete: job {job_id}")


async def _format_contractor_sms(job_data: dict, job_id: str, user_language: str = "en") -> str:
    """Format the SMS for the contractor in their language."""
    call_type = job_data.get("call_type", "unknown")
    urgency = job_data.get("urgency", "none")
    icon = URGENCY_ICONS.get(urgency, "\U0001f4de")
    header = CALL_TYPE_HEADERS.get(call_type, "MISSED CALL")

    name = job_data.get("caller_name", "") or "Unknown"
    business = job_data.get("business_name", "")
    phone = job_data.get("caller_phone", "")
    address = job_data.get("address", "")
    issue = job_data.get("issue_description", "")
    message = job_data.get("message", "")
    callback = job_data.get("callback_number", "")

    lines = [f"{icon} {header}"]

    if business:
        lines.append(f"From: {name} ({business})")
    else:
        lines.append(f"From: {name}")

    if phone:
        lines.append(f"\U0001f4de {phone}")
    if address:
        lines.append(f"\U0001f4cd {address}")
    if issue:
        lines.append(f"Re: {issue}")

    if call_type == "service_request" and urgency != "none":
        lines.append(f"Urgency: {urgency.upper().replace('_', ' ')}")

    if message:
        lines.append(f"Message: {message}")
    if callback and callback != phone:
        lines.append(f"Callback: {callback}")
    if phone:
        lines.append(f"\u2192 Tap to call: tel:{phone}")

    sms = "\n".join(lines)

    # Translate to contractor's language if not English
    if user_language and user_language != "en":
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            resp = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                messages=[{"role": "user", "content": (
                    f"Translate this call notification SMS to language code '{user_language}'. "
                    f"Keep all phone numbers, names, and the 'tel:' link exactly as-is. "
                    f"Keep emojis. Only translate the labels and descriptions. "
                    f"Return ONLY the translated message:\n\n{sms}"
                )}],
            )
            sms = resp.content[0].text.strip()
        except Exception as e:
            logger.warning(f"Contractor SMS translation failed: {e}")

    return sms


async def _format_caller_sms_with_estimate(
    job_data: dict,
    job_id: str,
    contractor: dict,
    twilio_number: str,
) -> str:
    """Format caller SMS with estimate link for service requests."""
    owner_name = contractor.get("owner_name", settings.user_name)
    business_name = contractor.get("business_name", f"{owner_name}'s office")
    issue = job_data.get("issue_description", "your request")
    caller_phone = job_data.get("caller_phone", "")
    contractor_id = contractor.get("contractor_id", "")

    base_msg = (
        f"Thanks for calling {business_name}! Your request has been received "
        f"and {owner_name} will get back to you shortly.\n\n"
        f"Issue: {issue}\n"
        f"Ref: KV-{job_id[:6].upper()}"
    )

    # Generate estimate link for service requests if contractor has services
    services = contractor.get("services", [])
    if services and contractor_id and caller_phone:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{settings.cloud_run_url}/api/estimates/create-token",
                    headers={
                        "Authorization": f"Bearer {settings.api_bearer_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "contractor_id": contractor_id,
                        "caller_phone": caller_phone,
                        "call_sid": job_data.get("call_sid", ""),
                    },
                    timeout=5.0,
                )
                if response.status_code == 200:
                    data = response.json()
                    estimate_url = data.get("url", "")
                    if estimate_url:
                        base_msg += (
                            f"\n\n\U0001f4f7 Want a free AI diagnosis and estimate? "
                            f"Upload a photo or video of the issue:\n{estimate_url}"
                        )
        except Exception as e:
            logger.warning(f"Failed to create estimate token: {e}")

    return base_msg


async def _send_summary_push(job_data: dict, contractor: dict):
    """Send a push notification with the call summary after job card extraction.

    Only sends if the caller left a message (has issue_description or message content).
    If the caller hung up without leaving info, no summary push is sent.
    """
    try:
        from app.services.push_notification import send_regular_push, get_device_token

        caller_name = job_data.get("caller_name", "") or "Unknown caller"
        issue = job_data.get("issue_description", "") or job_data.get("message", "")
        call_type = job_data.get("call_type", "unknown")

        # Only send summary if the caller actually left a message
        if not issue and call_type in ("spam", "unknown"):
            logger.info("No message left — skipping summary push")
            return

        contractor_id = contractor.get("contractor_id", "")
        device_token = await get_device_token(contractor_id=contractor_id)
        if not device_token:
            return

        urgency = job_data.get("urgency", "")

        # Build summary body — prefix with "Caller says:" to prevent social engineering
        if issue:
            body = f"Caller says: {caller_name} called about {issue}"
            if urgency and urgency not in ("none", ""):
                body += f". Urgency: {urgency}"
        else:
            body = f"Caller says: {caller_name} called"

        # Truncate to 200 chars
        if len(body) > 200:
            body = body[:197] + "..."

        await send_regular_push(
            device_token=device_token,
            title="Call Summary",
            body=body,
            call_sid=job_data.get("call_sid", ""),
            caller_phone=job_data.get("caller_phone", ""),
            caller_name=caller_name,
        )
        logger.info(f"Call summary push sent for {call_type} call from {caller_name[:1] if caller_name else ''}***")
    except Exception as e:
        logger.warning(f"Call summary push failed: {e}")


def _detect_spanish(transcript_text: str) -> bool:
    """Simple check if transcript is likely in Spanish."""
    spanish_indicators = ["hola", "gracias", "por favor", "necesito", "quiero", "puede",
                          "llamar", "ayuda", "buenos", "buenas", "señor", "señora"]
    text_lower = transcript_text.lower()
    matches = sum(1 for word in spanish_indicators if word in text_lower)
    return matches >= 2


async def _send_auto_reply(caller_phone: str, contractor: dict, twilio_number: str, transcript_text: str = "", caller_language: str = "en"):
    """Send a courtesy auto-reply SMS to the caller in their language. Opt-in, rate-limited via Firestore."""
    if not caller_phone:
        return

    # Per-phone dedup: max 1 auto-reply per phone per hour (Firestore-backed)
    now = time.time()
    phone_key = caller_phone.replace("+", "").replace("-", "").replace(" ", "")

    try:
        from app.db.firestore_client import get_firestore_client
        db = get_firestore_client()
        loop = asyncio.get_event_loop()
        doc = await loop.run_in_executor(
            None, lambda: db.collection("auto_reply_timestamps").document(phone_key).get()
        )
        if doc.exists:
            last_sent = doc.to_dict().get("last_sent", 0)
            if now - last_sent < 3600:
                logger.info(f"Auto-reply skipped (dedup): {redact_phone(caller_phone)}")
                return
    except Exception as e:
        logger.warning(f"Auto-reply rate limit check failed: {e}")
        return  # Fail closed — skip if we can't verify

    # Block premium/shortcode numbers (less than 10 digits or starts with non-1)
    if len(phone_key) < 10 or (len(phone_key) == 11 and not phone_key.startswith("1")):
        logger.info(f"Auto-reply skipped (invalid destination): {redact_phone(caller_phone)}")
        return

    owner_name = contractor.get("owner_name", "")
    business_name = contractor.get("business_name", owner_name or "us")
    reply_name = owner_name or business_name

    # Generate reply in the caller's language
    if caller_language == "en":
        msg = f"Thanks for calling {business_name}! {reply_name} got your message and will get back to you shortly."
    else:
        # Use Claude to generate a natural reply in the caller's language
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            resp = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=100,
                messages=[{"role": "user", "content": (
                    f"Write a brief, friendly SMS auto-reply in language code '{caller_language}'. "
                    f"The message should say: Thanks for calling {business_name}. "
                    f"{reply_name} got your message and will get back to you shortly. "
                    f"Keep it under 160 characters. Return ONLY the message text, nothing else."
                )}],
            )
            msg = resp.content[0].text.strip()
        except Exception as e:
            logger.warning(f"Auto-reply translation failed, falling back to English: {e}")
            msg = f"Thanks for calling {business_name}! {reply_name} got your message and will get back to you shortly."

    try:
        await send_sms(caller_phone, msg, from_number=twilio_number)
        # Record timestamp in Firestore
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: db.collection("auto_reply_timestamps").document(phone_key).set({"last_sent": now})
        )
        logger.info(f"Auto-reply SMS sent to: {redact_phone(caller_phone)} (lang={caller_language})")
    except Exception as e:
        logger.warning(f"Auto-reply SMS failed: {e}")


def _get_vcard_url(contractor: dict) -> str:
    """Generate a signed vCard URL for the contractor, or empty string."""
    contractor_id = contractor.get("contractor_id", "")
    if not contractor_id:
        return ""
    try:
        from app.services.vcard import generate_signed_vcard_url
        return generate_signed_vcard_url(contractor_id)
    except Exception as e:
        logger.warning(f"Failed to generate vCard URL: {e}")
        return ""


async def _create_jobber_job(contractor: dict, job_data: dict):
    """Best-effort: create a job card in Jobber after a service-request call."""
    try:
        from app.services.jobber import lookup_customer, create_job
        token = contractor["jobber_access_token"]
        caller_phone = job_data.get("caller_phone", "")

        # Look up existing client so we can attach job to their record
        client_id = None
        if caller_phone:
            customer = await asyncio.wait_for(
                lookup_customer(token, caller_phone),
                timeout=3.0,
            )
            if customer:
                client_id = customer.get("id")

        issue = job_data.get("issue_description", "Phone inquiry")
        caller_name = job_data.get("caller_name", "")
        address = job_data.get("address", "")

        instructions_parts = []
        if caller_name:
            instructions_parts.append(f"Caller: {caller_name}")
        if caller_phone:
            instructions_parts.append(f"Phone: {caller_phone}")
        if address:
            instructions_parts.append(f"Address: {address}")
        instructions_parts.append(f"Screened by Kevin AI")

        job_id = await asyncio.wait_for(
            create_job(token, {
                "title": issue[:100],
                "instructions": "\n".join(instructions_parts),
                "client_id": client_id,
            }),
            timeout=5.0,
        )
        if job_id:
            logger.info(f"Jobber job created: {job_id} for call {job_data.get('call_sid', '')[:8]}")
        else:
            logger.warning("Jobber job creation returned no ID")
    except asyncio.TimeoutError:
        logger.warning("Jobber job creation timed out")
    except Exception as e:
        logger.warning(f"Jobber job creation failed (non-critical): {e}")
