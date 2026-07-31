"""Pure, revision-bound call-lifecycle reducer for the offline voice bakeoff."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.services.voice_lifecycle import (
    VoiceEvent,
    VoiceLifecycle,
    VoiceSemanticActKind,
    VoiceSessionBinding,
)


class SilencePhase(str, Enum):
    IDLE = "idle"
    QUESTION_RESERVED = "question_reserved"
    QUESTION_CONFIRMED = "question_confirmed"
    FIRST_ARMED = "first_armed"
    PRESENCE_PENDING = "presence_pending"
    SECOND_ARMED = "second_armed"
    MORE_TIME_PENDING = "more_time_pending"
    MORE_TIME_ARMED = "more_time_armed"
    CLOSING_PENDING = "closing_pending"
    TERMINAL_ELIGIBLE = "terminal_eligible"
    TERMINATED = "terminated"


class PlaybackEvidence(str, Enum):
    CALLER_PLAYBACK_OBSERVED = "caller_playback_observed"
    PLAYBACK_INFERRED = "playback_inferred"


class CallIntentKind(str, Enum):
    ARM_TIMER = "arm_timer"
    CANCEL_TIMER = "cancel_timer"
    CANCEL_ACT = "cancel_act"
    REQUEST_PRESENCE_CHECK = "request_presence_check"
    REQUEST_MORE_TIME_ACKNOWLEDGEMENT = (
        "request_more_time_acknowledgement"
    )
    REQUEST_CLOSING = "request_closing"
    TERMINAL_ELIGIBLE = "terminal_eligible"


@dataclass(frozen=True, slots=True)
class CallIntent:
    kind: CallIntentKind
    binding: VoiceSessionBinding
    revision: int
    action_id: str
    act_id: str | None = None
    deadline_ms: int | None = None
    turn_id: str | None = None
    turn_sequence: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, CallIntentKind)
            or not isinstance(self.binding, VoiceSessionBinding)
            or type(self.revision) is not int
            or self.revision < 0
            or not _id(self.action_id)
            or (self.act_id is not None and not _id(self.act_id))
            or (
                self.deadline_ms is not None
                and (
                    type(self.deadline_ms) is not int
                    or self.deadline_ms < 0
                )
            )
            or (self.turn_id is not None and not _id(self.turn_id))
            or (
                self.turn_sequence is not None
                and (
                    type(self.turn_sequence) is not int
                    or self.turn_sequence < 0
                )
            )
            or (
                self.turn_id is None
                and self.turn_sequence is not None
            )
        ):
            raise ValueError("call intent is invalid")


@dataclass(frozen=True, slots=True)
class QuestionIntent:
    slot: str
    turn_id: str
    act_id: str
    turn_sequence: int | None = None

    def __post_init__(self) -> None:
        if (
            not all(
                _id(value)
                for value in (
                    self.slot,
                    self.turn_id,
                    self.act_id,
                )
            )
            or (
                self.turn_sequence is not None
                and (
                    type(self.turn_sequence) is not int
                    or self.turn_sequence < 0
                )
            )
        ):
            raise ValueError("question intent is invalid")


class CallLifecycle:
    """Reducer only: no provider calls, timers, state writes, or terminal execution."""

    def __init__(
        self,
        *,
        binding: VoiceSessionBinding,
        voice_lifecycle: VoiceLifecycle,
        first_silence_ms: int,
        second_silence_ms: int,
        inference_delay_ms: int = 1,
        more_time_extension_ms: int = 20_000,
    ) -> None:
        if not isinstance(binding, VoiceSessionBinding) or any(type(value) is not int or value < 1 for value in (first_silence_ms, second_silence_ms, inference_delay_ms, more_time_extension_ms)):
            raise ValueError("call lifecycle configuration is invalid")
        self.binding = binding
        if not isinstance(voice_lifecycle, VoiceLifecycle) or voice_lifecycle.binding != binding:
            raise ValueError("canonical lifecycle binding is invalid")
        self.voice_lifecycle = voice_lifecycle
        self.first_silence_ms = first_silence_ms
        self.second_silence_ms = second_silence_ms
        self.inference_delay_ms = inference_delay_ms
        self.more_time_extension_ms = more_time_extension_ms
        self.phase = SilencePhase.IDLE
        self.revision = 0
        self._question: QuestionIntent | None = None
        self._silence_origin_act_id: str | None = None
        self._silence_turn_id: str | None = None
        self._silence_turn_sequence: int | None = None
        self._presence_id: str | None = None
        self._presence_delivered = False
        self._more_time_used = False
        self._more_time_ack_id: str | None = None
        self._closing_id: str | None = None
        self._deadline_ms: int | None = None
        self._timer_intent: CallIntent | None = None
        self._seen_events: set[str] = set()
        self._sequence = -1
        self._at_ms = -1
        self._transport: dict[str, tuple[int, str]] = {}
        self._issued_intents: dict[str, CallIntent] = {}
        self._materialized_act_ids: set[str] = set()

    def reserve_question(
        self,
        *,
        binding: VoiceSessionBinding,
        event_id: str,
        sequence: int,
        at_ms: int,
        question: QuestionIntent,
    ) -> bool:
        """Reserve one typed question without raising or partially mutating."""
        if self.phase is not SilencePhase.IDLE or not isinstance(question, QuestionIntent) or not self._accept(binding, event_id, sequence, at_ms):
            return False
        self._question = question
        self._silence_origin_act_id = question.act_id
        self._silence_turn_id = question.turn_id
        self._silence_turn_sequence = question.turn_sequence
        self._presence_delivered = False
        self._more_time_used = False
        self.phase = SilencePhase.QUESTION_RESERVED
        return True

    def rollback_question_reservation(
        self,
        *,
        binding: VoiceSessionBinding,
        question: QuestionIntent,
    ) -> bool:
        """Clear only the exact pristine question reservation."""
        if binding != self.binding or not isinstance(question, QuestionIntent) or self.phase is not SilencePhase.QUESTION_RESERVED or self._question != question or self._deadline_ms is not None or self._transport:
            return False
        self._clear_silence_cycle()
        self.phase = SilencePhase.IDLE
        return True

    def semantic_confirmed(self, *, event_id: str, sequence: int, event: VoiceEvent) -> bool:
        if not self.can_semantic_confirm(event) or not self.can_accept_position(
            binding=event.binding,
            event_id=event_id,
            sequence=sequence,
            at_ms=event.at_ms,
        ):
            return False
        self._accept(event.binding, event_id, sequence, event.at_ms)
        self.phase = SilencePhase.QUESTION_CONFIRMED
        return True

    def can_semantic_confirm(self, event: VoiceEvent) -> bool:
        question = self._question
        return (
            self.phase is SilencePhase.QUESTION_RESERVED
            and question is not None
            and isinstance(event, VoiceEvent)
            and event.semantic_act_id == question.act_id
            and event.input_turn_id == question.turn_id
            and event.semantic_act_kind is VoiceSemanticActKind.QUESTION
            and self.voice_lifecycle.accepts_semantic_confirmation(event)
        )

    def can_accept_position(
        self,
        *,
        binding: VoiceSessionBinding,
        event_id: str,
        sequence: int,
        at_ms: int,
    ) -> bool:
        return (
            binding == self.binding
            and _id(event_id)
            and event_id not in self._seen_events
            and type(sequence) is int
            and sequence > self._sequence
            and type(at_ms) is int
            and at_ms >= self._at_ms
        )

    def next_position(self, *, at_ms: int) -> tuple[int, int]:
        """Return the next valid local reducer position without mutating state."""
        if type(at_ms) is not int or at_ms < 0:
            raise ValueError("call lifecycle timestamp is invalid")
        return self._sequence + 1, max(self._at_ms, at_ms)

    @property
    def is_quiescent(self) -> bool:
        return (
            self.phase in {
                SilencePhase.IDLE,
                SilencePhase.TERMINATED,
            }
            and self._question is None
            and self._silence_origin_act_id is None
            and self._silence_turn_id is None
            and self._silence_turn_sequence is None
            and self._presence_id is None
            and self._more_time_ack_id is None
            and self._closing_id is None
            and self._deadline_ms is None
            and self._timer_intent is None
            and not self._transport
            and not self._issued_intents
            and not self._materialized_act_ids
        )

    def hard_terminalize(self) -> tuple[CallIntent, ...]:
        """Permanently seal ambiguous local timer and act authority."""
        intents: list[CallIntent] = []
        if self._deadline_ms is not None:
            intents.append(
                self._intent(
                    CallIntentKind.CANCEL_TIMER,
                    f"hard_cancel_timer_{self.revision}",
                    act_id=(
                        self._question.act_id
                        if self._question is not None
                        else None
                    ),
                )
            )
        for act_id in (
            (
                self._question.act_id
                if self._question is not None
                else None
            ),
            self._presence_id,
            self._more_time_ack_id,
            self._closing_id,
        ):
            if act_id is not None:
                intents.append(
                    self._intent(
                        CallIntentKind.CANCEL_ACT,
                        f"hard_cancel_{act_id}_{self.revision}",
                        act_id=act_id,
                    )
                )
        self._clear_silence_cycle()
        self.phase = SilencePhase.TERMINATED
        self.revision += 1
        return tuple(intents)

    def consume_intent(
        self,
        intent: CallIntent,
        *,
        materialized_act_id: str | None = None,
    ) -> bool:
        """Consume one exact inert lifecycle intent before act materialization."""
        if (
            not isinstance(intent, CallIntent)
            or (
                materialized_act_id is not None
                and not _id(materialized_act_id)
            )
        ):
            return False
        expected = self._issued_intents.pop(
            intent.action_id,
            None,
        )
        accepted = (
            expected is intent
            and intent.binding == self.binding
            and intent.revision == self.revision
            and (
                (
                    intent.kind
                    is CallIntentKind.REQUEST_PRESENCE_CHECK
                    and self.phase is SilencePhase.PRESENCE_PENDING
                    and intent.act_id == self._presence_id
                )
                or (
                    intent.kind
                    is CallIntentKind.REQUEST_MORE_TIME_ACKNOWLEDGEMENT
                    and self.phase is SilencePhase.MORE_TIME_PENDING
                    and intent.act_id == self._more_time_ack_id
                )
                or (
                    intent.kind is CallIntentKind.REQUEST_CLOSING
                    and self.phase is SilencePhase.CLOSING_PENDING
                    and intent.act_id == self._closing_id
                )
            )
        )
        if accepted and intent.act_id is not None:
            actual_act_id = materialized_act_id or intent.act_id
            if (
                intent.kind
                is CallIntentKind.REQUEST_PRESENCE_CHECK
            ):
                self._presence_id = actual_act_id
            elif (
                intent.kind
                is CallIntentKind.REQUEST_MORE_TIME_ACKNOWLEDGEMENT
            ):
                self._more_time_ack_id = actual_act_id
            else:
                self._closing_id = actual_act_id
            self._materialized_act_ids.add(actual_act_id)
        elif expected is not None:
            self._clear_silence_cycle()
            self.phase = SilencePhase.TERMINATED
            self.revision += 1
        return accepted

    def terminalize(self, intent: CallIntent) -> bool:
        """Consume one exact local terminal-eligibility receipt."""
        if not isinstance(intent, CallIntent):
            return False
        expected = self._issued_intents.pop(
            intent.action_id,
            None,
        )
        accepted = (
            expected is intent
            and intent.kind is CallIntentKind.TERMINAL_ELIGIBLE
            and intent.binding == self.binding
            and intent.revision == self.revision
            and self.phase is SilencePhase.TERMINAL_ELIGIBLE
        )
        if not accepted:
            if expected is not None:
                self._clear_silence_cycle()
                self.phase = SilencePhase.TERMINATED
                self.revision += 1
            return False
        self._clear_silence_cycle()
        self.phase = SilencePhase.TERMINATED
        self.revision += 1
        return True

    def timer_receipt(self) -> CallIntent | None:
        """Expose only the exact currently armed inert timer receipt."""
        if self.phase not in {
            SilencePhase.FIRST_ARMED,
            SilencePhase.SECOND_ARMED,
            SilencePhase.MORE_TIME_ARMED,
        }:
            return None
        return self._timer_intent

    def transport_resolved(self, *, event_id: str, sequence: int, event: VoiceEvent) -> bool:
        act_id = event.semantic_act_id
        expected_question = self._question is not None and act_id == self._question.act_id and event.input_turn_id == self._question.turn_id and self.phase is SilencePhase.QUESTION_CONFIRMED and event.semantic_act_kind is VoiceSemanticActKind.QUESTION
        expected_presence = (
            act_id == self._presence_id
            and self.phase is SilencePhase.PRESENCE_PENDING
            and event.semantic_act_kind
            is VoiceSemanticActKind.PRESENCE_CHECK
            and act_id in self._materialized_act_ids
        )
        expected_more_time = (
            act_id == self._more_time_ack_id
            and self.phase is SilencePhase.MORE_TIME_PENDING
            and event.semantic_act_kind
            is VoiceSemanticActKind.ACKNOWLEDGEMENT
            and act_id in self._materialized_act_ids
        )
        expected_closing = (
            act_id == self._closing_id
            and self.phase is SilencePhase.CLOSING_PENDING
            and event.semantic_act_kind is VoiceSemanticActKind.CLOSING
            and act_id in self._materialized_act_ids
        )
        expected = (
            expected_question
            or expected_presence
            or expected_more_time
            or expected_closing
        )
        if not expected or not self.voice_lifecycle.accepts_transport_resolution(event) or not self._accept(event.binding, event_id, sequence, event.at_ms):
            return False
        self._transport[act_id] = (event.at_ms, event.payload.playout_id or "")
        return True

    def playback(
        self,
        *,
        binding: VoiceSessionBinding,
        event_id: str,
        sequence: int,
        act_id: str,
        evidence: PlaybackEvidence,
        at_ms: int,
        inference_id: str | None = None,
        transport_id: str | None = None,
    ) -> tuple[CallIntent, ...]:
        if not isinstance(evidence, PlaybackEvidence) or evidence is not PlaybackEvidence.PLAYBACK_INFERRED:
            return ()
        if evidence is PlaybackEvidence.PLAYBACK_INFERRED:
            transport = self._transport.get(act_id)
            active = (
                (
                    self._question is not None
                    and act_id == self._question.act_id
                    and self.phase is SilencePhase.QUESTION_CONFIRMED
                )
                or (
                    act_id == self._presence_id
                    and self.phase is SilencePhase.PRESENCE_PENDING
                    and act_id in self._materialized_act_ids
                )
                or (
                    act_id == self._more_time_ack_id
                    and self.phase is SilencePhase.MORE_TIME_PENDING
                    and act_id in self._materialized_act_ids
                )
                or (
                    act_id == self._closing_id
                    and self.phase is SilencePhase.CLOSING_PENDING
                    and act_id in self._materialized_act_ids
                )
            )
            if not active or transport is None or not _id(inference_id) or transport_id != transport[1] or at_ms < transport[0] + self.inference_delay_ms:
                return ()
        if not self._accept(binding, event_id, sequence, at_ms):
            return ()
        if evidence is PlaybackEvidence.PLAYBACK_INFERRED:
            self._transport.pop(act_id, None)
        if self._question is not None and act_id == self._question.act_id and self.phase is SilencePhase.QUESTION_CONFIRMED:
            return self._arm(SilencePhase.FIRST_ARMED, act_id, at_ms + self.first_silence_ms)
        if act_id == self._presence_id and self.phase is SilencePhase.PRESENCE_PENDING:
            self._materialized_act_ids.discard(act_id)
            self._presence_id = None
            self._presence_delivered = True
            return self._arm(SilencePhase.SECOND_ARMED, act_id, at_ms + self.second_silence_ms)
        if (
            act_id == self._more_time_ack_id
            and self.phase is SilencePhase.MORE_TIME_PENDING
        ):
            self._materialized_act_ids.discard(act_id)
            self._more_time_ack_id = None
            return self._arm(
                SilencePhase.MORE_TIME_ARMED,
                act_id,
                at_ms + self.more_time_extension_ms,
            )
        if act_id == self._closing_id and self.phase is SilencePhase.CLOSING_PENDING:
            self._materialized_act_ids.discard(act_id)
            self._closing_id = None
            self.phase = SilencePhase.TERMINAL_ELIGIBLE
            return (self._issue_intent(CallIntentKind.TERMINAL_ELIGIBLE, f"terminal_{act_id}", act_id=act_id),)
        return ()

    def observed_playback(self, *, event_id: str, sequence: int, event: VoiceEvent) -> tuple[CallIntent, ...]:
        expected_kind = (
            "question"
            if self._question is not None
            and event.semantic_act_id == self._question.act_id
            and event.input_turn_id == self._question.turn_id
            else "presence_check"
            if event.semantic_act_id == self._presence_id
            else "acknowledgement"
            if event.semantic_act_id == self._more_time_ack_id
            else "closing"
            if event.semantic_act_id == self._closing_id
            else ""
        )
        active = (
            (
                expected_kind == "question"
                and self.phase is SilencePhase.QUESTION_CONFIRMED
            )
            or (
                expected_kind == "presence_check"
                and self.phase is SilencePhase.PRESENCE_PENDING
                and event.semantic_act_id in self._materialized_act_ids
            )
            or (
                expected_kind == "acknowledgement"
                and self.phase is SilencePhase.MORE_TIME_PENDING
                and event.semantic_act_id in self._materialized_act_ids
            )
            or (
                expected_kind == "closing"
                and self.phase is SilencePhase.CLOSING_PENDING
                and event.semantic_act_id in self._materialized_act_ids
            )
        )
        if not active or event.semantic_act_kind.value != expected_kind or not self.voice_lifecycle.accepts_caller_playback(event) or not self._accept(event.binding, event_id, sequence, event.at_ms):
            return ()
        act_id = event.semantic_act_id
        if self._question is not None and act_id == self._question.act_id and self.phase is SilencePhase.QUESTION_CONFIRMED:
            return self._arm(SilencePhase.FIRST_ARMED, act_id, event.at_ms + self.first_silence_ms)
        if act_id == self._presence_id and self.phase is SilencePhase.PRESENCE_PENDING:
            self._materialized_act_ids.discard(act_id)
            self._presence_id = None
            self._presence_delivered = True
            return self._arm(SilencePhase.SECOND_ARMED, act_id, event.at_ms + self.second_silence_ms)
        if (
            act_id == self._more_time_ack_id
            and self.phase is SilencePhase.MORE_TIME_PENDING
        ):
            self._materialized_act_ids.discard(act_id)
            self._more_time_ack_id = None
            return self._arm(
                SilencePhase.MORE_TIME_ARMED,
                act_id,
                event.at_ms + self.more_time_extension_ms,
            )
        if act_id == self._closing_id and self.phase is SilencePhase.CLOSING_PENDING:
            self._materialized_act_ids.discard(act_id)
            self._closing_id = None
            self.phase = SilencePhase.TERMINAL_ELIGIBLE
            return (self._issue_intent(CallIntentKind.TERMINAL_ELIGIBLE, f"terminal_{act_id}", act_id=act_id),)
        return ()

    def timer_fired(
        self,
        *,
        binding: VoiceSessionBinding,
        event_id: str,
        sequence: int,
        action_id: str,
        revision: int,
        now_ms: int,
    ) -> tuple[CallIntent, ...]:
        timer_intent = self._timer_intent
        if self.phase not in {
            SilencePhase.FIRST_ARMED,
            SilencePhase.SECOND_ARMED,
            SilencePhase.MORE_TIME_ARMED,
        } or timer_intent is None or revision != self.revision or self._deadline_ms is None or now_ms < self._deadline_ms or action_id != timer_intent.action_id or revision != timer_intent.revision or not self._accept(binding, event_id, sequence, now_ms):
            return ()
        self._deadline_ms = None
        self._timer_intent = None
        if self.phase is SilencePhase.FIRST_ARMED:
            self._supersede_question()
            self.phase = SilencePhase.PRESENCE_PENDING
            self._presence_id = f"presence_{self.revision}"
            return (
                self._issue_intent(
                    CallIntentKind.REQUEST_PRESENCE_CHECK,
                    self._presence_id,
                    act_id=self._presence_id,
                ),
            )
        if self.phase is SilencePhase.SECOND_ARMED:
            self.phase = SilencePhase.CLOSING_PENDING
            self._closing_id = f"closing_{self.revision}"
            return (self._issue_intent(CallIntentKind.REQUEST_CLOSING, self._closing_id, act_id=self._closing_id),)
        if self.phase is SilencePhase.MORE_TIME_ARMED:
            if not self._presence_delivered:
                self._supersede_question()
                self.phase = SilencePhase.PRESENCE_PENDING
                self._presence_id = f"presence_{self.revision}"
                return (
                    self._issue_intent(
                        CallIntentKind.REQUEST_PRESENCE_CHECK,
                        self._presence_id,
                        act_id=self._presence_id,
                    ),
                )
            self.phase = SilencePhase.CLOSING_PENDING
            self._closing_id = f"closing_{self.revision}"
            return (
                self._issue_intent(
                    CallIntentKind.REQUEST_CLOSING,
                    self._closing_id,
                    act_id=self._closing_id,
                ),
            )
        return ()

    def request_more_time(
        self,
        *,
        binding: VoiceSessionBinding,
        event_id: str,
        sequence: int,
        at_ms: int,
    ) -> tuple[CallIntent, ...]:
        """Accept one bounded extension without moving it on later requests."""
        if type(at_ms) is not int:
            return ()
        eligible = self.phase in {
            SilencePhase.FIRST_ARMED,
            SilencePhase.SECOND_ARMED,
            SilencePhase.MORE_TIME_PENDING,
            SilencePhase.MORE_TIME_ARMED,
        }
        deadline_expired = (
            self.phase
            in {
                SilencePhase.FIRST_ARMED,
                SilencePhase.SECOND_ARMED,
                SilencePhase.MORE_TIME_ARMED,
            }
            and self._deadline_ms is not None
            and at_ms > self._deadline_ms
        )
        if (
            not eligible
            or deadline_expired
            or not self.can_accept_position(
                binding=binding,
                event_id=event_id,
                sequence=sequence,
                at_ms=at_ms,
            )
        ):
            return ()
        if not self._accept(
            binding,
            event_id,
            sequence,
            at_ms,
        ):
            return ()
        if self._more_time_used:
            return ()
        if self._deadline_ms is None:
            return ()
        cancel = self._intent(
            CallIntentKind.CANCEL_TIMER,
            f"more_time_cancel_{self.revision}",
            act_id=self._silence_origin_act_id,
        )
        self._deadline_ms = None
        self._timer_intent = None
        self._more_time_used = True
        self._more_time_ack_id = (
            f"more_time_{self.revision}_{sequence}"
        )
        self.phase = SilencePhase.MORE_TIME_PENDING
        request = self._issue_intent(
            CallIntentKind.REQUEST_MORE_TIME_ACKNOWLEDGEMENT,
            self._more_time_ack_id,
            act_id=self._more_time_ack_id,
        )
        return cancel, request

    def cancel(
        self, *, binding: VoiceSessionBinding, event_id: str, sequence: int, at_ms: int
    ) -> tuple[CallIntent, ...]:
        if type(at_ms) is not int:
            return ()
        deadline_expired = (
            self.phase
            in {
                SilencePhase.FIRST_ARMED,
                SilencePhase.SECOND_ARMED,
                SilencePhase.MORE_TIME_ARMED,
            }
            and self._deadline_ms is not None
            and at_ms > self._deadline_ms
        )
        if (
            deadline_expired
            or not self._accept(
                binding,
                event_id,
                sequence,
                at_ms,
            )
            or self.phase is SilencePhase.TERMINATED
        ):
            return ()
        intents: list[CallIntent] = []
        if self._deadline_ms is not None:
            intents.append(
                self._intent(
                    CallIntentKind.CANCEL_TIMER,
                    f"cancel_timer_{self.revision}",
                    act_id=self._question.act_id if self._question is not None else None,
                )
            )
        for act_id in (
            self._question.act_id if self._question is not None else None,
            self._presence_id,
            self._more_time_ack_id,
            self._closing_id,
        ):
            if act_id is not None:
                intents.append(self._intent(CallIntentKind.CANCEL_ACT, f"cancel_{act_id}_{self.revision}", act_id=act_id))
        self._clear_silence_cycle()
        self.phase = SilencePhase.IDLE
        self.revision += 1
        return tuple(intents)

    def _arm(self, phase: SilencePhase, act_id: str, deadline_ms: int) -> tuple[CallIntent, ...]:
        self.phase, self._deadline_ms = phase, deadline_ms
        self._timer_intent = self._intent(
            CallIntentKind.ARM_TIMER,
            self._timer_action_id(),
            act_id=act_id,
            deadline_ms=deadline_ms,
        )
        return (self._timer_intent,)

    def _supersede_question(self) -> None:
        if self._question is not None:
            self._transport.pop(self._question.act_id, None)
        self._question = None
        self.revision += 1

    def _clear_silence_cycle(self) -> None:
        self._deadline_ms = None
        self._question = None
        self._silence_origin_act_id = None
        self._silence_turn_id = None
        self._silence_turn_sequence = None
        self._presence_id = None
        self._presence_delivered = False
        self._more_time_used = False
        self._more_time_ack_id = None
        self._closing_id = None
        self._transport.clear()
        self._timer_intent = None
        self._issued_intents.clear()
        self._materialized_act_ids.clear()

    def _timer_action_id(self) -> str:
        return f"timer_{self.revision}_{self._deadline_ms}"

    def _intent(
        self,
        kind: CallIntentKind,
        action_id: str,
        *,
        act_id: str | None = None,
        deadline_ms: int | None = None,
    ) -> CallIntent:
        return CallIntent(
            kind,
            self.binding,
            self.revision,
            action_id,
            act_id,
            deadline_ms,
            self._silence_turn_id,
            self._silence_turn_sequence,
        )

    def _issue_intent(
        self,
        kind: CallIntentKind,
        action_id: str,
        *,
        act_id: str,
    ) -> CallIntent:
        intent = self._intent(
            kind,
            action_id,
            act_id=act_id,
        )
        self._issued_intents[action_id] = intent
        return intent

    def _accept(self, binding: VoiceSessionBinding, event_id: str, sequence: int, at_ms: int) -> bool:
        if not self.can_accept_position(
            binding=binding,
            event_id=event_id,
            sequence=sequence,
            at_ms=at_ms,
        ):
            return False
        self._seen_events.add(event_id)
        self._sequence, self._at_ms = sequence, at_ms
        return True


def _id(value: object) -> bool:
    return isinstance(value, str) and value and len(value) <= 128 and value.replace("_", "").isalnum()
