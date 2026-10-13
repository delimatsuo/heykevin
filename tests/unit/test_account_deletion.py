"""Irreversible account operations: deletion and Twilio number release.

The Phase 0 side-effect matrix classifies app/api/contractors.py as
"irreversible" and requires confirmation, idempotency, partial-failure
handling, and audit for deletion and number release. These tests pin that
contract:

- The ACCOUNT_DELETE / TWILIO_NUMBER_RELEASE gates allow an authenticated
  owner's own request without any per-contractor flag (account deletion is an
  App Store requirement — it must work for every user), while still refusing
  unconfirmed or key-less invocations.
- The endpoints report failure as HTTP 5xx, never 200-with-an-error-body.
  A 200 on failure is what strands users: iOS clears local state while the
  account stays active and the number keeps billing.
- release_twilio_number never *silently* succeeds: a number missing from the
  Twilio account is recorded as an anomaly, and a Twilio failure propagates
  without touching the profile so a retry can heal.
"""

import os
import time

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15550000000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+15550000001")

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client as FirestoreClient
from google.cloud.firestore_v1.document import DocumentReference
from google.protobuf.timestamp_pb2 import Timestamp

from app.api import contractors as contractors_api
from app.db import contractors as contractors_db
from app.services.gated_actions import (
    ActionKey,
    GateContext,
    GateDecision,
    GateReason,
    check_gated_action,
)


def _owner_request(contractor_id: str = "c1"):
    return SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id=contractor_id))


def _contractor(**overrides):
    doc = {
        "contractor_id": "c1",
        "twilio_number": "+15559999999",
        "active": True,
        # Deliberately no gated_actions / automation_approvals: 0 of 113
        # production contractors have them, and deletion must still work.
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# Gate policy layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action", [ActionKey.ACCOUNT_DELETE, ActionKey.TWILIO_NUMBER_RELEASE]
)
def test_owner_confirmed_request_is_allowed_without_contractor_flag(action):
    """An authenticated owner's own tap must pass with no gated_actions set."""
    decision = check_gated_action(
        _contractor(),
        action,
        GateContext(
            source="ios",
            actor="owner",
            owner_confirmed=True,
            idempotency_key="c1:test",
            environment="production",
        ),
    )
    assert decision.allowed, decision.reason


@pytest.mark.parametrize(
    "action", [ActionKey.ACCOUNT_DELETE, ActionKey.TWILIO_NUMBER_RELEASE]
)
def test_unconfirmed_request_is_refused_for_owner_confirmation(action):
    """Without owner confirmation the gate refuses — for that reason, not a flag."""
    decision = check_gated_action(
        _contractor(),
        action,
        GateContext(
            source="ios",
            actor="owner",
            owner_confirmed=False,
            idempotency_key="c1:test",
            environment="production",
        ),
    )
    assert not decision.allowed
    assert decision.reason == GateReason.OWNER_CONFIRMATION_REQUIRED


@pytest.mark.parametrize(
    "action", [ActionKey.ACCOUNT_DELETE, ActionKey.TWILIO_NUMBER_RELEASE]
)
def test_missing_idempotency_key_is_refused(action):
    decision = check_gated_action(
        _contractor(),
        action,
        GateContext(
            source="ios",
            actor="owner",
            owner_confirmed=True,
            idempotency_key="",
            environment="production",
        ),
    )
    assert not decision.allowed
    assert decision.reason == GateReason.IDEMPOTENCY_REQUIRED


# ---------------------------------------------------------------------------
# Endpoint layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_contractor_success_returns_ok(monkeypatch):
    calls = []

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_deactivate(cid, **kwargs):
        calls.append(cid)
        return True

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)

    result = await contractors_api.api_delete_contractor("c1", _owner_request())

    assert result == {"status": "ok"}
    assert calls == ["c1"]


@pytest.mark.asyncio
async def test_delete_contractor_failure_returns_http_500(monkeypatch):
    """A failed deletion must be a 5xx, never 200 {"status": "error"}."""

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_deactivate(cid, **kwargs):
        raise RuntimeError("twilio down")

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor("c1", _owner_request())
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_delete_contractor_refused_by_gate_makes_no_changes(monkeypatch):
    """If the gate refuses, the endpoint returns 403 and touches nothing."""
    calls = []

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_deactivate(cid, **kwargs):
        calls.append(cid)
        return True

    def refusing_gate(contractor, action, context):
        return GateDecision(
            allowed=False,
            action=action,
            reason=GateReason.OWNER_CONFIRMATION_REQUIRED,
            message="Owner confirmation is required for this action.",
        )

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)
    monkeypatch.setattr(contractors_api, "check_gated_action", refusing_gate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor("c1", _owner_request())
    assert exc.value.status_code == 403
    assert calls == []


@pytest.mark.asyncio
async def test_release_number_refused_by_gate_makes_no_changes(monkeypatch):
    """If the gate refuses, the endpoint returns 403 and releases nothing."""
    calls = []

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_release(cid):
        calls.append(cid)
        return True

    def refusing_gate(contractor, action, context):
        return GateDecision(
            allowed=False,
            action=action,
            reason=GateReason.OWNER_CONFIRMATION_REQUIRED,
            message="Owner confirmation is required for this action.",
        )

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "release_twilio_number", fake_release)
    monkeypatch.setattr(contractors_api, "check_gated_action", refusing_gate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_release_number("c1", _owner_request())
    assert exc.value.status_code == 403
    assert calls == []


@pytest.mark.asyncio
async def test_release_number_failure_returns_http_500(monkeypatch):
    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_release(cid):
        raise RuntimeError("twilio down")

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "release_twilio_number", fake_release)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_release_number("c1", _owner_request())
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_release_number_with_no_number_is_idempotent_ok(monkeypatch):
    """No number on file means the desired end state already holds: 200, not error.

    This is what lets a retry after a partial failure heal instead of erroring
    forever.
    """

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid, twilio_number="")

    async def fake_release(cid):
        return False

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "release_twilio_number", fake_release)

    result = await contractors_api.api_release_number("c1", _owner_request())

    assert result["status"] == "ok"
    assert result["released"] is False


@pytest.mark.asyncio
async def test_release_number_false_result_rereads_completed_concurrent_release(monkeypatch):
    """A parallel completion is idempotent success, not a stale conflict."""
    reads = [
        _contractor(contractor_id="c1", twilio_number="+15559999999"),
        _contractor(contractor_id="c1", twilio_number=""),
    ]

    async def fake_get_contractor(_cid):
        return reads.pop(0)

    async def fake_release(_cid):
        return False

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "release_twilio_number", fake_release)

    response = await contractors_api.api_release_number("c1", _owner_request())

    assert response == {"status": "ok", "released": False}
    assert reads == []


@pytest.mark.asyncio
async def test_cross_tenant_delete_is_refused_before_any_work(monkeypatch):
    calls = []

    async def fake_deactivate(cid, **kwargs):
        calls.append(cid)
        return True

    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor(
            "victim", _owner_request(contractor_id="attacker")
        )
    assert exc.value.status_code == 403
    assert calls == []


# ---------------------------------------------------------------------------
# DB layer: release_twilio_number / deactivate_contractor
# ---------------------------------------------------------------------------


class _FakeNumber:
    def __init__(self, log):
        self._log = log

    def delete(self):
        self._log.append("deleted")
        return True


class _FakeNumberUnconfirmed:
    def delete(self):
        return False


class _FakeTwilioClient:
    """Stands in for twilio.rest.Client; behavior injected per-test."""

    lookup_result = None  # set per test
    log = None

    def __init__(self, *args, **kwargs):
        pass

    @property
    def incoming_phone_numbers(self):
        outer = type(self)

        class _Numbers:
            def list(self, **kwargs):
                if isinstance(outer.lookup_result, Exception):
                    raise outer.lookup_result
                return outer.lookup_result

        return _Numbers()


@pytest.fixture
def fake_twilio(monkeypatch):
    """Twilio plus an in-memory stand-in for the release state machine."""
    _FakeTwilioClient.log = []
    _FakeTwilioClient.lookup_result = []
    monkeypatch.setattr("twilio.rest.Client", _FakeTwilioClient)

    async def fake_claim(_cid, *, strict_precondition):
        return contractors_db.StrictNumberReleaseClaim("claim-nonce")

    async def fake_begin(_cid, claim, *, strict_precondition):
        assert claim.state == contractors_db.NUMBER_RELEASE_CLAIM_RESERVED
        return contractors_db.StrictNumberReleaseClaim(
            claim.nonce,
            state=contractors_db.NUMBER_RELEASE_CLAIM_DELETING,
        )

    async def fake_hold(cid, _claim, *, anomaly=None):
        if anomaly is not None:
            await contractors_db.update_contractor(cid, {"number_release_anomaly": anomaly})
        return True

    async def fake_finalize(
        cid,
        _claim,
        release_updates,
        *,
        strict_precondition,
        deactivate_profile,
        deactivation_updates=None,
    ):
        updates = dict(release_updates)
        if deactivate_profile:
            updates.update({"active": False, "deactivated_at": 1})
            updates.update(deactivation_updates or {})
        return await contractors_db.update_contractor(cid, updates)

    monkeypatch.setattr(contractors_db, "claim_strict_number_release", fake_claim)
    monkeypatch.setattr(contractors_db, "begin_strict_number_release_delete", fake_begin)
    monkeypatch.setattr(contractors_db, "hold_strict_number_release", fake_hold)
    monkeypatch.setattr(contractors_db, "finalize_strict_number_release", fake_finalize)
    return _FakeTwilioClient



def _install_in_memory_contractor_store(monkeypatch, state: dict):
    """Give release-lock helpers a transaction-shaped local contractor doc."""
    class _Snapshot:
        exists = True
        id = "c1"

        def to_dict(self):
            return dict(state)

    class _Ref:
        def get(self, transaction=None, retry=None, timeout=None):
            return _Snapshot()

        def update(self, updates):
            state.update(updates)

    ref = _Ref()

    class _Collection:
        def document(self, _contractor_id):
            return ref

    class _FirestoreApi:
        def begin_transaction(self, *, request, metadata, retry, timeout):
            return SimpleNamespace(transaction=b"in-memory-release")

        def commit(self, *, request, metadata, retry, timeout):
            return SimpleNamespace(write_results=(), commit_time=None)

        def rollback(self, *, request, metadata, retry, timeout):
            return SimpleNamespace()

    class _FirestoreClient:
        _database_string = "projects/test/databases/(default)"
        _rpc_metadata = ()
        _firestore_api = _FirestoreApi()

    class _Transaction:
        def __init__(self):
            self._id = None
            self._write_pbs = []
            self._client = _FirestoreClient()
            self.write_results = ()
            self.commit_time = None

        @property
        def in_progress(self):
            return self._id is not None

        def _options_protobuf(self, _retry_id):
            return {}

        def _clean_up(self):
            self._id = None
            self._write_pbs = []

        def update(self, doc_ref, updates):
            doc_ref.update(updates)

        def commit(self, timeout=None):
            return []

    class _Db:
        def collection(self, _collection):
            return _Collection()

        def transaction(self, *args, **kwargs):
            return _Transaction()

    monkeypatch.setattr(contractors_db, "get_firestore_client", _Db)
    monkeypatch.setattr(contractors_db.firestore, "transactional", lambda fn: fn)
    return state


class _InboundSafetySnapshot:
    exists = True

    def __init__(self, data):
        self._data = dict(data)

    def to_dict(self):
        return dict(self._data)


class _BoundedInboundFirestoreApi:
    """Credential-free wire double for a real Firestore Transaction object."""

    def __init__(self, *, commit_error=None):
        self.begin_calls = []
        self.commit_calls = []
        self.rollback_calls = []
        self.commit_error = commit_error

    def begin_transaction(self, *, request, metadata, retry, timeout):
        self.begin_calls.append((request, metadata, retry, timeout))
        return SimpleNamespace(transaction=b"bounded-inbound-safety")

    def commit(self, *, request, metadata, retry, timeout):
        self.commit_calls.append((request, metadata, retry, timeout))
        if self.commit_error is not None:
            raise self.commit_error
        return SimpleNamespace(write_results=(), commit_time=Timestamp())

    def rollback(self, *, request, metadata, retry, timeout):
        self.rollback_calls.append((request, metadata, retry, timeout))
        return SimpleNamespace()


def _real_inbound_safety_client(api):
    client = FirestoreClient(
        project="inbound-safety-test",
        credentials=AnonymousCredentials(),
    )
    # The real SDK transaction object is important: it rejects a document
    # read before begin(), which an in-memory transaction double cannot see.
    client._firestore_api_internal = api
    return client


def _patch_inbound_safety_document_reads(monkeypatch, calls):
    def get(_self, transaction=None, retry=None, timeout=None):
        calls.append((transaction, retry, timeout))
        if transaction is not None:
            assert transaction.in_progress
        return _InboundSafetySnapshot({
            "active": True,
            "twilio_number": "+15559999999",
            "last_inbound_call_at": None,
        })

    monkeypatch.setattr(DocumentReference, "get", get)


@pytest.mark.asyncio
async def test_inbound_evidence_starts_real_transaction_with_bounded_wire_rpcs(monkeypatch):
    api = _BoundedInboundFirestoreApi()
    client = _real_inbound_safety_client(api)
    document_reads = []
    _patch_inbound_safety_document_reads(monkeypatch, document_reads)
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: client)

    assert await contractors_db.record_inbound_call_evidence("c1", 5000.0, rpc_timeout=0.1)

    assert len(document_reads) == 2
    assert document_reads[0][0] is None
    assert document_reads[1][0] is not None
    assert len(api.begin_calls) == 1
    assert len(api.commit_calls) == 1
    assert api.commit_calls[0][0]["transaction"] == b"bounded-inbound-safety"
    assert api.rollback_calls == []
    for _request, _metadata, retry, timeout in [*api.begin_calls, *api.commit_calls]:
        assert retry is None
        assert 0 < timeout <= 0.1
    assert all(retry is None and 0 < timeout <= 0.1 for _transaction, retry, timeout in document_reads)


@pytest.mark.asyncio
async def test_inbound_evidence_does_not_retry_an_uncertain_commit(monkeypatch):
    api = _BoundedInboundFirestoreApi(commit_error=RuntimeError("ambiguous commit"))
    client = _real_inbound_safety_client(api)
    document_reads = []
    _patch_inbound_safety_document_reads(monkeypatch, document_reads)
    monkeypatch.setattr(contractors_db, "get_firestore_client", lambda: client)

    assert not await contractors_db.record_inbound_call_evidence("c1", 5000.0, rpc_timeout=0.1)

    assert len(api.begin_calls) == 1
    assert len(api.commit_calls) == 1
    assert len(api.rollback_calls) == 1
    assert api.rollback_calls[0][0]["transaction"] == b"bounded-inbound-safety"
    for _request, _metadata, retry, timeout in [*api.begin_calls, *api.commit_calls, *api.rollback_calls]:
        assert retry is None
        assert 0 < timeout <= 0.1


@pytest.mark.asyncio
async def test_inbound_evidence_transaction_cancels_reserved_release(monkeypatch):
    state = _install_in_memory_contractor_store(monkeypatch, {
        "active": True,
        "twilio_number": "+15559999999",
        "last_inbound_call_at": None,
    })
    claim = await contractors_db.claim_strict_number_release(
        "c1",
        strict_precondition=lambda candidate: candidate["last_inbound_call_at"] is None,
    )
    assert claim is not None

    recorded = await contractors_db.record_inbound_call_evidence(
        "c1",
        123.0,
        rpc_timeout=0.1,
    )

    assert recorded is True
    assert state["last_inbound_call_at"] == 123.0
    assert state["number_release_claim"]["state"] == contractors_db.NUMBER_RELEASE_CLAIM_CANCELLED


@pytest.mark.asyncio
async def test_release_found_number_clears_profile_and_stamps_release(
    monkeypatch, fake_twilio
):
    updates = []
    fake_twilio.lookup_result = [_FakeNumber(fake_twilio.log)]

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    released = await contractors_db.release_twilio_number("c1")

    assert released is True
    assert fake_twilio.log == ["deleted"]
    assert len(updates) == 1
    cid, fields = updates[0]
    assert cid == "c1"
    assert fields["twilio_number"] == ""
    assert fields.get("number_released_at"), "release must be stamped for audit"


@pytest.mark.asyncio
async def test_release_missing_number_records_anomaly(monkeypatch, fake_twilio):
    """A number we believe we own but Twilio doesn't list is an anomaly, not a
    silent success — it may be a format mismatch on a number that still bills."""
    updates = []
    fake_twilio.lookup_result = []  # Twilio says: no such number

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    released = await contractors_db.release_twilio_number("c1")

    assert released is True
    assert len(updates) == 1
    _cid, fields = updates[0]
    assert fields["twilio_number"] == ""
    assert fields.get("number_release_anomaly"), (
        "a Twilio-side miss must be recorded on the document for reconciliation"
    )


@pytest.mark.asyncio
async def test_strict_release_reports_empty_twilio_lookup_as_unconfirmed(monkeypatch, fake_twilio):
    """The external-worker ledger needs a hold, not legacy best-effort success."""
    updates = []
    fake_twilio.lookup_result = []

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    confirmed = await contractors_db.release_twilio_number("c1", require_confirmed=True)

    assert confirmed is False
    assert updates[0][1]["number_release_anomaly"]["number"] == "+15559999999"
    assert "twilio_number" not in updates[0][1]


@pytest.mark.asyncio
async def test_strict_release_reports_unconfirmed_delete_result(monkeypatch, fake_twilio):
    updates = []
    fake_twilio.lookup_result = [_FakeNumberUnconfirmed()]

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    confirmed = await contractors_db.release_twilio_number("c1", require_confirmed=True)

    assert confirmed is False
    assert updates[0][0] == "c1"
    assert updates[0][1]["number_release_anomaly"]["number"] == "+15559999999"
    assert "twilio_number" not in updates[0][1]


@pytest.mark.asyncio
async def test_strict_release_does_not_delete_when_begin_rejects_safety_change(monkeypatch, fake_twilio):
    """The Firestore phase transition is the only path to Twilio DELETE."""
    fake_twilio.lookup_result = [_FakeNumber(fake_twilio.log)]

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid, last_inbound_call_at=None)

    async def rejected_begin(_cid, _claim, *, strict_precondition):
        return None

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "begin_strict_number_release_delete", rejected_begin)

    confirmed = await contractors_db.release_twilio_number(
        "c1",
        require_confirmed=True,
        strict_precondition=lambda candidate: candidate.get("last_inbound_call_at") is None,
    )

    assert confirmed is False
    assert fake_twilio.log == []


@pytest.mark.asyncio
async def test_reserved_inbound_mutation_is_retained_and_cancels_release(monkeypatch):
    """A live call before deleting both persists its stamp and stops release."""
    state = _install_in_memory_contractor_store(monkeypatch, {
        "active": True,
        "twilio_number": "+15559999999",
        "last_inbound_call_at": None,
    })
    claim = await contractors_db.claim_strict_number_release(
        "c1", strict_precondition=lambda candidate: candidate["last_inbound_call_at"] is None,
    )
    assert claim is not None

    assert await contractors_db.update_contractor("c1", {"last_inbound_call_at": 123})
    assert state["last_inbound_call_at"] == 123
    assert state["number_release_claim"]["state"] == contractors_db.NUMBER_RELEASE_CLAIM_CANCELLED
    assert await contractors_db.begin_strict_number_release_delete(
        "c1",
        claim,
        strict_precondition=lambda candidate: candidate["last_inbound_call_at"] is None,
    ) is None


@pytest.mark.asyncio
async def test_deleting_inbound_mutation_holds_and_prevents_finalization(monkeypatch):
    """A safety event after the boundary leaves the profile intact for review."""
    state = _install_in_memory_contractor_store(monkeypatch, {
        "active": True,
        "twilio_number": "+15559999999",
        "last_inbound_call_at": None,
    })
    predicate = lambda candidate: candidate["last_inbound_call_at"] is None
    claim = await contractors_db.claim_strict_number_release("c1", strict_precondition=predicate)
    deleting = await contractors_db.begin_strict_number_release_delete(
        "c1", claim, strict_precondition=predicate,
    )
    assert deleting is not None

    assert not await contractors_db.update_contractor("c1", {"last_inbound_call_at": 456})
    assert state["last_inbound_call_at"] is None
    assert state["number_release_claim"]["state"] == contractors_db.NUMBER_RELEASE_CLAIM_NEEDS_ATTENTION
    finalized = await contractors_db.finalize_strict_number_release(
        "c1",
        deleting,
        {"twilio_number": "", "number_released_at": 1},
        strict_precondition=predicate,
        deactivate_profile=True,
    )
    assert finalized is False
    assert state["active"] is True
    assert state["twilio_number"] == "+15559999999"


@pytest.mark.asyncio
async def test_direct_entitlement_hold_returns_typed_retry_signal(monkeypatch):
    """A direct paid purchase can distinguish a release hold from invalid input."""
    state = _install_in_memory_contractor_store(monkeypatch, {
        "active": True,
        "twilio_number": "+15559999999",
        "subscription_uuid": "uuid-1",
        "subscription_status": "expired",
    })
    guard = lambda candidate: candidate["active"] is True
    claim = await contractors_db.claim_strict_number_release(
        "c1",
        strict_precondition=guard,
    )
    deleting = await contractors_db.begin_strict_number_release_delete(
        "c1",
        claim,
        strict_precondition=guard,
    )
    assert deleting is not None

    activated = await contractors_db.activate_subscription_entitlement(
        "c1",
        "personal",
        4_102_444_800.0,
        "otx-1",
        "uuid-1",
    )

    assert activated is False
    assert (
        contractors_db.take_subscription_entitlement_activation_outcome()
        is contractors_db.SubscriptionEntitlementActivationOutcome.RELEASE_CONFLICT
    )
    assert state["number_release_claim"]["state"] == contractors_db.NUMBER_RELEASE_CLAIM_NEEDS_ATTENTION
    assert state["number_release_conflict"]["deferred_updates"]["subscription_status"] == "active"


@pytest.mark.asyncio
async def test_deleting_subscription_renewal_is_retained_for_reconciliation(monkeypatch):
    """A post-boundary App Store renewal cannot be discarded behind a 200 webhook."""
    state = _install_in_memory_contractor_store(monkeypatch, {
        "active": True,
        "twilio_number": "+15559999999",
        "subscription_status": "expired",
        "subscription_tier": "none",
        "subscription_expires": 0,
    })
    claim = await contractors_db.claim_strict_number_release(
        "c1", strict_precondition=lambda candidate: candidate["active"] is True,
    )
    deleting = await contractors_db.begin_strict_number_release_delete(
        "c1", claim, strict_precondition=lambda candidate: candidate["active"] is True,
    )
    assert deleting is not None
    renewal = {
        "subscription_status": "active",
        "subscription_tier": "personal",
        "subscription_expires": 4_102_444_800.0,
    }

    assert not await contractors_db.update_contractor("c1", renewal)
    assert state["subscription_status"] == "expired"
    assert state["number_release_claim"]["state"] == contractors_db.NUMBER_RELEASE_CLAIM_NEEDS_ATTENTION
    conflict = state["number_release_conflict"]
    assert isinstance(conflict["at"], int)
    assert conflict["phase"] == contractors_db.NUMBER_RELEASE_CLAIM_DELETING
    assert conflict["kind"] == "profile_mutation"
    assert conflict["fields"] == sorted(renewal)
    assert conflict["deferred_updates"] == renewal

    # A later safety signal must add to the reconciliation record, not erase
    # the entitlement that prompted it.
    assert not await contractors_db.update_contractor("c1", {"last_inbound_call_at": 123})
    conflict = state["number_release_conflict"]
    assert conflict["phase"] == contractors_db.NUMBER_RELEASE_CLAIM_NEEDS_ATTENTION
    assert conflict["fields"] == ["last_inbound_call_at"]
    assert conflict["deferred_updates"] == {
        **renewal,
        "last_inbound_call_at": 123,
    }
    assert conflict["deferred_subscription_events"] == [{
        "at": conflict["deferred_subscription_events"][0]["at"],
        "updates": renewal,
    }]

    # A delayed terminal event cannot overwrite the renewal evidence. The
    # current summary reflects the latest hold, while the ordered event history
    # keeps both observations for reconciliation.
    assert not await contractors_db.update_contractor(
        "c1",
        {"subscription_status": "expired"},
    )
    conflict = state["number_release_conflict"]
    assert conflict["deferred_updates"]["subscription_status"] == "expired"
    events = conflict["deferred_subscription_events"]
    assert [event["updates"]["subscription_status"] for event in events] == [
        "active",
        "expired",
    ]


@pytest.mark.asyncio
async def test_claim_records_expected_number_and_allows_reclaim_after_cancel(monkeypatch):
    state = _install_in_memory_contractor_store(monkeypatch, {
        "active": True,
        "twilio_number": "+15559999999",
    })
    first = await contractors_db.claim_strict_number_release(
        "c1", strict_precondition=lambda candidate: candidate["active"] is True,
    )
    assert first is not None
    assert state["number_release_claim"]["expected_twilio_number"] == "+15559999999"

    assert await contractors_db.update_contractor("c1", {"subscription_status": "active"})
    assert state["number_release_claim"]["state"] == contractors_db.NUMBER_RELEASE_CLAIM_CANCELLED
    second = await contractors_db.claim_strict_number_release(
        "c1", strict_precondition=lambda candidate: candidate["active"] is True,
    )
    assert second is not None
    assert second.nonce != first.nonce


@pytest.mark.asyncio
async def test_stale_reserved_claim_is_reclaimed_without_provider_delete(monkeypatch):
    """A crash before ``deleting`` cannot pin a quiet number indefinitely."""
    state = _install_in_memory_contractor_store(monkeypatch, {
        "active": True,
        "twilio_number": "+15559999999",
        "number_release_claim": {
            "nonce": "abandoned-reservation",
            "state": contractors_db.NUMBER_RELEASE_CLAIM_RESERVED,
            "claimed_at": (
                int(time.time())
                - contractors_db.NUMBER_RELEASE_RESERVATION_TTL_SECONDS
                - 1
            ),
            "expected_twilio_number": "+15559999999",
        },
    })

    replacement = await contractors_db.claim_strict_number_release(
        "c1", strict_precondition=lambda candidate: candidate["active"] is True,
    )

    assert replacement is not None
    assert replacement.nonce != "abandoned-reservation"
    assert state["number_release_claim"]["state"] == contractors_db.NUMBER_RELEASE_CLAIM_RESERVED
    stale = contractors_db.StrictNumberReleaseClaim("abandoned-reservation")
    assert await contractors_db.begin_strict_number_release_delete(
        "c1", stale, strict_precondition=lambda _candidate: True,
    ) is None


@pytest.mark.asyncio
async def test_provider_success_after_deleting_safety_event_holds_profile(monkeypatch):
    """Twilio may succeed after a concurrent call; finalization must not lie."""
    import asyncio
    import threading

    state = _install_in_memory_contractor_store(monkeypatch, {
        "active": True,
        "twilio_number": "+15559999999",
        "last_inbound_call_at": None,
    })
    entered = threading.Event()
    proceed = threading.Event()
    deletes: list[str] = []

    class _BlockingNumber:
        def delete(self):
            entered.set()
            assert proceed.wait(timeout=2)
            deletes.append("delete")
            return True

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        @property
        def incoming_phone_numbers(self):
            class _Numbers:
                def list(self, **_kwargs):
                    return [_BlockingNumber()]
            return _Numbers()

    monkeypatch.setattr("twilio.rest.Client", _Client)
    predicate = lambda candidate: candidate["last_inbound_call_at"] is None
    task = asyncio.create_task(contractors_db.release_twilio_number(
        "c1", require_confirmed=True, strict_precondition=predicate,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not await contractors_db.update_contractor("c1", {"last_inbound_call_at": 999})
        proceed.set()
        assert await task is False
    finally:
        proceed.set()
        if not task.done():
            await task

    assert deletes == ["delete"]
    assert state["number_release_claim"]["state"] == contractors_db.NUMBER_RELEASE_CLAIM_NEEDS_ATTENTION
    assert state["active"] is True
    assert state["twilio_number"] == "+15559999999"


@pytest.mark.asyncio
async def test_two_release_entrypoints_produce_one_provider_delete(monkeypatch):
    """A manual and an embedded path cannot both cross the provider boundary."""
    import asyncio
    import threading

    state = _install_in_memory_contractor_store(monkeypatch, {
        "active": True,
        "twilio_number": "+15559999999",
    })
    entered = threading.Event()
    proceed = threading.Event()
    deletes: list[str] = []

    class _BlockingNumber:
        def delete(self):
            entered.set()
            assert proceed.wait(timeout=2)
            deletes.append("delete")
            return True

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        @property
        def incoming_phone_numbers(self):
            class _Numbers:
                def list(self, **_kwargs):
                    return [_BlockingNumber()]
            return _Numbers()

    monkeypatch.setattr("twilio.rest.Client", _Client)
    first = asyncio.create_task(contractors_db.release_twilio_number("c1"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert await contractors_db.deactivate_contractor("c1") is False
        proceed.set()
        assert await first is True
    finally:
        proceed.set()
        if not first.done():
            await first

    assert deletes == ["delete"]
    assert state["twilio_number"] == ""


@pytest.mark.asyncio
async def test_deleting_claim_blocks_an_automatic_second_attempt(monkeypatch):
    _install_in_memory_contractor_store(monkeypatch, {
        "active": True,
        "twilio_number": "+15559999999",
    })
    claim = await contractors_db.claim_strict_number_release(
        "c1", strict_precondition=lambda _candidate: True,
    )
    deleting = await contractors_db.begin_strict_number_release_delete(
        "c1", claim, strict_precondition=lambda _candidate: True,
    )
    assert deleting is not None

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        @property
        def incoming_phone_numbers(self):
            raise AssertionError("a deleting claim must block a second Twilio lookup")

    monkeypatch.setattr("twilio.rest.Client", _Client)
    assert await contractors_db.release_twilio_number("c1") is False


def test_finalizer_rejects_stale_or_wrong_phase_claim(monkeypatch):
    state = _install_in_memory_contractor_store(monkeypatch, {
        "active": True,
        "twilio_number": "+15559999999",
        "number_release_claim": {"nonce": "live", "state": "reserved"},
    })

    async def run():
        return await contractors_db.finalize_strict_number_release(
            "c1",
            contractors_db.StrictNumberReleaseClaim(
                "other", state=contractors_db.NUMBER_RELEASE_CLAIM_DELETING,
            ),
            {"twilio_number": ""},
            strict_precondition=lambda _candidate: True,
            deactivate_profile=True,
        )

    import asyncio
    assert asyncio.run(run()) is False
    assert state["twilio_number"] == "+15559999999"


@pytest.mark.asyncio
async def test_release_twilio_failure_propagates_without_touching_profile(
    monkeypatch, fake_twilio
):
    updates = []
    fake_twilio.lookup_result = RuntimeError("twilio 5xx")

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    with pytest.raises(RuntimeError):
        await contractors_db.release_twilio_number("c1")
    assert updates[0][1]["number_release_anomaly"]["number"] == "+15559999999"


@pytest.mark.asyncio
async def test_deactivate_asks_release_finalizer_to_stamp_deactivation(monkeypatch):
    calls = []

    async def fake_get(cid):
        return _contractor(contractor_id=cid)

    async def fake_release(cid, **kwargs):
        calls.append((cid, kwargs))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get)
    monkeypatch.setattr(contractors_db, "release_twilio_number", fake_release)

    ok = await contractors_db.deactivate_contractor("c1")

    assert ok is True
    assert calls == [("c1", {
        "require_confirmed": False,
        "deactivate_profile": True,
        "deactivation_updates": {},
    })]


@pytest.mark.asyncio
async def test_strict_deactivation_surfaces_an_unconfirmed_release(monkeypatch):
    """The strict worker preserves account routing until reconciliation."""
    calls = []

    async def fake_get(cid):
        return _contractor(contractor_id=cid)

    async def fake_release(cid, **kwargs):
        calls.append((cid, kwargs))
        assert kwargs["require_confirmed"] is True
        return False

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get)
    monkeypatch.setattr(contractors_db, "release_twilio_number", fake_release)

    confirmed = await contractors_db.deactivate_contractor(
        "c1", require_confirmed_number_release=True,
    )

    assert confirmed is False
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Review-round fixes (PR #192 findings)
# ---------------------------------------------------------------------------

import hashlib
import importlib.util
from pathlib import Path

from twilio.base.exceptions import TwilioRestException

from app.db.contractors import PROTECTED_FIELDS
from app.middleware import auth as auth_middleware
from app.services.gated_actions import GATE_POLICIES, GatePolicy


class _FakeNumberGone:
    def delete(self):
        raise TwilioRestException(404, "/IncomingPhoneNumbers/PNxxx", "not found", 20404, "DELETE")


@pytest.mark.asyncio
async def test_release_tolerates_number_already_gone_at_twilio(monkeypatch, fake_twilio):
    """A 20404 on delete() means another request already released it: success,
    no anomaly — the concurrent-delete race must not surface as a 500."""
    updates = []
    fake_twilio.lookup_result = [_FakeNumberGone()]

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    released = await contractors_db.release_twilio_number("c1")

    assert released is True
    assert len(updates) == 1
    _cid, fields = updates[0]
    assert fields["twilio_number"] == ""
    assert "number_release_anomaly" not in fields


@pytest.mark.asyncio
async def test_release_anomaly_records_which_number(monkeypatch, fake_twilio):
    """The anomaly record must carry the number itself — the doc clears it and
    the log redacts it, so without this reconciliation has no key."""
    updates = []
    fake_twilio.lookup_result = []

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_update(cid, fields):
        updates.append((cid, fields))
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_db, "update_contractor", fake_update)

    await contractors_db.release_twilio_number("c1")

    _cid, fields = updates[0]
    anomaly = fields["number_release_anomaly"]
    assert anomaly["number"] == "+15559999999"
    assert anomaly["at"]


@pytest.mark.asyncio
async def test_delete_missing_contractor_returns_404(monkeypatch):
    """A doc that does not exist is 404 'already gone', never a 403 dead-end."""

    async def fake_get_contractor(cid):
        return None

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)

    admin_request = SimpleNamespace(state=SimpleNamespace(is_admin=True, contractor_id=""))
    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor("gone", admin_request)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_gate_context_reflects_the_real_actor(monkeypatch):
    """Admin-token deletions must not be audited as owner-confirmed iOS taps."""
    contexts = []
    real_check = contractors_api.check_gated_action

    def spying_check(contractor, action, context):
        contexts.append(context)
        return real_check(contractor, action, context)

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_deactivate(cid, **kwargs):
        return True

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)
    monkeypatch.setattr(contractors_api, "check_gated_action", spying_check)

    await contractors_api.api_delete_contractor("c1", _owner_request())
    admin_request = SimpleNamespace(state=SimpleNamespace(is_admin=True, contractor_id=""))
    await contractors_api.api_delete_contractor("c1", admin_request)

    assert contexts[0].actor == "owner"
    assert contexts[0].source == "ios"
    assert contexts[1].actor == "admin"
    assert contexts[1].source == "api"


@pytest.mark.asyncio
async def test_endpoint_preserves_http_exceptions_from_callees(monkeypatch):
    """A deliberate 4xx raised inside the deletion path must not be rewrapped
    as a generic 500."""

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    async def fake_deactivate(cid, **kwargs):
        raise HTTPException(status_code=409, detail="forwarding still live")

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "deactivate_contractor", fake_deactivate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor("c1", _owner_request())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_deactivate_invalidates_token_cache(monkeypatch):
    """A deleted account's token must stop authenticating on this instance."""
    token_hash = hashlib.sha256(b"kv_ct_c1_secret").hexdigest()
    auth_middleware._token_cache[token_hash] = "c1"
    auth_middleware._token_cache["other-hash"] = "c2"

    async def fake_get(cid):
        return _contractor(contractor_id=cid)

    async def fake_release(cid, **kwargs):
        return True

    monkeypatch.setattr(contractors_db, "get_contractor", fake_get)
    monkeypatch.setattr(contractors_db, "release_twilio_number", fake_release)

    try:
        await contractors_db.deactivate_contractor("c1")
        assert token_hash not in auth_middleware._token_cache
        assert auth_middleware._token_cache.get("other-hash") == "c2"
    finally:
        auth_middleware._token_cache.pop("other-hash", None)
        auth_middleware._token_cache.pop(token_hash, None)


@pytest.mark.asyncio
async def test_gate_refusal_detail_carries_the_reason(monkeypatch):
    """Denial payloads must match the estimates endpoints' shape so clients
    can distinguish refusal reasons."""

    async def fake_get_contractor(cid):
        return _contractor(contractor_id=cid)

    def refusing_gate(contractor, action, context):
        return GateDecision(
            allowed=False,
            action=action,
            reason=GateReason.OWNER_CONFIRMATION_REQUIRED,
            message="Owner confirmation is required for this action.",
        )

    monkeypatch.setattr(contractors_api, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(contractors_api, "check_gated_action", refusing_gate)

    with pytest.raises(HTTPException) as exc:
        await contractors_api.api_delete_contractor("c1", _owner_request())
    assert exc.value.detail["reason"] == GateReason.OWNER_CONFIRMATION_REQUIRED.value


def test_new_lifecycle_stamps_are_protected_fields():
    assert {
        "number_released_at", "number_release_anomaly", "number_release_conflict", "deactivated_at",
    } <= set(PROTECTED_FIELDS)


@pytest.mark.parametrize(
    "action", [ActionKey.ACCOUNT_DELETE, ActionKey.TWILIO_NUMBER_RELEASE]
)
def test_gate_policy_shape_is_pinned(action):
    assert GATE_POLICIES[action] == GatePolicy(
        requires_flag=False,
        requires_owner_confirmation=True,
        requires_idempotency=True,
    )


def test_kill_switch_scripts_do_not_offer_flagless_actions():
    """gated_actions flags are never consulted for requires_flag=False actions;
    offering them in the operator writer or counting them in the audit script
    fabricates a kill switch that does not exist."""
    root = Path(__file__).resolve().parents[2]

    spec = importlib.util.spec_from_file_location(
        "set_gated_action_killswitch", root / "scripts" / "set_gated_action.py"
    )
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    assert "account_delete" not in writer.KNOWN_ACTIONS
    assert "twilio_number_release" not in writer.KNOWN_ACTIONS

    spec = importlib.util.spec_from_file_location(
        "phase0_account_audit_killswitch", root / "scripts" / "phase0_account_audit.py"
    )
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    flags = audit.KNOWN_ACTION_KEYS
    assert "account_delete" not in flags
    assert "twilio_number_release" not in flags
