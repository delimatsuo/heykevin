"""Deterministic receptionist planner for next allowed action."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.services.receptionist_state import (
    AddressNeed,
    CallbackConfirmation,
    CallbackIntent,
    IntakeState,
    Intent,
    ServiceAction,
    Urgency,
)


class ActionName(str, Enum):
    ANSWER_DIRECT_QUESTION = "answer_direct_question"
    ASK_NAME = "ask_name"
    ASK_ONE_CLARIFYING_QUESTION = "ask_one_clarifying_question"
    CONFIRM_KNOWN_PROPERTY = "confirm_known_property"
    ASK_URGENCY = "ask_urgency"
    OFFER_PHOTO_LINK_AFTER_CALL = "offer_photo_link_after_call"
    OFFER_CALLBACK_OR_SCHEDULING = "offer_callback_or_scheduling"
    CONFIRM_CALLBACK_LAST_FOUR = "confirm_callback_last_four"
    ASK_CALLBACK_NUMBER = "ask_callback_number"
    TAKE_MESSAGE = "take_message"
    TRY_LIVE_OWNER_TRANSFER = "try_live_owner_transfer"
    WRAP_UP = "wrap_up"
    DECLINE_OUT_OF_SCOPE = "decline_out_of_scope"
    SAFETY_GUIDANCE = "safety_guidance"


@dataclass(frozen=True)
class NextAction:
    name: ActionName
    reason: str
    allowed_slots: tuple[str, ...] = ()
    forbidden_slots: tuple[str, ...] = ()
    memory_facts_safe_to_use: tuple[str, ...] = ()
    max_spoken_shape: str = "one or two short sentences, one question maximum"
    tool_calls_allowed: bool = False


def plan_next_action(state: IntakeState) -> NextAction:
    forbidden = _forbidden_slots(state)
    memory_facts = _safe_memory_facts(state)

    if state.urgency == Urgency.EMERGENCY or state.intent == Intent.EMERGENCY:
        return NextAction(
            name=ActionName.SAFETY_GUIDANCE,
            reason="emergency intent detected",
            allowed_slots=_allowed_slots(("safety_location",), forbidden),
            forbidden_slots=tuple(sorted(forbidden)),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="give immediate safety guidance, then ask one relevant safety question",
            tool_calls_allowed=False,
        )

    if (
        state.callback_intent == CallbackIntent.ACCEPTED
        and state.callback_confirmation == CallbackConfirmation.CONFIRMED
        and state.service_action != ServiceAction.UNKNOWN
        and state.service_object
    ):
        return NextAction(
            name=ActionName.WRAP_UP,
            reason="callback number and service request are confirmed",
            forbidden_slots=tuple(sorted(forbidden)),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="briefly confirm the callback and close without another question",
            tool_calls_allowed=False,
        )

    if (
        state.callback_intent in {CallbackIntent.REQUESTED, CallbackIntent.ACCEPTED}
        and state.callback_confirmation != CallbackConfirmation.CONFIRMED
    ):
        if state.callback_phone_last_four:
            if "callback_confirmation" in state.asked_slots:
                return _take_callback_message(
                    reason="replacement callback confirmation was requested once but remains unavailable",
                    forbidden=forbidden,
                    memory_facts=memory_facts,
                )
            return NextAction(
                name=ActionName.CONFIRM_CALLBACK_LAST_FOUR,
                reason=(
                    "replacement callback number ending "
                    f"{state.callback_phone_last_four} is available"
                ),
                allowed_slots=("callback_confirmation",),
                forbidden_slots=tuple(sorted(forbidden - {"callback_confirmation"})),
                memory_facts_safe_to_use=memory_facts,
                max_spoken_shape="confirm the replacement callback number last four in one short question",
                tool_calls_allowed=False,
            )
        if (
            state.callback_confirmation == CallbackConfirmation.REJECTED
            or not state.caller_phone_last_four
        ):
            if "callback_number" in state.asked_slots:
                return _take_callback_message(
                    reason="callback number was requested once but remains unavailable",
                    forbidden=forbidden,
                    memory_facts=memory_facts,
                )
            reason = (
                "caller rejected the caller ID callback number"
                if state.callback_confirmation == CallbackConfirmation.REJECTED
                else "callback intent exists and caller ID is missing"
            )
            return NextAction(
                name=ActionName.ASK_CALLBACK_NUMBER,
                reason=reason,
                allowed_slots=("callback_number",),
                forbidden_slots=tuple(sorted(forbidden - {"callback_number"})),
                memory_facts_safe_to_use=memory_facts,
                max_spoken_shape="ask for the best callback number in one short question",
                tool_calls_allowed=False,
            )
        if "callback_confirmation" in state.asked_slots:
            return _take_callback_message(
                reason="caller ID callback confirmation was requested once but remains unavailable",
                forbidden=forbidden,
                memory_facts=memory_facts,
            )
        return NextAction(
            name=ActionName.CONFIRM_CALLBACK_LAST_FOUR,
            reason=f"callback intent exists; caller ID ending {state.caller_phone_last_four} is available",
            allowed_slots=("callback_confirmation",),
            forbidden_slots=tuple(sorted(forbidden)),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="confirm the caller ID last four in one short question",
            tool_calls_allowed=False,
        )

    if state.address_need == AddressNeed.REQUIRED_NOW:
        return NextAction(
            name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
            reason="address is required for the current scheduling or dispatch action",
            allowed_slots=_allowed_slots(("service_address",), forbidden),
            forbidden_slots=tuple(sorted(forbidden - {"service_address"})),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="ask one concise service-address question",
            tool_calls_allowed=False,
        )

    if state.intent == Intent.PRICING_QUESTION:
        return NextAction(
            name=ActionName.ANSWER_DIRECT_QUESTION,
            reason="caller asked a direct pricing or scope question",
            allowed_slots=_allowed_slots(("job_complexity", "urgency"), forbidden)[:1],
            forbidden_slots=tuple(sorted(forbidden)),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="answer briefly, then ask one useful next question",
            tool_calls_allowed=False,
        )

    if state.service_action == ServiceAction.UNKNOWN and "service_action" not in forbidden:
        return NextAction(
            name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
            reason="service action is still unknown",
            allowed_slots=("service_action",),
            forbidden_slots=tuple(sorted(forbidden)),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="ask only whether this is repair, replacement, installation, or inspection",
            tool_calls_allowed=False,
        )

    if not state.service_object and "service_object" not in forbidden:
        return NextAction(
            name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
            reason="service object is still unknown",
            allowed_slots=("service_object",),
            forbidden_slots=tuple(sorted(forbidden)),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="ask one question about the fixture, appliance, or system",
            tool_calls_allowed=False,
        )

    return NextAction(
        name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
        reason="continue intake with one relevant detail",
        allowed_slots=_allowed_slots(("job_complexity", "urgency"), forbidden)[:1],
        forbidden_slots=tuple(sorted(forbidden)),
        memory_facts_safe_to_use=memory_facts,
        max_spoken_shape="ask one useful next question",
        tool_calls_allowed=False,
    )


def _forbidden_slots(state: IntakeState) -> set[str]:
    forbidden = set(state.asked_slots)

    if state.service_action != ServiceAction.UNKNOWN:
        forbidden.add("service_action")
    if state.service_object:
        forbidden.add("service_object")
    if state.caller_identity.name and state.caller_identity.confidence >= 0.8:
        forbidden.add("caller_name")
    if state.callback_intent in {
        CallbackIntent.NONE,
        CallbackIntent.DECLINED,
        CallbackIntent.OFFERED,
    }:
        forbidden.add("callback_number")
    if state.callback_confirmation == CallbackConfirmation.CONFIRMED:
        forbidden.add("callback_confirmation")
        forbidden.add("callback_number")
    if state.callback_phone_last_four:
        forbidden.add("callback_number")
    if state.address_need in {
        AddressNeed.NONE,
        AddressNeed.MAYBE_LATER,
        AddressNeed.ALREADY_KNOWN,
        AddressNeed.CONFIRMED,
    }:
        forbidden.add("service_address")

    return forbidden


def _allowed_slots(candidates: tuple[str, ...], forbidden: set[str]) -> tuple[str, ...]:
    return tuple(slot for slot in candidates if slot not in forbidden)


def _take_callback_message(
    *,
    reason: str,
    forbidden: set[str],
    memory_facts: tuple[str, ...],
) -> NextAction:
    return NextAction(
        name=ActionName.TAKE_MESSAGE,
        reason=reason,
        forbidden_slots=tuple(sorted(forbidden | {"callback_confirmation", "callback_number"})),
        memory_facts_safe_to_use=memory_facts,
        max_spoken_shape="briefly take a message without asking another callback question",
        tool_calls_allowed=False,
    )


def _safe_memory_facts(state: IntakeState) -> tuple[str, ...]:
    facts: list[str] = []
    if state.caller_identity.name and state.caller_identity.confidence >= 0.8:
        facts.append(f"caller_identity:{state.caller_identity.name}")
    return tuple(facts)
