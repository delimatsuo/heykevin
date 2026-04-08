"""Real-time speech-to-text using Deepgram.

Runs alongside Gemini — receives the same audio from Twilio,
produces text transcripts for the iOS app and Telegram.
"""

import asyncio
import json
import base64
from typing import Callable, Awaitable, Optional

import websockets

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"


class RealtimeTranscriber:
    """Streams audio to Deepgram and returns text transcripts."""

    def __init__(self, on_transcript: Callable[[str, str], Awaitable[None]]):
        """
        Args:
            on_transcript: async callback(speaker, text) called when speech is detected.
                          speaker is "caller" (from Twilio audio) or "kevin" (from Gemini audio)
        """
        self.on_transcript = on_transcript
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._receive_task: Optional[asyncio.Task] = None

    async def connect(self) -> bool:
        """Connect to Deepgram streaming STT."""
        if not settings.deepgram_api_key:
            logger.warning("No Deepgram API key — transcription disabled")
            return False

        try:
            url = (
                f"{DEEPGRAM_WS_URL}"
                f"?encoding=linear16"
                f"&sample_rate=16000"
                f"&channels=1"
                f"&punctuate=true"
                f"&interim_results=false"
                f"&endpointing=300"
                f"&vad_events=false"
            )

            self._ws = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Token {settings.deepgram_api_key}"},
            )

            self._receive_task = asyncio.create_task(self._receive_loop())
            logger.info("Deepgram transcriber connected")
            return True

        except Exception as e:
            logger.error(f"Deepgram connect failed: {e}", exc_info=True)
            return False

    async def send_audio(self, pcm_16k_bytes: bytes):
        """Send PCM 16kHz audio to Deepgram for transcription."""
        if self._ws:
            try:
                await self._ws.send(pcm_16k_bytes)
            except Exception:
                pass

    async def _receive_loop(self):
        """Receive transcription results from Deepgram."""
        try:
            async for message in self._ws:
                data = json.loads(message)

                # Check for final transcript
                channel = data.get("channel", {})
                alternatives = channel.get("alternatives", [])
                if not alternatives:
                    continue

                transcript = alternatives[0].get("transcript", "").strip()
                is_final = data.get("is_final", False)

                if transcript and is_final:
                    await self.on_transcript("Caller", transcript)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Deepgram receive error: {e}", exc_info=True)

    async def close(self):
        """Close the Deepgram connection."""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:
                pass
        logger.info("Deepgram transcriber closed")
