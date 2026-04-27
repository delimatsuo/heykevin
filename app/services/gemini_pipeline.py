"""Gemini Live API voice pipeline — audio-native alternative to Deepgram+Claude+ElevenLabs.

Single WebSocket handles STT + LLM reasoning + TTS natively.
Audio conversion at boundaries: mulaw 8kHz (Twilio) <-> PCM 16kHz/24kHz (Gemini).
"""

import asyncio
import base64
import json
import time
from typing import Callable, Awaitable, Optional

import websockets

from app.config import settings
from app.services.entitlements import effective_mode
from app.services.voice_pipeline import build_system_prompt
from app.utils.audio import mulaw_to_pcm16k, pcm24k_to_mulaw
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Per-language Gemini voice selection (male voices for Kevin persona)
GEMINI_VOICES = {
    "en": "Puck",      # Warm, upbeat American — best-tested English voice
    "pt": "Orus",      # Authoritative, clear — suits Brazilian Portuguese formality
    "de": "Charon",    # Calm, professional — matches German business tone
    "fr": "Puck",      # Adapts well to French prosody
    "it": "Puck",      # Expressive, warm — suits Italian's melodic cadence
    "es": "Charon",    # Clear, composed — suits Castilian Spanish
}
GEMINI_VOICE_DEFAULT = "Puck"

GEMINI_MODEL = "gemini-2.5-flash-native-audio-latest"


def _gemini_ws_url() -> str:
    """Construct Gemini Live API WebSocket URL."""
    return (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
        f"?key={settings.gemini_api_key}"
    )


class GeminiPipeline:
    """Voice pipeline using Gemini Live API (audio-native).

    Drop-in alternative to VoicePipeline with the same public interface:
    - start() -> connects and delivers greeting
    - process_audio_in(mulaw_bytes) -> feeds caller audio
    - stop() -> closes connection

    Same callbacks: on_audio_out, on_transcript, on_clear_audio,
    on_call_complete, on_urgency_detected.
    """

    URGENCY_KEYWORDS = {
        "emergency", "flood", "flooding", "fire", "gas leak", "pipe burst",
        "no water", "sewage", "sparking", "smoke", "hospital", "accident",
        "burst pipe", "water everywhere", "electrical fire", "carbon monoxide",
        "burning smell", "smell burning", "electrical panel", "electric panel",
        "breaker tripped", "tripped breaker",
    }

    GOODBYE_PHRASES = [
        "have a great day", "have a good day", "have a nice day",
        "goodbye", "take care",
    ]

    def __init__(
        self,
        on_audio_out: Callable[[bytes], Awaitable[None]],
        on_transcript: Callable[[str, str], Awaitable[None]],
        on_clear_audio: Optional[Callable[[], Awaitable[None]]] = None,
        on_call_complete: Optional[Callable[[], Awaitable[None]]] = None,
        on_urgency_detected: Optional[Callable[[str], Awaitable[None]]] = None,
        call_sid: str = "",
        contractor_config: Optional[dict] = None,
    ):
        self.on_audio_out = on_audio_out
        self.on_transcript = on_transcript
        self.on_clear_audio = on_clear_audio
        self.on_call_complete = on_call_complete
        self.on_urgency_detected = on_urgency_detected
        self._call_sid = call_sid
        self._contractor_config = contractor_config or {}

        self._ws = None
        self._receive_task = None
        self._connected = False

        # State tracking
        self._is_speaking = False
        self._interrupt_speaking = False
        self._urgency_detected = False
        self._exchange_count = 0
        self._last_speech_time = time.time()
        self._silence_check_task = None
        self._unavailable_task = None
        self._unavailable_said = False
        self._command_check_task = None

        # Transcript accumulation (for post-call processing)
        self._transcript_lines: list[str] = []

        # Buffers for streaming transcript fragments (Gemini sends word-by-word)
        self._kevin_transcript_buf: list[str] = []
        self._caller_transcript_buf: list[str] = []

        # Build system prompt from contractor config (reuse existing logic)
        mode = self._contractor_config.get("effective_mode") or effective_mode(self._contractor_config)
        if mode == "personal":
            self._after_hours = False
        else:
            from app.services.quiet_hours import is_business_hours
            self._after_hours = not is_business_hours(self._contractor_config)
        self._system_prompt = build_system_prompt(self._contractor_config, after_hours=self._after_hours)

        # Voice selection — pick the best voice for the contractor's language
        user_language = self._contractor_config.get("user_language", "en")
        self._voice = GEMINI_VOICES.get(user_language, GEMINI_VOICE_DEFAULT)

        # Language for post-call processing
        self._language = user_language or "en"

    async def start(self) -> bool:
        """Connect to Gemini Live API and send setup message."""
        try:
            self._ws = await websockets.connect(
                _gemini_ws_url(),
                max_size=10 * 1024 * 1024,  # 10MB max message
            )

            # Build setup message
            setup = {
                "setup": {
                    "model": f"models/{GEMINI_MODEL}",
                    "generation_config": {
                        "response_modalities": ["AUDIO"],
                        "speech_config": {
                            "voice_config": {
                                "prebuilt_voice_config": {
                                    "voice_name": self._voice,
                                }
                            }
                        },
                    },
                    "system_instruction": {
                        "parts": [{"text": self._system_prompt}]
                    },
                    "input_audio_transcription": {},
                    "output_audio_transcription": {},
                }
            }

            # Add tools if contractor has Jobber or Google Calendar
            tools = self._build_gemini_tools()
            if tools:
                setup["setup"]["tools"] = tools

            await self._ws.send(json.dumps(setup))
            response = await asyncio.wait_for(self._ws.recv(), timeout=10)
            data = json.loads(response)

            if "setupComplete" not in data:
                logger.error(f"Gemini setup failed: {json.dumps(data)[:200]}")
                return False

            self._connected = True
            logger.info(f"Gemini Live session established (voice={self._voice}, model={GEMINI_MODEL})")

            # Start receiving audio/text from Gemini
            self._receive_task = asyncio.create_task(self._receive_loop())

            # Start silence timeout check
            self._silence_check_task = asyncio.create_task(self._silence_check_loop())

            # Start RTDB command polling (for decline/take_message from iOS app)
            if self._call_sid:
                self._command_check_task = asyncio.create_task(self._command_check_loop())

            # Send greeting prompt — Gemini will speak the greeting
            business_name = self._contractor_config.get(
                "business_name",
                f"{self._contractor_config.get('owner_name', settings.user_name)}'s office",
            )
            owner_name = self._contractor_config.get("owner_name", settings.user_name)
            mode = self._contractor_config.get("effective_mode") or effective_mode(self._contractor_config)

            if mode == "personal":
                greeting_prompt = (
                    f"Greet the caller now. Say: 'Hi, this is Kevin, "
                    f"{owner_name.split()[0]}'s assistant. How can I help?'"
                )
            elif self._after_hours:
                hours_start = self._contractor_config.get("business_hours_start", "8:00")
                hours_end = self._contractor_config.get("business_hours_end", "5:00")
                greeting_prompt = (
                    f"Greet the caller now. You are answering the phone for {business_name}. "
                    f"The business is currently closed — hours are {hours_start} to {hours_end}. "
                    f"Offer to take a message."
                )
            else:
                greeting_prompt = (
                    f"Greet the caller now. Say: 'Hi, thanks for calling {business_name}, "
                    f"this is Kevin. How can I help you?'"
                )

            await self._ws.send(json.dumps({
                "client_content": {
                    "turns": [
                        {"role": "user", "parts": [{"text": greeting_prompt}]}
                    ],
                    "turn_complete": True,
                }
            }))

            return True

        except Exception as e:
            logger.error(f"Gemini connect failed: {e}", exc_info=True)
            return False

    async def process_audio_in(self, mulaw_bytes: bytes):
        """Convert mulaw 8kHz -> PCM 16kHz and send to Gemini."""
        if not self._connected or not self._ws:
            return
        try:
            pcm_16k = mulaw_to_pcm16k(mulaw_bytes)
            audio_b64 = base64.b64encode(pcm_16k).decode("utf-8")
            await self._ws.send(json.dumps({
                "realtime_input": {
                    "media_chunks": [{
                        "data": audio_b64,
                        "mime_type": "audio/pcm;rate=16000",
                    }]
                }
            }))
        except Exception:
            pass  # Non-critical — audio will resume on next chunk

    async def stop(self):
        """Close Gemini session and cancel background tasks."""
        self._connected = False
        self._interrupt_speaking = True
        if self._silence_check_task:
            self._silence_check_task.cancel()
        if self._unavailable_task:
            self._unavailable_task.cancel()
        if self._command_check_task:
            self._command_check_task.cancel()
        if self._receive_task:
            self._receive_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        logger.info("Gemini pipeline stopped")

    # --- Receive Loop ---

    async def _receive_loop(self):
        """Process messages from Gemini Live API."""
        try:
            async for message in self._ws:
                if not self._connected:
                    break

                data = json.loads(message)
                server_content = data.get("serverContent", {})

                # Handle interruption (barge-in)
                if server_content.get("interrupted"):
                    self._interrupt_speaking = True
                    self._kevin_transcript_buf.clear()
                    if self.on_clear_audio:
                        await self.on_clear_audio()
                    logger.info("Gemini: caller interrupted (barge-in)")
                    continue

                # Handle model turn (audio output)
                model_turn = server_content.get("modelTurn", {})
                for part in model_turn.get("parts", []):
                    inline_data = part.get("inlineData", {})
                    if inline_data.get("mimeType", "").startswith("audio/"):
                        audio_b64 = inline_data.get("data", "")
                        if audio_b64:
                            pcm_24k = base64.b64decode(audio_b64)
                            mulaw_chunk = pcm24k_to_mulaw(pcm_24k)
                            await self.on_audio_out(mulaw_chunk)
                            self._is_speaking = True

                # Buffer Kevin's transcript fragments (sent word-by-word)
                output_text = self._extract_transcript(server_content, "output")
                if output_text:
                    self._kevin_transcript_buf.append(output_text)

                # Buffer caller's transcript fragments (sent word-by-word)
                input_text = self._extract_transcript(server_content, "input")
                if not input_text:
                    input_text = self._extract_transcript(data, "input")
                if input_text:
                    self._caller_transcript_buf.append(input_text)

                # Flush caller transcript when Kevin starts speaking (turn boundary)
                if model_turn.get("parts") and self._caller_transcript_buf:
                    await self._flush_caller_transcript()

                # Handle turn completion — Kevin finished speaking
                if server_content.get("turnComplete"):
                    self._is_speaking = False
                    # Flush Kevin's buffered transcript as one message
                    if self._kevin_transcript_buf:
                        full_text = "".join(self._kevin_transcript_buf)
                        self._kevin_transcript_buf.clear()
                        self._transcript_lines.append(f"Kevin: {full_text}")
                        await self.on_transcript("Kevin", full_text)
                        self._last_speech_time = time.time()
                        self._exchange_count += 1

                        # Goodbye detection
                        if any(p in full_text.lower() for p in self.GOODBYE_PHRASES):
                            logger.info("Kevin said goodbye — ending call in 2 seconds")
                            await asyncio.sleep(2)
                            if self.on_call_complete:
                                await self.on_call_complete()
                            return

                        # Start unavailability timer after 3 exchanges
                        if self._exchange_count >= 3 and not self._unavailable_task:
                            self._unavailable_task = asyncio.create_task(self._unavailable_timer())

                # Handle tool calls
                tool_call = data.get("toolCall", {})
                function_calls = tool_call.get("functionCalls", [])
                if function_calls:
                    # Flush any pending caller transcript before tool execution
                    if self._caller_transcript_buf:
                        await self._flush_caller_transcript()
                    await self._handle_tool_calls(function_calls)

        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed as e:
            # rcvd_then_sent: True = peer (Gemini) closed first, False = we closed first, None = abnormal
            peer_initiated = getattr(e, "rcvd_then_sent", None)
            logger.warning(
                f"Gemini WebSocket closed: code={e.code} reason={e.reason!r} "
                f"peer_initiated={peer_initiated}"
            )
            # Attempt one reconnect
            if self._connected:
                logger.info("Attempting Gemini reconnection...")
                reconnected = await self.start()
                if not reconnected:
                    logger.error("Gemini reconnection failed")
                    if self.on_call_complete:
                        await self.on_call_complete()
        except Exception as e:
            logger.error(f"Gemini receive error: {e}", exc_info=True)

    @staticmethod
    def _extract_transcript(obj: dict, direction: str) -> str:
        """Extract transcript text from a Gemini message for 'input' or 'output'."""
        # Try outputTranscript / inputTranscript (string)
        text = obj.get(f"{direction}Transcript", "")
        if text:
            return text
        # Try outputTranscription / inputTranscription (dict with text field)
        t = obj.get(f"{direction}Transcription", {})
        if isinstance(t, dict):
            return t.get("text", "")
        if isinstance(t, str):
            return t
        return ""

    async def _flush_caller_transcript(self):
        """Flush buffered caller transcript fragments as one message."""
        full_text = "".join(self._caller_transcript_buf)
        self._caller_transcript_buf.clear()
        if not full_text.strip():
            return
        self._transcript_lines.append(f"Caller: {full_text}")
        await self.on_transcript("Caller", full_text)
        self._last_speech_time = time.time()

        # Urgency detection
        if not self._urgency_detected and self.on_urgency_detected:
            text_lower = full_text.lower()
            for keyword in self.URGENCY_KEYWORDS:
                if keyword in text_lower:
                    self._urgency_detected = True
                    logger.info(f"URGENCY DETECTED: '{keyword}' in '{full_text}'")
                    asyncio.create_task(self.on_urgency_detected(full_text))
                    if self._unavailable_task and not self._unavailable_task.done():
                        self._unavailable_task.cancel()
                        self._unavailable_task = None
                    break

    # --- Tool Calling ---

    def _build_gemini_tools(self) -> list:
        """Build Gemini-format tool definitions from contractor config."""
        has_jobber = bool(self._contractor_config.get("jobber_access_token"))
        has_gcal = bool(self._contractor_config.get("google_calendar_access_token"))

        if not has_jobber and not has_gcal:
            return []

        declarations = []

        if has_jobber:
            declarations.extend([
                {
                    "name": "check_customer",
                    "description": "Look up the caller in the business's customer database by phone number.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "phone": {"type": "STRING", "description": "Phone number in E.164 format"}
                        },
                        "required": ["phone"],
                    },
                },
                {
                    "name": "check_availability",
                    "description": "Check the business's schedule for available appointment slots in the next 7 days.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "days_ahead": {"type": "INTEGER", "description": "Days ahead to check (default 7, max 14)"}
                        },
                    },
                },
                {
                    "name": "book_appointment",
                    "description": "Create a new job/appointment in the business's scheduling system.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "title": {"type": "STRING", "description": "Short description of the job"},
                            "instructions": {"type": "STRING", "description": "Detailed notes about what the customer needs"},
                            "client_id": {"type": "STRING", "description": "Jobber client ID if existing customer"},
                        },
                        "required": ["title"],
                    },
                },
            ])
        elif has_gcal:
            declarations.extend([
                {
                    "name": "check_availability",
                    "description": "Check the business owner's calendar for available appointment slots.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "days_ahead": {"type": "INTEGER", "description": "Days ahead to check (default 7, max 14)"}
                        },
                    },
                },
                {
                    "name": "book_appointment",
                    "description": "Create an appointment on the business owner's calendar.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "title": {"type": "STRING", "description": "Short description of the appointment"},
                            "start_time": {"type": "STRING", "description": "Start time in ISO 8601 format"},
                            "end_time": {"type": "STRING", "description": "End time in ISO 8601 format"},
                            "description": {"type": "STRING", "description": "Additional notes"},
                        },
                        "required": ["title", "start_time", "end_time"],
                    },
                },
            ])

        return [{"function_declarations": declarations}]

    async def _handle_tool_calls(self, function_calls: list):
        """Execute tool calls and send results back to Gemini."""
        from app.services.voice_pipeline import VoicePipeline

        # Reuse VoicePipeline's _execute_tool — it has all the Jobber/Calendar logic
        temp_pipeline = VoicePipeline.__new__(VoicePipeline)
        temp_pipeline._contractor_config = self._contractor_config

        responses = []
        for fc in function_calls:
            tool_name = fc.get("name", "")
            tool_args = fc.get("args", {})
            call_id = fc.get("id", "")

            logger.info(f"Gemini tool call: {tool_name}({tool_args})")

            try:
                result_str = await asyncio.wait_for(
                    temp_pipeline._execute_tool(tool_name, tool_args),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                result_str = json.dumps({"error": "Tool execution timed out"})
            except Exception as e:
                result_str = json.dumps({"error": str(e)})

            responses.append({
                "id": call_id,
                "name": tool_name,
                "response": json.loads(result_str),
            })

        # Send tool responses back to Gemini
        await self._ws.send(json.dumps({
            "tool_response": {
                "function_responses": responses,
            }
        }))

    # --- Timers and Background Tasks ---

    async def _silence_check_loop(self):
        """End call after 2 minutes of silence."""
        SILENCE_TIMEOUT = 120
        try:
            while self._connected:
                await asyncio.sleep(30)
                if not self._connected:
                    break
                elapsed = time.time() - self._last_speech_time
                if elapsed > SILENCE_TIMEOUT:
                    logger.info(f"Silence timeout ({elapsed:.0f}s) — ending call")
                    # Ask Gemini to say goodbye
                    if self._ws and self._connected:
                        await self._ws.send(json.dumps({
                            "client_content": {
                                "turns": [{"role": "user", "parts": [{"text": "The line has gone quiet. Say goodbye and end the call."}]}],
                                "turn_complete": True,
                            }
                        }))
                        await asyncio.sleep(3)
                    if self.on_call_complete:
                        await self.on_call_complete()
                    break
        except asyncio.CancelledError:
            pass

    async def _unavailable_timer(self):
        """After 30 seconds, tell the caller the owner is unavailable."""
        try:
            await asyncio.sleep(30)
            if not self._connected or self._unavailable_said:
                return
            self._unavailable_said = True

            owner_name = self._contractor_config.get("owner_name", settings.user_name)
            pronoun = self._contractor_config.get("pronoun", "he")

            if not self._ws:
                logger.warning("unavailable_timer: Gemini WS not open")
                return
            try:
                await self._ws.send(json.dumps({
                    "client_content": {
                        "turns": [{"role": "user", "parts": [{"text": (
                            f"Tell the caller that {owner_name} is not available right now. "
                            f"Offer to take a message and make sure {pronoun} gets it. "
                            f"Be warm and apologetic."
                        )}]}],
                        "turn_complete": True,
                    }
                }))
                logger.info("Gemini: unavailability message triggered (30s timer)")
            except Exception as e:
                logger.error(f"Failed to send unavailability to Gemini: {e}")
                self._unavailable_said = False  # allow retry
        except asyncio.CancelledError:
            pass

    async def _command_check_loop(self):
        """Poll RTDB for commands from the iOS app (decline, take_message)."""
        try:
            while self._connected:
                await self._check_commands()
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    async def _check_commands(self):
        """Check for pending commands."""
        if not self._call_sid:
            return
        try:
            from app.db.cache import _init_firebase
            from firebase_admin import db as rtdb

            _init_firebase()
            ref = rtdb.reference(f"/call_commands/{self._call_sid}")
            loop = asyncio.get_event_loop()
            command = await loop.run_in_executor(None, ref.get)
            if command:
                await loop.run_in_executor(None, ref.delete)
                cmd_type = command.get("type", "")
                if cmd_type == "take_message" and not self._unavailable_said:
                    if self._unavailable_task:
                        self._unavailable_task.cancel()
                    if not self._ws:
                        logger.warning("take_message: Gemini WS not open — cannot inject")
                        return
                    owner_name = self._contractor_config.get("owner_name", settings.user_name)
                    try:
                        await self._ws.send(json.dumps({
                            "client_content": {
                                "turns": [{"role": "user", "parts": [{"text": (
                                    f"The owner ({owner_name}) has declined the call. "
                                    "Tell the caller they are unavailable and offer to take a message. "
                                    "Be warm and apologetic."
                                )}]}],
                                "turn_complete": True,
                            }
                        }))
                        self._unavailable_said = True
                        logger.info(f"take_message injected into Gemini for {self._call_sid[:8]}")
                    except Exception as e:
                        logger.error(f"Failed to inject take_message into Gemini: {e}")
        except Exception as e:
            logger.warning(f"Command check error: {e}")
