"""Real Jobber service/CAS paths against a staged offline transactional store."""
from __future__ import annotations
import base64
import copy
import json
import math
from dataclasses import replace
from types import SimpleNamespace

import pytest
from google.cloud.firestore_v1 import DELETE_FIELD
from tests.unit.jobber_disconnect_test_support import (
    seed_required_environment, jobber_settings, FakeFirestoreDb, active_db, admit, binding,
)
seed_required_environment()

from app.services import integration_token_mutations as mutations
from app.services import jobber, jobber_admissions as admission
from app.services.integration_tokens import (
    IntegrationTokenCASConflict, parse_provider_operation_intent, resolve_usable_token_pair,
    get_provider_operation_intent_keys, MAX_KEY_VERSION,
)


class BoundedLegacyTestTransaction:
    """Adapt older rich test fixtures to the bounded SDK lifecycle without I/O.

    Google uses the fixture's original transaction. Jobber explicitly opts in;
    all reads precede writes and a failed mid-commit restores every touched ref.
    Existing fixture commit fault injection remains effective.
    """
    def __init__(self, db, *, deadline, read_only):
        self.inner = db.transaction()
        self.inner._read_only = read_only
        self.jobber_deadline = deadline
        self._read_only = read_only
        self._max_attempts = 3
        self._reads = {}
        self._references = {}
        self._touched = {}
        self._write_started = False
    @property
    def id(self):
        return self.inner._id
    def _begin(self, retry_id=None):
        self.jobber_deadline.timeout()
        self.inner._begin(retry_id=retry_id)
    def _reference_identity(self, ref):
        # collection.document() may return a fresh wrapper on every call. Keep
        # the actual document alive so Python cannot recycle a cache key.
        target = getattr(ref, "real_ref", ref)
        key = id(target)
        self._references.setdefault(key, (target, ref))
        return key, target
    def get(self, ref, *, retry, timeout):
        assert retry is None and type(timeout) in (int, float) and 0 < timeout <= 2
        if self._write_started:
            raise RuntimeError("Read after staged write")
        if not getattr(ref, "id", None):
            ref.id = getattr(ref, "doc_id", None) or f"test_ref_{id(ref)}"
        key, _ = self._reference_identity(ref)
        if key not in self._reads:
            snap = ref.get(transaction=self.inner)
            # Fixtures originally returned snapshots without an ID. Preserve the
            # exact queried document identity, including explicit missing rows.
            snap.id = getattr(ref, "id", getattr(ref, "doc_id", None))
            self._reads[key] = snap
        return iter([self._reads[key]])
    def _stage(self, method, ref, *args):
        assert not self._read_only
        self._write_started = True
        key, target = self._reference_identity(ref)
        if key not in self._touched:
            self._touched[key] = (target, copy.deepcopy(target.__dict__, {id(DELETE_FIELD): DELETE_FIELD}))
        return getattr(self.inner, method)(target, *args)
    def update(self, ref, data): return self._stage("update", ref, data)
    def set(self, ref, data): return self._stage("set", ref, data)
    def create(self, ref, data): return self._stage("create", ref, data)
    def delete(self, ref): return self._stage("delete", ref)
    def _commit(self):
        self.jobber_deadline.timeout()
        try:
            return self.inner._commit()
        except Exception as exc:
            from google.api_core.exceptions import Aborted
            if not self.inner.committed and not isinstance(exc, Aborted):
                for ref, before in self._touched.values():
                    ref.__dict__.clear()
                    ref.__dict__.update(before)
            raise
    def _rollback(self):
        return self.inner._rollback()


@pytest.fixture(autouse=True)
def fictional_encryption(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "integration_token_encryption_keys", json.dumps({"1": base64.b64encode(b"q" * 32).decode()}))
    monkeypatch.setattr(settings, "integration_token_active_key_version", "1")
    monkeypatch.setattr(settings, "integration_token_encrypted_writes_enabled", True)


def patch_database(monkeypatch, db):
    monkeypatch.setattr(admission, "get_firestore_client", lambda: db)
    monkeypatch.setattr(mutations, "get_firestore_client", lambda: db)


def forbid_http(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Paused binding must not dispatch provider HTTP")
    monkeypatch.setattr(jobber.httpx, "AsyncClient", fail)


def data(db, cid="contractor"):
    return db.collection("contractors").document(cid).get().to_dict()


async def reserved(db, kind="refresh", cid="contractor"):
    snapshot = await mutations.load_durable_provider_snapshot(cid, "jobber", db=db)
    assert snapshot is not None
    claim, _ = await mutations.acquire_provider_operation_intent_cas(contractor_id=cid, provider="jobber", kind=kind,
        observed_generation=snapshot["generation"], observed_lifecycle_epoch=snapshot["lifecycle_epoch"],
        observed_access_raw=snapshot["access_token_raw"], observed_refresh_raw=snapshot["refresh_token_raw"],
        observed_jobber_context=snapshot["jobber_context"], db=db)
    return snapshot, claim


async def started(db, kind="refresh", cid="contractor"):
    snapshot, claim = await reserved(db, kind, cid)
    await mutations.transition_provider_operation_intent_to_started_cas(contractor_id=cid, provider="jobber", claim_id=claim, kind=kind, db=db)
    return snapshot, claim


async def oauth_proof(db, *, cid="contractor", destination="account", state="fresh_state_123456789"):
    await mutations.create_oauth_state(db=db, collection_name="jobber_oauth_states", state=state, contractor_id=cid, provider="jobber")
    payload, obs, proof = await mutations.consume_jobber_oauth_state(db=db, state=state)
    await mutations.transition_provider_operation_intent_to_started_cas(contractor_id=cid, provider="jobber", claim_id=obs["claim_id"], kind="connect", db=db)
    binding_proof = mutations.finalize_jobber_binding_proof(proof, destination, "new-access", "new-refresh")
    return payload, obs, binding_proof


async def complete(db, obs, proof, destination="account"):
    return await mutations.connect_provider_cas(contractor_id=obs["contractor_id"], provider="jobber",
        access_token="new-access", refresh_token="new-refresh", jobber_account_id=destination,
        observed_generation=obs["generation"], observed_lifecycle_epoch=obs["lifecycle_epoch"],
        observed_access_raw=obs["observed_access_raw"], observed_refresh_raw=obs["observed_refresh_raw"],
        claim_id=obs["claim_id"], jobber_binding_proof=proof, db=db)


@pytest.mark.asyncio
async def test_receipt_immediately_blocks_actual_service_status_refresh_and_capture(monkeypatch):
    db = active_db(); binding(db)
    db.collection("contractors").document("contractor").update({"jobber_token_expires_at": 9999999999.0, "jobber_connected_at": 100.0})
    patch_database(monkeypatch, db); forbid_http(monkeypatch)
    admit(db)
    status = await mutations.load_jobber_connection_status("contractor", db=db)
    assert (status.connection_status, status.connected, status.lead_capture_enabled, status.connected_at) == ("disconnect_pending", False, False, None)
    assert await mutations.load_durable_provider_snapshot("contractor", "jobber", db=db) is None
    assert await jobber.refresh_access_token({"contractor_id": "contractor"}) is None
    assert await jobber._graphql_request_with_refresh({"contractor_id": "contractor"}, "query { account { id } }") is None
    with pytest.raises(IntegrationTokenCASConflict):
        await mutations.update_jobber_lead_capture_cas(contractor_id="contractor", enabled=True, db=db)
    disabled = await mutations.update_jobber_lead_capture_cas(contractor_id="contractor", enabled=False, db=db)
    assert disabled.enabled is False and disabled.connected is False
    assert len(list(db.collection("admin_audit_events").stream())) == 1


@pytest.mark.asyncio
async def test_same_account_all_bindings_paused_other_account_unaffected():
    db = active_db(); binding(db, "one"); binding(db, "two"); binding(db, "other", "different")
    admit(db)
    for cid in ("one", "two"):
        assert (await mutations.load_jobber_connection_status(cid, db=db)).connection_status == "disconnect_pending"
    assert (await mutations.load_jobber_connection_status("other", db=db)).connection_status == "connected"
    assert await mutations.load_durable_provider_snapshot("other", "jobber", db=db) is not None


@pytest.mark.asyncio
async def test_started_refresh_result_and_quarantine_cannot_cross_receipt():
    db = active_db(); original = binding(db)
    snap, claim = await started(db)
    admit(db)
    kwargs = dict(contractor_id="contractor", provider="jobber", claim_id=claim,
        observed_generation=snap["generation"], observed_lifecycle_epoch=snap["lifecycle_epoch"],
        observed_access_raw=snap["access_token_raw"], observed_refresh_raw=snap["refresh_token_raw"], db=db)
    with pytest.raises(IntegrationTokenCASConflict):
        await mutations.persist_refreshed_tokens_cas(**kwargs, new_access_token="stale-access", new_refresh_token="stale-refresh")
    assert await mutations.quarantine_provider_reauth_cas(**kwargs) is False
    assert data(db)["jobber_access_token"] == original["jobber_access_token"]
    assert "jobber_refresh_outcome_unknown" not in data(db)


@pytest.mark.asyncio
async def test_activation_at_zero_invalidates_reserved_preactivation_claim():
    db = FakeFirestoreDb()
    db.collection("contractors").document("contractor").set(dict(active=True, jobber_connected=True,
        jobber_generation=0, jobber_lifecycle_epoch=0, jobber_access_token="legacy-access", jobber_refresh_token="legacy-refresh"))
    _, claim = await reserved(db)
    control = active_db().collection("jobber_disconnect_control").document("current").get().to_dict()
    db.collection("jobber_disconnect_control").document("current").set(control)
    with pytest.raises(IntegrationTokenCASConflict):
        await mutations.transition_provider_operation_intent_to_started_cas(contractor_id="contractor", provider="jobber", claim_id=claim, db=db)


@pytest.mark.asyncio
async def test_healthy_oauth_opening_never_clears_existing_started_claim():
    db = active_db(); binding(db)
    _, claim = await started(db, "business")
    before = data(db)
    await mutations.create_oauth_state(db=db, collection_name="jobber_oauth_states", state="healthy_state_123456", contractor_id="contractor", provider="jobber")
    assert data(db) == before
    assert data(db)["jobber_operation_intent_id"] == claim


@pytest.mark.asyncio
async def test_suspended_started_claim_preempted_and_complete_pair_reconnect_leaves_others_paused():
    db = active_db(); original = binding(db); binding(db, "other")
    snap, old_claim = await started(db)
    admit(db)
    _, obs, proof = await oauth_proof(db)
    assert obs["generation"] == original["jobber_generation"] + 1
    assert obs["lifecycle_epoch"] == original["jobber_lifecycle_epoch"] + 1
    assert data(db)["jobber_lead_capture_enabled"] is False
    await complete(db, obs, proof)
    final = data(db)
    assert resolve_usable_token_pair(final, "jobber", contractor_id="contractor") == ("new-access", "new-refresh")
    assert final["jobber_accepted_disconnect_barrier"] == 1
    assert final["jobber_lead_capture_enabled"] is False
    assert (await mutations.load_jobber_connection_status("other", db=db)).connection_status == "disconnect_pending"
    assert not get_provider_operation_intent_keys("jobber").intersection(final)
    assert await mutations.quarantine_provider_reauth_cas(contractor_id="contractor", provider="jobber", claim_id=old_claim,
        observed_generation=snap["generation"], observed_lifecycle_epoch=snap["lifecycle_epoch"],
        observed_access_raw=snap["access_token_raw"], observed_refresh_raw=snap["refresh_token_raw"], db=db) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("event_account,allowed", [("account", False), ("destination", False), ("unrelated", True)])
async def test_source_and_destination_event_after_consumption_blocks_switch_only_when_relevant(event_account, allowed):
    db = active_db(); binding(db)
    _, obs, proof = await oauth_proof(db, destination="destination")
    admit(db, account=event_account)
    if allowed:
        await complete(db, obs, proof, destination="destination")
        assert data(db)["jobber_account_id"] == "destination"
    else:
        with pytest.raises(IntegrationTokenCASConflict):
            await complete(db, obs, proof, destination="destination")
        assert data(db)["jobber_account_id"] == "account"


@pytest.mark.asyncio
async def test_event_before_state_allows_verified_unknown_destination():
    db = active_db(); binding(db)
    admit(db, account="new-account")
    _, obs, proof = await oauth_proof(db, destination="new-account")
    await complete(db, obs, proof, destination="new-account")
    assert data(db)["jobber_accepted_disconnect_barrier"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_counter", [True, -1, MAX_KEY_VERSION + 1])
async def test_malformed_start_counter_rejects_consumption(bad_counter):
    db = active_db(); binding(db)
    state = "bad_counter_state_123456"
    await mutations.create_oauth_state(db=db, collection_name="jobber_oauth_states", state=state, contractor_id="contractor", provider="jobber")
    db.collection("jobber_oauth_states").document(state).update({"receipt_counter_at_start": bad_counter})
    with pytest.raises(Exception):
        await mutations.consume_jobber_oauth_state(db=db, state=state)
    assert parse_provider_operation_intent(data(db), "jobber")[0] == "absent"


@pytest.mark.asyncio
async def test_active_claimless_loose_counter_and_candidate_mismatch_rejected():
    db = active_db(); binding(db)
    with pytest.raises(IntegrationTokenCASConflict):
        await mutations.connect_provider_cas(contractor_id="contractor", provider="jobber", access_token="new-access", refresh_token="new-refresh",
            jobber_account_id="account", receipt_counter_at_start=0, db=db)
    _, obs, proof = await oauth_proof(db)
    for bad in (replace(proof, candidate_fingerprint="0" * 64), replace(proof, verified_account_id="another")):
        with pytest.raises(IntegrationTokenCASConflict):
            await complete(db, obs, bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("quarantined", [False, True])
async def test_local_removal_before_fanout_preempts_claims_no_provider_or_outbox(monkeypatch, quarantined):
    db = active_db(); original = binding(db)
    snap, claim = await started(db)
    if quarantined:
        assert await mutations.quarantine_provider_reauth_cas(contractor_id="contractor", provider="jobber", claim_id=claim,
            observed_generation=snap["generation"], observed_lifecycle_epoch=snap["lifecycle_epoch"],
            observed_access_raw=snap["access_token_raw"], observed_refresh_raw=snap["refresh_token_raw"], db=db)
    admit(db); forbid_http(monkeypatch)
    first = await mutations.disconnect_and_revoke_provider_orchestration(contractor_id="contractor", provider="jobber", db=db)
    second = await mutations.disconnect_and_revoke_provider_orchestration(contractor_id="contractor", provider="jobber", db=db)
    for key in ("audit_id", "generation", "lifecycle_epoch", "status", "revocation_status"):
        assert first[key] == second[key]
    assert first["credential_deletion"] == {"status": "executed", "attempted_by_this_request": True}
    assert second["credential_deletion"] == {"status": "executed", "attempted_by_this_request": False}
    assert first["provider_revocation"] == {"status": "not_attempted_local_only", "attempted": False, "attempted_by_this_request": False}
    assert first["outbox_id"] is None
    assert mutations.is_durable_provider_tombstone(data(db), "jobber", "contractor")
    assert not admission.BINDING_KEYS.intersection(data(db))
    assert data(db)["jobber_token_envelope_required"] is True
    assert data(db)["jobber_generation"] == original["jobber_generation"] + 1
    from app.db.integration_lifecycle_audit import REVOCATION_OUTBOX_COLLECTION
    assert list(db.collection(REVOCATION_OUTBOX_COLLECTION).stream()) == []
    assert len(list(db.collection("jobber_local_removal_audit").stream())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_receipt_after_refresh_commit_before_readback_cannot_return_credentials(monkeypatch, ambiguous):
    db = active_db(); binding(db)
    snap, claim = await started(db)
    original_factory = db.jobber_transaction_factory
    fired = False
    def factory(**kwargs):
        txn = original_factory(**kwargs)
        commit = txn._commit
        def commit_then_receipt():
            nonlocal fired
            writes = bool(txn.writes)
            result = commit()
            if writes and not fired:
                fired = True
                admit(db)
                if ambiguous:
                    raise TimeoutError("fictional lost acknowledgement")
            return result
        txn._commit = commit_then_receipt
        return txn
    monkeypatch.setattr(db, "jobber_transaction_factory", factory)
    with pytest.raises(IntegrationTokenCASConflict):
        await mutations.persist_refreshed_tokens_cas(contractor_id="contractor", provider="jobber", claim_id=claim,
            observed_generation=snap["generation"], observed_lifecycle_epoch=snap["lifecycle_epoch"],
            observed_access_raw=snap["access_token_raw"], observed_refresh_raw=snap["refresh_token_raw"],
            new_access_token="new-access", new_refresh_token="new-refresh", db=db)
    assert (await mutations.load_jobber_connection_status("contractor", db=db)).connection_status == "disconnect_pending"


@pytest.mark.asyncio
async def test_status_endpoint_and_admin_use_authoritative_snapshot(monkeypatch):
    from app.api import integrations, admin
    db = active_db(); binding(db); admit(db)
    monkeypatch.setattr(integrations, "_get_firestore", lambda: db)
    monkeypatch.setattr(integrations, "require_contractor_access", lambda *args: None)
    response = await integrations.jobber_status(contractor_id="contractor", request=None)
    assert response == dict(connected=False, connected_at=None, lead_capture_enabled=False, connection_status="disconnect_pending")
    assert admin._jobber_summary(await mutations.load_jobber_connection_status("contractor", db=db)) == {**response, "lead_capture_updated_at": None}


@pytest.mark.asyncio
async def test_worker_suspension_is_not_legacy_quarantine():
    from app.services.jobber_disconnect_recovery import run_jobber_disconnect_recovery_once
    db = active_db(); original = binding(db); admit(db)
    await run_jobber_disconnect_recovery_once(db=db)
    final = data(db)
    assert final["jobber_disconnect_state"] == "reauthorization_required"
    assert "jobber_reauthorization_required" not in final
    assert final["jobber_access_token"] == original["jobber_access_token"]
    assert parse_provider_operation_intent(final, "jobber")[0] == "absent"
    assert resolve_usable_token_pair(final, "jobber", contractor_id="contractor") == (None, None)
    assert (await mutations.load_jobber_connection_status("contractor", db=db)).connection_status == "reauthorization_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [None, True, -1, 1, MAX_KEY_VERSION + 1])
async def test_malformed_or_greater_accepted_metadata_never_admits(accepted):
    db = active_db(); binding(db)
    db.collection("contractors").document("contractor").update({"jobber_accepted_disconnect_barrier": accepted})
    assert await mutations.load_durable_provider_snapshot("contractor", "jobber", db=db) is None
    status = await mutations.load_jobber_connection_status("contractor", db=db)
    assert status.connection_status == "reauthorization_required" and not status.connected


@pytest.mark.asyncio
async def test_configured_missing_control_and_sticky_control_flags_off(monkeypatch, jobber_settings):
    db = active_db(); binding(db); admit(db)
    monkeypatch.setattr(jobber_settings, "jobber_disconnect_expected_app_id", "")
    assert await mutations.load_durable_provider_snapshot("contractor", "jobber", db=db) is None
    db.collection("jobber_disconnect_control").document("current").delete()
    monkeypatch.setattr(jobber_settings, "jobber_disconnect_enforcement_mode", "enforce")
    assert (await mutations.load_jobber_connection_status("contractor", db=db)).connection_status == "reauthorization_required"


@pytest.mark.asyncio
async def test_preactivation_state_cannot_commit_after_activation_at_zero():
    db = FakeFirestoreDb()
    db.collection("contractors").document("contractor").set(dict(active=True, jobber_connected=False, jobber_generation=0, jobber_lifecycle_epoch=0))
    _, obs, proof = await oauth_proof(db)
    control = active_db().collection("jobber_disconnect_control").document("current").get().to_dict()
    db.collection("jobber_disconnect_control").document("current").set(control)
    with pytest.raises(IntegrationTokenCASConflict):
        await complete(db, obs, proof)
    assert "jobber_access_token" not in data(db)


@pytest.mark.asyncio
async def test_normal_disconnect_commit_readback_receipt_never_returns_dispatch_token(monkeypatch):
    db = active_db(); binding(db)
    original_factory = db.jobber_transaction_factory
    fired = False
    def factory(**kwargs):
        txn = original_factory(**kwargs)
        commit = txn._commit
        def commit_with_receipt():
            nonlocal fired
            writes = bool(txn.writes)
            result = commit()
            if writes and not fired:
                fired = True
                admit(db)
            return result
        txn._commit = commit_with_receipt
        return txn
    monkeypatch.setattr(db, "jobber_transaction_factory", factory)
    forbid_http(monkeypatch)
    with pytest.raises(IntegrationTokenCASConflict):
        await mutations.disconnect_and_revoke_provider_orchestration(contractor_id="contractor", provider="jobber", db=db)


@pytest.mark.asyncio
async def test_jobber_outcome_ambiguity_uses_bounded_lifecycle_reread(monkeypatch):
    from tests.unit.jobber_disconnect_test_support import FakeDocumentReference
    db = active_db()
    binding(db)
    actual_record = mutations.record_revocation_outcome_cas
    actual_read = admission.read_disconnect_lifecycle
    reads = []
    async def lose_response(**kwargs):
        await actual_record(**kwargs)
        raise RuntimeError("fictional response lost after durable commit")
    async def observe_read(**kwargs):
        reads.append(kwargs["result"].outbox_id)
        return await actual_read(**kwargs)
    def forbid_unbounded_get(*args, **kwargs):
        raise AssertionError("Direct document get bypassed bounded transaction")
    class OfflineHTTP:
        calls = 0
        async def post(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(status_code=200, json=lambda: {"data": {"appDisconnect": {"userErrors": []}}})
    monkeypatch.setattr(mutations, "record_revocation_outcome_cas", lose_response)
    monkeypatch.setattr(admission, "read_disconnect_lifecycle", observe_read)
    monkeypatch.setattr(FakeDocumentReference, "get", forbid_unbounded_get)
    http = OfflineHTTP()
    result = await mutations.disconnect_and_revoke_provider_orchestration(
        contractor_id="contractor", provider="jobber", db=db, http_client=http)
    assert http.calls == 1
    assert len(reads) >= 2
    assert result["revocation_status"] == "provider_confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [True, None, -1, 0, float("nan"), float("inf"), "100"])
async def test_malformed_expiry_status_snapshot_capture_agree(bad):
    db = active_db()
    binding(db)
    db.collection("contractors").document("contractor").update({"jobber_token_expires_at": bad})
    status = await mutations.load_jobber_connection_status("contractor", db=db)
    assert status.connection_status == "reauthorization_required"
    assert status.connected is False and status.lead_capture_enabled is False
    assert await mutations.load_durable_provider_snapshot("contractor", "jobber", db=db) is None
    with pytest.raises(IntegrationTokenCASConflict):
        await mutations.update_jobber_lead_capture_cas(contractor_id="contractor", enabled=True, db=db)


@pytest.mark.asyncio
async def test_past_numeric_access_expiry_still_allows_refresh_admission():
    db = active_db()
    binding(db)
    db.collection("contractors").document("contractor").update({"jobber_token_expires_at": 1.0})
    assert (await mutations.load_jobber_connection_status("contractor", db=db)).connected is True
    assert (await mutations.load_durable_provider_snapshot("contractor", "jobber", db=db)) is not None
    _, claim = await reserved(db)
    assert claim


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["connect", "quarantine", "normalization", "floor_promotion", "capture_noop"])
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_receipt_between_operation_commit_and_readback_blocks_success(monkeypatch, operation, ambiguous):
    db = active_db()
    binding(db)
    snap = claim = obs = proof = None
    if operation == "connect":
        _, obs, proof = await oauth_proof(db)
    elif operation == "quarantine":
        snap, claim = await started(db)
    elif operation == "normalization":
        db.collection("contractors").document("contractor").update({
            "jobber_connected": DELETE_FIELD, "jobber_generation": DELETE_FIELD,
            "jobber_lifecycle_epoch": DELETE_FIELD})
    elif operation == "floor_promotion":
        db.collection("contractors").document("contractor").update({"jobber_token_envelope_required": False})
    original_factory = db.jobber_transaction_factory
    fired = False
    def factory(**kwargs):
        txn = original_factory(**kwargs)
        commit = txn._commit
        def commit_with_receipt():
            nonlocal fired
            result = commit()
            if not fired:
                fired = True
                admit(db)
                if ambiguous:
                    raise TimeoutError("fictional lost acknowledgement")
            return result
        txn._commit = commit_with_receipt
        return txn
    monkeypatch.setattr(db, "jobber_transaction_factory", factory)
    if operation == "connect":
        with pytest.raises(IntegrationTokenCASConflict):
            await complete(db, obs, proof)
    elif operation == "quarantine":
        assert await mutations.quarantine_provider_reauth_cas(contractor_id="contractor", provider="jobber", claim_id=claim,
            observed_generation=snap["generation"], observed_lifecycle_epoch=snap["lifecycle_epoch"],
            observed_access_raw=snap["access_token_raw"], observed_refresh_raw=snap["refresh_token_raw"], db=db) is False
    elif operation in ("normalization", "floor_promotion"):
        assert await mutations.load_durable_provider_snapshot("contractor", "jobber", db=db) is None
    else:
        with pytest.raises(IntegrationTokenCASConflict):
            await mutations.update_jobber_lead_capture_cas(contractor_id="contractor", enabled=True, db=db)
    assert fired
    assert await mutations.load_durable_provider_snapshot("contractor", "jobber", db=db) is None
    assert (await mutations.load_jobber_connection_status("contractor", db=db)).connected is False


@pytest.mark.asyncio
async def test_service_401_refresh_retry_receipt_at_final_start_fence_blocks_second_http(monkeypatch):
    db = active_db()
    binding(db)
    db.collection("contractors").document("contractor").update({"jobber_token_expires_at": 9999999999.0})
    patch_database(monkeypatch, db)
    actual_start = mutations.transition_provider_operation_intent_to_started_cas
    business_starts = 0
    business_http = 0
    refresh_http = 0
    async def start_with_receipt(**kwargs):
        nonlocal business_starts
        if kwargs.get("kind") == "business":
            business_starts += 1
            if business_starts == 2:
                admit(db)
        return await actual_start(**kwargs)
    async def offline_business(*args, **kwargs):
        nonlocal business_http
        business_http += 1
        if business_http != 1:
            pytest.fail("Second business HTTP must be fenced")
        raise jobber.JobberAuthError("fictional 401")
    class OfflineRefreshHTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, **kwargs):
            nonlocal refresh_http
            assert url == jobber.JOBBER_TOKEN_URL
            refresh_http += 1
            return SimpleNamespace(status_code=200, json=lambda: {
                "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})
    monkeypatch.setattr(mutations, "transition_provider_operation_intent_to_started_cas", start_with_receipt)
    monkeypatch.setattr(jobber, "_graphql_request", offline_business)
    monkeypatch.setattr(jobber.httpx, "AsyncClient", OfflineRefreshHTTP)
    assert await jobber._graphql_request_with_refresh({"contractor_id": "contractor"}, "query { account { id } }") is None
    assert (business_http, refresh_http, business_starts) == (1, 1, 2)
    assert await mutations.load_durable_provider_snapshot("contractor", "jobber", db=db) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [True, None, -1, 0, float("nan"), float("inf"), "100"])
async def test_malformed_stored_expiry_does_not_block_normal_tombstone_cleanup(monkeypatch, bad):
    db = active_db()
    original = binding(db)
    db.collection("contractors").document("contractor").update({"jobber_token_expires_at": bad})
    forbid_http(monkeypatch)
    result = await mutations.disconnect_provider_envelope_cas(
        contractor_id="contractor", provider="jobber", db=db)
    assert result.local_only is False
    assert result.credential_deletion == "executed"
    assert result.generation == original["jobber_generation"] + 1
    final = data(db)
    assert mutations.is_durable_provider_tombstone(final, "jobber", "contractor")
    assert "jobber_token_expires_at" not in final
    assert final["jobber_token_envelope_required"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("field,bad", [
    ("contractor_id", None), ("contractor_id", " padded"),
    ("new_access_token", {"raw": "invalid"}), ("new_refresh_token", False),
    ("claim_id", None), ("claim_id", 42), ("observed_generation", None),
    ("observed_generation", True), ("observed_lifecycle_epoch", -1),
    ("expires_in", True), ("extra_updates", {"unapproved": True}),
])
async def test_public_refresh_validation_precedes_all_datastore_access(monkeypatch, field, bad):
    calls = []
    def forbidden_store():
        calls.append(True)
        raise AssertionError("Invalid public input reached datastore factory")
    monkeypatch.setattr(mutations, "get_firestore_client", forbidden_store)
    arguments = dict(contractor_id="contractor", provider="jobber", new_access_token="new-access",
        new_refresh_token="new-refresh", observed_generation=0, observed_lifecycle_epoch=0,
        observed_access_raw="old-access", observed_refresh_raw="old-refresh", claim_id="claim_valid_123456")
    arguments[field] = bad
    from app.services.integration_tokens import IntegrationTokenEnvelopeError
    with pytest.raises(IntegrationTokenEnvelopeError):
        await mutations.persist_refreshed_tokens_cas(**arguments)
    assert calls == []


@pytest.mark.asyncio
async def test_missing_client_configuration_and_poison_ids_never_open_datastore(monkeypatch, jobber_settings):
    calls = []
    def forbidden_store():
        calls.append(True)
        raise AssertionError("Rejected request opened datastore")
    monkeypatch.setattr(mutations, "get_firestore_client", forbidden_store)
    monkeypatch.setattr(jobber_settings, "jobber_client_secret", "")
    assert await jobber.refresh_access_token({"contractor_id": "contractor"}, force=True) is None
    class PoisonString(str):
        def strip(self, *args): raise AssertionError("Poison ID method invoked")
    for bad in (None, " padded", PoisonString("contractor")):
        assert await jobber._read_jobber_tokens(bad) is None
        assert await mutations.load_durable_provider_snapshot(bad, "jobber") is None
    assert calls == []


@pytest.mark.asyncio
async def test_unavailable_database_loaders_fail_closed(monkeypatch):
    monkeypatch.setattr(mutations, "get_firestore_client", lambda: None)
    assert await mutations.load_durable_provider_snapshot("contractor", "jobber") is None
    outcome, _ = await mutations.check_and_recover_expired_intent_preflight_cas(contractor_id="contractor", provider="jobber")
    assert outcome == "blocked"


@pytest.mark.asyncio
async def test_live_refresh_contention_retains_public_lease_error():
    db = active_db()
    binding(db)
    snapshot, _ = await reserved(db)
    with pytest.raises(mutations.IntegrationTokenLeaseError):
        await mutations.acquire_provider_operation_intent_cas(contractor_id="contractor", provider="jobber", kind="refresh",
            observed_generation=snapshot["generation"], observed_lifecycle_epoch=snapshot["lifecycle_epoch"],
            observed_access_raw=snapshot["access_token_raw"], observed_refresh_raw=snapshot["refresh_token_raw"],
            observed_jobber_context=snapshot["jobber_context"], db=db)


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_after_commit", [False, True])
async def test_expired_started_acquisition_commits_quarantine_without_new_claim(monkeypatch, receipt_after_commit):
    import datetime as dt
    db = active_db()
    original = binding(db)
    snapshot, _ = await started(db)
    db.set_server_time(db.get_server_time() + dt.timedelta(seconds=61))
    if receipt_after_commit:
        factory = db.jobber_transaction_factory
        fired = False
        def inject(**kwargs):
            txn = factory(**kwargs)
            commit = txn._commit
            def committed():
                nonlocal fired
                had_writes = bool(txn.writes)
                result = commit()
                if had_writes and not fired:
                    fired = True
                    admit(db)
                return result
            txn._commit = committed
            return txn
        monkeypatch.setattr(db, "jobber_transaction_factory", inject)
    with pytest.raises(IntegrationTokenCASConflict):
        await mutations.acquire_provider_operation_intent_cas(contractor_id="contractor", provider="jobber", kind="refresh",
            observed_generation=snapshot["generation"], observed_lifecycle_epoch=snapshot["lifecycle_epoch"],
            observed_access_raw=snapshot["access_token_raw"], observed_refresh_raw=snapshot["refresh_token_raw"],
            observed_jobber_context=snapshot["jobber_context"], db=db)
    final = data(db)
    assert final["jobber_reauthorization_required"] is True
    assert final["jobber_refresh_outcome_unknown"] is True
    assert "jobber_operation_intent_id" not in final and "jobber_refresh_claim_id" not in final
    assert final["jobber_access_token"] == original["jobber_access_token"]
    assert final["jobber_refresh_token"] == original["jobber_refresh_token"]
    assert await mutations.load_durable_provider_snapshot("contractor", "jobber", db=db) is None


@pytest.mark.asyncio
async def test_normal_disconnect_retains_account_diagnostic_and_repeat_status(monkeypatch):
    db = active_db(); original = binding(db)
    forbid_http(monkeypatch)
    first = await mutations.disconnect_provider_envelope_cas(contractor_id="contractor", provider="jobber", db=db)
    assert not first.local_only
    assert data(db)["jobber_account_id"] == original["jobber_account_id"]
    assert not admission.ENFORCEMENT_FIELDS.intersection(data(db))
    status = await mutations.load_jobber_connection_status("contractor", db=db)
    assert status.connection_status == "disconnected"
    assert status.connected is False and status.lead_capture_enabled is False
    second = await mutations.disconnect_provider_envelope_cas(contractor_id="contractor", provider="jobber", db=db)
    assert not second.local_only
    assert second.generation == first.generation and second.audit_id == first.audit_id
    assert data(db)["jobber_account_id"] == original["jobber_account_id"]
    assert list(db.collection("jobber_local_removal_audit").stream()) == []


@pytest.mark.asyncio
async def test_active_enforcement_tokenless_dirty_record_has_local_cleanup(monkeypatch):
    db = active_db(); original = binding(db)
    dirty = {key: value for key, value in original.items()
             if key not in admission.BINDING_KEYS | {"jobber_access_token", "jobber_refresh_token"}}
    dirty["active"] = False
    db.collection("contractors").document("contractor").set(dirty)
    forbid_http(monkeypatch)
    result = await mutations.disconnect_and_revoke_provider_orchestration(contractor_id="contractor", provider="jobber", db=db)
    assert result["revocation_status"] == "not_attempted_local_only"
    assert result["provider_revocation"]["attempted"] is False
    assert result["generation"] == original["jobber_generation"] + 1
    assert mutations.is_durable_provider_tombstone(data(db), "jobber", "contractor")
    assert not admission.BINDING_KEYS.intersection(data(db))
    assert data(db)["jobber_token_envelope_required"] is True
    assert list(db.collection("integration_revocation_outbox").stream()) == []


def test_legacy_transaction_retains_stable_document_identity_for_transient_wrappers():
    import datetime
    import gc
    import weakref
    from app.db.jobber_disconnect_transactions import Deadline, read_document

    class Document:
        def __init__(self, identifier, value):
            self.id, self.value, self.reads = identifier, value, 0
        def get(self, transaction=None):
            self.reads += 1
            value = self.value
            return SimpleNamespace(id=self.id, exists=True,
                read_time=datetime.datetime(2026, 10, 11, tzinfo=datetime.UTC),
                to_dict=lambda: {"value": value})

    class Reference:
        def __init__(self, target):
            self.real_ref, self.id = target, target.id
        def get(self, transaction=None):
            return self.real_ref.get(transaction=transaction)

    class Inner:
        _id = b"fixture-transaction"
        def __init__(self):
            self.writes = []
        def update(self, target, updates):
            self.writes.append((target, updates))

    inner = Inner()
    transaction = BoundedLegacyTestTransaction(SimpleNamespace(transaction=lambda: inner),
        deadline=Deadline(), read_only=False)
    first, second = Document("audit", "audit-payload"), Document("outbox", "outbox-payload")
    first_ref = Reference(first)
    retained_ref = weakref.ref(first_ref)
    snapshot = read_document(first_ref, transaction)
    del first_ref
    gc.collect()
    assert retained_ref() is not None
    assert read_document(Reference(second), transaction).to_dict() == {"value": "outbox-payload"}
    assert read_document(Reference(first), transaction) is snapshot
    assert first.reads == second.reads == 1
    wrong_identity = Reference(first)
    wrong_identity.id = "outbox"
    with pytest.raises(ValueError, match="Server document identity mismatch"):
        read_document(wrong_identity, transaction)
    assert set(transaction._reads) == {id(first), id(second)}
    transaction.update(Reference(first), {"value": "replacement"})
    transaction.update(Reference(first), {"value": "replacement-again"})
    transaction.update(Reference(second), {"value": "outbox-update"})
    assert set(transaction._touched) == {id(first), id(second)}
    assert transaction._touched[id(first)][1]["value"] == "audit-payload"
    assert [target for target, _ in inner.writes] == [first, first, second]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connected", "disconnect_pending", "reauthorization_required", "disconnected"])
@pytest.mark.parametrize("stamp,expected", [(456, 456.0), (456.5, 456.5), (True, None),
    (False, None), (None, None), (0, None), (-1.0, None), (float("nan"), None),
    (float("inf"), None), ("456", None), (10 ** 400, None)])
async def test_capture_update_history_is_safe_and_independent_of_connection_status(phase, stamp, expected):
    from app.api import admin
    from app.services.jobber_disconnect_recovery import run_jobber_disconnect_recovery_once
    db = active_db(); binding(db)
    db.collection("contractors").document("contractor").update({
        "jobber_lead_capture_updated_at": stamp, "jobber_connected_at": 123.0})
    if phase in ("disconnect_pending", "reauthorization_required"):
        admit(db)
        if phase == "reauthorization_required":
            await run_jobber_disconnect_recovery_once(db=db)
    elif phase == "disconnected":
        await mutations.disconnect_provider_envelope_cas(contractor_id="contractor", provider="jobber", db=db)
    snapshot = await mutations.load_jobber_connection_status("contractor", db=db)
    assert snapshot.connection_status == phase
    assert snapshot.connected is (phase == "connected")
    assert snapshot.lead_capture_enabled is (phase == "connected")
    assert snapshot.connected_at == (123.0 if phase == "connected" else None)
    assert snapshot.lead_capture_updated_at == expected
    summary = admin._jobber_summary(snapshot)
    assert summary["lead_capture_updated_at"] == expected
    assert summary["connection_status"] == phase
    assert set(summary) == {"connection_status", "connected", "connected_at", "lead_capture_enabled", "lead_capture_updated_at"}


async def cancellation_owner(db):
    binding(db)
    owner = await mutations.disconnect_provider_envelope_cas(
        contractor_id="contractor", provider="jobber", candidate_claim_id="original_dispatch_owner", db=db)
    assert owner.claim_id == "original_dispatch_owner" and owner.access_token_for_revocation
    admit(db)
    return owner


def cancellation_pair(db, owner):
    from app.db.integration_lifecycle_audit import (
        AUDIT_COLLECTION, REVOCATION_OUTBOX_COLLECTION, validate_disconnect_lifecycle_pair,
    )
    a = db.collection(AUDIT_COLLECTION).document(owner.audit_id).get().to_dict()
    o = db.collection(REVOCATION_OUTBOX_COLLECTION).document(owner.outbox_id).get().to_dict()
    return validate_disconnect_lifecycle_pair(a, o, expected_contractor_id=owner.contractor_id,
        expected_provider="jobber", expected_generation=owner.generation,
        expected_lifecycle_epoch=owner.lifecycle_epoch, expected_audit_id=owner.audit_id,
        expected_outbox_id=owner.outbox_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_ack", [False, True])
async def test_receipt_before_dispatch_cancels_and_finalizes_zero_http_with_stable_repeat(monkeypatch, lost_ack):
    db = active_db(); binding(db)
    forbid_http(monkeypatch)
    real_admit = admission.admit_revocation
    owners = []
    async def receipt_before_dispatch(result, **kwargs):
        owners.append(result)
        admit(db)
        allowed = await real_admit(result, **kwargs)
        db.commit_then_raise = lost_ack
        return allowed
    monkeypatch.setattr(admission, "admit_revocation", receipt_before_dispatch)
    first = await mutations.disconnect_and_revoke_provider_orchestration(contractor_id="contractor", provider="jobber", db=db)
    assert len(owners) == 1
    a, o = cancellation_pair(db, owners[0])
    assert a["revocation_status"] == o["status"] == "not_attempted_unavailable_token"
    assert o["claim_id"] is None and o["audit_finalized"] is True
    assert a["revocation_completed_at"] == o["updated_at"] == o["audit_finalized_at"]
    assert first["provider_revocation"] == {"status": o["status"], "attempted": False, "attempted_by_this_request": False}
    assert first["audit_finalization"] == {"status": "finalized", "finalized": True, "attempted_by_this_request": True}
    before, version = copy.deepcopy(db._data), db.version
    second = await mutations.disconnect_and_revoke_provider_orchestration(contractor_id="contractor", provider="jobber", db=db)
    assert second["audit_id"] == first["audit_id"] and second["generation"] == first["generation"]
    assert second["provider_revocation"] == first["provider_revocation"]
    assert second["credential_deletion"]["attempted_by_this_request"] is False
    assert second["audit_finalization"] == {"status": "finalized", "finalized": True, "attempted_by_this_request": False}
    assert db._data == before and db.version == version


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_zero_http_cancellation_dropped_writes_never_return_success(ambiguous):
    db = active_db(); owner = await cancellation_owner(db)
    before = copy.deepcopy(db._data)
    def drop(txn):
        assert len(txn.writes) == 2
        txn.writes.clear()
        if ambiguous: raise RuntimeError("fictional missing acknowledgement without commit")
    db.before_commit = drop
    with pytest.raises(IntegrationTokenCASConflict):
        await admission.cancel_denied_revocation(owner, db=db)
    assert db._data == before
    assert cancellation_pair(db, owner)[1]["status"] == "provider_request_started"


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", [
    "claim", "generation", "epoch", "floor", "disconnected_at", "capture", "credential",
    "source", "audit_extra", "pair_disposition", "outbox_created", "control_missing",
    "control_malformed", "barrier_malformed", "barrier_beyond_control", "barrier_missing",
    "app_changed", "replacement",
])
async def test_cancellation_rejects_hostile_records_without_writes(corruption):
    from app.db.integration_lifecycle_audit import AUDIT_COLLECTION, REVOCATION_OUTBOX_COLLECTION
    from app.db.jobber_disconnect_receipts import BARRIERS_COLLECTION, compute_account_barrier_key
    from app.services.integration_tokens import IntegrationTokenError
    db = active_db(); owner = await cancellation_owner(db)
    c = db.collection("contractors").document("contractor")
    a = db.collection(AUDIT_COLLECTION).document(owner.audit_id)
    o = db.collection(REVOCATION_OUTBOX_COLLECTION).document(owner.outbox_id)
    control = db.collection("jobber_disconnect_control").document("current")
    barrier = db.collection(BARRIERS_COLLECTION).document(compute_account_barrier_key("app", "account"))
    if corruption == "claim": o.update({"claim_id": "different_owner"})
    elif corruption == "generation": c.update({"jobber_generation": owner.generation + 1})
    elif corruption == "epoch": c.update({"jobber_lifecycle_epoch": owner.lifecycle_epoch + 1})
    elif corruption == "floor": c.update({"jobber_token_envelope_required": False})
    elif corruption == "disconnected_at": c.update({"jobber_disconnected_at": owner.expected_disconnected_at + 1.0})
    elif corruption == "capture": c.update({"jobber_lead_capture_enabled": True})
    elif corruption == "credential": c.update({"jobber_refresh_token": "fictional-stale"})
    elif corruption == "source": c.update({"jobber_account_id": "other"})
    elif corruption == "audit_extra": a.update({"unexpected": "payload-safe"})
    elif corruption == "pair_disposition": a.update({"credential_deletion_disposition": "legacy_reconciled"})
    elif corruption == "outbox_created": o.update({"created_at": owner.created_at + 1.0})
    elif corruption == "control_missing": control.delete()
    elif corruption == "control_malformed": control.update({"receipt_counter": True})
    elif corruption == "barrier_malformed": barrier.update({"barrier": True})
    elif corruption == "barrier_beyond_control": barrier.update({"barrier": 2})
    elif corruption == "barrier_missing": barrier.delete()
    elif corruption == "app_changed": control.update({"expected_app_id": "different-app"})
    elif corruption == "replacement": binding(db, accepted=1)
    before, version = copy.deepcopy(db._data), db.version
    with pytest.raises(IntegrationTokenError):
        await admission.cancel_denied_revocation(owner, db=db)
    assert db._data == before and db.version == version


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("claim_id", None), ("access_token_for_revocation", None), ("jobber_context", None),
    ("claim_id", "wrong_owner"), ("created_at", 1.0), ("updated_at", 1.0),
])
async def test_cancellation_requires_original_token_owner_and_context(field, value):
    db = active_db(); owner = await cancellation_owner(db)
    before = copy.deepcopy(db._data)
    with pytest.raises(IntegrationTokenCASConflict):
        await admission.cancel_denied_revocation(replace(owner, **{field: value}), db=db)
    assert db._data == before


@pytest.mark.asyncio
async def test_false_dispatch_admission_is_not_itself_proof_of_cancellation(monkeypatch):
    db = active_db(); binding(db)
    forbid_http(monkeypatch)
    async def denied(*args, **kwargs): return False
    monkeypatch.setattr(admission, "admit_revocation", denied)
    with pytest.raises(IntegrationTokenCASConflict):
        await mutations.disconnect_and_revoke_provider_orchestration(contractor_id="contractor", provider="jobber", db=db)
    records = [v for k, v in db._data.items() if k.startswith("integration_revocation_outbox/")]
    assert len(records) == 1 and records[0]["status"] == "provider_request_started"
    assert records[0]["claim_id"] and records[0]["audit_finalized"] is False


@pytest.mark.asyncio
async def test_ownerless_historical_started_is_never_relabelled_zero_http(monkeypatch):
    db = active_db(); owner = await cancellation_owner(db)
    forbid_http(monkeypatch)
    before = copy.deepcopy(db._data)
    with pytest.raises(IntegrationTokenCASConflict, match="non-terminal"):
        await mutations.disconnect_and_revoke_provider_orchestration(contractor_id="contractor", provider="jobber", db=db)
    assert db._data == before
    assert cancellation_pair(db, owner)[1]["claim_id"] == owner.claim_id


@pytest.mark.asyncio
async def test_cancellation_commit_conflict_preserves_new_binding():
    db = active_db(); owner = await cancellation_owner(db)
    replacement = {}
    def reconnect(txn):
        assert len(txn.writes) == 2
        replacement.update(binding(db, accepted=1))
    db.before_commit = reconnect
    with pytest.raises(IntegrationTokenCASConflict):
        await admission.cancel_denied_revocation(owner, db=db)
    assert data(db) == replacement
    assert cancellation_pair(db, owner)[1]["status"] == "provider_request_started"


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
@pytest.mark.parametrize("mutation", ["replacement", "control", "barrier", "pair"])
async def test_cancellation_normal_and_ambiguous_readbacks_recheck_every_authority(monkeypatch, ambiguous, mutation):
    from app.db.integration_lifecycle_audit import REVOCATION_OUTBOX_COLLECTION
    from app.db.jobber_disconnect_receipts import BARRIERS_COLLECTION, compute_account_barrier_key
    from app.services.integration_tokens import IntegrationTokenError
    db = active_db(); owner = await cancellation_owner(db)
    original_factory = db.jobber_transaction_factory
    changed = []
    def factory(**kwargs):
        txn = original_factory(**kwargs)
        commit = txn._commit
        def altered_commit():
            writes = bool(txn.writes)
            result = commit()
            if writes and not changed:
                changed.append(True)
                if mutation == "replacement": binding(db, accepted=1)
                elif mutation == "control": db.collection("jobber_disconnect_control").document("current").update({"receipt_counter": True})
                elif mutation == "barrier":
                    db.collection(BARRIERS_COLLECTION).document(compute_account_barrier_key("app", "account")).delete()
                else: db.collection(REVOCATION_OUTBOX_COLLECTION).document(owner.outbox_id).update({"updated_at": owner.created_at + 1.0})
                if ambiguous: raise RuntimeError("fictional lost commit acknowledgement")
            return result
        txn._commit = altered_commit
        return txn
    monkeypatch.setattr(db, "jobber_transaction_factory", factory)
    with pytest.raises((IntegrationTokenError, ValueError)):
        await admission.cancel_denied_revocation(owner, db=db)
    assert changed == [True]
    if mutation == "replacement": assert data(db)["jobber_connected"] is True


@pytest.mark.asyncio
async def test_preactivation_dispatch_cancelled_by_valid_control_activation():
    db = FakeFirestoreDb()
    db.collection("contractors").document("contractor").set({
        "active": True, "jobber_connected": True, "jobber_generation": 2, "jobber_lifecycle_epoch": 3,
        "jobber_access_token": "fictional-access", "jobber_refresh_token": "fictional-refresh",
        "jobber_account_id": "account", "jobber_lead_capture_enabled": True,
    })
    owner = await mutations.disconnect_provider_envelope_cas(contractor_id="contractor", provider="jobber", db=db)
    assert owner.jobber_context.mode == "preactivation"
    control = active_db().collection("jobber_disconnect_control").document("current").get().to_dict()
    db.collection("jobber_disconnect_control").document("current").set(control)
    assert await admission.admit_revocation(owner, db=db) is False
    _, result = await admission.cancel_denied_revocation(owner, db=db)
    assert result["status"] == "not_attempted_unavailable_token" and result["audit_finalized"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["jobber", "google_calendar"])
async def test_unchanged_admitted_disconnect_has_one_provider_attempt(provider):
    db = active_db()
    if provider == "jobber": binding(db)
    else:
        db.collection("contractors").document("contractor").set({
            "active": True, "google_calendar_connected": True, "google_calendar_generation": 2,
            "google_calendar_lifecycle_epoch": 3, "google_calendar_access_token": "fictional-access",
            "google_calendar_refresh_token": "fictional-refresh",
        })
    class HTTP:
        calls = 0
        async def post(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(status_code=200, json=lambda: {"data": {"appDisconnect": {"userErrors": []}}})
    http = HTTP()
    first = await mutations.disconnect_and_revoke_provider_orchestration(
        contractor_id="contractor", provider=provider, db=db, http_client=http)
    second = await mutations.disconnect_and_revoke_provider_orchestration(
        contractor_id="contractor", provider=provider, db=db, http_client=http)
    assert http.calls == 1
    assert first["revocation_status"] == second["revocation_status"] == "provider_confirmed"
    assert first["provider_revocation"]["attempted"] is True
    assert second["provider_revocation"]["attempted"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["pair", "replacement", "authority"])
async def test_orchestration_rechecks_cancelled_pair_after_cas_returns(monkeypatch, mutation):
    from app.db.integration_lifecycle_audit import AUDIT_COLLECTION
    db = active_db(); binding(db)
    forbid_http(monkeypatch)
    real_admit, real_cancel = admission.admit_revocation, admission.cancel_denied_revocation
    async def deny(owner, **kwargs):
        admit(db)
        return await real_admit(owner, **kwargs)
    async def cancel_then_replace(owner, **kwargs):
        result = await real_cancel(owner, **kwargs)
        if mutation == "pair":
            # Still a valid canonical pair, but no longer the verified pair.
            db.collection(AUDIT_COLLECTION).document(owner.audit_id).update({"actor": "system_reconciliation"})
        elif mutation == "replacement": binding(db, accepted=1)
        else: db.collection("jobber_disconnect_control").document("current").delete()
        return result
    monkeypatch.setattr(admission, "admit_revocation", deny)
    monkeypatch.setattr(admission, "cancel_denied_revocation", cancel_then_replace)
    with pytest.raises(IntegrationTokenCASConflict):
        await mutations.disconnect_and_revoke_provider_orchestration(contractor_id="contractor", provider="jobber", db=db)
    if mutation == "replacement": assert data(db)["jobber_connected"] is True
