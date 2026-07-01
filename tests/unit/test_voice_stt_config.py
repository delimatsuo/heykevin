import asyncio
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


def _pipeline():
    return VoicePipeline(
        on_audio_out=_noop,
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
async def test_deepgram_endpointing_waits_600ms_before_finalizing(monkeypatch):
    connected_urls = []

    class FakeDeepgramWebSocket:
        async def recv(self):
            await asyncio.sleep(60)

        async def close(self):
            return None

    async def fake_connect(url, *_args, **_kwargs):
        connected_urls.append(url)
        return FakeDeepgramWebSocket()

    monkeypatch.setattr("app.services.voice_pipeline.websockets.connect", fake_connect)

    pipeline = _pipeline()
    await pipeline._http_client.aclose()
    try:
        assert await pipeline._connect_deepgram()
        assert "&endpointing=600" in connected_urls[0]
        assert "&utterance_end_ms=1000" in connected_urls[0]
    finally:
        if pipeline._deepgram_task:
            pipeline._deepgram_task.cancel()
            try:
                await pipeline._deepgram_task
            except asyncio.CancelledError:
                pass
