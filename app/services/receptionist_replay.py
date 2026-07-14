"""Replay receptionist transcript fixtures through state, planner, and composer."""

from __future__ import annotations

from collections.abc import Iterable
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from app.services.dialogue_planner import ActionName, NextAction, plan_next_action
from app.services.instruction_composer import (
    CALLER_ID_SENTINEL_PATTERN,
    PHONE_PATTERN,
    PRIVATE_SOURCE_PATTERN,
    SECRET_MARKER_PATTERN,
    compose_turn_instructions,
)
from app.services.receptionist_state import (
    CallbackConfirmation,
    CallbackIntent,
    CallerObservation,
    IntakeState,
    Intent,
    ServiceAction,
    Urgency,
)


SCENARIO_FIELDS = frozenset(
    {"scenario", "policy", "initial_state", "private_memory_lines", "turns"}
)
POLICY_FIELDS = frozenset({"max_questions", "max_words"})
INITIAL_STATE_FIELDS = frozenset(
    {
        "call_sid",
        "phase",
        "caller_identity",
        "caller_phone_last_four",
        "callback_phone_last_four",
        "business_scope",
        "business_scope_reason",
        "intent",
        "service_object",
        "service_action",
        "urgency",
        "known_facts",
        "asked_slots",
        "callback_intent",
        "callback_confirmation",
        "address_need",
        "memory_refs_used",
        "side_effects_allowed",
        "language",
    }
)
CALLER_TURN_FIELDS = frozenset({"speaker", "text", "observation", "expect"})
ASSISTANT_TURN_FIELDS = frozenset(
    {"speaker", "text", "observed", "expect", "interrupted", "tool_calls"}
)
EXPECTATION_SCALAR_FIELDS = frozenset(
    {
        "service_object",
        "service_action",
        "intent",
        "urgency",
        "callback_intent",
        "callback_confirmation",
        "language",
        "action_name",
    }
)
EXPECTATION_SLOT_LIST_FIELDS = frozenset(
    {
        "allowed_slots",
        "forbidden_slots",
        "asked_slots_state",
        "unasked_slots_state",
    }
)
EXPECTATION_TEXT_LIST_FIELDS = frozenset(
    {
        "known_facts_include",
        "known_facts_exclude",
        "instruction_includes",
        "instruction_excludes",
        "output_includes",
        "output_excludes",
    }
)
EXPECTATION_INTEGER_FIELDS = frozenset({"max_questions", "max_words"})
EXPECTATION_FIELDS = (
    EXPECTATION_SCALAR_FIELDS
    | EXPECTATION_SLOT_LIST_FIELDS
    | EXPECTATION_TEXT_LIST_FIELDS
    | EXPECTATION_INTEGER_FIELDS
)
KNOWN_SLOTS = frozenset(
    {
        "caller_name",
        "service_action",
        "service_object",
        "callback_number",
        "callback_confirmation",
        "callback_preference",
        "service_address",
        "job_complexity",
        "urgency",
        "safety_location",
    }
)
TIMING_FIELD_NAMES = frozenset(
    {
        "metrics",
        "require_metrics",
        "response_first_audio_ms",
        "generated_audio_ms",
        "max_response_first_audio_ms",
        "max_generated_audio_ms",
        "latency_ms",
        "max_latency_ms",
    }
)


@dataclass(frozen=True)
class ReplayStepResult:
    speaker: str
    text: str
    next_action: NextAction
    instructions: str
    interrupted: bool = False


@dataclass(frozen=True)
class ReplayResult:
    final_state: IntakeState
    steps: tuple[ReplayStepResult, ...]
    violations: list[str]

    @property
    def violation_codes(self) -> tuple[str, ...]:
        return _extract_violation_codes(self.violations)


@dataclass(frozen=True)
class ReplaySuiteThresholds:
    min_scenarios: int = 10
    min_assistant_turns: int = 10
    min_interrupted_assistant_turns: int = 1
    max_policy_violations: int = 0


def load_replay_fixture(path: str | Path) -> Any:
    with Path(path).open() as handle:
        return json.load(handle)


def offline_policy_report_metadata() -> dict[str, object]:
    return {
        "decision_scope": "offline_structured_policy_contract",
        "caller_observation_source": "fixture",
        "assistant_output_source": "fixture",
        "assistant_observation_source": "fixture_annotation",
        "assistant_text_semantics_validated": False,
        "plain_text_acceptance_status": "review_required",
        "latency_measured": False,
        "live_behavior_validated": False,
        "release_authorized": False,
    }


def validate_replay_scenario(scenario: object) -> tuple[str, ...]:
    violations: list[str] = []
    timing_fields = _collect_timing_fields(scenario)
    if timing_fields:
        violations.append(
            _violation(
                -1,
                "fixture_timing_not_evidence",
                f"unsupported timing fields: {', '.join(sorted(timing_fields))}",
            )
        )

    if not isinstance(scenario, dict):
        violations.append(_violation(-1, "fixture_schema_invalid", "scenario must be an object"))
        return tuple(violations)

    _check_unknown_fields(violations, -1, scenario, SCENARIO_FIELDS, "scenario")
    if not isinstance(scenario.get("scenario"), str) or not scenario["scenario"].strip():
        violations.append(
            _violation(-1, "fixture_schema_invalid", "scenario name must be a string")
        )

    policy = scenario.get("policy", {})
    if not isinstance(policy, dict):
        violations.append(_violation(-1, "fixture_schema_invalid", "policy must be an object"))
    else:
        _check_unknown_fields(violations, -1, policy, POLICY_FIELDS, "policy")
        for key, value in policy.items():
            if _is_timing_field(key):
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                violations.append(
                    _violation(-1, "fixture_schema_invalid", f"policy.{key} must be nonnegative")
                )

    initial_state = scenario.get("initial_state")
    if not isinstance(initial_state, dict):
        violations.append(
            _violation(-1, "fixture_schema_invalid", "initial_state must be an object")
        )
    else:
        violations.extend(_validate_initial_state(initial_state))

    private_memory_lines = scenario.get("private_memory_lines", [])
    if not _is_string_list(private_memory_lines):
        violations.append(
            _violation(-1, "fixture_schema_invalid", "private_memory_lines must be strings")
        )

    turns = scenario.get("turns")
    if not isinstance(turns, list) or not turns:
        violations.append(_violation(-1, "fixture_schema_invalid", "turns must be a nonempty list"))
        return tuple(violations)

    for index, turn in enumerate(turns):
        violations.extend(_validate_turn(index, turn))

    return tuple(violations)


def _validate_initial_state(initial_state: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    _check_unknown_fields(
        violations,
        -1,
        initial_state,
        INITIAL_STATE_FIELDS,
        "initial_state",
    )

    string_fields = {
        "call_sid",
        "phase",
        "caller_phone_last_four",
        "callback_phone_last_four",
        "business_scope",
        "business_scope_reason",
        "intent",
        "service_object",
        "service_action",
        "urgency",
        "callback_intent",
        "callback_confirmation",
        "address_need",
        "language",
    }
    for key in string_fields.intersection(initial_state):
        if not isinstance(initial_state[key], str):
            violations.append(
                _violation(-1, "fixture_schema_invalid", f"initial_state.{key} must be a string")
            )

    for key in ("known_facts", "memory_refs_used"):
        if key in initial_state and not _is_string_list(initial_state[key]):
            violations.append(
                _violation(-1, "fixture_schema_invalid", f"initial_state.{key} must be strings")
            )
    if "asked_slots" in initial_state:
        _validate_slot_list(
            violations,
            -1,
            initial_state["asked_slots"],
            "initial_state.asked_slots",
        )

    for key in ("caller_phone_last_four", "callback_phone_last_four"):
        value = initial_state.get(key)
        if isinstance(value, str) and value and (len(value) != 4 or not value.isdigit()):
            violations.append(
                _violation(
                    -1,
                    "fixture_schema_invalid",
                    f"initial_state.{key} must contain exactly four digits",
                )
            )

    if "side_effects_allowed" in initial_state and not isinstance(
        initial_state["side_effects_allowed"], bool
    ):
        violations.append(
            _violation(
                -1,
                "fixture_schema_invalid",
                "initial_state.side_effects_allowed must be a boolean",
            )
        )

    identity = initial_state.get("caller_identity")
    if identity is not None:
        if not isinstance(identity, dict):
            violations.append(
                _violation(-1, "fixture_schema_invalid", "caller_identity must be an object")
            )
        else:
            _check_unknown_fields(
                violations,
                -1,
                identity,
                frozenset({"name", "confidence", "source", "confirmed"}),
                "caller_identity",
            )
            if any(
                key in identity and not isinstance(identity[key], str) for key in ("name", "source")
            ):
                violations.append(
                    _violation(-1, "fixture_schema_invalid", "caller identity text is invalid")
                )
            confidence = identity.get("confidence")
            if confidence is not None and (
                isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            ):
                violations.append(
                    _violation(-1, "fixture_schema_invalid", "caller confidence is invalid")
                )
            if "confirmed" in identity and not isinstance(identity["confirmed"], bool):
                violations.append(
                    _violation(-1, "fixture_schema_invalid", "caller confirmed is invalid")
                )

    if violations:
        return violations
    try:
        IntakeState.from_dict(initial_state)
    except (AttributeError, TypeError, ValueError):
        violations.append(
            _violation(-1, "fixture_schema_invalid", "initial_state values are invalid")
        )
    return violations


def _validate_turn(index: int, turn: object) -> list[str]:
    violations: list[str] = []
    if not isinstance(turn, dict):
        return [_violation(index, "fixture_schema_invalid", "turn must be an object")]

    speaker = turn.get("speaker")
    if speaker not in {"caller", "assistant"}:
        return [_violation(index, "fixture_schema_invalid", "speaker must be caller or assistant")]

    allowed_fields = CALLER_TURN_FIELDS if speaker == "caller" else ASSISTANT_TURN_FIELDS
    _check_unknown_fields(violations, index, turn, allowed_fields, f"{speaker} turn")
    if not isinstance(turn.get("text"), str):
        violations.append(_violation(index, "fixture_schema_invalid", "turn text must be a string"))

    expectation = turn.get("expect", {})
    if not isinstance(expectation, dict):
        violations.append(_violation(index, "fixture_schema_invalid", "expect must be an object"))
    else:
        violations.extend(_validate_expectation(index, expectation))

    if speaker == "caller":
        observation = turn.get("observation")
        if not isinstance(observation, dict):
            violations.append(
                _violation(index, "caller_observation_missing", "structured observation required")
            )
        else:
            try:
                CallerObservation.from_dict(observation)
            except (TypeError, ValueError):
                violations.append(
                    _violation(
                        index, "caller_observation_invalid", "invalid structured observation"
                    )
                )
        return violations

    observed = turn.get("observed")
    if not isinstance(observed, dict) or "asked_slots" not in observed:
        violations.append(
            _violation(
                index,
                "assistant_observation_missing",
                "observed.asked_slots is required",
            )
        )
    else:
        _check_unknown_fields(
            violations,
            index,
            observed,
            frozenset({"asked_slots"}),
            "assistant observed",
        )
        _validate_slot_list(violations, index, observed["asked_slots"], "observed.asked_slots")

    if "interrupted" in turn and not isinstance(turn["interrupted"], bool):
        violations.append(
            _violation(index, "fixture_schema_invalid", "interrupted must be a boolean")
        )
    if "tool_calls" in turn and not _is_string_list(turn["tool_calls"]):
        violations.append(_violation(index, "fixture_schema_invalid", "tool_calls must be strings"))
    return violations


def _validate_expectation(index: int, expectation: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    _check_unknown_fields(
        violations,
        index,
        expectation,
        EXPECTATION_FIELDS,
        "expect",
    )
    for key in EXPECTATION_SCALAR_FIELDS.intersection(expectation):
        if not isinstance(expectation[key], str):
            violations.append(
                _violation(index, "fixture_schema_invalid", f"expect.{key} must be a string")
            )
    for key in EXPECTATION_SLOT_LIST_FIELDS.intersection(expectation):
        _validate_slot_list(violations, index, expectation[key], f"expect.{key}")
    for key in EXPECTATION_TEXT_LIST_FIELDS.intersection(expectation):
        if not _is_string_list(expectation[key]):
            violations.append(
                _violation(index, "fixture_schema_invalid", f"expect.{key} must be strings")
            )
    for key in EXPECTATION_INTEGER_FIELDS.intersection(expectation):
        value = expectation[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            violations.append(
                _violation(index, "fixture_schema_invalid", f"expect.{key} must be nonnegative")
            )

    enum_fields = {
        "service_action": ServiceAction,
        "intent": Intent,
        "urgency": Urgency,
        "callback_intent": CallbackIntent,
        "callback_confirmation": CallbackConfirmation,
        "action_name": ActionName,
    }
    for key, enum_type in enum_fields.items():
        if key in expectation and isinstance(expectation[key], str):
            try:
                enum_type(expectation[key])
            except ValueError:
                violations.append(
                    _violation(index, "fixture_schema_invalid", f"expect.{key} is invalid")
                )
    return violations


def _validate_slot_list(
    violations: list[str],
    index: int,
    value: object,
    field_name: str,
) -> None:
    if (
        not _is_string_list(value)
        or len(value) != len(set(value))
        or any(slot not in KNOWN_SLOTS for slot in value)
    ):
        violations.append(
            _violation(index, "fixture_schema_invalid", f"{field_name} contains invalid slots")
        )


def _check_unknown_fields(
    violations: list[str],
    index: int,
    data: dict[str, Any],
    allowed_fields: frozenset[str],
    location: str,
) -> None:
    unknown = {key for key in data if key not in allowed_fields and not _is_timing_field(key)}
    if unknown:
        violations.append(
            _violation(
                index,
                "fixture_schema_invalid",
                f"unknown {location} fields: {', '.join(sorted(unknown))}",
            )
        )


def _collect_timing_fields(value: object) -> set[str]:
    fields: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if _is_timing_field(key):
                fields.add(key)
            fields.update(_collect_timing_fields(nested))
    elif isinstance(value, list):
        for nested in value:
            fields.update(_collect_timing_fields(nested))
    return fields


def _is_timing_field(key: object) -> bool:
    return isinstance(key, str) and (
        key in TIMING_FIELD_NAMES or "latency" in key or key.endswith("_ms")
    )


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def run_replay_scenario(scenario: object) -> ReplayResult:
    schema_violations = validate_replay_scenario(scenario)
    if not isinstance(scenario, dict):
        return ReplayResult(
            final_state=IntakeState(),
            steps=(),
            violations=list(schema_violations),
        )

    initial_state = scenario.get("initial_state")
    if schema_violations:
        return ReplayResult(
            final_state=IntakeState(),
            steps=(),
            violations=list(schema_violations),
        )
    state = IntakeState.from_dict(initial_state if isinstance(initial_state, dict) else {})

    private_memory_lines = tuple(scenario.get("private_memory_lines") or [])
    policy = dict(scenario.get("policy") or {})
    steps: list[ReplayStepResult] = []
    violations: list[str] = []
    pending_action: NextAction | None = None
    pending_instructions = ""

    for index, turn in enumerate(scenario.get("turns") or []):
        speaker = str(turn.get("speaker") or "")
        text = str(turn.get("text") or "")
        if speaker == "caller":
            observation_data = turn.get("observation")
            state.apply_caller_observation(CallerObservation.from_dict(observation_data))
            action = plan_next_action(state)
            instructions = compose_turn_instructions(
                state,
                action,
                private_memory_lines=private_memory_lines,
            )
            pending_action = action
            pending_instructions = instructions
        elif speaker == "assistant":
            action = pending_action or plan_next_action(state)
            instructions = pending_instructions or compose_turn_instructions(
                state,
                action,
                private_memory_lines=private_memory_lines,
            )
            violations.extend(_check_assistant_output(index, turn, action, text, policy))
            if not turn.get("interrupted"):
                observed = turn.get("observed") or {}
                for slot in observed.get("asked_slots") or []:
                    state.mark_slot_asked(str(slot))
            pending_action = None
            pending_instructions = ""
        else:
            action = plan_next_action(state)
            instructions = compose_turn_instructions(
                state,
                action,
                private_memory_lines=private_memory_lines,
            )
            violations.append(_violation(index, "unsupported_speaker", speaker or "missing"))

        steps.append(
            ReplayStepResult(
                speaker=speaker,
                text=text,
                next_action=action,
                instructions=instructions,
                interrupted=bool(turn.get("interrupted") or False),
            )
        )
        violations.extend(
            _check_expectations(
                index,
                turn.get("expect") or {},
                state,
                action,
                instructions,
                text,
            )
        )

    return ReplayResult(
        final_state=state,
        steps=tuple(steps),
        violations=violations,
    )


def evaluate_replay_suite(
    scenarios: list[object],
    *,
    thresholds: ReplaySuiteThresholds | None = None,
) -> dict[str, Any]:
    """Return aggregate offline policy-contract results without fixture payloads."""
    limits = thresholds or ReplaySuiteThresholds()
    assistant_turns = 0
    interrupted_assistant_turns = 0
    executed_scenarios = 0
    violation_counts: Counter[str] = Counter()

    for scenario in scenarios:
        schema_violations = validate_replay_scenario(scenario)
        if schema_violations:
            violation_counts.update(_extract_violation_codes(schema_violations))
            continue

        try:
            result = run_replay_scenario(scenario)
        except (KeyError, TypeError, ValueError):
            violation_counts["scenario_execution_error"] += 1
            continue

        executed_scenarios += 1
        for step in result.steps:
            if step.speaker != "assistant":
                continue
            assistant_turns += 1
            if step.interrupted:
                interrupted_assistant_turns += 1
        violation_counts.update(result.violation_codes)

    violation_total = sum(violation_counts.values())
    gates = [
        _suite_gate(
            "minimum_scenarios",
            executed_scenarios >= limits.min_scenarios,
            executed_scenarios,
            f">= {limits.min_scenarios}",
        ),
        _suite_gate(
            "minimum_assistant_turns",
            assistant_turns >= limits.min_assistant_turns,
            assistant_turns,
            f">= {limits.min_assistant_turns}",
        ),
        _suite_gate(
            "minimum_interrupted_assistant_turns",
            interrupted_assistant_turns >= limits.min_interrupted_assistant_turns,
            interrupted_assistant_turns,
            f">= {limits.min_interrupted_assistant_turns}",
        ),
        _suite_gate(
            "structured_and_syntactic_violations",
            violation_total <= limits.max_policy_violations,
            violation_total,
            f"<= {limits.max_policy_violations}",
        ),
    ]

    structured_contract_status = "pass" if all(gate["passed"] for gate in gates) else "fail"
    return {
        **offline_policy_report_metadata(),
        "status": ("structured_contract_pass" if structured_contract_status == "pass" else "fail"),
        "structured_contract_status": structured_contract_status,
        "sample": {
            "input_scenarios": len(scenarios),
            "scenarios": executed_scenarios,
            "assistant_turns": assistant_turns,
            "interrupted_assistant_turns": interrupted_assistant_turns,
        },
        "violation_counts": dict(sorted(violation_counts.items())),
        "gates": gates,
    }


def _check_expectations(
    index: int,
    expect: dict[str, Any],
    state: IntakeState,
    action: NextAction,
    instructions: str,
    output_text: str,
) -> list[str]:
    violations: list[str] = []

    expected_object = expect.get("service_object")
    if "service_object" in expect and state.service_object != expected_object:
        violations.append(
            _violation(
                index,
                "expected_service_object",
                f"expected {expected_object}, got {state.service_object}",
            )
        )

    expected_action = expect.get("service_action")
    if "service_action" in expect and state.service_action.value != expected_action:
        violations.append(
            _violation(
                index,
                "expected_service_action",
                f"expected {expected_action}, got {state.service_action.value}",
            )
        )

    expected_intent = expect.get("intent")
    if expected_intent and state.intent.value != expected_intent:
        violations.append(
            _violation(
                index,
                "expected_intent",
                f"expected {expected_intent}, got {state.intent.value}",
            )
        )

    expected_urgency = expect.get("urgency")
    if expected_urgency and state.urgency.value != expected_urgency:
        violations.append(
            _violation(
                index,
                "expected_urgency",
                f"expected {expected_urgency}, got {state.urgency.value}",
            )
        )

    expected_callback_intent = expect.get("callback_intent")
    if expected_callback_intent and state.callback_intent.value != expected_callback_intent:
        violations.append(
            _violation(
                index,
                "expected_callback_intent",
                f"expected {expected_callback_intent}, got {state.callback_intent.value}",
            )
        )

    expected_callback_confirmation = expect.get("callback_confirmation")
    if (
        expected_callback_confirmation
        and state.callback_confirmation.value != expected_callback_confirmation
    ):
        violations.append(
            _violation(
                index,
                "expected_callback_confirmation",
                f"expected {expected_callback_confirmation}, got {state.callback_confirmation.value}",
            )
        )

    expected_language = expect.get("language")
    if expected_language and state.language != expected_language:
        violations.append(
            _violation(
                index,
                "expected_language",
                f"expected {expected_language}, got {state.language}",
            )
        )

    expected_action_name = expect.get("action_name")
    if expected_action_name and action.name.value != expected_action_name:
        violations.append(
            _violation(
                index,
                "expected_action_name",
                f"expected {expected_action_name}, got {action.name.value}",
            )
        )

    for slot in expect.get("allowed_slots") or []:
        if slot not in action.allowed_slots:
            violations.append(_violation(index, "expected_allowed_slot", str(slot)))

    for slot in expect.get("forbidden_slots") or []:
        if slot not in action.forbidden_slots:
            violations.append(_violation(index, "expected_forbidden_slot", str(slot)))

    for slot in expect.get("asked_slots_state") or []:
        if slot not in state.asked_slots:
            violations.append(_violation(index, "expected_asked_slot_state", str(slot)))

    for slot in expect.get("unasked_slots_state") or []:
        if slot in state.asked_slots:
            violations.append(_violation(index, "expected_unasked_slot_state", str(slot)))

    for fact in expect.get("known_facts_include") or []:
        if fact not in state.known_facts:
            violations.append(_violation(index, "expected_known_fact", str(fact)))

    for fact in expect.get("known_facts_exclude") or []:
        if fact in state.known_facts:
            violations.append(_violation(index, "forbidden_known_fact", str(fact)))

    for required_text in expect.get("instruction_includes") or []:
        if required_text not in instructions:
            violations.append(
                _violation(
                    index,
                    "instruction_missing_text",
                    repr(required_text),
                )
            )

    for forbidden_text in expect.get("instruction_excludes") or []:
        if forbidden_text in instructions:
            violations.append(
                _violation(
                    index,
                    "instruction_forbidden_text",
                    repr(forbidden_text),
                )
            )

    for required_text in expect.get("output_includes") or []:
        if required_text not in output_text:
            violations.append(_violation(index, "output_missing_text", repr(required_text)))

    for forbidden_text in expect.get("output_excludes") or []:
        if forbidden_text in output_text:
            violations.append(_violation(index, "output_forbidden_text", repr(forbidden_text)))

    return violations


def _check_assistant_output(
    index: int,
    turn: dict[str, Any],
    action: NextAction,
    text: str,
    policy: dict[str, Any],
) -> list[str]:
    violations: list[str] = []
    expect = turn.get("expect") or {}
    observed = turn.get("observed") or {}
    asked_slots = tuple(observed.get("asked_slots") or ())

    max_words = int(expect.get("max_words", policy.get("max_words", 40)))
    if len(text.split()) > max_words:
        violations.append(
            _violation(
                index,
                "assistant_word_count",
                f"{len(text.split())} > {max_words}",
            )
        )

    max_questions = int(expect.get("max_questions", policy.get("max_questions", 1)))
    question_count = text.count("?")
    if question_count > max_questions:
        violations.append(
            _violation(
                index,
                "assistant_question_count",
                f"{question_count} > {max_questions}",
            )
        )

    if PRIVATE_SOURCE_PATTERN.search(text):
        violations.append(_violation(index, "assistant_private_source", "private source label"))
    if SECRET_MARKER_PATTERN.search(text):
        violations.append(_violation(index, "assistant_secret_marker", "secret marker"))
    if PHONE_PATTERN.search(text) or CALLER_ID_SENTINEL_PATTERN.search(text):
        violations.append(_violation(index, "assistant_full_phone", "phone-shaped output"))

    if not turn.get("interrupted"):
        if action.question_required and not asked_slots:
            violations.append(
                _violation(
                    index,
                    "assistant_required_question_missing",
                    "question-required action has no annotated slot",
                )
            )
        elif action.question_required and len(asked_slots) != 1:
            violations.append(
                _violation(
                    index,
                    "assistant_question_annotation_cardinality",
                    f"expected one annotated slot, got {len(asked_slots)}",
                )
            )
        elif not action.question_required and asked_slots:
            violations.append(
                _violation(
                    index,
                    "assistant_unexpected_question_annotation",
                    "non-question action has annotated slots",
                )
            )

    if action.question_required:
        for slot in asked_slots:
            if slot not in action.allowed_slots:
                violations.append(_violation(index, "assistant_forbidden_slot", str(slot)))

    if turn.get("tool_calls") and not action.tool_calls_allowed:
        violations.append(
            _violation(
                index,
                "assistant_tool_call_forbidden",
                "tool call emitted while disabled",
            )
        )

    return violations


def _violation(index: int, code: str, detail: str) -> str:
    return f"turn {index} [{code}]: {detail}"


def _extract_violation_codes(violations: Iterable[str]) -> tuple[str, ...]:
    codes = []
    for violation in violations:
        match = re.search(r"\[([a-z0-9_]+)\]", violation)
        if match:
            codes.append(match.group(1))
    return tuple(codes)


def _suite_gate(
    name: str,
    passed: bool,
    observed: object,
    requirement: str,
) -> dict[str, object]:
    return {
        "name": name,
        "passed": passed,
        "observed": observed,
        "requirement": requirement,
    }
