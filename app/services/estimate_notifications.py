"""Estimate SMS notifications for callers and contractors."""

from typing import Callable, Optional

from app.services.gated_actions import ActionKey, GateContext
from app.services.sms import send_sms
from app.utils.logging import get_logger

logger = get_logger(__name__)


async def send_estimate_notifications(
    caller_phone: str,
    contractor: Optional[dict],
    call_sid: str,
    token_hash: str,
    result: Optional[dict] = None,
    is_failure: bool = False,
    watch_url: str = "",
    send_sms_fn: Optional[Callable] = None,
) -> None:
    """Send customer and contractor notification SMS for an estimate outcome."""
    _send_sms = send_sms_fn or send_sms
    business_name = (contractor or {}).get("business_name", "the business")
    twilio_number = (contractor or {}).get("twilio_number", "")
    contractor_phone = (contractor or {}).get("owner_phone", "")

    # Customer SMS
    if caller_phone:
        if is_failure:
            customer_msg = (
                f"Thanks for your upload. We couldn't process this media. "
                f"Please call {business_name} directly at {twilio_number}."
                if twilio_number
                else f"Thanks for your upload. We couldn't process this media. "
                f"Please call {business_name} directly."
            )
        elif result and result.get("requires_manual_investigation"):
            customer_msg = (
                f"Thanks for your upload. This issue will require {business_name}'s "
                f"technician to manually investigate. We are unable to provide an "
                f"AI estimate at this time.\n\n"
                f"Call {business_name}: {twilio_number}"
            )
        else:
            diagnosis = (result or {}).get("diagnosis", "")
            est_min = (result or {}).get("estimate_min", 0)
            est_max = (result or {}).get("estimate_max", 0)
            customer_msg = (
                f"AI Diagnosis: {diagnosis}\n\n"
                f"Estimated Cost: ${est_min}-${est_max}\n\n"
                f"⚠️ This is an AI-generated estimate. The actual cost may differ "
                f"based on the technician's hands-on diagnosis.\n\n"
                f"Call {business_name}: {twilio_number}"
            )

        context = GateContext(
            source="estimate",
            actor="system",
            idempotency_key=f"{token_hash[:12]}:result",
        )
        try:
            await _send_sms(
                caller_phone,
                customer_msg,
                from_number=twilio_number,
                contractor=contractor,
                action=ActionKey.ESTIMATE_RESULT_SMS,
                gate_context=context,
            )
        except Exception as e:
            logger.error(
                "Failed to send customer estimate SMS for %s: %s",
                token_hash[:8],
                type(e).__name__,
            )

    # Contractor SMS
    if contractor_phone:
        if is_failure:
            contractor_msg = (
                f"📋 AI ESTIMATE FAILED\n"
                f"From: {caller_phone}\n"
                f"Result: We couldn't process the caller's media."
            )
        elif result and result.get("requires_manual_investigation"):
            contractor_msg = (
                f"📋 AI ESTIMATE REQUEST\n"
                f"From: {caller_phone}\n"
                f"Result: Requires manual investigation\n"
                f"The AI could not confidently diagnose the issue."
            )
        else:
            diagnosis = (result or {}).get("diagnosis", "")
            est_min = (result or {}).get("estimate_min", 0)
            est_max = (result or {}).get("estimate_max", 0)
            matched = ", ".join(s.get("name", "") for s in (result or {}).get("matched_services", []))
            contractor_msg = (
                f"📋 AI ESTIMATE SENT\n"
                f"To: {caller_phone}\n"
                f"Diagnosis: {diagnosis}\n"
                f"Services: {matched}\n"
                f"Estimate: ${est_min}-${est_max}\n"
                f"Confidence: {(result or {}).get('confidence', 'unknown')}"
            )

        if watch_url:
            contractor_msg += f"\n\nWatch the caller's video: {watch_url}"

        try:
            await _send_sms(contractor_phone, contractor_msg, from_number=twilio_number)
        except Exception as e:
            logger.error(
                "Failed to send contractor estimate SMS for %s: %s",
                token_hash[:8],
                type(e).__name__,
            )
