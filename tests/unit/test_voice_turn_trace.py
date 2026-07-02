import os

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
