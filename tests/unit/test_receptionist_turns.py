"""Shared live/replay receptionist turn transitions."""

from app.services.dialogue_planner import ActionName
from app.services.receptionist_replay import run_replay_scenario
from app.services.receptionist_state import IntakeState, ServiceAction
from app.services.receptionist_turns import ReceptionistTurnReducer


def _reducer() -> ReceptionistTurnReducer:
    return ReceptionistTurnReducer(IntakeState.new(call_sid="CA_redacted"))


def test_completed_assistant_turn_commits_pending_allowed_slot():
    reducer = _reducer()

    planned = reducer.complete_caller_turn("I need plumbing help.")
    completed = reducer.complete_assistant_turn(interrupted=False)

    assert planned is not None
    assert planned.turn_id == 1
    assert planned.action.allowed_slots == ("service_action",)
    assert completed.turn_id == 1
    assert completed.committed_slots == ("service_action",)
    assert reducer.state.asked_slots == {"service_action"}


def test_interrupted_assistant_turn_does_not_commit_pending_slot():
    reducer = _reducer()

    first = reducer.complete_caller_turn("I need plumbing help.")
    interrupted = reducer.complete_assistant_turn(interrupted=True)
    second = reducer.complete_caller_turn("Sorry, go ahead.")

    assert first is not None
    assert interrupted.turn_id == first.turn_id
    assert interrupted.committed_slots == ()
    assert reducer.state.asked_slots == set()
    assert second is not None
    assert second.turn_id == 2
    assert second.action.allowed_slots == ("service_action",)


def test_caller_continuation_amends_state_without_second_decision():
    reducer = _reducer()

    first = reducer.complete_caller_turn("I need plumbing help.")
    continuation = reducer.complete_caller_turn("It is a toilet replacement.")

    assert first is not None
    assert continuation is None
    assert reducer.pending_turn_id == first.turn_id
    assert reducer.state.service_object == "toilet"
    assert reducer.state.service_action == ServiceAction.REPLACE
    assert reducer.pending_action is not None
    assert reducer.pending_action.allowed_slots == ("job_complexity",)


def test_assistant_greeting_without_pending_caller_turn_is_noop():
    reducer = _reducer()

    completed = reducer.complete_assistant_turn(interrupted=False)

    assert completed.turn_id is None
    assert completed.committed_slots == ()
    assert reducer.state.asked_slots == set()


def test_late_emergency_fragment_replans_same_turn_id():
    reducer = _reducer()

    first = reducer.complete_caller_turn("I need plumbing help.")
    continuation = reducer.complete_caller_turn("Actually there is a gas leak emergency.")

    assert first is not None
    assert continuation is None
    assert reducer.pending_turn_id == first.turn_id
    assert reducer.pending_action is not None
    assert reducer.pending_action.name == ActionName.SAFETY_GUIDANCE


def test_replay_and_live_reducer_share_interrupted_turn_semantics():
    scenario = {
        "initial_state": IntakeState.new(call_sid="CA_redacted").to_dict(),
        "turns": [
            {"speaker": "caller", "text": "I need plumbing help."},
            {
                "speaker": "assistant",
                "text": "Is this a repair or replacement?",
                "interrupted": True,
                "observed": {"asked_slots": ["service_action"]},
            },
            {"speaker": "caller", "text": "Sorry, go ahead."},
            {
                "speaker": "assistant",
                "text": "Is this a repair or replacement?",
                "observed": {"asked_slots": ["service_action"]},
            },
        ],
    }
    replay = run_replay_scenario(scenario)
    live = _reducer()

    live.complete_caller_turn("I need plumbing help.")
    live.complete_assistant_turn(
        interrupted=True,
        asked_slots=("service_action",),
    )
    live.complete_caller_turn("Sorry, go ahead.")
    live.complete_assistant_turn(
        interrupted=False,
        asked_slots=("service_action",),
    )

    assert replay.final_state.asked_slots == live.state.asked_slots == {
        "service_action"
    }
