"""Strictly tenant-scoped Firestore adapter for product customer memory."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

from google.cloud import firestore

from app.db.firestore_client import get_firestore_client
from app.services.customer_memory import (
    CustomerMemory,
    CustomerMemoryConflict,
    CustomerMemoryError,
    IdentitySource,
    IdentityState,
    customer_key_for_phone,
    remember_customer,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)
SUBCOLLECTION = "customer_memory"
RECEIPTS_SUBCOLLECTION = "command_receipts"
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_RETENTION_DAYS = 90
LOOKUP_TIMEOUT_SECONDS = 0.75
WRITE_TIMEOUT_SECONDS = 5.0


def _document(db, contractor_id: str, customer_key: str):
    if not contractor_id:
        raise ValueError("contractor_id is required")
    return (
        db.collection("contractors")
        .document(contractor_id)
        .collection(SUBCOLLECTION)
        .document(customer_key)
    )


def _receipt_document(memory_ref, command_id: str):
    if not isinstance(command_id, str) or not command_id:
        raise CustomerMemoryError("command_id is required")
    command_key = hashlib.sha256(command_id.encode("utf-8")).hexdigest()
    return memory_ref.collection(RECEIPTS_SUBCOLLECTION).document(command_key)


def _fingerprint(operation: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"operation": operation, **payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _remember_fingerprint(
    *,
    display_name: str,
    identity_state: IdentityState,
    identity_source: IdentitySource,
    confidence: float,
    language: str,
    expected_revision: int,
) -> str:
    if not isinstance(display_name, str):
        raise CustomerMemoryError("display_name must be text")
    if not isinstance(identity_state, IdentityState):
        raise CustomerMemoryError("identity_state must be an IdentityState")
    if not isinstance(identity_source, IdentitySource):
        raise CustomerMemoryError("identity_source must be an IdentitySource")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise CustomerMemoryError("confidence must be numeric")
    if not isinstance(language, str):
        raise CustomerMemoryError("language must be text")
    return _fingerprint(
        "remember",
        {
            "confidence": float(confidence),
            "display_name": " ".join(display_name.split()),
            "expected_revision": expected_revision,
            "identity_source": identity_source.value,
            "identity_state": identity_state.value,
            "language": language,
        },
    )


def _forget_fingerprint(expected_revision: int) -> str:
    return _fingerprint("forget", {"expected_revision": expected_revision})


def _validate_receipt(snapshot, *, operation: str, fingerprint: str) -> dict | None:
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    if (
        data.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or data.get("operation") != operation
        or data.get("fingerprint") != fingerprint
    ):
        raise CustomerMemoryConflict("command id was reused with different data")
    return data


def _decode_memory(
    snapshot,
    *,
    contractor_id: str,
    customer_key: str,
) -> CustomerMemory | None:
    if not snapshot.exists:
        return None
    memory = CustomerMemory.from_dict(snapshot.to_dict() or {})
    if memory.contractor_id != contractor_id or memory.customer_key != customer_key:
        raise CustomerMemoryConflict("stored memory binding does not match its tenant path")
    return memory


class FirestoreCustomerMemoryRepository:
    """Firestore implementation with no global or legacy read fallback."""

    def __init__(self, client=None) -> None:
        self._client = client

    def _db(self):
        return self._client or get_firestore_client()

    async def lookup(
        self,
        contractor_id: str,
        caller_phone: str,
        now: datetime,
    ) -> CustomerMemory | None:
        customer_key = customer_key_for_phone(caller_phone)
        ref = _document(self._db(), contractor_id, customer_key)
        try:
            snapshot = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, ref.get),
                timeout=LOOKUP_TIMEOUT_SECONDS,
            )
            if not snapshot.exists:
                return None
            memory = _decode_memory(
                snapshot,
                contractor_id=contractor_id,
                customer_key=customer_key,
            )
            assert memory is not None
            return memory if memory.is_greeting_eligible(now) or now < memory.expires_at else None
        except TimeoutError:
            logger.warning("customer_memory lookup timed out")
        except Exception as error:  # noqa: BLE001 - call setup fails open to unknown caller
            logger.warning(
                "customer_memory lookup failed exception_type=%s",
                type(error).__name__,
            )
        return None

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
        customer_key = customer_key_for_phone(caller_phone)
        db = self._db()
        ref = _document(db, contractor_id, customer_key)
        receipt_ref = _receipt_document(ref, command_id)
        fingerprint = _remember_fingerprint(
            display_name=display_name,
            identity_state=identity_state,
            identity_source=identity_source,
            confidence=confidence,
            language=language,
            expected_revision=expected_revision,
        )

        def _write() -> CustomerMemory:
            transaction = db.transaction()

            @firestore.transactional
            def _transaction(tx) -> CustomerMemory:
                receipt_snapshot = receipt_ref.get(transaction=tx)
                snapshot = ref.get(transaction=tx)
                existing = _decode_memory(
                    snapshot,
                    contractor_id=contractor_id,
                    customer_key=customer_key,
                )
                receipt = _validate_receipt(
                    receipt_snapshot,
                    operation="remember",
                    fingerprint=fingerprint,
                )
                if receipt is not None:
                    if existing is None:
                        raise CustomerMemoryConflict("remember command was superseded by forget")
                    # Receipts intentionally contain no PII outcome. Returning the
                    # current aggregate makes an old retry stable without rolling
                    # later customer-memory revisions backward.
                    return existing
                updated = remember_customer(
                    existing,
                    contractor_id=contractor_id,
                    customer_key=customer_key,
                    display_name=display_name,
                    identity_state=identity_state,
                    identity_source=identity_source,
                    confidence=confidence,
                    language=language,
                    expected_revision=expected_revision,
                    command_id=command_id,
                    occurred_at=occurred_at,
                )
                if updated is not existing:
                    tx.set(ref, updated.to_dict())
                tx.set(
                    receipt_ref,
                    {
                        "schema_version": RECEIPT_SCHEMA_VERSION,
                        "operation": "remember",
                        "fingerprint": fingerprint,
                        "result_revision": updated.revision,
                        "created_at": updated.updated_at,
                        "expires_at": updated.expires_at,
                    },
                )
                return updated

            return _transaction(transaction)

        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _write),
            timeout=WRITE_TIMEOUT_SECONDS,
        )

    async def forget(
        self,
        contractor_id: str,
        caller_phone: str,
        *,
        expected_revision: int,
        command_id: str,
    ) -> bool:
        customer_key = customer_key_for_phone(caller_phone)
        db = self._db()
        ref = _document(db, contractor_id, customer_key)
        receipt_ref = _receipt_document(ref, command_id)
        fingerprint = _forget_fingerprint(expected_revision)
        receipt_created_at = datetime.now(UTC)
        receipt_expires_at = receipt_created_at + timedelta(days=RECEIPT_RETENTION_DAYS)

        def _delete() -> bool:
            transaction = db.transaction()

            @firestore.transactional
            def _transaction(tx) -> bool:
                receipt_snapshot = receipt_ref.get(transaction=tx)
                snapshot = ref.get(transaction=tx)
                receipt = _validate_receipt(
                    receipt_snapshot,
                    operation="forget",
                    fingerprint=fingerprint,
                )
                if receipt is not None:
                    forgotten = receipt.get("forgotten")
                    if not isinstance(forgotten, bool):
                        raise CustomerMemoryConflict("forget receipt has an invalid outcome")
                    return forgotten

                memory = _decode_memory(
                    snapshot,
                    contractor_id=contractor_id,
                    customer_key=customer_key,
                )
                if memory is None:
                    if expected_revision != 0:
                        raise CustomerMemoryConflict(
                            f"expected revision {expected_revision}, actual 0"
                        )
                    tx.set(
                        receipt_ref,
                        {
                            "schema_version": RECEIPT_SCHEMA_VERSION,
                            "operation": "forget",
                            "fingerprint": fingerprint,
                            "forgotten": False,
                            "created_at": receipt_created_at,
                            "expires_at": receipt_expires_at,
                        },
                    )
                    return False
                if memory.revision != expected_revision:
                    raise CustomerMemoryConflict(
                        f"expected revision {expected_revision}, actual {memory.revision}"
                    )
                tx.set(
                    receipt_ref,
                    {
                        "schema_version": RECEIPT_SCHEMA_VERSION,
                        "operation": "forget",
                        "fingerprint": fingerprint,
                        "forgotten": True,
                        "created_at": receipt_created_at,
                        "expires_at": receipt_expires_at,
                    },
                )
                tx.delete(ref)
                return True

            return _transaction(transaction)

        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _delete),
            timeout=WRITE_TIMEOUT_SECONDS,
        )
