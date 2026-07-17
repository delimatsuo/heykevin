"""Fail-closed release preflight for the observation-shadow pilot."""

from __future__ import annotations

import json

import pytest

from scripts.verify_receptionist_observation_shadow_release import (
    REQUIRED_CANDIDATE_BASE,
    REQUIRED_WORKFLOW_SOURCE_SHA,
    PreflightError,
    ReleaseContext,
    verify_release_prerequisites,
)


CANDIDATE_SHA = "a" * 40
ROLLBACK_REVISION = "kevin-api-staging-00076-nug"
HEAD_BRANCH = "codex/voice-pilot-shadow"
DEPLOY_WORKFLOW = "reviewed deploy workflow\n"
ROLLBACK_WORKFLOW = "reviewed rollback workflow\n"


def _context(**changes) -> ReleaseContext:
    values = {
        "local_head": CANDIDATE_SHA,
        "remote_heads": {HEAD_BRANCH: CANDIDATE_SHA},
        "candidate_pr": {
            "state": "OPEN",
            "headRefOid": CANDIDATE_SHA,
            "headRefName": HEAD_BRANCH,
            "baseRefName": REQUIRED_CANDIDATE_BASE,
        },
        "workflow_pr": {
            "state": "MERGED",
            "headRefOid": REQUIRED_WORKFLOW_SOURCE_SHA,
            "headRefName": "codex/voice-pilot-readiness",
            "baseRefName": "main",
        },
        "main_deploy_workflow": DEPLOY_WORKFLOW,
        "reviewed_deploy_workflow": DEPLOY_WORKFLOW,
        "main_rollback_workflow": ROLLBACK_WORKFLOW,
        "reviewed_rollback_workflow": ROLLBACK_WORKFLOW,
        "live_staging_revision": ROLLBACK_REVISION,
    }
    values.update(changes)
    return ReleaseContext(**values)


def test_preflight_accepts_only_exact_reviewed_release_identity():
    summary = verify_release_prerequisites(
        candidate_sha=CANDIDATE_SHA,
        rollback_revision=ROLLBACK_REVISION,
        context=_context(),
    )

    assert summary == {
        "status": "ready",
        "candidate_sha": CANDIDATE_SHA,
        "rollback_revision": ROLLBACK_REVISION,
        "candidate_identity_verified": True,
        "default_branch_workflows_verified": True,
        "live_rollback_verified": True,
        "deployment_authorized": False,
        "flag_enablement_authorized": False,
        "production_authorized": False,
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"local_head": "b" * 40}, "local HEAD"),
        ({"remote_heads": {HEAD_BRANCH: "b" * 40}}, "candidate PR"),
        (
            {
                "candidate_pr": {
                    "state": "OPEN",
                    "headRefOid": "b" * 40,
                    "headRefName": HEAD_BRANCH,
                    "baseRefName": REQUIRED_CANDIDATE_BASE,
                }
            },
            "candidate PR",
        ),
        (
            {
                "candidate_pr": {
                    "state": "OPEN",
                    "headRefOid": CANDIDATE_SHA,
                    "headRefName": HEAD_BRANCH,
                    "baseRefName": "main",
                }
            },
            "candidate PR",
        ),
        (
            {
                "workflow_pr": {
                    "state": "OPEN",
                    "headRefOid": REQUIRED_WORKFLOW_SOURCE_SHA,
                }
            },
            "not merged",
        ),
        (
            {
                "workflow_pr": {
                    "state": "MERGED",
                    "headRefOid": "b" * 40,
                }
            },
            "reviewed SHA",
        ),
        ({"main_deploy_workflow": "different\n"}, "deploy workflow"),
        ({"main_rollback_workflow": "different\n"}, "rollback workflow"),
        ({"live_staging_revision": "kevin-api-staging-00075-old"}, "live staging"),
    ],
)
def test_preflight_rejects_identity_or_default_branch_drift(changes, message):
    with pytest.raises(PreflightError, match=message):
        verify_release_prerequisites(
            candidate_sha=CANDIDATE_SHA,
            rollback_revision=ROLLBACK_REVISION,
            context=_context(**changes),
        )


@pytest.mark.parametrize(
    ("candidate_sha", "rollback_revision"),
    [
        ("short", ROLLBACK_REVISION),
        (CANDIDATE_SHA, "kevin-api-00076-nug"),
    ],
)
def test_preflight_rejects_malformed_release_identifiers(
    candidate_sha,
    rollback_revision,
):
    with pytest.raises(PreflightError):
        verify_release_prerequisites(
            candidate_sha=candidate_sha,
            rollback_revision=rollback_revision,
            context=_context(),
        )


def test_ready_summary_remains_nonauthorizing():
    summary = verify_release_prerequisites(
        candidate_sha=CANDIDATE_SHA,
        rollback_revision=ROLLBACK_REVISION,
        context=_context(),
    )

    serialized = json.dumps(summary)
    assert '"deployment_authorized": false' in serialized
    assert '"flag_enablement_authorized": false' in serialized
    assert '"production_authorized": false' in serialized
