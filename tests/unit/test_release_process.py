"""Release process hardening tests and workflow contract verification."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _get_workflow_triggers(workflow_dict: dict) -> dict:
    on = workflow_dict.get("on")
    if on is None:
        on = workflow_dict.get(True)
    if on is None:
        raise AssertionError("Workflow is missing 'on' trigger specification")
    return on


def validate_deploy_workflow_contract(workflow_text: str) -> None:
    # Forbidden tokens across entire workflow text
    assert "secrets." not in workflow_text, "Workflow must not reference secrets.* (use vars.* or OIDC)"
    assert "pull_request_target" not in workflow_text, "pull_request_target is strictly forbidden"
    assert "self-hosted" not in workflow_text, "self-hosted runners are strictly forbidden"
    assert "macos" not in workflow_text.lower(), "macOS runners are strictly forbidden"

    data = yaml.safe_load(workflow_text)
    assert isinstance(data, dict), "Workflow YAML must parse to a dictionary"

    triggers = _get_workflow_triggers(data)

    # Forbidden triggers & path filters
    assert "schedule" not in triggers, "schedules are not permitted"
    assert "paths" not in triggers, "Workflow-level paths filter is forbidden"
    assert "paths-ignore" not in triggers, "Workflow-level paths-ignore filter is forbidden"

    # PR trigger covers main and staging
    pr_config = triggers.get("pull_request")
    assert pr_config is not None, "pull_request trigger must be defined"
    pr_branches = pr_config.get("branches", [])
    assert "main" in pr_branches and "staging" in pr_branches, "PR trigger must target main and staging"
    assert "paths" not in pr_config and "paths-ignore" not in pr_config, "PR path filters are forbidden"

    # Push trigger covers staging
    push_config = triggers.get("push")
    assert push_config is not None, "push trigger must be defined"
    push_branches = push_config.get("branches", [])
    assert "staging" in push_branches, "push trigger must target staging"
    assert "paths" not in push_config and "paths-ignore" not in push_config, "Push path filters are forbidden"

    # Workflow dispatch inputs
    dispatch_config = triggers.get("workflow_dispatch")
    assert dispatch_config is not None, "workflow_dispatch must be defined"
    inputs = dispatch_config.get("inputs", {})
    assert "target" in inputs and "candidate_sha" in inputs, "workflow_dispatch missing required inputs"

    # Concurrency
    concurrency = data.get("concurrency")
    assert concurrency is not None, "Workflow-level concurrency must be configured"
    group = str(concurrency.get("group", ""))
    assert "github.event_name" in group, "Concurrency group must include event name"
    assert "pull_request" in group and "ref" in group, "Concurrency group must include PR number or ref"
    cancel_in_progress = str(concurrency.get("cancel-in-progress", ""))
    assert "github.event_name == 'pull_request'" in cancel_in_progress, (
        "cancel-in-progress must be true only for pull_request"
    )

    # Permissions
    permissions = data.get("permissions", {})
    assert permissions == {"contents": "read"}, "Global permissions must be least privilege 'contents: read'"

    # Jobs
    jobs = data.get("jobs", {})
    expected_jobs = {"test_suites", "quality", "test", "deploy-staging", "deploy-production"}
    assert set(jobs.keys()) == expected_jobs, f"Exact jobs mismatch: {set(jobs.keys())} vs {expected_jobs}"

    # Pinned actions check across all jobs
    full_sha_action_regex = re.compile(r"^[a-zA-Z0-9_\-\./]+@[0-9a-f]{40}(\s*#.*)?$")
    for j_name, job in jobs.items():
        assert job.get("runs-on") == "ubuntu-latest", f"Job {j_name} must run on ubuntu-latest"
        assert isinstance(job.get("timeout-minutes"), int) and job["timeout-minutes"] > 0, (
            f"Job {j_name} missing positive timeout-minutes"
        )

        steps = job.get("steps", [])
        for step in steps:
            if "uses" in step:
                uses_val = step["uses"].strip()
                assert full_sha_action_regex.match(uses_val), (
                    f"Action in job {j_name} step '{step.get('name')}' must use exact pinned SHA: {uses_val}"
                )

    # test_suites job validation
    suites_job = jobs["test_suites"]
    assert "if" not in suites_job, "test_suites job must not have a job-level 'if' condition"
    assert suites_job.get("name") == "Python suite ${{ matrix.shard }}/7"
    assert suites_job.get("timeout-minutes") == 15
    suites_perm = suites_job.get("permissions")
    if isinstance(suites_perm, dict):
        assert "id-token" not in suites_perm, "test_suites must not grant id-token"
    strategy = suites_job.get("strategy", {})
    assert strategy.get("fail-fast") is False, "test_suites strategy.fail-fast must be False"
    matrix = strategy.get("matrix", {})
    assert matrix.get("shard") == [1, 2, 3, 4, 5, 6, 7], "test_suites matrix.shard must be exactly [1, 2, 3, 4, 5, 6, 7]"

    # test_suites steps
    suite_steps = suites_job.get("steps", [])
    checkout_step = suite_steps[0]
    assert "actions/checkout" in checkout_step.get("uses", "")
    with_clause = checkout_step.get("with", {})
    assert with_clause.get("fetch-depth") == 0
    assert with_clause.get("persist-credentials") is False
    ref_expr = str(with_clause.get("ref", ""))
    assert "github.event_name == 'pull_request'" in ref_expr
    assert "github.event.pull_request.head.sha" in ref_expr
    assert "inputs.candidate_sha" in ref_expr
    assert "github.sha" in ref_expr

    # Exact head validation step in test_suites
    head_val_step = suite_steps[1]
    head_run = head_val_step.get("run", "")
    assert "set -euo pipefail" in head_run
    assert "^[0-9a-f]{40}$" in head_run
    assert "git rev-parse HEAD" in head_run
    assert 'test "$ACTUAL_SHA" = "$EXPECTED_SHA"' in head_run or 'test "$EXPECTED_SHA" = "$ACTUAL_SHA"' in head_run

    # Partition tests step in test_suites
    run_shard_step = next(s for s in suite_steps if "scripts/partition_tests.py" in s.get("run", ""))
    run_script = run_shard_step.get("run", "")
    assert "set -euo pipefail" in run_script
    assert "--total-shards 7" in run_script
    assert "mktemp" in run_script
    assert "trap" in run_script
    assert "mapfile" in run_script
    assert '"${test_files[@]}"' in run_script
    assert '"${#test_files[@]}"' in run_script or '${#test_files[@]}' in run_script
    assert 'test -n "$f"' in run_script or 'test -n "$file"' in run_script
    assert "python -m pytest" in run_script

    # quality job validation
    quality_job = jobs["quality"]
    assert "if" not in quality_job, "quality job must not have a job-level 'if' condition"
    assert quality_job.get("name") == "Python quality"
    assert quality_job.get("timeout-minutes") == 15
    quality_perm = quality_job.get("permissions")
    if isinstance(quality_perm, dict):
        assert "id-token" not in quality_perm, "quality must not grant id-token"

    quality_steps = quality_job.get("steps", [])
    q_checkout = quality_steps[0]
    assert "actions/checkout" in q_checkout.get("uses", "")
    assert q_checkout.get("with", {}).get("persist-credentials") is False
    assert q_checkout.get("with", {}).get("fetch-depth") == 0
    q_ref_expr = str(q_checkout.get("with", {}).get("ref", ""))
    assert "github.event_name == 'pull_request'" in q_ref_expr
    assert "github.event.pull_request.head.sha" in q_ref_expr
    assert "inputs.candidate_sha" in q_ref_expr
    assert "github.sha" in q_ref_expr

    q_head_val = quality_steps[1]
    q_head_run = q_head_val.get("run", "")
    assert "set -euo pipefail" in q_head_run
    assert "^[0-9a-f]{40}$" in q_head_run
    assert "git rev-parse HEAD" in q_head_run
    assert 'test "$ACTUAL_SHA" = "$EXPECTED_SHA"' in q_head_run or 'test "$EXPECTED_SHA" = "$ACTUAL_SHA"' in q_head_run

    all_q_runs = "\n".join(s.get("run", "") for s in quality_steps if "run" in s)
    assert "ruff check --select E9,F63,F7,F82" in all_q_runs
    assert "python -m compileall" in all_q_runs
    assert "git diff --check" in all_q_runs
    assert "PR_BASE_SHA" in str(quality_steps) or "PR_BASE_SHA" in all_q_runs

    # test aggregator job validation
    test_agg = jobs["test"]
    assert test_agg.get("name") == "Test"
    assert test_agg.get("timeout-minutes") == 5
    assert test_agg.get("if") == "always()"
    test_perm = test_agg.get("permissions")
    if isinstance(test_perm, dict):
        assert "id-token" not in test_perm, "test aggregator must not grant id-token"
    needs = test_agg.get("needs", [])
    if isinstance(needs, str):
        needs = [needs]
    assert set(needs) == {"test_suites", "quality"}, f"test aggregator needs must be [test_suites, quality], got {needs}"
    # Aggregator must not have checkout
    for s in test_agg.get("steps", []):
        assert "actions/checkout" not in s.get("uses", ""), "Aggregator job 'test' must not check out code"
    agg_step = next(s for s in test_agg.get("steps", []) if "run" in s)
    agg_env = agg_step.get("env", {})
    assert agg_env.get("SUITES_RESULT") == "${{ needs.test_suites.result }}"
    assert agg_env.get("QUALITY_RESULT") == "${{ needs.quality.result }}"
    agg_run = agg_step.get("run", "")
    assert "set -euo pipefail" in agg_run
    assert re.search(r'\[\s*"\$SUITES_RESULT"\s*=\s*"success"\s*\]\s*&&\s*\[\s*"\$QUALITY_RESULT"\s*=\s*"success"\s*\]', agg_run) is not None, (
        "Aggregator predicate must require both SUITES_RESULT and QUALITY_RESULT equal success"
    )

    # Deploy jobs depend on test
    for deploy_job_name in ("deploy-staging", "deploy-production"):
        deploy_job = jobs[deploy_job_name]
        d_needs = deploy_job.get("needs", [])
        if isinstance(d_needs, str):
            d_needs = [d_needs]
        assert "test" in d_needs, f"Job {deploy_job_name} must depend on test gate ('needs: test')"
        d_checkout = next(s for s in deploy_job.get("steps", []) if "actions/checkout" in s.get("uses", ""))
        assert d_checkout.get("with", {}).get("persist-credentials") is False


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


def _staging_health_check_script(workflow: str) -> str:
    return next(
        block
        for block in _workflow_run_blocks(workflow)
        if "STAGING_TRAFFIC=$(gcloud run services describe" in block
    )


def _rollback_promo_containment_script(workflow: str) -> str:
    return next(
        block
        for block in _workflow_run_blocks(workflow)
        if "TARGET_PROMO_FLAG=$(gcloud run revisions describe" in block
    )


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
    data = yaml.safe_load(workflow)

    global_permissions = data.get("permissions", {})
    assert global_permissions == {"contents": "read"}
    assert "id-token" not in global_permissions

    jobs = data.get("jobs", {})
    for unpriv_job in ("test_suites", "quality", "test"):
        job_perms = jobs[unpriv_job].get("permissions")
        if isinstance(job_perms, dict):
            assert "id-token" not in job_perms

    for deploy_job_name in ("deploy-staging", "deploy-production"):
        deploy_perms = jobs[deploy_job_name].get("permissions", {})
        assert isinstance(deploy_perms, dict)
        assert deploy_perms.get("contents") == "read"
        assert deploy_perms.get("id-token") == "write"


def test_candidate_test_job_does_not_persist_checkout_credentials():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    data = yaml.safe_load(workflow)
    jobs = data.get("jobs", {})

    for job_name in ("test_suites", "quality", "deploy-staging", "deploy-production"):
        job = jobs[job_name]
        checkout_steps = [
            s for s in job.get("steps", []) if "actions/checkout" in s.get("uses", "")
        ]
        assert len(checkout_steps) >= 1, f"Job {job_name} missing checkout step"
        for chk in checkout_steps:
            assert chk.get("with", {}).get("persist-credentials") is False


def test_deploy_health_check_requires_exact_deployed_sha():
    workflow = Path(".github/workflows/deploy.yml").read_text()

    assert 'get("deploy_sha", "")' in workflow
    assert 'test "$DEPLOYED_SHA" = "$DEPLOY_SHA"' in workflow


def test_staging_health_check_uses_the_tagged_candidate_revision():
    workflow = Path(".github/workflows/deploy.yml").read_text()
    staging_job = workflow.split("deploy-staging:", 1)[1].split(
        "deploy-production:", 1
    )[0]

    assert "--tag staging" in staging_job
    staging_deploy = staging_job.split("Deploy to Cloud Run (Staging)", 1)[1].split(
        "Health check", 1
    )[0]
    assert "--no-traffic" in staging_deploy
    assert "--format='json(status.traffic)'" in staging_job
    assert 'entry.get("tag") == "staging"' in staging_job
    assert 'test -n "$STAGING_TAG_URL"' in staging_job
    assert '"$STAGING_TAG_URL/health"' in staging_job
    assert 'test -n "$STAGING_TAG_REVISION"' in staging_job
    assert 'test "$CANDIDATE_SHA" = "$DEPLOY_SHA"' in staging_job
    assert 'gcloud run services update-traffic "$STAGING_SERVICE"' in staging_job
    assert '--to-revisions="${STAGING_TAG_REVISION}=100"' in staging_job
    assert staging_job.index('test "$CANDIDATE_SHA" = "$DEPLOY_SHA"') < staging_job.index(
        'gcloud run services update-traffic "$STAGING_SERVICE"'
    )
    assert "--format='value(status.url)'" in staging_job
    assert '"$STAGING_URL/health"' in staging_job


def test_tagged_candidate_sha_mismatch_never_updates_staging_traffic(
    tmp_path: Path, monkeypatch
):
    workflow = Path(".github/workflows/deploy.yml").read_text()
    script = _staging_health_check_script(workflow)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    traffic_updated = tmp_path / "traffic-updated"

    gcloud = fake_bin / "gcloud"
    gcloud.write_text(
        """#!/bin/sh
if [ "$3" = "describe" ]; then
  printf '%s\\n' '{"status":{"traffic":[{"tag":"staging","url":"https://candidate.example","revisionName":"candidate-revision"}]}}'
  exit 0
fi
if [ "$3" = "update-traffic" ]; then
  : > "$UPDATE_TRAFFIC_MARKER"
fi
"""
    )
    gcloud.chmod(0o755)

    curl = fake_bin / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"deploy_sha\":\"mismatch\"}'\n"
    )
    curl.chmod(0o755)

    sleep = fake_bin / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    (fake_bin / "python").symlink_to(sys.executable)

    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GCP_PROJECT_ID", "project")
    monkeypatch.setenv("GCP_REGION", "region")
    monkeypatch.setenv("STAGING_SERVICE", "service")
    monkeypatch.setenv("DEPLOY_SHA", "expected")
    monkeypatch.setenv("UPDATE_TRAFFIC_MARKER", str(traffic_updated))

    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Tagged staging candidate deploy SHA mismatch" in result.stdout
    assert not traffic_updated.exists()


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


def test_release_paths_keep_subscription_promotions_disabled():
    deploy_workflow = Path(".github/workflows/deploy.yml").read_text()
    rollback_workflow = Path(".github/workflows/rollback.yml").read_text()
    disabled = "SUBSCRIPTION_PROMOTIONAL_OFFERS_ENABLED=false"

    assert deploy_workflow.count(disabled) == 2
    assert rollback_workflow.count(disabled) == 2
    assert "Verify rollback preserves subscription promo containment" in rollback_workflow
    assert "TARGET_PROMO_FLAG" in rollback_workflow
    assert "subscription_promotional_offers_enabled: bool = False" in rollback_workflow
    assert "if not settings.subscription_promotional_offers_enabled:" in rollback_workflow
    assert rollback_workflow.index(
        "Verify rollback preserves subscription promo containment"
    ) < rollback_workflow.index("Rollback via traffic split")


def test_traffic_split_rollback_refuses_revision_without_disabled_promo_flag(
    tmp_path: Path, monkeypatch
):
    workflow = Path(".github/workflows/rollback.yml").read_text()
    script = _rollback_promo_containment_script(workflow)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gcloud = fake_bin / "gcloud"
    gcloud.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"spec\":{\"containers\":[{\"env\":["
        "{\"name\":\"DEPLOY_SHA\",\"value\":\"0000000000000000000000000000000000000000\"}"
        "]}]}}'\n"
    )
    gcloud.chmod(0o755)
    (fake_bin / "python").symlink_to(sys.executable)

    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ROLLBACK_METHOD", "traffic-split")
    monkeypatch.setenv("REVISION_OR_TAG", "kevin-api-00001-old")
    monkeypatch.setenv("GCP_PROJECT_ID", "project")
    monkeypatch.setenv("GCP_REGION", "region")

    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Rollback revision does not keep promotional offers disabled" in result.stdout


def test_redeploy_tag_rollback_refuses_source_without_promo_guards(tmp_path: Path):
    workflow = Path(".github/workflows/rollback.yml").read_text()
    script = _rollback_promo_containment_script(workflow)
    repository = tmp_path / "repository"
    (repository / "app" / "api").mkdir(parents=True)
    (repository / "app" / "config.py").write_text("class Settings:\n    pass\n")
    (repository / "app" / "api" / "subscription.py").write_text(
        "async def get_promo_eligible():\n    return {'eligible': True}\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "app"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "pre-fix",
        ],
        cwd=repository,
        check=True,
    )
    target = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    env = os.environ.copy()
    env.update(
        {
            "ROLLBACK_METHOD": "redeploy-tag",
            "ROLLBACK_COMMIT_SHA": target,
        }
    )

    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=repository,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Rollback target lacks the promotional-offer default-off guard" in result.stdout


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

    data_deploy = yaml.safe_load(deploy_workflow)
    data_rollback = yaml.safe_load(rollback_workflow)

    for j_name, j_val in data_deploy.get("jobs", {}).items():
        assert isinstance(j_val.get("timeout-minutes"), int)
        assert j_val["timeout-minutes"] > 0

    for j_name, j_val in data_rollback.get("jobs", {}).items():
        assert isinstance(j_val.get("timeout-minutes"), int)
        assert j_val["timeout-minutes"] > 0


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

    assert workflows.count("--retry 5") == 4
    assert workflows.count("--retry-all-errors") == 4
    assert workflows.count("--connect-timeout 5") == 4
    assert workflows.count("--max-time 30") == 4


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
    policy_write = 'printf \'%s\\n\' "$CLOUD_BUILD_IGNORE_POLICY" > .gcloudignore'

    for workflow in (deploy_workflow, rollback_workflow):
        assert "CLOUD_BUILD_IGNORE_POLICY: |-" in workflow
        assert "--ignore-file" not in workflow
        assert "gha-creds-*.json" in workflow
        assert "!Dockerfile" in workflow
        assert "!app/**" in workflow

    assert deploy_workflow.count("--source .") == 2
    assert deploy_workflow.count(policy_write) == 2
    assert rollback_workflow.count("--source .") == 1
    assert rollback_workflow.count(policy_write) == 1
    assert rollback_workflow.index('git checkout --detach "$ROLLBACK_COMMIT_SHA"') < (
        rollback_workflow.index(policy_write)
    ) < rollback_workflow.index('gcloud run deploy "$SERVICE"')


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


# ==============================================================================
# Structural Workflow Contract & Mutation Effectiveness Tests
# ==============================================================================


def test_deploy_workflow_satisfies_contract():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    validate_deploy_workflow_contract(workflow_text)


def test_workflow_contract_fails_when_shards_changed_to_six():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated = workflow_text.replace(
        "shard: [1, 2, 3, 4, 5, 6, 7]", "shard: [1, 2, 3, 4, 5, 6]"
    )
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated)


def test_workflow_contract_fails_when_partition_total_changed_from_seven():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated = workflow_text.replace("--total-shards 7", "--total-shards 6")
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated)


def test_workflow_contract_fails_when_exact_head_equality_removed():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated = workflow_text.replace('test "$ACTUAL_SHA" = "$EXPECTED_SHA"', 'true')
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated)


def test_workflow_contract_fails_when_exact_head_git_rev_parse_removed():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated = workflow_text.replace('ACTUAL_SHA="$(git rev-parse HEAD)"', 'ACTUAL_SHA="$EXPECTED_SHA"')
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated)


def test_workflow_contract_fails_when_aggregator_predicate_weakened_to_ignore_quality():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated = workflow_text.replace(
        '[ "$SUITES_RESULT" = "success" ] && [ "$QUALITY_RESULT" = "success" ]',
        '[ "$SUITES_RESULT" = "success" ]',
    )
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated)


def test_workflow_contract_fails_when_aggregator_dependency_weakened():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated = workflow_text.replace(
        "needs: [test_suites, quality]", "needs: [test_suites]"
    )
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated)

    mutated_if = workflow_text.replace("if: always()", "if: success()")
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated_if)


def test_workflow_contract_fails_when_job_level_if_added_to_test_suites_or_quality():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated_suites = workflow_text.replace(
        "    timeout-minutes: 15\n    strategy:",
        "    timeout-minutes: 15\n    if: github.event_name == 'pull_request'\n    strategy:",
    )
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated_suites)

    mutated_quality = workflow_text.replace(
        "  quality:\n    name: Python quality\n    runs-on: ubuntu-latest\n    timeout-minutes: 15",
        "  quality:\n    name: Python quality\n    runs-on: ubuntu-latest\n    timeout-minutes: 15\n    if: github.event_name == 'pull_request'",
    )
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated_quality)


def test_workflow_contract_fails_on_forbidden_pull_request_target():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated = workflow_text.replace("pull_request:", "pull_request_target:")
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated)


def test_workflow_contract_fails_on_secret_reference():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated = workflow_text.replace("TEST_SHARD: ${{ matrix.shard }}", "SECRET_TOKEN: ${{ secrets.SOME_SECRET }}")
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated)


def test_workflow_contract_fails_on_path_filters_in_pull_request_or_push():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated_pr = workflow_text.replace("branches: [staging, main]", "branches: [staging, main]\n    paths:\n      - 'app/**'")
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated_pr)

    mutated_push = workflow_text.replace("branches: [staging]", "branches: [staging]\n    paths-ignore:\n      - 'docs/**'")
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated_push)


def test_workflow_contract_fails_when_deploy_jobs_do_not_need_test():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated_stg = workflow_text.replace(
        "  deploy-staging:\n    name: Deploy to Staging\n    needs: test",
        "  deploy-staging:\n    name: Deploy to Staging\n    needs: quality",
    )
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated_stg)

    mutated_prod = workflow_text.replace(
        "  deploy-production:\n    name: Deploy to Production\n    needs: test",
        "  deploy-production:\n    name: Deploy to Production\n    needs: quality",
    )
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated_prod)


def test_workflow_contract_fails_when_shard_mapfile_omitted():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated = workflow_text.replace('mapfile -t test_files < "$SHARD_FILE"', 'test_files=$(cat "$SHARD_FILE")')
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated)


def test_workflow_contract_fails_on_unpinned_action():
    workflow_text = Path(".github/workflows/deploy.yml").read_text()
    mutated = workflow_text.replace(
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
        "actions/checkout@v6",
    )
    with pytest.raises(AssertionError):
        validate_deploy_workflow_contract(mutated)
