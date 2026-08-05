# Hey Kevin Voice Response Length Staging Validation Handoff

Created: 2026-07-19 19:39 EDT
Updated: 2026-07-20 11:15 EDT
Prepared by: Codex

## Objective

Validate the new 192-token Gemini Live staging candidate after the 128-token experiment failed caller-heard completeness. The exact 192-token candidate is now deployed only to staging and requires a five-turn plus safety caller test before merge or any production decision. Do not add prompt rules or change the model, VAD, or output pacing.

## Current State

- Repo root: `/Volumes/Extreme Pro/myprojects/Kevin`
- Fresh validation worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-response-length-staging-validation`
- Branch: `codex/voice-response-length-staging-validation`, tracking `origin/main`
- Validation worktree commit remains `33e73cfea2b953057c0f320ef33d9afd6239c4bd`; current `origin/main` advanced to `6dc3013df78070cd60871febb1a541977ea4c3b3` after the mode fix and build 25 metadata merged.
- Dirty state: the fresh worktree was clean before this handoff; only the two untracked handoff artifacts under `docs/handoffs/` should exist afterward.
- Dirty root warning: `/Volumes/Extreme Pro/myprojects/Kevin` is `ahead 1, behind 147`, with 11 modified files, 10 untracked entries, and no staged files. Treat it as read-only.
- Historical worktree warning: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-session-latency-eval` remains on merged branch `codex/voice-response-length-cap` at `d61f67a464b7b3e6ad9eeda01d049fc0df6946c0` and has its own untracked `docs/handoffs/`. Do not edit, clean, or resume implementation there.
- Fresh staging health at 2026-07-19 19:39 EDT: `status=ok`, revision `kevin-api-staging-00095-bav`, deploy SHA `33e73cfea2b953057c0f320ef33d9afd6239c4bd`.
- Post-call staging health at 2026-07-20 10:23 EDT remained `status=ok`, revision `kevin-api-staging-00095-bav`, deploy SHA `33e73cfea2b953057c0f320ef33d9afd6239c4bd`.
- Current staging health at 2026-07-20 11:04 EDT: `status=ok`, revision `kevin-api-staging-00097-tic`, exact deploy SHA `2614dab2bfdf468ac78734f2877c52d3901104d7`; this candidate sets the cap to 192 tokens.
- No production deployment was requested or performed in this handoff turn.
- PR #127 is the draft 192-token voice candidate and passed PR CI. Guarded workflow run `29753133818` passed tests, deployed that exact SHA to staging, verified health, and skipped production.
- PR #128, the Business Assistant to Personal Assistant persistence fix, merged to main at `e653db39d0c2cf22c229c39e68bc214dc95ff184`. Do not mix its release work into the voice candidate.
- TestFlight source branch `codex/ios-build-25` is at exact SHA `95edb21e2b7e49894576a35691243ee828f69b95`, version `1.2.5 (25)`. Apple validation and upload succeeded, processing reached `VALID`, and metadata PR #129 merged to main at `6dc3013df78070cd60871febb1a541977ea4c3b3`. No App Store release submission occurred.

## Newest User Request

The user said the 128-token replies kept cutting out and required the issue to be fixed. The 192-token recovery is staged and awaiting the user's repeat call. The separate Personal-mode fix is merged and TestFlight build `1.2.5 (25)` has reached a valid processed state.

## Completed Work

- PR #124, `fix: cap live receptionist response length`, merged at `0b8de0be6ca07c67f18949ded7c26006bdf3aa75`.
- The cap is code-enforced in `app/services/gemini_pipeline.py` as `MAX_RESPONSE_OUTPUT_TOKENS = 128`; the Live generation config passes that constant as `max_output_tokens`.
- The original cap candidate `d61f67a464b7b3e6ad9eeda01d049fc0df6946c0` passed prior focused and full unit validation and was staged historically as revision `kevin-api-staging-00093-rim`.
- The exact merged PR workflow state was refreshed: PR #124 is merged, its test check succeeded, and merge-triggered staging and production jobs were skipped.
- Manual workflow run `29701191227` succeeded for tests and staging deployment; its production job was skipped.
- Current `origin/main` and current staging both resolve to `33e73cfea2b953057c0f320ef33d9afd6239c4bd`, which contains the 128-token cap plus later PRs #125 and #126.
- The current deployed SHA was inspected directly rather than inferred from the revision name; it contains `MAX_RESPONSE_OUTPUT_TOKENS = 128` at `app/services/gemini_pipeline.py:89`.
- A post-cap staging call completed on revision `kevin-api-staging-00095-bav` with redacted call label `CAe19e3f`.
- Five replies drained in 3.165 s, 2.607 s, 3.310 s, 3.570 s, and 3.167 s. Measured non-greeting first-audio latency was 942 ms, 911 ms, 805 ms, and 1,012 ms.
- The call exercised the safety path: payload-safe telemetry emitted `urgency_detected` on response turn 3.
- Twilio received first media 1-2 ms after provider first audio. Each response emitted a playback mark and `response_playout_drained`; the call summary reported mean inbound delivery lag 1 ms, max lag 110 ms, and max queued input audio 320 ms.
- The user reported repeated caller-heard mid-phrase cutoffs. This fails the required safety/completeness acceptance criterion even though the duration and latency criteria passed.
- Independent staff review approved 192 only as a reversible, single-variable staging experiment with exact-SHA CI and caller-heard safety validation.
- Recovery worktree `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-response-length-192-recovery`, branch `codex/voice-response-length-192-recovery`, exact SHA `2614dab2bfdf468ac78734f2877c52d3901104d7`, changes only the cap assertion from 128 to 192. It passed 76 focused tests, 842 mainline unit tests, Ruff, diff integrity, added-line secret scan, PR CI, and the guarded staging workflow.
- PR #128 fixed the existing-member mode switch by PATCHing backend `mode` before completing the existing-number onboarding fast path. Its focused 10 tests, full 844-test suite, Ruff, diff integrity, iOS simulator build, and PR CI passed before merge.
- TestFlight build `1.2.5 (25)` was exported from exact SHA `95edb21e2b7e49894576a35691243ee828f69b95`. The final IPA proved production backend/environment, Apple Distribution signing, production APNs, correct application identifier, `get-task-allow=false`, and strict signature validity. Apple package validation and upload succeeded.

## In Progress

- The user has been asked to call the same staging number and repeat five concise turns plus the plumbing safety request. No post-deploy caller events have appeared yet for revision `kevin-api-staging-00097-tic`.
- PR #127 must remain unmerged until caller-heard phrases and safety guidance are complete and the former 6-7 second normal-response tail does not recur.
- TestFlight delivery for `1.2.5 (25)` reached Apple status `VALID`. Do not submit an App Store release or widen beta distribution without separate authority.

## Important Decisions

- The pre-cap trace did not show meaningful cumulative startup latency. Provider first audio for four measured turns was 868, 922, 942, and 972 ms; Twilio sent first media 1 ms after provider audio; inbound media lag peaked at 177 ms.
- The actionable pre-cap problem was long generated speech: two normal replies drained 7.120 s and 6.828 s of paced playout, followed by 3.044 s and 3.313 s.
- An independent staff review approved only a reversible 128-token staging experiment and required a five-turn normal conversation plus safety validation before further tuning.
- Keep response length as a generation-config control. Do not add another ad hoc prompt rule, change the Gemini model, change VAD, or change output pacing from the existing evidence alone.
- Caller-experience success requires two sources: payload-safe timing events and the user's direct feedback. Neither source alone is sufficient.
- On 2026-07-20, the combined sources produced a clear FAIL: response duration and first-audio targets passed, while caller-heard completeness failed.
- The playout path did not log a backlog overflow, dropped queued output, or an interrupted response for turns 2-5. All generated output drained to Twilio. The trace therefore does not support a Twilio pacing or ingress change; the observed cutoff is upstream at model generation/turn completion and is consistent with the cap experiment. The provider's explicit finish reason is not currently logged, so do not claim a more specific provider stop cause than the evidence supports.
- Production deployment remains a separate reserved action requiring explicit current-session user authorization.

## Files And Artifacts

- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-response-length-staging-validation/app/services/gemini_pipeline.py`: active response cap and payload-safe Gemini timing events.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-response-length-staging-validation/app/webhooks/media_stream.py`: Twilio playback-mark and inbound-delivery timing events.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-response-length-staging-validation/tests/unit/test_receptionist_intelligence.py`: generation-config contract test for the 128-token cap.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-response-length-staging-validation/.github/workflows/deploy.yml`: staging and production deployment authority.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-session-latency-eval/docs/handoffs/2026-07-19-voice-response-length-staging-handoff.md`: prior full diagnostic and implementation handoff; historical and uncommitted.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-response-length-staging-validation/docs/handoffs/2026-07-19-voice-response-length-staging-validation-new-session-prompt.md`: paste-ready continuation prompt.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-response-length-192-recovery`: exact 192-token candidate and PR #127 worktree.
- `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/ios-build-25`: exact TestFlight `1.2.5 (25)` source and draft PR #129 worktree.

## Commands Run And Results

```bash
git status --short --branch
```

Result in the repo root: `main...origin/main [ahead 1, behind 147]` with unrelated user changes. No cleanup was attempted.

```bash
git fetch origin main
git worktree add -b codex/voice-response-length-staging-validation \
  /Volumes/Extreme\ Pro/myprojects/Kevin/.worktrees/voice-response-length-staging-validation \
  origin/main
```

Result: clean fresh worktree created at `33e73cfea2b953057c0f320ef33d9afd6239c4bd`.

```bash
curl -sf https://kevin-api-staging-l63rergg7a-uc.a.run.app/health
```

Result before and after the call: `status=ok`, environment `staging`, revision `kevin-api-staging-00095-bav`, deploy SHA `33e73cfea2b953057c0f320ef33d9afd6239c4bd`.

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="kevin-api-staging" AND resource.labels.revision_name="kevin-api-staging-00095-bav" AND jsonPayload.message:"voice_timing" AND jsonPayload.message:"CAe19e3f"' \
  --project=kevin-491315 --order=asc \
  --format='value(timestamp,jsonPayload.message)'
```

Result: five `response_playout_drained` events at 3.165 s, 2.607 s, 3.310 s, 3.570 s, and 3.167 s; non-greeting `response_first_audio` at 942 ms, 911 ms, 805 ms, and 1,012 ms; first-media send delay 1-2 ms; `urgency_detected` on turn 3; final inbound delivery mean/max 1/110 ms. Only payload-safe `voice_timing` records were queried.

```bash
git show 33e73cfea2b953057c0f320ef33d9afd6239c4bd:app/services/gemini_pipeline.py \
  | rg -n -C 2 'MAX_RESPONSE_OUTPUT_TOKENS|max_output_tokens'
```

Result: the deployed SHA defines `MAX_RESPONSE_OUTPUT_TOKENS = 128` and passes it as `max_output_tokens`.

```bash
gh pr view 124 --repo delimatsuo/heykevin \
  --json number,title,state,isDraft,mergedAt,mergeCommit,url,statusCheckRollup
gh run view 29701191227 --repo delimatsuo/heykevin \
  --json databaseId,displayTitle,event,headSha,conclusion,status,url,jobs
```

Result: PR #124 is merged; PR test succeeded; manual workflow run succeeded for tests and staging; production remained skipped.

Historical validation recorded in the prior handoff, not rerun in this handoff turn:

- Focused receptionist test: 76 passed.
- Full unit suite: 804 passed.
- Ruff, `git diff --check`, payload-safe added-line secret scan, PR CI, and original staging workflow: passed.

## Verification

- Freshly passed: fetched current `origin/main`; created an isolated current-main worktree; confirmed its pre-handoff clean state; refreshed staging `/health`; confirmed the active deploy SHA contains the 128-token cap; refreshed PR #124 and workflow state.
- Not run: another test suite, because no code changed.
- Passed: response duration, first-audio latency, Twilio first-media delivery, and stable exact-SHA staging health.
- Failed: caller-heard completeness. The user reported that replies repeatedly cut off mid-phrase, including during the required validation call.
- Not run: no new code tests, because no post-validation tuning change was made.

## Risks And Watchouts

- High: another agent can supersede staging before or during the call. Read `/health` immediately before and after the test, and bind all log queries to the revision observed for that call window.
- High: 128 tokens truncates caller-heard phrases in the validated staging experience. Do not promote it or describe the experiment as successful.
- Medium: `response_first_audio` starts at the latest input transcript fragment, not at a precise caller end-of-speech boundary. Treat it as a consistent proxy and pair it with playout metrics.
- High privacy risk: full phone numbers, raw transcripts, OAuth callback codes, admin bearer tokens, Jobber tokens, and secrets must not appear in commands, logs, docs, or responses.
- Medium: current main includes unrelated controller changes. Preserve them; do not reset, revert, or clean other worktrees.

## Do Not Do

- Do not deploy production or run the production workflow without explicit current-session authorization.
- Do not implement another prompt rule as the next response-length fix.
- Do not change the Gemini model, VAD, or output pacing from the pre-cap trace.
- Do not query or repeat raw caller transcripts. Restrict Cloud Logging to `jsonPayload.message:"voice_timing"` and payload-safe timing messages.
- Do not print the staging phone number; ask the user to call the same existing staging number from Twilio.
- Do not work in or clean the dirty repo root or historical voice worktree.
- Do not promote or production-deploy the 128-token setting.
- Do not alter response-cap code in this validation worktree; create a fresh current-main implementation worktree for any approved follow-up.

## Next Recommended Steps

1. Treat the 128-token experiment as failed and preserve revision/call evidence. Do not request another 128-token call.
2. Before implementing a follow-up, fetch current `origin/main` and create a fresh isolated worktree. Do not code in this validation worktree or the historical merged worktree.
3. Propose a single reversible generation-config-only increase (a 192-token staging candidate is the natural midpoint between the failed 128 cap and the original 256 cap). Obtain exact-candidate review if the follow-up introduces anything beyond the one constant and its contract test.
4. Run focused receptionist tests, the full unit suite, Ruff on touched files, `git diff --check`, and the payload-safe added-line secret scan.
5. Deploy only to staging after normal review/CI authority. Verify the exact deployed SHA and repeat five concise turns plus one safety request. Acceptance still requires normal playout near four seconds or less and complete caller-heard phrases/safety guidance.
6. For any evidence refresh, query only payload-safe timing events from the exact staging revision:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision" AND resource.labels.service_name="kevin-api-staging" AND resource.labels.revision_name="<ACTIVE_REVISION>" AND jsonPayload.message:"voice_timing" AND jsonPayload.message:"event=response_first_audio"' \
  --project=kevin-491315 \
  --limit=50 \
  --order=desc \
  --format='value(timestamp,jsonPayload.message)'
```

7. Keep the separate Business to Personal mode fix isolated from all response-length work.

## Open Questions

- Which higher cap best preserves complete phrases while holding normal playout near four seconds: 192, or a return to 256 with a different non-prompt mechanism?
- Should the next candidate add payload-safe provider stop-reason telemetry so a future trace can distinguish token-limit termination from another model turn-completion cause?
