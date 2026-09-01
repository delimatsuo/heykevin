# Hey Kevin iOS 1.2.10 Release Preparation

## Status

**Source candidate only.** This candidate has not been archived, signed with a distribution identity, uploaded, submitted, approved, deployed, or released.

## Candidate Source Baseline

- **Repository:** `delimatsuo/heykevin`
- **Branch:** `codex/ios-1.2.10-release-prep`
- **Baseline Commit:** `8e637d29db0100db9cd7a5c2d1754433fc49e05c`
- **Baseline Tree:** `5f0cca6c617d9ba68cad2b932c0bb37966fd5864`

## Public-Build Provenance

- **Public Version:** 1.2.9 (verified via public store listing).
- **Public Binary Build Provenance:** Unresolved. App Store Connect was not authenticated and no retained local Kevin archive was found. Build 34 is merely the current source-declared build number in `project.yml`; its exact relationship to the live public binary, including the binary's actual build number, Git commit SHA, and upload date, is unknown.

## Conservative Comparison Baseline

- Commit `dae40cc9f3629ef2ab2cecf440fadc855fe3a7ab` is the historical source commit that labeled 1.2.9 and source build 34 in `project.yml`.
- It serves as a conservative comparison baseline for source changes, not a confirmed public-binary artifact SHA.

## App Store Build Number

- **Build Number:** `TBD`
- `CURRENT_PROJECT_VERSION` intentionally remains `34` in `ios/project.yml` and the generated project until authenticated, read-only App Store Connect inventory confirms the next accepted build number.

## Bounded Source Delta Since `dae40cc`

Prior to this candidate's version-bump and test changes, the iOS source delta between `dae40cc` and `HEAD` consists of 12 commits touching 8 paths (+647 lines, -29 lines):

1. **Appointment-confirmation persistence:**
   - Kept appointment cards visible after owner confirmation ("Added to Google Calendar" with booked time) instead of disappearing (`CallHistoryView.swift`).
   - Server-confirmed `caller_notified` state returned and recorded rather than assuming delivery or discarding response payload (`APIClient.swift`).
2. **Caller-texted persistence:**
   - Retained the "The caller was texted this time" indicator across reloads/reappearances by reading persisted `caller_notified_at` on `CallRecord` (`Call.swift`, `CallHistoryView.swift`).
3. **Fail-closed account deletion:**
   - Preserved local credentials and contractor state when server deletion fails; prevented stranding users in de-authenticated active accounts (`APIClient.swift`, `AccountDeletion.swift`, `SettingsView.swift`).
   - Mapped HTTP 401/404 to already-deleted state; required explicit `{"status": "ok"}` on 2xx responses (`AccountDeletion.swift`, `AccountDeletionTests.swift`).
4. **Subscription warning before deletion & localizations:**
   - Added flow warning active subscribers that account deletion does not cancel auto-renewing Apple subscriptions, providing direct link to App Store subscription management (`SettingsView.swift`, `AccountDeletion.swift`).
   - Added tier-aware logic, race-free delayed task lifecycle, and full Spanish (`es`) and Brazilian Portuguese (`pt-BR`) localization strings (`Localizable.xcstrings`).
5. **Accurate Settings version display (Candidate Delta):**
   - Replaced static `Text("1.0.0")` with shared, testable `AppVersionService.marketingVersion()` (`SettingsView.swift`, `AppVersionService.swift`).
   - Bumped `MARKETING_VERSION` to `1.2.10` in `ios/project.yml` and regenerated PBX project.

*Note:* Because public-build binary provenance is unresolved, portions of this source delta may already be present in the live public binary.

## Proposed Customer-Facing Notes (Provisional)

*These notes are explicitly provisional pending confirmation of live build provenance. They must not overclaim changes as new to public users if already included in the live public binary.*

- **Version 1.2.10:**
  - Improvements to appointment confirmation and status persistence.
  - Added reminders to manage active Apple subscriptions when deleting an account.
  - Stability fixes and improved in-app version reporting.

## Verification Evidence & Evidence Ceiling

### Completed Verification Evidence

1. **Source & Static Configuration Verification:**
   - Shared marketing version helper added to `AppVersionService.swift` with fallback `"0.0.0"`.
   - `SettingsView.swift` updated to consume `AppVersionService.marketingVersion()`.
   - `ios/project.yml` updated with `MARKETING_VERSION: "1.2.10"` and `CURRENT_PROJECT_VERSION: "34"`.
   - `ios/KevinTests/AppVersionTests.swift` created with unit tests covering valid string, nil dictionary, absent key, and non-string values.
   - `xcodegen generate` executed successfully; `ios/Kevin.xcodeproj/project.pbxproj` generated without hand-edits.
   - PBX project verified for `MARKETING_VERSION = 1.2.10;` and `CURRENT_PROJECT_VERSION = 34;` across all configurations (Debug, Staging, Release).
   - `git diff --check` passed cleanly for tracked modifications; untracked additions (`docs/releases/ios-1.2.10.md` and `ios/KevinTests/AppVersionTests.swift`) were separately verified for trailing whitespace and final newlines and passed.

2. **Focused Debug Simulator XCTest (`AppVersionTests`):**
   - Focused execution of `AppVersionTests` passed (4 tests, 0 failures).
   - *Diagnostic Note on Network Traffic:* During app-host launch, at least two successful external GET requests were observed: Google `generate_204` returned 204, and staging `/health` returned 200 at revision `kevin-api-staging-00150-zuh` with `deploy_sha 57cb3af57140d9c44f8be6958e4700a0b8261dbe`. The app source also schedules an app-version GET to staging during Debug host launch, but the focused result log did not capture a complete request inventory, so the exact external request count is unknown.
   - No external write was intentionally invoked or observed, but comprehensive traffic capture was not available and therefore a blanket no-write proof is not claimed.
   - The test simulator was subsequently deleted.

3. **Full iOS Test Suite (Staging Configuration & Loopback Redirect):**
   - Full suite executed under Staging configuration with `ENABLE_TESTABILITY=YES` and `BACKEND_URL=http://127.0.0.1:9`.
   - Staging excluded the source-level `DEBUG` Google and health diagnostics; `BACKEND_URL` redirected the app-version request to `http://127.0.0.1:9`, where it failed locally.
   - No external request was observed in the retained `xcodebuild` action log.
   - No comprehensive traffic capture was performed, so a complete traffic inventory and provider/system non-access are not proven.
   - 38 tests passed, 0 failures.
   - Result bundle: `/Volumes/Extreme Pro/XcodeStorage/Results/kevin-1-2-10-full-ios-20260901T180101Z-83840/result.xcresult`.
   - The job-owned test simulator was subsequently deleted.

4. **Full Backend Regression Suite (Corrected Clean-Env Rerun):**
   - 7,481 passed, 1 skipped, 0 failed, 52 warnings in 104.14 seconds.
   - Executed with `env -i` containing exactly:
     - `PATH=/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin:/usr/bin:/bin`
     - `PYTHONDONTWRITEBYTECODE=1`
     - `KEVIN_DISABLE_DOTENV=1`
   - `KEVIN_DISABLE_DOTENV=1` satisfied the repository safety rule and no dotenv warning appeared.
   - *Credential Isolation Clarification:* This run is not termed credential-isolated. The Google auth library still warned that it discovered local end-user Application Default Credentials (ADC) without a quota project. The clean environment and dotenv flag do not prove filesystem credential isolation, and no credential content was inspected.
   - Tracked candidate status remained completely unchanged.

5. **Release Simulator Build & Bundle Metadata:**
   - Release simulator build succeeded via the required external `xcodebuild` wrapper with distribution code signing disabled via `CODE_SIGNING_ALLOWED=NO`.
   - Inspection reports the retained simulator app binary as ad hoc / linker-signed with no `TeamIdentifier`; this is not identity-based or distribution signing.
   - Result bundle: `/Volumes/Extreme Pro/XcodeStorage/Results/kevin-1-2-10-release-sim-20260901T175726Z-79328/result.xcresult`.
   - Built Release simulator app `Info.plist` inspected and verified:
     - `CFBundleIdentifier`: `com.kevin.callscreen`
     - `CFBundleShortVersionString`: `1.2.10`
     - `CFBundleVersion`: `34`
     - `BackendURL`: `https://kevin-api-752910912062.us-central1.run.app` (production URL)
     - `AppEnvironment`: `production`

### Evidence Ceiling & Non-Negotiable Boundaries

- **Local Scope Limitation:** The above verification proves only local simulator compilation, simulator test execution, and built-bundle metadata.
- **Signing & Provisioning Boundaries:** No distribution identity or provisioning profile was supplied or embedded in the retained simulator app; no identity-based or distribution signing was performed. The app reports ad hoc / linker-signed with no `TeamIdentifier`. Local signing-asset access was not independently audited (the build log includes a *Gather provisioning inputs* phase).
- **No Packaging or Distribution:** No release archive was generated, no binary was uploaded, no TestFlight distribution was created, no App Store submission occurred, and no public release was made.
- **No Physical Devices:** No physical device execution or hardware testing was performed.
- **No Production Deploy:** No production service was deployed or mutated. Current production runtime remains unverified despite the observed read-only staging health response.
- **Build Number Unresolved:** `CURRENT_PROJECT_VERSION` intentionally remains `34` pending authenticated, read-only App Store Connect inventory to determine the next accepted build number.

## Separate Release Tracks

- **Backend Independence & Deployment Status:** The post-August-23 backend source delta is not coupled by this iOS candidate. The latest observed GitHub production deploy workflow was `57cb3af` on 2026-08-23, but current production runtime was not queried, so runtime deployment status remains unverified.
- **Out of Scope:** International country/forwarding selection UI, voice model updates, removal of legacy Apple GET endpoints, and all external provider/cloud operations remain out of scope for this release candidate.
