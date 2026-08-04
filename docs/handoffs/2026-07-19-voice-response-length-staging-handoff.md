# Hey Kevin Voice Response Length Staging Handoff

Created: 2026-07-19 19:35 EDT
Prepared by: Codex

## Objective

Validate the staged, code-enforced Gemini Live response-length cap. The reported symptom was that calls felt slower over successive turns. The diagnostic evidence showed stable response startup and transport, but some generated replies kept the phone line occupied for about seven seconds. The change reduces the existing Live `max_output_tokens` limit from 256 to 128 without changing prompts, VAD, pacing, model selection, or production.

## Current State

- Repo/workspace: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-session-latency-eval`
- Historical branch in this worktree: `codex/voice-response-length-cap` at `d61f67a464b7b3e6ad9eeda01d049fc0df6946c0` (`fix: cap live receptionist response length`)
- Current remote main: `33e73cfea2b953057c0f320ef33d9afd6239c4bd`, which includes merged PR #124 plus later offline-controller PRs #125 and #126.
- Worktree state before these handoff artifacts: clean. The two handoff files are intentionally uncommitted.
- Current active staging revision: `kevin-api-staging-00095-bav`, deploy SHA `33e73cfea2b953057c0f320ef33d9afd6239c4bd`.
- No production deployment was requested or performed.
- No background agents remain active.

## Newest User Request

Prepare a handoff. The next session should first honor any newer user request, then complete the outstanding staging call validation if still relevant.

## Completed Work

- Added payload-safe voice timing telemetry in merged PR #122 (`baf2fd9`), including provider first audio, Twilio first-media send, paced playout drain, Twilio mark state, model token usage, and ingress summaries.
- Diagnosed one five-turn staging call before the cap. Provider first audio was 868, 922, 942, and 972 ms across response turns 2-5. Twilio first-media send was 1 ms after provider audio on each measured turn. Ingress mean lag was 1 ms and max lag was 177 ms. These values do not show a meaningful turn-by-turn startup regression.
- The same trace showed paced playout of 7.120 s and 6.828 s for two normal replies, followed by 3.044 s and 3.313 s. Those long generated replies were the actionable caller-experience issue.
- Obtained an independent staff review: approve a staging-only, reversible cap experiment; do not change prompts, VAD, pacing, or model based on the pre-cap diagnostic trace.
- Implemented the cap in `app/services/gemini_pipeline.py`: `MAX_RESPONSE_OUTPUT_TOKENS` changed from 256 to 128. Updated the generation-config assertion in `tests/unit/test_receptionist_intelligence.py`.
- PR #124 was merged: https://github.com/delimatsuo/heykevin/pull/124. Merge commit: `0b8de0be6ca07c67f18949ded7c26006bdf3aa75`.
- The exact original staging candidate `d61f67a` deployed successfully as `kevin-api-staging-00093-rim`. It was later superseded by `00095-bav`; current `origin/main` still contains the cap.

## In Progress

- Caller-heard validation of the 128-token cap has not occurred. The user switched to requesting a handoff before reporting completion of the new call.
- The current active staging revision contains the cap and later offline-controller-only merges. It is suitable for a new staging validation, but it is not the exact `00093-rim` diagnostic revision.

## Important Decisions

- The reported symptom is not yet proven to be cumulative Gemini latency. Treat it as response duration until new evidence shows otherwise.
- Keep the reply cap as a configuration control, not another ad hoc prompt rule. The existing system prompt already asks for brief responses.
- Do not alter the output pacing factor. Pacing protects Twilio buffering and barge-in behavior; making it faster would not make generated speech shorter.
- Production deployment is a separate manual action and is out of scope without explicit user authorization.
- Use a fresh worktree from `origin/main` for any new edits. Do not extend this historical feature branch, which is behind current main.

## Files And Artifacts

- `app/services/gemini_pipeline.py`: Gemini Live generation config and `MAX_RESPONSE_OUTPUT_TOKENS` cap.
- `app/webhooks/media_stream.py`: Twilio playback mark and ingress timing telemetry added by PR #122.
- `tests/unit/test_receptionist_intelligence.py`: generation-config contract test for the cap.
- `.github/workflows/deploy.yml`: current deployment authority. Staging requires `workflow_dispatch` with an exact candidate SHA; production requires a distinct manual dispatch from main.
- `docs/handoffs/2026-07-19-voice-response-length-staging-new-session-prompt.md`: paste-ready continuation prompt.

## Commands Run And Results

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit/test_receptionist_intelligence.py -q
```

Result: `76 passed, 2 warnings`.

```bash
uv run --python 3.12 --with '.[dev]' python -m pytest tests/unit -q
```

Result: `804 passed, 16 warnings`.

```bash
uv run --python 3.12 --with '.[dev]' ruff check app/services/gemini_pipeline.py tests/unit/test_receptionist_intelligence.py
git diff --check
```

Result: passed.

```bash
gh pr view 124 --repo delimatsuo/heykevin --json state,mergedAt,mergeCommit,url
gh run view 29701191227 --repo delimatsuo/heykevin --json conclusion,url
```

Result: PR #124 merged; staging workflow `29701191227` succeeded; production job was skipped.

```bash
curl -sf https://kevin-api-staging-l63rergg7a-uc.a.run.app/health
```

Result at handoff: `status=ok`, environment `staging`, revision `kevin-api-staging-00095-bav`, deploy SHA `33e73cfea2b953057c0f320ef33d9afd6239c4bd`.

## Verification

- Passed: focused receptionist test, full unit suite, Ruff, diff integrity, payload-safe added-line secret scan, PR CI, staging deploy workflow, and staging health readback for the original cap candidate.
- Not run: a caller test after the 128-token cap became active. No post-cap `response_first_audio` events were found in the original `00093-rim` revision before it was superseded.
- Needed next: caller-heard five-turn and safety validation on the active staging revision, then a fresh payload-safe log analysis.

## Risks And Watchouts

- The 128-token cap may still permit a long reply or may truncate unusually detailed safety guidance. Do not claim improvement until a new call produces playout evidence and the caller confirms the safety response remained complete.
- `response_first_audio` is measured from the latest input-transcript fragment, not a perfect caller-end-of-speech marker. Pair it with playout and caller feedback.
- Current staging can change again because another agent may deploy a newer main revision. Always read `/health` immediately before and after the caller test and bind log queries to that revision.
- Do not expose full phone numbers, OAuth codes, admin bearer tokens, Jobber tokens, or secrets in logs, docs, prompts, or responses.
- The repository root `/Volumes/Extreme Pro/myprojects/Kevin` is a dirty/stale checkout. Do not implement work there.

## Do Not Do

- Do not deploy production or run the production workflow without explicit current user authorization.
- Do not edit `app/services/voice_pipeline.py` to add more prompt rules as the next latency fix.
- Do not change Gemini model, VAD, or output pacing from this one diagnostic trace.
- Do not revert or clean unrelated controller work or user changes.
- Do not assume the old staging revision remains live.

## Next Recommended Steps

1. In a fresh worktree from current `origin/main`, read current staging health and verify `MAX_RESPONSE_OUTPUT_TOKENS = 128` is present at the deployed SHA.
2. Ask the user to call the existing staging number from Twilio and complete five brief turns, then one safety scenario. Do not print the full number.
3. Query `voice_timing` for the new active revision. Compare per-turn `response_first_audio`, `response_first_twilio_media_sent`, `twilio_playback_mark_resolved`, `response_playout_drained`, `model_usage`, and `inbound_media_delivery_summary`.
4. Decide only from the new caller feedback and payload-safe timing evidence whether the cap materially reduced normal playout and preserved complete safety guidance. Keep staging only until that decision is recorded.

## Open Questions

- Did the caller perceive materially shorter normal replies after the cap?
- Does the cap ever truncate safety guidance in the tested scenario?
- Is the user asking to continue controller work after validation, or only to close the live-latency loop?
