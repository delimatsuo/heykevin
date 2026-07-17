"""Release process hardening tests."""

from pathlib import Path


def _workflow_run_blocks(workflow: str) -> list[str]:
    lines = workflow.splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        if not stripped.startswith("run:"):
            index += 1
            continue

        indent = len(line) - len(stripped)
        inline = stripped.removeprefix("run:").strip()
        if inline and inline != "|":
            blocks.append(inline)
            index += 1
            continue

        index += 1
        block = []
        while index < len(lines):
            candidate = lines[index]
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate.strip() and candidate_indent <= indent:
                break
            block.append(candidate)
            index += 1
        blocks.append("\n".join(block))
    return blocks


def test_deploy_workflow_passes_commit_sha_to_cloud_run():
    workflow = Path(".github/workflows/deploy.yml").read_text()

    assert 'DEPLOY_SHA="$(git rev-parse HEAD)"' in workflow
    assert "DEPLOY_SHA=$DEPLOY_SHA" in workflow


def test_manual_staging_deploy_validates_exact_remote_branch_head_before_gcp_auth():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    staging_job = workflow.split("deploy-staging:", 1)[1].split("deploy-production:", 1)[0]

    assert "candidate_sha:" in workflow
    assert "inputs.candidate_sha" in staging_job
    assert "github.ref == 'refs/heads/main'" in staging_job
    assert "^[0-9a-f]{40}$" in staging_job
    assert "git ls-remote --heads origin" in staging_job
    assert "git rev-parse HEAD" in staging_job
    assert staging_job.index("Validate exact staging candidate") < staging_job.index(
        "Authenticate to GCP"
    )


def test_candidate_test_job_cannot_request_oidc_token():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    global_permissions = workflow.split("permissions:", 1)[1].split("env:", 1)[0]
    test_job = workflow.split("test:", 1)[1].split("deploy-staging:", 1)[0]
    staging_job = workflow.split("deploy-staging:", 1)[1].split("deploy-production:", 1)[0]
    production_job = workflow.split("deploy-production:", 1)[1]

    assert "id-token: write" not in global_permissions
    assert "id-token: write" not in test_job
    assert "id-token: write" in staging_job
    assert "id-token: write" in production_job


def test_candidate_test_job_does_not_persist_checkout_credentials():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    test_job = workflow.split("test:", 1)[1].split("deploy-staging:", 1)[0]

    assert "persist-credentials: false" in test_job


def test_deploy_health_check_requires_exact_deployed_sha():
    workflow = Path(".github/workflows/deploy.yml").read_text()

    assert 'get("deploy_sha", "")' in workflow
    assert 'test "$DEPLOYED_SHA" = "$DEPLOY_SHA"' in workflow


def test_production_deploy_rejects_candidate_override():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    production_job = workflow.split("deploy-production:", 1)[1]

    assert "Production does not accept candidate_sha" in production_job
    assert 'test -z "$CANDIDATE_SHA"' in production_job


def test_deploy_workflow_defaults_observation_shadow_off_and_removes_key():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    staging_job = workflow.split("deploy-staging:", 1)[1].split(
        "deploy-production:", 1
    )[0]
    production_job = workflow.split("deploy-production:", 1)[1]

    disabled = "RECEPTIONIST_OBSERVATION_SHADOW_ENABLED=false"
    remove_key = (
        "--remove-secrets RECEPTIONIST_OBSERVATION_SHADOW_CALLER_HMAC_KEY"
    )
    assert disabled in staging_job
    assert disabled in production_job
    assert remove_key in staging_job
    assert remove_key in production_job


def test_staging_release_paths_require_sandbox_apns_before_gcp_auth():
    deploy_workflow = Path(".github/workflows/deploy.yml").read_text()
    staging_job = deploy_workflow.split("deploy-staging:", 1)[1].split(
        "deploy-production:", 1
    )[0]
    rollback_workflow = Path(".github/workflows/rollback.yml").read_text()

    assert "APNS_SANDBOX: ${{ vars.APNS_SANDBOX }}" in staging_job
    assert 'test "$APNS_SANDBOX" = "true"' in staging_job
    assert staging_job.index('test "$APNS_SANDBOX" = "true"') < staging_job.index(
        "Authenticate to GCP"
    )
    assert "APNS_SANDBOX: ${{ vars.APNS_SANDBOX }}" in rollback_workflow
    assert 'test "$APNS_SANDBOX" = "true"' in rollback_workflow
    assert rollback_workflow.index(
        'test "$APNS_SANDBOX" = "true"'
    ) < rollback_workflow.index("Authenticate to GCP")


def test_production_release_paths_require_twilio_boundary_before_gcp_auth():
    deploy_workflow = Path(".github/workflows/deploy.yml").read_text()
    production_job = deploy_workflow.split("deploy-production:", 1)[1]
    rollback_workflow = Path(".github/workflows/rollback.yml").read_text()

    check = 'test -n "$PRODUCTION_TWILIO_ACCOUNT_SID"'
    assert check in production_job
    assert production_job.index(check) < production_job.index("Authenticate to GCP")
    assert check in rollback_workflow
    assert rollback_workflow.index(check) < rollback_workflow.index(
        "Authenticate to GCP"
    )


def test_rollback_serializes_with_deployments_for_selected_environment():
    deploy_workflow = Path(".github/workflows/deploy.yml").read_text()
    rollback_workflow = Path(".github/workflows/rollback.yml").read_text()

    assert "group: deploy-staging" in deploy_workflow
    assert "group: deploy-production" in deploy_workflow
    assert "group: deploy-${{ inputs.environment }}" in rollback_workflow
    assert "cancel-in-progress: false" in rollback_workflow


def test_release_jobs_have_bounded_execution_time():
    deploy_workflow = Path(".github/workflows/deploy.yml").read_text()
    rollback_workflow = Path(".github/workflows/rollback.yml").read_text()

    assert deploy_workflow.count("timeout-minutes:") == 3
    assert rollback_workflow.count("timeout-minutes:") == 1


def test_rollback_redeploy_pins_remote_tag_and_production_main_commit():
    workflow = Path(".github/workflows/rollback.yml").read_text()

    assert "git ls-remote --exit-code --refs --tags origin" in workflow
    assert 'test "$LOCAL_TAG_OBJECT_SHA" = "$REMOTE_TAG_OBJECT_SHA"' in workflow
    assert 'ROLLBACK_COMMIT_SHA="$(git rev-parse' in workflow
    assert 'git merge-base --is-ancestor "$ROLLBACK_COMMIT_SHA" "origin/main"' in workflow
    assert "Production redeploy tags must start with prod-" in workflow
    assert 'git checkout --detach "$ROLLBACK_COMMIT_SHA"' in workflow
    assert 'test "$DEPLOY_SHA" = "$ROLLBACK_COMMIT_SHA"' in workflow


def test_release_health_checks_retry_transient_failures_with_timeouts():
    workflows = "\n".join(
        Path(path).read_text()
        for path in (".github/workflows/deploy.yml", ".github/workflows/rollback.yml")
    )

    assert workflows.count("--retry 5") == 3
    assert workflows.count("--retry-all-errors") == 3
    assert workflows.count("--connect-timeout 5") == 3
    assert workflows.count("--max-time 30") == 3


def test_temporary_gcp_credentials_are_excluded_from_build_contexts():
    pattern = "gha-creds-*.json"
    gcloudignore = Path(".gcloudignore").read_text().splitlines()

    assert pattern in Path(".gitignore").read_text().splitlines()
    assert pattern in gcloudignore
    assert "**" in gcloudignore
    assert "!Dockerfile" in gcloudignore
    assert "!pyproject.toml" in gcloudignore
    assert "!app/**" in gcloudignore
    assert gcloudignore.index(pattern) > gcloudignore.index("!app/**")
    assert "**/.env" in gcloudignore
    assert "**/*.p8" in gcloudignore
    assert pattern in Path(".dockerignore").read_text().splitlines()


def test_source_deploys_use_workflow_owned_cloud_build_allowlist():
    deploy_workflow = Path(".github/workflows/deploy.yml").read_text()
    rollback_workflow = Path(".github/workflows/rollback.yml").read_text()
    ignore_flag = '--ignore-file "$RUNNER_TEMP/kevin-release.gcloudignore"'

    for workflow in (deploy_workflow, rollback_workflow):
        assert "CLOUD_BUILD_IGNORE_POLICY: |-" in workflow
        assert 'printf \'%s\\n\' "$CLOUD_BUILD_IGNORE_POLICY"' in workflow
        assert "gha-creds-*.json" in workflow
        assert "!Dockerfile" in workflow
        assert "!app/**" in workflow

    assert deploy_workflow.count("--source .") == 2
    assert deploy_workflow.count(ignore_flag) == 2
    assert rollback_workflow.count("--source .") == 1
    assert rollback_workflow.count(ignore_flag) == 1


def test_public_apple_trust_anchors_are_included_in_docker_context():
    dockerignore = Path(".dockerignore").read_text().splitlines()
    trust_anchor_rule = "!app/services/apple_certs/*.cer"

    assert trust_anchor_rule in dockerignore
    assert dockerignore.index(trust_anchor_rule) > dockerignore.index("*.cer")
    assert Path("app/services/apple_certs/AppleRootCA-G2.cer").is_file()
    assert Path("app/services/apple_certs/AppleRootCA-G3.cer").is_file()


def test_rollback_inputs_are_validated_and_not_interpolated_into_shell_commands():
    workflow = Path(".github/workflows/rollback.yml").read_text()

    assert "Validate rollback target" in workflow
    assert 'git checkout --detach "$ROLLBACK_COMMIT_SHA"' in workflow
    assert '--to-revisions="${REVISION_OR_TAG}=100"' in workflow
    assert "git checkout ${{ inputs.revision_or_tag }}" not in workflow
    assert "--to-revisions=${{ inputs.revision_or_tag }}" not in workflow
    assert 'test "$CONFIRM_PRODUCTION" = "production"' in workflow


def test_workflow_shell_blocks_do_not_directly_interpolate_inputs_or_variables():
    for path in (".github/workflows/deploy.yml", ".github/workflows/rollback.yml"):
        run_script = "\n".join(_workflow_run_blocks(Path(path).read_text()))

        assert "${{ inputs." not in run_script
        assert "${{ vars." not in run_script


def test_release_smoke_script_checks_health_admin_and_authenticated_api():
    script = Path("scripts/smoke_release.sh")

    assert script.exists()

    content = script.read_text()
    assert "/health" in content
    assert "/admin" in content
    assert "/api/admin/overview" in content
    assert "ADMIN_API_TOKEN" in content
