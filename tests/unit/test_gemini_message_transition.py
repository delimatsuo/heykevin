"""Unit tests for Gemini Live message taking transition and boundary invariants."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from unittest.mock import AsyncMock, Mock

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest00000000000000000000000000")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550101")
os.environ.setdefault("GEMINI_API_KEY", "test-api-key")

from app.services.gemini_pipeline import GeminiPipeline
from app.services.message_taking import compute_grace_delay


class FakeWebSocket:
    """Async iterator and message capture mock for Gemini Live WebSocket."""

    def __init__(self):
        self._inbound: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent_messages: list[dict] = []
        self.closed = False

    async def send(self, data: str):
        if self.closed:
            raise ConnectionError("WebSocket is closed")
        self.sent_messages.append(json.loads(data))

    async def put_server_message(self, message: dict):
        await self._inbound.put(json.dumps(message))

    async def close(self):
        self.closed = True
        await self._inbound.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed and self._inbound.empty():
            raise StopAsyncIteration
        item = await self._inbound.get()
        if item is None:
            raise StopAsyncIteration
        return item


@pytest.fixture
def fake_ws():
    return FakeWebSocket()


@pytest.fixture(autouse=True)
def isolate_background_tasks(monkeypatch):
    monkeypatch.setattr(GeminiPipeline, "_trigger_screening_summary_push", AsyncMock(return_value=None))
    monkeypatch.setattr(GeminiPipeline, "_unavailable_timer", AsyncMock(return_value=None))



def create_pipeline(
    fake_ws: FakeWebSocket,
    *,
    contractor_config: dict | None = None,
    on_audio_out=None,
    on_transcript=None,
    on_clear_audio=None,
    on_response_first_media_sent=None,
    on_response_end_media_sent=None,
    on_call_complete=None,
    pace_audio_output: bool = False,
) -> GeminiPipeline:
    cfg = contractor_config or {
        "owner_name": "Bob",
        "contractor_id": "test_contractor",
        "user_language": "en",
    }
    audio_out = on_audio_out or AsyncMock(return_value=None)
    transcript_cb = on_transcript or AsyncMock(return_value=None)
    clear_audio = on_clear_audio or AsyncMock(return_value=True)

    pipeline = GeminiPipeline(
        on_audio_out=audio_out,
        on_transcript=transcript_cb,
        on_clear_audio=clear_audio,
        on_response_first_media_sent=on_response_first_media_sent,
        on_response_end_media_sent=on_response_end_media_sent,
        on_call_complete=on_call_complete,
        call_sid="CAtest12345678",
        contractor_config=cfg,
        caller_phone="+15551234567",
    )
    pipeline.PACE_AUDIO_OUTPUT = pace_audio_output
    pipeline._ws = fake_ws
    pipeline._connected = True
    return pipeline


async def cancel_and_wait(*tasks: asyncio.Task | None):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_unsounded_old_response_suppressed_without_false_kevin_transcript(fake_ws, monkeypatch):
    transcript_mock = AsyncMock()
    pipeline = create_pipeline(fake_ws, on_transcript=transcript_mock)
    monkeypatch.setattr(pipeline, "_ensure_audio_playout_task", lambda: None)
    receive_task = asyncio.create_task(pipeline._receive_loop())

    try:
        audio_bytes = b"\x00" * 480
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        await fake_ws.put_server_message({
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": audio_b64}},
                    ]
                },
                "outputTranscription": {"text": "Let me check if Bob is available right now."},
            }
        })
        for _ in range(5):
            if pipeline._model_turn_generating and not pipeline._audio_queue.empty():
                break
            await asyncio.sleep(0.005)

        assert pipeline._model_turn_generating is True
        assert not pipeline._audio_queue.empty()
        assert pipeline._is_speaking is False

        prepare_task = asyncio.create_task(pipeline._prepare_message_delivery({
            "action": "decline",
            "accepted_at": time.monotonic(),
        }))
        await asyncio.sleep(0.01)

        assert pipeline._current_turn_suppressed is True
        assert pipeline._audio_queue.empty()

        await fake_ws.put_server_message({
            "serverContent": {
                "turnComplete": True,
            }
        })
        prepared = await asyncio.wait_for(prepare_task, timeout=1.0)
        assert prepared is True

        assert transcript_mock.await_count == 0
        assert not pipeline._transcript_lines
        assert pipeline._hold_offered is False

        delivered = await pipeline._deliver_message_instruction()
        assert delivered is True
        assert pipeline._unavailable_said is True

        assert len(fake_ws.sent_messages) == 1
        sent_text = fake_ws.sent_messages[0]["client_content"]["turns"][0]["parts"][0]["text"]
        assert "Bob is not available and offer to take a message" in sent_text
        assert "Do not offer to check availability" in sent_text
    finally:
        await fake_ws.close()
        await pipeline.stop()
        await cancel_and_wait(receive_task)


@pytest.mark.asyncio
async def test_already_playing_speech_completes_generation_and_playout_before_instruction(fake_ws):
    audio_delivered: list[bytes] = []
    first_media_event = asyncio.Event()

    async def mock_audio_out(chunk: bytes):
        audio_delivered.append(chunk)
        first_media_event.set()

    pipeline = create_pipeline(fake_ws, on_audio_out=mock_audio_out, pace_audio_output=False)
    receive_task = asyncio.create_task(pipeline._receive_loop())

    try:
        audio_bytes = b"\x00" * 480
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        await fake_ws.put_server_message({
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": audio_b64}},
                    ]
                },
                "outputTranscription": {"text": "Let me see if Bob is available."},
            }
        })

        await asyncio.wait_for(first_media_event.wait(), timeout=1.0)
        assert pipeline._has_speech_started_for_current_turn() is True
        assert len(audio_delivered) == 1

        await fake_ws.put_server_message({
            "serverContent": {
                "turnComplete": True,
            }
        })
        for _ in range(5):
            if not pipeline._model_turn_generating:
                break
            await asyncio.sleep(0.005)

        assert pipeline._hold_offered is True

        prepared = await pipeline._prepare_message_delivery({
            "action": "decline",
            "accepted_at": time.monotonic() - 5.0,
        })
        assert prepared is True

        delivered = await pipeline._deliver_message_instruction()
        assert delivered is True
        assert pipeline._unavailable_said is True

        sent_text = fake_ws.sent_messages[0]["client_content"]["turns"][0]["parts"][0]["text"]
        assert "unfortunately Bob is not available and ask if you can take a message" in sent_text
    finally:
        await fake_ws.close()
        await pipeline.stop()
        await cancel_and_wait(receive_task)


@pytest.mark.asyncio
async def test_second_oldchunk_dropped_after_suppression(fake_ws):
    audio_delivered: list[bytes] = []

    async def mock_audio_out(chunk: bytes):
        audio_delivered.append(chunk)

    pipeline = create_pipeline(fake_ws, on_audio_out=mock_audio_out, pace_audio_output=False)
    pipeline._assistant_instruction_pending = True
    pipeline._model_turn_generating = True
    pipeline._turn_complete_event.clear()

    prepare_task = asyncio.create_task(pipeline._prepare_message_delivery({
        "action": "decline",
        "accepted_at": time.monotonic(),
    }))
    await asyncio.sleep(0.01)
    assert pipeline._current_turn_suppressed is True

    receive_task = asyncio.create_task(pipeline._receive_loop())

    try:
        audio_bytes = b"\x00" * 480
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        await fake_ws.put_server_message({
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": audio_b64}},
                    ]
                },
                "outputTranscription": {"text": "I will check for you."},
            }
        })
        await asyncio.sleep(0.01)

        await fake_ws.put_server_message({
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": audio_b64}},
                    ]
                },
                "outputTranscription": {"text": "Please hold."},
            }
        })
        await asyncio.sleep(0.01)

        assert pipeline._audio_queue.empty()
        assert len(audio_delivered) == 0

        await fake_ws.put_server_message({
            "serverContent": {
                "turnComplete": True,
            }
        })
        prepared = await asyncio.wait_for(prepare_task, timeout=1.0)
        assert prepared is True
        assert len(audio_delivered) == 0
        assert not pipeline._transcript_lines

        delivered = await pipeline._deliver_message_instruction()
        assert delivered is True
    finally:
        await fake_ws.close()
        await pipeline.stop()
        await cancel_and_wait(receive_task)


@pytest.mark.asyncio
async def test_streamed_gap_after_first_media_preserves_started_flag_and_waits_for_playout(fake_ws):
    audio_delivered: list[bytes] = []
    first_chunk_done = asyncio.Event()

    async def mock_audio_out(chunk: bytes):
        audio_delivered.append(chunk)
        if len(audio_delivered) == 1:
            first_chunk_done.set()

    pipeline = create_pipeline(fake_ws, on_audio_out=mock_audio_out, pace_audio_output=False)
    receive_task = asyncio.create_task(pipeline._receive_loop())

    try:
        audio_bytes = b"\x00" * 480
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        await fake_ws.put_server_message({
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": audio_b64}},
                    ]
                },
            }
        })
        await asyncio.wait_for(first_chunk_done.wait(), timeout=1.0)
        await asyncio.sleep(0.01)

        assert pipeline._audio_queue.empty()
        assert pipeline._model_turn_generating is True
        assert pipeline._has_speech_started_for_current_turn() is True

        prepare_task = asyncio.create_task(pipeline._prepare_message_delivery({
            "action": "decline",
            "accepted_at": time.monotonic() - 5.0,
        }))
        await asyncio.sleep(0.01)

        assert pipeline._current_turn_suppressed is False

        await fake_ws.put_server_message({
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": audio_b64}},
                    ]
                },
                "outputTranscription": {"text": "Bob is checking his schedule."},
            }
        })
        await fake_ws.put_server_message({
            "serverContent": {
                "turnComplete": True,
            }
        })

        prepared = await asyncio.wait_for(prepare_task, timeout=1.0)
        assert prepared is True
        assert len(audio_delivered) == 2

        delivered = await pipeline._deliver_message_instruction()
        assert delivered is True
    finally:
        await fake_ws.close()
        await pipeline.stop()
        await cancel_and_wait(receive_task)


@pytest.mark.asyncio
async def test_decline_before_first_audio_suppresses_turn(fake_ws):
    audio_delivered: list[bytes] = []

    async def mock_audio_out(chunk: bytes):
        audio_delivered.append(chunk)

    pipeline = create_pipeline(fake_ws, on_audio_out=mock_audio_out, pace_audio_output=False)
    pipeline._assistant_instruction_pending = True
    pipeline._model_turn_generating = True
    pipeline._turn_complete_event.clear()

    prepare_task = asyncio.create_task(pipeline._prepare_message_delivery({
        "action": "decline",
        "accepted_at": time.monotonic(),
    }))
    await asyncio.sleep(0.01)
    assert pipeline._current_turn_suppressed is True

    pipeline._model_turn_generating = False
    pipeline._assistant_instruction_pending = False
    pipeline._turn_complete_event.set()

    prepared = await asyncio.wait_for(prepare_task, timeout=1.0)
    assert prepared is True
    assert len(audio_delivered) == 0

    delivered = await pipeline._deliver_message_instruction()
    assert delivered is True
    await pipeline.stop()


@pytest.mark.asyncio
async def test_reconnect_resets_suppression_and_turn_metrics_for_new_turn(fake_ws, monkeypatch):
    audio_delivered: list[bytes] = []

    async def mock_audio_out(chunk: bytes):
        audio_delivered.append(chunk)

    pipeline = create_pipeline(fake_ws, on_audio_out=mock_audio_out, pace_audio_output=False)

    pipeline._current_turn_suppressed = True
    pipeline._model_turn_generating = True
    pipeline._assistant_instruction_pending = True
    pipeline._message_response_allowed = True
    pipeline._turn_complete_event.clear()

    new_ws = FakeWebSocket()
    async def mock_start(**kwargs):
        pipeline._ws = new_ws
        pipeline._connected = True
        return True

    monkeypatch.setattr(pipeline, "start", mock_start)
    monkeypatch.setattr(pipeline, "_flush_reconnect_audio", AsyncMock(return_value=True))

    await pipeline._recover_receive_loop(close_websocket=False)

    assert pipeline._current_turn_suppressed is False
    assert pipeline._model_turn_generating is False
    assert pipeline._assistant_instruction_pending is False
    assert pipeline._message_response_allowed is False
    assert pipeline._turn_complete_event.is_set() is True

    receive_task = asyncio.create_task(pipeline._receive_loop())

    try:
        audio_bytes = b"\x00" * 480
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        await new_ws.put_server_message({
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": audio_b64}},
                    ]
                },
                "outputTranscription": {"text": "How can I help you today?"},
            }
        })
        await new_ws.put_server_message({
            "serverContent": {
                "turnComplete": True,
            }
        })

        for _ in range(10):
            if len(audio_delivered) > 0 and len(pipeline._transcript_lines) > 0:
                break
            await asyncio.sleep(0.005)

        assert len(audio_delivered) == 1
        assert "Kevin: How can I help you today?" in pipeline._transcript_lines
    finally:
        await new_ws.close()
        await pipeline.stop()
        await cancel_and_wait(receive_task)


@pytest.mark.asyncio
async def test_fast_ws_response_not_suppressed_while_message_taking_pending():
    audio_delivered: list[bytes] = []

    async def mock_audio_out(chunk: bytes):
        audio_delivered.append(chunk)

    received_response = asyncio.Event()

    class FastRespondingWebSocket(FakeWebSocket):
        def __init__(self, target_pipeline):
            super().__init__()
            self.target_pipeline = target_pipeline

        async def send(self, data: str):
            await super().send(data)
            audio_bytes = b"\x00" * 480
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
            await self.put_server_message({
                "serverContent": {
                    "modelTurn": {
                        "parts": [
                            {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": audio_b64}},
                        ]
                    },
                    "outputTranscription": {"text": "Unfortunately Bob is not available. Can I take a message?"},
                }
            })
            await self.put_server_message({
                "serverContent": {
                    "turnComplete": True,
                }
            })
            for _ in range(10):
                if len(self.target_pipeline._transcript_lines) > 0:
                    break
                await asyncio.sleep(0.005)
            received_response.set()

    fast_ws = FastRespondingWebSocket(target_pipeline=None)
    pipeline = create_pipeline(fast_ws, on_audio_out=mock_audio_out, pace_audio_output=False)
    fast_ws.target_pipeline = pipeline

    receive_task = asyncio.create_task(pipeline._receive_loop())

    try:
        pipeline._message_taking_pending = True

        delivered = await pipeline._deliver_message_instruction()
        assert delivered is True
        assert pipeline._unavailable_said is True
        assert pipeline._message_response_allowed is False

        await asyncio.wait_for(received_response.wait(), timeout=1.0)

        assert len(audio_delivered) == 1
        assert "Kevin: Unfortunately Bob is not available. Can I take a message?" in pipeline._transcript_lines
    finally:
        await fast_ws.close()
        await pipeline.stop()
        await cancel_and_wait(receive_task)


@pytest.mark.asyncio
async def test_prepare_timeout_preserves_old_turn_suppression_after_pending_clears(fake_ws, monkeypatch):
    audio_delivered: list[bytes] = []

    async def mock_audio_out(chunk: bytes):
        audio_delivered.append(chunk)

    pipeline = create_pipeline(fake_ws, on_audio_out=mock_audio_out, pace_audio_output=False)
    pipeline._model_turn_generating = True
    pipeline._turn_complete_event.clear()

    async def short_wait_timeout(timeout: float = 15.0):
        return False

    monkeypatch.setattr(pipeline, "_wait_for_old_turn_complete", short_wait_timeout)

    prepared = await pipeline._prepare_message_delivery({
        "action": "decline",
        "accepted_at": time.monotonic(),
    })
    assert prepared is False
    assert pipeline._current_turn_suppressed is True

    pipeline._finish_message_delivery_attempt()
    assert pipeline._message_taking_pending is False
    assert pipeline._current_turn_suppressed is True

    receive_task = asyncio.create_task(pipeline._receive_loop())

    try:
        audio_bytes = b"\x00" * 480
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        await fake_ws.put_server_message({
            "serverContent": {
                "modelTurn": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": audio_b64}},
                    ]
                },
                "outputTranscription": {"text": "Late leaked old response"},
            }
        })
        await asyncio.sleep(0.01)

        assert pipeline._audio_queue.empty()
        assert len(audio_delivered) == 0
        assert not pipeline._transcript_lines

        await fake_ws.put_server_message({
            "serverContent": {
                "turnComplete": True,
            }
        })
        for _ in range(5):
            if not pipeline._current_turn_suppressed:
                break
            await asyncio.sleep(0.005)

        assert pipeline._current_turn_suppressed is False
    finally:
        await fake_ws.close()
        await pipeline.stop()
        await cancel_and_wait(receive_task)


@pytest.mark.asyncio
async def test_old_hold_ended_previously_applies_full_3s_grace_on_new_decline(fake_ws):
    pipeline = create_pipeline(fake_ws)

    t0 = 100.0
    pipeline._hold_offered = True
    pipeline._hold_offer_completed_at = t0 - 5.0
    pipeline._last_playout_drained_at = t0 - 5.0

    delay = compute_grace_delay(
        action="decline",
        hold_offered=True,
        intent_accepted_at=t0,
        speaking_finished_at=t0 - 5.0,
        now=t0,
    )
    assert delay == 3.0

    delay_elapsed = compute_grace_delay(
        action="decline",
        hold_offered=True,
        intent_accepted_at=t0 - 3.5,
        speaking_finished_at=t0 - 5.0,
        now=t0,
    )
    assert delay_elapsed == 0.0

    await pipeline.stop()


@pytest.mark.asyncio
async def test_timeout_action_has_no_extra_grace_delay(fake_ws):
    pipeline = create_pipeline(fake_ws)
    pipeline._hold_offered = True
    pipeline._last_playout_drained_at = 100.0

    delay = compute_grace_delay(
        action="timeout",
        hold_offered=True,
        intent_accepted_at=105.0,
        speaking_finished_at=100.0,
        now=105.0,
    )
    assert delay == 0.0

    prepared = await pipeline._prepare_message_delivery({
        "action": "timeout",
        "accepted_at": 105.0,
    })
    assert prepared is True
    await pipeline.stop()


@pytest.mark.asyncio
async def test_no_hold_action_has_no_extra_grace_delay(fake_ws):
    pipeline = create_pipeline(fake_ws)
    pipeline._hold_offered = False

    delay = compute_grace_delay(
        action="decline",
        hold_offered=False,
        intent_accepted_at=100.0,
        speaking_finished_at=95.0,
        now=100.0,
    )
    assert delay == 0.0

    prepared = await pipeline._prepare_message_delivery({
        "action": "decline",
        "accepted_at": 100.0,
    })
    assert prepared is True
    await pipeline.stop()


@pytest.mark.asyncio
async def test_bounded_incomplete_wait_returns_false_when_turn_still_busy(fake_ws):
    pipeline = create_pipeline(fake_ws)
    pipeline._model_turn_generating = True
    pipeline._is_speaking = True

    completed = await pipeline._wait_for_current_turn_and_playout(timeout=0.01)
    assert completed is False
    await pipeline.stop()


@pytest.mark.asyncio
async def test_pipeline_stopped_aborts_preparation_and_delivery(fake_ws):
    pipeline = create_pipeline(fake_ws)
    await pipeline.stop()

    prepared = await pipeline._prepare_message_delivery({
        "action": "decline",
        "accepted_at": time.monotonic(),
    })
    assert prepared is False

    delivered = await pipeline._deliver_message_instruction()
    assert delivered is False
    assert pipeline._unavailable_said is False


@pytest.mark.asyncio
async def test_delivery_guard_rejection_prevents_instruction_and_state_change(fake_ws):
    pipeline = create_pipeline(fake_ws)
    pipeline._message_delivery_guard = AsyncMock(return_value=False)

    delivered = await pipeline._deliver_message_instruction()
    assert delivered is False
    assert pipeline._unavailable_said is False
    assert len(fake_ws.sent_messages) == 0
    await pipeline.stop()


@pytest.mark.asyncio
async def test_send_client_instruction_closed_ws_fails_delivery():
    pipeline = create_pipeline(FakeWebSocket())
    pipeline._ws = None
    pipeline._connected = False

    sent = await pipeline._send_client_instruction("Hello")
    assert sent is False

    delivered = await pipeline._deliver_message_instruction()
    assert delivered is False
    assert pipeline._unavailable_said is False
    await pipeline.stop()


@pytest.mark.asyncio
async def test_caller_barge_in_clears_old_audio_and_preserves_caller_transcript(fake_ws):
    transcript_calls = []

    async def on_transcript(speaker, text):
        transcript_calls.append((speaker, text))

    pipeline = create_pipeline(fake_ws, on_transcript=on_transcript)
    receive_task = asyncio.create_task(pipeline._receive_loop())

    try:
        await fake_ws.put_server_message({
            "serverContent": {
                "interrupted": True,
                "inputTranscription": {"text": "Wait I have an emergency"},
            }
        })
        await asyncio.sleep(0.01)

        await fake_ws.put_server_message({
            "serverContent": {
                "modelTurn": {
                    "parts": [{"text": "Understood"}],
                }
            }
        })
        await asyncio.sleep(0.01)

        assert ("Caller", "Wait I have an emergency") in transcript_calls
        assert "Caller: Wait I have an emergency" in pipeline._transcript_lines
    finally:
        await fake_ws.close()
        await pipeline.stop()
        await cancel_and_wait(receive_task)


@pytest.mark.asyncio
async def test_caller_language_preserved_in_gemini_instruction(fake_ws):
    pipeline = create_pipeline(
        fake_ws,
        contractor_config={"owner_name": "Carlos", "user_language": "en"},
    )
    pipeline._caller_language = "es"
    delivered = await pipeline._deliver_message_instruction()
    assert delivered is True
    assert len(fake_ws.sent_messages) == 1
    sent_text = fake_ws.sent_messages[0]["client_content"]["turns"][0]["parts"][0]["text"]
    assert "Carlos" in sent_text
    assert "Respond in the caller's language (es)." in sent_text
    await pipeline.stop()


@pytest.mark.asyncio
async def test_owner_availability_wait_skipped_when_message_taking_pending(fake_ws):
    pipeline = create_pipeline(fake_ws)
    try:
        pipeline._message_taking_pending = True
        pipeline._start_owner_availability_wait()
        assert pipeline._unavailable_task is None
        assert pipeline._summary_task is None
        assert pipeline._waiting_for_owner_availability is False
    finally:
        await pipeline.stop()


@pytest.mark.asyncio
async def test_finish_message_delivery_attempt_hook(fake_ws):
    pipeline = create_pipeline(fake_ws)
    pipeline._message_taking_pending = True
    pipeline._message_response_allowed = True
    pipeline._current_turn_suppressed = True
    pipeline._finish_message_delivery_attempt()
    assert pipeline._message_taking_pending is False
    assert pipeline._message_response_allowed is False
    assert pipeline._current_turn_suppressed is True
    await pipeline.stop()


@pytest.mark.asyncio
async def test_post_transition_silence_resumes_and_no_new_hold_promise(fake_ws):
    pipeline = create_pipeline(fake_ws)
    pipeline._message_taking_pending = True

    assert pipeline._waiting_on_caller() is False

    pipeline._finish_message_delivery_attempt()
    assert pipeline._message_taking_pending is False

    pipeline._unavailable_said = True
    pipeline._start_owner_availability_wait()
    assert pipeline._waiting_for_owner_availability is False

    pipeline._last_kevin_speech_time = time.time()
    pipeline._last_caller_speech_time = pipeline._last_kevin_speech_time - 5.0
    pipeline._is_speaking = False
    pipeline._assistant_instruction_pending = False
    assert pipeline._waiting_on_caller() is True
    await pipeline.stop()


@pytest.mark.asyncio
async def test_caller_speech_during_grace_preserves_caller_transcript_and_suppresses_new_turn(fake_ws):
    transcript_calls = []

    async def on_transcript(speaker, text):
        transcript_calls.append((speaker, text))

    audio_delivered = []

    async def on_audio_out(chunk: bytes):
        audio_delivered.append(chunk)

    pipeline = create_pipeline(
        fake_ws,
        on_transcript=on_transcript,
        on_audio_out=on_audio_out,
        pace_audio_output=False,
    )
    receive_task = asyncio.create_task(pipeline._receive_loop())

    try:
        pipeline._message_taking_pending = True

        audio_bytes = b"\x00" * 480
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        await fake_ws.put_server_message({
            "serverContent": {
                "inputTranscription": {"text": "Are you still there?"},
                "modelTurn": {
                    "parts": [
                        {"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": audio_b64}},
                    ]
                },
                "outputTranscription": {"text": "Yes I am here."},
            }
        })
        await asyncio.sleep(0.01)

        assert pipeline._current_turn_suppressed is True
        assert len(audio_delivered) == 0

        await fake_ws.put_server_message({
            "serverContent": {
                "turnComplete": True,
            }
        })
        await asyncio.sleep(0.01)

        if pipeline._caller_transcript_buf:
            await pipeline._flush_caller_transcript()

        assert ("Caller", "Are you still there?") in transcript_calls

        delivered = await pipeline._deliver_message_instruction()
        assert delivered is True
        pipeline._finish_message_delivery_attempt()
        assert pipeline._message_taking_pending is False
        assert pipeline._current_turn_suppressed is False
    finally:
        await fake_ws.close()
        await pipeline.stop()
        await cancel_and_wait(receive_task)


@pytest.mark.asyncio
async def test_send_client_instruction_send_exception_propagates_and_clears_pending(fake_ws, monkeypatch):
    pipeline = create_pipeline(fake_ws)
    monkeypatch.setattr(fake_ws, "send", AsyncMock(side_effect=RuntimeError("ws send failed")))

    with pytest.raises(RuntimeError, match="ws send failed"):
        await pipeline._send_client_instruction("Test instruction")

    assert pipeline._assistant_instruction_pending is False
    await pipeline.stop()


@pytest.mark.asyncio
async def test_deliver_message_instruction_swallows_send_exception_and_resets_state(fake_ws, monkeypatch):
    pipeline = create_pipeline(fake_ws)
    pipeline._message_taking_pending = True
    monkeypatch.setattr(fake_ws, "send", AsyncMock(side_effect=RuntimeError("ws send failed")))

    delivered = await pipeline._deliver_message_instruction()
    assert delivered is False
    assert pipeline._unavailable_said is False
    assert pipeline._message_response_allowed is False
    assert len(fake_ws.sent_messages) == 0
    await pipeline.stop()


@pytest.mark.asyncio
async def test_hangup_for_caller_silence_propagates_send_exception_without_completing(fake_ws, monkeypatch):
    on_complete = AsyncMock()
    pipeline = create_pipeline(fake_ws, on_call_complete=on_complete)
    monkeypatch.setattr(pipeline, "_waiting_on_caller", lambda: True)
    monkeypatch.setattr(fake_ws, "send", AsyncMock(side_effect=RuntimeError("ws send failed")))

    with pytest.raises(RuntimeError, match="ws send failed"):
        await pipeline._hangup_for_caller_silence()

    on_complete.assert_not_awaited()
    await pipeline.stop()


@pytest.mark.asyncio
async def test_send_live_intake_text_send_exception_logs_error_not_success(fake_ws, monkeypatch):
    pipeline = create_pipeline(fake_ws)
    log_mock = Mock()
    pipeline._log_voice_timing = log_mock
    monkeypatch.setattr(fake_ws, "send", AsyncMock(side_effect=RuntimeError("ws send failed")))

    await pipeline._send_live_intake_text("Test intake instruction")

    logged_events = [call.args[0] for call in log_mock.call_args_list]
    assert "intake_instruction_error" in logged_events
    assert "intake_instruction" not in logged_events
    await pipeline.stop()


@pytest.mark.asyncio
async def test_prompt_for_caller_silence_send_exception_propagates_without_success_log(fake_ws, monkeypatch):
    pipeline = create_pipeline(fake_ws)
    pipeline._caller_silence_prompted_at = None
    monkeypatch.setattr(pipeline, "_waiting_on_caller", lambda: True)
    log_mock = Mock()
    pipeline._log_voice_timing = log_mock
    monkeypatch.setattr(fake_ws, "send", AsyncMock(side_effect=RuntimeError("ws send failed")))

    with pytest.raises(RuntimeError, match="ws send failed"):
        await pipeline._prompt_for_caller_silence()

    logged_events = [call.args[0] for call in log_mock.call_args_list]
    assert "silence_prompt_injected" not in logged_events
    await pipeline.stop()
