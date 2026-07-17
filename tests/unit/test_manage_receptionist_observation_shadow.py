"""Dry-run-first operator authorization for the staging observation shadow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.manage_receptionist_observation_shadow as operator
from scripts.manage_receptionist_observation_shadow import (
    AuthorizationError,
    STAGING_HEALTH_HOST,
    STAGING_PROJECT_ID,
    apply_disable,
    apply_enablement,
    build_enablement_plan,
    verify_staging_health,
)


TEST_SHA = "a" * 40
TEST_CALLER = "synthetic-test-caller"
TEST_KEY = "k" * 32
EVIDENCE_TEMPLATE = Path(
    "docs/runbooks/receptionist-observation-shadow-evidence.template.json"
)


class _Snapshot:
    exists = True


class _Document:
    def __init__(self):
        self.updates = []

    def get(self):
        return _Snapshot()

    def update(self, fields):
        self.updates.append(fields)


class _Collection:
    def __init__(self, document):
        self._document = document

    def document(self, _contractor_id):
        return self._document


class _Firestore:
    def __init__(self):
        self.document = _Document()

    def collection(self, name):
        assert name == "contractors"
        return _Collection(self.document)


def _plan(**changes):
    values = {
        "project": STAGING_PROJECT_ID,
        "health_url": f"https://{STAGING_HEALTH_HOST}/health",
        "expected_sha": TEST_SHA,
        "contractor_id": "synthetic-contractor",
        "caller_identifier": TEST_CALLER,
        "hmac_key": TEST_KEY,
        "ttl_seconds": 600,
        "now": 1_000,
    }
    values.update(changes)
    return build_enablement_plan(**values)


def _enable_argv(*, confirmation: str) -> list[str]:
    return [
        "enable",
        "--project",
        STAGING_PROJECT_ID,
        "--health-url",
        f"https://{STAGING_HEALTH_HOST}/health",
        "--expected-sha",
        TEST_SHA,
        "--contractor-id",
        "synthetic-contractor",
        "--ttl-seconds",
        "600",
        "--confirm",
        confirmation,
    ]


def _fail_on_call(*_args, **_kwargs):
    pytest.fail("operation must not be reached")


def test_plan_contains_exact_runtime_fields_but_summary_is_payload_safe():
    plan = _plan()

    assert plan.fields["receptionist_observation_shadow_enabled"] is True
    assert plan.fields["receptionist_observation_shadow_authorized_sha"] == TEST_SHA
    assert plan.fields["receptionist_observation_shadow_expires_at"] == 1_600
    assert len(plan.fields["receptionist_observation_shadow_caller_digests"]) == 1
    assert plan.fields["receptionist_observation_shadow_authorized_at"] == 1_000

    serialized = json.dumps(plan.redacted_summary())
    assert TEST_CALLER not in serialized
    assert TEST_KEY not in serialized
    assert plan.contractor_id not in serialized
    assert plan.fields["receptionist_observation_shadow_caller_digests"][0] not in serialized
    assert plan.redacted_summary() == {
        "status": "ready",
        "project": STAGING_PROJECT_ID,
        "expected_sha": TEST_SHA,
        "contractor_label": plan.contractor_label,
        "ttl_seconds": 600,
        "caller_digest_count": 1,
        "writes_authorized": False,
    }


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"project": "kevin-491315"}, "staging project"),
        ({"project": "different-staging"}, "staging project"),
        ({"health_url": "http://kevin-api-staging.example/health"}, "HTTPS"),
        ({"health_url": "https://kevin-api.example.run.app/health"}, "staging host"),
        (
            {
                "health_url": (
                    f"https://{STAGING_HEALTH_HOST}.attacker.example/health"
                )
            },
            "staging host",
        ),
        (
            {"health_url": f"https://{STAGING_HEALTH_HOST}:invalid/health"},
            "valid HTTPS",
        ),
        (
            {
                "health_url": (
                    f"https://user:secret@{STAGING_HEALTH_HOST}/health"
                )
            },
            "staging host",
        ),
        ({"expected_sha": "short"}, "40-character"),
        ({"contractor_id": ""}, "contractor"),
        ({"contractor_id": "nested/path"}, "document segment"),
        ({"caller_identifier": ""}, "caller"),
        ({"hmac_key": "short"}, "HMAC"),
        ({"ttl_seconds": 59}, "TTL"),
        ({"ttl_seconds": 3_601}, "TTL"),
    ],
)
def test_plan_rejects_unsafe_or_unbounded_input(changes, message):
    with pytest.raises(AuthorizationError, match=message):
        _plan(**changes)


def test_health_verification_requires_exact_staging_sha():
    plan = _plan()

    verify_staging_health(
        plan,
        {
            "environment": "staging",
            "service": "kevin-api-staging",
            "deploy_sha": TEST_SHA,
        },
    )

    with pytest.raises(AuthorizationError, match="deploy SHA"):
        verify_staging_health(
            plan,
            {
                "environment": "staging",
                "service": "kevin-api-staging",
                "deploy_sha": "b" * 40,
            },
        )

    with pytest.raises(AuthorizationError, match="staging identity"):
        verify_staging_health(
            plan,
            {
                "environment": "production",
                "service": "kevin-api",
                "deploy_sha": TEST_SHA,
            },
        )


def test_apply_enablement_writes_only_plan_fields_after_document_exists():
    firestore = _Firestore()
    plan = _plan()

    apply_enablement(plan, firestore)

    assert firestore.document.updates == [plan.fields]


def test_disable_fails_closed_and_removes_all_sensitive_authorization_fields():
    firestore = _Firestore()
    delete = object()

    summary = apply_disable(
        contractor_id="synthetic-contractor",
        firestore_client=firestore,
        delete_field=delete,
    )

    assert firestore.document.updates == [
        {
            "receptionist_observation_shadow_enabled": False,
            "receptionist_observation_shadow_authorized_sha": delete,
            "receptionist_observation_shadow_expires_at": delete,
            "receptionist_observation_shadow_caller_digests": delete,
            "receptionist_observation_shadow_authorized_at": delete,
        }
    ]
    serialized = json.dumps(summary)
    assert "synthetic-contractor" not in serialized
    assert summary["status"] == "disabled"


def test_evidence_template_is_payload_free_and_nonauthorizing():
    evidence = json.loads(EVIDENCE_TEMPLATE.read_text())

    assert evidence["schema_version"] == 1
    assert evidence["contractor_label"] is None
    assert evidence["caller_digest_count"] == 0
    assert [window["mode"] for window in evidence["windows"]] == [
        "shadow_off",
        "shadow_on",
    ]
    authorization_fields = {
        key: value
        for key, value in evidence.items()
        if key.endswith("_authorized") or key.endswith("_validated")
    }
    assert authorization_fields
    assert set(authorization_fields.values()) == {False}
    serialized = json.dumps(evidence).lower()
    assert "phone" not in serialized
    assert "caller_digest\"" not in serialized
    assert "transcript" not in serialized
    assert "contractor_id" not in serialized


def test_plan_command_has_no_network_or_firestore_side_effects(monkeypatch, capsys):
    monkeypatch.setenv(operator.HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setattr(operator, "_read_caller_identifier", lambda: TEST_CALLER)
    monkeypatch.setattr(operator, "_fetch_health", _fail_on_call)

    exit_code = operator.main(
        ["plan", *_enable_argv(confirmation="unused")[1:-2]]
    )

    assert exit_code == 0
    output = capsys.readouterr()
    assert output.err == ""
    summary = json.loads(output.out)
    assert summary["writes_authorized"] is False
    assert TEST_CALLER not in output.out
    assert TEST_KEY not in output.out
    assert "synthetic-contractor" not in output.out


def test_enable_rejects_confirmation_before_reading_caller_or_calling_cloud(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(operator, "_read_caller_identifier", _fail_on_call)
    monkeypatch.setattr(operator, "_fetch_health", _fail_on_call)

    exit_code = operator.main(_enable_argv(confirmation="not-authorized"))

    assert exit_code == 2
    output = capsys.readouterr()
    assert json.loads(output.err)["status"] == "blocked"


def test_enable_rejects_health_mismatch_before_creating_firestore_client(
    monkeypatch,
    capsys,
):
    from google.cloud import firestore

    monkeypatch.setenv(operator.HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setattr(operator, "_read_caller_identifier", lambda: TEST_CALLER)
    monkeypatch.setattr(
        operator,
        "_fetch_health",
        lambda _url: {
            "environment": "production",
            "service": "kevin-api",
            "deploy_sha": TEST_SHA,
        },
    )
    monkeypatch.setattr(firestore, "Client", _fail_on_call)

    exit_code = operator.main(
        _enable_argv(confirmation=operator.ENABLE_CONFIRMATION)
    )

    assert exit_code == 2
    output = capsys.readouterr()
    assert json.loads(output.err)["status"] == "blocked"


def test_health_fetch_rejects_redirected_response(monkeypatch):
    health_url = f"https://{STAGING_HEALTH_HOST}/health"

    class RedirectedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://attacker.example/health"

        def read(self, _limit):
            pytest.fail("redirected response body must not be trusted")

    monkeypatch.setattr(operator, "urlopen", lambda *_args, **_kwargs: RedirectedResponse())

    with pytest.raises(AuthorizationError, match="redirected"):
        operator._fetch_health(health_url)
