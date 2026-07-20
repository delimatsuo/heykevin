"""Dialogue planner policy for receptionist next actions."""

import pytest

from app.services.dialogue_planner import ActionName, NextAction, plan_next_action
from app.services.receptionist_state import (
    ASKABLE_SLOTS,
    AddressNeed,
    BusinessScope,
    CallerObservation,
    CallbackConfirmation,
    CallbackIntent,
    IntakeState,
    Intent,
    ServiceAction,
    Urgency,
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


def test_out_of_scope_declined_followup_wraps_without_trade_intake():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_name="Fixture Caller",
        caller_confidence=1.0,
    )
    state.business_scope = BusinessScope.OUT_OF_SCOPE
    state.callback_intent = CallbackIntent.DECLINED

    action = plan_next_action(state)

    assert action.name == ActionName.WRAP_UP
    assert action.allowed_slots == ()


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


def test_planner_offers_followup_after_supported_intake_slots_are_exhausted():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_name="Fixture Caller",
        caller_confidence=1.0,
    )
    state.intent = Intent.SERVICE_REQUEST
    state.service_object = "faucet"
    state.service_action = ServiceAction.REPAIR
    state.mark_slot_asked("job_complexity")
    state.mark_slot_asked("urgency")

    action = plan_next_action(state)

    assert action.name == ActionName.OFFER_CALLBACK_OR_SCHEDULING
    assert action.allowed_slots == ("callback_preference",)


def test_controlled_live_policy_collects_name_before_optional_intake_details():
    state = IntakeState.new(call_sid="CA_test")
    state.intent = Intent.SERVICE_REQUEST
    state.service_object = "faucet"
    state.service_action = ServiceAction.REPAIR

    action = plan_next_action(state, require_caller_name=True)

    assert action.name == ActionName.ASK_NAME
    assert action.allowed_slots == ("caller_name",)
    assert action.question_required is True
    assert set(action.allowed_slots).isdisjoint(action.forbidden_slots)


def test_planner_wraps_up_after_followup_offer_is_exhausted():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_name="Fixture Caller",
        caller_confidence=1.0,
    )
    state.intent = Intent.SERVICE_REQUEST
    state.service_object = "faucet"
    state.service_action = ServiceAction.REPAIR
    state.mark_slot_asked("job_complexity")
    state.mark_slot_asked("urgency")
    state.mark_slot_asked("callback_preference")

    action = plan_next_action(state)

    assert action.name == ActionName.WRAP_UP
    assert action.allowed_slots == ()
    assert action.question_required is False
    assert "another question" in action.max_spoken_shape


def test_planner_answers_pricing_without_question_when_intake_is_exhausted():
    state = IntakeState.new(call_sid="CA_test")
    state.intent = Intent.PRICING_QUESTION
    state.service_object = "faucet"
    state.service_action = ServiceAction.REPLACE
    state.mark_slot_asked("job_complexity")
    state.mark_slot_asked("urgency")

    action = plan_next_action(state)

    assert action.name == ActionName.ANSWER_DIRECT_QUESTION
    assert action.allowed_slots == ()
    assert action.question_required is False
    assert "without another question" in action.max_spoken_shape


def test_planner_does_not_ask_for_known_urgency():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_name="Fixture Caller",
        caller_confidence=1.0,
    )
    state.intent = Intent.SERVICE_REQUEST
    state.service_object = "faucet"
    state.service_action = ServiceAction.REPAIR
    state.urgency = Urgency.ROUTINE
    state.mark_slot_asked("job_complexity")

    action = plan_next_action(state)

    assert action.name == ActionName.OFFER_CALLBACK_OR_SCHEDULING
    assert action.allowed_slots == ("callback_preference",)
    assert "urgency" in action.forbidden_slots


def test_planner_does_not_ask_for_known_job_complexity_fact():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_name="Fixture Caller",
        caller_confidence=1.0,
    )
    state.intent = Intent.SERVICE_REQUEST
    state.service_object = "faucet"
    state.service_action = ServiceAction.REPAIR
    state.urgency = Urgency.ROUTINE
    state.known_facts = ["job_complexity:existing fixture already removed"]

    action = plan_next_action(state)

    assert action.name == ActionName.OFFER_CALLBACK_OR_SCHEDULING
    assert action.allowed_slots == ("callback_preference",)
    assert "job_complexity" in action.forbidden_slots
    assert "urgency" in action.forbidden_slots


def test_planner_does_not_reask_known_callback_slots():
    number_state = IntakeState.new(call_sid="CA_test")
    number_state.intent = Intent.CALLBACK
    number_state.callback_intent = CallbackIntent.REQUESTED
    number_state.known_facts = ["callback_number:provided"]

    number_action = plan_next_action(number_state)

    assert number_action.name == ActionName.TAKE_MESSAGE
    assert number_action.allowed_slots == ()
    assert "callback_number" in number_action.forbidden_slots

    confirmation_state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
    )
    confirmation_state.intent = Intent.CALLBACK
    confirmation_state.callback_intent = CallbackIntent.REQUESTED
    confirmation_state.known_facts = ["callback_confirmation:recorded"]

    confirmation_action = plan_next_action(confirmation_state)

    assert confirmation_action.name == ActionName.TAKE_MESSAGE
    assert confirmation_action.allowed_slots == ()
    assert "callback_confirmation" in confirmation_action.forbidden_slots


@pytest.mark.parametrize(
    "known_fact",
    [
        "callback_preference:declined",
        "callback_intent:declined",
    ],
)
def test_planner_does_not_reoffer_known_callback_preference(known_fact: str):
    state = IntakeState.new(
        call_sid="CA_test",
        caller_name="Fixture Caller",
        caller_confidence=1.0,
    )
    state.intent = Intent.SERVICE_REQUEST
    state.service_object = "faucet"
    state.service_action = ServiceAction.REPAIR
    state.urgency = Urgency.ROUTINE
    state.mark_slot_asked("job_complexity")
    state.known_facts = [known_fact]

    action = plan_next_action(state)

    assert action.name == ActionName.WRAP_UP
    assert action.allowed_slots == ()
    assert "callback_preference" in action.forbidden_slots


@pytest.mark.parametrize("known_slot", sorted(ASKABLE_SLOTS))
def test_planner_never_allows_a_canonical_known_slot(known_slot: str):
    state = IntakeState.new(call_sid="CA_test")
    state.intent = Intent.SERVICE_REQUEST
    state.service_object = "faucet"
    state.service_action = ServiceAction.REPAIR
    state.urgency = Urgency.ROUTINE
    state.mark_slot_asked("job_complexity")
    state.known_facts = [f"{known_slot}:known"]

    action = plan_next_action(state)

    assert known_slot in action.forbidden_slots
    assert known_slot not in action.allowed_slots
    assert set(action.allowed_slots).isdisjoint(action.forbidden_slots)


def test_planner_does_not_repeat_required_address_question():
    state = IntakeState.new(call_sid="CA_test")
    state.intent = Intent.SCHEDULING
    state.address_need = AddressNeed.REQUIRED_NOW
    state.mark_slot_asked("service_address")

    action = plan_next_action(state)

    assert action.name == ActionName.TAKE_MESSAGE
    assert action.allowed_slots == ()
    assert action.question_required is False
    assert "service_address" in action.forbidden_slots


def test_next_action_rejects_contradictory_question_contracts():
    with pytest.raises(ValueError, match="question requires an allowed slot"):
        NextAction(
            name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
            reason="invalid test action",
            question_required=True,
        )

    with pytest.raises(ValueError, match="both allowed and forbidden"):
        NextAction(
            name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
            reason="invalid test action",
            allowed_slots=("urgency",),
            forbidden_slots=("urgency",),
            question_required=True,
        )

    with pytest.raises(ValueError, match="allowed slots require a question contract"):
        NextAction(
            name=ActionName.ANSWER_DIRECT_QUESTION,
            reason="invalid test action",
            allowed_slots=("urgency",),
        )

    with pytest.raises(ValueError, match="question-producing action"):
        NextAction(
            name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
            reason="invalid test action",
        )


@pytest.mark.parametrize(
    "invalid_name",
    [ActionName.WRAP_UP.value, "unsupported_action", None, 0],
)
def test_next_action_requires_action_name_enum(invalid_name: object):
    with pytest.raises(TypeError, match="name must be an ActionName"):
        NextAction(
            name=invalid_name,  # type: ignore[arg-type]
            reason="invalid test action",
        )


@pytest.mark.parametrize("invalid_bool", ["false", 0, 1, None])
def test_next_action_requires_real_tool_calls_allowed_bool(invalid_bool: object):
    with pytest.raises(TypeError, match="tool_calls_allowed must be a bool"):
        NextAction(
            name=ActionName.WRAP_UP,
            reason="invalid test action",
            tool_calls_allowed=invalid_bool,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("invalid_bool", ["false", 0, 1, None])
def test_next_action_requires_real_question_required_bool(invalid_bool: object):
    allowed_slots = ("urgency",) if bool(invalid_bool) else ()

    with pytest.raises(TypeError, match="question_required must be a bool"):
        NextAction(
            name=ActionName.ANSWER_DIRECT_QUESTION,
            reason="invalid test action",
            allowed_slots=allowed_slots,
            question_required=invalid_bool,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("allowed_slots", ["urgency"]),
        ("allowed_slots", ("urgency", 1)),
        ("forbidden_slots", ["service_address"]),
        ("forbidden_slots", ("service_address", None)),
        ("memory_facts_safe_to_use", ["caller_identity:Fixture Caller"]),
        ("memory_facts_safe_to_use", ("caller_identity:Fixture Caller", 1)),
    ],
)
def test_next_action_requires_immutable_string_tuples(
    field_name: str,
    invalid_value: object,
):
    arguments = {
        "name": ActionName.ANSWER_DIRECT_QUESTION,
        "reason": "invalid test action",
        field_name: invalid_value,
    }
    if field_name == "allowed_slots":
        arguments["question_required"] = True

    with pytest.raises(TypeError, match=rf"{field_name} must be a tuple of strings"):
        NextAction(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("reason", None),
        ("reason", 1),
        ("max_spoken_shape", None),
        ("max_spoken_shape", ["one short sentence"]),
    ],
)
def test_next_action_requires_string_text_fields(
    field_name: str,
    invalid_value: object,
):
    arguments = {
        "name": ActionName.WRAP_UP,
        "reason": "valid reason",
        field_name: invalid_value,
    }

    with pytest.raises(TypeError, match=rf"{field_name} must be a string"):
        NextAction(**arguments)  # type: ignore[arg-type]


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
