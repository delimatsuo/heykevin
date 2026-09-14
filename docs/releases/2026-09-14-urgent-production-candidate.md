# Urgent notification production candidate — September 14, 2026

**Production requested; deployment approval and production acceptance are not
recorded as complete.** [Run 34897431657](https://github.com/delimatsuo/heykevin/actions/runs/34897431657)
was dispatched at `2026-09-14T21:12:04Z` from main at exact SHA
`7377c7ba402297625dec5de97a2250f50b1c8013`, without `candidate_sha`.
Only Deli may approve its production environment. Do not dispatch a duplicate.

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
The source PR used 402 runner-seconds and refreshed staging used 678. Production
is expected to be of similar order; Cloud Build/runtime charges are separate.
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

1. Deli approves the production job; verify the completed workflow, exact
   production `deploy_sha`, revision, traffic and basic serving afterward.
2. Record the installed iOS version/build and perform the owner-run notification,
   CallKit, pickup, message, timeout and stale-alert checks in the current PRD.
   This backend release does not authorize TestFlight/App Store publication.
3. Keep the HTML frontend proposal available for review. The broader native
   frontend refactor and later product ideas remain deferred.

A later documentation-only merge may advance main. The production run above is
bound to its recorded dispatch SHA; do not replace that identity with a newer
main commit when reporting its result.
