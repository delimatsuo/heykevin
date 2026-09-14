# Urgent notification production release — September 14, 2026

**Production deployed and verified at `2026-09-14T21:45:33Z`.** [Run 34897431657](https://github.com/delimatsuo/heykevin/actions/runs/34897431657)
was dispatched at `2026-09-14T21:12:04Z` from main at exact SHA
`7377c7ba402297625dec5de97a2250f50b1c8013`, without `candidate_sha`.
Deli approved its production environment. The run completed successfully at
`2026-09-14T21:39:36Z`; all ten applicable jobs passed. The serving revision is
`kevin-api-00270-l9s`, with 100% of production traffic. No duplicate was dispatched.

## Source and staging completed

[PR #244](https://github.com/delimatsuo/heykevin/pull/244) fixes message delivery
between old and new call handlers during a backend rollout. Required CI
[34896280482](https://github.com/delimatsuo/heykevin/actions/runs/34896280482)
passed all nine applicable jobs on `bfb4584c6b3cb80855062aa2165505b8547f7ab6`;
the merge and reviewed head have tree `01420cd531b617cfe193c686355af1091563f81e`.
The [compatibility packet](2026-09-14-rolling-call-compatibility.md) records the
full tests, mutation evidence, exact-head independent reviews and recovery patch.
Automatic Cursor/Codex reviews were unavailable because of account limits;
required CI and the independent reviews completed without outstanding findings.

[Staging run 34896587160](https://github.com/delimatsuo/heykevin/actions/runs/34896587160)
passed all ten applicable jobs, completed at `2026-09-14T21:10:17Z`, and deployed
that merge SHA. Parent verification at `2026-09-14T21:11:16Z` established:

- Revision `kevin-api-staging-00169-muy`, 100% serving traffic, exact matching
  runtime and `/health` SHA, `status=ok`, environment staging and service name.
- Separate Firestore/RTDB, sandbox APNs/App Store, matching runtime identity
  and required deployment variables. Promotional offers and observation shadow
  remained disabled. No credentials, caller records or authenticated admin API
  were used in the verification.
- Staging safety controls enabled, Gemini model tools and automatic terminal
  actions disabled. These differences prevent treating staging as equivalent
  to a live production call.
- Anonymous `/admin` returned HTTP 200 and title `Kevin AI Admin` (4,984 bytes).
  CSS and JS returned HTTP 200 with 7,795 and 24,575 bytes respectively.

The previous staging record remains historical. Its `2b56fdae...` candidate is
superseded by this repaired source.

## Production verification

After the completed workflow, independent parent checks at `21:45:33Z` confirmed:

- `/health` returned `status=ok`, environment `production`, service `kevin-api`,
  revision `kevin-api-00270-l9s` and the exact dispatch SHA above. Cloud Run's
  latest ready revision and 100% serving traffic matched; its runtime
  `DEPLOY_SHA` matched too.
- Production Firestore/RTDB bindings, APNs (`APNS_SANDBOX=false`) and App Store
  environment were correct. Promotional offers and observation shadow remained
  false; the existing lapsed-number release flag remained true. These checks
  did not change flags or credentials.
- Production's Gemini staging-safety controls remained disabled, with model
  tools and automatic terminal actions enabled as before.
- Anonymous `/admin` returned HTTP 200 with title `Kevin AI Admin` and 4,984
  bytes; CSS/JS returned HTTP 200 and 7,795/24,575 bytes. No authenticated app
  endpoint, caller record, notification send or live call was exercised.

An independent Codex staff reviewer (`gpt-6-astra`, high) separately verified
the completed run, both production URLs, exact SHA/revision, 100% traffic and
Cloud Run health conditions at `21:46:27Z`, with no findings.

This establishes deployed source identity and basic serving. It does not
establish notification delivery, voice-provider behavior, installed-iPhone
acceptance or cessation of all older workers.

## Production preflight and recovery

Immediately before dispatch, main and the reviewed tree matched the candidate
above, the worktree was clean and no workflow was active or queued. Production
was healthy at `407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc`, revision
`kevin-api-00269-42l`, serving 100% of traffic. Production APNs/App Store,
Firestore/RTDB, runtime identity, telephony binding and required deployment
variables were rechecked. Promotional offers and observation shadow were false.
The production environment's required reviewer was verified as `delimatsuo`.

The existing workflow performs nine validation jobs before its gated deployment
job. All use bounded standard Ubuntu runners. The public repository and personal
billing owner are `delimatsuo`; chargeable runner usage is $0 under
[GitHub's public-repository policy](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
The source PR used 402 runner-seconds, refreshed staging used 678 and the
completed production run used 650. Cloud Build/runtime charges are separate.
No workflow, runner, billing or credentials configuration was changed.

The approved plan is one normal production deployment with slower forward
recovery. The tested containment patch can suppress new urgent alerts and
screening-summary updates while retaining the repaired call-action protocol.
Applying it requires a new candidate, matching containment tests, CI, a build
and separate owner deployment approval. It is not an instant traffic rollback
or a prebuilt production revision. A shared call-action defect needs a targeted
forward fix. Do not send live durable operations back to unmodified 407.

The workflow does not automatically roll back failed health checks. During
revision overlap, old workers retain their pre-existing pickup/fallback races;
the adapter repairs message delivery, not the old writers. Do not claim global
operation ownership before incompatible workers cease, or use a fixed wait as
proof. No maintenance, forced call termination or ingress restriction is planned.

## Remaining acceptance

1. Record the installed iOS version/build and perform the owner-run notification,
   CallKit, pickup, message, timeout and stale-alert checks in the current PRD.
   This backend release does not authorize TestFlight/App Store publication.
2. Keep the HTML frontend proposal available for review. The broader native
   frontend refactor and later product ideas remain deferred.

A later documentation-only merge may advance main. The production run above is
bound to its recorded dispatch SHA; do not replace that identity with a newer
main commit when reporting its result.
