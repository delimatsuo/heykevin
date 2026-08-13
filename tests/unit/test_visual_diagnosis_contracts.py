"""A1 contract tests with an inline pre-import isolation guard."""

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
    "ANTHROPIC_API_KEY",
    "ADMIN_API_TOKEN",
    "APNS_KEY_CONTENT",
    "APNS_KEY_ID",
    "APNS_TEAM_ID",
    "API_BEARER_TOKEN",
    "APPSTORE_ISSUER_ID",
    "APPSTORE_KEY_ID",
    "APPSTORE_PRIVATE_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "FISH_AUDIO_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CALENDAR_CLIENT_ID",
    "GOOGLE_CALENDAR_CLIENT_SECRET",
    "JOBBER_CLIENT_ID",
    "JOBBER_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "RECEPTIONIST_OBSERVATION_SHADOW_CALLER_HMAC_KEY",
    "TRANSCRIPT_ENCRYPTION_KEY",
    "TWILIO_ACCOUNT_SID",
    "PRODUCTION_TWILIO_ACCOUNT_SID",
    "TWILIO_API_KEY_SECRET",
    "TWILIO_API_KEY_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_TWIML_APP_SID",
    "VAPI_API_KEY",
    "VAPI_PUBLIC_KEY",
    "VAPI_WEBHOOK_SECRET",
    "VCARD_HMAC_SECRET",
    "APNS_BUNDLE_ID",
    "APPSTORE_BUNDLE_ID",
    "CLOUD_RUN_URL",
    "DIAL_IN_NUMBER",
    "DIAL_IN_NUMBERS",
    "FIREBASE_DATABASE_URL",
    "FIRESTORE_PROJECT_ID",
    "GCLOUD_PROJECT",
    "GCP_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "TELEGRAM_CHAT_ID",
    "TWILIO_PHONE_NUMBER",
    "USER_NAME",
    "USER_PHONE",
    "VAPI_PHONE_NUMBER_ID",
}
FORBIDDEN_IMPORT_ROOTS = {
    "app.config",
    "app.main",
    "app.api",
    "app.webhooks",
    "app.db",
    "app.services.gemini_pipeline",
    "app.services.post_call",
    "app.services.sms",
    "google",
    "twilio",
    "socket",
    "subprocess",
    "os",
    "pathlib",
}
ALLOWED_CONTRACT_IMPORTS = {
    "__future__",
    "collections",
    "dataclasses",
    "datetime",
    "enum",
    "hashlib",
    "json",
    "re",
    "typing",
    "uuid",
    "pydantic",
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


def _assert_clean_tree_and_reviewed_ancestry() -> None:
    """Prove the checkout is untampered and descends from the reviewed
    baseline -- works whether the candidate files are still uncommitted
    (local dev, pre-PR) or already committed on a feature branch (CI, PR
    review). Ancestry is a monotonic check (never un-true once satisfied),
    so it stays valid forever after this branch merges, unlike a diff
    comparison against origin/main: these test files are collected on every
    future pytest run once merged, including unrelated PRs, whose
    origin/main..HEAD diff would never be a subset of this feature's own
    file allowlist -- a diff-scope assertion here would break CI on every
    future PR that touches anything outside these files. EXPECTED_PATHS
    stays as a record of what this feature's own review covered, not as a
    live per-run constraint. Baseline provenance is an ancestor check rather
    than an exact HEAD pin, because CI checks out an ephemeral merge commit
    for pull_request events, not the branch tip itself.
    """

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
        # importing configuration or filesystem APIs (see IMPORT_CLOSURE /
        # FORBIDDEN_IMPORT_ROOTS), so this is a workstation opt-in check like
        # the others, not a permanent collection gate.
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


def _assert_network_denied() -> None:
    try:
        with socket.create_connection(("1.1.1.1", 443), 0.5):
            raise AssertionError("candidate tests are not running under denied egress")
    except PermissionError as error:
        assert error.errno == 1
    except OSError as error:
        raise AssertionError("egress failure was not the sandbox denial") from error


def _assert_candidate_isolated(*, require_environment: bool = True) -> None:
    _assert_clean_tree_and_reviewed_ancestry()
    if _local_isolation_check_enabled():
        # Denied-egress and this exact dev machine's pinned toolchain are
        # local-sandbox properties (sandbox-exec, a specific Homebrew Python
        # and venv-local ruff) that a hosted CI runner cannot reproduce and
        # was never meant to: CI's isolation instead comes from running an
        # ordinary, unprivileged test process with no provider credentials
        # present (still enforced below, unconditionally).
        _assert_network_denied()
        assert os.environ.get("VISUAL_DIAG_EGRESS_DENIED") == "sandbox-exec"
    if require_environment:
        # Check the pristine pre-collection snapshot (see conftest.py), not
        # live os.environ: several sibling test files set dummy provider-
        # credential env vars at their own module level, which would
        # otherwise look identical to a real secret leak to this scan.
        pristine = os.environ.get("_VISUAL_DIAG_PRISTINE_ENVIRON_NAMES", "").split("\x1f")
        env_names = {name.casefold() for name in pristine if name}
        forbidden = {name.casefold() for name in FORBIDDEN_ENV_NAMES}
        assert env_names.isdisjoint(forbidden)
        assert not any(
            name.startswith("bakeoff_nonprod_credential__")
            or name.startswith("bakeoff_nonprod_account_region__")
            for name in env_names
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

    candidate_paths = {
        ROOT / "app/services/visual_diagnosis_contracts.py",
        ROOT / "app/services/visual_diagnosis_state.py",
    }
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
    for path in candidate_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".", 1)[0]
                if path.name == "visual_diagnosis_state.py" and name == "app.services.visual_diagnosis_contracts":
                    continue
                assert root in ALLOWED_CONTRACT_IMPORTS, (path.name, name)
                assert not any(
                    name == forbidden or name.startswith(f"{forbidden}.")
                    for forbidden in FORBIDDEN_IMPORT_ROOTS
                )
        forbidden_calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert not forbidden_calls.intersection({"eval", "exec", "open", "__import__"})

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
    for raw_path in tracked:
        if not raw_path:
            continue
        path = ROOT / raw_path.decode("utf-8")
        if path.name in candidate_names:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "visual_diagnosis_contracts" not in text
        assert "visual_diagnosis_state" not in text


_assert_candidate_isolated()


@pytest.fixture(scope="module")
def contracts_module():
    """Run the guard before the controlled candidate import."""

    _assert_candidate_isolated(require_environment=False)
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
            yield importlib.import_module("app.services.visual_diagnosis_contracts")
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


def _event(module, *, kind, payload=None, revision=0, event_id="event-1"):
    return module.VisualTriageEvent.build(
        case_id="case-1",
        contractor_id="contractor-1",
        event_id=event_id,
        kind=kind,
        payload=payload or {},
        expected_revision=revision,
        source_kind=module.EventSource.SYNTHETIC,
        event_time=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )


def test_event_digest_and_semantic_fingerprint_are_canonical(contracts_module):
    c = contracts_module
    event = _event(c, kind=c.EventKind.CASE_CREATED, payload={"scenario": "hvac.demo"})
    assert len(event.canonical_payload_digest) == 64
    assert len(event.semantic_envelope_fingerprint) == 64
    with pytest.raises(ValueError):
        c.VisualTriageEvent(
            **event.model_dump(exclude={"canonical_payload_digest"}),
            canonical_payload_digest="0" * 64,
        )


def test_event_rejects_sensitive_or_non_structural_payload(contracts_module):
    c = contracts_module
    with pytest.raises(ValueError):
        _event(c, kind=c.EventKind.CASE_CREATED, payload={"phone": "opaque"})
    with pytest.raises(ValueError):
        _event(c, kind=c.EventKind.CASE_CREATED, payload={"nested": {"serial": "x"}})
    with pytest.raises(ValueError):
        _event(c, kind=c.EventKind.CASE_CREATED, payload={"note": "raw transcript from phone"})


def test_validation_errors_and_model_strings_are_payload_safe(contracts_module):
    c = contracts_module
    with pytest.raises(ValueError) as error:
        c.CallFacts(
            scenario="hvac",
            symptom_code="not_cooling",
            source_ref="raw transcript text",
            provenance_code="call_fact",
        )
    assert "raw transcript text" not in str(error.value)
    with pytest.raises(ValueError) as error:
        c.ArtifactReference(artifact_id="raw phone 555", readiness=c.ArtifactReadiness.READY)
    assert "raw phone 555" not in str(error.value)
    event = _event(c, kind=c.EventKind.CASE_CREATED, payload={"scenario": "hvac.demo"})
    assert "hvac.demo" not in repr(event)
    assert "hvac.demo" not in str(event)


def test_model_validate_entry_points_are_also_payload_safe(contracts_module):
    # __init__ (the plain keyword-argument constructor) sanitizes
    # ValidationError, but model_validate()/model_validate_json() are
    # separate Pydantic-public entry points that don't route through
    # __init__ -- each needs its own sanitization or a caller using one of
    # them gets a raw pydantic ValidationError with the offending input
    # value embedded verbatim in its message.
    c = contracts_module
    canary = "raw phone 555"
    with pytest.raises(c.StructuralValidationError) as error:
        c.ArtifactReference.model_validate({"artifact_id": canary, "readiness": "ready"})
    assert canary not in str(error.value)
    with pytest.raises(c.StructuralValidationError) as error:
        c.ArtifactReference.model_validate_json(f'{{"artifact_id": "{canary}", "readiness": "ready"}}')
    assert canary not in str(error.value)


def test_privacy_canary_categories_are_rejected_without_sink_leakage(contracts_module):
    c = contracts_module
    canaries = (
        "raw transcript canary",
        "phone 5551234567 canary",
        "address 123 Main Street canary",
        "token secret canary",
        "serial SN123456 canary",
        "provider url canary",
        "https://provider.example/id url canary",
        "diagnosis not_cooling canary",
        "answer prompt canary",
        "price 99 canary",
        "repair part canary",
    )
    for canary in canaries:
        with pytest.raises(ValueError) as error:
            _event(c, kind=c.EventKind.CASE_CREATED, payload={"scenario": canary})
        assert canary not in str(error.value)
        with pytest.raises(ValueError) as error:
            c.CallFacts(
                scenario="hvac",
                symptom_code="not_cooling",
                source_ref=canary,
                provenance_code="call_fact",
            )
        assert canary not in str(error.value)


def test_unlabeled_structural_canaries_pass_structural_validation_without_leaking_through_exceptions(contracts_module):
    """Sibling to the prose-canary test above: these values contain no
    forbidden term, so -- unlike the prose canaries, rejected above by their
    own label text -- they pass structural validation and become live data.
    (State-machine-level sinks for these same shapes -- projections, pending
    action refs -- are covered in test_visual_diagnosis_state.py.)
    """
    c = contracts_module
    canaries = {
        "phone_shaped": "5555550142",  # reserved 555-01xx exchange, digits only
        "serial_shaped": "XJ48-77Q2-KTN9",
        "token_shaped": "a9f3c7e1b2d84660",
        "ref_shaped": "ref_8f21ac",
        "url_shaped": "https://svc.example.invalid/x/8f21ac93",
    }
    for label, value in canaries.items():
        lowered = value.casefold()
        assert not any(term in lowered for term in c._FORBIDDEN_PAYLOAD_TERMS), (label, value)

    # 1. Recursive nested acceptance: list -> dict -> list -> value, well
    #    within the structural bounds (depth <= 4, list <= 16, dict <= 32).
    for label, value in canaries.items():
        nested = [{"leaf": [value]}]
        c._validate_structural_value(nested)  # must not raise

    # 2. A canary that is structurally accepted (no forbidden term) but is
    #    ultimately rejected for an unrelated reason -- exceeding the closed
    #    per-kind payload-key schema -- must not leak through the exception.
    url_canary = canaries["url_shaped"]
    with pytest.raises(ValueError) as error:
        _event(
            c,
            kind=c.EventKind.CASE_CREATED,
            payload={"scenario": "hvac.demo", "unexpected_key": url_canary},
            event_id="leak-check",
        )
    assert url_canary not in str(error.value)


def test_event_integrity_recheck_detects_nested_mutation(contracts_module):
    c = contracts_module
    event = _event(c, kind=c.EventKind.CASE_CREATED, payload={"scenario": "hvac.demo"})
    event.payload["scenario"] = "hvac.other"
    with pytest.raises(ValueError):
        event.assert_integrity()


def test_closed_models_reject_unknown_fields_and_raw_action_content(contracts_module):
    c = contracts_module
    with pytest.raises(ValueError):
        c.PendingCustomerAction(
            request_id="request-1",
            kind=c.ActionKind.DIAGNOSTIC_QUESTION,
            budget_bucket="question",
            status=c.ActionStatus.ISSUED,
            issued_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
            rendered_prose="not allowed",
        )
    action = c.PendingCustomerAction(
        request_id="request-1",
        kind=c.ActionKind.DIAGNOSTIC_QUESTION,
        budget_bucket="question",
        status=c.ActionStatus.ISSUED,
        issued_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        response_option_codes=("yes", "no"),
    )
    assert action.model_dump().get("rendered_prose") is None
    assert c.PendingCustomerAction(
        request_id="request-2",
        kind=c.ActionKind.DIAGNOSTIC_QUESTION,
        budget_bucket="question",
        status=c.ActionStatus.ISSUED,
        issued_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        locale="en-US",
    ).locale == "en-US"
    with pytest.raises(ValueError):
        c.PendingCustomerAction(
            request_id="request-3",
            kind=c.ActionKind.DIAGNOSTIC_QUESTION,
            budget_bucket="question",
            status=c.ActionStatus.ISSUED,
            issued_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
            locale="en-Lat",
        )


def test_structural_receipt_reference_has_no_external_payload_or_policy_fields(contracts_module):
    c = contracts_module
    reference = c.StructuralReceiptReference(
        event_id="event-1",
        semantic_envelope_fingerprint="a" * 64,
        status_code="accepted",
        resulting_revision=1,
    )
    assert set(reference.model_dump()) == {
        "event_id",
        "semantic_envelope_fingerprint",
        "status_code",
        "resulting_revision",
    }


def test_call_facts_are_bounded_and_opaque(contracts_module):
    c = contracts_module
    facts = c.CallFacts(
        scenario="hvac",
        symptom_code="not_cooling",
        source_ref="call-ref-1",
        provenance_code="call_fact",
        unknown_fields=("equipment_identity",),
    )
    assert facts.source_ref == "call-ref-1"
    with pytest.raises(ValueError):
        c.CallFacts(
            scenario="hvac",
            symptom_code="not_cooling",
            source_ref="raw transcript text",
            provenance_code="call_fact",
        )


def test_a1_does_not_define_hypothesis_or_delivery_eligibility(contracts_module):
    c = contracts_module
    assert not hasattr(c, "DiagnosticHypothesis")
    source = (ROOT / "app/services/visual_diagnosis_contracts.py").read_text(encoding="utf-8")
    assert "delivery_eligibility" not in source


def test_projection_codes_are_closed_and_bounded(contracts_module):
    c = contracts_module
    base = {
        "role": c.ProjectionRole.INTERNAL,
        "case_status": c.CaseStatus.ACTIVE,
        "consent_status": c.ConsentStatus.GRANTED,
        "media_status": c.MediaStatus.VALIDATED,
        "analysis_status": c.AnalysisStatus.READY,
        "contractor_packet_status": c.PacketStatus.NOT_READY,
        "customer_delivery_status": c.DeliveryStatus.NOT_EVALUATED,
        "deletion_status": c.DeletionStatus.NOT_REQUESTED,
    }
    with pytest.raises(ValueError):
        c.Projection(**base, reason_code="phone 555 raw prose")
    with pytest.raises(ValueError):
        c.Projection(**base, historical_decision_code="phone_555")
    with pytest.raises(ValueError):
        c.Projection(**base, next_action="unbounded arbitrary action")
