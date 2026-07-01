import os

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from app.services.voice_pipeline import VoicePipeline


async def _noop(*_args, **_kwargs):
    return None


class FakeTTSResponse:
    status_code = 200
    text = ""

    def __init__(self, content: bytes):
        self.content = content


class FakeTTSClient:
    def __init__(self, content: bytes):
        self.content = content
        self.requests = []

    async def post(self, *_args, **kwargs):
        self.requests.append(kwargs)
        return FakeTTSResponse(self.content)


def _pipeline(on_audio_out):
    return VoicePipeline(
        on_audio_out=on_audio_out,
        on_transcript=_noop,
        call_sid="CA_test",
        contractor_config={
            "owner_name": "Alex Rivera",
            "business_name": "Bayview Plumbing & Drain",
            "mode": "business",
            "effective_mode": "business",
        },
    )


@pytest.mark.asyncio
async def test_tts_text_normalizes_currency_for_spoken_audio(monkeypatch):
    spoken_chunks = []

    async def on_audio_out(chunk: bytes):
        spoken_chunks.append(chunk)

    async def no_sleep(_delay: float):
        return None

    pipeline = _pipeline(on_audio_out)
    await pipeline._http_client.aclose()
    pipeline._http_client = FakeTTSClient(b"\xff" * 160)
    pipeline._connected = True
    monkeypatch.setattr("app.services.voice_pipeline.asyncio.sleep", no_sleep)

    await pipeline._speak(
        "Yes, it typically runs $175 to $650, plus a $95 diagnostic fee."
    )

    request_text = pipeline._http_client.requests[0]["json"]["text"]
    assert request_text == (
        "Yes, it typically runs one hundred seventy-five dollars to six hundred "
        "fifty dollars, plus a ninety-five dollar diagnostic fee."
    )
    assert spoken_chunks


@pytest.mark.asyncio
async def test_tts_streams_mulaw_as_20ms_frames(monkeypatch):
    spoken_chunks = []

    async def on_audio_out(chunk: bytes):
        spoken_chunks.append(chunk)

    async def no_sleep(_delay: float):
        return None

    pipeline = _pipeline(on_audio_out)
    await pipeline._http_client.aclose()
    pipeline._http_client = FakeTTSClient(b"\xff" * 400)
    pipeline._connected = True
    monkeypatch.setattr("app.services.voice_pipeline.asyncio.sleep", no_sleep)

    await pipeline._speak("Hi there.")

    assert [len(chunk) for chunk in spoken_chunks] == [160, 160, 80]
