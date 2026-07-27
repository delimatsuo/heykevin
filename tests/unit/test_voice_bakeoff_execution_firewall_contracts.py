"""Adversarial tests for the offline metadata-only execution firewall."""

from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from app.services.voice_bakeoff_execution_firewall_contracts import (
    ApprovedDependencyBinding,
    BrokerPolicy,
    CurrentFirewallPolicy,
    DeclaredProductionDenylist,
    DigestDomain,
    ExecutionFirewallResolver,
    FirewallArm,
    FirewallDependency,
    InMemoryCurrentFirewallPolicyAuthority,
    MetadataOnlyRequest,
    NonproductionScope,
    SourcePinnedApprovalProjection,
    TypedDigest,
)
from app.services.voice_bakeoff_gate_report import build_task_4_8_gate_report


_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_PATH = (
    _ROOT
    / "tests/fixtures/voice_architecture_bakeoff"
    / "task_4_8_gate_package.template.json"
)


def _digest(domain: DigestDomain, label: str) -> TypedDigest:
    return TypedDigest(
        domain=domain,
        value=hashlib.sha256(f"{domain.value}:{label}".encode()).hexdigest(),
    )


def _binding(
    arm: FirewallArm,
    dependency: FirewallDependency,
) -> ApprovedDependencyBinding:
    return ApprovedDependencyBinding(
        arm=arm,
        dependency=dependency,
        destination_digests=_ordered(
            (
                _digest(
                    DigestDomain.DESTINATION,
                    f"{arm.value}:{dependency.value}:workload",
                ),
                _digest(
                    DigestDomain.DESTINATION,
                    f"{arm.value}:{dependency.value}:control",
                ),
            )
        ),
        dependency_binding_digest=_digest(
            DigestDomain.DEPENDENCY_BINDING,
            f"{arm.value}:{dependency.value}",
        ),
        credential_reference_digest=_digest(
            DigestDomain.CREDENTIAL_REFERENCE,
            f"{arm.value}:{dependency.value}",
        ),
    )


def _ordered(values: tuple[TypedDigest, ...]) -> tuple[TypedDigest, ...]:
    return tuple(sorted(values, key=lambda item: item.value))


def _bindings_for(arm: FirewallArm) -> tuple[ApprovedDependencyBinding, ...]:
    dependency_sets = {
        FirewallArm.A: (
            FirewallDependency.NATIVE_VOICE,
            FirewallDependency.TELEPHONY,
        ),
        FirewallArm.B1: (
            FirewallDependency.SPEECH_TO_TEXT,
            FirewallDependency.TELEPHONY,
            FirewallDependency.TEXT_GENERATION,
            FirewallDependency.TEXT_TO_SPEECH,
        ),
        FirewallArm.B2: (
            FirewallDependency.CONVERSATION_RELAY,
            FirewallDependency.TELEPHONY,
            FirewallDependency.TEXT_GENERATION,
        ),
        FirewallArm.C: (
            FirewallDependency.NATIVE_VOICE,
            FirewallDependency.TELEPHONY,
        ),
    }
    return tuple(_binding(arm, dependency) for dependency in dependency_sets[arm])


def _all_bindings() -> tuple[ApprovedDependencyBinding, ...]:
    return tuple(
        sorted(
            (
                *(_bindings_for(FirewallArm.A)),
                *(_bindings_for(FirewallArm.B1)),
                *(_bindings_for(FirewallArm.B2)),
                *(_bindings_for(FirewallArm.C)),
            ),
            key=lambda item: (item.arm.value, item.dependency.value),
        )
    )


def _scope() -> NonproductionScope:
    return NonproductionScope(
        project_digest=_digest(DigestDomain.NONPRODUCTION_PROJECT, "project"),
        region_digest=_digest(DigestDomain.NONPRODUCTION_REGION, "region"),
    )


def _policies(
    *,
    denylist_revoked_at_ms: int | None = None,
    broker_revoked_at_ms: int | None = None,
    production_destination_digests: tuple[TypedDigest, ...] | None = None,
    production_identity_digests: tuple[TypedDigest, ...] | None = None,
) -> tuple[DeclaredProductionDenylist, BrokerPolicy, CurrentFirewallPolicy]:
    if production_destination_digests is None:
        production_destination_digests = _ordered(
            (
                _digest(DigestDomain.DESTINATION, "destination-a"),
                _digest(DigestDomain.DESTINATION, "destination-b"),
            )
        )
    if production_identity_digests is None:
        production_identity_digests = _ordered(
            (
                _digest(DigestDomain.IDENTITY, "identity-a"),
                _digest(DigestDomain.IDENTITY, "identity-b"),
            )
        )
    denylist = DeclaredProductionDenylist.create(
        generation=4,
        effective_at_ms=1,
        expires_at_ms=100,
        revoked_at_ms=denylist_revoked_at_ms,
        production_destination_digests=production_destination_digests,
        production_identity_digests=production_identity_digests,
        external_inventory_attestation_digest=_digest(
            DigestDomain.EXTERNAL_INVENTORY_ATTESTATION,
            "attestation",
        ),
    )
    broker = BrokerPolicy.create(
        generation=7,
        effective_at_ms=1,
        expires_at_ms=90,
        revoked_at_ms=broker_revoked_at_ms,
        denylist_policy_digest=denylist.policy_digest,
        broker_identity_digest=_digest(DigestDomain.IDENTITY, "broker"),
        scope=_scope(),
        permitted_dependencies=_all_bindings(),
        metadata_grant_ttl_ms=20,
    )
    return (
        denylist,
        broker,
        CurrentFirewallPolicy(
            denylist=denylist,
            broker_policy=broker,
            minimum_denylist_generation=4,
            minimum_broker_generation=7,
            expected_denylist_policy_digest=denylist.policy_digest,
            expected_broker_policy_digest=broker.policy_digest,
        ),
    )


def _projection(
    denylist: DeclaredProductionDenylist,
    broker: BrokerPolicy,
    *,
    arm: FirewallArm = FirewallArm.B1,
) -> SourcePinnedApprovalProjection:
    return SourcePinnedApprovalProjection.create(
        approval_id_digest=_digest(DigestDomain.APPROVAL_ID, "approval"),
        source_tree_digest=_digest(DigestDomain.SOURCE_TREE, "source"),
        dependency_inventory_digest=_digest(
            DigestDomain.DEPENDENCY_INVENTORY,
            "dependencies",
        ),
        arm=arm,
        dependencies=_bindings_for(arm),
        denylist_policy_digest=denylist.policy_digest,
        broker_policy_digest=broker.policy_digest,
        issued_at_ms=1,
        expires_at_ms=80,
    )


class _Verifier:
    def __init__(self, *, approval: object, projection: SourcePinnedApprovalProjection):
        self._approval = approval
        self._projection = projection

    def verify(
        self,
        approval: object,
        *,
        now_ms: int,
    ) -> SourcePinnedApprovalProjection | None:
        if approval is self._approval and self._projection.is_current(now_ms=now_ms):
            return self._projection
        return None


def _resolver_and_request() -> tuple[
    ExecutionFirewallResolver,
    object,
    MetadataOnlyRequest,
]:
    denylist, broker, current = _policies()
    projection = _projection(denylist, broker)
    approval = object()
    binding = next(
        item
        for item in projection.dependencies
        if item.dependency is FirewallDependency.SPEECH_TO_TEXT
    )
    return (
        ExecutionFirewallResolver(
            approval_verifier=_Verifier(approval=approval, projection=projection),
            policy_authority=InMemoryCurrentFirewallPolicyAuthority(current),
        ),
        approval,
        MetadataOnlyRequest(
            broker_identity_digest=broker.broker_identity_digest,
            scope=broker.scope,
            dependency=FirewallDependency.SPEECH_TO_TEXT,
            dependency_binding_digest=binding.dependency_binding_digest,
            credential_reference_digest=binding.credential_reference_digest,
            requested_expires_at_ms=25,
        ),
    )


def test_metadata_only_grant_is_exactly_bound_and_repeatable() -> None:
    resolver, approval, request = _resolver_and_request()

    first = resolver.resolve_metadata(approval, request, now_ms=10)
    second = resolver.resolve_metadata(approval, request, now_ms=10)

    assert first is not None
    assert second == first
    assert first.expires_at_ms == 25
    assert first.dependency is FirewallDependency.SPEECH_TO_TEXT
    assert first.destination_digests == _binding(
        FirewallArm.B1,
        FirewallDependency.SPEECH_TO_TEXT,
    ).destination_digests
    assert first.dependency_binding_digest == request.dependency_binding_digest
    serialized = repr(first) + json.dumps(first.unsigned_value(), sort_keys=True)
    for prohibited in (
        "credential_value",
        "access_token",
        "private_key",
        "https://",
        "ref_",
        "hk-voice-bakeoff",
    ):
        assert prohibited not in serialized


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_approval",
        "wrong_dependency",
        "wrong_dependency_binding",
        "swapped_dependency_binding",
        "wrong_credential",
        "wrong_broker",
        "wrong_scope",
        "overlong_expiry",
    ],
)
def test_resolver_rejects_swaps_and_overlong_metadata_windows(mutation: str) -> None:
    resolver, approval, request = _resolver_and_request()
    if mutation == "wrong_approval":
        approval = object()
    elif mutation == "wrong_dependency":
        request = replace(request, dependency=FirewallDependency.TEXT_GENERATION)
    elif mutation == "wrong_dependency_binding":
        request = replace(
            request,
            dependency_binding_digest=_digest(
                DigestDomain.DEPENDENCY_BINDING,
                "wrong",
            ),
        )
    elif mutation == "swapped_dependency_binding":
        request = replace(
            request,
            dependency_binding_digest=_binding(
                FirewallArm.B1,
                FirewallDependency.TEXT_GENERATION,
            ).dependency_binding_digest,
        )
    elif mutation == "wrong_credential":
        request = replace(
            request,
            credential_reference_digest=_digest(
                DigestDomain.CREDENTIAL_REFERENCE,
                "wrong",
            ),
        )
    elif mutation == "wrong_broker":
        request = replace(
            request,
            broker_identity_digest=_digest(DigestDomain.IDENTITY, "wrong"),
        )
    elif mutation == "wrong_scope":
        request = replace(
            request,
            scope=NonproductionScope(
                project_digest=_digest(DigestDomain.NONPRODUCTION_PROJECT, "wrong"),
                region_digest=_scope().region_digest,
            ),
        )
    else:
        request = replace(request, requested_expires_at_ms=31)

    assert resolver.resolve_metadata(approval, request, now_ms=10) is None


@pytest.mark.parametrize(
    ("denylist_revoked_at_ms", "broker_revoked_at_ms"),
    [(10, None), (None, 10)],
)
def test_resolver_rejects_revoked_current_policy(
    denylist_revoked_at_ms: int | None,
    broker_revoked_at_ms: int | None,
) -> None:
    denylist, broker, current = _policies(
        denylist_revoked_at_ms=denylist_revoked_at_ms,
        broker_revoked_at_ms=broker_revoked_at_ms,
    )
    projection = _projection(denylist, broker)
    approval = object()
    binding = projection.dependencies[0]
    resolver = ExecutionFirewallResolver(
        approval_verifier=_Verifier(approval=approval, projection=projection),
        policy_authority=InMemoryCurrentFirewallPolicyAuthority(current),
    )
    request = MetadataOnlyRequest(
        broker_identity_digest=broker.broker_identity_digest,
        scope=broker.scope,
        dependency=binding.dependency,
        dependency_binding_digest=binding.dependency_binding_digest,
        credential_reference_digest=binding.credential_reference_digest,
        requested_expires_at_ms=20,
    )

    assert resolver.resolve_metadata(approval, request, now_ms=10) is None


@pytest.mark.parametrize("denylisted_member", ["broker", "destination"])
def test_resolver_rejects_exact_denylisted_broker_or_destination(
    denylisted_member: str,
) -> None:
    binding = _binding(FirewallArm.B1, FirewallDependency.SPEECH_TO_TEXT)
    policy_kwargs: dict[str, tuple[TypedDigest, ...]] = {}
    if denylisted_member == "broker":
        policy_kwargs["production_identity_digests"] = (
            _digest(DigestDomain.IDENTITY, "broker"),
        )
    else:
        policy_kwargs["production_destination_digests"] = (
            binding.destination_digests[1],
        )
    denylist, broker, current = _policies(**policy_kwargs)
    projection = _projection(denylist, broker)
    approval = object()
    resolver = ExecutionFirewallResolver(
        approval_verifier=_Verifier(approval=approval, projection=projection),
        policy_authority=InMemoryCurrentFirewallPolicyAuthority(current),
    )
    request = MetadataOnlyRequest(
        broker_identity_digest=broker.broker_identity_digest,
        scope=broker.scope,
        dependency=binding.dependency,
        dependency_binding_digest=binding.dependency_binding_digest,
        credential_reference_digest=binding.credential_reference_digest,
        requested_expires_at_ms=20,
    )

    assert resolver.resolve_metadata(approval, request, now_ms=10) is None


def test_typed_digests_and_closed_policy_shapes_reject_raw_or_stale_values() -> None:
    with pytest.raises(ValueError):
        TypedDigest(DigestDomain.CREDENTIAL_REFERENCE, "ref_CREDENTIAL")
    with pytest.raises(ValueError):
        NonproductionScope(
            project_digest="hk-voice-bakeoff-preauth-iso",  # type: ignore[arg-type]
            region_digest=_scope().region_digest,
        )

    denylist, broker, current = _policies()
    with pytest.raises(ValueError, match="digest does not match"):
        replace(broker, metadata_grant_ttl_ms=21)
    with pytest.raises(ValueError, match="generation rolled back"):
        CurrentFirewallPolicy(
            denylist=denylist,
            broker_policy=broker,
            minimum_denylist_generation=5,
            minimum_broker_generation=7,
            expected_denylist_policy_digest=denylist.policy_digest,
            expected_broker_policy_digest=broker.policy_digest,
        )
    with pytest.raises(ValueError, match="policy digest changed"):
        replace(
            current,
            expected_broker_policy_digest=_digest(
                DigestDomain.BROKER_POLICY,
                "unexpected",
            ),
        )


def test_projection_cannot_omit_or_mutate_a_source_pinned_dependency_binding() -> None:
    denylist, broker, _ = _policies()
    projection = _projection(denylist, broker)

    with pytest.raises(ValueError, match="must bind every dependency"):
        SourcePinnedApprovalProjection.create(
            approval_id_digest=projection.approval_id_digest,
            source_tree_digest=projection.source_tree_digest,
            dependency_inventory_digest=projection.dependency_inventory_digest,
            arm=projection.arm,
            dependencies=projection.dependencies[:-1],
            denylist_policy_digest=projection.denylist_policy_digest,
            broker_policy_digest=projection.broker_policy_digest,
            issued_at_ms=projection.issued_at_ms,
            expires_at_ms=projection.expires_at_ms,
        )
    with pytest.raises(ValueError, match="not source-pinned"):
        replace(
            projection,
            approval_binding_digest=_digest(DigestDomain.APPROVAL_BINDING, "forged"),
        )


def test_resolver_never_relaxes_task_4_8_gate_report() -> None:
    package = json.loads(_PACKAGE_PATH.read_text(encoding="utf-8"))
    report = build_task_4_8_gate_report(package=package, source_sha="a" * 40)

    assert report.execution_status == "not_authorized"
    assert {
        "identity_and_credential_broker",
        "complete_production_denylist",
    } <= {gate.gate_id for gate in report.blocking_gates}


def test_resolver_fails_closed_without_current_policy() -> None:
    denylist, broker, _ = _policies()
    projection = _projection(denylist, broker)
    approval = object()
    binding = projection.dependencies[0]
    request = MetadataOnlyRequest(
        broker_identity_digest=broker.broker_identity_digest,
        scope=broker.scope,
        dependency=binding.dependency,
        dependency_binding_digest=binding.dependency_binding_digest,
        credential_reference_digest=binding.credential_reference_digest,
        requested_expires_at_ms=20,
    )
    resolver = ExecutionFirewallResolver(
        approval_verifier=_Verifier(approval=approval, projection=projection),
        policy_authority=InMemoryCurrentFirewallPolicyAuthority(None),
    )

    assert resolver.resolve_metadata(approval, request, now_ms=10) is None


@pytest.mark.parametrize("now_ms", [0, 90, 100])
def test_resolver_fails_closed_for_noncurrent_policy(now_ms: int) -> None:
    resolver, approval, request = _resolver_and_request()

    assert resolver.resolve_metadata(approval, request, now_ms=now_ms) is None


def test_module_is_stdlib_only_and_unreachable_from_runtime_paths() -> None:
    module_path = _ROOT / "app/services/voice_bakeoff_execution_firewall_contracts.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        (node.module or "", alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    } | {
        ("", alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imports == {
        ("__future__", "annotations"),
        ("", "dataclasses"),
        ("", "enum"),
        ("", "hashlib"),
        ("", "json"),
        ("typing", "Protocol"),
    }
    forbidden_calls = {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "import_module",
        "getenv",
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "run",
        "Popen",
    }
    assert not {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } & forbidden_calls
    for runtime_path in (
        _ROOT / "app/main.py",
        _ROOT / "app/experiments/voice_bakeoff_app.py",
        _ROOT / "scripts/run_voice_architecture_bakeoff.py",
        _ROOT / "app/services/voice_bakeoff_gate_report.py",
    ):
        runtime_source = runtime_path.read_text(encoding="utf-8")
        runtime_tree = ast.parse(runtime_source)
        import_from_targets = {
            (
                node.module or "",
                alias.name,
            )
            for node in ast.walk(runtime_tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        } | {
            ("", alias.name)
            for node in ast.walk(runtime_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert (
            "",
            "app.services.voice_bakeoff_execution_firewall_contracts",
        ) not in import_from_targets
        assert (
            "app.services",
            "voice_bakeoff_execution_firewall_contracts",
        ) not in import_from_targets
        assert "voice_bakeoff_execution_firewall_contracts" not in runtime_source
