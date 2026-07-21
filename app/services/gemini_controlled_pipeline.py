"""Staging-gated Gemini text pipeline with application-owned turn control."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import os
import re
import time
from typing import Awaitable, Callable, Optional

from app.config import (
    PRODUCTION_CLOUD_RUN_URL,
    PRODUCTION_FIREBASE_DATABASE_URL,
    PRODUCTION_GCP_PROJECT_ID,
    settings,
)
from app.services.dialogue_planner import ActionName, NextAction, plan_next_action
from app.services.gemini_controlled_turn import (
    CallerTurnCompleteness,
    DirectAnswerKind,
    DirectQuestionAssessment,
    DirectQuestionTopic,
    GEMINI_CONTROLLED_MODEL,
    GeminiControlledTurnGenerator,
    PresenceReplyKind,
    ValidatedTurn,
    ValidationReason,
    deterministic_question_for_slot,
    deterministic_spoken_fallback,
    validate_spoken_turn,
)
from app.services.receptionist_state import (
    BusinessScope,
    CallbackConfirmation,
    CallbackIntent,
    CallerObservation,
    IntakeState,
    Intent,
    ServiceAction,
    Urgency,
)
from app.services.urgency import find_urgent_signal
from app.services.voice_pipeline import (
    ELEVENLABS_MODEL_DEFAULT,
    VoicePipeline,
    _log_voice_exception,
    _log_voice_event,
    _log_voice_timing,
)
from app.services.voice_turn_coordinator import (
    CoordinatorDirective,
    PlaybackReceipt,
    PlaybackStatus,
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


@dataclass(frozen=True, slots=True)
class _PresenceContext:
    question_text: str
    asked_slot: str = ""


@dataclass(slots=True)
class _CallerSemanticEpisode:
    """One caller thought, which can span several transport-level commits."""

    revision: int
    fragments: list[str]
    caller_turn: int
    committed_at: float
    intake_state_snapshot: IntakeState
    pending_reply_slot_snapshot: str
    presence_reply_pending_snapshot: bool
    suspended_presence_context_snapshot: _PresenceContext | None
    response_turn: int | None = None
    assistant_text: str = ""


class GeminiControlledPipeline(VoicePipeline):
    """Deepgram -> controlled Gemini text -> ElevenLabs, guarded to staging."""

    CALLER_SILENCE_CHECK_INTERVAL_SECONDS = 0.25
    MAX_SEMANTIC_EPISODE_FRAGMENTS = 6
    MAX_SEMANTIC_EPISODE_CHARS = 480
    SEMANTIC_EPISODE_SETTLEMENT_SECONDS = 2.5

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
        self._downstream_first_media_sent = on_response_first_media_sent
        super().__init__(
            on_audio_out=on_audio_out,
            on_transcript=on_transcript,
            on_clear_audio=on_clear_audio,
            on_response_first_media_sent=self._on_response_first_media_sent,
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
        self._suspended_presence_context: _PresenceContext | None = None
        self._last_played_question: tuple[str, str] | None = None
        self._playback_question_candidates: dict[int, tuple[str, str]] = {}
        self._active_generation_task: asyncio.Task | None = None
        self._semantic_episode: _CallerSemanticEpisode | None = None
        self._semantic_episode_revision = 0
        self._semantic_episodes_by_response_turn: dict[int, _CallerSemanticEpisode] = {}
        self._semantic_episode_settlement_task: asyncio.Task | None = None
        self._pending_transcripts_by_response_turn: dict[int, str] = {}

    def _begin_or_extend_semantic_episode(
        self,
        *,
        caller_text: str,
        caller_turn: int,
        committed_at: float,
    ) -> _CallerSemanticEpisode:
        episode = self._semantic_episode
        if episode is not None and (
            len(episode.fragments) >= self.MAX_SEMANTIC_EPISODE_FRAGMENTS
            or len(" ".join(episode.fragments)) + len(caller_text)
            > self.MAX_SEMANTIC_EPISODE_CHARS
        ):
            self._discard_semantic_episode(reason="episode_bound")
            episode = None
        if episode is None:
            self._semantic_episode_revision += 1
            episode = _CallerSemanticEpisode(
                revision=self._semantic_episode_revision,
                fragments=[caller_text],
                caller_turn=caller_turn,
                committed_at=committed_at,
                intake_state_snapshot=IntakeState.from_dict(self._intake_state.to_dict()),
                pending_reply_slot_snapshot=self._pending_reply_slot,
                presence_reply_pending_snapshot=self._presence_reply_pending,
                suspended_presence_context_snapshot=self._suspended_presence_context,
            )
            self._semantic_episode = episode
            return episode

        episode.fragments.append(caller_text)
        episode.caller_turn = caller_turn
        episode.committed_at = committed_at
        episode.response_turn = None
        episode.assistant_text = ""
        return episode

    def _cancel_semantic_episode_settlement(self) -> None:
        task = self._semantic_episode_settlement_task
        self._semantic_episode_settlement_task = None
        if (
            task is not None
            and not task.done()
            and task is not asyncio.current_task()
        ):
            task.cancel()

    def _restore_semantic_episode(
        self,
        *,
        reason: str,
        restore_presence_authority: bool = False,
    ) -> None:
        """Discard speculative state while retaining fragments for the next commit."""
        episode = self._semantic_episode
        if episode is None:
            return
        if episode.response_turn is not None:
            self._semantic_episodes_by_response_turn.pop(episode.response_turn, None)
        self._intake_state = IntakeState.from_dict(episode.intake_state_snapshot.to_dict())
        self._pending_reply_slot = episode.pending_reply_slot_snapshot
        self._presence_reply_pending = episode.presence_reply_pending_snapshot
        self._suspended_presence_context = episode.suspended_presence_context_snapshot
        self._pending_speech_contract = None
        episode.response_turn = None
        episode.assistant_text = ""
        presence_restored = False
        if restore_presence_authority and episode.presence_reply_pending_snapshot:
            presence_restored = self._turn_coordinator.restore_unplayed_presence_resolution(
                episode.caller_turn
            )
        _log_voice_timing(
            "controlled_semantic_episode_reset",
            self._call_sid,
            episode_revision=episode.revision,
            caller_turn=episode.caller_turn,
            fragments=len(episode.fragments),
            reason=reason,
            presence_restored=presence_restored,
        )

    def _discard_semantic_episode(self, *, reason: str) -> None:
        episode = self._semantic_episode
        if episode is None:
            return
        self._restore_semantic_episode(reason=reason)
        self._semantic_episode = None

    async def _on_response_first_media_sent(self, response_turn: int) -> object:
        """Request a first-media mark without treating it as a playback receipt."""
        if self._downstream_first_media_sent is not None:
            try:
                accepted = await self._downstream_first_media_sent(response_turn)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._fail_first_media_delivery(
                    response_turn=response_turn,
                    reason="first_media_mark_error",
                )
                _log_voice_exception(
                    "controlled_first_media_mark_error",
                    error,
                    self._call_sid,
                )
                return False
            if accepted is False:
                self._fail_first_media_delivery(
                    response_turn=response_turn,
                    reason="first_media_mark_rejected",
                )
            return accepted
        return None

    def _promote_semantic_episode_after_first_media_receipt(
        self,
        *,
        response_turn: int,
    ) -> None:
        episode = self._semantic_episodes_by_response_turn.pop(response_turn, None)
        if episode is None:
            return
        if self._semantic_episode is episode:
            self._semantic_episode = None
        self._cancel_semantic_episode_settlement()
        if episode.assistant_text:
            self._pending_transcripts_by_response_turn[response_turn] = (
                episode.assistant_text
            )
        _log_voice_timing(
            "controlled_semantic_episode_promoted",
            self._call_sid,
            episode_revision=episode.revision,
            caller_turn=episode.caller_turn,
            fragments=len(episode.fragments),
            response_turn=response_turn,
            receipt="first_media_played",
        )

    def _fail_first_media_delivery(
        self,
        *,
        response_turn: int,
        reason: str,
    ) -> None:
        episode = self._semantic_episodes_by_response_turn.get(response_turn)
        if episode is not None:
            if self._semantic_episode is episode:
                self._discard_semantic_episode(reason=reason)
            else:
                self._semantic_episodes_by_response_turn.pop(response_turn, None)
        self._pending_transcripts_by_response_turn.pop(response_turn, None)
        recovered = self._turn_coordinator.delivery_failed(response_turn)
        if episode is None:
            return
        _log_voice_timing(
            "controlled_semantic_episode_rollback",
            self._call_sid,
            episode_revision=episode.revision,
            caller_turn=episode.caller_turn,
            response_turn=response_turn,
            reason=reason,
            recovered=recovered,
        )

    def _rollback_semantic_episode_after_failed_first_media_receipt(
        self,
        *,
        response_turn: int,
        status: PlaybackStatus,
    ) -> None:
        self._fail_first_media_delivery(
            response_turn=response_turn,
            reason=f"first_media_{status.value}",
        )

    async def _publish_transcript_after_response_end(
        self,
        *,
        response_turn: int,
    ) -> None:
        transcript_text = self._pending_transcripts_by_response_turn.pop(
            response_turn,
            "",
        )
        if not transcript_text:
            return
        self._conversation.append({"role": "assistant", "content": transcript_text})
        if len(self._conversation) > 30:
            self._conversation = self._conversation[-30:]
        try:
            await self.on_transcript("Kevin", transcript_text)
        except Exception as error:
            _log_voice_exception(
                "controlled_transcript_after_response_end_failed",
                error,
                self._call_sid,
            )

    def _schedule_semantic_episode_settlement(
        self,
        *,
        episode_revision: int,
        caller_turn: int,
    ) -> None:
        self._cancel_semantic_episode_settlement()
        self._semantic_episode_settlement_task = asyncio.create_task(
            self._settle_incomplete_semantic_episode(
                episode_revision=episode_revision,
                caller_turn=caller_turn,
            )
        )

    async def _settle_incomplete_semantic_episode(
        self,
        *,
        episode_revision: int,
        caller_turn: int,
    ) -> None:
        """Ask for one completion only if the caller thought remains unfinished."""
        try:
            await asyncio.sleep(self.SEMANTIC_EPISODE_SETTLEMENT_SECONDS)
            async with self._response_lock:
                episode = self._semantic_episode
                if (
                    episode is None
                    or episode.revision != episode_revision
                    or episode.caller_turn != caller_turn
                    or episode.assistant_text
                    or self._caller_turn_number != caller_turn
                ):
                    return
                if not self._turn_coordinator.begin_semantic_settlement(caller_turn):
                    return
                message = (
                    "Lo siento, ¿puede terminar su pregunta?"
                    if self._intake_state.language.casefold().startswith("es")
                    else "I'm sorry, could you finish your question?"
                )
                self._pending_speech_contract = _PendingSpeechContract(
                    expects_input=True,
                    question_text=message,
                    kind="fallback",
                )
                _log_voice_timing(
                    "controlled_semantic_episode_settlement_started",
                    self._call_sid,
                    episode_revision=episode_revision,
                    caller_turn=caller_turn,
                )
                await self._speak(
                    message,
                    source="fallback",
                    caller_turn=caller_turn,
                    transcript_after_response_end=message,
                )
        except asyncio.CancelledError:
            raise
        finally:
            if self._semantic_episode_settlement_task is asyncio.current_task():
                self._semantic_episode_settlement_task = None

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
        self._cancel_semantic_episode_settlement()
        if self._semantic_episode is not None:
            # A later transport commit before first outbound media continues the
            # same caller thought. Revert any candidate facts or transcript
            # intent before its task is cancelled, then assemble the next commit.
            self._restore_semantic_episode(
                reason="caller_continuation",
                restore_presence_authority=True,
            )
        if current_response_turn is not None:
            self._pending_transcripts_by_response_turn.pop(current_response_turn, None)
        if state_before_activity in {
            TurnLifecycle.REPROMPTING,
            TurnLifecycle.AWAITING_PRESENCE,
            TurnLifecycle.RESOLVING_PRESENCE,
        }:
            self._suspend_original_question()
            self._presence_reply_pending = True
        super()._mark_caller_activity()
        self._turn_coordinator.caller_activity()
        if current_response_turn is not None:
            self._playback_question_candidates.pop(current_response_turn, None)
        if (
            state_before_activity
            in {TurnLifecycle.GENERATING, TurnLifecycle.RESOLVING_PRESENCE}
            and self._active_generation_task
            and self._active_generation_task is not asyncio.current_task()
        ):
            self._active_generation_task.cancel()

    def _suspend_original_question(self) -> None:
        """Remove business-slot authority while a presence reply is unresolved."""
        if self._suspended_presence_context is None:
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
            self._suspended_presence_context = _PresenceContext(*question)
        self._pending_reply_slot = ""

    @staticmethod
    def _presence_answer_is_explicit(
        observation: CallerObservation,
        asked_slot: str,
    ) -> bool:
        if asked_slot == "callback_confirmation":
            return observation.callback_confirmation not in {
                None,
                CallbackConfirmation.UNKNOWN,
            }
        if asked_slot == "callback_preference":
            return observation.callback_intent not in {
                None,
                CallbackIntent.NONE,
                CallbackIntent.OFFERED,
            }
        return True

    def _authorize_observation(self, observation: CallerObservation) -> CallerObservation:
        """Bind answer-only facts to the slot whose question was audibly played."""
        authorized = observation
        if (
            authorized.service_action == ServiceAction.UNKNOWN
            and self._intake_state.service_action != ServiceAction.UNKNOWN
        ):
            authorized = replace(authorized, service_action=None)
        request_changed = bool(
            authorized.service_object
            and self._intake_state.service_object
            and authorized.service_object.casefold()
            != self._intake_state.service_object.casefold()
        ) or bool(
            authorized.service_action is not None
            and authorized.service_action != ServiceAction.UNKNOWN
            and self._intake_state.service_action != ServiceAction.UNKNOWN
            and authorized.service_action != self._intake_state.service_action
        )
        if (
            self._intake_state.business_scope == BusinessScope.IN_SCOPE
            and authorized.business_scope
            in {BusinessScope.OUT_OF_SCOPE, BusinessScope.UNCLEAR}
            and not request_changed
        ):
            authorized = replace(
                authorized,
                business_scope=None,
                business_scope_reason=None,
            )
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

    @staticmethod
    def _direct_scope_answer_action(
        answer_kind: DirectAnswerKind | None,
        planned_action: NextAction,
    ) -> NextAction:
        if answer_kind not in {
            DirectAnswerKind.SCOPE_SUPPORTED,
            DirectAnswerKind.SCOPE_REQUIRES_REVIEW,
        } or planned_action.name == ActionName.SAFETY_GUIDANCE:
            return planned_action
        return NextAction(
            name=ActionName.ANSWER_DIRECT_QUESTION,
            reason="caller asked a direct service-scope question",
            allowed_slots=(
                planned_action.allowed_slots if planned_action.question_required else ()
            ),
            forbidden_slots=planned_action.forbidden_slots,
            memory_facts_safe_to_use=planned_action.memory_facts_safe_to_use,
            max_spoken_shape=(
                "answer the service-scope question, then ask one allowed follow-up"
                if planned_action.question_required
                else "answer the service-scope question briefly"
            ),
            tool_calls_allowed=False,
            question_required=planned_action.question_required,
        )

    def _reconcile_direct_answer_kind(
        self,
        *,
        extracted_kind: DirectAnswerKind | None,
        assessment: DirectQuestionAssessment,
    ) -> DirectAnswerKind | None:
        """Require independent semantic authority before any direct answer is spoken."""
        if assessment.topic == DirectQuestionTopic.PRICING:
            resolved = DirectAnswerKind.PRICING_REQUIRES_REVIEW
        elif assessment.topic == DirectQuestionTopic.SERVICE_SCOPE:
            resolved = (
                DirectAnswerKind.SCOPE_SUPPORTED
                if self._intake_state.business_scope == BusinessScope.IN_SCOPE
                else DirectAnswerKind.SCOPE_REQUIRES_REVIEW
            )
        else:
            resolved = None
        _log_voice_timing(
            "controlled_direct_answer_reconciled",
            self._call_sid,
            extracted_kind=extracted_kind.value if extracted_kind else "none",
            assessment_topic=assessment.topic.value,
            resolved_kind=resolved.value if resolved else "none",
            mismatch=(
                extracted_kind is not None
                and resolved is not None
                and extracted_kind != resolved
            ),
        )
        return resolved

    @staticmethod
    def _safe_direct_answer_fallback_action(action: NextAction) -> NextAction:
        """Never turn an unverified direct-answer intent into a pricing statement."""
        if action.question_required and action.allowed_slots:
            return NextAction(
                name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
                reason="direct-question semantics were not independently established",
                allowed_slots=action.allowed_slots[:1],
                forbidden_slots=action.forbidden_slots,
                memory_facts_safe_to_use=action.memory_facts_safe_to_use,
                max_spoken_shape="ask one concise clarifying question",
                tool_calls_allowed=False,
                question_required=True,
            )
        return NextAction(
            name=ActionName.TAKE_MESSAGE,
            reason="direct-question semantics were not independently established",
            forbidden_slots=action.forbidden_slots,
            memory_facts_safe_to_use=action.memory_facts_safe_to_use,
            max_spoken_shape="acknowledge the message without asserting an answer",
            tool_calls_allowed=False,
        )

    async def _process_utterance(
        self,
        text: str,
        *,
        caller_turn: int | None = None,
        committed_at: float | None = None,
    ) -> None:
        """Process one controlled turn and turn internal failures into a heard retry."""
        resolved_turn = caller_turn or self._caller_turn_number
        resolved_committed_at = committed_at or self._caller_turn_committed_at
        async with self._response_lock:
            _log_voice_timing(
                "response_processing_started",
                self._call_sid,
                caller_turn=resolved_turn,
                queue_wait_ms=(
                    max(0, round((time.monotonic() - resolved_committed_at) * 1_000))
                    if resolved_committed_at > 0
                    else 0
                ),
                call_elapsed_ms=self._call_elapsed_ms(),
            )
            try:
                await self._handle_caller_speech(
                    text,
                    caller_turn=resolved_turn,
                    committed_at=resolved_committed_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _log_voice_exception(
                    "controlled_response_processing_failed",
                    error,
                    self._call_sid,
                )
                self._discard_semantic_episode(reason="generation_failure")
                recovering_presence = (
                    self._turn_coordinator.state == TurnLifecycle.RESOLVING_PRESENCE
                )
                if not self._turn_coordinator.begin_failure_recovery(resolved_turn):
                    _log_voice_timing(
                        "controlled_failure_recovery_suppressed",
                        self._call_sid,
                        caller_turn=resolved_turn,
                        reason="invalid_lifecycle",
                    )
                    return
                if recovering_presence:
                    context = self._suspended_presence_context
                    question = (
                        (context.question_text, context.asked_slot)
                        if context is not None
                        else self._last_played_question
                    )
                    if question is None:
                        question = (
                            (
                                "¿Puede repetirlo una vez más?"
                                if self._intake_state.language.casefold().startswith("es")
                                else "Could you say that one more time?"
                            ),
                            "",
                        )
                    message, asked_slot = question
                    contract = _PendingSpeechContract(
                        expects_input=True,
                        asked_slot=asked_slot,
                        question_text=message,
                        kind="question_replay",
                    )
                    source = "question_replay"
                    self._pending_reply_slot = ""
                    self._suspended_presence_context = None
                else:
                    message = (
                        "Lo siento, tuve un problema. ¿Puede repetirlo una vez más?"
                        if self._intake_state.language.casefold().startswith("es")
                        else (
                            "I'm sorry, I had trouble with that. "
                            "Could you say that one more time?"
                        )
                    )
                    contract = _PendingSpeechContract(
                        expects_input=True,
                        kind="fallback",
                    )
                    source = "fallback"
                self._pending_speech_contract = contract
                _log_voice_timing(
                    "controlled_failure_recovery_started",
                    self._call_sid,
                    caller_turn=resolved_turn,
                    kind=contract.kind,
                )
                delivered = await self._speak(
                    message,
                    source=source,
                    caller_turn=resolved_turn,
                    caller_committed_at=resolved_committed_at,
                )
                if delivered:
                    await self.on_transcript("Kevin", message)

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
        episode = self._begin_or_extend_semantic_episode(
            caller_text=caller_text,
            caller_turn=resolved_turn,
            committed_at=resolved_committed_at,
        )
        semantic_text = " ".join(episode.fragments)
        presence_context = (
            self._suspended_presence_context
            if self._presence_reply_pending
            else None
        )
        self._presence_reply_pending = False
        begin_accepted = (
            self._turn_coordinator.begin_presence_resolution(resolved_turn)
            if presence_context
            else self._turn_coordinator.begin_generation(resolved_turn)
        )
        if not begin_accepted:
            self._discard_semantic_episode(reason="generation_not_accepted")
            return
        self._active_generation_task = asyncio.current_task()
        try:
            analyze_turn = getattr(self._turn_generator, "analyze_caller_turn", None)
            if analyze_turn is not None:
                controlled_observation, direct_assessment = await analyze_turn(
                    caller_text=semantic_text,
                    state=self._intake_state,
                    caller_turn=resolved_turn,
                    presence_check_active=presence_context is not None,
                    suspended_slot=(
                        presence_context.asked_slot if presence_context else ""
                    ),
                )
            else:
                controlled_observation = await self._turn_generator.extract_observation(
                    caller_text=semantic_text,
                    state=self._intake_state,
                    caller_turn=resolved_turn,
                    presence_check_active=presence_context is not None,
                    suspended_slot=(
                        presence_context.asked_slot if presence_context else ""
                    ),
                )
                direct_assessment = DirectQuestionAssessment()
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

        urgent_signal = find_urgent_signal(semantic_text)
        if (
            presence_context is None
            and direct_assessment.completeness == CallerTurnCompleteness.INCOMPLETE
            and not urgent_signal
        ):
            self._restore_semantic_episode(reason="incomplete_semantic_turn")
            deferred = self._turn_coordinator.defer_generation(resolved_turn)
            _log_voice_timing(
                "controlled_semantic_episode_deferred",
                self._call_sid,
                episode_revision=episode.revision,
                caller_turn=resolved_turn,
                fragments=len(episode.fragments),
                deferred=deferred,
            )
            if deferred:
                self._schedule_semantic_episode_settlement(
                    episode_revision=episode.revision,
                    caller_turn=resolved_turn,
                )
            return

        if presence_context is not None:
            presence_kind = controlled_observation.presence_reply_kind
            explicit_answer = (
                presence_kind == PresenceReplyKind.SUBSTANTIVE
                and self._presence_answer_is_explicit(
                    controlled_observation.facts,
                    presence_context.asked_slot,
                )
            )
            _log_voice_timing(
                "controlled_presence_resolution",
                self._call_sid,
                caller_turn=resolved_turn,
                result=(
                    PresenceReplyKind.SUBSTANTIVE.value
                    if explicit_answer
                    else "replay"
                ),
                model_kind=(presence_kind.value if presence_kind else "absent"),
                suspended_slot=bool(presence_context.asked_slot),
            )
            if not explicit_answer:
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
            if not self._turn_coordinator.accept_presence_answer(resolved_turn):
                return
            self._pending_reply_slot = presence_context.asked_slot
            self._suspended_presence_context = None

        observation = self._authorize_observation(controlled_observation.facts)
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
        direct_answer_kind = self._reconcile_direct_answer_kind(
            extracted_kind=controlled_observation.direct_answer_kind,
            assessment=direct_assessment,
        )
        action = self._direct_scope_answer_action(direct_answer_kind, action)
        if action.name == ActionName.ANSWER_DIRECT_QUESTION and direct_answer_kind is None:
            action = self._safe_direct_answer_fallback_action(action)
        if action.name == ActionName.ANSWER_DIRECT_QUESTION:
            validated = self._turn_generator.build_direct_turn(
                answer_kind=direct_answer_kind,
                caller_text=semantic_text,
                state=self._intake_state,
                action=action,
                caller_turn=resolved_turn,
            )
            source = "fallback" if validated.fallback else "model"
        else:
            deterministic = deterministic_spoken_fallback(
                action=action,
                state=self._intake_state,
                caller_text=semantic_text,
            )
            validation = validate_spoken_turn(
                deterministic,
                action=action,
                caller_text=semantic_text,
            )
            _log_voice_timing(
                "controlled_server_turn_validation",
                self._call_sid,
                caller_turn=resolved_turn,
                action=action.name.value,
                reason=validation.value,
                valid=validation == ValidationReason.VALID,
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
        episode.assistant_text = spoken.spoken_text
        _log_voice_event(
            "assistant_response_ready",
            self._call_sid,
            chars=len(spoken.spoken_text),
            words=len(spoken.spoken_text.split()),
            action=action.name.value,
            repaired=validated.repaired,
            fallback=validated.fallback,
        )
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
        transcript_after_response_end: str = "",
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
        episode = self._semantic_episode
        if (
            episode is not None
            and episode.caller_turn == resolved_caller_turn
            and episode.assistant_text
            and source in {"deterministic", "fallback", "model"}
        ):
            episode.response_turn = response_turn
            self._semantic_episodes_by_response_turn[response_turn] = episode
        if transcript_after_response_end:
            self._pending_transcripts_by_response_turn[response_turn] = (
                transcript_after_response_end
            )
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
            self._pending_transcripts_by_response_turn.pop(response_turn, None)
            recovered = self._turn_coordinator.delivery_failed(response_turn)
            episode = self._semantic_episodes_by_response_turn.get(response_turn)
            if episode is not None and self._semantic_episode is episode:
                self._restore_semantic_episode(
                    reason="zero_outbound_media",
                    restore_presence_authority=True,
                )
            _log_voice_timing(
                "controlled_delivery_failed",
                self._call_sid,
                turn=response_turn,
                recovered=recovered,
            )
        return delivered

    async def on_playback_receipt(self, receipt: PlaybackReceipt) -> None:
        current_response_turn = self._turn_coordinator.current_response_turn
        if (
            receipt.phase == "first_media"
            and receipt.turn == current_response_turn
            and receipt.epoch == current_response_turn
        ):
            if receipt.status == PlaybackStatus.PLAYED:
                self._promote_semantic_episode_after_first_media_receipt(
                    response_turn=receipt.turn,
                )
            else:
                self._rollback_semantic_episode_after_failed_first_media_receipt(
                    response_turn=receipt.turn,
                    status=receipt.status,
                )
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
        if (
            receipt.phase == "response_end"
            and receipt.turn == current_response_turn
            and receipt.epoch == current_response_turn
        ):
            if receipt.status == PlaybackStatus.PLAYED:
                await self._publish_transcript_after_response_end(
                    response_turn=receipt.turn,
                )
            else:
                self._pending_transcripts_by_response_turn.pop(receipt.turn, None)
        if (
            outcome.played
            and self._turn_coordinator.state == TurnLifecycle.AWAITING_PRESENCE
        ):
            self._suspend_original_question()
        if outcome.directive == CoordinatorDirective.HANGUP and self.on_call_complete:
            await self.on_call_complete()
            self._turn_coordinator.mark_ended()

    async def stop(self):
        self._cancel_semantic_episode_settlement()
        self._semantic_episode = None
        self._semantic_episodes_by_response_turn.clear()
        self._pending_transcripts_by_response_turn.clear()
        await super().stop()

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
        context = self._suspended_presence_context
        question = (
            (context.question_text, context.asked_slot)
            if context is not None
            else self._last_played_question
        )
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
        self._suspended_presence_context = None
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

    async def _unavailable_now(self):
        """Replace any pending question with one receipt-gated message request."""
        self._cancel_semantic_episode_settlement()
        async with self._response_lock:
            if self._unavailable_said:
                return
            self._discard_semantic_episode(reason="owner_message_takeover")
            if not self._turn_coordinator.begin_owner_message():
                _log_voice_timing(
                    "controlled_owner_message_suppressed",
                    self._call_sid,
                    reason="invalid_lifecycle",
                )
                return

            self._unavailable_said = True
            self._finish_owner_availability_wait()
            self._pending_reply_slot = ""
            self._presence_reply_pending = False
            self._suspended_presence_context = None
            self._last_played_question = None
            self._playback_question_candidates.clear()

            owner_name = self._contractor_config.get("owner_name", settings.user_name)
            spanish = self._intake_state.language.casefold().startswith("es")
            question = (
                "¿Qué mensaje quiere que le transmita?"
                if spanish
                else "What message would you like me to pass along?"
            )
            message = (
                f"Lo siento, {owner_name} no está disponible ahora. {question}"
                if spanish
                else f"I'm sorry, {owner_name} isn't available right now. {question}"
            )
            self._pending_speech_contract = _PendingSpeechContract(
                expects_input=True,
                asked_slot="message_details",
                question_text=question,
                kind="owner_unavailable",
            )
            self._conversation.append({"role": "assistant", "content": message})
            _log_voice_event(
                "assistant_message_ready",
                self._call_sid,
                source="unavailable",
                chars=len(message),
                words=len(message.split()),
            )
            await self.on_transcript("Kevin", message)
            await self._speak(message, source="unavailable")

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
