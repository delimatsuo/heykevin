You are continuing work on Hey Kevin PR #76, the Jobber customer-memory feature, after the user approved a strategy shift toward a hybrid stateful AI receptionist architecture.

Current objective:
Preserve PR #76 as the read-only Jobber customer-memory slice, then move the next work toward memory, explicit call state, planning, tool orchestration, and evaluations. The user does not want more ad hoc prompt rules.

Workspace:
- Repo root: `/Volumes/Extreme Pro/myprojects/Kevin`
- Feature worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory`
- Branch: `codex/jobber-customer-memory`
- PR: `https://github.com/delimatsuo/heykevin/pull/76`
- Important docs to read first:
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/AGENTS.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/handoffs/2026-07-09-hybrid-stateful-receptionist-handoff.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/superpowers/specs/2026-07-09-hybrid-stateful-ai-receptionist-design.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/handoffs/2026-07-09-jobber-memory-resilient-agent-handoff.md`

Newest user request:
The newest request was `$handoff`. Immediately before that, the user agreed with the recommended hybrid stateful receptionist strategy and the design spec was committed locally.

Current state:
- Latest local commit: `8cbda76 docs: add stateful receptionist design`.
- Local branch is ahead of `origin/codex/jobber-customer-memory` by 1 commit.
- Remote PR #76 still points to `384ac99125dd6f904140cd833afbc43066168c1b` (`384ac99 Avoid reasking known service action`).
- PR #76 is open, draft, base `main`, merge state `CLEAN`; latest remote Test check succeeded and deploy jobs were skipped.
- Staging is deployed from `384ac99`, not `8cbda76`.
- Staging health returned:
  `{"status":"ok","environment":"staging","service":"kevin-api-staging","revision":"kevin-api-staging-00060-crv","deploy_sha":"384ac99125dd6f904140cd833afbc43066168c1b"}`
- Worktree dirty state before this handoff:
  `## codex/jobber-customer-memory...origin/codex/jobber-customer-memory [ahead 1]`
  `?? docs/handoffs/`
  `?? docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`
- Existing untracked handoff docs from 2026-07-08 and 2026-07-09 are present under `docs/handoffs/`.
- The approved design spec is committed at `docs/superpowers/specs/2026-07-09-hybrid-stateful-ai-receptionist-design.md`.
- The earlier research brief remains untracked at `docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`.

Critical constraints:
- Newest user request wins.
- Do not work in the dirty main checkout `/Volumes/Extreme Pro/myprojects/Kevin` for this feature.
- Do not revert user/other-agent changes or untracked research/handoff files.
- Do not deploy production unless explicitly asked.
- Do not add more ad hoc prompt rules as the next fix.
- Do not expose Jobber OAuth callback codes, admin bearer tokens, Jobber tokens, or full phone numbers.
- Be careful with GCP defaults. For Kevin GCP operations, use explicit `--account=deli@ellaexecutivesearch.com --project=kevin-491315`.
- Do not casually merge or force-update `staging`; the staging branch has known divergence.
- `scripts/phase0_staging_smoke.py` can reset the staging smoke contractor to `voice_engine=elevenlabs`.

Facts and evidence:
- `git log --oneline -3 --decorate` shows:
  `8cbda76 (HEAD -> codex/jobber-customer-memory) docs: add stateful receptionist design`
  `384ac99 (origin/codex/jobber-customer-memory) Avoid reasking known service action`
  `a4c24fb Fix job card parsing for non-text Anthropic blocks`
- `git show --stat --oneline 8cbda76` shows one docs-only file: `docs/superpowers/specs/2026-07-09-hybrid-stateful-ai-receptionist-design.md`, 448 insertions.
- `gh pr view 76` showed PR remote head `384ac99125dd6f904140cd833afbc43066168c1b`, draft open, clean merge state, Test success.
- `curl -fsS https://kevin-api-staging-l63rergg7a-uc.a.run.app/health` showed staging revision `kevin-api-staging-00060-crv` and deploy SHA `384ac99125dd6f904140cd833afbc43066168c1b`.
- Tests were not run after `8cbda76` because the commit is docs-only.

Next recommended action:
1. Ask whether to push local commit `8cbda76` to PR #76 or keep the design commit local/separate.
2. Ask whether to commit the untracked research spec and handoff docs, or leave them local.
3. If continuing implementation, create an implementation plan from `docs/superpowers/specs/2026-07-09-hybrid-stateful-ai-receptionist-design.md` for `IntakeState`, `DialoguePlanner`, `InstructionComposer`, replay/eval tests, and callback/address/duplicate-slot gating.
4. Decide whether `384ac99` stays as temporary mitigation, gets reverted, or is superseded by the first state/planner implementation.

Verification expected:
- If changing code, use TDD and run targeted tests plus:
  `uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit -q`
- If validating staging behavior, call the staging number ending `0993`, then inspect Cloud Run logs for Gemini Live, Jobber memory loaded, duplicate-question behavior, and latency.
- If deploying staging again, verify `/health` shows the intended deploy SHA.

Known risks:
- Local design commit `8cbda76` is not on the PR until pushed.
- Remote PR and staging remain on `384ac99`.
- The current `384ac99` prompt-rule patch is temporary and may need to be reverted or replaced.
- Untracked research and handoff docs can be lost if someone cleans untracked files.
- Production was not touched and must stay untouched without explicit approval.

If anything conflicts, the newest user request wins. Start by running:

```bash
git status --short --branch
```
