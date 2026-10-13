"""Estimate SMS notifications for callers and contractors."""

from typing import Callable, Optional

from app.services.gated_actions import ActionKey, GateContext
from app.services.sms import send_sms
from app.utils.logging import get_logger

logger = get_logger(__name__)


class EstimateNotificationOutcomeUncertain(RuntimeError):
    """A strict worker cannot prove that an estimate notification completed.

    The embedded/API paths retain their historical best-effort notification
    behavior.  The external worker has a durable ledger, so it must stop for
    operator reconciliation when a provider declines a send or its result is
    otherwise unknown.
    """


async def send_estimate_notifications(
    caller_phone: str,
    contractor: Optional[dict],
    call_sid: str,
    token_hash: str,
    result: Optional[dict] = None,
    is_failure: bool = False,
    watch_url: str = "",
    send_sms_fn: Optional[Callable] = None,
    *,
    strict: bool = False,
) -> None:
    """Send customer and contractor notification SMS for an estimate outcome.

    ``strict`` is reserved for the ledger-backed external worker.  It raises
    when a required provider send is not explicitly accepted, rather than
    allowing the caller to record a terminal notification outcome.
    """
    _send_sms = send_sms_fn or send_sms
    business_name = (contractor or {}).get("business_name", "the business")
    twilio_number = (contractor or {}).get("twilio_number", "")

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
            customer_sent = await _send_sms(
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
            if strict:
                raise EstimateNotificationOutcomeUncertain(
                    "Customer estimate SMS outcome is uncertain"
                ) from e
        else:
            if strict and customer_sent is not True:
                logger.error("Customer estimate SMS outcome uncertain for %s", token_hash[:8])
                raise EstimateNotificationOutcomeUncertain(
                    "Customer estimate SMS outcome is uncertain"
                )

    # Contractor SMS: single body construction block, sent only through send_owner_sms when contractor_id is present
    contractor_id = (contractor or {}).get("contractor_id", "")
    if contractor_id:
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
            from app.services.owner_sms import send_owner_sms
            owner_sent = await send_owner_sms(contractor_id, contractor_msg)
        except Exception as e:
            logger.error(
                "Failed to send contractor estimate SMS for %s: %s",
                token_hash[:8],
                type(e).__name__,
            )
            if strict:
                raise EstimateNotificationOutcomeUncertain(
                    "Contractor estimate SMS outcome is uncertain"
                ) from e
        else:
            # None is an intentional owner-SMS suppression, which is a
            # complete outcome. Only True confirms provider acceptance; any
            # other result must be reconciled by the strict worker.
            if strict and owner_sent is not True and owner_sent is not None:
                logger.error("Contractor estimate SMS outcome uncertain for %s", token_hash[:8])
                raise EstimateNotificationOutcomeUncertain(
                    "Contractor estimate SMS outcome is uncertain"
                )
