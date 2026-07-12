"""Default-off shadow integration for the receptionist controller."""

from dataclasses import asdict
import json
import logging
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.config import settings
from app.db.contractors import PROTECTED_FIELDS
from app.services.dialogue_planner import ActionName
from app.services.gemini_pipeline import GeminiPipeline
from app.services.receptionist_controller import ShadowReceptionistController
from app.services.receptionist_state import ServiceAction


async def _noop(*_args, **_kwargs):
    return None


def test_shadow_account_allowlist_is_server_protected():
    assert "receptionist_controller_shadow_enabled" in PROTECTED_FIELDS


def _config(account_flag=True):
    return {
        "contractor_id": "contractor-test",
        "business_name": "Example Services",
        "owner_name": "Owner",
        "receptionist_controller_shadow_enabled": account_flag,
    }


def _pipeline(config):
    return GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="test-call",
        contractor_config=config,
        caller_phone="caller-id-ending-8667",
    )


class _OneMessageWebSocket:
    def __init__(self, message):
        self._messages = [json.dumps(message)]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


def test_shadow_decision_contains_metrics_only_not_private_turn_context():
    controller = ShadowReceptionistController.new(
        call_sid="test-call",
        caller_phone="caller-id-ending-8667",
        contractor_config={"known_caller_name": "Private Caller"},
    )

    decision = controller.observe_caller_turn(
        "Private Caller needs a toilet replacement at a private address."
    )

    assert decision.action_name == ActionName.ASK_ONE_CLARIFYING_QUESTION
    assert decision.turn_id == 1
    assert decision.known_fact_count == 2
    assert decision.instruction_chars > 0
    assert controller.state.caller_phone_last_four == "8667"
    assert controller.state.caller_identity.name == "Private Caller"
    serialized = json.dumps(asdict(decision))
    assert "Private Caller" not in serialized
    assert "private address" not in serialized
    assert "8667" not in serialized


def test_shadow_caller_continuation_amends_state_without_second_decision():
    controller = ShadowReceptionistController.new(call_sid="test-call")

    first = controller.observe_caller_turn("I need plumbing help.")
    continuation = controller.observe_caller_turn("It is a toilet replacement.")

    assert first is not None
    assert first.turn_id == 1
    assert continuation is None
    assert controller.pending_turn_id == 1
    assert controller.state.service_object == "toilet"
    assert controller.state.service_action == ServiceAction.REPLACE
    assert controller.pending_action is not None
    assert controller.pending_action.allowed_slots == ("job_complexity",)


@pytest.mark.parametrize(
    ("global_enabled", "account_flag"),
    [
        (False, True),
        (True, False),
        (True, "true"),
    ],
)
def test_shadow_controller_requires_global_and_exact_account_opt_in(
    monkeypatch,
    global_enabled,
    account_flag,
):
    monkeypatch.setattr(
        settings,
        "receptionist_controller_shadow_enabled",
        global_enabled,
        raising=False,
    )

    pipeline = _pipeline(_config(account_flag))

    assert pipeline._receptionist_controller is None


@pytest.mark.asyncio
async def test_enabled_shadow_observes_final_turn_without_sending_or_changing_prompt(
    monkeypatch,
    caplog,
):
    class RecordingWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

    events = []
    transcripts = []

    async def record_transcript(speaker, text):
        events.append("transcript")
        transcripts.append((speaker, text))

    monkeypatch.setattr(
        settings,
        "receptionist_controller_shadow_enabled",
        True,
        raising=False,
    )
    config = _config(True)
    config["known_caller_name"] = "Private Caller"
    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=record_transcript,
        call_sid="test-call",
        contractor_config=config,
        caller_phone="caller-id-ending-8667",
    )
    websocket = RecordingWebSocket()
    pipeline._ws = websocket
    pipeline._connected = True
    pipeline._caller_transcript_buf = [
        "Private Caller needs a toilet replacement at a private address."
    ]
    observe_caller_turn = pipeline._receptionist_controller.observe_caller_turn

    def record_observation(text):
        events.append("shadow")
        return observe_caller_turn(text)

    monkeypatch.setattr(
        pipeline._receptionist_controller,
        "observe_caller_turn",
        record_observation,
    )
    original_prompt = pipeline._system_prompt

    with caplog.at_level(logging.INFO, logger="app.services.gemini_pipeline"):
        await pipeline._flush_caller_transcript()

    assert pipeline._receptionist_controller is not None
    assert pipeline._receptionist_controller.state.service_action == ServiceAction.REPLACE
    assert events == ["transcript", "shadow"]
    assert transcripts == [
        (
            "Caller",
            "Private Caller needs a toilet replacement at a private address.",
        )
    ]
    assert websocket.sent == []
    assert pipeline._system_prompt == original_prompt
    assert pipeline._assistant_instruction_pending is False

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_event event=controller_shadow_decision" in messages
    assert "action=ask_one_clarifying_question" in messages
    assert "Private Caller" not in messages
    assert "private address" not in messages
    assert "8667" not in messages


@pytest.mark.asyncio
async def test_shadow_observation_runs_after_live_urgency_detection(monkeypatch):
    monkeypatch.setattr(
        settings,
        "receptionist_controller_shadow_enabled",
        True,
        raising=False,
    )
    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        on_urgency_detected=_noop,
        call_sid="test-call",
        contractor_config=_config(True),
        caller_phone="caller-id-ending-8667",
    )
    urgency_state_at_observation = []
    observe_caller_turn = pipeline._receptionist_controller.observe_caller_turn

    def record_observation(text):
        urgency_state_at_observation.append(pipeline._urgency_detected)
        return observe_caller_turn(text)

    monkeypatch.setattr(
        pipeline._receptionist_controller,
        "observe_caller_turn",
        record_observation,
    )
    pipeline._caller_transcript_buf = ["This is an emergency."]

    await pipeline._flush_caller_transcript()

    assert urgency_state_at_observation == [True]


@pytest.mark.asyncio
async def test_completed_live_assistant_turn_commits_pending_shadow_slot(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(
        settings,
        "receptionist_controller_shadow_enabled",
        True,
        raising=False,
    )
    pipeline = _pipeline(_config(True))
    pipeline._connected = True
    pipeline._caller_transcript_buf = ["I need plumbing help."]
    await pipeline._flush_caller_transcript()
    pipeline._kevin_transcript_buf = ["Is this a repair or replacement?"]
    pipeline._ws = _OneMessageWebSocket({"serverContent": {"turnComplete": True}})

    with caplog.at_level(logging.INFO, logger="app.services.gemini_pipeline"):
        await pipeline._receive_loop()

    assert pipeline._receptionist_controller.state.asked_slots == {
        "service_action"
    }
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=controller_shadow_assistant_turn" in messages
    assert "turn_id=1" in messages
    assert "interrupted=False" in messages
    assert "committed_slot_count=1" in messages
    assert "repair or replacement" not in messages


@pytest.mark.asyncio
async def test_fragmented_live_caller_turn_emits_one_shadow_decision(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(
        settings,
        "receptionist_controller_shadow_enabled",
        True,
        raising=False,
    )
    pipeline = _pipeline(_config(True))
    pipeline._connected = True

    with caplog.at_level(logging.INFO, logger="app.services.gemini_pipeline"):
        pipeline._caller_transcript_buf = ["I need plumbing help."]
        await pipeline._flush_caller_transcript()
        pipeline._caller_transcript_buf = ["It is a toilet replacement."]
        await pipeline._flush_caller_transcript()

    assert pipeline._receptionist_controller is not None
    assert pipeline._receptionist_controller.pending_turn_id == 1
    assert pipeline._receptionist_controller.state.service_object == "toilet"
    assert (
        pipeline._receptionist_controller.state.service_action
        == ServiceAction.REPLACE
    )
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert messages.count("event=controller_shadow_decision") == 1
    assert messages.count("event=controller_shadow_caller_amendment") == 1
    assert "turn_id=1" in messages


@pytest.mark.asyncio
async def test_interrupted_live_assistant_turn_does_not_commit_shadow_slot(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(
        settings,
        "receptionist_controller_shadow_enabled",
        True,
        raising=False,
    )
    pipeline = _pipeline(_config(True))
    pipeline._connected = True
    pipeline._caller_transcript_buf = ["I need plumbing help."]
    await pipeline._flush_caller_transcript()
    pipeline._ws = _OneMessageWebSocket({"serverContent": {"interrupted": True}})

    with caplog.at_level(logging.INFO, logger="app.services.gemini_pipeline"):
        await pipeline._receive_loop()

    assert pipeline._receptionist_controller.state.asked_slots == set()
    assert pipeline._receptionist_controller.pending_turn_id is None
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=controller_shadow_assistant_turn" in messages
    assert "turn_id=1" in messages
    assert "interrupted=True" in messages
    assert "committed_slot_count=0" in messages


@pytest.mark.asyncio
async def test_shadow_error_disables_controller_for_call_without_logging_payload(monkeypatch, caplog):
    private_error = "controller failed for Private Caller at a private address"
    monkeypatch.setattr(
        settings,
        "receptionist_controller_shadow_enabled",
        True,
        raising=False,
    )
    pipeline = _pipeline(_config(True))

    def fail_observation(_text):
        raise RuntimeError(private_error)

    monkeypatch.setattr(
        pipeline._receptionist_controller,
        "observe_caller_turn",
        fail_observation,
    )
    pipeline._caller_transcript_buf = ["Private Caller needs help at a private address."]

    with caplog.at_level(logging.ERROR, logger="app.services.gemini_pipeline"):
        await pipeline._flush_caller_transcript()

    assert pipeline._receptionist_controller is None
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_event event=controller_shadow_error" in messages
    assert "exception_type=RuntimeError" in messages
    assert private_error not in messages
    assert "Private Caller" not in messages
    assert "private address" not in messages
