"""Receptionist prompt, call-state, and post-call scope guardrails."""

import asyncio
import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services.gemini_pipeline import GeminiPipeline
from app.services import job_card as job_card_module
from app.services.job_card import _build_extraction_prompt, extract_job_card
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


def test_business_prompt_prioritizes_fast_live_phone_turns():
    prompt = build_system_prompt(_plumbing_config())

    assert "LIVE PHONE LATENCY POLICY" in prompt
    assert "Keep most replies under 12 words" in prompt
    assert "Ask exactly one question per turn" in prompt
    assert "Do not recap the caller's address, issue, or phone number unless they ask" in prompt
    assert "For closing, keep it under 10 seconds of speech" in prompt


def test_business_prompt_confirms_full_phone_number_concisely():
    prompt = build_system_prompt(_plumbing_config())

    assert "Repeat the full callback number once" in prompt
    assert "Use three compact groups" in prompt
    assert "Do not use long hyphenated digit-by-digit readbacks" in prompt
    assert "Do not repeat the full callback number more than once unless the caller asks" in prompt
    assert "last four digits only" not in prompt
    assert "Always read back phone numbers digit by digit" not in prompt


def test_business_prompt_defers_callback_number_until_followup_intent():
    prompt = build_system_prompt(_plumbing_config())

    assert "Do not ask for a callback number during early qualification" in prompt
    assert "Treat caller ID as the default callback number when available" in prompt
    assert "only if they want a callback, dispatch, booking, or owner handoff" in prompt
    assert "confirm with the last four digits" in prompt
    assert "Is the number ending in eight six six seven the best one" in prompt
    assert "Get their name, callback number, city/town" not in prompt
    assert "identify caller, issue, city/town or service area, urgency, and callback number" not in prompt


def test_business_prompt_speaks_hours_in_words():
    prompt = build_system_prompt(_plumbing_config())

    assert "When saying business hours, speak them in words" in prompt
    assert "say \"seven in the morning to six in the evening\"" in prompt
    assert "Do not say compact forms like \"7 AM to 6 PM\"" in prompt


def test_business_prompt_collects_city_not_full_street_address_during_screening():
    prompt = build_system_prompt(_plumbing_config())

    assert "city/town or service area" in prompt
    assert "Do not ask for a full street address during AI screening" in prompt
    assert "Only capture a full street address if the caller volunteers it" in prompt
    assert "owner-approved booking or dispatch" in prompt
    assert "service address when relevant" not in prompt
    assert "address if relevant" not in prompt
    assert "collect callback/location" not in prompt


def test_job_card_extraction_prompt_can_classify_out_of_scope_requests():
    prompt = _build_extraction_prompt(
        "Caller: Can you help with my electric panel?\nKevin: Matsuo Plumbing may not be the right company.",
        _plumbing_config(),
    )

    assert '"out_of_scope"' in prompt
    assert "electrical panel or breaker request is out_of_scope" in prompt
    assert "Only use service_request when the caller's request appears related" in prompt
    assert "Water heater services" in prompt


def test_job_card_prompt_treats_street_address_as_optional_volunteered_data():
    prompt = _build_extraction_prompt(
        "Caller: I have a leak in San Mateo.\nKevin: I'll pass this to Deli.",
        _plumbing_config(),
    )

    assert "service_area: string (city/town or service area if given, empty if not)" in prompt
    assert "address: string (full street address only if the caller volunteered it, empty if not)" in prompt
    assert "Missing a full street address is acceptable" in prompt
    assert "service address if given" not in prompt


@pytest.mark.asyncio
async def test_extract_job_card_defaults_service_area_when_model_omits_it(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "content": [
                    {
                        "text": (
                            '{"call_type":"service_request","caller_name":"Pat",'
                            '"business_name":"","address":"","issue_description":"leak",'
                            '"urgency":"routine","message":"","callback_number":""}'
                        )
                    }
                ]
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(job_card_module.httpx, "AsyncClient", FakeClient)

    result = await extract_job_card(
        "Caller: I have a leak in San Mateo.",
        "+15551234567",
        contractor=_plumbing_config(),
    )

    assert result["service_area"] == ""


@pytest.mark.asyncio
async def test_extract_job_card_reads_first_text_content_block(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "stop_reason": "end_turn",
                "content": [
                    {"type": "thinking", "thinking": "internal notes are not text"},
                    {
                        "type": "text",
                        "text": (
                            '{"call_type":"service_request","caller_name":"Pat",'
                            '"business_name":"","service_area":"San Francisco",'
                            '"address":"","issue_description":"sink leak",'
                            '"urgency":"same_day","message":"","callback_number":"6504228667"}'
                        ),
                    },
                ],
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(job_card_module.httpx, "AsyncClient", FakeClient)

    result = await extract_job_card(
        "Caller: This is Pat. My sink is leaking in San Francisco. Call me at 6504228667.",
        "+15551234567",
        contractor=_plumbing_config(),
    )

    assert result["call_type"] == "service_request"
    assert result["caller_name"] == "Pat"
    assert result["service_area"] == "San Francisco"
    assert result["issue_description"] == "sink leak"
    assert result["callback_number"] == "6504228667"


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
async def test_voice_pipeline_uses_configured_voice_model(monkeypatch):
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

    assert hasattr(voice_pipeline_module.settings, "anthropic_voice_model")
    monkeypatch.setattr(voice_pipeline_module.settings, "anthropic_model", "claude-sonnet-5")
    monkeypatch.setattr(voice_pipeline_module.settings, "anthropic_voice_model", "claude-haiku-4-5")

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

    assert voice_pipeline_module.settings.anthropic_model == "claude-sonnet-5"
    assert request_bodies[0]["model"] == "claude-haiku-4-5"
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


def test_vcard_ignores_generic_or_wrong_service_type_labels():
    vcard = generate_vcard({
        "owner_name": "Deli Matsuo",
        "business_name": "Matsuo Plumbing",
        "twilio_number": "+15555550123",
        "service_type": "personal",
    })

    assert "FN:Deli Matsuo\r\n" in vcard
    assert "personal" not in vcard
