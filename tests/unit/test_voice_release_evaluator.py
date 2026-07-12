"""Programmatic release gates for aggregate Gemini voice timing logs."""

import json

from scripts.evaluate_voice_release import (
    VoiceReleaseThresholds,
    evaluate_voice_release,
    extract_log_messages,
    parse_voice_timing_event,
)


def _event(event: str, call: str, **metrics: object) -> str:
    suffix = " ".join(f"{key}={value}" for key, value in metrics.items())
    return f"voice_timing event={event} call={call} {suffix}".strip()


def _passing_messages() -> list[str]:
    messages = []
    for call_number in range(10):
        call = f"call{call_number:04d}"
        messages.extend([
            _event("gemini_ws_connected", call, call_elapsed_ms=350),
            _event("greeting_instruction_sent", call, words=24),
            _event("first_outbound_audio", call, call_elapsed_ms=2100),
        ])
        for turn in range(1, 4):
            messages.extend([
                _event(
                    "response_first_audio",
                    call,
                    turn=turn,
                    latency_ms=1100,
                ),
                _event(
                    "model_turn_complete",
                    call,
                    turn=turn,
                    model_stream_ms=700,
                    generated_audio_ms=4200,
                    chars=80,
                    words=14,
                ),
            ])
        if call_number < 5:
            messages.append(_event("barge_in_clear", call, clear_ms=90))
    return messages


def test_extract_log_messages_accepts_cloud_logging_json_without_other_payloads():
    raw = json.dumps([
        {"jsonPayload": {"message": _event("first_outbound_audio", "call0001", call_elapsed_ms=2000)}},
        {"textPayload": _event("response_first_audio", "call0001", latency_ms=900)},
        {"jsonPayload": {"private_field": "must not be extracted"}},
    ])

    messages = extract_log_messages(raw)

    assert len(messages) == 2
    assert all("must not be extracted" not in message for message in messages)


def test_parse_voice_timing_event_returns_typed_safe_metadata():
    parsed = parse_voice_timing_event(
        "prefix voice_timing event=reconnect_result call=call0001 success=False clear_ms=42"
    )

    assert parsed == {
        "event": "reconnect_result",
        "call": "call0001",
        "success": False,
        "clear_ms": 42,
    }


def test_voice_release_evaluator_passes_certification_sample():
    report = evaluate_voice_release(_passing_messages())

    assert report["status"] == "pass"
    assert report["sample"] == {
        "attempted_calls": 10,
        "calls_with_first_audio": 10,
        "response_turns": 30,
        "barge_in_events": 5,
        "reconnect_attempts": 0,
        "receive_errors": 0,
        "audio_backlog_overflows": 0,
        "outbound_audio_errors": 0,
    }
    assert all(gate["passed"] for gate in report["gates"])
    serialized = json.dumps(report)
    assert "call0001" not in serialized
    assert "transcript" not in serialized


def test_voice_release_evaluator_fails_slow_verbose_and_error_sample():
    messages = _passing_messages()
    messages.extend([
        _event("first_outbound_audio", "call0009", call_elapsed_ms=4000),
        _event("response_first_audio", "call0009", latency_ms=3000),
        _event("model_turn_complete", "call0009", generated_audio_ms=9000),
        _event("barge_in_clear", "call0009", clear_ms=700),
        _event("inbound_audio_error", "call0009", exception_type="RuntimeError"),
        _event("receive_error", "call0009", exception_type="RuntimeError"),
        _event("reconnect_result", "call0009", success=False),
        _event("audio_backlog_overflow", "call0009", attempted_backlog_ms=12001),
        _event("outbound_audio_error", "call0009"),
    ])

    report = evaluate_voice_release(messages)
    failed = {gate["name"] for gate in report["gates"] if not gate["passed"]}

    assert report["status"] == "fail"
    assert {
        "first_audio_max_ms",
        "response_first_audio_max_ms",
        "generated_audio_max_ms",
        "barge_in_clear_max_ms",
        "inbound_audio_errors",
        "receive_errors",
        "reconnect_failures",
        "audio_backlog_overflows",
        "outbound_audio_errors",
    } <= failed


def test_voice_release_evaluator_rejects_insufficient_sample():
    thresholds = VoiceReleaseThresholds(
        min_calls=2,
        min_response_turns=2,
        min_barge_in_events=1,
    )
    messages = [
        _event("gemini_ws_connected", "onlycall", call_elapsed_ms=100),
        _event("greeting_instruction_sent", "onlycall", words=20),
        _event("first_outbound_audio", "onlycall", call_elapsed_ms=1000),
        _event("response_first_audio", "onlycall", latency_ms=500),
        _event("model_turn_complete", "onlycall", generated_audio_ms=2000),
    ]

    report = evaluate_voice_release(messages, thresholds=thresholds)
    failed = {gate["name"] for gate in report["gates"] if not gate["passed"]}

    assert report["status"] == "fail"
    assert {"minimum_calls", "minimum_response_turns", "minimum_barge_in_events"} <= failed
