# Gemini Live Voice Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Gemini Live API voice pipeline as a switchable alternative to the current Deepgram+Claude+ElevenLabs pipeline, reducing AI costs by 42% and latency by 2-3x.

**Architecture:** A `voice_engine` field on the contractor model selects between `"elevenlabs"` (current default) and `"gemini"`. Both pipelines share the same callback interface (`on_audio_out`, `on_transcript`, etc.) so `media_stream.py` and all post-call processing remain unchanged. The Gemini pipeline sends/receives audio over a single WebSocket with format conversion (mulaw↔PCM) at the boundary.

**Tech Stack:** Gemini Live API (WebSocket), Python `audioop` (audio conversion), `websockets` (already in project), existing Twilio/Firebase/Firestore stack.

**Spec:** `docs/superpowers/specs/2026-04-07-gemini-live-pipeline-design.md`

**Key files in current codebase:**
- `app/services/voice_pipeline.py` — current pipeline (DO NOT MODIFY, reference only)
- `app/webhooks/media_stream.py` — WebSocket bridge, pipeline instantiation at line 361
- `app/config.py` — settings, `gemini_api_key` at line 39
- `app/db/contractors.py` — contractor CRUD
- `app/api/settings.py` — settings API
- `app/services/gemini_agent.py` — old prototype (reference only, will be deleted)

---

### Task 1: Audio Conversion Utilities

**Files:**
- Create: `app/utils/audio.py`

- [ ] **Step 1: Create the audio conversion module**

```python
"""Audio format conversion utilities for Twilio ↔ Gemini.

Twilio sends/expects: mulaw 8kHz mono
Gemini expects: PCM 16-bit 16kHz mono
Gemini outputs: PCM 16-bit 24kHz mono
"""

import audioop


def mulaw_to_pcm16k(mulaw_8k: bytes) -> bytes:
    """Decode mulaw 8kHz to linear PCM 16kHz for Gemini input.

    Steps:
    1. mulaw → linear PCM 16-bit at 8kHz
    2. Upsample 8kHz → 16kHz
    """
    pcm_8k = audioop.ulaw2lin(mulaw_8k, 2)
    pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
    return pcm_16k


def pcm24k_to_mulaw(pcm_24k: bytes) -> bytes:
    """Convert PCM 24kHz from Gemini output to mulaw 8kHz for Twilio.

    Steps:
    1. Downsample 24kHz → 8kHz
    2. Linear PCM → mulaw
    """
    pcm_8k, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, None)
    return audioop.lin2ulaw(pcm_8k, 2)
```

- [ ] **Step 2: Verify with a quick smoke test**

Run in Python REPL or a temp script:

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
python3 -c "
from app.utils.audio import mulaw_to_pcm16k, pcm24k_to_mulaw
import audioop

# Generate 100ms of silence in mulaw 8kHz (800 bytes)
mulaw_silence = b'\xff' * 800
pcm16k = mulaw_to_pcm16k(mulaw_silence)
print(f'mulaw 800 bytes -> PCM 16kHz {len(pcm16k)} bytes')  # expect 3200 (2x sample rate, 2 bytes/sample)

# Generate 100ms of silence in PCM 24kHz (4800 bytes = 2400 samples * 2 bytes)
pcm24k_silence = b'\x00' * 4800
mulaw = pcm24k_to_mulaw(pcm24k_silence)
print(f'PCM 24kHz 4800 bytes -> mulaw {len(mulaw)} bytes')  # expect ~800 (8000 samples/sec * 0.1s)
print('Audio conversion OK')
"
```

Expected: Both conversions succeed, byte counts are approximately correct.

- [ ] **Step 3: Commit**

```bash
git add app/utils/audio.py
git commit -m "feat: add audio conversion utilities for Twilio ↔ Gemini (mulaw/PCM)"
```

---

### Task 2: Gemini Pipeline — Core Connection and Audio Loop

**Files:**
- Create: `app/services/gemini_pipeline.py`

This is the largest task. It creates the `GeminiPipeline` class with the same interface as `VoicePipeline`.

- [ ] **Step 1: Create the Gemini pipeline module with connection and audio flow**

```python
"""Gemini Live API voice pipeline — audio-native alternative to Deepgram+Claude+ElevenLabs.

Single WebSocket handles STT + LLM reasoning + TTS natively.
Audio conversion at boundaries: mulaw 8kHz (Twilio) ↔ PCM 16kHz/24kHz (Gemini).
"""

import asyncio
import base64
import json
import time
from typing import Callable, Awaitable, Optional

import websockets

from app.config import settings
from app.services.voice_pipeline import build_system_prompt
from app.utils.audio import mulaw_to_pcm16k, pcm24k_to_mulaw
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Gemini voice options — male voices sound best per benchmarks
GEMINI_VOICE_DEFAULT = "Puck"       # Male, warm, American
GEMINI_VOICE_SPANISH = "Orus"       # Male, multilingual

GEMINI_MODEL = "gemini-2.5-flash-live-preview-native-audio"


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
    - start() → connects and delivers greeting
    - process_audio_in(mulaw_bytes) → feeds caller audio
    - stop() → closes connection

    Same callbacks: on_audio_out, on_transcript, on_clear_audio,
    on_call_complete, on_urgency_detected.
    """

    URGENCY_KEYWORDS = {
        "emergency", "flood", "flooding", "fire", "gas leak", "pipe burst",
        "no water", "sewage", "sparking", "smoke", "hospital", "accident",
        "burst pipe", "water everywhere", "electrical fire", "carbon monoxide",
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

        # Build system prompt from contractor config (reuse existing logic)
        mode = self._contractor_config.get("mode", "business")
        if mode == "personal":
            self._after_hours = False
        else:
            from app.services.quiet_hours import is_business_hours
            self._after_hours = not is_business_hours(self._contractor_config)
        self._system_prompt = build_system_prompt(self._contractor_config, after_hours=self._after_hours)

        # Voice selection
        user_language = self._contractor_config.get("user_language", "en")
        self._voice = GEMINI_VOICE_SPANISH if (user_language and user_language != "en") else GEMINI_VOICE_DEFAULT

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
                    "session_config": {
                        "contextWindowCompression": {
                            "enabled": True,
                        },
                    },
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

            if self._after_hours:
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
        """Convert mulaw 8kHz → PCM 16kHz and send to Gemini."""
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
                    if self.on_clear_audio:
                        await self.on_clear_audio()
                    logger.info("Gemini: caller interrupted (barge-in)")
                    continue

                # Handle model turn (audio + text output)
                model_turn = server_content.get("modelTurn", {})
                parts = model_turn.get("parts", [])

                for part in parts:
                    # Audio output from Gemini
                    inline_data = part.get("inlineData", {})
                    if inline_data.get("mimeType", "").startswith("audio/"):
                        audio_b64 = inline_data.get("data", "")
                        if audio_b64:
                            pcm_24k = base64.b64decode(audio_b64)
                            mulaw_chunk = pcm24k_to_mulaw(pcm_24k)
                            await self.on_audio_out(mulaw_chunk)
                            self._is_speaking = True

                    # Text output from Gemini (Kevin's words)
                    if "text" in part:
                        kevin_text = part["text"]
                        if kevin_text.strip():
                            self._transcript_lines.append(f"Kevin: {kevin_text}")
                            await self.on_transcript("Kevin", kevin_text)
                            self._exchange_count += 1
                            self._last_speech_time = time.time()

                            # Goodbye detection
                            if any(p in kevin_text.lower() for p in self.GOODBYE_PHRASES):
                                logger.info("Kevin said goodbye — ending call in 2 seconds")
                                await asyncio.sleep(2)
                                if self.on_call_complete:
                                    await self.on_call_complete()
                                return

                            # Start unavailability timer after 3 exchanges
                            if self._exchange_count >= 3 and not self._unavailable_task:
                                self._unavailable_task = asyncio.create_task(self._unavailable_timer())

                # Handle turn completion — Kevin finished speaking
                if server_content.get("turnComplete"):
                    self._is_speaking = False

                # Handle input transcript (caller's words, from Gemini's STT)
                input_transcript = data.get("serverContent", {}).get("inputTranscript", "")
                if not input_transcript:
                    # Alternative location in some Gemini versions
                    input_transcript = data.get("inputTranscript", "")
                if input_transcript:
                    self._transcript_lines.append(f"Caller: {input_transcript}")
                    await self.on_transcript("Caller", input_transcript)
                    self._last_speech_time = time.time()

                    # Urgency detection
                    if not self._urgency_detected and self.on_urgency_detected:
                        text_lower = input_transcript.lower()
                        for keyword in self.URGENCY_KEYWORDS:
                            if keyword in text_lower:
                                self._urgency_detected = True
                                logger.info(f"URGENCY DETECTED: '{keyword}' in '{input_transcript}'")
                                asyncio.create_task(self.on_urgency_detected(input_transcript))
                                if self._unavailable_task and not self._unavailable_task.done():
                                    self._unavailable_task.cancel()
                                    self._unavailable_task = None
                                break

                # Handle tool calls
                tool_call = data.get("toolCall", {})
                function_calls = tool_call.get("functionCalls", [])
                if function_calls:
                    await self._handle_tool_calls(function_calls)

        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Gemini WebSocket closed")
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
        """After 45 seconds, tell the caller the owner is unavailable."""
        try:
            await asyncio.sleep(45)
            if not self._connected or self._unavailable_said:
                return
            self._unavailable_said = True

            owner_name = self._contractor_config.get("owner_name", settings.user_name)
            pronoun = self._contractor_config.get("pronoun", "he")

            # Send instruction to Gemini to deliver the unavailability message
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
            logger.info("Gemini: unavailability message triggered")
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
                    # Trigger unavailability immediately
                    self._unavailable_said = True
                    await self._ws.send(json.dumps({
                        "client_content": {
                            "turns": [{"role": "user", "parts": [{"text": (
                                "The owner has declined the call. Tell the caller they are unavailable "
                                "and offer to take a message. Be warm and apologetic."
                            )}]}],
                            "turn_complete": True,
                        }
                    }))
        except Exception:
            pass
```

- [ ] **Step 2: Verify the module compiles**

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
python3 -m py_compile app/services/gemini_pipeline.py
echo "Compile OK"
```

Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add app/services/gemini_pipeline.py
git commit -m "feat: add Gemini Live voice pipeline (audio-native, same interface as VoicePipeline)"
```

---

### Task 3: Wire Pipeline Selection into Media Stream

**Files:**
- Modify: `app/webhooks/media_stream.py:360-370`

- [ ] **Step 1: Add pipeline selection logic**

Replace the pipeline creation block at line 360-369 with:

```python
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
            )
            logger.info(f"Using ElevenLabs pipeline for call {call_sid}")
```

Everything after this (the `started = await pipeline.start()` check, main loop, post-call processing) remains unchanged — both pipelines have the same interface.

- [ ] **Step 2: Verify syntax**

```bash
python3 -m py_compile app/webhooks/media_stream.py
echo "Compile OK"
```

- [ ] **Step 3: Commit**

```bash
git add app/webhooks/media_stream.py
git commit -m "feat: wire pipeline selection in media_stream (voice_engine toggle)"
```

---

### Task 4: Add voice_engine to Contractor Model and Settings API

**Files:**
- Modify: `app/db/contractors.py` — add default field
- Modify: `app/api/settings.py` — expose in settings update

- [ ] **Step 1: Add voice_engine default to contractor creation**

In `app/db/contractors.py`, find the `create_contractor` function where the default contractor document is built. Add `"voice_engine": "elevenlabs"` to the defaults dict.

- [ ] **Step 2: Add voice_engine to the settings update API**

In `app/api/settings.py`, find the settings update endpoint and add `voice_engine` to the allowed fields. Validate that the value is either `"elevenlabs"` or `"gemini"`.

```python
# In the settings update handler, add to the allowed fields:
if "voice_engine" in body:
    if body["voice_engine"] in ("elevenlabs", "gemini"):
        updates["voice_engine"] = body["voice_engine"]
```

- [ ] **Step 3: Verify syntax**

```bash
python3 -m py_compile app/db/contractors.py
python3 -m py_compile app/api/settings.py
echo "Compile OK"
```

- [ ] **Step 4: Commit**

```bash
git add app/db/contractors.py app/api/settings.py
git commit -m "feat: add voice_engine field to contractor model and settings API"
```

---

### Task 5: Set Test Contractor to Gemini and Deploy

**Files:**
- No code changes — Firestore update + deploy

- [ ] **Step 1: Deploy the backend**

```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin"
gcloud run deploy kevin-api --source . --project kevin-491315 --region us-central1 --allow-unauthenticated
```

- [ ] **Step 2: Set the test contractor's voice_engine to gemini**

Use a Python script or Firestore console to update:

```bash
python3 -c "
from app.db.firestore_client import get_firestore_client
db = get_firestore_client()
db.document('contractors/COgOeaSL4lbmuSvD7sOu').update({'voice_engine': 'gemini'})
print('Updated contractor to Gemini pipeline')
"
```

- [ ] **Step 3: Verify with a test call**

Call the Kevin number (+16504222677) from a test phone. Verify:
- Kevin greets the caller with the Gemini voice
- Caller speech is recognized and responded to
- Transcript appears in RTDB (check iOS app or Firebase console)
- Post-call SMS is sent after the call ends

Check server logs:
```bash
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="kevin-api" AND (jsonPayload.message=~"Gemini|gemini|pipeline")' --project kevin-491315 --limit=20 --format='value(timestamp, jsonPayload.message)' --freshness=15m
```

- [ ] **Step 4: If issues, revert to ElevenLabs**

```bash
python3 -c "
from app.db.firestore_client import get_firestore_client
db = get_firestore_client()
db.document('contractors/COgOeaSL4lbmuSvD7sOu').update({'voice_engine': 'elevenlabs'})
print('Reverted to ElevenLabs pipeline')
"
```

---

### Task 6: Delete Old Gemini Agent Prototype

**Files:**
- Delete: `app/services/gemini_agent.py`

- [ ] **Step 1: Verify nothing imports the old module**

```bash
grep -r "gemini_agent" app/ --include="*.py"
```

Expected: No results (the file is unused).

- [ ] **Step 2: Delete the old prototype**

```bash
rm app/services/gemini_agent.py
```

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "chore: remove deprecated gemini_agent.py (replaced by gemini_pipeline.py)"
```
