"""Guards for the blocked cross-environment IAM attestation package."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PATH = (
    _ROOT
    / "docs/security/"
    "voice-bakeoff-cross-environment-iam-attestation-authorization.review.json"
)
_PACKAGE_MARKDOWN_PATH = (
    _ROOT
    / "docs/security/"
    "voice-bakeoff-cross-environment-iam-attestation-authorization-package.md"
)
_BOOTSTRAP_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-bootstrap-runtime-authorization.review.json"
)
_REVALIDATION_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-bootstrap-revalidation-state-2026-07-28.json"
)
_SOURCE_SHA = "2ed8ea7d1d7f338e84ddf08d5a50a714835e1533"
_BOOTSTRAP_PAYLOAD_DIGEST = (
    "3323f05b3384f02ac87f111935304a6e0224720e1beab46fc91841a69b8caefb"
)
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_ALLOWED_ENDPOINTS = [
    "cloudasset.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "policytroubleshooter.googleapis.com",
    "serviceusage.googleapis.com",
]
_ALLOWED_READ_METHODS = [
    "cloudasset.assets.searchAllIamPolicies",
    "cloudasset.assets.searchAllResources",
    "cloudresourcemanager.projects.get",
    "cloudresourcemanager.folders.getIamPolicy",
    "cloudresourcemanager.organizations.getIamPolicy",
    "cloudresourcemanager.projects.getAncestry",
    "cloudresourcemanager.projects.getIamPolicy",
    "iam.organizations.roles.get",
    "iam.policies.get",
    "iam.policies.list",
    "iam.projects.roles.get",
    "iam.projects.serviceAccounts.getIamPolicy",
    "iam.projects.serviceAccounts.list",
    "iam.roles.get",
    "policytroubleshooter.iam.troubleshoot",
    "serviceusage.services.get",
]


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


def _review_contract_digest(package: dict[str, object]) -> str:
    contract = json.loads(json.dumps(package))
    del contract["review_contract_binding"]["digest"]
    return _canonical_digest(contract)


def _template_methods(templates: dict[str, object]) -> set[str]:
    methods: set[str] = set()
    for template in templates.values():
        if not isinstance(template, dict):
            continue
        for key in ("method", "list_method", "get_method"):
            value = template.get(key)
            if isinstance(value, str):
                methods.add(value)
        values = template.get("methods")
        if isinstance(values, list):
            assert all(isinstance(value, str) for value in values)
            methods.update(values)
    return methods


def test_attestation_package_is_exact_source_and_bootstrap_bound() -> None:
    package = _load(_PACKAGE_PATH)
    source = package["source_binding"]
    assert source["source_sha"] == _SOURCE_SHA
    assert source["tracked_source_clean_at_observation"] is True
    assert source["untracked_handoff_material_excluded"] is True
    assert source["bootstrap_payload_digest"] == _BOOTSTRAP_PAYLOAD_DIGEST
    assert source["bootstrap_package_file_digest"] == _file_digest(_BOOTSTRAP_PATH)
    assert source["bootstrap_revalidation_file_digest"] == _file_digest(
        _REVALIDATION_PATH
    )
    for path, expected_digest in source["target_inventory_source_digests"].items():
        assert _DIGEST_PATTERN.fullmatch(expected_digest)
        assert _file_digest(_ROOT / path) == expected_digest

    attestation = package["attestation"]
    assert attestation["attestation_payload_digest"] == _canonical_digest(
        attestation["payload"]
    )
    assert attestation["payload"]["source_sha"] == _SOURCE_SHA
    assert (
        attestation["payload"]["bootstrap_payload_digest"]
        == _BOOTSTRAP_PAYLOAD_DIGEST
    )
    binding = package["review_contract_binding"]
    assert binding["scope"] == (
        "entire_document_excluding_only_review_contract_binding.digest"
    )
    assert binding["digest"] == _review_contract_digest(package)


def test_markdown_summary_matches_the_canonical_json_digests() -> None:
    package = _load(_PACKAGE_PATH)
    markdown = _PACKAGE_MARKDOWN_PATH.read_text(encoding="utf-8")
    attestation_digest = package["attestation"]["attestation_payload_digest"]
    contract_digest = package["review_contract_binding"]["digest"]

    for value in (
        _SOURCE_SHA,
        _BOOTSTRAP_PAYLOAD_DIGEST,
        attestation_digest,
        contract_digest,
    ):
        assert markdown.count(f"`{value}`") == 1
    assert "**Status:** preparation authorized; execution not authorized." in markdown
    assert re.search(
        r"blocked,\s+non-owner-signable review candidate",
        markdown,
    )
    assert "does not authorize a production or staging query" in markdown


def test_current_authority_is_preparation_only_and_non_executable() -> None:
    package = _load(_PACKAGE_PATH)
    assert (
        package["package_status"]
        == "preparation_authorized_execution_not_authorized"
    )
    assert package["execution_supported"] is False
    boundary = package["authority_boundary"]
    assert boundary["preparation_authorized"] is True
    assert boundary["connected_read_execution_authorized"] is False
    assert boundary["owner_signature_recorded"] is False
    assert boundary["iam_or_resource_mutation_authorized"] is False
    assert boundary["credential_creation_or_impersonation_authorized"] is False
    assert boundary["task_4_8_authorized"] is False

    attestation = package["attestation"]
    assert attestation["status"] == "blocked_not_owner_signable"
    assert attestation["one_use"] is True
    assert attestation["read_only"] is True
    assert attestation["execution_supported"] is False
    assert attestation["technical_review"]["status"] == "pending"
    assert attestation["owner_authorization"]["status"] == "not_materializable"
    assert all(
        value is None
        for key, value in attestation["owner_authorization"].items()
        if key != "status"
    )
    assert package["downstream_authority"] == {
        "bootstrap_authorized": False,
        "runtime_authorized": False,
        "task_4_8_authorized": False,
        "attestation_pass_is_evidence_only": True,
        "authenticated_attestation_receipt_digest_required": True,
        "fresh_bootstrap_package_and_owner_signature_required_after_attestation": True,
    }


def test_scope_is_exact_read_only_and_fails_closed() -> None:
    package = _load(_PACKAGE_PATH)
    payload = package["attestation"]["payload"]
    inventory = payload["target_inventory"]
    assert inventory["status"] == "source_derived_not_current_confirmed"
    assert inventory["owner_confirmed_complete_target_inventory_digest"] is None
    assert inventory["project_discovery_authorized"] is False
    assert {
        project["project_id"] for project in inventory["projects"]
    } == {
        "kevin-491315",
        "kevin-staging-491315",
    }
    assert all(
        project["quota_project_if_later_authorized"] == project["project_id"]
        for project in inventory["projects"]
    )
    principal_sets = payload["future_principal_set_inventory"]
    assert principal_sets["status"] == "required_not_materialized"
    assert principal_sets["principal_container_project_id"] == (
        "hk-voice-bakeoff-0724-iso"
    )
    assert principal_sets["principal_container_project_number"] is None
    assert principal_sets["principal_container_ancestor_resource_numbers"] == []
    assert principal_sets["owner_confirmed_inventory_digest"] is None
    assert (
        principal_sets["materialization_action_if_any_placeholder_or_unknown"]
        == "blocked"
    )
    resource_inventory = payload["iam_bearing_resource_inventory"]
    assert resource_inventory["status"] == "required_not_materialized"
    assert resource_inventory["owner_confirmed_inventory_digest"] is None
    assert (
        resource_inventory["owner_confirmed_asset_type_coverage_matrix_digest"]
        is None
    )
    broad_manifest = payload["broad_binding_exception_manifest"]
    assert broad_manifest["status"] == "source_derived_not_owner_confirmed"
    assert broad_manifest["owner_confirmed_manifest_digest"] is None
    assert broad_manifest["all_other_broad_bindings_action"] == "fail"
    assert broad_manifest["all_authenticated_users_exception_count"] == 0

    assert payload["allowed_endpoints"] == _ALLOWED_ENDPOINTS
    assert payload["allowed_read_methods"] == _ALLOWED_READ_METHODS
    assert all(step["mutation"] is False for step in payload["operation_plan"])
    api_contract = payload["api_contract"]
    assert api_contract["service_enablement_forbidden"] is True
    assert api_contract["ambient_or_configured_quota_project_forbidden"] is True
    assert api_contract["credential_export_forbidden"] is True
    assert api_contract["service_account_impersonation_forbidden"] is True
    assert api_contract["application_default_credentials_discovery_forbidden"] is True
    effects = payload["expected_observable_control_plane_effects"]
    assert effects["resource_state_mutation"] is False
    assert effects["cloud_audit_log_entries"] is True
    assert effects["quota_accounting_records"] is True
    assert effects["owner_acceptance_required_before_materialization"] is True

    search = payload["policy_search_contract"]
    assert search["exact_principal_binding_count_required"] == 0
    assert search["exact_principal_impersonation_binding_count_required"] == 0
    assert search["encompassing_principal_set_binding_count_required"] == 0
    assert (
        search["encompassing_principal_set_impersonation_binding_count_required"]
        == 0
    )
    assert search["exact_principal_deny_exception_count_required"] == 0
    assert (
        search["encompassing_principal_set_deny_exception_count_required"]
        == 0
    )
    assert (
        search["listed_deny_policy_count_must_equal_fully_fetched_deny_policy_count"]
        is True
    )
    assert search["unexpected_broad_binding_count_required"] == 0
    assert (
        search[
            "every_broad_binding_must_be_a_subset_of_owner_confirmed_exception_manifest"
        ]
        is True
    )
    assert search["all_pages_required"] is True
    assert search["role_permission_resolution_required"] is True
    assert search["encompassing_principal_set_post_filter_required"] is True
    assert search["deny_rule_principal_and_exception_post_filter_required"] is True
    assert search["iam_bearing_resource_inventory_required"] is True
    assert search["asset_type_coverage_matrix_required"] is True
    assert search["partial_visibility_or_omitted_policy_action"] == (
        "incomplete_and_abort"
    )
    assert "cloudresourcemanager.projects.getAncestry" in payload[
        "allowed_read_methods"
    ]
    assert "iam.policies.list" in payload["allowed_read_methods"]
    assert "iam.policies.get" in payload["allowed_read_methods"]
    assert "cloudasset.assets.searchAllResources" in payload["allowed_read_methods"]
    deny_read = next(
        step for step in payload["operation_plan"] if step["step"] == 5
    )
    assert "get_every_deny_policy" in deny_read["operation"]
    request_templates = payload["read_request_templates"]
    assert _template_methods(request_templates) == set(_ALLOWED_READ_METHODS)
    identity = request_templates["project_identity"]
    assert identity == {
        "method": "cloudresourcemanager.projects.get",
        "http_method": "GET",
        "endpoint": "cloudresourcemanager.googleapis.com",
        "path": "/v1/projects/{EXACT_TARGET_PROJECT_ID}",
        "path_parameter_source": "target_inventory.projects[].project_id",
        "request_body": None,
        "body_requirement": "absent",
        "quota_project_header": {
            "x-goog-user-project": "{EXACT_TARGET_PROJECT_ID}"
        },
        "response_projection": [
            "projectId",
            "projectNumber",
            "lifecycleState",
            "parent",
        ],
        "response_requirement": (
            "project_id_exact_match_project_number_present_"
            "lifecycle_active_parent_preserved"
        ),
    }
    ancestry = request_templates["project_ancestry"]
    assert ancestry == {
        "method": "cloudresourcemanager.projects.getAncestry",
        "http_method": "POST",
        "endpoint": "cloudresourcemanager.googleapis.com",
        "path": "/v1/projects/{EXACT_TARGET_PROJECT_ID}:getAncestry",
        "path_parameter_source": "target_inventory.projects[].project_id",
        "request_body": None,
        "body_requirement": "absent_zero_length",
        "quota_project_header": {
            "x-goog-user-project": "{EXACT_TARGET_PROJECT_ID}"
        },
        "response_projection": [
            "ancestor[].resourceId.type",
            "ancestor[].resourceId.id",
        ],
        "response_requirement": (
            "complete_bottom_to_top_chain_beginning_with_exact_project_"
            "and_within_ancestor_cap"
        ),
    }
    service_usage = request_templates["service_usage_state"]
    assert service_usage == {
        "method": "serviceusage.services.get",
        "http_method": "GET",
        "endpoint": "serviceusage.googleapis.com",
        "path": (
            "/v1/projects/{EXACT_TARGET_PROJECT_NUMBER}/services/"
            "{EACH_REQUIRED_SERVICE_NAME}"
        ),
        "project_number_source": "project_identity.response.projectNumber",
        "required_service_names": _ALLOWED_ENDPOINTS,
        "request_body": None,
        "body_requirement": "absent",
        "quota_project_header": {
            "x-goog-user-project": "{EXACT_TARGET_PROJECT_ID}"
        },
        "response_projection": ["name", "parent", "state"],
        "response_requirement": (
            "exact_project_number_and_service_name_match_and_state_enabled"
        ),
    }
    assert (
        request_templates["resource_manager_allow_policy"]["request_body"]["options"][
            "requestedPolicyVersion"
        ]
        == 3
    )
    assert (
        request_templates["service_account_allow_policy"]["request_body"]["options"][
            "requestedPolicyVersion"
        ]
        == 3
    )
    assert request_templates["deny_policy_list_and_get"]["get_method"] == (
        "iam.policies.get"
    )
    assert payload["operation_plan"][2]["operation"] == (
        "read_exact_project_identity_lifecycle_and_ancestor_chain"
    )
    assert payload["operation_plan"][3]["operation"] == (
        "verify_required_api_state_without_enabling_services"
    )

    evidence = payload["evidence_contract"]
    assert evidence["raw_policy_or_identity_output_in_git_forbidden"] is True
    assert evidence["raw_policy_or_identity_output_in_chat_forbidden"] is True
    assert evidence["raw_access_token_output_forbidden"] is True
    assert evidence["external_immutable_custody_status"] == "not_implemented"
    receipt = evidence["authenticated_receipt_contract"]
    assert receipt["status"] == (
        "specified_attestor_signer_and_verifier_not_implemented"
    )
    assert receipt["signature_algorithm"] == "ed25519"
    assert receipt["signature_message_fields"] == sorted(
        receipt["signature_message_fields"]
    )
    assert receipt["signature_domain_separator_hex"] == "00"
    assert receipt["receipt_payload_digest_scope"] == (
        "canonical_payload_safe_receipt_excluding_exactly_"
        "receipt_payload_digest_attestor_detached_signature_ref_"
        "and_attestor_signature_digest"
    )
    assert receipt[
        "downstream_bootstrap_requires_verified_authenticated_receipt_digest"
    ] is True
    assert {
        "approval_id_digest",
        "attestation_payload_digest",
        "authorization_envelope_self_digest",
        "attestor_detached_signature_ref",
        "executor_artifact_digest",
        "nonce_digest",
        "receipt_payload_digest",
        "review_contract_digest",
        "verifier_artifact_digest",
    } <= set(evidence["receipt_fields"])
    verification = evidence["receipt_verification_record_contract"]
    assert verification["status"] == "specified_verifier_not_implemented"
    assert verification["record_digest_scope"] == (
        "canonical_verification_record_excluding_only_verification_record_digest"
    )
    assert "attestor_signature_verified" not in evidence["receipt_fields"]
    assert set(verification["allowed_verification_results"]) == {
        "verified",
        "rejected",
        "unknown",
    }


def test_caps_and_acceptance_preserve_every_sealed_boundary() -> None:
    package = _load(_PACKAGE_PATH)
    payload = package["attestation"]["payload"]
    caps = payload["caps"]
    assert caps["target_projects"] == 2
    assert caps["active_gcloud_users"] == 1
    assert caps["total_external_api_requests"] == 200
    assert caps["retries"] == 0
    assert caps["concurrency"] == 1
    for key in (
        "mutating_operations",
        "service_enablement_operations",
        "service_account_impersonations",
        "access_tokens_exported",
        "firestore_document_reads",
        "firestore_document_writes",
        "provider_or_pstn_requests",
        "workload_invocations",
    ):
        assert caps[key] == 0

    acceptance = payload["acceptance_contract"]
    assert acceptance["allowed_results"] == ["pass", "fail", "incomplete"]
    assert acceptance["fail_closed_on_any_unknown"] is True
    assert acceptance["receipt_validity_ms"] == 900000
    assert acceptance["pass_does_not_authorize_bootstrap_or_runtime"] is True
    assert {
        "owner_confirmed_complete_target_inventory_digest_present",
        "owner_confirmed_future_principal_set_inventory_digest_present",
        "owner_confirmed_iam_bearing_resource_inventory_digest_present",
        "owner_confirmed_asset_type_coverage_matrix_digest_present",
        "owner_confirmed_broad_binding_exception_manifest_digest_present",
        "every_listed_deny_policy_fully_fetched_with_rules",
        "every_present_iam_bearing_resource_type_has_exact_policy_read_coverage",
        "authenticated_receipt_signature_and_trust_snapshot_verified",
        "authorization_approval_nonce_payload_and_review_digests_match_receipt",
        "executor_and_verifier_artifact_digests_match_authorization",
        "raw_evidence_in_external_encrypted_immutable_custody",
        "later_bootstrap_rechecks_policy_etags_and_receipt_digest_before_identity_creation",
        "later_bootstrap_rechecks_control_project_number_ancestry_and_principal_set_inventory_digest_before_identity_creation",
    } <= set(acceptance["pass_requires"])

    forbidden = set(payload["forbidden_actions"])
    assert {
        "discover_or_query_an_unlisted_project",
        "enable_or_disable_any_api",
        "set_or_change_any_iam_allow_deny_or_principal_access_boundary_policy",
        "create_or_use_service_account_impersonation",
        "create_export_or_print_any_credential_or_access_token",
        "read_or_write_any_firestore_document_or_realtime_database_value",
        "invoke_any_cloud_run_or_other_workload",
        "contact_any_provider_or_pstn",
        "modify_staging_or_production",
        "execute_bootstrap_runtime_or_task_4_8",
    } <= forbidden

    materialization = package["attestation"]["materialization_policy"]
    assert materialization["signature_message_fields"] == sorted(
        materialization["signature_message_fields"]
    )
    assert materialization["signature_domain_separator_hex"] == "00"
    assert materialization["self_digest_algorithm"] == "sha256"
    assert materialization["atomic_consumption_contract"] == (
        "durably_create_once_consume_both_approval_id_and_nonce_"
        "before_first_external_query"
    )
    assert materialization["retry_or_reuse_after_any_external_query"] == "forbidden"
    assert materialization["signature_message_construction"] == (
        "signature_domain_ascii_bytes || 0x00 || "
        "canonical_json_ascii_bytes_of_object_containing_exactly_"
        "signature_message_fields"
    )
    assert "review_contract_digest" in materialization["signature_message_fields"]
    assert "envelope_self_digest" in materialization["signature_message_fields"]
    assert set(materialization["required_envelope_artifact_digest_fields"]) == {
        "authorization_verifier_artifact_digest",
        "durable_approval_nonce_consumer_artifact_digest",
        "exact_read_only_executor_artifact_digest",
        "receipt_attestor_artifact_digest",
        "receipt_verifier_artifact_digest",
    }
    assert package["downstream_authority"][
        "authenticated_attestation_receipt_digest_required"
    ] is True
