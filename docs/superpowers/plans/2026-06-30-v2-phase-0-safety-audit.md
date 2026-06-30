# Hey Kevin v2 Phase 0 Safety Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 0 safety layer required before v2 UI or feature work: a complete side-effect inventory, canonical backend gate registry, endpoint ownership matrix, fail-closed tests, and disabled-by-default gates for existing outbound actions.

**Architecture:** Add a small backend safety layer around existing side effects instead of rewriting call flow. `app/services/side_effect_inventory.py` records the repo-derived inventory, `app/services/gated_actions.py` is the canonical backend gate source, and existing paths call the gate before contacting users, mutating Twilio, or writing integrations. Phase 0 is complete only when high-risk current paths fail closed by default and tests prove ownership, gating, payload safety, and rollback/disable behavior.

**Tech Stack:** Python 3.12, FastAPI, pytest, Firestore client abstractions, Twilio SDK, Jobber/Google Calendar service helpers.

---

## Scope

This plan implements only Phase 0 from `docs/superpowers/specs/2026-06-30-business-first-dispatch-v2-design.md` Section 18.1.

In scope:

- Current side-effect inventory and endpoint matrix.
- Backend gated-action registry with fail-closed defaults.
- Tests for disabled-by-default caller SMS/MMS, integration writes, estimate SMS, text reply, voice-tool booking, Telegram alternate paths, and mark-read ownership.
- Lock-screen-safe push body helpers for urgent and summary pushes.
- Documentation of which current paths are gated, audited, or explicitly left read-only.

Out of scope:

- Dispatch, Calls, and Kevin UI.
- Verified forwarding implementation.
- New job-card UI.
- A2P approval work.
- Real booking/integration enablement.
- Production deployment.

## Preflight: Clean Worktree Requirement

The current local `main` is dirty and diverged. Do not implement this plan there.

- [ ] **Step 1: Confirm the dirty source worktree**

Run:

```bash
git status --short --branch
```

Expected: shows `main...origin/main [ahead 1, behind 22]` and dirty files. This confirms why implementation must use a clean worktree.

- [ ] **Step 2: Create a clean worktree from the remote base**

Run from `/Volumes/Extreme Pro/myprojects/Kevin`:

```bash
git fetch origin
git worktree add .worktrees/v2-phase0-safety-audit origin/main -b codex/v2-phase0-safety-audit
cd .worktrees/v2-phase0-safety-audit
```

Expected: clean worktree on `codex/v2-phase0-safety-audit`.

- [ ] **Step 3: Copy approved planning docs into the clean worktree**

Run from `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/v2-phase0-safety-audit`:

```bash
mkdir -p docs/superpowers/specs docs/superpowers/plans
cp /Volumes/Extreme\ Pro/myprojects/Kevin/docs/superpowers/specs/2026-06-30-business-first-dispatch-v2-design.md docs/superpowers/specs/
cp /Volumes/Extreme\ Pro/myprojects/Kevin/docs/superpowers/plans/2026-06-30-v2-phase-0-safety-audit.md docs/superpowers/plans/
git status --short
```

Expected: only the copied spec and plan are new or modified.

- [ ] **Step 4: Commit planning docs before code**

Run:

```bash
git add docs/superpowers/specs/2026-06-30-business-first-dispatch-v2-design.md docs/superpowers/plans/2026-06-30-v2-phase-0-safety-audit.md
git commit -m "docs: add v2 phase 0 safety plan"
```

Expected: commit succeeds on `codex/v2-phase0-safety-audit`.

## File Structure

Create:

- `app/services/side_effect_inventory.py`: static inventory of current side-effect surfaces required by the spec.
- `app/services/gated_actions.py`: canonical backend registry and fail-closed decision helper.
- `app/services/side_effect_audit.py`: payload-safe audit logging helper.
- `tests/unit/test_phase0_side_effect_inventory.py`: inventory completeness tests.
- `tests/unit/test_gated_actions.py`: registry and fail-closed behavior tests.
- `tests/unit/test_phase0_call_ownership.py`: call mutation ownership tests.
- `tests/unit/test_phase0_sms_gates.py`: SMS/MMS gate tests.
- `tests/unit/test_phase0_post_call_gates.py`: post-call side-effect tests.
- `tests/unit/test_phase0_voice_tool_gates.py`: Jobber/Calendar voice tool write tests.
- `tests/unit/test_phase0_estimate_gates.py`: estimate result SMS gate tests.
- `tests/unit/test_phase0_push_payloads.py`: lock-screen-safe payload tests.
- `docs/security/phase0-side-effect-matrix.md`: human-readable matrix generated from the inventory.

Modify:

- `app/api/calls.py`: enforce per-SID ownership in `mark-read`.
- `app/api/voip.py`: gate `text_reply`; audit call actions.
- `app/webhooks/telegram_callback.py`: gate text reply, callback, and follow-up paths.
- `app/services/sms.py`: add optional gate context to SMS/MMS sends.
- `app/services/post_call.py`: gate caller SMS/MMS, vCard MMS, estimate-token creation, auto-reply, and Jobber auto-create.
- `app/services/voice_pipeline.py`: gate `book_appointment` write tools for Jobber and Google Calendar.
- `app/services/gemini_pipeline.py`: ensure Gemini uses the same gated tool contract or explicitly cannot run write tools.
- `app/api/estimates.py`: gate result SMS to caller and contractor.
- `app/webhooks/media_stream.py`: remove raw caller speech from urgent push body.

## Task 1: Side-Effect Inventory

**Files:**
- Create: `app/services/side_effect_inventory.py`
- Create: `tests/unit/test_phase0_side_effect_inventory.py`
- Create: `docs/security/phase0-side-effect-matrix.md`

- [ ] **Step 1: Write failing inventory completeness tests**

Create `tests/unit/test_phase0_side_effect_inventory.py`:

```python
from app.services.side_effect_inventory import SIDE_EFFECT_SURFACES, surfaces_by_path


REQUIRED_PATHS = {
    "app/services/post_call.py",
    "app/services/voice_pipeline.py",
    "app/services/gemini_pipeline.py",
    "app/services/sms.py",
    "app/api/calls.py",
    "app/api/voip.py",
    "app/webhooks/telegram_callback.py",
    "app/webhooks/twilio_incoming.py",
    "app/webhooks/media_stream.py",
    "app/db/jobs.py",
    "app/services/job_card.py",
    "app/services/calendar.py",
    "app/services/jobber.py",
    "app/api/integrations.py",
    "app/api/estimates.py",
    "app/api/contractors.py",
    "app/services/conference.py",
    "app/services/warm_transfer.py",
    "app/services/vcard.py",
    "app/api/vcard.py",
    "app/services/push_notification.py",
}


def test_inventory_covers_required_phase0_paths():
    paths = {surface.path for surface in SIDE_EFFECT_SURFACES}
    assert REQUIRED_PATHS <= paths


def test_every_surface_has_gate_and_evidence():
    for surface in SIDE_EFFECT_SURFACES:
        assert surface.path
        assert surface.current_behavior
        assert surface.required_gate
        assert surface.required_evidence
        assert surface.risk in {"user_contact", "external_write", "twilio_mutation", "sensitive_read", "irreversible"}


def test_surfaces_by_path_groups_all_entries():
    grouped = surfaces_by_path()
    assert "app/services/post_call.py" in grouped
    assert any("caller SMS" in s.current_behavior for s in grouped["app/services/post_call.py"])
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
pytest tests/unit/test_phase0_side_effect_inventory.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.side_effect_inventory'`.

- [ ] **Step 3: Create the inventory module**

Create `app/services/side_effect_inventory.py`:

```python
"""Phase 0 side-effect inventory for v2 safety gating.

This file is intentionally static. It is the implementation source for the
Section 18.1 inventory in the v2 product spec and should be updated whenever a
route, webhook, service, scheduled job, or helper can contact users, mutate
Twilio, mutate integrations, create public links, release numbers, delete data,
write sensitive records, or expose push/log payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


RiskKind = Literal["user_contact", "external_write", "twilio_mutation", "sensitive_read", "irreversible"]


@dataclass(frozen=True)
class SideEffectSurface:
    path: str
    current_behavior: str
    required_gate: str
    required_evidence: str
    risk: RiskKind


SIDE_EFFECT_SURFACES: tuple[SideEffectSurface, ...] = (
    SideEffectSurface(
        path="app/services/post_call.py",
        current_behavior="Extracts job cards, saves jobs, can auto-create Jobber jobs, sends contractor SMS, sends caller SMS/MMS, sends vCard MMS, and can send auto-reply SMS.",
        required_gate="Caller-facing SMS/MMS and integration writes require backend gated actions that default off.",
        required_evidence="Disabled-gate tests for caller SMS/MMS, Jobber writes, vCard MMS, estimate links, and auto-replies.",
        risk="user_contact",
    ),
    SideEffectSurface(
        path="app/services/voice_pipeline.py",
        current_behavior="Voice tools can check Jobber and Google Calendar, and can create Jobber jobs or Google Calendar events.",
        required_gate="Write tools require backend gates, idempotency, and owner confirmation or explicit automation approval.",
        required_evidence="Tests proving model tool calls cannot write integrations while gates are disabled.",
        risk="external_write",
    ),
    SideEffectSurface(
        path="app/services/gemini_pipeline.py",
        current_behavior="Gemini Live shares prompt, callback, transcript, command, urgency, and tool surfaces with the legacy voice pipeline.",
        required_gate="Gemini must obey the same gate registry and sensitive-data rules as the legacy pipeline.",
        required_evidence="Parity tests or contract tests for tools, transcripts, urgency pushes, and completion callbacks.",
        risk="external_write",
    ),
    SideEffectSurface(
        path="app/services/sms.py",
        current_behavior="Sends SMS/MMS through Twilio without delivery-state UI, A2P proof, or opt-out enforcement in the helper.",
        required_gate="Caller-facing SMS/MMS requires A2P, delivery tracking, opt-out handling, send limits, and failure UI.",
        required_evidence="A2P proof, webhook tests, opt-out tests, failed-delivery UI test, and log audit.",
        risk="user_contact",
    ),
    SideEffectSurface(
        path="app/api/calls.py",
        current_behavior="mark-read can write submitted call SIDs.",
        required_gate="All call mutations must verify every call SID belongs to the authenticated contractor.",
        required_evidence="Cross-tenant negative tests for list, detail, mark-read, status update, export, and delete.",
        risk="sensitive_read",
    ),
    SideEffectSurface(
        path="app/api/voip.py",
        current_behavior="Call actions can redirect Twilio calls, queue take-message commands, route to voicemail, and send text replies.",
        required_gate="Every call action must pass ownership, CallSession state, backend gate, and idempotency checks.",
        required_evidence="Cross-tenant call-action tests and disabled-gate tests for accept, decline, voicemail, and text_reply.",
        risk="twilio_mutation",
    ),
    SideEffectSurface(
        path="app/webhooks/telegram_callback.py",
        current_behavior="Telegram buttons can pick up, text reply, send voicemail, ignore, call back, and send follow-up texts.",
        required_gate="Legacy/admin alternate control paths must use the same backend gated-action registry as iOS.",
        required_evidence="Tests proving Telegram text/callback/follow-up paths fail closed when gates are disabled.",
        risk="user_contact",
    ),
    SideEffectSurface(
        path="app/webhooks/twilio_incoming.py",
        current_behavior="Routes incoming calls, computes trust, redirects calls, creates conferences, and can send owner SMS for deleted-app voicemail.",
        required_gate="Routing and history must be tenant-scoped; verification calls must bypass normal screening; Twilio mutations must be CallSession-bound.",
        required_evidence="Tenant route tests, verification-call spoof tests, and CallSession idempotency tests.",
        risk="twilio_mutation",
    ),
    SideEffectSurface(
        path="app/webhooks/media_stream.py",
        current_behavior="Can store full transcript in RTDB, reject streams based on RTDB race, redirect calls, and send urgent pushes with caller speech snippets.",
        required_gate="Live state is sensitive; push payloads must be lock-screen safe; media auth must be race-free.",
        required_evidence="Race test, live-state retention test, and urgent push payload snapshot test.",
        risk="sensitive_read",
    ),
    SideEffectSurface(
        path="app/db/jobs.py",
        current_behavior="Stores job cards and can list/update jobs.",
        required_gate="Job records are call-derived sensitive data with contractor ownership, retention, deletion, export, and encryption rules.",
        required_evidence="Job ownership, deletion/export, and encryption tests.",
        risk="sensitive_read",
    ),
    SideEffectSurface(
        path="app/services/job_card.py",
        current_behavior="Sends transcripts and business context to LLM extraction and logs extracted summaries.",
        required_gate="LLM prompts and extracted fields are sensitive and must be redacted in logs and covered by deletion/export policy.",
        required_evidence="Prompt/log redaction tests and extraction payload classification test.",
        risk="sensitive_read",
    ),
    SideEffectSurface(
        path="app/services/calendar.py",
        current_behavior="Refreshes Google tokens, reads free/busy data, and creates Google Calendar events.",
        required_gate="Event creation is a gated write action; token refresh and calendar data are sensitive integration operations.",
        required_evidence="Disabled calendar write tests, token handling tests, and log redaction tests.",
        risk="external_write",
    ),
    SideEffectSurface(
        path="app/services/jobber.py",
        current_behavior="Refreshes and persists Jobber tokens, reads customer/calendar data, creates jobs, and creates quotes.",
        required_gate="Jobber writes are gated actions; token persistence must use encrypted storage and payload-safe audit logs.",
        required_evidence="Disabled Jobber write tests, duplicate-prevention tests, token refresh tests, and log redaction tests.",
        risk="external_write",
    ),
    SideEffectSurface(
        path="app/api/integrations.py",
        current_behavior="OAuth connect/callback/disconnect writes and deletes Jobber and Google Calendar tokens on contractor documents.",
        required_gate="Integration tokens require state binding, contractor ownership, encrypted storage, revocation, deletion, and audit.",
        required_evidence="OAuth state replay tests, cross-tenant state tests, token encryption tests, and disconnect deletion tests.",
        risk="external_write",
    ),
    SideEffectSurface(
        path="app/api/estimates.py",
        current_behavior="Creates public estimate tokens, accepts uploads, analyzes media, stores results, and sends caller/contractor SMS.",
        required_gate="Estimate links, uploads, analysis, and result SMS are gated side effects with expiry, upload caps, and deletion/export policy.",
        required_evidence="Disabled-gate tests, upload abuse tests, token expiry tests, SMS disabled tests, and deletion/export tests.",
        risk="user_contact",
    ),
    SideEffectSurface(
        path="app/api/contractors.py",
        current_behavior="Provisions Twilio numbers, patches config, deactivates accounts, and releases phone numbers.",
        required_gate="Provisioning, deletion, and number release require confirmation, idempotency, partial-failure handling, and audit.",
        required_evidence="Duplicate provisioning, protected-field, deletion completeness, and number-release partial failure tests.",
        risk="irreversible",
    ),
    SideEffectSurface(
        path="app/services/conference.py",
        current_behavior="Adds/removes participants and ends Twilio conferences.",
        required_gate="Conference actions must be CallSession-owned and idempotent.",
        required_evidence="Conference ownership and duplicate-action tests.",
        risk="twilio_mutation",
    ),
    SideEffectSurface(
        path="app/services/warm_transfer.py",
        current_behavior="Redirects callers into conferences and sends dial-in details via Telegram.",
        required_gate="Pickup must be CallSession-owned, idempotent, and payload-safe.",
        required_evidence="Pickup ownership, duplicate pickup, rollback-to-screening, and redacted logging tests.",
        risk="twilio_mutation",
    ),
    SideEffectSurface(
        path="app/services/vcard.py",
        current_behavior="Generates signed public vCard URLs.",
        required_gate="vCard links require dedicated HMAC secret, expiry, contractor ownership at creation, and approved public fields.",
        required_evidence="HMAC secret, expiry, forged signature, and public-data review tests.",
        risk="sensitive_read",
    ),
    SideEffectSurface(
        path="app/api/vcard.py",
        current_behavior="Serves signed public contractor vCards.",
        required_gate="Public vCard downloads must be limited to approved contact data and valid signatures.",
        required_evidence="Signature rejection, expiry, and response-field tests.",
        risk="sensitive_read",
    ),
    SideEffectSurface(
        path="app/services/push_notification.py",
        current_behavior="Sends VoIP, urgent, regular, and summary pushes; can delete expired device tokens.",
        required_gate="Payloads must be lock-screen-safe; token deletion must be contractor-scoped or otherwise safe.",
        required_evidence="Push payload snapshot tests and expired-token deletion ownership tests.",
        risk="user_contact",
    ),
)


def surfaces_by_path() -> dict[str, list[SideEffectSurface]]:
    grouped: dict[str, list[SideEffectSurface]] = {}
    for surface in SIDE_EFFECT_SURFACES:
        grouped.setdefault(surface.path, []).append(surface)
    return grouped
```

- [ ] **Step 4: Create the human-readable matrix**

Create `docs/security/phase0-side-effect-matrix.md`:

```markdown
# Phase 0 Side-Effect Matrix

This matrix is the human-readable companion to `app/services/side_effect_inventory.py`.

The canonical inventory lives in code so tests can enforce coverage. During Phase 0, each row must be verified against current code, assigned a backend gate where needed, and covered by a disabled-by-default test before v2 UI or copy can rely on it.

Run:

```bash
pytest tests/unit/test_phase0_side_effect_inventory.py -q
```

Expected: inventory completeness tests pass.
```

- [ ] **Step 5: Run test to verify it passes**

Run:

```bash
pytest tests/unit/test_phase0_side_effect_inventory.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/services/side_effect_inventory.py tests/unit/test_phase0_side_effect_inventory.py docs/security/phase0-side-effect-matrix.md
git commit -m "security: inventory v2 phase 0 side effects"
```

## Task 2: Canonical Backend Gate Registry

**Files:**
- Create: `app/services/gated_actions.py`
- Create: `app/services/side_effect_audit.py`
- Create: `tests/unit/test_gated_actions.py`

- [ ] **Step 1: Write failing tests for fail-closed behavior**

Create `tests/unit/test_gated_actions.py`:

```python
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
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
pytest tests/unit/test_gated_actions.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.gated_actions'`.

- [ ] **Step 3: Create the gate registry**

Create `app/services/gated_actions.py`:

```python
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
```

- [ ] **Step 4: Create payload-safe audit helper**

Create `app/services/side_effect_audit.py`:

```python
"""Payload-safe audit events for gated side effects."""

from __future__ import annotations

from app.services.gated_actions import ActionKey, GateDecision
from app.utils.logging import get_logger

logger = get_logger(__name__)


def record_gate_decision(
    *,
    action: ActionKey,
    contractor_id: str,
    source: str,
    resource_id: str = "",
    decision: GateDecision,
) -> None:
    """Log a gate decision without caller speech, message bodies, tokens, or payloads."""
    logger.info(
        "side_effect_gate_decision",
        extra={
            "action": action.value,
            "contractor_id": contractor_id[:8] if contractor_id else "",
            "source": source,
            "resource_id": resource_id[:12] if resource_id else "",
            "allowed": decision.allowed,
            "reason": decision.reason.value,
        },
    )
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/unit/test_gated_actions.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/services/gated_actions.py app/services/side_effect_audit.py tests/unit/test_gated_actions.py
git commit -m "security: add backend gated action registry"
```

## Task 3: Call Mutation Ownership

**Files:**
- Modify: `app/api/calls.py`
- Create: `tests/unit/test_phase0_call_ownership.py`

- [ ] **Step 1: Write failing mark-read ownership test**

Create `tests/unit/test_phase0_call_ownership.py`:

```python
import pytest
from fastapi import HTTPException

from app.api import calls as calls_api


class _Doc:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _DocRef:
    def __init__(self, store, sid):
        self.store = store
        self.sid = sid

    def get(self):
        return _Doc(self.store.get(self.sid))

    def set(self, data, merge=False):
        existing = self.store.setdefault(self.sid, {})
        existing.update(data)


class _Collection:
    def __init__(self, store):
        self.store = store

    def document(self, sid):
        return _DocRef(self.store, sid)


class _DB:
    def __init__(self, store):
        self.store = store

    def collection(self, name):
        assert name == "calls"
        return _Collection(self.store)


class _State:
    contractor_id = "owner-1"
    is_admin = False


class _Request:
    state = _State()


@pytest.mark.asyncio
async def test_mark_read_rejects_mixed_owner_sids(monkeypatch):
    store = {
        "CA-owner": {"contractor_id": "owner-1", "read": False},
        "CA-other": {"contractor_id": "owner-2", "read": False},
    }
    monkeypatch.setattr(calls_api, "get_firestore_client", lambda: _DB(store))

    with pytest.raises(HTTPException) as exc:
        await calls_api.api_mark_calls_read(calls_api.MarkReadRequest(call_sids=["CA-owner", "CA-other"]), _Request())

    assert exc.value.status_code == 403
    assert store["CA-owner"]["read"] is False
    assert store["CA-other"]["read"] is False


@pytest.mark.asyncio
async def test_mark_read_updates_only_owned_sids(monkeypatch):
    store = {
        "CA-owner-1": {"contractor_id": "owner-1", "read": False},
        "CA-owner-2": {"contractor_id": "owner-1", "read": False},
    }
    monkeypatch.setattr(calls_api, "get_firestore_client", lambda: _DB(store))

    result = await calls_api.api_mark_calls_read(
        calls_api.MarkReadRequest(call_sids=["CA-owner-1", "CA-owner-2"]),
        _Request(),
    )

    assert result == {"status": "ok", "updated": 2}
    assert store["CA-owner-1"]["read"] is True
    assert store["CA-owner-2"]["read"] is True
```

- [ ] **Step 2: Run failing test**

Run:

```bash
pytest tests/unit/test_phase0_call_ownership.py -q
```

Expected: FAIL because `api_mark_calls_read` does not enforce per-SID ownership.

- [ ] **Step 3: Modify `app/api/calls.py`**

Change imports:

```python
from app.db.firestore_client import get_firestore_client
```

Replace `api_mark_calls_read` with:

```python
@router.post("/mark-read")
async def api_mark_calls_read(body: MarkReadRequest, request: Request):
    """Mark one or more owned calls as read. Persists to Firestore."""
    if not body.call_sids:
        return {"status": "ok", "updated": 0}

    import asyncio

    contractor_id = getattr(request.state, "contractor_id", "")
    is_admin = bool(getattr(request.state, "is_admin", False))
    if not contractor_id and not is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    db = get_firestore_client()
    loop = asyncio.get_event_loop()
    call_sids = body.call_sids[:100]

    def _load_owned_status():
        loaded = []
        for sid in call_sids:
            doc = db.collection("calls").document(sid).get()
            if not doc.exists:
                loaded.append((sid, None))
            else:
                loaded.append((sid, doc.to_dict()))
        return loaded

    loaded = await loop.run_in_executor(None, _load_owned_status)
    for sid, data in loaded:
        owner = (data or {}).get("contractor_id", "")
        if not data or (not is_admin and owner != contractor_id):
            logger.warning(f"Denied mark-read for call {sid[:8]}")
            raise HTTPException(status_code=403, detail="Access denied")

    async def _mark(sid: str):
        try:
            await loop.run_in_executor(
                None,
                lambda: db.collection("calls").document(sid).set({"read": True}, merge=True),
            )
        except Exception as e:
            logger.warning(f"Failed to mark call {sid[:8]} as read: {e}")

    await asyncio.gather(*[_mark(sid) for sid in call_sids])
    return {"status": "ok", "updated": len(call_sids)}
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/unit/test_phase0_call_ownership.py tests/unit/test_conference_security.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/api/calls.py tests/unit/test_phase0_call_ownership.py
git commit -m "security: enforce call mutation ownership"
```

## Task 4: SMS and MMS Gate Checks

**Files:**
- Modify: `app/services/sms.py`
- Create: `tests/unit/test_phase0_sms_gates.py`

- [ ] **Step 1: Write failing SMS gate tests**

Create `tests/unit/test_phase0_sms_gates.py`:

```python
import pytest

from app.services import sms
from app.services.gated_actions import ActionKey, GateContext


class _Messages:
    def __init__(self):
        self.created = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return type("Message", (), {"sid": "SM123"})()


class _Client:
    messages = _Messages()


@pytest.mark.asyncio
async def test_send_sms_with_disabled_gate_does_not_call_twilio(monkeypatch):
    client = _Client()
    monkeypatch.setattr(sms, "Client", lambda *_args, **_kwargs: client)

    result = await sms.send_sms(
        "+15551234567",
        "hello",
        from_number="+15557654321",
        contractor={"contractor_id": "c1"},
        action=ActionKey.CALLER_TEXT_REPLY,
        gate_context=GateContext(source="ios", actor="owner", idempotency_key="msg-1", owner_confirmed=True),
    )

    assert result is False
    assert client.messages.created == []


@pytest.mark.asyncio
async def test_send_sms_with_enabled_gate_calls_twilio(monkeypatch):
    client = _Client()
    monkeypatch.setattr(sms, "Client", lambda *_args, **_kwargs: client)

    result = await sms.send_sms(
        "+15551234567",
        "hello",
        from_number="+15557654321",
        contractor={
            "contractor_id": "c1",
            "gated_actions": {ActionKey.CALLER_TEXT_REPLY.value: True},
            "sms_compliance_status": "approved",
        },
        action=ActionKey.CALLER_TEXT_REPLY,
        gate_context=GateContext(source="ios", actor="owner", idempotency_key="msg-1", owner_confirmed=True),
    )

    assert result is True
    assert client.messages.created[0]["to"] == "+15551234567"
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/unit/test_phase0_sms_gates.py -q
```

Expected: FAIL because `send_sms` does not accept `contractor`, `action`, or `gate_context`.

- [ ] **Step 3: Modify `app/services/sms.py`**

Add imports:

```python
from app.services.gated_actions import ActionKey, GateContext, check_gated_action
from app.services.side_effect_audit import record_gate_decision
```

Change `send_sms` signature and add the gate before Twilio:

```python
async def send_sms(
    to: str,
    body: str,
    from_number: str = "",
    *,
    contractor: dict | None = None,
    action: ActionKey | None = None,
    gate_context: GateContext | None = None,
) -> bool:
    """Send an SMS via Twilio. Caller-facing sends can be gated fail-closed."""
    if action is not None:
        context = gate_context or GateContext(source="unknown", actor="system")
        decision = check_gated_action(contractor, action, context)
        record_gate_decision(
            action=action,
            contractor_id=(contractor or {}).get("contractor_id", ""),
            source=context.source,
            resource_id=context.idempotency_key,
            decision=decision,
        )
        if not decision.allowed:
            logger.info("SMS blocked by gated action registry", extra={"action": action.value, "reason": decision.reason.value})
            return False

    try:
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        loop = asyncio.get_running_loop()
        message = await loop.run_in_executor(None, lambda: client.messages.create(
            to=to,
            from_=from_number or settings.twilio_phone_number,
            body=body,
        ))
        logger.info(f"SMS sent: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"SMS send failed: {e}", exc_info=True)
        return False
```

Change `send_mms` the same way:

```python
async def send_mms(
    to: str,
    body: str,
    media_url: str,
    from_number: str = "",
    *,
    contractor: dict | None = None,
    action: ActionKey | None = None,
    gate_context: GateContext | None = None,
) -> bool:
    """Send an MMS with a media attachment. Caller-facing sends can be gated fail-closed."""
    if action is not None:
        context = gate_context or GateContext(source="unknown", actor="system")
        decision = check_gated_action(contractor, action, context)
        record_gate_decision(
            action=action,
            contractor_id=(contractor or {}).get("contractor_id", ""),
            source=context.source,
            resource_id=context.idempotency_key,
            decision=decision,
        )
        if not decision.allowed:
            logger.info("MMS blocked by gated action registry", extra={"action": action.value, "reason": decision.reason.value})
            return False

    try:
        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        loop = asyncio.get_running_loop()
        message = await loop.run_in_executor(None, lambda: client.messages.create(
            to=to,
            from_=from_number or settings.twilio_phone_number,
            body=body,
            media_url=[media_url],
        ))
        logger.info(f"MMS sent: {message.sid}")
        return True
    except Exception as e:
        logger.error(f"MMS send failed: {e}", exc_info=True)
        return False
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/unit/test_phase0_sms_gates.py tests/unit/test_gated_actions.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/services/sms.py tests/unit/test_phase0_sms_gates.py
git commit -m "security: gate caller-facing SMS sends"
```

## Task 5: Gate Existing Post-Call Side Effects

**Files:**
- Modify: `app/services/post_call.py`
- Create: `tests/unit/test_phase0_post_call_gates.py`

- [ ] **Step 1: Write failing post-call gate tests**

Create `tests/unit/test_phase0_post_call_gates.py`:

```python
import pytest

from app.services import post_call
from app.services.gated_actions import ActionKey


@pytest.mark.asyncio
async def test_business_post_call_does_not_contact_caller_or_jobber_when_gates_disabled(monkeypatch):
    sent_sms = []
    sent_mms = []
    created_jobs = []

    async def fake_extract_job_card(*_args, **_kwargs):
        return {
            "caller_phone": "+15551234567",
            "caller_name": "Pat",
            "call_type": "service_request",
            "issue_description": "leaky faucet",
        }

    async def fake_save_call(*_args, **_kwargs):
        return None

    async def fake_get_job_by_call_sid(_sid):
        return None

    async def fake_save_job(data):
        return "job-1"

    async def fake_send_sms(*args, **kwargs):
        sent_sms.append((args, kwargs))
        return True

    async def fake_send_mms(*args, **kwargs):
        sent_mms.append((args, kwargs))
        return True

    async def fake_create_jobber_job(*args, **kwargs):
        created_jobs.append((args, kwargs))

    monkeypatch.setattr(post_call, "extract_job_card", fake_extract_job_card)
    monkeypatch.setattr("app.db.calls.save_call", fake_save_call)
    monkeypatch.setattr("app.db.jobs.get_job_by_call_sid", fake_get_job_by_call_sid)
    monkeypatch.setattr(post_call, "save_job", fake_save_job)
    monkeypatch.setattr(post_call, "send_sms", fake_send_sms)
    monkeypatch.setattr(post_call, "send_mms", fake_send_mms)
    monkeypatch.setattr(post_call, "_create_jobber_job", fake_create_jobber_job)

    await post_call._process_business_call(
        call_sid="CA123",
        transcript_text="Caller: I need a leaky faucet fixed",
        caller_phone="+15551234567",
        contractor_phone="+15550000000",
        twilio_number="+15559999999",
        contractor={
            "contractor_id": "c1",
            "jobber_access_token": "token",
            "owner_name": "Owner",
            "business_name": "Owner Plumbing",
        },
    )

    caller_side_effects = [
        item for item in sent_sms + sent_mms
        if item[1].get("action") in {
            ActionKey.CALLER_CONFIRMATION_SMS,
            ActionKey.CALLER_CONFIRMATION_MMS,
            ActionKey.CALLER_VCARD_MMS,
            ActionKey.CALLER_AUTO_REPLY,
        }
    ]
    assert caller_side_effects == []
    assert created_jobs == []
```

- [ ] **Step 2: Run failing test**

Run:

```bash
pytest tests/unit/test_phase0_post_call_gates.py -q
```

Expected: FAIL because post-call processing sends caller SMS/MMS or creates Jobber jobs without backend gate checks.

- [ ] **Step 3: Modify `app/services/post_call.py` imports**

Add:

```python
from app.services.gated_actions import ActionKey, GateContext, check_gated_action
from app.services.side_effect_audit import record_gate_decision
```

- [ ] **Step 4: Add local helper in `app/services/post_call.py`**

Add near other helpers:

```python
def _post_call_gate(contractor: dict, action: ActionKey, call_sid: str, *, owner_confirmed: bool = False):
    context = GateContext(
        source="post_call",
        actor="system",
        idempotency_key=f"{call_sid}:{action.value}",
        owner_confirmed=owner_confirmed,
    )
    decision = check_gated_action(contractor, action, context)
    record_gate_decision(
        action=action,
        contractor_id=contractor.get("contractor_id", ""),
        source="post_call",
        resource_id=call_sid,
        decision=decision,
    )
    return decision, context
```

- [ ] **Step 5: Gate Jobber auto-create**

Change the Jobber auto-create block:

```python
    if contractor.get("jobber_access_token") and job_data.get("call_type") == "service_request":
        decision, _context = _post_call_gate(contractor, ActionKey.JOBBER_CREATE_JOB, call_sid)
        if decision.allowed:
            asyncio.create_task(_create_jobber_job(contractor, job_data))
        else:
            logger.info("Jobber auto-create blocked by gate", extra={"reason": decision.reason.value})
```

- [ ] **Step 6: Gate caller confirmation SMS/MMS and vCard MMS**

Use this pattern for caller-facing sends:

```python
        decision, context = _post_call_gate(contractor, ActionKey.CALLER_CONFIRMATION_SMS, call_sid)
        if decision.allowed:
            if vcard_url:
                await send_mms(
                    caller_phone,
                    caller_sms,
                    media_url=vcard_url,
                    from_number=twilio_number,
                    contractor=contractor,
                    action=ActionKey.CALLER_CONFIRMATION_MMS,
                    gate_context=context,
                )
            else:
                await send_sms(
                    caller_phone,
                    caller_sms,
                    from_number=twilio_number,
                    contractor=contractor,
                    action=ActionKey.CALLER_CONFIRMATION_SMS,
                    gate_context=context,
                )
        else:
            logger.info("Caller confirmation SMS blocked by gate", extra={"reason": decision.reason.value})
```

For non-service vCard MMS:

```python
            decision, context = _post_call_gate(contractor, ActionKey.CALLER_VCARD_MMS, call_sid)
            if decision.allowed:
                await send_mms(
                    caller_phone,
                    msg,
                    media_url=vcard_url,
                    from_number=twilio_number,
                    contractor=contractor,
                    action=ActionKey.CALLER_VCARD_MMS,
                    gate_context=context,
                )
            else:
                logger.info("vCard MMS blocked by gate", extra={"reason": decision.reason.value})
```

For auto-reply:

```python
        if contractor.get("auto_reply_sms", False):
            decision, _context = _post_call_gate(contractor, ActionKey.CALLER_AUTO_REPLY, call_sid)
            if decision.allowed:
                await _send_auto_reply(caller_phone, contractor, twilio_number, transcript_text, caller_language=caller_language)
            else:
                logger.info("Auto-reply blocked by gate", extra={"reason": decision.reason.value})
```

- [ ] **Step 7: Gate estimate token creation**

Inside `_format_caller_sms_with_estimate`, before the `httpx.AsyncClient()` call:

```python
            decision, _context = _post_call_gate(contractor, ActionKey.ESTIMATE_TOKEN_CREATE, job_data.get("call_sid", ""))
            if not decision.allowed:
                logger.info("Estimate token creation blocked by gate", extra={"reason": decision.reason.value})
                return base_msg
```

- [ ] **Step 8: Run tests**

Run:

```bash
pytest tests/unit/test_phase0_post_call_gates.py tests/unit/test_phase0_sms_gates.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

Run:

```bash
git add app/services/post_call.py tests/unit/test_phase0_post_call_gates.py
git commit -m "security: gate post-call outbound side effects"
```

## Task 6: Gate iOS and Telegram Text/Callback Paths

**Files:**
- Modify: `app/api/voip.py`
- Modify: `app/webhooks/telegram_callback.py`
- Create: `tests/unit/test_phase0_action_gates.py`

- [ ] **Step 1: Write failing action gate tests**

Create `tests/unit/test_phase0_action_gates.py`:

```python
import pytest

from app.api import voip as voip_api


class _ActiveCall:
    contractor_id = "c1"
    caller_phone = "+15551234567"


@pytest.mark.asyncio
async def test_voip_text_reply_fails_closed_without_sms_gate(monkeypatch):
    sent = []

    async def fake_get_active_call(_sid):
        return _ActiveCall()

    async def fake_get_contractor(_cid):
        return {"contractor_id": "c1", "twilio_number": "+15559999999"}

    async def fake_send_sms(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    monkeypatch.setattr("app.db.cache.get_active_call", fake_get_active_call)
    monkeypatch.setattr("app.db.contractors.get_contractor", fake_get_contractor)
    monkeypatch.setattr("app.services.sms.send_sms", fake_send_sms)

    result = await voip_api._handle_text_reply("CA123", "hello", "c1")

    assert result["status"] == "error"
    assert result["message"] == "Texting is not enabled for this account."
    assert sent == []
```

- [ ] **Step 2: Run failing test**

Run:

```bash
pytest tests/unit/test_phase0_action_gates.py -q
```

Expected: FAIL because `_handle_text_reply` sends without a gate.

- [ ] **Step 3: Modify `app/api/voip.py` text reply**

Inside `_handle_text_reply`, after contractor lookup:

```python
    from app.services.gated_actions import ActionKey, GateContext, check_gated_action
    from app.services.side_effect_audit import record_gate_decision

    contractor = None
    from_number = ""
    if contractor_id:
        from app.db.contractors import get_contractor
        contractor = await get_contractor(contractor_id)
        if contractor:
            from_number = contractor.get("twilio_number", "")

    context = GateContext(
        source="ios",
        actor="owner",
        idempotency_key=f"{call_sid}:text_reply",
        owner_confirmed=True,
    )
    decision = check_gated_action(contractor, ActionKey.CALLER_TEXT_REPLY, context)
    record_gate_decision(
        action=ActionKey.CALLER_TEXT_REPLY,
        contractor_id=contractor_id,
        source="ios",
        resource_id=call_sid,
        decision=decision,
    )
    if not decision.allowed:
        return {"status": "error", "message": decision.message}
```

Then call:

```python
    success = await send_sms(
        active_call.caller_phone,
        body,
        from_number=from_number,
        contractor=contractor,
        action=ActionKey.CALLER_TEXT_REPLY,
        gate_context=context,
    )
```

- [ ] **Step 4: Gate Telegram text paths**

In `app/webhooks/telegram_callback.py`, before `_handle_text_reply` calls `send_text_reply` and before `_handle_text_them` calls `send_followup_text`, load the call owner and contractor, then use `check_gated_action` with:

```python
GateContext(source="telegram", actor="operator", idempotency_key=f"{call_sid}:telegram_text", owner_confirmed=True)
```

Use `ActionKey.CALLER_TEXT_REPLY` for active text reply and `ActionKey.CALLER_AUTO_REPLY` for post-call follow-up. If denied, call `answer_callback_query("", decision.message)` and return before SMS.

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/unit/test_phase0_action_gates.py tests/unit/test_conference_security.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/api/voip.py app/webhooks/telegram_callback.py tests/unit/test_phase0_action_gates.py
git commit -m "security: gate alternate text action paths"
```

## Task 7: Gate Voice Tool Integration Writes

**Files:**
- Modify: `app/services/voice_pipeline.py`
- Create: `tests/unit/test_phase0_voice_tool_gates.py`

- [ ] **Step 1: Write failing voice tool gate tests**

Create `tests/unit/test_phase0_voice_tool_gates.py`:

```python
import json
import pytest

from app.services.voice_pipeline import VoicePipeline


async def _noop(*_args, **_kwargs):
    return None


def _pipeline(config):
    return VoicePipeline(
        on_audio_out=_noop,
        on_transcript=_noop,
        on_call_complete=_noop,
        call_sid="CA123",
        contractor_config=config,
    )


@pytest.mark.asyncio
async def test_jobber_book_appointment_returns_disabled_without_gate(monkeypatch):
    created = []

    async def fake_create_job(*args, **kwargs):
        created.append((args, kwargs))
        return "jobber-1"

    monkeypatch.setattr("app.services.jobber.create_job", fake_create_job)

    pipeline = _pipeline({"contractor_id": "c1", "jobber_access_token": "token"})
    result = json.loads(await pipeline._execute_tool("book_appointment", {"title": "Repair"}))

    assert result == {"success": False, "error": "Owner confirmation is required for this action."}
    assert created == []
```

- [ ] **Step 2: Run failing test**

Run:

```bash
pytest tests/unit/test_phase0_voice_tool_gates.py -q
```

Expected: FAIL because `_execute_tool` writes integrations without checking the gate.

- [ ] **Step 3: Modify `app/services/voice_pipeline.py`**

Add imports inside `_execute_tool`:

```python
        from app.services.gated_actions import ActionKey, GateContext, check_gated_action
        from app.services.side_effect_audit import record_gate_decision
```

Before Google Calendar `gcal_book`:

```python
                    context = GateContext(source="voice_tool", actor="automation", idempotency_key=f"{self._call_sid}:google_create_event")
                    decision = check_gated_action(self._contractor_config, ActionKey.GOOGLE_CREATE_EVENT, context)
                    record_gate_decision(
                        action=ActionKey.GOOGLE_CREATE_EVENT,
                        contractor_id=self._contractor_config.get("contractor_id", ""),
                        source="voice_tool",
                        resource_id=self._call_sid,
                        decision=decision,
                    )
                    if not decision.allowed:
                        return json.dumps({"success": False, "error": decision.message})
```

Before Jobber `create_job`:

```python
                context = GateContext(source="voice_tool", actor="automation", idempotency_key=f"{self._call_sid}:jobber_create_job")
                decision = check_gated_action(self._contractor_config, ActionKey.JOBBER_CREATE_JOB, context)
                record_gate_decision(
                    action=ActionKey.JOBBER_CREATE_JOB,
                    contractor_id=self._contractor_config.get("contractor_id", ""),
                    source="voice_tool",
                    resource_id=self._call_sid,
                    decision=decision,
                )
                if not decision.allowed:
                    return json.dumps({"success": False, "error": decision.message})
```

- [ ] **Step 4: Ensure Gemini cannot bypass tool gates**

Search for Gemini tool execution:

```bash
rg -n "tool|function|book_appointment|create_job|calendar" app/services/gemini_pipeline.py
```

If Gemini delegates to `VoicePipeline` tool handling, add a test comment in `tests/unit/test_phase0_voice_tool_gates.py` asserting the shared helper path. If Gemini has its own write tool executor, apply the same `check_gated_action` pattern there.

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/unit/test_phase0_voice_tool_gates.py tests/unit/test_jobber.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/services/voice_pipeline.py app/services/gemini_pipeline.py tests/unit/test_phase0_voice_tool_gates.py
git commit -m "security: gate voice integration write tools"
```

## Task 8: Gate Estimate Result SMS

**Files:**
- Modify: `app/api/estimates.py`
- Create: `tests/unit/test_phase0_estimate_gates.py`

- [ ] **Step 1: Write failing estimate SMS gate test**

Create `tests/unit/test_phase0_estimate_gates.py`:

```python
import pytest

from app.api import estimates


@pytest.mark.asyncio
async def test_estimate_result_sms_fails_closed_when_sms_gate_disabled(monkeypatch):
    sent = []

    async def fake_get_contractor(_cid):
        return {"contractor_id": "c1", "twilio_number": "+15559999999", "owner_phone": "+15550000000"}

    async def fake_analyze_media(**_kwargs):
        return {"diagnosis": "leak", "estimate_min": 100, "estimate_max": 200, "confidence": "medium"}

    async def fake_send_sms(*args, **kwargs):
        sent.append((args, kwargs))
        return True

    monkeypatch.setattr(estimates, "_get_estimate_doc", lambda _token: {
        "contractor_id": "c1",
        "caller_phone": "+15551234567",
        "upload_count": 0,
    })
    monkeypatch.setattr(estimates, "get_contractor", fake_get_contractor)
    monkeypatch.setattr(estimates, "analyze_media", fake_analyze_media)
    monkeypatch.setattr(estimates, "send_sms", fake_send_sms)

    class _Request:
        headers = {"content-type": "image/jpeg", "content-length": "4"}

        async def stream(self):
            yield b"data"

    class _Doc:
        def update(self, _data):
            return None

    class _Collection:
        def document(self, _key):
            return _Doc()

    class _DB:
        def collection(self, _name):
            return _Collection()

    monkeypatch.setattr(estimates, "get_firestore_client", lambda: _DB())

    result = await estimates.upload_and_analyze("token", request=_Request())

    assert result["status"] == "ok"
    assert sent == []
```

- [ ] **Step 2: Run failing test**

Run:

```bash
pytest tests/unit/test_phase0_estimate_gates.py -q
```

Expected: FAIL because estimate endpoint sends SMS without gate checks.

- [ ] **Step 3: Modify `app/api/estimates.py`**

Add imports:

```python
from app.services.gated_actions import ActionKey, GateContext, check_gated_action
from app.services.side_effect_audit import record_gate_decision
```

Before sending SMS to caller:

```python
        context = GateContext(source="estimate", actor="system", idempotency_key=f"{token}:caller_sms")
        decision = check_gated_action(contractor, ActionKey.ESTIMATE_RESULT_SMS, context)
        record_gate_decision(
            action=ActionKey.ESTIMATE_RESULT_SMS,
            contractor_id=estimate["contractor_id"],
            source="estimate",
            resource_id=token_hash[:12],
            decision=decision,
        )
        if decision.allowed:
            await send_sms(
                caller_phone,
                customer_msg,
                from_number=twilio_number,
                contractor=contractor,
                action=ActionKey.ESTIMATE_RESULT_SMS,
                gate_context=context,
            )
        else:
            logger.info("Estimate caller SMS blocked by gate", extra={"reason": decision.reason.value})
```

Before sending SMS to contractor, do not use caller SMS gate. Owner notification can remain ungated for Phase 0, but ensure logs redact phone and do not include diagnosis text.

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/unit/test_phase0_estimate_gates.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/api/estimates.py tests/unit/test_phase0_estimate_gates.py
git commit -m "security: gate estimate result SMS"
```

## Task 9: Lock-Screen-Safe Push Payloads

**Files:**
- Modify: `app/webhooks/media_stream.py`
- Modify: `app/services/post_call.py`
- Create: `tests/unit/test_phase0_push_payloads.py`

- [ ] **Step 1: Write failing push payload tests**

Create `tests/unit/test_phase0_push_payloads.py`:

```python
from app.webhooks import media_stream
from app.services import post_call


def test_urgent_push_body_does_not_include_raw_speech():
    body = media_stream._safe_urgent_push_body(caller_name="Pat Customer", caller_phone="+15551234567")

    assert "Caller says:" not in body
    assert "+15551234567" not in body
    assert body == "Urgent call needs review. Open Kevin for details."


def test_summary_push_body_does_not_include_issue_details():
    body = post_call._safe_summary_push_body(
        caller_name="Pat Customer",
        call_type="service_request",
        urgency="emergency",
    )

    assert "Pat Customer" not in body
    assert "emergency" in body.lower()
    assert "Open Kevin" in body
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/unit/test_phase0_push_payloads.py -q
```

Expected: FAIL because helper functions do not exist.

- [ ] **Step 3: Add urgent push helper**

In `app/webhooks/media_stream.py`, add near urgency handling:

```python
def _safe_urgent_push_body(caller_name: str = "", caller_phone: str = "") -> str:
    """Return lock-screen-safe urgent call copy with no raw speech or full phone."""
    return "Urgent call needs review. Open Kevin for details."
```

Change:

```python
body = f"Caller says: {caller_name or caller_phone} - {transcript_snippet[:150]}"
```

to:

```python
body = _safe_urgent_push_body(caller_name=caller_name, caller_phone=caller_phone)
```

- [ ] **Step 4: Add summary push helper**

In `app/services/post_call.py`, add:

```python
def _safe_summary_push_body(caller_name: str, call_type: str, urgency: str = "") -> str:
    """Return lock-screen-safe summary copy with no raw issue text."""
    if urgency and urgency not in ("none", ""):
        return f"New {urgency} call summary. Open Kevin for details."
    if call_type == "service_request":
        return "New service call summary. Open Kevin for details."
    return "New call summary. Open Kevin for details."
```

Inside `_send_summary_push`, replace body construction with:

```python
        body = _safe_summary_push_body(caller_name=caller_name, call_type=call_type, urgency=urgency)
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/unit/test_phase0_push_payloads.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/webhooks/media_stream.py app/services/post_call.py tests/unit/test_phase0_push_payloads.py
git commit -m "privacy: make call push payloads lock-screen safe"
```

## Task 10: Phase 0 Verification Sweep

**Files:**
- Modify: `docs/security/phase0-side-effect-matrix.md`

- [ ] **Step 1: Update matrix with verification commands**

Append to `docs/security/phase0-side-effect-matrix.md`:

```markdown
## Phase 0 Verification Commands

Run the focused Phase 0 suite:

```bash
pytest \
  tests/unit/test_phase0_side_effect_inventory.py \
  tests/unit/test_gated_actions.py \
  tests/unit/test_phase0_call_ownership.py \
  tests/unit/test_phase0_sms_gates.py \
  tests/unit/test_phase0_post_call_gates.py \
  tests/unit/test_phase0_action_gates.py \
  tests/unit/test_phase0_voice_tool_gates.py \
  tests/unit/test_phase0_estimate_gates.py \
  tests/unit/test_phase0_push_payloads.py \
  -q
```

Run the adjacent security regression suite:

```bash
pytest \
  tests/unit/test_conference_security.py \
  tests/unit/test_security_audit_medium.py \
  tests/unit/test_security_audit_f9_f10_f11.py \
  tests/unit/test_jobber.py \
  tests/unit/test_twilio_provisioning.py \
  tests/unit/test_voip_token.py \
  -q
```

Run full backend tests before PR:

```bash
pytest --tb=short -q
```
```

- [ ] **Step 2: Run focused Phase 0 suite**

Run:

```bash
pytest \
  tests/unit/test_phase0_side_effect_inventory.py \
  tests/unit/test_gated_actions.py \
  tests/unit/test_phase0_call_ownership.py \
  tests/unit/test_phase0_sms_gates.py \
  tests/unit/test_phase0_post_call_gates.py \
  tests/unit/test_phase0_action_gates.py \
  tests/unit/test_phase0_voice_tool_gates.py \
  tests/unit/test_phase0_estimate_gates.py \
  tests/unit/test_phase0_push_payloads.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run adjacent security regression suite**

Run:

```bash
pytest \
  tests/unit/test_conference_security.py \
  tests/unit/test_security_audit_medium.py \
  tests/unit/test_security_audit_f9_f10_f11.py \
  tests/unit/test_jobber.py \
  tests/unit/test_twilio_provisioning.py \
  tests/unit/test_voip_token.py \
  -q
```

Expected: PASS.

- [ ] **Step 4: Run full backend tests**

Run:

```bash
pytest --tb=short -q
```

Expected: PASS.

- [ ] **Step 5: Confirm no production deploy happened**

Run:

```bash
git branch --show-current
git status --short --branch
```

Expected: branch is `codex/v2-phase0-safety-audit`, not `main`; worktree is clean after commits.

- [ ] **Step 6: Commit verification docs**

Run:

```bash
git add docs/security/phase0-side-effect-matrix.md
git commit -m "docs: record phase 0 verification matrix"
```

## Phase 0 Exit Criteria

Phase 0 is complete when:

- The side-effect inventory covers every path in Section 18.1 of the spec.
- Backend gates are canonical, default-off in production, and fail closed.
- Caller-facing SMS/MMS, estimate links/results, vCard MMS, Jobber writes, Google Calendar writes, Telegram text/follow-up, and iOS text reply are disabled unless backend gates allow them.
- `mark-read` and other call mutations enforce per-resource contractor ownership.
- Push payloads no longer expose raw caller speech or detailed issue text on the lock screen.
- Focused Phase 0 tests pass.
- Adjacent security regression tests pass.
- Full backend pytest passes.
- Work is on a clean `codex/` branch or clean worktree, not dirty/diverged local `main`.

## Do Not Start Yet

Do not start these until Phase 0 exits:

- Dispatch UI.
- Calls queue UI.
- Kevin control center UI.
- Verified forwarding implementation.
- New job-card extraction schema.
- A2P enablement.
- Booking or integration write enablement.
- Production deployment.
