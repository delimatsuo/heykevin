#!/usr/bin/env python3
"""Run paired, payload-safe Gemini automatic/manual turn-detection replays."""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.voice_turn_replay import (  # noqa: E402
    AUTOMATIC_ARM,
    AUTOMATIC_LATENCY_MAX_LIMIT_MS,
    AUTOMATIC_LATENCY_P95_LIMIT_MS,
    MANUAL_ARM,
    MANUAL_LATENCY_MAX_LIMIT_MS,
    MANUAL_LATENCY_P95_LIMIT_MS,
    OFFLINE_DIAGNOSTIC_SCOPE,
    VoiceReplayAttempt,
    VoiceTurnBenchmarkThresholds,
    VoiceTurnObservation,
    VoiceTurnReplayCase,
    build_gemini_setup_message,
    build_paired_schedule,
    build_replay_inputs,
    evaluate_voice_turn_benchmark,
    load_voice_turn_cases,
    render_voice_turn_case,
    voice_turn_manifest_identity,
)
from app.utils.audio import mulaw_to_pcm16k  # noqa: E402


DEFAULT_MANIFEST = Path(
    "tests/fixtures/voice_vad/fleurs_turn_replay_manifest.json"
)
GEMINI_WS_BASE = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
MAX_PROVIDER_ATTEMPTS = 60
SETUP_TIMEOUT_SECONDS = 5.0
SILENCE_AUDIO = mulaw_to_pcm16k(b"\xff" * 160)
PROVIDER_CLOSE_ERROR_CODES = {
    1000: "provider_closed_normal",
    1001: "provider_closed_going_away",
    1006: "provider_closed_abnormal",
    1008: "provider_closed_policy",
    1011: "provider_closed_internal",
    1012: "provider_closed_restart",
    1013: "provider_closed_retry",
}


class _AttemptState:
    def __init__(self) -> None:
        self.first_audio_at: float | None = None
        self.interruption_events = 0
        self.turn_complete = False
        self.first_audio_ready = asyncio.Event()
        self.turn_complete_ready = asyncio.Event()
        self.receive_error: str | None = None


async def run_attempt(
    *,
    api_key: str,
    model: str,
    case: VoiceTurnReplayCase,
    attempt: VoiceReplayAttempt,
    response_timeout_seconds: float,
    terminal_timeout_seconds: float,
) -> VoiceTurnObservation:
    """Run one isolated provider attempt and retain numeric lifecycle data only."""
    rendered = render_voice_turn_case(case)
    inputs = build_replay_inputs(rendered, arm=attempt.arm)
    state = _AttemptState()
    speech_end_at: float | None = None
    activity_end_at: float | None = None
    error: str | None = None
    url = f"{GEMINI_WS_BASE}?key={api_key}"

    try:
        async with websockets.connect(
            url,
            max_size=10 * 1024 * 1024,
            open_timeout=5,
            ping_interval=10,
            ping_timeout=5,
            close_timeout=1,
        ) as websocket:
            await websocket.send(json.dumps(
                build_gemini_setup_message(model, arm=attempt.arm)
            ))
            try:
                acknowledgement = json.loads(
                    await asyncio.wait_for(
                        websocket.recv(),
                        timeout=SETUP_TIMEOUT_SECONDS,
                    )
                )
            except TimeoutError:
                error = "setup_timeout"
            if error is None and "setupComplete" not in acknowledgement:
                error = "setup_rejected"
            if error is None:
                receiver = asyncio.create_task(
                    _receive_provider_events(websocket, state)
                )
                try:
                    stream_started_at = time.monotonic()
                    for replay_input in inputs:
                        await _sleep_until(
                            stream_started_at + replay_input.at_ms / 1_000
                        )
                        if replay_input.kind == "audio":
                            await websocket.send(json.dumps({
                                "realtimeInput": {
                                    "audio": {
                                        "data": base64.b64encode(
                                            replay_input.audio
                                        ).decode("ascii"),
                                        "mimeType": "audio/pcm;rate=16000",
                                    }
                                }
                            }))
                            if (
                                replay_input.at_ms < rendered.speech_end_ms
                                <= replay_input.at_ms + replay_input.duration_ms
                            ):
                                speech_end_at = time.monotonic()
                        elif replay_input.kind == "activity_start":
                            await websocket.send(json.dumps({
                                "realtimeInput": {"activityStart": {}}
                            }))
                        elif replay_input.kind == "activity_end":
                            await websocket.send(json.dumps({
                                "realtimeInput": {"activityEnd": {}}
                            }))
                            activity_end_at = time.monotonic()

                    if speech_end_at is None:
                        raise ValueError("labeled speech end was not forwarded")

                    stream_end_at = (
                        stream_started_at + rendered.duration_ms / 1_000
                    )
                    await _sleep_until(stream_end_at)
                    if attempt.arm == AUTOMATIC_ARM:
                        try:
                            await _continue_automatic_silence(
                                websocket,
                                state,
                                started_at=stream_end_at,
                                timeout_seconds=response_timeout_seconds,
                            )
                        except TimeoutError:
                            error = "first_audio_timeout"
                    else:
                        try:
                            await asyncio.wait_for(
                                state.first_audio_ready.wait(),
                                timeout=response_timeout_seconds,
                            )
                        except TimeoutError:
                            error = "first_audio_timeout"
                    if error is None:
                        try:
                            await asyncio.wait_for(
                                state.turn_complete_ready.wait(),
                                timeout=terminal_timeout_seconds,
                            )
                        except TimeoutError:
                            error = "turn_complete_timeout"
                except TimeoutError:
                    if error is None:
                        error = "provider_timeout"
                finally:
                    receiver.cancel()
                    try:
                        await receiver
                    except asyncio.CancelledError:
                        pass
                if state.receive_error and error is None:
                    error = state.receive_error
    except TimeoutError:
        error = "provider_timeout"
    except ConnectionClosed as exc:
        error = _provider_close_error(exc)
    except Exception:
        error = "provider_error"

    first_audio_after_speech_end_ms = None
    if state.first_audio_at is not None and speech_end_at is not None:
        first_audio_after_speech_end_ms = round(
            (state.first_audio_at - speech_end_at) * 1_000
        )
    first_audio_after_activity_end_ms = None
    if state.first_audio_at is not None and activity_end_at is not None:
        first_audio_after_activity_end_ms = round(
            (state.first_audio_at - activity_end_at) * 1_000
        )
    return VoiceTurnObservation(
        case_index=attempt.case_index,
        trial=attempt.trial,
        arm=attempt.arm,
        first_audio_after_speech_end_ms=first_audio_after_speech_end_ms,
        first_audio_after_activity_end_ms=first_audio_after_activity_end_ms,
        turn_complete=state.turn_complete,
        interruption_events=state.interruption_events,
        error=error,
    )


async def _receive_provider_events(websocket: Any, state: _AttemptState) -> None:
    try:
        async for raw_message in websocket:
            message = json.loads(raw_message)
            if "error" in message:
                _record_receive_error(state, "provider_error")
                return
            content = message.get("serverContent", {})
            if content.get("interrupted"):
                state.interruption_events += 1
            for part in content.get("modelTurn", {}).get("parts", []):
                inline = part.get("inlineData", {})
                if (
                    state.first_audio_at is None
                    and inline.get("mimeType", "").startswith("audio/")
                    and inline.get("data")
                ):
                    state.first_audio_at = time.monotonic()
                    state.first_audio_ready.set()
            if content.get("turnComplete"):
                state.turn_complete = True
                state.turn_complete_ready.set()
        if not state.turn_complete:
            _record_receive_error(
                state,
                _provider_close_code_error(getattr(websocket, "close_code", None)),
            )
    except asyncio.CancelledError:
        raise
    except ConnectionClosed as exc:
        _record_receive_error(state, _provider_close_error(exc))
    except Exception:
        _record_receive_error(state, "receive_error")


def _record_receive_error(state: _AttemptState, error: str) -> None:
    state.receive_error = error
    state.first_audio_ready.set()
    state.turn_complete_ready.set()


def _provider_close_error(error: ConnectionClosed) -> str:
    code = error.rcvd.code if error.rcvd is not None else 1006
    return _provider_close_code_error(code)


def _provider_close_code_error(code: object) -> str:
    if isinstance(code, bool) or not isinstance(code, int):
        code = 1006
    return PROVIDER_CLOSE_ERROR_CODES.get(code, "provider_closed")


async def _continue_automatic_silence(
    websocket: Any,
    state: _AttemptState,
    *,
    started_at: float,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    next_frame_at = started_at
    while not state.first_audio_ready.is_set():
        if time.monotonic() >= deadline:
            raise TimeoutError
        await _sleep_until(next_frame_at)
        await websocket.send(json.dumps({
            "realtimeInput": {
                "audio": {
                    "data": base64.b64encode(SILENCE_AUDIO).decode("ascii"),
                    "mimeType": "audio/pcm;rate=16000",
                }
            }
        }))
        next_frame_at += 0.02


async def _sleep_until(target: float) -> None:
    await asyncio.sleep(max(0.0, target - time.monotonic()))


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    cases = load_voice_turn_cases(args.manifest)
    corpus_identity = voice_turn_manifest_identity(args.manifest, cases=cases)
    planned_attempts = len(cases) * args.trials_per_case * 2
    if (
        not 1 <= args.max_provider_attempts <= MAX_PROVIDER_ATTEMPTS
        or planned_attempts > args.max_provider_attempts
    ):
        return _failed_report("attempt_limit_exceeded")
    schedule = build_paired_schedule(
        case_count=len(cases),
        trials_per_case=args.trials_per_case,
        seed=args.seed,
    )

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return _failed_report("credential_unavailable")

    observations = []
    for attempt in schedule:
        observation = await run_attempt(
            api_key=api_key,
            model=args.model,
            case=cases[attempt.case_index],
            attempt=attempt,
            response_timeout_seconds=args.response_timeout_seconds,
            terminal_timeout_seconds=args.terminal_timeout_seconds,
        )
        observations.append(observation)
        if observation.error is not None:
            break

    thresholds = VoiceTurnBenchmarkThresholds(
        min_attempts_per_arm=args.min_attempts_per_arm,
        min_paired_attempts=args.min_paired_attempts,
        automatic_latency_p95_ms=AUTOMATIC_LATENCY_P95_LIMIT_MS,
        automatic_latency_max_ms=AUTOMATIC_LATENCY_MAX_LIMIT_MS,
        manual_latency_p95_ms=MANUAL_LATENCY_P95_LIMIT_MS,
        manual_latency_max_ms=MANUAL_LATENCY_MAX_LIMIT_MS,
    )
    report = evaluate_voice_turn_benchmark(
        observations,
        thresholds=thresholds,
    )
    report["configuration"] = {
        "scope": "labeled_fixture_endpoint",
        "model": args.model,
        "cases": len(cases),
        "trials_per_case": args.trials_per_case,
        "max_provider_attempts": args.max_provider_attempts,
        "seed": args.seed,
        "thresholds": asdict(thresholds),
        "timeouts_seconds": {
            "first_audio": args.response_timeout_seconds,
            "turn_complete": args.terminal_timeout_seconds,
        },
        **corpus_identity,
    }
    return report


def _failed_report(error: str) -> dict[str, Any]:
    return {
        "status": "fail",
        "error": error,
        "decision_scope": OFFLINE_DIAGNOSTIC_SCOPE,
        "release_authorized": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--model", required=True)
    parser.add_argument("--trials-per-case", type=int, default=5)
    parser.add_argument("--max-provider-attempts", type=int, default=60)
    parser.add_argument("--seed", type=int, default=29)
    parser.add_argument("--min-attempts-per-arm", type=int, default=30)
    parser.add_argument("--min-paired-attempts", type=int, default=30)
    parser.add_argument("--response-timeout-seconds", type=float, default=12.0)
    parser.add_argument("--terminal-timeout-seconds", type=float, default=12.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        build_gemini_setup_message(args.model, arm=MANUAL_ARM)
        if args.trials_per_case < 1:
            raise ValueError("trials_per_case must be positive")
        report = asyncio.run(run_benchmark(args))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        report = _failed_report("configuration_invalid")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
