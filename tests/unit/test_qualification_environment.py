"""Gate 0B preregistration and execution identity parity tests."""

from hashlib import sha256

import app.services.qualification_environment as environment_module
from app.services.qualification_identity import canonical_json_bytes
import scripts.run_gemini_caller_turn_qualification as runner_module
import scripts.verify_qualification_environment as verifier_module


SOURCE_SHA = "b" * 40


class _Identity:
    def __init__(self, value: str) -> None:
        self._value = value

    def redacted_report_dict(self) -> dict[str, str]:
        return {"identity": self._value}


def test_verifier_and_runtime_use_the_same_environment_identity_contract(monkeypatch) -> None:
    source_calls: list[tuple[object, str, tuple[str, ...]]] = []
    environment_calls: list[tuple[object, str, str, tuple[str, ...]]] = []

    def capture_source(repo_root, *, expected_source_sha, dependency_paths):
        source_calls.append((repo_root, expected_source_sha, tuple(dependency_paths)))
        return _Identity("source")

    def capture_environment(*, repo_root, expected_python, expected_uv, import_names):
        environment_calls.append(
            (repo_root, expected_python, expected_uv, tuple(import_names))
        )
        return _Identity("environment")

    monkeypatch.setattr(environment_module, "capture_source_identity", capture_source)
    monkeypatch.setattr(
        environment_module,
        "capture_environment_identity",
        capture_environment,
    )

    verifier_report = verifier_module._identity_report(SOURCE_SHA)
    runtime_digest = runner_module._capture_current_execution_identity(
        expected_source_sha=SOURCE_SHA
    )

    assert runtime_digest == sha256(canonical_json_bytes(verifier_report)).hexdigest()
    assert source_calls[0] == source_calls[1]
    assert environment_calls[0] == environment_calls[1]
    assert "app/services/qualification_ledger.py" in source_calls[0][2]
    assert "app.services.qualification_ledger" in environment_calls[0][3]
