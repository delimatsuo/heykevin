# Voice Architecture Bakeoff Security and Privacy Controls

Status: proposed Stage 0 control annex. No provider execution is authorized.

## Trust boundary

```text
Synthetic/consented caller PCMU -> isolated Twilio resource -> isolated bakeoff app
  -> authenticated token store -> approved provider adapters -> encrypted evidence store
  -> allowlisted aggregate evaluator
```

No production router, credential, datastore, log sink, customer route, tool, write,
terminal action, or callback is in this path. Before `AUTHENTICATED`, only canonical
Twilio signature verification and bounded auth-store operations are allowed.

## Data and provider matrix

`receive` below means a dependency receives that category during an approved run;
`none` means the adapter must prohibit it. Every receiving row is synthetic-only
until the separate consenting-participant gate, and its logging/training/tracing,
retention, region, cache, deletion, account identity, owner, recheck date, and
residue method must be pinned before connection.

| Arm / dependency | Audio | Transcript | Prompts | Metadata | Generated text | Synthesized audio |
| --- | --- | --- | --- | --- | --- | --- |
| A/C Twilio | receive PCMU | none | none | bounded routing only | none | receive return PCMU |
| A/C isolated app | receive PCMU | only transient if enabled | receive authorized config | bounded bindings | none unless authorized output transcript evidence | receive transient native audio |
| A/C Gemini | receive audio | provider-derived only if enabled | receive bounded authorized instructions | bounded session config | provider-generated only | generate |
| B1 Twilio | receive/send PCMU | none | none | bounded routing only | none | receive/send PCMU |
| B1 Deepgram | receive caller audio | generate provider result | none | bounded stream config | none | none |
| B1 text model | none | receive approved typed observation only | receive bounded plan | bounded config | generate bounded act text | none |
| B1 ElevenLabs | none | none | receive authorized text only | bounded voice config | none | generate |
| B2 ConversationRelay | receive/send managed audio | generate prompt | receive text tokens | bounded session config | receive tokens / managed STT | generate managed TTS |
| encrypted evidence store | receive caller-side PCMU only under approved evidence tier | bounded labels only | none | digests only | none | receive caller-side PCMU only |

For every dependency, the approval envelope records provider/API version, endpoint,
account/project/subaccount/region, dedicated credential reference, retention,
training/data-sharing, abuse monitoring, logs/traces, DPA/subprocessor, recording,
cache/resumption and deletion setting. Each row has source/control-plane evidence,
owner, expiry/recheck date, and residue-verification method. Unknown is no-go.

## Isolation, telemetry, and side effects

Dedicated nonproduction accounts, principals, quotas, KMS keys, token store, log
sink, Twilio resources, integration sandbox, egress list, and evidence location are
required. None may reach production technically. Logs emit only HMAC pseudonym,
candidate, bounded enums/counts/ordinals/durations/error classes; never transcript,
audio, generated wording, phone, raw session ID, tool data, credential, callback
code, or exception text.

All tools, writes, notifications, transfers, automatic terminal actions, recording,
Voice Trace, request/response logging, tracing, and data sharing are disabled.
Side-effect paths remain governed by `phase0-side-effect-matrix.md`; this annex adds
no permissive row.

## Approval and execution

Execution is dry-run by default. A connected run needs a one-use immutable envelope
with staff, security/privacy, and product signatures from distinct identities. The
trust store pins signer-key provenance, algorithm, key ID, rotation/revocation and
immutable-store access. Self-approval and break-glass execution are prohibited.
The runner verifies everything before DNS, credentials, sockets, PSTN, or provider
construction, then atomically consumes the nonce and creates the bound auth record.

Caps cover requests, concurrency, duration, bytes, audio, retries, tokens, spend,
and artifact TTL. Stop triggers revoke credentials/tokens, drain tasks, close
sessions, verify no residue in providers/logs/stores/caches/backups, and retain only
aggregate receipts. Participant consent is explicit; withdrawal halts access and
requires deletion receipts for evidence, caches, exports, backups, and derivatives.
