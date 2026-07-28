"""Fake-only tests for the unwired Firestore-shaped transaction port."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Mapping

import pytest

from app.services.voice_bakeoff_firestore_transaction_port import (
    FirestoreControlDatabaseBinding,
    FirestoreRunnerResult,
    FirestoreRunnerStatus,
    FirestoreStoredDocument,
    FirestoreTransaction,
    FirestoreTransactionPort,
    FirestoreTransactionRunner,
)
from app.services.voice_bakeoff_transactional_control_seam import (
    StoreNamespace,
    StoreRole,
    StoredRecord,
    TransactionAborted,
    TransactionResult,
    TransactionScope,
    TransactionStatus,
)


_ROOT = Path(__file__).resolve().parents[2]
_SOURCE = _ROOT / "app/services/voice_bakeoff_firestore_transaction_port.py"
_GOOGLE_RUNNER_SOURCE = _ROOT / "app/services/voice_bakeoff_google_firestore_runner.py"
_ASSEMBLY_SOURCE = _ROOT / "app/services/voice_bakeoff_control_store_assembly.py"


class _FakeFirestoreTransaction(FirestoreTransaction):
    def __init__(
        self,
        records: dict[tuple[str, ...], FirestoreStoredDocument],
        *,
        next_backend_version,
    ) -> None:
        self.records = records
        self._next_backend_version = next_backend_version

    def read_document(
        self,
        path: tuple[str, ...],
    ) -> FirestoreStoredDocument | None:
        document = self.records.get(path)
        return deepcopy(document) if document is not None else None

    def create_document(
        self,
        path: tuple[str, ...],
        envelope: Mapping[str, object],
    ) -> None:
        if path in self.records:
            raise RuntimeError("create precondition failed")
        self.records[path] = FirestoreStoredDocument(
            backend_version=self._next_backend_version(),
            envelope=deepcopy(dict(envelope)),
        )

    def replace_document(
        self,
        path: tuple[str, ...],
        *,
        expected_backend_version: str,
        envelope: Mapping[str, object],
    ) -> None:
        current = self.records.get(path)
        if current is None or current.backend_version != expected_backend_version:
            raise RuntimeError("backend version precondition failed")
        self.records[path] = FirestoreStoredDocument(
            backend_version=self._next_backend_version(),
            envelope=deepcopy(dict(envelope)),
        )


class _FakeFirestoreRunner(FirestoreTransactionRunner):
    """Serializable fake; it does not instantiate a client or contact Firestore."""

    def __init__(self, binding: FirestoreControlDatabaseBinding) -> None:
        self._lock = RLock()
        self._binding = binding
        self._records: dict[tuple[str, ...], FirestoreStoredDocument] = {}
        self._forced_status: FirestoreRunnerStatus | None = None
        self._retry_once = False
        self._version = 0

    @property
    def binding(self) -> FirestoreControlDatabaseBinding:
        return self._binding

    def _next_backend_version(self) -> str:
        self._version += 1
        return f"version_{self._version}"

    def force_next(self, status: FirestoreRunnerStatus) -> None:
        if status is FirestoreRunnerStatus.COMMITTED:
            raise ValueError("forced status must be non-committed")
        self._forced_status = status

    def retry_once(self) -> None:
        self._retry_once = True

    def inject(
        self,
        path: tuple[str, ...],
        *,
        version: int,
        fields: Mapping[str, object],
        envelope_extra: Mapping[str, object] | None = None,
    ) -> None:
        envelope: dict[str, object] = {
            "fields": dict(fields),
            "record_version": version,
            "schema_version": 1,
        }
        if envelope_extra is not None:
            envelope.update(envelope_extra)
        self._records[path] = FirestoreStoredDocument(
            backend_version=self._next_backend_version(),
            envelope=envelope,
        )

    def run_transaction(self, callback) -> FirestoreRunnerResult:
        with self._lock:
            if self._forced_status is not None:
                status = self._forced_status
                self._forced_status = None
                return FirestoreRunnerResult(status=status)
            if self._retry_once:
                self._retry_once = False
                callback(
                    _FakeFirestoreTransaction(
                        deepcopy(self._records),
                        next_backend_version=self._next_backend_version,
                    )
                )
            transaction = _FakeFirestoreTransaction(
                deepcopy(self._records),
                next_backend_version=self._next_backend_version,
            )
            value = callback(transaction)
            self._records = transaction.records
            return FirestoreRunnerResult(
                status=FirestoreRunnerStatus.COMMITTED,
                value=value,
            )

    def stored(self, path: tuple[str, ...]) -> FirestoreStoredDocument | None:
        document = self._records.get(path)
        return deepcopy(document) if document is not None else None

    def count(self) -> int:
        return len(self._records)


def _scope() -> TransactionScope:
    return TransactionScope(
        role=StoreRole.EXECUTION_CONTROL,
        project_ref="ref_control_project",
        database_ref="ref_control_database",
    )


def _binding(scope: TransactionScope | None = None) -> FirestoreControlDatabaseBinding:
    selected_scope = scope or _scope()
    return FirestoreControlDatabaseBinding(
        scope=selected_scope,
        project_ref=selected_scope.project_ref,
        database_ref=selected_scope.database_ref,
        root_collection="voice_bakeoff_control",
        root_document="execution_control",
    )


def _port(
    runner: _FakeFirestoreRunner | None = None,
) -> tuple[FirestoreTransactionPort, _FakeFirestoreRunner]:
    selected_binding = _binding()
    selected_runner = runner or _FakeFirestoreRunner(selected_binding)
    return (
        FirestoreTransactionPort(runner=selected_runner, binding=selected_binding),
        selected_runner,
    )


def _digest(character: str) -> str:
    return character * 64


def test_constructor_rejects_non_control_or_mismatched_attested_scope() -> None:
    with pytest.raises(ValueError, match="control scope"):
        FirestoreControlDatabaseBinding(
            scope=TransactionScope(
                role=StoreRole.PREAUTH,
                project_ref="ref_preauth_project",
                database_ref="ref_preauth_database",
            ),
            project_ref="ref_preauth_project",
            database_ref="ref_preauth_database",
            root_collection="voice_bakeoff_control",
            root_document="execution_control",
        )


def test_constructor_rejects_runner_project_database_and_root_substitution() -> None:
    binding = _binding()
    substitutions = (
        FirestoreControlDatabaseBinding(
            scope=binding.scope,
            project_ref=binding.scope.project_ref,
            database_ref=binding.scope.database_ref,
            root_collection="voice_bakeoff_control",
            root_document="other_control",
        ),
        FirestoreControlDatabaseBinding(
            scope=TransactionScope(
                role=StoreRole.EXECUTION_CONTROL,
                project_ref="ref_other_project",
                database_ref="ref_control_database",
            ),
            project_ref="ref_other_project",
            database_ref="ref_control_database",
            root_collection="voice_bakeoff_control",
            root_document="execution_control",
        ),
        FirestoreControlDatabaseBinding(
            scope=TransactionScope(
                role=StoreRole.EXECUTION_CONTROL,
                project_ref="ref_control_project",
                database_ref="ref_other_database",
            ),
            project_ref="ref_control_project",
            database_ref="ref_other_database",
            root_collection="voice_bakeoff_control",
            root_document="execution_control",
        ),
    )
    for runner_binding in substitutions:
        with pytest.raises(ValueError, match="does not match attested scope"):
            FirestoreTransactionPort(
                runner=_FakeFirestoreRunner(runner_binding),
                binding=binding,
            )
    scope = _scope()
    with pytest.raises(ValueError, match="control scope"):
        FirestoreControlDatabaseBinding(
            scope=scope,
            project_ref="ref_other_project",
            database_ref=scope.database_ref,
            root_collection="voice_bakeoff_control",
            root_document="execution_control",
        )
    with pytest.raises(ValueError, match="control scope"):
        FirestoreControlDatabaseBinding(
            scope=scope,
            project_ref=scope.project_ref,
            database_ref=scope.database_ref,
            root_collection="control/escape",
            root_document="execution_control",
        )


def test_deterministic_document_mapping_rejects_scope_and_identifier_substitution() -> None:
    port, _ = _port()
    scope = _scope()
    assert port.document_path_for(scope.key(StoreNamespace.TRUST_PIN, "current")) == (
        "voice_bakeoff_control",
        "execution_control",
        "trust_pins",
        "current",
    )
    assert port.document_path_for(
        scope.key(StoreNamespace.CONSUMED_NONCE, _digest("a"))
    ) == (
        "voice_bakeoff_control",
        "execution_control",
        "consumed_nonces",
        _digest("a"),
    )
    assert port.document_path_for(
        scope.key(StoreNamespace.BINDING_EPOCH, f"{_digest('b')}:3")
    ) == (
        "voice_bakeoff_control",
        "execution_control",
        "binding_epochs",
        _digest("b"),
        "epochs",
        "3",
    )
    assert port.document_path_for(
        scope.key(StoreNamespace.CONTROL_RESERVATION, "ref_control_abc123")
    ) == (
        "voice_bakeoff_control",
        "execution_control",
        "reservations",
        "ref_control_abc123",
    )
    with pytest.raises(TransactionAborted, match="scope mismatch"):
        port.document_path_for(
            TransactionScope(
                role=StoreRole.EXECUTION_CONTROL,
                project_ref="ref_other_project",
                database_ref="ref_control_database",
            ).key(StoreNamespace.TRUST_PIN, "current")
        )
    with pytest.raises(TransactionAborted, match="control reservation key"):
        port.document_path_for(
            scope.key(StoreNamespace.CONTROL_RESERVATION, "ref_control/escape")
        )


@pytest.mark.parametrize(
    "key",
    [
        lambda scope: scope.key(StoreNamespace.TRUST_PIN, "current"),
        lambda scope: scope.key(StoreNamespace.CONSUMED_NONCE, _digest("a")),
        lambda scope: scope.key(StoreNamespace.CONSUMED_APPROVAL, _digest("b")),
        lambda scope: scope.key(StoreNamespace.BINDING_EPOCH, f"{_digest('c')}:4"),
        lambda scope: scope.key(
            StoreNamespace.CONTROL_RESERVATION,
            "ref_control_abc123",
        ),
    ],
)
def test_every_supported_document_path_has_firestore_parity_and_safe_segments(key) -> None:
    port, _ = _port()
    path = port.document_path_for(key(_scope()))
    assert len(path) % 2 == 0
    assert all(segment.isascii() and segment.replace("_", "").isalnum() for segment in path)


def test_create_and_version_fenced_replace_use_closed_envelope() -> None:
    port, runner = _port()
    key = _scope().key(StoreNamespace.TRUST_PIN, "current")
    initial = StoredRecord.create({"alpha": "one"})
    created = port.transact(lambda view: view.create(key, initial))
    assert created == TransactionResult(status=TransactionStatus.COMMITTED, value=True)
    path = port.document_path_for(key)
    assert runner.stored(path) == FirestoreStoredDocument(
        backend_version="version_1",
        envelope={
            "fields": {"alpha": "one"},
            "record_version": 1,
            "schema_version": 1,
        },
    )

    def replace(view):
        current = view.read(key)
        assert current is not None
        assert current.version == 1
        return view.replace(
            key,
            expected_version=current.version,
            record=StoredRecord.create({"alpha": "two"}),
        )

    replaced = port.transact(replace)
    assert replaced == TransactionResult(status=TransactionStatus.COMMITTED, value=True)
    assert runner.stored(path) == FirestoreStoredDocument(
        backend_version="version_2",
        envelope={
            "fields": {"alpha": "two"},
            "record_version": 2,
            "schema_version": 1,
        },
    )

    stale = port.transact(
        lambda view: view.replace(
            key,
            expected_version=1,
            record=StoredRecord.create({"alpha": "stale"}),
        )
    )
    assert stale == TransactionResult(status=TransactionStatus.COMMITTED, value=False)


def test_malformed_backend_envelope_aborts_without_a_value_or_write() -> None:
    port, runner = _port()
    key = _scope().key(StoreNamespace.TRUST_PIN, "current")
    path = port.document_path_for(key)
    runner.inject(
        path,
        version=1,
        fields={"alpha": "one"},
        envelope_extra={"unexpected": "field"},
    )
    result = port.transact(lambda view: view.read(key))
    assert result == TransactionResult(status=TransactionStatus.ABORTED)
    assert runner.count() == 1


@pytest.mark.parametrize(
    "envelope_extra",
    [
        {"schema_version": True},
        {"fields": {}},
        {"fields": {"alpha": 1.5}},
    ],
)
def test_malformed_backend_envelope_types_abort_without_a_value(
    envelope_extra: Mapping[str, object],
) -> None:
    port, runner = _port()
    key = _scope().key(StoreNamespace.TRUST_PIN, "current")
    runner.inject(
        port.document_path_for(key),
        version=1,
        fields={"alpha": "one"},
        envelope_extra=envelope_extra,
    )
    assert port.transact(lambda view: view.read(key)) == TransactionResult(
        status=TransactionStatus.ABORTED
    )
    assert runner.count() == 1


def test_late_abort_rolls_back_staged_creates() -> None:
    port, runner = _port()
    scope = _scope()
    nonce_key = scope.key(StoreNamespace.CONSUMED_NONCE, _digest("a"))
    approval_key = scope.key(StoreNamespace.CONSUMED_APPROVAL, _digest("b"))

    def abort_after_create(view):
        assert view.create(nonce_key, StoredRecord.create({"control_ref": "ref_control_a"}))
        assert view.create(
            approval_key,
            StoredRecord.create({"control_ref": "ref_control_a"}),
        )
        raise TransactionAborted("injected late failure")

    assert port.transact(abort_after_create) == TransactionResult(
        status=TransactionStatus.ABORTED
    )
    assert runner.count() == 0


def test_callback_retry_releases_only_final_committed_value() -> None:
    port, runner = _port()
    runner.retry_once()
    attempts = 0
    key = _scope().key(StoreNamespace.CONSUMED_NONCE, _digest("a"))

    def callback(view):
        nonlocal attempts
        attempts += 1
        assert view.create(key, StoredRecord.create({"control_ref": "ref_control_a"}))
        return f"attempt-{attempts}"

    result = port.transact(callback)
    assert attempts == 2
    assert result == TransactionResult(
        status=TransactionStatus.COMMITTED,
        value="attempt-2",
    )
    assert runner.count() == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            FirestoreRunnerStatus.CONFLICT,
            TransactionStatus.CONFLICT,
        ),
        (
            FirestoreRunnerStatus.UNAVAILABLE,
            TransactionStatus.UNAVAILABLE,
        ),
        (
            FirestoreRunnerStatus.UNKNOWN,
            TransactionStatus.UNKNOWN,
        ),
    ],
)
def test_noncommitted_runner_outcomes_return_no_value(
    status: FirestoreRunnerStatus,
    expected: TransactionStatus,
) -> None:
    port, runner = _port()
    runner.force_next(status)
    result = port.transact(lambda view: "must-not-run")
    assert result == TransactionResult(status=expected)
    assert result.value is None
    assert runner.count() == 0


def test_concurrent_ports_admit_one_nonce_winner() -> None:
    runner = _FakeFirestoreRunner(_binding())
    first, _ = _port(runner)
    second, _ = _port(runner)
    key = _scope().key(StoreNamespace.CONSUMED_NONCE, _digest("a"))

    def create_once(port: FirestoreTransactionPort) -> TransactionResult:
        def callback(view):
            if view.read(key) is not None:
                return False
            return view.create(
                key,
                StoredRecord.create({"control_ref": "ref_control_winner"}),
            )

        return port.transact(callback)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create_once, (first, second)))
    assert [result.value for result in results].count(True) == 1
    assert [result.value for result in results].count(False) == 1
    assert runner.count() == 1


def test_reads_after_staged_write_abort_before_backend_access() -> None:
    port, runner = _port()
    scope = _scope()
    nonce_key = scope.key(StoreNamespace.CONSUMED_NONCE, _digest("a"))
    approval_key = scope.key(StoreNamespace.CONSUMED_APPROVAL, _digest("b"))

    def invalid_order(view):
        assert view.create(nonce_key, StoredRecord.create({"control_ref": "ref_control_a"}))
        view.read(approval_key)
        return True

    assert port.transact(invalid_order) == TransactionResult(
        status=TransactionStatus.ABORTED
    )
    assert runner.count() == 0


def test_adapter_is_unwired_and_has_no_cloud_or_runtime_authority_imports() -> None:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    forbidden_roots = {
        "asyncio",
        "boto",
        "google",
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

    runtime_sources = (
        *(
            path
            for path in (_ROOT / "app").rglob("*.py")
            if path not in {_SOURCE, _GOOGLE_RUNNER_SOURCE, _ASSEMBLY_SOURCE}
        ),
        *(_ROOT / "scripts").rglob("*.py"),
    )
    for path in runtime_sources:
        assert "voice_bakeoff_firestore_transaction_port" not in path.read_text(
            encoding="utf-8"
        )
