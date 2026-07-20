"""Staging-gated Gemini text pipeline with application-owned turn control."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import os
import re
from typing import Awaitable, Callable, Optional

from app.config import (
    PRODUCTION_CLOUD_RUN_URL,
    PRODUCTION_FIREBASE_DATABASE_URL,
    PRODUCTION_GCP_PROJECT_ID,
    settings,
)
from app.services.dialogue_planner import ActionName, plan_next_action
from app.services.gemini_controlled_turn import (
    GEMINI_CONTROLLED_MODEL,
    GeminiControlledTurnGenerator,
    ValidatedTurn,
    ValidationReason,
    deterministic_question_for_slot,
    deterministic_spoken_fallback,
    validate_spoken_turn,
)
from app.services.receptionist_state import (
    CallbackConfirmation,
    CallbackIntent,
    CallerObservation,
    IntakeState,
    Intent,
    Urgency,
)
from app.services.urgency import find_urgent_signal
from app.services.voice_pipeline import (
    ELEVENLABS_MODEL_DEFAULT,
    VoicePipeline,
    _log_voice_event,
    _log_voice_timing,
)
from app.services.voice_turn_coordinator import (
    CoordinatorDirective,
    PlaybackReceipt,
    TurnLifecycle,
    VoiceTurnCoordinator,
)


CONTROLLED_PIPELINE_VERSION = "gemini-controlled-v2"
_COHORT_HASH_PATTERN = re.compile(r"^[0-9a-f]{12}$")
_CONTROLLED_SOURCE_NAMES = frozenset(
    {
        "deterministic",
        "fallback",
        "greeting",
        "model",
        "question_replay",
        "reprompt",
        "silence_close",
        "unavailable",
    }
)
_PRESENCE_ACK_TOKENS = frozenset(
    {
        "am",
        "aqui",
        "can",
        "estoy",
        "hear",
        "hello",
        "here",
        "hi",
        "hola",
        "i",
        "im",
        "sigo",
        "si",
        "still",
        "sure",
        "te",
        "you",
        "ya",
        "yeah",
        "yep",
        "yes",
    }
)
_PRESENCE_ACK_SIGNALS = frozenset(
    {"aqui", "estoy", "hello", "here", "hi", "hola", "si", "yeah", "yep", "yes"}
)


def _is_presence_acknowledgement(caller_text: str) -> bool:
    normalized = caller_text.casefold().replace("í", "i")
    tokens = re.findall(r"[a-z]+", normalized.replace("'", ""))
    return bool(
        tokens
        and len(tokens) <= 6
        and set(tokens) <= _PRESENCE_ACK_TOKENS
        and set(tokens).intersection(_PRESENCE_ACK_SIGNALS)
    )


def _opening_question_from_greeting(greeting: str) -> str:
    match = re.search(r"(?:¿[^?]+\?|[^.!?]+\?)\s*$", greeting)
    return match.group(0).strip() if match else ""


class ControlledPipelineUnavailable(RuntimeError):
    """Raised when an allowlisted controlled call cannot use its required provider."""


def require_controlled_provider() -> None:
    """Fail closed instead of silently routing an allowlisted call elsewhere."""
    if not settings.gemini_api_key:
        raise ControlledPipelineUnavailable("controlled Gemini provider is unavailable")


def contractor_cohort_hash(contractor_id: str) -> str:
    """Return the opaque label used for configuration and telemetry."""
    return hashlib.sha256(contractor_id.encode("utf-8")).hexdigest()[:12]


def controlled_pipeline_allowed(
    *,
    contractor_id: str,
    contractor_config: dict | None = None,
) -> bool:
    if not contractor_id or (settings.environment or "").strip().lower() != "staging":
        return False
    if not settings.gemini_controlled_pipeline_enabled:
        return False
    if not settings.gemini_controlled_tts_zero_retention_enabled:
        return False
    if settings.allow_production_resources_in_non_production:
        return False
    if (
        not settings.firestore_project_id
        or settings.firestore_project_id == PRODUCTION_GCP_PROJECT_ID
        or not settings.firebase_database_url
        or settings.firebase_database_url == PRODUCTION_FIREBASE_DATABASE_URL
        or settings.cloud_run_url == PRODUCTION_CLOUD_RUN_URL
        or "staging" not in settings.cloud_run_url.casefold()
        or not settings.production_twilio_account_sid
        or settings.twilio_account_sid == settings.production_twilio_account_sid
    ):
        return False
    service_name = os.getenv("K_SERVICE", "")
    if service_name and "staging" not in service_name.casefold():
        return False
    if (contractor_config or {}).get("effective_mode") != "business":
        return False
    allowlist = {
        value.strip().lower()
        for value in settings.gemini_controlled_contractor_hashes.split(",")
        if _COHORT_HASH_PATTERN.fullmatch(value.strip().lower())
    }
    return contractor_cohort_hash(contractor_id) in allowlist


@dataclass(frozen=True, slots=True)
class _PendingSpeechContract:
    expects_input: bool
    asked_slot: str = ""
    question_text: str = ""
    close_after_playback: bool = False
    kind: str = "model"


class GeminiControlledPipeline(VoicePipeline):
    """Deepgram -> controlled Gemini text -> ElevenLabs, guarded to staging."""

    CALLER_SILENCE_CHECK_INTERVAL_SECONDS = 0.25

    def __init__(
        self,
        on_audio_out: Callable[[bytes], Awaitable[None]],
        on_transcript: Callable[[str, str], Awaitable[None]],
        on_clear_audio: Optional[Callable[[], Awaitable[None]]] = None,
        on_response_first_media_sent: Optional[Callable[[int], Awaitable[object]]] = None,
        on_response_end_media_sent: Optional[Callable[[int], Awaitable[object]]] = None,
        on_call_complete: Optional[Callable[[], Awaitable[None]]] = None,
        on_urgency_detected: Optional[Callable[[str], Awaitable[None]]] = None,
        call_sid: str = "",
        contractor_config: Optional[dict] = None,
        caller_phone: str = "",
        call_started_at: Optional[float] = None,
    ) -> None:
        super().__init__(
            on_audio_out=on_audio_out,
            on_transcript=on_transcript,
            on_clear_audio=on_clear_audio,
            on_response_first_media_sent=on_response_first_media_sent,
            on_response_end_media_sent=on_response_end_media_sent,
            on_call_complete=on_call_complete,
            on_urgency_detected=on_urgency_detected,
            call_sid=call_sid,
            contractor_config=contractor_config,
            caller_phone=caller_phone,
            call_started_at=call_started_at,
        )
        known_name = self._contractor_config.get("known_caller_name", "")
        self._intake_state = IntakeState.new(
            call_sid=call_sid,
            caller_phone=caller_phone,
            caller_name=known_name,
            caller_source="known_contact" if known_name else "",
            caller_confidence=1.0 if known_name else 0.0,
        )
        configured_language = str(
            self._contractor_config.get("user_language", "")
        ).casefold()[:2]
        if configured_language in {"en", "es"}:
            self._intake_state.language = configured_language
        self._turn_coordinator = VoiceTurnCoordinator(
            no_input_seconds=self.CALLER_SILENCE_PROMPT_SECONDS
        )
        self._turn_generator = GeminiControlledTurnGenerator(
            api_key=settings.gemini_api_key,
            http_client=self._http_client,
            call_sid=call_sid,
            receptionist_prompt=self._system_prompt,
        )
        self._pending_speech_contract: _PendingSpeechContract | None = None
        self._pending_reply_slot = ""
        self._presence_reply_pending = False
        self._last_played_question: tuple[str, str] | None = None
        self._playback_question_candidates: dict[int, tuple[str, str]] = {}
        self._active_generation_task: asyncio.Task | None = None

    def _log_cohort_configuration(self) -> None:
        _log_voice_timing(
            "voice_cohort_configuration",
            self._call_sid,
            cohort="gemini_controlled",
            engine="gemini_controlled_text",
            architecture_version=CONTROLLED_PIPELINE_VERSION,
            deepgram_model=self.DEEPGRAM_MODEL,
            deepgram_endpointing_ms=self.DEEPGRAM_ENDPOINTING_MS,
            gemini_model=GEMINI_CONTROLLED_MODEL,
            elevenlabs_model=self._tts_model_id or ELEVENLABS_MODEL_DEFAULT,
            pacing_ratio=self.ELEVENLABS_PACING_RATIO,
            deploy_sha=settings.deploy_sha,
        )

    async def _translate_greeting(
        self,
        *,
        greeting: str,
        business_name: str,
        user_language: str,
    ) -> str:
        return await self._turn_generator.translate_greeting(
            greeting=greeting,
            business_name=business_name,
            user_language=user_language,
        )

    async def _switch_language(self, lang_code: str):
        await super()._switch_language(lang_code)
        self._intake_state.language = self._language

    def _should_prefetch_jobber(self) -> bool:
        """Keep CRM data outside the first controlled cohort's model and memory path."""
        return False

    def _tts_request_url(self) -> str:
        if not settings.gemini_controlled_tts_zero_retention_enabled:
            raise RuntimeError("controlled TTS zero retention is not enabled")
        return f"{super()._tts_request_url()}&enable_logging=false"

    def _mark_caller_activity(self):
        state_before_activity = self._turn_coordinator.state
        current_response_turn = self._turn_coordinator.current_response_turn
        if state_before_activity in {
            TurnLifecycle.REPROMPTING,
            TurnLifecycle.AWAITING_PRESENCE,
        }:
            self._presence_reply_pending = True
        super()._mark_caller_activity()
        self._turn_coordinator.caller_activity()
        if current_response_turn is not None:
            self._playback_question_candidates.pop(current_response_turn, None)
        if (
            state_before_activity == TurnLifecycle.GENERATING
            and self._active_generation_task
            and self._active_generation_task is not asyncio.current_task()
        ):
            self._active_generation_task.cancel()

    def _authorize_observation(self, observation: CallerObservation) -> CallerObservation:
        """Bind answer-only facts to the slot whose question was audibly played."""
        authorized = observation
        if (
            observation.callback_confirmation is not None
            and observation.callback_confirmation != CallbackConfirmation.UNKNOWN
            and self._pending_reply_slot != "callback_confirmation"
        ):
            authorized = replace(authorized, callback_confirmation=None)
        if (
            observation.callback_intent == CallbackIntent.ACCEPTED
            and self._pending_reply_slot != "callback_preference"
        ):
            authorized = replace(authorized, callback_intent=None)
        self._pending_reply_slot = ""
        return authorized

    async def _handle_caller_speech(
        self,
        caller_text: str,
        *,
        caller_turn: int | None = None,
        committed_at: float | None = None,
    ) -> None:
        resolved_turn = caller_turn or self._caller_turn_number
        resolved_committed_at = committed_at or self._caller_turn_committed_at
        if resolved_turn != self._caller_turn_number:
            _log_voice_timing(
                "controlled_generation_suppressed",
                self._call_sid,
                caller_turn=resolved_turn,
                reason="stale_caller_turn",
            )
            return
        if self._presence_reply_pending:
            self._presence_reply_pending = False
            if _is_presence_acknowledgement(caller_text):
                if await self._replay_last_question(
                    caller_turn=resolved_turn,
                    committed_at=resolved_committed_at,
                ):
                    return
                self._pending_reply_slot = ""
                _log_voice_timing(
                    "controlled_question_replay_suppressed",
                    self._call_sid,
                    caller_turn=resolved_turn,
                    reason="invalid_lifecycle",
                )
                return
        if not self._turn_coordinator.begin_generation(resolved_turn):
            return
        self._active_generation_task = asyncio.current_task()
        try:
            controlled_observation = await self._turn_generator.extract_observation(
                caller_text=caller_text,
                state=self._intake_state,
                caller_turn=resolved_turn,
            )
        finally:
            if self._active_generation_task is asyncio.current_task():
                self._active_generation_task = None
        if not self._turn_coordinator.accepts_generated_turn(resolved_turn):
            _log_voice_timing(
                "controlled_generation_suppressed",
                self._call_sid,
                caller_turn=resolved_turn,
                reason="caller_activity",
            )
            return

        observation = self._authorize_observation(controlled_observation.facts)
        urgent_signal = find_urgent_signal(caller_text)
        if urgent_signal:
            observation = CallerObservation(
                language=observation.language,
                caller_name=observation.caller_name,
                identity_confirmed=observation.identity_confirmed,
                business_scope=observation.business_scope,
                business_scope_reason=observation.business_scope_reason,
                intent=Intent.EMERGENCY,
                service_object=observation.service_object,
                service_action=observation.service_action,
                urgency=Urgency.EMERGENCY,
                callback_intent=observation.callback_intent,
                callback_confirmation=observation.callback_confirmation,
                callback_phone_last_four=observation.callback_phone_last_four,
                address_need=observation.address_need,
            )
        self._intake_state.apply_caller_observation(observation)
        action = plan_next_action(
            self._intake_state,
            require_caller_name=True,
        )
        if action.name == ActionName.ANSWER_DIRECT_QUESTION:
            validated = self._turn_generator.build_direct_turn(
                answer_text=controlled_observation.direct_answer_text,
                caller_text=caller_text,
                state=self._intake_state,
                action=action,
                caller_turn=resolved_turn,
            )
            source = "fallback" if validated.fallback else "model"
        else:
            deterministic = deterministic_spoken_fallback(
                action=action,
                state=self._intake_state,
                caller_text=caller_text,
            )
            validation = validate_spoken_turn(
                deterministic,
                action=action,
                caller_text=caller_text,
            )
            if validation != ValidationReason.VALID:
                raise RuntimeError("server-owned spoken turn violated its contract")
            validated = ValidatedTurn(
                deterministic,
                repaired=False,
                fallback=False,
            )
            source = "deterministic"
            _log_voice_timing(
                "controlled_turn_server_rendered",
                self._call_sid,
                caller_turn=resolved_turn,
                action=action.name.value,
            )
        if not self._turn_coordinator.accepts_generated_turn(resolved_turn):
            _log_voice_timing(
                "controlled_generation_suppressed",
                self._call_sid,
                caller_turn=resolved_turn,
                reason="caller_activity",
            )
            return

        spoken = validated.turn
        self._pending_speech_contract = _PendingSpeechContract(
            expects_input=spoken.expects_input,
            asked_slot=spoken.asked_slot,
            question_text=(
                deterministic_question_for_slot(
                    slot=spoken.asked_slot,
                    state=self._intake_state,
                    spanish=self._intake_state.language.casefold().startswith("es"),
                )
                if spoken.asked_slot
                else ""
            ),
            close_after_playback=action.name == ActionName.WRAP_UP,
            kind="fallback" if validated.fallback else "model",
        )
        self._conversation.append({"role": "assistant", "content": spoken.spoken_text})
        if len(self._conversation) > 30:
            self._conversation = self._conversation[-30:]
        _log_voice_event(
            "assistant_response_ready",
            self._call_sid,
            chars=len(spoken.spoken_text),
            words=len(spoken.spoken_text.split()),
            action=action.name.value,
            repaired=validated.repaired,
            fallback=validated.fallback,
        )
        await self.on_transcript("Kevin", spoken.spoken_text)
        await self._speak(
            spoken.spoken_text,
            source=source,
            caller_turn=resolved_turn,
            caller_committed_at=resolved_committed_at,
        )

    async def _speak(
        self,
        text: str,
        *,
        source: str = "runtime",
        caller_turn: int | None = None,
        caller_committed_at: float | None = None,
    ):
        resolved_caller_turn = caller_turn or self._caller_turn_number
        contract = self._pending_speech_contract
        self._pending_speech_contract = None
        if contract is None:
            if source == "greeting":
                contract = _PendingSpeechContract(
                    expects_input=True,
                    question_text=_opening_question_from_greeting(text),
                    kind="greeting",
                )
            elif source == "unavailable":
                contract = _PendingSpeechContract(expects_input=True, kind="greeting")
            else:
                contract = _PendingSpeechContract(expects_input=False, kind="fallback")

        response_turn = self._response_turn_number + 1
        if not self._turn_coordinator.begin_playback(
            response_turn=response_turn,
            caller_turn=resolved_caller_turn,
            expects_input=contract.expects_input,
            asked_slot=contract.asked_slot,
            close_after_playback=contract.close_after_playback,
            kind=contract.kind,
        ):
            _log_voice_timing(
                "controlled_playback_suppressed",
                self._call_sid,
                caller_turn=resolved_caller_turn,
                source=source if source in _CONTROLLED_SOURCE_NAMES else "unknown",
                reason="stale_turn",
            )
            return None
        if contract.question_text:
            self._playback_question_candidates[response_turn] = (
                contract.question_text,
                contract.asked_slot,
            )
        delivered = await super()._speak(
            text,
            source=source,
            caller_turn=caller_turn,
            caller_committed_at=caller_committed_at,
        )
        if delivered is False:
            self._playback_question_candidates.pop(response_turn, None)
            recovered = self._turn_coordinator.delivery_failed(response_turn)
            _log_voice_timing(
                "controlled_delivery_failed",
                self._call_sid,
                turn=response_turn,
                recovered=recovered,
            )
        return delivered

    async def on_playback_receipt(self, receipt: PlaybackReceipt) -> None:
        current_response_turn = self._turn_coordinator.current_response_turn
        outcome = self._turn_coordinator.resolve_playback(receipt)
        question_candidate = None
        if (
            receipt.phase == "response_end"
            and receipt.turn == current_response_turn
            and receipt.epoch == current_response_turn
        ):
            question_candidate = self._playback_question_candidates.pop(receipt.turn, None)
        _log_voice_timing(
            "controlled_playback_transition",
            self._call_sid,
            turn=receipt.turn,
            phase=receipt.phase,
            status=receipt.status.value,
            state=self._turn_coordinator.state.value,
            directive=outcome.directive.value,
            slot_committed=bool(outcome.committed_slot),
        )
        if outcome.committed_slot:
            self._intake_state.mark_slot_asked(outcome.committed_slot)
            self._pending_reply_slot = outcome.committed_slot
        if outcome.played and question_candidate:
            self._last_played_question = question_candidate
        if outcome.directive == CoordinatorDirective.HANGUP and self.on_call_complete:
            await self.on_call_complete()
            self._turn_coordinator.mark_ended()

    async def _silence_check_loop(self):
        try:
            while self._connected:
                await asyncio.sleep(self.CALLER_SILENCE_CHECK_INTERVAL_SECONDS)
                directive = self._turn_coordinator.due_action()
                if directive == CoordinatorDirective.REPROMPT:
                    await self._speak_presence_check()
                elif directive == CoordinatorDirective.CLOSE_FOR_SILENCE:
                    await self._speak_silence_close()
        except asyncio.CancelledError:
            pass

    async def _speak_presence_check(self) -> None:
        async with self._response_lock:
            message = (
                "¿Sigue ahí?"
                if self._intake_state.language.casefold().startswith("es")
                else "Are you still there?"
            )
            self._pending_speech_contract = _PendingSpeechContract(
                expects_input=True,
                kind="reprompt",
            )
            await self.on_transcript("Kevin", message)
            await self._speak(message, source="reprompt")

    async def _replay_last_question(
        self,
        *,
        caller_turn: int,
        committed_at: float,
    ) -> bool:
        question = self._last_played_question
        if question is None:
            asked_slot = self._pending_reply_slot
            question_text = (
                deterministic_question_for_slot(
                    slot=asked_slot,
                    state=self._intake_state,
                    spanish=self._intake_state.language.casefold().startswith("es"),
                )
                if asked_slot
                else (
                    "¿Cómo puedo ayudarle?"
                    if self._intake_state.language.casefold().startswith("es")
                    else "How can I help you?"
                )
            )
            question = (question_text, asked_slot)
        if not self._turn_coordinator.begin_question_replay(caller_turn):
            return False
        question_text, asked_slot = question
        self._pending_reply_slot = ""
        self._pending_speech_contract = _PendingSpeechContract(
            expects_input=True,
            asked_slot=asked_slot,
            question_text=question_text,
            kind="question_replay",
        )
        await self.on_transcript("Kevin", question_text)
        await self._speak(
            question_text,
            source="question_replay",
            caller_turn=caller_turn,
            caller_committed_at=committed_at,
        )
        return True

    async def _speak_silence_close(self) -> None:
        async with self._response_lock:
            message = (
                "Voy a colgar por ahora. Llame de nuevo cuando pueda. Adiós."
                if self._intake_state.language.casefold().startswith("es")
                else (
                    "I'm going to hang up for now. Please call back when you're ready. "
                    "Goodbye."
                )
            )
            self._pending_speech_contract = _PendingSpeechContract(
                expects_input=False,
                close_after_playback=True,
                kind="silence_close",
            )
            await self.on_transcript("Kevin", message)
            await self._speak(message, source="silence_close")
