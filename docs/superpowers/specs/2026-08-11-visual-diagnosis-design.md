# Hey Kevin Visual Diagnosis Design

Date: 2026-08-11; A0-A2 contract amendment 2026-08-12
Status: Rebased A0-A2 implementation draft; exact-diff exit review pending
Bound implementation baseline: `origin/main` commit `d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`
Scope: Business-mode, post-call, visual-first preliminary diagnosis and contractor preparation

## 1. Decision Summary

Hey Kevin should turn a customer's existing call narrative plus a short photo or
video into two separate products:

1. an immediate, evidence-linked **preliminary visual diagnosis** for the
   customer when a deterministic delivery policy says the case is supported and
   low risk; and
2. a richer **pre-visit intelligence packet** for the contractor containing
   equipment identity, evidence, ranked possibilities, missing onsite tests,
   and grounded parts candidates.

The interaction principle is:

> **Show Kevin, do not answer a questionnaire.**

Kevin must reuse the call transcript and inspect the submitted media before it
asks anything. The normal question count is zero. The hard maximum is two
one-tap diagnostic questions, asked one at a time. Kevin may ask only when the
answer can change safety handling, the top diagnostic possibilities, urgency,
or a verified equipment/parts match.

The first supported slice is deliberately narrow:

- HVAC only;
- business accounts only;
- the symptom family `not_cooling_with_outdoor_unit_noise`;
- one narrated video of 10-20 seconds;
- one optional rating-plate photo, requested only when the plate is not legible
  in the video;
- at most one targeted media recapture;
- at most two adaptive one-tap questions;
- no DIY electrical, refrigerant, panel-opening, or moving-component advice;
- no autonomous parts ordering;
- no customer-facing exact part numbers;
- no automatic repair price in the first release.

The existing estimate prototype is input to this design, not the design to ship.
Its current one-pass `AI Diagnosis` plus price message is superseded as a product
direction by the evidence, safety, delivery, and parts contracts in this document.
No connected or customer-visible source behavior changes are authorized by this
document; the isolated synthetic-offline A0-A2 implementation remains subject to
exact-diff review before any later phase.

## 2. Authority And Approval Boundary

The owner's later 2026-08-12 `approved` message authorizes the isolated,
synthetic-offline A0-A2 implementation recorded in the continuation handoff.
It does not authorize A3-A8, providers, real customer data, live delivery, or
deployment.

This document authorizes only the recorded synthetic-offline A0-A2 branch work.
It does not authorize work outside that slice:

- application or backend implementation outside the two new A0-A2 modules;
- provider calls using customer or production media;
- collection of real customer media for evaluation;
- changes to SMS/MMS, Twilio, Gemini, GCS, Firestore, IAM, or Secret Manager;
- enabling `estimate_token_create`, `estimate_result_sms`, or any new gate;
- staging or production deployment;
- App Store copy or customer claims;
- automated customer delivery;
- parts-catalog purchases, distributor integrations, or parts ordering.

The owner explicitly approved this specification and its companion plan by
their exact reviewed hashes for scope `synthetic_offline_A0_A2`. Provider-
connected qualification, real-customer data, customer delivery, and release
each remain separate gates.

The controlling documents/source/tests are content-addressed in the isolated
worktree. An exact-diff review must issue a detached approval receipt containing
the bound baseline, the full SHA-256 digest and byte count of each of the eight
allowlisted paths, the allowed path set, and the panel decision. The receipt is
external to the paths so recording it does not invalidate the bytes it approves.

The advisory receipt uses schema `visual_diagnosis_review_receipt/v1` and records
UTC issue time, baseline, document path/digest/byte-count tuples, the exact
changed-path allowlist, scope, review round, per-role reviewer task reference and
decision, unresolved findings, overall decision, debate result, a canonical
receipt digest, and `grants_implementation_authority: false`. A separately
recorded owner message from a trusted session must approve the same hashes for
scope `synthetic_offline_A0_A2`; a valid receipt never substitutes for that
authority. The receipt digest covers the versioned canonical receipt body with
the `receipt_digest` field omitted, avoiding a self-referential hash. Canonical
receipt JSON uses UTF-8, sorted keys, compact separators, closed fields, and a
lowercase hexadecimal SHA-256 digest.

During exact-diff review, `origin/main` advanced from
`fecec74220b16c561545d175e291be24058f5ad4` to
`7e9550b5f0792576709a671aa901bc3b0af29c3f`. A documentation-only source-gap
audit found seven changed paths: the deploy and rollback workflows, subscription
API/configuration, iOS paywall, and two subscription/release test files. The
change disables subscription promotional offers by default and does not alter
the inspected visual-diagnosis source-gap paths, `Dockerfile`, `app/main.py`, or
`pyproject.toml`. The two changed workflow-root hashes are rebound in the
companion plan. Primary `HEAD`/`main` intentionally remain at the earlier commit
during documentation review; no checkout, branch, worktree, source file, or
deployment state was moved. This document revision is bound to the newer commit
for any later implementation.

During the subsequent implementation rebaseline, `origin/main` advanced from
`7e9550b5f0792576709a671aa901bc3b0af29c3f` to
`d09c58f19db5a0054945628ba0a24e67747f8341` (PR #163). The exact source-gap
audit found twelve subscription-verification paths changed: backend
subscription API/service, iOS project and subscription clients, the new iOS
verification source/test, and two subscription unit-test files. No visual-
diagnosis path, `Dockerfile`, `app/main.py`, `pyproject.toml`, or deploy/
rollback workflow changed. The implementation worktree was fast-forwarded to
the new bound baseline; primary `main` was not moved.

During review of this rebaseline, `origin/main` advanced from
`d09c58f19db5a0054945628ba0a24e67747f8341` to
`d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`. The two changed paths only update
iOS project/version metadata; no visual-diagnosis path, backend source,
`Dockerfile`, `app/main.py`, `pyproject.toml`, or deploy/rollback workflow
changed. The implementation worktree was fast-forwarded to this current bound
baseline.

## 3. Problem

Customers already show real contractors photos and videos. A narrated clip often
contains more useful information than a long AI interview:

- visible equipment type and condition;
- manufacturer, model, serial number, voltage, capacity, and other rating-plate
  fields;
- fan motion, vibration, ice, leaks, damage, debris, or error indicators;
- humming, clicking, grinding, squealing, rattling, or startup behavior;
- the customer's natural explanation of what changed.

The current phone intake can capture the spoken symptom, but the contractor may
still arrive without the equipment identity, a useful visual, or the right
diagnostic preparation. Asking the customer to complete a long troubleshooting
questionnaire adds abandonment risk and repeats information already present in
the call or media.

The feature should provide immediate customer value without making a remote
model the final authority for electrical, mechanical, refrigerant, safety, or
parts decisions.

## 4. Product Outcomes

### 4.1 Customer outcome

After one simple submission, the customer should understand:

- what equipment Kevin identified;
- what Kevin directly observed;
- the one or two most plausible issue categories;
- why those categories fit the evidence;
- whether the situation appears routine, urgent, or unsafe;
- what cannot be confirmed remotely;
- what the contractor will inspect next;
- that the contractor received the media and equipment context.

The result must be useful. A generic response that only says "call a technician"
does not satisfy the product goal when the evidence supports a narrower, safe
preliminary result.

### 4.2 Contractor outcome

Before dispatch, the contractor should receive:

- the existing call/job context without requiring the customer to repeat it;
- original media and evidence timestamps;
- a label crop plus normalized manufacturer, model, and serial candidates;
- identity and catalog-verification status;
- ranked diagnostic hypotheses with supporting and conflicting evidence;
- explicit onsite tests that remain necessary;
- dispatch urgency and safety flags;
- tools or parts categories worth preparing;
- exact parts only when a deterministic, authoritative lookup confirms
  compatibility for the equipment identity and applicable serial/configuration;
- one-tap feedback for actual fault, part used, and usefulness.

### 4.3 Business outcome

The feature should be evaluated for:

- higher qualified-lead and booking conversion;
- faster contractor review and dispatch prioritization;
- better first-visit preparation;
- fewer avoidable follow-up calls;
- improved first-visit completion;
- customer trust and satisfaction;
- acceptable cost and latency;
- no degradation in safety or privacy.

These are hypotheses until measured. Documentation, implementation, and model
outputs must not present them as proven outcomes.

## 5. Non-Goals

The MVP will not:

- support every HVAC complaint, trade, appliance, or jurisdiction;
- produce a definitive diagnosis;
- replace licensed or qualified onsite inspection;
- tell a customer to open panels, touch wiring, push a fan, handle refrigerant,
  bypass controls, reset unsafe equipment, or perform a repair;
- infer exact parts from appearance or unconstrained model generation;
- expose exact contractor-only part numbers to customers;
- place parts orders or reserve inventory;
- guarantee that a suggested part is the installed or failed part;
- provide an automatic repair price or binding quote;
- analyze live media during the phone call;
- reuse customer media for training without a separate explicit opt-in and
  approved policy;
- enable the existing estimate workflow globally;
- treat a model's self-reported confidence as calibrated probability;
- treat a valid JSON response as a safe or correct diagnosis.

## 6. Terms And Claims Contract

The product may be named **Kevin Visual Diagnosis**. The actual result must be
labeled **Preliminary visual diagnosis**.

Definitions:

- **Observation**: a bounded fact extracted from the transcript, a frame, audio,
  a label, or deterministic metadata, with provenance.
- **Equipment identity**: manufacturer, model, serial, and related fields plus
  verification status. OCR text alone is not verified identity.
- **Hypothesis**: one plausible issue category, ranked within the supported fault
  taxonomy. It is not a confirmed failure.
- **Preliminary visual diagnosis**: an evidence-linked, customer-safe summary of
  at most two hypotheses plus what remains unknown.
- **Abstention**: a typed decision not to narrow the issue or not to deliver a
  customer result.
- **Verified compatible part**: a part retrieved from an approved authoritative
  source whose documented model, serial/configuration, and supersession rules
  match the verified equipment identity.
- **Parts candidate**: a contractor-only suggestion requiring verification.
- **Ground truth**: the contractor-recorded onsite finding and, when applicable,
  the installed or replaced part. Model agreement is not ground truth.

Allowed customer phrasing:

- "The video is most consistent with ..."
- "The two most plausible causes are ..."
- "A technician needs to test ... to confirm."
- "Kevin could not narrow this safely from the media."

Disallowed phrasing:

- "Kevin confirmed the problem."
- "This is definitely the capacitor/compressor/motor."
- "The contractor will bring the exact part" unless a contractor explicitly
  makes that commitment.
- "Safe to keep running" unless the statement comes from a reviewed,
  deterministic safety policy for the exact supported condition.
- guaranteed cost, savings, completion, revenue, or safety claims.

## 7. MVP User Journey

### 7.1 Call intake

1. Kevin handles the normal business call.
2. Existing intake captures the customer's natural description, including
   `not cooling`, the location of the noise, onset, and any facts volunteered.
3. Kevin must not turn the call into a diagnostic questionnaire.
4. For an in-scope visual problem, Kevin may offer a post-call secure upload.
5. The existing transcript/job-card facts become input to visual diagnosis; the
   customer is never asked to repeat them.

### 7.2 Capture

The customer receives a short-lived secure link with a single primary action:

> Record 10-20 seconds. Show the whole outdoor unit and the sound or movement.
> You can say what changed while recording. Stay at a safe distance and do not
> open or touch the equipment.

The capture page should:

- show a brief consent and provider-processing notice before upload;
- advise the customer not to record faces, bystanders, house numbers, license
  plates, paperwork, or unrelated property;
- prefer a direct camera capture or selected existing file;
- show progress and a recoverable upload failure state;
- request a rating-plate image only if the plate cannot be read from the video;
- request at most one targeted retake using a visual overlay, not a paragraph of
  instructions;
- let the customer decline, answer `not sure`, or report that a requested action
  cannot be completed safely without penalty or repeated prompting;
- support the app's approved languages without changing diagnostic scope.

The maximum customer-effort envelope is explicit: one initial symptom video,
at most one rating-plate request, at most one targeted symptom-media recapture,
and at most two diagnostic questions. A rating-plate request and a targeted
recapture use separate counters and may both occur only when each independently
changes a permitted decision. Only one customer action may be outstanding at a
time. Transport failure and replay do not consume another budget unit.

### 7.3 Media-first analysis

Kevin combines:

- call-derived structured facts and bounded transcript context;
- the narrated video and its audio;
- selected high-resolution frames;
- an optional rating-plate image;
- contractor business scope and supported-service policy;
- an approved HVAC observation and fault taxonomy;
- an approved equipment/catalog source, when available.

Kevin first extracts observations. It does not jump directly from raw media to a
customer message.

### 7.4 Adaptive question policy

The normal question count is zero. The maximum is two.

A question is eligible only if all of the following are true:

1. The answer is not already known from the call, media, account context, or a
   prior answer.
2. The question is selected from a reviewed, versioned question library.
3. The expected answer can change at least one of:
   - safety handling;
   - top-one/top-two hypothesis membership or order;
   - dispatch urgency;
   - equipment identity verification;
   - authoritative parts compatibility.
4. The answer format is `yes`, `no`, `not sure`, or another bounded one-tap set.
5. The question is safe for a non-technician to answer without approaching,
   opening, touching, testing, or restarting equipment.

The planner must record why the question was asked and which decision it could
change. It must never generate an open-ended troubleshooting interview.

Examples:

- Do not ask whether the outdoor fan is moving when motion analysis is usable.
- Ask whether there is a burning smell only when that missing fact changes the
  safety branch and cannot be observed from media.
- Ask whether indoor vents still have airflow only when that fact separates the
  supported outdoor-unit path from a broader airflow path.
- Request a clear label photo instead of asking a customer to type a long model
  or serial number.

If the budget is exhausted, Kevin either delivers from current evidence or
abstains. It does not exceed the budget.

Question selection, safety meaning, and customer wording are later policy
responsibilities. The Phase A state machine records only opaque library/copy
references, stable response codes, presentation locale, and structural action
status; it never stores or generates rendered question prose.

### 7.5 Customer result

The customer result contains:

1. **Equipment identified**: manufacturer/model only when display is permitted;
   serial is contractor-only by default.
2. **What Kevin observed**: short, evidence-linked facts.
3. **Preliminary diagnosis**: one or two supported hypotheses.
4. **Why**: the observations that support them.
5. **What remains unknown**: onsite measurements or inspection still needed.
6. **Urgency and safe next step**: selected from approved policy copy.
7. **Contractor handoff**: confirmation that the contractor received the packet,
   only after delivery is actually recorded.

Automatic customer delivery is allowed only when the Customer Delivery Policy
in Section 12 returns `deliver`. Otherwise the result is contractor-only or an
abstention message.

### 7.6 Contractor packet

The contractor packet contains:

- customer/job reference and call summary;
- original media behind contractor-authenticated access;
- label crop;
- equipment identity candidates and verification state;
- timestamped observations;
- top two diagnostic hypotheses;
- supporting, conflicting, and missing evidence;
- safety and urgency classification;
- recommended onsite checks from an expert-reviewed library;
- parts candidates with provenance and compatibility state;
- the exact customer message and delivery state;
- feedback controls for actual finding and actual part.

The packet must distinguish model output, deterministic catalog facts, customer
statements, and contractor-confirmed facts visually and in the API schema.

## 8. System Architecture

```text
Call/job context
      +
Short-lived upload token
      |
      v
Hardened media ingress -> quarantine/private object storage -> media normalization
      |                                                    |
      |                                                    +-> metadata stripping
      |                                                    +-> selected frames
      |                                                    +-> bounded audio track
      v
Provider-neutral observation extraction
      |
      v
Schema + semantic validation -> safety policy -> equipment identity resolver
      |                                      |              |
      |                                      |              +-> authoritative catalog lookup
      |                                      v
      |                                  safety stop
      v
Supported fault taxonomy + hypothesis ranker
      |
      +-> adaptive-question planner (0 normally, 2 maximum)
      |
      v
Customer Delivery Policy
      |                    \
      v                     v
customer-safe result     contractor packet + feedback
```

Architectural rules:

- Provider-specific media APIs stay behind adapters.
- Raw model output is untrusted.
- Observation extraction, diagnosis ranking, question planning, customer
  delivery, and parts compatibility are separate decisions.
- The generative model cannot directly send SMS, create a customer result,
  approve a part, or choose a novel safety instruction.
- Customer copy is assembled from validated fields and reviewed templates.
- Every diagnosis and parts statement carries provenance.
- Failure in any downstream stage must not expose a partial model result.

## 9. Domain Contracts

The names below are conceptual. The implementation plan may refine module and
field names but must preserve the boundaries.

Phase ownership is explicit. A1 owns only the structural subset of
`VisualTriageCase`, `MediaAsset`, `PendingCustomerAction`, `VisualTriageEvent`,
opaque artifact references, and the four projections. Sections 9.3-9.6 and
9.8-9.10 describe later A3-A7 domain contracts and are outside A1. A1 must not
predeclare their content fields through a generic model or serializer.

### 9.1 `VisualTriageCase`

- `schema_version`
- `case_id`
- `contractor_id`
- `call_sid_ref` or job reference, never raw in customer URLs
- `supported_scenario`
- `aggregate_revision`
- orthogonal `state_vector`
- at most one unresolved `pending_customer_action`
- `question_count`
- `rating_plate_request_count`
- `recapture_count`
- `created_at`, `expires_at`, `completed_at`, `deleted_at`
- `policy_versions`
- opaque external-receipt slots, fixed empty in A0-A2 and added only by a later
  reviewed phase
- bounded ordinary and terminal/control processed-event receipt lanes containing
  event ID, kind, semantic-envelope fingerprint, historical decision code, and
  resulting revision, never payload

One lifecycle enum cannot represent processing, contractor delivery, customer
delivery, and deletion states that legitimately coexist. The canonical case
state is therefore an orthogonal vector:

| Dimension | Closed structural states |
| --- | --- |
| Case | `created`, `active`, `closed`, `cancelled`, `expired` plus a closed reason code |
| Consent | `not_requested`, `awaiting_decision`, `granted`, `declined`, `withdrawn` |
| Media assets | Per role/request: `none`, `awaiting_required`, `upload_pending`, `uploaded_quarantined`, `validated`, `rejected`, `unavailable` |
| Analysis | `not_started`, `ready`, `processing`, `awaiting_customer_action`, `complete`, `abstained`, `failed_retriable`, `failed_terminal` |
| Contractor packet | `not_ready`, `ready`, `delivery_pending`, `delivered`, `delivery_failed` |
| Customer delivery | `not_evaluated`, `policy_receipt_recorded`, `delivery_pending`, `delivered`, `delivery_failed` |
| Deletion | `not_requested`, `deletion_pending`, `verified_deleted` |

These are structural statuses, not permission or policy decisions. In A1-A2,
`policy_receipt_recorded` means only that a correctly shaped opaque receipt was
accepted. A later delivery policy owns whether the receipt may be created. The
pure state machine never authenticates an actor, establishes tenant authority,
proves external deletion, or decides diagnostic, safety, delivery, or parts
eligibility.

For A0-A2, that "receipt" is only a synthetic structural reference represented
by the immutable event ID, semantic-envelope fingerprint, closed status, and
resulting revision in the processed-event lane. It does not populate an external
receipt slot and contains no external payload, provider receipt, policy body, or
permission meaning. Only a later reviewed phase may define and store an external
receipt.

All timestamps and identifiers are injected. Transitions are idempotent,
revision-bound, auditable through payload-safe receipts, and monotonic within
each dimension except a named stage-specific retry. While deletion is
`deletion_pending`, every new non-deletion event and every late completion is
rejected; only an identical historical replay plus the closed deletion-retry and
verification events may proceed. `verified_deleted` has terminal precedence and
permits only identical replay of the terminal deletion-verification event.
`expired`, `cancelled`, a closed terminal outcome, and terminal analysis failure
also prohibit new analysis, customer actions, or delivery but still permit
deletion progress. Media state is keyed by opaque asset/request identity so a
validated symptom video can coexist with a later plate or recapture request. No
state may imply that delivery or deletion happened without its typed receipt.

### 9.2 `MediaAsset`

A1 implements only the opaque asset/request identity, media role, closed
validation status, bounded synthetic metadata/digest, and injected timestamps.
The storage reference, source channel, consent receipt, normalization, scan, and
real deletion fields below belong to Gate 2 and later; they are not A1 fields.

- opaque asset ID;
- media role: `symptom_video`, `rating_plate`, or `targeted_recapture`;
- server-detected media type;
- byte size, duration, dimensions, and digest;
- quarantine and scan state;
- normalized-object reference;
- source: secure web upload or later approved MMS ingestion;
- consent receipt/version;
- created/expiry/deletion timestamps;
- no customer-supplied filename in storage keys.

### 9.3 `EvidenceObservation`

- typed observation code;
- normalized value;
- source type: customer statement, call fact, video frame, audio interval,
  rating plate, deterministic metadata, or catalog;
- source reference and timestamp/frame range;
- extraction method/version;
- evidence strength class;
- conflicts;
- safety relevance;
- validation state.

Evidence strength is not a probability. Model self-confidence alone cannot make
an observation valid.

### 9.4 `EquipmentIdentity`

- manufacturer candidates;
- model candidates;
- serial candidates;
- label crop reference;
- per-character OCR alternatives where ambiguous;
- customer confirmation state when used;
- format-validation state;
- catalog lookup state;
- final identity state:
  `unreadable`, `ocr_candidate`, `customer_confirmed`, or `catalog_verified`.

Model and serial should not be silently corrected. Ambiguous `0/O`, `1/I`,
`5/S`, punctuation, and suffixes must remain explicit until resolved.

### 9.5 `DiagnosticHypothesis`

- supported taxonomy code;
- customer-safe label;
- contractor label;
- rank, limited to one or two;
- supporting observation references;
- conflicting observation references;
- missing onsite tests;
- evidence-strength class;
- policy version.

A hypothesis never carries customer-delivery eligibility. Diagnosis ranking and
customer delivery are separate decisions; only the later Customer Delivery
Policy may issue an opaque delivery-policy receipt.

### 9.6 `SafetyAssessment`

- observed red flags;
- reported red flags;
- unresolved red flags;
- deterministic disposition:
  `normal_triage`, `urgent_contractor`, `stop_and_escalate`, or
  `insufficient_safety_information`;
- approved customer-copy code;
- policy and reviewer version.

The model may extract a possible red flag. A deterministic policy makes the
disposition and selects the customer instruction.

### 9.7 `PendingCustomerAction`

This is neutral lifecycle plumbing, not question, safety, or delivery policy.

- opaque `request_id`;
- kind: `diagnostic_question`, `rating_plate`, or `targeted_recapture`;
- budget bucket corresponding to the kind;
- status: unresolved `issued`, `uploading`, or `submitted`; resolved question
  `answered` or `not_sure`; resolved media `fulfilled` or `unavailable`; or
  common resolved `declined`, `cannot_safely_complete`, `expired`, or
  `cancelled`;
- opaque question-library/copy ID and version when applicable;
- stable response-option codes, never rendered labels;
- BCP-47 presentation locale, which does not imply diagnostic-language support;
- optionality and safe-refusal codes;
- opaque decision and evidence references;
- issued, expiry, and resolved timestamps injected by the state-machine caller;
- no rendered prose, raw transcript, raw media, model output, token, provider
  payload, safety disposition, diagnosis, part, or delivery decision.

Only one unresolved action may exist. Replaying its issuance does not create a
new action or increment a counter. A terminal case or deletion transition clears
or invalidates it. `answered`/`not_sure` are invalid for media actions and
`fulfilled`/`unavailable` are invalid for questions. Media fulfillment requires
an exact request ID, action kind, asset ID, media-role, and final validation-state
match. Rating-plate and targeted-recapture actions have separate one-request
budgets; diagnostic questions have a two-question budget.

### 9.8 `AdaptiveQuestionDecision`

- question-library ID and version;
- eligible answer choices;
- facts already known;
- decision expected to change;
- expected information-gain class;
- selected/not-selected reason;
- question index;
- answer and timestamp.

### 9.9 `PartsCandidate`

- contractor-only part category;
- exact part number only when retrieved, never generated;
- manufacturer/source;
- source URL or catalog reference;
- source version/retrieval timestamp;
- matched model, serial/configuration rule, and supersession chain;
- compatibility state:
  `verified_compatible`, `candidate_verify`, or `no_reliable_match`;
- evidence and warnings;
- contractor confirmation state.

### 9.10 `CustomerPreliminaryDiagnosis`

- equipment display summary;
- observation bullets;
- at most two customer-safe hypotheses;
- why they fit;
- what remains unknown;
- urgency/safe-next-step copy code;
- contractor-handoff state;
- disclaimer version;
- delivery decision and reason;
- no exact serial, exact part number, raw transcript, raw internal prompt, or
  hidden score.

### 9.11 `VisualTriageEvent`

Every structural transition uses an immutable envelope:

- `schema_version`;
- `case_id` and `contractor_id` as structural binding values, not proof of
  authorization;
- globally unique opaque `event_id`;
- closed event-kind code;
- canonical payload digest, never the payload in the receipt;
- internally computed versioned semantic-envelope fingerprint;
- `expected_revision`;
- closed source-kind code;
- injected event time;
- retry stage and attempt where applicable;
- evidence scope, which is fixed to `synthetic_offline_only` for A0-A2.

A0-A2 event payloads contain only closed structural codes, bounded synthetic
values, and opaque references. They never contain raw transcript/media, contact
data, equipment identifiers, credentials, provider data, diagnosis prose, or
other sensitive content. Before real data is permitted, security/privacy review
must decide whether the digest is keyed, how its key is isolated, and how long
receipts remain. A plain digest is not treated as anonymization.

Replay and binding rules:

- the event constructor computes the digest from a versioned canonical payload
  form; it never trusts a caller-supplied digest. Canonical maps use UTF-8 JSON,
  sorted keys, closed schema fields, deterministic scalar encoding, and no NaN,
  infinity, bytes, or arbitrary object representation;
- schema and aggregate case/contractor binding are validated before event-ledger
  lookup. A binding mismatch is rejected without disclosing whether an event ID
  or decision exists;
- the semantic-envelope fingerprint is computed internally over schema version,
  case ID, contractor ID, event kind, canonical payload digest, expected
  revision, source kind, injected event time, retry stage/attempt, and evidence
  scope. The implementation serializes that closed fingerprint map with the
  same UTF-8 canonical-JSON rules above and hashes those exact bytes with
  SHA-256, rendered as lowercase hexadecimal. It is never accepted from the
  caller;
- after binding validation, replay lookup by event ID happens before revision
  comparison;
- same event ID and same complete semantic-envelope fingerprint: accepted no-op
  returning `replayed=true`, the historical decision code and original resulting
  revision, plus the distinct current aggregate revision and current safe
  projection. Historical acceptance is not current permission;
- terminal precedence still applies to replay: while deletion is pending an
  identical historical replay may return the nonmutating projection above, but
  after verified deletion only identical replay of `deletion_verified` is
  accepted;
- same event ID with any semantic-envelope mismatch: conflict rejection;
- stale revision, impossible order, or skipped prerequisite: rejection without
  mutation;
- only the first committed issuance increments a customer-effort counter;
- a transport retry reuses the event and does not consume another counter;
- a late completion after cancellation, expiry, terminal failure, deletion
  request, or verified deletion is rejected;
- a deletion receipt is structural and must be explicitly labeled synthetic and
  nonauthorizing in A2; it cannot prove that any external copy was removed.

The A1 contract has two fixed, non-evicting lanes. The ordinary lane holds at
most 64 committed receipts. A separate six-receipt terminal/control lane is
selected solely by a hard-coded event-kind table, never a caller flag or payload:
zero or one of `consent_declined`, `consent_withdrawn`, `case_closed`,
`case_cancelled`, or `case_expired`, followed when applicable by
`deletion_requested`, at most three `deletion_retry_recorded` events, and
`deletion_verified`. These mutually exclusive source-state rules make six
sufficient for every valid path. Ordinary events fail closed at their ceiling
without consuming the control lane.
Identical replay consumes no slot; rejected events consume no slot; neither lane
evicts. A fourth deletion retry and every unreachable control sequence reject.
Other named retry stages also permit at most three retry events after the initial
failed attempt; later policy may choose a lower limit but not a higher one.

The minimum transition matrix is:

| Event | Required structural precondition | State effect | Additional invariant |
| --- | --- | --- | --- |
| `case_created` | No aggregate exists | Case `created`, revision 1 | One case/tenant binding for life; no implicit consent or media request |
| `consent_requested` | Case `created`, consent `not_requested`, deletion `not_requested`, no media/action | Consent `awaiting_decision` | No diagnostic/media action may coexist |
| `consent_granted` | Case `created`, consent awaiting, deletion not requested | Consent granted; case active; one initial-video slot opens | Ordinary lane; no action before grant |
| `consent_declined` | Case `created`, consent awaiting, deletion not requested | Consent declined; case cancelled | Control lane; blocks new work |
| `consent_withdrawn` | Case active, consent granted, deletion not requested | Consent withdrawn; case cancelled | Control lane; invalidates pending action |
| `media_action_issued` | Case active, consent granted, deletion not requested, no pending action; opaque synthetic later-action receipt after analysis, or closed initial-video rejection for first recapture only | Pending plate/recapture; analysis awaiting action | A2 validates kind/reason/receipt shape only; corresponding count at most 1 |
| `diagnostic_question_issued` | Case active, consent granted, deletion not requested, no pending action, analysis complete/abstained, opaque synthetic later-action receipt | Pending diagnostic question; analysis awaiting action | A2 validates receipt shape only; question count at most 2 |
| `customer_action_resolved` | Matching unresolved action and kind-valid answer/refusal | Clear action; analysis ready or abstained by closed outcome code | Media success cannot use this event; stale/mismatch rejected |
| `upload_started` | Case active, consent granted, deletion not requested, and matching issued media action or unused initial-video slot | Media upload pending; matching issued action, if any, becomes uploading | Initial slot creates no pending action, commits once; retry consumes no budget |
| `upload_finalized` | Matching upload pending, case active, deletion not requested | Media uploaded/quarantined; matching issued action, if any, becomes submitted | Duplicate completion is no-op |
| `media_validated` | Matching quarantined asset, case active, deletion not requested | Media validated/rejected | Initial validation makes analysis ready/abstained; issued action stays unresolved |
| `media_action_resolved` | Matching submitted plate/recapture action, final asset validation, case active, deletion not requested | Fulfilled/unavailable; clear action; analysis ready/abstained | Request, kind, asset, role, and validation must match |
| `analysis_started` | Analysis ready, consent granted, deletion not requested, no unresolved action, validated symptom evidence media (initial video or targeted recapture) | Analysis processing | Does not authorize provider I/O |
| `analysis_retry_recorded` | Case active, analysis failed retriable, deletion not requested, retry 1-3 | Analysis ready | Fourth retry rejected |
| `analysis_completed` | Case active, analysis processing, deletion not requested | Analysis complete/abstained | No customer-delivery implication |
| `analysis_failed` | Case active, analysis processing, deletion not requested | Analysis failed retriable/terminal | Terminal failure blocks new action/delivery; deletion remains allowed |
| `contractor_packet_ready_recorded` | Case active, analysis complete/abstained, deletion not requested, no pending action | Packet ready | Structural readiness only in A2 |
| `contractor_packet_delivery_started` | Packet ready, case active, deletion not requested, no pending action | Packet delivery pending | A2 performs no send or authority decision |
| `contractor_packet_delivery_receipt_recorded` | Packet delivery pending, case active, deletion not requested | Packet delivered/failed | Opaque receipt only in A2 |
| `delivery_policy_receipt_recorded` | Analysis complete/abstained, case active, deletion not requested, no pending action | Policy receipt recorded | Policy meaning supplied only by A5 |
| `customer_delivery_started` | Matching opaque policy receipt, case active, deletion not requested, no pending action | Delivery pending | A2 validates structural binding only; receipt does not grant authority |
| `customer_delivery_receipt_recorded` | Delivery pending, case active, deletion not requested | Delivery delivered/failed | Duplicate send receipt cannot resend |
| `case_closed` | Case active, deletion not requested, quiescent across action, upload/quarantine, analysis/retry, packet delivery, and customer delivery | Case closed with closed reason | Control work may not be in flight; deletion remains allowed |
| `case_cancelled` / `case_expired` | Case created/active, deletion not requested, not already terminal | Case terminal; invalidate pending action | Exactly one terminal control event; deletion remains allowed |
| `deletion_requested` | Deletion not requested, case not verified deleted | Deletion pending; invalidate pending action | Control lane; freezes all new non-deletion work |
| `deletion_retry_recorded` | Deletion pending, retry attempt exactly 1-3 | Deletion remains pending | Control lane; attempt 4 rejected |
| `deletion_verified` | Deletion pending | Verified deleted | Synthetic nonauthorizing receipt in A2; later phases require typed all-copies proof |

The implementation plan may split event types more finely, but it may not weaken
these bindings, replay rules, budgets, or terminal precedence.

### 9.12 Trust Classes And Serialization Projections

Every field has one declared trust class: closed code, opaque reference, version,
timestamp, bounded synthetic fact, untrusted candidate, sensitive internal fact,
or opaque policy/external receipt. Schemas reject unknown fields recursively and
bound every string, list, and map. Validation errors, `repr`, and `str` must be
payload-safe.

Four projections are distinct and deny by construction:

1. **Internal structural form**: only the minimum state, opaque references, and
   receipts required for orchestration; no raw media, raw transcript,
   credentials, provider bodies, or arbitrary prose. A1 represents analysis
   artifacts and candidate sets only by opaque reference; candidate content is
   added only by its later reviewed contract.
2. **Customer-safe structural form**: status, reason/copy codes, approved-field
   references, and next-action codes only. A1 does not implement final customer
   diagnosis semantics.
3. **Contractor-safe structural form**: provenance/status codes and opaque asset
   references only until the contractor packet task defines its reviewed view.
4. **Audit form**: allowlisted codes, versions, timestamps/buckets, revisions,
   event IDs/digests, and opaque correlation references only.

Every safe projection gives deletion state precedence. During
`deletion_pending`, it exposes no next action and never implies an in-flight
packet/customer delivery completed. A replay projection reports historical and
current revisions separately and cannot recreate a cleared action.

Recursive tests seed synthetic canaries for transcripts, phones, addresses,
tokens, model/serial values, provider IDs and URLs, diagnoses, answers, prices,
repair instructions, and exact parts. None may appear in audit output, logs,
exceptions, validation errors, `repr`, or `str`. Customer output additionally
rejects serials, exact parts, price, repair instructions, arbitrary copy, raw
evidence, hidden scores, and unapproved delivery fields.

## 10. Equipment And Parts Resolution

Equipment recognition and parts compatibility are different problems.

### 10.1 Identity sequence

1. Detect and crop a probable rating plate.
2. Run OCR with character alternatives and geometry retained.
3. Parse manufacturer/model/serial without inventing missing characters.
4. Validate format against a versioned manufacturer parser where available.
5. Ask for one targeted plate image only if resolving the identity changes the
   diagnosis or contractor preparation.
6. Query an approved authoritative source.
7. Mark identity `catalog_verified` only after the source accepts the required
   identifier combination.

### 10.2 Parts sequence

1. Use verified equipment identity.
2. Query approved OEM or distributor data; do not search arbitrary web pages as
   the authoritative production path.
3. Apply serial ranges, options, capacity, voltage, revision, and supersession.
4. Preserve the source and retrieval time.
5. Return `verified_compatible` only when deterministic rules match.
6. Otherwise return `candidate_verify` or `no_reliable_match`.
7. Let the contractor choose what to bring.

The implementation must account for prior field replacements: the currently
installed component can differ from the original bill of materials. A verified
catalog match means compatible with the identified configuration, not proof that
the part is currently installed or failed.

## 11. Media Processing

The upload path must not repeat the current direct-body/inline-provider design.

Required controls:

- short-lived, high-entropy, hashed, one-time, case-scoped bootstrap
  authorization fixed to its contractor/call/caller purpose;
- immediate bootstrap-token exchange for an `HttpOnly`, `Secure`, appropriate
  `SameSite` customer session so a long-lived bearer does not remain in browser
  history, referrers, logs, caches, or subsequent URLs;
- separate capabilities for upload, customer status/result read, contractor
  access, and deletion; each capability must be revocable;
- real typed HTTP behavior for missing, expired, used, or revoked capabilities,
  including reviewed `404` versus `410` disclosure semantics;
- server-enforced upload count, role, byte, duration, dimension, and rate limits;
- detection by magic bytes and safe decode, not trust in `Content-Type` or file
  extension;
- generated object names and private object storage;
- quarantine until validation and scanning complete;
- image decode/re-encode and metadata stripping where supported;
- video demux/decode in an isolated bounded worker;
- no serving customer uploads as executable or active content;
- no public bucket or stable bearer URL;
- checksum and idempotency handling;
- explicit deletion for original, normalized, derived, Twilio, provider-file,
  cache, and record copies;
- lifecycle rules as defense in depth, not a replacement for deletion jobs;
- provider request size compatible with current official limits;
- provider credentials sent only in approved authentication headers and loaded
  from managed secrets, never placed in request URLs or client code;
- provider files deleted after processing unless an approved retention need is
  documented;
- one short video per analysis request for the MVP;
- custom frame selection or frame rate appropriate for fan motion and vibration;
- separate bounded audio analysis where useful;
- no raw media or base64 in logs, exceptions, traces, analytics, or fixtures.

Google's current video guidance says inline video is suitable for short requests
under 20 MB and recommends file-based input for larger or reusable media. The
current backend accepts up to 50 MB and sends bytes inline; the new path must use
a provider-compatible file/Cloud Storage flow or reduce the enforced limit.

## 12. Customer Delivery Policy

Automatic customer delivery is a separate deterministic policy decision. It
must default to `hold`.

`deliver` requires all of:

- supported contractor, trade, scenario, equipment class, and jurisdiction;
- valid consent and active case;
- validated media and complete processing;
- no unresolved safety red flag;
- no prompt-injection/content-control violation;
- schema-valid and semantically valid observations;
- sufficient non-conflicting evidence for one or two supported hypotheses;
- question count within budget;
- customer copy assembled only from approved fields/templates;
- no exact parts, repair instructions, binding price, or guaranteed outcome;
- SMS/contact gate approved, if SMS is the delivery channel;
- idempotent delivery receipt.

The policy returns one of:

- `deliver_preliminary_diagnosis`;
- `deliver_abstention`;
- `contractor_only_review`;
- `safety_stop`;
- `hold_delivery_failure`.

A generative model cannot override this decision.

## 13. Safety Contract

Before customer-facing implementation, a licensed HVAC review panel must approve:

- the supported fault taxonomy;
- evidence required for each hypothesis;
- conflicting evidence and abstention rules;
- the safety red-flag taxonomy;
- customer safe-next-step copy;
- onsite-test language;
- question library;
- equipment capture instructions;
- claims and disclaimer language;
- qualification fixtures and final release gates.

At minimum, the safety taxonomy must handle reported or observed smoke, sparks,
arcing, burning odor, severe vibration, exposed wiring, active fire, unsafe water
near electrical equipment, and any state in which approaching or operating the
unit may increase risk.

Only versioned, reviewed copy can tell a customer what to do. The model must not
write novel safety or repair instructions. When the exact safe action is not
approved, Kevin must stop the diagnostic flow and direct the customer to the
contractor or appropriate emergency/manufacturer guidance without improvising.

## 14. Privacy, Consent, And Data Governance

Customer media can contain faces, voices, addresses, property interiors,
license plates, serial numbers, paperwork, location metadata, and bystanders.
Treat raw and derived media, transcript context, equipment serials, diagnoses,
and contractor feedback as sensitive tenant-scoped data.

Required before any real media:

- an approved just-in-time consent and privacy notice;
- disclosed provider categories and purpose;
- no provider/data-sharing opt-in for model improvement;
- confirmation that the production project is on approved paid data terms and
  logging/retention settings;
- data inventory and processor/subprocessor update;
- explicit retention periods for originals, derived assets, results, and ground
  truth;
- tenant-scoped read, export, correction, and deletion paths;
- deletion orchestration across GCS, Firestore, Twilio media/message resources,
  Gemini/provider file resources, caches, backups where applicable, and derived
  artifacts;
- contractor visibility and customer deletion request handling;
- a policy for incidental third-party and prohibited content;
- no use for training or unrelated product analytics without separate consent;
- payload-safe audit events and aggregate telemetry only;
- access logs without raw tokens, media URLs, transcripts, diagnoses, serials,
  addresses, or phone numbers.

If direct MMS is later supported, Kevin must authenticate retrieval from Twilio,
copy only validated media into the controlled quarantine path, and delete the
Twilio media/message content according to the approved policy. Storing a Twilio
media URL is not ingestion, retention control, or diagnosis.

## 15. Current Source Gap Matrix

Current facts are source-only at the bound baseline. They do not prove deployed
or released behavior.

| Current source | Gap against this design | Required direction |
| --- | --- | --- |
| `app/services/post_call.py` can append a photo/video estimate link. | Link creation is tied to existing estimate and SMS behavior, not a visual-triage case or delivery policy. | Introduce separate visual-diagnosis orchestration and gates; keep old behavior default-off. |
| `app/api/estimates.py` creates a 48-hour bearer token. | One token covers public status/upload behavior; lifecycle and deletion are incomplete. | Split scoped capabilities, hash tokens, bind state transitions, and orchestrate deletion. |
| `/upload-url` enforces type/count while `/upload` can be called directly. | MIME and nominal three-upload checks can be bypassed. | Enforce every rule at the ingest boundary and object finalization. |
| Images up to 10 MB and videos up to 50 MB are read by the app and sent inline. | Memory pressure, provider limit mismatch, and no quarantine/normalization. | Direct private object upload plus isolated normalization and provider-compatible file flow. |
| `analyze_media(..., text_description="")` supports text in its signature. | Upload orchestration never passes the call/customer explanation. | Use structured call facts and bounded transcript context as first-class evidence. |
| Gemini returns one free-text diagnosis, price range, and self-reported confidence. | No evidence provenance, calibrated policy, top-two taxonomy, safety decision, or parts grounding. | Separate typed observation, hypothesis, safety, delivery, and parts contracts. |
| Medium/high model confidence can be texted automatically. | Model confidence directly influences customer contact without contractor/safety policy. | Deterministic default-hold Customer Delivery Policy. |
| Existing result can include an automatic cost range. | Price can be misleading before onsite diagnosis and parts/labor verification. | Exclude customer price from MVP. |
| Firestore stores status/result and token expiry limits access. | Expiry is not deletion; raw/derived/provider retention is not orchestrated. | Explicit retention and verified deletion across every copy. |
| MMS handler stores body and up to ten Twilio media URLs. | No media retrieval, validation, analysis, contractor UI, reply, or deletion. | Keep out of MVP or route through the same hardened ingest contract later. |
| iOS has no visual-diagnosis client or attachment view. | Contractor cannot review evidence or confirm outcome in the app. | Add authenticated contractor packet and feedback only after backend contracts pass. |
| Unit tests stub model analysis. | No real HVAC video accuracy, safety, parts, latency, or provider qualification. | Build expert-labeled, privacy-safe offline qualification before any customer data. |

## 16. Evaluation Contract

Evaluation must separate capability, safety, product usefulness, and economics.

### 16.1 Dataset requirements

Before connected or customer testing, create a rights-cleared fixture set with:

- licensed-HVAC-expert labels;
- actual equipment identity and label transcription;
- actual onsite diagnosis and part where available;
- supported and confusable fault categories;
- normal/no-fault and out-of-scope cases;
- explicit safety-red-flag cases;
- readable, partially readable, dirty, occluded, corroded, and ambiguous labels;
- varied lighting, distance, camera motion, background noise, accents, and
  supported languages;
- adversarial content, prompt injection in speech/text/labels, malformed files,
  duplicate/replayed uploads, and cross-tenant cases;
- no real customer PII unless separately approved and consented.

Train/development and sealed qualification sets must be separate. The release
set must not be used for prompt tuning.

### 16.2 Required metrics

- manufacturer exact match;
- model exact match, including suffix;
- serial exact match and ambiguous-character abstention;
- catalog identity verification precision;
- top-one and top-two agreement with onsite ground truth;
- dangerous under-triage rate;
- unsafe instruction rate;
- unsupported customer-delivery rate;
- appropriate abstention rate by evidence quality;
- exact-part provenance and compatibility precision;
- hallucinated/generated exact-part count;
- question count, repeat-question count, and decision-changing-question rate;
- media recapture rate;
- contractor accept/edit/reject rate;
- dispatch-priority agreement;
- first-visit preparation usefulness;
- customer comprehension and trust;
- p50/p95 latency and per-case provider/compute cost;
- retention/deletion completion and tenant-isolation failures.

### 16.3 Provisional hard gates

These gates are deliberately conservative and must be ratified by the HVAC,
security/privacy, and product reviewers before customer release:

- zero customer-visible repair, electrical, refrigerant, panel-opening, or
  moving-component instructions outside the approved copy library;
- zero automatic customer deliveries for out-of-scope, unresolved-safety,
  malformed, schema-invalid, semantically invalid, or conflicting cases;
- zero cases exceeding two diagnostic questions;
- zero repeated questions whose answer was already available;
- zero exact part numbers generated by a model;
- 100% provenance coverage for exact parts shown to contractors;
- 100% tenant-isolation and token-replay negative tests passing;
- 100% deletion workflow completion in the qualification environment, including
  failure/retry cases;
- zero raw media, transcript, serial, phone, address, token, or diagnosis text in
  application logs and analytics fixtures;
- zero dangerous under-triage events in the sealed release qualification set;
- numeric OCR and top-two agreement thresholds remain `TBD-domain-ratification`
  rather than being invented without a representative dataset and licensed-HVAC
  signoff.

Zero observed failures in a small dataset is not proof of zero risk. The review
must consider sample size, confidence intervals, class coverage, and severity.

## 17. Rollout Gates

### Gate 0: A0-A2 exact-document and isolation entry

This gate authorizes no work by itself. Entry to a separately owner-authorized
`synthetic_offline_A0_A2` slice requires all of:

- a detached exact-hash staff/security/privacy/Product-UX receipt with no P0/P1;
- a separate trusted-session owner message approving those exact hashes for
  `synthetic_offline_A0_A2`;
- the bound baseline and exact changed-path allowlist verified;
- credential-clean worktree, enforceable denied egress, and executable
  pre-import isolation checks;
- security approval only of the A0-A2 structural isolation, projections, digest
  nonclaims, and synthetic-data boundary.

Licensed-HVAC taxonomy/copy ownership begins at A3. The real-media threat model,
retention/deletion design, and provider path/terms block Gate 2 or Gate 3 as
named below, not Gate 1. Real data, language/channel, customer delivery,
quantitative thresholds, and production remain later independent gates.

### Gate 1: Pure offline contracts

- provider-neutral structural schemas, orthogonal state vector, neutral pending
  action, immutable event envelope, projections, and state machine;
- synthetic fixtures only;
- no live pipeline imports, SMS, provider, storage, or customer data;
- no rendered customer copy or safety/delivery/question/parts policy semantics;
- replay, conflict, terminal-precedence, customer-effort budget, projection, and
  isolation negative tests pass;
- a detached exact-diff approval receipt binds the baseline, all eight allowlisted
  document/source/test hashes and byte counts, allowed paths, and
  staff/security/product-UX decision.

### Gate 2: Hardened local/staging-like ingestion with synthetic media

- approved real-media threat model and explicit retention/deletion design before
  storage or ingestion implementation;
- quarantine, validation, normalization, limits, deletion, and tenant ownership;
- fake/provider-stub analysis only;
- abuse and failure injection pass;
- gates remain disabled.

### Gate 3: Provider qualification with rights-cleared fixtures

- approved provider path, region, data terms, logging/retention configuration,
  and provider/catalog boundary;
- explicit provider authorization;
- paid project and logging/data-sharing settings verified;
- sealed model/configuration manifest;
- equipment/OCR/observation qualification;
- no real customer data;
- results remain diagnostic evidence, not customer capability proof.

### Gate 4: Contractor-only shadow pilot

- opt-in contractor accounts;
- customer consent for media;
- no automatic preliminary diagnosis sent to customers;
- contractors label onsite ground truth;
- safety, identity, top-two, parts, latency, and usefulness thresholds reviewed.

### Gate 5: Bounded customer-result pilot

- separate owner approval;
- automatic delivery only for the supported low-risk policy envelope;
- no customer price or exact parts;
- low-volume allowlist, kill switch, rollback, and daily review;
- A2P/SMS compliance and delivery receipts proven if SMS is used;
- no App Store or broad marketing claims.

### Gate 6: General availability decision

- representative outcome evidence;
- validated unit economics and operational support;
- privacy/deletion SLA demonstrated;
- incident and rollback runbooks approved;
- customer claims limited to demonstrated scope.

Passing one gate never implies permission for the next.

## 18. Feature Gates And Isolation

Do not overload the existing estimate flags to enable the new product. Proposed
separate controls are:

- `visual_diagnosis_case_create`
- `visual_diagnosis_media_process`
- `visual_diagnosis_provider_analyze`
- `visual_diagnosis_parts_lookup`
- `visual_diagnosis_contractor_packet`
- `visual_diagnosis_customer_delivery`
- `visual_diagnosis_feedback_capture`

All default off. Provider analysis and customer delivery require separate gates.
The customer-delivery gate must also enforce channel compliance, policy-envelope
eligibility, and idempotency. No prompt or client flag can override backend gates.

## 19. Observability

Allowed aggregate events include:

- case state transition;
- media role, bounded size/duration bucket, and validation outcome;
- supported-scenario code;
- observation/hypothesis taxonomy codes;
- equipment identity state;
- question-library ID and count;
- delivery-policy decision code;
- abstention/failure reason code;
- catalog source ID and compatibility state;
- latency/cost buckets;
- deletion workflow state;
- contractor feedback category.

Do not log raw or truncated media, base64, media URLs, bearer tokens, transcript
text, free-text diagnosis, customer copy, exact serial/model, part number, phone,
address, names, or provider prompt/response bodies.

## 20. Failure And Recovery

- Upload failure: preserve a bounded retry without consuming additional upload
  count until object finalization succeeds.
- Invalid media: reject safely and allow one replacement within the case budget.
- Provider timeout/failure: return typed abstention; never send partial output.
- OCR ambiguity: request one targeted photo only if decision-changing; otherwise
  mark identity unverified.
- Catalog failure: omit exact parts and retain contractor-only category guidance.
- Delivery failure: keep diagnosis state separate from channel receipt; do not
  claim the contractor/customer received it.
- Deletion failure: retry idempotently, alert using safe identifiers, and keep the
  deletion dimension in `deletion_pending`; do not record `verified_deleted`
  without the typed all-copies receipt.
- Elevated safety or delivery-policy regression: disable customer delivery first,
  then provider processing if needed, while preserving deletion and audit jobs.
- Rollback: gates default off and old estimate flags remain unchanged.

## 21. Open Decisions

These do not block writing pure contracts but block the named later gates:

1. Which licensed HVAC reviewers approve the scenario taxonomy, questions,
   customer copy, and qualification set? Blocks Gate 3.
2. Which paid Gemini/Vertex deployment and logging/retention configuration is
   approved for customer media? Blocks Gate 3.
3. Which OEM/distributor sources provide lawful, stable, authoritative model,
   serial, parts, and supersession data? Blocks production parts lookup.
4. What are the exact media/result/ground-truth retention periods? Blocks Gate 2.
5. Will the secure web uploader live in this repo, the `heykevin.one` site repo,
   or a separately owned frontend? Blocks end-to-end staging.
6. Is customer delivery via SMS link, web result, iOS, or more than one channel?
   Blocks Gate 5.
7. Which safe no-tool customer steps, if any, may contractors configure? Blocks
   customer copy approval.
8. Which languages are allowed for the first pilot, and how will domain review
   validate translations? Blocks Gate 5 for non-English delivery.
9. What representative sample size and numeric OCR/top-two thresholds will the
   domain and statistical reviewers ratify? Blocks Gate 4 exit.

## 22. Approval Record

- Owner documentation-amendment approval: **recorded 2026-08-12; documents only**
- Owner product approval: **recorded for synthetic-offline A0-A2 only**
- Licensed HVAC domain approval: **pending**
- Security/privacy approval: **pending**
- Staff architecture review: **approve with conditions, incorporated
  2026-08-11; A0-A2 only**
- A0-A2 exact-diff staff/security/product-UX receipt: **required externally for
  the current eight-path bytes; advisory only**
- Implementation authorization: **recorded for synthetic-offline A0-A2 only**
- Provider-connected qualification authorization: **closed**
- Real-customer data authorization: **closed**
- Staging authorization: **closed**
- Production/customer-delivery authorization: **closed**

Any edit to product scope, customer-delivery policy, safety taxonomy, data
lifecycle, provider, parts source, or bound baseline requires targeted re-review.

## 23. References

Repository:

- `app/api/estimates.py`
- `app/services/ai_estimate.py`
- `app/services/post_call.py`
- `app/webhooks/twilio_incoming.py`
- `app/services/gated_actions.py`
- `docs/security/phase0-side-effect-matrix.md`
- `docs/security/phase0-release-readiness.md`
- `docs/superpowers/specs/2026-06-30-business-first-dispatch-v2-design.md`
- `docs/superpowers/specs/2026-07-09-hybrid-stateful-ai-receptionist-design.md`

Current external primary guidance, to be re-verified at implementation time:

- Gemini video understanding:
  https://ai.google.dev/gemini-api/docs/video-understanding
- Gemini Files API:
  https://ai.google.dev/api/files
- Gemini data logging and sharing:
  https://ai.google.dev/gemini-api/docs/logs-policy
- Gemini API paid-services data terms:
  https://ai.google.dev/gemini-api/terms
- Twilio Messaging API and media resources:
  https://www.twilio.com/docs/messaging/api
- Twilio message/media deletion:
  https://help.twilio.com/articles/223133687-Deleting-messages-message-media-or-message-bodies
- OWASP File Upload Cheat Sheet:
  https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html
- Carrier model/serial and replacement-parts guidance:
  https://www.carrier.com/us/en/residential/homeowner-resources/hvac-parts/
