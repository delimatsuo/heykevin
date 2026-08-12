"""Product-owned, tenant-scoped memory for returning callers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol

from app.utils.phone import normalize_phone, phone_hash

SCHEMA_VERSION = 1
MEMORY_RETENTION_DAYS = 90
MIN_GREETING_CONFIDENCE = 0.85


class CustomerMemoryError(ValueError):
    """Base class for memory validation and transition failures."""


class CustomerMemoryConflict(CustomerMemoryError):
    """Raised for stale revisions or conflicting command re-use."""


class IdentityState(str, Enum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    CONFLICTED = "conflicted"


class IdentitySource(str, Enum):
    CALLER_CONFIRMED = "caller_confirmed"
    OWNER_CONTACT = "owner_contact"
    PROVIDER_EXACT_MATCH = "provider_exact_match"
    TRANSCRIPT_EXTRACTED = "transcript_extracted"


@dataclass(frozen=True, slots=True)
class CustomerMemory:
    contractor_id: str
    customer_key: str
    display_name: str
    identity_state: IdentityState
    identity_source: IdentitySource
    confidence: float
    language: str
    revision: int
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    last_command_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != SCHEMA_VERSION:
            raise CustomerMemoryError("unsupported customer-memory schema")
        if not isinstance(self.contractor_id, str) or not self.contractor_id:
            raise CustomerMemoryError("tenant and customer keys are required")
        if not isinstance(self.customer_key, str) or not self.customer_key:
            raise CustomerMemoryError("tenant and customer keys are required")
        if not isinstance(self.identity_state, IdentityState):
            raise CustomerMemoryError("identity_state must be an IdentityState")
        if not isinstance(self.identity_source, IdentitySource):
            raise CustomerMemoryError("identity_source must be an IdentitySource")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise CustomerMemoryError("invalid revision or confidence")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise CustomerMemoryError("invalid revision or confidence")
        if self.revision < 1 or not 0 <= self.confidence <= 1:
            raise CustomerMemoryError("invalid revision or confidence")
        if not isinstance(self.display_name, str) or not isinstance(self.language, str):
            raise CustomerMemoryError("memory fields must be text")
        if len(self.display_name) > 80 or len(self.language) > 16:
            raise CustomerMemoryError("memory field exceeds its bound")
        if not isinstance(self.last_command_id, str) or not self.last_command_id:
            raise CustomerMemoryError("last_command_id is required")
        for value in (self.created_at, self.updated_at, self.last_seen_at, self.expires_at):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise CustomerMemoryError("timestamps must be timezone-aware")
            if value.utcoffset() != timedelta(0):
                raise CustomerMemoryError("timestamps must use UTC")
        if not self.created_at <= self.updated_at <= self.last_seen_at < self.expires_at:
            raise CustomerMemoryError("memory timestamps are inconsistent")

    def is_greeting_eligible(self, now: datetime) -> bool:
        now = _utc(now)
        return (
            bool(self.display_name)
            and self.identity_state is IdentityState.CONFIRMED
            and self.confidence >= MIN_GREETING_CONFIDENCE
            and now < self.expires_at
        )

    def greeting_name(self, now: datetime) -> str:
        return self.display_name if self.is_greeting_eligible(now) else ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "contractor_id": self.contractor_id,
            "customer_key": self.customer_key,
            "display_name": self.display_name,
            "identity_state": self.identity_state.value,
            "identity_source": self.identity_source.value,
            "confidence": self.confidence,
            "language": self.language,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_seen_at": self.last_seen_at,
            "expires_at": self.expires_at,
            "last_command_id": self.last_command_id,
        }

    @classmethod
    def from_dict(cls, value: dict) -> CustomerMemory:
        return cls(
            schema_version=value.get("schema_version", 0),
            contractor_id=value.get("contractor_id", ""),
            customer_key=value.get("customer_key", ""),
            display_name=value.get("display_name", ""),
            identity_state=IdentityState(value.get("identity_state", "candidate")),
            identity_source=IdentitySource(value.get("identity_source", "transcript_extracted")),
            confidence=float(value.get("confidence", 0)),
            language=value.get("language", ""),
            revision=int(value.get("revision", 0)),
            created_at=_utc(value.get("created_at")),
            updated_at=_utc(value.get("updated_at")),
            last_seen_at=_utc(value.get("last_seen_at")),
            expires_at=_utc(value.get("expires_at")),
            last_command_id=value.get("last_command_id", ""),
        )


class CustomerMemoryRepository(Protocol):
    async def lookup(
        self, contractor_id: str, caller_phone: str, now: datetime
    ) -> CustomerMemory | None: ...

    async def remember(
        self,
        contractor_id: str,
        caller_phone: str,
        *,
        display_name: str,
        identity_state: IdentityState,
        identity_source: IdentitySource,
        confidence: float,
        language: str,
        expected_revision: int,
        command_id: str,
        occurred_at: datetime,
    ) -> CustomerMemory: ...

    async def forget(
        self,
        contractor_id: str,
        caller_phone: str,
        *,
        expected_revision: int,
        command_id: str,
    ) -> bool: ...


def customer_key_for_phone(caller_phone: str) -> str:
    normalized = normalize_phone(caller_phone)
    if not normalized:
        raise CustomerMemoryError("caller phone must be valid E.164")
    return phone_hash(normalized)


def _clean_name(value: str) -> str:
    if not isinstance(value, str):
        raise CustomerMemoryError("display_name must be text")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > 80 or any(ord(char) < 32 for char in cleaned):
        raise CustomerMemoryError("display_name is invalid")
    return cleaned


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CustomerMemoryError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def remember_customer(
    existing: CustomerMemory | None,
    *,
    contractor_id: str,
    customer_key: str,
    display_name: str,
    identity_state: IdentityState,
    identity_source: IdentitySource,
    confidence: float,
    language: str,
    expected_revision: int,
    command_id: str,
    occurred_at: datetime,
) -> CustomerMemory:
    """Apply one deterministic memory observation for any repository adapter."""
    if not contractor_id or not customer_key or not command_id:
        raise CustomerMemoryError("contractor_id, customer_key, and command_id are required")
    occurred_at = _utc(occurred_at)
    name = _clean_name(display_name)
    if existing and (
        existing.contractor_id != contractor_id or existing.customer_key != customer_key
    ):
        raise CustomerMemoryConflict("memory binding does not match the requested customer")
    # Firestore TTL deletion is asynchronous. Once a correctly bound record has
    # expired it is logically absent, even if the physical document has not
    # been swept yet, so a new observation can start at revision one.
    if existing is not None and occurred_at >= existing.expires_at:
        existing = None
    if existing and existing.last_command_id == command_id:
        if (
            existing.display_name == name
            and existing.identity_state is identity_state
            and existing.identity_source is identity_source
            and existing.confidence == confidence
            and existing.language == language
        ):
            return existing
        raise CustomerMemoryConflict("command id was reused with different data")
    actual_revision = existing.revision if existing else 0
    if expected_revision != actual_revision:
        raise CustomerMemoryConflict(
            f"expected revision {expected_revision}, actual {actual_revision}"
        )
    if existing and occurred_at < existing.updated_at:
        raise CustomerMemoryConflict("observation cannot precede the current memory revision")
    if (
        existing
        and existing.identity_state is IdentityState.CONFIRMED
        and identity_state is IdentityState.CONFIRMED
        and existing.display_name.casefold() != name.casefold()
    ):
        identity_state = IdentityState.CONFLICTED
        confidence = min(float(confidence), existing.confidence)
    if existing:
        return replace(
            existing,
            display_name=name,
            identity_state=identity_state,
            identity_source=identity_source,
            confidence=confidence,
            language=language,
            revision=existing.revision + 1,
            updated_at=occurred_at,
            last_seen_at=occurred_at,
            expires_at=occurred_at + timedelta(days=MEMORY_RETENTION_DAYS),
            last_command_id=command_id,
        )
    return CustomerMemory(
        contractor_id=contractor_id,
        customer_key=customer_key,
        display_name=name,
        identity_state=identity_state,
        identity_source=identity_source,
        confidence=confidence,
        language=language,
        revision=1,
        created_at=occurred_at,
        updated_at=occurred_at,
        last_seen_at=occurred_at,
        expires_at=occurred_at + timedelta(days=MEMORY_RETENTION_DAYS),
        last_command_id=command_id,
    )


class InMemoryCustomerMemoryRepository:
    """Concurrency-safe reference adapter used by tests and local composition."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], CustomerMemory] = {}
        self._receipts: dict[
            tuple[str, str, str], tuple[tuple[object, ...], CustomerMemory | bool | None]
        ] = {}
        self._lock = asyncio.Lock()

    async def lookup(
        self, contractor_id: str, caller_phone: str, now: datetime
    ) -> CustomerMemory | None:
        key = (contractor_id, customer_key_for_phone(caller_phone))
        record = self._records.get(key)
        return record if record and _utc(now) < record.expires_at else None

    async def remember(
        self,
        contractor_id: str,
        caller_phone: str,
        *,
        display_name: str,
        identity_state: IdentityState,
        identity_source: IdentitySource,
        confidence: float,
        language: str = "",
        expected_revision: int,
        command_id: str,
        occurred_at: datetime,
    ) -> CustomerMemory:
        if not contractor_id or not command_id:
            raise CustomerMemoryError("contractor_id and command_id are required")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise CustomerMemoryError("expected_revision must be a non-negative integer")
        if not isinstance(identity_state, IdentityState):
            raise CustomerMemoryError("identity_state must be an IdentityState")
        if not isinstance(identity_source, IdentitySource):
            raise CustomerMemoryError("identity_source must be an IdentitySource")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise CustomerMemoryError("confidence must be numeric")
        if not isinstance(language, str):
            raise CustomerMemoryError("language must be text")
        customer_key = customer_key_for_phone(caller_phone)
        key = (contractor_id, customer_key)
        receipt_key = (*key, command_id)
        name = _clean_name(display_name)
        fingerprint = (
            "remember",
            expected_revision,
            name,
            identity_state.value,
            identity_source.value,
            float(confidence),
            language,
        )
        async with self._lock:
            receipt = self._receipts.get(receipt_key)
            if receipt:
                saved_fingerprint, result = receipt
                if saved_fingerprint != fingerprint:
                    raise CustomerMemoryConflict("command id was reused with different data")
                if not isinstance(result, CustomerMemory):
                    raise CustomerMemoryConflict("command was superseded by forget")
                return result
            existing = self._records.get(key)
            record = remember_customer(
                existing,
                contractor_id=contractor_id,
                customer_key=customer_key,
                display_name=name,
                identity_state=identity_state,
                identity_source=identity_source,
                confidence=confidence,
                language=language,
                expected_revision=expected_revision,
                command_id=command_id,
                occurred_at=occurred_at,
            )
            self._records[key] = record
            self._receipts[receipt_key] = (fingerprint, record)
            return record

    async def forget(
        self,
        contractor_id: str,
        caller_phone: str,
        *,
        expected_revision: int,
        command_id: str,
    ) -> bool:
        if not contractor_id or not command_id:
            raise CustomerMemoryError("contractor_id and command_id are required")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise CustomerMemoryError("expected_revision must be a non-negative integer")
        key = (contractor_id, customer_key_for_phone(caller_phone))
        receipt_key = (*key, command_id)
        fingerprint = ("forget", expected_revision)
        async with self._lock:
            receipt = self._receipts.get(receipt_key)
            if receipt:
                saved_fingerprint, result = receipt
                if saved_fingerprint != fingerprint:
                    raise CustomerMemoryConflict("command id was reused with different data")
                if not isinstance(result, bool):
                    raise CustomerMemoryConflict("command id was reused for a different operation")
                return result

            existing = self._records.get(key)
            actual_revision = existing.revision if existing else 0
            if expected_revision != actual_revision:
                raise CustomerMemoryConflict(
                    f"expected revision {expected_revision}, actual {actual_revision}"
                )
            forgotten = self._records.pop(key, None) is not None
            # Keep fingerprints, not memory values, so a delayed retry cannot
            # recreate erased PII and this adapter no longer holds that PII.
            for saved_key, (saved_fingerprint, result) in tuple(self._receipts.items()):
                if saved_key[:2] == key and isinstance(result, CustomerMemory):
                    self._receipts[saved_key] = (saved_fingerprint, None)
            self._receipts[receipt_key] = (fingerprint, forgotten)
            return forgotten
