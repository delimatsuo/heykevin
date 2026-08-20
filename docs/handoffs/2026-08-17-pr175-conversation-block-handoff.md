# PR #175 Conversation-Resolution Block + Wedge Next Handoff

Created: 2026-08-17 19:25 EDT
Prepared by: Cursor Grok 4.6

Supersedes (for current SHAs and merge diagnosis):
`docs/handoffs/2026-08-17-pr165-rebase-and-operating-law-handoff.md`
(16:38 EDT). Keep that file for rebase/pytest evidence. Do not treat it as
current on *why* `gh pr merge 175` failed.

## Objective

Preserve in-flight Hey Kevin receptionist-wedge state so a new session can
continue without re-asking. Slice 2 (draft PR #165) is already rebased, CI
green, flags off. Standing agent operating law is on PR #175, CI green, **not
merged**. The next engineering move is: unblock and merge #175, then take the
highest-leverage unblocked wedge slice (tenant-scoped call history, not flag
activation).

## Current State

- Repo/workspace: `/Volumes/Extreme Pro/MYPROJECTS/Kevin`
- Primary checkout: `main` at `d3cd5c7`, **5 commits behind** `origin/main`.
  **Do not implement here.**
- `origin/main`: `9bc275c01071e5cf255237a533a6a40130558c09` —
  `Wire live intake so Kevin asks the job before the form (#174)`
- This 19:19–19:25 EDT session: **verify + handoff only**. No code edits, no
  merge retry, no deploy, no flag change.

### Worktrees (fresh `git worktree list` 19:19 EDT)

| Path | Branch | HEAD | Role |
| --- | --- | --- | --- |
| `/Volumes/Extreme Pro/MYPROJECTS/Kevin` | `main` | `d3cd5c7` | Stale primary. Untracked handoffs only. |
| `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory` | `codex/customer-memory` | `8713680` | Slice 2 memory; rebase **done**. Clean except untracked 2026-08-12 handoffs. Tracks `origin/codex/customer-memory`. |
| `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/agent-operating` | `docs/agent-operating` | `50d38aa` | Operating law PR #175. Clean. Tracks `origin/docs/agent-operating`. |
| `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/live-intake-controller` | `feat/live-intake-controller` | `5c74ce5` | Stale Slice 1; merged via #174. **Do not touch.** |

### Dirty state (primary checkout)

Untracked handoff pairs only (none committed):

- `docs/handoffs/2026-08-17-pr165-rebase-and-operating-law-handoff.md` (+ prompt)
- `docs/handoffs/2026-08-17-public-demo-live-ux-handoff.md` (+ prompt)
- `docs/handoffs/2026-08-17-receptionist-wedge-pr165-handoff.md` (+ prompt; **stale SHAs** — still `4f99e8b`)
- This pair (`2026-08-17-pr175-conversation-block-*`)

Customer-memory worktree also has untracked:

- `docs/handoffs/2026-08-12-customer-memory-pr165-handoff.md`
- `docs/handoffs/2026-08-12-customer-memory-pr165-new-session-prompt.md`

Those 08-12 files remain the canonical **activation P0/P1** list. Their SHA/CI
rows are stale (`4f99e8b`); post-rebase head is `8713680`.

### Pull requests (fresh `gh pr view` 19:19 EDT)

- **#165** — OPEN, **DRAFT**, MERGEABLE, mergeStateStatus **CLEAN**,
  head `87136803490f3877157da1bb4d7ff413169142ea`, base `9bc275c`.
  Test SUCCESS run `32063044339`. Flags still default-off. **Not merged.**
  https://github.com/delimatsuo/heykevin/pull/165
- **#175** — OPEN (not draft), MERGEABLE, mergeStateStatus **BLOCKED**,
  head `50d38aaae071e6712bebcc6960337b0f80030ffe`, base `9bc275c`.
  Test SUCCESS run `32064385995`. Deploy jobs skipped (PR, not push to
  staging/main). **Not merged.**
  https://github.com/delimatsuo/heykevin/pull/175

### Deployed services (fresh curl 19:19–19:25 EDT)

- Production `kevin-api-00245-jjs`:
  `deploy_sha=9bc275c01071e5cf255237a533a6a40130558c09`
  `https://kevin-api-752910912062.us-central1.run.app/health`
- Staging `kevin-api-staging-00136-sic`: same SHA
  `https://kevin-api-staging-l63rergg7a-uc.a.run.app/health`
- Public demo `kevin-public-demo-00031-tgd` (project
  `hk-public-demo-bos-260811`, **not** `kevin-491315`):
  `deploy_sha=d3cd5c7369933a4a2563fd7c505b741437b1625d`
  `https://kevin-public-demo-947421709125.us-east4.run.app/health`
  Boston try-Kevin line `+1 857-810-6804`. Do not iterate unless a live demo
  regression is reported. Do not deploy Slice 1 onto this service unless the
  owner asks.

## Newest User Request

`/handoff` (2026-08-17 19:19 EDT) — preserve state; next session continues
without re-asking. Incoming prompt already said next work is merge #175 if
owner/UI allows, then pick the highest-leverage unblocked wedge slice. Newest
request wins for **this** session (write handoff; do not merge or code).

## Completed Work

### This session (19:19 EDT)

- Re-fetched `origin/main`; SHA unchanged (`9bc275c`).
- Confirmed worktrees, PR list, CI, health endpoints.
- Diagnosed the #175 merge block (see Important Decisions). Prior 16:38
  handoff left this as “unknown / try `--admin`.” That diagnosis is wrong.

### Prior session (rebase + operating law) — still true

- PR #165 rebased onto `9bc275c` in `.worktrees/customer-memory`. Git
  auto-merged. Force-with-lease: `4f99e8b` → `8713680` on
  `origin/codex/customer-memory` only.
- Live intake from main **and** `build_greeting_text` from
  `app/services/receptionist_context.py` both present. Do not drop
  `_live_intake` from `gemini_pipeline.py`.
- Focused pytest **128 passed**; full pytest **2510 passed** with
  `PATH=".../.venv/bin:$PATH" python -m pytest -q` and **no** `TWILIO_*` at
  process start.
- Operating law on branch `docs/agent-operating`:
  `docs/agent-operating.md`, `.cursor/rules/agent-autonomy.mdc`,
  `graph-engineering.mdc`, `slice-execution.mdc`, pointers in `AGENTS.md` and
  `CLAUDE.md`.
- Review fix commit `50d38aa`: Task slug `cursor-grok-4.6-xhigh`; pytest gate
  mentions `TWILIO_*` / `FORBIDDEN_ENV_NAMES`.
- `gh pr merge 175 --merge` failed: “base branch policy prohibits the merge.”
  `--auto` failed: auto-merge not enabled on the repo.

## In Progress

- PR #175 merge is **blocked by unresolved review conversations**, not by
  failing CI and not proven to be a missing-admin-token problem.
- PR #165 remains draft. Merge eligibility ≠ activation.
- Handoff files uncommitted.

## Important Decisions

- **#175 merge block root cause (2026-08-17 19:20 EDT).** Branch protection
  on `main` has `required_conversation_resolution: true`, `enforce_admins:
  true`, `required_approving_review_count: 0`, required check `Test` (strict).
  GraphQL on PR #175 shows two Codex threads:
  1. **Live (not outdated), P2, `AGENTS.md` line 7:** make the worktree rule
     portable. Absolute `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/…`
     rejects valid clones (including GitHub `/workspace/heykevin`). Present
     the Mac path as a local example; require a non-primary Git worktree
     discovered or created relative to the current clone.
  2. **Outdated, P2, `docs/agent-operating.md` original line 144:** name the
     complete `FORBIDDEN_ENV_NAMES` set (or a clean env), not only three
     variables. Partially addressed in `50d38aa` (now says `TWILIO_*` /
     `FORBIDDEN_ENV_NAMES`); thread is still unresolved, which still blocks.
- **Do not lead with `gh pr merge --admin`.** `enforce_admins` is enabled, so
  even an admin bypass is supposed to obey the same rules. Resolving (or
  addressing) both conversations is the merge path. Owner UI merge will also
  fail until conversations are resolved unless GitHub UI offers a separate
  override the API does not.
- **GitHub permission deny already happened** for plain `gh pr merge --merge`.
  Operating law says paste the exact command and stop. Next session may retry
  **after** conversations are resolved. Do not loop `--admin` first.
- **Rebase, not merge, was used for #165.** `--force-with-lease` on
  `codex/customer-memory` only. Never force-push `main` or `staging`.
- **Memory flags stay off.** P0 activation blockers from 2026-08-12 still
  apply: bare ANI auth for cancel/reschedule/add-service; Google reschedule
  blind PATCH concurrency.
- **Merge to `main` does not deploy production.** `deploy.yml`: push to `main`
  runs nothing; production is `workflow_dispatch` from `main` only.
- **Kevin operating law mirrors ATS method, not ATS product gates.** Kopadze
  graph engineering (not LangGraph). Owner-gates include **staging and
  production** Cloud Run, flags, App Store/TestFlight, Twilio/A2P, credentials,
  real caller PII, opening `firestore.rules`.
- **Do not use the primary checkout** while local `main` is behind
  `origin/main`.
- **Lead with a recommendation**, never a neutral option menu
  (`feedback_decisions_need_recommendations.md`).
- **The app is live** in the App Store. Do not frame work as pre-launch.

## Files And Artifacts

- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/agent-operating`: PR #175
  workspace. Edit operating-law files **here**.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/agent-operating/docs/agent-operating.md`:
  standing law. Exists only on this branch until merged.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/agent-operating/AGENTS.md`:
  Codex live P2 is on the worktree pointer at line 7.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory`: Slice 2
  tree at `8713680`.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/docs/customer-memory-rollout.md`:
  flag contract, Firestore indexes/TTL, qualification gates.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/docs/handoffs/2026-08-12-customer-memory-pr165-handoff.md`:
  P0/P1 activation blockers and the tenant-scoped `get_call_history` follow-up.
- Wedge plan (on `origin/main`, **not** in stale primary checkout):
  `git show origin/main:docs/superpowers/plans/2026-08-17-receptionist-wedge.md`
- Owner memory:
  `/Users/delimatsuo/.claude/projects/-Volumes-Extreme-Pro-myprojects-Kevin/memory/MEMORY.md`

## Commands Run And Results

```bash
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" worktree list
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" fetch origin main
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" log -1 --oneline origin/main
gh pr list --repo delimatsuo/heykevin --state open
```

Result: four worktrees as tabled; `origin/main` = `9bc275c`; open PRs 175 and
165 (draft).

```bash
gh pr view 175 --repo delimatsuo/heykevin --json mergeable,mergeStateStatus,headRefOid,statusCheckRollup
gh pr checks 175 --repo delimatsuo/heykevin
gh pr view 165 --repo delimatsuo/heykevin --json isDraft,mergeable,mergeStateStatus,headRefOid
```

Result: #175 MERGEABLE + BLOCKED, head `50d38aa`, Test pass; #165 draft CLEAN
head `8713680`, Test pass.

```bash
gh api repos/delimatsuo/heykevin/branches/main/protection
```

Result (abridged): required check `Test` strict; 0 approving reviews;
`enforce_admins.enabled=true`; `required_conversation_resolution.enabled=true`.
Rulesets: `[]`. `gh auth`: account `delimatsuo`, scopes include `repo`.

```bash
gh api graphql -f query='... pullRequest(number:175) { reviewThreads { ... } }'
```

Result: two unresolved threads (one live P2 on `AGENTS.md:7`, one outdated P2
on `docs/agent-operating.md`).

```bash
curl -fsS https://kevin-api-752910912062.us-central1.run.app/health
curl -fsS https://kevin-api-staging-l63rergg7a-uc.a.run.app/health
curl -fsS https://kevin-public-demo-947421709125.us-east4.run.app/health
```

Result: prod/staging `deploy_sha=9bc275c…`; demo `d3cd5c7…` / revision
`kevin-public-demo-00031-tgd`.

## Verification

- Passed this session: fetch; worktree list; PR JSON; CI checks; branch
  protection; review-thread GraphQL; prod/staging/demo health.
- Passed prior session (not re-run here): #165 rebase; focused 128 tests;
  full 2510 tests; coexistence of live intake + greeting in
  `gemini_pipeline.py`.
- Failed (prior session, not retried here): `gh pr merge 175 --merge`;
  `gh pr merge 175 --auto`.
- Not run this session: any merge; `--admin`; addressing Codex comments;
  pytest; deploy; flag activation; owner live-test of rebased #165.

## Risks And Watchouts

- **High — accidental flag activation on #165.** Do not enable
  `customer_memory_capture_enabled`, `customer_memory_personalization_enabled`,
  `service_request_mutations_enabled`, or `SERVICE_REQUEST_RECOVERY_ENABLED`.
- **High — dropping live intake** in future `gemini_pipeline.py` edits.
- **High — treating `--admin` as the #175 fix.** Conversations are the block;
  `enforce_admins` means bypass is the wrong tool.
- **Medium — working in primary checkout at `d3cd5c7`.** Files such as
  `docs/superpowers/plans/2026-08-17-receptionist-wedge.md` are **missing**
  there; read them via `git show origin/main:…` or a worktree on `9bc275c`.
- **Medium — two agents on one worktree.** One git-mutation owner per tree.
- **Low — stale 15:38 receptionist-wedge handoff** still says rebase has not
  started / head `4f99e8b`. Ignore its SHA/CI rows.
- **Low — 08-12 full pytest used exported `TWILIO_*`.** That pattern now fails
  visual-diagnosis collection. Use PATH-only pytest.

## Do Not Do

- Do not implement in `/Volumes/Extreme Pro/MYPROJECTS/Kevin` (primary).
- Do not merge PR #165 or mark it ready unless the owner explicitly asks.
- Do not enable memory/mutation/recovery/Jobber/public-demo flags.
- Do not deploy `kevin-api`, staging, or `kevin-public-demo` unless authorized.
- Do not force-push `main` or `staging`.
- Do not `git add -A`, `--no-verify`, or change git config.
- Do not `git reset --hard` / `git clean` in trees with untracked handoffs.
- Do not edit `.worktrees/live-intake-controller`.
- Do not iterate public-demo UX unless a live demo regression is reported.
- Do not export `TWILIO_*`, `TELEGRAM_BOT_TOKEN`, `USER_PHONE`, or other
  `FORBIDDEN_ENV_NAMES` at pytest process start.
- Do not reopen Slice 1 intake unless a live call skips the job and jumps to
  the form.
- Do not reintroduce global contact fallback.
- Do not claim #175 is merged.
- Do not present Deli a neutral “which slice?” menu.

## Mistakes Do Not Repeat

- **Misdiagnosed #175 block as “CLI/admin permission.”** The 16:38 handoff
  recommended `--admin` / UI merge without listing review threads. Fresh
  GraphQL shows unresolved Codex conversations + `required_conversation_resolution`.
- **Primary checkout as source of files.** `docs/superpowers/plans/2026-08-17-receptionist-wedge.md`
  is not on local `main` (`d3cd5c7`). Fetch and use `origin/main` or a worktree.
- **Neutral option menus.** Owner: always recommend, then a single “go?”.
- **Calling `/health` “verified.”** Health is liveness. Production deploy still
  needs `docs/voice-enterprise-release-gates.md`; `smoke_release.sh` is not
  a call test. Do not recommend prod deploy from this handoff.
- **pytest env snapshot.** Do not copy the 08-12 command that exported
  `TWILIO_*` at process start.
- **Worktree `??` is untracked, not junk.** Do not `git clean` handoffs.
- **Invalid Task slug `grok-4.6-xhigh`.** Correct slug:
  `cursor-grok-4.6-xhigh` (already fixed in `50d38aa`).

## Next Recommended Steps

**Recommendation:** treat #175 as a **loop** (one docs PR, fake-edge finds no
independent jobs). Do not fan out a graph. Do not ask Deli which slice.

1. Work in `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/agent-operating`.
   `git fetch origin` and confirm still `50d38aa` / base `9bc275c`.
2. Address the **live** Codex P2: portable worktree rule in `AGENTS.md` line 7
   and the matching absolute-path language in `docs/agent-operating.md` and
   `.cursor/rules/agent-autonomy.mdc`. Keep Deli’s Mac path as the local
   example. Require: never trust the primary checkout; use `git worktree`
   under `.worktrees/` **relative to the current clone**.
3. Optionally tighten the outdated env-names comment: point at
   `FORBIDDEN_ENV_NAMES` in `tests/unit/test_visual_diagnosis_*.py` (or quote
   that agents must run pytest without extra credential exports). `50d38aa`
   already names the set.
4. Push to `origin/docs/agent-operating`. Wait for Test. Resolve **both**
   review threads (the outdated one can be resolved after the later commit
   supersedes line 144). Then:

   ```bash
   gh pr merge 175 --repo delimatsuo/heykevin --merge
   ```

   If that still fails, paste the **exact** command + error and stop. Do not
   immediately `--admin`.
5. After #175 is on `origin/main`, `git fetch origin main`. Then the highest-
   leverage **unblocked** product slice: tenant-scoped `get_call_history`
   (P1 from the 08-12 handoff). This bug is on **main already**
   (`app/webhooks/twilio_incoming.py` → `app/db/calls.py` filters by caller
   phone only). Fake-edge: it does **not** need #165 merged. Cut a **new**
   worktree from `origin/main`; do not pile it onto draft #165 unless that is
   clearly smaller. Inventory every `get_call_history(` call, require
   `contractor_id`, add negative tests that tenant A outcomes cannot affect
   tenant B, independent review, then PR + `gh pr merge --merge`.
6. Leave #165 draft. Do not enable flags. Slice 3 Confirm UI still needs its
   own plan written (wedge says write that plan after Slice 1, which is on
   main); that is lower leverage than the live tenant-routing hole.
7. Commit this handoff pair only if Deli asks.

## Open Questions

- None that block step 1–4. Portable-worktree wording is an engineering
  choice the next agent should make and merge; do not bounce it to Deli.
- Owner input is required for: merging #165, any deploy, any flag, A2P,
  opening `firestore.rules`, or `--admin` if conversation-resolution merge
  still fails after threads are resolved.
