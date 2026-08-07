"""ConversationRelay session engine — the receptionist brain, text-in/text-out.

Twilio ConversationRelay owns the entire audio layer: Deepgram STT (multi-
language), ElevenLabs TTS, barge-in, and playback pacing. This module owns
only the conversation: Gemini *text* generation with tools, conversation
history, interruption truncation, urgency keywords, goodbye detection, and
iOS in-call commands. There is no audio code here by design — every failure
class we debugged in gemini_pipeline.py (premature turnComplete, audio token
caps, VAD phantom starts, playout pacing) lives on Twilio's side of the
ConversationRelay boundary.

Selection: contractor ``voice_engine == "relay"`` switches the screening
TwiML from <Connect><Stream> to <Connect><ConversationRelay> (see
twilio_incoming.py) and the session lands on /relay-stream instead of
/media-stream. Default remains the existing Gemini Live engine.
"""

import asyncio
import json
import time
from typing import Awaitable, Callable, Optional

import httpx

from app.config import settings
from app.services.entitlements import effective_mode
from app.services.quiet_hours import is_business_hours
from app.services.urgency import find_urgent_signal
from app.services.voice_pipeline import VoicePipeline, _call_label, build_system_prompt
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Text model for the relay brain comes from settings.relay_text_model —
# deliberately NOT the native-audio family: text models have none of the
# audio-generation truncation behavior and their function calling is the
# reliable kind. First live call (CA80d3fd) 404'd on the hardcoded
# gemini-2.5-flash: the model is listed by ListModels but gated ("no longer
# available to new users"), so the model id must stay configurable.

# Text tokens, not audio: 256 is several sentences. The prompt enforces
# "ONE or two short sentences"; this is a runaway guard, same philosophy as
# MAX_RESPONSE_OUTPUT_TOKENS in gemini_pipeline.py.
MAX_REPLY_TOKENS = 256

# Bound tool-call loops per caller turn (a tool round is generate -> execute
# -> generate). Two rounds covers check-availability + book.
MAX_TOOL_ROUNDS = 3

GENERATE_TIMEOUT_SECONDS = 20.0


def build_greeting_text(contractor_config: dict, after_hours: bool) -> str:
    """Deterministic greeting, spoken by Twilio via welcomeGreeting.

    Same wording rules as GeminiPipeline._build_greeting_text so the caller
    experience is identical across engines.
    """
    from app.services.gemini_pipeline import GeminiPipeline

    config = contractor_config or {}
    business_name = config.get(
        "business_name",
        f"{config.get('owner_name', settings.user_name)}'s office",
    )
    business_name = " ".join(
        str(business_name).split()[: GeminiPipeline.MAX_GREETING_BUSINESS_NAME_WORDS]
    ) or "the office"
    owner_name = config.get("owner_name", settings.user_name)
    owner_parts = owner_name.split()
    owner_first = owner_parts[0] if owner_parts else "the owner"
    mode = config.get("effective_mode") or effective_mode(config)

    if mode == "personal":
        return f"Hi, this is Kevin, {owner_first}'s assistant. How can I help?"
    if after_hours:
        return f"{business_name} is currently closed. My name is Kevin. How can I help?"
    return (
        f"Hi, thank you for calling {business_name}. My name is Kevin. "
        "How can I help you?"
    )


class RelayPipeline:
    """One ConversationRelay session: history, generation, tools, commands."""

    def __init__(
        self,
        *,
        contractor_config: dict,
        call_sid: str = "",
        caller_phone: str = "",
        send_to_twilio: Callable[[dict], Awaitable[None]],
        on_transcript: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_urgency_detected: Optional[Callable[[str], Awaitable[None]]] = None,
        on_call_complete: Optional[Callable[[], Awaitable[None]]] = None,
        stream_generate: Optional[Callable] = None,
    ):
        self._contractor_config = contractor_config or {}
        self._call_sid = call_sid
        self._caller_phone = caller_phone
        self._send = send_to_twilio
        self._on_transcript = on_transcript
        self._on_urgency_detected = on_urgency_detected
        self._on_call_complete = on_call_complete
        # Injectable LLM transport for tests; defaults to the real SSE stream.
        self._stream_generate = stream_generate or self._stream_generate_httpx

        mode = self._contractor_config.get("effective_mode") or effective_mode(
            self._contractor_config
        )
        if mode == "personal":
            self._after_hours = False
        else:
            self._after_hours = not is_business_hours(self._contractor_config)

        self._system_prompt = build_system_prompt(
            self._contractor_config,
            after_hours=self._after_hours,
            caller_phone=caller_phone,
        )
        self.greeting_text = build_greeting_text(self._contractor_config, self._after_hours)

        self._history: list[dict] = []
        self._language = "en"
        self._active = True
        self._generating = False
        self._urgency_signalled = False
        self._unavailable_said = False
        self._ending = False
        self._command_task: Optional[asyncio.Task] = None
        self._tools = self._build_tools()

        # The welcome greeting is spoken by Twilio before any prompt arrives;
        # seed history so the model knows the call did not start cold.
        self._history.append(
            {"role": "model", "parts": [{"text": self.greeting_text}]}
        )

    # --- lifecycle -------------------------------------------------------

    def start_background_tasks(self) -> None:
        if self._call_sid and (self._command_task is None or self._command_task.done()):
            self._command_task = asyncio.create_task(self._command_check_loop())

    async def stop(self) -> None:
        self._active = False
        if self._command_task and not self._command_task.done():
            self._command_task.cancel()

    @property
    def language(self) -> str:
        return self._language

    # --- inbound events from ConversationRelay ---------------------------

    async def handle_message(self, message: dict) -> None:
        """Dispatch one decoded ConversationRelay WebSocket message."""
        msg_type = message.get("type", "")
        if msg_type == "prompt":
            await self._handle_prompt(message)
        elif msg_type == "interrupt":
            self._handle_interrupt(message)
        elif msg_type == "error":
            logger.warning(
                "relay_event event=twilio_error call=%s description=%s",
                _call_label(self._call_sid),
                str(message.get("description", ""))[:200],
            )
        # setup is consumed by the webhook layer; dtmf is not enabled.

    async def _handle_prompt(self, message: dict) -> None:
        # Interim results arrive with last=false; only complete utterances
        # drive a turn. ConversationRelay owns endpointing.
        if not message.get("last", False):
            return
        text = (message.get("voicePrompt") or "").strip()
        if not text:
            return
        lang = message.get("lang") or ""
        if lang:
            self._language = lang.split("-")[0]

        if self._on_transcript:
            await self._on_transcript("Caller", text)

        if not self._urgency_signalled and find_urgent_signal(text):
            self._urgency_signalled = True
            if self._on_urgency_detected:
                await self._on_urgency_detected(text)

        self._history.append({"role": "user", "parts": [{"text": text}]})
        await self._generate_reply()

    def _handle_interrupt(self, message: dict) -> None:
        """Caller barged in: keep only what was actually spoken in history."""
        spoken = (message.get("utteranceUntilInterrupt") or "").strip()
        for entry in reversed(self._history):
            if entry.get("role") == "model":
                parts = entry.get("parts", [])
                if parts and "text" in parts[0]:
                    parts[0]["text"] = spoken or parts[0]["text"]
                break
        logger.info(
            "relay_event event=caller_interrupt call=%s", _call_label(self._call_sid)
        )

    # --- generation ------------------------------------------------------

    async def _generate_reply(self, extra_instruction: str = "") -> None:
        if self._generating or self._ending:
            return
        self._generating = True
        try:
            contents = list(self._history)
            if extra_instruction:
                contents.append(
                    {"role": "user", "parts": [{"text": extra_instruction}]}
                )

            reply_text = ""
            for _round in range(MAX_TOOL_ROUNDS):
                started_at = time.monotonic()
                text_out, function_calls, raw_parts = await self._run_stream(contents)
                reply_text += text_out
                logger.info(
                    "relay_event event=reply_generated call=%s ms=%d tool_calls=%d",
                    _call_label(self._call_sid),
                    int((time.monotonic() - started_at) * 1000),
                    len(function_calls),
                )
                if not function_calls:
                    break
                # The model turn must be echoed back with its parts VERBATIM:
                # Gemini 3.x functionCall parts carry a thoughtSignature the
                # API requires on replay — rebuilding the part from just the
                # functionCall drops it and every tool round 400s ("Function
                # call is missing a thought_signature"), observed live on
                # call CAcae04f. raw_parts preserves signatures and ids.
                function_response_parts = []
                for fc in function_calls:
                    response_payload = await self._execute_tool(fc)
                    function_response = {
                        "name": fc.get("name", ""),
                        "response": response_payload,
                    }
                    if fc.get("id"):
                        function_response["id"] = fc["id"]
                    function_response_parts.append(
                        {"functionResponse": function_response}
                    )
                contents = contents + [
                    {"role": "model", "parts": raw_parts},
                    {"role": "user", "parts": function_response_parts},
                ]

            # Close the TTS turn even if the model produced no text.
            await self._send({"type": "text", "token": "", "last": True})

            if reply_text:
                self._history.append(
                    {"role": "model", "parts": [{"text": reply_text}]}
                )
                if self._on_transcript:
                    await self._on_transcript("Kevin", reply_text)
                await self._maybe_end_on_goodbye(reply_text)
        except Exception as error:
            logger.error(
                "relay_event event=generate_error call=%s type=%s",
                _call_label(self._call_sid),
                type(error).__name__,
            )
            # Never leave the caller in silence: degrade with a short apology.
            await self._send(
                {
                    "type": "text",
                    "token": "I'm sorry, I'm having a little trouble. Could you say that again?",
                    "last": True,
                }
            )
        finally:
            self._generating = False

    async def _run_stream(
        self, contents: list[dict]
    ) -> tuple[str, list[dict], list[dict]]:
        """Run one streaming generate; forward text tokens as they arrive.

        Returns (concatenated_text, function_calls, raw_parts). raw_parts is
        the model turn as the API delivered it — consecutive unsigned text
        deltas merged, every part carrying a thoughtSignature (and every
        functionCall part) preserved verbatim for history replay.
        """
        text_out = ""
        function_calls: list[dict] = []
        raw_parts: list[dict] = []
        async for part in self._stream_generate(contents):
            if "text" in part and part["text"]:
                text_out += part["text"]
                await self._send(
                    {"type": "text", "token": part["text"], "last": False}
                )
                if "thoughtSignature" in part:
                    raw_parts.append(dict(part))
                elif (
                    raw_parts
                    and "text" in raw_parts[-1]
                    and "thoughtSignature" not in raw_parts[-1]
                ):
                    raw_parts[-1]["text"] += part["text"]
                else:
                    raw_parts.append({"text": part["text"]})
            elif "functionCall" in part:
                function_calls.append(part["functionCall"])
                raw_parts.append(dict(part))
        return text_out, function_calls, raw_parts

    def _build_generate_body(self, contents: list[dict]) -> dict:
        body: dict = {
            "system_instruction": {"parts": [{"text": self._system_prompt}]},
            "contents": contents,
            "generationConfig": {
                "temperature": settings.gemini_live_temperature,
                "maxOutputTokens": MAX_REPLY_TOKENS,
                # Current flash models think by default and will burn the
                # entire token budget on thoughts, returning empty text
                # (observed live on gemini-3.5-flash). A receptionist needs
                # the first token, not deliberation — same setting the Live
                # engine pins via gemini_live_thinking_budget.
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        if self._tools:
            body["tools"] = self._tools
        return body

    async def _stream_generate_httpx(self, contents: list[dict]):
        """Real transport: Gemini streamGenerateContent over SSE."""
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{settings.relay_text_model}:streamGenerateContent?alt=sse"
        )
        body = self._build_generate_body(contents)

        async with httpx.AsyncClient(timeout=GENERATE_TIMEOUT_SECONDS) as client:
            async with client.stream(
                "POST",
                url,
                headers={"x-goog-api-key": settings.gemini_api_key},
                json=body,
            ) as response:
                if response.status_code >= 400:
                    # Google error bodies name the exact contract violation
                    # (e.g. "missing a thought_signature") — without this the
                    # log shows only HTTPStatusError and the cause needs a
                    # live reproduction to find.
                    error_body = await response.aread()
                    logger.error(
                        "relay_event event=llm_http_error call=%s status=%d body=%s",
                        _call_label(self._call_sid),
                        response.status_code,
                        error_body.decode(errors="replace")[:300],
                    )
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    candidates = chunk.get("candidates") or []
                    if not candidates:
                        continue
                    for part in (candidates[0].get("content") or {}).get("parts", []):
                        yield part

    # --- tools -----------------------------------------------------------

    def _build_tools(self) -> list:
        """Reuse the Gemini-format tool declarations from the live engine."""
        from app.services.gemini_pipeline import GeminiPipeline

        borrowed = GeminiPipeline.__new__(GeminiPipeline)
        borrowed._contractor_config = self._contractor_config
        borrowed._log_voice_timing = lambda *args, **kwargs: None
        try:
            return borrowed._build_gemini_tools()
        except Exception as error:
            logger.error(
                "relay_event event=tool_build_error call=%s type=%s",
                _call_label(self._call_sid),
                type(error).__name__,
            )
            return []

    async def _execute_tool(self, function_call: dict) -> dict:
        """Execute one tool via the shared VoicePipeline implementation."""
        tool_name = function_call.get("name", "")
        tool_args = function_call.get("args", {}) or {}
        borrowed = VoicePipeline.__new__(VoicePipeline)
        borrowed._contractor_config = self._contractor_config
        borrowed._call_sid = self._call_sid
        logger.info(
            "relay_event event=tool_call call=%s tool=%s",
            _call_label(self._call_sid),
            tool_name[:40],
        )
        try:
            result_str = await asyncio.wait_for(
                borrowed._execute_tool(tool_name, tool_args),
                timeout=GENERATE_TIMEOUT_SECONDS,
            )
            return json.loads(result_str)
        except Exception as error:
            logger.error(
                "relay_event event=tool_error call=%s tool=%s type=%s",
                _call_label(self._call_sid),
                tool_name[:40],
                type(error).__name__,
            )
            return {"error": "The tool is unavailable right now."}

    # --- goodbye / end ---------------------------------------------------

    async def _maybe_end_on_goodbye(self, reply_text: str) -> None:
        from app.services.gemini_pipeline import GeminiPipeline

        lowered = reply_text.lower()
        if not any(phrase in lowered for phrase in GeminiPipeline.GOODBYE_PHRASES):
            return
        self._ending = True
        logger.info(
            "relay_event event=goodbye_detected call=%s", _call_label(self._call_sid)
        )
        # Twilio is still speaking the goodbye; give TTS time to play out
        # before tearing the session down. tokens-played events would make
        # this exact — revisit once the events attribute is enabled.
        await asyncio.sleep(4)
        await self.end_call()

    async def end_call(self) -> None:
        try:
            await self._send({"type": "end"})
        except Exception:
            pass
        if self._on_call_complete:
            await self._on_call_complete()

    # --- iOS commands (decline / take_message) ---------------------------

    async def _command_check_loop(self):
        try:
            while self._active:
                await self._check_commands()
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    async def _check_commands(self):
        if not self._call_sid:
            return
        try:
            from app.db.cache import _init_firebase
            from firebase_admin import db as rtdb

            _init_firebase()
            ref = rtdb.reference(f"/call_commands/{self._call_sid}")
            loop = asyncio.get_event_loop()
            command = await loop.run_in_executor(None, ref.get)
            if not command:
                return
            await loop.run_in_executor(None, ref.delete)
            if command.get("type") == "take_message" and not self._unavailable_said:
                self._unavailable_said = True
                owner_name = self._contractor_config.get(
                    "owner_name", settings.user_name
                )
                await self._generate_reply(
                    extra_instruction=(
                        f"SYSTEM INSTRUCTION: The owner ({owner_name}) has declined "
                        "the call. Tell the caller they are unavailable and offer to "
                        "take a message. Be warm and apologetic."
                    )
                )
        except Exception as error:
            logger.error(
                "relay_event event=command_check_error call=%s type=%s",
                _call_label(self._call_sid),
                type(error).__name__,
            )
