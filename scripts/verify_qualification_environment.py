#!/usr/bin/env python3
"""Verify the frozen Gate 0B source and runtime identity without network access."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
_STARTUP_MARKER_ENV = "KEVIN_GATE0B_TRUSTED_STARTUP"
_TRUSTED_STARTUP_FLAGS = (
    sys.flags.isolated == 1
    and sys.flags.no_site == 1
    and sys.flags.ignore_environment == 1
    and sys.flags.no_user_site == 1
    and sys.flags.safe_path is True
)
if __name__ == "__main__" and (
    _STARTUP_MARKER_ENV not in os.environ or not _TRUSTED_STARTUP_FLAGS
):
    print('{"error_code":"qualification_startup_required","status":"blocked"}')
    raise SystemExit(2)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.qualification_identity import (  # noqa: E402
    IdentityError,
    canonical_json_bytes,
    capture_trusted_startup_identity,
)
from app.services.qualification_environment import (  # noqa: E402
    build_execution_identity_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    return parser


def _snapshot_path(source_sha: str) -> Path:
    root_digest = sha256(str(REPO_ROOT).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"kevin-gate0b-env-{root_digest}-{source_sha}.json"


def _identity_report(
    source_sha: str,
    *,
    trusted_startup: dict[str, object],
) -> dict[str, object]:
    return build_execution_identity_report(
        REPO_ROOT,
        expected_source_sha=source_sha,
        trusted_startup=trusted_startup,
    )


def _write_private(path: Path, report: dict[str, object]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, canonical_json_bytes(report) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        startup = capture_trusted_startup_identity(
            REPO_ROOT,
            expected_target="verify-environment",
        )
        startup_policy = startup.policy_report_dict()
        source_sha = startup_policy["source_preflight"]["source_sha"]
        report = _identity_report(source_sha, trusted_startup=startup_policy)
        snapshot = _snapshot_path(source_sha)
        if args.phase == "before":
            if snapshot.exists():
                raise IdentityError("environment snapshot already exists")
            _write_private(snapshot, report)
        else:
            try:
                expected = json.loads(snapshot.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise IdentityError("before snapshot is unavailable") from exc
            if expected != report:
                raise IdentityError("before and after environment identity differ")
            snapshot.unlink()
    except (IdentityError, OSError):
        print('{"error_code":"identity_verification_failed","status":"fail"}')
        return 1
    print(
        json.dumps(
            {
                "identity_sha256": sha256(canonical_json_bytes(report)).hexdigest(),
                "phase": args.phase,
                "startup_identity_sha256": sha256(
                    canonical_json_bytes(startup_policy)
                ).hexdigest(),
                "status": "pass",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
