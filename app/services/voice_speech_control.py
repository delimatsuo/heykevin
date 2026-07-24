"""Offline, provider-neutral speech authorization for the voice bakeoff.

This module owns policy authorization and opaque render-to-audio correlation only.
`VoiceLifecycle` remains the source of truth for observed playout and terminal
state; candidate adapters must not infer either from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re

from app.services.voice_lifecycle import VoiceSemanticActKind as SemanticActKind
from app.services.voice_lifecycle import VoiceSessionBinding


_IDENTIFIER = re.compile(r"[A-Za-z0-9_]{1,128}")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} is invalid")
    return value


def _word_count(text: str) -> int:
    return len(text.split())


def _act_id(binding: VoiceSessionBinding, turn_id: str, plan_id: str, index: int) -> str:
    material = {
        "binding": {
            "environment": binding.environment,
            "contractor_binding": binding.contractor_binding,
            "call_binding": binding.call_binding,
            "stream_binding": binding.stream_binding,
            "epoch": binding.epoch,
        },
        "turn_id": turn_id,
        "plan_id": plan_id,
        "index": index,
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"act_{digest}"


class FailureClass(str, Enum):
    RECOVERABLE = "recoverable"
    SECURITY = "security"
    UNCERTAIN = "uncertain"
    IRRECOVERABLE = "irrecoverable"


class CancellationReason(str, Enum):
    CALLER_ACTIVITY = "caller_activity"
    INTERRUPTION = "interruption"
    RECONNECT = "reconnect"
    EPOCH_SUPERSEDED = "epoch_superseded"


_TERMINAL_KINDS = {SemanticActKind.CLOSING, SemanticActKind.OPT_OUT, SemanticActKind.VOICEMAIL}


@dataclass(frozen=True, slots=True)
class SemanticAct:
    """A typed act from the future provider-neutral planner, never a prompt."""

    kind: SemanticActKind
    text: str
    question_slot: str | None = None
    private_disclosure: bool = False
    unsupported_promise: bool = False
    complete: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SemanticActKind):
            raise ValueError("semantic act kind is invalid")
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 4_096:
            raise ValueError("semantic act text is invalid")
        if self.kind is SemanticActKind.QUESTION:
            _identifier(self.question_slot, "question slot")
        elif self.question_slot is not None:
            raise ValueError("only a question may reserve a slot")
        if type(self.private_disclosure) is not bool or type(self.unsupported_promise) is not bool or type(self.complete) is not bool:
            raise ValueError("semantic act policy flags are invalid")


@dataclass(frozen=True, slots=True)
class SpokenPlan:
    plan_id: str
    acts: tuple[SemanticAct, ...]

    def __post_init__(self) -> None:
        _identifier(self.plan_id, "plan id")
        if not self.acts or any(not isinstance(act, SemanticAct) for act in self.acts):
            raise ValueError("spoken plan acts are invalid")
        questions = [index for index, act in enumerate(self.acts) if act.kind is SemanticActKind.QUESTION]
        if len(questions) > 1:
            raise ValueError("spoken plan allows one question")
        answers = [index for index, act in enumerate(self.acts) if act.kind is SemanticActKind.ANSWER]
        if questions and answers and min(answers) > questions[0]:
            raise ValueError("direct answer must precede a question")


@dataclass(frozen=True, slots=True)
class SpeechPolicy:
    normal_word_budget: int
    safety_word_budget: int
    required_safety_fragments: tuple[str, ...]
    terminal_fragments: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in (self.normal_word_budget, self.safety_word_budget)):
            raise ValueError("speech budgets must be positive integers")
        if not isinstance(self.required_safety_fragments, tuple) or not self.required_safety_fragments:
            raise ValueError("safety fragments are required")
        if any(not isinstance(fragment, str) or not fragment.strip() for fragment in self.required_safety_fragments):
            raise ValueError("safety fragments are invalid")
        if not isinstance(self.terminal_fragments, tuple) or not self.terminal_fragments:
            raise ValueError("terminal fragments are required")
        if any(not isinstance(fragment, str) or not fragment.strip() for fragment in self.terminal_fragments):
            raise ValueError("terminal fragments are invalid")


@dataclass(frozen=True, slots=True)
class SpeechAuthorization:
    binding: VoiceSessionBinding
    turn_id: str
    authorized_kinds: tuple[SemanticActKind, ...]
    terminal_allowed: bool
    answered_slots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.binding, VoiceSessionBinding):
            raise ValueError("speech binding is invalid")
        _identifier(self.turn_id, "turn id")
        if not self.authorized_kinds or any(not isinstance(kind, SemanticActKind) for kind in self.authorized_kinds):
            raise ValueError("authorized kinds are invalid")
        if len(set(self.authorized_kinds)) != len(self.authorized_kinds) or type(self.terminal_allowed) is not bool:
            raise ValueError("speech authorization is invalid")
        if any(_identifier(slot, "answered slot") != slot for slot in self.answered_slots):
            raise ValueError("answered slots are invalid")


@dataclass(frozen=True, slots=True)
class ReservedSpeech:
    act_id: str
    reservation_id: str | None
    kind: SemanticActKind
    text: str
    binding: VoiceSessionBinding
    turn_id: str


@dataclass(frozen=True, slots=True)
class AudioBinding:
    act_id: str
    text_digest: str
    audio_id: str
    binding: VoiceSessionBinding


@dataclass(frozen=True, slots=True)
class PlayoutBinding:
    act_id: str
    text_digest: str
    audio_id: str
    playout_id: str
    binding: VoiceSessionBinding


@dataclass(frozen=True, slots=True)
class RepairIntent:
    original_act_id: str
    repair_act_id: str
    turn_id: str
    epoch: int
    confirmed_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.original_act_id, "original act id")
        _identifier(self.repair_act_id, "repair act id")
        _identifier(self.turn_id, "repair turn id")
        if type(self.epoch) is not int or self.epoch < 0:
            raise ValueError("repair epoch is invalid")
        if any(_identifier(item, "confirmed fact id") != item for item in self.confirmed_fact_ids):
            raise ValueError("confirmed facts are invalid")


@dataclass(slots=True)
class _Record:
    reserved: ReservedSpeech
    question_slot: str | None = None
    authorized: bool = False
    cancelled: bool = False
    audio: AudioBinding | None = None
    playout: PlayoutBinding | None = None
    streamed_text: str = ""


class SpeechControl:
    """Fail-closed policy reducer; it never calls TTS or a provider."""

    def __init__(self, policy: SpeechPolicy) -> None:
        if not isinstance(policy, SpeechPolicy):
            raise ValueError("speech policy is invalid")
        self.policy = policy
        self._records: dict[str, _Record] = {}
        self._reservation_batches: set[tuple[str, ...]] = set()
        self._reserved_slots: set[tuple[VoiceSessionBinding, str]] = set()
        self._repairs: set[tuple[VoiceSessionBinding, str]] = set()

    def reserve(self, plan: SpokenPlan, authorization: SpeechAuthorization) -> tuple[ReservedSpeech, ...]:
        if not isinstance(plan, SpokenPlan) or not isinstance(authorization, SpeechAuthorization):
            raise ValueError("speech plan or authorization is invalid")
        for index, act in enumerate(plan.acts):
            self._validate_act(act, authorization)
            act_id = _act_id(authorization.binding, authorization.turn_id, plan.plan_id, index)
            if act_id in self._records:
                raise ValueError("speech act is already reserved")
            if act.kind is SemanticActKind.QUESTION:
                assert act.question_slot is not None
                if act.question_slot in authorization.answered_slots:
                    raise ValueError("question slot is already answered")
                if (authorization.binding, act.question_slot) in self._reserved_slots:
                    raise ValueError("question slot is already reserved")
                if not any(prior.kind is SemanticActKind.ANSWER for prior in plan.acts[:index]):
                    raise ValueError("question requires a preceding direct answer")
        reserved: list[ReservedSpeech] = []
        for index, act in enumerate(plan.acts):
            act_id = _act_id(authorization.binding, authorization.turn_id, plan.plan_id, index)
            reservation_id = None
            if act.kind is SemanticActKind.QUESTION:
                assert act.question_slot is not None
                slot_key = (authorization.binding, act.question_slot)
                self._reserved_slots.add(slot_key)
                reservation_id = f"reservation_{act_id}"
            entry = ReservedSpeech(
                act_id=act_id,
                reservation_id=reservation_id,
                kind=act.kind,
                text=act.text,
                binding=authorization.binding,
                turn_id=authorization.turn_id,
            )
            self._records[act_id] = _Record(reserved=entry, question_slot=act.question_slot)
            reserved.append(entry)
        batch = tuple(reserved)
        self._reservation_batches.add(tuple(item.act_id for item in batch))
        return batch

    def rollback_reservation(self, reserved: tuple[ReservedSpeech, ...]) -> bool:
        """Atomically remove one still-pristine reservation batch.

        The bakeoff coordinator uses this only when CallLifecycle rejects the
        matching question reservation. Once any act has advanced, rollback fails
        closed instead of erasing lifecycle evidence.
        """
        if not isinstance(reserved, tuple) or not reserved:
            return False
        act_ids = tuple(item.act_id for item in reserved if isinstance(item, ReservedSpeech))
        if len(act_ids) != len(reserved) or len(set(act_ids)) != len(act_ids) or act_ids not in self._reservation_batches:
            return False
        records: list[_Record] = []
        for item in reserved:
            record = self._records.get(item.act_id)
            if (
                record is None
                or record.reserved != item
                or record.authorized
                or record.cancelled
                or record.audio is not None
                or record.playout is not None
                or record.streamed_text
                or (record.reserved.binding, item.act_id) in self._repairs
            ):
                return False
            records.append(record)
        for record in records:
            self._records.pop(record.reserved.act_id)
            if record.question_slot is not None:
                self._reserved_slots.discard(
                    (record.reserved.binding, record.question_slot)
                )
        self._reservation_batches.discard(act_ids)
        return True

    def authorize_text(self, act_id: str, text: str) -> bool:
        record = self._records.get(act_id)
        if record is None or record.cancelled or record.authorized or text != record.reserved.text:
            return False
        try:
            self._validate_text(record.reserved.kind, text)
        except ValueError:
            return False
        record.authorized = True
        return True

    def accept_segment(self, act_id: str, segment: str, *, final: bool) -> bool:
        """Accept one bounded lower-risk segment before an adapter can send it to TTS."""
        record = self._records.get(act_id)
        if record is None or record.cancelled or not record.authorized:
            return False
        if record.reserved.kind in _TERMINAL_KINDS | {SemanticActKind.QUESTION, SemanticActKind.SAFETY}:
            return False
        if type(final) is not bool or not isinstance(segment, str) or not segment:
            return False
        combined = record.streamed_text + segment
        if not record.reserved.text.startswith(combined):
            return False
        try:
            self._validate_text(record.reserved.kind, combined)
        except ValueError:
            return False
        if final != (combined == record.reserved.text):
            return False
        record.streamed_text = combined
        return True

    def bind_tts(self, act_id: str, *, audio_id: str) -> bool:
        record = self._records.get(act_id)
        if record is None or record.cancelled or not record.authorized or record.audio is not None:
            return False
        try:
            _identifier(audio_id, "audio id")
        except ValueError:
            return False
        record.audio = AudioBinding(
            act_id=act_id,
            text_digest=hashlib.sha256(record.reserved.text.encode("utf-8")).hexdigest(),
            audio_id=audio_id,
            binding=record.reserved.binding,
        )
        return True

    def audio_binding(self, act_id: str) -> AudioBinding | None:
        record = self._records.get(act_id)
        return None if record is None else record.audio

    def bind_playout(self, act_id: str, *, playout_id: str) -> bool:
        record = self._records.get(act_id)
        if record is None or record.cancelled or record.audio is None or record.playout is not None:
            return False
        try:
            _identifier(playout_id, "playout id")
        except ValueError:
            return False
        record.playout = PlayoutBinding(
            act_id=act_id,
            text_digest=record.audio.text_digest,
            audio_id=record.audio.audio_id,
            playout_id=playout_id,
            binding=record.audio.binding,
        )
        return True

    def playout_binding(self, act_id: str) -> PlayoutBinding | None:
        record = self._records.get(act_id)
        return None if record is None else record.playout

    def cancel(self, act_id: str, *, reason: CancellationReason, superseding_epoch: int | None = None) -> bool:
        record = self._records.get(act_id)
        if record is None or record.cancelled or not isinstance(reason, CancellationReason):
            return False
        if reason is CancellationReason.EPOCH_SUPERSEDED and (type(superseding_epoch) is not int or superseding_epoch <= record.reserved.binding.epoch):
            return False
        if reason is not CancellationReason.EPOCH_SUPERSEDED and superseding_epoch is not None:
            return False
        record.cancelled = True
        if record.question_slot is not None:
            self._reserved_slots.discard((record.reserved.binding, record.question_slot))
        return True

    def is_cancelled(self, act_id: str) -> bool:
        record = self._records.get(act_id)
        return record is not None and record.cancelled

    def reserve_repair(self, *, original_act_id: str, failure: FailureClass, plan: SpokenPlan, authorization: SpeechAuthorization, confirmed_fact_ids: tuple[str, ...]) -> RepairIntent | None:
        if not isinstance(failure, FailureClass) or failure is not FailureClass.RECOVERABLE:
            return None
        record = self._records.get(original_act_id)
        if record is None or record.reserved.kind is SemanticActKind.REPAIR or (record.reserved.binding, original_act_id) in self._repairs:
            return None
        if not isinstance(plan, SpokenPlan) or len(plan.acts) != 1 or plan.acts[0].kind is not SemanticActKind.REPAIR or authorization.binding != record.reserved.binding or authorization.turn_id != record.reserved.turn_id:
            return None
        try:
            repair = self.reserve(plan, authorization)[0]
            intent = RepairIntent(
                original_act_id=original_act_id,
                repair_act_id=repair.act_id,
                turn_id=record.reserved.turn_id,
                epoch=record.reserved.binding.epoch,
                confirmed_fact_ids=confirmed_fact_ids,
            )
        except ValueError:
            return None
        self._repairs.add((record.reserved.binding, original_act_id))
        return intent

    def _validate_act(self, act: SemanticAct, authorization: SpeechAuthorization) -> None:
        if act.kind in _TERMINAL_KINDS and not authorization.terminal_allowed:
            raise ValueError("terminal semantic act is not authorized")
        if act.kind not in authorization.authorized_kinds:
            raise ValueError("semantic act is not authorized")
        if act.private_disclosure or act.unsupported_promise or not act.complete:
            raise ValueError("semantic act violates policy")
        self._validate_text(act.kind, act.text)
        if act.kind is SemanticActKind.SAFETY:
            text = act.text.casefold()
            if any(fragment.casefold() not in text for fragment in self.policy.required_safety_fragments):
                raise ValueError("safety semantic act is incomplete")

    def _validate_text(self, kind: SemanticActKind, text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("semantic act text is invalid")
        budget = self.policy.safety_word_budget if kind is SemanticActKind.SAFETY else self.policy.normal_word_budget
        if _word_count(text) > budget:
            raise ValueError("semantic act exceeds speech budget")
        if kind not in _TERMINAL_KINDS and any(fragment.casefold() in text.casefold() for fragment in self.policy.terminal_fragments):
            raise ValueError("terminal wording is not authorized")
