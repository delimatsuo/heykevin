"""Dedicated, offline-only authority for scripted opt-out closure."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import Enum
from threading import RLock

from app.services.voice_lifecycle import VoiceSessionBinding

_CONFIRMATION_DOMAIN = b"hey-kevin/offline-opt-out-confirmation/v1\x00"
_CAPABILITY_DOMAIN = b"hey-kevin/offline-local-closure-capability/v1\x00"
_STAGE_DOMAIN = b"hey-kevin/offline-local-closure-stage/v1\x00"
_COMMIT_DOMAIN = b"hey-kevin/offline-local-closure-commit/v1\x00"
_AUDIO_DOMAIN = b"hey-kevin/offline-local-closure-audio/v1\x00"
_DIGEST_LENGTH = 64
_MAX_ID = 128

_OPT_OUT_TEXT = {
    "en": "Okay. I’ll stop this test call now. Goodbye.",
    "es": (
        "De acuerdo. Finalizaré esta llamada de prueba ahora. "
        "Adiós."
    ),
    "zh": "好的。我现在结束这次测试通话。再见。",
}
_OPT_OUT_TEXT_DIGESTS = {
    locale: hashlib.sha256(text.encode("utf-8")).hexdigest()
    for locale, text in _OPT_OUT_TEXT.items()
}
_INPUT_LOCALES = frozenset({"en", "es", "pt", "zh"})


class OfflineClosureDestination(str, Enum):
    SYNTHETIC_LOOPBACK = "synthetic_loopback"


class OfflineClosurePrivacy(str, Enum):
    LOCAL_BUFFER_SCRUB = "local_buffer_scrub"


class OfflineClosureTransport(str, Enum):
    LOCAL_READY = "offline_local_ready"


class OfflineClosureStep(str, Enum):
    SCRIPTED_OPT_OUT = "scripted_opt_out"


class OfflineClosurePhase(str, Enum):
    LEASED = "leased"
    ACTIVE = "active"
    CAPABLE = "capable"
    STAGED = "staged"
    COMMITTED = "committed"
    TERMINATED = "terminated"


@dataclass(frozen=True, slots=True)
class OfflineAuthorityInventory:
    transaction_pending: int
    admission_receipts: int
    silence_pending: int
    speech_batches: int
    live_speech_acts: int
    queued_outbound_frames: int
    call_quiescent: bool
    call_terminated: bool
    adapter_terminally_closed: bool

    def __post_init__(self) -> None:
        counts = (
            self.transaction_pending,
            self.admission_receipts,
            self.silence_pending,
            self.speech_batches,
            self.live_speech_acts,
            self.queued_outbound_frames,
        )
        if (
            any(
                type(value) is not int or value < 0
                for value in counts
            )
            or any(
                type(value) is not bool
                for value in (
                    self.call_quiescent,
                    self.call_terminated,
                    self.adapter_terminally_closed,
                )
            )
        ):
            raise ValueError("offline authority inventory is invalid")

    @property
    def is_sealed(self) -> bool:
        return (
            self.transaction_pending == 0
            and self.admission_receipts == 0
            and self.silence_pending == 0
            and self.speech_batches == 0
            and self.live_speech_acts == 0
            and self.queued_outbound_frames == 0
            and self.call_quiescent
            and self.call_terminated
            and self.adapter_terminally_closed
        )


@dataclass(frozen=True, slots=True)
class ScriptedOptOutConfirmationReceipt:
    confirmation_id: str
    lease_revision: int
    expires_at_ms: int
    arm: str
    journey: str
    contract_digest: str
    binding: VoiceSessionBinding
    locale: str
    step: OfflineClosureStep

    def __post_init__(self) -> None:
        if (
            not _identifier(self.confirmation_id)
            or type(self.lease_revision) is not int
            or self.lease_revision < 0
            or type(self.expires_at_ms) is not int
            or self.expires_at_ms < 0
            or not _identifier(self.arm)
            or not _identifier(self.journey)
            or not _digest(self.contract_digest)
            or not isinstance(self.binding, VoiceSessionBinding)
            or self.locale not in _OPT_OUT_TEXT
            or self.step is not OfflineClosureStep.SCRIPTED_OPT_OUT
        ):
            raise ValueError("scripted opt-out confirmation is invalid")


@dataclass(frozen=True, slots=True)
class OfflineClosureCapability:
    capability_id: str
    confirmation_id: str
    active_revision: int
    expires_at_ms: int
    arm: str
    journey: str
    contract_digest: str
    binding: VoiceSessionBinding
    locale: str
    destination: OfflineClosureDestination
    privacy: OfflineClosurePrivacy
    transport: OfflineClosureTransport

    def __post_init__(self) -> None:
        if (
            not _identifier(self.capability_id)
            or not _identifier(self.confirmation_id)
            or type(self.active_revision) is not int
            or self.active_revision < 1
            or type(self.expires_at_ms) is not int
            or self.expires_at_ms < 0
            or not _identifier(self.arm)
            or not _identifier(self.journey)
            or not _digest(self.contract_digest)
            or not isinstance(self.binding, VoiceSessionBinding)
            or self.locale not in _OPT_OUT_TEXT
            or not isinstance(
                self.destination,
                OfflineClosureDestination,
            )
            or not isinstance(self.privacy, OfflineClosurePrivacy)
            or not isinstance(
                self.transport,
                OfflineClosureTransport,
            )
        ):
            raise ValueError("offline closure capability is invalid")


@dataclass(frozen=True, slots=True)
class OfflineClosureCommittedFrame:
    ordinal: int
    duration_ms: int
    payload: bytes
    audio_digest: str

    def __post_init__(self) -> None:
        if (
            self.ordinal != 0
            or type(self.duration_ms) is not int
            or self.duration_ms < 1
            or type(self.payload) is not bytes
            or not self.payload
            or not _digest(self.audio_digest)
            or hashlib.sha256(self.payload).hexdigest()
            != self.audio_digest
        ):
            raise ValueError("committed offline closure frame is invalid")


@dataclass(frozen=True, slots=True)
class _OwnedClosureFrame:
    ordinal: int
    duration_ms: int
    payload: bytearray
    audio_digest: str


@dataclass(frozen=True, slots=True)
class _OwnedClosureStage:
    receipt: OfflineClosureStageReceipt
    stage_id: str
    capability_id: str
    locale: str
    text: str
    text_digest: str
    audio_id: str
    playout_id: str
    transport: OfflineClosureTransport
    frame_ordinal: int
    frame_duration_ms: int
    frame_byte_count: int
    audio_digest: str
    frame: _OwnedClosureFrame


@dataclass(frozen=True, slots=True)
class _OwnedClosureCommit:
    receipt: OfflineClosureCommitReceipt
    commit_id: str
    stage_id: str
    locale: str
    text_digest: str
    audio_digest: str
    frame_count: int
    byte_count: int
    audio_ms: int
    committed_at_ms: int
    frame: _OwnedClosureFrame


@dataclass(frozen=True, slots=True)
class OfflineClosureStageReceipt:
    stage_id: str
    capability_id: str
    locale: str
    text: str
    text_digest: str
    audio_id: str
    playout_id: str
    transport: OfflineClosureTransport
    frame_ordinal: int
    frame_duration_ms: int
    frame_byte_count: int
    audio_digest: str

    def __post_init__(self) -> None:
        if (
            not _identifier(self.stage_id)
            or not _identifier(self.capability_id)
            or self.locale not in _OPT_OUT_TEXT
            or self.text != _OPT_OUT_TEXT[self.locale]
            or self.text_digest
            != _OPT_OUT_TEXT_DIGESTS[self.locale]
            or not _identifier(self.audio_id)
            or not _identifier(self.playout_id)
            or self.transport is not OfflineClosureTransport.LOCAL_READY
            or self.frame_ordinal != 0
            or self.frame_duration_ms != 20
            or type(self.frame_byte_count) is not int
            or not 0 < self.frame_byte_count <= 160
            or not _digest(self.audio_digest)
        ):
            raise ValueError("offline closure stage is invalid")


@dataclass(frozen=True, slots=True)
class OfflineClosureCommitReceipt:
    commit_id: str
    stage_id: str
    locale: str
    text_digest: str
    audio_digest: str
    frame_count: int
    byte_count: int
    audio_ms: int
    committed_at_ms: int

    def __post_init__(self) -> None:
        if (
            not _identifier(self.commit_id)
            or not _identifier(self.stage_id)
            or self.locale not in _OPT_OUT_TEXT
            or self.text_digest
            != _OPT_OUT_TEXT_DIGESTS[self.locale]
            or not _digest(self.audio_digest)
            or self.frame_count != 1
            or type(self.byte_count) is not int
            or self.byte_count < 1
            or type(self.audio_ms) is not int
            or self.audio_ms < 1
            or type(self.committed_at_ms) is not int
            or self.committed_at_ms < 0
        ):
            raise ValueError("offline closure commit is invalid")


@dataclass(frozen=True, slots=True)
class OfflineClosureSnapshot:
    phase: OfflineClosurePhase
    withdrawn: bool
    lease_revision: int
    active_revision: int
    confirmation_live: bool
    confirmation_tombstoned: bool
    capability_live: bool
    capability_tombstoned: bool
    committed_frame_count: int
    text_digest: str | None
    synthetic_playback_observed: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.phase, OfflineClosurePhase)
            or type(self.withdrawn) is not bool
            or type(self.lease_revision) is not int
            or self.lease_revision < 0
            or type(self.active_revision) is not int
            or self.active_revision < 0
            or any(
                type(value) is not bool
                for value in (
                    self.confirmation_live,
                    self.confirmation_tombstoned,
                    self.capability_live,
                    self.capability_tombstoned,
                    self.synthetic_playback_observed,
                )
            )
            or self.committed_frame_count not in {0, 1}
            or (
                self.text_digest is not None
                and not _digest(self.text_digest)
            )
        ):
            raise ValueError("offline closure snapshot is invalid")


@dataclass(frozen=True, slots=True)
class _RegistryState:
    phase: OfflineClosurePhase
    facade: object | None
    leased_record: object | None
    active_record: object | None
    participant_surrogate: object | None
    driver_identity: object | None
    lease_revision: int
    active_revision: int
    expires_at_ms: int
    arm: str
    journey: str
    contract_digest: str
    binding: VoiceSessionBinding
    locale: str
    destination: OfflineClosureDestination
    privacy: OfflineClosurePrivacy
    transport: OfflineClosureTransport
    withdrawn: bool = False
    confirmation: ScriptedOptOutConfirmationReceipt | None = None
    confirmation_id_tombstone: str | None = None
    confirmation_tombstoned: bool = False
    capability: OfflineClosureCapability | None = None
    capability_tombstoned: bool = False
    stage: _OwnedClosureStage | None = None
    commit: _OwnedClosureCommit | None = None
    inventory_sealed: bool = False
    synthetic_playback_observed: bool = False


@dataclass(frozen=True, slots=True)
class _TerminalRegistryState:
    phase: OfflineClosurePhase
    lease_revision: int
    active_revision: int
    withdrawn: bool
    confirmation_tombstoned: bool
    capability_tombstoned: bool
    text_digest: str | None
    synthetic_playback_observed: bool


@dataclass(slots=True)
class _RegistryEntry:
    state: _RegistryState | _TerminalRegistryState


class OfflineLocalClosureAuthority:
    """One stable, latch-owned registry for one active offline lease.

    Lock order is driver lock then this latch. This authority never acquires
    the driver lock or invokes a caller callback while holding its latch.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._entry: _RegistryEntry | None = None

    def register_leased(
        self,
        *,
        facade: object,
        leased_record: object,
        driver_identity: object,
        participant_surrogate: object,
        lease_revision: int,
        expires_at_ms: int,
        arm: str,
        journey: str,
        contract_digest: str,
        binding: VoiceSessionBinding,
        locale: str,
    ) -> bool:
        private_binding = _copy_binding(binding)
        if (
            facade is None
            or leased_record is None
            or driver_identity is None
            or participant_surrogate is None
            or type(lease_revision) is not int
            or lease_revision < 0
            or type(expires_at_ms) is not int
            or expires_at_ms < 0
            or not _identifier(arm)
            or not _identifier(journey)
            or not _digest(contract_digest)
            or private_binding is None
            or locale not in _INPUT_LOCALES
        ):
            return False
        with self._lock:
            current = self._entry
            if (
                current is not None
                and current.state.phase
                is not OfflineClosurePhase.TERMINATED
            ):
                return False
            self._entry = _RegistryEntry(
                _RegistryState(
                    phase=OfflineClosurePhase.LEASED,
                    facade=facade,
                    leased_record=leased_record,
                    active_record=None,
                    participant_surrogate=participant_surrogate,
                    driver_identity=driver_identity,
                    lease_revision=lease_revision,
                    active_revision=0,
                    expires_at_ms=expires_at_ms,
                    arm=arm,
                    journey=journey,
                    contract_digest=contract_digest,
                    binding=private_binding,
                    locale=locale,
                    destination=(
                        OfflineClosureDestination.SYNTHETIC_LOOPBACK
                    ),
                    privacy=(
                        OfflineClosurePrivacy.LOCAL_BUFFER_SCRUB
                    ),
                    transport=OfflineClosureTransport.LOCAL_READY,
                )
            )
            return True

    def confirm_scripted_step(
        self,
        *,
        facade: object,
        leased_record: object,
        driver_identity: object,
        participant_surrogate: object,
        now_ms: int,
    ) -> ScriptedOptOutConfirmationReceipt | None:
        if type(now_ms) is not int or now_ms < 0:
            return None
        with self._lock:
            entry = self._entry
            if entry is None:
                return None
            state = entry.state
            if (
                state.phase is not OfflineClosurePhase.LEASED
                or state.facade is not facade
                or state.leased_record is not leased_record
                or state.driver_identity is not driver_identity
                or state.participant_surrogate
                is not participant_surrogate
                or state.withdrawn
                or state.confirmation is not None
                or state.confirmation_tombstoned
                or now_ms > state.expires_at_ms
                or state.locale not in _OPT_OUT_TEXT
            ):
                return None
            confirmation_id = self._expected_confirmation_id(
                state
            )
            public_binding = _copy_binding(state.binding)
            if confirmation_id is None or public_binding is None:
                return None
            confirmation = ScriptedOptOutConfirmationReceipt(
                confirmation_id=confirmation_id,
                lease_revision=state.lease_revision,
                expires_at_ms=state.expires_at_ms,
                arm=state.arm,
                journey=state.journey,
                contract_digest=state.contract_digest,
                binding=public_binding,
                locale=state.locale,
                step=OfflineClosureStep.SCRIPTED_OPT_OUT,
            )
            entry.state = replace(
                state,
                confirmation=confirmation,
            )
            return confirmation

    def activate(
        self,
        *,
        facade: object,
        leased_record: object,
        active_record: object,
        driver_identity: object,
        active_revision: int,
    ) -> bool:
        if (
            active_record is None
            or type(active_revision) is not int
            or active_revision < 1
        ):
            return False
        with self._lock:
            entry = self._entry
            if entry is None:
                return False
            state = entry.state
            if (
                state.phase is not OfflineClosurePhase.LEASED
                or state.facade is not facade
                or state.leased_record is not leased_record
                or state.driver_identity is not driver_identity
                or state.active_record is not None
                or active_revision != state.lease_revision + 1
            ):
                return False
            entry.state = replace(
                state,
                phase=OfflineClosurePhase.ACTIVE,
                active_record=active_record,
                active_revision=active_revision,
            )
            return True

    def withdraw(self, *, facade: object) -> bool:
        """Irreversibly linearize withdrawal without taking the driver lock."""
        with self._lock:
            entry = self._entry
            if entry is None:
                return False
            state = entry.state
            if (
                state.phase is OfflineClosurePhase.TERMINATED
                or not isinstance(state, _RegistryState)
                or state.facade is not facade
            ):
                return False
            if state.withdrawn:
                return True
            entry.state = replace(state, withdrawn=True)
            return True

    def is_withdrawn(
        self,
        *,
        facade: object,
        active_record: object | None = None,
    ) -> bool:
        with self._lock:
            state = self._state_for(
                facade=facade,
                active_record=active_record,
            )
            return state is not None and state.withdrawn

    def mint_capability(
        self,
        *,
        facade: object,
        active_record: object,
        confirmation: ScriptedOptOutConfirmationReceipt,
        inventory: OfflineAuthorityInventory,
        now_ms: int,
    ) -> OfflineClosureCapability | None:
        if (
            not isinstance(inventory, OfflineAuthorityInventory)
            or not inventory.is_sealed
            or type(now_ms) is not int
            or now_ms < 0
        ):
            return None
        with self._lock:
            entry = self._entry
            if entry is None:
                return None
            state = entry.state
            if (
                state.phase is not OfflineClosurePhase.ACTIVE
                or state.facade is not facade
                or state.active_record is not active_record
                or state.withdrawn
                or state.confirmation is not confirmation
                or state.confirmation_tombstoned
                or state.capability is not None
                or state.capability_tombstoned
                or now_ms > state.expires_at_ms
                or not self._confirmation_matches(
                    state,
                    confirmation,
                )
            ):
                return None
            confirmation_id = self._expected_confirmation_id(
                state
            )
            if confirmation_id is None:
                return None
            public_binding = _copy_binding(state.binding)
            if public_binding is None:
                return None
            capability = OfflineClosureCapability(
                capability_id=_token(
                    _CAPABILITY_DOMAIN,
                    confirmation_id,
                    str(state.active_revision),
                    state.binding.stream_binding,
                ),
                confirmation_id=confirmation_id,
                active_revision=state.active_revision,
                expires_at_ms=state.expires_at_ms,
                arm=state.arm,
                journey=state.journey,
                contract_digest=state.contract_digest,
                binding=public_binding,
                locale=state.locale,
                destination=state.destination,
                privacy=state.privacy,
                transport=state.transport,
            )
            if entry.state is not state:
                return None
            entry.state = replace(
                state,
                phase=OfflineClosurePhase.CAPABLE,
                confirmation=None,
                confirmation_id_tombstone=confirmation_id,
                confirmation_tombstoned=True,
                capability=capability,
                inventory_sealed=True,
            )
            return capability

    def stage(
        self,
        *,
        facade: object,
        active_record: object,
        capability: OfflineClosureCapability,
        now_ms: int,
        max_frame_bytes: int,
        max_outbound_frames: int,
        max_outbound_bytes: int,
        max_outbound_audio_ms: int,
    ) -> OfflineClosureStageReceipt | None:
        limits = (
            max_frame_bytes,
            max_outbound_frames,
            max_outbound_bytes,
            max_outbound_audio_ms,
        )
        if (
            type(now_ms) is not int
            or now_ms < 0
            or any(
                type(value) is not int or value < 1
                for value in limits
            )
        ):
            return None
        with self._lock:
            entry = self._entry
            if entry is None:
                return None
            state = entry.state
            if (
                state.phase is not OfflineClosurePhase.CAPABLE
                or state.facade is not facade
                or state.active_record is not active_record
                or state.withdrawn
                or state.capability is not capability
                or state.capability_tombstoned
                or not state.inventory_sealed
                or now_ms > state.expires_at_ms
                or not self._capability_matches(state, capability)
                or max_outbound_frames < 1
                or max_outbound_audio_ms < 20
            ):
                return None
            frame_size = min(160, max_frame_bytes)
            if frame_size > max_outbound_bytes:
                return None
            capability_id = self._expected_capability_id(
                state
            )
            if capability_id is None:
                return None
            text = _OPT_OUT_TEXT[state.locale]
            text_digest = _OPT_OUT_TEXT_DIGESTS[state.locale]
            stage_id = _token(
                _STAGE_DOMAIN,
                capability_id,
                text_digest,
            )
            seed = hashlib.sha256(
                _AUDIO_DOMAIN + stage_id.encode("ascii")
            ).digest()
            payload = bytearray(
                seed[index % len(seed)]
                for index in range(frame_size)
            )
            audio_digest = hashlib.sha256(payload).hexdigest()
            stage = OfflineClosureStageReceipt(
                stage_id=stage_id,
                capability_id=capability_id,
                locale=state.locale,
                text=text,
                text_digest=text_digest,
                audio_id=f"local_audio_{stage_id[:24]}",
                playout_id=f"local_playout_{stage_id[:24]}",
                transport=state.transport,
                frame_ordinal=0,
                frame_duration_ms=20,
                frame_byte_count=len(payload),
                audio_digest=audio_digest,
            )
            owned_stage = _OwnedClosureStage(
                receipt=stage,
                stage_id=stage_id,
                capability_id=capability_id,
                locale=state.locale,
                text=text,
                text_digest=text_digest,
                audio_id=f"local_audio_{stage_id[:24]}",
                playout_id=f"local_playout_{stage_id[:24]}",
                transport=state.transport,
                frame_ordinal=0,
                frame_duration_ms=20,
                frame_byte_count=len(payload),
                audio_digest=audio_digest,
                frame=_OwnedClosureFrame(
                    ordinal=0,
                    duration_ms=20,
                    payload=payload,
                    audio_digest=audio_digest,
                ),
            )
            if entry.state is not state:
                payload[:] = b"\x00" * len(payload)
                return None
            entry.state = replace(
                state,
                phase=OfflineClosurePhase.STAGED,
                stage=owned_stage,
            )
            return stage

    def commit(
        self,
        *,
        facade: object,
        active_record: object,
        capability: OfflineClosureCapability,
        stage: OfflineClosureStageReceipt,
        now_ms: int,
    ) -> OfflineClosureCommitReceipt | None:
        """Transfer one private staged frame by one state assignment."""
        if type(now_ms) is not int or now_ms < 0:
            self._tombstone_failed_commit(
                facade=facade,
                active_record=active_record,
            )
            return None
        with self._lock:
            entry = self._entry
            if entry is None:
                return None
            state = entry.state
            if not isinstance(state, _RegistryState):
                return None
            if not self._can_commit(
                state,
                facade=facade,
                active_record=active_record,
                capability=capability,
                stage=stage,
                now_ms=now_ms,
            ):
                self._tombstone_live_capability(
                    entry=entry,
                    state=state,
                    facade=facade,
                    active_record=active_record,
                )
                return None
            captured_state = state
            captured_stage = state.stage
            if captured_stage is None:
                return None
            captured_frame = captured_stage.frame
            captured_stage_id = captured_stage.stage_id
            captured_locale = captured_stage.locale
            captured_text_digest = captured_stage.text_digest
        commit = OfflineClosureCommitReceipt(
            commit_id=_token(
                _COMMIT_DOMAIN,
                captured_stage_id,
                str(now_ms),
            ),
            stage_id=captured_stage_id,
            locale=captured_locale,
            text_digest=captured_text_digest,
            audio_digest=captured_frame.audio_digest,
            frame_count=1,
            byte_count=len(captured_frame.payload),
            audio_ms=captured_frame.duration_ms,
            committed_at_ms=now_ms,
        )
        owned_commit = _OwnedClosureCommit(
            receipt=commit,
            commit_id=commit.commit_id,
            stage_id=captured_stage_id,
            locale=captured_locale,
            text_digest=captured_text_digest,
            audio_digest=captured_frame.audio_digest,
            frame_count=1,
            byte_count=len(captured_frame.payload),
            audio_ms=captured_frame.duration_ms,
            committed_at_ms=now_ms,
            frame=captured_frame,
        )
        committed_state = replace(
            captured_state,
            phase=OfflineClosurePhase.COMMITTED,
            capability=None,
            capability_tombstoned=True,
            stage=None,
            commit=owned_commit,
        )
        with self._lock:
            entry = self._entry
            if (
                entry is None
                or not isinstance(entry.state, _RegistryState)
            ):
                return None
            current = entry.state
            if (
                current is not captured_state
                or not self._can_commit(
                    current,
                    facade=facade,
                    active_record=active_record,
                    capability=capability,
                    stage=stage,
                    now_ms=now_ms,
                )
                or current.stage is not captured_stage
                or not self._owned_stage_matches(
                    current,
                    stage=captured_stage,
                )
                or not self._owned_commit_matches_stage(
                    commit=owned_commit,
                    stage=captured_stage,
                    now_ms=now_ms,
                )
            ):
                self._tombstone_live_capability(
                    entry=entry,
                    state=current,
                    facade=facade,
                    active_record=active_record,
                )
                return None
            entry.state = committed_state
        return commit

    def committed_frame(
        self,
        *,
        facade: object,
        active_record: object,
    ) -> OfflineClosureCommittedFrame | None:
        with self._lock:
            state = self._state_for(
                facade=facade,
                active_record=active_record,
            )
            if (
                state is None
                or state.phase is not OfflineClosurePhase.COMMITTED
                or state.commit is None
                or not self._committed_frame_matches(state)
            ):
                return None
            frame = state.commit.frame
            return OfflineClosureCommittedFrame(
                ordinal=frame.ordinal,
                duration_ms=frame.duration_ms,
                payload=bytes(frame.payload),
                audio_digest=frame.audio_digest,
            )

    def mark_synthetic_playback(
        self,
        *,
        facade: object,
        active_record: object,
        commit: OfflineClosureCommitReceipt,
    ) -> bool:
        """Record fixture evidence only; this is never delivery evidence."""
        with self._lock:
            entry = self._entry
            if entry is None:
                return False
            state = entry.state
            if (
                state.phase is not OfflineClosurePhase.COMMITTED
                or state.facade is not facade
                or state.active_record is not active_record
                or state.commit is None
                or state.commit.receipt is not commit
                or state.synthetic_playback_observed
                or not self._committed_frame_matches(state)
            ):
                return False
            entry.state = replace(
                state,
                synthetic_playback_observed=True,
            )
            return True

    def snapshot(
        self,
        *,
        facade: object,
        active_record: object | None = None,
    ) -> OfflineClosureSnapshot | None:
        with self._lock:
            state = self._state_for(
                facade=facade,
                active_record=active_record,
            )
            if state is None:
                return None
            return self._snapshot(state)

    def terminate(
        self,
        *,
        facade: object,
        active_record: object | None = None,
    ) -> tuple[OfflineClosureSnapshot, tuple[bytearray, ...]] | None:
        """Drop authority references and zeroize every owned payload."""
        with self._lock:
            entry = self._entry
            if entry is None:
                return None
            state = entry.state
            if (
                not isinstance(state, _RegistryState)
                or state.facade is not facade
                or (
                    active_record is not None
                    and state.active_record is not active_record
                )
            ):
                return None
            payloads = self._payloads(state)
            terminal_state = _TerminalRegistryState(
                phase=OfflineClosurePhase.TERMINATED,
                lease_revision=state.lease_revision,
                active_revision=state.active_revision,
                withdrawn=state.withdrawn,
                confirmation_tombstoned=(
                    state.confirmation_tombstoned
                    or state.confirmation is not None
                ),
                capability_tombstoned=(
                    state.capability_tombstoned
                    or state.capability is not None
                ),
                text_digest=(
                    state.commit.text_digest
                    if state.commit is not None
                    else None
                ),
                synthetic_playback_observed=(
                    state.synthetic_playback_observed
                ),
            )
            entry.state = terminal_state
            for payload in payloads:
                payload[:] = b"\x00" * len(payload)
            snapshot = self._snapshot(terminal_state)
        return snapshot, payloads

    @property
    def tombstone_count(self) -> int:
        with self._lock:
            if self._entry is None:
                return 0
            state = self._entry.state
            return int(state.confirmation_tombstoned) + int(
                state.capability_tombstoned
            )

    def _state_for(
        self,
        *,
        facade: object,
        active_record: object | None,
    ) -> _RegistryState | None:
        entry = self._entry
        if entry is None:
            return None
        state = entry.state
        if (
            not isinstance(state, _RegistryState)
            or state.facade is not facade
        ):
            return None
        if (
            active_record is not None
            and state.active_record is not active_record
        ):
            return None
        return state

    @staticmethod
    def _expected_confirmation_id(
        state: _RegistryState,
    ) -> str | None:
        if (
            not _digest(state.contract_digest)
            or not _valid_binding(state.binding)
            or type(state.lease_revision) is not int
            or type(state.locale) is not str
            or state.locale not in _OPT_OUT_TEXT
        ):
            return None
        return _token(
            _CONFIRMATION_DOMAIN,
            state.contract_digest,
            state.binding.call_binding,
            str(state.binding.epoch),
            str(state.lease_revision),
            state.locale,
        )

    @staticmethod
    def _expected_capability_id(
        state: _RegistryState,
    ) -> str | None:
        confirmation_id = state.confirmation_id_tombstone
        expected_confirmation_id = (
            OfflineLocalClosureAuthority
            ._expected_confirmation_id(state)
        )
        if (
            not _identifier(confirmation_id)
            or confirmation_id != expected_confirmation_id
            or type(state.active_revision) is not int
            or not _valid_binding(state.binding)
        ):
            return None
        return _token(
            _CAPABILITY_DOMAIN,
            confirmation_id,
            str(state.active_revision),
            state.binding.stream_binding,
        )

    @classmethod
    def _confirmation_matches(
        cls,
        state: _RegistryState,
        confirmation: ScriptedOptOutConfirmationReceipt,
    ) -> bool:
        if (
            type(confirmation)
            is not ScriptedOptOutConfirmationReceipt
            or not _identifier(confirmation.confirmation_id)
            or type(confirmation.lease_revision) is not int
            or type(confirmation.expires_at_ms) is not int
            or not _identifier(confirmation.arm)
            or not _identifier(confirmation.journey)
            or not _digest(confirmation.contract_digest)
            or not _bindings_match(
                confirmation.binding,
                state.binding,
            )
            or type(confirmation.locale) is not str
            or confirmation.locale not in _OPT_OUT_TEXT
            or confirmation.step
            is not OfflineClosureStep.SCRIPTED_OPT_OUT
        ):
            return False
        expected_id = cls._expected_confirmation_id(state)
        return (
            expected_id is not None
            and
            confirmation.confirmation_id == expected_id
            and confirmation.lease_revision == state.lease_revision
            and confirmation.expires_at_ms == state.expires_at_ms
            and confirmation.arm == state.arm
            and confirmation.journey == state.journey
            and confirmation.contract_digest
            == state.contract_digest
            and confirmation.locale == state.locale
            and confirmation.step
            is OfflineClosureStep.SCRIPTED_OPT_OUT
        )

    @classmethod
    def _capability_matches(
        cls,
        state: _RegistryState,
        capability: OfflineClosureCapability,
    ) -> bool:
        if (
            type(capability) is not OfflineClosureCapability
            or not _identifier(capability.confirmation_id)
            or not _identifier(capability.capability_id)
            or type(capability.active_revision) is not int
            or type(capability.expires_at_ms) is not int
            or not _identifier(capability.arm)
            or not _identifier(capability.journey)
            or not _digest(capability.contract_digest)
            or not _bindings_match(
                capability.binding,
                state.binding,
            )
            or type(capability.locale) is not str
            or capability.locale not in _OPT_OUT_TEXT
            or not isinstance(
                capability.destination,
                OfflineClosureDestination,
            )
            or not isinstance(
                capability.privacy,
                OfflineClosurePrivacy,
            )
            or not isinstance(
                capability.transport,
                OfflineClosureTransport,
            )
        ):
            return False
        expected_capability_id = cls._expected_capability_id(
            state
        )
        return (
            state.confirmation_id_tombstone is not None
            and expected_capability_id is not None
            and capability.confirmation_id
            == state.confirmation_id_tombstone
            and capability.capability_id == expected_capability_id
            and capability.active_revision == state.active_revision
            and capability.expires_at_ms == state.expires_at_ms
            and capability.arm == state.arm
            and capability.journey == state.journey
            and capability.contract_digest == state.contract_digest
            and capability.locale == state.locale
            and capability.destination is state.destination
            and capability.privacy is state.privacy
            and capability.transport is state.transport
        )

    @classmethod
    def _can_commit(
        cls,
        state: _RegistryState,
        *,
        facade: object,
        active_record: object,
        capability: OfflineClosureCapability,
        stage: OfflineClosureStageReceipt,
        now_ms: int,
    ) -> bool:
        return (
            state.phase is OfflineClosurePhase.STAGED
            and state.facade is facade
            and state.active_record is active_record
            and not state.withdrawn
            and state.inventory_sealed
            and state.capability is capability
            and not state.capability_tombstoned
            and state.stage is not None
            and state.stage.receipt is stage
            and state.commit is None
            and now_ms <= state.expires_at_ms
            and cls._capability_matches(state, capability)
            and cls._stage_matches(
                state,
                stage=stage,
            )
        )

    def _tombstone_failed_commit(
        self,
        *,
        facade: object,
        active_record: object,
    ) -> None:
        with self._lock:
            entry = self._entry
            if entry is not None and isinstance(
                entry.state,
                _RegistryState,
            ):
                self._tombstone_live_capability(
                    entry=entry,
                    state=entry.state,
                    facade=facade,
                    active_record=active_record,
                )

    @staticmethod
    def _tombstone_live_capability(
        *,
        entry: _RegistryEntry,
        state: _RegistryState,
        facade: object,
        active_record: object,
    ) -> bool:
        if (
            state.phase is not OfflineClosurePhase.STAGED
            or state.facade is not facade
            or state.active_record is not active_record
            or state.capability is None
            or state.capability_tombstoned
            or state.commit is not None
        ):
            return False
        payload = (
            state.stage.frame.payload
            if state.stage is not None
            else None
        )
        if payload is not None:
            payload[:] = b"\x00" * len(payload)
        entry.state = replace(
            state,
            capability=None,
            capability_tombstoned=True,
            stage=None,
        )
        return True

    @classmethod
    def _committed_frame_matches(
        cls,
        state: _RegistryState,
    ) -> bool:
        commit = state.commit
        if commit is None:
            return False
        frame = commit.frame
        return (
            type(frame) is _OwnedClosureFrame
            and type(frame.ordinal) is int
            and frame.ordinal == 0
            and type(frame.duration_ms) is int
            and frame.duration_ms == commit.audio_ms
            and type(frame.payload) is bytearray
            and len(frame.payload) == commit.byte_count
            and _digest(frame.audio_digest)
            and frame.audio_digest == commit.audio_digest
            and hashlib.sha256(frame.payload).hexdigest()
            == commit.audio_digest
            and cls._commit_receipt_matches(commit)
        )

    @staticmethod
    def _commit_receipt_matches(
        commit: _OwnedClosureCommit,
    ) -> bool:
        receipt = commit.receipt
        if (
            type(receipt) is not OfflineClosureCommitReceipt
            or not _identifier(receipt.commit_id)
            or not _identifier(receipt.stage_id)
            or type(receipt.locale) is not str
            or receipt.locale not in _OPT_OUT_TEXT
            or not _digest(receipt.text_digest)
            or not _digest(receipt.audio_digest)
            or type(receipt.frame_count) is not int
            or type(receipt.byte_count) is not int
            or type(receipt.audio_ms) is not int
            or type(receipt.committed_at_ms) is not int
        ):
            return False
        return (
            receipt.commit_id == commit.commit_id
            and receipt.stage_id == commit.stage_id
            and receipt.locale == commit.locale
            and receipt.text_digest == commit.text_digest
            and receipt.audio_digest == commit.audio_digest
            and receipt.frame_count == commit.frame_count
            and receipt.byte_count == commit.byte_count
            and receipt.audio_ms == commit.audio_ms
            and receipt.committed_at_ms == commit.committed_at_ms
            and commit.locale in _OPT_OUT_TEXT
            and commit.text_digest
            == _OPT_OUT_TEXT_DIGESTS[commit.locale]
            and commit.frame_count == 1
            and commit.byte_count > 0
            and commit.audio_ms == 20
        )

    @classmethod
    def _owned_stage_matches(
        cls,
        state: _RegistryState,
        *,
        stage: _OwnedClosureStage,
    ) -> bool:
        receipt = stage.receipt
        frame = stage.frame
        capability_id = cls._expected_capability_id(state)
        if (
            capability_id is None
            or type(receipt) is not OfflineClosureStageReceipt
            or not _identifier(receipt.stage_id)
            or not _identifier(receipt.capability_id)
            or type(receipt.locale) is not str
            or type(receipt.text) is not str
            or not _digest(receipt.text_digest)
            or not _identifier(receipt.audio_id)
            or not _identifier(receipt.playout_id)
            or not isinstance(
                receipt.transport,
                OfflineClosureTransport,
            )
            or type(receipt.frame_ordinal) is not int
            or type(receipt.frame_duration_ms) is not int
            or type(receipt.frame_byte_count) is not int
            or not _digest(receipt.audio_digest)
            or not _identifier(stage.stage_id)
            or not _identifier(stage.capability_id)
            or type(stage.locale) is not str
            or type(stage.text) is not str
            or not _digest(stage.text_digest)
            or not _identifier(stage.audio_id)
            or not _identifier(stage.playout_id)
            or not isinstance(
                stage.transport,
                OfflineClosureTransport,
            )
            or type(stage.frame_ordinal) is not int
            or type(stage.frame_duration_ms) is not int
            or type(stage.frame_byte_count) is not int
            or not _digest(stage.audio_digest)
            or state.locale not in _OPT_OUT_TEXT
            or type(frame.payload) is not bytearray
        ):
            return False
        text = _OPT_OUT_TEXT[state.locale]
        text_digest = _OPT_OUT_TEXT_DIGESTS[state.locale]
        stage_id = _token(
            _STAGE_DOMAIN,
            capability_id,
            text_digest,
        )
        seed = hashlib.sha256(
            _AUDIO_DOMAIN + stage_id.encode("ascii")
        ).digest()
        payload = frame.payload
        audio_digest = hashlib.sha256(payload).hexdigest()
        return (
            stage.capability_id == capability_id
            and stage.stage_id == stage_id
            and stage.locale == state.locale
            and stage.text == text
            and stage.text_digest == text_digest
            and stage.audio_id == f"local_audio_{stage_id[:24]}"
            and stage.playout_id
            == f"local_playout_{stage_id[:24]}"
            and stage.transport is state.transport
            and stage.frame_ordinal == 0
            and stage.frame_duration_ms == 20
            and stage.frame_byte_count == len(payload)
            and stage.audio_digest == audio_digest
            and receipt.capability_id == stage.capability_id
            and receipt.stage_id == stage.stage_id
            and receipt.locale == stage.locale
            and receipt.text == stage.text
            and receipt.text_digest == stage.text_digest
            and receipt.audio_id == stage.audio_id
            and receipt.playout_id
            == stage.playout_id
            and receipt.transport is stage.transport
            and receipt.frame_ordinal == stage.frame_ordinal
            and receipt.frame_duration_ms
            == stage.frame_duration_ms
            and receipt.frame_byte_count
            == stage.frame_byte_count
            and receipt.audio_digest == stage.audio_digest
            and frame.ordinal == stage.frame_ordinal
            and frame.duration_ms == stage.frame_duration_ms
            and frame.audio_digest == stage.audio_digest
            and 0 < len(payload) <= 160
            and payload
            == bytearray(
                seed[index % len(seed)]
                for index in range(len(payload))
            )
        )

    @classmethod
    def _stage_matches(
        cls,
        state: _RegistryState,
        *,
        stage: OfflineClosureStageReceipt,
    ) -> bool:
        owned_stage = state.stage
        return (
            type(stage) is OfflineClosureStageReceipt
            and owned_stage is not None
            and owned_stage.receipt is stage
            and cls._owned_stage_matches(
                state,
                stage=owned_stage,
            )
        )

    @classmethod
    def _owned_commit_matches_stage(
        cls,
        *,
        commit: _OwnedClosureCommit,
        stage: _OwnedClosureStage,
        now_ms: int,
    ) -> bool:
        expected_commit_id = _token(
            _COMMIT_DOMAIN,
            stage.stage_id,
            str(now_ms),
        )
        return (
            type(commit) is _OwnedClosureCommit
            and commit.commit_id == expected_commit_id
            and commit.stage_id == stage.stage_id
            and commit.locale == stage.locale
            and commit.text_digest == stage.text_digest
            and commit.audio_digest == stage.audio_digest
            and commit.frame_count == 1
            and commit.byte_count == stage.frame_byte_count
            and commit.audio_ms == stage.frame_duration_ms
            and commit.committed_at_ms == now_ms
            and commit.frame is stage.frame
            and cls._commit_receipt_matches(commit)
        )

    @staticmethod
    def _payloads(
        state: _RegistryState,
    ) -> tuple[bytearray, ...]:
        payloads: list[bytearray] = []
        for frame in (
            state.stage.frame if state.stage is not None else None,
            (
                state.commit.frame
                if state.commit is not None
                else None
            ),
        ):
            if (
                frame is not None
                and all(frame.payload is not item for item in payloads)
            ):
                payloads.append(frame.payload)
        return tuple(payloads)

    @staticmethod
    def _snapshot(
        state: _RegistryState | _TerminalRegistryState,
    ) -> OfflineClosureSnapshot:
        terminal = isinstance(state, _TerminalRegistryState)
        return OfflineClosureSnapshot(
            phase=state.phase,
            withdrawn=state.withdrawn,
            lease_revision=state.lease_revision,
            active_revision=state.active_revision,
            confirmation_live=(
                False
                if terminal
                else state.confirmation is not None
            ),
            confirmation_tombstoned=(
                state.confirmation_tombstoned
            ),
            capability_live=(
                False
                if terminal
                else state.capability is not None
            ),
            capability_tombstoned=state.capability_tombstoned,
            committed_frame_count=(
                0
                if terminal
                else int(state.commit is not None)
            ),
            text_digest=(
                state.text_digest
                if terminal
                else state.commit.text_digest
                if state.commit is not None
                else None
            ),
            synthetic_playback_observed=(
                state.synthetic_playback_observed
            ),
        )


def opt_out_text_digest(locale: str) -> str | None:
    return _OPT_OUT_TEXT_DIGESTS.get(locale)


def _identifier(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= _MAX_ID
        and value.isascii()
    )


def _digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == _DIGEST_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _copy_binding(
    binding: object,
) -> VoiceSessionBinding | None:
    values = _binding_values(binding)
    if values is None:
        return None
    return VoiceSessionBinding(
        environment=values[0],
        contractor_binding=values[1],
        call_binding=values[2],
        stream_binding=values[3],
        epoch=values[4],
    )


def _valid_binding(binding: object) -> bool:
    return _binding_values(binding) is not None


def _bindings_match(
    public_binding: object,
    private_binding: object,
) -> bool:
    public_values = _binding_values(public_binding)
    private_values = _binding_values(private_binding)
    return (
        public_values is not None
        and private_values is not None
        and public_values == private_values
    )


def _binding_values(
    binding: object,
) -> tuple[str, str, str, str, int] | None:
    if type(binding) is not VoiceSessionBinding:
        return None
    try:
        values = (
            binding.environment,
            binding.contractor_binding,
            binding.call_binding,
            binding.stream_binding,
            binding.epoch,
        )
    except AttributeError:
        return None
    if not _valid_binding_values(*values):
        return None
    return values


def _valid_binding_values(
    environment: object,
    contractor_binding: object,
    call_binding: object,
    stream_binding: object,
    epoch: object,
) -> bool:
    identifiers = (
        environment,
        contractor_binding,
        call_binding,
        stream_binding,
    )
    return (
        all(
            type(value) is str
            and value
            and len(value) <= _MAX_ID
            for value in identifiers
        )
        and type(epoch) is int
        and epoch >= 0
    )


def _token(domain: bytes, *parts: str) -> str:
    digest = hashlib.sha256()
    digest.update(domain)
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


__all__ = [
    "OfflineAuthorityInventory",
    "OfflineClosureCapability",
    "OfflineClosureCommitReceipt",
    "OfflineClosureCommittedFrame",
    "OfflineClosureDestination",
    "OfflineClosurePhase",
    "OfflineClosurePrivacy",
    "OfflineClosureSnapshot",
    "OfflineClosureStageReceipt",
    "OfflineClosureStep",
    "OfflineClosureTransport",
    "OfflineLocalClosureAuthority",
    "ScriptedOptOutConfirmationReceipt",
    "opt_out_text_digest",
]
