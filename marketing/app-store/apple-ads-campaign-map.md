# Apple Ads campaign map

Keep intent, product page, and bid decisions separate. Start with exact-match campaigns so early conversion data is interpretable; use discovery to find new terms, not to absorb the whole budget.

## Campaigns

| Campaign | Product page | Match | Starting budget share | Example intent |
|---|---|---:|---:|---|
| Business Exact | `contractor-after-hours` | Exact | 40% | ai receptionist, call answering service, virtual receptionist, after hours answering service |
| Trade Exact | `contractor-after-hours` | Exact | 25% | plumber answering service, hvac answering service, electrician answering service, contractor answering service |
| Personal Exact | `personal-call-screening` | Exact | 20% | call screening, unknown caller screening, spam call blocker, ai phone assistant |
| Discovery | Matching CPP by ad group | Broad and Search Match | 10% | New-term harvesting only |
| Brand | Default | Exact | 5% | hey kevin, hey kevin app |

Budget shares are starting hypotheses, not permanent targets. Reallocate only after each segment has enough taps and first-time downloads to avoid reacting to noise.

## Negative-keyword boundaries

- Business campaigns: exclude `free`, `jobs`, `salary`, `software developer`, and unrelated human-receptionist employment intent.
- Personal campaigns: exclude trade terms such as `plumber`, `hvac`, `electrician`, `contractor`, and `answering service for business`.
- Move converting discovery terms into Exact weekly, then add them as Exact negatives in Discovery to prevent overlap.

## Weekly decision table

| Signal | Likely issue | Action |
|---|---|---|
| Low tap-through rate | Keyword/creative mismatch | Tighten keyword theme or route to the correct CPP |
| Healthy tap-through, low download rate | Product-page message or screenshot weakness | Change the first three screenshots or promotional text |
| Healthy downloads, weak completed setup | Onboarding/forwarding friction | Fix activation; do not increase bids |
| Healthy setup, weak first screened call | Trial value not reached | Add a safe test-call path or stronger setup guidance |
| Healthy activation, weak paid conversion | Pricing/value mismatch | Review plans and paywall only after cohort data is stable |

## Measurement contract

At minimum, review by product-page/campaign segment:

1. Impressions
2. Taps and tap-through rate
3. First-time downloads and product-page conversion rate
4. Account creation
5. Forwarding activated
6. First screened call
7. Trial-to-paid conversion by tier

Apple Ads and App Store Connect cover the top of this funnel. Hey Kevin currently has no product analytics layer connecting acquisition to activation. Until that is implemented with an explicit privacy review, use aggregate App Store metrics plus server counts for account creation, forwarding activation, first calls, and paid subscriptions; do not claim user-level campaign attribution.

