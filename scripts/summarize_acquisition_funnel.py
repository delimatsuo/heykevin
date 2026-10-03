#!/usr/bin/env python3
"""Offline Aggregate Reducer for Acquisition and Activation Measurement.

Accepts a JSON list of allowlisted measurement maps (schema_version=1).
Bounds input to 5,000 records and a maximum 90-day UTC cohort window [start, end).
Rejects unexpected root fields and invalid schema values without echoing input values.
No network, cloud, provider, or database calls.
"""

import argparse
import datetime
import json
import math
import sys
from typing import Any, Optional

MAX_RECORDS = 5000
MAX_COHORT_DAYS = 90
MAX_SIGNED_64_BIT_INT = (1 << 63) - 1

MIN_TIMESTAMP_S = 978307200.0   # 2001-01-01T00:00:00Z
MAX_TIMESTAMP_S = 4102444800.0  # 2100-01-01T00:00:00Z

KNOWN_PLACEHOLDER_IDS = frozenset({1234567890})
VALID_INTENTS = frozenset({"personal", "business", "unknown"})
VALID_TIERS = frozenset({"personal", "business", "businessPro"})
VALID_ATTRIBUTION_STATUSES = frozenset({"recorded", "unattributed", "exhausted", "ineligible", "disabled", "retryable"})
VALID_SOURCES = frozenset({"apple_ads", "unattributed"})
VALID_SIGNUP_COUNTRIES = frozenset({"US", "CA", "BR", "GB", "DE", "FR", "IT", "ES", "PT"})
VALID_CONVERSION_TYPES = frozenset({"Download", "Redownload", "PreOrder"})
VALID_CLAIM_TYPES = frozenset({"Click", "Impression"})

ALLOWLISTED_KEYS = frozenset({
    "schema_version",
    "cohort",
    "created_at",
    "account_country_at_signup",
    "declared_onboarding_intent",
    "attempts",
    "last_attempt_at",
    "attribution_status",
    "attribution",
    "org_id",
    "campaign_id",
    "ad_group_id",
    "keyword_id",
    "conversion_type",
    "claim_type",
    "source",
    "attribution_recorded_at",
    "lease_attempt",
    "lease_expires_at",
    "first_inbound_observed_at",
    "first_forwarded_observed_at",
    "first_screening_conversation_observed_at",
    "first_verified_entitlement_observed_at",
    "first_verified_entitlement_tier",
    "first_positive_price_purchase_observed_at",
    "first_positive_price_purchase_signed_at",
    "first_positive_price_purchase_tier",
    "first_storekit_trial_observed_at",
    "first_storekit_trial_signed_at",
})


def _is_valid_timestamp(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return math.isfinite(val) and MIN_TIMESTAMP_S <= val <= MAX_TIMESTAMP_S
    return False


def _is_valid_id(val: Any) -> bool:
    if type(val) is not int or isinstance(val, bool):
        return False
    return 0 < val <= MAX_SIGNED_64_BIT_INT and val not in KNOWN_PLACEHOLDER_IDS


def parse_cli_datetime(val: str) -> Optional[datetime.datetime]:
    """Parse ISO8601 UTC datetime string from CLI argument."""
    if not isinstance(val, str) or not val.strip():
        return None
    val = val.strip()
    try:
        if val.endswith("Z"):
            val = val[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


def validate_measurement_record(record: Any) -> dict:
    """Validate all fields and value constraints of an acquisition measurement record.

    Raises ValueError with a generic message without echoing unexpected fields or values.
    """
    if not isinstance(record, dict):
        raise ValueError("Invalid record: not a JSON object")

    for key in record.keys():
        if key not in ALLOWLISTED_KEYS:
            raise ValueError("Invalid record: contains unexpected field")

    # 1. Schema version
    schema_version = record.get("schema_version")
    if type(schema_version) is not int or isinstance(schema_version, bool) or schema_version != 1:
        raise ValueError("Invalid record: schema_version must be integer 1")

    # 2. created_at & cohort
    created_at = record.get("created_at")
    if not _is_valid_timestamp(created_at):
        raise ValueError("Invalid record: created_at must be a valid timestamp")

    cohort = record.get("cohort")
    if not _is_valid_timestamp(cohort) or abs(float(cohort) - float(created_at)) > 0.001:
        raise ValueError("Invalid record: cohort must equal created_at")

    created_ts = float(created_at)

    # 2b. account_country_at_signup (OPTIONAL, exact enum)
    if "account_country_at_signup" in record:
        country_val = record.get("account_country_at_signup")
        if country_val is not None:
            if not isinstance(country_val, str) or isinstance(country_val, bool) or country_val not in VALID_SIGNUP_COUNTRIES:
                raise ValueError("Invalid record: account_country_at_signup must be valid enum")

    # 3. declared_onboarding_intent (REQUIRED, exact enum)
    if "declared_onboarding_intent" not in record:
        raise ValueError("Invalid record: declared_onboarding_intent is required")
    intent = record.get("declared_onboarding_intent")
    if not isinstance(intent, str) or isinstance(intent, bool) or intent not in VALID_INTENTS:
        raise ValueError("Invalid record: declared_onboarding_intent must be valid enum")

    # 4. attempts & last_attempt_at
    if "attempts" not in record:
        raise ValueError("Invalid record: attempts is required")
    attempts = record.get("attempts")
    if type(attempts) is not int or isinstance(attempts, bool) or not (0 <= attempts <= 3):
        raise ValueError("Invalid record: attempts must be integer 0..3")

    last_attempt = record.get("last_attempt_at")
    if attempts == 0:
        if last_attempt is not None and not (_is_valid_timestamp(last_attempt) and float(last_attempt) >= created_ts):
            raise ValueError("Invalid record: last_attempt_at must be valid timestamp")
    else:
        if not _is_valid_timestamp(last_attempt) or float(last_attempt) < created_ts:
            raise ValueError("Invalid record: last_attempt_at required and must be >= created_at")

    # 5. Lease fields
    lease_attempt = record.get("lease_attempt")
    lease_expires = record.get("lease_expires_at")
    if (lease_attempt is None) != (lease_expires is None):
        raise ValueError("Invalid record: lease fields must be paired")
    if lease_attempt is not None:
        if type(lease_attempt) is not int or isinstance(lease_attempt, bool) or not (1 <= lease_attempt <= 3) or lease_attempt != attempts:
            raise ValueError("Invalid record: lease_attempt must match current attempts (1..3)")
        if not _is_valid_timestamp(lease_expires):
            raise ValueError("Invalid record: lease_expires_at must be a valid timestamp")
        if last_attempt is None or not (float(last_attempt) <= float(lease_expires) <= float(last_attempt) + 15.0):
            raise ValueError("Invalid record: lease_expires_at must be between last_attempt_at and last_attempt_at + 15")

    # 6. Attribution status and fields (REQUIRED key, null allowed)
    if "attribution_status" not in record:
        raise ValueError("Invalid record: attribution_status key is required")
    status = record.get("attribution_status")
    if status is not None:
        if not isinstance(status, str) or isinstance(status, bool) or status not in VALID_ATTRIBUTION_STATUSES:
            raise ValueError("Invalid record: attribution_status must be valid enum")

    attribution = record.get("attribution")
    source = record.get("source")
    attr_rec_at = record.get("attribution_recorded_at")

    if status == "recorded":
        if attribution is not True or not isinstance(source, str) or source != "apple_ads":
            raise ValueError("Invalid record: recorded status requires attribution=True and source=apple_ads")
        if not _is_valid_timestamp(attr_rec_at) or float(attr_rec_at) < created_ts:
            raise ValueError("Invalid record: attribution_recorded_at must be >= created_at")
        if not (_is_valid_id(record.get("org_id")) and _is_valid_id(record.get("campaign_id")) and _is_valid_id(record.get("ad_group_id"))):
            raise ValueError("Invalid record: recorded status requires valid orgId, campaignId, and adGroupId")
        if "keyword_id" in record and record["keyword_id"] is not None and not _is_valid_id(record["keyword_id"]):
            raise ValueError("Invalid record: keyword_id must be valid positive int64")
        if "conversion_type" in record and record["conversion_type"] is not None:
            conv = record["conversion_type"]
            if not isinstance(conv, str) or isinstance(conv, bool) or conv not in VALID_CONVERSION_TYPES:
                raise ValueError("Invalid record: conversion_type must be valid enum")
        if "claim_type" in record and record["claim_type"] is not None:
            claim = record["claim_type"]
            if not isinstance(claim, str) or isinstance(claim, bool) or claim not in VALID_CLAIM_TYPES:
                raise ValueError("Invalid record: claim_type must be valid enum")

    elif status == "unattributed":
        if attribution is not False or not isinstance(source, str) or source != "unattributed":
            raise ValueError("Invalid record: unattributed status requires attribution=False and source=unattributed")
        if not _is_valid_timestamp(attr_rec_at) or float(attr_rec_at) < created_ts:
            raise ValueError("Invalid record: attribution_recorded_at must be >= created_at")
        # Ensure no ad IDs or enums are present
        for forbidden_key in ("org_id", "campaign_id", "ad_group_id", "keyword_id", "conversion_type", "claim_type"):
            if record.get(forbidden_key) is not None:
                raise ValueError("Invalid record: unattributed status cannot contain ad metadata")

    else:
        # Pending / retryable / exhausted / disabled / ineligible / None
        if attribution is not None or source is not None or attr_rec_at is not None:
            raise ValueError("Invalid record: non-terminal status cannot contain attribution facts")
        for forbidden_key in ("org_id", "campaign_id", "ad_group_id", "keyword_id", "conversion_type", "claim_type"):
            if record.get(forbidden_key) is not None:
                raise ValueError("Invalid record: non-terminal status cannot contain ad metadata")

    # 7. Inbound & Forwarded call observations
    inbound_at = record.get("first_inbound_observed_at")
    if inbound_at is not None:
        if not _is_valid_timestamp(inbound_at) or float(inbound_at) < created_ts:
            raise ValueError("Invalid record: first_inbound_observed_at must be >= created_at")

    fwd_at = record.get("first_forwarded_observed_at")
    if fwd_at is not None:
        if not _is_valid_timestamp(fwd_at) or float(fwd_at) < created_ts:
            raise ValueError("Invalid record: first_forwarded_observed_at must be >= created_at")

    # 7b. Screening conversation observation
    conv_at = record.get("first_screening_conversation_observed_at")
    if conv_at is not None:
        if not _is_valid_timestamp(conv_at) or float(conv_at) < created_ts:
            raise ValueError("Invalid record: first_screening_conversation_observed_at must be >= created_at")

    # 8. Verified entitlement pairing
    ent_at = record.get("first_verified_entitlement_observed_at")
    ent_tier = record.get("first_verified_entitlement_tier")
    if (ent_at is None) != (ent_tier is None):
        raise ValueError("Invalid record: entitlement observation timestamp and tier must be paired")
    if ent_at is not None:
        if not _is_valid_timestamp(ent_at) or float(ent_at) < created_ts:
            raise ValueError("Invalid record: first_verified_entitlement_observed_at must be >= created_at")
        if not isinstance(ent_tier, str) or isinstance(ent_tier, bool) or ent_tier not in VALID_TIERS:
            raise ValueError("Invalid record: first_verified_entitlement_tier must be valid enum")

    # 9. Positive-price purchase pairing
    paid_at = record.get("first_positive_price_purchase_observed_at")
    paid_signed = record.get("first_positive_price_purchase_signed_at")
    paid_tier = record.get("first_positive_price_purchase_tier")
    paid_fields = [paid_at is not None, paid_signed is not None, paid_tier is not None]
    if any(paid_fields) and not all(paid_fields):
        raise ValueError("Invalid record: positive price purchase fields must be paired")
    if paid_at is not None:
        if not _is_valid_timestamp(paid_at) or float(paid_at) < created_ts:
            raise ValueError("Invalid record: first_positive_price_purchase_observed_at must be >= created_at")
        if not _is_valid_timestamp(paid_signed) or float(paid_signed) > (float(paid_at) + 300.0):
            raise ValueError("Invalid record: purchase signed time cannot exceed observation time + 300s")
        if not isinstance(paid_tier, str) or isinstance(paid_tier, bool) or paid_tier not in VALID_TIERS:
            raise ValueError("Invalid record: first_positive_price_purchase_tier must be valid enum")

    # 10. StoreKit free trial pairing
    trial_at = record.get("first_storekit_trial_observed_at")
    trial_signed = record.get("first_storekit_trial_signed_at")
    if (trial_at is None) != (trial_signed is None):
        raise ValueError("Invalid record: StoreKit trial observation and signed time must be paired")
    if trial_at is not None:
        if not _is_valid_timestamp(trial_at) or float(trial_at) < created_ts:
            raise ValueError("Invalid record: first_storekit_trial_observed_at must be >= created_at")
        if not _is_valid_timestamp(trial_signed) or float(trial_signed) > (float(trial_at) + 300.0):
            raise ValueError("Invalid record: trial signed time cannot exceed observation time + 300s")

    return record


def summarize_acquisition_funnel(
    records: list[Any],
    cohort_start: datetime.datetime,
    cohort_end: datetime.datetime,
    as_of: datetime.datetime,
    by_keyword: bool = False,
) -> dict:
    """Aggregate a list of measurement records into cohort funnel metrics."""
    if not isinstance(records, list):
        raise ValueError("Input data must be a JSON array of measurement records")

    if len(records) > MAX_RECORDS:
        raise ValueError(f"Input exceeds maximum allowed record count of {MAX_RECORDS}")

    if cohort_start.tzinfo is None or cohort_end.tzinfo is None or as_of.tzinfo is None:
        raise ValueError("cohort_start, cohort_end, and as_of must be timezone-aware UTC datetimes")

    cohort_start_utc = cohort_start.astimezone(datetime.timezone.utc)
    cohort_end_utc = cohort_end.astimezone(datetime.timezone.utc)
    as_of_utc = as_of.astimezone(datetime.timezone.utc)

    if cohort_start_utc >= cohort_end_utc:
        raise ValueError("cohort_start must be strictly before cohort_end")

    if (cohort_end_utc - cohort_start_utc).total_seconds() > MAX_COHORT_DAYS * 86400:
        raise ValueError(f"Cohort duration exceeds maximum allowed window of {MAX_COHORT_DAYS} days")

    if cohort_end_utc > as_of_utc:
        raise ValueError("cohort_end cannot be in the future relative to as_of")

    start_ts = cohort_start_utc.timestamp()
    end_ts = cohort_end_utc.timestamp()
    as_of_ts = as_of_utc.timestamp()

    # Pre-validate ALL records first (fails closed if any record is malformed)
    validated_records = [validate_measurement_record(r) for r in records]

    groups: dict[tuple, dict[str, Any]] = {}

    def get_group(group_key: tuple) -> dict[str, Any]:
        if group_key not in groups:
            country, intent, source, camp_id, adg_id, conv_type = group_key[:6]
            g: dict[str, Any] = {
                "account_country_at_signup": country,
                "declared_onboarding_intent": intent,
                "source": source,
                "campaign_id": camp_id,
                "ad_group_id": adg_id,
                "conversion_type": conv_type,
            }
            if by_keyword:
                g["keyword_id"] = group_key[6]
            g.update({
                "accounts_created": 0,
                "attribution_recorded": 0,
                "inbound_observed": 0,
                "forwarding_confirmed": 0,
                "screening_conversation_observed": 0,
                "verified_entitlement_observed": 0,
                "storekit_trial_observed": 0,
                "positive_price_purchase_observed": 0,
                "paid_tiers": {
                    "personal": 0,
                    "business": 0,
                    "business_pro": 0,
                },
            })
            groups[group_key] = g
        return groups[group_key]

    total_accounts_processed = 0

    for rec in validated_records:
        created_ts = float(rec["created_at"])
        if not (start_ts <= created_ts < end_ts):
            continue

        total_accounts_processed += 1

        country = rec.get("account_country_at_signup")
        if not isinstance(country, str) or country not in VALID_SIGNUP_COUNTRIES:
            country = "unknown"

        intent = rec.get("declared_onboarding_intent")
        if intent not in ("personal", "business"):
            intent = "unknown"

        # Check attribution observation time vs as_of
        attr_rec_at = rec.get("attribution_recorded_at")
        status = rec.get("attribution_status")

        if status == "recorded" and attr_rec_at is not None and float(attr_rec_at) <= as_of_ts:
            source = "apple_ads"
            camp_id = rec.get("campaign_id")
            adg_id = rec.get("ad_group_id")
            raw_conv = rec.get("conversion_type")
            conv_type = raw_conv if (raw_conv in VALID_CONVERSION_TYPES) else "unknown"
            kw_id = rec.get("keyword_id") if by_keyword else None
            is_attr_recorded = True
        elif status == "unattributed" and attr_rec_at is not None and float(attr_rec_at) <= as_of_ts:
            source = "unattributed"
            camp_id = None
            adg_id = None
            conv_type = "unknown"
            kw_id = None
            is_attr_recorded = False
        else:
            source = "unknown"
            camp_id = None
            adg_id = None
            conv_type = "unknown"
            kw_id = None
            is_attr_recorded = False

        if by_keyword:
            group_key = (country, intent, source, camp_id, adg_id, conv_type, kw_id)
        else:
            group_key = (country, intent, source, camp_id, adg_id, conv_type)

        g = get_group(group_key)
        g["accounts_created"] += 1

        if is_attr_recorded:
            g["attribution_recorded"] += 1

        # Inbound call observed (<= as_of)
        inbound_ts = rec.get("first_inbound_observed_at")
        if inbound_ts is not None and float(inbound_ts) <= as_of_ts:
            g["inbound_observed"] += 1

        # Forwarded call confirmed (<= as_of)
        fwd_ts = rec.get("first_forwarded_observed_at")
        if fwd_ts is not None and float(fwd_ts) <= as_of_ts:
            g["forwarding_confirmed"] += 1

        # Screening conversation observed (<= as_of)
        conv_ts = rec.get("first_screening_conversation_observed_at")
        if conv_ts is not None and float(conv_ts) <= as_of_ts:
            g["screening_conversation_observed"] += 1

        # Verified entitlement observed (<= as_of)
        ent_ts = rec.get("first_verified_entitlement_observed_at")
        if ent_ts is not None and float(ent_ts) <= as_of_ts:
            g["verified_entitlement_observed"] += 1

        # StoreKit free trial observed (<= as_of)
        trial_ts = rec.get("first_storekit_trial_observed_at")
        if trial_ts is not None and float(trial_ts) <= as_of_ts:
            g["storekit_trial_observed"] += 1

        # Positive-price purchase observed (<= as_of)
        paid_ts = rec.get("first_positive_price_purchase_observed_at")
        if paid_ts is not None and float(paid_ts) <= as_of_ts:
            g["positive_price_purchase_observed"] += 1
            tier = rec.get("first_positive_price_purchase_tier")
            if tier == "personal":
                g["paid_tiers"]["personal"] += 1
            elif tier == "business":
                g["paid_tiers"]["business"] += 1
            elif tier == "businessPro":
                g["paid_tiers"]["business_pro"] += 1

    group_list = sorted(
        groups.values(),
        key=lambda item: (
            item["account_country_at_signup"],
            item["declared_onboarding_intent"],
            item["source"],
            str(item.get("campaign_id") or ""),
            str(item.get("ad_group_id") or ""),
            item["conversion_type"],
            str(item.get("keyword_id") or ""),
        )
    )

    totals = {
        "accounts_created": total_accounts_processed,
        "attribution_recorded": sum(g["attribution_recorded"] for g in group_list),
        "inbound_observed": sum(g["inbound_observed"] for g in group_list),
        "forwarding_confirmed": sum(g["forwarding_confirmed"] for g in group_list),
        "screening_conversation_observed": sum(g["screening_conversation_observed"] for g in group_list),
        "verified_entitlement_observed": sum(g["verified_entitlement_observed"] for g in group_list),
        "storekit_trial_observed": sum(g["storekit_trial_observed"] for g in group_list),
        "positive_price_purchase_observed": sum(g["positive_price_purchase_observed"] for g in group_list),
        "paid_tiers": {
            "personal": sum(g["paid_tiers"]["personal"] for g in group_list),
            "business": sum(g["paid_tiers"]["business"] for g in group_list),
            "business_pro": sum(g["paid_tiers"]["business_pro"] for g in group_list),
        },
    }

    return {
        "schema_version": 1,
        "cohort_start": cohort_start_utc.isoformat(),
        "cohort_end": cohort_end_utc.isoformat(),
        "as_of": as_of_utc.isoformat(),
        "complete": True,
        "input_validation_complete": True,
        "source_population_complete": None,
        "input_uniqueness_verified": False,
        "input_record_count": len(records),
        "totals": totals,
        "groups": group_list,
        "deletion_notice": "Account deletion erases the measurement map and can reduce historical cohort counts.",
        "limitations": "This report contains independent milestone record counts of supplied records only, not an ordered funnel, conversion rates, or sequential pipeline. complete means all supplied records were validated and processed; the offline reducer cannot certify export completeness or unique accounts. Deletions, missing records, or unmigrated builds affect counts. Observed positive-price purchase is not current paid subscribers, renewals, net revenue, or profitability, and positive-price observations may reflect historical restored purchases. Account country snapshot is account setup country, not residence, number location, storefront, or ad targeting geography. Milestone timestamps represent observation/processing time. No ROAS or causal attribution claims are made.",
    }


def main():
    parser = argparse.ArgumentParser(description="Summarize acquisition funnel from measurement JSON dump.")
    parser.add_argument("--input", required=True, help="Path to JSON file containing list of measurement records, or '-' for stdin")
    parser.add_argument("--cohort-start", required=True, help="Cohort start date/time (ISO 8601 UTC, inclusive)")
    parser.add_argument("--cohort-end", required=True, help="Cohort end date/time (ISO 8601 UTC, exclusive)")
    parser.add_argument("--as-of", required=True, help="As-of evaluation timestamp (ISO 8601 UTC)")
    parser.add_argument("--by-keyword", action="store_true", help="Group by keywordId in addition to campaign and ad group")
    parser.add_argument("--output", default=None, help="Output JSON file path (defaults to stdout)")

    args = parser.parse_args()

    try:
        if args.input == "-":
            raw_data = json.load(sys.stdin)
        else:
            with open(args.input, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
    except Exception as e:
        sys.stderr.write(f"Error reading input: {type(e).__name__}\n")
        sys.exit(1)

    cohort_start = parse_cli_datetime(args.cohort_start)
    cohort_end = parse_cli_datetime(args.cohort_end)
    as_of = parse_cli_datetime(args.as_of)

    if cohort_start is None or cohort_end is None or as_of is None:
        sys.stderr.write("Invalid date format for --cohort-start, --cohort-end, or --as-of\n")
        sys.exit(1)

    try:
        result = summarize_acquisition_funnel(
            records=raw_data,
            cohort_start=cohort_start,
            cohort_end=cohort_end,
            as_of=as_of,
            by_keyword=args.by_keyword,
        )
    except Exception as e:
        sys.stderr.write(f"Aggregation error: {type(e).__name__}\n")
        sys.exit(1)

    output_str = json.dumps(result, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_str + "\n")
    else:
        print(output_str)


if __name__ == "__main__":
    main()
