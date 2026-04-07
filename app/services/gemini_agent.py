"""Gemini Live API client — manages WebSocket connection for voice conversations."""

import asyncio
import base64
import json
from typing import Optional, Callable, Awaitable

import websockets

from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage"
    ".v1beta.GenerativeService.BidiGenerateContent"
    f"?key={settings.gemini_api_key}"
)

KEVIN_SYSTEM_PROMPT = f"""You are Kevin, {settings.user_name}'s personal phone assistant. You answer the phone when {settings.user_name} is not available.

YOUR ROLE: You are a gatekeeper. Find out WHO is calling and WHAT it's about. Then check if {settings.user_name} is available.

FLOW:
1. Greet: "Hi, this is Kevin, {settings.user_name}'s assistant. How can I help you?"
2. Get their name and one-line reason for calling.
3. Say "Let me check if he's available, one moment please." Then wait silently for about 20 seconds.
4. After waiting, come back and say: "I'm sorry, {settings.user_name} is not available right now. Can I take a message? I'll make sure he gets it right away."
5. If they want to leave a message, listen and acknowledge: "Got it, I'll pass that along."
6. Ask: "What's the best number to reach you?"
7. Once you have their message and number, say: "I'll make sure {settings.user_name} gets this. Have a great day!"

RULES:
- ONE short sentence per response. Two sentences max.
- NEVER repeat or paraphrase what the caller said.
- NEVER ask detailed follow-up questions about the topic of their call.
- NEVER reveal you are AI.
- Sound natural and warm. Use "Got it", "Sure thing", "Absolutely".
- Refer to {settings.user_name} as "he" (male)."""


class GeminiSession:
    """Manages a single Gemini Live API WebSocket session."""

    def __init__(self, on_audio: Callable[[bytes], Awaitable[None]], on_transcript: Optional[Callable[[str], Awaitable[None]]] = None):
        self.on_audio = on_audio  # callback when Gemini produces audio
        self.on_transcript = on_transcript  # callback when text is available
        self._ws = None
        self._receive_task = None
        self._connected = False

    async def connect(self):
        """Connect to Gemini Live API and send setup."""
        try:
            self._ws = await websockets.connect(GEMINI_WS_URL)

            setup_msg = {
                "setup": {
                    "model": "models/gemini-2.5-flash-native-audio-preview-12-2025",
                    "generation_config": {
                        "response_modalities": ["AUDIO"],
                        "speech_config": {
                            "voice_config": {
                                "prebuilt_voice_config": {
                                    "voice_name": "Puck"
                                }
                            }
                        },
                    },
                    "system_instruction": {
                        "parts": [{"text": KEVIN_SYSTEM_PROMPT}]
                    },
                }
            }

            await self._ws.send(json.dumps(setup_msg))
            response = await self._ws.recv()
            data = json.loads(response)

            if "setupComplete" in data:
                self._connected = True
                logger.info("Gemini session established")
            else:
                logger.error(f"Gemini setup failed: {json.dumps(data)[:200]}")
                return False

            # Prompt Gemini to greet the caller
            await self._ws.send(json.dumps({
                "client_content": {
                    "turns": [
                        {"role": "user", "parts": [{"text": "A caller just connected. Greet them now."}]}
                    ],
                    "turn_complete": True,
                }
            }))

            # Start receiving audio from Gemini
            self._receive_task = asyncio.create_task(self._receive_loop())
            return True

        except Exception as e:
            logger.error(f"Gemini connect failed: {e}", exc_info=True)
            return False

    async def send_audio(self, pcm_16k_bytes: bytes):
        """Send PCM 16kHz audio to Gemini."""
        if not self._connected or not self._ws:
            return

        try:
            audio_b64 = base64.b64encode(pcm_16k_bytes).decode("utf-8")
            await self._ws.send(json.dumps({
                "realtime_input": {
                    "media_chunks": [
                        {
                            "data": audio_b64,
                            "mime_type": "audio/pcm;rate=16000",
                        }
                    ]
                }
            }))
        except Exception as e:
            logger.warning(f"Failed to send audio to Gemini: {e}")

    async def _receive_loop(self):
        """Receive audio/text from Gemini and forward via callbacks."""
        try:
            async for message in self._ws:
                data = json.loads(message)

                server_content = data.get("serverContent", {})
                model_turn = server_content.get("modelTurn", {})
                parts = model_turn.get("parts", [])

                for part in parts:
                    # Audio response
                    inline_data = part.get("inlineData", {})
                    if inline_data.get("mimeType", "").startswith("audio/"):
                        audio_b64 = inline_data.get("data", "")
                        if audio_b64:
                            audio_bytes = base64.b64decode(audio_b64)
                            await self.on_audio(audio_bytes)

                    # Text transcript
                    if "text" in part and self.on_transcript:
                        await self.on_transcript(part["text"])

        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Gemini WebSocket connection closed")
        except Exception as e:
            logger.error(f"Gemini receive error: {e}", exc_info=True)

    async def close(self):
        """Close the Gemini session."""
        self._connected = False
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        logger.info("Gemini session closed")
