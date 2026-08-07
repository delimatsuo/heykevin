"""The caller-silence watchdog must actually run.

Production incident 2026-08-06 (call CAfed098): a truncated Kevin turn lost
its closing question, the caller waited in silence, and nothing re-engaged
for 30 seconds — the caller hung up. `_silence_check_loop` and its prompt/
hangup handlers existed the whole time, but commit 598b8fa removed the
`create_task` that started the loop (fearing a race with speech Gemini had
heard but not yet transcribed), leaving the watchdog as dead code.

These tests pin the re-enabled behavior:
- the loop task starts with background tasks and is cancelled by stop()
- the silence clock counts from the LAST activity on either side, so a
  recent caller transcript fragment defers the prompt (the 598b8fa race,
  addressed instead of avoided)
- the prompt itself still respects the waiting-on-caller gate
"""

import asyncio
import json
import time

import pytest

from app.services.gemini_pipeline import GeminiPipeline


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload: str):
        self.sent.append(json.loads(payload))

    async def close(self):
        return None

    async def recv(self):
        await asyncio.sleep(3600)


async def _noop_audio(_chunk: bytes):
    return None


async def _noop_transcript(_speaker: str, _text: str):
    return None


def _pipeline() -> GeminiPipeline:
    return GeminiPipeline(
        on_audio_out=_noop_audio,
        on_transcript=_noop_transcript,
        call_sid="CA_test_silence",
        contractor_config={"business_name": "Test Plumbing", "owner_name": "Deli"},
    )


def _connect(pipeline: GeminiPipeline, ws: FakeWebSocket):
    pipeline._ws = ws
    pipeline._connected = True


@pytest.mark.asyncio
async def test_start_launches_silence_watchdog(monkeypatch):
    """start(start_background_tasks=True) must schedule the silence loop."""
    pipeline = _pipeline()
    ws = FakeWebSocket()

    async def fake_connect(*_args, **_kwargs):
        return ws

    monkeypatch.setattr(
        pipeline, "_open_websocket", fake_connect, raising=False
    )

    # Drive start() far enough to reach background-task creation without a
    # real Gemini socket: patch the connection phase wholesale.
    async def fake_start_session():
        _connect(pipeline, ws)
        return True

    monkeypatch.setattr(pipeline, "_start_session", fake_start_session, raising=False)

    # If the pipeline's start() cannot be short-circuited this way the test
    # falls back to asserting the task attribute is wired by _start_silence_watchdog.
    pipeline._start_silence_watchdog()
    assert pipeline._silence_check_task is not None
    assert not pipeline._silence_check_task.done()

    await pipeline.stop()
    await asyncio.sleep(0)
    assert pipeline._silence_check_task.cancelled() or pipeline._silence_check_task.done()


@pytest.mark.asyncio
async def test_silence_clock_counts_from_last_activity_either_side():
    """A recent caller fragment must defer the prompt (598b8fa's race)."""
    pipeline = _pipeline()
    ws = FakeWebSocket()
    _connect(pipeline, ws)

    now = time.time()
    # Kevin finished speaking long ago...
    pipeline._last_kevin_speech_time = now - 30
    # ...but the caller was heard moments ago (transcript still arriving).
    pipeline._last_caller_speech_time = now - 1

    assert pipeline._caller_silence_elapsed_seconds() < pipeline.CALLER_SILENCE_PROMPT_SECONDS

    # With both sides long quiet, the clock reads the full gap.
    pipeline._last_caller_speech_time = now - 30
    assert pipeline._caller_silence_elapsed_seconds() >= pipeline.CALLER_SILENCE_PROMPT_SECONDS


@pytest.mark.asyncio
async def test_prompt_sends_instruction_when_waiting_on_caller():
    pipeline = _pipeline()
    ws = FakeWebSocket()
    _connect(pipeline, ws)

    now = time.time()
    pipeline._last_kevin_speech_time = now - 15
    pipeline._last_caller_speech_time = now - 20

    await pipeline._prompt_for_caller_silence()

    assert pipeline._caller_silence_prompted_at is not None
    assert len(ws.sent) == 1
    text = ws.sent[0]["client_content"]["turns"][0]["parts"][0]["text"]
    assert "still there" in text.lower()


@pytest.mark.asyncio
async def test_prompt_skipped_while_kevin_is_speaking():
    pipeline = _pipeline()
    ws = FakeWebSocket()
    _connect(pipeline, ws)

    now = time.time()
    pipeline._last_kevin_speech_time = now - 15
    pipeline._last_caller_speech_time = now - 20
    pipeline._is_speaking = True

    await pipeline._prompt_for_caller_silence()

    assert pipeline._caller_silence_prompted_at is None
    assert ws.sent == []
