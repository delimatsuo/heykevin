"""Live wiring tests for the staging-only controlled Gemini pipeline."""

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
from app.services.gemini_controlled_turn import SpokenTurn, ValidatedTurn
from app.services.receptionist_state import (
    CallbackConfirmation,
    CallbackIntent,
    CallerObservation,
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
async def test_live_controller_plans_before_requesting_spoken_turn(monkeypatch):
    pipeline = _pipeline()
    events = []

    class FakeGenerator:
        async def extract_observation(self, **_kwargs):
            events.append("observation")
            return CallerObservation(
                caller_name="Fixture Caller",
                service_object="sink",
                service_action=ServiceAction.REPAIR,
                urgency=Urgency.ROUTINE,
            )

        async def generate_turn(self, *, action, **_kwargs):
            events.append(f"plan:{action.name.value}")
            slot = action.allowed_slots[0]
            return ValidatedTurn(
                SpokenTurn(
                    action=action.name,
                    expects_input=True,
                    asked_slot=slot,
                    spoken_text="How extensive is the sink issue?",
                    safety_complete=False,
                ),
                repaired=False,
                fallback=False,
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
