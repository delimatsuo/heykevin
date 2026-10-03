"""SMS readiness audit tool — read-only operational reconciliation tool.

Follows pinned contract: 2026-10-02-sms-readiness-contract.md
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import itertools
import json
import sys
from typing import Any

import phonenumbers

SCHEMA_VERSION = "1.0"
MAX_RECORDS_DEFAULT = 5000
READ_TIMEOUT_SECONDS = 15.0

PROJECTED_CONTRACTOR_FIELDS = frozenset({
    "twilio_number",
    "active",
    "deletion_requested_at",
    "deactivated_at",
    "deleted_app_detected_at",
    "subscription_status",
    "subscription_tier",
    "subscription_expires",
    "trial_start",
    "last_inbound_call_at",
    "forwarding_last_seen_at",
    "owner_sms_enabled",
    "owner_sms_opted_out",
})

ALLOWED_SNAPSHOT_TOP_LEVEL_KEYS = frozenset({
    "schema_version",
    "observed_at",
    "expected_project",
    "expected_account_sid",
    "messaging_service_sid",
    "sources",
    "owned_numbers",
    "service_numbers",
    "contractors",
})

FIXED_SOURCE_NAMES = ("firestore", "twilio_incoming", "twilio_service")
ALLOWED_SOURCE_KEYS = frozenset({"complete", "records_read", "error"})
ALLOWED_SOURCE_ERRORS = frozenset({None, "binding_mismatch", "cap_overflow", "read_error", "unknown_error"})

ALLOWED_SUBSCRIPTION_STATUSES = frozenset({
    "trial",
    "active",
    "expired",
    "cancelled",
    "none",
})

ALLOWED_SUBSCRIPTION_TIERS = frozenset({
    "none",
    "personal",
    "business",
    "businessPro",
})


def parse_and_validate_e164(phone: Any) -> str | None:
    """Validate that phone is a canonical E.164 string without inferring region.

    Returns the canonical E.164 string if valid, or None if invalid/malformed.
    """
    if not isinstance(phone, str):
        return None
    if phone != phone.strip():
        return None
    if not phone.startswith("+"):
        return None
    try:
        parsed = phonenumbers.parse(phone, None)
        if not phonenumbers.is_valid_number(parsed):
            return None
        canonical = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        if canonical != phone:
            return None
        return canonical
    except Exception:
        return None


def parse_utc_iso(ts: Any) -> datetime | None:
    """Parse ISO8601 string or datetime into UTC datetime."""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_timestamp_to_utc(val: Any) -> datetime | None:
    """Parse numeric unix timestamp, ISO string, or datetime to UTC datetime."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        try:
            if val < 0 or val > 32503680000:  # Cap at year 3000
                return None
            return datetime.fromtimestamp(val, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    if isinstance(val, str):
        val_str = val.strip()
        try:
            num = float(val_str)
            if 0 <= num <= 32503680000:
                return datetime.fromtimestamp(num, tz=timezone.utc)
        except ValueError:
            pass
        return parse_utc_iso(val_str)
    return None


def _extract_capabilities(cap_obj: Any) -> dict[str, bool]:
    """Extract and bound capabilities to strictly boolean fields (sms, voice, mms).

    Omits missing or malformed/non-boolean fields rather than inventing facts or bool-coercing strings.
    """
    res: dict[str, bool] = {}
    allowed_keys = ("sms", "voice", "mms")
    if isinstance(cap_obj, dict):
        for k in allowed_keys:
            if k in cap_obj:
                val = cap_obj[k]
                if isinstance(val, bool):
                    res[k] = val
    elif cap_obj is not None:
        for k in allowed_keys:
            if hasattr(cap_obj, k):
                val = getattr(cap_obj, k)
                if isinstance(val, bool):
                    res[k] = val
    return res


def collect_snapshot(
    firestore_client: Any,
    twilio_client: Any,
    *,
    expected_project: str,
    expected_account_sid: str,
    messaging_service_sid: str,
    observed_at: str | datetime,
    max_records: int = MAX_RECORDS_DEFAULT,
) -> dict[str, Any]:
    """Collect read-only SMS readiness snapshot using injected authenticated clients.

    Uses passed clients only. Never instantiates settings, reads secrets, or constructs
    ambient credentials.
    """
    # Strict validation of max_records before any client read
    if (
        isinstance(max_records, bool)
        or not isinstance(max_records, int)
        or max_records < 1
        or max_records > MAX_RECORDS_DEFAULT
    ):
        raise ValueError("max_records must be a strict integer in range 1..5000")

    observed_at_dt = parse_utc_iso(observed_at)
    if observed_at_dt is None:
        raise ValueError("Invalid observed_at timestamp")
    observed_at_str = observed_at_dt.isoformat().replace("+00:00", "Z")

    # Check Firestore client binding
    firestore_complete = True
    firestore_error: str | None = None
    contractors: list[dict[str, Any]] = []

    client_project = getattr(firestore_client, "project", None)
    if client_project != expected_project:
        firestore_complete = False
        firestore_error = "binding_mismatch"
    else:
        try:
            query = (
                firestore_client.collection("contractors")
                .select(PROJECTED_CONTRACTOR_FIELDS)
                .limit(max_records + 1)
            )
            # Timeout of 15s required; inability to honor 15s fails source closed
            stream_iter = query.stream(timeout=READ_TIMEOUT_SECONDS)
            raw_docs = list(itertools.islice(stream_iter, max_records + 1))

            if len(raw_docs) > max_records:
                firestore_complete = False
                firestore_error = "cap_overflow"
                raw_docs = raw_docs[:max_records]

            for doc in raw_docs:
                data = doc.to_dict() if hasattr(doc, "to_dict") else (dict(doc) if isinstance(doc, dict) else None)
                if data is None or not isinstance(data, dict):
                    firestore_complete = False
                    if firestore_error is None:
                        firestore_error = "read_error"
                    continue
                # Keep only projected fields
                projected = {k: data[k] for k in PROJECTED_CONTRACTOR_FIELDS if k in data}
                contractors.append(projected)
        except Exception:
            firestore_complete = False
            firestore_error = "read_error"
            contractors = []

    # Check Twilio client binding
    twilio_incoming_complete = True
    twilio_incoming_error: str | None = None
    owned_numbers: list[dict[str, Any]] = []

    twilio_service_complete = True
    twilio_service_error: str | None = None
    service_numbers: list[dict[str, Any]] = []

    client_account_sid = getattr(twilio_client, "account_sid", None) or getattr(twilio_client, "username", None)
    if client_account_sid != expected_account_sid:
        twilio_incoming_complete = False
        twilio_incoming_error = "binding_mismatch"
        twilio_service_complete = False
        twilio_service_error = "binding_mismatch"
    else:
        # Fetch messaging service
        try:
            service = twilio_client.messaging.v1.services(messaging_service_sid).fetch()
            service_sid = getattr(service, "sid", None)
            service_account = getattr(service, "account_sid", None)
            if service_sid != messaging_service_sid or service_account != expected_account_sid:
                twilio_service_complete = False
                twilio_service_error = "binding_mismatch"
            else:
                # Read service phone numbers
                pns_list = twilio_client.messaging.v1.services(messaging_service_sid).phone_numbers.list(
                    limit=max_records + 1
                )
                if len(pns_list) > max_records:
                    twilio_service_complete = False
                    twilio_service_error = "cap_overflow"
                    pns_list = pns_list[:max_records]

                for pn in pns_list:
                    pn_sid = getattr(pn, "sid", None) if not isinstance(pn, dict) else pn.get("sid")
                    pn_num = getattr(pn, "phone_number", None) if not isinstance(pn, dict) else pn.get("phone_number")
                    pn_acc = getattr(pn, "account_sid", None) if not isinstance(pn, dict) else pn.get("account_sid")
                    pn_svc = getattr(pn, "service_sid", None) if not isinstance(pn, dict) else pn.get("service_sid")
                    pn_cap = getattr(pn, "capabilities", None) if not isinstance(pn, dict) else pn.get("capabilities")

                    canonical_phone = parse_and_validate_e164(pn_num)
                    if not isinstance(pn_sid, str) or not pn_sid.strip() or canonical_phone is None:
                        twilio_service_complete = False
                        if twilio_service_error is None:
                            twilio_service_error = "read_error"

                    if pn_acc != expected_account_sid or pn_svc != messaging_service_sid:
                        twilio_service_complete = False
                        twilio_service_error = "binding_mismatch"

                    service_numbers.append({
                        "sid": pn_sid if isinstance(pn_sid, str) else "",
                        "phone_number": canonical_phone if canonical_phone is not None else (str(pn_num) if pn_num is not None else ""),
                        "account_sid": pn_acc if isinstance(pn_acc, str) else "",
                        "service_sid": pn_svc if isinstance(pn_svc, str) else "",
                        "capabilities": _extract_capabilities(pn_cap),
                    })
        except Exception:
            twilio_service_complete = False
            twilio_service_error = "read_error"
            service_numbers = []

        # Read owned incoming phone numbers
        try:
            incoming_list = twilio_client.incoming_phone_numbers.list(limit=max_records + 1)
            if len(incoming_list) > max_records:
                twilio_incoming_complete = False
                twilio_incoming_error = "cap_overflow"
                incoming_list = incoming_list[:max_records]

            for pn in incoming_list:
                pn_sid = getattr(pn, "sid", None) if not isinstance(pn, dict) else pn.get("sid")
                pn_num = getattr(pn, "phone_number", None) if not isinstance(pn, dict) else pn.get("phone_number")
                pn_acc = getattr(pn, "account_sid", None) if not isinstance(pn, dict) else pn.get("account_sid")
                pn_cap = getattr(pn, "capabilities", None) if not isinstance(pn, dict) else pn.get("capabilities")

                canonical_phone = parse_and_validate_e164(pn_num)
                if not isinstance(pn_sid, str) or not pn_sid.strip() or canonical_phone is None:
                    twilio_incoming_complete = False
                    if twilio_incoming_error is None:
                        twilio_incoming_error = "read_error"

                if pn_acc != expected_account_sid:
                    twilio_incoming_complete = False
                    twilio_incoming_error = "binding_mismatch"

                owned_numbers.append({
                    "sid": pn_sid if isinstance(pn_sid, str) else "",
                    "phone_number": canonical_phone if canonical_phone is not None else (str(pn_num) if pn_num is not None else ""),
                    "account_sid": pn_acc if isinstance(pn_acc, str) else "",
                    "capabilities": _extract_capabilities(pn_cap),
                })
        except Exception:
            twilio_incoming_complete = False
            twilio_incoming_error = "read_error"
            owned_numbers = []

    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at_str,
        "expected_project": expected_project,
        "expected_account_sid": expected_account_sid,
        "messaging_service_sid": messaging_service_sid,
        "sources": {
            "firestore": {
                "complete": firestore_complete,
                "records_read": len(contractors),
                "error": firestore_error,
            },
            "twilio_incoming": {
                "complete": twilio_incoming_complete,
                "records_read": len(owned_numbers),
                "error": twilio_incoming_error,
            },
            "twilio_service": {
                "complete": twilio_service_complete,
                "records_read": len(service_numbers),
                "error": twilio_service_error,
            },
        },
        "owned_numbers": owned_numbers,
        "service_numbers": service_numbers,
        "contractors": contractors,
    }


def summarize_sms_readiness(
    snapshot: dict[str, Any],
    *,
    as_of: str | datetime,
    expected_project: str | None = None,
    expected_account_sid: str | None = None,
    messaging_service_sid: str | None = None,
) -> dict[str, Any]:
    """Pure summary function reconciling SMS readiness against contract.

    Never prints phones, contractor IDs, provider number IDs, raw unexpected keys/values
    or exception text. Fails closed on incomplete, stale, future or binding mismatch.
    """
    if not isinstance(snapshot, dict):
        raise ValueError("Invalid snapshot structure")

    # Reject unexpected top-level fields without echoing them
    snapshot_keys = set(snapshot.keys())
    if not snapshot_keys.issubset(ALLOWED_SNAPSHOT_TOP_LEVEL_KEYS):
        raise ValueError("Invalid snapshot: unexpected top-level field")

    # Check required top-level fields
    required_keys = ALLOWED_SNAPSHOT_TOP_LEVEL_KEYS
    if not required_keys.issubset(snapshot_keys):
        raise ValueError("Invalid snapshot: missing required top-level field")

    # Validate exact schema_version before reconciliation
    schema_ver = snapshot.get("schema_version")
    if schema_ver != SCHEMA_VERSION:
        raise ValueError("Invalid schema_version")

    as_of_dt = parse_utc_iso(as_of)
    if as_of_dt is None:
        raise ValueError("Invalid as_of timestamp format")
    as_of_iso = as_of_dt.isoformat().replace("+00:00", "Z")

    observed_dt = parse_utc_iso(snapshot.get("observed_at"))
    if observed_dt is None:
        raise ValueError("Invalid observed_at timestamp format")
    observed_at_iso = observed_dt.isoformat().replace("+00:00", "Z")

    # Check bindings
    snap_project = snapshot.get("expected_project")
    snap_account_sid = snapshot.get("expected_account_sid")
    snap_service_sid = snapshot.get("messaging_service_sid")

    binding_valid = True
    if not isinstance(snap_project, str) or not snap_project.strip():
        binding_valid = False
    if not isinstance(snap_account_sid, str) or not snap_account_sid.strip():
        binding_valid = False
    if not isinstance(snap_service_sid, str) or not snap_service_sid.strip():
        binding_valid = False

    if expected_project is not None and snap_project != expected_project:
        binding_valid = False
    if expected_account_sid is not None and snap_account_sid != expected_account_sid:
        binding_valid = False
    if messaging_service_sid is not None and snap_service_sid != messaging_service_sid:
        binding_valid = False

    # Check staleness (observed_at must not be > 15 min before as_of or in the future)
    delta_seconds = (as_of_dt - observed_dt).total_seconds()
    is_future = delta_seconds < 0
    is_stale = delta_seconds > 900.0  # 15 minutes

    raw_owned = snapshot.get("owned_numbers")
    raw_service = snapshot.get("service_numbers")
    raw_contractors = snapshot.get("contractors")

    if not isinstance(raw_owned, list) or not isinstance(raw_service, list) or not isinstance(raw_contractors, list):
        raise ValueError("Invalid snapshot data lists")

    # Cap each input list at 5000 BEFORE iteration, reject oversized input
    if (
        len(raw_owned) > MAX_RECORDS_DEFAULT
        or len(raw_service) > MAX_RECORDS_DEFAULT
        or len(raw_contractors) > MAX_RECORDS_DEFAULT
    ):
        raise ValueError("Input list exceeds maximum allowed size")

    # Process and sanitize sources data - NEVER echo verbatim
    sources_data_raw = snapshot.get("sources")
    sanitized_sources: dict[str, dict[str, Any]] = {}
    sources_complete = True

    source_list_map = {
        "firestore": raw_contractors,
        "twilio_incoming": raw_owned,
        "twilio_service": raw_service,
    }

    if not isinstance(sources_data_raw, dict):
        sources_complete = False
        for s_name in FIXED_SOURCE_NAMES:
            sanitized_sources[s_name] = {
                "complete": False,
                "records_read": 0,
                "error": "read_error",
            }
    else:
        # Check if unexpected source names exist or missing fixed source names
        raw_source_keys = set(sources_data_raw.keys())
        if raw_source_keys != set(FIXED_SOURCE_NAMES):
            sources_complete = False

        for s_name in FIXED_SOURCE_NAMES:
            src_entry = sources_data_raw.get(s_name)
            src_valid = True

            if not isinstance(src_entry, dict):
                sources_complete = False
                sanitized_sources[s_name] = {
                    "complete": False,
                    "records_read": 0,
                    "error": "read_error",
                }
                continue

            # Check for unexpected or missing keys inside source dictionary
            if not set(src_entry.keys()).issubset(ALLOWED_SOURCE_KEYS) or not ALLOWED_SOURCE_KEYS.issubset(set(src_entry.keys())):
                src_valid = False
                sources_complete = False

            # Validate complete
            comp = src_entry.get("complete")
            if not isinstance(comp, bool) or comp is not True:
                src_valid = False
                comp_clean = False
            else:
                comp_clean = True

            # Validate records_read: strict int in 0..5000
            rr = src_entry.get("records_read")
            if isinstance(rr, bool) or not isinstance(rr, int) or rr < 0 or rr > MAX_RECORDS_DEFAULT:
                src_valid = False
                sources_complete = False
                rr_clean = 0
            else:
                rr_clean = rr

            # Validate error
            err = src_entry.get("error")
            if err is None:
                err_clean = None
            elif isinstance(err, str) and err in ALLOWED_SOURCE_ERRORS and err is not None:
                err_clean = err
                src_valid = False
                sources_complete = False
            else:
                err_clean = "unknown_error"
                src_valid = False
                sources_complete = False

            # Count check: records_read must equal corresponding source list length
            target_list = source_list_map.get(s_name)
            if target_list is not None and rr_clean != len(target_list):
                src_valid = False
                sources_complete = False

            is_this_source_complete = src_valid and comp_clean and (err_clean is None)
            if not is_this_source_complete:
                sources_complete = False

            sanitized_sources[s_name] = {
                "complete": is_this_source_complete,
                "records_read": rr_clean,
                "error": err_clean,
            }

    # Process and validate owned numbers
    owned_sids_seen: set[str] = set()
    duplicate_owned_sids: set[str] = set()
    owned_phones_seen: set[str] = set()
    duplicate_owned_phones: set[str] = set()
    owned_sid_to_phone: dict[str, str] = {}
    owned_phone_to_sid: dict[str, str] = {}
    owned_inventory_total = len(raw_owned)

    for item in raw_owned:
        if not isinstance(item, dict):
            raise ValueError("Invalid row structure in owned_numbers")
        sid = item.get("sid")
        raw_phone = item.get("phone_number")
        acc = item.get("account_sid")
        if acc != snap_account_sid:
            binding_valid = False
            sanitized_sources["twilio_incoming"]["complete"] = False
            sources_complete = False

        if not isinstance(sid, str) or not sid.strip():
            sanitized_sources["twilio_incoming"]["complete"] = False
            sources_complete = False
            continue
        sid = sid.strip()
        if sid in owned_sids_seen:
            duplicate_owned_sids.add(sid)
        owned_sids_seen.add(sid)

        e164 = parse_and_validate_e164(raw_phone)
        if e164 is None or e164 != raw_phone:
            sanitized_sources["twilio_incoming"]["complete"] = False
            sources_complete = False
            continue

        if e164 in owned_phones_seen:
            duplicate_owned_phones.add(e164)
        owned_phones_seen.add(e164)

        owned_sid_to_phone[sid] = e164
        owned_phone_to_sid[e164] = sid

    # Process and validate service numbers (pool)
    pool_sids_seen: set[str] = set()
    duplicate_pool_sids: set[str] = set()
    pool_phones_seen: set[str] = set()
    duplicate_pool_phones: set[str] = set()
    pool_sids_absent_from_inventory: set[str] = set()
    pool_sid_phone_conflicts: set[str] = set()
    service_numbers_total = len(raw_service)

    for item in raw_service:
        if not isinstance(item, dict):
            raise ValueError("Invalid row structure in service_numbers")
        sid = item.get("sid")
        raw_phone = item.get("phone_number")
        acc = item.get("account_sid")
        svc = item.get("service_sid")
        if acc != snap_account_sid or svc != snap_service_sid:
            binding_valid = False
            sanitized_sources["twilio_service"]["complete"] = False
            sources_complete = False

        if not isinstance(sid, str) or not sid.strip():
            sanitized_sources["twilio_service"]["complete"] = False
            sources_complete = False
            continue
        sid = sid.strip()
        if sid in pool_sids_seen:
            duplicate_pool_sids.add(sid)
        pool_sids_seen.add(sid)

        e164 = parse_and_validate_e164(raw_phone)
        if e164 is None or e164 != raw_phone:
            sanitized_sources["twilio_service"]["complete"] = False
            sources_complete = False
            continue

        if e164 in pool_phones_seen:
            duplicate_pool_phones.add(e164)
        pool_phones_seen.add(e164)

        # Detect pool entries whose SID is absent from inventory
        if sid not in owned_sids_seen:
            pool_sids_absent_from_inventory.add(sid)

        # Detect pool number/SID conflicts
        if sid in owned_sid_to_phone and owned_sid_to_phone[sid] != e164:
            pool_sid_phone_conflicts.add(sid)
        if e164 in owned_phone_to_sid and owned_phone_to_sid[e164] != sid:
            pool_sid_phone_conflicts.add(sid)
            if e164 in owned_phone_to_sid:
                pool_sid_phone_conflicts.add(owned_phone_to_sid[e164])

    # If pool SID is absent from owned inventory: mark source / reconciliation incomplete
    if len(pool_sids_absent_from_inventory) > 0:
        sanitized_sources["twilio_service"]["complete"] = False
        sources_complete = False

    # Inventory pool membership counts
    pool_present_count = 0
    pool_missing_count = 0
    pool_unknown_count = 0

    if not sources_complete or not binding_valid:
        pool_unknown_count = owned_inventory_total
    else:
        for sid, e164 in owned_sid_to_phone.items():
            has_owned_anomaly = (
                sid in duplicate_owned_sids
                or e164 in duplicate_owned_phones
            )
            has_pool_anomaly = (
                sid in duplicate_pool_sids
                or e164 in duplicate_pool_phones
                or sid in pool_sid_phone_conflicts
            )
            if has_owned_anomaly or has_pool_anomaly:
                pool_unknown_count += 1
            elif sid in pool_sids_seen:
                pool_present_count += 1
            else:
                pool_missing_count += 1

        unparsed_owned = owned_inventory_total - (pool_present_count + pool_missing_count + pool_unknown_count)
        if unparsed_owned > 0:
            pool_unknown_count += unparsed_owned

    # Process contractors and assignments
    total_contractors_count = 0
    assigned_contractors_by_phone: dict[str, list[dict[str, Any]]] = {}
    malformed_assignments_count = 0

    for contractor in raw_contractors:
        if not isinstance(contractor, dict):
            raise ValueError("Invalid row structure in contractors")
        total_contractors_count += 1
        raw_num = contractor.get("twilio_number")
        if raw_num is None or raw_num == "":
            # Unassigned contractor sentinel (None or exact empty string)
            continue
        if not isinstance(raw_num, str):
            malformed_assignments_count += 1
            continue
        e164 = parse_and_validate_e164(raw_num)
        if e164 is None or e164 != raw_num:
            malformed_assignments_count += 1
        else:
            assigned_contractors_by_phone.setdefault(e164, []).append(contractor)

    # Reconcile assignments
    assigned_owned_count = 0
    unassigned_owned_count = 0
    ambiguous_assignments_count = 0
    unowned_assignments_count = 0
    missing_owned_unique_count = 0

    # Unassigned owned numbers
    for e164 in owned_phone_to_sid:
        if e164 not in assigned_contractors_by_phone:
            unassigned_owned_count += 1

    for e164, c_list in assigned_contractors_by_phone.items():
        num_c = len(c_list)
        is_in_owned = e164 in owned_phone_to_sid

        if not is_in_owned:
            unowned_assignments_count += num_c
        else:
            assigned_owned_count += num_c
            sid = owned_phone_to_sid[e164]
            is_duplicate_assign = num_c > 1
            is_duplicate_owned = (
                e164 in duplicate_owned_phones
                or sid in duplicate_owned_sids
            )
            is_pool_anomaly = (
                sid in duplicate_pool_sids
                or e164 in duplicate_pool_phones
                or sid in pool_sid_phone_conflicts
            )

            if is_duplicate_assign or is_duplicate_owned or is_pool_anomaly:
                ambiguous_assignments_count += num_c
            else:
                if sources_complete and binding_valid:
                    if sid not in pool_sids_seen:
                        missing_owned_unique_count += num_c

    # Check for ambiguity anomalies across inventory, pool, and contractor assignments
    has_tenant_duplicates = any(len(c_list) > 1 for c_list in assigned_contractors_by_phone.values())
    has_duplicate_owned_sids = len(duplicate_owned_sids) > 0
    has_duplicate_owned_phones = len(duplicate_owned_phones) > 0
    has_duplicate_pool_sids = len(duplicate_pool_sids) > 0
    has_duplicate_pool_phones = len(duplicate_pool_phones) > 0
    has_pool_sid_phone_conflicts = len(pool_sid_phone_conflicts) > 0

    has_ambiguity = (
        has_duplicate_owned_sids
        or has_duplicate_owned_phones
        or has_duplicate_pool_sids
        or has_duplicate_pool_phones
        or has_pool_sid_phone_conflicts
        or has_tenant_duplicates
    )

    # Review candidates: ONLY complete unambiguous missing-membership join
    is_complete_overall = (
        binding_valid
        and sources_complete
        and not is_stale
        and not is_future
        and len(pool_sids_absent_from_inventory) == 0
        and not has_ambiguity
    )

    if is_complete_overall:
        review_candidates_count = missing_owned_unique_count
    else:
        review_candidates_count = 0

    # Build cohort classifications for every contractor
    cohort_counts: dict[tuple[str, ...], int] = {}

    for contractor in raw_contractors:
        raw_num = contractor.get("twilio_number")
        if raw_num is None or raw_num == "":
            membership = "none"
        elif not isinstance(raw_num, str):
            membership = "malformed"
        else:
            e164 = parse_and_validate_e164(raw_num)
            if e164 is None or e164 != raw_num:
                membership = "malformed"
            elif e164 not in owned_phone_to_sid:
                membership = "unowned"
            else:
                sid = owned_phone_to_sid[e164]
                is_duplicate_assign = len(assigned_contractors_by_phone.get(e164, [])) > 1
                is_duplicate_owned = (
                    e164 in duplicate_owned_phones
                    or sid in duplicate_owned_sids
                )
                is_pool_anomaly = (
                    sid in duplicate_pool_sids
                    or e164 in duplicate_pool_phones
                    or sid in pool_sid_phone_conflicts
                )

                if is_duplicate_assign or is_duplicate_owned or is_pool_anomaly:
                    membership = "ambiguous"
                elif not sources_complete or not binding_valid or len(pool_sids_absent_from_inventory) > 0:
                    membership = "unknown"
                else:
                    if sid in pool_sids_seen:
                        membership = "present"
                    else:
                        membership = "missing"

        # active dimension
        act = contractor.get("active")
        if act is True:
            active_dim = "true"
        elif act is False:
            active_dim = "false"
        elif act is None:
            active_dim = "missing"
        else:
            active_dim = "malformed"

        # deletion marker dimension
        del_req = contractor.get("deletion_requested_at")
        deact = contractor.get("deactivated_at")
        del_app = contractor.get("deleted_app_detected_at")

        del_req_dt = parse_timestamp_to_utc(del_req) if del_req is not None else None
        deact_dt = parse_timestamp_to_utc(deact) if deact is not None else None
        del_app_dt = parse_timestamp_to_utc(del_app) if del_app is not None else None

        has_malformed_del = (
            (del_req is not None and del_req_dt is None)
            or (deact is not None and deact_dt is None)
            or (del_app is not None and del_app_dt is None)
        )

        if has_malformed_del:
            deletion_dim = "malformed"
        else:
            set_markers = []
            if del_req_dt is not None:
                set_markers.append("deletion_requested")
            if deact_dt is not None:
                set_markers.append("deactivated")
            if del_app_dt is not None:
                set_markers.append("app_deleted")

            if len(set_markers) == 0:
                deletion_dim = "none"
            elif len(set_markers) == 1:
                deletion_dim = set_markers[0]
            else:
                deletion_dim = "multiple"

        # subscription_status dimension
        st = contractor.get("subscription_status")
        if st is None:
            status_dim = "missing"
        elif isinstance(st, str) and st in ALLOWED_SUBSCRIPTION_STATUSES:
            status_dim = st
        else:
            status_dim = "unknown"

        # subscription_tier dimension
        tier = contractor.get("subscription_tier")
        if tier is None:
            tier_dim = "missing"
        elif isinstance(tier, str) and tier in ALLOWED_SUBSCRIPTION_TIERS:
            tier_dim = tier
        else:
            tier_dim = "unknown"

        # expiry_validity dimension
        exp = contractor.get("subscription_expires")
        if exp is None:
            expiry_dim = "missing"
        else:
            exp_dt = parse_timestamp_to_utc(exp)
            if exp_dt is None:
                expiry_dim = "malformed"
            elif exp_dt > as_of_dt:
                expiry_dim = "unexpired"
            else:
                expiry_dim = "expired"

        # trial_validity dimension
        trial = contractor.get("trial_start")
        if trial is None:
            trial_dim = "missing"
        else:
            trial_dt = parse_timestamp_to_utc(trial)
            if trial_dt is None:
                trial_dim = "malformed"
            elif trial_dt > as_of_dt:
                trial_dim = "future_trial"
            elif trial_dt <= as_of_dt < trial_dt + timedelta(days=14):
                trial_dim = "within_trial"
            else:
                trial_dim = "past_trial"

        # inbound_age dimension
        inbound = contractor.get("last_inbound_call_at")
        if inbound is None:
            inbound_dim = "missing"
        else:
            inbound_dt = parse_timestamp_to_utc(inbound)
            if inbound_dt is None:
                inbound_dim = "malformed"
            elif inbound_dt > as_of_dt:
                inbound_dim = "future"
            else:
                age_secs = (as_of_dt - inbound_dt).total_seconds()
                if age_secs <= 7 * 86400:
                    inbound_dim = "within_7d"
                elif age_secs <= 30 * 86400:
                    inbound_dim = "within_30d"
                elif age_secs <= 90 * 86400:
                    inbound_dim = "within_90d"
                else:
                    inbound_dim = "over_90d"

        # forwarding_age dimension
        fwd = contractor.get("forwarding_last_seen_at")
        if fwd is None:
            fwd_dim = "missing"
        else:
            fwd_dt = parse_timestamp_to_utc(fwd)
            if fwd_dt is None:
                fwd_dim = "malformed"
            elif fwd_dt > as_of_dt:
                fwd_dim = "future"
            else:
                age_secs = (as_of_dt - fwd_dt).total_seconds()
                if age_secs <= 7 * 86400:
                    fwd_dim = "within_7d"
                elif age_secs <= 30 * 86400:
                    fwd_dim = "within_30d"
                elif age_secs <= 90 * 86400:
                    fwd_dim = "within_90d"
                else:
                    fwd_dim = "over_90d"

        # owner_sms_preference dimension
        sms_en = contractor.get("owner_sms_enabled")
        sms_out = contractor.get("owner_sms_opted_out")

        if sms_en is None and sms_out is None:
            pref_dim = "missing"
        elif (sms_en is not None and not isinstance(sms_en, bool)) or (
            sms_out is not None and not isinstance(sms_out, bool)
        ):
            pref_dim = "unknown"
        elif sms_out is True:
            pref_dim = "opted_out"
        elif sms_en is True and sms_out is False:
            pref_dim = "opted_in"
        elif sms_en is False and sms_out is False:
            pref_dim = "disabled"
        elif sms_en is False and sms_out is None:
            pref_dim = "disabled"
        else:
            pref_dim = "unknown"

        cohort_key = (
            membership,
            active_dim,
            deletion_dim,
            status_dim,
            tier_dim,
            expiry_dim,
            trial_dim,
            inbound_dim,
            fwd_dim,
            pref_dim,
        )
        cohort_counts[cohort_key] = cohort_counts.get(cohort_key, 0) + 1

    cohorts_list: list[dict[str, Any]] = []
    for key, count in sorted(cohort_counts.items()):
        cohorts_list.append({
            "membership": key[0],
            "active": key[1],
            "deletion_marker": key[2],
            "subscription_status": key[3],
            "subscription_tier": key[4],
            "expiry_validity": key[5],
            "trial_validity": key[6],
            "inbound_age": key[7],
            "forwarding_age": key[8],
            "owner_sms_preference": key[9],
            "count": count,
        })

    limitations = [
        "Snapshot provenance is caller supplied and not independently certified.",
        "Review candidates represent join status only and do not prove campaign eligibility, consent or repair authorization.",
        "Stale, future or incomplete snapshots yield zero review candidates.",
    ]
    if not binding_valid:
        limitations.append("Binding mismatch detected; reconciliation failed closed.")
    if is_stale:
        limitations.append("Snapshot is older than 15 minutes relative to as_of timestamp.")
    if is_future:
        limitations.append("Snapshot observation timestamp is in the future relative to as_of timestamp.")
    if not sources_complete or len(pool_sids_absent_from_inventory) > 0:
        limitations.append("One or more snapshot sources are incomplete or encountered read errors.")
    if has_ambiguity:
        limitations.append("Ambiguity in provider inventory, pool membership, or tenant assignments blocks candidate selection.")

    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of_iso,
        "observed_at": observed_at_iso,
        "completeness": {
            "is_complete": is_complete_overall,
            "is_stale": is_stale,
            "is_future": is_future,
            "bindings_valid": binding_valid,
            "sources": sanitized_sources,
        },
        "limitations": limitations,
        "totals": {
            "owned_inventory": owned_inventory_total,
            "pool_numbers": service_numbers_total,
            "pool_membership": {
                "present": pool_present_count,
                "missing": pool_missing_count,
                "unknown": pool_unknown_count,
            },
            "assignments": {
                "total_contractors": total_contractors_count,
                "assigned_owned": assigned_owned_count,
                "unassigned_owned": unassigned_owned_count,
                "malformed_assignments": malformed_assignments_count,
                "ambiguous_assignments": ambiguous_assignments_count,
                "unowned_assignments": unowned_assignments_count,
                "missing_owned_unique": missing_owned_unique_count,
            },
            "anomalies": {
                "duplicate_owned_sids": len(duplicate_owned_sids),
                "duplicate_owned_numbers": len(duplicate_owned_phones),
                "duplicate_pool_sids": len(duplicate_pool_sids),
                "duplicate_pool_numbers": len(duplicate_pool_phones),
                "pool_sids_absent_from_inventory": len(pool_sids_absent_from_inventory),
                "pool_sid_phone_conflicts": len(pool_sid_phone_conflicts),
            },
            "review_candidates": review_candidates_count,
        },
        "cohorts": cohorts_list,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for offline SMS readiness audit tool."""
    parser = argparse.ArgumentParser(
        description="Offline SMS readiness audit tool (read-only aggregate reporting)"
    )
    parser.add_argument("--snapshot", default="-", help="Path to snapshot JSON file or '-' for stdin")
    parser.add_argument("--expected-project", required=True, help="Expected GCP project ID")
    parser.add_argument("--expected-account-sid", required=True, help="Expected Twilio Account SID")
    parser.add_argument("--messaging-service-sid", required=True, help="Expected Messaging Service SID")
    parser.add_argument("--as-of", required=True, help="Explicit UTC timestamp (ISO8601)")

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    # Read snapshot
    try:
        if args.snapshot == "-":
            raw_text = sys.stdin.read()
        else:
            with open(args.snapshot, "r", encoding="utf-8") as f:
                raw_text = f.read()
        snapshot_data = json.loads(raw_text)
    except Exception:
        sys.stderr.write("Error: Failed to read or parse snapshot JSON\n")
        return 1

    try:
        report = summarize_sms_readiness(
            snapshot_data,
            as_of=args.as_of,
            expected_project=args.expected_project,
            expected_account_sid=args.expected_account_sid,
            messaging_service_sid=args.messaging_service_sid,
        )
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return 0
    except Exception:
        sys.stderr.write("Error: Failed to execute SMS readiness summary\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
