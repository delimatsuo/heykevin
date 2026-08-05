# Offline voice composition and session-driver addendum

**Status:** panel-approved with the conditions below incorporated.

**Authority:** offline construction and synthetic verification only. This addendum
does not authorize provider access, credentials, PSTN calls, staging, production,
participant work, or Task 4.8. The isolated bakeoff application must retain its
current close-after-authentication behavior.

**Bound baseline:**

- source HEAD before implementation:
  `41599321bb8ab8d45162432fba2bce81a88f7daa`
- `app/experiments/voice_bakeoff_app.py`:
  `082d82e73deff2db331ba120513327f6911f41f1c9f0e9e7279e8f711df13127`
- `app/main.py`:
  `e73f0cd47ad1e10358e47e7db1981c39f0e03e041996cb4d2fd50cc9c308b7e9`
- `app/webhooks/media_stream.py`:
  `4dab265d4b82336d8b0239090ee4c751cff345da359d2b70b38aa4f5e48e850c`

The implementation and verification commits must assert that those three
application and production-routing files remain unchanged.

## Delivery sequence

1. Implement and exact-review `TurnCompositionTransaction`.
2. Commit the composition only after staff and security approve its exact tree.
3. Implement and exact-review the sealed offline session driver and synthetic
   journey fixtures.
4. Re-run the full offline gate and report remaining external blockers.

There is no connected-session handler in these two increments. Any future
post-authentication invocation in `voice_bakeoff_app.py` is a Task-4.8 source and
configuration change requiring a separate exact-SHA, one-use authorization.

## Final-turn admission

Composition accepts a caller turn only with a one-use
`FinalTurnAdmissionReceipt`. The receipt is minted after the selected real
offline candidate adapter:

1. emits an exact canonical `INPUT_TURN_FINAL` event;
2. has that event accepted by the shared `VoiceLifecycle`; and
3. returns an opaque one-use final-transition capability registered by object
   identity inside that exact adapter; and
4. proves the final content bytes and length match the event's domain-separated
   content digest.

The receipt binds:

- candidate arm;
- exact adapter class, sealed-assembly reviewed source/contract digest, and
  configuration digest;
- canonical event digest;
- content digest and byte length;
- session binding, call, stream, epoch, input-turn ID, sequence, and time;
- a one-use receipt ID and bounded expiry.

Receipts are content-free, internal-only, never logged, exported, or persisted,
and retained only as bounded replay tombstones. A receipt is consumed exactly
once. Candidate-final content cannot be manufactured by composition or accepted
from a different arm, adapter, binding, epoch, or turn.
Public result helpers cannot create the capability, subclasses are rejected, and
a cloned event cannot spend it.

## Extractor admission state

Extractor concurrency and replay state is separate from canonical conversation
state. It contains only bounded, content-free operational records:

- `IN_FLIGHT`;
- a terminal extraction outcome;
- binding, epoch, turn, sequence, and opaque receipt identity.

It is serialized per binding, capped by count and TTL, and terminalized
deterministically. It contains no raw content, observation fields, spoken text,
or content digest suitable for offline guessing. Rejected, late, duplicate, or
expired extraction cannot mutate `IntakeState`, speech, call, lifecycle, or
adapter state.

Admission bookkeeping is performed under its lock, but no backend or
current-turn callback is invoked while that lock is held. Synchronous re-entry
therefore observes the existing `IN_FLIGHT` record and fails closed without
deadlocking or running a duplicate backend.

## `TurnCompositionTransaction`

Transactions are serialized per exact binding. Each has a replay key and stores
one terminal outcome. Replays return that stored outcome without extraction,
planning, reservation, or adapter work. Terminal outcomes have bounded
retention; capacity rejection consumes the receipt without growing the retained
map.

### Phase 1: accepted facts

```text
RECEIVED
  -> FINAL_TURN_ADMITTED
  -> EXTRACTION_IN_FLIGHT
  -> OBSERVATION_ACCEPTED
  -> FACTS_STAGED(N -> N+1)
  -> FACTS_COMMITTED(N+1)
```

- The `IntakeState` mutation occurs on a detached staged copy.
- A compare-and-swap commits only when the canonical state is still version `N`.
- Accepted facts at version `N+1` survive later response failure.
- `side_effects_allowed` remains `false`.
- A newer caller turn superseding version `N` terminates the stale transaction;
  it cannot plan, speak, overwrite state, retain a permit, or arm a timer.

### Phase 2: proposed response

```text
FACTS_COMMITTED(N+1)
  -> ACTION_PLANNED
  -> PROPOSAL_VALIDATED
  -> SPEECH_BATCH_RESERVED
  -> QUESTION_RESERVED
  -> STATE_VERSION_REVALIDATED(N+1)
  -> RESPONSE_AUTHORIZED
  -> ADAPTER_PERMITS_ACCEPTED
  -> RESPONSE_PENDING_PLAYBACK
  -> RESPONSE_OBSERVED_OR_INFERRED
  -> RESPONSE_COMMITTED
```

The materializer is structurally non-authorizing. It receives only the closed
`NextAction`, the state version, locale, and allowlisted safe facts. It returns a
typed content proposal; it cannot import, construct, or return
`SpeechAuthorization`.

Composition-owned policy constructs authorization and validates:

- exact binding, epoch, turn, state version, action, plan, and proposal identity;
- semantic kinds and terminal eligibility;
- allowed and forbidden question slots;
- safe-fact provenance;
- tools and side effects disabled;
- a direct answer only when the caller asked an applicable direct question;
- at most one question;
- separate typed shapes for repair, safety, presence, and closure acts.

The state-version compare-and-swap is repeated immediately before authorization
and permit issuance. The exact state delivery lease remains held from that
revalidation through speech authorization, canonical lifecycle authorization,
adapter permit acceptance, pending-response publication, and result publication.
A mismatch runs compensation and emits no speech.

`QuestionIntent` may be reserved before speech, but `asked_slots` and delivered
response state are committed only after canonical `caller_playback_observed` or
the preregistered conservative `playback_inferred` fallback. Adapter permit,
generation completion, final token, TTS completion, playout binding, and transport
resolution do not make a question asked and do not arm a timer.
Playback is accepted only while the exact speech act and adapter permit remain
live. Each permit is retired before observation is committed, batch cleanup
completes before success is published, and late playback after cancellation
cannot mark a slot asked or arm a timer.

Multi-act batches expose and permit exactly the first unresolved act. A later act
has no usable adapter permit or public authorization receipt until canonical
playback of every preceding act is observed. Activating the next exact permit is
part of the same guarded observation transaction; failure hard-terminalizes the
pending batch.

## Compensation and terminal evidence

Before canonical response authorization, an exact pristine speech/question batch
may be rolled back.

After any canonical `RESPONSE_AUTHORIZED` event:

- evidence is never erased;
- only canonically authorized acts receive canonical `ACT_FAILED`;
- reserved but unauthorized acts are cancelled without invented authorization;
- accepted permits are retired;
- speech acts are cancelled;
- the exact pristine question reservation is cleared;
- no `asked_slot`, delivered response, or timer is committed;
- the replay tombstone records the terminal outcome.

Multi-act authorization is all-success-or-terminalized. Ambiguous or failed
compensation terminalizes the affected adapter and turn and returns a hard
fail-closed result; it never reports success or rollback. The local call reducer
is permanently sealed on that path so no residual question, act, or timer
authority remains usable.

Adapter terminalization, resume, permit, final-input, and receipt state share one
atomic authority lock. A native resume stages an exact one-use adapter result but
does not reopen authority until the identical `SESSION_RESUMED` event has already
been accepted by the exact assembly-bound canonical lifecycle object. The
final-turn receipt authority is likewise bound to that lifecycle and one exact
adapter instance. Rejected, cloned, replayed, stale, shadow-lifecycle, or
concurrent evidence leaves admission closed.

Arm C permit admission and its generation deadlines are one atomic transition
under that same lock. Permanent terminalization clears begin, completion, and
pending-timeout authority before releasing the lock; later timer, authorization,
or timeout-receipt calls fail closed.

The composition transaction binds itself once as the exact timeout owner of its
exact-class Arm C adapter only when the transaction also names that same adapter,
canonical lifecycle object, and session binding. Every public adapter timeout
request releases the adapter lock and delegates to a callback captured directly
from the reviewed transaction class at bind time.
The transaction acquires composition ownership before invoking the adapter's
internal commit path, which holds the adapter authority lock across
intent validation, canonical sequence allocation, lifecycle ingestion, and
consumption of the identical one-use timeout receipt. Every successful internal
commit invokes the stored transaction cleanup callback after releasing the
adapter lock, so direct internal-method reachability cannot consume a timeout
without the same mandatory compensation. Transaction-owned batch terminalization
then runs before composition ownership is released. Equal clones, alternate
owners or adapters, foreign calls or epochs, subclasses, replaced instance
callbacks, and terminalization races cannot mint or consume timeout authority.
A committed timeout removes retained audio, TTS, playout, frame, speech,
question, timer, and pending-response authority, records a bounded terminal
composition outcome, and permanently seals the adapter and local call reducer.

Same-thread terminalization requested by a lifecycle callback is deferred until
the in-progress timeout transaction has either committed or failed. This orders
reentrant and cross-thread terminalization identically. External terminalization
invokes its transaction-owned cleanup callback only after releasing the adapter
lock. Public terminalization has no owner-suppression argument. Reentrant
owner cleanup is instead detected inside the transaction by receipt identity,
without exposing a bypass surface. The single lock order is therefore
transaction then adapter, without exposing a partially ingested lifecycle
event, retained timeout receipt, or orphaned composition response.

Disconnect-revoked permits remain bounded replay tombstones. Later supersession
recognizes the exact already-revoked authorization as safe cleanup evidence,
while an absent or mismatched permit still hard-terminalizes.
Disconnect provenance is stored separately from ordinary retirement and is
accepted only for the exact canonical authorization object; an ordinary,
cloned, forged, or same-key retirement is never promoted to disconnect proof.

`ACT_FAILED` terminalizes the semantic act, not automatically the call. Recovery
continues through the shared caller-outcome matrix whenever current authenticated
transport authority remains proven.

## Caller outcome matrix

| Outcome | Canonical state | Caller-visible act | Timer and teardown |
| --- | --- | --- | --- |
| Partial, late, cancelled, duplicate, or superseded turn | No facts or response commit | None | Canonical caller activity still cancels an existing timer; no new timer |
| Low confidence, timeout, provider error, or malformed extraction with current binding and repair budget | Unchanged | One fixed localized input-repair act through the canonical authorization chain | No question timer |
| First downstream recoverable failure | Accepted facts remain | One authorized fixed repair or exact complete-act replay | Global repair budget consumed for exact binding, call, and epoch |
| Second recoverable failure or unavailable repair | Accepted facts remain | Composition returns typed `closure_required` with no audio; the future driver may use fixed generic closure only with every positive binding and a one-use local closure capability | Teardown only after canonical playback observation/inference and no newer activity |
| Security, privacy, capability, binding, epoch, or external-outcome uncertainty | No new response state | None | Revoke and no-audio teardown |
| Proven reconnect with a fresh matching epoch | Confirmed facts only | Fixed reconnect repair through the canonical chain | Stale acts and timers remain cancelled |
| Actual caller transport loss | Preserve prior observed/inferred playout evidence | No additional audio | Residue-only teardown |

Every audible repair, safety, presence, voicemail, unsupported-mode, or closure
act follows the sole chain:

```text
shared policy
  -> SpeechControl
  -> canonical RESPONSE_AUTHORIZED
  -> selected-adapter permit
  -> canonical playback observation or approved inference
```

No emergency or closure bypass exists. Closure audio never grants terminal
execution. Fresh canonical caller activity or interruption cancels closure
progression. Transport loss never rewrites uncertain prior playout as silence.
Fixed localized copy comes only from the closed reviewed catalog and never
interpolates raw turn or state content. This increment includes fixed English,
Spanish, Brazilian Portuguese, and Mandarin assets with locale-specific safety
authorization; a known unsupported locale fails closed instead of silently
receiving English.

## Sealed offline session driver

The second increment is a synchronous, statically pinned, repo-local driver. It
does not accept arbitrary callables, plugins, coroutines, or handler imports.

```text
CREATED -> LEASED -> ACTIVE -> CLOSED
                         \-> ABORTED
```

It uses synthetic fixtures and a single-use in-memory facade. The facade exposes
no `WebSocket`, `Request`, runtime, authenticator, token, dependency, URL,
credential, filesystem, environment, process, reflection, dynamic import, or
network object.

Every operation verifies the current binding, turn, epoch, lease, and revocation
state. The contract digest binds:

- driver and facade source/code digests;
- composition, materializer, policy, catalog, and adapter identities;
- codec and frame schema;
- inbound and outbound frame-size, frame-count, cumulative-byte, audio-duration,
  session-duration, queue, and concurrency limits.

The driver permits one active invocation, owns and joins all child work, rejects
use after return, and scrubs mutable audio and byte buffers before dropping
references. It promises no durable, logged, cached, persisted, or exception-echoed
raw content. It does not claim immutable Python strings or bytes are zeroizable.

### Current core driver slice

The first driver slice implements only these closed synthetic journeys:

- applicable direct answer followed by one question;
- question-only intake;
- one localized low-confidence repair;
- localized safety guidance;
- unsupported-language fixed English-to-Spanish-to-Mandarin prompt from exact
  French and Modern Standard Arabic triggers, with integrated English, Spanish,
  and Mandarin recovery journeys plus Portuguese, ambiguous, timeout, repeated
  French, and repeated Arabic typed no-audio exhaustion journeys;
- a superseding turn during Phase 2;
- Spanish to Mandarin to Spanish correction with stale-response suppression
  and one canonical value per state fact;
- one localized repair observed before a second low-confidence turn returns
  typed `closure_required` with no second repair or closure audio;
- the same two-turn exhaustion path followed by either one exact generic
  English, Spanish, or Mandarin Tier-A closure marker when every private
  predicate is proven, or exact no-audio teardown for every injected
  uncertainty, caller-transport loss, unsupported locale, or superseding
  activity;
- partial, cleared, failed, and interrupted question playouts followed by exact
  supersession without any slot becoming asked;
- an interrupted pending question followed by canonical disconnect cleanup, a
  synthetic re-established fresh epoch, and one fixed repair without stale
  speech.

All four candidate arms must produce an identical content-free canonical trace
for those journeys. The slice also enforces exact inbound and outbound frame,
byte, audio-duration, queue, session-duration, and concurrency limits; a
single-use nonblocking lease; exact reviewed-source digests; and mutable-buffer
scrubbing on success, revocation, expiry, limit failure, and internal failure.
Lease scope, expiry, binding, contract, and consumed state live in a frozen
driver-owned grant rather than the caller-held facade. Every facade field must
still match that grant exactly at admission, and active execution and result
publication use only the grant. Frame validation and accounting occur inside the
same cleanup-guaranteed boundary; malformed or replaced frame state aborts,
scrubs every driver-issued or still-reachable mutable payload, and leaves the
driver reusable. Reviewed fixture, adapter, and source-identity maps are
immutable, and the source identity set covers every direct behavior-bearing
service import plus the reviewed transitive dialogue planner.

The unsupported-language slice uses one immutable `language_choice` descriptor,
not candidate-generated copy. It binds the exact English, Spanish, and Mandarin
NFC UTF-8 text and per-segment SHA-256 digests, ordered ordinals, two 250 ms
pauses, and aggregate descriptor SHA-256
`f840fd799016c0f3369e8c4894e15abfa58138d5f99e82e1d9585c4d00a3d622`.
In this offline tier, canonical observation of the third final-segment playback
marker latches one 10,000 ms deadline. Complete speech-batch cleanup transitions
to the response-window phase without moving that deadline; eligible cleanup-
boundary onset or final input is arbitrated against the same clock. Those local
markers do not prove that a caller heard any audio; a later rendered/live tier
must separately start and verify timing from approved caller-receipt evidence or
inference. Eligible speech onset at or before the offline window's inclusive
deadline may run for at most 15,000 ms, and a final may arrive within 2,000 ms
of accepted speech end. Canonical retained input wins a same-boundary timer
race. A final-only response at the early, middle, final, or cleanup boundary
atomically seals all remaining prompt authority before it can qualify.

Only a final detected as `en`, `es`, or `zh` can mint the distinct,
purpose-sealed, one-use language-recovery receipt/admission pair. `pt`, `fr_fr`,
`ar_msa`, missing, ambiguous, and every other locale fail closed. A qualified
same-call recovery proceeds through the ordinary fixed response pipeline only
after consuming that exact pair and proving that extraction returns the same
locale bound by the candidate-final receipt. Stale pre-prompt input and a
wrong-turn activity end cannot open or indefinitely defer the response window;
the lifecycle arbitrates every retained accepted input observation in monotonic
sequence order. Timeout or unqualified response uses the separate
`language_choice_exhausted` trigger and can issue only a no-audio terminal
receipt after the typed transaction, admission, silence, speech, outbound,
call, and adapter inventory is exactly sealed. The integrated driver retains
exactly the three prompt markers and emits no additional outbound frame on each
exhaustion journey. That receipt cannot satisfy playback or disconnect
evidence and cannot be substituted for generic failure closure.

The low-confidence Spanish fixture starts from a trusted reviewed fixture locale
before extraction, reserves the fixed Spanish repair asset, and records only the
allowlisted locale code in its content-free trace. The superseding fixture emits
one `INPUT_FINAL` trace event for each admitted final turn before stale-response
supersession and new-response delivery. The bidirectional code-switch fixture
commits three final turns (`es` to `zh` to `es`), proves both stale responses are
superseded before the one delivered response, and verifies corrected state facts
replace rather than duplicate prior values. The repair-exhaustion fixture proves
the first authorized repair is fully observed before a second final turn is
admitted; the already-consumed global call/epoch repair budget then yields typed
`closure_required` without reserving, confirming, or playing another act. The
unobserved-question fixture drives four separately authorized question attempts
through partial, cleared, failed, and interrupted playout outcomes. Each stale
attempt is terminalized; the next final turn then supersedes it before preparing
its replacement response. A final cancelled turn leaves no pending response,
timer, or asked-slot mutation.
The failed question attempt records transport resolution first, then proves an
accepted `ACT_FAILED` blocks preregistered playback inference and cannot mark the
question asked or arm its silence timer.
The driver also proves each superseded question has no live playback permit or
speech authority, and the completed journey retains no unconsumed admission
receipt.
The reconnect fixture interrupts an authorized pending question before caller
playback, consumes an exact canonical disconnect event to retire the old
response, tombstones every still-live final-turn receipt, and permanently closes
the old adapter after its re-established boundary. A driver-derived binding
preserves environment, tenant, call, confirmed facts, and monotonic state version
while advancing the epoch and stream identity. Only the fresh assembly may
deliver the existing fixed input-repair asset. The old receipt replays only its
typed `session_disconnected` terminal outcome, and the old question never
becomes playback-observed or asked. A receipt-retirement or ordinary
speech-cancellation fault hard-terminalizes the old adapter, local call reducer,
pending composition, live receipt set, and speech authority before ownership is
released. Content-free speech act identities remain durably enumerable by exact
binding even after bounded composition outcomes expire, so per-act hard cleanup
does not depend on replay retention. The same session-wide cleanup applies after
already observed question playback: confirmed asked-slot evidence remains, while
the old silence timer and all old-binding speech authority are permanently
retired.

### Generic-failure closure ownership and linearization

The generic-failure fixture extends the typed `closure_required` outcome without
turning a test posture into authority. A lease-bound fault selector chooses only
which negative condition the harness injects. It is recorded for reproducibility
but is never passed to or read by the closure authority. The no-fault case must
still derive one coherent private proof from current owned state.

| Predicate | Offline owner and evidence | Freshness and revocation |
| --- | --- | --- |
| Closure kind | Closure registry; exact `scripted_opt_out` or `generic_failure` enum | Domain-separated in proof, capability, stage, commit, IDs, asset lookup, and trace |
| Lease and active execution | Driver-owned exact lease-grant objects, revisions, arm, journey, expiry, and contract digest | Revalidated before every state assignment; this is synthetic offline evidence, never live authentication |
| Environment, tenant, call, session, stream, and epoch | One exact-primitive private binding snapshot in the closure registry | Public receipts receive distinct value copies; mismatch, mutation, expiry, or rebind invalidates |
| Failure trigger and external outcome | Exact driver-owned `CompositionResult` with `closure_required`, zero acts, an already observed first repair, and captured intake version | Captured under the driver lock only after ordinary authority is sealed; any different or newer result is uncertain |
| Locale | Latest unambiguous accepted caller-language fact and state version at the second failure | No configured or English fallback; missing, drifted, unsupported, or Portuguese provenance is silent |
| General authority | Driver-owned transaction, admission, speech, silence, and general outbound inventories | All are terminal, empty, and zeroized before proof; none is referenced by the retained closure lane |
| Destination, privacy, and local closure transport | Closed private registry enums plus an independently invalidatable local generation | Provider-free and distinct from candidate/provider transport; loss or uncertainty revokes before audio |
| Proof, capability, stage, commit, and audio | Sole private registry ownership; public objects are identity-bound non-authoritative views | Exact, expiry-bound, copy-resistant, cross-kind/cross-binding non-transferable, and single-use |

The provider-free local closure lane is the only authority retained after the
ordinary composition assembly is terminalized. It contains no transaction,
admission, speech, timer, general-queue, caller-content, provider, or business
reference. Its state machine is:

```text
CLOSURE_REQUIRED
  -> GENERAL_AUTHORITY_SEALED
  -> PRIVATE_PROOF_LIVE
  -> CAPABLE
  -> STAGED
  -> COMMITTED
  -> FRAME_CONSUMED_FOR_SYNTHETIC_PLAYBACK
  -> TERMINATED

any uncertainty, loss, expiry, mismatch, or newer activity before consume
  -> NO_AUDIO_TEARDOWN
```

The final consume is one latch-owned atomic operation. Under the closure latch
it revalidates the complete private snapshot and invalidation generation, returns
one immutable ordinal-zero frame copy, records one fixture-only synthetic
playback marker, tombstones proof/capability/stage/commit authority, and scrubs
the sole private mutable source before releasing the latch. If invalidation
linearizes first, it returns no bytes and records no marker. If invalidation
arrives after consume, it cannot create a second frame or undo the single
fixture marker and only confirms teardown.

The generic-failure lane uses semantic kind `closing`; `opt_out` remains exclusive
to scripted opt-out. Ordinary first-repair traces may truthfully include
`transport_resolved`, `playback_observed`, and `response_observed`. Those labels
are forbidden for the generic-closure lane from
`general_authority_sealed` onward. The closure lane may report only typed local
proof retention, offline commit, atomic synthetic consume, no-audio teardown,
and teardown completion. It never reports authentication, provider acceptance,
delivery, caller-heard audio, or barge-in behavior.

English, Spanish, and Mandarin generic-failure text is provisional source-fixture
material only. Portuguese and every unsupported or ambiguous locale remain
silent. This increment may prove local one-use authority, ordering, negative
silence, cleanup, and a 160-byte/20 ms synthetic marker. Full utterance,
pronunciation, pacing, clipping, naturalness, comprehension, and caller-heard
timing remain blocked on separate authorized rendered-audio and native-speaker
qualification.

This slice is not the completed synthetic-journey gate. Journeys not explicitly
listed in the current core slice still require later offline-only increments
and fresh exact-tree review before the broader driver gate can be marked
complete.
The language-choice slice proves only immutable metadata, ordering, canonical
state transitions, boundary clocks, one-use authority, cleanup, and
content-free synthetic trace parity. It does not prove rendered audio,
pronunciation, comprehension, provider behavior, caller-heard timing, or live
terminal UX. Provider/PSTN access, credentials, staging, production, and Task
4.8 remain sealed.

## Synthetic journey fixtures

Every arm must produce the same canonical externally observable trace for:

- applicable direct answer followed by at most one question;
- question-only intake without a manufactured answer;
- low-confidence input and one localized repair;
- correction and bidirectional code switching without repeated facts;
- interruption and proven reconnect without stale speech;
- exact silence, boundary speech, and `more_time`;
- `repeat` and `slower`;
- scripted opt-out versus actual or ambiguous participant withdrawal;
- simulated voicemail;
- unsupported language and access mode;
- safety guidance;
- one repair success followed by a second failure;
- locally proven generic closure versus uncertain-binding no-audio teardown;
- a superseding turn during Phase 2;
- partial, cleared, failed, and interrupted questions never becoming asked.

The fixtures contain synthetic personas and no customer, participant, production,
or credential data.

## Required verification

- fault injection after every transaction mutation boundary;
- exact state-version and superseding-turn races;
- exact and one-over resource limits, including Unicode byte/character cases;
- all-arm canonical trace parity;
- exact authorized-versus-reserved compensation;
- replay, expiry, stale epoch, rebind, revoke, and use-after-return;
- no orphan question, timer, permit, speech act, child task, or buffer;
- recursive transitive import and capability firewall;
- reverse and dynamic import checks from production modules;
- exact bakeoff-app hash and route-table baseline assertion;
- gate report remains `execution_status: not_authorized` with all nine external
  blockers;
- focused and complete unit suites;
- independent exact-tree staff and security review for each implementation commit.

Offline evidence may prove deterministic act ordering, lifecycle safety, fixed
localized asset selection, and absence of stale or duplicate speech. It cannot
prove caller-heard audio, comprehension, linguistic quality, naturalness, provider
behavior, real latency, connected readiness, or live delivery.
