# AI Receptionist Memory and Resiliency Research

Date: 2026-07-09

Purpose: summarize current market and platform patterns for building AI receptionist systems with memory, tool use, latency control, and production resilience. This is intended as a discussion brief for the next Hey Kevin architecture conversation.

## Executive Summary

The consistent pattern across current voice-agent platforms is not "train a true AI receptionist from scratch." The pattern is a voice model wrapped in a product system:

1. A realtime voice layer for natural turn-taking, interruptions, and low latency.
2. A deterministic call-state controller for what the receptionist is allowed to do next.
3. Static business knowledge retrieval for policies, pricing ranges, service areas, hours, FAQs, and trade-specific guidance.
4. Dynamic customer memory from CRM/job systems and prior calls.
5. Tool calls for operations such as creating requests, checking schedule availability, booking appointments, and sending follow-up messages.
6. Post-call extraction that updates durable customer memory and call analytics.
7. Monitoring, simulations, logs, and fallback providers for production reliability.

For Hey Kevin, the right direction is to keep the model conversational, but move more control out of the prompt and into explicit state, memory, and tools. The model should not be expected to "remember how to be a receptionist" solely from a long prompt. It should receive compact, relevant context and operate inside a workflow that prevents intrusive questions, repeated questions, unsupported commitments, and bad handoffs.

## What Other Voice-Agent Platforms Are Doing

### 1. Static Knowledge Bases Are Treated As Core Infrastructure

Vapi, Retell, and Bland all expose knowledge-base functionality as a standard feature for voice agents. Vapi describes knowledge bases as custom files that let assistants answer using verified, up-to-date business information rather than general model knowledge. Retell describes its knowledge base as a retrieval system that chunks sources, embeds them, stores them in a vector database, and retrieves relevant chunks during calls. Bland's Pathways product includes a knowledge-base node for answering user questions inside a controlled conversation flow.

Implication for Hey Kevin:

- Contractor business facts should not live primarily in the receptionist prompt.
- We should store business facts in structured fields and/or a compact knowledge base.
- The call prompt should receive only what is relevant to the current contractor and call.
- Retrieval must be bounded, fast, and inspected in logs because wrong or excessive context can degrade voice behavior.

### 2. Dynamic Customer Memory Is Becoming A Standard Product Feature

Retell now documents "persistent contact memory" as a first-class feature. Its pattern is especially relevant:

- Post-call analysis extracts structured facts from each call.
- Extracted fields map into contact fields.
- Merge behavior combines new information with existing memory, deduplicates, reconciles conflicts, and keeps memory current.
- On the next call, contact fields are injected into the agent prompt as dynamic variables.
- Memory can sync back to CRM.

Vapi documents a similar runtime personalization pattern: when an inbound call arrives, the server identifies the caller by phone number, fetches data from a database or CRM, and returns either dynamic variables or a customized assistant configuration. Vapi explicitly calls out using this for customer support and account management scenarios.

Implication for Hey Kevin:

- Jobber memory lookup is the right first step, but it should become one layer in a broader memory system.
- We should maintain our own durable `customer_memory` profile keyed by contractor and caller identity.
- That profile should store stable facts, recent interactions, open commitments, known properties, service history, preferences, and privacy-sensitive notes.
- The model should receive a compact memory card, not raw CRM dumps.
- Memory updates should happen after the call through structured extraction and merge rules, not during every turn.

### 3. Voice Agents Use Workflow State, Not Just Prompts

Bland's Conversational Pathways are explicitly about controlling conversational flow through nodes and pathways. Retell has both single/multi-prompt agents and conversation-flow agents, with node-level knowledge base and dynamic variable behavior. OpenAI's voice-agent docs distinguish between speech-to-speech realtime sessions for natural low-latency interactions and chained voice pipelines for predictable workflows where the app controls transcription, reasoning, and speech output.

Implication for Hey Kevin:

- We should not encode every receptionist rule as natural-language prompt text.
- We need an explicit call-state controller for phases such as greeting, intent classification, service clarification, callback offer, scheduling intent, confirmation, wrap-up, and escalation.
- The controller should track which facts are already known, which facts are optional, and which facts are appropriate to ask only after the caller's intent is clear.
- This directly addresses the recent bug class where Kevin asked for name/address/callback too early or repeated questions.

### 4. Low-Latency Voice Is A Product Architecture Decision

OpenAI's voice-agent docs frame the main architecture choice as:

- Speech-to-speech realtime sessions: best for natural, low-latency conversations, barge-in, live audio, realtime tools, and natural turn-taking.
- Chained voice pipelines: best when the app needs explicit control over STT, text reasoning, TTS, durable transcripts, and deterministic intermediate logic.

Gemini Live API is positioned similarly: low-latency real-time voice/video interactions over a stateful WebSocket, with features such as barge-in, multilingual support, tool use, audio transcripts, and proactive audio. Twilio Media Streams gives telephony apps raw call audio over WebSockets, including bidirectional streams for realtime AI assistants.

Implication for Hey Kevin:

- Gemini Live is a reasonable model layer for low-latency receptionist behavior.
- The delays we saw were likely not evidence that "AI receptionist is impossible"; they were evidence that pipeline setup, pre-call lookup, turn detection, tool timing, and state management need strict budgets.
- Customer memory lookup must be timeout-bounded and optional. If CRM lookup is slow, the call should continue without memory.
- Any operation that can block first audio should have a strict deadline, fallback, or deferred path.

### 5. Production Voice Agents Need Fallbacks, Logs, And Simulations

Vapi documents transcriber fallback plans, voice fallback plans, call logs, API logs, webhook logs, provider status checks, voice test suites, and tool testing. Retell documents enterprise reliability around uptime, latency monitoring, error rates, provider retries/fallbacks, and testing. Retell specifically mentions tracking ASR, TTS, LLM, knowledge-base latency, time-to-first-token, network latency distributions, failed calls, and timeout/error rates.

Implication for Hey Kevin:

- Our product needs its own reliability layer instead of relying on "the model should handle it."
- We should track first response latency, per-turn latency, STT/TTS/model/tool timing, post-call extraction status, Jobber sync status, duplicate-question events, and fallback behavior.
- We should build scenario tests for receptionist behavior before expanding prompts or memory.
- A test call that "felt good" is useful but not enough. We need repeatable evaluation cases.

## Recommended Hey Kevin Architecture

### A. Three Memory Layers

1. Business memory
   - Business name, services, hours, service area, pricing guidance, warranty policy, emergency policy, booking rules.
   - Source: contractor settings, uploaded KB, Jobber account metadata where available.
   - Update cadence: owner-controlled, not learned automatically without review.

2. Customer memory
   - Known caller identity, properties, prior jobs, prior requests, open issues, callback preferences, language preference, relevant notes.
   - Source: Jobber plus Hey Kevin post-call summaries.
   - Update cadence: after each call, using structured extraction and merge rules.

3. Call-state memory
   - Facts already collected in this call, current intent, unanswered questions, caller consent, whether callback was requested, whether scheduling was requested.
   - Source: live transcript and deterministic controller.
   - Update cadence: every turn.

### B. Receptionist Controller

The model should operate inside a state machine. The controller should decide what information is needed and when it is appropriate to ask:

- It is acceptable to ask the caller's name early so Kevin can address them politely.
- It is not acceptable to ask for callback number until callback, scheduling, or follow-up is relevant.
- If caller ID is available, ask "Is the number ending in 8667 the best callback number?" only after callback intent exists.
- Do not ask for address unless an onsite visit, quote, service dispatch, property-specific diagnosis, or Jobber request creation requires it.
- If Jobber memory already contains likely address or prior property, confirm rather than re-ask.
- Never mention private source names such as Jobber notes unless needed for transparency.

### C. Tool And CRM Rules

Recommended tool sequence:

1. Pre-call lookup: find known customer by caller ID. Timeout under one second.
2. During call: no blocking Jobber writes. Keep conversation moving.
3. Intent confirmed: if the caller wants follow-up, scheduling, or a quote, prepare structured intake.
4. Post-call: create/update Jobber request, attach transcript/note, update local memory, send owner SMS.
5. Retry queue: failed Jobber operations should be retried with idempotency keys and visible admin status.

### D. Evaluation Suite

Create a test suite with at least these scenarios:

- New caller asks "How much to replace a toilet?"
- Known caller with prior sink repair calls for toilet replacement.
- Caller asks for pricing but does not want callback.
- Caller wants callback and caller ID is available.
- Caller wants callback but caller ID is blocked.
- Caller asks a confused phrase such as "toilet replacement in the sink" and Kevin should clarify naturally.
- Caller interrupts Kevin mid-answer.
- Caller asks for an exact quote that the business cannot guarantee.
- Existing Jobber profile has conflicting or stale notes.
- Jobber lookup times out.
- Jobber token is expired.
- Post-call Jobber write fails.

Each scenario should score:

- First response latency.
- Per-turn latency.
- No repeated questions.
- No intrusive callback request.
- Correct use of known memory.
- No unsupported pricing commitment.
- Correct owner-facing summary.
- Correct Jobber outcome or clear failure status.

## Design Position For The Next Discussion

The goal should not be a narrow prompt or a fully trained receptionist model. The goal should be a receptionist system:

- The model handles natural language, empathy, clarification, and extraction.
- The controller handles state, permissions, and next-step eligibility.
- Memory provides context but does not override caller consent.
- Tools perform side effects after intent is clear.
- Logs and evaluations make regressions obvious.
- Fallbacks keep calls from collapsing when a provider or integration fails.

This architecture is more work than a prompt-only system, but it is the direction supported by current voice-agent platforms and by the issues we have seen in staging.

## Near-Term Recommendations

1. Finish staging validation of Jobber memory injection.
2. Add local `customer_memory` storage and post-call merge rules.
3. Introduce an explicit call-state object for known facts and already-asked questions.
4. Convert the prompt from a long list of instructions into:
   - role/personality,
   - state-specific policy,
   - compact business memory,
   - compact customer memory,
   - allowed next actions.
5. Add eval scenarios before further prompt expansion.
6. Add latency and duplicate-question metrics to admin logs.
7. Keep CRM lookups and writes on strict timeouts with retries.

## References

Primary sources checked on 2026-07-09:

- Vapi, "Introduction to Knowledge Bases": https://docs.vapi.ai/knowledge-base
- Vapi, "Personalization with user information": https://docs.vapi.ai/assistants/personalization
- Vapi, "Voice fallback configuration": https://docs.vapi.ai/voice-fallback-plan
- Vapi, "Transcriber fallback configuration": https://docs.vapi.ai/customization/transcriber-fallback-plan
- Vapi, "Debugging voice agents": https://docs.vapi.ai/debugging
- Vapi, "Test Suites": https://docs.vapi.ai/test/test-suites
- Retell AI, "Knowledge base setup and retrieval tuning": https://docs.retellai.com/build/knowledge-base
- Retell AI, "Dynamic variables: personalize Retell agent calls": https://docs.retellai.com/build/dynamic-variables
- Retell AI, "Give your AI agent persistent contact memory": https://docs.retellai.com/integrations/build-contact-memory
- Retell AI, "Reliability Overview": https://docs.retellai.com/reliability/reliability-overview
- Retell AI, "Automatically test your agent": https://docs.retellai.com/test/llm-simulation-testing
- Bland AI, "Conversational Pathways": https://docs.bland.ai/tutorials/pathways
- Twilio, "Media Streams Overview": https://www.twilio.com/docs/voice/media-streams
- OpenAI, "Voice agents": https://developers.openai.com/api/docs/guides/voice-agents
- OpenAI, "Realtime and audio": https://developers.openai.com/api/docs/guides/realtime
- Google Cloud, "Gemini Live API overview": https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api
- LangGraph, "Persistence": https://docs.langchain.com/oss/python/langgraph/persistence
