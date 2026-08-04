# Hey Kevin Voice Bakeoff Continuation Handoff

Created: 2026-07-24 09:30 EDT

Last verified: 2026-07-24 09:56 EDT

Prepared by: Codex

## Objective

Continue the offline-only voice-architecture bakeoff plan until its explicit external-authority gate. The immediate work is Task 4.2: finish the bakeoff-only coordinator that binds shared speech reservations, canonical lifecycle receipts, and the deterministic call lifecycle. Do not wire any candidate, staging, or production route.

## Current State

- Repo/workspace: `/Volumes/Extreme Pro/myprojects/Kevin`
- Implementation worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-architecture-bakeoff-plan`
- Branch: `codex/voice-architecture-bakeoff-plan` (17 commits ahead of `origin/main`)
- Latest commit: `9ab8869 feat: add offline voice call lifecycle`
- Dirty state: modified `app/services/voice_call_lifecycle.py`, modified `app/services/voice_lifecycle.py`, and untracked coordinator source/test plus the handoff pair in `docs/handoffs/`. Nothing is staged.
- Staff reviewer: `/root/plan_completion_staff` approved the lifecycle foundation, then blocked the first coordinator implementation on canonical semantic-receipt and transactional-reservation gaps. Initial code changes addressing that block now pass the existing tests and lint, but remain architecturally incomplete and have not been re-reviewed.

## Newest User Request

The user requested a handoff for resuming the offline bakeoff work in a new session. On resumption, continue Tasks 4.2-4.7 through the Task-4.8 external-approval boundary; any newer user instructions supersede this document.

## Completed Work

- `eab467e`: fail-closed offline provider-approval preflight. The CLI can return only `rejected_local_preflight` or nonzero `blocked_external_verification_required`; it does not execute providers.
- `f4135d2`: provider-neutral speech control and the shared lifecycle’s exact semantic-act -> text digest -> audio -> playout chain.
- `d75a1b3`: candidate-final `CallerObservation` extractor with atomic admission, current-turn guard, payload-free rejected outcomes, and no `IntakeState` mutation.
- `9ab8869`: revision-bound `CallLifecycle`; independent review approved its lifecycle foundation after multiple hardening rounds. It owns inert silence/closure intents only.
- Earlier offline contract, corpus, caller-UX, evaluator, and auth-hardening commits remain on the branch. No provider connection, deployment, staging change, caller test, production operation, raw transcript query, or secret use occurred in this worktree.

## In Progress

The uncommitted Task 4.2 coordinator is at:

- `app/services/voice_bakeoff_coordinator.py`
- `tests/unit/test_voice_bakeoff_coordinator.py`

The latest staff block requires:

1. Add a canonical `VoiceLifecycle` semantic-confirmation receipt verifier and have the coordinator reject raw/unaccepted confirmation events.
2. Make coordinator plan reservation atomic across `SpeechControl` and `CallLifecycle`. The current preflight attempt uses `CallLifecycle.can_reserve_question`, but its placeholder act ID is insufficient and no rollback exists if the second mutation fails.
3. Replace the coordinator substring isolation scan with AST import validation and add behavior tests for canonical confirmation rejection/acceptance and rollback.

Uncommitted partial edits already exist:

- `VoiceLifecycle.accepts_semantic_confirmation()`
- `CallLifecycle.can_reserve_question()`
- coordinator check of `accepts_semantic_confirmation()`

They pass the current focused tests, Ruff, `git diff --check`, and full suite. Those checks do not cover the blocked receipt/transaction contracts, so preserve the edits for assessment and do not assume they are correct. No staff re-review has occurred.

## Important Decisions

- Keep `VoiceLifecycle` as factual semantic/audio/playout evidence, `SpeechControl` as policy/reservation, `CallLifecycle` as silence/closure reducer, and coordinator as wiring only.
- Caller playback must arrive through a canonical accepted `VoiceLifecycle` receipt; raw enums cannot arm timers.
- Inferred playback requires accepted canonical transport evidence, matching correlation, and a positive conservative deadline.
- The bakeoff coordinator must never import `app.main`, `media_stream`, `GeminiPipeline`, `VoicePipeline`, candidate adapters, provider SDKs, sockets, or a terminal executor.
- The user explicitly does not want another prompt-rule patch. Do not change Gemini model, VAD, output pacing, production, or staging from the offline evidence.

## Files And Artifacts

- `docs/superpowers/plans/2026-07-22-voice-architecture-bakeoff-and-lifecycle-control.md`: governing bakeoff plan; Task 4.2 begins near line 1340.
- `app/services/voice_lifecycle.py`: canonical evidence state machine; uncommitted semantic-confirmation verifier added at end.
- `app/services/voice_speech_control.py`: shared policy/reservation and opaque audio/playout correlation.
- `app/services/voice_call_lifecycle.py`: approved lifecycle foundation plus uncommitted `can_reserve_question` preflight helper.
- `app/services/voice_bakeoff_coordinator.py`: uncommitted, incomplete coordinator.
- `tests/unit/test_voice_lifecycle.py`, `tests/unit/test_voice_call_lifecycle.py`, `tests/unit/test_voice_speech_control.py`: existing contract coverage.

## Commands Run And Results

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
```

Result: `936 passed, 19 warnings` before the current coordinator/uncommitted semantic-confirmation/preflight edits.

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q tests/unit/test_voice_lifecycle.py tests/unit/test_voice_call_lifecycle.py tests/unit/test_voice_speech_control.py
```

Result: `31 passed` before the latest coordinator partial edits.

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q tests/unit/test_voice_bakeoff_coordinator.py
```

Result: `1 passed` for the initial, inadequate substring-only isolation test; do not treat it as sufficient.

Fresh verification on the current dirty state:

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q \
  tests/unit/test_voice_bakeoff_coordinator.py \
  tests/unit/test_voice_call_lifecycle.py \
  tests/unit/test_voice_lifecycle.py
```

Result: `26 passed in 0.05s`.

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/ruff check \
  app/services/voice_bakeoff_coordinator.py \
  tests/unit/test_voice_bakeoff_coordinator.py \
  app/services/voice_call_lifecycle.py \
  app/services/voice_lifecycle.py
```

Result: `All checks passed!`.

```bash
git diff --check
```

Result: passed with no output.

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
```

Result: `937 passed, 19 warnings in 6.56s`.

## Verification

- Passed: lifecycle foundation staff review; fresh current-dirty-state focused suite at `26 passed`; Ruff on all four current coordinator/lifecycle files; fresh `git diff --check`; fresh full suite at `937 passed, 19 warnings`.
- Historical only: full suite at `936 passed` and focused lifecycle/speech suite at `31 passed` before the current coordinator edits.
- Missing coverage: canonical accepted/rejected semantic receipt behavior, atomic rollback after second-step lifecycle rejection, and AST-based import isolation.
- Not run: independent staff re-review of the coordinator correction.
- Not run: staging/caller validation for the older 128-token cap; it remains separate and requires user feedback plus payload-safe staging logs.

## Risks And Watchouts

- High: Do not deploy or connect a provider. Task 4.8 is the first provider-connected exception and requires a sealed one-use approval envelope, trusted external verification, and nonproduction isolation.
- High: Current coordinator changes are incomplete despite the green suite. Do not commit them before resolving the staff P1s, adding the missing behavioral/AST tests, rerunning verification, and obtaining staff approval.
- High: Preserve all unrelated worktrees and the dirty repository root. Do implementation only in the bakeoff worktree.
- Authority: `/Volumes/Extreme Pro/myprojects/Kevin/AGENTS.md` differs from the worktree copy and contains an embedded historical memory note claiming Task 4.2 was complete. The current staff block, dirty diff, and newest user request supersede that stale completion note.
- Privacy: Do not query/repeat raw caller transcripts, full phone numbers, OAuth codes, tokens, or secrets. Use only payload-safe data.
- Do not turn offline tests or mocks into caller-experience, provider-latency, caller-playback, or safety-completeness claims.

## Do Not Do

- Do not work in `/Volumes/Extreme Pro/myprojects/Kevin` root or the historical `voice-session-latency-eval` worktree.
- Do not deploy staging/production or change live routing without current explicit user direction and gates.
- Do not add prompt rules, alter Gemini/VAD/pacing, or mount bakeoff code in `app.main`/`media_stream.py`.
- Do not reset, clean, or discard current uncommitted changes.

## Next Recommended Steps

1. Start with `git status --short --branch`; inspect the three uncommitted coordinator-related edits.
2. Replace the coordinator’s `can_reserve_question(... act_id="pending_question")` approach with a real transactional contract or narrowly-scoped `SpeechControl` rollback, then add tests proving no partial speech reservation survives lifecycle rejection.
3. Test canonical semantic confirmation by constructing an accepted `VoiceLifecycle` event chain; reject raw/unaccepted events.
4. Replace the coordinator test’s substring check with AST import validation restricting imports to approved shared-contract modules.
5. Run focused coordinator/lifecycle tests, Ruff, `git diff --check`, then the full suite. Request staff re-review; only then commit Task 4.2 and proceed to Tasks 4.3–4.7.

## Open Questions

- No user decision is currently needed for offline work. The first needed authority is the sealed external approval required before Task 4.8/provider connection.
