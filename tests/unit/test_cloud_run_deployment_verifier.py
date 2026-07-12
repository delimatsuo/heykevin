"""Exact Cloud Run deployment identity verifier tests."""

import json
import os
from pathlib import Path
import subprocess


SCRIPT = Path("scripts/verify_cloud_run_deployment.sh")
SHA = "a" * 40
REVISION = "kevin-api-staging-00066-abc"


def _write_command(directory: Path, name: str, body: str) -> None:
    command = directory / name
    command.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
    command.chmod(0o755)


def _run_verifier(
    tmp_path: Path,
    *,
    traffic_percent: int = 100,
    health_sha: str = SHA,
    health_marker: str = "",
    latest_created: str = REVISION,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    service_document = {
        "status": {
            "latestCreatedRevisionName": latest_created,
            "latestReadyRevisionName": REVISION,
            "traffic": [{"revisionName": REVISION, "percent": traffic_percent}],
            "url": "https://kevin-api-staging-example-uc.a.run.app",
        }
    }
    health_document = {
        "status": "ok",
        "environment": "staging",
        "service": "kevin-api-staging",
        "revision": REVISION,
        "deploy_sha": health_sha,
        "marker": health_marker,
    }
    _write_command(fake_bin, "gcloud", f"printf '%s' '{json.dumps(service_document)}'")
    _write_command(fake_bin, "curl", f"printf '%s' '{json.dumps(health_document)}'")
    _write_command(fake_bin, "sleep", "exit 0")
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "kevin-api-staging",
            "kevin-491315",
            "us-central1",
            "staging",
            SHA,
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def test_deployment_verifier_proves_exact_serving_identity(tmp_path):
    result = _run_verifier(tmp_path)

    assert result.returncode == 0
    assert "status=verified" in result.stdout
    assert REVISION in result.stdout


def test_deployment_verifier_rejects_wrong_sha_without_echoing_health(tmp_path):
    marker = "private-health-body-marker"

    result = _run_verifier(tmp_path, health_sha="b" * 40, health_marker=marker)

    assert result.returncode != 0
    assert marker not in result.stdout
    assert marker not in result.stderr


def test_deployment_verifier_rejects_stale_traffic(tmp_path):
    result = _run_verifier(tmp_path, traffic_percent=0)

    assert result.returncode != 0


def test_deployment_verifier_rejects_unready_latest_creation(tmp_path):
    result = _run_verifier(
        tmp_path,
        latest_created="kevin-api-staging-00067-def",
    )

    assert result.returncode != 0
