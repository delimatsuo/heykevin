#!/usr/bin/env python3
"""Connector-injected Gemini caller-turn qualification session executor."""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_CEILING
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from app.services.caller_turn_alignment import ActivityReference
from app.services.caller_turn_measurement import (
    MeasurementError,
    WireObservation,
    combined_usage_evidence_sha256,
    derive_audit_capsule_accounting,
    require_reducer_agreement,
    seal_audit_capsule,
    usage_evidence_sha256,
)
from app.services.caller_turn_qualification import (
    PricingSchedule,
    empty_evidence_flags,
)
from app.services.caller_turns import CallerTurnEvent, CallerTurnEventKind
from app.services.gemini_turn_events import (
    GeminiTurnEventAdapter,
    GeminiTurnEventDecodeStatus,
)
from app.services.voice_turn_replay import (
    DEVELOPER_PROVIDER,
    Gate0BReplayInput,
    build_gemini_audio_message,
)
from app.services.qualification_environment import execution_identity_sha256
from app.services.qualification_identity import (
    AttemptAuthorization,
    AttemptClaim,
    CampaignApproval,
    canonical_json_bytes,
    verify_attempt_authorization,
    verify_campaign_approval,
)
from app.services.qualification_ledger import (
    CustodyLedgerState,
    LedgerCustodyClient,
    LedgerCustodyIdentity,
    validate_custody_ledger_snapshot,
)


OFFICIAL_ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
EXACT_MODEL = "models/gemini-3.1-flash-live-preview"
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
VALID_SPLITS = frozenset({"development", "holdout"})
VALID_LANGUAGES = frozenset({"ar", "en", "es", "fr", "hi", "ht", "pt", "zh"})
VALID_POLICIES_MS = frozenset({100, 250, 500, 750})
MAX_OUTPUT_AUDIO_BYTES = 120 * 24_000 * 2
MAX_OUTPUT_AUDIO_PER_RUN_BYTES = 1_800 * 24_000 * 2
MAX_COST_PER_SESSION_MICROUSD = 250_000
MAX_WHOLE_RUN_SECONDS = 3_600
SHA256 = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA = re.compile(r"[0-9a-f]{40,64}")
REPO_ROOT = Path(__file__).resolve().parents[1]
PINNED_APPROVAL_ROOT_PATH = REPO_ROOT / "config/qualification/gate_0b_approval_root.ed25519.pub"
PREREGISTRATION_EXTERNAL_FIELDS = frozenset(
    {
        "project",
        "credential_reference",
        "approval_key_id",
        "approval_public_key_sha256",
        "custodian_key_id",
        "custodian_public_key_sha256",
        "record_root_key_id",
        "record_root_public_key_sha256",
        "ledger_instance_id",
        "ledger_custodian_key_id",
        "ledger_custodian_public_key_sha256",
        "source_sha",
        "environment_identity_sha256",
        "manifest_sha256",
        "corpus_sha256",
        "development_schedule_sha256",
        "setup_sha256",
        "pricing_sha256",
        "runner_sha256",
        "evaluator_sha256",
        "ledger_location_sha256",
        "audit_capsule_location_sha256",
        "holdout_capsule_location_sha256",
        "evidence_location_sha256",
        "consent_attestation_sha256",
        "retention_attestation_sha256",
        "zdr_or_residual_retention_acceptance_sha256",
    }
)


class RunnerError(ValueError):
    """Raised for local runner contract violations."""


class ProviderSessionClosed(Exception):
    """Transport-neutral close signal that intentionally discards provider text."""

    def __init__(self, _provider_reason: object = None) -> None:
        super().__init__("provider session closed")


class RequestReservationError(RunnerError):
    """Raised when the signed request reservation has been consumed."""


class _AbortConnection(Exception):
    pass


class SecretCredential:
    """Opaque credential whose string representations are always redacted."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if (
            not isinstance(value, str)
            or not 16 <= len(value) <= 8_192
            or any(ord(character) < 33 for character in value)
        ):
            raise RunnerError("credential is invalid")
        self._value = value

    def reveal_for_connector(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretCredential(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class ConnectionPolicy:
    proxy: None = None
    follow_redirects: bool = False
    debug: bool = False
    crash_dump: bool = False
    tls_key_log: bool = False

    def __post_init__(self) -> None:
        if self.proxy is not None:
            raise RunnerError("qualification connections cannot use a proxy")
        if any(
            value is not False
            for value in (
                self.follow_redirects,
                self.debug,
                self.crash_dump,
                self.tls_key_log,
            )
        ):
            raise RunnerError("qualification connection diagnostics must remain disabled")


@dataclass(frozen=True, slots=True)
class InjectedConnectionRequest:
    endpoint: str
    project: str
    credential: SecretCredential
    policy: ConnectionPolicy
    epoch: int


class InjectedSession(Protocol):
    async def send(self, message: Mapping[str, Any]) -> None: ...

    async def receive(self) -> object | None: ...

    async def close(self) -> None: ...


class InjectedConnector(Protocol):
    async def connect(self, request: InjectedConnectionRequest) -> InjectedSession: ...


@dataclass(frozen=True, slots=True)
class SessionExecutionConfig:
    endpoint: str
    model: str
    project: str
    max_message_bytes: int
    session_timeout_seconds: int
    response_gap_limit_ms: int

    def __post_init__(self) -> None:
        if self.endpoint != OFFICIAL_ENDPOINT:
            raise RunnerError("qualification endpoint is not the pinned official endpoint")
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme != "wss"
            or parsed.hostname != "generativelanguage.googleapis.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RunnerError("qualification endpoint TLS scope is invalid")
        if self.model != EXACT_MODEL:
            raise RunnerError("qualification model is not the pinned 3.1 model")
        if not isinstance(self.project, str) or not PROJECT_ID.fullmatch(self.project):
            raise RunnerError("qualification project is invalid")
        if "prod" in self.project or self.project == "kevin-491315":
            raise RunnerError("production project is forbidden")
        if (
            isinstance(self.max_message_bytes, bool)
            or not isinstance(self.max_message_bytes, int)
            or not 1_024 <= self.max_message_bytes <= 1024 * 1024
        ):
            raise RunnerError("message size bound is invalid")
        if (
            isinstance(self.session_timeout_seconds, bool)
            or not isinstance(self.session_timeout_seconds, int)
            or not 1 <= self.session_timeout_seconds <= 120
        ):
            raise RunnerError("session timeout is invalid")
        if self.response_gap_limit_ms != 500:
            raise RunnerError("response gap limit must remain 500 ms")


@dataclass(frozen=True, slots=True)
class AuthorizedAttemptConfig:
    """Values inherited from one digest-bound external preregistration."""

    preregistration_sha256: str
    source_sha: str
    approval_key_id: str
    credential_reference: str
    policy_ms: int
    whole_run_timeout_seconds: int

    def __post_init__(self) -> None:
        if not isinstance(self.preregistration_sha256, str) or not SHA256.fullmatch(
            self.preregistration_sha256
        ):
            raise RunnerError("preregistration digest is invalid")
        if not isinstance(self.source_sha, str) or not SOURCE_SHA.fullmatch(self.source_sha):
            raise RunnerError("approved source SHA is invalid")
        _safe_id(self.approval_key_id, label="approval key ID")
        _safe_id(self.credential_reference, label="credential reference")
        if self.policy_ms not in VALID_POLICIES_MS:
            raise RunnerError("qualification policy is invalid")
        if (
            isinstance(self.whole_run_timeout_seconds, bool)
            or not isinstance(self.whole_run_timeout_seconds, int)
            or not 1 <= self.whole_run_timeout_seconds <= MAX_WHOLE_RUN_SECONDS
        ):
            raise RunnerError("whole-run timeout is invalid")


@dataclass(frozen=True, slots=True)
class SessionActivityPlan:
    activity_ordinal: int
    split: str
    language: str
    condition: str
    scenario_tags: tuple[str, ...]
    reference: ActivityReference
    expected_lifecycle_status: str
    expected_epoch: int
    start_at_ms: int
    end_at_ms: int

    def __post_init__(self) -> None:
        _bounded_int(self.activity_ordinal, label="activity ordinal", maximum=255)
        if self.split not in VALID_SPLITS:
            raise RunnerError("activity split is invalid")
        if self.language not in VALID_LANGUAGES:
            raise RunnerError("activity language is invalid")
        _safe_id(self.condition, label="condition")
        if (
            not isinstance(self.scenario_tags, tuple)
            or not self.scenario_tags
            or len(self.scenario_tags) > 16
            or any(
                not isinstance(value, str) or not SAFE_ID.fullmatch(value)
                for value in self.scenario_tags
            )
        ):
            raise RunnerError("activity scenario tags are invalid")
        if not isinstance(self.reference, ActivityReference):
            raise RunnerError("activity reference is invalid")
        if (
            self.reference.activity_ordinal != self.activity_ordinal
            or self.reference.language != self.language
        ):
            raise RunnerError("activity reference identity mismatch")
        if self.expected_lifecycle_status not in {
            "retrospective_complete",
            "partial",
            "cancelled",
            "dropped",
        }:
            raise RunnerError("activity lifecycle expectation is invalid")
        _bounded_int(self.expected_epoch, label="expected epoch", maximum=1_000)
        _bounded_int(self.start_at_ms, label="activity start", maximum=120_000)
        _bounded_int(self.end_at_ms, label="activity end", maximum=120_000)
        if self.end_at_ms <= self.start_at_ms:
            raise RunnerError("activity timing is invalid")


@dataclass(frozen=True, slots=True)
class SessionPlan:
    session_ordinal: int
    split: str
    activities: tuple[SessionActivityPlan, ...]
    replay_inputs: tuple[Gate0BReplayInput, ...]

    def __post_init__(self) -> None:
        _bounded_int(self.session_ordinal, label="session ordinal", maximum=63)
        if self.split not in VALID_SPLITS:
            raise RunnerError("session split is invalid")
        if (
            not isinstance(self.activities, tuple)
            or not 1 <= len(self.activities) <= 10
            or any(not isinstance(value, SessionActivityPlan) for value in self.activities)
        ):
            raise RunnerError("session activities are invalid")
        if any(value.split != self.split for value in self.activities):
            raise RunnerError("session cannot cross splits")
        if len({value.activity_ordinal for value in self.activities}) != len(self.activities):
            raise RunnerError("session activity ordinals must be unique")
        if (
            not isinstance(self.replay_inputs, tuple)
            or not self.replay_inputs
            or any(not isinstance(value, Gate0BReplayInput) for value in self.replay_inputs)
        ):
            raise RunnerError("session replay inputs are invalid")
        if [value.at_ms for value in self.replay_inputs] != sorted(
            value.at_ms for value in self.replay_inputs
        ):
            raise RunnerError("session replay schedule must be monotonic")
        activity_ordinals = {value.activity_ordinal for value in self.activities}
        if any(
            value.activity_ordinal is not None and value.activity_ordinal not in activity_ordinals
            for value in self.replay_inputs
        ):
            raise RunnerError("session replay input references an unknown activity")
        _validate_restart_schedule(self.activities, self.replay_inputs)


@dataclass(frozen=True, slots=True)
class NoSpeechWindowPlan:
    window_ordinal: int
    split: str
    condition: str
    replay_inputs: tuple[Gate0BReplayInput, ...]

    def __post_init__(self) -> None:
        _bounded_int(self.window_ordinal, label="window ordinal", maximum=63)
        if self.split not in VALID_SPLITS:
            raise RunnerError("no-speech split is invalid")
        _safe_id(self.condition, label="no-speech condition")
        if (
            not isinstance(self.replay_inputs, tuple)
            or not self.replay_inputs
            or any(not isinstance(value, Gate0BReplayInput) for value in self.replay_inputs)
        ):
            raise RunnerError("no-speech replay inputs are invalid")
        if [value.at_ms for value in self.replay_inputs] != sorted(
            value.at_ms for value in self.replay_inputs
        ):
            raise RunnerError("no-speech replay schedule must be monotonic")
        if any(
            value.kind != "audio"
            or value.epoch != 1
            or value.activity_ordinal is not None
            or not isinstance(value.audio, bytes)
            or not value.audio
            or value.duration_ms <= 0
            for value in self.replay_inputs
        ):
            raise RunnerError("no-speech replay may contain only epoch-one PCM audio")
        if (
            sum(value.duration_ms for value in self.replay_inputs) > 120_000
            or self.replay_inputs[-1].at_ms + self.replay_inputs[-1].duration_ms > 120_000
        ):
            raise RunnerError("no-speech replay exceeds the session duration cap")


@dataclass(frozen=True, slots=True)
class UsageCounts:
    input_audio_tokens: int = 0
    output_audio_tokens: int = 0
    input_text_tokens: int = 0
    output_text_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "input_audio_tokens": self.input_audio_tokens,
            "output_audio_tokens": self.output_audio_tokens,
            "input_text_tokens": self.input_text_tokens,
            "output_text_tokens": self.output_text_tokens,
        }


@dataclass(frozen=True, slots=True)
class ReductionResult:
    status: str
    events: tuple[CallerTurnEvent, ...]
    rejection_code: str | None


@dataclass(frozen=True, slots=True)
class SessionExecutionResult:
    complete: bool
    error_code: str | None
    audit_events: tuple[CallerTurnEvent, ...]
    wire_observations: dict[int, WireObservation]
    usage: UsageCounts
    output_audio_bytes: int
    provider_request_count: int
    epoch_count: int

    def redacted_report_dict(self) -> dict[str, Any]:
        event_counts = Counter(value.kind.value for value in self.audit_events)
        return {
            "complete": self.complete,
            "error_code": self.error_code,
            "event_count": len(self.audit_events),
            "event_type_counts": dict(sorted(event_counts.items())),
            "wire_activity_count": len(self.wire_observations),
            "usage": self.usage.to_dict(),
            "output_audio_bytes": self.output_audio_bytes,
            "provider_request_count": self.provider_request_count,
            "epoch_count": self.epoch_count,
        }


@dataclass(frozen=True, slots=True)
class WireFact:
    kind: str
    at_ms: int
    response_ordinal: int | None = None
    activity_ordinal: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in {
            "audio_received",
            "false_activity",
        }:
            raise RunnerError("wire fact kind is invalid")
        _bounded_int(self.at_ms, label="wire fact time", maximum=7_200_000)
        if self.response_ordinal is not None:
            _bounded_int(
                self.response_ordinal,
                label="wire response ordinal",
                maximum=10_000,
            )
        if self.activity_ordinal is not None:
            _bounded_int(
                self.activity_ordinal,
                label="wire activity ordinal",
                maximum=255,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at_ms": self.at_ms,
            "response_ordinal": self.response_ordinal,
            "activity_ordinal": self.activity_ordinal,
        }


@dataclass(frozen=True, slots=True)
class NoSpeechExecutionResult:
    complete: bool
    error_code: str | None
    false_activity_count: int
    model_audio_chunk_count: int
    abnormal_close_count: int
    audio_after_teardown_count: int
    output_audio_bytes: int
    usage: UsageCounts
    provider_request_count: int
    wire_facts: tuple[WireFact, ...]

    def redacted_report_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "error_code": self.error_code,
            "false_activity_count": self.false_activity_count,
            "model_audio_chunk_count": self.model_audio_chunk_count,
            "abnormal_close_count": self.abnormal_close_count,
            "audio_after_teardown_count": self.audio_after_teardown_count,
            "output_audio_bytes": self.output_audio_bytes,
            "usage": self.usage.to_dict(),
            "provider_request_count": self.provider_request_count,
        }


@dataclass(frozen=True, slots=True)
class AttemptExecutionResult:
    complete: bool
    error_code: str | None
    provider_request_count: int
    cost_microusd: int
    capsule_handed_off: bool

    def redacted_report_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "error_code": self.error_code,
            "provider_request_count": self.provider_request_count,
            "cost_microusd": self.cost_microusd,
            "capsule_handed_off": self.capsule_handed_off,
            "future_execution_authorized": False,
            "runtime_wiring_authorized": False,
            "deployment_authorized": False,
            "production_authorized": False,
            "release_authorized": False,
        }


@dataclass(slots=True)
class _MutableWire:
    first_audio_ms: int | None = None
    interruption_tail_ms: int | None = None
    premature_current_audio_count: int = 0
    audio_after_terminal_count: int = 0
    response_gap_violation_count: int = 0
    abnormal_close_count: int = 0
    runaway_output_count: int = 0
    response_timeout_count: int = 0
    malformed_count: int = 0
    teardown_violation_count: int = 0

    def freeze(self) -> WireObservation:
        return WireObservation(
            timing_covered=self.first_audio_ms is not None,
            first_audio_ms=self.first_audio_ms,
            interruption_tail_ms=self.interruption_tail_ms,
            premature_current_audio_count=self.premature_current_audio_count,
            audio_after_terminal_count=self.audio_after_terminal_count,
            response_gap_violation_count=self.response_gap_violation_count,
            abnormal_close_count=self.abnormal_close_count,
            runaway_output_count=self.runaway_output_count,
            response_timeout_count=self.response_timeout_count,
            malformed_count=self.malformed_count,
            teardown_violation_count=self.teardown_violation_count,
        )


@dataclass(slots=True)
class _SessionProgress:
    wires: dict[int, _MutableWire]
    provider_request_count: int = 0
    epoch_count: int = 0


def build_gate0b_setup_message(
    config: SessionExecutionConfig,
    *,
    include_tool: bool = False,
) -> dict[str, Any]:
    if not isinstance(config, SessionExecutionConfig):
        raise TypeError("config must be a SessionExecutionConfig")
    if not isinstance(include_tool, bool):
        raise TypeError("include_tool must be a boolean")
    message = {
        "setup": {
            "model": config.model,
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "temperature": 0.4,
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": "Puck"},
                    }
                },
                "thinkingConfig": {"thinkingLevel": "minimal"},
            },
            "systemInstruction": {
                "parts": [
                    {
                        "text": (
                            "This is a purpose-recorded synthetic receptionist qualification. "
                            "Reply briefly and use only the declared synthetic tool."
                        )
                    }
                ]
            },
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "realtimeInputConfig": {
                "automaticActivityDetection": {
                    "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
                    "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
                    "prefixPaddingMs": 100,
                    "silenceDurationMs": 500,
                },
                "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
                "turnCoverage": "TURN_INCLUDES_ONLY_ACTIVITY",
            },
        }
    }
    if include_tool:
        message["setup"]["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": "synthetic_lookup",
                        "description": "Return one fixed synthetic qualification result.",
                        "parameters": {"type": "OBJECT", "properties": {}},
                    }
                ]
            }
        ]
    return message


def build_gate0b_setup_identity(config: SessionExecutionConfig) -> dict[str, Any]:
    """Canonical setup variants approved for standard and synthetic-tool scenarios."""
    return {
        "standard": build_gate0b_setup_message(config),
        "synthetic_tool": build_gate0b_setup_message(config, include_tool=True),
    }


async def execute_injected_session(
    plan: SessionPlan,
    *,
    config: SessionExecutionConfig,
    connector: InjectedConnector,
    credential: SecretCredential,
    receipt_clock_ms: Callable[[], int],
    sleep_ms: Callable[[int], Awaitable[None]],
    secondary_reducer: Callable[..., ReductionResult] | None = None,
    request_reserver: Callable[[], None] | None = None,
) -> SessionExecutionResult:
    """Execute one bounded fake-or-approved injected session with no default transport."""
    if not isinstance(plan, SessionPlan):
        raise TypeError("plan must be a SessionPlan")
    if not isinstance(config, SessionExecutionConfig):
        raise TypeError("config must be a SessionExecutionConfig")
    if not isinstance(credential, SecretCredential):
        raise TypeError("credential must be a SecretCredential")
    reducer = secondary_reducer or independent_reduce_message
    progress = _SessionProgress(
        wires={value.activity_ordinal: _MutableWire() for value in plan.activities}
    )
    try:
        return await asyncio.wait_for(
            _execute_session_flow(
                plan,
                config=config,
                connector=connector,
                credential=credential,
                receipt_clock_ms=receipt_clock_ms,
                sleep_ms=sleep_ms,
                secondary_reducer=reducer,
                request_reserver=request_reserver,
                progress=progress,
            ),
            timeout=config.session_timeout_seconds,
        )
    except TimeoutError:
        return _failed_result(
            plan,
            "session_timeout",
            wires=progress.wires,
            provider_request_count=progress.provider_request_count,
            epoch_count=progress.epoch_count,
        )
    except RequestReservationError:
        return _failed_result(plan, "provider_request_reservation_exhausted")
    except Exception:  # A connector failure is reported only as a bounded enum.
        return _failed_result(plan, "connector_failure")


async def _execute_session_flow(
    plan: SessionPlan,
    *,
    config: SessionExecutionConfig,
    connector: InjectedConnector,
    credential: SecretCredential,
    receipt_clock_ms: Callable[[], int],
    sleep_ms: Callable[[int], Awaitable[None]],
    secondary_reducer: Callable[..., ReductionResult],
    request_reserver: Callable[[], None] | None,
    progress: _SessionProgress,
) -> SessionExecutionResult:
    audit_events: list[CallerTurnEvent] = []
    wires = progress.wires
    usage = UsageCounts()
    output_audio_bytes = 0
    error_code: str | None = None
    active_response_activity: int | None = None
    terminal_activities: set[int] = set()
    last_response_audio_ms: int | None = None
    sequence = 0
    adapter = GeminiTurnEventAdapter()
    provider_request_count = 0
    epoch_count = 0

    for epoch, base_at_ms, replay_inputs in _connection_segments(plan):
        if active_response_activity is not None:
            error_code = "response_crossed_restart"
            break
        if request_reserver is not None:
            request_reserver()
        provider_request_count += 1
        epoch_count += 1
        progress.provider_request_count = provider_request_count
        progress.epoch_count = epoch_count
        request = InjectedConnectionRequest(
            endpoint=config.endpoint,
            project=config.project,
            credential=credential,
            policy=ConnectionPolicy(),
            epoch=epoch,
        )
        try:
            session = await connector.connect(request)
        except RequestReservationError:
            raise
        except Exception:
            error_code = "connector_failure"
            break

        connection_usage = UsageCounts()
        usage_frame_count = 0
        last_receipt_ms = base_at_ms
        sender_done = asyncio.Event()
        observed_interactions: set[str] = set()
        interaction_events = {
            kind: asyncio.Event()
            for kind in (
                "expect_synchronous_tool",
                "expect_tool_cancellation",
                "expect_interruption",
            )
        }
        sender_error: str | None = None
        sender_task: asyncio.Task[None] | None = None
        try:
            try:
                await session.send(
                    build_gate0b_setup_message(config, include_tool=_plan_uses_tools(plan))
                )
                setup_response = await session.receive()
                receipt_clock_ms()
            except ProviderSessionClosed:
                error_code = "provider_closed"
                raise _AbortConnection from None
            except Exception:
                error_code = "connector_failure"
                raise _AbortConnection from None
            if setup_response != {"setupComplete": {}}:
                error_code = "setup_rejected"
                raise _AbortConnection

            async def send_replay_inputs() -> None:
                nonlocal sender_error
                previous_at_ms = base_at_ms
                try:
                    for replay_input in replay_inputs:
                        delay = replay_input.at_ms - previous_at_ms
                        if delay > 0:
                            await sleep_ms(delay)
                        previous_at_ms = replay_input.at_ms
                        if replay_input.kind in interaction_events:
                            await interaction_events[replay_input.kind].wait()
                            continue
                        outbound = _outbound_message(replay_input)
                        if outbound is not None:
                            await session.send(outbound)
                except TimeoutError:
                    raise
                except Exception:
                    sender_error = "connector_failure"
                finally:
                    sender_done.set()

            sender_task = asyncio.create_task(send_replay_inputs())

            while True:
                try:
                    message = await session.receive()
                except ProviderSessionClosed:
                    error_code = "provider_closed"
                    _increment_wire_counter(
                        wires,
                        plan.activities,
                        "abnormal_close_count",
                        epoch=epoch,
                        at_ms=last_receipt_ms,
                        preferred_ordinal=active_response_activity,
                    )
                    break
                except Exception:
                    error_code = "connector_failure"
                    break
                if message is None:
                    if not sender_done.is_set():
                        pending_interactions = {
                            replay_input.kind
                            for replay_input in replay_inputs
                            if replay_input.kind in interaction_events
                            and replay_input.kind not in observed_interactions
                        }
                        if pending_interactions:
                            error_code = "expected_interaction_missing"
                            break
                        await sender_done.wait()
                    break
                at_ms = receipt_clock_ms()
                last_receipt_ms = at_ms
                if not isinstance(message, Mapping):
                    error_code = "malformed_message"
                    _increment_wire_counter(
                        wires,
                        plan.activities,
                        "malformed_count",
                        epoch=epoch,
                        at_ms=at_ms,
                        preferred_ordinal=active_response_activity,
                    )
                    break
                try:
                    encoded = json.dumps(
                        message,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                except (TypeError, ValueError, UnicodeEncodeError):
                    error_code = "malformed_message"
                    _increment_wire_counter(
                        wires,
                        plan.activities,
                        "malformed_count",
                        epoch=epoch,
                        at_ms=at_ms,
                        preferred_ordinal=active_response_activity,
                    )
                    break
                if len(encoded) > config.max_message_bytes:
                    error_code = "message_too_large"
                    _increment_wire_counter(
                        wires,
                        plan.activities,
                        "malformed_count",
                        epoch=epoch,
                        at_ms=at_ms,
                        preferred_ordinal=active_response_activity,
                    )
                    break
                if "goAway" in message:
                    error_code = "provider_goaway"
                    _increment_wire_counter(
                        wires,
                        plan.activities,
                        "abnormal_close_count",
                        epoch=epoch,
                        at_ms=at_ms,
                        preferred_ordinal=active_response_activity,
                    )
                    break

                if "usageMetadata" in message:
                    try:
                        parsed_usage = _parse_usage_metadata(message["usageMetadata"])
                    except RunnerError:
                        error_code = "usage_metadata_inconsistent"
                        break
                    if usage_frame_count and not _usage_snapshot_is_monotonic(
                        connection_usage,
                        parsed_usage,
                    ):
                        error_code = "usage_metadata_inconsistent"
                        break
                    connection_usage = parsed_usage
                    usage_frame_count += 1
                    if set(message) == {"usageMetadata"}:
                        if (
                            sender_done.is_set()
                            and active_response_activity is None
                            and len(terminal_activities) == len(plan.activities)
                        ):
                            break
                        continue

                primary_batch = adapter.adapt_message(
                    message,
                    at_ms=at_ms,
                    first_sequence=sequence,
                    epoch=epoch,
                )
                secondary = secondary_reducer(
                    message,
                    at_ms=at_ms,
                    first_sequence=sequence,
                    epoch=epoch,
                )
                primary = ReductionResult(
                    status=primary_batch.status.value,
                    events=primary_batch.events,
                    rejection_code=(
                        primary_batch.rejection_code.value
                        if primary_batch.rejection_code is not None
                        else None
                    ),
                )
                if (
                    primary.status != secondary.status
                    or primary.rejection_code != secondary.rejection_code
                ):
                    error_code = "reducer_disagreement"
                    break
                try:
                    require_reducer_agreement(primary.events, secondary.events)
                except MeasurementError:
                    error_code = "reducer_disagreement"
                    break
                if primary.status == GeminiTurnEventDecodeStatus.REJECTED.value:
                    error_code = primary.rejection_code or "malformed_message"
                    _increment_wire_counter(
                        wires,
                        plan.activities,
                        "malformed_count",
                        epoch=epoch,
                        at_ms=at_ms,
                        preferred_ordinal=active_response_activity,
                    )
                    break
                sequence += len(primary.events)
                audit_events.extend(primary.events)

                try:
                    tool_response = _synthetic_tool_response(message)
                except RunnerError as exc:
                    error_code = str(exc)
                    _increment_wire_counter(
                        wires,
                        plan.activities,
                        "malformed_count",
                        epoch=epoch,
                        at_ms=at_ms,
                        preferred_ordinal=active_response_activity,
                    )
                    break
                if tool_response is not None:
                    await session.send(tool_response)

                newly_observed = _observed_interaction_kinds(primary.events)
                observed_interactions.update(newly_observed)
                for kind in newly_observed:
                    interaction_events[kind].set()

                try:
                    audio_bytes = _extract_output_audio(message)
                except RunnerError as exc:
                    error_code = str(exc)
                    _increment_wire_counter(
                        wires,
                        plan.activities,
                        (
                            "runaway_output_count"
                            if error_code == "oversized_output_audio"
                            else "malformed_count"
                        ),
                        epoch=epoch,
                        at_ms=at_ms,
                        preferred_ordinal=active_response_activity,
                    )
                    break
                if audio_bytes:
                    output_audio_bytes += audio_bytes
                    if output_audio_bytes > MAX_OUTPUT_AUDIO_BYTES:
                        error_code = "runaway_output"
                        _increment_wire_counter(
                            wires,
                            plan.activities,
                            "runaway_output_count",
                            epoch=epoch,
                            at_ms=at_ms,
                            preferred_ordinal=active_response_activity,
                        )
                        break
                    if active_response_activity is None:
                        activity = _latest_activity(plan.activities, at_ms, epoch=epoch)
                        if activity is None:
                            error_code = "unattributed_response"
                            break
                        if activity.activity_ordinal in terminal_activities:
                            wires[activity.activity_ordinal].audio_after_terminal_count += 1
                            error_code = "audio_after_terminal"
                            break
                        active_response_activity = activity.activity_ordinal
                        if at_ms < activity.end_at_ms:
                            wires[activity.activity_ordinal].premature_current_audio_count += 1
                            error_code = "premature_current_response"
                        elif wires[activity.activity_ordinal].first_audio_ms is None:
                            wires[activity.activity_ordinal].first_audio_ms = (
                                at_ms - activity.end_at_ms
                            )
                    else:
                        newer = _latest_activity(plan.activities, at_ms, epoch=epoch)
                        if (
                            newer is not None
                            and newer.activity_ordinal != active_response_activity
                            and newer.start_at_ms <= at_ms
                        ):
                            wires[newer.activity_ordinal].interruption_tail_ms = (
                                at_ms - newer.start_at_ms
                            )
                    if last_response_audio_ms is not None and (
                        at_ms - last_response_audio_ms > config.response_gap_limit_ms
                    ):
                        wires[active_response_activity].response_gap_violation_count += 1
                        error_code = "response_gap_exceeded"
                    last_response_audio_ms = at_ms

                if _has_response_terminal(message) and active_response_activity is not None:
                    terminal_activities.add(active_response_activity)
                    active_response_activity = None
                    last_response_audio_ms = None
                if error_code is not None:
                    break
                if (
                    usage_frame_count
                    and sender_done.is_set()
                    and active_response_activity is None
                    and len(terminal_activities) == len(plan.activities)
                ):
                    break

            if sender_task is not None:
                if error_code is not None and not sender_task.done():
                    sender_task.cancel()
                try:
                    await sender_task
                except asyncio.CancelledError:
                    pass
            if error_code is None and sender_error is not None:
                error_code = sender_error
            if error_code is None and usage_frame_count == 0:
                error_code = "usage_metadata_missing"
            if error_code is None and active_response_activity is not None:
                wires[active_response_activity].response_timeout_count += 1
                error_code = "response_terminal_missing"
            if usage_frame_count:
                usage = _add_usage(usage, connection_usage)
        except _AbortConnection:
            pass
        except ProviderSessionClosed:
            error_code = "provider_closed"
            _increment_wire_counter(
                wires,
                plan.activities,
                "abnormal_close_count",
                epoch=epoch,
                at_ms=last_receipt_ms,
                preferred_ordinal=active_response_activity,
            )
        except TimeoutError:
            raise
        except Exception:
            error_code = "connector_failure"
        finally:
            try:
                await session.close()
            except Exception:
                if error_code is None:
                    error_code = "teardown_failure"
                    _increment_wire_counter(
                        wires,
                        plan.activities,
                        "teardown_violation_count",
                        epoch=epoch,
                        at_ms=last_receipt_ms,
                        preferred_ordinal=active_response_activity,
                    )
        if error_code is not None:
            break

    frozen_wires = {ordinal: value.freeze() for ordinal, value in wires.items()}
    if error_code is not None:
        return SessionExecutionResult(
            complete=False,
            error_code=error_code,
            audit_events=(),
            wire_observations=frozen_wires,
            usage=usage,
            output_audio_bytes=output_audio_bytes,
            provider_request_count=provider_request_count,
            epoch_count=epoch_count,
        )
    return SessionExecutionResult(
        complete=True,
        error_code=None,
        audit_events=tuple(audit_events),
        wire_observations=frozen_wires,
        usage=usage,
        output_audio_bytes=output_audio_bytes,
        provider_request_count=provider_request_count,
        epoch_count=epoch_count,
    )


async def execute_injected_no_speech_window(
    plan: NoSpeechWindowPlan,
    *,
    config: SessionExecutionConfig,
    connector: InjectedConnector,
    credential: SecretCredential,
    receipt_clock_ms: Callable[[], int],
    sleep_ms: Callable[[int], Awaitable[None]],
    secondary_reducer: Callable[..., ReductionResult] | None = None,
    request_reserver: Callable[[], None] | None = None,
) -> NoSpeechExecutionResult:
    """Execute one separately scheduled noise window and retain only wire facts."""
    if not isinstance(plan, NoSpeechWindowPlan):
        raise TypeError("plan must be a NoSpeechWindowPlan")
    if not isinstance(config, SessionExecutionConfig):
        raise TypeError("config must be a SessionExecutionConfig")
    if not isinstance(credential, SecretCredential):
        raise TypeError("credential must be a SecretCredential")
    try:
        return await asyncio.wait_for(
            _execute_no_speech_flow(
                plan,
                config=config,
                connector=connector,
                credential=credential,
                receipt_clock_ms=receipt_clock_ms,
                sleep_ms=sleep_ms,
                secondary_reducer=secondary_reducer or independent_reduce_message,
                request_reserver=request_reserver,
            ),
            timeout=config.session_timeout_seconds,
        )
    except TimeoutError:
        return _failed_no_speech("session_timeout")
    except RequestReservationError:
        return _failed_no_speech("provider_request_reservation_exhausted")
    except Exception:
        return _failed_no_speech("connector_failure")


async def _execute_no_speech_flow(
    plan: NoSpeechWindowPlan,
    *,
    config: SessionExecutionConfig,
    connector: InjectedConnector,
    credential: SecretCredential,
    receipt_clock_ms: Callable[[], int],
    sleep_ms: Callable[[int], Awaitable[None]],
    secondary_reducer: Callable[..., ReductionResult],
    request_reserver: Callable[[], None] | None,
) -> NoSpeechExecutionResult:
    if request_reserver is not None:
        request_reserver()
    request = InjectedConnectionRequest(
        endpoint=config.endpoint,
        project=config.project,
        credential=credential,
        policy=ConnectionPolicy(),
        epoch=1,
    )
    try:
        session = await connector.connect(request)
    except RequestReservationError:
        raise
    except Exception:
        return _failed_no_speech("connector_failure", provider_request_count=1)

    usage = UsageCounts()
    usage_frame_count = 0
    error_code: str | None = None
    false_activity_count = 0
    model_audio_chunk_count = 0
    abnormal_close_count = 0
    audio_after_teardown_count = 0
    output_audio_bytes = 0
    wire_facts: list[WireFact] = []
    response_open = False
    response_terminal = False
    sequence = 0
    adapter = GeminiTurnEventAdapter()
    sender_done = asyncio.Event()
    sender_error: str | None = None
    sender_task: asyncio.Task[None] | None = None
    try:
        await session.send(build_gate0b_setup_message(config))
        setup = await session.receive()
        receipt_clock_ms()
        if setup != {"setupComplete": {}}:
            error_code = "setup_rejected"
        else:

            async def send_replay_inputs() -> None:
                nonlocal sender_error
                previous_at_ms = 0
                try:
                    for replay_input in plan.replay_inputs:
                        delay = replay_input.at_ms - previous_at_ms
                        if delay > 0:
                            await sleep_ms(delay)
                        previous_at_ms = replay_input.at_ms
                        await session.send(
                            build_gemini_audio_message(
                                replay_input.audio,
                                provider=DEVELOPER_PROVIDER,
                            )
                        )
                except TimeoutError:
                    raise
                except Exception:
                    sender_error = "connector_failure"
                finally:
                    sender_done.set()

            sender_task = asyncio.create_task(send_replay_inputs())

            while error_code is None:
                try:
                    message = await session.receive()
                except ProviderSessionClosed:
                    abnormal_close_count += 1
                    error_code = "provider_closed"
                    break
                if message is None:
                    if not sender_done.is_set():
                        await sender_done.wait()
                    break
                at_ms = receipt_clock_ms()
                if not isinstance(message, Mapping):
                    error_code = "malformed_message"
                    break
                try:
                    encoded = json.dumps(
                        message,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                except (TypeError, ValueError, UnicodeEncodeError):
                    error_code = "malformed_message"
                    break
                if len(encoded) > config.max_message_bytes:
                    error_code = "message_too_large"
                    break
                if "goAway" in message:
                    abnormal_close_count += 1
                    error_code = "provider_goaway"
                    break

                if "usageMetadata" in message:
                    try:
                        parsed_usage = _parse_usage_metadata(message["usageMetadata"])
                    except RunnerError:
                        error_code = "usage_metadata_inconsistent"
                        break
                    if usage_frame_count and not _usage_snapshot_is_monotonic(
                        usage,
                        parsed_usage,
                    ):
                        error_code = "usage_metadata_inconsistent"
                        break
                    usage = parsed_usage
                    usage_frame_count += 1
                    if set(message) == {"usageMetadata"}:
                        if sender_done.is_set() and not response_open:
                            break
                        continue

                primary_batch = adapter.adapt_message(
                    message,
                    at_ms=at_ms,
                    first_sequence=sequence,
                    epoch=1,
                )
                secondary = secondary_reducer(
                    message,
                    at_ms=at_ms,
                    first_sequence=sequence,
                    epoch=1,
                )
                primary = ReductionResult(
                    status=primary_batch.status.value,
                    events=primary_batch.events,
                    rejection_code=(
                        primary_batch.rejection_code.value
                        if primary_batch.rejection_code is not None
                        else None
                    ),
                )
                if (
                    primary.status != secondary.status
                    or primary.rejection_code != secondary.rejection_code
                ):
                    error_code = "reducer_disagreement"
                    break
                try:
                    require_reducer_agreement(primary.events, secondary.events)
                except MeasurementError:
                    error_code = "reducer_disagreement"
                    break
                if primary.status == GeminiTurnEventDecodeStatus.REJECTED.value:
                    error_code = primary.rejection_code or "malformed_message"
                    break
                sequence += len(primary.events)

                activated = any(
                    event.kind
                    in {
                        CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                        CallerTurnEventKind.MODEL_OUTPUT_STARTED,
                        CallerTurnEventKind.TOOL_CALL_STARTED,
                    }
                    for event in primary.events
                )
                if activated:
                    false_activity_count += 1
                    wire_facts.append(WireFact("false_activity", at_ms))

                try:
                    tool_response = _synthetic_tool_response(message)
                except RunnerError as exc:
                    error_code = str(exc)
                    break
                if tool_response is not None:
                    await session.send(tool_response)

                try:
                    audio_bytes = _extract_output_audio(message)
                    audio_chunks = _count_output_audio_chunks(message)
                except RunnerError as exc:
                    error_code = str(exc)
                    break
                if audio_chunks:
                    if response_terminal:
                        audio_after_teardown_count += audio_chunks
                        error_code = "audio_after_terminal"
                        break
                    response_open = True
                    model_audio_chunk_count += audio_chunks
                    output_audio_bytes += audio_bytes
                    wire_facts.extend(
                        WireFact("audio_received", at_ms, response_ordinal=1)
                        for _ in range(audio_chunks)
                    )
                    if output_audio_bytes > MAX_OUTPUT_AUDIO_BYTES:
                        error_code = "runaway_output"
                        break

                if _has_response_terminal(message) and response_open:
                    response_open = False
                    response_terminal = True
                if usage_frame_count and sender_done.is_set() and not response_open:
                    break

            if sender_task is not None:
                if error_code is not None and not sender_task.done():
                    sender_task.cancel()
                try:
                    await sender_task
                except asyncio.CancelledError:
                    pass
            if error_code is None and sender_error is not None:
                error_code = sender_error
            if error_code is None and usage_frame_count == 0:
                error_code = "usage_metadata_missing"
            if error_code is None and response_open:
                error_code = "response_terminal_missing"
    except ProviderSessionClosed:
        abnormal_close_count += 1
        error_code = "provider_closed"
    except TimeoutError:
        raise
    except Exception:
        error_code = "connector_failure"
    finally:
        if sender_task is not None:
            if not sender_task.done():
                sender_task.cancel()
            try:
                await sender_task
            except (asyncio.CancelledError, TimeoutError):
                pass
        try:
            await session.close()
        except Exception:
            if error_code is None:
                error_code = "teardown_failure"

    return NoSpeechExecutionResult(
        complete=error_code is None,
        error_code=error_code,
        false_activity_count=false_activity_count,
        model_audio_chunk_count=model_audio_chunk_count,
        abnormal_close_count=abnormal_close_count,
        audio_after_teardown_count=audio_after_teardown_count,
        output_audio_bytes=output_audio_bytes,
        usage=usage,
        provider_request_count=1,
        wire_facts=tuple(wire_facts),
    )


@dataclass(slots=True)
class _RequestBudget:
    limit: int
    consumed: int = 0

    def consume(self) -> None:
        if self.consumed >= self.limit:
            raise RequestReservationError("provider request reservation exhausted")
        self.consumed += 1


class _ReservedConnector:
    def __init__(
        self,
        *,
        budget: _RequestBudget,
        credential: SecretCredential,
        factory: Callable[[SecretCredential], InjectedConnector],
    ) -> None:
        self._budget = budget
        self._credential = credential
        self._factory = factory

    async def connect(self, request: InjectedConnectionRequest) -> InjectedSession:
        self._budget.consume()
        connector = self._factory(self._credential)
        if connector is None or not callable(getattr(connector, "connect", None)):
            raise RunnerError("connector factory returned an invalid connector")
        return await connector.connect(request)


def _validate_ledger_custody_binding(
    *,
    campaign: CampaignApproval,
    ledger: LedgerCustodyClient,
    public_key: bytes,
    label: str,
) -> LedgerCustodyIdentity:
    identity = ledger.identity()
    if (
        not isinstance(identity, LedgerCustodyIdentity)
        or not isinstance(public_key, bytes)
        or len(public_key) != 32
        or sha256(public_key).hexdigest() != identity.public_key_sha256
        or campaign.ledger_instance_id != identity.ledger_instance_id
        or campaign.ledger_custodian_key_id != identity.key_id
        or campaign.ledger_custodian_public_key_sha256 != identity.public_key_sha256
        or campaign.ledger_location_sha256 != identity.ledger_location_sha256
    ):
        raise RunnerError(f"{label} ledger custody binding mismatch")
    return identity


def _replay_bound_ledger_snapshot(
    *,
    ledger: LedgerCustodyClient,
    public_key: bytes,
    identity: LedgerCustodyIdentity,
    campaign: CampaignApproval,
    authorization: AttemptAuthorization,
) -> CustodyLedgerState:
    state = validate_custody_ledger_snapshot(
        ledger.export_snapshot(),
        public_key=public_key,
        expected_key_id=identity.key_id,
        expected_ledger_instance_id=identity.ledger_instance_id,
        expected_campaign_id=campaign.campaign_id,
        expected_authorization_id=campaign.authorization_id,
        expected_preregistration_sha256=campaign.preregistration_sha256,
        expected_source_sha=campaign.source_sha,
        expected_ledger_location_sha256=campaign.ledger_location_sha256,
    )
    if not isinstance(state, CustodyLedgerState):
        raise RunnerError("custodian ledger snapshot is invalid")
    return state


def _require_exact_claim(
    claim: object,
    *,
    campaign: CampaignApproval,
    authorization: AttemptAuthorization,
    label: str,
) -> AttemptClaim:
    if (
        not isinstance(claim, AttemptClaim)
        or claim.campaign_id != campaign.campaign_id
        or claim.attempt_id != authorization.attempt_id
        or claim.attempt_index != authorization.attempt_index
        or not isinstance(claim.lease_id, str)
        or not SHA256.fullmatch(claim.lease_id)
        or claim.provider_requests_reserved != authorization.provider_request_reservation
        or claim.cost_reserved_microusd != authorization.cost_reservation_microusd
    ):
        raise RunnerError(f"{label} custodian returned an invalid active claim")
    return claim


def _require_postclaim_state(
    state: CustodyLedgerState,
    *,
    campaign: CampaignApproval,
    authorization: AttemptAuthorization,
    expected_claimed_at: datetime,
) -> None:
    if (
        state.phase != "development_collection"
        or state.active_attempt_id != authorization.attempt_id
        or state.completed_attempt_id is not None
        or len(state.attempt_ids) != authorization.attempt_index
        or state.attempt_ids[-1] != authorization.attempt_id
        or state.campaign_approval_sha256 != campaign.signed_payload_sha256
        or state.attempt_authorization_sha256 != authorization.signed_payload_sha256
        or state.attempt_claimed_at != expected_claimed_at
        or state.provider_requests_reserved
        != authorization.provider_request_reservation
        or state.cost_reserved_microusd != authorization.cost_reservation_microusd
        or state.development_capsule_sha256 is not None
        or state.holdout_execution_claimed
    ):
        raise RunnerError("signed ledger did not durably consume the attempt claim")


async def execute_authorized_attempt(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...] = (),
    preregistration: Mapping[str, Any],
    config: AuthorizedAttemptConfig,
    session_config: SessionExecutionConfig,
    campaign_envelope: Mapping[str, Any],
    attempt_envelope: Mapping[str, Any],
    ledger: LedgerCustodyClient,
    ledger_custodian_public_key: bytes,
    now: datetime,
    credential_loader: Callable[[str], SecretCredential],
    connector_factory: Callable[[SecretCredential], InjectedConnector],
    receipt_clock_factory: Callable[[object], Callable[[], int]],
    sleep_ms: Callable[[int], Awaitable[None]],
    pricing: PricingSchedule,
    custodian_public_key: bytes,
    custodian_key_id: str,
    capsule_path: Path,
) -> AttemptExecutionResult:
    """Execute one consumed attempt using only injected secret, transport, and sinks."""
    _validate_attempt_inputs(
        plans,
        no_speech_plans=no_speech_plans,
        config=config,
        session_config=session_config,
        pricing=pricing,
    )
    approval_public_key = _load_pinned_approval_public_key()
    _verify_execution_preregistration(
        preregistration,
        config=config,
        session_config=session_config,
        approval_public_key=approval_public_key,
        ledger=ledger,
        plans=plans,
        no_speech_plans=no_speech_plans,
        pricing=pricing,
        custodian_public_key=custodian_public_key,
        custodian_key_id=custodian_key_id,
        capsule_path=capsule_path,
        capsule_location_field="audit_capsule_location_sha256",
        required_split="development",
    )
    campaign = verify_campaign_approval(
        campaign_envelope,
        public_key=approval_public_key,
        expected_key_id=config.approval_key_id,
        expected_preregistration_sha256=config.preregistration_sha256,
        expected_source_sha=config.source_sha,
        now=now,
    )
    ledger_identity = _validate_ledger_custody_binding(
        campaign=campaign,
        ledger=ledger,
        public_key=ledger_custodian_public_key,
        label="campaign",
    )
    authorization = verify_attempt_authorization(
        attempt_envelope,
        public_key=approval_public_key,
        expected_key_id=config.approval_key_id,
        campaign=campaign,
        now=now,
    )
    if (
        compute_development_schedule_sha256(
            plans,
            no_speech_plans=no_speech_plans,
        )
        != preregistration["immutable_values"]["development_schedule_sha256"]
    ):
        raise RunnerError("preregistered development schedule digest mismatch")
    _validate_exact_development_schedule(plans, no_speech_plans=no_speech_plans)
    required_requests = sum(len(_connection_segments(plan)) for plan in plans) + len(
        no_speech_plans
    )
    if required_requests > authorization.provider_request_reservation:
        raise RunnerError("signed request reservation is insufficient")
    claim = _require_exact_claim(
        ledger.claim_attempt(
            campaign=campaign,
            authorization=authorization,
            now=now,
        ),
        campaign=campaign,
        authorization=authorization,
        label="development",
    )
    claimed_state = _replay_bound_ledger_snapshot(
        ledger=ledger,
        public_key=ledger_custodian_public_key,
        identity=ledger_identity,
        campaign=campaign,
        authorization=authorization,
    )
    _require_postclaim_state(
        claimed_state,
        campaign=campaign,
        authorization=authorization,
        expected_claimed_at=now,
    )

    budget = _RequestBudget(claim.provider_requests_reserved)
    cost_microusd = 0
    error_code: str | None = None
    capsule_handed_off = False
    capsule_sha256: str | None = None
    usage = UsageCounts()
    session_results: list[tuple[SessionPlan, SessionExecutionResult]] = []
    no_speech_results: list[tuple[NoSpeechWindowPlan, NoSpeechExecutionResult]] = []

    try:
        try:
            environment_identity_sha256 = _capture_current_execution_identity(
                expected_source_sha=config.source_sha
            )
            if (
                environment_identity_sha256
                != preregistration["immutable_values"]["environment_identity_sha256"]
            ):
                raise RunnerError("execution environment identity mismatch")
        except Exception:
            error_code = "source_identity_failed"

        credential: SecretCredential | None = None
        if error_code is None:
            try:
                credential = credential_loader(config.credential_reference)
                if not isinstance(credential, SecretCredential):
                    raise TypeError
            except Exception:
                error_code = "credential_lookup_failed"

        if error_code is None and credential is not None:
            connector = _ReservedConnector(
                budget=budget,
                credential=credential,
                factory=connector_factory,
            )
            try:
                session_results, no_speech_results = await asyncio.wait_for(
                    _execute_attempt_work(
                        plans,
                        no_speech_plans=no_speech_plans,
                        config=session_config,
                        connector=connector,
                        credential=credential,
                        receipt_clock_factory=receipt_clock_factory,
                        sleep_ms=sleep_ms,
                        pricing=pricing,
                        run_cost_limit_microusd=claim.cost_reserved_microusd,
                    ),
                    timeout=config.whole_run_timeout_seconds,
                )
            except TimeoutError:
                error_code = "whole_run_timeout"

        if error_code is None:
            failed = next(
                (result for _, result in session_results if not result.complete),
                None,
            )
            if failed is not None:
                error_code = failed.error_code or "session_failed"
        if error_code is None:
            failed_window = next(
                (result for _, result in no_speech_results if not result.complete),
                None,
            )
            if failed_window is not None:
                error_code = failed_window.error_code or "no_speech_window_failed"

        for _, result in session_results:
            usage = _add_usage(usage, result.usage)
        for _, result in no_speech_results:
            usage = _add_usage(usage, result.usage)
        cost_microusd = _cost_microusd(pricing, usage)
        if cost_microusd > claim.cost_reserved_microusd:
            error_code = "cost_reservation_exhausted"

        if error_code is None:
            try:
                capsule = _build_audit_capsule(
                    campaign_id=campaign.campaign_id,
                    policy_ms=config.policy_ms,
                    session_results=session_results,
                    no_speech_results=no_speech_results,
                )
                capsule_usage, capsule_failures = derive_audit_capsule_accounting(capsule)
                expected_capsule_usage = {
                    **usage.to_dict(),
                    "provider_requests": budget.consumed,
                }
                if (
                    capsule_failures
                    or not capsule_usage["metadata_complete"]
                    or any(
                        capsule_usage[field] != value
                        for field, value in expected_capsule_usage.items()
                    )
                    or _cost_microusd(
                        pricing,
                        UsageCounts(
                            input_audio_tokens=int(capsule_usage["input_audio_tokens"]),
                            output_audio_tokens=int(capsule_usage["output_audio_tokens"]),
                            input_text_tokens=int(capsule_usage["input_text_tokens"]),
                            output_text_tokens=int(capsule_usage["output_text_tokens"]),
                        ),
                    )
                    != cost_microusd
                ):
                    raise RunnerError("audit capsule accounting mismatch")
                sealed = seal_audit_capsule(
                    capsule,
                    custodian_public_key=custodian_public_key,
                    custodian_key_id=custodian_key_id,
                )
            except (MeasurementError, RunnerError, TypeError, ValueError):
                error_code = "audit_capsule_failed"
            else:
                try:
                    capsule_sha256 = _write_sealed_capsule(capsule_path, sealed)
                    capsule_handed_off = True
                except Exception:
                    error_code = "capsule_handoff_failed"
    finally:
        complete = error_code is None and capsule_handed_off
        usage_evidence_digest = usage_evidence_sha256(
            usage.to_dict(),
            provider_requests=budget.consumed,
            cost_microusd=cost_microusd,
        )
        if complete:
            if capsule_sha256 is None:
                raise RunnerError("completed attempt is missing its capsule digest")
            ledger.record_development_checkpoint(
                claim=claim,
                development_capsule_sha256=capsule_sha256,
                usage_evidence_sha256=usage_evidence_digest,
                actual_provider_requests=budget.consumed,
                actual_cost_microusd=cost_microusd,
                now=now,
            )
            final_state = _replay_bound_ledger_snapshot(
                ledger=ledger,
                public_key=ledger_custodian_public_key,
                identity=ledger_identity,
                campaign=campaign,
                authorization=authorization,
            )
            if (
                final_state.phase != "development_collection"
                or final_state.active_attempt_id != authorization.attempt_id
                or final_state.development_capsule_sha256 != capsule_sha256
                or final_state.development_usage_evidence_sha256
                != usage_evidence_digest
                or final_state.development_provider_requests != budget.consumed
                or final_state.development_cost_microusd != cost_microusd
                or final_state.final_ledger_head_sha256
                == claimed_state.final_ledger_head_sha256
            ):
                raise RunnerError("signed development checkpoint is not durable")
        else:
            ledger.record_terminal_outcome(
                claim=claim,
                outcome="failed",
                outage_enum=None,
                holdout_capsule_sha256=None,
                usage_evidence_sha256=usage_evidence_digest,
                actual_provider_requests=budget.consumed,
                actual_cost_microusd=cost_microusd,
                now=now,
            )
            final_state = _replay_bound_ledger_snapshot(
                ledger=ledger,
                public_key=ledger_custodian_public_key,
                identity=ledger_identity,
                campaign=campaign,
                authorization=authorization,
            )
            if (
                final_state.phase != "aborted"
                or final_state.active_attempt_id is not None
                or final_state.attempt_authorization_sha256
                != authorization.signed_payload_sha256
                or final_state.final_usage_evidence_sha256 != usage_evidence_digest
                or final_state.actual_provider_requests != budget.consumed
                or final_state.actual_cost_microusd != cost_microusd
                or final_state.final_ledger_head_sha256
                == claimed_state.final_ledger_head_sha256
            ):
                raise RunnerError("signed terminal outcome is not durable")

    return AttemptExecutionResult(
        complete=complete,
        error_code=error_code,
        provider_request_count=budget.consumed,
        cost_microusd=cost_microusd,
        capsule_handed_off=capsule_handed_off,
    )


async def execute_authorized_holdout(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
    preregistration: Mapping[str, Any],
    config: AuthorizedAttemptConfig,
    session_config: SessionExecutionConfig,
    campaign_envelope: Mapping[str, Any],
    attempt_envelope: Mapping[str, Any],
    ledger: LedgerCustodyClient,
    ledger_custodian_public_key: bytes,
    now: datetime,
    credential_loader: Callable[[str], SecretCredential],
    connector_factory: Callable[[SecretCredential], InjectedConnector],
    receipt_clock_factory: Callable[[object], Callable[[], int]],
    sleep_ms: Callable[[int], Awaitable[None]],
    pricing: PricingSchedule,
    custodian_public_key: bytes,
    custodian_key_id: str,
    capsule_path: Path,
) -> AttemptExecutionResult:
    """Resume one active post-lock attempt and execute its released holdout once."""
    _validate_attempt_inputs(
        plans,
        no_speech_plans=no_speech_plans,
        config=config,
        session_config=session_config,
        pricing=pricing,
    )
    _validate_exact_holdout_schedule(plans, no_speech_plans=no_speech_plans)
    approval_public_key = _load_pinned_approval_public_key()
    _verify_execution_preregistration(
        preregistration,
        config=config,
        session_config=session_config,
        approval_public_key=approval_public_key,
        ledger=ledger,
        plans=plans,
        no_speech_plans=no_speech_plans,
        pricing=pricing,
        custodian_public_key=custodian_public_key,
        custodian_key_id=custodian_key_id,
        capsule_path=capsule_path,
        capsule_location_field="holdout_capsule_location_sha256",
        required_split="holdout",
    )
    campaign = verify_campaign_approval(
        campaign_envelope,
        public_key=approval_public_key,
        expected_key_id=config.approval_key_id,
        expected_preregistration_sha256=config.preregistration_sha256,
        expected_source_sha=config.source_sha,
        now=now,
    )
    authorization = verify_attempt_authorization(
        attempt_envelope,
        public_key=approval_public_key,
        expected_key_id=config.approval_key_id,
        campaign=campaign,
        now=now,
    )
    ledger_identity = _validate_ledger_custody_binding(
        campaign=campaign,
        ledger=ledger,
        public_key=ledger_custodian_public_key,
        label="holdout",
    )
    state = _replay_bound_ledger_snapshot(
        ledger=ledger,
        public_key=ledger_custodian_public_key,
        identity=ledger_identity,
        campaign=campaign,
        authorization=authorization,
    )
    holdout_manifest_sha256 = compute_holdout_schedule_sha256(
        plans,
        no_speech_plans=no_speech_plans,
    )
    if not isinstance(state, CustodyLedgerState) or (
        state.phase != "holdout_collection"
        or state.active_attempt_id != authorization.attempt_id
        or state.completed_attempt_id is not None
        or state.campaign_approval_sha256 != campaign.signed_payload_sha256
        or state.attempt_authorization_sha256 != authorization.signed_payload_sha256
        or state.selected_policy_ms != config.policy_ms
        or state.holdout_manifest_sha256 != holdout_manifest_sha256
        or state.holdout_execution_claimed
        or state.development_usage_evidence_sha256 is None
    ):
        raise RunnerError("holdout release does not match the active signed attempt")
    required_requests = sum(len(_connection_segments(plan)) for plan in plans) + len(
        no_speech_plans
    )
    remaining_requests = authorization.provider_request_reservation - (
        state.development_provider_requests
    )
    remaining_cost_microusd = (
        authorization.cost_reservation_microusd - state.development_cost_microusd
    )
    if (
        remaining_requests < required_requests
        or remaining_cost_microusd < 0
        or state.development_provider_requests < 0
        or state.development_cost_microusd < 0
    ):
        raise RunnerError("holdout remaining reservation is insufficient")
    claim = _require_exact_claim(
        ledger.resume_holdout(
            campaign=campaign,
            authorization=authorization,
            selected_policy_ms=config.policy_ms,
            holdout_manifest_sha256=holdout_manifest_sha256,
            expected_ledger_head_sha256=state.final_ledger_head_sha256,
            now=now,
        ),
        campaign=campaign,
        authorization=authorization,
        label="holdout",
    )
    resumed_state = _replay_bound_ledger_snapshot(
        ledger=ledger,
        public_key=ledger_custodian_public_key,
        identity=ledger_identity,
        campaign=campaign,
        authorization=authorization,
    )
    if (
        resumed_state.phase != "holdout_collection"
        or resumed_state.active_attempt_id != authorization.attempt_id
        or resumed_state.campaign_approval_sha256 != campaign.signed_payload_sha256
        or resumed_state.attempt_authorization_sha256
        != authorization.signed_payload_sha256
        or resumed_state.selected_policy_ms != config.policy_ms
        or resumed_state.holdout_manifest_sha256 != holdout_manifest_sha256
        or not resumed_state.holdout_execution_claimed
        or resumed_state.provider_requests_reserved
        != authorization.provider_request_reservation
        or resumed_state.cost_reserved_microusd
        != authorization.cost_reservation_microusd
        or resumed_state.development_usage_evidence_sha256
        != state.development_usage_evidence_sha256
        or resumed_state.final_ledger_head_sha256 == state.final_ledger_head_sha256
    ):
        raise RunnerError("signed ledger did not durably consume the holdout claim")

    budget = _RequestBudget(remaining_requests)
    holdout_cost_microusd = 0
    error_code: str | None = None
    capsule_handed_off = False
    capsule_sha256: str | None = None
    usage = UsageCounts()
    session_results: list[tuple[SessionPlan, SessionExecutionResult]] = []
    no_speech_results: list[tuple[NoSpeechWindowPlan, NoSpeechExecutionResult]] = []
    try:
        try:
            environment_identity_sha256 = _capture_current_execution_identity(
                expected_source_sha=config.source_sha
            )
            if (
                environment_identity_sha256
                != preregistration["immutable_values"]["environment_identity_sha256"]
            ):
                raise RunnerError("execution environment identity mismatch")
        except Exception:
            error_code = "source_identity_failed"

        credential: SecretCredential | None = None
        if error_code is None:
            try:
                credential = credential_loader(config.credential_reference)
                if not isinstance(credential, SecretCredential):
                    raise TypeError
            except Exception:
                error_code = "credential_lookup_failed"

        if error_code is None and credential is not None:
            connector = _ReservedConnector(
                budget=budget,
                credential=credential,
                factory=connector_factory,
            )
            try:
                session_results, no_speech_results = await asyncio.wait_for(
                    _execute_attempt_work(
                        plans,
                        no_speech_plans=no_speech_plans,
                        config=session_config,
                        connector=connector,
                        credential=credential,
                        receipt_clock_factory=receipt_clock_factory,
                        sleep_ms=sleep_ms,
                        pricing=pricing,
                        run_cost_limit_microusd=remaining_cost_microusd,
                    ),
                    timeout=config.whole_run_timeout_seconds,
                )
            except TimeoutError:
                error_code = "whole_run_timeout"

        if error_code is None:
            failed = next((result for _, result in session_results if not result.complete), None)
            if failed is not None:
                error_code = failed.error_code or "session_failed"
        if error_code is None:
            failed_window = next(
                (result for _, result in no_speech_results if not result.complete),
                None,
            )
            if failed_window is not None:
                error_code = failed_window.error_code or "no_speech_window_failed"

        for _, result in session_results:
            usage = _add_usage(usage, result.usage)
        for _, result in no_speech_results:
            usage = _add_usage(usage, result.usage)
        holdout_cost_microusd = _cost_microusd(pricing, usage)
        if holdout_cost_microusd > remaining_cost_microusd:
            error_code = "cost_reservation_exhausted"

        if error_code is None:
            try:
                capsule = _build_audit_capsule(
                    campaign_id=campaign.campaign_id,
                    policy_ms=config.policy_ms,
                    session_results=session_results,
                    no_speech_results=no_speech_results,
                )
                capsule_usage, capsule_failures = derive_audit_capsule_accounting(capsule)
                if (
                    capsule_failures
                    or not capsule_usage["metadata_complete"]
                    or capsule_usage["provider_requests"] != budget.consumed
                    or any(
                        capsule_usage[field] != value
                        for field, value in usage.to_dict().items()
                    )
                ):
                    raise RunnerError("holdout capsule accounting mismatch")
                sealed = seal_audit_capsule(
                    capsule,
                    custodian_public_key=custodian_public_key,
                    custodian_key_id=custodian_key_id,
                )
            except (MeasurementError, RunnerError, TypeError, ValueError):
                error_code = "audit_capsule_failed"
            else:
                try:
                    capsule_sha256 = _write_sealed_capsule(capsule_path, sealed)
                    capsule_handed_off = True
                except Exception:
                    error_code = "capsule_handoff_failed"
    finally:
        complete = error_code is None and capsule_handed_off
        total_requests = state.development_provider_requests + budget.consumed
        total_cost_microusd = state.development_cost_microusd + holdout_cost_microusd
        holdout_usage_digest = usage_evidence_sha256(
            usage.to_dict(),
            provider_requests=budget.consumed,
            cost_microusd=holdout_cost_microusd,
        )
        final_usage_digest = combined_usage_evidence_sha256(
            development_usage_evidence_sha256=state.development_usage_evidence_sha256,
            holdout_usage_evidence_sha256=holdout_usage_digest,
            provider_requests=total_requests,
            cost_microusd=total_cost_microusd,
        )
        ledger.record_terminal_outcome(
            claim=claim,
            outcome="completed" if complete else "failed",
            outage_enum=None,
            holdout_capsule_sha256=capsule_sha256 if complete else None,
            usage_evidence_sha256=final_usage_digest,
            actual_provider_requests=total_requests,
            actual_cost_microusd=total_cost_microusd,
            now=now,
        )
        final_state = _replay_bound_ledger_snapshot(
            ledger=ledger,
            public_key=ledger_custodian_public_key,
            identity=ledger_identity,
            campaign=campaign,
            authorization=authorization,
        )
        if (
            final_state.phase != ("completed" if complete else "aborted")
            or final_state.active_attempt_id is not None
            or final_state.completed_attempt_id
            != (authorization.attempt_id if complete else None)
            or final_state.attempt_authorization_sha256
            != authorization.signed_payload_sha256
            or final_state.final_usage_evidence_sha256 != final_usage_digest
            or final_state.actual_provider_requests != total_requests
            or final_state.actual_cost_microusd != total_cost_microusd
            or final_state.holdout_capsule_sha256
            != (capsule_sha256 if complete else None)
            or final_state.final_ledger_head_sha256
            == resumed_state.final_ledger_head_sha256
        ):
            raise RunnerError("signed holdout terminal outcome is not durable")

    return AttemptExecutionResult(
        complete=complete,
        error_code=error_code,
        provider_request_count=total_requests,
        cost_microusd=total_cost_microusd,
        capsule_handed_off=capsule_handed_off,
    )


async def _execute_attempt_work(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
    config: SessionExecutionConfig,
    connector: InjectedConnector,
    credential: SecretCredential,
    receipt_clock_factory: Callable[[object], Callable[[], int]],
    sleep_ms: Callable[[int], Awaitable[None]],
    pricing: PricingSchedule,
    run_cost_limit_microusd: int,
) -> tuple[
    list[tuple[SessionPlan, SessionExecutionResult]],
    list[tuple[NoSpeechWindowPlan, NoSpeechExecutionResult]],
]:
    results: list[tuple[SessionPlan, SessionExecutionResult]] = []
    running_usage = UsageCounts()
    output_audio_bytes = 0
    for plan in plans:
        result = await execute_injected_session(
            plan,
            config=config,
            connector=connector,
            credential=credential,
            receipt_clock_ms=receipt_clock_factory(plan),
            sleep_ms=sleep_ms,
        )
        running_usage = _add_usage(running_usage, result.usage)
        output_audio_bytes += result.output_audio_bytes
        session_cost = _cost_microusd(pricing, result.usage)
        if session_cost > MAX_COST_PER_SESSION_MICROUSD:
            result = replace(result, complete=False, error_code="session_cost_cap_exceeded")
        elif _cost_microusd(pricing, running_usage) > run_cost_limit_microusd:
            result = replace(result, complete=False, error_code="cost_reservation_exhausted")
        elif output_audio_bytes > MAX_OUTPUT_AUDIO_PER_RUN_BYTES:
            result = replace(result, complete=False, error_code="run_output_audio_cap_exceeded")
        results.append((plan, result))
        if not result.complete:
            return results, []
    window_results: list[tuple[NoSpeechWindowPlan, NoSpeechExecutionResult]] = []
    for plan in no_speech_plans:
        result = await execute_injected_no_speech_window(
            plan,
            config=config,
            connector=connector,
            credential=credential,
            receipt_clock_ms=receipt_clock_factory(plan),
            sleep_ms=sleep_ms,
        )
        running_usage = _add_usage(running_usage, result.usage)
        output_audio_bytes += result.output_audio_bytes
        session_cost = _cost_microusd(pricing, result.usage)
        if session_cost > MAX_COST_PER_SESSION_MICROUSD:
            result = replace(result, complete=False, error_code="session_cost_cap_exceeded")
        elif _cost_microusd(pricing, running_usage) > run_cost_limit_microusd:
            result = replace(result, complete=False, error_code="cost_reservation_exhausted")
        elif output_audio_bytes > MAX_OUTPUT_AUDIO_PER_RUN_BYTES:
            result = replace(result, complete=False, error_code="run_output_audio_cap_exceeded")
        window_results.append((plan, result))
        if not result.complete:
            break
    return results, window_results


def _validate_attempt_inputs(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
    config: AuthorizedAttemptConfig,
    session_config: SessionExecutionConfig,
    pricing: PricingSchedule,
) -> None:
    if not isinstance(config, AuthorizedAttemptConfig):
        raise TypeError("config must be an AuthorizedAttemptConfig")
    if not isinstance(session_config, SessionExecutionConfig):
        raise TypeError("session_config must be a SessionExecutionConfig")
    if (
        not isinstance(plans, tuple)
        or not 1 <= len(plans) <= 64
        or any(not isinstance(plan, SessionPlan) for plan in plans)
    ):
        raise RunnerError("attempt session plans are invalid")
    if len({plan.session_ordinal for plan in plans}) != len(plans):
        raise RunnerError("attempt session ordinals must be unique")
    if (
        not isinstance(no_speech_plans, tuple)
        or len(no_speech_plans) > 64
        or any(not isinstance(plan, NoSpeechWindowPlan) for plan in no_speech_plans)
    ):
        raise RunnerError("attempt no-speech plans are invalid")
    if len({plan.window_ordinal for plan in no_speech_plans}) != len(no_speech_plans):
        raise RunnerError("attempt no-speech ordinals must be unique")
    for plan in (*plans, *no_speech_plans):
        for replay_input in plan.replay_inputs:
            if replay_input.kind != "audio":
                continue
            expected_bytes = replay_input.duration_ms * 16_000 * 2 // 1_000
            if (
                replay_input.duration_ms <= 0
                or not isinstance(replay_input.audio, bytes)
                or not replay_input.audio
                or len(replay_input.audio) % 2
                or not expected_bytes - 2 <= len(replay_input.audio) <= expected_bytes
            ):
                raise RunnerError("audio bytes and declared duration are inconsistent")
    activity_ordinals = [
        activity.activity_ordinal for plan in plans for activity in plan.activities
    ]
    if len(set(activity_ordinals)) != len(activity_ordinals):
        raise RunnerError("attempt activity ordinals must be unique")
    if (
        sum(
            replay_input.duration_ms
            for plan in (*plans, *no_speech_plans)
            for replay_input in plan.replay_inputs
            if replay_input.kind == "audio"
        )
        > 3_600_000
    ):
        raise RunnerError("attempt input audio duration exceeds the fixed cap")
    if sum(len(_connection_segments(plan)) for plan in plans) + len(no_speech_plans) > 128:
        raise RunnerError("attempt provider request count exceeds the fixed cap")
    if not isinstance(pricing, PricingSchedule):
        raise TypeError("pricing must be a PricingSchedule")
    if pricing.model != session_config.model.removeprefix("models/"):
        raise RunnerError("pricing and model identity mismatch")


def _validate_exact_development_schedule(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
) -> None:
    activities = tuple(activity for plan in plans for activity in plan.activities)
    language_counts = Counter(activity.language for activity in activities)
    condition_counts = Counter(activity.condition for activity in activities)
    if (
        len(activities) != 128
        or len(no_speech_plans) != 32
        or {plan.session_ordinal for plan in plans} != set(range(32))
        or {activity.activity_ordinal for activity in activities} != set(range(128))
        or {plan.window_ordinal for plan in no_speech_plans} != set(range(32))
        or {plan.split for plan in (*plans, *no_speech_plans)} != {"development"}
        or set(language_counts) != VALID_LANGUAGES
        or set(language_counts.values()) != {16}
        or condition_counts
        != Counter(
            {
                "clean": 32,
                "twilio_codec_only": 32,
                "acoustic_impairment": 32,
                "interaction_stress": 32,
            }
        )
    ):
        raise RunnerError("development schedule cardinality is invalid")


def _validate_exact_holdout_schedule(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
) -> None:
    activities = tuple(activity for plan in plans for activity in plan.activities)
    language_counts = Counter(activity.language for activity in activities)
    condition_counts = Counter(activity.condition for activity in activities)
    if (
        len(activities) != 128
        or len(no_speech_plans) != 32
        or {plan.session_ordinal for plan in plans} != set(range(32, 64))
        or {activity.activity_ordinal for activity in activities} != set(range(128, 256))
        or {plan.window_ordinal for plan in no_speech_plans} != set(range(32, 64))
        or {plan.split for plan in (*plans, *no_speech_plans)} != {"holdout"}
        or set(language_counts) != VALID_LANGUAGES
        or set(language_counts.values()) != {16}
        or condition_counts
        != Counter(
            {
                "clean": 32,
                "twilio_codec_only": 32,
                "acoustic_impairment": 32,
                "interaction_stress": 32,
            }
        )
    ):
        raise RunnerError("holdout schedule cardinality is invalid")


def compute_development_schedule_sha256(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
) -> str:
    """Bind replay order, references, timing, and audio bytes without publishing payloads."""
    return _compute_schedule_sha256(
        plans,
        no_speech_plans=no_speech_plans,
        schema_id="gate_0b_development_schedule_identity_v1",
    )


def compute_holdout_schedule_sha256(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
) -> str:
    """Bind the post-lock holdout schedule released by the custodian."""
    return _compute_schedule_sha256(
        plans,
        no_speech_plans=no_speech_plans,
        schema_id="gate_0b_holdout_schedule_identity_v1",
    )


def _compute_schedule_sha256(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
    schema_id: str,
) -> str:

    def replay_identity(replay_input: Gate0BReplayInput) -> dict[str, Any]:
        return {
            "kind": replay_input.kind,
            "at_ms": replay_input.at_ms,
            "epoch": replay_input.epoch,
            "activity_ordinal": replay_input.activity_ordinal,
            "duration_ms": replay_input.duration_ms,
            "audio_sha256": (
                sha256(replay_input.audio).hexdigest() if replay_input.audio else None
            ),
        }

    value = {
        "schema_id": schema_id,
        "sessions": [
            {
                "session_ordinal": plan.session_ordinal,
                "split": plan.split,
                "activities": [
                    {
                        "activity_ordinal": activity.activity_ordinal,
                        "split": activity.split,
                        "language": activity.language,
                        "condition": activity.condition,
                        "scenario_tags": list(activity.scenario_tags),
                        "reference_sha256": sha256(
                            canonical_json_bytes(
                                {
                                    "text": activity.reference.text,
                                    "critical_spans": [
                                        {
                                            "kind": span.kind.value,
                                            "text": span.text,
                                            "language": span.language,
                                        }
                                        for span in activity.reference.critical_spans
                                    ],
                                }
                            )
                        ).hexdigest(),
                        "expected_lifecycle_status": activity.expected_lifecycle_status,
                        "expected_epoch": activity.expected_epoch,
                        "start_at_ms": activity.start_at_ms,
                        "end_at_ms": activity.end_at_ms,
                    }
                    for activity in plan.activities
                ],
                "replay_inputs": [replay_identity(value) for value in plan.replay_inputs],
            }
            for plan in plans
        ],
        "no_speech_windows": [
            {
                "window_ordinal": plan.window_ordinal,
                "split": plan.split,
                "condition": plan.condition,
                "replay_inputs": [replay_identity(value) for value in plan.replay_inputs],
            }
            for plan in no_speech_plans
        ],
    }
    return sha256(canonical_json_bytes(value)).hexdigest()


def _verify_execution_preregistration(
    document: Mapping[str, Any],
    *,
    config: AuthorizedAttemptConfig,
    session_config: SessionExecutionConfig,
    approval_public_key: bytes,
    ledger: LedgerCustodyClient,
    plans: tuple[SessionPlan, ...],
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
    pricing: PricingSchedule,
    custodian_public_key: bytes,
    custodian_key_id: str,
    capsule_path: Path,
    capsule_location_field: str,
    required_split: str,
) -> None:
    """Recompute the approved document and bind every directly observable input."""
    if not isinstance(document, Mapping):
        raise RunnerError("preregistration document is invalid")
    immutable = document.get("immutable_values")
    if not isinstance(immutable, Mapping):
        raise RunnerError("preregistration document is invalid")
    try:
        values = {
            "schema_id": "gate_0b_preregistration_values_v1",
            **{field: immutable[field] for field in PREREGISTRATION_EXTERNAL_FIELDS},
        }
    except KeyError as exc:
        raise RunnerError("preregistration document is invalid") from exc
    expected = build_preregistration(values)
    if dict(document) != expected:
        raise RunnerError("preregistration document or digest mismatch")

    if not isinstance(ledger, LedgerCustodyClient):
        raise RunnerError("preregistration ledger custody client is unavailable")
    ledger_identity = ledger.identity()
    if not isinstance(ledger_identity, LedgerCustodyIdentity):
        raise RunnerError("preregistration ledger custody identity is invalid")
    if not isinstance(approval_public_key, bytes) or not isinstance(custodian_public_key, bytes):
        raise RunnerError("preregistration trust root is invalid")
    if capsule_location_field not in {
        "audit_capsule_location_sha256",
        "holdout_capsule_location_sha256",
    } or required_split not in VALID_SPLITS:
        raise RunnerError("preregistration execution stage is invalid")
    observable = {
        "preregistration_sha256": config.preregistration_sha256,
        "source_sha": config.source_sha,
        "approval_key_id": config.approval_key_id,
        "credential_reference": config.credential_reference,
        "approval_public_key_sha256": sha256(approval_public_key).hexdigest(),
        "custodian_key_id": custodian_key_id,
        "custodian_public_key_sha256": sha256(custodian_public_key).hexdigest(),
        "model": session_config.model,
        "endpoint": session_config.endpoint,
        "project": session_config.project,
        "setup_sha256": sha256(
            canonical_json_bytes(build_gate0b_setup_identity(session_config))
        ).hexdigest(),
        "pricing_sha256": pricing.artifact_sha256,
        "runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "evaluator_sha256": sha256(
            (REPO_ROOT / "scripts/evaluate_gemini_caller_turn_qualification.py").read_bytes()
        ).hexdigest(),
        "ledger_instance_id": ledger_identity.ledger_instance_id,
        "ledger_custodian_key_id": ledger_identity.key_id,
        "ledger_custodian_public_key_sha256": ledger_identity.public_key_sha256,
        "ledger_location_sha256": ledger_identity.ledger_location_sha256,
        capsule_location_field: artifact_location_sha256(capsule_path),
    }
    expected_observable = {
        "preregistration_sha256": expected["preregistration_sha256"],
        **{field: immutable[field] for field in observable if field != "preregistration_sha256"},
    }
    if observable != expected_observable:
        raise RunnerError("preregistration execution binding mismatch")
    if (
        config.policy_ms not in immutable["candidate_policies_ms"]
        or session_config.session_timeout_seconds
        > immutable["usage_caps"]["session_timeout_seconds"]
        or config.whole_run_timeout_seconds
        > immutable["usage_caps"]["whole_run_wall_clock_seconds"]
    ):
        raise RunnerError("preregistration execution cap mismatch")

    all_splits = {plan.split for plan in (*plans, *no_speech_plans)}
    if all_splits != {required_split}:
        raise RunnerError("executor split does not match its preregistration stage")


def _load_pinned_approval_public_key() -> bytes:
    path = PINNED_APPROVAL_ROOT_PATH
    if path.is_symlink() or not path.is_file() or path.parent.is_symlink():
        raise RunnerError("pinned approval trust root is unavailable")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RunnerError("pinned approval trust root is unavailable") from exc
    if len(data) != 32:
        raise RunnerError("pinned approval trust root is unprovisioned")
    return data


def _capture_current_execution_identity(*, expected_source_sha: str) -> str:
    return execution_identity_sha256(
        REPO_ROOT,
        expected_source_sha=expected_source_sha,
    )


def artifact_location_sha256(path: Path) -> str:
    """Hash one validated, absent, outside-repository artifact destination."""
    canonical = _canonical_artifact_path(path)
    return sha256(str(canonical).encode("utf-8")).hexdigest()


def _canonical_artifact_path(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute() or path.name in {"", ".", ".."}:
        raise RunnerError("artifact destination is invalid")
    if path.exists() or path.is_symlink():
        raise RunnerError("artifact destination must be absent")
    for ancestor in path.parents:
        if ancestor.is_symlink():
            raise RunnerError("artifact destination ancestors must not be symlinks")
    parent = path.parent.resolve()
    if not parent.is_dir():
        raise RunnerError("artifact destination parent is unavailable")
    canonical = parent / path.name
    try:
        canonical.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return canonical
    raise RunnerError("artifact destination must be outside the repository")


def _write_sealed_capsule(path: Path, envelope: Mapping[str, Any]) -> str:
    canonical = _canonical_artifact_path(path)
    payload = canonical_json_bytes(envelope) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(canonical, flags, 0o600)
    except OSError as exc:
        raise RunnerError("capsule destination is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RunnerError("capsule destination is not a private regular file")
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise RunnerError("capsule write did not make progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(canonical.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return sha256(payload[:-1]).hexdigest()


def _build_audit_capsule(
    *,
    campaign_id: str,
    policy_ms: int,
    session_results: Sequence[tuple[SessionPlan, SessionExecutionResult]],
    no_speech_results: Sequence[tuple[NoSpeechWindowPlan, NoSpeechExecutionResult]],
) -> dict[str, Any]:
    activities: list[dict[str, Any]] = []
    accounting_units: list[dict[str, Any]] = []
    for plan, result in session_results:
        events = [
            {
                "kind": event.kind.value,
                "at_ms": event.at_ms,
                "sequence": event.sequence,
                "epoch": event.epoch,
                "text": event.text,
            }
            for event in result.audit_events
        ]
        latest_event_ms = max((event.at_ms for event in result.audit_events), default=0)
        accounting_units.append(
            _accounting_unit(
                kind="session",
                ordinal=plan.session_ordinal,
                replay_inputs=plan.replay_inputs,
                observed_elapsed_ms=max(
                    latest_event_ms,
                    _replay_observation_end_ms(plan.replay_inputs),
                ),
                result=result,
            )
        )
        for activity in plan.activities:
            wire = result.wire_observations[activity.activity_ordinal]
            activities.append(
                {
                    "activity_ordinal": activity.activity_ordinal,
                    "session_ordinal": plan.session_ordinal,
                    "split": activity.split,
                    "language": activity.language,
                    "condition": activity.condition,
                    "scenario_tags": list(activity.scenario_tags),
                    "reference_text": activity.reference.text,
                    "critical_spans": [
                        {
                            "kind": span.kind.value,
                            "text": span.text,
                            "language": span.language,
                        }
                        for span in activity.reference.critical_spans
                    ],
                    "events": events,
                    "expected_lifecycle_status": activity.expected_lifecycle_status,
                    "expected_epoch": activity.expected_epoch,
                    "advance_to_ms": max(
                        activity.end_at_ms + max(VALID_POLICIES_MS),
                        latest_event_ms,
                    ),
                    "wire_facts": _wire_facts(activity, wire),
                }
            )
    accounting_units.extend(
        _accounting_unit(
            kind="no_speech_window",
            ordinal=plan.window_ordinal,
            replay_inputs=plan.replay_inputs,
            observed_elapsed_ms=max(
                _replay_observation_end_ms(plan.replay_inputs),
                max((fact.at_ms for fact in result.wire_facts), default=0),
            ),
            result=result,
        )
        for plan, result in no_speech_results
    )
    splits = {plan.split for plan, _ in (*session_results, *no_speech_results)}
    if len(splits) != 1:
        raise RunnerError("audit capsule results must contain exactly one split")
    return {
        "schema_id": "gate_0b_audit_capsule_v2",
        "campaign_id": campaign_id,
        "policy_ms": policy_ms,
        "accounting": {
            "schema_id": "gate_0b_capsule_accounting_v1",
            "split": next(iter(splits)),
            "units": accounting_units,
        },
        "activities": activities,
        "no_speech_windows": [
            {
                "window_ordinal": plan.window_ordinal,
                "split": plan.split,
                "condition": plan.condition,
                "wire_facts": [fact.to_dict() for fact in result.wire_facts],
            }
            for plan, result in no_speech_results
        ],
    }


def _accounting_unit(
    *,
    kind: str,
    ordinal: int,
    replay_inputs: Sequence[Gate0BReplayInput],
    observed_elapsed_ms: int,
    result: SessionExecutionResult | NoSpeechExecutionResult,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "ordinal": ordinal,
        "metadata_complete": result.complete and result.error_code is None,
        "complete": result.complete,
        "error_code": result.error_code,
        "provider_request_count": result.provider_request_count,
        "observed_elapsed_ms": observed_elapsed_ms,
        "input_audio_duration_ms": sum(
            value.duration_ms for value in replay_inputs if value.kind == "audio"
        ),
        "output_audio_bytes": result.output_audio_bytes,
        **result.usage.to_dict(),
    }


def _replay_observation_end_ms(replay_inputs: Sequence[Gate0BReplayInput]) -> int:
    return max(
        (
            value.at_ms + (value.duration_ms if value.kind == "audio" else 0)
            for value in replay_inputs
        ),
        default=0,
    )


def _wire_facts(
    activity: SessionActivityPlan,
    wire: WireObservation,
) -> list[dict[str, Any]]:
    facts = [
        _wire_fact("caller_activity_start", activity.start_at_ms, activity.activity_ordinal),
        _wire_fact("caller_activity_end", activity.end_at_ms, activity.activity_ordinal),
    ]
    if wire.first_audio_ms is not None:
        at_ms = activity.end_at_ms + wire.first_audio_ms
        facts.extend(
            (
                _wire_fact("response_open", at_ms, activity.activity_ordinal, response_ordinal=1),
                _wire_fact("audio_received", at_ms, activity.activity_ordinal, response_ordinal=1),
            )
        )
    if wire.interruption_tail_ms is not None:
        facts.append(
            _wire_fact(
                "audio_received",
                activity.start_at_ms + wire.interruption_tail_ms,
                activity.activity_ordinal,
                response_ordinal=1,
            )
        )
    return facts


def _wire_fact(
    kind: str,
    at_ms: int,
    activity_ordinal: int,
    *,
    response_ordinal: int | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "at_ms": at_ms,
        "response_ordinal": response_ordinal,
        "activity_ordinal": activity_ordinal,
    }


def _cost_microusd(pricing: PricingSchedule, usage: UsageCounts) -> int:
    value = pricing.cost_usd(**usage.to_dict()) * 1_000_000
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _add_usage(left: UsageCounts, right: UsageCounts) -> UsageCounts:
    return UsageCounts(
        input_audio_tokens=left.input_audio_tokens + right.input_audio_tokens,
        output_audio_tokens=left.output_audio_tokens + right.output_audio_tokens,
        input_text_tokens=left.input_text_tokens + right.input_text_tokens,
        output_text_tokens=left.output_text_tokens + right.output_text_tokens,
    )


def _usage_snapshot_is_monotonic(previous: UsageCounts, current: UsageCounts) -> bool:
    return all(
        current_value >= previous_value
        for current_value, previous_value in zip(
            current.to_dict().values(),
            previous.to_dict().values(),
            strict=True,
        )
    )


async def _receive_expected_interaction(
    session: InjectedSession,
    *,
    expected_kind: str,
    config: SessionExecutionConfig,
    adapter: GeminiTurnEventAdapter,
    secondary_reducer: Callable[..., ReductionResult],
    receipt_clock_ms: Callable[[], int],
    first_sequence: int,
    epoch: int,
) -> tuple[
    tuple[CallerTurnEvent, ...],
    set[str],
    dict[str, Any] | None,
    str | None,
]:
    message = await session.receive()
    at_ms = receipt_clock_ms()
    if not isinstance(message, Mapping):
        return (), set(), None, "interaction_message_missing"
    try:
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return (), set(), None, "malformed_message"
    if len(encoded) > config.max_message_bytes:
        return (), set(), None, "message_too_large"
    if "goAway" in message:
        return (), set(), None, "provider_goaway"
    if "usageMetadata" in message:
        return (), set(), None, "premature_usage_metadata"
    try:
        if _extract_output_audio(message):
            return (), set(), None, "unexpected_interaction_audio"
    except RunnerError as exc:
        return (), set(), None, str(exc)

    primary_batch = adapter.adapt_message(
        message,
        at_ms=at_ms,
        first_sequence=first_sequence,
        epoch=epoch,
    )
    secondary = secondary_reducer(
        message,
        at_ms=at_ms,
        first_sequence=first_sequence,
        epoch=epoch,
    )
    primary = ReductionResult(
        status=primary_batch.status.value,
        events=primary_batch.events,
        rejection_code=(
            primary_batch.rejection_code.value if primary_batch.rejection_code is not None else None
        ),
    )
    if primary.status != secondary.status or primary.rejection_code != secondary.rejection_code:
        return (), set(), None, "reducer_disagreement"
    try:
        require_reducer_agreement(primary.events, secondary.events)
    except MeasurementError:
        return (), set(), None, "reducer_disagreement"
    if primary.status == GeminiTurnEventDecodeStatus.REJECTED.value:
        return (), set(), None, primary.rejection_code or "malformed_message"

    event_markers = {
        CallerTurnEventKind.TOOL_CALL_STARTED: "expect_synchronous_tool",
        CallerTurnEventKind.TOOL_CALL_CANCELLED: "expect_tool_cancellation",
        CallerTurnEventKind.INTERRUPTED: "expect_interruption",
    }
    observed = {
        event_markers[event.kind] for event in primary.events if event.kind in event_markers
    }
    if expected_kind not in observed:
        return (), set(), None, "expected_interaction_missing"
    try:
        response = _synthetic_tool_response(message)
    except RunnerError as exc:
        return (), set(), None, str(exc)
    if expected_kind == "expect_synchronous_tool" and response is None:
        return (), set(), None, "expected_tool_call_missing"
    return primary.events, observed, response, None


def _observed_interaction_kinds(events: Sequence[CallerTurnEvent]) -> set[str]:
    markers = {
        CallerTurnEventKind.TOOL_CALL_STARTED: "expect_synchronous_tool",
        CallerTurnEventKind.TOOL_CALL_CANCELLED: "expect_tool_cancellation",
        CallerTurnEventKind.INTERRUPTED: "expect_interruption",
    }
    return {markers[event.kind] for event in events if event.kind in markers}


def independent_reduce_message(
    message: object,
    *,
    at_ms: int,
    first_sequence: int,
    epoch: int,
) -> ReductionResult:
    """Second, separately implemented reduction of supported provider fields."""
    if not isinstance(message, Mapping):
        return ReductionResult("rejected", (), "malformed_message")
    try:
        events: list[CallerTurnEvent] = []
        content = message.get("serverContent")
        if content is not None and not isinstance(content, Mapping):
            raise TypeError
        content = content or {}
        transcript = _secondary_transcript(content)
        if not transcript:
            transcript = _secondary_transcript(message)
        if transcript:
            events.append(
                CallerTurnEvent(
                    CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
                    at_ms,
                    first_sequence + len(events),
                    epoch,
                    transcript,
                )
            )
        if "modelTurn" in content:
            model_turn = content["modelTurn"]
            if not isinstance(model_turn, Mapping) or not isinstance(
                model_turn.get("parts", []),
                list,
            ):
                raise TypeError
            if model_turn.get("parts", []):
                events.append(
                    CallerTurnEvent(
                        CallerTurnEventKind.MODEL_OUTPUT_STARTED,
                        at_ms,
                        first_sequence + len(events),
                        epoch,
                    )
                )
        for field, kind in (
            ("generationComplete", CallerTurnEventKind.GENERATION_COMPLETE),
            ("turnComplete", CallerTurnEventKind.TURN_COMPLETE),
            ("interrupted", CallerTurnEventKind.INTERRUPTED),
        ):
            if field in content:
                if not isinstance(content[field], bool):
                    raise TypeError
                if content[field]:
                    events.append(
                        CallerTurnEvent(
                            kind,
                            at_ms,
                            first_sequence + len(events),
                            epoch,
                        )
                    )
        if "toolCall" in message:
            tool = message["toolCall"]
            if not isinstance(tool, Mapping) or not isinstance(
                tool.get("functionCalls", []),
                list,
            ):
                raise TypeError
            if tool.get("functionCalls"):
                events.append(
                    CallerTurnEvent(
                        CallerTurnEventKind.TOOL_CALL_STARTED,
                        at_ms,
                        first_sequence + len(events),
                        epoch,
                    )
                )
        if "toolCallCancellation" in message:
            cancellation = message["toolCallCancellation"]
            if not isinstance(cancellation, Mapping):
                raise TypeError
            ids = cancellation.get("ids", [])
            if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
                raise TypeError
            if ids:
                events.append(
                    CallerTurnEvent(
                        CallerTurnEventKind.TOOL_CALL_CANCELLED,
                        at_ms,
                        first_sequence + len(events),
                        epoch,
                    )
                )
    except (TypeError, ValueError):
        return ReductionResult("rejected", (), "malformed_message")
    return ReductionResult("decoded" if events else "ignored", tuple(events), None)


def _secondary_transcript(content: Mapping[str, Any]) -> str:
    compatibility = content.get("inputTranscript")
    if compatibility is not None and not isinstance(compatibility, str):
        raise TypeError
    if compatibility:
        return compatibility
    transcription = content.get("inputTranscription")
    if transcription is None:
        return ""
    if isinstance(transcription, str):
        return transcription
    if not isinstance(transcription, Mapping):
        raise TypeError
    text = transcription.get("text", "")
    if not isinstance(text, str):
        raise TypeError
    return text


def _validate_restart_schedule(
    activities: tuple[SessionActivityPlan, ...],
    replay_inputs: tuple[Gate0BReplayInput, ...],
) -> None:
    _connection_segments_from_inputs(replay_inputs)
    markers = [value for value in replay_inputs if value.kind == "fresh_connection_restart"]
    if len(markers) > 1:
        raise RunnerError("session permits at most one fresh restart")
    for activity in activities:
        related = [
            value
            for value in replay_inputs
            if value.activity_ordinal == activity.activity_ordinal
            and value.kind != "fresh_connection_restart"
        ]
        if not related or any(value.epoch != activity.expected_epoch for value in related):
            raise RunnerError("activity replay epoch does not match its expectation")
        kinds = {value.kind for value in related}
        if not {"caller_activity_start", "caller_activity_end"} <= kinds:
            raise RunnerError("activity replay boundaries are incomplete")
    if markers:
        marker = markers[0]
        prior = next(
            (
                activity
                for activity in activities
                if activity.activity_ordinal == marker.activity_ordinal
            ),
            None,
        )
        if prior is None or prior.expected_epoch >= marker.epoch or prior.end_at_ms > marker.at_ms:
            raise RunnerError("fresh restart boundary is inconsistent")
        if any(
            activity.expected_epoch == marker.epoch and activity.start_at_ms <= marker.at_ms
            for activity in activities
        ):
            raise RunnerError("activity crosses the fresh restart boundary")


def _connection_segments(
    plan: SessionPlan,
) -> tuple[tuple[int, int, tuple[Gate0BReplayInput, ...]], ...]:
    return _connection_segments_from_inputs(plan.replay_inputs)


def _connection_segments_from_inputs(
    replay_inputs: tuple[Gate0BReplayInput, ...],
) -> tuple[tuple[int, int, tuple[Gate0BReplayInput, ...]], ...]:
    if replay_inputs[0].kind == "fresh_connection_restart":
        raise RunnerError("session cannot begin with a fresh restart")
    current_epoch = replay_inputs[0].epoch
    base_at_ms = 0
    current: list[Gate0BReplayInput] = []
    segments: list[tuple[int, int, tuple[Gate0BReplayInput, ...]]] = []
    for replay_input in replay_inputs:
        if replay_input.kind == "fresh_connection_restart":
            if not current or replay_input.epoch != current_epoch + 1:
                raise RunnerError("fresh restart epoch is invalid")
            segments.append((current_epoch, base_at_ms, tuple(current)))
            current_epoch = replay_input.epoch
            base_at_ms = replay_input.at_ms
            current = []
            continue
        if replay_input.epoch != current_epoch:
            raise RunnerError("replay epoch changed without a fresh restart")
        current.append(replay_input)
    if not current:
        raise RunnerError("fresh restart requires following replay input")
    segments.append((current_epoch, base_at_ms, tuple(current)))
    return tuple(segments)


def _outbound_message(replay_input: Gate0BReplayInput) -> dict[str, Any] | None:
    if replay_input.kind in {"caller_activity_start", "caller_activity_end"}:
        return None
    if replay_input.kind == "audio":
        return build_gemini_audio_message(replay_input.audio, provider=DEVELOPER_PROVIDER)
    if replay_input.kind in {
        "expect_synchronous_tool",
        "expect_tool_cancellation",
        "expect_interruption",
        "fresh_connection_restart",
    }:
        return None
    raise RunnerError("unsupported replay input kind")


def _plan_uses_tools(plan: SessionPlan) -> bool:
    return any(
        replay_input.kind in {"expect_synchronous_tool", "expect_tool_cancellation"}
        for replay_input in plan.replay_inputs
    )


def _parse_usage_metadata(raw: object) -> UsageCounts:
    required_fields = {
        "promptTokenCount",
        "responseTokenCount",
        "totalTokenCount",
        "promptTokensDetails",
        "responseTokensDetails",
    }
    optional_fields = {"thoughtsTokenCount"}
    if (
        not isinstance(raw, Mapping)
        or not required_fields <= set(raw)
        or set(raw) - required_fields - optional_fields
    ):
        raise RunnerError("usage metadata is inconsistent")
    prompt = _token_details(raw["promptTokensDetails"])
    response = _token_details(raw["responseTokensDetails"])
    prompt_count = _bounded_int(
        raw["promptTokenCount"], label="prompt token count", maximum=100_000_000
    )
    response_count = _bounded_int(
        raw["responseTokenCount"], label="response token count", maximum=100_000_000
    )
    thoughts_count = _bounded_int(
        raw.get("thoughtsTokenCount", 0), label="thoughts token count", maximum=100_000_000
    )
    total_count = _bounded_int(
        raw["totalTokenCount"], label="total token count", maximum=100_000_000
    )
    if (
        prompt_count != sum(prompt.values())
        or response_count != sum(response.values())
        or total_count != prompt_count + response_count + thoughts_count
    ):
        raise RunnerError("usage metadata is inconsistent")
    return UsageCounts(
        input_audio_tokens=prompt.get("AUDIO", 0),
        output_audio_tokens=response.get("AUDIO", 0),
        input_text_tokens=prompt.get("TEXT", 0),
        output_text_tokens=response.get("TEXT", 0) + thoughts_count,
    )


def _token_details(raw: object) -> dict[str, int]:
    if not isinstance(raw, list) or not raw:
        raise RunnerError("usage metadata is inconsistent")
    result: dict[str, int] = {}
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"modality", "tokenCount"}:
            raise RunnerError("usage metadata is inconsistent")
        modality = item["modality"]
        count = item["tokenCount"]
        if modality not in {"AUDIO", "TEXT"} or modality in result:
            raise RunnerError("usage metadata is inconsistent")
        _bounded_int(count, label="token count", maximum=100_000_000)
        result[modality] = count
    return result


def _extract_output_audio(message: Mapping[str, Any]) -> int:
    content = message.get("serverContent")
    if not isinstance(content, Mapping):
        return 0
    model_turn = content.get("modelTurn")
    if model_turn is None:
        return 0
    if not isinstance(model_turn, Mapping):
        raise RunnerError("malformed_output_audio")
    parts = model_turn.get("parts", [])
    if not isinstance(parts, list):
        raise RunnerError("malformed_output_audio")
    total = 0
    for part in parts:
        if not isinstance(part, Mapping):
            raise RunnerError("malformed_output_audio")
        inline = part.get("inlineData")
        if inline is None:
            continue
        if not isinstance(inline, Mapping) or set(inline) != {"mimeType", "data"}:
            raise RunnerError("malformed_output_audio")
        if inline["mimeType"] != "audio/pcm;rate=24000":
            raise RunnerError("malformed_output_audio")
        try:
            decoded = base64.b64decode(inline["data"], validate=True)
        except (TypeError, ValueError) as exc:
            raise RunnerError("malformed_output_audio") from exc
        if len(decoded) > 1024 * 1024:
            raise RunnerError("oversized_output_audio")
        total += len(decoded)
    return total


def _count_output_audio_chunks(message: Mapping[str, Any]) -> int:
    content = message.get("serverContent")
    if not isinstance(content, Mapping):
        return 0
    model_turn = content.get("modelTurn")
    if not isinstance(model_turn, Mapping):
        return 0
    parts = model_turn.get("parts", [])
    if not isinstance(parts, list):
        return 0
    return sum(
        1 for part in parts if isinstance(part, Mapping) and part.get("inlineData") is not None
    )


def _synthetic_tool_response(message: Mapping[str, Any]) -> dict[str, Any] | None:
    tool = message.get("toolCall")
    if tool is None:
        return None
    if not isinstance(tool, Mapping):
        raise RunnerError("malformed_tool_call")
    calls = tool.get("functionCalls", [])
    if not isinstance(calls, list) or len(calls) != 1:
        raise RunnerError("malformed_tool_call")
    call = calls[0]
    if not isinstance(call, Mapping):
        raise RunnerError("malformed_tool_call")
    call_id = call.get("id")
    name = call.get("name")
    if not isinstance(call_id, str) or not SAFE_ID.fullmatch(call_id):
        raise RunnerError("malformed_tool_call")
    if name != "synthetic_lookup":
        raise RunnerError("unsupported_tool_call")
    return {
        "toolResponse": {
            "functionResponses": [
                {
                    "id": call_id,
                    "name": name,
                    "response": {"result": "synthetic_ok"},
                }
            ]
        }
    }


def _increment_wire_counter(
    wires: Mapping[int, _MutableWire],
    activities: tuple[SessionActivityPlan, ...],
    field: str,
    *,
    epoch: int,
    at_ms: int,
    preferred_ordinal: int | None,
) -> None:
    ordinal = preferred_ordinal
    if ordinal is None:
        activity = _latest_activity(activities, at_ms, epoch=epoch)
        ordinal = activity.activity_ordinal if activity is not None else None
    if ordinal is None:
        return
    wire = wires[ordinal]
    setattr(wire, field, getattr(wire, field) + 1)


def _latest_activity(
    activities: tuple[SessionActivityPlan, ...],
    at_ms: int,
    *,
    epoch: int,
) -> SessionActivityPlan | None:
    eligible = [
        value
        for value in activities
        if value.expected_epoch == epoch and value.start_at_ms <= at_ms
    ]
    return max(eligible, key=lambda value: value.start_at_ms) if eligible else None


def _has_response_terminal(message: Mapping[str, Any]) -> bool:
    content = message.get("serverContent")
    if isinstance(content, Mapping) and any(
        content.get(field) is True for field in ("turnComplete", "interrupted")
    ):
        return True
    cancellation = message.get("toolCallCancellation")
    return isinstance(cancellation, Mapping) and bool(cancellation.get("ids"))


def _failed_result(
    plan: SessionPlan,
    error_code: str,
    *,
    wires: Mapping[int, _MutableWire] | None = None,
    provider_request_count: int = 0,
    epoch_count: int = 0,
) -> SessionExecutionResult:
    observations = (
        {ordinal: value.freeze() for ordinal, value in wires.items()}
        if wires is not None
        else {value.activity_ordinal: _MutableWire().freeze() for value in plan.activities}
    )
    return SessionExecutionResult(
        complete=False,
        error_code=error_code,
        audit_events=(),
        wire_observations=observations,
        usage=UsageCounts(),
        output_audio_bytes=0,
        provider_request_count=provider_request_count,
        epoch_count=epoch_count,
    )


def _failed_no_speech(
    error_code: str,
    *,
    provider_request_count: int = 0,
) -> NoSpeechExecutionResult:
    return NoSpeechExecutionResult(
        complete=False,
        error_code=error_code,
        false_activity_count=0,
        model_audio_chunk_count=0,
        abnormal_close_count=0,
        audio_after_teardown_count=0,
        output_audio_bytes=0,
        usage=UsageCounts(),
        provider_request_count=provider_request_count,
        wire_facts=(),
    )


def _safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise RunnerError(f"{label} is invalid")
    return value


def _bounded_int(value: object, *, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise RunnerError(f"{label} is outside its fixed bound")
    return value


def build_dry_run_preregistration() -> dict[str, Any]:
    """Describe the exact Gate 0B contract without creating executable approval."""
    return {
        "schema_id": "gate_0b_preregistration_dry_run_v1",
        "scope": "gate_0b_purpose_recorded_turn_assembly",
        "status": "implementation_only_not_executable",
        "immutable_values": {
            "model": EXACT_MODEL,
            "api_version": "v1beta",
            "endpoint": OFFICIAL_ENDPOINT,
            "transport": "websocket_bidi_tls",
            "project": None,
            "credential_reference": None,
            "approval_key_id": None,
            "approval_public_key_sha256": None,
            "custodian_key_id": None,
            "custodian_public_key_sha256": None,
            "record_root_key_id": None,
            "record_root_public_key_sha256": None,
            "ledger_instance_id": None,
            "ledger_custodian_key_id": None,
            "ledger_custodian_public_key_sha256": None,
            "source_sha": None,
            "environment_identity_sha256": None,
            "manifest_sha256": None,
            "corpus_sha256": None,
            "development_schedule_sha256": None,
            "setup_sha256": None,
            "pricing_sha256": None,
            "runner_sha256": None,
            "evaluator_sha256": None,
            "ledger_location_sha256": None,
            "audit_capsule_location_sha256": None,
            "holdout_capsule_location_sha256": None,
            "evidence_location_sha256": None,
            "consent_attestation_sha256": None,
            "retention_attestation_sha256": None,
            "zdr_or_residual_retention_acceptance_sha256": None,
            "python_version": "3.12.13",
            "uv_version": "0.11.7",
            "candidate_policies_ms": [100, 250, 500, 750],
            "languages": ["ar", "en", "es", "fr", "hi", "ht", "pt", "zh"],
            "attempt_caps": {
                "whole_run_attempts": 3,
                "sessions_per_run": 64,
                "eligible_activities_per_run": 256,
                "holdout_activities": 128,
                "no_speech_windows_per_run": 64,
                "activities_per_session": 10,
                "fresh_connection_restarts_per_session": 1,
            },
            "usage_caps": {
                "provider_requests_per_run": 128,
                "provider_requests_per_campaign": 384,
                "session_timeout_seconds": 120,
                "whole_run_wall_clock_seconds": 3_600,
            },
            "audio_caps": {
                "input_audio_seconds_per_run": 3_600,
                "output_audio_seconds_per_run": 1_800,
            },
            "cost_caps_microusd": {
                "per_session": 250_000,
                "per_run": 10_000_000,
                "per_campaign": 30_000_000,
            },
        },
        "credential_default_present": False,
        "provider_execution_authorized": False,
        "evidence": empty_evidence_flags(),
    }


def build_preregistration(values: Mapping[str, Any]) -> dict[str, Any]:
    """Fill the dry-run contract from one strict, externally reviewed value set."""
    if not isinstance(values, Mapping) or set(values) != {
        "schema_id",
        *PREREGISTRATION_EXTERNAL_FIELDS,
    }:
        raise RunnerError("preregistration values fields are invalid")
    if values.get("schema_id") != "gate_0b_preregistration_values_v1":
        raise RunnerError("preregistration values schema is invalid")
    project = values["project"]
    if (
        not isinstance(project, str)
        or not PROJECT_ID.fullmatch(project)
        or "prod" in project
        or project == "kevin-491315"
    ):
        raise RunnerError("preregistration project is invalid")

    validated: dict[str, str] = {"project": project}
    for field in (
        "credential_reference",
        "approval_key_id",
        "custodian_key_id",
        "record_root_key_id",
        "ledger_instance_id",
        "ledger_custodian_key_id",
    ):
        validated[field] = _safe_id(values[field], label=field.replace("_", " "))
    source_sha = values["source_sha"]
    if not isinstance(source_sha, str) or not SOURCE_SHA.fullmatch(source_sha):
        raise RunnerError("preregistration source SHA is invalid")
    validated["source_sha"] = source_sha
    for field in PREREGISTRATION_EXTERNAL_FIELDS - {
        "project",
        "credential_reference",
        "approval_key_id",
        "custodian_key_id",
        "record_root_key_id",
        "ledger_instance_id",
        "ledger_custodian_key_id",
        "source_sha",
    }:
        value = values[field]
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise RunnerError("preregistration digest is invalid")
        validated[field] = value

    document = build_dry_run_preregistration()
    document["status"] = "preregistered_pending_separate_approval"
    document["immutable_values"].update(validated)
    document["preregistration_sha256"] = sha256(canonical_json_bytes(document)).hexdigest()
    return document


def _load_external_values(path_value: str) -> Mapping[str, Any]:
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RunnerError("preregistration values file is invalid")
    resolved = path.resolve()
    if resolved.is_relative_to(REPO_ROOT) or resolved.stat().st_size > 64 * 1024:
        raise RunnerError("preregistration values file is invalid")
    try:
        decoded = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("preregistration values file is invalid") from exc
    if not isinstance(decoded, Mapping):
        raise RunnerError("preregistration values file is invalid")
    return decoded


def _write_dry_run_output(path_value: str, document: Mapping[str, Any]) -> None:
    path = Path(path_value)
    if not path.is_absolute():
        raise RunnerError("dry-run output path must be absolute")
    if path.exists() and path.is_symlink():
        raise RunnerError("dry-run output path must not be a symlink")
    parent = path.parent.resolve()
    if not parent.is_dir():
        raise RunnerError("dry-run output parent is unavailable")
    resolved = parent / path.name
    if resolved.is_relative_to(REPO_ROOT):
        raise RunnerError("dry-run output must be outside the repository")
    descriptor = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, canonical_json_bytes(document) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_parser() -> argparse.ArgumentParser:
    immutable_fields = "\n".join(
        f"  {field}" for field in sorted(build_dry_run_preregistration()["immutable_values"])
    )
    parser = argparse.ArgumentParser(
        description="Validate Gate 0B runner availability without network execution.",
        epilog="Immutable dry-run fields:\n" + immutable_fields,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Emit the non-executable preregistration contract (default behavior).",
    )
    parser.add_argument(
        "--output",
        help="Absolute owner-only output path outside the repository.",
    )
    parser.add_argument(
        "--values",
        help="Absolute external JSON values file; requires --output.",
    )
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.execute:
        print('{"error_code":"provider_execution_not_authorized","status":"blocked"}')
        return 2
    if args.values is not None and args.output is None:
        print('{"error_code":"dry_run_output_required","status":"blocked"}')
        return 2
    try:
        document = (
            build_preregistration(_load_external_values(args.values))
            if args.values is not None
            else build_dry_run_preregistration()
        )
    except RunnerError:
        print('{"error_code":"preregistration_values_rejected","status":"blocked"}')
        return 2
    if args.output is not None:
        try:
            _write_dry_run_output(args.output, document)
        except (OSError, RunnerError):
            print('{"error_code":"dry_run_output_rejected","status":"blocked"}')
            return 2
    else:
        print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
