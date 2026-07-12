"""Twilio Media Streams WebSocket — bridges audio through Deepgram STT → Claude → ElevenLabs TTS."""

import asyncio
import base64
import json
import time
from types import SimpleNamespace

from fastapi import APIRouter, WebSocket

from app.config import settings
from app.services.voice_pipeline import VoicePipeline
from app.db.cache import get_active_call, update_active_call, _init_firebase, ACTIVE_CALLS_PATH
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()


def _safe_urgent_push_body(caller_name: str = "", caller_phone: str = "") -> str:
    """Return lock-screen-safe urgent call copy with no raw speech or full phone."""
    return "Urgent call needs review. Open Kevin for details."


def _log_task_exception(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"Background task failed: {exc}", exc_info=exc)

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


@router.websocket("/media-stream/{call_sid}")
async def media_stream_ws(websocket: WebSocket, call_sid: str):
    """Bidirectional audio bridge: Twilio <-> Voice Pipeline (STT + Claude + TTS)."""

    # Accept the WebSocket first — Twilio sends custom parameters in the `start` message
    await websocket.accept()
    logger.info(f"Media stream connected: {call_sid}")

    # Wait for the Twilio `start` event to get the ws_token from customParameters
    ws_token = ""
    start_stream_sid = ""
    try:
        # Read messages until we get the start event (should be the first message)
        for _ in range(5):
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=5)
            msg = json.loads(raw)
            if msg.get("event") == "start":
                ws_token = msg.get("start", {}).get("customParameters", {}).get("ws_token", "")
                start_stream_sid = msg.get("streamSid", "")
                break
    except Exception as e:
        logger.error(f"Failed to receive start event for {call_sid}: {e}")
        await websocket.close(code=1008)
        return

    # Validate WebSocket token against RTDB
    _init_firebase()
    from firebase_admin import db as rtdb

    call_data = None
    try:
        ref = rtdb.reference(f"{ACTIVE_CALLS_PATH}/{call_sid}")
        loop = asyncio.get_event_loop()
        for attempt in range(3):
            call_data = await loop.run_in_executor(None, ref.get)
            if call_data:
                break
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"RTDB lookup failed for {call_sid}: {e}")

    # Verify the token matches what we stored in RTDB for this call.
    if not call_data or not call_data.get("ws_token"):
        logger.warning(f"WebSocket: no RTDB record or ws_token for {call_sid} — rejecting")
        await websocket.close(code=1008)
        return
    if not ws_token or ws_token != call_data["ws_token"]:
        logger.warning(f"WebSocket: invalid token for {call_sid} — rejecting")
        await websocket.close(code=1008)
        return

    # Payload size limit for incoming WebSocket messages (64KB)
    WS_MAX_MESSAGE_SIZE = 65536

    stream_sid = None
    pipeline = None
    transcript_lines = []
    last_rtdb_update = 0.0

    # Retry active call lookup — RTDB write from twilio_incoming may still be in-flight
    active_call = await get_active_call(call_sid)
    if not active_call:
        await asyncio.sleep(1)
        active_call = await get_active_call(call_sid)
    if not active_call:
        active_call = _active_call_fallback(call_sid, call_data)
        if active_call:
            logger.warning("Active call lookup missed; using authenticated stream context")

    # Load contractor config for this call
    contractor_config_loaded = {}
    _contractor_id = ""
    if active_call:
        _contractor_id = getattr(active_call, 'contractor_id', '') or ''
        if _contractor_id:
            from app.db.contractors import get_contractor
            from app.services.entitlements import with_entitlement_flags
            contractor_data = await get_contractor(_contractor_id)
            if contractor_data:
                contractor_config_loaded = with_entitlement_flags(contractor_data)
        # Pass known caller name
        if active_call.caller_name:
            contractor_config_loaded["known_caller_name"] = active_call.caller_name

    async def on_audio_out(mulaw_chunk: bytes):
        """Voice pipeline produced audio — send to Twilio."""
        nonlocal stream_sid
        if not stream_sid:
            return
        try:
            payload_b64 = base64.b64encode(mulaw_chunk).decode("utf-8")
            await websocket.send_json({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": payload_b64},
            })
        except Exception as e:
            logger.warning(f"Error sending audio to Twilio: {e}")

    async def on_clear_audio():
        """Clear Twilio's outbound audio buffer (used during barge-in)."""
        nonlocal stream_sid
        if not stream_sid:
            return
        try:
            await websocket.send_json({
                "event": "clear",
                "streamSid": stream_sid,
            })
            logger.info("Cleared Twilio audio buffer (barge-in)")
        except Exception as e:
            logger.warning(f"Error clearing Twilio audio: {e}")

    call_redirected = False  # Set when call is accepted/redirected to conference

    async def on_call_complete():
        """Hang up the call after Kevin says goodbye.

        Skip hangup if the call was redirected to a conference (user picked up).
        """
        nonlocal call_redirected
        if call_redirected:
            logger.info(f"Call {call_sid} redirected to conference — skipping hangup")
            return
        try:
            from twilio.rest import Client
            client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: client.calls(call_sid).update(
                    twiml="<Response><Hangup/></Response>"
                )
            )
            logger.info(f"Call {call_sid} hung up after goodbye")
        except Exception as e:
            logger.warning(f"Error hanging up call: {e}")

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

        logger.info(f"Urgency escalation sent for call {call_sid}")

    MAX_CALL_DURATION = 5400  # 90 minutes in seconds

    try:
        # Use stream_sid from the start event we already consumed during auth
        stream_sid = start_stream_sid
        if not stream_sid:
            logger.error("No stream_sid from Twilio start event")
            await websocket.close()
            return
        logger.info(f"Twilio stream started: {stream_sid}")

        # Select voice pipeline based on contractor config
        voice_engine = contractor_config_loaded.get("voice_engine", "elevenlabs")

        if voice_engine == "gemini" and settings.gemini_api_key:
            from app.services.gemini_pipeline import GeminiPipeline
            pipeline = GeminiPipeline(
                on_audio_out=on_audio_out,
                on_transcript=on_transcript,
                on_clear_audio=on_clear_audio,
                on_call_complete=on_call_complete,
                on_urgency_detected=on_urgency_detected,
                call_sid=call_sid,
                contractor_config=contractor_config_loaded,
                caller_phone=active_call.caller_phone if active_call else "",
            )
            logger.info(f"Using Gemini Live pipeline for call {call_sid}")
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
            logger.info(f"Using ElevenLabs pipeline for call {call_sid}")
        started = await pipeline.start()
        if not started:
            logger.error("Failed to start voice pipeline — closing stream")
            await websocket.close()
            return

        # Track call start time for max duration safeguard
        call_start_time = time.time()

        # Main loop: receive Twilio audio
        async for message in websocket.iter_text():
            if len(message) > WS_MAX_MESSAGE_SIZE:
                logger.warning(f"WebSocket message too large ({len(message)} bytes) — closing")
                await websocket.close(code=1009)
                return

            # Max call duration safeguard (90 minutes)
            if time.time() - call_start_time > MAX_CALL_DURATION:
                logger.info(f"Max call duration ({MAX_CALL_DURATION}s) reached for {call_sid} — ending call")
                try:
                    await pipeline._speak("I'm sorry, we've reached the maximum call duration. Goodbye.")
                except Exception as e:
                    logger.warning(f"Failed to speak max duration message: {e}")
                await on_call_complete()
                break

            data = json.loads(message)
            event = data.get("event", "")

            if event == "media":
                payload = data.get("media", {}).get("payload", "")
                if not payload:
                    continue

                # Send raw mulaw directly to Deepgram — no conversion needed.
                # Deepgram accepts mulaw 8kHz natively.
                mulaw_bytes = base64.b64decode(payload)
                await pipeline.process_audio_in(mulaw_bytes)

            elif event == "stop":
                # Check if the call was accepted (redirected to conference)
                # If so, the stream stop is expected — don't trigger post-call processing
                try:
                    refreshed = await get_active_call(call_sid)
                    if refreshed and getattr(refreshed, 'accepted', False):
                        call_redirected = True
                        logger.info("Twilio stream stopped — call accepted, skipping post-call")
                    elif refreshed:
                        raw = refreshed.__dict__ if hasattr(refreshed, '__dict__') else {}
                        if raw.get("accepted"):
                            call_redirected = True
                            logger.info("Twilio stream stopped — call accepted (dict), skipping post-call")
                except Exception:
                    pass
                if not call_redirected:
                    logger.info("Twilio stream stopped")
                break

    except Exception as e:
        logger.error(f"Media stream error: {e}", exc_info=True)

    finally:
        try:
            await websocket.close()
        except Exception:
            pass  # Already closed or connection lost

        if pipeline:
            try:
                await pipeline.stop()
            except Exception as error:
                logger.error(
                    "Voice pipeline shutdown failed: %s",
                    type(error).__name__,
                )

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

        logger.info(f"Media stream closed: {call_sid}")
