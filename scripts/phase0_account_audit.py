#!/usr/bin/env python3
"""Redacted Phase 0 account audit.

This script reads contractor and estimate documents and prints aggregate counts
only. It must never print customer names, phone numbers, tokens, transcripts,
message bodies, token hashes, or document IDs.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from typing import Any

try:
    from google.api_core.datetime_helpers import (
        DatetimeWithNanoseconds as _GoogleDatetimeWithNanoseconds,
    )
except ImportError:
    _GoogleDatetimeWithNanoseconds = None


from app.services.gated_actions import GATE_POLICIES

# Retired action keys that are no longer supported or registered in check_gated_action.
# Preserved for aggregate audit reporting of legacy persisted document state.
RETIRED_ACTION_KEYS = {
    "jobber_create_job",
    "jobber_create_quote",
}

KNOWN_BILLING_TYPE_ALLOWLIST = frozenset({
    "INITIAL_BUY",
    "RENEWAL",
    "DID_RENEW",
    "DID_FAIL_TO_RENEW",
    "EXPIRED",
    "EXPIRATION",
    "CANCELLATION",
    "SUBSCRIBED",
    "DID_CHANGE_RENEWAL_PREF",
    "DID_CHANGE_RENEWAL_STATUS",
    "OFFER_REDEEMED",
    "REFUND",
    "REFUND_DECLINED",
    "REFUND_REVERSED",
    "REVOCATION",
    "BILLING_RETRY",
    "GRACE_PERIOD",
    "GRACE_PERIOD_EXPIRED",
    "PRICE_INCREASE",
    "subscription_renewal",
    "subscription_cancellation",
    "estimate_fee",
    "overage_charge",
    "call_credit",
})

# Currently active gated action keys derived from the canonical GATE_POLICIES registry.
ACTIVE_ACTION_KEYS = {action.value for action, policy in GATE_POLICIES.items() if policy.requires_flag is True}

KNOWN_ACTION_KEYS = ACTIVE_ACTION_KEYS | RETIRED_ACTION_KEYS

SMS_COMPLIANCE_STATUSES = {"approved", "pending", "rejected", "missing"}
INTEGRATION_WRITE_STATUSES = {"approved", "pending", "rejected", "missing"}
SUBSCRIPTION_STATUSES = {"active", "cancelled", "expired", "trial", "missing", "none"}
SUBSCRIPTION_TIERS = {"business", "businessPro", "missing", "none", "personal"}
ESTIMATE_STATUSES = {"complete", "error", "expired", "failed", "pending", "processing"}
FIRESTORE_STREAM_TIMEOUT_SECONDS = 15

ENVELOPE_KEYS = frozenset({"schema_version", "key_version", "algorithm", "nonce", "ciphertext"})


def _is_exact_str_dict(d: Any) -> bool:
    """Verify d is an exact built-in dict and every key is an exact built-in str."""
    if type(d) is not dict:
        return False
    for k in d:
        if type(k) is not str:
            return False
    return True


def _validate_canonical_base64(
    raw: Any,
    *,
    expected_len: int | None = None,
    min_len: int | None = None,
    max_len: int | None = None,
) -> bool:
    if type(raw) is not str or len(raw) == 0:
        return False
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        return False
    if base64.b64encode(decoded).decode("ascii") != raw:
        return False
    if expected_len is not None and len(decoded) != expected_len:
        return False
    if min_len is not None and max_len is not None and not (min_len <= len(decoded) <= max_len):
        return False
    return True


def _is_valid_envelope_structure(envelope: Any) -> bool:
    """Pure structural validation of an envelope without key loading or decryption."""
    if type(envelope) is not dict:
        return False
    if len(envelope) != 5:
        return False
    for k in envelope:
        if type(k) is not str or k not in ENVELOPE_KEYS:
            return False
    sv = envelope["schema_version"]
    if type(sv) is not int or type(sv) is bool or sv != 1:
        return False
    kv = envelope["key_version"]
    if type(kv) is not int or type(kv) is bool or not (1 <= kv <= 2147483647):
        return False
    alg = envelope["algorithm"]
    if type(alg) is not str or alg != "AES-256-GCM":
        return False
    if not _validate_canonical_base64(envelope["nonce"], expected_len=12):
        return False
    if not _validate_canonical_base64(envelope["ciphertext"], min_len=17, max_len=16400):
        return False
    return True


def _classify_single_token(val: Any) -> str:
    """Classify a single token side as 'plaintext', 'envelope', or 'malformed'."""
    if type(val) is str:
        return "plaintext" if len(val) > 0 else "malformed"
    if type(val) is dict:
        return "envelope" if _is_valid_envelope_structure(val) else "malformed"
    return "malformed"


def _classify_credential_representation(record: dict[str, Any], provider: str) -> str:
    """Classify provider credentials without inspecting secrets or decrypting."""
    if not _is_exact_str_dict(record):
        return "malformed"

    access_key = f"{provider}_access_token"
    refresh_key = f"{provider}_refresh_token"
    has_access = access_key in record
    has_refresh = refresh_key in record

    if not has_access and not has_refresh:
        return "absent"

    if has_access and not has_refresh:
        kind = _classify_single_token(record[access_key])
        return "partial" if kind in ("plaintext", "envelope") else "malformed"

    if not has_access and has_refresh:
        kind = _classify_single_token(record[refresh_key])
        return "partial" if kind in ("plaintext", "envelope") else "malformed"

    access_kind = _classify_single_token(record[access_key])
    refresh_kind = _classify_single_token(record[refresh_key])

    if access_kind == "plaintext" and refresh_kind == "plaintext":
        return "plaintext_pair"
    if access_kind == "envelope" and refresh_kind == "envelope":
        return "envelope_pair"
    if (access_kind == "plaintext" and refresh_kind == "envelope") or (
        access_kind == "envelope" and refresh_kind == "plaintext"
    ):
        return "mixed"
    return "malformed"


def _safe_bucket(value: Any, allowed: set[str]) -> str:
    if value is None:
        return "missing"
    if type(value) is not str:
        return "other"
    if value == "":
        return "missing"
    return value if value in allowed else "other"


def _presence(value: Any) -> str:
    if value is None:
        return "false"
    if type(value) is str:
        return "true" if len(value.strip()) > 0 else "false"
    if type(value) is dict:
        return "true" if len(value) > 0 else "false"
    return "false"


def _bool_bucket(record: dict[str, Any], key: str) -> str:
    if not _is_exact_str_dict(record):
        return "other"
    if key not in record:
        return "missing"
    value = record.get(key)
    if type(value) is bool:
        return "true" if value is True else "false"
    return "other"


def _sorted_counter(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


def _count_mapping_keys(counter: Counter[str], value: Any) -> None:
    if type(value) is not dict:
        return
    for key in value:
        if type(key) is not str:
            counter["other"] += 1
        elif key in KNOWN_ACTION_KEYS:
            counter[key] += 1
        else:
            counter["other"] += 1


def _is_trusted_datetime(val: Any) -> bool:
    """Accept only exact built-in datetime or exact imported Google DatetimeWithNanoseconds class."""
    t = type(val)
    if t is datetime:
        return True
    if _GoogleDatetimeWithNanoseconds is not None and t is _GoogleDatetimeWithNanoseconds:
        return True
    return False


def _timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) in (int, float) and type(value) is not bool:
        try:
            f_val = float(value)
            return f_val if math.isfinite(f_val) else None
        except (OverflowError, ValueError):
            return None
    if _is_trusted_datetime(value):
        try:
            f_val = float(value.timestamp())
            return f_val if math.isfinite(f_val) else None
        except Exception:
            return None
    return None


def summarize_contractors(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    sms_statuses: Counter[str] = Counter()
    integration_statuses: Counter[str] = Counter()
    subscription_statuses: Counter[str] = Counter()
    subscription_tiers: Counter[str] = Counter()
    gated_action_keys: Counter[str] = Counter()
    automation_approval_keys: Counter[str] = Counter()
    auto_reply_sms: Counter[str] = Counter()
    jobber_connected: Counter[str] = Counter()
    google_calendar_connected: Counter[str] = Counter()
    jobber_credentials: Counter[str] = Counter()
    google_calendar_credentials: Counter[str] = Counter()
    twilio_number_assigned: Counter[str] = Counter()
    post_deletion_billing_types: Counter[str] = Counter()
    post_deletion_charged_accounts = 0
    post_deletion_rebound_accounts = 0

    total = 0
    active_or_trial_business_accounts = 0

    for record in records:
        if not _is_exact_str_dict(record):
            continue
        total += 1
        pdb = record.get("post_deletion_billing")
        if _is_exact_str_dict(pdb):
            last_type = pdb.get("last_type")
            if type(last_type) is str and last_type in KNOWN_BILLING_TYPE_ALLOWLIST:
                post_deletion_billing_types[last_type] += 1
            elif type(last_type) is str and len(last_type) > 0:
                post_deletion_billing_types["other"] += 1
            else:
                post_deletion_billing_types["unknown"] += 1

            rebound = pdb.get("rebound_contractor_id")
            if type(rebound) is str and len(rebound) > 0:
                post_deletion_rebound_accounts += 1
            else:
                charges = pdb.get("charges")
                if type(charges) is int and type(charges) is not bool and charges > 0:
                    post_deletion_charged_accounts += 1
        elif pdb is not None:
            post_deletion_billing_types["other"] += 1

        sms_status = _safe_bucket(record.get("sms_compliance_status"), SMS_COMPLIANCE_STATUSES)
        integration_status = _safe_bucket(
            record.get("integration_write_status"),
            INTEGRATION_WRITE_STATUSES,
        )
        subscription_status = _safe_bucket(
            record.get("subscription_status"),
            SUBSCRIPTION_STATUSES,
        )
        subscription_tier = _safe_bucket(record.get("subscription_tier"), SUBSCRIPTION_TIERS)

        sms_statuses[sms_status] += 1
        integration_statuses[integration_status] += 1
        subscription_statuses[subscription_status] += 1
        subscription_tiers[subscription_tier] += 1
        auto_reply_sms[_bool_bucket(record, "auto_reply_sms")] += 1

        jobber_connected[_bool_bucket(record, "jobber_connected")] += 1
        google_calendar_connected[_bool_bucket(record, "google_calendar_connected")] += 1
        jobber_credentials[_classify_credential_representation(record, "jobber")] += 1
        google_calendar_credentials[_classify_credential_representation(record, "google_calendar")] += 1
        twilio_number_assigned[_presence(record.get("twilio_number"))] += 1

        _count_mapping_keys(gated_action_keys, record.get("gated_actions"))
        _count_mapping_keys(automation_approval_keys, record.get("automation_approvals"))

        if subscription_status in {"active", "trial"} and subscription_tier in {
            "business",
            "businessPro",
        }:
            active_or_trial_business_accounts += 1

    return {
        "total_contractors": total,
        "active_or_trial_business_accounts": active_or_trial_business_accounts,
        "gated_action_keys": _sorted_counter(gated_action_keys),
        "sms_compliance_status": _sorted_counter(sms_statuses),
        "integration_write_status": _sorted_counter(integration_statuses),
        "automation_approval_keys": _sorted_counter(automation_approval_keys),
        "auto_reply_sms": _sorted_counter(auto_reply_sms),
        "jobber_connected": _sorted_counter(jobber_connected),
        "google_calendar_connected": _sorted_counter(google_calendar_connected),
        "jobber_credentials": _sorted_counter(jobber_credentials),
        "google_calendar_credentials": _sorted_counter(google_calendar_credentials),
        "twilio_number_assigned": _sorted_counter(twilio_number_assigned),
        "subscription_status": _sorted_counter(subscription_statuses),
        "subscription_tier": _sorted_counter(subscription_tiers),
        "post_deletion_billing_types": _sorted_counter(post_deletion_billing_types),
        "post_deletion_charged_accounts": post_deletion_charged_accounts,
        "post_deletion_rebound_accounts": post_deletion_rebound_accounts,
    }


def _age_bucket(created_at: Any, now: float) -> str:
    created_ts = _timestamp(created_at)
    if created_ts is None:
        return "missing_created_at"
    try:
        age_days = (now - created_ts) / 86400
    except Exception:
        return "missing_created_at"
    if age_days < 0:
        return "future"
    if age_days <= 7:
        return "0_7_days"
    if age_days <= 30:
        return "8_30_days"
    if age_days <= 90:
        return "31_90_days"
    return "over_90_days"


def _resolve_effective_now(now: Any) -> float | None:
    """Resolve supplied now to a finite float or None if malformed/hostile/overflowing."""
    if now is None:
        return time.time()
    if type(now) in (int, float) and type(now) is not bool:
        try:
            f_val = float(now)
            if math.isfinite(f_val):
                return f_val
        except (OverflowError, ValueError):
            return None
    return None


def summarize_estimates(records: Iterable[dict[str, Any]], *, now: float | None = None) -> dict[str, Any]:
    effective_now = _resolve_effective_now(now)

    statuses: Counter[str] = Counter()
    age_buckets: Counter[str] = Counter()
    total = 0

    for record in records:
        if not _is_exact_str_dict(record):
            continue
        total += 1
        status = _safe_bucket(record.get("status"), ESTIMATE_STATUSES)
        statuses[status] += 1
        if effective_now is None:
            age_buckets["missing_created_at"] += 1
        else:
            age_buckets[_age_bucket(record.get("created_at"), effective_now)] += 1

    return {
        "total_estimates": total,
        "status": _sorted_counter(statuses),
        "age_buckets": _sorted_counter(age_buckets),
    }


def _stream_collection(client: Any, collection_name: str) -> Iterable[dict[str, Any]]:
    for doc in client.collection(collection_name).stream(
        retry=None,
        timeout=FIRESTORE_STREAM_TIMEOUT_SECONDS,
    ):
        try:
            data = doc.to_dict()
        except Exception:
            continue
        if type(data) is dict:
            yield data


def build_report(client: Any, *, project: str, environment: str) -> dict[str, Any]:
    return {
        "contractors": summarize_contractors(_stream_collection(client, "contractors")),
        "estimates": summarize_estimates(_stream_collection(client, "estimates")),
    }


def main(argv: list[str] | None = None, client_factory: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a redacted Phase 0 account audit.")
    parser.add_argument("--project", required=True, help="Firestore project ID to audit.")
    parser.add_argument(
        "--environment",
        required=True,
        choices=("production", "staging"),
        help="Environment label for the report.",
    )
    parser.add_argument(
        "--database",
        default="(default)",
        help="Firestore database ID. Defaults to '(default)'.",
    )
    args = parser.parse_args(argv)

    if type(args.project) is not str or not args.project:
        print("Audit failed while reading Firestore.", file=sys.stderr)
        return 1

    try:
        if client_factory is None:
            from google.cloud import firestore

            client_factory = firestore.Client

        client = client_factory(project=args.project, database=args.database)
        report = build_report(client, project=args.project, environment=args.environment)
    except Exception:
        print("Audit failed while reading Firestore.", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
