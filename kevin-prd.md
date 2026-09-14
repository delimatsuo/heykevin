# Hey Kevin — Current Product Requirements

**Updated:** 2026-09-14
**Owner:** Deli Matsuo
**Source reviewed:** `15d11f3d31fa69f9d31e1c026572d58126301a41`
**Scope decision:** Finish the planned screening-notification enhancement. Work
only on necessary improvements; keep valuable later features in this PRD rather
than starting them automatically.

This is the current product scope. It replaces the original Telegram-first,
single-user PRD and the broader unapproved Business Dispatch v2 plan as the guide
to what to build next. Historical designs remain references for individual
contracts, not an active implementation queue. The [current roadmap](docs/current-roadmap.md)
records shipped, unfinished, and deferred status with evidence.

## Product and users

Kevin is an iPhone call-screening assistant and AI receptionist. It answers
forwarded calls, identifies the caller and purpose, lets the owner follow the
conversation and take over, and retains a useful summary. Business mode adds
structured intake and owner-confirmed appointment requests. Personal mode stays
simple and does not invoke business scheduling or CRM tools.

The primary job is to help an owner decide whether to answer now or follow up
later. Reliability, understandable setup, and useful caller information matter
more than a new tab layout or a larger feature list.

The current product uses native iOS, APNs/CallKit, a Python backend on Cloud Run,
Firestore/RTDB, Twilio and the configured voice providers. This PRD does not change
provider selection, pricing, subscriptions, or existing account boundaries.

## Necessary work now

### N1 — Finish the screening notification enhancement

**Problem:** An owner receiving an incoming-call alert must open the app to learn
who is calling and why before deciding whether to answer.

**Required experience:**

1. When Kevin has screened the caller and asks them to hold, update that call's
   notification with a short caller name and reason.
2. Update the existing call notification in place rather than producing a series
   of unrelated alerts.
3. Tapping the notification opens the matching call's live view. The Pick Up action
   targets that call when it is still answerable.
4. Extraction or push failure must preserve normal screening, transcription, the
   existing hold behavior, and ordinary pickup.
5. Ended or different calls must not be answered because an older notification
   was tapped. The existing device notification-preview settings remain relevant
   to what is visible while locked.

**Release acceptance:**

- [ ] The reviewed notification backend is deployed to production and `/health`
      identifies the approved SHA.
- [ ] On the owner's iPhone, the incoming alert updates with caller and reason
      during screening; lock-screen and unlocked behavior are checked.
- [ ] Banner navigation and Pick Up target the same call; ending the call or
      tapping an old alert does not answer another call.
- [ ] A notification failure leaves the live call usable and does not interrupt
      the hold or transcript.
- [ ] Delayed extraction or a stale task cannot send a new actionable screening
      summary after pickup, hangup, or the end of the screening wait.
- [ ] Record the installed iOS version and build used for this check. Public App
      Store version metadata alone does not identify that build.

Existing PR #239 contains this enhancement and client feedback/review code.
This scope does not add another feedback system, redesign the app, or expand
caller SMS. The exact release candidate and approval state are recorded in the
[current roadmap](docs/current-roadmap.md#pending-notification-release).

### N2 — Fix confirmed regressions affecting the core call flow

Repair a reproducible defect that blocks setup, screening, transcript delivery,
correct call pickup, or subscription access. A proposed improvement does not
become a regression simply because an older plan listed it. Each repair needs a
concrete failing case, a bounded change, and verification of the affected flow.

## Valuable later features — not active implementation

The order below is a recommendation. None is a prerequisite to shipping N1 unless
its absence demonstrably prevents N1 from meeting its acceptance criteria. Promote
one feature at a time only after an explicit owner scope decision and definition
of the problem, minimum scope, and acceptance checks; do not begin the entire
former v2 program.

| Priority | Feature | Why it may be worth adding | Minimum requirements and acceptance | Start condition |
|---|---|---|---|---|
| Next candidate | Verified forwarding readiness | An owner should know whether Kevin can actually receive forwarded calls. | Distinguish skipped, unverified, verified, and stale setup; verification belongs to the server; a direct call to the Kevin number is not forwarding proof; phone/carrier changes invalidate prior readiness. Keep the existing app navigation. | Complete N1, then define a bounded verification flow and the necessary owner-run carrier test. |
| Later | Calls needing follow-up | Useful if owners lose leads in a chronological history. | Add a small persistent action state and a needs-follow-up filter to Recents; a completed action survives reload; owner and call boundaries are preserved. A three-tab redesign is not required. | Confirm missed follow-ups are a real problem and specify the smallest useful workflow. |
| Later | Returning-customer continuity | Avoid asking a repeat caller for information already safely known. | Reuse the existing memory and request contracts; distinguish trusted identity from caller ID; verify retention, deletion, recovery, and provider prerequisites before activation. | Establish value with an owner-approved qualification plan; source defaults are not evidence of current runtime configuration. |
| Later | Photo/video diagnosis and estimates | May help a tradesperson assess a job before visiting. | Reuse the existing video-first design; complete caller upload/watch experience, bounded media processing and recovery, retention/deletion, and owner/caller result delivery. | Confirm demand and qualify infrastructure, media handling and delivery before enabling the feature. |
| Later | Secure account-phone change | Customers who change phone numbers need a safe way to retain their account. | Verify possession, prevent ownership collisions, and atomically update the protected phone pair with honest failure recovery. | Resolve the open product/provider choices in the existing [phone-rebind spec](docs/specs/phone-rebind-possession-verified.md); do not reopen generic PATCH access. |
| Later | Broader international qualification | Existing forwarding UI is only useful where the carrier and number setup work. | Qualify the supported country/carrier combinations, regional numbers, regulatory address requirements, forwarding target format, and physical-device flow. | Prioritize countries with actual demand and obtain a bounded owner-run test envelope. Do not equate store release notes with carrier qualification. |

## Deferred ideas and superseded plans

- **Full Dispatch / Calls / Kevin redesign:** deferred. Solve a demonstrated
  readiness or follow-up problem in the current app before replacing its
  navigation or building a general work-management system.
- **WhatsApp notifications, voice cloning, and multiple forwarding numbers:**
  historical future ideas, with no current implementation commitment.
- **A new voice architecture or another public-demo iteration:** not part of this
  work without a demonstrated problem in the production receptionist flow.
- **Telegram-first control flow, the original Vapi/Fish Audio architecture, and
  the original Free/Pro/Premium pricing:** superseded assumptions; do not use them
  to direct new implementation or change the live product.
- **Legacy Apple lookup removal:** maintenance deferred until the adoption
  requirement in [issue #33](https://github.com/delimatsuo/heykevin/issues/33) is
  verified and removal is explicitly authorized.

## Definition of completion

N1 is complete only when deployment and the relevant owner-device checks are
recorded. A green unit suite, merged PR, TestFlight upload, and production release
are different facts. The later-feature list is not a checklist that must be
exhausted before the current product can be called shipped.

Measure whether owners receive useful caller information and can choose the
right action. Keep deployment/notification failures and confirmed setup failures
visible. Numeric latency, retention, conversion, or satisfaction targets require
an agreed measurement method and real observations; this update does not invent
achieved KPIs or carry forward the original PRD's unverified targets.

For each later feature, require an explicit problem, success measure, small
implementation scope, failure behavior, tests, and any provider/device gates
before moving it into necessary work.
