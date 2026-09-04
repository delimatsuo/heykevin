# Hey Kevin iOS 1.2.11 Release Preparation

## Status

**Source candidate only.** This candidate has not been archived, signed with a distribution identity, uploaded, submitted, approved, deployed, or released. Build 36 remains source/local only, does not exist in App Store Connect, and has not been uploaded, validated, tested in TestFlight, submitted, approved, or released.

## Candidate Source Baseline

- **Repository:** `delimatsuo/heykevin`
- **Branch:** `codex/prepare-ios-1-2-11`
- **Marketing Version:** `1.2.11`
- **Candidate Build Number:** `36`

## Selected Candidate Build Number

- **Marketing Version:** `1.2.11`
- **Candidate Build Number:** `36`
- **Rationale:** Build `35` was previously cut for `1.2.10` source candidate. Following monotonic build numbering, build `36` is selected and configured in `ios/project.yml` (`CURRENT_PROJECT_VERSION: "36"`) and the generated Xcode project.
- *Status:* Build `36` is declared in source/local build configuration only; it does not exist in App Store Connect.

## Bounded Source Delta Since Build 35

The iOS delta incorporated in this release candidate includes:

1. **Server-Supplied Call Forwarding for International Countries:**
   - Dials server-supplied MMI codes (`ForwardingInstructions.swift`, `OnboardingView.swift`, `SettingsView.swift`) for all non-NANP countries.
   - Retains tested local carrier split (Verizon vs GSM) for US and CA.
   - Implements robust error handling and offline fallbacks.

2. **Settings Country Control:**
   - Added country picker in Settings (`SettingsCountry.swift`, `SettingsView.swift`) allowing users to view and select their forwarding country.
   - Onboarding keys initial forwarding setup on account country with device locale fallback.

3. **Regulatory Business Address Capture:**
   - Onboarding captures street address and city for the six regulatory countries (`DE`, `FR`, `IT`, `ES`, `PT`, `BR`) required for Twilio number provisioning.
   - Guard against caching unconfirmed business addresses (`f42b3c6`).

4. **Version & Build Configuration:**
   - Updated `ios/project.yml` to `MARKETING_VERSION: "1.2.11"` and `CURRENT_PROJECT_VERSION: "36"`.
   - Regenerated `ios/Kevin.xcodeproj/project.pbxproj` using `xcodegen generate`.
   - Updated `ios/KevinTests/AppVersionTests.swift` marketing version test fixture.
