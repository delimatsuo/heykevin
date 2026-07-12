"""Receptionist prompt, call-state, and post-call scope guardrails."""

import asyncio
import base64
import inspect
import json
import logging
import os
import time

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services.gemini_pipeline import GeminiPipeline
from app.services.job_card import _build_extraction_prompt
from app.services.vcard import generate_vcard
from app.services.voice_pipeline import build_system_prompt, is_owner_availability_hold, VoicePipeline
from app.services import voice_pipeline as voice_pipeline_module


def _plumbing_config() -> dict:
    return {
        "owner_name": "Deli Matsuo",
        "business_name": "Matsuo Plumbing",
        "mode": "business",
        "effective_mode": "business",
        "knowledge": (
            "## Services\n"
            "- Water heater services\n"
            "- Faucet replacement\n"
            "- Water filter replacement\n"
            "- Dishwasher installation"
        ),
        "services": [
            {"name": "House call", "price_min": 100, "price_max": 100},
            {"name": "Faucet replacement", "price_min": 150, "price_max": 250},
        ],
    }


class _FakeGeminiWebSocket:
    def __init__(self, messages: list[str] | None = None):
        self.messages = list(messages or [])
        self.sent_payloads: list[dict] = []

    async def send(self, payload: str):
        self.sent_payloads.append(json.loads(payload))

    async def recv(self):
        return json.dumps({"setupComplete": {}})

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def close(self):
        return None


def _gemini_audio_message(chunk: bytes) -> str:
    return json.dumps({
        "serverContent": {
            "modelTurn": {
                "parts": [{
                    "inlineData": {
                        "mimeType": "audio/pcm;rate=24000",
                        "data": base64.b64encode(chunk).decode("ascii"),
                    }
                }]
            }
        }
    })


def test_business_prompt_rejects_out_of_scope_trade_work():
    prompt = build_system_prompt(_plumbing_config())

    assert "BUSINESS PROFILE AND SERVICE SCOPE" in prompt
    assert "Listed services: House call, Faucet replacement" in prompt
    assert "If it is OUT OF SCOPE" in prompt
    assert "do not ask trade-specific diagnostic questions for a different trade" in prompt
    assert "Do not say \"Sure, I can help with that\"" in prompt
    assert "electrical panel" in prompt


def test_business_prompt_instructs_media_followup_without_live_review_claim():
    prompt = build_system_prompt(_plumbing_config())

    assert "upload a photo or short video" in prompt
    assert "Do not claim you can review media live during the phone call" in prompt


def test_business_prompt_prevents_immediate_close_after_availability_check():
    prompt = build_system_prompt(_plumbing_config())

    assert "Never say \"I'll pass this along\" immediately after" in prompt
    assert "First wait for the availability result" in prompt
    assert "Owner handoff" in prompt
    assert "Silent caller" in prompt


def test_business_prompt_confirms_only_phone_last_four():
    prompt = build_system_prompt(_plumbing_config())

    assert "confirm only the last 4 digits" in prompt
    assert "Do not read back the full phone number" in prompt
    assert "Always read back phone numbers digit by digit" not in prompt
    assert "6-5-0, 6-9-1, 8-6-6-7" not in prompt


def test_personal_prompt_confirms_only_phone_last_four():
    prompt = build_system_prompt({
        "owner_name": "Deli Matsuo",
        "mode": "personal",
        "effective_mode": "personal",
    })

    assert "confirm only the last 4 digits" in prompt
    assert "Do not read back the full phone number" in prompt
    assert "Always read back phone numbers digit by digit" not in prompt
    assert "6-5-0, 6-9-1, 8-6-6-7" not in prompt


def test_business_prompt_defers_callback_number_until_callback_intent():
    prompt = build_system_prompt(_plumbing_config(), caller_phone="+16504228667")

    assert "It is okay to ask for the caller's name early" in prompt
    assert "Do not ask for or confirm a callback number" in prompt
    assert "only after the caller asks for or agrees to a callback" in prompt
    assert "Get their name, callback number" not in prompt
    assert "If you don't have their callback number, ask for it" not in prompt
    assert "urgency, and callback number" not in prompt
    assert "Still collect their name, reason for calling, and callback number" not in prompt


def test_business_prompt_requires_explicit_callback_opt_in():
    prompt = build_system_prompt(_plumbing_config(), caller_phone="+16504228667")

    assert "Do not treat a normal service request as callback intent" in prompt
    assert "Only confirm the callback number after the caller explicitly asks for" in prompt
    assert "or clearly accepts your offer of a callback" in prompt
    assert "Do not ask for callback confirmation immediately after detecting urgency" in prompt


def test_business_prompt_limits_pricing_turns_to_one_followup():
    prompt = build_system_prompt(_plumbing_config(), caller_phone="+16504228667")

    assert "When answering pricing questions, answer first, then ask at most one short follow-up question" in prompt
    assert "Do not bundle multiple intake questions into the same pricing answer" in prompt
    assert "Keep spoken turns brief" in prompt
    assert "one short question at a time" in prompt
    assert "ask 1-2 smart follow-up questions" not in prompt


def test_business_prompt_does_not_reask_known_fixture_category():
    prompt = build_system_prompt(_plumbing_config(), caller_phone="+16504228667")

    assert "If the caller already named the fixture, appliance, or object" in prompt
    assert "do not ask which fixture or category it is" in prompt
    assert "Is it a sink, toilet, water heater, or appliance connection?" not in prompt


def test_business_prompt_restricts_live_owner_hold_to_emergencies_or_live_transfer():
    prompt = build_system_prompt(_plumbing_config(), caller_phone="+16504228667")

    assert "Only try the owner live for emergencies or when the caller explicitly asks to speak with" in prompt
    assert "For routine and same-day leads, take a concise message instead of putting the caller on hold" in prompt
    assert "For urgent or same-day issues" not in prompt


def test_personal_prompt_defers_callback_number_until_callback_intent():
    prompt = build_system_prompt(
        {
            "owner_name": "Deli Matsuo",
            "mode": "personal",
            "effective_mode": "personal",
        },
        caller_phone="+16504228667",
    )

    assert "It is okay to ask for the caller's name early" in prompt
    assert "Do not ask for or confirm a callback number" in prompt
    assert "only after the caller asks for or agrees to a callback" in prompt
    assert "name, message, and callback number" not in prompt
    assert "collect any missing callback details" not in prompt


def test_after_hours_prompt_defers_callback_number_until_callback_intent():
    prompt = build_system_prompt(
        {
            **_plumbing_config(),
            "business_hours_start": "8:00",
            "business_hours_end": "5:00",
        },
        after_hours=True,
        caller_phone="+16504228667",
    )

    assert "after-hours" in prompt.lower()
    assert "only after the caller asks for or agrees to a callback" in prompt
    assert "Still collect their name, reason for calling, and callback number" not in prompt


def test_prompt_uses_caller_id_last_four_without_exposing_full_number():
    prompt = build_system_prompt(_plumbing_config(), caller_phone="+16504228667")

    assert "caller ID number ending in 8667" in prompt
    assert "Is the number ending in 8667 the best number for a callback?" in prompt
    assert "+16504228667" not in prompt
    assert "6504228667" not in prompt


def test_prompt_without_caller_id_asks_full_number_only_after_callback_intent():
    prompt = build_system_prompt(_plumbing_config())

    assert "If caller ID is missing or blocked" in prompt
    assert "only after callback, scheduling, or follow-up intent is established" in prompt


def test_business_prompt_answers_scope_and_pricing_before_address_collection():
    prompt = build_system_prompt(_plumbing_config(), caller_phone="+16504228667")

    assert "Answer direct service, scope, and pricing questions before asking for name" in prompt
    assert "Do not ask for a service address during basic intake" in prompt
    assert "Ask for a service address only after" in prompt
    assert "Get their name, one-line reason for calling, and service address" not in prompt


def test_job_card_extraction_prompt_can_classify_out_of_scope_requests():
    prompt = _build_extraction_prompt(
        "Caller: Can you help with my electric panel?\nKevin: Matsuo Plumbing may not be the right company.",
        _plumbing_config(),
    )

    assert '"out_of_scope"' in prompt
    assert "electrical panel or breaker request is out_of_scope" in prompt
    assert "Only use service_request when the caller's request appears related" in prompt
    assert "Water heater services" in prompt


def test_electrical_panel_terms_trigger_urgency_escalation():
    assert "electric panel" in VoicePipeline.URGENCY_KEYWORDS
    assert "breaker tripped" in VoicePipeline.URGENCY_KEYWORDS
    assert "electric panel" in GeminiPipeline.URGENCY_KEYWORDS
    assert "breaker tripped" in GeminiPipeline.URGENCY_KEYWORDS


def test_owner_availability_hold_detection_is_specific():
    assert is_owner_availability_hold("Got it. I'm going to try Deli now, one moment.")
    assert is_owner_availability_hold("Let me see if Deli is available, one moment.")
    assert not is_owner_availability_hold(
        "I'm sorry, it looks like Deli is not available right now."
    )
    assert not is_owner_availability_hold("Let me check on that for you.")


@pytest.mark.asyncio
async def test_voice_pipeline_silence_waits_for_owner_availability_before_prompting():
    transcripts = []
    completed = asyncio.Event()

    async def on_audio_out(_chunk: bytes):
        return None

    async def on_transcript(speaker: str, text: str):
        transcripts.append((speaker, text))

    async def on_call_complete():
        completed.set()

    pipeline = VoicePipeline(
        on_audio_out=on_audio_out,
        on_transcript=on_transcript,
        on_call_complete=on_call_complete,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline.CALLER_SILENCE_PROMPT_SECONDS = 0.01
    pipeline.CALLER_SILENCE_HANGUP_SECONDS = 0.01
    pipeline.CALLER_SILENCE_CHECK_INTERVAL_SECONDS = 0.005
    pipeline.CALLER_SILENCE_GOODBYE_SECONDS = 0
    pipeline.OWNER_AVAILABILITY_TIMEOUT_SECONDS = 0.03

    async def fake_speak(_text: str):
        pipeline._is_speaking = True
        await asyncio.sleep(0)
        pipeline._is_speaking = False
        pipeline._mark_kevin_activity()

    pipeline._speak = fake_speak
    pipeline._connected = True
    pipeline._mark_kevin_activity()
    pipeline._start_owner_availability_wait()
    silence_task = asyncio.create_task(pipeline._silence_check_loop())

    await asyncio.sleep(0.02)
    assert not any("still there" in text.lower() for _, text in transcripts)

    await asyncio.wait_for(completed.wait(), timeout=1)
    silence_task.cancel()
    if pipeline._unavailable_task:
        pipeline._unavailable_task.cancel()
    await pipeline._http_client.aclose()

    spoken = " ".join(text for _, text in transcripts)
    assert "not available to take the call right now" in spoken
    assert "Are you still there?" in spoken
    assert "hang up for now" in spoken


@pytest.mark.asyncio
async def test_voice_pipeline_uses_configured_anthropic_model(monkeypatch):
    request_bodies = []
    transcripts = []

    class FakeClaudeResponse:
        status_code = 200

        def json(self):
            return {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "We handle plumbing repairs."}],
            }

    class FakeClaudeClient:
        async def post(self, *_args, **kwargs):
            request_bodies.append(kwargs["json"])
            return FakeClaudeResponse()

    async def on_audio_out(_chunk: bytes):
        return None

    async def on_transcript(speaker: str, text: str):
        transcripts.append((speaker, text))

    assert hasattr(voice_pipeline_module.settings, "anthropic_model")
    monkeypatch.setattr(voice_pipeline_module.settings, "anthropic_model", "claude-sonnet-5")

    pipeline = VoicePipeline(
        on_audio_out=on_audio_out,
        on_transcript=on_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    await pipeline._http_client.aclose()
    pipeline._http_client = FakeClaudeClient()
    pipeline._speak = on_audio_out

    await pipeline._handle_caller_speech("What kind of services do you offer?")

    assert request_bodies[0]["model"] == "claude-sonnet-5"
    assert transcripts[-1] == ("Kevin", "We handle plumbing repairs.")


@pytest.mark.asyncio
async def test_gemini_owner_availability_hold_suppresses_caller_silence():
    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )

    pipeline._connected = True
    pipeline._mark_kevin_activity()
    pipeline._start_owner_availability_wait()
    try:
        assert not pipeline._waiting_on_caller()
        pipeline._finish_owner_availability_wait()
        assert pipeline._waiting_on_caller()
    finally:
        if pipeline._unavailable_task:
            pipeline._unavailable_task.cancel()


def test_gemini_pipeline_receives_caller_phone_context():
    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
        caller_phone="+16504228667",
    )

    assert pipeline._caller_phone == "+16504228667"
    assert "caller ID number ending in 8667" in pipeline._system_prompt
    assert "+16504228667" not in pipeline._system_prompt


def test_media_stream_passes_caller_phone_to_gemini_pipeline():
    from app.webhooks import media_stream

    source = inspect.getsource(media_stream.media_stream_ws)
    gemini_call = source.split("pipeline = GeminiPipeline(", 1)[1].split(
        "logger.info(f\"Using Gemini Live pipeline", 1
    )[0]

    assert "caller_phone=active_call.caller_phone if active_call else \"\"" in gemini_call


def test_media_stream_passes_twilio_start_time_to_gemini_pipeline():
    from app.webhooks import media_stream

    source = inspect.getsource(media_stream.media_stream_ws)
    gemini_call = source.split("pipeline = GeminiPipeline(", 1)[1].split(
        "logger.info(f\"Using Gemini Live pipeline", 1
    )[0]

    assert "media_stream_started_at = time.monotonic()" in source
    assert "call_started_at=media_stream_started_at" in gemini_call


def test_gemini_greetings_are_bounded_and_disclose_ai_transcription():
    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    business = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        contractor_config=_plumbing_config(),
    )
    business._after_hours = False

    personal_config = _plumbing_config() | {
        "mode": "personal",
        "effective_mode": "personal",
    }
    personal = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        contractor_config=personal_config,
    )

    after_hours = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        contractor_config=_plumbing_config(),
    )
    after_hours._after_hours = True

    greetings = [
        business._build_greeting_text(),
        personal._build_greeting_text(),
        after_hours._build_greeting_text(),
    ]

    assert all("AI assistant" in greeting for greeting in greetings)
    assert all("may be transcribed and summarized" in greeting for greeting in greetings)
    assert all(len(greeting.split()) <= 24 for greeting in greetings)
    assert "Matsuo Plumbing" in greetings[0]
    assert "Deli's AI assistant" in greetings[1]
    assert "currently closed" in greetings[2]


@pytest.mark.asyncio
async def test_gemini_start_sends_exact_greeting_and_safe_startup_metrics(
    monkeypatch,
    caplog,
):
    websocket = _FakeGeminiWebSocket()

    async def fake_connect(*_args, **_kwargs):
        return websocket

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    monkeypatch.setattr("app.services.gemini_pipeline.websockets.connect", fake_connect)
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    call_started_at = time.monotonic() - 2
    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
        call_started_at=call_started_at,
    )
    pipeline._after_hours = False

    try:
        started = await pipeline.start(start_background_tasks=False)

        assert started
        greeting_text = pipeline._build_greeting_text()
        greeting_prompt = websocket.sent_payloads[1]["client_content"]["turns"][0][
            "parts"
        ][0]["text"]
        assert greeting_prompt == (
            f"Say exactly this greeting and nothing else: {json.dumps(greeting_text)}"
        )

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "voice_timing event=gemini_ws_connected" in messages
        assert "voice_timing event=gemini_setup_sent" in messages
        assert "voice_timing event=gemini_setup_ack" in messages
        assert "call_elapsed_ms=2" in messages
        assert "voice_timing event=greeting_instruction_sent" in messages
        assert "Matsuo Plumbing" not in messages
    finally:
        await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_reconnect_can_resume_without_repeating_greeting(monkeypatch):
    sent_messages = []

    class FakeWebSocket:
        async def send(self, payload: str):
            sent_messages.append(json.loads(payload))

        async def recv(self):
            return json.dumps({"setupComplete": {}})

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self):
            return None

    async def fake_connect(*_args, **_kwargs):
        return FakeWebSocket()

    monkeypatch.setattr("app.services.gemini_pipeline.websockets.connect", fake_connect)

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )

    started = await pipeline.start(
        send_greeting=False,
        start_background_tasks=False,
        reconnect_context="Caller: Do you replace toilets?\nKevin: Yes, we do.",
    )

    assert started
    assert len(sent_messages) == 1
    setup_text = sent_messages[0]["setup"]["system_instruction"]["parts"][0]["text"]
    assert "CONVERSATION CONTEXT BEFORE RECONNECT" in setup_text
    assert "Do not greet the caller again" in setup_text
    assert "Caller: Do you replace toilets?" in setup_text


@pytest.mark.asyncio
async def test_gemini_setup_configures_fast_endpointing(monkeypatch):
    sent_messages = []

    class FakeWebSocket:
        async def send(self, payload: str):
            sent_messages.append(json.loads(payload))

        async def recv(self):
            return json.dumps({"setupComplete": {}})

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self):
            return None

    async def fake_connect(*_args, **_kwargs):
        return FakeWebSocket()

    monkeypatch.setattr("app.services.gemini_pipeline.websockets.connect", fake_connect)

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )

    started = await pipeline.start(send_greeting=False, start_background_tasks=False)

    assert started
    setup = sent_messages[0]["setup"]
    realtime_config = setup["realtime_input_config"]
    activity_detection = realtime_config["automatic_activity_detection"]
    assert realtime_config["turn_coverage"] == "TURN_INCLUDES_ONLY_ACTIVITY"
    assert realtime_config["activity_handling"] == "START_OF_ACTIVITY_INTERRUPTS"
    assert activity_detection["start_of_speech_sensitivity"] == "START_SENSITIVITY_HIGH"
    assert activity_detection["end_of_speech_sensitivity"] == "END_SENSITIVITY_HIGH"
    assert activity_detection["silence_duration_ms"] <= 500
    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_setup_disables_dynamic_thinking_for_low_latency(monkeypatch):
    sent_messages = []

    class FakeWebSocket:
        async def send(self, payload: str):
            sent_messages.append(json.loads(payload))

        async def recv(self):
            return json.dumps({"setupComplete": {}})

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self):
            return None

    async def fake_connect(*_args, **_kwargs):
        return FakeWebSocket()

    monkeypatch.setattr("app.services.gemini_pipeline.websockets.connect", fake_connect)

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )

    started = await pipeline.start(send_greeting=False, start_background_tasks=False)

    assert started
    generation_config = sent_messages[0]["setup"]["generation_config"]
    assert generation_config["thinking_config"] == {"thinking_budget": 0}
    assert generation_config["temperature"] <= 0.5
    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_process_audio_uses_current_realtime_audio_field():
    sent_messages = []

    class FakeWebSocket:
        async def send(self, payload: str):
            sent_messages.append(json.loads(payload))

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = FakeWebSocket()

    await pipeline.process_audio_in(b"\xff" * 160)

    realtime_input = sent_messages[0]["realtime_input"]
    assert "audio" in realtime_input
    assert realtime_input["audio"]["mime_type"] == "audio/pcm;rate=16000"
    assert "data" in realtime_input["audio"]
    assert "media_chunks" not in realtime_input


@pytest.mark.asyncio
async def test_gemini_audio_playout_is_paced_and_tracks_speaking(monkeypatch):
    sent_chunks = []
    sleep_calls = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float):
        sleep_calls.append(delay)
        await real_sleep(0)

    monkeypatch.setattr("app.services.gemini_pipeline.asyncio.sleep", fake_sleep)

    async def record_audio(chunk: bytes):
        sent_chunks.append((len(chunk), pipeline._is_speaking))

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=record_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._audio_playout_task = asyncio.create_task(pipeline._audio_playout_loop())

    half_second_pcm_24k = b"\0\0" * 12_000
    await pipeline._enqueue_model_audio(half_second_pcm_24k)
    await pipeline._enqueue_model_audio(half_second_pcm_24k)
    await asyncio.wait_for(pipeline._audio_queue.join(), timeout=1)

    assert len(sent_chunks) == 2
    assert all(was_speaking for _, was_speaking in sent_chunks)
    assert any(delay >= 0.4 for delay in sleep_calls)
    assert pipeline._is_speaking is False
    assert pipeline._last_kevin_speech_time > 0

    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_first_outbound_audio_metric_uses_twilio_start_clock(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr("app.services.gemini_pipeline.pcm24k_to_mulaw", lambda chunk: chunk)

    sent_chunks = []

    async def record_audio(chunk: bytes):
        sent_chunks.append(chunk)

    async def noop_transcript(_speaker: str, _text: str):
        return None

    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")
    pipeline = GeminiPipeline(
        on_audio_out=record_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
        call_started_at=time.monotonic() - 1,
    )
    pipeline._connected = True
    pipeline._audio_playout_task = asyncio.create_task(pipeline._audio_playout_loop())

    private_audio = b"private outbound audio"
    await pipeline._enqueue_model_audio(private_audio)
    await asyncio.wait_for(pipeline._audio_queue.join(), timeout=1)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert sent_chunks == [private_audio]
    assert messages.count("voice_timing event=first_outbound_audio") == 1
    assert "call_elapsed_ms=1" in messages
    assert base64.b64encode(private_audio).decode("ascii") not in messages
    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_goodbye_waits_for_audio_playout_before_hangup(monkeypatch):
    real_sleep = asyncio.sleep
    join_started = asyncio.Event()
    release_join = asyncio.Event()
    completed = asyncio.Event()

    async def fake_sleep(_delay: float):
        await real_sleep(0)

    monkeypatch.setattr("app.services.gemini_pipeline.asyncio.sleep", fake_sleep)

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def on_call_complete():
        completed.set()

    class FakeAudioQueue:
        async def join(self):
            join_started.set()
            await release_join.wait()

    class OneMessageWebSocket:
        def __init__(self, message: dict):
            self._messages = [json.dumps(message)]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        on_call_complete=on_call_complete,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = OneMessageWebSocket({"serverContent": {"turnComplete": True}})
    pipeline._audio_queue = FakeAudioQueue()

    async def goodbye_flush(*_args, **_kwargs):
        return True

    pipeline._flush_kevin_transcript = goodbye_flush

    task = asyncio.create_task(pipeline._receive_loop())

    await asyncio.wait_for(join_started.wait(), timeout=1)
    assert not completed.is_set()

    release_join.set()
    await asyncio.wait_for(completed.wait(), timeout=1)
    await task


@pytest.mark.asyncio
async def test_gemini_receive_reconnect_preserves_context_without_greeting(monkeypatch, caplog):
    from websockets.exceptions import ConnectionClosedError

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    class ClosingWebSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ConnectionClosedError(None, None)

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = ClosingWebSocket()
    pipeline._transcript_lines = [
        "Caller: Do you replace toilets?",
        "Kevin: Yes, we do.",
    ]

    reconnect_calls = []

    async def fake_start(**kwargs):
        reconnect_calls.append(kwargs)
        return True

    monkeypatch.setattr(pipeline, "start", fake_start)
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._receive_loop()

    assert reconnect_calls == [
        {
            "send_greeting": False,
            "start_background_tasks": False,
            "reconnect_context": "Caller: Do you replace toilets?\nKevin: Yes, we do.",
        }
    ]
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_timing event=reconnect_result" in messages
    assert "attempt=1 success=True" in messages


@pytest.mark.asyncio
async def test_gemini_reconnect_limit_ends_call_without_another_session(monkeypatch, caplog):
    from websockets.exceptions import ConnectionClosedError

    completed = asyncio.Event()

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def on_call_complete():
        completed.set()

    class ClosingWebSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ConnectionClosedError(None, None)

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        on_call_complete=on_call_complete,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = ClosingWebSocket()
    pipeline._reconnect_attempts = pipeline.MAX_RECONNECT_ATTEMPTS

    async def unexpected_start(**_kwargs):
        pytest.fail("reconnect limit must prevent another session")

    monkeypatch.setattr(pipeline, "start", unexpected_start)
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._receive_loop()

    assert completed.is_set()
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_timing event=reconnect_result" in messages
    assert "attempt=2 success=False reason=limit" in messages


@pytest.mark.asyncio
async def test_gemini_reconnect_discards_stale_model_output_before_new_session(monkeypatch):
    from websockets.exceptions import ConnectionClosedError

    transcripts = []
    clear_events = []

    async def noop_audio(_chunk: bytes):
        return None

    async def record_transcript(speaker: str, text: str):
        transcripts.append((speaker, text))

    async def clear_audio():
        clear_events.append("clear")

    class ClosingWebSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ConnectionClosedError(None, None)

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=record_transcript,
        on_clear_audio=clear_audio,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = ClosingWebSocket()
    pipeline._caller_transcript_buf = ["I need help with a leaking faucet."]
    pipeline._kevin_transcript_buf = ["This unfinished response must not survive."]
    original_epoch = pipeline._audio_epoch
    await pipeline._audio_queue.put((b"stale model audio", 1.0, original_epoch))

    reconnect_calls = []

    async def fake_start(**kwargs):
        assert pipeline._audio_queue.empty()
        assert pipeline._kevin_transcript_buf == []
        assert pipeline._audio_epoch > original_epoch
        assert clear_events == ["clear"]
        reconnect_calls.append(kwargs)
        return True

    monkeypatch.setattr(pipeline, "start", fake_start)

    await pipeline._receive_loop()
    await asyncio.wait_for(pipeline._audio_queue.join(), timeout=1)

    assert transcripts == [("Caller", "I need help with a leaking faucet.")]
    assert pipeline._transcript_lines == ["Caller: I need help with a leaking faucet."]
    assert reconnect_calls[0]["reconnect_context"] == (
        "Caller: I need help with a leaking faucet."
    )


@pytest.mark.asyncio
async def test_gemini_reconnect_does_not_start_owner_hold_from_partial_kevin_text(monkeypatch):
    from websockets.exceptions import ConnectionClosedError

    async def noop_audio(_chunk: bytes):
        return None

    async def record_transcript(_speaker: str, _text: str):
        return None

    class ClosingWebSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ConnectionClosedError(None, None)

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=record_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = ClosingWebSocket()
    pipeline._kevin_transcript_buf = ["Got it. I'm going to try Deli now, one moment."]

    async def fake_start(**_kwargs):
        return True

    monkeypatch.setattr(pipeline, "start", fake_start)

    await pipeline._receive_loop()

    assert pipeline._waiting_for_owner_availability is False
    assert pipeline._unavailable_task is None


@pytest.mark.asyncio
async def test_gemini_transcript_flush_records_response_timing():
    transcripts = []

    async def noop_audio(_chunk: bytes):
        return None

    async def record_transcript(speaker: str, text: str):
        transcripts.append((speaker, text))

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=record_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._caller_transcript_buf = ["Do you replace toilets?"]

    await pipeline._flush_caller_transcript()

    assert pipeline._last_caller_transcript_flushed_at > 0

    pipeline._kevin_transcript_buf = ["Yes, we do."]

    await pipeline._flush_kevin_transcript()

    assert transcripts == [
        ("Caller", "Do you replace toilets?"),
        ("Kevin", "Yes, we do."),
    ]
    assert pipeline._last_caller_transcript_flushed_at == 0.0


@pytest.mark.asyncio
async def test_gemini_logs_response_latency_and_generated_duration_without_text(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr("app.services.gemini_pipeline.pcm24k_to_mulaw", lambda chunk: chunk)

    sent_chunks = []

    async def record_audio(chunk: bytes):
        sent_chunks.append(chunk)

    async def noop_transcript(_speaker: str, _text: str):
        return None

    private_caller_text = "Private caller request"
    private_response_text = "Private response with four words"
    generated_audio = b"a" * 8_000
    pipeline = GeminiPipeline(
        on_audio_out=record_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = _FakeGeminiWebSocket([
        json.dumps({
            "serverContent": {
                "inputTranscription": {"text": private_caller_text}
            }
        }),
        _gemini_audio_message(generated_audio),
        json.dumps({
            "serverContent": {
                "outputTranscription": {"text": private_response_text}
            }
        }),
        json.dumps({"serverContent": {"turnComplete": True}}),
    ])
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._receive_loop()
    await asyncio.wait_for(pipeline._audio_queue.join(), timeout=2)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert sent_chunks == [generated_audio]
    assert messages.count("voice_timing event=response_first_audio") == 1
    assert "latency_ms=" in messages
    assert messages.count("voice_timing event=model_turn_complete") == 1
    assert "generated_audio_ms=1000" in messages
    assert "words=5" in messages
    assert private_caller_text not in messages
    assert private_response_text not in messages
    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_turn_complete_without_transcript_closes_response_metrics(caplog):
    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._response_turn_number = 1
    pipeline._response_first_audio_at = time.monotonic()
    pipeline._generated_audio_ms = 750
    pipeline._ws = _FakeGeminiWebSocket([
        json.dumps({"serverContent": {"turnComplete": True}}),
    ])
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._receive_loop()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_timing event=model_turn_complete" in messages
    assert "generated_audio_ms=750" in messages
    assert "chars=0 words=0" in messages
    assert pipeline._response_first_audio_at == 0.0
    assert pipeline._generated_audio_ms == 0


@pytest.mark.asyncio
async def test_gemini_barge_in_resets_caller_silence_state():
    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    clear_count = 0

    async def clear_audio():
        nonlocal clear_count
        clear_count += 1

    class OneMessageWebSocket:
        def __init__(self, message: dict):
            self._messages = [json.dumps(message)]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        on_clear_audio=clear_audio,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._mark_kevin_activity()
    pipeline._caller_silence_prompted_at = pipeline._last_kevin_speech_time
    pipeline._ws = OneMessageWebSocket({"serverContent": {"interrupted": True}})

    assert pipeline._waiting_on_caller()

    await pipeline._receive_loop()

    assert clear_count == 1
    assert pipeline._caller_silence_prompted_at is None
    assert pipeline._last_caller_speech_time >= pipeline._last_kevin_speech_time
    assert not pipeline._waiting_on_caller()


@pytest.mark.asyncio
async def test_gemini_start_does_not_enable_proactive_silence_prompts(monkeypatch):
    websocket = _FakeGeminiWebSocket()

    async def fake_connect(*_args, **_kwargs):
        return websocket

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    monkeypatch.setattr("app.services.gemini_pipeline.websockets.connect", fake_connect)

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        contractor_config=_plumbing_config(),
    )

    try:
        started = await pipeline.start(send_greeting=False, start_background_tasks=True)

        assert started
        assert pipeline._silence_check_task is None
    finally:
        await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_urgency_log_does_not_include_transcript_text(caplog):
    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def noop_urgency(_text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        on_urgency_detected=noop_urgency,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._caller_transcript_buf = [
        "There is a fire at my private address, 100 Market Street."
    ]

    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._flush_caller_transcript()
    await asyncio.sleep(0)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_timing event=urgency_detected" in messages
    assert "keyword=fire" in messages
    assert "100 Market Street" not in messages


@pytest.mark.asyncio
async def test_gemini_logs_first_inbound_audio_forward_without_payload(caplog):
    websocket = _FakeGeminiWebSocket()

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = websocket

    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline.process_audio_in(b"\xff" * 160)
    await pipeline.process_audio_in(b"\xff" * 160)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert messages.count("voice_timing event=first_inbound_audio_forwarded") == 1
    assert "chunk_bytes=160" in messages
    audio_payload = websocket.sent_payloads[0]["realtime_input"]["audio"]["data"]
    assert audio_payload not in messages


@pytest.mark.asyncio
async def test_gemini_logs_inbound_audio_error_once_without_exception_message(caplog):
    class FailingWebSocket:
        async def send(self, _payload: str):
            raise RuntimeError("private caller audio at 100 Market Street")

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = FailingWebSocket()

    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline.process_audio_in(b"\xff" * 160)
    await pipeline.process_audio_in(b"\xff" * 160)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert messages.count("voice_timing event=inbound_audio_error") == 1
    assert "exception_type=RuntimeError" in messages
    assert "100 Market Street" not in messages


@pytest.mark.asyncio
async def test_gemini_barge_in_clear_waits_for_in_flight_audio(monkeypatch, caplog):
    events = []
    audio_send_started = asyncio.Event()
    release_audio_send = asyncio.Event()
    clear_completed = asyncio.Event()

    monkeypatch.setattr("app.services.gemini_pipeline.pcm24k_to_mulaw", lambda chunk: chunk)

    async def blocked_audio_send(_chunk: bytes):
        events.append("audio_started")
        audio_send_started.set()
        await release_audio_send.wait()
        events.append("audio_sent")

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def clear_audio():
        events.append("clear")
        clear_completed.set()

    pipeline = GeminiPipeline(
        on_audio_out=blocked_audio_send,
        on_transcript=noop_transcript,
        on_clear_audio=clear_audio,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")
    pipeline._audio_playout_task = asyncio.create_task(pipeline._audio_playout_loop())
    await pipeline._enqueue_model_audio(b"model audio")
    await asyncio.wait_for(audio_send_started.wait(), timeout=1)

    pipeline._ws = _FakeGeminiWebSocket([
        json.dumps({"serverContent": {"interrupted": True}})
    ])
    receive_task = asyncio.create_task(pipeline._receive_loop())

    for _ in range(10):
        if pipeline._interrupt_speaking:
            break
        await asyncio.sleep(0)

    assert pipeline._interrupt_speaking
    assert not clear_completed.is_set()

    release_audio_send.set()
    await asyncio.wait_for(receive_task, timeout=1)
    await asyncio.wait_for(clear_completed.wait(), timeout=1)

    assert events == ["audio_started", "audio_sent", "clear"]
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_timing event=barge_in_clear" in messages
    assert "clear_ms=" in messages
    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_barge_in_discards_stale_output_until_turn_completes(monkeypatch):
    sent_chunks = []
    transcripts = []
    call_completed = asyncio.Event()

    monkeypatch.setattr("app.services.gemini_pipeline.pcm24k_to_mulaw", lambda chunk: chunk)

    async def record_audio(chunk: bytes):
        sent_chunks.append(chunk)

    async def record_transcript(speaker: str, text: str):
        transcripts.append((speaker, text))

    async def on_call_complete():
        call_completed.set()

    async def noop_clear():
        return None

    pipeline = GeminiPipeline(
        on_audio_out=record_audio,
        on_transcript=record_transcript,
        on_clear_audio=noop_clear,
        on_call_complete=on_call_complete,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = _FakeGeminiWebSocket([
        json.dumps({"serverContent": {"interrupted": True}}),
        _gemini_audio_message(b"stale audio"),
        json.dumps({
            "serverContent": {
                "outputTranscription": {"text": "Goodbye from the interrupted response."}
            }
        }),
        json.dumps({"serverContent": {"turnComplete": True}}),
        _gemini_audio_message(b"fresh audio"),
        json.dumps({
            "serverContent": {
                "outputTranscription": {"text": "I heard your interruption."}
            }
        }),
        json.dumps({"serverContent": {"turnComplete": True}}),
    ])
    pipeline._audio_playout_task = asyncio.create_task(pipeline._audio_playout_loop())

    await pipeline._receive_loop()
    await asyncio.wait_for(pipeline._audio_queue.join(), timeout=1)

    assert sent_chunks == [b"fresh audio"]
    assert transcripts == [("Kevin", "I heard your interruption.")]
    assert pipeline._transcript_lines == ["Kevin: I heard your interruption."]
    assert not call_completed.is_set()
    await pipeline.stop()


def test_vcard_ignores_generic_or_wrong_service_type_labels():
    vcard = generate_vcard({
        "owner_name": "Deli Matsuo",
        "business_name": "Matsuo Plumbing",
        "twilio_number": "+15555550123",
        "service_type": "personal",
    })

    assert "FN:Deli Matsuo\r\n" in vcard
    assert "personal" not in vcard
