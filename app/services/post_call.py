"""Post-call processing: extract job card, save to Firestore, send SMS.

Supports two modes:
- "personal": simple missed-call notification, no job card
- "business" (default): full job card extraction + estimate link + vCard
"""

import asyncio
from dataclasses import dataclass, field
import logging
import re
import time

from app.db import calls as call_db
from app.db import jobs as job_db
from app.services.job_card import extract_job_card
from app.services.entitlements import effective_mode
from app.services.gated_actions import ActionKey, GateContext, check_gated_action
from app.services import jobber as jobber_service
from app.services.side_effect_audit import record_gate_decision
from app.services.sms import MessageDeliveryContext, send_sms, send_mms
from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

_SAFE_LOG_METRIC_PATTERN = re.compile(r"[^a-zA-Z0-9_.:-]+")


def _call_label(call_sid: str) -> str:
    return str(call_sid or "")[:8] or "unknown"


def _safe_log_metric(value: object) -> str:
    if isinstance(value, (bool, int, float)):
        return str(value)
    sanitized = _SAFE_LOG_METRIC_PATTERN.sub("_", str(value or "")[:40]).strip("_.:-")
    return sanitized or "unknown"


def _log_post_call_event(
    event: str,
    call_sid: str = "",
    *,
    level: int = logging.INFO,
    **metrics: object,
) -> None:
    metric_text = " ".join(
        f"{key}={_safe_log_metric(value)}" for key, value in metrics.items()
    )
    suffix = f" {metric_text}" if metric_text else ""
    logger.log(
        level,
        "post_call_event event=%s call=%s%s",
        event,
        _call_label(call_sid),
        suffix,
    )


def _log_post_call_exception(
    event: str,
    error: BaseException,
    call_sid: str = "",
    *,
    level: int = logging.WARNING,
) -> None:
    _log_post_call_event(
        event,
        call_sid,
        level=level,
        exception_type=type(error).__name__,
    )

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
    "out_of_scope": "OUT OF SCOPE REQUEST",
    "personal": "PERSONAL CALL",
    "business": "BUSINESS CALL",
    "spam": "SPAM",
    "unknown": "MISSED CALL",
}

SAFE_SUMMARY_URGENCY_LABELS = {
    "emergency": "urgent",
    "same_day": "same-day",
    "routine": "routine",
    "quote": "quote",
}


@dataclass(frozen=True)
class PostCallResult:
    status: str
    completed_effects: tuple[str, ...]
    failed_effects: tuple[str, ...]


@dataclass
class _PostCallTracker:
    completed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)

    def record(self, effect: str, succeeded: bool | None) -> None:
        if succeeded is None:
            return
        target = self.completed if succeeded else self.failed
        target.add(effect)

    def result(self) -> PostCallResult:
        if self.failed and self.completed:
            status = "partial"
        elif self.failed:
            status = "failed"
        else:
            status = "complete"
        return PostCallResult(
            status=status,
            completed_effects=tuple(sorted(self.completed)),
            failed_effects=tuple(sorted(self.failed)),
        )


def _record_effect(
    tracker: _PostCallTracker | None,
    effect: str,
    succeeded: bool | None,
) -> None:
    if tracker:
        tracker.record(effect, succeeded)


def _safe_summary_push_body(caller_name: str, call_type: str, urgency: str = "") -> str:
    """Return lock-screen-safe summary copy with no raw issue text."""
    urgency_label = SAFE_SUMMARY_URGENCY_LABELS.get((urgency or "").strip().lower())
    if urgency_label:
        return f"New {urgency_label} call summary. Open Kevin for details."
    if call_type == "service_request":
        return "New service call summary. Open Kevin for details."
    return "New call summary. Open Kevin for details."


def _post_call_gate(contractor: dict, action: ActionKey, call_sid: str, *, owner_confirmed: bool = False):
    context = GateContext(
        source="post_call",
        actor="system",
        idempotency_key=f"{call_sid}:{action.value}",
        owner_confirmed=owner_confirmed,
    )
    decision = check_gated_action(contractor, action, context)
    record_gate_decision(
        action=action,
        contractor_id=contractor.get("contractor_id", ""),
        source="post_call",
        resource_id=call_sid,
        decision=decision,
    )
    return decision, context


def _call_summary_from_job_data(job_data: dict) -> str:
    return (job_data.get("issue_description") or job_data.get("message") or "").strip()


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _normalize_callback_number(callback_number: str, caller_phone: str) -> str:
    """Resolve last-four caller-ID confirmations to a dialable callback number."""
    callback = (callback_number or "").strip()
    if not callback:
        return ""

    callback_digits = _digits_only(callback)
    caller_digits = _digits_only(caller_phone)

    if len(callback_digits) >= 7:
        return callback

    if len(callback_digits) == 4:
        if caller_digits.endswith(callback_digits):
            return caller_phone
        return ""

    lowered = callback.lower()
    if caller_phone and any(
        phrase in lowered
        for phrase in ("caller id", "caller-id", "same number", "that number", "this number")
    ):
        return caller_phone

    return callback


def _normalize_job_callback_data(job_data: dict) -> dict:
    normalized = dict(job_data)
    normalized["callback_number"] = _normalize_callback_number(
        str(normalized.get("callback_number", "")),
        str(normalized.get("caller_phone", "")),
    )
    return normalized


def _call_record_updates_from_job_data(job_data: dict) -> dict:
    updates = {"caller_name": job_data.get("caller_name", "")}
    if job_data.get("callback_number"):
        updates["callback_number"] = job_data["callback_number"]

    summary = _call_summary_from_job_data(job_data)
    if summary:
        updates["summary"] = summary

    call_type = job_data.get("call_type", "")
    if call_type:
        updates["call_type"] = call_type

    urgency = job_data.get("urgency", "")
    if urgency:
        updates["urgency"] = urgency

    return updates


async def process_post_call(
    transcript_lines: list,
    caller_phone: str,
    call_sid: str,
    contractor_phone: str = "",
    twilio_number: str = "",
    contractor: dict = None,
    caller_language: str = "en",
) -> PostCallResult:
    """Full post-call pipeline: extract -> save -> notify contractor + caller."""
    contractor = contractor or {}
    tracker = _PostCallTracker()
    mode = contractor.get("effective_mode") or effective_mode(contractor)

    # Treat legacy "kevin" mode as "business"
    if mode not in ("personal",):
        mode = "business"

    try:
        transcript_text = "\n".join(transcript_lines)

        if mode == "personal":
            await _process_personal(transcript_text, caller_phone, call_sid,
                                    contractor_phone, twilio_number,
                                    user_language=contractor.get("user_language", "en"),
                                    contractor=contractor,
                                    tracker=tracker)
        else:
            await _process_business(transcript_text, caller_phone, call_sid,
                                    contractor_phone, twilio_number, contractor,
                                    caller_language=caller_language,
                                    tracker=tracker)

    except Exception as error:
        _log_post_call_exception(
            "processing_error",
            error,
            call_sid,
            level=logging.ERROR,
        )
        tracker.record("processing", False)

    result = tracker.result()
    _log_post_call_event(
        "processing_result",
        call_sid,
        status=result.status,
        completed_count=len(result.completed_effects),
        failed_count=len(result.failed_effects),
    )
    return result


async def _process_personal(
    transcript_text: str,
    caller_phone: str,
    call_sid: str,
    contractor_phone: str,
    twilio_number: str = "",
    user_language: str = "en",
    contractor: dict | None = None,
    tracker: _PostCallTracker | None = None,
):
    """Personal mode: simple notification, no job card extraction."""
    # Simple extraction: just get name and reason via Claude (with retry)
    from app.services.job_card import extract_job_card
    job_data = None
    for attempt in range(2):
        try:
            job_data = await extract_job_card(transcript_text, caller_phone)
            break
        except Exception as error:
            if attempt == 0:
                _log_post_call_exception("job_card_extract_retry", error, call_sid)
                await asyncio.sleep(1)
            else:
                _log_post_call_exception(
                    "job_card_extract_failed",
                    error,
                    call_sid,
                    level=logging.ERROR,
                )
                job_data = {"caller_phone": caller_phone, "call_type": "unknown"}

    name = job_data.get("caller_name", "") or "Unknown caller"
    reason = job_data.get("issue_description", "") or job_data.get("message", "") or "No details"
    callback = job_data.get("callback_number", "") or caller_phone

    call_job_data = dict(job_data)
    call_job_data["caller_name"] = name
    call_job_data.setdefault("caller_phone", caller_phone)
    call_job_data = _normalize_job_callback_data(call_job_data)
    call_saved = await call_db.save_call(
        call_sid,
        _call_record_updates_from_job_data(call_job_data),
    )
    _record_effect(tracker, "call_record", call_saved)
    contact_saved = await _update_caller_contact(
        job_data,
        (contractor or {}).get("contractor_id", ""),
        call_sid,
    )
    _record_effect(tracker, "caller_contact", contact_saved)

    # Send simple SMS to owner (in their language)
    owner_phone = contractor_phone
    if owner_phone and twilio_number:
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
                    model=settings.anthropic_model,
                    max_tokens=200,
                    messages=[{"role": "user", "content": (
                        f"Translate this missed call notification to language code '{user_language}'. "
                        f"Keep phone numbers and names exactly as-is. Keep emojis. "
                        f"Return ONLY the translated message:\n\n{sms}"
                    )}],
                )
                sms = resp.content[0].text.strip()
            except Exception as error:
                _log_post_call_exception("personal_sms_translation_error", error, call_sid)
        try:
            sent = await send_sms(
                owner_phone,
                sms,
                from_number=twilio_number,
                delivery_context=MessageDeliveryContext(call_sid, "owner_sms"),
            )
            _record_effect(tracker, "owner_sms", sent)
            if sent:
                _log_post_call_event("personal_sms_sent", call_sid)
            else:
                _log_post_call_event(
                    "personal_sms_failed",
                    call_sid,
                    level=logging.ERROR,
                )
        except Exception as error:
            _record_effect(tracker, "owner_sms", False)
            _log_post_call_exception(
                "personal_sms_error",
                error,
                call_sid,
                level=logging.ERROR,
            )
    else:
        _record_effect(tracker, "owner_sms", False)
        _log_post_call_event(
            "personal_sms_configuration_missing",
            call_sid,
            level=logging.ERROR,
        )

    # Send call summary push for personal mode
    summary_sent = await _send_summary_push(job_data, contractor or {})
    _record_effect(tracker, "summary_push", summary_sent)


async def _process_business(
    transcript_text: str,
    caller_phone: str,
    call_sid: str,
    contractor_phone: str,
    twilio_number: str,
    contractor: dict,
    caller_language: str = "en",
    tracker: _PostCallTracker | None = None,
):
    """Business mode: full job card + estimate link + vCard."""
    # 1. Extract structured job card from transcript (with retry)
    job_data = None
    for attempt in range(2):
        try:
            job_data = await extract_job_card(transcript_text, caller_phone, contractor=contractor)
            break
        except Exception as error:
            if attempt == 0:
                _log_post_call_exception("job_card_extract_retry", error, call_sid)
                await asyncio.sleep(1)
            else:
                _log_post_call_exception(
                    "job_card_extract_failed",
                    error,
                    call_sid,
                    level=logging.ERROR,
                )
                job_data = {"caller_phone": caller_phone, "call_type": "unknown"}
    job_data["call_sid"] = call_sid
    job_data.setdefault("caller_phone", caller_phone)
    job_data = _normalize_job_callback_data(job_data)
    contractor_id = contractor.get("contractor_id", "")
    if contractor_id:
        job_data["contractor_id"] = contractor_id

    call_saved = await call_db.save_call(
        call_sid,
        _call_record_updates_from_job_data(job_data),
    )
    _record_effect(tracker, "call_record", call_saved)
    contact_saved = await _update_caller_contact(
        job_data,
        contractor_id,
        call_sid,
    )
    _record_effect(tracker, "caller_contact", contact_saved)

    # 2. Save to Firestore (with idempotency check on call_sid)
    job_data["transcript"] = transcript_text
    existing_job = await job_db.get_job_by_call_sid(call_sid)
    if existing_job:
        job_id = existing_job["job_id"]
        _log_post_call_event("job_create_deduplicated", call_sid)
    else:
        job_data_for_storage = dict(job_data)
        job_data_for_storage.pop("transcript", None)
        job_id = await job_db.save_job(job_data_for_storage)
    _record_effect(tracker, "job_record", True)

    # 2b. Best-effort Jobber lead capture for service requests.
    if (
        contractor.get("jobber_access_token")
        and job_data.get("call_type") == "service_request"
        and _jobber_lead_capture_enabled(contractor)
    ):
        jobber_captured = await _capture_jobber_lead(contractor, job_data, job_id)
        _record_effect(tracker, "jobber_lead", jobber_captured)

    # 3. Send SMS to contractor (in their language)
    user_language = contractor.get("user_language", "en")
    if contractor_phone and twilio_number:
        contractor_sms = await _format_contractor_sms(job_data, job_id, user_language=user_language)
        try:
            sent = await send_sms(
                contractor_phone,
                contractor_sms,
                from_number=twilio_number,
                delivery_context=MessageDeliveryContext(call_sid, "owner_sms"),
            )
            _record_effect(tracker, "owner_sms", sent)
            if sent:
                _log_post_call_event("contractor_sms_sent", call_sid)
            else:
                _log_post_call_event(
                    "contractor_sms_failed",
                    call_sid,
                    level=logging.ERROR,
                )
        except Exception as error:
            _record_effect(tracker, "owner_sms", False)
            _log_post_call_exception(
                "contractor_sms_error",
                error,
                call_sid,
                level=logging.ERROR,
            )
    else:
        _record_effect(tracker, "owner_sms", False)
        _log_post_call_event(
            "owner_sms_configuration_missing",
            call_sid,
            level=logging.ERROR,
        )

    # 4. Send confirmation SMS to caller (service requests only)
    call_type = job_data.get("call_type", "unknown")
    if caller_phone and caller_phone != contractor_phone and call_type == "service_request":
        caller_sms = await _format_caller_sms_with_estimate(
            job_data, job_id, contractor, twilio_number
        )

        # Attach vCard if available
        vcard_url = _get_vcard_url(contractor)
        try:
            sent_confirmation = False
            if vcard_url:
                action = ActionKey.CALLER_CONFIRMATION_MMS
                decision, context = _post_call_gate(contractor, action, call_sid)
                if decision.allowed:
                    sent_confirmation = await send_mms(
                        caller_phone,
                        caller_sms,
                        media_url=vcard_url,
                        from_number=twilio_number,
                        contractor=contractor,
                        action=action,
                        gate_context=context,
                        delivery_context=MessageDeliveryContext(
                            call_sid,
                            "caller_confirmation",
                        ),
                    )
                else:
                    logger.info("Caller confirmation MMS blocked by gate", extra={"reason": decision.reason.value})
            else:
                action = ActionKey.CALLER_CONFIRMATION_SMS
                decision, context = _post_call_gate(contractor, action, call_sid)
                if decision.allowed:
                    sent_confirmation = await send_sms(
                        caller_phone,
                        caller_sms,
                        from_number=twilio_number,
                        contractor=contractor,
                        action=action,
                        gate_context=context,
                        delivery_context=MessageDeliveryContext(
                            call_sid,
                            "caller_confirmation",
                        ),
                    )
                else:
                    logger.info("Caller confirmation SMS blocked by gate", extra={"reason": decision.reason.value})
            if sent_confirmation:
                _log_post_call_event("caller_confirmation_sent", call_sid)
            _record_effect(
                tracker,
                "caller_confirmation",
                sent_confirmation if decision.allowed else None,
            )
        except Exception as error:
            _record_effect(tracker, "caller_confirmation", False)
            _log_post_call_exception(
                "caller_confirmation_error",
                error,
                call_sid,
                level=logging.ERROR,
            )

    # 5. For non-service calls, still send vCard if we have one
    elif caller_phone and caller_phone != contractor_phone and call_type not in ("spam",):
        vcard_url = _get_vcard_url(contractor)
        if vcard_url:
            business_name = contractor.get("business_name", "us")
            msg = f"Thanks for calling {business_name}! Save our contact info:"
            try:
                action = ActionKey.CALLER_VCARD_MMS
                decision, context = _post_call_gate(contractor, action, call_sid)
                if decision.allowed:
                    vcard_sent = await send_mms(
                        caller_phone,
                        msg,
                        media_url=vcard_url,
                        from_number=twilio_number,
                        contractor=contractor,
                        action=action,
                        gate_context=context,
                        delivery_context=MessageDeliveryContext(
                            call_sid,
                            "caller_vcard",
                        ),
                    )
                    _record_effect(tracker, "caller_vcard", vcard_sent)
                else:
                    logger.info("Caller vCard MMS blocked by gate", extra={"reason": decision.reason.value})
            except Exception as error:
                _record_effect(tracker, "caller_vcard", False)
                _log_post_call_exception(
                    "caller_vcard_error",
                    error,
                    call_sid,
                    level=logging.ERROR,
                )

        # 5b. Auto-reply SMS for non-service calls (opt-in)
        if contractor.get("auto_reply_sms", False):
            decision, context = _post_call_gate(contractor, ActionKey.CALLER_AUTO_REPLY, call_sid)
            if decision.allowed:
                auto_reply_sent = await _send_auto_reply(
                    caller_phone,
                    contractor,
                    twilio_number,
                    transcript_text,
                    caller_language=caller_language,
                    gate_context=context,
                    call_sid=call_sid,
                )
                _record_effect(tracker, "caller_auto_reply", auto_reply_sent)
            else:
                logger.info("Auto-reply blocked by gate", extra={"reason": decision.reason.value})

    # 6. Send call summary push notification
    summary_sent = await _send_summary_push(job_data, contractor)
    _record_effect(tracker, "summary_push", summary_sent)

    _log_post_call_event("processing_complete", call_sid)


async def _update_caller_contact(
    job_data: dict,
    contractor_id: str,
    call_sid: str,
) -> bool | None:
    """Persist one tenant-scoped caller contact from the claimed job card."""
    caller_phone = str(job_data.get("caller_phone") or "")
    caller_name = str(job_data.get("caller_name") or "")
    business_name = str(job_data.get("business_name") or "")
    if not contractor_id or not caller_phone or not (caller_name or business_name):
        return None

    from app.db.contacts import get_caller_contact, upsert_caller_contact

    try:
        existing = await get_caller_contact(contractor_id, caller_phone) or {}
        now = time.time()
        summary = _call_summary_from_job_data(job_data)
        if existing:
            updates = {
                "last_call_at": now,
                "last_call_sid": call_sid,
            }
            if caller_name and not existing.get("caller_name"):
                updates["caller_name"] = caller_name
            if business_name and not existing.get("business_name"):
                updates["business_name"] = business_name
            if summary:
                history = [
                    item
                    for item in list(existing.get("call_history") or [])
                    if isinstance(item, dict) and item.get("call_sid") != call_sid
                ]
                history.append(
                    {
                        "date": now,
                        "call_sid": call_sid,
                        "summary": summary,
                    }
                )
                updates["call_history"] = history[-20:]
            saved = await upsert_caller_contact(
                contractor_id,
                caller_phone,
                updates,
                merge=True,
            )
        else:
            data = {
                "caller_name": caller_name,
                "business_name": business_name,
                "phone": caller_phone,
                "created_at": now,
                "last_call_at": now,
                "last_call_sid": call_sid,
                "notes": "",
                "tags": [],
                "call_history": [],
            }
            if summary:
                data["call_history"] = [
                    {
                        "date": now,
                        "call_sid": call_sid,
                        "summary": summary,
                    }
                ]
            saved = await upsert_caller_contact(
                contractor_id,
                caller_phone,
                data,
                merge=False,
            )
        if saved:
            _log_post_call_event("caller_contact_saved", call_sid)
        return saved
    except Exception as error:
        _log_post_call_exception("caller_contact_error", error, call_sid)
        return False


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
                model=settings.anthropic_model,
                max_tokens=300,
                messages=[{"role": "user", "content": (
                    f"Translate this call notification SMS to language code '{user_language}'. "
                    f"Keep all phone numbers, names, and the 'tel:' link exactly as-is. "
                    f"Keep emojis. Only translate the labels and descriptions. "
                    f"Return ONLY the translated message:\n\n{sms}"
                )}],
            )
            sms = resp.content[0].text.strip()
        except Exception as error:
            _log_post_call_exception("contractor_sms_translation_error", error)

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
            decision, _context = _post_call_gate(contractor, ActionKey.ESTIMATE_TOKEN_CREATE, job_data.get("call_sid", ""))
            if not decision.allowed:
                logger.info("Estimate token creation blocked by gate", extra={"reason": decision.reason.value})
                return base_msg

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
        except Exception as error:
            _log_post_call_exception(
                "estimate_token_error",
                error,
                str(job_data.get("call_sid", "")),
            )

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
            return None

        contractor_id = contractor.get("contractor_id", "")
        device_token = await get_device_token(contractor_id=contractor_id)
        if not device_token:
            return None

        urgency = job_data.get("urgency", "")
        body = _safe_summary_push_body(caller_name, call_type, urgency)

        sent = await send_regular_push(
            device_token=device_token,
            title="Call Summary",
            body=body,
            call_sid=job_data.get("call_sid", ""),
            caller_phone=job_data.get("caller_phone", ""),
            caller_name=caller_name,
        )
        if sent:
            _log_post_call_event(
                "summary_push_sent",
                str(job_data.get("call_sid", "")),
            )
            return True
        _log_post_call_event(
            "summary_push_failed",
            str(job_data.get("call_sid", "")),
            level=logging.ERROR,
        )
        return False
    except Exception as error:
        _log_post_call_exception(
            "summary_push_error",
            error,
            str(job_data.get("call_sid", "")),
        )
        return False


def _detect_spanish(transcript_text: str) -> bool:
    """Simple check if transcript is likely in Spanish."""
    spanish_indicators = ["hola", "gracias", "por favor", "necesito", "quiero", "puede",
                          "llamar", "ayuda", "buenos", "buenas", "señor", "señora"]
    text_lower = transcript_text.lower()
    matches = sum(1 for word in spanish_indicators if word in text_lower)
    return matches >= 2


async def _send_auto_reply(
    caller_phone: str,
    contractor: dict,
    twilio_number: str,
    transcript_text: str = "",
    caller_language: str = "en",
    gate_context: GateContext | None = None,
    call_sid: str = "",
):
    """Send a courtesy auto-reply SMS to the caller in their language. Opt-in, rate-limited via Firestore."""
    if not caller_phone:
        return None

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
                _log_post_call_event("auto_reply_deduplicated", call_sid)
                return None
    except Exception as error:
        _log_post_call_exception("auto_reply_rate_limit_error", error, call_sid)
        return False

    # Block premium/shortcode numbers (less than 10 digits or starts with non-1)
    if len(phone_key) < 10 or (len(phone_key) == 11 and not phone_key.startswith("1")):
        _log_post_call_event("auto_reply_invalid_destination", call_sid)
        return None

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
                model=settings.anthropic_model,
                max_tokens=100,
                messages=[{"role": "user", "content": (
                    f"Write a brief, friendly SMS auto-reply in language code '{caller_language}'. "
                    f"The message should say: Thanks for calling {business_name}. "
                    f"{reply_name} got your message and will get back to you shortly. "
                    f"Keep it under 160 characters. Return ONLY the message text, nothing else."
                )}],
            )
            msg = resp.content[0].text.strip()
        except Exception as error:
            _log_post_call_exception("auto_reply_translation_error", error, call_sid)
            msg = f"Thanks for calling {business_name}! {reply_name} got your message and will get back to you shortly."

    try:
        gate_context = gate_context or GateContext(
            source="post_call",
            actor="system",
            idempotency_key=f"{call_sid}:caller_auto_reply" if call_sid else "",
        )
        sent = await send_sms(
            caller_phone,
            msg,
            from_number=twilio_number,
            contractor=contractor,
            action=ActionKey.CALLER_AUTO_REPLY,
            gate_context=gate_context,
            delivery_context=MessageDeliveryContext(
                call_sid,
                "caller_auto_reply",
            ),
        )
        if not sent:
            _log_post_call_event(
                "auto_reply_delivery_failed",
                call_sid,
                level=logging.ERROR,
            )
            return False
        # Record timestamp in Firestore
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: db.collection("auto_reply_timestamps").document(phone_key).set({"last_sent": now})
        )
        _log_post_call_event("auto_reply_sent", call_sid)
        return True
    except Exception as error:
        _log_post_call_exception("auto_reply_error", error, call_sid)
        return False


def _get_vcard_url(contractor: dict) -> str:
    """Generate a signed vCard URL for the contractor, or empty string."""
    contractor_id = contractor.get("contractor_id", "")
    if not contractor_id:
        return ""
    try:
        from app.services.vcard import generate_signed_vcard_url
        return generate_signed_vcard_url(contractor_id)
    except Exception as error:
        _log_post_call_exception("vcard_url_error", error)
        return ""


JOBBER_LOOKUP_TIMEOUT_SECONDS = 8.0
JOBBER_MUTATION_TIMEOUT_SECONDS = 15.0
JOBBER_NOTE_LIMIT = 5000
PHONE_LIKE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().,\-]{6,}\d)(?!\w)")


def _jobber_lead_capture_enabled(contractor: dict) -> bool:
    """Return True only for explicit server-enabled Jobber lead capture."""
    return contractor.get("jobber_lead_capture_enabled") is True


def _jobber_request_title(job_data: dict) -> str:
    """Build a concise Jobber Request title."""
    issue = (job_data.get("issue_description") or "").strip()
    if issue:
        return issue[:100]

    caller = (job_data.get("caller_name") or "").strip()
    if caller:
        return f"Phone inquiry from {caller}"[:100]

    return "Phone inquiry from Hey Kevin"


def _format_jobber_lead_note(job_data: dict) -> str:
    """Format deterministic Hey Kevin call details for a Jobber Request note."""
    lines = ["Source: Hey Kevin", "Lead captured from an AI-screened phone call."]

    fields = [
        ("Caller", job_data.get("caller_name")),
        ("Phone", job_data.get("caller_phone")),
        ("Callback", job_data.get("callback_number")),
        ("Address", job_data.get("address")),
        ("Urgency", job_data.get("urgency")),
        ("Issue", job_data.get("issue_description")),
        ("Message", job_data.get("message")),
    ]
    for label, value in fields:
        if value:
            lines.append(f"{label}: {_mask_phone_like_text(str(value))}")

    transcript = (job_data.get("transcript") or "").strip()
    if transcript:
        lines.extend(["", "Transcript:", _mask_phone_like_text(transcript)])

    note = "\n".join(lines).strip()
    if len(note) > JOBBER_NOTE_LIMIT:
        return note[: JOBBER_NOTE_LIMIT - 3].rstrip() + "..."
    return note


def _mask_phone_like_text(text: str) -> str:
    """Mask phone-shaped text in external notes while keeping the last 4 digits."""
    def _replace(match: re.Match) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) < 7:
            return match.group(0)
        return f"***{digits[-4:]}"

    return PHONE_LIKE_RE.sub(_replace, text)


async def _capture_jobber_lead(contractor: dict, job_data: dict, job_id: str):
    """Best-effort: capture a service-request call as a Jobber Request."""
    if not _jobber_lead_capture_enabled(contractor):
        return None

    call_sid = job_data.get("call_sid", "")
    try:
        claimed = await job_db.claim_jobber_sync(job_id)
        if not claimed:
            return None

        caller_phone = job_data.get("caller_phone", "")
        customer = None
        if caller_phone:
            try:
                customer = await asyncio.wait_for(
                    jobber_service.lookup_customer(contractor, caller_phone),
                    timeout=JOBBER_LOOKUP_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("Jobber customer lookup timed out; creating a new client")

        if not customer:
            customer = await asyncio.wait_for(
                jobber_service.create_client(contractor, job_data),
                timeout=JOBBER_MUTATION_TIMEOUT_SECONDS,
            )

        client_id = _jobber_client_id(customer)
        property_id = _jobber_property_id(customer)
        if not client_id:
            await _mark_jobber_sync_failed(job_id, call_sid, "client_missing")
            return False

        request = await asyncio.wait_for(
            jobber_service.create_request(
                contractor,
                {
                    "client_id": client_id,
                    "property_id": property_id,
                    "title": _jobber_request_title(job_data),
                },
            ),
            timeout=JOBBER_MUTATION_TIMEOUT_SECONDS,
        )
        request_id = (request or {}).get("id", "")
        if not request_id:
            await _mark_jobber_sync_failed(job_id, call_sid, "request_create_failed")
            return False

        note_id = ""
        try:
            note_id = await asyncio.wait_for(
                jobber_service.create_request_note(
                    contractor,
                    request_id,
                    _format_jobber_lead_note(job_data),
                ),
                timeout=JOBBER_MUTATION_TIMEOUT_SECONDS,
            ) or ""
        except asyncio.TimeoutError:
            logger.warning("Jobber request note creation timed out")
        except Exception as error:
            _log_post_call_exception("jobber_note_error", error, call_sid)

        updates = {
            "jobber_sync_status": "succeeded",
            "jobber_request_id": request_id,
            "jobber_client_id": client_id,
            "jobber_synced_at": time.time(),
        }
        request_url = (request or {}).get("jobberWebUri", "")
        if request_url:
            updates["jobber_request_url"] = request_url
        if note_id:
            updates["jobber_note_id"] = note_id

        await job_db.update_job(job_id, updates)
        await _mirror_jobber_sync_to_call(call_sid, updates)
        _log_post_call_event("jobber_lead_captured", call_sid)
        return True
    except asyncio.CancelledError:
        await _mark_jobber_sync_failed(job_id, call_sid, "cancelled")
        raise
    except asyncio.TimeoutError:
        await _mark_jobber_sync_failed(job_id, call_sid, "timeout")
        logger.warning("Jobber lead capture timed out")
        return False
    except Exception as error:
        error_type = type(error).__name__
        await _mark_jobber_sync_failed(job_id, call_sid, error_type)
        _log_post_call_exception("jobber_lead_capture_error", error, call_sid)
        return False


def _jobber_client_id(customer: dict | None) -> str:
    if not isinstance(customer, dict):
        return ""
    return customer.get("id", "")


def _jobber_property_id(customer: dict | None) -> str:
    if not isinstance(customer, dict):
        return ""
    if customer.get("property_id"):
        return customer["property_id"]
    nodes = ((customer.get("clientProperties") or {}).get("nodes") or [])
    if nodes:
        return nodes[0].get("id", "")
    return ""


async def _mark_jobber_sync_failed(job_id: str, call_sid: str, error: str):
    updates = {
        "jobber_sync_status": "failed",
        "jobber_sync_error": error,
        "jobber_sync_finished_at": time.time(),
    }
    await job_db.update_job(job_id, updates)
    await _mirror_jobber_sync_to_call(call_sid, updates)


async def _mirror_jobber_sync_to_call(call_sid: str, updates: dict):
    if not call_sid:
        return
    try:
        await call_db.save_call(call_sid, updates)
    except asyncio.CancelledError:
        logger.warning("Jobber sync call mirror cancelled")
    except Exception as error:
        _log_post_call_exception("jobber_sync_mirror_error", error, call_sid)
