"""Offline-only, one-shot unsupported-language recovery.

This module proves data, ordering, bounded-state, and cleanup contracts only.
It never renders audio, calls a provider, opens a network path, or claims that
the fixed multilingual asset was heard or understood by a caller.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from threading import RLock

from app.services.voice_bakeoff_closure import (
    ClosureTrigger,
    OfflineAuthorityInventory,
)
from app.services.voice_lifecycle import (
    VoiceEvent,
    VoiceEventKind,
    VoiceLifecycle,
    VoiceSemanticActKind,
    VoiceSessionBinding,
)
from app.services.voice_session_auth import CandidateArm
from app.services.voice_speech_control import (
    CancellationReason,
    ReservedSpeech,
    SemanticAct,
    SpeechControl,
    SpokenPlan,
)

_DESCRIPTOR_DOMAIN = b"hey-kevin/offline-language-choice-asset/v1\x00"
_RECOVERY_RECEIPT_DOMAIN = (
    b"hey-kevin/offline-language-recovery-final-receipt/v1\x00"
)
_RECOVERY_ADMISSION_DOMAIN = (
    b"hey-kevin/offline-language-recovery-admission/v1\x00"
)
_TERMINAL_RECEIPT_DOMAIN = (
    b"hey-kevin/offline-language-choice-terminal-receipt/v1\x00"
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]{1,128}")
_DIGEST = re.compile(r"[0-9a-f]{64}")

LANGUAGE_CHOICE_SCHEMA_VERSION = 1
LANGUAGE_CHOICE_ASSET_ID = "unsupported_language_response_v1"
LANGUAGE_CHOICE_ASSET_LOCALE = "mul"
LANGUAGE_CHOICE_RESPONSE_MS = 10_000
LANGUAGE_CHOICE_MAX_SPEECH_MS = 15_000
LANGUAGE_CHOICE_FINALIZATION_MS = 2_000
LANGUAGE_CHOICE_PAUSES_MS = (250, 250)
LANGUAGE_CHOICE_TEXT = (
    (
        "en",
        (
            "This test can continue only in English, Spanish, or Mandarin. "
            "Please respond in one of those languages."
        ),
    ),
    (
        "es",
        (
            "Esta prueba solo puede continuar en inglés, español o mandarín. "
            "Responda en uno de esos idiomas."
        ),
    ),
    (
        "zh",
        "本次测试只能使用英语、西班牙语或普通话。请使用其中一种语言回答。",
    ),
)
LANGUAGE_CHOICE_TEXT_DIGESTS = (
    "fa2ef0f88a82bb845ae31a47a7f8a6092fc374973c2d9244b1c6cfa403eebf10",
    "7d045720baf2dad66a05813c1f971f02cd1667877007a878b510e62ae033078d",
    "f6373e32485d9ac35138e52c860de716567a158ec9fbf7509236d20b76589d6e",
)
LANGUAGE_CHOICE_DESCRIPTOR_BYTES = 528
LANGUAGE_CHOICE_DESCRIPTOR_DIGEST = (
    "f840fd799016c0f3369e8c4894e15abfa58138d5f99e82e1d9585c4d00a3d622"
)
QUALIFIED_RECOVERY_LOCALES = frozenset({"en", "es", "zh"})
UNLISTED_CHALLENGE_LOCALES = frozenset({"fr_fr", "ar_msa"})


def _exact_nonnegative(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative exact integer")
    return value


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _nfc(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} is invalid")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{name} must already be NFC")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    return value


def _u8(value: int) -> bytes:
    if not 0 <= value <= 0xFF:
        raise ValueError("u8 value is out of range")
    return value.to_bytes(1, "big")


def _u16(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError("u16 value is out of range")
    return value.to_bytes(2, "big")


def _u32(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("u32 value is out of range")
    return value.to_bytes(4, "big")


def _str16(value: str, *, encoding: str) -> bytes:
    encoded = value.encode(encoding)
    return _u16(len(encoded)) + encoded


def _str32(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _u32(len(encoded)) + encoded


def _binding_bytes(binding: VoiceSessionBinding) -> bytes:
    return b"".join(
        (
            _str16(binding.environment, encoding="utf-8"),
            _str16(binding.contractor_binding, encoding="utf-8"),
            _str16(binding.call_binding, encoding="utf-8"),
            _str16(binding.stream_binding, encoding="utf-8"),
            binding.epoch.to_bytes(8, "big"),
        )
    )


class AdmissionPurpose(str, Enum):
    ORDINARY = "ordinary"
    LANGUAGE_RECOVERY = "language_recovery"


class LanguageChoicePhase(str, Enum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    PRESENTING = "presenting"
    CLEANUP_PENDING = "cleanup_pending"
    RESPONSE_WINDOW = "response_window"
    ACTIVITY_OPEN = "activity_open"
    FINALIZING = "finalizing"
    RECOVERY_PENDING = "recovery_pending"
    RECOVERY_CONSUMED = "recovery_consumed"
    RECOVERY_VALIDATED = "recovery_validated"
    RECOVERED = "recovered"
    TERMINAL = "terminal"


class LanguageFinalDisposition(str, Enum):
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    REJECTED = "rejected"


class LanguageChoiceTerminalOutcome(str, Enum):
    NO_AUDIO_TEARDOWN = "no_audio_teardown"


@dataclass(frozen=True, slots=True)
class LanguageChoiceSegment:
    ordinal: int
    locale: str
    text: str
    text_digest: str

    def __post_init__(self) -> None:
        _exact_nonnegative(self.ordinal, "segment ordinal")
        if self.locale not in {"en", "es", "zh"}:
            raise ValueError("segment locale is invalid")
        text = _nfc(self.text, "segment text")
        digest = _digest(self.text_digest, "segment text digest")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
            raise ValueError("segment text digest does not match")


@dataclass(frozen=True, slots=True)
class LanguageChoiceDescriptor:
    schema_version: int
    asset_id: str
    semantic_kind: VoiceSemanticActKind
    asset_locale: str
    segments: tuple[LanguageChoiceSegment, ...]
    pauses_ms: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != LANGUAGE_CHOICE_SCHEMA_VERSION
            or _identifier(self.asset_id, "asset id")
            != LANGUAGE_CHOICE_ASSET_ID
            or self.semantic_kind is not VoiceSemanticActKind.LANGUAGE_CHOICE
            or self.asset_locale != LANGUAGE_CHOICE_ASSET_LOCALE
            or type(self.segments) is not tuple
            or len(self.segments) != 3
            or any(
                type(segment) is not LanguageChoiceSegment
                for segment in self.segments
            )
            or tuple(segment.ordinal for segment in self.segments)
            != (0, 1, 2)
            or tuple(segment.locale for segment in self.segments)
            != ("en", "es", "zh")
            or tuple(
                (segment.text, segment.text_digest)
                for segment in self.segments
            )
            != tuple(
                (text, LANGUAGE_CHOICE_TEXT_DIGESTS[ordinal])
                for ordinal, (_, text) in enumerate(
                    LANGUAGE_CHOICE_TEXT
                )
            )
            or type(self.pauses_ms) is not tuple
            or self.pauses_ms != LANGUAGE_CHOICE_PAUSES_MS
            or any(type(value) is not int for value in self.pauses_ms)
        ):
            raise ValueError("language choice descriptor is invalid")

    def canonical_bytes(self) -> bytes:
        parts = [
            _DESCRIPTOR_DOMAIN,
            _u16(self.schema_version),
            _str16(self.asset_id, encoding="utf-8"),
            _str16(self.semantic_kind.value, encoding="ascii"),
            _str16(self.asset_locale, encoding="ascii"),
            _u8(len(self.segments)),
        ]
        for segment in self.segments:
            parts.extend(
                (
                    _u8(segment.ordinal),
                    _str16(segment.locale, encoding="ascii"),
                    _str32(segment.text),
                    bytes.fromhex(segment.text_digest),
                )
            )
        parts.append(_u8(len(self.pauses_ms)))
        parts.extend(_u16(value) for value in self.pauses_ms)
        return b"".join(parts)

    @property
    def descriptor_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


LANGUAGE_CHOICE_DESCRIPTOR = LanguageChoiceDescriptor(
    schema_version=LANGUAGE_CHOICE_SCHEMA_VERSION,
    asset_id=LANGUAGE_CHOICE_ASSET_ID,
    semantic_kind=VoiceSemanticActKind.LANGUAGE_CHOICE,
    asset_locale=LANGUAGE_CHOICE_ASSET_LOCALE,
    segments=tuple(
        LanguageChoiceSegment(
            ordinal=ordinal,
            locale=locale,
            text=text,
            text_digest=LANGUAGE_CHOICE_TEXT_DIGESTS[ordinal],
        )
        for ordinal, (locale, text) in enumerate(LANGUAGE_CHOICE_TEXT)
    ),
    pauses_ms=LANGUAGE_CHOICE_PAUSES_MS,
)
if (
    len(LANGUAGE_CHOICE_DESCRIPTOR.canonical_bytes())
    != LANGUAGE_CHOICE_DESCRIPTOR_BYTES
    or LANGUAGE_CHOICE_DESCRIPTOR.descriptor_digest
    != LANGUAGE_CHOICE_DESCRIPTOR_DIGEST
):
    raise RuntimeError("language choice descriptor pin drifted")


@dataclass(frozen=True, slots=True)
class LanguageChoiceProposal:
    state_version: int
    descriptor: LanguageChoiceDescriptor
    plan: SpokenPlan
    proposal_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.state_version) is not int
            or self.state_version < 0
            or self.descriptor is not LANGUAGE_CHOICE_DESCRIPTOR
            or type(self.plan) is not SpokenPlan
            or tuple(
                (act.kind, act.text)
                for act in self.plan.acts
            )
            != tuple(
                (
                    VoiceSemanticActKind.LANGUAGE_CHOICE,
                    segment.text,
                )
                for segment in self.descriptor.segments
            )
            or _digest(self.proposal_digest, "proposal digest")
            != self.descriptor.descriptor_digest
        ):
            raise ValueError("language choice proposal is invalid")


def materialize_language_choice(
    *,
    state_version: int,
) -> LanguageChoiceProposal:
    if type(state_version) is not int or state_version < 0:
        raise ValueError("language choice state version is invalid")
    return LanguageChoiceProposal(
        state_version=state_version,
        descriptor=LANGUAGE_CHOICE_DESCRIPTOR,
        plan=SpokenPlan(
            plan_id="unsupported_language_response_v1",
            acts=tuple(
                SemanticAct(
                    kind=VoiceSemanticActKind.LANGUAGE_CHOICE,
                    text=segment.text,
                )
                for segment in LANGUAGE_CHOICE_DESCRIPTOR.segments
            ),
        ),
        proposal_digest=LANGUAGE_CHOICE_DESCRIPTOR.descriptor_digest,
    )


@dataclass(frozen=True, slots=True)
class LanguageRecoveryFinalTurnReceipt:
    receipt_id: str
    purpose: AdmissionPurpose
    arm: CandidateArm
    adapter_implementation_digest: str
    adapter_configuration_digest: str
    canonical_event_digest: str
    content_digest: str
    content_byte_length: int
    adapter_admission_revision: int
    binding: VoiceSessionBinding
    input_turn_id: str
    input_semantic_act_kind: VoiceSemanticActKind
    sequence: int
    at_ms: int
    expires_at_ms: int
    language_generation: int
    detected_locale: str

    def __post_init__(self) -> None:
        if (
            _identifier(self.receipt_id, "recovery receipt id")
            != self.receipt_id
            or self.purpose is not AdmissionPurpose.LANGUAGE_RECOVERY
            or not isinstance(self.arm, CandidateArm)
            or any(
                _digest(value, "recovery receipt digest") != value
                for value in (
                    self.adapter_implementation_digest,
                    self.adapter_configuration_digest,
                    self.canonical_event_digest,
                    self.content_digest,
                )
            )
            or type(self.content_byte_length) is not int
            or self.content_byte_length < 1
            or type(self.adapter_admission_revision) is not int
            or self.adapter_admission_revision < 0
            or not isinstance(self.binding, VoiceSessionBinding)
            or _identifier(self.input_turn_id, "recovery input turn id")
            != self.input_turn_id
            or self.input_semantic_act_kind
            is not VoiceSemanticActKind.ACKNOWLEDGEMENT
            or type(self.sequence) is not int
            or self.sequence < 0
            or type(self.at_ms) is not int
            or self.at_ms < 0
            or type(self.expires_at_ms) is not int
            or self.expires_at_ms <= self.at_ms
            or type(self.language_generation) is not int
            or self.language_generation < 1
            or self.detected_locale not in QUALIFIED_RECOVERY_LOCALES
        ):
            raise ValueError("language recovery receipt is invalid")


@dataclass(frozen=True, slots=True)
class LanguageRecoveryAdmission:
    admission_id: str
    purpose: AdmissionPurpose
    receipt_id: str
    binding: VoiceSessionBinding
    language_generation: int
    detected_locale: str
    content_digest: str
    content_byte_length: int
    final_sequence: int
    expires_at_ms: int

    def __post_init__(self) -> None:
        if (
            _identifier(self.admission_id, "language recovery admission id")
            != self.admission_id
            or self.purpose is not AdmissionPurpose.LANGUAGE_RECOVERY
            or _identifier(self.receipt_id, "language recovery receipt id")
            != self.receipt_id
            or not isinstance(self.binding, VoiceSessionBinding)
            or type(self.language_generation) is not int
            or self.language_generation < 1
            or self.detected_locale not in QUALIFIED_RECOVERY_LOCALES
            or _digest(self.content_digest, "language recovery content digest")
            != self.content_digest
            or type(self.content_byte_length) is not int
            or self.content_byte_length < 1
            or type(self.final_sequence) is not int
            or self.final_sequence < 0
            or type(self.expires_at_ms) is not int
            or self.expires_at_ms < 1
        ):
            raise ValueError("language recovery admission is invalid")


@dataclass(frozen=True, slots=True)
class LanguageChoiceTerminalReceipt:
    receipt_id: str
    binding: VoiceSessionBinding
    language_generation: int
    trigger: ClosureTrigger
    outcome: LanguageChoiceTerminalOutcome
    descriptor_digest: str
    at_ms: int

    def __post_init__(self) -> None:
        if (
            _identifier(self.receipt_id, "language terminal receipt id")
            != self.receipt_id
            or not isinstance(self.binding, VoiceSessionBinding)
            or type(self.language_generation) is not int
            or self.language_generation < 1
            or self.trigger
            is not ClosureTrigger.LANGUAGE_CHOICE_EXHAUSTED
            or self.outcome
            is not LanguageChoiceTerminalOutcome.NO_AUDIO_TEARDOWN
            or _digest(self.descriptor_digest, "language descriptor digest")
            != LANGUAGE_CHOICE_DESCRIPTOR_DIGEST
            or type(self.at_ms) is not int
            or self.at_ms < 0
        ):
            raise ValueError("language choice terminal receipt is invalid")

    @property
    def satisfies_playback_observation(self) -> bool:
        return False

    @property
    def satisfies_disconnect_observation(self) -> bool:
        return False


class OfflineLanguageChoiceLifecycle:
    """Own one immutable, one-shot language-choice attempt under one latch."""

    def __init__(
        self,
        *,
        binding: VoiceSessionBinding,
        speech: SpeechControl,
        response_window_ms: int = LANGUAGE_CHOICE_RESPONSE_MS,
        max_speech_ms: int = LANGUAGE_CHOICE_MAX_SPEECH_MS,
        finalization_ms: int = LANGUAGE_CHOICE_FINALIZATION_MS,
    ) -> None:
        if (
            not isinstance(binding, VoiceSessionBinding)
            or type(speech) is not SpeechControl
            or type(response_window_ms) is not int
            or response_window_ms != LANGUAGE_CHOICE_RESPONSE_MS
            or type(max_speech_ms) is not int
            or max_speech_ms != LANGUAGE_CHOICE_MAX_SPEECH_MS
            or type(finalization_ms) is not int
            or finalization_ms != LANGUAGE_CHOICE_FINALIZATION_MS
        ):
            raise ValueError("language choice lifecycle dependencies are invalid")
        self.binding = binding
        self.speech = speech
        self.response_window_ms = response_window_ms
        self.max_speech_ms = max_speech_ms
        self.finalization_ms = finalization_ms
        self.phase = LanguageChoicePhase.AVAILABLE
        self.generation = 0
        self._reserved: tuple[ReservedSpeech, ...] = ()
        self._observed: list[VoiceEvent] = []
        self._response_deadline_ms: int | None = None
        self._activity_turn_id: str | None = None
        self._arbitration_sequence = -1
        self._activity_onset_ms: int | None = None
        self._activity_deadline_ms: int | None = None
        self._finalization_deadline_ms: int | None = None
        self._final_event: VoiceEvent | None = None
        self._detected_locale: str | None = None
        self._pending_receipt: LanguageRecoveryFinalTurnReceipt | None = None
        self._pending_admission: LanguageRecoveryAdmission | None = None
        self._terminal_receipt: LanguageChoiceTerminalReceipt | None = None
        self._lock = RLock()

    @property
    def response_deadline_ms(self) -> int | None:
        with self._lock:
            return self._response_deadline_ms

    @property
    def activity_deadline_ms(self) -> int | None:
        with self._lock:
            return self._activity_deadline_ms

    @property
    def finalization_deadline_ms(self) -> int | None:
        with self._lock:
            return self._finalization_deadline_ms

    @property
    def pending_pair_count(self) -> int:
        with self._lock:
            return int(
                self._pending_receipt is not None
                or self._pending_admission is not None
            )

    @property
    def observed_segment_count(self) -> int:
        with self._lock:
            return len(self._observed)

    def reserve(
        self,
        *,
        proposal: LanguageChoiceProposal,
        reserved: tuple[ReservedSpeech, ...],
    ) -> bool:
        with self._lock:
            if (
                self.phase is not LanguageChoicePhase.AVAILABLE
                or type(proposal) is not LanguageChoiceProposal
                or proposal.descriptor is not LANGUAGE_CHOICE_DESCRIPTOR
                or type(reserved) is not tuple
                or len(reserved) != 3
                or tuple(
                    (item.kind, item.text, item.binding)
                    for item in reserved
                    if type(item) is ReservedSpeech
                )
                != tuple(
                    (
                        VoiceSemanticActKind.LANGUAGE_CHOICE,
                        segment.text,
                        self.binding,
                    )
                    for segment in proposal.descriptor.segments
                )
            ):
                return False
            self.generation += 1
            self._reserved = reserved
            self.phase = LanguageChoicePhase.RESERVED
            return True

    def begin_presentation(
        self,
        *,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        """Anchor eligibility after every pre-prompt canonical event."""
        with self._lock:
            if (
                self.phase is not LanguageChoicePhase.RESERVED
                or type(lifecycle) is not VoiceLifecycle
                or lifecycle.binding != self.binding
                or lifecycle.latest_sequence < -1
            ):
                return False
            self._arbitration_sequence = lifecycle.latest_sequence
            self.phase = LanguageChoicePhase.PRESENTING
            return True

    def observe_segment(
        self,
        *,
        event: VoiceEvent,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        with self._lock:
            if not self._is_current_segment_locked(
                event=event,
                lifecycle=lifecycle,
            ):
                return False
            pending_input = lifecycle.input_observations_after(
                self._arbitration_sequence
            )
            if pending_input is None:
                self._terminalize_locked()
                return False
            if (
                pending_input
                and pending_input[0].sequence < event.sequence
            ):
                return False
            self._observed.append(event)
            self._arbitration_sequence = event.sequence
            if len(self._observed) == len(self._reserved):
                self._response_deadline_ms = (
                    event.at_ms + self.response_window_ms
                )
                self.phase = LanguageChoicePhase.CLEANUP_PENDING
            return True

    def defers_playback(
        self,
        *,
        event: VoiceEvent,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        """Defer playback while an earlier retained caller input is pending."""
        with self._lock:
            if not self._is_current_segment_locked(
                event=event,
                lifecycle=lifecycle,
            ):
                return False
            pending_input = lifecycle.input_observations_after(
                self._arbitration_sequence
            )
            if pending_input is None:
                self._terminalize_locked()
                return False
            return bool(
                pending_input
                and pending_input[0].sequence < event.sequence
            )

    def complete_prompt_cleanup(self) -> bool:
        with self._lock:
            if (
                self.phase is not LanguageChoicePhase.CLEANUP_PENDING
                or not self._reserved
                or self._response_deadline_ms is None
                or self.speech.tracks_reservation_batch(self._reserved)
            ):
                return False
            for item in self._reserved:
                if not self.speech.hard_terminalize(item.act_id):
                    return False
            if any(
                self.speech.is_live(item.act_id)
                for item in self._reserved
            ):
                return False
            self._arbitration_sequence = max(
                self._arbitration_sequence,
                self._observed[-1].sequence,
            )
            self.phase = LanguageChoicePhase.RESPONSE_WINDOW
            return True

    def accept_activity_started(
        self,
        *,
        event: VoiceEvent,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        with self._lock:
            invalid_lifecycle = (
                type(lifecycle) is not VoiceLifecycle
                or lifecycle.binding != self.binding
            )
            if invalid_lifecycle:
                return False
            canonical = (
                event.kind is VoiceEventKind.INPUT_ACTIVITY_STARTED
                and lifecycle.accepts_input_observation(event)
                and event.sequence > self._arbitration_sequence
                and self._is_next_input_locked(
                    lifecycle=lifecycle,
                    event=event,
                )
            )
            if not canonical:
                return False
            if (
                self.phase not in {
                    LanguageChoicePhase.PRESENTING,
                    LanguageChoicePhase.CLEANUP_PENDING,
                    LanguageChoicePhase.RESPONSE_WINDOW,
                }
                or (
                    self.phase
                    in {
                        LanguageChoicePhase.CLEANUP_PENDING,
                        LanguageChoicePhase.RESPONSE_WINDOW,
                    }
                    and (
                        self._response_deadline_ms is None
                        or event.at_ms > self._response_deadline_ms
                    )
                )
            ):
                self._arbitration_sequence = event.sequence
                if (
                    self.phase
                    in {
                        LanguageChoicePhase.CLEANUP_PENDING,
                        LanguageChoicePhase.RESPONSE_WINDOW,
                    }
                    and self._response_deadline_ms is not None
                    and isinstance(event, VoiceEvent)
                    and event.at_ms > self._response_deadline_ms
                ):
                    self._terminalize_locked()
                return False
            if not self._seal_prompt_locked():
                self._arbitration_sequence = event.sequence
                self._terminalize_locked()
                return False
            self._activity_turn_id = event.input_turn_id
            self._arbitration_sequence = event.sequence
            self._activity_onset_ms = event.at_ms
            self._activity_deadline_ms = (
                event.at_ms + self.max_speech_ms
            )
            self._response_deadline_ms = None
            self.phase = LanguageChoicePhase.ACTIVITY_OPEN
            return True

    def accept_activity_ended(
        self,
        *,
        event: VoiceEvent,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        with self._lock:
            canonical = (
                type(lifecycle) is VoiceLifecycle
                and lifecycle.binding == self.binding
                and event.kind is VoiceEventKind.INPUT_ACTIVITY_ENDED
                and lifecycle.accepts_input_observation(event)
                and event.sequence > self._arbitration_sequence
                and self._is_next_input_locked(
                    lifecycle=lifecycle,
                    event=event,
                )
            )
            if not canonical:
                return False
            if (
                self.phase is not LanguageChoicePhase.ACTIVITY_OPEN
                or event.input_turn_id != self._activity_turn_id
                or self._activity_deadline_ms is None
                or event.at_ms > self._activity_deadline_ms
            ):
                self._arbitration_sequence = event.sequence
                if (
                    self.phase is LanguageChoicePhase.ACTIVITY_OPEN
                    and self._activity_deadline_ms is not None
                    and isinstance(event, VoiceEvent)
                    and event.at_ms > self._activity_deadline_ms
                ):
                    self._terminalize_locked()
                return False
            self._finalization_deadline_ms = (
                event.at_ms + self.finalization_ms
            )
            self._arbitration_sequence = event.sequence
            self.phase = LanguageChoicePhase.FINALIZING
            return True

    def stage_final(
        self,
        *,
        event: VoiceEvent,
        lifecycle: VoiceLifecycle,
        detected_locale: str | None,
    ) -> LanguageFinalDisposition:
        with self._lock:
            invalid_lifecycle = (
                type(lifecycle) is not VoiceLifecycle
                or lifecycle.binding != self.binding
            )
            if invalid_lifecycle:
                return LanguageFinalDisposition.REJECTED
            canonical = (
                lifecycle.accepts_input_final(event)
                and lifecycle.accepts_input_observation(event)
                and event.sequence > self._arbitration_sequence
                and self._is_next_input_locked(
                    lifecycle=lifecycle,
                    event=event,
                )
            )
            if not canonical:
                return LanguageFinalDisposition.REJECTED
            if (
                event.input_turn_id != (
                    self._activity_turn_id
                    if self._activity_turn_id is not None
                    else event.input_turn_id
                )
            ):
                self._arbitration_sequence = event.sequence
                return LanguageFinalDisposition.REJECTED
            if self.phase in {
                LanguageChoicePhase.PRESENTING,
                LanguageChoicePhase.CLEANUP_PENDING,
                LanguageChoicePhase.RESPONSE_WINDOW,
            }:
                if (
                    self.phase
                    in {
                        LanguageChoicePhase.CLEANUP_PENDING,
                        LanguageChoicePhase.RESPONSE_WINDOW,
                    }
                    and (
                        self._response_deadline_ms is None
                        or event.at_ms > self._response_deadline_ms
                    )
                ):
                    self._arbitration_sequence = event.sequence
                    self._terminalize_locked()
                    return LanguageFinalDisposition.UNQUALIFIED
                if not self._seal_prompt_locked():
                    self._arbitration_sequence = event.sequence
                    self._terminalize_locked()
                    return LanguageFinalDisposition.REJECTED
                self._activity_turn_id = event.input_turn_id
                self._arbitration_sequence = event.sequence
                self._activity_onset_ms = event.at_ms
                self._activity_deadline_ms = (
                    event.at_ms + self.max_speech_ms
                )
                self._finalization_deadline_ms = (
                    event.at_ms + self.finalization_ms
                )
                self.phase = LanguageChoicePhase.FINALIZING
            elif self.phase is LanguageChoicePhase.ACTIVITY_OPEN:
                if (
                    self._activity_deadline_ms is None
                    or event.at_ms > self._activity_deadline_ms
                ):
                    self._arbitration_sequence = event.sequence
                    self._terminalize_locked()
                    return LanguageFinalDisposition.UNQUALIFIED
                self._finalization_deadline_ms = (
                    event.at_ms + self.finalization_ms
                )
                self.phase = LanguageChoicePhase.FINALIZING
            if (
                self.phase is not LanguageChoicePhase.FINALIZING
                or self._finalization_deadline_ms is None
                or event.at_ms > self._finalization_deadline_ms
            ):
                self._arbitration_sequence = event.sequence
                self._terminalize_locked()
                return LanguageFinalDisposition.UNQUALIFIED
            self._final_event = event
            self._arbitration_sequence = event.sequence
            self._detected_locale = detected_locale
            if detected_locale not in QUALIFIED_RECOVERY_LOCALES:
                self._terminalize_locked()
                return LanguageFinalDisposition.UNQUALIFIED
            return LanguageFinalDisposition.QUALIFIED

    def bind_recovery_receipt(
        self,
        receipt: LanguageRecoveryFinalTurnReceipt,
    ) -> LanguageRecoveryAdmission | None:
        with self._lock:
            event = self._final_event
            if (
                self.phase is not LanguageChoicePhase.FINALIZING
                or type(receipt) is not LanguageRecoveryFinalTurnReceipt
                or event is None
                or receipt.binding != self.binding
                or receipt.language_generation != self.generation
                or receipt.input_turn_id != event.input_turn_id
                or receipt.sequence != event.sequence
                or receipt.at_ms != event.at_ms
                or receipt.detected_locale != self._detected_locale
                or self._pending_receipt is not None
                or self._pending_admission is not None
            ):
                self._terminalize_locked()
                return None
            material = (
                _RECOVERY_ADMISSION_DOMAIN
                + receipt.receipt_id.encode("ascii")
                + bytes.fromhex(receipt.content_digest)
                + receipt.content_byte_length.to_bytes(4, "big")
                + receipt.sequence.to_bytes(8, "big")
                + self.generation.to_bytes(8, "big")
            )
            admission = LanguageRecoveryAdmission(
                admission_id=(
                    "language_admission_"
                    + hashlib.sha256(material).hexdigest()
                ),
                purpose=AdmissionPurpose.LANGUAGE_RECOVERY,
                receipt_id=receipt.receipt_id,
                binding=self.binding,
                language_generation=self.generation,
                detected_locale=receipt.detected_locale,
                content_digest=receipt.content_digest,
                content_byte_length=receipt.content_byte_length,
                final_sequence=receipt.sequence,
                expires_at_ms=receipt.expires_at_ms,
            )
            self._pending_receipt = receipt
            self._pending_admission = admission
            self.phase = LanguageChoicePhase.RECOVERY_PENDING
            return admission

    def consume_recovery_pair(
        self,
        *,
        receipt: LanguageRecoveryFinalTurnReceipt,
        admission: LanguageRecoveryAdmission,
        now_ms: int,
    ) -> bool:
        with self._lock:
            accepted = (
                self.phase is LanguageChoicePhase.RECOVERY_PENDING
                and type(receipt) is LanguageRecoveryFinalTurnReceipt
                and type(admission) is LanguageRecoveryAdmission
                and self._pending_receipt is receipt
                and self._pending_admission is admission
                and admission.receipt_id == receipt.receipt_id
                and admission.binding == receipt.binding
                and admission.language_generation
                == receipt.language_generation
                and admission.detected_locale
                == receipt.detected_locale
                and admission.content_digest == receipt.content_digest
                and admission.content_byte_length
                == receipt.content_byte_length
                and admission.final_sequence == receipt.sequence
                and type(now_ms) is int
                and 0 <= now_ms <= receipt.expires_at_ms
            )
            self._pending_receipt = None
            self._pending_admission = None
            if accepted:
                self.phase = LanguageChoicePhase.RECOVERY_CONSUMED
                return True
            self._terminalize_locked()
            return False

    def validate_recovery_locale(
        self,
        detected_locale: str | None,
    ) -> bool:
        """Validate extraction against the consumed candidate-final authority."""
        with self._lock:
            accepted = (
                self.phase is LanguageChoicePhase.RECOVERY_CONSUMED
                and detected_locale in QUALIFIED_RECOVERY_LOCALES
                and detected_locale == self._detected_locale
            )
            if accepted:
                self.phase = LanguageChoicePhase.RECOVERY_VALIDATED
                return True
            self._terminalize_locked()
            return False

    def commit_recovery(self) -> bool:
        """Publish recovery only after validated facts commit."""
        with self._lock:
            if self.phase is not LanguageChoicePhase.RECOVERY_VALIDATED:
                self._terminalize_locked()
                return False
            self.phase = LanguageChoicePhase.RECOVERED
            return True

    def tombstone_pending_pair(self) -> bool:
        with self._lock:
            self._pending_receipt = None
            self._pending_admission = None
            self._terminalize_locked()
            return self.pending_pair_count == 0

    def advance_time(
        self,
        *,
        lifecycle: VoiceLifecycle,
        at_ms: int,
    ) -> bool:
        with self._lock:
            _exact_nonnegative(at_ms, "language choice clock")
            if (
                type(lifecycle) is not VoiceLifecycle
                or lifecycle.binding != self.binding
            ):
                return False
            observations = lifecycle.input_observations_after(
                self._arbitration_sequence
            )
            if observations is None:
                self._terminalize_locked()
                return True
            for candidate in observations:
                if candidate.at_ms > at_ms:
                    break
                pending_canonical_event = (
                    (
                        self.phase
                        in {
                            LanguageChoicePhase.CLEANUP_PENDING,
                            LanguageChoicePhase.RESPONSE_WINDOW,
                        }
                        and self._response_deadline_ms is not None
                        and candidate.kind
                        in {
                            VoiceEventKind.INPUT_ACTIVITY_STARTED,
                            VoiceEventKind.INPUT_TURN_FINAL,
                        }
                        and candidate.at_ms
                        <= self._response_deadline_ms
                    )
                    or (
                        self.phase
                        is LanguageChoicePhase.ACTIVITY_OPEN
                        and self._activity_deadline_ms is not None
                        and candidate.input_turn_id
                        == self._activity_turn_id
                        and candidate.kind
                        in {
                            VoiceEventKind.INPUT_ACTIVITY_ENDED,
                            VoiceEventKind.INPUT_TURN_FINAL,
                        }
                        and candidate.at_ms
                        <= self._activity_deadline_ms
                    )
                    or (
                        self.phase
                        is LanguageChoicePhase.FINALIZING
                        and self._finalization_deadline_ms
                        is not None
                        and candidate.input_turn_id
                        == self._activity_turn_id
                        and candidate.kind
                        is VoiceEventKind.INPUT_TURN_FINAL
                        and candidate.at_ms
                        <= self._finalization_deadline_ms
                    )
                )
                if pending_canonical_event:
                    return False
                self._arbitration_sequence = candidate.sequence
            expired = (
                (
                    self.phase
                    in {
                        LanguageChoicePhase.CLEANUP_PENDING,
                        LanguageChoicePhase.RESPONSE_WINDOW,
                    }
                    and self._response_deadline_ms is not None
                    and at_ms > self._response_deadline_ms
                )
                or (
                    self.phase is LanguageChoicePhase.ACTIVITY_OPEN
                    and self._activity_deadline_ms is not None
                    and at_ms > self._activity_deadline_ms
                )
                or (
                    self.phase is LanguageChoicePhase.FINALIZING
                    and self._finalization_deadline_ms is not None
                    and at_ms > self._finalization_deadline_ms
                )
            )
            if expired:
                self._terminalize_locked()
            return expired

    def issue_terminal_receipt(
        self,
        *,
        inventory: OfflineAuthorityInventory,
        at_ms: int,
    ) -> LanguageChoiceTerminalReceipt | None:
        with self._lock:
            if (
                self.phase is not LanguageChoicePhase.TERMINAL
                or type(inventory) is not OfflineAuthorityInventory
                or not inventory.is_sealed
                or type(at_ms) is not int
                or at_ms < 0
                or self._pending_receipt is not None
                or self._pending_admission is not None
                or self._response_deadline_ms is not None
                or self._activity_deadline_ms is not None
                or self._finalization_deadline_ms is not None
                or any(
                    self.speech.is_live(item.act_id)
                    for item in self._reserved
                )
                or (
                    self._reserved
                    and self.speech.tracks_reservation_batch(
                        self._reserved
                    )
                )
            ):
                return None
            if self._terminal_receipt is not None:
                return self._terminal_receipt
            material = (
                _TERMINAL_RECEIPT_DOMAIN
                + _binding_bytes(self.binding)
                + LANGUAGE_CHOICE_DESCRIPTOR.canonical_bytes()
                + self.generation.to_bytes(8, "big")
                + at_ms.to_bytes(8, "big")
            )
            self._terminal_receipt = LanguageChoiceTerminalReceipt(
                receipt_id=(
                    "language_terminal_"
                    + hashlib.sha256(material).hexdigest()
                ),
                binding=self.binding,
                language_generation=self.generation,
                trigger=ClosureTrigger.LANGUAGE_CHOICE_EXHAUSTED,
                outcome=LanguageChoiceTerminalOutcome.NO_AUDIO_TEARDOWN,
                descriptor_digest=LANGUAGE_CHOICE_DESCRIPTOR_DIGEST,
                at_ms=at_ms,
            )
            return self._terminal_receipt

    def _seal_prompt_locked(self) -> bool:
        if not self._reserved:
            return False
        tracked = self.speech.tracks_reservation_batch(self._reserved)
        for item in self._reserved:
            if not self.speech.is_cancelled(item.act_id):
                self.speech.cancel(
                    item.act_id,
                    reason=CancellationReason.CALLER_ACTIVITY,
                )
            self.speech.hard_terminalize(item.act_id)
        if tracked and not self.speech.force_retire_reservation(
            self._reserved
        ):
            return False
        return (
            not self.speech.tracks_reservation_batch(self._reserved)
            and all(
                not self.speech.is_live(item.act_id)
                for item in self._reserved
            )
        )

    def _is_next_input_locked(
        self,
        *,
        lifecycle: VoiceLifecycle,
        event: VoiceEvent,
    ) -> bool:
        observations = lifecycle.input_observations_after(
            self._arbitration_sequence
        )
        if observations is None:
            self._terminalize_locked()
            return False
        return bool(observations) and observations[0] is event

    def _is_current_segment_locked(
        self,
        *,
        lifecycle: VoiceLifecycle,
        event: VoiceEvent,
    ) -> bool:
        ordinal = len(self._observed)
        return (
            self.phase is LanguageChoicePhase.PRESENTING
            and ordinal < len(self._reserved)
            and type(lifecycle) is VoiceLifecycle
            and lifecycle.binding == self.binding
            and lifecycle.accepts_caller_playback(event)
            and event.semantic_act_kind
            is VoiceSemanticActKind.LANGUAGE_CHOICE
            and event.semantic_act_id
            == self._reserved[ordinal].act_id
            and event.payload.text_digest
            == LANGUAGE_CHOICE_DESCRIPTOR.segments[
                ordinal
            ].text_digest
        )

    def _terminalize_locked(self) -> None:
        if self._reserved:
            self._seal_prompt_locked()
        self._pending_receipt = None
        self._pending_admission = None
        self._response_deadline_ms = None
        self._activity_deadline_ms = None
        self._finalization_deadline_ms = None
        self.phase = LanguageChoicePhase.TERMINAL


def language_recovery_receipt_id(
    *,
    arm: CandidateArm,
    canonical_event_digest: str,
    content_digest: str,
    content_byte_length: int,
    language_generation: int,
    detected_locale: str,
    counter: int,
    expires_at_ms: int,
) -> str:
    if (
        not isinstance(arm, CandidateArm)
        or _digest(canonical_event_digest, "canonical event digest")
        != canonical_event_digest
        or _digest(content_digest, "content digest") != content_digest
        or type(content_byte_length) is not int
        or content_byte_length < 1
        or type(language_generation) is not int
        or language_generation < 1
        or detected_locale not in QUALIFIED_RECOVERY_LOCALES
        or type(counter) is not int
        or counter < 1
        or type(expires_at_ms) is not int
        or expires_at_ms < 1
    ):
        raise ValueError("language recovery receipt material is invalid")
    material = (
        _RECOVERY_RECEIPT_DOMAIN
        + arm.value.encode("ascii")
        + bytes.fromhex(canonical_event_digest)
        + bytes.fromhex(content_digest)
        + content_byte_length.to_bytes(4, "big")
        + language_generation.to_bytes(8, "big")
        + detected_locale.encode("ascii")
        + counter.to_bytes(8, "big")
        + expires_at_ms.to_bytes(8, "big")
    )
    return "language_receipt_" + hashlib.sha256(material).hexdigest()


__all__ = [
    "LANGUAGE_CHOICE_ASSET_ID",
    "LANGUAGE_CHOICE_ASSET_LOCALE",
    "LANGUAGE_CHOICE_DESCRIPTOR",
    "LANGUAGE_CHOICE_DESCRIPTOR_BYTES",
    "LANGUAGE_CHOICE_DESCRIPTOR_DIGEST",
    "LANGUAGE_CHOICE_FINALIZATION_MS",
    "LANGUAGE_CHOICE_MAX_SPEECH_MS",
    "LANGUAGE_CHOICE_PAUSES_MS",
    "LANGUAGE_CHOICE_RESPONSE_MS",
    "LANGUAGE_CHOICE_TEXT",
    "LANGUAGE_CHOICE_TEXT_DIGESTS",
    "QUALIFIED_RECOVERY_LOCALES",
    "UNLISTED_CHALLENGE_LOCALES",
    "AdmissionPurpose",
    "LanguageChoiceDescriptor",
    "LanguageChoicePhase",
    "LanguageChoiceProposal",
    "LanguageChoiceSegment",
    "LanguageChoiceTerminalOutcome",
    "LanguageChoiceTerminalReceipt",
    "LanguageFinalDisposition",
    "LanguageRecoveryAdmission",
    "LanguageRecoveryFinalTurnReceipt",
    "OfflineLanguageChoiceLifecycle",
    "language_recovery_receipt_id",
    "materialize_language_choice",
]
