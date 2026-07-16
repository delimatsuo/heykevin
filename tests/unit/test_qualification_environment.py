"""Gate 0B preregistration and execution identity parity tests."""

from copy import deepcopy
from hashlib import sha256

import app.services.qualification_environment as environment_module
from app.services.qualification_identity import canonical_json_bytes
import pytest
import scripts.run_gemini_caller_turn_qualification as runner_module
import scripts.verify_qualification_environment as verifier_module


SOURCE_SHA = "b" * 40


class _Identity:
    def __init__(self, value: dict[str, object]) -> None:
        self._value = value

    def redacted_report_dict(self) -> dict[str, object]:
        return self._value


def test_verifier_and_runtime_use_the_same_environment_identity_contract(monkeypatch) -> None:
    source_calls: list[tuple[object, str, tuple[str, ...]]] = []
    environment_calls: list[tuple[object, str, str, tuple[str, ...]]] = []

    def capture_source(repo_root, *, expected_source_sha, dependency_paths):
        source_calls.append((repo_root, expected_source_sha, tuple(dependency_paths)))
        return _Identity(
            {
                "source_sha": SOURCE_SHA,
                "clean": True,
                "dependencies": {
                    "0" * 64: {
                        "worktree_sha256": "1" * 64,
                        "git_blob_id": "2" * 40,
                    }
                },
            }
        )

    def capture_environment(*, repo_root, expected_python, expected_uv, import_names):
        environment_calls.append(
            (repo_root, expected_python, expected_uv, tuple(import_names))
        )
        return _Identity(
            {
                "python_version": "3.12.13",
                "uv_version": "0.11.7",
                "python_executable_sha256": "3" * 64,
                "uv_executable_sha256": "4" * 64,
                "python_executable_location_sha256": "5" * 64,
                "uv_executable_location_sha256": "6" * 64,
                "runtime_image_kind": "interpreter",
                "runtime_image_sha256": "3" * 64,
                "platform_id": "darwin-test",
                "architecture": "arm64",
                "unicode_version": "15.0.0",
                "monotonic_clock_implementation": "mach_absolute_time",
                "monotonic_clock_resolution_ns": 1,
                "bytecode_write_disabled": True,
                "openssl_version": "OpenSSL 3.0.0",
                "ca_bundle_sha256": "7" * 64,
                "lock_sha256": "8" * 64,
                "codec_golden_sha256": "9" * 64,
                "import_sha256": {"app.services.example": "a" * 64},
                "distributions": {"test-package": "1.0.0"},
                "distribution_files_sha256": {"test-package": "b" * 64},
            }
        )

    monkeypatch.setattr(environment_module, "capture_source_identity", capture_source)
    monkeypatch.setattr(
        environment_module,
        "capture_environment_identity",
        capture_environment,
    )

    verifier_report = verifier_module._identity_report(SOURCE_SHA)
    runtime_identity = runner_module._capture_current_execution_identity(
        expected_source_sha=SOURCE_SHA
    )

    assert runtime_identity.sha256 == sha256(canonical_json_bytes(verifier_report)).hexdigest()
    assert runtime_identity.report == verifier_report
    assert source_calls[0] == source_calls[1]
    assert environment_calls[0] == environment_calls[1]
    assert "app/services/qualification_ledger.py" in source_calls[0][2]
    assert "app/services/qualification_allocation.py" in source_calls[0][2]
    assert "app/services/qualification_privacy.py" in source_calls[0][2]
    assert "app/services/qualification_private_paths.py" in source_calls[0][2]
    assert "tests/fixtures/caller_turn_qualification/pricing.json" in source_calls[0][2]
    assert "app.services.qualification_ledger" in environment_calls[0][3]
    assert "app.services.qualification_allocation" in environment_calls[0][3]
    assert "app.services.qualification_privacy" in environment_calls[0][3]
    assert "app.services.qualification_private_paths" in environment_calls[0][3]

    wrong_version = deepcopy(verifier_report)
    wrong_version["environment"]["python_version"] = "3.12.12"
    with pytest.raises(ValueError, match="runtime policy"):
        environment_module.validate_execution_identity_report(wrong_version)

    inconsistent_distribution = deepcopy(verifier_report)
    inconsistent_distribution["environment"]["distributions"] = {
        "other-package": "1.0.0"
    }
    with pytest.raises(ValueError, match="distribution identities"):
        environment_module.validate_execution_identity_report(inconsistent_distribution)
