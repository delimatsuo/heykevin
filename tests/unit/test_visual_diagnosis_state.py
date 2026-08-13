"""A2 pure state-machine tests with an independent pre-import guard."""

from __future__ import annotations

import ast
import hashlib
import importlib
import os
import re
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BOUND_BASELINE = "d2a2f003134a66b35cd76cabb8c2aaa43ca184f5"
PYTHON_REALPATH = "/opt/homebrew/Cellar/python@3.12/3.12.13/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
PYTHON_DIGEST = "261a3951c895427210dfb7780693600b820f70841c078ab2554ee6fbeba7f376"
RUFF_PATH = Path("/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin/ruff")
RUFF_DIGEST = "1edd2e6e57286bdddedb1fb55493a91dc17f42838f3d6be488ded7cfe2a4f3a1"
CANDIDATE_HASHES = {
    "app/services/visual_diagnosis_contracts.py": "d66994f0063474c05558a8d0f0f71c3317337d5ede88860b4ab9a99e60be1c13",
    "app/services/visual_diagnosis_state.py": "bf61ac5b8a7608b0f564ab7d2a1def93147ba02085371eb910cf2f2c79b77662",
}
IMPORT_CLOSURE = {
    "app/services/visual_diagnosis_contracts.py",
    "app/services/visual_diagnosis_state.py",
}
EXPECTED_PATHS = {
    "docs/superpowers/specs/2026-08-11-visual-diagnosis-design.md",
    "docs/superpowers/plans/2026-08-11-visual-diagnosis.md",
    "docs/handoffs/2026-08-11-visual-diagnosis-handoff.md",
    "docs/handoffs/2026-08-11-visual-diagnosis-new-session-prompt.md",
    "app/services/visual_diagnosis_contracts.py",
    "app/services/visual_diagnosis_state.py",
    "tests/unit/test_visual_diagnosis_contracts.py",
    "tests/unit/test_visual_diagnosis_state.py",
    "tests/conftest.py",
}
LIVE_ROOT_HASHES = {
    "Dockerfile": "a8b96ae525dcd94a3e839a1980b14710c8e98663d42b812f4a1754878ddc4a2b",
    "app/main.py": "057a810dd5eb2e08651fd965f9264c48681e5bb17ce87bad4fafb4260c6e0334",
    ".github/workflows/deploy.yml": "672555b73c92478a3d92bdabd7233b964fa1bdcc52ebf1a7cb2e9d17cc37ede7",
    ".github/workflows/rollback.yml": "3be7f5a8863f623a280abab7dd7b350ae270eb2ae4178f419e50a0d2c74dfae0",
}
FORBIDDEN_ENV_NAMES = {
    "ANTHROPIC_API_KEY", "ADMIN_API_TOKEN", "APNS_KEY_CONTENT", "APNS_KEY_ID",
    "APNS_TEAM_ID", "API_BEARER_TOKEN", "APPSTORE_ISSUER_ID", "APPSTORE_KEY_ID",
    "APPSTORE_PRIVATE_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY",
    "FISH_AUDIO_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET", "JOBBER_CLIENT_ID",
    "JOBBER_CLIENT_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET",
    "RECEPTIONIST_OBSERVATION_SHADOW_CALLER_HMAC_KEY", "TRANSCRIPT_ENCRYPTION_KEY",
    "TWILIO_ACCOUNT_SID", "PRODUCTION_TWILIO_ACCOUNT_SID", "TWILIO_API_KEY_SECRET",
    "TWILIO_API_KEY_SID", "TWILIO_AUTH_TOKEN", "TWILIO_TWIML_APP_SID", "VAPI_API_KEY",
    "VAPI_PUBLIC_KEY", "VAPI_WEBHOOK_SECRET", "VCARD_HMAC_SECRET", "APNS_BUNDLE_ID",
    "APPSTORE_BUNDLE_ID", "CLOUD_RUN_URL", "DIAL_IN_NUMBER", "DIAL_IN_NUMBERS",
    "FIREBASE_DATABASE_URL", "FIRESTORE_PROJECT_ID", "GCLOUD_PROJECT", "GCP_PROJECT",
    "GOOGLE_CLOUD_PROJECT", "TELEGRAM_CHAT_ID", "TWILIO_PHONE_NUMBER", "USER_NAME",
    "USER_PHONE", "VAPI_PHONE_NUMBER_ID",
}
ALLOWED_IMPORTS = {
    "__future__", "collections", "dataclasses", "datetime", "enum", "hashlib", "json",
    "re", "typing", "uuid", "pydantic",
}


def _running_in_ci() -> bool:
    """True on a GitHub Actions runner -- never set by local dev tooling."""

    return os.environ.get("GITHUB_ACTIONS") == "true"


def _guard_before_import(*, require_environment: bool = True) -> None:
    # Clean-tree + diff-scope check: works whether the candidate files are
    # still uncommitted (local dev, pre-PR) or already committed on a
    # feature branch (CI, PR review), and does not regress once merged (the
    # diff-vs-origin/main then becomes empty, a trivial subset of the
    # allowlist). Baseline provenance is a monotonic ancestor check rather
    # than an exact HEAD pin, because CI checks out an ephemeral merge
    # commit for pull_request events, not the branch tip itself.
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    dirty = {entry[3:].decode("utf-8") for entry in status.split(b"\0") if entry}
    assert not dirty, f"unexpected working-tree changes: {sorted(dirty)}"
    is_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", BOUND_BASELINE, "HEAD"],
        cwd=ROOT,
        capture_output=True,
    )
    assert is_ancestor.returncode == 0, "reviewed baseline is not an ancestor of HEAD"
    diff = subprocess.run(
        ["git", "diff", "--name-only", "-z", "origin/main", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    changed = {entry.decode("utf-8") for entry in diff.split(b"\0") if entry}
    assert changed <= EXPECTED_PATHS, f"diff vs origin/main exceeds the allowlist: {changed - EXPECTED_PATHS}"

    ignored = subprocess.run(
        ["git", "status", "--porcelain=v1", "--ignored", "-z", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    sensitive_path = re.compile(r"(?:^|/)(?:\.env(?:\.|$)|.*(?:credential|secret|token|provider|customer|payload|\.pem$|\.p8$|\.key$))", re.I)
    for entry in ignored.split(b"\0"):
        if entry and entry[:2] == b"!!":
            ignored_path = entry[3:].decode("utf-8")
            if ignored_path.endswith(".pyc"):
                continue
            assert not sensitive_path.search(ignored_path)

    if not _running_in_ci():
        # Denied-egress and this exact dev machine's pinned toolchain are
        # local-sandbox properties (sandbox-exec, a specific Homebrew Python
        # and venv-local ruff) that a hosted CI runner cannot reproduce and
        # was never meant to: CI's isolation instead comes from running an
        # ordinary, unprivileged test process with no provider credentials
        # present (still enforced below, unconditionally).
        try:
            with socket.create_connection(("1.1.1.1", 443), 0.5):
                raise AssertionError("candidate tests are not running under denied egress")
        except PermissionError as error:
            assert error.errno == 1
        except OSError as error:
            raise AssertionError("egress failure was not the sandbox denial") from error
        assert os.environ.get("VISUAL_DIAG_EGRESS_DENIED") == "sandbox-exec"
    if require_environment:
        # Check the pristine pre-collection snapshot (see conftest.py), not
        # live os.environ: several sibling test files set dummy provider-
        # credential env vars at their own module level, which would
        # otherwise look identical to a real secret leak to this scan.
        pristine = os.environ.get("_VISUAL_DIAG_PRISTINE_ENVIRON_NAMES", "").split("\x1f")
        names = {name.casefold() for name in pristine if name}
        assert names.isdisjoint({name.casefold() for name in FORBIDDEN_ENV_NAMES})
        assert not any(
            name.startswith("bakeoff_nonprod_credential__")
            or name.startswith("bakeoff_nonprod_account_region__")
            for name in names
        )
    assert sys.version_info[:2] == (3, 12)
    if not _running_in_ci():
        assert os.path.realpath(sys.executable) == PYTHON_REALPATH
        assert hashlib.sha256(Path(PYTHON_REALPATH).read_bytes()).hexdigest() == PYTHON_DIGEST
        assert hashlib.sha256(RUFF_PATH.read_bytes()).hexdigest() == RUFF_DIGEST
    pydantic = importlib.import_module("pydantic")
    assert pydantic.__version__ == "2.12.5"
    assert hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest() == (
        "9fea68c27dbe4e24cd31fb6c6af4d77a8caa3796a2298d3a52cce2d40fd1764a"
    )
    for relative, expected in LIVE_ROOT_HASHES.items():
        shown = subprocess.run(
            ["git", "show", f"origin/main:{relative}"], cwd=ROOT, check=True, capture_output=True
        ).stdout
        assert hashlib.sha256(shown).hexdigest() == expected
    for path in (
        ROOT / "app/services/visual_diagnosis_contracts.py",
        ROOT / "app/services/visual_diagnosis_state.py",
        ):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                if path.name == "visual_diagnosis_state.py" and module == "app.services.visual_diagnosis_contracts":
                    continue
                assert module.split(".", 1)[0] in ALLOWED_IMPORTS
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not calls.intersection({"eval", "exec", "open", "__import__"})
    for relative, expected in CANDIDATE_HASHES.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
    assert set(CANDIDATE_HASHES) == IMPORT_CLOSURE
    if not _running_in_ci():
        assert Path(sys.executable) == Path("/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin/python")
        assert os.readlink(sys.executable) == "/opt/homebrew/opt/python@3.12/bin/python3.12"
        ruff_version = subprocess.run(
            [str(RUFF_PATH), "--version"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout.strip()
        assert ruff_version == "ruff 0.15.20"
        assert os.path.realpath(RUFF_PATH) == str(RUFF_PATH)
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "app"], cwd=ROOT, check=True, capture_output=True
    ).stdout.split(b"\0")
    for relative in ("app/main.py", "Dockerfile", ".github/workflows/deploy.yml", ".github/workflows/rollback.yml"):
        path = ROOT / relative
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "visual_diagnosis_" not in text
        if path.suffix == ".py":
            tree = ast.parse(text, filename=str(path))
            assert not any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec", "__import__", "import_module"}
                for node in ast.walk(tree)
            )
    candidate_names = {"visual_diagnosis_contracts.py", "visual_diagnosis_state.py"}
    for raw in tracked:
        if not raw:
            continue
        raw_path = ROOT / raw.decode("utf-8")
        if raw_path.name in candidate_names:
            continue
        text = raw_path.read_text(encoding="utf-8", errors="ignore")
        assert "visual_diagnosis_contracts" not in text
        assert "visual_diagnosis_state" not in text


_guard_before_import()


@pytest.fixture(scope="module")
def modules():
    _guard_before_import(require_environment=False)
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix="visual-diagnosis-config-") as config_dir:
        forbidden_names = {
            name.casefold()
            for name in os.environ
            if name.casefold() in {item.casefold() for item in FORBIDDEN_ENV_NAMES}
            or name.casefold().startswith("bakeoff_nonprod_credential__")
            or name.casefold().startswith("bakeoff_nonprod_account_region__")
        }
        saved_forbidden = {name: os.environ[name] for name in os.environ if name.casefold() in forbidden_names}
        for name in list(os.environ):
            if name.casefold() in forbidden_names:
                os.environ.pop(name, None)
        old_cloud = os.environ.get("CLOUDSDK_CONFIG")
        old_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["CLOUDSDK_CONFIG"] = config_dir
        os.environ["XDG_CONFIG_HOME"] = config_dir
        try:
            contracts = importlib.import_module("app.services.visual_diagnosis_contracts")
            state = importlib.import_module("app.services.visual_diagnosis_state")
            yield contracts, state
        finally:
            for name, value in saved_forbidden.items():
                os.environ[name] = value
            if old_cloud is None:
                os.environ.pop("CLOUDSDK_CONFIG", None)
            else:
                os.environ["CLOUDSDK_CONFIG"] = old_cloud
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def event(c, kind, revision, event_id, payload=None, *, at=None, source=None, retry_stage=None, retry_attempt=None):
    return c.VisualTriageEvent.build(
        case_id="case-1",
        contractor_id="contractor-1",
        event_id=event_id,
        kind=kind,
        payload=payload or {},
        expected_revision=revision,
        source_kind=source or c.EventSource.SYNTHETIC,
        event_time=at or datetime(2026, 8, 12, tzinfo=timezone.utc),
        retry_stage=retry_stage,
        retry_attempt=retry_attempt,
    )


def accepted(sm, event_obj):
    decision = sm.apply(event_obj)
    assert decision.accepted, decision.decision_code
    return decision


def bootstrap_valid(sm, c, *, source_ref="call-ref"):
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo", "source_ref": source_ref}))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, 1, "consent-request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, 2, "consent-grant"))
    upload = {
        "asset_id": "asset-video",
        "media_type": "video/mp4",
        "byte_size": 100,
        "duration_ms": 10_000,
        "width": 320,
        "height": 240,
        "digest": "a" * 64,
    }
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 3, "upload-start", upload))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, 4, "upload-final", {"asset_id": "asset-video"}))
    accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, 5, "media-validate", {"asset_id": "asset-video", "validation": "validated"}))


def test_full_structural_lifecycle_and_deletion_precedence(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "analysis-start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "analysis-complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_READY_RECORDED, 8, "packet-ready"))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_STARTED, 9, "packet-start"))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_RECEIPT_RECORDED, 10, "packet-receipt", {"status": "delivered"}))
    accepted(sm, event(c, c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED, 11, "policy", {"receipt_ref": "policy"}))
    accepted(sm, event(c, c.EventKind.CUSTOMER_DELIVERY_STARTED, 12, "customer-start", {"receipt_ref": "policy"}))
    accepted(sm, event(c, c.EventKind.CUSTOMER_DELIVERY_RECEIPT_RECORDED, 13, "customer-receipt", {"status": "delivered"}))
    accepted(sm, event(c, c.EventKind.CASE_CLOSED, 14, "close"))
    deletion = accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, 15, "delete"))
    assert deletion.projection.next_action is None
    blocked = sm.apply(event(c, c.EventKind.CASE_CREATED, 16, "late-create"))
    assert not blocked.accepted and blocked.decision_code == "deletion_pending"
    accepted(sm, event(c, c.EventKind.DELETION_RETRY_RECORDED, 16, "delete-retry-1", {"attempt": 1}, retry_stage="deletion", retry_attempt=1))
    accepted(sm, event(c, c.EventKind.DELETION_RETRY_RECORDED, 17, "delete-retry-2", {"attempt": 2}, retry_stage="deletion", retry_attempt=2))
    accepted(sm, event(c, c.EventKind.DELETION_RETRY_RECORDED, 18, "delete-retry-3", {"attempt": 3}, retry_stage="deletion", retry_attempt=3))
    fourth = sm.apply(event(c, c.EventKind.DELETION_RETRY_RECORDED, 19, "delete-retry-4", {"attempt": 4}, retry_stage="deletion", retry_attempt=3))
    assert not fourth.accepted and fourth.decision_code == "deletion_retry_invalid"
    verified = accepted(sm, event(c, c.EventKind.DELETION_VERIFIED, 19, "delete-verified"))
    assert verified.projection.reason_code == "verified_deleted"
    assert verified.projection.next_action is None


def test_replay_conflict_binding_and_historical_current_revisions(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    first = event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"})
    accepted(sm, first)
    replay = sm.apply(first)
    assert replay.accepted and replay.replayed
    assert replay.historical_revision == 1
    assert replay.current_revision == 1
    conflicting = event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.other"})
    conflict = sm.apply(conflicting)
    assert not conflict.accepted and conflict.decision_code == "event_id_conflict"
    wrong_binding = c.VisualTriageEvent.build(
        case_id="other-case",
        contractor_id="contractor-1",
        event_id="wrong-binding",
        kind=c.EventKind.CONSENT_REQUESTED,
        payload={},
        expected_revision=1,
        source_kind=c.EventSource.SYNTHETIC,
        event_time=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    decision = sm.apply(wrong_binding)
    assert not decision.accepted and decision.decision_code == "binding_mismatch"
    assert decision.current_revision == 0 and decision.projection is None

    non_synthetic = c.VisualTriageEvent.build(
        case_id="case-1", contractor_id="contractor-1", event_id="create",
        kind=c.EventKind.CASE_CREATED, payload={"scenario": "hvac.other"},
        expected_revision=0, source_kind=c.EventSource.SYSTEM,
        event_time=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    collision = sm.apply(non_synthetic)
    assert not collision.accepted and collision.decision_code == "event_id_conflict"


def test_apply_rechecks_mutated_event_payload_before_state_change(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    first = event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"})
    first.payload["scenario"] = "hvac.other"
    rejected = sm.apply(first)
    assert not rejected.accepted and rejected.decision_code == "event_integrity_mismatch"
    assert sm.current_revision == 0 and sm.case is None


def test_non_synthetic_source_is_rejected_in_a0_a2(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    source_event = c.VisualTriageEvent.build(
        case_id="case-1",
        contractor_id="contractor-1",
        event_id="create",
        kind=c.EventKind.CASE_CREATED,
        payload={},
        expected_revision=0,
        source_kind=c.EventSource.SYSTEM,
        event_time=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    rejected = sm.apply(source_event)
    assert not rejected.accepted and rejected.decision_code == "source_kind_mismatch"
    assert sm.case is None


def test_pre_binding_failures_never_reveal_aggregate_state(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    tampered = event(c, c.EventKind.CONSENT_REQUESTED, 1, "request")
    tampered.payload["unexpected"] = "x"
    invalid = sm.apply(tampered)
    assert not invalid.accepted and invalid.decision_code == "event_integrity_mismatch"
    assert invalid.current_revision == 0 and invalid.projection is None
    wrong_type = sm.apply(object())
    assert not wrong_type.accepted and wrong_type.current_revision == 0 and wrong_type.projection is None


def test_rating_plate_requires_validated_initial_symptom_media(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, 2, "grant"))
    blocked = sm.apply(event(c, c.EventKind.MEDIA_ACTION_ISSUED, 3, "plate-early", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "budget_bucket": "rating_plate", "receipt_ref": "plate-early",
    }))
    assert not blocked.accepted and blocked.decision_code == "rating_plate_budget_exhausted"


def test_case_creation_rejects_malformed_payload_before_mutation(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    malformed = sm.apply(event(c, c.EventKind.CASE_CREATED, 0, "bad-create", {
        "scenario": 123, "source_ref": ["not-opaque"],
    }))
    assert not malformed.accepted and malformed.decision_code == "invalid_case_payload"
    assert sm.current_revision == 0 and sm.case is None


def test_case_created_rejects_scenarios_the_model_would_also_reject(modules):
    # _case_created's local scenario shape check must be at least as strict
    # as VisualTriageCase.validate_scenario's CODE_PATTERN, or a payload can
    # be accepted (revision advances, accepted=True) yet permanently poison
    # the aggregate: every subsequent snapshot()/.case access raises once the
    # model refuses to construct. str.isalnum() accepts non-ASCII Unicode
    # letters and has no length bound, unlike CODE_PATTERN.
    c, state = modules
    non_ascii = state.VisualTriageStateMachine()
    result = non_ascii.apply(event(c, c.EventKind.CASE_CREATED, 0, "create-non-ascii", {"scenario": "hvac.café"}))
    assert not result.accepted and result.decision_code == "invalid_case_payload"
    assert non_ascii.current_revision == 0 and non_ascii.case is None

    overlong = state.VisualTriageStateMachine()
    long_scenario = "hvac." + ("a" * 100)
    assert len(long_scenario) > 64
    result = overlong.apply(event(c, c.EventKind.CASE_CREATED, 0, "create-long", {"scenario": long_scenario}))
    assert not result.accepted and result.decision_code == "invalid_case_payload"
    assert overlong.current_revision == 0 and overlong.case is None


def test_case_created_accepts_the_scenarios_used_elsewhere_in_this_suite(modules):
    c, state = modules
    for scenario in ("hvac.demo", "hvac.not_cooling_with_outdoor_unit_noise"):
        sm = state.VisualTriageStateMachine()
        result = accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, f"create-{scenario}", {"scenario": scenario}))
        assert result.decision_code == "case_created"
        assert sm.case is not None and sm.case.supported_scenario == scenario


def test_terminal_projection_has_no_action(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, 2, "grant"))
    cancelled = accepted(sm, event(c, c.EventKind.CASE_CANCELLED, 3, "cancel"))
    assert cancelled.projection.reason_code == "case_terminal"
    assert cancelled.projection.next_action is None


def test_terminal_event_clears_pending_action_and_late_resolution(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 8, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question", "budget_bucket": "question",
        "receipt_ref": "question", "locale": "und", "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }))
    cancelled = accepted(sm, event(c, c.EventKind.CASE_CANCELLED, 9, "cancel"))
    assert cancelled.projection.next_action is None and sm.case.pending_customer_action is None
    for role in c.ProjectionRole:
        assert sm.project(role).next_action is None
    late = sm.apply(event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, 10, "late-answer", {
        "request_id": "question-request", "action_kind": "diagnostic_question", "locale": "und",
        "response_option_code": "yes", "status": "answered",
    }))
    assert not late.accepted and late.decision_code == "case_not_active"


def test_expiry_records_expiry_timestamp_and_is_terminal(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, 2, "grant"))
    expired = accepted(sm, event(c, c.EventKind.CASE_EXPIRED, 3, "expire"))
    assert sm.case is not None and sm.case.expires_at == datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    assert expired.projection.reason_code == "case_terminal"


def test_role_projections_keep_pending_action_bounded(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 8, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "en-US",
        "copy_ref": "question-copy", "response_option_codes": ["yes", "no"],
    }))
    customer = sm.project(c.ProjectionRole.CUSTOMER)
    contractor = sm.project(c.ProjectionRole.CONTRACTOR)
    internal = sm.project(c.ProjectionRole.INTERNAL)
    audit = sm.project(c.ProjectionRole.AUDIT)
    assert customer.pending_action_locale == "en-US"
    assert customer.pending_action_request_ref == "question-request"
    assert contractor.pending_action_request_ref is None
    assert internal.pending_action_request_ref == "question-request"
    assert audit.next_action is None


def test_source_ref_and_diagnostic_action_refs_are_shape_checked_and_role_bounded(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    source_ref = "5555550142"  # phone-shaped, opaque
    assert not any(term in source_ref.casefold() for term in c._FORBIDDEN_PAYLOAD_TERMS)
    bootstrap_valid(sm, c, source_ref=source_ref)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))

    # call_sid_ref is public on VisualTriageCase but _projection() never reads it.
    assert sm.case.call_sid_ref == source_ref
    for role in c.ProjectionRole:
        projection = sm.project(role)
        rendered = repr(projection) + str(projection) + repr(projection.model_dump())
        assert source_ref not in rendered

    # request_id/copy_ref are shape-checked only (OPAQUE_REF_PATTERN forbids
    # whitespace); free text never becomes a live pending action.
    revision_before_question = sm.current_revision
    pending_before_question = sm.case.pending_customer_action
    prose_request_id = sm.apply(event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, revision_before_question, "question-prose-1", {
        "request_id": "customer says it clicks loudly", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question-prose-1", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes", "no"],
    }))
    assert not prose_request_id.accepted
    assert prose_request_id.decision_code == "invalid_structural_payload"
    assert sm.current_revision == revision_before_question
    assert sm.case.pending_customer_action == pending_before_question

    prose_copy_ref = sm.apply(event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, revision_before_question, "question-prose-2", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question-prose-2", "locale": "und",
        "copy_ref": "please describe the noise you are hearing", "response_option_codes": ["yes", "no"],
    }))
    assert not prose_copy_ref.accepted
    assert prose_copy_ref.decision_code == "invalid_structural_payload"
    assert sm.current_revision == revision_before_question
    assert sm.case.pending_customer_action == pending_before_question

    # A whitespace-free value that merely looks realistic passes the same
    # check: visible to INTERNAL/CUSTOMER/AUDIT (an intentional pass-through),
    # excluded from CONTRACTOR.
    request_id = "XJ48-77Q2-KTN9"
    copy_ref = "a9f3c7e1b2d84660"
    for value in (request_id, copy_ref):
        assert not any(term in value.casefold() for term in c._FORBIDDEN_PAYLOAD_TERMS)
    decision = accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, revision_before_question, "question", {
        "request_id": request_id, "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": copy_ref, "response_option_codes": ["yes", "no"],
    }))
    for role in (c.ProjectionRole.INTERNAL, c.ProjectionRole.CUSTOMER, c.ProjectionRole.AUDIT):
        projection = sm.project(role)
        assert projection.pending_action_request_ref == request_id
        assert projection.pending_action_copy_ref == copy_ref
        assert request_id not in repr(projection) + str(projection)
        assert copy_ref not in repr(projection) + str(projection)
    contractor = sm.project(c.ProjectionRole.CONTRACTOR)
    assert contractor.pending_action_request_ref is None
    assert contractor.pending_action_copy_ref is None

    case_snapshot = sm.case
    for rendered in (
        repr(case_snapshot), str(case_snapshot),
        repr(case_snapshot.pending_customer_action), str(case_snapshot.pending_customer_action),
        repr(decision), str(decision),
    ):
        assert request_id not in rendered
        assert copy_ref not in rendered


def test_rejected_initial_media_reaches_one_recapture_and_fulfillment(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, 2, "grant"))
    upload = {
        "asset_id": "bad-video", "media_type": "video/mp4", "byte_size": 100,
        "duration_ms": 10_000, "width": 320, "height": 240, "digest": "b" * 64,
    }
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 3, "upload", upload))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, 4, "final", {"asset_id": "bad-video"}))
    accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, 5, "reject", {"asset_id": "bad-video", "validation": "rejected"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, 6, "recapture-issue", {
        "request_id": "recapture-request", "action_kind": "targeted_recapture",
        "budget_bucket": "recapture", "copy_ref": "recapture-copy", "receipt_ref": "recapture-issue",
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 7, "recapture-upload", {
        "asset_id": "good-video", "media_type": "video/mp4", "byte_size": 100,
        "duration_ms": 10_000, "width": 320, "height": 240, "digest": "c" * 64,
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, 8, "recapture-final", {"asset_id": "good-video"}))
    accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, 9, "recapture-valid", {"asset_id": "good-video", "validation": "validated"}))
    fulfilled = accepted(sm, event(c, c.EventKind.MEDIA_ACTION_RESOLVED, 10, "recapture-done", {
        "request_id": "recapture-request", "action_kind": "targeted_recapture",
        "media_role": "targeted_recapture", "validation": "validated",
        "status": "fulfilled", "asset_id": "good-video",
    }))
    assert fulfilled.projection.analysis_status.value == "ready"
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 11, "recapture-analysis-start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 12, "recapture-analysis-complete", {"outcome": "abstained"}))
    second = sm.apply(event(c, c.EventKind.MEDIA_ACTION_ISSUED, 13, "recapture-second", {
        "request_id": "recapture-2", "action_kind": "targeted_recapture", "budget_bucket": "recapture", "receipt_ref": "recapture-second",
    }))
    assert not second.accepted and second.decision_code == "recapture_not_reachable"


def test_customer_action_resolved_after_rejected_media_does_not_advertise_start_analysis(modules):
    # If initial symptom media is rejected and never replaced, analysis lands
    # on ABSTAINED (not READY). A diagnostic question can still be issued
    # from ABSTAINED, so resolving that question must not unconditionally
    # flip analysis to READY -- _analysis_started also requires validated
    # symptom evidence, so an unconditional READY here would advertise
    # next_action="start_analysis" for a transition that is always rejected.
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, 2, "grant"))
    upload = {
        "asset_id": "bad-video", "media_type": "video/mp4", "byte_size": 100,
        "duration_ms": 10_000, "width": 320, "height": 240, "digest": "b" * 64,
    }
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 3, "upload", upload))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, 4, "final", {"asset_id": "bad-video"}))
    rejected = accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, 5, "reject", {"asset_id": "bad-video", "validation": "rejected"}))
    assert rejected.projection.analysis_status is c.AnalysisStatus.ABSTAINED
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, sm.current_revision, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question", "budget_bucket": "question",
        "receipt_ref": "question", "locale": "und", "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }))
    resolved = accepted(sm, event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, sm.current_revision, "answer", {
        "request_id": "question-request", "action_kind": "diagnostic_question", "locale": "und",
        "response_option_code": "yes", "status": "answered",
    }))
    assert resolved.projection.analysis_status is c.AnalysisStatus.ABSTAINED
    assert resolved.projection.next_action is None
    # Regression guard: submitting the (never-advertised) action was always
    # going to be rejected, before and after the fix -- only the misleading
    # advertisement changes.
    blocked = sm.apply(event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start-after-rejected-media"))
    assert not blocked.accepted and blocked.decision_code == "analysis_not_ready"


def test_successful_rating_plate_fulfillment_restart_and_replay(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, 8, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "budget_bucket": "rating_plate", "receipt_ref": "plate-issue", "copy_ref": "plate-copy",
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 9, "plate-upload", {
        "asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100,
        "digest": "c" * 64,
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, 10, "plate-final", {"asset_id": "plate-asset"}))
    accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, 11, "plate-valid", {
        "asset_id": "plate-asset", "validation": "validated",
    }))
    fulfilled_event = event(c, c.EventKind.MEDIA_ACTION_RESOLVED, 12, "plate-done", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "media_role": "rating_plate", "status": "fulfilled",
        "asset_id": "plate-asset", "validation": "validated",
    })
    accepted(sm, fulfilled_event)
    assert sm.apply(fulfilled_event).replayed
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 13, "restart"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 14, "restart-complete", {"outcome": "complete"}))
    second = sm.apply(event(c, c.EventKind.MEDIA_ACTION_ISSUED, 15, "plate-again", {
        "request_id": "plate-request-2", "action_kind": "rating_plate",
        "budget_bucket": "rating_plate", "receipt_ref": "plate-again", "copy_ref": "plate-copy-2",
    }))
    assert not second.accepted and second.decision_code == "rating_plate_budget_exhausted"


def test_recapture_unavailability_abstains_and_exhausts_budget(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, 2, "grant"))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 3, "upload", {
        "asset_id": "bad-video", "media_type": "video/mp4", "byte_size": 100,
        "duration_ms": 10_000, "width": 320, "height": 240, "digest": "b" * 64,
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, 4, "final", {"asset_id": "bad-video"}))
    accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, 5, "reject", {
        "asset_id": "bad-video", "validation": "rejected",
    }))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, 6, "recapture-issue", {
        "request_id": "recapture-request", "action_kind": "targeted_recapture",
        "budget_bucket": "recapture", "copy_ref": "recapture-copy", "receipt_ref": "recapture-issue",
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 7, "recapture-upload", {
        "asset_id": "recapture-asset", "media_type": "video/mp4", "byte_size": 100,
        "duration_ms": 10_000, "width": 320, "height": 240, "digest": "d" * 64,
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, 8, "recapture-final", {"asset_id": "recapture-asset"}))
    accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, 9, "recapture-unavailable", {
        "asset_id": "recapture-asset", "validation": "unavailable",
    }))
    resolved = accepted(sm, event(c, c.EventKind.MEDIA_ACTION_RESOLVED, 10, "recapture-done", {
        "request_id": "recapture-request", "action_kind": "targeted_recapture",
        "media_role": "targeted_recapture", "status": "unavailable",
        "asset_id": "recapture-asset", "validation": "unavailable",
    }))
    assert resolved.projection.analysis_status is c.AnalysisStatus.ABSTAINED
    blocked = sm.apply(event(c, c.EventKind.MEDIA_ACTION_ISSUED, 11, "recapture-again", {
        "request_id": "recapture-request-2", "action_kind": "targeted_recapture",
        "budget_bucket": "recapture", "receipt_ref": "recapture-again", "copy_ref": "recapture-copy-2",
    }))
    assert not blocked.accepted and blocked.decision_code == "recapture_not_reachable"


def test_one_pending_action_and_question_budget(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 8, "question-1", {
        "request_id": "question-request-1", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "response_option_codes": ["yes", "no"], "receipt_ref": "question-1", "locale": "und", "copy_ref": "question-copy-1",
    }))
    pending = sm.case.pending_customer_action
    assert pending is not None and pending.issued_at.tzinfo is not None
    assert pending.expires_at is None and pending.resolved_at is None
    customer_projection = sm.project(c.ProjectionRole.CUSTOMER)
    contractor_projection = sm.project(c.ProjectionRole.CONTRACTOR)
    assert customer_projection.pending_action_kind is c.ActionKind.DIAGNOSTIC_QUESTION
    assert customer_projection.pending_action_request_ref == "question-request-1"
    assert customer_projection.pending_action_locale == "und"
    assert customer_projection.pending_action_response_option_codes == ("yes", "no")
    assert customer_projection.pending_action_copy_ref == "question-copy-1"
    assert customer_projection.pending_action_budget_bucket is None
    assert contractor_projection.pending_action_request_ref is None
    blocked = sm.apply(event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 9, "question-2", {
        "request_id": "question-request-2", "action_kind": "diagnostic_question", "budget_bucket": "question", "receipt_ref": "question-2",
    }))
    assert not blocked.accepted and blocked.decision_code == "customer_action_pending"
    accepted(sm, event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, 9, "answer-1", {
        "request_id": "question-request-1", "action_kind": "diagnostic_question", "locale": "und", "response_option_code": "yes", "status": "answered",
    }))
    assert sm.case is not None
    assert sm.case.resolved_customer_actions[-1].response_option_code == "yes"
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 10, "restart-analysis"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 11, "restart-complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 12, "question-2", {
        "request_id": "question-request-2", "action_kind": "diagnostic_question", "budget_bucket": "question", "receipt_ref": "question-2", "locale": "und", "copy_ref": "question-copy-2", "response_option_codes": ["yes", "no"],
    }))
    accepted(sm, event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, 13, "answer-2", {
        "request_id": "question-request-2", "action_kind": "diagnostic_question", "locale": "und", "status": "not_sure",
    }))
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 14, "resume-after-not-sure"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 15, "resume-after-not-sure-complete", {"outcome": "complete"}))
    exhausted = sm.apply(event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 16, "question-3", {
        "request_id": "question-request-3", "action_kind": "diagnostic_question", "budget_bucket": "question", "receipt_ref": "question-3", "locale": "und", "copy_ref": "question-copy-3", "response_option_codes": ["yes", "no"],
    }))
    assert not exhausted.accepted and exhausted.decision_code == "question_budget_exhausted"


def test_case_close_rejects_processing_and_late_completion(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    close = sm.apply(event(c, c.EventKind.CASE_CLOSED, 7, "close"))
    assert not close.accepted and close.decision_code == "case_not_quiescent"
    accepted(sm, event(c, c.EventKind.ANALYSIS_FAILED, 7, "failed", {"failure_state": "failed_terminal"}))
    accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, 8, "delete"))
    late = sm.apply(event(c, c.EventKind.ANALYSIS_COMPLETED, 9, "late-complete", {"outcome": "complete"}))
    assert not late.accepted and late.decision_code == "deletion_pending"


def test_analysis_abstention_is_terminal_without_explicit_resume_event(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "abstain", {"outcome": "abstained"}))
    blocked = sm.apply(event(c, c.EventKind.ANALYSIS_STARTED, 8, "restart"))
    assert not blocked.accepted and blocked.decision_code == "analysis_not_ready"


def test_optional_plate_rejection_preserves_symptom_readiness(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, 8, "plate", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "budget_bucket": "rating_plate", "receipt_ref": "plate",
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 9, "plate-upload", {
        "asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100,
        "digest": "d" * 64,
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, 10, "plate-final", {"asset_id": "plate-asset"}))
    accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, 11, "plate-reject", {
        "asset_id": "plate-asset", "validation": "rejected",
    }))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_RESOLVED, 12, "plate-unavailable", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "media_role": "rating_plate", "status": "unavailable", "asset_id": "plate-asset", "validation": "rejected",
    }))
    resumed = sm.apply(event(c, c.EventKind.ANALYSIS_STARTED, 13, "resume"))
    assert resumed.accepted


def test_media_refusal_is_available_before_upload_and_not_sure_can_resume(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, 8, "plate", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "budget_bucket": "rating_plate", "receipt_ref": "plate", "copy_ref": "plate-copy",
    }))
    refused = sm.apply(event(c, c.EventKind.MEDIA_ACTION_RESOLVED, 9, "plate-refused", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "media_role": "rating_plate", "status": "declined",
    }))
    assert refused.accepted and refused.projection.analysis_status is c.AnalysisStatus.READY
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 10, "restart"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 11, "restart-complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 12, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "response_option_codes": ["yes"], "copy_ref": "question-copy",
    }))
    accepted(sm, event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, 13, "not-sure", {
        "request_id": "question-request", "action_kind": "diagnostic_question", "locale": "und", "status": "not_sure",
    }))
    resumed = sm.apply(event(c, c.EventKind.ANALYSIS_STARTED, 14, "resume"))
    assert resumed.accepted


def test_question_decline_does_not_reopen_question_loop(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 8, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }))
    accepted(sm, event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, 9, "decline", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "locale": "und", "status": "declined",
    }))
    blocked = sm.apply(event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 10, "question-2", {
        "request_id": "question-request-2", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question-2", "locale": "und",
        "copy_ref": "question-copy-2", "response_option_codes": ["yes"],
    }))
    assert not blocked.accepted and blocked.decision_code == "question_prompt_closed"


def test_initial_abstention_can_issue_one_bounded_question(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "abstain", {"outcome": "abstained"}))
    issued = sm.apply(event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 8, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }))
    assert issued.accepted


def test_expired_question_requires_elapsed_bound(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 8, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes"],
        "expires_at": "2026-08-12T00:10:00+00:00",
    }))
    early = sm.apply(event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, 9, "early", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "locale": "und", "status": "expired",
    }))
    assert not early.accepted and early.decision_code == "action_not_expired"
    expired_event = event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, 9, "expired", {
        "request_id": "question-request", "action_kind": "diagnostic_question", "locale": "und", "status": "expired",
    }, at=datetime(2026, 8, 12, 0, 11, tzinfo=timezone.utc))
    expired = accepted(sm, expired_event)
    assert expired.projection.analysis_status is c.AnalysisStatus.ABSTAINED
    assert sm.apply(expired_event).replayed


def test_question_cannot_safely_complete_closes_prompt_and_answer_conflicts_are_bound(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 8, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes", "no"],
    }))
    unsafe = accepted(sm, event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, 9, "unsafe", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "locale": "und", "status": "cannot_safely_complete",
    }))
    assert unsafe.projection.analysis_status is c.AnalysisStatus.ABSTAINED
    blocked = sm.apply(event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 10, "question-again", {
        "request_id": "question-request-2", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question-again", "locale": "und",
        "copy_ref": "question-copy-2", "response_option_codes": ["yes"],
    }))
    assert not blocked.accepted and blocked.decision_code == "question_prompt_closed"


def test_stale_and_conflicting_answer_rejection(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 8, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes", "no"],
    }))
    answer = event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, 9, "answer", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "locale": "und", "response_option_code": "yes", "status": "answered",
    })
    stale = sm.apply(event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, 8, "stale-answer", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "locale": "und", "response_option_code": "yes", "status": "answered",
    }))
    assert not stale.accepted and stale.decision_code == "stale_or_future_revision"
    accepted(sm, answer)
    conflict = sm.apply(event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, 9, "answer", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "locale": "und", "response_option_code": "no", "status": "answered",
    }))
    assert not conflict.accepted and conflict.decision_code == "event_id_conflict"


def test_interrupted_upload_replay_does_not_consume_media_budget(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, 8, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "budget_bucket": "rating_plate", "receipt_ref": "plate-issue", "copy_ref": "plate-copy",
    }))
    upload = event(c, c.EventKind.UPLOAD_STARTED, 9, "plate-upload", {
        "asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100,
        "digest": "e" * 64,
    })
    accepted(sm, upload)
    assert sm.apply(upload).replayed
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, 10, "plate-final", {"asset_id": "plate-asset"}))
    accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, 11, "plate-valid", {
        "asset_id": "plate-asset", "validation": "validated",
    }))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_RESOLVED, 12, "plate-done", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "media_role": "rating_plate", "status": "fulfilled",
        "asset_id": "plate-asset", "validation": "validated",
    }))
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 13, "restart"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 14, "restart-complete", {"outcome": "complete"}))
    blocked = sm.apply(event(c, c.EventKind.MEDIA_ACTION_ISSUED, 15, "plate-again", {
        "request_id": "plate-request-2", "action_kind": "rating_plate",
        "budget_bucket": "rating_plate", "receipt_ref": "plate-again", "copy_ref": "plate-copy-2",
    }))
    assert not blocked.accepted and blocked.decision_code == "rating_plate_budget_exhausted"


def test_consent_decline_and_withdrawal_are_terminal_control_paths(modules):
    c, state = modules
    declined = state.VisualTriageStateMachine()
    accepted(declined, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(declined, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    result = accepted(declined, event(c, c.EventKind.CONSENT_DECLINED, 2, "decline"))
    assert result.projection.reason_code == "case_terminal"
    late_grant = declined.apply(event(c, c.EventKind.CONSENT_GRANTED, 3, "late-grant"))
    assert not late_grant.accepted and late_grant.decision_code == "consent_grant_not_allowed"

    withdrawn = state.VisualTriageStateMachine()
    bootstrap_valid(withdrawn, c)
    result = accepted(withdrawn, event(c, c.EventKind.CONSENT_WITHDRAWN, withdrawn.current_revision, "withdraw"))
    assert result.projection.reason_code == "case_terminal"
    late_analysis = withdrawn.apply(event(c, c.EventKind.ANALYSIS_STARTED, withdrawn.current_revision, "late-analysis"))
    assert not late_analysis.accepted and late_analysis.decision_code == "case_not_active"


def test_analysis_retry_budget_and_fourth_attempt_rejection(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start-1"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_FAILED, sm.current_revision, "fail-1", {"failure_state": "failed_retriable"}))
    accepted(sm, event(c, c.EventKind.ANALYSIS_RETRY_RECORDED, sm.current_revision, "retry-1", {"attempt": 1}, retry_stage="analysis", retry_attempt=1))
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start-2"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_FAILED, sm.current_revision, "fail-2", {"failure_state": "failed_retriable"}))
    accepted(sm, event(c, c.EventKind.ANALYSIS_RETRY_RECORDED, sm.current_revision, "retry-2", {"attempt": 2}, retry_stage="analysis", retry_attempt=2))
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start-3"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_FAILED, sm.current_revision, "fail-3", {"failure_state": "failed_retriable"}))
    accepted(sm, event(c, c.EventKind.ANALYSIS_RETRY_RECORDED, sm.current_revision, "retry-3", {"attempt": 3}, retry_stage="analysis", retry_attempt=3))
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start-4"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_FAILED, sm.current_revision, "fail-4", {"failure_state": "failed_retriable"}))
    fourth = sm.apply(event(c, c.EventKind.ANALYSIS_RETRY_RECORDED, sm.current_revision, "retry-4", {"attempt": 4}, retry_stage="analysis", retry_attempt=3))
    assert not fourth.accepted and fourth.decision_code == "retry_attempt_invalid"


def test_analysis_retry_recorded_rejects_boolean_attempt(modules):
    # bool is a subclass of int in Python, so isinstance(True, int) is True
    # and True == 1 is also True -- a JSON boolean payload can pass every
    # numeric guard here and get stored as the literal Python object True,
    # corrupting the retry-count aggregate's type invariant.
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start-1"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_FAILED, sm.current_revision, "fail-1", {"failure_state": "failed_retriable"}))
    boolean_attempt = sm.apply(event(c, c.EventKind.ANALYSIS_RETRY_RECORDED, sm.current_revision, "retry-bool", {"attempt": True}, retry_stage="analysis", retry_attempt=1))
    assert not boolean_attempt.accepted
    assert boolean_attempt.decision_code == "retry_attempt_invalid"
    assert sm._analysis_retry_count == 0
    # Regression guard: a genuine int attempt at the same position still works.
    real_attempt = accepted(sm, event(c, c.EventKind.ANALYSIS_RETRY_RECORDED, sm.current_revision, "retry-1", {"attempt": 1}, retry_stage="analysis", retry_attempt=1))
    assert real_attempt.decision_code == "analysis_retry_recorded"
    assert sm._analysis_retry_count == 1


def test_deletion_retry_recorded_rejects_boolean_attempt(modules):
    # Same defect as the analysis retry counter, but here the corrupted
    # value (a bare Python True) is fed straight into
    # VisualTriageCase.deletion_retry_count, an int field under pydantic
    # strict mode -- so an accepted boolean attempt permanently poisons the
    # aggregate: every later snapshot()/.case access raises
    # StructuralValidationError, confirmed empirically against pydantic
    # 2.12.5 (bool is not coerced to int in strict mode).
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete"))
    boolean_attempt = sm.apply(event(c, c.EventKind.DELETION_RETRY_RECORDED, sm.current_revision, "retry-bool", {"attempt": True}, retry_stage="deletion", retry_attempt=1))
    assert not boolean_attempt.accepted
    assert boolean_attempt.decision_code == "deletion_retry_invalid"
    assert sm._deletion_retry_count == 0
    # Regression guard: a genuine int attempt at the same position still works.
    real_attempt = accepted(sm, event(c, c.EventKind.DELETION_RETRY_RECORDED, sm.current_revision, "retry-1", {"attempt": 1}, retry_stage="deletion", retry_attempt=1))
    assert real_attempt.decision_code == "deletion_retry_recorded"
    assert sm._deletion_retry_count == 1
    assert sm.case is not None and sm.case.deletion_retry_count == 1


def test_ordinary_lane_saturation_does_not_block_control_lane(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    receipt = sm.case.ordinary_receipts[0]
    sm._ordinary_receipts = [receipt] * 64
    ordinary = sm.apply(event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "ordinary-overflow"))
    assert not ordinary.accepted and ordinary.decision_code == "ordinary_lane_capacity"
    control = sm.apply(event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "control-delete"))
    assert control.accepted and control.projection.reason_code == "deletion_pending"


def test_ordinary_lane_capacity_replay_and_full_deletion_control_sequence(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    sm._ordinary_receipts = list((sm.case.ordinary_receipts * 11)[:63])
    start = event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "ordinary-commit")
    accepted(sm, start)
    assert sm.apply(start).replayed
    accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete"))
    for attempt in range(1, 4):
        accepted(sm, event(c, c.EventKind.DELETION_RETRY_RECORDED, sm.current_revision, f"retry-{attempt}", {"attempt": attempt}, retry_stage="deletion", retry_attempt=attempt))
    overflow = sm.apply(event(c, c.EventKind.DELETION_RETRY_RECORDED, sm.current_revision, "retry-4", {"attempt": 4}, retry_stage="deletion", retry_attempt=3))
    assert not overflow.accepted and overflow.decision_code == "deletion_retry_invalid"
    verified = accepted(sm, event(c, c.EventKind.DELETION_VERIFIED, sm.current_revision, "verified"))
    assert verified.projection.next_action is None


def test_deletion_replay_and_pending_delivery_precedence(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "analysis-start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "analysis-complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_READY_RECORDED, sm.current_revision, "packet-ready"))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_STARTED, sm.current_revision, "packet-start"))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_RECEIPT_RECORDED, sm.current_revision, "packet-receipt", {"status": "delivered"}))
    accepted(sm, event(c, c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED, sm.current_revision, "policy", {"receipt_ref": "policy"}))
    accepted(sm, event(c, c.EventKind.CUSTOMER_DELIVERY_STARTED, sm.current_revision, "customer-start", {"receipt_ref": "policy"}))
    deletion = event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete")
    accepted(sm, deletion)
    replay = sm.apply(deletion)
    assert replay.accepted and replay.replayed
    late = sm.apply(event(c, c.EventKind.CUSTOMER_DELIVERY_RECEIPT_RECORDED, sm.current_revision, "late-receipt", {"status": "delivered"}))
    assert not late.accepted and late.decision_code == "deletion_pending"
    verified = accepted(sm, event(c, c.EventKind.DELETION_VERIFIED, sm.current_revision, "verified"))
    assert sm.apply(event(c, c.EventKind.DELETION_VERIFIED, sm.current_revision - 1, "verified")).replayed
    assert verified.projection.next_action is None


def test_packet_and_policy_receipts_are_monotonic_for_new_event_ids(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "analysis-start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "analysis-complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_READY_RECORDED, sm.current_revision, "packet-ready"))
    duplicate_packet = sm.apply(event(c, c.EventKind.CONTRACTOR_PACKET_READY_RECORDED, sm.current_revision, "packet-ready-2"))
    assert not duplicate_packet.accepted and duplicate_packet.decision_code == "packet_already_ready"
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_STARTED, sm.current_revision, "packet-start"))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_RECEIPT_RECORDED, sm.current_revision, "packet-receipt", {"status": "delivered"}))
    accepted(sm, event(c, c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED, sm.current_revision, "policy", {"receipt_ref": "policy"}))
    duplicate_policy = sm.apply(event(c, c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED, sm.current_revision, "policy-2", {"receipt_ref": "policy-2"}))
    assert not duplicate_policy.accepted and duplicate_policy.decision_code == "policy_receipt_already_recorded"
    accepted(sm, event(c, c.EventKind.CUSTOMER_DELIVERY_STARTED, sm.current_revision, "customer-start", {"receipt_ref": "policy"}))
    late_policy = sm.apply(event(c, c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED, sm.current_revision, "policy-3", {"receipt_ref": "policy-3"}))
    assert not late_policy.accepted and late_policy.decision_code == "policy_receipt_already_recorded"


def test_policy_receipt_and_customer_delivery_mismatch_reject(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    wrong = sm.apply(event(c, c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED, sm.current_revision, "policy", {"receipt_ref": "wrong"}))
    assert not wrong.accepted and wrong.decision_code == "invalid_receipt_ref"
    accepted(sm, event(c, c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED, sm.current_revision, "policy-ok", {"receipt_ref": "policy-ok"}))
    mismatch = sm.apply(event(c, c.EventKind.CUSTOMER_DELIVERY_STARTED, sm.current_revision, "customer", {"receipt_ref": "wrong"}))
    assert not mismatch.accepted and mismatch.decision_code == "policy_receipt_binding_mismatch"


def test_submitted_media_resolution_requires_its_own_asset_role(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, sm.current_revision, "plate", {
        "request_id": "plate-request", "action_kind": "rating_plate", "budget_bucket": "rating_plate", "receipt_ref": "plate", "copy_ref": "plate-copy",
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, sm.current_revision, "plate-upload", {"asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100, "digest": "e" * 64}))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, sm.current_revision, "plate-final", {"asset_id": "plate-asset"}))
    wrong_asset = sm.apply(event(c, c.EventKind.MEDIA_ACTION_RESOLVED, sm.current_revision, "plate-wrong", {
        "request_id": "plate-request", "action_kind": "rating_plate", "media_role": "rating_plate", "status": "declined", "asset_id": "asset-video", "validation": "validated",
    }))
    assert not wrong_asset.accepted and wrong_asset.decision_code == "media_resolution_binding_mismatch"


def test_submitted_media_resolution_rejects_each_binding_field_mismatch(modules):
    c, state = modules
    for index, updates in enumerate((
        {"request_id": "wrong-request"},
        {"action_kind": "targeted_recapture"},
        {"media_role": "targeted_recapture"},
        {"validation": "rejected"},
        {"asset_id": "asset-video"},
    )):
        sm = state.VisualTriageStateMachine()
        bootstrap_valid(sm, c)
        accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, f"start-{index}"))
        accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, f"complete-{index}", {"outcome": "complete"}))
        accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, sm.current_revision, f"plate-{index}", {
            "request_id": "plate-request", "action_kind": "rating_plate", "budget_bucket": "rating_plate", "receipt_ref": f"plate-{index}", "copy_ref": "plate-copy",
        }))
        accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, sm.current_revision, f"upload-{index}", {"asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100, "digest": "a" * 64}))
        accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, sm.current_revision, f"final-{index}", {"asset_id": "plate-asset"}))
        accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, sm.current_revision, f"valid-{index}", {"asset_id": "plate-asset", "validation": "validated"}))
        payload = {"request_id": "plate-request", "action_kind": "rating_plate", "media_role": "rating_plate", "status": "fulfilled", "asset_id": "plate-asset", "validation": "validated"}
        payload.update(updates)
        blocked = sm.apply(event(c, c.EventKind.MEDIA_ACTION_RESOLVED, sm.current_revision, f"resolve-{index}", payload))
        assert not blocked.accepted and blocked.decision_code in {"action_binding_mismatch", "media_fulfillment_binding_mismatch", "media_resolution_binding_mismatch"}


def test_media_action_expiry_is_time_bound_and_replayable(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, sm.current_revision, "plate", {
        "request_id": "plate-request", "action_kind": "rating_plate", "budget_bucket": "rating_plate", "receipt_ref": "plate", "copy_ref": "plate-copy", "expires_at": "2026-08-12T00:10:00+00:00",
    }))
    early = sm.apply(event(c, c.EventKind.MEDIA_ACTION_RESOLVED, sm.current_revision, "early", {
        "request_id": "plate-request", "action_kind": "rating_plate", "media_role": "rating_plate", "status": "expired",
    }))
    assert not early.accepted and early.decision_code == "action_not_expired"
    expired = accepted(sm, event(c, c.EventKind.MEDIA_ACTION_RESOLVED, sm.current_revision, "expired", {
        "request_id": "plate-request", "action_kind": "rating_plate", "media_role": "rating_plate", "status": "expired",
    }, at=datetime(2026, 8, 12, 0, 11, tzinfo=timezone.utc)))
    assert expired.projection.analysis_status is c.AnalysisStatus.READY
    assert sm.apply(event(c, c.EventKind.MEDIA_ACTION_RESOLVED, sm.current_revision - 1, "expired", {
        "request_id": "plate-request", "action_kind": "rating_plate", "media_role": "rating_plate", "status": "expired",
    }, at=datetime(2026, 8, 12, 0, 11, tzinfo=timezone.utc))).replayed


def test_interrupted_media_upload_can_be_cancelled_without_case_termination(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, sm.current_revision, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate", "budget_bucket": "rating_plate",
        "receipt_ref": "plate-issue", "copy_ref": "plate-copy",
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, sm.current_revision, "plate-upload", {
        "asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100, "digest": "f" * 64,
    }))
    cancelled = accepted(sm, event(c, c.EventKind.MEDIA_ACTION_RESOLVED, sm.current_revision, "plate-cancel", {
        "request_id": "plate-request", "action_kind": "rating_plate", "media_role": "rating_plate", "status": "cancelled",
        "asset_id": "plate-asset",
    }))
    assert cancelled.projection.analysis_status is c.AnalysisStatus.READY
    # The aborted-upload asset must not linger as a phantom PENDING record.
    aborted_asset = next(a for a in sm.case.media_assets if a.asset_id == "plate-asset")
    assert aborted_asset.validation is c.MediaValidation.UNAVAILABLE
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "restart-after-cancel"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "restart-complete", {"outcome": "complete"}))
    assert sm.case.pending_customer_action is None


def test_interrupted_media_upload_can_expire_and_replay(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, sm.current_revision, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate", "budget_bucket": "rating_plate",
        "receipt_ref": "plate-issue", "copy_ref": "plate-copy", "expires_at": "2026-08-12T00:10:00+00:00",
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, sm.current_revision, "plate-upload", {
        "asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100, "digest": "f" * 64,
    }))
    expired = event(c, c.EventKind.MEDIA_ACTION_RESOLVED, sm.current_revision, "plate-expired", {
        "request_id": "plate-request", "action_kind": "rating_plate", "media_role": "rating_plate", "status": "expired",
        "asset_id": "plate-asset",
    }, at=datetime(2026, 8, 12, 0, 11, tzinfo=timezone.utc))
    result = accepted(sm, expired)
    assert result.projection.analysis_status is c.AnalysisStatus.READY
    # The aborted-upload asset must not linger as a phantom PENDING record.
    aborted_asset = next(a for a in sm.case.media_assets if a.asset_id == "plate-asset")
    assert aborted_asset.validation is c.MediaValidation.UNAVAILABLE
    assert sm.apply(expired).replayed


def test_aborted_media_upload_finalizes_phantom_asset_as_unavailable(modules):
    # If UPLOAD_STARTED already ran, the pending action's asset exists in
    # _media_assets with validation=PENDING. Aborting (cancel/expire) while
    # still UPLOADING must finalize that asset rather than silently
    # abandoning it -- otherwise every future snapshot()/.case.media_assets
    # keeps reporting an asset "still awaiting validation" forever, even
    # after the case reaches case_closed. This is the dedicated proving test
    # for that defect; the two sibling tests above also assert the fixed
    # behavior inline for the cancel and expire paths respectively.
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, sm.current_revision, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate", "budget_bucket": "rating_plate",
        "receipt_ref": "plate-issue", "copy_ref": "plate-copy",
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, sm.current_revision, "plate-upload", {
        "asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100, "digest": "f" * 64,
    }))
    before = sm.case.media_assets
    pending_asset_before = next(a for a in before if a.asset_id == "plate-asset")
    assert pending_asset_before.validation is c.MediaValidation.PENDING
    cancelled = accepted(sm, event(c, c.EventKind.MEDIA_ACTION_RESOLVED, sm.current_revision, "plate-cancel", {
        "request_id": "plate-request", "action_kind": "rating_plate", "media_role": "rating_plate",
        "status": "cancelled", "asset_id": "plate-asset",
    }))
    assert cancelled.projection.analysis_status is c.AnalysisStatus.READY
    asset_after = next(a for a in sm.case.media_assets if a.asset_id == "plate-asset")
    assert asset_after.validation is c.MediaValidation.UNAVAILABLE
    # The case can still reach case_closed afterward -- the asset must not
    # keep the case perpetually non-quiescent, and the snapshot must never
    # regress back to reporting it PENDING.
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "restart"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "restart-complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_READY_RECORDED, sm.current_revision, "packet-ready"))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_STARTED, sm.current_revision, "packet-start"))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_RECEIPT_RECORDED, sm.current_revision, "packet-receipt", {"status": "delivered"}))
    accepted(sm, event(c, c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED, sm.current_revision, "policy", {"receipt_ref": "policy"}))
    accepted(sm, event(c, c.EventKind.CUSTOMER_DELIVERY_STARTED, sm.current_revision, "customer-start", {"receipt_ref": "policy"}))
    accepted(sm, event(c, c.EventKind.CUSTOMER_DELIVERY_RECEIPT_RECORDED, sm.current_revision, "customer-receipt", {"status": "delivered"}))
    closed = accepted(sm, event(c, c.EventKind.CASE_CLOSED, sm.current_revision, "close"))
    assert closed.projection.case_status is c.CaseStatus.CLOSED
    final_asset = next(a for a in sm.case.media_assets if a.asset_id == "plate-asset")
    assert final_asset.validation is c.MediaValidation.UNAVAILABLE


def test_action_expiry_must_follow_issue_time(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    invalid = sm.apply(event(c, c.EventKind.MEDIA_ACTION_ISSUED, sm.current_revision, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate", "budget_bucket": "rating_plate",
        "receipt_ref": "plate-issue", "copy_ref": "plate-copy", "expires_at": "2026-08-12T00:00:00+00:00",
    }))
    assert not invalid.accepted and invalid.decision_code == "invalid_action_expiry"


def test_stale_and_future_revisions_reject_without_mutation(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    stale = sm.apply(event(c, c.EventKind.CONSENT_REQUESTED, 0, "stale"))
    future = sm.apply(event(c, c.EventKind.CONSENT_REQUESTED, 2, "future"))
    assert not stale.accepted and stale.decision_code == "stale_or_future_revision"
    assert not future.accepted and future.decision_code == "stale_or_future_revision"
    assert sm.current_revision == 1


def test_control_lane_capacity_is_explicit(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    receipt = sm.case.ordinary_receipts[0]
    sm._control_receipts = [receipt] * 6
    blocked = sm.apply(event(c, c.EventKind.CASE_CANCELLED, sm.current_revision, "control-overflow"))
    assert not blocked.accepted and blocked.decision_code == "control_lane_capacity"


def test_delivery_receipts_are_idempotent(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_READY_RECORDED, sm.current_revision, "packet-ready"))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_STARTED, sm.current_revision, "packet-start"))
    packet = event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_RECEIPT_RECORDED, sm.current_revision, "packet-receipt", {"status": "delivered"})
    accepted(sm, packet)
    assert sm.apply(packet).replayed
    accepted(sm, event(c, c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED, sm.current_revision, "policy", {"receipt_ref": "policy"}))
    accepted(sm, event(c, c.EventKind.CUSTOMER_DELIVERY_STARTED, sm.current_revision, "customer-start", {"receipt_ref": "policy"}))
    customer = event(c, c.EventKind.CUSTOMER_DELIVERY_RECEIPT_RECORDED, sm.current_revision, "customer-receipt", {"status": "delivered"})
    accepted(sm, customer)
    assert sm.apply(customer).replayed


def test_distinct_duplicate_transition_events_are_rejected(modules):
    c, state = modules

    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, 2, "grant"))
    upload = {"asset_id": "asset", "media_type": "video/mp4", "byte_size": 100, "duration_ms": 1000, "width": 320, "height": 240, "digest": "a" * 64}
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 3, "upload", upload))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, 4, "final", {"asset_id": "asset"}))
    duplicate_final = sm.apply(event(c, c.EventKind.UPLOAD_FINALIZED, 5, "final-2", {"asset_id": "asset"}))
    assert not duplicate_final.accepted

    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    duplicate_complete = sm.apply(event(c, c.EventKind.ANALYSIS_COMPLETED, 8, "complete-2", {"outcome": "complete"}))
    assert not duplicate_complete.accepted
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_READY_RECORDED, 8, "packet-ready"))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_STARTED, 9, "packet-start"))
    duplicate_packet_start = sm.apply(event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_STARTED, 10, "packet-start-2"))
    assert not duplicate_packet_start.accepted
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_RECEIPT_RECORDED, 10, "packet-receipt", {"status": "delivered"}))
    duplicate_packet_receipt = sm.apply(event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_RECEIPT_RECORDED, 11, "packet-receipt-2", {"status": "delivered"}))
    assert not duplicate_packet_receipt.accepted
    accepted(sm, event(c, c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED, 11, "policy", {"receipt_ref": "policy"}))
    accepted(sm, event(c, c.EventKind.CUSTOMER_DELIVERY_STARTED, 12, "customer-start", {"receipt_ref": "policy"}))
    duplicate_customer_start = sm.apply(event(c, c.EventKind.CUSTOMER_DELIVERY_STARTED, 13, "customer-start-2", {"receipt_ref": "policy"}))
    assert not duplicate_customer_start.accepted
    accepted(sm, event(c, c.EventKind.CUSTOMER_DELIVERY_RECEIPT_RECORDED, 13, "customer-receipt", {"status": "delivered"}))
    duplicate_customer_receipt = sm.apply(event(c, c.EventKind.CUSTOMER_DELIVERY_RECEIPT_RECORDED, 14, "customer-receipt-2", {"status": "delivered"}))
    assert not duplicate_customer_receipt.accepted
    accepted(sm, event(c, c.EventKind.CASE_CLOSED, 14, "close"))
    duplicate_close = sm.apply(event(c, c.EventKind.CASE_CLOSED, 15, "close-2"))
    assert not duplicate_close.accepted


def test_not_sure_resolution_replays_without_repetition(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, sm.current_revision, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question", "budget_bucket": "question", "receipt_ref": "question", "locale": "und", "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }))
    answer = event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, sm.current_revision, "not-sure", {
        "request_id": "question-request", "action_kind": "diagnostic_question", "locale": "und", "status": "not_sure",
    })
    accepted(sm, answer)
    assert sm.apply(answer).replayed
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "resume"))


def test_terminal_replay_exposes_historical_and_current_role_safe_projections(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(sm, event(c, c.EventKind.CASE_CANCELLED, sm.current_revision, "cancel"))
    replay = sm.apply(event(c, c.EventKind.CASE_CANCELLED, 1, "cancel"))
    assert replay.replayed and replay.historical_revision == 2
    assert replay.current_revision == sm.current_revision
    for role in c.ProjectionRole:
        projection = sm.project(role)
        assert projection.next_action is None


def test_deletion_pending_clears_packet_and_customer_delivery_actions(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_READY_RECORDED, sm.current_revision, "packet-ready"))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_DELIVERY_STARTED, sm.current_revision, "packet-start"))
    accepted(sm, event(c, c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED, sm.current_revision, "policy", {"receipt_ref": "policy"}))
    accepted(sm, event(c, c.EventKind.CUSTOMER_DELIVERY_STARTED, sm.current_revision, "customer-start", {"receipt_ref": "policy"}))
    deletion = accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete"))
    assert deletion.projection.next_action is None
    assert sm.project(c.ProjectionRole.CUSTOMER).next_action is None
    assert sm.project(c.ProjectionRole.CONTRACTOR).next_action is None


def test_fingerprint_field_mutations_are_rejected_without_mutation(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    original = event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"})
    accepted(sm, original)
    variants = (
        ("expected_revision", event(c, c.EventKind.CASE_CREATED, 1, "create", {"scenario": "hvac.demo"})),
        ("payload", event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo", "source_ref": "call-ref-alt"})),
        ("event_time", event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"}, at=datetime(2026, 8, 13, tzinfo=timezone.utc))),
        ("source_kind", event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"}, source=c.EventSource.SYSTEM)),
        ("retry_stage_and_attempt", event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"}, retry_stage="analysis", retry_attempt=1)),
    )
    for label, variant in variants:
        # Each variant is a valid, correctly-fingerprinted alternate envelope
        # reusing the original event_id, so it must hit event_id_conflict
        # specifically, without mutating revision or projection.
        revision_before = sm.current_revision
        projection_before = sm.project(c.ProjectionRole.INTERNAL)
        result = sm.apply(variant)
        assert not result.accepted, label
        assert result.decision_code == "event_id_conflict", (label, result.decision_code)
        assert sm.current_revision == revision_before, label
        assert sm.project(c.ProjectionRole.INTERNAL) == projection_before, label
    assert sm.current_revision == 1

    # schema_version/evidence_scope can never form a valid alternate envelope:
    # assert_integrity()'s whitelist check rejects them even in a hand-built,
    # self-consistent event that bypasses .build() entirely.


def test_each_event_envelope_field_is_fingerprint_bound(modules):
    # model_copy() skips validators, so each mutation is self-inconsistent
    # (stale fingerprint) rather than a valid alternate envelope, so this can
    # only prove tamper-evidence -- it never reaches event_id_conflict.
    c, state = modules
    expected_codes = {
        "schema_version": "event_integrity_mismatch",
        "case_id": "binding_mismatch",
        "contractor_id": "binding_mismatch",
        "kind": "event_integrity_mismatch",
        "payload": "event_integrity_mismatch",
        "canonical_payload_digest": "event_integrity_mismatch",
        "expected_revision": "event_integrity_mismatch",
        "source_kind": "event_integrity_mismatch",
        "event_time": "event_integrity_mismatch",
        "retry_stage": "event_integrity_mismatch",
        "retry_attempt": "event_integrity_mismatch",
        "evidence_scope": "event_integrity_mismatch",
    }
    variants = (
        {"schema_version": "visual_diagnosis.v999"},
        {"case_id": "other-case"},
        {"contractor_id": "other-contractor"},
        {"kind": c.EventKind.CONSENT_REQUESTED},
        {"payload": {"scenario": "hvac.other"}},
        {"canonical_payload_digest": "0" * 64},
        {"expected_revision": 1},
        {"source_kind": c.EventSource.SYSTEM},
        {"event_time": datetime(2026, 8, 13, tzinfo=timezone.utc)},
        {"retry_stage": "analysis"},
        {"retry_attempt": 1},
        {"evidence_scope": "live"},
    )
    assert set(expected_codes) == {next(iter(updates)) for updates in variants}
    for index, updates in enumerate(variants):
        sm = state.VisualTriageStateMachine()
        original = event(c, c.EventKind.CASE_CREATED, 0, f"create-{index}", {"scenario": "hvac.demo"})
        accepted(sm, original)
        mutated = original.model_copy(update=updates)
        result = sm.apply(mutated)
        field = next(iter(updates))
        assert not result.accepted, field
        assert result.decision_code == expected_codes[field], (field, result.decision_code)


def test_deletion_pending_rejects_late_non_deletion_events(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete"))
    late_events = (
        event(c, c.EventKind.CONSENT_REQUESTED, sm.current_revision, "late-consent"),
        event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "late-analysis"),
        event(c, c.EventKind.UPLOAD_STARTED, sm.current_revision, "late-upload", {
            "asset_id": "late-asset", "media_type": "video/mp4", "byte_size": 100,
            "duration_ms": 1000, "width": 320, "height": 240, "digest": "l" * 64,
        }),
        event(c, c.EventKind.CUSTOMER_DELIVERY_RECEIPT_RECORDED, sm.current_revision, "late-delivery", {"status": "delivered"}),
    )
    for late in late_events:
        result = sm.apply(late)
        assert not result.accepted and result.decision_code == "deletion_pending"


def test_deletion_pending_rejects_every_non_deletion_event_kind(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete"))
    payloads = {
        c.EventKind.CASE_CREATED: {"scenario": "hvac.demo"},
        c.EventKind.UPLOAD_STARTED: {"asset_id": "asset", "media_type": "video/mp4", "byte_size": 100, "duration_ms": 1000, "width": 320, "height": 240, "digest": "z" * 64},
        c.EventKind.UPLOAD_FINALIZED: {"asset_id": "asset"},
        c.EventKind.MEDIA_VALIDATED: {"asset_id": "asset", "validation": "validated"},
        c.EventKind.CUSTOMER_ACTION_RESOLVED: {"request_id": "request", "action_kind": "diagnostic_question", "locale": "und", "status": "cancelled"},
        c.EventKind.MEDIA_ACTION_RESOLVED: {"request_id": "request", "action_kind": "rating_plate", "media_role": "rating_plate", "status": "cancelled"},
        c.EventKind.CONTRACTOR_PACKET_DELIVERY_RECEIPT_RECORDED: {"status": "delivered"},
        c.EventKind.CUSTOMER_DELIVERY_RECEIPT_RECORDED: {"status": "delivered"},
        c.EventKind.ANALYSIS_COMPLETED: {"outcome": "complete"},
        c.EventKind.ANALYSIS_FAILED: {"failure_state": "failed_terminal"},
        c.EventKind.DELIVERY_POLICY_RECEIPT_RECORDED: {"receipt_ref": "policy"},
        c.EventKind.CUSTOMER_DELIVERY_STARTED: {"receipt_ref": "policy"},
    }
    excluded = {c.EventKind.DELETION_REQUESTED, c.EventKind.DELETION_RETRY_RECORDED, c.EventKind.DELETION_VERIFIED}
    for index, kind in enumerate(c.EventKind):
        if kind in excluded:
            continue
        result = sm.apply(event(c, kind, sm.current_revision, f"late-kind-{index}", payloads.get(kind)))
        assert not result.accepted and result.decision_code == "deletion_pending"


def test_question_cancel_is_terminal_for_prompt_and_replayable(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, sm.current_revision, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question", "budget_bucket": "question",
        "receipt_ref": "question", "locale": "und", "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }))
    cancel = event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, sm.current_revision, "question-cancel", {
        "request_id": "question-request", "action_kind": "diagnostic_question", "locale": "und", "status": "cancelled",
    })
    result = accepted(sm, cancel)
    assert result.projection.analysis_status is c.AnalysisStatus.ABSTAINED
    assert sm.apply(cancel).replayed
