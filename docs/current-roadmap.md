# Hey Kevin — Current Roadmap and Release Status

**Reconciled:** 2026-09-17
**Backend implementation baseline:** `2b402796d1cbffaf59bcb6ecb63d661d9dd0ef12`
**Repository:** `delimatsuo/heykevin`

The [current PRD](../kevin-prd.md) defines necessary work and later opportunities.
This document records delivery status. Older handoffs, the original PRD and the
June Business Dispatch proposal do not define the active backlog. Historical
security evidence and approved safety constraints remain applicable.

Dates below use America/New_York unless a timestamp explicitly ends in `Z`.
External observations are dated snapshots, not a standing claim about production.

## Observed release baseline

Backend verification times are stated in UTC below; both current deployments
occurred on September 17. The public US lookup at 18:33:08 UTC now confirms
version 1.3.1, released at `2026-09-17T08:44:22Z`, superseding the earlier 1.3.0
lookup. Apple reports the new transcript-first **1.3.2 (42)** submission
`WAITING_FOR_REVIEW` at 18:32:28 UTC. Submission, public availability and
physical-phone acceptance remain separate:

| Surface | Observed state | Evidence and limit |
|---|---|---|
| Production backend | `kevin-api-00273-f7g`, SHA `2b402796d1cbffaf59bcb6ecb63d661d9dd0ef12`, verified `2026-09-17T17:36:33Z` | Successful [production run 35249645557](https://github.com/delimatsuo/heykevin/actions/runs/35249645557), exact live health/runtime identity, 100% serving traffic, unchanged runtime settings, anonymous smoke and canonical pause-WAV retrieval. Adds owner SMS identity and the screening-reason field; retains the Relay correction. The owner confirmed the SMS fix working on September 17; separate call/frontend phone acceptance remains open. See the [rollout record](releases/2026-09-17-owner-sms-identity-backend.md). |
| Staging backend | `kevin-api-staging-00175-sag`, SHA `2b402796d1cbffaf59bcb6ecb63d661d9dd0ef12`, verified `2026-09-17T16:55:30Z` | Successful [staging run 35248672440](https://github.com/delimatsuo/heykevin/actions/runs/35248672440), canonical/tagged health identity, 100% serving traffic, preserved runtime isolation, anonymous static smoke and canonical pause-WAV retrieval. See the [rollout record](releases/2026-09-17-owner-sms-identity-backend.md). |
| Public iOS | Version **1.3.1 (41)**, `READY_FOR_SALE`, review `COMPLETE` | Public US lookup at `2026-09-17T18:33:08Z` confirms 1.3.1, released `2026-09-17T08:44:22Z`. Apple binds that version to build 41. See the [submission record](releases/2026-09-16-app-store-ios-41.md) and [dated public observation](releases/2026-09-17-app-store-ios-42.md). |
| Historical Public iOS | Version `1.2.11`, **build 36**, `READY_FOR_DISTRIBUTION` | Historical public baseline as of September 14, now superseded by build 39. |
| Previous TestFlight candidate | `1.2.11 (37)`, `VALID`, `IN_BETA_TESTING` | Historical TestFlight state prior to build 38/39. |
| Notification iPhone candidate | `1.2.12 (38)`, **VALID**, **IN_BETA_TESTING** | Historical candidate delivering N1 notifications, superseded by build 39. See [candidate record](releases/2026-09-14-notification-ios-38.md). |
| Original native frontend release | **1.3.0 (39)**, first published September 15 with internal QA availability | Delivered Calls + Kevin design, bounded history and notification enhancement. [Release record](releases/2026-09-15-native-frontend-ios-39.md) binds packaged source `4bf090a...`, CI, reviews, and the dated Apple release state. Current public version is 1.3.1 (41), as recorded above. Physical phone acceptance remains open. |
| Latest Apple submission | **1.3.2 (42)**, **WAITING_FOR_REVIEW**, verified `2026-09-17T18:32:28Z`; release **AFTER_APPROVAL** | [Submission record](releases/2026-09-17-app-store-ios-42.md) binds the existing build and six updated screenshots, including full live and retained screening transcripts. Approval, public availability and physical phone acceptance remain unconfirmed for this version. |
| Current internal iPhone candidate | **1.3.2 (42)**, **VALID / IN_BETA_TESTING** in the existing QA group, verified `2026-09-17T15:33:58Z` | [Delivery record and phone checks](releases/2026-09-17-transcript-first-ios-42.md). Includes direct full-transcript notification entry, retained screening text after pickup and consistent system appearance. The backend screening-reason field is deployed; phone acceptance remains open. |
| Development inventory | Transcript-first flow and appearance correction delivered in internal build 42; owner SMS identity and screening reason deployed in the backend | [PR #261](https://github.com/delimatsuo/heykevin/pull/261) adds the current frontend behavior and screening-reason field. [PR #263](https://github.com/delimatsuo/heykevin/pull/263) brands owner summaries. Earlier [PR #256](https://github.com/delimatsuo/heykevin/pull/256) supplied the iOS confirmation repair, and [PR #258](https://github.com/delimatsuo/heykevin/pull/258) supplied the retained Relay correction. N1/N3 phone acceptance remains open; issue #33 and later PRD features remain deferred. |

## Shipped

- [x] **Core native iPhone product:** call screening, trusted-contact routing,
      live transcript, takeover, personal/business modes and subscription access.
      Current architecture is described in [AGENTS.md](../AGENTS.md).
- [x] **Owner-confirmed appointments and caller confirmation texts:** public
      version 1.2.9, released August 22, describes Google Calendar confirmation
      from Recents and incoming-caller-text notifications. This does not authorize
      unrelated automated caller follow-ups.
- [x] **International forwarding UI and country selection:** public version
      1.2.11 describes international setup, a Settings country picker, screening
      and onboarding improvements. These are client-release observations, not
      certification of every advertised carrier or country.
- [x] **Personal-mode hold behavior:** [PR #238](https://github.com/delimatsuo/heykevin/pull/238)
      preserves the 30-second silent hold while continuing caller transcription
      and suppresses business scheduling/CRM tools in personal mode. Its backend
      is in the observed production SHA.
- [x] **September backend hardening:** phone-number log privacy, App Store
      notification verification/replay, and provisioning/release safety changes
      through PR #235 are included in production. The September 3 notes claiming
      this wave was undeployed are superseded by the observed September 4 deploys.

<a id="pending-notification-release"></a>

## Notification release and remaining device acceptance

**Active priority:** Complete the remaining physical call and frontend checks for N1/N3
in the [current PRD](../kevin-prd.md). The notification backend, urgent handoff and
rolling-call compatibility repair are deployed to production, and the native frontend
in 1.3.0 (39) is published to the App Store. Physical-device acceptance on the owner's
iPhone remains open. Per the owner's September 16, 2026 scope decision,
VoiceOver qualification is deferred and is not a current acceptance requirement
or release blocker. See the [PRD deferral](../kevin-prd.md#deferred-ideas-and-superseded-plans).

The September 16 owner call prompted the clearer action buttons and natural
message-taking transition. That backend is deployed. The owner installed build
40 and reported a false confirmation warning despite a normal message response.
**1.3.1 (41)** is in internal TestFlight QA with the iOS-only repair and was
[submitted for App Review](releases/2026-09-16-app-store-ios-41.md) with six updated
screenshots at the owner's request. Apple reported `READY_FOR_SALE` with review
`COMPLETE` at the September 17 15:16 UTC check. The owner then approved a test
build of the transcript-first flow and consistent appearance. Use the
[build 42 phone checks](releases/2026-09-17-transcript-first-ios-42.md#owner-phone-checks--open)
alongside the earlier speech/pickup checklist. QA availability is verified.
At the owner's later request, build 42 was [submitted for App Review](releases/2026-09-17-app-store-ios-42.md)
with six updated screenshots; Apple reports `WAITING_FOR_REVIEW` at 18:32:28 UTC.
Provider/package verification does not close those physical-call rows.

A later build 40 call also reported repeated questions after Take a message.
The [Relay message-state correction](releases/2026-09-16-relay-message-state-backend.md)
is deployed to staging and production. Install or confirm build 42 before
retesting the warning and conversation together; finishing current speech,
the conditional three-second pause and continued caller replies remain open
phone checks.

[PR #239](https://github.com/delimatsuo/heykevin/pull/239) adds caller/reason summary
updates to an existing notification and the matching Pick Up action. The same
source package contains client feedback/review improvements; no additional
feedback work is planned.

The superseded [production run 33938295394](https://github.com/delimatsuo/heykevin/actions/runs/33938295394),
pinned to `c093ed6ef1c505e8d68f01d6f6f365bf2553ac55`, was **cancelled on September 14
with Deli's explicit authorization**. It is no longer holding the dispatch queue.
That candidate contained a delayed-summary defect: extraction could survive
pipeline teardown and send a stale actionable alert. Do not revive that release.
[PR #241](https://github.com/delimatsuo/heykevin/pull/241) repaired that lifecycle
defect and merged as `66f8a446e0e873ecb280c39aaab60f7cf0448a36` on September 14
at `14:01:59Z`; all nine required-workflow jobs passed on its exact source head
`43354aaca657e089e42d4a1bb529a1869996e24f`. Staging and production jobs were
skipped. At that earlier preflight, production reported
`407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc`; the later production release below
supersedes that observation.

The owner then approved **Pick up + Take a message**, passive dismissal, a
bounded urgent-call fallback and a server-persisted Urgent alerts preference.
That is active N1 work under the [handoff contract](superpowers/plans/2026-09-14-urgent-call-handoff.md).
Its source merged in [PR #242](https://github.com/delimatsuo/heykevin/pull/242)
as `2b56fdaec3eafddbd42f4ebb0516e60db812161d`. All nine required-workflow jobs passed
on reviewed source head `66030cd41318d75b423373aebfcb6c5f3ffc63cb`; the merge has
the same tree. The [staging record](releases/2026-09-14-urgent-staging.md) tracks
the subsequent deployment independently from production and iPhone acceptance.
The [interactive HTML](frontend-concept/index.html) demonstrates the experience.
The owner subsequently approved the native Calls + Kevin frontend and bounded
history, tracked below as N3. New callback/text-reply features and the later-feature
list remain outside active implementation.

- [x] Notification source merged and staged.
- [x] Independently reproduce the delayed-summary teardown defect with fictional
      data and mocked extraction/push.
- [x] Repair task ownership/cancellation in all three voice pipelines and prove
      locally that ending screening prevents a delayed summary from being sent.
- [x] Complete independent review and required CI for the summary lifecycle
      repair and merge PR #241. Its [packet](releases/2026-09-14-screening-notification.md)
      records the earlier repair; it is not evidence of urgent handoff delivery.
- [x] Complete and independently verify the approved urgent handoff and two
      notification actions, including iOS session/call ownership and lost-response
      recovery. The [urgent-handoff packet](releases/2026-09-14-urgent-call-handoff.md)
      records local tests and the remaining release gates; its publishing PR
      records required exact-HEAD CI and the final merge candidate.
- [x] Stage the initial N1 source (subsequently superseded): run `34887443367` passed all ten
      applicable jobs, and serving revision `kevin-api-staging-00167-qiq` reports
      the approved SHA. Anonymous smoke and runtime isolation checks passed.
- [x] Cancel the superseded production run with Deli's explicit authorization.
- [x] Merge and restage the [rolling-call compatibility repair](releases/2026-09-14-rolling-call-compatibility.md).
      PR #244 passed all nine CI jobs and merged as `7377c7ba...`. Staging run
      `34896587160` passed all ten jobs; revision `kevin-api-staging-00169-muy`
      serves that exact SHA. The repair also passed 7,869 offline tests and
      all eight safeguard mutation probes.
- [x] Request one [production run 34897431657](https://github.com/delimatsuo/heykevin/actions/runs/34897431657)
      from main at exact candidate `7377c7ba402297625dec5de97a2250f50b1c8013`.
      The [release record](releases/2026-09-14-urgent-production-candidate.md)
      preserves source, staging, recovery and approval boundaries.
- [x] Deli approves that production job in GitHub.
- [x] Confirm successful deployment: all ten applicable jobs passed. Production
      revision `kevin-api-00270-l9s` serves the exact candidate SHA with 100%
      traffic; runtime, health and anonymous serving checks passed at `21:45:33Z`.
- [x] Complete TestFlight delivery: **1.2.12 (38)** is `VALID`,
      `IN_BETA_TESTING` and present in the internal QA group. The
      [candidate record](releases/2026-09-14-notification-ios-38.md) includes
      package identity, verification and the owner test sequence.
- [x] Public App Store release: **1.3.0 (39)** is `READY_FOR_DISTRIBUTION` (verified September 16, 2026 at `00:20:10Z` / Sep 15 America/New_York) following owner's submission instruction ("submit the last build"). See the canonical [build 39 release record](releases/2026-09-15-native-frontend-ios-39.md).
- [ ] Record the owner's installed **1.3.2 (42)** and the remaining notification/
      pickup acceptance from [N1 in the PRD](../kevin-prd.md#n1--finish-the-screening-notification-enhancement),
      including stale-alert, ended-call, banner, urgent-preference and failure
      scenarios. The [build 42 phone checks](releases/2026-09-17-transcript-first-ios-42.md#owner-phone-checks--open)
      cover the transcript-first flow alongside the confirmation and button/timing
      checks; passing them does not close other unperformed N1 rows. Earlier
      build 39/41 records remain historical evidence.

Deli's approval and the completed September 14 production deployment above did
not authorize an Apple release. The owner
subsequently gave explicit instruction to submit the existing build ("submit the last build"),
and submission was completed. The native frontend was first released in
1.3.0 (39); the current public baseline is 1.3.1 (41), as recorded above.
The remaining physical-device acceptance stays open.
The tested forward-recovery patch still requires another build and approval if
needed; it does not rely on returning live operations to the older backend.

## Native frontend — released in 1.3.0 (39)

After testing notification build 38, the owner approved translating the
expert-reviewed HTML into SwiftUI and asked for bounded history. Build 38
contains the notification changes; it predates this native design.

The [native plan](superpowers/plans/2026-09-14-native-frontend.md) is implemented
and was first released in **1.3.0 (39)**, with App Store publication and internal
TestFlight QA availability. The current public baseline is **1.3.1 (41)**, as
recorded above. Calls + Kevin are the two tabs,
with labeled account Settings and the live caller reachable throughout.
History starts at 20 rows, expands by 20 to the existing 100-call/90-day bound,
and searches/filters the full bounded snapshot before limiting visible rows.
The retrieval limit does not delete older stored calls or extend retention.

Implementation, independent native review and local verification are complete:
269 native unit tests, 9 fixture UI tests and targeted mutation probes passed.
The [release record](releases/2026-09-15-native-frontend-ios-39.md) binds the
source and evidence. Required CI passed, independent package review approved,
Apple processing, internal QA availability and What to Test readback were verified,
and App Store publication completed September 15. The remaining physical call and
frontend acceptance on the owner's phone stays open.

## Unfinished or unverified, not an automatic implementation queue

| Item | Current fact | Next decision or evidence |
|---|---|---|
| Verified forwarding readiness | Onboarding records the owner's setup intent and observed forwarded calls; it does not implement the former v2 server-verification/session/readiness experience. | Highest-priority later candidate in the PRD; design the smallest reliable setup check after N1. |
| Calls follow-up workflow | Recents remains a chronological call list. The proposed work queue and persistent action statuses are not shipped. | Confirm need before adding a small workflow; a full Dispatch / Calls / Kevin redesign is deferred. |
| Returning-customer memory and service-request mutations | Source and independent default-closed controls exist; current per-account activation was not inspected. | Follow [customer-memory rollout](customer-memory-rollout.md) for identity, retention/deletion, Firestore and recovery qualification. Existing trusted-contact greeting is separate from memory activation. |
| Video diagnosis | Video-first source/design exists. The August 20 spec's statement that production was dark is historical, not a September flag audit. | Qualify the caller page, provider/media infrastructure, recovery, retention and delivery before claiming a live product. See the [video design](superpowers/specs/2026-08-20-video-diagnosis-design.md). |
| Account-phone change | The [possession-verified rebind spec](specs/phone-rebind-possession-verified.md) is design only; generic PATCH correctly protects both phone fields. | Resolve the spec's owner decisions before implementation. |
| International qualification | The nine-country source scope is `US`, `CA`, `BR`, `GB`, `DE`, `FR`, `IT`, `ES`, `PT`. Address/city capture and regulatory-provider unit tests have landed; those old missing-source entries are closed. | Carrier/SIM behavior, regional dial-in numbers and regulatory bundle approval still need owner-run qualification. Source currently creates regulatory addresses with empty region/postal-code, and forwarding target format needs real-carrier confirmation. |
| International release wording | Public 1.2.11 notes also name Australia, but `SUPPORTED_COUNTRIES` in [contractors.py](../app/db/contractors.py) does not include `AU`. | Reconcile the supported-market claim before promising Australia; this document does not expand supported countries or alter store metadata. |
| Legacy Apple lookup retirement | [Issue #33](https://github.com/delimatsuo/heykevin/issues/33) is still open. | Verify client adoption and obtain the required removal authorization. |

The US/CA Verizon/GSM notes were reconciled in PR #236. The account-country picker
and regulatory street/city capture were implemented in PRs #218/#219/#233. The
App Store replay CLI follow-ups were resolved in PR #235. Do not reopen them from
an older table. September 3 environment-variable observations are historical;
current regulatory configuration was not audited in this reconciliation.

## Completed source safeguards with separate activation evidence

Tenant-explicit contact isolation (PR #204), integration-token encryption
envelopes (PR #205), account-creation phone normalization (PR #207) and Google
Calendar reschedule fencing (PR #209) are merged. Their source is contained in the
observed backend revisions, but that does not establish encrypted-write flags,
provisioned keys, indexes, backfills, or provider-enabled behavior.

The Google Calendar contract still requires fresh ETags, bound tenant/credential
ownership, conditional mutations, and reconciliation after an unknown outcome
rather than blind replay. See [PR #209](https://github.com/delimatsuo/heykevin/pull/209),
[customer-memory rollout](customer-memory-rollout.md), and the
[integration-token runbook](runbooks/integration-token-envelope.md). Historical
Phase 0 evidence in [phase0-release-readiness.md](security/phase0-release-readiness.md)
is not a new release signoff.

## Deferred

The [current PRD](../kevin-prd.md#deferred-ideas-and-superseded-plans) records the
approved native refresh separately from the deferred former v2 work-management
program, WhatsApp, voice cloning, multiple numbers and other
later ideas. These are not tasks that an agent should automatically start.
No new provider, SMS, feature flag, pricing, or public-market commitment is created
by this documentation update.
