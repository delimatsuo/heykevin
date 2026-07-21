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
    CallerTurnCompleteness,
    ControlledObservation,
    DirectAnswerKind,
    DirectQuestionAssessment,
    DirectQuestionTopic,
    PresenceReplyKind,
)
from app.services.receptionist_state import (
    BusinessScope,
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


def _pipeline(
    *,
    on_call_complete=_noop,
    on_response_first_media_sent=None,
) -> GeminiControlledPipeline:
    return GeminiControlledPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        on_call_complete=on_call_complete,
        on_response_first_media_sent=on_response_first_media_sent,
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
async def test_semantic_episode_fix_does_not_change_endpointing_or_output_pacing():
    pipeline = _pipeline()

    assert pipeline.DEEPGRAM_ENDPOINTING_MS == 400
    assert pipeline.ELEVENLABS_PACING_RATIO == 0.9
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

    async def analyze_once(**_kwargs):
        events.append("observation")
        return (
            ControlledObservation(
                facts=CallerObservation(
                    intent=Intent.PRICING_QUESTION,
                    service_object="sink",
                    service_action=ServiceAction.REPAIR,
                ),
                direct_answer_kind=DirectAnswerKind.PRICING_REQUIRES_REVIEW,
            ),
            DirectQuestionAssessment(topic=DirectQuestionTopic.PRICING),
        )

    spoken = []

    async def capture_speak(text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_once)
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


@pytest.mark.asyncio
async def test_scope_question_is_answered_before_the_next_intake_question(monkeypatch):
    pipeline = _pipeline()

    async def analyze_once(**_kwargs):
        return (
            ControlledObservation(
                facts=CallerObservation(
                    business_scope=BusinessScope.IN_SCOPE,
                    intent=Intent.SERVICE_REQUEST,
                    service_object="toilet",
                    service_action=ServiceAction.REPLACE,
                ),
                direct_answer_kind=DirectAnswerKind.SCOPE_SUPPORTED,
            ),
            DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
        )

    spoken = []

    async def capture_speak(text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_once)
    monkeypatch.setattr(pipeline, "_speak", capture_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech(
        "Do you replace toilets?",
        caller_turn=1,
        committed_at=1.0,
    )

    assert spoken == [
        "Yes, this business handles that type of work. May I have your name?"
    ]
    assert pipeline._pending_speech_contract is not None
    assert pipeline._pending_speech_contract.asked_slot == "caller_name"
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_fragmented_scope_question_is_assembled_before_any_audio_or_state_commit(
    monkeypatch,
):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript
    seen_texts = []

    async def analyze_turn(**kwargs):
        seen_texts.append(kwargs["caller_text"])
        if len(seen_texts) == 1:
            return (
                ControlledObservation(facts=CallerObservation()),
                DirectQuestionAssessment(
                    topic=DirectQuestionTopic.SERVICE_SCOPE,
                    completeness=CallerTurnCompleteness.INCOMPLETE,
                ),
            )
        return (
            ControlledObservation(
                facts=CallerObservation(
                    business_scope=BusinessScope.IN_SCOPE,
                    intent=Intent.SERVICE_REQUEST,
                    service_object="fixture",
                    service_action=ServiceAction.REPLACE,
                ),
                direct_answer_kind=DirectAnswerKind.SCOPE_SUPPORTED,
            ),
            DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
        )

    sent_audio = []

    async def capture_base_speak(_self, text, **_kwargs):
        sent_audio.append(text)
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)

    pipeline._caller_turn_number = 1
    await pipeline._handle_caller_speech(
        "Do you handle",
        caller_turn=1,
        committed_at=1.0,
    )

    assert sent_audio == []
    assert transcripts == []
    assert pipeline._intake_state.service_object == ""
    assert pipeline._turn_coordinator.state == TurnLifecycle.LISTENING

    pipeline._caller_turn_number = 2
    pipeline._mark_caller_activity()
    await pipeline._handle_caller_speech(
        "fixture replacement?",
        caller_turn=2,
        committed_at=2.0,
    )

    assert seen_texts == ["Do you handle", "Do you handle fixture replacement?"]
    assert sent_audio == [
        "Yes, this business handles that type of work. May I have your name?"
    ]
    assert transcripts == []
    # Candidate state remains speculative until the first outbound media chunk.
    assert pipeline._intake_state.service_object == "fixture"

    await pipeline._on_response_first_media_sent(1)
    assert transcripts == []
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "first_media", PlaybackStatus.PLAYED)
    )
    assert transcripts == []
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "response_end", PlaybackStatus.PLAYED)
    )
    assert transcripts == [
        ("Kevin", "Yes, this business handles that type of work. May I have your name?")
    ]
    assert pipeline._intake_state.service_object == "fixture"
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_incomplete_emergency_fragment_bypasses_semantic_deferral(monkeypatch):
    pipeline = _pipeline()

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(facts=CallerObservation()),
            DirectQuestionAssessment(
                completeness=CallerTurnCompleteness.INCOMPLETE,
            ),
        )

    spoken = []

    async def capture_speak(text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(pipeline, "_speak", capture_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech(
        "I smell a gas leak and",
        caller_turn=1,
        committed_at=1.0,
    )

    assert spoken
    assert "leave the area now" in spoken[0].casefold()
    assert pipeline._semantic_episode_settlement_task is None
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_incomplete_semantic_episode_settles_with_one_heard_clarification(
    monkeypatch,
):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript
    monkeypatch.setattr(pipeline, "SEMANTIC_EPISODE_SETTLEMENT_SECONDS", 0)

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(facts=CallerObservation()),
            DirectQuestionAssessment(
                completeness=CallerTurnCompleteness.INCOMPLETE,
            ),
        )

    sent_audio = []

    async def capture_base_speak(_self, text, **_kwargs):
        sent_audio.append(text)
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech("Can you help with", caller_turn=1)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    message = "I'm sorry, could you finish your question?"
    assert sent_audio == [message]
    assert transcripts == []
    await pipeline._on_response_first_media_sent(1)
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "first_media", PlaybackStatus.PLAYED)
    )
    assert transcripts == []
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "response_end", PlaybackStatus.PLAYED)
    )
    assert transcripts == [("Kevin", message)]
    assert pipeline._last_played_question == (message, "")
    assert pipeline._pending_reply_slot == ""
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_later_fragment_cancels_incomplete_episode_settlement(monkeypatch):
    pipeline = _pipeline()
    monkeypatch.setattr(pipeline, "SEMANTIC_EPISODE_SETTLEMENT_SECONDS", 60)

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(facts=CallerObservation()),
            DirectQuestionAssessment(
                completeness=CallerTurnCompleteness.INCOMPLETE,
            ),
        )

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    pipeline._caller_turn_number = 1
    await pipeline._handle_caller_speech("Can you help with", caller_turn=1)
    task = pipeline._semantic_episode_settlement_task
    assert task is not None

    pipeline._caller_turn_number = 2
    pipeline._mark_caller_activity()
    await asyncio.sleep(0)

    assert task.cancelled()
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_settlement_presence_ack_replays_the_exact_settlement_question(monkeypatch):
    pipeline = _pipeline()
    coordinator = pipeline._turn_coordinator
    message = "I'm sorry, could you finish your question?"

    async def capture_base_speak(_self, _text, **_kwargs):
        return True

    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)
    assert coordinator.begin_generation(1)
    assert coordinator.defer_generation(1)
    assert coordinator.begin_semantic_settlement(1)
    pipeline._pending_speech_contract = _PendingSpeechContract(
        expects_input=True,
        question_text=message,
        kind="fallback",
    )
    await pipeline._speak(message, source="fallback", caller_turn=1)
    pipeline._response_turn_number = 1
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "first_media", PlaybackStatus.PLAYED)
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "response_end", PlaybackStatus.PLAYED)
    )
    assert pipeline._last_played_question == (message, "")
    assert coordinator.state == TurnLifecycle.AWAITING_REPLY

    coordinator._deadline = 0
    assert coordinator.due_action().value == "reprompt"
    await pipeline._speak_presence_check()
    await pipeline.on_playback_receipt(
        PlaybackReceipt(2, 2, "response_end", PlaybackStatus.PLAYED)
    )
    pipeline._response_turn_number = 2
    pipeline._mark_caller_activity()

    async def acknowledge_presence(**_kwargs):
        return (
            ControlledObservation(
                facts=CallerObservation(),
                presence_reply_kind=PresenceReplyKind.ACKNOWLEDGEMENT,
            ),
            DirectQuestionAssessment(),
        )

    replayed = []

    async def capture_replay(text, **_kwargs):
        replayed.append(text)
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", acknowledge_presence)
    monkeypatch.setattr(pipeline, "_speak", capture_replay)
    pipeline._caller_turn_number = 2
    await pipeline._handle_caller_speech("Yes", caller_turn=2)

    assert replayed == [message]
    assert pipeline._pending_reply_slot == ""
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_pipeline_stop_cancels_incomplete_episode_settlement(monkeypatch):
    pipeline = _pipeline()
    monkeypatch.setattr(pipeline, "SEMANTIC_EPISODE_SETTLEMENT_SECONDS", 60)

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(facts=CallerObservation()),
            DirectQuestionAssessment(
                completeness=CallerTurnCompleteness.INCOMPLETE,
            ),
        )

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    pipeline._caller_turn_number = 1
    await pipeline._handle_caller_speech("Can you help with", caller_turn=1)
    task = pipeline._semantic_episode_settlement_task
    assert task is not None

    await pipeline.stop()
    await asyncio.sleep(0)

    assert task.cancelled()


@pytest.mark.asyncio
async def test_unheard_presence_resolution_candidate_restores_replay_authority(
    monkeypatch,
):
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
    pipeline._response_turn_number = 2
    pipeline._mark_caller_activity()

    seen_texts = []

    async def analyze_turn(**kwargs):
        seen_texts.append(kwargs["caller_text"])
        return (
            ControlledObservation(
                facts=CallerObservation(
                    callback_confirmation=CallbackConfirmation.CONFIRMED,
                ),
                presence_reply_kind=PresenceReplyKind.SUBSTANTIVE,
            ),
            DirectQuestionAssessment(),
        )

    async def no_media_speak(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(pipeline, "_speak", no_media_speak)

    pipeline._caller_turn_number = 2
    await pipeline._handle_caller_speech("Yes", caller_turn=2)
    assert coordinator.state == TurnLifecycle.GENERATING

    pipeline._caller_turn_number = 3
    pipeline._mark_caller_activity()
    assert pipeline._presence_reply_pending is True
    assert coordinator.state == TurnLifecycle.LISTENING
    await pipeline._handle_caller_speech("the number is correct", caller_turn=3)

    assert seen_texts == ["Yes", "Yes the number is correct"]
    assert coordinator.state == TurnLifecycle.GENERATING
    assert pipeline._intake_state.callback_confirmation == CallbackConfirmation.CONFIRMED
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_owner_message_takeover_discards_an_incomplete_semantic_episode(
    monkeypatch,
):
    pipeline = _pipeline()
    monkeypatch.setattr(pipeline, "SEMANTIC_EPISODE_SETTLEMENT_SECONDS", 60)

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(facts=CallerObservation()),
            DirectQuestionAssessment(
                completeness=CallerTurnCompleteness.INCOMPLETE,
            ),
        )

    spoken = []

    async def capture_base_speak(_self, text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)
    pipeline._caller_turn_number = 1
    await pipeline._handle_caller_speech("Can you help with", caller_turn=1)
    assert pipeline._semantic_episode is not None

    await pipeline._unavailable_now()

    assert pipeline._semantic_episode is None
    assert pipeline._semantic_episode_settlement_task is None
    assert spoken
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "response_end", PlaybackStatus.PLAYED)
    )
    assert pipeline._pending_reply_slot == "message_details"

    pipeline._caller_turn_number = 2
    pipeline._mark_caller_activity()
    assert pipeline._pending_reply_slot == "message_details"
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_superseded_zero_media_candidate_restores_state_and_transcript(monkeypatch):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript
    seen_texts = []

    async def analyze_turn(**kwargs):
        seen_texts.append(kwargs["caller_text"])
        if len(seen_texts) == 1:
            return (
                ControlledObservation(
                    facts=CallerObservation(
                        intent=Intent.PRICING_QUESTION,
                        service_object="fixture",
                        service_action=ServiceAction.REPAIR,
                    ),
                    direct_answer_kind=DirectAnswerKind.PRICING_REQUIRES_REVIEW,
                ),
                DirectQuestionAssessment(topic=DirectQuestionTopic.PRICING),
            )
        return (
            ControlledObservation(
                facts=CallerObservation(
                    business_scope=BusinessScope.IN_SCOPE,
                    intent=Intent.SERVICE_REQUEST,
                    service_object="fixture",
                    service_action=ServiceAction.REPLACE,
                ),
                direct_answer_kind=DirectAnswerKind.SCOPE_SUPPORTED,
            ),
            DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
        )

    candidate_texts = []

    async def no_media_speak(text, **_kwargs):
        candidate_texts.append(text)
        return None

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(pipeline, "_speak", no_media_speak)

    pipeline._caller_turn_number = 1
    await pipeline._handle_caller_speech("How much is it", caller_turn=1)
    assert pipeline._intake_state.intent == Intent.PRICING_QUESTION
    assert transcripts == []

    pipeline._caller_turn_number = 2
    pipeline._mark_caller_activity()
    assert pipeline._intake_state.intent == Intent.UNKNOWN
    await pipeline._handle_caller_speech("for fixture replacement?", caller_turn=2)

    assert seen_texts == [
        "How much is it",
        "How much is it for fixture replacement?",
    ]
    assert len(candidate_texts) == 2
    assert transcripts == []
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_zero_media_delivery_drops_speculative_state_and_transcript(monkeypatch):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(
                facts=CallerObservation(
                    business_scope=BusinessScope.IN_SCOPE,
                    intent=Intent.SERVICE_REQUEST,
                    service_object="fixture",
                    service_action=ServiceAction.REPLACE,
                ),
                direct_answer_kind=DirectAnswerKind.SCOPE_SUPPORTED,
            ),
            DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
        )

    async def no_media_base_speak(*_args, **_kwargs):
        return False

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(VoicePipeline, "_speak", no_media_base_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech("Do you handle fixture replacement?", caller_turn=1)

    assert pipeline._intake_state.service_object == ""
    assert pipeline._conversation == []
    assert transcripts == []
    assert pipeline._turn_coordinator.state == TurnLifecycle.LISTENING
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mark_behavior", ["false", "error"])
async def test_first_media_mark_request_failure_rolls_back_semantic_candidate(
    monkeypatch,
    mark_behavior,
):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    async def request_mark(_turn):
        if mark_behavior == "error":
            raise RuntimeError("mark request unavailable")
        return False

    pipeline = _pipeline(on_response_first_media_sent=request_mark)
    pipeline.on_transcript = capture_transcript

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(
                facts=CallerObservation(
                    business_scope=BusinessScope.IN_SCOPE,
                    intent=Intent.SERVICE_REQUEST,
                    service_object="fixture",
                    service_action=ServiceAction.REPLACE,
                ),
                direct_answer_kind=DirectAnswerKind.SCOPE_SUPPORTED,
            ),
            DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
        )

    async def capture_base_speak(self, _text, **_kwargs):
        self._response_turn_number += 1
        return await self.on_response_first_media_sent(self._response_turn_number)

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech(
        "Do you handle fixture replacement?", caller_turn=1
    )

    assert pipeline._intake_state.service_object == ""
    assert pipeline._semantic_episode is None
    assert pipeline._semantic_episodes_by_response_turn == {}
    assert pipeline._conversation == []
    assert transcripts == []
    assert pipeline._turn_coordinator.state == TurnLifecycle.LISTENING
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [PlaybackStatus.CLEARED, PlaybackStatus.STALE, PlaybackStatus.TIMEOUT],
)
async def test_unplayed_first_media_receipt_rolls_back_semantic_candidate(
    monkeypatch,
    status,
):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(
                facts=CallerObservation(
                    business_scope=BusinessScope.IN_SCOPE,
                    intent=Intent.SERVICE_REQUEST,
                    service_object="fixture",
                    service_action=ServiceAction.REPLACE,
                ),
                direct_answer_kind=DirectAnswerKind.SCOPE_SUPPORTED,
            ),
            DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
        )

    async def capture_base_speak(_self, _text, **_kwargs):
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech(
        "Do you handle fixture replacement?", caller_turn=1
    )
    await pipeline.on_playback_receipt(PlaybackReceipt(1, 1, "first_media", status))

    assert pipeline._intake_state.service_object == ""
    assert pipeline._semantic_episode is None
    assert pipeline._semantic_episodes_by_response_turn == {}
    assert pipeline._conversation == []
    assert transcripts == []
    assert pipeline._turn_coordinator.state == TurnLifecycle.LISTENING
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_caller_continuation_before_first_media_receipt_restores_candidate(
    monkeypatch,
):
    pipeline = _pipeline()
    seen_texts = []

    async def analyze_turn(**kwargs):
        seen_texts.append(kwargs["caller_text"])
        return (
            ControlledObservation(
                facts=CallerObservation(
                    business_scope=BusinessScope.IN_SCOPE,
                    intent=Intent.SERVICE_REQUEST,
                    service_object="fixture",
                    service_action=ServiceAction.REPLACE,
                ),
                direct_answer_kind=DirectAnswerKind.SCOPE_SUPPORTED,
            ),
            DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
        )

    async def capture_base_speak(_self, _text, **_kwargs):
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech("Do you handle", caller_turn=1)
    await pipeline._on_response_first_media_sent(1)
    pipeline._response_turn_number = 1

    pipeline._caller_turn_number = 2
    pipeline._mark_caller_activity()
    await pipeline._handle_caller_speech("fixture replacement?", caller_turn=2)

    assert seen_texts == ["Do you handle", "Do you handle fixture replacement?"]
    assert 1 not in pipeline._semantic_episodes_by_response_turn
    assert pipeline._intake_state.service_object == "fixture"
    assert pipeline._conversation == []
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [PlaybackStatus.CLEARED, PlaybackStatus.STALE, PlaybackStatus.TIMEOUT],
)
async def test_response_end_without_full_playout_never_publishes_semantic_transcript(
    monkeypatch,
    status,
):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(
                facts=CallerObservation(
                    business_scope=BusinessScope.IN_SCOPE,
                    intent=Intent.SERVICE_REQUEST,
                    service_object="fixture",
                    service_action=ServiceAction.REPLACE,
                ),
                direct_answer_kind=DirectAnswerKind.SCOPE_SUPPORTED,
            ),
            DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
        )

    async def capture_base_speak(_self, _text, **_kwargs):
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech(
        "Do you handle fixture replacement?", caller_turn=1
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "first_media", PlaybackStatus.PLAYED)
    )
    assert pipeline._intake_state.service_object == "fixture"
    assert pipeline._conversation == []
    assert transcripts == []

    await pipeline.on_playback_receipt(PlaybackReceipt(1, 1, "response_end", status))

    assert pipeline._intake_state.service_object == "fixture"
    assert pipeline._conversation == []
    assert transcripts == []
    assert pipeline._turn_coordinator.state == TurnLifecycle.LISTENING
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_barge_in_after_first_media_before_response_end_drops_transcript_claim(
    monkeypatch,
):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(
                facts=CallerObservation(
                    business_scope=BusinessScope.IN_SCOPE,
                    intent=Intent.SERVICE_REQUEST,
                    service_object="fixture",
                    service_action=ServiceAction.REPLACE,
                ),
                direct_answer_kind=DirectAnswerKind.SCOPE_SUPPORTED,
            ),
            DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
        )

    async def capture_base_speak(_self, _text, **_kwargs):
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech(
        "Do you handle fixture replacement?", caller_turn=1
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "first_media", PlaybackStatus.PLAYED)
    )
    assert 1 in pipeline._pending_transcripts_by_response_turn

    pipeline._response_turn_number = 1
    pipeline._caller_turn_number = 2
    pipeline._mark_caller_activity()

    assert pipeline._pending_transcripts_by_response_turn == {}
    assert pipeline._conversation == []
    assert transcripts == []
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_fragmented_scope_correction_cannot_publish_pricing_or_preempt_followup(
    monkeypatch,
):
    """Redacted semantic equivalent of the live fragmented scope/correction trace."""
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript
    analyses = []

    async def analyze_turn(**kwargs):
        analyses.append(kwargs["caller_text"])
        fixtures = [
            (
                ControlledObservation(facts=CallerObservation()),
                DirectQuestionAssessment(
                    topic=DirectQuestionTopic.SERVICE_SCOPE,
                    completeness=CallerTurnCompleteness.INCOMPLETE,
                ),
            ),
            (
                ControlledObservation(
                    facts=CallerObservation(
                        intent=Intent.PRICING_QUESTION,
                        service_object="fixture",
                        service_action=ServiceAction.REPLACE,
                    ),
                    direct_answer_kind=DirectAnswerKind.PRICING_REQUIRES_REVIEW,
                ),
                DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
            ),
            (
                ControlledObservation(
                    facts=CallerObservation(
                        intent=Intent.PRICING_QUESTION,
                        service_object="fixture",
                        service_action=ServiceAction.REPLACE,
                    ),
                    direct_answer_kind=DirectAnswerKind.PRICING_REQUIRES_REVIEW,
                ),
                DirectQuestionAssessment(topic=DirectQuestionTopic.PRICING),
            ),
            (
                ControlledObservation(
                    facts=CallerObservation(
                        business_scope=BusinessScope.IN_SCOPE,
                        intent=Intent.SERVICE_REQUEST,
                        service_object="fixture",
                        service_action=ServiceAction.REPLACE,
                    ),
                    direct_answer_kind=DirectAnswerKind.SCOPE_SUPPORTED,
                ),
                DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
            ),
            (
                ControlledObservation(
                    facts=CallerObservation(caller_name="Caller"),
                ),
                DirectQuestionAssessment(),
            ),
        ]
        return fixtures[len(analyses) - 1]

    candidate_audio = []

    async def capture_base_speak(_self, text, **_kwargs):
        candidate_audio.append(text)
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)

    pipeline._caller_turn_number = 1
    await pipeline._handle_caller_speech("Do you handle", caller_turn=1)
    assert candidate_audio == []

    pipeline._caller_turn_number = 2
    pipeline._mark_caller_activity()
    await pipeline._handle_caller_speech("fixture replacement?", caller_turn=2)
    await pipeline._on_response_first_media_sent(1)
    pipeline._response_turn_number = 1
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "first_media", PlaybackStatus.PLAYED)
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "response_end", PlaybackStatus.PLAYED)
    )

    pipeline._caller_turn_number = 3
    pipeline._mark_caller_activity()
    await pipeline._handle_caller_speech("Actually, do you handle", caller_turn=3)
    # The candidate is deliberately not promoted: this is the zero-media
    # supersession that previously leaked an incorrect pricing answer.
    assert "pricing" not in " ".join(text for _, text in transcripts).casefold()

    pipeline._response_turn_number = 2
    pipeline._caller_turn_number = 4
    pipeline._mark_caller_activity()
    await pipeline._handle_caller_speech("fixture replacement?", caller_turn=4)
    await pipeline._on_response_first_media_sent(3)
    pipeline._response_turn_number = 3
    await pipeline.on_playback_receipt(
        PlaybackReceipt(3, 3, "first_media", PlaybackStatus.PLAYED)
    )
    await pipeline.on_playback_receipt(
        PlaybackReceipt(3, 3, "response_end", PlaybackStatus.PLAYED)
    )

    pipeline._caller_turn_number = 5
    pipeline._mark_caller_activity()
    await pipeline._handle_caller_speech("My name is Caller", caller_turn=5)

    assert analyses == [
        "Do you handle",
        "Do you handle fixture replacement?",
        "Actually, do you handle",
        "Actually, do you handle fixture replacement?",
        "My name is Caller",
    ]
    assert "pricing" not in " ".join(text for _, text in transcripts).casefold()
    assert any("handles that type of work" in text for _, text in transcripts)
    assert pipeline._turn_coordinator.state == TurnLifecycle.PLAYING
    assert pipeline._turn_coordinator.current_response_turn == 4
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_direct_question_topic_disagreement_never_speaks_pricing(monkeypatch):
    pipeline = _pipeline()

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(
                facts=CallerObservation(
                    intent=Intent.PRICING_QUESTION,
                    service_object="fixture",
                    service_action=ServiceAction.REPLACE,
                ),
                direct_answer_kind=DirectAnswerKind.PRICING_REQUIRES_REVIEW,
            ),
            DirectQuestionAssessment(topic=DirectQuestionTopic.SERVICE_SCOPE),
        )

    spoken = []

    async def capture_speak(text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(pipeline, "_speak", capture_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech(
        "Does your business handle fixture replacement?",
        caller_turn=1,
        committed_at=1.0,
    )

    assert spoken
    assert "pricing" not in spoken[0].casefold()
    assert spoken[0].startswith("I can't confirm that service")
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_unavailable_direct_question_assessment_never_falls_back_to_pricing(
    monkeypatch,
):
    pipeline = _pipeline()

    async def analyze_turn(**_kwargs):
        return (
            ControlledObservation(
                facts=CallerObservation(
                    intent=Intent.PRICING_QUESTION,
                    service_object="fixture",
                    service_action=ServiceAction.REPAIR,
                ),
                direct_answer_kind=DirectAnswerKind.PRICING_REQUIRES_REVIEW,
            ),
            DirectQuestionAssessment(),
        )

    spoken = []

    async def capture_speak(text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(pipeline, "_speak", capture_speak)
    pipeline._caller_turn_number = 1

    await pipeline._handle_caller_speech("How much is it?", caller_turn=1)

    assert spoken == ["Could you briefly describe how extensive the issue is?"]
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_barge_in_after_first_media_starts_a_new_semantic_episode(monkeypatch):
    pipeline = _pipeline()
    seen_texts = []

    async def analyze_turn(**kwargs):
        seen_texts.append(kwargs["caller_text"])
        return (
            ControlledObservation(
                facts=CallerObservation(
                    service_object="fixture",
                    service_action=ServiceAction.REPAIR,
                )
            ),
            DirectQuestionAssessment(),
        )

    async def capture_base_speak(_self, _text, **_kwargs):
        return True

    monkeypatch.setattr(pipeline._turn_generator, "analyze_caller_turn", analyze_turn)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)

    pipeline._caller_turn_number = 1
    await pipeline._handle_caller_speech("The fixture is leaking.", caller_turn=1)
    await pipeline._on_response_first_media_sent(1)
    await pipeline.on_playback_receipt(
        PlaybackReceipt(1, 1, "first_media", PlaybackStatus.PLAYED)
    )
    pipeline._response_turn_number = 1

    pipeline._caller_turn_number = 2
    pipeline._mark_caller_activity()
    await pipeline._handle_caller_speech("Actually, it is a drain.", caller_turn=2)

    assert seen_texts == ["The fixture is leaking.", "Actually, it is a drain."]
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_semantic_episode_bound_starts_a_fresh_episode(monkeypatch):
    pipeline = _pipeline()
    monkeypatch.setattr(pipeline, "MAX_SEMANTIC_EPISODE_FRAGMENTS", 1)

    first = pipeline._begin_or_extend_semantic_episode(
        caller_text="First independent question.",
        caller_turn=1,
        committed_at=1.0,
    )
    pipeline._intake_state.intent = Intent.SERVICE_REQUEST

    second = pipeline._begin_or_extend_semantic_episode(
        caller_text="Second independent question.",
        caller_turn=2,
        committed_at=2.0,
    )

    assert second is not first
    assert second.fragments == ["Second independent question."]
    assert pipeline._intake_state.intent == Intent.UNKNOWN
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_established_scope_is_not_reclassified_without_a_changed_request():
    pipeline = _pipeline()
    pipeline._intake_state.business_scope = BusinessScope.IN_SCOPE
    pipeline._intake_state.service_object = "toilet"
    pipeline._intake_state.service_action = ServiceAction.REPLACE

    authorized = pipeline._authorize_observation(
        CallerObservation(
            business_scope=BusinessScope.OUT_OF_SCOPE,
            business_scope_reason="caller said there is no existing issue",
            service_object="toilet",
            service_action=ServiceAction.REPLACE,
        )
    )

    assert authorized.business_scope is None
    assert authorized.business_scope_reason is None
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_internal_turn_failure_produces_bounded_recovery_instead_of_silence(
    monkeypatch,
):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript
    spoken = []

    async def fail_after_generation(*_args, **_kwargs):
        assert pipeline._turn_coordinator.begin_generation(1)
        raise RuntimeError("synthetic renderer contract failure")

    async def capture_base_speak(_self, text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(pipeline, "_handle_caller_speech", fail_after_generation)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)

    await pipeline._process_utterance(
        "There is no issue, I just want replacement.",
        caller_turn=1,
        committed_at=1.0,
    )

    recovery = "I'm sorry, I had trouble with that. Could you say that one more time?"
    assert transcripts == [("Kevin", recovery)]
    assert spoken == [recovery]
    assert pipeline._pending_speech_contract is None
    assert pipeline._turn_coordinator.state == TurnLifecycle.PLAYING
    assert pipeline._turn_coordinator.current_response_turn == 1
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_presence_resolution_failure_replays_the_heard_question(monkeypatch):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript
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
    pipeline._response_turn_number = 2
    pipeline._mark_caller_activity()
    pipeline._presence_reply_pending = False
    assert coordinator.begin_presence_resolution(2)

    async def fail_presence_resolution(*_args, **_kwargs):
        raise RuntimeError("synthetic presence classification failure")

    spoken = []

    async def capture_base_speak(_self, text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(pipeline, "_handle_caller_speech", fail_presence_resolution)
    monkeypatch.setattr(VoicePipeline, "_speak", capture_base_speak)

    await pipeline._process_utterance(
        "Yes, I'm here.",
        caller_turn=2,
        committed_at=1.0,
    )

    assert spoken == [question]
    assert transcripts == [("Kevin", question)]
    assert coordinator.state == TurnLifecycle.PLAYING
    assert coordinator.current_response_turn == 3
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_stale_failure_does_not_claim_an_unheard_recovery(monkeypatch):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript
    spoken = []

    async def fail_after_caller_activity(*_args, **_kwargs):
        assert pipeline._turn_coordinator.begin_generation(1)
        pipeline._turn_coordinator.caller_activity()
        raise RuntimeError("synthetic stale turn")

    async def capture_speak(text, **_kwargs):
        spoken.append(text)
        return True

    monkeypatch.setattr(pipeline, "_handle_caller_speech", fail_after_caller_activity)
    monkeypatch.setattr(pipeline, "_speak", capture_speak)

    await pipeline._process_utterance(
        "newer caller activity",
        caller_turn=1,
        committed_at=1.0,
    )

    assert transcripts == []
    assert spoken == []
    assert pipeline._turn_coordinator.state == TurnLifecycle.LISTENING
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_cancelled_controlled_turn_does_not_start_failure_recovery(monkeypatch):
    pipeline = _pipeline()
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    async def cancel_generation(*_args, **_kwargs):
        raise asyncio.CancelledError

    pipeline.on_transcript = capture_transcript
    monkeypatch.setattr(pipeline, "_handle_caller_speech", cancel_generation)

    with pytest.raises(asyncio.CancelledError):
        await pipeline._process_utterance(
            "superseded caller turn",
            caller_turn=1,
            committed_at=1.0,
        )

    assert transcripts == []
    await pipeline._http_client.aclose()


@pytest.mark.asyncio
async def test_failed_recovery_delivery_is_not_added_to_the_transcript(monkeypatch):
    transcripts = []

    async def capture_transcript(speaker, text):
        transcripts.append((speaker, text))

    pipeline = _pipeline()
    pipeline.on_transcript = capture_transcript

    async def fail_after_generation(*_args, **_kwargs):
        assert pipeline._turn_coordinator.begin_generation(1)
        raise RuntimeError("synthetic renderer failure")

    async def fail_base_speak(_self, _text, **_kwargs):
        return False

    monkeypatch.setattr(pipeline, "_handle_caller_speech", fail_after_generation)
    monkeypatch.setattr(VoicePipeline, "_speak", fail_base_speak)

    await pipeline._process_utterance(
        "routine request",
        caller_turn=1,
        committed_at=1.0,
    )

    assert transcripts == []
    assert pipeline._turn_coordinator.state == TurnLifecycle.LISTENING
    await pipeline._http_client.aclose()
