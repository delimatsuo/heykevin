#!/usr/bin/env python3
"""Evaluate synthetic caller-turn permutations without provider activity."""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Sequence

from app.services.caller_turns import (
    CALLER_TURN_SCHEMA_VERSION,
    CallerTurnAssembler,
    CallerTurnEvent,
)
from app.services.gemini_turn_events import GEMINI_TURN_EVENT_SCHEMA_VERSION


REPORT_SCHEMA_VERSION = 1
OFFLINE_SCOPE = "offline_synthetic_permutations"
DEFAULT_FIXTURE = Path("tests/fixtures/caller_turn_events/permutations.json")
MAX_FIXTURE_BYTES = 2 * 1024 * 1024
SOURCE_SHA_PATTERN = re.compile(r"[0-9a-f]{40,64}")
MODEL_ID_PATTERN = re.compile(r"gemini-[a-z0-9][a-z0-9.-]*")
EVIDENCE_FIELDS = (
    "turn_assembly_validated",
    "semantic_extraction_validated",
    "shadow_operational_isolation_validated",
    "caller_experience_neutrality_validated",
    "active_control_validated",
    "provider_execution_authorized",
    "real_caller_data_authorized",
    "staging_authorized",
    "production_authorized",
    "release_authorized",
)


def evaluate_caller_turn_fixture(
    fixture_path: str | Path,
    *,
    source_sha: str,
    model_id: str,
) -> dict[str, Any]:
    """Return a payload-free aggregate report for one synthetic fixture."""
    _validate_execution_identity(source_sha=source_sha, model_id=model_id)
    path = Path(fixture_path)
    try:
        fixture_digest = _file_sha256(path)
    except OSError:
        return _report(
            status="fail",
            identity=_identity(
                fixture_digest=None,
                source_sha=source_sha,
                model_id=model_id,
            ),
            scenario_count=0,
            failed_scenarios=0,
            observed_turns=0,
            status_counts=Counter(),
            close_reason_counts=Counter(),
            finalization_lags_ms=[],
            failures=Counter({"fixture_unavailable": 1}),
            quiescence_ms=None,
        )
    identity = _identity(
        fixture_digest=fixture_digest,
        source_sha=source_sha,
        model_id=model_id,
    )
    try:
        fixture = _load_fixture(path)
        return _evaluate_loaded_fixture(fixture, identity=identity)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        return _report(
            status="fail",
            identity=identity,
            scenario_count=0,
            failed_scenarios=0,
            observed_turns=0,
            status_counts=Counter(),
            close_reason_counts=Counter(),
            finalization_lags_ms=[],
            failures=Counter({"fixture_invalid": 1}),
            quiescence_ms=None,
        )


def _evaluate_loaded_fixture(
    fixture: dict[str, Any],
    *,
    identity: dict[str, Any],
) -> dict[str, Any]:
    raw_cases = fixture["cases"]
    quiescence_ms = fixture["quiescence_ms"]
    if (
        isinstance(quiescence_ms, bool)
        or not isinstance(quiescence_ms, int)
        or not 1 <= quiescence_ms <= 5_000
    ):
        raise ValueError("fixture quiescence policy is invalid")
    failures: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    close_reason_counts: Counter[str] = Counter()
    finalization_lags_ms: list[int] = []
    observed_turns = 0

    for raw_case in raw_cases:
        try:
            observed = _evaluate_case(raw_case, quiescence_ms=quiescence_ms)
        except (KeyError, TypeError, ValueError):
            failures["scenario_invalid"] += 1
            continue
        expected = raw_case["expected_turns"]
        if [turn.to_dict() for turn in observed] != expected:
            failures["scenario_mismatch"] += 1
        observed_turns += len(observed)
        for turn in observed:
            status_counts[turn.status.value] += 1
            close_reason_counts[turn.close_reason.value] += 1
            finalization_lags_ms.append(turn.finalized_at_ms - turn.terminal_at_ms)

    failed_scenarios = sum(failures.values())
    return _report(
        status="pass" if failed_scenarios == 0 else "fail",
        identity=identity,
        scenario_count=len(raw_cases),
        failed_scenarios=failed_scenarios,
        observed_turns=observed_turns,
        status_counts=status_counts,
        close_reason_counts=close_reason_counts,
        finalization_lags_ms=finalization_lags_ms,
        failures=failures,
        quiescence_ms=quiescence_ms,
    )


def _evaluate_case(raw_case: object, *, quiescence_ms: int):
    if not isinstance(raw_case, dict):
        raise TypeError("case must be an object")
    events = raw_case["events"]
    expected = raw_case["expected_turns"]
    advance_to_ms = raw_case["advance_to_ms"]
    if not isinstance(events, list) or not isinstance(expected, list):
        raise TypeError("case events and expected turns must be arrays")
    if isinstance(advance_to_ms, bool) or not isinstance(advance_to_ms, int):
        raise TypeError("advance time must be an integer")
    typed_events = [CallerTurnEvent.from_dict(event) for event in events]
    active_epoch = typed_events[0].epoch if typed_events else 1
    assembler = CallerTurnAssembler(
        active_epoch=active_epoch,
        quiescence_ms=quiescence_ms,
    )
    observed = []
    for event in typed_events:
        observed.extend(assembler.ingest(event))
    observed.extend(assembler.advance_time(advance_to_ms))
    return tuple(observed)


def _report(
    *,
    status: str,
    identity: dict[str, Any],
    scenario_count: int,
    failed_scenarios: int,
    observed_turns: int,
    status_counts: Counter[str],
    close_reason_counts: Counter[str],
    finalization_lags_ms: list[int],
    failures: Counter[str],
    quiescence_ms: int | None,
) -> dict[str, Any]:
    evidence = {field: False for field in EVIDENCE_FIELDS}
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "scope": OFFLINE_SCOPE,
        "identity": identity,
        "policy": {"quiescence_ms": quiescence_ms},
        "sample": {
            "scenarios": scenario_count,
            "failed_scenarios": failed_scenarios,
            "observed_turns": observed_turns,
        },
        "metrics": {
            "completion_status_counts": dict(sorted(status_counts.items())),
            "close_reason_counts": dict(sorted(close_reason_counts.items())),
            "finalization_lag_ms": _bounded_latency_summary(finalization_lags_ms),
        },
        "failures": dict(sorted(failures.items())),
        **evidence,
    }


def _bounded_latency_summary(values: list[int]) -> dict[str, int | None]:
    if not values:
        return {"min": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "p50": _nearest_rank(ordered, 0.50),
        "p95": _nearest_rank(ordered, 0.95),
        "max": ordered[-1],
    }


def _nearest_rank(ordered: list[int], percentile: float) -> int:
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile + 0.9999) - 1))
    return ordered[index]


def _load_fixture(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_FIXTURE_BYTES:
        raise ValueError("fixture exceeds size bound")
    fixture = json.loads(raw)
    if not isinstance(fixture, dict) or fixture.get("version") != 1:
        raise ValueError("unsupported fixture")
    if fixture.get("provenance") != "fixture_authored_synthetic":
        raise ValueError("fixture provenance must be synthetic")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("fixture cases must be a non-empty array")
    return fixture


def _validate_execution_identity(*, source_sha: str, model_id: str) -> None:
    if not isinstance(source_sha, str) or not SOURCE_SHA_PATTERN.fullmatch(source_sha):
        raise ValueError("source_sha must be an exact hexadecimal commit SHA")
    if (
        not isinstance(model_id, str)
        or not MODEL_ID_PATTERN.fullmatch(model_id)
        or "latest" in model_id
    ):
        raise ValueError("model_id must be an exact Gemini model ID")


def _identity(
    *,
    fixture_digest: str | None,
    source_sha: str,
    model_id: str,
) -> dict[str, Any]:
    return {
        "fixture_sha256": fixture_digest,
        "source_sha": source_sha,
        "model_id": model_id,
        "caller_turn_schema_version": CALLER_TURN_SCHEMA_VERSION,
        "gemini_event_schema_version": GEMINI_TURN_EVENT_SCHEMA_VERSION,
    }


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate synthetic caller-turn event permutations offline.",
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = evaluate_caller_turn_fixture(
        args.fixture,
        source_sha=args.source_sha,
        model_id=args.model_id,
    )
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
