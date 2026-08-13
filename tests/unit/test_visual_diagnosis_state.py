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
from datetime import datetime, timezone, tzinfo
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BOUND_BASELINE = "d2a2f003134a66b35cd76cabb8c2aaa43ca184f5"
PYTHON_REALPATH = "/opt/homebrew/Cellar/python@3.12/3.12.13/Frameworks/Python.framework/Versions/3.12/bin/python3.12"
PYTHON_DIGEST = "261a3951c895427210dfb7780693600b820f70841c078ab2554ee6fbeba7f376"
RUFF_PATH = Path("/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin/ruff")
RUFF_DIGEST = "1edd2e6e57286bdddedb1fb55493a91dc17f42838f3d6be488ded7cfe2a4f3a1"
CANDIDATE_HASHES = {
    "app/services/visual_diagnosis_contracts.py": "c921c9c2ce9ba3a3492ca125a9172b4757a4b48fd1ddba244b4c8e3988ba99d3",
    "app/services/visual_diagnosis_state.py": "b2a268134c8a7b1fe9ad0498eb6acde01e2390a8ff2354b510c596208099d93f",
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


def _local_isolation_check_enabled() -> bool:
    """Opt-in only. This dev machine's exact pinned toolchain (a specific
    Homebrew Python and venv-local ruff) and the sandbox-exec network-denial
    requirement are not portable to any other environment -- a different
    Mac, Linux, a container, or any CI other than this repo's own GitHub
    Actions all fail collection outright if these run unconditionally, so
    they must never run by default during ordinary test collection. Set
    this explicitly only when deliberately running the stricter local
    verification pass on this exact workstation; GitHub Actions (or any
    other environment) never sets it and always skips these checks.
    """

    return os.environ.get("VISUAL_DIAG_LOCAL_ISOLATION_CHECK") == "1"


def _guard_before_import(*, require_environment: bool = True) -> None:
    # Clean-tree + reviewed-ancestry check: works whether the candidate
    # files are still uncommitted (local dev, pre-PR) or already committed
    # on a feature branch (CI, PR review). Ancestry is a monotonic check
    # (never un-true once satisfied), so it stays valid forever after this
    # branch merges -- unlike a diff comparison against origin/main: these
    # test files are collected on every future pytest run once merged,
    # including unrelated PRs, whose origin/main..HEAD diff would never be a
    # subset of this feature's own file allowlist. A diff-scope assertion
    # here would break CI on every future PR that touches anything outside
    # these files, so EXPECTED_PATHS stays a record of what this feature's
    # own review covered, not a live per-run constraint. Baseline provenance
    # is an ancestor check rather than an exact HEAD pin, because CI checks
    # out an ephemeral merge commit for pull_request events, not the branch
    # tip itself.
    if _local_isolation_check_enabled():
        # The repo-wide dirty-tree scan below is a point-in-time proof for
        # this feature's own pre-merge review, not a permanent collection
        # gate: it inspects the ENTIRE repo's git status, not just this
        # feature's paths, so leaving it unconditional would fail collection
        # for any developer with an unrelated uncommitted or untracked file
        # anywhere in the repo -- the ordinary mid-edit state of active
        # development. Opt in explicitly on this workstation instead.
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

    if _local_isolation_check_enabled():
        # A gitignored .env, secrets/, or *.p8 file is normal, expected local
        # dev state (explicitly permitted by this repo's own .gitignore),
        # not a sign of tampering -- scanning for it unconditionally would
        # fail collection for any developer who has one anywhere in the
        # repo. The candidate modules are already statically restricted from
        # importing configuration or filesystem APIs, so this is a
        # workstation opt-in check like the others, not a permanent
        # collection gate.
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

    if _local_isolation_check_enabled():
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
    if _local_isolation_check_enabled():
        assert os.path.realpath(sys.executable) == PYTHON_REALPATH
        assert hashlib.sha256(Path(PYTHON_REALPATH).read_bytes()).hexdigest() == PYTHON_DIGEST
        assert hashlib.sha256(RUFF_PATH.read_bytes()).hexdigest() == RUFF_DIGEST
    pydantic = importlib.import_module("pydantic")
    assert pydantic.__version__ == "2.12.5"
    # pyproject.toml and the live-root files (app/main.py, Dockerfile, the
    # deploy workflows) are NOT hash-pinned here, deliberately: like the
    # diff-scope check removed above, pinning their exact bytes was only
    # ever a point-in-time proof for this feature's own pre-merge review.
    # Once merged, these test files are collected on every future pytest
    # run, including unrelated PRs -- a routine dependency bump in
    # pyproject.toml, or any legitimate future change to those live-root
    # files, would fail every subsequent PR's collection until someone
    # remembered to update these hardcoded digests here. The permanent,
    # ongoing guarantee that actually matters (no live route reaches the
    # candidate modules) is enforced below by scanning for the module names
    # themselves, which stays valid regardless of those files' content.
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
    if _local_isolation_check_enabled():
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


def event_with_raw_retry_attempt(c, kind, revision, event_id, payload, retry_stage, retry_attempt, at=None):
    """Build a fully self-consistent event whose envelope-level
    retry_attempt is exactly the raw value given (e.g. a bool), bypassing
    VisualTriageEvent.build()'s strict-mode field validation via
    model_construct(). .build() itself rejects retry_attempt=True outright
    (strict mode), so this reproduces what a caller could still do via
    Pydantic's public model_construct() -- a real bypass distinct from
    model_copy(update=...), which the existing fingerprint-tamper tests
    already cover.
    """

    event_time = at or datetime(2026, 8, 12, tzinfo=timezone.utc)
    payload_digest = c._sha256(payload)
    envelope = {
        "schema_version": c.SCHEMA_VERSION,
        "case_id": "case-1",
        "contractor_id": "contractor-1",
        "event_kind": kind.value,
        "canonical_payload_digest": payload_digest,
        "expected_revision": revision,
        "source_kind": c.EventSource.SYNTHETIC.value,
        "event_time": event_time.isoformat(),
        "retry_stage": retry_stage,
        "retry_attempt": retry_attempt,
        "evidence_scope": c.EVIDENCE_SCOPE,
    }
    return c.VisualTriageEvent.model_construct(
        case_id="case-1", contractor_id="contractor-1", event_id=event_id,
        kind=kind, payload=payload,
        canonical_payload_digest=payload_digest,
        semantic_envelope_fingerprint=c._sha256(envelope),
        expected_revision=revision, source_kind=c.EventSource.SYNTHETIC,
        event_time=event_time, retry_stage=retry_stage, retry_attempt=retry_attempt,
        schema_version=c.SCHEMA_VERSION, evidence_scope=c.EVIDENCE_SCOPE,
    )


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


def test_media_validated_rejects_an_asset_that_is_not_the_pending_one(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)  # validates the symptom video at asset_id "asset-video"
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

    # The plate is now quarantined (pending validation). Submitting
    # MEDIA_VALIDATED against the *already-validated* symptom video instead
    # of the pending plate must not be accepted -- it must not silently
    # re-resolve an unrelated asset and move global media state out of
    # quarantine while the actual pending asset is left stranded.
    revision_before = sm.current_revision
    wrong_asset = sm.apply(event(c, c.EventKind.MEDIA_VALIDATED, revision_before, "wrong-asset-valid", {
        "asset_id": "asset-video", "validation": "validated",
    }))
    assert not wrong_asset.accepted
    assert wrong_asset.decision_code == "asset_not_pending_validation"
    assert sm.current_revision == revision_before
    plate_asset = next(a for a in sm.case.media_assets if a.asset_id == "plate-asset")
    assert plate_asset.validation.value == "pending"

    # The actual pending asset can still be validated normally afterward.
    accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, revision_before, "plate-valid", {
        "asset_id": "plate-asset", "validation": "validated",
    }))
    plate_asset = next(a for a in sm.case.media_assets if a.asset_id == "plate-asset")
    assert plate_asset.validation.value == "validated"


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


def test_answering_a_question_after_its_deadline_is_rejected(modules):
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
    revision_before = sm.current_revision
    late_answer = sm.apply(event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, revision_before, "late-answer", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "locale": "und", "status": "answered", "response_option_code": "yes",
    }, at=datetime(2026, 8, 12, 0, 11, tzinfo=timezone.utc)))
    assert not late_answer.accepted and late_answer.decision_code == "action_expired"
    assert sm.current_revision == revision_before
    assert sm.case.pending_customer_action is not None  # still pending, not silently resolved

    # The same deadline-passed action can still be resolved as expired.
    expired = accepted(sm, event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, revision_before, "actually-expired", {
        "request_id": "question-request", "action_kind": "diagnostic_question", "locale": "und", "status": "expired",
    }, at=datetime(2026, 8, 12, 0, 11, tzinfo=timezone.utc)))
    assert expired.projection.analysis_status is c.AnalysisStatus.ABSTAINED


def test_fulfilling_a_media_action_after_its_deadline_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, 8, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "budget_bucket": "rating_plate", "receipt_ref": "plate-issue", "copy_ref": "plate-copy",
        "expires_at": "2026-08-12T00:10:00+00:00",
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 9, "plate-upload", {
        "asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100,
        "digest": "c" * 64,
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, 10, "plate-final", {"asset_id": "plate-asset"}))
    accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, 11, "plate-valid", {
        "asset_id": "plate-asset", "validation": "validated",
    }))
    revision_before = sm.current_revision
    late_fulfillment = sm.apply(event(c, c.EventKind.MEDIA_ACTION_RESOLVED, revision_before, "late-fulfill", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "media_role": "rating_plate", "status": "fulfilled",
        "asset_id": "plate-asset", "validation": "validated",
    }, at=datetime(2026, 8, 12, 0, 11, tzinfo=timezone.utc)))
    assert not late_fulfillment.accepted and late_fulfillment.decision_code == "action_expired"
    assert sm.current_revision == revision_before
    assert sm.case.pending_customer_action is not None

    # Cancelling the same deadline-passed action is still allowed (a customer
    # withdrawal isn't governed by the response deadline).
    cancelled = sm.apply(event(c, c.EventKind.MEDIA_ACTION_RESOLVED, revision_before, "late-cancel", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "media_role": "rating_plate", "status": "cancelled",
        "asset_id": "plate-asset", "validation": "validated",
    }, at=datetime(2026, 8, 12, 0, 11, tzinfo=timezone.utc)))
    assert cancelled.accepted, cancelled.decision_code


def test_cancelling_a_submitted_media_action_with_a_stalled_validator_finalizes_the_asset(modules):
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
    # The action is now SUBMITTED and quarantined, but the validator never
    # responds -- the asset's validation stays PENDING indefinitely.
    plate_asset = next(a for a in sm.case.media_assets if a.asset_id == "plate-asset")
    assert plate_asset.validation.value == "pending"

    cancelled = accepted(sm, event(c, c.EventKind.MEDIA_ACTION_RESOLVED, sm.current_revision, "plate-cancel", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "media_role": "rating_plate", "status": "cancelled", "asset_id": "plate-asset",
    }))
    assert cancelled.projection.pending_action_status is None
    plate_asset = next(a for a in sm.case.media_assets if a.asset_id == "plate-asset")
    assert plate_asset.validation.value == "unavailable"
    # The media dimension itself must leave UPLOADED_QUARANTINED too --
    # otherwise the case is stranded (analysis_started requires VALIDATED,
    # case_closed rejects quarantine, and the now-unavailable asset can
    # never be validated).
    assert sm.case.state_vector.media.value != "uploaded_quarantined"
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "restart"))
    closed = accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "restart-complete", {"outcome": "complete"}))
    assert closed.accepted


def test_question_resolution_dated_before_issuance_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, 8, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }, at=datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc)))
    revision_before = sm.current_revision
    time_traveling_answer = sm.apply(event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, revision_before, "early-answer", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "locale": "und", "status": "answered", "response_option_code": "yes",
    }, at=datetime(2026, 8, 12, 0, 9, tzinfo=timezone.utc)))  # before issued_at
    assert not time_traveling_answer.accepted
    assert time_traveling_answer.decision_code == "action_resolved_before_issuance"
    assert sm.current_revision == revision_before
    assert sm.case.pending_customer_action is not None


def test_media_action_resolution_dated_before_issuance_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, 6, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, 7, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, 8, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "budget_bucket": "rating_plate", "receipt_ref": "plate-issue", "copy_ref": "plate-copy",
    }, at=datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc)))
    revision_before = sm.current_revision
    time_traveling_cancel = sm.apply(event(c, c.EventKind.MEDIA_ACTION_RESOLVED, revision_before, "early-cancel", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "media_role": "rating_plate", "status": "cancelled",
    }, at=datetime(2026, 8, 12, 0, 9, tzinfo=timezone.utc)))  # before issued_at
    assert not time_traveling_cancel.accepted
    assert time_traveling_cancel.decision_code == "action_resolved_before_issuance"
    assert sm.current_revision == revision_before


def test_followup_question_blocked_once_contractor_packet_is_ready(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_READY_RECORDED, sm.current_revision, "packet-ready"))
    late_question = sm.apply(event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, sm.current_revision, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }))
    assert not late_question.accepted
    assert late_question.decision_code == "output_already_prepared"


def test_followup_rating_plate_blocked_once_contractor_packet_is_ready(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_READY_RECORDED, sm.current_revision, "packet-ready"))
    late_plate = sm.apply(event(c, c.EventKind.MEDIA_ACTION_ISSUED, sm.current_revision, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "budget_bucket": "rating_plate", "receipt_ref": "plate-issue", "copy_ref": "plate-copy",
    }))
    assert not late_plate.accepted
    assert late_plate.decision_code == "output_already_prepared"


def test_case_cancelled_finalizes_an_in_flight_upload(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, 2, "grant"))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 3, "upload", {
        "asset_id": "asset-video", "media_type": "video/mp4", "byte_size": 100,
        "duration_ms": 10_000, "width": 320, "height": 240, "digest": "a" * 64,
    }))
    # The upload is still in flight (UPLOAD_PENDING, asset PENDING) when the
    # case is cancelled -- nothing can ever finalize/validate it afterward.
    cancelled = accepted(sm, event(c, c.EventKind.CASE_CANCELLED, sm.current_revision, "cancel"))
    assert cancelled.projection.media_status.value == "unavailable"
    asset = next(a for a in sm.case.media_assets if a.asset_id == "asset-video")
    assert asset.validation.value == "unavailable"


def test_terminal_cleanup_preserves_validated_symptom_evidence(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)  # validates the symptom video at asset_id "asset-video"
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, sm.current_revision, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate",
        "budget_bucket": "rating_plate", "receipt_ref": "plate-issue", "copy_ref": "plate-copy",
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, sm.current_revision, "plate-upload", {
        "asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100,
        "digest": "c" * 64,
    }))
    # The rating-plate upload is in flight (PENDING) when the case is
    # cancelled -- but the symptom video was separately already validated,
    # so the case still has genuine evidence.
    cancelled = accepted(sm, event(c, c.EventKind.CASE_CANCELLED, sm.current_revision, "cancel"))
    assert cancelled.projection.media_status.value == "validated"
    plate_asset = next(a for a in sm.case.media_assets if a.asset_id == "plate-asset")
    assert plate_asset.validation.value == "unavailable"
    video_asset = next(a for a in sm.case.media_assets if a.asset_id == "asset-video")
    assert video_asset.validation.value == "validated"



def test_consent_declined_and_withdrawn_set_completed_at(modules):
    c, state = modules
    declined = state.VisualTriageStateMachine()
    accepted(declined, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(declined, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(declined, event(c, c.EventKind.CONSENT_DECLINED, 2, "decline"))
    assert declined.case.completed_at is not None

    withdrawn = state.VisualTriageStateMachine()
    accepted(withdrawn, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(withdrawn, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(withdrawn, event(c, c.EventKind.CONSENT_GRANTED, 2, "grant"))
    accepted(withdrawn, event(c, c.EventKind.CONSENT_WITHDRAWN, 3, "withdraw"))
    assert withdrawn.case.completed_at is not None


def test_consent_withdrawn_finalizes_an_in_flight_upload(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, 2, "grant"))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 3, "upload", {
        "asset_id": "asset-video", "media_type": "video/mp4", "byte_size": 100,
        "duration_ms": 10_000, "width": 320, "height": 240, "digest": "a" * 64,
    }))
    withdrawn = accepted(sm, event(c, c.EventKind.CONSENT_WITHDRAWN, sm.current_revision, "withdraw"))
    assert withdrawn.projection.media_status.value == "unavailable"
    asset = next(a for a in sm.case.media_assets if a.asset_id == "asset-video")
    assert asset.validation.value == "unavailable"


def test_deletion_requested_finalizes_an_in_flight_upload(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create"))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, 1, "request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, 2, "grant"))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, 3, "upload", {
        "asset_id": "asset-video", "media_type": "video/mp4", "byte_size": 100,
        "duration_ms": 10_000, "width": 320, "height": 240, "digest": "a" * 64,
    }))
    deletion = accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete"))
    assert deletion.projection.media_status.value == "unavailable"
    asset = next(a for a in sm.case.media_assets if a.asset_id == "asset-video")
    assert asset.validation.value == "unavailable"


def test_terminal_case_events_before_creation_time_are_rejected(modules):
    c, state = modules
    for kind in (c.EventKind.CASE_CANCELLED, c.EventKind.CASE_EXPIRED):
        sm = state.VisualTriageStateMachine()
        accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create", at=datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc)))
        revision_before = sm.current_revision
        time_traveling = sm.apply(event(
            c, kind, revision_before, "terminal",
            at=datetime(2026, 8, 12, 0, 9, tzinfo=timezone.utc),  # before created_at
        ))
        assert not time_traveling.accepted, kind
        assert time_traveling.decision_code == "terminal_event_predates_case", (kind, time_traveling.decision_code)


def test_case_closed_before_creation_time_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"}, at=datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc)))
    accepted(sm, event(c, c.EventKind.CONSENT_REQUESTED, sm.current_revision, "request"))
    accepted(sm, event(c, c.EventKind.CONSENT_GRANTED, sm.current_revision, "grant"))
    accepted(sm, event(c, c.EventKind.UPLOAD_STARTED, sm.current_revision, "upload", {
        "asset_id": "asset-video", "media_type": "video/mp4", "byte_size": 100,
        "duration_ms": 10_000, "width": 320, "height": 240, "digest": "a" * 64,
    }))
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, sm.current_revision, "final", {"asset_id": "asset-video"}))
    accepted(sm, event(c, c.EventKind.MEDIA_VALIDATED, sm.current_revision, "validate", {
        "asset_id": "asset-video", "validation": "validated",
    }))
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    revision_before = sm.current_revision
    time_traveling = sm.apply(event(
        c, c.EventKind.CASE_CLOSED, revision_before, "close",
        at=datetime(2026, 8, 12, 0, 9, tzinfo=timezone.utc),  # before created_at
    ))
    assert not time_traveling.accepted
    assert time_traveling.decision_code == "terminal_event_predates_case"
    assert sm.current_revision == revision_before


def test_consent_declined_and_withdrawn_before_creation_time_are_rejected(modules):
    c, state = modules

    declined = state.VisualTriageStateMachine()
    accepted(declined, event(c, c.EventKind.CASE_CREATED, 0, "create", at=datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc)))
    accepted(declined, event(c, c.EventKind.CONSENT_REQUESTED, declined.current_revision, "request"))
    revision_before = declined.current_revision
    early_decline = declined.apply(event(
        c, c.EventKind.CONSENT_DECLINED, revision_before, "decline",
        at=datetime(2026, 8, 12, 0, 9, tzinfo=timezone.utc),  # before created_at
    ))
    assert not early_decline.accepted
    assert early_decline.decision_code == "terminal_event_predates_case"
    assert declined.current_revision == revision_before

    withdrawn = state.VisualTriageStateMachine()
    accepted(withdrawn, event(c, c.EventKind.CASE_CREATED, 0, "create", at=datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc)))
    accepted(withdrawn, event(c, c.EventKind.CONSENT_REQUESTED, withdrawn.current_revision, "request"))
    accepted(withdrawn, event(c, c.EventKind.CONSENT_GRANTED, withdrawn.current_revision, "grant"))
    revision_before = withdrawn.current_revision
    early_withdraw = withdrawn.apply(event(
        c, c.EventKind.CONSENT_WITHDRAWN, revision_before, "withdraw",
        at=datetime(2026, 8, 12, 0, 9, tzinfo=timezone.utc),  # before created_at
    ))
    assert not early_withdraw.accepted
    assert early_withdraw.decision_code == "terminal_event_predates_case"
    assert withdrawn.current_revision == revision_before


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

    # A 4th "still retriable" report is rejected once the 3-attempt retry
    # budget is spent -- accepting it would leave analysis in
    # FAILED_RETRIABLE with a 4th retry itself also rejected below and no
    # route left to a terminal analysis state (case_closed requires the
    # analysis out of FAILED_RETRIABLE).
    revision_before = sm.current_revision
    exhausted = sm.apply(event(c, c.EventKind.ANALYSIS_FAILED, revision_before, "fail-4", {"failure_state": "failed_retriable"}))
    assert not exhausted.accepted and exhausted.decision_code == "analysis_retry_exhausted"
    assert sm.current_revision == revision_before

    # Analysis is still PROCESSING (the exhausted claim above was rejected,
    # not accepted), so a retry can't be recorded against it either.
    fourth = sm.apply(event(c, c.EventKind.ANALYSIS_RETRY_RECORDED, revision_before, "retry-4", {"attempt": 4}, retry_stage="analysis", retry_attempt=3))
    assert not fourth.accepted and fourth.decision_code == "analysis_retry_not_allowed"

    # The caller correctly reports failed_terminal instead, and the case can
    # still reach closure -- the exhausted retry budget doesn't strand it.
    accepted(sm, event(c, c.EventKind.ANALYSIS_FAILED, revision_before, "fail-4-terminal", {"failure_state": "failed_terminal"}))
    closed = accepted(sm, event(c, c.EventKind.CASE_CLOSED, sm.current_revision, "close"))
    assert closed.projection.case_status.value == "closed"


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


def test_analysis_retry_recorded_rejects_boolean_envelope_retry_attempt(modules):
    # .build() itself rejects retry_attempt=True outright (strict mode), so
    # this uses model_construct() to reproduce a fully self-consistent event
    # (matching fingerprint) whose *envelope* retry_attempt is a bool while
    # the payload's "attempt" is a genuine int -- True == 1, so the
    # cross-check between them doesn't catch it on its own.
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start-1"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_FAILED, sm.current_revision, "fail-1", {"failure_state": "failed_retriable"}))
    revision_before = sm.current_revision
    malformed = event_with_raw_retry_attempt(
        c, c.EventKind.ANALYSIS_RETRY_RECORDED, revision_before, "retry-bool-envelope",
        {"attempt": 1}, "analysis", True,
    )
    result = sm.apply(malformed)
    assert not result.accepted
    assert result.decision_code == "retry_attempt_invalid"
    assert sm._analysis_retry_count == 0
    assert sm.current_revision == revision_before


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


def test_deletion_retry_recorded_rejects_boolean_envelope_retry_attempt(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete"))
    revision_before = sm.current_revision
    malformed = event_with_raw_retry_attempt(
        c, c.EventKind.DELETION_RETRY_RECORDED, revision_before, "retry-bool-envelope",
        {"attempt": 1}, "deletion", True,
    )
    result = sm.apply(malformed)
    assert not result.accepted
    assert result.decision_code == "deletion_retry_invalid"
    assert sm._deletion_retry_count == 0
    assert sm.current_revision == revision_before


def test_deletion_verified_before_creation_time_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create", at=datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc)))
    accepted(sm, event(
        c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete",
        at=datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc),
    ))
    revision_before = sm.current_revision
    time_traveling = sm.apply(event(
        c, c.EventKind.DELETION_VERIFIED, revision_before, "verify",
        at=datetime(2026, 8, 12, 0, 9, tzinfo=timezone.utc),  # before created_at
    ))
    assert not time_traveling.accepted
    assert time_traveling.decision_code == "terminal_event_predates_case"
    assert sm.current_revision == revision_before


def test_malformed_event_id_via_model_copy_does_not_partially_commit(modules):
    # event_id is not part of the semantic fingerprint, so
    # model_copy(update={"event_id": ...}) can smuggle a malformed one past
    # construction -- assert_integrity() now rejects it before dispatch.
    # apply() must not raise, and must not leave the aggregate revision
    # incremented with no corresponding ledger entry.
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"}))
    revision_before = sm.current_revision
    genuine = event(c, c.EventKind.CONSENT_REQUESTED, revision_before, "request")
    malformed = genuine.model_copy(update={"event_id": "bad id with spaces"})
    result = sm.apply(malformed)  # must not raise
    assert not result.accepted
    assert sm.current_revision == revision_before
    assert sm.case.state_vector.consent.value == "not_requested"


def test_unhashable_event_id_via_model_copy_does_not_raise(modules):
    # event_id is neither re-validated by assert_integrity() nor part of the
    # semantic fingerprint, so model_copy(update={"event_id": ...}) can
    # smuggle an unhashable value (e.g. a list) past dispatch. Pre-fix,
    # self._ledger.get(event.event_id) raises TypeError outside the
    # rollback-protected exception handler, violating apply()'s documented
    # non-throwing contract.
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"}))
    revision_before = sm.current_revision
    genuine = event(c, c.EventKind.CONSENT_REQUESTED, revision_before, "request")
    malformed = genuine.model_copy(update={"event_id": []})
    result = sm.apply(malformed)  # must not raise
    assert not result.accepted
    assert result.decision_code == "event_integrity_mismatch"
    assert sm.current_revision == revision_before
    assert sm.case.state_vector.consent.value == "not_requested"


def test_case_closed_before_last_resolved_action_time_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, sm.current_revision, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }))
    resolved_at = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
    accepted(sm, event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, sm.current_revision, "answer", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "locale": "und", "status": "answered", "response_option_code": "yes",
    }, at=resolved_at))
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "restart", at=resolved_at))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "restart-complete", {"outcome": "complete"}, at=resolved_at))
    revision_before = sm.current_revision
    early_close = sm.apply(event(
        c, c.EventKind.CASE_CLOSED, revision_before, "close",
        at=datetime(2026, 8, 12, 0, 30, tzinfo=timezone.utc),  # after created_at, before resolved_at
    ))
    assert not early_close.accepted
    assert early_close.decision_code == "terminal_event_predates_case"
    assert sm.current_revision == revision_before


def test_case_cancelled_and_expired_before_last_resolved_action_time_are_rejected(modules):
    c, state = modules
    for kind in (c.EventKind.CASE_CANCELLED, c.EventKind.CASE_EXPIRED):
        sm = state.VisualTriageStateMachine()
        bootstrap_valid(sm, c)
        accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
        accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
        accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, sm.current_revision, "question", {
            "request_id": "question-request", "action_kind": "diagnostic_question",
            "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
            "copy_ref": "question-copy", "response_option_codes": ["yes"],
        }))
        resolved_at = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
        accepted(sm, event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, sm.current_revision, "answer", {
            "request_id": "question-request", "action_kind": "diagnostic_question",
            "locale": "und", "status": "answered", "response_option_code": "yes",
        }, at=resolved_at))
        revision_before = sm.current_revision
        early_terminal = sm.apply(event(
            c, kind, revision_before, "terminal",
            at=datetime(2026, 8, 12, 0, 30, tzinfo=timezone.utc),  # after created_at, before resolved_at
        ))
        assert not early_terminal.accepted, kind
        assert early_terminal.decision_code == "terminal_event_predates_case", (kind, early_terminal.decision_code)
        assert sm.current_revision == revision_before


def test_non_dict_payload_via_model_construct_is_rejected(modules):
    # _validate_structural_value() accepts a list as a valid nested payload
    # VALUE (payloads legitimately contain list-typed fields), so calling it
    # on self.payload itself doesn't reject a non-dict top-level payload.
    # With a correctly hand-recomputed matching digest and fingerprint, a
    # list payload is fully self-consistent and must still be caught by
    # assert_integrity() before it reaches a handler that calls .get() on it.
    c, state = modules
    sm = state.VisualTriageStateMachine()
    payload = []
    payload_digest = c._sha256(payload)
    event_time = datetime(2026, 8, 12, tzinfo=timezone.utc)
    envelope = {
        "schema_version": c.SCHEMA_VERSION,
        "case_id": "case-1",
        "contractor_id": "contractor-1",
        "event_kind": c.EventKind.CASE_CREATED.value,
        "canonical_payload_digest": payload_digest,
        "expected_revision": 0,
        "source_kind": c.EventSource.SYNTHETIC.value,
        "event_time": event_time.isoformat(),
        "retry_stage": None,
        "retry_attempt": None,
        "evidence_scope": c.EVIDENCE_SCOPE,
    }
    malformed = c.VisualTriageEvent.model_construct(
        case_id="case-1", contractor_id="contractor-1", event_id="create",
        kind=c.EventKind.CASE_CREATED, payload=payload,
        canonical_payload_digest=payload_digest,
        semantic_envelope_fingerprint=c._sha256(envelope),
        expected_revision=0, source_kind=c.EventSource.SYNTHETIC,
        event_time=event_time, retry_stage=None, retry_attempt=None,
        schema_version=c.SCHEMA_VERSION, evidence_scope=c.EVIDENCE_SCOPE,
    )
    result = sm.apply(malformed)  # must not raise
    assert not result.accepted
    assert result.decision_code == "event_integrity_mismatch"
    assert sm.current_revision == 0
    assert sm.case is None


def test_terminal_transitions_reject_time_before_pending_action_issuance(modules):
    # The resolved-action-timestamp guards only ever look at
    # _resolved_customer_actions (actions that already finished); they never
    # check a still-open self._pending action's issued_at before it gets
    # silently discarded by the same transition. A terminal event can then
    # be timestamped before an action the case history shows was issued but
    # never resolved, corrupting the audit chronology the resolved-action
    # check exists to protect.
    c, state = modules
    for kind in (c.EventKind.CONSENT_WITHDRAWN, c.EventKind.CASE_CANCELLED, c.EventKind.CASE_EXPIRED):
        sm = state.VisualTriageStateMachine()
        bootstrap_valid(sm, c)
        accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
        accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
        issued_at = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
        accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, sm.current_revision, "question", {
            "request_id": "question-request", "action_kind": "diagnostic_question",
            "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
            "copy_ref": "question-copy", "response_option_codes": ["yes"],
        }, at=issued_at))
        revision_before = sm.current_revision
        early_terminal = sm.apply(event(
            c, kind, revision_before, "terminal",
            at=datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc),  # after created_at, before the pending question's issued_at
        ))
        assert not early_terminal.accepted, kind
        assert early_terminal.decision_code == "terminal_event_predates_case", (kind, early_terminal.decision_code)
        assert sm.current_revision == revision_before


def test_malformed_case_identity_on_creation_via_model_construct_is_rejected(modules):
    # assert_integrity() validates event_id's format but, before this fix,
    # not case_id/contractor_id's -- it only checks that they're internally
    # consistent with the recomputed fingerprint, which a caller can always
    # satisfy by hand-recomputing that fingerprint over the malformed value.
    # The pre-dispatch binding check in apply() is skipped for the very
    # first event (no aggregate exists yet to bind against), so a malformed
    # case_id could previously be committed by _case_created. Every later
    # event with the real, valid case_id would then fail to match
    # self._case_id, and snapshot() would only fail much later trying to
    # construct VisualTriageCase with the malformed id.
    c, state = modules
    sm = state.VisualTriageStateMachine()
    payload = {"scenario": "hvac.demo", "source_ref": "call-ref"}
    payload_digest = c._sha256(payload)
    event_time = datetime(2026, 8, 12, tzinfo=timezone.utc)
    envelope = {
        "schema_version": c.SCHEMA_VERSION,
        "case_id": "bad id with spaces",
        "contractor_id": "contractor-1",
        "event_kind": c.EventKind.CASE_CREATED.value,
        "canonical_payload_digest": payload_digest,
        "expected_revision": 0,
        "source_kind": c.EventSource.SYNTHETIC.value,
        "event_time": event_time.isoformat(),
        "retry_stage": None,
        "retry_attempt": None,
        "evidence_scope": c.EVIDENCE_SCOPE,
    }
    malformed = c.VisualTriageEvent.model_construct(
        case_id="bad id with spaces", contractor_id="contractor-1", event_id="create",
        kind=c.EventKind.CASE_CREATED, payload=payload,
        canonical_payload_digest=payload_digest,
        semantic_envelope_fingerprint=c._sha256(envelope),
        expected_revision=0, source_kind=c.EventSource.SYNTHETIC,
        event_time=event_time, retry_stage=None, retry_attempt=None,
        schema_version=c.SCHEMA_VERSION, evidence_scope=c.EVIDENCE_SCOPE,
    )
    result = sm.apply(malformed)  # must not raise
    assert not result.accepted
    assert result.decision_code == "event_integrity_mismatch"
    assert sm.current_revision == 0
    assert sm.case is None


class _NoOffsetTzinfo(tzinfo):
    """A tzinfo that is not None but whose utcoffset() is still None --
    Python treats such a datetime as naive for arithmetic/comparison
    purposes even though `value.tzinfo is None` is False."""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return None


def test_effectively_naive_event_time_via_custom_tzinfo_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    payload = {"scenario": "hvac.demo", "source_ref": "call-ref"}
    payload_digest = c._sha256(payload)
    event_time = datetime(2026, 8, 12, tzinfo=_NoOffsetTzinfo())
    assert event_time.tzinfo is not None and event_time.utcoffset() is None
    envelope = {
        "schema_version": c.SCHEMA_VERSION,
        "case_id": "case-1",
        "contractor_id": "contractor-1",
        "event_kind": c.EventKind.CASE_CREATED.value,
        "canonical_payload_digest": payload_digest,
        "expected_revision": 0,
        "source_kind": c.EventSource.SYNTHETIC.value,
        "event_time": event_time.isoformat(),
        "retry_stage": None,
        "retry_attempt": None,
        "evidence_scope": c.EVIDENCE_SCOPE,
    }
    malformed = c.VisualTriageEvent.model_construct(
        case_id="case-1", contractor_id="contractor-1", event_id="create",
        kind=c.EventKind.CASE_CREATED, payload=payload,
        canonical_payload_digest=payload_digest,
        semantic_envelope_fingerprint=c._sha256(envelope),
        expected_revision=0, source_kind=c.EventSource.SYNTHETIC,
        event_time=event_time, retry_stage=None, retry_attempt=None,
        schema_version=c.SCHEMA_VERSION, evidence_scope=c.EVIDENCE_SCOPE,
    )
    result = sm.apply(malformed)  # must not raise
    assert not result.accepted
    assert result.decision_code == "event_integrity_mismatch"
    assert sm.current_revision == 0
    assert sm.case is None


def test_boolean_expected_revision_via_model_construct_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"}))
    revision_before = sm.current_revision  # 1
    payload = {}
    payload_digest = c._sha256(payload)
    event_time = datetime(2026, 8, 12, tzinfo=timezone.utc)
    envelope = {
        "schema_version": c.SCHEMA_VERSION,
        "case_id": "case-1",
        "contractor_id": "contractor-1",
        "event_kind": c.EventKind.CONSENT_REQUESTED.value,
        "canonical_payload_digest": payload_digest,
        "expected_revision": True,
        "source_kind": c.EventSource.SYNTHETIC.value,
        "event_time": event_time.isoformat(),
        "retry_stage": None,
        "retry_attempt": None,
        "evidence_scope": c.EVIDENCE_SCOPE,
    }
    malformed = c.VisualTriageEvent.model_construct(
        case_id="case-1", contractor_id="contractor-1", event_id="request",
        kind=c.EventKind.CONSENT_REQUESTED, payload=payload,
        canonical_payload_digest=payload_digest,
        semantic_envelope_fingerprint=c._sha256(envelope),
        expected_revision=True, source_kind=c.EventSource.SYNTHETIC,
        event_time=event_time, retry_stage=None, retry_attempt=None,
        schema_version=c.SCHEMA_VERSION, evidence_scope=c.EVIDENCE_SCOPE,
    )
    result = sm.apply(malformed)  # must not raise
    assert not result.accepted
    assert result.decision_code == "event_integrity_mismatch"
    assert sm.current_revision == revision_before


def test_naive_event_time_via_model_construct_is_rejected(modules):
    # .build() itself rejects a naive event_time outright (strict field
    # validator), but model_construct() bypasses that; with a correctly
    # hand-recomputed matching fingerprint (isoformat() works fine on a
    # naive datetime, it just omits the offset), the resulting event is
    # fully self-consistent and must still be caught by assert_integrity().
    c, state = modules
    sm = state.VisualTriageStateMachine()
    payload = {"scenario": "hvac.demo", "source_ref": "call-ref"}
    payload_digest = c._sha256(payload)
    naive_time = datetime(2026, 8, 12)
    envelope = {
        "schema_version": c.SCHEMA_VERSION,
        "case_id": "case-1",
        "contractor_id": "contractor-1",
        "event_kind": c.EventKind.CASE_CREATED.value,
        "canonical_payload_digest": payload_digest,
        "expected_revision": 0,
        "source_kind": c.EventSource.SYNTHETIC.value,
        "event_time": naive_time.isoformat(),
        "retry_stage": None,
        "retry_attempt": None,
        "evidence_scope": c.EVIDENCE_SCOPE,
    }
    malformed = c.VisualTriageEvent.model_construct(
        case_id="case-1", contractor_id="contractor-1", event_id="create",
        kind=c.EventKind.CASE_CREATED, payload=payload,
        canonical_payload_digest=payload_digest,
        semantic_envelope_fingerprint=c._sha256(envelope),
        expected_revision=0, source_kind=c.EventSource.SYNTHETIC,
        event_time=naive_time, retry_stage=None, retry_attempt=None,
        schema_version=c.SCHEMA_VERSION, evidence_scope=c.EVIDENCE_SCOPE,
    )
    result = sm.apply(malformed)  # must not raise
    assert not result.accepted
    assert result.decision_code == "event_integrity_mismatch"
    assert sm.current_revision == 0
    assert sm.case is None  # snapshot() must not raise either


def test_deletion_verified_before_deletion_requested_time_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    requested_at = datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc)
    accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete", at=requested_at))
    revision_before = sm.current_revision
    early_verification = sm.apply(event(
        c, c.EventKind.DELETION_VERIFIED, revision_before, "verify",
        at=datetime(2026, 8, 12, 0, 5, tzinfo=timezone.utc),  # after created_at, before deletion was requested
    ))
    assert not early_verification.accepted
    assert early_verification.decision_code == "terminal_event_predates_case", early_verification.decision_code
    assert sm.current_revision == revision_before


def test_deletion_verified_before_last_resolved_action_time_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, sm.current_revision, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }))
    resolved_at = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
    accepted(sm, event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, sm.current_revision, "answer", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "locale": "und", "status": "answered", "response_option_code": "yes",
    }, at=resolved_at))
    accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete", at=resolved_at))
    revision_before = sm.current_revision
    early_verification = sm.apply(event(
        c, c.EventKind.DELETION_VERIFIED, revision_before, "verify",
        at=datetime(2026, 8, 12, 0, 30, tzinfo=timezone.utc),  # after created_at, before resolved_at
    ))
    assert not early_verification.accepted
    assert early_verification.decision_code == "terminal_event_predates_case", early_verification.decision_code
    assert sm.current_revision == revision_before


def test_upload_started_before_action_issuance_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    issued_at = datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc)
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, sm.current_revision, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate", "budget_bucket": "rating_plate",
        "receipt_ref": "plate-issue", "copy_ref": "plate-copy",
    }, at=issued_at))
    revision_before = sm.current_revision
    early_upload = sm.apply(event(
        c, c.EventKind.UPLOAD_STARTED, revision_before, "plate-upload",
        {"asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100, "digest": "f" * 64},
        at=datetime(2026, 8, 12, 0, 5, tzinfo=timezone.utc),  # before the action was issued
    ))
    assert not early_upload.accepted
    assert early_upload.decision_code == "action_resolved_before_issuance", early_upload.decision_code
    assert sm.current_revision == revision_before


def test_upload_started_after_action_expiry_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.MEDIA_ACTION_ISSUED, sm.current_revision, "plate-issue", {
        "request_id": "plate-request", "action_kind": "rating_plate", "budget_bucket": "rating_plate",
        "receipt_ref": "plate-issue", "copy_ref": "plate-copy", "expires_at": "2026-08-12T00:10:00+00:00",
    }))
    revision_before = sm.current_revision
    late_upload = sm.apply(event(
        c, c.EventKind.UPLOAD_STARTED, revision_before, "plate-upload",
        {"asset_id": "plate-asset", "media_type": "image/jpeg", "byte_size": 100, "digest": "f" * 64},
        at=datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc),  # at the deadline, not before it
    ))
    assert not late_upload.accepted
    assert late_upload.decision_code == "action_expired", late_upload.decision_code
    assert sm.current_revision == revision_before


def test_upload_finalized_after_action_expiry_is_rejected(modules):
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
    revision_before = sm.current_revision
    late_finalize = sm.apply(event(
        c, c.EventKind.UPLOAD_FINALIZED, revision_before, "plate-final", {"asset_id": "plate-asset"},
        at=datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc),  # at the deadline, not before it
    ))
    assert not late_finalize.accepted
    assert late_finalize.decision_code == "action_expired", late_finalize.decision_code
    assert sm.current_revision == revision_before


def test_media_validated_after_action_expiry_is_rejected(modules):
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
    accepted(sm, event(c, c.EventKind.UPLOAD_FINALIZED, sm.current_revision, "plate-final", {"asset_id": "plate-asset"}))
    revision_before = sm.current_revision
    late_validate = sm.apply(event(
        c, c.EventKind.MEDIA_VALIDATED, revision_before, "plate-validate",
        {"asset_id": "plate-asset", "validation": "validated"},
        at=datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc),  # at the deadline, not before it
    ))
    assert not late_validate.accepted
    assert late_validate.decision_code == "action_expired", late_validate.decision_code
    assert sm.current_revision == revision_before


def test_analysis_retry_recorded_rejects_float_attempt(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_FAILED, sm.current_revision, "fail-1", {"failure_state": "failed_retriable"}))
    revision_before = sm.current_revision
    payload = {"attempt": 1}
    malformed = event_with_raw_retry_attempt(
        c, c.EventKind.ANALYSIS_RETRY_RECORDED, revision_before, "retry-1", payload,
        retry_stage="analysis", retry_attempt=1.0,
    )
    result = sm.apply(malformed)  # must not raise
    assert not result.accepted
    assert result.decision_code == "retry_attempt_invalid", result.decision_code
    assert sm.current_revision == revision_before


def test_deletion_retry_recorded_rejects_float_attempt(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.DELETION_REQUESTED, sm.current_revision, "delete"))
    revision_before = sm.current_revision
    payload = {"attempt": 1}
    malformed = event_with_raw_retry_attempt(
        c, c.EventKind.DELETION_RETRY_RECORDED, revision_before, "retry-1", payload,
        retry_stage="deletion", retry_attempt=1.0,
    )
    result = sm.apply(malformed)  # must not raise
    assert not result.accepted
    assert result.decision_code == "deletion_retry_invalid", result.decision_code
    assert sm.current_revision == revision_before


def test_deletion_requested_rejects_time_before_pending_action_issuance(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    issued_at = datetime(2026, 8, 12, 2, 0, tzinfo=timezone.utc)
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, sm.current_revision, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }, at=issued_at))
    revision_before = sm.current_revision
    early_deletion = sm.apply(event(
        c, c.EventKind.DELETION_REQUESTED, revision_before, "delete",
        at=datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc),  # after created_at, before the pending question's issued_at
    ))
    assert not early_deletion.accepted
    assert early_deletion.decision_code == "terminal_event_predates_case", early_deletion.decision_code
    assert sm.current_revision == revision_before


def test_recapture_blocked_after_outputs_are_prepared(modules):
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
    accepted(sm, event(c, c.EventKind.CONTRACTOR_PACKET_READY_RECORDED, 6, "packet-ready"))
    blocked = sm.apply(event(c, c.EventKind.MEDIA_ACTION_ISSUED, 7, "recapture-issue", {
        "request_id": "recapture-request", "action_kind": "targeted_recapture",
        "budget_bucket": "recapture", "copy_ref": "recapture-copy", "receipt_ref": "recapture-issue",
    }))
    assert not blocked.accepted
    assert blocked.decision_code == "output_already_prepared", blocked.decision_code


def test_consent_withdrawn_before_last_resolved_action_time_is_rejected(modules):
    c, state = modules
    sm = state.VisualTriageStateMachine()
    bootstrap_valid(sm, c)
    accepted(sm, event(c, c.EventKind.ANALYSIS_STARTED, sm.current_revision, "start"))
    accepted(sm, event(c, c.EventKind.ANALYSIS_COMPLETED, sm.current_revision, "complete", {"outcome": "complete"}))
    accepted(sm, event(c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, sm.current_revision, "question", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
        "copy_ref": "question-copy", "response_option_codes": ["yes"],
    }))
    resolved_at = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
    accepted(sm, event(c, c.EventKind.CUSTOMER_ACTION_RESOLVED, sm.current_revision, "answer", {
        "request_id": "question-request", "action_kind": "diagnostic_question",
        "locale": "und", "status": "answered", "response_option_code": "yes",
    }, at=resolved_at))
    revision_before = sm.current_revision
    early_withdrawal = sm.apply(event(
        c, c.EventKind.CONSENT_WITHDRAWN, revision_before, "withdraw",
        at=datetime(2026, 8, 12, 0, 30, tzinfo=timezone.utc),  # after created_at, before resolved_at
    ))
    assert not early_withdrawal.accepted
    assert early_withdrawal.decision_code == "terminal_event_predates_case", early_withdrawal.decision_code
    assert sm.current_revision == revision_before


def test_actions_issued_before_case_created_are_rejected(modules):
    c, state = modules
    created_at = datetime(2026, 8, 12, 0, 10, tzinfo=timezone.utc)
    before_creation = datetime(2026, 8, 12, 0, 9, tzinfo=timezone.utc)

    question_sm = state.VisualTriageStateMachine()
    accepted(question_sm, event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"}, at=created_at))
    accepted(question_sm, event(c, c.EventKind.CONSENT_REQUESTED, question_sm.current_revision, "request"))
    accepted(question_sm, event(c, c.EventKind.CONSENT_GRANTED, question_sm.current_revision, "grant"))
    accepted(question_sm, event(c, c.EventKind.UPLOAD_STARTED, question_sm.current_revision, "upload", {
        "asset_id": "asset-video", "media_type": "video/mp4", "byte_size": 100,
        "duration_ms": 10_000, "width": 320, "height": 240, "digest": "a" * 64,
    }))
    accepted(question_sm, event(c, c.EventKind.UPLOAD_FINALIZED, question_sm.current_revision, "final", {"asset_id": "asset-video"}))
    accepted(question_sm, event(c, c.EventKind.MEDIA_VALIDATED, question_sm.current_revision, "validate", {
        "asset_id": "asset-video", "validation": "validated",
    }))
    accepted(question_sm, event(c, c.EventKind.ANALYSIS_STARTED, question_sm.current_revision, "start"))
    accepted(question_sm, event(c, c.EventKind.ANALYSIS_COMPLETED, question_sm.current_revision, "complete", {"outcome": "complete"}))
    revision_before = question_sm.current_revision
    early_question = question_sm.apply(event(
        c, c.EventKind.DIAGNOSTIC_QUESTION_ISSUED, revision_before, "question", {
            "request_id": "question-request", "action_kind": "diagnostic_question",
            "budget_bucket": "question", "receipt_ref": "question", "locale": "und",
            "copy_ref": "question-copy", "response_option_codes": ["yes"],
        },
        at=before_creation,
    ))
    assert not early_question.accepted
    assert early_question.decision_code == "action_issued_before_case_created"
    assert question_sm.current_revision == revision_before

    plate_sm = state.VisualTriageStateMachine()
    accepted(plate_sm, event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"}, at=created_at))
    accepted(plate_sm, event(c, c.EventKind.CONSENT_REQUESTED, plate_sm.current_revision, "request"))
    accepted(plate_sm, event(c, c.EventKind.CONSENT_GRANTED, plate_sm.current_revision, "grant"))
    accepted(plate_sm, event(c, c.EventKind.UPLOAD_STARTED, plate_sm.current_revision, "upload", {
        "asset_id": "asset-video", "media_type": "video/mp4", "byte_size": 100,
        "duration_ms": 10_000, "width": 320, "height": 240, "digest": "a" * 64,
    }))
    accepted(plate_sm, event(c, c.EventKind.UPLOAD_FINALIZED, plate_sm.current_revision, "final", {"asset_id": "asset-video"}))
    accepted(plate_sm, event(c, c.EventKind.MEDIA_VALIDATED, plate_sm.current_revision, "validate", {
        "asset_id": "asset-video", "validation": "validated",
    }))
    accepted(plate_sm, event(c, c.EventKind.ANALYSIS_STARTED, plate_sm.current_revision, "start"))
    accepted(plate_sm, event(c, c.EventKind.ANALYSIS_COMPLETED, plate_sm.current_revision, "complete", {"outcome": "complete"}))
    revision_before = plate_sm.current_revision
    early_plate = plate_sm.apply(event(
        c, c.EventKind.MEDIA_ACTION_ISSUED, revision_before, "plate-issue", {
            "request_id": "plate-request", "action_kind": "rating_plate",
            "budget_bucket": "rating_plate", "receipt_ref": "plate-issue", "copy_ref": "plate-copy",
        },
        at=before_creation,
    ))
    assert not early_plate.accepted
    assert early_plate.decision_code == "action_issued_before_case_created"
    assert plate_sm.current_revision == revision_before


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


def test_non_enum_kind_or_source_from_bypassed_validation_never_raises(modules):
    # Pydantic's model_copy(update=...) does not revalidate the updated
    # fields, so a caller can produce a VisualTriageEvent whose kind/
    # source_kind is a raw string rather than the real enum member.
    # assert_integrity() accesses .value on both, which raises AttributeError
    # for a plain str -- apply() promises to never throw, so this must still
    # resolve to a clean rejection instead of an unhandled crash.
    c, state = modules
    for field in ("kind", "source_kind"):
        sm = state.VisualTriageStateMachine()
        original = event(c, c.EventKind.CASE_CREATED, 0, f"create-{field}", {"scenario": "hvac.demo"})
        accepted(sm, original)
        malformed = original.model_copy(update={field: "not-a-real-enum-member"})
        result = sm.apply(malformed)  # must not raise
        assert not result.accepted, field
        assert result.decision_code == "event_integrity_mismatch", (field, result.decision_code)


def test_foreign_enum_kind_with_matching_value_never_raises(modules):
    # A plain (non-str-mixed) Enum member whose .value equals a real
    # EventKind string passes assert_integrity() unchanged (the envelope
    # fingerprint is recomputed from .value alone, so a same-valued foreign
    # member reproduces the identical hash and the check can't tell the
    # difference), but the member itself is not EventKind.CONSENT_REQUESTED
    # -- the handler-map lookup in _dispatch (keyed by real EventKind
    # members) then raises KeyError, a different bypass than the raw-string
    # case above and not covered by that fix. apply() must still not throw.
    import enum

    class ForeignKind(enum.Enum):
        CONSENT_REQUESTED = "consent_requested"

    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"}))
    genuine = event(c, c.EventKind.CONSENT_REQUESTED, 1, "request")
    foreign = genuine.model_copy(update={"kind": ForeignKind.CONSENT_REQUESTED})
    result = sm.apply(foreign)  # must not raise
    assert not result.accepted
    assert result.decision_code == "event_integrity_mismatch"


def test_foreign_enum_kind_with_unrecognized_value_never_raises(modules):
    # A foreign Enum member whose .value doesn't match ANY real EventKind
    # string fails even earlier than the matching-value case above:
    # assert_integrity()'s own _EVENT_PAYLOAD_KEYS[self.kind.value] lookup
    # raises KeyError before the fingerprint is ever checked, inside the
    # first try/except in apply() (around event.assert_integrity()), not
    # the second one around _dispatch. apply() must still not throw.
    import enum

    class ForeignKind(enum.Enum):
        BOGUS = "not_a_real_event_kind"

    c, state = modules
    sm = state.VisualTriageStateMachine()
    original = event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"})
    accepted(sm, original)
    malformed = original.model_copy(update={"kind": ForeignKind.BOGUS})
    result = sm.apply(malformed)  # must not raise
    assert not result.accepted
    assert result.decision_code == "event_integrity_mismatch"


def test_foreign_enum_kind_on_replay_path_is_rejected(modules):
    # Unlike the first-application cases above, a caller can copy an
    # ALREADY-COMMITTED genuine event (same event_id, so the ledger already
    # has a Receipt for it) with kind replaced by a same-valued foreign
    # member. model_copy() doesn't touch the fingerprint, and the fingerprint
    # was computed from kind.value alone, so it still matches what's in the
    # ledger -- pre-fix, apply()'s replay branch only compares fingerprints
    # and never re-checks that kind is a genuine EventKind, so this would be
    # reported as an accepted replay instead of rejected.
    import enum

    class ForeignKind(enum.Enum):
        CONSENT_REQUESTED = "consent_requested"

    c, state = modules
    sm = state.VisualTriageStateMachine()
    accepted(sm, event(c, c.EventKind.CASE_CREATED, 0, "create", {"scenario": "hvac.demo"}))
    genuine = event(c, c.EventKind.CONSENT_REQUESTED, sm.current_revision, "request")
    accepted(sm, genuine)
    revision_before = sm.current_revision
    foreign_replay = genuine.model_copy(update={"kind": ForeignKind.CONSENT_REQUESTED})
    result = sm.apply(foreign_replay)  # must not raise
    assert not result.accepted
    assert not result.replayed
    assert result.decision_code == "event_integrity_mismatch"
    assert sm.current_revision == revision_before


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
