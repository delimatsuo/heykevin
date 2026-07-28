"""Inert assembly seam for the isolated bakeoff execution-control store.

This source-only module accepts already-attested, injected dependencies and
constructs only control-store adapters. It creates no SDK client, resolves no
credential, reads no configuration, and starts no transaction. It does not
construct a pre-auth store or saga, and it is unmounted from every normal,
deployment, and runtime path.

It is not a Task 4.8 composition root. A future sealed runtime composition must
remain a separately reviewed boundary that consumes a verified authority object
and keeps the physically separate pre-auth store outside this module.
"""

from __future__ import annotations

import dataclasses

from .voice_bakeoff_firestore_transaction_port import (
    FirestoreControlDatabaseBinding,
    FirestoreTransactionPort,
)
from .voice_bakeoff_google_firestore_runner import (
    GoogleFirestoreClientHandle,
    GoogleFirestoreTransactionRunner,
)
from .voice_bakeoff_security_contracts import (
    ApprovalProvenanceVerifier,
    ControlActivationSigner,
    ControlGrantSigner,
    PreAuthAcknowledgementVerifier,
)
from .voice_bakeoff_transactional_control_seam import (
    StoreRole,
    TransactionScope,
    TransactionalExecutionControlStore,
    TransactionalTrustGenerationPinStore,
)


@dataclasses.dataclass(frozen=True, slots=True)
class ControlStoreAssemblyInputs:
    """Already-attested control dependencies; never credentials or runtime config."""

    handle: GoogleFirestoreClientHandle
    binding: FirestoreControlDatabaseBinding
    scope: TransactionScope
    approval_provenance_verifier: ApprovalProvenanceVerifier
    grant_signer: ControlGrantSigner
    acknowledgement_verifier: PreAuthAcknowledgementVerifier
    activation_signer: ControlActivationSigner
    issuer_ref: str
    preauth_audience_ref: str
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if (
            not isinstance(self.handle, GoogleFirestoreClientHandle)
            or not isinstance(self.binding, FirestoreControlDatabaseBinding)
            or not isinstance(self.scope, TransactionScope)
            or self.scope.role is not StoreRole.EXECUTION_CONTROL
            or self.binding.scope != self.scope
            or self.handle.binding != self.binding
            or not isinstance(
                self.approval_provenance_verifier,
                ApprovalProvenanceVerifier,
            )
            or not isinstance(self.grant_signer, ControlGrantSigner)
            or not isinstance(
                self.acknowledgement_verifier,
                PreAuthAcknowledgementVerifier,
            )
            or not isinstance(self.activation_signer, ControlActivationSigner)
            or type(self.max_attempts) is not int
            or not 1 <= self.max_attempts <= 5
        ):
            raise ValueError("control store assembly inputs are invalid")


@dataclasses.dataclass(frozen=True, slots=True)
class ControlStoreAssembly:
    """The only surface returned by the inert assembly seam."""

    scope: TransactionScope
    binding: FirestoreControlDatabaseBinding
    trust_pins: TransactionalTrustGenerationPinStore
    control_store: TransactionalExecutionControlStore

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scope, TransactionScope)
            or self.scope.role is not StoreRole.EXECUTION_CONTROL
            or not isinstance(self.binding, FirestoreControlDatabaseBinding)
            or self.binding.scope != self.scope
            or not isinstance(self.trust_pins, TransactionalTrustGenerationPinStore)
            or self.trust_pins.scope != self.scope
            or not isinstance(self.control_store, TransactionalExecutionControlStore)
            or self.control_store.scope != self.scope
        ):
            raise ValueError("control store assembly is invalid")


def assemble_control_stores(
    inputs: ControlStoreAssemblyInputs,
) -> ControlStoreAssembly:
    """Assemble inert control stores without beginning a Firestore transaction."""

    if not isinstance(inputs, ControlStoreAssemblyInputs):
        raise ValueError("control store assembly requires closed inputs")
    try:
        inputs.handle.validate_target()
    except Exception as error:
        raise ValueError("control store target attestation is invalid") from error

    runner = GoogleFirestoreTransactionRunner(
        handle=inputs.handle,
        max_attempts=inputs.max_attempts,
    )
    port = FirestoreTransactionPort(
        runner=runner,
        binding=inputs.binding,
    )
    return ControlStoreAssembly(
        scope=inputs.scope,
        binding=inputs.binding,
        trust_pins=TransactionalTrustGenerationPinStore(
            port=port,
            scope=inputs.scope,
        ),
        control_store=TransactionalExecutionControlStore(
            port=port,
            scope=inputs.scope,
            approval_provenance_verifier=inputs.approval_provenance_verifier,
            grant_signer=inputs.grant_signer,
            acknowledgement_verifier=inputs.acknowledgement_verifier,
            activation_signer=inputs.activation_signer,
            issuer_ref=inputs.issuer_ref,
            preauth_audience_ref=inputs.preauth_audience_ref,
        ),
    )
