You are continuing Hey Kevin's hybrid stateful AI receptionist controller work.

Current objective:
Implement the offline controller slice for `IntakeState`, `DialoguePlanner`, `InstructionComposer`, and replay/eval fixtures. Keep PR #76 as the separate read-only Jobber customer-memory PR.

Workspace:
- Repo/worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/hybrid-stateful-receptionist-controller`
- Branch: `codex/hybrid-stateful-receptionist-controller`
- Base: `origin/main` at `508190a Tighten Gemini live latency config`
- Important docs to read first:
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/hybrid-stateful-receptionist-controller/AGENTS.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/hybrid-stateful-receptionist-controller/docs/handoffs/2026-07-09-hybrid-stateful-controller-handoff.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/hybrid-stateful-receptionist-controller/docs/superpowers/specs/2026-07-09-hybrid-stateful-ai-receptionist-design.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/hybrid-stateful-receptionist-controller/docs/superpowers/plans/2026-07-09-hybrid-stateful-ai-receptionist.md`

Newest user request:
`$handoff`

Current state:
- New controller worktree was created from `origin/main`.
- Current branch has two committed docs commits:
  - `372a325 docs: add stateful receptionist design`
  - `1b38402 docs: add stateful receptionist implementation plan`
- Full unit baseline in this worktree passed: `291 passed, 16 warnings`.
- Handoff artifacts were just written under `docs/handoffs/` and are expected to be uncommitted unless a later user asks to commit them.
- No controller implementation code has started.
- PR #76 remote head remains `384ac99125dd6f904140cd833afbc43066168c1b`; staging health reports deploy SHA `384ac99125dd6f904140cd833afbc43066168c1b`.

Critical constraints:
- Do not work in `/Volumes/Extreme Pro/myprojects/Kevin` for this feature; that main checkout is dirty/stale.
- Do not implement controller code in `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/jobber-customer-memory`; that worktree is for PR #76.
- Do not push controller implementation commits to `codex/jobber-customer-memory`.
- Do not deploy production unless explicitly instructed.
- Do not add more ad hoc prompt rules as the next fix.
- Do not expose Jobber OAuth callback codes, admin bearer tokens, Jobber tokens, or full phone numbers.
- Do not wire new controller modules into live Gemini or ElevenLabs paths in the first offline slice.

Facts and evidence:
- `git status --short --branch` in the controller worktree before handoff docs: `## codex/hybrid-stateful-receptionist-controller...origin/main [ahead 2]`.
- `git diff --stat origin/main..HEAD`: two docs files, 2226 insertions.
- `uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit -q`: `291 passed, 16 warnings`.
- `gh pr view 76 ...`: PR #76 open draft, clean merge state, remote head `384ac99125dd6f904140cd833afbc43066168c1b`, Test success, deploy jobs skipped.
- Staging `/health`: revision `kevin-api-staging-00060-crv`, deploy SHA `384ac99125dd6f904140cd833afbc43066168c1b`.

Next recommended action:
1. Run `git status --short --branch` in `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/hybrid-stateful-receptionist-controller`.
2. Read the plan and start Task 1 with TDD: write failing `IntakeState` tests, verify RED, implement minimal state module, verify GREEN, commit.
3. Keep implementation strictly inside the new controller branch/worktree.

Verification expected:
- Focused controller tests for each task.
- Existing Jobber/receptionist tests.
- `ruff check` on touched files.
- PII/secret scan on source, tests, fixtures, and docs.
- Proof that `app/services/gemini_pipeline.py` and `app/services/voice_pipeline.py` do not import or wire new controller modules.
- Full unit suite before publishing.

Known risks:
- PR #76 still has temporary prompt mitigation `384ac99`; keep/revert/supersede is a separate decision.
- New controller branch is local only and not pushed.
- Handoff docs in this branch are uncommitted until explicitly committed.
- PR #76 worktree has untracked research/handoff/plan files; do not clean them without user approval.

If anything conflicts, the newest user request wins. Start by running:

```bash
git status --short --branch
```
