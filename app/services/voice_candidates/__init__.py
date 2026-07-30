"""Provider-SDK-free contracts for offline voice candidate adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock

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


class AdapterRejectReason(str, Enum):
    INVALID_SIGNAL = "invalid_signal"
    BINDING_MISMATCH = "binding_mismatch"
    STALE_EPOCH = "stale_epoch"
    PERMIT_REQUIRED = "permit_required"
    LIMIT_EXCEEDED = "limit_exceeded"
    TOOL_DENIED = "tool_denied"
    TERMINAL_DENIED = "terminal_denied"
    TIMEOUT = "timeout"
    OUT_OF_ORDER = "out_of_order"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"


@dataclass(frozen=True, slots=True)
class CandidateLimits:
    output_tokens: int
    audio_ms: int
    byte_count: int
    wall_clock_ms: int
    cost_minor_units: int
    request_count: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (
                self.output_tokens,
                self.audio_ms,
                self.byte_count,
                self.wall_clock_ms,
                self.cost_minor_units,
                self.request_count,
            )
        ):
            raise ValueError("candidate limits must be positive integers")


@dataclass(frozen=True, slots=True)
class CandidateUsage:
    output_tokens: int = 0
    audio_ms: int = 0
    byte_count: int = 0
    wall_clock_ms: int = 0
    cost_minor_units: int = 0

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.output_tokens,
                self.audio_ms,
                self.byte_count,
                self.wall_clock_ms,
                self.cost_minor_units,
            )
        ):
            raise ValueError("candidate usage must be nonnegative integers")

    def exceeds(self, limits: CandidateLimits) -> bool:
        return self.output_tokens >= limits.output_tokens or self.audio_ms >= limits.audio_ms or self.byte_count >= limits.byte_count or self.wall_clock_ms >= limits.wall_clock_ms or self.cost_minor_units >= limits.cost_minor_units

    def regresses(self, prior: CandidateUsage) -> bool:
        return self.output_tokens < prior.output_tokens or self.audio_ms < prior.audio_ms or self.byte_count < prior.byte_count or self.wall_clock_ms < prior.wall_clock_ms or self.cost_minor_units < prior.cost_minor_units


@dataclass(frozen=True, slots=True)
class EventContext:
    binding: VoiceSessionBinding
    sequence: int
    at_ms: int
    input_turn_id: str
    generation_id: str
    semantic_act_id: str
    semantic_act_kind: VoiceSemanticActKind

    def __post_init__(self) -> None:
        VoiceEvent(
            schema_version=VOICE_SCHEMA_VERSION,
            kind=VoiceEventKind.RESPONSE_AUTHORIZED,
            source=VoiceSource.LOCAL_AUTHORITATIVE,
            sensitivity=VoiceSensitivity.OPERATIONAL,
            binding=self.binding,
            sequence=self.sequence,
            at_ms=self.at_ms,
            input_turn_id=self.input_turn_id,
            generation_id=self.generation_id,
            semantic_act_id=self.semantic_act_id,
            semantic_act_kind=self.semantic_act_kind,
            payload=VoicePayload(),
        )

    def event(
        self,
        kind: VoiceEventKind,
        *,
        source: VoiceSource,
        payload: VoicePayload | None = None,
    ) -> VoiceEvent:
        return VoiceEvent(
            schema_version=VOICE_SCHEMA_VERSION,
            kind=kind,
            source=source,
            sensitivity=VoiceSensitivity.OPERATIONAL,
            binding=self.binding,
            sequence=self.sequence,
            at_ms=self.at_ms,
            input_turn_id=self.input_turn_id,
            generation_id=self.generation_id,
            semantic_act_id=self.semantic_act_id,
            semantic_act_kind=self.semantic_act_kind,
            payload=payload or VoicePayload(),
        )


@dataclass(frozen=True, slots=True)
class _AdapterFinalInputAdmission:
    """Opaque one-use proof created only by an adapter final-input transition."""

    marker: object


@dataclass(frozen=True, slots=True)
class AdapterResult:
    accepted: bool
    events: tuple[VoiceEvent, ...] = ()
    timeout_intents: tuple[VoiceTimeoutIntent, ...] = ()
    reason: AdapterRejectReason | None = None
    final_input_admission: _AdapterFinalInputAdmission | None = None

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise ValueError("adapter result acceptance is invalid")
        if any(not isinstance(event, VoiceEvent) for event in self.events):
            raise ValueError("adapter result events are invalid")
        if any(not isinstance(intent, VoiceTimeoutIntent) for intent in self.timeout_intents):
            raise ValueError("adapter result timeout intents are invalid")
        if self.accepted == (self.reason is not None):
            raise ValueError("adapter result reason is inconsistent")
        final_events = tuple(
            event
            for event in self.events
            if event.kind is VoiceEventKind.INPUT_TURN_FINAL
        )
        if (
            (self.final_input_admission is None) != (not final_events)
            or len(final_events) > 1
            or (
                self.final_input_admission is not None
                and not isinstance(
                    self.final_input_admission,
                    _AdapterFinalInputAdmission,
                )
            )
        ):
            raise ValueError("adapter final-input admission is inconsistent")


class OfflineCandidateAdapter:
    """Common permit and bound enforcement; it performs no I/O."""

    arm: CandidateArm

    def __init__(
        self,
        *,
        binding: VoiceSessionBinding,
        limits: CandidateLimits,
    ) -> None:
        if not isinstance(binding, VoiceSessionBinding):
            raise TypeError("candidate binding is invalid")
        if not isinstance(limits, CandidateLimits):
            raise TypeError("candidate limits are invalid")
        self.binding = binding
        self.limits = limits
        self._permitted: set[tuple[str, str, str, VoiceSemanticActKind]] = set()
        self._confirmed: set[tuple[str, str, str, VoiceSemanticActKind]] = set()
        self._failed: set[tuple[str, str, str, VoiceSemanticActKind]] = set()
        self._retired_permit_keys: set[tuple[str, str, str, VoiceSemanticActKind]] = set()
        self._disconnect_revoked_permit_keys: set[
            tuple[str, str, str, VoiceSemanticActKind]
        ] = set()
        self._admitted_input_keys: set[tuple[str, str, str, VoiceSemanticActKind]] = set()
        self._final_input_admissions: dict[
            int,
            tuple[_AdapterFinalInputAdmission, VoiceEvent, int],
        ] = {}
        self._final_input_transition_ids: set[str] = set()
        self._permit_admission_closed = False
        self._terminally_closed = False
        self._admission_revision = 0
        self._authority_lock = RLock()
        self._canonical_lifecycle: VoiceLifecycle | None = None
        self._pending_resume_admission: (
            tuple[AdapterResult, VoiceEvent, int] | None
        ) = None
        self._usage: dict[tuple[str, str, str, VoiceSemanticActKind], CandidateUsage] = {}
        self._request_count = 0

    @staticmethod
    def permit_key(
        context: EventContext,
    ) -> tuple[str, str, str, VoiceSemanticActKind]:
        return (
            context.input_turn_id,
            context.generation_id,
            context.semantic_act_id,
            context.semantic_act_kind,
        )

    def accept_permit(
        self,
        event: VoiceEvent,
        *,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        with self._authority_lock:
            if not isinstance(event, VoiceEvent) or not isinstance(lifecycle, VoiceLifecycle) or lifecycle.binding != self.binding or (self._canonical_lifecycle is not None and lifecycle is not self._canonical_lifecycle) or event.binding != self.binding or event.kind is not VoiceEventKind.RESPONSE_AUTHORIZED or event.source is not VoiceSource.LOCAL_AUTHORITATIVE or not lifecycle.accepts_response_authorization(event) or self._permit_admission_closed or self._terminally_closed:
                return False
            key = (
                event.input_turn_id,
                event.generation_id,
                event.semantic_act_id,
                event.semantic_act_kind,
            )
            if key in self._permitted or key in self._failed or key in self._retired_permit_keys or len(self._permitted | self._retired_permit_keys) >= self.limits.request_count:
                return False
            self._permitted.add(key)
            return True

    @property
    def permit_admission_closed(self) -> bool:
        with self._authority_lock:
            return self._permit_admission_closed

    @property
    def terminally_closed(self) -> bool:
        with self._authority_lock:
            return self._terminally_closed

    @property
    def canonical_lifecycle(self) -> VoiceLifecycle | None:
        with self._authority_lock:
            return self._canonical_lifecycle

    def bind_canonical_lifecycle(
        self,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        """Bind authority checks once to the exact assembly reducer."""
        with self._authority_lock:
            if (
                type(lifecycle) is not VoiceLifecycle
                or lifecycle.binding != self.binding
            ):
                return False
            if self._canonical_lifecycle is None:
                self._canonical_lifecycle = lifecycle
            return self._canonical_lifecycle is lifecycle

    @property
    def admission_revision(self) -> int:
        with self._authority_lock:
            return self._admission_revision

    @property
    def retained_permit_count(self) -> int:
        with self._authority_lock:
            return len(self._permitted | self._retired_permit_keys)

    @property
    def retained_input_turn_count(self) -> int:
        with self._authority_lock:
            return len(self._admitted_input_keys)

    @property
    def configuration_digest(self) -> str:
        provider_configuration = getattr(self, "provider_configuration", None)
        configuration = provider_configuration() if callable(provider_configuration) else {"adapter_only": True}
        material = {
            "domain": "hey-kevin/offline-candidate-configuration/v1",
            "arm": self.arm.value,
            "limits": {
                "output_tokens": self.limits.output_tokens,
                "audio_ms": self.limits.audio_ms,
                "byte_count": self.limits.byte_count,
                "wall_clock_ms": self.limits.wall_clock_ms,
                "cost_minor_units": self.limits.cost_minor_units,
                "request_count": self.limits.request_count,
            },
            "provider_configuration": configuration,
        }
        import hashlib
        import json

        return hashlib.sha256(
            json.dumps(
                material,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def admit_input(self, context: EventContext) -> bool:
        """Bound provider input state before it can allocate reducer records."""
        with self._authority_lock:
            if (
                not isinstance(context, EventContext)
                or context.binding != self.binding
                or self._permit_admission_closed
                or self._terminally_closed
            ):
                return False
            key = self.permit_key(context)
            if key in self._admitted_input_keys:
                return True
            if len(self._admitted_input_keys) >= self.limits.request_count:
                return False
            self._admitted_input_keys.add(key)
            return True

    def revoke_permits_for_disconnect(self) -> None:
        """Revoke active speech authority while preserving bounded tombstones."""
        with self._authority_lock:
            self._retired_permit_keys.update(self._permitted)
            self._disconnect_revoked_permit_keys.update(
                self._permitted
            )
            self._permitted.clear()
            self._confirmed.clear()
            self._final_input_admissions.clear()
            self._final_input_transition_ids.clear()
            self._pending_resume_admission = None
            self._permit_admission_closed = True
            self._admission_revision += 1

    def retire_permit(self, event: VoiceEvent) -> bool:
        """Retire one exact accepted permit while retaining its replay tombstone."""
        with self._authority_lock:
            if not isinstance(event, VoiceEvent) or event.binding != self.binding or event.kind is not VoiceEventKind.RESPONSE_AUTHORIZED or event.source is not VoiceSource.LOCAL_AUTHORITATIVE or self._terminally_closed or self._canonical_lifecycle is None or not self._canonical_lifecycle.recognizes_response_authorization(event):
                return False
            key = (
                event.input_turn_id,
                event.generation_id,
                event.semantic_act_id,
                event.semantic_act_kind,
            )
            if key not in self._permitted:
                return False
            self._permitted.remove(key)
            self._confirmed.discard(key)
            self._retired_permit_keys.add(key)
            self._disconnect_revoked_permit_keys.discard(key)
            return True

    def permit_was_revoked(self, event: VoiceEvent) -> bool:
        """Recognize an exact disconnect-revoked permit without reopening it."""
        with self._authority_lock:
            if (
                not isinstance(event, VoiceEvent)
                or event.binding != self.binding
                or event.kind is not VoiceEventKind.RESPONSE_AUTHORIZED
                or event.source is not VoiceSource.LOCAL_AUTHORITATIVE
                or self._terminally_closed
                or self._canonical_lifecycle is None
                or not self._canonical_lifecycle.recognizes_response_authorization(
                    event
                )
            ):
                return False
            key = (
                event.input_turn_id,
                event.generation_id,
                event.semantic_act_id,
                event.semantic_act_kind,
            )
            return (
                key in self._disconnect_revoked_permit_keys
                and key in self._retired_permit_keys
                and key not in self._permitted
            )

    def _accept_resume_transition(
        self,
        event: VoiceEvent,
    ) -> AdapterResult:
        """Stage one exact resume event without reopening authority."""
        with self._authority_lock:
            if (
                not isinstance(event, VoiceEvent)
                or event.binding != self.binding
                or event.kind is not VoiceEventKind.SESSION_RESUMED
                or event.source is not VoiceSource.PROVIDER_UNTRUSTED
                or not self._permit_admission_closed
                or self._terminally_closed
                or self._pending_resume_admission is not None
            ):
                return self.rejected(AdapterRejectReason.OUT_OF_ORDER)
            result = AdapterResult(True, events=(event,))
            self._pending_resume_admission = (
                result,
                event,
                self._admission_revision,
            )
            return result

    def resume_permit_admission(
        self,
        *,
        result: AdapterResult,
        event: VoiceEvent,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        """Consume an exact canonical resume receipt before reopening."""
        with self._authority_lock:
            record = self._pending_resume_admission
            if (
                record is None
                or record[0] is not result
                or record[1] is not event
            ):
                return False
            self._pending_resume_admission = None
            if (
                self._terminally_closed
                or record[2] != self._admission_revision
                or not isinstance(lifecycle, VoiceLifecycle)
                or lifecycle.binding != self.binding
                or lifecycle is not self._canonical_lifecycle
                or not lifecycle.accepts_session_resume(event)
            ):
                return False
            self._permit_admission_closed = False
            self._admission_revision += 1
            return True

    def _retire_permit_admission_for_fresh_epoch(self) -> None:
        """Retire authority while allowing the final re-established event."""
        with self._authority_lock:
            self._permitted.clear()
            self._confirmed.clear()
            self._retired_permit_keys.clear()
            self._disconnect_revoked_permit_keys.clear()
            self._admitted_input_keys.clear()
            self._final_input_admissions.clear()
            self._final_input_transition_ids.clear()
            self._pending_resume_admission = None
            self._usage.clear()
            self._failed.clear()
            self._permit_admission_closed = True
            self._admission_revision += 1

    def terminalize_permit_admission(self) -> None:
        """Close an old adapter permanently after ambiguous execution."""
        with self._authority_lock:
            self._retire_permit_admission_for_fresh_epoch()
            self._terminally_closed = True

    def accept_semantic_confirmation(
        self,
        event: VoiceEvent,
        *,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        with self._authority_lock:
            if not self.can_accept_semantic_confirmation(
                event,
                lifecycle=lifecycle,
            ):
                return False
            key = (
                event.input_turn_id,
                event.generation_id,
                event.semantic_act_id,
                event.semantic_act_kind,
            )
            self._confirmed.add(key)
            return True

    def can_accept_semantic_confirmation(
        self,
        event: VoiceEvent,
        *,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        with self._authority_lock:
            if not isinstance(event, VoiceEvent) or not isinstance(lifecycle, VoiceLifecycle) or lifecycle.binding != self.binding or (self._canonical_lifecycle is not None and lifecycle is not self._canonical_lifecycle) or not lifecycle.accepts_semantic_confirmation(event) or self._terminally_closed:
                return False
            key = (
                event.input_turn_id,
                event.generation_id,
                event.semantic_act_id,
                event.semantic_act_kind,
            )
            return key in self._permitted and key not in self._confirmed and key not in self._failed

    def permitted(self, context: EventContext) -> bool:
        with self._authority_lock:
            return (
                isinstance(context, EventContext)
                and context.binding == self.binding
                and not self._terminally_closed
                and self.permit_key(context) in self._permitted
            )

    def preflight(
        self,
        *,
        context: EventContext,
        usage: CandidateUsage,
        permit_required: bool,
        count_request: bool = False,
    ) -> AdapterResult | None:
        with self._authority_lock:
            return self._preflight_locked(
                context=context,
                usage=usage,
                permit_required=permit_required,
                count_request=count_request,
            )

    def _preflight_locked(
        self,
        *,
        context: EventContext,
        usage: CandidateUsage,
        permit_required: bool,
        count_request: bool,
    ) -> AdapterResult | None:
        if self._terminally_closed:
            return AdapterResult(
                False,
                reason=AdapterRejectReason.STALE_EPOCH,
            )
        if context.binding != self.binding:
            reason = AdapterRejectReason.STALE_EPOCH if context.binding.epoch != self.binding.epoch else AdapterRejectReason.BINDING_MISMATCH
            return AdapterResult(False, reason=reason)
        key = self.permit_key(context)
        if key in self._failed:
            return AdapterResult(False, reason=AdapterRejectReason.OUT_OF_ORDER)
        if permit_required and key not in self._permitted:
            return AdapterResult(False, reason=AdapterRejectReason.PERMIT_REQUIRED)
        track_usage = key in self._permitted or key in self._admitted_input_keys or key in self._usage
        prior_usage = self._usage.get(key) if track_usage else None
        if prior_usage is not None and usage.regresses(prior_usage):
            return AdapterResult(False, reason=AdapterRejectReason.OUT_OF_ORDER)
        next_request_count = self._request_count + (1 if count_request else 0)
        if count_request and next_request_count >= self.limits.request_count:
            return (
                self.fail(
                    context,
                    reason=AdapterRejectReason.LIMIT_EXCEEDED,
                )
                if track_usage
                else AdapterResult(
                    False,
                    reason=AdapterRejectReason.LIMIT_EXCEEDED,
                )
            )
        if usage.exceeds(self.limits):
            return (
                self.fail(
                    context,
                    reason=AdapterRejectReason.LIMIT_EXCEEDED,
                )
                if track_usage
                else AdapterResult(
                    False,
                    reason=AdapterRejectReason.LIMIT_EXCEEDED,
                )
            )
        if track_usage:
            self._usage[key] = usage
        self._request_count = next_request_count
        return None

    @property
    def request_count(self) -> int:
        with self._authority_lock:
            return self._request_count

    def fail(
        self,
        context: EventContext,
        *,
        reason: AdapterRejectReason,
        kind: VoiceEventKind = VoiceEventKind.ACT_FAILED,
    ) -> AdapterResult:
        with self._authority_lock:
            key = self.permit_key(context)
            self._failed.add(key)
            failure = context.event(
                kind,
                source=VoiceSource.LOCAL_AUTHORITATIVE,
            )
            return AdapterResult(
                False,
                events=(failure,) if key in self._permitted else (),
                reason=reason,
            )

    def accepted(self, event: VoiceEvent) -> AdapterResult:
        if (
            not isinstance(event, VoiceEvent)
            or event.kind is VoiceEventKind.INPUT_TURN_FINAL
        ):
            raise ValueError(
                "final input must be emitted by the adapter transition"
            )
        return AdapterResult(True, events=(event,))

    def _accept_final_transition(self, event: VoiceEvent) -> AdapterResult:
        """Create an opaque proof while completing a concrete final transition."""
        with self._authority_lock:
            if (
                not isinstance(event, VoiceEvent)
                or event.binding != self.binding
                or event.kind is not VoiceEventKind.INPUT_TURN_FINAL
                or event.source is not VoiceSource.PROVIDER_UNTRUSTED
                or self._permit_admission_closed
                or self._terminally_closed
                or event.input_turn_id
                not in getattr(self, "_final_turns", set())
                or event.input_turn_id
                in self._final_input_transition_ids
            ):
                raise ValueError("adapter final-input transition is invalid")
            self._final_input_transition_ids.add(event.input_turn_id)
            admission = _AdapterFinalInputAdmission(marker=object())
            self._final_input_admissions[id(admission)] = (
                admission,
                event,
                self._admission_revision,
            )
            return AdapterResult(
                True,
                events=(event,),
                final_input_admission=admission,
            )

    def consume_final_input_admission(
        self,
        result: AdapterResult,
        event: VoiceEvent,
    ) -> bool:
        """Atomically consume the exact one-use capability carried by a result."""
        with self._authority_lock:
            if (
                not isinstance(result, AdapterResult)
                or not result.accepted
                or result.events != (event,)
                or result.final_input_admission is None
                or self._permit_admission_closed
                or self._terminally_closed
            ):
                return False
            admission = result.final_input_admission
            record = self._final_input_admissions.pop(id(admission), None)
            return (
                record is not None
                and record[0] is admission
                and record[1] is event
                and record[2] == self._admission_revision
            )

    def has_permit(self, event: VoiceEvent) -> bool:
        """Report whether the exact canonical response permit remains usable."""
        with self._authority_lock:
            if (
                not isinstance(event, VoiceEvent)
                or event.binding != self.binding
                or event.kind is not VoiceEventKind.RESPONSE_AUTHORIZED
                or self._permit_admission_closed
                or self._terminally_closed
                or self._canonical_lifecycle is None
                or not self._canonical_lifecycle.recognizes_response_authorization(
                    event
                )
            ):
                return False
            key = (
                event.input_turn_id,
                event.generation_id,
                event.semantic_act_id,
                event.semantic_act_kind,
            )
            return key in self._permitted and key not in self._failed

    @staticmethod
    def rejected(reason: AdapterRejectReason) -> AdapterResult:
        return AdapterResult(False, reason=reason)


__all__ = [
    "AdapterRejectReason",
    "AdapterResult",
    "CandidateArm",
    "CandidateLimits",
    "CandidateUsage",
    "EventContext",
    "OfflineCandidateAdapter",
]
