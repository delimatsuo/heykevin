"""Shared Twilio WebSocket transport, playback mark tracking, and live transcript writer."""

import asyncio
import base64
import binascii
from dataclasses import dataclass
import json
import threading
import time
from types import SimpleNamespace
from typing import Callable

from fastapi import WebSocket

from app.config import settings
from app.db.cache import get_active_call, update_live_transcript
from app.services.voice_events import _call_label as _call_label
from app.utils.logging import get_logger

# Keep the original logger name so existing transport filters and tests receive events.
logger = get_logger("app.webhooks.media_stream")


def _log_safe_exception(event: str, error: BaseException, call_sid: str = "") -> None:
    logger.warning(
        "media_event event=%s call=%s exception_type=%s",
        event,
        _call_label(call_sid),
        type(error).__name__,
    )


def _safe_urgent_push_body(caller_name: str = "", caller_phone: str = "") -> str:
    """Return lock-screen-safe urgent call copy with no raw speech or full phone."""
    return "Urgent call needs review. Open Kevin for details."


async def _send_twilio_audio(
    websocket: WebSocket,
    *,
    stream_sid: str,
    mulaw_chunk: bytes,
    call_sid: str,
) -> bool:
    """Send one audio chunk and report whether Twilio accepted the frame."""
    if not stream_sid:
        logger.warning(
            "media_event event=twilio_audio_send_skipped call=%s reason=missing_stream",
            _call_label(call_sid),
        )
        return False
    try:
        payload_b64 = base64.b64encode(mulaw_chunk).decode("utf-8")
        await websocket.send_json({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload_b64},
        })
    except Exception as error:
        _log_safe_exception("twilio_audio_send_error", error, call_sid)
        return False
    return True


async def _send_twilio_clear(
    websocket: WebSocket,
    *,
    stream_sid: str,
    call_sid: str,
) -> bool:
    """Send a clear frame and report whether Twilio accepted it."""
    if not stream_sid:
        logger.warning(
            "media_event event=twilio_audio_clear_skipped call=%s reason=missing_stream",
            _call_label(call_sid),
        )
        return False
    try:
        await websocket.send_json({
            "event": "clear",
            "streamSid": stream_sid,
        })
    except Exception as error:
        _log_safe_exception("twilio_audio_clear_error", error, call_sid)
        return False
    logger.info(
        "media_event event=twilio_audio_cleared call=%s",
        _call_label(call_sid),
    )
    return True


async def _send_twilio_playback_mark(
    websocket: WebSocket,
    *,
    stream_sid: str,
    playback_marks: "_TwilioPlaybackMarks",
    turn: int,
    phase: str = "first_media",
    call_sid: str,
) -> bool:
    """Request a Twilio transport receipt, not caller-heard latency evidence."""
    if not stream_sid:
        return False
    name = playback_marks.reserve(turn=turn, phase=phase)
    if not name:
        return False
    try:
        await websocket.send_json({
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {"name": name},
        })
    except Exception as error:
        playback_marks.discard(name)
        _log_safe_exception("twilio_playback_mark_error", error, call_sid)
        return False
    return True


async def _finish_max_call_duration(on_call_complete) -> None:
    """Play a provider-independent limit message, then hang up the call."""
    from twilio.twiml.voice_response import VoiceResponse

    response = VoiceResponse()
    response.say(
        "This call has reached the maximum duration. Please call back to continue."
    )
    response.hangup()
    await on_call_complete(twiml=str(response))


async def _complete_twilio_call(
    *,
    call_sid: str,
    websocket: WebSocket,
    twiml: str | None = None,
) -> bool:
    """End the call through Twilio REST, closing the stream as a fallback."""
    try:
        from twilio.rest import Client

        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: client.calls(call_sid).update(
                twiml=twiml or "<Response><Hangup/></Response>"
            ),
        )
    except Exception as error:
        _log_safe_exception("call_hangup_error", error, call_sid)
        try:
            await websocket.close(code=1000)
        except Exception as close_error:
            _log_safe_exception("call_stream_close_error", close_error, call_sid)
        else:
            logger.info(
                "media_event event=call_stream_close_fallback call=%s",
                _call_label(call_sid),
            )
        return False

    logger.info("media_event event=call_hung_up call=%s", _call_label(call_sid))
    return True


def _log_task_exception(task: asyncio.Task):
    if task.cancelled():
        return
    error = task.exception()
    if error:
        _log_safe_exception("background_task_error", error)


WS_MAX_MESSAGE_SIZE = 65_536
MAX_MEDIA_INGRESS_AUDIO_BYTES = 96_000  # 12 seconds of 8 kHz mulaw
MAX_MEDIA_INGRESS_AUDIO_CHUNKS = 600
MAX_PENDING_TWILIO_PLAYBACK_MARKS = 32


@dataclass(frozen=True, slots=True)
class _TwilioIngressEvent:
    kind: str
    audio: bytes = b""
    received_at: float = 0.0


@dataclass(slots=True)
class _PendingPlaybackMark:
    turn: int
    sent_at: float
    phase: str = "first_media"
    cleared: bool = False


class _TwilioPlaybackMarks:
    """Bounded, payload-free Twilio transport receipts for played or cleared media."""

    def __init__(self, *, call_sid: str, max_pending: int = MAX_PENDING_TWILIO_PLAYBACK_MARKS):
        self._call_sid = call_sid
        self._max_pending = max_pending
        self._pending: dict[str, _PendingPlaybackMark] = {}
        self._sequence = 0

    def reserve(self, *, turn: int, phase: str = "first_media") -> str | None:
        self._sequence += 1
        name = f"response-{turn}-{self._sequence}"
        return name if self.register(turn=turn, name=name, phase=phase) else None

    def register(self, *, turn: int, name: str, phase: str = "first_media") -> bool:
        if len(self._pending) >= self._max_pending:
            logger.warning(
                "voice_timing event=twilio_playback_mark_skipped call=%s "
                "reason=pending_limit pending=%s limit=%s",
                _call_label(self._call_sid),
                len(self._pending),
                self._max_pending,
            )
            return False
        self._pending[name] = _PendingPlaybackMark(
            turn=turn,
            sent_at=time.monotonic(),
            phase=phase,
        )
        return True

    def discard(self, name: str) -> None:
        self._pending.pop(name, None)

    def mark_pending_cleared(self) -> None:
        for pending in self._pending.values():
            pending.cleared = True

    def resolve(self, name: str) -> bool:
        pending = self._pending.pop(name, None)
        if pending is None:
            return False
        logger.info(
            "voice_timing event=twilio_playback_mark_resolved call=%s "
            "turn=%s phase=%s status=%s media_to_mark_ms=%s",
            _call_label(self._call_sid),
            pending.turn,
            pending.phase,
            "cleared" if pending.cleared else "played",
            max(0, round((time.monotonic() - pending.sent_at) * 1_000)),
        )
        return True


class _TwilioMediaIngress:
    """Read Twilio media continuously into a bounded, ordered queue."""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        call_sid: str,
        max_buffered_audio_bytes: int = MAX_MEDIA_INGRESS_AUDIO_BYTES,
        max_buffered_audio_chunks: int = MAX_MEDIA_INGRESS_AUDIO_CHUNKS,
        on_playback_mark: Callable[[str], object] | None = None,
    ) -> None:
        self._websocket = websocket
        self._call_sid = call_sid
        self._max_buffered_audio_bytes = max_buffered_audio_bytes
        self._max_buffered_audio_chunks = max_buffered_audio_chunks
        self._on_playback_mark = on_playback_mark
        # Two reserved slots guarantee stop and close signals cannot be starved.
        self._queue: asyncio.Queue[_TwilioIngressEvent | None] = asyncio.Queue(
            maxsize=max_buffered_audio_chunks + 2
        )
        self.buffered_audio_bytes = 0
        self.buffered_audio_chunks = 0
        self.high_water_audio_bytes = 0
        self.delivery_lag_samples = 0
        self.max_delivery_lag_ms = 0
        self.total_delivery_lag_ms = 0
        self.overflowed = False
        self.stop_received = False
        self.ended = False

    async def _close(self, code: int) -> None:
        try:
            await self._websocket.close(code=code)
        except Exception as error:
            _log_safe_exception("ingress_close_error", error, self._call_sid)

    async def run(self) -> None:
        """Read until Twilio stops, disconnects, or violates a bound."""
        try:
            async for message in self._websocket.iter_text():
                if len(message) > WS_MAX_MESSAGE_SIZE:
                    logger.warning(
                        "voice_timing event=inbound_media_message_oversize "
                        "call=%s message_bytes=%s limit_bytes=%s",
                        _call_label(self._call_sid),
                        len(message),
                        WS_MAX_MESSAGE_SIZE,
                    )
                    await self._close(1009)
                    break

                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning(
                        "voice_timing event=inbound_media_invalid_json call=%s",
                        _call_label(self._call_sid),
                    )
                    await self._close(1003)
                    break
                if not isinstance(data, dict):
                    logger.warning(
                        "voice_timing event=inbound_media_invalid_json call=%s",
                        _call_label(self._call_sid),
                    )
                    await self._close(1003)
                    break

                kind = data.get("event", "")
                if kind == "mark":
                    mark = data.get("mark")
                    name = mark.get("name") if isinstance(mark, dict) else ""
                    if isinstance(name, str) and name and self._on_playback_mark:
                        try:
                            self._on_playback_mark(name)
                        except Exception as error:
                            _log_safe_exception("playback_mark_handler_error", error, self._call_sid)
                    continue
                if kind == "stop":
                    self.stop_received = True
                    self._queue.put_nowait(_TwilioIngressEvent(kind="stop"))
                    break
                if kind != "media":
                    continue

                media = data.get("media", {})
                if not isinstance(media, dict):
                    logger.warning(
                        "voice_timing event=inbound_media_invalid_audio call=%s",
                        _call_label(self._call_sid),
                    )
                    await self._close(1003)
                    break
                payload = media.get("payload", "")
                if not payload:
                    continue
                try:
                    audio = base64.b64decode(payload, validate=True)
                except (binascii.Error, ValueError, TypeError):
                    logger.warning(
                        "voice_timing event=inbound_media_invalid_audio call=%s",
                        _call_label(self._call_sid),
                    )
                    await self._close(1003)
                    break
                if not audio:
                    continue

                attempted_bytes = self.buffered_audio_bytes + len(audio)
                attempted_chunks = self.buffered_audio_chunks + 1
                if (
                    attempted_bytes > self._max_buffered_audio_bytes
                    or attempted_chunks > self._max_buffered_audio_chunks
                ):
                    self.overflowed = True
                    logger.warning(
                        "voice_timing event=inbound_media_buffer_overflow "
                        "call=%s attempted_audio_ms=%s limit_audio_ms=%s",
                        _call_label(self._call_sid),
                        round(attempted_bytes / 8),
                        round(self._max_buffered_audio_bytes / 8),
                    )
                    await self._close(1009)
                    break

                self._queue.put_nowait(
                    _TwilioIngressEvent(
                        kind="media",
                        audio=audio,
                        received_at=time.monotonic(),
                    )
                )
                self.buffered_audio_bytes = attempted_bytes
                self.buffered_audio_chunks = attempted_chunks
                self.high_water_audio_bytes = max(
                    self.high_water_audio_bytes,
                    self.buffered_audio_bytes,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_safe_exception("inbound_media_receive_error", error, self._call_sid)
        finally:
            self.ended = True
            self._queue.put_nowait(None)

    async def receive(self) -> _TwilioIngressEvent | None:
        event = await self._queue.get()
        if event and event.kind == "media":
            self.buffered_audio_bytes -= len(event.audio)
            self.buffered_audio_chunks -= 1
            if event.received_at > 0:
                self.delivery_lag_samples += 1
                self.max_delivery_lag_ms = max(
                    self.max_delivery_lag_ms,
                    max(0, round((time.monotonic() - event.received_at) * 1000)),
                )
                self.total_delivery_lag_ms += max(
                    0,
                    round((time.monotonic() - event.received_at) * 1000),
                )
        return event


async def _cancel_task(task: asyncio.Task | None) -> None:
    if not task:
        return
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _consume_twilio_ingress(
    pipeline,
    ingress: _TwilioMediaIngress,
    *,
    call_sid: str,
    media_stream_started_at: float,
    call_started_at: float,
    max_call_duration_seconds: int,
    on_stream_stop,
    on_max_duration,
) -> str:
    try:
        ready = await pipeline.wait_until_audio_ready()
        if not ready:
            return "pipeline_unavailable"

        elapsed_ms = (
            max(0, round((time.monotonic() - media_stream_started_at) * 1000))
            if media_stream_started_at > 0
            else 0
        )
        logger.info(
            "voice_timing event=inbound_media_ready call=%s "
            "call_elapsed_ms=%s buffered_audio_ms=%s buffered_chunks=%s",
            _call_label(call_sid),
            elapsed_ms,
            round(ingress.buffered_audio_bytes / 8),
            ingress.buffered_audio_chunks,
        )

        while True:
            if ingress.overflowed:
                return "overflow"
            event = await ingress.receive()
            if ingress.overflowed:
                return "overflow"
            if event is None:
                return "closed"
            if time.time() - call_started_at > max_call_duration_seconds:
                await on_max_duration()
                return "max_duration"
            if event.kind == "media":
                await pipeline.process_audio_in(event.audio)
            elif event.kind == "stop":
                await on_stream_stop()
                return "stop"
    finally:
        logger.info(
            "voice_timing event=inbound_media_delivery_summary call=%s "
            "samples=%s mean_delivery_lag_ms=%s max_delivery_lag_ms=%s "
            "max_queue_audio_ms=%s",
            _call_label(call_sid),
            ingress.delivery_lag_samples,
            round(ingress.total_delivery_lag_ms / ingress.delivery_lag_samples)
            if ingress.delivery_lag_samples
            else 0,
            ingress.max_delivery_lag_ms,
            round(ingress.high_water_audio_bytes / 8),
        )


async def _serve_pipeline_ingress(
    pipeline,
    ingress: _TwilioMediaIngress,
    *,
    call_sid: str,
    media_stream_started_at: float,
    call_started_at: float,
    max_call_duration_seconds: int,
    on_stream_stop,
    on_max_duration,
) -> bool:
    """Start the pipeline while forwarding media as soon as it is ready."""
    start_task = asyncio.create_task(pipeline.start())
    consume_task = asyncio.create_task(
        _consume_twilio_ingress(
            pipeline,
            ingress,
            call_sid=call_sid,
            media_stream_started_at=media_stream_started_at,
            call_started_at=call_started_at,
            max_call_duration_seconds=max_call_duration_seconds,
            on_stream_stop=on_stream_stop,
            on_max_duration=on_max_duration,
        )
    )
    try:
        done, _pending = await asyncio.wait(
            {start_task, consume_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if start_task in done:
            started = bool(start_task.result())
            if not started:
                return False
            await consume_task
            return True

        outcome = consume_task.result()
        if outcome == "pipeline_unavailable":
            return bool(await start_task)

        await _cancel_task(start_task)
        await pipeline.stop()
        return True
    finally:
        await _cancel_task(start_task)
        await _cancel_task(consume_task)


TRANSCRIPT_THROTTLE = 1.0


class LiveTranscriptPusher:
    """One owned writer, coalescing snapshots while a write or throttle is pending."""

    _compat_module = None

    def __init__(
        self, call_sid: str, lines: list, throttle: float = TRANSCRIPT_THROTTLE,
        *, contractor_id: str, ws_token: str,
    ):
        self._call_sid = call_sid
        self._lines = lines
        self._contractor_id = contractor_id
        self._ws_token = ws_token
        self._throttle = throttle
        self._last_push: float | None = None
        self._pending_snapshot: tuple[str, float] | None = None
        self._worker: asyncio.Task | None = None
        self._closed = False
        self._closing = self._resolve_asyncio().Event()
        self._cancelled = threading.Event()

    def _resolve_time(self):
        if self._compat_module is not None:
            return getattr(self._compat_module, "time", time)
        return time

    def _resolve_asyncio(self):
        if self._compat_module is not None:
            return getattr(self._compat_module, "asyncio", asyncio)
        return asyncio

    def _resolve_update_live_transcript(self):
        if self._compat_module is not None:
            return getattr(
                self._compat_module,
                "update_live_transcript",
                update_live_transcript,
            )
        return update_live_transcript

    def push(self) -> None:
        if self._closed:
            return
        time_mod = self._resolve_time()
        asyncio_mod = self._resolve_asyncio()
        self._pending_snapshot = ("\n".join(self._lines), time_mod.time())
        if self._worker is None or self._worker.done():
            self._worker = asyncio_mod.create_task(self._run())
            self._worker.add_done_callback(_log_task_exception)

    async def _run(self) -> None:
        while self._pending_snapshot is not None and not self._cancelled.is_set():
            if self._last_push is not None and not self._closed:
                time_mod = self._resolve_time()
                remaining = self._throttle - (time_mod.monotonic() - self._last_push)
                if remaining > 0:
                    try:
                        await self._resolve_asyncio().wait_for(
                            self._closing.wait(), timeout=remaining
                        )
                    except TimeoutError:
                        pass
            text, observed_at = self._pending_snapshot
            self._pending_snapshot = None
            self._last_push = self._resolve_time().monotonic()
            write_fn = self._resolve_update_live_transcript()
            await write_fn(
                self._call_sid, text,
                contractor_id=self._contractor_id, ws_token=self._ws_token,
                observed_at=observed_at,
                cancelled=self._cancelled,
            )

    async def close(self, timeout: float = 1.0) -> None:
        """Seal input and flush the last snapshot within a bounded drain window.

        A timed-out executor may finish its current conditional request later;
        the cancellation flag suppresses retries, not an already-sent commit.
        """
        self._closed = True
        self._closing.set()  # closing bypasses the trailing throttle
        task = self._worker
        if task is None or task.done():
            return
        asyncio_mod = self._resolve_asyncio()
        try:
            await asyncio_mod.wait({task}, timeout=max(0.0, timeout))
        finally:
            if not task.done():
                self._cancelled.set()
                self._pending_snapshot = None
                task.cancel()
                # Yield for cooperative cancellation without extending the bound
                # to include an executor request that Python cannot recall.
                await asyncio_mod.wait({task}, timeout=0)


def _active_call_fallback(call_sid: str, call_data: dict | None):
    """Build minimal call context from authenticated RTDB data."""
    if not isinstance(call_data, dict):
        return None

    contractor_id = call_data.get("contractor_id", "")
    caller_phone = call_data.get("caller_phone", "")
    if not contractor_id or not caller_phone:
        return None

    return SimpleNamespace(
        call_sid=call_sid,
        contractor_id=contractor_id,
        caller_phone=caller_phone,
        caller_name=call_data.get("caller_name", ""),
        accepted=call_data.get("accepted") is True,
    )


async def _resolve_active_call(
    call_sid: str,
    call_data: dict | None,
    *,
    get_active_call_fn=None,
):
    """Load active call state without delaying an authenticated stream."""
    lookup = get_active_call if get_active_call_fn is None else get_active_call_fn
    active_call = await lookup(call_sid)
    if active_call:
        return active_call

    active_call = _active_call_fallback(call_sid, call_data)
    if active_call:
        logger.warning(
            "media_event event=active_call_fallback call=%s",
            _call_label(call_sid),
        )
    return active_call
