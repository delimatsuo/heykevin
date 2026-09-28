# First-response latency audit — September 28, 2026

## Finding

Recent production calls contain unusually slow completed generation rounds alongside the owner's report of a 6–8 second pause after the first introduction and request to speak with the owner. The measured generation/streaming interval is longer than earlier calls on the same revision. These clocks do **not** reproduce or locate the reported initial silent pause: text could have started streaming before the generation completed, and playback could have started before its first receipt.

The generation path is a concrete lead, but the cause of the caller-heard pause remains unresolved. First model text, first outbound text send, and speech-start timing are needed to distinguish model/connection delay from Twilio synthesis/playback delay. Startup, tools, owner-hold sequencing, and the recent notification repair do not explain the measured long generation round. No performance fix or provider change was made by this audit.

## Source and runtime binding

- Investigated source: `c8d974daf8d34cbfa62c5088a2db9f53c808330b` on `codex/first-response-latency-audit`.
- Live production SHA: `2b402796d1cbffaf59bcb6ecb63d661d9dd0ef12`; revision `kevin-api-00273-f7g`, 100% traffic when inspected. That backend was released September 17.
- The only `app/` difference between inspected source and production is `app/api/voip.py`, the notification device-registration repair. The voice files match production.
- Selected engine in the observed calls: `relay`. Its configured text-model default is `gemini-3.5-flash`; no production override for `RELAY_TEXT_MODEL` was present. This binds the requested model, not an immutable provider model implementation.
- Runtime inspection found minimum instances 1, maximum 10, CPU throttling disabled, and startup CPU boost enabled. These settings alone do not rule out transient contention.

## Measurements

Read-only Cloud Logging query: September 21, 00:00 UTC through September 28, 18:09:01 UTC. The bounded query returned **224 matching events for 14 session labels**, below its 10,000-row cap, all on `kevin-api-00273-f7g`. Twelve sessions had a completed generation round. Metrics exclude cancelled/failed generations that never emitted `reply_generated`.

Only timing events were selected. The export projected timestamp, revision, and an allowlisted event message; its parser retained numeric timing/count fields and further hashed shortened session labels. The committed evidence omits even those hashes. No caller transcript, audio, phone number, account record, API key, or error-response body was read.

| Recent call start (Eastern) | Pipeline setup | First completed generation round | Final prompt → first following playback receipt | Tool calls |
| --- | ---: | ---: | ---: | ---: |
| Sep 28, 1:57:41 p.m. | 188 ms | 5,445 ms | 6,520 ms | 0 |
| Sep 28, 1:58:52 p.m. | 174 ms | 5,884 ms | 7,028 ms | 0 |

Both pipelines were ready well before the final caller prompt. In the second call, a short three-character prompt preceded the fuller prompt; the reported interval starts at the fuller prompt, which superseded the first generation. The exact utterance identity remains unverified because the audit did not read transcripts. The recent receipt intervals are numerically similar to the reported duration; neither a transcript match nor acoustic timing is confirmed.

The 29 completed rounds logged September 22–27 were all at or below **1,174 ms**. The first completed round in the September 28 morning call was **743 ms**. The two afternoon first replies then rose to **5,445 and 5,884 ms**, and the first afternoon call had a later **6,383 ms** round. The second afternoon call subsequently had **3,335, 1,410, and 838 ms** rounds. This is variation within one production revision, not evidence of a newly deployed voice-code regression.

Exact timestamps, daily counts, first completed rounds, source anchors, and counted graph inputs are in [the companion evidence](2026-09-28-first-response-latency-evidence.json).

### What the clocks mean

`reply_generated.ms` wraps the entire `_run_stream()` operation: provider connection/request/stream processing **and awaited text sends to Twilio**. It is not a time-to-first-token metric. `tokens-played` receipts report playback progress, not the exact instant the caller first heard speech. Caller speech-end detection occurs before the final prompt and is not measured by these intervals. Consequently, the 6.5–7.0 second receipt interval must not be described as an exact acoustic response delay.

## Source and history checks

1. `RelayPipeline._run_stream` sends each nonempty text part as it arrives (`app/services/relay_pipeline.py:608`). There is no ordinary first-turn sentence buffer or wait for the whole reply. `_send_current` awaits the websocket send (`:403`).
2. The model round timer ends only after streaming completes (`:475`). Neither slow first round invoked tools. Personal mode exposes no tools (`:768`).
3. The ordinary phrase-detected owner hold and summary push start after acknowledgement text is sent (`:519–532`, `:847–872`). The separate urgent-signal path can start a background hold timer before generation (`:297–304`), but does not await that timer or notification delivery. Changing the hold timer would not address the measured model-round interval.
4. The transcript callback appends locally and schedules persistence (`app/webhooks/relay_stream.py:191–195`); ordinary first-reply generation does not await a notification delivery.
5. The HTTP transport creates a new `httpx.AsyncClient` for each model round (`app/services/relay_pipeline.py:730`), so it does not reuse an earlier round's connection. Its measured contribution is unknown.
6. Commit `83eeba084a37f3c0dffd968c0d9aa6707e3ed586` (August 7) introduced the configurable `gemini-3.5-flash` default and `thinkingBudget: 0`. The September 16 Relay change `d25bbd3` adjusts the later message-taking system instruction; it does not insert a first-response wait. September 17 change `e298c3c` concerns screening-reason/status handling. No later voice source change accounts for today's variation.

## Provider contract check

Google recommends `thinkingLevel` for Gemini 3.x and describes `minimal` as suitable for fast chat responses. However, its Gemini 3.5 FAQ explicitly says the older numeric `thinkingBudget` remains supported. Therefore the current `thinkingBudget: 0` is **not a confirmed incompatibility or root cause**. A configuration migration is a candidate to benchmark, not a demonstrated latency repair. See [Google's Gemini 3.5 migration guide and FAQ](https://ai.google.dev/gemini-api/docs/generate-content/whats-new-gemini-3.5) and [ThinkingConfig API reference](https://ai.google.dev/api/generate-content#ThinkingConfig).

Twilio documents streaming text as it becomes available and describes `tokens-played` as playback-progress events. Kevin already follows the text-streaming pattern. See [ConversationRelay websocket messages](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages) and [ConversationRelay TwiML events](https://www.twilio.com/docs/voice/twiml/connect/conversationrelay).

## Recommended next engineering step

Add metadata-only timing around the existing Relay path before tuning it: generation/request start, HTTP headers, first SSE data, first model text, first completed outbound text send, and stream completion. Bind each event to a pseudonymous session label, generation epoch, and model round so cancelled/superseded prompts cannot be mistaken for the active turn. Record safe model/config names and numeric usage counts where available; exclude text, audio, phone numbers, credentials, raw call identifiers, and error bodies.

Capture Twilio speaker-event timing alongside those measurements, with owner-controlled audible validation, to distinguish timely first text followed by delayed synthesis/playback from a delayed first text. Use the measured stage to select the repair: reuse a managed HTTP connection if setup dominates; evaluate Google's recommended minimal thinking level and the existing prompt if model first-text latency dominates; inspect websocket/backpressure if first-send is delayed; investigate Twilio synthesis/playback if speech starts late after a timely send. Preserve multilingual behavior and owner-hold sequencing during comparison. No model switch, endpointing change, paid benchmark, real call, or deployment is justified as a proven fix by this audit alone.

For an eventual fix, compare first usable caller turn to first outgoing text and caller-heard onset on an owner-controlled call, with the exact deployed SHA and model setting recorded. A passing mock test cannot prove provider or audible latency improvement.

## Verification and scope

Two independent read-only source/history jobs plus the root's runtime query returned all three expected inputs; findings and timing differences were reduced in code into the companion JSON.

Focused local command:

```sh
KEVIN_DISABLE_DOTENV=1 '<clone>/.venv/bin/python' -m pytest -q \
  tests/unit/test_relay_message_envelope.py \
  tests/unit/test_relay_message_transition.py \
  tests/unit/test_relay_pipeline.py
```

Result: **64 passed**, four existing dependency deprecation warnings, 2.90 seconds. The initial invocation listed `test_relay_pipeline.py` first and failed collection because required synthetic settings were absent. Reordering the existing modules let their built-in fictional test configuration initialize first; no credentials or dotenv file were loaded. No application or test source changed. These are protocol/state tests, not live performance measurements.

The audit confirms unusually slow completed generation rounds and a concrete measurement gap. The location and cause of the reported initial silent pause, and any audible improvement, remain unverified.
