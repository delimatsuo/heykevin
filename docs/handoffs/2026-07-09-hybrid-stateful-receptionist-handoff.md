# Hybrid Stateful Receptionist Handoff

Created: 2026-07-09 11:58 EDT
Prepared by: Codex

## Objective

Continue Hey Kevin PR #76, the Jobber customer-memory feature, while preserving the user's agreed strategy shift: PR #76 should remain a narrow read-only Jobber memory slice, and the next implementation work should move Kevin toward a resilient AI receptionist architecture based on explicit memory, call state, planning, tool orchestration, and evaluations rather than more ad hoc prompt rules.

## Current State

- Repo/workspace: `/Volumes/Extreme Pro/myprojects/Kevin`
- Active worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory`
- Branch: `codex/jobber-customer-memory`
- Latest local commit: `8cbda76 docs: add stateful receptionist design`
- Remote PR head: `384ac99125dd6f904140cd833afbc43066168c1b`
- Local branch state: ahead of `origin/codex/jobber-customer-memory` by 1 commit.
- PR: `https://github.com/delimatsuo/heykevin/pull/76`
- PR state from `gh pr view`: open, draft, base `main`, merge state `CLEAN`, remote head `384ac99125dd6f904140cd833afbc43066168c1b`.
- Latest GitHub Actions check on remote PR head: `Test` succeeded; `Deploy to Staging` and `Deploy to Production` skipped for PR.
- Staging service: `kevin-api-staging`
- Staging URL: `https://kevin-api-staging-l63rergg7a-uc.a.run.app`
- Staging revision: `kevin-api-staging-00060-crv`
- Staging deploy SHA: `384ac99125dd6f904140cd833afbc43066168c1b`
- Dirty state before writing this handoff:
  - no modified tracked files
  - no staged files
  - untracked `docs/handoffs/`
  - untracked `docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`
- Related checkout warning: `/Volumes/Extreme Pro/myprojects/Kevin` is a dirty, stale main checkout. Do not work there for this feature.

## Newest User Request

The newest user request is `$handoff`. Immediately before that, the user agreed with the recommended strategy to turn the research into a concrete hybrid stateful receptionist design. The approved design spec was written and committed locally as `8cbda76`.

Newest user request wins if anything conflicts with earlier plans.

## Completed Work

- PR #76 already implemented read-only Jobber customer memory for Gemini Live:
  - `app/services/jobber.py`: Jobber phone lookup, returned-phone match guard, memory normalization, compact prompt formatting.
  - `app/services/gemini_pipeline.py`: starts Jobber memory lookup before Gemini setup, uses a `0.9s` latency budget, injects compact memory context, and lets greeting use a known caller first name without mentioning Jobber.
  - `tests/unit/test_jobber.py`: Jobber memory, matching, formatting, conflict ordering, and API error tests.
  - `tests/unit/test_receptionist_intelligence.py`: receptionist prompt and Gemini setup/greeting tests.
- Commit `d185edd Guard Jobber memory matching` tightened privacy correctness by requiring returned Jobber candidate phone numbers to match the caller before injecting memory.
- Commit `a4c24fb Fix job card parsing for non-text Anthropic blocks` fixed post-call extraction parsing when Anthropic returns non-text blocks before text blocks.
- Commit `384ac99 Avoid reasking known service action` added a narrow prompt guardrail and regression test after a staging call where Kevin asked whether a toilet request was repair/replacement/new installation after the caller had already said replacement. The user later rejected prompt-rule accumulation as the long-term approach.
- The user asked whether the earlier research still existed. Answer: yes, as an untracked spec at `docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`; it was not a committed plan.
- Independent research was run against primary/current sources including OpenAI voice-agent docs, OpenAI Realtime docs, OpenAI Agents SDK docs, Gemini Live docs, Gemini Interactions API docs, Twilio Media Streams, Vapi, Retell, Bland, LangGraph, and agent-memory/evaluation research papers.
- The user agreed with the recommendation: keep Gemini Live as the realtime voice layer but move receptionist policy into Hey Kevin-owned `IntakeState`, `DialoguePlanner`, `InstructionComposer`, `MemoryStore`, `ToolOrchestrator`, and evals.
- New committed design spec:
  - Path: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/superpowers/specs/2026-07-09-hybrid-stateful-ai-receptionist-design.md`
  - Commit: `8cbda76 docs: add stateful receptionist design`
  - Scope: architecture design only; no runtime code changes.

## In Progress

- Handoff creation is in progress in `docs/handoffs/`.
- The new design spec commit `8cbda76` is local only and has not been pushed to `origin/codex/jobber-customer-memory`.
- The earlier research spec remains untracked:
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`
- Existing older handoff docs remain untracked in `docs/handoffs/`.
- No implementation plan has been written yet for the hybrid stateful receptionist design.
- No code implementation has started for `IntakeState`, `DialoguePlanner`, `InstructionComposer`, local customer memory, or eval harness.

## Important Decisions

- 2026-07-08: Keep PR #76 narrow and read-only.
  - Rationale: avoid mixing customer memory with scheduling/write behavior before proving live-call behavior.
  - Consequence: PR #76 should not create Jobber requests, book appointments, or write CRM data during the call.
- 2026-07-08: Validate Jobber phone matches before memory injection.
  - Rationale: wrong customer memory in a live call would be a privacy and product correctness failure.
  - Consequence: `lookup_customer_memory` returns no memory when returned Jobber candidates do not match the caller phone.
- 2026-07-09: `384ac99` is a temporary symptom mitigation, not the architecture.
  - Rationale: the user explicitly does not want dozens of prompt exceptions.
  - Consequence: do not add more ad hoc prompt rules as the next fix.
- 2026-07-09: Adopt the hybrid stateful receptionist strategy.
  - Rationale: research and staging evidence both support voice + state + memory + planner + tools + evals.
  - Consequence: next implementation work should start with state and eval scaffolding, not prompt expansion.
- 2026-07-09: Keep Gemini Live as the current live voice layer, but make provider details an adapter boundary.
  - Rationale: Gemini Live provides low-latency voice behavior, but Gemini Live is still described as Preview while Gemini Interactions API is GA for new non-realtime Gemini model/agent work.
  - Consequence: avoid entrenching business logic inside Gemini-specific prompts or sessions.

## Files And Artifacts

- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/AGENTS.md`: project guide, branch/deploy rules, production/staging warnings.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/superpowers/specs/2026-07-09-hybrid-stateful-ai-receptionist-design.md`: committed approved design spec for the next architecture phase.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`: untracked research brief used as input for the approved design.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/handoffs/2026-07-08-jobber-customer-memory-handoff.md`: older untracked handoff from the initial Jobber memory work.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/handoffs/2026-07-09-jobber-memory-resilient-agent-handoff.md`: older untracked handoff after staging validation and architecture concern.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/app/services/jobber.py`: Jobber read/write service and current customer memory lookup.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/app/services/gemini_pipeline.py`: Gemini Live customer memory injection and live call path.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/app/services/voice_pipeline.py`: shared prompt builder; contains temporary prompt guardrail from `384ac99`.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/tests/unit/test_jobber.py`: Jobber customer memory tests.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/tests/unit/test_receptionist_intelligence.py`: current prompt/Gemini/receptionist regression tests.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/scripts/phase0_staging_smoke.py`: warning from earlier handoff: script can reset staging smoke contractor `voice_engine` to `elevenlabs`.

## Commands Run And Results

```bash
git status --short --branch
```

Result before writing this handoff:

```text
## codex/jobber-customer-memory...origin/codex/jobber-customer-memory [ahead 1]
?? docs/handoffs/
?? docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md
```

```bash
git log --oneline -8 --decorate
```

Result:

```text
8cbda76 (HEAD -> codex/jobber-customer-memory) docs: add stateful receptionist design
384ac99 (origin/codex/jobber-customer-memory) Avoid reasking known service action
a4c24fb Fix job card parsing for non-text Anthropic blocks
d185edd Guard Jobber memory matching
3c1087e Add Jobber customer memory context
508190a (origin/main, origin/HEAD) Tighten Gemini live latency config
b900394 Fix Gemini voice latency pacing (#73)
7b8082e Harden voice lead capture flow (#72)
```

```bash
git show --stat --oneline --decorate 8cbda76
```

Result:

```text
8cbda76 (HEAD -> codex/jobber-customer-memory) docs: add stateful receptionist design
 ...07-09-hybrid-stateful-ai-receptionist-design.md | 448 +++++++++++++++++++++
 1 file changed, 448 insertions(+)
```

```bash
git show --stat --oneline --decorate 384ac99
```

Result:

```text
384ac99 (origin/codex/jobber-customer-memory) Avoid reasking known service action
 app/services/voice_pipeline.py               | 1 +
 tests/unit/test_receptionist_intelligence.py | 7 +++++++
 2 files changed, 8 insertions(+)
```

```bash
gh pr view 76 --repo delimatsuo/heykevin --json number,title,state,isDraft,mergeStateStatus,headRefName,baseRefName,headRefOid,statusCheckRollup,updatedAt,url
```

Result summary:

```text
PR #76 is OPEN, draft=true, base=main, head=codex/jobber-customer-memory,
remote head SHA=384ac99125dd6f904140cd833afbc43066168c1b,
mergeStateStatus=CLEAN.
Test check completed SUCCESS at 2026-07-09T14:56:28Z.
Deploy to Staging and Deploy to Production were SKIPPED.
```

```bash
curl -fsS https://kevin-api-staging-l63rergg7a-uc.a.run.app/health
```

Result:

```json
{"status":"ok","environment":"staging","service":"kevin-api-staging","revision":"kevin-api-staging-00060-crv","deploy_sha":"384ac99125dd6f904140cd833afbc43066168c1b"}
```

```bash
git status --short --branch
```

Result for the main checkout at `/Volumes/Extreme Pro/myprojects/Kevin`:

```text
## main...origin/main [ahead 1, behind 88]
 M AGENTS.md
 M ios/Kevin.xcodeproj/project.pbxproj
 M ios/Kevin/App/AppDelegate.swift
 M ios/Kevin/App/KevinApp.swift
 M ios/Kevin/Models/AppState.swift
 M ios/Kevin/Services/APIClient.swift
 M ios/Kevin/Services/ContactSyncManager.swift
 M ios/Kevin/Services/KeychainManager.swift
 M ios/Kevin/Views/OnboardingView.swift
 M ios/Kevin/Views/SettingsView.swift
 M ios/project.yml
?? .playwright-mcp/
?? docs/superpowers/plans/2026-05-18-admin-dashboard.md
?? docs/superpowers/plans/2026-06-30-v2-phase-0-safety-audit.md
?? docs/superpowers/specs/2026-06-30-business-first-dispatch-v2-design.md
?? jobber-login.png
?? live-screen-1.png
?? live-screen-2.png
?? recents.png
?? scripts/jobber_oauth_probe.py
?? uv.lock
```

## Verification

- Passed earlier after `384ac99`: focused prompt regression test, targeted Jobber/receptionist tests, ruff, and full unit suite. Evidence from earlier handoff:
  - `uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_intelligence.py::test_business_prompt_does_not_reask_known_service_action -q` -> `1 passed, 1 warning`.
  - `uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_jobber.py tests/unit/test_receptionist_intelligence.py -q` -> `56 passed, 2 warnings`.
  - `uv run --python 3.12 --with '.[dev]' ruff check ...` -> `All checks passed!`.
  - `uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit -q` -> `302 passed, 16 warnings`.
- Passed freshly for this handoff: staging health check returned service `kevin-api-staging`, revision `kevin-api-staging-00060-crv`, deploy SHA `384ac99125dd6f904140cd833afbc43066168c1b`.
- Passed freshly for this handoff: PR metadata check confirmed remote PR is still draft/open, clean merge state, and Test succeeded for remote head `384ac99`.
- Passed for design-doc self-review before commit: placeholder scan on `docs/superpowers/specs/2026-07-09-hybrid-stateful-ai-receptionist-design.md` found no `TBD`, `TODO`, `FIXME`, `implement later`, `fill in`, `placeholder`, or `??`.
- Not run after `8cbda76`: tests, because `8cbda76` is docs-only.
- Not run after `8cbda76`: GitHub Actions, because `8cbda76` has not been pushed.
- Not run after `384ac99`: another live staging call against revision `kevin-api-staging-00060-crv`.
- Not run: production deploy.

## Risks And Watchouts

- High: Do not work in `/Volumes/Extreme Pro/myprojects/Kevin` for this feature. The main checkout is stale and dirty with unrelated iOS/backend artifacts.
- High: Do not deploy production unless the user explicitly asks.
- High: Do not add more ad hoc prompt rules as the next fix. The user explicitly wants memory, state, planning, and evals.
- High: The local branch is ahead of the PR by one docs commit. If the design spec should appear in PR #76, push `codex/jobber-customer-memory`; if not, leave it local or move it to a separate branch.
- High: Remote PR #76 and staging still point to `384ac99`, not local `8cbda76`.
- Medium: `384ac99` conflicts with the desired long-term architecture. It may stay as temporary mitigation, be reverted, or be superseded by a planner/eval implementation.
- Medium: The earlier research spec and all handoff docs are untracked. They can be lost if someone cleans untracked files.
- Medium: `scripts/phase0_staging_smoke.py` can reset staging smoke contractor `voice_engine` to `elevenlabs`, which would invalidate Gemini Live staging validation.
- Medium: Current local `gcloud` defaults may not be the Kevin deploy account/project. For Kevin GCP operations, use explicit `--account=deli@ellaexecutivesearch.com --project=kevin-491315`.
- Medium: PR #76 was deployed directly to Cloud Run staging from the feature worktree; do not casually merge or force-update the divergent `staging` branch.
- Medium: Jobber OAuth callback codes, Jobber tokens, admin bearer tokens, and full phone numbers are sensitive. Redact them in user-facing text, fixtures, docs, and logs.

## Do Not Do

- Do not deploy production.
- Do not use the dirty main checkout for this feature.
- Do not revert unrelated main checkout changes or untracked artifacts.
- Do not add another natural-language prompt exception as the next response to a receptionist failure.
- Do not merge PR #76 or mark it ready until the user decides whether `384ac99` should stay, be reverted, or be superseded.
- Do not broaden PR #76 into scheduling, booking, or Jobber writes without explicit user approval.
- Do not expose callback URLs with Jobber `code`, full phone numbers, bearer tokens, or OAuth tokens.
- Do not run staging smoke scripts without checking whether they reset `voice_engine`.

## Next Recommended Steps

1. Decide whether to push local commit `8cbda76` to `origin/codex/jobber-customer-memory` so the approved design spec becomes part of PR #76.
2. Decide whether the untracked research spec should be committed, left uncommitted, or folded into the committed design spec.
3. Decide whether `384ac99` should remain as temporary mitigation on PR #76, be reverted before merge, or be superseded by the first planner/eval slice.
4. If continuing implementation, write an implementation plan from the committed design spec for:
   - serializable `IntakeState`
   - `DialoguePlanner`
   - `InstructionComposer`
   - replay/eval tests for the known repeated-question transcript
   - callback/address/duplicate-slot gating
5. If validating current PR behavior before architecture work, run another live staging call to the staging number ending `0993`, then inspect Cloud Run logs for Gemini Live, Jobber memory loaded, duplicate-question behavior, and latency.

## Open Questions

- Should the approved design spec commit `8cbda76` be pushed to PR #76?
- Should the untracked research spec `docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md` be committed or left local?
- Should the untracked handoff docs in `docs/handoffs/` be committed or intentionally left local?
- Should `384ac99` stay as temporary mitigation, be reverted, or be superseded by the first state/planner implementation?
- Should PR #76 remain strictly read-only Jobber memory, or should the first state/eval skeleton be added to the same PR?
