# Owner SMS identity and screening reason — backend rollout

All times in this record are UTC.

**Deployment complete:** staging and production serve the approved source with
100% traffic. Production was independently verified at **17:36:33Z**. The owner
subsequently confirmed the SMS fix working on September 17; the separate call
and frontend phone checks remain open.

## Approved scope

On September 17, the owner approved staging followed by production for commit
`2b402796d1cbffaf59bcb6ecb63d661d9dd0ef12`, tree
`c11c89e362842a306917b30f2b90cadf05a52a72`.

Compared with the previously deployed backend
`976202dfd418e0c4ca551a96e8075e5d5065b45b`, the cumulative backend change is six
application files, 90 additions and 14 deletions:

- [PR #263](https://github.com/delimatsuo/heykevin/pull/263) adds the fixed first
  line `Hey Kevin: Call summary` to personal and business owner summaries after
  optional translation. Empty translations retain useful details. Screened calls
  use neutral wording; recipients, sending numbers and caller confirmations are
  preserved.
- The already-merged backend part of [PR #261](https://github.com/delimatsuo/heykevin/pull/261)
  publishes the existing screening reason to the same live call and exposes it
  through authenticated call status. It adds an optional field, preserves
  call/account/stream ownership, and limits waiting for optional metadata
  publication to one second. A timed-out transaction may finish later under
  the same ownership guards.

No Docker, dependency, workflow, configuration-default, database-rule or
migration change is included. No new iOS package, App Store operation, Twilio
number naming or agent-initiated live call/SMS is included. Existing build 42
can use the new reason field; older clients ignore it.

The [SMS implementation contract](../superpowers/plans/2026-09-17-owner-sms-identity.md)
records 73 focused passing tests and four detected mutations. Its implementation
head `e7783b99f8488ef747697ffaec17ae36831abc6c` has the same tree as the approved
merge. All nine jobs in [source CI 35246480874](https://github.com/delimatsuo/heykevin/actions/runs/35246480874)
passed; independent source and Codex reviews found no actionable issues.
A separate fresh-context release review found no cumulative source or procedure
blocker, conditional on live preflight, staged verification and the current
production environment approval.

## Preflight

Freshly fetched main matched the approved SHA. The isolated worktree
`codex/owner-sms-rollout` began clean at that SHA. No deployment was active,
queued, waiting or pending. Both environments were healthy on the previous SHA.

| Environment | Previous revision / rollback target | Traffic |
|---|---|---|
| Staging | `kevin-api-staging-00173-qur` | 100% |
| Production | `kevin-api-00272-8z4` | 100% |

Read-only checks verified staging Firestore/RTDB use `kevin-staging-491315` and
sandbox APNs/App Store, while production uses `kevin-491315` and production
APNs/App Store. Runtime service accounts and provider identifiers matched the
existing workflow variables. All repository and environment variable pages were
accounted for. Existing WIF and build identities are configured for the explicit
GCP project `kevin-491315`; the local CLI default project was not changed.

Promotional offers and receptionist observation shadow are false in both
environments. The shadow HMAC binding is already absent. The existing production
lapsed-number-release setting is true. Each old revision's 57 environment fields
excluding `DEPLOY_SHA` were fingerprinted for comparison without recording secret
values. No dotenv file or caller record was used for verification.

## Staging

[Run 35248672440](https://github.com/delimatsuo/heykevin/actions/runs/35248672440)
was dispatched once at **16:46:40Z** with `target=staging` and the exact approved
candidate. The run's `headSha` matches the candidate. Validation/deployment is
complete: all ten applicable jobs succeeded, including deployment and its health
check, by **16:52:33Z**. Production was skipped in this staging run as intended.

Independent readback at **16:55:30Z** verified:

- Ready revision `kevin-api-staging-00175-sag`, created at
  `2026-09-17T16:51:42.013487Z`, has 100% of service traffic and the staging tag.
- Canonical and tagged `/health` responses report `ok`, staging, that revision
  and the exact approved SHA. Staging safety controls remain on, with model tools
  and automatic terminal actions off.
- All 57 runtime environment fields other than `DEPLOY_SHA` and the runtime
  service account match the previous revision.
- Anonymous admin HTML, CSS and JavaScript return 200. The three-second pause
  WAV returns 200, 48,044 bytes and SHA-256
  `59db6dfb709393b7aab8efcc2b395df4def43f1c976d8b157bb9009dbacdc0ec`.
- A metadata-only log query from revision creation through approximately
  **16:56Z** found no error-severity or HTTP 5xx entries. This is a short
  observation window, not live-call or SMS acceptance.

The bounded log query used `resource.type="cloud_run_revision"`, service
`kevin-api-staging`, revision `kevin-api-staging-00175-sag`, timestamp at or after
`2026-09-17T16:51:42.013487Z`, and `(severity>=ERROR OR httpRequest.status>=500)`.
At most 50 entries were requested; the result was an empty array (zero entries).
Only timestamp, severity, HTTP status and revision metadata were selected, with
no log text or caller content. The revision and health observations above were
captured separately from the successful workflow job results.

Cloud Run reports image
`us-central1-docker.pkg.dev/kevin-491315/cloud-run-source-deploy/kevin-api-staging@sha256:11c240da2fb5e0ed64db73dbba9b8c4f793cf689e4e5ad208bde51f4fe0dbe12`
and build `188332cb-16e7-48bf-bd44-a45704588a66`, using the existing staging build
service account. Direct Cloud Build listing was unavailable to the local account;
artifact identity comes from the successful workflow and Cloud Run readback.

## Production

After successful staging verification, a fresh fetch still resolved main to the
approved SHA and no competing workflow was active. Production
[run 35249645557](https://github.com/delimatsuo/heykevin/actions/runs/35249645557)
was dispatched once at **16:56:20Z**, with `target=production` from main and no
`candidate_sha` input. Its `headSha` matches the approved SHA. By **16:59Z**, all
nine validation jobs had passed and `Deploy to Production` was waiting on the
existing `production` environment gate (`13924929328`), whose required reviewer
is `delimatsuo`. The owner was directed to this exact run for the final approval.

The owner then instructed: “remove that rule. You can click with my express
authorization.” The repository instructions now permit an agent to approve a
specific production deployment with express owner authorization, after verifying
repository/run/SHA/environment. This preserves the approval gate and does not
authorize future deployments automatically. On the next provider readback,
GitHub already recorded `approved` by `delimatsuo`, and the production deploy
job had started at **17:31:50Z**. The agent did not submit a duplicate approval.
All ten applicable jobs passed; deployment completed at **17:35:59Z**, and the
workflow concluded success at **17:36:00Z**. Staging was skipped in this run.

Independent readback at **17:36:33Z** verified:

- Ready revision `kevin-api-00273-f7g`, created at
  `2026-09-17T17:34:30.405350Z`, serves 100% of production traffic.
- Canonical `/health` at `https://kevin-api-752910912062.us-central1.run.app`
  and service `/health` at `https://kevin-api-l63rergg7a-uc.a.run.app` report
  `ok`, production, this revision and the exact approved SHA.
- All 57 runtime environment fields other than `DEPLOY_SHA` and the runtime
  service account match the previous revision. Production model tools and
  automatic terminal actions remain on, and staging safety controls remain off.
- Anonymous admin HTML, CSS and JavaScript return 200. The pause WAV returns
  200, 48,044 bytes and the same canonical SHA-256 recorded above for staging.

Cloud Run reports image
`us-central1-docker.pkg.dev/kevin-491315/cloud-run-source-deploy/kevin-api@sha256:612dc7f439c4817a7dccbb0c8fa35297283eec7832a06c2d69d434ba96e7764a`
and build `7c10e4cb-4b76-4883-93fd-225776f18b7a`, using the existing production
build service account.

A metadata-only query selected `resource.type="cloud_run_revision"`, service
`kevin-api`, revision `kevin-api-00273-f7g`, timestamps from
`2026-09-17T17:34:30.405350Z` through `2026-09-17T17:37:04.864900Z`, and
`(severity>=ERROR OR httpRequest.status>=500)`. With a limit of 50, it returned
zero entries. No log text or caller content was read. This short observation
does not establish live-call or actual SMS delivery acceptance.

Previous revisions remain the rollback targets listed above. No schema or
migration rollback is needed; a rollback cannot recall already delivered SMS.
The later release-status and authorization-rule documentation changes do not
alter the backend or iOS trees and do not require another deployment or upload.

## Owner acceptance

- [x] Owner-reported SMS receipt and identification passed on September 17.
  After being asked to check new post-call summaries beginning with
  `Hey Kevin: Call summary`, Deli replied: “tested. All working. What's next?”
  This records acceptance of the SMS fix; the separate build 42 call checks
  below require their own results.
- [ ] On installed build 42, the owner observes the reason when it becomes
  available on the exact live call; unavailable reason retains the honest fallback.
- [ ] Existing N1/N3 phone acceptance remains open. Backend health, CI and mocks
  do not complete phone/call acceptance. VoiceOver remains outside current scope.

## Actions cost and bounds

Repository and billing owner: public `delimatsuo/heykevin`, `delimatsuo`.
September account-wide Actions net was **$72.392002411** at preflight, with
Hey Kevin net **$0**. Each dispatch runs seven Ubuntu test shards, Python
quality, the required stable `Test` aggregator and one deploy job. Expected
chargeable Actions cost is **$0**; recent successful staging/production samples
used 16/14 rounded runner-minutes, about $0.096/$0.084 gross reference cost.

Tests/quality have 15-minute timeouts, aggregation five minutes, and deployment
30 minutes. Existing dispatch concurrency serializes the environments. Existing
Cloud Run/Cloud Build are within the approved rollout; no duplicate dispatch,
workflow rerun, runner migration or billing change is planned.

Before publishing the documentation follow-up, September account-wide Actions
net was **$72.395433337**, with Hey Kevin still **$0**. The follow-up is Markdown
only: its PR runs the existing nine Ubuntu checks, with expected chargeable
Actions cost **$0**; merging to main does not deploy. No workflow or repository
protection setting is changed.
