"""Canonical backend gate registry for Phase 0 side effects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.config import settings


class ActionKey(str, Enum):
    CALLER_TEXT_REPLY = "caller_text_reply"
    CALLER_AUTO_REPLY = "caller_auto_reply"
    CALLER_CONFIRMATION_SMS = "caller_confirmation_sms"
    CALLER_CONFIRMATION_MMS = "caller_confirmation_mms"
    CALLER_VCARD_MMS = "caller_vcard_mms"
    ESTIMATE_TOKEN_CREATE = "estimate_token_create"
    ESTIMATE_RESULT_SMS = "estimate_result_sms"
    JOBBER_CREATE_JOB = "jobber_create_job"
    JOBBER_CREATE_QUOTE = "jobber_create_quote"
    GOOGLE_CREATE_EVENT = "google_create_event"
    TWILIO_CALL_REDIRECT = "twilio_call_redirect"
    TWILIO_CONFERENCE_MUTATION = "twilio_conference_mutation"
    TWILIO_NUMBER_PROVISION = "twilio_number_provision"
    TWILIO_NUMBER_RELEASE = "twilio_number_release"
    ACCOUNT_DELETE = "account_delete"
    PUSH_LOCK_SCREEN_CONTEXT = "push_lock_screen_context"


class GateReason(str, Enum):
    ALLOWED = "allowed"
    MISSING_CONTRACTOR = "missing_contractor"
    FEATURE_DISABLED = "feature_disabled"
    COMPLIANCE_NOT_APPROVED = "compliance_not_approved"
    OWNER_CONFIRMATION_REQUIRED = "owner_confirmation_required"
    IDEMPOTENCY_REQUIRED = "idempotency_required"
    ENVIRONMENT_DISABLED = "environment_disabled"


@dataclass(frozen=True)
class GateContext:
    source: str
    actor: str
    idempotency_key: str = ""
    owner_confirmed: bool = False
    environment: str = ""


@dataclass(frozen=True)
class GatePolicy:
    requires_flag: bool = True
    requires_sms_compliance: bool = False
    requires_integration_approval: bool = False
    requires_owner_confirmation: bool = False
    requires_idempotency: bool = True
    allow_local_without_flag: bool = False


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    action: ActionKey
    reason: GateReason
    message: str

    def to_response(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason.value,
            "message": self.message,
        }


SMS_ACTIONS = {
    ActionKey.CALLER_TEXT_REPLY,
    ActionKey.CALLER_AUTO_REPLY,
    ActionKey.CALLER_CONFIRMATION_SMS,
    ActionKey.CALLER_CONFIRMATION_MMS,
    ActionKey.CALLER_VCARD_MMS,
    ActionKey.ESTIMATE_RESULT_SMS,
}

INTEGRATION_WRITE_ACTIONS = {
    ActionKey.JOBBER_CREATE_JOB,
    ActionKey.JOBBER_CREATE_QUOTE,
    ActionKey.GOOGLE_CREATE_EVENT,
}


GATE_POLICIES: dict[ActionKey, GatePolicy] = {
    ActionKey.CALLER_TEXT_REPLY: GatePolicy(requires_sms_compliance=True, requires_owner_confirmation=True),
    ActionKey.CALLER_AUTO_REPLY: GatePolicy(requires_sms_compliance=True),
    ActionKey.CALLER_CONFIRMATION_SMS: GatePolicy(requires_sms_compliance=True),
    ActionKey.CALLER_CONFIRMATION_MMS: GatePolicy(requires_sms_compliance=True),
    ActionKey.CALLER_VCARD_MMS: GatePolicy(requires_sms_compliance=True),
    ActionKey.ESTIMATE_TOKEN_CREATE: GatePolicy(requires_owner_confirmation=True),
    ActionKey.ESTIMATE_RESULT_SMS: GatePolicy(requires_sms_compliance=True),
    ActionKey.JOBBER_CREATE_JOB: GatePolicy(requires_integration_approval=True, requires_owner_confirmation=True),
    ActionKey.JOBBER_CREATE_QUOTE: GatePolicy(requires_integration_approval=True, requires_owner_confirmation=True),
    ActionKey.GOOGLE_CREATE_EVENT: GatePolicy(requires_integration_approval=True, requires_owner_confirmation=True),
    ActionKey.TWILIO_CALL_REDIRECT: GatePolicy(requires_owner_confirmation=True),
    ActionKey.TWILIO_CONFERENCE_MUTATION: GatePolicy(requires_owner_confirmation=True),
    ActionKey.TWILIO_NUMBER_PROVISION: GatePolicy(requires_owner_confirmation=True),
    ActionKey.TWILIO_NUMBER_RELEASE: GatePolicy(requires_owner_confirmation=True),
    ActionKey.ACCOUNT_DELETE: GatePolicy(requires_owner_confirmation=True),
    ActionKey.PUSH_LOCK_SCREEN_CONTEXT: GatePolicy(requires_idempotency=False),
}


def _environment(context: GateContext) -> str:
    return context.environment or getattr(settings, "environment", "") or "production"


def _flag_enabled(contractor: dict[str, Any], action: ActionKey) -> bool:
    flags = contractor.get("gated_actions") or {}
    return flags.get(action.value) is True


def _automation_approved(contractor: dict[str, Any], action: ActionKey) -> bool:
    approvals = contractor.get("automation_approvals") or {}
    return approvals.get(action.value) is True


def _allowed_false(action: ActionKey, reason: GateReason, message: str) -> GateDecision:
    return GateDecision(allowed=False, action=action, reason=reason, message=message)


def check_gated_action(contractor: dict[str, Any] | None, action: ActionKey, context: GateContext) -> GateDecision:
    """Return the fail-closed backend decision for a side-effect action."""
    if not contractor or not contractor.get("contractor_id"):
        return _allowed_false(action, GateReason.MISSING_CONTRACTOR, "No account owner was found for this action.")

    policy = GATE_POLICIES[action]
    env = _environment(context)

    if env == "production" and policy.requires_flag and not _flag_enabled(contractor, action):
        return _allowed_false(action, GateReason.FEATURE_DISABLED, "This action is not enabled for this account.")

    if env != "production" and policy.requires_flag and not (policy.allow_local_without_flag or _flag_enabled(contractor, action)):
        return _allowed_false(action, GateReason.FEATURE_DISABLED, "This action is not enabled for this account.")

    if policy.requires_sms_compliance and contractor.get("sms_compliance_status") != "approved":
        return _allowed_false(action, GateReason.COMPLIANCE_NOT_APPROVED, "Texting is not enabled for this account.")

    if policy.requires_integration_approval and contractor.get("integration_write_status") != "approved":
        return _allowed_false(action, GateReason.COMPLIANCE_NOT_APPROVED, "Integration writes are not enabled for this account.")

    if policy.requires_owner_confirmation and not (context.owner_confirmed or _automation_approved(contractor, action)):
        return _allowed_false(action, GateReason.OWNER_CONFIRMATION_REQUIRED, "Owner confirmation is required for this action.")

    if policy.requires_idempotency and not context.idempotency_key:
        return _allowed_false(action, GateReason.IDEMPOTENCY_REQUIRED, "This action requires an idempotency key.")

    return GateDecision(allowed=True, action=action, reason=GateReason.ALLOWED, message="Allowed.")
