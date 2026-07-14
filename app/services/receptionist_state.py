"""Call-scoped receptionist state for live and replayed intake."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class IntakePhase(str, Enum):
    GREETING = "greeting"
    UNDERSTAND_REQUEST = "understand_request"
    ANSWER_QUESTION = "answer_question"
    CLARIFY_SCOPE = "clarify_scope"
    COLLECT_INTAKE = "collect_intake"
    OFFER_FOLLOWUP = "offer_followup"
    SCHEDULE_OR_CALLBACK = "schedule_or_callback"
    HANDOFF = "handoff"
    WRAP_UP = "wrap_up"


class BusinessScope(str, Enum):
    IN_SCOPE = "in_scope"
    OUT_OF_SCOPE = "out_of_scope"
    UNCLEAR = "unclear"


class Intent(str, Enum):
    UNKNOWN = "unknown"
    PRICING_QUESTION = "pricing_question"
    SERVICE_REQUEST = "service_request"
    EMERGENCY = "emergency"
    PERSONAL_CALL = "personal_call"
    SALES_CALL = "sales_call"
    MESSAGE = "message"
    SCHEDULING = "scheduling"
    CALLBACK = "callback"


class ServiceAction(str, Enum):
    UNKNOWN = "unknown"
    REPAIR = "repair"
    REPLACE = "replace"
    INSTALL = "install"
    INSPECT = "inspect"
    QUOTE = "quote"
    MAINTAIN = "maintain"


class Urgency(str, Enum):
    UNKNOWN = "unknown"
    ROUTINE = "routine"
    URGENT = "urgent"
    EMERGENCY = "emergency"


class CallbackIntent(str, Enum):
    NONE = "none"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    REQUESTED = "requested"
    DECLINED = "declined"


class CallbackConfirmation(str, Enum):
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class AddressNeed(str, Enum):
    NONE = "none"
    MAYBE_LATER = "maybe_later"
    REQUIRED_NOW = "required_now"
    ALREADY_KNOWN = "already_known"
    CONFIRMED = "confirmed"


@dataclass
class CallerIdentity:
    name: str = ""
    confidence: float = 0.0
    source: str = ""
    confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "confidence": self.confidence,
            "source": self.source,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CallerIdentity":
        data = data or {}
        return cls(
            name=str(data.get("name") or ""),
            confidence=float(data.get("confidence") or 0.0),
            source=str(data.get("source") or ""),
            confirmed=bool(data.get("confirmed") or False),
        )


@dataclass(frozen=True)
class CallerObservation:
    """Provider-neutral facts extracted from one caller turn."""

    language: str | None = None
    identity_confirmed: bool | None = None
    business_scope: BusinessScope | None = None
    business_scope_reason: str | None = None
    intent: Intent | None = None
    service_object: str | None = None
    service_action: ServiceAction | None = None
    urgency: Urgency | None = None
    callback_intent: CallbackIntent | None = None
    callback_confirmation: CallbackConfirmation | None = None
    address_need: AddressNeed | None = None

    def __post_init__(self) -> None:
        if self.identity_confirmed is not None and not isinstance(self.identity_confirmed, bool):
            raise TypeError("identity_confirmed must be a boolean")
        enum_fields = (
            (self.business_scope, BusinessScope),
            (self.intent, Intent),
            (self.service_action, ServiceAction),
            (self.urgency, Urgency),
            (self.callback_intent, CallbackIntent),
            (self.callback_confirmation, CallbackConfirmation),
            (self.address_need, AddressNeed),
        )
        if any(
            value is not None and not isinstance(value, enum_type)
            for value, enum_type in enum_fields
        ):
            raise TypeError("caller observation enums must use controller enum values")
        if self.language is not None:
            object.__setattr__(self, "language", _normalize_language_code(self.language))
        if self.service_object is not None:
            object.__setattr__(
                self,
                "service_object",
                _normalize_observation_text(self.service_object, max_length=80),
            )
        if self.business_scope_reason is not None:
            object.__setattr__(
                self,
                "business_scope_reason",
                _normalize_observation_text(self.business_scope_reason, max_length=160),
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallerObservation":
        allowed_fields = {
            "language",
            "identity_confirmed",
            "business_scope",
            "business_scope_reason",
            "intent",
            "service_object",
            "service_action",
            "urgency",
            "callback_intent",
            "callback_confirmation",
            "address_need",
        }
        if set(data) - allowed_fields:
            raise ValueError("unknown caller observation field")

        identity_confirmed = data.get("identity_confirmed")
        if identity_confirmed is not None and not isinstance(identity_confirmed, bool):
            raise TypeError("identity_confirmed must be a boolean")

        return cls(
            language=_optional_observation_text(data, "language"),
            identity_confirmed=identity_confirmed,
            business_scope=(
                BusinessScope(data["business_scope"])
                if data.get("business_scope") is not None
                else None
            ),
            business_scope_reason=_optional_observation_text(data, "business_scope_reason"),
            intent=Intent(data["intent"]) if data.get("intent") is not None else None,
            service_object=_optional_observation_text(data, "service_object"),
            service_action=(
                ServiceAction(data["service_action"])
                if data.get("service_action") is not None
                else None
            ),
            urgency=Urgency(data["urgency"]) if data.get("urgency") is not None else None,
            callback_intent=(
                CallbackIntent(data["callback_intent"])
                if data.get("callback_intent") is not None
                else None
            ),
            callback_confirmation=(
                CallbackConfirmation(data["callback_confirmation"])
                if data.get("callback_confirmation") is not None
                else None
            ),
            address_need=(
                AddressNeed(data["address_need"]) if data.get("address_need") is not None else None
            ),
        )


def _optional_observation_text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _normalize_language_code(value: str) -> str:
    code = value.strip()
    parts = code.split("-")
    if (
        not code
        or len(code) > 35
        or any(not part or len(part) > 8 or not part.isalnum() for part in parts)
    ):
        raise ValueError("language must be a bounded language tag")
    return code


def _normalize_observation_text(value: str, *, max_length: int) -> str:
    normalized = value.strip()
    if len(normalized) > max_length or any(ord(character) < 32 for character in normalized):
        raise ValueError("observation text is invalid")
    return normalized


def phone_last_four(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else ""


@dataclass
class IntakeState:
    call_sid: str = ""
    phase: IntakePhase = IntakePhase.GREETING
    caller_identity: CallerIdentity = field(default_factory=CallerIdentity)
    caller_phone_last_four: str = ""
    business_scope: BusinessScope = BusinessScope.UNCLEAR
    business_scope_reason: str = ""
    intent: Intent = Intent.UNKNOWN
    service_object: str = ""
    service_action: ServiceAction = ServiceAction.UNKNOWN
    urgency: Urgency = Urgency.UNKNOWN
    known_facts: list[str] = field(default_factory=list)
    asked_slots: set[str] = field(default_factory=set)
    callback_intent: CallbackIntent = CallbackIntent.NONE
    callback_confirmation: CallbackConfirmation = CallbackConfirmation.UNKNOWN
    address_need: AddressNeed = AddressNeed.NONE
    memory_refs_used: set[str] = field(default_factory=set)
    side_effects_allowed: bool = False
    language: str = "unknown"

    @classmethod
    def new(
        cls,
        *,
        call_sid: str,
        caller_phone: str = "",
        caller_name: str = "",
        caller_source: str = "",
        caller_confidence: float = 0.0,
        memory_refs_used: Iterable[str] = (),
    ) -> "IntakeState":
        return cls(
            call_sid=call_sid,
            caller_identity=CallerIdentity(
                name=caller_name,
                confidence=caller_confidence,
                source=caller_source,
                confirmed=bool(caller_name and caller_confidence >= 0.8),
            ),
            caller_phone_last_four=phone_last_four(caller_phone),
            memory_refs_used=set(memory_refs_used),
        )

    def apply_caller_observation(self, observation: CallerObservation) -> None:
        self.phase = IntakePhase.UNDERSTAND_REQUEST

        if observation.identity_confirmed is not None:
            self.caller_identity.confirmed = observation.identity_confirmed
        if observation.language is not None:
            self.language = observation.language.strip() or "unknown"
        if observation.business_scope is not None:
            self.business_scope = observation.business_scope
        if observation.business_scope_reason is not None:
            self.business_scope_reason = observation.business_scope_reason.strip()
        if observation.intent is not None:
            self.intent = observation.intent
        if observation.address_need is not None:
            self.address_need = observation.address_need

        if observation.service_object is not None:
            self.service_object = observation.service_object.strip()
            self._replace_known_slot("service_object", self.service_object)
        if observation.service_action is not None:
            self.service_action = observation.service_action
            self._replace_known_slot(
                "service_action",
                ""
                if observation.service_action == ServiceAction.UNKNOWN
                else observation.service_action.value,
            )
        if observation.urgency is not None:
            self.urgency = observation.urgency
            self._replace_known_slot(
                "urgency",
                "" if observation.urgency == Urgency.UNKNOWN else observation.urgency.value,
            )
        if observation.callback_intent is not None:
            self.callback_intent = observation.callback_intent
            self._replace_known_slot(
                "callback_intent",
                ""
                if observation.callback_intent == CallbackIntent.NONE
                else observation.callback_intent.value,
            )
        if observation.callback_confirmation is not None:
            self.callback_confirmation = observation.callback_confirmation
            self._replace_known_slot(
                "callback_confirmation",
                ""
                if observation.callback_confirmation == CallbackConfirmation.UNKNOWN
                else observation.callback_confirmation.value,
            )

    def mark_slot_asked(self, slot: str) -> None:
        if slot:
            self.asked_slots.add(slot)

    def _replace_known_slot(self, slot: str, value: str) -> None:
        prefix = f"{slot}:"
        self.known_facts = [fact for fact in self.known_facts if not fact.startswith(prefix)]
        if value:
            self.known_facts.append(f"{prefix}{value}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_sid": self.call_sid,
            "phase": self.phase.value,
            "caller_identity": self.caller_identity.to_dict(),
            "caller_phone_last_four": self.caller_phone_last_four,
            "business_scope": self.business_scope.value,
            "business_scope_reason": self.business_scope_reason,
            "intent": self.intent.value,
            "service_object": self.service_object,
            "service_action": self.service_action.value,
            "urgency": self.urgency.value,
            "known_facts": list(self.known_facts),
            "asked_slots": sorted(self.asked_slots),
            "callback_intent": self.callback_intent.value,
            "callback_confirmation": self.callback_confirmation.value,
            "address_need": self.address_need.value,
            "memory_refs_used": sorted(self.memory_refs_used),
            "side_effects_allowed": self.side_effects_allowed,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IntakeState":
        return cls(
            call_sid=str(data.get("call_sid") or ""),
            phase=IntakePhase(data.get("phase") or IntakePhase.GREETING.value),
            caller_identity=CallerIdentity.from_dict(data.get("caller_identity")),
            caller_phone_last_four=str(data.get("caller_phone_last_four") or ""),
            business_scope=BusinessScope(data.get("business_scope") or BusinessScope.UNCLEAR.value),
            business_scope_reason=str(data.get("business_scope_reason") or ""),
            intent=Intent(data.get("intent") or Intent.UNKNOWN.value),
            service_object=str(data.get("service_object") or ""),
            service_action=ServiceAction(data.get("service_action") or ServiceAction.UNKNOWN.value),
            urgency=Urgency(data.get("urgency") or Urgency.UNKNOWN.value),
            known_facts=list(data.get("known_facts") or []),
            asked_slots=set(data.get("asked_slots") or []),
            callback_intent=CallbackIntent(
                data.get("callback_intent") or CallbackIntent.NONE.value
            ),
            callback_confirmation=CallbackConfirmation(
                data.get("callback_confirmation") or CallbackConfirmation.UNKNOWN.value
            ),
            address_need=AddressNeed(data.get("address_need") or AddressNeed.NONE.value),
            memory_refs_used=set(data.get("memory_refs_used") or []),
            side_effects_allowed=bool(data.get("side_effects_allowed") or False),
            language=str(data.get("language") or "unknown"),
        )

    def redacted_log_dict(self) -> dict[str, Any]:
        return {
            "call_sid": self.call_sid,
            "phase": self.phase.value,
            "caller_phone_last_four": self.caller_phone_last_four,
            "intent": self.intent.value,
            "service_object_present": bool(self.service_object),
            "service_action": self.service_action.value,
            "urgency": self.urgency.value,
            "asked_slots": sorted(self.asked_slots),
            "callback_intent": self.callback_intent.value,
            "callback_confirmation": self.callback_confirmation.value,
            "address_need": self.address_need.value,
            "known_fact_count": len(self.known_facts),
            "memory_ref_count": len(self.memory_refs_used),
            "side_effects_allowed": self.side_effects_allowed,
            "language": self.language,
        }
