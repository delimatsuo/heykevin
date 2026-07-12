"""Offline, aggregate-only calibration for deterministic caller activity."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from app.services import caller_activity
from app.services.caller_activity import (
    CallerActivityEvent,
    CallerActivityTracker,
    FrameClassifier,
)
from app.services.voice_turn_replay import (
    RenderedVoiceTurn,
    VoiceTurnReplayCase,
    render_voice_turn_case,
)


ClassifierFactory = Callable[[int], FrameClassifier]


@dataclass(frozen=True, slots=True)
class CallerActivityReplayThresholds:
    boundary_tolerance_ms: int = 150
    max_endpoint_confirmation_delay_ms: int = 500


def evaluate_caller_activity_replay(
    cases: Iterable[VoiceTurnReplayCase],
    *,
    mode: int = 2,
    min_speech_frames: int = 3,
    end_silence_frames: int = 15,
    classifier_factory: ClassifierFactory | None = None,
    thresholds: CallerActivityReplayThresholds | None = None,
) -> dict[str, Any]:
    """Score tracker segmentation and endpoint bias against labeled fixtures."""
    if mode not in {0, 1, 2, 3}:
        raise ValueError("mode must be between 0 and 3")
    limits = thresholds or CallerActivityReplayThresholds()
    if limits.boundary_tolerance_ms < 0:
        raise ValueError("boundary_tolerance_ms must be non-negative")
    if limits.max_endpoint_confirmation_delay_ms < 1:
        raise ValueError("confirmation delay limit must be positive")

    factory = classifier_factory or _webrtc_classifier_factory
    replay_cases = tuple(cases)
    single_segment_cases = 0
    false_pre_roll_starts = 0
    premature_end_events = 0
    missing_start_cases = 0
    missing_final_end_cases = 0
    covered_boundaries = 0
    start_absolute_errors = []
    final_end_absolute_errors = []

    for case in replay_cases:
        rendered = render_voice_turn_case(case)
        tracker = CallerActivityTracker(
            classifier=factory(mode),
            min_speech_frames=min_speech_frames,
            end_silence_frames=end_silence_frames,
        )
        events = _replay_tracker(tracker, rendered)
        starts_ms = [
            round(event.at * 1_000)
            for event in events
            if event.kind == "start"
        ]
        ends_ms = [
            round(event.at * 1_000)
            for event in events
            if event.kind == "end"
        ]
        if len(starts_ms) == 1 and len(ends_ms) == 1:
            single_segment_cases += 1

        earliest_valid_start = (
            rendered.speech_start_ms - limits.boundary_tolerance_ms
        )
        earliest_valid_end = (
            rendered.speech_end_ms - limits.boundary_tolerance_ms
        )
        false_pre_roll_starts += sum(
            start < earliest_valid_start for start in starts_ms
        )
        premature_end_events += sum(end < earliest_valid_end for end in ends_ms)

        start_candidates = [
            start
            for start in starts_ms
            if earliest_valid_start <= start <= rendered.speech_end_ms
        ]
        final_end_candidates = [
            end for end in ends_ms if end >= earliest_valid_end
        ]
        if start_candidates:
            matched_start = min(
                start_candidates,
                key=lambda value: abs(value - rendered.speech_start_ms),
            )
            start_absolute_errors.append(
                abs(matched_start - rendered.speech_start_ms)
            )
        else:
            missing_start_cases += 1
        if final_end_candidates:
            matched_end = min(
                final_end_candidates,
                key=lambda value: abs(value - rendered.speech_end_ms),
            )
            final_end_absolute_errors.append(
                abs(matched_end - rendered.speech_end_ms)
            )
        else:
            missing_final_end_cases += 1
        if start_candidates and final_end_candidates:
            covered_boundaries += 1

    case_count = len(replay_cases)
    boundary_coverage = covered_boundaries / case_count if case_count else 0.0
    endpoint_confirmation_delay_ms = end_silence_frames * 20
    diagnostics = {
        "single_segment_cases": single_segment_cases,
        "false_pre_roll_starts": false_pre_roll_starts,
        "premature_end_events": premature_end_events,
        "missing_start_cases": missing_start_cases,
        "missing_final_end_cases": missing_final_end_cases,
        "boundary_coverage_rate": round(boundary_coverage, 4),
        "start_absolute_error_p95_ms": _percentile(start_absolute_errors, 0.95),
        "start_absolute_error_max_ms": max(start_absolute_errors)
        if start_absolute_errors
        else None,
        "final_end_absolute_error_p95_ms": _percentile(
            final_end_absolute_errors,
            0.95,
        ),
        "final_end_absolute_error_max_ms": max(final_end_absolute_errors)
        if final_end_absolute_errors
        else None,
        "endpoint_confirmation_delay_ms": endpoint_confirmation_delay_ms,
    }
    gates = [
        _gate(
            "minimum_cases",
            case_count > 0,
            case_count,
            "> 0",
        ),
        _gate(
            "single_segment_coverage",
            single_segment_cases == case_count and case_count > 0,
            single_segment_cases,
            f"= {case_count}",
        ),
        _gate(
            "false_pre_roll_starts",
            false_pre_roll_starts == 0,
            false_pre_roll_starts,
            "= 0",
        ),
        _gate(
            "premature_end_events",
            premature_end_events == 0,
            premature_end_events,
            "= 0",
        ),
        _gate(
            "boundary_coverage",
            boundary_coverage == 1.0,
            round(boundary_coverage, 4),
            "= 1.0",
        ),
        _gate(
            "start_boundary_p95_ms",
            _at_most(
                diagnostics["start_absolute_error_p95_ms"],
                limits.boundary_tolerance_ms,
            ),
            diagnostics["start_absolute_error_p95_ms"],
            f"<= {limits.boundary_tolerance_ms}",
        ),
        _gate(
            "start_boundary_max_ms",
            _at_most(
                diagnostics["start_absolute_error_max_ms"],
                limits.boundary_tolerance_ms,
            ),
            diagnostics["start_absolute_error_max_ms"],
            f"<= {limits.boundary_tolerance_ms}",
        ),
        _gate(
            "final_end_boundary_p95_ms",
            _at_most(
                diagnostics["final_end_absolute_error_p95_ms"],
                limits.boundary_tolerance_ms,
            ),
            diagnostics["final_end_absolute_error_p95_ms"],
            f"<= {limits.boundary_tolerance_ms}",
        ),
        _gate(
            "final_end_boundary_max_ms",
            _at_most(
                diagnostics["final_end_absolute_error_max_ms"],
                limits.boundary_tolerance_ms,
            ),
            diagnostics["final_end_absolute_error_max_ms"],
            f"<= {limits.boundary_tolerance_ms}",
        ),
        _gate(
            "endpoint_confirmation_delay_ms",
            endpoint_confirmation_delay_ms
            <= limits.max_endpoint_confirmation_delay_ms,
            endpoint_confirmation_delay_ms,
            f"<= {limits.max_endpoint_confirmation_delay_ms}",
        ),
    ]
    return {
        "status": "pass" if all(gate["passed"] for gate in gates) else "fail",
        "sample": {"cases": case_count},
        "diagnostics": diagnostics,
        "gates": gates,
    }


def _webrtc_classifier_factory(mode: int) -> FrameClassifier:
    if caller_activity.webrtcvad is None:
        raise RuntimeError("WebRTC VAD is unavailable")
    return caller_activity.webrtcvad.Vad(mode).is_speech


def _replay_tracker(
    tracker: CallerActivityTracker,
    rendered: RenderedVoiceTurn,
) -> tuple[CallerActivityEvent, ...]:
    events = []
    position = 0
    pattern_index = 0
    while position < len(rendered.mulaw8):
        chunk_ms = rendered.frame_pattern_ms[
            pattern_index % len(rendered.frame_pattern_ms)
        ]
        pattern_index += 1
        chunk = rendered.mulaw8[position : position + chunk_ms * 8]
        position += len(chunk)
        events.extend(
            tracker.process_mulaw(
                chunk,
                received_at=position / 8 / 1_000,
            )
        )
    return tuple(events)


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, (round(percentile * 100) * len(ordered) + 99) // 100 - 1)
    return ordered[index]


def _at_most(value: object, limit: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value <= limit


def _gate(
    name: str,
    passed: bool,
    observed: object,
    requirement: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "observed": observed,
        "requirement": requirement,
    }
