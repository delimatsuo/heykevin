"""Pure domain model for versioned service-request workflows.

The aggregate is intentionally persistence- and provider-agnostic.  Callers supply
timestamps, expected revisions, and idempotency keys; repositories are responsible for
atomically storing the returned aggregate.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

SERVICE_REQUEST_SCHEMA_VERSION = 1
MAX_SERVICE_LABEL_LENGTH = 120
MAX_SERVICE_COUNT = 20
MAX_REQUEST_ID_LENGTH = 160
MAX_IDEMPOTENCY_KEY_LENGTH = 200

_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ServiceRequestError(ValueError):
    """Base class for service-request domain failures."""


class ServiceRequestValidationError(ServiceRequestError):
    """Raised when a command or serialized aggregate is malformed."""


class ServiceRequestConcurrencyError(ServiceRequestError):
    """Raised when optimistic-concurrency expectations are stale."""

    def __init__(self, *, expected_revision: int, actual_revision: int):
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        super().__init__(
            f"expected revision {expected_revision}, actual revision {actual_revision}"
        )


class ServiceRequestIdempotencyConflict(ServiceRequestError):
    """Raised when an idempotency key is reused for a different command."""


class ServiceRequestTransitionError(ServiceRequestError):
    """Raised when a command is not legal for the aggregate's current state."""


class ServiceRequestStatus(str, Enum):
    OPEN = "open"
    CANCELLED = "cancelled"


class ServiceRequestOperation(str, Enum):
    CREATE = "create"
    CANCEL = "cancel"
    RESCHEDULE = "reschedule"
    ADD_SERVICE = "add_service"


@dataclass(frozen=True)
class ServiceRequestCommandOutcome:
    """Stable result returned for an applied command and all of its retries."""

    request_id: str
    operation: ServiceRequestOperation
    revision: int
    status: ServiceRequestStatus
    services: tuple[str, ...]
    scheduled_start: datetime
    scheduled_end: datetime
    applied_at: datetime

    def __post_init__(self) -> None:
        _validate_identifier(
            self.request_id,
            field_name="request_id",
            max_length=MAX_REQUEST_ID_LENGTH,
        )
        if not isinstance(self.operation, ServiceRequestOperation):
            raise ServiceRequestValidationError("operation must be a ServiceRequestOperation")
        if not isinstance(self.status, ServiceRequestStatus):
            raise ServiceRequestValidationError("status must be a ServiceRequestStatus")
        _validate_revision(self.revision, field_name="revision", minimum=1)
        _validate_canonical_services(self.services)
        _validate_canonical_schedule(self.scheduled_start, self.scheduled_end)
        _validate_canonical_utc(self.applied_at, field_name="applied_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "operation": self.operation.value,
            "revision": self.revision,
            "status": self.status.value,
            "services": list(self.services),
            "scheduled_start": _serialize_datetime(self.scheduled_start),
            "scheduled_end": _serialize_datetime(self.scheduled_end),
            "applied_at": _serialize_datetime(self.applied_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServiceRequestCommandOutcome:
        _require_exact_keys(
            data,
            {
                "request_id",
                "operation",
                "revision",
                "status",
                "services",
                "scheduled_start",
                "scheduled_end",
                "applied_at",
            },
            field_name="command outcome",
        )
        return cls(
            request_id=_require_text(data["request_id"], field_name="request_id"),
            operation=_parse_operation(data["operation"]),
            revision=_require_integer(data["revision"], field_name="revision"),
            status=_parse_status(data["status"]),
            services=_restore_services(data["services"]),
            scheduled_start=_parse_datetime(data["scheduled_start"], field_name="scheduled_start"),
            scheduled_end=_parse_datetime(data["scheduled_end"], field_name="scheduled_end"),
            applied_at=_parse_datetime(data["applied_at"], field_name="applied_at"),
        )


@dataclass(frozen=True)
class ServiceRequestIdempotencyRecord:
    """A durable command fingerprint and its original stable outcome."""

    idempotency_key: str
    command_fingerprint: str
    outcome: ServiceRequestCommandOutcome

    def __post_init__(self) -> None:
        _validate_identifier(
            self.idempotency_key,
            field_name="idempotency_key",
            max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
        )
        if not isinstance(self.command_fingerprint, str) or not _FINGERPRINT_PATTERN.fullmatch(
            self.command_fingerprint
        ):
            raise ServiceRequestValidationError(
                "command_fingerprint must be a lowercase SHA-256 digest"
            )
        if not isinstance(self.outcome, ServiceRequestCommandOutcome):
            raise ServiceRequestValidationError("outcome must be a ServiceRequestCommandOutcome")

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "command_fingerprint": self.command_fingerprint,
            "outcome": self.outcome.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServiceRequestIdempotencyRecord:
        _require_exact_keys(
            data,
            {"idempotency_key", "command_fingerprint", "outcome"},
            field_name="idempotency record",
        )
        outcome = data["outcome"]
        if not isinstance(outcome, dict):
            raise ServiceRequestValidationError("idempotency record outcome must be an object")
        return cls(
            idempotency_key=_require_text(data["idempotency_key"], field_name="idempotency_key"),
            command_fingerprint=_require_text(
                data["command_fingerprint"], field_name="command_fingerprint"
            ),
            outcome=ServiceRequestCommandOutcome.from_dict(outcome),
        )


@dataclass(frozen=True)
class ServiceRequestCommandResult:
    """Current aggregate plus the stable outcome of the requested command."""

    request: ServiceRequest
    outcome: ServiceRequestCommandOutcome


@dataclass(frozen=True)
class ServiceRequest:
    """Immutable, versioned service-request aggregate."""

    request_id: str
    status: ServiceRequestStatus
    revision: int
    services: tuple[str, ...]
    scheduled_start: datetime
    scheduled_end: datetime
    created_at: datetime
    updated_at: datetime
    idempotency_records: tuple[ServiceRequestIdempotencyRecord, ...]
    schema_version: int = SERVICE_REQUEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != SERVICE_REQUEST_SCHEMA_VERSION
        ):
            raise ServiceRequestValidationError(
                f"unsupported service-request schema version: {self.schema_version}"
            )
        _validate_identifier(
            self.request_id,
            field_name="request_id",
            max_length=MAX_REQUEST_ID_LENGTH,
        )
        if not isinstance(self.status, ServiceRequestStatus):
            raise ServiceRequestValidationError("status must be a ServiceRequestStatus")
        _validate_revision(self.revision, field_name="revision", minimum=1)
        _validate_canonical_services(self.services)
        _validate_canonical_schedule(self.scheduled_start, self.scheduled_end)
        _validate_canonical_utc(self.created_at, field_name="created_at")
        _validate_canonical_utc(self.updated_at, field_name="updated_at")
        if self.updated_at < self.created_at:
            raise ServiceRequestValidationError("updated_at cannot precede created_at")
        self._validate_history()

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        services: Sequence[str],
        scheduled_start: datetime,
        scheduled_end: datetime,
        expected_revision: int,
        idempotency_key: str,
        occurred_at: datetime,
        existing: ServiceRequest | None = None,
    ) -> ServiceRequestCommandResult:
        """Create an aggregate, or replay the same create against an existing one."""

        normalized_request_id = _normalize_identifier(
            request_id,
            field_name="request_id",
            max_length=MAX_REQUEST_ID_LENGTH,
        )
        normalized_services = _normalize_services(services)
        start, end = _normalize_schedule(scheduled_start, scheduled_end)
        revision = _normalize_expected_revision(expected_revision)
        key = _normalize_identifier(
            idempotency_key,
            field_name="idempotency_key",
            max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
        )
        applied_at = _normalize_utc(occurred_at, field_name="occurred_at")
        fingerprint = _command_fingerprint(
            ServiceRequestOperation.CREATE,
            revision,
            {
                "request_id": normalized_request_id,
                "services": list(normalized_services),
                "scheduled_start": _serialize_datetime(start),
                "scheduled_end": _serialize_datetime(end),
            },
        )

        if existing is not None:
            if not isinstance(existing, cls):
                raise TypeError("existing must be a ServiceRequest or None")
            replay = existing._replay(key, fingerprint)
            if replay is not None:
                return replay
            raise ServiceRequestTransitionError("service request already exists")

        if revision != 0:
            raise ServiceRequestConcurrencyError(
                expected_revision=revision,
                actual_revision=0,
            )

        outcome = ServiceRequestCommandOutcome(
            request_id=normalized_request_id,
            operation=ServiceRequestOperation.CREATE,
            revision=1,
            status=ServiceRequestStatus.OPEN,
            services=normalized_services,
            scheduled_start=start,
            scheduled_end=end,
            applied_at=applied_at,
        )
        record = ServiceRequestIdempotencyRecord(
            idempotency_key=key,
            command_fingerprint=fingerprint,
            outcome=outcome,
        )
        request = cls(
            request_id=normalized_request_id,
            status=ServiceRequestStatus.OPEN,
            revision=1,
            services=normalized_services,
            scheduled_start=start,
            scheduled_end=end,
            created_at=applied_at,
            updated_at=applied_at,
            idempotency_records=(record,),
        )
        return ServiceRequestCommandResult(request=request, outcome=outcome)

    def cancel(
        self,
        *,
        expected_revision: int,
        idempotency_key: str,
        occurred_at: datetime,
    ) -> ServiceRequestCommandResult:
        revision = _normalize_expected_revision(expected_revision)
        key = _normalize_identifier(
            idempotency_key,
            field_name="idempotency_key",
            max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
        )
        applied_at = _normalize_utc(occurred_at, field_name="occurred_at")
        fingerprint = _command_fingerprint(
            ServiceRequestOperation.CANCEL,
            revision,
            {},
        )
        replay = self._replay(key, fingerprint)
        if replay is not None:
            return replay
        self._require_revision(revision)
        self._require_open(ServiceRequestOperation.CANCEL)
        return self._next(
            operation=ServiceRequestOperation.CANCEL,
            key=key,
            fingerprint=fingerprint,
            applied_at=applied_at,
            status=ServiceRequestStatus.CANCELLED,
            services=self.services,
            scheduled_start=self.scheduled_start,
            scheduled_end=self.scheduled_end,
        )

    def reschedule(
        self,
        *,
        scheduled_start: datetime,
        scheduled_end: datetime,
        expected_revision: int,
        idempotency_key: str,
        occurred_at: datetime,
    ) -> ServiceRequestCommandResult:
        start, end = _normalize_schedule(scheduled_start, scheduled_end)
        revision = _normalize_expected_revision(expected_revision)
        key = _normalize_identifier(
            idempotency_key,
            field_name="idempotency_key",
            max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
        )
        applied_at = _normalize_utc(occurred_at, field_name="occurred_at")
        fingerprint = _command_fingerprint(
            ServiceRequestOperation.RESCHEDULE,
            revision,
            {
                "scheduled_start": _serialize_datetime(start),
                "scheduled_end": _serialize_datetime(end),
            },
        )
        replay = self._replay(key, fingerprint)
        if replay is not None:
            return replay
        self._require_revision(revision)
        self._require_open(ServiceRequestOperation.RESCHEDULE)
        if start == self.scheduled_start and end == self.scheduled_end:
            raise ServiceRequestTransitionError("reschedule must change the scheduled time")
        return self._next(
            operation=ServiceRequestOperation.RESCHEDULE,
            key=key,
            fingerprint=fingerprint,
            applied_at=applied_at,
            status=self.status,
            services=self.services,
            scheduled_start=start,
            scheduled_end=end,
        )

    def add_service(
        self,
        *,
        service: str,
        expected_revision: int,
        idempotency_key: str,
        occurred_at: datetime,
    ) -> ServiceRequestCommandResult:
        normalized_service = normalize_service_label(service)
        revision = _normalize_expected_revision(expected_revision)
        key = _normalize_identifier(
            idempotency_key,
            field_name="idempotency_key",
            max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
        )
        applied_at = _normalize_utc(occurred_at, field_name="occurred_at")
        fingerprint = _command_fingerprint(
            ServiceRequestOperation.ADD_SERVICE,
            revision,
            {"service": normalized_service},
        )
        replay = self._replay(key, fingerprint)
        if replay is not None:
            return replay
        self._require_revision(revision)
        self._require_open(ServiceRequestOperation.ADD_SERVICE)
        if len(self.services) >= MAX_SERVICE_COUNT:
            raise ServiceRequestValidationError(
                f"a service request may contain at most {MAX_SERVICE_COUNT} services"
            )
        if normalized_service.casefold() in {label.casefold() for label in self.services}:
            raise ServiceRequestTransitionError("service is already present on the request")
        return self._next(
            operation=ServiceRequestOperation.ADD_SERVICE,
            key=key,
            fingerprint=fingerprint,
            applied_at=applied_at,
            status=self.status,
            services=(*self.services, normalized_service),
            scheduled_start=self.scheduled_start,
            scheduled_end=self.scheduled_end,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the fixed-shape schema used by persistence adapters."""

        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "status": self.status.value,
            "revision": self.revision,
            "services": list(self.services),
            "scheduled_start": _serialize_datetime(self.scheduled_start),
            "scheduled_end": _serialize_datetime(self.scheduled_end),
            "created_at": _serialize_datetime(self.created_at),
            "updated_at": _serialize_datetime(self.updated_at),
            "idempotency_records": [
                record.to_dict()
                for record in sorted(
                    self.idempotency_records,
                    key=lambda item: item.outcome.revision,
                )
            ],
        }

    def to_json(self) -> str:
        """Serialize with stable keys, separators, Unicode, and UTC timestamps."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServiceRequest:
        _require_exact_keys(
            data,
            {
                "schema_version",
                "request_id",
                "status",
                "revision",
                "services",
                "scheduled_start",
                "scheduled_end",
                "created_at",
                "updated_at",
                "idempotency_records",
            },
            field_name="service request",
        )
        records = data["idempotency_records"]
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ServiceRequestValidationError("idempotency_records must be a list of objects")
        return cls(
            schema_version=_require_integer(data["schema_version"], field_name="schema_version"),
            request_id=_require_text(data["request_id"], field_name="request_id"),
            status=_parse_status(data["status"]),
            revision=_require_integer(data["revision"], field_name="revision"),
            services=_restore_services(data["services"]),
            scheduled_start=_parse_datetime(data["scheduled_start"], field_name="scheduled_start"),
            scheduled_end=_parse_datetime(data["scheduled_end"], field_name="scheduled_end"),
            created_at=_parse_datetime(data["created_at"], field_name="created_at"),
            updated_at=_parse_datetime(data["updated_at"], field_name="updated_at"),
            idempotency_records=tuple(
                ServiceRequestIdempotencyRecord.from_dict(item) for item in records
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> ServiceRequest:
        if not isinstance(value, str):
            raise TypeError("value must be a JSON string")
        try:
            data = json.loads(value)
        except json.JSONDecodeError as error:
            raise ServiceRequestValidationError("service request JSON is invalid") from error
        if not isinstance(data, dict):
            raise ServiceRequestValidationError("service request JSON must contain an object")
        return cls.from_dict(data)

    def _replay(
        self,
        key: str,
        fingerprint: str,
    ) -> ServiceRequestCommandResult | None:
        record = next(
            (item for item in self.idempotency_records if item.idempotency_key == key),
            None,
        )
        if record is None:
            return None
        if record.command_fingerprint != fingerprint:
            raise ServiceRequestIdempotencyConflict(
                "idempotency key was already used for a different command"
            )
        return ServiceRequestCommandResult(request=self, outcome=record.outcome)

    def _require_revision(self, expected_revision: int) -> None:
        if expected_revision != self.revision:
            raise ServiceRequestConcurrencyError(
                expected_revision=expected_revision,
                actual_revision=self.revision,
            )

    def _require_open(self, operation: ServiceRequestOperation) -> None:
        if self.status is not ServiceRequestStatus.OPEN:
            raise ServiceRequestTransitionError(
                f"cannot {operation.value} a {self.status.value} service request"
            )

    def _next(
        self,
        *,
        operation: ServiceRequestOperation,
        key: str,
        fingerprint: str,
        applied_at: datetime,
        status: ServiceRequestStatus,
        services: tuple[str, ...],
        scheduled_start: datetime,
        scheduled_end: datetime,
    ) -> ServiceRequestCommandResult:
        if applied_at < self.updated_at:
            raise ServiceRequestValidationError("occurred_at cannot precede updated_at")
        next_revision = self.revision + 1
        outcome = ServiceRequestCommandOutcome(
            request_id=self.request_id,
            operation=operation,
            revision=next_revision,
            status=status,
            services=services,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            applied_at=applied_at,
        )
        record = ServiceRequestIdempotencyRecord(
            idempotency_key=key,
            command_fingerprint=fingerprint,
            outcome=outcome,
        )
        request = ServiceRequest(
            request_id=self.request_id,
            status=status,
            revision=next_revision,
            services=services,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            created_at=self.created_at,
            updated_at=applied_at,
            idempotency_records=(*self.idempotency_records, record),
        )
        return ServiceRequestCommandResult(request=request, outcome=outcome)

    def _validate_history(self) -> None:
        if not isinstance(self.idempotency_records, tuple) or any(
            not isinstance(record, ServiceRequestIdempotencyRecord)
            for record in self.idempotency_records
        ):
            raise ServiceRequestValidationError(
                "idempotency_records must be a tuple of ServiceRequestIdempotencyRecord values"
            )
        if len(self.idempotency_records) != self.revision:
            raise ServiceRequestValidationError(
                "revision must equal the number of applied idempotent commands"
            )
        keys = [record.idempotency_key for record in self.idempotency_records]
        if len(keys) != len(set(keys)):
            raise ServiceRequestValidationError("idempotency keys must be unique")

        records = sorted(
            self.idempotency_records,
            key=lambda record: record.outcome.revision,
        )
        if [record.outcome.revision for record in records] != list(range(1, self.revision + 1)):
            raise ServiceRequestValidationError(
                "idempotency outcomes must cover each aggregate revision exactly once"
            )
        if records[0].outcome.operation is not ServiceRequestOperation.CREATE:
            raise ServiceRequestValidationError("revision one must be the create command")

        previous: ServiceRequestCommandOutcome | None = None
        for record in records:
            outcome = record.outcome
            if outcome.request_id != self.request_id:
                raise ServiceRequestValidationError(
                    "idempotency outcome request_id does not match aggregate"
                )
            if not self.created_at <= outcome.applied_at <= self.updated_at:
                raise ServiceRequestValidationError(
                    "idempotency outcome timestamp is outside aggregate history"
                )
            self._validate_history_transition(previous, outcome)
            previous = outcome

        assert previous is not None
        if self.created_at != records[0].outcome.applied_at:
            raise ServiceRequestValidationError("created_at must match the create outcome")
        if self.updated_at != previous.applied_at:
            raise ServiceRequestValidationError("updated_at must match the latest outcome")
        if (
            self.status,
            self.services,
            self.scheduled_start,
            self.scheduled_end,
        ) != (
            previous.status,
            previous.services,
            previous.scheduled_start,
            previous.scheduled_end,
        ):
            raise ServiceRequestValidationError(
                "aggregate state must match the latest command outcome"
            )

    @staticmethod
    def _validate_history_transition(
        previous: ServiceRequestCommandOutcome | None,
        current: ServiceRequestCommandOutcome,
    ) -> None:
        if previous is None:
            if current.revision != 1 or current.status is not ServiceRequestStatus.OPEN:
                raise ServiceRequestValidationError(
                    "create must produce revision one in open state"
                )
            return
        if current.applied_at < previous.applied_at:
            raise ServiceRequestValidationError("command timestamps must be monotonic")
        if previous.status is not ServiceRequestStatus.OPEN:
            raise ServiceRequestValidationError("cancelled service requests are terminal")
        if current.operation is ServiceRequestOperation.CREATE:
            raise ServiceRequestValidationError("create may occur only at revision one")
        if current.operation is ServiceRequestOperation.CANCEL:
            if (
                current.status is not ServiceRequestStatus.CANCELLED
                or current.services != previous.services
                or current.scheduled_start != previous.scheduled_start
                or current.scheduled_end != previous.scheduled_end
            ):
                raise ServiceRequestValidationError("cancel outcome is inconsistent")
            return
        if current.status is not ServiceRequestStatus.OPEN:
            raise ServiceRequestValidationError("non-cancel commands must remain open")
        if current.operation is ServiceRequestOperation.RESCHEDULE:
            if current.services != previous.services or (
                current.scheduled_start,
                current.scheduled_end,
            ) == (
                previous.scheduled_start,
                previous.scheduled_end,
            ):
                raise ServiceRequestValidationError("reschedule outcome is inconsistent")
            return
        if current.operation is ServiceRequestOperation.ADD_SERVICE:
            if (
                current.scheduled_start != previous.scheduled_start
                or current.scheduled_end != previous.scheduled_end
                or len(current.services) != len(previous.services) + 1
                or current.services[:-1] != previous.services
            ):
                raise ServiceRequestValidationError("add-service outcome is inconsistent")
            return
        raise ServiceRequestValidationError("unsupported command operation in history")


def normalize_service_label(value: str) -> str:
    """Return a stable display label while rejecting unsafe or unbounded input."""

    if not isinstance(value, str):
        raise TypeError("service label must be a string")
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ServiceRequestValidationError("service label cannot contain control characters")
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ServiceRequestValidationError("service label must not be empty")
    if len(normalized) > MAX_SERVICE_LABEL_LENGTH:
        raise ServiceRequestValidationError(
            f"service label exceeds {MAX_SERVICE_LABEL_LENGTH} characters"
        )
    return normalized


def _normalize_services(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("services must be a non-string sequence")
    if not values:
        raise ServiceRequestValidationError("at least one service is required")
    if len(values) > MAX_SERVICE_COUNT:
        raise ServiceRequestValidationError(
            f"a service request may contain at most {MAX_SERVICE_COUNT} services"
        )
    normalized = tuple(normalize_service_label(value) for value in values)
    folded = [value.casefold() for value in normalized]
    if len(folded) != len(set(folded)):
        raise ServiceRequestValidationError("service labels must be unique")
    return normalized


def _restore_services(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ServiceRequestValidationError("services must be a list")
    return _normalize_services(value)


def _validate_canonical_services(values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple):
        raise ServiceRequestValidationError("services must be a tuple")
    if _normalize_services(values) != values:
        raise ServiceRequestValidationError("services must contain canonical labels")


def _normalize_schedule(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    normalized_start = _normalize_utc(start, field_name="scheduled_start")
    normalized_end = _normalize_utc(end, field_name="scheduled_end")
    if normalized_end <= normalized_start:
        raise ServiceRequestValidationError("scheduled_end must be after scheduled_start")
    return normalized_start, normalized_end


def _validate_canonical_schedule(start: datetime, end: datetime) -> None:
    _validate_canonical_utc(start, field_name="scheduled_start")
    _validate_canonical_utc(end, field_name="scheduled_end")
    if end <= start:
        raise ServiceRequestValidationError("scheduled_end must be after scheduled_start")


def _normalize_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as error:
        raise ServiceRequestValidationError(f"{field_name} has an invalid timezone") from error
    if value.tzinfo is None or offset is None:
        raise ServiceRequestValidationError(f"{field_name} must be timezone-aware")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as error:
        raise ServiceRequestValidationError(
            f"{field_name} is outside the supported range"
        ) from error


def _validate_canonical_utc(value: datetime, *, field_name: str) -> None:
    normalized = _normalize_utc(value, field_name=field_name)
    if normalized != value or value.utcoffset() != UTC.utcoffset(value):
        raise ServiceRequestValidationError(f"{field_name} must be normalized to UTC")


def _serialize_datetime(value: datetime) -> str:
    _validate_canonical_utc(value, field_name="datetime")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ServiceRequestValidationError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ServiceRequestValidationError(
            f"{field_name} must be a valid ISO-8601 datetime"
        ) from error
    return _normalize_utc(parsed, field_name=field_name)


def _normalize_identifier(value: str, *, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    _validate_identifier(normalized, field_name=field_name, max_length=max_length)
    return normalized


def _validate_identifier(value: str, *, field_name: str, max_length: int) -> None:
    if not isinstance(value, str):
        raise ServiceRequestValidationError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise ServiceRequestValidationError(f"{field_name} must be non-empty and trimmed")
    if len(value) > max_length:
        raise ServiceRequestValidationError(f"{field_name} exceeds {max_length} characters")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ServiceRequestValidationError(f"{field_name} cannot contain control characters")


def _normalize_expected_revision(value: int) -> int:
    return _validate_revision(value, field_name="expected_revision", minimum=0)


def _validate_revision(value: int, *, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ServiceRequestValidationError(f"{field_name} must be at least {minimum}")
    return value


def _command_fingerprint(
    operation: ServiceRequestOperation,
    expected_revision: int,
    payload: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "operation": operation.value,
            "expected_revision": expected_revision,
            "payload": payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_exact_keys(
    data: dict[str, Any],
    expected: set[str],
    *,
    field_name: str,
) -> None:
    if not isinstance(data, dict):
        raise ServiceRequestValidationError(f"{field_name} must be an object")
    if set(data) != expected:
        raise ServiceRequestValidationError(f"{field_name} has an unexpected schema")


def _require_text(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ServiceRequestValidationError(f"{field_name} must be a string")
    return value


def _require_integer(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceRequestValidationError(f"{field_name} must be an integer")
    return value


def _parse_operation(value: Any) -> ServiceRequestOperation:
    if not isinstance(value, str):
        raise ServiceRequestValidationError("operation must be a string")
    try:
        return ServiceRequestOperation(value)
    except ValueError as error:
        raise ServiceRequestValidationError("operation is unsupported") from error


def _parse_status(value: Any) -> ServiceRequestStatus:
    if not isinstance(value, str):
        raise ServiceRequestValidationError("status must be a string")
    try:
        return ServiceRequestStatus(value)
    except ValueError as error:
        raise ServiceRequestValidationError("status is unsupported") from error


__all__ = [
    "MAX_SERVICE_COUNT",
    "MAX_SERVICE_LABEL_LENGTH",
    "SERVICE_REQUEST_SCHEMA_VERSION",
    "ServiceRequest",
    "ServiceRequestCommandOutcome",
    "ServiceRequestCommandResult",
    "ServiceRequestConcurrencyError",
    "ServiceRequestError",
    "ServiceRequestIdempotencyConflict",
    "ServiceRequestOperation",
    "ServiceRequestStatus",
    "ServiceRequestTransitionError",
    "ServiceRequestValidationError",
    "normalize_service_label",
]
