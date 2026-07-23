# ADR 0002: Voice Architecture Bakeoff

- Status: Proposed; documentation gate only
- Date: 2026-07-22
- Baseline: `origin/main` `6dc3013df78070cd60871febb1a541977ea4c3b3`

## Decision

Choose no live architecture yet. Run an isolated, nonproduction bakeoff after
offline gates and one-use provider approval. `MAX_RESPONSE_OUTPUT_TOKENS` is a
runaway guardrail: a ceiling hit is a failed act, not normal response control.

Candidate arms are A native Gemini control, B1 chained streaming reference, B2
ConversationRelay challenger, and C manual-turn native feasibility. Pipecat is an
optional framework, not an arm. Tools, automatic terminal actions, customer data,
production routing, and historical branch-wide pipelines are excluded.

## Shared decision contract

All arms use versioned `VoiceEvent` and `VoiceCommand`, with bounded provenance,
environment, epoch, sequence, call, input-turn, generation, and semantic-act
bindings. Policy, generation, playout, and call lifecycle remain separate owners.
No model wording authorizes a side effect or hangup.

An act records `transport_resolved` separately from `caller_playback_observed`.
Only encrypted caller-side PCMU establishes the latter. A runtime without that
observation may use a preregistered, cancellable `playback_inferred` deadline but
may not claim the caller heard it; transport evidence alone never arms closure.

## Evidence and selection

Evidence tiers are offline/static, bounded synthetic non-scoring probe, sealed
technical selection, and closed-loop consenting-participant acceptance. Exact
source, model/API version, configuration, manifest, evaluator, artifact, and
approval-envelope digests are frozen before sealed evidence.

Hard gates precede weighted scoring: complete thoughts; answer-before-follow-up;
one question; safety completeness; no premature closure or repeated answered slot;
caller-side interruption; privacy/authentication; and evidence integrity. Eligible
arms are then scored 30% semantic task success, 20% caller-heard completeness,
15% latency, 15% interruption/silence/reconnect, 10% language parity, 5% security,
and 5% operations/cost. No winner on insufficient evidence or any hard-gate failure.

## Control and custody

Provider execution requires a separately signed, immutable, one-use envelope:
three independent roles, named signer-key provenance/algorithm/key ID/trust-store,
rotation/revocation, no self-approval or break-glass, pinned nonproduction
identities and destinations, strict caps, and production-deny technical isolation.
Unknown retention, data sharing, tracing, recording, cache, region, or deletion is
a no-go.

## Consequences

This ADR supersedes no retrospective scope in
[ADR 0001](0001-gemini-retrospective-caller-turns.md): its caller-turn assembly
remains retrospective only. A bakeoff result selects a candidate only; a separately
reviewed winner-specific integration plan and explicit owner authorization are
required before staging or production.
