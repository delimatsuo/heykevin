"""Provider-SDK-free contracts for offline voice candidate adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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
        return (
            self.output_tokens >= limits.output_tokens
            or self.audio_ms >= limits.audio_ms
            or self.byte_count >= limits.byte_count
            or self.wall_clock_ms >= limits.wall_clock_ms
            or self.cost_minor_units >= limits.cost_minor_units
        )

    def regresses(self, prior: CandidateUsage) -> bool:
        return (
            self.output_tokens < prior.output_tokens
            or self.audio_ms < prior.audio_ms
            or self.byte_count < prior.byte_count
            or self.wall_clock_ms < prior.wall_clock_ms
            or self.cost_minor_units < prior.cost_minor_units
        )


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
class AdapterResult:
    accepted: bool
    events: tuple[VoiceEvent, ...] = ()
    timeout_intents: tuple[VoiceTimeoutIntent, ...] = ()
    reason: AdapterRejectReason | None = None

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise ValueError("adapter result acceptance is invalid")
        if any(not isinstance(event, VoiceEvent) for event in self.events):
            raise ValueError("adapter result events are invalid")
        if any(
            not isinstance(intent, VoiceTimeoutIntent)
            for intent in self.timeout_intents
        ):
            raise ValueError("adapter result timeout intents are invalid")
        if self.accepted == (self.reason is not None):
            raise ValueError("adapter result reason is inconsistent")


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
        self._permitted: set[
            tuple[str, str, str, VoiceSemanticActKind]
        ] = set()
        self._confirmed: set[
            tuple[str, str, str, VoiceSemanticActKind]
        ] = set()
        self._failed: set[
            tuple[str, str, str, VoiceSemanticActKind]
        ] = set()
        self._retired_permit_keys: set[
            tuple[str, str, str, VoiceSemanticActKind]
        ] = set()
        self._admitted_input_keys: set[
            tuple[str, str, str, VoiceSemanticActKind]
        ] = set()
        self._permit_admission_closed = False
        self._usage: dict[
            tuple[str, str, str, VoiceSemanticActKind], CandidateUsage
        ] = {}
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
        if (
            not isinstance(event, VoiceEvent)
            or not isinstance(lifecycle, VoiceLifecycle)
            or lifecycle.binding != self.binding
            or event.binding != self.binding
            or event.kind is not VoiceEventKind.RESPONSE_AUTHORIZED
            or event.source is not VoiceSource.LOCAL_AUTHORITATIVE
            or not lifecycle.accepts_response_authorization(event)
            or self._permit_admission_closed
        ):
            return False
        key = (
            event.input_turn_id,
            event.generation_id,
            event.semantic_act_id,
            event.semantic_act_kind,
        )
        if (
            key in self._permitted
            or key in self._failed
            or key in self._retired_permit_keys
            or len(self._permitted | self._retired_permit_keys)
            >= self.limits.request_count
        ):
            return False
        self._permitted.add(key)
        return True

    @property
    def permit_admission_closed(self) -> bool:
        return self._permit_admission_closed

    @property
    def retained_permit_count(self) -> int:
        return len(self._permitted | self._retired_permit_keys)

    @property
    def retained_input_turn_count(self) -> int:
        return len(self._admitted_input_keys)

    def admit_input(self, context: EventContext) -> bool:
        """Bound provider input state before it can allocate reducer records."""
        if not isinstance(context, EventContext) or context.binding != self.binding:
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
        self._retired_permit_keys.update(self._permitted)
        self._permitted.clear()
        self._confirmed.clear()
        self._permit_admission_closed = True

    def resume_permit_admission(self) -> None:
        """Reopen admission after an adapter-specific valid resume event."""
        self._permit_admission_closed = False

    def terminalize_permit_admission(self) -> None:
        """Close an old adapter permanently when a fresh epoch is required."""
        self._permitted.clear()
        self._confirmed.clear()
        self._retired_permit_keys.clear()
        self._admitted_input_keys.clear()
        self._usage.clear()
        self._failed.clear()
        self._permit_admission_closed = True

    def accept_semantic_confirmation(
        self,
        event: VoiceEvent,
        *,
        lifecycle: VoiceLifecycle,
    ) -> bool:
        if (
            not isinstance(event, VoiceEvent)
            or not isinstance(lifecycle, VoiceLifecycle)
            or lifecycle.binding != self.binding
            or not lifecycle.accepts_semantic_confirmation(event)
        ):
            return False
        key = (
            event.input_turn_id,
            event.generation_id,
            event.semantic_act_id,
            event.semantic_act_kind,
        )
        if key not in self._permitted or key in self._confirmed or key in self._failed:
            return False
        self._confirmed.add(key)
        return True

    def permitted(self, context: EventContext) -> bool:
        return context.binding == self.binding and self.permit_key(context) in self._permitted

    def preflight(
        self,
        *,
        context: EventContext,
        usage: CandidateUsage,
        permit_required: bool,
        count_request: bool = False,
    ) -> AdapterResult | None:
        if context.binding != self.binding:
            reason = (
                AdapterRejectReason.STALE_EPOCH
                if context.binding.epoch != self.binding.epoch
                else AdapterRejectReason.BINDING_MISMATCH
            )
            return AdapterResult(False, reason=reason)
        key = self.permit_key(context)
        if key in self._failed:
            return AdapterResult(False, reason=AdapterRejectReason.OUT_OF_ORDER)
        if permit_required and key not in self._permitted:
            return AdapterResult(False, reason=AdapterRejectReason.PERMIT_REQUIRED)
        track_usage = (
            key in self._permitted
            or key in self._admitted_input_keys
            or key in self._usage
        )
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
        return self._request_count

    def fail(
        self,
        context: EventContext,
        *,
        reason: AdapterRejectReason,
        kind: VoiceEventKind = VoiceEventKind.ACT_FAILED,
    ) -> AdapterResult:
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

    @staticmethod
    def accepted(event: VoiceEvent) -> AdapterResult:
        return AdapterResult(True, events=(event,))

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
