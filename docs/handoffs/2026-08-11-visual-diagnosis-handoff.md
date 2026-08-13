# Hey Kevin Visual Diagnosis Documentation Handoff

Created: 2026-08-11 22:18 EDT
Amended: 2026-08-12 under owner authorization for documentation and synthetic-offline A0-A2 continuation
Prepared by: Codex

## 2026-08-12 Handoff Override

The newest user request is `$handoff`. This override is the current durable
state for the next agent; older historical sections below remain provenance.

- Current isolated worktree: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis`
- Branch: `codex/visual-diagnosis`
- Baseline: `HEAD == origin/main == d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`
- Status before this override: exactly eight allowlisted untracked paths, with no staged or tracked changes; primary checkout untouched.
- Latest reported verification before this override: denied-egress focused suite `60 passed`; Ruff clean; non-diagnostic unit suite `2046 passed, 19 warnings`.
- Latest panel result: staff/code review approved; Product/UX approved; security/privacy rejected the exact exit on evidence-quality P1s, with no P0.
- This override edits an allowlisted handoff document. The prior eight-file SHA receipt is therefore expired; recompute all eight hashes and obtain fresh review before any commit or branch-readiness claim.

### Remaining security P1s

1. Privacy canaries must use realistic digit-only and alphanumeric opaque phone,
   serial, token, provider-reference, and URL values through recursive maps and
   lists, then assert safety across projection, audit, `repr`, `str`, log, and
   exception sinks. Labeled prose canaries are insufficient because validators
   reject the prose before the sinks are exercised.
2. Fingerprint tests must construct internally recomputed same-event-ID
   envelope variants for supported mutable fields and assert
   `event_id_conflict`, unchanged revision, and unchanged projection. Current
   stale-digest mutations prove malformed-envelope rejection, not valid
   semantic conflict. Unsupported schema/evidence-scope handling must be
   documented as a deliberate neutral pre-ledger rejection if it remains so.
3. Keep explicit tests for submitted-media expiry/cancellation semantics and
   duplicate-transition stable decision codes. The runtime guards for packet /
   policy monotonicity, uploading abort restoration, submitted-media binding,
   and expiry chronology are already present.

### Handoff next action

Read the design, plan, and this override; verify status, baseline, and hashes;
then make only the minimum A0-A2 test/contract/documentation changes needed for
the security P1s. Rerun the denied-egress focused suite, the non-diagnostic full
unit suite, Ruff, static/allowlist guards, and two stable eight-file hash checks.
Request fresh staff, security/privacy, and Product/UX exact-diff review. Stop
before staging, committing, pushing, provider access, live data, customer
delivery, or deployment unless the owner explicitly expands authority.

## Objective

Prepare a branch-ready, safety-gated design and implementation package for Hey
Kevin Visual Diagnosis. The proposed feature reuses a contractor call's facts,
accepts one short narrated HVAC video and an optional rating-plate photo, asks
zero questions normally and no more than two bounded adaptive questions, gives
an eligible customer an immediate preliminary diagnosis, and gives the
contractor a richer equipment/parts-preparation packet.

This handoff transfers the exact documentation context and the approved
synthetic-offline A0-A2 implementation state. No provider call, external
mutation, deployment, feature flag, or customer behavior was changed.

On 2026-08-12 the owner first said `proceed` for the four-document amendment,
then explicitly said `approved` for the exact synthetic-offline A0-A2
implementation. The work is isolated in the recorded branch/worktree and
remains outside A3-A8, providers, real data, live delivery, and deployment.

## Current State

### Continuation update (2026-08-12)

The historical documentation-only snapshot below is retained for provenance.
The owner subsequently approved proceeding with the synthetic offline A0-A2
implementation. That work is isolated in
`.worktrees/visual-diagnosis` on `codex/visual-diagnosis`, currently bound to
`d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`. Only the eight allowlisted
documents/source/test paths are untracked there; no provider, live, customer,
deployment, or A3-A8 work has been performed. The current continuation is
subject to the rebaseline source-gap audit and fresh exact-diff review below.

- Repo/workspace: `/Volumes/Extreme Pro/MYPROJECTS/Kevin`
- Repository root resolved by Git: `/Volumes/Extreme Pro/MYPROJECTS/Kevin`
- Branch: `main`
- Upstream: `origin/main`
- Worktree role: primary checkout remains documentation-only; implementation is
  isolated in `.worktrees/visual-diagnosis`
- Local `HEAD` / `main`: `fecec74220b16c561545d175e291be24058f5ad4`
  (`fix: personal-mode Kevin must not invent answers for the owner (#161)`)
- Observed upstream and rebound implementation baseline:
  `origin/main` at `d2a2f003134a66b35cd76cabb8c2aaa43ca184f5` (current
  post-PR #163 baseline)
- Baseline relationship: drafting began with `main == origin/main` at
  `fecec742…`; during exact review `origin/main` advanced by two commits. Local
  `HEAD`/`main` remain intentionally unmoved and behind by two.
- Dirty state before documentation: clean
- Dirty state after documentation: four expected untracked documentation files;
  no application/source/test/config files modified
- Related implementation worktree: `.worktrees/visual-diagnosis` on
  `codex/visual-diagnosis`, bound to `d2a2f003…`
- Deployment/runtime state: not inspected and not authorized
- A0-A2 amendment authority: documentation edits and exact-diff review
- Implementation authority: recorded for synthetic-offline A0-A2 only; no A3-A8,
  provider, live, customer, or deployment authority
- Approval identity: detached receipt required with baseline, all eight full
  SHA-256 path hashes/byte counts, allowed paths, and panel decision

## Newest User Request

The user asked:

> Can you start the documentation needed to develop this feature? I will launch
> a new agent to work on this in a branch.

The newest instruction was `proceed`, in direct response to the explicitly
bounded proposal to amend these four documents and run exact-diff review. It did
not expand authority to branch creation or implementation.

The newest product decisions carried into the documents are:

- photos/video should identify manufacturer, model, and serial where possible;
- the contractor should receive parts-preparation intelligence;
- the experience must not ask a long questionnaire;
- Kevin analyzes call facts and media first;
- zero questions normally, no more than two decision-changing bounded questions;
- the customer should receive a genuinely useful preliminary diagnosis, not
  only a generic referral to a technician.

## Completed Work

- Bound the work to the exact repo, branch, clean state, and SHA.
- Inspected the current estimate, media-analysis, post-call, MMS, gate, security,
  test, iOS, and documentation paths.
- Confirmed source-only that the existing estimate feature is a default-off,
  incomplete prototype and not evidence of released end-to-end capability.
- Researched current primary guidance for Gemini video/file handling, Gemini
  paid-data/logging terms, Twilio media lifecycle, OWASP upload security, and
  manufacturer model/serial parts guidance.
- Obtained an independent staff review. Decision: **approve with conditions**
  for documentation; implementation/customer delivery remains gated.
- Obtained a separate read-only security/privacy/safety review with P0
  requirements for capability scope, upload hardening, tenant isolation,
  consent, provider terms, retention/deletion/export, output policy, and gate
  separation.
- Wrote a detailed design specification.
- Wrote a phased implementation and qualification plan.
- Wrote this handoff and a paste-ready new-session prompt.
- Ran a three-role staff, security/privacy, and Product/UX review plus one debate
  round on 2026-08-12. Consensus: no P0 inside a corrected synthetic-only A0-A2
  slice, but the original lifecycle, serialization, and reproducibility gaps were
  P1 blockers to branch creation and implementation.
- Amended all four documents to add the orthogonal state vector, immutable event
  rules, neutral pending-customer-action envelope, strict projections, detached
  approval receipt, exact transfer procedure, and reproducible A0-A2 stop gate.
- Ran a first exact-hash review of the amendment. All roles found no P0 in the
  strict offline slice but rejected the bytes for residual P1 reachability,
  receipt-capacity, Gate 0, replay, and pre-import-proof defects.
- Ran one cross-role debate round. Staff, security/privacy, and Product/UX
  unanimously supported the consolidated document-only repair now reflected in
  this package.
- The repair review then observed `origin/main` advance to `7e9550b…`. The panel
  found no remaining structural A0-A2 P0/P1 but correctly stopped branch
  readiness on source drift. A documentation-only audit inspected all seven
  changed paths, confirmed the change is a subscription-promotion containment
  fix, rebound the two changed workflow hashes, and left primary `main` unmoved.
  These newly rebound bytes still require their own exact-hash review.
- During implementation, `origin/main` advanced again to `d09c58f…` (PR #163).
  A second source-gap audit found twelve subscription-verification paths only;
  no visual-diagnosis path, Dockerfile, app/main.py, pyproject.toml, or
  deploy/rollback workflow changed. The isolated implementation branch was
  fast-forwarded to `d09c58f…`; primary `main` remains unmoved. This rebaseline
  changes the controlling-document hashes and requires fresh exact-diff review.
- During the fresh review, `origin/main` advanced from `d09c58f…` to
  `d2a2f00…`; the two-path delta only updates iOS project/version metadata. The
  implementation branch was fast-forwarded again, and the current baseline is
  `d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`.

## In Progress

The implementation is in progress only as an uncommitted synthetic-offline
A0-A2 slice in the isolated worktree. The current eight-path bytes require a
fresh exact-diff review receipt before exit. The design explicitly records these
remaining approvals:

- later-phase owner approvals;
- licensed HVAC domain approval;
- security/privacy approval;
- product/UX review;
- statistical/evaluation threshold review;
- later provider, real-data, staging, customer-delivery, and production
  authorizations.

## Important Decisions

### Product contract

- Feature name may be **Kevin Visual Diagnosis**.
- Customer result label is **Preliminary visual diagnosis**.
- Interaction promise: **Show Kevin, do not answer a questionnaire.**
- First slice: HVAC, residential split-system outdoor unit,
  `not_cooling_with_outdoor_unit_noise`.
- Input: call-derived facts plus one 10-20 second narrated video; one optional
  rating-plate photo only if necessary.
- Question budget: zero normally, hard maximum two, one at a time, bounded
  answers, and only when decision-changing.
- At most one targeted media recapture.
- Customer gets up to two evidence-linked possibilities, why they fit, urgency,
  what remains unknown, and next step.
- Contractor gets evidence timestamps, equipment identity/provenance, top-two
  hypotheses, onsite tests, and grounded parts candidates.
- No automatic price, DIY electrical/refrigerant guidance, exact customer-facing
  parts, parts ordering, or guaranteed diagnosis in the MVP.

### Safety and architecture

- Observation extraction, safety assessment, diagnosis ranking, adaptive
  questions, equipment identity, parts compatibility, customer delivery, and
  external contact are separate typed decisions.
- Raw model output and model self-confidence are untrusted.
- Customer delivery is a deterministic, default-hold policy.
- The model cannot author novel safety/repair instructions, approve a part, or
  directly send a result.
- Exact parts can be retrieved only from an approved authoritative OEM or
  distributor source and remain contractor-only with provenance and
  compatibility status.
- Expiry is not deletion; every original, derived, provider, Twilio, object,
  cache, record, and ground-truth copy needs an explicit lifecycle.
- The existing estimate flags are not sufficient. New media processing,
  provider analysis, parts, contractor packet, customer delivery, and feedback
  gates are separate and default off.

### A0-A2 amendment contract

- One lifecycle enum is replaced by an orthogonal case, consent, media,
  analysis, contractor-packet, customer-delivery, and deletion state vector.
- Every structural mutation uses an immutable event ID, kind, canonical payload
  digest, internally computed complete semantic-envelope fingerprint, expected
  revision, source kind, injected time, and `synthetic_offline_only` scope.
- Binding is validated before event lookup. Identical full-envelope replay is a
  no-op that separates historical decision/revision from current revision and
  projection; it never recreates permission. Any semantic mismatch, stale
  revision, skipped prerequisite, or late terminal event rejects.
- A fixed 64-receipt ordinary lane and separate hard-coded six-receipt terminal/
  control lane prevent ordinary saturation from blocking consent termination,
  closure, cancellation/expiry, or deletion request/retries/verification.
- `deletion_pending` freezes every new non-deletion event and late completion;
  verified deletion permits only identical terminal-receipt replay.
- `PendingCustomerAction` is neutral lifecycle plumbing with opaque IDs/copy
  references and stable response codes. It contains no prose, model output,
  diagnosis, safety meaning, parts, or delivery decision.
- Only one customer action can be unresolved. Questions are capped at two;
  rating-plate requests and symptom-media recaptures have separate one-request
  budgets. Replay and transport retry do not increment counters.
- Rejected initial symptom media can reach one targeted recapture. Plate and
  recapture actions remain pending through upload/validation and resolve only
  through a request/kind/asset/role/validation-bound `fulfilled`, refusal, or
  unavailable result before reanalysis.
- A1 is limited to strict provider-neutral structural contracts and projections.
  Safety, delivery, question selection, customer copy, and parts compatibility
  remain A3-A7 responsibilities.
- Internal, customer-safe structural, contractor-safe structural, and audit
  projections are distinct. Schemas reject unknown fields and recursive canary
  tests protect logs, errors, `repr`, and serialized output.
- A2 validates structural transition admissibility only. It does not
  authenticate, authorize, call providers, prove deletion, or decide policy.
- A0-A2 receipt states use only synthetic event IDs/fingerprints, closed status,
  and resulting revision. External receipt slots/payloads and policy meaning
  remain absent until a later reviewed phase.
- Branch transfer and implementation require a detached exact-document receipt
  plus a later explicit owner approval.
- The A0-A2 gate fixes exact dependency/live-entrypoint hashes, path milestones,
  presence-only credential/resource/customer-identifier and ignored-file
  inventory, isolated ambient config discovery, OS-level denied egress, an exact
  pre-existing offline runtime, and a no-collection-time-import guard. The
  source-gap audit stays detached.

### Development sequence

- Tasks A0-A2: first possible slice, structural contracts/state only, synthetic
  fixtures, offline and unmounted.
- Tasks A3-A8: later offline policy/domain work requiring a new plan, allowlist,
  review, and owner authorization.
- Phase B: hardened ingestion/persistence with synthetic media only.
- Phase C: connected provider/catalog qualification with rights-cleared expert
  fixtures only.
- Phase D: explicit-consent contractor-only shadow pilot.
- Phase E: bounded automatic customer-result pilot after quantitative review.
- Phase F: separate general-availability decision.

Passing one phase never authorizes the next.

## Files And Artifacts

- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis/docs/superpowers/specs/2026-08-11-visual-diagnosis-design.md`
  - Product, claims, UX, architecture, contracts, equipment/parts resolution,
    media handling, customer-delivery policy, safety/privacy, current source
    gaps, evaluation, rollout gates, failure handling, and open decisions.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis/docs/superpowers/plans/2026-08-11-visual-diagnosis.md`
  - Authorization boundaries, branch/worktree contract, proposed modules,
    phase-by-phase TDD work, qualification, pilot gates, tests, and review
    matrix.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis/docs/handoffs/2026-08-11-visual-diagnosis-handoff.md`
  - This durable context package.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis/docs/handoffs/2026-08-11-visual-diagnosis-new-session-prompt.md`
  - Paste-ready prompt for the branch agent.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis/app/services/visual_diagnosis_contracts.py`
  - Synthetic A0-A2 contracts and payload-safe structural models.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis/app/services/visual_diagnosis_state.py`
  - Offline state machine, replay/idempotency, bounded lanes, and projections.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis/tests/unit/test_visual_diagnosis_contracts.py`
  - Contract guard and structural validation tests.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis/tests/unit/test_visual_diagnosis_state.py`
  - Deterministic transition, replay, safety, privacy, and matrix traces.

### Detached approval receipt

The exact-diff review result must remain outside these eight allowlisted paths so adding
the result does not change the approved bytes. Schema
`visual_diagnosis_review_receipt/v1` records:

- UTC issue time and review round;
- baseline `d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`;
- full SHA-256 digest and byte count of each of the eight allowlisted paths above;
- exact allowed changed paths for A0-A2;
- staff, security/privacy, and Product/UX reviewer task references and decisions
  after one debate round;
- unresolved findings by severity;
- overall decision, debate result, and canonical receipt digest;
- historical scope `documentation_review_only`; the later owner message records
  `synthetic_offline_A0_A2` implementation authority for the existing isolated
  worktree only;
- `grants_implementation_authority: false`.

The canonical digest covers the versioned receipt body with `receipt_digest`
omitted; the receipt never attempts to hash its own digest field.

Any byte change invalidates the receipt and requires new hashes and targeted
re-review. A reviewer decision, passing test, or conforming receipt never grants
owner authority by itself.

Current source that the next agent must inspect, not blindly extend:

- `app/api/estimates.py`
- `app/services/ai_estimate.py`
- `app/services/post_call.py`
- `app/webhooks/twilio_incoming.py`
- `app/services/gated_actions.py`
- `app/services/side_effect_inventory.py`
- `tests/unit/test_phase0_estimate_gates.py`
- `tests/unit/test_security_audit_f9_f10_f11.py`
- `tests/unit/test_inbound_message_webhook.py`
- `docs/security/phase0-side-effect-matrix.md`
- `docs/security/phase0-release-readiness.md`
- `docs/superpowers/specs/2026-06-30-business-first-dispatch-v2-design.md`
- `docs/superpowers/specs/2026-07-09-hybrid-stateful-ai-receptionist-design.md`

## Current Source Facts

These facts are source-only at `7e9550b…`; they do not prove deployed or
released behavior.

- `app/services/post_call.py` can create an estimate link only when account
  gates permit it.
- `app/api/estimates.py` supports intended JPEG/PNG/HEIC and MP4/QuickTime
  uploads with nominal limits and a 48-hour token.
- `/upload-url` checks type/count, but direct `/upload` does not enforce that
  allowlist or nominal count and trusts `Content-Type`.
- Direct upload reads bytes into the application and sends base64 inline to
  Gemini.
- `analyze_media` accepts `text_description`, but the upload orchestration does
  not pass the caller's explanation.
- The current model response is a one-pass free-text diagnosis, service match,
  price, and model-reported confidence.
- Medium/high model confidence can produce automatic customer `AI Diagnosis`
  and price SMS when gates permit; no contractor review or deterministic
  customer-delivery policy exists.
- Access expiry does not delete the estimate record; no complete media/result
  deletion/export workflow was found.
- Inbound MMS records text plus Twilio media URLs but does not fetch, validate,
  analyze, display, reply, or delete them.
- The iOS app has no visual-diagnosis upload/packet client.
- Current unit tests stub provider diagnosis and do not prove HVAC accuracy,
  safety, equipment identification, or parts compatibility.
- Phase 0 documentation keeps estimate links/results default off.

## Commands Run And Results

```bash
git status --short --branch
git rev-parse --show-toplevel
git rev-parse HEAD
git log -5 --oneline --decorate
git diff --stat
git diff --cached --stat
```

Historical result before writing: clean `main...origin/main`, repository root
`/Volumes/Extreme Pro/MYPROJECTS/Kevin`, HEAD `fecec742…`. During the later
review, another process refreshed the remote-tracking ref to `7e9550b…`; this
documentation task did not fetch or move source. The primary checkout is now
`main...origin/main [behind 2]` with the same local HEAD and four expected
untracked documents.

```bash
rg --files app ios tests | rg -i '(estimate|upload|media|image|video|attachment|diagnos|intake)'
rg -n -i 'estimate.*upload|upload.*estimate|image/|video/|multipart|UploadFile|media_url|attachment|diagnos|gemini.*(image|video)|inline_data|file_data|MAX_UPLOAD_BYTES' app ios tests
```

Result: located current estimate/media prototype, MMS capture, tests, and no iOS
visual-diagnosis client.

```bash
nl -ba app/api/estimates.py
nl -ba app/services/ai_estimate.py
nl -ba app/services/post_call.py
nl -ba app/webhooks/twilio_incoming.py
nl -ba app/services/gated_actions.py
```

Result: confirmed the current flow and gaps summarized above.

## Verification

- Passed: documentation files were created with repo-relative references and
  bound to the exact starting SHA.
- Passed: independent staff review returned `approve with conditions` for
  documentation.
- Passed: security/privacy review produced P0 requirements incorporated in the
  design and plan.
- Passed: the 2026-08-12 panel debate produced a unanimous
  `approve with conditions` direction for a corrected A0-A2 contract.
- Passed: the first frozen-hash review verified all four files byte-for-byte and
  found zero P0; its P1 findings were consolidated in a unanimous repair debate.
- Passed: the source-drift audit inspected the exact seven-path delta from
  `fecec742…` to `7e9550b…`, confirmed no inspected visual-diagnosis gap path or
  unchanged live root moved, and recomputed both changed workflow-root hashes.
- Pending by construction: the amended document bytes need a detached exact-diff
  receipt after their final hashes are frozen.
- Passed: no application, test, config, provider, environment, flag, or
  deployment file was edited.
- Passed: Markdown structural self-review found balanced fences, unique headings,
  no trailing whitespace, clean untracked-file diff checks, consistent tables,
  and all explicitly required repository paths present.
- Historical snapshot: backend/iOS tests were not run because the original task
  changed documentation only; the continuation separately runs the bounded
  A0-A2 contract/state tests.
- Not run: external-reference reachability; current guidance must be re-verified
  at the later phase that would use it.
- Not run: staging/production/provider/device checks; unauthorized and irrelevant
  to the documentation-only outcome.
- Not proven: released-product media capability, HVAC accuracy, safety efficacy,
  parts compatibility, customer value, latency, cost, retention/deletion, or
  production readiness.

## Risks And Watchouts

- **P0 customer safety:** automatic customer diagnosis must not be based on
  model confidence or valid JSON alone.
- **P0 upload security:** the current direct upload trusts headers and is not a
  hardened media-ingestion boundary.
- **P0 privacy:** media/audio/labels can expose faces, voices, property,
  addresses, serials, documents, location, and bystanders.
- **P0 lifecycle:** token expiry is not deletion; derived and provider copies
  need verified cleanup.
- **P0 tenancy/capability:** the current token is a bearer in the URL. New design
  requires one-time scoped exchange and separate customer/contractor rights.
- **P0 output:** exact part numbers must never be model-generated. Compatibility
  needs authoritative provenance and contractor confirmation.
- **P1 architecture:** extending `ai_estimate.py` or the synchronous upload-and-
  SMS handler would continue coupling ingestion, analysis, policy, and contact.
- **P1 evidence:** unit tests with stubbed model output do not qualify real HVAC
  video behavior.
- **P1 coordination:** docs/source/tests remain uncommitted in the isolated
  worktree and the primary checkout remains untouched. Any new agent must verify
  the detached receipt and preserve unrelated work; it must not create another
  worktree.
- **P1 source drift:** the documentation approval expires after decision-changing
  source, provider, policy, or design changes.
- **P1 structural isolation:** A0-A2 must fail before import if candidate modules
  reach config, live entrypoints, I/O capabilities, dynamic imports, providers,
  storage, SMS, or credentials.

## Do Not Do

- Do not reset, clean, stash, discard, or broadly stage the primary checkout.
- Do not commit or push these docs without owner authorization.
- Do not implement directly on `main`.
- Do not interpret the 2026-08-12 documentation-amendment approval as branch or
  implementation authorization.
- Do not silently create a branch from a different baseline.
- Do not fetch, install tools, or update dependencies under A0-A2 authority.
- Do not enable existing estimate flags as a shortcut.
- Do not connect to Gemini/Twilio/GCS/OEM/distributor services without the
  matching approval gate.
- Do not use production or real customer media/transcripts for development.
- Do not put real media, phone numbers, addresses, model/serial values, tokens,
  prompts, responses, or diagnoses in source, fixtures, logs, or reports.
- Do not add another large prompt and call it the diagnosis architecture.
- Do not let model confidence make safety, delivery, or exact-parts decisions.
- Do not send customer results, price, or parts during Phase A-D.
- Do not claim that documentation/tests prove the feature works live.

## Next Recommended Steps

1. Freeze all eight path hashes and issue the detached exact-diff
   staff/security/Product-UX review receipt.
2. Preserve the recorded owner approval for synthetic-offline A0-A2 only.
3. No listed product/provider/domain question blocks the structural A0-A2 slice.
   Licensed reviewer, provider, catalog, retention, frontend, channel,
   languages, and numeric thresholds remain closed later gates.
4. Verify the receipt against the current eight-path isolated worktree.
5. Keep `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis` on
   `codex/visual-diagnosis` bound to the approved SHA.
6. Bind the review to the design, plan, handoff, prompt, and detached receipt.
7. Implement/review Tasks A0-A2 only using TDD.
8. Run focused/full tests, lint, isolation guards, payload scans, deterministic
   state traces, role-safe projection snapshots, and static
   no-live-wiring proof.
9. Obtain exact-diff staff/security/Product-UX review before A3.
10. Keep every provider, real-data, staging, customer, and production gate closed.

## Open Questions

No question blocks reviewing the documentation.

The following block later phases and are intentionally unresolved:

- named licensed HVAC reviewers and adjudication process;
- approved Gemini/Vertex product, account, region, data terms, and retention;
- lawful authoritative OEM/distributor parts sources;
- exact retention periods for each asset/result/feedback class;
- ownership/repository of the `heykevin.one` customer capture page;
- customer result channel;
- approved safe no-tool actions;
- first-pilot languages and translation review;
- representative sample sizes and numeric identity/top-two thresholds.
