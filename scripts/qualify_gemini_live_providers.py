#!/usr/bin/env python3
"""Run the fixed, payload-safe Gemini Live provider qualification matrix."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.voice_turn_replay import (  # noqa: E402
    APPROVED_QUALIFICATION_CORPUS_SHA256,
    APPROVED_QUALIFICATION_MANIFEST_SHA256,
    DEVELOPER_MODEL,
    DEVELOPER_PROVIDER,
    QUALIFICATION_SEEDS,
    SAFE_ERROR_CODES,
    VERTEX_LOCATION,
    VERTEX_MODEL,
    VERTEX_PROVIDER,
    build_gemini_setup_message,
    evaluate_gemini_provider_matrix,
    load_voice_turn_cases,
    voice_turn_manifest_identity,
)
from scripts.benchmark_gemini_turn_detection import run_benchmark  # noqa: E402


MANIFEST = REPO_ROOT / "tests/fixtures/voice_vad/fleurs_turn_replay_manifest.json"
SMOKE_SEED = 7
SMOKE_TRIALS_PER_CASE = 1
QUALIFICATION_TRIALS_PER_CASE = 5
ATTEMPTS_PER_SMOKE_RUN = 12
ATTEMPTS_PER_QUALIFICATION_RUN = 60
ATTEMPT_CEILING = 264
PROVIDER_MODELS = {
    DEVELOPER_PROVIDER: DEVELOPER_MODEL,
    VERTEX_PROVIDER: VERTEX_MODEL,
}


async def run_qualification_matrix(
    *,
    project: str,
    location: str = VERTEX_LOCATION,
) -> dict[str, Any]:
    """Execute the precommitted smoke and two-seed provider matrix once."""
    _validate_qualification_corpus()
    build_gemini_setup_message(
        VERTEX_MODEL,
        arm="manual",
        provider=VERTEX_PROVIDER,
        project=project,
        location=location,
    )

    attempts_scheduled = 0
    smoke_reports = {}
    for provider in (DEVELOPER_PROVIDER, VERTEX_PROVIDER):
        smoke_reports[provider] = await run_benchmark(
            _benchmark_args(
                provider=provider,
                project=project,
                location=location,
                seed=SMOKE_SEED,
                trials_per_case=SMOKE_TRIALS_PER_CASE,
            )
        )
        attempts_scheduled += ATTEMPTS_PER_SMOKE_RUN

    smoke = {provider: _smoke_summary(report) for provider, report in smoke_reports.items()}
    if not all(item["ready"] for item in smoke.values()):
        return {
            "status": "fail",
            "decision": "smoke_blocked",
            "decision_scope": "offline_candidate_only",
            "release_authorized": False,
            "attempt_ceiling": ATTEMPT_CEILING,
            "attempts_scheduled": attempts_scheduled,
            "smoke": smoke,
        }

    qualification_reports = []
    provider_order = {
        29: (VERTEX_PROVIDER, DEVELOPER_PROVIDER),
        41: (DEVELOPER_PROVIDER, VERTEX_PROVIDER),
    }
    for seed in sorted(QUALIFICATION_SEEDS):
        for provider in provider_order[seed]:
            qualification_reports.append(
                await run_benchmark(
                    _benchmark_args(
                        provider=provider,
                        project=project,
                        location=location,
                        seed=seed,
                        trials_per_case=QUALIFICATION_TRIALS_PER_CASE,
                    )
                )
            )
            attempts_scheduled += ATTEMPTS_PER_QUALIFICATION_RUN

    if attempts_scheduled != ATTEMPT_CEILING:
        raise ValueError("qualification matrix attempt ceiling mismatch")
    decision = evaluate_gemini_provider_matrix(qualification_reports)
    return {
        **decision,
        "attempt_ceiling": ATTEMPT_CEILING,
        "attempts_scheduled": attempts_scheduled,
        "smoke": smoke,
    }


def _benchmark_args(
    *,
    provider: str,
    project: str,
    location: str,
    seed: int,
    trials_per_case: int,
) -> argparse.Namespace:
    pairs = 6 * trials_per_case
    return argparse.Namespace(
        manifest=MANIFEST,
        qualification_mode=True,
        provider=provider,
        model=PROVIDER_MODELS[provider],
        project=project if provider == VERTEX_PROVIDER else None,
        location=location if provider == VERTEX_PROVIDER else None,
        trials_per_case=trials_per_case,
        max_provider_attempts=pairs * 2,
        seed=seed,
        min_attempts_per_arm=pairs,
        min_paired_attempts=pairs,
        manual_latency_p95_ms=1_500,
        manual_latency_max_ms=2_500,
        response_timeout_seconds=12.0,
        terminal_timeout_seconds=12.0,
    )


def _validate_qualification_corpus() -> None:
    cases = load_voice_turn_cases(MANIFEST)
    identity = voice_turn_manifest_identity(MANIFEST, cases=cases)
    if identity != {
        "manifest_sha256": APPROVED_QUALIFICATION_MANIFEST_SHA256,
        "corpus_sha256": APPROVED_QUALIFICATION_CORPUS_SHA256,
    }:
        raise ValueError("qualification requires the approved corpus")


def _smoke_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    diagnostics = report.get("diagnostics")
    sample = report.get("sample")
    automatic = diagnostics.get("automatic") if isinstance(diagnostics, Mapping) else None
    manual = diagnostics.get("manual") if isinstance(diagnostics, Mapping) else None
    ready = (
        isinstance(sample, Mapping)
        and sample.get("automatic_attempts") == 6
        and sample.get("manual_attempts") == 6
        and sample.get("paired_attempts") == 6
        and _arm_smoke_ready(automatic, expected_turns=6, require_activity=False)
        and _arm_smoke_ready(manual, expected_turns=6, require_activity=True)
    )
    return {
        "ready": ready,
        "status": report.get("status") if report.get("status") in {"pass", "fail"} else "fail",
        "automatic": _safe_arm_summary(automatic),
        "manual": _safe_arm_summary(manual),
    }


def _arm_smoke_ready(
    diagnostics: object,
    *,
    expected_turns: int,
    require_activity: bool,
) -> bool:
    if not isinstance(diagnostics, Mapping):
        return False
    if (
        diagnostics.get("completed_turns") != expected_turns
        or diagnostics.get("errors") != 0
        or not isinstance(diagnostics.get("speech_end_to_first_audio_p95_ms"), int)
        or not isinstance(diagnostics.get("speech_end_to_first_audio_max_ms"), int)
    ):
        return False
    if require_activity:
        return isinstance(
            diagnostics.get("activity_end_to_first_audio_p95_ms"), int
        ) and isinstance(diagnostics.get("activity_end_to_first_audio_max_ms"), int)
    return True


def _safe_arm_summary(
    diagnostics: object,
) -> dict[str, int | None | dict[str, int]]:
    if not isinstance(diagnostics, Mapping):
        return {
            "completed_turns": None,
            "errors": None,
            "error_counts": {},
            "speech_end_to_first_audio_p95_ms": None,
            "speech_end_to_first_audio_max_ms": None,
        }
    return {
        "completed_turns": _safe_int(diagnostics.get("completed_turns")),
        "errors": _safe_int(diagnostics.get("errors")),
        "error_counts": _safe_error_counts(diagnostics.get("error_counts")),
        "speech_end_to_first_audio_p95_ms": _safe_int(
            diagnostics.get("speech_end_to_first_audio_p95_ms")
        ),
        "speech_end_to_first_audio_max_ms": _safe_int(
            diagnostics.get("speech_end_to_first_audio_max_ms")
        ),
    }


def _safe_error_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    counts: dict[str, int] = {}
    for raw_code, raw_count in value.items():
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 1
        ):
            continue
        code = raw_code if raw_code in SAFE_ERROR_CODES else "other"
        counts[code] = counts.get(code, 0) + raw_count
    return dict(sorted(counts.items()))


def _safe_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--location", default=VERTEX_LOCATION, choices=[VERTEX_LOCATION])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(
            run_qualification_matrix(
                project=args.project,
                location=args.location,
            )
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        report = {
            "status": "fail",
            "decision": "configuration_invalid",
            "decision_scope": "offline_candidate_only",
            "release_authorized": False,
        }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
