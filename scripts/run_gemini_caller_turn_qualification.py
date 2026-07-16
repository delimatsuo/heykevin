#!/usr/bin/env python3
"""Connector-injected Gemini caller-turn qualification session executor."""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
_STARTUP_MARKER_ENV = "KEVIN_GATE0B_TRUSTED_STARTUP"
_TRUSTED_STARTUP_FLAGS = (
    sys.flags.isolated == 1
    and sys.flags.no_site == 1
    and sys.flags.ignore_environment == 1
    and sys.flags.no_user_site == 1
    and sys.flags.safe_path is True
)
if __name__ == "__main__" and (
    _STARTUP_MARKER_ENV not in os.environ or not _TRUSTED_STARTUP_FLAGS
):
    print('{"error_code":"qualification_startup_required","status":"blocked"}')
    raise SystemExit(2)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.caller_turn_alignment import ActivityReference  # noqa: E402
from app.services.caller_turn_measurement import (  # noqa: E402
    MeasurementError,
    WireObservation,
    combined_usage_evidence_sha256,
    derive_audit_capsule_accounting,
    require_reducer_agreement,
    seal_audit_capsule,
    usage_evidence_sha256,
)
from app.services.caller_turn_qualification import (  # noqa: E402
    PricingSchedule,
    empty_evidence_flags,
)
from app.services.caller_turns import CallerTurnEvent, CallerTurnEventKind  # noqa: E402
from app.services.gemini_turn_events import (  # noqa: E402
    GeminiTurnEventAdapter,
    GeminiTurnEventDecodeStatus,
)
from app.services.voice_turn_replay import (  # noqa: E402
    DEVELOPER_PROVIDER,
    Gate0BReplayInput,
    build_gemini_audio_message,
)
from app.services.qualification_environment import (  # noqa: E402
    build_execution_identity_report,
    execution_identity_report_sha256,
)
from app.services.qualification_allocation import (  # noqa: E402
    AllocationActivity,
    AllocationError,
    NoSpeechAllocation,
    validate_gate0b_allocation,
)
from app.services.qualification_identity import (  # noqa: E402
    AttemptAuthorization,
    AttemptClaim,
    CampaignApproval,
    IdentityError,
    canonical_json_bytes,
    capture_trusted_startup_identity,
    verify_attempt_authorization,
    verify_campaign_approval,
)
from app.services.qualification_ledger import (  # noqa: E402
    CustodyLedgerState,
    LedgerCustodyClient,
    LedgerCustodyIdentity,
    validate_custody_ledger_snapshot,
)
from app.services.qualification_privacy import (  # noqa: E402
    OpaqueQualificationAssetLoader,
    PrivacyCustodyAuthorization,
    QualificationAssets,
    verify_privacy_custody,
)
from app.services.qualification_private_paths import (  # noqa: E402
    PrivatePathError,
    read_private_file,
    validate_private_output_path,
    write_private_file,
)


OFFICIAL_ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
EXACT_MODEL = "models/gemini-3.1-flash-live-preview"
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
PROJECT_NUMBER = re.compile(r"[0-9]{6,20}")
VALID_SPLITS = frozenset({"development", "holdout"})
VALID_LANGUAGES = frozenset({"ar", "en", "es", "fr", "hi", "ht", "pt", "zh"})
VALID_POLICIES_MS = frozenset({100, 250, 500, 750})
MAX_OUTPUT_AUDIO_BYTES = 120 * 24_000 * 2
MAX_OUTPUT_AUDIO_PER_RUN_BYTES = 1_800 * 24_000 * 2
MAX_COST_PER_SESSION_MICROUSD = 250_000
MAX_WHOLE_RUN_SECONDS = 3_600
SHA256 = re.compile(r"[0-9a-f]{64}")
SOURCE_SHA = re.compile(r"[0-9a-f]{40,64}")
PINNED_APPROVAL_ROOT_PATH = REPO_ROOT / "config/qualification/gate_0b_approval_root.ed25519.pub"
PINNED_APPROVAL_ROOT_RELATIVE_PATH = Path(
    "config/qualification/gate_0b_approval_root.ed25519.pub"
)
APPROVAL_ROOT_BYTES = 32
PREREGISTRATION_EXTERNAL_FIELDS = frozenset(
    {
        "project",
        "project_number",
        "credential_reference",
        "credential_key_resource_sha256",
        "credential_restrictions_sha256",
        "provider_quota_sha256",
        "credential_activated_at",
        "credential_expires_at",
        "credential_revocation_required_by",
        "credential_revocation_policy_sha256",
        "approval_key_id",
        "approval_public_key_sha256",
        "custodian_key_id",
        "custodian_public_key_sha256",
        "privacy_custodian_key_id",
        "privacy_custodian_public_key_sha256",
        "record_root_key_id",
        "record_root_public_key_sha256",
        "ledger_instance_id",
        "ledger_custodian_key_id",
        "ledger_custodian_public_key_sha256",
        "source_sha",
        "source_fact_bundle_sha256",
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


@dataclass(frozen=True, slots=True)
class CapturedExecutionIdentity:
    report: Mapping[str, Any]
    sha256: str


class ProviderSessionClosed(Exception):
    """Transport-neutral close signal that intentionally discards provider text."""

    def __init__(self, _provider_reason: object = None) -> None:
        super().__init__("provider session closed")


@dataclass(frozen=True, slots=True)
class AuthorizedAssetRelease:
    loader: OpaqueQualificationAssetLoader
    privacy_envelope: Mapping[str, Any]
    privacy_public_key: bytes


@dataclass(frozen=True, slots=True)
class CapsuleHandoffRequest:
    path: Path
    payload: bytes
    campaign_id: str
    attempt_id: str
    split: str
    capsule_sha256: str
    location_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, Path)
            or not self.path.is_absolute()
            or not isinstance(self.payload, bytes)
            or not self.payload.endswith(b"\n")
            or not isinstance(self.campaign_id, str)
            or not SAFE_ID.fullmatch(self.campaign_id)
            or not isinstance(self.attempt_id, str)
            or not SAFE_ID.fullmatch(self.attempt_id)
            or not isinstance(self.split, str)
            or self.split not in VALID_SPLITS
            or not isinstance(self.capsule_sha256, str)
            or not SHA256.fullmatch(self.capsule_sha256)
            or not isinstance(self.location_sha256, str)
            or not SHA256.fullmatch(self.location_sha256)
            or sha256(self.payload[:-1]).hexdigest() != self.capsule_sha256
        ):
            raise RunnerError("capsule handoff request is invalid")


@dataclass(frozen=True, slots=True)
class CapsuleHandoffReceipt:
    campaign_id: str
    attempt_id: str
    split: str
    capsule_sha256: str
    location_sha256: str
    durable: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.campaign_id, str)
            or not SAFE_ID.fullmatch(self.campaign_id)
            or not isinstance(self.attempt_id, str)
            or not SAFE_ID.fullmatch(self.attempt_id)
            or not isinstance(self.split, str)
            or self.split not in VALID_SPLITS
            or not isinstance(self.capsule_sha256, str)
            or not SHA256.fullmatch(self.capsule_sha256)
            or not isinstance(self.location_sha256, str)
            or not SHA256.fullmatch(self.location_sha256)
            or not isinstance(self.durable, bool)
        ):
            raise RunnerError("capsule handoff receipt is invalid")


class CapsuleSink(Protocol):
    def handoff(self, request: CapsuleHandoffRequest) -> CapsuleHandoffReceipt: ...


@dataclass(frozen=True, slots=True)
class PrivateFileCapsuleSink:
    """Durable local sink that must be explicitly injected by the executor caller."""

    repo_root: Path = REPO_ROOT

    def handoff(self, request: CapsuleHandoffRequest) -> CapsuleHandoffReceipt:
        if not isinstance(request, CapsuleHandoffRequest):
            raise RunnerError("capsule handoff request is invalid")
        try:
            written = write_private_file(
                request.path,
                request.payload,
                repo_root=self.repo_root,
            )
        except PrivatePathError as exc:
            raise RunnerError("capsule destination is unavailable") from exc
        if written != request.path:
            raise RunnerError("capsule sink changed the bound destination")
        return CapsuleHandoffReceipt(
            campaign_id=request.campaign_id,
            attempt_id=request.attempt_id,
            split=request.split,
            capsule_sha256=request.capsule_sha256,
            location_sha256=request.location_sha256,
            durable=True,
        )


class RequestReservationError(RunnerError):
    """Raised when the signed request reservation has been consumed."""


class _AbortConnection(Exception):
    pass


class _MeasurementClockError(Exception):
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
    speech_end_at_ms: int
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
        _bounded_int(self.speech_end_at_ms, label="speech end", maximum=120_000)
        _bounded_int(self.end_at_ms, label="activity end", maximum=120_000)
        if not self.start_at_ms < self.speech_end_at_ms <= self.end_at_ms:
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
        _validate_replay_topology(self.activities, self.replay_inputs)
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
    wire_facts: tuple[WireFact, ...] = ()
    event_activity_ordinals: tuple[int, ...] = ()

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
    sequence: int = 0
    epoch: int = 1
    audio_bytes: int = 0

    def __post_init__(self) -> None:
        if self.kind not in {
            "abnormal_close",
            "audio_after_terminal",
            "audio_received",
            "caller_activity_end",
            "caller_speech_end",
            "caller_activity_start",
            "caller_audio_sent",
            "connection_open",
            "false_activity",
            "goaway",
            "interrupted",
            "malformed",
            "response_open",
            "response_terminal",
            "teardown_complete",
            "teardown_failed",
            "tool_call_cancelled",
            "tool_call_open",
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
        _bounded_int(self.sequence, label="wire sequence", maximum=100_000)
        _bounded_int(self.epoch, label="wire epoch", maximum=1_000)
        _bounded_int(self.audio_bytes, label="wire audio bytes", maximum=1024 * 1024)
        if self.kind in {"audio_received", "caller_audio_sent"}:
            if self.audio_bytes <= 0:
                raise RunnerError("audio wire facts require a positive byte count")
        elif self.audio_bytes:
            raise RunnerError("non-audio wire facts cannot carry an audio byte count")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at_ms": self.at_ms,
            "response_ordinal": self.response_ordinal,
            "activity_ordinal": self.activity_ordinal,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "audio_bytes": self.audio_bytes,
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
    wire_facts: list[WireFact] = field(default_factory=list)


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
    measurement_clock_factory: Callable[[object], Callable[[], int]],
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
    if not callable(measurement_clock_factory):
        raise TypeError("measurement clock factory must be callable")
    reducer = secondary_reducer or independent_reduce_message
    measurement_clock_ms = _monotonic_measurement_clock(
        measurement_clock_factory(plan)
    )
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
                measurement_clock_ms=measurement_clock_ms,
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
            wire_facts=tuple(progress.wire_facts),
        )
    except RequestReservationError:
        return _failed_result(
            plan,
            "provider_request_reservation_exhausted",
            wires=progress.wires,
            provider_request_count=progress.provider_request_count,
            epoch_count=progress.epoch_count,
            wire_facts=tuple(progress.wire_facts),
        )
    except Exception:  # A connector failure is reported only as a bounded enum.
        return _failed_result(
            plan,
            "connector_failure",
            wires=progress.wires,
            provider_request_count=progress.provider_request_count,
            epoch_count=progress.epoch_count,
            wire_facts=tuple(progress.wire_facts),
        )


async def _execute_session_flow(
    plan: SessionPlan,
    *,
    config: SessionExecutionConfig,
    connector: InjectedConnector,
    credential: SecretCredential,
    measurement_clock_ms: Callable[[], int],
    sleep_ms: Callable[[int], Awaitable[None]],
    secondary_reducer: Callable[..., ReductionResult],
    request_reserver: Callable[[], None] | None,
    progress: _SessionProgress,
) -> SessionExecutionResult:
    audit_events: list[CallerTurnEvent] = []
    event_activity_ordinals: list[int] = []
    wires = progress.wires
    wire_facts = progress.wire_facts
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
    response_ordinal = 0
    activity_started_at: dict[int, int] = {}
    activity_speech_ended_at: dict[int, int] = {}
    activity_ended_at: dict[int, int] = {}

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
        outstanding_tool_call_id: str | None = None
        cancellation_scenario = any(
            replay_input.kind == "expect_tool_cancellation" for replay_input in replay_inputs
        )
        try:
            try:
                await session.send(
                    build_gate0b_setup_message(config, include_tool=_plan_uses_tools(plan))
                )
                setup_response = await session.receive()
                setup_at_ms = measurement_clock_ms()
                last_receipt_ms = setup_at_ms
                _record_wire_fact(
                    wire_facts,
                    "connection_open",
                    setup_at_ms,
                    epoch=epoch,
                )
            except _MeasurementClockError:
                error_code = "measurement_clock_invalid"
                raise _AbortConnection from None
            except ProviderSessionClosed:
                error_code = "provider_closed"
                raise _AbortConnection from None
            except Exception:
                error_code = "connector_failure"
                raise _AbortConnection from None
            if setup_response != {"setupComplete": {}}:
                error_code = "setup_rejected"
                raise _AbortConnection
            if epoch_count > 1:
                reconnect_owner = next(
                    (
                        replay_input.activity_ordinal
                        for replay_input in replay_inputs
                        if replay_input.kind == "caller_activity_start"
                        and replay_input.activity_ordinal is not None
                    ),
                    None,
                )
                if reconnect_owner is None:
                    raise RunnerError("fresh restart activity identity is missing")
                audit_events.append(
                    CallerTurnEvent(
                        kind=CallerTurnEventKind.RECONNECT_STARTED,
                        at_ms=setup_at_ms,
                        sequence=sequence,
                        epoch=epoch,
                    )
                )
                event_activity_ordinals.append(reconnect_owner)
                sequence += 1

            async def send_replay_inputs() -> None:
                nonlocal sender_error
                previous_at_ms = base_at_ms
                try:
                    for replay_input in replay_inputs:
                        cursor_at_ms = previous_at_ms
                        crossed_speech_ends = sorted(
                            (
                                activity
                                for activity in plan.activities
                                if activity.expected_epoch == epoch
                                and activity.activity_ordinal
                                not in activity_speech_ended_at
                                and cursor_at_ms
                                < activity.speech_end_at_ms
                                <= replay_input.at_ms
                            ),
                            key=lambda activity: activity.speech_end_at_ms,
                        )
                        for activity in crossed_speech_ends:
                            delay = activity.speech_end_at_ms - cursor_at_ms
                            if delay > 0:
                                await sleep_ms(delay)
                            cursor_at_ms = activity.speech_end_at_ms
                            actual_at_ms = measurement_clock_ms()
                            activity_speech_ended_at[
                                activity.activity_ordinal
                            ] = actual_at_ms
                            _record_wire_fact(
                                wire_facts,
                                "caller_speech_end",
                                actual_at_ms,
                                epoch=activity.expected_epoch,
                                activity_ordinal=activity.activity_ordinal,
                            )
                        delay = replay_input.at_ms - cursor_at_ms
                        if delay > 0:
                            await sleep_ms(delay)
                        previous_at_ms = replay_input.at_ms
                        if replay_input.kind in interaction_events:
                            await interaction_events[replay_input.kind].wait()
                            continue
                        outbound = _outbound_message(replay_input)
                        if outbound is not None:
                            await session.send(outbound)
                        if replay_input.kind == "caller_activity_start":
                            _record_wire_fact(
                                wire_facts,
                                replay_input.kind,
                                measurement_clock_ms(),
                                epoch=replay_input.epoch,
                                activity_ordinal=replay_input.activity_ordinal,
                            )
                        elif replay_input.kind == "audio":
                            actual_at_ms = measurement_clock_ms()
                            if replay_input.activity_ordinal is None:
                                raise RunnerError("audio activity identity is missing")
                            activity_started_at.setdefault(
                                replay_input.activity_ordinal,
                                actual_at_ms,
                            )
                            _record_wire_fact(
                                wire_facts,
                                "caller_audio_sent",
                                actual_at_ms,
                                epoch=replay_input.epoch,
                                activity_ordinal=replay_input.activity_ordinal,
                                audio_bytes=len(replay_input.audio),
                            )
                        elif replay_input.kind == "caller_activity_end":
                            if replay_input.activity_ordinal is None:
                                raise RunnerError("activity end identity is missing")
                            actual_at_ms = measurement_clock_ms()
                            activity_ended_at[replay_input.activity_ordinal] = actual_at_ms
                            _record_wire_fact(
                                wire_facts,
                                replay_input.kind,
                                actual_at_ms,
                                epoch=replay_input.epoch,
                                activity_ordinal=replay_input.activity_ordinal,
                            )
                except _MeasurementClockError:
                    sender_error = "measurement_clock_invalid"
                except TimeoutError:
                    raise
                except Exception:
                    sender_error = "connector_failure"
                finally:
                    sender_done.set()

            sender_task = asyncio.create_task(send_replay_inputs())

            while True:
                try:
                    message, quiet_period_elapsed = await _receive_with_completion_drain(
                        session,
                        sender_done=sender_done,
                        completion_pending=(
                            usage_frame_count > 0
                            and active_response_activity is None
                            and len(terminal_activities) == len(plan.activities)
                        ),
                        quiet_timeout_ms=config.response_gap_limit_ms,
                    )
                except ProviderSessionClosed:
                    error_code = "provider_closed"
                    _record_wire_fact(
                        wire_facts,
                        "abnormal_close",
                        last_receipt_ms,
                        epoch=epoch,
                        activity_ordinal=active_response_activity,
                    )
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
                if quiet_period_elapsed:
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
                at_ms = measurement_clock_ms()
                last_receipt_ms = at_ms
                if not isinstance(message, Mapping):
                    error_code = "malformed_message"
                    _record_wire_fact(
                        wire_facts,
                        "malformed",
                        at_ms,
                        epoch=epoch,
                        activity_ordinal=active_response_activity,
                    )
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
                    _record_wire_fact(
                        wire_facts,
                        "malformed",
                        at_ms,
                        epoch=epoch,
                        activity_ordinal=active_response_activity,
                    )
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
                    _record_wire_fact(
                        wire_facts,
                        "malformed",
                        at_ms,
                        epoch=epoch,
                        activity_ordinal=active_response_activity,
                    )
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
                    _record_wire_fact(
                        wire_facts,
                        "goaway",
                        at_ms,
                        epoch=epoch,
                        activity_ordinal=active_response_activity,
                    )
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
                latest_activity = _latest_sent_activity(
                    plan.activities,
                    activity_started_at,
                    at_ms=at_ms,
                    epoch=epoch,
                )
                latest_ordinal = (
                    latest_activity.activity_ordinal if latest_activity is not None else None
                )
                if primary.events and latest_ordinal is None:
                    error_code = "unattributed_response"
                    break
                audit_events.extend(primary.events)
                if latest_ordinal is not None:
                    event_activity_ordinals.extend(
                        [latest_ordinal] * len(primary.events)
                    )
                for event in primary.events:
                    wire_kind = {
                        CallerTurnEventKind.TOOL_CALL_STARTED: "tool_call_open",
                        CallerTurnEventKind.TOOL_CALL_CANCELLED: "tool_call_cancelled",
                        CallerTurnEventKind.INTERRUPTED: "interrupted",
                    }.get(event.kind)
                    if wire_kind is not None:
                        _record_wire_fact(
                            wire_facts,
                            wire_kind,
                            at_ms,
                            epoch=epoch,
                            activity_ordinal=latest_ordinal,
                        )

                if (
                    cancellation_scenario
                    and any(
                        event.kind is CallerTurnEventKind.TOOL_CALL_STARTED
                        for event in primary.events
                    )
                    and active_response_activity is None
                ):
                    if latest_activity is None:
                        error_code = "unattributed_response"
                        break
                    active_response_activity = latest_activity.activity_ordinal
                    response_ordinal += 1
                    _record_wire_fact(
                        wire_facts,
                        "response_open",
                        at_ms,
                        epoch=epoch,
                        response_ordinal=response_ordinal,
                        activity_ordinal=active_response_activity,
                    )

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
                    call = tool_response["toolResponse"]["functionResponses"][0]
                    call_id = call["id"]
                    if outstanding_tool_call_id is not None:
                        error_code = "overlapping_tool_call"
                        break
                    outstanding_tool_call_id = call_id
                    if not cancellation_scenario:
                        await session.send(tool_response)
                        outstanding_tool_call_id = None

                try:
                    cancellation_ids = _tool_cancellation_ids(message)
                except RunnerError as exc:
                    error_code = str(exc)
                    break
                if cancellation_ids:
                    if (
                        outstanding_tool_call_id is None
                        or cancellation_ids != (outstanding_tool_call_id,)
                    ):
                        error_code = "tool_cancellation_mismatch"
                        break
                    outstanding_tool_call_id = None

                newly_observed = _observed_interaction_kinds(primary.events)
                observed_interactions.update(newly_observed)
                for kind in newly_observed:
                    interaction_events[kind].set()

                try:
                    audio_chunk_sizes = _extract_output_audio_chunks(message)
                    audio_bytes = sum(audio_chunk_sizes)
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
                        activity = _latest_sent_activity(
                            plan.activities,
                            activity_started_at,
                            at_ms=at_ms,
                            epoch=epoch,
                        )
                        if activity is None:
                            error_code = "unattributed_response"
                            break
                        if activity.activity_ordinal in terminal_activities:
                            wires[activity.activity_ordinal].audio_after_terminal_count += 1
                            _record_wire_fact(
                                wire_facts,
                                "audio_after_terminal",
                                at_ms,
                                epoch=epoch,
                                activity_ordinal=activity.activity_ordinal,
                            )
                            error_code = "audio_after_terminal"
                            break
                        active_response_activity = activity.activity_ordinal
                        response_ordinal += 1
                        _record_wire_fact(
                            wire_facts,
                            "response_open",
                            at_ms,
                            epoch=epoch,
                            response_ordinal=response_ordinal,
                            activity_ordinal=active_response_activity,
                        )
                    else:
                        newer = _latest_sent_activity(
                            plan.activities,
                            activity_started_at,
                            at_ms=at_ms,
                            epoch=epoch,
                        )
                        if (
                            newer is not None
                            and newer.activity_ordinal != active_response_activity
                        ):
                            wires[newer.activity_ordinal].interruption_tail_ms = (
                                at_ms - activity_started_at[newer.activity_ordinal]
                            )
                    if wires[active_response_activity].first_audio_ms is None:
                        actual_end_at_ms = activity_ended_at.get(
                            active_response_activity
                        )
                        actual_speech_end_at_ms = activity_speech_ended_at.get(
                            active_response_activity
                        )
                        if (
                            actual_end_at_ms is None
                            or actual_speech_end_at_ms is None
                            or at_ms < actual_end_at_ms
                        ):
                            wires[
                                active_response_activity
                            ].premature_current_audio_count += 1
                            error_code = "premature_current_response"
                        else:
                            wires[active_response_activity].first_audio_ms = (
                                at_ms - actual_speech_end_at_ms
                            )
                    if last_response_audio_ms is not None and (
                        at_ms - last_response_audio_ms > config.response_gap_limit_ms
                    ):
                        wires[active_response_activity].response_gap_violation_count += 1
                        error_code = "response_gap_exceeded"
                    last_response_audio_ms = at_ms
                    for chunk_size in audio_chunk_sizes:
                        _record_wire_fact(
                            wire_facts,
                            "audio_received",
                            at_ms,
                            epoch=epoch,
                            response_ordinal=response_ordinal,
                            activity_ordinal=active_response_activity,
                            audio_bytes=chunk_size,
                        )

                if _has_response_terminal(message):
                    if active_response_activity is None:
                        _record_wire_fact(
                            wire_facts,
                            "malformed",
                            at_ms,
                            epoch=epoch,
                            activity_ordinal=latest_ordinal,
                        )
                        _increment_wire_counter(
                            wires,
                            plan.activities,
                            "malformed_count",
                            epoch=epoch,
                            at_ms=at_ms,
                            preferred_ordinal=latest_ordinal,
                        )
                        error_code = "malformed_message"
                    else:
                        _record_wire_fact(
                            wire_facts,
                            "response_terminal",
                            at_ms,
                            epoch=epoch,
                            response_ordinal=response_ordinal,
                            activity_ordinal=active_response_activity,
                        )
                        terminal_activities.add(active_response_activity)
                        active_response_activity = None
                        last_response_audio_ms = None
                if error_code is not None:
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
        except _MeasurementClockError:
            error_code = "measurement_clock_invalid"
        except TimeoutError:
            raise
        except Exception:
            error_code = "connector_failure"
        finally:
            teardown_at_ms = max(
                last_receipt_ms,
                max(
                    (
                        fact.at_ms
                        for fact in wire_facts
                        if fact.epoch == epoch
                        and fact.kind
                        in {
                            "caller_activity_start",
                            "caller_activity_end",
                            "caller_audio_sent",
                        }
                    ),
                    default=last_receipt_ms,
                ),
            )
            try:
                await session.close()
                _record_wire_fact(
                    wire_facts,
                    "teardown_complete",
                    teardown_at_ms,
                    epoch=epoch,
                    activity_ordinal=active_response_activity,
                )
            except Exception:
                _record_wire_fact(
                    wire_facts,
                    "teardown_failed",
                    teardown_at_ms,
                    epoch=epoch,
                    activity_ordinal=active_response_activity,
                )
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
            wire_facts=tuple(wire_facts),
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
        wire_facts=tuple(wire_facts),
        event_activity_ordinals=tuple(event_activity_ordinals),
    )


async def execute_injected_no_speech_window(
    plan: NoSpeechWindowPlan,
    *,
    config: SessionExecutionConfig,
    connector: InjectedConnector,
    credential: SecretCredential,
    measurement_clock_factory: Callable[[object], Callable[[], int]],
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
    if not callable(measurement_clock_factory):
        raise TypeError("measurement clock factory must be callable")
    measurement_clock_ms = _monotonic_measurement_clock(
        measurement_clock_factory(plan)
    )
    try:
        return await asyncio.wait_for(
            _execute_no_speech_flow(
                plan,
                config=config,
                connector=connector,
                credential=credential,
                measurement_clock_ms=measurement_clock_ms,
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
    measurement_clock_ms: Callable[[], int],
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
    last_receipt_ms = 0
    try:
        await session.send(build_gate0b_setup_message(config))
        setup = await session.receive()
        last_receipt_ms = measurement_clock_ms()
        _record_wire_fact(wire_facts, "connection_open", last_receipt_ms, epoch=1)
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
                        _record_wire_fact(
                            wire_facts,
                            "caller_audio_sent",
                            measurement_clock_ms(),
                            epoch=1,
                            audio_bytes=len(replay_input.audio),
                        )
                except _MeasurementClockError:
                    sender_error = "measurement_clock_invalid"
                except TimeoutError:
                    raise
                except Exception:
                    sender_error = "connector_failure"
                finally:
                    sender_done.set()

            sender_task = asyncio.create_task(send_replay_inputs())

            while error_code is None:
                try:
                    message, quiet_period_elapsed = await _receive_with_completion_drain(
                        session,
                        sender_done=sender_done,
                        completion_pending=usage_frame_count > 0 and not response_open,
                        quiet_timeout_ms=config.response_gap_limit_ms,
                    )
                except ProviderSessionClosed:
                    abnormal_close_count += 1
                    error_code = "provider_closed"
                    _record_wire_fact(
                        wire_facts,
                        "abnormal_close",
                        last_receipt_ms,
                        epoch=1,
                    )
                    break
                if quiet_period_elapsed:
                    break
                if message is None:
                    if not sender_done.is_set():
                        await sender_done.wait()
                    break
                at_ms = measurement_clock_ms()
                last_receipt_ms = at_ms
                if not isinstance(message, Mapping):
                    error_code = "malformed_message"
                    _record_wire_fact(wire_facts, "malformed", at_ms, epoch=1)
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
                    _record_wire_fact(wire_facts, "malformed", at_ms, epoch=1)
                    break
                if len(encoded) > config.max_message_bytes:
                    error_code = "message_too_large"
                    _record_wire_fact(wire_facts, "malformed", at_ms, epoch=1)
                    break
                if "goAway" in message:
                    abnormal_close_count += 1
                    error_code = "provider_goaway"
                    _record_wire_fact(wire_facts, "goaway", at_ms, epoch=1)
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
                    _record_wire_fact(wire_facts, "false_activity", at_ms, epoch=1)

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
                        for _ in range(audio_chunks):
                            _record_wire_fact(
                                wire_facts,
                                "audio_after_terminal",
                                at_ms,
                                epoch=1,
                                response_ordinal=1,
                            )
                        break
                    if not response_open:
                        _record_wire_fact(
                            wire_facts,
                            "response_open",
                            at_ms,
                            epoch=1,
                            response_ordinal=1,
                        )
                    response_open = True
                    model_audio_chunk_count += audio_chunks
                    output_audio_bytes += audio_bytes
                    chunk_sizes = _extract_output_audio_chunks(message)
                    wire_facts.extend(
                        WireFact(
                            "audio_received",
                            at_ms,
                            response_ordinal=1,
                            sequence=len(wire_facts) + index,
                            audio_bytes=chunk_size,
                        )
                        for index, chunk_size in enumerate(chunk_sizes)
                    )
                    if output_audio_bytes > MAX_OUTPUT_AUDIO_BYTES:
                        error_code = "runaway_output"
                        break

                if _has_response_terminal(message):
                    if not response_open:
                        error_code = "malformed_message"
                        _record_wire_fact(wire_facts, "malformed", at_ms, epoch=1)
                    else:
                        response_open = False
                        response_terminal = True
                        _record_wire_fact(
                            wire_facts,
                            "response_terminal",
                            at_ms,
                            epoch=1,
                            response_ordinal=1,
                        )

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
        _record_wire_fact(
            wire_facts,
            "abnormal_close",
            last_receipt_ms,
            epoch=1,
        )
    except _MeasurementClockError:
        error_code = "measurement_clock_invalid"
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
        teardown_at_ms = max(
            last_receipt_ms,
            max((fact.at_ms for fact in wire_facts), default=last_receipt_ms),
        )
        try:
            await session.close()
        except Exception:
            _record_wire_fact(
                wire_facts,
                "teardown_failed",
                teardown_at_ms,
                epoch=1,
            )
            if error_code is None:
                error_code = "teardown_failure"
        else:
            _record_wire_fact(
                wire_facts,
                "teardown_complete",
                teardown_at_ms,
                epoch=1,
            )

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
    expected_lease_id_sha256: str | None = None,
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
    lease_id_sha256 = sha256(claim.lease_id.encode("ascii")).hexdigest()
    if (
        expected_lease_id_sha256 is not None
        and lease_id_sha256 != expected_lease_id_sha256
    ):
        raise RunnerError(f"{label} custodian returned a substituted lease")
    return claim


def _require_preregistered_attempt_liability(
    authorization: AttemptAuthorization,
    preregistration: Mapping[str, Any],
) -> None:
    immutable = preregistration["immutable_values"]
    if (
        authorization.provider_request_reservation
        != immutable["usage_caps"]["provider_requests_per_run"]
        or authorization.cost_reservation_microusd
        != immutable["cost_caps_microusd"]["per_run"]
    ):
        raise RunnerError("signed attempt reservation does not cover preregistered liability")


def _require_exact_split_request_count(
    required_requests: int,
    preregistration: Mapping[str, Any],
) -> None:
    per_run = preregistration["immutable_values"]["usage_caps"][
        "provider_requests_per_run"
    ]
    if per_run % 2 or required_requests != per_run // 2:
        raise RunnerError("sealed split request cardinality is not exact")


def _require_exact_campaign_ceiling(
    campaign: CampaignApproval,
    preregistration: Mapping[str, Any],
) -> None:
    immutable = preregistration["immutable_values"]
    if (
        campaign.max_attempts != immutable["attempt_caps"]["whole_run_attempts"]
        or campaign.max_provider_requests
        != immutable["usage_caps"]["provider_requests_per_campaign"]
        or campaign.max_cost_microusd
        != immutable["cost_caps_microusd"]["per_campaign"]
    ):
        raise RunnerError("signed campaign ceiling does not match preregistration")


def _require_ledger_campaign_ceiling(
    state: CustodyLedgerState,
    campaign: CampaignApproval,
) -> None:
    if (
        state.campaign_max_attempts != campaign.max_attempts
        or state.campaign_max_provider_requests != campaign.max_provider_requests
        or state.campaign_max_cost_microusd != campaign.max_cost_microusd
    ):
        raise RunnerError("signed ledger campaign ceiling does not match approval")


def _require_single_signed_append(
    before: CustodyLedgerState,
    after: CustodyLedgerState,
    *,
    expected_event: str,
) -> None:
    if (
        not before.record_sha256s
        or len(after.record_sha256s) != len(before.record_sha256s) + 1
        or after.record_sha256s[:-1] != before.record_sha256s
        or after.record_events[:-1] != before.record_events
        or after.record_events[-1] != expected_event
        or before.final_ledger_head_sha256 != before.record_sha256s[-1]
        or after.final_ledger_head_sha256 != after.record_sha256s[-1]
    ):
        raise RunnerError("signed ledger mutation does not extend the accepted chain")


def _require_preclaim_state(
    state: CustodyLedgerState,
    *,
    campaign: CampaignApproval,
    authorization: AttemptAuthorization,
) -> None:
    invalid = (
        state.campaign_approval_sha256 != campaign.signed_payload_sha256
        or state.active_attempt_id is not None
        or state.completed_attempt_id is not None
        or len(state.attempt_ids) != authorization.attempt_index - 1
    )
    if authorization.attempt_index == 1:
        invalid = invalid or state.phase != "preregistered" or state.attempt_ids != ()
    else:
        invalid = (
            invalid
            or state.phase != "development_collection"
            or not state.attempt_ids
            or state.attempt_ids[-1] != authorization.prior_attempt_id
        )
    if invalid:
        raise RunnerError("signed ledger is not ready for this attempt claim")


def _require_postclaim_state(
    state: CustodyLedgerState,
    *,
    campaign: CampaignApproval,
    authorization: AttemptAuthorization,
    claim: AttemptClaim,
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
        or state.lease_id_sha256
        != sha256(claim.lease_id.encode("ascii")).hexdigest()
        or state.provider_requests_reserved
        != authorization.provider_request_reservation
        or state.cost_reserved_microusd != authorization.cost_reservation_microusd
        or state.development_capsule_sha256 is not None
        or state.holdout_execution_claimed
        or state.holdout_execution_claimed_at is not None
    ):
        raise RunnerError("signed ledger did not durably consume the attempt claim")


async def execute_authorized_attempt(
    asset_release: AuthorizedAssetRelease,
    *,
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
    measurement_clock_factory: Callable[[object], Callable[[], int]],
    sleep_ms: Callable[[int], Awaitable[None]],
    pricing: PricingSchedule,
    custodian_public_key: bytes,
    custodian_key_id: str,
    capsule_path: Path,
    capsule_sink: CapsuleSink,
) -> AttemptExecutionResult:
    """Execute one consumed attempt using only injected secret, transport, and sinks."""
    environment_identity = _capture_current_execution_identity(
        expected_source_sha=config.source_sha
    )
    try:
        expected_environment_sha256 = preregistration["immutable_values"][
            "environment_identity_sha256"
        ]
    except (KeyError, TypeError) as exc:
        raise RunnerError("execution environment identity is not preregistered") from exc
    if environment_identity.sha256 != expected_environment_sha256:
        raise RunnerError("execution environment identity mismatch")
    _require_capsule_sink(capsule_sink)
    _validate_asset_release(asset_release)
    _validate_attempt_configuration(
        config=config,
        session_config=session_config,
        pricing=pricing,
    )
    approval_public_key = _load_pinned_approval_public_key(environment_identity)
    _verify_execution_preregistration(
        preregistration,
        config=config,
        session_config=session_config,
        approval_public_key=approval_public_key,
        ledger=ledger,
        privacy_public_key=asset_release.privacy_public_key,
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
    _require_exact_campaign_ceiling(campaign, preregistration)
    authorization = verify_attempt_authorization(
        attempt_envelope,
        public_key=approval_public_key,
        expected_key_id=config.approval_key_id,
        campaign=campaign,
        now=now,
    )
    privacy_authorization = verify_privacy_custody(
        asset_release.privacy_envelope,
        public_key=asset_release.privacy_public_key,
        expected_key_id=preregistration["immutable_values"]["privacy_custodian_key_id"],
        expected_campaign_id=campaign.campaign_id,
        expected_authorization_id=campaign.authorization_id,
        expected_attempt_id=authorization.attempt_id,
        expected_split="development",
        expected_preregistration_sha256=config.preregistration_sha256,
        expected_source_sha=config.source_sha,
        expected_schedule_sha256=preregistration["immutable_values"][
            "development_schedule_sha256"
        ],
        expected_corpus_sha256=preregistration["immutable_values"]["corpus_sha256"],
        expected_project=session_config.project,
        expected_model=session_config.model,
        expected_consent_registry_sha256=preregistration["immutable_values"][
            "consent_attestation_sha256"
        ],
        expected_retention_policy_sha256=preregistration["immutable_values"][
            "retention_attestation_sha256"
        ],
        expected_residual_retention_acceptance_sha256=preregistration[
            "immutable_values"
        ]["zdr_or_residual_retention_acceptance_sha256"],
        now=now,
    )
    plans, no_speech_plans = _materialize_qualification_assets(
        asset_release,
        privacy_authorization,
    )
    _validate_attempt_inputs(
        plans,
        no_speech_plans=no_speech_plans,
        config=config,
        session_config=session_config,
        pricing=pricing,
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
    _require_exact_split_request_count(required_requests, preregistration)
    if required_requests > authorization.provider_request_reservation:
        raise RunnerError("signed request reservation is insufficient")
    _require_preregistered_attempt_liability(authorization, preregistration)
    ledger_identity = _validate_ledger_custody_binding(
        campaign=campaign,
        ledger=ledger,
        public_key=ledger_custodian_public_key,
        label="campaign",
    )
    preclaim_state = _replay_bound_ledger_snapshot(
        ledger=ledger,
        public_key=ledger_custodian_public_key,
        identity=ledger_identity,
        campaign=campaign,
        authorization=authorization,
    )
    _require_ledger_campaign_ceiling(preclaim_state, campaign)
    _require_preclaim_state(
        preclaim_state,
        campaign=campaign,
        authorization=authorization,
    )
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
    _require_ledger_campaign_ceiling(claimed_state, campaign)
    _require_postclaim_state(
        claimed_state,
        campaign=campaign,
        authorization=authorization,
        claim=claim,
        expected_claimed_at=now,
    )
    _require_single_signed_append(
        preclaim_state,
        claimed_state,
        expected_event="claim",
    )

    budget = _RequestBudget(claim.provider_requests_reserved)
    execution_started_at = datetime.now(timezone.utc)
    cost_microusd = 0
    error_code: str | None = None
    capsule_handed_off = False
    capsule_sha256: str | None = None
    usage = UsageCounts()
    session_results: list[tuple[SessionPlan, SessionExecutionResult]] = []
    no_speech_results: list[tuple[NoSpeechWindowPlan, NoSpeechExecutionResult]] = []

    try:
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
                        measurement_clock_factory=measurement_clock_factory,
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
        if error_code is None and budget.consumed != required_requests:
            error_code = "provider_request_count_mismatch"

        for _, result in session_results:
            usage = _add_usage(usage, result.usage)
        for _, result in no_speech_results:
            usage = _add_usage(usage, result.usage)
        cost_microusd = _cost_microusd(pricing, usage)
        if cost_microusd > claim.cost_reserved_microusd:
            error_code = "cost_reservation_exhausted"

        if error_code is None:
            try:
                environment_identity_after = _capture_current_execution_identity(
                    expected_source_sha=config.source_sha
                )
                if (
                    environment_identity is None
                    or environment_identity_after != environment_identity
                ):
                    raise RunnerError("execution environment identity drifted")
            except Exception:
                error_code = "source_identity_failed"

        if error_code is None:
            try:
                capsule = _build_audit_capsule(
                    campaign_id=campaign.campaign_id,
                    policy_ms=config.policy_ms,
                    source_fact_bundle_sha256=preregistration["immutable_values"][
                        "source_fact_bundle_sha256"
                    ],
                    execution_started_at=execution_started_at,
                    execution_completed_at=datetime.now(timezone.utc),
                    provider_revision=None,
                    runtime_identity_before_sha256=environment_identity.sha256,
                    runtime_identity_after_sha256=environment_identity_after.sha256,
                    runtime_identity_before=environment_identity.report,
                    runtime_identity_after=environment_identity_after.report,
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
                    capsule_sha256 = _handoff_sealed_capsule(
                        capsule_path,
                        sealed,
                        sink=capsule_sink,
                        campaign_id=campaign.campaign_id,
                        attempt_id=authorization.attempt_id,
                        split="development",
                    )
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
            _require_ledger_campaign_ceiling(final_state, campaign)
            if (
                final_state.phase != "development_collection"
                or final_state.active_attempt_id != authorization.attempt_id
                or final_state.development_capsule_sha256 != capsule_sha256
                or final_state.development_usage_evidence_sha256
                != usage_evidence_digest
                or final_state.development_provider_requests != budget.consumed
                or final_state.development_cost_microusd != cost_microusd
            ):
                raise RunnerError("signed development checkpoint is not durable")
            _require_single_signed_append(
                claimed_state,
                final_state,
                expected_event="development_checkpoint",
            )
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
            _require_ledger_campaign_ceiling(final_state, campaign)
            if (
                final_state.phase != "aborted"
                or final_state.active_attempt_id is not None
                or final_state.attempt_authorization_sha256
                != authorization.signed_payload_sha256
                or final_state.final_usage_evidence_sha256 != usage_evidence_digest
                or final_state.actual_provider_requests != budget.consumed
                or final_state.actual_cost_microusd != cost_microusd
            ):
                raise RunnerError("signed terminal outcome is not durable")
            _require_single_signed_append(
                claimed_state,
                final_state,
                expected_event="terminal_outcome",
            )

    return AttemptExecutionResult(
        complete=complete,
        error_code=error_code,
        provider_request_count=budget.consumed,
        cost_microusd=cost_microusd,
        capsule_handed_off=capsule_handed_off,
    )


async def execute_authorized_holdout(
    asset_release: AuthorizedAssetRelease,
    *,
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
    measurement_clock_factory: Callable[[object], Callable[[], int]],
    sleep_ms: Callable[[int], Awaitable[None]],
    pricing: PricingSchedule,
    custodian_public_key: bytes,
    custodian_key_id: str,
    capsule_path: Path,
    capsule_sink: CapsuleSink,
) -> AttemptExecutionResult:
    """Resume one active post-lock attempt and execute its released holdout once."""
    environment_identity = _capture_current_execution_identity(
        expected_source_sha=config.source_sha
    )
    try:
        expected_environment_sha256 = preregistration["immutable_values"][
            "environment_identity_sha256"
        ]
    except (KeyError, TypeError) as exc:
        raise RunnerError("execution environment identity is not preregistered") from exc
    if environment_identity.sha256 != expected_environment_sha256:
        raise RunnerError("execution environment identity mismatch")
    _require_capsule_sink(capsule_sink)
    _validate_asset_release(asset_release)
    _validate_attempt_configuration(
        config=config,
        session_config=session_config,
        pricing=pricing,
    )
    approval_public_key = _load_pinned_approval_public_key(environment_identity)
    _verify_execution_preregistration(
        preregistration,
        config=config,
        session_config=session_config,
        approval_public_key=approval_public_key,
        ledger=ledger,
        privacy_public_key=asset_release.privacy_public_key,
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
    _require_exact_campaign_ceiling(campaign, preregistration)
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
    _require_ledger_campaign_ceiling(state, campaign)
    if not isinstance(state, CustodyLedgerState) or (
        state.phase != "holdout_collection"
        or state.active_attempt_id != authorization.attempt_id
        or state.completed_attempt_id is not None
        or state.campaign_approval_sha256 != campaign.signed_payload_sha256
        or state.attempt_authorization_sha256 != authorization.signed_payload_sha256
        or state.selected_policy_ms != config.policy_ms
        or not isinstance(state.holdout_manifest_sha256, str)
        or not SHA256.fullmatch(state.holdout_manifest_sha256)
        or state.holdout_execution_claimed
        or state.holdout_execution_claimed_at is not None
        or state.development_usage_evidence_sha256 is None
        or state.lease_id_sha256 is None
    ):
        raise RunnerError("holdout release does not match the active signed attempt")
    privacy_authorization = verify_privacy_custody(
        asset_release.privacy_envelope,
        public_key=asset_release.privacy_public_key,
        expected_key_id=preregistration["immutable_values"]["privacy_custodian_key_id"],
        expected_campaign_id=campaign.campaign_id,
        expected_authorization_id=campaign.authorization_id,
        expected_attempt_id=authorization.attempt_id,
        expected_split="holdout",
        expected_preregistration_sha256=config.preregistration_sha256,
        expected_source_sha=config.source_sha,
        expected_schedule_sha256=state.holdout_manifest_sha256,
        expected_corpus_sha256=preregistration["immutable_values"]["corpus_sha256"],
        expected_project=session_config.project,
        expected_model=session_config.model,
        expected_consent_registry_sha256=preregistration["immutable_values"][
            "consent_attestation_sha256"
        ],
        expected_retention_policy_sha256=preregistration["immutable_values"][
            "retention_attestation_sha256"
        ],
        expected_residual_retention_acceptance_sha256=preregistration[
            "immutable_values"
        ]["zdr_or_residual_retention_acceptance_sha256"],
        now=now,
    )
    plans, no_speech_plans = _materialize_qualification_assets(
        asset_release,
        privacy_authorization,
    )
    _validate_attempt_inputs(
        plans,
        no_speech_plans=no_speech_plans,
        config=config,
        session_config=session_config,
        pricing=pricing,
    )
    _validate_exact_holdout_schedule(plans, no_speech_plans=no_speech_plans)
    holdout_manifest_sha256 = compute_holdout_schedule_sha256(
        plans,
        no_speech_plans=no_speech_plans,
    )
    if state.holdout_manifest_sha256 != holdout_manifest_sha256:
        raise RunnerError("holdout release does not match the active signed attempt")
    required_requests = sum(len(_connection_segments(plan)) for plan in plans) + len(
        no_speech_plans
    )
    _require_exact_split_request_count(required_requests, preregistration)
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
    _require_preregistered_attempt_liability(authorization, preregistration)
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
        expected_lease_id_sha256=state.lease_id_sha256,
    )
    resumed_state = _replay_bound_ledger_snapshot(
        ledger=ledger,
        public_key=ledger_custodian_public_key,
        identity=ledger_identity,
        campaign=campaign,
        authorization=authorization,
    )
    _require_ledger_campaign_ceiling(resumed_state, campaign)
    if (
        resumed_state.phase != "holdout_collection"
        or resumed_state.active_attempt_id != authorization.attempt_id
        or resumed_state.campaign_approval_sha256 != campaign.signed_payload_sha256
        or resumed_state.attempt_authorization_sha256
        != authorization.signed_payload_sha256
        or resumed_state.selected_policy_ms != config.policy_ms
        or resumed_state.holdout_manifest_sha256 != holdout_manifest_sha256
        or not resumed_state.holdout_execution_claimed
        or resumed_state.holdout_execution_claimed_at != now
        or resumed_state.provider_requests_reserved
        != authorization.provider_request_reservation
        or resumed_state.cost_reserved_microusd
        != authorization.cost_reservation_microusd
        or resumed_state.development_usage_evidence_sha256
        != state.development_usage_evidence_sha256
        or resumed_state.lease_id_sha256 != state.lease_id_sha256
    ):
        raise RunnerError("signed ledger did not durably consume the holdout claim")
    _require_single_signed_append(
        state,
        resumed_state,
        expected_event="holdout_execution_claim",
    )

    budget = _RequestBudget(remaining_requests)
    execution_started_at = datetime.now(timezone.utc)
    holdout_cost_microusd = 0
    error_code: str | None = None
    capsule_handed_off = False
    capsule_sha256: str | None = None
    usage = UsageCounts()
    session_results: list[tuple[SessionPlan, SessionExecutionResult]] = []
    no_speech_results: list[tuple[NoSpeechWindowPlan, NoSpeechExecutionResult]] = []
    try:
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
                        measurement_clock_factory=measurement_clock_factory,
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
        if error_code is None and budget.consumed != required_requests:
            error_code = "provider_request_count_mismatch"

        for _, result in session_results:
            usage = _add_usage(usage, result.usage)
        for _, result in no_speech_results:
            usage = _add_usage(usage, result.usage)
        holdout_cost_microusd = _cost_microusd(pricing, usage)
        if holdout_cost_microusd > remaining_cost_microusd:
            error_code = "cost_reservation_exhausted"

        if error_code is None:
            try:
                environment_identity_after = _capture_current_execution_identity(
                    expected_source_sha=config.source_sha
                )
                if (
                    environment_identity is None
                    or environment_identity_after != environment_identity
                ):
                    raise RunnerError("execution environment identity drifted")
            except Exception:
                error_code = "source_identity_failed"

        if error_code is None:
            try:
                capsule = _build_audit_capsule(
                    campaign_id=campaign.campaign_id,
                    policy_ms=config.policy_ms,
                    source_fact_bundle_sha256=preregistration["immutable_values"][
                        "source_fact_bundle_sha256"
                    ],
                    execution_started_at=execution_started_at,
                    execution_completed_at=datetime.now(timezone.utc),
                    provider_revision=None,
                    runtime_identity_before_sha256=environment_identity.sha256,
                    runtime_identity_after_sha256=environment_identity_after.sha256,
                    runtime_identity_before=environment_identity.report,
                    runtime_identity_after=environment_identity_after.report,
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
                    capsule_sha256 = _handoff_sealed_capsule(
                        capsule_path,
                        sealed,
                        sink=capsule_sink,
                        campaign_id=campaign.campaign_id,
                        attempt_id=authorization.attempt_id,
                        split="holdout",
                    )
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
        _require_ledger_campaign_ceiling(final_state, campaign)
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
            or final_state.holdout_execution_claimed_at != now
        ):
            raise RunnerError("signed holdout terminal outcome is not durable")
        _require_single_signed_append(
            resumed_state,
            final_state,
            expected_event="terminal_outcome",
        )

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
    measurement_clock_factory: Callable[[object], Callable[[], int]],
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
            measurement_clock_factory=measurement_clock_factory,
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
            measurement_clock_factory=measurement_clock_factory,
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


def _validate_attempt_configuration(
    *,
    config: AuthorizedAttemptConfig,
    session_config: SessionExecutionConfig,
    pricing: PricingSchedule,
) -> None:
    if not isinstance(config, AuthorizedAttemptConfig):
        raise TypeError("config must be an AuthorizedAttemptConfig")
    if not isinstance(session_config, SessionExecutionConfig):
        raise TypeError("session_config must be a SessionExecutionConfig")
    if not isinstance(pricing, PricingSchedule):
        raise TypeError("pricing must be a PricingSchedule")
    if pricing.model != session_config.model.removeprefix("models/"):
        raise RunnerError("pricing and model identity mismatch")


def _validate_asset_release(release: AuthorizedAssetRelease) -> None:
    if (
        not isinstance(release, AuthorizedAssetRelease)
        or not isinstance(release.privacy_envelope, Mapping)
        or not isinstance(release.privacy_public_key, bytes)
        or len(release.privacy_public_key) != 32
        or not callable(getattr(release.loader, "load", None))
    ):
        raise RunnerError("qualification asset release is invalid")


def _materialize_qualification_assets(
    release: AuthorizedAssetRelease,
    authorization: PrivacyCustodyAuthorization,
) -> tuple[tuple[SessionPlan, ...], tuple[NoSpeechWindowPlan, ...]]:
    try:
        assets = release.loader.load(authorization)
    except Exception:
        raise RunnerError("qualification assets could not be released") from None
    if not isinstance(assets, QualificationAssets):
        raise RunnerError("qualification asset release is invalid")
    if not isinstance(assets.plans, tuple) or not isinstance(
        assets.no_speech_plans, tuple
    ):
        raise RunnerError("qualification asset release is invalid")
    return assets.plans, assets.no_speech_plans


def _validate_exact_development_schedule(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
) -> None:
    if {plan.session_ordinal for plan in plans} != set(range(24)):
        raise RunnerError("development schedule cardinality is invalid")
    _validate_shared_allocation(
        plans,
        no_speech_plans=no_speech_plans,
        split="development",
    )


def _validate_exact_holdout_schedule(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
) -> None:
    if {plan.session_ordinal for plan in plans} != set(range(24, 48)):
        raise RunnerError("holdout schedule cardinality is invalid")
    _validate_shared_allocation(plans, no_speech_plans=no_speech_plans, split="holdout")


def _validate_shared_allocation(
    plans: tuple[SessionPlan, ...],
    *,
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
    split: str,
) -> None:
    try:
        validate_gate0b_allocation(
            (
                AllocationActivity(
                    ordinal=activity.activity_ordinal,
                    split=activity.split,
                    language=activity.language,
                    condition=activity.condition,
                    scenario_tags=activity.scenario_tags,
                    critical_span_kinds=tuple(
                        span.kind.value for span in activity.reference.critical_spans
                    ),
                )
                for plan in plans
                for activity in plan.activities
            ),
            (
                NoSpeechAllocation(
                    ordinal=plan.window_ordinal,
                    split=plan.split,
                    condition=plan.condition,
                )
                for plan in no_speech_plans
            ),
            split=split,
        )
    except AllocationError as exc:
        raise RunnerError(f"{split} schedule allocation is invalid") from exc


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
                        "speech_end_at_ms": activity.speech_end_at_ms,
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
    privacy_public_key: bytes,
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
            "schema_id": "gate_0b_preregistration_values_v2",
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
    if (
        not isinstance(approval_public_key, bytes)
        or not isinstance(privacy_public_key, bytes)
        or not isinstance(custodian_public_key, bytes)
    ):
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
        "privacy_custodian_public_key_sha256": sha256(
            privacy_public_key
        ).hexdigest(),
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



def _load_pinned_approval_public_key(
    environment_identity: CapturedExecutionIdentity,
) -> bytes:
    expected_worktree_sha256, expected_git_blob_id = (
        _approval_root_dependency_identity(environment_identity)
    )
    try:
        relative = PINNED_APPROVAL_ROOT_PATH.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise RunnerError("pinned approval trust root is unavailable") from exc
    if relative != PINNED_APPROVAL_ROOT_RELATIVE_PATH:
        raise RunnerError("pinned approval trust root is unavailable")

    descriptors: list[int] = []
    opened_entries: list[tuple[int, str, os.stat_result]] = []
    try:
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = os.open(REPO_ROOT, directory_flags)
        descriptors.append(root_fd)
        root_metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise RunnerError("pinned approval trust root is unavailable")

        parent_fd = root_fd
        for component in relative.parts[:-1]:
            child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            descriptors.append(child_fd)
            child_metadata = os.fstat(child_fd)
            if not stat.S_ISDIR(child_metadata.st_mode):
                raise RunnerError("pinned approval trust root is unavailable")
            opened_entries.append((parent_fd, component, child_metadata))
            parent_fd = child_fd

        file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        file_fd = os.open(relative.name, file_flags, dir_fd=parent_fd)
        descriptors.append(file_fd)
        file_metadata_before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(file_metadata_before.st_mode)
            or file_metadata_before.st_nlink != 1
        ):
            raise RunnerError("pinned approval trust root is unavailable")
        opened_entries.append((parent_fd, relative.name, file_metadata_before))
        data = _read_bounded_descriptor(file_fd, maximum=APPROVAL_ROOT_BYTES)
        file_metadata_after = os.fstat(file_fd)
        if _stable_metadata(file_metadata_before) != _stable_metadata(
            file_metadata_after
        ):
            raise RunnerError("pinned approval trust root is unavailable")

        current_root = os.stat(REPO_ROOT, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current_root.st_mode)
            or _entry_identity(current_root) != _entry_identity(root_metadata)
        ):
            raise RunnerError("pinned approval trust root is unavailable")
        for entry_parent_fd, component, opened_metadata in opened_entries:
            current = os.stat(
                component,
                dir_fd=entry_parent_fd,
                follow_symlinks=False,
            )
            if _entry_identity(current) != _entry_identity(opened_metadata):
                raise RunnerError("pinned approval trust root is unavailable")
    except OSError as exc:
        raise RunnerError("pinned approval trust root is unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    if len(data) != APPROVAL_ROOT_BYTES:
        raise RunnerError("pinned approval trust root is unprovisioned")
    if (
        sha256(data).hexdigest() != expected_worktree_sha256
        or _git_blob_id(data, expected=expected_git_blob_id) != expected_git_blob_id
    ):
        raise RunnerError("pinned approval trust root source identity mismatch")
    return data


def _approval_root_dependency_identity(
    environment_identity: CapturedExecutionIdentity,
) -> tuple[str, str]:
    if not isinstance(environment_identity, CapturedExecutionIdentity):
        raise TypeError("environment_identity must be a CapturedExecutionIdentity")
    try:
        source = environment_identity.report["source"]
        dependencies = source["dependencies"]
        path_sha256 = sha256(
            PINNED_APPROVAL_ROOT_RELATIVE_PATH.as_posix().encode("utf-8")
        ).hexdigest()
        dependency = dependencies[path_sha256]
        worktree_sha256 = dependency["worktree_sha256"]
        git_blob_id = dependency["git_blob_id"]
    except (KeyError, TypeError) as exc:
        raise RunnerError("pinned approval trust root source identity mismatch") from exc
    if (
        not isinstance(worktree_sha256, str)
        or not SHA256.fullmatch(worktree_sha256)
        or not isinstance(git_blob_id, str)
        or not SOURCE_SHA.fullmatch(git_blob_id)
    ):
        raise RunnerError("pinned approval trust root source identity mismatch")
    return worktree_sha256, git_blob_id


def _read_bounded_descriptor(file_fd: int, *, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(file_fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _stable_metadata(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _entry_identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)


def _git_blob_id(data: bytes, *, expected: str) -> str:
    payload = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
    if len(expected) == 40:
        return sha1(payload, usedforsecurity=False).hexdigest()
    if len(expected) == 64:
        return sha256(payload).hexdigest()
    raise RunnerError("pinned approval trust root source identity mismatch")


def _capture_current_execution_identity(
    *, expected_source_sha: str
) -> CapturedExecutionIdentity:
    startup = capture_trusted_startup_identity(
        REPO_ROOT,
        expected_target="run-qualification",
    )
    report = build_execution_identity_report(
        REPO_ROOT,
        expected_source_sha=expected_source_sha,
        trusted_startup=startup.policy_report_dict(),
    )
    return CapturedExecutionIdentity(
        report=report,
        sha256=execution_identity_report_sha256(report),
    )


def artifact_location_sha256(path: Path) -> str:
    """Hash one validated, absent, outside-repository artifact destination."""
    canonical = _canonical_artifact_path(path)
    return sha256(str(canonical).encode("utf-8")).hexdigest()


def _canonical_artifact_path(path: Path) -> Path:
    try:
        return validate_private_output_path(path, repo_root=REPO_ROOT)
    except PrivatePathError as exc:
        raise RunnerError("artifact destination is invalid") from exc


def _require_capsule_sink(sink: object) -> None:
    if not callable(getattr(sink, "handoff", None)):
        raise RunnerError("capsule sink is not configured")


def _handoff_sealed_capsule(
    path: Path,
    envelope: Mapping[str, Any],
    *,
    sink: CapsuleSink,
    campaign_id: str,
    attempt_id: str,
    split: str,
) -> str:
    payload = canonical_json_bytes(envelope) + b"\n"
    canonical = _canonical_artifact_path(path)
    capsule_sha256 = sha256(payload[:-1]).hexdigest()
    request = CapsuleHandoffRequest(
        path=canonical,
        payload=payload,
        campaign_id=campaign_id,
        attempt_id=attempt_id,
        split=split,
        capsule_sha256=capsule_sha256,
        location_sha256=sha256(str(canonical).encode("utf-8")).hexdigest(),
    )
    try:
        receipt = sink.handoff(request)
        persisted = read_private_file(
            canonical,
            repo_root=REPO_ROOT,
            maximum_bytes=len(payload),
        )
    except PrivatePathError as exc:
        raise RunnerError("capsule content was not durably persisted") from exc
    expected_receipt = CapsuleHandoffReceipt(
        campaign_id=campaign_id,
        attempt_id=attempt_id,
        split=split,
        capsule_sha256=capsule_sha256,
        location_sha256=request.location_sha256,
        durable=True,
    )
    if not isinstance(receipt, CapsuleHandoffReceipt) or receipt != expected_receipt:
        raise RunnerError("capsule handoff receipt does not match its request")
    if persisted != payload:
        raise RunnerError("capsule content was not durably persisted")
    return capsule_sha256


def _build_audit_capsule(
    *,
    campaign_id: str,
    policy_ms: int,
    source_fact_bundle_sha256: str,
    execution_started_at: datetime,
    execution_completed_at: datetime,
    provider_revision: str | None,
    runtime_identity_before_sha256: str,
    runtime_identity_after_sha256: str,
    runtime_identity_before: Mapping[str, Any],
    runtime_identity_after: Mapping[str, Any],
    session_results: Sequence[tuple[SessionPlan, SessionExecutionResult]],
    no_speech_results: Sequence[tuple[NoSpeechWindowPlan, NoSpeechExecutionResult]],
) -> dict[str, Any]:
    activities: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
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
        if len(result.event_activity_ordinals) != len(result.audit_events):
            raise RunnerError("successful session lacks causal event ownership")
        latest_event_ms = max((event.at_ms for event in result.audit_events), default=0)
        sessions.append(
            {
                "session_ordinal": plan.session_ordinal,
                "split": plan.split,
                "events": events,
                "event_activity_ordinals": list(result.event_activity_ordinals),
                "wire_facts": _ordered_wire_fact_dicts(result.wire_facts),
            }
        )
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
            speech_end_facts = [
                fact.at_ms
                for fact in result.wire_facts
                if fact.kind == "caller_speech_end"
                and fact.activity_ordinal == activity.activity_ordinal
            ]
            activity_end_facts = [
                fact.at_ms
                for fact in result.wire_facts
                if fact.kind == "caller_activity_end"
                and fact.activity_ordinal == activity.activity_ordinal
            ]
            if len(speech_end_facts) != 1 or len(activity_end_facts) != 1:
                raise RunnerError("successful activity lacks actual outbound boundaries")
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
                    "expected_lifecycle_status": activity.expected_lifecycle_status,
                    "expected_epoch": activity.expected_epoch,
                    "speech_end_at_ms": speech_end_facts[0],
                    "advance_to_ms": max(
                        activity_end_facts[0] + max(VALID_POLICIES_MS),
                        latest_event_ms,
                    ),
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
        "schema_id": "gate_0b_audit_capsule_v6",
        "campaign_id": campaign_id,
        "policy_ms": policy_ms,
        "source_fact_bundle_sha256": source_fact_bundle_sha256,
        "execution_started_at": _format_utc_timestamp(execution_started_at),
        "execution_completed_at": _format_utc_timestamp(execution_completed_at),
        "provider_revision": provider_revision,
        "runtime_identity_before_sha256": runtime_identity_before_sha256,
        "runtime_identity_after_sha256": runtime_identity_after_sha256,
        "runtime_identity_before": runtime_identity_before,
        "runtime_identity_after": runtime_identity_after,
        "accounting": {
            "schema_id": "gate_0b_capsule_accounting_v1",
            "split": next(iter(splits)),
            "units": accounting_units,
        },
        "sessions": sessions,
        "activities": activities,
        "no_speech_windows": [
            {
                "window_ordinal": plan.window_ordinal,
                "split": plan.split,
                "condition": plan.condition,
                "wire_facts": _ordered_wire_fact_dicts(result.wire_facts),
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


def _ordered_wire_fact_dicts(facts: tuple[WireFact, ...]) -> list[dict[str, Any]]:
    return [
        {**fact.to_dict(), "sequence": sequence}
        for sequence, fact in enumerate(
            sorted(facts, key=lambda value: (value.at_ms, value.sequence))
        )
    ]


def _replay_observation_end_ms(replay_inputs: Sequence[Gate0BReplayInput]) -> int:
    return max(
        (
            value.at_ms + (value.duration_ms if value.kind == "audio" else 0)
            for value in replay_inputs
        ),
        default=0,
    )


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
    measurement_clock_ms: Callable[[], int],
    first_sequence: int,
    epoch: int,
) -> tuple[
    tuple[CallerTurnEvent, ...],
    set[str],
    dict[str, Any] | None,
    str | None,
]:
    message = await session.receive()
    at_ms = measurement_clock_ms()
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


def _validate_replay_topology(
    activities: tuple[SessionActivityPlan, ...],
    replay_inputs: tuple[Gate0BReplayInput, ...],
) -> None:
    if len(replay_inputs) > 10_000:
        raise RunnerError("session replay event count exceeds the fixed bound")
    supported = {
        "caller_activity_start",
        "audio",
        "caller_activity_end",
        "expect_synchronous_tool",
        "expect_tool_cancellation",
        "expect_interruption",
        "fresh_connection_restart",
    }
    if any(value.kind not in supported for value in replay_inputs):
        raise RunnerError("session replay input kind is invalid")
    ordered_activities = tuple(sorted(activities, key=lambda value: value.start_at_ms))
    for index, activity in enumerate(ordered_activities):
        related = tuple(
            value
            for value in replay_inputs
            if value.activity_ordinal == activity.activity_ordinal
            and value.kind != "fresh_connection_restart"
        )
        starts = [value for value in related if value.kind == "caller_activity_start"]
        ends = [value for value in related if value.kind == "caller_activity_end"]
        audio = [value for value in related if value.kind == "audio"]
        if len(starts) != 1 or len(ends) != 1 or not audio:
            raise RunnerError("activity replay requires one boundary pair and PCM audio")
        if (
            starts[0].at_ms != activity.start_at_ms
            or ends[0].at_ms != activity.end_at_ms
            or starts[0].epoch != activity.expected_epoch
            or ends[0].epoch != activity.expected_epoch
        ):
            raise RunnerError("activity replay boundaries do not match the activity")
        start_index = replay_inputs.index(starts[0])
        end_index = replay_inputs.index(ends[0])
        if start_index >= end_index:
            raise RunnerError("activity replay boundary order is invalid")
        for frame in audio:
            expected_bytes = frame.duration_ms * 16_000 * 2 // 1_000
            frame_index = replay_inputs.index(frame)
            if (
                frame.epoch != activity.expected_epoch
                or frame.duration_ms not in {10, 20, 30, 40}
                or not isinstance(frame.audio, bytes)
                or not expected_bytes - 2 <= len(frame.audio) <= expected_bytes
                or not activity.start_at_ms <= frame.at_ms
                or frame.at_ms + frame.duration_ms > activity.end_at_ms
                or not start_index < frame_index < end_index
            ):
                raise RunnerError("activity replay PCM topology is invalid")
        marker_requirements = {
            "expect_synchronous_tool": "synchronous_tool_use",
            "expect_tool_cancellation": "tool_cancellation_interruption",
            "expect_interruption": "tool_cancellation_interruption",
        }
        for marker_kind, scenario_tag in marker_requirements.items():
            markers = [value for value in related if value.kind == marker_kind]
            expected_count = 1 if scenario_tag in activity.scenario_tags else 0
            if len(markers) != expected_count:
                raise RunnerError("activity interaction markers do not match its scenario")
            if markers and (
                markers[0].at_ms != activity.end_at_ms
                or replay_inputs.index(markers[0]) <= end_index
                or markers[0].audio
                or markers[0].duration_ms
            ):
                raise RunnerError("activity interaction marker ordering is invalid")
        if "tool_cancellation_interruption" in activity.scenario_tags:
            if index == 0:
                raise RunnerError("tool cancellation requires a preceding tool activity")
            prior = ordered_activities[index - 1]
            if (
                "synchronous_tool_use" not in prior.scenario_tags
                or prior.expected_epoch != activity.expected_epoch
                or prior.end_at_ms > activity.start_at_ms
            ):
                raise RunnerError("tool cancellation is not causally paired")

    restart_markers = [
        value for value in replay_inputs if value.kind == "fresh_connection_restart"
    ]
    restart_activities = [
        value for value in ordered_activities if "fresh_connection_restart" in value.scenario_tags
    ]
    if len(restart_markers) != len(restart_activities):
        raise RunnerError("fresh restart markers do not match the allocation")
    for activity, marker in zip(restart_activities, restart_markers, strict=True):
        index = ordered_activities.index(activity)
        prior = ordered_activities[index - 1] if index else None
        if (
            prior is None
            or marker.activity_ordinal != prior.activity_ordinal
            or marker.at_ms != prior.end_at_ms
            or marker.epoch != activity.expected_epoch
            or marker.audio
            or marker.duration_ms
        ):
            raise RunnerError("fresh restart marker ordering is invalid")


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
    return sum(_extract_output_audio_chunks(message))


def _extract_output_audio_chunks(message: Mapping[str, Any]) -> tuple[int, ...]:
    content = message.get("serverContent")
    if not isinstance(content, Mapping):
        return ()
    model_turn = content.get("modelTurn")
    if model_turn is None:
        return ()
    if not isinstance(model_turn, Mapping):
        raise RunnerError("malformed_output_audio")
    parts = model_turn.get("parts", [])
    if not isinstance(parts, list):
        raise RunnerError("malformed_output_audio")
    sizes: list[int] = []
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
        if not decoded or len(decoded) > 1024 * 1024:
            raise RunnerError("oversized_output_audio")
        sizes.append(len(decoded))
    return tuple(sizes)


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


def _tool_cancellation_ids(message: Mapping[str, Any]) -> tuple[str, ...]:
    cancellation = message.get("toolCallCancellation")
    if cancellation is None:
        return ()
    if not isinstance(cancellation, Mapping) or set(cancellation) != {"ids"}:
        raise RunnerError("malformed_tool_cancellation")
    ids = cancellation["ids"]
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) != len(set(ids))
        or any(not isinstance(value, str) or not SAFE_ID.fullmatch(value) for value in ids)
    ):
        raise RunnerError("malformed_tool_cancellation")
    return tuple(ids)


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


def _latest_sent_activity(
    activities: tuple[SessionActivityPlan, ...],
    activity_started_at: Mapping[int, int],
    *,
    at_ms: int,
    epoch: int,
) -> SessionActivityPlan | None:
    eligible = [
        activity
        for activity in activities
        if activity.expected_epoch == epoch
        and activity.activity_ordinal in activity_started_at
        and activity_started_at[activity.activity_ordinal] <= at_ms
    ]
    return (
        max(
            eligible,
            key=lambda activity: (
                activity_started_at[activity.activity_ordinal],
                activity.activity_ordinal,
            ),
        )
        if eligible
        else None
    )


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
    wire_facts: tuple[WireFact, ...] = (),
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
        wire_facts=wire_facts,
    )


def _record_wire_fact(
    facts: list[WireFact],
    kind: str,
    at_ms: int,
    *,
    epoch: int,
    response_ordinal: int | None = None,
    activity_ordinal: int | None = None,
    audio_bytes: int = 0,
) -> None:
    facts.append(
        WireFact(
            kind=kind,
            at_ms=at_ms,
            response_ordinal=response_ordinal,
            activity_ordinal=activity_ordinal,
            sequence=len(facts),
            epoch=epoch,
            audio_bytes=audio_bytes,
        )
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


def _monotonic_measurement_clock(source: Callable[[], int]) -> Callable[[], int]:
    if not callable(source):
        raise TypeError("measurement clock must be callable")
    previous = -1

    def read() -> int:
        nonlocal previous
        try:
            value = source()
        except Exception:
            raise _MeasurementClockError from None
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 7_200_000
            or value < previous
        ):
            raise _MeasurementClockError
        previous = value
        return value

    return read


async def _receive_with_completion_drain(
    session: InjectedSession,
    *,
    sender_done: asyncio.Event,
    completion_pending: bool,
    quiet_timeout_ms: int,
) -> tuple[object | None, bool]:
    if not completion_pending:
        return await session.receive(), False

    receive_task = asyncio.create_task(session.receive())
    sender_wait_task: asyncio.Task[bool] | None = None
    receive_resolved = False
    try:
        if not sender_done.is_set():
            sender_wait_task = asyncio.create_task(sender_done.wait())
            completed, _ = await asyncio.wait(
                (receive_task, sender_wait_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receive_task in completed:
                receive_resolved = True
                return await receive_task, False

        receive_resolved = True
        try:
            message = await asyncio.wait_for(
                receive_task,
                timeout=quiet_timeout_ms / 1_000,
            )
        except TimeoutError:
            return None, True
        return message, False
    finally:
        if sender_wait_task is not None:
            if not sender_wait_task.done():
                sender_wait_task.cancel()
            try:
                await sender_wait_task
            except asyncio.CancelledError:
                pass
        if not receive_resolved:
            if not receive_task.done():
                receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass


def build_dry_run_preregistration() -> dict[str, Any]:
    """Describe the exact Gate 0B contract without creating executable approval."""
    return {
        "schema_id": "gate_0b_preregistration_dry_run_v2",
        "scope": "gate_0b_purpose_recorded_turn_assembly",
        "status": "implementation_only_not_executable",
        "immutable_values": {
            "model": EXACT_MODEL,
            "api_version": "v1beta",
            "endpoint": OFFICIAL_ENDPOINT,
            "transport": "websocket_bidi_tls",
            "project": None,
            "project_number": None,
            "credential_reference": None,
            "credential_key_resource_sha256": None,
            "credential_restrictions_sha256": None,
            "provider_quota_sha256": None,
            "credential_activated_at": None,
            "credential_expires_at": None,
            "credential_revocation_required_by": None,
            "credential_revocation_policy_sha256": None,
            "approval_key_id": None,
            "approval_public_key_sha256": None,
            "custodian_key_id": None,
            "custodian_public_key_sha256": None,
            "privacy_custodian_key_id": None,
            "privacy_custodian_public_key_sha256": None,
            "record_root_key_id": None,
            "record_root_public_key_sha256": None,
            "ledger_instance_id": None,
            "ledger_custodian_key_id": None,
            "ledger_custodian_public_key_sha256": None,
            "source_sha": None,
            "source_fact_bundle_sha256": None,
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
    if values.get("schema_id") != "gate_0b_preregistration_values_v2":
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
    project_number = values["project_number"]
    if not isinstance(project_number, str) or not PROJECT_NUMBER.fullmatch(project_number):
        raise RunnerError("preregistration project number is invalid")
    validated["project_number"] = project_number
    for field_name in (
        "credential_reference",
        "approval_key_id",
        "custodian_key_id",
        "privacy_custodian_key_id",
        "record_root_key_id",
        "ledger_instance_id",
        "ledger_custodian_key_id",
    ):
        validated[field_name] = _safe_id(
            values[field_name],
            label=field_name.replace("_", " "),
        )
    source_sha = values["source_sha"]
    if not isinstance(source_sha, str) or not SOURCE_SHA.fullmatch(source_sha):
        raise RunnerError("preregistration source SHA is invalid")
    validated["source_sha"] = source_sha
    activated_at = _parse_utc_timestamp(
        values["credential_activated_at"],
        label="credential activation time",
    )
    expires_at = _parse_utc_timestamp(
        values["credential_expires_at"],
        label="credential expiry time",
    )
    revocation_required_by = _parse_utc_timestamp(
        values["credential_revocation_required_by"],
        label="credential revocation deadline",
    )
    if not activated_at < revocation_required_by <= expires_at:
        raise RunnerError("preregistration credential lifetime is invalid")
    if expires_at - activated_at > timedelta(hours=24):
        raise RunnerError("preregistration credential lifetime is invalid")
    if any(
        values[field] != _format_utc_timestamp(value)
        for field, value in (
            ("credential_activated_at", activated_at),
            ("credential_expires_at", expires_at),
            ("credential_revocation_required_by", revocation_required_by),
        )
    ):
        raise RunnerError("preregistration credential timestamps are not canonical")
    validated.update(
        {
            "credential_activated_at": _format_utc_timestamp(activated_at),
            "credential_expires_at": _format_utc_timestamp(expires_at),
            "credential_revocation_required_by": _format_utc_timestamp(
                revocation_required_by
            ),
        }
    )
    for field_name in PREREGISTRATION_EXTERNAL_FIELDS - {
        "project",
        "project_number",
        "credential_reference",
        "approval_key_id",
        "custodian_key_id",
        "privacy_custodian_key_id",
        "record_root_key_id",
        "ledger_instance_id",
        "ledger_custodian_key_id",
        "source_sha",
        "credential_activated_at",
        "credential_expires_at",
        "credential_revocation_required_by",
    }:
        value = values[field_name]
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise RunnerError("preregistration digest is invalid")
        validated[field_name] = value

    key_id_fields = (
        "approval_key_id",
        "custodian_key_id",
        "privacy_custodian_key_id",
        "record_root_key_id",
        "ledger_custodian_key_id",
    )
    public_key_digest_fields = (
        "approval_public_key_sha256",
        "custodian_public_key_sha256",
        "privacy_custodian_public_key_sha256",
        "record_root_public_key_sha256",
        "ledger_custodian_public_key_sha256",
    )
    if len({validated[field] for field in key_id_fields}) != len(key_id_fields) or len(
        {validated[field] for field in public_key_digest_fields}
    ) != len(public_key_digest_fields):
        raise RunnerError("preregistration key roles must use distinct identities")

    document = build_dry_run_preregistration()
    document["status"] = "preregistered_pending_separate_approval"
    document["immutable_values"].update(validated)
    document["preregistration_sha256"] = sha256(canonical_json_bytes(document)).hexdigest()
    return document


def _parse_utc_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RunnerError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RunnerError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RunnerError(f"{label} is invalid")
    return parsed.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_external_values(path_value: str) -> Mapping[str, Any]:
    try:
        raw = read_private_file(
            Path(path_value),
            repo_root=REPO_ROOT,
            maximum_bytes=64 * 1024,
        )
        decoded = json.loads(raw)
    except (OSError, PrivatePathError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("preregistration values file is invalid") from exc
    if not isinstance(decoded, Mapping):
        raise RunnerError("preregistration values file is invalid")
    return decoded


def _write_dry_run_output(path_value: str, document: Mapping[str, Any]) -> None:
    try:
        write_private_file(
            Path(path_value),
            canonical_json_bytes(document) + b"\n",
            repo_root=REPO_ROOT,
        )
    except PrivatePathError as exc:
        raise RunnerError("dry-run output path is invalid") from exc


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
    if argv is None:
        try:
            capture_trusted_startup_identity(
                REPO_ROOT,
                expected_target="run-qualification",
            )
        except (IdentityError, OSError):
            print('{"error_code":"qualification_startup_invalid","status":"blocked"}')
            return 2
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
