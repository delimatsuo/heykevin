# Receptionist Wedge / PR #165 Rebase Handoff

Created: 2026-08-17 15:38 EDT
Prepared by: Codex

## Objective

Land returning-customer memory as Slice 2 of the receptionist wedge: rebase draft
PR #165 (`codex/customer-memory`) onto current `origin/main` (which already has
live Gemini intake from PR #174), keep every memory flag default-off, and update
the PR. Do not activate capture, personalization, mutations, or recovery. Do not
deploy.

## Current State

- Repo/workspace: `/Volumes/Extreme Pro/MYPROJECTS/Kevin`
- Primary checkout: `main` at `d3cd5c7`, **5 commits behind** `origin/main`
  (`9bc275c Wire live intake so Kevin asks the job before the form (#174)`).
- Dirty state (primary): untracked public-demo handoffs only:
  - `docs/handoffs/2026-08-17-public-demo-live-ux-handoff.md`
  - `docs/handoffs/2026-08-17-public-demo-live-ux-new-session-prompt.md`
- Worktree for Slice 2: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory`
  on `codex/customer-memory` at `4f99e8bbcdae77081786798990e1f6d922158c68`, tracking
  `origin/codex/customer-memory`, clean except untracked 2026-08-12 PR #165 handoffs.
- Stale Slice 1 worktree: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/live-intake-controller`
  on `feat/live-intake-controller` at `5c74ce5` (already merged via PR #174). Leave it.
- Draft PR: https://github.com/delimatsuo/heykevin/pull/165 — OPEN, DRAFT, head
  `4f99e8b`, base `main`. GitHub currently reports `MERGEABLE`; that is stale
  versus live `main` because `gemini_pipeline.py` changed in #174.
- Rebase **has not started**. No conflict markers exist in any worktree.

## Newest User Request

`/handoff` (2026-08-17 15:38 EDT). Immediately before that, the owner said `go`
to rebase PR #165 onto current `main` with flags remaining off. The handoff
interrupts that work before the rebase began. Newest request wins for this
session (write handoff, do not continue the rebase here). The next session should
resume the authorized rebase.

## Completed Work

- Slice 1 live intake shipped in PR #174, merge commit
  `9bc275c01071e5cf255237a533a6a40130558c09`.
- Production: `kevin-api-00245-jjs`, `deploy_sha=9bc275c01071e5cf255237a533a6a40130558c09`.
- Staging: `kevin-api-staging-00136-sic`, same SHA.
- Owner live test on a **real business Kevin number** (not the Boston demo):
  Kevin asked name and what service was needed, offered Tuesday, honored Friday
  at 2pm, said Deli would confirm. Owner judged it a pass. Job-before-form is
  the Slice 1 success criterion; name still coming from the static prompt is
  expected.
- Plans on `origin/main`:
  - `docs/superpowers/plans/2026-08-17-receptionist-wedge.md`
  - `docs/superpowers/plans/2026-08-17-live-intake-controller.md`
- Public demo Cloud Run was **not** deployed with Slice 1.
  `kevin-public-demo-00031-tgd` is still `d3cd5c7369933a4a2563fd7c505b741437b1625d`.
- PR #165 remains the default-off product memory + service-request stack
  (49 files at branch creation). Flags:
  - `customer_memory_capture_enabled`
  - `customer_memory_personalization_enabled`
  - `service_request_mutations_enabled`
  - env `SERVICE_REQUEST_RECOVERY_ENABLED`
  All default false / absent-is-false, PROTECTED.

## In Progress

- Authorized but not started: rebase `codex/customer-memory` onto `origin/main`
  inside `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory`.
- No files were edited for that rebase in this session.
- This handoff pair is new and uncommitted.

## Important Decisions

- **Wedge order (2026-08-17):** live intake → memory (default-off) → owner
  Confirm UI → hang-up SMS (A2P-blocked). Jobber Request lead capture is already
  in tree behind an admin flag.
- **Slice 1 is done.** Do not iterate public-demo greeting/legal/weekday UX.
  Do not reopen intake unless a live call skips the job and jumps to the form.
- **Memory merge is source-only.** Default-off merge eligibility ≠ activation.
  Do not enable flags, provision production Firestore indexes/TTL, or run
  provider mutations as part of the rebase.
- **Conflict resolution for `gemini_pipeline.py`:** keep **both** (a) main's
  live intake controller (`_live_intake`, opening hold-speech, fail-closed
  send, greeting must not credit `service_action`) and (b) PR #165's
  `build_greeting_text` from `app/services/receptionist_context.py`. Do not
  drop intake to make memory apply cleanly. Do not wire
  `LiveIntakeController.start(caller_name=...)` in this rebase unless it is a
  trivial fail-closed read of `returning_caller_first_name`; that integration
  is a follow-on after flags stay off.
- **Updating the PR after rebase:** `git push --force-with-lease origin
  codex/customer-memory` is the expected way to update the feature branch.
  Never force-push `main` or `staging`.
- **P0 activation blockers still open** (from 2026-08-12 PR #165 handoff; not
  closed by Slice 1): bare ANI for cancel/reschedule/add-service; Google
  reschedule blind PATCH concurrency. Keep mutations off until those are
  closed.

## Files And Artifacts

- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory`: rebase
  workspace. Work here, not in the primary `main` checkout.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/docs/handoffs/2026-08-12-customer-memory-pr165-handoff.md`:
  prior exact-tree evidence, P0/P1 blockers, do-not-do list. Still valid for
  activation policy; SHA/CI evidence is for `4f99e8b` only and is invalidated
  by any rebase.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/docs/customer-memory-rollout.md`:
  flag contract, Firestore indexes/TTL, recovery rules.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/docs/superpowers/plans/2026-08-17-receptionist-wedge.md`
  (on `origin/main`): Slice 2 instructions.
- `app/services/live_intake_controller.py` (on `origin/main`): must survive the
  rebase.
- `app/services/gemini_pipeline.py`: primary conflict. Main has live intake;
  `codex/customer-memory` imports `build_greeting_text` and uses it in
  `_build_greeting_text`.
- `app/config.py`, `app/db/contractors.py`: also `changed in both` vs
  `origin/main`. Keep PROTECTED flag names from PR #165; keep any main-side
  additions.

## Commands Run And Results

```bash
git fetch origin main codex/customer-memory
git log -1 --oneline origin/main
git -C .worktrees/customer-memory status --short --branch
git -C .worktrees/customer-memory log -1 --oneline
```

Result at 15:38 EDT: `origin/main` = `9bc275c`; customer-memory worktree =
`4f99e8b` matching `origin/codex/customer-memory`; rebase not in progress.

```bash
curl -fsS https://kevin-api-752910912062.us-central1.run.app/health
curl -fsS https://kevin-api-staging-l63rergg7a-uc.a.run.app/health
curl -fsS https://kevin-public-demo-947421709125.us-east4.run.app/health
```

Result: production and staging on `9bc275c`; public demo on `d3cd5c7`.

```bash
git merge-tree $(git merge-base origin/main origin/codex/customer-memory) origin/main origin/codex/customer-memory
```

Result: `changed in both` for exactly three files: `app/config.py`,
`app/db/contractors.py`, `app/services/gemini_pipeline.py`.

## Verification

- Passed (Slice 1): owner live plumber-style call on production business
  number; GitHub Actions Test on PR #174; staging and production health SHA
  match `9bc275c`.
- Passed (PR #165 at `4f99e8b`, stale vs current main): CI Test run
  31632664524; local 2268 passed on 2026-08-12. That suite does **not** prove
  the post-rebase tree.
- Not run: rebase, post-rebase pytest, force-with-lease, merge of #165, any
  memory-flag change, demo deploy.

## Risks And Watchouts

- **Severity high — dropping live intake during conflict resolution.** Main's
  `_live_intake` wiring and the greeting-must-not-credit-`service_action` fix
  (`_credit_kevin_turns`) must remain. Tests to keep:
  `tests/unit/test_live_intake_controller.py`,
  `test_greeting_transcript_does_not_skip_service_action_question`,
  `test_public_demo_pipeline_starts_live_intake_controller`.
- **Severity high — accidental activation.** Do not set any of the four gates
  true. Literal `is True` / env false remain required.
- **Severity medium — rebase vs merge.** Owner authorized rebase. After rebase,
  updating `origin/codex/customer-memory` requires `--force-with-lease` to that
  branch only.
- **Severity medium — primary checkout is behind.** Do not rebase or commit
  Slice 2 work in `/Volumes/Extreme Pro/MYPROJECTS/Kevin` while it sits at
  `d3cd5c7`. Use the customer-memory worktree.
- **Severity medium — GitHub 503s** were common during the #174 merge/deploy.
  Retry REST; do not force-push `main` as a workaround.
- **Exact-tree review of `4f99e8b` is void** after rebase. Rerun focused
  intake + memory tests, then full `pytest -q`.

## Do Not Do

- Do not continue this handoff session's interrupted rebase here; the next
  agent starts it fresh from a clean `4f99e8b` worktree.
- Do not enable `customer_memory_*`, `service_request_mutations_enabled`, or
  `SERVICE_REQUEST_RECOVERY_ENABLED`.
- Do not merge PR #165 or deploy `kevin-api` as part of the rebase unless the
  owner explicitly asks after tests are green.
- Do not deploy or iterate `kevin-public-demo` (Boston `+1 857-810-6804`).
- Do not force-push `main` or `staging`.
- Do not `git reset --hard` or `git clean` in the primary checkout (unrelated
  untracked public-demo handoffs) or the customer-memory worktree (2026-08-12
  handoffs).
- Do not edit `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/live-intake-controller`.
- Do not reintroduce global contact fallback or a demo-specific memory model.
- Do not claim tenant-isolated call routing is fixed; `get_call_history(phone)`
  remains unscoped (P1 on PR #165).

## Next Recommended Steps

1. In `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory`,
   `git fetch origin` and `git rebase origin/main`.
2. Resolve the three `changed in both` files: keep live intake from `main` plus
   `receptionist_context.build_greeting_text` and PROTECTED memory flags from
   PR #165.
3. Run:
   ```bash
   TWILIO_ACCOUNT_SID=test TWILIO_AUTH_TOKEN=test TWILIO_PHONE_NUMBER=+15555550100 TELEGRAM_BOT_TOKEN=test USER_PHONE=+15555550101 \
     /Volumes/Extreme\ Pro/MYPROJECTS/Kevin/.venv/bin/python -m pytest tests/unit/test_live_intake_controller.py tests/unit/test_receptionist_intelligence.py tests/unit/test_customer_memory.py tests/unit/test_receptionist_customer_context.py tests/unit/test_post_call_customer_memory.py tests/unit/test_contact_tenant_voice_isolation.py -q
   ```
   then full `pytest -q`.
4. `git push --force-with-lease origin codex/customer-memory`. Leave PR #165
   draft. Do not merge or deploy until the owner says so.

## Open Questions

- None blocking the rebase. Activation (named greeting on the owner's
  contractor only) is a separate later decision after default-off source is on
  `main` and P0s are closed.
