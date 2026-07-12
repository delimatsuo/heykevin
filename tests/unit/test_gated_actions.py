import importlib
import json
import sys


REQUIRED_CONFIG_ENV = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "TELEGRAM_BOT_TOKEN",
    "USER_PHONE",
)


def import_gated_actions_without_config_env(monkeypatch):
    for key in REQUIRED_CONFIG_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    # Exercise the import without app.config while letting monkeypatch restore
    # both sys.modules and package attributes afterward. A raw sys.modules.pop()
    # leaves app.config attached to the package object, which can make later
    # tests patch a stale settings module while app code imports a fresh one.
    monkeypatch.delitem(sys.modules, "app.config", raising=False)
    monkeypatch.delitem(sys.modules, "app.services.gated_actions", raising=False)
    import app
    import app.services
    monkeypatch.delattr(app, "config", raising=False)
    monkeypatch.delattr(app.services, "gated_actions", raising=False)

    return importlib.import_module("app.services.gated_actions")


def test_module_imports_without_app_config_env_seeding(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)

    decision = gated_actions.check_gated_action(
        contractor=None,
        action=gated_actions.ActionKey.CALLER_TEXT_REPLY,
        context=gated_actions.GateContext(
            source="ios",
            actor="owner",
            idempotency_key="msg-1",
            owner_confirmed=True,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == gated_actions.GateReason.MISSING_CONTRACTOR


def test_unknown_or_missing_contractor_fails_closed(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)
    decision = gated_actions.check_gated_action(
        contractor=None,
        action=gated_actions.ActionKey.CALLER_TEXT_REPLY,
        context=gated_actions.GateContext(source="ios", actor="owner", idempotency_key="msg-1", owner_confirmed=True),
    )

    assert decision.allowed is False
    assert decision.reason == gated_actions.GateReason.MISSING_CONTRACTOR


def test_sms_action_requires_enabled_flag(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)
    contractor = {
        "contractor_id": "c1",
        "sms_compliance_status": "approved",
    }
    decision = gated_actions.check_gated_action(
        contractor=contractor,
        action=gated_actions.ActionKey.CALLER_TEXT_REPLY,
        context=gated_actions.GateContext(source="ios", actor="owner", idempotency_key="msg-1", owner_confirmed=True),
    )

    assert decision.allowed is False
    assert decision.reason == gated_actions.GateReason.FEATURE_DISABLED


def test_sms_action_requires_compliance(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)
    contractor = {
        "contractor_id": "c1",
        "gated_actions": {gated_actions.ActionKey.CALLER_TEXT_REPLY.value: True},
        "sms_compliance_status": "pending",
    }
    decision = gated_actions.check_gated_action(
        contractor=contractor,
        action=gated_actions.ActionKey.CALLER_TEXT_REPLY,
        context=gated_actions.GateContext(source="ios", actor="owner", idempotency_key="msg-1", owner_confirmed=True),
    )

    assert decision.allowed is False
    assert decision.reason == gated_actions.GateReason.COMPLIANCE_NOT_APPROVED


def test_sms_action_requires_idempotency(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)
    contractor = {
        "contractor_id": "c1",
        "gated_actions": {gated_actions.ActionKey.CALLER_CONFIRMATION_SMS.value: True},
        "sms_compliance_status": "approved",
    }
    decision = gated_actions.check_gated_action(
        contractor=contractor,
        action=gated_actions.ActionKey.CALLER_CONFIRMATION_SMS,
        context=gated_actions.GateContext(source="ios", actor="owner", owner_confirmed=True),
    )

    assert decision.allowed is False
    assert decision.reason == gated_actions.GateReason.IDEMPOTENCY_REQUIRED


def test_sms_action_allows_when_flag_compliance_confirmation_and_idempotency_present(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)
    contractor = {
        "contractor_id": "c1",
        "gated_actions": {gated_actions.ActionKey.CALLER_TEXT_REPLY.value: True},
        "sms_compliance_status": "approved",
    }
    decision = gated_actions.check_gated_action(
        contractor=contractor,
        action=gated_actions.ActionKey.CALLER_TEXT_REPLY,
        context=gated_actions.GateContext(source="ios", actor="owner", idempotency_key="msg-1", owner_confirmed=True),
    )

    assert decision.allowed is True
    assert decision.reason == gated_actions.GateReason.ALLOWED


def test_integration_write_requires_integration_approval(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)
    contractor = {
        "contractor_id": "c1",
        "gated_actions": {gated_actions.ActionKey.JOBBER_CREATE_JOB.value: True},
        "integration_write_status": "pending",
    }
    decision = gated_actions.check_gated_action(
        contractor=contractor,
        action=gated_actions.ActionKey.JOBBER_CREATE_JOB,
        context=gated_actions.GateContext(
            source="voice_tool",
            actor="automation",
            idempotency_key="job-1",
            owner_confirmed=True,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == gated_actions.GateReason.COMPLIANCE_NOT_APPROVED


def test_integration_write_requires_owner_confirmation_or_automation_approval(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)
    contractor = {
        "contractor_id": "c1",
        "gated_actions": {gated_actions.ActionKey.JOBBER_CREATE_JOB.value: True},
        "integration_write_status": "approved",
    }
    decision = gated_actions.check_gated_action(
        contractor=contractor,
        action=gated_actions.ActionKey.JOBBER_CREATE_JOB,
        context=gated_actions.GateContext(
            source="voice_tool",
            actor="automation",
            idempotency_key="job-1",
            owner_confirmed=False,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == gated_actions.GateReason.OWNER_CONFIRMATION_REQUIRED


def test_automation_approval_allows_integration_write_when_status_and_flag_approved(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)
    contractor = {
        "contractor_id": "c1",
        "gated_actions": {gated_actions.ActionKey.JOBBER_CREATE_JOB.value: True},
        "automation_approvals": {gated_actions.ActionKey.JOBBER_CREATE_JOB.value: True},
        "integration_write_status": "approved",
    }
    decision = gated_actions.check_gated_action(
        contractor=contractor,
        action=gated_actions.ActionKey.JOBBER_CREATE_JOB,
        context=gated_actions.GateContext(
            source="voice_tool",
            actor="automation",
            idempotency_key="job-1",
            owner_confirmed=False,
        ),
    )

    assert decision.allowed is True
    assert decision.reason == gated_actions.GateReason.ALLOWED


def test_google_create_event_is_disabled_even_with_all_approvals(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)
    contractor = {
        "contractor_id": "c1",
        "gated_actions": {gated_actions.ActionKey.GOOGLE_CREATE_EVENT.value: True},
        "automation_approvals": {gated_actions.ActionKey.GOOGLE_CREATE_EVENT.value: True},
        "integration_write_status": "approved",
    }

    decision = gated_actions.check_gated_action(
        contractor=contractor,
        action=gated_actions.ActionKey.GOOGLE_CREATE_EVENT,
        context=gated_actions.GateContext(
            source="voice_tool",
            actor="automation",
            idempotency_key="call-1:google-create-event",
            owner_confirmed=True,
        ),
    )

    assert decision.allowed is False
    assert decision.reason == gated_actions.GateReason.ENVIRONMENT_DISABLED
    assert decision.message == "Live appointment booking is disabled in this release."


def test_every_action_key_has_explicit_policy(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)

    assert set(gated_actions.GATE_POLICIES) == set(gated_actions.ActionKey)


def test_disabled_response_is_typed_and_payload_safe(monkeypatch):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)
    decision = gated_actions.check_gated_action(
        contractor={"contractor_id": "c1"},
        action=gated_actions.ActionKey.CALLER_AUTO_REPLY,
        context=gated_actions.GateContext(source="post_call", actor="system", idempotency_key="auto-1", owner_confirmed=False),
    )

    body = decision.to_response()
    assert body == {
        "allowed": False,
        "reason": "feature_disabled",
        "message": "This action is not enabled for this account.",
    }


def test_record_gate_decision_emits_sanitized_payload_safe_json_fields(monkeypatch, capsys):
    gated_actions = import_gated_actions_without_config_env(monkeypatch)
    side_effect_audit = importlib.reload(importlib.import_module("app.services.side_effect_audit"))
    log_utils = importlib.import_module("app.utils.logging")
    log_utils.setup_logging("INFO")

    unsafe_source = "voice_tool raw caller said call me at +15555550123 " + ("x" * 200)
    unsafe_resource_id = "call_sid:CA1234567890 idempotency-key:secret-token phone:+15555550123"
    decision = gated_actions.GateDecision(
        allowed=False,
        action=gated_actions.ActionKey.CALLER_TEXT_REPLY,
        reason=gated_actions.GateReason.FEATURE_DISABLED,
        message="This action is not enabled for this account.",
    )

    side_effect_audit.record_gate_decision(
        action=gated_actions.ActionKey.CALLER_TEXT_REPLY,
        contractor_id="contractor-1234567890-phone-+15555550123",
        source=unsafe_source,
        resource_id=unsafe_resource_id,
        decision=decision,
    )

    logged = json.loads(capsys.readouterr().out)
    assert logged["message"] == "side_effect_gate_decision"
    assert logged["action"] == "caller_text_reply"
    assert logged["contractor_id"] == "contract"
    assert logged["source"] != unsafe_source
    assert len(logged["source"]) <= 40
    assert logged["resource_id"] != unsafe_resource_id
    assert len(logged["resource_id"]) <= 12
    assert logged["allowed"] is False
    assert logged["reason"] == "feature_disabled"

    serialized = json.dumps(logged)
    assert "+15555550123" not in serialized
    assert "secret-token" not in serialized
    assert "raw caller said" not in serialized
