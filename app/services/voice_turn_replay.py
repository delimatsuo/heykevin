"""Offline, payload-safe replay primitives for Gemini turn detection experiments."""

from __future__ import annotations

from array import array
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
import re
import sys
from typing import Any, Iterable
import audioop

from app.utils.audio import mulaw_to_pcm16k


AUTOMATIC_ARM = "automatic"
MANUAL_ARM = "manual"
VALID_ARMS = {AUTOMATIC_ARM, MANUAL_ARM}
OFFLINE_DIAGNOSTIC_SCOPE = "offline_diagnostic_only"
AUTOMATIC_LATENCY_P95_LIMIT_MS = 1_500
AUTOMATIC_LATENCY_MAX_LIMIT_MS = 2_500
MANUAL_LATENCY_P95_LIMIT_MS = 1_500
MANUAL_LATENCY_MAX_LIMIT_MS = 2_500
SAFE_ERROR_CODES = {
    "first_audio_timeout",
    "provider_closed_abnormal",
    "provider_closed_going_away",
    "provider_closed_internal",
    "provider_closed_normal",
    "provider_closed_policy",
    "provider_closed_restart",
    "provider_closed_retry",
    "provider_closed",
    "provider_error",
    "provider_timeout",
    "receive_error",
    "setup_rejected",
    "setup_timeout",
    "turn_complete_timeout",
}
MODEL_PATTERN = re.compile(r"gemini-[a-z0-9][a-z0-9.-]*")


@dataclass(frozen=True, slots=True)
class _VoiceTurnReplaySource:
    path: Path
    sha256: str
    sample_rate_hz: int
    speech_start_ms: int
    speech_end_ms: int


@dataclass(frozen=True, slots=True)
class VoiceTurnReplayCase:
    name: str
    source_path: Path
    source_sha256: str
    source_sample_rate_hz: int
    source_speech_start_ms: int
    source_speech_end_ms: int
    repetitions: int = 1
    inter_repeat_silence_ms: int = 0
    post_speech_silence_ms: int = 500
    gain: float = 1.0
    noise_peak: int = 0
    noise_seed: int = 0
    frame_pattern_ms: tuple[int, ...] = (20,)


@dataclass(frozen=True, slots=True)
class RenderedVoiceTurn:
    mulaw8: bytes
    sample_rate_hz: int
    speech_start_ms: int
    speech_end_ms: int
    duration_ms: int
    frame_pattern_ms: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class VoiceReplayInput:
    kind: str
    at_ms: int
    audio: bytes = b""
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class VoiceReplayAttempt:
    case_index: int
    trial: int
    arm: str


@dataclass(frozen=True, slots=True)
class VoiceTurnObservation:
    case_index: int
    trial: int
    arm: str
    first_audio_after_speech_end_ms: int | None
    first_audio_after_activity_end_ms: int | None
    turn_complete: bool
    interruption_events: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class VoiceTurnBenchmarkThresholds:
    min_attempts_per_arm: int = 30
    min_paired_attempts: int = 30
    completion_rate: float = 1.0
    latency_coverage: float = 1.0
    max_manual_premature_responses: int = 0
    max_manual_interruption_events: int = 0
    max_errors: int = 0
    automatic_latency_p95_ms: int = AUTOMATIC_LATENCY_P95_LIMIT_MS
    automatic_latency_max_ms: int = AUTOMATIC_LATENCY_MAX_LIMIT_MS
    manual_latency_p95_ms: int = MANUAL_LATENCY_P95_LIMIT_MS
    manual_latency_max_ms: int = MANUAL_LATENCY_MAX_LIMIT_MS


def load_voice_turn_cases(path: str | Path) -> tuple[VoiceTurnReplayCase, ...]:
    """Load and validate a path-confined voice replay manifest."""
    manifest_path = Path(path)
    with manifest_path.open() as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict) or manifest.get("version") not in {1, 2}:
        raise ValueError("unsupported voice replay manifest")

    fixture_root = manifest_path.resolve().parent
    version = manifest["version"]
    if version == 1:
        sources = {"default": _load_voice_turn_source(fixture_root, manifest)}
    else:
        raw_sources = manifest.get("sources")
        if not isinstance(raw_sources, dict) or not raw_sources:
            raise ValueError("sources must be a non-empty object")
        sources = {}
        for source_id, raw_source in raw_sources.items():
            if not isinstance(source_id, str) or not re.fullmatch(
                r"[a-z0-9_]+", source_id
            ):
                raise ValueError("source id must be a safe identifier")
            if not isinstance(raw_source, dict):
                raise ValueError("each source must be an object")
            sources[source_id] = _load_voice_turn_source(
                fixture_root,
                raw_source,
            )

    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("cases must be a non-empty list")

    cases = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("each voice replay case must be an object")
        name = raw_case.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]+", name):
            raise ValueError("case name must be a safe identifier")
        if version == 1:
            source = sources["default"]
        else:
            source_id = raw_case.get("source")
            if not isinstance(source_id, str) or source_id not in sources:
                raise ValueError("case source must identify a declared source")
            source = sources[source_id]
        repetitions = _bounded_int(raw_case, "repetitions", 1, 1, 4)
        inter_silence = _bounded_int(
            raw_case,
            "inter_repeat_silence_ms",
            0,
            0,
            2_000,
        )
        post_silence = _bounded_int(
            raw_case,
            "post_speech_silence_ms",
            500,
            500,
            2_000,
        )
        gain = raw_case.get("gain", 1.0)
        if isinstance(gain, bool) or not isinstance(gain, (int, float)):
            raise ValueError("gain must be numeric")
        gain = float(gain)
        if not 0.1 <= gain <= 2.0:
            raise ValueError("gain must be between 0.1 and 2.0")
        noise_peak = _bounded_int(raw_case, "noise_peak", 0, 0, 2_000)
        noise_seed = _bounded_int(raw_case, "noise_seed", 0, 0, 2**31 - 1)
        frame_pattern = raw_case.get("frame_pattern_ms", [20])
        if (
            not isinstance(frame_pattern, list)
            or not frame_pattern
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value not in {10, 20, 30}
                for value in frame_pattern
            )
        ):
            raise ValueError("frame_pattern_ms supports only 10, 20, or 30 ms")
        cases.append(
            VoiceTurnReplayCase(
                name=name,
                source_path=source.path,
                source_sha256=source.sha256,
                source_sample_rate_hz=source.sample_rate_hz,
                source_speech_start_ms=source.speech_start_ms,
                source_speech_end_ms=source.speech_end_ms,
                repetitions=repetitions,
                inter_repeat_silence_ms=inter_silence,
                post_speech_silence_ms=post_silence,
                gain=gain,
                noise_peak=noise_peak,
                noise_seed=noise_seed,
                frame_pattern_ms=tuple(frame_pattern),
            )
        )
    return tuple(cases)


def voice_turn_manifest_identity(
    path: str | Path,
    *,
    cases: Iterable[VoiceTurnReplayCase] | None = None,
) -> dict[str, str]:
    """Return raw and semantic hashes for an auditable replay corpus."""
    manifest_path = Path(path)
    loaded_cases = tuple(cases) if cases is not None else load_voice_turn_cases(path)
    semantic_cases = [
        {
            "name": case.name,
            "source_sha256": case.source_sha256,
            "source_sample_rate_hz": case.source_sample_rate_hz,
            "source_speech_start_ms": case.source_speech_start_ms,
            "source_speech_end_ms": case.source_speech_end_ms,
            "repetitions": case.repetitions,
            "inter_repeat_silence_ms": case.inter_repeat_silence_ms,
            "post_speech_silence_ms": case.post_speech_silence_ms,
            "gain": case.gain,
            "noise_peak": case.noise_peak,
            "noise_seed": case.noise_seed,
            "frame_pattern_ms": case.frame_pattern_ms,
        }
        for case in loaded_cases
    ]
    canonical = json.dumps(
        {"fingerprint_version": 1, "cases": semantic_cases},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return {
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "corpus_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _load_voice_turn_source(
    fixture_root: Path,
    raw_source: dict[str, Any],
) -> _VoiceTurnReplaySource:
    source_value = raw_source.get("source_pcm")
    if not isinstance(source_value, str) or not source_value:
        raise ValueError("source_pcm is required")
    source_path = (fixture_root / source_value).resolve()
    if source_path.parent != fixture_root or not source_path.is_file():
        raise ValueError("source_pcm must remain inside the fixture directory")

    checksum_chunks = raw_source.get("source_sha256_chunks")
    if (
        not isinstance(checksum_chunks, list)
        or not checksum_chunks
        or not all(isinstance(chunk, str) for chunk in checksum_chunks)
    ):
        raise ValueError("source_sha256_chunks is required")
    source_sha256 = "".join(checksum_chunks)
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("source checksum must be SHA-256")
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha256:
        raise ValueError("source checksum mismatch")

    source_rate = _required_int(raw_source, "source_sample_rate_hz")
    speech_start_ms = _required_int(raw_source, "source_speech_start_ms")
    speech_end_ms = _required_int(raw_source, "source_speech_end_ms")
    if source_rate != 8_000:
        raise ValueError("source_sample_rate_hz must be 8000")
    if speech_start_ms < 0 or speech_end_ms <= speech_start_ms:
        raise ValueError("source speech boundaries are invalid")
    return _VoiceTurnReplaySource(
        path=source_path,
        sha256=source_sha256,
        sample_rate_hz=source_rate,
        speech_start_ms=speech_start_ms,
        speech_end_ms=speech_end_ms,
    )


def render_voice_turn_case(case: VoiceTurnReplayCase) -> RenderedVoiceTurn:
    """Render one labeled fixture through deterministic Twilio codec conditions."""
    source = case.source_path.read_bytes()
    if hashlib.sha256(source).hexdigest() != case.source_sha256:
        raise ValueError("source checksum mismatch")
    if not source or len(source) % 2:
        raise ValueError("source PCM must contain complete 16-bit samples")

    transformed = _transform_pcm16(
        source,
        gain=case.gain,
        noise_peak=case.noise_peak,
        noise_seed=case.noise_seed,
    )
    source_samples = len(transformed) // 2
    source_start_sample = round(
        case.source_speech_start_ms * case.source_sample_rate_hz / 1_000
    )
    source_end_sample = round(
        case.source_speech_end_ms * case.source_sample_rate_hz / 1_000
    )
    if source_end_sample > source_samples:
        raise ValueError("speech boundary exceeds source duration")

    inter_silence_samples = (
        case.inter_repeat_silence_ms * case.source_sample_rate_hz // 1_000
    )
    inter_silence = b"\x00\x00" * inter_silence_samples
    parts = []
    for repetition in range(case.repetitions):
        if repetition:
            parts.append(inter_silence)
        parts.append(transformed)
    rendered_pcm = b"".join(parts)

    final_offset_samples = (case.repetitions - 1) * (
        source_samples + inter_silence_samples
    )
    final_speech_end_sample = final_offset_samples + source_end_sample
    required_end_sample = final_speech_end_sample + (
        case.post_speech_silence_ms * case.source_sample_rate_hz // 1_000
    )
    rendered_samples = len(rendered_pcm) // 2
    if rendered_samples < required_end_sample:
        rendered_pcm += b"\x00\x00" * (required_end_sample - rendered_samples)

    padded_samples = len(rendered_pcm) // 2
    samples_per_ms = case.source_sample_rate_hz // 1_000
    remainder = padded_samples % samples_per_ms
    if remainder:
        rendered_pcm += b"\x00\x00" * (samples_per_ms - remainder)

    mulaw8 = audioop.lin2ulaw(rendered_pcm, 2)
    duration_ms = len(mulaw8) // samples_per_ms
    speech_start_ms = round(source_start_sample * 1_000 / case.source_sample_rate_hz)
    speech_end_ms = round(
        final_speech_end_sample * 1_000 / case.source_sample_rate_hz
    )
    return RenderedVoiceTurn(
        mulaw8=mulaw8,
        sample_rate_hz=16_000,
        speech_start_ms=speech_start_ms,
        speech_end_ms=speech_end_ms,
        duration_ms=duration_ms,
        frame_pattern_ms=case.frame_pattern_ms,
    )


def build_replay_inputs(
    rendered: RenderedVoiceTurn,
    *,
    arm: str,
) -> tuple[VoiceReplayInput, ...]:
    """Build an ordered realtime-input sequence with identical audio per arm."""
    _validate_arm(arm)
    events = []
    if arm == MANUAL_ARM:
        events.append(VoiceReplayInput(kind="activity_start", at_ms=0))

    position = 0
    pattern_index = 0
    while position < len(rendered.mulaw8):
        chunk_ms = rendered.frame_pattern_ms[
            pattern_index % len(rendered.frame_pattern_ms)
        ]
        pattern_index += 1
        chunk_bytes = chunk_ms * 8
        chunk = rendered.mulaw8[position : position + chunk_bytes]
        events.append(
            VoiceReplayInput(
                kind="audio",
                at_ms=position // 8,
                audio=mulaw_to_pcm16k(chunk),
                duration_ms=len(chunk) // 8,
            )
        )
        position += len(chunk)

    if arm == MANUAL_ARM:
        events.append(
            VoiceReplayInput(
                kind="activity_end",
                at_ms=rendered.duration_ms,
            )
        )
    return tuple(events)


def build_paired_schedule(
    *,
    case_count: int,
    trials_per_case: int,
    seed: int,
) -> tuple[VoiceReplayAttempt, ...]:
    """Return randomized paired arms while preserving one treatment per control."""
    if case_count < 1 or trials_per_case < 1:
        raise ValueError("case_count and trials_per_case must be positive")
    rng = random.Random(seed)
    pairs = [
        (case_index, trial)
        for case_index in range(case_count)
        for trial in range(1, trials_per_case + 1)
    ]
    rng.shuffle(pairs)
    first_arms = [
        AUTOMATIC_ARM if index % 2 == 0 else MANUAL_ARM
        for index in range(len(pairs))
    ]
    rng.shuffle(first_arms)
    schedule = []
    for (case_index, trial), first_arm in zip(pairs, first_arms, strict=True):
        second_arm = MANUAL_ARM if first_arm == AUTOMATIC_ARM else AUTOMATIC_ARM
        schedule.extend(
            VoiceReplayAttempt(
                case_index=case_index,
                trial=trial,
                arm=arm,
            )
            for arm in (first_arm, second_arm)
        )
    return tuple(schedule)


def build_gemini_setup_message(model: str, *, arm: str) -> dict[str, Any]:
    """Build provider setup for one explicit non-latest replay model ID."""
    _validate_arm(arm)
    if (
        not MODEL_PATTERN.fullmatch(model)
        or "latest" in model
        or model.endswith("-exp")
    ):
        raise ValueError("an explicit non-latest Gemini model ID is required")

    thinking_config = (
        {"thinkingLevel": "minimal"}
        if model.startswith("gemini-3")
        else {"thinkingBudget": 0}
    )
    automatic_detection = (
        {"disabled": True}
        if arm == MANUAL_ARM
        else {
            "disabled": False,
            "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
            "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
            "prefixPaddingMs": 100,
            "silenceDurationMs": 500,
        }
    )
    return {
        "setup": {
            "model": f"models/{model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "maxOutputTokens": 120,
                "temperature": 0.4,
                "thinkingConfig": thinking_config,
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": "Puck"}
                    }
                },
            },
            "systemInstruction": {
                "parts": [{
                    "text": (
                        "You are a concise receptionist. Respond to the caller "
                        "in one short sentence."
                    )
                }]
            },
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "realtimeInputConfig": {
                "automaticActivityDetection": automatic_detection,
                "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
                "turnCoverage": "TURN_INCLUDES_ONLY_ACTIVITY",
            },
        }
    }


def evaluate_voice_turn_benchmark(
    observations: Iterable[VoiceTurnObservation],
    *,
    thresholds: VoiceTurnBenchmarkThresholds | None = None,
) -> dict[str, Any]:
    """Return aggregate paired replay gates without fixture or provider payloads."""
    limits = thresholds or VoiceTurnBenchmarkThresholds()
    all_observations = list(observations)
    grouped = {
        arm: [item for item in all_observations if item.arm == arm]
        for arm in (AUTOMATIC_ARM, MANUAL_ARM)
    }
    pair_arms: dict[tuple[int, int], set[str]] = {}
    for item in all_observations:
        _validate_arm(item.arm)
        pair_arms.setdefault((item.case_index, item.trial), set()).add(item.arm)
    paired_attempts = sum(arms == VALID_ARMS for arms in pair_arms.values())

    diagnostics = {
        arm: _arm_diagnostics(grouped[arm])
        for arm in (AUTOMATIC_ARM, MANUAL_ARM)
    }
    automatic = diagnostics[AUTOMATIC_ARM]
    manual = diagnostics[MANUAL_ARM]
    automatic_attempts = len(grouped[AUTOMATIC_ARM])
    manual_attempts = len(grouped[MANUAL_ARM])
    paired_coverage = (
        paired_attempts / max(automatic_attempts, manual_attempts)
        if automatic_attempts or manual_attempts
        else 0.0
    )
    gates = [
        _gate(
            "minimum_automatic_attempts",
            automatic_attempts >= limits.min_attempts_per_arm,
            automatic_attempts,
            f">= {limits.min_attempts_per_arm}",
        ),
        _gate(
            "minimum_manual_attempts",
            manual_attempts >= limits.min_attempts_per_arm,
            manual_attempts,
            f">= {limits.min_attempts_per_arm}",
        ),
        _gate(
            "minimum_paired_attempts",
            paired_attempts >= limits.min_paired_attempts,
            paired_attempts,
            f">= {limits.min_paired_attempts}",
        ),
        _gate(
            "paired_coverage",
            paired_coverage == 1.0,
            round(paired_coverage, 4),
            "= 1.0",
        ),
        _gate(
            "automatic_completion_rate",
            automatic["completion_rate"] >= limits.completion_rate,
            automatic["completion_rate"],
            f">= {limits.completion_rate}",
        ),
        _gate(
            "manual_completion_rate",
            manual["completion_rate"] >= limits.completion_rate,
            manual["completion_rate"],
            f">= {limits.completion_rate}",
        ),
        _gate(
            "automatic_latency_coverage",
            _latency_coverage(grouped[AUTOMATIC_ARM]) >= limits.latency_coverage,
            round(_latency_coverage(grouped[AUTOMATIC_ARM]), 4),
            f">= {limits.latency_coverage}",
        ),
        _gate(
            "manual_latency_coverage",
            _latency_coverage(grouped[MANUAL_ARM]) >= limits.latency_coverage,
            round(_latency_coverage(grouped[MANUAL_ARM]), 4),
            f">= {limits.latency_coverage}",
        ),
        _gate(
            "manual_activity_end_latency_coverage",
            _activity_latency_coverage(grouped[MANUAL_ARM])
            >= limits.latency_coverage,
            round(_activity_latency_coverage(grouped[MANUAL_ARM]), 4),
            f">= {limits.latency_coverage}",
        ),
        _gate(
            "manual_premature_responses",
            manual["premature_responses"]
            <= limits.max_manual_premature_responses,
            manual["premature_responses"],
            f"<= {limits.max_manual_premature_responses}",
        ),
        _gate(
            "manual_interruption_events",
            manual["interruption_events"]
            <= limits.max_manual_interruption_events,
            manual["interruption_events"],
            f"<= {limits.max_manual_interruption_events}",
        ),
        _gate(
            "automatic_errors",
            automatic["errors"] <= limits.max_errors,
            automatic["errors"],
            f"<= {limits.max_errors}",
        ),
        _gate(
            "manual_errors",
            manual["errors"] <= limits.max_errors,
            manual["errors"],
            f"<= {limits.max_errors}",
        ),
        _gate(
            "automatic_latency_p95_ms",
            _at_most(
                automatic["speech_end_to_first_audio_p95_ms"],
                limits.automatic_latency_p95_ms,
            ),
            automatic["speech_end_to_first_audio_p95_ms"],
            f"<= {limits.automatic_latency_p95_ms}",
        ),
        _gate(
            "automatic_latency_max_ms",
            _at_most(
                automatic["speech_end_to_first_audio_max_ms"],
                limits.automatic_latency_max_ms,
            ),
            automatic["speech_end_to_first_audio_max_ms"],
            f"<= {limits.automatic_latency_max_ms}",
        ),
        _gate(
            "manual_latency_p95_ms",
            _at_most(
                manual["speech_end_to_first_audio_p95_ms"],
                limits.manual_latency_p95_ms,
            ),
            manual["speech_end_to_first_audio_p95_ms"],
            f"<= {limits.manual_latency_p95_ms}",
        ),
        _gate(
            "manual_latency_max_ms",
            _at_most(
                manual["speech_end_to_first_audio_max_ms"],
                limits.manual_latency_max_ms,
            ),
            manual["speech_end_to_first_audio_max_ms"],
            f"<= {limits.manual_latency_max_ms}",
        ),
    ]
    return {
        "status": "pass" if all(gate["passed"] for gate in gates) else "fail",
        "decision_scope": OFFLINE_DIAGNOSTIC_SCOPE,
        "release_authorized": False,
        "sample": {
            "attempts": len(all_observations),
            "automatic_attempts": automatic_attempts,
            "manual_attempts": manual_attempts,
            "paired_attempts": paired_attempts,
        },
        "diagnostics": diagnostics,
        "gates": gates,
    }


def _transform_pcm16(
    source: bytes,
    *,
    gain: float,
    noise_peak: int,
    noise_seed: int,
) -> bytes:
    samples = array("h")
    samples.frombytes(source)
    if sys.byteorder != "little":
        samples.byteswap()
    rng = random.Random(noise_seed)
    for index, sample in enumerate(samples):
        noise = rng.randint(-noise_peak, noise_peak) if noise_peak else 0
        samples[index] = max(-32_768, min(32_767, round(sample * gain) + noise))
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def _arm_diagnostics(
    observations: list[VoiceTurnObservation],
) -> dict[str, int | float | None]:
    attempts = len(observations)
    completed = sum(item.turn_complete for item in observations)
    speech_latencies = [
        item.first_audio_after_speech_end_ms
        for item in observations
        if item.first_audio_after_speech_end_ms is not None
        and item.first_audio_after_speech_end_ms >= 0
    ]
    activity_latencies = [
        item.first_audio_after_activity_end_ms
        for item in observations
        if item.first_audio_after_activity_end_ms is not None
        and item.first_audio_after_activity_end_ms >= 0
    ]
    return {
        "completed_turns": completed,
        "completion_rate": round(completed / attempts, 4) if attempts else 0.0,
        "premature_responses": sum(
            item.first_audio_after_speech_end_ms is not None
            and item.first_audio_after_speech_end_ms < 0
            for item in observations
        ),
        "interruption_events": sum(item.interruption_events for item in observations),
        "speech_end_to_first_audio_p95_ms": _percentile(speech_latencies, 0.95),
        "speech_end_to_first_audio_max_ms": max(speech_latencies)
        if speech_latencies
        else None,
        "activity_end_to_first_audio_p95_ms": _percentile(
            activity_latencies,
            0.95,
        ),
        "activity_end_to_first_audio_max_ms": max(activity_latencies)
        if activity_latencies
        else None,
        "errors": sum(item.error is not None for item in observations),
        "error_counts": dict(sorted(Counter(
            _safe_error_code(item.error)
            for item in observations
            if item.error is not None
        ).items())),
    }


def _latency_coverage(observations: list[VoiceTurnObservation]) -> float:
    if not observations:
        return 0.0
    covered = sum(
        item.first_audio_after_speech_end_ms is not None
        and item.first_audio_after_speech_end_ms >= 0
        for item in observations
    )
    return covered / len(observations)


def _activity_latency_coverage(
    observations: list[VoiceTurnObservation],
) -> float:
    if not observations:
        return 0.0
    covered = sum(
        item.first_audio_after_activity_end_ms is not None
        and item.first_audio_after_activity_end_ms >= 0
        for item in observations
    )
    return covered / len(observations)


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, (round(percentile * 100) * len(ordered) + 99) // 100 - 1)
    return ordered[index]


def _at_most(value: object, limit: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value <= limit


def _gate(name: str, passed: bool, observed: object, requirement: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "observed": observed,
        "requirement": requirement,
    }


def _required_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _bounded_int(
    data: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = data.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _validate_arm(arm: str) -> None:
    if arm not in VALID_ARMS:
        raise ValueError("arm must be automatic or manual")


def _safe_error_code(error: str) -> str:
    return error if error in SAFE_ERROR_CODES else "other"
