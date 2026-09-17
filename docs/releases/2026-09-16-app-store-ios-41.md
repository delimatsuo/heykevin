# App Store submission — iOS 1.3.1 (41)

**Submitted:** Apple reports **WAITING_FOR_REVIEW** for version **1.3.1 (41)**
at **2026-09-17T03:16:05Z** (September 16, 11:16 PM America/New_York).
Six updated screenshots are processed and included. Release policy remains
**AFTER_APPROVAL**. Submission is not Apple approval or public availability.

## Authorization and scope

The owner requested: “submit and make sure the screenshots are updated.” This
authorized submission of the existing build 41 and refreshed version screenshots.
No binary was uploaded again, and this submission changed no application source,
backend deployment, provider settings, pricing, subscription, custom product page
or advertising campaign.

The signed package and earlier verification are recorded in the
[build 41 delivery record](2026-09-16-message-confirmation-ios-41.md).
Production already includes the separately authorized
[Relay correction](2026-09-16-relay-message-state-backend.md).
Build 41 installation, physical-call retest and unperformed N1/N3 rows remain
open. VoiceOver remains deferred by the owner.

## Exact Apple submission

| Identity | Verified value |
|---|---|
| App / bundle / team | `6761427495` / `com.kevin.callscreen` / `3FLG8W6B95` |
| App Store version | `a6249802-b6c1-4f5e-8ed8-bbe17e9b8b58`, **1.3.1** |
| Selected build | `d0326647-8465-4757-8679-58186383a390`, **41** |
| Prerelease version | `98a95d4b-5c43-439c-a56f-a7c7d5d73a99`, **1.3.1** |
| Review submission | `23ccf83e-5465-4282-8b8f-7dc0376a8448` |
| Submitted date | `2026-09-17T03:16:04.335Z` |
| Version and submission state | `WAITING_FOR_REVIEW` |
| Release policy | `AFTER_APPROVAL`, preserved from the current public version |
| English localization | `582c5c19-3d90-45d7-ac1f-32f46bf3d241`, `en-US` |
| Screenshot set | `61830224-a065-4c34-9caa-3f06a8f88b23`, `APP_IPHONE_67` |

Fresh checks before submission verified the exact version/build relationship,
`VALID`, `APP_STORE_ELIGIBLE`, unexpired build and
`usesNonExemptEncryption=false`. There was no other unfinished app review
submission. One new submission was created, the exact 1.3.1 version was added,
and submission succeeded. Both the returned submission and subsequent version
readback reported `WAITING_FOR_REVIEW`.

The inherited description, keywords, support and marketing URLs, and review
contact/account details were preserved. English What's New was written and
read back as:

> Clearer Pick up and Take a message buttons make live calls easier to manage.
>
> Message requests now handle delayed confirmations more reliably, and Kevin transitions more naturally into taking a message.

The public US lookup still reported **1.3.0**, released
`2026-09-15T21:35:24Z`, after this submission. Its existing build 39 screenshot
sets were not modified. The new screenshots become public with the new release.

## Screenshot provenance and readback

Capture source was `929c79778668868f8079921a5f3dbe11057c2f36` in the isolated
`codex/build41-app-store` worktree. Its iOS tree,
`573c5b0d20a46b907744d830a8b4d1dc66a112da`, exactly matches packaged source
`3865883387e0a0ba555cd6fd187a6ab60c0caa28`. No Swift or project edits were needed.

A Debug simulator build used the required `xcodebuild-external` wrapper with
one explicit `-project ios/Kevin.xcodeproj`. The wrapper result is
`kevin-build41-store-screenshots-20260917T030113Z-54665/result.xcresult` under
the managed XcodeStorage Results directory. The built app identifies as
`com.kevin.callscreen`, **1.3.1 (41)**, iPhone device family only.

Images were captured on the dedicated **Kevin-App-Store-41** iPhone 17 Pro Max,
iOS 26.0 simulator using the existing Debug-only, network-free screenshot
fixtures. They show the current native `ContentView` / `CallDetailView` inside
the existing marketing frame, using fictional identities, numbers and calls.
The Kevin business screenshot is scrolled to answering controls and hours.
No customer data or actual phone-acceptance claim is included.

All six final images are **1320 × 2868 RGB PNGs**. Their fully opaque alpha
channels were verified and removed losslessly; visible content was not edited.
This size and encoding follow Apple's
[screenshot specifications](https://developer.apple.com/help/app-store-connect/reference/app-information/screenshot-specifications).
Apple accepted these assets under API display type `APP_IPHONE_67` and returned
the same dimensions and matching source checksums.

Final order and Apple readback, all **COMPLETE**, without processing errors:

| Order | Image | Apple screenshot ID | Source MD5 |
|---|---|---|---|
| 1 | `business-live.png` | `d1c1992d-2441-4d8c-a6a9-f10c4224bdd4` | `8bd21b744b23bca98d382e3cf11d7e61` |
| 2 | `business-recents.png` | `fe43c914-cc80-4be9-ae5a-b624e4f320ad` | `c7f1c8eb392a885ded7434304715f11e` |
| 3 | `business-detail.png` | `080c19b5-f4fe-4206-af22-e7b0e0169d0b` | `6df1c7c4a404a90dd8225ce575db34d8` |
| 4 | `business-kevin.png` | `1be38663-9924-4fb6-a6ee-254fa0f50fa1` | `5b0a4ad453a164622705ad2c4145a9f6` |
| 5 | `personal-live.png` | `0864b759-939c-4505-9b26-d8681abc8475` | `3c92daae6731d5d0902a9c0c86f76996` |
| 6 | `personal-recents.png` | `f7f89ae6-16fa-4ce5-9318-e6de32aff3e5` | `8967deb719c76fa68e100fb6663d6d66` |

Independent visual review inspected all six final images and approved their
readability, current controls, truthful content and ordering. It identified an
outdated spam-sensitivity claim in the alternative `personal-kevin.png`; that
image was removed from Apple's draft and replaced by `personal-recents.png`.
The inherited pre-native iPhone and obsolete iPad sets were removed from the new
draft only, after replacement processing completed. The final localization has
one screenshot set containing exactly the six approved images in the order above.

Local outputs are ignored assets in
`marketing/app-store/screenshots/en-US/build41/`; Apple retains the submitted
assets. Durable repository evidence is this text record. No new distribution
archive or signed binary was created.

## Verification and Actions boundary

The existing package/source test and mutation evidence remains in the delivery
record; no full local test suite was repeated for screenshot or metadata work.
This update uses direct provider readback, screenshot inspection, source-tree
identity and documentation checks. Simulator screenshots do not prove live
telephony behavior.

Repository and personal billing owner are **delimatsuo/heykevin** and
**delimatsuo**. Before the documentation PR, the public repository's unchanged
workflow was checked: seven Ubuntu test shards, Python quality and stable
fail-closed **Test**; no hosted macOS job or deployment on PR/main merge.
The latest twelve runs were completed without retries, and no duplicate release
dispatch was created. September paid Actions usage was **$72.031559191**;
Hey Kevin net usage was **$0**. Expected additional chargeable cost for the
documentation PR is **$0**. Required exact-head CI and configured Codex review
must complete before merging this record.
