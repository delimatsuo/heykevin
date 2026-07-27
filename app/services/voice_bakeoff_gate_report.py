"""Read-only Task 4.8 gate status for the sole project owner.

This module is an offline status projection. It has no signature, credential,
provider, networking, or state-transition capability.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import re

from app.services.voice_bakeoff_preauth_reference import (
    PreAuthReferenceError,
    validate_preauth_store_reference,
)


_SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PACKAGE_STATUSES = {
    "preparation_only",
    "review_ready_external_gates_required",
}


class GateReportError(ValueError):
    """Raised when local input could be mistaken for an executable gate."""


@dataclass(frozen=True, slots=True)
class BlockingGate:
    gate_id: str
    protection: str


@dataclass(frozen=True, slots=True)
class Task48GateReport:
    report_source_sha: str
    package_source_binding: str
    package_status: str
    advisory_review_status: str
    owner_approval_status: str
    preauth_reference_status: str
    execution_status: str
    required_pre_network_controls: tuple[str, ...]
    blocking_gates: tuple[BlockingGate, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_BLOCKING_GATES = (
    BlockingGate(
        "sealed_owner_authorization",
        "Requires a source-pinned, one-use owner authorization record and separate custody before execution can begin.",
    ),
    BlockingGate(
        "independent_technical_review",
        "Requires an independent technical review with no unresolved P1; advisory review cannot authorize execution.",
    ),
    BlockingGate(
        "physically_separate_preauth_store",
        "Keeps pre-auth token material away from execution metadata and business data.",
    ),
    BlockingGate(
        "identity_and_credential_broker",
        "Prevents the runner from resolving broad, production-capable credentials.",
    ),
    BlockingGate(
        "durable_trust_and_revocation_store",
        "Rejects revoked or stale authorization before any external dependency is contacted.",
    ),
    BlockingGate(
        "provider_privacy_and_region_attestations",
        "Pins nonproduction account, region, retention, logging, and data-sharing posture.",
    ),
    BlockingGate(
        "complete_production_denylist",
        "Prevents the isolated experiment from reaching production destinations.",
    ),
    BlockingGate(
        "immutable_custody_and_residue_routing",
        "Preserves payload-safe evidence and supports deletion/residue audit.",
    ),
    BlockingGate(
        "one_use_runtime_envelope",
        "Binds one bounded run to its exact source, caps, and expiry.",
    ),
)


def build_task_4_8_gate_report(
    *,
    package: Mapping[str, object],
    source_sha: str,
) -> Task48GateReport:
    """Project a non-authorizing report from a known non-executable package."""
    if not _SOURCE_SHA.fullmatch(source_sha):
        raise GateReportError("source SHA is invalid")

    package_status = package.get("package_status")
    if not isinstance(package_status, str) or package_status not in _PACKAGE_STATUSES:
        raise GateReportError("package status is not preparation-only")
    if package.get("execution_supported") is not False:
        raise GateReportError("execution-capable package refused")

    envelope = package.get("signature_envelope")
    package_source_sha = (
        envelope.get("source_sha") if isinstance(envelope, Mapping) else None
    )
    if package_source_sha is None:
        package_source_binding = "unbound_template"
    elif package_source_sha == source_sha:
        package_source_binding = "matches_report_source"
    else:
        raise GateReportError("package source SHA does not match report source")

    preauth_reference = package.get("preauth_store_reference")
    preauth_reference_status = "not_recorded"
    if preauth_reference is not None:
        if not isinstance(preauth_reference, Mapping):
            raise GateReportError("pre-auth reference is invalid")
        try:
            validate_preauth_store_reference(preauth_reference)
        except PreAuthReferenceError as error:
            raise GateReportError("pre-auth reference is invalid") from error
        preauth_reference_status = "reference_only_observed"

    return Task48GateReport(
        report_source_sha=source_sha,
        package_source_binding=package_source_binding,
        package_status=package_status,
        advisory_review_status="advisory_only",
        owner_approval_status="not_recorded",
        preauth_reference_status=preauth_reference_status,
        execution_status="not_authorized",
        required_pre_network_controls=(
            "credential_resolution_must_remain_blocked",
            "networking_must_remain_blocked",
            "provider_and_pstn_must_remain_blocked",
        ),
        blocking_gates=_BLOCKING_GATES,
    )
