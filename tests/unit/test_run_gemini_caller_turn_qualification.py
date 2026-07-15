"""Injected, zero-real-network Gate 0B session executor tests."""

import asyncio
import base64
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import pytest
import scripts.run_gemini_caller_turn_qualification as runner_module

from app.services.caller_turn_alignment import ActivityReference
from app.services.caller_turn_measurement import open_audit_capsule
from app.services.caller_turn_qualification import CampaignPhase, load_pricing
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
    CapsuleHandoffReceipt,
    ConnectionPolicy,
    NoSpeechWindowPlan,
    ProviderSessionClosed,
    ReductionResult,
    SecretCredential,
    SessionActivityPlan,
    SessionExecutionConfig,
    SessionPlan,
    build_gate0b_setup_message,
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


def _usage_message():
    return {
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "promptTokensDetails": [
                {"modality": "AUDIO", "tokenCount": 8},
                {"modality": "TEXT", "tokenCount": 2},
            ],
            "candidatesTokensDetails": [
                {"modality": "AUDIO", "tokenCount": 4},
                {"modality": "TEXT", "tokenCount": 1},
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


def _signed(private: Ed25519PrivateKey, payload: dict[str, object]) -> dict[str, object]:
    return {
        "key_id": KEY_ID,
        "payload": payload,
        "signature": base64.b64encode(private.sign(canonical_json_bytes(payload))).decode("ascii"),
    }


def _approval_envelopes(
    private: Ed25519PrivateKey,
    ledger_path: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    campaign = {
        "schema_id": "gate_0b_campaign_approval_v1",
        "scope": "gate_0b_purpose_recorded_turn_assembly",
        "campaign_id": "campaign_001",
        "authorization_id": "authorization_001",
        "nonce": "nonce_001",
        "preregistration_sha256": PREREGISTRATION_SHA,
        "source_sha": SOURCE_SHA,
        "issued_at": "2026-07-15T14:59:00Z",
        "expires_at": "2026-07-15T16:00:00Z",
        "max_attempts": 3,
        "max_provider_requests": 384,
        "max_cost_microusd": 30_000_000,
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
        "preregistration_sha256": PREREGISTRATION_SHA,
        "source_sha": SOURCE_SHA,
        "issued_at": "2026-07-15T14:59:00Z",
        "expires_at": "2026-07-15T16:00:00Z",
        "provider_request_reservation": 128,
        "cost_reservation_microusd": 10_000_000,
    }
    return _signed(private, campaign), _signed(private, attempt)


def test_setup_and_connection_policy_are_exact_and_non_debuggable() -> None:
    setup = build_gate0b_setup_message(_config())
    policy = ConnectionPolicy()

    assert set(setup) == {"setup"}
    assert setup["setup"]["model"] == "models/gemini-3.1-flash-live-preview"
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
    assert session.messages == [None]
    assert CANARY_SECRET not in repr(connector.requests[0])
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())
    assert base64.b64encode(b"\x01\x02\x03\x04").decode("ascii") not in json.dumps(
        result.redacted_report_dict()
    )


def test_outbound_schedule_uses_activity_messages_and_pcm_without_payload_mutation() -> None:
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

    assert session.sent[1] == {"realtimeInput": {"activityStart": {}}}
    assert session.sent[2]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
    assert base64.b64decode(session.sent[2]["realtimeInput"]["audio"]["data"]) == (
        b"\x00\x00" * 319
    )
    assert session.sent[3] == {"realtimeInput": {"activityEnd": {}}}


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
            _plan(),
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
    activity_starts = [
        index
        for index, value in enumerate(session.sent)
        if value == {"realtimeInput": {"activityStart": {}}}
    ]
    assert result.complete is True
    assert tool_index < activity_starts[1]


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
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    campaign, attempt = _approval_envelopes(private, ledger_path)
    order: list[str] = []
    session = FakeSession([{"setupComplete": {}}, _server_event(), _usage_message(), None])
    no_speech_session = FakeSession(
        [{"setupComplete": {}}, _server_event(), _usage_message(), None]
    )
    connector = FakeConnector([session, no_speech_session])
    custodian = X25519PrivateKey.generate()
    custodian_public = custodian.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    capsules: list[dict[str, object]] = []

    class RecordingLedger:
        def __init__(self) -> None:
            self.delegate = AttemptLedger(ledger_path)

        def claim_attempt(self, **kwargs):
            order.append("claim")
            return self.delegate.claim_attempt(**kwargs)

        def record_outcome(self, *args, **kwargs):
            order.append("outcome")
            return self.delegate.record_outcome(*args, **kwargs)

    def source_identity_check() -> None:
        order.append("source")

    def credential_loader(reference: str) -> SecretCredential:
        order.append("credential:" + reference)
        return SecretCredential(CANARY_SECRET)

    def connector_factory(_credential: SecretCredential):
        order.append("connector")
        return connector

    async def capsule_sink(envelope):
        order.append("capsule")
        capsules.append(envelope)
        return CapsuleHandoffReceipt(sha256(canonical_json_bytes(envelope)).hexdigest())

    result = asyncio.run(
        execute_authorized_attempt(
            (_plan(),),
            no_speech_plans=(_no_speech_plan(),),
            config=AuthorizedAttemptConfig(
                preregistration_sha256=PREREGISTRATION_SHA,
                source_sha=SOURCE_SHA,
                approval_key_id=KEY_ID,
                credential_reference="qualification_secret_v1",
                policy_ms=250,
                whole_run_timeout_seconds=30,
            ),
            session_config=_config(),
            campaign_envelope=campaign,
            attempt_envelope=attempt,
            approval_public_key=public,
            ledger=RecordingLedger(),
            phase=CampaignPhase.DEVELOPMENT_COLLECTION,
            holdout_materialized=False,
            now=NOW,
            source_identity_check=source_identity_check,
            credential_loader=credential_loader,
            connector_factory=connector_factory,
            receipt_clock_factory=lambda _plan: ReceiptClock([0, 120, 130]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
            custodian_public_key=custodian_public,
            custodian_key_id="audit_custodian_1",
            capsule_sink=capsule_sink,
        )
    )

    assert result.complete is True
    assert result.capsule_handed_off is True
    assert result.provider_request_count == 2
    assert result.cost_microusd == 150
    assert order == [
        "claim",
        "source",
        "credential:qualification_secret_v1",
        "connector",
        "connector",
        "capsule",
        "outcome",
    ]
    opened = open_audit_capsule(
        capsules[0],
        custodian_private_key=custodian,
        expected_key_id="audit_custodian_1",
    )
    assert opened["activities"][0]["reference_text"] == "book service today 1"
    assert opened["no_speech_windows"][0]["wire_facts"][0]["kind"] == "false_activity"
    assert CANARY_SECRET not in json.dumps(capsules[0])
    assert CANARY_SECRET not in json.dumps(result.redacted_report_dict())
    assert not hasattr(result, "audit_events")
    snapshot = AttemptLedger(ledger_path).snapshot()
    assert [record["event"] for record in snapshot["records"]] == ["claim", "outcome"]
    assert snapshot["records"][-1]["actual_cost_microusd"] == 150


def test_invalid_approval_never_reads_secret_or_constructs_connector(tmp_path: Path) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    campaign, attempt = _approval_envelopes(private, ledger_path)
    campaign["signature"] = base64.b64encode(b"invalid").decode("ascii")
    touched: list[str] = []

    with pytest.raises(ValueError, match="signature"):
        asyncio.run(
            execute_authorized_attempt(
                (_plan(),),
                config=AuthorizedAttemptConfig(
                    preregistration_sha256=PREREGISTRATION_SHA,
                    source_sha=SOURCE_SHA,
                    approval_key_id=KEY_ID,
                    credential_reference="qualification_secret_v1",
                    policy_ms=250,
                    whole_run_timeout_seconds=30,
                ),
                session_config=_config(),
                campaign_envelope=campaign,
                attempt_envelope=attempt,
                approval_public_key=public,
                ledger=AttemptLedger(ledger_path),
                phase=CampaignPhase.DEVELOPMENT_COLLECTION,
                holdout_materialized=False,
                now=NOW,
                source_identity_check=lambda: touched.append("source"),
                credential_loader=lambda _reference: touched.append("credential"),
                connector_factory=lambda _credential: touched.append("connector"),
                receipt_clock_factory=lambda _plan: ReceiptClock([]),
                sleep_ms=lambda _value: asyncio.sleep(0),
                pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
                custodian_public_key=b"0" * 32,
                custodian_key_id="audit_custodian_1",
                capsule_sink=lambda _capsule: None,
            )
        )

    assert touched == []
    assert not ledger_path.exists()


def test_connector_failure_consumes_request_records_outcome_and_never_retries(
    tmp_path: Path,
) -> None:
    private, public = _key_pair()
    ledger_path = tmp_path / "attempt-ledger.json"
    campaign, attempt = _approval_envelopes(private, ledger_path)
    connector_attempts = 0

    def connector_factory(_credential: SecretCredential):
        nonlocal connector_attempts
        connector_attempts += 1
        raise RuntimeError("transport detail " + CANARY_SECRET)

    result = asyncio.run(
        execute_authorized_attempt(
            (_plan(),),
            config=AuthorizedAttemptConfig(
                preregistration_sha256=PREREGISTRATION_SHA,
                source_sha=SOURCE_SHA,
                approval_key_id=KEY_ID,
                credential_reference="qualification_secret_v1",
                policy_ms=250,
                whole_run_timeout_seconds=30,
            ),
            session_config=_config(),
            campaign_envelope=campaign,
            attempt_envelope=attempt,
            approval_public_key=public,
            ledger=AttemptLedger(ledger_path),
            phase=CampaignPhase.DEVELOPMENT_COLLECTION,
            holdout_materialized=False,
            now=NOW,
            source_identity_check=lambda: None,
            credential_loader=lambda _reference: SecretCredential(CANARY_SECRET),
            connector_factory=connector_factory,
            receipt_clock_factory=lambda _plan: ReceiptClock([]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
            custodian_public_key=X25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
            custodian_key_id="audit_custodian_1",
            capsule_sink=lambda _capsule: None,
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
    campaign, attempt = _approval_envelopes(private, ledger_path)

    async def expire(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(runner_module, "_execute_attempt_work", expire)
    result = asyncio.run(
        execute_authorized_attempt(
            (_plan(),),
            config=AuthorizedAttemptConfig(
                preregistration_sha256=PREREGISTRATION_SHA,
                source_sha=SOURCE_SHA,
                approval_key_id=KEY_ID,
                credential_reference="qualification_secret_v1",
                policy_ms=250,
                whole_run_timeout_seconds=30,
            ),
            session_config=_config(),
            campaign_envelope=campaign,
            attempt_envelope=attempt,
            approval_public_key=public,
            ledger=AttemptLedger(ledger_path),
            phase=CampaignPhase.DEVELOPMENT_COLLECTION,
            holdout_materialized=False,
            now=NOW,
            source_identity_check=lambda: None,
            credential_loader=lambda _reference: SecretCredential(CANARY_SECRET),
            connector_factory=lambda _credential: pytest.fail("connector must not be built"),
            receipt_clock_factory=lambda _plan: ReceiptClock([]),
            sleep_ms=lambda _value: asyncio.sleep(0),
            pricing=load_pricing(Path("tests/fixtures/caller_turn_qualification/pricing.json")),
            custodian_public_key=X25519PrivateKey.generate()
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw),
            custodian_key_id="audit_custodian_1",
            capsule_sink=lambda _capsule: None,
        )
    )

    assert result.error_code == "whole_run_timeout"
    assert result.provider_request_count == 0
    outcome = AttemptLedger(ledger_path).snapshot()["records"][-1]
    assert outcome["outcome"] == "failed"
    assert outcome["actual_provider_requests"] == 0


def test_cli_execute_is_hard_blocked_and_dry_run_has_no_connector() -> None:
    assert main([]) == 0
    assert main(["--execute"]) == 2
