"""Release process hardening tests."""

from pathlib import Path


def test_deploy_workflow_passes_commit_sha_to_cloud_run():
    workflow = Path(".github/workflows/deploy.yml").read_text()

    assert "DEPLOY_SHA=$GITHUB_SHA" in workflow


def test_release_smoke_script_checks_health_admin_and_authenticated_api():
    script = Path("scripts/smoke_release.sh")

    assert script.exists()

    content = script.read_text()
    assert "/health" in content
    assert "/admin" in content
    assert "/api/admin/overview" in content
    assert "ADMIN_API_TOKEN" in content


def test_deploy_workflow_gates_background_worker_runtime():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    verifier = Path("scripts/verify_cloud_run_worker_runtime.sh")

    assert workflow.count("verify_cloud_run_worker_runtime.sh") == 2
    assert verifier.exists()
    content = verifier.read_text()
    assert "run.googleapis.com/cpu-throttling" in content
    assert "run.googleapis.com/minScale" in content
    assert "autoscaling.knative.dev/minScale" in content
    assert "gcloud run services update" not in content


def test_deploy_workflow_has_branch_restricted_staging_preparation():
    workflow = Path(".github/workflows/deploy.yml").read_text()

    assert "staging-audit" in workflow
    assert "staging-prepare" in workflow
    assert "prepare-staging-message-delivery:" in workflow

    prepare_job = workflow.split("prepare-staging-message-delivery:", 1)[1].split(
        "deploy-staging:", 1
    )[0]
    assert "refs/heads/codex/enterprise-voice-integration" in prepare_job
    assert "WIF_STAGING_SERVICE_ACCOUNT" in prepare_job
    assert "manage_staging_message_delivery.py" in prepare_job
    assert "gcloud run deploy" not in prepare_job
    assert "WIF_PRODUCTION_SERVICE_ACCOUNT" not in prepare_job
    assert "PRODUCTION_SERVICE" not in prepare_job
    assert 'case "${{ inputs.target }}"' not in prepare_job
    assert "RELEASE_OPERATION: ${{ inputs.target }}" in prepare_job
    assert "STAGING_FIRESTORE_PROJECT_ID: ${{ vars.FIRESTORE_PROJECT_ID }}" in prepare_job
    assert "Health check after staging preparation" in prepare_job


def test_deploy_workflow_uses_least_privilege_and_nonpersistent_checkout():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    workflow_header = workflow.split("jobs:", 1)[0]

    assert "id-token: write" not in workflow_header
    assert workflow.count("id-token: write") == 3
    assert "concurrency:" in workflow_header
    checkout_count = workflow.count("uses: actions/checkout@")
    assert checkout_count == 4
    assert workflow.count("persist-credentials: false") == checkout_count


def test_deploy_workflow_scopes_service_names_to_their_environment_jobs():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    workflow_header = workflow.split("jobs:", 1)[0]
    prepare_job = workflow.split("prepare-staging-message-delivery:", 1)[1].split(
        "deploy-staging:", 1
    )[0]
    staging_job = workflow.split("deploy-staging:", 1)[1].split(
        "deploy-production:", 1
    )[0]
    production_job = workflow.split("deploy-production:", 1)[1]

    assert "STAGING_SERVICE" not in workflow_header
    assert "PRODUCTION_SERVICE" not in workflow_header
    assert "PRODUCTION_SERVICE" not in prepare_job
    assert "PRODUCTION_URL" not in prepare_job
    assert "PRODUCTION_SERVICE" not in staging_job
    assert "PRODUCTION_URL" not in staging_job
    assert "STAGING_SERVICE" not in production_job
    assert "STAGING_URL" not in production_job
