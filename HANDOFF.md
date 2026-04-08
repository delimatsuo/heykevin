# Hey Kevin — Agent Handoff Prompt

Copy everything below the line into a new conversation.

---

You are taking over development of **Hey Kevin**, an AI phone assistant app for iOS. The app screens incoming calls using AI (Claude LLM + Deepgram STT + ElevenLabs TTS), lets the user monitor calls in real-time with a live transcript, and optionally pick up calls through an in-app conference bridge. The goal is App Store submission.

## Architecture

**Backend**: FastAPI on Google Cloud Run (`kevin-api` in project `kevin-491315`, region `us-central1`)
- Twilio handles telephony (incoming calls, media streams, conferencing, SMS)
- Deepgram nova-3 for speech-to-text (WebSocket, mulaw 8kHz)
- ElevenLabs for text-to-speech (multilingual)
- Claude (Anthropic) for conversation intelligence + job card extraction
- Firebase RTDB for real-time call state (`/active_calls/{call_sid}`)
- Firestore for persistent data (contractors, calls, contacts, jobs, settings)
- APNs for push notifications (currently using **sandbox** endpoint — `APNS_SANDBOX=true` env var)

**iOS**: SwiftUI app, Xcode 26, deployment target iOS 17+, uses XcodeGen (`project.yml`)
- TwilioVoice SDK v6 for in-app call pickup via conference bridge
- CallKit for system call integration
- PushKit for VoIP push notifications
- Sign in with Apple for authentication
- Bundle ID: `com.kevin.callscreen`, Team: `3FLG8W6B95`

**Key files**:
- Backend entry: `app/main.py`
- Call routing: `app/webhooks/twilio_incoming.py`
- Voice pipeline (STT→LLM→TTS): `app/services/voice_pipeline.py`
- Media stream WebSocket: `app/webhooks/media_stream.py`
- Call pickup/conference: `app/api/voip.py` (`_handle_accept`)
- iOS call manager: `ios/Kevin/Services/CallManager.swift`
- iOS API client: `ios/Kevin/Services/APIClient.swift`
- iOS main views: `ios/Kevin/Views/ContentView.swift`, `OnboardingView.swift`, `SettingsView.swift`
- iOS app state: `ios/Kevin/Models/AppState.swift`
- Project config: `ios/project.yml`

## Current State — What Works

1. ✅ Incoming calls forwarded from carrier → Twilio → Kevin screens the call
2. ✅ Real-time transcript visible in the app (polling every 2s)
3. ✅ Kevin speaks any language the caller uses (auto-detection via Deepgram, Claude responds in that language)
4. ✅ Post-call SMS to contractor in their system language, auto-reply to caller in caller's language
5. ✅ Silence timeout (2 min) and max call duration (90 min) during Kevin screening
6. ✅ Conference bridge max duration (90 min, Twilio `timeLimit`)
7. ✅ Orphan call cleanup (every 5 min, removes RTDB entries >2h old)
8. ✅ WebSocket auth (ws_token per call, validated against RTDB)
9. ✅ Tenant isolation on all API endpoints
10. ✅ Security audit completed (3 rounds, all CRITICALs and HIGHs resolved)
11. ✅ Portrait-only orientation
12. ✅ Timezone auto-detected from device, stored per contractor

## Active Issues — What Needs Fixing

### CRITICAL: In-app call pickup has no audio

When the user taps "Pick Up" in the app:
- Backend successfully redirects caller from `<Stream>` to `<Conference>` ✅
- Backend writes `accepted: true` to RTDB to prevent premature hangup/post-call processing ✅
- iOS Twilio Voice SDK connects to the conference via `/webhooks/twilio/ios-voice` ✅
- Twilio reports the call as "connected" ✅
- **But no audio flows** — both sides hear silence ❌

The current implementation uses CallKit's `CXStartCallAction` and connects the Twilio SDK inside the `perform action:` handler. The theory is that CallKit must activate the audio session before the SDK can use it. Previous attempts that failed:
- Connecting before CallKit → no audio
- Manually configuring AVAudioSession → conflicts with SDK's DefaultAudioDevice
- Connecting inside CallKit completion handler → no audio
- Removing CallKit entirely → no audio

The Twilio Voice SDK v6 uses a `DefaultAudioDevice` that manages AVAudioSession internally. The issue may be related to how `CXProvider.didActivate(audioSession:)` interacts with the SDK's audio device.

**Investigate**: Check Twilio's official iOS quickstart sample for the correct CallKit + TwilioVoice SDK v6 integration pattern. The sample app is at https://github.com/twilio/voice-quickstart-ios. Compare their `ViewController.swift` and audio handling with our `CallManager.swift`.

### HIGH: Push notifications — DeviceTokenNotForTopic

APNs returns `DeviceTokenNotForTopic` for VoIP push tokens. This means the APNs topic (bundle ID + `.voip` suffix) doesn't match the push certificate or the token type. The regular push token works (when device registration succeeds), but VoIP pushes fail.

Check:
- Is the VoIP push certificate configured in Apple Developer Portal for `com.kevin.callscreen`?
- Is the `apns-topic` header set correctly to `com.kevin.callscreen.voip` for VoIP pushes?
- Read `app/services/push_notification.py` — check the `_send_push` function's topic header

### HIGH: Cold-start networking timeout on iOS

When the app launches fresh from Xcode, ALL network requests time out for 60-90 seconds (including google.com). After backgrounding and foregrounding, everything works in <200ms. This appears to be an Xcode debugger issue — verify by testing without the debugger (install via Xcode, then launch from home screen).

The error is `_kCFStreamErrorCodeKey=-2102, _kCFStreamErrorDomainKey=4` which is `kCFStreamErrorDomainHTTP` with an internal CFNetwork error code. A 3-second delay was added on first launch but isn't sufficient.

### MEDIUM: Device token registration often fails

Because of the cold-start timeout, `register-device` frequently fails. The backend then has stale push tokens. A retry mechanism exists (retries after 3s on first launch) but doesn't always succeed. Consider registering from `applicationDidBecomeActive` with exponential backoff.

## Deployment

**Backend deploy**:
```bash
gcloud config set account deli@ellaexecutivesearch.com
cd "/Volumes/Extreme Pro/myprojects/Kevin"
gcloud run deploy kevin-api --source . --project kevin-491315 --region us-central1 --allow-unauthenticated
```

**iOS build**:
```bash
cd "/Volumes/Extreme Pro/myprojects/Kevin/ios"
xcodegen generate
xcodebuild -project Kevin.xcodeproj -scheme Kevin -destination 'generic/platform=iOS' -configuration Debug build
```

**For App Store submission**:
1. Change `APNS_SANDBOX=false` in Cloud Run env vars
2. Change `aps-environment` to `production` in `project.yml`
3. Archive with Release configuration
4. Version 1.2.0, Build 1

## Environment Notes

- gcloud auth expires frequently — re-auth with `gcloud auth login --account=deli@ellaexecutivesearch.com`
- The user (Deli) is in Eastern Time
- The app is set to "personal" mode (not business) — no business hours, no "we're closed" messages
- The user's contractor ID is `COgOeaSL4lbmuSvD7sOu`
- The Kevin Twilio number is `+16504222677`
- Always build and verify before asking the user to test
- The user expects proactive behavior — run commands, fix errors, don't ask permission for routine operations
- Lock orientation to portrait only
- Do not add features beyond what's requested
- When debugging, check server logs: `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="kevin-api"' --project kevin-491315 --limit=30 --format='value(timestamp, textPayload, jsonPayload.message)' --freshness=15m`

## Security Notes

- `aps-environment: development` in project.yml for debug builds
- `APNS_SANDBOX=true` in Cloud Run for dev-signed iOS apps
- Onboarding endpoints (`/api/contractors/lookup-by-apple-id`, `POST /api/contractors`) are exempt from bearer token auth — they're the login/signup flow
- The lookup endpoint issues a fresh API token (acts as login)
- `vapi_webhook_secret` is not set in production — the startup logs a CRITICAL warning but doesn't block
- All webhook endpoints require signature verification (Twilio, Telegram)
- WebSocket media stream requires `ws_token` validated against RTDB
