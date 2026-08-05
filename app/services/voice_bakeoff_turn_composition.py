"""Transactional, offline-only composition for candidate-final caller turns."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from threading import Lock, RLock

from app.services.caller_observation_extractor import (
    CandidateFinalTurn,
    ExtractionOutcome,
    Finality,
    ObservationBackend,
    ObservationExtractor,
)
from app.services.dialogue_planner import ActionName, NextAction, plan_next_action
from app.services.receptionist_state import IntakeState
from app.services.voice_bakeoff_closure import ClosureTrigger
from app.services.voice_bakeoff_coordinator import VoiceBakeoffCoordinator
from app.services.voice_bakeoff_language_choice import (
    AdmissionPurpose,
    LanguageChoicePhase,
    LanguageChoiceProposal,
    LanguageFinalDisposition,
    LanguageRecoveryAdmission,
    LanguageRecoveryFinalTurnReceipt,
    OfflineLanguageChoiceLifecycle,
    language_recovery_receipt_id,
)
from app.services.voice_bakeoff_materializer import (
    ContentProposal,
    FixedProposalMaterializer,
    ProposalKind,
)
from app.services.voice_call_lifecycle import (
    PlaybackEvidence,
    SilencePhase,
)
from app.services.voice_candidates import AdapterResult, OfflineCandidateAdapter
from app.services.voice_candidates.chained_streaming import ChainedStreamingAdapter
from app.services.voice_candidates.conversation_relay import ConversationRelayAdapter
from app.services.voice_candidates.manual_native import ManualNativeAdapter
from app.services.voice_candidates.native_gemini import NativeGeminiAdapter
from app.services.voice_lifecycle import (
    VOICE_SCHEMA_VERSION,
    VoiceEvent,
    VoiceEventKind,
    VoiceLifecycle,
    VoicePayload,
    VoiceSemanticActKind,
    VoiceSensitivity,
    VoiceSessionBinding,
    VoiceSource,
    VoiceTimeoutIntent,
)
from app.services.voice_session_auth import CandidateArm
from app.services.voice_speech_control import (
    CancellationReason,
    ReplayMode,
    ReplaySource,
    ReservedSpeech,
    SpeechAuthorization,
)

_CONTENT_DOMAIN = b"hey-kevin/offline-final-turn-content/v1\x00"
_MAX_CONTENT_CHARS = 4_000
_MAX_CONTENT_BYTES = 16_000
_ALLOWED_ADAPTERS: dict[type[OfflineCandidateAdapter], CandidateArm] = {
    NativeGeminiAdapter: CandidateArm.A,
    ChainedStreamingAdapter: CandidateArm.B1,
    ConversationRelayAdapter: CandidateArm.B2,
    ManualNativeAdapter: CandidateArm.C,
}
_RECOVERABLE_EXTRACTION = {
    ExtractionOutcome.LOW_CONFIDENCE,
    ExtractionOutcome.MALFORMED,
    ExtractionOutcome.TIMEOUT,
    ExtractionOutcome.PROVIDER_ERROR,
}


class CompositionStatus(str, Enum):
    RESPONSE_PENDING = "response_pending"
    REPAIR_PENDING = "repair_pending"
    REPLAY_PENDING = "replay_pending"
    RESPONSE_OBSERVED = "response_observed"
    REPLAY_OBSERVED = "replay_observed"
    SILENT = "silent"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    TERMINAL_FAILURE = "terminal_failure"
    CLOSURE_REQUIRED = "closure_required"
    LANGUAGE_CHOICE_REQUIRED = "language_choice_required"
    LANGUAGE_CHOICE_PENDING = "language_choice_pending"
    LANGUAGE_CHOICE_WINDOW = "language_choice_window"


class CompositionPhase(str, Enum):
    RECEIVED = "received"
    FINAL_TURN_ADMITTED = "final_turn_admitted"
    EXTRACTION_TERMINAL = "extraction_terminal"
    FACTS_COMMITTED = "facts_committed"
    RESPONSE_PENDING_PLAYBACK = "response_pending_playback"
    RESPONSE_COMMITTED = "response_committed"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class FinalTurnAdmissionReceipt:
    receipt_id: str
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

    def __post_init__(self) -> None:
        if (
            not _identifier(self.receipt_id)
            or not isinstance(self.arm, CandidateArm)
            or any(
                not _digest(value)
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
            or not _identifier(self.input_turn_id)
            or not isinstance(
                self.input_semantic_act_kind,
                VoiceSemanticActKind,
            )
            or type(self.sequence) is not int
            or self.sequence < 0
            or type(self.at_ms) is not int
            or self.at_ms < 0
            or type(self.expires_at_ms) is not int
            or self.expires_at_ms <= self.at_ms
        ):
            raise ValueError("final-turn admission receipt is invalid")


@dataclass(frozen=True, slots=True)
class AdapterImplementationBinding:
    """Exact class and reviewed source/contract digest supplied by assembly."""

    adapter_type: type[OfflineCandidateAdapter]
    arm: CandidateArm
    implementation_digest: str

    def __post_init__(self) -> None:
        if (
            self.adapter_type not in _ALLOWED_ADAPTERS
            or _ALLOWED_ADAPTERS[self.adapter_type] is not self.arm
            or not _digest(self.implementation_digest)
        ):
            raise ValueError("adapter implementation binding is invalid")


@dataclass(frozen=True, slots=True)
class _ReceiptTombstone:
    receipt_id: str
    binding: VoiceSessionBinding
    input_turn_id: str
    sequence: int
    expires_at_ms: int
    accepted: bool


class FinalTurnAdmissionAuthority:
    """Mint and atomically consume bounded, one-use adapter-final receipts."""

    def __init__(
        self,
        *,
        adapter: OfflineCandidateAdapter,
        lifecycle: VoiceLifecycle,
        implementation_bindings: tuple[AdapterImplementationBinding, ...],
        max_records: int = 256,
        max_ttl_ms: int = 60_000,
    ) -> None:
        if (
            not isinstance(implementation_bindings, tuple)
            or not implementation_bindings
            or any(
                not isinstance(binding, AdapterImplementationBinding)
                for binding in implementation_bindings
            )
            or len({binding.adapter_type for binding in implementation_bindings})
            != len(implementation_bindings)
            or type(max_records) is not int
            or max_records < 1
            or type(max_ttl_ms) is not int
            or max_ttl_ms < 1
            or not _allowed_adapter(adapter)
            or type(lifecycle) is not VoiceLifecycle
            or lifecycle.binding != adapter.binding
        ):
            raise ValueError("receipt authority limits are invalid")
        self.max_records = max_records
        self.max_ttl_ms = max_ttl_ms
        self._implementation_bindings = {
            binding.adapter_type: binding
            for binding in implementation_bindings
        }
        if (
            type(adapter) not in self._implementation_bindings
            or not adapter.bind_canonical_lifecycle(lifecycle)
        ):
            raise ValueError("receipt authority assembly is invalid")
        self._adapter = adapter
        self._lifecycle = lifecycle
        self._counter = 0
        self._live: dict[str, FinalTurnAdmissionReceipt] = {}
        self._pending_recovery: dict[
            str,
            tuple[
                LanguageRecoveryFinalTurnReceipt,
                LanguageRecoveryAdmission,
                OfflineLanguageChoiceLifecycle,
            ],
        ] = {}
        self._tombstones: dict[str, _ReceiptTombstone] = {}
        self._disconnect_owner: object | None = None
        self._lock = Lock()

    def mint(
        self,
        *,
        adapter: OfflineCandidateAdapter,
        lifecycle: VoiceLifecycle,
        result: AdapterResult,
        event: VoiceEvent,
        content: str,
        now_ms: int,
        ttl_ms: int,
    ) -> FinalTurnAdmissionReceipt | None:
        if (
            not self.accepts_adapter(adapter)
            or lifecycle is not self._lifecycle
            or not isinstance(event, VoiceEvent)
            or event.binding != adapter.binding
            or type(now_ms) is not int
            or now_ms < 0
            or type(ttl_ms) is not int
            or ttl_ms < 1
            or ttl_ms > self.max_ttl_ms
            or not isinstance(content, str)
            or not content
            or len(content) > _MAX_CONTENT_CHARS
            or now_ms < event.at_ms
        ):
            return None
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_CONTENT_BYTES:
            return None
        content_digest = final_turn_content_digest(content)
        if (
            event.payload.text_digest != content_digest
            or not lifecycle.accepts_input_final(event)
        ):
            return None
        with self._lock:
            self._prune(now_ms=now_ms)
            if not adapter.consume_final_input_admission(result, event):
                return None
            if (
                len(self._live)
                + len(self._pending_recovery)
                + len(self._tombstones)
                >= self.max_records
            ):
                return None
            implementation = self._implementation_bindings[type(adapter)]
            self._counter += 1
            canonical_event_digest = _event_digest(event)
            receipt_id = (
                "receipt_"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "domain": "hey-kevin/offline-final-turn-receipt/v1",
                            "counter": self._counter,
                            "arm": adapter.arm.value,
                            "event": canonical_event_digest,
                            "configuration": adapter.configuration_digest,
                            "admission_revision": adapter.admission_revision,
                            "expires_at_ms": now_ms + ttl_ms,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            )
            receipt = FinalTurnAdmissionReceipt(
                receipt_id=receipt_id,
                arm=adapter.arm,
                adapter_implementation_digest=implementation.implementation_digest,
                adapter_configuration_digest=adapter.configuration_digest,
                canonical_event_digest=canonical_event_digest,
                content_digest=content_digest,
                content_byte_length=len(encoded),
                adapter_admission_revision=adapter.admission_revision,
                binding=event.binding,
                input_turn_id=event.input_turn_id,
                input_semantic_act_kind=(
                    event.semantic_act_kind
                ),
                sequence=event.sequence,
                at_ms=event.at_ms,
                expires_at_ms=now_ms + ttl_ms,
            )
            self._live[receipt_id] = receipt
            return receipt

    def consume(
        self,
        receipt: FinalTurnAdmissionReceipt,
        *,
        adapter: OfflineCandidateAdapter,
        content: str,
        now_ms: int,
    ) -> bool:
        if (
            type(receipt) is not FinalTurnAdmissionReceipt
            or not self.accepts_adapter(adapter)
            or not isinstance(content, str)
            or not content
            or len(content) > _MAX_CONTENT_CHARS
            or type(now_ms) is not int
            or now_ms < 0
        ):
            return False
        if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            return False
        with self._lock:
            self._prune(now_ms=now_ms)
            expected = self._live.pop(receipt.receipt_id, None)
            if expected is None:
                return False
            implementation = self._implementation_bindings[type(adapter)]
            accepted = (
                expected is receipt
                and now_ms <= receipt.expires_at_ms
                and receipt.arm is adapter.arm
                and receipt.binding == adapter.binding
                and receipt.adapter_implementation_digest
                == implementation.implementation_digest
                and receipt.adapter_configuration_digest == adapter.configuration_digest
                and receipt.adapter_admission_revision == adapter.admission_revision
                and not adapter.permit_admission_closed
                and receipt.content_digest == final_turn_content_digest(content)
                and receipt.content_byte_length == len(content.encode("utf-8"))
            )
            self._tombstones[receipt.receipt_id] = _ReceiptTombstone(
                receipt_id=receipt.receipt_id,
                binding=receipt.binding,
                input_turn_id=receipt.input_turn_id,
                sequence=receipt.sequence,
                expires_at_ms=receipt.expires_at_ms + self.max_ttl_ms,
                accepted=accepted,
            )
            return accepted

    def mint_language_recovery(
        self,
        *,
        adapter: OfflineCandidateAdapter,
        lifecycle: VoiceLifecycle,
        language_choice: OfflineLanguageChoiceLifecycle,
        result: AdapterResult,
        event: VoiceEvent,
        content: str,
        detected_locale: str | None,
        now_ms: int,
        ttl_ms: int,
    ) -> tuple[
        LanguageRecoveryFinalTurnReceipt,
        LanguageRecoveryAdmission,
    ] | None:
        """Mint a purpose-sealed pair that ordinary composition cannot consume."""
        if (
            not self.accepts_adapter(adapter)
            or lifecycle is not self._lifecycle
            or type(language_choice)
            is not OfflineLanguageChoiceLifecycle
            or language_choice.binding != adapter.binding
            or not isinstance(event, VoiceEvent)
            or event.binding != adapter.binding
            or event.semantic_act_kind
            is not VoiceSemanticActKind.ACKNOWLEDGEMENT
            or type(now_ms) is not int
            or now_ms < event.at_ms
            or type(ttl_ms) is not int
            or not 1 <= ttl_ms <= self.max_ttl_ms
            or type(content) is not str
            or not content
            or len(content) > _MAX_CONTENT_CHARS
        ):
            return None
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_CONTENT_BYTES:
            return None
        content_digest = final_turn_content_digest(content)
        if (
            event.payload.text_digest != content_digest
            or not lifecycle.accepts_input_final(event)
        ):
            return None
        with self._lock:
            self._prune(now_ms=now_ms)
            if (
                len(self._live)
                + len(self._pending_recovery)
                + len(self._tombstones)
                >= self.max_records
                or not adapter.consume_final_input_admission(
                    result,
                    event,
                )
            ):
                return None
            disposition = language_choice.stage_final(
                event=event,
                lifecycle=lifecycle,
                detected_locale=detected_locale,
            )
            if disposition is not LanguageFinalDisposition.QUALIFIED:
                return None
            implementation = self._implementation_bindings[
                type(adapter)
            ]
            self._counter += 1
            canonical_event_digest = _event_digest(event)
            expires_at_ms = now_ms + ttl_ms
            try:
                receipt = LanguageRecoveryFinalTurnReceipt(
                    receipt_id=language_recovery_receipt_id(
                        arm=adapter.arm,
                        canonical_event_digest=(
                            canonical_event_digest
                        ),
                        content_digest=content_digest,
                        content_byte_length=len(encoded),
                        language_generation=(
                            language_choice.generation
                        ),
                        detected_locale=detected_locale,
                        counter=self._counter,
                        expires_at_ms=expires_at_ms,
                    ),
                    purpose=AdmissionPurpose.LANGUAGE_RECOVERY,
                    arm=adapter.arm,
                    adapter_implementation_digest=(
                        implementation.implementation_digest
                    ),
                    adapter_configuration_digest=(
                        adapter.configuration_digest
                    ),
                    canonical_event_digest=canonical_event_digest,
                    content_digest=content_digest,
                    content_byte_length=len(encoded),
                    adapter_admission_revision=(
                        adapter.admission_revision
                    ),
                    binding=event.binding,
                    input_turn_id=event.input_turn_id,
                    input_semantic_act_kind=(
                        event.semantic_act_kind
                    ),
                    sequence=event.sequence,
                    at_ms=event.at_ms,
                    expires_at_ms=expires_at_ms,
                    language_generation=(
                        language_choice.generation
                    ),
                    detected_locale=detected_locale,
                )
            except (TypeError, ValueError):
                language_choice.tombstone_pending_pair()
                return None
            admission = language_choice.bind_recovery_receipt(
                receipt
            )
            if admission is None:
                self._tombstones[receipt.receipt_id] = (
                    _ReceiptTombstone(
                        receipt_id=receipt.receipt_id,
                        binding=receipt.binding,
                        input_turn_id=receipt.input_turn_id,
                        sequence=receipt.sequence,
                        expires_at_ms=(
                            receipt.expires_at_ms
                            + self.max_ttl_ms
                        ),
                        accepted=False,
                    )
                )
                language_choice.tombstone_pending_pair()
                return None
            self._pending_recovery[receipt.receipt_id] = (
                receipt,
                admission,
                language_choice,
            )
            return receipt, admission

    def consume_language_recovery(
        self,
        receipt: LanguageRecoveryFinalTurnReceipt,
        admission: LanguageRecoveryAdmission,
        *,
        adapter: OfflineCandidateAdapter,
        language_choice: OfflineLanguageChoiceLifecycle,
        content: str,
        now_ms: int,
    ) -> bool:
        if (
            type(receipt) is not LanguageRecoveryFinalTurnReceipt
            or type(admission) is not LanguageRecoveryAdmission
            or type(language_choice)
            is not OfflineLanguageChoiceLifecycle
            or not self.accepts_adapter(adapter)
            or type(content) is not str
            or not content
            or len(content) > _MAX_CONTENT_CHARS
            or type(now_ms) is not int
            or now_ms < 0
            or len(content.encode("utf-8")) > _MAX_CONTENT_BYTES
        ):
            return False
        with self._lock:
            self._prune(now_ms=now_ms)
            pending = self._pending_recovery.get(
                receipt.receipt_id
            )
            if pending is None:
                return False
            expected, expected_admission, expected_choice = pending
            implementation = self._implementation_bindings[
                type(adapter)
            ]
            accepted = (
                expected is receipt
                and expected_admission is admission
                and expected_choice is language_choice
                and receipt.purpose
                is AdmissionPurpose.LANGUAGE_RECOVERY
                and admission.purpose
                is AdmissionPurpose.LANGUAGE_RECOVERY
                and now_ms <= receipt.expires_at_ms
                and receipt.arm is adapter.arm
                and receipt.binding == adapter.binding
                and receipt.adapter_implementation_digest
                == implementation.implementation_digest
                and receipt.adapter_configuration_digest
                == adapter.configuration_digest
                and receipt.adapter_admission_revision
                == adapter.admission_revision
                and not adapter.permit_admission_closed
                and receipt.content_digest
                == final_turn_content_digest(content)
                and receipt.content_byte_length
                == len(content.encode("utf-8"))
                and language_choice.consume_recovery_pair(
                    receipt=receipt,
                    admission=admission,
                    now_ms=now_ms,
                )
            )
            self._pending_recovery.pop(
                receipt.receipt_id,
                None,
            )
            if not accepted:
                language_choice.tombstone_pending_pair()
            self._tombstones[receipt.receipt_id] = (
                _ReceiptTombstone(
                    receipt_id=receipt.receipt_id,
                    binding=receipt.binding,
                    input_turn_id=receipt.input_turn_id,
                    sequence=receipt.sequence,
                    expires_at_ms=(
                        receipt.expires_at_ms
                        + self.max_ttl_ms
                    ),
                    accepted=accepted,
                )
            )
            return accepted

    def accepts_adapter(self, adapter: object) -> bool:
        return (
            adapter is self._adapter
            and _allowed_adapter(adapter)
            and type(adapter) in self._implementation_bindings
            and self._implementation_bindings[type(adapter)].arm is adapter.arm
        )

    def bind_disconnect_owner(self, owner: object) -> bool:
        """Bind disconnect retirement once to the exact composition owner."""
        with self._lock:
            if (
                type(owner) is not TurnCompositionTransaction
                or owner.receipts is not self
                or owner.adapter is not self._adapter
                or owner.lifecycle is not self._lifecycle
                or owner.binding != self._adapter.binding
            ):
                return False
            if self._disconnect_owner is None:
                self._disconnect_owner = owner
            return self._disconnect_owner is owner

    def retire_disconnected_session(
        self,
        *,
        event: VoiceEvent,
        owner: object,
    ) -> bool:
        """Tombstone every live receipt for one exact canonical disconnect."""
        if (
            owner is not self._disconnect_owner
            or type(owner) is not TurnCompositionTransaction
            or owner.receipts is not self
            or owner.adapter is not self._adapter
            or owner.lifecycle is not self._lifecycle
            or not self._lifecycle.accepts_session_disconnect(event)
            or not self._adapter.permit_admission_closed
        ):
            return False
        with self._lock:
            if owner is not self._disconnect_owner:
                return False
            self._terminalize_live_receipts(at_ms=event.at_ms)
            return not self._live and not self._pending_recovery

    def hard_terminalize_live_receipts(
        self,
        *,
        owner: object,
        at_ms: int,
    ) -> bool:
        """Fail-closed fallback for an exact bound composition owner."""
        if (
            owner is not self._disconnect_owner
            or type(owner) is not TurnCompositionTransaction
            or type(at_ms) is not int
            or at_ms < 0
        ):
            return False
        with self._lock:
            if owner is not self._disconnect_owner:
                return False
            self._terminalize_live_receipts(at_ms=at_ms)
            return not self._live and not self._pending_recovery

    @property
    def unconsumed_receipt_count(self) -> int:
        with self._lock:
            return len(self._live) + len(
                self._pending_recovery
            )

    def _terminalize_live_receipts(self, *, at_ms: int) -> None:
        for receipt_id, receipt in tuple(self._live.items()):
            self._live.pop(receipt_id)
            self._tombstones[receipt_id] = _ReceiptTombstone(
                receipt_id=receipt.receipt_id,
                binding=receipt.binding,
                input_turn_id=receipt.input_turn_id,
                sequence=receipt.sequence,
                expires_at_ms=(
                    max(receipt.expires_at_ms, at_ms)
                    + self.max_ttl_ms
                ),
                accepted=False,
            )
        for receipt_id, pending in tuple(
            self._pending_recovery.items()
        ):
            receipt, _, language_choice = pending
            self._pending_recovery.pop(receipt_id)
            language_choice.tombstone_pending_pair()
            self._tombstones[receipt_id] = _ReceiptTombstone(
                receipt_id=receipt.receipt_id,
                binding=receipt.binding,
                input_turn_id=receipt.input_turn_id,
                sequence=receipt.sequence,
                expires_at_ms=(
                    max(receipt.expires_at_ms, at_ms)
                    + self.max_ttl_ms
                ),
                accepted=False,
            )

    def _prune(self, *, now_ms: int) -> None:
        for receipt_id in tuple(
            key for key, receipt in self._live.items() if receipt.expires_at_ms < now_ms
        ):
            receipt = self._live.pop(receipt_id)
            self._tombstones[receipt_id] = _ReceiptTombstone(
                receipt_id=receipt.receipt_id,
                binding=receipt.binding,
                input_turn_id=receipt.input_turn_id,
                sequence=receipt.sequence,
                expires_at_ms=now_ms + self.max_ttl_ms,
                accepted=False,
            )
        for receipt_id in tuple(
            key
            for key, pending in self._pending_recovery.items()
            if pending[0].expires_at_ms < now_ms
        ):
            receipt, _, language_choice = (
                self._pending_recovery.pop(receipt_id)
            )
            language_choice.tombstone_pending_pair()
            self._tombstones[receipt_id] = _ReceiptTombstone(
                receipt_id=receipt.receipt_id,
                binding=receipt.binding,
                input_turn_id=receipt.input_turn_id,
                sequence=receipt.sequence,
                expires_at_ms=now_ms + self.max_ttl_ms,
                accepted=False,
            )
        for receipt_id in tuple(
            key for key, tombstone in self._tombstones.items() if tombstone.expires_at_ms < now_ms
        ):
            self._tombstones.pop(receipt_id, None)


@dataclass(frozen=True, slots=True)
class IntakeSnapshot:
    version: int
    state: IntakeState


class VersionedIntakeStore:
    """CAS-backed canonical intake state plus content-free turn authority."""

    def __init__(
        self,
        *,
        binding: VoiceSessionBinding,
        initial_state: IntakeState,
        initial_version: int = 0,
    ) -> None:
        if (
            not isinstance(binding, VoiceSessionBinding)
            or not isinstance(initial_state, IntakeState)
            or initial_state.side_effects_allowed
            or initial_state.call_sid != binding.call_binding
            or type(initial_version) is not int
            or initial_version < 0
        ):
            raise ValueError("intake store input is invalid")
        self.binding = binding
        self._state = _copy_state(initial_state)
        self._version = initial_version
        self._active_turn_id: str | None = None
        self._active_sequence = -1
        self._repair_consumed = False
        self._lock = RLock()

    def admit_turn(self, *, turn_id: str, sequence: int) -> bool:
        if not _identifier(turn_id) or type(sequence) is not int or sequence < 0:
            return False
        with self._lock:
            if sequence <= self._active_sequence:
                return False
            self._active_turn_id = turn_id
            self._active_sequence = sequence
            return True

    def is_current(self, *, turn_id: str, sequence: int) -> bool:
        with self._lock:
            return turn_id == self._active_turn_id and sequence == self._active_sequence

    def snapshot(self) -> IntakeSnapshot:
        with self._lock:
            return IntakeSnapshot(self._version, _copy_state(self._state))

    def commit_facts(
        self,
        *,
        expected_version: int,
        turn_id: str,
        sequence: int,
        staged: IntakeState,
    ) -> int | None:
        if (
            not isinstance(staged, IntakeState)
            or staged.side_effects_allowed
            or staged.call_sid != self.binding.call_binding
        ):
            return None
        with self._lock:
            if self._version != expected_version or not self.is_current(
                turn_id=turn_id, sequence=sequence
            ):
                return None
            self._state = _copy_state(staged)
            self._version += 1
            return self._version

    def revalidate(
        self,
        *,
        expected_version: int,
        turn_id: str,
        sequence: int,
    ) -> bool:
        with self._lock:
            return self._version == expected_version and self.is_current(
                turn_id=turn_id,
                sequence=sequence,
            )

    def mark_slot_asked(
        self,
        *,
        expected_version: int,
        turn_id: str,
        sequence: int,
        slot: str,
    ) -> int | None:
        with self._lock:
            if self._version != expected_version or not self.is_current(
                turn_id=turn_id, sequence=sequence
            ):
                return None
            staged = _copy_state(self._state)
            staged.mark_slot_asked(slot)
            self._state = staged
            self._version += 1
            return self._version

    def consume_repair(self, *, turn_id: str) -> bool:
        if not _identifier(turn_id):
            return False
        with self._lock:
            if (
                turn_id != self._active_turn_id
                or self._repair_consumed
            ):
                return False
            self._repair_consumed = True
            return True

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def current_state(self) -> IntakeState:
        with self._lock:
            return _copy_state(self._state)

    def delivery_guard(self):
        """Hold turn/version authority across playback evidence and state commit."""
        return self._lock


class CompositionPolicy:
    """The sole constructor of speech authorization for content proposals."""

    def authorize(
        self,
        *,
        action: NextAction | None,
        proposal: ContentProposal,
        binding: VoiceSessionBinding,
        turn_id: str,
        state_version: int,
    ) -> SpeechAuthorization:
        if (
            not isinstance(proposal, ContentProposal)
            or proposal.state_version != state_version
            or not isinstance(binding, VoiceSessionBinding)
            or not _identifier(turn_id)
            or proposal.proposal_kind
            not in {
                ProposalKind.PLANNED,
                ProposalKind.INPUT_REPAIR,
            }
        ):
            raise ValueError("proposal authorization input is invalid")
        acts = proposal.plan.acts
        questions = tuple(act for act in acts if act.kind is VoiceSemanticActKind.QUESTION)
        terminal = {
            VoiceSemanticActKind.CLOSING,
            VoiceSemanticActKind.LANGUAGE_CHOICE,
            VoiceSemanticActKind.OPT_OUT,
            VoiceSemanticActKind.VOICEMAIL,
        }
        if any(act.kind in terminal for act in acts):
            raise ValueError("normal composition cannot authorize terminal acts")
        if proposal.proposal_kind is ProposalKind.INPUT_REPAIR:
            if (
                action is not None
                or len(acts) != 1
                or acts[0].kind is not VoiceSemanticActKind.REPAIR
            ):
                raise ValueError("input repair proposal is invalid")
            answered_slots: tuple[str, ...] = ()
        else:
            if (
                not isinstance(action, NextAction)
                or proposal.action_name is not action.name
                or action.tool_calls_allowed
                or len(questions) != (1 if action.question_required else 0)
            ):
                raise ValueError("planned proposal is invalid")
            if questions:
                slot = questions[0].question_slot
                if slot not in action.allowed_slots or slot in action.forbidden_slots:
                    raise ValueError("question slot is not authorized")
            answers = tuple(act for act in acts if act.kind is VoiceSemanticActKind.ANSWER)
            if bool(answers) != (action.name is ActionName.ANSWER_DIRECT_QUESTION):
                raise ValueError("direct answer authority is invalid")
            safety = tuple(act for act in acts if act.kind is VoiceSemanticActKind.SAFETY)
            if bool(safety) != (action.name is ActionName.SAFETY_GUIDANCE):
                raise ValueError("safety authority is invalid")
            answered_slots = action.forbidden_slots
        kinds = tuple(dict.fromkeys(act.kind for act in acts))
        return SpeechAuthorization(
            binding=binding,
            turn_id=turn_id,
            authorized_kinds=kinds,
            terminal_allowed=False,
            answered_slots=answered_slots,
            locale=proposal.locale,
        )

    def authorize_lifecycle(
        self,
        *,
        proposal: ContentProposal,
        binding: VoiceSessionBinding,
        turn_id: str,
        state_version: int,
    ) -> SpeechAuthorization:
        if (
            not isinstance(proposal, ContentProposal)
            or proposal.state_version != state_version
            or not isinstance(binding, VoiceSessionBinding)
            or not _identifier(turn_id)
            or len(proposal.plan.acts) != 1
        ):
            raise ValueError("lifecycle authorization input is invalid")
        expected = {
            ProposalKind.PRESENCE_CHECK:
                VoiceSemanticActKind.PRESENCE_CHECK,
            ProposalKind.MORE_TIME_ACKNOWLEDGEMENT:
                VoiceSemanticActKind.ACKNOWLEDGEMENT,
            ProposalKind.UNSUPPORTED_ACCESS_MODE:
                VoiceSemanticActKind.ACKNOWLEDGEMENT,
            ProposalKind.SIMULATED_VOICEMAIL:
                VoiceSemanticActKind.ACKNOWLEDGEMENT,
            ProposalKind.SILENCE_CLOSURE:
                VoiceSemanticActKind.CLOSING,
        }.get(proposal.proposal_kind)
        act = proposal.plan.acts[0]
        if (
            expected is None
            or proposal.action_name is not None
            or act.kind is not expected
            or act.question_slot is not None
            or act.private_disclosure
            or act.unsupported_promise
            or not act.complete
        ):
            raise ValueError("lifecycle proposal is invalid")
        return SpeechAuthorization(
            binding=binding,
            turn_id=turn_id,
            authorized_kinds=(act.kind,),
            terminal_allowed=(
                act.kind is VoiceSemanticActKind.CLOSING
            ),
            answered_slots=(),
            locale=proposal.locale,
        )

    def authorize_language_choice(
        self,
        *,
        proposal: LanguageChoiceProposal,
        binding: VoiceSessionBinding,
        turn_id: str,
        state_version: int,
    ) -> SpeechAuthorization:
        """Authorize only the exact immutable multilingual choice asset."""
        if (
            type(proposal) is not LanguageChoiceProposal
            or proposal.state_version != state_version
            or not isinstance(binding, VoiceSessionBinding)
            or not _identifier(turn_id)
            or len(proposal.plan.acts) != 3
            or any(
                act.kind
                is not VoiceSemanticActKind.LANGUAGE_CHOICE
                or act.question_slot is not None
                or act.private_disclosure
                or act.unsupported_promise
                or not act.complete
                for act in proposal.plan.acts
            )
        ):
            raise ValueError(
                "language choice authorization input is invalid"
            )
        return SpeechAuthorization(
            binding=binding,
            turn_id=turn_id,
            authorized_kinds=(
                VoiceSemanticActKind.LANGUAGE_CHOICE,
            ),
            terminal_allowed=False,
            answered_slots=(),
            locale="mul",
        )

    def authorize_replay(
        self,
        *,
        source: ReplaySource,
        binding: VoiceSessionBinding,
        turn_id: str,
        state_version: int,
    ) -> SpeechAuthorization:
        terminal = {
            VoiceSemanticActKind.CLOSING,
            VoiceSemanticActKind.OPT_OUT,
            VoiceSemanticActKind.VOICEMAIL,
        }
        if (
            not isinstance(source, ReplaySource)
            or source.binding != binding
            or source.kind in terminal
            or not isinstance(binding, VoiceSessionBinding)
            or not _identifier(turn_id)
            or turn_id == source.turn_id
            or type(state_version) is not int
            or state_version < 0
        ):
            raise ValueError("replay authorization input is invalid")
        return SpeechAuthorization(
            binding=binding,
            turn_id=turn_id,
            authorized_kinds=(source.kind,),
            terminal_allowed=False,
            answered_slots=(),
            locale=source.locale,
        )


@dataclass(frozen=True, slots=True)
class CompositionResult:
    status: CompositionStatus
    phase: CompositionPhase
    receipt_id: str
    input_turn_id: str
    state_version: int
    act_ids: tuple[str, ...] = ()
    act_kinds: tuple[VoiceSemanticActKind, ...] = ()
    replay_mode: ReplayMode | None = None
    replay_source_act_id: str | None = None
    closure_trigger: ClosureTrigger | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status, CompositionStatus)
            or not isinstance(self.phase, CompositionPhase)
            or not _identifier(self.receipt_id)
            or not _identifier(self.input_turn_id)
            or type(self.state_version) is not int
            or self.state_version < 0
            or len(self.act_ids) != len(self.act_kinds)
            or any(not _identifier(value) for value in self.act_ids)
            or any(not isinstance(value, VoiceSemanticActKind) for value in self.act_kinds)
            or (
                (self.replay_mode is None)
                != (self.replay_source_act_id is None)
            )
            or (
                self.replay_mode is not None
                and (
                    not isinstance(self.replay_mode, ReplayMode)
                    or not _identifier(
                        self.replay_source_act_id
                    )
                    or self.status
                    not in {
                        CompositionStatus.REPLAY_PENDING,
                        CompositionStatus.REPLAY_OBSERVED,
                        CompositionStatus.SUPERSEDED,
                        CompositionStatus.TERMINAL_FAILURE,
                    }
                )
            )
            or (
                self.status is CompositionStatus.CLOSURE_REQUIRED
                and self.closure_trigger
                is not ClosureTrigger.REPAIR_EXHAUSTED
            )
            or (
                self.status is not CompositionStatus.CLOSURE_REQUIRED
                and self.closure_trigger is not None
            )
            or (
                self.status
                is CompositionStatus.LANGUAGE_CHOICE_REQUIRED
                and (
                    self.phase is not CompositionPhase.FACTS_COMMITTED
                    or self.act_ids
                    or self.reason != "unlisted_language"
                )
            )
            or (
                self.status
                in {
                    CompositionStatus.LANGUAGE_CHOICE_PENDING,
                    CompositionStatus.LANGUAGE_CHOICE_WINDOW,
                }
                and (
                    len(self.act_kinds) != 3
                    or any(
                        kind
                        is not VoiceSemanticActKind.LANGUAGE_CHOICE
                        for kind in self.act_kinds
                    )
                )
            )
        ):
            raise ValueError("composition result is invalid")


@dataclass(slots=True)
class _PendingResponse:
    receipt_id: str
    outcome_expires_at_ms: int
    input_turn_id: str
    turn_sequence: int
    state_version: int
    reserved: tuple[ReservedSpeech, ...]
    authorizations: tuple[VoiceEvent, ...]
    question_slot: str | None
    permitted_act_ids: set[str]
    confirmed_act_ids: set[str]
    tts_act_ids: set[str]
    playout_act_ids: set[str]
    transport_act_ids: set[str]
    observed_act_ids: set[str]
    retired_permit_act_ids: set[str]
    replay_mode: ReplayMode | None = None
    replay_source_act_id: str | None = None
    language_choice: OfflineLanguageChoiceLifecycle | None = None


class TurnCompositionTransaction:
    """Serialize one exact binding from final input through pending playback."""

    def __init__(
        self,
        *,
        binding: VoiceSessionBinding,
        adapter: OfflineCandidateAdapter,
        lifecycle: VoiceLifecycle,
        extractor: ObservationExtractor,
        receipts: FinalTurnAdmissionAuthority,
        state: VersionedIntakeStore,
        coordinator: VoiceBakeoffCoordinator,
        materializer: FixedProposalMaterializer,
        policy: CompositionPolicy,
        max_outcomes: int = 256,
    ) -> None:
        if (
            not isinstance(binding, VoiceSessionBinding)
            or not _allowed_adapter(adapter)
            or adapter.binding != binding
            or adapter.canonical_lifecycle is not lifecycle
            or type(lifecycle) is not VoiceLifecycle
            or lifecycle.binding != binding
            or type(extractor) is not ObservationExtractor
            or extractor.binding != binding
            or type(receipts) is not FinalTurnAdmissionAuthority
            or not receipts.accepts_adapter(adapter)
            or type(state) is not VersionedIntakeStore
            or state.binding != binding
            or type(coordinator) is not VoiceBakeoffCoordinator
            or coordinator.calls.binding != binding
            or coordinator.calls.voice_lifecycle is not lifecycle
            or type(materializer) is not FixedProposalMaterializer
            or type(policy) is not CompositionPolicy
            or type(max_outcomes) is not int
            or max_outcomes < 1
        ):
            raise ValueError("turn composition dependencies are invalid")
        self.binding = binding
        self.adapter = adapter
        self.lifecycle = lifecycle
        self.extractor = extractor
        self.receipts = receipts
        self.state = state
        self.coordinator = coordinator
        self.materializer = materializer
        self.policy = policy
        self.max_outcomes = max_outcomes
        self._outcomes: dict[str, CompositionResult] = {}
        self._outcome_expiries: dict[str, int] = {}
        self._pending_by_act: dict[str, _PendingResponse] = {}
        self._terminalizing_receipt_ids: set[str] = set()
        self._lock = RLock()
        if not receipts.bind_disconnect_owner(self):
            raise ValueError("turn composition disconnect authority is invalid")
        if type(adapter) is ManualNativeAdapter and not adapter.bind_timeout_materializer(
            self
        ):
            raise ValueError("turn composition timeout authority is invalid")

    def execute(
        self,
        receipt: FinalTurnAdmissionReceipt,
        *,
        content: str,
        backend: ObservationBackend,
        now_ms: int,
    ) -> CompositionResult:
        with self._lock:
            self._prune_outcomes(now_ms=now_ms)
            if type(receipt) is FinalTurnAdmissionReceipt:
                replay = self._outcomes.get(receipt.receipt_id)
                if replay is not None:
                    return replay
            if type(receipt) is not FinalTurnAdmissionReceipt or not self.receipts.consume(
                receipt,
                adapter=self.adapter,
                content=content,
                now_ms=now_ms,
            ):
                return _untracked_rejection(receipt, self.state.version)
            return self._execute_admitted(
                receipt,
                content=content,
                backend=backend,
            )

    def execute_language_recovery(
        self,
        receipt: LanguageRecoveryFinalTurnReceipt,
        admission: LanguageRecoveryAdmission,
        *,
        language_choice: OfflineLanguageChoiceLifecycle,
        content: str,
        backend: ObservationBackend,
        now_ms: int,
    ) -> CompositionResult:
        """Consume the exact paired recovery authority, then compose normally."""
        with self._lock:
            self._prune_outcomes(now_ms=now_ms)
            if type(receipt) is LanguageRecoveryFinalTurnReceipt:
                replay = self._outcomes.get(receipt.receipt_id)
                if replay is not None:
                    return replay
            if (
                type(receipt)
                is not LanguageRecoveryFinalTurnReceipt
                or type(admission) is not LanguageRecoveryAdmission
                or type(language_choice)
                is not OfflineLanguageChoiceLifecycle
                or language_choice.binding != self.binding
                or not self.receipts.consume_language_recovery(
                    receipt,
                    admission,
                    adapter=self.adapter,
                    language_choice=language_choice,
                    content=content,
                    now_ms=now_ms,
                )
            ):
                return _untracked_rejection(
                    receipt,
                    self.state.version,
                )
            return self._execute_admitted(
                receipt,
                content=content,
                backend=backend,
                language_choice=language_choice,
            )

    def _execute_admitted(
        self,
        receipt: (
            FinalTurnAdmissionReceipt
            | LanguageRecoveryFinalTurnReceipt
        ),
        *,
        content: str,
        backend: ObservationBackend,
        language_choice: (
            OfflineLanguageChoiceLifecycle | None
        ) = None,
    ) -> CompositionResult:
        """Compose only after the caller receipt's owning authority consumed it."""
        with self._lock:
            if (
                receipt.input_semantic_act_kind
                is not VoiceSemanticActKind.ACKNOWLEDGEMENT
            ):
                if language_choice is not None:
                    language_choice.tombstone_pending_pair()
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="input_semantic_kind",
                )
            if not self.state.admit_turn(
                turn_id=receipt.input_turn_id,
                sequence=receipt.sequence,
            ):
                if language_choice is not None:
                    language_choice.tombstone_pending_pair()
                return self._record(
                    receipt,
                    CompositionStatus.SUPERSEDED,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="stale_turn",
                )
            if not self._supersede_pending(receipt):
                if language_choice is not None:
                    language_choice.tombstone_pending_pair()
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="supersede_compensation_failed",
                )
            if len(self._outcomes) >= self.max_outcomes:
                if language_choice is not None:
                    language_choice.tombstone_pending_pair()
                return _untracked_capacity(
                    receipt,
                    self.state.version,
                )
            turn = CandidateFinalTurn(
                binding=receipt.binding,
                input_turn_id=receipt.input_turn_id,
                sequence=receipt.sequence,
                at_ms=receipt.at_ms,
                finality=Finality.FINAL,
                content=content,
                admission_id=receipt.receipt_id,
            )
            extraction = self.extractor.extract(
                turn,
                backend=backend,
                current_turn=lambda candidate: self.state.is_current(
                    turn_id=candidate.input_turn_id,
                    sequence=candidate.sequence,
                ),
            )
            if extraction.outcome is not ExtractionOutcome.ACCEPTED:
                if language_choice is not None:
                    language_choice.tombstone_pending_pair()
                    self._seal_replay_authority()
                    return self._record(
                        receipt,
                        CompositionStatus.TERMINAL_FAILURE,
                        CompositionPhase.EXTRACTION_TERMINAL,
                        self.state.version,
                        reason=(
                            "language_recovery_extraction_"
                            f"{extraction.outcome.value}"
                        ),
                    )
                return self._handle_extraction_failure(
                    receipt=receipt,
                    outcome=extraction.outcome,
                )
            assert extraction.observation is not None
            if (
                language_choice is not None
                and not language_choice.validate_recovery_locale(
                    extraction.observation.language
                )
            ):
                self._seal_replay_authority()
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.EXTRACTION_TERMINAL,
                    self.state.version,
                    reason="language_recovery_locale_mismatch",
                )
            snapshot = self.state.snapshot()
            staged = _copy_state(snapshot.state)
            staged.apply_caller_observation(extraction.observation)
            staged.side_effects_allowed = False
            committed_version = self.state.commit_facts(
                expected_version=snapshot.version,
                turn_id=receipt.input_turn_id,
                sequence=receipt.sequence,
                staged=staged,
            )
            if committed_version is None:
                if language_choice is not None:
                    language_choice.tombstone_pending_pair()
                return self._record(
                    receipt,
                    CompositionStatus.SUPERSEDED,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="facts_cas",
                )
            if (
                language_choice is not None
                and not language_choice.commit_recovery()
            ):
                self._seal_replay_authority()
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.FACTS_COMMITTED,
                    committed_version,
                    reason="language_recovery_commit",
                )
            if not self.materializer.supports_locale(
                staged.language
            ):
                return self._record(
                    receipt,
                    CompositionStatus.LANGUAGE_CHOICE_REQUIRED,
                    CompositionPhase.FACTS_COMMITTED,
                    committed_version,
                    reason="unlisted_language",
                )
            action = plan_next_action(staged)
            try:
                proposal = self.materializer.materialize(
                    action=action,
                    state_version=committed_version,
                    locale=staged.language,
                    safe_facts=(),
                )
            except (TypeError, ValueError):
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.FACTS_COMMITTED,
                    committed_version,
                    reason="proposal",
                )
            return self._prepare_response(
                receipt=receipt,
                action=action,
                proposal=proposal,
                state_version=committed_version,
                pending_status=CompositionStatus.RESPONSE_PENDING,
            )

    def execute_replay(
        self,
        receipt: FinalTurnAdmissionReceipt,
        *,
        content: str,
        command: VoiceSemanticActKind,
        now_ms: int,
    ) -> CompositionResult:
        """Consume one typed caller repeat/slower turn without extraction."""
        with self._lock:
            self._prune_outcomes(now_ms=now_ms)
            if isinstance(receipt, FinalTurnAdmissionReceipt):
                replay = self._outcomes.get(receipt.receipt_id)
                if replay is not None:
                    return replay
            if (
                not isinstance(command, VoiceSemanticActKind)
                or command
                not in {
                    VoiceSemanticActKind.REPEAT,
                    VoiceSemanticActKind.SLOWER_SPEECH,
                }
                or not isinstance(receipt, FinalTurnAdmissionReceipt)
                or not self.receipts.consume(
                    receipt,
                    adapter=self.adapter,
                    content=content,
                    now_ms=now_ms,
                )
            ):
                return _untracked_rejection(
                    receipt,
                    self.state.version,
                )
            if receipt.input_semantic_act_kind is not command:
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="replay_command_mismatch",
                )
            if not self.state.admit_turn(
                turn_id=receipt.input_turn_id,
                sequence=receipt.sequence,
            ):
                return self._record(
                    receipt,
                    CompositionStatus.SUPERSEDED,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="stale_replay_turn",
                )
            if not self._supersede_pending(receipt):
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="replay_supersede_compensation_failed",
                )
            if len(self._outcomes) >= self.max_outcomes:
                return _untracked_capacity(
                    receipt,
                    self.state.version,
                )
            if (
                self.coordinator.calls.phase
                is SilencePhase.TERMINATED
                or self.adapter.permit_admission_closed
            ):
                self._seal_replay_authority()
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="replay_authority_closed",
                )
            if not self._cancel_call(
                    event_id=f"replay_activity_{receipt.sequence}",
                    at_ms=receipt.at_ms,
            ):
                self._seal_replay_authority()
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="replay_call_cleanup",
                )
            try:
                source = (
                    self.coordinator.speech.latest_replay_source(
                        self.binding
                    )
                )
            except Exception:  # noqa: BLE001
                self._seal_replay_authority()
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="replay_source_inventory",
                )
            if source is None:
                return self._record(
                    receipt,
                    CompositionStatus.SILENT,
                    CompositionPhase.EXTRACTION_TERMINAL,
                    self.state.version,
                    reason="no_caller_observed_replay_source",
                )
            mode = (
                ReplayMode.EXACT
                if command is VoiceSemanticActKind.REPEAT
                else ReplayMode.SLOWER
            )
            state_version = self.state.version
            try:
                authorization = self.policy.authorize_replay(
                    source=source,
                    binding=self.binding,
                    turn_id=receipt.input_turn_id,
                    state_version=state_version,
                )
                reserve_sequence, reserve_at_ms = (
                    self.lifecycle.next_position(
                        at_ms=receipt.at_ms
                    )
                )
                reserved = self.coordinator.reserve_replay_batch(
                    source=source,
                    request_id=receipt.receipt_id,
                    mode=mode,
                    authorization=authorization,
                    event_id=f"reserve_replay_{receipt.sequence}",
                    sequence=reserve_sequence,
                    at_ms=reserve_at_ms,
                    turn_sequence=receipt.sequence,
                )
            except (TypeError, ValueError):
                reserved = ()
            except Exception:  # noqa: BLE001
                self._seal_replay_authority()
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.TERMINAL,
                    state_version,
                    replay_mode=mode,
                    replay_source_act_id=source.act_id,
                    reason="replay_reservation_cleanup",
                )
            if len(reserved) != 1:
                self._seal_replay_authority()
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.FACTS_COMMITTED,
                    state_version,
                    replay_mode=mode,
                    replay_source_act_id=source.act_id,
                    reason="replay_reservation",
                )
            item = reserved[0]
            with self.state.delivery_guard():
                if not self.state.revalidate(
                    expected_version=state_version,
                    turn_id=receipt.input_turn_id,
                    sequence=receipt.sequence,
                ):
                    if not self.coordinator.rollback_batch(reserved):
                        return self._terminalize_partial(
                            receipt=receipt,
                            reserved=reserved,
                            canonical=(),
                            accepted_permits=(),
                            state_version=state_version,
                            reason="replay_cas_rollback_failed",
                            replay_mode=mode,
                            replay_source_act_id=source.act_id,
                        )
                    return self._record(
                        receipt,
                        CompositionStatus.SUPERSEDED,
                        CompositionPhase.TERMINAL,
                        self.state.version,
                        replay_mode=mode,
                        replay_source_act_id=source.act_id,
                        reason="replay_cas",
                    )
                if not self.coordinator.speech.authorize_text(
                    item.act_id,
                    item.text,
                ):
                    return self._compensate(
                        receipt=receipt,
                        reserved=reserved,
                        canonical=(),
                        accepted_permits=(),
                        state_version=state_version,
                        reason="replay_speech_authorization",
                        replay_mode=mode,
                        replay_source_act_id=source.act_id,
                    )
                sequence, canonical_at_ms = (
                    self.lifecycle.next_position(
                        at_ms=receipt.at_ms
                    )
                )
                canonical = VoiceEvent(
                    schema_version=VOICE_SCHEMA_VERSION,
                    kind=VoiceEventKind.RESPONSE_AUTHORIZED,
                    source=VoiceSource.LOCAL_AUTHORITATIVE,
                    sensitivity=VoiceSensitivity.OPERATIONAL,
                    binding=self.binding,
                    sequence=sequence,
                    at_ms=canonical_at_ms,
                    input_turn_id=receipt.input_turn_id,
                    generation_id=(
                        f"replay_generation_{item.act_id}"
                    ),
                    semantic_act_id=item.act_id,
                    semantic_act_kind=item.kind,
                    payload=VoicePayload(),
                )
                canonical_events: tuple[VoiceEvent, ...] = ()
                accepted_permits: tuple[VoiceEvent, ...] = ()
                if self.lifecycle.ingest(canonical):
                    canonical_events = (canonical,)
                else:
                    return self._compensate(
                        receipt=receipt,
                        reserved=reserved,
                        canonical=canonical_events,
                        accepted_permits=accepted_permits,
                        state_version=state_version,
                        reason="replay_canonical_authorization",
                        replay_mode=mode,
                        replay_source_act_id=source.act_id,
                    )
                if self.adapter.accept_permit(
                    canonical,
                    lifecycle=self.lifecycle,
                ):
                    accepted_permits = (canonical,)
                else:
                    return self._compensate(
                        receipt=receipt,
                        reserved=reserved,
                        canonical=canonical_events,
                        accepted_permits=accepted_permits,
                        state_version=state_version,
                        reason="replay_adapter_permit",
                        replay_mode=mode,
                        replay_source_act_id=source.act_id,
                    )
                pending = _PendingResponse(
                    receipt_id=receipt.receipt_id,
                    outcome_expires_at_ms=(
                        receipt.expires_at_ms
                        + self.receipts.max_ttl_ms
                    ),
                    input_turn_id=receipt.input_turn_id,
                    turn_sequence=receipt.sequence,
                    state_version=state_version,
                    reserved=reserved,
                    authorizations=canonical_events,
                    question_slot=source.question_slot,
                    permitted_act_ids={item.act_id},
                    confirmed_act_ids=set(),
                    tts_act_ids=set(),
                    playout_act_ids=set(),
                    transport_act_ids=set(),
                    observed_act_ids=set(),
                    retired_permit_act_ids=set(),
                    replay_mode=mode,
                    replay_source_act_id=source.act_id,
                )
                self._pending_by_act[item.act_id] = pending
                return self._record(
                    receipt,
                    CompositionStatus.REPLAY_PENDING,
                    CompositionPhase.RESPONSE_PENDING_PLAYBACK,
                    state_version,
                    act_ids=(item.act_id,),
                    act_kinds=(item.kind,),
                    replay_mode=mode,
                    replay_source_act_id=source.act_id,
                )

    def _materialize_timeout_from_adapter(
        self,
        intent: VoiceTimeoutIntent,
        lifecycle: VoiceLifecycle,
        at_ms: int,
    ) -> VoiceEvent | None:
        """Route the bound adapter's public request through this transaction."""
        if lifecycle is not self.lifecycle:
            return None
        committed = self.materialize_timeout(
            intent=intent,
            authority=self.adapter,
            at_ms=at_ms,
        )
        return None if committed is None else committed[0]

    def _terminalize_owned_timeout_authority(
        self,
        reason: str,
        at_ms: int,
    ) -> bool:
        """Compensate every response owned by externally closed Arm C authority."""
        if (
            reason not in {
                "act_timed_out",
                "timeout_authority_terminalized",
            }
            or type(at_ms) is not int
            or at_ms < 0
        ):
            return False
        with self._lock:
            pending_responses = {
                id(pending): pending
                for pending in self._pending_by_act.values()
                if pending.receipt_id
                not in self._terminalizing_receipt_ids
            }
            for pending in pending_responses.values():
                self._hard_terminalize_pending(
                    pending,
                    reason=reason,
                    at_ms=at_ms,
                )
            return all(
                pending.receipt_id
                in self._terminalizing_receipt_ids
                for pending in self._pending_by_act.values()
            )

    def materialize_timeout(
        self,
        *,
        intent: VoiceTimeoutIntent,
        authority: OfflineCandidateAdapter,
        at_ms: int,
    ) -> tuple[VoiceEvent, CompositionResult] | None:
        """Commit one Arm C timeout and terminalize its owned response batch."""
        with self._lock:
            if (
                type(self.adapter) is not ManualNativeAdapter
                or authority is not self.adapter
                or not isinstance(intent, VoiceTimeoutIntent)
                or intent.binding != self.binding
            ):
                return None
            pending = self._pending_by_act.get(intent.semantic_act_id)
            if (
                pending is None
                or intent.semantic_act_id
                != self._next_unobserved_act_id(pending)
                or intent.semantic_act_id not in pending.permitted_act_ids
                or intent.semantic_act_id in pending.observed_act_ids
            ):
                return None
            try:
                event = self.adapter._commit_timeout_from_owner(
                    intent,
                    lifecycle=self.lifecycle,
                    at_ms=at_ms,
                    owner=self,
                )
            except Exception:  # noqa: BLE001
                if any(
                    self._pending_by_act.get(item.act_id) is pending
                    for item in pending.reserved
                ):
                    self._hard_terminalize_pending(
                        pending,
                        reason="timeout_commit_failed",
                        at_ms=at_ms,
                    )
                return None
            if event is None:
                if (
                    self.adapter.terminally_closed
                    and any(
                        self._pending_by_act.get(item.act_id) is pending
                        for item in pending.reserved
                    )
                ):
                    self._hard_terminalize_pending(
                        pending,
                        reason="timeout_authority_terminalized",
                        at_ms=at_ms,
                    )
                return None
            if any(
                self._pending_by_act.get(item.act_id) is pending
                for item in pending.reserved
            ):
                result = self._hard_terminalize_pending(
                    pending,
                    reason="act_timed_out",
                    at_ms=event.at_ms,
                )
            else:
                result = self._outcomes.get(pending.receipt_id)
                if result is None:
                    return None
            return event, result

    def accept_semantic_confirmation(
        self,
        *,
        event: VoiceEvent,
        event_id: str,
        sequence: int,
    ) -> bool:
        with self._lock:
            pending = self._pending_by_act.get(
                event.semantic_act_id if isinstance(event, VoiceEvent) else ""
            )
            if (
                pending is None
                or event.semantic_act_id
                != self._next_unobserved_act_id(pending)
                or event.semantic_act_id
                not in pending.permitted_act_ids
            ):
                return False
            authorization = self._authorization(
                pending,
                event.semantic_act_id,
            )
            if (
                authorization is None
                or not self.coordinator.speech.is_live(
                    event.semantic_act_id
                )
                or not self.adapter.has_permit(authorization)
            ):
                return False
            if not self.adapter.can_accept_semantic_confirmation(
                event,
                lifecycle=self.lifecycle,
            ):
                return False
            if event.semantic_act_kind is VoiceSemanticActKind.QUESTION:
                if not self.coordinator.calls.can_semantic_confirm(
                    event
                ) or not self.coordinator.calls.can_accept_position(
                    binding=event.binding,
                    event_id=event_id,
                    sequence=sequence,
                    at_ms=event.at_ms,
                ):
                    return False
                if not self.coordinator.semantic_confirmed(
                    event=event,
                    event_id=event_id,
                    sequence=sequence,
                ):
                    return False
            if not self.adapter.accept_semantic_confirmation(
                event,
                lifecycle=self.lifecycle,
            ):
                self._hard_terminalize_pending(
                    pending,
                    reason="semantic_confirmation_commit_failed",
                    at_ms=event.at_ms,
                )
                return False
            pending.confirmed_act_ids.add(event.semantic_act_id)
            return True

    def accept_tts_binding(self, *, event: VoiceEvent) -> bool:
        """Bind canonical TTS to the exact authorized response text."""
        with self._lock:
            pending = self._pending_by_act.get(
                event.semantic_act_id
                if isinstance(event, VoiceEvent)
                else ""
            )
            if pending is None:
                return False
            failure_at_ms = (
                event.at_ms
                if isinstance(event, VoiceEvent)
                else 0
            )
            with self.state.delivery_guard():
                if not self.state.revalidate(
                    expected_version=pending.state_version,
                    turn_id=pending.input_turn_id,
                    sequence=pending.turn_sequence,
                ):
                    self._hard_terminalize_pending(
                        pending,
                        reason="tts_binding_state_drift",
                        at_ms=failure_at_ms,
                    )
                    return False
                authorization = self._authorization(
                    pending,
                    event.semantic_act_id,
                )
                if (
                    authorization is None
                    or not self._matches_lifecycle_binding(
                        event=event,
                        authorization=authorization,
                        kind=VoiceEventKind.TTS_BOUND,
                    )
                    or event.semantic_act_id
                    != self._next_unobserved_act_id(pending)
                    or event.semantic_act_id
                    not in pending.confirmed_act_ids
                    or event.semantic_act_id in pending.tts_act_ids
                    or self.lifecycle.act_state(authorization)
                    is not VoiceEventKind.TTS_BOUND
                    or not self.coordinator.speech.is_live(
                        event.semantic_act_id
                    )
                    or not self.adapter.has_permit(authorization)
                    or event.payload.audio_id is None
                ):
                    self._hard_terminalize_pending(
                        pending,
                        reason="tts_binding_validation_failed",
                        at_ms=failure_at_ms,
                    )
                    return False
                try:
                    accepted = self.coordinator.speech.bind_tts(
                        event.semantic_act_id,
                        audio_id=event.payload.audio_id,
                    )
                except Exception:  # noqa: BLE001
                    accepted = False
                binding = self.coordinator.speech.audio_binding(
                    event.semantic_act_id
                )
                if (
                    not accepted
                    or binding is None
                    or binding.binding != self.binding
                    or binding.text_digest
                    != event.payload.text_digest
                    or binding.audio_id != event.payload.audio_id
                ):
                    self._hard_terminalize_pending(
                        pending,
                        reason="tts_binding_commit_failed",
                        at_ms=failure_at_ms,
                    )
                    return False
                pending.tts_act_ids.add(event.semantic_act_id)
                return True

    def accept_playout_binding(self, *, event: VoiceEvent) -> bool:
        """Bind canonical playout to the exact SpeechControl audio."""
        with self._lock:
            pending = self._pending_by_act.get(
                event.semantic_act_id
                if isinstance(event, VoiceEvent)
                else ""
            )
            if pending is None:
                return False
            failure_at_ms = (
                event.at_ms
                if isinstance(event, VoiceEvent)
                else 0
            )
            with self.state.delivery_guard():
                if not self.state.revalidate(
                    expected_version=pending.state_version,
                    turn_id=pending.input_turn_id,
                    sequence=pending.turn_sequence,
                ):
                    self._hard_terminalize_pending(
                        pending,
                        reason="playout_binding_state_drift",
                        at_ms=failure_at_ms,
                    )
                    return False
                authorization = self._authorization(
                    pending,
                    event.semantic_act_id,
                )
                if (
                    authorization is None
                    or not self._matches_lifecycle_binding(
                        event=event,
                        authorization=authorization,
                        kind=VoiceEventKind.PLAYOUT_BOUND,
                    )
                    or event.semantic_act_id
                    != self._next_unobserved_act_id(pending)
                    or event.semantic_act_id
                    not in pending.tts_act_ids
                    or event.semantic_act_id
                    in pending.playout_act_ids
                    or self.lifecycle.act_state(authorization)
                    is not VoiceEventKind.PLAYOUT_BOUND
                    or not self.coordinator.speech.is_live(
                        event.semantic_act_id
                    )
                    or not self.adapter.has_permit(authorization)
                    or event.payload.playout_id is None
                ):
                    self._hard_terminalize_pending(
                        pending,
                        reason=(
                            "playout_binding_validation_failed"
                        ),
                        at_ms=failure_at_ms,
                    )
                    return False
                try:
                    accepted = (
                        self.coordinator.speech.bind_playout(
                            event.semantic_act_id,
                            playout_id=event.payload.playout_id,
                        )
                    )
                except Exception:  # noqa: BLE001
                    accepted = False
                binding = self.coordinator.speech.playout_binding(
                    event.semantic_act_id
                )
                if (
                    not accepted
                    or binding is None
                    or binding.binding != self.binding
                    or binding.text_digest
                    != event.payload.text_digest
                    or binding.audio_id != event.payload.audio_id
                    or binding.playout_id
                    != event.payload.playout_id
                ):
                    self._hard_terminalize_pending(
                        pending,
                        reason="playout_binding_commit_failed",
                        at_ms=failure_at_ms,
                    )
                    return False
                pending.playout_act_ids.add(
                    event.semantic_act_id
                )
                return True

    def authorization_receipt(self, act_id: str) -> VoiceEvent | None:
        """Expose only the exact canonical operational receipt for offline driving."""
        if not _identifier(act_id):
            return None
        with self._lock:
            pending = self._pending_by_act.get(act_id)
            if pending is None:
                return None
            authorization = next(
                (
                    event
                    for event in pending.authorizations
                    if event.semantic_act_id == act_id
                ),
                None,
            )
            if (
                authorization is None
                or act_id != self._next_unobserved_act_id(pending)
                or act_id not in pending.permitted_act_ids
                or not self.adapter.has_permit(authorization)
            ):
                return None
            return authorization

    def abort(self, *, at_ms: int) -> bool:
        """Idempotently seal every transaction-owned local capability."""
        with self._lock:
            canonical_at_ms = (
                at_ms
                if type(at_ms) is int and at_ms >= 0
                else 0
            )
            try:
                self.adapter.terminalize_permit_admission()
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                receipts_closed = (
                    self.receipts.hard_terminalize_live_receipts(
                        owner=self,
                        at_ms=canonical_at_ms,
                    )
                )
            except Exception:  # noqa: BLE001
                receipts_closed = False
            pending_responses = {
                id(pending): pending
                for pending in self._pending_by_act.values()
            }
            pending_closed = True
            for pending in pending_responses.values():
                try:
                    self._hard_terminalize_pending(
                        pending,
                        reason="transaction_aborted",
                        at_ms=canonical_at_ms,
                    )
                except Exception:  # noqa: BLE001
                    pending_closed = False
            try:
                self.coordinator.calls.hard_terminalize()
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                speech_closed = (
                    self._hard_terminalize_session_speech()
                )
            except Exception:  # noqa: BLE001
                speech_closed = False
            return (
                receipts_closed
                and pending_closed
                and not self._pending_by_act
                and self.receipts.unconsumed_receipt_count == 0
                and self.coordinator.speech
                .reservation_batch_count(self.binding)
                == 0
                and speech_closed
                and self.adapter.terminally_closed
                and self.coordinator.calls.phase
                is SilencePhase.TERMINATED
                and self.coordinator.calls.is_quiescent
            )

    def terminalize_disconnected_session(
        self,
        *,
        event: VoiceEvent,
    ) -> bool:
        """Retire all old-session authority after an exact canonical disconnect."""
        with self._lock:
            if (
                not self.lifecycle.accepts_session_disconnect(event)
                or not self.adapter.permit_admission_closed
            ):
                return False
            pending_responses = {
                id(pending): pending
                for pending in self._pending_by_act.values()
            }
            if not self.receipts.retire_disconnected_session(
                event=event,
                owner=self,
            ):
                self.receipts.hard_terminalize_live_receipts(
                    owner=self,
                    at_ms=event.at_ms,
                )
                for pending in pending_responses.values():
                    self._hard_terminalize_pending(
                        pending,
                        reason="disconnect_receipt_retirement_failed",
                        at_ms=event.at_ms,
                    )
                self.adapter.terminalize_permit_admission()
                self.coordinator.calls.hard_terminalize()
                self._hard_terminalize_session_speech()
                return False
            call_cleanup_succeeded = self._cancel_call(
                event_id=f"disconnect_{event.sequence}",
                at_ms=event.at_ms,
            )
            self.coordinator.calls.hard_terminalize()
            for pending in pending_responses.values():
                success = call_cleanup_succeeded
                for reserved, authorization in zip(
                    pending.reserved,
                    pending.authorizations,
                    strict=True,
                ):
                    if (
                        reserved.act_id
                        in pending.permitted_act_ids
                        and reserved.act_id
                        not in pending.retired_permit_act_ids
                    ):
                        revoked = self.adapter.permit_was_revoked(
                            authorization
                        )
                        success = revoked and success
                        if revoked:
                            pending.retired_permit_act_ids.add(
                                reserved.act_id
                            )
                    if reserved.act_id in pending.observed_act_ids:
                        continue
                    success = (
                        self._cancel_speech(
                            reserved.act_id,
                            reason=CancellationReason.INTERRUPTION,
                        )
                        and success
                    )
                    success = (
                        self._terminalize_authorization(
                            authorization,
                            at_ms=event.at_ms,
                        )
                        and success
                    )
                success = (
                    self.coordinator.retire_batch(
                        pending.reserved
                    )
                    and success
                )
                if not success:
                    self._hard_terminalize_pending(
                        pending,
                        reason="disconnect_compensation_failed",
                        at_ms=event.at_ms,
                    )
                    self._hard_terminalize_session_speech()
                    return False
                for item in pending.reserved:
                    self._pending_by_act.pop(item.act_id, None)
                self._outcomes[pending.receipt_id] = (
                    CompositionResult(
                        status=CompositionStatus.TERMINAL_FAILURE,
                        phase=CompositionPhase.TERMINAL,
                        receipt_id=pending.receipt_id,
                        input_turn_id=pending.input_turn_id,
                        state_version=pending.state_version,
                        act_ids=tuple(
                            item.act_id
                            for item in pending.reserved
                        ),
                        act_kinds=tuple(
                            item.kind
                            for item in pending.reserved
                        ),
                        replay_mode=pending.replay_mode,
                        replay_source_act_id=(
                            pending.replay_source_act_id
                        ),
                        reason="session_disconnected",
                    )
                )
                self._outcome_expiries[pending.receipt_id] = (
                    pending.outcome_expires_at_ms
                )
            speech_cleanup_succeeded = (
                self._hard_terminalize_session_speech()
            )
            return (
                call_cleanup_succeeded
                and speech_cleanup_succeeded
                and not self._pending_by_act
                and self.coordinator.calls.is_quiescent
            )

    def accept_transport_resolution(
        self,
        *,
        event: VoiceEvent,
        event_id: str,
        sequence: int,
    ) -> bool:
        with self._lock:
            pending = self._pending_by_act.get(
                event.semantic_act_id if isinstance(event, VoiceEvent) else ""
            )
            if (
                pending is None
                or event.semantic_act_id
                != self._next_unobserved_act_id(pending)
                or event.semantic_act_id
                not in pending.permitted_act_ids
                or event.semantic_act_id not in pending.confirmed_act_ids
                or event.semantic_act_id not in pending.playout_act_ids
                or event.semantic_act_id in pending.transport_act_ids
                or not self.lifecycle.accepts_transport_resolution(event)
            ):
                return False
            authorization = self._authorization(
                pending,
                event.semantic_act_id,
            )
            if (
                authorization is None
                or not self.coordinator.speech.is_live(
                    event.semantic_act_id
                )
                or not self.adapter.has_permit(authorization)
            ):
                return False
            binding = self.coordinator.speech.playout_binding(
                event.semantic_act_id
            )
            if (
                binding is None
                or binding.binding != self.binding
                or binding.text_digest
                != event.payload.text_digest
                or binding.audio_id != event.payload.audio_id
                or binding.playout_id != event.payload.playout_id
            ):
                self._hard_terminalize_pending(
                    pending,
                    reason="transport_playout_binding_mismatch",
                    at_ms=event.at_ms,
                )
                return False
            if (
                event.semantic_act_kind
                is VoiceSemanticActKind.QUESTION
                and not self.coordinator.calls.transport_resolved(
                    event=event,
                    event_id=event_id,
                    sequence=sequence,
                )
            ):
                return False
            pending.transport_act_ids.add(event.semantic_act_id)
            return True

    def infer_playback(
        self,
        *,
        act_id: str,
        event_id: str,
        sequence: int,
        at_ms: int,
        inference_id: str,
        transport_id: str,
    ) -> CompositionResult | None:
        with self._lock:
            pending = self._pending_by_act.get(act_id)
            if (
                pending is None
                or pending.replay_mode is not None
                or act_id not in pending.confirmed_act_ids
                or act_id not in pending.transport_act_ids
                or act_id in pending.observed_act_ids
            ):
                return None
            reserved = next(
                (item for item in pending.reserved if item.act_id == act_id),
                None,
            )
            if (
                reserved is None
                or reserved.kind is not VoiceSemanticActKind.QUESTION
            ):
                return None
            next_act = next(
                (
                    item.act_id
                    for item in pending.reserved
                    if item.act_id not in pending.observed_act_ids
                ),
                None,
            )
            if act_id != next_act:
                return None
            authorization = self._authorization(pending, act_id)
            if (
                authorization is None
                or self.lifecycle.act_state(authorization)
                is not VoiceEventKind.TRANSPORT_RESOLVED
                or not self.coordinator.speech.is_live(act_id)
                or not self.adapter.has_permit(authorization)
            ):
                return None
            with self.state.delivery_guard():
                if not self.state.revalidate(
                    expected_version=pending.state_version,
                    turn_id=pending.input_turn_id,
                    sequence=pending.turn_sequence,
                ):
                    return None
                intents = self.coordinator.calls.playback(
                    binding=self.binding,
                    event_id=event_id,
                    sequence=sequence,
                    act_id=act_id,
                    evidence=PlaybackEvidence.PLAYBACK_INFERRED,
                    at_ms=at_ms,
                    inference_id=inference_id,
                    transport_id=transport_id,
                )
                if not intents:
                    return None
                asked_version = self.state.mark_slot_asked(
                    expected_version=pending.state_version,
                    turn_id=pending.input_turn_id,
                    sequence=pending.turn_sequence,
                    slot=pending.question_slot or "",
                )
                if asked_version is None:
                    return self._hard_terminalize_pending(
                        pending,
                        reason="inferred_playback_state_commit_failed",
                        at_ms=at_ms,
                    )
                if not self.adapter.retire_permit(authorization):
                    return self._hard_terminalize_pending(
                        pending,
                        reason="inferred_playback_permit_retirement_failed",
                        at_ms=at_ms,
                    )
                pending.retired_permit_act_ids.add(act_id)
                pending.observed_act_ids.add(act_id)
                pending.state_version = asked_version
                if not self._activate_next_permit(pending):
                    return self._hard_terminalize_pending(
                        pending,
                        reason="next_act_permit_failed",
                        at_ms=at_ms,
                    )
                return self._finish_if_observed(
                    pending,
                    at_ms=at_ms,
                )

    def observe_playback(
        self,
        *,
        event: VoiceEvent,
        event_id: str,
        sequence: int,
    ) -> CompositionResult | None:
        with self._lock:
            if not isinstance(event, VoiceEvent):
                return None
            pending = self._pending_by_act.get(event.semantic_act_id)
            if (
                pending is None
                or not self.lifecycle.accepts_caller_playback(event)
                or event.semantic_act_id not in pending.confirmed_act_ids
                or event.semantic_act_id not in pending.playout_act_ids
                or event.semantic_act_id not in pending.transport_act_ids
                or event.semantic_act_id in pending.observed_act_ids
            ):
                return None
            next_act = next(
                (
                    item.act_id
                    for item in pending.reserved
                    if item.act_id not in pending.observed_act_ids
                ),
                None,
            )
            if event.semantic_act_id != next_act:
                return None
            authorization = self._authorization(
                pending,
                event.semantic_act_id,
            )
            if (
                authorization is None
                or not self.coordinator.speech.is_live(
                    event.semantic_act_id
                )
                or not self.adapter.has_permit(authorization)
            ):
                return None
            binding = self.coordinator.speech.playout_binding(
                event.semantic_act_id
            )
            if (
                binding is None
                or binding.binding != self.binding
                or binding.text_digest
                != event.payload.text_digest
                or binding.audio_id != event.payload.audio_id
                or binding.playout_id != event.payload.playout_id
            ):
                return self._hard_terminalize_pending(
                    pending,
                    reason="playback_playout_binding_mismatch",
                    at_ms=event.at_ms,
                )
            if (
                pending.language_choice is not None
                and pending.language_choice.defers_playback(
                    event=event,
                    lifecycle=self.lifecycle,
                )
            ):
                return self._outcomes[pending.receipt_id]
            state_version = pending.state_version
            with self.state.delivery_guard():
                if not self.state.revalidate(
                    expected_version=pending.state_version,
                    turn_id=pending.input_turn_id,
                    sequence=pending.turn_sequence,
                ):
                    return self._hard_terminalize_pending(
                        pending,
                        reason="observed_playback_state_drift",
                        at_ms=event.at_ms,
                    )
                if (
                    event.semantic_act_kind
                    is VoiceSemanticActKind.QUESTION
                    and pending.question_slot is not None
                ):
                    intents = self.coordinator.caller_playback(
                        event=event,
                        event_id=event_id,
                        sequence=sequence,
                    )
                    if not intents:
                        return None
                    if pending.replay_mode is None:
                        asked_version = self.state.mark_slot_asked(
                            expected_version=pending.state_version,
                            turn_id=pending.input_turn_id,
                            sequence=pending.turn_sequence,
                            slot=pending.question_slot,
                        )
                        if asked_version is None:
                            return self._hard_terminalize_pending(
                                pending,
                                reason=(
                                    "observed_playback_state_commit_failed"
                                ),
                                at_ms=event.at_ms,
                            )
                        state_version = asked_version
                if not self.adapter.retire_permit(authorization):
                    return self._hard_terminalize_pending(
                        pending,
                        reason=(
                            "observed_playback_permit_retirement_failed"
                        ),
                        at_ms=event.at_ms,
                    )
                pending.retired_permit_act_ids.add(
                    event.semantic_act_id
                )
                if not (
                    self.coordinator.speech
                    .record_caller_playback_observed(
                        event.semantic_act_id,
                        playout_id=event.payload.playout_id or "",
                    )
                ):
                    return self._hard_terminalize_pending(
                        pending,
                        reason=(
                            "caller_playback_evidence_commit_failed"
                        ),
                        at_ms=event.at_ms,
                    )
                if (
                    pending.language_choice is not None
                    and not pending.language_choice.observe_segment(
                        event=event,
                        lifecycle=self.lifecycle,
                    )
                ):
                    failure = self._hard_terminalize_pending(
                        pending,
                        reason=(
                            "language_choice_observation_failed"
                        ),
                        at_ms=event.at_ms,
                    )
                    pending.language_choice.tombstone_pending_pair()
                    return failure
                pending.observed_act_ids.add(event.semantic_act_id)
                pending.state_version = state_version
                if not self._activate_next_permit(pending):
                    return self._hard_terminalize_pending(
                        pending,
                        reason="next_act_permit_failed",
                        at_ms=event.at_ms,
                    )
                return self._finish_if_observed(
                    pending,
                    at_ms=event.at_ms,
                )

    def _handle_extraction_failure(
        self,
        *,
        receipt: FinalTurnAdmissionReceipt,
        outcome: ExtractionOutcome,
    ) -> CompositionResult:
        if outcome not in _RECOVERABLE_EXTRACTION or not self.state.is_current(
            turn_id=receipt.input_turn_id,
            sequence=receipt.sequence,
        ):
            return self._record(
                receipt,
                CompositionStatus.SILENT,
                CompositionPhase.EXTRACTION_TERMINAL,
                self.state.version,
                reason=outcome.value,
            )
        if not self.state.consume_repair(turn_id=receipt.input_turn_id):
            return self._record(
                receipt,
                CompositionStatus.CLOSURE_REQUIRED,
                CompositionPhase.EXTRACTION_TERMINAL,
                self.state.version,
                closure_trigger=ClosureTrigger.REPAIR_EXHAUSTED,
                reason="call_repair_exhausted",
            )
        state_version = self.state.version
        try:
            proposal = self.materializer.input_repair(
                state_version=state_version,
                locale=self.state.current_state().language,
            )
        except (TypeError, ValueError):
            return self._record(
                receipt,
                CompositionStatus.TERMINAL_FAILURE,
                CompositionPhase.EXTRACTION_TERMINAL,
                state_version,
                reason="repair_proposal",
            )
        return self._prepare_response(
            receipt=receipt,
            action=None,
            proposal=proposal,
            state_version=state_version,
            pending_status=CompositionStatus.REPAIR_PENDING,
        )

    def prepare_language_choice(
        self,
        *,
        receipt: FinalTurnAdmissionReceipt,
        trigger: CompositionResult,
        language_choice: OfflineLanguageChoiceLifecycle,
        proposal: LanguageChoiceProposal,
    ) -> CompositionResult:
        """Prepare only the exact one-shot choice after an unlisted locale."""
        with self._lock:
            if (
                type(receipt) is not FinalTurnAdmissionReceipt
                or type(trigger) is not CompositionResult
                or self._outcomes.get(receipt.receipt_id)
                is not trigger
                or trigger.status
                is not CompositionStatus.LANGUAGE_CHOICE_REQUIRED
                or trigger.phase
                is not CompositionPhase.FACTS_COMMITTED
                or trigger.reason != "unlisted_language"
                or trigger.receipt_id != receipt.receipt_id
                or trigger.input_turn_id != receipt.input_turn_id
                or type(language_choice)
                is not OfflineLanguageChoiceLifecycle
                or language_choice.binding != self.binding
                or language_choice.phase
                is not LanguageChoicePhase.AVAILABLE
                or type(proposal) is not LanguageChoiceProposal
                or proposal.state_version != trigger.state_version
            ):
                return _untracked_rejection(
                    receipt,
                    self.state.version,
                )
            return self._prepare_response(
                receipt=receipt,
                action=None,
                proposal=proposal,
                state_version=trigger.state_version,
                pending_status=(
                    CompositionStatus.LANGUAGE_CHOICE_PENDING
                ),
                language_choice=language_choice,
            )

    def _prepare_response(
        self,
        *,
        receipt: (
            FinalTurnAdmissionReceipt
            | LanguageRecoveryFinalTurnReceipt
        ),
        action: NextAction | None,
        proposal: ContentProposal | LanguageChoiceProposal,
        state_version: int,
        pending_status: CompositionStatus,
        language_choice: (
            OfflineLanguageChoiceLifecycle | None
        ) = None,
    ) -> CompositionResult:
        try:
            reserve_sequence, reserve_at_ms = self.lifecycle.next_position(
                at_ms=receipt.at_ms
            )
            authorization = (
                self.policy.authorize_language_choice(
                    proposal=proposal,
                    binding=self.binding,
                    turn_id=receipt.input_turn_id,
                    state_version=state_version,
                )
                if type(proposal) is LanguageChoiceProposal
                and type(language_choice)
                is OfflineLanguageChoiceLifecycle
                else self.policy.authorize(
                    action=action,
                    proposal=proposal,
                    binding=self.binding,
                    turn_id=receipt.input_turn_id,
                    state_version=state_version,
                )
            )
            reserved = self.coordinator.reserve_batch(
                plan=proposal.plan,
                authorization=authorization,
                event_id=f"reserve_{receipt.sequence}",
                sequence=reserve_sequence,
                at_ms=reserve_at_ms,
                turn_sequence=receipt.sequence,
            )
        except (TypeError, ValueError):
            reserved = ()
        if len(reserved) != len(proposal.plan.acts):
            return self._record(
                receipt,
                CompositionStatus.TERMINAL_FAILURE,
                CompositionPhase.FACTS_COMMITTED,
                state_version,
                reason="reservation",
            )
        if (
            language_choice is not None
            and (
                type(proposal) is not LanguageChoiceProposal
                or not language_choice.reserve(
                    proposal=proposal,
                    reserved=reserved,
                )
            )
        ):
            if not self.coordinator.rollback_batch(reserved):
                return self._terminalize_partial(
                    receipt=receipt,
                    reserved=reserved,
                    canonical=(),
                    accepted_permits=(),
                    state_version=state_version,
                    reason=(
                        "language_choice_reservation_cleanup"
                    ),
                    language_choice=language_choice,
                )
            language_choice.tombstone_pending_pair()
            return self._record(
                receipt,
                CompositionStatus.TERMINAL_FAILURE,
                CompositionPhase.FACTS_COMMITTED,
                state_version,
                reason="language_choice_reservation",
            )
        with self.state.delivery_guard():
            if not self.state.revalidate(
                expected_version=state_version,
                turn_id=receipt.input_turn_id,
                sequence=receipt.sequence,
            ):
                if not self.coordinator.rollback_batch(reserved):
                    return self._terminalize_partial(
                        receipt=receipt,
                        reserved=reserved,
                        canonical=(),
                        accepted_permits=(),
                        state_version=state_version,
                        reason="response_cas_rollback_failed",
                        language_choice=language_choice,
                    )
                if language_choice is not None:
                    language_choice.tombstone_pending_pair()
                return self._record(
                    receipt,
                    CompositionStatus.SUPERSEDED,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="response_cas",
                )
            if not all(
                self.coordinator.speech.authorize_text(
                    item.act_id,
                    item.text,
                )
                for item in reserved
            ):
                return self._compensate(
                    receipt=receipt,
                    reserved=reserved,
                    canonical=(),
                    accepted_permits=(),
                    state_version=state_version,
                    reason="speech_authorization",
                    language_choice=language_choice,
                )
            canonical: list[VoiceEvent] = []
            accepted_permits: list[VoiceEvent] = []
            generation_id = f"generation_{proposal.proposal_digest}"
            for item in reserved:
                sequence, at_ms = self.lifecycle.next_position(
                    at_ms=receipt.at_ms
                )
                event = VoiceEvent(
                    schema_version=VOICE_SCHEMA_VERSION,
                    kind=VoiceEventKind.RESPONSE_AUTHORIZED,
                    source=VoiceSource.LOCAL_AUTHORITATIVE,
                    sensitivity=VoiceSensitivity.OPERATIONAL,
                    binding=self.binding,
                    sequence=sequence,
                    at_ms=at_ms,
                    input_turn_id=receipt.input_turn_id,
                    generation_id=generation_id,
                    semantic_act_id=item.act_id,
                    semantic_act_kind=item.kind,
                    payload=VoicePayload(),
                )
                if not self.lifecycle.ingest(event):
                    return self._compensate(
                        receipt=receipt,
                        reserved=reserved,
                        canonical=tuple(canonical),
                        accepted_permits=tuple(accepted_permits),
                        state_version=state_version,
                        reason="canonical_authorization",
                        language_choice=language_choice,
                    )
                canonical.append(event)
                if not accepted_permits:
                    if not self.adapter.accept_permit(
                        event,
                        lifecycle=self.lifecycle,
                    ):
                        return self._compensate(
                            receipt=receipt,
                            reserved=reserved,
                            canonical=tuple(canonical),
                            accepted_permits=tuple(accepted_permits),
                            state_version=state_version,
                            reason="adapter_permit",
                            language_choice=language_choice,
                        )
                    accepted_permits.append(event)
            if (
                language_choice is not None
                and not language_choice.begin_presentation(
                    lifecycle=self.lifecycle,
                )
            ):
                return self._compensate(
                    receipt=receipt,
                    reserved=reserved,
                    canonical=tuple(canonical),
                    accepted_permits=tuple(accepted_permits),
                    state_version=state_version,
                    reason="language_choice_presentation",
                    language_choice=language_choice,
                )
            question_slot = next(
                (
                    act.question_slot
                    for act in proposal.plan.acts
                    if act.kind is VoiceSemanticActKind.QUESTION
                ),
                None,
            )
            pending = _PendingResponse(
                receipt_id=receipt.receipt_id,
                outcome_expires_at_ms=(
                    receipt.expires_at_ms + self.receipts.max_ttl_ms
                ),
                input_turn_id=receipt.input_turn_id,
                turn_sequence=receipt.sequence,
                state_version=state_version,
                reserved=reserved,
                authorizations=tuple(canonical),
                question_slot=question_slot,
                permitted_act_ids={
                    event.semantic_act_id
                    for event in accepted_permits
                },
                confirmed_act_ids=set(),
                tts_act_ids=set(),
                playout_act_ids=set(),
                transport_act_ids=set(),
                observed_act_ids=set(),
                retired_permit_act_ids=set(),
                language_choice=language_choice,
            )
            for item in reserved:
                self._pending_by_act[item.act_id] = pending
            return self._record(
                receipt,
                pending_status,
                CompositionPhase.RESPONSE_PENDING_PLAYBACK,
                state_version,
                act_ids=tuple(item.act_id for item in reserved),
                act_kinds=tuple(item.kind for item in reserved),
            )

    @staticmethod
    def _next_unobserved_act_id(
        pending: _PendingResponse,
    ) -> str | None:
        return next(
            (
                item.act_id
                for item in pending.reserved
                if item.act_id not in pending.observed_act_ids
            ),
            None,
        )

    def _activate_next_permit(
        self,
        pending: _PendingResponse,
    ) -> bool:
        act_id = self._next_unobserved_act_id(pending)
        if act_id is None:
            return True
        authorization = self._authorization(pending, act_id)
        if authorization is None:
            return False
        if act_id in pending.permitted_act_ids:
            return self.adapter.has_permit(authorization)
        if not self.coordinator.speech.is_live(act_id):
            return False
        if not self.adapter.accept_permit(
            authorization,
            lifecycle=self.lifecycle,
        ):
            return False
        pending.permitted_act_ids.add(act_id)
        return True

    def _finish_if_observed(
        self,
        pending: _PendingResponse,
        *,
        at_ms: int,
    ) -> CompositionResult:
        if len(pending.observed_act_ids) != len(pending.authorizations):
            return self._outcomes[pending.receipt_id]
        if (
            len(pending.retired_permit_act_ids)
            != len(pending.authorizations)
            or not self.coordinator.complete_batch(pending.reserved)
        ):
            return self._hard_terminalize_pending(
                pending,
                reason="delivered_response_cleanup_failed",
                at_ms=at_ms,
            )
        if (
            pending.language_choice is not None
            and not pending.language_choice.complete_prompt_cleanup()
        ):
            failure = self._hard_terminalize_pending(
                pending,
                reason="language_choice_cleanup_failed",
                at_ms=at_ms,
            )
            pending.language_choice.tombstone_pending_pair()
            return failure
        for item in pending.reserved:
            self._pending_by_act.pop(item.act_id, None)
        result = CompositionResult(
            status=(
                CompositionStatus.REPLAY_OBSERVED
                if pending.replay_mode is not None
                else CompositionStatus.LANGUAGE_CHOICE_WINDOW
                if pending.language_choice is not None
                else CompositionStatus.RESPONSE_OBSERVED
            ),
            phase=CompositionPhase.RESPONSE_COMMITTED,
            receipt_id=pending.receipt_id,
            input_turn_id=pending.input_turn_id,
            state_version=pending.state_version,
            act_ids=tuple(item.act_id for item in pending.reserved),
            act_kinds=tuple(item.kind for item in pending.reserved),
            replay_mode=pending.replay_mode,
            replay_source_act_id=pending.replay_source_act_id,
        )
        self._outcomes[pending.receipt_id] = result
        self._outcome_expiries[pending.receipt_id] = (
            pending.outcome_expires_at_ms
        )
        return result

    def _supersede_pending(
        self,
        receipt: FinalTurnAdmissionReceipt,
    ) -> bool:
        pending_responses = {
            id(pending): pending for pending in self._pending_by_act.values()
        }
        for index, pending in enumerate(pending_responses.values()):
            success = self._cancel_call(
                event_id=f"supersede_{receipt.sequence}_{index}",
                at_ms=receipt.at_ms,
            )
            for reserved, authorization in zip(
                pending.reserved,
                pending.authorizations,
                strict=True,
            ):
                if (
                    reserved.act_id
                    in pending.permitted_act_ids
                    and reserved.act_id
                    not in pending.retired_permit_act_ids
                ):
                    retired = self.adapter.retire_permit(
                        authorization
                    )
                    already_revoked = (
                        not retired
                        and self.adapter.permit_was_revoked(
                            authorization
                        )
                    )
                    success = (retired or already_revoked) and success
                    if retired or already_revoked:
                        pending.retired_permit_act_ids.add(
                            reserved.act_id
                        )
                if reserved.act_id in pending.observed_act_ids:
                    continue
                success = (
                    self._cancel_speech(
                        reserved.act_id,
                        reason=CancellationReason.CALLER_ACTIVITY,
                    )
                    and success
                )
                success = (
                    self._terminalize_authorization(
                        authorization,
                        at_ms=receipt.at_ms,
                    )
                    and success
                )
            success = self.coordinator.retire_batch(pending.reserved) and success
            if not success:
                self._hard_terminalize_pending(
                    pending,
                    reason="supersede_compensation_failed",
                    at_ms=receipt.at_ms,
                )
                return False
            for item in pending.reserved:
                self._pending_by_act.pop(item.act_id, None)
            self._outcomes[pending.receipt_id] = CompositionResult(
                status=CompositionStatus.SUPERSEDED,
                phase=CompositionPhase.TERMINAL,
                receipt_id=pending.receipt_id,
                input_turn_id=pending.input_turn_id,
                state_version=pending.state_version,
                act_ids=tuple(item.act_id for item in pending.reserved),
                act_kinds=tuple(item.kind for item in pending.reserved),
                replay_mode=pending.replay_mode,
                replay_source_act_id=pending.replay_source_act_id,
                reason="newer_turn",
            )
            self._outcome_expiries[pending.receipt_id] = (
                pending.outcome_expires_at_ms
            )
        return True

    def _compensate(
        self,
        *,
        receipt: (
            FinalTurnAdmissionReceipt
            | LanguageRecoveryFinalTurnReceipt
        ),
        reserved: tuple[ReservedSpeech, ...],
        canonical: tuple[VoiceEvent, ...],
        accepted_permits: tuple[VoiceEvent, ...],
        state_version: int,
        reason: str,
        replay_mode: ReplayMode | None = None,
        replay_source_act_id: str | None = None,
        language_choice: (
            OfflineLanguageChoiceLifecycle | None
        ) = None,
    ) -> CompositionResult:
        success = self.coordinator.rollback_pristine_question(reserved)
        retired_ids: set[str] = set()
        for event in accepted_permits:
            retired = self.adapter.retire_permit(event)
            success = retired and success
            if retired:
                retired_ids.add(event.semantic_act_id)
        canonical_by_id = {
            event.semantic_act_id: event for event in canonical
        }
        for item in reserved:
            success = (
                self._cancel_speech(
                    item.act_id,
                    reason=CancellationReason.INTERRUPTION,
                )
                and success
            )
            prior = canonical_by_id.get(item.act_id)
            if prior is None:
                continue
            success = (
                self._terminalize_authorization(
                    prior,
                    at_ms=prior.at_ms,
                )
                and success
            )
        success = self.coordinator.retire_batch(reserved) and success
        if not success:
            pending = _PendingResponse(
                receipt_id=receipt.receipt_id,
                outcome_expires_at_ms=(
                    receipt.expires_at_ms
                    + self.receipts.max_ttl_ms
                ),
                input_turn_id=receipt.input_turn_id,
                turn_sequence=receipt.sequence,
                state_version=state_version,
                reserved=reserved,
                authorizations=canonical,
                question_slot=None,
                permitted_act_ids={
                    event.semantic_act_id
                    for event in accepted_permits
                },
                confirmed_act_ids=set(),
                tts_act_ids=set(),
                playout_act_ids=set(),
                transport_act_ids=set(),
                observed_act_ids=set(),
                retired_permit_act_ids=retired_ids,
                replay_mode=replay_mode,
                replay_source_act_id=replay_source_act_id,
                language_choice=language_choice,
            )
            return self._hard_terminalize_pending(
                pending,
                reason=f"compensation_failed_{reason}",
                at_ms=receipt.at_ms,
            )
        if language_choice is not None:
            language_choice.tombstone_pending_pair()
        return self._record(
            receipt,
            CompositionStatus.TERMINAL_FAILURE,
            CompositionPhase.TERMINAL,
            state_version,
            replay_mode=replay_mode,
            replay_source_act_id=replay_source_act_id,
            reason=reason,
        )

    def _terminalize_partial(
        self,
        *,
        receipt: (
            FinalTurnAdmissionReceipt
            | LanguageRecoveryFinalTurnReceipt
        ),
        reserved: tuple[ReservedSpeech, ...],
        canonical: tuple[VoiceEvent, ...],
        accepted_permits: tuple[VoiceEvent, ...],
        state_version: int,
        reason: str,
        replay_mode: ReplayMode | None = None,
        replay_source_act_id: str | None = None,
        language_choice: (
            OfflineLanguageChoiceLifecycle | None
        ) = None,
    ) -> CompositionResult:
        pending = _PendingResponse(
            receipt_id=receipt.receipt_id,
            outcome_expires_at_ms=(
                receipt.expires_at_ms + self.receipts.max_ttl_ms
            ),
            input_turn_id=receipt.input_turn_id,
            turn_sequence=receipt.sequence,
            state_version=state_version,
            reserved=reserved,
            authorizations=canonical,
            question_slot=None,
            permitted_act_ids={
                event.semantic_act_id
                for event in accepted_permits
            },
            confirmed_act_ids=set(),
            tts_act_ids=set(),
            playout_act_ids=set(),
            transport_act_ids=set(),
            observed_act_ids=set(),
            retired_permit_act_ids=set(),
            replay_mode=replay_mode,
            replay_source_act_id=replay_source_act_id,
            language_choice=language_choice,
        )
        for event in accepted_permits:
            if self.adapter.retire_permit(event):
                pending.retired_permit_act_ids.add(
                    event.semantic_act_id
                )
        return self._hard_terminalize_pending(
            pending,
            reason=reason,
            at_ms=receipt.at_ms,
        )

    def _hard_terminalize_pending(
        self,
        pending: _PendingResponse,
        *,
        reason: str,
        at_ms: int,
    ) -> CompositionResult:
        """Close all adapter authority before releasing ambiguous tracking."""
        if pending.receipt_id in self._terminalizing_receipt_ids:
            return self._outcomes[pending.receipt_id]
        self._terminalizing_receipt_ids.add(pending.receipt_id)
        try:
            self.adapter.terminalize_permit_admission()
            self._cancel_call(
                event_id=f"terminalize_{pending.turn_sequence}",
                at_ms=at_ms,
            )
            self.coordinator.calls.hard_terminalize()
            for reserved in pending.reserved:
                cancelled = self._cancel_speech(
                    reserved.act_id,
                    reason=CancellationReason.INTERRUPTION,
                )
                if not cancelled:
                    self.coordinator.speech.hard_terminalize(
                        reserved.act_id
                    )
            for authorization in pending.authorizations:
                self._terminalize_authorization(
                    authorization,
                    at_ms=at_ms,
                )
            try:
                batch_retired = self.coordinator.retire_batch(
                    pending.reserved
                )
            except Exception:  # noqa: BLE001
                batch_retired = False
            try:
                batch_still_tracked = (
                    self.coordinator.speech
                    .tracks_reservation_batch(
                        pending.reserved
                    )
                )
            except Exception:  # noqa: BLE001
                batch_still_tracked = True
            if batch_retired or not batch_still_tracked:
                for item in pending.reserved:
                    self._pending_by_act.pop(
                        item.act_id,
                        None,
                    )
            if pending.language_choice is not None:
                pending.language_choice.tombstone_pending_pair()
            result = CompositionResult(
                status=CompositionStatus.TERMINAL_FAILURE,
                phase=CompositionPhase.TERMINAL,
                receipt_id=pending.receipt_id,
                input_turn_id=pending.input_turn_id,
                state_version=pending.state_version,
                act_ids=tuple(
                    item.act_id for item in pending.reserved
                ),
                act_kinds=tuple(
                    item.kind for item in pending.reserved
                ),
                replay_mode=pending.replay_mode,
                replay_source_act_id=(
                    pending.replay_source_act_id
                ),
                reason=reason,
            )
            self._outcomes[pending.receipt_id] = result
            self._outcome_expiries[pending.receipt_id] = (
                pending.outcome_expires_at_ms
            )
            return result
        finally:
            self._terminalizing_receipt_ids.discard(
                pending.receipt_id
            )

    def _hard_terminalize_session_speech(self) -> bool:
        """Retire every transaction-owned act even if binding cleanup faults."""
        act_ids = set(
            self.coordinator.speech.act_ids_for_binding(
                self.binding
            )
        )
        binding_cleanup_succeeded = (
            self.coordinator.speech.hard_terminalize_binding(
                self.binding
            )
        )
        act_ids |= {
            act_id
            for result in self._outcomes.values()
            for act_id in result.act_ids
        } | {
            item.act_id
            for pending in self._pending_by_act.values()
            for item in pending.reserved
        }
        act_cleanup_succeeded = True
        for act_id in act_ids:
            if self.coordinator.speech.is_live(act_id):
                act_cleanup_succeeded = (
                    self.coordinator.speech.hard_terminalize(
                        act_id
                    )
                    and act_cleanup_succeeded
                )
        return (
            binding_cleanup_succeeded
            and act_cleanup_succeeded
            and all(
                not self.coordinator.speech.is_live(act_id)
                for act_id in act_ids
            )
        )

    def _seal_replay_authority(self) -> bool:
        """Fail closed when replay admission cannot prove clean authority."""
        try:
            self.adapter.terminalize_permit_admission()
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.coordinator.calls.hard_terminalize()
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            speech_closed = (
                self._hard_terminalize_session_speech()
            )
        except Exception:  # noqa: BLE001
            speech_closed = False
        return (
            speech_closed
            and self.adapter.terminally_closed
            and self.coordinator.calls.is_quiescent
            and self.coordinator.calls.phase
            is SilencePhase.TERMINATED
        )

    def _cancel_call(self, *, event_id: str, at_ms: int) -> bool:
        if self.coordinator.calls.is_quiescent:
            return True
        sequence, canonical_at_ms = (
            self.coordinator.calls.next_position(at_ms=at_ms)
        )
        if not self.coordinator.calls.can_accept_position(
            binding=self.binding,
            event_id=event_id,
            sequence=sequence,
            at_ms=canonical_at_ms,
        ):
            return False
        self.coordinator.calls.cancel(
            binding=self.binding,
            event_id=event_id,
            sequence=sequence,
            at_ms=canonical_at_ms,
        )
        return self.coordinator.calls.is_quiescent

    def _cancel_speech(
        self,
        act_id: str,
        *,
        reason: CancellationReason,
    ) -> bool:
        if self.coordinator.speech.is_cancelled(act_id):
            return True
        return self.coordinator.speech.cancel(
            act_id,
            reason=reason,
        )

    def _terminalize_authorization(
        self,
        authorization: VoiceEvent,
        *,
        at_ms: int,
    ) -> bool:
        state = self.lifecycle.act_state(authorization)
        if state in {
            VoiceEventKind.ACT_FAILED,
            VoiceEventKind.ACT_TIMED_OUT,
            VoiceEventKind.CALLER_PLAYBACK_OBSERVED,
            VoiceEventKind.PLAYOUT_PARTIAL,
            VoiceEventKind.PLAYOUT_CLEARED,
            VoiceEventKind.PLAYOUT_INTERRUPTED,
        }:
            return True
        payload = self.lifecycle.terminal_payload(authorization)
        if payload is None:
            return False
        sequence, canonical_at_ms = self.lifecycle.next_position(
            at_ms=at_ms
        )
        return self.lifecycle.ingest(
            VoiceEvent(
                schema_version=VOICE_SCHEMA_VERSION,
                kind=VoiceEventKind.ACT_FAILED,
                source=VoiceSource.LOCAL_AUTHORITATIVE,
                sensitivity=VoiceSensitivity.OPERATIONAL,
                binding=self.binding,
                sequence=sequence,
                at_ms=canonical_at_ms,
                input_turn_id=authorization.input_turn_id,
                generation_id=authorization.generation_id,
                semantic_act_id=authorization.semantic_act_id,
                semantic_act_kind=authorization.semantic_act_kind,
                payload=payload,
            )
        )

    @staticmethod
    def _authorization(
        pending: _PendingResponse,
        act_id: str,
    ) -> VoiceEvent | None:
        return next(
            (
                event
                for event in pending.authorizations
                if event.semantic_act_id == act_id
            ),
            None,
        )

    def _matches_lifecycle_binding(
        self,
        *,
        event: VoiceEvent,
        authorization: VoiceEvent,
        kind: VoiceEventKind,
    ) -> bool:
        """Bind a supplied receipt to the lifecycle's exact current act."""
        return (
            isinstance(event, VoiceEvent)
            and isinstance(authorization, VoiceEvent)
            and event.kind is kind
            and event.source is VoiceSource.LOCAL_AUTHORITATIVE
            and event.sensitivity
            is VoiceSensitivity.OPERATIONAL
            and event.binding == self.binding
            and event.input_turn_id
            == authorization.input_turn_id
            and event.generation_id
            == authorization.generation_id
            and event.semantic_act_id
            == authorization.semantic_act_id
            and event.semantic_act_kind
            is authorization.semantic_act_kind
            and self.lifecycle.act_state(authorization) is kind
            and self.lifecycle.terminal_payload(
                authorization
            )
            == event.payload
        )

    @property
    def retained_outcome_count(self) -> int:
        with self._lock:
            return len(self._outcomes)

    @property
    def pending_response_count(self) -> int:
        with self._lock:
            return len({
                id(pending)
                for pending in self._pending_by_act.values()
            })

    def _prune_outcomes(self, *, now_ms: int) -> None:
        pending_receipts = {
            pending.receipt_id
            for pending in self._pending_by_act.values()
        }
        for receipt_id in tuple(self._outcomes):
            if (
                receipt_id not in pending_receipts
                and self._outcome_expiries.get(
                    receipt_id,
                    now_ms,
                )
                < now_ms
            ):
                self._outcomes.pop(receipt_id, None)
                self._outcome_expiries.pop(receipt_id, None)

    def _record(
        self,
        receipt: FinalTurnAdmissionReceipt,
        status: CompositionStatus,
        phase: CompositionPhase,
        state_version: int,
        *,
        act_ids: tuple[str, ...] = (),
        act_kinds: tuple[VoiceSemanticActKind, ...] = (),
        replay_mode: ReplayMode | None = None,
        replay_source_act_id: str | None = None,
        closure_trigger: ClosureTrigger | None = None,
        reason: str | None = None,
    ) -> CompositionResult:
        result = CompositionResult(
            status=status,
            phase=phase,
            receipt_id=receipt.receipt_id,
            input_turn_id=receipt.input_turn_id,
            state_version=state_version,
            act_ids=act_ids,
            act_kinds=act_kinds,
            replay_mode=replay_mode,
            replay_source_act_id=replay_source_act_id,
            closure_trigger=closure_trigger,
            reason=reason,
        )
        if (
            receipt.receipt_id not in self._outcomes
            and len(self._outcomes) >= self.max_outcomes
        ):
            return _untracked_capacity(
                receipt,
                state_version,
            )
        self._outcomes[receipt.receipt_id] = result
        self._outcome_expiries[receipt.receipt_id] = (
            receipt.expires_at_ms + self.receipts.max_ttl_ms
        )
        return result


def final_turn_content_digest(content: str) -> str:
    if not isinstance(content, str) or not content:
        raise ValueError("final-turn content is invalid")
    return hashlib.sha256(_CONTENT_DOMAIN + content.encode("utf-8")).hexdigest()


def _event_digest(event: VoiceEvent) -> str:
    material = {
        "schema_version": event.schema_version,
        "kind": event.kind.value,
        "source": event.source.value,
        "sensitivity": event.sensitivity.value,
        "binding": {
            "environment": event.binding.environment,
            "contractor_binding": event.binding.contractor_binding,
            "call_binding": event.binding.call_binding,
            "stream_binding": event.binding.stream_binding,
            "epoch": event.binding.epoch,
        },
        "sequence": event.sequence,
        "at_ms": event.at_ms,
        "input_turn_id": event.input_turn_id,
        "generation_id": event.generation_id,
        "semantic_act_id": event.semantic_act_id,
        "semantic_act_kind": event.semantic_act_kind.value,
        "payload": {
            "ordinal": event.payload.ordinal,
            "duration_ms": event.payload.duration_ms,
            "text_digest": event.payload.text_digest,
            "audio_id": event.payload.audio_id,
            "playout_id": event.payload.playout_id,
        },
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _allowed_adapter(adapter: object) -> bool:
    expected = _ALLOWED_ADAPTERS.get(type(adapter))
    return (
        expected is not None
        and isinstance(adapter, OfflineCandidateAdapter)
        and expected is adapter.arm
    )


def _copy_state(state: IntakeState) -> IntakeState:
    return IntakeState.from_dict(state.to_dict())


def _untracked_rejection(
    receipt: object,
    state_version: int,
) -> CompositionResult:
    receipt_id = (
        receipt.receipt_id if isinstance(receipt, FinalTurnAdmissionReceipt) else "receipt_invalid"
    )
    turn_id = (
        receipt.input_turn_id if isinstance(receipt, FinalTurnAdmissionReceipt) else "turn_invalid"
    )
    return CompositionResult(
        status=CompositionStatus.REJECTED,
        phase=CompositionPhase.TERMINAL,
        receipt_id=receipt_id,
        input_turn_id=turn_id,
        state_version=state_version,
        reason="receipt",
    )


def _untracked_capacity(
    receipt: FinalTurnAdmissionReceipt,
    state_version: int,
) -> CompositionResult:
    return CompositionResult(
        status=CompositionStatus.REJECTED,
        phase=CompositionPhase.TERMINAL,
        receipt_id=receipt.receipt_id,
        input_turn_id=receipt.input_turn_id,
        state_version=state_version,
        reason="outcome_capacity",
    )


def _identifier(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and value.replace("_", "").isalnum()


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "AdapterImplementationBinding",
    "CompositionPhase",
    "CompositionPolicy",
    "CompositionResult",
    "CompositionStatus",
    "FinalTurnAdmissionAuthority",
    "FinalTurnAdmissionReceipt",
    "IntakeSnapshot",
    "TurnCompositionTransaction",
    "VersionedIntakeStore",
    "final_turn_content_digest",
]
