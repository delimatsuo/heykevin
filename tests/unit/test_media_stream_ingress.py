"""Bounded, ordered Twilio media ingress during voice pipeline startup."""

import asyncio
import base64
import json
import logging
import os
import time

import pytest

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "test-twilio-number")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "test-user-number")

from app.services.gemini_pipeline import GeminiPipeline
from app.services.voice_pipeline import VoicePipeline
from app.webhooks.media_stream import (
    _clear_twilio_audio_with_playback_marks,
    _send_twilio_playback_mark,
    _TwilioPlaybackMarks,
    _TwilioMediaIngress,
    _consume_twilio_ingress,
    _serve_pipeline_ingress,
)


def _media_message(audio: bytes) -> str:
    return json.dumps(
        {
            "event": "media",
            "media": {"payload": base64.b64encode(audio).decode("ascii")},
        }
    )


class _IngressWebSocket:
    def __init__(self, messages: list[str]):
        self._messages = messages
        self.close_codes: list[int] = []

    async def iter_text(self):
        for message in self._messages:
            yield message

    async def close(self, code: int = 1000):
        self.close_codes.append(code)


@pytest.mark.asyncio
async def test_twilio_ingress_forwards_playback_mark_events_without_queueing_them():
    received_marks = []
    websocket = _IngressWebSocket([
        json.dumps({"event": "mark", "mark": {"name": "response-1-1"}}),
        json.dumps({"event": "stop"}),
    ])
    ingress = _TwilioMediaIngress(
        websocket,
        call_sid="CA_test",
        on_playback_mark=received_marks.append,
    )

    await ingress.run()

    assert received_marks == ["response-1-1"]
    assert (await ingress.receive()).kind == "stop"
    assert await ingress.receive() is None


def test_twilio_playback_marks_are_bounded_and_classify_clear_without_names(caplog):
    private_mark_name = "response-7-private-marker"
    marks = _TwilioPlaybackMarks(
        call_sid="CA_private_identifier",
        max_pending=1,
    )
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    assert marks.register(
        turn=7,
        epoch=7,
        phase="response_end",
        name=private_mark_name,
    )
    assert marks.register(
        turn=8,
        epoch=8,
        phase="response_end",
        name="response-8-overflow",
    ) is False
    marks.mark_pending_cleared()
    assert marks.resolve(private_mark_name)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_timing event=twilio_playback_mark_skipped" in messages
    assert "voice_timing event=twilio_playback_mark_resolved" in messages
    assert "turn=7" in messages
    assert "epoch=7" in messages
    assert "phase=response_end" in messages
    assert "status=cleared" in messages
    assert private_mark_name not in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_twilio_playback_mark_is_sent_after_audio_with_opaque_name(caplog):
    class RecordingWebSocket:
        def __init__(self):
            self.payloads = []

        async def send_json(self, payload):
            self.payloads.append(payload)

    websocket = RecordingWebSocket()
    marks = _TwilioPlaybackMarks(call_sid="CA_private_identifier")
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    assert await _send_twilio_playback_mark(
        websocket,
        stream_sid="stream-redacted",
        playback_marks=marks,
        turn=3,
        epoch=3,
        phase="response_end",
        call_sid="CA_private_identifier",
    )

    payload = websocket.payloads[0]
    assert payload["event"] == "mark"
    assert payload["mark"]["name"].startswith("playback-")
    assert marks.resolve(payload["mark"]["name"])

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "voice_timing event=twilio_playback_mark_resolved" in messages
    assert "turn=3" in messages
    assert "epoch=3" in messages
    assert "phase=response_end" in messages
    assert "status=played" in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_twilio_playback_marks_classify_timeout_stale_and_duplicate_without_names(
    caplog,
):
    marks = _TwilioPlaybackMarks(
        call_sid="CA_private_identifier",
        timeout_seconds=0.01,
    )
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    stale_name = marks.reserve(turn=4, epoch=4, phase="response_end")
    current_name = marks.reserve(turn=5, epoch=5, phase="response_end")
    assert stale_name is not None
    assert current_name is not None
    assert marks.mark_sent(stale_name)
    assert marks.mark_sent(current_name)
    assert marks.resolve(stale_name)
    assert marks.resolve(stale_name) is False

    await asyncio.sleep(0.02)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "turn=4 epoch=4 phase=response_end status=stale" in messages
    assert "reason=unknown_or_duplicate" in messages
    assert "turn=5 epoch=5 phase=response_end status=timeout" in messages
    assert stale_name not in messages
    assert current_name not in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_twilio_clear_invalidates_mark_before_awaited_send_can_resolve_it(caplog):
    marks = _TwilioPlaybackMarks(call_sid="CA_private_identifier")
    private_name = marks.reserve(turn=10, epoch=10, phase="response_end")
    assert private_name is not None
    assert marks.mark_sent(private_name)
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    class RacingWebSocket:
        async def send_json(self, payload):
            assert payload["event"] == "clear"
            assert marks.resolve(private_name)

    assert await _clear_twilio_audio_with_playback_marks(
        RacingWebSocket(),
        stream_sid="stream-redacted",
        playback_marks=marks,
        call_sid="CA_private_identifier",
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "turn=10 epoch=10 phase=response_end status=stale" in messages
    assert "reason=clear_requested" in messages
    assert "status=played" not in messages
    assert private_name not in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_failed_twilio_clear_keeps_pending_mark_conservatively_stale(caplog):
    marks = _TwilioPlaybackMarks(call_sid="CA_private_identifier")
    private_name = marks.reserve(turn=11, epoch=11, phase="response_end")
    assert private_name is not None
    assert marks.mark_sent(private_name)
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    class FailingWebSocket:
        async def send_json(self, _payload):
            raise RuntimeError("private clear failure")

    assert await _clear_twilio_audio_with_playback_marks(
        FailingWebSocket(),
        stream_sid="stream-redacted",
        playback_marks=marks,
        call_sid="CA_private_identifier",
    ) is False
    assert marks.resolve(private_name)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "turn=11 epoch=11 phase=response_end status=stale" in messages
    assert "reason=clear_requested" in messages
    assert "status=played" not in messages
    assert "private clear failure" not in messages
    assert private_name not in messages
    assert "CA_private_identifier" not in messages


def test_twilio_playback_marks_close_invalidates_pending_receipts(caplog):
    marks = _TwilioPlaybackMarks(call_sid="CA_private_identifier")
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    private_name = marks.reserve(turn=9, epoch=9, phase="response_end")
    assert private_name is not None
    marks.close()
    assert marks.resolve(private_name) is False

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "turn=9 epoch=9 phase=response_end status=stale" in messages
    assert "reason=stream_closed" in messages
    assert private_name not in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_twilio_playback_mark_send_failure_discards_timeout_and_error_payload(
    caplog,
):
    private_error = "playback mark failed with private caller payload"

    class FailingWebSocket:
        async def send_json(self, _payload):
            raise RuntimeError(private_error)

    marks = _TwilioPlaybackMarks(
        call_sid="CA_private_identifier",
        timeout_seconds=0.01,
    )
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    assert await _send_twilio_playback_mark(
        FailingWebSocket(),
        stream_sid="stream-redacted",
        playback_marks=marks,
        turn=6,
        epoch=6,
        phase="response_end",
        call_sid="CA_private_identifier",
    ) is False
    await asyncio.sleep(0.02)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "media_event event=twilio_playback_mark_error" in messages
    assert "status=timeout" not in messages
    assert private_error not in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_twilio_playback_mark_send_timeout_is_bounded_and_discards_receipt(
    monkeypatch,
    caplog,
):
    private_payload = "private never-returning mark payload"

    class HangingWebSocket:
        async def send_json(self, _payload):
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "app.webhooks.media_stream.TWILIO_PLAYBACK_MARK_SEND_TIMEOUT_SECONDS",
        0.01,
    )
    marks = _TwilioPlaybackMarks(
        call_sid="CA_private_identifier",
        timeout_seconds=0.01,
    )
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    started_at = time.monotonic()
    assert await _send_twilio_playback_mark(
        HangingWebSocket(),
        stream_sid=private_payload,
        playback_marks=marks,
        turn=12,
        epoch=12,
        phase="response_end",
        call_sid="CA_private_identifier",
    ) is False
    assert time.monotonic() - started_at < 0.2
    assert marks._pending == {}
    await asyncio.sleep(0.02)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "media_event event=twilio_playback_mark_send_timeout" in messages
    assert "status=timeout" not in messages
    assert private_payload not in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_twilio_playback_mark_send_cancellation_discards_receipt_and_propagates():
    send_started = asyncio.Event()

    class HangingWebSocket:
        async def send_json(self, _payload):
            send_started.set()
            await asyncio.Event().wait()

    marks = _TwilioPlaybackMarks(call_sid="CA_private_identifier")
    send_task = asyncio.create_task(_send_twilio_playback_mark(
        HangingWebSocket(),
        stream_sid="stream-redacted",
        playback_marks=marks,
        turn=13,
        epoch=13,
        phase="response_end",
        call_sid="CA_private_identifier",
    ))
    await asyncio.wait_for(send_started.wait(), timeout=1)

    send_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send_task

    assert marks._pending == {}


@pytest.mark.asyncio
async def test_twilio_ingress_buffers_audio_in_order_under_a_byte_bound():
    first = b"first caller frame"
    second = b"second caller frame"
    websocket = _IngressWebSocket(
        [_media_message(first), _media_message(second), json.dumps({"event": "stop"})]
    )
    ingress = _TwilioMediaIngress(websocket, call_sid="CA_test")

    await ingress.run()

    assert ingress.buffered_audio_bytes == len(first) + len(second)
    assert ingress.buffered_audio_chunks == 2
    assert ingress.high_water_audio_bytes == len(first) + len(second)

    first_event = await ingress.receive()
    second_event = await ingress.receive()
    stop_event = await ingress.receive()
    closed_event = await ingress.receive()

    assert (first_event.kind, first_event.audio) == ("media", first)
    assert (second_event.kind, second_event.audio) == ("media", second)
    assert stop_event.kind == "stop"
    assert closed_event is None
    assert ingress.buffered_audio_bytes == 0
    assert ingress.buffered_audio_chunks == 0
    assert ingress.delivery_lag_samples == 2
    assert ingress.max_delivery_lag_ms >= 0


@pytest.mark.asyncio
async def test_twilio_ingress_closes_on_overflow_without_logging_audio(caplog):
    private_frame = b"private caller audio"
    websocket = _IngressWebSocket(
        [_media_message(private_frame), _media_message(private_frame)]
    )
    ingress = _TwilioMediaIngress(
        websocket,
        call_sid="CA_private_identifier",
        max_buffered_audio_bytes=len(private_frame),
    )
    caplog.set_level(logging.WARNING, logger="app.webhooks.media_stream")

    await ingress.run()

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert ingress.overflowed is True
    assert websocket.close_codes == [1009]
    assert "voice_timing event=inbound_media_buffer_overflow" in messages
    assert private_frame.decode() not in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_ingress_delivery_summary_is_payload_free_on_stream_stop(caplog):
    private_frame = b"private caller audio"
    ingress = _TwilioMediaIngress(
        _IngressWebSocket([_media_message(private_frame), json.dumps({"event": "stop"})]),
        call_sid="CA_private_identifier",
    )
    await ingress.run()

    class ReadyPipeline:
        async def wait_until_audio_ready(self):
            return True

        async def process_audio_in(self, audio: bytes):
            assert audio == private_frame

    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")
    result = await _consume_twilio_ingress(
        ReadyPipeline(),
        ingress,
        call_sid="CA_private_identifier",
        media_stream_started_at=0.0,
        call_started_at=time.time(),
        max_call_duration_seconds=60,
        on_stream_stop=_noop,
        on_max_duration=_noop,
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert result == "stop"
    assert "voice_timing event=inbound_media_delivery_summary" in messages
    assert "samples=1" in messages
    assert private_frame.decode() not in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_ingress_delivery_summary_is_payload_free_when_pipeline_is_unavailable(
    caplog,
):
    private_frame = b"private caller audio"
    ingress = _TwilioMediaIngress(
        _IngressWebSocket([_media_message(private_frame), json.dumps({"event": "stop"})]),
        call_sid="CA_private_identifier",
    )
    await ingress.run()

    class UnavailablePipeline:
        async def wait_until_audio_ready(self):
            return False

        async def process_audio_in(self, _audio: bytes):
            raise AssertionError("audio must not be forwarded")

    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")
    result = await _consume_twilio_ingress(
        UnavailablePipeline(),
        ingress,
        call_sid="CA_private_identifier",
        media_stream_started_at=0.0,
        call_started_at=time.time(),
        max_call_duration_seconds=60,
        on_stream_stop=_noop,
        on_max_duration=_noop,
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert result == "pipeline_unavailable"
    assert "voice_timing event=inbound_media_delivery_summary" in messages
    assert "samples=0" in messages
    assert private_frame.decode() not in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_pipeline_start_and_ingress_consumption_run_concurrently():
    frame = b"caller spoke during startup"
    ingress = _TwilioMediaIngress(
        _IngressWebSocket([_media_message(frame), json.dumps({"event": "stop"})]),
        call_sid="CA_test",
    )
    await ingress.run()

    class StartupBlockedPipeline:
        def __init__(self):
            self.ready = asyncio.Event()
            self.audio_received = asyncio.Event()
            self.processed: list[bytes] = []
            self.stopped = False

        async def start(self):
            self.ready.set()
            await self.audio_received.wait()
            return True

        async def wait_until_audio_ready(self):
            await self.ready.wait()
            return True

        async def process_audio_in(self, audio: bytes):
            self.processed.append(audio)
            self.audio_received.set()

        async def stop(self):
            self.stopped = True

    pipeline = StartupBlockedPipeline()
    stops = []

    async def on_stream_stop():
        stops.append(True)

    async def on_max_duration():
        raise AssertionError("duration limit should not fire")

    started = await asyncio.wait_for(
        _serve_pipeline_ingress(
            pipeline,
            ingress,
            call_sid="CA_test",
            media_stream_started_at=0.0,
            call_started_at=0.0,
            max_call_duration_seconds=10**12,
            on_stream_stop=on_stream_stop,
            on_max_duration=on_max_duration,
        ),
        timeout=1,
    )

    assert started is True
    assert pipeline.processed == [frame]
    assert stops == [True]


@pytest.mark.asyncio
async def test_failed_pipeline_start_cancels_readiness_waiter():
    ingress = _TwilioMediaIngress(
        _IngressWebSocket([_media_message(b"buffered caller frame")]),
        call_sid="CA_test",
    )
    await ingress.run()

    class FailedPipeline:
        async def start(self):
            return False

        async def wait_until_audio_ready(self):
            await asyncio.Event().wait()

        async def process_audio_in(self, _audio: bytes):
            raise AssertionError("audio must not be forwarded")

        async def stop(self):
            return None

    started = await asyncio.wait_for(
        _serve_pipeline_ingress(
            FailedPipeline(),
            ingress,
            call_sid="CA_test",
            media_stream_started_at=0.0,
            call_started_at=0.0,
            max_call_duration_seconds=10**12,
            on_stream_stop=_noop,
            on_max_duration=_noop,
        ),
        timeout=1,
    )

    assert started is False


@pytest.mark.asyncio
async def test_stream_stop_cancels_blocked_greeting_startup():
    ingress = _TwilioMediaIngress(
        _IngressWebSocket([json.dumps({"event": "stop"})]),
        call_sid="CA_test",
    )
    await ingress.run()
    start_cancelled = asyncio.Event()
    stops = []

    class BlockedPipeline:
        def __init__(self):
            self.ready = asyncio.Event()
            self.stopped = False

        async def start(self):
            self.ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                start_cancelled.set()
                raise

        async def wait_until_audio_ready(self):
            await self.ready.wait()
            return True

        async def process_audio_in(self, _audio: bytes):
            raise AssertionError("no media should be forwarded")

        async def stop(self):
            self.stopped = True

    pipeline = BlockedPipeline()

    async def on_stream_stop():
        stops.append(True)

    started = await asyncio.wait_for(
        _serve_pipeline_ingress(
            pipeline,
            ingress,
            call_sid="CA_test",
            media_stream_started_at=0.0,
            call_started_at=0.0,
            max_call_duration_seconds=10**12,
            on_stream_stop=on_stream_stop,
            on_max_duration=_noop,
        ),
        timeout=1,
    )

    assert started is True
    assert stops == [True]
    assert pipeline.stopped is True
    assert start_cancelled.is_set()


async def _noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_gemini_audio_ready_waits_for_greeting_instruction(monkeypatch):
    class GeminiWebSocket:
        async def send(self, _payload):
            return None

        async def recv(self):
            return json.dumps({"setupComplete": {}})

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self):
            return None

    async def connect(*_args, **_kwargs):
        return GeminiWebSocket()

    monkeypatch.setattr("app.services.gemini_pipeline.websockets.connect", connect)
    pipeline = GeminiPipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        contractor_config={"effective_mode": "personal"},
    )
    greeting_entered = asyncio.Event()
    release_greeting = asyncio.Event()

    async def blocked_greeting():
        greeting_entered.set()
        await release_greeting.wait()

    monkeypatch.setattr(pipeline, "_send_greeting", blocked_greeting)
    start_task = asyncio.create_task(pipeline.start())
    await asyncio.wait_for(greeting_entered.wait(), timeout=1)
    ready_task = asyncio.create_task(pipeline.wait_until_audio_ready())
    await asyncio.sleep(0)

    assert ready_task.done() is False

    release_greeting.set()
    assert await asyncio.wait_for(start_task, timeout=1) is True
    assert await asyncio.wait_for(ready_task, timeout=1) is True
    await pipeline.stop()


@pytest.mark.asyncio
async def test_legacy_pipeline_accepts_audio_while_greeting_is_in_progress(monkeypatch):
    class DeepgramWebSocket:
        def __init__(self):
            self.sent: list[bytes | str] = []

        async def send(self, payload):
            self.sent.append(payload)

        async def close(self):
            return None

    websocket = DeepgramWebSocket()
    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        contractor_config={"effective_mode": "personal"},
    )

    async def connect_deepgram():
        pipeline._deepgram_ws = websocket
        return True

    greeting_started = asyncio.Event()
    release_greeting = asyncio.Event()

    async def blocked_speak(_text: str):
        pipeline._is_speaking = True
        greeting_started.set()
        await release_greeting.wait()
        pipeline._is_speaking = False

    monkeypatch.setattr(pipeline, "_connect_deepgram", connect_deepgram)
    monkeypatch.setattr(pipeline, "_speak", blocked_speak)

    start_task = asyncio.create_task(pipeline.start())
    assert await asyncio.wait_for(pipeline.wait_until_audio_ready(), timeout=1) is True
    await asyncio.wait_for(greeting_started.wait(), timeout=1)
    assert start_task.done() is False

    await pipeline.process_audio_in(b"early caller frame")
    assert websocket.sent == [b"early caller frame"]

    release_greeting.set()
    assert await asyncio.wait_for(start_task, timeout=1) is True
    await pipeline.stop()


@pytest.mark.asyncio
async def test_legacy_pipeline_does_not_discard_final_speech_during_greeting():
    transcript_message = json.dumps(
        {
            "is_final": True,
            "speech_final": False,
            "channel": {"alternatives": [{"transcript": "I need help"}]},
        }
    )

    class DeepgramWebSocket:
        def __init__(self):
            self.returned = False

        async def recv(self):
            if not self.returned:
                self.returned = True
                return transcript_message
            raise asyncio.CancelledError

    transcripts = []
    clears = []

    async def on_transcript(speaker: str, text: str):
        transcripts.append((speaker, text))

    async def on_clear():
        clears.append(True)
        return True

    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=on_transcript,
        on_clear_audio=on_clear,
        contractor_config={"effective_mode": "personal"},
    )
    pipeline._deepgram_ws = DeepgramWebSocket()
    pipeline._connected = True
    pipeline._greeting_done = False
    pipeline._is_speaking = True

    await pipeline._deepgram_receive_loop()

    assert transcripts == [("Caller", "I need help")]
    assert clears == [True]


@pytest.mark.asyncio
async def test_legacy_pipeline_clears_greeting_on_first_interim_speech():
    interim_message = json.dumps(
        {
            "is_final": False,
            "speech_final": False,
            "channel": {"alternatives": [{"transcript": "Hello"}]},
        }
    )

    class DeepgramWebSocket:
        def __init__(self):
            self.returned = False

        async def recv(self):
            if not self.returned:
                self.returned = True
                return interim_message
            raise asyncio.CancelledError

    transcripts = []
    clears = []

    async def on_transcript(speaker: str, text: str):
        transcripts.append((speaker, text))

    async def on_clear():
        clears.append(True)
        return True

    pipeline = VoicePipeline(
        on_audio_out=_noop,
        on_transcript=on_transcript,
        on_clear_audio=on_clear,
        contractor_config={"effective_mode": "personal"},
    )
    pipeline._deepgram_ws = DeepgramWebSocket()
    pipeline._connected = True
    pipeline._greeting_done = False
    pipeline._is_speaking = True

    await pipeline._deepgram_receive_loop()

    assert transcripts == []
    assert clears == [True]
    assert pipeline._interrupt_speaking is True
