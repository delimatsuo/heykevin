"""Offline guards for the source-only environment reconciliation package."""

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
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v1.json"
)
_SCHEMA_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v1.schema.json"
)
_GUIDE_PATH = (
    _ROOT
    / "docs/security/voice-bakeoff-environment-reconciliation-phase-0-5-v1.md"
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
    """Validate the checked-in schema subset without a runtime dependency."""

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

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"{path}:minLength")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            errors.append(f"{path}:pattern")
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


def test_package_matches_closed_schema_and_all_objects_reject_extra_fields():
    package = _package()
    schema = _load_json(_SCHEMA_PATH)

    assert _schema_errors(package, schema) == []
    object_schemas = _object_schemas(schema)
    assert object_schemas
    assert all(item.get("additionalProperties") is False for item in object_schemas)


def test_package_is_source_bound_and_cannot_authorize_connected_work():
    package = _package()
    authority = package["authority"]

    assert package["package_status"] == "source_only_draft"
    assert package["source_binding"] == {
        "repository": "https://github.com/delimatsuo/heykevin",
        "worktree": (
            "/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/"
            "voice-architecture-bakeoff-plan"
        ),
        "branch": "codex/voice-architecture-bakeoff-plan",
        "source_sha": _SOURCE_SHA,
        "tracked_tree_expected_clean": True,
        "package_is_authorization": False,
    }
    assert authority == {
        "authorized_scope": "source_only_phase_0_5",
        "connected_inventory_status": "not_authorized",
        "mutation_status": "not_authorized",
        "execution_status": "not_authorized",
        "task_4_8_status": "sealed",
        "owner_authorization_required_for_connected_inventory": True,
        "advisory_review_is_owner_authorization": False,
    }
    assert package["read_manifest"]["execution_supported"] is False
    assert package["read_manifest"]["project_creation_cap"] == 0
    assert package["transition_policy"]["project_creation_cap"] == 0


def test_registry_models_resources_instead_of_flat_project_environments():
    registry = _package()["environment_registry"]
    projects = registry["projects"]
    resources = registry["resources"]

    assert registry["project_identity_rule"] == (
        "project_id_plus_immutable_project_number_not_display_name_or_"
        "account_visibility"
    )
    assert tuple(project["project_id"] for project in projects) == _PROJECT_IDS
    assert len({project["project_id"] for project in projects}) == 4
    assert all(project["project_number"] is None for project in projects)
    assert all(project["display_name_selector_allowed"] is False for project in projects)
    assert all(project["inventory_status"] == "incomplete" for project in projects)
    assert all(project["governance_status"] == "retain_frozen" for project in projects)
    assert all(
        project["resource_authority_status"] == "candidate"
        for project in projects
    )

    by_ref = {resource["resource_ref"]: resource for resource in resources}
    assert len(by_ref) == len(resources)
    assert by_ref["production_cloud_run"]["project_id"] == "kevin-491315"
    assert by_ref["staging_cloud_run"]["project_id"] == "kevin-491315"
    assert by_ref["staging_firestore"]["project_id"] == "kevin-staging-491315"
    assert by_ref["staging_rtdb"]["project_id"] == "kevin-staging-491315"
    assert (
        by_ref["bakeoff_control_firestore"]["project_id"]
        == "hk-voice-bakeoff-0724-iso"
    )
    assert (
        by_ref["bakeoff_preauth_firestore"]["project_id"]
        == "hk-voice-bakeoff-preauth-iso"
    )

    authoritative_by_domain: dict[str, list[str]] = {}
    for resource in resources:
        if resource["resource_authority_status"] == "authoritative":
            authoritative_by_domain.setdefault(
                resource["security_domain"], []
            ).append(resource["resource_ref"])
    assert all(len(refs) <= 1 for refs in authoritative_by_domain.values())


def test_graph_has_closed_refs_and_declares_split_staging_and_isolation():
    registry = _package()["environment_registry"]
    resources = {resource["resource_ref"] for resource in registry["resources"]}
    edges = registry["edges"]

    assert all(edge["from_ref"] in resources for edge in edges)
    assert all(edge["to_ref"] in resources for edge in edges)
    edge_set = {
        (edge["from_ref"], edge["relationship"], edge["to_ref"])
        for edge in edges
    }
    assert (
        "staging_cloud_run",
        "hosted_by",
        "project_kevin_production",
    ) in edge_set
    assert (
        "staging_cloud_run",
        "source_declared_reads_from",
        "staging_firestore",
    ) in edge_set
    assert (
        "staging_cloud_run",
        "source_declared_reads_from",
        "staging_rtdb",
    ) in edge_set
    assert (
        "bakeoff_control_firestore",
        "must_remain_distinct_from",
        "bakeoff_preauth_firestore",
    ) in edge_set
    assert {
        target
        for source, relationship, target in edge_set
        if relationship == "must_not_reach"
        and source in {"bakeoff_control_firestore", "bakeoff_preauth_firestore"}
    } == {"production_firestore", "staging_firestore"}


def test_operator_contract_fails_closed_on_account_and_target_ambiguity():
    contract = _package()["operator_targeting_contract"]

    assert contract["banner_literal"] == _SEALED_BANNER
    assert {
        "local_configuration_ref",
        "identity_ref",
        "target_project_id",
        "expected_project_number",
        "observed_ancestry_digest",
        "quota_project_id",
        "api_method",
        "response_field_mask",
        "mutation_status",
        "execution_status",
    }.issubset(contract["required_banner_fields"])
    assert {
        "wrong_google_account",
        "wrong_project_id",
        "same_project_visible_from_two_accounts",
        "permission_denied_or_partial_visibility",
        "ambient_quota_project",
        "unbound_raw_evidence_custody",
    }.issubset(contract["tabletop_scenarios"])
    assert {
        "display_name",
        "recent_project_list",
        "ambient_gcloud_project",
        "application_default_credentials",
        "project_discovery",
        "account_visibility_alone",
    }.issubset(contract["prohibited_target_sources"])
    bootstrap = contract["bootstrap_identity_call"]
    assert bootstrap["method_ref"] == "resource_manager_project_get"
    assert bootstrap["only_call_allowed_before_project_number_binding"] is True
    assert (
        bootstrap["next_calls_require_exact_number_and_ancestry_binding"] is True
    )


def test_read_manifest_is_exact_non_mutating_and_payload_safe():
    manifest = _package()["read_manifest"]

    assert tuple(manifest["target_project_ids"]) == _PROJECT_IDS
    assert manifest["manifest_status"] == "non_executable_source_only"
    assert manifest["execution_supported"] is False
    assert manifest["completion_rule"].startswith(
        "missing_denied_ambiguous_unpaginated_or_unresolved_visibility"
    )
    assert all(manifest["request_rules"].values())

    methods = manifest["methods"]
    assert len({method["method_ref"] for method in methods}) == len(methods)
    assert {method["http_method"] for method in methods} <= {"GET", "POST"}
    prohibited_method_tokens = (
        ".create",
        ".delete",
        ".set",
        ".update",
        ".enable",
        ".disable",
        ".move",
        ".undelete",
    )
    assert all(
        not any(token in method["api_method"] for token in prohibited_method_tokens)
        for method in methods
    )
    assert all(method["response_field_mask"] for method in methods)
    assert all(
        method["pagination"] == "all_pages_required"
        for method in methods
        if method["api_method"].endswith((".list", "searchAllResources"))
    )

    policy_methods = [
        method for method in methods if method["api_method"].endswith("getIamPolicy")
    ]
    assert policy_methods
    assert all(
        method["request_body"] == {"options": {"requestedPolicyVersion": 3}}
        for method in policy_methods
    )
    assert all(
        method["raw_evidence_class"] == "restricted_external_custody"
        for method in policy_methods
    )

    custody = manifest["raw_evidence_custody"]
    assert custody == {
        "repository_storage_allowed": False,
        "chat_storage_allowed": False,
        "external_private_custody_required": True,
        "custody_mechanism_bound": False,
        "connected_execution_precondition_satisfied": False,
    }
    forbidden = set(manifest["payload_safe_receipt"]["forbidden_fields"])
    assert {
        "account_email",
        "billing_account_id",
        "policy_binding_members",
        "service_account_email",
        "resource_name",
        "credential",
        "access_token",
        "raw_api_response",
        "transcript",
        "phone_number",
    } <= forbidden


def test_schema_rejects_authority_target_and_shape_expansion():
    package = _package()
    schema = _load_json(_SCHEMA_PATH)

    mutations: list[tuple[tuple[object, ...], object]] = [
        (("package_status",), "owner_authorized"),
        (("authority", "connected_inventory_status"), "authorized"),
        (("authority", "execution_status"), "authorized"),
        (("read_manifest", "execution_supported"), True),
        (("read_manifest", "project_creation_cap"), 1),
        (
            ("environment_registry", "projects", 0, "project_number"),
            "752910912062",
        ),
        (
            (
                "environment_registry",
                "projects",
                0,
                "display_name_selector_allowed",
            ),
            True,
        ),
        (
            ("read_manifest", "target_project_ids"),
            [*_PROJECT_IDS, "new-voice-bakeoff-project"],
        ),
    ]
    for path, replacement in mutations:
        candidate = deepcopy(package)
        current: object = candidate
        for part in path[:-1]:
            current = current[part]  # type: ignore[index]
        current[path[-1]] = replacement  # type: ignore[index]
        assert _schema_errors(candidate, schema), path

    extra_field = deepcopy(package)
    extra_field["read_manifest"]["methods"][0]["unexpected"] = "discovery"
    assert _schema_errors(extra_field, schema)


def test_protected_historical_artifacts_match_recorded_digests_when_present():
    protected = _package()["historical_artifact_policy"]["protected_artifacts"]
    observed = 0
    for artifact in protected:
        path = _ROOT / artifact["path"]
        if not path.exists():
            continue
        observed += 1
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]

    # The files are intentionally untracked local evidence. A clean CI checkout
    # may have none; this working session must have the complete protected set.
    assert observed in {0, len(protected)}


def test_guide_recommends_freeze_and_contains_no_connected_command_recipe():
    guide = _GUIDE_PATH.read_text(encoding="utf-8")

    assert _SOURCE_SHA in guide
    assert _SEALED_BANNER in guide
    assert "Do not create another project" in guide
    assert "That is not evidence that a project was duplicated" in guide
    assert "Retain frozen" in guide
    assert "Task 4.8 remains sealed" in guide
    assert "gcloud " not in guide
    assert "curl " not in guide
    assert "gsutil " not in guide
