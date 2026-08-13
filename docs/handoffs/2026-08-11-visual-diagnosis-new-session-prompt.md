# Hey Kevin Visual Diagnosis New-Session Prompt

Copy everything below into the new agent session.

## Current 2026-08-12 continuation override

The newest user request is `$handoff`. Continue only in the existing isolated
worktree and read the handoff override before acting.

- Worktree: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis`
- Branch: `codex/visual-diagnosis`
- Baseline: `HEAD == origin/main == d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`
- Primary checkout: `/Volumes/Extreme Pro/MYPROJECTS/Kevin`; leave it untouched.
- Eight allowlisted paths are untracked and uncommitted. The handoff/prompt edit expires the prior exact receipt; recompute hashes before claiming readiness.
- Latest evidence before the handoff edit: denied-egress focused `60 passed`, Ruff clean, non-diagnostic unit suite `2046 passed, 19 warnings`.
- Staff/code and Product/UX approved the prior freeze. Security/privacy rejected the exact exit on evidence P1s; no P0.

Required next work:

1. Use realistic digit-only/alphanumeric opaque phone, serial, token,
   provider-reference, and URL canaries in recursive payloads and verify every
   required projection/audit/`repr`/`str`/log/exception sink.
2. Build valid internally recomputed same-event-ID envelope variants for each
   supported mutable field; assert `event_id_conflict`, unchanged revision,
   and unchanged projection. Document neutral pre-ledger rejection for
   unsupported schema/evidence-scope if intentional.
3. Preserve explicit submitted-media expiry/cancellation and duplicate-event
   decision-code tests.
4. Re-run denied-egress focused tests, the non-diagnostic full unit suite,
   Ruff, static/allowlist guards, and two stable eight-file hash checks.
5. Obtain fresh exact-diff staff/security/Product-UX review. Do not stage,
   commit, push, access providers, use live data, deliver customer results, or
   deploy without explicit owner authorization.

Any source or document edit invalidates the current exact receipt. If anything
conflicts, the newest user request wins. Start with `git status --short --branch`.

---

You are continuing Hey Kevin Visual Diagnosis Phase A synthetic-offline A0-A2
implementation under explicit owner approval.

The isolated branch/worktree exists and remains uncommitted. Do not expand to
A3-A8, providers, real data, live flows, or deployment. The current source
baseline is `d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`; fresh hashes and
exact-diff review remain required before the A0-A2 exit gate.

## Current objective

Review the amended design and implementation plan for a visual-first HVAC
preliminary diagnosis feature, then implement only Tasks A0-A2 using TDD in the
existing isolated `codex/visual-diagnosis` worktree. Tasks A3-A8 and every
connected phase remain outside scope. Do not connect providers, process real
media, wire live flows, send messages, or deploy.

## Workspace

- Primary repo: `/Volumes/Extreme Pro/MYPROJECTS/Kevin`
- Bound baseline: `d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`
- Intended implementation worktree:
  `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis`
- Intended branch: `codex/visual-diagnosis`
- The four visual-diagnosis documents originated as uncommitted primary-checkout
  material; the existing isolated worktree now contains the exact eight-path
  allowlist. Preserve primary and unrelated work. Do not reset, clean, stash, or
  broadly stage.
- The exact approval unit is a detached receipt containing the baseline, full
  SHA-256 digest and byte count of all eight allowlisted paths, allowed paths,
  and final panel decision.
- The receipt schema is `visual_diagnosis_review_receipt/v1` and includes issue
  time, review round, document digest/byte-count tuples, per-role task references
  and decisions, findings, overall/debate decisions, canonical receipt digest,
  and `grants_implementation_authority: false`.
- Any document-byte mismatch requires new hashes and targeted re-review.

## Read first

1. `/Volumes/Extreme Pro/MYPROJECTS/Kevin/AGENTS.md`
2. `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis/docs/superpowers/specs/2026-08-11-visual-diagnosis-design.md`
3. `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis/docs/superpowers/plans/2026-08-11-visual-diagnosis.md`
4. `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis/docs/handoffs/2026-08-11-visual-diagnosis-handoff.md`
5. `/Volumes/Extreme Pro/MYPROJECTS/Kevin/docs/security/phase0-side-effect-matrix.md`
6. `/Volumes/Extreme Pro/MYPROJECTS/Kevin/docs/security/phase0-release-readiness.md`
7. `/Volumes/Extreme Pro/MYPROJECTS/Kevin/docs/superpowers/specs/2026-06-30-business-first-dispatch-v2-design.md`
8. `/Volumes/Extreme Pro/MYPROJECTS/Kevin/docs/superpowers/specs/2026-07-09-hybrid-stateful-ai-receptionist-design.md`

Inspect, do not blindly extend:

- `app/api/estimates.py`
- `app/services/ai_estimate.py`
- `app/services/post_call.py`
- `app/webhooks/twilio_incoming.py`
- `app/services/gated_actions.py`
- `tests/unit/test_phase0_estimate_gates.py`
- `tests/unit/test_security_audit_f9_f10_f11.py`
- `tests/unit/test_inbound_message_webhook.py`

## Newest user request

Deli asked Codex to start the documentation needed to develop the feature and
said a new agent would work on it in a branch.

Product direction:

- "Show Kevin, do not answer a questionnaire."
- Reuse the call facts; never ask the customer to repeat the problem.
- Analyze a single narrated 10-20 second HVAC video first.
- Request one rating-plate photo only when needed.
- Ask zero questions normally and no more than two one-tap,
  decision-changing questions.
- Give eligible customers an immediate evidence-linked preliminary diagnosis.
- Give contractors equipment identity, evidence, top-two possibilities, onsite
  tests, and grounded parts-preparation information.
- First scenario: residential HVAC, not cooling plus outdoor-unit noise.
- No automatic price, DIY electrical/refrigerant work, customer exact parts,
  parts ordering, or guaranteed diagnosis.

## Current state

- Primary `main` and `origin/main` were equal and clean at `fecec742…` before
  documentation was created. During exact review, `origin/main` advanced by two
  commits to `7e9550b…`; local `HEAD`/`main` intentionally remain at `fecec742…`.
- A documentation-only audit inspected the seven changed subscription-incident
  paths, confirmed the visual-diagnosis gap paths and unchanged live roots did
  not move, recomputed the two changed workflow-root hashes, and rebound these
  documents to `7e9550b…`. Do not fast-forward primary `main` as a side effect.
- During implementation, `origin/main` advanced from `7e9550b…` to
  `d09c58f…` (PR #163). A second source-gap audit found twelve subscription-
  verification paths only; no visual-diagnosis path, Dockerfile, app/main.py,
  pyproject.toml, or deploy/rollback workflow changed. The isolated branch was
  fast-forwarded to `d09c58f…`; primary `main` remains unmoved.
- During review, `origin/main` advanced from `d09c58f…` to `d2a2f00…`; the
  two-path delta only updates iOS project/version metadata. The branch was
  fast-forwarded again and the current exact baseline is `d2a2f00…`.
- The implementation branch/worktree exists at the recorded path and is bound
  to `d2a2f00…`.
- Only the eight allowlisted visual-diagnosis documents/source/tests are
  untracked and intentionally uncommitted; no provider, gate, deployment, or
  customer flow was changed.
- A 2026-08-12 staff, security/privacy, and Product/UX panel plus debate returned
  `approve with conditions` for a corrected synthetic-only A0-A2 contract;
  automatic customer diagnosis remains the later P0 risk.
- The documents were amended to incorporate the orthogonal state, immutable
  event, neutral pending-action, strict projection, reproducibility, and
  isolation conditions.
- A first exact-hash review verified its bytes but rejected that revision with
  zero P0 and residual P1 state/capacity/gate/proof defects. One debate round
  unanimously supported the consolidated repair now present; only a fresh
  exact-hash receipt can approve these repaired bytes.
- The exact amended bytes require a detached re-review receipt by construction.
- Owner approval is recorded for synthetic-offline A0-A2 only; later product,
  domain, provider, live, and release approvals remain pending/closed.

## Critical constraints

- Newest user request wins.
- The owner approval grants implementation only in the existing isolated
  worktree and only for synthetic-offline A0-A2.
- Do not create another branch/worktree or implement outside A0-A2.
- Do not lose or overwrite the uncommitted documentation while making a branch.
- Do not implement on `main`; use an isolated `codex/` worktree.
- Do not silently rebase to a new SHA. If source moved, stop for a documented
  rebaseline.
- Preserve all unrelated dirty work.
- Tasks A0-A2 are synthetic/offline only; no external I/O and no live imports.
- Do not fetch, install tools, or update dependencies under A0-A2 authority.
- Use only the exact pre-existing Python/Ruff runtime and versions sealed in the
  plan; stop rather than installing or repairing a missing or changed runtime.
- The implementation worktree must have no untracked/local `.env`, credential,
  token, provider payload, customer data, or active cloud-configuration
  material; tracked source/inert examples remain allowed. Unset provider/cloud
  credential variables plus customer/owner/dial-in identifiers, isolate ambient
  config discovery without inspecting private stores, and deny egress before
  candidate import.
- Do not use real customer media, transcripts, phones, addresses, model/serial
  values, tokens, provider payloads, or diagnoses in source/fixtures/logs.
- Do not extend `ai_estimate.py` with another large prompt as the architecture.
- Do not enable or backfill estimate or visual-diagnosis gates.
- Do not call Gemini/Twilio/GCS/OEM/distributor APIs.
- Do not modify IAM, secrets, Cloud Run, Firebase, Twilio, TestFlight, App Store,
  staging, or production.
- Model confidence cannot authorize safety, delivery, or exact parts.
- Customer delivery remains default-hold and unwired.
- Exact parts must be retrieved from an approved authoritative source; never
  generated. Parts stay contractor-only and non-ordering.
- A1 is structural only. It does not implement safety dispositions/copy,
  question selection, delivery eligibility, customer diagnosis content, or
  exact-parts compatibility.
- A2 validates structural transition admissibility only. It does not
  authenticate actors, establish tenant authority, prove deletion, or decide
  diagnosis/safety/delivery/question/parts policy.
- A0-A2 receipt states contain only synthetic event IDs/fingerprints, closed
  status, and resulting revision. External receipt slots/payloads and policy
  meaning remain absent.
- Binding validation precedes receipt lookup. Replay equality covers the full
  internally computed semantic envelope and reports historical versus current
  revision/projection separately; replay never renews permission.
- The fixed 64-receipt ordinary lane cannot consume or select the separate
  hard-coded six-receipt control lane. `deletion_pending` rejects every new
  non-deletion event; deletion retries are exactly attempts 1-3.
- Media follow-up stays pending through upload/validation and resolves only with
  exact request/kind/asset/role/validation binding. Initial rejection can reach
  one recapture; analysis cannot start with an unresolved action.

## Facts and evidence

- Current estimate upload does not connect the caller explanation even though
  `analyze_media` accepts a text description.
- Direct `/upload` does not enforce the allowlist/nominal upload count enforced
  by `/upload-url` and trusts `Content-Type`.
- Current media is read into the application and base64-sent inline to Gemini.
- Model-reported medium/high confidence can lead to automatic `AI Diagnosis`
  and price SMS when old gates permit.
- Token expiry limits access but does not implement complete deletion/export.
- MMS records media URLs but does not validate/analyze/display/delete them.
- Current tests stub provider analysis and do not prove HVAC accuracy or safety.
- Existing Phase 0 controls keep estimate links/results disabled by default.

## Next recommended action

1. Start with the read-only context lock below.
2. Read all required documents completely.
3. Verify the versioned detached advisory receipt: provenance,
   baseline, full SHA-256 hash/byte count of all eight allowlisted paths, allowed path
   set, per-role staff/security/Product-UX results, findings, and receipt digest.
4. Confirm the trusted owner approval still covers only
   `synthetic_offline_A0_A2`; if it does not, stop without implementation.
5. Verify the existing worktree/branch and bound commit without fetching. If
   `origin/main` moved, stop for a documented rebaseline; do not create another
   worktree or silently substitute a SHA.
7. Establish the credential-clean, denied-egress environment and run the
   exact path/dependency/live-root/credential-presence checks. Each test module
   must run its own AST/import-closure/reverse-reachability guard before its
   controlled import; candidate modules must not import during pytest collection.
8. Implement Tasks A0-A2 only:
   - exact-SHA source audit;
   - strict provider-neutral structural contracts and role-safe projections;
   - orthogonal state vector, immutable event envelope, neutral pending action;
   - pure idempotent structural state machine with hard effort budgets, fixed
     receipt lanes, deletion precedence, and media fulfillment binding.
9. Use TDD: demonstrate RED, make the minimum implementation GREEN, run focused
   and full unit tests, Ruff, repository-local isolation guards, recursive
   payload canaries, deterministic state traces, and role-safe projection
   snapshots. Do not install Bandit; run it only if a separately approved pinned
   offline runtime already exists.
10. Request exact-diff staff/security/Product-UX review before Task A3.

## Expected verification

- focused contract/state tests;
- full backend unit suite;
- Ruff plus repository-local pre-import isolation and capability checks;
- no-PII/no-secret/no-payload canary scan;
- strict unknown-field and customer/contractor/audit projection checks;
- event replay/collision/stale-revision/terminal-precedence tests;
- per-field semantic-envelope mismatch and binding-before-lookup tests;
- one-pending-action, two-question, one-plate, and one-recapture budget tests;
- successful plate/recapture fulfillment plus mismatch/refusal/unsafe-refusal,
  retry/restart, cancellation, expiry, and deletion state traces;
- ordinary/control-lane saturation, deletion retry 1-3/fourth-retry rejection,
  deletion freeze, and historical/current replay-projection tests;
- static transitive/reverse no-live-import/wiring check and live-file hashes;
- exact milestone changed-path sets including untracked files, receipt hash
  comparison, credential-variable presence-only and sensitive ignored-file scan;
- `git status`, diff, and exact SHA recorded;
- no provider/network calls;
- no changed environment, gate, deployment, or production state.

## Known risks

- Automatic customer diagnosis is safety-relevant and cannot launch on model
  confidence or schema validity.
- The detached panel receipt is advisory; it never substitutes for owner
  implementation approval.
- Current upload/token/lifecycle path is not safe to reuse unchanged.
- The customer uploader repo/owner is unresolved.
- Licensed HVAC reviewers, provider/data configuration, parts source, exact
  retention, languages, and numeric qualification thresholds remain later gates.

If anything conflicts, the newest user request wins. Start by running:

```bash
cd '/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/visual-diagnosis'
git status --short --branch
git rev-parse --show-toplevel
git rev-parse HEAD
git rev-parse origin/main
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

Do not run `git fetch`, create another branch/worktree, transfer documents, or
import candidate code until the existing exact path, hashes, denied-egress
guard, and trusted owner approval are clear. Any source or document drift
invalidates the current receipt and requires rebaseline and fresh review.
