# Hey Kevin - Product and Architecture

## Product Summary

Hey Kevin is an iPhone call screening app. Users route missed or forwarded calls to a Kevin-owned Twilio number. Kevin answers unknown callers, asks who is calling and why, streams the transcript to the iOS app, and lets the user pick up, decline, text back, or let Kevin take a message.

The current implementation is iOS-first:

- The iOS app is the user control surface.
- Twilio handles numbers, incoming calls, SMS, Media Streams, and Voice SDK conferences.
- The backend is a FastAPI service on Cloud Run.
- Firestore stores durable account, call, contact, job, and subscription data.
- Firebase Realtime Database stores active call state and transcript buffers.
- APNs regular push opens the live screening view; APNs VoIP push rings through CallKit for direct pickup.

Legacy Telegram and Vapi modules remain in the repository for compatibility/history, but they are not the primary product path.

## Product Modes

### Personal

Personal mode screens unknown callers, blocks likely spam, and lets trusted contacts ring through directly.

### Business

Business mode acts as an AI receptionist for contractors and service businesses. Kevin asks intake questions, uses business hours, answers from the knowledge base, captures job details, and can integrate with Jobber or Google Calendar.

## Core User Stories

1. As a user, I want Kevin to answer unknown calls so I do not have to deal with spam or interruptions.
2. As a user, I want known contacts to ring through directly.
3. As a user, I want to see a live transcript while Kevin is screening a call.
4. As a user, I want to tap Pick Up and join the caller without making them call back.
5. As a business user, I want Kevin to collect enough detail to create a useful job card or callback summary.
6. As a business user, I want Kevin to respect business hours and take messages after hours.
7. As a subscriber, I expect billing state to be enforced by the server, not by local app state.

## Current Call Flow

```text
Caller dials the user's Kevin number
  -> Twilio POST /webhooks/twilio/incoming
  -> Backend loads contractor by the To number
  -> Backend checks subscription status
     -> trial/active: continue normal routing
     -> expired with app installed: ring via VoIP push, then voicemail
     -> expired with app deleted: simple voicemail and SMS fallback
  -> Backend computes trust score from contacts and call history
  -> Routing decision
     -> whitelisted contact: CallKit ring-through / conference
     -> likely known: forward/ring-through path
     -> unknown: Twilio Media Stream to Kevin
     -> spam: reject or play SIT disconnect tone
  -> For AI screening:
     -> Twilio opens /media-stream/{call_sid}
     -> WebSocket token is validated against RTDB
     -> Voice pipeline handles STT, LLM, and TTS
     -> Transcript is written to RTDB
     -> Regular push opens the iOS Live tab
  -> User action from iOS:
     -> accept: caller is moved into a Twilio conference and iOS joins via Twilio Voice SDK
     -> decline: Kevin takes a message
     -> text_reply: backend sends SMS to caller
     -> voicemail: backend redirects the call to voicemail TwiML
```

## Routing and Trust

The trust score is computed in `app/services/scoring.py` and routed in `app/services/routing.py`.

| Score | Route | Behavior |
|---|---|---|
| 90-100 | `whitelist_forward` | Known/VIP caller bypasses screening. |
| 70-89 | `ring_then_screen` | Likely known caller gets ring-through behavior. |
| 30-69 | `ai_screening` | Kevin answers immediately. |
| 0-29 | `spam_block` | Call is rejected or given a SIT disconnected message. |

Inputs currently used in the synchronous webhook path:

- Firestore contact record
- Recent call history
- Whitelist/blacklist flags
- Ring-through contacts setting
- Quiet-hours override

Twilio Lookup enrichment can run after the initial routing decision when enabled, so the webhook does not wait on slow external lookups.

## Voice Pipeline

The main path is Twilio Media Streams plus a backend pipeline:

- Twilio sends caller audio over WebSocket to `/media-stream/{call_sid}`.
- Deepgram receives continuous audio and returns final utterances.
- Kevin's prompt is built from contractor settings, mode, business hours, services, and knowledge.
- The default pipeline uses Claude-compatible LLM calls and ElevenLabs TTS.
- A Gemini Live pipeline is available when `voice_engine == "gemini"` and `GEMINI_API_KEY` is configured.
- Transcript updates are throttled into Firebase RTDB for the iOS app.
- Barge-in clears Twilio's outbound audio buffer when the caller interrupts Kevin.

Kevin supports multiple languages by using Deepgram language detection and switching response language when needed.

## iOS App

The iOS app is SwiftUI, iOS 17+, configured by XcodeGen in `ios/project.yml`.

Primary areas:

- `ContentView.swift`: Live, Recents, and Settings tabs.
- `AppState.swift`: onboarding, subscription cache, active call state, settings, and local read state.
- `APIClient.swift`: backend REST calls with per-contractor bearer token auth.
- `CallManager.swift`: CallKit and Twilio Voice SDK conference connection.
- `AppDelegate.swift`: regular APNs and VoIP PushKit handling.
- `SubscriptionManager.swift`: StoreKit 2 products, purchases, transaction listener, restore, and server verification.

Regular pushes show live screening state. VoIP pushes are used when the app must ring like a phone call through CallKit.

## Backend

The backend is a Python 3.12 FastAPI service.

Important modules:

- `app/main.py`: app setup, router registration, startup checks, cleanup tasks.
- `app/webhooks/twilio_incoming.py`: primary incoming-call routing webhook.
- `app/webhooks/media_stream.py`: Twilio audio WebSocket bridge.
- `app/services/voice_pipeline.py`: Deepgram, LLM, TTS, barge-in, transcript, and command handling.
- `app/api/voip.py`: active call, transcript, device registration, VoIP token, and call action endpoints.
- `app/api/contractors.py`: account management and protected field enforcement.
- `app/api/subscription.py`: App Store subscription verification, promo eligibility, and offer signing.
- `app/webhooks/appstore.py`: App Store Server Notifications V2 webhook.
- `app/services/post_call.py`: post-call summary, job card, SMS, and auto-reply handling.

## Data Model

Firestore:

- `contractors`: account profile, Twilio number, owner phone, mode, business settings, subscription fields, API token hash, and device references.
- `contacts`: per-contractor VIP/blocked caller records.
- `calls`: call history, trust score, route, transcript, outcome, caller info, and post-call fields.
- `jobs`: extracted job cards for business mode.
- `knowledge_base`: FAQ and business knowledge entries.
- `auto_reply_timestamps`: rate limiting for automatic SMS replies.

Firebase RTDB:

- `/active_calls/{call_sid}`: ephemeral call state, caller info, transcript buffer, contractor id, conference name, WebSocket token, and action flags.
- `/call_commands/{call_sid}`: commands from app actions to the voice pipeline.

Firestore and RTDB client access should remain denied. Server-side access goes through the Admin SDK.

## Subscription Model

The server is the source of truth.

Contractor subscription fields are protected from client PATCH:

- `subscription_status`
- `subscription_tier`
- `subscription_expires`
- `trial_start`
- `deleted_app_detected_at`

StoreKit product IDs:

| Tier | Product ID |
|---|---|
| Personal | `com.kevin.callscreen.personal.monthly` |
| Business | `com.kevin.callscreen.business.monthly` |
| Business Pro | `com.kevin.callscreen.businesspro.monthly` |

Purchase flow:

1. iOS buys via StoreKit 2.
2. iOS sends the transaction id to `/api/subscription/verify`.
3. Backend verifies ownership with the App Store Server API.
4. Backend updates subscription fields in Firestore.
5. App Store Server Notifications keep the server synchronized after renewals, expiry, refunds, and revocations.

Expired users do not get AI screening. If the app is installed, Kevin rings through with CallKit and then falls back to voicemail. If APNs reports the app deleted, Kevin records voicemail and sends SMS instructions to disable call forwarding.

## Integrations

Current active integrations:

- Twilio Voice, SMS, numbers, Media Streams, Lookup, and Voice SDK
- APNs regular push and VoIP PushKit
- Deepgram
- ElevenLabs
- Gemini Live, when enabled per contractor
- App Store Server API
- Firestore and Firebase RTDB
- Jobber OAuth and Google Calendar OAuth

Legacy integrations:

- Telegram callback and notification modules are retained but not part of the current iOS-first flow.
- Vapi modules are retained for older experiments and should not be used for new call-routing work unless deliberately revived.

## Deployment

Production:

- Branch: `main`
- Cloud Run service: `kevin-api`
- URL: `https://kevin-api-752910912062.us-central1.run.app`
- App Store environment: production
- APNs: production

Staging:

- Branch: `staging`
- Cloud Run service: `kevin-api-staging`
- URL: `https://kevin-api-staging-l63rergg7a-uc.a.run.app`
- App Store environment: sandbox
- APNs: sandbox

CI/CD uses GitHub Actions with GCP Workload Identity Federation. Deployment details live in `DEPLOY-SETUP.md` and `.claude/deploy-config.yaml`.

## Operational Constraints

- Keep Twilio webhook responses fast. Do not block incoming-call TwiML on slow external APIs.
- Validate Twilio webhook signatures.
- Require bearer auth on management APIs.
- Enforce contractor ownership on all contractor-scoped endpoints.
- Keep subscription checks fail-open for active/paying users when Firestore is temporarily slow.
- Keep active call state short-lived and clean it up after call end.
- Never trust client-written subscription fields.
- Do not use AI screening for expired users.
- Treat caller speech and contractor-provided knowledge as untrusted input when building prompts.
