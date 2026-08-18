"""Owner-tapped appointment confirmation: write Google Calendar, then mark confirmed.

This is a separate action from auto-book (`GOOGLE_CREATE_EVENT`). Confirm must
work when auto-book is off; the owner tap is the confirmation.
"""

from __future__ import annotations

import time
from typing import Any

from app.db.calls import save_call
from app.services.calendar import book_appointment
from app.services.gated_actions import ActionKey, GateContext, check_gated_action
from app.services.side_effect_audit import record_gate_decision

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


async def confirm_appointment(*, contractor: dict, call: dict, call_sid: str) -> dict:
    """Confirm a pending appointment_request onto Google Calendar."""
    request = _appointment_request(call)
    if request is None:
        raise AppointmentConfirmError(404, "Not found")

    status = str(request.get("status") or "")
    existing_event_id = str(request.get("event_id") or "")
    if status in _ALREADY_CONFIRMED_STATUSES and existing_event_id:
        return {
            "status": "already_confirmed",
            "booked": True,
            "event_id": existing_event_id,
        }

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

    start_time = str(request.get("start_time") or "")
    if not start_time:
        raise AppointmentConfirmError(502, "Appointment is missing a start time")

    event_id = await book_appointment(
        contractor,
        str(request.get("title") or ""),
        start_time,
        str(request.get("end_time") or ""),
        str(request.get("description") or ""),
        call_sid=call_sid,
    )
    if not event_id:
        raise AppointmentConfirmError(502, "Failed to create calendar event")

    confirmed = dict(request)
    confirmed["status"] = "confirmed"
    confirmed["event_id"] = event_id
    confirmed["confirmed_at"] = int(time.time())
    await save_call(call_sid, {"appointment_request": confirmed})

    return {
        "status": "already_confirmed" if status in _ALREADY_CONFIRMED_STATUSES else "confirmed",
        "booked": True,
        "event_id": event_id,
    }
