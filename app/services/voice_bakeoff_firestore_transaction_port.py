"""Unwired Firestore-shaped transaction port for isolated bakeoff control data.

This source-only adapter deliberately imports no cloud SDK and constructs no
client.  It accepts an already-attested, narrowly typed transaction runner, but
is not composed by the application, runner, webhook, experiment, deployment, or
configuration paths.  Its tests use fake runners only.

The adapter handles one execution-control store.  The physically separate
pre-auth store remains a compensating-saga participant and is intentionally not
represented here.  Nothing in this module authorizes Task 4.8, IAM, identity,
credential, provider, PSTN, production, staging, or network activity.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
import dataclasses
import enum
import re

from .voice_bakeoff_transactional_control_seam import (
    StoreNamespace,
    StoreRole,
    StoredRecord,
    TransactionAborted,
    TransactionPort,
    TransactionResult,
    TransactionScope,
    TransactionStatus,
    TransactionView,
)


_HEX = frozenset("0123456789abcdef")
_DOCUMENT_SEGMENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SAFE_REF = re.compile(r"^ref_[a-z0-9_]{1,123}$")


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _is_exact_int(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _is_document_segment(value: object) -> bool:
    return isinstance(value, str) and _DOCUMENT_SEGMENT.fullmatch(value) is not None


def _is_safe_ref(value: object) -> bool:
    return isinstance(value, str) and _SAFE_REF.fullmatch(value) is not None


class FirestoreRunnerStatus(str, enum.Enum):
    """The closed outcomes surfaced by an injected Firestore transaction runner."""

    COMMITTED = "committed"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True, slots=True)
class FirestoreRunnerResult:
    """Result returned only after an injected runner finishes one transaction."""

    status: FirestoreRunnerStatus
    value: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, FirestoreRunnerStatus):
            raise ValueError("runner status must be closed")
        if self.status is not FirestoreRunnerStatus.COMMITTED and self.value is not None:
            raise ValueError("non-committed runner result cannot carry a value")


@dataclasses.dataclass(frozen=True, slots=True)
class FirestoreStoredDocument:
    """Opaque backend version plus the closed record envelope read by a runner."""

    backend_version: str
    envelope: Mapping[str, object]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.backend_version, str)
            or not self.backend_version
            or len(self.backend_version) > 256
            or not isinstance(self.envelope, Mapping)
        ):
            raise ValueError("stored Firestore document is invalid")


class FirestoreTransaction(ABC):
    """Narrow Firestore-shaped operation set used only inside a retryable callback."""

    @abstractmethod
    def read_document(
        self,
        path: tuple[str, ...],
    ) -> FirestoreStoredDocument | None:
        raise NotImplementedError

    @abstractmethod
    def create_document(
        self,
        path: tuple[str, ...],
        envelope: Mapping[str, object],
    ) -> None:
        """Stage a create with an exists-false precondition."""

        raise NotImplementedError

    @abstractmethod
    def replace_document(
        self,
        path: tuple[str, ...],
        *,
        expected_backend_version: str,
        envelope: Mapping[str, object],
    ) -> None:
        """Stage a replacement fenced by the read backend document version."""

        raise NotImplementedError


class FirestoreTransactionRunner(ABC):
    """Injected single-store transaction executor; no client escapes this boundary."""

    @property
    @abstractmethod
    def binding(self) -> FirestoreControlDatabaseBinding:
        """Return the immutable attested store binding held by this runner."""

        raise NotImplementedError

    @abstractmethod
    def run_transaction(
        self,
        callback: Callable[[FirestoreTransaction], object],
    ) -> FirestoreRunnerResult:
        """Commit a retryable callback or return a closed fail-safe outcome.

        The runner may execute ``callback`` multiple times before a final
        commit.  It must run each attempt atomically, enforce all staged create
        and replacement preconditions, return a value only after a confirmed
        commit, and let callback exceptions escape so the adapter can map them.
        """

        raise NotImplementedError


@dataclasses.dataclass(frozen=True, slots=True)
class FirestoreControlDatabaseBinding:
    """Attested fixed location and root for one execution-control transaction port."""

    scope: TransactionScope
    project_ref: str
    database_ref: str
    root_collection: str
    root_document: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.scope, TransactionScope)
            or self.scope.role is not StoreRole.EXECUTION_CONTROL
            or self.project_ref != self.scope.project_ref
            or self.database_ref != self.scope.database_ref
            or not _is_document_segment(self.root_collection)
            or not _is_document_segment(self.root_document)
        ):
            raise ValueError("Firestore binding must exactly match one control scope")

    @property
    def root_path(self) -> tuple[str, str]:
        return (self.root_collection, self.root_document)


@dataclasses.dataclass(frozen=True, slots=True)
class _CachedDocument:
    record: StoredRecord
    backend_version: str | None


class _AbortTransaction(RuntimeError):
    """Internal callback signal that forces the injected runner to roll back."""


def _encode_record(record: StoredRecord, *, version: int) -> dict[str, object]:
    if not isinstance(record, StoredRecord) or not _is_exact_int(version, minimum=1):
        raise ValueError("record envelope inputs are invalid")
    return {
        "fields": record.values(),
        "record_version": version,
        "schema_version": 1,
    }


def _decode_record(document: FirestoreStoredDocument) -> StoredRecord | None:
    if not isinstance(document, FirestoreStoredDocument):
        return None
    envelope = document.envelope
    if set(envelope) != {"fields", "record_version", "schema_version"}:
        return None
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1 or not _is_exact_int(
        envelope["record_version"], minimum=1
    ):
        return None
    fields = envelope["fields"]
    if (
        not isinstance(fields, Mapping)
        or not fields
        or any(
            not isinstance(name, str)
            or not name
            or type(value) not in {str, int, bool, type(None)}
            for name, value in fields.items()
        )
    ):
        return None
    try:
        return StoredRecord(
            version=envelope["record_version"],
            fields=tuple(sorted(dict(fields).items(), key=lambda item: item[0])),
        )
    except (TypeError, ValueError):
        return None


class _FirestoreTransactionView(TransactionView):
    """Closed record mapper over one runner attempt; never escapes the attempt."""

    def __init__(
        self,
        *,
        transaction: FirestoreTransaction,
        binding: FirestoreControlDatabaseBinding,
    ) -> None:
        if not isinstance(transaction, FirestoreTransaction):
            raise ValueError("Firestore transaction must implement the closed port")
        self._transaction = transaction
        self._binding = binding
        self._cache: dict[str, _CachedDocument | None] = {}
        self._written_keys: set[str] = set()
        self._write_started = False

    def _path(self, key: str) -> tuple[str, ...]:
        if not isinstance(key, str):
            raise TransactionAborted("transaction key is invalid")
        prefix = f"{self._binding.scope.scope_digest}:"
        if not key.startswith(prefix):
            raise TransactionAborted("transaction key scope mismatch")
        remainder = key[len(prefix) :]
        namespace_text, separator, identifier = remainder.partition(":")
        if not separator or not identifier:
            raise TransactionAborted("transaction key shape is invalid")
        try:
            namespace = StoreNamespace(namespace_text)
        except ValueError as error:
            raise TransactionAborted("transaction namespace is invalid") from error

        relative: tuple[str, ...]
        if namespace is StoreNamespace.TRUST_PIN:
            if identifier != "current":
                raise TransactionAborted("trust pin key is invalid")
            relative = ("trust_pins", "current")
        elif namespace is StoreNamespace.CONSUMED_NONCE:
            if not _is_digest(identifier):
                raise TransactionAborted("nonce key is invalid")
            relative = ("consumed_nonces", identifier)
        elif namespace is StoreNamespace.CONSUMED_APPROVAL:
            if not _is_digest(identifier):
                raise TransactionAborted("approval key is invalid")
            relative = ("consumed_approvals", identifier)
        elif namespace is StoreNamespace.BINDING_EPOCH:
            binding_digest, epoch_separator, epoch_text = identifier.partition(":")
            if (
                not epoch_separator
                or not _is_digest(binding_digest)
                or not epoch_text.isdecimal()
                or not _is_exact_int(int(epoch_text), minimum=1)
            ):
                raise TransactionAborted("binding epoch key is invalid")
            relative = ("binding_epochs", binding_digest, "epochs", epoch_text)
        elif namespace is StoreNamespace.CONTROL_RESERVATION:
            if not _is_safe_ref(identifier):
                raise TransactionAborted("control reservation key is invalid")
            relative = ("reservations", identifier)
        else:  # pragma: no cover - closed enum above
            raise TransactionAborted("transaction namespace is unsupported")
        return (*self._binding.root_path, *relative)

    def read(self, key: str) -> StoredRecord | None:
        if key in self._cache:
            cached = self._cache[key]
            return None if cached is None else cached.record
        if self._write_started:
            raise TransactionAborted("Firestore transaction cannot read after write")
        path = self._path(key)
        document = self._transaction.read_document(path)
        if document is None:
            self._cache[key] = None
            return None
        record = _decode_record(document)
        if record is None:
            raise TransactionAborted("stored record envelope is malformed")
        self._cache[key] = _CachedDocument(
            record=record,
            backend_version=document.backend_version,
        )
        return record

    def create(self, key: str, record: StoredRecord) -> bool:
        path = self._path(key)
        if not isinstance(record, StoredRecord) or key in self._written_keys:
            return False
        cached = self._cache.get(key)
        if cached is not None:
            return False
        self._transaction.create_document(
            path,
            _encode_record(record, version=1),
        )
        self._cache[key] = _CachedDocument(record=record, backend_version=None)
        self._written_keys.add(key)
        self._write_started = True
        return True

    def replace(self, key: str, *, expected_version: int, record: StoredRecord) -> bool:
        path = self._path(key)
        cached = self._cache.get(key)
        if (
            not _is_exact_int(expected_version, minimum=1)
            or not isinstance(record, StoredRecord)
            or cached is None
            or cached.backend_version is None
            or cached.record.version != expected_version
            or key in self._written_keys
        ):
            return False
        replacement = StoredRecord(
            version=expected_version + 1,
            fields=record.fields,
        )
        self._transaction.replace_document(
            path,
            expected_backend_version=cached.backend_version,
            envelope=_encode_record(replacement, version=replacement.version),
        )
        self._cache[key] = _CachedDocument(
            record=replacement,
            backend_version=None,
        )
        self._written_keys.add(key)
        self._write_started = True
        return True


class FirestoreTransactionPort(TransactionPort):
    """Unwired execution-control ``TransactionPort`` over a narrow injected runner."""

    def __init__(
        self,
        *,
        runner: FirestoreTransactionRunner,
        binding: FirestoreControlDatabaseBinding,
    ) -> None:
        if not isinstance(runner, FirestoreTransactionRunner) or not isinstance(
            binding, FirestoreControlDatabaseBinding
        ):
            raise ValueError("Firestore port requires a closed runner and binding")
        try:
            runner_binding = runner.binding
        except Exception as error:
            raise ValueError("Firestore runner binding is unavailable") from error
        if (
            not isinstance(runner_binding, FirestoreControlDatabaseBinding)
            or runner_binding != binding
        ):
            raise ValueError("Firestore runner binding does not match attested scope")
        self._runner = runner
        self._binding = binding

    @property
    def binding(self) -> FirestoreControlDatabaseBinding:
        return self._binding

    def document_path_for(self, key: str) -> tuple[str, ...]:
        """Return the deterministic document path without contacting a datastore."""

        view = _FirestoreTransactionView(
            transaction=_PathOnlyFirestoreTransaction(),
            binding=self._binding,
        )
        return view._path(key)

    def transact(self, callback: Callable[[TransactionView], object]) -> TransactionResult:
        if not callable(callback):
            return TransactionResult(status=TransactionStatus.UNKNOWN)

        def attempt(transaction: FirestoreTransaction) -> object:
            try:
                return callback(
                    _FirestoreTransactionView(
                        transaction=transaction,
                        binding=self._binding,
                    )
                )
            except TransactionAborted as error:
                raise _AbortTransaction() from error

        try:
            result = self._runner.run_transaction(attempt)
        except _AbortTransaction:
            return TransactionResult(status=TransactionStatus.ABORTED)
        except Exception:
            return TransactionResult(status=TransactionStatus.UNKNOWN)
        if not isinstance(result, FirestoreRunnerResult):
            return TransactionResult(status=TransactionStatus.UNKNOWN)
        statuses = {
            FirestoreRunnerStatus.COMMITTED: TransactionStatus.COMMITTED,
            FirestoreRunnerStatus.CONFLICT: TransactionStatus.CONFLICT,
            FirestoreRunnerStatus.UNAVAILABLE: TransactionStatus.UNAVAILABLE,
            FirestoreRunnerStatus.UNKNOWN: TransactionStatus.UNKNOWN,
        }
        return TransactionResult(status=statuses[result.status], value=result.value)


class _PathOnlyFirestoreTransaction(FirestoreTransaction):
    """No-I/O helper used only by ``document_path_for``."""

    def read_document(self, path: tuple[str, ...]) -> FirestoreStoredDocument | None:
        raise AssertionError("path mapping must not read a datastore")

    def create_document(
        self,
        path: tuple[str, ...],
        envelope: Mapping[str, object],
    ) -> None:
        raise AssertionError("path mapping must not write a datastore")

    def replace_document(
        self,
        path: tuple[str, ...],
        *,
        expected_backend_version: str,
        envelope: Mapping[str, object],
    ) -> None:
        raise AssertionError("path mapping must not write a datastore")
