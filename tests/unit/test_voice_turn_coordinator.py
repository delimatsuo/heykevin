"""Receipt-driven voice lifecycle tests."""

import pytest

from app.services.voice_turn_coordinator import (
    CoordinatorDirective,
    CoordinatorOutcome,
    PlaybackReceipt,
    PlaybackStatus,
    TurnLifecycle,
    VoiceTurnCoordinator,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _receipt(turn: int, status: PlaybackStatus = PlaybackStatus.PLAYED):
    return PlaybackReceipt(
        turn=turn,
        epoch=turn,
        phase="response_end",
        status=status,
    )


def test_question_is_committed_and_timer_starts_only_after_played_receipt():
    clock = _Clock()
    coordinator = VoiceTurnCoordinator(no_input_seconds=10, clock=clock)

    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=4,
        caller_turn=1,
        expects_input=True,
        asked_slot="callback_confirmation",
    )
    clock.now = 500
    assert coordinator.due_action() == CoordinatorDirective.NONE

    cleared = coordinator.resolve_playback(_receipt(4, PlaybackStatus.CLEARED))
    assert cleared.committed_slot == ""
    assert coordinator.state == TurnLifecycle.LISTENING
    assert coordinator.due_action() == CoordinatorDirective.NONE


def test_reprompt_and_silence_close_each_wait_for_their_own_played_receipt():
    clock = _Clock()
    coordinator = VoiceTurnCoordinator(no_input_seconds=10, clock=clock)
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=True,
        asked_slot="service_action",
    )

    outcome = coordinator.resolve_playback(_receipt(1))
    assert outcome.committed_slot == "service_action"
    assert coordinator.state == TurnLifecycle.AWAITING_REPLY
    clock.now += 9.9
    assert coordinator.due_action() == CoordinatorDirective.NONE
    clock.now += 0.1
    assert coordinator.due_action() == CoordinatorDirective.REPROMPT
    assert coordinator.state == TurnLifecycle.REPROMPTING

    assert coordinator.begin_playback(
        response_turn=2,
        caller_turn=1,
        expects_input=True,
        kind="reprompt",
    )
    clock.now += 100
    assert coordinator.due_action() == CoordinatorDirective.NONE
    coordinator.resolve_playback(_receipt(2))
    assert coordinator.state == TurnLifecycle.AWAITING_PRESENCE
    clock.now += 10
    assert coordinator.due_action() == CoordinatorDirective.CLOSE_FOR_SILENCE

    assert coordinator.begin_playback(
        response_turn=3,
        caller_turn=1,
        expects_input=False,
        close_after_playback=True,
        kind="silence_close",
    )
    assert coordinator.resolve_playback(
        _receipt(3, PlaybackStatus.STALE)
    ).directive == CoordinatorDirective.NONE
    assert coordinator.state == TurnLifecycle.LISTENING
    assert coordinator.resolve_playback(
        _receipt(3)
    ).directive == CoordinatorDirective.NONE


def test_presence_ack_replays_original_question_before_accepting_an_answer():
    clock = _Clock()
    coordinator = VoiceTurnCoordinator(no_input_seconds=10, clock=clock)
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=True,
        asked_slot="callback_confirmation",
    )
    coordinator.resolve_playback(_receipt(1))
    clock.now += 10
    assert coordinator.due_action() == CoordinatorDirective.REPROMPT
    assert coordinator.begin_playback(
        response_turn=2,
        caller_turn=1,
        expects_input=True,
        kind="reprompt",
    )
    coordinator.resolve_playback(_receipt(2))

    coordinator.caller_activity()
    assert coordinator.begin_presence_resolution(2)
    assert coordinator.begin_question_replay(2)
    assert coordinator.begin_playback(
        response_turn=3,
        caller_turn=2,
        expects_input=True,
        asked_slot="callback_confirmation",
        kind="question_replay",
    )
    outcome = coordinator.resolve_playback(_receipt(3))

    assert outcome.committed_slot == "callback_confirmation"
    assert coordinator.state == TurnLifecycle.AWAITING_REPLY
    clock.now += 10
    assert coordinator.due_action() == CoordinatorDirective.CLOSE_FOR_SILENCE


def test_presence_context_requires_typed_acceptance_before_normal_generation():
    coordinator = VoiceTurnCoordinator()
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=True,
        asked_slot="callback_confirmation",
    )
    coordinator.resolve_playback(_receipt(1))
    coordinator._deadline = 0
    assert coordinator.due_action() == CoordinatorDirective.REPROMPT
    assert coordinator.begin_playback(
        response_turn=2,
        caller_turn=1,
        expects_input=True,
        kind="reprompt",
    )
    coordinator.resolve_playback(_receipt(2))
    coordinator.caller_activity()

    assert coordinator.begin_generation(2) is False
    assert coordinator.begin_presence_resolution(2) is True
    assert coordinator.state == TurnLifecycle.RESOLVING_PRESENCE
    assert coordinator.accept_presence_answer(2) is True
    assert coordinator.state == TurnLifecycle.GENERATING


def test_owner_message_transition_replaces_active_question_contract():
    coordinator = VoiceTurnCoordinator()
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=True,
        asked_slot="callback_confirmation",
    )
    coordinator.resolve_playback(_receipt(1))

    assert coordinator.begin_owner_message() is True
    assert coordinator.state == TurnLifecycle.OWNER_MESSAGE_PENDING
    assert coordinator.begin_playback(
        response_turn=2,
        caller_turn=1,
        expects_input=True,
        asked_slot="message_details",
        kind="owner_unavailable",
    )
    outcome = coordinator.resolve_playback(_receipt(2))

    assert outcome.committed_slot == "message_details"
    assert coordinator.state == TurnLifecycle.AWAITING_REPLY


@pytest.mark.parametrize(
    "status",
    [PlaybackStatus.CLEARED, PlaybackStatus.STALE, PlaybackStatus.TIMEOUT],
)
def test_failed_current_playback_recovers_without_committing_or_closing(status):
    coordinator = VoiceTurnCoordinator()
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=False,
        close_after_playback=True,
    )

    outcome = coordinator.resolve_playback(_receipt(1, status))

    assert outcome == CoordinatorOutcome()
    assert coordinator.state == TurnLifecycle.LISTENING
    assert coordinator.current_response_turn is None


def test_caller_activity_cancels_stale_generation_and_no_input_deadline():
    clock = _Clock()
    coordinator = VoiceTurnCoordinator(no_input_seconds=10, clock=clock)
    assert coordinator.begin_generation(1)
    coordinator.caller_activity()
    assert coordinator.accepts_generated_turn(1) is False
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=True,
    ) is False

    assert coordinator.begin_generation(2)
    assert coordinator.begin_playback(
        response_turn=2,
        caller_turn=2,
        expects_input=True,
    )
    coordinator.resolve_playback(_receipt(2))
    coordinator.caller_activity()
    clock.now += 30
    assert coordinator.state == TurnLifecycle.LISTENING
    assert coordinator.due_action() == CoordinatorDirective.NONE


def test_older_caller_turn_cannot_restart_after_a_newer_turn():
    coordinator = VoiceTurnCoordinator()
    assert coordinator.begin_generation(2)
    coordinator.caller_activity()

    assert coordinator.begin_generation(1) is False
    assert coordinator.state == TurnLifecycle.LISTENING


def test_close_pending_rejects_new_generation():
    coordinator = VoiceTurnCoordinator()
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=False,
        close_after_playback=True,
    )
    assert coordinator.resolve_playback(
        _receipt(1)
    ).directive == CoordinatorDirective.HANGUP

    assert coordinator.begin_generation(2) is False
    assert coordinator.state == TurnLifecycle.CLOSE_PENDING


def test_closing_turn_cannot_hang_up_before_matching_response_end_played():
    coordinator = VoiceTurnCoordinator()
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=7,
        caller_turn=1,
        expects_input=False,
        close_after_playback=True,
    )

    first_media = PlaybackReceipt(
        turn=7,
        epoch=7,
        phase="first_media",
        status=PlaybackStatus.PLAYED,
    )
    assert coordinator.resolve_playback(first_media).directive == CoordinatorDirective.NONE
    assert coordinator.resolve_playback(_receipt(6)).directive == CoordinatorDirective.NONE
    assert coordinator.resolve_playback(_receipt(7)).directive == CoordinatorDirective.HANGUP
    assert coordinator.state == TurnLifecycle.CLOSE_PENDING


def test_nonclosing_statement_gets_receipt_gated_silence_close_without_reprompt():
    clock = _Clock()
    coordinator = VoiceTurnCoordinator(no_input_seconds=10, clock=clock)
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=False,
    )

    clock.now += 100
    assert coordinator.due_action() == CoordinatorDirective.NONE
    outcome = coordinator.resolve_playback(_receipt(1))
    assert outcome.committed_slot == ""
    assert coordinator.state == TurnLifecycle.AWAITING_REPLY
    clock.now += 10
    assert coordinator.due_action() == CoordinatorDirective.CLOSE_FOR_SILENCE
