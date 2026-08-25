#!/usr/bin/env python3
"""Seed and smoke-test Phase 0 safety behavior against staging.

The script is intentionally staging-only. It creates a disposable contractor
document via create-only semantics, calls safe deployed API surfaces with that
contractor's scoped token, and can run synthetic mutable checks only when the
deployed SHA matches the expected PR SHA.

It must not print generated tokens, phone numbers, customer data, transcripts,
or service environment variables.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import secrets
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

import requests

REQUIRED_STAGING_PROJECT = "kevin-staging-491315"
REQUIRED_STAGING_BASE_URL = "https://kevin-api-staging-l63rergg7a-uc.a.run.app"

# Authoritative staging RTDB URL allowlist is empty until an owner-gated staging
# RTDB binding is verified and committed to repository source.
ALLOWED_STAGING_DATABASE_URLS: frozenset[str] = frozenset()

CODEX_PURPOSE = "phase0_staging_smoke"
CODEX_SCHEMA_VERSION = 1

CONTRACTOR_ID_PATTERN = re.compile(r"^codex_phase0_smoke_[0-9a-f]{32}$")
CALL_SID_PATTERN = re.compile(r"^CA[0-9a-f]{32}$")
NONCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")

EXPECTED_RTDB_KEYS = frozenset({
    "call_sid",
    "caller_phone",
    "state",
    "contractor_id",
    "state_updated_at",
    "transcript_buffer",
    "codex_managed",
    "codex_purpose",
    "codex_schema_version",
    "codex_run_nonce",
})

HTTP_TIMEOUT_SECONDS = 20

ALLOWED_DIAGNOSTIC_CODES: frozenset[str] = frozenset({
    "non_staging_project",
    "invalid_base_url",
    "invalid_database_url",
    "non_staging_database_url",
    "non_canonical_database_url",
    "no_authoritative_staging_rtdb_url",
    "invalid_call_sid",
    "invalid_contractor_id",
    "invalid_run_nonce",
    "contractor_seed_failed",
    "call_seed_failed",
    "contractor_ownership_mismatch",
    "call_ownership_mismatch",
    "firestore_cleanup_failed",
    "health_not_ok",
    "health_not_staging",
    "sha_mismatch",
    "contractor_id_mismatch",
    "profile_leaked_token_hash",
    "active_call_not_inactive",
    "calls_not_list",
    "jobs_not_list",
    "settings_missing_defaults",
    "jobber_not_disconnected",
    "calendar_not_disconnected",
    "estimate_gate_not_denied",
    "rtdb_payload_verification_failed",
    "text_reply_gate_not_denied",
    "rtdb_create_conflict",
    "rtdb_cleanup_failed",
    "firestore_client_construction_failed",
    "http_request_failed",
    "url_parse_error",
})

ROUTE_LABELS: frozenset[str] = frozenset({
    "health",
    "contractor_profile",
    "cross_contractor_profile",
    "active_call",
    "calls",
    "jobs",
    "settings",
    "jobber_status",
    "google_calendar_status",
    "estimate_token_gate",
    "cross_tenant_call_action",
    "text_reply",
})

ALLOWED_HTTP_STATUS_CODES = (0, 200, 400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504)

DERIVED_HTTP_DIAGNOSTIC_CODES = frozenset({
    f"{route}_http_{status}"
    for route in ROUTE_LABELS
    for status in ALLOWED_HTTP_STATUS_CODES
})

AUTHORITATIVE_DIAGNOSTIC_CODES = frozenset(
    ALLOWED_DIAGNOSTIC_CODES
    | DERIVED_HTTP_DIAGNOSTIC_CODES
    | {
        "unknown_error",
        "rtdb_cleanup_absence",
        "rtdb_cleanup_mismatch",
        "rtdb_cleanup_failed",
        "rtdb_create_conflict",
        "delete_app_cleanup_failed",
        "firestore_cleanup_failed",
        "http_request_failed",
    }
)


@dataclass
class Step:
    name: str
    status: str
    detail: str = ""


class SmokeFailure(RuntimeError):
    pass


class RTDBCreateConflictError(Exception):
    """Raised inside RTDB create transaction when target node already exists."""


class RTDBCleanupNoOpError(Exception):
    """Raised inside RTDB cleanup transaction when node is absent."""


class RTDBCleanupAbsenceSentinel(RTDBCleanupNoOpError):
    """Sentinel exception indicating verified absence of RTDB node during cleanup."""


class RTDBCleanupMismatchError(Exception):
    """Raised inside RTDB cleanup transaction when node exists but ownership/schema mismatches."""


def _to_diagnostic_code(exc: Exception) -> str:
    """Convert an exception to a closed allowlisted diagnostic code without exposing raw details."""
    if isinstance(exc, requests.RequestException):
        return "http_request_failed"
    if isinstance(exc, SmokeFailure):
        msg = str(exc)
        if msg in AUTHORITATIVE_DIAGNOSTIC_CODES:
            return msg
        return "unknown_error"
    if isinstance(exc, RTDBCreateConflictError):
        return "rtdb_create_conflict"
    if isinstance(exc, RTDBCleanupAbsenceSentinel):
        return "rtdb_cleanup_absence"
    if isinstance(exc, RTDBCleanupMismatchError):
        return "rtdb_cleanup_mismatch"
    return "unknown_error"


def _is_exact_str_dict(d: Any) -> bool:
    """Verify d is an exact built-in dict and every key is an exact built-in str."""
    if type(d) is not dict:
        return False
    for k in d:
        if type(k) is not str:
            return False
    return True


def generate_run_nonce() -> str:
    return secrets.token_hex(16)


def generate_contractor_id() -> str:
    return f"codex_phase0_smoke_{secrets.token_hex(16)}"


def generate_call_sid() -> str:
    return f"CA{secrets.token_hex(16)}"


def compute_cross_tenant_contractor_id(call_sid: str) -> str:
    """Compute deterministic cross-tenant contractor ID from random call SID."""
    if not CALL_SID_PATTERN.fullmatch(call_sid):
        raise SmokeFailure("invalid_call_sid")
    return f"codex_phase0_other_{call_sid[2:18]}"


def validate_and_canonicalize_base_url(raw_url: Any) -> str:
    """Accept only exact required staging base URL with at most one trailing slash."""
    if type(raw_url) is not str:
        raise SmokeFailure("invalid_base_url")
    if raw_url == REQUIRED_STAGING_BASE_URL:
        return REQUIRED_STAGING_BASE_URL
    if raw_url == f"{REQUIRED_STAGING_BASE_URL}/":
        return REQUIRED_STAGING_BASE_URL
    raise SmokeFailure("invalid_base_url")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=REQUIRED_STAGING_BASE_URL)
    parser.add_argument("--project", default=REQUIRED_STAGING_PROJECT)
    parser.add_argument(
        "--contractor-id",
        default="",
        help="Optional contractor ID. If provided, must match ^codex_phase0_smoke_[0-9a-f]{32}$",
    )
    parser.add_argument("--expected-sha", default="")
    parser.add_argument(
        "--require-expected-sha",
        action="store_true",
        help="fail if /health deploy_sha does not match --expected-sha",
    )
    parser.add_argument(
        "--mutable-checks",
        action="store_true",
        help="run synthetic gate/ownership checks that write staging-only RTDB/Firestore docs",
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="staging Firebase RTDB URL; required for synthetic text-reply gate smoke",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete the seeded contractor and synthetic smoke docs at the end",
    )
    return parser.parse_args(argv)


def fail_if_not_staging(project: str, raw_base_url: str, database_url: str = "") -> str:
    if type(project) is not str or project != REQUIRED_STAGING_PROJECT:
        raise SmokeFailure("non_staging_project")

    canonical_base_url = validate_and_canonicalize_base_url(raw_base_url)

    if database_url:
        if type(database_url) is not str:
            raise SmokeFailure("invalid_database_url")
        if not ALLOWED_STAGING_DATABASE_URLS:
            raise SmokeFailure("no_authoritative_staging_rtdb_url")
        normalized_db = database_url.rstrip("/")
        if normalized_db not in ALLOWED_STAGING_DATABASE_URLS:
            raise SmokeFailure("non_staging_database_url")
        try:
            db_parts = urllib.parse.urlsplit(normalized_db)
        except Exception:
            raise SmokeFailure("url_parse_error") from None
        if (
            db_parts.scheme != "https"
            or db_parts.port is not None
            or db_parts.username is not None
            or db_parts.password is not None
            or db_parts.query != ""
            or db_parts.fragment != ""
            or db_parts.path != ""
        ):
            raise SmokeFailure("non_canonical_database_url")

    return canonical_base_url


ALLOWED_STEP_NAMES = frozenset({
    "staging health",
    "staging contractor seeded",
    "scoped contractor profile",
    "cross-contractor profile denied",
    "active-call empty state",
    "calls work queue",
    "jobs work queue",
    "settings defaults",
    "jobber status disconnected",
    "google calendar status disconnected",
    "estimate token gate disabled",
    "cross-tenant call action denied",
    "mutable safety checks",
    "text reply gate disabled",
    "staging cleanup",
})

ALLOWED_STATUSES = frozenset({"PASS", "SKIP", "FAIL", "CLEANUP_FAIL"})


def print_step(step: Any) -> None:
    if type(step) is not Step:
        print("FAIL: unknown_step")
        return
    name = step.name if (type(step.name) is str and step.name in ALLOWED_STEP_NAMES) else "unknown_step"
    status = step.status if (type(step.status) is str and step.status in ALLOWED_STATUSES) else "FAIL"

    detail = ""
    if type(step.detail) is str and step.detail:
        if name == "staging health" and step.detail in ("sha_match=true", "sha_match=false"):
            detail = step.detail
        elif name in ("calls work queue", "jobs work queue") and step.detail.startswith("count="):
            count_str = step.detail[6:]
            if count_str.isdigit():
                c_val = int(count_str)
                if 0 <= c_val <= 1000000:
                    detail = f"count={c_val}"
        elif name == "mutable safety checks" and step.detail in ("missing_expected_sha", "unintended_revision", "mutable_checks_disabled"):
            detail = step.detail
        elif name == "text reply gate disabled" and step.detail in ("rtdb_unconfigured", "firebase_admin_missing", "rtdb_unavailable"):
            detail = step.detail
        elif name == "staging cleanup" and step.detail == "cleanup_failed":
            detail = step.detail

    suffix = f" - {detail}" if detail else ""
    print(f"{status}: {name}{suffix}")


def request_json(
    method: str,
    base_url: str,
    path: str,
    route_label: str,
    *,
    token: str = "",
    payload: dict[str, Any] | None = None,
    expected_status: int = 200,
) -> tuple[int, dict[str, Any]]:
    if type(route_label) is not str or route_label not in ROUTE_LABELS:
        route_label = "unknown_route"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(
            method,
            f"{base_url.rstrip('/')}{path}",
            headers=headers,
            json=payload,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        raise SmokeFailure(f"{route_label}_http_0") from None
    try:
        body = response.json()
    except ValueError:
        body = {}

    raw_status = getattr(response, "status_code", 0)
    if type(raw_status) is not int or type(raw_status) is bool or raw_status not in ALLOWED_HTTP_STATUS_CODES:
        raise SmokeFailure(f"{route_label}_http_500") from None
    status_val = raw_status

    if status_val != expected_status:
        raise SmokeFailure(f"{route_label}_http_{status_val}")
    return status_val, body


def generate_contractor_token(contractor_id: str) -> tuple[str, str]:
    secret = secrets.token_urlsafe(32)
    raw_token = f"kv_ct_{contractor_id[:8]}_{secret}"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, token_hash


def seed_contractor(
    db: Any,
    contractor_id: str,
    *,
    run_nonce: str,
) -> str:
    if not CONTRACTOR_ID_PATTERN.fullmatch(contractor_id):
        raise SmokeFailure("invalid_contractor_id")
    if not NONCE_PATTERN.fullmatch(run_nonce):
        raise SmokeFailure("invalid_run_nonce")

    raw_token, token_hash = generate_contractor_token(contractor_id)
    now = time.time()
    doc_ref = db.collection("contractors").document(contractor_id)

    data: dict[str, Any] = {
        "active": True,
        "api_token_hash": token_hash,
        "apple_user_id": f"{contractor_id}_apple_user",
        "business_address": "Staging only",
        "business_city": "Staging",
        "business_country_name": "United States",
        "business_hours_end": "18:00",
        "business_hours_start": "07:00",
        "business_name": "Codex Phase 0 Smoke",
        "calendar_type": "none",
        "callback_sla_minutes": 15,
        "codex_managed": True,
        "codex_purpose": CODEX_PURPOSE,
        "codex_schema_version": CODEX_SCHEMA_VERSION,
        "codex_run_nonce": run_nonce,
        "contractor_id": contractor_id,
        "country_code": "US",
        "created_at": now,
        "deleted_app_detected_at": None,
        "dial_in_pin": "000000",
        "home_base_address": "Staging only",
        "mode": "business",
        "owner_name": "Codex Smoke",
        "owner_phone": "+15005550006",
        "service_area_zips": ["94105"],
        "service_fee_cents": 0,
        "service_type": "plumbing",
        "subscription_expires": now + 7 * 86400,
        "subscription_status": "trial",
        "subscription_tier": "business",
        "subscription_uuid": str(uuid.uuid4()),
        "timezone": "America/Los_Angeles",
        "twilio_number": "",
        "updated_at": now,
        "voice_engine": "elevenlabs",
    }

    try:
        doc_ref.create(data)
    except Exception:
        raise SmokeFailure("contractor_seed_failed") from None

    return raw_token


def seed_cross_tenant_call(
    db: Any,
    call_sid: str,
    *,
    smoke_contractor_id: str,
    run_nonce: str,
) -> None:
    if not CALL_SID_PATTERN.fullmatch(call_sid):
        raise SmokeFailure("invalid_call_sid")
    if not CONTRACTOR_ID_PATTERN.fullmatch(smoke_contractor_id):
        raise SmokeFailure("invalid_contractor_id")
    if not NONCE_PATTERN.fullmatch(run_nonce):
        raise SmokeFailure("invalid_run_nonce")

    now = time.time()
    cross_tenant_cid = compute_cross_tenant_contractor_id(call_sid)
    doc_ref = db.collection("calls").document(call_sid)
    data: dict[str, Any] = {
        "call_sid": call_sid,
        "contractor_id": cross_tenant_cid,
        "codex_owner_contractor_id": smoke_contractor_id,
        "timestamp": now,
        "read": False,
        "codex_managed": True,
        "codex_purpose": CODEX_PURPOSE,
        "codex_schema_version": CODEX_SCHEMA_VERSION,
        "codex_run_nonce": run_nonce,
    }
    try:
        doc_ref.create(data)
    except Exception:
        raise SmokeFailure("call_seed_failed") from None


def cleanup_firestore(
    db: Any,
    contractor_id: str,
    *,
    run_nonce: str,
    created_call_sids: list[str] | set[str] | None = None,
) -> None:
    if not CONTRACTOR_ID_PATTERN.fullmatch(contractor_id):
        raise SmokeFailure("invalid_contractor_id")
    if not NONCE_PATTERN.fullmatch(run_nonce):
        raise SmokeFailure("invalid_run_nonce")

    if created_call_sids is None:
        created_call_sids = []

    contractor_ref = db.collection("contractors").document(contractor_id)
    call_refs = [db.collection("calls").document(sid) for sid in created_call_sids]

    def _atomic_cleanup_logic(transaction: Any) -> None:
        c_snap = contractor_ref.get(transaction=transaction)
        c_to_delete = False
        if getattr(c_snap, "exists", False):
            c_data = c_snap.to_dict()
            if not _is_exact_str_dict(c_data):
                raise SmokeFailure("contractor_ownership_mismatch")
            c_cid = c_data.get("contractor_id")
            c_managed = c_data.get("codex_managed")
            c_purpose = c_data.get("codex_purpose")
            c_version = c_data.get("codex_schema_version")
            c_nonce = c_data.get("codex_run_nonce")
            if (
                type(c_cid) is not str
                or c_cid != contractor_id
                or type(c_managed) is not bool
                or c_managed is not True
                or type(c_purpose) is not str
                or c_purpose != CODEX_PURPOSE
                or type(c_version) is not int
                or type(c_version) is bool
                or c_version != CODEX_SCHEMA_VERSION
                or type(c_nonce) is not str
                or c_nonce != run_nonce
            ):
                raise SmokeFailure("contractor_ownership_mismatch")
            c_to_delete = True

        calls_to_delete = []
        for c_ref in call_refs:
            call_snap = c_ref.get(transaction=transaction)
            if getattr(call_snap, "exists", False):
                call_data = call_snap.to_dict()
                if not _is_exact_str_dict(call_data):
                    raise SmokeFailure("call_ownership_mismatch")
                expected_cross_tenant_cid = compute_cross_tenant_contractor_id(c_ref.id)
                call_sid_val = call_data.get("call_sid")
                call_cid = call_data.get("contractor_id")
                call_owner = call_data.get("codex_owner_contractor_id")
                call_managed = call_data.get("codex_managed")
                call_purpose = call_data.get("codex_purpose")
                call_version = call_data.get("codex_schema_version")
                call_nonce = call_data.get("codex_run_nonce")
                if (
                    type(call_sid_val) is not str
                    or call_sid_val != c_ref.id
                    or type(call_cid) is not str
                    or call_cid != expected_cross_tenant_cid
                    or type(call_owner) is not str
                    or call_owner != contractor_id
                    or type(call_managed) is not bool
                    or call_managed is not True
                    or type(call_purpose) is not str
                    or call_purpose != CODEX_PURPOSE
                    or type(call_version) is not int
                    or type(call_version) is bool
                    or call_version != CODEX_SCHEMA_VERSION
                    or type(call_nonce) is not str
                    or call_nonce != run_nonce
                ):
                    raise SmokeFailure("call_ownership_mismatch")
                calls_to_delete.append(c_ref)

        if c_to_delete:
            transaction.delete(contractor_ref)
        for c_ref in calls_to_delete:
            transaction.delete(c_ref)

    try:
        transaction = db.transaction()
        try:
            from google.cloud import firestore
            txn_func = firestore.transactional(_atomic_cleanup_logic)
        except Exception:
            txn_func = _atomic_cleanup_logic
        txn_func(transaction)
    except SmokeFailure:
        raise
    except Exception:
        raise SmokeFailure("firestore_cleanup_failed") from None


def health_check(base_url: str, expected_sha: str, require_expected_sha: bool) -> tuple[dict[str, Any], bool]:
    _, body = request_json("GET", base_url, "/health", "health")
    if body.get("status") != "ok":
        raise SmokeFailure("health_not_ok")
    if body.get("environment") != "staging":
        raise SmokeFailure("health_not_staging")

    deployed_sha = str(body.get("deploy_sha") or "")
    sha_matches = not expected_sha or deployed_sha == expected_sha
    if expected_sha and not sha_matches and require_expected_sha:
        raise SmokeFailure("sha_mismatch")
    return body, sha_matches


def read_only_api_smoke(base_url: str, contractor_id: str, token: str) -> list[Step]:
    steps: list[Step] = []

    _, profile = request_json("GET", base_url, f"/api/contractors/{contractor_id}", "contractor_profile", token=token)
    if profile.get("contractor_id") != contractor_id:
        raise SmokeFailure("contractor_id_mismatch")
    if "api_token_hash" in profile:
        raise SmokeFailure("profile_leaked_token_hash")
    steps.append(Step("scoped contractor profile", "PASS"))

    request_json("GET", base_url, "/api/contractors/not_the_owner", "cross_contractor_profile", token=token, expected_status=403)
    steps.append(Step("cross-contractor profile denied", "PASS"))

    _, active = request_json("GET", base_url, f"/api/active-call?contractor_id={contractor_id}", "active_call", token=token)
    if active.get("active") is not False:
        raise SmokeFailure("active_call_not_inactive")
    steps.append(Step("active-call empty state", "PASS"))

    _, calls = request_json("GET", base_url, f"/api/calls?contractor_id={contractor_id}", "calls", token=token)
    if not isinstance(calls.get("calls"), list):
        raise SmokeFailure("calls_not_list")
    calls_count = calls.get("count", len(calls.get("calls", [])))
    c_val = int(calls_count) if type(calls_count) is int and type(calls_count) is not bool and calls_count >= 0 else 0
    steps.append(Step("calls work queue", "PASS", f"count={c_val}"))

    _, jobs = request_json("GET", base_url, f"/api/jobs?contractor_id={contractor_id}", "jobs", token=token)
    if not isinstance(jobs.get("jobs"), list):
        raise SmokeFailure("jobs_not_list")
    j_val = len(jobs.get("jobs", []))
    steps.append(Step("jobs work queue", "PASS", f"count={j_val}"))

    _, settings = request_json("GET", base_url, f"/api/settings?contractor_id={contractor_id}", "settings", token=token)
    if "text_reply_message" not in settings:
        raise SmokeFailure("settings_missing_defaults")
    steps.append(Step("settings defaults", "PASS"))

    _, jobber = request_json(
        "GET",
        base_url,
        f"/api/integrations/jobber/status?contractor_id={contractor_id}",
        "jobber_status",
        token=token,
    )
    if jobber.get("connected") is not False:
        raise SmokeFailure("jobber_not_disconnected")
    steps.append(Step("jobber status disconnected", "PASS"))

    _, calendar = request_json(
        "GET",
        base_url,
        f"/api/integrations/google-calendar/status?contractor_id={contractor_id}",
        "google_calendar_status",
        token=token,
    )
    if calendar.get("connected") is not False:
        raise SmokeFailure("calendar_not_disconnected")
    steps.append(Step("google calendar status disconnected", "PASS"))

    return steps


def mutable_firestore_gate_smoke(
    db: Any,
    base_url: str,
    contractor_id: str,
    token: str,
    *,
    run_nonce: str,
    call_sid: str,
    other_call_sid: str,
) -> list[Step]:
    steps: list[Step] = []

    _, estimate_denied = request_json(
        "POST",
        base_url,
        "/api/estimates/create-token",
        "estimate_token_gate",
        token=token,
        payload={
            "contractor_id": contractor_id,
            "caller_phone": "+15005550006",
            "call_sid": call_sid,
        },
        expected_status=403,
    )
    detail = estimate_denied.get("detail") or {}
    if detail.get("reason") != "feature_disabled":
        raise SmokeFailure("estimate_gate_not_denied")
    steps.append(Step("estimate token gate disabled", "PASS"))

    seed_cross_tenant_call(db, other_call_sid, smoke_contractor_id=contractor_id, run_nonce=run_nonce)
    request_json(
        "POST",
        base_url,
        f"/api/call-action?contractor_id={contractor_id}",
        "cross_tenant_call_action",
        token=token,
        payload={"call_sid": other_call_sid, "action": "decline"},
        expected_status=403,
    )
    steps.append(Step("cross-tenant call action denied", "PASS"))

    return steps


def build_rtdb_call_payload(
    call_sid: str,
    contractor_id: str,
    *,
    run_nonce: str,
    now: float,
) -> dict[str, Any]:
    return {
        "call_sid": call_sid,
        "caller_phone": "+15005550006",
        "state": "screening",
        "contractor_id": contractor_id,
        "state_updated_at": now,
        "transcript_buffer": "synthetic staging smoke",
        "codex_managed": True,
        "codex_purpose": CODEX_PURPOSE,
        "codex_schema_version": CODEX_SCHEMA_VERSION,
        "codex_run_nonce": run_nonce,
    }


def is_exact_owned_rtdb_payload(
    data: Any,
    *,
    contractor_id: str,
    call_sid: str,
    run_nonce: str,
) -> bool:
    """Validate the complete closed expected RTDB projection and exact scalar types/values."""
    if not _is_exact_str_dict(data):
        return False
    if len(data) != 10:
        return False
    for k in data.keys():
        if k not in EXPECTED_RTDB_KEYS:
            return False

    sid = data.get("call_sid")
    if type(sid) is not str or sid != call_sid:
        return False

    phone = data.get("caller_phone")
    if type(phone) is not str or phone != "+15005550006":
        return False

    state = data.get("state")
    if type(state) is not str or state != "screening":
        return False

    cid = data.get("contractor_id")
    if type(cid) is not str or cid != contractor_id:
        return False

    buf = data.get("transcript_buffer")
    if type(buf) is not str or buf != "synthetic staging smoke":
        return False

    managed = data.get("codex_managed")
    if type(managed) is not bool or managed is not True:
        return False

    purpose = data.get("codex_purpose")
    if type(purpose) is not str or purpose != CODEX_PURPOSE:
        return False

    schema = data.get("codex_schema_version")
    if type(schema) is not int or type(schema) is bool or schema != CODEX_SCHEMA_VERSION:
        return False

    nonce = data.get("codex_run_nonce")
    if type(nonce) is not str or nonce != run_nonce:
        return False

    ts = data.get("state_updated_at")
    if type(ts) is not float or not math.isfinite(ts):
        return False

    return True


def rtdb_create_transaction_update(current: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """RTDB transaction update callback for create-only."""
    if current is not None:
        raise RTDBCreateConflictError("rtdb_create_conflict")
    return payload


def rtdb_cleanup_transaction_update(
    current: Any,
    *,
    contractor_id: str,
    call_sid: str,
    run_nonce: str,
) -> None:
    """RTDB transaction update callback for cleanup."""
    if current is None:
        raise RTDBCleanupAbsenceSentinel("rtdb_cleanup_absence")

    if not _is_exact_str_dict(current):
        raise RTDBCleanupMismatchError("rtdb_cleanup_mismatch")

    managed = current.get("codex_managed")
    purpose = current.get("codex_purpose")
    schema = current.get("codex_schema_version")
    nonce = current.get("codex_run_nonce")
    cid = current.get("contractor_id")
    sid = current.get("call_sid")

    if (
        type(managed) is bool
        and managed is True
        and type(purpose) is str
        and purpose == CODEX_PURPOSE
        and type(schema) is int
        and type(schema) is not bool
        and schema == CODEX_SCHEMA_VERSION
        and type(nonce) is str
        and nonce == run_nonce
        and type(cid) is str
        and cid == contractor_id
        and type(sid) is str
        and sid == call_sid
    ):
        return

    raise RTDBCleanupMismatchError("rtdb_cleanup_mismatch")


def mutable_text_reply_smoke(
    base_url: str,
    database_url: str,
    contractor_id: str,
    token: str,
    *,
    run_nonce: str,
    call_sid: str,
) -> Step:
    if not database_url or not ALLOWED_STAGING_DATABASE_URLS:
        return Step(
            "text reply gate disabled",
            "SKIP",
            "rtdb_unconfigured",
        )

    try:
        import firebase_admin
        from firebase_admin import db as rtdb
    except ImportError:
        return Step("text reply gate disabled", "SKIP", "firebase_admin_missing")

    app_name = f"phase0-smoke-{secrets.token_hex(8)}"
    app = None
    ref = None
    created = False
    primary_failure: Exception | None = None
    cleanup_failure: Exception | None = None

    try:
        try:
            app = firebase_admin.initialize_app(None, {"databaseURL": database_url}, name=app_name)
            ref = rtdb.reference(f"/active_calls/{call_sid}", app=app)
        except Exception:
            return Step("text reply gate disabled", "SKIP", "rtdb_unavailable")

        payload = build_rtdb_call_payload(
            call_sid,
            contractor_id,
            run_nonce=run_nonce,
            now=time.time(),
        )

        def _create_txn(current: Any) -> dict[str, Any]:
            return rtdb_create_transaction_update(current, payload)

        try:
            res = ref.transaction(_create_txn)
        except RTDBCreateConflictError:
            raise SmokeFailure("rtdb_create_conflict") from None
        except Exception:
            return Step("text reply gate disabled", "SKIP", "rtdb_unavailable")

        if not is_exact_owned_rtdb_payload(
            res,
            contractor_id=contractor_id,
            call_sid=call_sid,
            run_nonce=run_nonce,
        ):
            raise SmokeFailure("rtdb_payload_verification_failed")
        created = True

        _, body = request_json(
            "POST",
            base_url,
            f"/api/call-action?contractor_id={contractor_id}",
            "text_reply",
            token=token,
            payload={
                "call_sid": call_sid,
                "action": "text_reply",
                "message": "Synthetic staging smoke.",
            },
        )
        if body.get("status") != "error" or "not enabled" not in str(body.get("message", "")):
            raise SmokeFailure("text_reply_gate_not_denied")
        return Step("text reply gate disabled", "PASS")
    except Exception as exc:
        primary_failure = exc
        raise
    finally:
        if ref is not None and created:
            try:
                def _delete_txn(current: Any) -> None:
                    return rtdb_cleanup_transaction_update(
                        current,
                        contractor_id=contractor_id,
                        call_sid=call_sid,
                        run_nonce=run_nonce,
                    )

                ref.transaction(_delete_txn)
            except RTDBCleanupAbsenceSentinel:
                # Verified node absence swallowed as cleanup no-op
                pass
            except (RTDBCleanupMismatchError, Exception):
                cleanup_failure = SmokeFailure("rtdb_cleanup_failed")

        if app is not None:
            try:
                firebase_admin.delete_app(app)
            except Exception:
                if cleanup_failure is None:
                    cleanup_failure = SmokeFailure("delete_app_cleanup_failed")

        if primary_failure is not None:
            if cleanup_failure is not None:
                setattr(primary_failure, "cleanup_error", cleanup_failure)
            raise primary_failure
        elif cleanup_failure is not None:
            raise cleanup_failure


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    steps: list[Step] = []

    run_nonce = generate_run_nonce()
    contractor_id = args.contractor_id if args.contractor_id else generate_contractor_id()
    call_sid = generate_call_sid()
    other_call_sid = generate_call_sid()
    created_call_sids: list[str] = []
    db = None
    primary_exc: Exception | None = None
    cleanup_exc: Exception | None = None

    try:
        canonical_base_url = fail_if_not_staging(args.project, args.base_url, args.database_url)
        try:
            from google.cloud import firestore
            db = firestore.Client(project=args.project)
        except Exception:
            raise SmokeFailure("firestore_client_construction_failed") from None

        health, sha_matches = health_check(
            canonical_base_url,
            args.expected_sha,
            args.require_expected_sha,
        )
        steps.append(
            Step(
                "staging health",
                "PASS",
                f"sha_match={str(sha_matches).lower()}",
            )
        )

        token = seed_contractor(db, contractor_id, run_nonce=run_nonce)
        steps.append(Step("staging contractor seeded", "PASS"))

        steps.extend(read_only_api_smoke(canonical_base_url, contractor_id, token))

        if args.mutable_checks:
            if not args.expected_sha:
                steps.append(
                    Step(
                        "mutable safety checks",
                        "SKIP",
                        "missing_expected_sha",
                    )
                )
            elif not sha_matches:
                steps.append(
                    Step(
                        "mutable safety checks",
                        "SKIP",
                        "unintended_revision",
                    )
                )
            else:
                created_call_sids.append(other_call_sid)
                steps.extend(
                    mutable_firestore_gate_smoke(
                        db,
                        canonical_base_url,
                        contractor_id,
                        token,
                        run_nonce=run_nonce,
                        call_sid=call_sid,
                        other_call_sid=other_call_sid,
                    )
                )
                steps.append(
                    mutable_text_reply_smoke(
                        canonical_base_url,
                        args.database_url,
                        contractor_id,
                        token,
                        run_nonce=run_nonce,
                        call_sid=call_sid,
                    )
                )
        else:
            steps.append(Step("mutable safety checks", "SKIP", "mutable_checks_disabled"))

        if args.cleanup:
            cleanup_firestore(
                db,
                contractor_id,
                run_nonce=run_nonce,
                created_call_sids=created_call_sids,
            )
            steps.append(Step("staging cleanup", "PASS"))

    except Exception as exc:
        primary_exc = exc
        c_err = getattr(exc, "cleanup_error", None)
        if c_err is not None and cleanup_exc is None:
            cleanup_exc = c_err

    # Cleanup in finally block if requested and db exists
    if args.cleanup and db is not None:
        try:
            if not any(s.name == "staging cleanup" and s.status == "PASS" for s in steps):
                cleanup_firestore(
                    db,
                    contractor_id,
                    run_nonce=run_nonce,
                    created_call_sids=created_call_sids,
                )
                steps.append(Step("staging cleanup", "PASS"))
        except Exception as c_err:
            cleanup_exc = c_err
            if not any(s.name == "staging cleanup" for s in steps):
                steps.append(Step("staging cleanup", "FAIL", "cleanup_failed"))
            else:
                for i, s in enumerate(steps):
                    if s.name == "staging cleanup":
                        steps[i] = Step("staging cleanup", "FAIL", "cleanup_failed")

    # ALWAYS print step output on SUCCESS as well as failure!
    for step in steps:
        print_step(step)

    if primary_exc is not None or cleanup_exc is not None:
        if primary_exc is not None:
            code = _to_diagnostic_code(primary_exc)
            print(f"FAIL: {code}", file=sys.stderr)
        if cleanup_exc is not None:
            c_code = _to_diagnostic_code(cleanup_exc)
            print(f"CLEANUP_FAIL: {c_code}", file=sys.stderr)
        return 1

    failures = [step for step in steps if step.status == "FAIL"]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
