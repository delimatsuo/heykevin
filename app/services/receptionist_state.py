"""Call-scoped receptionist state for live and replayed intake."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
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


SERVICE_OBJECT_TERMS = (
    "toilet",
    "sink",
    "faucet",
    "water heater",
    "dishwasher",
    "garbage disposal",
    "shower",
    "tub",
    "drain",
    "pipe",
)

SERVICE_ACTION_PATTERNS: tuple[tuple[ServiceAction, tuple[str, ...]], ...] = (
    (ServiceAction.REPLACE, ("replace", "replacement", "swap out", "upgrade", "reemplazar")),
    (ServiceAction.REPAIR, ("repair", "fix", "leak", "broken", "not working")),
    (ServiceAction.INSTALL, ("install", "installation", "new installation", "put in")),
    (ServiceAction.INSPECT, ("inspect", "look at", "diagnose", "check out")),
    (ServiceAction.QUOTE, ("quote", "estimate", "pricing", "price", "how much", "cost")),
    (ServiceAction.MAINTAIN, ("maintain", "maintenance", "service tune")),
)

CALLBACK_REQUEST_PATTERNS = (
    "call me back",
    "call back",
    "get back to me",
    "reach me",
    "return my call",
)

SCHEDULING_PATTERNS = (
    "schedule",
    "appointment",
    "book",
    "come out",
    "send someone",
)

EMERGENCY_PATTERNS = (
    "emergency",
    "flood",
    "flooding",
    "burst pipe",
    "gas leak",
    "sewage",
    "sparking",
    "smoke",
    "burning smell",
)

SPANISH_PATTERNS = (
    "hola",
    "precio",
    "bano",
    "numero",
    "correcto",
    "llamar",
)

CALLBACK_REJECTION_PATTERNS = (
    "not the right number",
    "wrong number",
    "different number",
    "no es el numero correcto",
)


def phone_last_four(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits[-4:] if len(digits) >= 4 else ""


def _contains_any(text: str, patterns: Iterable[str]) -> bool:
    normalized = text.lower()
    return any(pattern in normalized for pattern in patterns)


def _extract_service_object(text: str) -> str:
    normalized = text.lower()
    for term in SERVICE_OBJECT_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", normalized):
            return term
    return ""


def _extract_service_action(text: str) -> ServiceAction:
    normalized = text.lower()
    for action, patterns in SERVICE_ACTION_PATTERNS:
        if any(pattern in normalized for pattern in patterns):
            return action
    return ServiceAction.UNKNOWN


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

    def observe_caller_turn(self, text: str) -> None:
        normalized = text.lower()
        self.phase = IntakePhase.UNDERSTAND_REQUEST

        if self.caller_identity.name and self.caller_identity.name.lower().split()[0] in normalized:
            self.caller_identity.confirmed = True

        if _contains_any(normalized, SPANISH_PATTERNS):
            self.language = "es"

        if _contains_any(normalized, EMERGENCY_PATTERNS):
            self.intent = Intent.EMERGENCY
            self.urgency = Urgency.EMERGENCY
            self._remember("urgency:emergency")
        elif _contains_any(normalized, SCHEDULING_PATTERNS):
            self.intent = Intent.SCHEDULING
            self.address_need = AddressNeed.MAYBE_LATER
        elif _contains_any(normalized, CALLBACK_REQUEST_PATTERNS):
            self.intent = Intent.CALLBACK
        elif any(term in normalized for term in ("how much", "cost", "price", "pricing", "estimate", "quote")):
            self.intent = Intent.PRICING_QUESTION
        elif _extract_service_object(normalized) or _extract_service_action(normalized) != ServiceAction.UNKNOWN:
            self.intent = Intent.SERVICE_REQUEST

        if _contains_any(normalized, CALLBACK_REQUEST_PATTERNS):
            self.callback_intent = CallbackIntent.REQUESTED
            self._remember("callback_intent:requested")
        if self.callback_intent in {CallbackIntent.REQUESTED, CallbackIntent.ACCEPTED} and _contains_any(
            normalized,
            CALLBACK_REJECTION_PATTERNS,
        ):
            self.callback_confirmation = CallbackConfirmation.REJECTED
            self._remember("callback_confirmation:rejected")

        service_object = _extract_service_object(normalized)
        if service_object:
            self.service_object = service_object
            self._remember(f"service_object:{service_object}")

        service_action = _extract_service_action(normalized)
        if service_action != ServiceAction.UNKNOWN:
            self.service_action = service_action
            self._remember(f"service_action:{service_action.value}")

    def mark_slot_asked(self, slot: str) -> None:
        if slot:
            self.asked_slots.add(slot)

    def _remember(self, fact: str) -> None:
        if fact not in self.known_facts:
            self.known_facts.append(fact)

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
            callback_intent=CallbackIntent(data.get("callback_intent") or CallbackIntent.NONE.value),
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
