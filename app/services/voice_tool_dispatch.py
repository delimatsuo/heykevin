"""Shared execution of tenant-bound voice tools, independent of model transport."""

import asyncio
import base64
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import settings
from app.services.gated_actions import ActionKey, GateContext, GateReason, check_gated_action
from app.services.receptionist_tools import RECEPTIONIST_TOOL_NAMES, ReceptionistToolExecutor
from app.services.side_effect_audit import record_gate_decision
from app.services.voice_events import _log_tool_execution_failure, _log_voice_event, _tool_label

# Calendar reads may refresh an expired token and retry. Managed writes need
# time for durable prepare (5s), provider confirmation (8s), and finalize (5s).
GOOGLE_CALENDAR_TOOL_TIMEOUT_SECONDS = 20.0


def provider_mutation_infrastructure_ready() -> bool:
    """Return the server-owned readiness gate for durable provider writes."""

    return settings.service_request_recovery_enabled is True


def _managed_booking_identity(
    contractor_id: str,
    caller_phone: str,
    title: object,
    start_time: object,
    end_time: object,
) -> tuple[str, str, str, str, str]:
    """Return stable request/provider IDs plus canonical booking fields.

    Identity is product semantic state, never a call SID or model tool-call ID,
    so a crash or later-call retry resolves to the same durable intent.
    """
    from app.services.service_request_repository import customer_key_for_phone

    if not isinstance(contractor_id, str) or not contractor_id.strip():
        raise ValueError("contractor_id is required")
    if not isinstance(title, str):
        raise ValueError("booking title is invalid")
    normalized_title = " ".join(
        re.sub(
            r"[\x00-\x1f\x7f]+",
            " ",
            unicodedata.normalize("NFKC", title),
        ).split()
    )
    if not normalized_title or len(normalized_title) > 200:
        raise ValueError("booking title is invalid")
    if (
        not isinstance(start_time, str)
        or not isinstance(end_time, str)
        or len(start_time) > 80
        or len(end_time) > 80
    ):
        raise ValueError("booking schedule is invalid")
    start = datetime.fromisoformat(start_time)
    end = datetime.fromisoformat(end_time)
    if (
        start.tzinfo is None
        or start.utcoffset() is None
        or end.tzinfo is None
        or end.utcoffset() is None
        or end <= start
    ):
        raise ValueError("booking schedule is invalid")
    canonical_start = start.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    canonical_end = end.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    material = json.dumps(
        {
            "contractor_id": contractor_id.strip(),
            "customer_key": customer_key_for_phone(caller_phone),
            "end": canonical_end,
            "start": canonical_start,
            "title": normalized_title,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    request_id = f"sr_{digest.hex()[:32]}"
    event_id = base64.b32hexencode(digest).decode("ascii").lower().rstrip("=")[:32]
    return request_id, event_id, normalized_title, canonical_start, canonical_end


# Gate denials that mean "this owner has not turned on automatic booking"
# rather than "something went wrong". These become appointment requests the
# owner confirms; every other denial stays an error.
_BOOKING_REQUEST_REASONS = frozenset({
    GateReason.FEATURE_DISABLED,
    GateReason.COMPLIANCE_NOT_APPROVED,
    GateReason.OWNER_CONFIRMATION_REQUIRED,
})


def _tool_execution_error_response() -> str:
    """Tool-failure payload that tells the model how to recover in conversation.

    A bare {"success": false, "error": "..."} leaves the model with nothing to
    say: it cannot tell whether to retry, apologise, or move on, and the caller
    hears dead air. That is exactly what happened on call CA54f11e — an expired
    Jobber refresh token failed `check_customer` in 200ms and the caller sat in
    silence for 54 seconds until the pipeline gave up.

    The instruction field is the fix. The caller must never wait on a backend
    integration: Kevin apologises briefly, gathers the same details by voice, and
    promises a callback, which is what a receptionist would do if their computer
    was down.
    """
    return json.dumps({
        "success": False,
        "error": "unavailable",
        "instruction": (
            "This system is temporarily unavailable. Do not retry it and do not "
            "mention the technical problem. Continue the conversation normally: "
            "briefly apologise for not being able to confirm right now, ask the "
            "caller for the details you still need (such as their preferred day "
            "and time), and tell them someone will call back shortly to confirm."
        ),
    })


def _tool_timeout_response() -> str:
    """Same recovery contract as above, for a tool that ran too long."""
    return json.dumps({
        "success": False,
        "error": "timed_out",
        "instruction": (
            "This request took too long and was cancelled. Do not retry it. "
            "Continue the conversation normally: take the caller's details by "
            "voice and tell them someone will call back shortly to confirm."
        ),
    })


@dataclass(frozen=True, kw_only=True, repr=False)
class VoiceToolContext:
    """Server-bound call identity and the engine's mutable, refreshable config."""

    contractor_config: dict
    call_sid: str
    caller_phone: str


class VoiceToolDispatcher:
    """Bind a request executor lazily. The engine chooses the dispatch lifetime."""

    def __init__(
        self,
        context: VoiceToolContext,
        *,
        receptionist_executor: ReceptionistToolExecutor | None = None,
    ) -> None:
        self.context = context
        self.receptionist_executor = receptionist_executor

    def has_service_request_context(self) -> bool:
        context = self.context.contractor_config.get("service_request_context")
        return (
            isinstance(context, dict)
            and bool(context.get("customer_key"))
            and bool(context.get("open_service_requests"))
            and self._managed_provider_create_enabled()
        )

    def _managed_provider_create_enabled(self) -> bool:
        """Fail closed unless this tenant is enrolled in durable provider writes."""
        from app.services.entitlements import has_entitled_token

        return (
            provider_mutation_infrastructure_ready()
            and self.context.contractor_config.get("service_request_mutations_enabled") is True
            and self.context.contractor_config.get("integration_write_status") == "approved"
            and has_entitled_token(self.context.contractor_config, "google_calendar", "access")
        )

    def _service_request_executor(self) -> ReceptionistToolExecutor:
        if self.receptionist_executor is not None:
            return self.receptionist_executor
        service = self.context.contractor_config.get("service_request_command_service")
        if service is None:
            raise RuntimeError("durable service-request command service is unavailable")
        executor = ReceptionistToolExecutor(
            service,
            contractor_id=self.context.contractor_config.get("contractor_id", ""),
            caller_phone=self.context.caller_phone,
        )
        self.receptionist_executor = executor
        return executor

    async def _create_managed_google_booking(
        self,
        *,
        tool_input: dict,
        operation_id: str,
    ) -> dict:
        """Run booking through the shared, durable provider-create saga."""
        if not self._managed_provider_create_enabled():
            return {
                "success": False,
                "error": "Managed appointment creation is not enabled for this account.",
            }
        contractor_id = str(self.context.contractor_config.get("contractor_id") or "")
        caller_phone = str(self.context.caller_phone or "")
        command_service = self.context.contractor_config.get("service_request_command_service")
        if not contractor_id or not caller_phone or command_service is None:
            return {
                "success": False,
                "error": "I couldn't safely prepare and confirm that appointment.",
            }

        try:
            from app.services.service_request_repository import (
                ProviderBinding,
                ServiceRequestCommandStatus,
            )

            request_id, event_id, title, start_time, end_time = _managed_booking_identity(
                contractor_id,
                caller_phone,
                tool_input.get("title", "Appointment"),
                tool_input.get("start_time", ""),
                tool_input.get("end_time", ""),
            )
            description = tool_input.get("description", "")
            if not isinstance(description, str):
                raise ValueError("booking description is invalid")
            transport_key = hashlib.sha256(
                "\x00".join(
                    [
                        "provider-create-transport-v1",
                        str(self.context.call_sid),
                        operation_id,
                        request_id,
                    ]
                ).encode("utf-8")
            ).hexdigest()
            created = await command_service.create_provider_service_request(
                contractor_id=contractor_id,
                caller_phone=caller_phone,
                request_id=request_id,
                services=[title],
                scheduled_start=start_time,
                scheduled_end=end_time,
                expected_revision=0,
                idempotency_key=transport_key,
                binding=ProviderBinding(
                    kind="google_calendar",
                    resource_id=event_id,
                ),
                title=title,
                description=description,
            )
            if created.status is ServiceRequestCommandStatus.APPLIED:
                return {
                    "success": True,
                    "revision": created.revision,
                }
            if created.status is ServiceRequestCommandStatus.PENDING_PROVIDER:
                return {
                    "success": False,
                    "pending_provider": True,
                    "error": (
                        "The appointment could not be fully confirmed yet. Do not claim "
                        "it is booked; ask the business to follow up."
                    ),
                }
            return {
                "success": False,
                "error": "The appointment could not be safely created or confirmed.",
            }
        except Exception as error:  # noqa: BLE001 - do not expose provider/customer data
            _log_tool_execution_failure(
                "create_managed_google_booking",
                self.context.call_sid,
                error,
            )
            return {
                "success": False,
                "error": "The appointment could not be safely created or confirmed.",
            }

    def has_jobber(self) -> bool:
        """Check if the contractor has Jobber connected."""
        from app.services.entitlements import has_entitled_token

        return has_entitled_token(self.context.contractor_config, "jobber")

    def has_google_calendar(self) -> bool:
        """Check if the contractor has Google Calendar connected (fallback)."""
        from app.services.entitlements import has_entitled_token

        return has_entitled_token(self.context.contractor_config, "google_calendar")

    def _get_jobber_token(self) -> str:
        from app.services.integration_tokens import resolve_usable_token

        return resolve_usable_token(self.context.contractor_config, "jobber") or ""

    def _get_google_calendar_token(self) -> str:
        from app.services.integration_tokens import resolve_usable_token

        return resolve_usable_token(self.context.contractor_config, "google_calendar") or ""

    def _check_tool_write_gate(self, action: ActionKey):
        call_sid = self.context.call_sid
        context = GateContext(
            source="voice_tool",
            actor="automation",
            idempotency_key=f"{call_sid}:{action.value}",
        )
        decision = check_gated_action(self.context.contractor_config, action, context)
        record_gate_decision(
            action=action,
            contractor_id=self.context.contractor_config.get("contractor_id", ""),
            source="voice_tool",
            resource_id=call_sid,
            decision=decision,
        )
        return decision

    def _reject_implausible_slot(self, tool_input: dict) -> str | None:
        """Refuse a start_time that cannot be a real appointment.

        Returns a tool result to send back, or None when the slot is fine.

        The prompt now states today's date, but a stated date is guidance, not
        a guarantee. `slot_is_plausible` already guarded the owner's Confirm
        tap — except that fires at the *end*, after the wrong date has been
        spoken aloud to the caller and texted to the owner. On 2026-08-19 a
        caller asking for "Friday, August 21st" heard Kevin accept it and the
        owner got an SMS for it, and only Confirm caught the stored year 2020.

        Checking here means a bad date never leaves the call. The model is told
        explicitly not to retry the same value — a bare error reads as a
        transient fault, which is how call CA9c4f4d ended up retrying four
        times and improvising (PR #156).
        """
        from app.services.appointment_time import slot_is_bookable

        contractor = self.context.contractor_config
        start_time = str(tool_input.get("start_time", "") or "")
        if slot_is_bookable(start_time, contractor):
            return None

        _log_voice_event(
            "appointment_slot_rejected",
            self.context.call_sid,
            level=logging.WARNING,
        )
        # No "error" key on purpose. _handle_caller_speech treats any tool
        # result carrying one as a hard failure: it speaks "I can't check the
        # schedule right now", takes a message, and returns without giving the
        # model another turn — so the instruction below would never be read on
        # the legacy pipeline. This mirrors the request_recorded shape, which
        # is a denial the model is meant to act on rather than a fault.
        return json.dumps({
            "status": "date_not_understood",
            "booked": False,
            "message": (
                "Nothing was recorded: that date did not resolve to a usable "
                "appointment time. Do not call book_appointment again with the "
                "same value. Ask the caller to say the day and month again, "
                "resolve it against today's date by choosing the next occurrence "
                "that has not already passed — near year-end that may be next "
                "year — then call book_appointment once with the corrected time."
            ),
        })

    async def _record_appointment_request(self, tool_input: dict) -> str:
        """Pass a requested slot to the owner instead of booking it.

        Automated calendar writes stay off unless the owner turns them on, so
        a denied booking is the ordinary path here, not a failure. Answering
        with a bare error made the model treat it as a transient fault: on
        call CA9c4f4d it retried four times and then improvised a reassurance
        the caller heard as a held slot. The request is written to the call
        record so the post-call SMS can lead with the time the caller asked
        for.
        """
        from app.services.appointment_time import localize_spoken_slot

        contractor = self.context.contractor_config
        request = {
            "title": tool_input.get("title", ""),
            "start_time": localize_spoken_slot(str(tool_input.get("start_time", "")), contractor),
            "end_time": localize_spoken_slot(str(tool_input.get("end_time", "")), contractor),
            "description": tool_input.get("description", ""),
            "status": "pending_owner_confirmation",
        }
        call_sid = self.context.call_sid
        if call_sid:
            try:
                from app.db.calls import save_call

                saved = await save_call(call_sid, {"appointment_request": request})
            except Exception as error:
                saved = False
                _log_tool_execution_failure("record_appointment_request", call_sid, error)
            if not saved:
                # Not fatal: the owner still gets the call SMS and transcript,
                # so only the machine-readable time is lost.
                _log_voice_event(
                    "appointment_request_persist_failed",
                    call_sid,
                    level=logging.WARNING,
                )
        return json.dumps({
            "status": "request_recorded",
            "booked": False,
            "requested_start": request["start_time"],
            "message": (
                "Not booked. This account does not book automatically, so the time was "
                "passed to the owner as a request. Tell the caller you have sent the "
                "request and the owner will confirm the appointment with them directly, "
                "then ask if there is anything else and close warmly — do not end on the "
                "bare statement. Do not say it is booked, reserved, or held. Do not call "
                "book_appointment again on this call."
            ),
        })

    async def execute(
        self,
        tool_name: str,
        tool_input: dict,
        *,
        operation_id: str = "",
    ) -> str:
        """Return a JSON result for a request mutation, calendar tool, or customer lookup."""

        if tool_name in RECEPTIONIST_TOOL_NAMES:
            if not self.has_service_request_context():
                return json.dumps({
                    "status": "failed",
                    "success": False,
                    "confirmed": False,
                    "message": "That request change is not enabled for this account.",
                })
            try:
                result = await self._service_request_executor().execute(
                    tool_name,
                    tool_input,
                    operation_id=(
                        f"{self.context.call_sid}:{operation_id}"
                        if operation_id
                        else ""
                    ),
                )
            except Exception as error:
                _log_tool_execution_failure(
                    tool_name,
                    self.context.call_sid,
                    error,
                )
                result = {
                    "status": "failed",
                    "success": False,
                    "confirmed": False,
                    "message": "I couldn't complete or confirm that change.",
                }
            return json.dumps(result)

        # --- Google Calendar tools ---
        if self.has_google_calendar() and not self.has_jobber():
            from app.services.calendar import (
                GoogleCalendarUnavailableError,
            )
            from app.services.calendar import (
                get_available_slots as gcal_slots,
            )

            token = self._get_google_calendar_token()
            if not token:
                return json.dumps({"error": "Google Calendar is not connected."})

            try:
                if tool_name == "check_availability":
                    days = min(tool_input.get("days_ahead", 7), 14)
                    try:
                        slots = await asyncio.wait_for(
                            gcal_slots(self.context.contractor_config, days),
                            timeout=GOOGLE_CALENDAR_TOOL_TIMEOUT_SECONDS,
                        )
                    except GoogleCalendarUnavailableError:
                        return json.dumps({"error": "Calendar availability is temporarily unavailable."})
                    return json.dumps({"available_slots": slots, "days_checked": days})

                elif tool_name == "book_appointment":
                    # Before the gate, so it covers both outcomes: the request
                    # path that production actually uses today, and auto-book
                    # if it is ever turned on.
                    rejection = self._reject_implausible_slot(tool_input)
                    if rejection is not None:
                        return rejection
                    decision = self._check_tool_write_gate(ActionKey.GOOGLE_CREATE_EVENT)
                    if not decision.allowed:
                        if decision.reason in _BOOKING_REQUEST_REASONS:
                            return await self._record_appointment_request(tool_input)
                        return json.dumps({"success": False, "error": decision.message})
                    if not self._managed_provider_create_enabled():
                        return await self._record_appointment_request(tool_input)

                    return json.dumps(await asyncio.wait_for(
                        self._create_managed_google_booking(
                            tool_input=tool_input,
                            operation_id=operation_id,
                        ),
                        timeout=GOOGLE_CALENDAR_TOOL_TIMEOUT_SECONDS,
                    ))

                else:
                    return json.dumps({"error": f"Unknown tool: {tool_name}"})

            except asyncio.TimeoutError:
                _log_voice_event(
                    "tool_timeout",
                    self.context.call_sid,
                    level=logging.WARNING,
                    tool=_tool_label(tool_name),
                )
                return json.dumps({"error": "Request timed out"})
            except Exception as e:
                _log_tool_execution_failure(tool_name, self.context.call_sid, e)
                return _tool_execution_error_response()

        # --- Jobber tools ---
        from app.services.jobber import lookup_customer

        if not self._get_jobber_token():
            return json.dumps({"error": "No scheduling integration connected."})

        try:
            if tool_name == "check_customer":
                customer = await asyncio.wait_for(
                    lookup_customer(self.context.contractor_config, tool_input.get("phone", "")),
                    timeout=3.0,
                )
                if customer:
                    return json.dumps({
                        "found": True,
                        "name": customer.get("name", ""),
                        "id": customer.get("id", ""),
                        "address": customer.get("billingAddress", {}),
                    })
                return json.dumps({"found": False})

            else:
                return json.dumps({"error": f"Unknown tool: {tool_name}"})

        except asyncio.TimeoutError:
            _log_voice_event(
                "tool_timeout",
                self.context.call_sid,
                level=logging.WARNING,
                tool=_tool_label(tool_name),
            )
            return _tool_timeout_response()
        except Exception as e:
            _log_tool_execution_failure(tool_name, self.context.call_sid, e)
            return _tool_execution_error_response()
