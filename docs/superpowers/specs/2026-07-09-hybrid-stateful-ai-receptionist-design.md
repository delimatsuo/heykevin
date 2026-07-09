# Hybrid Stateful AI Receptionist Design

Date: 2026-07-09
Status: Approved strategy for implementation planning
Scope: Hey Kevin business-mode AI receptionist architecture

## Goal

Make Kevin behave like a resilient AI receptionist by moving control out of accumulated prompt rules and into Hey Kevin-owned memory, call state, planning, tool orchestration, and evaluations.

Kevin should keep the natural low-latency voice experience of Gemini Live, but the product should no longer rely on one long static prompt to decide what is known, what has already been asked, what is safe to ask next, or when side effects should run.

## Context

PR #76 adds read-only Jobber customer memory for Gemini Live. Staging validated that Jobber memory can load and be injected into the live call, but the latest inspected call still showed Kevin asking a repeated service-action question after the caller had already said they wanted a replacement.

Commit `384ac99` added a narrow prompt guardrail for that repeated-question symptom. That commit is acceptable as a short-lived staging mitigation, but it is not the desired architecture. The next work should supersede prompt-rule accumulation with explicit state and planner behavior.

Independent research supports this direction:

- OpenAI frames voice-agent architecture as a choice between speech-to-speech sessions for natural low-latency conversation and chained pipelines for predictable workflows where the app controls intermediate text and logic.
- Gemini Live provides the realtime voice layer Kevin needs: WebSocket sessions, barge-in, transcripts, tool use, proactive audio, and multilingual voice behavior. It is still described as Preview, while Gemini Interactions API is now GA for new Gemini model and agent work.
- Retell, Vapi, and Bland all expose structured conversation flows, knowledge bases, dynamic variables, post-call analysis, and automated simulations as core voice-agent product primitives.
- Agent-memory research treats memory as a write, manage, read loop, not as unbounded chat history. Agent evaluation research emphasizes planning, tool use, memory, safety, cost, and robustness rather than response text alone.

## Strategy

Use a hybrid stateful architecture:

1. Keep Gemini Live as the primary realtime audio layer for low first-audio latency, natural turn-taking, barge-in, multilingual speech, and caller interruption.
2. Add a Hey Kevin-owned receptionist controller that tracks call state and decides the next allowed action.
3. Feed Gemini compact, per-turn operating context generated from explicit state instead of continuing to grow a static prompt rule list.
4. Store durable customer memory locally, with Jobber as one source rather than the only source.
5. Move CRM writes and memory updates to post-call jobs with idempotency and retries.
6. Build replay and simulated-caller evaluations before further expanding live-call behavior.

This preserves the live voice quality while giving the backend deterministic control over the product behavior that matters: no repeated questions, no intrusive callback collection, no address collection before it is relevant, no unsupported quote commitments, and no unsafe or unauthorized tool side effects.

## Non-Goals

- Do not train a custom receptionist model.
- Do not rewrite the full telephony stack.
- Do not replace Gemini Live before proving the controller design.
- Do not add another broad prompt-rule pass as the primary fix.
- Do not expand PR #76 into scheduling, booking, or Jobber writes unless the user explicitly chooses to broaden that PR.
- Do not expose Jobber OAuth tokens, callback codes, admin bearer tokens, or full phone numbers in logs, docs, prompts, or eval fixtures.

## Architecture

The system should be split into six units with clear ownership.

### 1. Live Voice Adapter

Responsibility: maintain the realtime audio session and provider-specific protocol.

For the current implementation, this is Gemini Live over the existing Twilio Media Streams bridge. It handles setup, audio input, audio output, transcripts, barge-in, session errors, tool-call messages, and reconnect behavior.

The adapter should not decide receptionist policy. Its boundary should be:

- Input: audio and text events from Twilio/Gemini.
- Output: caller transcript events, Kevin transcript events, tool-call events, session lifecycle events.
- Commands accepted from controller: update instructions, send text turn, allow response, suppress response, close session, reconnect session.

Provider details such as Gemini system-instruction updates, tool-call response format, session resumption, and context-window compression should stay inside this adapter.

### 2. IntakeState

Responsibility: represent what Kevin knows during one call.

`IntakeState` is the call-scoped memory. It should be serializable, loggable with redaction, and replayable in tests.

Core fields:

- `phase`: `greeting`, `understand_request`, `answer_question`, `clarify_scope`, `collect_intake`, `offer_followup`, `schedule_or_callback`, `handoff`, `wrap_up`
- `caller_identity`: known name, confidence, source, and whether the caller confirmed it
- `caller_phone`: available last four only for prompts and logs
- `business_scope`: in scope, out of scope, unclear, with reason
- `intent`: pricing question, service request, emergency, personal call, sales call, message, scheduling, callback, unknown
- `service_object`: fixture, appliance, room, system, or property item mentioned by the caller
- `service_action`: repair, replace, install, inspect, quote, maintain, unknown
- `urgency`: emergency, urgent, routine, unknown
- `known_facts`: concise normalized facts captured from this call
- `asked_slots`: set of slots already asked in this call
- `callback_intent`: none, offered, accepted, requested, declined
- `address_need`: none, maybe_later, required_now, already_known, confirmed
- `memory_refs_used`: IDs of business or customer memory facts used in the call
- `side_effects_allowed`: whether Jobber writes, SMS, transfer, or scheduling actions are allowed

`IntakeState` should be updated from transcript events and selected tool results. It should not depend on the model remembering prior turns correctly.

### 3. MemoryStore

Responsibility: own durable memory that survives calls.

Memory has three layers.

Business memory:

- Source: contractor settings, services, pricing, knowledge base, business hours, service area, emergency policy, owner preferences.
- Update rule: owner-controlled or admin-controlled. Do not auto-learn business policy from callers.
- Prompt form: compact business memory card with only relevant services and policies.

Customer memory:

- Source: Jobber lookup, local post-call extraction, prior Hey Kevin calls, owner-confirmed corrections.
- Update rule: post-call extraction plus deterministic merge policy.
- Prompt form: compact private customer memory card, never raw CRM dumps.

Call-state memory:

- Source: live transcript and planner updates.
- Update rule: every caller/Kevin turn.
- Prompt form: current state summary and allowed next action.

Customer memory records should include:

- contractor ID
- normalized phone identity keys
- display name and confirmation status
- known properties with source, confidence, and last seen
- prior services and open issues
- callback and language preferences
- recent commitments and unresolved follow-ups
- source records from Jobber or Hey Kevin calls
- sensitivity labels
- stale/conflict markers

Merge policies:

- Stable facts such as first name and property address: fill if empty, require confirmation before overwriting high-confidence values.
- Status fields such as open issue or latest request: overwrite with latest credible source.
- Free-text summaries: merge sparingly, deduplicate, keep newest interaction prominent, and preserve unresolved commitments.
- Conflicts: keep both facts with source and confidence, and prompt Kevin to confirm only when relevant.

### 4. DialoguePlanner

Responsibility: choose what Kevin is allowed to do next.

The planner consumes `IntakeState`, memory cards, business policy, and the latest caller turn. It emits a `NextAction`.

Representative actions:

- `answer_direct_question`
- `ask_name`
- `ask_one_clarifying_question`
- `confirm_known_property`
- `ask_urgency`
- `offer_photo_link_after_call`
- `offer_callback_or_scheduling`
- `confirm_callback_last_four`
- `take_message`
- `try_live_owner_transfer`
- `wrap_up`
- `decline_out_of_scope`
- `safety_guidance`

Each `NextAction` includes:

- action name
- reason
- allowed slots to ask
- forbidden slots to ask
- memory facts safe to use
- maximum spoken shape, such as answer plus one question
- whether tool calls are allowed

The planner enforces the high-risk policies that should not live only in prose:

- Never ask for a slot already in `asked_slots` unless the caller contradicted or corrected it.
- Never ask "repair, replacement, or installation" if `service_action` is already known.
- Never ask which fixture/object if `service_object` is already known.
- Never request or confirm callback number until callback, scheduling, booking, or follow-up intent exists.
- Never ask for service address unless onsite service, dispatch, quote, emergency, scheduling, or Jobber request creation makes address relevant.
- Confirm known memory rather than re-asking it when the memory is relevant and confidence is high.
- Keep side effects disabled until caller intent is explicit.

### 5. InstructionComposer

Responsibility: translate state and planner output into short model-facing instructions.

This is where natural language belongs. The composer should produce compact per-turn context rather than one large static rule list.

Instruction shape:

1. Role: Kevin is the business phone assistant.
2. Current state: what is known and what is not.
3. Private memory: compact memory cards, only if relevant.
4. Allowed next action: one action from the planner.
5. Forbidden repeats: derived mechanically from `asked_slots` and known facts.
6. Speaking style: brief, natural, one question max.
7. Safety/tool constraints: only what applies to this turn.

Example:

```text
Current state:
- Caller is Jonathan, matched from customer memory. Do not mention Jobber.
- Caller asked about replacing a toilet.
- Service object: toilet. Service action: replace.
- Callback intent: none.
- Address is not needed yet.

Allowed next action:
- Answer the pricing/scope question briefly, then ask one useful next detail about the replacement.

Do not ask:
- Whether this is repair, replacement, or installation.
- Which fixture this is.
- Callback number.
- Service address.
```

The model still handles natural language, warmth, multilingual speech, and phrase choice. The controller decides eligibility.

### 6. ToolOrchestrator

Responsibility: control external reads and writes.

Pre-call:

- Start customer lookup by caller phone immediately.
- Use a strict latency budget under one second.
- If Jobber/local memory is slow or unavailable, continue without it.
- Never inject memory unless phone matching or identity confidence is sufficient.

During call:

- Allow low-risk read-only tools only if they do not block conversation.
- Avoid Jobber writes during normal conversation.
- Do not let the model directly decide side effects.

Post-call:

- Extract structured job card, memory update candidates, call summary, and eval signals.
- Create or update Jobber request only after caller intent and required fields are clear.
- Attach transcript/note where allowed.
- Update local `customer_memory`.
- Send owner SMS.
- Queue retries with idempotency keys for failed Jobber writes.

## Data Flow

### Inbound Call Startup

1. Twilio routes the call to Hey Kevin.
2. Backend resolves contractor by Twilio number and subscription state.
3. Backend starts the Live Voice Adapter.
4. Backend starts local memory and Jobber memory lookup in parallel.
5. `MemoryStore` returns a compact memory card or no memory within budget.
6. `IntakeState` initializes with caller ID last four, contractor profile, after-hours state, and any trusted memory.
7. `DialoguePlanner` emits the greeting action.
8. `InstructionComposer` sends concise setup and greeting instructions to the live model.

### Per-Turn Loop

1. Caller transcript event arrives.
2. `StateUpdater` extracts facts from the new caller turn and updates `IntakeState`.
3. `DialoguePlanner` computes the next allowed action.
4. `InstructionComposer` generates short model-facing instructions for that action.
5. Live Voice Adapter sends the instruction to Gemini Live.
6. Kevin responds.
7. Kevin transcript event updates `asked_slots`, action history, and latency metrics.

### Call End

1. Transcript finalizes.
2. Post-call extraction produces call summary, job card, memory candidates, and eval observations.
3. `MemoryStore` merges durable memory candidates according to field-specific policies.
4. `ToolOrchestrator` creates or updates Jobber records if allowed and complete.
5. Owner notification includes concise summary and integration status.
6. Eval harness can replay the transcript and planner decisions.

## Error Handling

Memory lookup failure:

- Continue the call without customer memory.
- Log provider, timeout/error class, and elapsed time.
- Do not tell the caller that memory failed.

Conflicting memory:

- Do not pick an arbitrary fact silently.
- Keep both facts in memory with source and confidence.
- Ask a confirmation only when the conflict matters to the current action.

Gemini Live disconnect:

- Reconnect through the Live Voice Adapter.
- Rebuild session context from `IntakeState`, not from raw unbounded transcript.
- If provider session resumption is available, use it as an optimization, not the only recovery path.

Tool call failure:

- Return a safe conversational fallback.
- Queue retry for post-call side effects.
- Include integration failure status in owner/admin surfaces without exposing tokens or sensitive raw payloads.

Planner uncertainty:

- Emit `ask_one_clarifying_question`.
- Do not collect callback number or address as a fallback unless current state makes those slots relevant.

Eval failure:

- Block broad prompt expansion.
- Add or update the scenario fixture first, then adjust planner/state behavior.

## Observability

Every call should emit structured, redacted events for:

- first audio latency
- per-turn model latency
- memory lookup latency and source
- memory card included or skipped
- planner action chosen
- slots asked
- slots known before asking
- duplicate-question prevention
- callback/address gating
- tool calls requested, allowed, blocked, succeeded, failed
- Jobber write queued, succeeded, failed, retried
- Gemini disconnect/reconnect events

Use last-four phone references only. Never log full phone numbers, OAuth codes, Jobber tokens, or admin bearer tokens.

## Evaluation Strategy

Add a repeatable eval suite before expanding behavior.

Required scenarios:

- New caller asks how much to replace a toilet.
- Known caller with prior sink repair calls for toilet replacement.
- Caller asks for pricing but does not want callback.
- Caller wants callback and caller ID is available.
- Caller wants callback but caller ID is blocked.
- Caller gives a confused phrase such as "toilet replacement in the sink".
- Caller interrupts Kevin mid-answer.
- Caller asks for an exact quote the business cannot guarantee.
- Existing Jobber profile has conflicting or stale notes.
- Jobber lookup times out.
- Jobber token is expired.
- Post-call Jobber write fails.

Each scenario should score:

- no repeated questions
- no intrusive callback request
- no premature address request
- correct use of known memory
- no private source disclosure
- no unsupported pricing commitment
- correct next action from planner
- acceptable first-audio and per-turn latency
- correct owner summary
- correct Jobber outcome or visible retry/failure status

Testing levels:

- Unit tests for `IntakeState`, `DialoguePlanner`, memory merge policies, and instruction composition.
- Transcript replay tests using staging transcripts.
- Simulated-caller tests for expected behavior across several attempts.
- Live staging calls only after replay and simulation pass.

## Rollout

Phase 0: Preserve PR #76 as the read-only Jobber memory slice.

- Do not add more ad hoc prompt rules.
- Keep or revert `384ac99` as a release-management decision, but do not treat it as architectural progress.
- Ensure staging evidence remains documented.

Phase 1: Add state and eval skeleton.

- Introduce serializable `IntakeState`.
- Add transcript replay fixtures for the known repeated-question failure.
- Add planner unit tests for duplicate slots, callback gating, and address gating.
- Do not change live-call behavior until tests define the target behavior.

Phase 2: Add planner-driven instruction composition.

- Generate per-turn instructions from state.
- Keep Gemini Live as the voice layer.
- Reduce static prompt scope to stable role, security, language, and style constraints.
- Compare replay behavior before live deploy.

Phase 3: Add local customer memory.

- Store durable customer memory independent of Jobber.
- Merge Jobber memory and Hey Kevin post-call memory into compact cards.
- Track source, confidence, stale markers, and sensitivity.

Phase 4: Move side effects to resilient post-call orchestration.

- Gate Jobber writes behind explicit intent and required fields.
- Add idempotent retry queue.
- Surface integration status in owner/admin outputs.

Phase 5: Expand tools and provider options.

- Evaluate Gemini Interactions API for non-realtime post-call workflows and agentic back-office tasks.
- Keep Live Voice Adapter provider-abstracted so OpenAI Realtime, Gemini Live, or another realtime layer can be swapped without rewriting receptionist policy.

## Design Decisions

1. Hey Kevin owns state and planning.
   - Rationale: prompts cannot reliably enforce eligibility, memory use, and repeated-question prevention across live calls.

2. Gemini Live remains the current voice layer.
   - Rationale: it provides the realtime call experience Kevin needs today. Provider-specific details stay behind an adapter because Live API status and capabilities are still moving.

3. Memory is compact and layered.
   - Rationale: raw CRM context creates privacy, latency, and prompt-quality risks. Memory cards should be selected, bounded, and source-aware.

4. Side effects happen after intent is clear.
   - Rationale: callers should not experience blocking CRM writes during basic intake, and accidental Jobber writes are harder to recover than missed post-call retries.

5. Evals gate future prompt and planner changes.
   - Rationale: live-call anecdotes are useful but insufficient. The product needs replayable checks for the failure modes already seen in staging.

## Source Research

- OpenAI Voice Agents: https://developers.openai.com/api/docs/guides/voice-agents
- OpenAI Realtime and Audio: https://developers.openai.com/api/docs/guides/realtime
- OpenAI Agents SDK: https://developers.openai.com/api/docs/guides/agents
- OpenAI Agents SDK Tracing: https://openai.github.io/openai-agents-python/tracing/
- Gemini Live API Overview: https://ai.google.dev/gemini-api/docs/live-api
- Gemini Live Session Management: https://ai.google.dev/gemini-api/docs/live-api/session-management
- Gemini Live Tool Use: https://ai.google.dev/gemini-api/docs/live-api/tools
- Gemini Interactions API: https://ai.google.dev/gemini-api/docs/interactions-overview
- Twilio Media Streams: https://www.twilio.com/docs/voice/media-streams
- Retell Conversation Flow: https://docs.retellai.com/build/conversation-flow/overview
- Retell CRM Field Mapping: https://docs.retellai.com/integrations/crm-mappings
- Retell Post-Call Analysis: https://docs.retellai.com/features/post-call-analysis-overview
- Retell Simulation Testing: https://docs.retellai.com/test/llm-simulation-testing
- Vapi Test Suites: https://docs.vapi.ai/test/test-suites
- Vapi Voice Testing: https://docs.vapi.ai/test/voice-testing
- Vapi Server URLs: https://docs.vapi.ai/server-url
- Bland Conversational Pathways: https://docs.bland.ai/tutorials/pathways
- LangGraph Persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- Memory for Autonomous LLM Agents: https://arxiv.org/html/2603.07670v1
- Generative Agents: https://arxiv.org/abs/2304.03442
- MemGPT: https://arxiv.org/abs/2310.08560
- Understanding the Planning of LLM Agents: https://arxiv.org/abs/2402.02716
- A Survey on Evaluation of LLM-based Agents: https://arxiv.org/html/2503.16416v2
- Toward Efficient Agents: https://efficient-agents.github.io/
