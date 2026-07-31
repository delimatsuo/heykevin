"""Tests for provider-neutral retrospective caller-turn assembly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.caller_turns import (
    CallerTurnAssembler,
    CallerTurnCloseReason,
    CallerTurnCompletionStatus,
    CallerTurnEvent,
    CallerTurnEventKind,
    retrospective_turn_observation,
)


FIXTURE_PATH = Path("tests/fixtures/caller_turn_events/permutations.json")


def _event(
    kind: CallerTurnEventKind,
    *,
    at_ms: int,
    sequence: int,
    epoch: int = 1,
    text: str = "",
) -> CallerTurnEvent:
    return CallerTurnEvent(
        kind=kind,
        at_ms=at_ms,
        sequence=sequence,
        epoch=epoch,
        text=text,
    )


def test_permutation_fixtures_define_deterministic_retrospective_turns():
    fixture = json.loads(FIXTURE_PATH.read_text())
    assert fixture["version"] == 1

    for case in fixture["cases"]:
        assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=100)
        turns = []
        for raw_event in case["events"]:
            turns.extend(
                assembler.ingest(
                    CallerTurnEvent.from_dict(raw_event),
                )
            )
        turns.extend(assembler.advance_time(case["advance_to_ms"]))

        assert [turn.to_dict() for turn in turns] == case["expected_turns"], case["name"]


def test_duplicate_receipt_sequence_is_ignored_once():
    assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=100)

    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=10,
            sequence=1,
            text="Need a plumber",
        )
    )
    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=10,
            sequence=1,
            text="Need a plumber",
        )
    )
    assembler.ingest(
        _event(CallerTurnEventKind.MODEL_OUTPUT_STARTED, at_ms=20, sequence=2)
    )

    turns = assembler.advance_time(120)

    assert len(turns) == 1
    assert turns[0].transcript == "Need a plumber"
    assert turns[0].event_count == 2
    assert assembler.duplicate_event_count == 1


def test_reconnect_finalizes_partial_turn_and_ignores_old_epoch_events():
    assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=100)
    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=10,
            sequence=1,
            text="The sink is leaking",
        )
    )

    turns = assembler.ingest(
        _event(
            CallerTurnEventKind.RECONNECT_STARTED,
            at_ms=20,
            sequence=1,
            epoch=2,
        )
    )

    assert len(turns) == 1
    assert turns[0].status is CallerTurnCompletionStatus.PARTIAL
    assert turns[0].close_reason is CallerTurnCloseReason.RECONNECT_STARTED
    assert turns[0].epoch == 1

    assert not assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=30,
            sequence=2,
            epoch=1,
            text=" stale",
        )
    )
    assert assembler.stale_event_count == 1

    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=40,
            sequence=2,
            epoch=2,
            text="A toilet is blocked",
        )
    )
    assembler.ingest(
        _event(
            CallerTurnEventKind.TURN_COMPLETE,
            at_ms=50,
            sequence=3,
            epoch=2,
        )
    )
    completed = assembler.advance_time(150)

    assert len(completed) == 1
    assert completed[0].turn_id == 2
    assert completed[0].epoch == 2
    assert completed[0].transcript == "A toilet is blocked"


def test_resource_limit_drops_turn_without_retaining_transcript():
    assembler = CallerTurnAssembler(
        active_epoch=1,
        quiescence_ms=100,
        max_events_per_turn=2,
        max_transcript_codepoints=12,
        max_transcript_utf8_bytes=24,
    )
    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=10,
            sequence=1,
            text="123456789012",
        )
    )

    turns = assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=20,
            sequence=2,
            text="3",
        )
    )

    assert len(turns) == 1
    assert turns[0].status is CallerTurnCompletionStatus.DROPPED
    assert turns[0].close_reason is CallerTurnCloseReason.RESOURCE_LIMIT
    assert turns[0].transcript == ""
    assert assembler.next_deadline_ms is None


@pytest.mark.parametrize(
    "changes",
    [
        {"at_ms": -1},
        {"sequence": -1},
        {"epoch": -1},
        {"text": "bad\x00text"},
        {"text": "\ud800"},
    ],
)
def test_event_validation_rejects_invalid_or_unencodable_values(changes):
    values = {
        "kind": CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
        "at_ms": 1,
        "sequence": 1,
        "epoch": 1,
        "text": "valid",
    }
    values.update(changes)

    with pytest.raises((TypeError, ValueError)):
        CallerTurnEvent(**values)


def test_event_from_dict_rejects_unknown_fields_and_wrong_types():
    with pytest.raises(ValueError, match="unknown caller turn event field"):
        CallerTurnEvent.from_dict(
            {
                "kind": "turn_complete",
                "at_ms": 1,
                "sequence": 1,
                "epoch": 1,
                "unexpected": True,
            }
        )

    with pytest.raises(TypeError, match="text must be a string"):
        CallerTurnEvent.from_dict(
            {
                "kind": "input_transcript_fragment",
                "at_ms": 1,
                "sequence": 1,
                "epoch": 1,
                "text": 123,
            }
        )


def test_finish_classifies_pending_turn_and_clears_deadline():
    assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=100)
    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=10,
            sequence=1,
            text="Please call back",
        )
    )

    turns = assembler.finish(
        at_ms=20,
        reason=CallerTurnCloseReason.PIPELINE_STOPPED,
    )

    assert len(turns) == 1
    assert turns[0].status is CallerTurnCompletionStatus.PARTIAL
    assert assembler.next_deadline_ms is None
    assert not assembler.finish(
        at_ms=30,
        reason=CallerTurnCloseReason.PIPELINE_STOPPED,
    )


def test_consecutive_turns_receive_distinct_monotonic_ids():
    assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=100)

    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=10,
            sequence=1,
            text="First request",
        )
    )
    assembler.ingest(
        _event(CallerTurnEventKind.TURN_COMPLETE, at_ms=20, sequence=2)
    )
    first = assembler.advance_time(120)

    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=130,
            sequence=3,
            text="Second request",
        )
    )
    assembler.ingest(
        _event(CallerTurnEventKind.TURN_COMPLETE, at_ms=140, sequence=4)
    )
    second = assembler.advance_time(240)

    assert [turn.turn_id for turn in (*first, *second)] == [1, 2]
    assert [turn.transcript for turn in (*first, *second)] == [
        "First request",
        "Second request",
    ]


def test_tool_call_cancellation_emits_cancelled_turn_immediately():
    assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=100)
    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=10,
            sequence=1,
            text="Cancel that request",
        )
    )

    turns = assembler.ingest(
        _event(
            CallerTurnEventKind.TOOL_CALL_CANCELLED,
            at_ms=20,
            sequence=2,
        )
    )

    assert len(turns) == 1
    assert turns[0].status is CallerTurnCompletionStatus.CANCELLED
    assert turns[0].close_reason is CallerTurnCloseReason.TOOL_CALL_CANCELLED
    assert assembler.next_deadline_ms is None


def test_time_and_configuration_must_be_positive_and_monotonic():
    with pytest.raises(ValueError, match="quiescence_ms must be positive"):
        CallerTurnAssembler(active_epoch=1, quiescence_ms=0)
    with pytest.raises(ValueError, match="resource limits must be positive"):
        CallerTurnAssembler(active_epoch=1, max_events_per_turn=0)
    with pytest.raises(ValueError, match="retained sequence limit must be positive"):
        CallerTurnAssembler(active_epoch=1, max_retained_sequences=0)

    assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=100)
    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=10,
            sequence=1,
            text="Hello",
        )
    )
    with pytest.raises(ValueError, match="event time must be monotonic"):
        assembler.ingest(
            _event(
                CallerTurnEventKind.TURN_COMPLETE,
                at_ms=9,
                sequence=2,
            )
        )
    with pytest.raises(ValueError, match="time must be monotonic"):
        assembler.advance_time(9)


def test_receipt_deduplication_memory_is_bounded_across_a_long_epoch():
    assembler = CallerTurnAssembler(
        active_epoch=1,
        quiescence_ms=100,
        max_retained_sequences=3,
    )

    for sequence in range(1, 6):
        assembler.ingest(
            _event(
                CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                at_ms=sequence,
                sequence=sequence,
                text="x",
            )
        )

    assert assembler.retained_sequence_count == 3


def test_event_at_expired_deadline_finalizes_before_starting_next_turn():
    assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=100)
    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=0,
            sequence=1,
            text="First activity",
        )
    )
    assembler.ingest(
        _event(CallerTurnEventKind.TURN_COMPLETE, at_ms=10, sequence=2)
    )

    first = assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=110,
            sequence=3,
            text="Second activity",
        )
    )
    assembler.ingest(
        _event(CallerTurnEventKind.TURN_COMPLETE, at_ms=120, sequence=4)
    )
    second = assembler.advance_time(500)

    assert [turn.transcript for turn in (*first, *second)] == [
        "First activity",
        "Second activity",
    ]
    assert first[0].finalized_at_ms == 110
    assert second[0].finalized_at_ms == 220


def test_redacted_report_excludes_transcript_and_event_payloads():
    assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=100)
    assembler.ingest(
        _event(
            CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
            at_ms=10,
            sequence=1,
            text="Synthetic caller text",
        )
    )
    assembler.ingest(
        _event(CallerTurnEventKind.TURN_COMPLETE, at_ms=20, sequence=2)
    )
    turn = assembler.advance_time(120)[0]

    report = turn.redacted_report_dict()

    assert "transcript" not in report
    assert "text" not in json.dumps(report)
    assert report["transcript_codepoints"] == len("Synthetic caller text")
    assert report["status"] == "retrospective_complete"


def test_retrospective_observation_excludes_transcript_and_live_authority():
    assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=100)
    assembler.ingest(_event(CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT, at_ms=1, sequence=1, text="Synthetic caller text"))
    assembler.ingest(_event(CallerTurnEventKind.TURN_COMPLETE, at_ms=2, sequence=2))
    observation = retrospective_turn_observation(assembler.advance_time(102)[0])

    assert observation["scope"] == "retrospective_telemetry_only"
    assert "transcript" not in observation
    assert "authorization" not in observation
