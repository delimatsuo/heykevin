# Voice Architecture Bakeoff Security and Privacy Controls

Status: Task 3.4A non-executable offline contract preparation. Task 3.4 remains
incomplete. No provider execution, participant activity, manifest sealing,
credential resolution, network access, Task 4.8, staging, or production action is
authorized.

The current repository runner performs local shape and digest preflight only. It
does not cryptographically verify a signature, consult a real trust store, prove
immutable custody, persist or consume a nonce, resolve a credential, attest an
account, construct a provider, or open a network connection. The CLI intentionally
rejects `--execute-provider`; the approval schema remains
`x-execution: unsupported`, and the manifest remains `template_only`.

## Task 3.4A production firewall

This slice is reviewed relative to
`0af3f564cff5d56c98b31f4c90de8e24389c9a4e`. Its exact tracked edit allowlist is:

- `docs/adr/0002-voice-architecture-bakeoff.md`;
- `docs/voice-architecture-caller-ux-acceptance.md`;
- `docs/security/voice-architecture-bakeoff-controls.md`;
- `scripts/voice_bakeoff_caller.py`;
- `tests/unit/test_voice_bakeoff_caller.py`;
- `tests/unit/test_run_voice_architecture_bakeoff.py`.

No `app/**`, package initializer, dependency or lock file, CI, deployment,
environment, routing, provider schema, manifest, or unrelated script/test edit is
permitted in Task 3.4A. The untracked handoff files are excluded from staging.
That historical Task 3.4A scope does not govern the separately reviewed Task 4.8
offline-only execution-firewall contract below, which adds exactly
`app/services/voice_bakeoff_execution_firewall_contracts.py` and its focused unit
test. It is not connected to the runner or application and does not modify the
Task 3.4A dry-run behavior.
The current runner and harness sources are digest-pinned. Their exact AST import,
dynamic-call, `getattr`, and filesystem-I/O contracts reject additions of
provider/network/credential SDKs, dynamic loading, builtins indirection,
`exec`/`eval`, URL/DNS/socket calls, environment/credential reads, and process
calls. The only subprocess reference is the AST-exact
`git -C <repo> rev-parse HEAD` source-SHA read. A separate test proves
`--execute-provider` is rejected during argument parsing before any local input,
subprocess, harness, secret, or network authority can be reached.

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

The shipped runner is dry-run-only, not merely dry-run by default. A future
connected runner cannot be added by dependency injection, environment variable,
plugin, dynamic import, or a test fake. Implementations and fakes for future
execution authority remain outside shipped composition until a separately
approved exact-SHA change.

Future connected execution requires all of the following, in this irreversible
order:

1. verify canonical envelope encoding and self-digest;
2. cryptographically verify the sole owner's signature against a provenance-pinned,
   current, non-revoked trust store, and verify the envelope-bound advisory
   technical-review receipt has trustworthy provenance and no unresolved P1;
3. prove immutable custody and exact source, manifest, artifact, configuration,
   evaluator, dependency, destination, and cap bindings;
4. atomically consume the durable one-use nonce and create the active execution
   record;
5. resolve only the approved dedicated nonproduction credentials;
6. attest the actual account, subaccount/project, region, endpoint, privacy
   posture, and production-access denial for every dependency;
7. allow a bounded workload request only through candidate-specific capabilities.

Any failure after nonce consumption permanently consumes that nonce, revokes the
active record, tears down, audits residue, and emits only a bounded receipt.
Self-approval, identity reuse across roles, break-glass execution, credential
substitution, an omitted dependency, and an unknown or drifting provider setting
are prohibited.

Caps cover requests, concurrency, duration, bytes, audio, retries, tokens, spend,
and artifact TTL. Stop triggers revoke credentials/tokens, drain tasks, close
sessions, verify no residue in providers/logs/stores/caches/backups, and retain only
aggregate receipts. Participant consent is explicit; withdrawal halts access and
requires deletion receipts for evidence, caches, exports, backups, and derivatives.

Offline cap accounting treats a declared maximum as inclusive: a reservation
exactly at the cap is allowed, and cap plus one is rejected atomically. This local
behavior does not prove a future provider-side quota or spend control.

## Reference-only pre-auth control-store observation

The tracked
`tests/fixtures/voice_architecture_bakeoff/task_4_8_gate_package.template.json`
contains a closed, payload-safe snapshot of an **administratively separate** GCP
project and a dedicated Firestore Native database in `us-central1`. It records
only opaque references and the bounded control-plane posture: pessimistic
concurrency, App Engine and point-in-time recovery disabled, deletion protection
disabled, one-hour version retention, free tier, and no user-managed service
accounts listed at observation time. Preparation made zero document writes; the
record deliberately does not claim a database-wide document scan.

This snapshot is a prerequisite reference, not a satisfied execution gate. It
does not establish an independent root of trust, credential broker, durable
trust/revocation store, immutable custody, provider account, workload identity,
or permission to contact a provider or PSTN. Google-managed service agents and
broader standard control-plane APIs may exist; neither fact is evidence of a
workload or a provider integration. The gate report can surface the snapshot only
as `reference_only_observed` and remains `not_authorized` with every blocker
present.

## Offline execution-firewall model

`app/services/voice_bakeoff_execution_firewall_contracts.py` is a stdlib-only,
unwired model for a future execution firewall. It binds a declared,
digest-only production destination and identity denylist to a source-pinned
approval projection, an isolated nonproduction scope, and a metadata-only
broker-policy request. The grant carries the exact approved dependency-binding
digest, its canonical destination set, and the dependency and
credential-reference digests, so a future adapter cannot exchange its bound
endpoint/account/privacy constraints for a same-named dependency. Every member
of that destination set is checked against the declared production denylist.
Domain-typed digests prevent substitution between those fields; unknown,
expired, revoked, lower-generation, or digest-changed policy state fails closed.
Its resolver is stateless and repeatable: it returns only a bounded metadata
grant, never a credential, token, endpoint, workload request, nonce transition,
or execution permit.

The model is defense in depth, not evidence that the production denylist is
complete or that a broker exists. It has no runtime composition path, external
policy authority, identity attestation, IAM enforcement, credential store,
network access, or provider integration. A later connected implementation must
establish those controls independently and must not treat this offline model or
its tests as Task 4.8 authorization.

## Gates that keep Task 3.4 and Task 4.8 blocked

The following do not exist in this slice and cannot be inferred from protocols,
templates, or passing offline tests:

- concrete provider-specific launcher, credential broker, attestors, adapters,
  evidence reader, residue auditor, or connected denial probes;
- an execution-ready dedicated nonproduction Twilio/provider account, principal,
  destination, token-store implementation, evidence sink, or credential
  inventory; the reference-only Firestore observation does not supply any of
  these;
- cryptographic signer keys, current trust store, immutable custody service, or
  durable atomic nonce store;
- provider account/region/privacy attestations and verified deletion procedures;
- a clean exact-SHA worktree with the complete executable bundle and dependency
  digests;
- a sealed sole-owner authorization following an advisory technical review with no
  unresolved P1, and a just-in-time one-use envelope bound to that exact bundle.

Those controls require a fresh exact-SHA advisory technical review with no
unresolved P1 and a later sealed sole-owner authorization. Until then, a successful
local preflight returns
`blocked_external_verification_required`; it never returns an execution permit.
