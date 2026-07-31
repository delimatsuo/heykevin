"""Fake-SDK-only tests for the unmounted Google Firestore transaction runner."""

from __future__ import annotations

import ast
from copy import deepcopy
import dataclasses
from pathlib import Path
from typing import Mapping

import pytest
from google.api_core import exceptions as google_exceptions

from app.services.voice_bakeoff_firestore_transaction_port import (
    FirestoreControlDatabaseBinding,
    FirestoreRunnerStatus,
    FirestoreTransactionPort,
)
from app.services.voice_bakeoff_google_firestore_runner import (
    GoogleFirestoreClientHandle,
    GoogleFirestoreTargetAttestation,
    GoogleFirestoreTransactionalExecutor,
    GoogleFirestoreTransactionRunner,
)
from app.services.voice_bakeoff_transactional_control_seam import (
    StoreNamespace,
    StoreRole,
    StoredRecord,
    TransactionAborted,
    TransactionScope,
    TransactionStatus,
)


_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _ROOT / "app/services/voice_bakeoff_google_firestore_runner.py"
_ASSEMBLY_SOURCE = _ROOT / "app/services/voice_bakeoff_control_store_assembly.py"


class _FakeReference:
    def __init__(self, path: tuple[str, ...]) -> None:
        self.path = path


class _FakeSnapshot:
    def __init__(
        self,
        *,
        exists: bool,
        envelope: Mapping[str, object] | None,
        update_time: object | None,
    ) -> None:
        self.exists = exists
        self._envelope = deepcopy(dict(envelope)) if envelope is not None else None
        self.update_time = update_time

    def to_dict(self) -> dict[str, object] | None:
        return deepcopy(self._envelope)


class _FakeVendorTransaction:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client
        self._records = deepcopy(client.records)
        self._writes: list[tuple[str, tuple[str, ...], object]] = []

    def fork(self) -> _FakeVendorTransaction:
        return _FakeVendorTransaction(self._client)

    def get(self, reference: _FakeReference):
        self._client.rpc_attempts += 1
        document = self._records.get(reference.path)
        if document is None:
            return iter(
                [
                    _FakeSnapshot(
                        exists=False,
                        envelope=None,
                        update_time=None,
                    )
                ]
            )
        return iter(
            [
                _FakeSnapshot(
                    exists=True,
                    envelope=document["envelope"],
                    update_time=document["update_time"],
                )
            ]
        )

    def create(self, reference: _FakeReference, envelope: Mapping[str, object]) -> None:
        self._client.rpc_attempts += 1
        if reference.path in self._records:
            raise google_exceptions.Conflict("create precondition failed")
        self._records[reference.path] = {
            "envelope": deepcopy(dict(envelope)),
            "update_time": self._client.next_update_time(),
        }
        self._writes.append(("create", reference.path, None))

    def update(
        self,
        reference: _FakeReference,
        envelope: Mapping[str, object],
        *,
        option: object,
    ) -> None:
        self._client.rpc_attempts += 1
        current = self._records.get(reference.path)
        expected = ("last_update_time", current["update_time"]) if current else None
        if current is None or option != expected:
            raise google_exceptions.Conflict("update precondition failed")
        self._records[reference.path] = {
            "envelope": deepcopy(dict(envelope)),
            "update_time": self._client.next_update_time(),
        }
        self._writes.append(("update", reference.path, option))

    def commit(self) -> None:
        self._client.records = self._records
        self._client.commits += 1


class _FakeClient:
    def __init__(self, *, project: str, database: str) -> None:
        self.project = project
        self._database = database
        self.records: dict[tuple[str, ...], dict[str, object]] = {}
        self.commits = 0
        self.document_calls = 0
        self.transaction_calls = 0
        self.write_option_calls = 0
        self.rpc_attempts = 0
        self._update_counter = 0

    def next_update_time(self) -> str:
        self._update_counter += 1
        return f"update_time_{self._update_counter}"

    def document(self, *path: str) -> _FakeReference:
        self.document_calls += 1
        return _FakeReference(tuple(path))

    def transaction(self, *, max_attempts: int) -> _FakeVendorTransaction:
        self.transaction_calls += 1
        assert max_attempts == 3
        return _FakeVendorTransaction(self)

    def write_option(self, *, last_update_time: object) -> tuple[str, object]:
        self.write_option_calls += 1
        return ("last_update_time", last_update_time)


class _FakeTransactionalExecutor(GoogleFirestoreTransactionalExecutor):
    def __init__(self) -> None:
        self.retry_once = False
        self.error: Exception | None = None

    def execute(self, transaction: _FakeVendorTransaction, callback):
        if self.error is not None:
            raise self.error
        if self.retry_once:
            self.retry_once = False
            callback(transaction.fork())
        value = callback(transaction)
        transaction.commit()
        return value


def _scope() -> TransactionScope:
    return TransactionScope(
        role=StoreRole.EXECUTION_CONTROL,
        project_ref="ref_control_project",
        database_ref="ref_control_database",
    )


def _binding() -> FirestoreControlDatabaseBinding:
    scope = _scope()
    return FirestoreControlDatabaseBinding(
        scope=scope,
        project_ref=scope.project_ref,
        database_ref=scope.database_ref,
        root_collection="voice_bakeoff_control",
        root_document="execution_control",
    )


def _runner(
    *,
    client: _FakeClient | None = None,
    executor: _FakeTransactionalExecutor | None = None,
) -> tuple[
    GoogleFirestoreTransactionRunner,
    _FakeClient,
    _FakeTransactionalExecutor,
]:
    selected_client = client or _FakeClient(
        project="voice-bakeoff-control-0724",
        database="voice-bakeoff-control",
    )
    selected_executor = executor or _FakeTransactionalExecutor()
    target = GoogleFirestoreTargetAttestation(
        binding=_binding(),
        project_id="voice-bakeoff-control-0724",
        database_id="voice-bakeoff-control",
        attestation_ref="ref_control_target_attestation",
    )
    handle = GoogleFirestoreClientHandle(
        client=selected_client,
        target=target,
    )
    return (
        GoogleFirestoreTransactionRunner(
            handle=handle,
            executor=selected_executor,
        ),
        selected_client,
        selected_executor,
    )


def _port() -> tuple[
    FirestoreTransactionPort,
    _FakeClient,
    _FakeTransactionalExecutor,
]:
    runner, client, executor = _runner()
    return FirestoreTransactionPort(runner=runner, binding=_binding()), client, executor


def _digest(character: str) -> str:
    return character * 64


@pytest.mark.parametrize("database_id", ["default", "(default)"])
def test_handle_rejects_default_target_forms_without_io(database_id: str) -> None:
    binding = _binding()
    with pytest.raises(ValueError, match="attestation"):
        GoogleFirestoreTargetAttestation(
            binding=binding,
            project_id="voice-bakeoff-control-0724",
            database_id=database_id,
            attestation_ref="ref_control_target_attestation",
        )


def test_handle_rejects_mismatched_client_target_without_io() -> None:
    binding = _binding()
    client = _FakeClient(
        project="voice-bakeoff-control-0724",
        database="voice-bakeoff-control",
    )
    with pytest.raises(ValueError, match="does not match"):
        GoogleFirestoreClientHandle(
            client=client,
            target=GoogleFirestoreTargetAttestation(
                binding=binding,
                project_id="voice-bakeoff-other-0724",
                database_id="voice-bakeoff-control",
                attestation_ref="ref_control_target_attestation",
            ),
        )
    assert client.transaction_calls == client.document_calls == client.rpc_attempts == 0


def test_handle_freezes_target_and_revalidates_client_before_every_operation() -> None:
    runner, client, _ = _runner()
    handle = runner._handle
    with pytest.raises(dataclasses.FrozenInstanceError):
        handle._target = GoogleFirestoreTargetAttestation(
            binding=_binding(),
            project_id="voice-bakeoff-control-0724",
            database_id="voice-bakeoff-control",
            attestation_ref="ref_other_target_attestation",
        )
    with pytest.raises(dataclasses.FrozenInstanceError):
        handle._target.binding = _binding()

    client._database = "wrong-nondefault-database"
    with pytest.raises(TransactionAborted, match="target changed"):
        handle.new_transaction(max_attempts=3)
    with pytest.raises(TransactionAborted, match="target changed"):
        handle.document_reference(
            (
                "voice_bakeoff_control",
                "execution_control",
                "trust_pins",
                "current",
            )
        )
    assert client.transaction_calls == client.document_calls == client.rpc_attempts == 0


def test_target_attestation_scope_mismatch_cannot_construct_a_port_or_call_client() -> None:
    runner, client, _ = _runner()
    other_scope = TransactionScope(
        role=StoreRole.EXECUTION_CONTROL,
        project_ref="ref_other_control_project",
        database_ref="ref_control_database",
    )
    other_binding = FirestoreControlDatabaseBinding(
        scope=other_scope,
        project_ref=other_scope.project_ref,
        database_ref=other_scope.database_ref,
        root_collection="voice_bakeoff_control",
        root_document="other_control",
    )
    mismatched_handle = GoogleFirestoreClientHandle(
        client=client,
        target=GoogleFirestoreTargetAttestation(
            binding=other_binding,
            project_id="voice-bakeoff-control-0724",
            database_id="voice-bakeoff-control",
            attestation_ref="ref_other_target_attestation",
        ),
    )
    mismatched_runner = GoogleFirestoreTransactionRunner(
        handle=mismatched_handle,
        executor=_FakeTransactionalExecutor(),
    )
    with pytest.raises(ValueError, match="does not match attested scope"):
        FirestoreTransactionPort(runner=mismatched_runner, binding=_binding())
    assert client.transaction_calls == client.document_calls == client.rpc_attempts == 0


def test_construction_is_inert_and_the_port_uses_create_then_update_preconditions() -> None:
    runner, client, _ = _runner()
    assert client.transaction_calls == client.document_calls == client.rpc_attempts == 0
    port = FirestoreTransactionPort(runner=runner, binding=_binding())
    key = _scope().key(StoreNamespace.TRUST_PIN, "current")
    assert port.transact(
        lambda view: view.create(key, StoredRecord.create({"alpha": "one"}))
    ).status is TransactionStatus.COMMITTED
    path = port.document_path_for(key)
    assert client.records[path]["envelope"] == {
        "fields": {"alpha": "one"},
        "record_version": 1,
        "schema_version": 1,
    }
    assert client.commits == 1

    def replace(view):
        record = view.read(key)
        assert record is not None
        return view.replace(
            key,
            expected_version=record.version,
            record=StoredRecord.create({"alpha": "two"}),
        )

    assert port.transact(replace).status is TransactionStatus.COMMITTED
    assert client.records[path]["envelope"] == {
        "fields": {"alpha": "two"},
        "record_version": 2,
        "schema_version": 1,
    }
    assert client.write_option_calls == 1


def test_late_abort_rolls_back_vendor_transaction() -> None:
    port, client, _ = _port()
    key = _scope().key(StoreNamespace.CONSUMED_NONCE, _digest("a"))

    def abort(view):
        assert view.create(key, StoredRecord.create({"control_ref": "ref_control_a"}))
        raise TransactionAborted("stop")

    assert port.transact(abort).status is TransactionStatus.ABORTED
    assert client.records == {}
    assert client.commits == 0


def test_vendor_retry_returns_only_final_committed_callback_value() -> None:
    port, client, executor = _port()
    executor.retry_once = True
    attempts = 0
    key = _scope().key(StoreNamespace.CONSUMED_NONCE, _digest("a"))

    def create(view):
        nonlocal attempts
        attempts += 1
        assert view.create(key, StoredRecord.create({"control_ref": "ref_control_a"}))
        return attempts

    result = port.transact(create)
    assert attempts == 2
    assert result.status is TransactionStatus.COMMITTED
    assert result.value == 2
    assert client.commits == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (google_exceptions.Conflict("conflict"), FirestoreRunnerStatus.CONFLICT),
        (
            google_exceptions.PermissionDenied("permission"),
            FirestoreRunnerStatus.UNAVAILABLE,
        ),
        (
            google_exceptions.ServiceUnavailable("unavailable"),
            FirestoreRunnerStatus.UNAVAILABLE,
        ),
        (
            google_exceptions.DeadlineExceeded("deadline"),
            FirestoreRunnerStatus.UNAVAILABLE,
        ),
        (
            google_exceptions.Unauthenticated("unauthenticated"),
            FirestoreRunnerStatus.UNAVAILABLE,
        ),
        (ValueError("programming"), FirestoreRunnerStatus.UNKNOWN),
        (RuntimeError("ambiguous"), FirestoreRunnerStatus.UNKNOWN),
    ],
)
def test_vendor_error_mapping_returns_no_value(
    error: Exception,
    expected: FirestoreRunnerStatus,
) -> None:
    runner, client, executor = _runner()
    executor.error = error
    result = runner.run_transaction(lambda view: "must-not-run")
    assert result.status is expected
    assert result.value is None
    assert client.transaction_calls == 1
    assert client.commits == 0


def test_malformed_vendor_snapshot_fails_closed() -> None:
    port, client, _ = _port()
    key = _scope().key(StoreNamespace.TRUST_PIN, "current")
    path = port.document_path_for(key)
    client.records[path] = {
        "envelope": {
            "fields": {"alpha": "one"},
            "record_version": 1,
            "schema_version": 1,
        },
        "update_time": None,
    }
    assert port.transact(lambda view: view.read(key)).status is TransactionStatus.ABORTED
    assert client.commits == 0


def test_runner_is_unwired_and_only_bakeoff_vendor_module_imports_google_sdk() -> None:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    forbidden_roots = {
        "asyncio",
        "http",
        "importlib",
        "os",
        "pathlib",
        "socket",
        "subprocess",
        "sys",
        "urllib",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(name.name.split(".")[0] not in forbidden_roots for name in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden_roots

    bakeoff_sources = tuple((_ROOT / "app/services").glob("voice_bakeoff*.py"))
    for path in bakeoff_sources:
        source = path.read_text(encoding="utf-8")
        if path == _SOURCE:
            assert "from google." in source
        else:
            assert "from google." not in source
            assert "import google." not in source

    runtime_sources = (
        *((_ROOT / "app").rglob("*.py")),
        *((_ROOT / "scripts").rglob("*.py")),
    )
    for path in runtime_sources:
        if path in {_SOURCE, _ASSEMBLY_SOURCE}:
            continue
        assert "voice_bakeoff_google_firestore_runner" not in path.read_text(
            encoding="utf-8"
        )
