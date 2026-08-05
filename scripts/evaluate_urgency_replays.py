#!/usr/bin/env python3
"""Evaluate redacted urgency fixtures and print aggregate JSON only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.urgency_replay import (  # noqa: E402
    UrgencyReplayThresholds,
    evaluate_urgency_replays,
    load_urgency_replay_fixture,
)


DEFAULT_FIXTURE = Path(
    "tests/fixtures/urgency_replays/multilingual_corrections.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--min-scenarios", type=int, default=12)
    parser.add_argument("--min-languages", type=int, default=2)
    parser.add_argument("--min-corrections", type=int, default=4)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        scenarios = load_urgency_replay_fixture(args.fixture)
        if not isinstance(scenarios, list):
            raise TypeError("urgency fixture must contain a list")
        report = evaluate_urgency_replays(
            scenarios,
            thresholds=UrgencyReplayThresholds(
                min_scenarios=args.min_scenarios,
                min_languages=args.min_languages,
                min_correction_scenarios=args.min_corrections,
            ),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        report = {"status": "fail", "error": "fixture_load_failed"}

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
