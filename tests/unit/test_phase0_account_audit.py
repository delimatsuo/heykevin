import base64
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest


def _load_audit_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "phase0_account_audit.py"
    spec = importlib.util.spec_from_file_location("phase0_account_audit", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase0_account_audit"] = module
    spec.loader.exec_module(module)
    return module


class _HostileKeyObj:
    """Key object that allows initial insertion into a test dict but throws if rehashed or compared."""
    def __init__(self):
        self._inserted = False

    def __hash__(self):
        if not self._inserted:
            self._inserted = True
            return 42
        raise AssertionError("Hostile key hash invoked by audit code!")

    def __eq__(self, other):
        raise AssertionError("Hostile key equality invoked by audit code!")

    def __str__(self):
        raise AssertionError("Hostile key str check invoked by audit code!")


class _HostileValObj:
    def __hash__(self):
        raise AssertionError("Hostile hash check invoked on val!")

    def __eq__(self, other):
        raise AssertionError("Hostile equality check invoked on val!")

    def __bool__(self):
        raise AssertionError("Hostile bool check invoked on val!")

    def __str__(self):
        raise AssertionError("Hostile str check invoked on val!")

    def __iter__(self):
        raise AssertionError("Hostile iter check invoked on val!")

    def timestamp(self):
        raise AssertionError("Hostile timestamp method invoked on val!")


class _HostileDatetimeSubclass(datetime):
    def timestamp(self):
        raise AssertionError("Hostile datetime subclass timestamp override invoked!")


class _SpoofGoogleDatetimeMeta(type):
    pass


class _SpoofGoogleDatetime(metaclass=_SpoofGoogleDatetimeMeta):
    __module__ = "google.api_core.datetime_helpers"
    __qualname__ = "DatetimeWithNanoseconds"

    def timestamp(self):
        raise AssertionError("Spoof datetime timestamp method invoked!")


def _make_valid_envelope():
    nonce = base64.b64encode(b"0" * 12).decode("ascii")
    ct = base64.b64encode(b"0" * 32).decode("ascii")
    return {
        "schema_version": 1,
        "key_version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": nonce,
        "ciphertext": ct,
    }


# ---------------------------------------------------------------------------
# Report Structure & Key Constraints
# ---------------------------------------------------------------------------

def test_report_top_level_exact_key_set_and_no_metadata_leakage():
    audit = _load_audit_module()

    class _FakeDocSnap:
        def __init__(self, data):
            self._data = data

        def to_dict(self):
            return self._data

    class _FakeCol:
        def stream(self, **kwargs):
            return []

    class _FakeClient:
        def collection(self, name):
            return _FakeCol()

    report = audit.build_report(_FakeClient(), project="kevin-staging-491315", environment="staging")
    assert set(report.keys()) == {"contractors", "estimates"}
    assert "generated_at" not in report
    assert "environment" not in report
    assert "project" not in report


def test_post_deletion_billing_types_classification():
    """Verify known types match, arbitrary strings map to 'other', missing/empty to 'unknown'."""
    audit = _load_audit_module()

    records = [
        # Known allowlist string
        {"post_deletion_billing": {"last_type": "subscription_renewal"}},
        # Arbitrary safe-looking string maps to "other"
        {"post_deletion_billing": {"last_type": "custom_promo_type_123"}},
        # Missing last_type maps to "unknown"
        {"post_deletion_billing": {}},
        # Empty string maps to "unknown"
        {"post_deletion_billing": {"last_type": ""}},
        # None maps to "unknown"
        {"post_deletion_billing": {"last_type": None}},
    ]

    summary = audit.summarize_contractors(records)
    b_types = summary["post_deletion_billing_types"]
    assert b_types.get("subscription_renewal") == 1
    assert b_types.get("other") == 1
    assert b_types.get("unknown") == 3
    assert "custom_promo_type_123" not in b_types


def test_audit_constructor_and_read_failure_sentinel_nondisclosure(capsys):
    """Constructor and read failure must return 1 with exact fixed error message and no sentinel leakage."""
    audit = _load_audit_module()
    secret_sentinel = "secret_audit_sentinel_999"

    def failing_client_factory(**kwargs):
        raise RuntimeError(f"Firestore connection failed: {secret_sentinel}")

    exit_code = audit.main(
        ["--project", f"project_{secret_sentinel}", "--environment", "staging"],
        client_factory=failing_client_factory,
    )
    assert exit_code == 1

    captured = capsys.readouterr()
    assert captured.err == "Audit failed while reading Firestore.\n"
    assert secret_sentinel not in captured.err
    assert secret_sentinel not in captured.out


# ---------------------------------------------------------------------------
# Connected Status Classification Tests
# ---------------------------------------------------------------------------

def test_connected_status_classification_exact_bool():
    audit = _load_audit_module()

    records = [
        {"jobber_connected": True, "google_calendar_connected": False},
        {"jobber_connected": False, "google_calendar_connected": True},
        {},  # missing
        {"jobber_connected": 1, "google_calendar_connected": "true"},  # other
        {"jobber_connected": _HostileValObj(), "google_calendar_connected": None},  # other
    ]
    summary = audit.summarize_contractors(records)
    assert summary["jobber_connected"] == {"false": 1, "missing": 1, "other": 2, "true": 1}
    assert summary["google_calendar_connected"] == {"false": 1, "missing": 1, "other": 2, "true": 1}


# ---------------------------------------------------------------------------
# Credential Representation Tests
# ---------------------------------------------------------------------------

def test_credential_representation_categories():
    audit = _load_audit_module()

    valid_env = _make_valid_envelope()
    bad_env = dict(valid_env, algorithm="DES")

    records = [
        {},
        {"jobber_access_token": "acc", "jobber_refresh_token": "ref"},
        {"jobber_access_token": valid_env, "jobber_refresh_token": valid_env},
        {"jobber_access_token": "acc"},
        {"jobber_refresh_token": valid_env},
        {"jobber_access_token": "acc", "jobber_refresh_token": valid_env},
        {"jobber_access_token": "", "jobber_refresh_token": "ref"},
        {"jobber_access_token": bad_env, "jobber_refresh_token": "ref"},
        {"jobber_access_token": None, "jobber_refresh_token": "ref"},
        {"jobber_access_token": _HostileValObj(), "jobber_refresh_token": "ref"},
    ]

    summary = audit.summarize_contractors(records)
    creds = summary["jobber_credentials"]
    assert creds["absent"] == 1
    assert creds["plaintext_pair"] == 1
    assert creds["envelope_pair"] == 1
    assert creds["partial"] == 2
    assert creds["mixed"] == 1
    assert creds["malformed"] == 4


# ---------------------------------------------------------------------------
# Hostile Input Hardening & Zero-Leak Tests
# ---------------------------------------------------------------------------

def test_hostile_values_across_all_fields_do_not_throw_or_leak():
    audit = _load_audit_module()
    hostile_key = _HostileKeyObj()
    hostile_val = _HostileValObj()

    gated_dict = {hostile_key: hostile_val}
    hostile_key._inserted = True

    records = [
        {
            "contractor_id": "c-secret-12345",
            "owner_phone": "+15005550006",
            "sms_compliance_status": hostile_val,
            "integration_write_status": hostile_val,
            "subscription_status": hostile_val,
            "subscription_tier": hostile_val,
            "auto_reply_sms": hostile_val,
            "jobber_connected": hostile_val,
            "google_calendar_connected": hostile_val,
            "gated_actions": gated_dict,
            "automation_approvals": {"valid_key": hostile_val},
            "post_deletion_billing": {
                "last_type": hostile_val,
                "rebound_contractor_id": hostile_val,
                "charges": hostile_val,
            },
            "twilio_number": hostile_val,
        },
        {
            "contractor_id": "c-clean",
            "sms_compliance_status": "approved",
            "integration_write_status": "approved",
            "subscription_status": "active",
            "subscription_tier": "business",
            "auto_reply_sms": True,
            "jobber_connected": True,
            "google_calendar_connected": False,
            "jobber_access_token": "secret-token-value",
            "jobber_refresh_token": "secret-refresh-value",
            "post_deletion_billing": {
                "last_type": "subscription_renewal",
                "rebound_contractor_id": "c-rebound",
                "charges": 1,
            },
        },
    ]

    summary = audit.summarize_contractors(records)
    assert summary["total_contractors"] == 2
    assert summary["active_or_trial_business_accounts"] == 1
    assert summary["post_deletion_rebound_accounts"] == 1

    encoded = json.dumps(summary)
    assert "secret-token-value" not in encoded
    assert "secret-refresh-value" not in encoded
    assert "+15005550006" not in encoded
    assert "c-secret-12345" not in encoded
    assert "c-clean" not in encoded
    assert "c-rebound" not in encoded


def test_timestamp_handling_safely_rejects_subclasses_and_huge_ints():
    audit = _load_audit_module()

    assert audit._timestamp(1700000000) == 1700000000.0
    assert audit._timestamp(1700000000.5) == 1700000000.5

    dt = datetime(2026, 1, 1)
    assert audit._timestamp(dt) == dt.timestamp()

    hostile_dt = _HostileDatetimeSubclass(2026, 1, 1)
    assert audit._timestamp(hostile_dt) is None

    spoof_dt = _SpoofGoogleDatetime()
    assert audit._timestamp(spoof_dt) is None

    huge_int = 10**1000
    assert audit._timestamp(huge_int) is None

    assert audit._timestamp(True) is None
    assert audit._timestamp(False) is None
    assert audit._timestamp(float("nan")) is None
    assert audit._timestamp(float("inf")) is None
    assert audit._timestamp("2026-01-01") is None
    assert audit._timestamp(_HostileValObj()) is None
    assert audit._timestamp(None) is None


def test_estimate_summary_with_hostile_and_clean_records():
    audit = _load_audit_module()
    now = 1_700_000_000.0
    hostile = _HostileValObj()

    estimates = [
        {"status": "complete", "created_at": now - 3600, "estimate_id": "est-secret-1"},
        {"status": hostile, "created_at": hostile, "estimate_id": "est-secret-2"},
        {},
    ]
    summary = audit.summarize_estimates(estimates, now=now)
    assert summary["total_estimates"] == 3
    assert summary["status"] == {"complete": 1, "missing": 1, "other": 1}
    assert summary["age_buckets"] == {"0_7_days": 1, "missing_created_at": 2}

    for bad_now in (
        10**1000,
        True,
        False,
        float("nan"),
        float("inf"),
        hostile,
        _HostileDatetimeSubclass(2026, 1, 1),
    ):
        summary_bad_now = audit.summarize_estimates(estimates, now=bad_now)
        assert summary_bad_now["total_estimates"] == 3
        assert summary_bad_now["age_buckets"] == {"missing_created_at": 3}

    encoded = json.dumps(summary)
    assert "est-secret" not in encoded


def test_envelope_canonical_validation_and_malformed_edges():
    audit = _load_audit_module()
    valid_env = _make_valid_envelope()

    assert audit._is_valid_envelope_structure(valid_env) is True

    assert audit._is_valid_envelope_structure(dict(valid_env, schema_version=2)) is False
    assert audit._is_valid_envelope_structure(dict(valid_env, schema_version=True)) is False
    assert audit._is_valid_envelope_structure(dict(valid_env, schema_version="1")) is False

    assert audit._is_valid_envelope_structure(dict(valid_env, key_version=0)) is False
    assert audit._is_valid_envelope_structure(dict(valid_env, key_version=-1)) is False
    assert audit._is_valid_envelope_structure(dict(valid_env, key_version=True)) is False

    assert audit._is_valid_envelope_structure(dict(valid_env, algorithm="AES-128-GCM")) is False

    short_nonce = base64.b64encode(b"0" * 8).decode("ascii")
    assert audit._is_valid_envelope_structure(dict(valid_env, nonce=short_nonce)) is False
    assert audit._is_valid_envelope_structure(dict(valid_env, nonce="invalid!!!")) is False

    short_ct = base64.b64encode(b"0" * 16).decode("ascii")
    assert audit._is_valid_envelope_structure(dict(valid_env, ciphertext=short_ct)) is False

    extra_env = dict(valid_env, extra_field="bad")
    assert audit._is_valid_envelope_structure(extra_env) is False
    missing_key_env = {k: v for k, v in valid_env.items() if k != "nonce"}
    assert audit._is_valid_envelope_structure(missing_key_env) is False


def test_post_deletion_billing_boolean_charges_not_counted():
    audit = _load_audit_module()

    records = [
        {
            "post_deletion_billing": {
                "last_type": "subscription_renewal",
                "charges": True,
            }
        },
        {
            "post_deletion_billing": {
                "last_type": "subscription_renewal",
                "charges": 2,
            }
        },
    ]
    summary = audit.summarize_contractors(records)
    assert summary["post_deletion_charged_accounts"] == 1


def test_stream_collection_safely_skips_throwing_documents():
    audit = _load_audit_module()

    class _DocSnap:
        def __init__(self, data, should_raise=False):
            self._data = data
            self._should_raise = should_raise

        def to_dict(self):
            if self._should_raise:
                raise RuntimeError("Corrupted document snapshot with secret_key_123")
            return self._data

    class _FakeCol:
        def stream(self, **kwargs):
            return [
                _DocSnap({"contractor_id": "c1"}),
                _DocSnap(None, should_raise=True),
                _DocSnap(None),
                _DocSnap("not a dict"),
                _DocSnap({"contractor_id": "c2"}),
            ]

    class _FakeClient:
        def collection(self, name):
            return _FakeCol()

    docs = list(audit._stream_collection(_FakeClient(), "contractors"))
    assert len(docs) == 2
    assert docs[0] == {"contractor_id": "c1"}
    assert docs[1] == {"contractor_id": "c2"}


def test_active_action_keys_equals_gate_policies_registry():
    audit = _load_audit_module()
    from app.services.gated_actions import GATE_POLICIES

    expected = {action.value for action, policy in GATE_POLICIES.items() if policy.requires_flag is True}
    assert audit.ACTIVE_ACTION_KEYS == expected
    assert len(audit.ACTIVE_ACTION_KEYS) > 0


def test_18qc_account_audit_exact_nested_keysets(capsys):
    """Prove report top-level key set is exactly {'contractors', 'estimates'} and nested keysets are exact."""
    audit = _load_audit_module()
    report = audit.summarize_contractors([])
    assert set(report.keys()) == {
        "total_contractors",
        "active_or_trial_business_accounts",
        "gated_action_keys",
        "sms_compliance_status",
        "integration_write_status",
        "automation_approval_keys",
        "auto_reply_sms",
        "jobber_connected",
        "google_calendar_connected",
        "jobber_credentials",
        "google_calendar_credentials",
        "twilio_number_assigned",
        "subscription_status",
        "subscription_tier",
        "post_deletion_billing_types",
        "post_deletion_charged_accounts",
        "post_deletion_rebound_accounts",
    }

    est_report = audit.summarize_estimates([])
    assert set(est_report.keys()) == {
        "total_estimates",
        "status",
        "age_buckets",
    }

    # Test main failures (constructor and stream failure) return 1 and print exact fixed stderr
    for fail_factory in (
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Constructor failed secret_123")),
        lambda **kwargs: type("Client", (), {"collection": lambda self, name: (_ for _ in ()).throw(RuntimeError("Stream failed secret_456"))})(),
    ):
        exit_code = audit.main(["--project", "test-proj", "--environment", "staging"], client_factory=fail_factory)
        assert exit_code == 1
        captured = capsys.readouterr()
        assert captured.err == "Audit failed while reading Firestore.\n"
        assert "secret_123" not in captured.err
        assert "secret_456" not in captured.err


def test_18qe_account_audit_exact_emitted_bucket_keysets():
    """Assert exact set equality for every emitted inner bucket in account audit report."""
    audit = _load_audit_module()
    import base64
    valid_env = {
        "schema_version": 1,
        "key_version": 1,
        "algorithm": "AES-256-GCM",
        "nonce": base64.b64encode(b"0" * 12).decode("ascii"),
        "ciphertext": base64.b64encode(b"0" * 32).decode("ascii"),
    }
    sample_records = [
        # Record 1: approved, approved, active, business, true bools, envelope_pair credentials, twilio assigned
        {
            "sms_compliance_status": "approved",
            "integration_write_status": "approved",
            "subscription_status": "active",
            "subscription_tier": "business",
            "jobber_connected": True,
            "google_calendar_connected": True,
            "auto_reply_sms": True,
            "jobber_access_token": valid_env,
            "jobber_refresh_token": valid_env,
            "google_calendar_access_token": valid_env,
            "google_calendar_refresh_token": valid_env,
            "twilio_number": "+15555550123",
            "post_deletion_billing": {"last_type": "INITIAL_BUY", "charges": 1},
        },
        # Record 2: pending, pending, trial, personal, false bools, plaintext_pair credentials
        {
            "sms_compliance_status": "pending",
            "integration_write_status": "pending",
            "subscription_status": "trial",
            "subscription_tier": "personal",
            "jobber_connected": False,
            "google_calendar_connected": False,
            "auto_reply_sms": False,
            "jobber_access_token": "legacy_acc",
            "jobber_refresh_token": "legacy_ref",
            "google_calendar_access_token": "legacy_acc",
            "google_calendar_refresh_token": "legacy_ref",
            "post_deletion_billing": {"last_type": "RENEWAL"},
        },
        # Record 3: rejected, rejected, expired, businessPro, invalid bools (other), malformed credentials
        {
            "sms_compliance_status": "rejected",
            "integration_write_status": "rejected",
            "subscription_status": "expired",
            "subscription_tier": "businessPro",
            "jobber_connected": "invalid_bool",
            "google_calendar_connected": "invalid_bool",
            "auto_reply_sms": "invalid_bool",
            "jobber_access_token": 12345,
            "google_calendar_access_token": 12345,
            "post_deletion_billing": {"last_type": "EXPIRED"},
        },
        # Record 4: missing keys, cancelled, none, absent credentials
        {
            "subscription_status": "cancelled",
            "subscription_tier": "none",
            "post_deletion_billing": {"last_type": "custom_type"},
        },
        # Record 5: other values for statuses/tiers, post_deletion_billing unknown, mixed credentials
        {
            "sms_compliance_status": "invalid_status",
            "integration_write_status": "invalid_status",
            "subscription_status": "invalid_status",
            "subscription_tier": "invalid_tier",
            "jobber_access_token": "plain",
            "jobber_refresh_token": valid_env,
            "google_calendar_access_token": "plain",
            "google_calendar_refresh_token": valid_env,
            "post_deletion_billing": {"last_type": 999},
        },
        # Record 6: partial credentials
        {
            "jobber_access_token": "plain_only",
            "google_calendar_access_token": "plain_only",
        },
    ]
    report = audit.summarize_contractors(sample_records)

    expected_bucket_keysets = {
        "sms_compliance_status": {"approved", "pending", "rejected", "missing", "other"},
        "integration_write_status": {"approved", "pending", "rejected", "missing", "other"},
        "auto_reply_sms": {"true", "false", "missing", "other"},
        "jobber_connected": {"true", "false", "missing", "other"},
        "google_calendar_connected": {"true", "false", "missing", "other"},
        "jobber_credentials": {"absent", "plaintext_pair", "envelope_pair", "mixed", "partial", "malformed"},
        "google_calendar_credentials": {"absent", "plaintext_pair", "envelope_pair", "mixed", "partial", "malformed"},
        "twilio_number_assigned": {"true", "false"},
        "subscription_status": {"trial", "active", "expired", "cancelled", "missing", "other"},
        "subscription_tier": {"personal", "business", "businessPro", "none", "missing", "other"},
        "post_deletion_billing_types": {"INITIAL_BUY", "RENEWAL", "EXPIRED", "other", "unknown"},
    }

    for key, expected_keys in expected_bucket_keysets.items():
        bucket = report[key]
        assert isinstance(bucket, dict), f"Bucket {key} must be a dict"
        assert set(bucket.keys()) == expected_keys, f"Bucket {key} keyset mismatch: observed {set(bucket.keys())} != expected {expected_keys}"
