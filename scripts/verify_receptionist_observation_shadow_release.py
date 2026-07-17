#!/usr/bin/env python3
"""Fail-closed preflight for the staging observation-shadow deployment path."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
import subprocess
import sys
from typing import Any

REQUIRED_WORKFLOW_SOURCE_SHA = "b10a36561a11d17eee97e3496a4a3971d7a5c16b"
REQUIRED_WORKFLOW_PR = 110
REQUIRED_CANDIDATE_PR = 111
REQUIRED_CANDIDATE_BASE = "codex/voice-pilot-readiness"
DEPLOY_WORKFLOW_PATH = ".github/workflows/deploy.yml"
ROLLBACK_WORKFLOW_PATH = ".github/workflows/rollback.yml"
STAGING_PROJECT = "kevin-491315"
STAGING_REGION = "us-central1"
STAGING_SERVICE = "kevin-api-staging"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
ROLLBACK_PATTERN = re.compile(r"kevin-api-staging-[0-9]{5}-[a-z0-9]+")


class PreflightError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseContext:
    local_head: str
    remote_heads: dict[str, str]
    candidate_pr: dict[str, Any]
    workflow_pr: dict[str, Any]
    main_deploy_workflow: str
    reviewed_deploy_workflow: str
    main_rollback_workflow: str
    reviewed_rollback_workflow: str
    live_staging_revision: str


def verify_release_prerequisites(
    *,
    candidate_sha: str,
    rollback_revision: str,
    context: ReleaseContext,
) -> dict[str, Any]:
    if SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise PreflightError("candidate SHA must be an exact lowercase commit SHA")
    if ROLLBACK_PATTERN.fullmatch(rollback_revision) is None:
        raise PreflightError("rollback revision is invalid")
    if context.local_head != candidate_sha:
        raise PreflightError("local HEAD does not match the candidate SHA")

    candidate_head = context.candidate_pr.get("headRefName")
    if (
        context.candidate_pr.get("state") != "OPEN"
        or context.candidate_pr.get("headRefOid") != candidate_sha
        or context.candidate_pr.get("baseRefName") != REQUIRED_CANDIDATE_BASE
        or not isinstance(candidate_head, str)
        or context.remote_heads.get(candidate_head) != candidate_sha
    ):
        raise PreflightError("candidate PR and remote branch identity do not match")

    if (
        context.workflow_pr.get("state") != "MERGED"
        or context.workflow_pr.get("headRefOid") != REQUIRED_WORKFLOW_SOURCE_SHA
    ):
        raise PreflightError("required release workflow PR is not merged at the reviewed SHA")
    if context.main_deploy_workflow != context.reviewed_deploy_workflow:
        raise PreflightError("default-branch deploy workflow is not the reviewed version")
    if context.main_rollback_workflow != context.reviewed_rollback_workflow:
        raise PreflightError("default-branch rollback workflow is not the reviewed version")
    if context.live_staging_revision != rollback_revision:
        raise PreflightError("rollback revision does not match live staging")

    return {
        "status": "ready",
        "candidate_sha": candidate_sha,
        "rollback_revision": rollback_revision,
        "candidate_identity_verified": True,
        "default_branch_workflows_verified": True,
        "live_rollback_verified": True,
        "deployment_authorized": False,
        "flag_enablement_authorized": False,
        "production_authorized": False,
    }


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - arguments are fixed or validated
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"{command[0]} preflight command failed") from exc
    if result.returncode != 0:
        raise PreflightError(f"{command[0]} preflight command failed")
    return result.stdout


def _pr(number: int) -> dict[str, Any]:
    output = _run(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--json",
            "state,headRefOid,headRefName,baseRefName",
        ]
    )
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        raise PreflightError("GitHub PR response is invalid") from exc
    if not isinstance(value, dict):
        raise PreflightError("GitHub PR response is invalid")
    return value


def _remote_heads() -> dict[str, str]:
    lines = _run(["git", "ls-remote", "--heads", "origin"]).splitlines()
    heads: dict[str, str] = {}
    for line in lines:
        fields = line.split()
        if len(fields) != 2 or not fields[1].startswith("refs/heads/"):
            raise PreflightError("remote branch response is invalid")
        heads[fields[1].removeprefix("refs/heads/")] = fields[0]
    return heads


def _collect_context(*, candidate_pr: int, workflow_pr: int) -> ReleaseContext:
    _run(["git", "fetch", "--quiet", "origin"])
    reviewed_ref = REQUIRED_WORKFLOW_SOURCE_SHA
    return ReleaseContext(
        local_head=_run(["git", "rev-parse", "HEAD"]).strip(),
        remote_heads=_remote_heads(),
        candidate_pr=_pr(candidate_pr),
        workflow_pr=_pr(workflow_pr),
        main_deploy_workflow=_run(["git", "show", f"origin/main:{DEPLOY_WORKFLOW_PATH}"]),
        reviewed_deploy_workflow=_run(
            ["git", "show", f"{reviewed_ref}:{DEPLOY_WORKFLOW_PATH}"]
        ),
        main_rollback_workflow=_run(
            ["git", "show", f"origin/main:{ROLLBACK_WORKFLOW_PATH}"]
        ),
        reviewed_rollback_workflow=_run(
            ["git", "show", f"{reviewed_ref}:{ROLLBACK_WORKFLOW_PATH}"]
        ),
        live_staging_revision=_run(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                STAGING_SERVICE,
                "--project",
                STAGING_PROJECT,
                "--region",
                STAGING_REGION,
                "--format=value(status.latestReadyRevisionName)",
            ]
        ).strip(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--rollback-revision", required=True)
    parser.add_argument("--candidate-pr", type=int, default=REQUIRED_CANDIDATE_PR)
    parser.add_argument("--workflow-pr", type=int, default=REQUIRED_WORKFLOW_PR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.candidate_pr != REQUIRED_CANDIDATE_PR:
            raise PreflightError("candidate PR is not approved for this pilot")
        if args.workflow_pr != REQUIRED_WORKFLOW_PR:
            raise PreflightError("workflow PR is not approved for this pilot")
        context = _collect_context(
            candidate_pr=args.candidate_pr,
            workflow_pr=args.workflow_pr,
        )
        summary = verify_release_prerequisites(
            candidate_sha=args.candidate_sha,
            rollback_revision=args.rollback_revision,
            context=context,
        )
        print(json.dumps(summary, sort_keys=True))
        return 0
    except PreflightError as error:
        print(
            json.dumps({"status": "blocked", "reason": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except Exception as error:
        print(
            json.dumps(
                {"status": "error", "exception_type": type(error).__name__},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
