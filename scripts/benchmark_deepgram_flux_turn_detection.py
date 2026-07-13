#!/usr/bin/env python3
"""Run a disposable, aggregate-only Deepgram Flux turn feasibility probe."""

from __future__ import annotations

import argparse
import asyncio
from array import array
from dataclasses import dataclass
import hashlib
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path
import platform
import random
import sys
import time
from typing import Any, Iterable
from urllib.parse import urlencode

import websockets

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.voice_turn_replay import (  # noqa: E402
    load_voice_turn_cases,
    render_voice_turn_case,
    voice_turn_manifest_identity,
)
from app.utils.audio import mulaw_to_pcm16k  # noqa: E402


DEFAULT_MANIFEST = Path("tests/fixtures/voice_vad/fleurs_turn_replay_manifest.json")
DEEPGRAM_FLUX_BASE_URL = "wss://api.deepgram.com/v2/listen"
FLUX_MODEL = "flux-general-multi"
SAMPLE_RATE_HZ = 16_000
STREAM_CHUNK_MS = 80
ANALYSIS_WINDOW_MS = 20
PCM16_BYTES_PER_MS = SAMPLE_RATE_HZ * 2 // 1_000
SILENCE_FRAME = b"\x00" * (PCM16_BYTES_PER_MS * STREAM_CHUNK_MS)
QUALIFICATION_BLOCKERS = (
    "development_corpus_incomplete",
    "holdout_unavailable",
    "hosted_network_egress_unapproved",
    "model_revision_unreported",
)
SAFE_ERROR_CODES = {
    "connection_error",
    "provider_closed",
    "provider_error",
    "provider_timeout",
    "receive_error",
}
ALLOWED_LANGUAGES = ("en-US", "es-419")
ALLOWED_PAUSE_DURATIONS_MS = (0, 500, 800, 1_200)
_CLEAN_CASE_LANGUAGES = (
    ("fleurs_en_us_clean", "en-US"),
    ("fleurs_es_419_clean", "es-419"),
)


@dataclass(frozen=True, slots=True)
class FluxScenario:
    """One transcript-free waveform and its local timing labels."""

    scenario_id: str
    language: str
    pcm16: bytes
    sample_rate_hz: int
    speech_end_ms: int
    duration_ms: int
    pause_duration_ms: int = 0
    pause_start_ms: int = 0
    pause_end_ms: int = 0


@dataclass(frozen=True, slots=True)
class FluxObservation:
    """Payload-free evidence retained from one isolated provider attempt."""

    language: str
    pause_duration_ms: int
    premature_end_count: int
    decision_latency_ms: int | None
    language_match: bool | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FluxThresholds:
    """Disposable feasibility gates copied from the turn-control contract."""

    min_attempts: int = 24
    max_premature_ends: int = 0
    max_missing_or_errors: int = 0
    min_decision_coverage: float = 1.0
    decision_p95_ms: int = 500
    decision_max_ms: int = 800


class _AttemptState:
    """Provider receiver state that cannot retain transcripts or identifiers."""

    def __init__(self) -> None:
        self.connected = asyncio.Event()
        self.updated = asyncio.Event()
        self.end_of_turn_arrivals: list[float] = []
        self.language_matches: list[bool | None] = []
        self.error: str | None = None


def build_flux_url(
    *,
    model: str,
    eot_threshold: float,
    eot_timeout_ms: int,
) -> str:
    """Build a bounded Flux URL without putting credentials in the query."""
    if model != FLUX_MODEL:
        raise ValueError("the multilingual Flux model alias is required")
    if (
        isinstance(eot_threshold, bool)
        or not isinstance(eot_threshold, (int, float))
        or not 0.5 <= float(eot_threshold) <= 0.9
    ):
        raise ValueError("eot_threshold must be between 0.5 and 0.9")
    if (
        isinstance(eot_timeout_ms, bool)
        or not isinstance(eot_timeout_ms, int)
        or not 500 <= eot_timeout_ms <= 10_000
    ):
        raise ValueError("eot_timeout_ms must be between 500 and 10000")
    query = urlencode(
        {
            "encoding": "linear16",
            "eot_threshold": format(float(eot_threshold), "g"),
            "eot_timeout_ms": str(eot_timeout_ms),
            "mip_opt_out": "true",
            "model": model,
            "sample_rate": str(SAMPLE_RATE_HZ),
        }
    )
    return f"{DEEPGRAM_FLUX_BASE_URL}?{query}"


def build_flux_scenarios(
    manifest: str | Path,
    *,
    pause_durations_ms: tuple[int, ...] = (500, 800, 1_200),
) -> tuple[FluxScenario, ...]:
    """Build deterministic bilingual baselines and internal-pause waveforms."""
    if (
        not pause_durations_ms
        or any(
            isinstance(duration, bool)
            or not isinstance(duration, int)
            or not 500 <= duration <= 2_000
            for duration in pause_durations_ms
        )
        or len(set(pause_durations_ms)) != len(pause_durations_ms)
    ):
        raise ValueError("pause durations must be unique values from 500 to 2000 ms")

    cases = {case.name: case for case in load_voice_turn_cases(manifest)}
    scenarios = []
    for case_name, language in _CLEAN_CASE_LANGUAGES:
        case = cases.get(case_name)
        if case is None:
            raise ValueError("required clean replay case is missing")
        rendered = render_voice_turn_case(case)
        pcm16 = _normalize_pcm_duration(
            mulaw_to_pcm16k(rendered.mulaw8),
            duration_ms=rendered.duration_ms,
        )
        language_id = language.lower().replace("-", "_")
        scenarios.append(
            FluxScenario(
                scenario_id=f"{language_id}_baseline",
                language=language,
                pcm16=pcm16,
                sample_rate_hz=SAMPLE_RATE_HZ,
                speech_end_ms=rendered.speech_end_ms,
                duration_ms=rendered.duration_ms,
            )
        )

        pause_start_ms = _find_quiet_internal_split(
            pcm16,
            speech_start_ms=rendered.speech_start_ms,
            speech_end_ms=rendered.speech_end_ms,
        )
        insert_at = pause_start_ms * PCM16_BYTES_PER_MS
        for pause_duration_ms in pause_durations_ms:
            pause = b"\x00" * (pause_duration_ms * PCM16_BYTES_PER_MS)
            scenarios.append(
                FluxScenario(
                    scenario_id=(f"{language_id}_internal_pause_{pause_duration_ms}ms"),
                    language=language,
                    pcm16=pcm16[:insert_at] + pause + pcm16[insert_at:],
                    sample_rate_hz=SAMPLE_RATE_HZ,
                    speech_end_ms=rendered.speech_end_ms + pause_duration_ms,
                    duration_ms=rendered.duration_ms + pause_duration_ms,
                    pause_duration_ms=pause_duration_ms,
                    pause_start_ms=pause_start_ms,
                    pause_end_ms=pause_start_ms + pause_duration_ms,
                )
            )
    return tuple(scenarios)


async def _receive_flux_events(
    websocket: Any,
    state: _AttemptState,
    *,
    expected_language: str,
    clock: Any = time.monotonic,
) -> None:
    """Reduce provider events immediately to safe numeric and boolean state."""
    try:
        async for raw_message in websocket:
            message = json.loads(raw_message)
            if not isinstance(message, dict):
                continue
            message_type = message.get("type")
            if message_type == "Connected":
                state.connected.set()
                state.updated.set()
            elif message_type == "TurnInfo" and message.get("event") == "EndOfTurn":
                state.end_of_turn_arrivals.append(float(clock()))
                state.language_matches.append(_event_language_matches(message, expected_language))
                state.updated.set()
            elif message_type == "Error":
                state.error = "provider_error"
                state.updated.set()
    except asyncio.CancelledError:
        raise
    except Exception:
        state.error = "receive_error"
        state.updated.set()


async def run_flux_attempt(
    *,
    api_key: str,
    scenario: FluxScenario,
    url: str,
    connect_timeout_seconds: float,
    decision_timeout_seconds: float,
) -> FluxObservation:
    """Stream one waveform in real time and retain aggregate-safe evidence."""
    state = _AttemptState()
    speech_end_at: float | None = None
    receiver: asyncio.Task[None] | None = None
    error: str | None = None
    try:
        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {api_key}"},
            max_size=2 * 1024 * 1024,
            open_timeout=connect_timeout_seconds,
            ping_interval=10,
            ping_timeout=5,
            close_timeout=1,
        ) as websocket:
            receiver = asyncio.create_task(
                _receive_flux_events(
                    websocket,
                    state,
                    expected_language=scenario.language,
                )
            )
            try:
                await asyncio.wait_for(
                    state.connected.wait(),
                    timeout=connect_timeout_seconds,
                )
                stream_started_at = time.monotonic()
                speech_end_at = stream_started_at + scenario.speech_end_ms / 1_000
                await _stream_scenario(websocket, scenario, stream_started_at)
                await _continue_silence_until_decision(
                    websocket,
                    state,
                    speech_end_at=speech_end_at,
                    next_frame_at=(
                        stream_started_at + (scenario.duration_ms + STREAM_CHUNK_MS) / 1_000
                    ),
                    timeout_seconds=decision_timeout_seconds,
                )
            except TimeoutError:
                error = "provider_timeout"
            finally:
                receiver.cancel()
                try:
                    await receiver
                except asyncio.CancelledError:
                    pass
                receiver = None
                try:
                    await websocket.send(json.dumps({"type": "CloseStream"}))
                except (
                    OSError,
                    RuntimeError,
                    websockets.exceptions.ConnectionClosed,
                ):
                    pass
            if state.error is not None:
                error = state.error
    except TimeoutError:
        error = "provider_timeout"
    except websockets.exceptions.ConnectionClosed:
        error = "provider_closed"
    except OSError:
        error = "connection_error"
    except Exception:
        error = "provider_error"
    finally:
        if receiver is not None:
            receiver.cancel()

    premature_end_count = 0
    decision_latency_ms = None
    language_match = None
    if speech_end_at is not None:
        final_events = []
        for arrival, matches in zip(
            state.end_of_turn_arrivals,
            state.language_matches,
            strict=True,
        ):
            if arrival < speech_end_at:
                premature_end_count += 1
            else:
                final_events.append((arrival, matches))
        if final_events:
            arrival, language_match = final_events[0]
            decision_latency_ms = max(
                0,
                round((arrival - speech_end_at) * 1_000),
            )
    if decision_latency_ms is None and error is None:
        error = "provider_timeout"
    return FluxObservation(
        language=scenario.language,
        pause_duration_ms=scenario.pause_duration_ms,
        premature_end_count=premature_end_count,
        decision_latency_ms=decision_latency_ms,
        language_match=language_match,
        error=_safe_error_code(error),
    )


def evaluate_flux_observations(
    observations: Iterable[FluxObservation],
    *,
    thresholds: FluxThresholds | None = None,
) -> dict[str, Any]:
    """Evaluate feasibility gates without serializing attempt-level evidence."""
    limits = thresholds or FluxThresholds()
    items = list(observations)
    _validate_observations(items)
    attempts = len(items)
    decisions = sum(item.decision_latency_ms is not None for item in items)
    premature_ends = sum(item.premature_end_count for item in items)
    missing_decisions = attempts - decisions
    provider_errors = sum(item.error is not None for item in items)
    language_matches = sum(item.language_match is True for item in items)
    decision_latencies = [
        item.decision_latency_ms for item in items if item.decision_latency_ms is not None
    ]
    coverage = decisions / attempts if attempts else 0.0
    language_coverage = language_matches / decisions if decisions else 0.0
    p95_ms = _percentile(decision_latencies, 0.95)
    max_ms = max(decision_latencies) if decision_latencies else None
    missing_or_errors = sum(
        item.decision_latency_ms is None or item.error is not None for item in items
    )
    gates = [
        _gate(
            "minimum_attempts",
            attempts >= limits.min_attempts,
            attempts,
            f">= {limits.min_attempts}",
        ),
        _gate(
            "premature_semantic_ends",
            premature_ends <= limits.max_premature_ends,
            premature_ends,
            f"<= {limits.max_premature_ends}",
        ),
        _gate(
            "missing_semantic_ends_or_errors",
            missing_or_errors <= limits.max_missing_or_errors,
            missing_or_errors,
            f"<= {limits.max_missing_or_errors}",
        ),
        _gate(
            "semantic_decision_coverage",
            coverage >= limits.min_decision_coverage,
            round(coverage, 4),
            f">= {limits.min_decision_coverage}",
        ),
        _gate(
            "speech_end_to_decision_p95_ms",
            _at_most(p95_ms, limits.decision_p95_ms),
            p95_ms,
            f"<= {limits.decision_p95_ms}",
        ),
        _gate(
            "speech_end_to_decision_max_ms",
            _at_most(max_ms, limits.decision_max_ms),
            max_ms,
            f"<= {limits.decision_max_ms}",
        ),
        _gate(
            "language_match_coverage",
            language_coverage == 1.0,
            round(language_coverage, 4),
            "= 1.0",
        ),
    ]
    passed = all(gate["passed"] for gate in gates)
    return {
        "status": "pass" if passed else "fail",
        "candidate_decision": ("advance_to_offline_corpus" if passed else "reject"),
        "qualification": {
            "eligible": False,
            "blockers": list(QUALIFICATION_BLOCKERS),
        },
        "sample": {
            "attempts": attempts,
            "decisions": decisions,
            "premature_ends": premature_ends,
            "missing_decisions": missing_decisions,
            "provider_errors": provider_errors,
            "language_matches": language_matches,
        },
        "metrics": {
            "semantic_decision_coverage": round(coverage, 4),
            "language_match_coverage": round(language_coverage, 4),
            "speech_end_to_decision_p95_ms": p95_ms,
            "speech_end_to_decision_max_ms": max_ms,
        },
        "buckets": {
            "languages": {
                language: _aggregate_bucket([item for item in items if item.language == language])
                for language in ALLOWED_LANGUAGES
            },
            "scenarios": {
                _pause_bucket_name(pause_duration_ms): _aggregate_bucket(
                    [item for item in items if item.pause_duration_ms == pause_duration_ms]
                )
                for pause_duration_ms in ALLOWED_PAUSE_DURATIONS_MS
            },
        },
        "gates": gates,
    }


async def run_benchmark(
    args: argparse.Namespace,
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Run the disposable probe and return only aggregate-safe evidence."""
    credential = os.environ.get("DEEPGRAM_API_KEY", "") if api_key is None else api_key
    if not credential:
        return {"status": "fail", "error": "credential_unavailable"}

    scenarios = build_flux_scenarios(args.manifest)
    url = build_flux_url(
        model=args.model,
        eot_threshold=args.eot_threshold,
        eot_timeout_ms=args.eot_timeout_ms,
    )
    schedule = [
        (scenario_index, trial)
        for scenario_index in range(len(scenarios))
        for trial in range(1, args.trials_per_scenario + 1)
    ]
    random.Random(args.seed).shuffle(schedule)
    observations = []
    for scenario_index, _trial in schedule:
        observations.append(
            await run_flux_attempt(
                api_key=credential,
                scenario=scenarios[scenario_index],
                url=url,
                connect_timeout_seconds=args.connect_timeout_seconds,
                decision_timeout_seconds=args.decision_timeout_seconds,
            )
        )

    report = evaluate_flux_observations(
        observations,
        thresholds=FluxThresholds(
            min_attempts=args.min_attempts,
            decision_p95_ms=args.decision_p95_ms,
            decision_max_ms=args.decision_max_ms,
        ),
    )
    source_identity = voice_turn_manifest_identity(args.manifest)
    report["configuration"] = {
        "scope": "disposable_bilingual_feasibility",
        "input_mode": "audio_waveform",
        "provider": "deepgram",
        "model_alias": args.model,
        "model_revision": "unreported",
        "model_digest": "unreported",
        "encoding": "linear16",
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "stream_chunk_ms": STREAM_CHUNK_MS,
        "stream_chunk_pacing": "trailing_edge",
        "eot_threshold": args.eot_threshold,
        "eot_timeout_ms": args.eot_timeout_ms,
        "model_improvement_opt_out": True,
        "languages": sorted({scenario.language for scenario in scenarios}),
        "scenarios": len(scenarios),
        "trials_per_scenario": args.trials_per_scenario,
        "seed": args.seed,
        "source_manifest_sha256": source_identity["manifest_sha256"],
        "source_corpus_sha256": source_identity["corpus_sha256"],
        "probe_corpus_sha256": _probe_corpus_sha256(scenarios),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "replay_module_sha256": hashlib.sha256(
            (REPO_ROOT / "app/services/voice_turn_replay.py").read_bytes()
        ).hexdigest(),
        "audio_module_sha256": hashlib.sha256(
            (REPO_ROOT / "app/utils/audio.py").read_bytes()
        ).hexdigest(),
        "dependency_manifest_sha256": hashlib.sha256(
            (REPO_ROOT / "pyproject.toml").read_bytes()
        ).hexdigest(),
        "python_version": platform.python_version(),
        "websockets_version": package_version("websockets"),
    }
    return report


async def _stream_scenario(
    websocket: Any,
    scenario: FluxScenario,
    started_at: float,
) -> None:
    frame_bytes = PCM16_BYTES_PER_MS * STREAM_CHUNK_MS
    for offset in range(0, len(scenario.pcm16), frame_bytes):
        chunk = scenario.pcm16[offset : offset + frame_bytes]
        chunk_end_ms = (offset + len(chunk)) // PCM16_BYTES_PER_MS
        await _sleep_until(started_at + chunk_end_ms / 1_000)
        await websocket.send(chunk)


async def _continue_silence_until_decision(
    websocket: Any,
    state: _AttemptState,
    *,
    speech_end_at: float,
    next_frame_at: float,
    timeout_seconds: float,
) -> None:
    deadline = speech_end_at + timeout_seconds
    while True:
        if state.error is not None:
            return
        if any(arrival >= speech_end_at for arrival in state.end_of_turn_arrivals):
            return
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError
        await _sleep_until(next_frame_at)
        if time.monotonic() >= deadline:
            raise TimeoutError
        await websocket.send(SILENCE_FRAME)
        next_frame_at += STREAM_CHUNK_MS / 1_000


async def _sleep_until(target: float) -> None:
    await asyncio.sleep(max(0.0, target - time.monotonic()))


def _event_language_matches(
    message: dict[str, Any],
    expected_language: str,
) -> bool | None:
    raw_languages = message.get("languages")
    if isinstance(raw_languages, str):
        candidates = (raw_languages,)
    elif isinstance(raw_languages, list):
        candidates = tuple(value for value in raw_languages if isinstance(value, str))
    else:
        raw_language = message.get("language")
        candidates = (raw_language,) if isinstance(raw_language, str) else ()
    if not candidates:
        return None
    expected_base = expected_language.lower().split("-", maxsplit=1)[0]
    return expected_base in {
        candidate.lower().replace("_", "-").split("-", maxsplit=1)[0] for candidate in candidates
    }


def _normalize_pcm_duration(pcm16: bytes, *, duration_ms: int) -> bytes:
    expected_length = duration_ms * PCM16_BYTES_PER_MS
    if len(pcm16) > expected_length:
        return pcm16[:expected_length]
    return pcm16 + b"\x00" * (expected_length - len(pcm16))


def _find_quiet_internal_split(
    pcm16: bytes,
    *,
    speech_start_ms: int,
    speech_end_ms: int,
) -> int:
    speech_span_ms = speech_end_ms - speech_start_ms
    margin_ms = max(500, speech_span_ms // 5)
    search_start_ms = _align_up(
        speech_start_ms + margin_ms,
        ANALYSIS_WINDOW_MS,
    )
    search_end_ms = _align_down(
        speech_end_ms - margin_ms,
        ANALYSIS_WINDOW_MS,
    )
    if search_end_ms - search_start_ms < 2 * ANALYSIS_WINDOW_MS:
        raise ValueError("speech span is too short for an internal pause")

    samples = array("h")
    samples.frombytes(pcm16)
    if sys.byteorder != "little":
        samples.byteswap()
    frame_samples = SAMPLE_RATE_HZ * ANALYSIS_WINDOW_MS // 1_000
    candidates = []
    for at_ms in range(search_start_ms, search_end_ms, ANALYSIS_WINDOW_MS):
        start = at_ms * SAMPLE_RATE_HZ // 1_000
        window = samples[start : start + frame_samples]
        if len(window) != frame_samples:
            continue
        energy = sum(sample * sample for sample in window)
        candidates.append((energy, at_ms))
    if not candidates:
        raise ValueError("no internal pause split is available")
    return min(candidates)[1]


def _probe_corpus_sha256(scenarios: Iterable[FluxScenario]) -> str:
    canonical = [
        {
            "scenario_id": scenario.scenario_id,
            "language": scenario.language,
            "pcm16_sha256": hashlib.sha256(scenario.pcm16).hexdigest(),
            "sample_rate_hz": scenario.sample_rate_hz,
            "speech_end_ms": scenario.speech_end_ms,
            "duration_ms": scenario.duration_ms,
            "pause_duration_ms": scenario.pause_duration_ms,
            "pause_start_ms": scenario.pause_start_ms,
            "pause_end_ms": scenario.pause_end_ms,
        }
        for scenario in scenarios
    ]
    serialized = json.dumps(
        {"fingerprint_version": 1, "scenarios": canonical},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()


def _validate_observations(items: Iterable[FluxObservation]) -> None:
    for item in items:
        if item.language not in ALLOWED_LANGUAGES:
            raise ValueError("observation language is not allowlisted")
        if item.pause_duration_ms not in ALLOWED_PAUSE_DURATIONS_MS:
            raise ValueError("observation pause bucket is not allowlisted")
        if (
            isinstance(item.premature_end_count, bool)
            or not isinstance(item.premature_end_count, int)
            or item.premature_end_count < 0
        ):
            raise ValueError("premature_end_count must be non-negative")
        if item.decision_latency_ms is not None and (
            isinstance(item.decision_latency_ms, bool)
            or not isinstance(item.decision_latency_ms, int)
            or item.decision_latency_ms < 0
        ):
            raise ValueError("decision_latency_ms must be non-negative")
        if item.language_match not in {True, False, None}:
            raise ValueError("language_match must be boolean or null")


def _aggregate_bucket(items: list[FluxObservation]) -> dict[str, int | None]:
    latencies = [item.decision_latency_ms for item in items if item.decision_latency_ms is not None]
    return {
        "attempts": len(items),
        "decisions": len(latencies),
        "premature_ends": sum(item.premature_end_count for item in items),
        "provider_errors": sum(item.error is not None for item in items),
        "language_matches": sum(item.language_match is True for item in items),
        "decision_p95_ms": _percentile(latencies, 0.95),
        "decision_max_ms": max(latencies) if latencies else None,
    }


def _pause_bucket_name(pause_duration_ms: int) -> str:
    if pause_duration_ms == 0:
        return "baseline"
    return f"internal_pause_{pause_duration_ms}ms"


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


def _safe_error_code(error: str | None) -> str | None:
    if error is None:
        return None
    return error if error in SAFE_ERROR_CODES else "provider_error"


def _align_up(value: int, multiple: int) -> int:
    return ((value + multiple - 1) // multiple) * multiple


def _align_down(value: int, multiple: int) -> int:
    return value // multiple * multiple


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--model", default=FLUX_MODEL)
    parser.add_argument("--eot-threshold", type=float, default=0.7)
    parser.add_argument("--eot-timeout-ms", type=int, default=5_000)
    parser.add_argument("--trials-per-scenario", type=int, default=3)
    parser.add_argument("--seed", type=int, default=73)
    parser.add_argument("--min-attempts", type=int, default=24)
    parser.add_argument("--decision-p95-ms", type=int, default=500)
    parser.add_argument("--decision-max-ms", type=int, default=800)
    parser.add_argument("--connect-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--decision-timeout-seconds", type=float, default=7.0)
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    build_flux_url(
        model=args.model,
        eot_threshold=args.eot_threshold,
        eot_timeout_ms=args.eot_timeout_ms,
    )
    for value, name in (
        (args.trials_per_scenario, "trials_per_scenario"),
        (args.min_attempts, "min_attempts"),
        (args.decision_p95_ms, "decision_p95_ms"),
        (args.decision_max_ms, "decision_max_ms"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if args.min_attempts > 8 * args.trials_per_scenario:
        raise ValueError("min_attempts exceeds the scheduled attempt count")
    if args.decision_p95_ms > args.decision_max_ms:
        raise ValueError("decision p95 cannot exceed the maximum")
    for value, name in (
        (args.connect_timeout_seconds, "connect_timeout_seconds"),
        (args.decision_timeout_seconds, "decision_timeout_seconds"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 1 <= float(value) <= 30
        ):
            raise ValueError(f"{name} must be between 1 and 30 seconds")


def main() -> int:
    args = parse_args()
    try:
        _validate_args(args)
        report = asyncio.run(run_benchmark(args))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        report = {"status": "fail", "error": "configuration_invalid"}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
