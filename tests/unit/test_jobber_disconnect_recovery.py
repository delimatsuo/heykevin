"""Durable fan-out, actual transaction commits, lease fencing and fair scheduling."""
from tests.unit.jobber_disconnect_test_support import seed_required_environment
seed_required_environment()

import datetime as dt
import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client

from tests.unit.jobber_disconnect_test_support import jobber_settings, active_db, admit, binding, clone
from app.db.jobber_disconnect_receipts import compute_work_doc_id, MAX_KEY_VERSION, JobberDisconnectError
from app.db.jobber_disconnect_transactions import Deadline, run_transaction
from app.services.jobber_disconnect_recovery import (
    acquire_work_lease_atomic, process_work_batch, finalize_work_lease_atomic,
    release_work_lease_on_failure_atomic, _sync_run_recovery_once, REVIEW_COLLECTION,
)


def setup_one():
    db = active_db()
    original = binding(db)
    admit(db)
    work = acquire_work_lease_atomic(db, "worker")
    return db, original, work


def complete_page(db, work):
    processed, more, cursor = process_work_batch(db, work, work["lease_id"])
    finalize_work_lease_atomic(db, work["work_id"], work["lease_id"], more, cursor, expected_work=work)
    return processed


def test_worker_retains_real_envelopes_and_audits_suspension():
    db, original, work = setup_one()
    assert complete_page(db, work) == 1
    data = db._data["contractors/contractor"]
    for key in ("jobber_access_token", "jobber_refresh_token", "jobber_token_envelope_required"):
        assert data[key] == original[key]
    assert data["jobber_generation"] == 3
    assert data["jobber_lifecycle_epoch"] == 4
    assert data["jobber_accepted_disconnect_barrier"] == 0
    assert data["jobber_disconnect_processed_barrier"] == 1
    assert data["jobber_disconnect_state"] == "reauthorization_required"
    assert data["jobber_lead_capture_enabled"] is False
    audits = [d for p, d in db._data.items() if p.startswith("jobber_provider_suspension_audit/")]
    assert len(audits) == 1
    assert audits[0]["credentials_retained"] is True
    assert audits[0]["provider_revocation_attempted"] is False
    assert not any("revocation" in p or "integration_lifecycle_audit" in p for p in db._data)


def test_at_least_300_bindings_complete_without_spending_failure_budget():
    db = active_db()
    for index in range(305):
        binding(db, cid=f"c{index:04d}")
    admit(db)
    total = 0
    for index in range(8):
        total += _sync_run_recovery_once(db, "same_worker_label", Deadline(20))
    assert total == 305
    work = db._data["jobber_disconnect_work/" + compute_work_doc_id("app", "account")]
    assert work["status"] == "completed" and work["attempt_count"] == 0


def test_failure_does_not_skip_contractor_or_advance_page():
    db = active_db()
    binding(db, "a")
    binding(db, "b")
    admit(db)
    db.fail_write_path = "contractors/b"
    _sync_run_recovery_once(db, "worker", Deadline(20))
    work = db._data["jobber_disconnect_work/" + compute_work_doc_id("app", "account")]
    assert work["cursor"] is None and work["attempt_count"] == 1
    assert db._data["contractors/a"]["jobber_generation"] == 3
    assert db._data["contractors/b"]["jobber_generation"] == 2
    db.fail_write_path = None
    db.set_server_time(db.get_server_time() + dt.timedelta(seconds=20))
    _sync_run_recovery_once(db, "worker", Deadline(20))
    assert db._data["contractors/a"]["jobber_generation"] == 3
    assert db._data["contractors/b"]["jobber_generation"] == 3


@pytest.mark.parametrize("field,value", [
    ("jobber_generation", True), ("jobber_generation", -1), ("jobber_generation", MAX_KEY_VERSION),
    ("jobber_lifecycle_epoch", None), ("jobber_accepted_disconnect_barrier", True),
    ("jobber_disconnect_processed_barrier", True),
])
def test_bad_lifecycle_never_coerces_or_saturates(field, value):
    db, original, work = setup_one()
    db.collection("contractors").document("contractor").update({field: value})
    before = clone(db._data["contractors/contractor"])
    with pytest.raises(JobberDisconnectError):
        process_work_batch(db, work, work["lease_id"])
    assert db._data["contractors/contractor"] == before


def test_same_worker_label_gets_new_nonce_and_old_lease_cannot_mutate():
    db, original, old = setup_one()
    db.set_server_time(db.get_server_time() + dt.timedelta(seconds=61))
    new = acquire_work_lease_atomic(db, "worker")
    assert new["lease_id"] != old["lease_id"]
    with pytest.raises(JobberDisconnectError):
        process_work_batch(db, old, old["lease_id"])
    with pytest.raises(JobberDisconnectError):
        finalize_work_lease_atomic(db, old["work_id"], old["lease_id"], False, None, expected_work=old)
    assert db._data["contractors/contractor"] == original


def test_new_receipt_prevents_old_finalization_and_mutation():
    db, original, work = setup_one()
    admit(db, admission="adm_2")
    with pytest.raises(JobberDisconnectError):
        process_work_batch(db, work, work["lease_id"])
    with pytest.raises(JobberDisconnectError):
        finalize_work_lease_atomic(db, work["work_id"], work["lease_id"], False, None, expected_work=work)
    assert db._data["contractors/contractor"] == original
    assert db._data["jobber_disconnect_work/" + work["work_id"]]["barrier"] == 2


def test_fresh_reconnect_not_suspended_by_old_page():
    db, original, work = setup_one()
    binding(db, accepted=1)
    assert complete_page(db, work) == 0
    assert db._data["contractors/contractor"]["jobber_connected"] is True


def test_poison_busy_and_unselected_candidates_cannot_starve_due_work():
    db = active_db()
    accounts = [f"a{i}" for i in range(15)]
    for i, account in enumerate(accounts):
        binding(db, cid=f"c{i}", account=account)
        admit(db, account=account, admission=f"adm_{i}")
    keys = sorted(p for p in db._data if p.startswith("jobber_disconnect_work/"))
    # A malformed prefix and nine future records must not block later due work.
    db._data[keys[0]]["next_attempt_at"] = "malformed"
    for key in keys[1:10]:
        db._data[key]["next_attempt_at"] += 1000
    completed = 0
    for _ in range(12):
        completed += _sync_run_recovery_once(db, "worker", Deadline(20))
    assert completed == 5
    assert any(p.startswith(REVIEW_COLLECTION + "/") for p in db._data)


def test_consecutive_failure_budget_reaches_review():
    db, original, work = setup_one()
    for index in range(5):
        release_work_lease_on_failure_atomic(db, work["work_id"], work["lease_id"], expected_work=work)
        db.set_server_time(db.get_server_time() + dt.timedelta(seconds=301))
        if index < 4:
            work = acquire_work_lease_atomic(db, "worker")
    assert db._data["jobber_disconnect_work/" + work["work_id"]]["status"] == "needs_review"


def test_real_sdk_cursor_serialization_without_network():
    client = Client(project="fictional-project", credentials=AnonymousCredentials())
    query = client.collection("contractors").order_by("__name__").limit(50)
    query = query.start_after({"__name__": client.collection("contractors").document("cursor")})
    pb = query._to_protobuf()
    assert pb.start_at.values[0].reference_value.endswith("/contractors/cursor")


@pytest.mark.parametrize("malformed_review", [False, True])
def test_legal_poison_document_name_and_bad_review_cannot_starve(malformed_review):
    from app.services.jobber_disconnect_recovery import _review_ref
    db = active_db()
    binding(db)
    admit(db)
    poison = {"untrusted": "fictional payload must not enter review marker"}
    db.collection("jobber_disconnect_work").document("! poison").set(poison)
    review_ref = _review_ref(db, "! poison")
    if malformed_review:
        review_ref.set({"raw_body": "fictional sensitive payload", "schema_version": False})
    work = acquire_work_lease_atomic(db, "worker")
    assert work is not None and work["work_id"] == compute_work_doc_id("app", "account")
    assert db._data["jobber_disconnect_work/! poison"] == poison
    review = db._data[review_ref.path]
    assert set(review) == {"schema_version", "work_document_digest", "reason", "status", "created_at"}
    assert review["status"] == "needs_review" and "poison" not in repr(review)
    assert db._data["jobber_disconnect_scan_cursor/current"]["cursor_work_id"] == work["work_id"]


def test_poison_cursor_tracks_only_examined_boundary_with_small_page(monkeypatch):
    import app.services.jobber_disconnect_recovery as recovery
    db = active_db()
    binding(db)
    admit(db)
    db.collection("jobber_disconnect_work").document("! poison").set({})
    monkeypatch.setattr(recovery, "CANDIDATE_SCAN_LIMIT", 1)
    assert acquire_work_lease_atomic(db, "worker") is None
    assert db._data["jobber_disconnect_scan_cursor/current"]["cursor_work_id"] == "! poison"
    assert acquire_work_lease_atomic(db, "worker")["work_id"] == compute_work_doc_id("app", "account")
