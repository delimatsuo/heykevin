"""Owner-tapped appointment confirmation: write Google Calendar, then mark confirmed.

This is a separate action from auto-book (`GOOGLE_CREATE_EVENT`). Confirm must
work when auto-book is off; the owner tap is the confirmation. After the
calendar write succeeds, the caller may get an informational SMS.
"""

from __future__ import annotations

import time
from typing import Any

from app.db.calls import save_call
from app.services.appointment_time import (
    format_wall_clock,
    localize_spoken_slot,
    slot_is_plausible,
)
from app.services.calendar import book_appointment
from app.services.gated_actions import ActionKey, GateContext, check_gated_action
from app.services.side_effect_audit import record_gate_decision
from app.services.sms import send_sms
from app.utils.phone import normalize_phone

_ALREADY_CONFIRMED_STATUSES = frozenset({"confirmed", "booked"})


class AppointmentConfirmError(Exception):
    """Mapped to HTTP by the calls API."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _appointment_request(call: dict[str, Any] | None) -> dict[str, Any] | None:
    if not call:
        return None
    request = call.get("appointment_request")
    return request if isinstance(request, dict) and request else None


def _same_phone(left: str, right: str) -> bool:
    normalized_left = normalize_phone(left)
    normalized_right = normalize_phone(right)
    if normalized_left and normalized_right:
        return normalized_left == normalized_right
    return bool(left) and bool(right) and left.strip() == right.strip()


def _caller_confirmation_body(contractor: dict, request: dict) -> str:
    owner_name = str(contractor.get("owner_name") or "").strip()
    business = str(contractor.get("business_name") or "").strip()
    if not business:
        business = f"{owner_name}'s office" if owner_name else "the business"
    when = format_wall_clock(str(request.get("start_time") or ""), contractor)
    contact = str(
        contractor.get("twilio_number") or contractor.get("owner_phone") or ""
    ).strip()
    lines = [f"Your appointment with {business} is confirmed for {when}."]
    if contact:
        lines.append(f"If this time no longer works, call {contact}.")
    # CTIA/carrier requirement: every caller-facing message carries an opt-out.
    # The caller never signed up with Hey Kevin — they phoned a business — so
    # the way out has to travel with the message itself. Twilio's Messaging
    # Service handles the STOP keyword; this line is the disclosure.
    lines.append("Reply STOP to opt out.")
    return "\n".join(lines)


async def _notify_caller(
    *,
    contractor: dict,
    call: dict,
    request: dict,
    call_sid: str,
) -> bool:
    caller_phone = str(call.get("caller_phone") or "").strip()
    owner_phone = str(contractor.get("owner_phone") or "").strip()
    if not caller_phone or _same_phone(caller_phone, owner_phone):
        return False

    context = GateContext(
        source="ios",
        actor="owner",
        owner_confirmed=True,
        idempotency_key=f"{call_sid}:caller_sms",
    )
    decision = check_gated_action(
        contractor, ActionKey.APPOINTMENT_CONFIRMED_CALLER_SMS, context
    )
    record_gate_decision(
        action=ActionKey.APPOINTMENT_CONFIRMED_CALLER_SMS,
        contractor_id=str((contractor or {}).get("contractor_id") or ""),
        source=context.source,
        resource_id=context.idempotency_key,
        decision=decision,
    )
    if not decision.allowed:
        return False
    return await send_sms(
        caller_phone,
        _caller_confirmation_body(contractor, request),
        from_number=str(contractor.get("twilio_number") or ""),
        contractor=contractor,
        action=ActionKey.APPOINTMENT_CONFIRMED_CALLER_SMS,
        gate_context=context,
    )


def _confirmed_payload(*, status: str, event_id: str, caller_notified: bool) -> dict:
    return {
        "status": status,
        "booked": True,
        "event_id": event_id,
        "caller_notified": caller_notified,
    }


async def confirm_appointment(*, contractor: dict, call: dict, call_sid: str) -> dict:
    """Confirm a pending appointment_request onto Google Calendar."""
    request = _appointment_request(call)
    if request is None:
        raise AppointmentConfirmError(404, "Not found")

    status = str(request.get("status") or "")
    existing_event_id = str(request.get("event_id") or "")
    if status in _ALREADY_CONFIRMED_STATUSES and existing_event_id:
        already_notified = bool(request.get("caller_notified_at"))
        if already_notified:
            return _confirmed_payload(
                status="already_confirmed",
                event_id=existing_event_id,
                caller_notified=True,
            )
        notified = await _notify_caller(
            contractor=contractor,
            call=call,
            request=request,
            call_sid=call_sid,
        )
        if notified:
            repaired = dict(request)
            repaired["caller_notified_at"] = int(time.time())
            await save_call(call_sid, {"appointment_request": repaired})
        return _confirmed_payload(
            status="already_confirmed",
            event_id=existing_event_id,
            caller_notified=notified,
        )

    context = GateContext(
        source="ios",
        actor="owner",
        owner_confirmed=True,
        idempotency_key=call_sid,
    )
    decision = check_gated_action(
        contractor, ActionKey.OWNER_CONFIRM_CALENDAR_EVENT, context
    )
    record_gate_decision(
        action=ActionKey.OWNER_CONFIRM_CALENDAR_EVENT,
        contractor_id=str((contractor or {}).get("contractor_id") or ""),
        source=context.source,
        resource_id=call_sid,
        decision=decision,
    )
    if not decision.allowed:
        raise AppointmentConfirmError(403, decision.message)

    start_time = localize_spoken_slot(str(request.get("start_time") or ""), contractor)
    if not start_time:
        raise AppointmentConfirmError(502, "Appointment is missing a start time")
    if not slot_is_plausible(start_time, contractor):
        raise AppointmentConfirmError(422, "Appointment time is not bookable")
    end_time = localize_spoken_slot(str(request.get("end_time") or ""), contractor)

    event_id = await book_appointment(
        contractor,
        str(request.get("title") or ""),
        start_time,
        end_time,
        str(request.get("description") or ""),
        call_sid=call_sid,
    )
    if not event_id:
        raise AppointmentConfirmError(502, "Failed to create calendar event")

    confirmed = dict(request)
    confirmed["status"] = "confirmed"
    confirmed["event_id"] = event_id
    confirmed["confirmed_at"] = int(time.time())
    confirmed["start_time"] = start_time
    if end_time:
        confirmed["end_time"] = end_time

    notified = await _notify_caller(
        contractor=contractor,
        call=call,
        request=confirmed,
        call_sid=call_sid,
    )
    if notified:
        confirmed["caller_notified_at"] = int(time.time())
    await save_call(call_sid, {"appointment_request": confirmed})

    return _confirmed_payload(
        status="already_confirmed" if status in _ALREADY_CONFIRMED_STATUSES else "confirmed",
        event_id=event_id,
        caller_notified=notified,
    )
