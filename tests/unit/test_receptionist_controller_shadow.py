"""Offline receptionist controller adapter and live-path isolation tests."""

from dataclasses import asdict
import json
from pathlib import Path

from app.services.dialogue_planner import ActionName
from app.services.receptionist_controller import ShadowReceptionistController
from app.services.receptionist_state import ServiceAction


def test_live_voice_pipelines_do_not_import_or_wire_controller_modules():
    controller_modules = (
        "dialogue_planner",
        "instruction_composer",
        "receptionist_controller",
        "receptionist_replay",
        "receptionist_state",
        "receptionist_turns",
    )

    for path in (
        Path("app/services/gemini_pipeline.py"),
        Path("app/services/voice_pipeline.py"),
    ):
        source = path.read_text()
        for module_name in controller_modules:
            assert module_name not in source


def test_offline_controller_decision_contains_metrics_only():
    controller = ShadowReceptionistController.new(
        call_sid="test-call",
        caller_phone="caller-id-ending-8667",
        contractor_config={"known_caller_name": "Private Caller"},
    )

    decision = controller.observe_caller_turn(
        "Private Caller needs a toilet replacement at a private address."
    )

    assert decision is not None
    assert decision.action_name == ActionName.ASK_ONE_CLARIFYING_QUESTION
    assert decision.turn_id == 1
    assert decision.known_fact_count == 2
    assert decision.instruction_chars > 0
    assert controller.state.caller_phone_last_four == "8667"
    assert controller.state.caller_identity.name == "Private Caller"
    serialized = json.dumps(asdict(decision))
    assert "Private Caller" not in serialized
    assert "private address" not in serialized
    assert "8667" not in serialized


def test_offline_controller_amends_pending_caller_turn():
    controller = ShadowReceptionistController.new(call_sid="test-call")

    first = controller.observe_caller_turn("I need plumbing help.")
    continuation = controller.observe_caller_turn("It is a toilet replacement.")

    assert first is not None
    assert first.turn_id == 1
    assert continuation is None
    assert controller.pending_turn_id == 1
    assert controller.state.service_object == "toilet"
    assert controller.state.service_action == ServiceAction.REPLACE
    assert controller.pending_action is not None
    assert controller.pending_action.allowed_slots == ("job_complexity",)


def test_offline_controller_commits_only_completed_assistant_turn():
    controller = ShadowReceptionistController.new(call_sid="test-call")
    decision = controller.observe_caller_turn("I need plumbing help.")

    observation = controller.observe_assistant_turn(interrupted=False)

    assert decision is not None
    assert observation.turn_id == decision.turn_id
    assert observation.interrupted is False
    assert observation.committed_slot_count == 1
    assert controller.state.asked_slots == {"service_action"}


def test_offline_controller_discards_interrupted_assistant_turn():
    controller = ShadowReceptionistController.new(call_sid="test-call")
    decision = controller.observe_caller_turn("I need plumbing help.")

    observation = controller.observe_assistant_turn(interrupted=True)

    assert decision is not None
    assert observation.turn_id == decision.turn_id
    assert observation.interrupted is True
    assert observation.committed_slot_count == 0
    assert controller.state.asked_slots == set()
