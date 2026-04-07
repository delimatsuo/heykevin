# KEVIN — Personal Call Filtering Assistant

**Tagline**: "Your phone rings. Kevin answers. You decide."

---

## Product Overview

**Kevin** is an AI-powered call screening assistant for personal mobile phones. Kevin answers unknown calls, identifies the caller, determines their purpose, and notifies the user in real-time via Telegram with action buttons — letting them decide to pick up, call back, text, or ignore. Kevin protects your time from spam and unwanted calls while ensuring important calls always get through.

## Target User

- Professionals who receive 10-50+ calls/day on their personal cell
- People tired of spam/robocalls but afraid to miss important calls
- Anyone who wants a human-sounding gatekeeper without hiring a human

## Problem Statement

The average person receives 5-10 spam calls per day. Existing solutions (carrier spam filters, Truecaller) either block too aggressively (missing real calls) or too passively (still letting spam through). Voicemail is broken — 85% of callers don't leave a message. There is no middle ground between "answer every call" and "ignore everything unknown."

## Core User Stories

1. **As a user**, I want unknown callers to be answered by Kevin so I don't have to deal with spam but don't miss real calls.
2. **As a user**, I want to see in real-time who is calling and why, on my Telegram, so I can decide what to do.
3. **As a user**, I want to tap "Pick Up" and seamlessly join the call in progress, without the caller knowing they were screened.
4. **As a user**, I want my known contacts (family, friends, clients) to ring through directly without screening.
5. **As a user**, I want Kevin to learn — numbers I always pick up should ring through next time.
6. **As a user**, I want Kevin to be able to answer simple questions on my behalf ("What are your office hours?" "Are you available Thursday?").

---

## Features

### P0 — MVP (Must Have)

#### F1: Call Forwarding & Intake
- User forwards their cell phone to a dedicated Twilio number (carrier-level forwarding)
- All calls land on our system first

#### F2: Number Intelligence & Scoring
- On every incoming call, run parallel lookups:
  - Local contacts database (whitelist, VIPs, known callers)
  - Call history database (have we seen this number before? What happened?)
  - Twilio Lookup API (carrier, line type: mobile/landline/VoIP)
  - Nomorobo (spam/robocall score, via Twilio add-on)
- Compute trust score (0-100) based on all signals
- Route based on score:
  - **90-100 (whitelist/VIP)**: Forward directly to user's phone, no screening
  - **70-89 (likely known)**: Ring user's phone for 10 seconds, then Kevin answers if no pickup
  - **30-69 (unknown)**: Kevin answers immediately
  - **0-29 (likely spam)**: Silent voicemail or block

#### F3: AI Voice Agent (Kevin)
- Answers calls naturally: "Hi, this is Kevin, [User]'s assistant. How can I help you?"
- Identifies the caller: "May I ask who's calling and what this is regarding?"
- Assesses urgency based on caller's response
- Can answer FAQs from a user-configured knowledge base
- Keeps caller engaged while user decides (natural conversation, not hold music)
- All calls placed in Twilio Conference Bridge (enables seamless pick-up)

**Voice Stack (verified March 2026):**
- STT: Deepgram Flux (streaming, integrated turn detection — saves 200-600ms vs separate VAD)
- LLM: Claude Sonnet 4.6 (best tool use: 61.3% MCP-Atlas, prompt injection resistant)
- TTS: Fish Audio S2-Pro (Audio Turing Test score 0.515 — callers can't tell it's AI)
- Fallback TTS: ElevenLabs Turbo v2.5 (native mulaw/8kHz phone audio)
- Platform: Vapi (MVP) → LiveKit Agents (scale)
- Expected per-turn latency: 450-650ms (within natural conversation range)

#### F4: Real-Time Telegram Notifications
- Telegram bot sends notification with:
  - Caller number
  - Identified name (from lookup or caller self-identification)
  - Carrier and line type
  - Spam score
  - Live transcript of the ongoing conversation (updated in real-time)
- Inline action buttons:
  - [Pick Up] — System calls user, adds to conference, Kevin drops off. Seamless.
  - [Call Back] — Kevin tells caller "They'll call you right back", hangs up. System bridges callback.
  - [Text Them] — Auto-sends SMS: "Hi, I saw you called. What can I help with? - [User]"
  - [Voicemail] — Kevin asks caller to leave a message. Records and transcribes.
  - [Ignore] — Kevin politely wraps up: "They're in a meeting, can I take a message?"

**Notification format:**
```
Incoming Call
━━━━━━━━━━━━━━━
From: +1-555-0123
ID: John Smith (Trestle)
Type: Mobile (T-Mobile)
Spam: Low (2%)
Business: None found

Live transcript:
"Hi, I'm calling about the project
proposal we discussed last week..."

[Pick Up] [Call Back]
[Text Them] [Voicemail]
[Ignore]
```

#### F5: Warm Transfer (Pick Up)
- When user taps "Pick Up":
  1. System calls user's phone
  2. User is added to the existing Twilio Conference
  3. Kevin says "Connecting you now" and drops off
  4. Caller and user are talking — caller never knew they were screened
- Target: tap to talking in <8 seconds

#### F6: Contact Management
- Whitelist: numbers that always ring through (family, close contacts)
- Blacklist: numbers that are always blocked
- Trust levels adjust based on user behavior (picked up = increase trust, ignored = decrease)

### P1 — Enhanced

#### F7: Deep Lookup for Unknowns
- CNAM caller name lookup ($0.01/call)
- Trestle Reverse Phone (full owner name, address — $0.07/call)
- Yelp / Google Places business match (free tiers)
- Only triggered for score 30-69 (unknown) callers to control costs

#### F8: Agent Knowledge Base
- User configures FAQ answers Kevin can give: "My office hours are 9-5", "I'm on vacation until Monday"
- Calendar integration: Kevin knows user's schedule and can say "They're in a meeting until 3pm, shall I have them call you back?"

#### F9: Quiet Hours & Escalation
- Set quiet hours (e.g., 10pm-7am) — Kevin answers everything, only escalates emergencies
- Escalation cascade: Telegram → SMS after 2 min → phone ring after 5 min (for urgent calls only)

#### F10: Learning / Adaptive Trust
- Numbers user always picks up → auto-promote to whitelist
- Numbers user always ignores → auto-demote
- Spam patterns detected across all Kevin users (crowdsourced spam intelligence)

### P2 — Future

- WhatsApp notifications (richer than SMS, but costs per message)
- Web dashboard for call history and analytics
- Multiple forwarding numbers (work + personal)
- Voice cloning (Kevin sounds like a specific person)
- Calendar-aware screening (less aggressive during expected call windows)

---

## Technical Architecture

```
User's Cell Phone
  → Unconditional call forwarding to Twilio number
    → Webhook → FastAPI on Cloud Run
      → IMMEDIATE TwiML response: place caller in Conference Bridge
      → Async: parallel lookups (Twilio + Nomorobo + Firestore contacts)
      → Scoring engine → Route decision (via Twilio REST API)
        → If whitelist: bridge caller directly to user's phone
        → If unknown: Vapi AI agent joins Conference Bridge
          → Deepgram Flux (STT) → Claude Sonnet 4.6 (LLM) → Fish Audio S2-Pro (TTS)
          → Telegram bot notification with live transcript + action buttons
            → User action → Conference API (pick up / callback / etc.)
        → If spam: end conference, reject or silent voicemail
      → Fallback: independent Twilio TwiML Bin forwards to user
```

### Infrastructure (GCP + Firebase)

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Compute | Cloud Run (min-instances=2, 1Gi, 900s timeout) | FastAPI backend |
| Database | Firestore (Native mode) | Contacts, call history, knowledge base |
| Active call state | Firebase Realtime Database | Ephemeral call state, live transcript buffer |
| Secrets | Secret Manager (volume mounts) | All API keys, securely injected |
| Async processing | Cloud Tasks | Idempotent post-call processing |
| CI/CD | Cloud Build + Artifact Registry | Automated test → build → deploy |
| Fallback | Twilio TwiML Bin | Independent of our infrastructure |

### Security

- **Twilio webhooks**: Validated via `X-Twilio-Signature`
- **Telegram webhooks**: Validated via `secret_token`
- **Management API**: Bearer token authentication
- **Firestore/RTDB**: Security rules deny all client access; server-side Admin SDK only
- **Secrets**: Injected via volume mounts (not env vars)

## Data Model (Firestore — Single-User MVP)

```
Firestore Collections:
  contacts/{phone_hash}     — SHA-256 of E.164 as doc ID
    phone, name, trust_level (0-100), tags[], is_whitelisted, is_blacklisted,
    times_picked_up, times_ignored, last_call_at, score_breakdown

  calls/{call_sid}           — Twilio Call SID as doc ID
    caller_phone, caller_name, timestamp, duration_seconds,
    outcome (picked_up/ignored/voicemail/blocked/callback/texted),
    trust_score, score_breakdown, route_taken,
    transcript[], lookup_data, voicemail_url, voicemail_transcript

  knowledge_base/{kb_id}
    category, question, answer, keywords[], enabled

Firebase RTDB:
  /active_calls/{call_sid}   — ephemeral, deleted after call ends
    conference_name, conference_sid, vapi_call_id,
    state (PENDING/SCORING/SCREENING/PICKUP_RINGING/CONNECTED/ENDED/...),
    state_updated_at, caller_phone, trust_score,
    transcript_buffer, telegram_message_id, idempotency_keys
```

### Call Lifecycle State Machine

```
PENDING → SCORING → SCREENING → [action taken]:
  → PICKUP_RINGING → CONNECTED → ENDED
  → CALLBACK_INITIATED → ENDED
  → TEXTED → ENDED
  → VOICEMAIL_RECORDING → ENDED
  → IGNORED → ENDED
  → CALLER_HANGUP (from any state)
  → ERROR_FORWARDED (from any state — fallback)
```

All state transitions are atomic (RTDB transactions). Button presses are
idempotent (reject duplicates via callback_query.id / CallSid).

## Latency Analysis

| Step | Component | Latency |
|------|-----------|---------|
| 1 | Caller finishes speaking | 0ms |
| 2 | Deepgram Flux STT + turn detection | 150-200ms |
| 3 | Network to Anthropic | 30-50ms |
| 4 | Claude Sonnet 4.6 TTFT | 180-300ms |
| 5 | First sentence generated (streaming) | 100-200ms |
| 6 | Fish Audio S2-Pro TTS first audio byte | 100-150ms |
| | **Total (pipelined)** | **450-650ms** |

Reference: Normal human conversational pause is 200-500ms. Our system feels natural.

## Cost Per Call

| Call Type | Avg Duration | Cost | % of Calls |
|-----------|-------------|------|------------|
| Spam (blocked/silent VM) | 0s | $0.008 | ~40% |
| Unknown (Kevin screens) | 30s | $0.056 | ~35% |
| Known (ring through) | 0s | $0.005 | ~20% |
| Deep lookup triggered | 30s | $0.126 | ~5% |
| **Blended average** | | **~$0.027** | |

**Monthly cost for 20 calls/day: ~$16 API + ~$40 hosting (Cloud Run min-instances=2) = ~$56/month**

## Monetization

| Tier | Price | Includes |
|------|-------|----------|
| Free | $0 | 50 screened calls/month, basic lookup, Telegram notifications |
| Pro | $9.99/mo | Unlimited screening, deep lookup, knowledge base, quiet hours |
| Premium | $19.99/mo | All Pro + voice cloning, calendar integration, WhatsApp, priority support |

## Success Metrics

- Call-to-notification latency: <5 seconds
- Per-turn voice latency: <650ms
- Spam detection accuracy: >90%
- User NPS: >50
- Free → Pro conversion: >10%
- Daily active usage retention (30-day): >60%

---

## AI Coding Agent Prompt

The following prompt contains everything an AI coding agent needs to build Kevin from scratch.

---

```
You are building "Kevin", a personal AI call screening assistant. Here is your complete technical specification.

## What Kevin Does
Kevin is an AI-powered phone gatekeeper. When someone calls the user's phone, the call is forwarded to Kevin. Kevin looks up the number, decides if it's spam/unknown/known, and either blocks it, answers it with an AI voice agent, or rings it through to the user. When Kevin answers, the user gets a real-time Telegram notification with caller info, live transcript, and action buttons (Pick Up, Call Back, Text Them, Voicemail, Ignore).

## Tech Stack
- **Language**: Python 3.12+
- **Framework**: FastAPI (async)
- **Telephony**: Twilio (Voice, SMS, Lookup API, Conference, Media Streams)
- **Voice Agent Platform**: Vapi (https://docs.vapi.ai) — orchestrates STT/LLM/TTS pipeline
- **STT**: Deepgram Flux (https://developers.deepgram.com/docs/flux) — streaming, integrated turn detection
- **LLM**: Claude Sonnet 4.6 via Anthropic API (https://docs.anthropic.com) — primary intelligence, tool use
- **TTS**: Fish Audio S2-Pro (https://docs.fish.audio) — most natural voice. Fallback: ElevenLabs Turbo v2.5 (https://elevenlabs.io/docs)
- **Notifications**: Telegram Bot API (https://core.telegram.org/bots/api) — python-telegram-bot library
- **Database**: Firestore (Native mode) for persistent data + Firebase Realtime Database for ephemeral call state
- **Hosting**: Google Cloud Run (min-instances=2, 1Gi memory, 900s timeout)
- **Secrets**: Google Secret Manager (injected via volume mounts)
- **Async Processing**: Google Cloud Tasks (idempotent post-call processing)
- **CI/CD**: Cloud Build + Artifact Registry
- **Fallback**: Twilio TwiML Bin (independent of our infrastructure)

## Architecture
All calls are placed into a Twilio Conference Bridge. This is critical — it enables the "Pick Up" feature where the user can join an ongoing call seamlessly.

The flow is:
1. Incoming call → Twilio webhook → FastAPI on Cloud Run
2. IMMEDIATE TwiML response: place caller in Conference Bridge (< 2 seconds)
3. Async: parallel lookups (Twilio Lookup, Nomorobo, Firestore contacts, call history) with 3s timeout
4. Score the caller (0-100 trust score with breakdown logging)
5. Route via Twilio REST API: whitelist → bridge to user | spam → block | unknown → Vapi joins Conference
6. Vapi agent talks to caller using Claude Sonnet 4.6 for intelligence
7. Telegram notification sent to user with caller info + live transcript + action buttons
8. User taps action → webhook → validate against state machine → execute (add user to conference / callback / SMS / etc.)
9. Post-call: Cloud Tasks handles transcript save, trust score update, RTDB cleanup (idempotent)

## Key Implementation Details

### Twilio Conference Bridge
Every AI-handled call MUST be placed in a Conference. This is non-negotiable — it's what enables warm transfer.
- Create a uniquely named conference per call (e.g., `call_{call_sid}`)
- Add the caller as participant 1
- Add the Vapi agent as participant 2
- When user taps "Pick Up": use Conference Participants API to add user's phone as participant 3, then remove the agent

### Vapi Integration
- Create a Vapi assistant with Claude Sonnet 4.6 as the LLM
- Configure Deepgram Flux as STT (set `model: "flux"`)
- Configure Fish Audio S2-Pro as TTS
- Define tools the assistant can call:
  - `notify_user(caller_info, transcript, urgency)` — sends Telegram notification
  - `lookup_caller(phone)` — returns lookup data
  - `check_knowledge_base(question)` — checks FAQ
- System prompt should instruct Kevin to: identify the caller, determine purpose, assess urgency, and keep the conversation natural

### Telegram Bot
- Use inline keyboard buttons for actions
- Update the notification message in real-time as transcript grows (edit_message_text)
- Callback query handler for each action button
- Rate limit: max 30 messages/sec to different chats

### Scoring Engine
```python
def calculate_trust_score(phone: str, lookups: dict) -> int:
    score = 50  # baseline for unknown

    if in_whitelist(phone): return 100
    if in_blacklist(phone): return 0

    # Known contact history
    history = get_call_history(phone)
    if history:
        if history.times_picked_up > 2: score += 30
        if history.times_ignored > 3: score -= 20

    # Spam signals
    if lookups['nomorobo']['spam_score'] > 0.7: score -= 40
    if lookups['twilio']['line_type'] == 'voip': score -= 10
    if lookups['twilio']['line_type'] == 'landline': score += 5

    # Contact in lookup
    if lookups.get('cnam', {}).get('name'): score += 10

    return max(0, min(100, score))
```

### Latency Budget
Target: <650ms per conversation turn (caller speaks → Kevin responds)
- Deepgram Flux STT: ~150-200ms
- Claude Sonnet 4.6 TTFT: ~180-300ms
- Fish Audio S2-Pro TTS: ~100-150ms
- Pipelining: TTS starts on first sentence while LLM streams rest
- Total pipelined: ~450-650ms

## Database
Use Firestore (Native mode) for persistent data and Firebase Realtime Database for ephemeral call state. See the data model section in the PRD above. All access via Admin SDK (server-side only). Security rules deny all client access.

## API Endpoints

### Webhook Endpoints (signature-verified)
- POST /webhooks/twilio/incoming — handles incoming calls (Twilio signature validation)
- POST /webhooks/twilio/status — call status updates
- POST /webhooks/twilio/conference — conference participant join/leave events
- POST /webhooks/twilio/fallback — zero-dependency emergency forward
- POST /webhooks/vapi/events — Vapi assistant events (transcript updates, tool calls)
- POST /webhooks/telegram/callback — Telegram button actions (secret_token validation)

### Management API (Bearer token auth, rate-limited)
- GET/POST/DELETE /api/contacts — contact CRUD
- POST /api/contacts/{phone}/whitelist — add to whitelist
- POST /api/contacts/{phone}/blacklist — add to blacklist
- GET /api/calls — call history
- GET /api/calls/{call_sid} — call detail with transcript
- GET/POST/PUT/DELETE /api/knowledge — knowledge base CRUD
- GET/PUT /api/settings — user settings
- GET /health — returns {"status": "ok"} only

## Secrets (stored in Google Secret Manager, injected via volume mounts)
- TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER
- VAPI_API_KEY
- DEEPGRAM_API_KEY
- ANTHROPIC_API_KEY
- FISH_AUDIO_API_KEY
- ELEVENLABS_API_KEY (fallback TTS)
- TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET
- API_BEARER_TOKEN (for management API auth)
- USER_PHONE (user's real cell number)
- USER_NAME (greeting name for Kevin)
- TELEGRAM_CHAT_ID (where to send notifications)

## Build Order
0. Prototype: Vapi + Twilio Conference Bridge integration (validate bidirectional audio, measure latency)
1. Foundation + Security: FastAPI on Cloud Run, Twilio webhook, signature validation, auth middleware, Firestore/RTDB security rules, structured logging, TwiML Bin fallback
2. Lookup & Scoring: parallel async lookups (3s timeout), trust score engine with breakdown logging, routing logic
3. Telegram bot: notifications with inline buttons, secret_token verification, idempotent callback handler
4. Conference Bridge + Vapi: call state machine, RTDB active call state, Vapi assistant, transcript streaming
5. Warm Transfer: Pick Up flow with race condition handling, participant join/leave events
6. Remaining actions: Call Back, Text Them, Voicemail (Cloud Storage + signed URLs), Ignore
7. Contact management: CRUD API, adaptive trust (capped at +/-5 per call), audit trail
8. Knowledge base: FAQ system, quiet hours, escalation cascade, monitoring dashboards
9. Hardening: circuit breaker, load testing, call recording consent, data retention policy, integration tests

## Critical Constraints
- NEVER let a call drop. If any component fails, fall back to forwarding to the user's phone. Exception: if error rate spikes (induced-failure attack), route to voicemail instead of direct forward.
- The Conference Bridge pattern is non-negotiable — it's what makes "Pick Up" work.
- Deepgram Flux's integrated turn detection is critical — do NOT add a separate VAD layer.
- Return TwiML IMMEDIATELY (< 2 seconds) — place caller in Conference, run scoring async, modify call via REST API. Never block the webhook waiting for lookups.
- Telegram inline keyboard callbacks must be answered within 30 seconds.
- All webhook handlers must be idempotent (use CallSid / callback_query.id as dedup keys).
- All state transitions must be atomic (RTDB transactions with compare-and-swap).
- Validate webhook signatures on EVERY request (Twilio X-Twilio-Signature, Telegram secret_token).
- All /api/* endpoints require Bearer token authentication.
- Firestore and RTDB security rules must deny all client access (Admin SDK only).
- Independent Twilio TwiML Bin as fallback — must work even if Cloud Run is completely down.
```
