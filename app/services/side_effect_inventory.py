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
        required_gate="Core live-call controls accept, decline, and voicemail remain ownership-only with CallSession/idempotency protections; text_reply requires the caller-text backend gate.",
        required_evidence="Cross-tenant call-action tests for accept, decline, voicemail, and text_reply; disabled-gate tests for text_reply.",
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
