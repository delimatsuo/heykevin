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
