# Acquisition release packaging brief

Owner instruction: "ok, complete the tasks" authorizes the previously listed
backend rollout, acquisition measurement/privacy work, iOS delivery and Personal
store/ad preparation. The owner has confirmed availability for a two-phone test
when the new build is ready. No customer calls or new spend allowance is implied.

## Bound inputs and ownership

- Repository: `delimatsuo/heykevin`, public; owner/billing owner `delimatsuo`.
- Worktree: `Kevin/.worktrees/acquisition-activation-20260930`.
- Branch: `codex/acquisition-rollout-20260930`.
- Base SHA: `b62eea38d9ab5656fb8d809fb872f05d9c81147d`.
- Base tree: `72d6eb6d001c9f0c5f51ca8b810cd2acd197911b`.
- App `6761427495`, bundle `com.kevin.callscreen`, team `3FLG8W6B95`.
- Apple readback: 1.3.3 (44) public, latest build 44; package 1.3.4 (45).
- Master owns judgment, verification, Xcode generation, Git and provider actions.
- Builder owns only the three allowlisted source/configuration files below.

Independent backend and iOS preflight nodes returned 2/2 results. Backend deploy
with measurement off proceeds independently of packaging. Privacy publication
and same-source rollback verification precede measurement activation. Signed
delivery and owner-observed carrier forwarding precede any ad resumption.

## Exact builder changes

Use headless `gemini-3.7-flash-medium`: this is deterministic configuration and
literal caption replacement, with no behavioral implementation or tests to add.

1. `ios/project.yml`: change only MARKETING_VERSION from 1.3.3 to 1.3.4 and
   CURRENT_PROJECT_VERSION from 44 to 45.
2. `ios/Kevin/Debug/AppStoreScreenshotFixtures.swift`: change only the title and
   subtitle for the three Personal screenshot scenarios as follows:

   | Scenario | Title | Subtitle |
   |---|---|---|
   | personalLive | Know who's calling and why | Kevin screens calls routed to your Kevin number. |
   | personalRecents | Catch up on your time | Review recent calls and their summaries. |
   | personalDetail | Get the context before calling back | Read what the caller said. |

   Preserve all Debug guards, mock content and other scenarios. Do not include
   personalKevin in the new screenshot set.
3. `ios/Kevin/Resources/PrivacyInfo.xcprivacy`: retain tracking=false, empty
   tracking domains and the exact existing UserDefaults CA92.1 declaration.
   Every collected-data row remains linked=true and tracking=false. Declare
   exactly these eleven distinct data types and purposes, using Apple's full
   `NSPrivacyCollectedDataType` and `NSPrivacyCollectedDataTypePurpose` prefixes:

   | Data type suffix | Purpose suffixes |
   |---|---|
   | PhoneNumber | AppFunctionality |
   | PhysicalAddress | AppFunctionality |
   | Name | AppFunctionality |
   | UserID | AppFunctionality, Analytics |
   | Contacts | AppFunctionality |
   | AudioData | AppFunctionality |
   | OtherUserContent | AppFunctionality |
   | DeviceID | AppFunctionality |
   | PurchaseHistory | AppFunctionality, Analytics |
   | ProductInteraction | AppFunctionality, Analytics |
   | AdvertisingData | Analytics |

PhysicalAddress covers the existing regulatory number-provisioning address
collected during onboarding and passed to Twilio. Contacts are synced names/numbers; OtherUserContent covers transcripts and
configuration; DeviceID covers notification tokens. Purchase history is also
used for entitlement verification. Account-linked attribution, onboarding
intent, inbound/forwarding and purchase observations support first-party
analytics. No IDFA, cross-company tracking, location analytics, raw-token storage
or identity export is introduced. Existing Apple sign-in email claims are
transient verification input, not a persisted email field in this source.

## Builder bounds

Read this brief and the three allowlisted files. Do not read `.env` files or
credentials. Do not run Git mutations, xcodegen, tests, builds, provider/network
commands, or another agent. Do not modify any other file. Stop if pinned source
values do not match. Return JSON with changed_files, changes and unresolved;
verification belongs to the master.

## Master acceptance

Review the literal diff and parse the plist, checking exact data categories and
purposes. Regenerate Xcode project, verify version/build across generated
configurations, run a guarded simulator build/test plus signed Release archive
and inspect package entitlements. Required exact-head CI and fresh independent
review precede merge. Apple upload acceptance, processing, internal testing,
App Review and public availability are separately recorded.

Apple category/purpose definitions checked September 30, 2026:
- https://developer.apple.com/app-store/app-privacy-details/
- https://developer.apple.com/documentation/bundleresources/app-privacy-configuration/nsprivacycollecteddatatypes/nsprivacycollecteddatatype

Before activation, publish matching App Store and public-policy disclosures.
Record campaign/ad-group/optional keyword IDs, enums and timestamps as linked
account data, removed with account deletion. Raw attribution tokens are exchanged
transiently and not saved. Do not claim historical backfill or anonymous records.

After backend rollout, retain the new measurement-off revision for rollback.
Older pre-acquisition source omits the map from profile redaction and must not
serve traffic or tags once measurement data exists. Ordinary client write access
on that old source was not demonstrated. Remove temporary activation tags after
promotion so they cannot bypass a later rollback.
