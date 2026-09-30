# Acquisition and activation measurement (default off)

Base `ba964d035325b8ba153a37e3ff2dc449130eeb60`, tree
`1b0ab87aa8c9da1ae43a75fbf7731a463ae15f87`. Source implementation only.
No production collection, release, backfill, provider reads or ad activation.

## Builder envelope

Builder `agy gemini-3.7-flash-high`, headless. Master owns architecture, Git and
independent verification. Implement pinned behavior only, inspect source as needed,
but do not run tests/builds, mutate Git, read `.env` files, invoke providers, access
credentials or other worktrees, deploy, create accounts, upload, or spawn agents.
Never use `rm -rf`. Return questions only for contradictions that cannot be resolved
without exceeding this envelope. Do not change this plan.

Allowlisted writes:

- `app/config.py`, `app/main.py`
- `app/api/acquisition.py` (new), `app/services/acquisition.py` (new)
- `app/db/contractors.py`, `app/api/contractors.py`
- `app/webhooks/twilio_incoming.py`
- `app/services/subscription.py`, `app/api/subscription.py`
- `ios/Kevin/Services/AcquisitionCoordinator.swift` (new)
- `ios/Kevin/Services/APIClient.swift`, `ios/Kevin/App/KevinApp.swift`
- `ios/Kevin/Views/OnboardingView.swift` (only add explicit intent to create calls)
- `scripts/summarize_acquisition_funnel.py` (new, offline JSON only)
- `tests/unit/test_acquisition_measurement.py` (new)
- `tests/unit/test_acquisition_api.py` (new)
- `tests/unit/test_acquisition_wiring.py` (new)
- `ios/KevinTests/AcquisitionCoordinatorTests.swift` (new)

## Safety and data contract

Add `ACQUISITION_MEASUREMENT_ENABLED` default false, plus expected Apple Ads org ID
default 0. Collection eligibility requires the flag AND a valid configured org ID.
No feature changes to entitlement, telephony routing, account creation, purge,
notifications, billing, or user-visible onboarding. New measurement calls must
fail without breaking existing flows. No raw token/payload/error logging.

Only newly created eligible accounts get an embedded `acquisition_measurement`
map, schema_version=1, created_at/cohort timestamp and declared_onboarding_intent.
Add map root to PROTECTED_FIELDS and normal API response sensitive keys; no public
client read of campaign IDs. Do not add a collection or purge exception. Existing
accounts stay ineligible; no backfill or mode mutation changes cohort identity.

Add optional strict personal/business `declared_onboarding_intent` to
ContractorCreate (default None) and APIClient.createContractor (default nil).
Business draft explicitly passes business despite runtime mode=personal;
provision(mode:) passes its selected intent; restoreOrContinue omits it. Missing
intent is unknown. Do not infer audience from the bootstrap's default business
request or the server-normalized personal mode. Strip the new request field from
ordinary contractor persistence; place it only inside the protected map.

Keep three distinct facts: declared intent, runtime account mode, and verified
purchased tier. The map's intent never changes. Do not send user IDs, phone numbers,
contacts, transcripts, recordings, IDFA or device fingerprints to Apple.

## API and attribution

New authenticated router `/api/acquisition`, registered in app/main.py.
`GET /status` returns only `{eligible: bool}` based on the bound contractor,
active=True, eligible new cohort, enabled config, and remaining nonterminal attempt
budget. Require contractor token via existing verify_api_token. Admin token cannot
bind a device's attribution. Do not accept contractor ID from the request.

`POST /apple-ads` accepts ONLY `{token: StrictStr}` (1..8192 UTF-8 bytes, extra
fields forbidden). Response `{status: recorded|already_recorded|unattributed|
ineligible|disabled|retryable|exhausted, retry_after_seconds?: integer}` without
identity or ad metadata. Derive contractor ID from request.state; unauthenticated
and admin attempts fail. Durable per-account attempt budget maximum three, lease
before network, terminal writes cannot be overwritten. Re-read active account in
every transactional mutation; never create a missing/deactivated document.

Exchange at hardcoded `https://api-adservices.apple.com/api/v1/`, POST text/plain,
no redirects, bounded connect/read timeout (3s/5s), bounded response body (16KiB).
Store neither the token nor raw response. 404 or retryable transport/5xx yields
retry_after_seconds=5 until max attempts, then exhausted/unknown. Never let errors
mean organic. Reserve attempts transactionally with a 5-second minimum spacing;
duplicate simultaneous requests cannot spend additional unbounded attempts.

Apple response allowlist: strict bool attribution; positive signed-64-bit integer
orgId/campaignId/adGroupId; optional positive keywordId; conversionType enum
Download/Redownload/PreOrder and claimType Click/Impression when present. Reject
bool-as-int, wrong configured org, known development placeholder IDs. For true,
require essential org/campaign/adgroup identifiers. Missing optional fields remain
unknown. Ignore unallowlisted content without logging it. A false payload records
unattributed, never label it proven organic. True stores only allowlisted IDs and
enums. Result is one terminal first-attribution record.

## iOS coordinator

Use a small @MainActor coordinator with injectable async eligibility, token, post,
sleep and auth-context dependencies so XCTest can drive all awaits. Production
adapter first checks backend eligibility using a captured bearer token. Only then
generate AAAttribution.attributionToken off the main thread, in memory. Avoid
blocking app startup/onboarding. Use the existing full CallAuthContext generation
fence. Capture it before awaits, send using its bearer token, recheck before sending
and after every suspension. Cancel/discard on auth rotation/signout and never
acknowledge attribution on behalf of the next account. At most three retries,
five-second spacing. A per-context single-flight guard prevents parallel starts.

Wire to KevinApp auth-context change and normal foreground activation, including
new authenticated users before onboarding is complete. Skip screenshot fixtures
and unit-test startup. No persistent token, receipt, UserDefaults or Keychain
attribution store. Default-off backend status must result in zero token requests.
Do not call live Apple services during tests. APIClient methods take explicit
captured bearer tokens and parse responses strictly; no implicit switched-account
token fallback or automatic POST retries outside the coordinator's bounded budget.

## Activation and payments

Within existing server evidence helpers, record first_inbound_observed_at and
first_forwarded_observed_at once transactionally, before the existing hourly
last-seen throttle. Only the existing authenticated Twilio/ForwardedFrom path may
record these. A received inbound call does not mean completed screening; never
name it completed/answered/real non-test call. Client self-report cannot set these.
Measurement failure must not alter original helper's last-seen updates or TwiML.

Add a pure classifier of already verified Apple transaction payloads, used only
after existing ownership/product/environment validation and successful accepted
subscription updates in BOTH direct verification and server-notification paths.
No receipt-verification changes. Store first_verified_entitlement_observed_at and
tier separately from first_positive_price_purchase_observed_at, signed purchase
time and paid tier. Positive-price conversion requires Production environment,
recognized product, strict integer price >0, valid purchaseDate, not a free trial,
not revoked. Missing price stays unknown, never guessed paid. If explicit verified
Production offerDiscountType=FREE_TRIAL and price=0, record a separately named
first_storekit_trial_observed_at; automatic backend trial_start is not this event.
Do not record transaction IDs, original transaction IDs, appAccountToken, raw JWS,
price or identity in the measurement map. Duplicate deliveries count once. Later
upgrade cannot rewrite intent or original conversion tier; historical observed
purchase is not active paid subscribers, retention, recognized or net revenue.

## Offline reporting

Pure aggregate reducer plus CLI accepting a JSON list of allowlisted measurement
maps only (no complete contractor dumps, IDs or PII). Bound input to 5000 records
and a maximum 90-day UTC cohort [start,end); explicit as_of. Validate and reject
unexpected root fields without echoing values. No Firestore/provider/network use.
Default group by declared intent and source apple_ads/unattributed/unknown plus
campaignId/adGroupId. Optional keyword grouping can include keywordId. Output only
schema/version, cohort dates, as_of, complete flag and counts: accounts_created,
attribution_recorded, inbound_observed, forwarding_confirmed,
verified_entitlement_observed, storekit_trial_observed,
positive_price_purchase_observed, paid Personal/Business/BusinessPro breakdown.
Report no conversion rates if input was truncated/incomplete; preferably reject
over-limit input. Validate date bounds, unknowns and strict types. No ROAS or
causal attribution claims for historical installs. Explain deletion erases the
measurement map and can reduce historical counts. No dashboard/admin route.

## Acceptance criteria and meaningful tests

- "A synthetic attributed new Personal account contributes one account, inbound observation, confirmed forwarding and verified positive-price Personal purchase to its immutable cohort."
- "Retries and duplicate callbacks cannot duplicate counts or rewrite attribution."
- "Free trials, Sandbox, missing/zero/invalid prices, revocations and client assertions cannot increment paid or confirmed-forwarding counts."
- "Missing, failed and Apple-unattributed results remain separate."
- "No raw attribution token, caller content or account identifier enters new storage, logs or report JSON."
- "Disabled collection produces no attribution token or outbound Apple request."

Tests must exercise production helpers/route/coordinator with fakes; no real API,
cloud or StoreKit calls. Cover cross-account auth, admin refusal, strict schema,
wrong-org payload, durable attempt bound and spacing, no redirects/bounded body,
deleted/missing account fencing, concurrency/idempotency, call-throttle placement,
failure-isolation, paid classifier false positives, direct and notification wiring,
purge removal, immutable intent with Personal->Business upgrade, exact synthetic
report counts and boundaries, iOS account rotation at every await, disabled token
generation and concurrent starts. Do not mirror implementation strings as the
primary proof. Master runs tests/mutation probes. Add no generic event framework.

Apple primary references (read before implementing uncertain API details):

- https://developer.apple.com/documentation/AdServices/AAAttribution/attributionToken%28%29
- https://developer.apple.com/documentation/AppStoreServerAPI/price
- https://developer.apple.com/documentation/appstoreservernotifications/offerdiscounttype
- https://developer.apple.com/app-store/app-privacy-details/

Before enabling, owner must review linked advertising/purchase analytics disclosure,
configure actual org ID, deploy backend and approve the corresponding iOS release.
This slice cannot count pre-account abandonment or prove carrier calls on-device.

Output strict JSON: {node:"measurement_builder",base_sha,files_changed,summary,
tests_added,commands_executed,risks,usage:{input_tokens,output_tokens,total_tokens}}.
Use null for unavailable usage. Do not claim tests passed or collection deployed.
