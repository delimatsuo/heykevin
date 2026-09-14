# Urgent call handoff release candidate — September 14, 2026

This completes the approved N1 source work: Pick up and Take a message,
passive notification dismissal, one bounded urgent owner wait, and a saved
Urgent alerts preference. The [current PRD](../../kevin-prd.md) defines scope;
the [contract](../superpowers/plans/2026-09-14-urgent-call-handoff.md) defines
request and ownership semantics. The [interactive HTML](../frontend-concept/index.html)
is available for design review. The wider native frontend refactor and later
features remain deferred.

Base: `66f8a446e0e873ecb280c39aaab60f7cf0448a36` (merged PR #241).
[PR #242](https://github.com/delimatsuo/heykevin/pull/242) passed its nine required
workflow jobs on `66030cd41318d75b423373aebfcb6c5f3ffc63cb` and merged as
`2b56fdaec3eafddbd42f4ebb0516e60db812161d` with the same tree. This packet records
source evidence. The later [staging record](2026-09-14-urgent-staging.md) records
the authorized cancellation and staging deployment; device acceptance remains open.

## Resulting behavior

- Pick up, Take a message and timeout compete for one durable decision on the
  exact authenticated call. Retried transaction callbacks and duplicate entry
  points cannot issue another transfer. Rejected transactions preserve records.
- A message request stays pending until its voice engine accepts the instruction.
  Failed acknowledgement retries do not repeat that instruction. Accepting an
  instruction does not claim that the caller finished leaving a message.
- Urgency preserves the first owner-wait deadline, at most 30 seconds. With no
  owner decision, Kevin says the owner is unavailable and continues taking a
  message. A failed preparation or storage interruption cannot restart the wait.
- Saved Urgent alerts controls extra urgent pushes/rings. Urgent lock-screen
  text is generic. Time-sensitive alerts use supported iOS behavior; this change
  makes no Critical Alert, Silent Mode or Do Not Disturb bypass promise.
- Urgent CallKit rings require the new device capability. They carry call
  identity and expiry; pickup obtains connection credentials only after the
  server accepts the action. Known-contact calls keep their original incoming
  UUID and conference. Older clients retain regular urgent notification delivery.
- Lost transfer responses retain the original operation. Status checks may
  establish that the exact caller already joined its registered conference;
  they never redirect it again. A definitely released preparation failure permits
  a later explicit retry. An unconfirmed failure does not.
- An authenticated fallback stream can confirm that a lost-response redirect
  reached Kevin. Confirmation rechecks owner, call, stream token, redirect nonce
  and live state in the transaction before accepting it.
- The iPhone shares action ownership across notifications, live controls and
  CallKit. Late responses, logout/login round trips, expired rings and old screen
  dismissals cannot connect or clear a different call. Connecting stays visible
  until native connected presentation takes over. Terminal direct-call failure
  does not leave an enabled button that cannot work.

## Local verification and independent review

- Final full backend suite: **7,836 passed, one skipped**, 30 existing warnings,
  134.22 seconds. `KEVIN_DISABLE_DOTENV=1` was set; network and application-default
  credential resolution were blocked (12 attempted connections and 26 credential
  resolutions denied). Fatal Ruff selectors, Python compilation and whitespace
  checks passed.
- Native iOS: full **125-test unit suite passed**, zero failures/skips, on the
  dedicated iPhone 16 / iOS 26.0 simulator. After the final Connecting and terminal
  direct-failure correction, all **22 call-action tests passed**, zero
  failures/skips and no compiler warnings. Result identifiers:
  `kevin-urgent-all-unit-20260914T165145Z-63347` and
  `kevin-urgent-final-actions-20260914T170416Z-70073` under the configured
  XcodeStorage result-bundle service. These are simulator tests, not physical
  iPhone acceptance.

- Parent backend mutation probes: 17 baseline cases passed. All six deliberate
  mutations were detected: rejected-record deletion (10 failures), missing
  committed nonce (one), missing live owner at finalization (two), credentials
  without an operation (one), repeated delivery during acknowledgement retry
  (one), and restarted owner deadline (one). Mutations executed in isolated
  Python processes and did not change repository source files.
- A separate actual-function mutation proves that removing the fallback stream
  token guard accepts a rotated token; production behavior rejects it. Tests
  also exercise the real media and Relay stream entry points.
- Seven Swift helper/parser mutations were detected using actual source on
  macOS Foundation: auth epoch, dismissal scope, preference revision, incoming
  ring tombstone, expiry, strict JSON Boolean and explicit ended-call liveness.
  These establish those helper contracts, not native CallKit delivery.
- HTML: final SHA-256
  `942c45e608987d8d539382c851b39860b1c0734e5d67bea92636d32a6a47cc21`.
  Eight timer probes, five detected timer mutations and seven truthful ended-call
  cases passed. The in-app browser covered real automatic timeout, action
  failures, preference suppression, stale alerts, keyboard focus, desktop and
  320px width with 200% product text. See [HTML evidence](../frontend-concept/VERIFICATION.md).
- Independent Codex staff review (`gpt-6-astra`, high) approved the final backend,
  native actions, HTML and scope documents with no remaining source findings.
  Final small UI and test-reliability corrections were reviewed again.
- The two existing bakeoff integrity pins deliberately track the reviewed
  `media_stream.py` SHA-256
  `246d7d2ad55b3e8ddbe9e63c0a07f34d7c9755f4cf466812dad91893b78240ae`.
  Structural import and activation prohibitions, and all other pins, remain.

The native unit-test host suppresses launch registration and scene network
effects. The dedicated simulator has no owner account. Tests use injected
credentials/call state and mocked providers; the host can still construct
AppState and read its empty simulator keychain. No real call, APNs delivery,
carrier forwarding, physical-device behavior or production effect is proved.

## Hosted-check preflight

Historical pre-publication rebind: September 14, 2026. Repository and personal
billing owner: `delimatsuo`, public repository `delimatsuo/heykevin`. At that
preflight, remote main was the base above. Main requires the strict, fail-closed
`Test` check (GitHub Actions app 15368); no additional rulesets were returned.

Feature-branch pushes do not run the deployment workflow. A PR to main runs
seven Python shards, Python quality and Test: nine standard `ubuntu-latest`
jobs. Shards and quality have 15-minute limits, Test five minutes, and obsolete
PR runs are cancelled by concurrency. Staging and production jobs are skipped
on a PR; merging main does not deploy. No workflow or runner changes are included.

Comparable PR #241 used approximately 8.9 runner-minutes. Standard public-repo
runners have $0 chargeable Actions cost; see
[GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
The account billing endpoint was unavailable to the current token; no positive
paid-CI allocation is assumed. Do not rerun a failed or stale workflow without
diagnosing it and rebinding the exact candidate and cost.

Required exact-HEAD CI must finish before merge. No separate Codex review workflow
was found in repository configuration; independent source review is recorded
above. Inspect any automatically configured external review on the publishing PR
and resolve valid findings before merge.

## Release gates still open

Production was rechecked during this work and remains
`407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc`, revision `kevin-api-00269-42l`.
The [old production run](https://github.com/delimatsuo/heykevin/actions/runs/33938295394)
on superseded `c093ed6ef1c505e8d68f01d6f6f365bf2553ac55` was cancelled on September 14
after Deli explicitly authorized cancellation. It contains the earlier
delayed-summary defect; do not revive it. The dispatch queue was verified clear
before the authorized staging run began.

After reviewed-clean merge:

1. Completed: Deli authorized staging and cancellation of the old run. Its
   terminal cancellation and the exact remote main SHA were verified before dispatch.
2. Completed: staging run `34887443367` deployed the approved merge SHA and
   passed exact-SHA health, serving-traffic, isolation and anonymous static
   smoke checks. See the [staging record](2026-09-14-urgent-staging.md). Rebind
   main before any production dispatch; production does not accept `candidate_sha`.
3. Only Deli approves the production environment. Verify the completed workflow,
   serving revision and exact production `deploy_sha` afterward.
4. Prepare the authorized iOS build under a fresh signing/App Store envelope,
   record its version/build, and perform the PRD's owner-device checks: both
   notification actions, old/ended alerts, duplicate taps, urgent CallKit answer,
   known-contact answer, 30-second no-answer fallback, preference persistence,
   locked/unlocked notification behavior and failed/unknown transfers.

The source implementation itself performed no deployment. The later authorized
staging run is recorded separately. Production deployment, App Store action,
real-call/device qualification and the PRD's final release acceptance remain open.

## Routing and audit record

Master model: Codex gpt-6-astra (high)

Builder model/tier: headless agy gemini-3.7-flash-high

Routing reason: File-disjoint backend, iOS and HTML implementation from pinned
contracts. Staff-level concurrency, ownership and unknown-outcome repairs were
rebound to Codex gpt-6-astra after independently reproduced builder defects.
The master owns integration, verification and all Git operations.

agy transport=headless: true

Input tokens: 2552574 across the six recorded builder passes below; Codex usage unavailable

Output tokens: 470220 across those passes; Codex usage unavailable

Total tokens: 3022794 across those passes; Codex usage unavailable

Retries: 0 transport retries; explicit repair passes are listed separately

Audit defects found: Exact combined unique count unavailable. Reproduced issues
included transaction deletion/retry, duplicate redirects, lost-response recovery,
unacknowledged message delivery, abandoned deadlines, stale session/call ownership,
direct-call routing, unsafe response parsing and HTML timer/focus/outcome handling.

Audit disposition: Remediated; independent final source reviews clean. Provider,
physical-device and deployment acceptance remain separate release gates.

| Builder pass | Input | Output | Total |
|---|---:|---:|---:|
| Initial frontend concept | 190935 | 60107 | 251042 |
| Concept interaction repair | 289978 | 93834 | 383812 |
| Urgent HTML implementation | 272113 | 74352 | 346465 |
| Urgent HTML repair | 312322 | 48739 | 361061 |
| Backend implementation | 786101 | 102981 | 889082 |
| iOS implementation | 701125 | 90207 | 791332 |
