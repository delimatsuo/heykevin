# SMS release preparation — iOS 1.3.3 (44)

**The reviewed backend is now deployed and verified in production.
App Store 1.3.3 (44) is prepared but has not been submitted to Apple review.**
Owner phone checks for SMS delivery and opt-out behavior remain pending. All
timestamps are UTC.

The owner authorized these commands after the release-readiness checklist.
The owner also confirmed that a call they pick up needs no summary SMS. That
existing behavior is preserved. The package and earlier source checks are in
[the build 44 receipt](2026-09-28-owner-sms-ios-44.md).

## Source and deployment

| Binding | Value |
| --- | --- |
| Repository / billing owner | `delimatsuo/heykevin` / `delimatsuo` |
| Release source | `56de9e64a11de0819a5101ca12298aa1d2032ce5` |
| App tree | `17449bb6ae3103e206b49096248d83305b2b4737` |
| iOS tree, matching build 44 | `d9181e020ab202b73ff941001ba0c4886285f295` |
| Work branch at dispatch | `codex/sms-release-133` |
| Prior production revision | `kevin-api-00273-f7g` |
| Prior production source | `2b402796d1cbffaf59bcb6ecb63d661d9dd0ef12` |

The obsolete notification-only production
[run 36458376853](https://github.com/delimatsuo/heykevin/actions/runs/36458376853)
was cancelled and read back as completed/cancelled at `21:40:31Z`, before
starting its replacement. Its source `f45fa6295cfcd290f4c6df2532318f1eb79dfa57`
predated the SMS changes. No overlapping dispatch was started.

[Staging run 36488115100](https://github.com/delimatsuo/heykevin/actions/runs/36488115100)
completed successfully with all seven Python shards, Python quality, the required
Test aggregator, and the staging deployment passing. Production was skipped as
intended. The dispatch explicitly selected the exact release source above.

Readback and HTTP smoke checks at `21:51:30Z` verified:

- `kevin-api-staging-00179-viv` receives 100% of staging traffic.
- Canonical and tagged health endpoints both return the release SHA, correct
  environment, service and revision.
- All 58 environment entries and the runtime service account were preserved;
  only `DEPLOY_SHA` changed. Comparison used hashes, without exposing secrets.
- `/admin`, `/static/admin.css`, and `/static/admin.js` return 200.
- Unsigned empty requests to both SMS webhooks return 403. A synthetic protected
  contractor path without credentials returns 401. These probes send no SMS.
- The new revision has zero matching error, worker-error, traceback or
  exception-type log entries in the inspected 30-minute window.

The staging image is
`us-central1-docker.pkg.dev/kevin-491315/cloud-run-source-deploy/kevin-api-staging@sha256:4d4527766b2270948bfcf5bf6317bb78758612c0d515e25be28423889d716a81`.
Authenticated account operations and physical SMS delivery are not established
by these smoke checks.

After staging passed, a fresh fetch confirmed clean HEAD and remote main still
at the release source. [Production run 36488930257](https://github.com/delimatsuo/heykevin/actions/runs/36488930257)
was dispatched from main with `target=production` and without `candidate_sha`.
All nine validation jobs passed. Readback confirmed `Deploy to Production`
waiting for environment `production` (`13924929328`), with `delimatsuo` as the
required reviewer at the initial checkpoint. The owner subsequently reported
completion; GitHub's approval history confirms `delimatsuo` approved that exact
environment. The workflow then completed successfully for the release source.

The current **owner-supplied AGENTS.md instructions in this conversation** say:
“only Deli approves it, never an agent and never via the API.” Those newer
instructions override the older September 17 agent-approval text in repository
guidance. The agent did not approve the production environment.

### Production verification

Live readback and smoke checks at `22:20:12Z` verified:

- `kevin-api-00274-5fv` receives 100% of production traffic and is Ready.
- Both the canonical production URL and Cloud Run service URL return healthy
  responses with source `56de9e64a11de0819a5101ca12298aa1d2032ce5`, the expected
  environment, service and revision.
- Compared with prior revision `kevin-api-00273-f7g`, the runtime service account
  and all 58 environment entries were preserved except `DEPLOY_SHA`. Values were
  compared in memory using hashes; credentials were not printed or copied into
  this receipt.
- Admin HTML, CSS and JavaScript return 200. Unsigned empty requests to the
  inbound SMS and owner SMS status webhooks return 403. An unauthenticated
  synthetic contractor lookup returns 401. These probes send no texts.
- No error, worker-error, traceback or exception-type entries were found for the
  new revision in the inspected 30-minute log window.

The production image is
`us-central1-docker.pkg.dev/kevin-491315/cloud-run-source-deploy/kevin-api@sha256:cbed472ff7609b787db07ab6c45e63af9e8ad0c4d341b5fa6ed230b2f6364880`.
This confirms backend deployment and the bounded server checks above, not SMS
delivery, authenticated app preference behavior, or phone notification delivery.

## App Store preparation

| Apple record | Verified value |
| --- | --- |
| App | `6761427495` / `com.kevin.callscreen` |
| Version / build | `1.3.3` / `44` |
| Version ID | `2bb9a9bf-643c-440b-be41-f043c90b6782` |
| Attached build ID | `e3076a1f-6f9c-4053-a1cf-772625cf8ce7` |
| Build processing | `VALID`, unexpired |
| Version / submission state | `READY_FOR_REVIEW` |
| Review submission ID | `45c3f19d-94b7-4283-adc1-e4d885eae3b7` |
| Release mode | `MANUAL` |

A review item was added to the draft submission; it was **not sent to Apple**.
Existing English description, keywords, support and marketing URLs, review
contact, and all six 6.7-inch screenshots were preserved. Screenshot source
checksums, sizes and ordering match 1.3.2; every asset is COMPLETE. No new upload,
legal acceptance, credential change or automatic public release was performed.

What's New describes notification recovery, the SMS switch, STOP/START, and
summaries after Kevin-handled calls end. Its SHA-256 is
`acd5a97bd0afed2868f78f35cb2a3b0c27b0bd158621121e206adb3fb655a908`.
Independent review found one minor onboarding-label correction. The corrected
review instructions match the new-account Sign up with Apple and returning-user
Sign in with Apple flow and were read back exactly. Final notes SHA-256:
`d785f48a4e193fadaf82534081bc519880f54f71011d5219517433db98ca1229`.

The latest public version remains 1.3.2 at this checkpoint. READY_FOR_REVIEW
describes the editable submission draft; it does not mean WAITING_FOR_REVIEW,
approval, or public availability.

After production verification, the existing English TestFlight notes for build
44 were updated to state that the matching backend is live and to describe the
completed-call, app-off, STOP/START and locked-phone checks. Apple readback matched
the exact notes. Localization ID: `f45ddb27-4bee-4cb3-924c-261a0db9e675`;
notes SHA-256:
`775c256b0b538dfc30846954a99079af3c919aa25119c547fdf7a62965f6406d`.
No build or testing-group assignment changed. App Review submission is still
pending the phone checks.

## Remaining release checks

1. Owner tests a call Kevin handles through hang-up and confirms the summary SMS
   arrives afterward. Confirm the app switch disables owner texts, STOP blocks
   them, and START does not override an app switch left off. Confirm notification
   recovery on the phone; source tests do not establish delivery.
2. Submit the prepared Apple review submission after acceptance, then read back
   its actual review state. Manual release keeps public publication separate.

No database migration, new secret, new provider configuration or feature flag
change is required. New consent fields are additive. An old-revision rollback
would ignore the new SMS preferences and opt-outs, so it must not be described
as preserving consent enforcement. Preserve the additive records.

## Cost and retention

Both dispatches plan ten jobs on standard Ubuntu runners in a public repository:
nine validation jobs and the selected deployment.
Job timeouts and the required fail-closed Test check remain intact. Expected
incremental paid Actions cost is $0; no paid allocation was added. This excludes
existing Cloud Build/Run and telephony costs. The receipt-publication preflight
at `21:58:09Z` read September paid Actions usage of $79.304402357. Billing reports
can lag; this release adds no paid Actions allocation.

Provider responses and inspection helpers are retained only in the task-owned
TMPDIR scratch until this text evidence is saved and the checkpoint is complete.
No binary archive is retained.
