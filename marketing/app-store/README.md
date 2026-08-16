# Hey Kevin App Store growth kit

This directory is the versioned source of truth for the App Store-native growth funnel. Apple Ads traffic should not be sent to one generic message. Use the default business-first product page for contractor intent and a Custom Product Page (CPP) for personal call-screening intent.

## What ships where

| Surface | Audience | Asset order | Message |
|---|---|---|---|
| Default product page | Contractors and service businesses | `default-01` through `default-03` | Missed calls become qualified, prioritized opportunities |
| `contractor-after-hours` CPP | Contractor, trades, answering-service searches | `default-01` through `default-03` | Kevin answers, qualifies urgency, and captures job details |
| `personal-call-screening` CPP | Personal, unknown-caller, spam-screening searches | `personal-01` through `personal-03` | Kevin screens unknown callers while contacts ring through |

The first three screenshots matter most because Apple uses them prominently on the product page and installation sheets. Keep their order intact.

## Generate screenshots

From the repository root:

```bash
scripts/capture_app_store_screenshots.sh
```

The script builds the real SwiftUI app in Debug, launches six deterministic and network-free scenarios on a dedicated iPhone 17 Pro Max simulator, and writes 6.9-inch assets to `screenshots/en-US/6.9-inch/`. It creates a simulator named `Kevin App Store Screenshots` when one does not already exist, so it never erases or changes a developer's normal simulator data.

The fixtures use fictional names, phone numbers from the reserved `555-01xx` range, and sample conversations. They are unavailable in Staging and Release builds.

## App Store Connect order of operations

1. Update the default promotional text now; this can be changed without a new binary.
2. Create the two CPPs and upload their three screenshots in the order above.
3. Submit the CPPs for review. Do not submit a new app version solely for CPP creative.
4. After approval, create Apple Ads ad variations and select the matching CPP for each campaign.
5. Apply the proposed title, subtitle, description, and keyword changes with the next app-version metadata submission.
6. Run Product Page Optimization only after the default page has enough traffic for a meaningful result; do not split sparse traffic across several tests.

## Guardrails

- Do not mix personal and contractor keywords in one ad group.
- Do not claim that Kevin guarantees booked jobs or answers every call; the product answers calls routed through the user's Kevin number.
- Do not change trial timing or subscription enforcement as part of an App Store creative update.
- Do not reuse real customer transcripts, names, or phone numbers in screenshots.
- Treat creation, App Review submission, ad launch, and budget changes as separate approvals.
