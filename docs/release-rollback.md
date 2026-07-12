# Release Rollback Runbook

This runbook covers manual Cloud Run rollback through the `Rollback` GitHub
Actions workflow. The workflow is an emergency control, not a substitute for
the normal release gates.

## Safety Boundaries

- The workflow runs only from `main` and serializes with normal deploys for the
  selected environment.
- Staging and production use separate GitHub environments, identities, service
  accounts, services, databases, and target naming rules.
- Production rollback requires the exact confirmation text `production` and
  explicit operator authorization.
- Never place phone numbers, transcripts, message payloads, OAuth material,
  provider tokens, or customer identifiers in workflow inputs or incident notes.
- The workflow records only environment, method, revisions, and deploy SHAs in
  the GitHub step summary.
- The current application does not need to answer `/health` for rollback to
  start. Existing traffic and deploy provenance are read from the Cloud Run
  control plane, including a valid multi-revision traffic split.
- Every target is attached to a run-scoped temporary traffic tag and must pass
  environment, runtime identity, readiness, deploy SHA, and exact `/health`
  checks through that isolated URL before any user traffic moves.

## Choose A Method

### Traffic split

Use `traffic-split` when a previously deployed, Ready Cloud Run revision is the
known-good target. This is the fastest rollback and does not rebuild an image.

The target must be an exact revision name for the selected environment:

- Staging: `kevin-api-staging-00065-jes`
- Production: `kevin-api-00150-abc`

The workflow verifies that the revision belongs to the selected service, is
Ready, contains exactly one 40-character `DEPLOY_SHA`, uses the expected runtime
identity and data boundaries, and passes isolated health validation. Production
revision SHAs must also resolve to commits in `main` history. Only then does the
workflow route 100% of service traffic to that exact revision and repeat the
traffic and health proof.

### Release tag redeploy

Use `redeploy-tag` when the original revision is unavailable or when a clean
build from a known release commit is required.

The target must be an exact environment-scoped tag:

- Staging tags begin with `staging-`.
- Production tags begin with `prod-`.

The workflow verifies the tag ref, resolves it to one commit, requires that
commit to be an ancestor of `origin/main`, and deploys from a detached checkout.
It preserves the selected service's existing environment configuration and
updates only `DEPLOY_SHA`. The candidate is deployed with `--no-traffic` and a
run-scoped tag. It receives no user traffic unless its isolated health response
proves the expected environment, service, revision, and SHA. After that proof,
the workflow routes traffic to the exact candidate revision and verifies again.

A legacy release that returns only a generic `{"status":"ok"}` health response
is not a valid target for this workflow. It fails before cutover. Use a newer
known-good target with the identity health contract or make an explicit incident
roll-forward decision; do not weaken the proof during an outage.

## Execute A Staging Rehearsal

1. Identify a known-good staging revision and its deploy SHA from the prior
   successful deployment evidence.
2. Confirm the current staging `/health` response and record only its revision
   and deploy SHA.
3. In GitHub Actions, run `Rollback` from `main` with environment `staging`, the
   selected method, and the exact target. Leave production confirmation blank.
4. Require the workflow's `Rollback verified` summary, including prior traffic
   and deploy provenance plus the exact serving target, before considering the
   rehearsal successful.
5. Exercise a synthetic call and the message-delivery canary. Confirm latency,
   interruption, replay, and payload-free observability gates.
6. Roll forward through the normal staging deployment workflow and verify the
   candidate receives 100% of traffic. Normal deploys explicitly restore traffic
   to the latest Ready revision after either a traffic rollback or a zero-traffic
   candidate deployment. The deploy is successful only when latest-created and
   latest-ready match and `/health` proves the expected environment, service,
   revision, and commit SHA.

## Production Procedure

Production rollback is permitted only after explicit authorization and a named
incident decision. Record the selected known-good target and reason outside any
customer-data payload, then run the workflow with environment `production` and
the exact confirmation text `production`. A successful job must prove 100%
traffic and exact `/health` identity before the incident is downgraded.

If verification fails, treat the service state as unknown. Do not repeatedly
rerun mutations. Inspect Cloud Run traffic and health using read-only commands,
then choose an explicit roll-forward or another validated target.

## Compatibility Caveat

Rolling back to a revision from before message-delivery receipt callbacks were
introduced can make provider callbacks return `404`. Treat that as an incident:
preserve provider retries, stop destructive cleanup, and roll forward to a
compatible revision. Do not add a temporary payload-logging workaround.

## Evidence To Retain

- GitHub Actions run URL and immutable workflow commit SHA
- selected environment and method
- previous traffic allocation and deploy provenance
- verified serving revision and deploy SHA
- release-gate and canary result links
- incident decision and operator authorization for production

Do not retain callback bodies, transcripts, message content, full phone numbers,
or credentials in release evidence.
