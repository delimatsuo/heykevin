# App Store submission — iOS 1.3.2 (42)

**Submitted:** Apple reports **WAITING_FOR_REVIEW** for version **1.3.2 (42)**
at **2026-09-17T18:32:28Z**. All six updated screenshots are processed and
included. Release policy is **AFTER_APPROVAL**. Submission is not Apple approval
or public availability.

## Authorization and scope

The owner said “go ahead and upload” after the recommendation to submit build
42 with screenshots matching its transcript-first interface. This authorized
the App Store submission and gallery refresh. The already uploaded, valid build
42 was reused; no duplicate binary upload or new distribution archive was made.

The [build 42 delivery record](2026-09-17-transcript-first-ios-42.md) binds the
signed package, tests and independent source/package reviews. This submission
changes no application source, backend deployment, provider configuration,
pricing, subscriptions, custom product pages or advertising. The separately
deployed [SMS identity and screening-reason update](2026-09-17-owner-sms-identity-backend.md)
remains the production backend.

The owner's “tested. All working” acceptance is recorded for SMS identification.
The detailed build 42 physical-call/frontend checks and unperformed N1/N3 rows
remain open; submission does not establish those results. VoiceOver remains
deferred by the owner.

## Exact Apple submission

| Identity | Verified value |
|---|---|
| App / bundle / team | `6761427495` / `com.kevin.callscreen` / `3FLG8W6B95` |
| App Store version | `c8a5941a-cf13-4848-b765-2d6d0c3a2e0c`, **1.3.2** |
| Selected build | `a81a22cf-cf59-43c8-889e-3f0774df9bea`, **42** |
| Prerelease version | `ab8b7423-a761-48f9-8298-e4d0010ceaaa`, **1.3.2** |
| Review submission | `f5b9b307-d6d1-4a42-ae6a-8c17ea2bf1fd` |
| Review item | `ZjViOWIzMDctZDZkMS00YTQyLWFlNmEtOGMxN2VhMmJmMWZkfDZ8ODkxNTQ2MTk2` |
| Submitted date | `2026-09-17T18:32:26.528Z` |
| Version and submission state | `WAITING_FOR_REVIEW` |
| Release policy | `AFTER_APPROVAL`, preserved from the existing released version |
| English localization | `cae54072-d8ae-4b9f-83ff-19a2f7da7e35`, `en-US` |
| Screenshot set | `ecea1497-c554-4870-bc87-3ab6b4fa8f38`, `APP_IPHONE_67` |

Fresh preflight at **18:30:23Z** verified the exact app/version/build binding,
`VALID`, `APP_STORE_ELIGIBLE`, unexpired build, and
`usesNonExemptEncryption=false`. The only English text change was What's New;
description, keywords, support/marketing URLs and promotional text were preserved.
Review contact/account attributes matched the previous version exactly, with
`demoAccountRequired=false`.

There was no unfinished review submission before creating this one. The item
creation response omitted relationships, so its version binding was verified
through a fresh items read with `include=appStoreVersion` before submitting.
Exactly one submission and one intended version item were created. Immediately
before submission, the selected build, release policy and complete gallery were
checked again. The submit response and fresh version/submission reads confirmed
`WAITING_FOR_REVIEW`.

English What's New was written and read back exactly:

> Go straight from a call notification to the full live transcript.
>
> Review the screening conversation after you pick up, with clearer call controls and consistent light and dark appearance.
>
> Post-call text summaries now clearly identify Hey Kevin.

At **18:33:08Z**, the public US lookup returned **1.3.1**, with release date
**2026-09-17T08:44:22Z**. This establishes public availability of the previous
build 41 version and supersedes the earlier lookup still reporting 1.3.0.
Version 1.3.2 remains awaiting review. The existing released screenshot gallery
was read back unchanged; the refreshed gallery belongs only to version 1.3.2.

## Screenshot provenance and readback

Capture source was `9a4517d79dea3e418f46cd1743976db08dc1c99b` in the isolated
`codex/build42-app-store` worktree. Its iOS tree,
`7dc72a8660313def1efe8fc217e28664ce2b41fd`, exactly matches packaged source
`043b4c8e9cea09f95def904e98045abe901f94d5`. No Swift or project edits were made.

A successful Debug simulator build used the required `xcodebuild-external`
wrapper, with one explicit `-project ios/Kevin.xcodeproj`. Managed result ID:
`kevin-build42-store-screenshots-20260917T181550Z-71525`. The installed app
identified as `com.kevin.callscreen`, **1.3.2 (42)**; its executable SHA-256 was
`06fde8c0ecca127c3c0731425faf260a4620d55fb265444931416feb6145fa71`.

The dedicated **Kevin-App-Store-42** simulator
(`E34AB566-1554-431B-9305-4CCE0AD9FD8E`) was an iPhone 17 Pro Max running iOS
26.0, in light appearance, English/US locale, with a deterministic 9:41 status
bar. It was shut down after capture and review.

Images use existing Debug-only, network-free fixtures with fictional callers.
The live-call images open the actual full transcript through the warm notification
fixture. The connected-call image shows the retained **Before you joined**
conversation and persistent call controls. Calls and Kevin screens use the
existing marketing frame; Kevin is scrolled to answering controls and business
hours. Existing reason fallbacks were preserved rather than manufactured for
the screenshots. These fixtures demonstrate presentation, not physical telephony.

All six final images are **1320 × 2868 RGB PNGs**, matching Apple's
[screenshot specifications](https://developer.apple.com/help/app-store-connect/reference/app-information/screenshot-specifications).
Fully opaque alpha was removed losslessly; RGB pixels were checked against the
raw captures. Independent review inspected every final image, verified source
provenance and hashes, and approved readability, truthfulness and the order below.

Apple returned **COMPLETE** without processing errors, the same dimensions and
matching source checksums for all six assets:

| Order | Image | Apple screenshot ID | Source MD5 |
|---|---|---|---|
| 1 | `business-live.png` | `66fd3c70-b6a1-4d19-85bd-2ae45f1eaf0a` | `aa24591e97d5368c2906dcdd817443a8` |
| 2 | `connected-with-transcript.png` | `0713c894-dd5e-48c5-a759-0192ab64661c` | `00c44bc249c342ec1bec31031b740422` |
| 3 | `business-recents.png` | `5352810b-86b0-488e-94f4-10fd7eb05c90` | `03d09793b81bde1aa53fe9a0a7259bcc` |
| 4 | `business-kevin.png` | `9c38d1fb-978f-4bcd-975d-6b7a55ae01a1` | `c921c9c28ebe16717a324b74a6a75853` |
| 5 | `personal-live.png` | `f47f5c8b-2904-4651-9e0a-d45561c7fc73` | `7aa12ff2a81531b666a69a1708b9e01e` |
| 6 | `personal-recents.png` | `b452f556-aeba-4d09-b5f7-7e023a7672a9` | `b06a68a38668086bf6c38e24091329d8` |

Source SHA-256 identities:

| Image | SHA-256 |
|---|---|
| `business-live.png` | `754516bee90bc0cddcf89c6446f5d3fca8757aa013518f70375949a6eb70b621` |
| `connected-with-transcript.png` | `5507176869e7671a47b743d3b69acf0d94bdf6d2640a4d4700282a76950121b1` |
| `business-recents.png` | `4bc7a31a8023d77df5fc53b61861b8a6750270de246c91d4f396fe7be5ed4fff` |
| `business-kevin.png` | `857f0125201aa32450c7040dbf1db4acaa965af5804cbd8b1d8fa21b8394a4b3` |
| `personal-live.png` | `4fc947637b86224a977cd0615ce807ce45bd3b2af7f3adc89f96e43d0e663813` |
| `personal-recents.png` | `5ae519efdb898850d335c7fd587074bedf0f99970a58c58554701cd641ae5442` |

Replacements were processed before inherited draft images were removed. The
final localization has one set with exactly these six assets in this order.
The connected transcript replaces the earlier detail-screen slot; the obsolete
personal spam-sensitivity image is excluded. Raw captures and task helpers are
temporary scratch; Apple retains the submitted images and this document retains
durable text evidence. No long-term local binary archive was created.

## Verification and Actions boundary

The existing package/source tests and mutation evidence remain in the delivery
record. No full local suite was repeated for screenshots or release metadata.
Verification uses direct provider readback, independent visual/source review,
exact iOS tree identity and local documentation checks.

Repository and personal billing owner are **delimatsuo/heykevin** and
**delimatsuo**. Preflight confirmed this public repository's unchanged workflow:
seven Ubuntu test shards, Python quality and required fail-closed **Test**;
PR-scoped cancellation, 15-minute worker limits and a five-minute aggregator.
There is no hosted macOS job or deployment on this documentation PR/main merge.
The latest 12 runs were completed without retries; the latest PR's nine successful
jobs ranged from 3 to 98 seconds.

September paid Actions usage at **18:30:51Z** was **$72.39913864**, including
**$0** for Hey Kevin. A simple month-end extrapolation exceeds the portfolio
ceiling, so no additional paid allocation is assumed. This public-repository
Ubuntu run has expected incremental chargeable cost **$0**. Required exact-head
CI and configured Codex review must complete before merging this record.

Master model: gpt-6-astra
Builder model/tier: not used — release operations and evidence documentation
Routing reason: master owns provider submission and Git; independent agents captured and reviewed the actual screenshot artifacts
agy transport=headless: false
Input tokens: unavailable
Output tokens: unavailable
Total tokens: unavailable
Retries: 0 binary uploads; 0 duplicate submissions; 0 hosted reruns
Audit defects found: 1 documentation P3 — historical build 39 milestones still used current-live wording; no source, package or final gallery findings
Audit disposition: stale current-status wording corrected; independently reviewed gallery and exact provider submission verified; physical call acceptance remains open
