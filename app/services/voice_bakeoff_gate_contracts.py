"""Neutral, payload-safe Task 4.8 gate report value contracts.

These dataclasses deliberately import no pre-auth, cloud, provider, credential,
filesystem, or network functionality. They are shared by deny-only reporting
and source-only blocked diagnostics without making either path authoritative.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


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
