"""Replay receptionist transcript fixtures through state, planner, and composer."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from app.services.dialogue_planner import NextAction, plan_next_action
from app.services.instruction_composer import (
    CALLER_ID_SENTINEL_PATTERN,
    PHONE_PATTERN,
    PRIVATE_SOURCE_PATTERN,
    SECRET_MARKER_PATTERN,
    compose_turn_instructions,
)
from app.services.receptionist_state import IntakeState


@dataclass(frozen=True)
class ReplayStepResult:
    speaker: str
    text: str
    next_action: NextAction
    instructions: str
    interrupted: bool = False
    response_first_audio_ms: int | None = None
    generated_audio_ms: int | None = None


@dataclass(frozen=True)
class ReplayResult:
    final_state: IntakeState
    steps: tuple[ReplayStepResult, ...]
    violations: list[str]

    @property
    def violation_codes(self) -> tuple[str, ...]:
        codes = []
        for violation in self.violations:
            match = re.search(r"\[([a-z0-9_]+)\]", violation)
            if match:
                codes.append(match.group(1))
        return tuple(codes)


@dataclass(frozen=True)
class ReplaySuiteThresholds:
    min_scenarios: int = 10
    min_assistant_turns: int = 10
    min_interrupted_assistant_turns: int = 1
    metric_coverage: float = 1.0
    max_policy_violations: int = 0


def load_replay_fixture(path: str | Path) -> Any:
    with Path(path).open() as handle:
        return json.load(handle)


def run_replay_scenario(scenario: dict[str, Any]) -> ReplayResult:
    state = IntakeState.from_dict(scenario["initial_state"])
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
            state.observe_caller_turn(text)
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
            violations.extend(
                _check_assistant_output(index, turn, action, text, policy)
            )
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

        metrics = turn.get("metrics") or {}
        steps.append(
            ReplayStepResult(
                speaker=speaker,
                text=text,
                next_action=action,
                instructions=instructions,
                interrupted=bool(turn.get("interrupted") or False),
                response_first_audio_ms=_optional_int(metrics.get("response_first_audio_ms")),
                generated_audio_ms=_optional_int(metrics.get("generated_audio_ms")),
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
    scenarios: list[dict[str, Any]],
    *,
    thresholds: ReplaySuiteThresholds | None = None,
) -> dict[str, Any]:
    """Return aggregate replay certification results without fixture payloads."""
    limits = thresholds or ReplaySuiteThresholds()
    assistant_turns = 0
    interrupted_assistant_turns = 0
    assistant_turns_with_metrics = 0
    violation_counts: Counter[str] = Counter()

    for scenario in scenarios:
        turns = scenario.get("turns") or []
        for turn in turns:
            if turn.get("speaker") != "assistant":
                continue
            assistant_turns += 1
            if turn.get("interrupted"):
                interrupted_assistant_turns += 1
            metrics = turn.get("metrics") or {}
            if (
                _optional_int(metrics.get("response_first_audio_ms")) is not None
                and _optional_int(metrics.get("generated_audio_ms")) is not None
            ):
                assistant_turns_with_metrics += 1

        try:
            result = run_replay_scenario(scenario)
        except (KeyError, TypeError, ValueError):
            violation_counts["scenario_execution_error"] += 1
            continue
        violation_counts.update(result.violation_codes)

    metric_coverage = (
        assistant_turns_with_metrics / assistant_turns
        if assistant_turns
        else 0.0
    )
    violation_total = sum(violation_counts.values())
    gates = [
        _suite_gate(
            "minimum_scenarios",
            len(scenarios) >= limits.min_scenarios,
            len(scenarios),
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
            "assistant_metric_coverage",
            metric_coverage >= limits.metric_coverage,
            round(metric_coverage, 4),
            f">= {limits.metric_coverage}",
        ),
        _suite_gate(
            "policy_violations",
            violation_total <= limits.max_policy_violations,
            violation_total,
            f"<= {limits.max_policy_violations}",
        ),
    ]

    return {
        "status": "pass" if all(gate["passed"] for gate in gates) else "fail",
        "sample": {
            "scenarios": len(scenarios),
            "assistant_turns": assistant_turns,
            "interrupted_assistant_turns": interrupted_assistant_turns,
            "metric_coverage": round(metric_coverage, 4),
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
        violations.append(_violation(
            index,
            "expected_service_action",
            f"expected {expected_action}, got {state.service_action.value}",
        ))

    expected_intent = expect.get("intent")
    if expected_intent and state.intent.value != expected_intent:
        violations.append(_violation(
            index,
            "expected_intent",
            f"expected {expected_intent}, got {state.intent.value}",
        ))

    expected_urgency = expect.get("urgency")
    if expected_urgency and state.urgency.value != expected_urgency:
        violations.append(_violation(
            index,
            "expected_urgency",
            f"expected {expected_urgency}, got {state.urgency.value}",
        ))

    expected_callback_intent = expect.get("callback_intent")
    if expected_callback_intent and state.callback_intent.value != expected_callback_intent:
        violations.append(_violation(
            index,
            "expected_callback_intent",
            f"expected {expected_callback_intent}, got {state.callback_intent.value}",
        ))

    expected_callback_confirmation = expect.get("callback_confirmation")
    if (
        expected_callback_confirmation
        and state.callback_confirmation.value != expected_callback_confirmation
    ):
        violations.append(_violation(
            index,
            "expected_callback_confirmation",
            f"expected {expected_callback_confirmation}, got {state.callback_confirmation.value}",
        ))

    expected_language = expect.get("language")
    if expected_language and state.language != expected_language:
        violations.append(_violation(
            index,
            "expected_language",
            f"expected {expected_language}, got {state.language}",
        ))

    expected_action_name = expect.get("action_name")
    if expected_action_name and action.name.value != expected_action_name:
        violations.append(_violation(
            index,
            "expected_action_name",
            f"expected {expected_action_name}, got {action.name.value}",
        ))

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
            violations.append(_violation(
                index,
                "instruction_missing_text",
                repr(required_text),
            ))

    for forbidden_text in expect.get("instruction_excludes") or []:
        if forbidden_text in instructions:
            violations.append(_violation(
                index,
                "instruction_forbidden_text",
                repr(forbidden_text),
            ))

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
    metrics = turn.get("metrics") or {}
    observed = turn.get("observed") or {}

    max_words = int(expect.get("max_words", policy.get("max_words", 40)))
    if len(text.split()) > max_words:
        violations.append(_violation(
            index,
            "assistant_word_count",
            f"{len(text.split())} > {max_words}",
        ))

    max_questions = int(expect.get("max_questions", policy.get("max_questions", 1)))
    question_count = text.count("?")
    if question_count > max_questions:
        violations.append(_violation(
            index,
            "assistant_question_count",
            f"{question_count} > {max_questions}",
        ))

    if PRIVATE_SOURCE_PATTERN.search(text):
        violations.append(_violation(index, "assistant_private_source", "private source label"))
    if SECRET_MARKER_PATTERN.search(text):
        violations.append(_violation(index, "assistant_secret_marker", "secret marker"))
    if PHONE_PATTERN.search(text) or CALLER_ID_SENTINEL_PATTERN.search(text):
        violations.append(_violation(index, "assistant_full_phone", "phone-shaped output"))

    require_metrics = bool(policy.get("require_metrics") or expect.get("require_metrics"))
    response_first_audio_ms = _optional_int(metrics.get("response_first_audio_ms"))
    generated_audio_ms = _optional_int(metrics.get("generated_audio_ms"))
    if require_metrics and response_first_audio_ms is None:
        violations.append(_violation(index, "response_first_audio_missing", "metric missing"))
    if require_metrics and generated_audio_ms is None:
        violations.append(_violation(index, "generated_audio_missing", "metric missing"))

    response_limit = int(
        expect.get(
            "max_response_first_audio_ms",
            policy.get("max_response_first_audio_ms", 1500),
        )
    )
    if response_first_audio_ms is not None and response_first_audio_ms > response_limit:
        violations.append(_violation(
            index,
            "response_first_audio_budget",
            f"{response_first_audio_ms} > {response_limit}",
        ))

    generated_limit = int(
        expect.get(
            "max_generated_audio_ms",
            policy.get("max_generated_audio_ms", 6000),
        )
    )
    if generated_audio_ms is not None and generated_audio_ms > generated_limit:
        violations.append(_violation(
            index,
            "generated_audio_budget",
            f"{generated_audio_ms} > {generated_limit}",
        ))

    for slot in observed.get("asked_slots") or []:
        if slot not in action.allowed_slots:
            violations.append(_violation(index, "assistant_forbidden_slot", str(slot)))

    if turn.get("tool_calls") and not action.tool_calls_allowed:
        violations.append(_violation(
            index,
            "assistant_tool_call_forbidden",
            "tool call emitted while disabled",
        ))

    return violations


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _violation(index: int, code: str, detail: str) -> str:
    return f"turn {index} [{code}]: {detail}"


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
