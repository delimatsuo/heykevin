"""Unwired Google Firestore transaction runner for isolated bakeoff control data.

This is the only bakeoff module that imports the Google Firestore SDK.  Import
and construction are inert: the runner neither creates a client nor discovers
credentials, configuration, environment, files, metadata, or a network target.
It accepts one pre-attested client capability and remains absent from every
runtime composition path. Tests use fake SDK-shaped clients and executors only.

The runner supports one execution-control database. It does not compose the
physically separate pre-auth store, create a distributed transaction, authorize
Task 4.8, or grant any cloud authority.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
import dataclasses
import re

from google.api_core import exceptions as google_exceptions
from google.cloud import firestore

from .voice_bakeoff_firestore_transaction_port import (
    FirestoreControlDatabaseBinding,
    FirestoreRunnerResult,
    FirestoreRunnerStatus,
    FirestoreStoredDocument,
    FirestoreTransaction,
    FirestoreTransactionAbort,
    FirestoreTransactionRunner,
)
from .voice_bakeoff_transactional_control_seam import TransactionAborted


_DOCUMENT_SEGMENT = re.compile(r"^[a-z][a-z0-9_]{0,1499}$")
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,62}$")
_DATABASE_ID = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_REFERENCE = re.compile(r"^ref_[a-z0-9_]{1,123}$")


def _is_document_path(path: object) -> bool:
    return (
        isinstance(path, tuple)
        and len(path) >= 4
        and len(path) % 2 == 0
        and all(
            isinstance(segment, str)
            and _DOCUMENT_SEGMENT.fullmatch(segment) is not None
            for segment in path
        )
    )


@dataclasses.dataclass(frozen=True, slots=True)
class GoogleFirestoreTargetAttestation:
    """Immutable target mapping with the opaque source that attests that mapping."""

    binding: FirestoreControlDatabaseBinding
    project_id: str
    database_id: str
    attestation_ref: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.binding, FirestoreControlDatabaseBinding)
            or not isinstance(self.project_id, str)
            or _PROJECT_ID.fullmatch(self.project_id) is None
            or not isinstance(self.database_id, str)
            or _DATABASE_ID.fullmatch(self.database_id) is None
            or self.database_id == "default"
            or not isinstance(self.attestation_ref, str)
            or _REFERENCE.fullmatch(self.attestation_ref) is None
        ):
            raise ValueError("Google Firestore target attestation is invalid")


@dataclasses.dataclass(frozen=True, slots=True, init=False)
class GoogleFirestoreClientHandle:
    """Frozen client-plus-target capability; the raw client remains private."""

    _client: object
    _target: GoogleFirestoreTargetAttestation

    def __init__(
        self,
        *,
        client: object,
        target: GoogleFirestoreTargetAttestation,
    ) -> None:
        if not isinstance(target, GoogleFirestoreTargetAttestation):
            raise ValueError("Google Firestore client handle is invalid")
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_target", target)
        self._validate_client_target(construction=True)

    def _validate_client_target(self, *, construction: bool) -> None:
        try:
            client_project = self._client.project
            client_database = self._client._database
        except Exception as error:
            if construction:
                raise ValueError("Google Firestore client handle is invalid") from error
            raise TransactionAborted("Google Firestore client target is invalid") from error
        if (
            client_project != self._target.project_id
            or client_database != self._target.database_id
        ):
            if construction:
                raise ValueError(
                    "Google Firestore client target does not match target attestation"
                )
            raise TransactionAborted("Google Firestore client target changed")

    @property
    def binding(self) -> FirestoreControlDatabaseBinding:
        return self._target.binding

    def new_transaction(self, *, max_attempts: int) -> object:
        self._validate_client_target(construction=False)
        return self._client.transaction(max_attempts=max_attempts)

    def document_reference(self, path: tuple[str, ...]) -> object:
        self._validate_client_target(construction=False)
        if (
            not _is_document_path(path)
            or path[:2] != self._target.binding.root_path
        ):
            raise TransactionAborted("Google Firestore document path is invalid")
        return self._client.document(*path)

    def write_option(self, *, update_time: object) -> object:
        self._validate_client_target(construction=False)
        if update_time is None:
            raise TransactionAborted("Google Firestore update version is missing")
        return self._client.write_option(last_update_time=update_time)


class GoogleFirestoreTransactionalExecutor(ABC):
    """Single retry owner around one vendor transaction object."""

    @abstractmethod
    def execute(
        self,
        transaction: object,
        callback: Callable[[object], object],
    ) -> object:
        """Run a callback and return its value only after the vendor confirms commit."""

        raise NotImplementedError


class _SdkTransactionalExecutor(GoogleFirestoreTransactionalExecutor):
    """Direct wrapper over the SDK transactional decorator; no client construction."""

    def execute(
        self,
        transaction: object,
        callback: Callable[[object], object],
    ) -> object:
        @firestore.transactional
        def run(vendor_transaction: object) -> object:
            return callback(vendor_transaction)

        return run(transaction)


class _GoogleFirestoreTransaction(FirestoreTransaction):
    """Closed Firestore operation mapper for one SDK transaction callback attempt."""

    def __init__(
        self,
        *,
        handle: GoogleFirestoreClientHandle,
        transaction: object,
    ) -> None:
        self._handle = handle
        self._transaction = transaction
        self._backend_versions: dict[str, tuple[tuple[str, ...], object]] = {}
        self._next_backend_version = 0

    def read_document(
        self,
        path: tuple[str, ...],
    ) -> FirestoreStoredDocument | None:
        reference = self._handle.document_reference(path)
        try:
            snapshots = iter(self._transaction.get(reference))
            snapshot = next(snapshots)
        except StopIteration as error:
            raise TransactionAborted("Google Firestore read returned no snapshot") from error
        try:
            next(snapshots)
        except StopIteration:
            pass
        else:
            raise TransactionAborted("Google Firestore read returned multiple snapshots")
        if type(snapshot.exists) is not bool:
            raise TransactionAborted("Google Firestore snapshot existence is invalid")
        if not snapshot.exists:
            return None
        try:
            envelope = snapshot.to_dict()
            update_time = snapshot.update_time
        except Exception as error:
            raise TransactionAborted("Google Firestore snapshot is malformed") from error
        if not isinstance(envelope, Mapping) or update_time is None:
            raise TransactionAborted("Google Firestore snapshot is malformed")
        self._next_backend_version += 1
        backend_version = f"version_{self._next_backend_version}"
        self._backend_versions[backend_version] = (path, update_time)
        return FirestoreStoredDocument(
            backend_version=backend_version,
            envelope=dict(envelope),
        )

    def create_document(
        self,
        path: tuple[str, ...],
        envelope: Mapping[str, object],
    ) -> None:
        reference = self._handle.document_reference(path)
        if not isinstance(envelope, Mapping):
            raise TransactionAborted("Google Firestore create envelope is invalid")
        self._transaction.create(reference, dict(envelope))

    def replace_document(
        self,
        path: tuple[str, ...],
        *,
        expected_backend_version: str,
        envelope: Mapping[str, object],
    ) -> None:
        prior = self._backend_versions.get(expected_backend_version)
        if (
            prior is None
            or prior[0] != path
            or not isinstance(envelope, Mapping)
        ):
            raise TransactionAborted("Google Firestore replacement is invalid")
        reference = self._handle.document_reference(path)
        self._transaction.update(
            reference,
            dict(envelope),
            option=self._handle.write_option(update_time=prior[1]),
        )


class GoogleFirestoreTransactionRunner(FirestoreTransactionRunner):
    """Vendor runner behind the source-only ``FirestoreTransactionRunner`` contract."""

    def __init__(
        self,
        *,
        handle: GoogleFirestoreClientHandle,
        max_attempts: int = 3,
        executor: GoogleFirestoreTransactionalExecutor | None = None,
    ) -> None:
        if (
            not isinstance(handle, GoogleFirestoreClientHandle)
            or type(max_attempts) is not int
            or not 1 <= max_attempts <= 5
            or (
                executor is not None
                and not isinstance(executor, GoogleFirestoreTransactionalExecutor)
            )
        ):
            raise ValueError("Google Firestore transaction runner inputs are invalid")
        self._handle = handle
        self._max_attempts = max_attempts
        self._executor = executor or _SdkTransactionalExecutor()

    @property
    def binding(self) -> FirestoreControlDatabaseBinding:
        return self._handle.binding

    def run_transaction(
        self,
        callback: Callable[[FirestoreTransaction], object],
    ) -> FirestoreRunnerResult:
        if not callable(callback):
            return FirestoreRunnerResult(status=FirestoreRunnerStatus.UNKNOWN)

        def run(vendor_transaction: object) -> object:
            return callback(
                _GoogleFirestoreTransaction(
                    handle=self._handle,
                    transaction=vendor_transaction,
                )
            )

        try:
            value = self._executor.execute(
                self._handle.new_transaction(max_attempts=self._max_attempts),
                run,
            )
        except FirestoreTransactionAbort:
            raise
        except google_exceptions.Aborted:
            return FirestoreRunnerResult(status=FirestoreRunnerStatus.CONFLICT)
        except google_exceptions.Conflict:
            return FirestoreRunnerResult(status=FirestoreRunnerStatus.CONFLICT)
        except (
            google_exceptions.DeadlineExceeded,
            google_exceptions.Forbidden,
            google_exceptions.PermissionDenied,
            google_exceptions.ServiceUnavailable,
            google_exceptions.Unauthenticated,
        ):
            return FirestoreRunnerResult(status=FirestoreRunnerStatus.UNAVAILABLE)
        except ValueError as error:
            if isinstance(error.__cause__, google_exceptions.Aborted):
                return FirestoreRunnerResult(status=FirestoreRunnerStatus.CONFLICT)
            return FirestoreRunnerResult(status=FirestoreRunnerStatus.UNKNOWN)
        except Exception:
            return FirestoreRunnerResult(status=FirestoreRunnerStatus.UNKNOWN)
        return FirestoreRunnerResult(
            status=FirestoreRunnerStatus.COMMITTED,
            value=value,
        )
