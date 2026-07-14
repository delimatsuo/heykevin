"""Dialogue planner policy for receptionist next actions."""

from app.services.dialogue_planner import ActionName, plan_next_action
from app.services.receptionist_state import (
    AddressNeed,
    CallerObservation,
    CallbackConfirmation,
    CallbackIntent,
    IntakeState,
    Intent,
    ServiceAction,
)


def test_planner_blocks_duplicate_service_action_and_object_questions():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan",
        caller_source="customer_memory",
        caller_confidence=0.92,
    )
    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.PRICING_QUESTION,
            service_object="toilet",
            service_action=ServiceAction.REPLACE,
        )
    )

    action = plan_next_action(state)

    assert action.name == ActionName.ANSWER_DIRECT_QUESTION
    assert "service_action" in action.forbidden_slots
    assert "service_object" in action.forbidden_slots
    assert "callback_number" in action.forbidden_slots
    assert "service_address" in action.forbidden_slots
    assert action.allowed_slots == ("job_complexity",)
    assert action.max_spoken_shape == "answer briefly, then ask one useful next question"
    assert action.tool_calls_allowed is False


def test_planner_forbids_slots_already_asked_even_when_unknown():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.intent = Intent.SERVICE_REQUEST
    state.service_action = ServiceAction.UNKNOWN
    state.mark_slot_asked("service_action")

    action = plan_next_action(state)

    assert "service_action" in action.forbidden_slots
    assert "service_action" not in action.allowed_slots


def test_planner_confirms_callback_last_four_only_after_callback_intent():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.CALLBACK,
            callback_intent=CallbackIntent.REQUESTED,
        )
    )

    action = plan_next_action(state)

    assert action.name == ActionName.CONFIRM_CALLBACK_LAST_FOUR
    assert action.allowed_slots == ("callback_confirmation",)
    assert "callback_number" not in action.forbidden_slots
    assert "service_address" in action.forbidden_slots
    assert "8667" in action.reason


def test_planner_allows_callback_number_after_intent_when_caller_id_missing():
    state = IntakeState.new(call_sid="CA_test")
    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.CALLBACK,
            callback_intent=CallbackIntent.REQUESTED,
        )
    )

    action = plan_next_action(state)

    assert action.name == ActionName.ASK_CALLBACK_NUMBER
    assert action.allowed_slots == ("callback_number",)
    assert "callback_number" not in action.forbidden_slots


def test_planner_asks_callback_number_when_caller_rejects_caller_id():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.callback_intent = CallbackIntent.REQUESTED
    state.callback_confirmation = CallbackConfirmation.REJECTED

    action = plan_next_action(state)

    assert action.name == ActionName.ASK_CALLBACK_NUMBER
    assert action.allowed_slots == ("callback_number",)
    assert "callback_number" not in action.forbidden_slots


def test_planner_confirms_replacement_callback_last_four_without_reasking_number():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.intent = Intent.CALLBACK
    state.service_object = "faucet"
    state.service_action = ServiceAction.REPAIR
    state.callback_intent = CallbackIntent.REQUESTED
    state.callback_confirmation = CallbackConfirmation.REJECTED
    state.mark_slot_asked("callback_number")
    state.apply_caller_observation(CallerObservation(callback_phone_last_four="4321"))

    action = plan_next_action(state)

    assert action.name == ActionName.CONFIRM_CALLBACK_LAST_FOUR
    assert action.allowed_slots == ("callback_confirmation",)
    assert "callback_number" in action.forbidden_slots
    assert "4321" in action.reason

    state.apply_caller_observation(
        CallerObservation(
            callback_intent=CallbackIntent.ACCEPTED,
            callback_confirmation=CallbackConfirmation.CONFIRMED,
        )
    )

    assert plan_next_action(state).name == ActionName.WRAP_UP


def test_planner_does_not_repeat_callback_number_question_without_new_number():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.callback_intent = CallbackIntent.REQUESTED
    state.callback_confirmation = CallbackConfirmation.REJECTED
    state.mark_slot_asked("callback_number")

    action = plan_next_action(state)

    assert action.name == ActionName.TAKE_MESSAGE
    assert action.allowed_slots == ()
    assert "callback_number" in action.forbidden_slots


def test_planner_does_not_repeat_callback_number_when_caller_id_is_missing():
    state = IntakeState.new(call_sid="CA_test")
    state.callback_intent = CallbackIntent.REQUESTED
    state.mark_slot_asked("callback_number")

    action = plan_next_action(state)

    assert action.name == ActionName.TAKE_MESSAGE
    assert action.allowed_slots == ()
    assert "callback_number" in action.forbidden_slots


def test_planner_does_not_repeat_unanswered_callback_confirmation():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.callback_intent = CallbackIntent.REQUESTED
    state.mark_slot_asked("callback_confirmation")

    action = plan_next_action(state)

    assert action.name == ActionName.TAKE_MESSAGE
    assert action.allowed_slots == ()
    assert "callback_confirmation" in action.forbidden_slots


def test_planner_allows_address_only_when_state_requires_it():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.intent = Intent.SCHEDULING
    state.address_need = AddressNeed.REQUIRED_NOW

    action = plan_next_action(state)

    assert action.name == ActionName.ASK_ONE_CLARIFYING_QUESTION
    assert action.allowed_slots == ("service_address",)
    assert "service_address" not in action.forbidden_slots


def test_planner_confirms_known_memory_instead_of_reasking_name():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan Caller",
        caller_source="customer_memory",
        caller_confidence=0.94,
        memory_refs_used=("scoped-memory-ref-1",),
    )
    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.SERVICE_REQUEST,
            service_object="faucet",
            service_action=ServiceAction.REPAIR,
        )
    )

    action = plan_next_action(state)

    assert "caller_name" in action.forbidden_slots
    assert "caller_name" not in action.allowed_slots
    assert "caller_identity:Jonathan Caller" in action.memory_facts_safe_to_use


def test_planner_does_not_repeat_callback_confirmation_after_acceptance():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.intent = Intent.CALLBACK
    state.service_object = "faucet"
    state.service_action = ServiceAction.REPAIR
    state.callback_intent = CallbackIntent.ACCEPTED
    state.callback_confirmation = CallbackConfirmation.CONFIRMED

    action = plan_next_action(state)

    assert action.name == ActionName.WRAP_UP
    assert "callback_confirmation" not in action.allowed_slots
    assert "callback_number" in action.forbidden_slots
