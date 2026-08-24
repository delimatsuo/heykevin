import importlib.util
import sys
from pathlib import Path
from typing import Any
from google.cloud import firestore


def _load_smoke_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "phase0_staging_smoke.py"
    spec = importlib.util.spec_from_file_location("phase0_staging_smoke", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["phase0_staging_smoke"] = module
    spec.loader.exec_module(module)
    return module


class _FakeDocSnapshot:
    def __init__(self, exists: bool = False, data: dict[str, Any] | None = None):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return dict(self._data)


class _FakeDocRef:
    def __init__(self, data: dict[str, Any] | None = None):
        self.data = data or {}
        self.set_calls = []
        self.update_calls = []

    def get(self):
        return _FakeDocSnapshot(exists=bool(self.data), data=self.data)

    def set(self, data: dict[str, Any], merge: bool = False):
        self.set_calls.append((data, merge))
        if merge:
            self.data.update(data)
        else:
            self.data = dict(data)

    def update(self, updates: dict[str, Any]):
        self.update_calls.append(updates)
        for k, v in updates.items():
            if v is firestore.DELETE_FIELD:
                self.data.pop(k, None)
            else:
                self.data[k] = v


class _FakeFirestoreClient:
    def __init__(self):
        self.docs = {}

    def collection(self, name: str):
        assert name == "contractors"
        return self

    def document(self, doc_id: str):
        if doc_id not in self.docs:
            self.docs[doc_id] = _FakeDocRef()
        return self.docs[doc_id]


def test_seed_contractor_resets_all_integration_lifecycle_and_safety_fields():
    """Proves seed_contractor deletes every Jobber/Google lifecycle field and safety gate under merge=True."""
    smoke = _load_smoke_module()
    db = _FakeFirestoreClient()
    contractor_id = "test_smoke_contractor"

    # Pre-populate with dirty/stale integration and safety state
    initial_doc = _FakeDocRef({
        "contractor_id": contractor_id,
        "jobber_connected": True,
        "jobber_generation": 5,
        "jobber_access_token": "stale-access",
        "jobber_refresh_token": "stale-refresh",
        "jobber_connected_at": 100.0,
        "jobber_disconnected_at": 200.0,
        "jobber_token_refreshed_at": 300.0,
        "jobber_token_expires_at": 400.0,
        "jobber_refresh_claim_id": "stale-claim",
        "jobber_refresh_claim_expires_at": 500.0,
        "jobber_refresh_claim_generation": 5,
        "jobber_lead_capture_enabled": True,
        "jobber_token_envelope_required": True,
        "google_calendar_connected": True,
        "google_calendar_generation": 3,
        "google_calendar_access_token": "stale-gcal-access",
        "google_calendar_refresh_token": "stale-gcal-refresh",
        "google_calendar_scope": "https://www.googleapis.com/auth/calendar",
        "google_calendar_connected_at": 150.0,
        "google_calendar_disconnected_at": 250.0,
        "google_calendar_token_refreshed_at": 350.0,
        "google_calendar_token_expires_at": 450.0,
        "google_calendar_refresh_claim_id": "stale-gcal-claim",
        "google_calendar_refresh_claim_expires_at": 550.0,
        "google_calendar_refresh_claim_generation": 3,
        "google_calendar_token_envelope_required": True,
        "automation_approvals": {"jobber_create_job": True},
        "gated_actions": {"caller_text_reply": True},
        "integration_write_status": "approved",
        "sms_compliance_status": "approved",
    })
    db.docs[contractor_id] = initial_doc

    raw_token = smoke.seed_contractor(db, contractor_id)
    assert raw_token.startswith(f"kv_ct_{contractor_id[:8]}_")

    # Verify that all deleted fields are absent from final doc
    deleted_fields = [
        "automation_approvals",
        "gated_actions",
        "integration_write_status",
        "sms_compliance_status",
        "jobber_connected",
        "jobber_generation",
        "jobber_access_token",
        "jobber_refresh_token",
        "jobber_connected_at",
        "jobber_disconnected_at",
        "jobber_token_refreshed_at",
        "jobber_token_expires_at",
        "jobber_refresh_claim_id",
        "jobber_refresh_claim_expires_at",
        "jobber_refresh_claim_generation",
        "jobber_lead_capture_enabled",
        "jobber_token_envelope_required",
        "google_calendar_connected",
        "google_calendar_generation",
        "google_calendar_access_token",
        "google_calendar_refresh_token",
        "google_calendar_scope",
        "google_calendar_connected_at",
        "google_calendar_disconnected_at",
        "google_calendar_token_refreshed_at",
        "google_calendar_token_expires_at",
        "google_calendar_refresh_claim_id",
        "google_calendar_refresh_claim_expires_at",
        "google_calendar_refresh_claim_generation",
        "google_calendar_token_envelope_required",
    ]

    for field in deleted_fields:
        assert field not in initial_doc.data, f"Field {field} was not deleted by seed_contractor"

    # Verify baseline properties are correctly seeded
    assert initial_doc.data["active"] is True
    assert initial_doc.data["codex_managed"] is True
    assert initial_doc.data["mode"] == "business"
