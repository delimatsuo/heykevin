# Notification migration repair — local verification

The repair detects restored setup without usable credentials, reconnects the existing account through verified Apple sign-in, and immediately reconciles notification permission and current device tokens. Registration is acknowledged only after both the token write and required profile update succeed. The [incident audit](2026-09-28-notification-migration-audit.md) remains a historical snapshot, including its limits on phone attribution.

## Source boundary

- Base: `6e35cb250f8d26f72dd7d36999fa7af6ce15640f`; branch: `codex/notification-migration-recovery`.
- Changed source/test/project files: 23. SHA-256 over sorted repository paths, each followed by NUL, its file bytes, and NUL: `036b107c3bf3bf5b81e02fbab4c60bef2c0d6362d63711d9fb59ed249aca623d`. Includes changed/new `app/`, `ios/`, and `tests/` files versus the base; excludes documentation.
- Version remains **1.3.2 (42)**. These changes are not in the owner's installed build 42.
- No signing, archive, upload, deployment, provider configuration, real call, or notification-send action was performed for this source slice.

## Verification that ran

| Evidence | Result |
| --- | --- |
| Real FastAPI registration router plus related VoIP, screening-summary and live-transcript suites | 28 passed; three existing dependency deprecation warnings |
| Full `KevinTests`, after the hosted review correction | 329 passed, zero failed/skipped, zero runtime warnings |
| Account recovery, registration and Settings suites | 65 passed before/after original mutation probes; final 68 passed after the checkpoint mutation was restored |
| Fatal Python lint (`E9,F63,F7,F82`), localization JSON parse and `git diff --check` | Passed |

iOS tests used the required `xcodebuild-external` wrapper, Debug, signing disabled, and the existing iPhone 16 / iOS 26.0 simulator (`8F2B4B62-693C-412A-A0A4-082AB860A0F4`). This is not physical iPhone 18 qualification. Local result bundles are under the managed XcodeStorage results directory:

- Full suite: `kevin-notification-review-fixes-20260928T160630Z-77839/result.xcresult`.
- Final focused suite: `kevin-notification-final-focused-20260928T160919Z-83709/result.xcresult`.
- Mutants: `kevin-notification-mutation-proof-20260928T161053Z-86587/result.xcresult`.
- Restored source: `kevin-notification-restored-20260928T161223Z-89774/result.xcresult`.
- Full suite after hosted review correction: `kevin-notification-interrupted-recovery-20260928T162749Z-17886/result.xcresult`.
- Restored final checkpoint source: `kevin-notification-checkpoint-restored-20260928T163357Z-29049/result.xcresult`.

Python command (dotenv disabled; synthetic dependencies only):

```sh
KEVIN_DISABLE_DOTENV=1 python -m pytest -q tests/unit/test_device_registration.py tests/unit/test_voip_token.py tests/unit/test_screening_summary_push.py tests/unit/test_live_transcript_push.py
```

## Adversarial proof

Five independent Swift guard removals were run together against their five targeted tests. Every target failed for the intended reason: interrupted credential commits opened the session; stale account epochs published recovery; invalid registration response bodies were acknowledged; Settings skipped permission refresh; and a failed registration immediately retried. All five source files were restored byte-for-byte, then all 65 focused tests passed.

Two backend mutations removed the missing-ID HTTP guard and replaced persistence HTTP 500 with a default HTTP 200 error body. Four targeted cases failed, with seven unaffected cases passing. The backend file was restored byte-for-byte, and all 28 focused backend tests passed again.

The hosted-review correction received a separate mutation probe: reinstating the early onboarding guard made both new checkpoint-routing tests fail for the intended reasons. Xcode completed both tests, then lingered collecting diagnostics; root interrupted only the verified wrapper for this run. The finalized result bundle (`kevin-notification-checkpoint-mutation-20260928T162913Z-20389/result.xcresult`) confirms two failed tests, zero passes, and the intended assertion failures. The wrapper exited 130 after cleanup; source was restored byte-for-byte. This adds one detected mutation to the seven original probes.

## Independent review and disposition

The graph used independent safety and CI inventory nodes, one headless builder, root audit/verification, and a fresh-context reviewer, followed by a targeted review of the hosted finding's correction. The root reducer accounted for all four final node outputs; none were omitted. Builder claims were not treated as test evidence.

Root repaired eight groups of builder-draft defects, including failure retry loops, stale acknowledgement writes, missing async ownership checks, identity publication order, partial secure commits, history error ownership, compilation issues and HTTP-test setup. The independent reviewer identified seven issues across review rounds; hosted Codex review found an eighth. All were resolved and independently re-reviewed clean:

1. Clear stale deletion state unconditionally in the required profile update; reject false update acknowledgements.
2. Clear unowned contact consent before credential writes; a pending commit after restart cannot establish consent ownership through its staged ID.
3. Retain safe accessibility migration for readable existing credentials, including the bearer token; unavailable reads do not write and failed migration stays unavailable.
4. Hydrate surviving account/Apple identity before routing missing-token recovery, so continuity checks still apply after initially locked storage.
5. Route create-endpoint returning-account results through the same fresh lookup, profile validation and verified credential publication. Failed profile/storage never opens the account.
6. Bind contact consent explicitly granted during new signup to that flow's authenticated Apple identity and newly created account, independently of restored defaults.
7. Preserve the selected provisioning mode when that same new signup is returned as an existing account by the later create request.
8. Inspect the recovery checkpoint before the onboarding flag, including clean installs. Persist the verified account and Apple identity plus recovery purpose in one atomic Keychain item before individual credentials. Both pending purposes forbid creating another account; only a verified unfinished account may resume provisioning after fresh verification and a successful credential commit. Missing storage remains unavailable and retries retain their original purpose.

Builder: `gemini-3.7-flash-high`, headless transport, conversation `262cc94a-ae4d-489c-a93f-cc54f097dd42`, zero retries. Usage as reported by the builder: input 957912, output 116031, total 1073943 (thinking 61613 and cache-read 15584047 reported separately). Root owns the corrected implementation and all Git actions.

## Publication and release boundary

Read-only inventory verified public repository `delimatsuo/heykevin`, billing owner `delimatsuo`, and unchanged workflows. An ordinary PR runs seven Python shards, Python quality and the stable required `Test` check on standard Ubuntu runners. Both deploy jobs are skipped. Recent successful runs used 10–12 rounded runner-minutes; this public-repository event has expected incremental paid Actions cost **$0**. No macOS hosted job or deployment is triggered. Exact candidate checks and configured Codex review must complete before merge; their durable results belong to the PR.

The first published head `e5dfaf4e213b215ac6d43d3c101be8007145d73d` passed all nine required checks in [run 36449792556](https://github.com/delimatsuo/heykevin/actions/runs/36449792556). Its hosted Codex review produced item 8 above, which required an updated candidate. The optional Cursor Security Agent returned a neutral check with `Security Review run failed`; that is unavailable security evidence, not a passed security review. No retry or configuration change was made for that optional service.

The remaining owner gate is a new iOS release candidate and the backend deployment. After installation, use the owner's direct call to the existing Kevin number with the iPhone locked and Focus off; observe the ordinary alert, matching call detail/transcript and final summary. APNs acceptance, simulator tests and successful SMS are not physical notification acceptance. The source repair does not establish that the current phone is fixed.
