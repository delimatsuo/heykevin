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

### Pinned caller-heard interruption boundary

Before any capability probe, the hard eligibility threshold for ground-truth
intentional caller-speech onset to the last audible assistant sample is pinned at
p95 at most 750 ms and maximum 1,000 ms. The interval begins at speech onset in
the caller-side capture and ends at the final assistant sample in caller-side
PCMU. Every manifest-labeled intentional interruption is measured. Labeled
backchannels and noise are negative controls and must produce zero false clears.

The threshold is an owner/product engineering rejection budget, not a number
derived from ITU-T and not a claim that the delay is imperceptible or
human-equivalent. Its explicit component allocations are p95 at most 250 ms and
maximum 500 ms from intentional interruption to Twilio `clear`, plus p95 at most
500 ms and maximum 500 ms from clear to the last caller-audible queued sample.
The directly measured end-to-end 750/1,000 ms gate governs; component percentiles
cannot substitute for it.

ITU-T G.114 provides context only: highly interactive speech can be affected at
delays well below its 400 ms one-way network-planning upper bound:
<https://www.itu.int/dms_pubrec/itu-t/rec/g/T-REC-G.114-200305-I!!SUM-HTM-E.htm>.
It does not establish this assistant-interruption cutoff. The later closed-loop
window independently applies an absolute perceived-talk-over `no_winner` gate in
addition to comparison measures.

This threshold cannot be weakened after this decision or after candidate evidence
is visible. If caller-side recording is not separately authorized for a
participant window, the timing gate is evaluated only in the approved synthetic
technical windows; participant reports cannot substitute for the caller-audio
measurement.

## Control and custody

Provider execution requires a separately owner-signed, immutable, one-use envelope:
a single closed owner key, named signer-key provenance/algorithm/key ID/trust-store,
rotation/revocation, no break-glass, a mandatory advisory technical-review receipt
with no unresolved P1, pinned nonproduction identities and destinations, strict caps,
and production-deny technical isolation.
Unknown retention, data sharing, tracing, recording, cache, region, or deletion is
a no-go.

## Consequences

This ADR supersedes no retrospective scope in
[ADR 0001](0001-gemini-retrospective-caller-turns.md): its caller-turn assembly
remains retrospective only. A bakeoff result selects a candidate only; a separately
reviewed winner-specific integration plan and explicit owner authorization are
required before staging or production.
