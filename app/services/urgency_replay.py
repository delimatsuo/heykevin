"""Aggregate evaluation for redacted multilingual urgency fixtures."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from app.services.urgency import find_urgent_signal


@dataclass(frozen=True)
class UrgencyReplayThresholds:
    min_scenarios: int = 12
    min_languages: int = 2
    min_correction_scenarios: int = 4


def load_urgency_replay_fixture(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _valid_scenario(scenario: object) -> bool:
    if not isinstance(scenario, dict):
        return False
    expected = scenario.get("expected_signal")
    tags = scenario.get("tags")
    return bool(
        isinstance(scenario.get("scenario"), str)
        and isinstance(scenario.get("language"), str)
        and isinstance(scenario.get("text"), str)
        and (expected is None or isinstance(expected, str))
        and isinstance(tags, list)
        and all(isinstance(tag, str) for tag in tags)
    )


def evaluate_urgency_replays(
    scenarios: list[dict[str, Any]],
    *,
    thresholds: UrgencyReplayThresholds | None = None,
) -> dict[str, Any]:
    """Return aggregate certification data without fixture payloads or IDs."""
    thresholds = thresholds or UrgencyReplayThresholds()
    violations: Counter[str] = Counter()
    languages: set[str] = set()
    correction_scenarios = 0

    for scenario in scenarios:
        if not _valid_scenario(scenario):
            violations["invalid_scenario"] += 1
            continue
        language = scenario["language"]
        tags = scenario["tags"]
        languages.add(language)
        if "correction" in tags:
            correction_scenarios += 1
        if find_urgent_signal(scenario["text"]) != scenario["expected_signal"]:
            violations["classifier_mismatch"] += 1

    violation_count = sum(violations.values())
    gates = [
        {
            "name": "minimum_scenarios",
            "observed": len(scenarios),
            "requirement": f">= {thresholds.min_scenarios}",
            "passed": len(scenarios) >= thresholds.min_scenarios,
        },
        {
            "name": "minimum_languages",
            "observed": len(languages),
            "requirement": f">= {thresholds.min_languages}",
            "passed": len(languages) >= thresholds.min_languages,
        },
        {
            "name": "minimum_correction_scenarios",
            "observed": correction_scenarios,
            "requirement": f">= {thresholds.min_correction_scenarios}",
            "passed": correction_scenarios >= thresholds.min_correction_scenarios,
        },
        {
            "name": "classifier_violations",
            "observed": violation_count,
            "requirement": "<= 0",
            "passed": violation_count == 0,
        },
    ]
    return {
        "status": "pass" if all(gate["passed"] for gate in gates) else "fail",
        "sample": {
            "scenarios": len(scenarios),
            "languages": len(languages),
            "correction_scenarios": correction_scenarios,
        },
        "gates": gates,
        "violation_counts": dict(sorted(violations.items())),
    }
