# Voice Quality Roadmap and Multi-Agent Orchestration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take Hey Kevin from today's verified state (offline bakeoff construction complete, provider execution blocked by design) to a production voice experience that passes ADR 0002's hard gates — callers never talked over, never cut off mid-thought, never left in dead air — selected by sealed evidence and shipped without ever exposing production or its paying customers to experiment risk.

**Architecture:** This is the execution roadmap for the already-panel-approved master plan (`docs/superpowers/plans/2026-07-22-voice-architecture-bakeoff-and-lifecycle-control.md`) and ADR 0002. It does not re-derive that plan's requirements; it sequences the remaining work into phases, defines the multi-agent orchestration machinery (roles, loops, exit criteria), registers every human-owner gate, and fully specifies the immediately executable Phase 0. Later phases whose content is contingent (winner unknown, scope decisions pending) each contain a fully-specified plan-authoring task instead of fabricated code — exactly the pattern the master plan itself uses (its Task 6.2 authors the winner-specific plan).

**Tech Stack:** Python 3.12 / FastAPI / Cloud Run backend; Twilio Media Streams + ConversationRelay; Gemini Live, Deepgram, ElevenLabs; Ed25519 owner-signature approval envelopes (`scripts/run_voice_architecture_bakeoff.py` + `app/services/voice_bakeoff_*`); pytest + ruff via `uv run --extra dev`; GitHub Actions CI (PRs run Test only; staging deploys on push to `staging`; production is manual `workflow_dispatch`).

## Global Constraints

Copied from the governing documents; every task in every phase implicitly includes these.

- **Production is frozen.** No live-path tuning, prompt/model/VAD/pacing change, or controller wiring until the winner-specific integration plan is panel-reviewed and owner-authorized (master plan §2.1, §13, §17).
- **The runner is dry-run-only, not merely dry-run by default.** Connected execution "cannot be added by dependency injection, environment variable, plugin, dynamic import, or a test fake" and requires a separately approved exact-SHA change implementing the seven-step irreversible order in `docs/security/voice-architecture-bakeoff-controls.md` §"Approval and execution" (Phase 4 of this roadmap).
- **Pinned baselines must not drift during offline phases.** `app/experiments/voice_bakeoff_app.py` = `082d82e7…f13127`, `app/main.py` = `e73f0cd4…08b7e9`, `app/webhooks/media_stream.py` = `4dab265d…8e850c` (full digests in `docs/superpowers/plans/2026-07-30-voice-bakeoff-offline-composition-addendum.md`; verified matching on 2026-08-03). Any task touching these files is out of scope until Phase 8.
- **Every provider-connected invocation requires a fresh owner-signed one-use envelope** — one envelope per arm, per window, per retry, never reused (master plan Task 3.4). The owner's signature cannot be delegated to an agent.
- **Nonce-ledger quirk (documented, accepted):** the runner hardcodes `epoch=1` per `manifest_digest`, so a legitimately re-signed approval for the *same* manifest must point `--nonce-ledger` at a fresh path (`docs/security/task-4-8-provider-approval-mechanism.md`).
- **Verification order for every implementation slice** is master plan §14 (focused tests → property tests → affected suites → full suite → ruff → `git diff --check` → payload-safe added-line scans → independent review → CI on exact SHA).
- **Test/lint invocation:** `uv run --extra dev pytest -q` and `uv run --extra dev ruff check <paths>` (CI equivalent: `pip install -e ".[dev]" && pytest --tb=short -q`).
- **Model assignment:** builders = Sonnet 5; per-task reviewers = Sonnet 5 (fresh context); security/boundary reviewers and independent whole-PR reviewers = Opus (fresh context, zero prior involvement); spec authoring, adjudication, synthesis = Fable 5 (orchestrator). Set via the Agent tool's `model` parameter.
- **No customer, production-call, or incidental-caller data anywhere in bakeoff evidence.** Synthetic or purpose-recorded consented audio only (master plan Task 3.1).
- **Commit hygiene:** write non-trivial commit messages to a scratch file and read them back before `git commit -F` (repeat-offense lesson, handoff 2026-08-02). Owner confirmation before any push, PR-open, merge, or branch-delete.

---

## 1. Definition of Done (the end goal, measurably)

The roadmap is complete when all of the following hold:

1. **Evidence-backed selection:** `docs/adr/0003-voice-runtime-selection.md` exists and records a winner (or a valid `no_winner`/`insufficient_evidence`, which stops the roadmap at Phase 7 by design) from two independently sealed technical windows plus the sealed closed-loop caller-UX window, with every ADR 0002 hard gate passed — including interruption→clear p95 ≤ 250 ms / max 500 ms, caller-speech-onset→last-audible-assistant-sample p95 ≤ 750 ms / max 1,000 ms, zero premature closures, zero accepted ceiling hits, 100% complete thoughts.
2. **Winner integrated:** the winner-specific integration plan (master plan Task 6.2) is executed, all flags default-off, with exact-SHA staging qualification passed **twice** (master plan §15) including rollback rehearsal.
3. **Production shipped safely:** a separately reviewed production release plan (master plan §17) is executed via owner-dispatched `gh workflow run deploy.yml -f target=production --ref main`, with monitoring live.
4. **Verified better, sustained:** for 14 consecutive days post-release, payload-safe production telemetry (the `voice_telemetry.py` allowlist projection) shows zero premature-closure terminals, zero accepted ceiling-hit turns, 100% response-terminal coverage, and interruption-clear latency within the sealed gate. Regressions trigger the L5 loop (rollback → diagnose → fix → redeploy) until the 14-day window holds.
5. **Cleaned up:** losing candidate adapters dispositioned per Task 6.2's exclusion rules; the 30 pre-existing draft `codex/*` PRs closed or harvested per the artifact-audit classifications (owner approval per PR); all governing docs current.

## 2. Verified current position (2026-08-03)

Established directly this session, not from memory:

- **Done and on `main`:** Stage 0 docs (artifact audit, provider-capability matrix, ADR 0002, security annex, release gates); Stage 1 lifecycle/telemetry/evaluator; Stage 2 session-auth contract + qualification doc; Stage 3 runner + approval schema + **the real Task 3.4 approval mechanism** (PR #134: Ed25519 verification, durable one-use nonce ledger, nonproduction credential broker, residue audit, owner sign CLI, review-receipt library — its 6 test files pass 111/111 today); Stage 4 offline adapters for all four arms, isolated bakeoff app, caller harness; the 2026-07-30 addendum increments (`voice_bakeoff_turn_composition.py`, `voice_bakeoff_session_driver.py`). All three addendum-pinned files match their sealed digests.
- **Blocking, by design:** `docs/voice-architecture-caller-ux-acceptance.md` is an **unsealed proposal** — its digest must be bound into every probe envelope, so nothing provider-connected can run until it is sealed. Connected execution itself is deliberately absent from the shipped runner. No approval envelope has ever been sealed; the owner signing key does not exist yet. No nonproduction provider estate (Twilio subaccount, provider keys, evidence KMS/storage) exists — only the reference-only Firestore observation project.
- **Scope decision pending (owner):** the drafted closed-loop participant window specifies 360 main-study participants + 90/stratum recruitment overhead + 32 unlisted-language challenge participants across 3 language cohorts, 6 endpoint strata, and 3 age bands. The master plan's own text provides the legitimate pre-seal lever: *"If the approved cost cap cannot support valid evidence, narrow the candidate set or declared language contract before sealing; never weaken counts or thresholds after unsealing"* (Task 3.2).
- **Deferred polish backlog** from PR #134's independent review (§9 below).
- Local `main` is 1 commit behind `origin/main` (`fb628eb`); fast-forward is Phase 0's first step.

## 3. Orchestration model

### 3.1 Roles

| Role | Model | Context | Responsibility |
| --- | --- | --- | --- |
| Orchestrator | Fable 5 | persistent (this session lineage) | Owns this roadmap; writes/curates specs; dispatches and sequences all agents; adjudicates ambiguity (`/agent-staff` pattern); never merges or pushes without owner confirmation |
| Builder | Sonnet 5 | fresh per task | Implements exactly one task's spec, TDD, following master plan §14 verification order; reports evidence, not narrative |
| Task reviewer | Sonnet 5 | fresh per review | Reviews one task's diff against its spec; findings must cite file:line and be empirically checked |
| Security/boundary reviewer | Opus | fresh | Reviews any task touching auth, envelopes, credentials, isolation, or lifecycle authority; instructed to attempt to refute the builder's claims, not confirm them |
| Independent PR reviewer | Opus | fresh, zero prior context | Whole-branch review before merge; told to verify claims empirically. **Never skipped** — in the PR #134 cycle, every genuinely fresh reviewer found real issues that 8+ prior rounds missed, through the very last round |
| Envelope review-receipt issuer | Opus | fresh, distinct identity string | Phase 5+: audits an exact approval envelope + source SHA and issues the advisory `TechnicalReviewReceipt` via `scripts/request_voice_bakeoff_review.py`. Its `provenance_ref` must differ from `owner_authorization.identity` — the runner enforces procedural separation |
| Owner (human) | — | — | Everything in §4. The only party who can sign, provision, seal, recruit, merge, and deploy |

### 3.2 Loops (all loops have exit criteria and iteration caps — none run open-ended)

- **L1 — Task build loop.** Spec → builder implements (TDD) → task review (+ security review if boundary-touching) → builder fixes → re-review. **Exit:** zero findings AND §14 verification green. **Cap:** 4 rounds, then the orchestrator re-examines the spec itself (the task, not the builder, is usually wrong by round 4) or escalates to the owner.
- **L2 — Merge loop.** Branch complete → independent fresh PR review (empirical) → fix → second fresh confirmation review. **Exit:** a fresh reviewer reports zero real findings. **Cap:** 3 fresh reviewers; unresolved disagreement escalates to owner with both positions stated.
- **L3 — Document/contract review loop.** Draft → three fresh reviewers with distinct lenses (staff-architecture, security/privacy, conversation-product — mirroring the master plan's panel) → P1s fixed → re-review of the exact corrected file. **Exit:** no unresolved P1 → owner authorization recorded. **Cap:** 3 panel rounds, then owner huddle.
- **L4 — Capability-probe loop (Phase 5, per arm).** Assemble manifest+approval → fresh receipt (envelope reviewer) → owner signs (emit → sign → embed, 3 commands) → run probe → update capability matrix → residue audit → if a required protocol fact failed: diagnose, fix offline via L1, re-freeze digests, fresh envelope (fresh nonce-ledger path if same manifest), re-run. **Exit:** the arm's Task 0.1 capability matrix is complete, or the arm is eliminated/marked control-only. **Cap:** 3 envelope cycles per arm, then owner huddle — each cycle costs an owner signature, which is the natural throttle.
- **L5 — Production quality loop (Phase 10).** Monitor KPIs (§1.4) → regression triggers rollback via `.github/workflows/rollback.yml` → diagnose (systematic-debugging skill) → fix via L1/L2 → owner redeploys → re-monitor. **Exit:** KPIs hold 14 consecutive days. **Cap:** none on iterations, but 2 consecutive rollbacks of the same cause escalate to an owner architecture huddle rather than a third redeploy.

### 3.3 Standing rules for all agent work

- Feature work in isolated git worktrees (superpowers:using-git-worktrees); the primary worktree stays on `main`.
- Builders receive their task spec verbatim plus pointers to the exact governing-doc sections — never a summary of a contract in place of the contract.
- Any reviewer finding of "plausible but unverified" is treated as unresolved — claims about behavior require a command run and its output.
- Agents never handle raw credentials, never create provider accounts, never sign envelopes, never dispatch production deploys. When a task reaches such a point it stops and hands the owner a runbook (§4).
- A `git diff <ref> -- <file>` with no second ref shows `-` = ref content, `+` = working tree. Verify direction before reporting version comparisons (repeat-offense lesson, handoff 2026-08-02).

## 4. Owner gate registry

Every point where the human is load-bearing. Agents prepare everything up to the gate; the owner's action at the gate is deliberately small.

| # | Gate | Phase | Owner does | Agents prepare | Est. frequency |
| --- | --- | --- | --- | --- | --- |
| G1 | Merge approvals | all | "merge it" per PR | L2-clean branch + PR | per PR |
| G2 | Scope decision: arms to probe, declared language matrix, participant-window scale | 1 | pick among presented options | decision brief with cost/evidence tradeoffs per option | once |
| G3 | Nonprod estate provisioning | 2 | run console/CLI steps from runbook (Twilio subaccount + numbers, provider nonprod keys, GCP nonprod project KMS/storage/log sink, env vars per `BAKEOFF_NONPROD_CREDENTIAL__{ROLE}` convention) | runbook + offline verification script | once (+ key rotations) |
| G4 | Signing-key bootstrap | 2 | run the 2-command Step 0 workflow (now with `--create-key` after Phase 0) | exact commands, verification | once |
| G5 | Contract sealing authorizations (caller-UX contract, corpus manifest, language contract) | 3 | record authorization after L3-clean review | sealed text + digests | ~3 documents |
| G6 | Connected-execution plan authorization | 4 | authorize the exact plan file after L3 | the plan + panel results | once |
| G7 | Envelope signatures | 5, 6, 7 | emit → sign → embed (3 commands per envelope, batched into sittings) | manifest, approval JSON, receipt, runbook | ~4–12 (Phase 5) + 2/surviving arm (Phase 6) + 1/arm (Phase 7) |
| G8 | Rater/participant program | 6, 7 | approve budget; source raters (3 blinded, language-fluent) and participants per sealed protocol | protocols, screening scripts, consent forms | per window |
| G9 | Staging deploys | 9 | approve push to `staging` | qualified branch | per qualification round |
| G10 | Production release | 10 | authorize release plan; run `gh workflow run deploy.yml -f target=production --ref main` | release plan (L3-clean), smoke evidence | once + rollback redeploys |
| G11 | Draft-PR disposition | 10 | approve close/harvest per PR | per-PR recommendation from artifact audit | once, batched |

## 5. Phases

Dependencies are strictly ordered except where noted. Each phase states objective, entry, work, loops, exit, owner gates, and estimated agent scale (dispatches, all models).

### Phase 0 — Ground truth, hygiene, and deferred fixes *(fully specified in §6; executable immediately)*

- **Objective:** local/remote sync; a committed, evidence-cited offline-gate status report; accurate docs (two files currently claim the app is pre-launch, which misled this very session); the cheap high-value polish fixes from PR #134's independent review.
- **Entry:** none. **Loops:** L1 per task, L2 once for the combined branch. **Exit:** all §6 tasks merged; every master-plan offline gate in the report is green with cited evidence, spawned as an L1 fix task and closed, or explicitly recorded as a gap and owner-accepted with reason at the G1 merge gate. **Owner:** G1. **Scale:** ~12–18 dispatches.

### Phase 1 — Scope decision and caller-UX contract revision

- **Objective:** resolve G2, then revise `docs/voice-architecture-caller-ux-acceptance.md` to match the decided scope so it can seal in Phase 3.
- **Entry:** Phase 0 gate report green.
- **Work:** (a) Orchestrator (Fable) writes a **decision brief** presenting, at minimum: probe all four arms vs. a subset (recommendation: probe all four — probes are bounded and cheap relative to what elimination evidence saves later); declared language matrix as drafted (en/es/zh + 2 code-switch pairs) vs. `en_us_general`-first (collapses rater-fluency and participant-strata requirements dramatically; note the product markets all-language support, but the bakeoff only ever evidences the declared matrix either way — master plan Task 3.1); participant window as drafted (360+32 participants, ~482 recruited) vs. a narrowed pre-seal design with an independently validated power calculation. Every option cites the governing text permitting it. (b) Owner decides (G2). (c) A builder revises the contract per the decision; the revision untangles the seal-prerequisite list so items that gate only the participant window (participant-data protocol, recording authorization) are explicitly separately-sealed later, per the master plan's own "separately approved" language for Task 5.5. (d) L3 panel review to P1-zero.
- **Exit:** revised contract text L3-clean and awaiting only Phase 2's estate values. **Owner:** G2. **Scale:** ~8–12 dispatches.

### Phase 2 — Nonproduction estate provisioning (owner-executed)

- **Objective:** the dedicated nonproduction identities the security annex requires: Twilio subaccount + two nonprod numbers (harness caller + callee), Deepgram/Gemini/ElevenLabs nonprod keys, GCP nonprod project (extending the existing reference-only observation project where appropriate) with KMS key, encrypted evidence bucket, log sink, auth-token Firestore; environment variables per `BAKEOFF_NONPROD_CREDENTIAL__{ROLE}` / `BAKEOFF_NONPROD_ACCOUNT_REGION__{ROLE}`; owner signing key via the Step 0 bootstrap (G4).
- **Entry:** Phase 1 scope decision (it determines which provider roles are needed). Note: prior sessions recorded the owner's gcloud account/permissions as intermittently blocked (memory S17/S18, May 2026) — the runbook's first step is verifying `gcloud auth list` shows a working account, and the runbook must not assume it.
- **Work:** builder writes `docs/security/bakeoff-nonprod-provisioning-runbook.md` (L1+L3-security-reviewed) with exact console/CLI steps, then a **no-network verification script** `scripts/verify_bakeoff_nonprod_env.py` + tests (L1): checks env-var presence and digest-matches against a provided reference file, resolves every role through `NonproductionCredentialBroker`, asserts the production denylist entry rejects, never prints a credential. Owner executes the runbook (G3, G4).
- **Exit:** verification script passes against the owner's environment; estate enumeration (accounts/regions/retention postures) recorded for the contract and envelopes. **Owner:** G3, G4. **Scale:** ~6–8 dispatches.

### Phase 3 — Seal the contracts

- **Objective:** sealed caller-UX acceptance contract, sealed corpus manifest (master plan Tasks 3.1/3.2: synthetic PCMU corpus with the decided language matrix and all numeric denominators), owner-approved language contract; digests recorded for envelope binding.
- **Entry:** Phases 1–2 complete.
- **Work:** builders complete the corpus manifest and synthetic fixture set (offline construction — permitted now); the contract gains the real estate values from Phase 2; L3 panel on each sealable document; owner authorizes each (G5); digests pinned in a committed record.
- **Exit:** every digest the Task 3.4 envelope schema requires exists and is owner-sealed. **Owner:** G5. **Scale:** ~10–15 dispatches.

### Phase 4 — Connected-execution capability (separately approved exact-SHA change)

- **Objective:** the runner gains real connected execution implementing the seven-step irreversible order (controls annex §"Approval and execution"): envelope verify → signature+receipt verify → custody/binding proof → atomic nonce consume + active-execution record → nonprod credential resolution → per-dependency account/region/privacy attestation → bounded candidate-specific workload capability. Plus `--execute-provider` re-added under this approved change, runner→`voice_bakeoff_app.py`/caller-harness invocation, and the active-execution record store against the nonprod Firestore.
- **Entry:** Phase 3 sealed digests (envelope tests need real digest shapes); Phase 2 estate (attestation targets).
- **Work:** **Plan-authoring task:** Fable writes `docs/superpowers/plans/<date>-bakeoff-connected-execution.md` to full writing-plans standard (exact files, test-first steps with code, per-task gates), covering the complete negative-test matrix from master plan Tasks 2.3 and 3.4 (unsigned/forged/wrong-owner/replayed/expired/revoked-key/credential-swapped/dependency-omitted/destination-mismatched/production-bound envelopes all fail at the earliest boundary). L3 panel review; owner authorizes the exact plan (G6). Then L1 per task (every task is boundary-touching → Opus security review mandatory), L2 for the branch.
- **Exit:** full negative matrix green; independent security review zero findings; merged; the runner with a valid envelope but no `--execute-provider` still behaves exactly as today (verdict `blocked_external_verification_required`, exit 3), and `--execute-provider` without a valid envelope fails at the earliest boundary. **Owner:** G1, G6. **Scale:** ~25–40 dispatches (largest build phase before integration).

### Phase 5 — Task 4.8 bounded capability probes (L4 loop per arm)

- **Objective:** for each in-scope arm, direct evidence for every unresolved Task 0.1 protocol fact: dependency attestation, input-finality/pre-response-permit ordering, generation lifecycle, transport-vs-caller-playback correlation, interruption/audible-stop, reconnect/epoch, no-media-before-auth. Non-scoring by definition; B2 stays control-only unless its real-time playback receipt is proven.
- **Entry:** Phase 4 merged; sealed digests; estate live.
- **Work:** per arm, the L4 loop as defined in §3.2. Agents run the runner and evaluator; the owner's involvement per cycle is exactly the emit→sign→embed command triplet (G7), batched. After all arms: freeze source/model/prompt/config per master plan; capability matrix committed (payload-safe).
- **Exit:** master plan Task 4.8 gate — matrix complete, residue audits pass, arms frozen or eliminated. **Owner:** G7 (~4–12 signatures). **Scale:** ~15–30 dispatches.

### Phase 6 — Sealed technical windows and adjudication

- **Objective:** two independently sealed windows per surviving arm (12 calls / 60 turns / 10 interruptions / 8 silence cases minimum each, plus all preregistered denominators), evaluated by `scripts/evaluate_voice_architecture_bakeoff.py`, semantically adjudicated by the 3-blinded-rater protocol, residue-audited, producing per-arm hard-gate tables (master plan Tasks 5.1–5.4, 5.6).
- **Entry:** Phase 5 freeze; Task 5.1 requalification of the exact frozen harness.
- **Work:** agents prepare envelopes/manifests, run windows, stream evaluator, compile aggregates; **humans rate** (G8 — 3 blinded raters, language-fluent per the sealed matrix; this is why the Phase 1 language decision dominates cost). No in-window patching — a hard-gate failure ends that arm's window (master plan rule); fixes mean a new freeze and full rerun, decided at an owner huddle.
- **Exit:** both windows' gate tables sealed; surviving-arm set known; aggregate package L3-reviewed and owner-authorized. **Owner:** G7 (2 envelopes/surviving arm), G8. **Scale:** ~15–25 dispatches + human rating program.

### Phase 7 — Closed-loop caller-UX acceptance window

- **Objective:** the sealed participant window at the scope decided in Phase 1, per the sealed contract: whole-call hard gates, counterbalanced arms, consenting participants on real handsets.
- **Entry:** ≥1 arm survived Phase 6; separately-sealed participant-data protocol (deferred from Phase 1) L3-clean and owner-authorized.
- **Work:** agents produce protocols, schedules, screeners, consent instruments, and analysis code (all preregistered); the owner runs recruitment/facilitation or contracts it out (G8). Agents compute preregistered analyses only.
- **Exit:** hard gates pass → eligible winner(s); any failure → valid `no_winner`, roadmap stops here and the owner decides next steps with the evidence in hand. **Owner:** G7, G8 (dominant cost). **Scale:** ~10–20 dispatches + the human program.

### Phase 8 — Selection ADR and winner integration plan

- **Objective:** `docs/adr/0003-voice-runtime-selection.md` (master plan Task 6.1) and the winner-specific design + implementation plan (Task 6.2) — the latter is **the** detailed Sonnet-builder spec for integration: exact files, provider-neutral coordinator ownership, flags all-off-by-default, migration/rollback/coexistence, full test matrix, harvesting winner-arm code only after exact-diff review (this is also where anything reusable from the 30 draft `codex/*` PRs gets pulled in, per the artifact-audit classifications — never wholesale merges).
- **Entry:** Phase 7 winner. **Loops:** L3 on both documents (master plan requires three-panel exact-file approval). **Exit:** owner authorizes the exact integration plan (Task 6.3). **Owner:** G1, G5-equivalent authorizations. **Scale:** ~10–15 dispatches.

### Phase 9 — Integration build and exact-SHA staging qualification

- **Objective:** execute the Task 6.2 plan task-by-task (L1 per task, Sonnet builders, Opus on boundary tasks; L2 per branch), then master plan §15 staging qualification: deploy via the protected staging workflow, two complete windows with the aggregate evaluator, synthetic/consented callers only, rollback rehearsal proving the prior SHA restores.
- **Entry:** Phase 8 authorization. **Exit:** two green staging windows at exact SHA + rehearsed rollback. **Owner:** G1, G9. **Scale:** ~40–80 dispatches (the big build).

### Phase 10 — Production release, post-release loop, cleanup

- **Objective:** the separately reviewed production release plan (§17: exact artifacts, migration, monitoring KPIs from the `voice_telemetry.py` allowlist, rollback triggers, incident ownership, post-release audit); owner-authorized release; L5 loop to the 14-day KPI hold; then cleanup (losing-adapter disposition, G11 draft-PR closure/harvest, doc currency pass, final report).
- **Entry:** Phase 9 complete. **Loops:** L3 (release plan), L5 (post-release). **Exit:** §1 Definition of Done, all five items. **Owner:** G10, G11. **Scale:** ~15–25 dispatches.

---

## 6. Phase 0 — Detailed task specifications

Five tasks. Tasks 0.1–0.2 are independent of 0.3–0.5 and may run in parallel worktrees. One combined branch and PR at the end (or two — hygiene vs. polish — at the orchestrator's discretion), L2 before merge.

### Task 0.1: Sync, full verification sweep, and offline-gate status report

**Files:**
- Create: `docs/voice-bakeoff-offline-gate-report-2026-08.md`

**Interfaces:**
- Consumes: master plan Tasks 1.1–4.7 gate statements + addendum delivery-sequence items.
- Produces: the evidence baseline every later phase's entry criteria cite.

- [ ] **Step 1: Fast-forward the primary worktree**

```bash
git status --short --branch   # confirm clean apart from known untracked files
git pull --ff-only
```
Expected: `Updating 5681fb6..fb628eb  Fast-forward` (or already up to date). If not fast-forwardable, STOP and report — do not merge or rebase.

- [ ] **Step 2: Run the full verification sweep and capture output**

```bash
uv run --extra dev pytest -q 2>&1 | tail -5
uv run --extra dev ruff check app scripts tests
git diff --check
shasum -a 256 app/experiments/voice_bakeoff_app.py app/main.py app/webhooks/media_stream.py
```
Expected: full suite passes; ruff clean; no whitespace errors; the three digests equal the addendum's pinned values (§Global Constraints). Any failure becomes its own L1 fix task before this task can complete — record it in the report either way.

- [ ] **Step 3: Write the gate report**

One table row per master-plan offline gate (Tasks 1.1, 1.2, 1.4, 2.1, 2.2, 2.3, 3.1-dev-tier, 3.4, 4.0–4.7, addendum increments 1–4). Columns: master-plan task, gate statement (quoted or tightly paraphrased with section reference), evidence (exact test file(s)/command and observed result from Step 2's run — cite test node IDs for load-bearing gates), verdict (`green` / `gap: <description>`). Close with: digests table (three pinned files + observed values), full-suite pass count, date, source SHA. Payload-safe: no credentials, transcripts, phone-shaped values, or raw IDs. The report must **not** claim any provider-connected, staging, or caller-experience fact — offline evidence only, labeled as such (master plan §14 rule).

- [ ] **Step 4: Reviewer verification pass (L1)**

Reviewer instruction: for every `green` verdict, re-run at least the cited command and confirm the citation is real; any gate marked green without reproducible evidence is a finding.

- [ ] **Step 5: Commit**

```bash
git add docs/voice-bakeoff-offline-gate-report-2026-08.md
git commit -F /path/to/reviewed-commit-message.txt
```

### Task 0.2: Documentation accuracy fixes (the app is live; stop saying otherwise)

**Files:**
- Modify: `CLAUDE.md` (App Store credentials bullet; Deployment→Backend block)
- Modify: `AGENTS.md` (same bullet; `.Codex/` path; `Codex (call summaries)`; missing env-var rows; embedded stale memory block)

**Interfaces:** none — prose only. No code, no behavior.

- [ ] **Step 1: Fix the stale pre-launch line in both files**

In `CLAUDE.md`, replace:
```markdown
- `APPSTORE_ENVIRONMENT=sandbox` (change to `production` for App Store launch)
```
with:
```markdown
- `APPSTORE_ENVIRONMENT` — `production` on the production service, `sandbox` on staging (see Environments table). The app is live in the App Store; do not read this row as "pre-launch".
```
Apply the same replacement in `AGENTS.md` (its copy reads identically today).

- [ ] **Step 2: Fix the contradictory deploy block in `CLAUDE.md`**

Replace the Deployment → Backend fence:
```bash
# Deploy to production
gcloud run deploy kevin-api --source . --project kevin-491315 --region us-central1 --allow-unauthenticated

# Or just push to main — GitHub Actions deploys automatically
git push origin main
```
with the corrected version already used in `AGENTS.md`:
```bash
# Normal production deploy: use the manual GitHub Actions workflow from main
gh workflow run deploy.yml -f target=production --ref main

# Smoke-test staging before production
scripts/smoke_release.sh https://kevin-api-staging-l63rergg7a-uc.a.run.app staging
```
(The later "Branches and deploy triggers" section in `CLAUDE.md` is already correct; this makes the file agree with itself.)

- [ ] **Step 3: Fix the bad find-replace artifacts in `AGENTS.md`**

- `├── .Codex/` → `├── .claude/` (repository-structure diagram)
- `| \`ANTHROPIC_API_KEY\` | Codex (call summaries) |` → `| \`ANTHROPIC_API_KEY\` | Claude (call summaries) |`

- [ ] **Step 4: Sync `AGENTS.md`'s env-var table and remove the stale embedded memory block**

Append to its Environment Variables table, copied **verbatim** from `CLAUDE.md`'s rows: `VCARD_HMAC_SECRET`, `PIN_RATE_LIMIT`, `PIN_RATE_WINDOW_SECONDS`, `MAX_UPLOAD_BYTES`, `TRANSCRIPT_ENCRYPTION_KEY`. Then delete the entire `<claude-mem-context>…</claude-mem-context>` block at the end of `AGENTS.md` (a July memory dump accidentally committed into the doc; it re-pollutes every agent that reads the file).

- [ ] **Step 5: Verify and commit**

```bash
grep -n "Codex\|change to \`production\` for App Store launch\|claude-mem-context" AGENTS.md CLAUDE.md
```
Expected: no matches. Commit with a reviewed message.

### Task 0.3: `sign_voice_bakeoff_approval.py` — require explicit `--create-key`

A typo'd `--key` path today silently mints a brand-new signing identity; the fix makes key creation explicit. (PR #134 independent-review item.)

**Files:**
- Modify: `scripts/sign_voice_bakeoff_approval.py`
- Modify: `tests/unit/test_sign_voice_bakeoff_approval.py`
- Modify: `docs/security/task-4-8-provider-approval-mechanism.md` (Step 0 bootstrap command)

**Interfaces:**
- Produces: `load_owner_key(key_path: pathlib.Path, *, create: bool) -> ed25519.Ed25519PrivateKey` (replaces `load_or_create_owner_key(key_path)`); CLI flag `--create-key` (store_true); missing key without the flag → message on stderr, exit 2.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_sign_voice_bakeoff_approval.py`:
```python
def test_missing_key_without_create_key_flag_is_an_error(tmp_path, capsys):
    payload = tmp_path / "payload.json"
    payload.write_text("{}")
    missing = tmp_path / "typo-dir" / "owner_key.pem"

    rc = main(
        [
            "--key", str(missing),
            "--payload", str(payload),
            "--domain-name", "approval",
        ]
    )

    assert rc == 2
    assert not missing.exists()
    assert "--create-key" in capsys.readouterr().err


def test_create_key_mints_once_then_later_runs_reuse_it(tmp_path, capsys):
    payload = tmp_path / "payload.json"
    payload.write_text("{}")
    key = tmp_path / "owner_key.pem"

    rc_first = main(
        [
            "--key", str(key),
            "--payload", str(payload),
            "--domain-name", "approval",
            "--create-key",
        ]
    )
    assert rc_first == 0
    first_signature = capsys.readouterr().out.strip()

    rc_second = main(
        [
            "--key", str(key),
            "--payload", str(payload),
            "--domain-name", "approval",
        ]
    )
    assert rc_second == 0
    assert capsys.readouterr().out.strip() == first_signature
```
(Ed25519 signing is deterministic: same key + same payload → identical signature, so the equality assertion is sound.)

- [ ] **Step 2: Run to verify failure**

```bash
uv run --extra dev pytest tests/unit/test_sign_voice_bakeoff_approval.py -q
```
Expected: the two new tests FAIL (unrecognized `--create-key`; missing-key path currently creates and returns 0).

- [ ] **Step 3: Implement**

Replace `load_or_create_owner_key` with:
```python
def load_owner_key(
    key_path: pathlib.Path, *, create: bool
) -> ed25519.Ed25519PrivateKey:
    if key_path.exists():
        _require_owner_only_permissions(key_path)
        raw = key_path.read_bytes()
        return ed25519.Ed25519PrivateKey.from_private_bytes(raw)

    if not create:
        raise FileNotFoundError(
            f"owner key not found at {key_path}; pass --create-key to mint a "
            "new keypair. A missing key file must fail loudly — a mistyped "
            "--key path must not silently sign under a fresh identity."
        )

    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = ed25519.Ed25519PrivateKey.generate()
    raw = private_key.private_bytes_raw()
    # Bake the restrictive mode into the creation syscall itself so the file
    # never exists, even momentarily, at the default umask-widened mode.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    return private_key
```
In `main()`, add the flag and the error path:
```python
    parser.add_argument(
        "--create-key",
        action="store_true",
        help=(
            "Explicitly allow minting a new Ed25519 keypair at --key when no "
            "file exists there. Without this flag a missing key file is an "
            "error, so a typo'd path cannot create a fresh signing identity."
        ),
    )
    args = parser.parse_args(argv)

    try:
        private_key = load_owner_key(args.key, create=args.create_key)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
```
Then `grep -rn "load_or_create_owner_key" tests/ scripts/ docs/` and update every call/reference site to the new signature (tests use `create=True` where they previously relied on implicit creation).

- [ ] **Step 4: Update the mechanism doc's Step 0**

In `docs/security/task-4-8-provider-approval-mechanism.md`, add `--create-key \` to the Step 0 bootstrap command block, and revise the surrounding sentence "There is no dedicated "create-key" flag" to state the flag now exists and is required for first-time creation.

- [ ] **Step 5: Verify and commit**

```bash
uv run --extra dev pytest tests/unit/test_sign_voice_bakeoff_approval.py tests/unit/test_run_voice_architecture_bakeoff.py -q
uv run --extra dev ruff check scripts/sign_voice_bakeoff_approval.py tests/unit/test_sign_voice_bakeoff_approval.py
```
Expected: all pass, ruff clean. Commit with a reviewed message.

### Task 0.4: Constant-time credential comparisons + public `APPROVAL_DOMAIN`

Two small PR #134 review items. (a) The broker compares SHA-256 digests with `!=`; use `hmac.compare_digest`. (b) The sign CLI imports private `_APPROVAL_DOMAIN` across a module boundary; export a public name (sibling `approval_signature_payload` was already exported for exactly this reason).

**Files:**
- Modify: `app/services/voice_bakeoff_credential_broker.py`
- Modify: `app/services/voice_bakeoff_security_contracts.py`
- Modify: `scripts/sign_voice_bakeoff_approval.py`
- Test: `tests/unit/test_voice_bakeoff_credential_broker.py` (existing behavior must stay green; add mismatch cases only if not already covered)

**Interfaces:**
- Produces: `APPROVAL_DOMAIN` (public, `== _APPROVAL_DOMAIN`) in `voice_bakeoff_security_contracts.py`.

- [ ] **Step 1: Confirm existing mismatch coverage**

```bash
uv run --extra dev pytest tests/unit/test_voice_bakeoff_credential_broker.py -q
grep -n "approved_credential_ref\|approved_account_region_ref" tests/unit/test_voice_bakeoff_credential_broker.py
```
If wrong-ref → `None` cases are absent for either parameter, add one test per parameter first (assert `resolve(...)` returns `None` when the env value's digest does not equal the approved ref).

- [ ] **Step 2: Swap to constant-time comparison**

In `voice_bakeoff_credential_broker.py`, add `import hmac` to the imports, and replace:
```python
        if _digest(credential_value) != approved_credential_ref:
            return None
        if _digest(account_region_value) != approved_account_region_ref:
            return None
```
with:
```python
        if not hmac.compare_digest(_digest(credential_value), approved_credential_ref):
            return None
        if not hmac.compare_digest(
            _digest(account_region_value), approved_account_region_ref
        ):
            return None
```

- [ ] **Step 3: Export the approval domain publicly**

In `app/services/voice_bakeoff_security_contracts.py`, directly below the `_APPROVAL_DOMAIN` definition:
```python
# Public alias: external signing tooling (scripts/sign_voice_bakeoff_approval.py)
# must sign under the exact domain bytes the verifier checks — exported for the
# same reason approval_signature_payload is.
APPROVAL_DOMAIN = _APPROVAL_DOMAIN
```
In `scripts/sign_voice_bakeoff_approval.py`, change the import to `from app.services.voice_bakeoff_security_contracts import APPROVAL_DOMAIN` and update the one usage in `_DOMAIN_NAME_TO_BYTES`. Then `grep -rn "_APPROVAL_DOMAIN" app/ scripts/ tests/` — any remaining cross-module import of the private name is in scope to switch; same-module uses stay.

- [ ] **Step 4: Verify and commit**

```bash
uv run --extra dev pytest tests/unit/test_voice_bakeoff_credential_broker.py tests/unit/test_sign_voice_bakeoff_approval.py tests/unit/test_run_voice_architecture_bakeoff.py -q
uv run --extra dev ruff check app/services/voice_bakeoff_credential_broker.py app/services/voice_bakeoff_security_contracts.py scripts/sign_voice_bakeoff_approval.py
```
Expected: all pass. Commit with a reviewed message.

### Task 0.5: Comment-density normalization in the five PR #134 modules

The five new modules carry review-history narrative far above house style (sibling `voice_bakeoff_security_contracts.py` is near-zero-comment). Judgment task — review-driven, not a code-block spec.

**Files:**
- Modify: `app/services/voice_bakeoff_nonce_ledger.py`, `app/services/voice_bakeoff_credential_broker.py`, `app/services/voice_bakeoff_residue_audit.py`, `scripts/sign_voice_bakeoff_approval.py`, `scripts/request_voice_bakeoff_review.py`

**Criterion (apply per comment):** keep a comment only if it states a constraint the code cannot show. **Keep** (examples): the broker's denylist scope-boundary block (load-bearing; the mechanism doc cites it), the `O_CREAT|O_EXCL` mode rationale, the NUL-byte `--domain-name` rationale. **Drop** (examples): what-the-next-line-does narration, review-round history ("Task 6 investigated…" style chronicles — the decision stays, compressed to one line pointing at the mechanism doc; the story moves to the PR description), restatements of the docstring.

- [ ] **Step 1:** Builder edits per the criterion; zero behavior changes (`git diff` shows only comment/docstring lines).
- [ ] **Step 2:** Verify:
```bash
uv run --extra dev pytest tests/unit/test_voice_bakeoff_nonce_ledger.py tests/unit/test_voice_bakeoff_credential_broker.py tests/unit/test_voice_bakeoff_residue_audit.py tests/unit/test_sign_voice_bakeoff_approval.py tests/unit/test_request_voice_bakeoff_review.py -q
git diff --stat
```
Expected: all pass; diff touches only the five files.
- [ ] **Step 3:** Opus review (these are security-relevant files): reviewer confirms no load-bearing constraint text was lost and no code changed; any doubt → the comment stays.
- [ ] **Step 4:** Commit with a reviewed message.

---

## 7. What agents cannot do (honest boundary)

The security architecture makes the human owner load-bearing on purpose. No agent in this plan will: create accounts or enter credentials anywhere (provisioning is owner-run from runbooks; the broker only ever sees env-var digests); sign approval envelopes (personal Ed25519 key); serve as the "sole owner" in any authorization; recruit or consent human participants; push to `staging`/`main` or dispatch production deploys without explicit owner confirmation; or weaken a sealed threshold (the no-weakening rules bind everyone, agents included — scope changes happen only pre-seal via L3 + owner authorization).

## 8. Estimated shape of the whole program

Rough, for planning; the owner has accepted large orchestration where rigor demands it.

| Phase | Agent dispatches | Owner touchpoints | Dominant cost |
| --- | --- | --- | --- |
| 0 | 12–18 | merges | agent time |
| 1 | 8–12 | 1 decision + merges | decision quality |
| 2 | 6–8 | provisioning session (~1–2 h) | owner console time |
| 3 | 10–15 | 3 seal authorizations | review rigor |
| 4 | 25–40 | 1 plan authorization + merges | agent build + security review |
| 5 | 15–30 | 4–12 signature triplets (batched) | signature sittings |
| 6 | 15–25 | 2 envelopes/arm + rater program | **human raters** |
| 7 | 10–20 | participant program | **participants** (scope-dependent: from small en-only study to the drafted 482-recruit program — G2 decides) |
| 8 | 10–15 | 2–3 authorizations | review |
| 9 | 40–80 | merges + staging approvals | the big build |
| 10 | 15–25 | release authorization + PR dispositions | monitoring window (14 days) |

## 9. Deferred backlog (tracked, deliberately not scheduled)

From PR #134's independent review, cheap-but-not-urgent or blocked-on-data; revisit at Phase 4 planning where several naturally fold in:

- Credential broker's `env` injection point vs. the hardcoded `env=os.environ` call site (Phase 4 will exercise the injection point in tests anyway).
- `_build_signed_approval` synthesizing `ApprovalCaps.calls`/`artifact_ttl_ms` values the owner never authored — the approval schema should carry them explicitly (fold into Phase 4's envelope work).
- Structurally unreachable `OfflineApprovalVerifier` checks (revocation, break-glass, generation-rollback, snapshot-expiry) given the fresh in-memory trust snapshot — either exercised for real by Phase 4's persisted trust store or documented as intentionally dormant.
- Owner signing key stored unencrypted (0600, no passphrase) — present option to owner at G4 (passphrase-wrapped storage vs. accepted risk on an owner-controlled machine).
- AST file-I/O contract's fixed call-name list (`touch`/`mkdir`/`mkstemp`/`rglob`/`lstat` not independently constrained) — defense-in-depth polish; the source-digest pin is the real backstop.
- `ExecutionFirewallResolver`/`DeclaredProductionDenylist` wiring — blocked on sourcing real per-provider production identity/destination digests; owner may choose to collect these during Phase 2 provisioning (they'd come from the owner's own production consoles), which would unblock wiring it in Phase 4. Flag at G3.
- ADR numbering collision: a second "ADR 0002" (voice-pilot-shadow) exists on draft branch `codex/voice-pilot-shadow` — if that branch is ever harvested, renumber first.
- `uv.lock` untracked at repo root — commit (reproducible test installs) or gitignore; owner preference, one line either way.

## 10. Known open risks

- **Participant/rater program scale** is the single biggest schedule-and-cost unknown; it is entirely a G2 scope decision, and the master plan's pre-seal narrowing lever is the legitimate control. Nothing else in Phases 0–5 depends on that decision beyond which language cohorts the corpus manifest bakes in.
- **Provider drift:** capability probes freeze model/API versions; a provider deprecating a pinned version mid-program forces a re-freeze and probe rerun (L4 handles it, at the cost of fresh envelopes).
- **Owner gcloud access** was intermittently broken in May 2026; Phase 2's runbook starts by verifying it rather than assuming.
- **`no_winner` is a real outcome** at Phases 6 and 7 and is treated as valid evidence, not failure — the roadmap stops, and the owner decides between re-scoping arms, revisiting thresholds pre-seal of a new cycle, or a different strategy, with data in hand.
- **claude-mem search was unavailable** (timeouts) when this plan was written; recent-context injections and the 2026-08-02 handoff were used instead. If observations surface contradicting this plan's "current position" claims, re-verify against the repo before acting — the repo was the source of truth for every claim in §2.
