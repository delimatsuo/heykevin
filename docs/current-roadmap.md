# Hey Kevin — Current Roadmap and Release Status

**Reconciled:** 2026-09-14
**Implementation baseline:** `7377c7ba402297625dec5de97a2250f50b1c8013`
**Repository:** `delimatsuo/heykevin`

The [current PRD](../kevin-prd.md) defines necessary work and later opportunities.
This document records delivery status. Older handoffs, the original PRD and the
June Business Dispatch proposal do not define the active backlog. Historical
security evidence and approved safety constraints remain applicable.

Dates below use America/New_York unless a timestamp explicitly ends in `Z`.
External observations are dated snapshots, not a standing claim about production.

## Observed release baseline

Read-only checks on September 14, with the native candidate updated September 15 UTC:

| Surface | Observed state | Evidence and limit |
|---|---|---|
| Production backend | `kevin-api-00270-l9s`, SHA `7377c7ba402297625dec5de97a2250f50b1c8013`, verified September 14 at `21:45:33Z` | Successful [production run 34897431657](https://github.com/delimatsuo/heykevin/actions/runs/34897431657), exact live health/runtime identity, 100% serving traffic and anonymous smoke. See the [release record](releases/2026-09-14-urgent-production-candidate.md). iOS release and real-call/device acceptance remain open. |
| Staging backend | `kevin-api-staging-00169-muy`, SHA `7377c7ba402297625dec5de97a2250f50b1c8013`, verified September 14 at `21:11:16Z` | Successful [staging run 34896587160](https://github.com/delimatsuo/heykevin/actions/runs/34896587160), live health identity, 100% serving traffic, runtime isolation and anonymous static smoke. Includes the rolling-call compatibility repair. Staging remains distinct from real caller/device acceptance. |
| Public iOS | Version `1.2.11`, **build 36**, `READY_FOR_DISTRIBUTION` | Authenticated read-only App Store Connect inspection on September 14 links the public version to build 36. The public [Apple lookup](https://itunes.apple.com/lookup?id=6761427495&country=us) reports the September 5 release date but does not expose the build number. |
| Previous TestFlight candidate | `1.2.11 (37)`, `VALID`, `IN_BETA_TESTING` | Authenticated Apple inspection before preparing build 38. This is distinct from the public build 36 and the new notification candidate below. |
| Notification iPhone candidate | `1.2.12 (38)`, **VALID**, **IN_BETA_TESTING** in the existing internal QA group, verified at `23:18:44Z` | Source `813e0e3b72e90c709832d72129b82f5e6622775b`. All 127 native unit tests, signed-package review and Apple validation passed. Upload and processing completed; phone acceptance and App Store submission remain open. See the [candidate record and device checklist](releases/2026-09-14-notification-ios-38.md). |
| Native frontend iPhone candidate | **1.3.0 (39)**, **VALID**, **IN_BETA_TESTING** in internal QA, verified September 15 at `03:51:07Z` | New Calls + Kevin design and bounded history. [Release record](releases/2026-09-15-native-frontend-ios-39.md) binds packaged source `4bf090a...`, CI, independent review, Apple processing and test notes. Owner device acceptance remains open. |
| Development inventory | Native frontend implementation is complete; N1/N3 owner acceptance remains open | [PR #248](https://github.com/delimatsuo/heykevin/pull/248) records the frontend. Issue #33 and later PRD features remain deferred. Old worktrees and retained branches do not establish active feature work. |

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

## Pending notification release

**Active priority:** Complete iOS release and device acceptance for N1 in the
[current PRD](../kevin-prd.md). The notification backend, urgent handoff and
rolling-call compatibility repair are now deployed to production. This does
not mark the full PRD or notification experience accepted on a physical iPhone.

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
- [ ] Publish the validated iPhone build to the App Store after the device checks
      and owner release approval.
- [ ] Record the owner's installed iOS build and notification/pickup checks from
      the PRD, including stale-alert and ended-call behavior.

Only Deli may approve the production environment; [AGENTS.md](../AGENTS.md)
explicitly forbids an agent or API approval. Deli's approval and the completed
production deployment above did not authorize an iPhone release. The owner
subsequently agreed to the TestFlight candidate and device-validation sequence;
App Store submission remains a later release approval. The tested
forward-recovery patch still requires another build and approval if needed;
it does not rely on returning live operations to the older backend.

## Native frontend — available for internal TestFlight review

After testing notification build 38, the owner approved translating the
expert-reviewed HTML into SwiftUI and asked for bounded history. Build 38
contains the notification changes; it predates this native design.

The [native plan](superpowers/plans/2026-09-14-native-frontend.md) is implemented
in **1.3.0 (39)**, now available in internal TestFlight QA. Calls + Kevin are the two tabs,
with labeled account Settings and the live caller reachable throughout.
History starts at 20 rows, expands by 20 to the existing 100-call/90-day bound,
and searches/filters the full bounded snapshot before limiting visible rows.
The retrieval limit does not delete older stored calls or extend retention.

Implementation, independent native review and local verification are complete:
269 native unit tests, 9 fixture UI tests and targeted mutation probes passed.
The [candidate record](releases/2026-09-15-native-frontend-ios-39.md) binds the
source and evidence. Required CI passed, independent package review approved,
and Apple processing, internal QA membership and What to Test readback are verified.
Physical notification/call checks and public App Store approval remain separate.

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
