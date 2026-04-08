# Voice Pipeline Latency Optimization

**Date:** 2026-04-06
**Status:** Tier 1 ready for testing

---

## Problem Statement

Kevin's voice pipeline (STT → LLM → TTS) has a noticeable delay between when the caller finishes speaking and when Kevin starts responding. The target is ~2.5 seconds. Actual measurements show:

- **Good path:** ~2.6s (acceptable but tight)
- **Bad path:** ~4.5s (too slow — happens on roughly half of utterances)

The caller must not feel talked over. The current 900ms Deepgram endpointing value is intentionally set high to prevent Kevin from interrupting callers who are pausing to think.

---

## Current Architecture

```
Caller speaks → Twilio Media Stream → Deepgram STT (WebSocket) → Claude Sonnet 4 (HTTP POST) → ElevenLabs TTS (HTTP POST) → Twilio playback
```

**Current config:**
- Deepgram nova-3, `endpointing=900`, `utterance_end_ms=2000`, `language=multi`
- Claude `claude-sonnet-4-20250514`, non-streaming, `max_tokens=100`
- ElevenLabs `eleven_turbo_v2_5`, non-streaming POST, `output_format=ulaw_8000`
- New `httpx.AsyncClient` created per request (no connection reuse)

---

## Root Cause Analysis

### How Deepgram End-of-Speech Detection Works

Deepgram sends three types of finalization events:

1. **`is_final=true, speech_final=true`** — Definitive end of utterance. Fires after `endpointing` ms of silence when Deepgram is confident the speaker is done. This is the fast path.

2. **`is_final=true, speech_final=false`** — Partial final. The segment is transcribed but the speaker might continue. We accumulate these in a buffer.

3. **`UtteranceEnd`** — Fallback timer. Fires `utterance_end_ms` after the last word, regardless of speech_final. This is the safety net.

### The Two Paths (from actual server logs)

**Good path — speech_final fires:**
```
21:37:27.514  STT [final, speech_final=True]: Hi, Kevin. Is Dell available?
21:37:28.703  Kevin: Can I get your name...                    (+1.19s Claude)
21:37:30.924  TTS: 16347 bytes                                 (+2.22s TTS)
              Total: ~2.6s from end-of-speech to first audio
```

**Bad path — speech_final doesn't fire, falls back to UtteranceEnd:**
```
21:37:40.511  STT [final, speech_final=False]: My name is Brian...
21:37:42.357  UtteranceEnd received — processing buffer         (+1.85s WAIT)
21:37:43.692  Kevin: Got it...                                  (+1.34s Claude)
21:37:43.941  TTS: 18204 bytes                                 (+0.25s TTS)
              Total: ~4.5s from end-of-speech to first audio
```

**Key finding:** Deepgram's `speech_final` frequently doesn't fire (documented in Deepgram GitHub Discussion #409). When it doesn't, we wait the full `utterance_end_ms=2000ms` before processing. This is the primary source of the extra delay.

### Latency Budget Breakdown

| Stage | Good Path | Bad Path | Notes |
|-------|-----------|----------|-------|
| Deepgram endpointing | 900ms | 900ms | Silence detection before marking final |
| Wait for speech_final/UtteranceEnd | 0ms | ~1100ms | UtteranceEnd fires at 2000ms minus endpointing |
| Claude API (full response) | ~1200ms | ~1200ms | New TCP+TLS connection each time |
| ElevenLabs TTS (full audio) | ~500ms | ~500ms | New TCP+TLS connection each time |
| **Total** | **~2.6s** | **~4.5s** | |

---

## Optimization Plan

### Tier 1 — Config and Connection Changes (Low Risk)

These changes don't alter the pipeline architecture. They tune existing parameters and eliminate wasteful connection overhead.

#### 1A. Reduce `utterance_end_ms` from 2000 → 1000

**What it does:** When `speech_final` doesn't fire, the UtteranceEnd fallback currently waits 2000ms after the last word. Reducing to 1000ms cuts 1000ms from the bad path.

**Why 1000 is safe:** Deepgram's docs state that values below 1000ms provide no benefit because interim results are sent roughly every second. The 1000ms floor is well-established in production voice systems. Importantly, this does NOT affect the talk-over protection — that's controlled by `endpointing=900`, which we're keeping unchanged.

**Risk:** Minimal. The endpointing (900ms silence gap) is what prevents Kevin from interrupting. The `utterance_end_ms` is only a fallback for when speech_final doesn't fire.

**Impact:** Bad path drops from ~4.5s to ~3.5s.

**Change:** `voice_pipeline.py` line ~438, `utterance_end_ms=2000` → `utterance_end_ms=1000`

#### 1B. Switch TTS model from `eleven_turbo_v2_5` → `eleven_flash_v2_5`

**What it does:** ElevenLabs Flash v2.5 generates first audio in ~75ms vs ~300ms for Turbo v2.5. Same voice quality, same price, same API. ElevenLabs officially recommends Flash over Turbo "in all use cases" as of 2025.

**Risk:** None. Drop-in replacement. Same voice, same quality, same cost.

**Impact:** Saves ~225ms per response on both paths.

**Change:** `voice_pipeline.py` line ~203, model ID constant.

#### 1C. Reuse httpx.AsyncClient (Connection Pooling)

**What it does:** Currently, every Claude call and every ElevenLabs call creates a brand new `httpx.AsyncClient`, which means a fresh TCP connection + TLS handshake per request (~100-150ms each). By creating persistent clients in `__init__` and reusing them, subsequent calls reuse the existing connection.

**Risk:** Low. Standard practice recommended by httpx docs. Need to close clients in `stop()`.

**Impact:** Saves ~100-150ms per API call. Two calls per turn (Claude + ElevenLabs) = 200-300ms saved.

**Change:** Create `self._http_client` in `VoicePipeline.__init__`, use it in `_handle_caller_speech` and `_speak`, close in `stop()`.

#### Tier 1 Projected Results

| Path | Before | After Tier 1 | Saved |
|------|--------|-------------|-------|
| Good (speech_final fires) | ~2.6s | ~2.1s | ~500ms |
| Bad (UtteranceEnd fallback) | ~4.5s | ~2.8s | ~1700ms |

---

### Tier 2 — Claude Streaming + Sentence Chunking (Medium Effort)

**Only proceed here if Tier 1 doesn't meet the 2.5s target.**

#### 2A. Claude Streaming API with Sentence-Boundary Flushing

**What it does:** Instead of waiting for Claude's complete response (~1200ms), use the streaming API. Claude's time-to-first-token (TTFT) is ~400ms. Buffer streaming tokens and flush to TTS at sentence boundaries (`. ? ! \n`).

**How it works:**
1. Open Claude streaming request (`stream=True`)
2. Accumulate tokens in a buffer
3. When a sentence boundary is detected, send that sentence to ElevenLabs immediately
4. Start playing first sentence audio while Claude continues generating the rest
5. Queue subsequent sentences

**Example timing:**
```
Claude TTFT:        400ms  (first tokens arrive)
First sentence:     600ms  (sentence complete, send to TTS)
TTS first audio:    675ms  (Flash v2.5 generates in 75ms)
Caller hears Kevin: 675ms  (meanwhile Claude is still generating)
```

**Risk:** Medium. Requires restructuring `_handle_caller_speech` and `_speak`. Need to handle barge-in correctly during streaming. The Anthropic + ElevenLabs cookbook documents this pattern.

**Impact:** Claude+TTS combined drops from ~1.7s to ~0.7s. Roughly 1 second saved.

**Reference:** [Anthropic + ElevenLabs Low Latency Cookbook](https://platform.claude.com/cookbook/third-party-elevenlabs-low-latency-stt-claude-tts)

#### 2B. ElevenLabs HTTP Streaming Response

**What it does:** Instead of waiting for the full TTS audio (POST, wait, receive all bytes), use the streaming endpoint that returns chunked audio. Start sending audio chunks to Twilio as they arrive instead of waiting for the complete file.

**Risk:** Medium. Need to handle chunked response and send to Twilio incrementally. The WAV/RIFF header stripping logic needs adjustment.

**Impact:** Saves ~200-400ms on longer responses (shorter responses are already fast).

#### Tier 2 Projected Results

| Path | After Tier 1 | After Tier 2 | Total Saved |
|------|-------------|-------------|-------------|
| Good | ~2.1s | ~1.2s | ~1.4s |
| Bad | ~2.8s | ~2.0s | ~2.5s |

---

### Tier 3 — Model and Architecture Changes (Higher Effort)

**Only proceed here if Tier 2 doesn't meet the target or if we want best-in-class latency.**

#### 3A. Switch to Claude Haiku 4.5

**What it does:** Claude Haiku 4.5 has ~350ms TTFT (streaming) vs ~800ms for Sonnet 4. For phone conversations (short, simple responses), Haiku's quality is more than sufficient.

**Trade-off:** Slightly less nuanced responses, but Kevin's responses are 1-2 sentences — Haiku handles this perfectly.

**Model ID:** `claude-haiku-4-5-20251001`

**Impact:** ~400ms saved on TTFT. Combined with streaming, TTFT drops to ~350ms.

#### 3B. ElevenLabs WebSocket Streaming (Persistent Connection)

**What it does:** Open a WebSocket to ElevenLabs at call start and keep it alive. Send text chunks as they arrive from Claude streaming. Audio chunks come back on the same WebSocket — no connection setup per utterance.

**Impact:** Eliminates per-utterance connection overhead entirely. Pairs perfectly with Claude streaming for minimum latency.

#### 3C. Deepgram Flux Model with EagerEndOfTurn

**What it does:** Deepgram's Flux model integrates end-of-turn detection directly into the STT model, providing `EagerEndOfTurn` events that fire before the definitive end-of-turn. This lets you start the LLM call speculatively, saving 200-600ms on the STT wait.

**Risk:** Higher — requires handling speculative execution (cancel LLM call if the caller continues speaking).

#### 3D. Speculative LLM Triggering on Interim Transcripts

**What it does:** Start the Claude call on interim (non-final) transcripts. If the caller continues speaking and the transcript changes significantly, cancel and restart. Used by Sierra AI and others.

**Risk:** High — wasted API calls, potential for wrong responses if cancellation is too slow.

#### Tier 3 Projected Results

| Path | After Tier 2 | After Tier 3 | Total Saved |
|------|-------------|-------------|-------------|
| Good | ~1.2s | ~0.7s | ~1.9s |
| Bad | ~2.0s | ~1.2s | ~3.3s |

---

## Testing Plan

### Tier 1 Test Protocol

1. Deploy Tier 1 changes to Cloud Run
2. Make 5+ test calls with varied utterance types:
   - Short utterances: "Hi, is Deli there?" (tests speech_final reliability)
   - Medium utterances: "My name is Brian, I'm calling about a plumbing issue" (tests UtteranceEnd path)
   - Long utterances: Full address or callback number (tests multi-segment accumulation)
3. Check server logs for timing:
   ```
   gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="kevin-api" AND (jsonPayload.message=~"STT|TTS:|Complete utterance|Kevin:|UtteranceEnd")' --project kevin-491315 --limit=50 --format='value(timestamp, jsonPayload.message)' --freshness=30m
   ```
4. Measure end-of-speech → Kevin speaks gap from logs
5. Verify Kevin does NOT talk over the caller (endpointing=900 unchanged)

### Success Criteria

- Good path: < 2.5s
- Bad path: < 3.0s
- No instances of Kevin talking over the caller

---

## Sources

- [Deepgram Endpointing Docs](https://developers.deepgram.com/docs/endpointing)
- [Deepgram UtteranceEnd Docs](https://developers.deepgram.com/docs/utterance-end)
- [Deepgram Discussion #409 — speech_final sometimes doesn't fire](https://github.com/orgs/deepgram/discussions/409)
- [Deepgram Discussion #588 — Best settings for conversational bot](https://github.com/orgs/deepgram/discussions/588)
- [Anthropic + ElevenLabs Low Latency Cookbook](https://platform.claude.com/cookbook/third-party-elevenlabs-low-latency-stt-claude-tts)
- [Anthropic — Reducing Latency](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-latency)
- [ElevenLabs Latency Optimization](https://elevenlabs.io/docs/best-practices/latency-optimization)
- [ElevenLabs Models — Flash vs Turbo](https://elevenlabs.io/docs/overview/models)
- [Sierra AI — Engineering Low-Latency Voice Agents](https://sierra.ai/blog/voice-latency)
- [Cresta — Engineering for Real-Time Voice Agent Latency](https://cresta.com/blog/engineering-for-real-time-voice-agent-latency)
- [AssemblyAI — Lowest latency Vapi agent](https://www.assemblyai.com/blog/how-to-build-lowest-latency-voice-agent-vapi)
- [httpx — Connection Pooling Best Practices](https://www.python-httpx.org/advanced/clients/)
