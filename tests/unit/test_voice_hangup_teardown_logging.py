"""Requests cut off by a caller hanging up are not voice-provider failures.

stop() closes the pipeline's shared HTTP client. A Claude or ElevenLabs request
still in flight then raises (ReadError, or RuntimeError on the closed client).
Before this guard each hangup during a reply logged claude_request_error twice
and tts_request_error once at WARNING, which is exactly what the production
"voice provider warnings" alert matches, and appended an "I'm having trouble"
fallback to a call that had already ended.
"""

import logging
import os

import httpx
import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services import voice_pipeline as voice_pipeline_module
from app.services.voice_pipeline import VoicePipeline


ALERTED_EVENTS = ("event=claude_request_error", "event=tts_request_error")


def _config():
    return {
        "business_name": "Test Plumbing",
        "owner_name": "Alex",
        "mode": "business",
    }


async def _pipeline(transcripts, spoken):
    async def on_audio_out(_chunk: bytes):
        return None

    async def on_transcript(speaker: str, text: str):
        transcripts.append((speaker, text))

    pipeline = VoicePipeline(
        on_audio_out=on_audio_out,
        on_transcript=on_transcript,
        call_sid="CA_test",
        contractor_config=_config(),
    )
    await pipeline._http_client.aclose()

    async def fake_speak(text: str, **_kwargs):
        spoken.append(text)
        return True

    pipeline._speak = fake_speak
    return pipeline


class HangupClient:
    """Simulates stop() closing the client while the request is in flight."""

    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.calls = 0

    async def post(self, *_args, **_kwargs):
        self.calls += 1
        self.pipeline._connected = False
        raise httpx.ReadError("connection closed")


class FailingClient:
    def __init__(self):
        self.calls = 0

    async def post(self, *_args, **_kwargs):
        self.calls += 1
        raise httpx.ReadError("provider dropped the connection")


@pytest.fixture
def no_backoff(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(voice_pipeline_module.asyncio, "sleep", no_sleep)


@pytest.mark.asyncio
async def test_claude_request_cut_off_by_hangup_is_not_a_provider_error(caplog, no_backoff):
    transcripts, spoken = [], []
    pipeline = await _pipeline(transcripts, spoken)
    pipeline._connected = True
    client = HangupClient(pipeline)
    pipeline._http_client = client
    caplog.set_level(logging.INFO, logger="app.services.voice_pipeline")

    await pipeline._handle_caller_speech("Can someone come out today?")

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=claude_request_abandoned" in messages
    assert "exception_type=ReadError" in messages
    assert not any(event in messages for event in ALERTED_EVENTS)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert client.calls == 1, "must not retry against a closed client"
    assert spoken == []
    assert ("Kevin", "I'm sorry, I'm having trouble. Could you repeat that?") not in transcripts


@pytest.mark.asyncio
async def test_claude_failure_during_live_call_still_warns_and_falls_back(caplog, no_backoff):
    transcripts, spoken = [], []
    pipeline = await _pipeline(transcripts, spoken)
    pipeline._connected = True
    client = FailingClient()
    pipeline._http_client = client
    caplog.set_level(logging.INFO, logger="app.services.voice_pipeline")

    await pipeline._handle_caller_speech("Can someone come out today?")

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert sum("event=claude_request_error" in w for w in warnings) == 2
    assert client.calls == 2
    assert spoken == ["I'm sorry, I'm having trouble. Could you repeat that?"]
    assert "claude_request_abandoned" not in "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_tts_request_after_hangup_is_not_a_provider_error(caplog):
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._call_sid = "CA_test"
    pipeline._tts_voice_id = "voice"
    pipeline._tts_model_id = "model"
    pipeline._connected = False

    class ClosedClient:
        async def post(self, *_args, **_kwargs):
            raise RuntimeError("Cannot send a request, as the client has been closed.")

    pipeline._http_client = ClosedClient()
    caplog.set_level(logging.INFO, logger="app.services.voice_pipeline")

    assert await pipeline._speak("I'm sorry, I'm having trouble.") is False

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=tts_request_abandoned" in messages
    assert "exception_type=RuntimeError" in messages
    assert "event=tts_request_error" not in messages
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_tts_failure_during_live_call_still_warns(caplog):
    pipeline = VoicePipeline.__new__(VoicePipeline)
    pipeline._call_sid = "CA_test"
    pipeline._tts_voice_id = "voice"
    pipeline._tts_model_id = "model"
    pipeline._connected = True

    class FailingTtsClient:
        async def post(self, *_args, **_kwargs):
            raise httpx.ReadError("provider dropped the connection")

    pipeline._http_client = FailingTtsClient()
    caplog.set_level(logging.INFO, logger="app.services.voice_pipeline")

    assert await pipeline._speak("Hello") is False

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert any("event=tts_request_error" in w for w in warnings)
