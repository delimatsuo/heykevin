# Relay message-taking request state

The owner reported repeated questions after Take a message during a September
16 phone test. Bounded request metadata identifies that test as build 40:
the action received HTTP 202, followed by successful status reads. Build 41,
already available internally, addresses the separate false confirmation
warning. Neither the screenshot nor those requests establish an app crash.

The active ConversationRelay backend supplies the owner transition as a
synthetic conversation message while leaving its system instruction in the
original screening mode. The instruction is present, but its authority is not
represented in the request's system context. This is a candidate explanation
for the repeated question, not proof of the model's internal reasoning.

## Bounded correction

Add application-owned message-taking state to the actual Gemini request's
system instruction during the existing guarded transition and subsequent
message-taking replies. Keep the original system prompt and caller history.
Only the existing private delivery state can activate this context; caller
speech, including text that claims to be a system instruction, cannot do so.

The accepted behavior remains:

> Take a message must let Kevin's currently playing speech finish.

> Preserve transcription, caller interruption and subsequent conversation.

The new request contract is:

- The guarded transition supplies owner unavailability as system context,
  overrides the normal availability-check/intake sequence, and offers to take a
  message in the caller's language. If the caller already gave a message,
  acknowledge it without asking for it again.
- Later replies retain the unavailable state, use the caller's latest words,
  and avoid restarting availability checks or repeating answered questions.
- Normal calls retain their existing request body. Rejected or failed
  transitions do not leave message-taking system state on an ordinary reply.
- Caller words remain in conversation history, never promoted into trusted
  system state. Tool-call parts and signatures remain intact.
- Temporary transition state clears even after cancellation following partial
  output. Persistent guidance applies to responses to caller speech and permits
  the existing silence check and goodbye without re-answering old content.

The request uses the existing text-only system-instruction field documented in
the [Gemini streaming API](https://ai.google.dev/api/generate-content#method:-models.streamgeneratecontent).
It does not migrate the model or API.

No change to speech completion, three-second pause, 30-second timeout,
ownership checks, acknowledgement, iOS, provider/model selection, dependencies,
flags, or deployment workflows is included. Other engine hypotheses remain
outside this correction.

## Verification and release boundary

Use fictional fixtures and a local HTTP transport to capture real generated
request envelopes through the durable action consumer. Cover transition,
continuation, caller text, rejected ownership, failed delivery/retry, and tool
rounds. Existing timing and lifecycle tests must remain green. Compare the new
tests with the baseline and mutate the new state guards to establish that the
assertions detect removal or unauthorized activation.

Local request tests establish instruction placement and state isolation; they
do not prove Gemini's spoken response or physical-call acceptance. An
independent review and exact-HEAD required CI precede merge. Staging and
production deployment require a new owner approval after the reviewed result
is concrete. No new iOS build or upload is part of this change.

## Local verification, September 16

- All 260 focused tests passed: the eight new HTTP-envelope tests plus 252
  existing Relay, owner-action, transition, legacy-command, urgent-handoff and
  ConversationRelay protocol tests. No tests failed or skipped. Four existing
  dependency/deprecation warnings were reported. Fatal Ruff checks, compilation
  and whitespace checks passed.
- Against the unchanged baseline implementation at
  `49bcc08bdff2cb2a0782e431cb03225e7fac6c23`, the new tests produced six expected
  failures and two passing controls. This demonstrates the request-contract
  change and cancellation cleanup, not a reproduction of model speech.
- Six separate import-time mutations each failed the intended assertion:
  removal of transition context; removal of continuation context; promotion of
  caller text to system state; conditional cleanup after cancellation;
  assignment before the cancellable supersede wait; and removal of the initial
  Spanish language cue. The source file was never modified by these probes.
- Independent source and test review returned no remaining findings. Reviewed
  SHA-256 values are `3300e2d1ad0d12252931df7dfa69a69952d8484f1282923549a1d612c9f89587`
  for the Relay source and
  `ba0d798ced8fc32ac23fef86e9cdc40c69b98f718805076b0e384d93838705e5`
  for the new tests. Exact-HEAD CI and automatic review are recorded in the PR.

The master rejected six initial test/evidence gaps and had the builder repair
them before this final run: standalone fictional configuration, missing tool
declarations, legacy-adapter isolation, a retry that did not actually fail ACK,
guard rejection before temporary state was set, and manually seeded
continuation/silence state. No fixture result is described as a phone result.

Master model: GPT-6 (Codex)
Builder model/tier: gemini-3.7-flash-high
Routing reason: pinned implementation and transport-test requirements; master-owned design, audit, verification and Git
agy transport=headless: true
Input tokens: 414132
Output tokens: 108825
Total tokens: 522957
Retries: 1 fixture revision; two successful headless invocations, no transport retry
Audit defects found: 6 initial test/evidence gaps, repaired as listed above
Audit disposition: pass after remediation and independent review

Usage values are the sums reported by the two builder invocations, not
estimates. Local checks use `KEVIN_DISABLE_DOTENV=1` and fictional fixtures.

Repository and billing owner are `delimatsuo/heykevin` and `delimatsuo`.
The public-repository PR runs seven standard Ubuntu shards, Python quality and
the stable fail-closed `Test` aggregator. Recent durations imply about eleven
rounded runner-minutes per candidate (roughly $0.066 gross reference cost),
with $0 expected chargeable cost. Feature pushes and main merges do not deploy;
no macOS job or workflow dispatch is included. September paid account Actions
usage observed during preflight was $71.470765035, with Hey Kevin net usage $0.
No paid-CI allocation or deployment approval is implied by this record.
