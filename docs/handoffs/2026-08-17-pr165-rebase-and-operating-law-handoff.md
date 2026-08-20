# PR #165 Rebase + Agent Operating Law Handoff

Created: 2026-08-17 16:38 EDT
Prepared by: Codex

## Objective

Finish Slice 2 receptionist-wedge prep: rebase draft PR #165 onto current
`origin/main` with all memory flags default-off, then install Hey Kevin's
standing agent operating law (adapted from the Ella ATS playbook) so future
sessions work from worktrees with Kopadze graph engineering and autonomous
reviewed-clean merges.

## Current State

- Repo/workspace: `/Volumes/Extreme Pro/MYPROJECTS/Kevin`
- Primary checkout: `main` at `d3cd5c7`, **5 commits behind**
  `origin/main` (`9bc275c`). Do not implement here.
- `origin/main`: `9bc275c01071e5cf255237a533a6a40130558c09` — PR #174 live
  intake merged.
- Production: `kevin-api-00245-jjs`, `deploy_sha=9bc275c`.
- Staging: `kevin-api-staging-00136-sic`, `deploy_sha=9bc275c`.
- Public demo: still on older SHA (`d3cd5c7` per prior handoff); not deployed
  with Slice 1.

### Worktrees

| Path | Branch | HEAD | Role |
| --- | --- | --- | --- |
| `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory` | `codex/customer-memory` | `8713680` | Slice 2 memory; rebase **done** |
| `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/agent-operating` | `docs/agent-operating` | `50d38aa` | Operating law PR #175 |
| `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/live-intake-controller` | `feat/live-intake-controller` | `5c74ce5` | Stale; merged via #174 — leave alone |

### Dirty state (primary checkout)

Untracked handoffs only:

- `docs/handoffs/2026-08-17-public-demo-live-ux-handoff.md`
- `docs/handoffs/2026-08-17-public-demo-live-ux-new-session-prompt.md`
- `docs/handoffs/2026-08-17-receptionist-wedge-pr165-handoff.md` (pre-rebase;
  stale SHA evidence)
- `docs/handoffs/2026-08-17-receptionist-wedge-pr165-new-session-prompt.md`
- This handoff pair (new, uncommitted)

### Pull requests

- **#165** — OPEN, DRAFT, MERGEABLE, merge state CLEAN, head `8713680`.
  CI Test passed (run 32063044339). Rebased onto `9bc275c`. Flags still
  default-off. **Not merged.**
  https://github.com/delimatsuo/heykevin/pull/165
- **#175** — OPEN (not draft), MERGEABLE, merge state **BLOCKED**, head
  `50d38aa`. CI Test passed (run 32064385995). **`gh pr merge --merge`
  failed:** “base branch policy prohibits the merge.” `--auto` also failed
  (auto-merge not enabled on repo). **Not merged.**
  https://github.com/delimatsuo/heykevin/pull/175

## Newest User Request

`/handoff` (2026-08-17 16:38 EDT). Immediately before that, the owner pasted
Ella ATS operating instructions and asked whether Kevin already had that
information; the session replicated them into Kevin-specific docs and opened
PR #175. Before that, PR #165 rebase was authorized and completed. Newest
request wins for this session (write handoff; do not continue merge attempts
here).

## Completed Work

### PR #165 rebase (Slice 2)

- Rebased `codex/customer-memory` onto `origin/main` in
  `.worktrees/customer-memory`. Git auto-merged; no manual conflict markers.
- New head: `87136803490f3877157da1bb4d7ff413169142ea` (`8713680 Block
  cross-tenant contact fallback` on top of `d79ab0d`).
- Force-with-lease pushed: `4f99e8b` → `8713680` on
  `origin/codex/customer-memory` only.
- Verified both sides present in tree:
  - Live intake from main: `_live_intake`, hold-speech opening, fail-closed
    send, `_credit_kevin_turns` in `LiveIntakeController`.
  - Memory from #165: `build_greeting_text` from
    `app/services/receptionist_context.py`, PROTECTED flags default false,
    `service_request_recovery_enabled: bool = False`.
- Focused pytest: **128 passed**.
- Full pytest: **2510 passed** (venv on PATH; no forbidden env vars at
  process start).
- GitHub CI on `8713680`: Test **SUCCESS**.

### Agent operating law (from ATS playbook)

- Created worktree `.worktrees/agent-operating` from `origin/main`.
- Added:
  - `docs/agent-operating.md` — full Kevin-adapted operating law
  - `.cursor/rules/agent-autonomy.mdc`
  - `.cursor/rules/graph-engineering.mdc`
  - `.cursor/rules/slice-execution.mdc`
  - Pointers in `AGENTS.md` and `CLAUDE.md`
- Fresh-context review caught two Important issues; fixed in `50d38aa`:
  invalid Task slug `grok-4.6-xhigh` → `cursor-grok-4.6-xhigh`; pytest env
  gate now says `TWILIO_*` / `FORBIDDEN_ENV_NAMES`.
- Opened PR #175; CI Test **SUCCESS**. Merge attempted and blocked (see
  above).

## In Progress

- PR #175 merge blocked by GitHub branch policy / CLI merge permission. Needs
  owner merge in GitHub UI or `--admin` if Deli has admin rights, or
  investigation of org/repo merge restrictions.
- PR #165 remains draft; merge eligibility is a separate owner decision after
  green CI (already green on rebased head).
- This handoff pair is new and **uncommitted**.

## Important Decisions

- **Rebase, not merge, for #165.** Owner authorized rebase; branch updated
  with `--force-with-lease` only.
- **Memory flags stay off.** Rebase and merge-readiness ≠ activation. P0
  blockers (bare ANI auth, Google reschedule concurrency) still apply.
- **Kevin operating law mirrors ATS method, not ATS product gates.** Kopadze
  graph engineering, worktrees under `.worktrees/`, autonomous push/PR/merge
  when reviewed-clean — but Kevin owner-gates include **staging and production
  Cloud Run deploys**, feature flags, App Store/TestFlight, Twilio/A2P, and
  opening `firestore.rules` (deny-all by design).
- **Merge to `main` does not deploy** production (`deploy.yml` is manual for
  production; push to `main` runs nothing). Documented in operating law.
- **Do not use primary checkout** when local `main` is behind `origin/main`.

## Files And Artifacts

- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory`: Slice 2
  rebase workspace; clean except untracked 2026-08-12 handoffs.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/agent-operating`: PR #175
  branch; clean.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/docs/agent-operating.md`: exists only
  on PR #175 branch until merged.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/docs/handoffs/2026-08-17-receptionist-wedge-pr165-handoff.md`:
  pre-rebase state; useful for constraints, stale for SHAs/CI.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/docs/superpowers/plans/2026-08-17-receptionist-wedge.md`
  (on `origin/main`): wedge sequencing.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/docs/customer-memory-rollout.md`:
  flag contract and activation gates.

## Commands Run And Results

```bash
# PR #165 rebase
git -C .worktrees/customer-memory fetch origin && git rebase origin/main
git push --force-with-lease origin codex/customer-memory

# PR #165 tests (focused)
TWILIO_ACCOUNT_SID=test ... pytest tests/unit/test_live_intake_controller.py \
  tests/unit/test_receptionist_intelligence.py tests/unit/test_customer_memory.py \
  tests/unit/test_receptionist_customer_context.py tests/unit/test_post_call_customer_memory.py \
  tests/unit/test_contact_tenant_voice_isolation.py -q
# 128 passed

# PR #165 tests (full — no forbidden env at process start, venv on PATH)
PATH=".../.venv/bin:$PATH" python -m pytest -q
# 2510 passed

# PR #175
git worktree add -b docs/agent-operating .worktrees/agent-operating origin/main
git push -u origin HEAD && gh pr create --repo delimatsuo/heykevin ...
gh pr checks 175 --watch  # Test pass
gh pr merge 175 --merge   # FAILED: base branch policy prohibits the merge
gh pr merge 175 --merge --auto  # FAILED: auto merge not allowed for repo
```

Health at handoff:

```bash
curl -fsS https://kevin-api-752910912062.us-central1.run.app/health
curl -fsS https://kevin-api-staging-l63rergg7a-uc.a.run.app/health
```

Both report `deploy_sha=9bc275c01071e5cf255237a533a6a40130558c09`.

## Verification

- Passed: PR #165 rebase; focused 128 tests; full 2510 tests; PR #165 CI;
  PR #175 CI; production/staging health SHA; live intake + greeting coexist
  in rebased `gemini_pipeline.py`.
- Failed: `gh pr merge 175 --merge` (policy block); `gh pr merge 175 --auto`
  (auto-merge disabled).
- Not run: merge of #165 or #175; any deploy; any flag activation; owner
  live-test of rebased #165 tree.

## Risks And Watchouts

- **High — accidental flag activation on #165.** Do not enable
  `customer_memory_*`, `service_request_mutations_enabled`, or
  `SERVICE_REQUEST_RECOVERY_ENABLED`.
- **High — dropping live intake during future edits to `gemini_pipeline.py`.**
- **Medium — working in primary checkout at `d3cd5c7`.** Always use worktrees.
- **Medium — PR #175 merge block unknown root cause.** Test green, 0 required
  reviews, strict base true but branch is current on `9bc275c`. May need UI
  merge or admin token.
- **Low — stale handoff `2026-08-17-receptionist-wedge-pr165-handoff.md`.**
  Prefer this document for post-rebase state.

## Do Not Do

- Do not merge PR #165 or enable memory flags without explicit owner ask.
- Do not deploy `kevin-api`, staging, or public demo unless owner authorizes.
- Do not force-push `main` or `staging`.
- Do not `git reset --hard` or `git clean` in primary checkout or worktrees
  with untracked handoffs.
- Do not edit `.worktrees/live-intake-controller`.
- Do not iterate public-demo UX unless a live demo regression is reported.
- Do not claim PR #175 is merged; it is open and blocked.

## Next Recommended Steps

1. **Merge PR #175** (owner or admin): try GitHub UI “Merge pull request” on
   https://github.com/delimatsuo/heykevin/pull/175 . If CLI retry:
   `gh pr merge 175 --repo delimatsuo/heykevin --merge --admin` (only if Deli
   has admin). Operating law then lands on `main` without deploy.
2. **PR #165:** leave draft unless owner asks to mark ready/merge. If merging
   later: still default-off; no flag activation; no deploy as part of merge.
3. **Next engineering slice:** fake-edge test on wedge plan — e.g. tenant-scoped
   `get_call_history` (P1 on #165) or Slice 3 Confirm UI — in a fresh worktree
   from `origin/main` (after #175 merges if you want operating law on disk in
   primary checkout).
4. Optionally commit this handoff pair if Deli wants them in git.

## Open Questions

- What exactly blocks `gh pr merge` on #175? Owner UI merge may succeed where
  the agent token cannot. Only Deli can confirm org-level merge restrictions.
