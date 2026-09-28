# Notification recovery — iOS 1.3.3 (43) and backend registration

**TestFlight delivery complete:** Apple reports **VALID / IN_BETA_TESTING** for
**1.3.3 (43)**, explicitly present in the existing QA group with exact English
What to Test readback at **2026-09-28T17:42:07Z**. Production deployment is waiting
for Deli's GitHub environment approval; the backend repair is live in staging.

All timestamps in this record are UTC. The owner approved production backend
deployment and a corrected TestFlight upload after the notification audit and
repair. This release does not include a public App Store submission. Physical
notification delivery remains an owner phone acceptance step.

## Source and verification

[PR #269](https://github.com/delimatsuo/heykevin/pull/269) repairs account recovery
after device migration, notification permission/bootstrap sequencing, token
registration ownership and acknowledgements, and stale app-deletion state. The
[audit](../audits/2026-09-28-notification-migration-audit.md) and
[verification record](../audits/2026-09-28-notification-migration-fix-verification.md)
retain the diagnosis, 329 passing iOS tests, 28 focused Python tests, eight detected
mutations, and review findings with their resolutions. This source evidence does
not establish delivery to the owner's iPhone.

| Identity | Value |
|---|---|
| Backend release source / PR #269 merge | `f45fa6295cfcd290f4c6df2532318f1eb79dfa57` |
| Packaged iOS source | `3fa45548ff4df7b0961c57f3617ef1d1260bcca7` |
| Packaged source tree | `ea20510a0a5ce4fd7f99abfbcca58f75c0366e35` |
| Packaged iOS tree | `725005dcc67316987719cf371522400cefea06aa` |
| Backend app tree, identical in both releases | `12a8b132c8af8fbd30550ed6e20bf05012af7e3e` |
| Version metadata merge / PR #270 | `8f20abc507cdf3ea3fec337c43d33e82bd0db74a` |

[PR #270](https://github.com/delimatsuo/heykevin/pull/270) contains only version
substitutions in `ios/project.yml` and its generated project: two YAML values and
six generated settings. Root and independent review compared complete files;
application code, entitlements and export settings are unchanged from reviewed
main. XcodeGen, plist validation and `git diff --check` passed. The merge tree is
identical to the packaged source tree.

All nine package-head CI jobs passed in
[run 36457730002](https://github.com/delimatsuo/heykevin/actions/runs/36457730002).
Codex review completed on `3fa4554` at `17:24:22Z` with no findings and a positive
reaction at `17:24:25Z`. The optional Cursor Security Agent failed to run and
reported a neutral check; this is unavailable evidence, not a security pass.

## Signed package

The required guarded Xcode wrapper produced the archive and export. An existing
job held the global lock during preflight; this release waited without removing
or bypassing the lock. Managed result IDs:

- `kevin-notification-43-archive-20260928T172537Z-91650`
- `kevin-notification-43-export-20260928T172615Z-92741`

| Package identity | Verified value |
|---|---|
| Version / build | **1.3.3 (43)** |
| App / bundle / team | `6761427495` / `com.kevin.callscreen` / `3FLG8W6B95` |
| IPA size / SHA-256 | 6,842,593 bytes / `8fe44b758d8f2175ad0530821f782f572cdcff58a3b0a4bf03da83ea7887062d` |
| Archive / export / app dSYM UUID | `10AEA280-9272-3B10-B0A2-D25671E6F7E9` |
| SDK / Xcode | iPhoneOS 27.0 / `27A266a` |
| Distribution profile | `639cf43f-e398-42cb-9927-3b610eb59e4c`, expires July 15, 2027 |
| Backend binding | `https://kevin-api-752910912062.us-central1.run.app`, production |

Root verified strict/deep archive and export signatures, all 21 ZIP file contents,
37 app and 31 Twilio file-backed Mach-O sections matching archive to export,
matching app/dSYM UUIDs, production APNs, Apple Sign In and Time Sensitive
entitlements, `get-task-allow=false`, no Critical Alert entitlement, iPhone-only
family, purpose strings and background modes. The three screenshot/native-review
activation strings are absent. The three actual Kevin Swift compile commands
bind this worktree, use Release optimization and have no DEBUG/STAGING defines;
the archive reports no compiler warnings. The pre-existing `KEVIN_UNIT_TESTS`
launch guard is distinct from screenshot fixture activation.

Apple validation passed. Validation JSON SHA-256:
`9af1ddac5e8db1ca9666acf9c3dea72ee9f57b4c2df85187f9b3b35573694a7f`.
Independent review passed the exact IPA above after inspecting the raw archive,
export, source and logs. All 39 checkout Swift files in the compile filelist match
the packaged commit; the additional file is Xcode-generated asset symbols. All
283 Spanish and 283 Brazilian Portuguese translations match the catalog. Existing
untranslated entries use source fallback; this package adds no localization
omissions. Exactly one upload completed successfully at `17:35:20Z`, delivery UUID
`9650465a-be22-428b-9918-daa33bbff313`. Upload JSON SHA-256:
`19dc5c025982a4a1f94356df98e219cc4a2a0f6195a203e631d0539c14a3a5e7`.
At `17:42:07Z`, authenticated readback confirmed build `43` is `VALID`, unexpired,
`usesNonExemptEncryption=false`, and `IN_BETA_TESTING`; its prerelease version
`4f9050c3-af08-4ea9-a912-2f0ed93602d1` is `1.3.3`. The existing internal QA group
`ce552be4-c362-4021-9a90-b77d2e910d15` explicitly contains this build. Its existing
access-to-all-builds setting was preserved; no group mutation was required.
English What to Test notes `28c7eee2-79d0-4845-9a4d-3a3813582835` were written and
read back exactly, covering account recovery, permission, direct-call notification
and transcript tests. External beta state is `READY_FOR_BETA_SUBMISSION`; no
external review or public App Store submission was requested.

## Backend rollout

Compared with the previous production SHA
`2b402796d1cbffaf59bcb6ecb63d661d9dd0ef12`, the backend changes only
`app/api/voip.py`: registration requires valid input, reports persistence failures
as HTTP failures, and clears the stale deleted-app marker as a required update.
There is no schema migration, workflow, dependency or feature-flag change.

Staging [run 36457541519](https://github.com/delimatsuo/heykevin/actions/runs/36457541519)
passed all ten applicable jobs. Independent readback at `17:28:05Z` confirmed
revision `kevin-api-staging-00177-zis`, the exact release SHA, canonical and tagged
health, and 100% traffic. All 57 runtime environment fields excluding DEPLOY_SHA
and the runtime service account are unchanged. Sandbox APNs/App Store and staging
Firestore/RTDB isolation remain intact. Static admin assets and the canonical
three-second pause WAV returned 200; anonymous device registration returned 401.
A bounded metadata-only query since revision creation (`17:25:36.178046Z`) found
no error-severity or HTTP 5xx records. Registration success/failure behavior is
covered by synthetic ASGI tests; no real account token was overwritten for smoke
testing, and these serving checks do not prove physical push delivery.

Production [run 36458376853](https://github.com/delimatsuo/heykevin/actions/runs/36458376853)
was dispatched once from freshly fetched main at the exact backend release SHA,
without candidate_sha. All nine validation jobs passed. It currently waits for
Deli's production environment approval. The newest user-supplied AGENTS.md says
only Deli approves this gate; older repository text permitting agent approval
does not override that instruction. No duplicate dispatch or agent gate approval
was performed. Rollback reference: `kevin-api-00273-f7g`, previous production SHA
above. Production delivery is not yet claimed.

## Owner phone checks — open

- [ ] Install **1.3.3 (43)** from TestFlight and record the installed build.
- [ ] Open Hey Kevin; complete Sign in with Apple if recovery is requested.
      Confirm the existing account and Kevin number return without creating a
      replacement account or number.
- [ ] Allow notifications if iOS asks. A previously denied permission must be
      enabled in iPhone Settings; an app cannot silently override that choice.
- [ ] Turn Focus off and lock the iPhone. From an owner-controlled second phone
      with an unsaved caller number, call the Kevin number directly. Confirm a
      screening notification appears and opens the matching full conversation.
- [ ] Reopen the app and repeat a separate call to confirm registration remains
      usable. Reading a transcript must not answer the call.
- [ ] Continue the existing [transcript, message-taking and pickup checks](2026-09-17-transcript-first-ios-42.md#owner-phone-checks--open)
      on build 43, along with unperformed N1/N3 rows in the PRD. Those earlier
      package records remain historical evidence. VoiceOver remains deferred.

## Cost, routing and retention

Public repository and personal billing owner: `delimatsuo/heykevin` and
`delimatsuo`. Unchanged workflows use standard Ubuntu runners: nine validation
jobs per PR, ten applicable jobs per deploy. Required `Test` remains fail closed;
no hosted macOS job, duplicate retry or new paid allocation was introduced.
Expected incremental chargeable Actions cost is $0. September paid usage observed
before this release was $79.195083772, with reporting through `16:16:44Z`.
Cloud builds use the existing approved deployment pipeline.

Package binaries and raw delivery logs were retained in TMPDIR only through
verification, upload and Apple readback, then removed by the exact scratch
directory cleanup trap. Durable text records preserve identity and results;
no long-term binary archive was created.

Master model: gpt-6-astra
Builder model/tier: gemini-3.7-flash-low
Routing reason: pinned mechanical version substitutions; root regenerated and audited the complete project diff
agy transport=headless: true
Input tokens: 38208
Output tokens: 1134
Total tokens: 39342
Retries: 0
Audit defects found: 1 documentation date issue, corrected; 0 source/package findings
Audit disposition: source, package, Apple processing and QA delivery verified; production approval and physical acceptance remain open
