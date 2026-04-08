# Hey Kevin — Pre-Launch Fix Plan

## Context

Full security audit (4 agents) + full code review (2 staff engineers) completed. All security fixes from the audit have been deployed. This plan covers the remaining **engineering quality issues** that must be fixed before App Store submission.

The app: AI phone assistant for contractors. FastAPI backend on Cloud Run, iOS SwiftUI frontend, Twilio telephony, Firestore/Firebase RTDB, Deepgram STT, ElevenLabs TTS, Claude LLM.

Version: 1.2.0, Build 1. Domain: heykevin.one.

---

## Execution Strategy

Group fixes by file to avoid merge conflicts. Launch parallel agents per group.

- **Group A**: voice_pipeline.py (backend core — most critical)
- **Group B**: media_stream.py + push_notification.py (backend WebSocket + push)
- **Group C**: twilio_incoming.py + post_call.py + config.py (backend webhooks + config)
- **Group D**: ContentView.swift + AppState.swift + KevinApp.swift (iOS core state + UI)
- **Group E**: SettingsView.swift + OnboardingView.swift (iOS settings + onboarding)
- **Group F**: APIClient.swift + CallHistoryView.swift + AppDelegate.swift + ContactSyncManager.swift (iOS networking + utilities)

After all groups complete: deploy backend, regenerate Xcode project (run `xcodegen generate` then `sed -i '' 's|<string>development</string>|<string>production</string>|' Kevin/Kevin.entitlements`), rebuild.

---

## Group A: voice_pipeline.py

File: `/Volumes/Extreme Pro/myprojects/Kevin/app/services/voice_pipeline.py`

### A1 — CRITICAL: Add timeout to Deepgram receive loop
- **Location**: `_deepgram_receive_loop()`, the `async for message in self._deepgram_ws:` loop
- **Problem**: No timeout. If Deepgram stops sending, loop hangs forever, call freezes.
- **Fix**: Wrap each iteration in `asyncio.wait_for(timeout=30)`. On timeout, log warning and attempt reconnection or end call gracefully.

### A2 — CRITICAL: Fix race condition in unavailability message
- **Location**: `_unavailable_now()` and `_unavailable_timer()` — both check `_unavailable_said` outside the lock before acquiring it.
- **Problem**: Between check and lock acquisition, another coroutine can set it → caller hears the message twice.
- **Fix**: Check `_unavailable_said` again INSIDE the lock, immediately after acquiring it. Both functions already acquire `_response_lock` — just add the re-check after `async with self._response_lock:`.

### A3 — HIGH: Fix language switch clearing conversation
- **Location**: `_switch_to_spanish()` — calls `self._conversation.clear()`
- **Problem**: Loses all context (who called, why, etc.)
- **Fix**: Instead of clearing, append a system-level message: keep the conversation but add instruction "The caller speaks Spanish. Respond only in Spanish from now on." Remove the `.clear()` call.

### A4 — HIGH: Add retry for Claude API failures
- **Location**: `_handle_caller_speech()` — the Claude API call inside the for loop
- **Problem**: If Claude returns 500 or times out, loop breaks silently. Caller hears silence.
- **Fix**: Add 1 retry with 2-second delay. If both fail, speak fallback: "I'm sorry, I'm having trouble. Could you repeat that?"

### A5 — HIGH: Cap utterance buffer
- **Location**: `_deepgram_receive_loop()` — `self._utterance_buffer.append(transcript)`
- **Problem**: Long monologue = unbounded buffer = memory spike + bloated Claude prompt.
- **Fix**: If `len(self._utterance_buffer) >= 15`, flush immediately (call `_flush_utterance()`).

### A6 — MEDIUM: Cap conversation history
- **Location**: `_handle_caller_speech()` — `self._conversation[-20:]`
- **Problem**: `self._conversation` list grows unbounded; only last 20 sent to Claude but memory still used.
- **Fix**: After each Claude response, trim `self._conversation` to last 30 entries max.

### A7 — MEDIUM: Reduce tool execution timeouts
- **Location**: `_execute_tool()` — 5s for Jobber, 8s for Google Calendar
- **Fix**: Reduce both to 3s. If timeout, return error JSON immediately.

---

## Group B: media_stream.py + push_notification.py

### B1 — CRITICAL: Add error callbacks to fire-and-forget tasks
- **File**: `/Volumes/Extreme Pro/myprojects/Kevin/app/webhooks/media_stream.py`
- **Location**: All `asyncio.create_task()` calls (approximately 6 locations: on_transcript RTDB update, push notification, urgency detection, post-call extract, transcript save)
- **Problem**: Exceptions in tasks are silently swallowed.
- **Fix**: Add a helper function at top of file:
```python
def _log_task_exception(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"Background task failed: {exc}", exc_info=exc)
```
Then for each `asyncio.create_task(...)`, add: `task.add_done_callback(_log_task_exception)`.

### B2 — CRITICAL: Fix WebSocket cleanup on exception
- **File**: media_stream.py
- **Location**: `media_stream_ws()` try/finally block
- **Problem**: If exception during pipeline start (before main loop), WebSocket stays open.
- **Fix**: Move `await websocket.close()` to the top of the finally block, before `pipeline.stop()`.

### B3 — CRITICAL: Handle APNs 410 (expired token)
- **File**: `/Volumes/Extreme Pro/myprojects/Kevin/app/services/push_notification.py`
- **Location**: `send_voip_push()`, `send_regular_push()`, `send_urgent_push()` — after checking `response.status_code`
- **Problem**: When APNs returns 410, the device token is expired but not deleted from Firestore.
- **Fix**: After detecting 410, delete the token from Firestore:
```python
if response.status_code == 410:
    logger.warning(f"APNs token expired for {device_token[:8]}...")
    # Delete expired token
    try:
        db = get_firestore_client()
        # Try contractor-scoped path first, then global
        # Query by token value and delete
    except Exception:
        pass
    return False
```

### B4 — HIGH: Add retry to APNs push
- **File**: push_notification.py
- **Location**: All three send functions
- **Problem**: Single attempt with 10s timeout. If APNs is slow, push fails permanently.
- **Fix**: Add 1 retry with 2s delay for non-410 failures.

### B5 — HIGH: Cache APNs JWT token
- **File**: push_notification.py
- **Location**: `_generate_apns_token()` — called on every push
- **Problem**: Creates new JWT every time (unnecessary CPU).
- **Fix**: Cache with 50-minute TTL (tokens valid 1 hour):
```python
_cached_apns_token = None
_cached_apns_token_expiry = 0

def _generate_apns_token() -> str:
    global _cached_apns_token, _cached_apns_token_expiry
    now = time.time()
    if _cached_apns_token and now < _cached_apns_token_expiry:
        return _cached_apns_token
    # ... generate new token ...
    _cached_apns_token = token
    _cached_apns_token_expiry = now + 3000  # 50 minutes
    return token
```

### B6 — HIGH: Limit urgency pushes to 1 per call
- **File**: media_stream.py
- **Location**: `on_urgency_detected()` — currently caps at 3
- **Fix**: Change `if _urgency_push_count >= 3` to `if _urgency_push_count >= 1`.

---

## Group C: twilio_incoming.py + post_call.py + config.py

### C1 — HIGH: Wrap synchronous Twilio calls in executor
- **File**: `/Volumes/Extreme Pro/myprojects/Kevin/app/webhooks/twilio_incoming.py`
- **Location**: `_ring_contractor()` — `client.conferences.list()` and `client.calls().update()` are synchronous Twilio SDK calls inside async context
- **Fix**: Wrap each in `await asyncio.get_event_loop().run_in_executor(None, lambda: ...)`.

### C2 — HIGH: Add retry to job card extraction
- **File**: `/Volumes/Extreme Pro/myprojects/Kevin/app/services/post_call.py`
- **Location**: `_process_personal()` line ~80 and `_process_business()` line ~111 — `extract_job_card()` called once
- **Fix**: Wrap in try/except with 1 retry:
```python
for attempt in range(2):
    try:
        job_data = await extract_job_card(transcript_text, caller_phone)
        break
    except Exception as e:
        if attempt == 0:
            logger.warning(f"Job card extraction failed, retrying: {e}")
            await asyncio.sleep(1)
        else:
            logger.error(f"Job card extraction failed permanently: {e}")
            job_data = {"caller_phone": caller_phone, "call_type": "unknown"}
```

### C3 — HIGH: Wrap SMS sends in try/except
- **File**: post_call.py
- **Location**: All `send_sms()` and `send_mms()` calls (lines ~94, ~121, ~128, ~140, ~142)
- **Problem**: Synchronous calls with no error handling. If Twilio is down, exception propagates and kills post-call processing.
- **Fix**: Wrap each in try/except with logging.

### C4 — HIGH: Validate required config at startup
- **File**: `/Volumes/Extreme Pro/myprojects/Kevin/app/config.py`
- **Location**: After `Settings` class definition
- **Fix**: Add validation in the startup event (or in Settings.__init__):
```python
@app.on_event("startup")
async def startup():
    required = ['twilio_account_sid', 'twilio_auth_token', 'anthropic_api_key',
                'deepgram_api_key', 'elevenlabs_api_key', 'api_bearer_token']
    for key in required:
        if not getattr(settings, key, None):
            logger.error(f"FATAL: Missing required config: {key}")
            # Don't exit — Cloud Run needs the health check to respond
```
Actually, add this to `app/main.py` in the `startup()` function, not config.py.

### C5 — MEDIUM: Move auto-reply rate limit to Firestore
- **File**: post_call.py
- **Location**: `_auto_reply_timestamps` dict (line ~19)
- **Problem**: In-memory dict resets on deploy.
- **Fix**: Store last auto-reply timestamp per phone in Firestore `auto_reply_timestamps` collection. Check before sending.

### C6 — MEDIUM: Add idempotency check for job creation
- **File**: post_call.py
- **Location**: `_process_business()` — `save_job(job_data)`
- **Fix**: Before saving, check if a job with the same `call_sid` already exists. If so, skip.

---

## Group D: ContentView.swift + AppState.swift + KevinApp.swift

File paths:
- `/Volumes/Extreme Pro/myprojects/Kevin/ios/Kevin/Views/ContentView.swift`
- `/Volumes/Extreme Pro/myprojects/Kevin/ios/Kevin/Models/AppState.swift`
- `/Volumes/Extreme Pro/myprojects/Kevin/ios/Kevin/App/KevinApp.swift`

### D1 — CRITICAL: Fix timer memory leak in ContentView
- **Location**: `LiveCallTab` — `@State private var timer: Timer?` and `@State private var elapsedTimer: Timer?`
- **Problem**: If view appears/disappears multiple times, timers accumulate.
- **Fix**: In `onDisappear`, invalidate BOTH timers unconditionally:
```swift
.onDisappear {
    stopPolling()
    elapsedTimer?.invalidate()
    elapsedTimer = nil
}
```

### D2 — CRITICAL: Reset callIgnored on new call
- **Location**: `AppState.setActiveCall()` 
- **Problem**: If a call is ignored and a new call arrives, `callIgnored` is still true → wrong UI.
- **Fix**: Add `callIgnored = false` inside `setActiveCall()`.

### D3 — CRITICAL: Fix AppState thread safety
- **Location**: All `@Published var` with `didSet { UserDefaults.standard.set(...) }`
- **Problem**: @Published triggers on any thread; UserDefaults.set from multiple threads = race condition.
- **Fix**: Wrap UserDefaults writes in `DispatchQueue.main.async`:
```swift
@Published var isOnboarded: Bool = UserDefaults.standard.bool(forKey: "isOnboarded") {
    didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.isOnboarded, forKey: "isOnboarded") } }
}
```
Or simpler: use `@AppStorage` where possible (but @AppStorage doesn't work with @Published in ObservableObject).

### D4 — HIGH: Validate checkForActiveCall response
- **Location**: `AppState.checkForActiveCall()` — if API returns data with empty callSid, it still calls `setActiveCall()`
- **Fix**: Add guard: `guard !call.callSid.isEmpty else { return }`

### D5 — HIGH: Use stable IDs for transcript lines
- **Location**: `ContentView.swift` — `ForEach(Array(appState.transcriptLines.enumerated()), id: \.offset)`
- **Problem**: Using offset as ID causes full list rebuild on every new line.
- **Fix**: Create a struct with stable UUID:
```swift
struct TranscriptLine: Identifiable {
    let id = UUID()
    let text: String
}
```
Change `transcriptLines` from `[String]` to `[TranscriptLine]` in AppState. Update ForEach to use `.id`.

### D6 — MEDIUM: Add error handling for checkForActiveCall
- **Location**: `KevinApp.swift` — `appState.checkForActiveCall()`
- **Fix**: The function already has try/catch internally. Just add a comment that errors are handled silently (acceptable for this polling check).

---

## Group E: SettingsView.swift + OnboardingView.swift

File paths:
- `/Volumes/Extreme Pro/myprojects/Kevin/ios/Kevin/Views/SettingsView.swift`
- `/Volumes/Extreme Pro/myprojects/Kevin/ios/Kevin/Views/OnboardingView.swift`

### E1 — CRITICAL: Add recovery path for onboarding network failure
- **Location**: `OnboardingView` — `provisioningStep` view
- **Problem**: If provisioning fails, user is stuck with error message and "Try Again" button, but if that also fails, no way out.
- **Fix**: Add a "Start Over" button below "Try Again" that resets to `.welcome` step:
```swift
Button("Start Over") {
    step = .welcome
    errorMessage = ""
}
.font(.subheadline)
.foregroundStyle(.secondary)
```

### E2 — HIGH: Consolidate duplicate provisioning functions
- **Location**: `provisionNumber()` and `provisionPersonal()` are 95% identical
- **Fix**: Merge into single function with a `mode` parameter:
```swift
private func provision(mode: String) async {
    let isPersonal = mode == "personal"
    let bizName = isPersonal ? "\(ownerName)'s phone" : businessName
    let svcType = isPersonal ? "personal" : serviceType
    // ... rest is identical
}
```
Then call `provision(mode: "business")` and `provision(mode: "personal")` from the respective buttons.

### E3 — HIGH: Prevent concurrent toggle saves in SettingsView
- **Location**: All toggle `onChange` handlers
- **Fix**: Add a `@State private var isSaving = false` flag. Disable toggles while saving:
```swift
Toggle("Known contacts ring through", isOn: $appState.ringThroughContacts)
    .disabled(isSaving)
    .onChange(of: appState.ringThroughContacts) { _, newValue in
        Task {
            isSaving = true
            await updateRingThrough(newValue)
            isSaving = false
        }
    }
```

### E4 — HIGH: Add error feedback for failed settings saves
- **Location**: All PATCH helper functions (`updateRingThrough`, `updateSitToneEnabled`, `updateContractorSetting`)
- **Fix**: Return success/failure, show brief toast on failure:
```swift
@State private var saveError = ""

// In helper:
private func updateContractorSetting(_ key: String, _ value: Any) async -> Bool {
    // ... existing code ...
    let (_, response) = try await URLSession.shared.data(for: request)
    return (response as? HTTPURLResponse)?.statusCode == 200
}

// On failure:
if !(await updateContractorSetting("key", value)) {
    saveError = "Failed to save. Check your connection."
}
```

### E5 — MEDIUM: Add knowledge text length limit
- **Location**: Knowledge editor in SettingsView
- **Fix**: Add `.onChange(of: knowledgeText)` that truncates to 10,000 chars with warning.

---

## Group F: APIClient.swift + CallHistoryView.swift + AppDelegate.swift + ContactSyncManager.swift

File paths:
- `/Volumes/Extreme Pro/myprojects/Kevin/ios/Kevin/Services/APIClient.swift`
- `/Volumes/Extreme Pro/myprojects/Kevin/ios/Kevin/Views/CallHistoryView.swift`
- `/Volumes/Extreme Pro/myprojects/Kevin/ios/Kevin/App/AppDelegate.swift`
- `/Volumes/Extreme Pro/myprojects/Kevin/ios/Kevin/Services/ContactSyncManager.swift`

### F1 — HIGH: Add retry logic to APIClient
- **Location**: All API methods in APIClient.swift
- **Fix**: Add a private retry wrapper:
```swift
private func retryRequest(_ request: URLRequest, maxRetries: Int = 2) async throws -> (Data, URLResponse) {
    var lastError: Error?
    for attempt in 0...maxRetries {
        do {
            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode >= 500 && attempt < maxRetries {
                try await Task.sleep(nanoseconds: UInt64(pow(2.0, Double(attempt))) * 1_000_000_000)
                continue
            }
            return (data, response)
        } catch {
            lastError = error
            if attempt < maxRetries {
                try await Task.sleep(nanoseconds: UInt64(pow(2.0, Double(attempt))) * 1_000_000_000)
            }
        }
    }
    throw lastError!
}
```
Use it in critical methods (getActiveCall, registerDevice, sendCallAction, getCallHistory).

### F2 — HIGH: Add error state to CallHistoryView
- **Location**: `CallHistoryView`
- **Fix**: Add `@State private var errorMessage = ""`. On load failure, show error instead of empty list:
```swift
if !errorMessage.isEmpty {
    Text(errorMessage)
        .foregroundStyle(.red)
        .font(.subheadline)
}
```

### F3 — HIGH: Fix VoIP push completion timing in AppDelegate
- **Location**: `pushRegistry(_:didReceiveIncomingPushWith:for:completion:)`
- **Problem**: `completion()` called before CallKit is notified.
- **Fix**: Move `completion()` AFTER `reportIncomingCall()`:
```swift
CallManager.shared.reportIncomingCall(/* ... */) { error in
    if let error = error {
        print("CallKit error: \(error)")
    }
    completion()  // Call AFTER CallKit is set up
}
```

### F4 — MEDIUM: Extract phone formatting utility
- **Problem**: Phone number formatting logic duplicated in ContentView, SettingsView, CallHistoryView, AppDelegate.
- **Fix**: Create `/Volumes/Extreme Pro/myprojects/Kevin/ios/Kevin/Utils/PhoneFormatter.swift`:
```swift
struct PhoneFormatter {
    static func format(_ phone: String) -> String {
        let digits = phone.filter { $0.isNumber }
        if digits.count == 11, digits.hasPrefix("1") {
            let area = digits.dropFirst().prefix(3)
            let mid = digits.dropFirst(4).prefix(3)
            let last = digits.suffix(4)
            return "(\(area)) \(mid)-\(last)"
        }
        if digits.count == 10 {
            let area = digits.prefix(3)
            let mid = digits.dropFirst(3).prefix(3)
            let last = digits.suffix(4)
            return "(\(area)) \(mid)-\(last)"
        }
        return phone
    }
}
```
Then replace all duplicate formatting in ContentView, SettingsView, CallHistoryView.

### F5 — MEDIUM: Normalize phone numbers before hashing in ContactSyncManager
- **Location**: `ContactSyncManager.syncContacts()` — hash computation
- **Problem**: "+1234567890" and "1234567890" hash differently → unnecessary syncs.
- **Fix**: Normalize all phones to digits-only before hashing:
```swift
let normalizedPhones = contacts.map { $0.phone.filter { $0.isNumber } }.sorted()
```

### F6 — MEDIUM: Add error state to ContactSyncManager
- **Fix**: Return an enum result instead of just counts:
```swift
enum SyncResult {
    case success(synced: Int, removed: Int)
    case permissionDenied
    case rateLimited
    case error(String)
}
```

---

## Post-Fix Checklist

After all groups complete:

1. Deploy backend: `gcloud config set account deli@ellaexecutivesearch.com && cd "/Volumes/Extreme Pro/myprojects/Kevin" && gcloud run deploy kevin-api --source . --project kevin-491315 --region us-central1 --allow-unauthenticated`
2. Regenerate Xcode: `cd "/Volumes/Extreme Pro/myprojects/Kevin/ios" && xcodegen generate && sed -i '' 's|<string>development</string>|<string>production</string>|' Kevin/Kevin.entitlements`
3. Verify NSAllowsArbitraryLoads is false in Info.plist
4. Verify APNs environment is production in Kevin.entitlements
5. Archive and upload to App Store Connect
6. Version: 1.2.0, Build: 1
