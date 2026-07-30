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
from app.services.voice_bakeoff_coordinator import VoiceBakeoffCoordinator
from app.services.voice_bakeoff_materializer import (
    ContentProposal,
    FixedProposalMaterializer,
    ProposalKind,
)
from app.services.voice_call_lifecycle import PlaybackEvidence
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
    RESPONSE_OBSERVED = "response_observed"
    SILENT = "silent"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    TERMINAL_FAILURE = "terminal_failure"
    CLOSURE_REQUIRED = "closure_required"


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
            if len(self._live) + len(self._tombstones) >= self.max_records:
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
            not isinstance(receipt, FinalTurnAdmissionReceipt)
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
            return not self._live

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
            return not self._live

    @property
    def unconsumed_receipt_count(self) -> int:
        with self._lock:
            return len(self._live)

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
        ):
            raise ValueError("proposal authorization input is invalid")
        acts = proposal.plan.acts
        questions = tuple(act for act in acts if act.kind is VoiceSemanticActKind.QUESTION)
        terminal = {
            VoiceSemanticActKind.CLOSING,
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


@dataclass(frozen=True, slots=True)
class CompositionResult:
    status: CompositionStatus
    phase: CompositionPhase
    receipt_id: str
    input_turn_id: str
    state_version: int
    act_ids: tuple[str, ...] = ()
    act_kinds: tuple[VoiceSemanticActKind, ...] = ()
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
    transport_act_ids: set[str]
    observed_act_ids: set[str]
    retired_permit_act_ids: set[str]


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
            if isinstance(receipt, FinalTurnAdmissionReceipt):
                replay = self._outcomes.get(receipt.receipt_id)
                if replay is not None:
                    return replay
            if not isinstance(receipt, FinalTurnAdmissionReceipt) or not self.receipts.consume(
                receipt,
                adapter=self.adapter,
                content=content,
                now_ms=now_ms,
            ):
                return _untracked_rejection(receipt, self.state.version)
            if not self.state.admit_turn(
                turn_id=receipt.input_turn_id,
                sequence=receipt.sequence,
            ):
                return self._record(
                    receipt,
                    CompositionStatus.SUPERSEDED,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="stale_turn",
                )
            if not self._supersede_pending(receipt):
                return self._record(
                    receipt,
                    CompositionStatus.TERMINAL_FAILURE,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="supersede_compensation_failed",
                )
            if len(self._outcomes) >= self.max_outcomes:
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
                return self._handle_extraction_failure(
                    receipt=receipt,
                    outcome=extraction.outcome,
                )
            assert extraction.observation is not None
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
                return self._record(
                    receipt,
                    CompositionStatus.SUPERSEDED,
                    CompositionPhase.TERMINAL,
                    self.state.version,
                    reason="facts_cas",
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
            except Exception:
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
            if event.semantic_act_kind is VoiceSemanticActKind.QUESTION:
                if not self.coordinator.calls.transport_resolved(
                    event=event,
                    event_id=event_id,
                    sequence=sequence,
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
            state_version = pending.state_version
            with self.state.delivery_guard():
                if not self.state.revalidate(
                    expected_version=pending.state_version,
                    turn_id=pending.input_turn_id,
                    sequence=pending.turn_sequence,
                ):
                    return None
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

    def _prepare_response(
        self,
        *,
        receipt: FinalTurnAdmissionReceipt,
        action: NextAction | None,
        proposal: ContentProposal,
        state_version: int,
        pending_status: CompositionStatus,
    ) -> CompositionResult:
        try:
            reserve_sequence, reserve_at_ms = self.lifecycle.next_position(
                at_ms=receipt.at_ms
            )
            authorization = self.policy.authorize(
                action=action,
                proposal=proposal,
                binding=self.binding,
                turn_id=receipt.input_turn_id,
                state_version=state_version,
            )
            reserved = self.coordinator.reserve_batch(
                plan=proposal.plan,
                authorization=authorization,
                event_id=f"reserve_{receipt.sequence}",
                sequence=reserve_sequence,
                at_ms=reserve_at_ms,
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
                    )
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
                        )
                    accepted_permits.append(event)
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
                transport_act_ids=set(),
                observed_act_ids=set(),
                retired_permit_act_ids=set(),
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
        for item in pending.reserved:
            self._pending_by_act.pop(item.act_id, None)
        result = CompositionResult(
            status=CompositionStatus.RESPONSE_OBSERVED,
            phase=CompositionPhase.RESPONSE_COMMITTED,
            receipt_id=pending.receipt_id,
            input_turn_id=pending.input_turn_id,
            state_version=pending.state_version,
            act_ids=tuple(item.act_id for item in pending.reserved),
            act_kinds=tuple(item.kind for item in pending.reserved),
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
                reason="newer_turn",
            )
            self._outcome_expiries[pending.receipt_id] = (
                pending.outcome_expires_at_ms
            )
        return True

    def _compensate(
        self,
        *,
        receipt: FinalTurnAdmissionReceipt,
        reserved: tuple[ReservedSpeech, ...],
        canonical: tuple[VoiceEvent, ...],
        accepted_permits: tuple[VoiceEvent, ...],
        state_version: int,
        reason: str,
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
                transport_act_ids=set(),
                observed_act_ids=set(),
                retired_permit_act_ids=retired_ids,
            )
            return self._hard_terminalize_pending(
                pending,
                reason=f"compensation_failed_{reason}",
                at_ms=receipt.at_ms,
            )
        return self._record(
            receipt,
            CompositionStatus.TERMINAL_FAILURE,
            CompositionPhase.TERMINAL,
            state_version,
            reason=reason,
        )

    def _terminalize_partial(
        self,
        *,
        receipt: FinalTurnAdmissionReceipt,
        reserved: tuple[ReservedSpeech, ...],
        canonical: tuple[VoiceEvent, ...],
        accepted_permits: tuple[VoiceEvent, ...],
        state_version: int,
        reason: str,
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
            transport_act_ids=set(),
            observed_act_ids=set(),
            retired_permit_act_ids=set(),
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
            self.coordinator.retire_batch(pending.reserved)
            for item in pending.reserved:
                self._pending_by_act.pop(item.act_id, None)
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
