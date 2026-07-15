"""Tests for bounded Gemini Live caller-turn event decoding."""

from __future__ import annotations

import json

import pytest

from app.services.caller_turns import CallerTurnEventKind
from app.services.gemini_turn_events import (
    GEMINI_RAW_MESSAGE_MAX_BYTES,
    GeminiTurnEventAdapter,
    GeminiTurnEventDecodeStatus,
    GeminiTurnEventRejectionCode,
)


def _adapt(message: object):
    return GeminiTurnEventAdapter().adapt_message(
        message,
        at_ms=125,
        first_sequence=20,
        epoch=3,
    )


def test_adapter_decodes_known_message_shapes_in_stable_order():
    batch = _adapt(
        {
            "serverContent": {
                "inputTranscription": {"text": "Synthetic request"},
                "modelTurn": {"parts": [{"text": "not retained"}]},
                "generationComplete": True,
                "turnComplete": True,
                "interrupted": True,
            },
            "toolCall": {"functionCalls": [{"name": "lookup"}]},
            "toolCallCancellation": {"ids": ["synthetic-tool-id"]},
        }
    )

    assert batch.status is GeminiTurnEventDecodeStatus.DECODED
    assert batch.rejection_code is None
    assert [event.kind for event in batch.events] == [
        CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
        CallerTurnEventKind.MODEL_OUTPUT_STARTED,
        CallerTurnEventKind.GENERATION_COMPLETE,
        CallerTurnEventKind.TURN_COMPLETE,
        CallerTurnEventKind.INTERRUPTED,
        CallerTurnEventKind.TOOL_CALL_STARTED,
        CallerTurnEventKind.TOOL_CALL_CANCELLED,
    ]
    assert [event.sequence for event in batch.events] == list(range(20, 27))
    assert {event.at_ms for event in batch.events} == {125}
    assert {event.epoch for event in batch.events} == {3}
    assert batch.events[0].text == "Synthetic request"
    assert all(not event.text for event in batch.events[1:])


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ({"inputTranscript": "Compatibility text"}, "Compatibility text"),
        ({"inputTranscription": "Compatibility text"}, "Compatibility text"),
        ({"inputTranscription": {"text": "Official text"}}, "Official text"),
    ],
)
def test_adapter_supports_current_transcription_compatibility_shapes(content, expected):
    batch = _adapt({"serverContent": content})

    assert batch.status is GeminiTurnEventDecodeStatus.DECODED
    assert len(batch.events) == 1
    assert batch.events[0].kind is CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT
    assert batch.events[0].text == expected


@pytest.mark.parametrize(
    ("field", "kind"),
    [
        ("generationComplete", CallerTurnEventKind.GENERATION_COMPLETE),
        ("turnComplete", CallerTurnEventKind.TURN_COMPLETE),
        ("interrupted", CallerTurnEventKind.INTERRUPTED),
    ],
)
def test_adapter_decodes_each_server_terminal(field, kind):
    batch = _adapt({"serverContent": {field: True}})

    assert [event.kind for event in batch.events] == [kind]


def test_adapter_ignores_unknown_messages_without_reflecting_payload():
    batch = _adapt({"futureProviderEvent": {"private": "must-not-escape"}})

    assert batch.status is GeminiTurnEventDecodeStatus.IGNORED
    assert batch.events == ()
    assert batch.rejection_code is None
    assert "must-not-escape" not in json.dumps(batch.redacted_report_dict())


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ([], GeminiTurnEventRejectionCode.MALFORMED_MESSAGE),
        (
            {"serverContent": {"turnComplete": "yes"}},
            GeminiTurnEventRejectionCode.MALFORMED_MESSAGE,
        ),
        (
            {"serverContent": {"inputTranscription": {"text": 123}}},
            GeminiTurnEventRejectionCode.MALFORMED_MESSAGE,
        ),
        (
            {"toolCall": {"functionCalls": "lookup"}},
            GeminiTurnEventRejectionCode.MALFORMED_MESSAGE,
        ),
        (
            {"toolCallCancellation": {"ids": [123]}},
            GeminiTurnEventRejectionCode.MALFORMED_MESSAGE,
        ),
    ],
)
def test_adapter_rejects_malformed_known_shapes_with_bounded_codes(message, code):
    batch = _adapt(message)

    assert batch.status is GeminiTurnEventDecodeStatus.REJECTED
    assert batch.rejection_code is code
    assert batch.events == ()


def test_adapter_rejects_oversized_raw_message_without_retaining_it():
    private_payload = "s" * GEMINI_RAW_MESSAGE_MAX_BYTES
    batch = _adapt({"unknown": private_payload})

    assert batch.status is GeminiTurnEventDecodeStatus.REJECTED
    assert batch.rejection_code is GeminiTurnEventRejectionCode.MESSAGE_TOO_LARGE
    serialized = json.dumps(batch.redacted_report_dict())
    assert private_payload not in serialized
    assert set(batch.redacted_report_dict()) == {
        "schema_version",
        "status",
        "rejection_code",
        "event_count",
        "event_types",
    }


def test_adapter_rejects_invalid_local_metadata_before_decoding():
    with pytest.raises(ValueError, match="nonnegative"):
        GeminiTurnEventAdapter().adapt_message(
            {},
            at_ms=-1,
            first_sequence=1,
            epoch=1,
        )


@pytest.mark.parametrize(
    "kind",
    [
        CallerTurnEventKind.CONNECTION_CLOSED,
        CallerTurnEventKind.RECONNECT_STARTED,
        CallerTurnEventKind.PIPELINE_STOPPED,
    ],
)
def test_adapter_builds_only_supported_local_lifecycle_events(kind):
    event = GeminiTurnEventAdapter().adapt_lifecycle(
        kind,
        at_ms=50,
        sequence=7,
        epoch=2,
    )

    assert event.kind is kind
    assert event.at_ms == 50
    assert event.sequence == 7
    assert event.epoch == 2

    with pytest.raises(ValueError, match="lifecycle"):
        GeminiTurnEventAdapter().adapt_lifecycle(
            CallerTurnEventKind.TURN_COMPLETE,
            at_ms=50,
            sequence=8,
            epoch=2,
        )
