# Hey Kevin notification audit — September 28, 2026

The audit found an incomplete account-recovery path after moving to a new iPhone, a misleading notification-permission label, and a separate notification setup gap for returning users on a fresh installation.

The account-recovery defect is the strongest explanation for the owner's observed phone behavior. Its code path is confirmed and the phone/network observations corroborate it. The phone's Keychain contents were not inspected, so the exact missing credential and the destination of owner-specific pushes remain unconfirmed.

## Scope and frozen versions

- Owner request: investigate notifications that stopped working, including the recent move to iPhone 18.
- Installed app observed in TestFlight: **1.3.2 (42)**.
- Packaged source: `043b4c8e9cea09f95def904e98045abe901f94d5`.
- Audited checkout: `8bb4f122e776b9d3ad7b1dd23767fcc1dc722fd3`, clean before audit artifacts.
- Freshly fetched main: `6e35cb250f8d26f72dd7d36999fa7af6ce15640f`.
- Live health: production `kevin-api-00273-f7g`, deploy SHA `2b402796d1cbffaf59bcb6ecb63d661d9dd0ef12`.
- Live backend source and audited backend source have identical `app/` tree `39f721c4f72af480c939488ad63a091c9be1a781`.

This was a source/history and bounded read-only runtime audit. No application fix, upload, deployment, provider configuration change, notification toggle, account mutation, or agent-placed call occurred.

## Main findings

### P1 — Restored setup can hide a missing account session

Kevin stores its account ID and bearer token in device-only Keychain items. Apple documents that these items do not migrate to a different device. Meanwhile, Kevin restores `isOnboarded` and the cached Kevin number from UserDefaults independently.

A restored installation can therefore show the main app and the old Kevin number while lacking the credentials needed to load calls or register the new phone. Missing subscription storage also defaults locally to “trial,” so the visible Free Trial label does not establish current server account state.

The entry screen checks `isOnboarded`, without requiring a usable account session. Device registration returns before sending a request if either account ID or token is absent. The missing-session path never obtains the HTTP 401 that normally triggers “Sign In Again.”

Build 42 references:

- `KeychainManager.swift:7–8,41–55`: device-only accessibility; failed/missing reads return nil.
- `AppState.swift:82–91`: independently restored account ID, onboarding flag, subscription cache and number.
- `KevinApp.swift:28–35`: main-screen routing uses onboarding alone.
- `APIClient.swift:459–477`: local credential guards skip registration.
- `ContentView.swift:299–309`: recovery UI depends on `needsReauth`.

The correct repair preserves device-bound credentials and restores the existing account through Apple sign-in. Making credentials migrate merely to hide this defect is unnecessary.

### P1 — The notification label and empty Calls screen conceal this state

The September 14 frontend initializes its permission variable to `.notDetermined`, then returns on invalid local authentication before asking iOS for the actual authorization status. That makes the label remain **Not enabled** even when iOS already permits notifications.

Call history similarly clears and returns on invalid local authentication. It supplies neither rows nor a visible missing-session error, which explains the blank Calls screen.

Build 42 references:

- `SettingsView.swift:80,216–244,540–555,1617–1625`.
- `CallHistoryModel.swift:402–407,460–490,534–535`.
- `CallHistoryView.swift:56–98`.

Tapping Enable may refresh the label and ask Apple for a token. It cannot recreate missing account credentials.

### P2 — Returning users can skip notification setup on a fresh installation

The new-account provisioning path requests notification authorization. The successful existing-account restore path saves credentials and sets onboarding complete but does not perform notification reconciliation or a registration retry.

If token callbacks and the initial foreground retry happened before Apple sign-in completed, registration can remain incomplete until another activation or callback. This is distinct from the restored-container problem above: valid returning-user credentials should allow call history to load, so this gap alone does not explain every symptom observed during the restart.

Build 42 references: `OnboardingView.swift:918–1013,1276–1280`; `KevinApp.swift:48–77,113–130`.

### P2 — Server registration can retain a previous phone's regular token

The server keeps ordinary and VoIP tokens in a single account-level primary document. It merges only nonempty fields. A new VoIP-only registration therefore preserves the previous regular token until a regular registration replaces it.

That behavior can leave the two channels associated with different installations. It makes accepted pushes to an old phone a plausible consequence of incomplete migration. It does **not** establish that this is the owner's current stored token.

Blindly clearing the omitted token is unsafe: ordinary and VoIP callbacks arrive separately. The first repair should complete registration on the current installation; a broader multi-device design is a separate hardening decision.

Deployed references: `app/api/voip.py:143–155`; `app/services/push_notification.py:445–473`.

### P2 — Registration acknowledgement can falsely report success

A caught persistence error returns an error dictionary with default HTTP 200. Build 42 discards the response body and sets `isRegistered` from HTTP 200 alone.

The offline probe reproduced this defect. No matching registration-write failure was found in the queried live logs, so it is not the leading explanation for this incident.

References: `app/api/voip.py:194–196`; `APIClient.swift:498–505`.

## Why it appeared after the phone change

| Date | Change and significance |
| --- | --- |
| April 8 | `0fae051` introduced device-only account storage alongside independently retained onboarding state and account-level partial token merging. The underlying migration weakness is old. |
| May 19 | `710ca64` changed access to AfterFirstUnlockThisDeviceOnly and improved locked-launch recovery. The credentials still intentionally do not transfer to a different phone. |
| September 14 | `11eccd1` added the permission-refresh authentication gate and silent missing-auth history behavior. These were present by build 39 and remain in builds 40–42. |
| September 17 | Build 42 packaged these paths. The current backend was also deployed that day; there is no later backend source divergence in this audit. |
| September 25 | `8bb4f12` added APNs registration on authorized foreground activation. It is merged in PR #268 but **not included in installed build 42**. It also does not restore missing credentials or fix the misleading status. |
| Recent owner phone change | A new device can expose the earlier migration weakness even without a new server deployment. |

The earlier permission-only diagnosis was incomplete for the combined symptoms. Build 42 already requests APNs registration during an authorized cold launch; repeating that request cannot repair a missing account session.

## Runtime evidence

The mirrored phone showed iOS authorization, Lock Screen, Notification Center, banners, sounds and time-sensitive alerts enabled. Kevin still showed Not enabled, cached account setup, and blank Calls after a fresh launch.

During **13:17–13:28 UTC / 9:17–9:28 a.m. Eastern on September 28**, all matching production `/api/` request metadata contained exactly two entries:

| UTC | Request | Result |
| --- | --- | --- |
| 13:19:21 | Kevin/42 — GET /api/app/version | HTTP 200 |
| 13:25:38 | Kevin/42 — GET /api/app/version | HTTP 200 |

There were no account/profile, call-history, active-call or device-registration requests in that launch window. The version requests coincide with the observed launches and support working connectivity to the correct production service. Attribution to this particular phone is an inference from timing and the shared Kevin/42 user agent, not a device-specific identifier. Together with the UI state, these observations support the local missing-session path without directly inspecting Keychain.

The September 17–28 push-event query returned 45 APNs-accepted sends: 42 ordinary, two VoIP and one urgent. It found no matching rejection, unconfigured-key, expired-token or missing-token events. This means the queried backend events do not show a service-wide push failure; APNs acceptance does not prove notification display on this phone.

The September 1–28 registration/push query returned 284 matching rows across 11 anonymized accounts and no matching registration-write failures. Recent ordinary sends used a token last logged as registered September 24. Owner/current-phone attribution was not established and is not asserted.

A projected lookup of the owner's account/device metadata returned **HTTP 403**. The audit did not retry with other identities or broaden privileges. No raw account IDs, phone numbers, credentials, token previews, transcripts or call content are retained in these audit artifacts.

## Verification and review

Three offline probes executed the actual deployed-equivalent registration handler with synthetic values and mocked Firestore:

1. VoIP-only registration retains the previous regular token.
2. A subsequent regular registration replaces that token.
3. A simulated persistence failure returns the error dictionary instead of an HTTP error.

All three passed. An initial attempt stopped at configuration initialization before executing any probes; the completed run used the repository test setup and synthetic configuration with dotenv disabled.

Two independent read-only source reviews returned all expected findings: five iOS and seven backend findings. A code reducer accounted for all 12 findings, grouped the overlapping local-session finding, and produced the accompanying evidence JSON. A separate fresh-context staff review verified the principal source claims and approved the audit with two wording conditions: treat phone attribution as inferred, and avoid claiming that current deployment identity rules out an intervening deploy or rollback. Both corrections are applied in these artifacts.

No physical phone migration was simulated, no new iOS candidate was tested, and notification delivery remains unaccepted.

## Smallest repair and acceptance criteria

1. After secure storage is usable, detect an onboarded installation with incomplete credentials and present **existing-account recovery**. Distinguish missing items from temporary storage unavailability.
2. Refresh iOS permission status independently of account/network loading. Show a clear sign-in recovery state in Calls.
3. On successful account recovery or returning-user setup, reconcile notification authorization, obtain current device tokens, and register them immediately.
4. Validate a successful registration acknowledgement; treat persistence failures as failures.
5. Preserve the original account and Kevin number. An unavailable recovery lookup must not silently create an account or provision a new number.
6. Test restored defaults with missing credentials; missing ID and missing token separately; delayed sign-in after both token callbacks; both callback orders; permission changes; locked-before-first-unlock recovery; registration failures.
7. Qualify the installed candidate using the owner's direct call to the existing Kevin number: verify an ordinary alert on the locked current phone, matching call detail/transcript, and final summary. APNs acceptance and successful SMS remain separate evidence.

The shipped app has no general Sign Out entry. Its Sign In Again alert is unreachable on this missing-credentials path. Do not instruct the owner to use Delete Account as a recovery workaround.

## References

- [Apple: device-only Keychain accessibility](https://developer.apple.com/documentation/security/ksecattraccessibleafterfirstunlockthisdeviceonly)
- [Apple: registering with APNs](https://developer.apple.com/documentation/usernotifications/registering-your-app-with-apns)
- [Merged foreground-registration repair, PR #268](https://github.com/delimatsuo/heykevin/pull/268)
- [Machine-readable audit evidence](2026-09-28-notification-migration-evidence.json)
