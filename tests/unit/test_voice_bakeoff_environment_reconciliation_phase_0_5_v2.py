"""Offline guards for the versioned Phase 0.5 environment reconciliation."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any


_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.json"
)
_SCHEMA_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.schema.json"
)
_GUIDE_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v2.md"
)
_SOURCE_SHA = "2ed8ea7d1d7f338e84ddf08d5a50a714835e1533"
_PROJECT_IDS = (
    "kevin-491315",
    "kevin-staging-491315",
    "hk-voice-bakeoff-0724-iso",
    "hk-voice-bakeoff-preauth-iso",
)
_SEALED_BANNER = (
    "CONNECTED EXECUTION SEALED — READ-ONLY RECONCILIATION NOT AUTHORIZED"
)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _package() -> dict[str, Any]:
    return _load_json(_PACKAGE_PATH)


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _type_matches(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise AssertionError(f"unsupported schema type in local validator: {expected}")


def _schema_errors(
    value: object,
    schema: dict[str, Any],
    *,
    root: dict[str, Any] | None = None,
    path: str = "$",
) -> list[str]:
    """Validate the schema subset used by this source-only package."""

    root = schema if root is None else root
    if "$ref" in schema:
        reference = schema["$ref"]
        assert isinstance(reference, str) and reference.startswith("#/")
        target: object = root
        for part in reference[2:].split("/"):
            assert isinstance(target, dict)
            target = target[part.replace("~1", "/").replace("~0", "~")]
        assert isinstance(target, dict)
        return _schema_errors(value, target, root=root, path=path)

    errors: list[str] = []
    for branch in schema.get("allOf", []):
        errors.extend(_schema_errors(value, branch, root=root, path=path))
    condition = schema.get("if")
    if isinstance(condition, dict):
        selected = (
            schema.get("then")
            if not _schema_errors(value, condition, root=root, path=path)
            else schema.get("else")
        )
        if isinstance(selected, dict):
            errors.extend(_schema_errors(value, selected, root=root, path=path))

    if "const" in schema and not _json_equal(value, schema["const"]):
        errors.append(f"{path}:const")
    if "enum" in schema and not any(
        _json_equal(value, member) for member in schema["enum"]
    ):
        errors.append(f"{path}:enum")

    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        type_valid = _type_matches(value, expected_type)
    elif isinstance(expected_type, list):
        type_valid = any(
            isinstance(member, str) and _type_matches(value, member)
            for member in expected_type
        )
    else:
        type_valid = True
    if not type_valid:
        errors.append(f"{path}:type")
        return errors

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}:required")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, child in value.items():
                child_schema = properties.get(key)
                if isinstance(child_schema, dict):
                    errors.extend(
                        _schema_errors(
                            child,
                            child_schema,
                            root=root,
                            path=f"{path}.{key}",
                        )
                    )
                elif schema.get("additionalProperties") is False:
                    errors.append(f"{path}.{key}:additional")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.append(f"{path}:minItems")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.append(f"{path}:maxItems")
        if schema.get("uniqueItems") is True:
            normalized = [
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in value
            ]
            if len(normalized) != len(set(normalized)):
                errors.append(f"{path}:uniqueItems")

        contains = schema.get("contains")
        if isinstance(contains, dict):
            matches = sum(
                not _schema_errors(
                    child, contains, root=root, path=f"{path}[{index}]"
                )
                for index, child in enumerate(value)
            )
            min_contains = schema.get("minContains", 1)
            max_contains = schema.get("maxContains")
            if isinstance(min_contains, int) and matches < min_contains:
                errors.append(f"{path}:minContains")
            if isinstance(max_contains, int) and matches > max_contains:
                errors.append(f"{path}:maxContains")

        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list):
            for index, child_schema in enumerate(prefix_items):
                if index >= len(value):
                    break
                assert isinstance(child_schema, dict)
                errors.extend(
                    _schema_errors(
                        value[index],
                        child_schema,
                        root=root,
                        path=f"{path}[{index}]",
                    )
                )
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                errors.extend(
                    _schema_errors(
                        child,
                        item_schema,
                        root=root,
                        path=f"{path}[{index}]",
                    )
                )
        elif item_schema is False and isinstance(prefix_items, list):
            if len(value) > len(prefix_items):
                errors.append(f"{path}:items")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"{path}:minLength")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            errors.append(f"{path}:pattern")
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and isinstance(schema.get("minimum"), int)
        and value < schema["minimum"]
    ):
        errors.append(f"{path}:minimum")
    return errors


def _object_schemas(value: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        if value.get("type") == "object":
            found.append(value)
        for child in value.values():
            found.extend(_object_schemas(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_object_schemas(child))
    return found


def _complete_receipt() -> dict[str, Any]:
    targets = []
    for index, project_id in enumerate(_PROJECT_IDS, start=1):
        identity_ref = (
            "organization_operator_identity"
            if project_id.startswith("kevin")
            else "isolated_bakeoff_operator_identity"
        )
        targets.append(
            {
                "project_id": project_id,
                "project_number": f"12345{index}",
                "identity_ref": identity_ref,
                "local_configuration_digest": f"{index:x}" * 64,
                "ancestry_digest": f"{index + 1:x}" * 64,
                "billing_association_digest": f"{index + 2:x}" * 64,
                "service_state_digest": f"{index + 3:x}" * 64,
                "resource_inventory_digest": f"{index + 4:x}" * 64,
                "iam_inventory_digest": f"{index + 5:x}" * 64,
                "org_policy_digest": f"{index + 6:x}" * 64,
                "pab_inventory_digest": f"{index + 7:x}" * 64,
                "coverage_status": "complete",
                "all_pages_exhausted": True,
                "all_list_items_fetched": True,
                "freshness_expires_at_ms": 2_000_000,
                "completeness_errors": [],
            }
        )
    return {
        "receipt_schema_version": 2,
        "source_sha": _SOURCE_SHA,
        "contract_digest": "a" * 64,
        "owner_identity_recovery_attestation_digest": "b" * 64,
        "coverage_matrix_digest": "c" * 64,
        "raw_evidence_custody_digest": "d" * 64,
        "request_receipt_digests": ["e" * 64],
        "target_results": targets,
        "pass_predicates": {
            "exactly_four_targets": True,
            "project_identity_and_ancestry_complete": True,
            "service_and_resource_coverage_complete": True,
            "iam_and_role_coverage_complete": True,
            "org_policy_and_pab_coverage_complete_or_proven_not_applicable": True,
            "private_custody_complete": True,
            "owner_identity_recovery_attestation_present": True,
            "single_request_authorizations_consumed_once": True,
            "within_all_caps": True,
            "fresh": True,
        },
        "completeness_errors": [],
        "completed_at_ms": 1_000_000,
        "expires_at_ms": 2_000_000,
        "result": "pass",
    }


def test_v2_package_matches_schema_and_schema_objects_are_closed():
    package = _package()
    schema = _load_json(_SCHEMA_PATH)

    assert _schema_errors(package, schema) == []
    object_schemas = _object_schemas(schema)
    assert object_schemas
    assert all(item.get("additionalProperties") is False for item in object_schemas)


def test_v2_preserves_v1_and_remains_non_authorizing():
    package = _package()

    assert package["provenance"]["successor_of"] == {
        "path": (
            "docs/security/"
            "voice-bakeoff-environment-reconciliation-phase-0-5-v1.json"
        ),
        "sha256": (
            "5d4f7dd2d492fe3bf8f26a32ec137f4485d4601fddd81f8890c56927c86507f9"
        ),
        "review_outcome": (
            "rejected_for_connected_read_contract_preserved_as_freeze_evidence"
        ),
    }
    assert package["package_status"] == "source_only_exact_review_candidate"
    assert package["source_binding"]["source_sha"] == _SOURCE_SHA
    assert package["source_binding"]["package_is_authorization"] is False
    assert package["authority"]["connected_inventory_status"] == "not_authorized"
    assert package["authority"]["mutation_status"] == "not_authorized"
    assert package["authority"]["execution_status"] == "not_authorized"
    assert package["authority"]["task_4_8_status"] == "sealed"
    assert package["read_contract"]["connected_execution_supported"] is False


def test_status_model_separates_governance_default_review_and_authority():
    package = _package()
    current = package["status_model"]["current"]
    registry = package["environment_registry"]

    assert current == {
        "inventory": "incomplete",
        "governance": "undecided",
        "default_disposition": "retain_frozen",
        "review": "pending_exact_artifact_review",
        "connected_read": "not_authorized",
        "mutation": "mutation_not_authorized",
        "execution": "execution_not_authorized",
    }
    assert registry["governance_status"] == "undecided"
    assert registry["default_disposition"] == "retain_frozen"
    assert registry["review_status"] == "pending_exact_artifact_review"
    assert package["status_model"]["dominant_display_precedence"][0] == (
        "execution_not_authorized"
    )
    assert any(
        "Receipt pass changes inventory evidence only" in rule
        for rule in package["status_model"]["rules"]
    )


def test_identity_visibility_is_many_to_many_and_not_project_identity():
    identity = _package()["identity_model"]

    assert identity["observed_visibility_claims"] == []
    assert identity["designated_query_identity_by_project"] == {
        "kevin-491315": "organization_operator_identity",
        "kevin-staging-491315": "organization_operator_identity",
        "hk-voice-bakeoff-0724-iso": "isolated_bakeoff_operator_identity",
        "hk-voice-bakeoff-preauth-iso": "isolated_bakeoff_operator_identity",
    }
    assert all(
        alias["checked_in_account_identifier"] is None
        and alias["checked_in_credential_material"] is False
        and alias["local_configuration_ref"] == "required_but_not_bound"
        for alias in identity["identity_aliases"]
    )
    assert any(
        "visible to multiple identities" in rule for rule in identity["rules"]
    )


def test_graph_uses_neutral_project_containers_and_role_cardinality():
    registry = _package()["environment_registry"]
    projects = registry["projects"]
    resources = {item["resource_ref"]: item for item in registry["resources"]}
    edges = {tuple(edge) for edge in registry["edges"]}

    assert tuple(project["project_id"] for project in projects) == _PROJECT_IDS
    assert all(project["project_number"] is None for project in projects)
    assert (
        resources["project_container_kevin_491315"]["environment_role"]
        == "mixed_control_plane_container"
    )
    assert resources["production_cloud_run"]["project_id"] == "kevin-491315"
    assert resources["staging_cloud_run"]["project_id"] == "kevin-491315"
    assert resources["staging_firestore"]["project_id"] == "kevin-staging-491315"
    assert (
        "production_cloud_run",
        "source_declared_reads_from",
        "production_firestore",
    ) in edges
    assert (
        "production_cloud_run",
        "source_declared_reads_from",
        "production_rtdb",
    ) in edges
    assert (
        "staging_cloud_run",
        "source_declared_reads_from",
        "staging_firestore",
    ) in edges
    assert registry["authority_cardinality"]["key"] == "canonical_resource_role"
    assert len(
        {resource["canonical_resource_role"] for resource in resources.values()}
    ) == len(resources)


def test_bootstrap_has_no_number_ancestry_deadlock():
    states = _package()["operator_targeting_contract"]["bootstrap_state_machine"]
    by_state = {state["state"]: state for state in states}

    assert set(by_state) == {
        "sealed",
        "project_identity_only",
        "ancestry_only",
        "bound_single_read",
    }
    assert by_state["sealed"]["only_permitted_connected_method_ref"] is None
    assert (
        by_state["project_identity_only"]["only_permitted_connected_method_ref"]
        == "project_identity_get"
    )
    assert "exact_project_number" in by_state["ancestry_only"]["bound_inputs"]
    assert (
        by_state["ancestry_only"]["only_permitted_connected_method_ref"]
        == "project_ancestry_get"
    )
    assert "ancestry_digest" in by_state["bound_single_read"]["bound_inputs"]
    assert all("return to sealed" in state["transition"] for state in states[1:])


def test_tabletops_are_behavioral_and_forbid_fallback_or_duplication():
    contract = _package()["operator_targeting_contract"]
    cases = contract["tabletop_cases"]

    assert len(cases) == 10
    assert len({case["scenario_id"] for case in cases}) == 10
    assert all(case["expected_inventory_status"] == "incomplete" for case in cases)
    assert all(case["fallback_allowed"] is False for case in cases)
    assert all(
        case["duplicate_project_recommendation_allowed"] is False
        for case in cases
    )
    assert all(
        case["expected_action"].startswith(("abort", "record_"))
        for case in cases
    )
    examples = {item["example"]: item for item in contract["status_examples"]}
    assert examples["current_source_only"]["dominant_banner"] == _SEALED_BANNER
    assert examples["future_first_bind_after_separate_owner_authorization"][
        "project_number_display"
    ] == "UNBOUND — PROJECT GET ONLY"
    assert (
        examples["blocked_incomplete"]["connected_read_status"]
        == "not_authorized"
    )


def test_read_contract_is_exact_bounded_and_covers_required_surfaces():
    read_contract = _package()["read_contract"]
    methods = read_contract["methods"]
    coverage = read_contract["coverage_matrix"]

    assert tuple(read_contract["target_project_ids"]) == _PROJECT_IDS
    assert len(methods) == 31
    assert len(read_contract["exact_service_name_allowlist"]) == 12
    assert len(read_contract["exact_detailed_asset_type_allowlist"]) == 7
    assert read_contract["parameter_binding_rules"][
        "unbound_ambiguous_mismatched_or_extra_parameter_action"
    ] == "incomplete_and_abort_without_fallback"
    assert read_contract["parameter_binding_rules"][
        "EXACT_OWNER_BOUND_MEMBER_QUERY"
    ].startswith("one of the exact future control principal")
    assert read_contract["caps"]["connected_requests_per_owner_authorization"] == 1
    assert read_contract["caps"]["project_creation"] == 0
    assert read_contract["caps"]["mutating_operations"] == 0
    assert read_contract["caps"]["access_tokens_exported"] == 0
    assert read_contract["caps"]["firestore_document_reads"] == 0
    assert read_contract["caps"]["firebase_record_reads"] == 0
    assert read_contract["caps"]["secret_payload_reads"] == 0
    assert read_contract["caps"]["provider_or_pstn_requests"] == 0
    assert all(read_contract["request_rules"].values())

    covered_methods = {
        method_ref
        for _, method_refs, _ in coverage
        for method_ref in method_refs
    }
    assert covered_methods == set(methods)
    surfaces = {row[0] for row in coverage}
    assert {
        "billing_association",
        "kms_keyring_and_cryptokey_metadata",
        "rtdb_instance_metadata",
        "secret_metadata_without_payload",
        "audit_sink_metadata",
        "project_and_ancestor_allow_iam",
        "resource_allow_iam",
        "deny_iam",
        "service_account_inventory_and_impersonation_policy",
        "role_permission_resolution",
        "principal_access_boundary_policy_and_bindings",
        "organization_policy_inheritance",
        "supplementary_effective_access",
        "owner_identity_recovery_and_account_custody",
        "raw_evidence_private_custody",
    } <= surfaces

    assert methods["project_identity_get"]["raw_evidence_class"] == (
        "restricted_external_custody"
    )
    cloud_run_fields = set(
        methods["cloud_run_service_get"]["response_field_mask"]
    )
    assert cloud_run_fields == {
        "createTime",
        "etag",
        "generation",
        "name",
        "observedGeneration",
        "uid",
        "updateTime",
    }
    assert {
        "template",
        "traffic",
        "uri",
        "buildConfig",
        "serviceAccount",
        "environmentVariables",
    }.isdisjoint(cloud_run_fields)


def test_schema_rejects_method_add_remove_token_post_body_and_field_widening():
    package = _package()
    schema = _load_json(_SCHEMA_PATH)
    methods_path = package["read_contract"]["methods"]

    added = deepcopy(package)
    added["read_contract"]["methods"]["generate_access_token"] = {
        "api_method": "iamcredentials.projects.serviceAccounts.generateAccessToken",
        "http_method": "POST",
        "endpoint": "iamcredentials.googleapis.com",
        "path": "/v1/projects/-/serviceAccounts/x:generateAccessToken",
        "request_body": {},
        "response_field_mask": ["accessToken"],
        "pagination": "not_applicable",
        "raw_evidence_class": "restricted_external_custody",
    }
    assert _schema_errors(added, schema)

    removed = deepcopy(package)
    del removed["read_contract"]["methods"]["organization_iam_policy_get"]
    assert _schema_errors(removed, schema)

    mutations = [
        ("project_identity_get", "api_method", "cloudresourcemanager.projects.delete"),
        ("project_ancestry_get", "request_body", {"discover": True}),
        (
            "cloud_run_service_get",
            "response_field_mask",
            [*methods_path["cloud_run_service_get"]["response_field_mask"], "template"],
        ),
        (
            "project_iam_policy_get",
            "request_body",
            {"options": {"requestedPolicyVersion": 1}},
        ),
        (
            "detailed_asset_inventory_search",
            "path",
            "/v1/projects/-:searchAllResources",
        ),
    ]
    for method_ref, field, replacement in mutations:
        candidate = deepcopy(package)
        candidate["read_contract"]["methods"][method_ref][field] = replacement
        assert _schema_errors(candidate, schema), (method_ref, field)


def test_schema_rejects_allowlist_cap_coverage_authority_and_target_expansion():
    package = _package()
    schema = _load_json(_SCHEMA_PATH)

    mutations = [
        (
            ("read_contract", "target_project_ids"),
            [*_PROJECT_IDS, "new-duplicate-project"],
        ),
        (
            ("read_contract", "exact_service_name_allowlist"),
            [*package["read_contract"]["exact_service_name_allowlist"], "x.invalid"],
        ),
        (
            ("read_contract", "caps", "project_creation"),
            1,
        ),
        (
            ("read_contract", "coverage_matrix"),
            package["read_contract"]["coverage_matrix"][:-1],
        ),
        (
            ("authority", "connected_inventory_status"),
            "authorized",
        ),
        (
            ("environment_registry", "review_status"),
            "advisory_pass",
        ),
    ]
    for path, replacement in mutations:
        candidate = deepcopy(package)
        current: object = candidate
        for part in path[:-1]:
            current = current[part]  # type: ignore[index]
        current[path[-1]] = replacement  # type: ignore[index]
        assert _schema_errors(candidate, schema), path


def test_receipt_schema_allows_incomplete_but_pass_requires_every_predicate():
    schema = _load_json(_SCHEMA_PATH)
    receipt_schema = schema["$defs"]["payload_safe_receipt"]
    complete = _complete_receipt()

    assert _schema_errors(complete, receipt_schema, root=schema) == []

    incomplete = deepcopy(complete)
    incomplete["result"] = "incomplete"
    incomplete["pass_predicates"]["private_custody_complete"] = False
    incomplete["target_results"][0]["coverage_status"] = "incomplete"
    incomplete["target_results"][0]["completeness_errors"] = ["custody_unbound"]
    incomplete["completeness_errors"] = ["custody_unbound"]
    assert _schema_errors(incomplete, receipt_schema, root=schema) == []

    invalid_passes = []
    false_predicate = deepcopy(complete)
    false_predicate["pass_predicates"]["fresh"] = False
    invalid_passes.append(false_predicate)
    target_incomplete = deepcopy(complete)
    target_incomplete["target_results"][1]["coverage_status"] = "incomplete"
    invalid_passes.append(target_incomplete)
    target_error = deepcopy(complete)
    target_error["target_results"][2]["completeness_errors"] = ["partial"]
    invalid_passes.append(target_error)
    duplicate_target = deepcopy(complete)
    duplicate_target["target_results"][3]["project_id"] = "kevin-491315"
    invalid_passes.append(duplicate_target)
    raw_field = deepcopy(complete)
    raw_field["policy_binding_members"] = ["allUsers"]
    invalid_passes.append(raw_field)

    assert all(
        _schema_errors(candidate, receipt_schema, root=schema)
        for candidate in invalid_passes
    )


def test_all_protected_historical_artifacts_match_when_present():
    protected = _package()["historical_artifact_policy"]["protected_artifacts"]
    observed = 0
    for relative_path, expected_digest in protected:
        path = _ROOT / relative_path
        if not path.exists():
            continue
        observed += 1
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_digest
    assert observed in {0, len(protected)}


def test_guide_requires_review_then_one_request_successor_not_v2_authorization():
    guide = _GUIDE_PATH.read_text(encoding="utf-8")

    assert _SOURCE_SHA in guide
    assert _SEALED_BANNER in guide
    assert "The screenshots do not show a duplicate project" in guide
    assert "Do not authorize v2 itself" in guide
    assert "one read request" in guide
    assert "Task 4.8 remains sealed" in guide
    assert "gcloud " not in guide
    assert "curl " not in guide
    assert "gsutil " not in guide
