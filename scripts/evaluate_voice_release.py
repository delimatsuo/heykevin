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
    greeting_max_words: int = 24
    first_audio_p95_ms: int = 2500
    first_audio_max_ms: int = 3500
    response_first_audio_p95_ms: int = 1500
    response_first_audio_max_ms: int = 2500
    generated_audio_p95_ms: int = 6000
    generated_audio_max_ms: int = 8000
    barge_in_clear_p95_ms: int = 250
    barge_in_clear_max_ms: int = 500
    max_inbound_audio_errors: int = 0
    max_receive_errors: int = 0
    max_reconnect_failures: int = 0
    max_audio_backlog_overflows: int = 0
    max_outbound_audio_errors: int = 0


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


def _numeric_values(events: Iterable[dict[str, object]], key: str) -> list[int]:
    values = []
    for event in events:
        value = event.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            values.append(int(value))
    return values


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
    first_audio_calls = {
        str(event["call"])
        for event in by_name.get("first_outbound_audio", [])
    }
    greeting_calls = {
        str(event["call"])
        for event in by_name.get("greeting_instruction_sent", [])
    }
    response_events = by_name.get("response_first_audio", [])
    completion_events = by_name.get("model_turn_complete", [])
    barge_events = by_name.get("barge_in_clear", [])
    reconnect_clear_events = by_name.get("reconnect_output_clear", [])
    reconnect_result_events = by_name.get("reconnect_result", [])

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
    reconnect_attempts = max(len(reconnect_clear_events), len(reconnect_result_events))
    reconnect_failures = sum(
        event.get("success") is not True for event in reconnect_result_events
    )

    first_audio_values = _numeric_values(
        by_name.get("first_outbound_audio", []),
        "call_elapsed_ms",
    )
    response_values = _numeric_values(response_events, "latency_ms")
    generated_audio_values = _numeric_values(completion_events, "generated_audio_ms")
    barge_clear_values = _numeric_values(barge_events, "clear_ms")
    greeting_word_values = _numeric_values(
        by_name.get("greeting_instruction_sent", []),
        "words",
    )
    inbound_errors = len(by_name.get("inbound_audio_error", []))
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
        _max_gate(
            "greeting_max_words",
            greeting_word_values,
            limits.greeting_max_words,
        ),
        _gate(
            "minimum_response_turns",
            len(response_events) >= limits.min_response_turns,
            len(response_events),
            f">= {limits.min_response_turns}",
        ),
        _gate(
            "minimum_barge_in_events",
            len(barge_events) >= limits.min_barge_in_events,
            len(barge_events),
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
            "inbound_audio_errors",
            inbound_errors <= limits.max_inbound_audio_errors,
            inbound_errors,
            f"<= {limits.max_inbound_audio_errors}",
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
            "response_turns": len(response_events),
            "barge_in_events": len(barge_events),
            "reconnect_attempts": reconnect_attempts,
            "receive_errors": receive_errors,
            "audio_backlog_overflows": audio_backlog_overflows,
            "outbound_audio_errors": outbound_audio_errors,
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
