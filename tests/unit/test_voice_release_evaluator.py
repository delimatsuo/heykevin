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
            _event("inbound_media_ready", call, call_elapsed_ms=800),
            _event("first_inbound_audio_forwarded", call, elapsed_ms=850),
            _event(
                "first_caller_transcript",
                call,
                call_elapsed_ms=1200,
                unsafe_payload="private-caller-sentinel",
            ),
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
            messages.append(_event("barge_in_clear", call, barge=1, clear_ms=90))
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
        "barge_in_clear_failures": 0,
        "reconnect_attempts": 0,
        "out_of_cohort_events": 0,
        "evidence_integrity_errors": 0,
        "calls_with_inbound_media_ready": 10,
        "calls_with_first_inbound_audio": 10,
        "calls_with_caller_transcript": 10,
        "completed_response_turns": 30,
        "interrupted_response_turns": 0,
        "terminal_response_turns": 30,
        "inbound_media_buffer_overflows": 0,
        "inbound_reconnect_audio_overflows": 0,
        "receive_errors": 0,
        "audio_backlog_overflows": 0,
        "outbound_audio_errors": 0,
    }
    assert all(gate["passed"] for gate in report["gates"])
    serialized = json.dumps(report)
    assert "call0001" not in serialized
    assert "private-caller-sentinel" not in serialized


def test_voice_release_evaluator_fails_any_barge_in_clear_failure():
    messages = _passing_messages()
    messages.append(
        _event(
            "barge_in_clear_failed",
            "call0009",
            barge=1,
            reason="delivery_rejected",
        )
    )

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert gates["barge_in_clear_failures"]["observed"] == 1
    assert not gates["barge_in_clear_failures"]["passed"]


def test_voice_release_evaluator_fails_any_reconnect_audio_overflow():
    messages = _passing_messages()
    messages.append(
        _event(
            "inbound_reconnect_audio_overflow",
            "call0009",
            attempted_ms=12001,
        )
    )

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert gates["inbound_reconnect_audio_overflows"]["observed"] == 1
    assert not gates["inbound_reconnect_audio_overflows"]["passed"]


def test_voice_release_evaluator_fails_any_startup_media_overflow():
    messages = _passing_messages()
    messages.append(
        _event(
            "inbound_media_buffer_overflow",
            "call0009",
            attempted_audio_ms=12001,
        )
    )

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert gates["inbound_media_buffer_overflows"]["observed"] == 1
    assert not gates["inbound_media_buffer_overflows"]["passed"]


def test_voice_release_evaluator_rejects_missing_media_readiness():
    messages = [
        message
        for message in _passing_messages()
        if not (
            "event=inbound_media_ready" in message
            and "call=call0009" in message
        )
    ]

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert gates["inbound_media_ready_coverage_rate"]["observed"] == 0.9
    assert not gates["inbound_media_ready_coverage_rate"]["passed"]


def test_voice_release_evaluator_rejects_slow_media_readiness():
    messages = _passing_messages()
    messages.append(
        _event("inbound_media_ready", "call0009", call_elapsed_ms=3501)
    )

    report = evaluate_voice_release(messages)
    failed = {gate["name"] for gate in report["gates"] if not gate["passed"]}

    assert report["status"] == "fail"
    assert {
        "inbound_media_ready_p95_ms",
        "inbound_media_ready_max_ms",
    } <= failed


def test_voice_release_evaluator_rejects_missing_first_inbound_forward():
    messages = [
        message
        for message in _passing_messages()
        if not (
            "event=first_inbound_audio_forwarded" in message
            and "call=call0009" in message
        )
    ]

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert gates["first_inbound_audio_coverage_rate"]["observed"] == 0.9
    assert not gates["first_inbound_audio_coverage_rate"]["passed"]


def test_voice_release_evaluator_rejects_slow_first_inbound_forward():
    messages = _passing_messages()
    messages.append(
        _event("first_inbound_audio_forwarded", "call0009", elapsed_ms=3501)
    )

    report = evaluate_voice_release(messages)
    failed = {gate["name"] for gate in report["gates"] if not gate["passed"]}

    assert report["status"] == "fail"
    assert {
        "first_inbound_audio_p95_ms",
        "first_inbound_audio_max_ms",
    } <= failed


def test_voice_release_evaluator_rejects_missing_caller_transcript():
    messages = [
        message
        for message in _passing_messages()
        if not (
            "event=first_caller_transcript" in message
            and "call=call0009" in message
        )
    ]

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert gates["caller_transcript_coverage_rate"]["observed"] == 0.9
    assert not gates["caller_transcript_coverage_rate"]["passed"]


def test_voice_release_evaluator_rejects_missing_response_completion():
    messages = [
        message
        for message in _passing_messages()
        if not (
            "event=model_turn_complete" in message
            and "call=call0009" in message
            and "turn=3" in message
        )
    ]

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert gates["response_completion_rate"]["observed"] == 0.9667
    assert not gates["response_completion_rate"]["passed"]
    assert gates["response_terminal_rate"]["observed"] == 0.9667
    assert not gates["response_terminal_rate"]["passed"]


def test_voice_release_evaluator_accepts_interrupted_response_terminal():
    messages = [
        message
        for message in _passing_messages()
        if not (
            "event=model_turn_complete" in message
            and "call=call0000" in message
            and "turn=1" in message
        )
    ]
    messages.append(
        _event(
            "model_turn_interrupted",
            "call0000",
            turn=1,
            model_stream_ms=250,
            generated_audio_ms=300,
        )
    )

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "pass"
    assert gates["response_completion_rate"]["observed"] == 1.0
    assert gates["response_terminal_rate"]["observed"] == 1.0
    assert report["sample"]["completed_response_turns"] == 29
    assert report["sample"]["interrupted_response_turns"] == 1
    assert report["sample"]["terminal_response_turns"] == 30


def test_voice_release_evaluator_counts_unique_response_turns():
    messages = _passing_messages()
    duplicate_response = _event(
        "response_first_audio",
        "call0000",
        turn=1,
        latency_ms=1100,
    )
    duplicate_completion = _event(
        "model_turn_complete",
        "call0000",
        turn=1,
        generated_audio_ms=4200,
    )
    messages.extend([duplicate_response, duplicate_completion] * 30)

    report = evaluate_voice_release(messages)

    assert report["status"] == "pass"
    assert report["sample"]["response_turns"] == 30
    assert report["sample"]["completed_response_turns"] == 30


def test_voice_release_evaluator_uses_worst_latency_per_response_turn():
    slow_calls = {"call0008", "call0009"}
    messages = [
        message
        for message in _passing_messages()
        if not (
            "event=response_first_audio" in message
            and any(f"call={call}" in message for call in slow_calls)
            and "turn=3" in message
        )
    ]
    messages.extend(
        _event("response_first_audio", call, turn=3, latency_ms=2000)
        for call in slow_calls
    )
    messages.extend([
        _event("response_first_audio", "call0000", turn=1, latency_ms=100)
    ] * 100)

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert gates["response_first_audio_p95_ms"]["observed"] == 2000
    assert not gates["response_first_audio_p95_ms"]["passed"]


def test_voice_release_evaluator_rejects_missing_timing_metric():
    messages = [
        message
        for message in _passing_messages()
        if not (
            "event=response_first_audio" in message
            and "call=call0009" in message
            and "turn=3" in message
        )
    ]
    messages.append(_event("response_first_audio", "call0009", turn=3))

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert gates["evidence_integrity_errors"]["observed"] == 1
    assert not gates["evidence_integrity_errors"]["passed"]


def test_voice_release_evaluator_does_not_count_duplicate_barge_ins():
    messages = [
        message
        for message in _passing_messages()
        if "event=barge_in_clear" not in message
    ]
    duplicate = _event("barge_in_clear", "call0000", barge=1, clear_ms=10)
    messages.extend([duplicate] * 5)

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert gates["minimum_barge_in_events"]["observed"] == 1


def test_voice_release_evaluator_rejects_out_of_cohort_response_turns():
    messages = [
        message
        for message in _passing_messages()
        if not (
            ("event=response_first_audio" in message or "event=model_turn_complete" in message)
            and ("turn=2" in message or "turn=3" in message)
        )
    ]
    for orphan_number in range(20):
        orphan_call = f"orphan{orphan_number:04d}"
        messages.extend([
            _event("response_first_audio", orphan_call, turn=1, latency_ms=100),
            _event(
                "model_turn_complete",
                orphan_call,
                turn=1,
                generated_audio_ms=100,
            ),
        ])

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert report["sample"]["response_turns"] == 10
    assert gates["minimum_response_turns"]["observed"] == 10
    assert gates["out_of_cohort_events"]["observed"] == 40
    assert not gates["out_of_cohort_events"]["passed"]


def test_voice_release_evaluator_rejects_out_of_cohort_barge_ins():
    messages = [
        message
        for message in _passing_messages()
        if "event=barge_in_clear" not in message
    ]
    messages.extend(
        _event(
            "barge_in_clear",
            f"orphan{number:04d}",
            barge=1,
            clear_ms=10,
        )
        for number in range(5)
    )

    report = evaluate_voice_release(messages)
    gates = {gate["name"]: gate for gate in report["gates"]}

    assert report["status"] == "fail"
    assert gates["minimum_barge_in_events"]["observed"] == 0
    assert gates["out_of_cohort_events"]["observed"] == 5


def test_voice_release_evaluator_fails_slow_verbose_and_error_sample():
    messages = _passing_messages()
    messages.extend([
        _event("first_outbound_audio", "call0009", call_elapsed_ms=4000),
        _event("response_first_audio", "call0009", turn=3, latency_ms=3000),
        _event(
            "model_turn_complete",
            "call0009",
            turn=3,
            generated_audio_ms=9000,
        ),
        _event("barge_in_clear", "call0009", barge=1, clear_ms=700),
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
