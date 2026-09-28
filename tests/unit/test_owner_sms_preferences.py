"""Unit tests for owner SMS preferences, identity resolution, API readback, and single send boundary."""

import os
import pytest
from pydantic import ValidationError

os.environ.setdefault("TWILIO_ACCOUNT_SID", "test-account-sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+12025550199")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("USER_PHONE", "+12025550123")
os.environ.setdefault("CLOUD_RUN_URL", "https://test.kevinai.app")

from fastapi import HTTPException
from twilio.base.exceptions import TwilioRestException

from app.api.contractors import ContractorUpdate, _redact_contractor, api_update_contractor
from app.db import contractors as contractor_db
from app.db import owner_sms as owner_sms_db
from app.services import owner_sms as owner_sms_service


@pytest.fixture(autouse=True)
def fail_fast_on_real_firestore(monkeypatch):
    """Ensure no test ever accidentally touches a real Firestore instance."""
    def _fail(*args, **kwargs):
        raise RuntimeError("Unstubbed Firestore access in unit test")
    monkeypatch.setattr("google.cloud.firestore.Client", _fail)
    monkeypatch.setattr("app.db.firestore_client.get_firestore_client", _fail)
    monkeypatch.setattr("app.db.contractors.get_firestore_client", _fail)
    monkeypatch.setattr("app.db.owner_sms.get_firestore_client", _fail)


def test_contractor_update_strict_bool_validation():
    # Valid strict booleans
    update_true = ContractorUpdate(owner_sms_enabled=True)
    assert update_true.owner_sms_enabled is True

    update_false = ContractorUpdate(owner_sms_enabled=False)
    assert update_false.owner_sms_enabled is False

    update_none = ContractorUpdate(owner_sms_enabled=None)
    assert update_none.owner_sms_enabled is None

    # Invalid truthy/falsy non-boolean types should be rejected by StrictBool
    for invalid_val in ("true", "false", "1", "0", 1, 0, [], {}):
        with pytest.raises(ValidationError):
            ContractorUpdate(owner_sms_enabled=invalid_val)


def test_protected_fields_cannot_be_overwritten_via_contractor_update():
    # Verify owner_sms_opted_out and related fields are in PROTECTED_FIELDS
    for field in (
        "owner_sms_opted_out",
        "owner_sms_opt_out_revision",
        "owner_sms_opt_out_source",
        "owner_sms_opt_out_updated_at",
    ):
        assert field in contractor_db.PROTECTED_FIELDS


def test_redact_contractor_projects_explicit_booleans_and_version_1():
    # Default/missing fields
    raw_doc = {
        "id": "c123",
        "contractor_id": "c123",
        "business_name": "Acme Plumbing",
    }
    projected = _redact_contractor(raw_doc)
    assert projected["owner_sms_enabled"] is True
    assert projected["owner_sms_opted_out"] is False
    assert projected["owner_sms_opt_out_revision"] == 0
    assert projected["owner_sms_settings_version"] == 1

    # Explicit values
    raw_doc_custom = {
        "id": "c123",
        "contractor_id": "c123",
        "owner_sms_enabled": False,
        "owner_sms_opted_out": True,
        "owner_sms_opt_out_revision": 3,
    }
    projected_custom = _redact_contractor(raw_doc_custom)
    assert projected_custom["owner_sms_enabled"] is False
    assert projected_custom["owner_sms_opted_out"] is True
    assert projected_custom["owner_sms_opt_out_revision"] == 3
    assert projected_custom["owner_sms_settings_version"] == 1

    # Malformed / null values fail closed
    for malformed_val in ("not_a_bool", 1, 0, [], {}, None):
        raw_doc_malformed = {
            "id": "c123",
            "owner_sms_enabled": malformed_val,
            "owner_sms_opted_out": malformed_val,
        }
        projected_malformed = _redact_contractor(raw_doc_malformed)
        assert projected_malformed["owner_sms_enabled"] is False  # fails closed
        assert projected_malformed["owner_sms_opted_out"] is True   # fails closed (carrier blocked)
        assert projected_malformed["owner_sms_settings_version"] == 1


def test_resolve_contractor_owner_sms_identities():
    # 1. Valid companion field wins
    doc_with_companion = {
        "owner_phone_e164": "+12025550123",
        "owner_phone": "202-555-0123",
        "twilio_number": "+12025550199",
    }
    owner, twilio = owner_sms_db.resolve_contractor_owner_sms_identities(doc_with_companion)
    assert owner == "+12025550123"
    assert twilio == "+12025550199"

    # 2. Present malformed companion fails closed
    for bad_companion in ("", "invalid", None, 12345):
        doc_bad_companion = {
            "owner_phone_e164": bad_companion,
            "owner_phone": "+12025550123",
            "twilio_number": "+12025550199",
        }
        owner_bad, _ = owner_sms_db.resolve_contractor_owner_sms_identities(doc_bad_companion)
        assert owner_bad is None

    # 3. Absent companion falls back to owner_phone with country_code
    doc_legacy_us = {
        "owner_phone": "2025550123",
        "country_code": "US",
        "twilio_number": "+12025550199",
    }
    owner_us, twilio_us = owner_sms_db.resolve_contractor_owner_sms_identities(doc_legacy_us)
    assert owner_us == "+12025550123"
    assert twilio_us == "+12025550199"

    # 4. Absent country defaults to US
    doc_legacy_no_country = {
        "owner_phone": "(202) 555-0123",
        "twilio_number": "(202) 555-0199",
    }
    owner_def, twilio_def = owner_sms_db.resolve_contractor_owner_sms_identities(doc_legacy_no_country)
    assert owner_def == "+12025550123"
    assert twilio_def == "+12025550199"


@pytest.mark.asyncio
async def test_api_update_contractor_owner_sms_patch_readback(monkeypatch):
    contractor_state = {
        "contractor_id": "c123",
        "owner_sms_enabled": True,
        "owner_sms_opted_out": False,
        "owner_sms_opt_out_revision": 1,
    }

    async def mock_update(cid, updates):
        if cid != "c123":
            return False
        contractor_state.update(updates)
        return True

    async def mock_get(cid):
        if cid == "c123":
            return dict(contractor_state)
        return None

    monkeypatch.setattr("app.api.contractors.update_contractor", mock_update)
    monkeypatch.setattr("app.api.contractors.get_contractor", mock_get)

    class FakeRequest:
        state = type("State", (), {"is_admin": False, "contractor_id": "c123"})()

    # 1. Update owner_sms_enabled -> returns authoritative versioned fields
    res = await api_update_contractor(
        "c123",
        ContractorUpdate(owner_sms_enabled=False),
        FakeRequest(),
    )
    assert res["status"] == "ok"
    assert res["owner_sms_enabled"] is False
    assert res["owner_sms_opted_out"] is False
    assert res["owner_sms_opt_out_revision"] == 1
    assert res["owner_sms_settings_version"] == 1

    # 2. Update unrelated field -> returns simple status ok without readback
    get_calls = []

    async def tracking_get(cid):
        get_calls.append(cid)
        return dict(contractor_state)

    monkeypatch.setattr("app.api.contractors.get_contractor", tracking_get)

    res_unrelated = await api_update_contractor(
        "c123",
        ContractorUpdate(business_name="New Business Name"),
        FakeRequest(),
    )
    assert res_unrelated == {"status": "ok"}
    assert len(get_calls) == 0  # no Firestore readback on unrelated update

    # 3. Empty update -> returns no changes without Firestore readback
    res_empty = await api_update_contractor(
        "c123",
        ContractorUpdate(),
        FakeRequest(),
    )
    assert res_empty == {"status": "no changes"}
    assert len(get_calls) == 0

    # 4. Failed persistence raises 500
    async def failing_update(cid, updates):
        return False

    monkeypatch.setattr("app.api.contractors.update_contractor", failing_update)
    with pytest.raises(HTTPException) as exc_info:
        await api_update_contractor(
            "c123",
            ContractorUpdate(owner_sms_enabled=True),
            FakeRequest(),
        )
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_send_owner_sms_suppressed_when_app_switch_disabled(monkeypatch):
    contractor = {
        "contractor_id": "contractor_abc",
        "owner_phone": "+12025550123",
        "twilio_number": "+12025550199",
        "owner_sms_enabled": False,
        "owner_sms_opted_out": False,
    }

    async def mock_get_contractor(cid):
        return contractor if cid == "contractor_abc" else None

    send_calls = []

    class FakeMessages:
        def create(self, **kwargs):
            send_calls.append(kwargs)
            class Msg:
                sid = "SM_fake"
            return Msg()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(contractor_db, "get_contractor", mock_get_contractor)
    monkeypatch.setattr(owner_sms_service, "Client", FakeClient)

    result = await owner_sms_service.send_owner_sms(
        "contractor_abc",
        "Call summary: Test call finished",
    )

    # Must return None (intentional suppression) without constructing provider client
    assert result is None
    assert len(send_calls) == 0


@pytest.mark.asyncio
async def test_send_owner_sms_suppressed_when_carrier_opted_out(monkeypatch):
    contractor = {
        "contractor_id": "contractor_abc",
        "owner_phone": "+12025550123",
        "twilio_number": "+12025550199",
        "owner_sms_enabled": True,
        "owner_sms_opted_out": True,
    }

    async def mock_get_contractor(cid):
        return contractor if cid == "contractor_abc" else None

    send_calls = []

    class FakeMessages:
        def create(self, **kwargs):
            send_calls.append(kwargs)
            class Msg:
                sid = "SM_fake"
            return Msg()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(contractor_db, "get_contractor", mock_get_contractor)
    monkeypatch.setattr(owner_sms_service, "Client", FakeClient)

    result = await owner_sms_service.send_owner_sms(
        "contractor_abc",
        "Call summary: Test call finished",
    )

    assert result is None
    assert len(send_calls) == 0


@pytest.mark.asyncio
async def test_send_owner_sms_fails_on_invalid_phone_or_missing_account(monkeypatch):
    # Missing contractor -> returns False
    async def mock_get_contractor(_cid):
        return None

    send_calls = []

    class FakeMessages:
        def create(self, **kwargs):
            send_calls.append(kwargs)
            class Msg:
                sid = "SM_fake"
            return Msg()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(contractor_db, "get_contractor", mock_get_contractor)
    monkeypatch.setattr(owner_sms_service, "Client", FakeClient)

    result = await owner_sms_service.send_owner_sms(
        "contractor_missing",
        "Call summary",
    )
    assert result is False
    assert len(send_calls) == 0

    # Invalid normalized numbers -> returns False
    contractor_bad_phone = {
        "contractor_id": "contractor_bad",
        "owner_phone": "invalid_phone",
        "twilio_number": "+12025550199",
        "owner_sms_enabled": True,
        "owner_sms_opted_out": False,
    }

    async def mock_get_contractor_bad(cid):
        return contractor_bad_phone

    monkeypatch.setattr(contractor_db, "get_contractor", mock_get_contractor_bad)

    result_bad = await owner_sms_service.send_owner_sms(
        "contractor_bad",
        "Call summary",
    )
    assert result_bad is False
    assert len(send_calls) == 0


@pytest.mark.asyncio
async def test_send_owner_sms_success_appends_stop_notice_and_status_callback(monkeypatch):
    contractor = {
        "contractor_id": "contractor_abc",
        "owner_phone": "+1 (202) 555-0123",
        "twilio_number": "+1 (202) 555-0199",
        "owner_sms_enabled": True,
        "owner_sms_opted_out": False,
        "owner_sms_opt_out_revision": 2,
    }

    async def mock_get_contractor(cid):
        return contractor if cid == "contractor_abc" else None

    send_calls = []

    class FakeMessages:
        def create(self, **kwargs):
            send_calls.append(kwargs)
            class Msg:
                sid = "SM11111111111111111111111111111111"
            return Msg()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(contractor_db, "get_contractor", mock_get_contractor)
    monkeypatch.setattr(owner_sms_service, "Client", FakeClient)

    result = await owner_sms_service.send_owner_sms(
        "contractor_abc",
        "Call summary: John Doe called about kitchen remodel.",
    )

    assert result is True
    assert len(send_calls) == 1
    call = send_calls[0]
    assert call["to"] == "+12025550123"
    assert call["from_"] == "+12025550199"
    assert "Reply STOP to stop texts." in call["body"]
    assert call["body"].endswith("Reply STOP to stop texts.")
    # Status callback url should contain contractor_id and revision query params
    assert call["status_callback"] is not None
    assert "contractor_id=contractor_abc" in call["status_callback"]
    assert "revision=2" in call["status_callback"]


@pytest.mark.asyncio
async def test_send_owner_sms_handles_twilio_21610_as_suppression_and_persists_block(monkeypatch):
    contractor = {
        "contractor_id": "contractor_abc",
        "owner_phone": "+12025550123",
        "twilio_number": "+12025550199",
        "owner_sms_enabled": True,
        "owner_sms_opted_out": False,
        "owner_sms_opt_out_revision": 1,
    }

    async def mock_get_contractor(cid):
        return contractor if cid == "contractor_abc" else None

    class FakeMessages:
        def create(self, **kwargs):
            raise TwilioRestException(
                status=400,
                uri="/2010-04-01/Accounts/AC123/Messages.json",
                msg="The message From/To pair violates a blacklist rule.",
                code=21610,
            )

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.messages = FakeMessages()

    transitions = []

    async def mock_apply_transition(contractor_id, **kwargs):
        transitions.append((contractor_id, kwargs))
        return True, {}

    monkeypatch.setattr(contractor_db, "get_contractor", mock_get_contractor)
    monkeypatch.setattr(owner_sms_service, "Client", FakeClient)
    monkeypatch.setattr(
        owner_sms_db,
        "apply_owner_sms_consent_transition",
        mock_apply_transition,
    )

    result = await owner_sms_service.send_owner_sms(
        "contractor_abc",
        "Call summary",
    )

    # 21610 must return None (intentional suppression, do not retry)
    assert result is None
    # Transition should have been applied with expected_revision=1
    assert len(transitions) == 1
    cid, kw = transitions[0]
    assert cid == "contractor_abc"
    assert kw["opt_out"] is True
    assert kw["expected_revision"] == 1
    assert kw["source"] == "provider_21610_sync"


@pytest.mark.asyncio
async def test_firestore_query_serialization_with_anonymous_credentials(monkeypatch):
    """Serialize the production query, including its document cursor, without RPCs."""
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.firestore_v1 import Client
    from google.cloud.firestore_v1.query import Query
    from types import SimpleNamespace
    from app.db import post_call_handoffs

    client = Client(project="test-project", credentials=AnonymousCredentials())
    serialized = []

    def capture_stream(query):
        serialized.append(query._to_protobuf())
        return iter([SimpleNamespace(id="CA_next")])

    monkeypatch.setattr(post_call_handoffs, "get_firestore_client", lambda: client)
    monkeypatch.setattr(Query, "stream", capture_stream)
    ids = await post_call_handoffs.list_handoff_ids(
        "pending", limit=3, start_after="CA_test_cursor"
    )
    assert ids == ["CA_next"]
    query = serialized[0]
    assert query.limit == 3
    assert query.where.field_filter.field.field_path == "status"
    assert query.where.field_filter.value.string_value == "pending"
    assert query.order_by[0].field.field_path == "__name__"
    assert query.start_at.before is False
    assert query.start_at.values[0].reference_value.endswith(
        "/documents/post_call_handoffs/CA_test_cursor"
    )


@pytest.mark.asyncio
async def test_sms_patch_rejects_cross_account_and_unconfirmed_readback(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    request = SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id="owner-a"))
    update = AsyncMock(return_value=True)
    read = AsyncMock(return_value=None)
    monkeypatch.setattr("app.api.contractors.update_contractor", update)
    monkeypatch.setattr("app.api.contractors.get_contractor", read)
    with pytest.raises(HTTPException) as denied:
        await api_update_contractor("owner-b", ContractorUpdate(owner_sms_enabled=False), request)
    assert denied.value.status_code == 403
    update.assert_not_awaited()
    for invalid in [None, {"contractor_id": "owner-b", "owner_sms_enabled": False}]:
        read.return_value = invalid
        with pytest.raises(HTTPException) as failed:
            await api_update_contractor("owner-a", ContractorUpdate(owner_sms_enabled=False), request)
        assert failed.value.status_code == 500


@pytest.mark.asyncio
async def test_enabling_app_switch_cannot_clear_provider_block(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    request = SimpleNamespace(state=SimpleNamespace(is_admin=False, contractor_id="c1"))
    update = AsyncMock(return_value=True)
    monkeypatch.setattr("app.api.contractors.update_contractor", update)
    monkeypatch.setattr("app.api.contractors.get_contractor", AsyncMock(return_value={
        "contractor_id": "c1", "owner_sms_enabled": True,
        "owner_sms_opted_out": True, "owner_sms_opt_out_revision": 2,
    }))
    response = await api_update_contractor("c1", ContractorUpdate(
        owner_sms_enabled=True, owner_sms_opted_out=False, owner_sms_opt_out_revision=0,
    ), request)
    assert update.await_args.args[1] == {"owner_sms_enabled": True}
    assert response["owner_sms_opted_out"] is True
    assert response["owner_sms_opt_out_revision"] == 2


@pytest.mark.asyncio
async def test_owner_sender_preserves_messaging_service_and_fresh_preferences(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from app.config import settings
    account = {"contractor_id": "c1", "owner_phone": "+12025550123", "twilio_number": "+12025550199"}
    read = AsyncMock(side_effect=[dict(account), {**account, "owner_sms_enabled": False}])
    create = Mock(return_value=SimpleNamespace(sid="SM" + "1" * 32))
    monkeypatch.setattr(contractor_db, "get_contractor", read)
    monkeypatch.setattr(owner_sms_service, "Client", lambda *a: SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(settings, "twilio_messaging_service_sid", "MG" + "1" * 32)
    assert await owner_sms_service.send_owner_sms("c1", "Finished summary") is True
    assert create.call_args.kwargs["messaging_service_sid"] == "MG" + "1" * 32
    assert create.call_args.kwargs["from_"] == "+12025550199"
    assert await owner_sms_service.send_owner_sms("c1", "Later summary") is None
    assert create.call_count == 1
    assert read.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [{"owner_sms_enabled": False}, {"owner_sms_opted_out": True}])
async def test_queued_summary_obeys_fresh_opt_out_and_preserves_record_and_push(monkeypatch, blocked):
    from unittest.mock import AsyncMock, Mock
    from app.services import post_call
    account = {"contractor_id": "c1", "owner_phone": "+12025550123", "twilio_number": "+12025550199", "effective_mode": "personal"}
    monkeypatch.setattr(contractor_db, "get_contractor", AsyncMock(return_value={**account, **blocked}))
    provider = Mock(side_effect=AssertionError("Opt-out must not reach provider"))
    monkeypatch.setattr(owner_sms_service, "Client", provider)
    monkeypatch.setattr("app.services.job_card.extract_job_card", AsyncMock(return_value={
        "caller_name": "Test caller", "issue_description": "Please call back", "call_type": "unknown",
    }))
    save = AsyncMock(return_value=True)
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(post_call.call_db, "save_call", save)
    monkeypatch.setattr(post_call, "_send_summary_push", push)
    monkeypatch.setattr(post_call, "_update_caller_contact", AsyncMock(return_value=True))
    result = await post_call.process_post_call(
        transcript_lines=["Caller: Please call back"], caller_phone="+12025550124",
        call_sid="CA_test", contractor_phone=account["owner_phone"],
        twilio_number=account["twilio_number"], contractor=account,
    )
    assert result.status == "complete"
    assert "call_record" in result.completed_effects
    assert "owner_sms" not in result.completed_effects
    assert not result.failed_effects
    save.assert_awaited()
    push.assert_awaited_once()
    provider.assert_not_called()


@pytest.mark.asyncio
async def test_owner_estimate_has_no_generic_sender_fallback(monkeypatch):
    from unittest.mock import AsyncMock
    from app.services.estimate_notifications import send_estimate_notifications
    generic = AsyncMock(return_value=True)
    owner = AsyncMock(return_value=None)
    monkeypatch.setattr(owner_sms_service, "send_owner_sms", owner)
    await send_estimate_notifications("+12025550124", {"contractor_id": "c1"}, "CA_test", "test-hash", send_sms_fn=generic)
    generic.assert_awaited_once()
    assert generic.await_args.args[0] == "+12025550124"
    owner.assert_awaited_once()
    generic.reset_mock()
    owner.reset_mock()
    await send_estimate_notifications("", {"owner_phone": "+12025550123"}, "CA_test", "test-hash", send_sms_fn=generic)
    generic.assert_not_awaited()
    owner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("write_raises", [False, True])
async def test_sync_provider_opt_out_persistence_failure_is_not_successful_suppression(monkeypatch, write_raises):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    account = {"contractor_id": "c1", "owner_phone": "+12025550123", "twilio_number": "+12025550199"}
    monkeypatch.setattr(contractor_db, "get_contractor", AsyncMock(return_value=account))
    create = Mock(side_effect=TwilioRestException(status=400, uri="/test", msg="blocked", code=21610))
    monkeypatch.setattr(owner_sms_service, "Client", lambda *a: SimpleNamespace(messages=SimpleNamespace(create=create)))
    transition = AsyncMock(side_effect=RuntimeError("unavailable")) if write_raises else AsyncMock(return_value=(False, {"error": "write failed"}))
    monkeypatch.setattr(owner_sms_db, "apply_owner_sms_consent_transition", transition)
    assert await owner_sms_service.send_owner_sms("c1", "Completed summary") is False
    create.assert_called_once()  # Surface failure without retrying a blocked recipient.
    transition.assert_awaited_once()
