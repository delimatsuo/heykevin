#!/usr/bin/env python3
"""Redacted Phase 0 account audit.

This script reads contractor and estimate documents and prints aggregate counts
only. It must never print customer names, phone numbers, tokens, transcripts,
message bodies, token hashes, or document IDs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable


KNOWN_ACTION_KEYS = {
    "caller_text_reply",
    "caller_auto_reply",
    "caller_confirmation_sms",
    "caller_confirmation_mms",
    "caller_vcard_mms",
    "estimate_token_create",
    "estimate_result_sms",
    "jobber_create_job",
    "jobber_create_quote",
    "google_create_event",
    "twilio_call_redirect",
    "twilio_conference_mutation",
    "twilio_number_provision",
    "twilio_number_release",
    "account_delete",
    "push_lock_screen_context",
}

SMS_COMPLIANCE_STATUSES = {"approved", "pending", "rejected", "missing"}
INTEGRATION_WRITE_STATUSES = {"approved", "pending", "rejected", "missing"}
SUBSCRIPTION_STATUSES = {"active", "cancelled", "expired", "trial", "missing", "none"}
SUBSCRIPTION_TIERS = {"business", "businessPro", "missing", "none", "personal"}
ESTIMATE_STATUSES = {"complete", "error", "expired", "failed", "pending", "processing"}
FIRESTORE_STREAM_TIMEOUT_SECONDS = 15


def _safe_bucket(value: Any, allowed: set[str]) -> str:
    if value is None or value == "":
        return "missing"
    normalized = str(value).strip()
    return normalized if normalized in allowed else "other"


def _presence(value: Any) -> str:
    return "true" if bool(str(value or "").strip()) else "false"


def _bool_bucket(record: dict[str, Any], key: str) -> str:
    if key not in record:
        return "missing"
    value = record.get(key)
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "other"


def _sorted_counter(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


def _count_mapping_keys(counter: Counter[str], value: Any) -> None:
    if not isinstance(value, dict):
        return
    for key in value:
        bucket = key if key in KNOWN_ACTION_KEYS else "other"
        counter[bucket] += 1


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
    twilio_number_assigned: Counter[str] = Counter()

    total = 0
    active_or_trial_business_accounts = 0

    for record in records:
        total += 1
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
        jobber_connected[_presence(record.get("jobber_access_token"))] += 1
        google_calendar_connected[_presence(record.get("google_calendar_access_token"))] += 1
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
        "twilio_number_assigned": _sorted_counter(twilio_number_assigned),
        "subscription_status": _sorted_counter(subscription_statuses),
        "subscription_tier": _sorted_counter(subscription_tiers),
    }


def _timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _age_bucket(created_at: Any, now: float) -> str:
    created_ts = _timestamp(created_at)
    if created_ts is None:
        return "missing_created_at"
    age_days = (now - created_ts) / 86400
    if age_days < 0:
        return "future"
    if age_days <= 7:
        return "0_7_days"
    if age_days <= 30:
        return "8_30_days"
    if age_days <= 90:
        return "31_90_days"
    return "over_90_days"


def summarize_estimates(records: Iterable[dict[str, Any]], *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    statuses: Counter[str] = Counter()
    age_buckets: Counter[str] = Counter()
    total = 0

    for record in records:
        total += 1
        status = _safe_bucket(record.get("status"), ESTIMATE_STATUSES)
        statuses[status] += 1
        age_buckets[_age_bucket(record.get("created_at"), now)] += 1

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
        yield doc.to_dict() or {}


def build_report(client: Any, *, project: str, environment: str) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": environment,
        "project": project,
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

    if client_factory is None:
        from google.cloud import firestore

        client_factory = firestore.Client

    client = client_factory(project=args.project, database=args.database)
    try:
        report = build_report(client, project=args.project, environment=args.environment)
    except Exception:
        print(
            "Audit failed while reading Firestore. Reauthenticate ADC and verify read access "
            "to the requested project.",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
