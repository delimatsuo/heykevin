# Acquisition recovery — backend live and iOS 1.3.4 (45) submitted

Observed September 30, 2026; timestamps below are UTC. Backend purchase fixes and
acquisition measurement are live. Build 45 is available in internal TestFlight.
The iOS version and revised Personal custom product page are waiting for Apple
review. Advertising remains paused. The owner reported the requested build 45
phone test passed. Paid conversion is not established by this receipt.

## Authorization and source

The owner's “ok, complete the tasks” covered the previously listed backend,
measurement/privacy, iOS delivery and Personal store/ad work. The owner agreed to
run a two-phone test when the build became ready and has now reported success.
No agent-initiated live call, outreach,
new analytics vendor, campaign budget increase or recurring automation occurred.

- Behavior source: `b62eea38d9ab5656fb8d809fb872f05d9c81147d`,
  [PR 279](https://github.com/delimatsuo/heykevin/pull/279).
- Packaged source: `f047121e5bb13af8a03c43195353f44615370828`, tree
  `0512222c26b38dd55859b6bbf0b266326fa9b1bb`.
- [PR 280](https://github.com/delimatsuo/heykevin/pull/280) merged as
  `b8958b1816470cc21d7dcd7bff7ba9554c438fe8`; its tree equals the packaged tree.
- 421 guarded Swift tests passed. Exact-head seven Python shards, quality and
  stable `Test` passed in [run 36744683871](https://github.com/delimatsuo/heykevin/actions/runs/36744683871).
  Fresh independent review was clean after adding the existing physical-address
  collection to the manifest. Codex completed review of `f047121` at
  `16:38:12Z`, with no findings and a positive reaction. Cursor's optional check
  was neutral, not a security pass.

## Backend and privacy

Both environments serve source `b62eea38d9ab5656fb8d809fb872f05d9c81147d`.

| Environment | Current revision and traffic | Retained measurement-off rollback |
| --- | --- | --- |
| Production | `kevin-api-00277-neb`, 100%, no tags | `kevin-api-00275-b82` |
| Staging | `kevin-api-staging-00183-gup`, 100%, `staging` tag | `kevin-api-staging-00181-jul` |

Initial source deployments succeeded in staging run
[36742326579](https://github.com/delimatsuo/heykevin/actions/runs/36742326579)
and production run
[36743269421](https://github.com/delimatsuo/heykevin/actions/runs/36743269421).
The production environment gate was approved for that exact authorized source;
environment protections were preserved. Configuration-only revisions then
enabled `ACQUISITION_MEASUREMENT_ENABLED=true` and
`APPLE_ADS_EXPECTED_ORG_ID=19089740`, staging before production. No-traffic tagged
health checks preceded promotion. Temporary `acquisition-enabled` tags were
removed. Health, traffic, tags, allowlisted flags and rollback source were also
independently verified. Promotional offers and receptionist observation shadow
remain false; production/staging App Store and APNs separation is preserved.

The [public privacy policy](https://heykevin.one/privacy) was published with the
September 30 date through the existing Lovable project, changing only
`src/pages/Privacy.tsx`. Matching App Store privacy labels now declare eleven
linked data types and no tracking. A mixed linked/unlinked User ID
answer was corrected and verified after a fresh page load. The signed manifest
matches those categories. No raw attribution token, customer identity export or
historical attribution backfill was added.

Measurement applies to eligible newly created accounts; it separates account
creation, Personal intent, inbound/forwarding observations, trial/entitlement and
verified positive-price production purchases. TestFlight/Sandbox transactions
and free trial starts are not paid acquisition. Apple financial reports remain
the accounting source. Health/configuration acceptance is not end-to-end
attribution or purchase evidence.

Rollback must use the same-source OFF revisions above. Do not serve earlier
source once acquisition records exist: its profile redaction omits the new map.
Repoint the staging tag when rolling back. Traffic rollback alone does not reset
the latest template's enabled flag; turn that off before a subsequent deployment.

## Signed package and Apple processing

App `6761427495`, bundle `com.kevin.callscreen`, team `3FLG8W6B95`.

- Version/build: **1.3.4 (45)**.
- IPA SHA-256: `9571ff83af9985d7fe46dbfc67bf9d72c80f78a092b8bf680c928012b7e42b7c`.
- Executable/dSYM UUID: `76932771-BD34-3464-A473-0F4A43BBF0F4` (arm64).
- Strict/deep code signature passed; production APNs, `get-task-allow=false`,
  production backend URL and eleven-type privacy manifest verified. Debug
  screenshot activation markers are absent from the Release executable.
- Apple validation succeeded at `16:37:43Z`; one upload succeeded at `16:45:25Z`.
- Build/delivery UUID: `edd99570-f502-4fa3-b720-21d8bf01dcfe`.
- Readback at `17:03:03Z`: `VALID`, internal `IN_BETA_TESTING`; the existing QA
  group `ce552be4-c362-4021-9a90-b77d2e910d15` includes build 45. What to Test notes
  were saved and read back. External beta submission was not requested.

## Store and advertising

The version's English metadata is Personal first and explains carrier setup.
Seven default screenshots are processed: three new Personal screens first,
followed by the four retained Business screens. The existing Personal CPP URL
is retained: page `3dabd4d9-67f6-41fc-aa26-c368732c8bd7`, new version
`4d350edd-29a2-4233-97b9-7491ee3ce25d` (version 3), three Personal screenshots and
the revised forwarding-aware promotional text. Apple locks its old internal
reference name, “Personal — Stop Spam Calls,” after first approval; a rename was
rejected without changing the page. Its new copy avoids universal spam-blocking
claims. The old approved version remains visible until Apple approves version 3.

Native Device Hub captured fictional Debug fixtures at 1320×2868. CLI capture
failed with EPERM; Device Hub succeeded. Its native Desktop output was moved
immediately to task scratch. A temporary documented capture-location preference
was restored. Screenshots were encoded as maximum-quality JPEG to meet Apple's
no-alpha requirement, without generative editing. Apple verified checksums and
dimensions and reported all six new asset resources COMPLETE. Final file hashes:

| File | SHA-256 |
| --- | --- |
| `personal-01-live.jpg` | `fc6ff38e1ce2add8c553c154e3f91bf62abb895e01c6547a3ece2f76ab46113b` |
| `personal-02-recents.jpg` | `72f10e9e7d45114c332ad84a19b7dc897925cebbff804fae9b7e9e4019db4472` |
| `personal-03-detail.jpg` | `207512b32c528a11ab3562a65b11d7c29bf5b08d51eca35c7ca8efa566462cf8` |

Review submission `28384f4e-656b-4b9f-b096-fea77377a8a7` contains exactly the
1.3.4 app version and Personal CPP version 3. Submitted at `17:01:41Z`; both
read back `WAITING_FOR_REVIEW`. The app version uses manual release. Public
1.3.4 availability has not been observed.
Fresh App Store Connect readback at `17:49:19Z` still showed 1.3.4 Waiting for
Review, attached build 45, and manual release selected.

Apple Ads account/org `19089740`, campaign `2144778097`, remains paused.
Its actual settings are September 30 at 00:00 through October 6 at 23:45,
America/New_York, $15/day. The name alone is not the spending control; the
existing end date is configured. No extension or additional allowance occurred.

New `Personal - Exact` group `2151491286` is explicitly paused, Search Match off,
with $0.75 max CPT on exactly `ai call screening`, `call screening assistant`
and `personal phone assistant`. Its new `Personal call screening - Build 45` ad
is also paused and references the existing Personal CPP. The prior
`AI Phone Assistant - Exact` group was also paused and directly read back as
Paused after the change. Latest reporting showed
$0 spend for these groups. Before launch, verify that Apple Ads has refreshed to
the newly approved CPP assets and select the Personal ad as active; the default
ad remains the fallback inside the paused group.

## Owner phone acceptance — September 30, 2026

The owner reported: “i tested build 45. All working”. This answers the requested
two-phone carrier-forwarding, transcript/summary and locked-phone notification
check for **1.3.4 (45)**. Record that bounded check as passed based on the owner's
report. No agent placed or observed the live call. Carrier family was not
provided; no phone numbers or call content were collected.

This report does not establish five separate new-user onboarding sessions,
purchase cancellation/restore outcomes, or a paid production conversion.

## Remaining acceptance

1. Apple must approve the app and revised CPP; release the approved app manually
   only after the required owner-controlled checks. Recheck public availability.
2. Complete the five consenting Personal onboarding checks in the
   [acceptance plan](../superpowers/plans/2026-09-30-acquisition-recovery.md),
   including cancellation/restore and carrier setup. One phone check does not
   satisfy all five cases, and these are usability checks, not conversion proof.
3. After those gates, recheck campaign spend, remaining allowance and end date,
   confirm the fresh Personal creative, and activate only the Personal experiment.
   Do not increase bids to force volume, extend October 6, or enable older groups.

The owner's test report establishes the bounded phone acceptance above. No new
customer acquisition, paid purchase or conversion improvement is claimed.

## Local cleanup and receipt

The task-owned `Kevin-Personal-Store-45` simulator and the temporary release
directory (archive, IPA, screenshots, provider helpers and logs) were removed
after recording the text evidence above. Their absence was verified. The prior
Device Hub selection was restored. Other simulators and the primary checkout
were preserved. This receipt is committed locally; implementation PRs 279 and
280 are merged.
