# Hey Kevin iOS 1.2.10 Release Preparation

## Status

**Source candidate only.** This candidate has not been archived, signed with a distribution identity, uploaded, submitted, approved, deployed, or released. Build 35 remains source/local only, does not exist in App Store Connect, and has not been uploaded, validated, tested in TestFlight, submitted, approved, or released.

## Candidate Source Baseline

- **Repository:** `delimatsuo/heykevin`
- **Branch:** `codex/ios-1.2.10-release-prep`
- **Baseline Commit:** `8e637d29db0100db9cd7a5c2d1754433fc49e05c`
- **Baseline Tree:** `5f0cca6c617d9ba68cad2b932c0bb37966fd5864`

## Authoritative App Store Connect Inventory & Public-Build Provenance

The coordinator performed an authenticated, read-only inspection of the App Store Connect UI without mutation:

- **App ID:** `6761427495`
- **Bundle ID:** `com.kevin.callscreen`
- **Public Version:** `1.2.9` is in state `Ready for Distribution` and uses build `34`.
- **Build Metadata Page State:** Build `1.2.9 (34)` showed `Validated` on its App Store Connect build metadata page.
- **Upload Timestamp:** `Aug 19, 2026 at 9:49 PM`.
- **Release History:** Displayed `Pending Developer Release` on `Aug 20, 2026 at 6:17 PM`, followed by `Ready for Distribution` on `Aug 22, 2026 at 11:41 AM`.
- **TestFlight Internal Groups:** `QA` group has 2 testers, 6 builds; build `1.2.9 (34)` has state `Testing` and expires in 78 days.
- **TestFlight External Groups:** `Alpha testers` group has 1 tester, 0 builds.
- **TestFlight iOS Build List:** The main iOS build row for `1.2.9 (34)` separately showed state `Ready to Submit` for external beta submission.

### Provenance Ceiling
- App Store Connect does not expose a Git commit SHA for build 34.
- `dae40cc9f3629ef2ab2cecf440fadc855fe3a7ab` is the only repository commit introducing source version `1.2.9` / build `34` (committed `2026-08-19T21:43:07-04:00`).
- Merge commit `b4d54f3d08efd156f7c260c34bdb4b9e516095e7` was created at `2026-08-19T21:45:29-04:00`, prior to the displayed 9:49 PM upload.
- `dae40cc` and `b4d54f3` share the identical full tree `377015222e60ab1aa7564fa4b8519a7580098234`, including an identical `ios` tree.
- This demonstrates strong timestamp and tree correlation, but is not cryptographic proof that the uploaded binary was built from that exact Git tree.

## Conservative Comparison Baseline

- Commits `dae40cc9f3629ef2ab2cecf440fadc855fe3a7ab` / `b4d54f3d08efd156f7c260c34bdb4b9e516095e7` represent the source tree that labeled 1.2.9 and build 34 and is strongly time-correlated with the App Store Connect upload (rather than a confirmed public-binary source tree).
- It serves as the conservative comparison baseline for all subsequent iOS changes leading to this candidate.

## Selected Candidate Build Number

- **Marketing Version:** `1.2.10`
- **Candidate Build Number:** `35`
- **Rationale:** Build `34` is verified as the accepted and live public build for version 1.2.9 in App Store Connect. Following monotonic build numbering, build `35` is selected and configured in `ios/project.yml` (`CURRENT_PROJECT_VERSION: "35"`) and the generated Xcode project.
- *Status:* Build `35` is declared in source/local build configuration only; it does not exist in App Store Connect.

## Bounded Source Delta Since `dae40cc`

The delta from `dae40cc9f3629ef2ab2cecf440fadc855fe3a7ab` through candidate baseline commit `8e637d29db0100db9cd7a5c2d1754433fc49e05c` consists of 12 post-1.2.9 iOS commits touching 8 paths (+647 lines, -29 lines). Separately, this candidate adds its version/build updates and test coverage:

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
5. **Accurate Settings version display & Candidate Bump:**
   - Replaced static `Text("1.0.0")` with shared, testable `AppVersionService.marketingVersion()` (`SettingsView.swift`, `AppVersionService.swift`).
   - Configured `MARKETING_VERSION: "1.2.10"` and `CURRENT_PROJECT_VERSION: "35"` in `ios/project.yml` and regenerated PBX project.

## Proposed Customer-Facing Notes (Provisional)

*These notes are provisional pending final release approval:*

- **Version 1.2.10 (Build 35):**
  - Improvements to appointment confirmation and status persistence.
  - Added reminders to manage active Apple subscriptions when deleting an account.
  - Stability fixes and improved in-app version reporting.

## Verification Evidence & Evidence Ceiling

### Completed Verification Evidence

1. **Source & Static Configuration Verification:**
   - Shared marketing version helper added to `AppVersionService.swift` with fallback `"0.0.0"`.
   - `SettingsView.swift` updated to consume `AppVersionService.marketingVersion()`.
   - `ios/project.yml` updated with `MARKETING_VERSION: "1.2.10"` and `CURRENT_PROJECT_VERSION: "35"`.
   - `ios/KevinTests/AppVersionTests.swift` created with unit tests covering valid string, nil dictionary, absent key, and non-string values.
   - `xcodegen generate` executed successfully; XcodeGen rerun after build selection was byte-deterministic for `ios/Kevin.xcodeproj/project.pbxproj`.
   - PBX project verified for `MARKETING_VERSION = 1.2.10;` and `CURRENT_PROJECT_VERSION = 35;` across all configurations (Debug, Staging, Release).
   - `git diff --check` passed cleanly across tracked modifications; when first introduced, the then-untracked release doc (`docs/releases/ios-1.2.10.md`) and unit test (`ios/KevinTests/AppVersionTests.swift`) additions were separately verified for trailing whitespace and final newlines and passed.

2. **Focused Debug Simulator XCTest (`AppVersionTests`):**
   - Focused execution of `AppVersionTests` passed (4 tests, 0 failures).
   - *Diagnostic Note on Network Traffic:* During app-host launch, at least two successful external GET requests were observed: Google `generate_204` returned 204, and staging `/health` returned 200 at revision `kevin-api-staging-00150-zuh` with `deploy_sha 57cb3af57140d9c44f8be6958e4700a0b8261dbe`. The app source also schedules an app-version GET to staging during Debug host launch, but the focused result log did not capture a complete request inventory, so the exact external request count is unknown.
   - No external write was intentionally invoked or observed, but comprehensive traffic capture was not available and therefore a blanket no-write proof is not claimed.
   - The test simulator was subsequently deleted.

3. **Build 35 Full iOS Test Suite (Staging Configuration & Loopback Redirect):**
   - Full suite executed for source build 35 under Staging configuration with `ENABLE_TESTABILITY=YES` and `BACKEND_URL=http://127.0.0.1:9`.
   - Staging excluded the source-level `DEBUG` Google and health diagnostics; `BACKEND_URL` redirected the app-version request to `http://127.0.0.1:9`, where it failed locally.
   - No external request was observed in the retained `xcodebuild` action log.
   - No comprehensive traffic capture exists, so complete provider/system non-access is not proven.
   - 38 tests passed, 0 failures.
   - Result bundle: `/Volumes/Extreme Pro/XcodeStorage/Results/kevin-1-2-10-build35-full-ios-20260901T190059Z-41522/result.xcresult`.
   - The job-owned test simulator was subsequently deleted.

4. **Backend Regression Suite (Earlier Functional Evidence):**
   - 7,481 passed, 1 skipped, 0 failed, 52 warnings in 104.14 seconds.
   - Executed with `env -i` containing exactly `PATH=/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin:/usr/bin:/bin`, `PYTHONDONTWRITEBYTECODE=1`, and `KEVIN_DISABLE_DOTENV=1`.
   - `KEVIN_DISABLE_DOTENV=1` satisfied the repository safety rule and no dotenv warning appeared.
   - *Scope & Credential Boundary:* This backend suite was executed prior to build-number selection and was not rerun after changing `CURRENT_PROJECT_VERSION` to 35. It is preserved as earlier functional regression evidence; subsequent modifications were strictly limited to iOS build configuration (`project.yml`), regenerated project metadata (`project.pbxproj`), and the release preparation document. This is not exact-new-tree provider/credential proof. The Google auth library warned of local end-user Application Default Credentials (ADC) without a quota project, the clean environment and dotenv flag do not prove filesystem credential isolation, and no credential content was inspected.
   - The candidate source state was unchanged by that test run.

5. **Build 35 Release Simulator Build & Bundle Metadata:**
   - Release simulator build for source build 35 succeeded via the required external `xcodebuild` wrapper with distribution code signing disabled via `CODE_SIGNING_ALLOWED=NO`.
   - Result bundle: `/Volumes/Extreme Pro/XcodeStorage/Results/kevin-1-2-10-build35-release-sim-20260901T190002Z-38707/result.xcresult`.
   - Built Release simulator app `Info.plist` inspected and verified:
     - `CFBundleIdentifier`: `com.kevin.callscreen`
     - `CFBundleShortVersionString`: `1.2.10`
     - `CFBundleVersion`: `35`
     - `BackendURL`: `https://kevin-api-752910912062.us-central1.run.app` (production URL)
     - `AppEnvironment`: `production`
   - `codesign` inspection: binary reports ad hoc / linker-signed with no `TeamIdentifier`. This is not identity-based or distribution signing. Local signing-asset access was not independently audited (the build log includes a *Gather provisioning inputs* phase).

### Evidence Ceiling & Non-Negotiable Boundaries

- **Local Scope Limitation:** The local build and test verification evidence proves only local simulator compilation, simulator test execution, and built-bundle metadata (and does not apply to the authenticated App Store Connect inventory).
- **Signing & Provisioning Boundaries:** No distribution identity or provisioning profile was supplied or embedded in the retained simulator app; no identity-based or distribution signing was performed. The app reports ad hoc / linker-signed with no `TeamIdentifier`. Local signing-asset access was not independently audited (the build log includes a *Gather provisioning inputs* phase).
- **No Packaging or Distribution:** No release archive was generated, no binary was uploaded, no TestFlight distribution was created, no App Store submission occurred, no approval was granted for build 35, and no public release was made. Build 35 remains source/local only and absent from App Store Connect.
- **No Physical Devices:** No physical device execution or hardware testing was performed.
- **No Production Deploy:** No production service was deployed or mutated. Current production runtime remains unverified despite the observed read-only staging health response.

## Separate Release Tracks

- **Backend Independence & Deployment Status:** The post-August-23 backend source delta is not coupled by this iOS candidate. The latest observed GitHub production deploy workflow was `57cb3af` on 2026-08-23, but current production runtime was not queried, so runtime deployment status remains unverified.
- **Out of Scope:** International country/forwarding selection UI, voice model updates, removal of legacy Apple GET endpoints, and all external provider/cloud operations remain out of scope for this release candidate.
