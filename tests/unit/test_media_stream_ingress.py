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
    _TwilioMediaIngress,
    _TwilioPlayoutTracker,
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


class _ControlledIngressWebSocket:
    def __init__(self):
        self.messages: asyncio.Queue[str | None] = asyncio.Queue()

    async def iter_text(self):
        while (message := await self.messages.get()) is not None:
            yield message

    async def close(self, code: int = 1000):
        return None


class _OutboundWebSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, message: dict):
        self.sent.append(message)


@pytest.mark.asyncio
async def test_twilio_ingress_buffers_audio_in_order_under_a_byte_bound():
    first = b"first caller frame"
    second = b"second caller frame"
    received_times = iter((100.25, 100.50))
    websocket = _IngressWebSocket(
        [_media_message(first), _media_message(second), json.dumps({"event": "stop"})]
    )
    ingress = _TwilioMediaIngress(
        websocket,
        call_sid="CA_test",
        clock=lambda: next(received_times),
    )

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
    assert first_event.received_at == 100.25
    assert second_event.received_at == 100.50
    assert stop_event.kind == "stop"
    assert closed_event is None
    assert ingress.buffered_audio_bytes == 0
    assert ingress.buffered_audio_chunks == 0


@pytest.mark.asyncio
async def test_twilio_ingress_dispatches_playout_marks_without_queueing_them():
    acknowledged: list[tuple[str, float]] = []
    received_times = iter((100.25,))
    websocket = _IngressWebSocket(
        [
            json.dumps({"event": "mark", "mark": {"name": "kv-a-1"}}),
            json.dumps({"event": "stop"}),
        ]
    )
    ingress = _TwilioMediaIngress(
        websocket,
        call_sid="CA_test",
        clock=lambda: next(received_times),
        on_mark=lambda name, received_at: acknowledged.append(
            (name, received_at)
        ),
    )

    await ingress.run()

    assert acknowledged == [("kv-a-1", 100.25)]
    assert (await ingress.receive()).kind == "stop"
    assert await ingress.receive() is None
    assert ingress.buffered_audio_chunks == 0


@pytest.mark.asyncio
async def test_twilio_playout_tracker_sends_audio_then_mark_and_records_ack(
    caplog,
):
    private_audio = b"private generated speech"
    times = iter((10.0, 10.125))
    websocket = _OutboundWebSocket()
    tracker = _TwilioPlayoutTracker(
        call_sid="CA_private_identifier",
        clock=lambda: next(times),
    )
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    sent = await tracker.send_audio(
        websocket,
        stream_sid="MZ_test",
        mulaw_chunk=private_audio,
    )

    assert sent is True
    assert [message["event"] for message in websocket.sent] == ["media", "mark"]
    mark_name = websocket.sent[1]["mark"]["name"]
    assert mark_name == "kv-a-1"
    assert tracker.pending_count == 1

    tracker.acknowledge(mark_name)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert tracker.pending_count == 0
    assert "voice_timing event=twilio_playout_ack" in messages
    assert "kind=audio" in messages
    assert "outcome=played" in messages
    assert "ack_ms=125" in messages
    assert private_audio.decode("ascii") not in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_twilio_playout_tracker_marks_pending_audio_at_clear_boundary(
    caplog,
):
    times = iter((20.0, 20.1, 20.15, 20.2))
    websocket = _OutboundWebSocket()
    tracker = _TwilioPlayoutTracker(
        call_sid="CA_private_identifier",
        clock=lambda: next(times),
    )
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    assert await tracker.send_audio(
        websocket,
        stream_sid="MZ_test",
        mulaw_chunk=b"generated speech",
    )
    assert await tracker.send_clear(websocket, stream_sid="MZ_test")

    assert [message["event"] for message in websocket.sent] == [
        "media",
        "mark",
        "clear",
        "mark",
    ]
    audio_mark = websocket.sent[1]["mark"]["name"]
    clear_mark = websocket.sent[3]["mark"]["name"]
    assert (audio_mark, clear_mark) == ("kv-a-1", "kv-c-2")

    tracker.acknowledge(audio_mark)
    tracker.acknowledge(clear_mark)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert tracker.pending_count == 0
    assert "kind=audio outcome=clear_requested" in messages
    assert "kind=clear outcome=confirmed" in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_twilio_clear_classifies_ack_that_arrives_during_clear_send(caplog):
    class ImmediateClearAckWebSocket(_OutboundWebSocket):
        tracker: _TwilioPlayoutTracker
        audio_mark: str

        async def send_json(self, message: dict):
            await super().send_json(message)
            if message["event"] == "clear":
                self.tracker.acknowledge(self.audio_mark, received_at=40.05)

    times = iter((40.0, 40.1))
    websocket = ImmediateClearAckWebSocket()
    tracker = _TwilioPlayoutTracker(
        call_sid="CA_private_identifier",
        clock=lambda: next(times),
    )
    websocket.tracker = tracker
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    assert await tracker.send_audio(
        websocket,
        stream_sid="MZ_test",
        mulaw_chunk=b"generated speech",
    )
    websocket.audio_mark = websocket.sent[1]["mark"]["name"]
    assert await tracker.send_clear(websocket, stream_sid="MZ_test")

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "kind=audio outcome=clear_requested" in messages
    assert "kind=audio outcome=played" not in messages


@pytest.mark.asyncio
async def test_twilio_clear_tolerates_marker_ack_before_send_returns(caplog):
    class ImmediateMarkAckWebSocket(_OutboundWebSocket):
        tracker: _TwilioPlayoutTracker

        async def send_json(self, message: dict):
            await super().send_json(message)
            if message["event"] == "mark":
                self.tracker.acknowledge(
                    message["mark"]["name"],
                    received_at=50.05,
                )

    websocket = ImmediateMarkAckWebSocket()
    tracker = _TwilioPlayoutTracker(
        call_sid="CA_private_identifier",
        clock=lambda: 50.0,
    )
    websocket.tracker = tracker
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")

    sent = await tracker.send_clear(websocket, stream_sid="MZ_test")

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert sent is True
    assert tracker.pending_count == 0
    assert "event=twilio_clear_mark_sent" in messages
    assert "kind=clear outcome=confirmed" in messages


@pytest.mark.asyncio
async def test_twilio_playout_tracker_bounds_unacknowledged_marks(caplog):
    times = iter((30.0, 30.1, 30.2))
    websocket = _OutboundWebSocket()
    tracker = _TwilioPlayoutTracker(
        call_sid="CA_private_identifier",
        max_pending_marks=2,
        clock=lambda: next(times),
    )
    caplog.set_level(logging.WARNING, logger="app.webhooks.media_stream")

    for _ in range(3):
        assert await tracker.send_audio(
            websocket,
            stream_sid="MZ_test",
            mulaw_chunk=b"generated speech",
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert tracker.pending_count == 2
    assert "voice_timing event=twilio_mark_evicted" in messages
    assert "CA_private_identifier" not in messages


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
            self.received_at: list[float] = []
            self.stopped = False

        async def start(self):
            self.ready.set()
            await self.audio_received.wait()
            return True

        async def wait_until_audio_ready(self):
            await self.ready.wait()
            return True

        async def process_audio_in(
            self,
            audio: bytes,
            *,
            received_at: float | None = None,
        ):
            self.processed.append(audio)
            self.received_at.append(received_at or 0.0)
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
    assert len(pipeline.received_at) == 1
    assert pipeline.received_at[0] > 0
    assert stops == [True]


@pytest.mark.asyncio
async def test_ingress_logs_one_audio_gap_and_resume_without_payload(caplog):
    private_frame = b"private gap calibration audio"
    websocket = _ControlledIngressWebSocket()
    ingress = _TwilioMediaIngress(websocket, call_sid="CA_private_identifier")

    class ReadyPipeline:
        def __init__(self):
            self.processed: list[bytes] = []
            self.processed_event = asyncio.Event()

        async def wait_until_audio_ready(self):
            return True

        async def process_audio_in(
            self,
            audio: bytes,
            *,
            received_at: float | None = None,
        ):
            self.processed.append(audio)
            self.processed_event.set()

    pipeline = ReadyPipeline()
    caplog.set_level(logging.INFO, logger="app.webhooks.media_stream")
    ingress_task = asyncio.create_task(ingress.run())
    consume_task = asyncio.create_task(
        _consume_twilio_ingress(
            pipeline,
            ingress,
            call_sid="CA_private_identifier",
            media_stream_started_at=asyncio.get_running_loop().time(),
            call_started_at=time.time(),
            max_call_duration_seconds=60,
            on_stream_stop=_noop,
            on_max_duration=_noop,
            audio_stream_gap_seconds=0.02,
        )
    )

    await websocket.messages.put(_media_message(private_frame))
    await asyncio.wait_for(pipeline.processed_event.wait(), timeout=1)
    for _ in range(50):
        if any(
            "event=inbound_audio_stream_gap" in record.getMessage()
            for record in caplog.records
        ):
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("audio gap was not observed")

    pipeline.processed_event.clear()
    await websocket.messages.put(_media_message(private_frame))
    await asyncio.wait_for(pipeline.processed_event.wait(), timeout=1)
    await websocket.messages.put(json.dumps({"event": "stop"}))

    assert await asyncio.wait_for(consume_task, timeout=1) == "stop"
    await asyncio.wait_for(ingress_task, timeout=1)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert pipeline.processed == [private_frame, private_frame]
    assert messages.count("voice_timing event=inbound_audio_stream_gap") == 1
    assert messages.count("voice_timing event=inbound_audio_stream_resumed") == 1
    assert "gap=1" in messages
    assert private_frame.decode("ascii") not in messages
    assert "CA_private_identifier" not in messages


@pytest.mark.asyncio
async def test_ingress_max_duration_fires_without_more_media():
    ingress = _TwilioMediaIngress(
        _ControlledIngressWebSocket(),
        call_sid="CA_test",
    )
    max_duration_fired = asyncio.Event()

    class ReadyPipeline:
        async def wait_until_audio_ready(self):
            return True

    async def on_max_duration():
        max_duration_fired.set()

    result = await asyncio.wait_for(
        _consume_twilio_ingress(
            ReadyPipeline(),
            ingress,
            call_sid="CA_test",
            media_stream_started_at=asyncio.get_running_loop().time(),
            call_started_at=0.0,
            max_call_duration_seconds=1,
            on_stream_stop=_noop,
            on_max_duration=on_max_duration,
            audio_stream_gap_seconds=0.01,
        ),
        timeout=1,
    )

    assert result == "max_duration"
    assert max_duration_fired.is_set()


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

        async def process_audio_in(
            self,
            _audio: bytes,
            *,
            received_at: float | None = None,
        ):
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

        async def process_audio_in(
            self,
            _audio: bytes,
            *,
            received_at: float | None = None,
        ):
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
