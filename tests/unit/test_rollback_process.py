"""Release rollback workflow and target validation tests."""

import importlib.util
from pathlib import Path

import pytest


SCRIPT_PATH = Path("scripts/validate_rollback_target.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("validate_rollback_target", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_blocks(workflow: str) -> tuple[str, ...]:
    lines = workflow.splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() != "run: |":
            index += 1
            continue
        indentation = len(line) - len(line.lstrip())
        body = []
        index += 1
        while index < len(lines):
            candidate = lines[index]
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate.strip() and candidate_indent <= indentation:
                break
            body.append(candidate)
            index += 1
        blocks.append("\n".join(body))
    return tuple(blocks)


def _named_step(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    assert marker in workflow
    return workflow.split(marker, 1)[1].split("\n      - ", 1)[0]


@pytest.mark.parametrize(
    ("environment", "method", "target", "confirmation", "service"),
    (
        (
            "staging",
            "traffic-split",
            "kevin-api-staging-00065-jes",
            "",
            "kevin-api-staging",
        ),
        (
            "production",
            "traffic-split",
            "kevin-api-00150-abc",
            "production",
            "kevin-api",
        ),
        (
            "staging",
            "redeploy-tag",
            "staging-2026-07-12-pre-candidate",
            "",
            "kevin-api-staging",
        ),
        (
            "production",
            "redeploy-tag",
            "prod-2026-04-27-receptionist-handoff",
            "production",
            "kevin-api",
        ),
    ),
)
def test_valid_rollback_targets_are_environment_scoped(
    environment, method, target, confirmation, service
):
    module = _load_module()

    context = module.validate_request(
        environment=environment,
        method=method,
        target=target,
        production_confirmation=confirmation,
    )

    assert context.service == service
    assert context.target == target


@pytest.mark.parametrize(
    ("environment", "method", "target", "confirmation"),
    (
        ("staging", "traffic-split", "kevin-api-00065-jes", ""),
        ("production", "traffic-split", "kevin-api-staging-00065-jes", "production"),
        ("staging", "redeploy-tag", "prod-2026-04-27-receptionist-handoff", ""),
        ("production", "redeploy-tag", "staging-2026-07-12-pre", "production"),
        ("production", "traffic-split", "kevin-api-00150-abc", ""),
        ("staging", "traffic-split", "kevin-api-staging-1-abc", ""),
        ("staging", "redeploy-tag", "staging-../../main", ""),
        ("staging", "redeploy-tag", "staging-ok; touch injected", ""),
        ("staging", "redeploy-tag", "staging-ok\nBAD=value", ""),
        ("unknown", "traffic-split", "kevin-api-staging-00065-jes", ""),
        ("staging", "unknown", "kevin-api-staging-00065-jes", ""),
    ),
)
def test_invalid_or_cross_environment_targets_fail_closed(
    environment, method, target, confirmation
):
    module = _load_module()

    with pytest.raises(module.RollbackValidationError):
        module.validate_request(
            environment=environment,
            method=method,
            target=target,
            production_confirmation=confirmation,
        )


def test_validated_context_writes_only_safe_github_environment_values(tmp_path):
    module = _load_module()
    context = module.validate_request(
        environment="staging",
        method="traffic-split",
        target="kevin-api-staging-00065-jes",
        production_confirmation="",
    )
    github_env = tmp_path / "github-env"

    module.write_github_environment(context, github_env)

    assert github_env.read_text().splitlines() == [
        "ROLLBACK_ENVIRONMENT=staging",
        "ROLLBACK_METHOD=traffic-split",  # pragma: allowlist secret
        "ROLLBACK_TARGET=kevin-api-staging-00065-jes",  # pragma: allowlist secret
        "SERVICE=kevin-api-staging",
    ]


def test_rollback_workflow_has_no_input_template_expansion_in_shell():
    workflow = Path(".github/workflows/rollback.yml").read_text()

    assert "ROLLBACK_TARGET: ${{ inputs.revision_or_tag }}" in workflow
    assert "ROLLBACK_ENVIRONMENT: ${{ inputs.environment }}" in workflow
    assert "ROLLBACK_METHOD: ${{ inputs.method }}" in workflow
    assert "PRODUCTION_CONFIRMATION: ${{ inputs.confirm_production }}" in workflow
    assert all("${{ inputs." not in block for block in _run_blocks(workflow))


def test_rollback_workflow_is_main_only_least_privilege_and_serialized():
    workflow = Path(".github/workflows/rollback.yml").read_text()
    header = workflow.split("jobs:", 1)[0]

    assert "id-token: write" not in header
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "id-token: write # Required for Workload Identity Federation." in workflow
    assert "persist-credentials: false" in workflow
    assert "group: deploy-${{ inputs.environment }}" in workflow
    assert "cancel-in-progress: false" in workflow


def test_rollback_authentication_fails_closed_by_environment():
    workflow = Path(".github/workflows/rollback.yml").read_text()

    assert "Authenticate to staging GCP" in workflow
    assert "Authenticate to production GCP" in workflow
    assert "Validate staging control identities" in workflow
    assert "Validate production control identities" in workflow
    assert workflow.count("uses: google-github-actions/auth@") == 2
    assert "&& vars.WIF_PRODUCTION_SERVICE_ACCOUNT ||" not in workflow
    assert 'WIF_SERVICE_ACCOUNT: ${{ vars.WIF_STAGING_SERVICE_ACCOUNT }}' in workflow
    assert 'WIF_SERVICE_ACCOUNT: ${{ vars.WIF_PRODUCTION_SERVICE_ACCOUNT }}' in workflow


def test_rollback_does_not_depend_on_current_application_health():
    workflow = Path(".github/workflows/rollback.yml").read_text()
    capture_step = _named_step(workflow, "Capture current serving state")

    assert "CURRENT_HEALTH" not in capture_step
    assert "curl" not in capture_step
    assert "PREVIOUS_TRAFFIC" in capture_step
    assert "PREVIOUS_PROVENANCE" in capture_step


def test_rollback_workflow_validates_targets_and_proves_serving_revision():
    workflow = Path(".github/workflows/rollback.yml").read_text()

    assert "validate_rollback_target.py" in workflow
    assert '.metadata.labels["serving.knative.dev/service"] == $service' in workflow
    assert 'select(.type == "Ready" and .status == "True")' in workflow
    assert "TARGET_ENVIRONMENT=$(revision_env ENVIRONMENT)" in workflow
    assert "TARGET_APPSTORE_ENVIRONMENT=$(revision_env APPSTORE_ENVIRONMENT)" in workflow
    assert "TARGET_FIRESTORE_PROJECT=$(revision_env FIRESTORE_PROJECT_ID)" in workflow
    assert "TARGET_FIREBASE_URL=$(revision_env FIREBASE_DATABASE_URL)" in workflow
    assert '.spec.serviceAccountName == $runtime_service_account' in workflow
    assert "git check-ref-format" in workflow
    assert "git merge-base --is-ancestor" in workflow
    assert 'git checkout --detach "$ROLLBACK_COMMIT"' in workflow
    assert '--to-revisions="$TARGET_REVISION=100"' in workflow
    assert 'DEPLOY_SHA=$ROLLBACK_COMMIT' in workflow
    assert 'select(.revisionName == \\$revision)' in workflow
    assert '.revision == \\$revision' in workflow
    assert '.deploy_sha == \\$sha' in workflow
    assert "GITHUB_STEP_SUMMARY" in workflow
    assert 'gcloud run deploy "$SERVICE"' in workflow
    assert 'ENV_VARS=' not in workflow


def test_rollback_candidate_is_verified_before_traffic_changes():
    workflow = Path(".github/workflows/rollback.yml").read_text()
    deploy_index = workflow.index("Deploy rollback candidate from release tag")
    preflight_index = workflow.index("Verify rollback candidate before traffic change")
    route_index = workflow.index("Route traffic to verified rollback candidate")
    final_index = workflow.index("Verify serving revision after rollback")

    assert deploy_index < preflight_index < route_index < final_index
    assert "--no-traffic" in workflow
    assert '--tag "$CANDIDATE_TAG"' in workflow
    assert '--update-tags="$CANDIDATE_TAG=$ROLLBACK_TARGET"' in workflow
    assert '.tag == $tag and .revisionName == $revision' in workflow
    assert workflow.count("$HEALTH_FILTER") == 2
    assert '--remove-tags="$CANDIDATE_TAG"' in workflow


def test_production_revision_must_resolve_to_main_history():
    workflow = Path(".github/workflows/rollback.yml").read_text()
    revision_step = _named_step(workflow, "Validate rollback revision")

    assert '[[ "$ROLLBACK_ENVIRONMENT" == "production" ]]' in revision_step
    assert 'git cat-file -e "$TARGET_DEPLOY_SHA^{commit}"' in revision_step
    assert 'git merge-base --is-ancestor "$TARGET_DEPLOY_SHA" origin/main' in revision_step


def test_future_deploys_restore_latest_traffic_after_a_rollback():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    workflow_header = workflow.split("jobs:", 1)[0]

    assert "id-token: write" not in workflow_header
    assert "concurrency:" in workflow_header
    checkout_count = workflow.count("uses: actions/checkout@")
    assert workflow.count("persist-credentials: false") == checkout_count
    assert workflow.count("gcloud run services update-traffic") == 2
    assert workflow.count("--to-latest") == 2
    assert workflow.count("verify_cloud_run_deployment.sh") == 2
    assert 'STATUS=$(curl' not in workflow
