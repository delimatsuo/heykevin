# Receptionist Controller Shadow Rollout

## Purpose

Shadow mode evaluates the call-scoped receptionist state, planner, and instruction composer on final Gemini caller transcripts. It does not send controller instructions to Gemini, change the system prompt, suppress a response, call a tool, or modify audio handling.

Shadow mode is evidence gathering only. It does not prove that controller policy is active in the caller experience.

## Enablement

Both controls must be enabled:

1. Set the service environment variable `RECEPTIONIST_CONTROLLER_SHADOW_ENABLED=true`.
2. Set `receptionist_controller_shadow_enabled` to the boolean `true` on only the approved staging contractor document.

The account value must be a boolean. A string value such as `"true"` does not enable the controller.

Do not enable the production service during shadow certification. Do not broaden the contractor allowlist until the initial staging calls pass the gates below.

## Expected Telemetry

Each final caller turn should emit one structured event:

```text
voice_event event=controller_shadow_decision call=<short-label> turn_id=<integer> action=<action> elapsed_ms=<ms> known_fact_count=<count> asked_slot_count=<count> allowed_slot_count=<count> forbidden_slot_count=<count> instruction_chars=<count> tool_calls_allowed=<bool>
```

Later caller fragments received before that assistant turn finishes emit
`controller_shadow_caller_amendment` with the same `turn_id`; they update state
without creating a second decision. A completed or interrupted assistant turn
emits `controller_shadow_assistant_turn`. Completed turns commit the pending
controller action's allowed slots; interrupted turns commit none.

These are counterfactual controller transitions. Shadow mode does not claim
that Gemini asked the controller's planned slot because controller instructions
are not sent to Gemini.

The event must not contain caller speech, names, addresses, phone digits, prompt text, memory text, or planner reasons.

An unexpected controller exception emits `controller_shadow_error` with the exception type only. Shadow evaluation is then disabled for that call. Gemini continues on the existing live path.

## Staging Gates

- The exact candidate SHA is reported by `/health`.
- Controller decisions appear only for the allowlisted staging contractor.
- Every final caller turn has at most one decision event.
- Decision and assistant events use the same integer `turn_id`.
- Completed assistant turns commit pending slots; interrupted turns commit zero.
- `controller_shadow_error` count is zero.
- Decision latency remains bounded and does not regress first-audio or response-first-audio release gates.
- Replayed state and planner decisions match the expected scenario outcome.
- No controller-generated `client_content`, tool call, system-prompt mutation, or audio event is present.
- Logs contain no full phone numbers, transcript text, names, addresses, credentials, or private memory source labels.

## Kill Switch

Set `RECEPTIONIST_CONTROLLER_SHADOW_ENABLED=false` and restore the approved staging revision if any privacy, latency, stability, or decision-quality gate fails. The global flag alone disables initialization for all contractors, even if an account flag remains set.

Production activation requires a separate reviewed active-control design, deterministic turn-boundary evidence, repeated staging canaries, rollback rehearsal, and explicit authorization.
