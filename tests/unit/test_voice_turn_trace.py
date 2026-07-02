import os
import time

import pytest
import httpx

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services import voice_pipeline as voice_pipeline_module
from app.services.voice_pipeline import VoicePipeline


async def _noop(*_args, **_kwargs):
    return None


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, *, json_body=None, content=b""):
        self._json_body = json_body or {}
        self.content = content

    def json(self):
        return self._json_body


class FakeTurnClient:
    def __init__(self):
        self.requests = []

    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if "anthropic.com" in url:
            return FakeResponse(json_body={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "We can help with that."}],
            })
        return FakeResponse(content=b"\xff" * 320)


class FakeRetryClient:
    def __init__(self):
        self.anthropic_timeouts = []
        self.anthropic_calls = 0

    async def post(self, url, **kwargs):
        if "anthropic.com" in url:
            self.anthropic_calls += 1
            self.anthropic_timeouts.append(kwargs.get("timeout"))
            if self.anthropic_calls == 1:
                raise httpx.ReadTimeout("slow Anthropic response")
            return FakeResponse(json_body={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Yes, I can help."}],
            })
        return FakeResponse(content=b"")


class FakeEmptyAnthropicClient:
    async def post(self, url, **_kwargs):
        if "anthropic.com" in url:
            return FakeResponse(json_body={
                "stop_reason": "end_turn",
                "content": [],
            })
        return FakeResponse(content=b"\xff" * 320)


class FakeScriptedAnthropicClient:
    def __init__(self, responses):
        self.responses = list(responses)

    async def post(self, url, **_kwargs):
        if "anthropic.com" in url:
            if self.responses:
                return FakeResponse(json_body=self.responses.pop(0))
            return FakeResponse(json_body={
                "stop_reason": "end_turn",
                "content": [],
            })
        return FakeResponse(content=b"\xff" * 320)


class FakeNoAnthropicClient:
    calls = 0

    async def post(self, url, **_kwargs):
        self.calls += 1
        if "anthropic.com" in url:
            raise AssertionError("Anthropic should not be called for deterministic caller-ID readback")
        return FakeResponse(content=b"\xff" * 320)


def _pipeline(on_audio_out):
    return VoicePipeline(
        on_audio_out=on_audio_out,
        on_transcript=_noop,
        call_sid="CA_trace_test",
        contractor_config={
            "contractor_id": "contractor-1",
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )


@pytest.mark.asyncio
async def test_voice_turn_emits_stage_timings_without_raw_caller_text(monkeypatch):
    trace_events = []
    spoken_chunks = []

    def fake_trace_event(_logger, event, **fields):
        trace_events.append({"event": event, **fields})

    async def on_audio_out(chunk: bytes):
        spoken_chunks.append(chunk)

    async def no_sleep(_delay: float):
        return None

    monkeypatch.setattr(voice_pipeline_module, "trace_event", fake_trace_event)
    monkeypatch.setattr(voice_pipeline_module.asyncio, "sleep", no_sleep)

    pipeline = _pipeline(on_audio_out)
    await pipeline._http_client.aclose()
    pipeline._http_client = FakeTurnClient()
    pipeline._connected = True

    await pipeline._process_utterance("my private phone is 650-422-8667", turn_id=7)

    event_names = [event["event"] for event in trace_events]
    assert "voice_turn_utterance_final" in event_names
    assert "voice_turn_llm_start" in event_names
    assert "voice_turn_llm_end" in event_names
    assert "voice_turn_tts_start" in event_names
    assert "voice_turn_tts_first_audio" in event_names
    assert "voice_turn_tts_end" in event_names
    assert spoken_chunks

    for event in trace_events:
        assert event["call_sid"] == "CA_trace_test"
        assert event["contractor_id"] == "contractor-1"
        assert event["turn_id"] == 7

    serialized = repr(trace_events)
    assert "650-422-8667" not in serialized
    assert "my private phone" not in serialized


@pytest.mark.asyncio
async def test_anthropic_retry_uses_fast_voice_timeout_without_fixed_sleep(monkeypatch):
    sleep_calls = []

    async def on_audio_out(_chunk: bytes):
        return None

    async def record_sleep(delay: float):
        sleep_calls.append(delay)

    monkeypatch.setattr(voice_pipeline_module.asyncio, "sleep", record_sleep)

    pipeline = _pipeline(on_audio_out)
    await pipeline._http_client.aclose()
    retry_client = FakeRetryClient()
    pipeline._http_client = retry_client
    pipeline._connected = True

    await pipeline._process_utterance("hello", turn_id=3)

    assert retry_client.anthropic_calls == 2
    assert retry_client.anthropic_timeouts[0] <= 4.0
    assert 2 not in sleep_calls


@pytest.mark.asyncio
async def test_stale_filler_utterance_is_dropped_after_prior_response(monkeypatch):
    trace_events = []
    transcript_lines = []
    spoken = []

    def fake_trace_event(_logger, event, **fields):
        trace_events.append({"event": event, **fields})

    async def on_transcript(speaker: str, text: str):
        transcript_lines.append((speaker, text))

    async def fake_speak(text: str):
        spoken.append(text)

    monkeypatch.setattr(voice_pipeline_module, "trace_event", fake_trace_event)

    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=on_transcript,
        call_sid="CA_stale_filler_test",
        contractor_config={
            "contractor_id": "contractor-1",
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )
    await pipeline._http_client.aclose()
    fake_client = FakeTurnClient()
    pipeline._http_client = fake_client
    pipeline._speak = fake_speak
    pipeline._last_response_completed_at = time.monotonic()

    queued_at = pipeline._last_response_completed_at - 0.5

    await pipeline._process_utterance("Hello?", turn_id=4, queued_at=queued_at)

    assert transcript_lines == []
    assert spoken == []
    assert not any("anthropic.com" in url for url, _kwargs in fake_client.requests)
    assert any(
        event["event"] == "voice_turn_stale_utterance_dropped"
        and event["turn_id"] == 4
        and event["reason"] == "stale_filler"
        for event in trace_events
    )


@pytest.mark.asyncio
async def test_stale_substantive_utterance_still_uses_claude(monkeypatch):
    transcript_lines = []
    spoken = []

    async def on_transcript(speaker: str, text: str):
        transcript_lines.append((speaker, text))

    async def fake_speak(text: str):
        spoken.append(text)

    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=on_transcript,
        call_sid="CA_stale_substantive_test",
        contractor_config={
            "contractor_id": "contractor-1",
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )
    await pipeline._http_client.aclose()
    fake_client = FakeTurnClient()
    pipeline._http_client = fake_client
    pipeline._speak = fake_speak
    pipeline._last_response_completed_at = time.monotonic()

    queued_at = pipeline._last_response_completed_at - 0.5

    await pipeline._process_utterance("My name is Jonathan.", turn_id=5, queued_at=queued_at)

    assert transcript_lines == [("Kevin", "We can help with that.")]
    assert spoken == ["We can help with that."]
    assert any("anthropic.com" in url for url, _kwargs in fake_client.requests)


@pytest.mark.asyncio
async def test_empty_anthropic_response_speaks_business_fallback(monkeypatch):
    trace_events = []
    transcript_lines = []
    spoken_chunks = []

    def fake_trace_event(_logger, event, **fields):
        trace_events.append({"event": event, **fields})

    async def on_transcript(speaker: str, text: str):
        transcript_lines.append((speaker, text))

    async def on_audio_out(chunk: bytes):
        spoken_chunks.append(chunk)

    async def no_sleep(_delay: float):
        return None

    monkeypatch.setattr(voice_pipeline_module, "trace_event", fake_trace_event)
    monkeypatch.setattr(voice_pipeline_module.asyncio, "sleep", no_sleep)

    pipeline = VoicePipeline(
        on_audio_out=on_audio_out,
        on_transcript=on_transcript,
        call_sid="CA_empty_response_test",
        contractor_config={
            "contractor_id": "contractor-1",
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )
    await pipeline._http_client.aclose()
    pipeline._http_client = FakeEmptyAnthropicClient()
    pipeline._connected = True

    await pipeline._process_utterance("there is standing water", turn_id=6)

    assert ("Kevin", "I'm here. What city or town are you in?") in transcript_lines
    assert spoken_chunks
    assert any(
        event["event"] == "voice_turn_no_spoken_response"
        and event["reason"] == "empty_response"
        and event["stop_reason"] == "end_turn"
        and event["content_block_types"] == []
        and event["has_issue"] is True
        and event["has_service_area"] is False
        for event in trace_events
    )


@pytest.mark.asyncio
async def test_empty_response_after_city_moves_to_handoff_instead_of_reasking_city(monkeypatch):
    transcript_lines = []
    spoken_chunks = []

    async def on_transcript(speaker: str, text: str):
        transcript_lines.append((speaker, text))

    async def on_audio_out(chunk: bytes):
        spoken_chunks.append(chunk)

    async def no_sleep(_delay: float):
        return None

    monkeypatch.setattr(voice_pipeline_module.asyncio, "sleep", no_sleep)

    pipeline = VoicePipeline(
        on_audio_out=on_audio_out,
        on_transcript=on_transcript,
        call_sid="CA_san_francisco_loop_test",
        contractor_config={
            "contractor_id": "contractor-1",
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )
    await pipeline._http_client.aclose()
    pipeline._http_client = FakeScriptedAnthropicClient([
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Can I get your name first?"}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "What's the best callback number?"}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Got it. What city are you in?"}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Just to confirm, is that San Francisco city?"}]},
        {"stop_reason": "end_turn", "content": []},
    ])
    pipeline._connected = True

    await pipeline._process_utterance("Can you do emergency service? I have a sink leaking.", turn_id=1)
    await pipeline._process_utterance("My name is Jonathan.", turn_id=2)
    await pipeline._process_utterance("(650) 422-8667.", turn_id=3)
    await pipeline._process_utterance("I'm in San Francisco, California.", turn_id=4)
    await pipeline._process_utterance("Yes.", turn_id=5)

    assert transcript_lines[-1] == ("Kevin", "Got it. I'm going to try Alex now, one moment.")
    assert "city or town" not in transcript_lines[-1][1].lower()
    assert spoken_chunks
    if pipeline._unavailable_task:
        pipeline._unavailable_task.cancel()


@pytest.mark.asyncio
async def test_business_fallback_qualifies_before_callback_number():
    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="CA_state_test",
        contractor_config={
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )
    await pipeline._http_client.aclose()

    assert pipeline._no_spoken_response_fallback_text() == "I'm here. What's going on?"
    pipeline._update_intake_state_from_caller("I have a sink leak.")
    assert pipeline._no_spoken_response_fallback_text() == "I'm here. What city or town are you in?"
    pipeline._update_intake_state_from_caller("I'm in San Francisco.")
    assert pipeline._no_spoken_response_fallback_text() == "I'm here. Could I get your name?"
    pipeline._update_intake_state_from_caller("My name is Jonathan.")
    assert pipeline._no_spoken_response_fallback_text() == "Got it. I'll make sure Alex gets this message."


@pytest.mark.asyncio
async def test_direct_city_answer_counts_as_service_area():
    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="CA_city_test",
        contractor_config={
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )
    await pipeline._http_client.aclose()

    pipeline._update_intake_state_from_caller("I need a toilet replacement.")
    pipeline._update_intake_state_from_caller("South San Francisco.")

    assert pipeline._no_spoken_response_fallback_text() == "I'm here. Could I get your name?"


@pytest.mark.asyncio
async def test_urgent_fallback_handoff_uses_caller_id_without_asking_callback_number():
    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="CA_caller_id_test",
        caller_phone="+16504228667",
        contractor_config={
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )
    await pipeline._http_client.aclose()

    pipeline._update_intake_state_from_caller("My name is Jonathan.")
    pipeline._update_intake_state_from_caller("I have an emergency sink leak.")
    pipeline._update_intake_state_from_caller("I'm in San Francisco.")

    fallback = pipeline._no_spoken_response_fallback_text()

    assert fallback == "Got it. I'm going to try Alex now, one moment."
    assert "callback" not in fallback.lower()
    assert "number" not in fallback.lower()


@pytest.mark.asyncio
async def test_callback_confirmation_uses_caller_id_last_four():
    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        call_sid="CA_last_four_test",
        caller_phone="+16504228667",
        contractor_config={
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )
    await pipeline._http_client.aclose()

    text = pipeline._caller_id_callback_confirmation_text()

    assert text == "Is the number ending in eight six six seven the best one for Alex to call back?"
    assert "6504228667" not in text
    assert "+16504228667" not in text


@pytest.mark.asyncio
async def test_repeat_caller_id_number_is_handled_without_claude():
    transcript_lines = []
    spoken = []

    async def on_transcript(speaker: str, text: str):
        transcript_lines.append((speaker, text))

    async def fake_speak(text: str):
        spoken.append(text)

    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=on_transcript,
        call_sid="CA_repeat_number_test",
        caller_phone="+16504228667",
        contractor_config={
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )
    await pipeline._http_client.aclose()
    no_anthropic_client = FakeNoAnthropicClient()
    pipeline._http_client = no_anthropic_client
    pipeline._speak = fake_speak

    await pipeline._handle_caller_speech("Can you repeat that number to me?", turn_id=9)

    assert transcript_lines == [("Kevin", "I have six five zero, four two two, eight six six seven.")]
    assert spoken == ["I have six five zero, four two two, eight six six seven."]
    assert no_anthropic_client.calls == 0


@pytest.mark.asyncio
async def test_volunteered_phone_number_still_uses_claude_flow():
    transcript_lines = []

    async def on_transcript(speaker: str, text: str):
        transcript_lines.append((speaker, text))

    async def fake_speak(_text: str):
        return None

    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=on_transcript,
        call_sid="CA_volunteered_number_test",
        caller_phone="+16504228667",
        contractor_config={
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )
    await pipeline._http_client.aclose()
    fake_client = FakeTurnClient()
    pipeline._http_client = fake_client
    pipeline._speak = fake_speak

    await pipeline._handle_caller_speech("My phone number is 415 555 1212.", turn_id=10)

    assert transcript_lines == [("Kevin", "We can help with that.")]
    assert any("anthropic.com" in url for url, _kwargs in fake_client.requests)
