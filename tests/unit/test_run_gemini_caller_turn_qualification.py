"""Injected, zero-real-network Gate 0B session executor tests."""

import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import stat

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import pytest
import scripts.run_gemini_caller_turn_qualification as runner_module

from app.services.caller_turn_alignment import ActivityReference
from app.services.caller_turn_measurement import open_audit_capsule
from app.services.caller_turn_qualification import load_pricing
from app.services.caller_turns import CallerTurnEventKind
from app.services.qualification_identity import (
    AttemptLedger,
    canonical_json_bytes,
    ledger_location_sha256,
)
from app.services.voice_turn_replay import Gate0BReplayInput
from scripts.run_gemini_caller_turn_qualification import (
    OFFICIAL_ENDPOINT,
    AuthorizedAttemptConfig,
    ConnectionPolicy,
    NoSpeechWindowPlan,
    ProviderSessionClosed,
    ReductionResult,
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
    execute_authorized_attempt,
    execute_injected_session,
    execute_injected_no_speech_window,
    main,
)


CANARY_SECRET = "qualification-canary-secret-must-not-escape"
NOW = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
PREREGISTRATION_SHA = "a" * 64
SOURCE_SHA = "b" * 40
KEY_ID = "qualification-reviewer-v1"


@pytest.fixture(autouse=True)
def _fixed_execution_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner_module,
        "_capture_current_execution_identity",
        lambda *, expected_source_sha: "2" * 64,
    )
PRICING_PATH = Path("tests/fixtures/caller_turn_qualification/pricing.json")
LANGUAGES = ("ar", "en", "es", "fr", "hi", "ht", "pt", "zh")
CONDITIONS = ("clean", "twilio_codec_only", "acoustic_impairment", "interaction_stress")


class FakeSession:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    async def receive(self):
        if not self.messages:
            return None
        value = self.messages.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self):
        self.closed = True


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
    plans = []
    for session_ordinal in range(32):
        activities = []
        replay_inputs = []
        for index in range(4):
            ordinal = session_ordinal * 4 + index
            start_ms = index * 150
            end_ms = start_ms + 100
            language = LANGUAGES[ordinal % len(LANGUAGES)]
            condition = CONDITIONS[ordinal % len(CONDITIONS)]
            activity = SessionActivityPlan(
                activity_ordinal=ordinal,
                split="development",
                language=language,
                condition=condition,
                scenario_tags=("standard",),
                reference=ActivityReference(
                    ordinal,
                    language,
                    f"purpose recorded phrase {ordinal}",
                ),
                expected_lifecycle_status="retrospective_complete",
                expected_epoch=1,
                start_at_ms=start_ms,
                end_at_ms=end_ms,
            )
            activities.append(activity)
            replay_inputs.extend(
                (
                    Gate0BReplayInput("caller_activity_start", start_ms, 1, ordinal),
                    Gate0BReplayInput(
                        "audio",
                        start_ms + 20,
                        1,
                        ordinal,
                        audio=b"\x00\x00" * 319,
                        duration_ms=20,
                    ),
                    Gate0BReplayInput("caller_activity_end", end_ms, 1, ordinal),
                )
            )
        plans.append(
            SessionPlan(
                session_ordinal=session_ordinal,
                split="development",
                activities=tuple(activities),
                replay_inputs=tuple(replay_inputs),
            )
        )
    windows = tuple(
        NoSpeechWindowPlan(
            window_ordinal=ordinal,
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
        for ordinal in range(32)
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
        start_at_ms=150,
        end_at_ms=250,
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
            Gate0BReplayInput("caller_activity_start", 150, 2, 2),
            Gate0BReplayInput("audio", 170, 2, 2, audio=b"\x00\x00" * 319, duration_ms=20),
            Gate0BReplayInput("caller_activity_end", 250, 2, 2),
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


def _tool_interaction_plan() -> SessionPlan:
    base = _plan(two_activities=True)
    return SessionPlan(
        session_ordinal=base.session_ordinal,
        split=base.split,
        activities=base.activities,
        replay_inputs=(
            *base.replay_inputs[:3],
            Gate0BReplayInput("expect_synchronous_tool", 100, 1, 1),
            *base.replay_inputs[3:],
        ),
    )


def _cancellation_interaction_plan() -> SessionPlan:
    base = _plan(two_activities=True)
    return SessionPlan(
        session_ordinal=base.session_ordinal,
        split=base.split,
        activities=base.activities,
        replay_inputs=(
            *base.replay_inputs[:3],
            Gate0BReplayInput("expect_tool_cancellation", 100, 1, 1),
            Gate0BReplayInput("expect_interruption", 100, 1, 1),
            *base.replay_inputs[3:],
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
    )


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
        "max_attempts": 3,
        "max_provider_requests": 384,
        "max_cost_microusd": 30_000_000,
        "ledger_instance_id": "ledger_instance_1",
        "ledger_custodian_key_id": "ledger_custodian_1",
        "ledger_custodian_public_key_sha256": "8" * 64,
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
) -> dict[str, object]:
    setup_sha256 = sha256(canonical_json_bytes(build_gate0b_setup_identity(_config()))).hexdigest()
    plans, no_speech_plans = _development_schedule()
    return build_preregistration(
        {
            "schema_id": "gate_0b_preregistration_values_v1",
            "project": _config().project,
            "credential_reference": "qualification_secret_v1",
            "approval_key_id": KEY_ID,
            "approval_public_key_sha256": sha256(approval_public_key).hexdigest(),
            "custodian_key_id": "audit_custodian_1",
            "custodian_public_key_sha256": sha256(custodian_public_key).hexdigest(),
            "record_root_key_id": "evidence_custodian_1",
            "record_root_public_key_sha256": "9" * 64,
            "ledger_instance_id": "ledger_instance_1",
            "ledger_custodian_key_id": "ledger_custodian_1",
            "ledger_custodian_public_key_sha256": "8" * 64,
            "source_sha": SOURCE_SHA,
            "environment_identity_sha256": "2" * 64,
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
            "audit_capsule_location_sha256": artifact_location_sha256(
                _capsule_path(ledger_path)
            ),
            "evidence_location_sha256": "b" * 64,
            "consent_attestation_sha256": "c" * 64,
            "retention_attestation_sha256": "d" * 64,
            "zdr_or_residual_retention_acceptance_sha256": "e" * 64,
        }
    )


def _capsule_path(ledger_path: Path) -> Path:
    return ledger_path.with_name("gate-0b-capsule.json")


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
    session = FakeSession(
        [
            {"setupComplete": {}},
            _server_event(),
            _usage_message(),
            None,
        ]
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
            receipt_clock_ms=ReceiptClock([0, 150, 160, 170]),
            sleep_ms=sleep_ms,
        )
    )

    assert result.complete is True
    assert result.error_code is None
    assert sleeps == [20, 80]
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


def test_receive_loop_is_live_while_the_paced_sender_is_still_running() -> None:
    class CoordinatedSession:
        def __init__(self) -> None:
            self.sent = []
            self.audio_sent = asyncio.Event()
            self.response_received = asyncio.Event()
            self.receive_index = 0
            self.closed = False

        async def send(self, message):
            self.sent.append(message)
            if isinstance(message.get("realtimeInput"), dict) and "audio" in message["realtimeInput"]:
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
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            receipt_clock_ms=ReceiptClock([0, 120, 130]),
            sleep_ms=sleep_ms,
        )
    )

    assert result.complete is True
    assert receiver_was_live_during_send is True


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
            if isinstance(message.get("realtimeInput"), dict) and "audio" in message["realtimeInput"]:
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
            receipt_clock_ms=ReceiptClock([0, 120, 130]),
            sleep_ms=sleep_ms,
        )
    )

    assert result.complete is True
    assert receiver_was_live_during_send is True


def test_multiple_official_usage_frames_use_latest_cumulative_snapshot() -> None:
    first = _server_event(text="book service today 1")
    second = _server_event(text="book service today 2")
    session = FakeSession(
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
        ]
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(two_activities=True),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            receipt_clock_ms=ReceiptClock([0, 120, 130, 300, 310]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.usage.input_audio_tokens == 12
    assert result.usage.output_audio_tokens == 6


def test_decreasing_cumulative_usage_snapshot_fails_closed() -> None:
    first = _server_event(text="book service today 1")
    second = _server_event(text="book service today 2")
    session = FakeSession(
        [
            {"setupComplete": {}},
            first,
            _usage_message(input_audio_tokens=12, output_audio_tokens=6),
            second,
            _usage_message(input_audio_tokens=8, output_audio_tokens=4),
            None,
        ]
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(two_activities=True),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            receipt_clock_ms=ReceiptClock([0, 120, 130, 300, 310]),
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
            receipt_clock_ms=ReceiptClock([0, 120, 130]),
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
            receipt_clock_ms=ReceiptClock([0, 120, 130, 140]),
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
            receipt_clock_ms=ReceiptClock([0, 120, 300]),
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
    session = FakeSession(
        [
            {"setupComplete": {}},
            {
                "serverContent": {"interrupted": True},
                "toolCallCancellation": {"ids": ["tool_1"]},
            },
            _usage_message(),
            None,
        ]
    )

    result = asyncio.run(
        execute_injected_session(
            _cancellation_interaction_plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            receipt_clock_ms=ReceiptClock([0, 120, 300]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert [event.kind for event in result.audit_events] == [
        CallerTurnEventKind.INTERRUPTED,
        CallerTurnEventKind.TOOL_CALL_CANCELLED,
    ]


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
            receipt_clock_ms=ReceiptClock([0, 120, 130]),
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
            receipt_clock_ms=ReceiptClock([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )
    second = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([inconsistent]),
            credential=SecretCredential(CANARY_SECRET),
            receipt_clock_ms=ReceiptClock([0, 120, 130, 140]),
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
            receipt_clock_ms=ReceiptClock([0]),
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
            receipt_clock_ms=ReceiptClock([0, 120, 130, 140]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            secondary_reducer=disagree,
        )
    )

    assert result.complete is False
    assert result.error_code == "reducer_disagreement"
    assert result.audit_events == ()


def test_current_response_before_activity_end_is_rejected() -> None:
    session = FakeSession([{"setupComplete": {}}, _server_event(), _usage_message(), None])

    result = asyncio.run(
        execute_injected_session(
            _plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            receipt_clock_ms=ReceiptClock([0, 50, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is False
    assert result.error_code == "premature_current_response"
    assert result.wire_observations[1].premature_current_audio_count == 1


def test_open_prior_response_remains_prior_during_next_activity() -> None:
    first_audio = _server_event(text="first", terminal=False)
    first_audio["serverContent"].pop("inputTranscription")
    second_audio = _server_event(text="second", terminal=False)
    second_audio["serverContent"].pop("inputTranscription")
    interrupted = {"serverContent": {"interrupted": True}}
    current = _server_event(text="second", terminal=True)
    session = FakeSession(
        [
            {"setupComplete": {}},
            first_audio,
            second_audio,
            interrupted,
            current,
            _usage_message(),
            None,
        ]
    )

    result = asyncio.run(
        execute_injected_session(
            _plan(two_activities=True),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            receipt_clock_ms=ReceiptClock([0, 120, 200, 220, 300, 310, 320]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.wire_observations[2].interruption_tail_ms == 50
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
            receipt_clock_ms=ReceiptClock([0, 120, 130, 150, 300, 310]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.provider_request_count == 2
    assert result.epoch_count == 2
    assert [request.epoch for request in connector.requests] == [1, 2]
    assert {event.epoch for event in result.audit_events} == {1, 2}
    assert first.sent[0] == second.sent[0] == build_gate0b_setup_message(_config())
    assert all("sessionResumption" not in json.dumps(message) for message in second.sent)


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
            receipt_clock_ms=ReceiptClock([0, 120, 130, 140, 150, 160]),
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
            receipt_clock_ms=ReceiptClock([0, 120]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.error_code == "provider_closed"
    assert result.wire_observations[1].abnormal_close_count == 1
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())


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
            receipt_clock_ms=ReceiptClock([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.error_code == "audio_after_terminal"
    assert result.wire_observations[1].audio_after_terminal_count == 1


def test_no_speech_window_records_false_activation_without_computing_verdict() -> None:
    session = FakeSession([{"setupComplete": {}}, _server_event(), _usage_message(), None])

    result = asyncio.run(
        execute_injected_no_speech_window(
            _no_speech_plan(),
            config=_config(),
            connector=FakeConnector([session]),
            credential=SecretCredential(CANARY_SECRET),
            receipt_clock_ms=ReceiptClock([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
        )
    )

    assert result.complete is True
    assert result.false_activity_count == 1
    assert result.model_audio_chunk_count == 1
    assert result.output_audio_bytes == 4
    assert [fact.kind for fact in result.wire_facts] == [
        "false_activity",
        "audio_received",
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
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    order: list[str] = []
    plans, no_speech_plans = _development_schedule()
    sessions = [
        FakeSession(
            [
                {"setupComplete": {}},
                _server_event(text=plan.activities[0].reference.text),
                _usage_message(),
                None,
            ]
        )
        for plan in plans
    ]
    no_speech_sessions = [
        FakeSession([{"setupComplete": {}}, _server_event(), _usage_message(), None])
        for _ in no_speech_plans
    ]
    connector = FakeConnector([*sessions, *no_speech_sessions])
    capsule_path = _capsule_path(ledger_path)

    class RecordingLedger:
        def __init__(self) -> None:
            self.delegate = AttemptLedger(ledger_path)
            self.path = self.delegate.path

        def claim_attempt(self, **kwargs):
            order.append("claim")
            return self.delegate.claim_attempt(**kwargs)

        def record_outcome(self, *args, **kwargs):
            order.append("outcome")
            return self.delegate.record_outcome(*args, **kwargs)

    def source_identity_check(*, expected_source_sha: str) -> str:
        assert expected_source_sha == SOURCE_SHA
        order.append("source")
        return "2" * 64

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

    result = asyncio.run(
        execute_authorized_attempt(
            plans,
            no_speech_plans=no_speech_plans,
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
            ledger=RecordingLedger(),
            now=NOW,
            credential_loader=credential_loader,
            connector_factory=connector_factory,
            receipt_clock_factory=lambda _plan: ReceiptClock([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_path=capsule_path,
        )
    )

    assert result.complete is True
    assert result.capsule_handed_off is True
    assert result.provider_request_count == 64
    assert result.cost_microusd == 4_992
    assert stat.S_IMODE(capsule_path.stat().st_mode) == 0o600
    assert order[:3] == ["claim", "source", "credential:qualification_secret_v1"]
    assert order[3:-1] == ["connector"] * 64
    assert order[-1:] == ["outcome"]
    envelope = json.loads(capsule_path.read_bytes())
    opened = open_audit_capsule(
        envelope,
        custodian_private_key=custodian,
        expected_key_id="audit_custodian_1",
    )
    assert opened["activities"][0]["reference_text"] == "purpose recorded phrase 0"
    assert opened["no_speech_windows"][0]["wire_facts"][0]["kind"] == "false_activity"
    assert CANARY_SECRET not in json.dumps(envelope)
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())
    assert not hasattr(result, "audit_events")
    snapshot = AttemptLedger(ledger_path).snapshot()
    assert [record["event"] for record in snapshot["records"]] == [
        "phase_transition",
        "claim",
        "outcome",
    ]
    assert snapshot["records"][-1]["actual_cost_microusd"] == 4_992
    assert snapshot["records"][-1]["capsule_sha256"] == sha256(
        capsule_path.read_bytes().rstrip(b"\n")
    ).hexdigest()


def test_environment_identity_mismatch_consumes_attempt_before_secret_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    plans, no_speech_plans = _development_schedule()
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
    monkeypatch.setattr(
        runner_module,
        "_capture_current_execution_identity",
        lambda *, expected_source_sha: "f" * 64,
    )
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    touched: list[str] = []

    result = asyncio.run(
        execute_authorized_attempt(
            plans,
            no_speech_plans=no_speech_plans,
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
            ledger=AttemptLedger(ledger_path),
            now=NOW,
            credential_loader=lambda _reference: touched.append("credential"),
            connector_factory=lambda _credential: touched.append("connector"),
            receipt_clock_factory=lambda _plan: ReceiptClock([]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(PRICING_PATH),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_path=_capsule_path(ledger_path),
        )
    )

    assert result.complete is False
    assert result.error_code == "source_identity_failed"
    assert touched == []
    snapshot = AttemptLedger(ledger_path).snapshot()
    assert [record["event"] for record in snapshot["records"]] == [
        "phase_transition",
        "claim",
        "outcome",
    ]
    assert snapshot["records"][-1]["outcome"] == "failed"


def test_substituted_capsule_destination_blocks_before_ledger_or_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    touched: list[str] = []

    with pytest.raises(ValueError, match="binding"):
        asyncio.run(
            execute_authorized_attempt(
                (_plan(),),
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
                ledger=AttemptLedger(ledger_path),
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                receipt_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=tmp_path / "substituted-capsule.json",
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
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )

    with pytest.raises(ValueError, match="artifact destination"):
        asyncio.run(
            execute_authorized_attempt(
                (_plan(),),
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
                ledger=AttemptLedger(ledger_path),
                now=NOW,
                credential_loader=lambda _reference: pytest.fail("secret must not be read"),
                connector_factory=lambda _credential: pytest.fail("connector must not be built"),
                receipt_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=capsule_path,
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
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    campaign["signature"] = base64.b64encode(b"invalid").decode("ascii")
    touched: list[str] = []

    with pytest.raises(ValueError, match="signature"):
        asyncio.run(
            execute_authorized_attempt(
                (_plan(),),
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
                ledger=AttemptLedger(ledger_path),
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                receipt_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


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
                (_plan(),),
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
                ledger=AttemptLedger(ledger_path),
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                receipt_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


@pytest.mark.parametrize(
    "mutation",
    (
        "preregistration_document",
        "project",
        "credential_reference",
        "approval_public_key",
        "custodian_public_key",
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
    elif mutation == "pricing":
        raw_pricing = json.loads(PRICING_PATH.read_text())
        raw_pricing["input_text_usd"] = "0.74"
        pricing = load_pricing(raw_pricing)
    monkeypatch.setattr(
        runner_module,
        "_load_pinned_approval_public_key",
        lambda: supplied_public,
    )

    touched: list[str] = []
    with pytest.raises(ValueError, match="preregistration"):
        asyncio.run(
            execute_authorized_attempt(
                (_plan(),),
                preregistration=supplied_preregistration,
                config=config,
                session_config=session_config,
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                ledger=AttemptLedger(ledger_path),
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                receipt_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=pricing,
                custodian_public_key=supplied_custodian,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


def test_development_claim_rejects_holdout_plans_before_ledger_or_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
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

    with pytest.raises(ValueError, match="holdout|split|phase"):
        asyncio.run(
            execute_authorized_attempt(
                (holdout_plan,),
                no_speech_plans=(holdout_window,),
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
                ledger=AttemptLedger(ledger_path),
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                receipt_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
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
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
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
    bad_plan = replace(base, replay_inputs=bad_inputs)

    with pytest.raises(ValueError, match="audio.*duration|duration.*audio"):
        asyncio.run(
            execute_authorized_attempt(
                (bad_plan,),
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
                ledger=AttemptLedger(ledger_path),
                now=NOW,
                credential_loader=lambda _reference: pytest.fail("secret must not be read"),
                connector_factory=lambda _credential: pytest.fail("connector must not be built"),
                receipt_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
            )
        )

    assert not ledger_path.exists()


def test_insufficient_signed_request_reservation_blocks_before_ledger_and_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
        provider_request_reservation=1,
    )
    plans, no_speech_plans = _development_schedule()
    touched: list[str] = []

    with pytest.raises(ValueError, match="request reservation"):
        asyncio.run(
            execute_authorized_attempt(
                plans,
                no_speech_plans=no_speech_plans,
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
                ledger=AttemptLedger(ledger_path),
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                receipt_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


def test_toy_development_schedule_is_rejected_before_ledger_and_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    touched: list[str] = []

    with pytest.raises(ValueError, match="development schedule"):
        asyncio.run(
            execute_authorized_attempt(
                (_plan(),),
                no_speech_plans=(_no_speech_plan(),),
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
                ledger=AttemptLedger(ledger_path),
                now=NOW,
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                receipt_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(PRICING_PATH),
                custodian_public_key=custodian_public,
                custodian_key_id="audit_custodian_1",
                capsule_path=_capsule_path(ledger_path),
            )
        )

    assert touched == []
    assert not ledger_path.exists()


def test_per_session_cost_cap_stops_before_the_next_provider_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
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
            plans,
            no_speech_plans=no_speech_plans,
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
            ledger=AttemptLedger(ledger_path),
            now=NOW,
            credential_loader=lambda _reference: SecretCredential(CANARY_SECRET),
            connector_factory=lambda _credential: connector,
            receipt_clock_factory=lambda _plan: ReceiptClock([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(PRICING_PATH),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_path=_capsule_path(ledger_path),
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
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    plans, no_speech_plans = _development_schedule()
    connector_attempts = 0

    def connector_factory(_credential: SecretCredential):
        nonlocal connector_attempts
        connector_attempts += 1
        raise RuntimeError("transport detail " + CANARY_SECRET)

    result = asyncio.run(
        execute_authorized_attempt(
            plans,
            no_speech_plans=no_speech_plans,
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
            ledger=AttemptLedger(ledger_path),
            now=NOW,
            credential_loader=lambda _reference: SecretCredential(CANARY_SECRET),
            connector_factory=connector_factory,
            receipt_clock_factory=lambda _plan: ReceiptClock([]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_path=_capsule_path(ledger_path),
        )
    )

    assert result.complete is False
    assert result.error_code == "connector_failure"
    assert result.provider_request_count == 1
    assert connector_attempts == 1
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())
    outcome = AttemptLedger(ledger_path).snapshot()["records"][-1]
    assert outcome["event"] == "outcome"
    assert outcome["actual_provider_requests"] == 1


def test_whole_run_deadline_records_failed_consumed_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    _, custodian_public = _custodian_key_pair()
    preregistration = _preregistration(public, ledger_path, custodian_public)
    monkeypatch.setattr(runner_module, "_load_pinned_approval_public_key", lambda: public)
    campaign, attempt = _approval_envelopes(
        private,
        ledger_path,
        preregistration_sha256=preregistration["preregistration_sha256"],
    )
    plans, no_speech_plans = _development_schedule()

    async def expire(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(runner_module, "_execute_attempt_work", expire)
    result = asyncio.run(
        execute_authorized_attempt(
            plans,
            no_speech_plans=no_speech_plans,
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
            ledger=AttemptLedger(ledger_path),
            now=NOW,
            credential_loader=lambda _reference: SecretCredential(CANARY_SECRET),
            connector_factory=lambda _credential: pytest.fail("connector must not be built"),
            receipt_clock_factory=lambda _plan: ReceiptClock([]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_path=_capsule_path(ledger_path),
        )
    )

    assert result.error_code == "whole_run_timeout"
    assert result.provider_request_count == 0
    outcome = AttemptLedger(ledger_path).snapshot()["records"][-1]
    assert outcome["outcome"] == "failed"
    assert outcome["actual_provider_requests"] == 0


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
        "consent_attestation_sha256",
        "corpus_sha256",
        "cost_caps_microusd",
        "credential_reference",
        "custodian_key_id",
        "custodian_public_key_sha256",
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
        "retention_attestation_sha256",
        "runner_sha256",
        "setup_sha256",
        "source_sha",
        "transport",
        "usage_caps",
        "zdr_or_residual_retention_acceptance_sha256",
    }

    document = build_dry_run_preregistration()

    assert required_fields <= set(document["immutable_values"])
    assert all(field in help_text for field in required_fields)
    assert document["immutable_values"]["project"] is None
    assert document["immutable_values"]["credential_reference"] is None
    assert document["credential_default_present"] is False
    assert document["provider_execution_authorized"] is False
    assert all(value is False for value in document["evidence"].values())
    assert main(["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == document


def test_exact_preregistration_uses_strict_external_values_and_canonical_digest(
    tmp_path: Path,
) -> None:
    values = {
        "schema_id": "gate_0b_preregistration_values_v1",
        "project": "kevin-qualification-test",
        "credential_reference": "qualification_secret_v1",
        "approval_key_id": KEY_ID,
        "approval_public_key_sha256": "1" * 64,
        "custodian_key_id": "audit_custodian_1",
        "custodian_public_key_sha256": "f" * 64,
        "record_root_key_id": "evidence_custodian_1",
        "record_root_public_key_sha256": "0" * 64,
        "ledger_instance_id": "ledger_instance_1",
        "ledger_custodian_key_id": "ledger_custodian_1",
        "ledger_custodian_public_key_sha256": "f" * 64,
        "source_sha": SOURCE_SHA,
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
    assert "/var/lib/hey-kevin-qualification/" in runbook
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
