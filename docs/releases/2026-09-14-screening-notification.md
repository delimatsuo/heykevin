# Screening notification release packet — September 14, 2026

Scope: finish N1 in the [current PRD](../../kevin-prd.md). The same change
reconciles the PRD and roadmap; it does not start the later-feature backlog.

## Release baseline and replacement

- Production observed September 14: `407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc`,
  revision `kevin-api-00269-42l`.
- Staging observed September 14: `c093ed6ef1c505e8d68f01d6f6f365bf2553ac55`,
  revision `kevin-api-staging-00165-kif`.
- [Old production run 33938295394](https://github.com/delimatsuo/heykevin/actions/runs/33938295394)
  is waiting on the staging SHA above. **Do not approve it:** it contains the
  confirmed delayed-summary defect. Only Deli may cancel it; it holds the
  dispatch concurrency group.
- The replacement is the reviewed PR containing this packet. Resolve its exact
  remote head and subsequent merge SHA from GitHub before staging or production
  dispatch. A local commit, green PR, or merge is not deployment evidence.

## Necessary repairs

The three voice engines previously discarded the screening-summary task handle.
A slow extraction could finish after pickup or hangup and send an actionable
notification for an ended screening session. The repair retains and cancels that
task when screening ends, rejects stale scheduling, and checks session liveness
before extraction and immediately before entering the notification sender.

The fallback voice engine also referenced a nonexistent transcript accessor.
It now derives spoken conversation text from the existing conversation record.
Internal language instructions and tool results are excluded from caller speech.
An independent reviewer reproduced the language-instruction leak; the repair adds
a real deterministic-fallback regression instead of mocking extraction.

This cannot retract a request already transmitted to APNs. Real notification
delivery/order and stale-alert behavior remain owner-device acceptance checks.
No Swift, voice prompt/model, hold duration, subscription, provider, flag or
caller-facing SMS changes are included in this repair.

## Local evidence

- Original defect independently reproduced with paused extraction, pipeline
  teardown and mocked notification delivery.
- All three engines cover normal delivery, duplicate hold starts, delayed
  extraction at stop and hold completion, stale work after teardown, and
  extraction that resists cancellation. Tests assert the contractor and call ID.
- Mutation: removing the post-extraction liveness check causes all three
  cancellation-resistant cases to fail.
- Mutation: removing summary-task cancellation, including the Relay stop task
  list entry, causes all six stop/hold-completion cases to fail.
- Mutation: restoring the nonexistent fallback transcript accessor causes its
  active-delivery test to fail.
- Mutation: restoring the unfiltered fallback transcript makes the real
  language-switch/fallback regression fail. Actual caller speech quoting a system
  message remains intact.
- Focused pre-review suite: 89 passed. Final lifecycle/push subset: 21 passed.
  Full pre-review backend suite: 7,740 passed, one skipped. Final candidate
  checks and review results will be recorded on the published PR.
- Tests use fictional data and mocked providers. Local socket connections and
  application-default credential resolution were blocked in parent runs
  (the full suite attempted eight socket connections and 29 credential resolutions;
  all were denied). No real call, APNs delivery or device behavior is proved by these tests.

## Release sequence and remaining acceptance

1. Finish independent review, required exact-head CI and any configured review;
   merge the repaired candidate only when clean.
2. Deli cancels the old production run. Verify its terminal state before any new
   dispatch; never queue another run behind it.
3. Fetch and record the exact remote `main` SHA. Dispatch staging with that
   `candidate_sha`, wait for success, and verify staging health reports it.
4. Rebind `main` immediately before the production dispatch. Dispatch production
   without `candidate_sha`; only Deli approves its environment gate.
5. Verify workflow success and the production `deploy_sha` and serving revision.
6. Record the owner's installed iOS version/build. Verify caller/reason updates
   while locked and unlocked, correct banner navigation and Pick Up, ended-call
   and old-alert behavior, and usable screening/transcript if notification fails.

Steps 2–6 are not completed by this source change. The [roadmap](../current-roadmap.md)
contains dated release observations; re-check live state before acting.

## Hosted-check preflight

The repository and personal billing owner are `delimatsuo`; the repository is
public. PRs targeting `main` run seven Python shards, Python quality and the
stable fail-closed required check `Test`: nine standard `ubuntu-latest` jobs.
The shard/quality timeout is 15 minutes, the aggregator timeout is five minutes,
and PR concurrency cancels obsolete runs. Feature-branch push and merge to
`main` do not deploy.

The comparable inspected PR run used about ten rounded Linux job minutes.
Standard runners in this public repository have $0 chargeable Actions cost;
see [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
Account-wide September billing was unavailable to the current token. No positive
paid-CI allocation is assumed, and this preflight does not authorize provider
spend or change release gates.
