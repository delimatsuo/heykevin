"""Live intake controller sequences job questions before callback."""

from app.services.dialogue_planner import ActionName
from app.services.live_intake_controller import (
    HOLD_SPEECH_PREFIX,
    LiveIntakeController,
    credits_asked_slots,
)
from app.services.receptionist_state import (
    CallerObservation,
    Intent,
    ServiceAction,
)


def test_opening_instructions_hold_speech_and_ask_service_action():
    controller = LiveIntakeController.start(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
    )

    text = controller.opening_instructions()

    assert text.startswith(HOLD_SPEECH_PREFIX)
    assert "ask_one_clarifying_question" in text
    assert "service_action" in text
    assert "callback number" in text
    assert "service address" in text
    assert controller.last_action_name == ActionName.ASK_ONE_CLARIFYING_QUESTION.value
    assert controller.last_action is not None
    assert controller.last_action.allowed_slots == ("service_action",)


def test_schedule_request_asks_job_before_callback_without_extraction():
    controller = LiveIntakeController.start(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
    )
    controller.opening_instructions()
    first = controller.after_caller_turn()
    controller.after_kevin_turn(
        "Is this a repair, replacement, installation, or inspection?"
    )
    second = controller.after_caller_turn()

    assert "Allowed slots: service_action." in first
    assert "Allowed slots: service_object." in second
    assert "callback_preference" not in second
    assert "service address" in second


def test_known_toilet_replacement_does_not_reask_action_or_object():
    controller = LiveIntakeController.start(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
    )
    text = controller.after_caller_turn(
        CallerObservation(
            intent=Intent.PRICING_QUESTION,
            service_object="toilet",
            service_action=ServiceAction.REPLACE,
        )
    )

    assert "Do not ask:" in text
    assert "whether this is repair, replacement, installation, or inspection" in text
    assert "which fixture, appliance, or object this is" in text
    assert controller.last_action is not None
    assert "service_action" not in controller.last_action.allowed_slots
    assert "service_object" not in controller.last_action.allowed_slots


def test_greeting_does_not_credit_opening_service_action():
    controller = LiveIntakeController.start(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
    )
    controller.opening_instructions()
    controller.after_kevin_turn(
        "Hi, thank you for calling Matsuo Plumbing. My name is Kevin. How can I help you?"
    )
    text = controller.after_caller_turn()

    assert "Allowed slots: service_action." in text


def test_silence_and_hangup_scripts_do_not_credit_asked_slots():
    assert credits_asked_slots("Are you still there?") is False
    assert credits_asked_slots(
        "I'm going to hang up for now. Please call back when you're ready. Goodbye."
    ) is False
    assert credits_asked_slots(
        "Is this a repair, replacement, installation, or inspection?"
    ) is True

    controller = LiveIntakeController.start(call_sid="CA_test")
    controller.after_caller_turn()
    controller.after_kevin_turn("Are you still there?")
    text = controller.after_caller_turn()

    assert "Allowed slots: service_action." in text


def test_start_with_trusted_returning_caller_identity():
    controller = LiveIntakeController.start(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan",
        caller_source="trusted_returning_caller",
        caller_confidence=1.0,
    )

    assert controller.state.caller_identity.name == "Jonathan"
    assert controller.state.caller_identity.source == "trusted_returning_caller"
    assert controller.state.caller_identity.confidence == 1.0
    assert controller.state.caller_identity.confirmed is True

    text = controller.opening_instructions()

    assert "- Caller is Jonathan." in text
    assert "- Caller identity is unknown." not in text
    assert "Do not ask:" in text
    assert "the caller's name" in text
    assert "Allowed slots: service_action." in text
    assert controller.last_action_name == ActionName.ASK_ONE_CLARIFYING_QUESTION.value


def test_start_default_remains_anonymous_and_backward_compatible():
    controller = LiveIntakeController.start(
        call_sid="CA_test",
    )

    assert controller.state.caller_identity.name == ""
    assert controller.state.caller_identity.source == ""
    assert controller.state.caller_identity.confidence == 0.0
    assert controller.state.caller_identity.confirmed is False

    text = controller.opening_instructions()

    assert "- Caller identity is unknown." in text
    assert "- Caller is" not in text
    assert "the caller's name" not in text


def test_start_with_low_confidence_caller_identity_remains_unconfirmed():
    controller = LiveIntakeController.start(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan",
        caller_source="trusted_returning_caller",
        caller_confidence=0.5,
    )

    assert controller.state.caller_identity.name == "Jonathan"
    assert controller.state.caller_identity.confidence == 0.5
    assert controller.state.caller_identity.confirmed is False

    text = controller.opening_instructions()

    assert "- Caller identity is unknown." in text
    assert "- Caller is Jonathan." not in text
    assert "the caller's name" not in text
