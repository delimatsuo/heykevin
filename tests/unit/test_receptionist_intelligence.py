"""Receptionist prompt, call-state, and post-call scope guardrails."""

import ast
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
from app.services.voice_pipeline import (
    build_system_prompt,
    is_owner_availability_hold,
    VoicePipeline,
)
from app.services import voice_pipeline as voice_pipeline_module
from app.webhooks.media_stream import _send_twilio_clear


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


@pytest.mark.asyncio
async def test_twilio_clear_reports_delivery_acknowledgement():
    class RecordingWebSocket:
        def __init__(self):
            self.payloads = []

        async def send_json(self, payload):
            self.payloads.append(payload)

    websocket = RecordingWebSocket()

    delivered = await _send_twilio_clear(
        websocket,
        stream_sid="stream-redacted",
        call_sid="CA_test",
    )
    skipped = await _send_twilio_clear(
        websocket,
        stream_sid="",
        call_sid="CA_test",
    )

    assert delivered is True
    assert skipped is False
    assert websocket.payloads == [{
        "event": "clear",
        "streamSid": "stream-redacted",
    }]


@pytest.mark.asyncio
async def test_twilio_clear_failure_omits_provider_error_text(caplog):
    private_error = "clear failed for private caller data"

    class FailingWebSocket:
        async def send_json(self, _payload):
            raise RuntimeError(private_error)

    caplog.set_level(logging.WARNING, logger="app.webhooks.media_stream")

    delivered = await _send_twilio_clear(
        FailingWebSocket(),
        stream_sid="stream-redacted",
        call_sid="CA_test",
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert delivered is False
    assert "media_event event=twilio_audio_clear_error" in messages
    assert "exception_type=RuntimeError" in messages
    assert private_error not in messages


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
    assert 'Do not say "Sure, I can help with that"' in prompt
    assert "electrical panel" in prompt


def test_business_prompt_instructs_media_followup_without_live_review_claim():
    prompt = build_system_prompt(_plumbing_config())

    assert "upload a photo or short video" in prompt
    assert "Do not claim you can review media live during the phone call" in prompt


def test_business_prompt_prevents_immediate_close_after_availability_check():
    prompt = build_system_prompt(_plumbing_config())

    assert 'Never say "I\'ll pass this along" immediately after' in prompt
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
    prompt = build_system_prompt(
        {
            "owner_name": "Deli Matsuo",
            "mode": "personal",
            "effective_mode": "personal",
        }
    )

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

    assert (
        "When answering pricing questions, answer first, then ask at most one short follow-up question"
        in prompt
    )
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

    assert (
        "Only try the owner live for emergencies or when the caller explicitly asks to speak with"
        in prompt
    )
    assert (
        "For routine and same-day leads, take a concise message instead of putting the caller on hold"
        in prompt
    )
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
async def test_elevenlabs_outbound_delivery_failure_ends_call(caplog):
    call_completed = asyncio.Event()

    class FakeTTSResponse:
        status_code = 200
        content = b"x" * 4000

    class FakeTTSClient:
        async def post(self, *_args, **_kwargs):
            return FakeTTSResponse()

    async def failed_audio_delivery(_chunk: bytes):
        return False

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def on_call_complete():
        call_completed.set()

    pipeline = VoicePipeline(
        on_audio_out=failed_audio_delivery,
        on_transcript=noop_transcript,
        on_call_complete=on_call_complete,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    await pipeline._http_client.aclose()
    pipeline._http_client = FakeTTSClient()
    pipeline._connected = True
    caplog.set_level(logging.INFO, logger="app.services.voice_pipeline")

    await pipeline._speak("private closing message")

    assert call_completed.is_set()
    assert not pipeline._connected
    assert not pipeline._is_speaking
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_timing event=outbound_audio_error" in messages
    assert "private closing message" not in messages


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
        'logger.info(f"Using Gemini Live pipeline', 1
    )[0]

    assert 'caller_phone=active_call.caller_phone if active_call else ""' in gemini_call


def test_media_stream_passes_twilio_start_time_to_gemini_pipeline():
    from app.webhooks import media_stream

    source = inspect.getsource(media_stream.media_stream_ws)
    gemini_call = source.split("pipeline = GeminiPipeline(", 1)[1].split(
        "logger.info(f\"Using Gemini Live pipeline", 1
    )[0]

    assert "media_stream_started_at = time.monotonic()" in source
    assert "call_started_at=media_stream_started_at" in gemini_call


def test_gemini_greetings_are_bounded_and_do_not_volunteer_ai_disclosure():
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

    assert all(len(greeting.split()) <= 24 for greeting in greetings)
    assert greetings[0] == (
        "Hi, thank you for calling Matsuo Plumbing. My name is Kevin. "
        "How can I help you?"
    )
    assert greetings[1] == "Hi, this is Kevin, Deli's assistant. How can I help?"
    assert "Matsuo Plumbing is currently closed" in greetings[2]
    assert all("AI assistant" not in greeting for greeting in greetings)
    assert all("transcribed and summarized" not in greeting for greeting in greetings)


def test_gemini_long_business_name_greetings_remain_within_word_budget():
    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    long_name_config = _plumbing_config() | {
        "business_name": "North Shore Emergency Plumbing and Heating Services",
    }
    business = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        contractor_config=long_name_config,
    )
    after_hours = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        contractor_config=long_name_config,
    )
    after_hours._after_hours = True

    greetings = [business._build_greeting_text(), after_hours._build_greeting_text()]

    assert all(len(greeting.split()) <= 24 for greeting in greetings)
    assert all("AI assistant" not in greeting for greeting in greetings)
    assert all("transcribed and summarized" not in greeting for greeting in greetings)
    assert all("North Shore Emergency Plumbing and Heating" in greeting for greeting in greetings)


def test_system_prompt_discloses_ai_identity_only_when_directly_asked():
    business_prompt = build_system_prompt(_plumbing_config())
    personal_prompt = build_system_prompt(
        {
            "owner_name": "Deli Matsuo",
            "mode": "personal",
            "effective_mode": "personal",
        }
    )

    for prompt in (business_prompt, personal_prompt):
        assert "Do not volunteer that you are an AI assistant" in prompt
        assert "AI assistant from heykevin.one" in prompt


@pytest.mark.asyncio
async def test_gemini_start_sends_exact_greeting_and_safe_startup_metrics(
    monkeypatch,
    caplog,
):
    websocket = _FakeGeminiWebSocket()
    connect_kwargs = {}

    async def fake_connect(*_args, **_kwargs):
        connect_kwargs.update(_kwargs)
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
        assert connect_kwargs == {
            "max_size": 10 * 1024 * 1024,
            "open_timeout": 5.0,
            "ping_interval": 10.0,
            "ping_timeout": 5.0,
            "close_timeout": 1.0,
        }
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
    assert generation_config["max_output_tokens"] == 128
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
    assert pipeline._queued_audio_bytes == 0

    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_audio_backlog_is_bounded_and_requests_one_short_retry(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr("app.services.gemini_pipeline.pcm24k_to_mulaw", lambda chunk: chunk)

    clear_calls = 0

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def clear_audio():
        nonlocal clear_calls
        clear_calls += 1
        return True

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        on_clear_audio=clear_audio,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline.MAX_AUDIO_BACKLOG_BYTES = 10
    pipeline._ensure_audio_playout_task = lambda: None
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._enqueue_model_audio(b"12345678")
    await pipeline._enqueue_model_audio(b"overflow")

    assert pipeline._audio_queue.maxsize == pipeline.MAX_AUDIO_QUEUE_CHUNKS
    assert pipeline._audio_queue.empty()
    assert pipeline._queued_audio_bytes == 0
    assert pipeline._audio_backlog_overflowed
    assert pipeline._interrupt_speaking
    assert clear_calls == 1

    websocket = _FakeGeminiWebSocket([
        json.dumps({"serverContent": {"turnComplete": True}}),
    ])
    pipeline._ws = websocket
    await pipeline._receive_loop()

    assert not pipeline._audio_backlog_overflowed
    assert not pipeline._interrupt_speaking
    assert pipeline._audio_backlog_recoveries == 1
    assert len(websocket.sent_payloads) == 1
    retry_text = websocket.sent_payloads[0]["client_content"]["turns"][0]["parts"][0]["text"]
    assert "one short sentence" in retry_text

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_timing event=audio_backlog_overflow" in messages
    assert "12345678" not in messages


@pytest.mark.asyncio
async def test_gemini_audio_queue_preserves_audio_until_byte_budget_is_reached(
    monkeypatch,
):
    monkeypatch.setattr("app.services.gemini_pipeline.pcm24k_to_mulaw", lambda chunk: chunk)

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ensure_audio_playout_task = lambda: None

    audio_chunk = b"x" * 320  # 40 ms of 8 kHz mulaw audio.
    for _ in range(129):
        await pipeline._enqueue_model_audio(audio_chunk)

    assert pipeline._audio_queue.qsize() == 129
    assert pipeline._queued_audio_bytes == 129 * len(audio_chunk)
    assert not pipeline._audio_backlog_overflowed


@pytest.mark.asyncio
async def test_gemini_nonstaging_second_audio_backlog_overflow_ends_call_without_retry(
    monkeypatch,
):
    call_completed = asyncio.Event()

    async def noop(_arg1, _arg2=None):
        return None

    async def on_call_complete():
        call_completed.set()

    monkeypatch.setattr(
        "app.services.gemini_pipeline.staging_native_live_safety_controls_enabled",
        lambda: False,
    )
    pipeline = GeminiPipeline(
        on_audio_out=noop,
        on_transcript=noop,
        on_call_complete=on_call_complete,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._interrupt_speaking = True
    pipeline._audio_backlog_overflowed = True
    pipeline._audio_backlog_recoveries = pipeline.MAX_AUDIO_BACKLOG_RECOVERIES
    websocket = _FakeGeminiWebSocket([
        json.dumps({"serverContent": {"turnComplete": True}}),
    ])
    pipeline._ws = websocket

    await pipeline._receive_loop()

    assert call_completed.is_set()
    assert websocket.sent_payloads == []


@pytest.mark.asyncio
async def test_gemini_staging_second_audio_backlog_overflow_does_not_end_call(
    monkeypatch,
    caplog,
):
    call_completed = asyncio.Event()

    async def noop(_arg1, _arg2=None):
        return None

    async def on_call_complete():
        call_completed.set()

    monkeypatch.setattr(
        "app.services.gemini_pipeline.staging_native_live_safety_controls_enabled",
        lambda: True,
    )
    pipeline = GeminiPipeline(
        on_audio_out=noop,
        on_transcript=noop,
        on_call_complete=on_call_complete,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._interrupt_speaking = True
    pipeline._audio_backlog_overflowed = True
    pipeline._audio_backlog_recoveries = pipeline.MAX_AUDIO_BACKLOG_RECOVERIES
    pipeline._response_turn_number = 7
    pipeline._ws = _FakeGeminiWebSocket([
        json.dumps({"serverContent": {"turnComplete": True}}),
    ])
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._receive_loop()

    assert not call_completed.is_set()
    assert "voice_timing event=audio_backlog_recovery_exhausted" in caplog.text
    assert "voice_timing event=terminal_action_suppressed" in caplog.text
    assert "reason=staging_audio_backlog_safety" in caplog.text


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
async def test_gemini_outbound_delivery_failure_is_not_counted_as_sent(monkeypatch, caplog):
    monkeypatch.setattr("app.services.gemini_pipeline.pcm24k_to_mulaw", lambda chunk: chunk)
    call_completed = asyncio.Event()

    async def failed_audio_delivery(_chunk: bytes):
        return False

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def on_call_complete():
        call_completed.set()

    pipeline = GeminiPipeline(
        on_audio_out=failed_audio_delivery,
        on_transcript=noop_transcript,
        on_call_complete=on_call_complete,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")
    pipeline._audio_playout_task = asyncio.create_task(pipeline._audio_playout_loop())

    try:
        await pipeline._enqueue_model_audio(b"model audio")
        await asyncio.wait_for(pipeline._audio_queue.join(), timeout=1)

        assert call_completed.is_set()
        assert pipeline._audio_chunks_sent == 0
        assert not pipeline._connected
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "voice_timing event=outbound_audio_error" in messages
        assert "model audio" not in messages
    finally:
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
    monkeypatch.setattr(
        "app.services.gemini_pipeline.staging_native_live_safety_controls_enabled",
        lambda: False,
    )

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
async def test_gemini_staging_suppresses_phrase_triggered_hangup(monkeypatch, caplog):
    completed = asyncio.Event()

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def on_call_complete():
        completed.set()

    monkeypatch.setattr(
        "app.services.gemini_pipeline.staging_native_live_safety_controls_enabled",
        lambda: True,
    )
    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        on_call_complete=on_call_complete,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = _FakeGeminiWebSocket([
        json.dumps({"serverContent": {"turnComplete": True}}),
    ])

    async def goodbye_flush(*_args, **_kwargs):
        return True

    pipeline._flush_kevin_transcript = goodbye_flush
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._receive_loop()

    assert not completed.is_set()
    assert "voice_timing event=terminal_action_suppressed" in caplog.text
    assert "reason=staging_native_safety" in caplog.text


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
        return True

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
    first_marked_turns = []
    final_marked_turns = []

    async def record_audio(chunk: bytes):
        sent_chunks.append(chunk)

    async def record_playback_mark(turn: int):
        first_marked_turns.append(turn)

    async def record_final_playback_mark(turn: int):
        final_marked_turns.append(turn)
        return True

    async def noop_transcript(_speaker: str, _text: str):
        return None

    private_caller_text = "Private caller request"
    private_response_text = "Private response with four words"
    generated_audio = b"a" * 8_000
    pipeline = GeminiPipeline(
        on_audio_out=record_audio,
        on_transcript=noop_transcript,
        on_response_first_media_sent=record_playback_mark,
        on_response_end_media_sent=record_final_playback_mark,
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
        json.dumps({
            "serverContent": {"turnComplete": True},
            "usageMetadata": {
                "promptTokenCount": 4321,
                "responseTokenCount": 123,
                "totalTokenCount": 4444,
            },
        }),
    ])
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._receive_loop()
    await asyncio.wait_for(pipeline._audio_queue.join(), timeout=2)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert sent_chunks == [generated_audio]
    assert first_marked_turns == [1]
    assert final_marked_turns == [1]
    assert messages.count("voice_timing event=response_first_audio") == 1
    assert "latency_ms=" in messages
    assert "latency_basis=input_transcript_fragment" in messages
    assert messages.count("voice_timing event=model_turn_complete") == 1
    assert "generated_audio_ms=1000" in messages
    assert "words=5" in messages
    assert messages.count("voice_timing event=response_playout_drained") == 1
    assert "first_audio_to_playout_ms=" in messages
    assert messages.count("voice_timing event=response_end_playback_mark_armed") == 1
    assert messages.count("voice_timing event=response_end_playback_mark_requested") == 1
    assert "accepted=True" in messages
    assert messages.count("voice_timing event=model_usage") == 1
    assert "prompt_tokens=4321" in messages
    assert "response_tokens=123" in messages
    assert "total_tokens=4444" in messages
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
async def test_gemini_marks_each_audio_turn_when_input_transcription_is_absent(
    monkeypatch,
):
    monkeypatch.setattr("app.services.gemini_pipeline.pcm24k_to_mulaw", lambda chunk: chunk)
    marked_turns = []

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def record_playback_mark(turn: int):
        marked_turns.append(turn)

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        on_response_first_media_sent=record_playback_mark,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = _FakeGeminiWebSocket([
        _gemini_audio_message(b"first response"),
        json.dumps({"serverContent": {"turnComplete": True}}),
        _gemini_audio_message(b"second response"),
        json.dumps({"serverContent": {"turnComplete": True}}),
    ])

    await pipeline._receive_loop()
    await asyncio.wait_for(pipeline._audio_queue.join(), timeout=1)

    assert marked_turns == [1, 2]
    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_interrupted_response_logs_terminal_without_payload(caplog):
    private_text = "private interrupted response sentinel"

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def clear_audio():
        return True

    end_marks = []

    async def record_end_mark(turn: int):
        end_marks.append(turn)
        return True

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        on_clear_audio=clear_audio,
        on_response_end_media_sent=record_end_mark,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._response_turn_number = 2
    pipeline._response_first_audio_at = time.monotonic()
    pipeline._generated_audio_ms = 500
    pipeline._kevin_transcript_buf = [private_text]
    pipeline._response_end_mark_pending = (2, pipeline._audio_epoch)
    pipeline._ws = _FakeGeminiWebSocket([
        json.dumps({"serverContent": {"interrupted": True}}),
        json.dumps({"serverContent": {"turnComplete": True}}),
    ])
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._receive_loop()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert messages.count("voice_timing event=model_turn_interrupted") == 1
    assert "turn=2" in messages
    assert "generated_audio_ms=500" in messages
    assert "voice_timing event=barge_in_clear" in messages
    assert "barge=1" in messages
    assert private_text not in messages
    assert end_marks == []
    assert pipeline._response_end_mark_pending is None
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
        return True

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
    assert "keyword=fire" not in messages
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


def test_gemini_logs_first_caller_transcript_once_without_payload(caplog):
    private_text = "private caller transcript sentinel"

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
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    event = {"inputTranscription": {"text": private_text}}
    pipeline._buffer_caller_transcript(event, {})
    pipeline._buffer_caller_transcript(event, {})

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert messages.count("voice_timing event=first_caller_transcript") == 1
    assert "call_elapsed_ms=" in messages
    assert private_text not in messages


@pytest.mark.asyncio
async def test_gemini_inbound_audio_error_reconnects_once_without_exception_message(
    monkeypatch, caplog
):
    class FailingWebSocket:
        def __init__(self):
            self.closed = False

        async def send(self, _payload: str):
            raise RuntimeError("private caller audio at 100 Market Street")

        async def close(self):
            self.closed = True

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
    failing_ws = FailingWebSocket()
    pipeline._ws = failing_ws
    reconnect_calls = []

    async def fake_start(**kwargs):
        assert failing_ws.closed is True
        reconnect_calls.append(kwargs)
        return True

    monkeypatch.setattr(pipeline, "start", fake_start)

    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline.process_audio_in(b"\xff" * 160)
    await pipeline.process_audio_in(b"\xff" * 160)
    await asyncio.wait_for(pipeline._recovery_task, timeout=1)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert messages.count("voice_timing event=inbound_audio_error") == 1
    assert "exception_type=RuntimeError" in messages
    assert len(reconnect_calls) == 1
    assert "voice_timing event=reconnect_result" in messages
    assert "attempt=1 success=True" in messages
    assert "100 Market Street" not in messages


@pytest.mark.asyncio
async def test_gemini_connect_error_log_excludes_exception_message(monkeypatch, caplog):
    private_error = "provider URL has a private API key and caller details"

    async def failing_connect(*_args, **_kwargs):
        raise RuntimeError(private_error)

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    monkeypatch.setattr("app.services.gemini_pipeline.websockets.connect", failing_connect)
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")
    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )

    started = await pipeline.start(send_greeting=False, start_background_tasks=False)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert started is False
    assert "voice_timing event=connect_error" in messages
    assert "exception_type=RuntimeError" in messages
    assert private_error not in messages


@pytest.mark.asyncio
async def test_gemini_setup_failure_log_excludes_provider_payload(monkeypatch, caplog):
    private_payload = "private provider payload with caller details"

    class SetupFailureWebSocket(_FakeGeminiWebSocket):
        async def recv(self):
            return json.dumps({"error": {"message": private_payload}})

    websocket = SetupFailureWebSocket()

    async def fake_connect(*_args, **_kwargs):
        return websocket

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    monkeypatch.setattr("app.services.gemini_pipeline.websockets.connect", fake_connect)
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")
    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )

    try:
        started = await pipeline.start(send_greeting=False, start_background_tasks=False)
    finally:
        await pipeline.stop()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert started is False
    assert "voice_timing event=setup_error" in messages
    assert private_payload not in messages


@pytest.mark.asyncio
async def test_gemini_receive_error_reconnects_without_logging_exception_message(
    monkeypatch, caplog
):
    private_error = "private transcript fragment in receive exception"

    class FailingReceiveWebSocket:
        def __init__(self):
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError(private_error)

        async def close(self):
            self.closed = True

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
    failing_ws = FailingReceiveWebSocket()
    pipeline._ws = failing_ws
    reconnect_calls = []

    async def fake_start(**kwargs):
        assert failing_ws.closed is True
        reconnect_calls.append(kwargs)
        return True

    monkeypatch.setattr(pipeline, "start", fake_start)
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._receive_loop()

    assert reconnect_calls == [
        {
            "send_greeting": False,
            "start_background_tasks": False,
            "reconnect_context": "",
        }
    ]
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_timing event=receive_error" in messages
    assert "exception_type=RuntimeError" in messages
    assert "voice_timing event=reconnect_result" in messages
    assert "attempt=1 success=True" in messages
    assert private_error not in messages


@pytest.mark.asyncio
async def test_gemini_failed_receive_reconnect_disconnects_and_completes_call(monkeypatch):
    completed = asyncio.Event()

    class FailingReceiveWebSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("private receive failure")

        async def close(self):
            return None

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def on_call_complete():
        completed.set()

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        on_call_complete=on_call_complete,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = FailingReceiveWebSocket()

    async def failed_start(**_kwargs):
        return False

    monkeypatch.setattr(pipeline, "start", failed_start)

    await pipeline._receive_loop()

    assert completed.is_set()
    assert pipeline._connected is False


@pytest.mark.asyncio
async def test_gemini_buffers_and_replays_inbound_audio_while_reconnecting(monkeypatch):
    sent = []

    class RecordingWebSocket:
        async def send(self, payload):
            sent.append(json.loads(payload))

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
    pipeline._reconnecting = True
    pipeline._ws = RecordingWebSocket()
    monkeypatch.setattr(
        "app.services.gemini_pipeline.mulaw_to_pcm16k",
        lambda chunk: chunk,
    )

    await pipeline.process_audio_in(b"first caller frame")
    await pipeline.process_audio_in(b"second caller frame")

    assert sent == []
    assert pipeline._reconnect_audio_buffer_bytes == 37

    flushed = await pipeline._flush_reconnect_audio()

    assert flushed is True
    assert pipeline._reconnecting is False
    assert pipeline._reconnect_audio_buffer_bytes == 0
    replayed_audio = [
        base64.b64decode(payload["realtime_input"]["audio"]["data"])
        for payload in sent
    ]
    assert replayed_audio == [b"first caller frame", b"second caller frame"]


@pytest.mark.asyncio
async def test_gemini_reconnect_replays_buffered_audio_before_new_live_frames(monkeypatch):
    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    replay_started = asyncio.Event()
    release_replay = asyncio.Event()
    sent = []

    monkeypatch.setattr(
        "app.services.gemini_pipeline.mulaw_to_pcm16k",
        lambda chunk: chunk,
    )

    class RecordingWebSocket:
        async def send(self, payload):
            data = base64.b64decode(
                json.loads(payload)["realtime_input"]["audio"]["data"]
            )
            if data == b"buffered caller frame":
                replay_started.set()
                await release_replay.wait()
            sent.append(data)

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

    async def fake_start(**_kwargs):
        pipeline._ws = RecordingWebSocket()
        start_entered.set()
        await release_start.wait()
        return True

    monkeypatch.setattr(pipeline, "start", fake_start)
    recovery_task = asyncio.create_task(
        pipeline._recover_receive_loop(close_websocket=False)
    )
    await asyncio.wait_for(start_entered.wait(), timeout=1)
    await pipeline.process_audio_in(b"buffered caller frame")

    release_start.set()
    await asyncio.wait_for(replay_started.wait(), timeout=1)
    live_send_task = asyncio.create_task(
        pipeline.process_audio_in(b"new live caller frame")
    )
    await asyncio.sleep(0)

    assert not live_send_task.done()

    release_replay.set()
    await asyncio.wait_for(recovery_task, timeout=1)
    await asyncio.wait_for(live_send_task, timeout=1)

    assert sent == [b"buffered caller frame", b"new live caller frame"]
    assert pipeline._reconnecting is False


@pytest.mark.asyncio
async def test_gemini_reconnect_audio_overflow_fails_closed_without_payload_log(
    caplog,
):
    private_frame = b"private caller frame"

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
    pipeline._reconnecting = True
    pipeline.MAX_RECONNECT_AUDIO_BUFFER_BYTES = len(private_frame) - 1
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline.process_audio_in(private_frame)
    replayed = await pipeline._flush_reconnect_audio()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert replayed is False
    assert pipeline._reconnecting is True
    assert pipeline._reconnect_audio_buffer_bytes == 0
    assert list(pipeline._reconnect_audio_buffer) == []
    assert "voice_timing event=inbound_reconnect_audio_overflow" in messages
    assert private_frame.decode() not in messages


@pytest.mark.asyncio
async def test_gemini_cancelled_reconnect_discards_buffer_before_resuming(monkeypatch):
    start_entered = asyncio.Event()
    hold_start = asyncio.Event()

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

    async def blocked_start(**_kwargs):
        start_entered.set()
        await hold_start.wait()
        return True

    monkeypatch.setattr(pipeline, "start", blocked_start)
    recovery_task = asyncio.create_task(
        pipeline._recover_receive_loop(close_websocket=False)
    )
    await asyncio.wait_for(start_entered.wait(), timeout=1)
    await pipeline.process_audio_in(b"buffered caller frame")

    recovery_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery_task

    assert pipeline._reconnecting is False
    assert pipeline._reconnect_audio_buffer_bytes == 0
    assert list(pipeline._reconnect_audio_buffer) == []


@pytest.mark.asyncio
async def test_gemini_waiting_inbound_frame_is_not_sent_after_disconnect():
    sent = []

    class RecordingWebSocket:
        async def send(self, payload):
            sent.append(payload)

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
    pipeline._ws = RecordingWebSocket()
    await pipeline._inbound_audio_lock.acquire()
    send_task = asyncio.create_task(pipeline.process_audio_in(b"caller frame"))
    await asyncio.sleep(0)

    pipeline._connected = False
    pipeline._inbound_audio_lock.release()
    await asyncio.wait_for(send_task, timeout=1)

    assert sent == []


def test_live_voice_logger_calls_do_not_embed_sensitive_runtime_values():
    from app.services import gemini_pipeline
    from app.webhooks import media_stream

    forbidden_names = {
        "api_err",
        "caller_name",
        "caller_phone",
        "close_reason",
        "e",
        "exc",
        "payload",
        "transcript",
        "ws_token",
    }

    for module in (gemini_pipeline, media_stream):
        tree = ast.parse(inspect.getsource(module))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            function = call.func
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "logger"
            ):
                continue

            assert not any(keyword.arg == "exc_info" for keyword in call.keywords)
            referenced_names = {
                node.id
                for argument in call.args
                for node in ast.walk(argument)
                if isinstance(node, ast.Name)
            }
            assert referenced_names.isdisjoint(forbidden_names), ast.unparse(call)
            assert "json.dumps(data)" not in ast.unparse(call)


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
        return True

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
    assert "voice_timing event=barge_in_clear_failed" not in messages
    assert "clear_ms=" in messages
    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_barge_in_preempts_blocked_tool_and_discards_result(monkeypatch):
    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()
    release_tool = asyncio.Event()
    clear_completed = asyncio.Event()

    async def blocked_tool(_self, _name, _args):
        tool_started.set()
        try:
            await release_tool.wait()
        except asyncio.CancelledError:
            tool_cancelled.set()
            raise
        return json.dumps({"available": True})

    monkeypatch.setattr(VoicePipeline, "_execute_tool", blocked_tool)

    class ToolThenInterruptWebSocket(_FakeGeminiWebSocket):
        async def __anext__(self):
            if not self.messages:
                raise StopAsyncIteration
            if len(self.messages) == 1:
                await tool_started.wait()
            return self.messages.pop(0)

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def clear_audio():
        clear_completed.set()
        return True

    websocket = ToolThenInterruptWebSocket([
        json.dumps({
            "toolCall": {
                "functionCalls": [{
                    "id": "tool-call-redacted",
                    "name": "check_availability",
                    "args": {},
                }]
            }
        }),
        json.dumps({"serverContent": {"interrupted": True}}),
    ])
    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        on_clear_audio=clear_audio,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = websocket

    await asyncio.wait_for(pipeline._receive_loop(), timeout=1)
    await asyncio.wait_for(tool_cancelled.wait(), timeout=1)

    assert clear_completed.is_set()
    assert not release_tool.is_set()
    assert not any("tool_response" in payload for payload in websocket.sent_payloads)
    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_discards_cancellation_resistant_stale_tool_result(monkeypatch, caplog):
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    tool_finished = asyncio.Event()

    async def cancellation_resistant_tool(_self, _name, _args):
        tool_started.set()
        try:
            await release_tool.wait()
        except asyncio.CancelledError:
            await release_tool.wait()
        tool_finished.set()
        return json.dumps({"available": True})

    monkeypatch.setattr(
        VoicePipeline,
        "_execute_tool",
        cancellation_resistant_tool,
    )

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    websocket = _FakeGeminiWebSocket()
    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = websocket
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    pipeline._schedule_tool_calls([{
        "id": "tool-call-redacted",
        "name": "check_availability",
        "args": {},
    }])
    await asyncio.wait_for(tool_started.wait(), timeout=1)

    pipeline._invalidate_tool_task("reconnect")
    release_tool.set()
    await asyncio.wait_for(tool_finished.wait(), timeout=1)
    await asyncio.sleep(0)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert websocket.sent_payloads == []
    assert "voice_timing event=tool_result_discarded" in messages
    assert pipeline._tool_task is None


@pytest.mark.asyncio
async def test_gemini_barge_in_clear_failure_is_not_reported_as_success(caplog):
    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    async def failed_clear():
        return False

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        on_clear_audio=failed_clear,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = _FakeGeminiWebSocket([
        json.dumps({"serverContent": {"interrupted": True}}),
    ])
    caplog.set_level(logging.INFO, logger="app.services.gemini_pipeline")

    await pipeline._receive_loop()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_timing event=barge_in_clear_failed" in messages
    assert "voice_timing event=barge_in_clear call=" not in messages


@pytest.mark.asyncio
async def test_gemini_barge_in_preserves_co_delivered_caller_transcript():
    transcripts = []

    async def noop_audio(_chunk: bytes):
        return None

    async def record_transcript(speaker: str, text: str):
        transcripts.append((speaker, text))

    async def clear_audio():
        return True

    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=record_transcript,
        on_clear_audio=clear_audio,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = _FakeGeminiWebSocket([
        json.dumps({
            "serverContent": {
                "interrupted": True,
                "inputTranscription": {"text": "I still need help."},
            }
        }),
        json.dumps({
            "serverContent": {
                "modelTurn": {"parts": [{"text": "Acknowledged."}]},
            }
        }),
    ])

    await pipeline._receive_loop()

    assert transcripts == [("Caller", "I still need help.")]
    assert pipeline._transcript_lines == ["Caller: I still need help."]


@pytest.mark.asyncio
async def test_gemini_background_tool_task_returns_current_result(monkeypatch):
    response_sent = asyncio.Event()

    async def available_tool(_self, _name, _args):
        return json.dumps({"available": True})

    monkeypatch.setattr(VoicePipeline, "_execute_tool", available_tool)

    class RecordingWebSocket(_FakeGeminiWebSocket):
        async def send(self, payload: str):
            await super().send(payload)
            response_sent.set()

    async def noop_audio(_chunk: bytes):
        return None

    async def noop_transcript(_speaker: str, _text: str):
        return None

    websocket = RecordingWebSocket([
        json.dumps({
            "toolCall": {
                "functionCalls": [{
                    "id": "tool-call-redacted",
                    "name": "check_availability",
                    "args": {},
                }]
            }
        }),
    ])
    pipeline = GeminiPipeline(
        on_audio_out=noop_audio,
        on_transcript=noop_transcript,
        call_sid="CA_test",
        contractor_config=_plumbing_config(),
    )
    pipeline._connected = True
    pipeline._ws = websocket

    await pipeline._receive_loop()
    await asyncio.wait_for(response_sent.wait(), timeout=1)

    assert websocket.sent_payloads == [{
        "tool_response": {
            "function_responses": [{
                "id": "tool-call-redacted",
                "name": "check_availability",
                "response": {"available": True},
            }]
        }
    }]
    await pipeline.stop()


@pytest.mark.asyncio
async def test_gemini_barge_in_discards_stale_output_until_turn_completes(monkeypatch):
    sent_chunks = []
    marked_turns = []
    transcripts = []
    call_completed = asyncio.Event()

    monkeypatch.setattr("app.services.gemini_pipeline.pcm24k_to_mulaw", lambda chunk: chunk)

    async def record_audio(chunk: bytes):
        sent_chunks.append(chunk)

    async def record_playback_mark(turn: int):
        marked_turns.append(turn)

    async def record_transcript(speaker: str, text: str):
        transcripts.append((speaker, text))

    async def on_call_complete():
        call_completed.set()

    async def noop_clear():
        return True

    pipeline = GeminiPipeline(
        on_audio_out=record_audio,
        on_transcript=record_transcript,
        on_clear_audio=noop_clear,
        on_response_first_media_sent=record_playback_mark,
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
    assert marked_turns == [2]
    assert transcripts == [("Kevin", "I heard your interruption.")]
    assert pipeline._transcript_lines == ["Kevin: I heard your interruption."]
    assert not call_completed.is_set()
    await pipeline.stop()


def test_vcard_ignores_generic_or_wrong_service_type_labels():
    vcard = generate_vcard(
        {
            "owner_name": "Deli Matsuo",
            "business_name": "Matsuo Plumbing",
            "twilio_number": "+15555550123",
            "service_type": "personal",
        }
    )

    assert "FN:Deli Matsuo\r\n" in vcard
    assert "personal" not in vcard


def test_stateful_receptionist_controller_is_not_live_wired_in_this_slice():
    """This slice keeps live-call behavior unchanged while controller tests define policy."""
    import app.services.gemini_pipeline as gemini_pipeline
    import app.services.voice_pipeline as voice_pipeline

    assert not hasattr(gemini_pipeline.GeminiPipeline, "_receptionist_controller")
    assert not hasattr(voice_pipeline.VoicePipeline, "_receptionist_controller")
    live_sources = "\n".join(
        [
            inspect.getsource(gemini_pipeline),
            inspect.getsource(voice_pipeline),
        ]
    )
    assert "receptionist_state" not in live_sources
    assert "dialogue_planner" not in live_sources
    assert "instruction_composer" not in live_sources
    assert "receptionist_replay" not in live_sources
