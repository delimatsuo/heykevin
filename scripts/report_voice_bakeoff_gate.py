#!/usr/bin/env python3
"""Print a payload-safe, read-only Task 4.8 gate-status report."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Sequence

from app.services.voice_bakeoff_gate_report import (
    GateReportError,
    build_task_4_8_gate_report,
)


_MAX_FILE_BYTES = 131_072
_DEFAULT_PACKAGE = (
    "tests/fixtures/voice_architecture_bakeoff/"
    "task_4_8_gate_package.template.json"
)
_REPORT_SOURCES = (
    "app/services/voice_bakeoff_gate_contracts.py",
    "app/services/voice_bakeoff_gate_report.py",
    "app/services/voice_bakeoff_preauth_reference.py",
    "scripts/report_voice_bakeoff_gate.py",
)


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _load_package(path: Path) -> dict[str, object]:
    if path.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("input exceeds size limit")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_no_duplicates,
    )
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _current_source_sha(root: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
    ).strip()


def _assert_report_sources_clean(root: Path) -> None:
    try:
        tracked = subprocess.check_output(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", *_REPORT_SOURCES],
            text=True,
        ).splitlines()
        status = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--", *_REPORT_SOURCES],
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise GateReportError("report sources are not tracked") from error
    if set(tracked) != set(_REPORT_SOURCES) or status:
        raise GateReportError("report sources are not clean against HEAD")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=Path(_DEFAULT_PACKAGE))
    args = parser.parse_args(argv)
    try:
        root = Path(__file__).resolve().parents[1]
        _assert_report_sources_clean(root)
        report = build_task_4_8_gate_report(
            package=_load_package(args.package),
            source_sha=_current_source_sha(root),
        )
    except (OSError, ValueError, json.JSONDecodeError, GateReportError):
        print(json.dumps({"report_status": "invalid_local_package"}, sort_keys=True))
        return 2

    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
