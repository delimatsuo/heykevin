"""Twilio Media Streams WebSocket — bridges audio through Deepgram STT → Claude → ElevenLabs TTS."""

import asyncio
import base64
import binascii
from dataclasses import dataclass
import json
import time
from types import SimpleNamespace
from typing import Callable

from fastapi import APIRouter, WebSocket

from app.config import settings
from app.services.voice_pipeline import VoicePipeline
from app.db.cache import get_active_call, update_active_call, _init_firebase, ACTIVE_CALLS_PATH
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


def _call_label(call_sid: str) -> str:
    return call_sid[:8] or "unknown"


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


async def _clear_twilio_audio_with_playback_marks(
    websocket: WebSocket,
    *,
    stream_sid: str,
    playback_marks: "_TwilioPlaybackMarks",
    call_sid: str,
) -> bool:
    """Invalidate receipts before the awaited clear can race an inbound mark."""
    playback_marks.mark_pending_clear_requested()
    cleared = await _send_twilio_clear(
        websocket,
        stream_sid=stream_sid,
        call_sid=call_sid,
    )
    if cleared:
        playback_marks.mark_pending_cleared()
    return cleared


async def _send_twilio_playback_mark(
    websocket: WebSocket,
    *,
    stream_sid: str,
    playback_marks: "_TwilioPlaybackMarks",
    turn: int,
    epoch: int = 0,
    phase: str = "first_media",
    call_sid: str,
) -> bool:
    """Request a Twilio transport receipt, not caller-heard latency evidence."""
    if not stream_sid:
        logger.warning(
            "voice_timing event=twilio_playback_mark_skipped call=%s "
            "turn=%s epoch=%s phase=%s reason=missing_stream",
            _call_label(call_sid),
            turn,
            epoch,
            phase,
        )
        return False
    name = playback_marks.reserve(turn=turn, epoch=epoch, phase=phase)
    if not name:
        return False
    try:
        await asyncio.wait_for(
            websocket.send_json({
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": name},
            }),
            timeout=TWILIO_PLAYBACK_MARK_SEND_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        playback_marks.discard(name)
        raise
    except TimeoutError as error:
        playback_marks.discard(name)
        _log_safe_exception("twilio_playback_mark_send_timeout", error, call_sid)
        return False
    except Exception as error:
        playback_marks.discard(name)
        _log_safe_exception("twilio_playback_mark_error", error, call_sid)
        return False
    playback_marks.mark_sent(name)
    logger.info(
        "voice_timing event=twilio_playback_mark_sent call=%s "
        "turn=%s epoch=%s phase=%s",
        _call_label(call_sid),
        turn,
        epoch,
        phase,
    )
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
TWILIO_PLAYBACK_MARK_SEND_TIMEOUT_SECONDS = 1.0
TWILIO_PLAYBACK_MARK_TIMEOUT_SECONDS = 30.0
TWILIO_PLAYBACK_MARK_PHASES = frozenset({"first_media", "response_end"})


@dataclass(frozen=True, slots=True)
class _TwilioIngressEvent:
    kind: str
    audio: bytes = b""
    received_at: float = 0.0


@dataclass(slots=True)
class _PendingPlaybackMark:
    turn: int
    epoch: int
    phase: str
    sent_at: float
    clear_requested: bool = False
    cleared: bool = False
    stale: bool = False
    timeout_handle: asyncio.TimerHandle | None = None


class _TwilioPlaybackMarks:
    """Bounded, payload-free Twilio transport receipts for played or cleared media."""

    def __init__(
        self,
        *,
        call_sid: str,
        max_pending: int = MAX_PENDING_TWILIO_PLAYBACK_MARKS,
        timeout_seconds: float = TWILIO_PLAYBACK_MARK_TIMEOUT_SECONDS,
    ):
        self._call_sid = call_sid
        self._max_pending = max_pending
        self._timeout_seconds = timeout_seconds
        self._pending: dict[str, _PendingPlaybackMark] = {}
        self._latest_epoch_by_phase: dict[str, int] = {}
        self._sequence = 0

    def reserve(
        self,
        *,
        turn: int,
        epoch: int = 0,
        phase: str = "first_media",
    ) -> str | None:
        self._sequence += 1
        name = f"playback-{self._sequence}"
        return (
            name
            if self.register(turn=turn, epoch=epoch, phase=phase, name=name)
            else None
        )

    def register(
        self,
        *,
        turn: int,
        name: str,
        epoch: int = 0,
        phase: str = "first_media",
    ) -> bool:
        if phase not in TWILIO_PLAYBACK_MARK_PHASES:
            logger.warning(
                "voice_timing event=twilio_playback_mark_skipped call=%s "
                "turn=%s epoch=%s phase=unknown reason=invalid_phase",
                _call_label(self._call_sid),
                turn,
                epoch,
            )
            return False
        if len(self._pending) >= self._max_pending:
            logger.warning(
                "voice_timing event=twilio_playback_mark_skipped call=%s "
                "turn=%s epoch=%s phase=%s reason=pending_limit "
                "pending=%s limit=%s",
                _call_label(self._call_sid),
                turn,
                epoch,
                phase,
                len(self._pending),
                self._max_pending,
            )
            return False

        previous_epoch = self._latest_epoch_by_phase.get(phase, -1)
        if epoch > previous_epoch:
            for pending in self._pending.values():
                if pending.phase == phase and pending.epoch < epoch:
                    pending.stale = True
            self._latest_epoch_by_phase[phase] = epoch

        pending = _PendingPlaybackMark(
            turn=turn,
            epoch=epoch,
            phase=phase,
            sent_at=time.monotonic(),
            stale=epoch < previous_epoch,
        )
        self._pending[name] = pending
        return True

    def mark_sent(self, name: str) -> bool:
        """Start the receipt timeout only after the mark frame was accepted."""
        pending = self._pending.get(name)
        if pending is None:
            return False
        pending.sent_at = time.monotonic()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            pending.timeout_handle = loop.call_later(
                self._timeout_seconds,
                self._expire,
                name,
            )
        return True

    def discard(self, name: str) -> None:
        pending = self._pending.pop(name, None)
        if pending and pending.timeout_handle:
            pending.timeout_handle.cancel()

    def mark_pending_cleared(self) -> None:
        for pending in self._pending.values():
            pending.cleared = True

    def mark_pending_clear_requested(self) -> None:
        for pending in self._pending.values():
            pending.clear_requested = True

    def resolve(self, name: str) -> bool:
        pending = self._pending.pop(name, None)
        if pending is None:
            logger.info(
                "voice_timing event=twilio_playback_mark_ignored call=%s "
                "reason=unknown_or_duplicate",
                _call_label(self._call_sid),
            )
            return False
        if pending.timeout_handle:
            pending.timeout_handle.cancel()
        reason = "receipt"
        if pending.cleared:
            status = "cleared"
        elif pending.clear_requested:
            status = "stale"
            reason = "clear_requested"
        elif pending.stale:
            status = "stale"
        else:
            status = "played"
        self._log_resolved(pending, status=status, reason=reason)
        return True

    def _expire(self, name: str) -> None:
        pending = self._pending.pop(name, None)
        if pending is None:
            return
        self._log_resolved(pending, status="timeout")

    def close(self) -> None:
        """Invalidate unresolved receipts when the Twilio stream ends."""
        pending_marks = list(self._pending.values())
        self._pending.clear()
        for pending in pending_marks:
            if pending.timeout_handle:
                pending.timeout_handle.cancel()
            self._log_resolved(pending, status="stale", reason="stream_closed")

    def _log_resolved(
        self,
        pending: _PendingPlaybackMark,
        *,
        status: str,
        reason: str = "receipt",
    ) -> None:
        logger.info(
            "voice_timing event=twilio_playback_mark_resolved call=%s "
            "turn=%s epoch=%s phase=%s status=%s reason=%s media_to_mark_ms=%s",
            _call_label(self._call_sid),
            pending.turn,
            pending.epoch,
            pending.phase,
            status,
            reason,
            max(0, round((time.monotonic() - pending.sent_at) * 1_000)),
        )


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
    outcome = "unknown"
    try:
        ready = await pipeline.wait_until_audio_ready()
        if not ready:
            outcome = "pipeline_unavailable"
            return outcome

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
                outcome = "overflow"
                return outcome
            event = await ingress.receive()
            if ingress.overflowed:
                outcome = "overflow"
                return outcome
            if event is None:
                outcome = "closed"
                return outcome
            if time.time() - call_started_at > max_call_duration_seconds:
                await on_max_duration()
                outcome = "max_duration"
                return outcome
            if event.kind == "media":
                await pipeline.process_audio_in(event.audio)
            elif event.kind == "stop":
                await on_stream_stop()
                outcome = "stop"
                return outcome
    finally:
        engine = (
            "gemini"
            if type(pipeline).__name__ == "GeminiPipeline"
            else "elevenlabs"
            if type(pipeline).__name__ == "VoicePipeline"
            else "unknown"
        )
        logger.info(
            "voice_timing event=pipeline_ingress_ended call=%s "
            "engine=%s reason=%s call_elapsed_ms=%s",
            _call_label(call_sid),
            engine,
            outcome,
            (
                max(0, round((time.monotonic() - media_stream_started_at) * 1000))
                if media_stream_started_at > 0
                else 0
            ),
        )
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


async def _resolve_active_call(call_sid: str, call_data: dict | None):
    """Load active call state without delaying an authenticated stream."""
    active_call = await get_active_call(call_sid)
    if active_call:
        return active_call

    active_call = _active_call_fallback(call_sid, call_data)
    if active_call:
        logger.warning(
            "media_event event=active_call_fallback call=%s",
            _call_label(call_sid),
        )
    return active_call


@router.websocket("/media-stream/{call_sid}")
async def media_stream_ws(websocket: WebSocket, call_sid: str):
    """Bidirectional audio bridge: Twilio <-> Voice Pipeline (STT + Claude + TTS)."""

    # Accept the WebSocket first — Twilio sends custom parameters in the `start` message
    await websocket.accept()
    logger.info("media_event event=stream_connected call=%s", _call_label(call_sid))

    # Wait for the Twilio `start` event to get the ws_token from customParameters
    ws_token = ""
    start_stream_sid = ""
    media_stream_started_at = None
    call_started_at = time.time()
    try:
        # Read messages until we get the start event (should be the first message)
        for _ in range(5):
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=5)
            msg = json.loads(raw)
            if msg.get("event") == "start":
                media_stream_started_at = time.monotonic()
                call_started_at = time.time()
                ws_token = msg.get("start", {}).get("customParameters", {}).get("ws_token", "")
                start_stream_sid = msg.get("streamSid", "")
                break
    except Exception as error:
        _log_safe_exception("start_event_error", error, call_sid)
        await websocket.close(code=1008)
        return

    if not ws_token or not start_stream_sid:
        logger.warning(
            "media_event event=stream_auth_rejected call=%s reason=invalid_start",
            _call_label(call_sid),
        )
        await websocket.close(code=1008)
        return

    # Begin bounded reads before database/config startup so Twilio audio cannot
    # sit unread while the call is authenticated and the provider connects.
    playback_marks = _TwilioPlaybackMarks(call_sid=call_sid)
    ingress = _TwilioMediaIngress(
        websocket,
        call_sid=call_sid,
        on_playback_mark=playback_marks.resolve,
    )
    ingress_task = asyncio.create_task(ingress.run())

    # Validate WebSocket token against RTDB
    call_data = None
    try:
        _init_firebase()
        from firebase_admin import db as rtdb

        ref = rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}")
        loop = asyncio.get_event_loop()
        for attempt in range(3):
            call_data = await loop.run_in_executor(None, ref.get)
            if call_data:
                break
            await asyncio.sleep(0.5)
    except Exception as error:
        _log_safe_exception("rtdb_lookup_error", error, call_sid)

    # Verify the token matches what we stored in RTDB for this call.
    if not call_data or not call_data.get("ws_token"):
        logger.warning(
            "media_event event=stream_auth_rejected call=%s reason=missing_record",
            _call_label(call_sid),
        )
        await _cancel_task(ingress_task)
        await websocket.close(code=1008)
        return
    if not ws_token or ws_token != call_data["ws_token"]:
        logger.warning(
            "media_event event=stream_auth_rejected call=%s reason=invalid_token",
            _call_label(call_sid),
        )
        await _cancel_task(ingress_task)
        await websocket.close(code=1008)
        return

    if ingress.ended:
        await _cancel_task(ingress_task)
        try:
            await websocket.close()
        except Exception:
            pass
        return

    stream_sid = start_stream_sid
    pipeline = None
    transcript_lines = []
    last_rtdb_update = 0.0

    try:
        active_call = await _resolve_active_call(call_sid, call_data)

        # Load contractor config for this call.
        contractor_config_loaded = {}
        _contractor_id = ""
        if active_call:
            _contractor_id = getattr(active_call, "contractor_id", "") or ""
            if _contractor_id:
                from app.db.contractors import get_contractor
                from app.services.entitlements import with_entitlement_flags

                contractor_data = await get_contractor(_contractor_id)
                if contractor_data:
                    contractor_config_loaded = with_entitlement_flags(contractor_data)
            if active_call.caller_name:
                contractor_config_loaded["known_caller_name"] = active_call.caller_name
    except Exception as error:
        _log_safe_exception("stream_setup_error", error, call_sid)
        await _cancel_task(ingress_task)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return

    async def on_audio_out(mulaw_chunk: bytes):
        """Voice pipeline produced audio — send to Twilio."""
        nonlocal stream_sid
        return await _send_twilio_audio(
            websocket,
            stream_sid=stream_sid or "",
            mulaw_chunk=mulaw_chunk,
            call_sid=call_sid,
        )

    async def on_clear_audio():
        """Clear Twilio's outbound audio buffer (used during barge-in)."""
        nonlocal stream_sid
        return await _clear_twilio_audio_with_playback_marks(
            websocket,
            stream_sid=stream_sid or "",
            playback_marks=playback_marks,
            call_sid=call_sid,
        )

    async def on_response_first_media_sent(turn: int):
        """Ask Twilio to acknowledge first-response media playout or clearing."""
        return await _send_twilio_playback_mark(
            websocket,
            stream_sid=stream_sid or "",
            playback_marks=playback_marks,
            turn=turn,
            epoch=turn,
            phase="first_media",
            call_sid=call_sid,
        )

    async def on_response_end_media_sent(turn: int):
        """Observe whether Twilio plays or clears the complete model response."""
        return await _send_twilio_playback_mark(
            websocket,
            stream_sid=stream_sid or "",
            playback_marks=playback_marks,
            turn=turn,
            epoch=turn,
            phase="response_end",
            call_sid=call_sid,
        )

    call_redirected = False  # Set when call is accepted/redirected to conference

    async def on_call_complete(*, twiml: str | None = None):
        """Hang up the call after Kevin says goodbye.

        Skip hangup if the call was redirected to a conference (user picked up).
        """
        nonlocal call_redirected
        if call_redirected:
            logger.info(
                "media_event event=hangup_skipped call=%s reason=conference_redirect",
                _call_label(call_sid),
            )
            return
        await _complete_twilio_call(
            call_sid=call_sid,
            websocket=websocket,
            twiml=twiml,
        )

    async def on_stream_stop():
        """Record whether Twilio stopped because the owner accepted the call."""
        nonlocal call_redirected
        try:
            refreshed = await get_active_call(call_sid)
            if refreshed and getattr(refreshed, "accepted", False):
                call_redirected = True
            elif refreshed:
                raw = refreshed.__dict__ if hasattr(refreshed, "__dict__") else {}
                call_redirected = raw.get("accepted") is True
        except Exception as error:
            _log_safe_exception("stream_stop_refresh_error", error, call_sid)

        logger.info(
            "media_event event=twilio_stream_stopped call=%s redirected=%s",
            _call_label(call_sid),
            call_redirected,
        )

    async def on_max_duration():
        logger.info(
            "media_event event=max_duration call=%s seconds=%s",
            _call_label(call_sid),
            MAX_CALL_DURATION,
        )
        await _finish_max_call_duration(on_call_complete)

    async def on_transcript(speaker: str, text: str):
        """Transcript update — both Kevin and Caller sides."""
        nonlocal last_rtdb_update

        transcript_lines.append(f"{speaker}: {text}")

        # Cap transcript lines to prevent unbounded memory growth
        if len(transcript_lines) > 500:
            transcript_lines[:] = transcript_lines[-500:]

        # Send FULL transcript to RTDB — no truncation
        transcript_text = "\n".join(transcript_lines)

        # Update RTDB (for app polling)
        now = time.time()
        if now - last_rtdb_update >= TRANSCRIPT_THROTTLE:
            last_rtdb_update = now
            task = asyncio.create_task(update_active_call(call_sid, {
                "transcript_buffer": transcript_text,
            }))
            task.add_done_callback(_log_task_exception)

    _urgency_push_count = 0

    async def on_urgency_detected(transcript_snippet: str):
        """Emergency keyword detected — send VoIP push + critical alert."""
        nonlocal _urgency_push_count
        if _urgency_push_count >= 1:
            return  # Rate limit: max 1 urgency push per call

        _urgency_push_count += 1
        _cid = contractor_config_loaded.get("contractor_id", "")

        # Send VoIP push to ring the contractor's phone
        from app.services.push_notification import send_voip_push, send_urgent_push, get_device_token
        from app.services.conference_registry import (
            new_conference_name,
            register_conference,
        )

        voip_token = await get_device_token(token_type="voip", contractor_id=_cid)
        if voip_token:
            caller_phone = active_call.caller_phone if active_call else ""
            caller_name = active_call.caller_name if active_call else ""
            # F-07/F-13: opaque random conference name (was f"urgent_{call_sid}").
            urgent_conf = new_conference_name("urgent")
            if _cid:
                await register_conference(urgent_conf, _cid, call_sid)
            await send_voip_push(
                device_token=voip_token,
                caller_phone=caller_phone,
                caller_name=f"URGENT: {caller_name or caller_phone}",
                reason="urgent_call",
                call_sid=call_sid,
                conference_name=urgent_conf,
            )

        # Also send critical push notification without lock-screen-sensitive context.
        push_token = await get_device_token(contractor_id=_cid)
        if push_token:
            caller_name = active_call.caller_name if active_call else ""
            caller_phone = active_call.caller_phone if active_call else ""
            body = _safe_urgent_push_body(caller_name=caller_name, caller_phone=caller_phone)
            await send_urgent_push(
                device_token=push_token,
                title="URGENT CALL",
                body=body,
                call_sid=call_sid,
                caller_phone=caller_phone,
                caller_name=caller_name,
            )

        logger.info("media_event event=urgency_escalated call=%s", _call_label(call_sid))

    MAX_CALL_DURATION = 5400  # 90 minutes in seconds

    try:
        logger.info("media_event event=twilio_stream_started call=%s", _call_label(call_sid))

        # Select voice pipeline based on contractor config
        voice_engine = contractor_config_loaded.get("voice_engine", "elevenlabs")

        if voice_engine == "gemini" and settings.gemini_api_key:
            from app.services.gemini_pipeline import GeminiPipeline
            pipeline = GeminiPipeline(
                on_audio_out=on_audio_out,
                on_transcript=on_transcript,
                on_clear_audio=on_clear_audio,
                on_response_first_media_sent=on_response_first_media_sent,
                on_response_end_media_sent=on_response_end_media_sent,
                on_call_complete=on_call_complete,
                on_urgency_detected=on_urgency_detected,
                call_sid=call_sid,
                contractor_config=contractor_config_loaded,
                caller_phone=active_call.caller_phone if active_call else "",
                call_started_at=media_stream_started_at,
            )
            logger.info(
                "media_event event=pipeline_selected call=%s engine=gemini",
                _call_label(call_sid),
            )
        else:
            pipeline = VoicePipeline(
                on_audio_out=on_audio_out,
                on_transcript=on_transcript,
                on_clear_audio=on_clear_audio,
                on_response_first_media_sent=on_response_first_media_sent,
                on_response_end_media_sent=on_response_end_media_sent,
                on_call_complete=on_call_complete,
                on_urgency_detected=on_urgency_detected,
                call_sid=call_sid,
                contractor_config=contractor_config_loaded,
                caller_phone=active_call.caller_phone if active_call else "",
                call_started_at=media_stream_started_at,
            )
            logger.info(
                "media_event event=pipeline_selected call=%s engine=elevenlabs",
                _call_label(call_sid),
            )
        started = await _serve_pipeline_ingress(
            pipeline,
            ingress,
            call_sid=call_sid,
            media_stream_started_at=media_stream_started_at or 0.0,
            call_started_at=call_started_at,
            max_call_duration_seconds=MAX_CALL_DURATION,
            on_stream_stop=on_stream_stop,
            on_max_duration=on_max_duration,
        )
        if not started:
            logger.error("Failed to start voice pipeline — closing stream")
            await websocket.close()
            return

    except Exception as error:
        _log_safe_exception("stream_error", error, call_sid)

    finally:
        await _cancel_task(ingress_task)
        playback_marks.close()

        if pipeline:
            try:
                await pipeline.stop()
            except Exception as error:
                logger.error(
                    "Voice pipeline shutdown failed: %s",
                    type(error).__name__,
                )

        try:
            await websocket.close()
        except Exception:
            pass  # Already closed or connection lost

        # Save transcript before creating a durable handoff for derived effects.
        transcript_saved = False
        if transcript_lines:
            from app.db.calls import save_call

            try:
                transcript_saved = await save_call(call_sid, {
                    "transcript": "\n".join(transcript_lines),
                })
            except Exception as error:
                logger.error(
                    "Post-call transcript persistence raised: %s",
                    type(error).__name__,
                )
            if not transcript_saved:
                logger.error("Post-call transcript persistence failed")

        # Post-call: extract caller info and save to contacts
        # Skip if call was accepted (redirected to conference) — post-call runs after conference ends
        if transcript_saved and active_call and not call_redirected:
            # Persist then await the claimed handoff. Pending work can be picked
            # up by another instance; stale uncertain work is never replayed.
            contractor_phone = contractor_config_loaded.get("owner_phone", "")
            from app.services.post_call_handoff import enqueue_and_run_post_call

            twilio_number = contractor_config_loaded.get("twilio_number", "")
            caller_language = pipeline._language if pipeline else "en"
            await enqueue_and_run_post_call(
                transcript_lines=list(transcript_lines),
                caller_phone=active_call.caller_phone if active_call else "",
                call_sid=call_sid,
                contractor_phone=contractor_phone,
                twilio_number=twilio_number,
                contractor=contractor_config_loaded,
                caller_language=caller_language,
            )

        logger.info("media_event event=stream_closed call=%s", _call_label(call_sid))
