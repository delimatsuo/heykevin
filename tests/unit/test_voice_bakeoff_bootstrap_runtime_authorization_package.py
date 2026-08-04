"""Guards for the non-executable bootstrap/runtime authorization review package."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-bootstrap-runtime-authorization.review.json"
)
_INVENTORY_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-bootstrap-readonly-inventory-2026-07-28.json"
)
_REVALIDATION_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-bootstrap-revalidation-state-2026-07-28.json"
)
_SOURCE_SHA = "2ed8ea7d1d7f338e84ddf08d5a50a714835e1533"
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_RUNTIME_GATES = {
    "sealed_owner_authorization",
    "independent_technical_review",
    "physically_separate_preauth_store",
    "identity_and_credential_broker",
    "durable_trust_and_revocation_store",
    "provider_privacy_and_region_attestations",
    "complete_production_denylist",
    "immutable_custody_and_residue_routing",
    "one_use_runtime_envelope",
}


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_review_package_is_source_pinned_and_digest_bound() -> None:
    package = _load(_PACKAGE_PATH)
    source = package["source_binding"]
    assert isinstance(source, dict)
    assert source["source_sha"] == _SOURCE_SHA
    assert source["tracked_source_clean_at_observation"] is True
    assert source["untracked_handoff_material_excluded"] is True

    artifact_digests = source["source_artifact_digests"]
    assert isinstance(artifact_digests, dict)
    assert artifact_digests
    assert list(artifact_digests) == sorted(artifact_digests)
    for relative_path, expected_digest in artifact_digests.items():
        assert isinstance(relative_path, str)
        assert isinstance(expected_digest, str)
        assert _DIGEST_PATTERN.fullmatch(expected_digest)
        assert _file_digest(_ROOT / relative_path) == expected_digest
    assert source["source_bundle_digest"] == _canonical_digest(artifact_digests)

    inventory_binding = package["read_only_inventory"]
    assert isinstance(inventory_binding, dict)
    assert inventory_binding["canonical_digest"] == _canonical_digest(
        _load(_INVENTORY_PATH)
    )
    assert inventory_binding["revalidation_state_digest"] == _canonical_digest(
        _load(_REVALIDATION_PATH)
    )

    bootstrap = package["bootstrap"]
    assert isinstance(bootstrap, dict)
    assert bootstrap["bootstrap_payload_digest"] == _canonical_digest(
        bootstrap["payload"]
    )


def test_read_only_inventory_records_only_the_two_isolated_projects() -> None:
    inventory = _load(_INVENTORY_PATH)
    assert inventory["status"] == "read_only_observation"
    assert inventory["environment"] == "bakeoff_nonproduction"
    assert inventory["source_sha"] == _SOURCE_SHA
    assert (
        inventory["gcloud_context"]["configured_quota_project_status"]
        == "unapproved_or_out_of_scope"
    )
    assert (
        inventory["gcloud_context"][
            "explicit_isolated_quota_required_for_future_commands"
        ]
        is True
    )

    projects = inventory["projects"]
    assert isinstance(projects, list)
    assert {
        (project["project_id"], project["database"]["database_id"])
        for project in projects
    } == {
        ("hk-voice-bakeoff-0724-iso", "voice-bakeoff-control"),
        ("hk-voice-bakeoff-preauth-iso", "voice-bakeoff-preauth"),
    }
    for project in projects:
        assert project["lifecycle_state"] == "ACTIVE"
        assert project["database"]["database_count"] == 1
        assert project["database"]["location_id"] == "us-central1"
        assert project["database"]["documents_read"] is False
        assert project["database"]["documents_written"] is False
        assert project["iam"]["expected_runtime_identity_present"] is False
        assert project["iam"]["expected_runtime_identity_binding_count"] == 0
        assert project["iam"]["conditioned_binding_count"] == 0
        assert project["iam"]["project_custom_role_count"] == 0
        assert project["iam"]["total_user_managed_key_count"] == 0
        for service in (
            "appengine",
            "cloudbuild",
            "cloudfunctions",
            "cloudresourcemanager",
            "compute",
            "container",
            "iam",
            "iamcredentials",
            "policytroubleshooter",
            "run",
            "secretmanager",
            "sts",
        ):
            assert project["enabled_services"][service] is False

    effects = inventory["effects"]
    assert isinstance(effects, dict)
    assert all(value == 0 for value in effects.values())


def test_bootstrap_package_cannot_authorize_runtime_or_preauth() -> None:
    package = _load(_PACKAGE_PATH)
    assert package["package_status"] == "review_candidate_not_authority"
    assert package["execution_supported"] is False
    assert package["authorization_model"] == "sole_owner"

    bootstrap = package["bootstrap"]
    assert isinstance(bootstrap, dict)
    assert bootstrap["status"] == "blocked_not_owner_signable"
    assert bootstrap["one_use"] is True
    assert bootstrap["execution_supported"] is False
    assert bootstrap["technical_review"]["status"] == "pending"
    assert bootstrap["owner_authorization"]["status"] == "not_materializable"
    assert all(
        value is None
        for key, value in bootstrap["owner_authorization"].items()
        if key != "status"
    )

    payload = bootstrap["payload"]
    assert isinstance(payload, dict)
    assert payload["mutation_allowed_projects"] == [
        "hk-voice-bakeoff-0724-iso"
    ]
    assert payload["preauth_mutation_plan"] == []
    assert payload["runtime_or_document_operations"] == []
    assert (
        payload["preauth_bootstrap_status"]
        == "blocked_pending_reviewed_preauth_adapter_and_least_privilege_role"
    )

    mutations = [
        step
        for step in payload["mutation_plan"]
        if step["mutation"] is True
    ]
    assert (
        sum(step["mutation_units"] for step in mutations)
        == payload["caps"]["forward_mutating_operations"]
    )
    assert {step["project"] for step in mutations} == {
        "hk-voice-bakeoff-0724-iso"
    }
    assert payload["explicit_quota_project_required"] is True
    assert payload["ambient_or_configured_quota_project_forbidden"] is True
    assert payload["control_quota_project"] == "hk-voice-bakeoff-0724-iso"
    api_faults = payload["api_enablement_fault_contract"]
    assert api_faults["enable_one_service_per_operation"] is True
    assert api_faults["record_service_usage_readback_after_each_operation"] is True
    assert api_faults["retry_unknown_mutation"] is False
    assert set(api_faults["allowed_transient_services"]) == {
        "cloudresourcemanager.googleapis.com",
        "iam.googleapis.com",
        "policytroubleshooter.googleapis.com",
    }
    assert {
        "cloudbilling.googleapis.com",
        "cloudkms.googleapis.com",
        "cloudresourcemanager.googleapis.com",
        "firestore.googleapis.com",
        "iam.googleapis.com",
        "policytroubleshooter.googleapis.com",
        "serviceusage.googleapis.com",
    } == set(payload["allowed_control_plane_endpoints"])
    conditional_binding = next(
        step
        for step in payload["mutation_plan"]
        if step["operation"] == "add_conditional_iam_binding"
    )
    assert (
        conditional_binding["condition"]
        == 'resource.type == "firestore.googleapis.com/Database" && '
        'resource.name == '
        '"projects/hk-voice-bakeoff-0724-iso/databases/voice-bakeoff-control"'
    )
    assert payload["caps"]["preauth_mutations"] == 0
    assert payload["caps"]["service_apis_enabled"] == 3
    assert payload["caps"]["service_apis_left_enabled"] == 0
    assert payload["caps"]["runtime_identities_left_enabled"] == 0
    assert payload["caps"]["iam_bindings_left_present"] == 0
    assert payload["caps"]["custom_roles_left_enabled"] == 0
    assert payload["caps"]["user_managed_keys"] == 0
    assert payload["caps"]["firestore_document_reads"] == 0
    assert payload["caps"]["firestore_document_writes"] == 0
    assert payload["caps"]["provider_or_pstn_requests"] == 0
    assert payload["caps"]["workload_invocations"] == 0
    assert payload["caps"]["total_mutating_operations"] == (
        payload["caps"]["forward_mutating_operations"]
        + payload["caps"]["rollback_mutating_operations"]
    )
    matrix = payload["effective_access_matrix"]
    queries = matrix["policy_troubleshooter_queries"]
    assert len(queries) == 10
    assert sum(query["expected"] == "GRANTED" for query in queries) == 4
    assert sum(query["expected"] == "NOT_GRANTED" for query in queries) == 6
    assert {
        query["resource"]
        for query in queries
        if query["expected"] == "GRANTED"
    } == {
        "//firestore.googleapis.com/projects/hk-voice-bakeoff-0724-iso/databases/voice-bakeoff-control"
    }
    assert {
        "iam.serviceAccounts.actAs",
        "iam.serviceAccounts.getAccessToken",
        "run.services.create",
        "secretmanager.versions.access",
    } <= set(matrix["project_and_resource_policy_negative_permissions"])
    materialization = bootstrap["materialization_policy"]
    assert (
        materialization["status"]
        == "specified_verifier_and_durable_consumer_not_implemented"
    )
    assert materialization["fail_closed_if_consumer_unavailable_or_result_unknown"]
    assert materialization["signature_message_fields"] == sorted(
        materialization["signature_message_fields"]
    )
    assert materialization["signature_domain_separator_hex"] == "00"
    assert materialization["envelope_self_digest_algorithm"] == "sha256"
    negative_attestation = materialization[
        "cross_environment_negative_grant_attestation"
    ]
    assert negative_attestation["status"] == (
        "required_not_collected_current_scope_forbids_query"
    )
    assert negative_attestation["digest"] is None
    assert negative_attestation["separate_owner_authorization_required"] is True
    assert "cross_environment_negative_grant_attestation_digest" in (
        materialization["signature_message_fields"]
    )
    assert {
        "canonical_materialized_envelope_schema_and_domain_separated_verifier_implemented_and_reviewed",
        "atomic_durable_approval_and_nonce_consumer_implemented_and_reviewed",
        "bootstrap_executor_exact_action_allowlist_and_dry_run_reviewed",
        "explicit_isolated_quota_project_bound_for_every_command",
        "separately_authorized_fresh_digest_bound_production_and_staging_negative_grant_attestation",
    } <= set(bootstrap["preconditions"])

    forbidden = set(payload["forbidden_actions"])
    assert {
        "create_or_invoke_workload",
        "create_user_managed_key",
        "create_preauth_identity_role_or_binding",
        "read_or_write_firestore_documents",
        "query_production_or_staging",
        "modify_production_or_staging",
        "contact_provider_or_pstn",
        "execute_task_4_8",
    } <= forbidden

    runtime = package["runtime"]
    assert isinstance(runtime, dict)
    assert runtime["execution_supported"] is False
    assert runtime["provider_or_pstn_supported"] is False
    assert runtime["owner_authorization"] is None
    assert runtime["technical_review"] is None
    assert set(runtime["blocking_gate_status"]) == _RUNTIME_GATES
    assert all(
        status not in {"complete", "satisfied", "authorized"}
        for status in runtime["blocking_gate_status"].values()
    )


def test_bootstrap_success_and_rollback_leave_no_dormant_binding() -> None:
    package = _load(_PACKAGE_PATH)
    payload = package["bootstrap"]["payload"]
    operations = [step["operation"] for step in payload["mutation_plan"]]
    assert operations.index("remove_exact_conditional_iam_binding") > operations.index(
        "add_conditional_iam_binding"
    )
    assert operations.index("disable_service_account") > operations.index(
        "remove_exact_conditional_iam_binding"
    )
    assert operations.index("disable_project_custom_role") > operations.index(
        "remove_exact_conditional_iam_binding"
    )
    assert operations.index("enable_required_control_plane_apis") < operations.index(
        "full_identity_policy_and_role_revalidation"
    )
    assert operations.index(
        "full_identity_policy_and_role_revalidation"
    ) < operations.index("create_service_account")
    assert operations.index(
        "pre_api_disable_iam_role_and_identity_readback"
    ) < operations.index("disable_bootstrap_control_plane_apis")
    assert operations[-1] == "post_api_disable_service_usage_readback"

    revalidation = _load(_REVALIDATION_PATH)
    staged = revalidation["staged_revalidation_contract"]
    assert staged["post_enable_quota_project"] == "hk-voice-bakeoff-0724-iso"
    assert set(staged["post_enable_read_targets"]) == {
        "hk-voice-bakeoff-0724-iso",
        "hk-voice-bakeoff-preauth-iso",
    }
    assert staged["mismatch_action"] == (
        "disable_only_the_three_newly_enabled_control_project_apis_then_abort_and_record_residue"
    )
    terminal = revalidation["required_terminal_state"]
    control_terminal = terminal["control_iam_readback_before_api_disable"]
    assert control_terminal["service_account_count"] == 2
    assert control_terminal["enabled_service_account_count"] == 1
    assert control_terminal["unexpected_new_service_account_or_service_identity_count"] == 0
    assert (
        control_terminal["project_policy_digest"]
        == "e86154e9c77313b6536d8c67dcb8b5788963a5a86e96a954951a8f2951b896c6"
    )
    assert control_terminal["new_project_or_resource_policy_binding_count"] == 0
    assert control_terminal["new_service_agent_binding_count"] == 0
    assert control_terminal["adapter_service_account_impersonation_binding_count"] == 0
    assert control_terminal["project_custom_role_count"] == 1
    assert control_terminal["exact_new_custom_role_stage"] == "DISABLED"
    assert control_terminal["unexpected_new_custom_role_count"] == 0

    rollback = payload["rollback"]
    rollback_steps = [
        step["operation"] for step in rollback["ordered_steps"]
    ]
    assert sum(
        step["mutation_units"] for step in rollback["ordered_steps"]
    ) == payload["caps"]["rollback_mutating_operations"]
    assert rollback_steps.index(
        "remove_exact_conditional_binding_if_created"
    ) < (
        rollback_steps.index("disable_exact_runtime_identity_if_created")
    )
    assert rollback_steps.index("disable_exact_runtime_identity_if_created") < (
        rollback_steps.index("disable_exact_custom_role_if_created")
    )
    assert "disable_exact_bootstrap_control_plane_apis_if_enabled" in rollback_steps
    assert rollback["delete_resources"] is False
    assert rollback["retention_lock"] is False
