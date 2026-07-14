"""Instruction composition from receptionist state and planner output."""

from app.services.dialogue_planner import plan_next_action
from app.services.instruction_composer import compose_turn_instructions
from app.services.receptionist_state import (
    CallerObservation,
    IntakeState,
    Intent,
    ServiceAction,
)


def test_composer_includes_state_allowed_action_and_forbidden_repeats():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan",
        caller_source="customer_memory",
        caller_confidence=0.93,
        memory_refs_used=("scoped-memory-ref-1",),
    )
    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.PRICING_QUESTION,
            service_object="toilet",
            service_action=ServiceAction.REPLACE,
        )
    )
    action = plan_next_action(state)

    instructions = compose_turn_instructions(
        state,
        action,
        private_memory_lines=("Prior service: kitchen sink drain repair.",),
    )

    assert "Current state:" in instructions
    assert "Caller is Jonathan" in instructions
    assert "Caller ID ending: 8667" in instructions
    assert "Service object: toilet" in instructions
    assert "Service action: replace" in instructions
    assert "Language: unknown" in instructions
    assert "Allowed next action:" in instructions
    assert "answer_direct_question" in instructions
    assert "Do not ask:" in instructions
    assert "whether this is repair, replacement, installation, or inspection" in instructions
    assert "which fixture, appliance, or object this is" in instructions
    assert "callback number" in instructions
    assert "service address" in instructions
    assert "Prior service: kitchen sink drain repair." in instructions
    assert "Jobber" not in instructions
    assert "caller-id-ending-8667" not in instructions
    assert "caller-full-phone" not in instructions


def test_composer_omits_empty_memory_section():
    state = IntakeState.new(call_sid="CA_test")
    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.SERVICE_REQUEST,
            service_object="faucet",
            service_action=ServiceAction.REPAIR,
        )
    )
    action = plan_next_action(state)

    instructions = compose_turn_instructions(state, action)

    assert "Private memory:" not in instructions
    assert "Allowed next action:" in instructions
    assert "one question maximum" in instructions


def test_composer_sanitizes_private_memory_before_model_context():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.SERVICE_REQUEST,
            service_object="faucet",
            service_action=ServiceAction.REPAIR,
        )
    )
    action = plan_next_action(state)

    instructions = compose_turn_instructions(
        state,
        action,
        private_memory_lines=(
            "PRIVATE_SOURCE note: caller-id-ending-8667. "
            "SENSITIVE_SENTINEL. Prior sink repair is relevant.",
        ),
    )

    assert "Prior sink repair is relevant." in instructions
    assert "PRIVATE_SOURCE" not in instructions
    assert "SENSITIVE_SENTINEL" not in instructions
    assert "caller-id-ending-8667" not in instructions
    assert "Caller ID ending: 8667" in instructions
