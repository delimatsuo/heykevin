"""Receptionist call-state memory behavior."""

import json

import pytest

from app.services.receptionist_state import (
    AddressNeed,
    BusinessScope,
    CallerIdentity,
    CallerObservation,
    CallbackConfirmation,
    CallbackIntent,
    IntakePhase,
    IntakeState,
    Intent,
    ServiceAction,
    Urgency,
)


@pytest.mark.parametrize(
    "confidence",
    [float("inf"), float("nan"), -0.01, 1.01, True, int("9" * 1000)],
)
def test_caller_identity_rejects_invalid_confidence(confidence: object):
    with pytest.raises((TypeError, ValueError)):
        CallerIdentity(name="Synthetic Caller", confidence=confidence)


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


def test_intake_state_applies_and_restores_scope_identity_and_address_transitions():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_name="Synthetic Caller",
        caller_source="synthetic_memory",
        caller_confidence=0.95,
    )

    state.apply_caller_observation(
        CallerObservation(
            identity_confirmed=False,
            business_scope=BusinessScope.OUT_OF_SCOPE,
            business_scope_reason="synthetic unsupported service",
            address_need=AddressNeed.REQUIRED_NOW,
        )
    )

    assert state.caller_identity.confirmed is False
    assert state.business_scope == BusinessScope.OUT_OF_SCOPE
    assert state.business_scope_reason == "synthetic unsupported service"
    assert state.address_need == AddressNeed.REQUIRED_NOW
    assert state.phase == IntakePhase.UNDERSTAND_REQUEST

    restored = IntakeState.from_dict(state.to_dict())
    assert restored.caller_identity.confirmed is False
    assert restored.business_scope == BusinessScope.OUT_OF_SCOPE
    assert restored.business_scope_reason == "synthetic unsupported service"
    assert restored.address_need == AddressNeed.REQUIRED_NOW
    assert restored.side_effects_allowed is False


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


def test_intake_state_tracks_only_replacement_callback_last_four():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.callback_confirmation = CallbackConfirmation.REJECTED
    state.mark_slot_asked("callback_confirmation")

    state.apply_caller_observation(CallerObservation(callback_phone_last_four="4321"))

    assert state.callback_phone_last_four == "4321"
    assert state.callback_confirmation == CallbackConfirmation.UNKNOWN
    assert "callback_confirmation" not in state.asked_slots
    assert state.to_dict()["callback_phone_last_four"] == "4321"
    assert state.redacted_log_dict()["callback_phone_last_four"] == "4321"

    with pytest.raises(ValueError, match="exactly four digits"):
        CallerObservation(callback_phone_last_four="caller-full-phone")


@pytest.mark.parametrize(
    "caller_phone_last_four",
    ["123", "12345", "12ab", "caller-full-phone"],
)
def test_intake_state_restore_rejects_non_last_four_caller_phone(
    caller_phone_last_four: str,
):
    exported = IntakeState.new(call_sid="CA_test").to_dict()
    exported["caller_phone_last_four"] = caller_phone_last_four

    with pytest.raises(ValueError, match="exactly four digits"):
        IntakeState.from_dict(exported)


@pytest.mark.parametrize("field_name", ["caller_phone_last_four", "callback_phone_last_four"])
@pytest.mark.parametrize("value", [1234, 0, False, True])
def test_intake_state_restore_rejects_non_string_phone_last_four(
    field_name: str,
    value: object,
):
    exported = IntakeState.new(call_sid="CA_test").to_dict()
    exported[field_name] = value

    with pytest.raises(TypeError, match=f"{field_name} must be a string"):
        IntakeState.from_dict(exported)


def test_intake_state_restore_rejects_non_boolean_side_effect_permission():
    exported = IntakeState.new(call_sid="CA_test").to_dict()
    exported["side_effects_allowed"] = "false"

    with pytest.raises(TypeError, match="side_effects_allowed must be a boolean"):
        IntakeState.from_dict(exported)


def test_intake_state_records_asked_slots_without_duplicates():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")

    state.mark_slot_asked("service_action")
    state.mark_slot_asked("service_action")
    state.mark_slot_asked("callback_number")

    assert state.asked_slots == {"service_action", "callback_number"}

    restored = IntakeState.from_dict(state.to_dict())
    assert restored.asked_slots == {"service_action", "callback_number"}


def test_intake_state_derives_canonical_askable_slots_from_known_facts():
    state = IntakeState.new(call_sid="CA_test")
    state.known_facts = [
        "job_complexity:existing fixture already removed",
        "callback_intent:declined",
        "urgency:",
        "unstructured note without a slot",
        "unsupported_slot:value",
    ]

    assert state.known_askable_slots() == {
        "job_complexity",
        "callback_preference",
    }


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


def _synthetic_full_phone() -> str:
    return "-".join(("212", "555", "0123"))


def _synthetic_slash_phone() -> str:
    return "/".join(("212", "555", "0123"))


def test_state_text_inputs_reject_full_phone_numbers():
    phone = _synthetic_full_phone()

    with pytest.raises(ValueError, match="full phone"):
        CallerIdentity(name=phone)
    with pytest.raises(ValueError, match="full phone"):
        CallerIdentity(source=phone)
    with pytest.raises(ValueError, match="full phone"):
        CallerObservation(service_object=phone)
    with pytest.raises(ValueError, match="full phone"):
        CallerObservation(business_scope_reason=phone)


def test_state_text_inputs_reject_slash_separated_phone_numbers():
    phone = _synthetic_slash_phone()

    with pytest.raises(ValueError, match="full phone"):
        CallerIdentity(name=phone)
    with pytest.raises(ValueError, match="full phone"):
        CallerObservation(service_object=phone)


@pytest.mark.parametrize("field", ["known_facts", "asked_slots", "memory_refs_used"])
def test_state_restore_rejects_full_phone_numbers_in_collections(field: str):
    exported = IntakeState.new(call_sid="CA_test").to_dict()
    exported[field] = [_synthetic_full_phone()]

    with pytest.raises(ValueError, match="full phone"):
        IntakeState.from_dict(exported)


def test_state_rejects_full_phone_number_when_recording_asked_slot():
    state = IntakeState.new(call_sid="CA_test")

    with pytest.raises(ValueError, match="full phone"):
        state.mark_slot_asked(_synthetic_full_phone())


def test_state_rejects_full_phone_number_in_memory_reference():
    with pytest.raises(ValueError, match="full phone"):
        IntakeState.new(
            call_sid="CA_test",
            memory_refs_used=(_synthetic_full_phone(),),
        )


@pytest.mark.parametrize("field", ["known_facts", "asked_slots", "memory_refs_used"])
def test_state_direct_construction_rejects_scalar_collection(field: str):
    with pytest.raises(TypeError, match=f"{field} must be a non-string collection"):
        IntakeState(**{field: "single-value"})


@pytest.mark.parametrize("field", ["known_facts", "asked_slots", "memory_refs_used"])
def test_state_restore_rejects_scalar_collection(field: str):
    exported = IntakeState.new(call_sid="CA_test").to_dict()
    exported[field] = "single-value"

    with pytest.raises(TypeError, match=f"{field} must be a non-string collection"):
        IntakeState.from_dict(exported)


def test_state_new_rejects_scalar_memory_references():
    with pytest.raises(TypeError, match="memory_refs_used must be a non-string collection"):
        IntakeState.new(call_sid="CA_test", memory_refs_used="single-value")


@pytest.mark.parametrize("field_name", ["caller_phone_last_four", "callback_phone_last_four"])
def test_state_direct_construction_rejects_full_phone_last_four(field_name: str):
    with pytest.raises(ValueError, match="exactly four digits"):
        IntakeState(**{field_name: _synthetic_full_phone()})


@pytest.mark.parametrize("field_name", ["caller_phone_last_four", "callback_phone_last_four"])
@pytest.mark.parametrize("value", [1234, False])
def test_state_direct_construction_rejects_non_string_phone_last_four(
    field_name: str,
    value: object,
):
    with pytest.raises(TypeError, match=f"{field_name} must be a string"):
        IntakeState(**{field_name: value})


@pytest.mark.parametrize("field_name", ["caller_phone_last_four", "callback_phone_last_four"])
def test_state_restore_rejects_full_phone_last_four(field_name: str):
    exported = IntakeState.new(call_sid="CA_test").to_dict()
    exported[field_name] = _synthetic_full_phone()

    with pytest.raises(ValueError, match="exactly four digits"):
        IntakeState.from_dict(exported)


def test_state_last_four_fields_preserve_valid_and_legacy_empty_values():
    state = IntakeState(
        caller_phone_last_four="1234",
        callback_phone_last_four="5678",
    )

    assert state.to_dict()["caller_phone_last_four"] == "1234"
    assert state.to_dict()["callback_phone_last_four"] == "5678"
    assert IntakeState.from_dict({}).caller_phone_last_four == ""
    assert IntakeState.from_dict({}).callback_phone_last_four == ""
