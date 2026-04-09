# Hey Kevin — Project Guide for AI Agents

## What This Is

**Hey Kevin** is an AI-powered call screening app for iPhone. When someone calls the user's forwarded number, Kevin (the AI) answers, finds out who's calling and why, transcribes the conversation live, and texts the user a summary. The user can watch live and pick up anytime.

Two modes:
- **Personal** — screen unknown callers, block robocalls, contacts ring through directly
- **Business** — full AI receptionist for contractors/trades: smart intake questions, business hours, knowledge base, after-hours mode

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| iOS App | Swift, SwiftUI, iOS 17+, XcodeGen |
| Backend | Python 3.12, FastAPI, Cloud Run (GCP) |
| Database | Firestore (profiles, calls, contacts) + Firebase RTDB (live call state) |
| Voice AI | Gemini Live API via WebSocket (media streaming) |
| Speech-to-Text | Deepgram (`nova-3`, `language=multi` — auto-detects all languages) |
| Text-to-Speech | ElevenLabs (multilingual voice) |
| Telephony | Twilio (numbers, call routing, SMS, Media Streams) |
| Push | APNs VoIP push (CallKit) + regular push |
| Payments | StoreKit 2 (iOS) + App Store Server API (backend verification) |
| CI/CD | GitHub Actions → Cloud Run (WIF auth, no stored credentials) |

---

## Repository Structure

```
Kevin/
├── app/                          # Python FastAPI backend
│   ├── main.py                   # App entry point, router registration, background tasks
│   ├── config.py                 # pydantic-settings (env vars / Secret Manager)
│   ├── api/                      # REST API endpoints
│   │   ├── contractors.py        # Account management (PROTECTED_FIELDS enforced here)
│   │   ├── subscription.py       # /api/subscription/* (verify, promo-eligible, sign-offer)
│   │   ├── calls.py              # Call history
│   │   ├── contacts.py           # VIP contacts
│   │   ├── voip.py               # Twilio Voice SDK tokens
│   │   └── ...
│   ├── db/
│   │   ├── contractors.py        # Firestore CRUD, subscription defaults, PROTECTED_FIELDS
│   │   ├── calls.py              # Call records (90-day retention, 100-call limit)
│   │   └── contacts.py
│   ├── services/
│   │   ├── voice_pipeline.py     # CORE: Gemini Live + Deepgram + ElevenLabs pipeline
│   │   ├── subscription.py       # App Store Server API, promo offer signing, notification handler
│   │   ├── push_notification.py  # APNs VoIP + regular push
│   │   ├── sms.py                # Twilio SMS (async, from_number support)
│   │   └── ...
│   └── webhooks/
│       ├── twilio_incoming.py    # CORE: Call routing logic (subscription check + expired handling)
│       ├── media_stream.py       # WebSocket bridge: Twilio ↔ Gemini Live
│       └── appstore.py           # App Store Server Notifications V2 webhook
├── ios/
│   ├── project.yml               # XcodeGen config — run `cd ios && xcodegen generate` after changes
│   ├── Kevin/
│   │   ├── App/
│   │   │   ├── KevinApp.swift    # App entry, SubscriptionManager start, contact sync
│   │   │   └── AppDelegate.swift # Push/VoIP token registration
│   │   ├── Models/
│   │   │   ├── AppState.swift    # @Published state, Keychain-backed sensitive fields
│   │   │   └── Call.swift        # CallRecord struct
│   │   ├── Views/
│   │   │   ├── ContentView.swift # Tab view + forced paywall when trial expired
│   │   │   ├── CallHistoryView.swift  # Recents tab, Mark All Read
│   │   │   ├── SettingsView.swift     # Settings + subscription section
│   │   │   ├── PaywallView.swift      # Subscription paywall (canDismiss param)
│   │   │   ├── OnboardingView.swift   # New user flow, Verizon forwarding toggle
│   │   │   └── InCallView.swift       # Live call screen (CallKit)
│   │   └── Services/
│   │       ├── APIClient.swift        # All backend API calls
│   │       ├── SubscriptionManager.swift  # StoreKit 2 singleton
│   │       ├── CallManager.swift      # Twilio Voice SDK
│   │       ├── ContactSyncManager.swift   # Contacts sync
│   │       └── KeychainManager.swift  # Secure storage wrapper
├── .claude/
│   └── deploy-config.yaml        # Deployment config read by /deploy-staging skill
├── .github/workflows/
│   ├── deploy.yml                # CI: tests → deploy to Cloud Run on push to main/staging
│   └── rollback.yml              # Manual rollback workflow
└── DEPLOY-SETUP.md               # WIF setup instructions, deployment notes
```

---

## Call Flow (the core product)

```
Caller dials user's Kevin number (Twilio)
  → POST /webhooks/twilio/incoming
  → Load contractor by Twilio number
  → Check subscription status (fail-open: if slow, treat as active)
    → trial/active → normal AI screening flow
    → expired + VoIP push succeeds → ring 20s via CallKit → voicemail if no answer
    → expired + push fails (app deleted) → simple voicemail → SMS to owner
  → Determine route (trust score from contact history)
    → Known contact (whitelisted) → VoIP push to ring directly via CallKit
    → Unknown caller → AI screening
  → AI screening: Twilio Media Stream WebSocket → voice_pipeline.py
    → Deepgram STT (multi-language) → Gemini Live → ElevenLabs TTS → back to caller
  → iOS app receives VoIP push → shows live transcript → user picks up or ignores
```

---

## Subscription System

**Server is the single source of truth.**

### Contractor document fields (Firestore)
| Field | Set by | Description |
|-------|--------|-------------|
| `subscription_status` | Server only | `trial` / `active` / `expired` / `cancelled` |
| `subscription_tier` | Server only | `none` / `personal` / `business` / `businessPro` |
| `subscription_expires` | Server only | Unix timestamp |
| `trial_start` | Server only | Set at `create_contractor()` — 14 days |
| `subscription_uuid` | Server only | UUID used as StoreKit `appAccountToken` |

These fields are in `PROTECTED_FIELDS` — the contractor PATCH endpoint silently drops any client attempt to write them.

### Product IDs (App Store Connect)
| Product | ID | Promo Offer ID |
|---------|-----|----------------|
| Personal ($9.99/mo) | `com.kevin.callscreen.personal.monthly` | `founding_member_75off_personal` |
| Business ($49.99/mo) | `com.kevin.callscreen.business.monthly` | `founding_member_75off_business` |
| Business Pro ($79.99/mo) | `com.kevin.callscreen.businesspro.monthly` | `founding_member_75off` |

Founding member promo: 75% off for 3 months, first 1,000 users. Atomic Firestore transaction enforces the 1,000-user limit.

### iOS trial/paywall behavior
- Trial starts at account creation (server-side, automatic)
- Onboarding shows paywall with "Try Free — then $X/mo" + "Continue without subscribing" link
- When `subscriptionStatus == "expired"`: `ContentView` shows forced `fullScreenCover(PaywallView(canDismiss: false))` — no way out without subscribing
- `SubscriptionManager` calls `POST /api/subscription/verify` after each purchase and on cold launch

### App Store credentials (Cloud Run env vars)
- `APPSTORE_KEY_ID=DZBPCD46KP` — In-App Purchase signing key (for promo offer signatures)
- `APPSTORE_ISSUER_ID=4b083963-2537-4f41-80ac-8976760521aa`
- `APPSTORE_BUNDLE_ID=com.kevin.callscreen`
- `APPSTORE_ENVIRONMENT=sandbox` (change to `production` for App Store launch)

---

## Multi-Contractor Architecture

Each user is a "contractor" — a Firestore document with:
- Their personal phone, Kevin Twilio number, business info, knowledge base
- Their own API token (stored in Keychain on iOS, used for all API auth)
- Per-contractor device tokens (`contractors/{id}/devices/primary`)
- Subscription fields (protected)

No shared state between users. Every call is routed to the right contractor by matching the `To` Twilio number.

---

## Language Support

Kevin speaks and understands **all languages automatically**. Deepgram runs in `language=multi` mode and detects the caller's language from the first words. The Gemini prompt instructs Kevin to switch languages mid-call. No user configuration needed.

---

## Deployment

### Backend
```bash
# Deploy to production
gcloud run deploy kevin-api --source . --project kevin-491315 --region us-central1 --allow-unauthenticated

# Or just push to main — GitHub Actions deploys automatically
git push origin main
```

### iOS
```bash
# After changing project.yml or adding files:
cd ios && xcodegen generate

# Upload to TestFlight (bump CURRENT_PROJECT_VERSION in project.yml first):
# 1. Archive: xcodebuild archive -scheme Kevin -configuration Release ...
# 2. Export: xcodebuild -exportArchive ... -exportOptionsPlist ExportOptions.plist
# 3. Upload: xcrun altool --upload-app --apiKey HLB7866PG8 --apiIssuer 4b083963-...
```

### Branches
- `main` → auto-deploys to `kevin-api` (production)
- `staging` → auto-deploys to `kevin-api-staging`

### Environments
| | Production | Staging |
|--|--|--|
| Cloud Run | `kevin-api` | `kevin-api-staging` |
| URL | `https://kevin-api-752910912062.us-central1.run.app` | pending (created on first staging push) |
| APNs | production | sandbox |
| App Store | production | sandbox |

---

## Key Design Decisions

1. **Fail open on subscription** — if Firestore is slow, treat subscription as active. Never break paying users.
2. **Server is source of truth** — iOS subscription state is a UI cache only (Keychain). Backend always wins.
3. **`appAccountToken` = `subscription_uuid`** — Firestore IDs are not UUIDs; we generate a UUID at contractor creation and store it as `subscription_uuid` for StoreKit ownership verification.
4. **No AI for expired users** — expired trial users get VoIP ring-through (if app installed) or simple voicemail (if deleted). Never run AI screening for free after expiry.
5. **Deleted-app detection** — APNs 410 response = app deleted. Set `deleted_app_detected_at`. After 14 days, release Twilio number + send final SMS.
6. **Forwarding codes** — most carriers use `*61*number#` (no-answer forward). Verizon uses `*71number`. The onboarding and Settings forwarding step has a Verizon toggle.
7. **Call history** — 90-day retention, 100-call limit. Read state stored in UserDefaults keyed by `call_sid` (stable ID).

---

## App Store Connect

- **App ID**: 6761427495
- **Bundle ID**: `com.kevin.callscreen`
- **Subscription Group ID**: 22007035
- **Team ID**: `3FLG8W6B95`
- **GitHub repo**: `https://github.com/delimatsuo/heykevin`
- **GCP project**: `kevin-491315`

---

## Environment Variables (Cloud Run)

Key variables — full list managed via `gcloud run services update`:

| Variable | Purpose |
|----------|---------|
| `TWILIO_ACCOUNT_SID` | Twilio account |
| `TWILIO_AUTH_TOKEN` | Twilio auth |
| `DEEPGRAM_API_KEY` | Speech-to-text |
| `ELEVENLABS_API_KEY` | Text-to-speech |
| `ANTHROPIC_API_KEY` | Claude (call summaries) |
| `GEMINI_API_KEY` | Gemini Live (voice AI) |
| `APNS_KEY_CONTENT` | APNs .p8 key (pipe-separated newlines) |
| `APNS_KEY_ID` | APNs key ID |
| `APNS_TEAM_ID` | Apple team ID |
| `APNS_BUNDLE_ID` | `com.kevin.callscreen` |
| `APNS_SANDBOX` | `false` for production |
| `APPSTORE_KEY_ID` | `DZBPCD46KP` |
| `APPSTORE_ISSUER_ID` | `4b083963-2537-4f41-80ac-8976760521aa` |
| `APPSTORE_PRIVATE_KEY` | In-App Purchase .p8 key (pipe-separated) |
| `APPSTORE_ENVIRONMENT` | `sandbox` or `production` |
| `API_BEARER_TOKEN` | Global admin token |
