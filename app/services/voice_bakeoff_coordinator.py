"""Bakeoff-only coordinator joining shared speech and lifecycle contracts."""

from __future__ import annotations

from app.services.voice_call_lifecycle import CallIntent, CallLifecycle, QuestionIntent
from app.services.voice_lifecycle import (
    VoiceEvent,
    VoiceSemanticActKind,
)
from app.services.voice_speech_control import (
    ReservedSpeech,
    SpeechAuthorization,
    SpeechControl,
    SpokenPlan,
)


class VoiceBakeoffCoordinator:
    """Provider-neutral coordinator; emits inert lifecycle intents only."""

    def __init__(
        self,
        *,
        speech: SpeechControl,
        calls: CallLifecycle,
    ) -> None:
        self.speech = speech
        self.calls = calls
        self._reserved_questions: dict[tuple[str, ...], QuestionIntent] = {}

    def reserve_plan(
        self,
        *,
        plan: SpokenPlan,
        authorization: SpeechAuthorization,
        event_id: str,
        sequence: int,
        at_ms: int,
    ) -> tuple[str, ...]:
        return tuple(
            act.act_id
            for act in self.reserve_batch(
                plan=plan,
                authorization=authorization,
                event_id=event_id,
                sequence=sequence,
                at_ms=at_ms,
            )
        )

    def reserve_batch(
        self,
        *,
        plan: SpokenPlan,
        authorization: SpeechAuthorization,
        event_id: str,
        sequence: int,
        at_ms: int,
    ) -> tuple[ReservedSpeech, ...]:
        if authorization.binding != self.calls.binding:
            return ()
        try:
            acts = self.speech.reserve(plan, authorization)
        except ValueError:
            return ()
        question = next(
            ((planned, reserved) for planned, reserved in zip(plan.acts, acts, strict=True) if reserved.kind is VoiceSemanticActKind.QUESTION),
            None,
        )
        if question is not None:
            planned, reserved = question
            assert planned.question_slot is not None
            intent = QuestionIntent(
                slot=planned.question_slot,
                turn_id=reserved.turn_id,
                act_id=reserved.act_id,
            )
        if question is not None and not self.calls.reserve_question(
            binding=authorization.binding,
            event_id=event_id,
            sequence=sequence,
            at_ms=at_ms,
            question=intent,
        ):
            if not self.speech.rollback_reservation(acts):
                raise RuntimeError("speech reservation rollback failed")
            return ()
        if question is not None:
            self._reserved_questions[tuple(act.act_id for act in acts)] = intent
        return acts

    def rollback_batch(self, reserved: tuple[ReservedSpeech, ...]) -> bool:
        batch_key = tuple(item.act_id for item in reserved)
        question = self._reserved_questions.get(batch_key)
        if question is not None and not self.calls.rollback_question_reservation(
            binding=reserved[0].binding,
            question=question,
        ):
            return False
        if self.speech.rollback_reservation(reserved):
            self._reserved_questions.pop(batch_key, None)
            return True
        if question is not None:
            raise RuntimeError("question rollback succeeded but speech rollback failed")
        return False

    def rollback_pristine_question(
        self,
        reserved: tuple[ReservedSpeech, ...],
    ) -> bool:
        """Clear only the call-side question for a batch being compensated."""
        batch_key = tuple(item.act_id for item in reserved)
        question = self._reserved_questions.get(batch_key)
        if question is None:
            return True
        if not self.calls.rollback_question_reservation(
            binding=reserved[0].binding,
            question=question,
        ):
            return False
        self._reserved_questions.pop(batch_key, None)
        return True

    def complete_batch(self, reserved: tuple[ReservedSpeech, ...]) -> bool:
        batch_key = tuple(item.act_id for item in reserved)
        self._reserved_questions.pop(batch_key, None)
        return self.speech.complete_reservation(reserved)

    def retire_batch(self, reserved: tuple[ReservedSpeech, ...]) -> bool:
        batch_key = tuple(item.act_id for item in reserved)
        self._reserved_questions.pop(batch_key, None)
        return self.speech.retire_reservation(reserved)

    def semantic_confirmed(self, *, event: VoiceEvent, event_id: str, sequence: int) -> bool:
        return self.calls.semantic_confirmed(
            event=event,
            event_id=event_id,
            sequence=sequence,
        )

    def caller_playback(self, *, event: VoiceEvent, event_id: str, sequence: int) -> tuple[CallIntent, ...]:
        return self.calls.observed_playback(event_id=event_id, sequence=sequence, event=event)
