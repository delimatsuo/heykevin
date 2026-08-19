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
from app.services.voice_pipeline import (
    GOOGLE_CALENDAR_TOOL_TIMEOUT_SECONDS,
    VoicePipeline,
    _call_label,
    build_system_prompt,
    is_owner_availability_hold,
)
from app.services.receptionist_context import build_greeting_text as _shared_greeting_text
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
TOOL_DISPATCH_TIMEOUT_SECONDS = GOOGLE_CALENDAR_TOOL_TIMEOUT_SECONDS + 2.0


def build_greeting_text(contractor_config: dict, after_hours: bool) -> str:
    """Deterministic greeting, spoken by Twilio via welcomeGreeting.

    Same wording rules as GeminiPipeline._build_greeting_text so the caller
    experience is identical across engines.
    """
    return _shared_greeting_text(contractor_config, after_hours)


class RelayPipeline:
    """One ConversationRelay session: history, generation, tools, commands."""

    # Goodbye teardown, in polls of PLAYBACK_POLL_SECONDS. See _await_playout.
    PLAYBACK_POLL_SECONDS = 0.25
    # Receipts during one utterance were 1.5-6.6s apart on CA0438f3, so
    # quiescence is a weak signal and this window has to clear the widest
    # observed gap. It is the fallback, not the primary path.
    PLAYBACK_QUIET_POLLS = 32     # 8.0s of silence after the last receipt
    PLAYBACK_FALLBACK_POLLS = 16  # 4.0s when no receipts ever arrive
    PLAYBACK_MAX_POLLS = 60       # 15s hard cap
    # Speech is "done" at 90% of the characters we streamed: TTS may not
    # voice trailing punctuation, and waiting on an exact match would stall.
    PLAYBACK_COMPLETE_RATIO = 0.9

    # "Let me see if Deli is available" arms this; at timeout Kevin returns
    # with the unavailability message. Same 30s the other engines use.
    OWNER_AVAILABILITY_TIMEOUT_SECONDS = 30.0

    # Dead-air guard. Activity = caller prompts (including interim
    # fragments), interrupts, playback receipts, and finished replies — so
    # the clock only runs when both sides are truly quiet. Kevin nudges once,
    # then says goodbye, then the hard stop covers a failed goodbye.
    SILENCE_CHECK_INTERVAL_SECONDS = 2.0
    CALLER_SILENCE_PROMPT_SECONDS = 15.0
    CALLER_SILENCE_HANGUP_SECONDS = 15.0
    SILENCE_FORCED_END_SECONDS = 30.0

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
        spoken_greeting: str = "",
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
        # ConversationRelay already spoke this exact value before opening the
        # WebSocket. Keep model history aligned even if memory changes between
        # the pre-TwiML lookup and authenticated stream setup.
        self.greeting_text = (
            spoken_greeting
            if isinstance(spoken_greeting, str) and 0 < len(spoken_greeting) <= 300
            else build_greeting_text(self._contractor_config, self._after_hours)
        )

        self._history: list[dict] = []
        self._language = "en"
        self._active = True
        # Turn epoch gates every outbound token: superseding caller speech or
        # a barge-in bumps it, and any in-flight generation stops sending the
        # instant its epoch is stale. Without this, tokens from a cancelled
        # turn keep arriving at Twilio and TTS resumes mid-thought after the
        # caller spoke — heard live on CA73dbd1 as audio "breaking up" and
        # Kevin answering the wrong (older) utterance.
        self._turn_epoch = 0
        self._generate_task: Optional[asyncio.Task] = None
        self._streamed_text = ""
        self._urgency_signalled = False
        self._unavailable_said = False
        self._ending = False
        self._playback_receipts = 0
        self._played_chars_sum = 0
        self._played_chars_max = 0
        self._hold_task: Optional[asyncio.Task] = None
        self._silence_task: Optional[asyncio.Task] = None
        self._last_activity = time.monotonic()
        self._silence_nudged = False
        self._nudged_monotonic = 0.0
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
        if self._silence_task is None or self._silence_task.done():
            self._silence_task = asyncio.create_task(self._silence_watchdog_loop())

    async def stop(self) -> None:
        self._active = False
        self._turn_epoch += 1
        for task in (self._generate_task, self._command_task, self._hold_task, self._silence_task):
            if task and not task.done():
                task.cancel()

    async def wait_idle(self) -> None:
        """Await the in-flight generation, if any (tests and shutdown)."""
        task = self._generate_task
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass

    @property
    def language(self) -> str:
        return self._language

    # --- inbound events from ConversationRelay ---------------------------

    async def handle_message(self, message: dict) -> None:
        """Dispatch one decoded ConversationRelay WebSocket message.

        Must never block on generation: prompts and interrupts arriving while
        a reply is being generated are what cancel it, so this dispatcher has
        to stay responsive. Generation runs as a task (_start_generation).
        """
        msg_type = message.get("type", "")
        # Any inbound traffic — caller speech (even interim fragments),
        # barge-ins, playback receipts — means the call is alive.
        self._last_activity = time.monotonic()
        if msg_type == "prompt":
            self._silence_nudged = False  # the caller is talking
            await self._handle_prompt(message)
        elif msg_type == "interrupt":
            self._silence_nudged = False
            await self._handle_interrupt(message)
        elif msg_type == "error":
            logger.warning(
                "relay_event event=twilio_error call=%s description=%s",
                _call_label(self._call_sid),
                str(message.get("description", ""))[:200],
            )
        elif msg_type not in ("setup", "dtmf"):
            # Playback receipt from events="tokens-played". Twilio documents
            # the subscription but not the message body, so we deliberately
            # read nothing out of it: its ARRIVAL is the signal that audio is
            # still playing, which is all the goodbye teardown needs. `value`
            # can carry spoken text, so it is never logged.
            self._playback_receipts += 1
            # When `value` carries the played text we can measure progress
            # against what we streamed, which beats inferring completion from
            # silence. Both encodings are covered without knowing which
            # Twilio uses: sum for per-fragment receipts, max for cumulative
            # ones. Only lengths are read — `value` is spoken content and is
            # never logged.
            value = message.get("value")
            if isinstance(value, str) and value:
                self._played_chars_sum += len(value)
                self._played_chars_max = max(self._played_chars_max, len(value))
            logger.info(
                "relay_event event=playback_receipt call=%s type=%s name=%s keys=%s value_chars=%d",
                _call_label(self._call_sid),
                str(msg_type)[:40],
                str(message.get("name", ""))[:40],
                ",".join(sorted(str(k)[:20] for k in message.keys()))[:120],
                len(value) if isinstance(value, str) else -1,
            )

    async def _handle_prompt(self, message: dict) -> None:
        last = bool(message.get("last", False))
        text = (message.get("voicePrompt") or "").strip()
        logger.info(
            "relay_event event=prompt_received call=%s last=%s chars=%d",
            _call_label(self._call_sid),
            last,
            len(text),
        )
        # Interim results arrive with last=false; only complete utterances
        # drive a turn. ConversationRelay owns endpointing.
        if not last or not text:
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

        # Caller speech supersedes any reply still being generated: record
        # what was actually spoken of it, then answer the fuller history.
        await self._supersede_in_flight()
        self._history.append({"role": "user", "parts": [{"text": text}]})
        self._start_generation()

    async def _handle_interrupt(self, message: dict) -> None:
        """Caller barged in: stop generating and keep only what was spoken."""
        spoken = (message.get("utteranceUntilInterrupt") or "").strip()
        in_flight = self._generate_task and not self._generate_task.done()
        partial = self._streamed_text
        self._cancel_generation()
        if in_flight and partial:
            # The interrupted turn never reached history — record the spoken
            # portion so the model knows where it was cut off.
            self._history.append(
                {"role": "model", "parts": [{"text": spoken or partial}]}
            )
            if self._on_transcript:
                await self._on_transcript("Kevin", spoken or partial)
        else:
            for entry in reversed(self._history):
                if entry.get("role") == "model":
                    parts = entry.get("parts", [])
                    if parts and "text" in parts[0]:
                        parts[0]["text"] = spoken or parts[0]["text"]
                    break
        logger.info(
            "relay_event event=caller_interrupt call=%s in_flight=%s",
            _call_label(self._call_sid),
            bool(in_flight),
        )

    # --- generation lifecycle --------------------------------------------

    def _cancel_generation(self) -> None:
        """Invalidate the current epoch and cancel any in-flight generation."""
        self._turn_epoch += 1
        task = self._generate_task
        if task and not task.done():
            task.cancel()
        self._generate_task = None
        self._streamed_text = ""

    async def _supersede_in_flight(self) -> None:
        """Cancel an in-flight reply, preserving its spoken partial in history."""
        in_flight = self._generate_task and not self._generate_task.done()
        partial = self._streamed_text
        self._cancel_generation()
        if in_flight and partial:
            self._history.append({"role": "model", "parts": [{"text": partial}]})
            if self._on_transcript:
                await self._on_transcript("Kevin", partial)

    def _start_generation(self, extra_instruction: str = "") -> None:
        if self._ending:
            return
        self._turn_epoch += 1
        epoch = self._turn_epoch
        self._streamed_text = ""
        self._generate_task = asyncio.create_task(
            self._generate_reply(epoch, extra_instruction)
        )

    # --- generation ------------------------------------------------------

    async def _send_current(self, epoch: int, message: dict) -> None:
        """Send to Twilio only while this generation's epoch is still live."""
        if epoch != self._turn_epoch:
            raise asyncio.CancelledError()
        if message.get("type") == "text" and message.get("token"):
            self._streamed_text += message["token"]
        await self._send(message)

    async def _generate_reply(self, epoch: int, extra_instruction: str = "") -> None:
        if self._ending:
            return
        try:
            contents = list(self._history)
            if extra_instruction:
                contents.append(
                    {"role": "user", "parts": [{"text": extra_instruction}]}
                )

            reply_text = ""
            for _round in range(MAX_TOOL_ROUNDS):
                started_at = time.monotonic()
                text_out, function_calls, raw_parts = await self._run_stream(
                    epoch, contents
                )
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
            await self._send_current(epoch, {"type": "text", "token": "", "last": True})

            if reply_text:
                self._history.append(
                    {"role": "model", "parts": [{"text": reply_text}]}
                )
                self._streamed_text = ""
                self._last_activity = time.monotonic()
                if self._on_transcript:
                    await self._on_transcript("Kevin", reply_text)
                self._maybe_start_owner_hold(reply_text)
                await self._maybe_end_on_goodbye(epoch, reply_text)
        except asyncio.CancelledError:
            # Superseded by newer caller speech or a barge-in. The successor
            # turn owns the channel now — no apology, no closing token.
            raise
        except Exception as error:
            logger.error(
                "relay_event event=generate_error call=%s type=%s",
                _call_label(self._call_sid),
                type(error).__name__,
            )
            if epoch != self._turn_epoch:
                return
            # Never leave the caller in silence: degrade with a short apology.
            await self._send(
                {
                    "type": "text",
                    "token": "I'm sorry, I'm having a little trouble. Could you say that again?",
                    "last": True,
                }
            )

    async def _run_stream(
        self, epoch: int, contents: list[dict]
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
                await self._send_current(
                    epoch, {"type": "text", "token": part["text"], "last": False}
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
        borrowed._caller_phone = self._caller_phone
        if hasattr(self, "_receptionist_tool_executor"):
            borrowed._receptionist_tool_executor = self._receptionist_tool_executor
        logger.info(
            "relay_event event=tool_call call=%s tool=%s",
            _call_label(self._call_sid),
            tool_name[:40],
        )
        try:
            from app.services.receptionist_tools import RECEPTIONIST_TOOL_NAMES

            execution_kwargs = (
                {"operation_id": str(function_call.get("id", ""))}
                if tool_name in RECEPTIONIST_TOOL_NAMES
                else {}
            )
            result_str = await asyncio.wait_for(
                borrowed._execute_tool(
                    tool_name,
                    tool_args,
                    **execution_kwargs,
                ),
                timeout=TOOL_DISPATCH_TIMEOUT_SECONDS,
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

    # --- owner-availability hold -----------------------------------------

    def _maybe_start_owner_hold(self, reply_text: str) -> None:
        """Arm the 30s unavailability return when Kevin puts the caller on hold.

        The prompt tells Kevin to say "let me see if <owner> is available"
        and then stay silent. Both older engines detect that phrase and come
        back after 30s; this engine didn't, so on CAa5e0de the caller sat in
        dead air until Twilio killed the session 4m40s later.
        """
        if self._ending or self._unavailable_said:
            return
        if not is_owner_availability_hold(reply_text):
            return
        if self._hold_task and not self._hold_task.done():
            self._hold_task.cancel()
        self._hold_task = asyncio.create_task(self._owner_hold_timer())
        logger.info(
            "relay_event event=owner_hold_started call=%s",
            _call_label(self._call_sid),
        )

    async def _owner_hold_timer(self) -> None:
        await asyncio.sleep(self.OWNER_AVAILABILITY_TIMEOUT_SECONDS)
        if not self._active or self._ending or self._unavailable_said:
            return
        # Owner pickup redirects the call and stops the pipeline, so reaching
        # this point means they did not take it.
        self._unavailable_said = True
        owner_name = self._contractor_config.get("owner_name", settings.user_name)
        pronoun = self._contractor_config.get("pronoun", "he")
        logger.info(
            "relay_event event=owner_hold_timeout call=%s",
            _call_label(self._call_sid),
        )
        await self._supersede_in_flight()
        self._start_generation(
            extra_instruction=(
                f"SYSTEM INSTRUCTION: {owner_name} has not picked up. Tell the "
                f"caller {owner_name} is not available right now, apologize "
                f"warmly, and offer to take a message and make sure {pronoun} "
                "gets it."
            )
        )

    # --- caller-silence watchdog ------------------------------------------

    async def _silence_watchdog_loop(self) -> None:
        """Never let the line dangle in silence.

        Nudge once when both sides have been quiet, say goodbye if the
        silence continues, and force the hangup if even the goodbye reply
        fails to end the call. Suspended while an owner-availability hold is
        open — the caller was told to wait, so that silence is expected and
        belongs to the hold timer.
        """
        goodbye_injected = False
        goodbye_monotonic = 0.0
        try:
            while self._active and not self._ending:
                await asyncio.sleep(self.SILENCE_CHECK_INTERVAL_SECONDS)
                if not self._active or self._ending:
                    return
                if self._hold_task and not self._hold_task.done():
                    continue
                if self._generate_task and not self._generate_task.done():
                    continue
                idle = time.monotonic() - self._last_activity
                if idle < self.CALLER_SILENCE_PROMPT_SECONDS:
                    # _silence_nudged clears only on caller speech, so fresh
                    # Kevin-side activity (the nudge itself playing out) does
                    # not restart the escalation from scratch.
                    if not self._silence_nudged:
                        goodbye_injected = False
                    continue
                if not self._silence_nudged:
                    self._silence_nudged = True
                    self._nudged_monotonic = time.monotonic()
                    logger.info(
                        "relay_event event=silence_nudge call=%s",
                        _call_label(self._call_sid),
                    )
                    self._start_generation(
                        extra_instruction=(
                            "SYSTEM INSTRUCTION: The caller has said nothing "
                            "for a while. Gently ask if they are still there. "
                            "One short sentence."
                        )
                    )
                elif not goodbye_injected:
                    if (
                        time.monotonic() - self._nudged_monotonic
                        >= self.CALLER_SILENCE_HANGUP_SECONDS
                    ):
                        goodbye_injected = True
                        goodbye_monotonic = time.monotonic()
                        logger.info(
                            "relay_event event=silence_goodbye call=%s",
                            _call_label(self._call_sid),
                        )
                        self._start_generation(
                            extra_instruction=(
                                "SYSTEM INSTRUCTION: The caller appears to "
                                "have left. Say one short, warm goodbye and "
                                "include the word 'goodbye'."
                            )
                        )
                elif (
                    time.monotonic() - goodbye_monotonic
                    >= self.SILENCE_FORCED_END_SECONDS
                ):
                    # The goodbye reply should have triggered the normal
                    # teardown; if it didn't, close the line anyway.
                    logger.warning(
                        "relay_event event=silence_forced_end call=%s",
                        _call_label(self._call_sid),
                    )
                    self._ending = True
                    await self.end_call()
                    return
        except asyncio.CancelledError:
            pass

    # --- goodbye / end ---------------------------------------------------

    async def _maybe_end_on_goodbye(self, epoch: int, reply_text: str) -> None:
        from app.services.gemini_pipeline import GeminiPipeline

        lowered = reply_text.lower()
        if not any(phrase in lowered for phrase in GeminiPipeline.GOODBYE_PHRASES):
            return
        logger.info(
            "relay_event event=goodbye_detected call=%s", _call_label(self._call_sid)
        )
        if not await self._await_playout(epoch, reply_text):
            return
        self._ending = True
        await self.end_call()

    async def _await_playout(self, epoch: int, spoken_text: str = "") -> bool:
        """Wait for Twilio to finish speaking. False means the caller spoke.

        Two earlier attempts both hung up mid-goodbye. A fixed 4s grace ended
        the call on a timer (CA40a9f4: `end` at 20:25:40.7, receipt at
        20:25:41.1). Replacing it with 1.5s of receipt silence was no better
        (CA0438f3: teardown ~21:05:46.2, receipt at 21:05:46.71) because
        receipts within a single utterance are 1.5-6.6s apart, so the quiet
        window expires *between* receipts while audio is still playing.

        Silence is therefore a weak signal. The strong one is arithmetic: we
        know how many characters we streamed for this turn, and when the
        receipt carries the played text we can count characters played and
        stop the moment they account for what we sent. Twilio documents the
        `events="tokens-played"` subscription but not the receipt body, so
        both plausible encodings are handled — summed fragments and a
        cumulative string — and only lengths are read, never content.

        Everything else is a bound, not a signal: a quiet window wide enough
        to clear the largest observed inter-receipt gap, a 4s grace for
        contractors whose TwiML predates the events attribute and will never
        send a receipt, a hard cap, and a per-poll epoch check so "wait, one
        more thing" still cancels the hangup.
        """
        self._played_chars_sum = 0
        self._played_chars_max = 0
        target = len(spoken_text or "")
        seen = self._playback_receipts
        heard_playback = False
        quiet_polls = 0
        reason = "cap"

        for poll in range(self.PLAYBACK_MAX_POLLS):
            await asyncio.sleep(self.PLAYBACK_POLL_SECONDS)
            if epoch != self._turn_epoch:
                return False

            played = max(self._played_chars_sum, self._played_chars_max)
            if target and played >= target * self.PLAYBACK_COMPLETE_RATIO:
                reason = "measured"
                break

            if self._playback_receipts > seen:
                seen = self._playback_receipts
                heard_playback = True
                quiet_polls = 0
                continue

            quiet_polls += 1
            if heard_playback:
                if quiet_polls >= self.PLAYBACK_QUIET_POLLS:
                    reason = "quiet"
                    break
            elif poll + 1 >= self.PLAYBACK_FALLBACK_POLLS:
                reason = "no_receipts"
                break

        logger.info(
            "relay_event event=playout_done call=%s via=%s played_chars=%d target_chars=%d",
            _call_label(self._call_sid),
            reason,
            max(self._played_chars_sum, self._played_chars_max),
            target,
        )
        return True

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
                await self._supersede_in_flight()
                self._start_generation(
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
