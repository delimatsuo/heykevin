"""Unit tests for offline SMS readiness audit tool and collector.

Follows pinned contract: 2026-10-02-sms-readiness-contract.md
"""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import sys
from typing import Any
import pytest

from scripts.sms_readiness_audit import (
    ALLOWED_SNAPSHOT_TOP_LEVEL_KEYS,
    FIXED_SOURCE_NAMES,
    MAX_RECORDS_DEFAULT,
    PROJECTED_CONTRACTOR_FIELDS,
    READ_TIMEOUT_SECONDS,
    SCHEMA_VERSION,
    collect_snapshot,
    main,
    parse_and_validate_e164,
    parse_timestamp_to_utc,
    parse_utc_iso,
    summarize_sms_readiness,
)


class FakeFirestoreQuery:
    def __init__(
        self,
        docs: Any,
        forbidden_tracker: list[str],
        *,
        expected_limit: int = 5001,
        reject_timeout: bool = False,
    ):
        self._docs = docs
        self._forbidden_tracker = forbidden_tracker
        self._selected_fields: frozenset[str] | None = None
        self._limit_val: int | None = None
        self._expected_limit = expected_limit
        self._reject_timeout = reject_timeout

    def select(self, fields: Any) -> "FakeFirestoreQuery":
        self._selected_fields = frozenset(fields)
        return self

    def limit(self, count: int) -> "FakeFirestoreQuery":
        self._limit_val = count
        return self

    def stream(self, timeout: float | None = None):
        if self._reject_timeout:
            raise TypeError("stream() does not accept timeout keyword argument")
        # Explicitly assert literal expected projection set and timeout
        assert timeout == 15.0
        assert self._selected_fields == frozenset({
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
        assert self._limit_val == self._expected_limit
        for doc_data in self._docs:
            yield FakeDocSnapshot(doc_data)

    def add(self, *args, **kwargs):
        self._forbidden_tracker.append("firestore.add")
        raise AssertionError("Mutation method 'add' is strictly forbidden")

    def set(self, *args, **kwargs):
        self._forbidden_tracker.append("firestore.set")
        raise AssertionError("Mutation method 'set' is strictly forbidden")

    def update(self, *args, **kwargs):
        self._forbidden_tracker.append("firestore.update")
        raise AssertionError("Mutation method 'update' is strictly forbidden")

    def delete(self, *args, **kwargs):
        self._forbidden_tracker.append("firestore.delete")
        raise AssertionError("Mutation method 'delete' is strictly forbidden")


class FakeDocSnapshot:
    def __init__(self, data: dict[str, Any]):
        self._data = data

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


class FakeFirestoreClient:
    def __init__(
        self,
        project: str,
        docs: Any,
        *,
        expected_limit: int = 5001,
        expected_max_records: int | None = None,
        max_records: int | None = None,
        limit: int | None = None,
        reject_timeout: bool = False,
    ):
        self.project = project
        self._docs = docs
        if max_records is not None:
            self._expected_limit = max_records + 1
        elif expected_max_records is not None:
            self._expected_limit = expected_max_records + 1
        elif limit is not None:
            self._expected_limit = limit
        else:
            self._expected_limit = expected_limit
        self._reject_timeout = reject_timeout
        self.forbidden_calls: list[str] = []

    def collection(self, name: str) -> FakeFirestoreQuery:
        assert name == "contractors", f"Forbidden collection access: {name}"
        return FakeFirestoreQuery(
            self._docs,
            self.forbidden_calls,
            expected_limit=self._expected_limit,
            reject_timeout=self._reject_timeout,
        )

    def document(self, path: str):
        self.forbidden_calls.append("firestore.document")
        raise AssertionError("Direct document reference is forbidden")


class FakeTwilioResource:
    def __init__(self, data: dict[str, Any]):
        for k, v in data.items():
            setattr(self, k, v)


class FakeTwilioPhoneNumbers:
    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        expected_limit: int = 5001,
        max_records: int | None = None,
        expected_max_records: int | None = None,
        limit: int | None = None,
    ):
        self._items = items
        if max_records is not None:
            self._expected_limit = max_records + 1
        elif expected_max_records is not None:
            self._expected_limit = expected_max_records + 1
        elif limit is not None:
            self._expected_limit = limit
        else:
            self._expected_limit = expected_limit

    def list(self, limit: int | None = None):
        assert limit == self._expected_limit
        return [FakeTwilioResource(d) for d in self._items]


class FakeTwilioService:
    def __init__(
        self,
        sid: str,
        account_sid: str,
        phone_numbers: list[dict[str, Any]],
        *,
        expected_limit: int = 5001,
        max_records: int | None = None,
        expected_max_records: int | None = None,
        limit: int | None = None,
    ):
        self.sid = sid
        self.account_sid = account_sid
        self.phone_numbers = FakeTwilioPhoneNumbers(
            phone_numbers,
            expected_limit=expected_limit,
            max_records=max_records,
            expected_max_records=expected_max_records,
            limit=limit,
        )

    def fetch(self):
        return self


class FakeTwilioServices:
    def __init__(self, services_map: dict[str, FakeTwilioService]):
        self._services = services_map

    def __call__(self, sid: str):
        if sid in self._services:
            return self._services[sid]
        raise ValueError("Service not found")


class FakeTwilioMessagingV1:
    def __init__(self, services_map: dict[str, FakeTwilioService]):
        self.services = FakeTwilioServices(services_map)


class FakeTwilioMessaging:
    def __init__(self, services_map: dict[str, FakeTwilioService]):
        self.v1 = FakeTwilioMessagingV1(services_map)


class FakeTwilioIncomingPhoneNumbers:
    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        expected_limit: int = 5001,
        max_records: int | None = None,
        expected_max_records: int | None = None,
        limit: int | None = None,
    ):
        self._items = items
        if max_records is not None:
            self._expected_limit = max_records + 1
        elif expected_max_records is not None:
            self._expected_limit = expected_max_records + 1
        elif limit is not None:
            self._expected_limit = limit
        else:
            self._expected_limit = expected_limit

    def list(self, limit: int | None = None):
        assert limit == self._expected_limit
        return [FakeTwilioResource(d) for d in self._items]


class FakeTwilioClient:
    def __init__(
        self,
        account_sid: str,
        services: dict[str, FakeTwilioService],
        incoming_numbers: list[dict[str, Any]],
        *,
        expected_limit: int = 5001,
        max_records: int | None = None,
        expected_max_records: int | None = None,
        limit: int | None = None,
    ):
        self.account_sid = account_sid
        self.messaging = FakeTwilioMessaging(services)
        self.incoming_phone_numbers = FakeTwilioIncomingPhoneNumbers(
            incoming_numbers,
            expected_limit=expected_limit,
            max_records=max_records,
            expected_max_records=expected_max_records,
            limit=limit,
        )
        self.forbidden_calls: list[str] = []

    @property
    def messages(self):
        self.forbidden_calls.append("twilio.messages")
        raise AssertionError("Twilio messages access is strictly forbidden")

    @property
    def calls(self):
        self.forbidden_calls.append("twilio.calls")
        raise AssertionError("Twilio calls access is strictly forbidden")


def _make_sample_snapshot(
    *,
    owned_count: int = 29,
    registered_count: int = 19,
    contractor_count: int = 29,
    observed_at: str = "2026-10-02T20:00:00Z",
    project: str = "test-project",
    account_sid: str = "ACtestaccount0000000000000000000000",
    service_sid: str = "MGtestservice0000000000000000000000",
) -> dict[str, Any]:
    owned_numbers = []
    for i in range(1, owned_count + 1):
        num_str = f"+1415555{i:04d}"
        sid_str = f"PN{i:04d}"
        owned_numbers.append({
            "sid": sid_str,
            "phone_number": num_str,
            "account_sid": account_sid,
            "capabilities": {"sms": True, "voice": True, "mms": False},
        })

    service_numbers = []
    for i in range(1, registered_count + 1):
        num_str = f"+1415555{i:04d}"
        sid_str = f"PN{i:04d}"
        service_numbers.append({
            "sid": sid_str,
            "phone_number": num_str,
            "account_sid": account_sid,
            "service_sid": service_sid,
            "capabilities": {"sms": True, "voice": True, "mms": False},
        })

    contractors = []
    for i in range(1, contractor_count + 1):
        num_str = f"+1415555{i:04d}"
        contractors.append({
            "twilio_number": num_str,
            "active": True,
            "subscription_status": "active",
            "subscription_tier": "personal",
            "subscription_expires": 1790000000,
            "trial_start": 1780000000,
            "last_inbound_call_at": 1790000000,
            "forwarding_last_seen_at": 1790000000,
            "owner_sms_enabled": True,
            "owner_sms_opted_out": False,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "expected_project": project,
        "expected_account_sid": account_sid,
        "messaging_service_sid": service_sid,
        "sources": {
            "firestore": {"complete": True, "records_read": contractor_count, "error": None},
            "twilio_incoming": {"complete": True, "records_read": owned_count, "error": None},
            "twilio_service": {"complete": True, "records_read": registered_count, "error": None},
        },
        "owned_numbers": owned_numbers,
        "service_numbers": service_numbers,
        "contractors": contractors,
    }


def test_synthetic_29_owned_19_registered_yields_10_missing_memberships():
    """Synthetic 29 owned / 19 registered inventory yields 10 missing memberships and review candidates."""
    as_of = "2026-10-02T20:05:00Z"
    snapshot = _make_sample_snapshot(owned_count=29, registered_count=19, contractor_count=29)

    report = summarize_sms_readiness(
        snapshot,
        as_of=as_of,
        expected_project="test-project",
        expected_account_sid="ACtestaccount0000000000000000000000",
        messaging_service_sid="MGtestservice0000000000000000000000",
    )

    assert report["completeness"]["is_complete"] is True
    assert report["totals"]["owned_inventory"] == 29
    assert report["totals"]["pool_numbers"] == 19
    assert report["totals"]["pool_membership"]["present"] == 19
    assert report["totals"]["pool_membership"]["missing"] == 10
    assert report["totals"]["pool_membership"]["unknown"] == 0
    assert report["totals"]["assignments"]["total_contractors"] == 29
    assert report["totals"]["assignments"]["assigned_owned"] == 29
    assert report["totals"]["assignments"]["unassigned_owned"] == 0
    assert report["totals"]["assignments"]["missing_owned_unique"] == 10
    assert report["totals"]["anomalies"]["duplicate_owned_sids"] == 0
    assert report["totals"]["anomalies"]["duplicate_owned_numbers"] == 0
    assert report["totals"]["anomalies"]["duplicate_pool_sids"] == 0
    assert report["totals"]["anomalies"]["duplicate_pool_numbers"] == 0
    assert report["totals"]["anomalies"]["pool_sids_absent_from_inventory"] == 0
    assert report["totals"]["anomalies"]["pool_sid_phone_conflicts"] == 0
    assert report["totals"]["review_candidates"] == 10


def test_independent_dimension_intersections_and_unknowns():
    """Active, subscription, deletion, activity, and preference intersections remain independent."""
    as_of = "2026-10-02T20:10:00Z"
    as_of_dt = parse_utc_iso(as_of)
    assert as_of_dt is not None
    as_of_ts = as_of_dt.timestamp()

    contractors = [
        # Normal active opted-in
        {
            "twilio_number": "+14155550001",
            "active": True,
            "subscription_status": "active",
            "subscription_tier": "personal",
            "subscription_expires": as_of_ts + 86400,
            "trial_start": as_of_ts - 5 * 86400,
            "last_inbound_call_at": as_of_ts - 2 * 86400,
            "forwarding_last_seen_at": as_of_ts - 2 * 86400,
            "owner_sms_enabled": True,
            "owner_sms_opted_out": False,
        },
        # Inactive, expired subscription, deleted app, opted out
        {
            "twilio_number": "+14155550002",
            "active": False,
            "deleted_app_detected_at": as_of_ts - 10 * 86400,
            "subscription_status": "expired",
            "subscription_tier": "business",
            "subscription_expires": as_of_ts - 10 * 86400,
            "trial_start": as_of_ts - 30 * 86400,
            "last_inbound_call_at": as_of_ts - 50 * 86400,
            "forwarding_last_seen_at": as_of_ts - 50 * 86400,
            "owner_sms_enabled": False,
            "owner_sms_opted_out": True,
        },
        # All fields missing / None
        {
            "twilio_number": "+14155550003",
            "active": None,
            "subscription_status": None,
            "subscription_tier": None,
            "subscription_expires": None,
            "trial_start": None,
            "last_inbound_call_at": None,
            "forwarding_last_seen_at": None,
            "owner_sms_enabled": None,
            "owner_sms_opted_out": None,
        },
        # Malformed / unknown fields
        {
            "twilio_number": "+14155550004",
            "active": "not_a_bool",
            "deletion_requested_at": "bad_date",
            "subscription_status": "unrecognized_status",
            "subscription_tier": "unrecognized_tier",
            "subscription_expires": "not_a_timestamp",
            "trial_start": "not_a_timestamp",
            "last_inbound_call_at": "bad_inbound",
            "forwarding_last_seen_at": "bad_fwd",
            "owner_sms_enabled": "not_bool",
            "owner_sms_opted_out": "not_bool",
        },
    ]

    snapshot = _make_sample_snapshot(owned_count=4, registered_count=4, contractor_count=4)
    snapshot["contractors"] = contractors

    report = summarize_sms_readiness(snapshot, as_of=as_of)
    cohorts = report["cohorts"]

    c_active = [c for c in cohorts if c["active"] == "true"][0]
    assert c_active["subscription_status"] == "active"
    assert c_active["subscription_tier"] == "personal"
    assert c_active["expiry_validity"] == "unexpired"
    assert c_active["trial_validity"] == "within_trial"
    assert c_active["inbound_age"] == "within_7d"
    assert c_active["owner_sms_preference"] == "opted_in"
    assert c_active["deletion_marker"] == "none"

    c_deleted = [c for c in cohorts if c["deletion_marker"] == "app_deleted"][0]
    assert c_deleted["active"] == "false"
    assert c_deleted["subscription_status"] == "expired"
    assert c_deleted["expiry_validity"] == "expired"
    assert c_deleted["trial_validity"] == "past_trial"
    assert c_deleted["inbound_age"] == "within_90d"
    assert c_deleted["owner_sms_preference"] == "opted_out"

    c_missing = [c for c in cohorts if c["active"] == "missing"][0]
    assert c_missing["subscription_status"] == "missing"
    assert c_missing["subscription_tier"] == "missing"
    assert c_missing["expiry_validity"] == "missing"
    assert c_missing["trial_validity"] == "missing"
    assert c_missing["inbound_age"] == "missing"
    assert c_missing["owner_sms_preference"] == "missing"
    assert c_missing["deletion_marker"] == "none"

    c_malformed = [c for c in cohorts if c["active"] == "malformed"][0]
    assert c_malformed["subscription_status"] == "unknown"
    assert c_malformed["subscription_tier"] == "unknown"
    assert c_malformed["expiry_validity"] == "malformed"
    assert c_malformed["trial_validity"] == "malformed"
    assert c_malformed["inbound_age"] == "malformed"
    assert c_malformed["owner_sms_preference"] == "unknown"
    assert c_malformed["deletion_marker"] == "malformed"


def test_binding_mismatch_fails_closed():
    """Binding mismatch fails closed and produces 0 review candidates."""
    as_of = "2026-10-02T20:05:00Z"
    snapshot = _make_sample_snapshot()

    report_bad_proj = summarize_sms_readiness(snapshot, as_of=as_of, expected_project="wrong-project")
    assert report_bad_proj["completeness"]["bindings_valid"] is False
    assert report_bad_proj["totals"]["review_candidates"] == 0
    assert report_bad_proj["totals"]["assignments"]["missing_owned_unique"] == 0

    report_bad_acc = summarize_sms_readiness(
        snapshot, as_of=as_of, expected_account_sid="ACwrongaccount00000000000000000000"
    )
    assert report_bad_acc["completeness"]["bindings_valid"] is False
    assert report_bad_acc["totals"]["review_candidates"] == 0
    assert report_bad_acc["totals"]["assignments"]["missing_owned_unique"] == 0

    report_bad_svc = summarize_sms_readiness(
        snapshot, as_of=as_of, messaging_service_sid="MGwrongservice00000000000000000000"
    )
    assert report_bad_svc["completeness"]["bindings_valid"] is False
    assert report_bad_svc["totals"]["review_candidates"] == 0
    assert report_bad_svc["totals"]["assignments"]["missing_owned_unique"] == 0

    # Row-level account SID mismatch in owned numbers
    bad_row_snapshot = _make_sample_snapshot()
    bad_row_snapshot["owned_numbers"][0]["account_sid"] = "ACforeignaccount00000000000000000"
    report_bad_row = summarize_sms_readiness(bad_row_snapshot, as_of=as_of)
    assert report_bad_row["completeness"]["bindings_valid"] is False
    assert report_bad_row["totals"]["review_candidates"] == 0
    assert report_bad_row["totals"]["assignments"]["missing_owned_unique"] == 0


def test_stale_and_future_snapshots_produce_zero_candidates():
    """Stale (>15m) and future snapshots produce 0 review candidates."""
    stale_snap = _make_sample_snapshot(observed_at="2026-10-02T20:00:00Z")
    report_stale = summarize_sms_readiness(stale_snap, as_of="2026-10-02T20:20:00Z")
    assert report_stale["completeness"]["is_stale"] is True
    assert report_stale["totals"]["review_candidates"] == 0

    future_snap = _make_sample_snapshot(observed_at="2026-10-02T20:30:00Z")
    report_future = summarize_sms_readiness(future_snap, as_of="2026-10-02T20:20:00Z")
    assert report_future["completeness"]["is_future"] is True
    assert report_future["totals"]["review_candidates"] == 0


def test_ambiguous_and_malformed_mappings_produce_no_review_candidates():
    """Duplicate assignments, malformed numbers, and unowned inventory produce no review candidates."""
    as_of = "2026-10-02T20:05:00Z"
    snapshot = _make_sample_snapshot(owned_count=2, registered_count=0, contractor_count=4)

    snapshot["contractors"] = [
        {"twilio_number": "+14155550001", "active": True},
        {"twilio_number": "+14155550001", "active": True},
        {"twilio_number": "4155550002", "active": True},
        {"twilio_number": "+14155559999", "active": True},
    ]

    report = summarize_sms_readiness(snapshot, as_of=as_of)
    totals = report["totals"]["assignments"]
    assert totals["ambiguous_assignments"] == 2
    assert totals["malformed_assignments"] == 1
    assert totals["unowned_assignments"] == 1
    assert totals["missing_owned_unique"] == 0
    assert report["totals"]["review_candidates"] == 0


def test_pagination_cap_and_source_error_fails_closed():
    """Cap overflow or source read error marks source incomplete with 0 candidates."""
    as_of = "2026-10-02T20:05:00Z"
    snapshot = _make_sample_snapshot()
    snapshot["sources"]["twilio_incoming"]["complete"] = False
    snapshot["sources"]["twilio_incoming"]["error"] = "cap_overflow"

    report = summarize_sms_readiness(snapshot, as_of=as_of)
    assert report["completeness"]["is_complete"] is False
    assert report["totals"]["review_candidates"] == 0
    assert report["totals"]["assignments"]["missing_owned_unique"] == 0
    assert report["totals"]["pool_membership"]["unknown"] == 29


def test_collector_asserts_projection_and_read_only_fake_clients():
    """Fake clients assert exact Firestore projection and read-only SDK calls."""
    doc_data = {
        "twilio_number": "+14155550001",
        "active": True,
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1790000000,
        "trial_start": 1780000000,
        "last_inbound_call_at": 1790000000,
        "forwarding_last_seen_at": 1790000000,
        "owner_sms_enabled": True,
        "owner_sms_opted_out": False,
        "deletion_requested_at": None,
        "deactivated_at": None,
        "deleted_app_detected_at": None,
        "secret_token_forbidden": "CANARY_TOKEN_1",
    }

    fake_fs = FakeFirestoreClient(project="test-project", docs=[doc_data])

    fake_service = FakeTwilioService(
        sid="MGtestservice0000000000000000000000",
        account_sid="ACtestaccount0000000000000000000000",
        phone_numbers=[{
            "sid": "PN0001",
            "phone_number": "+14155550001",
            "account_sid": "ACtestaccount0000000000000000000000",
            "service_sid": "MGtestservice0000000000000000000000",
            "capabilities": {"sms": True, "arbitrary_nested": "forbidden"},
        }],
    )

    fake_tw = FakeTwilioClient(
        account_sid="ACtestaccount0000000000000000000000",
        services={"MGtestservice0000000000000000000000": fake_service},
        incoming_numbers=[{
            "sid": "PN0001",
            "phone_number": "+14155550001",
            "account_sid": "ACtestaccount0000000000000000000000",
            "capabilities": {"sms": True, "voice": True, "custom_junk": 123},
        }],
    )

    snapshot = collect_snapshot(
        fake_fs,
        fake_tw,
        expected_project="test-project",
        expected_account_sid="ACtestaccount0000000000000000000000",
        messaging_service_sid="MGtestservice0000000000000000000000",
        observed_at="2026-10-02T20:00:00Z",
    )

    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["sources"]["firestore"]["complete"] is True
    assert snapshot["sources"]["twilio_incoming"]["complete"] is True
    assert snapshot["sources"]["twilio_service"]["complete"] is True
    assert len(fake_fs.forbidden_calls) == 0
    assert len(fake_tw.forbidden_calls) == 0

    assert "secret_token_forbidden" not in snapshot["contractors"][0]

    # Capabilities bounded strictly to sms/voice/mms booleans
    assert snapshot["owned_numbers"][0]["capabilities"] == {"sms": True, "voice": True}
    assert snapshot["service_numbers"][0]["capabilities"] == {"sms": True}


def test_collector_max_records_validation():
    """Validate max_records is strict integer not bool in range 1..5000 before any client read."""
    fake_fs = FakeFirestoreClient(project="test-project", docs=[])
    fake_tw = FakeTwilioClient(account_sid="ACtestaccount0000000000000000000000", services={}, incoming_numbers=[])

    with pytest.raises(ValueError, match="max_records"):
        collect_snapshot(
            fake_fs, fake_tw,
            expected_project="test-project",
            expected_account_sid="ACtestaccount0000000000000000000000",
            messaging_service_sid="MGtestservice0000000000000000000000",
            observed_at="2026-10-02T20:00:00Z",
            max_records=True,  # bool must be rejected
        )

    with pytest.raises(ValueError, match="max_records"):
        collect_snapshot(
            fake_fs, fake_tw,
            expected_project="test-project",
            expected_account_sid="ACtestaccount0000000000000000000000",
            messaging_service_sid="MGtestservice0000000000000000000000",
            observed_at="2026-10-02T20:00:00Z",
            max_records=0,  # below 1
        )

    with pytest.raises(ValueError, match="max_records"):
        collect_snapshot(
            fake_fs, fake_tw,
            expected_project="test-project",
            expected_account_sid="ACtestaccount0000000000000000000000",
            messaging_service_sid="MGtestservice0000000000000000000000",
            observed_at="2026-10-02T20:00:00Z",
            max_records=5001,  # above 5000
        )


def test_collector_timeout_inability_fails_source_closed():
    """Inability to honor 15 sec timeout fails firestore source closed."""
    fake_fs = FakeFirestoreClient(project="test-project", docs=[], reject_timeout=True)
    fake_service = FakeTwilioService(
        sid="MGtestservice0000000000000000000000",
        account_sid="ACtestaccount0000000000000000000000",
        phone_numbers=[],
    )
    fake_tw = FakeTwilioClient(
        account_sid="ACtestaccount0000000000000000000000",
        services={"MGtestservice0000000000000000000000": fake_service},
        incoming_numbers=[],
    )

    snapshot = collect_snapshot(
        fake_fs,
        fake_tw,
        expected_project="test-project",
        expected_account_sid="ACtestaccount0000000000000000000000",
        messaging_service_sid="MGtestservice0000000000000000000000",
        observed_at="2026-10-02T20:00:00Z",
    )

    assert snapshot["sources"]["firestore"]["complete"] is False
    assert snapshot["sources"]["firestore"]["error"] == "read_error"
    assert snapshot["contractors"] == []


def test_collector_islice_bounds_stream_consumption():
    """Firestore iterator is consumed boundedly with islice."""
    def infinite_docs():
        idx = 0
        while True:
            idx += 1
            yield {"twilio_number": f"+1415555{idx:04d}", "active": True}

    fake_fs = FakeFirestoreClient(project="test-project", docs=infinite_docs(), max_records=10)
    fake_service = FakeTwilioService(
        sid="MGtestservice0000000000000000000000",
        account_sid="ACtestaccount0000000000000000000000",
        phone_numbers=[],
        max_records=10,
    )
    fake_tw = FakeTwilioClient(
        account_sid="ACtestaccount0000000000000000000000",
        services={"MGtestservice0000000000000000000000": fake_service},
        incoming_numbers=[],
        max_records=10,
    )

    snapshot = collect_snapshot(
        fake_fs,
        fake_tw,
        expected_project="test-project",
        expected_account_sid="ACtestaccount0000000000000000000000",
        messaging_service_sid="MGtestservice0000000000000000000000",
        observed_at="2026-10-02T20:00:00Z",
        max_records=10,
    )

    assert snapshot["sources"]["firestore"]["complete"] is False
    assert snapshot["sources"]["firestore"]["error"] == "cap_overflow"
    assert len(snapshot["contractors"]) == 10


def test_collector_malformed_identity_marks_source_incomplete():
    """Collector rows with missing/malformed identity or binding set incomplete."""
    fake_fs = FakeFirestoreClient(project="test-project", docs=[])

    # Twilio incoming numbers with missing SID and invalid non-E164 number
    fake_service = FakeTwilioService(
        sid="MGtestservice0000000000000000000000",
        account_sid="ACtestaccount0000000000000000000000",
        phone_numbers=[{
            "sid": "PN0001",
            "phone_number": "not_e164",  # Malformed
            "account_sid": "ACtestaccount0000000000000000000000",
            "service_sid": "MGtestservice0000000000000000000000",
            "capabilities": {"sms": True},
        }],
    )
    fake_tw = FakeTwilioClient(
        account_sid="ACtestaccount0000000000000000000000",
        services={"MGtestservice0000000000000000000000": fake_service},
        incoming_numbers=[{
            "sid": "",  # Empty SID
            "phone_number": "+14155550001",
            "account_sid": "ACtestaccount0000000000000000000000",
            "capabilities": {"sms": True},
        }],
    )

    snapshot = collect_snapshot(
        fake_fs,
        fake_tw,
        expected_project="test-project",
        expected_account_sid="ACtestaccount0000000000000000000000",
        messaging_service_sid="MGtestservice0000000000000000000000",
        observed_at="2026-10-02T20:00:00Z",
    )

    assert snapshot["sources"]["twilio_incoming"]["complete"] is False
    assert snapshot["sources"]["twilio_service"]["complete"] is False


def test_sources_metadata_normalization_and_sentinel_protection():
    """Report normalizes source metadata to fixed safe enums and never echoes sentinels."""
    canary_error_token = "CANARY_ERROR_TRACE_SECRET_12345"
    canary_records_token = "CANARY_RECORDS_READ_STRING_9999"
    canary_extra_key = "forbidden_source_metadata_key"

    snapshot = _make_sample_snapshot()
    # Inject privacy sentinels and extra keys into sources
    snapshot["sources"]["firestore"][canary_extra_key] = canary_error_token
    snapshot["sources"]["twilio_incoming"]["error"] = f"Fatal trace: {canary_error_token}"
    snapshot["sources"]["twilio_service"]["records_read"] = canary_records_token
    snapshot["sources"]["unexpected_fourth_source"] = {
        "complete": True,
        "records_read": 10,
        "token": canary_error_token,
    }

    report = summarize_sms_readiness(snapshot, as_of="2026-10-02T20:05:00Z")
    report_json = json.dumps(report)

    # Sentinels must never appear anywhere in the output
    assert canary_error_token not in report_json
    assert canary_records_token not in report_json
    assert canary_extra_key not in report_json
    assert "unexpected_fourth_source" not in report_json

    # Report completeness sources must contain only fixed keys and safe enums
    sources_summary = report["completeness"]["sources"]
    assert set(sources_summary.keys()) == {"firestore", "twilio_incoming", "twilio_service"}
    assert sources_summary["firestore"]["complete"] is False  # Extra key invalidated it
    assert sources_summary["twilio_incoming"]["error"] == "unknown_error"
    assert sources_summary["twilio_service"]["records_read"] == 0
    assert report["completeness"]["is_complete"] is False
    assert report["totals"]["review_candidates"] == 0


def test_exact_schema_version_validation():
    """Snapshot with missing or invalid schema_version is rejected."""
    as_of = "2026-10-02T20:05:00Z"
    snapshot = _make_sample_snapshot()

    snapshot["schema_version"] = "2.0"
    with pytest.raises(ValueError, match="Invalid schema_version"):
        summarize_sms_readiness(snapshot, as_of=as_of)

    snapshot["schema_version"] = None
    with pytest.raises(ValueError, match="Invalid schema_version"):
        summarize_sms_readiness(snapshot, as_of=as_of)

    snapshot["schema_version"] = 1.0
    with pytest.raises(ValueError, match="Invalid schema_version"):
        summarize_sms_readiness(snapshot, as_of=as_of)


def test_oversized_input_list_rejected_before_iteration():
    """Input lists >5000 items are rejected before iteration."""
    as_of = "2026-10-02T20:05:00Z"

    # 5001 owned numbers
    snap_owned = _make_sample_snapshot(owned_count=1, registered_count=1, contractor_count=1)
    snap_owned["owned_numbers"] = [{"sid": f"PN{i}", "phone_number": "+14155550001", "account_sid": "ACtest"} for i in range(5001)]
    with pytest.raises(ValueError, match="Input list exceeds maximum allowed size"):
        summarize_sms_readiness(snap_owned, as_of=as_of)

    # 5001 service numbers
    snap_svc = _make_sample_snapshot(owned_count=1, registered_count=1, contractor_count=1)
    snap_svc["service_numbers"] = [{"sid": f"PN{i}", "phone_number": "+14155550001", "account_sid": "ACtest", "service_sid": "MGtest"} for i in range(5001)]
    with pytest.raises(ValueError, match="Input list exceeds maximum allowed size"):
        summarize_sms_readiness(snap_svc, as_of=as_of)

    # 5001 contractors
    snap_c = _make_sample_snapshot(owned_count=1, registered_count=1, contractor_count=1)
    snap_c["contractors"] = [{"twilio_number": "+14155550001"} for _ in range(5001)]
    with pytest.raises(ValueError, match="Input list exceeds maximum allowed size"):
        summarize_sms_readiness(snap_c, as_of=as_of)


def test_malformed_provider_or_contractor_rows_fail_closed_or_reject():
    """Non-dict rows reject; non-canonical provider numbers mark incomplete/fail closed."""
    as_of = "2026-10-02T20:05:00Z"

    snap1 = _make_sample_snapshot()
    snap1["owned_numbers"].append("not_a_dict")
    with pytest.raises(ValueError, match="Invalid row structure"):
        summarize_sms_readiness(snap1, as_of=as_of)

    snap2 = _make_sample_snapshot()
    snap2["service_numbers"].append(12345)
    with pytest.raises(ValueError, match="Invalid row structure"):
        summarize_sms_readiness(snap2, as_of=as_of)

    snap3 = _make_sample_snapshot()
    snap3["contractors"].append(None)
    with pytest.raises(ValueError, match="Invalid row structure"):
        summarize_sms_readiness(snap3, as_of=as_of)

    # Non-canonical phone in owned number
    snap4 = _make_sample_snapshot(owned_count=1, registered_count=1, contractor_count=1)
    snap4["owned_numbers"][0]["phone_number"] = "4155550001"  # Missing +
    report4 = summarize_sms_readiness(snap4, as_of=as_of)
    assert report4["completeness"]["is_complete"] is False
    assert report4["totals"]["review_candidates"] == 0


def test_pool_anomalies_and_sid_phone_conflicts():
    """Detect pool entries absent from inventory, duplicate SIDs, and SID/phone conflicts."""
    as_of = "2026-10-02T20:05:00Z"
    snapshot = _make_sample_snapshot(owned_count=3, registered_count=0, contractor_count=3)

    # Owned:
    # PN0001 -> +14155550001
    # PN0002 -> +14155550002
    # PN0003 -> +14155550003
    account_sid = snapshot["expected_account_sid"]
    service_sid = snapshot["messaging_service_sid"]

    snapshot["service_numbers"] = [
        # Normal match
        {
            "sid": "PN0001",
            "phone_number": "+14155550001",
            "account_sid": account_sid,
            "service_sid": service_sid,
            "capabilities": {"sms": True},
        },
        # SID absent from owned inventory
        {
            "sid": "PN9999",
            "phone_number": "+14155559999",
            "account_sid": account_sid,
            "service_sid": service_sid,
            "capabilities": {"sms": True},
        },
        # SID-to-phone conflict: PN0002 has phone +14155558888 instead of +14155550002
        {
            "sid": "PN0002",
            "phone_number": "+14155558888",
            "account_sid": account_sid,
            "service_sid": service_sid,
            "capabilities": {"sms": True},
        },
        # Duplicate pool SID: second PN0001
        {
            "sid": "PN0001",
            "phone_number": "+14155550001",
            "account_sid": account_sid,
            "service_sid": service_sid,
            "capabilities": {"sms": True},
        },
    ]

    snapshot["sources"]["twilio_service"]["records_read"] = len(snapshot["service_numbers"])

    report = summarize_sms_readiness(snapshot, as_of=as_of)
    anomalies = report["totals"]["anomalies"]

    assert anomalies["pool_sids_absent_from_inventory"] == 1
    assert anomalies["duplicate_pool_sids"] == 1
    assert anomalies["pool_sid_phone_conflicts"] >= 1

    # Conflicting / duplicate / orphan entries make reconciliation fail closed
    assert report["completeness"]["is_complete"] is False
    assert report["completeness"]["sources"]["twilio_service"]["complete"] is False
    assert report["totals"]["review_candidates"] == 0
    assert report["totals"]["pool_membership"]["present"] == 0
    assert report["totals"]["pool_membership"]["unknown"] == 3


def test_missing_owned_unique_unknown_when_incomplete_or_binding_invalid():
    """missing_owned_unique must not assert confirmed missing when incomplete or binding invalid."""
    as_of = "2026-10-02T20:05:00Z"
    # Normally 10 missing
    snapshot = _make_sample_snapshot(owned_count=29, registered_count=19, contractor_count=29)

    # Incomplete firestore source
    snapshot["sources"]["firestore"]["complete"] = False
    snapshot["sources"]["firestore"]["error"] = "read_error"

    report = summarize_sms_readiness(snapshot, as_of=as_of)
    assert report["totals"]["assignments"]["missing_owned_unique"] == 0
    assert report["totals"]["review_candidates"] == 0

    # Invalid binding
    clean_snap = _make_sample_snapshot(owned_count=29, registered_count=19, contractor_count=29)
    report_bad_bind = summarize_sms_readiness(clean_snap, as_of=as_of, expected_project="mismatched")
    assert report_bad_bind["totals"]["assignments"]["missing_owned_unique"] == 0
    assert report_bad_bind["totals"]["review_candidates"] == 0


def test_limitations_contain_caller_supplied_and_no_authorization_claims():
    """Limitations state snapshot provenance is caller supplied and review candidates do not prove consent/repair."""
    snapshot = _make_sample_snapshot()
    report = summarize_sms_readiness(snapshot, as_of="2026-10-02T20:05:00Z")
    limits = report["limitations"]

    assert any("caller supplied" in l for l in limits)
    assert any("consent or repair authorization" in l for l in limits)
    assert any("Stale, future or incomplete snapshots yield zero review candidates" in l for l in limits)


def test_privacy_sentinels_never_leak_in_outputs_or_errors(capsys):
    """Privacy sentinels in records, IDs, extra fields and exception messages never appear in output."""
    canary_token = "CANARY_SECRET_TOKEN_999888777"
    canary_email = "canary_user_email@supersecret.com"
    canary_name = "Canary Secret User"
    canary_extra_key = "forbidden_canary_key"

    snapshot = _make_sample_snapshot()
    snapshot["contractors"][0]["customer_email"] = canary_email
    snapshot["contractors"][0]["customer_name"] = canary_name
    snapshot["contractors"][0][canary_extra_key] = canary_token

    report = summarize_sms_readiness(snapshot, as_of="2026-10-02T20:05:00Z")
    report_json = json.dumps(report)

    assert canary_token not in report_json
    assert canary_email not in report_json
    assert canary_name not in report_json
    assert canary_extra_key not in report_json

    # CLI test with canary in unexpected top-level field
    bad_snapshot = dict(snapshot)
    bad_snapshot[canary_extra_key] = canary_token
    bad_input_str = json.dumps(bad_snapshot)

    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(bad_input_str)
        ret = main([
            "--expected-project", "test-project",
            "--expected-account-sid", "ACtestaccount0000000000000000000000",
            "--messaging-service-sid", "MGtestservice0000000000000000000000",
            "--as-of", "2026-10-02T20:05:00Z",
        ])
        captured = capsys.readouterr()
        assert ret == 1
        assert canary_token not in captured.out
        assert canary_token not in captured.err
        assert canary_extra_key not in captured.out
        assert canary_extra_key not in captured.err
    finally:
        sys.stdin = old_stdin


def test_offline_cli_valid_execution(capsys, tmp_path):
    """Offline CLI consumes file or stdin and outputs parseable aggregate report."""
    snapshot = _make_sample_snapshot(owned_count=29, registered_count=19, contractor_count=29)
    snap_file = tmp_path / "snapshot.json"
    snap_file.write_text(json.dumps(snapshot), encoding="utf-8")

    ret = main([
        "--snapshot", str(snap_file),
        "--expected-project", "test-project",
        "--expected-account-sid", "ACtestaccount0000000000000000000000",
        "--messaging-service-sid", "MGtestservice0000000000000000000000",
        "--as-of", "2026-10-02T20:05:00Z",
    ])
    captured = capsys.readouterr()
    assert ret == 0
    parsed = json.loads(captured.out)
    assert parsed["schema_version"] == SCHEMA_VERSION
    assert parsed["totals"]["review_candidates"] == 10
    assert captured.err == ""


def test_records_read_count_mismatch_and_bounds_validation():
    """Count mismatch between records_read and source list length fails closed; invalid counts reset to 0."""
    as_of = "2026-10-02T20:05:00Z"
    snapshot = _make_sample_snapshot(owned_count=10, registered_count=10, contractor_count=10)

    # 1. Count mismatch in firestore
    snapshot["sources"]["firestore"]["records_read"] = 9  # list has 10
    report = summarize_sms_readiness(snapshot, as_of=as_of)
    assert report["completeness"]["sources"]["firestore"]["complete"] is False
    assert report["completeness"]["is_complete"] is False
    assert report["totals"]["review_candidates"] == 0

    # 2. Count mismatch in twilio_incoming
    snapshot2 = _make_sample_snapshot(owned_count=10, registered_count=10, contractor_count=10)
    snapshot2["sources"]["twilio_incoming"]["records_read"] = 11  # list has 10
    report2 = summarize_sms_readiness(snapshot2, as_of=as_of)
    assert report2["completeness"]["sources"]["twilio_incoming"]["complete"] is False
    assert report2["completeness"]["is_complete"] is False

    # 3. Invalid records_read (bool, negative, >5000, string) resets to 0 and does not echo
    for bad_rr in (True, False, -1, 5001, "10", "CANARY_SECRET"):
        snapshot_bad = _make_sample_snapshot(owned_count=5, registered_count=5, contractor_count=5)
        snapshot_bad["sources"]["twilio_service"]["records_read"] = bad_rr
        rep = summarize_sms_readiness(snapshot_bad, as_of=as_of)
        assert rep["completeness"]["sources"]["twilio_service"]["records_read"] == 0
        assert rep["completeness"]["sources"]["twilio_service"]["complete"] is False
        assert rep["completeness"]["is_complete"] is False


def test_orphan_pool_sid_fails_closed_and_retains_aggregate_count():
    """Pool SID absent from owned inventory fails closed and retains aggregate orphan anomaly count."""
    as_of = "2026-10-02T20:05:00Z"
    snapshot = _make_sample_snapshot(owned_count=5, registered_count=5, contractor_count=5)

    # Add an orphan pool number (SID not in owned inventory)
    orphan_entry = {
        "sid": "PN_ORPHAN_9999",
        "phone_number": "+14155559999",
        "account_sid": snapshot["expected_account_sid"],
        "service_sid": snapshot["messaging_service_sid"],
        "capabilities": {"sms": True},
    }
    snapshot["service_numbers"].append(orphan_entry)
    snapshot["sources"]["twilio_service"]["records_read"] = len(snapshot["service_numbers"])

    report = summarize_sms_readiness(snapshot, as_of=as_of)
    assert report["totals"]["anomalies"]["pool_sids_absent_from_inventory"] == 1
    assert report["completeness"]["is_complete"] is False
    assert report["completeness"]["sources"]["twilio_service"]["complete"] is False
    assert report["totals"]["review_candidates"] == 0


def test_capabilities_strict_bool_extraction_no_string_coercion():
    """Capabilities retain only sms, voice, mms strictly boolean; strings and missing are not coerced."""
    from scripts.sms_readiness_audit import _extract_capabilities

    # String "false" must NOT be bool-coerced to True
    caps = _extract_capabilities({
        "sms": True,
        "voice": "false",
        "mms": "true",
        "extra_field": True,
    })
    assert caps == {"sms": True}
    assert "voice" not in caps
    assert "mms" not in caps
    assert "extra_field" not in caps

    # None input produces empty dict (no invented facts)
    assert _extract_capabilities(None) == {}
    assert _extract_capabilities({}) == {}

    # Strict booleans preserved
    assert _extract_capabilities({"sms": False, "voice": True, "mms": False}) == {
        "sms": False,
        "voice": True,
        "mms": False,
    }

    # Injected fake twilio client with string capabilities
    fake_fs = FakeFirestoreClient(project="test-project", docs=[])
    fake_service = FakeTwilioService(
        sid="MGtestservice0000000000000000000000",
        account_sid="ACtestaccount0000000000000000000000",
        phone_numbers=[{
            "sid": "PN0001",
            "phone_number": "+14155550001",
            "account_sid": "ACtestaccount0000000000000000000000",
            "service_sid": "MGtestservice0000000000000000000000",
            "capabilities": {"sms": True, "voice": "false", "mms": 0},
        }],
    )
    fake_tw = FakeTwilioClient(
        account_sid="ACtestaccount0000000000000000000000",
        services={"MGtestservice0000000000000000000000": fake_service},
        incoming_numbers=[],
    )

    snapshot = collect_snapshot(
        fake_fs,
        fake_tw,
        expected_project="test-project",
        expected_account_sid="ACtestaccount0000000000000000000000",
        messaging_service_sid="MGtestservice0000000000000000000000",
        observed_at="2026-10-02T20:00:00Z",
    )
    assert snapshot["service_numbers"][0]["capabilities"] == {"sms": True}


@pytest.mark.parametrize("target_source,padded_number", [
    ("twilio_incoming", "  +14155550001  "),
    ("twilio_service", "\t+14155550001\n"),
])
def test_padded_phone_number_collector_to_reducer_regression(target_source: str, padded_number: str):
    """Padded owned or pool phone numbers fail source completeness in collector and reducer produces zero review candidates."""
    project = "test-project"
    account_sid = "ACtestaccount0000000000000000000000"
    service_sid = "MGtestservice0000000000000000000000"
    observed_at = "2026-10-02T20:00:00Z"
    as_of = "2026-10-02T20:05:00Z"

    contractor_data = {
        "twilio_number": "+14155550001",
        "active": True,
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 1790000000,
        "trial_start": 1780000000,
        "last_inbound_call_at": 1790000000,
        "forwarding_last_seen_at": 1790000000,
        "owner_sms_enabled": True,
        "owner_sms_opted_out": False,
    }
    fake_fs = FakeFirestoreClient(project=project, docs=[contractor_data])

    owned_phone = padded_number if target_source == "twilio_incoming" else "+14155550001"
    pool_phone = padded_number if target_source == "twilio_service" else "+14155550001"

    fake_service = FakeTwilioService(
        sid=service_sid,
        account_sid=account_sid,
        phone_numbers=[{
            "sid": "PN0001",
            "phone_number": pool_phone,
            "account_sid": account_sid,
            "service_sid": service_sid,
            "capabilities": {"sms": True, "voice": True, "mms": False},
        }],
    )
    fake_tw = FakeTwilioClient(
        account_sid=account_sid,
        services={service_sid: fake_service},
        incoming_numbers=[{
            "sid": "PN0001",
            "phone_number": owned_phone,
            "account_sid": account_sid,
            "capabilities": {"sms": True, "voice": True, "mms": False},
        }],
    )

    # 1. Exercise collector
    snapshot = collect_snapshot(
        fake_fs,
        fake_tw,
        expected_project=project,
        expected_account_sid=account_sid,
        messaging_service_sid=service_sid,
        observed_at=observed_at,
    )

    # Assert affected source marked incomplete at collection time with read_error
    assert snapshot["sources"][target_source]["complete"] is False
    assert snapshot["sources"][target_source]["error"] == "read_error"
    # Fixture bindings and source counts are otherwise valid
    assert snapshot["sources"]["firestore"]["complete"] is True
    assert snapshot["sources"]["firestore"]["records_read"] == 1
    assert snapshot["sources"]["twilio_incoming"]["records_read"] == 1
    assert snapshot["sources"]["twilio_service"]["records_read"] == 1

    # 2. Exercise reducer
    report = summarize_sms_readiness(
        snapshot,
        as_of=as_of,
        expected_project=project,
        expected_account_sid=account_sid,
        messaging_service_sid=service_sid,
    )

    # Assert affected source incomplete, overall incomplete, and zero review candidates
    assert report["completeness"]["sources"][target_source]["complete"] is False
    assert report["completeness"]["is_complete"] is False
    assert report["totals"]["review_candidates"] == 0

    # Assert original sensitive number never output
    report_json = json.dumps(report)
    assert padded_number not in report_json
    assert "+14155550001" not in report_json
