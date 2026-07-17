# ADR 0002: Monitored Pilot Observation Shadow

- Status: Accepted for implementation; staging enablement remains separately gated
- Date: 2026-07-17
- Decision owner: Hey Kevin maintainer
- Scope: Synthetic, test-restricted staging diagnostics only

## Context

ADR 0001 correctly established that Gemini input transcription has no provider
finality marker and that opportunistic transcript flushes are not caller turns. Its
offline contracts and fail-closed terminology remain valid. The later Gate 0B
qualification design added cryptographic custody and sealed-provider execution that
is disproportionate to the current product goal: a monitored pilot with bounded
staging evidence and fast rollback.

The product still needs direct evidence about Gemini event ordering before semantic
extraction or controller decisions can be considered. Reusing the frozen PR #87
shadow is not acceptable because it sends raw flushed transcript text directly into
controller state and assumes a flush is a completed turn.

## Decision

Add a separate stacked branch containing a staging-only observation sidecar:

```text
existing Gemini server message
  -> nonblocking bounded enqueue
  -> GeminiTurnEventAdapter
  -> CallerTurnEvent
  -> CallerTurnAssembler
  -> payload-free operational metrics
```

The sidecar observes messages already received by the live staging session. It does
not open another provider connection, make another model request, alter Gemini
configuration, or delay the live receive loop. Queue saturation or any sidecar
error drops diagnostic work and leaves the live call unchanged.

This decision supersedes only ADR 0001's prohibition on importing the event adapter
and assembler from the Gemini live path for this narrowly gated staging diagnostic.
It does not finalize ADR 0001's proposed turn policy or authorize the archived Gate
0B runner.

## Hard Boundaries

The sidecar may initialize only when every condition is true:

1. `ENVIRONMENT` is exactly `staging`.
2. The global observation-shadow flag is the boolean `true`.
3. The contractor observation-shadow flag is the boolean `true` and is protected
   from client profile updates.
4. The contractor authorization has a future expiry within the configured maximum
   window.
5. The caller identifier matches a contractor allowlist using a dedicated HMAC key;
   raw caller identifiers and allowlist digests are never logged.

Production startup must reject an enabled global observation-shadow flag. The
production deployment workflow must force the flag off and must not mount the HMAC
key. The account flag and expiry provide a per-call dynamic kill switch; disabling
either prevents initialization on the next call. Disabling the global flag and
rolling back the exact staging revision remain the service-wide recovery path.

Only the maintainer's consented test calls using synthetic scripts are authorized.
Customer calls, production calls, existing call recordings, stored transcripts,
and CRM data are prohibited.

## Data And Isolation

- Raw Gemini messages and transcript fragments remain in memory only long enough
  for bounded reduction. They are never persisted or logged.
- The queue is bounded and uses nonblocking insertion. Full queues increment only a
  payload-free drop counter.
- Reports contain status enums, close-reason enums, counts, queue depth, bounded
  timing, epoch, and call-local turn IDs only.
- Reports contain no transcript text, audio, names, phone digits, addresses, call
  SID, contractor ID, prompts, model output, tool arguments, exception text,
  credentials, or private-memory labels.
- Sidecar code must not import `receptionist_state`, `dialogue_planner`,
  `instruction_composer`, Jobber services, post-call services, or tool executors.
- The sidecar cannot send client content, audio, tool responses, clear events, or
  websocket frames and cannot mutate the system prompt or post-call payload.

## Evidence Taxonomy

Observation telemetry never implies controller or release readiness. Until later
reviewed gates complete, every evidence package must preserve these values:

```json
{
  "turn_assembly_validated": false,
  "semantic_extraction_validated": false,
  "shadow_operational_isolation_validated": false,
  "caller_experience_neutrality_validated": false,
  "active_control_validated": false,
  "provider_execution_authorized": false,
  "real_caller_data_authorized": false,
  "staging_authorized": false,
  "production_authorized": false,
  "release_authorized": false
}
```

`staging_authorized` becomes true only in the separately recorded staging
enablement artifact for one exact SHA, contractor, caller HMAC digest, and expiry.
It is not changed by code review, merge, or deployment alone.

## Implementation Gates

Before the sidecar branch may be deployed to staging:

- unit tests prove production hard denial, exact boolean checks, expiry bounds,
  HMAC caller matching, queue bounds, lifecycle teardown, and payload-free logs;
- tests prove no prompt, websocket, tool, persistence, post-call, or controller
  mutation;
- full unit tests, changed-file Ruff, diff checks, and secret/PII scans pass;
- an independent review approves the exact SHA;
- the base reliability PR remains independently reviewable.

Before the sidecar may be enabled in staging:

- the exact deployed SHA and prior rollback revision are recorded;
- a short-lived contractor authorization is written through an operator-only path;
- only the dedicated staging HMAC key is mounted;
- paired shadow-off and shadow-on synthetic calls are scripted;
- first-audio, response-first-audio, interruption-clear, audio-gap, event-loop-lag,
  queue-drop, worker-error, and teardown metrics are available.

Any latency regression, queue drop, worker error, payload leak, cross-turn evidence,
or rollback failure blocks semantic extraction and controller work.

## Deferred Decisions

This ADR does not authorize:

- semantic `CallerObservation` extraction;
- applying any observation to `IntakeState`;
- `DialoguePlanner` or `InstructionComposer` execution in a live call;
- prompt updates, pre-response control, tool gating, or side effects;
- a provider or model change;
- customer-data processing;
- production deployment or canary calls.

A later ADR may authorize read-only controller decisions only after the observation
boundary and operational isolation pass their staging gates. Active control requires
its own design and explicit production authorization.

## Consequences

This approach gathers direct staging evidence with a smaller operational and data
surface than the archived qualification system. It cannot prove same-turn control
or fix repeated questions. It intentionally delays controller shadow decisions
until the event boundary is credible.

## References

- `docs/adr/0001-gemini-retrospective-caller-turns.md`
- `docs/superpowers/plans/2026-07-14-typed-receptionist-observation-qualification.md`
- `docs/voice-enterprise-release-gates.md`
