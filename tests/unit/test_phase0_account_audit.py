import importlib.util
import json
from pathlib import Path


def _load_audit_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "phase0_account_audit.py"
    spec = importlib.util.spec_from_file_location("phase0_account_audit", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contractor_summary_counts_release_gate_fields_without_raw_unknown_values():
    audit = _load_audit_module()

    summary = audit.summarize_contractors(
        [
            {
                "gated_actions": {
                    "caller_text_reply": True,
                    "jobber_create_job": True,
                    "jobber_create_quote": True,
                    "leak_+15551234567": True,
                },
                "sms_compliance_status": "approved",
                "integration_write_status": "pending",
                "automation_approvals": {
                    "google_create_event": True,
                    "jobber_create_job": True,
                    "jobber_create_quote": True,
                    "custom_secret_value": True,
                },
                "auto_reply_sms": True,
                "jobber_access_token": "secret-token",
                "google_calendar_access_token": "",
                "twilio_number": "+15551234567",
                "subscription_status": "active",
                "subscription_tier": "business",
            },
            {
                "sms_compliance_status": "freeform +15550000000",
                "integration_write_status": "approved",
                "auto_reply_sms": False,
                "subscription_status": "trial",
                "subscription_tier": "businessPro",
            },
            {},
        ]
    )

    assert summary["total_contractors"] == 3
    assert summary["gated_action_keys"] == {
        "caller_text_reply": 1,
        "jobber_create_job": 1,
        "jobber_create_quote": 1,
        "other": 1,
    }
    assert summary["sms_compliance_status"] == {"approved": 1, "missing": 1, "other": 1}
    assert summary["integration_write_status"] == {"approved": 1, "missing": 1, "pending": 1}
    assert summary["automation_approval_keys"] == {
        "google_create_event": 1,
        "jobber_create_job": 1,
        "jobber_create_quote": 1,
        "other": 1,
    }
    assert summary["auto_reply_sms"] == {"false": 1, "missing": 1, "true": 1}
    assert summary["jobber_connected"] == {"false": 2, "true": 1}
    assert summary["google_calendar_connected"] == {"false": 3}
    assert summary["twilio_number_assigned"] == {"false": 2, "true": 1}
    assert summary["active_or_trial_business_accounts"] == 2

    encoded = json.dumps(summary)
    assert "+15551234567" not in encoded
    assert "secret-token" not in encoded
    assert "freeform" not in encoded
    assert "custom_secret_value" not in encoded


def test_audit_defines_retired_and_active_action_keys():
    audit = _load_audit_module()
    assert audit.RETIRED_ACTION_KEYS == {"jobber_create_job", "jobber_create_quote"}
    assert audit.RETIRED_ACTION_KEYS.isdisjoint(audit.ACTIVE_ACTION_KEYS)
    assert audit.KNOWN_ACTION_KEYS == audit.ACTIVE_ACTION_KEYS | audit.RETIRED_ACTION_KEYS


def test_estimate_summary_counts_status_and_age_buckets_without_token_data():
    audit = _load_audit_module()
    now = 1_700_000_000

    summary = audit.summarize_estimates(
        [
            {"status": "pending", "created_at": now - 2 * 24 * 3600, "token_hash": "secret"},
            {"status": "complete", "created_at": now - 20 * 24 * 3600, "caller_phone": "+15550000000"},
            {"status": "weird +15551234567", "created_at": now - 120 * 24 * 3600},
            {"status": "processing"},
        ],
        now=now,
    )

    assert summary["total_estimates"] == 4
    assert summary["status"] == {"complete": 1, "other": 1, "pending": 1, "processing": 1}
    assert summary["age_buckets"] == {
        "0_7_days": 1,
        "8_30_days": 1,
        "over_90_days": 1,
        "missing_created_at": 1,
    }

    encoded = json.dumps(summary)
    assert "secret" not in encoded
    assert "+15550000000" not in encoded
    assert "weird" not in encoded


def test_stream_collection_uses_bounded_firestore_call():
    audit = _load_audit_module()
    calls = []

    class FakeDoc:
        def to_dict(self):
            return {"subscription_status": "trial"}

    class FakeCollection:
        def stream(self, **kwargs):
            calls.append(kwargs)
            return iter([FakeDoc()])

    class FakeClient:
        def collection(self, name):
            assert name == "contractors"
            return FakeCollection()

    assert list(audit._stream_collection(FakeClient(), "contractors")) == [
        {"subscription_status": "trial"}
    ]
    assert calls == [{"retry": None, "timeout": audit.FIRESTORE_STREAM_TIMEOUT_SECONDS}]


def test_main_reports_firestore_failure_without_traceback(capsys):
    audit = _load_audit_module()

    class BadClient:
        def collection(self, _name):
            raise RuntimeError("raw credential failure with token abc123")

    result = audit.main(
        ["--project", "kevin-491315", "--environment", "production"],
        client_factory=lambda **_kwargs: BadClient(),
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "Audit failed while reading Firestore" in captured.err
    assert "Traceback" not in captured.err
    assert "abc123" not in captured.err
