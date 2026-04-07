"""Twilio Media Streams WebSocket — bridges Twilio audio to Gemini Live API."""

import audioop
import asyncio
import base64
import json
import time

from fastapi import APIRouter, WebSocket

from app.services.gemini_agent import GeminiSession
from app.services.telegram_bot import update_transcript
from app.db.cache import get_active_call, update_active_call
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

# Throttle Telegram transcript updates
TRANSCRIPT_THROTTLE = 3.0


@router.websocket("/media-stream/{call_sid}")
async def media_stream_ws(websocket: WebSocket, call_sid: str):
    """Bidirectional audio bridge: Twilio <-> Gemini Live API."""
    await websocket.accept()
    logger.info(f"Media stream connected: {call_sid}")

    stream_sid = None
    gemini = None
    transcript_lines = []
    last_transcript_update = 0.0

    # Look up the active call for Telegram updates
    active_call = await get_active_call(call_sid)

    async def on_gemini_audio(pcm_24k_bytes: bytes):
        """Called when Gemini produces audio — convert and send to Twilio."""
        nonlocal stream_sid
        if not stream_sid:
            return
        try:
            pcm_8k, _ = audioop.ratecv(pcm_24k_bytes, 2, 1, 24000, 8000, None)
            mulaw_bytes = audioop.lin2ulaw(pcm_8k, 2)
            payload_b64 = base64.b64encode(mulaw_bytes).decode("utf-8")
            await websocket.send_json({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": payload_b64},
            })
        except Exception as e:
            logger.warning(f"Error sending audio to Twilio: {e}")

    async def on_gemini_transcript(text: str):
        """Called when Gemini produces text — update Telegram notification."""
        nonlocal last_transcript_update

        if not text.strip():
            return

        # Filter out thinking/reasoning text from Gemini
        # Reasoning text typically: starts with **, contains strategy keywords, is very long
        lower = text.lower().strip()
        if text.startswith("**"):
            return
        if any(kw in lower for kw in ["immediate task", "my approach", "i'll ", "i need to", "my goal", "strategy", "confirming", "i have received", "this clarifies"]):
            return
        if len(text) > 300:
            return

        transcript_lines.append(f"Kevin: {text.strip()}")
        # Keep last 5 lines
        if len(transcript_lines) > 5:
            transcript_lines.pop(0)

        logger.info(f"Kevin: {text.strip()}")

        # Throttle Telegram updates
        now = time.time()
        if now - last_transcript_update < TRANSCRIPT_THROTTLE:
            return
        last_transcript_update = now

        # Update Telegram with transcript
        if active_call and active_call.telegram_message_id:
            transcript_text = "\n".join(transcript_lines)
            asyncio.create_task(update_transcript(
                message_id=active_call.telegram_message_id,
                call_sid=call_sid,
                caller_phone=active_call.caller_phone,
                caller_name=active_call.caller_name,
                carrier=active_call.carrier,
                line_type=active_call.line_type,
                spam_score=active_call.spam_score,
                trust_score=active_call.trust_score,
                transcript=transcript_text,
            ))

    try:
        gemini = GeminiSession(on_audio=on_gemini_audio, on_transcript=on_gemini_transcript)
        connected = await gemini.connect()
        if not connected:
            logger.error("Failed to connect to Gemini — closing stream")
            await websocket.close()
            return

        async for message in websocket.iter_text():
            data = json.loads(message)
            event = data.get("event", "")

            if event == "start":
                stream_sid = data.get("streamSid", "")
                logger.info(f"Twilio stream started: {stream_sid}")

            elif event == "media":
                payload = data.get("media", {}).get("payload", "")
                if not payload:
                    continue

                mulaw_bytes = base64.b64decode(payload)
                pcm_bytes = audioop.ulaw2lin(mulaw_bytes, 2)
                pcm_16k, _ = audioop.ratecv(pcm_bytes, 2, 1, 8000, 16000, None)
                await gemini.send_audio(pcm_16k)

            elif event == "stop":
                logger.info("Twilio stream stopped")
                break

    except Exception as e:
        logger.error(f"Media stream error: {e}", exc_info=True)

    finally:
        if gemini:
            await gemini.close()
        logger.info(f"Media stream closed: {call_sid}")
