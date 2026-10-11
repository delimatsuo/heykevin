"""Durable Firestore store for Jobber provider disconnect control, barriers, receipts, and work.

Invariants:
- Fixed control document: jobber_disconnect_control/current (schema version 1, expected app ID,
  bounded monotonic global receipt counter, active enforcement marker, server timestamp).
- Account barrier: jobber_account_barriers/{key} (domain-separated JSON tuple SHA-256 hash of app_id & account_id,
  monotonic barrier counter <= control counter, server timestamp).
- Disconnect receipt: jobber_disconnect_receipts/{admission_id} (strict closed schema, delivery
  fingerprint, app/account, occurredAt, received timestamp, allocated receipt counter).
- Durable recovery work: jobber_disconnect_work/{work_id} (per-account work record with strict leases,
  server-time expiry, cursor, next-attempt timestamp, capped retry attempts, needs-review status).
- Durable recovery audit: jobber_provider_suspension_audit/{audit_id} (closed schema, binding_suspended=True,
  credentials_retained=True, capture_disabled=True, no provider revocation attempted).
- Zero credentials, tokens, authorization codes, secret keys, or provider response bodies are persisted or logged.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
from typing import Any, Optional
import unicodedata

from google.cloud import firestore
from app.db.jobber_disconnect_transactions import (
    Deadline, JobberDisconnectError, extract_snapshot_time, read_document, run_transaction,
)

from app.db.firestore_client import get_firestore_client
from app.utils.logging import get_logger

logger = get_logger(__name__)

CONTROL_COLLECTION = "jobber_disconnect_control"
CONTROL_DOC_ID = "current"
BARRIERS_COLLECTION = "jobber_account_barriers"
RECEIPTS_COLLECTION = "jobber_disconnect_receipts"
WORK_COLLECTION = "jobber_disconnect_work"
SCAN_CURSOR_COLLECTION = "jobber_disconnect_scan_cursor"
SCAN_CURSOR_DOC_ID = "current"
SUSPENSION_AUDIT_COLLECTION = "jobber_provider_suspension_audit"

SCHEMA_VERSION = 1
MAX_RECEIPT_COUNTER = 2147483647  # 31-bit signed integer maximum
MAX_KEY_VERSION = 2147483647
_HEX_64_REGEX = re.compile(r"^[0-9a-f]{64}\Z")
_ADMISSION_ID_REGEX = re.compile(r"^[0-9a-zA-Z_-]{1,128}\Z")

FORBIDDEN_PERSISTENCE_KEYS = frozenset({
    "access_token",
    "refresh_token",
    "token",
    "client_secret",
    "secret",
    "ciphertext",
    "auth_code",
    "authorization_code",
    "code",
    "iv",
    "tag",
    "raw_body",
    "raw",
    "headers",
    "signature",
})

VALID_WORK_STATUSES = frozenset({
    "pending",
    "processing",
    "completed",
    "needs_review",
})

_RFC3339_REGEX = re.compile(
    r"\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])[Tt]"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?"
    r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
    re.ASCII,
)


class JobberDisconnectControlError(JobberDisconnectError):
    """Raised when the Jobber disconnect control record is missing, malformed, or invalid."""


class JobberAccountBarrierError(JobberDisconnectError):
    """Raised when a Jobber account barrier record is malformed or invalid."""


class JobberDisconnectReceiptError(JobberDisconnectError):
    """Raised when a Jobber disconnect receipt record is malformed or invalid."""


class JobberDisconnectWorkError(JobberDisconnectError):
    """Raised when a Jobber disconnect work record is malformed or invalid."""


class JobberSuspensionAuditError(JobberDisconnectError):
    """Raised when a Jobber provider suspension audit record is malformed or invalid."""


class JobberLifecycleOverflowError(JobberDisconnectError):
    """Raised when a contractor generation or lifecycle epoch reaches max key version."""


def is_valid_opaque_id(value: Any) -> bool:
    """Validate opaque ID string.

    Must be exact str, 1..1024 Unicode code points, containing no whitespace
    or Unicode control category Cc characters.
    """
    if type(value) is not str:
        return False
    if not (1 <= len(value) <= 1024):
        return False
    for c in value:
        if c.isspace() or unicodedata.category(c) == "Cc":
            return False
    return True


def validate_rfc3339_timestamp(value: Any) -> str | None:
    """Validate RFC 3339 timestamp format and calendar validity."""
    if type(value) is not str:
        return None
    if not _RFC3339_REGEX.fullmatch(value):
        return None
    try:
        year = int(value[0:4])
        month = int(value[5:7])
        day = int(value[8:10])
        hour = int(value[11:13])
        minute = int(value[14:16])
        second = int(value[17:19])
        datetime.datetime(year, month, day, hour, minute, second)
        tz_part = value[19:]
        idx = max(tz_part.rfind("+"), tz_part.rfind("-"))
        if idx != -1:
            tz_str = tz_part[idx:]
            sign = 1 if tz_str[0] == "+" else -1
            tz_hour = int(tz_str[1:3])
            tz_min = int(tz_str[4:6])
            if not (0 <= tz_hour <= 23 and 0 <= tz_min <= 59):
                return None
            datetime.timezone(sign * datetime.timedelta(hours=tz_hour, minutes=tz_min))
        elif not (value.endswith("Z") or value.endswith("z")):
            return None
    except (ValueError, OverflowError):
        return None
    return value


def is_firestore_document_id(value: Any) -> bool:
    """A legal single Firestore path segment, including spaces in poison IDs."""
    if type(value) is not str:
        return False
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return (1 <= length <= 1500 and value not in (".", "..") and "/" not in value
            and not (value.startswith("__") and value.endswith("__")))


def is_safe_document_id(value: Any) -> bool:
    return (type(value) is str and 1 <= len(value.encode("utf-8", errors="replace")) <= 1500
            and value not in (".", "..") and "/" not in value
            and not (value.startswith("__") and value.endswith("__"))
            and all(not c.isspace() and unicodedata.category(c) != "Cc" for c in value))


def compute_account_barrier_key(app_id: str, account_id: str) -> str:
    """Return deterministic domain-separated SHA-256 hex digest for an app/account pair using JSON tuple encoding."""
    if not is_valid_opaque_id(app_id):
        raise ValueError("app_id must be valid opaque ID")
    if not is_valid_opaque_id(account_id):
        raise ValueError("account_id must be valid opaque ID")
    encoded = json.dumps(["jobber_account_barrier", "v1", app_id, account_id], separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_work_doc_id(app_id: str, account_id: str) -> str:
    """Return deterministic domain-separated SHA-256 hex digest for a work record using JSON tuple encoding."""
    if not is_valid_opaque_id(app_id):
        raise ValueError("app_id must be valid opaque ID")
    if not is_valid_opaque_id(account_id):
        raise ValueError("account_id must be valid opaque ID")
    encoded = json.dumps(["jobber_disconnect_work", "v1", app_id, account_id], separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_suspension_audit_doc_id(
    contractor_id: str,
    app_id: str,
    account_id: str,
    barrier: int,
    generation: int,
    lifecycle_epoch: int,
) -> str:
    """Return deterministic domain-separated SHA-256 hex digest for a provider suspension audit record."""
    if not is_valid_opaque_id(contractor_id):
        raise ValueError("contractor_id must be valid opaque ID")
    if not is_valid_opaque_id(app_id):
        raise ValueError("app_id must be valid opaque ID")
    if not is_valid_opaque_id(account_id):
        raise ValueError("account_id must be valid opaque ID")
    if type(barrier) is not int or type(barrier) is bool or not (1 <= barrier <= MAX_RECEIPT_COUNTER):
        raise ValueError("barrier must be bounded integer")
    if type(generation) is not int or type(generation) is bool or not (0 <= generation <= MAX_KEY_VERSION):
        raise ValueError("generation must be bounded integer")
    if type(lifecycle_epoch) is not int or type(lifecycle_epoch) is bool or not (0 <= lifecycle_epoch <= MAX_KEY_VERSION):
        raise ValueError("lifecycle_epoch must be bounded integer")

    encoded = json.dumps(
        ["jobber_provider_suspension_audit", "v1", contractor_id, app_id, account_id, barrier, generation, lifecycle_epoch],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_control_record(data: Any) -> dict[str, Any]:
    """Strict closed hostile validator for jobber_disconnect_control/current."""
    if type(data) is not dict:
        raise JobberDisconnectControlError("Control record is not a dictionary")

    for k in data.keys():
        if type(k) is not str:
            raise JobberDisconnectControlError("Control record contains non-string key")
        if k in FORBIDDEN_PERSISTENCE_KEYS:
            raise JobberDisconnectControlError(f"Control record contains forbidden key: {k}")

    expected_keys = {"schema_version", "expected_app_id", "receipt_counter", "enforcement_active", "updated_at"}
    actual_keys = set(data.keys())
    if actual_keys != expected_keys:
        raise JobberDisconnectControlError(f"Control record keys mismatch: expected {expected_keys}, got {actual_keys}")

    schema_ver = data["schema_version"]
    if type(schema_ver) is not int or type(schema_ver) is bool or schema_ver != SCHEMA_VERSION:
        raise JobberDisconnectControlError(f"Invalid schema_version in control record: {schema_ver}")

    app_id = data["expected_app_id"]
    if not is_valid_opaque_id(app_id):
        raise JobberDisconnectControlError("Invalid expected_app_id in control record")

    counter = data["receipt_counter"]
    if type(counter) is not int or type(counter) is bool or not (0 <= counter <= MAX_RECEIPT_COUNTER):
        raise JobberDisconnectControlError(f"Invalid receipt_counter in control record: {counter}")

    active = data["enforcement_active"]
    if active is not True:
        raise JobberDisconnectControlError("enforcement_active must be exactly true")

    updated_at = data["updated_at"]
    if type(updated_at) not in (int, float) or type(updated_at) is bool or not math.isfinite(updated_at) or updated_at <= 0.0:
        raise JobberDisconnectControlError("updated_at must be a finite positive number")

    return dict(data)


def validate_barrier_record(
    data: Any,
    expected_app_id: str | None = None,
    expected_account_id: str | None = None,
) -> dict[str, Any]:
    """Strict closed hostile validator for jobber_account_barriers/{key}."""
    if type(data) is not dict:
        raise JobberAccountBarrierError("Barrier record is not a dictionary")

    for k in data.keys():
        if type(k) is not str:
            raise JobberAccountBarrierError("Barrier record contains non-string key")
        if k in FORBIDDEN_PERSISTENCE_KEYS:
            raise JobberAccountBarrierError(f"Barrier record contains forbidden key: {k}")

    expected_keys = {"schema_version", "app_id", "account_id", "barrier", "last_receipt_id", "updated_at"}
    actual_keys = set(data.keys())
    if actual_keys != expected_keys:
        raise JobberAccountBarrierError(f"Barrier record keys mismatch: expected {expected_keys}, got {actual_keys}")

    schema_ver = data["schema_version"]
    if type(schema_ver) is not int or type(schema_ver) is bool or schema_ver != SCHEMA_VERSION:
        raise JobberAccountBarrierError(f"Invalid schema_version in barrier record: {schema_ver}")

    app_id = data["app_id"]
    if not is_valid_opaque_id(app_id):
        raise JobberAccountBarrierError("Invalid app_id in barrier record")
    if expected_app_id is not None and app_id != expected_app_id:
        raise JobberAccountBarrierError("app_id mismatch in barrier record")

    account_id = data["account_id"]
    if not is_valid_opaque_id(account_id):
        raise JobberAccountBarrierError("Invalid account_id in barrier record")
    if expected_account_id is not None and account_id != expected_account_id:
        raise JobberAccountBarrierError("account_id mismatch in barrier record")

    barrier = data["barrier"]
    if type(barrier) is not int or type(barrier) is bool or not (0 <= barrier <= MAX_RECEIPT_COUNTER):
        raise JobberAccountBarrierError(f"Invalid barrier in barrier record: {barrier}")

    receipt_id = data["last_receipt_id"]
    if type(receipt_id) is not str or not _ADMISSION_ID_REGEX.fullmatch(receipt_id):
        raise JobberAccountBarrierError("Invalid last_receipt_id in barrier record")

    updated_at = data["updated_at"]
    if type(updated_at) not in (int, float) or type(updated_at) is bool or not math.isfinite(updated_at) or updated_at <= 0.0:
        raise JobberAccountBarrierError("updated_at must be a finite positive number")

    return dict(data)


def validate_receipt_record(
    data: Any,
    expected_admission_id: str | None = None,
) -> dict[str, Any]:
    """Strict closed hostile validator for jobber_disconnect_receipts/{admission_id}."""
    if type(data) is not dict:
        raise JobberDisconnectReceiptError("Receipt record is not a dictionary")

    for k in data.keys():
        if type(k) is not str:
            raise JobberDisconnectReceiptError("Receipt record contains non-string key")
        if k in FORBIDDEN_PERSISTENCE_KEYS:
            raise JobberDisconnectReceiptError(f"Receipt record contains forbidden key: {k}")

    expected_keys = {
        "schema_version",
        "admission_id",
        "delivery_fingerprint",
        "app_id",
        "account_id",
        "occurred_at",
        "received_at",
        "receipt_counter",
    }
    actual_keys = set(data.keys())
    if actual_keys != expected_keys:
        raise JobberDisconnectReceiptError(f"Receipt record keys mismatch: expected {expected_keys}, got {actual_keys}")

    schema_ver = data["schema_version"]
    if type(schema_ver) is not int or type(schema_ver) is bool or schema_ver != SCHEMA_VERSION:
        raise JobberDisconnectReceiptError(f"Invalid schema_version in receipt record: {schema_ver}")

    admission_id = data["admission_id"]
    if type(admission_id) is not str or not _ADMISSION_ID_REGEX.fullmatch(admission_id):
        raise JobberDisconnectReceiptError("Invalid admission_id in receipt record")
    if expected_admission_id is not None and admission_id != expected_admission_id:
        raise JobberDisconnectReceiptError("admission_id mismatch in receipt record")

    fp = data["delivery_fingerprint"]
    if type(fp) is not str or not _HEX_64_REGEX.fullmatch(fp):
        raise JobberDisconnectReceiptError("Invalid delivery_fingerprint in receipt record")

    app_id = data["app_id"]
    if not is_valid_opaque_id(app_id):
        raise JobberDisconnectReceiptError("Invalid app_id in receipt record")

    account_id = data["account_id"]
    if not is_valid_opaque_id(account_id):
        raise JobberDisconnectReceiptError("Invalid account_id in receipt record")

    occurred_at_raw = data["occurred_at"]
    if validate_rfc3339_timestamp(occurred_at_raw) is None:
        raise JobberDisconnectReceiptError("Invalid RFC3339 occurred_at in receipt record")

    received_at = data["received_at"]
    if type(received_at) not in (int, float) or type(received_at) is bool or not math.isfinite(received_at) or received_at <= 0.0:
        raise JobberDisconnectReceiptError("received_at must be a finite positive number")

    counter = data["receipt_counter"]
    if type(counter) is not int or type(counter) is bool or not (1 <= counter <= MAX_RECEIPT_COUNTER):
        raise JobberDisconnectReceiptError(f"Invalid receipt_counter in receipt record: {counter}")

    return dict(data)


def validate_work_record(data: Any) -> dict[str, Any]:
    """Strict closed hostile validator for jobber_disconnect_work/{work_id}."""
    if type(data) is not dict:
        raise JobberDisconnectWorkError("Work record is not a dictionary")

    for k in data.keys():
        if type(k) is not str:
            raise JobberDisconnectWorkError("Work record contains non-string key")
        if k in FORBIDDEN_PERSISTENCE_KEYS:
            raise JobberDisconnectWorkError(f"Work record contains forbidden key: {k}")

    expected_keys = {
        "schema_version",
        "work_id",
        "app_id",
        "account_id",
        "barrier",
        "last_receipt_id",
        "status",
        "cursor",
        "lease_id",
        "lease_expires_at",
        "attempt_count",
        "next_attempt_at",
        "created_at",
        "updated_at",
    }
    actual_keys = set(data.keys())
    if actual_keys != expected_keys:
        raise JobberDisconnectWorkError(f"Work record keys mismatch: expected {expected_keys}, got {actual_keys}")

    schema_ver = data["schema_version"]
    if type(schema_ver) is not int or type(schema_ver) is bool or schema_ver != SCHEMA_VERSION:
        raise JobberDisconnectWorkError(f"Invalid schema_version in work record: {schema_ver}")

    work_id = data["work_id"]
    if type(work_id) is not str or not _HEX_64_REGEX.fullmatch(work_id):
        raise JobberDisconnectWorkError("Invalid work_id in work record")

    app_id = data["app_id"]
    if not is_valid_opaque_id(app_id):
        raise JobberDisconnectWorkError("Invalid app_id in work record")

    account_id = data["account_id"]
    if not is_valid_opaque_id(account_id):
        raise JobberDisconnectWorkError("Invalid account_id in work record")

    barrier = data["barrier"]
    if type(barrier) is not int or type(barrier) is bool or not (1 <= barrier <= MAX_RECEIPT_COUNTER):
        raise JobberDisconnectWorkError(f"Invalid barrier in work record: {barrier}")

    receipt_id = data["last_receipt_id"]
    if type(receipt_id) is not str or not _ADMISSION_ID_REGEX.fullmatch(receipt_id):
        raise JobberDisconnectWorkError("Invalid last_receipt_id in work record")

    status = data["status"]
    if type(status) is not str or status not in VALID_WORK_STATUSES:
        raise JobberDisconnectWorkError(f"Invalid status in work record: {status}")

    cursor = data["cursor"]
    if cursor is not None and not is_safe_document_id(cursor):
        raise JobberDisconnectWorkError("cursor must be None or non-empty string")

    lease_id = data["lease_id"]
    if lease_id is not None and (type(lease_id) is not str or not _ADMISSION_ID_REGEX.fullmatch(lease_id)):
        raise JobberDisconnectWorkError("lease_id must be None or valid string")

    lease_exp = data["lease_expires_at"]
    if lease_exp is not None:
        if type(lease_exp) not in (int, float) or type(lease_exp) is bool or not math.isfinite(lease_exp) or lease_exp <= 0.0:
            raise JobberDisconnectWorkError("lease_expires_at must be None or finite positive number")

    attempts = data["attempt_count"]
    if type(attempts) is not int or type(attempts) is bool or not (0 <= attempts <= 5):
        raise JobberDisconnectWorkError("attempt_count must be bounded integer")

    next_att = data["next_attempt_at"]
    if type(next_att) not in (int, float) or type(next_att) is bool or not math.isfinite(next_att) or next_att <= 0.0:
        raise JobberDisconnectWorkError("next_attempt_at must be finite positive number")

    created_at = data["created_at"]
    if type(created_at) not in (int, float) or type(created_at) is bool or not math.isfinite(created_at) or created_at <= 0.0:
        raise JobberDisconnectWorkError("created_at must be finite positive number")

    updated_at = data["updated_at"]
    if type(updated_at) not in (int, float) or type(updated_at) is bool or not math.isfinite(updated_at) or updated_at <= 0.0:
        raise JobberDisconnectWorkError("updated_at must be finite positive number")

    if work_id != compute_work_doc_id(app_id, account_id):
        raise JobberDisconnectWorkError("Work identity mismatch")
    if status == "processing":
        if lease_id is None or lease_exp is None:
            raise JobberDisconnectWorkError("Processing work requires complete lease")
    elif lease_id is not None or lease_exp is not None:
        raise JobberDisconnectWorkError("Nonprocessing work cannot retain lease")
    if status == "completed" and cursor is not None:
        raise JobberDisconnectWorkError("Completed work cannot retain cursor")
    if status in ("pending", "processing") and attempts >= 5:
        raise JobberDisconnectWorkError("Exhausted work must require review")
    return dict(data)


def validate_scan_cursor_record(data: Any) -> dict[str, Any]:
    """Strict closed hostile validator for jobber_disconnect_scan_cursor/current."""
    if type(data) is not dict:
        raise JobberDisconnectWorkError("Scan cursor record is not a dictionary")

    expected_keys = {"schema_version", "cursor_work_id", "updated_at"}
    actual_keys = set(data.keys())
    if actual_keys != expected_keys:
        raise JobberDisconnectWorkError(f"Scan cursor keys mismatch: expected {expected_keys}, got {actual_keys}")

    schema_ver = data["schema_version"]
    if type(schema_ver) is not int or type(schema_ver) is bool or schema_ver != SCHEMA_VERSION:
        raise JobberDisconnectWorkError("Invalid schema_version in scan cursor record")

    cursor_work_id = data["cursor_work_id"]
    if cursor_work_id is not None and not is_firestore_document_id(cursor_work_id):
        raise JobberDisconnectWorkError("Invalid cursor_work_id in scan cursor record")

    updated_at = data["updated_at"]
    if type(updated_at) not in (int, float) or type(updated_at) is bool or not math.isfinite(updated_at) or updated_at <= 0.0:
        raise JobberDisconnectWorkError("updated_at must be finite positive number in scan cursor")

    return dict(data)


def validate_suspension_audit_record(
    data: Any,
    expected_audit_id: str | None = None,
) -> dict[str, Any]:
    """Strict closed hostile validator for jobber_provider_suspension_audit/{audit_id}."""
    if type(data) is not dict:
        raise JobberSuspensionAuditError("Suspension audit record is not a dictionary")

    for k in data.keys():
        if type(k) is not str:
            raise JobberSuspensionAuditError("Suspension audit record contains non-string key")
        if k in FORBIDDEN_PERSISTENCE_KEYS:
            raise JobberSuspensionAuditError(f"Suspension audit record contains forbidden key: {k}")

    expected_keys = {
        "schema_version",
        "audit_id",
        "contractor_id",
        "app_id",
        "account_id",
        "barrier",
        "generation",
        "lifecycle_epoch",
        "binding_suspended",
        "credentials_retained",
        "capture_disabled",
        "provider_revocation_attempted",
        "created_at",
        "timestamp",
    }
    actual_keys = set(data.keys())
    if actual_keys != expected_keys:
        raise JobberSuspensionAuditError(f"Suspension audit record keys mismatch: expected {expected_keys}, got {actual_keys}")

    schema_ver = data["schema_version"]
    if type(schema_ver) is not int or type(schema_ver) is bool or schema_ver != SCHEMA_VERSION:
        raise JobberSuspensionAuditError("Invalid schema_version in suspension audit record")

    audit_id = data["audit_id"]
    if type(audit_id) is not str or not _HEX_64_REGEX.fullmatch(audit_id):
        raise JobberSuspensionAuditError("Invalid audit_id in suspension audit record")
    if expected_audit_id is not None and audit_id != expected_audit_id:
        raise JobberSuspensionAuditError("audit_id mismatch in suspension audit record")

    cid = data["contractor_id"]
    if not is_valid_opaque_id(cid):
        raise JobberSuspensionAuditError("Invalid contractor_id in suspension audit record")

    app_id = data["app_id"]
    if not is_valid_opaque_id(app_id):
        raise JobberSuspensionAuditError("Invalid app_id in suspension audit record")

    account_id = data["account_id"]
    if not is_valid_opaque_id(account_id):
        raise JobberSuspensionAuditError("Invalid account_id in suspension audit record")

    barrier = data["barrier"]
    if type(barrier) is not int or type(barrier) is bool or not (1 <= barrier <= MAX_RECEIPT_COUNTER):
        raise JobberSuspensionAuditError("Invalid barrier in suspension audit record")

    gen = data["generation"]
    if type(gen) is not int or type(gen) is bool or not (0 <= gen <= MAX_KEY_VERSION):
        raise JobberSuspensionAuditError("Invalid generation in suspension audit record")

    epoch = data["lifecycle_epoch"]
    if type(epoch) is not int or type(epoch) is bool or not (0 <= epoch <= MAX_KEY_VERSION):
        raise JobberSuspensionAuditError("Invalid lifecycle_epoch in suspension audit record")

    if data["binding_suspended"] is not True:
        raise JobberSuspensionAuditError("binding_suspended must be exact boolean True")

    if data["credentials_retained"] is not True:
        raise JobberSuspensionAuditError("credentials_retained must be exact boolean True")

    if data["capture_disabled"] is not True:
        raise JobberSuspensionAuditError("capture_disabled must be exact boolean True")

    if data["provider_revocation_attempted"] is not False:
        raise JobberSuspensionAuditError("provider_revocation_attempted must be exact boolean False")

    created_at = data["created_at"]
    if type(created_at) not in (int, float) or type(created_at) is bool or not math.isfinite(created_at) or created_at <= 0.0:
        raise JobberSuspensionAuditError("created_at must be finite positive float in suspension audit record")

    ts = data["timestamp"]
    if type(ts) not in (int, float) or type(ts) is bool or not math.isfinite(ts) or ts <= 0.0:
        raise JobberSuspensionAuditError("timestamp must be finite positive float in suspension audit record")
    if ts != created_at:
        raise JobberSuspensionAuditError("timestamp must exactly equal created_at in suspension audit record")

    return dict(data)


def read_control_in_txn(transaction: Any, db: Any) -> dict[str, Any] | None:
    """Read and validate jobber_disconnect_control/current in an active transaction."""
    ref = db.collection(CONTROL_COLLECTION).document(CONTROL_DOC_ID)
    snap = read_document(ref, transaction)
    if not getattr(snap, "exists", False):
        return None
    data = snap.to_dict()
    return validate_control_record(data)


def read_barrier_in_txn(
    transaction: Any,
    db: Any,
    app_id: str,
    account_id: str,
) -> dict[str, Any] | None:
    """Read and validate jobber_account_barriers/{key} in an active transaction."""
    key = compute_account_barrier_key(app_id, account_id)
    ref = db.collection(BARRIERS_COLLECTION).document(key)
    snap = read_document(ref, transaction)
    if not getattr(snap, "exists", False):
        return None
    data = snap.to_dict()
    return validate_barrier_record(data, expected_app_id=app_id, expected_account_id=account_id)


def read_receipt_in_txn(
    transaction: Any,
    db: Any,
    admission_id: str,
) -> dict[str, Any] | None:
    """Read and validate jobber_disconnect_receipts/{admission_id} in an active transaction."""
    if type(admission_id) is not str or not _ADMISSION_ID_REGEX.fullmatch(admission_id):
        raise JobberDisconnectReceiptError("admission_id must be valid string")
    ref = db.collection(RECEIPTS_COLLECTION).document(admission_id)
    snap = read_document(ref, transaction)
    if not getattr(snap, "exists", False):
        return None
    data = snap.to_dict()
    return validate_receipt_record(data, expected_admission_id=admission_id)


def read_work_in_txn(
    transaction: Any,
    db: Any,
    app_id: str,
    account_id: str,
) -> dict[str, Any] | None:
    """Read and validate jobber_disconnect_work/{work_id} in an active transaction."""
    work_id = compute_work_doc_id(app_id, account_id)
    ref = db.collection(WORK_COLLECTION).document(work_id)
    snap = read_document(ref, transaction)
    if not getattr(snap, "exists", False):
        return None
    data = snap.to_dict()
    return validate_work_record(data)


def read_scan_cursor_in_txn(transaction: Any, db: Any) -> dict[str, Any] | None:
    """Read and validate jobber_disconnect_scan_cursor/current in an active transaction."""
    ref = db.collection(SCAN_CURSOR_COLLECTION).document(SCAN_CURSOR_DOC_ID)
    snap = read_document(ref, transaction)
    if not getattr(snap, "exists", False):
        return None
    data = snap.to_dict()
    return validate_scan_cursor_record(data)


def read_suspension_audit_in_txn(transaction: Any, db: Any, audit_id: str) -> dict[str, Any] | None:
    """Read and validate jobber_provider_suspension_audit/{audit_id} in an active transaction."""
    if type(audit_id) is not str or not _HEX_64_REGEX.fullmatch(audit_id):
        raise JobberSuspensionAuditError("Invalid audit_id")
    ref = db.collection(SUSPENSION_AUDIT_COLLECTION).document(audit_id)
    snap = read_document(ref, transaction)
    if not getattr(snap, "exists", False):
        return None
    data = snap.to_dict()
    return validate_suspension_audit_record(data, expected_audit_id=audit_id)


def validate_account_records(control: Any, barrier: Any, work: Any, *, app_id: str,
                             account_id: str, allow_missing: bool = False) -> tuple[dict, dict | None, dict | None]:
    """One cross-record contract for receipt admission, readback and recovery."""
    c = validate_control_record(control)
    if c["expected_app_id"] != app_id:
        raise JobberDisconnectControlError("Control app mismatch")
    if barrier is None and work is None and allow_missing:
        return c, None, None
    if barrier is None or work is None:
        raise JobberDisconnectWorkError("Incomplete account records")
    b = validate_barrier_record(barrier, app_id, account_id)
    w = validate_work_record(work)
    if (w["work_id"] != compute_work_doc_id(app_id, account_id)
            or w["app_id"] != app_id or w["account_id"] != account_id
            or w["barrier"] != b["barrier"]
            or w["last_receipt_id"] != b["last_receipt_id"]
            or not 1 <= b["barrier"] <= c["receipt_counter"]):
        raise JobberDisconnectWorkError("Account record identity or counter mismatch")
    return c, b, w


def _read_account_snapshots(txn: Any, db: Any, app_id: str, account_id: str) -> tuple:
    refs = (
        db.collection(CONTROL_COLLECTION).document(CONTROL_DOC_ID),
        db.collection(BARRIERS_COLLECTION).document(compute_account_barrier_key(app_id, account_id)),
        db.collection(WORK_COLLECTION).document(compute_work_doc_id(app_id, account_id)),
    )
    snaps = tuple(read_document(ref, txn) for ref in refs)
    return refs, snaps


def _data(snap: Any) -> Any:
    return snap.to_dict() if snap.exists else None


def _validate_receipt_match(receipt: Any, admission_id: str, app_id: str,
                            account_id: str, delivery_fingerprint: str, occurred_at: str) -> dict:
    r = validate_receipt_record(receipt, admission_id)
    if (r["app_id"] != app_id or r["account_id"] != account_id
            or r["delivery_fingerprint"] != delivery_fingerprint or r["occurred_at"] != occurred_at):
        raise JobberDisconnectReceiptError("Admission immutable input mismatch")
    return r


def readback_disconnect_admission_in_txn(transaction: Any, db: Any, admission_id: str,
                                        app_id: str, account_id: str, delivery_fingerprint: str,
                                        occurred_at: str) -> dict[str, Any]:
    _, snaps = _read_account_snapshots(transaction, db, app_id, account_id)
    rs = read_document(db.collection(RECEIPTS_COLLECTION).document(admission_id), transaction)
    c, b, w = validate_account_records(*(_data(s) for s in snaps), app_id=app_id, account_id=account_id)
    extract_snapshot_time(snaps[0])
    r = _validate_receipt_match(_data(rs), admission_id, app_id, account_id, delivery_fingerprint, occurred_at)
    if not r["receipt_counter"] <= b["barrier"] <= c["receipt_counter"]:
        raise JobberDisconnectReceiptError("Admission fencing is not durable")
    if r["receipt_counter"] == b["barrier"] and b["last_receipt_id"] != admission_id:
        raise JobberDisconnectReceiptError("Admission barrier identity mismatch")
    return r


def record_disconnect_admission_atomic(db: Any, admission_id: str, delivery_fingerprint: str,
                                       app_id: str, account_id: str, occurred_at: str,
                                       timestamp: float | None = None, enforce_control_exists: bool = True,
                                       deadline: Deadline | None = None) -> dict[str, Any]:
    """Commit once per admission ID, then independently prove durable acceptance."""
    if type(admission_id) is not str or not _ADMISSION_ID_REGEX.fullmatch(admission_id):
        raise JobberDisconnectReceiptError("Invalid admission ID")
    if type(delivery_fingerprint) is not str or not _HEX_64_REGEX.fullmatch(delivery_fingerprint):
        raise JobberDisconnectReceiptError("Invalid fingerprint")
    if not is_valid_opaque_id(app_id) or not is_valid_opaque_id(account_id):
        raise JobberDisconnectReceiptError("Invalid identity")
    if validate_rfc3339_timestamp(occurred_at) is None:
        raise JobberDisconnectReceiptError("Invalid occurred_at")
    if timestamp is not None:
        raise JobberDisconnectReceiptError("Caller time is not authoritative")
    deadline = deadline or Deadline()
    receipt_ref = db.collection(RECEIPTS_COLLECTION).document(admission_id)

    def record(txn: Any) -> dict:
        refs, snaps = _read_account_snapshots(txn, db, app_id, account_id)
        receipt_snap = read_document(receipt_ref, txn)
        c, b, w = validate_account_records(*(_data(s) for s in snaps), app_id=app_id,
                                           account_id=account_id, allow_missing=not receipt_snap.exists)
        now = extract_snapshot_time(snaps[0])
        if receipt_snap.exists:
            r = _validate_receipt_match(receipt_snap.to_dict(), admission_id, app_id, account_id,
                                        delivery_fingerprint, occurred_at)
            if b is None or r["receipt_counter"] > b["barrier"]:
                raise JobberDisconnectReceiptError("Existing receipt lacks fencing")
            if r["receipt_counter"] == b["barrier"] and b["last_receipt_id"] != admission_id:
                raise JobberDisconnectReceiptError("Existing receipt identity mismatch")
            return r
        if c["receipt_counter"] >= MAX_RECEIPT_COUNTER:
            raise JobberDisconnectControlError("Counter overflow")
        counter = c["receipt_counter"] + 1
        r = {"schema_version": 1, "admission_id": admission_id,
             "delivery_fingerprint": delivery_fingerprint, "app_id": app_id,
             "account_id": account_id, "occurred_at": occurred_at,
             "received_at": now, "receipt_counter": counter}
        b = {"schema_version": 1, "app_id": app_id, "account_id": account_id,
             "barrier": counter, "last_receipt_id": admission_id, "updated_at": now}
        w = {"schema_version": 1, "work_id": compute_work_doc_id(app_id, account_id),
             "app_id": app_id, "account_id": account_id, "barrier": counter,
             "last_receipt_id": admission_id, "status": "pending", "cursor": None,
             "lease_id": None, "lease_expires_at": None, "attempt_count": 0,
             "next_attempt_at": now, "created_at": w["created_at"] if w else now,
             "updated_at": now}
        validate_receipt_record(r, admission_id)
        validate_barrier_record(b, app_id, account_id)
        validate_work_record(w)
        txn.update(refs[0], {"receipt_counter": counter, "updated_at": now})
        txn.create(receipt_ref, r)
        txn.set(refs[1], b)
        txn.set(refs[2], w)
        return r

    def readback(txn: Any) -> dict:
        return readback_disconnect_admission_in_txn(txn, db, admission_id, app_id, account_id,
                                                    delivery_fingerprint, occurred_at)

    try:
        run_transaction(db, record, deadline=deadline)
    except (JobberDisconnectError, ValueError):
        raise
    except Exception:
        # Same-ID readback can resolve a commit whose response was lost.
        return run_transaction(db, readback, deadline=deadline, read_only=True)
    return run_transaction(db, readback, deadline=deadline, read_only=True)
