"""A slow Deepgram connection gets one more try before the caller is dropped."""

from __future__ import annotations

import os

for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "TELEGRAM_BOT_TOKEN", "USER_PHONE"):
    os.environ.setdefault(key, "test-placeholder")

from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402

from app.services import voice_pipeline as vp  # noqa: E402


def _pipeline(results):
    pipeline = object.__new__(vp.VoicePipeline)
    pipeline._connect_deepgram = AsyncMock(side_effect=results)
    return pipeline


@pytest.mark.asyncio
async def test_first_success_connects_once():
    pipeline = _pipeline([True])

    assert await pipeline._connect_deepgram_with_retry() is True

    assert pipeline._connect_deepgram.await_count == 1


@pytest.mark.asyncio
async def test_failed_connection_is_retried():
    pipeline = _pipeline([False, True])

    assert await pipeline._connect_deepgram_with_retry() is True

    assert pipeline._connect_deepgram.await_count == 2


@pytest.mark.asyncio
async def test_gives_up_after_the_last_attempt():
    pipeline = _pipeline([False] * vp.DEEPGRAM_CONNECT_ATTEMPTS)

    assert await pipeline._connect_deepgram_with_retry() is False

    assert pipeline._connect_deepgram.await_count == vp.DEEPGRAM_CONNECT_ATTEMPTS


def test_total_wait_stays_under_the_old_single_ten_second_timeout():
    assert vp.DEEPGRAM_CONNECT_ATTEMPTS * vp.DEEPGRAM_OPEN_TIMEOUT_SECONDS < 10
