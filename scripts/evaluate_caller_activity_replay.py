#!/usr/bin/env python3
"""Evaluate local caller-activity control against labeled voice fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.caller_activity_replay import (  # noqa: E402
    CallerActivityReplayThresholds,
    evaluate_caller_activity_replay,
)
from app.services.voice_turn_replay import (  # noqa: E402
    load_voice_turn_cases,
    voice_turn_manifest_identity,
)


DEFAULT_MANIFEST = Path(
    "tests/fixtures/voice_vad/fleurs_turn_replay_manifest.json"
)


def run_evaluation(args: argparse.Namespace) -> dict[str, Any]:
    cases = load_voice_turn_cases(args.manifest)
    report = evaluate_caller_activity_replay(
        cases,
        mode=args.mode,
        min_speech_frames=args.min_speech_frames,
        end_silence_frames=args.end_silence_frames,
        thresholds=CallerActivityReplayThresholds(
            boundary_tolerance_ms=args.boundary_tolerance_ms,
            max_endpoint_confirmation_delay_ms=(
                args.max_endpoint_confirmation_delay_ms
            ),
        ),
    )
    report["configuration"] = {
        "scope": "labeled_caller_activity_endpoint",
        "mode": args.mode,
        "min_speech_frames": args.min_speech_frames,
        "end_silence_frames": args.end_silence_frames,
        **voice_turn_manifest_identity(args.manifest, cases=cases),
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--mode", type=int, default=2)
    parser.add_argument("--min-speech-frames", type=int, default=3)
    parser.add_argument("--end-silence-frames", type=int, default=15)
    parser.add_argument("--boundary-tolerance-ms", type=int, default=150)
    parser.add_argument(
        "--max-endpoint-confirmation-delay-ms",
        type=int,
        default=500,
    )
    return parser.parse_args()


def main() -> int:
    try:
        report = run_evaluation(parse_args())
    except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError):
        report = {"status": "fail", "error": "configuration_invalid"}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
