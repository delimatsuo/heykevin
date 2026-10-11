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


async def suspended_owner(phase="reserved"):
    from app.services import integration_token_mutations as mutations
    db, original, work = setup_one()
    state = "audited_worker_oauth_state"
    await mutations.create_oauth_state(db=db, collection_name="jobber_oauth_states", state=state,
        contractor_id="contractor", provider="jobber")
    _, obs, proof = await mutations.consume_jobber_oauth_state(db=db, state=state)
    if phase == "provider_request_started":
        await mutations.transition_provider_operation_intent_to_started_cas(
            contractor_id="contractor", provider="jobber", claim_id=obs["claim_id"], kind="connect", db=db)
    return db, original, work, obs, proof


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["ordinary", "missing_context", "foreign_app", "foreign_source", "future", "generation", "fingerprint", "missing_audit", "malformed_audit", "audit_time", "unaudited"])
async def test_worker_rejects_hostile_postbarrier_owner_or_audit_without_writes(corruption):
    from google.cloud.firestore_v1 import DELETE_FIELD
    from app.services.jobber_disconnect_recovery import _readback_binding
    db, _, work, _, _ = await suspended_owner()
    ref = db.collection("contractors").document("contractor")
    data = clone(db._data[ref.path]); prefix = "jobber_operation_intent_"
    audit_path = next(k for k in db._data if k.startswith("jobber_provider_suspension_audit/"))
    audit_ref = db.collection("jobber_provider_suspension_audit").document(audit_path.split("/")[1])
    if corruption == "ordinary":
        context = dict(data[prefix + "context"])
        context.update(purpose="ordinary", account_id=context.pop("source_account_id"), barrier=context.pop("receipt_counter_at_start"))
        ref.update({prefix + "kind": "business", prefix + "context": context})
    elif corruption == "missing_context": ref.update({prefix + "context": DELETE_FIELD})
    elif corruption in ("foreign_app", "foreign_source"):
        context = dict(data[prefix + "context"])
        context["app_id" if corruption == "foreign_app" else "source_account_id"] = "other"
        ref.update({prefix + "context": context})
    elif corruption == "future": ref.update({prefix + "acquired_at": db.get_server_time().timestamp() + 1.0})
    elif corruption == "generation": ref.update({prefix + "generation": 999})
    elif corruption == "fingerprint": ref.update({prefix + "credentials_fingerprint": "0" * 64})
    elif corruption == "missing_audit": audit_ref.delete()
    elif corruption == "malformed_audit": audit_ref.update({"credentials_retained": False})
    elif corruption == "audit_time": audit_ref.update({"timestamp": data["jobber_disconnected_at"] - 1.0, "created_at": data["jobber_disconnected_at"] - 1.0})
    else:
        ref.update({"jobber_disconnect_state": DELETE_FIELD, "jobber_disconnect_processed_barrier": DELETE_FIELD})
        audit_ref.delete()
    before, version = clone(db._data), db.version
    with pytest.raises(JobberDisconnectError): process_work_batch(db, work, work["lease_id"])
    assert db._data == before and db.version == version
    with pytest.raises(JobberDisconnectError):
        run_transaction(db, lambda txn: _readback_binding(txn, db, "contractor", work), read_only=True)
    assert db._data == before


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["reserved", "provider_request_started"])
async def test_worker_preserves_expired_postbarrier_owner_for_consume_recovery(phase):
    from fastapi import HTTPException
    from app.services import integration_token_mutations as mutations
    from app.services.integration_tokens import parse_provider_operation_intent
    db, original, work, old, _ = await suspended_owner(phase)
    db.set_server_time(db.get_server_time() + dt.timedelta(seconds=60))
    work = acquire_work_lease_atomic(db, "worker_again")
    before = clone(db._data["contractors/contractor"])
    assert process_work_batch(db, work, work["lease_id"])[0] == 0
    assert db._data["contractors/contractor"] == before
    state = "expired_postbarrier_fresh_state"
    await mutations.create_oauth_state(db=db, collection_name="jobber_oauth_states", state=state,
        contractor_id="contractor", provider="jobber")
    if phase == "provider_request_started":
        with pytest.raises(HTTPException): await mutations.consume_jobber_oauth_state(db=db, state=state)
        assert parse_provider_operation_intent(db._data["contractors/contractor"], "jobber")[0] == "quarantined"
        assert process_work_batch(db, work, work["lease_id"])[0] == 0
        state += "_retry"
        await mutations.create_oauth_state(db=db, collection_name="jobber_oauth_states", state=state,
            contractor_id="contractor", provider="jobber")
    _, obs, _ = await mutations.consume_jobber_oauth_state(db=db, state=state)
    assert obs["claim_id"] != old["claim_id"]
    assert process_work_batch(db, work, work["lease_id"])[0] == 0
    current = db._data["contractors/contractor"]
    for key in ("jobber_access_token", "jobber_refresh_token", "jobber_token_envelope_required"):
        assert current[key] == original[key]
    assert current["jobber_connected"] is False and current["jobber_lead_capture_enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_work", "work_mismatch", "audit_collision"])
async def test_state_creation_cannot_invent_suspension_evidence(fault):
    from app.services import integration_token_mutations as mutations
    from app.services.integration_tokens import IntegrationTokenCASConflict
    from app.db.jobber_disconnect_receipts import compute_suspension_audit_doc_id
    db, _, work = setup_one()
    work_ref = db.collection("jobber_disconnect_work").document(work["work_id"])
    if fault == "missing_work": work_ref.delete()
    elif fault == "work_mismatch": work_ref.update({"last_receipt_id": "different_receipt"})
    else:
        audit_id = compute_suspension_audit_doc_id("contractor", "app", "account", 1, 3, 4)
        db.collection("jobber_provider_suspension_audit").document(audit_id).set({"schema_version": 1})
    before, version = clone(db._data), db.version
    with pytest.raises(IntegrationTokenCASConflict):
        await mutations.create_oauth_state(db=db, collection_name="jobber_oauth_states", state="evidence_missing_state",
            contractor_id="contractor", provider="jobber")
    assert db._data == before and db.version == version


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["claim", "binding", "receipt"])
async def test_worker_postcommit_readback_preserves_replacement(monkeypatch, replacement):
    from app.services.jobber_disconnect_recovery import _readback_binding
    db, _, work, _, _ = await suspended_owner()
    original_factory = db.jobber_transaction_factory
    changed = []
    def factory(**kwargs):
        txn = original_factory(**kwargs); commit = txn._commit
        def finish():
            result = commit()
            if not kwargs["read_only"] and not changed:
                changed.append(True)
                if replacement == "claim":
                    # A malformed replacement cannot be accepted as audited ownership.
                    db.collection("contractors").document("contractor").update({"jobber_operation_intent_context": {}})
                elif replacement == "binding": binding(db, accepted=1)
                else: admit(db, admission="adm_superseding")
            return result
        txn._commit = finish
        return txn
    monkeypatch.setattr(db, "jobber_transaction_factory", factory)
    if replacement == "binding":
        assert process_work_batch(db, work, work["lease_id"])[0] == 0
        assert db._data["contractors/contractor"]["jobber_connected"] is True
    else:
        with pytest.raises(JobberDisconnectError): process_work_batch(db, work, work["lease_id"])
        if replacement == "claim": assert db._data["contractors/contractor"]["jobber_operation_intent_context"] == {}
        else: assert db._data["jobber_disconnect_work/" + work["work_id"]]["barrier"] == 2
    assert changed == [True]
