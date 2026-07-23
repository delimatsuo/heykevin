# Caller-UX acceptance contract

**Status:** unsealed template. It does not authorize probing, caller testing, or
provider execution, and it makes no support claim for an unlisted language or
access mode.

## Required owner decisions before sealing

- Finite qualified languages and code-switch pairs, plus truthful unsupported
  language fallback.
- Accessibility matrix; TTY, RTT, and DTMF are unsupported unless expressly
  qualified. DTMF never bypasses authentication or lifecycle controls.
- Evidence-backed numerical thresholds for dead air, interruption, coherence,
  task completion, sampling, power, and all hard gates.
- Caller-heard definitions and thresholds for intentional caller-speech onset to
  last audible assistant sample, missed interruption, false clear from
  backchannel/noise, and post-interruption coherence.
- Per-language rater fluency, per-code-switch dual-fluency, adjudication, and
  reliability handling for every declared cohort.
- The finite allowed repair-act and deterministic-fallback catalog.
- Consent, withdrawal, recording-default, participant access, retention,
  deletion, and blinded-rater protocol.

## Failure and repair contract

Recoverable generation, STT, TTS, playout, and reconnect failures permit at most
one repair act from the sealed allowed catalog. It preserves confirmed facts only
and uses its paired bounded, truthful fallback. Uncertain external state, security/privacy failure, or
irrecoverable transport fails closed without retry or implied spoken repair.

Every deadline, threshold, and fallback identifier must be bound to an approved
evidence source before sealing. A transport mark, queue receipt, generation
completion, or final token is not evidence that the caller heard audio.

## Closed-loop evaluation template

The sealed version must define the participant sampling/power rule,
counterbalancing, handset/network matrix, no-tuning rule, and hard gates for:

- whole-call task completion and direct-answer relevance;
- one-question maximum and pending-question comprehension;
- caller-heard complete thought, interruption response, and post-interruption
  coherence;
- repeat, slower speech, more time, opt-out, voicemail, unsupported language,
  and unsupported access-mode behavior;
- privacy, safety-content completeness, and truthful refusal behavior.

Insufficient evidence, a failed privacy approval, or any failed hard gate yields
`no_winner`; it cannot be waived by averages, post-hoc pooling, or tuning.
