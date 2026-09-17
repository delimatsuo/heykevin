# Relay message-taking state — backend rollout

Times in this record are UTC. Staging ran on September 16; production's
environment approval and deployment continued on September 17 (September 16
in the owner's local time).

## Scope and approval

On September 16, the owner approved deploying the reviewed Relay correction to
staging, then production after validation. The source is
[PR #258](https://github.com/delimatsuo/heykevin/pull/258), merge commit
`976202dfd418e0c4ca551a96e8075e5d5065b45b`, tree
`84ccb4724958a94e4b04d04a469786fcdca98fe0`.

Compared with the previously deployed backend
`eee7d42682bf4222ebee59f52e92082f5f9e13cb`, the application changes only
`app/services/relay_pipeline.py`: 43 additions and five deletions. There is no
Docker, dependency, deployment-workflow or runtime-default change. The owner
message-taking decision now appears in system context during transition and
subsequent replies, with cancellation cleanup and caller-language handling.

The [source plan](../superpowers/plans/2026-09-16-relay-message-state.md) records
260 focused passing tests, six detected mutations, and independent review.
All nine required jobs in
[CI 35160135304](https://github.com/delimatsuo/heykevin/actions/runs/35160135304)
passed on implementation head `d25bbd3bede6529daa7fac1607fabd8586fc2767`, which
has the same tree as the merged candidate. Automatic Codex review completed
without findings on that head; commit-bound independent review also approved it.

The reported call was identified by bounded request metadata as build 40. Its
separate false confirmation warning is addressed by already-delivered
[iOS 1.3.1 (41)](2026-09-16-message-confirmation-ios-41.md). This rollout includes
no iOS package, upload, public submission, caller communication or live test
initiated by an agent. Request tests do not prove the spoken conversation.

## Preflight

The source worktree was clean and freshly fetched main still identified the
approved candidate. No deployment was active or queued. The existing manual
workflow is used once per environment, sequentially. The production run first
stopped at the owner-only environment approval. The owner then explicitly
instructed the agent to "approve" this existing deployment, superseding the
standing actor restriction for this run only. The older delegated click on the
build 40 rollout was not reused as authority.

Both environments were healthy on the old backend SHA:

| Environment | Previous revision | Traffic |
|---|---|---|
| Staging | `kevin-api-staging-00171-sir` | 100% |
| Production | `kevin-api-00271-q4j` | 100% |

Read-only, allowlisted runtime checks confirmed staging uses
`kevin-staging-491315` Firestore/RTDB and sandbox APNs/App Store; production uses
`kevin-491315` Firestore/RTDB and production APNs/App Store. Subscription
promotional offers and receptionist shadow remain false in both. Production's
existing lapsed-number-release flag is true and is not changed by this rollout.
No credentials, dotenv files or customer records were read.

## Staging

[Run 35161429321](https://github.com/delimatsuo/heykevin/actions/runs/35161429321)
was dispatched at **23:14:50Z** with `target=staging` and the exact candidate SHA.
It completed successfully at **23:21:32Z**, with all ten applicable jobs passing.
Independent readback at **23:22:06Z** confirmed
**`kevin-api-staging-00173-qur`**, ready and receiving **100% traffic**. Canonical
and tagged health both identified staging, that revision and the exact approved
SHA. Anonymous health, admin page, CSS and JS checks passed with bounded request
timeouts. Authenticated admin checks were not exercised and no credentials were
loaded to extend coverage.

| Staging artifact | Identity |
|---|---|
| Service-reported Cloud Build ID | `b9bd0354-8847-4b74-aa84-0a56c601c8cb` |
| Image digest | `sha256:85e6a93f89b19e8ecfc42d9944b095a11fdb81fa8bf9fd50d3a16226dab250f3` |
| Revision created | `2026-09-16T23:20:24.487505Z` |
| Source object | `gs://run-sources-kevin-491315-us-central1/services/kevin-api-staging/1789600642.590622-8e9bf8ed1b144bd4ba104028abca30e1.zip#1789600642938300` |

Allowlisted runtime comparison with `kevin-api-staging-00171-sir` found no
differences except `DEPLOY_SHA`, including sandbox/data isolation, model-control
flags, spoken-response policy, integration-token write mode, promotional offers
and observation shadow. The silence WAV returned 48,044 bytes with unchanged
SHA-256 `59db6dfb709393b7aab8efcc2b395df4def43f1c976d8b157bb9009dbacdc0ec`.
The bounded post-creation query of this revision found no ERROR-or-higher events
or HTTP 5xx responses. This is a short deployment observation, not a live-call
qualification or a claim about future errors.

## Production

[Run 35162039018](https://github.com/delimatsuo/heykevin/actions/runs/35162039018)
was dispatched at **23:22:55Z**, after successful staging validation. Freshly
fetched main and the run's `headSha` both identify the approved candidate
`976202dfd418e0c4ca551a96e8075e5d5065b45b`; no `candidate_sha` was supplied to
the production workflow. All nine validation jobs passed. After the owner's
specific delegation, GitHub accepted the approval for production environment
`13924929328`, returning deployment `6492321518`. The production job began at
**2026-09-17T02:21:22Z**. The existing environment protection was used and was
not changed. The workflow completed successfully at **02:24:53Z**, with all ten
applicable jobs passing on the approved candidate.

Independent readback at **02:25:37Z** confirmed ready revision
**`kevin-api-00272-8z4`**, receiving **100% traffic**. Both the canonical and
alternate service URLs returned healthy production responses with the exact
approved SHA. Anonymous admin, CSS, JS and silence-WAV checks also passed.
Authenticated admin and physical calls were not exercised.

| Production artifact | Identity |
|---|---|
| Service-reported Cloud Build ID | `a65bdbee-10f1-4b53-8747-8b41992cbcaa` |
| Image digest | `sha256:e3577a7f189ee36e824f6c830c02160e98c45981fd76f7d5b041208057d5994c` |
| Revision created | `2026-09-17T02:23:51.243925Z` |
| Source object | `gs://run-sources-kevin-491315-us-central1/services/kevin-api/1789611696.188024-bc7e8b3df3094e54a11e80842bf1c46a.zip#1789611696554389` |
| Previous revision retained | `kevin-api-00271-q4j` |

Allowlisted runtime comparison with the previous production revision found no
differences except `DEPLOY_SHA`. Production APNs/App Store, Firestore/RTDB,
integration-token write mode, promotional offers, receptionist shadow and
lapsed-number-release settings were preserved. Health continued to report
staging safety controls false, model tools true and automatic terminal actions
true. The silence WAV retained the same size and SHA-256 recorded above.

The bounded metadata-only query since this revision's creation returned no
ERROR-or-higher events or HTTP 5xx responses. This short observation verifies
deployment health, not the reported conversation's resolution. No duplicate
dispatch, workflow rerun, rollback or flag change was performed.

## Phone acceptance

Still open. Production verification is complete. Install or confirm
**1.3.1 (41)** from TestFlight and record the installed build before testing.
Check the warning, tap Take a message while Kevin speaks, and continue as the
caller to check that Kevin takes the message without repeating answered
questions. Preserve the original finish-current-speech and conditional
three-second pause checks. Provider health, source tests and mock request
envelopes do not close these rows.

## Actions cost

The repository is public `delimatsuo/heykevin`; billing owner is `delimatsuo`.
Each dispatch runs seven Ubuntu test shards, Python quality, the stable `Test`
aggregator and one applicable deploy job. Tests/quality have 15-minute timeouts,
the aggregator five minutes, and deployment 30 minutes. Dispatch concurrency
serializes the runs without cancellation. No hosted macOS, duplicate dispatch,
rerun, billing change or runner migration is included.

Expected chargeable Actions cost is **$0**. September paid account usage at
preflight was **$71.470765035**, with Hey Kevin net usage $0. Recent validation
and deployment durations imply roughly 17–20 rounded Ubuntu runner-minutes per
environment (about $0.102–$0.120 gross reference cost). Existing Cloud Run and
Cloud Build are used within the owner's approved rollout; no new provider
resource or paid API qualification is introduced.

At the documentation preflight on September 17, paid account Actions usage was
**$71.625853898**, with Hey Kevin net usage still **$0**. The follow-up PR changes
Markdown only. Its nine required Ubuntu validation jobs have $0 expected
chargeable cost, and its feature push/main merge do not deploy either service.
