"""Injected, zero-real-network Gate 0B session executor tests."""

import asyncio
import base64
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha1, sha256
from inspect import signature
import json
from pathlib import Path
import stat

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import pytest
import scripts.run_gemini_caller_turn_qualification as runner_module

from app.services.caller_turn_alignment import (
    ActivityReference,
    CriticalSpan,
    CriticalSpanKind,
)
from app.services.caller_turn_measurement import (
    derive_audit_capsule_accounting,
    derive_primitive_records_from_capsule,
    open_audit_capsule,
    usage_evidence_sha256,
)
from app.services.caller_turn_qualification import load_pricing
from app.services.caller_turns import CallerTurnEventKind
from app.services.qualification_environment import (
    EXECUTION_IDENTITY_SCHEMA_ID,
    execution_identity_report_sha256,
)
from app.services.qualification_identity import (
    EXECUTION_DEPENDENCY_PATHS as STARTUP_DEPENDENCY_PATHS,
    RUNTIME_SITE_PACKAGES_SCHEMA_ID,
    TRUSTED_STARTUP_POLICY_SCHEMA_ID,
    AttemptClaim,
    IdentityError,
    canonical_json_bytes,
    ledger_location_sha256,
)
from app.services.qualification_ledger import (
    CustodyLedgerState,
    LedgerCustodyIdentity,
    LedgerReceipt,
)
from app.services.qualification_privacy import QualificationAssets
from app.services.voice_turn_replay import Gate0BReplayInput
from scripts.run_gemini_caller_turn_qualification import (
    OFFICIAL_ENDPOINT,
    AuthorizedAssetRelease,
    AuthorizedAttemptConfig,
    CapsuleHandoffReceipt,
    CapturedExecutionIdentity,
    ConnectionPolicy,
    NoSpeechWindowPlan,
    ProviderSessionClosed,
    PrivateFileCapsuleSink,
    ReductionResult,
    RunnerError,
    SecretCredential,
    SessionActivityPlan,
    SessionExecutionConfig,
    SessionPlan,
    artifact_location_sha256,
    build_gate0b_setup_message,
    build_gate0b_setup_identity,
    build_dry_run_preregistration,
    build_parser,
    build_preregistration,
    compute_development_schedule_sha256,
    compute_holdout_schedule_sha256,
    execute_authorized_attempt,
    execute_authorized_holdout,
    execute_injected_session,
    execute_injected_no_speech_window,
    main,
)


CANARY_SECRET = "qualification-canary-secret-must-not-escape"
NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
PREREGISTRATION_SHA = "a" * 64
SOURCE_SHA = "b" * 40
APPROVAL_ROOT_RELATIVE_PATH = "config/qualification/gate_0b_approval_root.ed25519.pub"
APPROVAL_ROOT_PATH_SHA256 = sha256(APPROVAL_ROOT_RELATIVE_PATH.encode("utf-8")).hexdigest()
_NATIVE_RUNTIME_CLOSURE_UNSIGNED = {
    "schema_id": "gate_0b_native_runtime_closure_v1",
    "regular_file_count": 2,
    "regular_files_sha256": "9" * 64,
    "virtual_dependency_count": 1,
    "virtual_dependencies_sha256": "a" * 64,
    "system_loader_identity_sha256": "b" * 64,
}
NATIVE_RUNTIME_CLOSURE = {
    **_NATIVE_RUNTIME_CLOSURE_UNSIGNED,
    "closure_sha256": sha256(
        canonical_json_bytes(_NATIVE_RUNTIME_CLOSURE_UNSIGNED)
    ).hexdigest(),
}
_INTERPRETER_INSTALLATION_UNSIGNED = {
    "schema_id": "gate_0b_interpreter_installation_v2",
    "python_executable_sha256": "3" * 64,
    "stdlib_source_bytecode_sha256": "4" * 64,
    "stdlib_source_bytecode_count": 1,
    "stdlib_archive_sha256": "5" * 64,
    "stdlib_archive_count": 0,
    "native_extension_sha256": "6" * 64,
    "native_extension_count": 1,
    "native_runtime_closure": NATIVE_RUNTIME_CLOSURE,
}
INTERPRETER_INSTALLATION = {
    **_INTERPRETER_INSTALLATION_UNSIGNED,
    "installation_sha256": sha256(
        canonical_json_bytes(_INTERPRETER_INSTALLATION_UNSIGNED)
    ).hexdigest(),
}
_RUNTIME_SITE_PACKAGES_UNSIGNED = {
    "schema_id": RUNTIME_SITE_PACKAGES_SCHEMA_ID,
    "source_count": 1,
    "bytecode_count": 0,
    "native_extension_count": 0,
    "metadata_data_count": 0,
    "file_count": 1,
    "files_sha256": "7" * 64,
}
RUNTIME_SITE_PACKAGES_MANIFEST = {
    **_RUNTIME_SITE_PACKAGES_UNSIGNED,
    "manifest_sha256": sha256(
        canonical_json_bytes(_RUNTIME_SITE_PACKAGES_UNSIGNED)
    ).hexdigest(),
}
SOURCE_PREFLIGHT = {
    "source_sha": SOURCE_SHA,
    "clean": True,
    "dependencies": {
        sha256(path.encode("utf-8")).hexdigest(): {
            "worktree_sha256": (
                "7127478ff105944064f7e2d0d6f028f0ce6587ac8e3412ae1e379f89a2802b4f"
                if path == APPROVAL_ROOT_RELATIVE_PATH
                else "1" * 64
            ),
            "git_blob_id": (
                "248b7f7f150fcbd7de0c8733120cd1c642c3014e"
                if path == APPROVAL_ROOT_RELATIVE_PATH
                else "2" * 40
            ),
        }
        for path in STARTUP_DEPENDENCY_PATHS
    },
}
TRUSTED_STARTUP_REPORT = {
    "schema_id": TRUSTED_STARTUP_POLICY_SCHEMA_ID,
    "startup_flags": {
        "bytes_warning": 0,
        "debug": 0,
        "dev_mode": False,
        "dont_write_bytecode": 1,
        "hash_randomization": 1,
        "ignore_environment": 1,
        "inspect": 0,
        "int_max_str_digits": 4300,
        "interactive": 0,
        "isolated": 1,
        "no_site": 1,
        "no_user_site": 1,
        "optimize": 0,
        "quiet": 0,
        "safe_path": True,
        "utf8_mode": 0,
        "verbose": 0,
        "warn_default_encoding": 0,
    },
    "bytecode_write_disabled": True,
    "pycache_prefix_location_sha256": "2" * 64,
    "repo_root_location_sha256": "c" * 64,
    "python_executable_location_sha256": "d" * 64,
    "runtime_site_packages_location_sha256": "e" * 64,
    "effective_sys_path_sha256": "f" * 64,
    "effective_sys_path_entry_sha256": ["0" * 64, "1" * 64],
    "neutralized_environment": ["PYTHONHOME", "PYTHONPATH"],
    "runtime_pth_files_sha256": {},
    "ignored_startup_hook_files_sha256": {},
    "source_preflight": SOURCE_PREFLIGHT,
    "interpreter_installation": INTERPRETER_INSTALLATION,
    "runtime_site_packages_manifest": RUNTIME_SITE_PACKAGES_MANIFEST,
}
TEST_ENVIRONMENT_IDENTITY = {
    "python_version": "3.12.13",
    "uv_version": "0.11.7",
    "python_executable_sha256": "3" * 64,
    "uv_executable_sha256": "4" * 64,
    "python_executable_location_sha256": "5" * 64,
    "uv_executable_location_sha256": "6" * 64,
    "platform_id": "test-platform",
    "architecture": "test-architecture",
    "unicode_version": "15.1.0",
    "monotonic_clock_implementation": "test-monotonic",
    "monotonic_clock_resolution_ns": 1,
    "bytecode_write_disabled": True,
    "openssl_version": "OpenSSL test",
    "ca_bundle_sha256": "7" * 64,
    "lock_sha256": "8" * 64,
    "codec_golden_sha256": "9" * 64,
    "import_sha256": {"app.test": "a" * 64},
    "distributions": {"test-package": "1.0.0"},
    "distribution_files_sha256": {"test-package": "b" * 64},
    "interpreter_installation": INTERPRETER_INSTALLATION,
    "runtime_site_packages_manifest": RUNTIME_SITE_PACKAGES_MANIFEST,
}
TEST_EXECUTION_IDENTITY_REPORT = {
    "schema_id": EXECUTION_IDENTITY_SCHEMA_ID,
    "source": SOURCE_PREFLIGHT,
    "environment": TEST_ENVIRONMENT_IDENTITY,
    "trusted_startup": TRUSTED_STARTUP_REPORT,
}
TEST_EXECUTION_IDENTITY_SHA256 = execution_identity_report_sha256(
    TEST_EXECUTION_IDENTITY_REPORT
)
KEY_ID = "qualification-reviewer-v1"
LEDGER_PUBLIC_KEY = b"l" * 32
LEDGER_PUBLIC_KEY_SHA256 = sha256(LEDGER_PUBLIC_KEY).hexdigest()
LEASE_ID = "7" * 64
LEASE_ID_SHA256 = sha256(LEASE_ID.encode("ascii")).hexdigest()
PRIVACY_KEY_ID = "privacy_custodian_1"
PRIVACY_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(b"p" * 32)
PRIVACY_PUBLIC_KEY = PRIVACY_PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)


class _WrongCapsuleReceiptSink:
    def handoff(self, request):
        receipt = PrivateFileCapsuleSink().handoff(request)
        return replace(receipt, capsule_sha256="0" * 64)


class _ReceiptWithoutPersistenceSink:
    def handoff(self, request):
        return CapsuleHandoffReceipt(
            campaign_id=request.campaign_id,
            attempt_id=request.attempt_id,
            split=request.split,
            capsule_sha256=request.capsule_sha256,
            location_sha256=request.location_sha256,
            durable=True,
        )


@pytest.fixture(autouse=True)
def _fixed_execution_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner_module,
        "_capture_current_execution_identity",
        lambda *, expected_source_sha: CapturedExecutionIdentity(
            report=TEST_EXECUTION_IDENTITY_REPORT,
            sha256=TEST_EXECUTION_IDENTITY_SHA256,
        ),
    )
    monkeypatch.setattr(
        runner_module,
        "validate_custody_ledger_snapshot",
        lambda raw, **_kwargs: raw["state"],
    )


PRICING_PATH = Path("tests/fixtures/caller_turn_qualification/pricing.json")
LANGUAGES = ("ar", "en", "es", "fr", "hi", "ht", "pt", "zh")
CONDITIONS = ("clean", "twilio_codec_only", "acoustic_impairment", "interaction_stress")
STRESS_TAGS = (
    "jitter_packet_loss",
    "fresh_connection_restart",
    "synchronous_tool_use",
    "tool_cancellation_interruption",
    "background_noise",
    "long_pause",
    "fast_speech",
    "correction",
    "number_dictation",
    "clipping",
    "echo_crosstalk",
    "far_field_low_volume",
)
CRITICAL_KINDS = tuple(CriticalSpanKind)


def _allocation_tags_and_spans(
    language: str,
    within_language: int,
) -> tuple[tuple[str, ...], tuple[CriticalSpan, ...]]:
    tags = ["standard"]
    if within_language >= 12:
        tags = [
            tag
            for tag_index, tag in enumerate(STRESS_TAGS)
            if tag_index % 4 == within_language - 12
        ]
    if language != "en" and within_language == 10:
        tags.append("code_switch_english_to_language")
    if language != "en" and within_language == 11:
        tags.append("code_switch_language_to_english")
    applicable = {
        "number_dictation": CriticalSpanKind.DIGITS,
        "correction": CriticalSpanKind.CORRECTION,
        "code_switch_english_to_language": CriticalSpanKind.ENGLISH_TO_LANGUAGE,
        "code_switch_language_to_english": CriticalSpanKind.LANGUAGE_TO_ENGLISH,
    }
    kinds = {CRITICAL_KINDS[within_language % len(CRITICAL_KINDS)]}
    kinds.update(applicable[tag] for tag in tags if tag in applicable)
    spans = tuple(
        CriticalSpan(kind, "purpose", language)
        for kind in sorted(kinds, key=lambda value: value.value)
    )
    return tuple(tags), spans


class FakeSession:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    async def receive(self):
        for _ in range(8):
            await asyncio.sleep(0)
        if not self.messages:
            return None
        value = self.messages.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self):
        self.closed = True


class YieldingFakeSession(FakeSession):
    async def receive(self):
        for _ in range(8):
            await asyncio.sleep(0)
        return await super().receive()


class CausallySequencedFakeSession(FakeSession):
    def __init__(self, messages, *, response_audio_counts):
        super().__init__(messages)
        self._response_audio_counts = iter(response_audio_counts)
        self._accepted_audio_count = 0
        self._delivered_audio_count = 0
        self._audio_accepted = asyncio.Event()
        self._response_delivered = asyncio.Event()
        self._setup_received = False

    async def send(self, message):
        realtime_input = message.get("realtimeInput")
        if isinstance(realtime_input, dict) and "audio" in realtime_input:
            next_audio_count = self._accepted_audio_count + 1
            while self._delivered_audio_count < next_audio_count - 1:
                self._response_delivered.clear()
                await self._response_delivered.wait()
            self._accepted_audio_count = next_audio_count
            self._audio_accepted.set()
        await super().send(message)

    async def receive(self):
        if not self._setup_received:
            self._setup_received = True
            return await super().receive()
        if not self.messages:
            return None
        message = self.messages[0]
        is_provider_event = isinstance(message, dict) and "usageMetadata" not in message
        if is_provider_event:
            required_audio_count = next(self._response_audio_counts)
            while self._accepted_audio_count < required_audio_count:
                self._audio_accepted.clear()
                await self._audio_accepted.wait()
            delivered = await super().receive()
            self._delivered_audio_count = max(
                self._delivered_audio_count,
                required_audio_count,
            )
            self._response_delivered.set()
            return delivered
        return await super().receive()


class HangingAfterMessagesSession(YieldingFakeSession):
    async def receive(self):
        if self.messages:
            return await super().receive()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class DelayedMessageSession(YieldingFakeSession):
    def __init__(self, messages, *, delayed_index: int, delay_seconds: float):
        super().__init__(messages)
        self._receive_index = 0
        self._delayed_index = delayed_index
        self._delay_seconds = delay_seconds

    async def receive(self):
        if self._receive_index == self._delayed_index:
            await asyncio.sleep(self._delay_seconds)
        self._receive_index += 1
        return await super().receive()


class FakeConnector:
    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.requests = []

    async def connect(self, request):
        self.requests.append(request)
        return self.sessions.pop(0)


class ReceiptClock:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


def test_injected_executor_accepts_only_one_measurement_clock_factory() -> None:
    parameters = signature(execute_injected_session).parameters

    assert "measurement_clock_factory" in parameters
    assert "receipt_clock_ms" not in parameters
    assert "outbound_clock_factory" not in parameters


def _outbound_clock_values(
    plan: SessionPlan | NoSpeechWindowPlan,
) -> list[int]:
    if isinstance(plan, NoSpeechWindowPlan):
        return [value.at_ms for value in plan.replay_inputs]

    values: list[int] = []
    recorded_speech_ends: set[int] = set()
    previous_at_ms = 0
    for replay_input in plan.replay_inputs:
        if replay_input.kind == "fresh_connection_restart":
            previous_at_ms = replay_input.at_ms
            continue
        crossed = sorted(
            (
                activity
                for activity in plan.activities
                if activity.expected_epoch == replay_input.epoch
                and activity.activity_ordinal not in recorded_speech_ends
                and previous_at_ms
                < activity.speech_end_at_ms
                <= replay_input.at_ms
            ),
            key=lambda activity: activity.speech_end_at_ms,
        )
        for activity in crossed:
            values.append(activity.speech_end_at_ms)
            recorded_speech_ends.add(activity.activity_ordinal)
        if replay_input.kind in {
            "caller_activity_start",
            "audio",
            "caller_activity_end",
        }:
            values.append(replay_input.at_ms)
        previous_at_ms = replay_input.at_ms
    return values


def _measurement_clock_factory(
    receipt_values: list[int],
):
    def build(plan: SessionPlan | NoSpeechWindowPlan) -> ReceiptClock:
        values = [*receipt_values, *_outbound_clock_values(plan)]
        if isinstance(plan, NoSpeechWindowPlan):
            values.append(max(values, default=0) + 1)
        else:
            segments = runner_module._connection_segments(plan)
            for index, (_epoch, base_at_ms, _inputs) in enumerate(segments):
                next_base_at_ms = (
                    segments[index + 1][1] if index + 1 < len(segments) else None
                )
                segment_values = [
                    value
                    for value in values
                    if value >= base_at_ms
                    and (next_base_at_ms is None or value < next_base_at_ms)
                ]
                values.append(max(segment_values, default=base_at_ms) + 1)
        return ReceiptClock(sorted(values))

    return build


def _identity_for_approval_root(data: bytes) -> CapturedExecutionIdentity:
    report = deepcopy(TEST_EXECUTION_IDENTITY_REPORT)
    blob_payload = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
    dependency = {
        "worktree_sha256": sha256(data).hexdigest(),
        "git_blob_id": sha1(blob_payload, usedforsecurity=False).hexdigest(),
    }
    report["source"]["dependencies"][APPROVAL_ROOT_PATH_SHA256] = dependency  # type: ignore[index]
    report["trusted_startup"]["source_preflight"] = deepcopy(report["source"])  # type: ignore[index]
    return CapturedExecutionIdentity(
        report=report,
        sha256=execution_identity_report_sha256(report),
    )


def _default_measurement_clock_factory(
    plan: SessionPlan | NoSpeechWindowPlan,
) -> ReceiptClock:
    return _measurement_clock_factory(_receipt_times_for_plan(plan))(plan)


class FakeCustodyLedger:
    def __init__(
        self,
        path: Path,
        *,
        order: list[str] | None = None,
        public_key_sha256: str = LEDGER_PUBLIC_KEY_SHA256,
        initial_state: CustodyLedgerState | None = None,
        campaign_envelope: dict[str, object] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._order = order
        self._state = initial_state
        self._identity = LedgerCustodyIdentity(
            ledger_instance_id="ledger_instance_1",
            key_id="ledger_custodian_1",
            public_key_sha256=public_key_sha256,
            ledger_location_sha256=ledger_location_sha256(path),
        )
        if self._state is None and campaign_envelope is not None:
            payload = campaign_envelope["payload"]
            assert isinstance(payload, dict)
            self._state = CustodyLedgerState(
                campaign_id=payload["campaign_id"],
                authorization_id=payload["authorization_id"],
                preregistration_sha256=payload["preregistration_sha256"],
                source_sha=payload["source_sha"],
                ledger_location_sha256=payload["ledger_location_sha256"],
                phase="preregistered",
                phase_history=("preregistered",),
                attempt_ids=(),
                active_attempt_id=None,
                completed_attempt_id=None,
                campaign_approval_sha256=sha256(
                    canonical_json_bytes(payload)
                ).hexdigest(),
                attempt_authorization_sha256=None,
                attempt_claimed_at=None,
                lease_id_sha256=None,
                provider_requests_reserved=0,
                cost_reserved_microusd=0,
                selected_policy_ms=None,
                policy_lock_sha256=None,
                development_capsule_sha256=None,
                development_ledger_head_sha256=None,
                holdout_manifest_sha256=None,
                holdout_execution_claimed=False,
                holdout_execution_claimed_at=None,
                holdout_capsule_sha256=None,
                development_usage_evidence_sha256=None,
                final_usage_evidence_sha256=None,
                development_provider_requests=0,
                development_cost_microusd=0,
                development_input_audio_ms=0,
                development_output_audio_bytes=0,
                development_elapsed_ms=0,
                actual_provider_requests=0,
                actual_cost_microusd=0,
                campaign_max_attempts=payload["max_attempts"],
                campaign_max_provider_requests=payload["max_provider_requests"],
                campaign_max_cost_microusd=payload["max_cost_microusd"],
                record_sha256s=("1" * 64,),
                record_events=("genesis",),
                final_ledger_head_sha256="1" * 64,
            )

    def identity(self) -> LedgerCustodyIdentity:
        return self._identity

    def claim_attempt(self, **values):
        self._record("claim", values)
        campaign = values["campaign"]
        authorization = values["authorization"]
        claim = AttemptClaim(
            campaign_id=campaign.campaign_id,
            attempt_id=authorization.attempt_id,
            attempt_index=authorization.attempt_index,
            lease_id=LEASE_ID,
            provider_requests_reserved=authorization.provider_request_reservation,
            cost_reserved_microusd=authorization.cost_reservation_microusd,
        )
        assert self._state is not None
        prior_state = self._state
        self._state = CustodyLedgerState(
            campaign_id=campaign.campaign_id,
            authorization_id=campaign.authorization_id,
            preregistration_sha256=campaign.preregistration_sha256,
            source_sha=campaign.source_sha,
            ledger_location_sha256=campaign.ledger_location_sha256,
            phase="development_collection",
            phase_history=(*prior_state.phase_history, "development_collection"),
            attempt_ids=(*prior_state.attempt_ids, authorization.attempt_id),
            active_attempt_id=authorization.attempt_id,
            completed_attempt_id=None,
            campaign_approval_sha256=campaign.signed_payload_sha256,
            attempt_authorization_sha256=authorization.signed_payload_sha256,
            attempt_claimed_at=values["now"],
            lease_id_sha256=LEASE_ID_SHA256,
            provider_requests_reserved=authorization.provider_request_reservation,
            cost_reserved_microusd=authorization.cost_reservation_microusd,
            selected_policy_ms=None,
            policy_lock_sha256=None,
            development_capsule_sha256=None,
            development_ledger_head_sha256=None,
            holdout_manifest_sha256=None,
            holdout_execution_claimed=False,
            holdout_execution_claimed_at=None,
            holdout_capsule_sha256=None,
            development_usage_evidence_sha256=None,
            final_usage_evidence_sha256=None,
            development_provider_requests=0,
            development_cost_microusd=0,
            development_input_audio_ms=0,
            development_output_audio_bytes=0,
            development_elapsed_ms=0,
            actual_provider_requests=0,
            actual_cost_microusd=0,
            campaign_max_attempts=prior_state.campaign_max_attempts,
            campaign_max_provider_requests=prior_state.campaign_max_provider_requests,
            campaign_max_cost_microusd=prior_state.campaign_max_cost_microusd,
            record_sha256s=(*prior_state.record_sha256s, "2" * 64),
            record_events=(*prior_state.record_events, "claim"),
            final_ledger_head_sha256="2" * 64,
        )
        return claim

    def record_development_checkpoint(self, **values) -> LedgerReceipt:
        self._record("development_checkpoint", values)
        assert self._state is not None
        self._state = replace(
            self._state,
            development_capsule_sha256=values["development_capsule_sha256"],
            development_usage_evidence_sha256=values["usage_evidence_sha256"],
            development_provider_requests=values["actual_provider_requests"],
            development_cost_microusd=values["actual_cost_microusd"],
            development_input_audio_ms=values["input_audio_duration_ms"],
            development_output_audio_bytes=values["output_audio_bytes"],
            development_elapsed_ms=values["elapsed_ms"],
            record_sha256s=(*self._state.record_sha256s, "3" * 64),
            record_events=(*self._state.record_events, "development_checkpoint"),
            final_ledger_head_sha256="3" * 64,
        )
        return LedgerReceipt("development_checkpoint", 3, "6" * 64, "development_collection")

    def resume_holdout(self, **values):
        self._record("resume_holdout", values)
        campaign = values["campaign"]
        authorization = values["authorization"]
        assert self._state is not None
        self._state = replace(
            self._state,
            holdout_execution_claimed=True,
            holdout_execution_claimed_at=values["now"],
            record_sha256s=(*self._state.record_sha256s, "6" * 64),
            record_events=(*self._state.record_events, "holdout_execution_claim"),
            final_ledger_head_sha256="6" * 64,
        )
        return AttemptClaim(
            campaign_id=campaign.campaign_id,
            attempt_id=authorization.attempt_id,
            attempt_index=authorization.attempt_index,
            lease_id=LEASE_ID,
            provider_requests_reserved=authorization.provider_request_reservation,
            cost_reserved_microusd=authorization.cost_reservation_microusd,
        )

    def record_policy_lock(self, **values) -> LedgerReceipt:
        self._record("policy_lock", values)
        return LedgerReceipt("policy_lock", 4, "5" * 64, "policy_selection_locked")

    def release_holdout(self, **values) -> LedgerReceipt:
        self._record("holdout_release", values)
        return LedgerReceipt("holdout_release", 5, "4" * 64, "holdout_collection")

    def record_terminal_outcome(self, **values) -> LedgerReceipt:
        self._record("terminal_outcome", values)
        assert self._state is not None
        completed = values["outcome"] == "completed"
        self._state = replace(
            self._state,
            phase="completed" if completed else "aborted",
            phase_history=(*self._state.phase_history, "completed" if completed else "aborted"),
            active_attempt_id=None,
            completed_attempt_id=(
                values["claim"].attempt_id if completed else None
            ),
            holdout_capsule_sha256=values["holdout_capsule_sha256"],
            final_usage_evidence_sha256=values["usage_evidence_sha256"],
            actual_provider_requests=values["actual_provider_requests"],
            actual_cost_microusd=values["actual_cost_microusd"],
            lease_id_sha256=None,
            record_sha256s=(*self._state.record_sha256s, "7" * 64),
            record_events=(*self._state.record_events, "terminal_outcome"),
            final_ledger_head_sha256="7" * 64,
        )
        return LedgerReceipt("terminal_outcome", 6, "3" * 64, "aborted")

    def export_snapshot(self):
        self._record("export_snapshot", {})
        if self._state is None:
            raise AssertionError("fake ledger state was not initialized")
        return {"state": self._state}

    def _record(self, name: str, values: dict[str, object]) -> None:
        self.calls.append((name, values))
        if self._order is not None:
            self._order.append(name)


def _usage_message(
    *,
    input_audio_tokens: int = 8,
    input_text_tokens: int = 2,
    output_audio_tokens: int = 4,
    output_text_tokens: int = 1,
    thoughts_tokens: int = 0,
):
    prompt_tokens = input_audio_tokens + input_text_tokens
    response_tokens = output_audio_tokens + output_text_tokens
    return {
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "responseTokenCount": response_tokens,
            "thoughtsTokenCount": thoughts_tokens,
            "totalTokenCount": prompt_tokens + response_tokens + thoughts_tokens,
            "promptTokensDetails": [
                {"modality": "AUDIO", "tokenCount": input_audio_tokens},
                {"modality": "TEXT", "tokenCount": input_text_tokens},
            ],
            "responseTokensDetails": [
                {"modality": "AUDIO", "tokenCount": output_audio_tokens},
                {"modality": "TEXT", "tokenCount": output_text_tokens},
            ],
        }
    }


def _server_event(
    *,
    text="book service today",
    audio=b"\x01\x02\x03\x04",
    terminal=True,
):
    content = {
        "inputTranscription": {"text": text},
        "modelTurn": {
            "parts": [
                {
                    "inlineData": {
                        "mimeType": "audio/pcm;rate=24000",
                        "data": base64.b64encode(audio).decode("ascii"),
                    }
                }
            ]
        },
    }
    if terminal:
        content["turnComplete"] = True
    return {"serverContent": content}


def _activity(
    ordinal: int,
    *,
    start_ms: int,
    end_ms: int,
) -> SessionActivityPlan:
    return SessionActivityPlan(
        activity_ordinal=ordinal,
        split="development",
        language="en",
        condition="clean",
        scenario_tags=("standard",),
        reference=ActivityReference(ordinal, "en", f"book service today {ordinal}"),
        expected_lifecycle_status="retrospective_complete",
        expected_epoch=1,
        start_at_ms=start_ms,
        speech_end_at_ms=end_ms - 20,
        end_at_ms=end_ms,
    )


def _plan(*, two_activities: bool = False) -> SessionPlan:
    activities = (_activity(1, start_ms=0, end_ms=100),)
    inputs = (
        Gate0BReplayInput("caller_activity_start", 0, 1, 1),
        Gate0BReplayInput("audio", 20, 1, 1, audio=b"\x00\x00" * 319, duration_ms=20),
        Gate0BReplayInput("caller_activity_end", 100, 1, 1),
    )
    if two_activities:
        activities += (_activity(2, start_ms=150, end_ms=250),)
        inputs += (
            Gate0BReplayInput("caller_activity_start", 150, 1, 2),
            Gate0BReplayInput("audio", 170, 1, 2, audio=b"\x00\x00" * 319, duration_ms=20),
            Gate0BReplayInput("caller_activity_end", 250, 1, 2),
        )
    return SessionPlan(
        session_ordinal=1,
        split="development",
        activities=activities,
        replay_inputs=inputs,
    )


def _development_schedule() -> tuple[tuple[SessionPlan, ...], tuple[NoSpeechWindowPlan, ...]]:
    return _split_schedule("development")


def _holdout_schedule() -> tuple[tuple[SessionPlan, ...], tuple[NoSpeechWindowPlan, ...]]:
    return _split_schedule("holdout")


def _split_schedule(
    split: str,
) -> tuple[tuple[SessionPlan, ...], tuple[NoSpeechWindowPlan, ...]]:
    activity_base = 0 if split == "development" else 128
    session_base = 0 if split == "development" else 24
    window_base = 0 if split == "development" else 32
    session_ranges = ((0, 6), (6, 12), (12, 16))
    plans: list[SessionPlan] = []
    for language_index, language in enumerate(LANGUAGES):
        for language_session, (first_position, final_position) in enumerate(session_ranges):
            activities: list[SessionActivityPlan] = []
            replay_inputs: list[Gate0BReplayInput] = []
            for local_index, within_language in enumerate(range(first_position, final_position)):
                ordinal = activity_base + language_index * 16 + within_language
                start_ms = local_index * 150
                end_ms = start_ms + 100
                condition = CONDITIONS[within_language // 4]
                scenario_tags, critical_spans = _allocation_tags_and_spans(
                    language,
                    within_language,
                )
                epoch = 2 if within_language >= 13 else 1
                if "fresh_connection_restart" in scenario_tags:
                    prior = activities[-1]
                    replay_inputs.append(
                        Gate0BReplayInput(
                            "fresh_connection_restart",
                            prior.end_at_ms,
                            epoch,
                            prior.activity_ordinal,
                        )
                    )
                activities.append(
                    SessionActivityPlan(
                        activity_ordinal=ordinal,
                        split=split,
                        language=language,
                        condition=condition,
                        scenario_tags=scenario_tags,
                        reference=ActivityReference(
                            ordinal,
                            language,
                            f"purpose recorded phrase {ordinal}",
                            critical_spans,
                        ),
                        expected_lifecycle_status="retrospective_complete",
                        expected_epoch=epoch,
                        start_at_ms=start_ms,
                        speech_end_at_ms=end_ms - 20,
                        end_at_ms=end_ms,
                    )
                )
                replay_inputs.extend(
                    (
                        Gate0BReplayInput("caller_activity_start", start_ms, epoch, ordinal),
                        Gate0BReplayInput(
                            "audio",
                            start_ms + 20,
                            epoch,
                            ordinal,
                            audio=b"\x00\x00" * 319,
                            duration_ms=20,
                        ),
                        Gate0BReplayInput("caller_activity_end", end_ms, epoch, ordinal),
                    )
                )
                if "synchronous_tool_use" in scenario_tags:
                    replay_inputs.append(
                        Gate0BReplayInput("expect_synchronous_tool", end_ms, epoch, ordinal)
                    )
                if "tool_cancellation_interruption" in scenario_tags:
                    replay_inputs.extend(
                        (
                            Gate0BReplayInput(
                                "expect_tool_cancellation", end_ms, epoch, ordinal
                            ),
                            Gate0BReplayInput("expect_interruption", end_ms, epoch, ordinal),
                        )
                    )
            plans.append(
                SessionPlan(
                    session_ordinal=session_base + language_index * 3 + language_session,
                    split=split,
                    activities=tuple(activities),
                    replay_inputs=tuple(replay_inputs),
                )
            )
    windows = tuple(
        NoSpeechWindowPlan(
            window_ordinal=window_base + index,
            split=split,
            condition="silence" if index < 16 else "background_noise",
            replay_inputs=(
                Gate0BReplayInput(
                    "audio",
                    0,
                    1,
                    None,
                    audio=b"\x00\x00" * 319,
                    duration_ms=20,
                ),
            ),
        )
        for index in range(32)
    )
    return tuple(plans), windows


def _restart_plan() -> SessionPlan:
    first = _activity(1, start_ms=0, end_ms=100)
    second = SessionActivityPlan(
        activity_ordinal=2,
        split="development",
        language="en",
        condition="clean",
        scenario_tags=("fresh_connection_restart",),
        reference=ActivityReference(2, "en", "book service today 2"),
        expected_lifecycle_status="retrospective_complete",
        expected_epoch=2,
        start_at_ms=700,
        speech_end_at_ms=780,
        end_at_ms=800,
    )
    return SessionPlan(
        session_ordinal=1,
        split="development",
        activities=(first, second),
        replay_inputs=(
            Gate0BReplayInput("caller_activity_start", 0, 1, 1),
            Gate0BReplayInput("audio", 20, 1, 1, audio=b"\x00\x00" * 319, duration_ms=20),
            Gate0BReplayInput("caller_activity_end", 100, 1, 1),
            Gate0BReplayInput("fresh_connection_restart", 100, 2, 1),
            Gate0BReplayInput("caller_activity_start", 700, 2, 2),
            Gate0BReplayInput("audio", 720, 2, 2, audio=b"\x00\x00" * 319, duration_ms=20),
            Gate0BReplayInput("caller_activity_end", 800, 2, 2),
        ),
    )


def _no_speech_plan() -> NoSpeechWindowPlan:
    return NoSpeechWindowPlan(
        window_ordinal=0,
        split="development",
        condition="background_noise",
        replay_inputs=(
            Gate0BReplayInput(
                "audio",
                0,
                1,
                None,
                audio=b"\x00\x00" * 319,
                duration_ms=20,
            ),
        ),
    )


def _successful_sessions_for_plans(plans: tuple[SessionPlan, ...]) -> list[FakeSession]:
    sessions: list[FakeSession] = []
    for plan in plans:
        activities = {value.activity_ordinal: value for value in plan.activities}
        has_cancellation = any(
            "tool_cancellation_interruption" in value.scenario_tags
            for value in plan.activities
        )
        for epoch, _base_at_ms, replay_inputs in runner_module._connection_segments(plan):
            ordinals = tuple(
                dict.fromkeys(
                    value.activity_ordinal
                    for value in replay_inputs
                    if value.activity_ordinal is not None and value.kind == "caller_activity_start"
                )
            )
            messages: list[object] = [{"setupComplete": {}}]
            for ordinal in ordinals:
                activity = activities[ordinal]
                message = _server_event(
                    text=activity.reference.text,
                    terminal=not (
                        has_cancellation
                        and "synchronous_tool_use" in activity.scenario_tags
                    ),
                )
                if "synchronous_tool_use" in activity.scenario_tags:
                    message["toolCall"] = {
                        "functionCalls": [
                            {"id": f"tool_{epoch}", "name": "synthetic_lookup", "args": {}}
                        ]
                    }
                if "tool_cancellation_interruption" in activity.scenario_tags:
                    message = {
                        "serverContent": {
                            "inputTranscription": {"text": activity.reference.text},
                            "interrupted": True,
                        },
                        "toolCallCancellation": {"ids": [f"tool_{epoch}"]},
                    }
                messages.append(message)
            messages.extend((_usage_message(), None))
            sessions.append(
                CausallySequencedFakeSession(
                    messages,
                    response_audio_counts=range(1, len(ordinals) + 1),
                )
            )
    return sessions


def _receipt_times_for_plan(plan: SessionPlan | NoSpeechWindowPlan) -> list[int]:
    if isinstance(plan, NoSpeechWindowPlan):
        return [0, 30]
    values: list[int] = []
    previous = 0
    for _epoch, base_at_ms, replay_inputs in runner_module._connection_segments(plan):
        previous = max(previous, base_at_ms)
        values.append(previous)
        ordinals = tuple(
            dict.fromkeys(
                value.activity_ordinal
                for value in replay_inputs
                if value.activity_ordinal is not None and value.kind == "caller_activity_start"
            )
        )
        activities = {value.activity_ordinal: value for value in plan.activities}
        for ordinal in ordinals:
            previous = max(previous, activities[ordinal].end_at_ms + 20)
            values.append(previous)
        previous = max(
            previous,
            max((activities[ordinal].end_at_ms for ordinal in ordinals), default=0) + 30,
        )
        values.append(previous)
    return values


def _tool_interaction_plan() -> SessionPlan:
    base = _plan(two_activities=True)
    activities = (
        replace(base.activities[0], scenario_tags=("synchronous_tool_use",)),
        base.activities[1],
    )
    return SessionPlan(
        session_ordinal=base.session_ordinal,
        split=base.split,
        activities=activities,
        replay_inputs=(
            *base.replay_inputs[:3],
            Gate0BReplayInput("expect_synchronous_tool", 100, 1, 1),
            *base.replay_inputs[3:],
        ),
    )


def _cancellation_interaction_plan() -> SessionPlan:
    base = _plan(two_activities=True)
    activities = (
        replace(base.activities[0], scenario_tags=("synchronous_tool_use",)),
        replace(
            base.activities[1],
            scenario_tags=("tool_cancellation_interruption",),
        ),
    )
    return SessionPlan(
        session_ordinal=base.session_ordinal,
        split=base.split,
        activities=activities,
        replay_inputs=(
            *base.replay_inputs[:3],
            Gate0BReplayInput("expect_synchronous_tool", 100, 1, 1),
            *base.replay_inputs[3:],
            Gate0BReplayInput("expect_tool_cancellation", 250, 1, 2),
            Gate0BReplayInput("expect_interruption", 250, 1, 2),
        ),
    )


def _config() -> SessionExecutionConfig:
    return SessionExecutionConfig(
        endpoint=OFFICIAL_ENDPOINT,
        model="models/gemini-3.1-flash-live-preview",
        project="kevin-qualification-test",
        max_message_bytes=64 * 1024,
        session_timeout_seconds=30,
        response_gap_limit_ms=500,
        post_completion_observation_ms=3_000,
    )


def test_session_plan_rejects_duplicate_activity_boundary() -> None:
    base = _plan()

    with pytest.raises(ValueError, match="one boundary pair"):
        replace(
            base,
            replay_inputs=(base.replay_inputs[0], *base.replay_inputs),
        )


def test_session_plan_rejects_audio_outside_activity_boundary() -> None:
    base = _plan()
    audio = replace(base.replay_inputs[1], at_ms=90, duration_ms=20)

    with pytest.raises(ValueError, match="PCM topology"):
        replace(base, replay_inputs=(base.replay_inputs[0], audio, base.replay_inputs[2]))


def test_session_plan_rejects_noncausal_cancellation_markers() -> None:
    base = _plan(two_activities=True)
    activities = (
        base.activities[0],
        replace(
            base.activities[1],
            scenario_tags=("tool_cancellation_interruption",),
        ),
    )
    replay_inputs = (
        *base.replay_inputs,
        Gate0BReplayInput("expect_tool_cancellation", 250, 1, 2),
        Gate0BReplayInput("expect_interruption", 250, 1, 2),
    )

    with pytest.raises(ValueError, match="causally paired"):
        replace(base, activities=activities, replay_inputs=replay_inputs)


def _key_pair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private, public


def _custodian_key_pair() -> tuple[X25519PrivateKey, bytes]:
    private = X25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private, public


def _signed(private: Ed25519PrivateKey, payload: dict[str, object]) -> dict[str, object]:
    return {
        "key_id": KEY_ID,
        "payload": payload,
        "signature": base64.b64encode(private.sign(canonical_json_bytes(payload))).decode("ascii"),
    }


def _approval_envelopes(
    private: Ed25519PrivateKey,
    ledger_path: Path,
    *,
    preregistration_sha256: str = PREREGISTRATION_SHA,
    provider_request_reservation: int = 128,
    cost_reservation_microusd: int = 10_000_000,
    max_attempts: int = 3,
    max_provider_requests: int = 384,
    max_cost_microusd: int = 30_000_000,
    ledger_custodian_public_key_sha256: str = LEDGER_PUBLIC_KEY_SHA256,
) -> tuple[dict[str, object], dict[str, object]]:
    campaign = {
        "schema_id": "gate_0b_campaign_approval_v1",
        "scope": "gate_0b_purpose_recorded_turn_assembly",
        "campaign_id": "campaign_001",
        "authorization_id": "authorization_001",
        "nonce": "nonce_001",
        "preregistration_sha256": preregistration_sha256,
        "source_sha": SOURCE_SHA,
        "issued_at": "2026-07-15T14:59:00Z",
        "expires_at": "2026-07-15T16:00:00Z",
        "max_attempts": max_attempts,
        "max_provider_requests": max_provider_requests,
        "max_cost_microusd": max_cost_microusd,
        "ledger_instance_id": "ledger_instance_1",
        "ledger_custodian_key_id": "ledger_custodian_1",
        "ledger_custodian_public_key_sha256": ledger_custodian_public_key_sha256,
        "ledger_location_sha256": ledger_location_sha256(ledger_path),
        "real_caller_data_authorized": False,
        "runtime_wiring_authorized": False,
        "deployment_authorized": False,
        "production_authorized": False,
        "release_authorized": False,
    }
    attempt = {
        "schema_id": "gate_0b_attempt_authorization_v1",
        "campaign_id": "campaign_001",
        "authorization_id": "authorization_001",
        "attempt_id": "attempt_001",
        "attempt_index": 1,
        "prior_attempt_id": None,
        "outage_enum": None,
        "preregistration_sha256": preregistration_sha256,
        "source_sha": SOURCE_SHA,
        "issued_at": "2026-07-15T14:59:00Z",
        "expires_at": "2026-07-15T16:00:00Z",
        "provider_request_reservation": provider_request_reservation,
        "cost_reservation_microusd": cost_reservation_microusd,
    }
    return _signed(private, campaign), _signed(private, attempt)


def _preregistration(
    approval_public_key: bytes,
    ledger_path: Path,
    custodian_public_key: bytes,
    *,
    ledger_custodian_public_key_sha256: str = LEDGER_PUBLIC_KEY_SHA256,
    environment_identity_sha256: str = TEST_EXECUTION_IDENTITY_SHA256,
) -> dict[str, object]:
    setup_sha256 = sha256(canonical_json_bytes(build_gate0b_setup_identity(_config()))).hexdigest()
    plans, no_speech_plans = _development_schedule()
    return build_preregistration(
        {
            "schema_id": "gate_0b_preregistration_values_v2",
            "project": _config().project,
            "project_number": "123456789012",
            "credential_reference": "qualification_secret_v1",
            "credential_key_resource_sha256": "1" * 64,
            "credential_restrictions_sha256": "2" * 64,
            "provider_quota_sha256": "3" * 64,
            "credential_activated_at": "2026-07-15T14:00:00Z",
            "credential_expires_at": "2026-07-15T16:00:00Z",
            "credential_revocation_required_by": "2026-07-15T16:00:00Z",
            "credential_revocation_policy_sha256": "4" * 64,
            "approval_key_id": KEY_ID,
            "approval_public_key_sha256": sha256(approval_public_key).hexdigest(),
            "custodian_key_id": "audit_custodian_1",
            "custodian_public_key_sha256": sha256(custodian_public_key).hexdigest(),
            "privacy_custodian_key_id": PRIVACY_KEY_ID,
            "privacy_custodian_public_key_sha256": sha256(
                PRIVACY_PUBLIC_KEY
            ).hexdigest(),
            "record_root_key_id": "evidence_custodian_1",
            "record_root_public_key_sha256": "9" * 64,
            "ledger_instance_id": "ledger_instance_1",
            "ledger_custodian_key_id": "ledger_custodian_1",
            "ledger_custodian_public_key_sha256": ledger_custodian_public_key_sha256,
            "source_sha": SOURCE_SHA,
            "source_fact_bundle_sha256": "5" * 64,
            "environment_identity_sha256": environment_identity_sha256,
            "manifest_sha256": "3" * 64,
            "corpus_sha256": "4" * 64,
            "development_schedule_sha256": compute_development_schedule_sha256(
                plans,
                no_speech_plans=no_speech_plans,
            ),
            "setup_sha256": setup_sha256,
            "pricing_sha256": sha256(PRICING_PATH.read_bytes()).hexdigest(),
            "runner_sha256": sha256(Path(runner_module.__file__).read_bytes()).hexdigest(),
            "evaluator_sha256": sha256(
                Path("scripts/evaluate_gemini_caller_turn_qualification.py").read_bytes()
            ).hexdigest(),
            "ledger_location_sha256": ledger_location_sha256(ledger_path),
            "audit_capsule_location_sha256": artifact_location_sha256(_capsule_path(ledger_path)),
            "holdout_capsule_location_sha256": artifact_location_sha256(
                _holdout_capsule_path(ledger_path)
            ),
            "evidence_location_sha256": "b" * 64,
            "consent_attestation_sha256": "c" * 64,
            "retention_attestation_sha256": "d" * 64,
            "zdr_or_residual_retention_acceptance_sha256": "e" * 64,
        }
    )


class FakeAssetLoader:
    def __init__(
        self,
        plans: tuple[SessionPlan, ...],
        no_speech_plans: tuple[NoSpeechWindowPlan, ...],
        *,
        order: list[str] | None = None,
    ) -> None:
        self.plans = plans
        self.no_speech_plans = no_speech_plans
        self.order = order
        self.authorizations = []

    def load(self, authorization):
        if self.order is not None:
            self.order.append("asset")
        self.authorizations.append(authorization)
        return QualificationAssets(self.plans, self.no_speech_plans)


def _asset_release(
    plans: tuple[SessionPlan, ...],
    no_speech_plans: tuple[NoSpeechWindowPlan, ...],
    preregistration: dict[str, object],
    campaign_envelope: dict[str, object],
    attempt_envelope: dict[str, object],
    *,
    split: str,
    order: list[str] | None = None,
) -> AuthorizedAssetRelease:
    immutable = preregistration["immutable_values"]
    campaign = campaign_envelope["payload"]
    attempt = attempt_envelope["payload"]
    assert isinstance(immutable, dict)
    assert isinstance(campaign, dict)
    assert isinstance(attempt, dict)
    schedule_sha256 = (
        immutable["development_schedule_sha256"]
        if split == "development"
        else compute_holdout_schedule_sha256(
            plans,
            no_speech_plans=no_speech_plans,
        )
    )
    payload = {
        "schema_id": "gate_0b_privacy_custody_authorization_v1",
        "campaign_id": campaign["campaign_id"],
        "authorization_id": campaign["authorization_id"],
        "attempt_id": attempt["attempt_id"],
        "split": split,
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "source_sha": SOURCE_SHA,
        "schedule_sha256": schedule_sha256,
        "corpus_sha256": immutable["corpus_sha256"],
        "project": immutable["project"],
        "model": immutable["model"],
        "consent_registry_sha256": immutable["consent_attestation_sha256"],
        "withdrawal_registry_sha256": "5" * 64,
        "purpose_attestation_sha256": "6" * 64,
        "rights_attestation_sha256": "7" * 64,
        "provider_disclosure_sha256": "8" * 64,
        "subject_set_sha256": "a" * 64,
        "retention_policy_sha256": immutable["retention_attestation_sha256"],
        "provider_retention_decision": "zdr_verified",
        "residual_retention_acceptance_sha256": immutable[
            "zdr_or_residual_retention_acceptance_sha256"
        ],
        "consent_active": True,
        "withdrawal_clear": True,
        "purpose_limited": True,
        "usage_rights_active": True,
        "provider_disclosures_current": True,
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=4)).isoformat(),
        "deletion_deadline": (NOW + timedelta(days=29)).isoformat(),
        "nonce": "privacy_nonce_1",
    }
    envelope = {
        "key_id": PRIVACY_KEY_ID,
        "payload": payload,
        "signature": base64.b64encode(
            PRIVACY_PRIVATE_KEY.sign(canonical_json_bytes(payload))
        ).decode("ascii"),
    }
    return AuthorizedAssetRelease(
        loader=FakeAssetLoader(plans, no_speech_plans, order=order),
        privacy_envelope=envelope,
        privacy_public_key=PRIVACY_PUBLIC_KEY,
    )


def _capsule_path(ledger_path: Path) -> Path:
    return ledger_path.with_name("gate-0b-capsule.json")


def _holdout_capsule_path(ledger_path: Path) -> Path:
    return ledger_path.with_name("gate-0b-holdout-capsule.json")


def test_capsule_handoff_requires_context_bound_durable_receipt(tmp_path: Path) -> None:
    custody = tmp_path / "capsule-custody"
    custody.mkdir(mode=0o700)
    capsule_path = custody / "capsule.json"

    capsule_sha256 = runner_module._handoff_sealed_capsule(
        capsule_path,
        {"sealed": "capsule"},
        sink=PrivateFileCapsuleSink(),
        campaign_id="campaign-1",
        attempt_id="attempt-1",
        split="development",
    )

    assert capsule_sha256 == sha256(canonical_json_bytes({"sealed": "capsule"})).hexdigest()
    assert capsule_path.read_bytes() == canonical_json_bytes({"sealed": "capsule"}) + b"\n"


@pytest.mark.parametrize(
    "sink,error",
    (
        (_WrongCapsuleReceiptSink(), "receipt"),
        (_ReceiptWithoutPersistenceSink(), "persisted"),
    ),
)
def test_capsule_handoff_rejects_false_acknowledgment(
    tmp_path: Path,
    sink: object,
    error: str,
) -> None:
    custody = tmp_path / error
    custody.mkdir(mode=0o700)

    with pytest.raises(ValueError, match=error):
        runner_module._handoff_sealed_capsule(
            custody / "capsule.json",
            {"sealed": "capsule"},
            sink=sink,
            campaign_id="campaign-1",
            attempt_id="attempt-1",
            split="development",
        )


def test_setup_and_connection_policy_are_exact_and_non_debuggable() -> None:
    setup = build_gate0b_setup_message(_config())
    tool_setup = build_gate0b_setup_message(_config(), include_tool=True)
    policy = ConnectionPolicy()

    assert set(setup) == {"setup"}
    assert setup["setup"]["model"] == "models/gemini-3.1-flash-live-preview"
    assert setup["setup"]["generationConfig"]["temperature"] == 0.4
    assert setup["setup"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "minimal"}
    assert setup["setup"]["inputAudioTranscription"] == {}
    assert setup["setup"]["outputAudioTranscription"] == {}
    assert setup["setup"]["realtimeInputConfig"] == {
        "automaticActivityDetection": {
            "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
            "endOfSpeechSensitivity": "END_SENSITIVITY_HIGH",
            "prefixPaddingMs": 100,
            "silenceDurationMs": 500,
        },
        "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
        "turnCoverage": "TURN_INCLUDES_ONLY_ACTIVITY",
    }
    assert "tools" not in setup["setup"]
    assert tool_setup["setup"]["tools"][0]["functionDeclarations"][0]["name"] == (
        "synthetic_lookup"
    )
    assert policy.proxy is None
    assert policy.follow_redirects is False
    assert policy.debug is False
    assert policy.crash_dump is False
    assert policy.tls_key_log is False


def test_injected_session_paces_audio_reduces_one_combined_event_and_discards_output() -> None:
    session = CausallySequencedFakeSession(
        [
            {"setupComplete": {}},
            _server_event(),
            _usage_message(),
            None,
        ],
        response_audio_counts=(1,),
    )
    connector = FakeConnector([session])
    sleeps = []

    async def sleep_ms(value):
        sleeps.append(value)

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=connector,
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 150, 160, 170]),
            sleep_ms=sleep_ms,
        )
    )

    assert result.complete is True
    assert result.error_code is None
    assert sleeps == [20, 60, 20]
    assert [event.kind for event in result.audit_events] == [
        CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
        CallerTurnEventKind.MODEL_OUTPUT_STARTED,
        CallerTurnEventKind.TURN_COMPLETE,
    ]
    assert result.output_audio_bytes == 4
    assert result.usage.input_audio_tokens == 8
    assert result.usage.output_audio_tokens == 4
    assert session.closed is True
    assert session.messages == []
    assert CANARY_SECRET not in repr(connector.requests[0])
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())
    assert base64.b64encode(b"\x01\x02\x03\x04").decode("ascii") not in json.dumps(
        result.redacted_report_dict()
    )


@pytest.mark.parametrize(
    ("initial_output_audio_bytes", "expected_complete", "expected_error"),
    (
        (runner_module.MAX_OUTPUT_AUDIO_PER_RUN_BYTES - 4, True, None),
        (
            runner_module.MAX_OUTPUT_AUDIO_PER_RUN_BYTES - 3,
            False,
            "run_output_audio_cap_exceeded",
        ),
    ),
)
def test_attempt_work_carries_prior_split_output_into_the_run_cap(
    initial_output_audio_bytes: int,
    expected_complete: bool,
    expected_error: str | None,
) -> None:
    session = CausallySequencedFakeSession(
        [
            {"setupComplete": {}},
            _server_event(),
            _usage_message(),
            None,
        ],
        response_audio_counts=(1,),
    )

    session_results, no_speech_results = asyncio.run(
        runner_module._execute_attempt_work(
            (_plan(),),
            no_speech_plans=(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 150, 160, 170]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(PRICING_PATH),
            run_cost_limit_microusd=10_000_000,
            initial_output_audio_bytes=initial_output_audio_bytes,
        )
    )

    assert no_speech_results == []
    assert session_results[0][1].complete is expected_complete
    assert session_results[0][1].error_code == expected_error
    if expected_error is not None:
        assert any(
            isinstance(message, dict) and "usageMetadata" in message
            for message in session.messages
        )


def test_attempt_work_enforces_carried_output_inside_no_speech_window() -> None:
    session = FakeSession(
        [
            {"setupComplete": {}},
            _server_event(),
            _usage_message(),
            None,
        ]
    )

    session_results, no_speech_results = asyncio.run(
        runner_module._execute_attempt_work(
            (),
            no_speech_plans=(_no_speech_plan(),),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(PRICING_PATH),
            run_cost_limit_microusd=10_000_000,
            initial_output_audio_bytes=(
                runner_module.MAX_OUTPUT_AUDIO_PER_RUN_BYTES - 3
            ),
        )
    )

    assert session_results == []
    assert no_speech_results[0][1].complete is False
    assert no_speech_results[0][1].error_code == "run_output_audio_cap_exceeded"
    assert any(
        isinstance(message, dict) and "usageMetadata" in message
        for message in session.messages
    )


def test_response_while_activity_end_is_still_pacing_fails_closed() -> None:
    class CoordinatedSession:
        def __init__(self) -> None:
            self.sent = []
            self.audio_sent = asyncio.Event()
            self.receive_index = 0
            self.closed = False

        async def send(self, message):
            self.sent.append(message)
            if (
                isinstance(message.get("realtimeInput"), dict)
                and "audio" in message["realtimeInput"]
            ):
                self.audio_sent.set()

        async def receive(self):
            self.receive_index += 1
            if self.receive_index == 1:
                return {"setupComplete": {}}
            if self.receive_index == 2:
                await self.audio_sent.wait()
                return _server_event()
            if self.receive_index == 3:
                return _usage_message()
            return None

        async def close(self):
            self.closed = True

    session = CoordinatedSession()
    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is False
    assert result.error_code == "premature_current_response"


def test_response_while_audio_send_is_blocked_is_not_credited_to_future_activity() -> None:
    class BlockedSendSession:
        def __init__(self) -> None:
            self.audio_send_started = asyncio.Event()
            self.release_audio_send = asyncio.Event()
            self.receive_index = 0
            self.closed = False

        async def send(self, message):
            if (
                isinstance(message.get("realtimeInput"), dict)
                and "audio" in message["realtimeInput"]
            ):
                self.audio_send_started.set()
                await self.release_audio_send.wait()

        async def receive(self):
            self.receive_index += 1
            if self.receive_index == 1:
                return {"setupComplete": {}}
            if self.receive_index == 2:
                await self.audio_send_started.wait()
                return _server_event()
            if self.receive_index == 3:
                self.release_audio_send.set()
                await asyncio.sleep(0)
                return _usage_message()
            return None

        async def close(self):
            self.closed = True

    session = BlockedSendSession()

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is False
    assert result.error_code in {"premature_current_response", "unattributed_response"}


def test_outbound_wire_facts_use_injected_actual_times_not_replay_schedule() -> None:
    session = FakeSession(
        [{"setupComplete": {}}, _server_event(), _usage_message(), None]
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=lambda _plan: ReceiptClock(
                [0, 10, 45, 110, 140, 200, 210, 220]
            ),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    outbound = {
        fact.kind: fact.at_ms
        for fact in result.wire_facts
        if fact.kind
        in {
            "caller_activity_start",
            "caller_audio_sent",
            "caller_speech_end",
            "caller_activity_end",
        }
    }
    assert result.complete is True
    assert outbound == {
        "caller_activity_start": 10,
        "caller_audio_sent": 45,
        "caller_speech_end": 110,
        "caller_activity_end": 140,
    }
    assert result.wire_observations[1].first_audio_ms == 90


def test_one_measurement_clock_preserves_latency_under_uniform_offset() -> None:
    def run(offset: int):
        values = [offset + value for value in (0, 10, 45, 110, 140, 200, 210, 220)]
        return asyncio.run(
            execute_injected_session(
                _plan(),
                config=_config(),
                connector=FakeConnector(
                    [
                        FakeSession(
                            [
                                {"setupComplete": {}},
                                _server_event(),
                                _usage_message(),
                                None,
                            ]
                        )
                    ]
                ),
                credential=SecretCredential(CANARY_SECRET),
                measurement_clock_factory=lambda _plan: ReceiptClock(values),
                sleep_ms=lambda _value: asyncio.sleep(0),
            )
        )

    baseline = run(0)
    shifted = run(1_900)

    assert baseline.complete is shifted.complete is True
    assert baseline.wire_observations[1].first_audio_ms == 90
    assert shifted.wire_observations[1].first_audio_ms == 90


def test_no_speech_receive_loop_is_live_while_paced_audio_is_still_sending() -> None:
    class CoordinatedSession:
        def __init__(self) -> None:
            self.sent = []
            self.audio_sent = asyncio.Event()
            self.response_received = asyncio.Event()
            self.receive_index = 0
            self.closed = False

        async def send(self, message):
            self.sent.append(message)
            if (
                isinstance(message.get("realtimeInput"), dict)
                and "audio" in message["realtimeInput"]
            ):
                self.audio_sent.set()

        async def receive(self):
            self.receive_index += 1
            if self.receive_index == 1:
                return {"setupComplete": {}}
            if self.receive_index == 2:
                await self.audio_sent.wait()
                self.response_received.set()
                return _server_event()
            if self.receive_index == 3:
                return _usage_message()
            return None

        async def close(self):
            self.closed = True

    plan = replace(
        _no_speech_plan(),
        replay_inputs=(
            Gate0BReplayInput(
                "audio",
                0,
                1,
                None,
                audio=b"\x00\x00" * 319,
                duration_ms=20,
            ),
            Gate0BReplayInput(
                "audio",
                80,
                1,
                None,
                audio=b"\x00\x00" * 319,
                duration_ms=20,
            ),
        ),
    )
    session = CoordinatedSession()
    receiver_was_live_during_send = False

    async def sleep_ms(value: int) -> None:
        nonlocal receiver_was_live_during_send
        if value == 80:
            await asyncio.wait_for(session.response_received.wait(), timeout=0.1)
            receiver_was_live_during_send = True
        else:
            await asyncio.sleep(0)

    result = asyncio.run(
        execute_injected_no_speech_window(
            plan,
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
                measurement_clock_factory=lambda _plan: ReceiptClock(
                    [0, 0, 120, 125, 130, 131]
                ),
            sleep_ms=sleep_ms,
        )
    )

    assert result.complete is True
    assert receiver_was_live_during_send is True


def test_multiple_official_usage_frames_use_latest_cumulative_snapshot() -> None:
    first = _server_event(text="book service today 1")
    second = _server_event(text="book service today 2")
    session = CausallySequencedFakeSession(
        [
            {"setupComplete": {}},
            first,
            _usage_message(),
            second,
            _usage_message(
                input_audio_tokens=12,
                input_text_tokens=3,
                output_audio_tokens=6,
                output_text_tokens=2,
            ),
            None,
        ],
        response_audio_counts=(1, 2),
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(two_activities=True),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130, 300, 310]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.usage.input_audio_tokens == 12
    assert result.usage.output_audio_tokens == 6


def test_decreasing_cumulative_usage_snapshot_fails_closed() -> None:
    first = _server_event(text="book service today 1")
    second = _server_event(text="book service today 2")
    session = CausallySequencedFakeSession(
        [
            {"setupComplete": {}},
            first,
            _usage_message(input_audio_tokens=12, output_audio_tokens=6),
            second,
            _usage_message(input_audio_tokens=8, output_audio_tokens=4),
            None,
        ],
        response_audio_counts=(1, 2),
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(two_activities=True),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130, 300, 310]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is False
    assert result.error_code == "usage_metadata_inconsistent"


def test_automatic_vad_schedule_keeps_activity_markers_local_and_sends_only_pcm() -> None:
    session = FakeSession([{"setupComplete": {}}, _usage_message(), None])
    connector = FakeConnector([session])
    asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=connector,
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert len(session.sent) == 2
    assert session.sent[1]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
    assert base64.b64decode(session.sent[1]["realtimeInput"]["audio"]["data"]) == (
        b"\x00\x00" * 319
    )


def test_synthetic_tool_calls_receive_synchronous_payload_free_responses() -> None:
    session = FakeSession(
        [
            {"setupComplete": {}},
            {
                "toolCall": {
                    "functionCalls": [
                        {
                            "id": "tool_1",
                            "name": "synthetic_lookup",
                            "args": {"private": "must-not-return"},
                        }
                    ]
                }
            },
            _usage_message(),
            None,
        ]
    )
    result = asyncio.run(
        execute_injected_session(
            _tool_interaction_plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130, 140]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    response = next(message for message in session.sent if "toolResponse" in message)
    assert response == {
        "toolResponse": {
            "functionResponses": [
                {
                    "id": "tool_1",
                    "name": "synthetic_lookup",
                    "response": {"result": "synthetic_ok"},
                }
            ]
        }
    }
    assert "must-not-return" not in json.dumps(response)
    assert result.complete is True


def test_synchronous_tool_response_precedes_the_next_caller_activity() -> None:
    session = FakeSession(
        [
            {"setupComplete": {}},
            {
                "toolCall": {
                    "functionCalls": [{"id": "tool_1", "name": "synthetic_lookup", "args": {}}]
                }
            },
            _usage_message(),
            None,
        ]
    )

    result = asyncio.run(
        execute_injected_session(
            _tool_interaction_plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 300]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    tool_index = next(index for index, value in enumerate(session.sent) if "toolResponse" in value)
    audio_inputs = [
        index
        for index, value in enumerate(session.sent)
        if isinstance(value.get("realtimeInput"), dict) and "audio" in value["realtimeInput"]
    ]
    assert result.complete is True
    assert tool_index < audio_inputs[1]


def test_combined_tool_cancellation_and_interruption_satisfies_both_markers() -> None:
    first = {
        "serverContent": {
            "inputTranscription": {"text": "book service today 1"},
        },
        "toolCall": {
            "functionCalls": [
                {"id": "tool_1", "name": "synthetic_lookup", "args": {}}
            ]
        },
    }
    second = {
        "serverContent": {
            "inputTranscription": {"text": "book service today 2"},
            "interrupted": True,
        },
        "toolCallCancellation": {"ids": ["tool_1"]},
    }
    session = CausallySequencedFakeSession(
        [
            {"setupComplete": {}},
            first,
            second,
            _usage_message(),
            None,
        ],
        response_audio_counts=(1, 2),
    )

    result = asyncio.run(
        execute_injected_session(
            _cancellation_interaction_plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 260, 300]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert [event.kind for event in result.audit_events] == [
        CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
        CallerTurnEventKind.TOOL_CALL_STARTED,
        CallerTurnEventKind.INPUT_TRANSCRIPT_FRAGMENT,
        CallerTurnEventKind.INTERRUPTED,
        CallerTurnEventKind.TOOL_CALL_CANCELLED,
    ]
    assert not any("toolResponse" in value for value in session.sent)
    audio_inputs = [
        value
        for value in session.sent
        if isinstance(value.get("realtimeInput"), dict) and "audio" in value["realtimeInput"]
    ]
    assert len(audio_inputs) == 2

    capsule = runner_module._build_audit_capsule(
        campaign_id="campaign_001",
        policy_ms=100,
        source_fact_bundle_sha256="5" * 64,
        execution_started_at=NOW,
        execution_completed_at=NOW + timedelta(seconds=1),
        provider_revision=None,
        runtime_identity_before_sha256=TEST_EXECUTION_IDENTITY_SHA256,
        runtime_identity_after_sha256=TEST_EXECUTION_IDENTITY_SHA256,
        runtime_identity_before=TEST_EXECUTION_IDENTITY_REPORT,
        runtime_identity_after=TEST_EXECUTION_IDENTITY_REPORT,
        session_results=((_cancellation_interaction_plan(), result),),
        no_speech_results=(),
    )
    records, no_speech_records = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(100,),
        commitment_key=b"c" * 32,
    )

    assert no_speech_records == ()
    assert [record.observed_lifecycle_status for record in records] == [
        "retrospective_complete",
        "retrospective_complete",
    ]
    assert all(record.assembled_turn_count == 1 for record in records)
    assert all(record.contamination_count == 0 for record in records)
    assert all(record.duplicate_count == 0 for record in records)
    assert all(record.stale_count == 0 for record in records)


@pytest.mark.parametrize(
    ("message", "error_code"),
    (
        ({"serverContent": {"inputTranscription": {"text": 7}}}, "malformed_message"),
        ({"oversized": "x" * (64 * 1024)}, "message_too_large"),
        ({"goAway": {"timeLeft": "1s"}}, "provider_goaway"),
    ),
)
def test_malformed_oversized_and_goaway_messages_fail_with_bounded_codes(
    message,
    error_code,
) -> None:
    session = FakeSession([{"setupComplete": {}}, message, None])

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is False
    assert result.error_code == error_code
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())


def test_missing_or_inconsistent_usage_metadata_fails_closed() -> None:
    missing = FakeSession([{"setupComplete": {}}, _server_event(), None])
    inconsistent_usage = _usage_message()
    inconsistent_usage["usageMetadata"]["promptTokenCount"] = 99
    inconsistent = FakeSession([{"setupComplete": {}}, _server_event(), inconsistent_usage, None])

    first = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([missing]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )
    second = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([inconsistent]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130, 140]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert first.error_code == "usage_metadata_missing"
    assert second.error_code == "usage_metadata_inconsistent"


def test_session_timeout_after_connect_is_bounded_counted_and_closes() -> None:
    session = FakeSession([{"setupComplete": {}}])

    async def expire(_milliseconds: int) -> None:
        raise TimeoutError

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0]),
            sleep_ms=expire,
        )
    )

    assert result.error_code == "session_timeout"
    assert result.provider_request_count == 1
    assert result.epoch_count == 1
    assert session.closed is True
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())


def test_dual_reducer_disagreement_stops_before_audit_handoff() -> None:
    session = FakeSession([{"setupComplete": {}}, _server_event(), _usage_message(), None])

    def disagree(*_args, **_kwargs):
        return ReductionResult(status="decoded", events=(), rejection_code=None)

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130, 140]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            secondary_reducer=disagree,
        )
    )

    assert result.complete is False
    assert result.error_code == "reducer_disagreement"
    assert result.audit_events == ()


def test_response_received_after_actual_activity_end_is_not_backdated() -> None:
    session = FakeSession([{"setupComplete": {}}, _server_event(), _usage_message(), None])

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 50, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.error_code is None
    assert result.wire_observations[1].premature_current_audio_count == 0


def test_first_audio_latency_uses_speech_end_before_trailing_silence() -> None:
    base = _plan()
    plan = replace(
        base,
        activities=(replace(base.activities[0], speech_end_at_ms=80),),
    )
    session = FakeSession(
        [{"setupComplete": {}}, _server_event(), _usage_message(), None]
    )

    result = asyncio.run(
        execute_injected_session(
            plan,
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.wire_observations[1].first_audio_ms == 40
    assert result.wire_observations[1].premature_current_audio_count == 0


def test_open_prior_response_remains_prior_during_next_activity() -> None:
    first_audio = _server_event(text="first", terminal=False)
    first_audio["serverContent"].pop("inputTranscription")
    second_audio = _server_event(text="second", terminal=False)
    second_audio["serverContent"].pop("inputTranscription")
    interrupted = {"serverContent": {"interrupted": True}}
    current = _server_event(text="second", terminal=True)
    session = CausallySequencedFakeSession(
        [
            {"setupComplete": {}},
            first_audio,
            second_audio,
            interrupted,
            current,
            _usage_message(),
            None,
        ],
        response_audio_counts=(1, 2, 2, 2),
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(two_activities=True),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 200, 220, 300, 310, 320]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    activity_start = next(
        fact.at_ms
        for fact in result.wire_facts
        if fact.kind == "caller_audio_sent" and fact.activity_ordinal == 2
    )
    prior_response_tail = max(
        fact.at_ms
        for fact in result.wire_facts
        if fact.kind == "audio_received" and fact.response_ordinal == 1
    )
    assert result.wire_observations[2].interruption_tail_ms == (
        prior_response_tail - activity_start
    )
    assert result.wire_observations[2].premature_current_audio_count == 0


def test_fresh_restart_uses_new_connection_and_epoch_without_context_restoration() -> None:
    first_event = _server_event(text="first epoch")
    second_event = _server_event(text="second epoch")
    first = FakeSession([{"setupComplete": {}}, first_event, _usage_message(), None])
    second = FakeSession([{"setupComplete": {}}, second_event, _usage_message(), None])
    connector = FakeConnector([first, second])

    result = asyncio.run(
        execute_injected_session(
            _restart_plan(),
            config=_config(),
            connector=connector,
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130, 700, 820, 830]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.provider_request_count == 2
    assert result.epoch_count == 2
    assert [request.epoch for request in connector.requests] == [1, 2]
    assert {event.epoch for event in result.audit_events} == {1, 2}
    assert [event.kind for event in result.audit_events].count(
        CallerTurnEventKind.RECONNECT_STARTED
    ) == 1
    reconnect = next(
        event
        for event in result.audit_events
        if event.kind is CallerTurnEventKind.RECONNECT_STARTED
    )
    second_open = next(
        fact
        for fact in result.wire_facts
        if fact.kind == "connection_open" and fact.epoch == 2
    )
    assert (reconnect.at_ms, reconnect.epoch) == (second_open.at_ms, 2)
    assert first.sent[0] == second.sent[0] == build_gate0b_setup_message(_config())
    assert all("sessionResumption" not in json.dumps(message) for message in second.sent)
    assert "reconnect_started" not in json.dumps(second.sent)

    capsule = runner_module._build_audit_capsule(
        campaign_id="campaign_001",
        policy_ms=250,
        source_fact_bundle_sha256="5" * 64,
        execution_started_at=NOW,
        execution_completed_at=NOW + timedelta(seconds=1),
        provider_revision=None,
        runtime_identity_before_sha256=TEST_EXECUTION_IDENTITY_SHA256,
        runtime_identity_after_sha256=TEST_EXECUTION_IDENTITY_SHA256,
        runtime_identity_before=TEST_EXECUTION_IDENTITY_REPORT,
        runtime_identity_after=TEST_EXECUTION_IDENTITY_REPORT,
        session_results=((_restart_plan(), result),),
        no_speech_results=(),
    )
    records, no_speech_records = derive_primitive_records_from_capsule(
        capsule,
        policies_ms=(250,),
        commitment_key=b"c" * 32,
    )

    assert no_speech_records == ()
    assert [record.activity_ordinal for record in records] == [1, 2]
    assert all(record.assembled_turn_count == 1 for record in records)
    assert all(record.stale_count == 0 for record in records)
    assert [record.observed_lifecycle_status for record in records] == [
        "retrospective_complete",
        "retrospective_complete",
    ]


def test_fresh_restart_drains_each_persistent_connection_segment() -> None:
    first = HangingAfterMessagesSession(
        [{"setupComplete": {}}, _server_event(text="first epoch"), _usage_message()]
    )
    second = HangingAfterMessagesSession(
        [{"setupComplete": {}}, _server_event(text="second epoch"), _usage_message()]
    )
    connector = FakeConnector([first, second])

    result = asyncio.run(
        execute_injected_session(
            _restart_plan(),
            config=replace(
                _config(),
                session_timeout_seconds=10,
            ),
            connector=connector,
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=lambda _plan: ReceiptClock(
                [
                    0,
                    0,
                    20,
                    80,
                    100,
                    120,
                    130,
                    3_130,
                    3_130,
                    3_200,
                    3_220,
                    3_280,
                    3_300,
                    3_320,
                    3_330,
                    6_330,
                ]
            ),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.provider_request_count == 2
    assert result.epoch_count == 2
    assert [request.epoch for request in connector.requests] == [1, 2]
    assert first.closed is True
    assert second.closed is True


def test_generation_complete_does_not_close_the_response_before_turn_complete() -> None:
    audio = _server_event(terminal=False)
    generation_complete = {"serverContent": {"generationComplete": True}}
    turn_complete = {"serverContent": {"turnComplete": True}}
    session = FakeSession(
        [
            {"setupComplete": {}},
            audio,
            generation_complete,
            turn_complete,
            _usage_message(),
            None,
        ]
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130, 140, 150, 160]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert [event.kind for event in result.audit_events][-2:] == [
        CallerTurnEventKind.GENERATION_COMPLETE,
        CallerTurnEventKind.TURN_COMPLETE,
    ]


def test_abnormal_close_is_reduced_to_bounded_code_without_exception_text() -> None:
    audio = _server_event(terminal=False)
    audio["serverContent"].pop("inputTranscription")
    session = FakeSession(
        [
            {"setupComplete": {}},
            audio,
            ProviderSessionClosed("private close reason " + CANARY_SECRET),
        ]
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.error_code == "provider_closed"
    assert result.wire_observations[1].abnormal_close_count == 1
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())


def test_clean_close_before_standard_sender_finishes_fails_and_cancels_input() -> None:
    session = FakeSession([{"setupComplete": {}}, _usage_message(), None])

    async def lagged_sleep(_value: int) -> None:
        for _ in range(100):
            await asyncio.sleep(0)

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=lambda _plan: ReceiptClock(range(1_000)),
            sleep_ms=lagged_sleep,
        )
    )

    assert result.complete is False
    assert result.error_code == "provider_closed"
    close = next(fact for fact in result.wire_facts if fact.kind == "stream_closed")
    usage = next(fact for fact in result.wire_facts if fact.kind == "usage_received")
    assert close.at_ms > usage.at_ms
    assert all(
        fact.at_ms <= close.at_ms
        for fact in result.wire_facts
        if fact.kind.startswith("caller_")
    )
    assert session.closed is True


def test_audio_after_turn_terminal_is_rejected_and_counted() -> None:
    terminal = _server_event()
    late = _server_event(terminal=False)
    late["serverContent"].pop("inputTranscription")
    session = FakeSession([{"setupComplete": {}}, terminal, late, _usage_message(), None])

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.error_code == "audio_after_terminal"
    assert result.wire_observations[1].audio_after_terminal_count == 1


def test_duplicate_response_terminal_is_recorded_as_malformed_and_rejected() -> None:
    session = FakeSession(
        [
            {"setupComplete": {}},
            _server_event(),
            {"serverContent": {"turnComplete": True}},
            _usage_message(),
            None,
        ]
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130, 140]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is False
    assert result.error_code == "malformed_message"
    assert result.wire_observations[1].malformed_count == 1
    assert [fact.kind for fact in result.wire_facts].count("malformed") == 1


def test_decreasing_measurement_clock_fails_closed() -> None:
    session = FakeSession(
        [{"setupComplete": {}}, _server_event(), _usage_message(), None]
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=lambda _plan: ReceiptClock(
                [0, 0, 20, 80, 100, 120, 110]
            ),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is False
    assert result.error_code == "measurement_clock_invalid"


def test_usage_metadata_does_not_hide_delayed_audio_after_terminal() -> None:
    late = _server_event(terminal=False)
    late["serverContent"].pop("inputTranscription")
    session = YieldingFakeSession(
        [
            {"setupComplete": {}},
            _server_event(),
            _usage_message(),
            late,
            None,
        ]
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130, 140]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is False
    assert result.error_code == "audio_after_terminal"
    assert result.wire_observations[1].audio_after_terminal_count == 1


def test_post_completion_observation_catches_audio_arriving_after_600_ms() -> None:
    late = _server_event(terminal=False)
    late["serverContent"].pop("inputTranscription")
    session = DelayedMessageSession(
        [
            {"setupComplete": {}},
            _server_event(),
            _usage_message(),
            late,
            None,
        ],
        delayed_index=3,
        delay_seconds=0.6,
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130, 730]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is False
    assert result.error_code == "audio_after_terminal"
    assert result.wire_observations[1].audio_after_terminal_count == 1


def test_no_speech_usage_metadata_does_not_hide_delayed_activation() -> None:
    session = YieldingFakeSession(
        [
            {"setupComplete": {}},
            _usage_message(),
            _server_event(),
            None,
        ]
    )

    result = asyncio.run(
        execute_injected_no_speech_window(
            _no_speech_plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.false_activity_count == 1
    assert result.model_audio_chunk_count == 1


def test_usage_completion_uses_bounded_quiet_drain_when_stream_stays_open() -> None:
    session = HangingAfterMessagesSession(
        [
            {"setupComplete": {}},
            _usage_message(),
        ]
    )

    result = asyncio.run(
        execute_injected_no_speech_window(
            _no_speech_plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 3_120]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.error_code is None
    assert session.closed is True
    observation = next(
        fact for fact in result.wire_facts if fact.kind == "observation_complete"
    )
    usage = next(fact for fact in result.wire_facts if fact.kind == "usage_received")
    assert observation.at_ms - usage.at_ms == 3_000


def test_no_speech_identity_failure_precedes_budget_and_connector_factory() -> None:
    budget = runner_module._RequestBudget(limit=1)
    factory_calls = 0

    def factory(_credential: SecretCredential):
        nonlocal factory_calls
        factory_calls += 1
        return FakeConnector([])

    def guard() -> None:
        raise IdentityError("identity recapture failed")

    connector = runner_module._ReservedConnector(
        budget=budget,
        credential=SecretCredential(CANARY_SECRET),
        factory=factory,
        identity_guard=guard,
    )

    result = asyncio.run(
        execute_injected_no_speech_window(
            _no_speech_plan(),
            config=_config(),
            connector=connector,
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is False
    assert result.error_code == "source_identity_failed"
    assert result.provider_request_count == 0
    assert budget.consumed == 0
    assert factory_calls == 0


def test_clean_close_before_no_speech_sender_finishes_fails_and_cancels_input() -> None:
    session = FakeSession([{"setupComplete": {}}, _usage_message(), None])
    plan = replace(
        _no_speech_plan(),
        replay_inputs=(
            replace(_no_speech_plan().replay_inputs[0], at_ms=20),
        ),
    )

    async def lagged_sleep(_value: int) -> None:
        for _ in range(100):
            await asyncio.sleep(0)

    result = asyncio.run(
        execute_injected_no_speech_window(
            plan,
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=lambda _plan: ReceiptClock(range(1_000)),
            sleep_ms=lagged_sleep,
        )
    )

    assert result.complete is False
    assert result.error_code == "provider_closed"
    close = next(fact for fact in result.wire_facts if fact.kind == "stream_closed")
    usage = next(fact for fact in result.wire_facts if fact.kind == "usage_received")
    assert close.at_ms > usage.at_ms
    assert all(
        fact.at_ms <= close.at_ms
        for fact in result.wire_facts
        if fact.kind == "caller_audio_sent"
    )
    assert session.closed is True


def test_no_speech_window_records_false_activation_without_computing_verdict() -> None:
    session = FakeSession([{"setupComplete": {}}, _server_event(), _usage_message(), None])

    result = asyncio.run(
        execute_injected_no_speech_window(
            _no_speech_plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.false_activity_count == 1
    assert result.model_audio_chunk_count == 1
    assert result.output_audio_bytes == 4
    assert [fact.kind for fact in result.wire_facts] == [
        "connection_open",
        "caller_audio_sent",
        "false_activity",
        "response_open",
        "audio_received",
        "response_terminal",
        "usage_received",
        "stream_closed",
        "teardown_complete",
    ]
    assert "passed" not in result.redacted_report_dict()
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())


def test_authorized_attempt_claims_before_secret_and_hands_off_encrypted_capsule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    custodian, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    order: list[str] = []
    plans, no_speech_plans = _development_schedule()
    sessions = _successful_sessions_for_plans(plans)
    no_speech_sessions = [
        FakeSession([{"setupComplete": {}}, _usage_message(), None])
        for _ in no_speech_plans
    ]
    connector = FakeConnector([*sessions, *no_speech_sessions])
    capsule_path = _capsule_path(ledger_path)

    ledger = FakeCustodyLedger(
        ledger_path,
        order=order,
        campaign_envelope=campaign,
    )

    def source_identity_check(*, expected_source_sha: str) -> CapturedExecutionIdentity:
        assert expected_source_sha == SOURCE_SHA
        order.append("source")
        return CapturedExecutionIdentity(
            report=TEST_EXECUTION_IDENTITY_REPORT,
            sha256=TEST_EXECUTION_IDENTITY_SHA256,
        )

    monkeypatch.setattr(
        runner_module,
        "_capture_current_execution_identity",
        source_identity_check,
    )

    def credential_loader(reference: str) -> SecretCredential:
        order.append("credential:" + reference)
        return SecretCredential(CANARY_SECRET)

    def connector_factory(_credential: SecretCredential):
        order.append("connector")
        return connector

    monkeypatch.setattr(
        runner_module,
        "_elapsed_clock_ns",
        ReceiptClock([0, 1_500_000_000]),
    )
    result = asyncio.run(
        execute_authorized_attempt(
            _asset_release(
                plans,
                no_speech_plans,
                preregistration,
                campaign,
                attempt,
                split="development",
                order=order,
            ),
            preregistration=preregistration,
            config=AuthorizedAttemptConfig(
                preregistration_sha256=preregistration["preregistration_sha256"],
                source_sha=SOURCE_SHA,
                approval_key_id=KEY_ID,
                credential_reference="qualification_secret_v1",
                policy_ms=250,
                whole_run_timeout_seconds=30,
            ),
            session_config=_config(),
            campaign_envelope=campaign,
            attempt_envelope=attempt,
            ledger=ledger,
            ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
            now=NOW,
            credential_loader=credential_loader,
            connector_factory=connector_factory,
            measurement_clock_factory=_default_measurement_clock_factory,
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_path=capsule_path,
            capsule_sink=PrivateFileCapsuleSink(),
        )
    )

    assert result.complete is True
    assert result.capsule_handed_off is True
    assert result.provider_request_count == 64
    assert result.cost_microusd == 4_992
    assert stat.S_IMODE(capsule_path.stat().st_mode) == 0o600
    assert order[:7] == [
        "source",
        "asset",
        "export_snapshot",
        "claim",
        "export_snapshot",
        "source",
        "credential:qualification_secret_v1",
    ]
    assert order[7:-3] == ["source", "connector"] * 64
    assert order[-3:] == ["source", "development_checkpoint", "export_snapshot"]
    envelope = json.loads(capsule_path.read_bytes())
    opened = open_audit_capsule(
        envelope,
        custodian_private_key=custodian,
        expected_key_id="audit_custodian_1",
    )
    assert opened["schema_id"] == "gate_0b_audit_capsule_v7"
    assert opened["source_fact_bundle_sha256"] == "5" * 64
    assert opened["runtime_identity_before_sha256"] == TEST_EXECUTION_IDENTITY_SHA256
    assert opened["runtime_identity_after_sha256"] == TEST_EXECUTION_IDENTITY_SHA256
    assert opened["runtime_identity_before"] == TEST_EXECUTION_IDENTITY_REPORT
    assert opened["runtime_identity_after"] == TEST_EXECUTION_IDENTITY_REPORT
    assert opened["activities"][0]["reference_text"] == "purpose recorded phrase 0"
    assert [
        fact["kind"] for fact in opened["no_speech_windows"][0]["wire_facts"]
    ] == [
        "connection_open",
        "caller_audio_sent",
        "usage_received",
        "stream_closed",
        "teardown_complete",
    ]
    usage, failures = derive_audit_capsule_accounting(opened)
    assert usage["provider_requests"] == 64
    assert usage["input_audio_seconds"] == 4
    assert usage["output_audio_seconds"] == 1
    assert len(opened["accounting"]["units"]) == 56
    assert failures == ()
    assert CANARY_SECRET not in json.dumps(envelope)
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())
    assert not hasattr(result, "audit_events")
    assert [name for name, _ in ledger.calls] == [
        "export_snapshot",
        "claim",
        "export_snapshot",
        "development_checkpoint",
        "export_snapshot",
    ]
    checkpoint = ledger.calls[-2][1]
    assert checkpoint["actual_cost_microusd"] == 4_992
    assert checkpoint["input_audio_duration_ms"] == sum(
        replay_input.duration_ms
        for plan in (*plans, *no_speech_plans)
        for replay_input in plan.replay_inputs
        if replay_input.kind == "audio"
    )
    assert checkpoint["output_audio_bytes"] == sum(
        unit["output_audio_bytes"] for unit in opened["accounting"]["units"]
    )
    assert checkpoint["elapsed_ms"] == 1_500
    assert checkpoint["usage_evidence_sha256"] == usage_evidence_sha256(
        usage,
        provider_requests=64,
        cost_microusd=4_992,
    )
    assert (
        checkpoint["development_capsule_sha256"]
        == sha256(capsule_path.read_bytes().rstrip(b"\n")).hexdigest()
    )


def test_identity_drift_blocks_reconnect_before_budget_or_connector_factory() -> None:
    session = FakeSession([])
    underlying = FakeConnector([session, session])
    budget = runner_module._RequestBudget(limit=2)
    guard_calls = 0
    factory_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            raise runner_module.ExecutionIdentityDriftError(
                "execution environment identity drifted"
            )

    def factory(_credential: SecretCredential):
        nonlocal factory_calls
        factory_calls += 1
        return underlying

    connector = runner_module._ReservedConnector(
        budget=budget,
        credential=SecretCredential(CANARY_SECRET),
        factory=factory,
        identity_guard=guard,
    )
    request = runner_module.InjectedConnectionRequest(
        endpoint=OFFICIAL_ENDPOINT,
        project="kevin-qualification-test",
        credential=SecretCredential(CANARY_SECRET),
        policy=ConnectionPolicy(),
        epoch=1,
    )

    assert asyncio.run(connector.connect(request)) is session
    with pytest.raises(
        runner_module.ExecutionIdentityDriftError,
        match="identity drifted",
    ):
        asyncio.run(connector.connect(replace(request, epoch=2)))

    assert guard_calls == 2
    assert budget.consumed == 1
    assert factory_calls == 1
    assert len(underlying.requests) == 1


def test_identity_capture_error_on_restart_is_source_identity_failure() -> None:
    first = FakeSession(
        [
            {"setupComplete": {}},
            _server_event(text="first epoch"),
            _usage_message(),
            None,
        ]
    )
    underlying = FakeConnector([first])
    budget = runner_module._RequestBudget(limit=2)
    guard_calls = 0
    factory_calls = 0

    def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            raise IdentityError("identity recapture failed")

    def factory(_credential: SecretCredential):
        nonlocal factory_calls
        factory_calls += 1
        return underlying

    connector = runner_module._ReservedConnector(
        budget=budget,
        credential=SecretCredential(CANARY_SECRET),
        factory=factory,
        identity_guard=guard,
    )

    result = asyncio.run(
        execute_injected_session(
            _restart_plan(),
            config=_config(),
            connector=connector,
            credential=SecretCredential(CANARY_SECRET),
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is False
    assert result.error_code == "source_identity_failed"
    assert guard_calls == 2
    assert budget.consumed == 1
    assert factory_calls == 1
    assert len(underlying.requests) == 1


def test_postclaim_identity_drift_blocks_before_secret_or_connector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    plans, no_speech_plans = _development_schedule()
    ledger = FakeCustodyLedger(ledger_path, campaign_envelope=campaign)
    touched: list[str] = []
    monkeypatch.setattr(
        runner_module,
        "_load_pinned_approval_public_key",
        lambda _identity: public,
    )
    monkeypatch.setattr(
        runner_module,
        "_require_unchanged_execution_identity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            runner_module.ExecutionIdentityDriftError(
                "execution environment identity drifted"
            )
        ),
    )

    result = asyncio.run(
        execute_authorized_attempt(
            _asset_release(
                plans,
                no_speech_plans,
                preregistration,
                campaign,
                attempt,
                split="development",
            ),
            preregistration=preregistration,
            config=AuthorizedAttemptConfig(
                preregistration_sha256=preregistration["preregistration_sha256"],
                source_sha=SOURCE_SHA,
                approval_key_id=KEY_ID,
                credential_reference="qualification_secret_v1",
                policy_ms=250,
                whole_run_timeout_seconds=30,
            ),
            session_config=_config(),
            campaign_envelope=campaign,
            attempt_envelope=attempt,
            ledger=ledger,
            ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
            now=NOW,
            credential_loader=lambda _reference: touched.append("credential"),
            connector_factory=lambda _credential: touched.append("connector"),
            measurement_clock_factory=lambda _plan: ReceiptClock([]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(PRICING_PATH),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_path=_capsule_path(ledger_path),
            capsule_sink=PrivateFileCapsuleSink(),
        )
    )

    assert result.complete is False
    assert result.error_code == "source_identity_failed"
    assert result.provider_request_count == 0
    assert touched == []
    assert [name for name, _ in ledger.calls] == [
        "export_snapshot",
        "claim",
        "export_snapshot",
        "terminal_outcome",
        "export_snapshot",
    ]


@pytest.mark.parametrize(
    "resume_mode",
    (
        "durable",
        "stale_snapshot",
        "substituted_lease",
        "wrong_claim_time",
        "stale_privacy",
        "input_exact",
        "input_one_over",
        "output_exhausted",
        "output_one_over",
        "elapsed_exhausted",
    ),
)
def test_holdout_resumes_only_after_signed_one_shot_claim_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume_mode: str,
) -> None:
    approval_private, approval_public = _key_pair()
    audit_private, audit_public = _custodian_key_pair()
    ledger_public = LEDGER_PUBLIC_KEY
    ledger_public_sha = sha256(ledger_public).hexdigest()
    ledger_path = tmp_path / "attempt-ledger.json"
    preregistration = _preregistration(
        approval_public,
        ledger_path,
        audit_public,
        ledger_custodian_public_key_sha256=ledger_public_sha,
    )
    campaign_envelope, attempt_envelope = _approval_envelopes(
        approval_private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
        ledger_custodian_public_key_sha256=ledger_public_sha,
    )
    plans, no_speech_plans = _holdout_schedule()
    holdout_manifest_sha256 = compute_holdout_schedule_sha256(
        plans,
        no_speech_plans=no_speech_plans,
    )
    state = CustodyLedgerState(
        campaign_id="campaign_001",
        authorization_id="authorization_001",
        preregistration_sha256=preregistration["preregistration_sha256"],
        source_sha=SOURCE_SHA,
        ledger_location_sha256=ledger_location_sha256(ledger_path),
        phase="holdout_collection",
        phase_history=(
            "preregistered",
            "development_collection",
            "policy_selection_locked",
            "holdout_collection",
        ),
        attempt_ids=("attempt_001",),
        active_attempt_id="attempt_001",
        completed_attempt_id=None,
        campaign_approval_sha256=sha256(
            canonical_json_bytes(campaign_envelope["payload"])
        ).hexdigest(),
        attempt_authorization_sha256=sha256(
            canonical_json_bytes(attempt_envelope["payload"])
        ).hexdigest(),
        attempt_claimed_at=NOW,
        lease_id_sha256=LEASE_ID_SHA256,
        provider_requests_reserved=128,
        cost_reserved_microusd=10_000_000,
        selected_policy_ms=100,
        policy_lock_sha256="1" * 64,
        development_capsule_sha256="2" * 64,
        development_ledger_head_sha256="3" * 64,
        holdout_manifest_sha256=holdout_manifest_sha256,
        holdout_execution_claimed=False,
        holdout_execution_claimed_at=None,
        holdout_capsule_sha256=None,
        development_usage_evidence_sha256="4" * 64,
        final_usage_evidence_sha256=None,
        development_provider_requests=64,
        development_cost_microusd=4_992,
        development_input_audio_ms=2_560,
        development_output_audio_bytes=256,
        development_elapsed_ms=1_000,
        actual_provider_requests=0,
        actual_cost_microusd=0,
        campaign_max_attempts=3,
        campaign_max_provider_requests=384,
        campaign_max_cost_microusd=30_000_000,
        record_sha256s=("1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64),
        record_events=(
            "genesis",
            "claim",
            "development_checkpoint",
            "policy_lock",
            "holdout_release",
        ),
        final_ledger_head_sha256="5" * 64,
    )
    holdout_input_audio_ms = sum(
        replay_input.duration_ms
        for plan in (*plans, *no_speech_plans)
        for replay_input in plan.replay_inputs
        if replay_input.kind == "audio"
    )
    if resume_mode == "input_exact":
        state = replace(
            state,
            development_input_audio_ms=(
                runner_module.MAX_INPUT_AUDIO_PER_RUN_MS - holdout_input_audio_ms
            ),
        )
    elif resume_mode == "input_one_over":
        state = replace(
            state,
            development_input_audio_ms=(
                runner_module.MAX_INPUT_AUDIO_PER_RUN_MS - holdout_input_audio_ms + 1
            ),
        )
    elif resume_mode in {"output_exhausted", "output_one_over"}:
        state = replace(
            state,
            development_output_audio_bytes=(
                runner_module.MAX_OUTPUT_AUDIO_PER_RUN_BYTES
                + (resume_mode == "output_one_over")
            ),
        )
    elif resume_mode == "elapsed_exhausted":
        state = replace(state, development_elapsed_ms=30_000)
    monkeypatch.setattr(
        runner_module,
        "_load_pinned_approval_public_key",
        lambda _identity: approval_public,
    )
    order: list[str] = []
    ledger = FakeCustodyLedger(
        ledger_path,
        order=order,
        public_key_sha256=ledger_public_sha,
        initial_state=state,
    )
    if resume_mode == "stale_snapshot":
        monkeypatch.setattr(
            runner_module,
            "validate_custody_ledger_snapshot",
            lambda *_args, **_kwargs: state,
        )
    elif resume_mode == "substituted_lease":
        original_resume = ledger.resume_holdout

        def resume_with_substituted_lease(**values):
            return replace(original_resume(**values), lease_id="8" * 64)

        monkeypatch.setattr(ledger, "resume_holdout", resume_with_substituted_lease)
    elif resume_mode == "wrong_claim_time":
        original_resume = ledger.resume_holdout

        def resume_with_wrong_claim_time(**values):
            claim = original_resume(**values)
            assert ledger._state is not None
            ledger._state = replace(
                ledger._state,
                holdout_execution_claimed_at=NOW - timedelta(seconds=1),
            )
            return claim

        monkeypatch.setattr(ledger, "resume_holdout", resume_with_wrong_claim_time)
    sessions = _successful_sessions_for_plans(plans)
    no_speech_sessions = [
        FakeSession([{"setupComplete": {}}, _usage_message(), None])
        for _ in no_speech_plans
    ]
    connector = FakeConnector([*sessions, *no_speech_sessions])
    capsule_path = _holdout_capsule_path(ledger_path)

    def credential_loader(reference: str) -> SecretCredential:
        order.append("credential:" + reference)
        return SecretCredential(CANARY_SECRET)

    release = _asset_release(
        plans,
        no_speech_plans,
        preregistration,
        campaign_envelope,
        attempt_envelope,
        split="holdout",
        order=order,
    )
    if resume_mode == "stale_privacy":
        stale_envelope = deepcopy(release.privacy_envelope)
        stale_payload = stale_envelope["payload"]
        assert isinstance(stale_payload, dict)
        stale_payload["issued_at"] = (NOW - timedelta(minutes=6)).isoformat()
        stale_payload["expires_at"] = (NOW + timedelta(minutes=1)).isoformat()
        stale_envelope["signature"] = base64.b64encode(
            PRIVACY_PRIVATE_KEY.sign(canonical_json_bytes(stale_payload))
        ).decode("ascii")
        release = replace(release, privacy_envelope=stale_envelope)

    execution = execute_authorized_holdout(
        release,
        preregistration=preregistration,
        config=AuthorizedAttemptConfig(
            preregistration_sha256=preregistration["preregistration_sha256"],
            source_sha=SOURCE_SHA,
            approval_key_id=KEY_ID,
            credential_reference="qualification_secret_v1",
            policy_ms=100,
            whole_run_timeout_seconds=30,
        ),
        session_config=_config(),
        campaign_envelope=campaign_envelope,
        attempt_envelope=attempt_envelope,
        ledger=ledger,
        ledger_custodian_public_key=ledger_public,
        now=NOW,
        credential_loader=credential_loader,
        connector_factory=lambda _credential: connector,
        measurement_clock_factory=_default_measurement_clock_factory,
        sleep_ms=lambda _value: asyncio.sleep(0),
        pricing=load_pricing(PRICING_PATH),
        custodian_public_key=audit_public,
        custodian_key_id="audit_custodian_1",
        capsule_path=capsule_path,
        capsule_sink=PrivateFileCapsuleSink(),
    )
    if resume_mode not in {"durable", "input_exact"}:
        error = (
            "durably consume the holdout claim"
            if resume_mode == "stale_snapshot"
            else "substituted lease"
            if resume_mode == "substituted_lease"
            else "durably consume the holdout claim"
            if resume_mode == "wrong_claim_time"
            else "fresh"
            if resume_mode == "stale_privacy"
            else "remaining resource budget"
        )
        with pytest.raises(ValueError, match=error):
            asyncio.run(execution)
        expected_order = ["export_snapshot"] if resume_mode == "stale_privacy" else [
            "export_snapshot",
            "asset",
            "resume_holdout",
        ]
        if resume_mode in {
            "input_one_over",
            "output_exhausted",
            "output_one_over",
            "elapsed_exhausted",
        }:
            expected_order = ["export_snapshot", "asset"]
        if resume_mode in {"stale_snapshot", "wrong_claim_time"}:
            expected_order.append("export_snapshot")
        assert order == expected_order
        assert not capsule_path.exists()
        return

    result = asyncio.run(execution)

    assert result.complete is True
    assert result.provider_request_count == 128
    assert result.cost_microusd == 9_984
    assert [name for name, _ in ledger.calls[:3]] == [
        "export_snapshot",
        "resume_holdout",
        "export_snapshot",
    ]
    assert "claim" not in [name for name, _ in ledger.calls]
    assert [name for name, _ in ledger.calls[-2:]] == [
        "terminal_outcome",
        "export_snapshot",
    ]
    terminal = ledger.calls[-2][1]
    assert terminal["outcome"] == "completed"
    assert terminal["actual_provider_requests"] == 128
    assert terminal["actual_cost_microusd"] == 9_984
    envelope = json.loads(capsule_path.read_bytes())
    opened = open_audit_capsule(
        envelope,
        custodian_private_key=audit_private,
        expected_key_id="audit_custodian_1",
    )
    assert opened["accounting"]["split"] == "holdout"
    assert opened["policy_ms"] == 100


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        ("failed_append", "durably consume"),
        ("unsigned_claim", "invalid active claim"),
        ("claim_identity", "invalid active claim"),
        ("lease_substitution", "durably consume"),
        ("reservation_inflation", "durably consume"),
        ("crash_replay", "already consumed"),
    ),
)
def test_development_claim_boundary_fails_closed_before_secret_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    error: str,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    plans, no_speech_plans = _development_schedule()
    ledger = FakeCustodyLedger(ledger_path, campaign_envelope=campaign)
    original_claim = ledger.claim_attempt

    def mutate_claim(**values):
        if mutation == "crash_replay":
            ledger._record("claim", values)
            raise RuntimeError("attempt already consumed after prior crash")
        claim = original_claim(**values)
        if mutation == "unsigned_claim":
            return {"attempt_id": claim.attempt_id}
        if mutation == "claim_identity":
            return replace(claim, campaign_id="campaign_wrong")
        if mutation == "lease_substitution":
            return replace(claim, lease_id="8" * 64)
        assert ledger._state is not None
        if mutation == "failed_append":
            ledger._state = replace(
                ledger._state,
                phase="preregistered",
                phase_history=("preregistered",),
                attempt_ids=(),
                active_attempt_id=None,
                attempt_authorization_sha256=None,
                attempt_claimed_at=None,
                provider_requests_reserved=0,
                cost_reserved_microusd=0,
                final_ledger_head_sha256="1" * 64,
            )
        else:
            ledger._state = replace(
                ledger._state,
                provider_requests_reserved=claim.provider_requests_reserved + 1,
            )
        return claim

    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    monkeypatch.setattr(ledger, "claim_attempt", mutate_claim)
    touched: list[str] = []

    with pytest.raises((ValueError, RuntimeError), match=error):
        asyncio.run(
            execute_authorized_attempt(
                _asset_release(
                    plans,
                    no_speech_plans,
                    preregistration,
                    campaign,
                    attempt,
                    split="development",
                ),
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=ledger,
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert touched == []


def test_environment_identity_mismatch_blocks_before_claim_or_secret_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    plans, no_speech_plans = _development_schedule()
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    monkeypatch.setattr(
        runner_module,
        "_capture_current_execution_identity",
        lambda *, expected_source_sha: CapturedExecutionIdentity(
            report=TEST_EXECUTION_IDENTITY_REPORT,
            sha256="f" * 64,
        ),
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    touched: list[str] = []
    ledger = FakeCustodyLedger(ledger_path, campaign_envelope=campaign)

    with pytest.raises(RunnerError, match="environment identity mismatch"):
        asyncio.run(
            execute_authorized_attempt(
            _asset_release(
                plans,
                no_speech_plans,
                preregistration,
                campaign,
                attempt,
                split="development",
            ),
            preregistration=preregistration,
            config=AuthorizedAttemptConfig(
                preregistration_sha256=preregistration["preregistration_sha256"],
                source_sha=SOURCE_SHA,
                approval_key_id=KEY_ID,
                credential_reference="qualification_secret_v1",
                policy_ms=250,
                whole_run_timeout_seconds=30,
            ),
            session_config=_config(),
            campaign_envelope=campaign,
            attempt_envelope=attempt,
            ledger=ledger,
            ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
            now=NOW,
            credential_loader=lambda _reference: touched.append("credential"),
            connector_factory=lambda _credential: touched.append("connector"),
            measurement_clock_factory=lambda _plan: ReceiptClock([]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(PRICING_PATH),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_path=_capsule_path(ledger_path),
            capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert touched == []
    assert ledger.calls == []


def test_substituted_capsule_destination_blocks_before_ledger_or_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    touched: list[str] = []

    with pytest.raises(ValueError, match="binding"):
        asyncio.run(
            execute_authorized_attempt(
                _asset_release(
                    (_plan(),),
                    (),
                    preregistration,
                    campaign,
                    attempt,
                    split="development",
                ),
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=FakeCustodyLedger(ledger_path, campaign_envelope=campaign),
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=tmp_path / "substituted-capsule.json",
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


@pytest.mark.parametrize("occupied_kind", ["file", "symlink"])
def test_capsule_destination_created_after_preregistration_blocks_before_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    occupied_kind: str,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    capsule_path = _capsule_path(ledger_path)
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    if occupied_kind == "file":
        capsule_path.write_text("occupied")
    else:
        target = tmp_path / "outside-capsule.json"
        target.write_text("occupied")
        capsule_path.symlink_to(target)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )

    with pytest.raises(ValueError, match="artifact destination"):
        asyncio.run(
            execute_authorized_attempt(
                _asset_release(
                    (_plan(),),
                    (),
                    preregistration,
                    campaign,
                    attempt,
                    split="development",
                ),
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=FakeCustodyLedger(ledger_path, campaign_envelope=campaign),
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: pytest.fail("secret must not be read"),
                connector_factory=lambda _credential: pytest.fail("connector must not be built"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=capsule_path,
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert not ledger_path.exists()


def test_invalid_approval_never_reads_secret_or_constructs_connector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    campaign["signature"] = base64.b64encode(b"invalid").decode("ascii")
    touched: list[str] = []
    release = _asset_release(
        (_plan(),),
        (),
        preregistration,
        campaign,
        attempt,
        split="development",
    )
    loader = release.loader
    assert isinstance(loader, FakeAssetLoader)
    ledger = FakeCustodyLedger(ledger_path, campaign_envelope=campaign)
    original_identity = ledger.identity

    def tracked_identity() -> LedgerCustodyIdentity:
        touched.append("ledger")
        return original_identity()

    monkeypatch.setattr(ledger, "identity", tracked_identity)

    with pytest.raises(ValueError, match="signature"):
        asyncio.run(
            execute_authorized_attempt(
                release,
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=ledger,
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert touched == []
    assert loader.authorizations == []
    assert not ledger_path.exists()


def test_asset_release_proxy_is_rejected_without_loader_property_access() -> None:
    touched: list[str] = []

    class HostileRelease:
        @property
        def loader(self):
            touched.append("loader")
            raise AssertionError("loader property must remain inert")

        @property
        def privacy_envelope(self):
            touched.append("privacy_envelope")
            raise AssertionError("release properties must remain inert")

        @property
        def privacy_public_key(self):
            touched.append("privacy_public_key")
            raise AssertionError("release properties must remain inert")

    with pytest.raises(RunnerError, match="asset release is invalid"):
        runner_module._validate_asset_release(HostileRelease())  # type: ignore[arg-type]

    assert touched == []


def test_stale_privacy_receipt_blocks_asset_ledger_secret_and_connector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    plans, no_speech_plans = _development_schedule()
    release = _asset_release(
        plans,
        no_speech_plans,
        preregistration,
        campaign,
        attempt,
        split="development",
    )
    stale_envelope = deepcopy(release.privacy_envelope)
    stale_payload = stale_envelope["payload"]
    assert isinstance(stale_payload, dict)
    stale_payload["issued_at"] = (NOW - timedelta(minutes=6)).isoformat()
    stale_payload["expires_at"] = (NOW + timedelta(minutes=1)).isoformat()
    stale_envelope["signature"] = base64.b64encode(
        PRIVACY_PRIVATE_KEY.sign(canonical_json_bytes(stale_payload))
    ).decode("ascii")
    release = replace(release, privacy_envelope=stale_envelope)
    loader = release.loader
    assert isinstance(loader, FakeAssetLoader)
    ledger = FakeCustodyLedger(ledger_path, campaign_envelope=campaign)
    touched: list[str] = []
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )

    with pytest.raises(ValueError, match="fresh"):
        asyncio.run(
            execute_authorized_attempt(
                release,
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=ledger,
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert loader.authorizations == []
    assert ledger.calls == []
    assert touched == []
    assert not ledger_path.exists()


def test_asset_loader_failure_suppresses_sensitive_exception_chain() -> None:
    class FailingLoader:
        def load(self, _authorization):
            raise RuntimeError(CANARY_SECRET)

    authorization = runner_module.PrivacyCustodyAuthorization(
        campaign_id="campaign_1",
        authorization_id="authorization_1",
        attempt_id="attempt_1",
        split="development",
        preregistration_sha256="a" * 64,
        source_sha="b" * 40,
        schedule_sha256="c" * 64,
        corpus_sha256="d" * 64,
        project="kevin-qualification-test",
        model="models/gemini-3.1-flash-live-preview",
        consent_registry_sha256="e" * 64,
        withdrawal_registry_sha256="f" * 64,
        purpose_attestation_sha256="1" * 64,
        rights_attestation_sha256="2" * 64,
        provider_disclosure_sha256="3" * 64,
        subject_set_sha256="4" * 64,
        retention_policy_sha256="5" * 64,
        provider_retention_decision="zdr_verified",
        residual_retention_acceptance_sha256="6" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        deletion_deadline=NOW + timedelta(days=29),
        nonce="privacy_nonce_1",
        signed_payload_sha256="7" * 64,
    )
    release = AuthorizedAssetRelease(
        loader=FailingLoader(),
        privacy_envelope={},
        privacy_public_key=PRIVACY_PUBLIC_KEY,
    )

    with pytest.raises(ValueError, match="could not be released") as caught:
        runner_module._materialize_qualification_assets(release, authorization)

    assert caught.value.__cause__ is None
    assert CANARY_SECRET not in repr(caught.value)


def test_unprovisioned_source_owned_trust_root_blocks_before_ledger_or_secret(
    tmp_path: Path,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    touched: list[str] = []

    with pytest.raises(ValueError, match="trust root.*unprovisioned"):
        asyncio.run(
            execute_authorized_attempt(
                _asset_release(
                    (_plan(),),
                    (),
                    preregistration,
                    campaign,
                    attempt,
                    split="development",
                ),
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=FakeCustodyLedger(ledger_path, campaign_envelope=campaign),
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


def test_approval_root_swap_after_identity_capture_blocks_before_secret_or_connector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    replacement_public = _key_pair()[1]
    repo_root = tmp_path / "repo"
    approval_root = repo_root / APPROVAL_ROOT_RELATIVE_PATH
    approval_root.parent.mkdir(parents=True)
    approval_root.write_bytes(public)
    replacement_path = tmp_path / "replacement.pub"
    replacement_path.write_bytes(replacement_public)
    identity = _identity_for_approval_root(public)
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(
        public,
        ledger_path,
        custodian_public,
        environment_identity_sha256=identity.sha256,
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    touched: list[str] = []

    def capture_identity(*, expected_source_sha: str) -> CapturedExecutionIdentity:
        assert expected_source_sha == SOURCE_SHA
        replacement_path.replace(approval_root)
        return identity

    monkeypatch.setattr(runner_module, "REPO_ROOT", repo_root)
    monkeypatch.setattr(runner_module, "PINNED_APPROVAL_ROOT_PATH", approval_root)
    monkeypatch.setattr(
        runner_module,
        "_capture_current_execution_identity",
        capture_identity,
    )

    with pytest.raises(ValueError, match="trust root.*source identity"):
        asyncio.run(
            execute_authorized_attempt(
                _asset_release(
                    (_plan(),),
                    (),
                    preregistration,
                    campaign,
                    attempt,
                    split="development",
                ),
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=FakeCustodyLedger(ledger_path, campaign_envelope=campaign),
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


def test_approval_root_loader_rejects_final_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public = _key_pair()[1]
    repo_root = tmp_path / "repo"
    approval_root = repo_root / APPROVAL_ROOT_RELATIVE_PATH
    approval_root.parent.mkdir(parents=True)
    target = tmp_path / "approval.pub"
    target.write_bytes(public)
    approval_root.symlink_to(target)
    monkeypatch.setattr(runner_module, "REPO_ROOT", repo_root)
    monkeypatch.setattr(runner_module, "PINNED_APPROVAL_ROOT_PATH", approval_root)

    with pytest.raises(ValueError, match="trust root.*unavailable"):
        runner_module._load_pinned_approval_public_key(
            _identity_for_approval_root(public)
        )


def test_approval_root_loader_rejects_ancestor_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public = _key_pair()[1]
    repo_root = tmp_path / "repo"
    config_dir = repo_root / "config"
    config_dir.mkdir(parents=True)
    external_qualification = tmp_path / "qualification"
    external_qualification.mkdir()
    (external_qualification / "gate_0b_approval_root.ed25519.pub").write_bytes(public)
    (config_dir / "qualification").symlink_to(external_qualification)
    approval_root = repo_root / APPROVAL_ROOT_RELATIVE_PATH
    monkeypatch.setattr(runner_module, "REPO_ROOT", repo_root)
    monkeypatch.setattr(runner_module, "PINNED_APPROVAL_ROOT_PATH", approval_root)

    with pytest.raises(ValueError, match="trust root.*unavailable"):
        runner_module._load_pinned_approval_public_key(
            _identity_for_approval_root(public)
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "preregistration_document",
        "project",
        "credential_reference",
        "approval_public_key",
        "custodian_public_key",
        "ledger_custodian",
        "pricing",
    ),
)
def test_preregistration_binds_every_observable_execution_input_before_claim(
    tmp_path: Path,
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    config = AuthorizedAttemptConfig(
        preregistration_sha256=preregistration["preregistration_sha256"],
        source_sha=SOURCE_SHA,
        approval_key_id=KEY_ID,
        credential_reference="qualification_secret_v1",
        policy_ms=250,
        whole_run_timeout_seconds=30,
    )
    session_config = _config()
    pricing = load_pricing(PRICING_PATH)
    supplied_preregistration = preregistration
    supplied_public = public
    supplied_custodian = custodian_public
    ledger = FakeCustodyLedger(ledger_path, campaign_envelope=campaign)

    if mutation == "preregistration_document":
        supplied_preregistration = json.loads(json.dumps(preregistration))
        supplied_preregistration["immutable_values"]["project"] = "kevin-qualification-other"
    elif mutation == "project":
        session_config = replace(session_config, project="kevin-qualification-other")
    elif mutation == "credential_reference":
        config = replace(config, credential_reference="different_secret_v1")
    elif mutation == "approval_public_key":
        supplied_public = _key_pair()[1]
    elif mutation == "custodian_public_key":
        supplied_custodian = _custodian_key_pair()[1]
    elif mutation == "ledger_custodian":
        ledger._identity = replace(ledger.identity(), public_key_sha256="0" * 64)
    elif mutation == "pricing":
        raw_pricing = json.loads(PRICING_PATH.read_text())
        raw_pricing["input_text_usd"] = "0.74"
        pricing = load_pricing(raw_pricing)
    monkeypatch.setattr(
        runner_module,
        "_load_pinned_approval_public_key",
        lambda _identity: supplied_public,
    )

    touched: list[str] = []
    expected_error = {
        "project": "privacy custody",
        "approval_public_key": "signature",
        "ledger_custodian": "custody binding",
    }.get(mutation, "preregistration")
    with pytest.raises(ValueError, match=expected_error):
        asyncio.run(
            execute_authorized_attempt(
                _asset_release(
                    (_plan(),),
                    (),
                    preregistration,
                    campaign,
                    attempt,
                    split="development",
                ),
                preregistration=supplied_preregistration,
                config=config,
                session_config=session_config,
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=ledger,
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=pricing,
                custodian_public_key=supplied_custodian,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


def test_runner_has_no_local_attempt_ledger_dependency() -> None:
    source = Path(runner_module.__file__).read_text()

    assert "AttemptLedger" not in source
    assert "LedgerCustodyClient" in source


def test_development_claim_rejects_holdout_plans_before_ledger_or_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    development = _plan()
    holdout_activity = replace(development.activities[0], split="holdout")
    holdout_plan = replace(development, split="holdout", activities=(holdout_activity,))
    holdout_window = replace(_no_speech_plan(), split="holdout")
    touched: list[str] = []

    with pytest.raises(ValueError, match="schedule|holdout|split|phase"):
        asyncio.run(
            execute_authorized_attempt(
                _asset_release(
                    (holdout_plan,),
                    (holdout_window,),
                    preregistration,
                    campaign,
                    attempt,
                    split="development",
                ),
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=FakeCustodyLedger(ledger_path, campaign_envelope=campaign),
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


def test_declared_audio_duration_must_match_pcm_bytes_before_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    base = _plan()
    bad_inputs = tuple(
        replace(value, audio=b"\x00\x00" * 500_000, duration_ms=0)
        if value.kind == "audio"
        else value
        for value in base.replay_inputs
    )
    bad_plan = copy(base)
    object.__setattr__(bad_plan, "replay_inputs", bad_inputs)

    with pytest.raises(ValueError, match="audio.*duration|duration.*audio"):
        asyncio.run(
            execute_authorized_attempt(
                _asset_release(
                    (bad_plan,),
                    (),
                    preregistration,
                    campaign,
                    attempt,
                    split="development",
                ),
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=FakeCustodyLedger(ledger_path, campaign_envelope=campaign),
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: pytest.fail("secret must not be read"),
                connector_factory=lambda _credential: pytest.fail("connector must not be built"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert not ledger_path.exists()


@pytest.mark.parametrize(
    "reservation_override",
    (
        {"provider_request_reservation": 1},
        {"cost_reservation_microusd": 1},
    ),
)
def test_insufficient_signed_liability_blocks_before_ledger_and_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reservation_override: dict[str, int],
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
        **reservation_override,
    )
    plans, no_speech_plans = _development_schedule()
    touched: list[str] = []

    with pytest.raises(ValueError, match="reservation"):
        asyncio.run(
            execute_authorized_attempt(
                _asset_release(
                    plans,
                    no_speech_plans,
                    preregistration,
                    campaign,
                    attempt,
                    split="development",
                ),
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=FakeCustodyLedger(ledger_path, campaign_envelope=campaign),
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


@pytest.mark.parametrize("request_count", (63, 65))
def test_each_split_requires_exactly_half_the_preregistered_requests(
    request_count: int,
) -> None:
    with pytest.raises(ValueError, match="split request cardinality"):
        runner_module._require_exact_split_request_count(
            request_count,
            build_dry_run_preregistration(),
        )


@pytest.mark.parametrize(
    "campaign_override",
    (
        {"max_attempts": 2},
        {"max_provider_requests": 383},
        {"max_cost_microusd": 29_999_999},
    ),
)
def test_nonexact_campaign_ceiling_blocks_before_assets_ledger_and_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    campaign_override: dict[str, int],
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
        **campaign_override,
    )
    plans, no_speech_plans = _development_schedule()
    release = _asset_release(
        plans,
        no_speech_plans,
        preregistration,
        campaign,
        attempt,
        split="development",
    )
    ledger = FakeCustodyLedger(ledger_path, campaign_envelope=campaign)
    touched: list[str] = []

    with pytest.raises(ValueError, match="campaign ceiling"):
        asyncio.run(
            execute_authorized_attempt(
                release,
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=ledger,
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    loader = release.loader
    assert isinstance(loader, FakeAssetLoader)
    assert loader.authorizations == []
    assert ledger.calls == []
    assert touched == []


@pytest.mark.parametrize(
    ("state_field", "value"),
    (
        ("campaign_max_attempts", 2),
        ("campaign_max_provider_requests", 383),
        ("campaign_max_cost_microusd", 29_999_999),
    ),
)
def test_ledger_genesis_ceiling_mismatch_blocks_before_claim_and_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_field: str,
    value: int,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    plans, no_speech_plans = _development_schedule()
    ledger = FakeCustodyLedger(ledger_path, campaign_envelope=campaign)
    assert ledger._state is not None
    ledger._state = replace(ledger._state, **{state_field: value})
    touched: list[str] = []

    with pytest.raises(ValueError, match="ledger campaign ceiling"):
        asyncio.run(
            execute_authorized_attempt(
                _asset_release(
                    plans,
                    no_speech_plans,
                    preregistration,
                    campaign,
                    attempt,
                    split="development",
                ),
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=ledger,
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert [name for name, _ in ledger.calls] == ["export_snapshot"]
    assert touched == []


def test_toy_development_schedule_is_rejected_before_ledger_and_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    touched: list[str] = []

    with pytest.raises(ValueError, match="development schedule"):
        asyncio.run(
            execute_authorized_attempt(
                _asset_release(
                    (_plan(),),
                    (_no_speech_plan(),),
                    preregistration,
                    campaign,
                    attempt,
                    split="development",
                ),
                preregistration=preregistration,
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=preregistration["preregistration_sha256"],
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=FakeCustodyLedger(ledger_path, campaign_envelope=campaign),
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                measurement_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
                capsule_sink=PrivateFileCapsuleSink(),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


def test_all_standard_development_schedule_fails_shared_preclaim_validation() -> None:
    plans, no_speech_plans = _development_schedule()
    mutable_plans = []
    for plan in plans:
        mutated_plan = copy(plan)
        object.__setattr__(
            mutated_plan,
            "activities",
            tuple(
                replace(activity, scenario_tags=("standard",))
                for activity in plan.activities
            ),
        )
        mutable_plans.append(mutated_plan)
    mutated = tuple(mutable_plans)

    with pytest.raises(ValueError, match="schedule allocation"):
        runner_module._validate_exact_development_schedule(
            mutated,
            no_speech_plans=no_speech_plans,
        )


def test_correlated_language_condition_schedule_fails_shared_preclaim_validation() -> None:
    plans, no_speech_plans = _development_schedule()
    mutated = tuple(
        replace(
            plan,
            activities=tuple(
                replace(
                    activity,
                    condition=CONDITIONS[LANGUAGES.index(activity.language) % 4],
                )
                for activity in plan.activities
            ),
        )
        for plan in plans
    )

    with pytest.raises(ValueError, match="schedule allocation"):
        runner_module._validate_exact_development_schedule(
            mutated,
            no_speech_plans=no_speech_plans,
        )


def test_per_session_cost_cap_stops_before_the_next_provider_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    plans, no_speech_plans = _development_schedule()
    first = FakeSession(
        [
            {"setupComplete": {}},
            _server_event(text="purpose recorded phrase 0"),
            _usage_message(output_audio_tokens=21_000),
            None,
        ]
    )
    second = FakeSession(
        [
            {"setupComplete": {}},
            _server_event(text="purpose recorded phrase 4"),
            _usage_message(),
            None,
        ]
    )
    connector = FakeConnector([first, second])

    result = asyncio.run(
        execute_authorized_attempt(
            _asset_release(
                plans,
                no_speech_plans,
                preregistration,
                campaign,
                attempt,
                split="development",
            ),
            preregistration=preregistration,
            config=AuthorizedAttemptConfig(
                preregistration_sha256=preregistration["preregistration_sha256"],
                source_sha=SOURCE_SHA,
                approval_key_id=KEY_ID,
                credential_reference="qualification_secret_v1",
                policy_ms=250,
                whole_run_timeout_seconds=30,
            ),
            session_config=_config(),
            campaign_envelope=campaign,
            attempt_envelope=attempt,
                ledger=FakeCustodyLedger(ledger_path, campaign_envelope=campaign),
                ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
                now=NOW,
            credential_loader=lambda _reference: SecretCredential(CANARY_SECRET),
            connector_factory=lambda _credential: connector,
            measurement_clock_factory=_measurement_clock_factory([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(PRICING_PATH),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_path=_capsule_path(ledger_path),
            capsule_sink=PrivateFileCapsuleSink(),
        )
    )

    assert result.complete is False
    assert result.error_code == "session_cost_cap_exceeded"
    assert result.provider_request_count == 1
    assert len(connector.requests) == 1


def test_connector_failure_consumes_request_records_outcome_and_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    plans, no_speech_plans = _development_schedule()
    connector_attempts = 0
    ledger = FakeCustodyLedger(ledger_path, campaign_envelope=campaign)

    def connector_factory(_credential: SecretCredential):
        nonlocal connector_attempts
        connector_attempts += 1
        raise RuntimeError("transport detail " + CANARY_SECRET)

    result = asyncio.run(
        execute_authorized_attempt(
            _asset_release(
                plans,
                no_speech_plans,
                preregistration,
                campaign,
                attempt,
                split="development",
            ),
            preregistration=preregistration,
            config=AuthorizedAttemptConfig(
                preregistration_sha256=preregistration["preregistration_sha256"],
                source_sha=SOURCE_SHA,
                approval_key_id=KEY_ID,
                credential_reference="qualification_secret_v1",
                policy_ms=250,
                whole_run_timeout_seconds=30,
            ),
            session_config=_config(),
            campaign_envelope=campaign,
            attempt_envelope=attempt,
            ledger=ledger,
            ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
            now=NOW,
            credential_loader=lambda _reference: SecretCredential(CANARY_SECRET),
            connector_factory=connector_factory,
            measurement_clock_factory=lambda _plan: ReceiptClock([]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_path=_capsule_path(ledger_path),
            capsule_sink=PrivateFileCapsuleSink(),
        )
    )

    assert result.complete is False
    assert result.error_code == "connector_failure"
    assert result.provider_request_count == 1
    assert connector_attempts == 1
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())
    assert [name for name, _ in ledger.calls] == [
        "export_snapshot",
        "claim",
        "export_snapshot",
        "terminal_outcome",
        "export_snapshot",
    ]
    assert ledger.calls[-2][1]["actual_provider_requests"] == 1


def test_whole_run_deadline_records_failed_consumed_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(
        runner_module, "_load_pinned_approval_public_key", lambda _identity: public
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    plans, no_speech_plans = _development_schedule()
    ledger = FakeCustodyLedger(ledger_path, campaign_envelope=campaign)

    async def expire(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(runner_module, "_execute_attempt_work", expire)
    result = asyncio.run(
        execute_authorized_attempt(
            _asset_release(
                plans,
                no_speech_plans,
                preregistration,
                campaign,
                attempt,
                split="development",
            ),
            preregistration=preregistration,
            config=AuthorizedAttemptConfig(
                preregistration_sha256=preregistration["preregistration_sha256"],
                source_sha=SOURCE_SHA,
                approval_key_id=KEY_ID,
                credential_reference="qualification_secret_v1",
                policy_ms=250,
                whole_run_timeout_seconds=30,
            ),
            session_config=_config(),
            campaign_envelope=campaign,
            attempt_envelope=attempt,
            ledger=ledger,
            ledger_custodian_public_key=LEDGER_PUBLIC_KEY,
            now=NOW,
            credential_loader=lambda _reference: SecretCredential(CANARY_SECRET),
            connector_factory=lambda _credential: pytest.fail("connector must not be built"),
            measurement_clock_factory=lambda _plan: ReceiptClock([]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_path=_capsule_path(ledger_path),
            capsule_sink=PrivateFileCapsuleSink(),
        )
    )

    assert result.error_code == "whole_run_timeout"
    assert result.provider_request_count == 0
    assert [name for name, _ in ledger.calls] == [
        "export_snapshot",
        "claim",
        "export_snapshot",
        "terminal_outcome",
        "export_snapshot",
    ]
    assert ledger.calls[-2][1]["outcome"] == "failed"
    assert ledger.calls[-2][1]["actual_provider_requests"] == 0


def test_cli_help_and_dry_run_name_every_immutable_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    help_text = build_parser().format_help()
    required_fields = {
        "api_version",
        "approval_key_id",
        "approval_public_key_sha256",
        "attempt_caps",
        "audio_caps",
        "audit_capsule_location_sha256",
        "holdout_capsule_location_sha256",
        "consent_attestation_sha256",
        "corpus_sha256",
        "cost_caps_microusd",
        "credential_activated_at",
        "credential_expires_at",
        "credential_key_resource_sha256",
        "credential_reference",
        "credential_restrictions_sha256",
        "credential_revocation_policy_sha256",
        "credential_revocation_required_by",
        "custodian_key_id",
        "custodian_public_key_sha256",
        "privacy_custodian_key_id",
        "privacy_custodian_public_key_sha256",
        "record_root_key_id",
        "record_root_public_key_sha256",
        "ledger_instance_id",
        "ledger_custodian_key_id",
        "ledger_custodian_public_key_sha256",
        "endpoint",
        "environment_identity_sha256",
        "development_schedule_sha256",
        "evaluator_sha256",
        "evidence_location_sha256",
        "ledger_location_sha256",
        "manifest_sha256",
        "model",
        "pricing_sha256",
        "project",
        "project_number",
        "provider_quota_sha256",
        "retention_attestation_sha256",
        "runner_sha256",
        "setup_sha256",
        "source_sha",
        "source_fact_bundle_sha256",
        "transport",
        "usage_caps",
        "zdr_or_residual_retention_acceptance_sha256",
    }

    document = build_dry_run_preregistration()

    assert required_fields <= set(document["immutable_values"])
    assert all(field in help_text for field in required_fields)
    assert document["immutable_values"]["project"] is None
    assert document["immutable_values"]["credential_reference"] is None
    assert all(
        document["immutable_values"][field] is None
        for field in (
            "project_number",
            "credential_key_resource_sha256",
            "credential_restrictions_sha256",
            "provider_quota_sha256",
            "credential_activated_at",
            "credential_expires_at",
            "credential_revocation_required_by",
            "credential_revocation_policy_sha256",
            "source_fact_bundle_sha256",
        )
    )
    assert document["credential_default_present"] is False
    assert document["provider_execution_authorized"] is False
    assert all(value is False for value in document["evidence"].values())
    assert main(["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == document


def test_exact_preregistration_uses_strict_external_values_and_canonical_digest(
    tmp_path: Path,
) -> None:
    values = {
        "schema_id": "gate_0b_preregistration_values_v2",
        "project": "kevin-qualification-test",
        "project_number": "123456789012",
        "credential_reference": "qualification_secret_v1",
        "credential_key_resource_sha256": "a" * 64,
        "credential_restrictions_sha256": "b" * 64,
        "provider_quota_sha256": "c" * 64,
        "credential_activated_at": "2026-07-15T14:00:00Z",
        "credential_expires_at": "2026-07-15T16:00:00Z",
        "credential_revocation_required_by": "2026-07-15T16:00:00Z",
        "credential_revocation_policy_sha256": "d" * 64,
        "approval_key_id": KEY_ID,
        "approval_public_key_sha256": "1" * 64,
        "custodian_key_id": "audit_custodian_1",
        "custodian_public_key_sha256": "f" * 64,
        "privacy_custodian_key_id": "privacy_custodian_1",
        "privacy_custodian_public_key_sha256": "3" * 64,
        "record_root_key_id": "evidence_custodian_1",
        "record_root_public_key_sha256": "0" * 64,
        "ledger_instance_id": "ledger_instance_1",
        "ledger_custodian_key_id": "ledger_custodian_1",
        "ledger_custodian_public_key_sha256": "2" * 64,
        "source_sha": SOURCE_SHA,
        "source_fact_bundle_sha256": "e" * 64,
        "environment_identity_sha256": "2" * 64,
        "manifest_sha256": "3" * 64,
        "corpus_sha256": "4" * 64,
        "development_schedule_sha256": "0" * 64,
        "setup_sha256": "5" * 64,
        "pricing_sha256": "6" * 64,
        "runner_sha256": "7" * 64,
        "evaluator_sha256": "8" * 64,
        "ledger_location_sha256": "9" * 64,
        "audit_capsule_location_sha256": "a" * 64,
        "holdout_capsule_location_sha256": "f" * 64,
        "evidence_location_sha256": "b" * 64,
        "consent_attestation_sha256": "c" * 64,
        "retention_attestation_sha256": "d" * 64,
        "zdr_or_residual_retention_acceptance_sha256": "e" * 64,
    }

    document = build_preregistration(values)
    unsigned = dict(document)
    digest = unsigned.pop("preregistration_sha256")

    assert digest == sha256(canonical_json_bytes(unsigned)).hexdigest()
    assert document["status"] == "preregistered_pending_separate_approval"
    assert document["immutable_values"]["credential_reference"] == ("qualification_secret_v1")
    assert document["credential_default_present"] is False
    assert all(value is False for value in document["evidence"].values())

    values_path = tmp_path / "values.json"
    output_path = tmp_path / "preregistration.json"
    values_path.write_text(json.dumps(values))
    values_path.chmod(0o600)
    assert (
        main(
            [
                "--dry-run",
                "--values",
                str(values_path),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    assert json.loads(output_path.read_text()) == document

    with pytest.raises(ValueError, match="fields"):
        build_preregistration({**values, "unexpected": True})

    with pytest.raises(ValueError, match="distinct identities"):
        build_preregistration(
            {
                **values,
                "privacy_custodian_key_id": values["custodian_key_id"],
            }
        )
    with pytest.raises(ValueError, match="project number"):
        build_preregistration({**values, "project_number": "not-a-number"})
    with pytest.raises(ValueError, match="credential lifetime"):
        build_preregistration(
            {
                **values,
                "credential_revocation_required_by": "2026-07-15T16:00:01Z",
            }
        )
    with pytest.raises(ValueError, match="credential timestamps"):
        build_preregistration(
            {
                **values,
                "credential_activated_at": "2026-07-15T14:00:00.000000Z",
            }
        )
    with pytest.raises(ValueError, match="distinct identities"):
        build_preregistration(
            {
                **values,
                "privacy_custodian_public_key_sha256": values[
                    "custodian_public_key_sha256"
                ],
            }
        )


def test_dry_run_output_must_be_outside_repository(tmp_path: Path) -> None:
    outside = tmp_path / "gate0b-preregistration.json"
    inside = Path("docs/gate0b-preregistration.invalid.json").resolve()

    assert main(["--dry-run", "--output", str(outside)]) == 0
    assert json.loads(outside.read_text()) == build_dry_run_preregistration()
    assert outside.stat().st_mode & 0o777 == 0o600
    assert main(["--dry-run", "--output", str(inside)]) == 2
    assert not inside.exists()


def test_gate0b_runbook_is_pending_external_only_and_non_authorizing() -> None:
    runbook = Path("docs/gemini-caller-turn-qualification-gate-0b.md").read_text()
    adr = Path("docs/adr/0001-gemini-retrospective-caller-turns.md").read_text()
    required_flags = {
        "future_execution_authorized",
        "model_migration_authorized",
        "runtime_wiring_authorized",
        "staging_authorized",
        "deployment_authorized",
        "production_authorized",
        "release_authorized",
    }

    assert "Status: Implementation-only; provider execution not approved" in runbook
    assert 'QUALIFICATION_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}' in runbook
    assert "sudo install" not in runbook
    assert "--dry-run" in runbook
    assert "--output" in runbook
    assert "--credential" not in runbook
    assert "GEMINI_" + "API_KEY=" not in runbook
    assert required_flags <= set(runbook.split())
    assert "Pending Gate 0B; no go decision" in adr
    assert "does not authorize provider execution" in adr


def test_cli_execute_is_hard_blocked_and_dry_run_has_no_connector() -> None:
    assert main([]) == 0
    assert main(["--execute"]) == 2
