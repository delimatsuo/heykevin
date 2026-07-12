#!/usr/bin/env python3
"""Evaluate aggregate Gemini voice timing logs against staging release gates.

Input may be a Cloud Logging JSON export, JSON Lines, or plain log messages.
Output contains aggregate counts and timing values only; raw messages and call
labels are never printed.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


VOICE_TIMING_PREFIX = "voice_timing "
METRIC_PATTERN = re.compile(r"\b([a-z][a-z0-9_]*)=([^\s]+)")


@dataclass(frozen=True)
class VoiceReleaseThresholds:
    """Default certification thresholds for a staging voice canary set."""

    min_calls: int = 10
    min_response_turns: int = 30
    min_barge_in_events: int = 5
    startup_completion_rate: float = 1.0
    greeting_coverage_rate: float = 1.0
    inbound_media_ready_coverage_rate: float = 1.0
    first_inbound_audio_coverage_rate: float = 1.0
    caller_transcript_coverage_rate: float = 1.0
    validated_response_latency_coverage_rate: float = 1.0
    response_completion_rate: float = 1.0
    response_terminal_rate: float = 1.0
    greeting_max_words: int = 24
    inbound_media_ready_p95_ms: int = 2000
    inbound_media_ready_max_ms: int = 3000
    first_inbound_audio_p95_ms: int = 2000
    first_inbound_audio_max_ms: int = 3000
    first_audio_p95_ms: int = 2500
    first_audio_max_ms: int = 3500
    response_first_audio_p95_ms: int = 1500
    response_first_audio_max_ms: int = 2500
    generated_audio_p95_ms: int = 6000
    generated_audio_max_ms: int = 8000
    barge_in_clear_p95_ms: int = 250
    barge_in_clear_max_ms: int = 500
    max_barge_in_clear_failures: int = 0
    max_inbound_audio_errors: int = 0
    max_inbound_media_buffer_overflows: int = 0
    max_inbound_reconnect_audio_overflows: int = 0
    max_receive_errors: int = 0
    max_reconnect_failures: int = 0
    max_audio_backlog_overflows: int = 0
    max_outbound_audio_errors: int = 0
    max_out_of_cohort_events: int = 0
    max_evidence_integrity_errors: int = 0


def _messages_from_json(value: object) -> list[str]:
    if isinstance(value, list):
        messages: list[str] = []
        for item in value:
            messages.extend(_messages_from_json(item))
        return messages
    if isinstance(value, str):
        return [value]
    if not isinstance(value, dict):
        return []

    messages = []
    text_payload = value.get("textPayload")
    if isinstance(text_payload, str):
        messages.append(text_payload)

    json_payload = value.get("jsonPayload")
    if isinstance(json_payload, dict):
        message = json_payload.get("message")
        if isinstance(message, str):
            messages.append(message)

    direct_message = value.get("message")
    if isinstance(direct_message, str):
        messages.append(direct_message)
    return messages


def extract_log_messages(raw: str) -> list[str]:
    """Extract only explicit log message fields from supported export formats."""
    stripped = raw.strip()
    if not stripped:
        return []

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        messages = []
        for line in stripped.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            try:
                parsed_line = json.loads(candidate)
            except json.JSONDecodeError:
                messages.append(candidate)
            else:
                messages.extend(_messages_from_json(parsed_line))
        return messages
    return _messages_from_json(parsed)


def _coerce_metric(value: str) -> int | float | bool | str:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def parse_voice_timing_event(message: str) -> dict[str, object] | None:
    """Parse one payload-free voice timing message into typed metadata."""
    prefix_index = message.find(VOICE_TIMING_PREFIX)
    if prefix_index < 0:
        return None
    timing_text = message[prefix_index + len(VOICE_TIMING_PREFIX):]
    event = {
        key: _coerce_metric(value)
        for key, value in METRIC_PATTERN.findall(timing_text)
    }
    if not isinstance(event.get("event"), str) or not isinstance(event.get("call"), str):
        return None
    return event


def _event_key(
    event: dict[str, object],
    ordinal: str,
) -> tuple[str, str] | None:
    value = event.get(ordinal)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return str(event["call"]), str(value)


def _event_keys(
    events: Iterable[dict[str, object]],
    ordinal: str,
) -> set[tuple[str, str]]:
    return {
        key
        for event in events
        if (key := _event_key(event, ordinal)) is not None
    }


def _worst_numeric_values(
    events: Iterable[dict[str, object]],
    metric: str,
    *,
    ordinal: str | None = None,
) -> list[int]:
    worst_by_event: dict[tuple[str, ...], int] = {}
    for event in events:
        identity = (
            _event_key(event, ordinal)
            if ordinal is not None
            else (str(event["call"]),)
        )
        value = event.get(metric)
        if identity is None or isinstance(value, bool):
            continue
        if not isinstance(value, (int, float)):
            continue
        numeric_value = int(value)
        if numeric_value < 0:
            continue
        worst_by_event[identity] = max(
            worst_by_event.get(identity, numeric_value),
            numeric_value,
        )
    return list(worst_by_event.values())


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _gate(name: str, passed: bool, observed: object, requirement: str) -> dict[str, object]:
    return {
        "name": name,
        "passed": passed,
        "observed": observed,
        "requirement": requirement,
    }


def _at_most_gate(name: str, values: list[int], limit: int, percentile: float) -> dict[str, object]:
    observed = _percentile(values, percentile)
    return _gate(
        name,
        observed is not None and observed <= limit,
        observed,
        f"p{int(percentile * 100)} <= {limit}",
    )


def _max_gate(name: str, values: list[int], limit: int) -> dict[str, object]:
    observed = max(values) if values else None
    return _gate(name, observed is not None and observed <= limit, observed, f"<= {limit}")


def _turn_keys(events: Iterable[dict[str, object]]) -> set[tuple[str, str]]:
    return _event_keys(events, "turn")


def _events_for_calls(
    events: Iterable[dict[str, object]],
    calls: set[str],
) -> list[dict[str, object]]:
    return [event for event in events if str(event["call"]) in calls]


def evaluate_voice_release(
    messages: Iterable[str],
    *,
    thresholds: VoiceReleaseThresholds | None = None,
) -> dict[str, object]:
    """Return an aggregate pass/fail report without raw call identifiers."""
    limits = thresholds or VoiceReleaseThresholds()
    events = [
        event
        for message in messages
        if (event := parse_voice_timing_event(message)) is not None
    ]

    by_name: dict[str, list[dict[str, object]]] = {}
    for event in events:
        by_name.setdefault(str(event["event"]), []).append(event)

    attempted_calls = {
        str(event["call"])
        for event in by_name.get("gemini_ws_connected", [])
    }
    out_of_cohort_events = sum(
        1
        for event in events
        if event["event"] != "gemini_ws_connected"
        and str(event["call"]) not in attempted_calls
    )
    first_audio_events = _events_for_calls(
        by_name.get("first_outbound_audio", []),
        attempted_calls,
    )
    first_audio_calls = {
        str(event["call"])
        for event in first_audio_events
    }
    greeting_events = _events_for_calls(
        by_name.get("greeting_instruction_sent", []),
        attempted_calls,
    )
    greeting_calls = {
        str(event["call"])
        for event in greeting_events
    }
    inbound_media_ready_events = _events_for_calls(
        by_name.get("inbound_media_ready", []),
        attempted_calls,
    )
    inbound_media_ready_calls = {
        str(event["call"])
        for event in inbound_media_ready_events
    }
    first_inbound_audio_events = _events_for_calls(
        by_name.get("first_inbound_audio_forwarded", []),
        attempted_calls,
    )
    first_inbound_audio_calls = {
        str(event["call"])
        for event in first_inbound_audio_events
    }
    caller_transcript_events = _events_for_calls(
        by_name.get("first_caller_transcript", []),
        attempted_calls,
    )
    caller_transcript_calls = {
        str(event["call"])
        for event in caller_transcript_events
    }
    response_events = _events_for_calls(
        by_name.get("response_first_audio", []),
        attempted_calls,
    )
    completion_events = _events_for_calls(
        by_name.get("model_turn_complete", []),
        attempted_calls,
    )
    interrupted_response_events = _events_for_calls(
        by_name.get("model_turn_interrupted", []),
        attempted_calls,
    )
    response_turn_keys = _turn_keys(response_events)
    completion_turn_keys = _turn_keys(completion_events)
    interrupted_turn_keys = _turn_keys(interrupted_response_events)
    completed_response_turn_keys = response_turn_keys & completion_turn_keys
    interrupted_response_turn_keys = response_turn_keys & interrupted_turn_keys
    terminal_response_turn_keys = response_turn_keys & (
        completion_turn_keys | interrupted_turn_keys
    )
    non_interrupted_turn_keys = response_turn_keys - interrupted_response_turn_keys
    completed_non_interrupted_turn_keys = (
        non_interrupted_turn_keys & completion_turn_keys
    )
    matched_completion_events = [
        event
        for event in completion_events
        if _event_key(event, "turn") in completed_response_turn_keys
    ]
    matched_interrupted_response_events = [
        event
        for event in interrupted_response_events
        if _event_key(event, "turn") in interrupted_response_turn_keys
    ]
    barge_events = _events_for_calls(
        by_name.get("barge_in_clear", []),
        attempted_calls,
    )
    all_barge_failure_events = by_name.get("barge_in_clear_failed", [])
    barge_failure_events = _events_for_calls(
        all_barge_failure_events,
        attempted_calls,
    )
    barge_event_keys = _event_keys(barge_events, "barge")
    barge_failure_keys = _event_keys(barge_failure_events, "barge")
    barge_in_attempts = len(barge_event_keys | barge_failure_keys)
    reconnect_clear_events = _events_for_calls(
        by_name.get("reconnect_output_clear", []),
        attempted_calls,
    )
    reconnect_result_events = _events_for_calls(
        by_name.get("reconnect_result", []),
        attempted_calls,
    )

    startup_rate = (
        len(first_audio_calls & attempted_calls) / len(attempted_calls)
        if attempted_calls
        else 0.0
    )
    greeting_rate = (
        len(first_audio_calls & greeting_calls) / len(first_audio_calls)
        if first_audio_calls
        else 0.0
    )
    inbound_media_ready_rate = (
        len(attempted_calls & inbound_media_ready_calls) / len(attempted_calls)
        if attempted_calls
        else 0.0
    )
    first_inbound_audio_rate = (
        len(attempted_calls & first_inbound_audio_calls) / len(attempted_calls)
        if attempted_calls
        else 0.0
    )
    caller_transcript_rate = (
        len(attempted_calls & caller_transcript_calls) / len(attempted_calls)
        if attempted_calls
        else 0.0
    )
    response_completion_rate = (
        len(completed_non_interrupted_turn_keys) / len(non_interrupted_turn_keys)
        if non_interrupted_turn_keys
        else (1.0 if response_turn_keys else 0.0)
    )
    response_terminal_rate = (
        len(terminal_response_turn_keys) / len(response_turn_keys)
        if response_turn_keys
        else 0.0
    )
    reconnect_attempts = max(len(reconnect_clear_events), len(reconnect_result_events))
    reconnect_failures = sum(
        event.get("success") is not True for event in reconnect_result_events
    )

    first_audio_values = _worst_numeric_values(
        first_audio_events,
        "call_elapsed_ms",
    )
    inbound_media_ready_values = _worst_numeric_values(
        inbound_media_ready_events,
        "call_elapsed_ms",
    )
    first_inbound_audio_values = _worst_numeric_values(
        first_inbound_audio_events,
        "elapsed_ms",
    )
    response_values = _worst_numeric_values(
        response_events,
        "speech_end_to_first_audio_ms",
        ordinal="turn",
    )
    transcript_to_audio_values = _worst_numeric_values(
        response_events,
        "transcript_to_audio_ms",
        ordinal="turn",
    )
    validated_response_latency_rate = (
        len(response_values) / len(response_turn_keys)
        if response_turn_keys
        else 0.0
    )
    generated_audio_values = _worst_numeric_values(
        matched_completion_events,
        "generated_audio_ms",
        ordinal="turn",
    )
    barge_clear_values = _worst_numeric_values(
        barge_events,
        "clear_ms",
        ordinal="barge",
    )
    greeting_word_values = _worst_numeric_values(
        greeting_events,
        "words",
    )
    caller_transcript_time_values = _worst_numeric_values(
        caller_transcript_events,
        "call_elapsed_ms",
    )
    completed_model_stream_values = _worst_numeric_values(
        matched_completion_events,
        "model_stream_ms",
        ordinal="turn",
    )
    interrupted_generated_audio_values = _worst_numeric_values(
        matched_interrupted_response_events,
        "generated_audio_ms",
        ordinal="turn",
    )
    interrupted_model_stream_values = _worst_numeric_values(
        matched_interrupted_response_events,
        "model_stream_ms",
        ordinal="turn",
    )
    unidentified_response_events = sum(
        _event_key(event, "turn") is None
        for event in (
            response_events
            + completion_events
            + interrupted_response_events
        )
    )
    unidentified_barge_events = sum(
        _event_key(event, "barge") is None
        for event in (barge_events + barge_failure_events)
    )
    unmatched_terminal_turns = len(
        (completion_turn_keys | interrupted_turn_keys) - response_turn_keys
    )
    conflicting_terminal_turns = len(
        completion_turn_keys & interrupted_turn_keys
    )
    missing_metric_evidence = sum([
        len(first_audio_calls) - len(first_audio_values),
        len(greeting_calls) - len(greeting_word_values),
        len(inbound_media_ready_calls) - len(inbound_media_ready_values),
        len(first_inbound_audio_calls) - len(first_inbound_audio_values),
        len(caller_transcript_calls) - len(caller_transcript_time_values),
        len(response_turn_keys) - len(response_values),
        len(completed_response_turn_keys) - len(generated_audio_values),
        len(completed_response_turn_keys) - len(completed_model_stream_values),
        (
            len(interrupted_response_turn_keys)
            - len(interrupted_generated_audio_values)
        ),
        (
            len(interrupted_response_turn_keys)
            - len(interrupted_model_stream_values)
        ),
        len(barge_event_keys) - len(barge_clear_values),
    ])
    evidence_integrity_errors = (
        unidentified_response_events
        + unidentified_barge_events
        + unmatched_terminal_turns
        + conflicting_terminal_turns
        + missing_metric_evidence
    )
    inbound_errors = len(by_name.get("inbound_audio_error", []))
    inbound_media_buffer_overflows = len(
        by_name.get("inbound_media_buffer_overflow", [])
    )
    inbound_reconnect_audio_overflows = len(
        by_name.get("inbound_reconnect_audio_overflow", [])
    )
    receive_errors = len(by_name.get("receive_error", []))
    audio_backlog_overflows = len(by_name.get("audio_backlog_overflow", []))
    outbound_audio_errors = len(by_name.get("outbound_audio_error", []))

    gates = [
        _gate(
            "minimum_calls",
            len(first_audio_calls) >= limits.min_calls,
            len(first_audio_calls),
            f">= {limits.min_calls}",
        ),
        _gate(
            "out_of_cohort_events",
            out_of_cohort_events <= limits.max_out_of_cohort_events,
            out_of_cohort_events,
            f"<= {limits.max_out_of_cohort_events}",
        ),
        _gate(
            "evidence_integrity_errors",
            evidence_integrity_errors <= limits.max_evidence_integrity_errors,
            evidence_integrity_errors,
            f"<= {limits.max_evidence_integrity_errors}",
        ),
        _gate(
            "startup_completion_rate",
            startup_rate >= limits.startup_completion_rate,
            round(startup_rate, 4),
            f">= {limits.startup_completion_rate}",
        ),
        _gate(
            "greeting_coverage_rate",
            greeting_rate >= limits.greeting_coverage_rate,
            round(greeting_rate, 4),
            f">= {limits.greeting_coverage_rate}",
        ),
        _gate(
            "inbound_media_ready_coverage_rate",
            inbound_media_ready_rate >= limits.inbound_media_ready_coverage_rate,
            round(inbound_media_ready_rate, 4),
            f">= {limits.inbound_media_ready_coverage_rate}",
        ),
        _max_gate(
            "greeting_max_words",
            greeting_word_values,
            limits.greeting_max_words,
        ),
        _at_most_gate(
            "inbound_media_ready_p95_ms",
            inbound_media_ready_values,
            limits.inbound_media_ready_p95_ms,
            0.95,
        ),
        _max_gate(
            "inbound_media_ready_max_ms",
            inbound_media_ready_values,
            limits.inbound_media_ready_max_ms,
        ),
        _gate(
            "first_inbound_audio_coverage_rate",
            first_inbound_audio_rate >= limits.first_inbound_audio_coverage_rate,
            round(first_inbound_audio_rate, 4),
            f">= {limits.first_inbound_audio_coverage_rate}",
        ),
        _at_most_gate(
            "first_inbound_audio_p95_ms",
            first_inbound_audio_values,
            limits.first_inbound_audio_p95_ms,
            0.95,
        ),
        _max_gate(
            "first_inbound_audio_max_ms",
            first_inbound_audio_values,
            limits.first_inbound_audio_max_ms,
        ),
        _gate(
            "caller_transcript_coverage_rate",
            caller_transcript_rate >= limits.caller_transcript_coverage_rate,
            round(caller_transcript_rate, 4),
            f">= {limits.caller_transcript_coverage_rate}",
        ),
        _gate(
            "minimum_response_turns",
            len(response_turn_keys) >= limits.min_response_turns,
            len(response_turn_keys),
            f">= {limits.min_response_turns}",
        ),
        _gate(
            "validated_response_latency_coverage_rate",
            (
                validated_response_latency_rate
                >= limits.validated_response_latency_coverage_rate
            ),
            round(validated_response_latency_rate, 4),
            f">= {limits.validated_response_latency_coverage_rate}",
        ),
        _gate(
            "response_completion_rate",
            response_completion_rate >= limits.response_completion_rate,
            round(response_completion_rate, 4),
            f">= {limits.response_completion_rate}",
        ),
        _gate(
            "response_terminal_rate",
            response_terminal_rate >= limits.response_terminal_rate,
            round(response_terminal_rate, 4),
            f">= {limits.response_terminal_rate}",
        ),
        _gate(
            "minimum_barge_in_events",
            barge_in_attempts >= limits.min_barge_in_events,
            barge_in_attempts,
            f">= {limits.min_barge_in_events}",
        ),
        _at_most_gate(
            "first_audio_p95_ms",
            first_audio_values,
            limits.first_audio_p95_ms,
            0.95,
        ),
        _max_gate("first_audio_max_ms", first_audio_values, limits.first_audio_max_ms),
        _at_most_gate(
            "response_first_audio_p95_ms",
            response_values,
            limits.response_first_audio_p95_ms,
            0.95,
        ),
        _max_gate(
            "response_first_audio_max_ms",
            response_values,
            limits.response_first_audio_max_ms,
        ),
        _at_most_gate(
            "generated_audio_p95_ms",
            generated_audio_values,
            limits.generated_audio_p95_ms,
            0.95,
        ),
        _max_gate(
            "generated_audio_max_ms",
            generated_audio_values,
            limits.generated_audio_max_ms,
        ),
        _at_most_gate(
            "barge_in_clear_p95_ms",
            barge_clear_values,
            limits.barge_in_clear_p95_ms,
            0.95,
        ),
        _max_gate(
            "barge_in_clear_max_ms",
            barge_clear_values,
            limits.barge_in_clear_max_ms,
        ),
        _gate(
            "barge_in_clear_failures",
            len(all_barge_failure_events) <= limits.max_barge_in_clear_failures,
            len(all_barge_failure_events),
            f"<= {limits.max_barge_in_clear_failures}",
        ),
        _gate(
            "inbound_audio_errors",
            inbound_errors <= limits.max_inbound_audio_errors,
            inbound_errors,
            f"<= {limits.max_inbound_audio_errors}",
        ),
        _gate(
            "inbound_media_buffer_overflows",
            (
                inbound_media_buffer_overflows
                <= limits.max_inbound_media_buffer_overflows
            ),
            inbound_media_buffer_overflows,
            f"<= {limits.max_inbound_media_buffer_overflows}",
        ),
        _gate(
            "inbound_reconnect_audio_overflows",
            (
                inbound_reconnect_audio_overflows
                <= limits.max_inbound_reconnect_audio_overflows
            ),
            inbound_reconnect_audio_overflows,
            f"<= {limits.max_inbound_reconnect_audio_overflows}",
        ),
        _gate(
            "receive_errors",
            receive_errors <= limits.max_receive_errors,
            receive_errors,
            f"<= {limits.max_receive_errors}",
        ),
        _gate(
            "reconnect_result_coverage",
            len(reconnect_result_events) >= reconnect_attempts,
            len(reconnect_result_events),
            f">= {reconnect_attempts}",
        ),
        _gate(
            "reconnect_failures",
            reconnect_failures <= limits.max_reconnect_failures,
            reconnect_failures,
            f"<= {limits.max_reconnect_failures}",
        ),
        _gate(
            "audio_backlog_overflows",
            audio_backlog_overflows <= limits.max_audio_backlog_overflows,
            audio_backlog_overflows,
            f"<= {limits.max_audio_backlog_overflows}",
        ),
        _gate(
            "outbound_audio_errors",
            outbound_audio_errors <= limits.max_outbound_audio_errors,
            outbound_audio_errors,
            f"<= {limits.max_outbound_audio_errors}",
        ),
    ]

    return {
        "status": "pass" if all(gate["passed"] for gate in gates) else "fail",
        "sample": {
            "attempted_calls": len(attempted_calls),
            "calls_with_first_audio": len(first_audio_calls),
            "response_turns": len(response_turn_keys),
            "barge_in_events": barge_in_attempts,
            "barge_in_clear_failures": len(all_barge_failure_events),
            "reconnect_attempts": reconnect_attempts,
            "out_of_cohort_events": out_of_cohort_events,
            "evidence_integrity_errors": evidence_integrity_errors,
            "calls_with_inbound_media_ready": len(
                inbound_media_ready_calls & attempted_calls
            ),
            "calls_with_first_inbound_audio": len(
                first_inbound_audio_calls & attempted_calls
            ),
            "calls_with_caller_transcript": len(
                caller_transcript_calls & attempted_calls
            ),
            "response_turns_with_valid_latency": len(response_values),
            "completed_response_turns": len(completed_response_turn_keys),
            "interrupted_response_turns": len(interrupted_response_turn_keys),
            "terminal_response_turns": len(terminal_response_turn_keys),
            "inbound_media_buffer_overflows": inbound_media_buffer_overflows,
            "inbound_reconnect_audio_overflows": inbound_reconnect_audio_overflows,
            "receive_errors": receive_errors,
            "audio_backlog_overflows": audio_backlog_overflows,
            "outbound_audio_errors": outbound_audio_errors,
        },
        "diagnostics": {
            "transcript_to_audio_p95_ms": _percentile(
                transcript_to_audio_values,
                0.95,
            ),
            "transcript_to_audio_max_ms": (
                max(transcript_to_audio_values)
                if transcript_to_audio_values
                else None
            ),
        },
        "gates": gates,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Cloud Logging JSON/plain export path, or - for stdin",
    )
    parser.add_argument("--min-calls", type=int, default=10)
    parser.add_argument("--min-response-turns", type=int, default=30)
    parser.add_argument("--min-barge-in-events", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
    thresholds = VoiceReleaseThresholds(
        min_calls=args.min_calls,
        min_response_turns=args.min_response_turns,
        min_barge_in_events=args.min_barge_in_events,
    )
    report = evaluate_voice_release(extract_log_messages(raw), thresholds=thresholds)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
