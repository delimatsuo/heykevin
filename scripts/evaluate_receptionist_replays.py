#!/usr/bin/env python3
"""Evaluate the redacted offline receptionist policy contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.receptionist_replay import (  # noqa: E402
    ReplaySuiteThresholds,
    evaluate_replay_suite,
    load_replay_fixture,
    offline_policy_report_metadata,
)


DEFAULT_FIXTURE_DIR = Path("tests/fixtures/receptionist_replays")


def load_scenarios(fixture_dir: Path) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for fixture_path in sorted(fixture_dir.glob("*.json")):
        loaded = load_replay_fixture(fixture_path)
        if isinstance(loaded, list):
            if not all(isinstance(item, dict) for item in loaded):
                raise ValueError("fixture entries must be objects")
            scenarios.extend(loaded)
        elif isinstance(loaded, dict):
            scenarios.append(loaded)
        else:
            raise ValueError("fixture root must be an object or list of objects")
    return scenarios


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-dir", type=Path, default=DEFAULT_FIXTURE_DIR)
    parser.add_argument("--min-scenarios", type=int, default=10)
    parser.add_argument("--min-assistant-turns", type=int, default=10)
    parser.add_argument("--min-interrupted-turns", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        scenarios = load_scenarios(args.fixture_dir)
        report = evaluate_replay_suite(
            scenarios,
            thresholds=ReplaySuiteThresholds(
                min_scenarios=args.min_scenarios,
                min_assistant_turns=args.min_assistant_turns,
                min_interrupted_assistant_turns=args.min_interrupted_turns,
            ),
        )
    except (OSError, OverflowError, TypeError, ValueError, json.JSONDecodeError):
        report = {
            **offline_policy_report_metadata(),
            "status": "fail",
            "structured_contract_status": "fail",
            "error": "fixture_load_failed",
        }

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["structured_contract_status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
