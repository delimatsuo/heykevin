#!/usr/bin/env python3
"""Fail-closed aggregate evaluator for payload-free bakeoff NDJSON.

The evaluator accepts only a deliberately small, non-content event schema. Its
expected cohort identity comes from trusted command-line inputs, never from the
events it is evaluating.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from typing import Iterable


_FIELDS = {
    "schema_version",
    "candidate_arm",
    "revision",
    "source_sha",
    "manifest_digest",
    "session",
    "turn",
    "act",
    "event",
    "ceiling_reached_candidate",
}
_EVENTS = {
    "generated",
    "sent",
    "played",
    "cleared",
    "partial",
    "interrupted",
    "failed",
}
_TERMINALS = _EVENTS - {"generated", "sent"}
_ARMS = {"A", "B1", "B2", "C"}
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID = re.compile(r"^[0-9a-f]{24}$")
_REVISION_ID = re.compile(r"^revision_[0-9]{1,12}$")
_ACT_ID = re.compile(r"^act_[0-9]{1,12}$")


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting ambiguous duplicate field names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _valid(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ValueError("event must be an object")
    if set(item) != _FIELDS:
        raise ValueError("unrecognized or missing field")
    if (
        isinstance(item["schema_version"], bool)
        or not isinstance(item["schema_version"], int)
        or item["schema_version"] != 1
    ):
        raise ValueError("invalid schema version")
    if not isinstance(item["candidate_arm"], str) or item["candidate_arm"] not in _ARMS:
        raise ValueError("invalid schema or arm")
    if not isinstance(item["source_sha"], str) or not _SOURCE_SHA.fullmatch(item["source_sha"]):
        raise ValueError("invalid source SHA")
    if not isinstance(item["manifest_digest"], str) or not _MANIFEST_DIGEST.fullmatch(
        item["manifest_digest"]
    ):
        raise ValueError("invalid manifest digest")
    if not isinstance(item["session"], str) or not _SESSION_ID.fullmatch(item["session"]):
        raise ValueError("invalid session identifier")
    if not isinstance(item["revision"], str) or not _REVISION_ID.fullmatch(item["revision"]):
        raise ValueError("invalid revision identifier")
    if not isinstance(item["act"], str) or not _ACT_ID.fullmatch(item["act"]):
        raise ValueError("invalid act identifier")
    if isinstance(item["turn"], bool) or not isinstance(item["turn"], int) or item["turn"] < 1:
        raise ValueError("invalid turn")
    if not isinstance(item["event"], str) or item["event"] not in _EVENTS:
        raise ValueError("invalid event")
    if not isinstance(item["ceiling_reached_candidate"], bool):
        raise ValueError("invalid ceiling flag")
    return item


def parse_ndjson(lines: Iterable[str]) -> list[dict[str, object]]:
    """Parse payload-free NDJSON and reject malformed or ambiguous records."""
    parsed: list[dict[str, object]] = []
    for line in lines:
        if not line.strip():
            continue
        item = json.loads(line, object_pairs_hook=_object_without_duplicate_keys)
        parsed.append(_valid(item))
    return parsed


def _validate_expected_identity(
    *,
    expected_arm: str,
    expected_revision: str,
    expected_source_sha: str,
    expected_manifest_digest: str,
    expected_sessions: int,
    expected_turns: int,
) -> None:
    if not isinstance(expected_arm, str) or expected_arm not in _ARMS:
        raise ValueError("invalid expected arm")
    if not isinstance(expected_revision, str) or not _REVISION_ID.fullmatch(expected_revision):
        raise ValueError("invalid expected revision")
    if not isinstance(expected_source_sha, str) or not _SOURCE_SHA.fullmatch(expected_source_sha):
        raise ValueError("invalid expected source SHA")
    if not isinstance(expected_manifest_digest, str) or not _MANIFEST_DIGEST.fullmatch(
        expected_manifest_digest
    ):
        raise ValueError("invalid expected manifest digest")
    if (
        isinstance(expected_sessions, bool)
        or not isinstance(expected_sessions, int)
        or expected_sessions < 1
    ):
        raise ValueError("expected sessions must be positive")
    if (
        isinstance(expected_turns, bool)
        or not isinstance(expected_turns, int)
        or expected_turns < 1
    ):
        raise ValueError("expected turns must be positive")


def evaluate_ndjson(
    lines: Iterable[dict[str, object]],
    *,
    expected_arm: str,
    expected_revision: str,
    expected_source_sha: str,
    expected_manifest_digest: str,
    expected_sessions: int,
    expected_turns: int,
) -> dict[str, object]:
    """Evaluate one pre-declared cohort without emitting call-level information."""
    _validate_expected_identity(
        expected_arm=expected_arm,
        expected_revision=expected_revision,
        expected_source_sha=expected_source_sha,
        expected_manifest_digest=expected_manifest_digest,
        expected_sessions=expected_sessions,
        expected_turns=expected_turns,
    )

    turns: dict[tuple[str, int, str], list[dict[str, object]]] = {}
    integrity_errors = 0
    expected_identity = (
        expected_arm,
        expected_revision,
        expected_source_sha,
        expected_manifest_digest,
    )
    for raw in lines:
        item = _valid(raw)
        event_identity = (
            item["candidate_arm"],
            item["revision"],
            item["source_sha"],
            item["manifest_digest"],
        )
        if event_identity != expected_identity:
            integrity_errors += 1
            continue
        key = (str(item["session"]), int(item["turn"]), str(item["act"]))
        turns.setdefault(key, []).append(item)

    terminals: Counter[str] = Counter()
    for events in turns.values():
        event_names = [str(item["event"]) for item in events]
        valid_sequence = (
            len(event_names) == 3
            and event_names[:2] == ["generated", "sent"]
            and event_names[2] in _TERMINALS
        )
        if not valid_sequence:
            integrity_errors += 1
            continue
        if event_names[2] == "played" and any(
            item["ceiling_reached_candidate"] for item in events
        ):
            integrity_errors += 1
            continue
        terminals[event_names[2]] += 1

    sessions = len({key[0] for key in turns})
    if sessions != expected_sessions or len(turns) != expected_turns:
        integrity_errors += 1
    return {
        "status": "pass" if integrity_errors == 0 else "insufficient_evidence",
        "sessions": sessions,
        "turns": len(turns),
        "terminal_counts": dict(sorted(terminals.items())),
        "integrity_errors": integrity_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--expected-sessions", type=int, required=True)
    parser.add_argument("--expected-turns", type=int, required=True)
    args = parser.parse_args()
    try:
        values = parse_ndjson(sys.stdin)
        report = evaluate_ndjson(
            values,
            expected_arm=args.arm,
            expected_revision=args.revision,
            expected_source_sha=args.source_sha,
            expected_manifest_digest=args.manifest_digest,
            expected_sessions=args.expected_sessions,
            expected_turns=args.expected_turns,
        )
    except (json.JSONDecodeError, ValueError):
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
