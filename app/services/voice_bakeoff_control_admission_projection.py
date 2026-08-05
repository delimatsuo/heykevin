"""Non-authorizing metadata projection for the sealed control-store boundary.

This source-only module composes existing source-pinned approval and current
firewall-policy seams. Its only successful result is an explicitly blocked,
payload-safe diagnostic for the current Task 4.8 report. It never constructs
control stores, handles, runners, clients, transactions, credentials, nonce
state, pre-auth state, or provider dependencies.

A future authorization schema must be separately designed. It cannot be
substituted for this fixed ``not_authorized`` diagnostic contract.
"""

from __future__ import annotations

import dataclasses
import re

from .voice_bakeoff_execution_firewall_contracts import (
    CurrentFirewallPolicy,
    CurrentFirewallPolicyAuthority,
    DigestDomain,
    NonproductionScope,
    SourcePinnedApprovalProjection,
    SourcePinnedApprovalVerifier,
    TypedDigest,
)
from .voice_bakeoff_gate_contracts import Task48GateReport


_SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _is_exact_int(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _has_domain(value: object, domain: DigestDomain) -> bool:
    return isinstance(value, TypedDigest) and value.domain is domain


@dataclasses.dataclass(frozen=True, slots=True)
class ControlAssemblyProjectionRequest:
    """Expected metadata bindings; it contains neither authority nor credentials."""

    expected_approval_binding_digest: TypedDigest
    expected_source_tree_digest: TypedDigest
    expected_dependency_inventory_digest: TypedDigest
    control_scope: NonproductionScope

    def __post_init__(self) -> None:
        if (
            not _has_domain(
                self.expected_approval_binding_digest,
                DigestDomain.APPROVAL_BINDING,
            )
            or not _has_domain(
                self.expected_source_tree_digest,
                DigestDomain.SOURCE_TREE,
            )
            or not _has_domain(
                self.expected_dependency_inventory_digest,
                DigestDomain.DEPENDENCY_INVENTORY,
            )
            or not isinstance(self.control_scope, NonproductionScope)
        ):
            raise ValueError("control assembly projection request is invalid")


@dataclasses.dataclass(frozen=True, slots=True)
class BlockedControlAssemblyDiagnostic:
    """Payload-safe proof that the current gate blocks control-store assembly."""

    schema_version: int
    execution_status: str
    report_source_sha: str
    approval_id_digest: TypedDigest
    approval_binding_digest: TypedDigest
    source_tree_digest: TypedDigest
    dependency_inventory_digest: TypedDigest
    denylist_policy_digest: TypedDigest
    broker_policy_digest: TypedDigest
    control_scope: NonproductionScope
    observed_at_ms: int
    valid_until_ms: int
    blocking_gate_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not _is_exact_int(self.schema_version, minimum=1)
            or self.schema_version != 1
            or self.execution_status != "not_authorized"
            or not isinstance(self.report_source_sha, str)
            or _SOURCE_SHA.fullmatch(self.report_source_sha) is None
            or not _has_domain(self.approval_id_digest, DigestDomain.APPROVAL_ID)
            or not _has_domain(
                self.approval_binding_digest,
                DigestDomain.APPROVAL_BINDING,
            )
            or not _has_domain(self.source_tree_digest, DigestDomain.SOURCE_TREE)
            or not _has_domain(
                self.dependency_inventory_digest,
                DigestDomain.DEPENDENCY_INVENTORY,
            )
            or not _has_domain(
                self.denylist_policy_digest,
                DigestDomain.DENYLIST_POLICY,
            )
            or not _has_domain(
                self.broker_policy_digest,
                DigestDomain.BROKER_POLICY,
            )
            or not isinstance(self.control_scope, NonproductionScope)
            or not _is_exact_int(self.observed_at_ms, minimum=1)
            or not _is_exact_int(self.valid_until_ms, minimum=1)
            or self.valid_until_ms <= self.observed_at_ms
            or not self.blocking_gate_ids
            or any(
                not isinstance(gate_id, str)
                or not gate_id
                or len(gate_id) > 128
                for gate_id in self.blocking_gate_ids
            )
            or self.blocking_gate_ids
            != tuple(sorted(set(self.blocking_gate_ids)))
        ):
            raise ValueError("blocked control assembly diagnostic is invalid")


class NonAuthorizingControlAssemblyProjector:
    """Projects only the currently sealed block; it has no admission path."""

    def __init__(
        self,
        *,
        approval_verifier: SourcePinnedApprovalVerifier,
        policy_authority: CurrentFirewallPolicyAuthority,
    ) -> None:
        self._approval_verifier = approval_verifier
        self._policy_authority = policy_authority

    def project_blocked(
        self,
        approval: object,
        request: ControlAssemblyProjectionRequest,
        report: Task48GateReport,
        *,
        now_ms: int,
    ) -> BlockedControlAssemblyDiagnostic | None:
        """Return a diagnostic only when the current sealed report blocks assembly."""

        if (
            not isinstance(request, ControlAssemblyProjectionRequest)
            or type(report) is not Task48GateReport
            or not _is_exact_int(now_ms, minimum=1)
            or report.execution_status != "not_authorized"
            or not report.blocking_gates
        ):
            return None
        try:
            projection = self._approval_verifier.verify(approval, now_ms=now_ms)
            current = self._policy_authority.current()
        except Exception:
            return None
        if (
            not isinstance(projection, SourcePinnedApprovalProjection)
            or not isinstance(current, CurrentFirewallPolicy)
            or not projection.is_current(now_ms=now_ms)
            or not current.denylist.is_current(now_ms=now_ms)
            or not current.broker_policy.is_current(now_ms=now_ms)
            or projection.approval_binding_digest
            != request.expected_approval_binding_digest
            or projection.source_tree_digest != request.expected_source_tree_digest
            or projection.dependency_inventory_digest
            != request.expected_dependency_inventory_digest
            or current.broker_policy.scope != request.control_scope
            or projection.denylist_policy_digest != current.denylist.policy_digest
            or projection.broker_policy_digest != current.broker_policy.policy_digest
        ):
            return None
        permitted_for_arm = tuple(
            binding
            for binding in current.broker_policy.permitted_dependencies
            if binding.arm is projection.arm
        )
        if (
            projection.dependencies != permitted_for_arm
            or any(
                destination in current.denylist.production_destination_digests
                for binding in projection.dependencies
                for destination in binding.destination_digests
            )
        ):
            return None
        valid_until_ms = min(
            projection.expires_at_ms,
            current.denylist.expires_at_ms,
            current.broker_policy.expires_at_ms,
        )
        if valid_until_ms <= now_ms:
            return None
        return BlockedControlAssemblyDiagnostic(
            schema_version=1,
            execution_status="not_authorized",
            report_source_sha=report.report_source_sha,
            approval_id_digest=projection.approval_id_digest,
            approval_binding_digest=projection.approval_binding_digest,
            source_tree_digest=projection.source_tree_digest,
            dependency_inventory_digest=projection.dependency_inventory_digest,
            denylist_policy_digest=current.denylist.policy_digest,
            broker_policy_digest=current.broker_policy.policy_digest,
            control_scope=request.control_scope,
            observed_at_ms=now_ms,
            valid_until_ms=valid_until_ms,
            blocking_gate_ids=tuple(
                sorted({gate.gate_id for gate in report.blocking_gates})
            ),
        )
