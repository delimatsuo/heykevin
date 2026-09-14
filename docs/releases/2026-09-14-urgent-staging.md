# Urgent notification staging release — September 14, 2026

**Staging deployed and verified at `2026-09-14T19:40:27Z`.**
[Run 34887443367](https://github.com/delimatsuo/heykevin/actions/runs/34887443367)
completed successfully on `2b56fdaec3eafddbd42f4ebb0516e60db812161d`: all ten
applicable jobs passed and production was skipped. Revision
`kevin-api-staging-00167-qiq` serves that SHA with 100% of staging traffic.

## Candidate and authorization

- Repository: `delimatsuo/heykevin`.
- Approved staging candidate: `2b56fdaec3eafddbd42f4ebb0516e60db812161d`, the merge
  of [PR #242](https://github.com/delimatsuo/heykevin/pull/242).
- Reviewed source: `66030cd41318d75b423373aebfcb6c5f3ffc63cb`. Both commits have tree
  `206df497fdb67acec78007ece0c495fbbe6c4438`; all nine required jobs passed on the
  reviewed source. Local and independent review evidence is in the
  [source packet](2026-09-14-urgent-call-handoff.md).
- Deli's continuation authorized staging; the subsequent “you may cancel it”
  explicitly authorized cancelling superseded production run `33938295394`.
  Its terminal state was verified as `completed/cancelled` before dispatch.
- No other dispatch was active. One staging run was started from `main` with
  the exact `candidate_sha`; production was not requested.

## Runtime boundaries and recovery

Before deployment, staging served revision `kevin-api-staging-00165-kif` at
`c093ed6ef1c505e8d68f01d6f6f365bf2553ac55`, with 100% of staging traffic. Its
promotional-offer flag was false, and its source contained both the default-off
and eligibility guards required by the rollback workflow. It is the recorded
pre-deployment recovery target, not a recommended production candidate.

The staging runtime used separate Firestore/RTDB resources, sandbox APNs and
App Store settings, and no production-resource bypass (unset, default false).
The GitHub staging variables passed the corresponding isolation checks, including
the configured runtime/build service accounts and WIF identity. No secret
payloads, caller records or authenticated admin data were used for verification.

The workflow checks the tagged revision's exact health SHA before moving staging
traffic. It does not automatically roll back a failed post-promotion check.
The recorded recovery path is the existing `rollback.yml` traffic-split method
for staging, subject to the required recovery authorization and fresh target checks.

## Verification

Independent parent checks after the completed workflow established:

- `/health`: `status=ok`, `environment=staging`, `service=kevin-api-staging`,
  exact `deploy_sha=2b56fdaec3eafddbd42f4ebb0516e60db812161d`, revision
  `kevin-api-staging-00167-qiq`.
- Cloud Run metadata: 100% of serving traffic targets that same revision;
  the runtime's `DEPLOY_SHA` also matches.
- Staging safety controls enabled; Gemini model tools and automatic terminal
  actions disabled. Firestore/RTDB isolation, sandbox APNs/App Store,
  production-resource bypass off and promotional offers disabled were rechecked.
- Anonymous `/admin` returned its expected static title; `/static/admin.css`
  and `/static/admin.js` returned HTTP 200 and nonempty content (7,795 and
  24,575 bytes). No `ADMIN_API_TOKEN`, authenticated admin API or call endpoint
  was used. Requests had bounded timeouts.
- Production health remained `ok` at `407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc`,
  revision `kevin-api-00269-42l`.

The independent staff release review approved staging with conditions covering
old-run cancellation, exact identity smoke, isolation and a recorded rollback
target. Those bounded deployment conditions are satisfied by the checks above;
owner-device and production qualification remain open.

Health and static assets establish deployment identity and basic serving. They
do not establish notification delivery, CallKit, carrier forwarding, real voice
behavior or physical-iPhone acceptance. Staging's Gemini model tools and automatic
terminal actions are disabled, so its behavior is not production-equivalent.

## Cost and remaining release steps

The public repository's owner and billing owner are `delimatsuo`. The staging
workflow runs seven Python shards, quality, Test and Deploy to Staging: ten
standard `ubuntu-latest` jobs, with 15-minute Python/quality, five-minute Test
and 30-minute deployment limits. The comparable prior staging run used 634
runner-seconds; this successful run used 661 runner-seconds. Chargeable GitHub
Actions cost is $0 under
[GitHub's public-repository runner policy](https://docs.github.com/en/billing/concepts/product-billing/github-actions). No runner or
workflow configuration was changed. Cloud Build/runtime charges are separate;
local Cloud Build history access was denied, and no IAM or billing settings were changed.

Production remains at `407bf0bc7b6604f33f0113e28c3a2ba82e48b7dc`, revision
`kevin-api-00269-42l`. Production deployment still requires owner authorization
and Deli's environment approval. Rebind remote main before dispatch; do not pass
`candidate_sha` to production. This staging candidate identifies the tested
application source even if later documentation commits advance main.

The iOS notification implementation is merged and simulator-tested. Signing,
TestFlight/App Store release, owner-device acceptance and the wider frontend
refactor remain separate approvals. No unrelated feature work was started.
