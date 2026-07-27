"""Offline contracts for the future bakeoff execution firewall.

The types in this module model a declared production denylist and a
metadata-only credential-broker policy.  They do not read configuration, resolve
credentials, access cloud services, contact providers, or authorize execution.
An external adapter must still prove independent nonproduction identity and
production isolation before the Task 4.8 gate can move.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from typing import Protocol


_HEX_DIGEST_LENGTH = 64


class FirewallArm(str, enum.Enum):
    """Closed bakeoff arm identifiers for metadata-only policy binding."""

    A = "A"
    B1 = "B1"
    B2 = "B2"
    C = "C"


class FirewallDependency(str, enum.Enum):
    """Provider-neutral external dependency slots."""

    TELEPHONY = "telephony"
    NATIVE_VOICE = "native_voice"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_GENERATION = "text_generation"
    TEXT_TO_SPEECH = "text_to_speech"
    CONVERSATION_RELAY = "conversation_relay"


class DigestDomain(str, enum.Enum):
    """Domains that prevent digest substitution across firewall fields."""

    APPROVAL_ID = "approval_id"
    APPROVAL_BINDING = "approval_binding"
    SOURCE_TREE = "source_tree"
    DEPENDENCY_INVENTORY = "dependency_inventory"
    DEPENDENCY_BINDING = "dependency_binding"
    CREDENTIAL_REFERENCE = "credential_reference"
    DESTINATION = "destination"
    IDENTITY = "identity"
    EXTERNAL_INVENTORY_ATTESTATION = "external_inventory_attestation"
    DENYLIST_POLICY = "denylist_policy"
    BROKER_POLICY = "broker_policy"
    NONPRODUCTION_PROJECT = "nonproduction_project"
    NONPRODUCTION_REGION = "nonproduction_region"
    METADATA_GRANT = "metadata_grant"


_ARM_DEPENDENCIES: dict[FirewallArm, tuple[FirewallDependency, ...]] = {
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


def _require_exact_int(value: object, field_name: str, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")


def _require_window(
    issued_at_ms: object,
    expires_at_ms: object,
    *,
    field_name: str,
) -> None:
    _require_exact_int(issued_at_ms, f"{field_name}.issued_at_ms", minimum=1)
    _require_exact_int(expires_at_ms, f"{field_name}.expires_at_ms", minimum=1)
    if issued_at_ms >= expires_at_ms:
        raise ValueError(f"{field_name} must be a bounded window")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


@dataclasses.dataclass(frozen=True, slots=True)
class TypedDigest:
    """A SHA-256 value scoped to one closed firewall domain."""

    domain: DigestDomain
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.domain, DigestDomain):
            raise ValueError("digest domain must be closed")
        if (
            not isinstance(self.value, str)
            or len(self.value) != _HEX_DIGEST_LENGTH
            or any(character not in "0123456789abcdef" for character in self.value)
        ):
            raise ValueError("digest value must be a lowercase SHA-256 value")

    def canonical_value(self) -> dict[str, str]:
        return {"domain": self.domain.value, "value": self.value}


def _require_typed_digest(
    value: object,
    domain: DigestDomain,
    field_name: str,
) -> TypedDigest:
    if not isinstance(value, TypedDigest) or value.domain is not domain:
        raise ValueError(f"{field_name} digest has the wrong domain")
    return value


def _typed_digest(domain: DigestDomain, value: object) -> TypedDigest:
    return TypedDigest(
        domain=domain,
        value=hashlib.sha256(
            domain.value.encode("ascii") + b"\x00" + _canonical_bytes(value)
        ).hexdigest(),
    )


@dataclasses.dataclass(frozen=True, slots=True)
class NonproductionScope:
    """Digest-only binding to one isolated project and region."""

    project_digest: TypedDigest
    region_digest: TypedDigest

    def __post_init__(self) -> None:
        _require_typed_digest(
            self.project_digest,
            DigestDomain.NONPRODUCTION_PROJECT,
            "scope project",
        )
        _require_typed_digest(
            self.region_digest,
            DigestDomain.NONPRODUCTION_REGION,
            "scope region",
        )

    def canonical_value(self) -> dict[str, object]:
        return {
            "project_digest": self.project_digest.canonical_value(),
            "region_digest": self.region_digest.canonical_value(),
        }


@dataclasses.dataclass(frozen=True, slots=True)
class ApprovedDependencyBinding:
    """One arm/dependency pair bound to a metadata-only credential reference."""

    arm: FirewallArm
    dependency: FirewallDependency
    destination_digests: tuple[TypedDigest, ...]
    dependency_binding_digest: TypedDigest
    credential_reference_digest: TypedDigest

    def __post_init__(self) -> None:
        if not isinstance(self.arm, FirewallArm):
            raise ValueError("binding arm must be closed")
        if not isinstance(self.dependency, FirewallDependency):
            raise ValueError("binding dependency must be closed")
        if self.dependency not in _ARM_DEPENDENCIES[self.arm]:
            raise ValueError("binding dependency is not valid for the arm")
        if (
            not self.destination_digests
            or any(
                not isinstance(item, TypedDigest)
                or item.domain is not DigestDomain.DESTINATION
                for item in self.destination_digests
            )
            or self.destination_digests
            != tuple(sorted(self.destination_digests, key=lambda item: item.value))
            or len({item.value for item in self.destination_digests})
            != len(self.destination_digests)
        ):
            raise ValueError(
                "binding destination digests must be nonempty, unique, and canonical"
            )
        _require_typed_digest(
            self.dependency_binding_digest,
            DigestDomain.DEPENDENCY_BINDING,
            "dependency binding",
        )
        _require_typed_digest(
            self.credential_reference_digest,
            DigestDomain.CREDENTIAL_REFERENCE,
            "credential reference",
        )

    def canonical_value(self) -> dict[str, object]:
        return {
            "arm": self.arm.value,
            "credential_reference_digest": self.credential_reference_digest.canonical_value(),
            "dependency": self.dependency.value,
            "dependency_binding_digest": self.dependency_binding_digest.canonical_value(),
            "destination_digests": [
                item.canonical_value() for item in self.destination_digests
            ],
        }


def _ordered_unique_bindings(
    bindings: tuple[ApprovedDependencyBinding, ...],
    *,
    require_single_arm: FirewallArm | None = None,
) -> None:
    if not bindings or any(not isinstance(item, ApprovedDependencyBinding) for item in bindings):
        raise ValueError("dependency bindings must be nonempty and closed")
    expected_order = tuple(
        sorted(
            bindings,
            key=lambda item: (item.arm.value, item.dependency.value),
        )
    )
    if bindings != expected_order:
        raise ValueError("dependency bindings must use canonical order")
    keys = {(item.arm, item.dependency) for item in bindings}
    if len(keys) != len(bindings):
        raise ValueError("dependency bindings must be unique")
    if require_single_arm is not None:
        if any(item.arm is not require_single_arm for item in bindings):
            raise ValueError("projection bindings must use exactly one arm")
        if tuple(item.dependency for item in bindings) != _ARM_DEPENDENCIES[require_single_arm]:
            raise ValueError("projection must bind every dependency for its arm")


@dataclasses.dataclass(frozen=True, slots=True)
class DeclaredProductionDenylist:
    """A committed offline snapshot, not proof of live inventory completeness."""

    schema_version: int
    generation: int
    effective_at_ms: int
    expires_at_ms: int
    revoked_at_ms: int | None
    production_destination_digests: tuple[TypedDigest, ...]
    production_identity_digests: tuple[TypedDigest, ...]
    external_inventory_attestation_digest: TypedDigest
    policy_digest: TypedDigest

    def __post_init__(self) -> None:
        _require_exact_int(self.schema_version, "denylist.schema_version", minimum=1)
        if self.schema_version != 1:
            raise ValueError("denylist schema version is unsupported")
        _require_exact_int(self.generation, "denylist.generation", minimum=1)
        _require_window(
            self.effective_at_ms,
            self.expires_at_ms,
            field_name="denylist window",
        )
        if self.revoked_at_ms is not None:
            _require_exact_int(self.revoked_at_ms, "denylist.revoked_at_ms", minimum=1)
            if self.revoked_at_ms < self.effective_at_ms:
                raise ValueError("denylist revocation predates its effective window")
        for values, domain, field_name in (
            (
                self.production_destination_digests,
                DigestDomain.DESTINATION,
                "production destination digests",
            ),
            (
                self.production_identity_digests,
                DigestDomain.IDENTITY,
                "production identity digests",
            ),
        ):
            if not values or any(
                not isinstance(item, TypedDigest) or item.domain is not domain
                for item in values
            ):
                raise ValueError(f"{field_name} must be nonempty and domain-bound")
            if values != tuple(sorted(values, key=lambda item: item.value)):
                raise ValueError(f"{field_name} must use canonical order")
            if len({item.value for item in values}) != len(values):
                raise ValueError(f"{field_name} must be unique")
        _require_typed_digest(
            self.external_inventory_attestation_digest,
            DigestDomain.EXTERNAL_INVENTORY_ATTESTATION,
            "external inventory attestation",
        )
        _require_typed_digest(
            self.policy_digest,
            DigestDomain.DENYLIST_POLICY,
            "denylist policy",
        )
        if self.policy_digest != _typed_digest(
            DigestDomain.DENYLIST_POLICY,
            self.unsigned_value(),
        ):
            raise ValueError("denylist policy digest does not match")

    @classmethod
    def create(
        cls,
        *,
        generation: int,
        effective_at_ms: int,
        expires_at_ms: int,
        revoked_at_ms: int | None,
        production_destination_digests: tuple[TypedDigest, ...],
        production_identity_digests: tuple[TypedDigest, ...],
        external_inventory_attestation_digest: TypedDigest,
    ) -> DeclaredProductionDenylist:
        unsigned = {
            "effective_at_ms": effective_at_ms,
            "expires_at_ms": expires_at_ms,
            "external_inventory_attestation_digest": external_inventory_attestation_digest.canonical_value(),
            "generation": generation,
            "production_destination_digests": [
                item.canonical_value() for item in production_destination_digests
            ],
            "production_identity_digests": [
                item.canonical_value() for item in production_identity_digests
            ],
            "revoked_at_ms": revoked_at_ms,
            "schema_version": 1,
        }
        return cls(
            schema_version=1,
            generation=generation,
            effective_at_ms=effective_at_ms,
            expires_at_ms=expires_at_ms,
            revoked_at_ms=revoked_at_ms,
            production_destination_digests=production_destination_digests,
            production_identity_digests=production_identity_digests,
            external_inventory_attestation_digest=external_inventory_attestation_digest,
            policy_digest=_typed_digest(DigestDomain.DENYLIST_POLICY, unsigned),
        )

    def unsigned_value(self) -> dict[str, object]:
        return {
            "effective_at_ms": self.effective_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "external_inventory_attestation_digest": self.external_inventory_attestation_digest.canonical_value(),
            "generation": self.generation,
            "production_destination_digests": [
                item.canonical_value() for item in self.production_destination_digests
            ],
            "production_identity_digests": [
                item.canonical_value() for item in self.production_identity_digests
            ],
            "revoked_at_ms": self.revoked_at_ms,
            "schema_version": self.schema_version,
        }

    def is_current(self, *, now_ms: int) -> bool:
        return (
            type(now_ms) is int
            and self.effective_at_ms <= now_ms < self.expires_at_ms
            and (self.revoked_at_ms is None or now_ms < self.revoked_at_ms)
        )


@dataclasses.dataclass(frozen=True, slots=True)
class BrokerPolicy:
    """Metadata-only policy for one dedicated nonproduction broker identity."""

    schema_version: int
    generation: int
    effective_at_ms: int
    expires_at_ms: int
    revoked_at_ms: int | None
    denylist_policy_digest: TypedDigest
    broker_identity_digest: TypedDigest
    scope: NonproductionScope
    permitted_dependencies: tuple[ApprovedDependencyBinding, ...]
    metadata_grant_ttl_ms: int
    policy_digest: TypedDigest

    def __post_init__(self) -> None:
        _require_exact_int(self.schema_version, "broker.schema_version", minimum=1)
        if self.schema_version != 1:
            raise ValueError("broker schema version is unsupported")
        _require_exact_int(self.generation, "broker.generation", minimum=1)
        _require_window(
            self.effective_at_ms,
            self.expires_at_ms,
            field_name="broker window",
        )
        if self.revoked_at_ms is not None:
            _require_exact_int(self.revoked_at_ms, "broker.revoked_at_ms", minimum=1)
            if self.revoked_at_ms < self.effective_at_ms:
                raise ValueError("broker revocation predates its effective window")
        _require_typed_digest(
            self.denylist_policy_digest,
            DigestDomain.DENYLIST_POLICY,
            "broker denylist policy",
        )
        _require_typed_digest(
            self.broker_identity_digest,
            DigestDomain.IDENTITY,
            "broker identity",
        )
        if not isinstance(self.scope, NonproductionScope):
            raise ValueError("broker scope must be closed")
        _ordered_unique_bindings(self.permitted_dependencies)
        _require_exact_int(
            self.metadata_grant_ttl_ms,
            "broker.metadata_grant_ttl_ms",
            minimum=1,
        )
        _require_typed_digest(
            self.policy_digest,
            DigestDomain.BROKER_POLICY,
            "broker policy",
        )
        if self.policy_digest != _typed_digest(
            DigestDomain.BROKER_POLICY,
            self.unsigned_value(),
        ):
            raise ValueError("broker policy digest does not match")

    @classmethod
    def create(
        cls,
        *,
        generation: int,
        effective_at_ms: int,
        expires_at_ms: int,
        revoked_at_ms: int | None,
        denylist_policy_digest: TypedDigest,
        broker_identity_digest: TypedDigest,
        scope: NonproductionScope,
        permitted_dependencies: tuple[ApprovedDependencyBinding, ...],
        metadata_grant_ttl_ms: int,
    ) -> BrokerPolicy:
        unsigned = {
            "broker_identity_digest": broker_identity_digest.canonical_value(),
            "denylist_policy_digest": denylist_policy_digest.canonical_value(),
            "effective_at_ms": effective_at_ms,
            "expires_at_ms": expires_at_ms,
            "generation": generation,
            "metadata_grant_ttl_ms": metadata_grant_ttl_ms,
            "permitted_dependencies": [
                item.canonical_value() for item in permitted_dependencies
            ],
            "revoked_at_ms": revoked_at_ms,
            "schema_version": 1,
            "scope": scope.canonical_value(),
        }
        return cls(
            schema_version=1,
            generation=generation,
            effective_at_ms=effective_at_ms,
            expires_at_ms=expires_at_ms,
            revoked_at_ms=revoked_at_ms,
            denylist_policy_digest=denylist_policy_digest,
            broker_identity_digest=broker_identity_digest,
            scope=scope,
            permitted_dependencies=permitted_dependencies,
            metadata_grant_ttl_ms=metadata_grant_ttl_ms,
            policy_digest=_typed_digest(DigestDomain.BROKER_POLICY, unsigned),
        )

    def unsigned_value(self) -> dict[str, object]:
        return {
            "broker_identity_digest": self.broker_identity_digest.canonical_value(),
            "denylist_policy_digest": self.denylist_policy_digest.canonical_value(),
            "effective_at_ms": self.effective_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "generation": self.generation,
            "metadata_grant_ttl_ms": self.metadata_grant_ttl_ms,
            "permitted_dependencies": [
                item.canonical_value() for item in self.permitted_dependencies
            ],
            "revoked_at_ms": self.revoked_at_ms,
            "schema_version": self.schema_version,
            "scope": self.scope.canonical_value(),
        }

    def is_current(self, *, now_ms: int) -> bool:
        return (
            type(now_ms) is int
            and self.effective_at_ms <= now_ms < self.expires_at_ms
            and (self.revoked_at_ms is None or now_ms < self.revoked_at_ms)
        )


@dataclasses.dataclass(frozen=True, slots=True)
class SourcePinnedApprovalProjection:
    """Metadata projection emitted only by a provenance-aware verifier seam."""

    schema_version: int
    approval_id_digest: TypedDigest
    approval_binding_digest: TypedDigest
    source_tree_digest: TypedDigest
    dependency_inventory_digest: TypedDigest
    arm: FirewallArm
    dependencies: tuple[ApprovedDependencyBinding, ...]
    denylist_policy_digest: TypedDigest
    broker_policy_digest: TypedDigest
    issued_at_ms: int
    expires_at_ms: int

    def __post_init__(self) -> None:
        _require_exact_int(self.schema_version, "projection.schema_version", minimum=1)
        if self.schema_version != 1:
            raise ValueError("projection schema version is unsupported")
        for value, domain, field_name in (
            (self.approval_id_digest, DigestDomain.APPROVAL_ID, "approval id"),
            (
                self.approval_binding_digest,
                DigestDomain.APPROVAL_BINDING,
                "approval binding",
            ),
            (self.source_tree_digest, DigestDomain.SOURCE_TREE, "source tree"),
            (
                self.dependency_inventory_digest,
                DigestDomain.DEPENDENCY_INVENTORY,
                "dependency inventory",
            ),
            (
                self.denylist_policy_digest,
                DigestDomain.DENYLIST_POLICY,
                "denylist policy",
            ),
            (
                self.broker_policy_digest,
                DigestDomain.BROKER_POLICY,
                "broker policy",
            ),
        ):
            _require_typed_digest(value, domain, f"projection {field_name}")
        if not isinstance(self.arm, FirewallArm):
            raise ValueError("projection arm must be closed")
        _ordered_unique_bindings(self.dependencies, require_single_arm=self.arm)
        _require_window(
            self.issued_at_ms,
            self.expires_at_ms,
            field_name="projection window",
        )
        if self.approval_binding_digest != _typed_digest(
            DigestDomain.APPROVAL_BINDING,
            self.binding_value(),
        ):
            raise ValueError("projection binding is not source-pinned")

    @classmethod
    def create(
        cls,
        *,
        approval_id_digest: TypedDigest,
        source_tree_digest: TypedDigest,
        dependency_inventory_digest: TypedDigest,
        arm: FirewallArm,
        dependencies: tuple[ApprovedDependencyBinding, ...],
        denylist_policy_digest: TypedDigest,
        broker_policy_digest: TypedDigest,
        issued_at_ms: int,
        expires_at_ms: int,
    ) -> SourcePinnedApprovalProjection:
        binding_value = {
            "approval_id_digest": approval_id_digest.canonical_value(),
            "arm": arm.value,
            "broker_policy_digest": broker_policy_digest.canonical_value(),
            "dependencies": [item.canonical_value() for item in dependencies],
            "dependency_inventory_digest": dependency_inventory_digest.canonical_value(),
            "denylist_policy_digest": denylist_policy_digest.canonical_value(),
            "source_tree_digest": source_tree_digest.canonical_value(),
        }
        return cls(
            schema_version=1,
            approval_id_digest=approval_id_digest,
            approval_binding_digest=_typed_digest(
                DigestDomain.APPROVAL_BINDING,
                binding_value,
            ),
            source_tree_digest=source_tree_digest,
            dependency_inventory_digest=dependency_inventory_digest,
            arm=arm,
            dependencies=dependencies,
            denylist_policy_digest=denylist_policy_digest,
            broker_policy_digest=broker_policy_digest,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
        )

    def binding_value(self) -> dict[str, object]:
        return {
            "approval_id_digest": self.approval_id_digest.canonical_value(),
            "arm": self.arm.value,
            "broker_policy_digest": self.broker_policy_digest.canonical_value(),
            "dependencies": [item.canonical_value() for item in self.dependencies],
            "dependency_inventory_digest": self.dependency_inventory_digest.canonical_value(),
            "denylist_policy_digest": self.denylist_policy_digest.canonical_value(),
            "source_tree_digest": self.source_tree_digest.canonical_value(),
        }

    def is_current(self, *, now_ms: int) -> bool:
        return type(now_ms) is int and self.issued_at_ms <= now_ms < self.expires_at_ms


class SourcePinnedApprovalVerifier(Protocol):
    """Narrow seam that may emit only an authenticated source-bound projection."""

    def verify(
        self,
        approval: object,
        *,
        now_ms: int,
    ) -> SourcePinnedApprovalProjection | None:
        """Return a verified projection or fail closed."""


@dataclasses.dataclass(frozen=True, slots=True)
class CurrentFirewallPolicy:
    """Current immutable policy readback supplied by an external authority seam."""

    denylist: DeclaredProductionDenylist
    broker_policy: BrokerPolicy
    minimum_denylist_generation: int
    minimum_broker_generation: int
    expected_denylist_policy_digest: TypedDigest
    expected_broker_policy_digest: TypedDigest

    def __post_init__(self) -> None:
        if not isinstance(self.denylist, DeclaredProductionDenylist):
            raise ValueError("current denylist must be closed")
        if not isinstance(self.broker_policy, BrokerPolicy):
            raise ValueError("current broker policy must be closed")
        _require_exact_int(
            self.minimum_denylist_generation,
            "minimum denylist generation",
            minimum=1,
        )
        _require_exact_int(
            self.minimum_broker_generation,
            "minimum broker generation",
            minimum=1,
        )
        if self.denylist.generation < self.minimum_denylist_generation:
            raise ValueError("denylist generation rolled back")
        if self.broker_policy.generation < self.minimum_broker_generation:
            raise ValueError("broker generation rolled back")
        _require_typed_digest(
            self.expected_denylist_policy_digest,
            DigestDomain.DENYLIST_POLICY,
            "current denylist policy",
        )
        _require_typed_digest(
            self.expected_broker_policy_digest,
            DigestDomain.BROKER_POLICY,
            "current broker policy",
        )
        if self.expected_denylist_policy_digest != self.denylist.policy_digest:
            raise ValueError("denylist policy digest changed")
        if self.expected_broker_policy_digest != self.broker_policy.policy_digest:
            raise ValueError("broker policy digest changed")
        if self.broker_policy.denylist_policy_digest != self.denylist.policy_digest:
            raise ValueError("broker policy is not bound to the current denylist")


class CurrentFirewallPolicyAuthority(Protocol):
    """Read-only seam for current, immutable denylist and broker policy state."""

    def current(self) -> CurrentFirewallPolicy | None:
        """Return the current policy snapshot or fail closed."""


class InMemoryCurrentFirewallPolicyAuthority:
    """Offline-only authority double; it neither persists nor contacts a service."""

    def __init__(self, current_policy: CurrentFirewallPolicy | None) -> None:
        if current_policy is not None and not isinstance(
            current_policy,
            CurrentFirewallPolicy,
        ):
            raise ValueError("current policy must be closed or absent")
        self._current_policy = current_policy

    def current(self) -> CurrentFirewallPolicy | None:
        return self._current_policy


@dataclasses.dataclass(frozen=True, slots=True)
class MetadataOnlyRequest:
    """A request to validate metadata bindings; it contains no credential value."""

    broker_identity_digest: TypedDigest
    scope: NonproductionScope
    dependency: FirewallDependency
    dependency_binding_digest: TypedDigest
    credential_reference_digest: TypedDigest
    requested_expires_at_ms: int

    def __post_init__(self) -> None:
        _require_typed_digest(
            self.broker_identity_digest,
            DigestDomain.IDENTITY,
            "request broker identity",
        )
        if not isinstance(self.scope, NonproductionScope):
            raise ValueError("request scope must be closed")
        if not isinstance(self.dependency, FirewallDependency):
            raise ValueError("request dependency must be closed")
        _require_typed_digest(
            self.dependency_binding_digest,
            DigestDomain.DEPENDENCY_BINDING,
            "request dependency binding",
        )
        _require_typed_digest(
            self.credential_reference_digest,
            DigestDomain.CREDENTIAL_REFERENCE,
            "request credential reference",
        )
        _require_exact_int(
            self.requested_expires_at_ms,
            "request requested_expires_at_ms",
            minimum=1,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class MetadataOnlyGrant:
    """Bound metadata result; it is neither a credential nor execution authority."""

    approval_id_digest: TypedDigest
    approval_binding_digest: TypedDigest
    source_tree_digest: TypedDigest
    denylist_policy_digest: TypedDigest
    broker_policy_digest: TypedDigest
    broker_identity_digest: TypedDigest
    scope: NonproductionScope
    arm: FirewallArm
    dependency: FirewallDependency
    destination_digests: tuple[TypedDigest, ...]
    dependency_binding_digest: TypedDigest
    credential_reference_digest: TypedDigest
    issued_at_ms: int
    expires_at_ms: int
    grant_digest: TypedDigest

    def __post_init__(self) -> None:
        for value, domain, field_name in (
            (self.approval_id_digest, DigestDomain.APPROVAL_ID, "approval id"),
            (
                self.approval_binding_digest,
                DigestDomain.APPROVAL_BINDING,
                "approval binding",
            ),
            (self.source_tree_digest, DigestDomain.SOURCE_TREE, "source tree"),
            (
                self.denylist_policy_digest,
                DigestDomain.DENYLIST_POLICY,
                "denylist policy",
            ),
            (
                self.broker_policy_digest,
                DigestDomain.BROKER_POLICY,
                "broker policy",
            ),
            (
                self.broker_identity_digest,
                DigestDomain.IDENTITY,
                "broker identity",
            ),
            (
                self.credential_reference_digest,
                DigestDomain.CREDENTIAL_REFERENCE,
                "credential reference",
            ),
            (
                self.dependency_binding_digest,
                DigestDomain.DEPENDENCY_BINDING,
                "dependency binding",
            ),
        ):
            _require_typed_digest(value, domain, f"grant {field_name}")
        if not isinstance(self.scope, NonproductionScope):
            raise ValueError("grant scope must be closed")
        if not isinstance(self.arm, FirewallArm) or not isinstance(
            self.dependency,
            FirewallDependency,
        ):
            raise ValueError("grant arm and dependency must be closed")
        if self.dependency not in _ARM_DEPENDENCIES[self.arm]:
            raise ValueError("grant dependency is not valid for the arm")
        if (
            not self.destination_digests
            or any(
                not isinstance(item, TypedDigest)
                or item.domain is not DigestDomain.DESTINATION
                for item in self.destination_digests
            )
            or self.destination_digests
            != tuple(sorted(self.destination_digests, key=lambda item: item.value))
            or len({item.value for item in self.destination_digests})
            != len(self.destination_digests)
        ):
            raise ValueError(
                "grant destination digests must be nonempty, unique, and canonical"
            )
        _require_window(
            self.issued_at_ms,
            self.expires_at_ms,
            field_name="grant window",
        )
        _require_typed_digest(
            self.grant_digest,
            DigestDomain.METADATA_GRANT,
            "grant",
        )
        if self.grant_digest != _typed_digest(
            DigestDomain.METADATA_GRANT,
            self.unsigned_value(),
        ):
            raise ValueError("grant digest does not match")

    def unsigned_value(self) -> dict[str, object]:
        return {
            "approval_binding_digest": self.approval_binding_digest.canonical_value(),
            "approval_id_digest": self.approval_id_digest.canonical_value(),
            "arm": self.arm.value,
            "broker_identity_digest": self.broker_identity_digest.canonical_value(),
            "broker_policy_digest": self.broker_policy_digest.canonical_value(),
            "credential_reference_digest": self.credential_reference_digest.canonical_value(),
            "denylist_policy_digest": self.denylist_policy_digest.canonical_value(),
            "dependency": self.dependency.value,
            "dependency_binding_digest": self.dependency_binding_digest.canonical_value(),
            "destination_digests": [
                item.canonical_value() for item in self.destination_digests
            ],
            "expires_at_ms": self.expires_at_ms,
            "issued_at_ms": self.issued_at_ms,
            "scope": self.scope.canonical_value(),
            "source_tree_digest": self.source_tree_digest.canonical_value(),
        }


class ExecutionFirewallResolver:
    """Stateless metadata checker for a future credential-broker adapter."""

    def __init__(
        self,
        *,
        approval_verifier: SourcePinnedApprovalVerifier,
        policy_authority: CurrentFirewallPolicyAuthority,
    ) -> None:
        self._approval_verifier = approval_verifier
        self._policy_authority = policy_authority

    def resolve_metadata(
        self,
        approval: object,
        request: MetadataOnlyRequest,
        *,
        now_ms: int,
    ) -> MetadataOnlyGrant | None:
        if not isinstance(request, MetadataOnlyRequest) or type(now_ms) is not int:
            return None
        try:
            current = self._policy_authority.current()
            projection = self._approval_verifier.verify(approval, now_ms=now_ms)
        except Exception:
            return None
        if (
            not isinstance(current, CurrentFirewallPolicy)
            or not isinstance(projection, SourcePinnedApprovalProjection)
            or not current.denylist.is_current(now_ms=now_ms)
            or not current.broker_policy.is_current(now_ms=now_ms)
            or not projection.is_current(now_ms=now_ms)
            or projection.denylist_policy_digest != current.denylist.policy_digest
            or projection.broker_policy_digest != current.broker_policy.policy_digest
            or request.broker_identity_digest != current.broker_policy.broker_identity_digest
            or request.scope != current.broker_policy.scope
            or request.broker_identity_digest
            in current.denylist.production_identity_digests
        ):
            return None
        projection_binding = next(
            (
                binding
                for binding in projection.dependencies
                if binding.dependency is request.dependency
            ),
            None,
        )
        if projection_binding is None:
            return None
        if (
            request.dependency_binding_digest
            != projection_binding.dependency_binding_digest
            or request.credential_reference_digest
            != projection_binding.credential_reference_digest
            or projection_binding not in current.broker_policy.permitted_dependencies
            or any(
                destination in current.denylist.production_destination_digests
                for destination in projection_binding.destination_digests
            )
        ):
            return None
        maximum_expiry = min(
            projection.expires_at_ms,
            current.denylist.expires_at_ms,
            current.broker_policy.expires_at_ms,
            now_ms + current.broker_policy.metadata_grant_ttl_ms,
        )
        if not now_ms < request.requested_expires_at_ms <= maximum_expiry:
            return None
        unsigned = {
            "approval_binding_digest": projection.approval_binding_digest.canonical_value(),
            "approval_id_digest": projection.approval_id_digest.canonical_value(),
            "arm": projection.arm.value,
            "broker_identity_digest": request.broker_identity_digest.canonical_value(),
            "broker_policy_digest": current.broker_policy.policy_digest.canonical_value(),
            "credential_reference_digest": request.credential_reference_digest.canonical_value(),
            "denylist_policy_digest": current.denylist.policy_digest.canonical_value(),
            "dependency": request.dependency.value,
            "dependency_binding_digest": request.dependency_binding_digest.canonical_value(),
            "destination_digests": [
                item.canonical_value()
                for item in projection_binding.destination_digests
            ],
            "expires_at_ms": request.requested_expires_at_ms,
            "issued_at_ms": now_ms,
            "scope": request.scope.canonical_value(),
            "source_tree_digest": projection.source_tree_digest.canonical_value(),
        }
        return MetadataOnlyGrant(
            approval_id_digest=projection.approval_id_digest,
            approval_binding_digest=projection.approval_binding_digest,
            source_tree_digest=projection.source_tree_digest,
            denylist_policy_digest=current.denylist.policy_digest,
            broker_policy_digest=current.broker_policy.policy_digest,
            broker_identity_digest=request.broker_identity_digest,
            scope=request.scope,
            arm=projection.arm,
            dependency=request.dependency,
            destination_digests=projection_binding.destination_digests,
            dependency_binding_digest=request.dependency_binding_digest,
            credential_reference_digest=request.credential_reference_digest,
            issued_at_ms=now_ms,
            expires_at_ms=request.requested_expires_at_ms,
            grant_digest=_typed_digest(DigestDomain.METADATA_GRANT, unsigned),
        )


__all__ = [
    "ApprovedDependencyBinding",
    "BrokerPolicy",
    "CurrentFirewallPolicy",
    "CurrentFirewallPolicyAuthority",
    "DeclaredProductionDenylist",
    "DigestDomain",
    "ExecutionFirewallResolver",
    "FirewallArm",
    "FirewallDependency",
    "InMemoryCurrentFirewallPolicyAuthority",
    "MetadataOnlyGrant",
    "MetadataOnlyRequest",
    "NonproductionScope",
    "SourcePinnedApprovalProjection",
    "SourcePinnedApprovalVerifier",
    "TypedDigest",
]
