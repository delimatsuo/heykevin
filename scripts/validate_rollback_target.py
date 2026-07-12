#!/usr/bin/env python3
"""Validate rollback inputs before credentials or external state are used."""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


ENVIRONMENTS = {"staging", "production"}
METHODS = {"traffic-split", "redeploy-tag"}
SERVICES = {"staging": "kevin-api-staging", "production": "kevin-api"}

_REVISION_PATTERNS = {
    "staging": re.compile(r"kevin-api-staging-[0-9]{5}-[a-z0-9]{3}"),
    "production": re.compile(r"kevin-api-[0-9]{5}-[a-z0-9]{3}"),
}
_TAG_PATTERNS = {
    "staging": re.compile(r"staging-[a-z0-9][a-z0-9._-]{0,95}"),
    "production": re.compile(r"prod-[a-z0-9][a-z0-9._-]{0,95}"),
}


class RollbackValidationError(ValueError):
    """A payload-free rollback request validation failure."""


@dataclass(frozen=True)
class RollbackContext:
    environment: str
    method: str
    target: str
    service: str


def _validate_target_text(target: str) -> None:
    if not target or len(target) > 110 or not target.isascii():
        raise RollbackValidationError("rollback target is invalid")
    if any(character.isspace() for character in target):
        raise RollbackValidationError("rollback target is invalid")
    if ".." in target or "@{" in target or target.endswith((".", "-")):
        raise RollbackValidationError("rollback target is invalid")


def validate_request(
    *,
    environment: str,
    method: str,
    target: str,
    production_confirmation: str,
) -> RollbackContext:
    if environment not in ENVIRONMENTS:
        raise RollbackValidationError("rollback environment is invalid")
    if method not in METHODS:
        raise RollbackValidationError("rollback method is invalid")
    if environment == "production" and production_confirmation != "production":
        raise RollbackValidationError("production rollback confirmation is invalid")

    _validate_target_text(target)
    pattern = (
        _REVISION_PATTERNS[environment]
        if method == "traffic-split"
        else _TAG_PATTERNS[environment]
    )
    if pattern.fullmatch(target) is None:
        raise RollbackValidationError("rollback target does not match environment")

    return RollbackContext(
        environment=environment,
        method=method,
        target=target,
        service=SERVICES[environment],
    )


def write_github_environment(context: RollbackContext, path: Path) -> None:
    values = (
        ("ROLLBACK_ENVIRONMENT", context.environment),
        ("ROLLBACK_METHOD", context.method),
        ("ROLLBACK_TARGET", context.target),
        ("SERVICE", context.service),
    )
    with path.open("a", encoding="utf-8") as handle:
        for name, value in values:
            handle.write(f"{name}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a release rollback request.")
    parser.add_argument("--environment", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--production-confirmation", default="")
    parser.add_argument("--github-env", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        context = validate_request(
            environment=args.environment,
            method=args.method,
            target=args.target,
            production_confirmation=args.production_confirmation,
        )
        write_github_environment(context, args.github_env)
    except (OSError, RollbackValidationError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, RollbackValidationError)
            else "GitHub environment output is unavailable"
        )
        print(f"rollback_validation status=failed reason={reason}", file=sys.stderr)
        return 1

    print(
        "rollback_validation status=ready "
        f"environment={context.environment} method={context.method}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
