"""Dedicated Gemini Live adapter for the stateless public phone demo.

This adapter intentionally subclasses only the audio transport implementation. It owns
its prompt and tool surface, passes no caller or call identifier into the inherited
pipeline, disables owner/RTDB behavior, and clears transient transcript context at stop.
"""

from __future__ import annotations

import asyncio
import json

from app.services.gemini_pipeline import GeminiPipeline
from app.services.public_demo import (
    build_public_demo_profile,
    build_public_demo_system_prompt,
    execute_public_demo_tool,
)

PUBLIC_DEMO_GEMINI_TOOLS = [
    {
        "function_declarations": [
            {
                "name": "check_availability",
                "description": (
                    "Return synthetic appointment windows for the fictional demo. "
                    "No real calendar is read."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "days_ahead": {
                            "type": "INTEGER",
                            "description": "Demo days to consider (default 7, max 14)",
                        }
                    },
                },
            },
            {
                "name": "book_appointment",
                "description": (
                    "Simulate an appointment request for the fictional demo. "
                    "Never creates or reserves anything."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {"type": "STRING"},
                        "start_time": {"type": "STRING"},
                        "end_time": {"type": "STRING"},
                        "description": {"type": "STRING"},
                    },
                    "required": ["title", "start_time", "end_time"],
                },
            },
        ]
    }
]

# The public demo has its own spoken identity. Keep this choice local to the
# demo so product users retain their configured language/persona voices.
# Google's voice catalogue identifies Achird as male and friendly.
PUBLIC_DEMO_FRIENDLY_MALE_VOICE = "Achird"
PUBLIC_DEMO_GEMINI_MODEL = "gemini-3.1-flash-live-preview"


class PublicDemoGeminiPipeline(GeminiPipeline):
    """Gemini transport with a fixed, no-side-effect public-demo policy."""

    MAX_RECONNECT_ATTEMPTS = 0
    # Twilio buffers outbound media in order and reports actual playback with
    # marks. Sending Gemini chunks as they arrive lets that jitter buffer absorb
    # provider timing variation instead of reproducing it on the phone line.
    PACE_AUDIO_OUTPUT = False
    # Hold only the beginning of each response so Gemini can build an audio
    # cushion before Twilio starts playback. This smooths sub-second provider
    # chunk gaps without restoring the old multi-second local pacing delay.
    AUDIO_START_BUFFER_SECONDS = 0.8
    # The public PSTN demo must reliably detect repeated caller turns. Keep the
    # longer prefix padding that rejects brief line noise, but do not inherit
    # the tenant pipeline's conservative speech-start threshold.
    REALTIME_START_OF_SPEECH_SENSITIVITY = "START_SENSITIVITY_HIGH"
    # A real caller paused for just over ten seconds after Kevin's answer. The
    # generic watchdog injected "Are you still there?" immediately before the
    # caller resumed, making the two turns compete in Gemini. Give demo callers
    # enough time to think or read a price before nudging them.
    CALLER_SILENCE_PROMPT_SECONDS = 20

    def __init__(self, *args, **kwargs):
        if args:
            raise TypeError("public demo callbacks must be passed by name")
        forbidden = {"contractor_config", "caller_phone", "call_sid"} & set(kwargs)
        if forbidden:
            raise TypeError("public demo identity and profile are code-owned")

        profile = build_public_demo_profile()
        super().__init__(
            **kwargs,
            contractor_config=profile,
            caller_phone="",
            call_sid="",
        )
        self._model = PUBLIC_DEMO_GEMINI_MODEL
        self._voice = PUBLIC_DEMO_FRIENDLY_MALE_VOICE
        self._system_prompt = build_public_demo_system_prompt(profile)

    def _build_greeting_text(self) -> str:
        return (
            "Thanks for calling Hey Kevin's Boston Plumbing demo. I'm Kevin, the AI "
            "receptionist. You can ask about services, the areas we cover, example "
            "pricing, or try booking a visit. What can I help with today?"
        )

    def _build_gemini_tools(self) -> list:
        """Expose only deterministic, pure demo tools."""

        return PUBLIC_DEMO_GEMINI_TOOLS

    def _start_owner_availability_wait(self) -> None:
        """Structurally disable inherited owner-transfer/message behavior."""

    def _build_reconnect_context(self, limit: int = 12) -> str:
        """Never replay caller transcript context into another provider session."""

        del limit
        return ""

    async def _check_commands(self) -> None:
        """The public demo has no RTDB command channel."""

    async def _handle_tool_calls(
        self,
        function_calls: list,
        *,
        websocket=None,
        tool_epoch: int | None = None,
    ) -> None:
        """Execute only pure synthetic tools and return payload-safe results."""

        websocket = websocket or self._ws
        if websocket is None:
            return

        responses = []
        for function_call in function_calls[:4]:
            if not isinstance(function_call, dict):
                continue
            name = function_call.get("name", "")
            args = function_call.get("args", {})
            call_id = function_call.get("id", "")
            result = execute_public_demo_tool(name, args)
            responses.append(
                {
                    "id": call_id if isinstance(call_id, str) else "",
                    "name": name if isinstance(name, str) else "",
                    "response": result,
                }
            )

        if tool_epoch is not None and (
            tool_epoch != self._tool_epoch
            or websocket is not self._ws
            or not self._connected
            or self._reconnecting
        ):
            self._log_voice_timing("tool_result_discarded", reason="stale_epoch")
            return

        await websocket.send(
            json.dumps(
                {
                    "tool_response": {
                        "function_responses": responses,
                    }
                }
            )
        )

    def enforce_deadline(self) -> None:
        """Synchronously disable further provider/media work before async teardown."""

        self._connected = False
        self._audio_input_ready.set()
        self._interrupt_speaking = True
        self._invalidate_tool_task("public_demo_deadline")
        for task in (
            self._receive_task,
            self._recovery_task,
            self._audio_playout_task,
            self._silence_check_task,
            self._unavailable_task,
            self._command_check_task,
        ):
            if task:
                task.cancel()
        websocket = self._ws
        self._ws = None
        if websocket is None:
            return
        transport = getattr(websocket, "transport", None)
        abort = getattr(transport, "abort", None)
        if callable(abort):
            abort()
        close = getattr(websocket, "close", None)
        if callable(close):
            close_task = asyncio.create_task(close())
            close_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )

    async def stop(self):
        """Stop transport and immediately discard all transient text context."""

        try:
            await super().stop()
        finally:
            self._transcript_lines.clear()
            self._caller_transcript_buf.clear()
            self._kevin_transcript_buf.clear()
            self._system_prompt = ""
            self._contractor_config = {}
