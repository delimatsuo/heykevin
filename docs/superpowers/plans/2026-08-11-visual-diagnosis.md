# Hey Kevin Visual Diagnosis Implementation And Qualification Plan

Date: 2026-08-11; A0-A2 contract amendment 2026-08-12
Status: Rebased A0-A2 implementation draft; exact-diff re-review required before exit
Bound baseline: `d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`
Companion design: `docs/superpowers/specs/2026-08-11-visual-diagnosis-design.md`

## 1. Goal

Develop a safe, visual-first HVAC diagnosis feature in which a customer submits
one short narrated video and, only when needed, one rating-plate photo. Kevin
reuses the call facts, asks zero questions normally and no more than two bounded
questions, sends an evidence-linked preliminary diagnosis to eligible customers,
and prepares a richer equipment/parts packet for the contractor.

The first supported symptom family is:

```text
trade: hvac
equipment: residential split-system outdoor unit
scenario: not_cooling_with_outdoor_unit_noise
```

This plan replaces extension of the current synchronous estimate prototype with
a separately gated, stateful visual-diagnosis workflow.

## 2. Current Authorization

The owner's later 2026-08-12 `approved` direction authorizes the isolated,
synthetic-offline A0-A2 implementation on the branch/worktree recorded in the
continuation handoff. It does not authorize A3-A8, providers, real data, live
delivery, staging, production, or deployment.

This plan does **not** authorize:

- implementation outside the A0-A2 synthetic slice;
- creating or switching branches or worktrees outside the recorded isolated one;
- connected Gemini, Twilio, GCS, OEM, or distributor operations;
- use of real customer media or transcripts;
- enabling any gate or sending any message;
- staging, production, TestFlight, App Store, IAM, Secret Manager, or deployment
  changes;
- external parts-data agreements or purchases.

The owner has explicitly approved the exact reviewed package for scope
`synthetic_offline_A0_A2`; implementation is limited to this isolated,
synthetic-offline branch. Each connected or customer-visible phase requires its
own approval as listed below.

The exact approval unit is a detached receipt containing the bound baseline,
full SHA-256 hashes and byte counts of all eight allowlisted documents/source/
test paths, the changed-path allowlist, and the staff/security/product-UX decision. Review or approval of
earlier document bytes does not authorize amended bytes.

A first exact-hash review of the amendment found zero P0 in the bounded offline
slice but rejected the bytes for residual P1 transition, capacity, Gate 0, and
pre-import-proof defects. One staff/security/privacy/Product-UX debate round
unanimously supported the consolidated repair now in this revision. The repaired
bytes still require a new exact-hash review and advisory receipt.

During that review, `origin/main` advanced from
`fecec74220b16c561545d175e291be24058f5ad4` to the bound commit above. A
documentation-only source-gap audit found seven subscription-incident-fix paths:
the deploy and rollback workflows, subscription API/configuration, iOS paywall,
and two subscription/release tests. None of the inspected visual-diagnosis gap
paths, `Dockerfile`, `app/main.py`, or `pyproject.toml` changed. The workflow
hashes below were recomputed from the newer Git object. Local `HEAD` and `main`
remain intentionally unmoved at the earlier commit.

After A0-A2 implementation began, `origin/main` advanced again from
`7e9550b5f0792576709a671aa901bc3b0af29c3f` to
`d09c58f19db5a0054945628ba0a24e67747f8341` (PR #163). The rebaseline audit
found twelve changed subscription-verification paths: backend subscription
API/service, iOS project and subscription clients, the new iOS verification
source/test, and two subscription unit-test files. No visual-diagnosis path,
`Dockerfile`, `app/main.py`, `pyproject.toml`, or deploy/rollback workflow
changed. The isolated implementation branch was fast-forwarded to the new
bound baseline; primary `main` remains unmoved.

During the review of this rebaseline, `origin/main` advanced once more from
`d09c58f19db5a0054945628ba0a24e67747f8341` to
`d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`. The two-path delta only changes
iOS project/version metadata; no visual-diagnosis path, backend source,
`Dockerfile`, `app/main.py`, `pyproject.toml`, or deploy/rollback workflow
changed. The branch was fast-forwarded again and this is the current bound
baseline.

## 3. Review Decision

An independent staff review on 2026-08-11 returned **approve with conditions**
for documentation and branch preparation. The highest-risk issue was P0:
automatic customer-facing diagnosis can convert an unvalidated model opinion
into safety-relevant HVAC advice.

Required conditions incorporated here:

1. claim and safety contracts precede model or UI work;
2. the workflow separates ingestion, observation, safety, diagnosis, questions,
   parts, contractor packet, and customer delivery;
3. model confidence never authorizes delivery;
4. parts remain grounded, contractor-facing, and non-ordering;
5. release proceeds from synthetic offline work to contractor-only shadow mode
   before any automatic customer result;
6. media security, consent, retention, deletion, export, and provider terms are
   proven before real media;
7. licensed-HVAC review and a sealed, representative qualification set are
   required before a customer pilot.

## 4. Branch And Worktree Contract

The exact-document review and owner implementation approval have already been
completed for the recorded isolated worktree. Remote refresh is outside A0-A2.
The bootstrap command below is historical provenance only and must not be run
again:

The following bootstrap command is retained as historical provenance only; the
approved worktree already exists and must not be recreated.

```bash
cd '/Volumes/Extreme Pro/MYPROJECTS/Kevin'
git status --short --branch
test "$(git rev-parse origin/main)" = \
  d2a2f003134a66b35cd76cabb8c2aaa43ca184f5
git cat-file -e d2a2f003134a66b35cd76cabb8c2aaa43ca184f5^{commit}
test ! -e '/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis'
test -z "$(git branch --list codex/visual-diagnosis)"
git worktree add \
  '/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis' \
  -b codex/visual-diagnosis \
  d2a2f003134a66b35cd76cabb8c2aaa43ca184f5
```

The existing worktree must be checked against the detached receipt before any
import or test. Do not reset, clean, move, or stage unrelated files. Any source
or document-byte mismatch invalidates the receipt and requires rebaseline and
fresh review. The primary checkout remains intentionally unmoved.

The implementation worktree must not contain untracked/local `.env`, credential,
token, provider-payload, customer-data, or active cloud-configuration material.
Tracked source and inert examples remain allowed. Run A0-A2 with provider and
cloud credential variables unset and network egress denied. Do not install or
update tools or dependencies over the network under this authority.

If `origin/main` changes before the fresh exact-diff A0-A2 exit review, stop and
rebaseline the design, source-gap matrix, and plan. Do not silently substitute a
new SHA.

## 5. Architectural Sequence

```text
Phase A: contracts and policies, synthetic/offline only
  -> Phase B: hardened ingest/storage, synthetic only
  -> Phase C: provider qualification, rights-cleared fixtures only
  -> Phase D: contractor-only shadow pilot
  -> Phase E: bounded automatic customer-result pilot
  -> Phase F: general availability decision
```

Passing a phase never authorizes the next phase.

The first branch agent may target **Tasks A0-A2 only**, and only after the exact
owner approval described above. The later Tasks A3-A8 are not part of that
authority even though they are documented in the same phase.

## 6. Proposed Ownership Boundaries

Names are proposed and may be refined in an approved plan amendment. Preserve
the responsibilities even if filenames change.

| Boundary | Proposed source | Responsibility |
| --- | --- | --- |
| Domain contracts | `app/services/visual_diagnosis_contracts.py` | Typed, provider-neutral schemas and enums only. |
| Case state machine | `app/services/visual_diagnosis_state.py` | Orthogonal, revision-bound, idempotent structural transitions. |
| Safety policy | `app/services/visual_diagnosis_safety.py` | Deterministic disposition and approved-copy codes. |
| Delivery policy | `app/services/visual_diagnosis_delivery.py` | Default-hold customer decision; no provider calls. |
| Question planner | `app/services/visual_diagnosis_questions.py` | Reviewed library, information-gain eligibility, hard budget. |
| Equipment identity | `app/services/equipment_identity.py` | OCR candidate normalization and verification states. |
| Parts compatibility | `app/services/parts_compatibility.py` | Authoritative-source interface and deterministic compatibility. |
| Media validation | `app/services/visual_media_validation.py` | Magic-byte/decode/metadata/duration/limit policies. |
| Media storage | `app/services/visual_media_store.py` | Private object lifecycle behind an injected interface. |
| Model adapter | `app/services/visual_diagnosis_provider.py` | Provider request/response mapping; never policy or delivery. |
| Orchestrator | `app/services/visual_diagnosis_orchestrator.py` | Idempotent stage coordination; no direct customer text. |
| Persistence | `app/db/visual_diagnosis.py` | Tenant-bound case/result/receipt persistence. |
| API | `app/api/visual_diagnosis.py` | Capability exchange, upload finalization, customer status, contractor APIs. |
| Evaluation | `scripts/evaluate_visual_diagnosis.py` | Offline corpus evaluation and signed manifest output. |
| iOS contractor UI | `ios/Kevin/Views/VisualDiagnosisDetailView.swift` | Authenticated packet review and outcome feedback. |
| Customer capture | location unresolved | Consent, video/plate capture, bounded questions, result display. |

Do not add visual-diagnosis logic to `app/services/ai_estimate.py` as a prompt
expansion. Keep the current estimate path default-off while the new contracts
are developed and qualified.

## 7. Phase A: Pure Offline Contracts And Policies

Current authority ceiling after later exact approval: Tasks A0-A2 only.
Authority required: trusted-session owner approval of the exact design/plan
hashes for `synthetic_offline_A0_A2`; the advisory panel receipt is not authority.
Data allowed: synthetic fixtures only.
External I/O: none.
Live imports/wiring: prohibited.

### Task A0: Context lock and exact baseline audit

1. Bind repo, worktree, branch, baseline, dirty state, and newest user request.
2. Re-read `AGENTS.md`, design, plan, handoff, Phase 0 security documents, and
   current estimate/MMS implementation.
3. Verify the detached approval receipt against the exact bytes of all eight
   controlling documents before transferring them.
4. Produce a source-gap audit tied to the exact SHA as detached captured-session
   evidence outside the repository and changed-path allowlist.
5. Prove no documentation or baseline live-entrypoint file was silently
   superseded.
6. Verify the allowed path set and credential-clean, denied-egress test
   environment before importing candidate modules.
7. Verify trusted-session owner approval separately from the advisory receipt;
   matching receipt bytes never establish implementation authority.

Commands:

```bash
git status --short --branch
git rev-parse --show-toplevel
git rev-parse HEAD
git diff --stat
git diff --cached --stat
shasum -a 256 \
  docs/superpowers/specs/2026-08-11-visual-diagnosis-design.md \
  docs/superpowers/plans/2026-08-11-visual-diagnosis.md \
  docs/handoffs/2026-08-11-visual-diagnosis-handoff.md \
  docs/handoffs/2026-08-11-visual-diagnosis-new-session-prompt.md \
  app/services/visual_diagnosis_contracts.py \
  app/services/visual_diagnosis_state.py \
  tests/unit/test_visual_diagnosis_contracts.py \
  tests/unit/test_visual_diagnosis_state.py
```

Stop when:

- the checkout is not the intended worktree;
- the baseline differs without an approved rebaseline;
- unrelated dirty work overlaps proposed files;
- owner approval for the exact synthetic-offline A0-A2 scope is absent;
- the detached receipt is absent or any hash differs;
- the target worktree contains untracked/local `.env`, credential, token,
  provider-payload, customer-data, or active cloud-configuration material;
- denied egress or the pre-import isolation check cannot be established.

The external advisory receipt must conform to
`visual_diagnosis_review_receipt/v1`: UTC issue time, baseline, exact eight-path
digest/byte-count tuples, exact changed-path allowlist, scope, review round,
per-role reviewer task reference and decision, unresolved findings, overall
decision, debate result, canonical receipt digest, and
`grants_implementation_authority: false`. Verify its provenance in the trusted
session before any branch command; do not store the receipt or source-gap audit
inside the implementation worktree under A0-A2 authority. Compute the receipt
digest over UTF-8 canonical JSON with sorted keys, compact separators, closed
fields, and `receipt_digest` omitted; encode SHA-256 as lowercase hexadecimal.

### Task A1: Define provider-neutral contracts

Write failing unit tests first for the neutral structural slice only:

- `VisualTriageCase` with orthogonal state vector and aggregate revision;
- immutable `VisualTriageEvent` envelope and payload-safe receipt;
- fixed ordinary and hard-coded terminal/control receipt lanes;
- neutral `PendingCustomerAction` for question, rating plate, or recapture;
- `MediaAsset` roles and validation-state codes without storage behavior;
- typed, bounded call facts with provenance and opaque transcript reference;
- opaque analysis-artifact and candidate-set references with closed readiness
  codes, but no evidence, identity, or hypothesis content;
- policy/model/configuration version and synthetic structural receipt-reference
  primitives limited to event ID, semantic-envelope fingerprint, closed status,
  and resulting revision; external receipt slots and payloads remain absent;
- strict internal, customer-safe structural, contractor-safe structural, and
  allowlisted audit projections.

A1 does not implement reviewed safety dispositions or copy, question selection,
delivery eligibility, customer diagnosis content, or exact-parts compatibility.
Those semantics remain owned by A3-A7. A neutral action/receipt type is lifecycle
plumbing and does not grant permission to produce it.

Required invariants:

- schemas are closed recursively (`extra=forbid` or equivalent), with bounded
  strings, lists, maps, closed enums, and safe validation errors;
- no raw transcript, media, public token, address, phone, provider secret,
  provider payload, provider ID/URL, model/serial, diagnosis, answer, price,
  repair instruction, or exact part appears in audit/log/exception/`repr`/`str`;
- customer-safe structural output rejects exact serial/part, price, repair
  instructions, arbitrary copy, raw evidence, hidden scores, and unapproved
  delivery fields;
- A1 does not define `DiagnosticHypothesis`; the later reviewed hypothesis
  contract may never carry customer-delivery eligibility;
- exact part fields are absent from A1 rather than merely provenance-checked;
- the ordinary receipt lane is capped at 64; the separate six-receipt control
  lane admits only the exact closed event kinds and source-state sequences in
  the design, never a caller-selected lane;
- neither lane evicts; identical replay and rejection consume no capacity;
- unknown/ambiguous fields remain explicit;
- typed call facts contain bounded normalized values, opaque source references,
  and provenance, never raw transcript text or live-controller imports;
- provider-specific and live-application types do not leak into contracts;
- every projection gives deletion precedence, exposes no next action during
  deletion, separates historical replay revision from current aggregate
  revision, and never implies an in-flight delivery completed.

Expected files:

- `app/services/visual_diagnosis_contracts.py`
- `tests/unit/test_visual_diagnosis_contracts.py`

### Task A2: Implement the pure state machine

Test first:

- every row in the design's minimum transition matrix;
- orthogonal state changes that may coexist without overwriting each other;
- activation only after explicit consent grant, and cancellation on consent
  decline or later withdrawal;
- invalid regression, prerequisite skipping, or out-of-order events;
- aggregate schema and case/contractor binding validation occurs before event-ID
  lookup and reveals no stored-decision information on binding failure;
- same-event/same-semantic-envelope-fingerprint replay returns a no-op with
  historical decision/original revision and current aggregate revision/projection
  as distinct fields;
- mutate each fingerprint field independently; same event ID with any semantic
  mismatch is a conflict;
- stale or future aggregate revision is rejected without mutation;
- wrong case/contractor structural binding is rejected without claiming that
  this performs authentication or authorization;
- duplicate upload finalization, analysis completion, contractor-packet
  readiness/delivery start/delivery receipts, delivery-policy receipt, customer
  delivery start, customer-delivery receipt, and case closure;
- one unresolved customer action maximum;
- at most two committed diagnostic-question issuances;
- at most one rating-plate issuance and one targeted-recapture issuance, tracked
  separately;
- transport retry/replay does not consume another customer-effort unit;
- rejected initial symptom video can issue exactly one targeted recapture;
- successful plate and recapture flows bind request/action/asset/role/validation,
  resolve the media action as `fulfilled`, then permit reanalysis;
- media action stays unresolved through upload/quarantine/validation;
  `analysis_started` rejects while any customer action is unresolved;
- `answered`/`not_sure` reject for media actions, while
  `fulfilled`/`unavailable` reject for diagnostic questions;
- the 64-receipt ordinary lane admits identical replay at capacity, rejects a
  new ordinary event with a stable capacity code, never evicts, and cannot be
  selected by caller data;
- the separate six-receipt control lane remains available after ordinary-lane
  saturation for zero or one terminal event, deletion request, deletion retries 1-3,
  and deletion verification; invalid sequence, fourth retry, or overflow rejects;
- at most three retry attempts per named stage, with a lower later-policy limit
  permitted but no higher runtime override; these are three retries after the
  initial failed attempt;
- request ID, kind, status, response-code, and locale mismatch rejection;
- `not_sure`, `declined`, and `cannot_safely_complete` resolution;
- consent decline/withdrawal and case cancellation;
- retriable and terminal analysis failure, including bounded stage-specific
  retry from only the retriable state;
- exact source-state preconditions for consent request/grant/decline/withdrawal,
  explicit cancellation, expiry, quiescent closure, and deletion;
- expiry, deletion-pending retry events 1-3, and verified-deleted precedence;
- every new non-deletion event and late completion rejects while deletion is
  pending; after verified deletion only identical terminal-receipt replay works;
- historical replay after cancellation/deletion cannot recreate an action or
  appear as current permission;
- failure and stage-specific bounded retry;
- pending-action invalidation on a terminal transition;
- tenant/case identity and injected time/ID immutability;
- structural policy/deletion receipts remain explicitly
  `synthetic_offline_only` and nonauthorizing.

The pure state machine accepts typed events and returns a decision. It performs
no Firestore, storage, provider, SMS, filesystem, environment, process, dynamic
import, credential, network, or clock I/O. Inject time and IDs. It validates only
structural transition admissibility; it does not authenticate actors, establish
tenant authority, verify external deletion, or decide diagnosis, safety,
customer-delivery, question-selection, or parts eligibility.

Canonical event behavior:

| Input condition | Result |
| --- | --- |
| New event, exact expected revision, valid prerequisite | Apply once and increment revision |
| Schema or aggregate binding invalid | Reject before ledger lookup; reveal no receipt/decision existence |
| Same event ID/fingerprint | Return replay flag, historical decision/original revision, and distinct current revision/projection without mutation or permission |
| Same event ID, any semantic-envelope mismatch | Reject conflict |
| Stale/future revision or wrong order | Reject without mutation |
| Wrong case/contractor structural binding | Reject without mutation; no auth claim |
| Transport retry of committed action issuance | No new action and no counter increment |
| Ordinary lane full | Reject new ordinary event; leave reserved control lane reachable |
| New non-deletion event while `deletion_pending` | Reject without mutation |
| Event after `verified_deleted` | Reject unless identical terminal deletion-receipt replay |
| Deletion receipt in A2 | Accept only as synthetic nonauthorizing structure |

Expected files:

- `app/services/visual_diagnosis_state.py`
- `tests/unit/test_visual_diagnosis_state.py`

### A0-A2 checkpoint before Task A3

This is a separate stop gate. The broader Phase A exit below is not usable here
because A3-A8 files do not yet exist.

Allowed changed paths are exactly:

- the four controlling visual-diagnosis documents;
- `app/services/visual_diagnosis_contracts.py`;
- `app/services/visual_diagnosis_state.py`;
- `tests/unit/test_visual_diagnosis_contracts.py`;
- `tests/unit/test_visual_diagnosis_state.py`.

No dependency, lock, configuration, route, API, webhook, live service, database,
SMS, iOS, workflow, gate, deployment, or environment file may change.

The test harness may perform read-only filesystem, Git-subprocess, environment-
name, and import-metadata inspection solely to establish this gate. The two
candidate modules remain zero-I/O. Neither test module may import a candidate at
module scope or pytest collection time. Each test module must contain and run
its own inline pre-import guard before a controlled candidate import; test order
or a guard in the other module is not evidence.

The exact allowed direct import roots for candidate code are
`__future__`, `collections`, `dataclasses`, `datetime`, `enum`, `hashlib`,
`json`, `re`, `typing`, `uuid`, and `pydantic`; the state module may additionally
import exactly `app.services.visual_diagnosis_contracts`. No other local or
third-party dependency is allowed. The dependency evidence must record Python
3.12, installed Pydantic version `2.12.5`, and the baseline `pyproject.toml`
SHA-256 `9fea68c27dbe4e24cd31fb6c6af4d77a8caa3796a2298d3a52cce2d40fd1764a`.
The reviewed pre-existing offline executables are
`/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin/python` (immediate symlink
target `/opt/homebrew/opt/python@3.12/bin/python3.12`; canonical `realpath`
`/opt/homebrew/Cellar/python@3.12/3.12.13/Frameworks/Python.framework/Versions/3.12/bin/python3.12`;
Python `3.12.13`; canonical-realpath executable SHA-256
`261a3951c895427210dfb7780693600b820f70841c078ab2554ee6fbeba7f376`) and
`/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin/ruff` (Ruff `0.15.20`;
SHA-256 `1edd2e6e57286bdddedb1fb55493a91dc17f42838f3d6be488ded7cfe2a4f3a1`).
Record invoked path, immediate link target, canonical `realpath`, version, and
digest immediately before use; compare the canonical `realpath` and its bytes to
the reviewed values, and stop if any differs or is absent. Do not fetch, install,
or repair a runtime under this authority.

Before importing either candidate module, parse candidate and transitive local
imports and fail closed on:

- imports outside the exact standard-library/neutral-schema allowlist above;
- `app.config`, `app.main`, APIs, webhooks, post-call, SMS, database, provider,
  storage, cloud, telephony, or live-controller imports;
- filesystem-write, environment/config read, socket/network, cloud SDK,
  subprocess/process, dynamic import, `eval`, or `exec` capability;
- reverse reachability from `app.main` or any deployed entrypoint to either new
  module;
- any baseline live-entrypoint hash change.

The deployed-entrypoint root manifest at the bound baseline is exact:

| Path | SHA-256 | Role |
| --- | --- | --- |
| `Dockerfile` | `a8b96ae525dcd94a3e839a1980b14710c8e98663d42b812f4a1754878ddc4a2b` | Cloud Run image and `app.main:app` command |
| `app/main.py` | `057a810dd5eb2e08651fd965f9264c48681e5bb17ce87bad4fafb4260c6e0334` | Deployed FastAPI root |
| `.github/workflows/deploy.yml` | `672555b73c92478a3d92bdabd7233b964fa1bdcc52ebf1a7cb2e9d17cc37ede7` | Staging/production source deploy root |
| `.github/workflows/rollback.yml` | `3be7f5a8863f623a280abab7dd7b350ae270eb2ae4178f419e50a0d2c74dfae0` | Rollback source deploy root |

The guard must hash both candidate files and every local file in their import
closure; scan all tracked `app/**/*.py` plus the four roots above for static
imports, dynamic-import constructs, and candidate module/path strings; and prove
no forward candidate dependency escapes the allowlist and no reverse path or
string reference reaches a deployed root.

Changed-path evidence must parse NUL-delimited Git status and union tracked,
staged, unstaged, and untracked paths. It must equal—not merely be a subset of—
the exact milestone set: the four documents after A0 transfer; those four plus
the contracts module/test (six paths) before A1 controlled import; and all eight
allowlisted paths before A2 controlled import and final exit. An empty candidate
module may establish the TDD path before RED behavior is implemented. For every
untracked text file, run the equivalent of `git diff --no-index --check
/dev/null <path>` or a byte-equivalent whitespace check because ordinary
`git diff --check` omits untracked files.

The contracts test guard accepts exactly the six-path A1 set, or the eight-path
A2 set after both state paths exist; it rejects every other subset/superset. The
state test guard accepts only the eight-path A2 set.

Before the focused pytest process is collected, detached session evidence must
require absence by name, without printing values, for these credential/provider/
cloud variables: `ANTHROPIC_API_KEY`,
`ADMIN_API_TOKEN`, `APNS_KEY_CONTENT`, `APNS_KEY_ID`, `APNS_TEAM_ID`,
`API_BEARER_TOKEN`,
`APPSTORE_ISSUER_ID`, `APPSTORE_KEY_ID`, `APPSTORE_PRIVATE_KEY`,
`DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `FISH_AUDIO_API_KEY`,
`GEMINI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`,
`GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`,
`JOBBER_CLIENT_ID`, `JOBBER_CLIENT_SECRET`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`,
`RECEPTIONIST_OBSERVATION_SHADOW_CALLER_HMAC_KEY`,
`TRANSCRIPT_ENCRYPTION_KEY`, `TWILIO_ACCOUNT_SID`,
`PRODUCTION_TWILIO_ACCOUNT_SID`, `TWILIO_API_KEY_SECRET`,
`TWILIO_API_KEY_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_TWIML_APP_SID`,
`VAPI_API_KEY`, `VAPI_PUBLIC_KEY`, `VAPI_WEBHOOK_SECRET`, and
`VCARD_HMAC_SECRET`. Also require these active-resource or customer/owner
identifier variables to be absent: `APNS_BUNDLE_ID`, `APPSTORE_BUNDLE_ID`,
`CLOUD_RUN_URL`, `DIAL_IN_NUMBER`, `DIAL_IN_NUMBERS`,
`FIREBASE_DATABASE_URL`, `FIRESTORE_PROJECT_ID`, `GCLOUD_PROJECT`,
`GCP_PROJECT`, `GOOGLE_CLOUD_PROJECT`, `TELEGRAM_CHAT_ID`,
`TWILIO_PHONE_NUMBER`, `USER_NAME`, `USER_PHONE`, and
`VAPI_PHONE_NUMBER_ID`, using case-insensitive name comparison.
Also reject and strip every case-insensitive environment-variable name beginning
`BAKEOFF_NONPROD_CREDENTIAL__` or `BAKEOFF_NONPROD_ACCOUNT_REGION__`; suffixes
are dynamic and must not be enumerated from values.
Inventory only ignored and untracked paths inside the worktree using NUL-safe
parsing and fail on `.env`/`.env.*`, PEM/P8/key files, credential/secret/token-
named files, or provider/customer payload artifacts. Do not inspect user
credential stores or print a matched filename. The inline guards recheck the
path inventory, and every controlled import strips the complete variable list
from its temporary environment even when the later full suite has created
synthetic placeholder variables for unrelated tests.

Do not probe ambient user credential/configuration stores. For the controlled
process, bind `CLOUDSDK_CONFIG` and `XDG_CONFIG_HOME` to newly created empty
temporary directories outside the worktree without changing `HOME`, keep
`GOOGLE_APPLICATION_CREDENTIALS` absent, and let the external egress denial
block metadata-service discovery. Record only the temporary isolation mechanism,
never private paths or contents.

An execution-environment mechanism, outside pytest, must enforce denied egress
before collection and be named in detached session evidence. Socket mocking is
defense in depth only. The controlled import runs with the credential variables
stripped and candidate filesystem/subprocess/socket/environment/config access
denied and monitored. Set `PYTHONDONTWRITEBYTECODE=1` and
`sys.dont_write_bytecode = True` before controlled import so interpreter cache
writes cannot create a false zero-I/O claim. Stop before import if any
prerequisite cannot be proven.

The exact-diff tests must also provide deterministic state traces for:

- zero-question completion;
- rating-plate refusal;
- successful rating-plate upload, validation, fulfillment, restart, and replay;
- rejected initial video, one successful recapture fulfillment, reanalysis,
  restart/replay, and rejection of a second recapture;
- recapture rejection/unavailability and budget-exhausted abstention;
- mismatched media request/action/asset/role/validation rejection;
- `not_sure` answer followed by restart/replay without repetition;
- `cannot_safely_complete` with no repeat prompt;
- interrupted upload retry without budget consumption;
- stale and conflicting answer rejection;
- consent decline/cancel;
- expiry and recoverable/terminal failure;
- ordinary-ledger saturation followed by reachable terminal/control transition,
  deletion request, retries 1-3, and verification; fourth retry/overflow rejects;
- every new non-deletion event and every late completion rejected while deletion
  is pending, with identical historical replay remaining a nonmutating exception;
- deletion precedence while packet/customer delivery is pending, without a false
  delivery receipt or next action;
- terminal replay returns historical and current revisions separately and never
  recreates a cleared action;
- each role-safe status/reason/next-action projection.

Required verification, run in the isolated worktree with no `.env`, no provider
or cloud credentials, denied egress, and no network installation:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  '/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin/python' -m pytest \
  tests/unit/test_visual_diagnosis_contracts.py \
  tests/unit/test_visual_diagnosis_state.py \
  -q
'/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin/ruff' check \
  app/services/visual_diagnosis_contracts.py \
  app/services/visual_diagnosis_state.py \
  tests/unit/test_visual_diagnosis_contracts.py \
  tests/unit/test_visual_diagnosis_state.py
PYTHONDONTWRITEBYTECODE=1 \
  '/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin/python' -m pytest tests/unit -q
git diff --check
git status --short --branch
```

The repository-local pre-import isolation checks and recursive canary tests are
the required A0-A2 security scan. Bandit is not pinned in the development
toolchain and must not be installed over the network under this authority. If a
separately approved, pinned offline Bandit runtime is already available, record
its version and result as supplemental evidence only.

All A0-A2 guards and fixtures must live in the two allowlisted test modules. Do
not create a helper script, `conftest.py`, tool configuration, or dependency file
under this authority. Use the active execution environment's enforceable
network-denial mechanism and record that mechanism; a test-only socket mock does
not by itself establish denied egress. If enforceable denial is unavailable,
stop without importing candidate modules.

Exit requires:

- RED evidence for the intended contract/state behavior before implementation;
- focused and full unit suites passing;
- Ruff, isolation, projection/canary, path, live-hash, and diff checks passing;
- no credential file, provider payload, network call, live import, route, gate,
  deployment, or external-state change;
- exact changed-document and changed-source hashes recorded;
- independent staff, security/privacy, and Product/UX approval of the exact diff.

Stop before A3 even if every check passes. Passing checks do not authorize A3,
provider use, real data, customer contact, staging, production, or release.

Tasks A3-A8 below document later contracts and policies only. They are outside
the proposed `synthetic_offline_A0_A2` branch scope and require a new exact plan,
changed-path allowlist, review, and owner authorization before any file is added.

### Task A3: Define the safety and claims contract

Before implementing policy behavior, add a versioned draft artifact for licensed
HVAC review containing:

- supported equipment and scenario boundaries;
- excluded and confusable conditions;
- red-flag taxonomy;
- minimum evidence per hypothesis;
- conflicting-evidence rules;
- abstention rules;
- customer-safe action copy;
- prohibited questions and instructions;
- permitted claim templates;
- draft fixtures and adjudication instructions.

Suggested file:

- `docs/safety/visual-diagnosis-hvac-not-cooling-outdoor-noise-v1.md`

Do not label the contract domain-approved until named licensed reviewers approve
an exact revision.

### Task A4: Implement deterministic safety policy

Write tests proving:

- every known red flag produces `stop_and_escalate` or another approved strict
  disposition;
- absent evidence never means absence of danger;
- a model hypothesis cannot clear or downgrade a red flag;
- unsupported scenario/trade/equipment abstains;
- unapproved customer copy cannot be emitted;
- contradictory or insufficient evidence cannot auto-deliver;
- output contains a copy code, not model-authored safety prose.

Expected files:

- `app/services/visual_diagnosis_safety.py`
- `tests/unit/test_visual_diagnosis_safety.py`

### Task A5: Implement default-hold Customer Delivery Policy

Test the complete conjunction in the design. The policy must return typed states:

- `deliver_preliminary_diagnosis`;
- `deliver_abstention`;
- `contractor_only_review`;
- `safety_stop`;
- `hold_delivery_failure`.

Negative tests must dominate:

- model self-confidence never changes delivery eligibility;
- schema-valid but semantically invalid output holds;
- unresolved safety holds/stops;
- missing consent holds;
- missing contact/compliance gate holds;
- unsupported scope holds;
- exact part, repair instruction, or price in customer fields is rejected;
- duplicate delivery receipt prevents another send;
- provider timeout or partial result cannot deliver.

Expected files:

- `app/services/visual_diagnosis_delivery.py`
- `tests/unit/test_visual_diagnosis_delivery.py`

### Task A6: Implement the adaptive question planner

Create a versioned, closed question library. The planner may select only library
items and never generate a new customer question.

Tests must prove:

- zero questions when existing evidence is sufficient;
- no question repeats a call fact, media observation, earlier question, or known
  account fact;
- each selected question names the decision it can change;
- only one question is emitted at a time;
- no more than two questions across retries, channels, or restarts;
- only bounded one-tap answers are accepted;
- no question asks the customer to approach danger, restart equipment, open a
  panel, touch a component, push a fan, measure electricity, or inspect
  refrigerant;
- a label recapture is not counted as a diagnostic question but is limited to
  one and only when equipment resolution is decision-changing;
- budget exhaustion leads to deliver-or-abstain, never question three.

Expected files:

- `app/services/visual_diagnosis_questions.py`
- `tests/unit/test_visual_diagnosis_questions.py`
- `tests/fixtures/visual_diagnosis/question_cases.json`

### Task A7: Define equipment identity and parts contracts

Tests must preserve:

- raw OCR candidates and character alternatives;
- manufacturer/model/serial separation;
- suffix and punctuation significance;
- explicit states `unreadable`, `ocr_candidate`, `customer_confirmed`, and
  `catalog_verified`;
- model/serial validation without silent correction;
- authoritative catalog adapter returns retrieved facts only;
- exact parts require source, retrieval time, equipment match, serial/config
  rule, region where relevant, and supersession chain;
- `candidate_verify` and `no_reliable_match` remain normal outcomes;
- model text can never populate an exact part number field;
- parts stay contractor-only and never authorize purchase/install.

Expected files:

- `app/services/equipment_identity.py`
- `app/services/parts_compatibility.py`
- `tests/unit/test_equipment_identity.py`
- `tests/unit/test_parts_compatibility.py`

### Task A8: Build the synthetic evaluation harness

The harness consumes pre-authored typed fixtures. It does not call a provider.

Output must contain:

- corpus and source digests;
- schema/policy/model placeholders;
- counts by class;
- identity, top-one/top-two, safety, delivery, abstention, question, parts,
  latency, and deletion metric slots;
- explicit evidence scope `synthetic_offline_only`;
- explicit false authorization fields for provider, real data, staging,
  customer delivery, production, and release;
- no fixture text or PII in reports.

Expected files:

- `scripts/evaluate_visual_diagnosis.py`
- `tests/unit/test_visual_diagnosis_evaluation.py`
- `tests/fixtures/visual_diagnosis/manifest.json`
- synthetic fixtures without real people, addresses, phones, serials, or media.

Phase A exit:

- focused tests pass;
- full unit suite passes;
- Ruff, repository-local isolation guards, and an approved pinned offline security
  scanner pass on touched Python;
- PII/secret/log canary scan passes;
- static check proves new modules are not imported by live pipelines, `main.py`,
  post-call, estimate, MMS, SMS, iOS, or deployed entrypoints;
- independent staff/security/Product-UX review approves the exact diff;
- no external I/O exists.

Suggested verification:

```bash
'/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin/python' -m pytest \
  tests/unit/test_visual_diagnosis_contracts.py \
  tests/unit/test_visual_diagnosis_state.py \
  tests/unit/test_visual_diagnosis_safety.py \
  tests/unit/test_visual_diagnosis_delivery.py \
  tests/unit/test_visual_diagnosis_questions.py \
  tests/unit/test_equipment_identity.py \
  tests/unit/test_parts_compatibility.py \
  tests/unit/test_visual_diagnosis_evaluation.py \
  -q
'/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin/ruff' check <touched-python-files>
# Run Bandit only from a separately approved, pinned offline toolchain.
'/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin/python' -m pytest tests/unit -q
```

## 8. Phase B: Hardened Ingestion And Persistence, Synthetic Media Only

Authority required: separate owner approval after Phase A review.
Data allowed: synthetic, rights-cleared generated media only.
Provider inference: prohibited.
Customer contact: prohibited.

### Task B0: Approve the threat model and retention table

Write and review:

- data-flow diagrams for secure web upload and later optional MMS;
- asset inventory: raw, normalized, frames, audio, label crop, OCR, prompt,
  response, diagnosis, packet, feedback, audit, and provider file;
- controller/processor/source/region table;
- exact TTL and deletion owner for every asset;
- link-forwarding and bearer-token threats;
- cross-tenant, replay, cost amplification, malware, polyglot, decompression-bomb,
  prompt-injection, prohibited-content, and insider threats;
- export, correction, legal hold, account deletion, and partial-deletion failure;
- incident and kill-switch runbook.

Suggested file:

- `docs/security/visual-diagnosis-threat-model.md`

Do not start storage/provider work with `TBD` retention.

### Task B1: Add separate default-off gate keys

Proposed keys:

- `visual_diagnosis_case_create`
- `visual_diagnosis_media_process`
- `visual_diagnosis_provider_analyze`
- `visual_diagnosis_parts_lookup`
- `visual_diagnosis_contractor_packet`
- `visual_diagnosis_customer_delivery`
- `visual_diagnosis_feedback_capture`

Test default-off behavior, contractor/environment scope, audit metadata,
idempotency, and the inability of one permission to imply another.

Do not enable or backfill any account.

### Task B2: Implement scoped bootstrap exchange and sessions

Tests first:

- high-entropy token stored only as a hash;
- one-time, purpose/case/contractor/call/caller binding;
- short TTL and revocation;
- exchange produces secure session/capability;
- raw token disappears from follow-up URLs and is not cached, referred, or
  logged;
- upload, customer status, contractor read, and delete are separate;
- typed 404/410 behavior does not leak case existence;
- replay, expiry, forwarding, wrong purpose, cross-tenant, and concurrent
  exchange fail safely.

### Task B3: Add private upload reservation/finalization

Do not stream accepted videos through the FastAPI worker or store them in public
paths. Use private object storage and server-mediated upload reservation.

Tests:

- atomic one successful upload per required role;
- retry before successful finalization;
- total case byte, request, concurrency, IP/session, and tenant cost limits;
- generated object names;
- checksum and finalization binding;
- content-length mismatch;
- abandoned reservation cleanup;
- no raw media access before quarantine passes.

No GCS or IAM mutation may occur without a separately reviewed exact plan and
rollback. A fake object-store adapter is sufficient for initial implementation.

### Task B4: Add isolated media validation and normalization

Validate the bytes, not headers or extensions:

- magic bytes and container;
- safe decoder success;
- codec allowlist;
- image dimensions and decoded-pixel limit;
- video duration, dimensions, frame rate, bitrate, audio track, and bounded
  decode resource use;
- malformed/polyglot/truncated/bomb cases;
- metadata stripping;
- canonical image/video output;
- content/moderation disposition;
- prompt-injection content remains inert evidence, never instructions.

Run decoding in an isolated bounded worker. Do not serve uploaded content with
active content types.

### Task B5: Implement persistence, deletion, and export

Tests:

- contractor ownership on every read/write;
- customer session limited to one case;
- state CAS/idempotency under concurrency;
- encrypted sensitive records and private objects;
- TTL and scheduled cleanup;
- explicit case/customer/account deletion cascade;
- derived assets and provider placeholders deleted;
- partial deletion remains `deletion_pending` and retries;
- export is tenant-scoped and does not expose internal prompts/secrets;
- log/audit canaries do not leak payloads;
- account deactivation cannot orphan cases.

Phase B exit:

- threat model and retention approved;
- every ingest abuse, tenant, deletion, export, and failure-injection test passes;
- synthetic objects leave zero unintended residue;
- new gates remain false;
- provider, SMS, post-call, MMS, and customer delivery remain unwired;
- security review approves the exact implementation.

## 9. Phase C: Provider And Catalog Qualification

Authority required: separate provider-connected approval.
Data allowed: rights-cleared, expert-labeled qualification fixtures only.
Real customers: prohibited.
Customer contact: prohibited.

### Task C0: Seal provider and data configuration

Before a call:

- approve Gemini Developer API versus Vertex AI and exact region/project;
- use a billed/approved project and verify model-training/data-sharing posture;
- record logging and provider retention settings;
- confirm DPA/subprocessors and incident terms;
- use managed server-side credentials in headers, not query strings;
- pin model, SDK/API, media settings, frame sampling, audio treatment, prompt,
  schema, safety, and delivery-policy digests;
- define provider-file deletion and verify it;
- establish cost and quota ceilings;
- prepare a no-production-data credential and isolated storage.

### Task C1: Create the expert-labeled corpus

Licensed HVAC reviewers must define and adjudicate:

- actual equipment identity;
- raw label transcription;
- actual onsite finding;
- evidence visible/audible before service;
- safety state;
- plausible top-two diagnosis set;
- actual installed/replaced part and uncertainty;
- useful onsite tests and preparation;
- cases that must abstain.

Separate development and sealed qualification sets. Preserve rights/consent
receipts. Do not put real customer PII in source control.

### Task C2: Implement the provider observation adapter

The provider returns observation candidates, not customer copy or final delivery.

Tests and qualification cover:

- strict schema and semantic validation;
- bounded strings/enums/counts;
- timestamps/provenance;
- adversarial narration, OCR, QR, label, and background text;
- partial, malformed, refused, timed-out, and duplicate responses;
- frame-sampling limitations for fan motion;
- separate audio and high-resolution label paths;
- deletion of provider files;
- no response body or diagnosis in logs.

### Task C3: Implement approved catalog adapters

Start with fake/static adapters until a lawful authoritative data source is
selected. Do not scrape or infer production part compatibility from arbitrary
web search.

Qualification must prove:

- exact retrieval provenance;
- manufacturer/model/serial/configuration validation;
- region/revision/voltage/capacity and supersession handling;
- no part emitted on ambiguous identity;
- no model-generated exact part;
- contractor-only visibility;
- prior-field-replacement limitation is visible.

### Task C4: Run sealed qualification

Report all metrics defined in the design, including denominators and confidence
intervals. Do not tune on the sealed set. Failed cases remain visible.

Phase C exit:

- zero safety/delivery/parts hard-gate violations;
- numeric identity and top-two thresholds are ratified by domain/statistical
  reviewers and met on the sealed set;
- provider residue deletion verified;
- latency/cost fit the bounded envelope;
- model/configuration manifest frozen;
- exact-diff staff, HVAC, and security reviews approve a contractor-only shadow
  plan;
- new provider/parts gates remain false outside the qualification account.

Provider qualification proves only the frozen fixture envelope. It does not
prove customer release.

## 10. Phase D: Contractor-Only Shadow Pilot

Authority required: separate real-customer-data and staging/pilot approval.
Accounts: small explicit allowlist.
Customer preliminary result: disabled.

### Task D0: Privacy and consent readiness

- approved upload consent and privacy notice;
- contractor agreement and media-handling training;
- Apple privacy declaration/usage descriptions if native capture is involved;
- processor/subprocessor inventory;
- deletion/export support path;
- incident, pause, and customer-request runbook;
- A2P review for invitation SMS, if used.

### Task D1: Orchestrate the workflow

Wire case creation, validated upload, observation extraction, equipment
resolution, safety, hypotheses, questions, parts candidates, and contractor
packet behind separate gates.

Keep customer delivery off. Send only generic notifications linking to
authenticated views; do not place diagnosis, phone, model/serial, or parts in
push/SMS.

### Task D2: Build contractor API and iOS packet

Required contractor actions:

- inspect original media and evidence timestamps;
- see identity and verification state;
- see hypotheses and missing tests;
- see parts candidates/provenance;
- mark useful/not useful;
- record actual finding, evidence, part used, and label certainty;
- correct identity;
- request deletion;
- never order/install from Kevin automatically.

Perform physical iPhone verification for accessibility, poor network,
backgrounding, cancellation, media playback, contractor correction, and
deletion state.

### Task D3: Monitor shadow outcomes

Review every pilot case. Measure safety false negatives, delivery eligibility
simulation, identity, top-two, abstention, question budget, parts compatibility,
contractor edits, latency, cost, deletion, and operational failures.

Phase D exit:

- no unresolved P0/P1 issue;
- no dangerous under-triage in the reviewed pilot set;
- contractor/technician outcomes adjudicated rather than blindly accepted;
- approved numeric thresholds met;
- deletion and incident handling meet SLA;
- automatic customer-result proposal receives new owner, HVAC, privacy,
  security, and staff approval.

## 11. Phase E: Bounded Automatic Customer Pilot

Authority required: explicit owner authorization after Phase D.
Accounts/scenario: allowlisted and unchanged.
Price/exact parts/DIY: prohibited.

Requirements:

- deterministic delivery policy default-hold;
- only supported low-risk envelope;
- safe copy library and reviewed translations;
- channel opt-out/compliance and delivery receipts;
- separate provider and customer-delivery kill switches;
- no claim that contractor received media until receipt exists;
- daily case review;
- rollback disables customer delivery before diagnosis processing/deletion;
- customer correction and deletion path;
- no App Store or broad marketing claims.

Evaluate customer comprehension, trust, usefulness, inappropriate reassurance,
booking conversion, complaints, safety, contractor corrections, and end-to-end
economics.

Stop immediately on:

- unsafe advice or dangerous under-triage;
- unsupported automatic diagnosis;
- exact-part/price/repair leakage;
- cross-tenant exposure;
- raw payload logging;
- deletion SLA breach;
- gate bypass or duplicate customer delivery;
- material model/configuration drift without requalification.

## 12. Phase F: General Availability Decision

General availability requires a new release plan. It is not part of this plan.

At minimum:

- representative safety and outcome evidence;
- stable provider/catalog agreements;
- versioned model requalification process;
- demonstrated data lifecycle and incident response;
- contractor support and customer appeal/correction paths;
- verified contribution margin and capacity;
- claims review limited to demonstrated scope;
- staged rollout, rollback, monitoring, and physical-device evidence.

## 13. Test Matrix

### 13.1 Domain and policy

- schema bounds and semantic validation;
- unsupported enum/version rejection;
- strict unknown-field rejection and safe validation/`repr`/exception behavior;
- internal/customer/contractor/audit projection separation;
- at most two hypotheses/questions;
- question deduplication and decision-changing rationale;
- customer field exclusion;
- safety disposition precedence;
- delivery default-hold;
- model confidence ignored by policy;
- exact-part provenance and customer exclusion.

### 13.2 Authorization and tenancy

- forged, expired, used, revoked, leaked, forwarded, and wrong-purpose token;
- concurrent one-time exchange;
- cross-contractor/case/caller access;
- contractor list/detail/export/delete ownership;
- customer session isolation;
- raw token absent from logs/referrers/follow-up URLs;
- gate independence and environment default-off.

### 13.3 Upload and abuse

- direct-upload bypass;
- MIME spoof, extension spoof, polyglot, malformed container;
- decompression/pixel bomb;
- oversized bytes/duration/dimensions/bitrate/frame rate;
- unsupported/multiple audio tracks;
- duplicate/concurrent/replayed upload;
- abandoned reservation;
- malware/prohibited/unexpected-person media;
- SSRF and redirect constraints for later MMS retrieval;
- cost/rate/concurrency exhaustion.

### 13.4 Model and adversarial

- prompt injection in narration, labels, QR, error display, and background text;
- fabricated evidence/identity/part;
- malformed/partial/duplicate/refused/timed-out response;
- conflicting call versus media evidence;
- low-light/occluded/corroded/ambiguous label;
- fast fan motion missed by default sampling;
- noisy audio and confusable sound;
- unsupported trade, equipment, and symptom;
- model/configuration drift.

### 13.5 Safety and claims

- every red flag;
- no-danger evidence absent versus observed safe state;
- no novel instructions;
- no panel, wiring, fan, refrigerant, gas, restart, or repair guidance;
- no definitive diagnosis, guaranteed outcome, price, or exact customer part;
- safe abstention copy;
- translation equivalence for approved languages.

### 13.6 Parts

- raw OCR preserved;
- ambiguous character/suffix handling;
- wrong model/serial/config/voltage/region;
- superseded part chain;
- prior field replacement warning;
- catalog unavailable/stale/conflicting;
- generated exact part rejected;
- contractor confirmation required.

### 13.7 Lifecycle and operations

- orthogonal case/consent/media/analysis/packet/delivery/deletion state;
- event ID, digest, revision, replay, collision, stale, and ordering rules;
- one outstanding action and separate question/plate/recapture budgets;
- consent refusal, cancellation, expiry, and terminal precedence;
- provider/object/Firestore/Twilio/cache/derived deletion;
- partial deletion retry and residue report;
- export/correction;
- account deletion cascade;
- idempotent retries without duplicate analysis or SMS;
- kill-switch independence;
- rollback preserving deletion/audit jobs;
- log and telemetry canaries;
- physical iPhone/web capture and poor-network flows.

## 14. Required Review Matrix

| Gate | Owner | Staff | Security/privacy | Licensed HVAC | Product/UX | Statistical/eval |
| --- | --- | --- | --- | --- | --- | --- |
| Four-document amendment | documentation-only approval recorded | exact-hash receipt required | exact-hash isolation/nonclaim review required | deferred to A3 | exact-hash structural UX review required | deferred |
| Tasks A0-A2 | separately authorize exact hashes | exact diff | exact diff | deferred to A3 | structural trace/projection exact diff | deferred |
| Tasks A3-A8 | separately authorize revised plan | exact diff | policy review | contract draft/review | question/result UX | metric plan |
| Phase B | authorize | architecture | approve threat/data lifecycle | safety boundary | consent/upload UX | abuse coverage |
| Phase C | authorize provider | qualification | provider/data terms | label/adjudicate | no customer UI | approve corpus/thresholds |
| Phase D | authorize real data/pilot | pilot plan | approve consent/ops | review cases | contractor UX | shadow analysis |
| Phase E | authorize customer result | release review | release review | customer copy/envelope | customer research | approve gates |
| Phase F | authorize GA | release readiness | release readiness | claims/scope | launch UX | representative evidence |

## 15. Documentation Deliverables

This initial package contains:

- `docs/superpowers/specs/2026-08-11-visual-diagnosis-design.md`
- `docs/superpowers/plans/2026-08-11-visual-diagnosis.md`
- `docs/handoffs/2026-08-11-visual-diagnosis-handoff.md`
- `docs/handoffs/2026-08-11-visual-diagnosis-new-session-prompt.md`

Later required documents:

- licensed-HVAC safety/claims contract;
- visual-diagnosis threat model and data-retention table;
- provider/data configuration manifest;
- corpus manifest and adjudication guide;
- parts-source approval and compatibility contract;
- shadow-pilot protocol;
- customer-pilot release and rollback plan;
- incident, deletion, and model-drift runbooks.

## 16. Immediate Next Action

The documentation-only amendment authorized on 2026-08-12 received an
exact-diff staff, security/privacy, and Product/UX review. The detached review
receipt must bind the baseline plus full SHA-256 hashes and byte counts for all
eight allowlisted document/source/test paths and the allowed path set.

The owner has approved those exact amended documents for implementation. The
isolated branch/worktree now contains the eight-path A0-A2 implementation and
must receive a fresh exact-diff staff/security/Product-UX review before A3.

No connected provider, media upload, integration, or customer-facing work is an
immediate next action.
