# Gemini Live Voice Pipeline — Design Spec

**Date:** 2026-04-07
**Status:** Approved for implementation

## Context

Kevin AI currently uses a 3-service voice pipeline: Deepgram STT → Claude Sonnet 4 → ElevenLabs TTS. This costs ~$59/month in AI services for a heavy business user (600 calls/month). Gemini Live API can replace all three with a single audio-native WebSocket, reducing AI costs to ~$34/month (42% savings) while cutting response latency from ~800-1500ms to ~250-500ms.

The Gemini pipeline will coexist alongside the current pipeline. A `voice_engine` field on the contractor model determines which runs. This allows A/B testing, per-user configuration, and graceful rollout.

---

## Architecture

```
Twilio Media Stream WebSocket
        │
        ▼
media_stream.py (unchanged callback interface)
        │
        ├── voice_engine = "elevenlabs"          ├── voice_engine = "gemini"
        │                                        │
        ▼                                        ▼
┌─────────────────────────┐   ┌──────────────────────────────────┐
│ VoicePipeline (current) │   │ GeminiPipeline (new)             │
│                         │   │                                  │
│ mulaw 8kHz              │   │ mulaw 8kHz                       │
│   → Deepgram STT        │   │   → audio.mulaw_to_pcm16k()     │
│   → Claude Sonnet 4     │   │   → Gemini Live API WebSocket    │
│   → ElevenLabs TTS      │   │   ← PCM 24kHz from Gemini       │
│   → mulaw 8kHz          │   │   → audio.pcm24k_to_mulaw()     │
│                         │   │   → mulaw 8kHz                   │
└─────────────────────────┘   └──────────────────────────────────┘
        │                                        │
        └────────────┬───────────────────────────┘
                     │
                     ▼
        Same callbacks for both:
        - on_audio_out(mulaw_chunk)
        - on_transcript(speaker, text)
        - on_clear_audio()
        - on_call_complete()
        - on_urgency_detected(snippet)
                     │
                     ▼
        Same post-call processing:
        - Claude extracts job card
        - SMS to contractor + caller
        - Contact extraction
        - Push notification
```

---

## New Files

### 1. `app/utils/audio.py` — Audio Conversion

Converts between Twilio's mulaw 8kHz and Gemini's PCM formats.

```python
def mulaw_to_pcm16k(mulaw_8k: bytes) -> bytes:
    """Decode mulaw to linear PCM, upsample 8kHz → 16kHz.
    
    Steps:
    1. audioop.ulaw2lin(mulaw_8k, 2) → PCM 16-bit 8kHz
    2. audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None) → PCM 16-bit 16kHz
    Returns: raw PCM bytes (16-bit signed LE, 16kHz mono)
    """

def pcm24k_to_mulaw(pcm_24k: bytes) -> bytes:
    """Downsample PCM 24kHz → 8kHz, encode to mulaw.
    
    Steps:
    1. audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, None) → PCM 16-bit 8kHz
    2. audioop.lin2ulaw(pcm_8k, 2) → mulaw 8kHz
    Returns: mulaw bytes ready for Twilio
    """
```

Uses Python's built-in `audioop` module (no external dependencies). Both functions are pure, stateless, and fast enough for real-time audio (<1ms per chunk).

Note: `audioop` is deprecated in Python 3.11+ and removed in 3.13. If the Cloud Run Python version is 3.13+, use the `audioop-lts` package instead.

### 2. `app/services/gemini_pipeline.py` — Gemini Live Pipeline

Same public interface as `VoicePipeline` so `media_stream.py` can use either interchangeably.

```python
class GeminiPipeline:
    """Voice pipeline using Gemini Live API (audio-native).
    
    Replaces Deepgram + Claude + ElevenLabs with a single
    Gemini WebSocket: audio in → reasoning → audio out.
    """

    def __init__(
        self,
        on_audio_out: Callable[[bytes], Awaitable[None]],
        on_transcript: Callable[[str, str], Awaitable[None]],
        on_clear_audio: Optional[Callable[[], Awaitable[None]]] = None,
        on_call_complete: Optional[Callable[[], Awaitable[None]]] = None,
        on_urgency_detected: Optional[Callable[[str], Awaitable[None]]] = None,
        call_sid: str = "",
        contractor_config: Optional[dict] = None,
    ):
        ...

    async def start(self) -> bool:
        """Connect to Gemini Live API, send setup, deliver greeting."""

    async def process_audio_in(self, mulaw_bytes: bytes):
        """Convert mulaw→PCM16k, send to Gemini."""

    async def stop(self):
        """Close Gemini WebSocket, cancel background tasks."""
```

#### Internal Architecture

**Connection & Setup:**
- WebSocket to `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={key}`
- Model: `gemini-2.5-flash-live-preview-native-audio`
- Setup message includes:
  - `response_modalities: ["AUDIO"]`
  - `speech_config` with voice selection (Puck for English male)
  - `system_instruction` — reuse `build_system_prompt()` from `voice_pipeline.py`
  - `tools` — Jobber/Calendar function definitions (converted to Gemini format)
  - `contextWindowCompression: { enabled: true }` — handles calls >15 minutes

**Audio Flow:**
- Incoming: `process_audio_in(mulaw)` → `mulaw_to_pcm16k()` → send as base64 `realtime_input.media_chunks`
- Outgoing: receive `serverContent.modelTurn.parts[].inlineData` → base64 decode → `pcm24k_to_mulaw()` → `on_audio_out(mulaw_chunk)`

**Transcript Extraction:**
- Gemini returns text parts alongside audio in `modelTurn.parts`
- Extract text → `on_transcript("Kevin", text)`
- For caller speech: Gemini processes it internally, but we need the text for RTDB/transcript display
- Use `inputTranscript` from Gemini's response (available in native audio models) → `on_transcript("Caller", text)`

**Barge-In:**
- Gemini sends `serverContent.interrupted: true` when the caller speaks during output
- On receiving this: set `_interrupt_speaking = True`, call `on_clear_audio()`
- No manual barge-in detection needed — Gemini handles it natively

**Urgency Detection:**
- Same keyword set as current pipeline (emergency, flood, fire, etc.)
- Scan caller transcript text for keywords
- Fire `on_urgency_detected(snippet)` callback

**Unavailability Timer:**
- Same 45-second timer after 3+ exchanges
- Same logic: `_unavailable_now()` sends a text instruction to Gemini to deliver the unavailability message
- Gemini speaks it in the current voice/language automatically

**Silence Detection:**
- Same 2-minute silence timeout
- Track `_last_speech_time` based on incoming audio activity or Gemini transcript events

**Function Calling (Jobber/Calendar):**
- Gemini 2.5 Flash Live supports async (non-blocking) function calling
- Define tools in Gemini format during setup:
  ```json
  {
    "tools": [{
      "function_declarations": [{
        "name": "check_availability",
        "description": "Check available appointment slots",
        "parameters": { ... }
      }]
    }]
  }
  ```
- When Gemini returns a `tool_call`, execute it (reuse existing `_execute_tool` logic from VoicePipeline)
- Send result back as `tool_response`
- Gemini continues speaking while tool executes (non-blocking)

**Language Handling:**
- Gemini natively detects and responds in the caller's language
- No separate language switching logic needed
- System prompt includes language instruction (same as current)

**Goodbye Detection:**
- Scan Kevin's text output for goodbye phrases (same list as current pipeline)
- Trigger `on_call_complete()` after 2-second delay

---

## Modified Files

### 3. `app/webhooks/media_stream.py` — Pipeline Selection

**Change:** After loading contractor config, check `voice_engine` and instantiate the correct pipeline.

```python
# Determine which voice pipeline to use
voice_engine = contractor_config_loaded.get("voice_engine", "elevenlabs")

if voice_engine == "gemini" and settings.gemini_api_key:
    from app.services.gemini_pipeline import GeminiPipeline
    pipeline = GeminiPipeline(
        on_audio_out=on_audio_out,
        on_transcript=on_transcript,
        on_clear_audio=on_clear_audio,
        on_call_complete=on_call_complete,
        on_urgency_detected=on_urgency_detected,
        call_sid=call_sid,
        contractor_config=contractor_config_loaded,
    )
else:
    pipeline = VoicePipeline(
        on_audio_out=on_audio_out,
        on_transcript=on_transcript,
        on_clear_audio=on_clear_audio,
        on_call_complete=on_call_complete,
        on_urgency_detected=on_urgency_detected,
        call_sid=call_sid,
        contractor_config=contractor_config_loaded,
    )
```

Everything below this (callbacks, post-call processing, RTDB updates) stays unchanged.

### 4. `app/db/contractors.py` — Add voice_engine Field

**Change:** Add `voice_engine` to contractor creation defaults and update model.

- Default: `"elevenlabs"` (no change for existing contractors)
- Values: `"elevenlabs"`, `"gemini"`

### 5. `app/api/settings.py` — Expose voice_engine Setting

**Change:** Allow contractors to update their `voice_engine` via the settings API.

### 6. `app/services/gemini_agent.py` — Deprecate

**Change:** This file is the old prototype. Once `gemini_pipeline.py` is built, this file can be deleted. Don't modify it.

---

## Gemini Tool Format Conversion

Current Jobber/Calendar tools are defined in Claude's format. Gemini uses a different format. The conversion:

**Claude format (current):**
```json
{
  "name": "check_availability",
  "description": "Check available slots",
  "input_schema": {
    "type": "object",
    "properties": { "days_ahead": { "type": "integer" } },
    "required": []
  }
}
```

**Gemini format (needed):**
```json
{
  "function_declarations": [{
    "name": "check_availability",
    "description": "Check available slots",
    "parameters": {
      "type": "OBJECT",
      "properties": { "days_ahead": { "type": "INTEGER" } }
    }
  }]
}
```

Build a helper `_convert_tools_to_gemini(claude_tools: list) -> dict` in `gemini_pipeline.py` that converts the existing tool definitions.

---

## Session Limits & Handling

- **15-minute session limit**: Handled by `contextWindowCompression: { enabled: true }` in the setup message. This lets Gemini summarize older conversation history and continue indefinitely.
- **Reconnection**: If the WebSocket drops, attempt one reconnect. If it fails, log and call `on_call_complete()` to hang up gracefully.
- **Max call duration**: Same 90-minute safeguard as current pipeline.

---

## Audio Output Chunking

Gemini sends audio in chunks as they're generated. Each chunk arrives as a base64-encoded PCM 24kHz segment in the WebSocket message. The receive loop:

1. Decode base64 → PCM 24kHz bytes
2. Convert: `pcm24k_to_mulaw(pcm_bytes)` → mulaw 8kHz
3. Call `on_audio_out(mulaw_chunk)`

No pacing/timing logic needed — Gemini streams audio at approximately real-time. The Twilio WebSocket handles playback timing.

---

## What Is NOT Changing

- `VoicePipeline` (current pipeline) — untouched, still the default
- `media_stream.py` callback structure — identical for both pipelines  
- Post-call processing — Claude still extracts job cards and sends SMS
- RTDB state management — same active call state
- Push notifications — same flow
- iOS app — no changes needed (voice_engine is a backend-only setting for now)
- Twilio configuration — same media streams, same webhooks

---

## Verification Plan

1. **Unit test audio conversion**: Encode known mulaw samples, verify round-trip (mulaw→PCM16k→mulaw) produces valid audio
2. **Local WebSocket test**: Connect to Gemini Live API, send a greeting prompt, verify audio response is received
3. **Integration test**: Set a test contractor's `voice_engine` to `"gemini"`, make a test call, verify:
   - Kevin greets the caller
   - Caller speech is recognized and responded to
   - Transcript appears in RTDB (iOS app can poll it)
   - Barge-in works (interrupt Kevin mid-sentence)
   - Unavailability timer fires at 45 seconds
   - Call ends after Kevin says goodbye
   - Post-call SMS is sent
   - Job card is extracted
4. **A/B quality test**: Same caller, same script — one call through ElevenLabs pipeline, one through Gemini. Compare voice quality and response latency.
5. **Latency measurement**: Log timestamps at key points (audio received → audio sent back) to measure actual response time.
6. **15-minute call test**: Verify context window compression works for longer calls.
