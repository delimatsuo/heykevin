import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.services.gated_actions import ActionKey, GateContext, GateReason, check_gated_action


def test_unknown_or_missing_contractor_fails_closed():
    decision = check_gated_action(
        contractor=None,
        action=ActionKey.CALLER_TEXT_REPLY,
        context=GateContext(source="ios", actor="owner", idempotency_key="msg-1", owner_confirmed=True),
    )

    assert decision.allowed is False
    assert decision.reason == GateReason.MISSING_CONTRACTOR


def test_sms_action_requires_enabled_flag_and_compliance():
    contractor = {
        "contractor_id": "c1",
        "gated_actions": {ActionKey.CALLER_TEXT_REPLY.value: True},
        "sms_compliance_status": "pending",
    }
    decision = check_gated_action(
        contractor=contractor,
        action=ActionKey.CALLER_TEXT_REPLY,
        context=GateContext(source="ios", actor="owner", idempotency_key="msg-1", owner_confirmed=True),
    )

    assert decision.allowed is False
    assert decision.reason == GateReason.COMPLIANCE_NOT_APPROVED


def test_sms_action_allows_when_flag_compliance_confirmation_and_idempotency_present():
    contractor = {
        "contractor_id": "c1",
        "gated_actions": {ActionKey.CALLER_TEXT_REPLY.value: True},
        "sms_compliance_status": "approved",
    }
    decision = check_gated_action(
        contractor=contractor,
        action=ActionKey.CALLER_TEXT_REPLY,
        context=GateContext(source="ios", actor="owner", idempotency_key="msg-1", owner_confirmed=True),
    )

    assert decision.allowed is True
    assert decision.reason == GateReason.ALLOWED


def test_integration_write_requires_owner_confirmation_or_automation_approval():
    contractor = {
        "contractor_id": "c1",
        "gated_actions": {ActionKey.JOBBER_CREATE_JOB.value: True},
        "integration_write_status": "approved",
    }
    decision = check_gated_action(
        contractor=contractor,
        action=ActionKey.JOBBER_CREATE_JOB,
        context=GateContext(source="voice_tool", actor="automation", idempotency_key="job-1", owner_confirmed=False),
    )

    assert decision.allowed is False
    assert decision.reason == GateReason.OWNER_CONFIRMATION_REQUIRED


def test_disabled_response_is_typed_and_payload_safe():
    decision = check_gated_action(
        contractor={"contractor_id": "c1"},
        action=ActionKey.CALLER_AUTO_REPLY,
        context=GateContext(source="post_call", actor="system", idempotency_key="auto-1", owner_confirmed=False),
    )

    body = decision.to_response()
    assert body == {
        "allowed": False,
        "reason": "feature_disabled",
        "message": "This action is not enabled for this account.",
    }
