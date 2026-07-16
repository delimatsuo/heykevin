"""Gate 0B preregistration and execution identity parity tests."""

from copy import deepcopy
from hashlib import sha256

import app.services.qualification_environment as environment_module
from app.services.qualification_identity import (
    EXECUTION_DEPENDENCY_PATHS,
    IdentityError,
    canonical_json_bytes,
)
import pytest
import scripts.run_gemini_caller_turn_qualification as runner_module
import scripts.verify_qualification_environment as verifier_module


SOURCE_SHA = "b" * 40
NATIVE_RUNTIME_CLOSURE = {
    "schema_id": "gate_0b_native_runtime_closure_v1",
    "regular_file_count": 2,
    "regular_files_sha256": "9" * 64,
    "virtual_dependency_count": 1,
    "virtual_dependencies_sha256": "a" * 64,
    "system_loader_identity_sha256": "b" * 64,
}
NATIVE_RUNTIME_CLOSURE["closure_sha256"] = sha256(
    canonical_json_bytes(NATIVE_RUNTIME_CLOSURE)
).hexdigest()
INTERPRETER_INSTALLATION = {
    "schema_id": "gate_0b_interpreter_installation_v2",
    "python_executable_sha256": "3" * 64,
    "stdlib_source_bytecode_sha256": "4" * 64,
    "stdlib_source_bytecode_count": 1,
    "stdlib_archive_sha256": "5" * 64,
    "stdlib_archive_count": 0,
    "native_extension_sha256": "6" * 64,
    "native_extension_count": 1,
    "native_runtime_closure": NATIVE_RUNTIME_CLOSURE,
}
INTERPRETER_INSTALLATION["installation_sha256"] = sha256(
    canonical_json_bytes(INTERPRETER_INSTALLATION)
).hexdigest()
RUNTIME_SITE_PACKAGES_MANIFEST = {
    "schema_id": "gate_0b_runtime_site_packages_v1",
    "source_count": 1,
    "bytecode_count": 0,
    "native_extension_count": 1,
    "metadata_data_count": 1,
    "file_count": 3,
    "files_sha256": "8" * 64,
}
RUNTIME_SITE_PACKAGES_MANIFEST["manifest_sha256"] = sha256(
    canonical_json_bytes(RUNTIME_SITE_PACKAGES_MANIFEST)
).hexdigest()
SOURCE_PREFLIGHT = {
    "source_sha": SOURCE_SHA,
    "clean": True,
    "dependencies": {
        sha256(path.encode("utf-8")).hexdigest(): {
            "worktree_sha256": "1" * 64,
            "git_blob_id": "2" * 40,
        }
        for path in EXECUTION_DEPENDENCY_PATHS
    },
}
STARTUP_POLICY = {
    "schema_id": "gate_0b_trusted_startup_policy_v5",
    "startup_flags": {
        "bytes_warning": 0,
        "debug": 0,
        "dev_mode": False,
        "dont_write_bytecode": 1,
        "hash_randomization": 1,
        "ignore_environment": 1,
        "inspect": 0,
        "int_max_str_digits": 4300,
        "interactive": 0,
        "isolated": 1,
        "no_site": 1,
        "no_user_site": 1,
        "optimize": 0,
        "quiet": 0,
        "safe_path": True,
        "utf8_mode": 0,
        "verbose": 0,
        "warn_default_encoding": 0,
    },
    "bytecode_write_disabled": True,
    "pycache_prefix_location_sha256": "2" * 64,
    "repo_root_location_sha256": "c" * 64,
    "python_executable_location_sha256": "d" * 64,
    "runtime_site_packages_location_sha256": "e" * 64,
    "effective_sys_path_sha256": "f" * 64,
    "effective_sys_path_entry_sha256": ["0" * 64, "1" * 64],
    "neutralized_environment": ["PYTHONHOME", "PYTHONPATH"],
    "runtime_pth_files_sha256": {},
    "ignored_startup_hook_files_sha256": {},
    "source_preflight": SOURCE_PREFLIGHT,
    "interpreter_installation": INTERPRETER_INSTALLATION,
    "runtime_site_packages_manifest": RUNTIME_SITE_PACKAGES_MANIFEST,
}


class _Identity:
    def __init__(self, value: dict[str, object]) -> None:
        self._value = value

    def redacted_report_dict(self) -> dict[str, object]:
        return self._value

    def policy_report_dict(self) -> dict[str, object]:
        return self._value


def test_verifier_requires_trusted_startup_before_building_an_identity_claim(
    monkeypatch,
    capsys,
) -> None:
    def reject_startup(*_args, **_kwargs):
        raise IdentityError("trusted qualification startup is unavailable")

    def identity_must_not_run(*_args, **_kwargs):
        raise AssertionError("identity capture ran before startup validation")

    monkeypatch.setattr(
        verifier_module,
        "capture_trusted_startup_identity",
        reject_startup,
    )
    monkeypatch.setattr(verifier_module, "_identity_report", identity_must_not_run)

    assert verifier_module.main(["--phase", "before"]) == 1
    assert capsys.readouterr().out.strip() == (
        '{"error_code":"identity_verification_failed","status":"fail"}'
    )


def test_verifier_binds_trusted_startup_to_before_and_after_snapshot(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    startup_report = deepcopy(STARTUP_POLICY)
    snapshot = tmp_path / "environment.json"
    monkeypatch.setattr(
        verifier_module,
        "capture_trusted_startup_identity",
        lambda *_args, **_kwargs: _Identity(startup_report),
    )
    monkeypatch.setattr(
        verifier_module,
        "_identity_report",
        lambda _source_sha, *, trusted_startup: {
            "schema_id": "gate_0b_environment_identity_v6",
            "trusted_startup": deepcopy(trusted_startup),
        },
    )
    monkeypatch.setattr(verifier_module, "_snapshot_path", lambda _source_sha: snapshot)

    assert verifier_module.main(["--phase", "before"]) == 0
    capsys.readouterr()
    startup_report["effective_sys_path_sha256"] = "a" * 64

    assert verifier_module.main(["--phase", "after"]) == 1
    assert snapshot.exists()
    assert capsys.readouterr().out.strip() == (
        '{"error_code":"identity_verification_failed","status":"fail"}'
    )


def test_verifier_and_runtime_use_the_same_environment_identity_contract(monkeypatch) -> None:
    source_calls: list[tuple[object, str, tuple[str, ...]]] = []
    environment_calls: list[
        tuple[
            object,
            str,
            str,
            tuple[str, ...],
            dict[str, object],
            dict[str, object],
        ]
    ] = []

    def capture_source(repo_root, *, expected_source_sha, dependency_paths):
        source_calls.append((repo_root, expected_source_sha, tuple(dependency_paths)))
        return _Identity(
            {
                "source_sha": SOURCE_SHA,
                "clean": True,
                "dependencies": {
                    sha256(path.encode("utf-8")).hexdigest(): {
                        "worktree_sha256": "1" * 64,
                        "git_blob_id": "2" * 40,
                    }
                    for path in EXECUTION_DEPENDENCY_PATHS
                },
            }
        )

    def capture_environment(
        *,
        repo_root,
        expected_python,
        expected_uv,
        import_names,
        expected_interpreter_installation,
        expected_runtime_site_packages_manifest,
    ):
        environment_calls.append(
            (
                repo_root,
                expected_python,
                expected_uv,
                tuple(import_names),
                expected_interpreter_installation,
                expected_runtime_site_packages_manifest,
            )
        )
        return _Identity(
            {
                "python_version": "3.12.13",
                "uv_version": "0.11.7",
                "python_executable_sha256": "3" * 64,
                "uv_executable_sha256": "4" * 64,
                "python_executable_location_sha256": "5" * 64,
                "uv_executable_location_sha256": "6" * 64,
                "interpreter_installation": deepcopy(INTERPRETER_INSTALLATION),
                "runtime_site_packages_manifest": deepcopy(
                    RUNTIME_SITE_PACKAGES_MANIFEST
                ),
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
    startup = _Identity(deepcopy(STARTUP_POLICY))
    monkeypatch.setattr(
        runner_module,
        "capture_trusted_startup_identity",
        lambda *_args, **_kwargs: startup,
    )

    verifier_report = verifier_module._identity_report(
        SOURCE_SHA,
        trusted_startup=startup.policy_report_dict(),
    )
    runtime_identity = runner_module._capture_current_execution_identity(
        expected_source_sha=SOURCE_SHA
    )

    assert runtime_identity.sha256 == sha256(canonical_json_bytes(verifier_report)).hexdigest()
    assert runtime_identity.report == verifier_report
    assert source_calls[0] == source_calls[1]
    assert environment_calls[0] == environment_calls[1]
    assert environment_calls[0][4] == INTERPRETER_INSTALLATION
    assert environment_calls[0][5] == RUNTIME_SITE_PACKAGES_MANIFEST
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


def test_execution_identity_rejects_copied_preregistration_source_before_recapture(
    monkeypatch,
) -> None:
    def identity_must_not_run(*_args, **_kwargs):
        raise AssertionError("identity recapture ran before startup source validation")

    monkeypatch.setattr(
        environment_module,
        "capture_source_identity",
        identity_must_not_run,
    )
    monkeypatch.setattr(
        environment_module,
        "capture_environment_identity",
        identity_must_not_run,
    )

    with pytest.raises(IdentityError, match="startup source SHA mismatch"):
        environment_module.build_execution_identity_report(
            ".",
            expected_source_sha="c" * 40,
            trusted_startup=deepcopy(STARTUP_POLICY),
        )
