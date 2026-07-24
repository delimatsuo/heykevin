"""Pure validation for the non-executable Task 4.8 gate review package."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Literal


RESOURCE_KINDS = {
    "execution_principal": "identity",
    "telephony": "telephony",
    "native_voice": "provider",
    "speech_to_text": "provider",
    "text_generation": "provider",
    "text_to_speech": "provider",
    "conversation_relay": "provider",
    "auth_token_store": "state_store",
    "firestore": "state_store",
    "rtdb": "state_store",
    "logging_evidence": "evidence_store",
    "kms": "key_management",
    "immutable_custody": "immutable_store",
    "nonce_store": "state_store",
    "active_execution_store": "state_store",
    "trust_store": "trust_store",
    "credential_broker": "credential_broker",
    "residue_receipt_sink": "evidence_store",
    "telephony_control_plane": "control_plane",
    "native_voice_control_plane": "control_plane",
    "speech_to_text_control_plane": "control_plane",
    "text_generation_control_plane": "control_plane",
    "text_to_speech_control_plane": "control_plane",
    "conversation_relay_control_plane": "control_plane",
}
RESOURCE_IDS = tuple(RESOURCE_KINDS)
ARM_DEPENDENCIES = {
    "A": (
        "execution_principal",
        "telephony",
        "telephony_control_plane",
        "native_voice",
        "native_voice_control_plane",
        "auth_token_store",
        "nonce_store",
        "active_execution_store",
        "firestore",
        "rtdb",
        "logging_evidence",
        "kms",
        "immutable_custody",
        "trust_store",
        "credential_broker",
        "residue_receipt_sink",
    ),
    "B1": (
        "execution_principal",
        "telephony",
        "telephony_control_plane",
        "speech_to_text",
        "speech_to_text_control_plane",
        "text_generation",
        "text_generation_control_plane",
        "text_to_speech",
        "text_to_speech_control_plane",
        "auth_token_store",
        "nonce_store",
        "active_execution_store",
        "firestore",
        "rtdb",
        "logging_evidence",
        "kms",
        "immutable_custody",
        "trust_store",
        "credential_broker",
        "residue_receipt_sink",
    ),
    "B2": (
        "execution_principal",
        "telephony",
        "telephony_control_plane",
        "conversation_relay",
        "conversation_relay_control_plane",
        "text_generation",
        "text_generation_control_plane",
        "auth_token_store",
        "nonce_store",
        "active_execution_store",
        "firestore",
        "rtdb",
        "logging_evidence",
        "kms",
        "immutable_custody",
        "trust_store",
        "credential_broker",
        "residue_receipt_sink",
    ),
    "C": (
        "execution_principal",
        "telephony",
        "telephony_control_plane",
        "native_voice",
        "native_voice_control_plane",
        "auth_token_store",
        "nonce_store",
        "active_execution_store",
        "firestore",
        "rtdb",
        "logging_evidence",
        "kms",
        "immutable_custody",
        "trust_store",
        "credential_broker",
        "residue_receipt_sink",
    ),
}
PROBE_FACTS = (
    "dependency_identity_attestation",
    "input_finality",
    "pre_response_permit",
    "generation_completed",
    "generation_cancelled",
    "generation_failed",
    "transport_resolved",
    "transport_partial",
    "transport_cleared",
    "transport_failed_delivery",
    "caller_playback_classification",
    "caller_activity",
    "interruption",
    "audible_stop",
    "reconnect",
    "epoch_invalidation",
    "pre_auth_no_provider_work",
)
B2_ONLY_PROBE_FACTS = ("response_correlated_normal_playback_receipt",)
PROBE_FACTS_BY_ARM = {
    arm: (
        PROBE_FACTS + B2_ONLY_PROBE_FACTS
        if arm == "B2"
        else PROBE_FACTS
    )
    for arm in ARM_DEPENDENCIES
}
CALLER_AUDIO_CORRELATED_FACTS = {
    "transport_resolved",
    "transport_partial",
    "transport_cleared",
    "transport_failed_delivery",
    "caller_playback_classification",
    "caller_activity",
    "interruption",
    "audible_stop",
    "response_correlated_normal_playback_receipt",
}
PRIVACY_SETTINGS = {
    "request_response_logging": "disabled",
    "training_data_sharing": "disabled",
    "tracing": "disabled",
    "recording": "disabled",
    "cache_resumption": "disabled",
    "retention": "approved_bounded",
    "region": "approved_bounded",
    "abuse_monitoring": "approved_bounded",
    "deletion": "approved_bounded",
    "dpa": "approved_bounded",
    "subprocessors": "approved_bounded",
}
SIGNER_ROLES = ("staff", "security_privacy", "conversation_product")
ARTIFACT_DIGEST_KEYS = (
    "corpus",
    "setup",
    "configuration",
    "evaluator",
    "security_annex",
    "caller_ux",
    "gate_validator",
)
CAP_KEYS = (
    "requests",
    "attempts",
    "calls",
    "concurrency",
    "duration_ms",
    "bytes",
    "audio_ms",
    "retries",
    "tokens",
    "cost_minor_units",
    "artifact_ttl_ms",
)
DISABLED_FEATURE_KEYS = (
    "tools",
    "writes",
    "notifications",
    "terminal_actions",
    "transfers",
    "recording",
    "tracing",
    "data_sharing",
    "request_response_logging",
    "session_resumption",
    "provider_cache",
)

_TOP_LEVEL_KEYS = {
    "package_version",
    "package_status",
    "execution_supported",
    "resource_inventory",
    "privacy_attestations",
    "signature_envelope",
    "review_digest",
}
_RESOURCE_INVENTORY_KEYS = {
    "schema_version",
    "status",
    "environment",
    "production_denylist_digest",
    "resources",
    "arm_dependencies",
    "probe_contracts",
}
_RESOURCE_KEYS = {
    "resource_id",
    "kind",
    "selection_status",
    "provider_ref",
    "product_ref",
    "api_version_ref",
    "account_region_ref",
    "workload_destination_allowlist_ref",
    "control_plane_destination_allowlist_ref",
    "credential_ref",
    "identity_ref",
    "isolation_proof_ref",
    "residue_procedure_ref",
}
_RESOURCE_REF_KEYS = _RESOURCE_KEYS - {
    "resource_id",
    "kind",
    "selection_status",
}
_PROBE_CONTRACT_KEYS = {
    "arm",
    "fact",
    "status",
    "synthetic_fixture_digest",
    "capability_mapping_ref",
    "evidence_sink_ref",
    "caller_audio_correlation_required",
    "correlation_contract_ref",
}
_PRIVACY_TOP_LEVEL_KEYS = {"schema_version", "status", "attestations"}
_ATTESTATION_KEYS = {
    "resource_id",
    "status",
    "provider_api_ref",
    "account_region_ref",
    "evidence_source_class",
    "evidence_ref",
    "evidence_digest",
    "observed_at_ms",
    "expires_at_ms",
    "owner_ref",
    "residue_verification_ref",
    "deletion_procedure_ref",
    "settings",
}
_SETTING_KEYS = {"state", "evidence_ref", "evidence_digest"}
_SIGNATURE_ENVELOPE_KEYS = {
    "schema_version",
    "status",
    "execution_supported",
    "evidence_tier",
    "arm",
    "source_sha",
    "manifest_digest",
    "resource_inventory_digest",
    "privacy_attestations_digest",
    "capability_contract_digest",
    "approved_pstn_source_digest",
    "approved_pstn_destination_digest",
    "evidence_kms_key_version_ref",
    "nonce_consumption_policy_digest",
    "nonce_consumption_record_ref",
    "active_execution_binding_digest",
    "findings_digest",
    "unresolved_p1_count",
    "artifact_digests",
    "caps",
    "disabled_features",
    "custody",
    "trust_store",
    "approval_id",
    "nonce",
    "issued_at_ms",
    "expires_at_ms",
    "signers",
    "envelope_self_digest",
}
_CUSTODY_KEYS = {
    "immutable_envelope_ref",
    "immutable_manifest_ref",
    "nonce_store_ref",
    "active_execution_store_ref",
    "residue_receipt_sink_ref",
}
_TRUST_STORE_KEYS = {
    "version_ref",
    "policy_digest",
    "algorithm",
    "rotation_status_ref",
    "revocation_status_ref",
    "immutable_custody_ref",
    "no_self_approval",
    "no_break_glass",
}
_SIGNER_KEYS = {
    "role",
    "identity_ref",
    "key_id",
    "public_key_ref",
    "revocation_status_ref",
    "detached_signature_ref",
    "signature_digest",
    "review_decision",
}


@dataclass(frozen=True, slots=True)
class PreparationResult:
    verdict: Literal[
        "preparation_incomplete",
        "preparation_complete_external_gates_required",
    ]
    errors: tuple[str, ...]


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def review_digest(package: Mapping[str, object]) -> str:
    return canonical_digest(
        {key: package[key] for key in sorted(package) if key != "review_digest"}
    )


def signature_payload_digest(envelope: Mapping[str, object]) -> str:
    material: dict[str, object] = {}
    for key in sorted(envelope):
        if key == "envelope_self_digest":
            continue
        value = envelope[key]
        if key == "signers" and isinstance(value, list):
            material[key] = [
                {
                    signer_key: signer[signer_key]
                    for signer_key in sorted(signer)
                    if signer_key
                    not in {"detached_signature_ref", "signature_digest"}
                }
                if isinstance(signer, Mapping)
                else signer
                for signer in value
            ]
        else:
            material[key] = value
    return canonical_digest(material)


def _is_digest(value: object, *, length: int = 64) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_ref(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("ref_")
        and 5 <= len(value) <= 160
        and all(
            character.isascii()
            and (character.isalnum() or character in "_-")
            for character in value
        )
    )


def _mapping(
    value: object,
    *,
    expected_keys: set[str],
    code: str,
    errors: list[str],
) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        errors.append(code)
        return None
    return value


def _validate_resource_inventory(
    value: object,
    *,
    errors: list[str],
) -> None:
    inventory = _mapping(
        value,
        expected_keys=_RESOURCE_INVENTORY_KEYS,
        code="resource_inventory.schema",
        errors=errors,
    )
    if inventory is None:
        return
    if inventory["schema_version"] != 1:
        errors.append("resource_inventory.version")
    if inventory["status"] != "review_ready_external_gates_required":
        errors.append("resource_inventory.status")
    if inventory["environment"] != "bakeoff_nonproduction":
        errors.append("resource_inventory.environment")
    if not _is_digest(inventory["production_denylist_digest"]):
        errors.append("resource_inventory.production_denylist_digest")

    resources = inventory["resources"]
    if not isinstance(resources, list):
        errors.append("resource_inventory.resources")
    else:
        seen: set[str] = set()
        for item in resources:
            resource = _mapping(
                item,
                expected_keys=_RESOURCE_KEYS,
                code="resource.schema",
                errors=errors,
            )
            if resource is None:
                continue
            resource_id = resource["resource_id"]
            if (
                not isinstance(resource_id, str)
                or resource_id not in RESOURCE_KINDS
                or resource_id in seen
            ):
                errors.append("resource.id")
                continue
            seen.add(resource_id)
            if resource["kind"] != RESOURCE_KINDS[resource_id]:
                errors.append(f"resource.{resource_id}.kind")
            if resource["selection_status"] != "selected_external_review_required":
                errors.append(f"resource.{resource_id}.selection_status")
            for key in _RESOURCE_REF_KEYS:
                if not _is_ref(resource[key]):
                    errors.append(f"resource.{resource_id}.{key}")
        if seen != set(RESOURCE_IDS) or len(resources) != len(RESOURCE_IDS):
            errors.append("resource_inventory.closed")

    dependencies = inventory["arm_dependencies"]
    if not isinstance(dependencies, Mapping) or set(dependencies) != set(
        ARM_DEPENDENCIES
    ):
        errors.append("arm_dependencies.closed")
    else:
        for arm, expected in ARM_DEPENDENCIES.items():
            actual = dependencies[arm]
            if not isinstance(actual, list) or tuple(actual) != expected:
                errors.append(f"arm_dependencies.{arm}")

    contracts = inventory["probe_contracts"]
    expected_contracts = {
        (arm, fact)
        for arm, facts in PROBE_FACTS_BY_ARM.items()
        for fact in facts
    }
    actual_contracts: set[tuple[str, str]] = set()
    if not isinstance(contracts, list):
        errors.append("probe_contracts.schema")
        return
    for item in contracts:
        contract = _mapping(
            item,
            expected_keys=_PROBE_CONTRACT_KEYS,
            code="probe_contract.schema",
            errors=errors,
        )
        if contract is None:
            continue
        arm = contract["arm"]
        fact = contract["fact"]
        if (
            not isinstance(arm, str)
            or arm not in ARM_DEPENDENCIES
            or not isinstance(fact, str)
            or fact not in PROBE_FACTS_BY_ARM.get(arm, ())
            or (arm, fact) in actual_contracts
        ):
            errors.append("probe_contract.identity")
            continue
        actual_contracts.add((arm, fact))
        if contract["status"] != "specified_external_review_required":
            errors.append(f"probe_contract.{arm}.{fact}.status")
        if not _is_digest(contract["synthetic_fixture_digest"]):
            errors.append(f"probe_contract.{arm}.{fact}.fixture")
        for key in ("capability_mapping_ref", "evidence_sink_ref"):
            if not _is_ref(contract[key]):
                errors.append(f"probe_contract.{arm}.{fact}.{key}")
        if contract["caller_audio_correlation_required"] is not (
            fact in CALLER_AUDIO_CORRELATED_FACTS
        ):
            errors.append(f"probe_contract.{arm}.{fact}.correlation_required")
        if not _is_ref(contract["correlation_contract_ref"]):
            errors.append(f"probe_contract.{arm}.{fact}.correlation_contract_ref")
    if actual_contracts != expected_contracts or len(contracts) != len(
        expected_contracts
    ):
        errors.append("probe_contracts.closed")


def _validate_privacy_attestations(
    value: object,
    *,
    errors: list[str],
) -> None:
    privacy = _mapping(
        value,
        expected_keys=_PRIVACY_TOP_LEVEL_KEYS,
        code="privacy.schema",
        errors=errors,
    )
    if privacy is None:
        return
    if privacy["schema_version"] != 1:
        errors.append("privacy.version")
    if privacy["status"] != "attested_external_review_required":
        errors.append("privacy.status")
    attestations = privacy["attestations"]
    if not isinstance(attestations, list):
        errors.append("privacy.attestations")
        return
    seen: set[str] = set()
    for item in attestations:
        attestation = _mapping(
            item,
            expected_keys=_ATTESTATION_KEYS,
            code="attestation.schema",
            errors=errors,
        )
        if attestation is None:
            continue
        resource_id = attestation["resource_id"]
        if (
            not isinstance(resource_id, str)
            or resource_id not in RESOURCE_KINDS
            or resource_id in seen
        ):
            errors.append("attestation.resource_id")
            continue
        seen.add(resource_id)
        if attestation["status"] != "attested_external_review_required":
            errors.append(f"attestation.{resource_id}.status")
        for key in (
            "provider_api_ref",
            "account_region_ref",
            "evidence_ref",
            "owner_ref",
            "residue_verification_ref",
            "deletion_procedure_ref",
        ):
            if not _is_ref(attestation[key]):
                errors.append(f"attestation.{resource_id}.{key}")
        evidence_source_class = attestation["evidence_source_class"]
        if (
            not isinstance(evidence_source_class, str)
            or evidence_source_class
            not in {
                "control_plane_api",
                "signed_configuration_export",
                "signed_provider_evidence",
            }
        ):
            errors.append(f"attestation.{resource_id}.evidence_source_class")
        if not _is_digest(attestation["evidence_digest"]):
            errors.append(f"attestation.{resource_id}.evidence_digest")
        observed = attestation["observed_at_ms"]
        expires = attestation["expires_at_ms"]
        if (
            isinstance(observed, bool)
            or not isinstance(observed, int)
            or isinstance(expires, bool)
            or not isinstance(expires, int)
            or observed < 1
            or observed >= expires
        ):
            errors.append(f"attestation.{resource_id}.timestamps")
        settings = attestation["settings"]
        if not isinstance(settings, Mapping) or set(settings) != set(
            PRIVACY_SETTINGS
        ):
            errors.append(f"attestation.{resource_id}.settings")
            continue
        for setting_name, required_state in PRIVACY_SETTINGS.items():
            setting = _mapping(
                settings[setting_name],
                expected_keys=_SETTING_KEYS,
                code=f"attestation.{resource_id}.{setting_name}.schema",
                errors=errors,
            )
            if setting is None:
                continue
            if setting["state"] != required_state:
                errors.append(f"attestation.{resource_id}.{setting_name}.state")
            if not _is_ref(setting["evidence_ref"]):
                errors.append(
                    f"attestation.{resource_id}.{setting_name}.evidence_ref"
                )
            if not _is_digest(setting["evidence_digest"]):
                errors.append(
                    f"attestation.{resource_id}.{setting_name}.evidence_digest"
                )
    if seen != set(RESOURCE_IDS) or len(attestations) != len(RESOURCE_IDS):
        errors.append("privacy.attestations.closed")


def _validate_signature_envelope(
    value: object,
    *,
    resource_inventory: object,
    privacy_attestations: object,
    errors: list[str],
) -> None:
    envelope = _mapping(
        value,
        expected_keys=_SIGNATURE_ENVELOPE_KEYS,
        code="signature_envelope.schema",
        errors=errors,
    )
    if envelope is None:
        return
    if envelope["schema_version"] != 1:
        errors.append("signature_envelope.version")
    if envelope["status"] != "signed_external_review_required":
        errors.append("signature_envelope.status")
    if envelope["execution_supported"] is not False:
        errors.append("signature_envelope.execution_supported")
    if envelope["evidence_tier"] != "bounded_connected_probe":
        errors.append("signature_envelope.evidence_tier")
    arm = envelope["arm"]
    if not isinstance(arm, str) or arm not in ARM_DEPENDENCIES:
        errors.append("signature_envelope.arm")
    if not _is_digest(envelope["source_sha"], length=40):
        errors.append("signature_envelope.source_sha")
    if not _is_digest(envelope["manifest_digest"]):
        errors.append("signature_envelope.manifest_digest")
    if (
        not _is_digest(envelope["resource_inventory_digest"])
        or envelope["resource_inventory_digest"]
        != canonical_digest(resource_inventory)
    ):
        errors.append("signature_envelope.resource_inventory_digest")
    if (
        not _is_digest(envelope["privacy_attestations_digest"])
        or envelope["privacy_attestations_digest"]
        != canonical_digest(privacy_attestations)
    ):
        errors.append("signature_envelope.privacy_attestations_digest")
    contracts = (
        resource_inventory.get("probe_contracts")
        if isinstance(resource_inventory, Mapping)
        else None
    )
    if (
        not _is_digest(envelope["capability_contract_digest"])
        or envelope["capability_contract_digest"] != canonical_digest(contracts)
    ):
        errors.append("signature_envelope.capability_contract_digest")
    for key in (
        "approved_pstn_source_digest",
        "approved_pstn_destination_digest",
        "nonce_consumption_policy_digest",
        "active_execution_binding_digest",
        "findings_digest",
    ):
        if not _is_digest(envelope[key]):
            errors.append(f"signature_envelope.{key}")
    for key in (
        "evidence_kms_key_version_ref",
        "nonce_consumption_record_ref",
    ):
        if not _is_ref(envelope[key]):
            errors.append(f"signature_envelope.{key}")
    if (
        isinstance(envelope["unresolved_p1_count"], bool)
        or envelope["unresolved_p1_count"] != 0
    ):
        errors.append("signature_envelope.unresolved_p1_count")

    artifacts = envelope["artifact_digests"]
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(
        ARTIFACT_DIGEST_KEYS
    ):
        errors.append("signature_envelope.artifact_digests")
    elif any(not _is_digest(artifacts[key]) for key in ARTIFACT_DIGEST_KEYS):
        errors.append("signature_envelope.artifact_digest_value")

    caps = envelope["caps"]
    if not isinstance(caps, Mapping) or set(caps) != set(CAP_KEYS):
        errors.append("signature_envelope.caps")
    elif any(
        isinstance(caps[key], bool)
        or not isinstance(caps[key], int)
        or caps[key] < 1
        for key in CAP_KEYS
    ):
        errors.append("signature_envelope.cap_value")

    disabled = envelope["disabled_features"]
    if (
        not isinstance(disabled, Mapping)
        or set(disabled) != set(DISABLED_FEATURE_KEYS)
        or any(disabled[key] is not True for key in DISABLED_FEATURE_KEYS)
    ):
        errors.append("signature_envelope.disabled_features")

    custody = _mapping(
        envelope["custody"],
        expected_keys=_CUSTODY_KEYS,
        code="signature_envelope.custody",
        errors=errors,
    )
    if custody is not None and any(not _is_ref(value) for value in custody.values()):
        errors.append("signature_envelope.custody_ref")

    trust_store = _mapping(
        envelope["trust_store"],
        expected_keys=_TRUST_STORE_KEYS,
        code="signature_envelope.trust_store",
        errors=errors,
    )
    if trust_store is not None:
        if trust_store["algorithm"] != "ed25519":
            errors.append("signature_envelope.algorithm")
        if (
            trust_store["no_self_approval"] is not True
            or trust_store["no_break_glass"] is not True
        ):
            errors.append("signature_envelope.trust_policy")
        for key in (
            "version_ref",
            "rotation_status_ref",
            "revocation_status_ref",
            "immutable_custody_ref",
        ):
            if not _is_ref(trust_store[key]):
                errors.append(f"signature_envelope.trust_store.{key}")
        if not _is_digest(trust_store["policy_digest"]):
            errors.append("signature_envelope.trust_store.policy_digest")

    for key in ("approval_id", "nonce"):
        if not _is_ref(envelope[key]):
            errors.append(f"signature_envelope.{key}")
    issued = envelope["issued_at_ms"]
    expires = envelope["expires_at_ms"]
    if (
        isinstance(issued, bool)
        or not isinstance(issued, int)
        or isinstance(expires, bool)
        or not isinstance(expires, int)
        or issued < 1
        or issued >= expires
    ):
        errors.append("signature_envelope.timestamps")

    signers = envelope["signers"]
    identities: list[object] = []
    key_ids: list[object] = []
    public_key_refs: list[object] = []
    roles: set[object] = set()
    if not isinstance(signers, list) or len(signers) != len(SIGNER_ROLES):
        errors.append("signature_envelope.signers")
    else:
        for item in signers:
            signer = _mapping(
                item,
                expected_keys=_SIGNER_KEYS,
                code="signer.schema",
                errors=errors,
            )
            if signer is None:
                continue
            role = signer["role"]
            identity = signer["identity_ref"]
            if isinstance(role, str):
                roles.add(role)
            if isinstance(identity, str):
                identities.append(identity)
            if isinstance(signer["key_id"], str):
                key_ids.append(signer["key_id"])
            if isinstance(signer["public_key_ref"], str):
                public_key_refs.append(signer["public_key_ref"])
            for key in (
                "identity_ref",
                "key_id",
                "public_key_ref",
                "revocation_status_ref",
                "detached_signature_ref",
            ):
                if not _is_ref(signer[key]):
                    errors.append(f"signer.{signer['role']}.{key}")
            if not _is_digest(signer["signature_digest"]):
                errors.append(f"signer.{signer['role']}.signature_digest")
            if signer["review_decision"] != "approved":
                errors.append(f"signer.{signer['role']}.review_decision")
        if roles != set(SIGNER_ROLES):
            errors.append("signature_envelope.signer_roles")
        if len(identities) != len(set(identities)):
            errors.append("signature_envelope.signer_identity_reuse")
        if len(key_ids) != len(set(key_ids)):
            errors.append("signature_envelope.signer_key_id_reuse")
        if len(public_key_refs) != len(set(public_key_refs)):
            errors.append("signature_envelope.signer_public_key_reuse")

    if (
        not _is_digest(envelope["envelope_self_digest"])
        or envelope["envelope_self_digest"] != signature_payload_digest(envelope)
    ):
        errors.append("signature_envelope.self_digest")


def validate_gate_package(package: object) -> PreparationResult:
    errors: list[str] = []
    root = _mapping(
        package,
        expected_keys=_TOP_LEVEL_KEYS,
        code="package.schema",
        errors=errors,
    )
    if root is None:
        return PreparationResult("preparation_incomplete", tuple(errors))
    if root["package_version"] != 1:
        errors.append("package.version")
    if root["package_status"] != "review_ready_external_gates_required":
        errors.append("package.status")
    if root["execution_supported"] is not False:
        errors.append("package.execution_supported")
    _validate_resource_inventory(root["resource_inventory"], errors=errors)
    _validate_privacy_attestations(root["privacy_attestations"], errors=errors)
    _validate_signature_envelope(
        root["signature_envelope"],
        resource_inventory=root["resource_inventory"],
        privacy_attestations=root["privacy_attestations"],
        errors=errors,
    )
    if (
        not _is_digest(root["review_digest"])
        or root["review_digest"] != review_digest(root)
    ):
        errors.append("package.review_digest")
    return PreparationResult(
        (
            "preparation_incomplete"
            if errors
            else "preparation_complete_external_gates_required"
        ),
        tuple(sorted(set(errors))),
    )


__all__ = [
    "ARM_DEPENDENCIES",
    "ARTIFACT_DIGEST_KEYS",
    "B2_ONLY_PROBE_FACTS",
    "CAP_KEYS",
    "CALLER_AUDIO_CORRELATED_FACTS",
    "DISABLED_FEATURE_KEYS",
    "PRIVACY_SETTINGS",
    "PROBE_FACTS",
    "PROBE_FACTS_BY_ARM",
    "PreparationResult",
    "RESOURCE_IDS",
    "SIGNER_ROLES",
    "canonical_digest",
    "review_digest",
    "signature_payload_digest",
    "validate_gate_package",
]
