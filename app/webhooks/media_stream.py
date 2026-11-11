"""Twilio Media Streams WebSocket — bridges audio through Deepgram STT → Claude → ElevenLabs TTS."""

import asyncio
import json
import sys
import time

from fastapi import APIRouter, WebSocket

from app.config import settings
from app.services.voice_pipeline import VoicePipeline
from app.db.cache import get_active_call, update_active_call, update_live_transcript, _init_firebase, ACTIVE_CALLS_PATH
from app.utils.logging import get_logger
from app.webhooks.stream_transport import (
    LiveTranscriptPusher as _StreamLiveTranscriptPusher,
    MAX_MEDIA_INGRESS_AUDIO_BYTES,
    MAX_MEDIA_INGRESS_AUDIO_CHUNKS,
    MAX_PENDING_TWILIO_PLAYBACK_MARKS,
    TRANSCRIPT_THROTTLE,
    WS_MAX_MESSAGE_SIZE,
    _PendingPlaybackMark,
    _TwilioIngressEvent,
    _TwilioMediaIngress,
    _TwilioPlaybackMarks,
    _active_call_fallback,
    _call_label,
    _cancel_task,
    _complete_twilio_call,
    _consume_twilio_ingress,
    _finish_max_call_duration,
    _log_safe_exception,
    _log_task_exception,
    _resolve_active_call as _stream_resolve_active_call,
    _safe_urgent_push_body,
    _send_twilio_audio,
    _send_twilio_clear,
    _send_twilio_playback_mark,
    _serve_pipeline_ingress,
)

logger = get_logger(__name__)

router = APIRouter()


class LiveTranscriptPusher(_StreamLiveTranscriptPusher):
    _compat_module = sys.modules[__name__]


async def _resolve_active_call(call_sid: str, call_data: dict | None):
    """Load active call state without delaying an authenticated stream."""
    return await _stream_resolve_active_call(
        call_sid,
        call_data,
        get_active_call_fn=get_active_call,
    )


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

    if call_data.get('kevin_redirect_nonce'):
        from app.services.owner_call_actions import confirm_fallback_stream
        try:
            confirmed = await confirm_fallback_stream(
                call_sid, contractor_id=call_data.get('contractor_id', ''),
                ws_token=ws_token, redirect_nonce=call_data['kevin_redirect_nonce'])
        except Exception:
            confirmed = False
        if not confirmed:
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
                contractor_config_loaded["known_caller_name_trusted"] = (
                    getattr(active_call, "caller_name_trusted", False) is True
                )
            if _contractor_id and active_call.caller_phone:
                from app.services.receptionist_context import load_customer_memory_context

                contractor_config_loaded.update(
                    await load_customer_memory_context(
                        _contractor_id,
                        active_call.caller_phone,
                        personalization_enabled=contractor_config_loaded.get(
                            "customer_memory_personalization_enabled"
                        )
                        is True,
                        mutations_enabled=(
                            settings.service_request_recovery_enabled is True
                            and
                            contractor_config_loaded.get(
                                "service_request_mutations_enabled"
                            )
                            is True
                            and contractor_config_loaded.get(
                                "integration_write_status"
                            )
                            == "approved"
                        ),
                    )
                )
            from app.services.entitlements import has_entitled_token

            if contractor_config_loaded.get("customer_memory") or has_entitled_token(
                contractor_config_loaded, "google_calendar"
            ):
                from app.db.service_requests import FirestoreServiceRequestRepository
                from app.services.google_calendar_request_provider import (
                    GoogleCalendarRequestProvider,
                )
                from app.services.service_request_repository import (
                    ServiceRequestCommandService,
                )

                provider_adapter = (
                    GoogleCalendarRequestProvider(contractor_config_loaded)
                    if has_entitled_token(contractor_config_loaded, "google_calendar")
                    else None
                )
                contractor_config_loaded["service_request_command_service"] = (
                    ServiceRequestCommandService(
                        FirestoreServiceRequestRepository(),
                        provider_adapter=provider_adapter,
                    )
                )
    except Exception as error:
        _log_safe_exception("stream_setup_error", error, call_sid)
        await _cancel_task(ingress_task)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
        return

    transcript_pusher = LiveTranscriptPusher(
        call_sid, transcript_lines,
        contractor_id=call_data.get("contractor_id", ""), ws_token=ws_token,
    )

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
        cleared = await _send_twilio_clear(
            websocket,
            stream_sid=stream_sid or "",
            call_sid=call_sid,
        )
        if cleared:
            playback_marks.mark_pending_cleared()
        return cleared

    async def on_response_first_media_sent(turn: int):
        """Ask Twilio to acknowledge first-response media playout or clearing."""
        return await _send_twilio_playback_mark(
            websocket,
            stream_sid=stream_sid or "",
            playback_marks=playback_marks,
            turn=turn,
            phase="first_media",
            call_sid=call_sid,
        )

    async def on_response_end_media_sent(turn: int):
        """Ask Twilio to acknowledge the final frame of a completed response."""
        return await _send_twilio_playback_mark(
            websocket,
            stream_sid=stream_sid or "",
            playback_marks=playback_marks,
            turn=turn,
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
        transcript_lines.append(f"{speaker}: {text}")

        # Cap transcript lines to prevent unbounded memory growth
        if len(transcript_lines) > 500:
            transcript_lines[:] = transcript_lines[-500:]

        # Update RTDB (for app polling) — throttled with a trailing flush
        transcript_pusher.push()

    _urgency_push_count = 0

    async def on_urgency_detected(transcript_snippet: str):
        """Emergency keyword detected — delegate to urgent_handoff."""
        nonlocal _urgency_push_count
        if _urgency_push_count >= 1:
            return  # Rate limit: max 1 urgency push per call

        _urgency_push_count += 1
        _cid = contractor_config_loaded.get("contractor_id", "")
        if not _cid:
            return

        from app.services.urgent_handoff import dispatch_urgent_escalation

        try:
            await dispatch_urgent_escalation(
                call_sid=call_sid,
                contractor_id=_cid,
                transcript_snippet=transcript_snippet,
            )
            logger.info("media_event event=urgency_escalated call=%s", _call_label(call_sid))
        except Exception as error:
            _log_safe_exception("urgency_escalation_error", error, call_sid)

    MAX_CALL_DURATION = 5400  # 90 minutes in seconds

    try:
        logger.info("media_event event=twilio_stream_started call=%s", _call_label(call_sid))

        # Initial incoming registration pins the engine across webhook retries
        # and profile edits before stream setup. Legacy and owner-directed
        # fallback streams retain their existing configuration behavior.
        voice_engine = contractor_config_loaded.get("voice_engine", "elevenlabs")
        if not call_data.get("kevin_redirect_nonce"):
            voice_engine = call_data.get("registration_voice_engine", voice_engine)

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
                on_call_complete=on_call_complete,
                on_urgency_detected=on_urgency_detected,
                call_sid=call_sid,
                contractor_config=contractor_config_loaded,
                caller_phone=active_call.caller_phone if active_call else "",
            )
            logger.info(
                "media_event event=pipeline_selected call=%s engine=elevenlabs",
                _call_label(call_sid),
            )
        pipeline._command_ws_token = ws_token
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
        try:
            await _cancel_task(ingress_task)
            if pipeline:
                try:
                    await pipeline.stop()
                except Exception as error:
                    logger.error(
                        "Voice pipeline shutdown failed: %s",
                        type(error).__name__,
                    )
        finally:
            await transcript_pusher.close()

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
