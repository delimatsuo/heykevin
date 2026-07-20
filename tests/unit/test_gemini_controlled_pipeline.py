"""Live wiring tests for the staging-only controlled Gemini pipeline."""

import asyncio
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "AC_TEST")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")

from app.services.gemini_controlled_pipeline import (
    GeminiControlledPipeline,
    _PendingSpeechContract,
)
from app.services.gemini_controlled_turn import (
    ControlledObservation,
    DirectAnswerKind,
    PresenceReplyKind,
)
from app.services.receptionist_state import (
    CallbackConfirmation,
    CallbackIntent,
    CallerObservation,
    Intent,
    ServiceAction,
    Urgency,
)
from app.services.voice_pipeline import VoicePipeline
from app.services.voice_turn_coordinator import PlaybackReceipt, PlaybackStatus, TurnLifecycle


async def _noop(*_args, **_kwargs):
    return None


def _pipeline(*, on_call_complete=_noop) -> GeminiControlledPipeline:
    return GeminiControlledPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        on_call_complete=on_call_complete,
        call_sid="CA_private",
        contractor_config={
            "effective_mode": "business",
            "owner_name": "Owner",
            "business_name": "Test Plumbing",
        },
        caller_phone="+15555550123",
    )


@pytest.mark.asyncio
async def test_controlled_cohort_never_prefetches_crm_context():
    pipeline = _pipeline()
    assert pipeline._should_prefetch_jobber() is False
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_generated_question_contract_is_registered_before_tts(monkeypatch):
    pipeline = _pipeline()
    spoken = []

    async def fake_base_speak(self, text, **_kwargs):
        spoken.append(text)

    monkeypatch.setattr(VoicePipeline, "_speak", fake_base_speak)
    assert pipeline._turn_coordinator.begin_generation(1)
    pipeline._pending_speech_contract = _PendingSpeechContract(
        expects_input=True,
        asked_slot="service_action",
        kind="model",
    )

    await pipeline._speak(
        "Do you need a repair or replacement?",
        source="model",
        caller_turn=1,
    )

    assert spoken == ["Do you need a repair or replacement?"]
    assert pipeline._turn_coordinator.current_response_turn == 1
    assert pipeline._turn_coordinator.state == TurnLifecycle.PLAYING
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_played_receipt_commits_slot_but_cleared_receipt_does_not():
    pipeline = _pipeline()
    coordinator = pipeline._turn_coordinator
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=True,
        asked_slot="service_action",
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "response_end", PlaybackStatus.CLEARED)
    )
    assert "service_action" not in pipeline._intake_state.asked_slots

    coordinator.caller_activity()
    assert coordinator.begin_generation(2)
    assert coordinator.begin_playback(
        response_turn=2,
        caller_turn=2,
        expects_input=True,
        asked_slot="service_action",
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(2, 2, "response_end", PlaybackStatus.PLAYED)
    )
    assert "service_action" in pipeline._intake_state.asked_slots
    assert coordinator.state == TurnLifecycle.AWAITING_REPLY
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_callback_confirmation_is_accepted_only_after_its_question_played():
    pipeline = _pipeline()
    pipeline._intake_state.callback_intent = CallbackIntent.REQUESTED

    unauthorized = pipeline._authorize_observation(
        CallerObservation(callback_confirmation=CallbackConfirmation.CONFIRMED)
    )
    assert unauthorized.callback_confirmation is None

    coordinator = pipeline._turn_coordinator
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=True,
        asked_slot="callback_confirmation",
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "response_end", PlaybackStatus.PLAYED)
    )
    authorized = pipeline._authorize_observation(
        CallerObservation(callback_confirmation=CallbackConfirmation.CONFIRMED)
    )
    assert authorized.callback_confirmation == CallbackConfirmation.CONFIRMED
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_close_callback_is_authorized_only_by_matching_played_receipt():
    completed = []

    async def on_complete():
        completed.append(True)

    pipeline = _pipeline(on_call_complete=on_complete)
    coordinator = pipeline._turn_coordinator
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=4,
        caller_turn=1,
        expects_input=False,
        close_after_playback=True,
    )

    await pipeline.on_playback_receipt(
        PlaybackReceipt(4, 4, "first_media", PlaybackStatus.PLAYED)
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(3, 3, "response_end", PlaybackStatus.PLAYED)
    )
    assert completed == []
    await pipeline.on_playback_receipt(
        PlaybackReceipt(4, 4, "response_end", PlaybackStatus.PLAYED)
    )
    assert completed == [True]
    assert coordinator.state == TurnLifecycle.ENDED
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_tts_or_mark_failure_returns_coordinator_to_listening(monkeypatch):
    pipeline = _pipeline()

    async def failed_base_speak(*_args, **_kwargs):
        return False

    monkeypatch.setattr(VoicePipeline, "_speak", failed_base_speak)
    assert pipeline._turn_coordinator.begin_generation(1)
    pipeline._pending_speech_contract = _PendingSpeechContract(
        expects_input=True,
        asked_slot="service_action",
        kind="model",
    )

    delivered = await pipeline._speak(
        "Do you need a repair or replacement?",
        source="model",
        caller_turn=1,
    )

    assert delivered is False
    assert pipeline._turn_coordinator.state == TurnLifecycle.LISTENING
    assert pipeline._turn_coordinator.current_response_turn is None
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_presence_and_silence_close_follow_detected_spanish(monkeypatch):
    pipeline = _pipeline()
    pipeline._intake_state.language = "es"
    spoken = []

    async def capture_speak(text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(pipeline, "_speak", capture_speak)

    await pipeline._speak_presence_check()
    await pipeline._speak_silence_close()

    assert spoken == [
        "¿Sigue ahí?",
        "Voy a colgar por ahora. Llame de nuevo cuando pueda. Adiós.",
    ]
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_deepgram_language_switch_updates_deterministic_recovery_language():
    pipeline = _pipeline()

    await pipeline._switch_language("es-US")

    assert pipeline._language == "es"
    assert pipeline._intake_state.language == "es"
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_presence_ack_replays_the_exact_last_played_question(monkeypatch):
    pipeline = _pipeline()
    coordinator = pipeline._turn_coordinator
    question = "Is the number ending in 1-2-3-4 best for the callback?"
    pipeline._last_played_question = (question, "callback_confirmation")

    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=True,
        asked_slot="callback_confirmation",
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "response_end", PlaybackStatus.PLAYED)
    )
    coordinator._deadline = 0
    assert coordinator.due_action().value == "reprompt"
    assert coordinator.begin_playback(
        response_turn=2,
        caller_turn=1,
        expects_input=True,
        kind="reprompt",
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(2, 2, "response_end", PlaybackStatus.PLAYED)
    )
    pipeline._mark_caller_activity()
    pipeline._caller_turn_number = 2
    spoken = []

    class PresenceGenerator:
        async def extract_observation(self, **kwargs):
            assert kwargs["presence_check_active"] is True
            assert kwargs["suspended_slot"] == "callback_confirmation"
            return ControlledObservation(
                facts=CallerObservation(
                    callback_confirmation=CallbackConfirmation.CONFIRMED
                ),
                presence_reply_kind=PresenceReplyKind.ACKNOWLEDGEMENT,
            )

    async def capture_speak(text, **_kwargs):
        spoken.append(text)
        return True

    pipeline._turn_generator = PresenceGenerator()
    monkeypatch.setattr(pipeline, "_speak", capture_speak)
    await pipeline._handle_caller_speech(
        "Sure, I'm listening.",
        caller_turn=2,
        committed_at=1.0,
    )

    assert spoken == [question]
    assert pipeline._intake_state.callback_confirmation == CallbackConfirmation.UNKNOWN
    assert pipeline._pending_reply_slot == ""
    assert pipeline._pending_speech_contract == _PendingSpeechContract(
        expects_input=True,
        asked_slot="callback_confirmation",
        question_text=question,
        kind="question_replay",
    )
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_typed_substantive_presence_reply_can_answer_suspended_slot(monkeypatch):
    pipeline = _pipeline()
    pipeline._intake_state.callback_intent = CallbackIntent.REQUESTED
    coordinator = pipeline._turn_coordinator
    question = "Is the number ending in 1-2-3-4 best for the callback?"
    pipeline._last_played_question = (question, "callback_confirmation")

    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=True,
        asked_slot="callback_confirmation",
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "response_end", PlaybackStatus.PLAYED)
    )
    coordinator._deadline = 0
    assert coordinator.due_action().value == "reprompt"
    assert coordinator.begin_playback(
        response_turn=2,
        caller_turn=1,
        expects_input=True,
        kind="reprompt",
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(2, 2, "response_end", PlaybackStatus.PLAYED)
    )
    pipeline._mark_caller_activity()
    pipeline._caller_turn_number = 2

    class PresenceGenerator:
        async def extract_observation(self, **_kwargs):
            return ControlledObservation(
                facts=CallerObservation(
                    callback_confirmation=CallbackConfirmation.CONFIRMED
                ),
                presence_reply_kind=PresenceReplyKind.SUBSTANTIVE,
            )

    spoken = []

    async def capture_speak(text, **_kwargs):
        spoken.append(text)
        return True

    pipeline._turn_generator = PresenceGenerator()
    monkeypatch.setattr(pipeline, "_speak", capture_speak)

    await pipeline._handle_caller_speech(
        "Yes, that number is correct.",
        caller_turn=2,
        committed_at=1.0,
    )

    assert pipeline._intake_state.callback_confirmation == CallbackConfirmation.CONFIRMED
    assert spoken != [question]
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_new_speech_during_presence_resolution_restarts_typed_context(monkeypatch):
    pipeline = _pipeline()
    coordinator = pipeline._turn_coordinator
    question = "Is the number ending in 1-2-3-4 best for the callback?"
    pipeline._last_played_question = (question, "callback_confirmation")

    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=True,
        asked_slot="callback_confirmation",
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "response_end", PlaybackStatus.PLAYED)
    )
    coordinator._deadline = 0
    assert coordinator.due_action().value == "reprompt"
    assert coordinator.begin_playback(
        response_turn=2,
        caller_turn=1,
        expects_input=True,
        kind="reprompt",
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(2, 2, "response_end", PlaybackStatus.PLAYED)
    )
    pipeline._mark_caller_activity()
    assert coordinator.begin_presence_resolution(2)
    pipeline._presence_reply_pending = False

    in_flight = asyncio.create_task(asyncio.sleep(30))
    pipeline._active_generation_task = in_flight
    pipeline._mark_caller_activity()
    await asyncio.sleep(0)

    assert in_flight.cancelled()
    assert coordinator.state == TurnLifecycle.LISTENING
    assert pipeline._presence_reply_pending is True

    class PresenceGenerator:
        async def extract_observation(self, **kwargs):
            assert kwargs["presence_check_active"] is True
            return ControlledObservation(
                facts=CallerObservation(),
                presence_reply_kind=PresenceReplyKind.ACKNOWLEDGEMENT,
            )

    spoken = []

    async def capture_speak(text, **_kwargs):
        spoken.append(text)
        return True

    pipeline._active_generation_task = None
    pipeline._turn_generator = PresenceGenerator()
    pipeline._caller_turn_number = 3
    monkeypatch.setattr(pipeline, "_speak", capture_speak)

    await pipeline._handle_caller_speech(
        "I can hear you now.",
        caller_turn=3,
        committed_at=1.0,
    )

    assert spoken == [question]
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_owner_unavailable_replaces_pending_question_contract(monkeypatch):
    pipeline = _pipeline()
    coordinator = pipeline._turn_coordinator
    assert coordinator.begin_generation(1)
    assert coordinator.begin_playback(
        response_turn=1,
        caller_turn=1,
        expects_input=True,
        asked_slot="callback_confirmation",
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "response_end", PlaybackStatus.PLAYED)
    )
    pipeline._response_turn_number = 1
    spoken = []

    async def capture_base_speak(_self, text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)
    await pipeline._unavailable_now()

    assert spoken == [
        "I'm sorry, Owner isn't available right now. "
        "What message would you like me to pass along?"
    ]
    assert coordinator.state == TurnLifecycle.PLAYING
    assert pipeline._pending_reply_slot == ""
    await pipeline.on_playback_receipt(
        PlaybackReceipt(2, 2, "response_end", PlaybackStatus.PLAYED)
    )
    assert pipeline._pending_reply_slot == "message_details"
    assert coordinator.state == TurnLifecycle.AWAITING_REPLY
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_live_controller_plans_before_requesting_spoken_turn(monkeypatch):
    pipeline = _pipeline()
    events = []

    class FakeGenerator:
        async def extract_observation(self, **_kwargs):
            events.append("observation")
            return ControlledObservation(
                facts=CallerObservation(
                    caller_name="Fixture Caller",
                    service_object="sink",
                    service_action=ServiceAction.REPAIR,
                    urgency=Urgency.ROUTINE,
                )
            )

    async def fake_speak(*_args, **_kwargs):
        events.append("tts")

    pipeline._turn_generator = FakeGenerator()
    monkeypatch.setattr(pipeline, "_speak", fake_speak)
    pipeline._caller_turn_number = 1
    await pipeline._handle_caller_speech(
        "The sink needs repair.",
        caller_turn=1,
        committed_at=1.0,
    )

    assert events == [
        "observation",
        "tts",
    ]
    assert pipeline._pending_speech_contract is not None
    assert pipeline._pending_speech_contract.asked_slot == "job_complexity"
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_pricing_answer_uses_same_observation_call_then_server_question(monkeypatch):
    pipeline = _pipeline()
    events = []

    async def extract_once(**_kwargs):
        events.append("observation")
        return ControlledObservation(
            facts=CallerObservation(
                intent=Intent.PRICING_QUESTION,
                service_object="sink",
                service_action=ServiceAction.REPAIR,
            ),
            direct_answer_kind=DirectAnswerKind.PRICING_REQUIRES_REVIEW,
        )

    spoken = []

    async def capture_speak(text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(pipeline._turn_generator, "extract_observation", extract_once)
    monkeypatch.setattr(pipeline, "_speak", capture_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech(
        "How much is a sink repair?",
        caller_turn=1,
        committed_at=1.0,
    )

    assert events == ["observation"]
    assert spoken == [
        "Pricing depends on the work involved. "
        "Could you briefly describe how extensive the issue is?"
    ]
    assert pipeline._pending_speech_contract is not None
    assert pipeline._pending_speech_contract.question_text == (
        "Could you briefly describe how extensive the issue is?"
    )
    await pipeline._http_client.aclose()
