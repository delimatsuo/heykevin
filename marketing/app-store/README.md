# Hey Kevin App Store growth kit

This directory holds proposed App Store copy and campaign routing. The September
30 recovery plan leads the next experiment with Personal use. Trial interest is
an audience hypothesis; it does not establish paid retention or actual usage.
These files are drafts, not evidence that App Store Connect has been updated.

## What ships where

| Surface | Audience | Asset order | Message |
|---|---|---|---|
| Proposed default product page | Personal call screening first, with Business described separately | `personal-01` through `personal-03` | Understand unknown callers and review their messages |
| `contractor-after-hours` CPP | Contractor, trades, answering-service searches | `default-01` through `default-03` | Kevin answers, qualifies urgency, and captures job details |
| `personal-call-screening` CPP | Personal, unknown-caller, spam-screening searches | `personal-01` through `personal-03` | Kevin screens unknown callers while contacts ring through |

The first three screenshots matter most because Apple uses them prominently on the product page and installation sheets. Keep their order intact.

## Generate screenshots

Capture the actual SwiftUI app's `personal-live`, `personal-recents` and
`personal-detail` Debug fixtures after the purchase fixes pass review. Use the
required Xcode wrapper and a compatible simulator/runtime under the repository's
current storage policy. The older `scripts/capture_app_store_screenshots.sh`
invokes raw Xcode and custom DerivedData paths; do not run it until it is migrated
to that policy. No new screenshot assets are claimed by this copy change.

The fixtures use fictional names, phone numbers from the reserved `555-01xx` range, and sample conversations. They are unavailable in Staging and Release builds.

## App Store Connect order of operations

1. Complete source qualification and owner-run activation checks in the
   [recovery plan](../../docs/superpowers/plans/2026-09-30-acquisition-recovery.md).
2. Review Personal copy and fresh screenshots against the build to be released.
3. Obtain the existing App Store publication authorization, then update the
   Personal CPP and proposed default metadata. Record actual review/live state.
4. Confirm privacy disclosures and deployment before enabling acquisition measurement.
5. After release and activation acceptance, activate one bounded Personal Exact
   experiment with its approved CPP and an enforced total allowance.
6. Review mature trial-to-paid cohorts before expanding the audience or budget.

## Guardrails

- Do not mix personal and contractor keywords in one ad group.
- Do not claim that Kevin guarantees booked jobs or answers every call; the product answers calls routed through the user's Kevin number.
- Do not change trial timing or subscription enforcement as part of an App Store creative update.
- Do not reuse real customer transcripts, names, or phone numbers in screenshots.
- Existing explicit authorization persists for its stated scope. Code and copy
  preparation do not imply authorization to publish a build or resume paid ads.
