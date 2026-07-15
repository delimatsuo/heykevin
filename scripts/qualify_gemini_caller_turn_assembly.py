#!/usr/bin/env python3
"""Dry-run-first Gemini Live caller-turn assembly qualification."""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.caller_turns import (  # noqa: E402
    CallerTurnAssembler,
    CallerTurnCompletionStatus,
    CallerTurnEventKind,
)
from app.services.gemini_turn_events import (  # noqa: E402
    GEMINI_RAW_MESSAGE_MAX_BYTES,
    GeminiTurnEventAdapter,
    GeminiTurnEventDecodeStatus,
    GeminiTurnEventRejectionCode,
)
from scripts.evaluate_caller_turn_assembly import EVIDENCE_FIELDS  # noqa: E402


REPORT_SCHEMA_VERSION = 1
PRODUCTION_PROJECT = "kevin-491315"
MAX_ATTEMPTS = 500
MAX_WALL_CLOCK_SECONDS = 4 * 60 * 60
MAX_SESSION_TIMEOUT_SECONDS = 120
MAX_COST_USD = 500.0
MAX_INPUT_FILE_BYTES = 2 * 1024 * 1024
MAX_AUDIO_FILE_BYTES = 10 * 1024 * 1024
MIN_EXECUTION_CASES = 200
MIN_LANGUAGE_CASES = 20
MIN_LANGUAGE_GROUPS = 8
OFFICIAL_ENDPOINTS = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent",
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA_PATTERN = re.compile(r"[0-9a-f]{40,64}")
MODEL_RESOURCE_PATTERN = re.compile(r"models/gemini-[a-z0-9][a-z0-9.-]*")
SAFE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-[A-Z]{2}|-[0-9]{3})?")
RUNNER_PATH = Path(__file__).resolve()
EVALUATOR_PATH = REPO_ROOT / "scripts/evaluate_caller_turn_assembly.py"
PIPELINE_PATH = REPO_ROOT / "app/services/gemini_pipeline.py"
BEHAVIOR_PROJECTION_FIELDS = {
    "api_version",
    "endpoint",
    "model_resource",
    "system_instruction_sha256",
    "synthetic_prompt_fixture_sha256",
    "generation_config",
    "input_audio_transcription",
    "output_audio_transcription",
    "realtime_input_config",
    "tool_declarations_sha256",
    "tool_response_policy",
    "reconnect_policy",
    "turn_assembly_policy",
    "websocket_policy",
}


class QualificationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class QualificationConfig:
    execute: bool
    model_resource: str
    api_version: str
    endpoint: str
    project: str
    credential_ref: str
    manifest_path: Path
    manifest_sha256: str
    setup_path: Path
    setup_file_sha256: str
    canonical_setup_sha256: str
    deviations_path: Path
    deviations_sha256: str
    source_sha: str
    attempt_cap: int
    wall_clock_cap_seconds: int
    session_timeout_seconds: int
    max_cost_usd: float
    max_cost_per_attempt_usd: float


@dataclass(frozen=True, slots=True)
class AudioManifestSummary:
    collection_status: str
    case_count: int
    speaker_count: int
    language_counts: dict[str, int]

    @property
    def execution_ready(self) -> bool:
        return self.collection_status == "ready"

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "collection_status": self.collection_status,
            "case_count": self.case_count,
            "speaker_count": self.speaker_count,
            "language_counts": dict(sorted(self.language_counts.items())),
            "execution_ready": self.execution_ready,
        }


@dataclass(frozen=True, slots=True)
class SessionAttemptResult:
    complete: bool
    error_code: str | None
    reconnect_count: int
    event_type_counts: dict[str, int]
    decode_rejection_counts: dict[str, int]
    turn_status_counts: dict[str, int]
    turn_close_reason_counts: dict[str, int]

    def redacted_report_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "error_code": self.error_code,
            "reconnect_count": self.reconnect_count,
            "event_type_counts": dict(sorted(self.event_type_counts.items())),
            "decode_rejection_counts": dict(
                sorted(self.decode_rejection_counts.items())
            ),
            "turn_status_counts": dict(sorted(self.turn_status_counts.items())),
            "turn_close_reason_counts": dict(
                sorted(self.turn_close_reason_counts.items())
            ),
        }


def canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def canonicalize_qualification_setup(
    document: object,
    *,
    setup_file_sha256: str,
    deviations_sha256: str,
) -> dict[str, Any]:
    """Project a complete provider setup into a non-sensitive canonical document."""
    _require_sha256(setup_file_sha256, "setup_digest_invalid")
    _require_sha256(deviations_sha256, "deviations_digest_invalid")
    if not isinstance(document, dict):
        raise QualificationError("setup_document_invalid")
    required = {
        "api_version",
        "endpoint",
        "setup",
        "synthetic_prompt_fixture_sha256",
        "tool_response_policy",
        "reconnect_policy",
        "turn_assembly_policy",
        "websocket_policy",
        "runner_identity",
        "evaluator_identity",
        "immutable_pipeline_identity",
        "immutable_pipeline_setup",
    }
    if set(document) != required:
        raise QualificationError("setup_document_invalid")
    if document["api_version"] != "v1beta" or document["endpoint"] not in (
        OFFICIAL_ENDPOINTS
    ):
        raise QualificationError("setup_document_invalid")

    provider_setup = document["setup"]
    if not isinstance(provider_setup, dict):
        raise QualificationError("setup_document_invalid")
    setup_required = {
        "model",
        "generation_config",
        "system_instruction",
        "input_audio_transcription",
        "output_audio_transcription",
        "realtime_input_config",
        "tools",
    }
    if set(provider_setup) != setup_required:
        raise QualificationError("setup_document_invalid")
    _validate_provider_setup(provider_setup)

    prompt_fixture_digest = document["synthetic_prompt_fixture_sha256"]
    _require_sha256(prompt_fixture_digest, "setup_document_invalid")
    system_instruction = provider_setup["system_instruction"]
    tools = provider_setup["tools"]
    system_instruction_digest = canonical_json_sha256(system_instruction)
    tools_digest = canonical_json_sha256(tools)

    reconnect_policy = _require_object(document, "reconnect_policy")
    if set(reconnect_policy) != {
        "max_attempts",
        "context_restoration",
        "retry_backoff_ms",
    }:
        raise QualificationError("setup_document_invalid")
    if not _bounded_int(reconnect_policy.get("max_attempts"), 0, 3):
        raise QualificationError("setup_document_invalid")
    if not isinstance(reconnect_policy.get("context_restoration"), str):
        raise QualificationError("setup_document_invalid")
    retry_backoff = reconnect_policy.get("retry_backoff_ms")
    if (
        not isinstance(retry_backoff, list)
        or any(not _bounded_int(value, 0, 10_000) for value in retry_backoff)
        or len(retry_backoff) != reconnect_policy["max_attempts"]
    ):
        raise QualificationError("setup_document_invalid")

    turn_policy = _require_object(document, "turn_assembly_policy")
    if set(turn_policy) != {"quiescence_ms"} or not _bounded_int(
        turn_policy.get("quiescence_ms"), 1, 5_000
    ):
        raise QualificationError("setup_document_invalid")
    websocket_policy = _validate_websocket_policy(
        _require_object(document, "websocket_policy")
    )
    runner_identity = _validate_source_identity(document.get("runner_identity"))
    evaluator_identity = _validate_source_identity(document.get("evaluator_identity"))
    pipeline_identity = _validate_source_identity(
        document.get("immutable_pipeline_identity")
    )
    immutable_pipeline_setup = document.get("immutable_pipeline_setup")
    _validate_behavior_projection(immutable_pipeline_setup)
    tool_response_policy = document["tool_response_policy"]
    if (
        not isinstance(tool_response_policy, str)
        or not SAFE_ID_PATTERN.fullmatch(tool_response_policy)
    ):
        raise QualificationError("setup_document_invalid")
    for configuration in (
        provider_setup["generation_config"],
        provider_setup["input_audio_transcription"],
        provider_setup["output_audio_transcription"],
        provider_setup["realtime_input_config"],
        reconnect_policy,
        turn_policy,
        websocket_policy,
        immutable_pipeline_setup,
    ):
        _assert_payload_free_configuration(configuration)

    return {
        "schema_version": 1,
        "api_version": document["api_version"],
        "endpoint": document["endpoint"],
        "model_resource": provider_setup["model"],
        "system_instruction_sha256": system_instruction_digest,
        "synthetic_prompt_fixture_sha256": prompt_fixture_digest,
        "generation_config": provider_setup["generation_config"],
        "input_audio_transcription": provider_setup["input_audio_transcription"],
        "output_audio_transcription": provider_setup["output_audio_transcription"],
        "realtime_input_config": provider_setup["realtime_input_config"],
        "tool_declarations_sha256": tools_digest,
        "tool_response_policy": tool_response_policy,
        "reconnect_policy": reconnect_policy,
        "turn_assembly_policy": turn_policy,
        "websocket_policy": websocket_policy,
        "runner_identity": runner_identity,
        "evaluator_identity": evaluator_identity,
        "immutable_pipeline_identity": pipeline_identity,
        "immutable_pipeline_setup": immutable_pipeline_setup,
        "provider_setup_file_sha256": setup_file_sha256,
        "deviations_sha256": deviations_sha256,
    }


def validate_audio_manifest(
    path: str | Path,
    *,
    require_execution_ready: bool,
) -> AudioManifestSummary:
    manifest_path = Path(path)
    manifest = _load_bounded_json(manifest_path, "manifest_invalid")
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise QualificationError("manifest_invalid")
    status = manifest.get("collection_status")
    if status not in {"pending", "ready"}:
        raise QualificationError("manifest_provenance_invalid")
    policy = manifest.get("provenance_policy")
    if policy != {
        "synthetic_scripts_only": True,
        "purpose_recorded_adult_speakers_only": True,
        "real_call_data_prohibited": True,
        "production_audio_prohibited": True,
    }:
        raise QualificationError("manifest_provenance_invalid")
    speakers = manifest.get("speakers")
    cases = manifest.get("cases")
    if not isinstance(speakers, list) or not isinstance(cases, list):
        raise QualificationError("manifest_invalid")
    if status == "pending" and (speakers or cases):
        raise QualificationError("manifest_invalid")

    speaker_ids = _validate_speakers(speakers)
    language_counts: Counter[str] = Counter()
    case_ids: set[str] = set()
    fixture_root = manifest_path.resolve().parent
    for case in cases:
        language = _validate_audio_case(
            case,
            fixture_root=fixture_root,
            speaker_ids=speaker_ids,
            case_ids=case_ids,
        )
        language_counts[language] += 1

    summary = AudioManifestSummary(
        collection_status=status,
        case_count=len(cases),
        speaker_count=len(speakers),
        language_counts=dict(language_counts),
    )
    if require_execution_ready:
        if status != "ready":
            raise QualificationError("manifest_collection_pending")
        if len(cases) < MIN_EXECUTION_CASES:
            raise QualificationError("manifest_coverage_incomplete")
        qualifying_languages = sum(
            count >= MIN_LANGUAGE_CASES for count in language_counts.values()
        )
        if qualifying_languages < MIN_LANGUAGE_GROUPS:
            raise QualificationError("manifest_coverage_incomplete")
    return summary


def build_preregistration(config: QualificationConfig) -> dict[str, Any]:
    _validate_config(config)
    manifest_digest = _file_sha256(config.manifest_path, "manifest_unavailable")
    if manifest_digest != config.manifest_sha256:
        raise QualificationError("manifest_digest_mismatch")
    setup_digest = _file_sha256(config.setup_path, "setup_unavailable")
    if setup_digest != config.setup_file_sha256:
        raise QualificationError("setup_file_digest_mismatch")
    deviations_digest = _file_sha256(
        config.deviations_path, "deviations_unavailable"
    )
    if deviations_digest != config.deviations_sha256:
        raise QualificationError("deviations_digest_mismatch")

    manifest = validate_audio_manifest(
        config.manifest_path,
        require_execution_ready=config.execute,
    )
    if config.execute and config.attempt_cap < manifest.case_count:
        raise QualificationError("attempt_cap_incomplete")
    setup_document = _load_bounded_json(config.setup_path, "setup_document_invalid")
    canonical_setup = canonicalize_qualification_setup(
        setup_document,
        setup_file_sha256=config.setup_file_sha256,
        deviations_sha256=config.deviations_sha256,
    )
    _validate_setup_identity(canonical_setup, config)
    if canonical_json_sha256(canonical_setup) != config.canonical_setup_sha256:
        raise QualificationError("canonical_setup_digest_mismatch")
    deviations = _load_and_validate_deviations(config.deviations_path)
    _validate_deviation_coverage(canonical_setup, deviations)

    return {
        "schema_version": 1,
        "execution_requested": config.execute,
        "source_sha": config.source_sha,
        "model_resource": config.model_resource,
        "api_version": config.api_version,
        "endpoint": config.endpoint,
        "project": config.project,
        "credential_ref": config.credential_ref,
        "manifest_sha256": config.manifest_sha256,
        "manifest": manifest.to_report_dict(),
        "provider_setup_file_sha256": config.setup_file_sha256,
        "canonical_setup_sha256": config.canonical_setup_sha256,
        "canonical_setup": canonical_setup,
        "deviations_sha256": config.deviations_sha256,
        "deviations": deviations,
        "caps": {
            "attempts": config.attempt_cap,
            "wall_clock_seconds": config.wall_clock_cap_seconds,
            "session_timeout_seconds": config.session_timeout_seconds,
            "max_cost_usd": config.max_cost_usd,
            "max_cost_per_attempt_usd": config.max_cost_per_attempt_usd,
        },
    }


async def run_qualification(
    config: QualificationConfig,
    *,
    connect: Callable[..., Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    registration = build_preregistration(config)
    if not config.execute:
        ready = registration["manifest"]["execution_ready"]
        return _qualification_report(
            status="dry_run_ready" if ready else "dry_run_blocked",
            registration=registration,
            failure_counts=(
                Counter() if ready else Counter({"manifest_collection_pending": 1})
            ),
        )

    environment = os.environ if environ is None else environ
    try:
        credential = environment[config.credential_ref]
    except KeyError:
        return _qualification_report(
            status="execution_blocked",
            registration=registration,
            failure_counts=Counter({"credential_unavailable": 1}),
        )
    if not credential:
        return _qualification_report(
            status="execution_blocked",
            registration=registration,
            failure_counts=Counter({"credential_unavailable": 1}),
        )
    connector = connect or _real_connect
    setup_document = _load_bounded_json(config.setup_path, "setup_document_invalid")
    provider_setup = setup_document["setup"]
    manifest_document = _load_bounded_json(config.manifest_path, "manifest_invalid")
    cases = manifest_document["cases"][: config.attempt_cap]
    attempts: list[SessionAttemptResult] = []
    started = time.monotonic()
    wall_clock_reached = False
    for case in cases:
        if time.monotonic() - started >= config.wall_clock_cap_seconds:
            wall_clock_reached = True
            break
        audio = _load_case_audio(config.manifest_path, case)
        attempts.append(
            await run_session_attempt(
                endpoint=config.endpoint,
                credential=credential,
                provider_setup=provider_setup,
                audio_bytes=audio,
                connect=connector,
                websocket_policy=registration["canonical_setup"]["websocket_policy"],
                quiescence_ms=registration["canonical_setup"]["turn_assembly_policy"][
                    "quiescence_ms"
                ],
                session_timeout_seconds=config.session_timeout_seconds,
                max_reconnect_attempts=registration["canonical_setup"][
                    "reconnect_policy"
                ]["max_attempts"],
            )
        )
        if not attempts[-1].complete:
            break
    return _aggregate_execution_report(
        registration,
        attempts,
        expected_attempts=len(cases),
        wall_clock_reached=wall_clock_reached,
    )


async def run_session_attempt(
    *,
    endpoint: str,
    credential: str,
    provider_setup: dict[str, Any],
    audio_bytes: bytes,
    connect: Callable[..., Any],
    websocket_policy: dict[str, Any],
    quiescence_ms: int,
    session_timeout_seconds: float,
    max_reconnect_attempts: int,
) -> SessionAttemptResult:
    """Run one injected-WebSocket attempt and retain aggregate enums only."""
    adapter = GeminiTurnEventAdapter()
    assembler = CallerTurnAssembler(active_epoch=1, quiescence_ms=quiescence_ms)
    event_counts: Counter[str] = Counter()
    rejection_counts: Counter[str] = Counter()
    turn_statuses: Counter[str] = Counter()
    close_reasons: Counter[str] = Counter()
    sequence = 1
    epoch = 1
    at_ms = 0
    reconnect_count = 0
    error_code: str | None = None
    emitted_turns = []

    def emit_lifecycle(
        kind: CallerTurnEventKind,
        *,
        event_epoch: int | None = None,
    ) -> None:
        nonlocal at_ms, sequence
        at_ms += 1
        event = adapter.adapt_lifecycle(
            kind,
            at_ms=at_ms,
            sequence=sequence,
            epoch=epoch if event_epoch is None else event_epoch,
        )
        sequence += 1
        event_counts[event.kind.value] += 1
        emitted_turns.extend(assembler.ingest(event))

    while True:
        try:
            timeout = min(
                max(0.001, float(session_timeout_seconds) / 2),
                float(websocket_policy["setup_timeout_seconds"]),
            )
            async with asyncio.timeout(session_timeout_seconds):
                async with connect(
                    endpoint,
                    additional_headers={"x-goog-api-key": credential},
                    max_size=websocket_policy["max_message_bytes"],
                    open_timeout=websocket_policy["open_timeout_seconds"],
                    ping_interval=websocket_policy["ping_interval_seconds"],
                    ping_timeout=websocket_policy["ping_timeout_seconds"],
                    close_timeout=websocket_policy["close_timeout_seconds"],
                ) as websocket:
                    await websocket.send(json.dumps({"setup": provider_setup}))
                    try:
                        acknowledgement = json.loads(
                            await asyncio.wait_for(websocket.recv(), timeout=timeout)
                        )
                    except TimeoutError:
                        error_code = "setup_timeout"
                        break
                    except (TypeError, ValueError, json.JSONDecodeError):
                        error_code = "setup_rejected"
                        break
                    if not isinstance(acknowledgement, dict) or (
                        "setupComplete" not in acknowledgement
                    ):
                        error_code = "setup_rejected"
                        break
                    if reconnect_count == 0:
                        encoded_audio = base64.b64encode(audio_bytes).decode("ascii")
                        await websocket.send(
                            json.dumps(
                                {
                                    "realtime_input": {
                                        "audio": {
                                            "data": encoded_audio,
                                            "mime_type": "audio/pcm;rate=16000",
                                        }
                                    }
                                }
                            )
                        )
                    async for raw_message in websocket:
                        at_ms += 10
                        if not isinstance(raw_message, (str, bytes)):
                            error_code = "malformed_message"
                            break
                        raw_bytes = (
                            raw_message.encode("utf-8")
                            if isinstance(raw_message, str)
                            else raw_message
                        )
                        if len(raw_bytes) > GEMINI_RAW_MESSAGE_MAX_BYTES:
                            error_code = "oversized_message"
                            break
                        try:
                            message = json.loads(raw_bytes)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            error_code = "malformed_message"
                            break
                        batch = adapter.adapt_message(
                            message,
                            at_ms=at_ms,
                            first_sequence=sequence,
                            epoch=epoch,
                        )
                        sequence += max(1, len(batch.events))
                        if batch.status is GeminiTurnEventDecodeStatus.REJECTED:
                            code = batch.rejection_code or (
                                GeminiTurnEventRejectionCode.MALFORMED_MESSAGE
                            )
                            rejection_counts[code.value] += 1
                            continue
                        for event in batch.events:
                            event_counts[event.kind.value] += 1
                            emitted_turns.extend(assembler.ingest(event))
                    if error_code is not None:
                        emit_lifecycle(CallerTurnEventKind.PIPELINE_STOPPED)
                        break
                emitted_turns.extend(assembler.advance_time(at_ms + quiescence_ms))
                at_ms += quiescence_ms
                emit_lifecycle(CallerTurnEventKind.CONNECTION_CLOSED)
                break
        except asyncio.CancelledError:
            emit_lifecycle(CallerTurnEventKind.PIPELINE_STOPPED)
            error_code = "cancelled"
            break
        except TimeoutError:
            emit_lifecycle(CallerTurnEventKind.PIPELINE_STOPPED)
            error_code = "session_timeout"
            break
        except Exception:
            if reconnect_count < max_reconnect_attempts:
                reconnect_count += 1
                epoch += 1
                emit_lifecycle(
                    CallerTurnEventKind.RECONNECT_STARTED,
                    event_epoch=epoch,
                )
                continue
            emit_lifecycle(CallerTurnEventKind.CONNECTION_CLOSED)
            error_code = "provider_closed"
            break

    for turn in emitted_turns:
        turn_statuses[turn.status.value] += 1
        close_reasons[turn.close_reason.value] += 1
    complete = error_code is None and bool(emitted_turns) and all(
        turn.status
        in {
            CallerTurnCompletionStatus.RETROSPECTIVE_COMPLETE,
            CallerTurnCompletionStatus.CANCELLED,
        }
        for turn in emitted_turns
    )
    return SessionAttemptResult(
        complete=complete,
        error_code=error_code,
        reconnect_count=reconnect_count,
        event_type_counts=dict(event_counts),
        decode_rejection_counts=dict(rejection_counts),
        turn_status_counts=dict(turn_statuses),
        turn_close_reason_counts=dict(close_reasons),
    )


def _validate_provider_setup(setup: dict[str, Any]) -> None:
    model = setup.get("model")
    if (
        not isinstance(model, str)
        or not MODEL_RESOURCE_PATTERN.fullmatch(model)
        or "latest" in model
    ):
        raise QualificationError("setup_document_invalid")
    generation = _require_object(setup, "generation_config")
    required_generation = {
        "response_modalities",
        "temperature",
        "speech_config",
        "thinking_config",
    }
    if not required_generation.issubset(generation):
        raise QualificationError("setup_document_invalid")
    if not isinstance(setup.get("input_audio_transcription"), dict) or not isinstance(
        setup.get("output_audio_transcription"), dict
    ):
        raise QualificationError("setup_document_invalid")
    realtime = _require_object(setup, "realtime_input_config")
    if set(realtime) != {
        "automatic_activity_detection",
        "activity_handling",
        "turn_coverage",
    }:
        raise QualificationError("setup_document_invalid")
    vad = _require_object(realtime, "automatic_activity_detection")
    if set(vad) != {
        "start_of_speech_sensitivity",
        "end_of_speech_sensitivity",
        "prefix_padding_ms",
        "silence_duration_ms",
    }:
        raise QualificationError("setup_document_invalid")
    if not isinstance(setup.get("system_instruction"), dict) or not isinstance(
        setup.get("tools"), list
    ):
        raise QualificationError("setup_document_invalid")


def _validate_websocket_policy(policy: dict[str, Any]) -> dict[str, Any]:
    bounds = {
        "max_message_bytes": (1, 16 * 1024 * 1024),
        "open_timeout_seconds": (1, 60),
        "setup_timeout_seconds": (1, 60),
        "ping_interval_seconds": (1, 120),
        "ping_timeout_seconds": (1, 60),
        "close_timeout_seconds": (1, 30),
    }
    if set(policy) != set(bounds):
        raise QualificationError("setup_document_invalid")
    for field, (minimum, maximum) in bounds.items():
        if not _bounded_number(policy.get(field), minimum, maximum):
            raise QualificationError("setup_document_invalid")
    return policy


def _validate_source_identity(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"source_sha", "file_sha256"}:
        raise QualificationError("setup_document_invalid")
    source_sha = value.get("source_sha")
    file_sha = value.get("file_sha256")
    if (
        not isinstance(source_sha, str)
        or not SOURCE_SHA_PATTERN.fullmatch(source_sha)
        or not isinstance(file_sha, str)
        or not SHA256_PATTERN.fullmatch(file_sha)
    ):
        raise QualificationError("setup_document_invalid")
    return {"source_sha": source_sha, "file_sha256": file_sha}


def _validate_config(config: QualificationConfig) -> None:
    if not isinstance(config.execute, bool):
        raise QualificationError("execution_mode_invalid")
    if (
        not isinstance(config.model_resource, str)
        or not MODEL_RESOURCE_PATTERN.fullmatch(config.model_resource)
        or "latest" in config.model_resource
    ):
        raise QualificationError("model_resource_invalid")
    if config.api_version != "v1beta" or config.endpoint not in OFFICIAL_ENDPOINTS:
        if config.endpoint not in OFFICIAL_ENDPOINTS:
            raise QualificationError("endpoint_not_official")
        raise QualificationError("api_version_invalid")
    project = config.project.lower() if isinstance(config.project, str) else ""
    if (
        project == PRODUCTION_PROJECT
        or "prod" in project
        or not any(label in project for label in ("qual", "test", "staging", "sandbox"))
    ):
        raise QualificationError("production_project_forbidden")
    if (
        not isinstance(config.credential_ref, str)
        or not re.fullmatch(r"QUALIFICATION_[A-Z0-9_]+", config.credential_ref)
        or "LIVE_CALL" in config.credential_ref
    ):
        raise QualificationError("credential_ref_not_dedicated")
    if not SOURCE_SHA_PATTERN.fullmatch(config.source_sha):
        raise QualificationError("source_sha_invalid")
    for digest, code in (
        (config.manifest_sha256, "manifest_digest_invalid"),
        (config.setup_file_sha256, "setup_digest_invalid"),
        (config.canonical_setup_sha256, "canonical_setup_digest_invalid"),
        (config.deviations_sha256, "deviations_digest_invalid"),
    ):
        _require_sha256(digest, code)
    if not _bounded_int(config.attempt_cap, 1, MAX_ATTEMPTS):
        raise QualificationError("attempt_cap_invalid")
    if not _bounded_int(
        config.wall_clock_cap_seconds, 1, MAX_WALL_CLOCK_SECONDS
    ):
        raise QualificationError("wall_clock_cap_invalid")
    if (
        not _bounded_int(
            config.session_timeout_seconds, 1, MAX_SESSION_TIMEOUT_SECONDS
        )
        or config.session_timeout_seconds > config.wall_clock_cap_seconds
    ):
        raise QualificationError("session_timeout_invalid")
    if not _bounded_number(config.max_cost_usd, 0.01, MAX_COST_USD):
        raise QualificationError("cost_cap_invalid")
    if not _bounded_number(
        config.max_cost_per_attempt_usd,
        0.01,
        MAX_COST_USD,
    ) or (
        float(config.max_cost_per_attempt_usd) * config.attempt_cap
        > float(config.max_cost_usd)
    ):
        raise QualificationError("cost_cap_invalid")


def _validate_setup_identity(
    canonical_setup: dict[str, Any], config: QualificationConfig
) -> None:
    if canonical_setup["model_resource"] != config.model_resource:
        raise QualificationError("model_resource_mismatch")
    if canonical_setup["api_version"] != config.api_version:
        raise QualificationError("api_version_mismatch")
    if canonical_setup["endpoint"] != config.endpoint:
        raise QualificationError("endpoint_mismatch")
    identities = (
        ("runner_identity", RUNNER_PATH, "runner_identity_mismatch"),
        ("evaluator_identity", EVALUATOR_PATH, "evaluator_identity_mismatch"),
        ("immutable_pipeline_identity", PIPELINE_PATH, "pipeline_identity_mismatch"),
    )
    for field, path, code in identities:
        identity = canonical_setup[field]
        if identity["source_sha"] != config.source_sha:
            raise QualificationError(code)
        if identity["file_sha256"] != _file_sha256(path, code):
            raise QualificationError(code)


def _load_and_validate_deviations(path: Path) -> list[dict[str, str]]:
    document = _load_bounded_json(path, "deviations_invalid")
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "review_status", "deviations"}
        or document.get("version") != 1
        or document.get("review_status") != "pending_gate_0b"
        or not isinstance(document.get("deviations"), list)
    ):
        raise QualificationError("deviations_invalid")
    validated = []
    for item in document["deviations"]:
        if not isinstance(item, dict) or set(item) != {
            "field",
            "immutable_value_sha256",
            "qualification_value_sha256",
            "reason_code",
        }:
            raise QualificationError("deviations_invalid")
        field = item["field"]
        if (
            not isinstance(field, str)
            or not re.fullmatch(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*", field)
            or not isinstance(item["reason_code"], str)
            or not SAFE_ID_PATTERN.fullmatch(item["reason_code"])
        ):
            raise QualificationError("deviations_invalid")
        _require_sha256(
            item["immutable_value_sha256"], "deviations_invalid"
        )
        _require_sha256(
            item["qualification_value_sha256"], "deviations_invalid"
        )
        validated.append(dict(item))
    return validated


def _validate_behavior_projection(value: object) -> None:
    if not isinstance(value, dict) or set(value) != BEHAVIOR_PROJECTION_FIELDS:
        raise QualificationError("setup_document_invalid")
    if value.get("api_version") != "v1beta" or value.get("endpoint") not in (
        OFFICIAL_ENDPOINTS
    ):
        raise QualificationError("setup_document_invalid")
    for field in (
        "system_instruction_sha256",
        "synthetic_prompt_fixture_sha256",
        "tool_declarations_sha256",
    ):
        _require_sha256(value.get(field), "setup_document_invalid")
    if not isinstance(value.get("model_resource"), str):
        raise QualificationError("setup_document_invalid")
    for field in (
        "generation_config",
        "input_audio_transcription",
        "output_audio_transcription",
        "realtime_input_config",
        "reconnect_policy",
        "turn_assembly_policy",
        "websocket_policy",
    ):
        if not isinstance(value.get(field), dict):
            raise QualificationError("setup_document_invalid")
    if not isinstance(value.get("tool_response_policy"), str):
        raise QualificationError("setup_document_invalid")


def _assert_payload_free_configuration(value: object) -> None:
    forbidden_fields = {
        "text",
        "prompt",
        "system_instruction",
        "parts",
        "function_declarations",
        "audio",
        "data",
        "transcript",
        "credential",
        "api_key",
        "token",
        "phone",
        "address",
        "caller",
        "contractor",
        "call_sid",
    }
    if isinstance(value, dict):
        for field, nested in value.items():
            if not isinstance(field, str) or field.lower() in forbidden_fields:
                raise QualificationError("setup_document_contains_payload")
            _assert_payload_free_configuration(nested)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _assert_payload_free_configuration(nested)
        return
    if isinstance(value, str) and (
        len(value) > 256 or any(ord(character) < 32 for character in value)
    ):
        raise QualificationError("setup_document_contains_payload")


def _validate_deviation_coverage(
    canonical_setup: dict[str, Any],
    deviations: list[dict[str, str]],
) -> None:
    qualification = {
        field: canonical_setup[field] for field in BEHAVIOR_PROJECTION_FIELDS
    }
    expected = {
        field: (immutable, candidate)
        for field, immutable, candidate in _diff_leaves(
            canonical_setup["immutable_pipeline_setup"],
            qualification,
        )
    }
    observed = {item["field"]: item for item in deviations}
    if len(observed) != len(deviations) or set(observed) != set(expected):
        raise QualificationError("setup_deviation_unexplained")
    for field, (immutable, candidate) in expected.items():
        item = observed[field]
        if item["immutable_value_sha256"] != canonical_json_sha256(immutable) or item[
            "qualification_value_sha256"
        ] != canonical_json_sha256(candidate):
            raise QualificationError("setup_deviation_unexplained")


def _diff_leaves(
    immutable: object,
    qualification: object,
    prefix: str = "",
) -> list[tuple[str, object, object]]:
    if (
        isinstance(immutable, dict)
        and isinstance(qualification, dict)
        and set(immutable) == set(qualification)
    ):
        differences = []
        for field in sorted(immutable):
            path = f"{prefix}.{field}" if prefix else field
            differences.extend(
                _diff_leaves(immutable[field], qualification[field], path)
            )
        return differences
    if immutable == qualification:
        return []
    return [(prefix, immutable, qualification)]


def _validate_speakers(speakers: list[object]) -> set[str]:
    speaker_ids: set[str] = set()
    for speaker in speakers:
        if not isinstance(speaker, dict) or set(speaker) != {
            "id",
            "adult_consent",
            "usage_rights",
            "consent_record_sha256",
        }:
            raise QualificationError("manifest_consent_invalid")
        speaker_id = speaker.get("id")
        if (
            not isinstance(speaker_id, str)
            or not SAFE_ID_PATTERN.fullmatch(speaker_id)
            or speaker_id in speaker_ids
            or speaker.get("adult_consent") is not True
            or speaker.get("usage_rights") != "qualification_only"
        ):
            raise QualificationError("manifest_consent_invalid")
        _require_sha256(
            speaker.get("consent_record_sha256"), "manifest_consent_invalid"
        )
        speaker_ids.add(speaker_id)
    return speaker_ids


def _validate_audio_case(
    case: object,
    *,
    fixture_root: Path,
    speaker_ids: set[str],
    case_ids: set[str],
) -> str:
    required = {
        "id",
        "audio_path",
        "audio_sha256",
        "script_sha256",
        "script_provenance",
        "speaker_id",
        "language",
        "codec",
        "condition",
        "split",
        "scenario",
    }
    if not isinstance(case, dict) or set(case) != required:
        raise QualificationError("manifest_case_invalid")
    case_id = case.get("id")
    if (
        not isinstance(case_id, str)
        or not SAFE_ID_PATTERN.fullmatch(case_id)
        or case_id in case_ids
        or case.get("speaker_id") not in speaker_ids
        or case.get("script_provenance") != "synthetic"
        or case.get("split") not in {"development", "holdout"}
    ):
        raise QualificationError("manifest_case_invalid")
    language = case.get("language")
    if not isinstance(language, str) or not LANGUAGE_PATTERN.fullmatch(language):
        raise QualificationError("manifest_case_invalid")
    for field in ("codec", "condition", "scenario"):
        value = case.get(field)
        if not isinstance(value, str) or not SAFE_ID_PATTERN.fullmatch(value):
            raise QualificationError("manifest_case_invalid")
    _require_sha256(case.get("audio_sha256"), "manifest_case_invalid")
    _require_sha256(case.get("script_sha256"), "manifest_case_invalid")
    audio_path = case.get("audio_path")
    if not isinstance(audio_path, str):
        raise QualificationError("manifest_case_invalid")
    resolved = (fixture_root / audio_path).resolve()
    if not resolved.is_relative_to(fixture_root) or not resolved.is_file():
        raise QualificationError("manifest_case_invalid")
    if resolved.stat().st_size > MAX_AUDIO_FILE_BYTES:
        raise QualificationError("manifest_case_invalid")
    if _file_sha256(resolved, "manifest_case_invalid") != case["audio_sha256"]:
        raise QualificationError("manifest_case_invalid")
    case_ids.add(case_id)
    return language


def _qualification_report(
    *,
    status: str,
    registration: dict[str, Any],
    failure_counts: Counter[str],
    attempts: int = 0,
    event_counts: Counter[str] | None = None,
    turn_statuses: Counter[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "scope": "synthetic_caller_turn_qualification",
        "preregistration": registration,
        "sample": {"attempts": attempts},
        "event_type_counts": dict(sorted((event_counts or Counter()).items())),
        "turn_status_counts": dict(sorted((turn_statuses or Counter()).items())),
        "failure_counts": dict(sorted(failure_counts.items())),
        **{field: False for field in EVIDENCE_FIELDS},
    }


def _aggregate_execution_report(
    registration: dict[str, Any],
    attempts: list[SessionAttemptResult],
    *,
    expected_attempts: int,
    wall_clock_reached: bool,
) -> dict[str, Any]:
    failures: Counter[str] = Counter()
    events: Counter[str] = Counter()
    turn_statuses: Counter[str] = Counter()
    for attempt in attempts:
        if attempt.error_code is not None:
            failures[attempt.error_code] += 1
        events.update(attempt.event_type_counts)
        turn_statuses.update(attempt.turn_status_counts)
    if wall_clock_reached:
        failures["wall_clock_cap_reached"] += 1
    if len(attempts) != expected_attempts:
        failures["attempts_incomplete"] += 1
    complete = (
        bool(attempts)
        and len(attempts) == expected_attempts
        and all(attempt.complete for attempt in attempts)
    )
    return _qualification_report(
        status="execution_complete_nonauthorizing" if complete else "partial",
        registration=registration,
        failure_counts=failures,
        attempts=len(attempts),
        event_counts=events,
        turn_statuses=turn_statuses,
    )


def _load_case_audio(manifest_path: Path, case: dict[str, Any]) -> bytes:
    root = manifest_path.resolve().parent
    path = (root / case["audio_path"]).resolve()
    if not path.is_relative_to(root):
        raise QualificationError("manifest_case_invalid")
    data = path.read_bytes()
    if len(data) > MAX_AUDIO_FILE_BYTES:
        raise QualificationError("manifest_case_invalid")
    return data


def _load_bounded_json(path: Path, code: str) -> Any:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_INPUT_FILE_BYTES:
            raise QualificationError(code)
        return json.loads(raw)
    except QualificationError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise QualificationError(code) from exc


def _file_sha256(path: Path, code: str) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise QualificationError(code) from exc


def _require_object(document: dict[str, Any], field: str) -> dict[str, Any]:
    value = document.get(field)
    if not isinstance(value, dict):
        raise QualificationError("setup_document_invalid")
    return value


def _require_sha256(value: object, code: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise QualificationError(code)


def _bounded_int(value: object, minimum: int, maximum: int) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and minimum <= value <= maximum
    )


def _bounded_number(value: object, minimum: float, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and minimum <= float(value) <= maximum
    )


def _real_connect(*args: Any, **kwargs: Any):
    import websockets

    return websockets.connect(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model-resource", required=True)
    parser.add_argument("--api-version", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--credential-ref", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--setup-file-sha256", required=True)
    parser.add_argument("--canonical-setup-sha256", required=True)
    parser.add_argument("--deviations", type=Path, required=True)
    parser.add_argument("--deviations-sha256", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--attempt-cap", type=int, required=True)
    parser.add_argument("--wall-clock-cap-seconds", type=int, required=True)
    parser.add_argument("--session-timeout-seconds", type=int, required=True)
    parser.add_argument("--max-cost-usd", type=float, required=True)
    parser.add_argument("--max-cost-per-attempt-usd", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = QualificationConfig(
        execute=args.execute,
        model_resource=args.model_resource,
        api_version=args.api_version,
        endpoint=args.endpoint,
        project=args.project,
        credential_ref=args.credential_ref,
        manifest_path=args.manifest,
        manifest_sha256=args.manifest_sha256,
        setup_path=args.setup,
        setup_file_sha256=args.setup_file_sha256,
        canonical_setup_sha256=args.canonical_setup_sha256,
        deviations_path=args.deviations,
        deviations_sha256=args.deviations_sha256,
        source_sha=args.source_sha,
        attempt_cap=args.attempt_cap,
        wall_clock_cap_seconds=args.wall_clock_cap_seconds,
        session_timeout_seconds=args.session_timeout_seconds,
        max_cost_usd=args.max_cost_usd,
        max_cost_per_attempt_usd=args.max_cost_per_attempt_usd,
    )
    try:
        report = asyncio.run(run_qualification(config))
    except QualificationError as exc:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "configuration_invalid",
            "failure_counts": {exc.code: 1},
            **{field: False for field in EVIDENCE_FIELDS},
        }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return (
        0
        if report["status"]
        in {"dry_run_ready", "execution_complete_nonauthorizing"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
