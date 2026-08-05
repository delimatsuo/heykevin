You are continuing work on Hey Kevin PR #76, the Jobber customer-memory feature.

Current objective:
Preserve the current PR and staging evidence, then shift the next work toward a resilient AI receptionist architecture. The user does not want more ad hoc prompt rules; the user wants Kevin to rely on memory, explicit conversation state, planning, and evaluations.

Workspace:
- Repo root: `/Volumes/Extreme Pro/myprojects/Kevin`
- Feature worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory`
- Branch: `codex/jobber-customer-memory`
- PR: `https://github.com/delimatsuo/heykevin/pull/76`
- Important docs to read first:
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/AGENTS.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/handoffs/2026-07-09-jobber-memory-resilient-agent-handoff.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/handoffs/2026-07-08-jobber-customer-memory-handoff.md`

Newest user request:
The newest request was `$handoff`. Immediately before that, the user asked whether the earlier research on latest trends in agent building and memory still existed in plans. The answer is: an untracked research spec exists in `docs/superpowers/specs/`, but it is not a committed plan.

Current state:
- Latest commit: `384ac99125dd6f904140cd833afbc43066168c1b` (`384ac99 Avoid reasking known service action`).
- PR #76 is open, draft, base `main`, merge state `CLEAN`, and latest GitHub Actions `Test` check succeeded.
- Staging is deployed from the latest commit:
  - Service: `kevin-api-staging`
  - Revision: `kevin-api-staging-00060-crv`
  - URL: `https://kevin-api-staging-l63rergg7a-uc.a.run.app`
  - Health deploy SHA: `384ac99125dd6f904140cd833afbc43066168c1b`
- Worktree dirty state before this handoff: no modified tracked files, no staged files, untracked `docs/handoffs/` and untracked `docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`.
- Existing untracked handoff docs from 2026-07-08 are present in `docs/handoffs/`.
- Staging data was manually corrected:
  - staging smoke contractor `codex_phase0_smoke` now has `voice_engine=gemini`.
  - staging Jobber OAuth was reauthorized and a read-only Jobber API smoke returned HTTP 200.
- Live-call evidence:
  - A call before staging data correction used ElevenLabs and Jobber refresh failed with 401.
  - Calls after correction used Gemini Live and loaded Jobber customer memory.
  - The latest inspected transcript still had Kevin ask a repeated "repair, replacement, or new installation" style question after the caller had said replacement.
- Commit `384ac99` patched that repeated-question symptom with a prompt guardrail and regression test, but the user objected to this approach and wants a deeper architecture.

Critical constraints:
- Newest user request wins.
- Do not work in the dirty main checkout `/Volumes/Extreme Pro/myprojects/Kevin` for this feature.
- Do not revert user/other-agent changes or untracked research/handoff files.
- Do not deploy production unless the user explicitly asks.
- Do not add more ad hoc prompt rules as the next fix.
- Do not expose Jobber OAuth callback codes, admin bearer tokens, Jobber tokens, or full phone numbers.
- Be careful with gcloud defaults: current local default account/project may not be the Kevin deploy account/project. Use explicit `--account=deli@ellaexecutivesearch.com --project=kevin-491315` for Kevin GCP operations.
- Do not casually merge or force-update `staging`; the staging branch has known divergence.

Facts and evidence:
- `git status --short --branch` before this handoff showed:
  - `## codex/jobber-customer-memory...origin/codex/jobber-customer-memory`
  - `?? docs/handoffs/`
  - `?? docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`
- `curl -fsS https://kevin-api-staging-l63rergg7a-uc.a.run.app/health` returned:
  - `{"status":"ok","environment":"staging","service":"kevin-api-staging","revision":"kevin-api-staging-00060-crv","deploy_sha":"384ac99125dd6f904140cd833afbc43066168c1b"}`
- Focused prompt regression after `384ac99`: `1 passed, 1 warning`.
- Targeted Jobber/receptionist tests after `384ac99`: `56 passed, 2 warnings`.
- Ruff after `384ac99`: `All checks passed!`
- Full unit suite after `384ac99`: `302 passed, 16 warnings`.
- `gh pr view 76` showed latest `Test` check conclusion `SUCCESS`; deploy jobs were skipped for PR.

Next recommended action:
1. Tell the user that the research exists as an untracked spec, not as a committed plan: `docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`.
2. Ask whether to commit the research spec and handoff docs or leave them uncommitted.
3. Decide whether to keep, revert, or supersede commit `384ac99`, because the user has rejected prompt-rule accumulation.
4. If continuing architecture work, draft an implementation plan for `IntakeState`, `DialoguePlanner`, compact memory cards, per-turn Gemini instruction generation, and replay/eval tests.

Verification expected:
- If changing code, use TDD and run the relevant targeted tests plus `uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit -q`.
- If validating staging behavior, call the staging number ending `0993`, then inspect Cloud Run logs for Gemini Live, Jobber memory loaded, duplicate-question behavior, and latency.
- If deploying staging again, verify health shows the intended deploy SHA.

Known risks:
- The research spec and handoff docs are untracked and can be lost if someone cleans untracked files.
- `scripts/phase0_staging_smoke.py` can reset the staging smoke contractor to `voice_engine=elevenlabs`.
- The current `384ac99` prompt-rule patch is a temporary mitigation and may need to be reverted or replaced.
- Jobber OAuth and phone numbers are sensitive; redact callback codes and full numbers in user-facing text.
- Production was not touched and must stay untouched without explicit approval.

If anything conflicts, the newest user request wins. Start by running:

```bash
git status --short --branch
```
