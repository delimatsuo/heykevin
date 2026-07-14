"""Receptionist call-state memory behavior."""

import json

import pytest

from app.services.receptionist_state import (
    AddressNeed,
    CallerObservation,
    CallbackConfirmation,
    CallbackIntent,
    IntakePhase,
    IntakeState,
    Intent,
    ServiceAction,
    Urgency,
)


@pytest.mark.parametrize("language", ["en", "es", "pt-BR", "ja"])
def test_intake_state_applies_provider_neutral_observations_in_any_language(language: str):
    state = IntakeState.new(call_sid="CA_test")

    state.apply_caller_observation(
        CallerObservation(
            language=language,
            intent=Intent.SERVICE_REQUEST,
            service_object="water heater",
            service_action=ServiceAction.REPAIR,
            urgency=Urgency.ROUTINE,
        )
    )

    assert state.language == language
    assert state.intent == Intent.SERVICE_REQUEST
    assert state.service_object == "water heater"
    assert state.service_action == ServiceAction.REPAIR
    assert state.urgency == Urgency.ROUTINE


def test_intake_state_extracts_known_service_facts_and_redacts_phone():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan",
        caller_source="customer_memory",
        caller_confidence=0.92,
        memory_refs_used=("scoped-memory-ref-1",),
    )

    state.apply_caller_observation(
        CallerObservation(
            language="en",
            identity_confirmed=True,
            intent=Intent.PRICING_QUESTION,
            service_object="toilet",
            service_action=ServiceAction.REPLACE,
        )
    )

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

    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.SCHEDULING,
            callback_intent=CallbackIntent.REQUESTED,
            address_need=AddressNeed.MAYBE_LATER,
        )
    )

    assert state.intent == Intent.SCHEDULING
    assert state.callback_intent == CallbackIntent.REQUESTED
    assert state.address_need == AddressNeed.MAYBE_LATER
    assert "callback_intent:requested" in state.known_facts


def test_intake_state_tracks_callback_rejection_and_language():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.callback_intent = CallbackIntent.REQUESTED

    state.apply_caller_observation(
        CallerObservation(
            language="es",
            callback_confirmation=CallbackConfirmation.REJECTED,
        )
    )

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
    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.SERVICE_REQUEST,
            service_object="faucet",
            service_action=ServiceAction.REPAIR,
        )
    )

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


def test_intake_state_correction_replaces_stale_service_facts():
    state = IntakeState.new(call_sid="CA_test")
    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.SERVICE_REQUEST,
            service_object="toilet",
            service_action=ServiceAction.REPLACE,
        )
    )

    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.SERVICE_REQUEST,
            service_object="sink",
            service_action=ServiceAction.REPAIR,
        )
    )

    assert state.service_object == "sink"
    assert state.service_action == ServiceAction.REPAIR
    assert "service_object:sink" in state.known_facts
    assert "service_action:repair" in state.known_facts
    assert "service_object:toilet" not in state.known_facts
    assert "service_action:replace" not in state.known_facts


def test_intake_state_leaves_conflicting_service_objects_unresolved():
    state = IntakeState.new(call_sid="CA_test")

    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.SERVICE_REQUEST,
            service_object="",
            service_action=ServiceAction.REPLACE,
        )
    )

    assert state.service_object == ""
    assert state.service_action == ServiceAction.REPLACE


def test_intake_state_tracks_callback_confirmation_and_decline():
    confirmed = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    confirmed.callback_intent = CallbackIntent.REQUESTED

    confirmed.apply_caller_observation(
        CallerObservation(
            callback_intent=CallbackIntent.ACCEPTED,
            callback_confirmation=CallbackConfirmation.CONFIRMED,
        )
    )

    assert confirmed.callback_intent == CallbackIntent.ACCEPTED
    assert confirmed.callback_confirmation == CallbackConfirmation.CONFIRMED

    declined = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    declined.callback_intent = CallbackIntent.REQUESTED

    declined.apply_caller_observation(
        CallerObservation(
            callback_intent=CallbackIntent.DECLINED,
            callback_confirmation=CallbackConfirmation.REJECTED,
        )
    )

    assert declined.callback_intent == CallbackIntent.DECLINED
    assert declined.callback_confirmation == CallbackConfirmation.REJECTED

    natural_confirmation = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
    )
    natural_confirmation.callback_intent = CallbackIntent.REQUESTED

    natural_confirmation.apply_caller_observation(
        CallerObservation(
            callback_intent=CallbackIntent.ACCEPTED,
            callback_confirmation=CallbackConfirmation.CONFIRMED,
        )
    )

    assert natural_confirmation.callback_intent == CallbackIntent.ACCEPTED
    assert natural_confirmation.callback_confirmation == CallbackConfirmation.CONFIRMED

    natural_decline = IntakeState.new(call_sid="CA_test")
    natural_decline.callback_intent = CallbackIntent.REQUESTED

    natural_decline.apply_caller_observation(
        CallerObservation(
            callback_intent=CallbackIntent.DECLINED,
            callback_confirmation=CallbackConfirmation.REJECTED,
        )
    )

    assert natural_decline.callback_intent == CallbackIntent.DECLINED
    assert natural_decline.callback_confirmation == CallbackConfirmation.REJECTED


def test_intake_state_does_not_escalate_explicitly_negated_emergency():
    state = IntakeState.new(call_sid="CA_test")

    state.apply_caller_observation(
        CallerObservation(
            intent=Intent.SERVICE_REQUEST,
            service_object="faucet",
            service_action=ServiceAction.REPAIR,
            urgency=Urgency.ROUTINE,
        )
    )

    assert state.intent == Intent.SERVICE_REQUEST
    assert state.urgency == Urgency.ROUTINE
