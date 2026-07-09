"""Receptionist call-state memory behavior."""

import json

from app.services.receptionist_state import (
    AddressNeed,
    CallbackConfirmation,
    CallbackIntent,
    IntakePhase,
    IntakeState,
    Intent,
    ServiceAction,
)


def test_intake_state_extracts_known_service_facts_and_redacts_phone():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan",
        caller_source="customer_memory",
        caller_confidence=0.92,
        memory_refs_used=("scoped-memory-ref-1",),
    )

    state.observe_caller_turn("Hi, this is Jonathan. I wanted to know how much to replace a toilet.")

    assert state.phase == IntakePhase.UNDERSTAND_REQUEST
    assert state.intent == Intent.PRICING_QUESTION
    assert state.service_object == "toilet"
    assert state.service_action == ServiceAction.REPLACE
    assert state.caller_identity.name == "Jonathan"
    assert state.caller_identity.confirmed is True
    assert state.caller_phone_last_four == "8667"
    assert "service_object:toilet" in state.known_facts
    assert "service_action:replace" in state.known_facts

    exported = state.to_dict()
    serialized = json.dumps(exported)
    assert "caller-id-ending-8667" not in serialized
    assert "caller-full-phone" not in serialized
    assert "8667" in serialized

    restored = IntakeState.from_dict(exported)
    assert restored.service_action == ServiceAction.REPLACE
    assert restored.service_object == "toilet"
    assert restored.memory_refs_used == {"scoped-memory-ref-1"}


def test_intake_state_tracks_callback_and_scheduling_intent():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")

    state.observe_caller_turn("Could someone call me back later today to schedule this?")

    assert state.intent == Intent.SCHEDULING
    assert state.callback_intent == CallbackIntent.REQUESTED
    assert state.address_need == AddressNeed.MAYBE_LATER
    assert "callback_intent:requested" in state.known_facts


def test_intake_state_tracks_callback_rejection_and_language():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.callback_intent = CallbackIntent.REQUESTED

    state.observe_caller_turn("No, ese no es el numero correcto.")

    assert state.callback_confirmation == CallbackConfirmation.REJECTED
    assert state.language == "es"
    assert "callback_confirmation:rejected" in state.known_facts


def test_intake_state_records_asked_slots_without_duplicates():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")

    state.mark_slot_asked("service_action")
    state.mark_slot_asked("service_action")
    state.mark_slot_asked("callback_number")

    assert state.asked_slots == {"service_action", "callback_number"}

    restored = IntakeState.from_dict(state.to_dict())
    assert restored.asked_slots == {"service_action", "callback_number"}


def test_intake_state_log_dict_uses_last_four_only():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan",
        caller_source="customer_memory",
        caller_confidence=0.92,
        memory_refs_used=("scoped-memory-ref-1",),
    )
    state.observe_caller_turn("I need a faucet repair.")

    redacted = state.redacted_log_dict()
    assert redacted["caller_phone_last_four"] == "8667"
    assert "caller_phone" not in redacted
    assert "caller_identity" not in redacted
    assert "known_facts" not in redacted
    assert "memory_refs_used" not in redacted
    assert redacted["known_fact_count"] == 2
    assert redacted["memory_ref_count"] == 1
    serialized = json.dumps(redacted)
    assert "caller-id-ending-8667" not in serialized
    assert "Jonathan" not in serialized
    assert "scoped-memory-ref-1" not in serialized
