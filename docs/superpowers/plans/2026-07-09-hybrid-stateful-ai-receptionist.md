# Hybrid Stateful AI Receptionist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first explicit receptionist state, planner, instruction composer, and replay-eval skeleton so Kevin can define and verify offline policies for repeated-question prevention, callback gating, and address gating without adding more prompt exceptions.

**Architecture:** This phase is offline-first. It creates Hey Kevin-owned `IntakeState`, `DialoguePlanner`, `InstructionComposer`, and replay helpers with unit coverage, but does not change live Gemini or ElevenLabs call behavior. Passing this plan proves the controller policy is testable; it does not prove live caller behavior is fixed until a later live-wiring plan passes replay, simulation, staging calls, and latency checks.

**Tech Stack:** Python 3.12, dataclasses, enums, pytest, existing `uv run --python 3.12 --with '.[dev]'` test workflow.

---

## Pre-Execution Gates

- [ ] **Resolve branch and PR ownership before code work**

Decision recorded on 2026-07-09: use a new branch/worktree from `origin/main` for this controller plan. PR #76 remains the read-only Jobber customer-memory slice and must not receive controller implementation commits.

The selected path is:

1. **New branch/worktree:** keep PR #76 as the read-only Jobber customer-memory slice, then create a new controller branch/worktree for this plan.

Do not push controller implementation commits to `codex/jobber-customer-memory`. This plan currently lives in the PR #76 worktree for review convenience only and should travel with the new controller branch.

- [ ] **Accept offline-only success criteria**

Success for this plan means:

- new controller modules and replay tests define the desired receptionist policy;
- no live Gemini or ElevenLabs behavior is wired to the controller;
- no staging or production behavior changes;
- no claim that the repeated-question bug is fixed live.

- [ ] **Apply fixture privacy policy**

All tests, fixtures, docs, and examples in this plan must use synthetic or redacted sentinel values only. Do not copy raw staging transcripts, full phone numbers, OAuth callback codes, admin bearer tokens, Jobber tokens, CRM URLs, CRM object IDs, or private source labels into fixtures. Last-four caller references are allowed only as values like `8667`.

## Product Acceptance

Before live wiring in a later plan, replay output must be reviewed as plain caller/Kevin conversation text. For this offline slice, each replay fixture should assert the policy that a caller would experience:

- Known caller toilet replacement: Kevin answers pricing/scope briefly, asks one useful next detail, and does not ask whether the caller means repair/replacement/install.
- Pricing-only/no callback: Kevin answers the question and does not ask for callback number or address.
- Callback rejected: if the caller says the caller-ID number is wrong, Kevin asks for the best callback number once and confirms only the last four.
- Blocked caller ID: Kevin waits for explicit callback intent before asking for a phone number.
- Mixed-language input: Kevin preserves language intent in state and does not force English-only policy.
- Exact-quote refusal: Kevin does not promise an exact quote when the business needs inspection or owner follow-up.
- No private source disclosure: Kevin never says Jobber, CRM source labels, tokens, raw notes, or full phone numbers.

---

## File Structure

- Create `app/services/receptionist_state.py`: serializable call-scoped intake state, redacted phone handling, deterministic caller-turn fact extraction, asked-slot tracking.
- Create `app/services/dialogue_planner.py`: pure planner that converts `IntakeState` into a `NextAction` with allowed and forbidden slots.
- Create `app/services/instruction_composer.py`: compact per-turn instruction composer generated from state and planner output.
- Create `app/services/receptionist_replay.py`: replay helper for transcript/eval fixtures.
- Create `tests/unit/test_receptionist_state.py`: tests state serialization, redaction, fact extraction, and asked-slot persistence.
- Create `tests/unit/test_dialogue_planner.py`: tests duplicate-slot, callback, address, and known-memory gating.
- Create `tests/unit/test_instruction_composer.py`: tests compact instructions and sensitive-data exclusion.
- Create `tests/unit/test_receptionist_replay.py`: tests the known staging failure fixture.
- Create `tests/fixtures/receptionist_replays/known_caller_toilet_replacement.json`: redacted replay scenario from the staging repeated-question failure.

---

### Task 1: Add Serializable Intake State

**Files:**
- Create: `app/services/receptionist_state.py`
- Create: `tests/unit/test_receptionist_state.py`

- [ ] **Step 1: Write failing tests for intake state**

Create `tests/unit/test_receptionist_state.py`:

```python
"""Receptionist call-state memory behavior."""

import json

from app.services.receptionist_state import (
    AddressNeed,
    CallbackConfirmation,
    CallbackIntent,
    IntakePhase,
    IntakeState,
    Intent,
    ServiceAction,
)


def test_intake_state_extracts_known_service_facts_and_redacts_phone():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan",
        caller_source="customer_memory",
        caller_confidence=0.92,
        memory_refs_used=("scoped-memory-ref-1",),
    )

    state.observe_caller_turn("Hi, this is Jonathan. I wanted to know how much to replace a toilet.")

    assert state.phase == IntakePhase.UNDERSTAND_REQUEST
    assert state.intent == Intent.PRICING_QUESTION
    assert state.service_object == "toilet"
    assert state.service_action == ServiceAction.REPLACE
    assert state.caller_identity.name == "Jonathan"
    assert state.caller_identity.confirmed is True
    assert state.caller_phone_last_four == "8667"
    assert "service_object:toilet" in state.known_facts
    assert "service_action:replace" in state.known_facts

    exported = state.to_dict()
    serialized = json.dumps(exported)
    assert "caller-id-ending-8667" not in serialized
    assert "caller-full-phone" not in serialized
    assert "8667" in serialized

    restored = IntakeState.from_dict(exported)
    assert restored.service_action == ServiceAction.REPLACE
    assert restored.service_object == "toilet"
    assert restored.memory_refs_used == {"scoped-memory-ref-1"}


def test_intake_state_tracks_callback_and_scheduling_intent():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")

    state.observe_caller_turn("Could someone call me back later today to schedule this?")

    assert state.intent == Intent.SCHEDULING
    assert state.callback_intent == CallbackIntent.REQUESTED
    assert state.address_need == AddressNeed.MAYBE_LATER
    assert "callback_intent:requested" in state.known_facts


def test_intake_state_tracks_callback_rejection_and_language():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.callback_intent = CallbackIntent.REQUESTED

    state.observe_caller_turn("No, ese no es el numero correcto.")

    assert state.callback_confirmation == CallbackConfirmation.REJECTED
    assert state.language == "es"
    assert "callback_confirmation:rejected" in state.known_facts


def test_intake_state_records_asked_slots_without_duplicates():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")

    state.mark_slot_asked("service_action")
    state.mark_slot_asked("service_action")
    state.mark_slot_asked("callback_number")

    assert state.asked_slots == {"service_action", "callback_number"}

    restored = IntakeState.from_dict(state.to_dict())
    assert restored.asked_slots == {"service_action", "callback_number"}


def test_intake_state_log_dict_uses_last_four_only():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan",
        caller_source="customer_memory",
        caller_confidence=0.92,
        memory_refs_used=("scoped-memory-ref-1",),
    )
    state.observe_caller_turn("I need a faucet repair.")

    redacted = state.redacted_log_dict()
    assert redacted["caller_phone_last_four"] == "8667"
    assert "caller_phone" not in redacted
    assert "caller_identity" not in redacted
    assert "known_facts" not in redacted
    assert "memory_refs_used" not in redacted
    assert redacted["known_fact_count"] == 2
    assert redacted["memory_ref_count"] == 1
    serialized = json.dumps(redacted)
    assert "caller-id-ending-8667" not in serialized
    assert "Jonathan" not in serialized
    assert "scoped-memory-ref-1" not in serialized
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_state.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.receptionist_state'`.

- [ ] **Step 3: Add the intake state implementation**

Create `app/services/receptionist_state.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify state behavior passes**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_state.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/receptionist_state.py tests/unit/test_receptionist_state.py
git commit -m "feat: add receptionist intake state"
```

---

### Task 2: Add Dialogue Planner Gating

**Files:**
- Create: `app/services/dialogue_planner.py`
- Create: `tests/unit/test_dialogue_planner.py`

- [ ] **Step 1: Write failing planner tests**

Create `tests/unit/test_dialogue_planner.py`:

```python
"""Dialogue planner policy for receptionist next actions."""

from app.services.dialogue_planner import ActionName, plan_next_action
from app.services.receptionist_state import (
    AddressNeed,
    CallbackConfirmation,
    CallbackIntent,
    IntakeState,
    Intent,
    ServiceAction,
)


def test_planner_blocks_duplicate_service_action_and_object_questions():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan",
        caller_source="customer_memory",
        caller_confidence=0.92,
    )
    state.observe_caller_turn("How much to replace a toilet?")

    action = plan_next_action(state)

    assert action.name == ActionName.ANSWER_DIRECT_QUESTION
    assert "service_action" in action.forbidden_slots
    assert "service_object" in action.forbidden_slots
    assert "callback_number" in action.forbidden_slots
    assert "service_address" in action.forbidden_slots
    assert action.allowed_slots == ("job_complexity",)
    assert action.max_spoken_shape == "answer briefly, then ask one useful next question"
    assert action.tool_calls_allowed is False


def test_planner_forbids_slots_already_asked_even_when_unknown():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.intent = Intent.SERVICE_REQUEST
    state.service_action = ServiceAction.UNKNOWN
    state.mark_slot_asked("service_action")

    action = plan_next_action(state)

    assert "service_action" in action.forbidden_slots
    assert "service_action" not in action.allowed_slots


def test_planner_confirms_callback_last_four_only_after_callback_intent():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.observe_caller_turn("Can someone call me back today?")

    action = plan_next_action(state)

    assert action.name == ActionName.CONFIRM_CALLBACK_LAST_FOUR
    assert action.allowed_slots == ("callback_confirmation",)
    assert "callback_number" not in action.forbidden_slots
    assert "service_address" in action.forbidden_slots
    assert "8667" in action.reason


def test_planner_allows_callback_number_after_intent_when_caller_id_missing():
    state = IntakeState.new(call_sid="CA_test")
    state.observe_caller_turn("Please call me back.")

    action = plan_next_action(state)

    assert action.name == ActionName.ASK_CALLBACK_NUMBER
    assert action.allowed_slots == ("callback_number",)
    assert "callback_number" not in action.forbidden_slots


def test_planner_asks_callback_number_when_caller_rejects_caller_id():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.callback_intent = CallbackIntent.REQUESTED
    state.callback_confirmation = CallbackConfirmation.REJECTED

    action = plan_next_action(state)

    assert action.name == ActionName.ASK_CALLBACK_NUMBER
    assert action.allowed_slots == ("callback_number",)
    assert "callback_number" not in action.forbidden_slots


def test_planner_allows_address_only_when_state_requires_it():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.intent = Intent.SCHEDULING
    state.address_need = AddressNeed.REQUIRED_NOW

    action = plan_next_action(state)

    assert action.name == ActionName.ASK_ONE_CLARIFYING_QUESTION
    assert action.allowed_slots == ("service_address",)
    assert "service_address" not in action.forbidden_slots


def test_planner_confirms_known_memory_instead_of_reasking_name():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan Caller",
        caller_source="customer_memory",
        caller_confidence=0.94,
        memory_refs_used=("scoped-memory-ref-1",),
    )
    state.observe_caller_turn("I need help with a faucet repair.")

    action = plan_next_action(state)

    assert "caller_name" in action.forbidden_slots
    assert "caller_name" not in action.allowed_slots
    assert "caller_identity:Jonathan Caller" in action.memory_facts_safe_to_use
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_dialogue_planner.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.dialogue_planner'`.

- [ ] **Step 3: Add planner implementation**

Create `app/services/dialogue_planner.py`:

```python
"""Deterministic receptionist planner for next allowed action."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.services.receptionist_state import (
    AddressNeed,
    CallbackConfirmation,
    CallbackIntent,
    IntakeState,
    Intent,
    ServiceAction,
    Urgency,
)


class ActionName(str, Enum):
    ANSWER_DIRECT_QUESTION = "answer_direct_question"
    ASK_NAME = "ask_name"
    ASK_ONE_CLARIFYING_QUESTION = "ask_one_clarifying_question"
    CONFIRM_KNOWN_PROPERTY = "confirm_known_property"
    ASK_URGENCY = "ask_urgency"
    OFFER_PHOTO_LINK_AFTER_CALL = "offer_photo_link_after_call"
    OFFER_CALLBACK_OR_SCHEDULING = "offer_callback_or_scheduling"
    CONFIRM_CALLBACK_LAST_FOUR = "confirm_callback_last_four"
    ASK_CALLBACK_NUMBER = "ask_callback_number"
    TAKE_MESSAGE = "take_message"
    TRY_LIVE_OWNER_TRANSFER = "try_live_owner_transfer"
    WRAP_UP = "wrap_up"
    DECLINE_OUT_OF_SCOPE = "decline_out_of_scope"
    SAFETY_GUIDANCE = "safety_guidance"


@dataclass(frozen=True)
class NextAction:
    name: ActionName
    reason: str
    allowed_slots: tuple[str, ...] = ()
    forbidden_slots: tuple[str, ...] = ()
    memory_facts_safe_to_use: tuple[str, ...] = ()
    max_spoken_shape: str = "one or two short sentences, one question maximum"
    tool_calls_allowed: bool = False


def plan_next_action(state: IntakeState) -> NextAction:
    forbidden = _forbidden_slots(state)
    memory_facts = _safe_memory_facts(state)

    if state.urgency == Urgency.EMERGENCY or state.intent == Intent.EMERGENCY:
        return NextAction(
            name=ActionName.SAFETY_GUIDANCE,
            reason="emergency intent detected",
            allowed_slots=_allowed_slots(("safety_location",), forbidden),
            forbidden_slots=tuple(sorted(forbidden)),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="give immediate safety guidance, then ask one relevant safety question",
            tool_calls_allowed=False,
        )

    if state.callback_intent in {CallbackIntent.REQUESTED, CallbackIntent.ACCEPTED}:
        if state.callback_confirmation == CallbackConfirmation.REJECTED:
            return NextAction(
                name=ActionName.ASK_CALLBACK_NUMBER,
                reason="caller rejected the caller ID callback number",
                allowed_slots=("callback_number",),
                forbidden_slots=tuple(sorted(forbidden - {"callback_number"})),
                memory_facts_safe_to_use=memory_facts,
                max_spoken_shape="ask for the best callback number in one short question",
                tool_calls_allowed=False,
            )
        if state.caller_phone_last_four:
            return NextAction(
                name=ActionName.CONFIRM_CALLBACK_LAST_FOUR,
                reason=f"callback intent exists; caller ID ending {state.caller_phone_last_four} is available",
                allowed_slots=("callback_confirmation",),
                forbidden_slots=tuple(sorted(forbidden - {"callback_number"})),
                memory_facts_safe_to_use=memory_facts,
                max_spoken_shape="confirm the caller ID last four in one short question",
                tool_calls_allowed=False,
            )
        return NextAction(
            name=ActionName.ASK_CALLBACK_NUMBER,
            reason="callback intent exists and caller ID is missing",
            allowed_slots=("callback_number",),
            forbidden_slots=tuple(sorted(forbidden - {"callback_number"})),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="ask for the best callback number in one short question",
            tool_calls_allowed=False,
        )

    if state.address_need == AddressNeed.REQUIRED_NOW:
        return NextAction(
            name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
            reason="address is required for the current scheduling or dispatch action",
            allowed_slots=_allowed_slots(("service_address",), forbidden),
            forbidden_slots=tuple(sorted(forbidden - {"service_address"})),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="ask one concise service-address question",
            tool_calls_allowed=False,
        )

    if state.intent == Intent.PRICING_QUESTION:
        return NextAction(
            name=ActionName.ANSWER_DIRECT_QUESTION,
            reason="caller asked a direct pricing or scope question",
            allowed_slots=_allowed_slots(("job_complexity", "urgency"), forbidden)[:1],
            forbidden_slots=tuple(sorted(forbidden)),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="answer briefly, then ask one useful next question",
            tool_calls_allowed=False,
        )

    if state.service_action == ServiceAction.UNKNOWN and "service_action" not in forbidden:
        return NextAction(
            name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
            reason="service action is still unknown",
            allowed_slots=("service_action",),
            forbidden_slots=tuple(sorted(forbidden)),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="ask only whether this is repair, replacement, installation, or inspection",
            tool_calls_allowed=False,
        )

    if not state.service_object and "service_object" not in forbidden:
        return NextAction(
            name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
            reason="service object is still unknown",
            allowed_slots=("service_object",),
            forbidden_slots=tuple(sorted(forbidden)),
            memory_facts_safe_to_use=memory_facts,
            max_spoken_shape="ask one question about the fixture, appliance, or system",
            tool_calls_allowed=False,
        )

    return NextAction(
        name=ActionName.ASK_ONE_CLARIFYING_QUESTION,
        reason="continue intake with one relevant detail",
        allowed_slots=_allowed_slots(("job_complexity", "urgency"), forbidden)[:1],
        forbidden_slots=tuple(sorted(forbidden)),
        memory_facts_safe_to_use=memory_facts,
        max_spoken_shape="ask one useful next question",
        tool_calls_allowed=False,
    )


def _forbidden_slots(state: IntakeState) -> set[str]:
    forbidden = set(state.asked_slots)

    if state.service_action != ServiceAction.UNKNOWN:
        forbidden.add("service_action")
    if state.service_object:
        forbidden.add("service_object")
    if state.caller_identity.name and state.caller_identity.confidence >= 0.8:
        forbidden.add("caller_name")
    if state.callback_intent in {CallbackIntent.NONE, CallbackIntent.DECLINED, CallbackIntent.OFFERED}:
        forbidden.add("callback_number")
    if state.address_need in {AddressNeed.NONE, AddressNeed.MAYBE_LATER, AddressNeed.ALREADY_KNOWN, AddressNeed.CONFIRMED}:
        forbidden.add("service_address")

    return forbidden


def _allowed_slots(candidates: tuple[str, ...], forbidden: set[str]) -> tuple[str, ...]:
    return tuple(slot for slot in candidates if slot not in forbidden)


def _safe_memory_facts(state: IntakeState) -> tuple[str, ...]:
    facts: list[str] = []
    if state.caller_identity.name and state.caller_identity.confidence >= 0.8:
        facts.append(f"caller_identity:{state.caller_identity.name}")
    return tuple(facts)
```

- [ ] **Step 4: Run planner tests to verify they pass**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_dialogue_planner.py -q
```

Expected: PASS.

- [ ] **Step 5: Run state and planner tests together**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_state.py tests/unit/test_dialogue_planner.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/dialogue_planner.py tests/unit/test_dialogue_planner.py
git commit -m "feat: add dialogue planner gating"
```

---

### Task 3: Add Instruction Composer

**Files:**
- Create: `app/services/instruction_composer.py`
- Create: `tests/unit/test_instruction_composer.py`

- [ ] **Step 1: Write failing composer tests**

Create `tests/unit/test_instruction_composer.py`:

```python
"""Instruction composition from receptionist state and planner output."""

from app.services.dialogue_planner import plan_next_action
from app.services.instruction_composer import compose_turn_instructions
from app.services.receptionist_state import IntakeState


def test_composer_includes_state_allowed_action_and_forbidden_repeats():
    state = IntakeState.new(
        call_sid="CA_test",
        caller_phone="caller-id-ending-8667",
        caller_name="Jonathan",
        caller_source="customer_memory",
        caller_confidence=0.93,
        memory_refs_used=("scoped-memory-ref-1",),
    )
    state.observe_caller_turn("How much to replace a toilet?")
    action = plan_next_action(state)

    instructions = compose_turn_instructions(
        state,
        action,
        private_memory_lines=("Prior service: kitchen sink drain repair.",),
    )

    assert "Current state:" in instructions
    assert "Caller is Jonathan" in instructions
    assert "Caller ID ending: 8667" in instructions
    assert "Service object: toilet" in instructions
    assert "Service action: replace" in instructions
    assert "Language: unknown" in instructions
    assert "Allowed next action:" in instructions
    assert "answer_direct_question" in instructions
    assert "Do not ask:" in instructions
    assert "whether this is repair, replacement, installation, or inspection" in instructions
    assert "which fixture, appliance, or object this is" in instructions
    assert "callback number" in instructions
    assert "service address" in instructions
    assert "Prior service: kitchen sink drain repair." in instructions
    assert "Jobber" not in instructions
    assert "caller-id-ending-8667" not in instructions
    assert "caller-full-phone" not in instructions


def test_composer_omits_empty_memory_section():
    state = IntakeState.new(call_sid="CA_test")
    state.observe_caller_turn("I need a faucet repair.")
    action = plan_next_action(state)

    instructions = compose_turn_instructions(state, action)

    assert "Private memory:" not in instructions
    assert "Allowed next action:" in instructions
    assert "one question maximum" in instructions


def test_composer_sanitizes_private_memory_before_model_context():
    state = IntakeState.new(call_sid="CA_test", caller_phone="caller-id-ending-8667")
    state.observe_caller_turn("I need a faucet repair.")
    action = plan_next_action(state)

    instructions = compose_turn_instructions(
        state,
        action,
        private_memory_lines=(
            "PRIVATE_SOURCE note: caller-id-ending-8667. SENSITIVE_SENTINEL. Prior sink repair is relevant.",
        ),
    )

    assert "Prior sink repair is relevant." in instructions
    assert "PRIVATE_SOURCE" not in instructions
    assert "SENSITIVE_SENTINEL" not in instructions
    assert "caller-id-ending-8667" not in instructions
    assert "Caller ID ending: 8667" in instructions
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_instruction_composer.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.instruction_composer'`.

- [ ] **Step 3: Add instruction composer implementation**

Create `app/services/instruction_composer.py`:

```python
"""Compose compact per-turn model instructions from receptionist state."""

from __future__ import annotations

from collections.abc import Iterable
import re

from app.services.dialogue_planner import NextAction
from app.services.receptionist_state import IntakeState, ServiceAction


FORBIDDEN_SLOT_PHRASES = {
    "caller_name": "the caller's name",
    "service_action": "whether this is repair, replacement, installation, or inspection",
    "service_object": "which fixture, appliance, or object this is",
    "callback_number": "callback number",
    "callback_confirmation": "callback number confirmation",
    "service_address": "service address",
}

PRIVATE_SOURCE_PATTERN = re.compile(r"\b(?:Jobber|PRIVATE_SOURCE|CRM)\b(?:\s+note)?:?\s*", re.IGNORECASE)
CALLER_ID_SENTINEL_PATTERN = re.compile(r"\bcaller-id-ending-\d{4}\b", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"\+?\d[\d .()\-]{7,}\d")
SECRET_MARKER_PATTERN = re.compile(r"\bSENSITIVE_SENTINEL\b", re.IGNORECASE)


def compose_turn_instructions(
    state: IntakeState,
    action: NextAction,
    private_memory_lines: Iterable[str] = (),
) -> str:
    sections: list[str] = ["Current state:"]
    sections.extend(_state_lines(state))

    memory_lines = tuple(
        sanitized
        for line in private_memory_lines
        if (sanitized := sanitize_private_memory_line(line))
    )
    if memory_lines:
        sections.append("")
        sections.append("Private memory:")
        sections.extend(f"- {line}" for line in memory_lines)

    sections.append("")
    sections.append("Allowed next action:")
    sections.append(f"- {action.name.value}: {action.reason}.")
    sections.append(f"- Spoken shape: {action.max_spoken_shape}.")
    if action.allowed_slots:
        sections.append(f"- Allowed slots: {', '.join(action.allowed_slots)}.")
    else:
        sections.append("- Allowed slots: none.")

    forbidden_lines = _forbidden_lines(action.forbidden_slots)
    if forbidden_lines:
        sections.append("")
        sections.append("Do not ask:")
        sections.extend(f"- {line}." for line in forbidden_lines)

    sections.append("")
    sections.append("Speaking style:")
    sections.append("- Be brief, natural, and professional.")
    sections.append("- Ask at most one question.")
    sections.append("- Do not mention private memory sources.")
    sections.append("- Do not expose full phone numbers.")
    sections.append("- Keep tool side effects disabled unless the allowed action explicitly permits them.")

    return "\n".join(sections)


def _state_lines(state: IntakeState) -> list[str]:
    lines: list[str] = []
    if state.caller_identity.name and state.caller_identity.confidence >= 0.8:
        lines.append(f"- Caller is {state.caller_identity.name}.")
    else:
        lines.append("- Caller identity is unknown.")

    if state.caller_phone_last_four:
        lines.append(f"- Caller ID ending: {state.caller_phone_last_four}.")

    if state.service_object:
        lines.append(f"- Service object: {state.service_object}.")
    else:
        lines.append("- Service object: unknown.")

    if state.service_action != ServiceAction.UNKNOWN:
        lines.append(f"- Service action: {state.service_action.value}.")
    else:
        lines.append("- Service action: unknown.")

    lines.append(f"- Intent: {state.intent.value}.")
    lines.append(f"- Callback intent: {state.callback_intent.value}.")
    lines.append(f"- Callback confirmation: {state.callback_confirmation.value}.")
    lines.append(f"- Address need: {state.address_need.value}.")
    lines.append(f"- Language: {state.language}.")
    return lines


def _forbidden_lines(forbidden_slots: tuple[str, ...]) -> list[str]:
    return [
        FORBIDDEN_SLOT_PHRASES.get(slot, slot.replace("_", " "))
        for slot in forbidden_slots
    ]


def sanitize_private_memory_line(line: str) -> str:
    sanitized = PRIVATE_SOURCE_PATTERN.sub("", line)
    sanitized = CALLER_ID_SENTINEL_PATTERN.sub("caller ID ending [redacted]", sanitized)
    sanitized = PHONE_PATTERN.sub("[redacted phone]", sanitized)
    sanitized = SECRET_MARKER_PATTERN.sub("[redacted secret]", sanitized)
    return " ".join(sanitized.split())
```

- [ ] **Step 4: Run composer tests to verify they pass**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_instruction_composer.py -q
```

Expected: PASS.

- [ ] **Step 5: Run controller unit tests together**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_state.py tests/unit/test_dialogue_planner.py tests/unit/test_instruction_composer.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/instruction_composer.py tests/unit/test_instruction_composer.py
git commit -m "feat: compose stateful receptionist instructions"
```

---

### Task 4: Add Redacted Replay Fixtures And Eval Harness

**Files:**
- Create: `app/services/receptionist_replay.py`
- Create: `tests/fixtures/receptionist_replays/known_caller_toilet_replacement.json`
- Create: `tests/fixtures/receptionist_replays/product_acceptance_scenarios.json`
- Create: `tests/unit/test_receptionist_replay.py`

- [ ] **Step 1: Write failing replay test and fixture**

Create `tests/fixtures/receptionist_replays/known_caller_toilet_replacement.json`:

```json
{
  "scenario": "known_caller_toilet_replacement",
  "initial_state": {
    "call_sid": "CA_redacted",
    "phase": "greeting",
    "caller_identity": {
      "name": "Jonathan",
      "confidence": 0.93,
      "source": "customer_memory",
      "confirmed": true
    },
    "caller_phone_last_four": "8667",
    "business_scope": "unclear",
    "business_scope_reason": "",
    "intent": "unknown",
    "service_object": "",
    "service_action": "unknown",
    "urgency": "unknown",
    "known_facts": [],
    "asked_slots": [],
    "callback_intent": "none",
    "address_need": "none",
    "memory_refs_used": ["scoped-memory-ref-1"],
    "side_effects_allowed": false
  },
  "private_memory_lines": [
    "Prior service: kitchen sink drain repair.",
    "Recent request: caller asked about toilet replacement pricing."
  ],
  "turns": [
    {
      "speaker": "caller",
      "text": "Hi, this is Jonathan. I wanted to know how much to replace a toilet.",
      "expect": {
        "service_object": "toilet",
        "service_action": "replace",
        "forbidden_slots": [
          "service_action",
          "service_object",
          "callback_number",
          "service_address"
        ],
        "instruction_excludes": [
          "repair, replacement, or new installation?",
          "callback number?",
          "service address?",
          "full-phone-number"
        ]
      }
    }
  ]
}
```

Create `tests/fixtures/receptionist_replays/product_acceptance_scenarios.json`:

```json
[
  {
    "scenario": "pricing_only_no_callback",
    "initial_state": {
      "call_sid": "CA_redacted",
      "phase": "greeting",
      "caller_identity": {"name": "", "confidence": 0.0, "source": "", "confirmed": false},
      "caller_phone_last_four": "8667",
      "business_scope": "unclear",
      "business_scope_reason": "",
      "intent": "unknown",
      "service_object": "",
      "service_action": "unknown",
      "urgency": "unknown",
      "known_facts": [],
      "asked_slots": [],
      "callback_intent": "none",
      "callback_confirmation": "unknown",
      "address_need": "none",
      "memory_refs_used": [],
      "side_effects_allowed": false,
      "language": "unknown"
    },
    "private_memory_lines": [],
    "turns": [
      {
        "speaker": "caller",
        "text": "How much does it cost to replace a faucet?",
        "expect": {
          "service_object": "faucet",
          "service_action": "replace",
          "forbidden_slots": ["service_action", "service_object", "callback_number", "service_address"],
          "instruction_excludes": ["callback number?", "service address?", "PRIVATE_SOURCE", "full-phone-number"]
        }
      }
    ]
  },
  {
    "scenario": "callback_rejected",
    "initial_state": {
      "call_sid": "CA_redacted",
      "phase": "collect_intake",
      "caller_identity": {"name": "", "confidence": 0.0, "source": "", "confirmed": false},
      "caller_phone_last_four": "8667",
      "business_scope": "unclear",
      "business_scope_reason": "",
      "intent": "callback",
      "service_object": "faucet",
      "service_action": "repair",
      "urgency": "unknown",
      "known_facts": [],
      "asked_slots": ["callback_confirmation"],
      "callback_intent": "requested",
      "callback_confirmation": "unknown",
      "address_need": "none",
      "memory_refs_used": [],
      "side_effects_allowed": false,
      "language": "unknown"
    },
    "private_memory_lines": [],
    "turns": [
      {
        "speaker": "caller",
        "text": "No, that is not the right number.",
        "expect": {
          "action_name": "ask_callback_number",
          "allowed_slots": ["callback_number"],
          "instruction_excludes": ["service address?", "full-phone-number", "PRIVATE_SOURCE"]
        }
      }
    ]
  },
  {
    "scenario": "blocked_caller_id_callback",
    "initial_state": {
      "call_sid": "CA_redacted",
      "phase": "collect_intake",
      "caller_identity": {"name": "", "confidence": 0.0, "source": "", "confirmed": false},
      "caller_phone_last_four": "",
      "business_scope": "unclear",
      "business_scope_reason": "",
      "intent": "unknown",
      "service_object": "",
      "service_action": "unknown",
      "urgency": "unknown",
      "known_facts": [],
      "asked_slots": [],
      "callback_intent": "none",
      "callback_confirmation": "unknown",
      "address_need": "none",
      "memory_refs_used": [],
      "side_effects_allowed": false,
      "language": "unknown"
    },
    "private_memory_lines": [],
    "turns": [
      {
        "speaker": "caller",
        "text": "Please call me back.",
        "expect": {
          "action_name": "ask_callback_number",
          "allowed_slots": ["callback_number"],
          "instruction_excludes": ["service address?", "full-phone-number", "PRIVATE_SOURCE"]
        }
      }
    ]
  },
  {
    "scenario": "mixed_language_request",
    "initial_state": {
      "call_sid": "CA_redacted",
      "phase": "greeting",
      "caller_identity": {"name": "", "confidence": 0.0, "source": "", "confirmed": false},
      "caller_phone_last_four": "8667",
      "business_scope": "unclear",
      "business_scope_reason": "",
      "intent": "unknown",
      "service_object": "",
      "service_action": "unknown",
      "urgency": "unknown",
      "known_facts": [],
      "asked_slots": [],
      "callback_intent": "none",
      "callback_confirmation": "unknown",
      "address_need": "none",
      "memory_refs_used": [],
      "side_effects_allowed": false,
      "language": "unknown"
    },
    "private_memory_lines": [],
    "turns": [
      {
        "speaker": "caller",
        "text": "Hola, necesito precio para reemplazar un bano.",
        "expect": {
          "language": "es",
          "service_action": "replace",
          "forbidden_slots": ["service_action", "callback_number", "service_address"],
          "instruction_includes": ["Language: es"],
          "instruction_excludes": ["callback number?", "service address?", "full-phone-number"]
        }
      }
    ]
  },
  {
    "scenario": "exact_quote_refusal",
    "initial_state": {
      "call_sid": "CA_redacted",
      "phase": "greeting",
      "caller_identity": {"name": "", "confidence": 0.0, "source": "", "confirmed": false},
      "caller_phone_last_four": "8667",
      "business_scope": "unclear",
      "business_scope_reason": "",
      "intent": "unknown",
      "service_object": "",
      "service_action": "unknown",
      "urgency": "unknown",
      "known_facts": [],
      "asked_slots": [],
      "callback_intent": "none",
      "callback_confirmation": "unknown",
      "address_need": "none",
      "memory_refs_used": [],
      "side_effects_allowed": false,
      "language": "unknown"
    },
    "private_memory_lines": [],
    "turns": [
      {
        "speaker": "caller",
        "text": "Can you guarantee the exact price to replace a toilet?",
        "expect": {
          "service_object": "toilet",
          "service_action": "replace",
          "forbidden_slots": ["service_action", "service_object", "callback_number", "service_address"],
          "instruction_includes": ["answer_direct_question"],
          "instruction_excludes": ["guarantee an exact quote", "service address?", "full-phone-number"]
        }
      }
    ]
  }
]
```

Create `tests/unit/test_receptionist_replay.py`:

```python
"""Replay tests for receptionist planner regressions."""

from pathlib import Path

from app.services.receptionist_replay import load_replay_fixture, run_replay_scenario
from app.services.receptionist_state import ServiceAction


FIXTURE_DIR = Path("tests/fixtures/receptionist_replays")


def test_known_caller_toilet_replacement_replay_blocks_duplicate_question():
    scenario = load_replay_fixture(FIXTURE_DIR / "known_caller_toilet_replacement.json")

    result = run_replay_scenario(scenario)

    assert result.violations == []
    assert result.final_state.service_object == "toilet"
    assert result.final_state.service_action == ServiceAction.REPLACE
    first_step = result.steps[0]
    assert "service_action" in first_step.next_action.forbidden_slots
    assert "service_object" in first_step.next_action.forbidden_slots
    assert "callback_number" in first_step.next_action.forbidden_slots
    assert "service_address" in first_step.next_action.forbidden_slots
    assert "whether this is repair, replacement, installation, or inspection" in first_step.instructions


def test_product_acceptance_replay_scenarios_have_no_policy_violations():
    scenarios = load_replay_fixture(FIXTURE_DIR / "product_acceptance_scenarios.json")

    for scenario in scenarios:
        result = run_replay_scenario(scenario)

        assert result.violations == [], scenario["scenario"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_replay.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.receptionist_replay'`.

- [ ] **Step 3: Add replay harness implementation**

Create `app/services/receptionist_replay.py`:

```python
"""Replay receptionist transcript fixtures through state, planner, and composer."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from app.services.dialogue_planner import NextAction, plan_next_action
from app.services.instruction_composer import compose_turn_instructions
from app.services.receptionist_state import IntakeState


@dataclass(frozen=True)
class ReplayStepResult:
    speaker: str
    text: str
    next_action: NextAction
    instructions: str


@dataclass(frozen=True)
class ReplayResult:
    final_state: IntakeState
    steps: tuple[ReplayStepResult, ...]
    violations: tuple[str, ...]


def load_replay_fixture(path: str | Path) -> dict[str, Any]:
    with Path(path).open() as handle:
        return json.load(handle)


def run_replay_scenario(scenario: dict[str, Any]) -> ReplayResult:
    state = IntakeState.from_dict(scenario["initial_state"])
    private_memory_lines = tuple(scenario.get("private_memory_lines") or [])
    steps: list[ReplayStepResult] = []
    violations: list[str] = []

    for index, turn in enumerate(scenario.get("turns") or []):
        speaker = turn.get("speaker")
        text = str(turn.get("text") or "")
        if speaker == "caller":
            state.observe_caller_turn(text)

        action = plan_next_action(state)
        instructions = compose_turn_instructions(
            state,
            action,
            private_memory_lines=private_memory_lines,
        )
        steps.append(
            ReplayStepResult(
                speaker=str(speaker),
                text=text,
                next_action=action,
                instructions=instructions,
            )
        )
        violations.extend(_check_expectations(index, turn.get("expect") or {}, state, action, instructions))

    return ReplayResult(
        final_state=state,
        steps=tuple(steps),
        violations=tuple(violations),
    )


def _check_expectations(
    index: int,
    expect: dict[str, Any],
    state: IntakeState,
    action: NextAction,
    instructions: str,
) -> list[str]:
    violations: list[str] = []

    expected_object = expect.get("service_object")
    if expected_object and state.service_object != expected_object:
        violations.append(
            f"turn {index}: expected service_object={expected_object}, got {state.service_object}"
        )

    expected_action = expect.get("service_action")
    if expected_action and state.service_action.value != expected_action:
        violations.append(
            f"turn {index}: expected service_action={expected_action}, got {state.service_action.value}"
        )

    expected_language = expect.get("language")
    if expected_language and state.language != expected_language:
        violations.append(f"turn {index}: expected language={expected_language}, got {state.language}")

    expected_action_name = expect.get("action_name")
    if expected_action_name and action.name.value != expected_action_name:
        violations.append(
            f"turn {index}: expected action_name={expected_action_name}, got {action.name.value}"
        )

    for slot in expect.get("allowed_slots") or []:
        if slot not in action.allowed_slots:
            violations.append(f"turn {index}: expected allowed slot {slot}")

    for slot in expect.get("forbidden_slots") or []:
        if slot not in action.forbidden_slots:
            violations.append(f"turn {index}: expected forbidden slot {slot}")

    for required_text in expect.get("instruction_includes") or []:
        if required_text not in instructions:
            violations.append(f"turn {index}: instruction missed required text {required_text!r}")

    for forbidden_text in expect.get("instruction_excludes") or []:
        if forbidden_text in instructions:
            violations.append(f"turn {index}: instruction included forbidden text {forbidden_text!r}")

    return violations
```

- [ ] **Step 4: Run replay test to verify it passes**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_replay.py -q
```

Expected: PASS.

- [ ] **Step 5: Run all new receptionist controller tests**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_state.py tests/unit/test_dialogue_planner.py tests/unit/test_instruction_composer.py tests/unit/test_receptionist_replay.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/services/receptionist_replay.py tests/fixtures/receptionist_replays/known_caller_toilet_replacement.json tests/fixtures/receptionist_replays/product_acceptance_scenarios.json tests/unit/test_receptionist_replay.py
git commit -m "test: add receptionist replay evaluation"
```

---

### Task 5: Keep Live Prompt Behavior Isolated For This Slice

**Files:**
- Modify: `tests/unit/test_receptionist_intelligence.py`

- [ ] **Step 1: Add a test documenting that the new controller is not wired into live calls yet**

Append this test near the other prompt/controller tests in `tests/unit/test_receptionist_intelligence.py`:

```python
def test_stateful_receptionist_controller_is_not_live_wired_in_this_slice():
    """This slice keeps live-call behavior unchanged while controller tests define policy."""
    import inspect

    import app.services.gemini_pipeline as gemini_pipeline
    import app.services.voice_pipeline as voice_pipeline

    assert not hasattr(gemini_pipeline.GeminiPipeline, "_receptionist_controller")
    assert not hasattr(voice_pipeline.VoicePipeline, "_receptionist_controller")
    live_sources = "\n".join(
        [
            inspect.getsource(gemini_pipeline),
            inspect.getsource(voice_pipeline),
        ]
    )
    assert "receptionist_state" not in live_sources
    assert "dialogue_planner" not in live_sources
    assert "instruction_composer" not in live_sources
    assert "receptionist_replay" not in live_sources
```

- [ ] **Step 2: Run the test to verify it passes**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_intelligence.py::test_stateful_receptionist_controller_is_not_live_wired_in_this_slice -q
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_receptionist_intelligence.py
git commit -m "test: document controller isolation from live calls"
```

---

### Task 6: Verification And PR Hygiene

**Files:**
- No new source files.

- [ ] **Step 1: Run focused receptionist tests**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_state.py tests/unit/test_dialogue_planner.py tests/unit/test_instruction_composer.py tests/unit/test_receptionist_replay.py tests/unit/test_receptionist_intelligence.py::test_stateful_receptionist_controller_is_not_live_wired_in_this_slice -q
```

Expected: PASS.

- [ ] **Step 2: Run targeted existing Jobber and receptionist tests**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_jobber.py tests/unit/test_receptionist_intelligence.py -q
```

Expected: PASS.

- [ ] **Step 3: Run ruff on touched files**

Run:

```bash
uv run --python 3.12 --with '.[dev]' ruff check app/services/receptionist_state.py app/services/dialogue_planner.py app/services/instruction_composer.py app/services/receptionist_replay.py tests/unit/test_receptionist_state.py tests/unit/test_dialogue_planner.py tests/unit/test_instruction_composer.py tests/unit/test_receptionist_replay.py tests/unit/test_receptionist_intelligence.py
```

Expected: `All checks passed!`

- [ ] **Step 4: Scan touched files for PII and secrets**

Run:

```bash
rg -n "\+[0-9][0-9 .()()-]{7,}[0-9]|[0-9]{10,}|oauth.*code|bearer.*[A-Za-z0-9_-]{8,}|API_BEARER_TOKEN|JOBBER_.*TOKEN|jobber_access_token|jobber_refresh_token" app/services/receptionist_state.py app/services/dialogue_planner.py app/services/instruction_composer.py app/services/receptionist_replay.py tests/unit/test_receptionist_state.py tests/unit/test_dialogue_planner.py tests/unit/test_instruction_composer.py tests/unit/test_receptionist_replay.py tests/fixtures/receptionist_replays
```

Expected: no matches.

- [ ] **Step 5: Prove live pipelines were not wired to the controller**

Run:

```bash
git diff --name-only -- app/services/gemini_pipeline.py app/services/voice_pipeline.py
rg -n "receptionist_state|dialogue_planner|instruction_composer|receptionist_replay" app/services/gemini_pipeline.py app/services/voice_pipeline.py
```

Expected: both commands produce no output. If either command prints output, stop and review before proceeding.

- [ ] **Step 6: Run full unit suite**

Run:

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit -q
```

Expected: PASS.

- [ ] **Step 7: Inspect branch state without touching untracked handoff and research docs**

Run:

```bash
git status --short --branch
```

Expected: source and test changes from this plan are cleanly committed if each task committed successfully. Existing untracked `docs/handoffs/` and `docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md` may still appear unless the user explicitly asked to commit them.

- [ ] **Step 8: Ask before publishing**

Ask the user before any of these actions:

- Push local commit `8cbda76` or any implementation commits to `origin/codex/jobber-customer-memory`.
- Commit the untracked research spec.
- Commit handoff docs under `docs/handoffs/`.
- Revert or keep commit `384ac99`.
- Deploy staging.
- Touch production.

---

## Scope Review

- Covered from the approved spec: `IntakeState`, `DialoguePlanner`, `InstructionComposer`, replay/eval tests, duplicate-slot gating, callback gating, address gating, privacy-minimized logging, private-memory sanitization, product acceptance fixtures, and no live behavior change before replay coverage.
- Covered from the panel review: branch/PR ownership gate, offline-only success criteria, synthetic fixture policy, PII/secret scan, and no-live-pipeline-wiring proof.
- Deferred to later plans: local durable `customer_memory`, memory merge policies, Jobber post-call orchestration retries, provider adapter refactor, live per-turn Gemini instruction updates, staging deployment, and production deployment.
- Type consistency: planner actions use `ActionName`; state uses `ServiceAction`, `CallbackIntent`, `CallbackConfirmation`, `AddressNeed`, and `language`; replay passes `NextAction` and `IntakeState` directly into the composer.
- Red-flag scan: no incomplete sections or unspecified code steps remain in this plan.

## Execution Choice

Plan complete. Two execution options:

1. Subagent-Driven (recommended): dispatch a fresh subagent per task, review between tasks, fast iteration.
2. Inline Execution: execute tasks in this session using executing-plans with checkpoints.

Which approach should we use?
