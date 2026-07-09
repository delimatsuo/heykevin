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
    violations: list[str]


def load_replay_fixture(path: str | Path) -> Any:
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
        violations=violations,
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
        violations.append(f"turn {index}: expected service_object={expected_object}, got {state.service_object}")

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
        violations.append(f"turn {index}: expected action_name={expected_action_name}, got {action.name.value}")

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
