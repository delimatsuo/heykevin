# Hey Kevin — Current Product Requirements

**Updated:** 2026-09-14
**Owner:** Deli Matsuo
**Backend release baseline:** `7377c7ba402297625dec5de97a2250f50b1c8013`
**iPhone test candidate:** `1.2.12 (38)`, source `813e0e3b72e90c709832d72129b82f5e6622775b`
**Native frontend candidate:** `1.3.0 (39)` in development; release evidence is separate.
**Scope decision:** Complete the screening notification and urgent-call handoff,
and implement the approved Calls + Kevin native design with bounded history.
Keep valuable later features in this PRD rather than starting them automatically.

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

**Problem:** Owners need enough information to decide whether to answer, a clear
way to let Kevin take a message, and a reliable fallback when an urgent call
cannot reach them. The owner approved the two-action experience and urgent-call
completion on September 14, 2026.

**Required experience:**

1. When Kevin has screened the caller and asks them to hold, update that call's
   notification with a short caller name and reason.
2. Update the existing call notification in place rather than producing a series
   of unrelated alerts.
3. Offer **Pick up** and **Take a message** as the two notification actions.
   Pickup targets the exact live call. Take a message keeps the caller connected
   and remains pending until Kevin accepts the instruction; acceptance does not
   mean a caller has finished leaving a message.
4. Tapping the body opens that call without answering. An ended call opens its
   matching summary or an honest unavailable state. Dismissing the notification
   changes presentation only.
5. When Kevin detects urgency, start or preserve one 30-second owner wait. Alert
   the owner if urgent alerts are enabled. If there is no owner decision, tell
   the caller the owner is unavailable and continue taking a message. Pickup,
   taking a message and timeout share one server decision. Urgency never implies
   the owner answered, help was dispatched or a response time was promised.
6. Persist **Urgent alerts** on the server. Turning it off suppresses additional
   urgent pushes and rings while screening continues. Use supported iOS alert
   behavior without a Critical Alert, Silent Mode or Do Not Disturb bypass promise.
7. Extraction or push failure preserves screening and transcription. Duplicate
   or conflicting actions, lost responses, ended calls, account changes and old
   notifications cannot connect another call. An uncertain transfer retains its
   operation and offers status reconciliation instead of an automatic new transfer.
8. Keep urgent lock-screen alert text generic. Existing iOS preview settings
   govern ordinary caller/reason notifications. Urgent CallKit ringing requires
   an app advertising the new handoff support; older builds receive the regular
   actionable urgent notification.

**Release acceptance:**

- [x] The reviewed notification backend is deployed to production and `/health`
      identifies the approved SHA: `7377c7ba402297625dec5de97a2250f50b1c8013`,
      revision `kevin-api-00270-l9s`, verified September 14 at `21:45:33Z`.
      See the [production record](docs/releases/2026-09-14-urgent-production-candidate.md).
      iOS publication and device acceptance below remain separate.
- [x] The iPhone candidate **1.2.12 (38)** passed Apple processing and is
      available in the existing internal TestFlight QA group, verified September
      14 at `23:18:44Z`. See the [candidate record and test sequence](docs/releases/2026-09-14-notification-ios-38.md).
- [ ] On the owner's iPhone, the incoming alert updates with caller and reason
      during screening; lock-screen and unlocked behavior are checked.
- [ ] Banner navigation and Pick Up target the same call; ending the call or
      tapping an old alert does not answer another call.
- [ ] Take a message shows request/acknowledgement truthfully, preserves the
      caller connection, and cannot conflict with pickup or timeout.
- [ ] An unanswered urgent call leaves the owner wait after 30 seconds. The
      urgent-alert preference survives reload and the backend respects it.
- [ ] Duplicate actions and late responses after hangup, a new call or an
      account change cannot create a second or unintended connection.
- [ ] Native urgent CallKit answering reuses the incoming call; no duplicate
      outgoing call appears. Existing direct-ring calls remain usable.
- [ ] A notification failure leaves the live call usable and does not interrupt
      the hold or transcript.
- [ ] Delayed extraction or a stale task cannot send a new actionable screening
      summary after pickup, hangup, or the end of the screening wait.
- [ ] Record the installed iOS version and build used for this check. Public App
      Store version metadata alone does not identify that build.
- [ ] Publish the validated iPhone build to the App Store after the device checks
      and owner release approval.

PR #239 contains the initial summary notification and feedback/review code;
PR #241 repairs delayed summary cancellation. The approved urgent handoff extends
that work using the [implementation contract](docs/superpowers/plans/2026-09-14-urgent-call-handoff.md).
The owner approved translating the reviewed HTML into the native experience on
September 14; N3 below defines that work. No new feedback
system, caller SMS or callback action is included. Release evidence is recorded in the
[current roadmap](docs/current-roadmap.md#pending-notification-release).

### N2 — Fix confirmed regressions affecting the core call flow

Repair a reproducible defect that blocks setup, screening, transcript delivery,
correct call pickup, or subscription access. A proposed improvement does not
become a regression simply because an older plan listed it. Each repair needs a
concrete failing case, a bounded change, and verification of the affected flow.

### N3 — Deliver the approved native frontend with bounded history

**Approval:** On September 14, after testing notification build 38 and reviewing
the interactive HTML, the owner approved the native enhancements and required a
history limit. The [native implementation plan](docs/superpowers/plans/2026-09-14-native-frontend.md)
records the expert panel, competitor research and verification contract.

**Required experience:**

1. Use **Calls** and **Kevin** as the two tabs. Calls opens first; Kevin contains
   answering behavior and business setup. A labeled Settings entry preserves
   account, subscription, country, support and deletion controls.
2. Keep the current call reachable from both tabs and account Settings, with
   the full live transcript and the existing Pick up / Take a message behavior.
   Historical details and notification actions remain bound to the exact call
   and account. Preserve forced paywall and personal/business behavior.
3. Show **20 calls initially**, then **Show 20 more**, up to the **100 most recent
   calls within 90 days** supplied by the existing history API. Twenty is our
   display choice. The retrieval cap does not delete the 101st stored call.
   This work does not change retention or add archive/deletion controls.
4. Search the entire bounded history before limiting visible rows. Support
   caller name, formatted phone number and available caller transcript text,
   with All, Unread and Spam filters. Keep query, filter and expansion when
   returning from details; reset expansion on an explicit search/filter change.
5. Show honest counts, distinct empty states and retained rows with Retry on
   observable refresh failures. Mark all read covers the fetched history,
   including hidden pages. Clear old-account content on authentication changes.
6. Preserve controls and unsaved settings drafts across navigation. Delayed
   responses cannot overwrite a newer save, revive a dismissed deletion
   confirmation or apply another account's state.

**Delivery acceptance:**

- [x] Owner approved the expert-reviewed design and bounded-history direction.
- [ ] Native unit, lifecycle/ownership mutation and fixture UI checks pass.
- [ ] Independent review and required CI pass for the final source.
- [ ] The new native candidate is available in the internal TestFlight QA group.
- [ ] Owner checks the installed frontend and the remaining N1 phone scenarios.

Apple Phone uses filters and per-call details; Google Voice offers search and
separates archiving from deletion; Quo offers searchable, filterable call logs.
The plan links their official documentation. These examples inform navigation,
not Kevin's retention policy. Public App Store publication remains a separate
owner release decision.

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
| Later | Ended-call callback or text reply | May shorten follow-up after Kevin has finished screening. | Keep actions tied to the selected ended call; define consent, delivery, duplicate prevention and uncertain-outcome recovery before adding caller communications. | Confirm owner demand after N1; the five actions in the historical Telegram PRD do not authorize implementation. |
| Later | Returning-customer continuity | Avoid asking a repeat caller for information already safely known. | Reuse the existing memory and request contracts; distinguish trusted identity from caller ID; verify retention, deletion, recovery, and provider prerequisites before activation. | Establish value with an owner-approved qualification plan; source defaults are not evidence of current runtime configuration. |
| Later | Photo/video diagnosis and estimates | May help a tradesperson assess a job before visiting. | Reuse the existing video-first design; complete caller upload/watch experience, bounded media processing and recovery, retention/deletion, and owner/caller result delivery. | Confirm demand and qualify infrastructure, media handling and delivery before enabling the feature. |
| Later | Secure account-phone change | Customers who change phone numbers need a safe way to retain their account. | Verify possession, prevent ownership collisions, and atomically update the protected phone pair with honest failure recovery. | Resolve the open product/provider choices in the existing [phone-rebind spec](docs/specs/phone-rebind-possession-verified.md); do not reopen generic PATCH access. |
| Later | Broader international qualification | Existing forwarding UI is only useful where the carrier and number setup work. | Qualify the supported country/carrier combinations, regional numbers, regulatory address requirements, forwarding target format, and physical-device flow. | Prioritize countries with actual demand and obtain a bounded owner-run test envelope. Do not equate store release notes with carrier qualification. |

## Deferred ideas and superseded plans

- **Dispatch work-management program:** persistent lead/action states and the
  former three-tab workflow remain deferred. The approved N3 visual refresh
  implements the existing call-screening product, without adding that program.
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
are different facts. N3 delivery requires the reviewed native candidate and
internal TestFlight evidence above; its owner acceptance remains explicit.
The later-feature list is not a checklist that must be
exhausted before the current product can be called shipped.

Measure whether owners receive useful caller information and can choose the
right action. Keep deployment/notification failures and confirmed setup failures
visible. Numeric latency, retention, conversion, or satisfaction targets require
an agreed measurement method and real observations; this update does not invent
achieved KPIs or carry forward the original PRD's unverified targets.

For each later feature, require an explicit problem, success measure, small
implementation scope, failure behavior, tests, and any provider/device gates
before moving it into necessary work.
