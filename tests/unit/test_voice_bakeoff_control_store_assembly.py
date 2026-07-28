"""Tests for the unmounted, zero-I/O control-store assembly seam."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services.voice_bakeoff_control_store_assembly import (
    ControlStoreAssemblyInputs,
    assemble_control_stores,
)
from app.services.voice_bakeoff_firestore_transaction_port import (
    FirestoreControlDatabaseBinding,
)
from app.services.voice_bakeoff_google_firestore_runner import (
    GoogleFirestoreClientHandle,
    GoogleFirestoreTargetAttestation,
)
from app.services.voice_bakeoff_security_contracts import (
    ApprovalProvenanceVerifier,
    ControlActivationSigner,
    ControlGrantSigner,
    PreAuthAcknowledgementVerifier,
)
from app.services.voice_bakeoff_transactional_control_seam import (
    StoreRole,
    TransactionScope,
    TransactionalExecutionControlStore,
    TransactionalTrustGenerationPinStore,
)


_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _ROOT / "app/services/voice_bakeoff_control_store_assembly.py"


class _CountingClient:
    def __init__(self, *, project: str, database: str) -> None:
        self.project = project
        self._database = database
        self.transaction_calls = 0
        self.document_calls = 0
        self.write_option_calls = 0

    def transaction(self, *, max_attempts: int) -> object:
        self.transaction_calls += 1
        raise AssertionError("assembly must not start a transaction")

    def document(self, *path: str) -> object:
        self.document_calls += 1
        raise AssertionError("assembly must not read or write a document")

    def write_option(self, *, last_update_time: object) -> object:
        self.write_option_calls += 1
        raise AssertionError("assembly must not create a write option")


def _public_key_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes_raw()


def _scope() -> TransactionScope:
    return TransactionScope(
        role=StoreRole.EXECUTION_CONTROL,
        project_ref="ref_control_project",
        database_ref="ref_control_database",
    )


def _binding(
    scope: TransactionScope | None = None,
    *,
    root_document: str = "execution_control",
) -> FirestoreControlDatabaseBinding:
    selected_scope = scope or _scope()
    return FirestoreControlDatabaseBinding(
        scope=selected_scope,
        project_ref=selected_scope.project_ref,
        database_ref=selected_scope.database_ref,
        root_collection="voice_bakeoff_control",
        root_document=root_document,
    )


def _inputs(
    *,
    client: _CountingClient | None = None,
    scope: TransactionScope | None = None,
    binding: FirestoreControlDatabaseBinding | None = None,
    max_attempts: int = 3,
) -> tuple[ControlStoreAssemblyInputs, _CountingClient]:
    selected_scope = scope or _scope()
    selected_binding = binding or _binding(selected_scope)
    selected_client = client or _CountingClient(
        project="voice-bakeoff-control-0724",
        database="voice-bakeoff-control",
    )
    handle = GoogleFirestoreClientHandle(
        client=selected_client,
        target=GoogleFirestoreTargetAttestation(
            binding=selected_binding,
            project_id="voice-bakeoff-control-0724",
            database_id="voice-bakeoff-control",
            attestation_ref="ref_control_target_attestation",
        ),
    )
    provenance_key = Ed25519PrivateKey.generate()
    acknowledgement_key = Ed25519PrivateKey.generate()
    return (
        ControlStoreAssemblyInputs(
            handle=handle,
            binding=selected_binding,
            scope=selected_scope,
            approval_provenance_verifier=ApprovalProvenanceVerifier(
                _public_key_bytes(provenance_key)
            ),
            grant_signer=ControlGrantSigner(Ed25519PrivateKey.generate()),
            acknowledgement_verifier=PreAuthAcknowledgementVerifier(
                _public_key_bytes(acknowledgement_key)
            ),
            activation_signer=ControlActivationSigner(Ed25519PrivateKey.generate()),
            issuer_ref="ref_control_issuer",
            preauth_audience_ref="ref_preauth_audience",
            max_attempts=max_attempts,
        ),
        selected_client,
    )


def _assert_no_client_operation(client: _CountingClient) -> None:
    assert client.transaction_calls == 0
    assert client.document_calls == 0
    assert client.write_option_calls == 0


def test_assembly_is_inert_and_exposes_only_matched_control_stores() -> None:
    inputs, client = _inputs()

    assembly = assemble_control_stores(inputs)

    assert assembly.scope == inputs.scope
    assert assembly.binding == inputs.binding
    assert isinstance(assembly.trust_pins, TransactionalTrustGenerationPinStore)
    assert isinstance(assembly.control_store, TransactionalExecutionControlStore)
    assert assembly.trust_pins.scope == assembly.control_store.scope == inputs.scope
    assert "executor" not in {
        field.name for field in dataclasses.fields(ControlStoreAssemblyInputs)
    }
    assert not any(
        hasattr(assembly, name)
        for name in (
            "runner",
            "port",
            "preauth",
            "preauth_store",
            "saga",
            "token_store",
            "credential",
            "provider",
            "evidence",
        )
    )
    _assert_no_client_operation(client)


def test_assembly_rejects_mutated_or_substituted_control_targets_without_io() -> None:
    inputs, client = _inputs()
    client._database = "substituted-control-database"

    with pytest.raises(ValueError, match="target attestation"):
        assemble_control_stores(inputs)
    _assert_no_client_operation(client)

    inputs, client = _inputs()
    substituted_binding = _binding(inputs.scope, root_document="substituted_root")
    with pytest.raises(ValueError, match="inputs"):
        ControlStoreAssemblyInputs(
            handle=inputs.handle,
            binding=substituted_binding,
            scope=inputs.scope,
            approval_provenance_verifier=inputs.approval_provenance_verifier,
            grant_signer=inputs.grant_signer,
            acknowledgement_verifier=inputs.acknowledgement_verifier,
            activation_signer=inputs.activation_signer,
            issuer_ref=inputs.issuer_ref,
            preauth_audience_ref=inputs.preauth_audience_ref,
        )
    _assert_no_client_operation(client)


@pytest.mark.parametrize("max_attempts", [0, 6, True])
def test_assembly_rejects_invalid_retry_bounds_without_io(max_attempts: object) -> None:
    inputs, client = _inputs()

    with pytest.raises(ValueError, match="inputs"):
        dataclasses.replace(inputs, max_attempts=max_attempts)
    _assert_no_client_operation(client)


def test_assembly_rejects_preauth_scope_and_default_database_without_io() -> None:
    inputs, client = _inputs()
    preauth_scope = TransactionScope(
        role=StoreRole.PREAUTH,
        project_ref="ref_preauth_project",
        database_ref="ref_preauth_database",
    )
    with pytest.raises(ValueError, match="inputs"):
        dataclasses.replace(inputs, scope=preauth_scope)
    _assert_no_client_operation(client)

    with pytest.raises(ValueError, match="attestation"):
        GoogleFirestoreTargetAttestation(
            binding=inputs.binding,
            project_id="voice-bakeoff-control-0724",
            database_id="default",
            attestation_ref="ref_control_target_attestation",
        )
    _assert_no_client_operation(client)


def test_assembly_source_is_direct_import_allowlisted_and_has_no_runtime_hooks() -> None:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imports == {
        "__future__",
        "voice_bakeoff_firestore_transaction_port",
        "voice_bakeoff_google_firestore_runner",
        "voice_bakeoff_security_contracts",
        "voice_bakeoff_transactional_control_seam",
    }
    assert all(
        isinstance(
            node,
            (
                ast.ClassDef,
                ast.FunctionDef,
                ast.Import,
                ast.ImportFrom,
                ast.Expr,
            ),
        )
        for node in tree.body
    )
    source = _SOURCE.read_text(encoding="utf-8")
    assert "GoogleFirestoreTransactionalExecutor" not in source
    assert "executor=" not in source
    assert not any(
        module in source
        for module in (
            "app.config",
            "app.main",
            "os.",
            "pathlib",
            "subprocess",
            "socket",
            "httpx",
            "requests",
            "google.auth",
            "PreAuthTokenStore",
            "ExecutionSecuritySaga",
            "voice_pipeline",
        )
    )
