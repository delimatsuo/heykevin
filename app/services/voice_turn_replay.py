"""Offline, payload-safe replay primitives for Gemini turn detection experiments."""

from __future__ import annotations

from array import array
import base64
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
DEVELOPER_PROVIDER = "developer"
VERTEX_PROVIDER = "vertex"
VALID_PROVIDERS = {DEVELOPER_PROVIDER, VERTEX_PROVIDER}
DEVELOPER_MODEL = "gemini-3.1-flash-live-preview"
VERTEX_MODEL = "gemini-live-2.5-flash-native-audio"
VERTEX_LOCATION = "us-central1"
COLD_SINGLE_TURN_SCOPE = "cold_single_turn"
GATE_0B_RENDERED_ACTIVITY_SCHEMA_ID = "gate_0b_rendered_activity_v1"
GATE_0B_MAX_ACTIVITIES_PER_SESSION = 10
GATE_0B_MAX_SESSION_DURATION_MS = 120_000
GATE_0B_FRAME_PATTERN_MS = {20, 30, 40}
GATE_0B_CODEC_REMAINDER_BYTES = 2
AUTOMATIC_LATENCY_P95_LIMIT_MS = 1_500
AUTOMATIC_LATENCY_MAX_LIMIT_MS = 2_500
MANUAL_LATENCY_P95_LIMIT_MS = 1_500
MANUAL_LATENCY_MAX_LIMIT_MS = 2_500
APPROVED_CONNECTION_SMOKE_MANIFEST_SHA256 = "".join(
    (
        "ae84a042",
        "8697119b",
        "c794e70d",
        "661bfda9",
        "4e3cbc7f",
        "c5f39628",
        "3b72e299",
        "95790e5c",
    )
)
APPROVED_CONNECTION_SMOKE_CORPUS_SHA256 = "".join(
    (
        "b0c23e5a",
        "964427d7",
        "e5119391",
        "3c760e39",
        "143d957a",
        "0660e9fe",
        "8ae3ad47",
        "ea515636",
    )
)
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
PROJECT_PATTERN = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")


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
class Gate0BReplayActivity:
    activity_ordinal: int
    source_path: Path
    source_sha256: str
    twilio_roundtrip_sha256: str
    duration_ms: int
    speech_start_ms: int
    speech_end_ms: int
    scenario_tags: tuple[str, ...] = ()
    fresh_restart_after: bool = False

    def __post_init__(self) -> None:
        _validate_gate0b_ordinal(self.activity_ordinal, label="activity_ordinal")
        _validate_gate0b_source_fields(
            self.source_path,
            self.source_sha256,
            self.twilio_roundtrip_sha256,
            self.duration_ms,
        )
        if (
            isinstance(self.speech_start_ms, bool)
            or not isinstance(self.speech_start_ms, int)
            or isinstance(self.speech_end_ms, bool)
            or not isinstance(self.speech_end_ms, int)
            or self.speech_start_ms < 0
            or self.speech_end_ms <= self.speech_start_ms
            or self.speech_end_ms > self.duration_ms
        ):
            raise ValueError("Gate 0B speech boundaries are invalid")
        if not isinstance(self.scenario_tags, tuple) or any(
            not isinstance(tag, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,63}", tag)
            for tag in self.scenario_tags
        ):
            raise ValueError("Gate 0B scenario tags are invalid")
        if not isinstance(self.fresh_restart_after, bool):
            raise TypeError("fresh_restart_after must be boolean")


@dataclass(frozen=True, slots=True)
class Gate0BNoSpeechWindow:
    window_ordinal: int
    source_path: Path
    source_sha256: str
    twilio_roundtrip_sha256: str
    duration_ms: int

    def __post_init__(self) -> None:
        _validate_gate0b_ordinal(self.window_ordinal, label="window_ordinal")
        _validate_gate0b_source_fields(
            self.source_path,
            self.source_sha256,
            self.twilio_roundtrip_sha256,
            self.duration_ms,
        )


@dataclass(frozen=True, slots=True)
class Gate0BReplaySession:
    session_ordinal: int
    activities: tuple[Gate0BReplayActivity, ...]
    inter_activity_gap_ms: int = 500

    def __post_init__(self) -> None:
        _validate_gate0b_ordinal(self.session_ordinal, label="session_ordinal")
        if (
            not isinstance(self.activities, tuple)
            or not 1 <= len(self.activities) <= GATE_0B_MAX_ACTIVITIES_PER_SESSION
            or not all(isinstance(activity, Gate0BReplayActivity) for activity in self.activities)
        ):
            raise ValueError("Gate 0B sessions require at most 10 typed activities")
        ordinals = [activity.activity_ordinal for activity in self.activities]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("Gate 0B session activity ordinals must be unique")
        if sum(activity.fresh_restart_after for activity in self.activities) > 1:
            raise ValueError("Gate 0B session permits at most one fresh restart")
        if self.activities[-1].fresh_restart_after:
            raise ValueError("Gate 0B fresh restart requires a following activity")
        if (
            isinstance(self.inter_activity_gap_ms, bool)
            or not isinstance(self.inter_activity_gap_ms, int)
            or not 0 <= self.inter_activity_gap_ms <= 5_000
        ):
            raise ValueError("Gate 0B inter-activity gap is invalid")
        total = sum(activity.duration_ms for activity in self.activities) + (
            max(0, len(self.activities) - 1) * self.inter_activity_gap_ms
        )
        if total > GATE_0B_MAX_SESSION_DURATION_MS:
            raise ValueError("Gate 0B session exceeds the 120 second bound")


@dataclass(frozen=True, slots=True)
class RenderedGate0BActivity:
    schema_id: str
    pcm16k: bytes
    duration_ms: int
    speech_start_ms: int | None
    speech_end_ms: int | None


@dataclass(frozen=True, slots=True)
class Gate0BReplayInput:
    kind: str
    at_ms: int
    epoch: int
    activity_ordinal: int | None
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
            if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9_]+", source_id):
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
                isinstance(value, bool) or not isinstance(value, int) or value not in {10, 20, 30}
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


def compute_gate0b_roundtrip_sha256(pcm16k: bytes) -> str:
    """Return the exact PCM16 16k -> Twilio mulaw 8k -> PCM16 16k digest."""
    if not isinstance(pcm16k, bytes) or not pcm16k or len(pcm16k) % 2:
        raise ValueError("Gate 0B PCM16 input must contain complete samples")
    pcm8k, _ = audioop.ratecv(pcm16k, 2, 1, 16_000, 8_000, None)
    mulaw8 = audioop.lin2ulaw(pcm8k, 2)
    return hashlib.sha256(mulaw_to_pcm16k(mulaw8)).hexdigest()


def render_gate0b_activity(activity: Gate0BReplayActivity) -> RenderedGate0BActivity:
    """Render one validated Gate 0B activity through the production codec transform."""
    if not isinstance(activity, Gate0BReplayActivity):
        raise TypeError("activity must be a Gate0BReplayActivity")
    pcm16k = _render_gate0b_source(activity)
    return RenderedGate0BActivity(
        schema_id=GATE_0B_RENDERED_ACTIVITY_SCHEMA_ID,
        pcm16k=pcm16k,
        duration_ms=activity.duration_ms,
        speech_start_ms=activity.speech_start_ms,
        speech_end_ms=activity.speech_end_ms,
    )


def render_gate0b_no_speech_window(
    window: Gate0BNoSpeechWindow,
) -> RenderedGate0BActivity:
    """Render one validated no-speech window without inventing activity boundaries."""
    if not isinstance(window, Gate0BNoSpeechWindow):
        raise TypeError("window must be a Gate0BNoSpeechWindow")
    pcm16k = _render_gate0b_source(window)
    return RenderedGate0BActivity(
        schema_id=GATE_0B_RENDERED_ACTIVITY_SCHEMA_ID,
        pcm16k=pcm16k,
        duration_ms=window.duration_ms,
        speech_start_ms=None,
        speech_end_ms=None,
    )


def build_gate0b_activity_inputs(
    rendered: RenderedGate0BActivity,
    *,
    activity_ordinal: int | None,
    epoch: int,
    start_at_ms: int,
    frame_pattern_ms: tuple[int, ...],
    include_activity_boundaries: bool = True,
) -> tuple[Gate0BReplayInput, ...]:
    """Build a deterministic, local-only Gate 0B paced activity schedule."""
    if not isinstance(rendered, RenderedGate0BActivity):
        raise TypeError("rendered must be a RenderedGate0BActivity")
    if rendered.schema_id != GATE_0B_RENDERED_ACTIVITY_SCHEMA_ID:
        raise ValueError("unsupported Gate 0B rendered activity schema")
    if activity_ordinal is not None:
        _validate_gate0b_ordinal(activity_ordinal, label="activity_ordinal")
    if include_activity_boundaries and activity_ordinal is None:
        raise ValueError("activity boundaries require an activity ordinal")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or not 1 <= epoch <= 1_000:
        raise ValueError("Gate 0B epoch is invalid")
    if (
        isinstance(start_at_ms, bool)
        or not isinstance(start_at_ms, int)
        or not 0 <= start_at_ms <= GATE_0B_MAX_SESSION_DURATION_MS
    ):
        raise ValueError("Gate 0B start time is invalid")
    _validate_gate0b_frame_pattern(frame_pattern_ms)
    nominal_bytes = rendered.duration_ms * 16_000 * 2 // 1_000
    if len(rendered.pcm16k) != nominal_bytes - GATE_0B_CODEC_REMAINDER_BYTES:
        raise ValueError("Gate 0B rendered duration does not match PCM length")

    events: list[Gate0BReplayInput] = []
    if include_activity_boundaries:
        events.append(
            Gate0BReplayInput(
                kind="caller_activity_start",
                at_ms=start_at_ms,
                epoch=epoch,
                activity_ordinal=activity_ordinal,
            )
        )
    position = 0
    elapsed_ms = 0
    pattern_index = 0
    bytes_per_ms = 16_000 * 2 // 1_000
    while position < len(rendered.pcm16k):
        requested_ms = frame_pattern_ms[pattern_index % len(frame_pattern_ms)]
        pattern_index += 1
        duration_ms = min(requested_ms, rendered.duration_ms - elapsed_ms)
        chunk_end = position + duration_ms * bytes_per_ms
        if elapsed_ms + duration_ms == rendered.duration_ms:
            chunk_end = len(rendered.pcm16k)
        chunk = rendered.pcm16k[position:chunk_end]
        events.append(
            Gate0BReplayInput(
                kind="audio",
                at_ms=start_at_ms + elapsed_ms,
                epoch=epoch,
                activity_ordinal=activity_ordinal,
                audio=chunk,
                duration_ms=duration_ms,
            )
        )
        position += len(chunk)
        elapsed_ms += duration_ms
    if elapsed_ms != rendered.duration_ms:
        raise ValueError("Gate 0B rendered audio did not fill its nominal duration")
    if include_activity_boundaries:
        events.append(
            Gate0BReplayInput(
                kind="caller_activity_end",
                at_ms=start_at_ms + rendered.duration_ms,
                epoch=epoch,
                activity_ordinal=activity_ordinal,
            )
        )
    return tuple(events)


def build_gate0b_session_inputs(
    session: Gate0BReplaySession,
    *,
    frame_pattern_ms: tuple[int, ...],
) -> tuple[Gate0BReplayInput, ...]:
    """Build a bounded multi-activity schedule with local interaction expectations."""
    if not isinstance(session, Gate0BReplaySession):
        raise TypeError("session must be a Gate0BReplaySession")
    _validate_gate0b_frame_pattern(frame_pattern_ms)
    events: list[Gate0BReplayInput] = []
    epoch = 1
    start_at_ms = 0
    for index, activity in enumerate(session.activities):
        rendered = render_gate0b_activity(activity)
        events.extend(
            build_gate0b_activity_inputs(
                rendered,
                activity_ordinal=activity.activity_ordinal,
                epoch=epoch,
                start_at_ms=start_at_ms,
                frame_pattern_ms=frame_pattern_ms,
            )
        )
        activity_end_ms = start_at_ms + rendered.duration_ms
        marker_kinds = []
        if "synchronous_tool_use" in activity.scenario_tags:
            marker_kinds.append("expect_synchronous_tool")
        if "tool_cancellation_interruption" in activity.scenario_tags:
            marker_kinds.extend(("expect_tool_cancellation", "expect_interruption"))
        for kind in marker_kinds:
            events.append(
                Gate0BReplayInput(
                    kind=kind,
                    at_ms=activity_end_ms,
                    epoch=epoch,
                    activity_ordinal=activity.activity_ordinal,
                )
            )
        if activity.fresh_restart_after:
            epoch += 1
            events.append(
                Gate0BReplayInput(
                    kind="fresh_connection_restart",
                    at_ms=activity_end_ms,
                    epoch=epoch,
                    activity_ordinal=activity.activity_ordinal,
                )
            )
        if index < len(session.activities) - 1:
            start_at_ms = activity_end_ms + session.inter_activity_gap_ms
    if events and events[-1].at_ms > GATE_0B_MAX_SESSION_DURATION_MS:
        raise ValueError("Gate 0B schedule exceeds the session bound")
    return tuple(events)


def _render_gate0b_source(
    source: Gate0BReplayActivity | Gate0BNoSpeechWindow,
) -> bytes:
    path = source.source_path
    if path.is_symlink() or not path.is_file():
        raise ValueError("Gate 0B source must be a regular file")
    expected_bytes = source.duration_ms * 16_000 * 2 // 1_000
    if path.stat().st_size != expected_bytes:
        raise ValueError("Gate 0B source duration does not match PCM length")
    payload = path.read_bytes()
    if len(payload) != expected_bytes:
        raise ValueError("Gate 0B source duration does not match PCM length")
    if hashlib.sha256(payload).hexdigest() != source.source_sha256:
        raise ValueError("Gate 0B source digest mismatch")
    pcm8k, _ = audioop.ratecv(payload, 2, 1, 16_000, 8_000, None)
    mulaw8 = audioop.lin2ulaw(pcm8k, 2)
    rendered = mulaw_to_pcm16k(mulaw8)
    if len(rendered) != expected_bytes - GATE_0B_CODEC_REMAINDER_BYTES:
        raise ValueError("Gate 0B codec render length is not reproducible")
    if hashlib.sha256(rendered).hexdigest() != source.twilio_roundtrip_sha256:
        raise ValueError("Gate 0B Twilio roundtrip digest mismatch")
    return rendered


def _validate_gate0b_ordinal(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
        raise ValueError(f"{label} is outside its Gate 0B bound")
    return value


def _validate_gate0b_source_fields(
    source_path: Path,
    source_sha256: str,
    roundtrip_sha256: str,
    duration_ms: int,
) -> None:
    if not isinstance(source_path, Path):
        raise TypeError("Gate 0B source_path must be a Path")
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("Gate 0B source checksum must be SHA-256")
    if not re.fullmatch(r"[0-9a-f]{64}", roundtrip_sha256):
        raise ValueError("Gate 0B roundtrip checksum must be SHA-256")
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or not 20 <= duration_ms <= 30_000
    ):
        raise ValueError("Gate 0B duration is invalid")


def _validate_gate0b_frame_pattern(frame_pattern_ms: tuple[int, ...]) -> None:
    if (
        not isinstance(frame_pattern_ms, tuple)
        or not frame_pattern_ms
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value not in GATE_0B_FRAME_PATTERN_MS
            for value in frame_pattern_ms
        )
    ):
        raise ValueError("Gate 0B frame pattern supports only 20, 30, or 40 ms")


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
    source_start_sample = round(case.source_speech_start_ms * case.source_sample_rate_hz / 1_000)
    source_end_sample = round(case.source_speech_end_ms * case.source_sample_rate_hz / 1_000)
    if source_end_sample > source_samples:
        raise ValueError("speech boundary exceeds source duration")

    inter_silence_samples = case.inter_repeat_silence_ms * case.source_sample_rate_hz // 1_000
    inter_silence = b"\x00\x00" * inter_silence_samples
    parts = []
    for repetition in range(case.repetitions):
        if repetition:
            parts.append(inter_silence)
        parts.append(transformed)
    rendered_pcm = b"".join(parts)

    final_offset_samples = (case.repetitions - 1) * (source_samples + inter_silence_samples)
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
    speech_end_ms = round(final_speech_end_sample * 1_000 / case.source_sample_rate_hz)
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
        chunk_ms = rendered.frame_pattern_ms[pattern_index % len(rendered.frame_pattern_ms)]
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
    first_arms = [AUTOMATIC_ARM if index % 2 == 0 else MANUAL_ARM for index in range(len(pairs))]
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


def build_gemini_setup_message(
    model: str,
    *,
    arm: str,
    provider: str = DEVELOPER_PROVIDER,
    project: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    """Build provider setup for one explicit non-latest replay model ID."""
    _validate_arm(arm)
    _validate_provider(provider)
    if not MODEL_PATTERN.fullmatch(model) or "latest" in model or model.endswith("-exp"):
        raise ValueError("an explicit non-latest Gemini model ID is required")

    setup_model = f"models/{model}"
    if provider == VERTEX_PROVIDER:
        if (
            model != VERTEX_MODEL
            or not project
            or not PROJECT_PATTERN.fullmatch(project)
            or location != VERTEX_LOCATION
        ):
            raise ValueError("Vertex replay requires the precommitted model and scope")
        setup_model = f"projects/{project}/locations/{location}/publishers/google/models/{model}"

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
    generation_config: dict[str, Any] = {
        "responseModalities": ["AUDIO"],
        "maxOutputTokens": 120,
        "temperature": 0.4,
        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Puck"}}},
    }
    if provider == DEVELOPER_PROVIDER:
        generation_config["thinkingConfig"] = (
            {"thinkingLevel": "minimal"} if model.startswith("gemini-3") else {"thinkingBudget": 0}
        )

    return {
        "setup": {
            "model": setup_model,
            "generationConfig": generation_config,
            "systemInstruction": {
                "parts": [
                    {
                        "text": (
                            "You are a concise receptionist. Respond to the caller "
                            "in one short sentence."
                        )
                    }
                ]
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


def build_gemini_audio_message(
    audio: bytes,
    *,
    provider: str,
) -> dict[str, Any]:
    """Serialize one public-fixture PCM chunk for the selected provider."""
    _validate_provider(provider)
    blob = {
        "data": base64.b64encode(audio).decode("ascii"),
        "mimeType": "audio/pcm;rate=16000",
    }
    if provider == VERTEX_PROVIDER:
        realtime_input = {"mediaChunks": [blob]}
    else:
        realtime_input = {"audio": blob}
    return {"realtimeInput": realtime_input}


def build_gemini_activity_message(kind: str) -> dict[str, Any]:
    """Serialize an explicit manual activity boundary."""
    fields = {
        "activity_start": "activityStart",
        "activity_end": "activityEnd",
    }
    try:
        field = fields[kind]
    except KeyError as exc:
        raise ValueError("unsupported activity event") from exc
    return {"realtimeInput": {field: {}}}


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

    diagnostics = {arm: _arm_diagnostics(grouped[arm]) for arm in (AUTOMATIC_ARM, MANUAL_ARM)}
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
            _activity_latency_coverage(grouped[MANUAL_ARM]) >= limits.latency_coverage,
            round(_activity_latency_coverage(grouped[MANUAL_ARM]), 4),
            f">= {limits.latency_coverage}",
        ),
        _gate(
            "manual_premature_responses",
            manual["premature_responses"] <= limits.max_manual_premature_responses,
            manual["premature_responses"],
            f"<= {limits.max_manual_premature_responses}",
        ),
        _gate(
            "manual_interruption_events",
            manual["interruption_events"] <= limits.max_manual_interruption_events,
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
        "speech_end_to_first_audio_max_ms": max(speech_latencies) if speech_latencies else None,
        "activity_end_to_first_audio_p95_ms": _percentile(
            activity_latencies,
            0.95,
        ),
        "activity_end_to_first_audio_max_ms": max(activity_latencies)
        if activity_latencies
        else None,
        "errors": sum(item.error is not None for item in observations),
        "error_counts": dict(
            sorted(
                Counter(
                    _safe_error_code(item.error) for item in observations if item.error is not None
                ).items()
            )
        ),
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
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _validate_arm(arm: str) -> None:
    if arm not in VALID_ARMS:
        raise ValueError("arm must be automatic or manual")


def _validate_provider(provider: str) -> None:
    if provider not in VALID_PROVIDERS:
        raise ValueError("provider must be developer or vertex")


def _safe_error_code(error: str) -> str:
    return error if error in SAFE_ERROR_CODES else "other"
