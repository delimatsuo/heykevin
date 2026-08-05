# Jobber Memory And Resilient Agent Handoff

Created: 2026-07-09 11:28 EDT
Prepared by: Codex

## Objective

Continue Hey Kevin PR #76 for Jobber customer memory, but update the next work direction based on the user's latest concern: Kevin should not become a brittle prompt-rule system. The user wants a resilient AI receptionist architecture that uses memory, conversation state, planning, and evaluation rather than dozens of small prompt exceptions.

## Current State

- Repo/workspace: `/Volumes/Extreme Pro/myprojects/Kevin`
- Active worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory`
- Branch: `codex/jobber-customer-memory`
- Latest commit: `384ac99 Avoid reasking known service action`
- Full latest SHA: `384ac99125dd6f904140cd833afbc43066168c1b`
- PR: `https://github.com/delimatsuo/heykevin/pull/76`
- PR state from `gh pr view 76`: open, draft, base `main`, merge state `CLEAN`, head `codex/jobber-customer-memory`, head SHA `384ac99125dd6f904140cd833afbc43066168c1b`
- GitHub Actions for latest PR head: `Test` succeeded; deploy jobs skipped because PR deploys do not run from draft PRs.
- Staging service: `kevin-api-staging`
- Staging URL: `https://kevin-api-staging-l63rergg7a-uc.a.run.app`
- Staging revision: `kevin-api-staging-00060-crv`
- Staging deploy SHA: `384ac99125dd6f904140cd833afbc43066168c1b`
- Staging traffic: 100% to latest revision `kevin-api-staging-00060-crv`; old revision `kevin-api-staging-00059-wav` remains tagged `staging` but has no percent traffic.
- Dirty state before writing this handoff: no modified tracked files, no staged files, untracked `docs/handoffs/` and untracked `docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`.
- Current local `gcloud config list --format='value(core.account,core.project,billing.quota_project)'`: `delimatsuo@gmail.com`, `mypeople-staging-20260425`, `kevin-491315`. For Kevin deploys, use explicit `--account=deli@ellaexecutivesearch.com --project=kevin-491315`.

## Newest User Request

The newest request is `$handoff`. Immediately before the handoff request, the user asked whether the research on latest trends in agent building and memory still exists in the plans. A local untracked research spec now exists at `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`; it is a spec, not a plan file, and it has not been committed.

## Completed Work

- PR #76 implements read-only Jobber customer memory for Gemini Live:
  - `app/services/jobber.py`: `lookup_customer_memory(auth, phone)`, phone-match guard, normalized memory structure, compact prompt formatter.
  - `app/services/gemini_pipeline.py`: starts Jobber memory lookup before Gemini setup, applies a `0.9s` latency budget, injects compact memory context into system setup, and lets greeting use known caller first name without mentioning Jobber.
  - `tests/unit/test_jobber.py`: memory lookup, phone-match, prompt formatting, conflict-ordering tests.
  - `tests/unit/test_receptionist_intelligence.py`: Gemini setup/greeting tests.
- Commit `d185edd Guard Jobber memory matching` tightened privacy correctness by requiring returned Jobber candidates to match caller phone before injecting memory.
- Commit `a4c24fb Fix job card parsing for non-text Anthropic blocks` fixed post-call extraction parsing when Anthropic returns non-text blocks before text blocks.
- Commit `384ac99 Avoid reasking known service action` added a narrow prompt guardrail and regression test after a staging call where Gemini asked whether a toilet request was repair/replacement/new installation after the caller already said replacement. The user later objected that this is not the desired long-term architecture.
- Staging data fixes were applied:
  - Staging smoke contractor `codex_phase0_smoke` was changed from `voice_engine=elevenlabs` to `voice_engine=gemini` in staging Firestore.
  - Staging Jobber OAuth was reauthorized after the previous refresh token returned `401 The provided refresh token is not valid`.
  - A read-only Jobber GraphQL smoke returned HTTP `200`, `JOBBER_AUTH=OK`, `JOBBER_HAS_DATA=True`, `JOBBER_HAS_ERRORS=False`.
- Live-call observations:
  - First staging call after deploy used legacy ElevenLabs because staging contractor had `voice_engine=elevenlabs`; this explained the different voice and talk-over behavior.
  - After `voice_engine=gemini` and Jobber reauth, live staging calls at `2026-07-09T14:48:24Z` and `2026-07-09T14:49:04Z` used Gemini Live and logged `Jobber customer memory loaded`.
  - The `14:49:04Z` call transcript showed the repeated service-action question. Gemini recognized Jonathan and used the Jobber path, but still followed a prompt example like a checklist.
- Staging was redeployed from the feature worktree after commit `384ac99`:
  - Cloud Run revision: `kevin-api-staging-00060-crv`
  - Health check returned `deploy_sha=384ac99125dd6f904140cd833afbc43066168c1b`.

## In Progress

- No code edits are currently in progress.
- The latest architectural direction is unresolved:
  - The committed prompt-rule mitigation exists on the PR branch and is deployed to staging.
  - The user explicitly pushed back against adding rules and asked for a resilient agent architecture.
  - A research/spec artifact exists but is untracked: `docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`.
- This handoff package is being created in `docs/handoffs/` and should remain uncommitted unless the user asks to commit.

## Important Decisions

- 2026-07-08: Keep PR #76 narrow and read-only for Jobber memory.
  - Rationale: avoid mixing customer memory with scheduling/write behavior before proving live-call behavior.
  - Consequence: PR #76 does not book appointments or write Jobber requests during the call.
- 2026-07-08: Validate Jobber phone match before injecting memory.
  - Rationale: wrong customer memory would be a privacy and product correctness failure.
  - Consequence: `lookup_customer_memory` returns no memory when Jobber phone candidates do not match the caller.
- 2026-07-09: Staging smoke contractor must use Gemini for PR validation.
  - Rationale: testing ElevenLabs does not validate Gemini Jobber-memory behavior.
  - Consequence: staging Firestore was updated to `voice_engine=gemini` for `codex_phase0_smoke`.
- 2026-07-09: Jobber OAuth had to be reauthorized for staging.
  - Rationale: staging logs showed refresh-token `401`.
  - Consequence: Jobber memory lookup now succeeds in staging.
- 2026-07-09: Prompt exception in commit `384ac99` is a temporary mitigation, not the preferred architecture.
  - Rationale: user rejected the pattern of adding dozens of small natural-language rules.
  - Consequence: next work should design or implement a stateful agent/controller approach instead of adding more prompt rules.

## Files And Artifacts

- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/app/services/jobber.py`: Jobber API reads, memory normalization, phone-match guard, prompt formatting.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/app/services/gemini_pipeline.py`: Gemini Live setup, Jobber memory timing and prompt injection.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/app/services/voice_pipeline.py`: shared prompt builder; includes commit `384ac99` temporary prompt guardrail.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/app/services/job_card.py`: post-call job-card extraction parser fixed by `a4c24fb`.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/tests/unit/test_jobber.py`: Jobber memory unit tests.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/tests/unit/test_receptionist_intelligence.py`: Gemini and prompt regression tests, including the temporary prompt-rule regression.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`: untracked research brief on current voice-agent memory/resiliency patterns. It says primary sources were checked on 2026-07-09 and recommends business memory, customer memory, call-state memory, a receptionist controller, tool sequencing, and eval scenarios.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/handoffs/2026-07-08-jobber-customer-memory-handoff.md`: earlier handoff before live-call validation and later patches.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/handoffs/2026-07-08-jobber-customer-memory-new-session-prompt.md`: earlier paste-ready prompt.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/scripts/phase0_staging_smoke.py`: warning: seed data still contains `"voice_engine": "elevenlabs"` and may reset the staging smoke contractor if rerun.

## Commands Run And Results

```bash
git status --short --branch
```

Result before writing this handoff:

```text
## codex/jobber-customer-memory...origin/codex/jobber-customer-memory
?? docs/handoffs/
?? docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md
```

```bash
git log --oneline -8 --decorate
```

Result included:

```text
384ac99 (HEAD -> codex/jobber-customer-memory, origin/codex/jobber-customer-memory) Avoid reasking known service action
a4c24fb Fix job card parsing for non-text Anthropic blocks
d185edd Guard Jobber memory matching
3c1087e Add Jobber customer memory context
508190a (origin/main, origin/HEAD) Tighten Gemini live latency config
```

```bash
gh pr view 76 --json number,title,state,isDraft,mergeStateStatus,headRefName,baseRefName,headRefOid,url,reviewDecision,statusCheckRollup,updatedAt
```

Result summary: PR #76 is open, draft, base `main`, head `codex/jobber-customer-memory`, merge state `CLEAN`, head SHA `384ac99125dd6f904140cd833afbc43066168c1b`; `Test` check succeeded for latest head; deploy checks skipped.

```bash
curl -fsS https://kevin-api-staging-l63rergg7a-uc.a.run.app/health
```

Result:

```json
{"status":"ok","environment":"staging","service":"kevin-api-staging","revision":"kevin-api-staging-00060-crv","deploy_sha":"384ac99125dd6f904140cd833afbc43066168c1b"}
```

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_intelligence.py::test_business_prompt_does_not_reask_known_service_action -q
```

Result after the temporary prompt patch: `1 passed, 1 warning`.

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_jobber.py tests/unit/test_receptionist_intelligence.py -q
```

Result after commit `384ac99`: `56 passed, 2 warnings`.

```bash
uv run --python 3.12 --with '.[dev]' ruff check app/services/jobber.py app/services/gemini_pipeline.py app/services/voice_pipeline.py tests/unit/test_jobber.py tests/unit/test_receptionist_intelligence.py
```

Result: `All checks passed!`

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit -q
```

Result after commit `384ac99`: `302 passed, 16 warnings`.

```bash
gcloud run deploy kevin-api-staging --source . --project kevin-491315 --region us-central1 --account=deli@ellaexecutivesearch.com --allow-unauthenticated --service-account kevin-api-staging-runtime@kevin-491315.iam.gserviceaccount.com --update-env-vars DEPLOY_SHA=384ac99125dd6f904140cd833afbc43066168c1b --quiet
```

Result: deployed revision `kevin-api-staging-00060-crv`, routed 100% traffic.

## Verification

- Passed: focused prompt regression test after temporary patch.
- Passed: targeted Jobber and receptionist unit tests, `56 passed`.
- Passed: ruff on touched services/tests.
- Passed: full unit suite, `302 passed`.
- Passed: GitHub Actions `Test` for latest PR head.
- Passed: staging health check for revision `kevin-api-staging-00060-crv`, deploy SHA `384ac99125dd6f904140cd833afbc43066168c1b`.
- Passed before latest deploy: staging Jobber OAuth callback and read-only Jobber API auth smoke.
- Not run after latest deploy: another live staging call against revision `kevin-api-staging-00060-crv`.
- Not run: production deploy.
- Not implemented: resilient agent architecture with explicit call-state/planner/evals.

## Risks And Watchouts

- High: The latest commit `384ac99` conflicts with the user's architectural preference. It is a narrow prompt-rule mitigation and should not be treated as the final design.
- High: Do not add more ad hoc prompt exceptions. The user explicitly wants a resilient agent that does not rely on dozens of small rules.
- High: Do not work in the dirty main checkout at `/Volumes/Extreme Pro/myprojects/Kevin`. Use `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory`.
- High: Do not deploy production unless the user explicitly asks.
- High: Do not expose Jobber OAuth callback codes, admin bearer tokens, Jobber tokens, or full phone numbers.
- Medium: `scripts/phase0_staging_smoke.py` can reset `codex_phase0_smoke` to `voice_engine=elevenlabs`. If the script is rerun, staging live-call validation may accidentally test the wrong voice pipeline again.
- Medium: The current local default gcloud account/project are not the successful Kevin deploy account/project. Use explicit deploy flags or set config intentionally before future deploys.
- Medium: The research spec is untracked. It can be lost if another agent cleans untracked files.
- Medium: Existing handoff files in `docs/handoffs/` are also untracked.
- Medium: PR #76 was deployed directly to Cloud Run staging from the feature worktree; the `staging` branch remains divergent and should not be force-updated casually.
- Medium: Staging and production Firestore projects differ. Staging data lives in `kevin-staging-491315`; production data lives in `kevin-491315`.

## Do Not Do

- Do not merge PR #76 or mark it ready until the user decides whether the temporary prompt-rule mitigation should stay, be reverted, or be superseded by a stateful architecture plan.
- Do not add another natural-language prompt exception as the next response to a live-call failure.
- Do not deploy production.
- Do not revert unrelated untracked handoff/spec files without asking.
- Do not work in `/Volumes/Extreme Pro/myprojects/Kevin` for this feature.
- Do not expose sensitive callback URLs with Jobber `code`, full phone numbers, or bearer tokens.

## Next Recommended Steps

1. Acknowledge to the user that the research artifact exists as an untracked spec, not a committed plan: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory/docs/superpowers/specs/2026-07-09-ai-receptionist-memory-resiliency.md`.
2. Ask whether to commit the research spec and handoff docs, or leave them uncommitted.
3. Decide with the user whether commit `384ac99 Avoid reasking known service action` should remain as temporary mitigation, be reverted, or be replaced by an explicit architecture plan.
4. If the user wants architecture work, produce a concrete implementation plan for:
   - `CallState` / `IntakeState` with known facts and already-asked slots.
   - `DialoguePlanner` that chooses the next allowed action from state.
   - compact business memory and customer memory cards.
   - per-turn Gemini instruction generated from state, not a long static rule list.
   - replay/eval tests from the staging transcripts.
5. If the user wants to continue PR #76 validation first, run another live call to the staging number ending `0993` and verify staging logs show Gemini Live, Jobber memory loaded, and no duplicate service-action question.

## Open Questions

- Should the untracked research spec be committed into PR #76 or moved into a separate design branch?
- Should the temporary prompt-rule commit `384ac99` be reverted before continuing architectural work?
- Should PR #76 remain a narrow read-only Jobber memory PR, or should it expand to include the first state/planner slice?
- Does the user want a staff-engineer review of the stateful agent architecture before more code changes?
