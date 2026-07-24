"""Bakeoff-only coordinator joining shared speech and lifecycle contracts."""

from __future__ import annotations

from app.services.voice_call_lifecycle import CallIntent, CallLifecycle, QuestionIntent
from app.services.voice_speech_control import SpeechAuthorization, SpeechControl, SpokenPlan
from app.services.voice_lifecycle import (
    VoiceEvent,
    VoiceSemanticActKind,
    VoiceTimeoutAuthority,
    VoiceTimeoutIntent,
)


class VoiceBakeoffCoordinator:
    """Provider-neutral coordinator; emits inert lifecycle intents only."""

    def __init__(self, *, speech: SpeechControl, calls: CallLifecycle) -> None:
        self.speech = speech
        self.calls = calls

    def reserve_plan(self, *, plan: SpokenPlan, authorization: SpeechAuthorization, event_id: str, sequence: int, at_ms: int) -> tuple[str, ...]:
        if authorization.binding != self.calls.binding:
            return ()
        try:
            acts = self.speech.reserve(plan, authorization)
        except ValueError:
            return ()
        question = next(
            (
                (planned, reserved)
                for planned, reserved in zip(plan.acts, acts, strict=True)
                if reserved.kind is VoiceSemanticActKind.QUESTION
            ),
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
        if question is not None and not self.calls.reserve_question(binding=authorization.binding, event_id=event_id, sequence=sequence, at_ms=at_ms, question=intent):
            if not self.speech.rollback_reservation(acts):
                raise RuntimeError("speech reservation rollback failed")
            return ()
        return tuple(act.act_id for act in acts)

    def semantic_confirmed(self, *, event: VoiceEvent, event_id: str, sequence: int) -> bool:
        return self.calls.semantic_confirmed(
            event=event,
            event_id=event_id,
            sequence=sequence,
        )

    def caller_playback(self, *, event: VoiceEvent, event_id: str, sequence: int) -> tuple[CallIntent, ...]:
        return self.calls.observed_playback(event_id=event_id, sequence=sequence, event=event)

    def materialize_timeout(
        self,
        *,
        intent: VoiceTimeoutIntent,
        authority: VoiceTimeoutAuthority,
        at_ms: int,
    ) -> VoiceEvent | None:
        """Assign canonical monotonic position and ingest one timeout intent."""
        lifecycle = self.calls.voice_lifecycle
        if (
            not isinstance(intent, VoiceTimeoutIntent)
            or not isinstance(authority, VoiceTimeoutAuthority)
            or intent.binding != lifecycle.binding
            or not authority.authorizes_timeout(intent, now_ms=at_ms)
        ):
            return None
        sequence, canonical_at_ms = lifecycle.next_position(at_ms=at_ms)
        event = intent.event(sequence=sequence, at_ms=canonical_at_ms)
        if not lifecycle.ingest(event):
            return None
        if not authority.accept_timeout(event, lifecycle=lifecycle):
            raise RuntimeError("timeout authority rejected canonical receipt")
        return event
