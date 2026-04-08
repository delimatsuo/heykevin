# Subscription System — Coding Handoff

## What to Build

Add a complete subscription system (StoreKit 2 + backend) with 14-day trial, 3 pricing tiers, promotional offers for the first 1,000 users, and expired/deleted user handling so calls always reach the user.

## Files to Read First

1. **Approved Plan** (follow this step by step): `/Users/delimatsuo/.claude/plans/indexed-sniffing-moon.md`
2. **Review Report** (amendments already applied to plan): `docs/superpowers/plans/2026-04-07-internationalization-REVIEW.md` — ignore this, it's for the internationalization feature, not subscriptions.
3. **Pricing Strategy** (context): `docs/pricing-competitive-strategy.md`

## Current Codebase State

### Backend (Python/FastAPI on Cloud Run)
- `app/config.py` — settings via pydantic-settings, env vars. Add App Store Server API config here.
- `app/db/contractors.py` — contractor CRUD in Firestore. Has `SUPPORTED_COUNTRIES`, `COUNTRY_NAMES`, `create_contractor()` with `setdefault()` pattern. Add subscription field defaults here.
- `app/api/contractors.py` — contractor API. Has `ContractorUpdate` model and `_SENSITIVE_KEYS`. Add `PROTECTED_FIELDS` here to prevent client-side subscription manipulation.
- `app/webhooks/twilio_incoming.py` — call routing. `handle_incoming_call()` is the main entry point. Loads contractor at line ~127. `_ring_contractor()` at line ~291 sends VoIP push + polls for answer. This is where subscription check and expired-user routing goes.
- `app/services/push_notification.py` — `send_voip_push()` sends APNs VoIP pushes. Already handles 410 responses (deletes expired tokens). Need to expose success/failure return value.
- `app/main.py` — app startup, router registration. Has `_orphan_call_cleanup` background task pattern to reuse for 14-day cleanup.
- `app/middleware/auth.py` — `verify_api_token` dependency for API auth. New endpoints need this.

### iOS (SwiftUI, Xcode 26, iOS 17+)
- `ios/Kevin/Models/AppState.swift` — app state with `@Published` properties. Uses Keychain for sensitive values (line ~33). Add subscription state here.
- `ios/Kevin/App/KevinApp.swift` — app entry point. Start SubscriptionManager singleton here.
- `ios/Kevin/Views/SettingsView.swift` — settings UI. Add subscription status display and manage subscription link.
- `ios/Kevin/Views/OnboardingView.swift` — onboarding flow. Trigger trial start here, show paywall after onboarding.
- `ios/Kevin/Services/APIClient.swift` — API client. Add subscription verification, promo eligibility, and offer signing endpoints.
- `ios/Kevin/Services/KeychainManager.swift` — Keychain wrapper. Use for subscription state persistence.
- `ios/project.yml` — XcodeGen config. Run `cd ios && xcodegen generate` after adding new files.

## Implementation Order

### Phase 1: Backend Subscription Infrastructure
1. Add subscription field defaults to `create_contractor()` in `app/db/contractors.py`: `subscription_status="trial"`, `subscription_tier="none"`, `trial_start=time.time()`, `subscription_expires=trial_start + 14*86400`, `deleted_app_detected_at=None`
2. Add `PROTECTED_FIELDS` set to `app/db/contractors.py` and enforce in `app/api/contractors.py` update endpoint
3. Add App Store config to `app/config.py`: `appstore_key_id`, `appstore_issuer_id`, `appstore_private_key` (loaded from env/Secret Manager), `appstore_bundle_id`
4. Create `app/services/subscription.py` — Apple App Store Server API verification, promotional offer signing (ECDSA P-256), transaction ID deduplication
5. Create `app/api/subscription.py` — endpoints: `POST /api/subscription/verify`, `GET /api/subscription/promo-eligible`, `POST /api/subscription/sign-offer`. Rate limited: 5 req/min verify, 3 req/min promo.
6. Create `app/webhooks/appstore.py` — App Store Server Notifications V2 webhook. Verify signed JWT from Apple. Handle: `DID_RENEW`, `EXPIRED`, `DID_FAIL_TO_RENEW`, `REFUND`, `REVOKE`.
7. Register routers in `app/main.py`

### Phase 2: Backend Call Routing for Expired Users
8. Modify `app/services/push_notification.py` — `send_voip_push()` returns success/failure boolean
9. Modify `app/webhooks/twilio_incoming.py` — add subscription check after contractor load:
   - Active/trial → normal flow
   - Expired → attempt VoIP push → if succeeds, ring 20s then voicemail → if fails (410), simple voicemail + SMS
10. Add voicemail TwiML function (record + transcribe via Twilio)
11. Add SMS fallback for deleted-app users (voicemail transcription + forwarding disable instructions)
12. Add 14-day cleanup background task in `app/main.py`

### Phase 3: iOS Subscription Manager
13. Create `ios/Kevin/Services/SubscriptionManager.swift` — StoreKit 2 singleton:
    - Product IDs: `com.kevin.callscreen.personal.monthly`, `com.kevin.callscreen.business.monthly`, `com.kevin.callscreen.businesspro.monthly`
    - `Transaction.updates` listener (started at app launch)
    - `Product.products(for:)` fetching
    - `product.purchase()` with promotional offer support
    - Calls `POST /api/subscription/verify` after purchase and on each launch
14. Add subscription state to `AppState.swift` (Keychain-backed, not UserDefaults)
15. Start SubscriptionManager in `KevinApp.swift`

### Phase 4: iOS Paywall & Integration
16. Create `ios/Kevin/Views/PaywallView.swift`:
    - Shows 3 tier cards with features and pricing
    - Strikethrough pricing for promo-eligible users
    - "Start Free Trial" / "Subscribe" button
    - "Cancel Forwarding" button → opens phone dialer with carrier disable code
    - "Restore Purchases" button
17. Show paywall: after onboarding, when trial expires, from settings
18. Update `SettingsView.swift` — subscription status section, "Manage Subscription" link
19. Update `OnboardingView.swift` — start trial on account creation
20. Update `APIClient.swift` — add subscription endpoints

### Phase 5: Deploy & Test
21. Deploy backend to Cloud Run
22. Configure App Store Connect: create subscription group, 3 products, promotional offers
23. Test full flow: trial → promo offer → subscribe → verify → expire → ring-through → voicemail → SMS

## Key Design Decisions

- **Server is the single source of truth** for subscription status. iOS state is UI cache only.
- **Fail open** — if Firestore is slow, treat subscription as active (don't break paying users).
- **Deleted-app users get voicemail + SMS** (not full AI screening — avoids cost spiral).
- **Expired-but-installed users ring through via CallKit** with voicemail fallback after 20s.
- **Promo counter uses atomic Firestore transaction** (check + increment in one operation).
- **App Store Server Notifications V2** is the primary sync mechanism for subscription lifecycle.
- **Protected fields** prevent client-side subscription manipulation.

## Environment

- Backend: Python 3.12 on Cloud Run, Firestore, `gcloud run deploy kevin-api --source . --project kevin-491315 --region us-central1 --allow-unauthenticated`
- iOS: Swift, SwiftUI, Xcode 26, iOS 17+, XcodeGen (`cd ios && xcodegen generate`)
- Auth: `gcloud auth login --account=deli@ellaexecutivesearch.com` for deploy
- Working directory: `/Volumes/Extreme Pro/myprojects/Kevin`

## Execution Method

Use `superpowers:subagent-driven-development` to implement the plan task-by-task. Commit after each phase. Deploy backend after Phase 2. Build iOS after Phase 4.
