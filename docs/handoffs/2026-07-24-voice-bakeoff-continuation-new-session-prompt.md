You are continuing Hey Kevin’s offline voice-architecture bakeoff plan.

Current objective:
Finish Task 4.2’s bakeoff-only coordinator safely, then continue the offline Tasks 4.3–4.7. Do not stop at intermediate review or commit checkpoints. The first provider-connected work is Task 4.8 and remains blocked by a sealed external approval gate.

Workspace:
- Repo/worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-architecture-bakeoff-plan`
- Branch: `codex/voice-architecture-bakeoff-plan`
- Important docs to read first:
  - `/Volumes/Extreme Pro/myprojects/Kevin/AGENTS.md`
  - `docs/handoffs/2026-07-24-voice-bakeoff-continuation-handoff.md`
  - `docs/superpowers/plans/2026-07-22-voice-architecture-bakeoff-and-lifecycle-control.md`

Newest user request:
This prompt is the requested handoff. Resume the offline Tasks 4.2–4.7 continuously from the dirty state below. Any user instruction sent after this prompt supersedes it.

Current state:
- Branch is 17 commits ahead of `origin/main`; latest commit is `9ab8869 feat: add offline voice call lifecycle`.
- Completed/committed: approval preflight, shared speech/lifecycle contract, candidate-final observation extractor, and independently approved call-lifecycle foundation.
- Dirty/uncommitted: `app/services/voice_call_lifecycle.py`, `app/services/voice_lifecycle.py`, `app/services/voice_bakeoff_coordinator.py`, `tests/unit/test_voice_bakeoff_coordinator.py`, and the handoff pair under `docs/handoffs/`. Nothing is staged.
- The coordinator is incomplete. Staff blocked it on canonical semantic receipt validation, atomic reservation across `SpeechControl` and `CallLifecycle`, and AST-based isolation testing. The current green suite does not cover those P1s.

Critical constraints:
- Do not work in the dirty repo root or historical worktrees; use only this bakeoff worktree.
- Do not deploy, connect a provider, query raw transcripts, or touch production/staging.
- Do not add prompt rules or change Gemini model/VAD/pacing.
- Preserve unrelated changes/worktrees. Use payload-safe evidence only.
- Use an independent staff review for architectural uncertainty or before committing a substantive coordinator correction.

Facts and evidence:
- On the current dirty state, the coordinator/call-lifecycle/voice-lifecycle focused suite passes: `26 passed`.
- On the current dirty state, Ruff passes, `git diff --check` passes, and the full suite passes: `937 passed, 19 warnings`.
- The lifecycle foundation passed independent staff review; current coordinator does not.

Next recommended action:
1. Inspect uncommitted files and resolve the coordinator’s atomic reservation and canonical semantic-receipt P1s.
2. Add AST import isolation and behavioral rollback tests.
3. Run focused tests, Ruff, `git diff --check`, full suite, and staff review; commit only when approved.

Verification expected:
- `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q tests/unit/test_voice_bakeoff_coordinator.py tests/unit/test_voice_call_lifecycle.py tests/unit/test_voice_lifecycle.py`
- `PATH="$PWD/.venv/bin:$PATH" .venv/bin/ruff check app/services/voice_bakeoff_coordinator.py tests/unit/test_voice_bakeoff_coordinator.py app/services/voice_call_lifecycle.py app/services/voice_lifecycle.py`
- `git diff --check`
- full `pytest -q` after focused checks.

Known risks:
- The latest coordinator/preflight changes pass existing checks but may still be wrong because receipt, rollback, and AST-isolation coverage is missing.
- No offline result proves live caller playback, safety completeness, latency, or provider behavior.
- The user is frustrated by agents stopping; keep working through safe offline steps and reserve questions only for actual external authority.

If anything conflicts, the newest user request wins. Start by running:

```bash
git status --short --branch
```
