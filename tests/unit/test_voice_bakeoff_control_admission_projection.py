"""Tests for the non-authorizing control-assembly diagnostic projection."""

from __future__ import annotations

import ast
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from app.services.voice_bakeoff_control_admission_projection import (
    BlockedControlAssemblyDiagnostic,
    ControlAssemblyProjectionRequest,
    NonAuthorizingControlAssemblyProjector,
)
from app.services.voice_bakeoff_control_store_assembly import (
    assemble_control_stores,
)
from app.services.voice_bakeoff_execution_firewall_contracts import (
    ApprovedDependencyBinding,
    BrokerPolicy,
    CurrentFirewallPolicy,
    DeclaredProductionDenylist,
    DigestDomain,
    FirewallArm,
    FirewallDependency,
    NonproductionScope,
    SourcePinnedApprovalProjection,
    TypedDigest,
)
from app.services.voice_bakeoff_gate_contracts import BlockingGate, Task48GateReport


_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _ROOT / "app/services/voice_bakeoff_control_admission_projection.py"


def _digest(domain: DigestDomain, label: str) -> TypedDigest:
    return TypedDigest(
        domain=domain,
        value=hashlib.sha256(f"{domain.value}:{label}".encode()).hexdigest(),
    )


def _ordered(values: tuple[TypedDigest, ...]) -> tuple[TypedDigest, ...]:
    return tuple(sorted(values, key=lambda item: item.value))


def _scope() -> NonproductionScope:
    return NonproductionScope(
        project_digest=_digest(DigestDomain.NONPRODUCTION_PROJECT, "control"),
        region_digest=_digest(DigestDomain.NONPRODUCTION_REGION, "us-central1"),
    )


def _binding(dependency: FirewallDependency) -> ApprovedDependencyBinding:
    return ApprovedDependencyBinding(
        arm=FirewallArm.B1,
        dependency=dependency,
        destination_digests=_ordered(
            (
                _digest(DigestDomain.DESTINATION, f"{dependency.value}:one"),
                _digest(DigestDomain.DESTINATION, f"{dependency.value}:two"),
            )
        ),
        dependency_binding_digest=_digest(
            DigestDomain.DEPENDENCY_BINDING,
            dependency.value,
        ),
        credential_reference_digest=_digest(
            DigestDomain.CREDENTIAL_REFERENCE,
            dependency.value,
        ),
    )


def _bindings() -> tuple[ApprovedDependencyBinding, ...]:
    return tuple(
        sorted(
            (
                _binding(FirewallDependency.SPEECH_TO_TEXT),
                _binding(FirewallDependency.TELEPHONY),
                _binding(FirewallDependency.TEXT_GENERATION),
                _binding(FirewallDependency.TEXT_TO_SPEECH),
            ),
            key=lambda item: (item.arm.value, item.dependency.value),
        )
    )


def _policy(
    *,
    revoked_at_ms: int | None = None,
    denylisted_destination: TypedDigest | None = None,
) -> CurrentFirewallPolicy:
    production_destinations = (
        _digest(DigestDomain.DESTINATION, "production-a"),
        _digest(DigestDomain.DESTINATION, "production-b"),
    )
    if denylisted_destination is not None:
        production_destinations = (*production_destinations, denylisted_destination)
    denylist = DeclaredProductionDenylist.create(
        generation=2,
        effective_at_ms=1,
        expires_at_ms=100,
        revoked_at_ms=revoked_at_ms,
        production_destination_digests=_ordered(production_destinations),
        production_identity_digests=_ordered(
            (
                _digest(DigestDomain.IDENTITY, "production-a"),
                _digest(DigestDomain.IDENTITY, "production-b"),
            )
        ),
        external_inventory_attestation_digest=_digest(
            DigestDomain.EXTERNAL_INVENTORY_ATTESTATION,
            "inventory",
        ),
    )
    broker = BrokerPolicy.create(
        generation=3,
        effective_at_ms=1,
        expires_at_ms=90,
        revoked_at_ms=None,
        denylist_policy_digest=denylist.policy_digest,
        broker_identity_digest=_digest(DigestDomain.IDENTITY, "broker"),
        scope=_scope(),
        permitted_dependencies=_bindings(),
        metadata_grant_ttl_ms=20,
    )
    return CurrentFirewallPolicy(
        denylist=denylist,
        broker_policy=broker,
        minimum_denylist_generation=2,
        minimum_broker_generation=3,
        expected_denylist_policy_digest=denylist.policy_digest,
        expected_broker_policy_digest=broker.policy_digest,
    )


def _projection(current: CurrentFirewallPolicy) -> SourcePinnedApprovalProjection:
    return SourcePinnedApprovalProjection.create(
        approval_id_digest=_digest(DigestDomain.APPROVAL_ID, "approval"),
        source_tree_digest=_digest(DigestDomain.SOURCE_TREE, "source"),
        dependency_inventory_digest=_digest(
            DigestDomain.DEPENDENCY_INVENTORY,
            "dependencies",
        ),
        arm=FirewallArm.B1,
        dependencies=_bindings(),
        denylist_policy_digest=current.denylist.policy_digest,
        broker_policy_digest=current.broker_policy.policy_digest,
        issued_at_ms=1,
        expires_at_ms=80,
    )


class _Verifier:
    def __init__(self, *, approval: object, projection: SourcePinnedApprovalProjection) -> None:
        self.approval = approval
        self.projection = projection
        self.calls = 0

    def verify(
        self,
        approval: object,
        *,
        now_ms: int,
    ) -> SourcePinnedApprovalProjection | None:
        self.calls += 1
        if approval is self.approval and self.projection.is_current(now_ms=now_ms):
            return self.projection
        return None


class _PolicyAuthority:
    def __init__(self, current: CurrentFirewallPolicy | None) -> None:
        self.current_policy = current
        self.calls = 0

    def current(self) -> CurrentFirewallPolicy | None:
        self.calls += 1
        return self.current_policy


def _report() -> Task48GateReport:
    return Task48GateReport(
        report_source_sha="a" * 40,
        package_source_binding="unbound_template",
        package_status="preparation_only",
        advisory_review_status="advisory_only",
        owner_approval_status="not_recorded",
        preauth_reference_status="reference_only_observed",
        execution_status="not_authorized",
        required_pre_network_controls=(
            "credential_resolution_must_remain_blocked",
            "networking_must_remain_blocked",
            "provider_and_pstn_must_remain_blocked",
        ),
        blocking_gates=(
            BlockingGate("complete_production_denylist", "deny production"),
            BlockingGate("one_use_runtime_envelope", "deny replay"),
        ),
    )


def _request(
    projection: SourcePinnedApprovalProjection,
) -> ControlAssemblyProjectionRequest:
    return ControlAssemblyProjectionRequest(
        expected_approval_binding_digest=projection.approval_binding_digest,
        expected_source_tree_digest=projection.source_tree_digest,
        expected_dependency_inventory_digest=projection.dependency_inventory_digest,
        control_scope=_scope(),
    )


def test_current_gate_returns_only_a_payload_safe_blocked_diagnostic() -> None:
    current = _policy()
    projection = _projection(current)
    approval = object()
    verifier = _Verifier(approval=approval, projection=projection)
    policy = _PolicyAuthority(current)

    diagnostic = NonAuthorizingControlAssemblyProjector(
        approval_verifier=verifier,
        policy_authority=policy,
    ).project_blocked(approval, _request(projection), _report(), now_ms=10)

    assert isinstance(diagnostic, BlockedControlAssemblyDiagnostic)
    assert diagnostic.execution_status == "not_authorized"
    assert diagnostic.valid_until_ms == 80
    assert diagnostic.blocking_gate_ids == (
        "complete_production_denylist",
        "one_use_runtime_envelope",
    )
    assert verifier.calls == policy.calls == 1
    serialized = repr(diagnostic)
    for prohibited in ("credential", "destination", "ref_", "hk-voice-bakeoff", "https://"):
        assert prohibited not in serialized
    with pytest.raises(ValueError, match="closed inputs"):
        assemble_control_stores(diagnostic)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong_source",
        "wrong_dependency_inventory",
        "wrong_approval_binding",
        "wrong_scope",
        "expired_projection",
        "revoked_policy",
        "denylisted_destination",
        "future_report",
    ),
)
def test_projector_fails_closed_on_binding_policy_and_gate_changes(mutation: str) -> None:
    current = _policy()
    projection = _projection(current)
    approval = object()
    request = _request(projection)
    report = _report()
    if mutation == "wrong_source":
        request = replace(
            request,
            expected_source_tree_digest=_digest(DigestDomain.SOURCE_TREE, "wrong"),
        )
    elif mutation == "wrong_dependency_inventory":
        request = replace(
            request,
            expected_dependency_inventory_digest=_digest(
                DigestDomain.DEPENDENCY_INVENTORY,
                "wrong",
            ),
        )
    elif mutation == "wrong_approval_binding":
        request = replace(
            request,
            expected_approval_binding_digest=_digest(
                DigestDomain.APPROVAL_BINDING,
                "wrong",
            ),
        )
    elif mutation == "wrong_scope":
        request = replace(
            request,
            control_scope=NonproductionScope(
                project_digest=_digest(DigestDomain.NONPRODUCTION_PROJECT, "wrong"),
                region_digest=_scope().region_digest,
            ),
        )
    elif mutation == "expired_projection":
        projection = replace(projection, expires_at_ms=10)
    elif mutation == "revoked_policy":
        current = _policy(revoked_at_ms=10)
        projection = _projection(current)
        request = _request(projection)
    elif mutation == "denylisted_destination":
        current = _policy(
            denylisted_destination=_bindings()[0].destination_digests[0],
        )
        projection = _projection(current)
        request = _request(projection)
    else:
        report = replace(report, execution_status="future_authorized_schema")

    assert (
        NonAuthorizingControlAssemblyProjector(
            approval_verifier=_Verifier(approval=approval, projection=projection),
            policy_authority=_PolicyAuthority(current),
        ).project_blocked(approval, request, report, now_ms=10)
        is None
    )


def test_projector_fails_closed_without_a_current_policy_or_matching_approval() -> None:
    current = _policy()
    projection = _projection(current)
    request = _request(projection)
    approval = object()
    assert (
        NonAuthorizingControlAssemblyProjector(
            approval_verifier=_Verifier(approval=object(), projection=projection),
            policy_authority=_PolicyAuthority(current),
        ).project_blocked(approval, request, _report(), now_ms=10)
        is None
    )
    assert (
        NonAuthorizingControlAssemblyProjector(
            approval_verifier=_Verifier(approval=approval, projection=projection),
            policy_authority=_PolicyAuthority(None),
        ).project_blocked(approval, request, _report(), now_ms=10)
        is None
    )


def test_projection_source_has_no_store_sdk_credential_or_runtime_imports() -> None:
    source = _SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imports == {
        "__future__",
        "voice_bakeoff_execution_firewall_contracts",
        "voice_bakeoff_gate_contracts",
    }
    assert {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } == {"dataclasses", "re"}
    assert not any(
        token in source
        for token in (
            "control_store_assembly",
            "firestore_transaction_port",
            "google_firestore_runner",
            "TransactionScope",
            "PreAuth",
            "voice_pipeline",
            "app.config",
            "app.main",
            "os.",
            "pathlib",
            "subprocess",
            "socket",
            "httpx",
            "requests",
        )
    )


def _local_service_import_closure(path: Path) -> set[Path]:
    pending = [path]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        tree = ast.parse(current.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level != 1 or not node.module:
                continue
            dependency = current.parent / f"{node.module}.py"
            if dependency.exists():
                pending.append(dependency)
    return visited


def test_projection_local_import_closure_excludes_preauth_and_runtime_paths() -> None:
    closure = _local_service_import_closure(_SOURCE)
    assert closure == {
        _ROOT / "app/services/voice_bakeoff_control_admission_projection.py",
        _ROOT / "app/services/voice_bakeoff_execution_firewall_contracts.py",
        _ROOT / "app/services/voice_bakeoff_gate_contracts.py",
    }
    forbidden = (
        "preauth",
        "firestore",
        "control_store_assembly",
        "credential",
        "voice_pipeline",
        "config",
        "network",
        "filesystem",
    )
    assert not any(token in path.name for path in closure for token in forbidden)
