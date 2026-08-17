# Receptionist Wedge Sequencing Plan

> **For agentic workers:** This file sequences independent slices. Implement
> only the linked plan for the current slice. Do not treat later slices as
> authorized work. REQUIRED SUB-SKILL for the current slice:
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans`.

**Goal:** Ship the next product-visible receptionist improvement now that the
Boston public demo works, without mixing A2P-blocked SMS, a 13k-line default-off
memory merge, or live Jobber booking into the same PR.

**Architecture:** Four already-separate subsystems. Each slice must produce
working, testable software on its own. Later slices may depend on earlier ones,
but none of them share a deploy or a feature flag.

**Tech Stack:** Python 3.12, FastAPI, Gemini Live, existing `IntakeState` /
`DialoguePlanner` / `InstructionComposer`, Firestore, StoreKit (unchanged).

## Global Constraints

- Do not deploy `kevin-api` or otherwise change production Cloud Run in GCP
  project `kevin-491315` without explicit owner authorization.
- Do not iterate public-demo greeting/legal/weekday UX unless a live demo
  regression is reported. `PublicDemoGeminiPipeline` subclasses
  `GeminiPipeline` and will inherit business-path intake automatically.
- Do not auto-book calendar events. Contractor confirm remains required.
- Do not prepend `PUBLIC_DEMO_DISCLOSURE` or any legal dump to spoken greetings.
- Do not merge or rebase PR #165 (`codex/customer-memory`) inside the live-intake
  slice. Leave `.worktrees/customer-memory` untouched until Slice 2.
- Do not add language phrase tables to `IntakeState`, `DialoguePlanner`, or
  `InstructionComposer`.
- Do not enable caller-facing SMS, MMS, auto-reply, vCard, or hang-up texts
  until `sms_compliance_status == "approved"` and A2P/10DLC is approved.
- Do not expose Jobber booking tools. Post-call Jobber Request lead capture is
  already shipped behind `jobber_lead_capture_enabled`.
- Fail closed on intake-controller errors: the call continues on the static
  system prompt.
- Log action names and exception types only. No transcript, phone, or name in
  intake logs.

---

## Why this order

Smith.ai AI Receptionist buyers (G2, n=21) keep the product and ask for finer
intake plus a human escape hatch. Jobber Home Service Community thread 2735 is
the trades-user complaint that matches Kevin today: the bot takes a booking and
never asks what is wrong.

Kevin already has the offline policy for that complaint. It is not live.

| Evidence | Kevin today | Next slice |
| --- | --- | --- |
| Jobber 2735: booking with no job details | Offline planner asks `service_action` then `service_object` before callback. Gemini Live ignores it. | Slice 1: live intake controller |
| Returning caller continuity | Draft PR #165, default-off, not on `main` | Slice 2: land memory with flags off |
| Owner Confirm → calendar → caller SMS | Receipt + owner SMS only (PR #156). No iOS Confirm API. Caller SMS gated on A2P. | Slice 3 |
| Hang-up / abandoned caller SMS | No dedicated path. Status webhook does not SMS. | Slice 4, A2P-blocked |
| Jobber Request lead capture | Shipped, admin flag off | No new plan |

Visual diagnosis, voice bakeoff, Dispatch rewrite, nine-country i18n, and the
admin dashboard stay on hold until Slice 1 is on `main` and a staging plumber
call proves Kevin asks the job before collecting a form.

---

## Slice 1 — Live Gemini intake controller (current)

**Plan:** `docs/superpowers/plans/2026-08-17-live-intake-controller.md`

**Ships:** Business-mode Gemini calls get per-turn `plan_next_action` /
`compose_turn_instructions` over the existing `_send_client_instruction` path.
Asked slots are marked from Kevin's turn so "schedule an appointment" cannot
skip the job questions on the next turn.

**Does not ship:** transcript→`CallerObservation` LLM extraction, personal-mode
changes, ElevenLabs `VoicePipeline` wiring, PR #165, caller SMS, demo-only
behavior.

**Done when:** unit tests prove the plumber-schedule instruction sequence, Gemini
business wiring, personal/ElevenLabs isolation, and fail-closed errors. A staging
plumber call is the live proof; unit tests do not claim live audio behavior.

---

## Slice 2 — Durable customer memory (after Slice 1)

**Existing code:** draft [PR #165](https://github.com/delimatsuo/heykevin/pull/165)
at `4f99e8bbcdae77081786798990e1f6d922158c68`, worktree
`.worktrees/customer-memory`. CI Test is green. Flags default false.

**This slice is a merge-readiness plan, not a greenfield rewrite.** Write it only
after Slice 1 is on `main`. Required contents at that time:

- Rebase onto current `main` (Slice 1 will have changed `gemini_pipeline.py`).
- Keep `customer_memory_capture_enabled`,
  `customer_memory_personalization_enabled`, and
  `service_request_mutations_enabled` default false and PROTECTED.
- Do not activate mutations until ANI-auth and reschedule-concurrency P0s from
  the PR #165 handoff are closed.
- Do not put memory cards in RTDB or lock-screen push.
- Optional later: feed confirmed memory into `LiveIntakeController.start`
  (`caller_name`, `caller_confidence`, `caller_source`) so returning callers skip
  name. That is a follow-on task, not part of Slice 1.

---

## Slice 3 — Appointment confirm loop (after Slice 1)

**Already shipped:** gated `book_appointment` returns
`{"status": "request_recorded", "booked": false, ...}` and writes
`appointment_request` with `status: "pending_owner_confirmation"`. Owner SMS
leads with that slot.

**Not shipped:** iOS Confirm control, `POST` owner-confirm API, calendar write
on confirm, caller confirmation SMS.

**Blocker:** caller SMS cannot send until A2P. Owner-confirm → Google Calendar
write can ship without texting the caller. Do not wait for A2P to build the
Confirm tap, but do not promise the caller a text.

Write this plan only after Slice 1 is on `main`. Keep Jobber `book_appointment`
absent.

---

## Slice 4 — Abandoned-call caller SMS (blocked)

**Already shipped:** owner missed-call / lead SMS on media-stream post-call.
Caller auto-reply exists and is gated on `CALLER_AUTO_REPLY` plus
`sms_compliance_status == "approved"`.

**Not shipped:** hang-up / incomplete / no-answer caller SMS.
`POST /webhooks/twilio/status` writes status and clears RTDB; it does not SMS.

**Hard blocker:** production has no `sms_compliance_status` approved contractors.
iOS `kTextReplyEnabled = false`. Do not write implementation tasks for this
slice until A2P/10DLC is approved and the owner authorizes caller-SMS flags.

---

## Already shipped — do not reopen

- Public demo short greeting, ISO `preferred_date`, weekday parser (PRs #171–#173).
- Contractor-confirm, not auto-book (PR #156).
- CallKit live takeover.
- Jobber post-call Request lead capture behind admin flag
  (`docs/superpowers/plans/2026-07-05-jobber-lead-capture.md` checkboxes are
  stale; the code is in tree).
- Offline hybrid receptionist controller
  (`docs/superpowers/plans/2026-07-09-hybrid-stateful-ai-receptionist.md`).
- Automatic language detection. Do not rebuild i18n as the next project.

---

## Execution

Start Slice 1 in an isolated worktree from `origin/main`. Do not commit the
untracked public-demo handoff markdown files unless the owner asks.
