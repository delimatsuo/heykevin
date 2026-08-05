You are continuing Hey Kevin's staging validation for the Gemini Live response-length cap.

Current objective:
Validate the exact 192-token Gemini Live candidate now deployed to staging after the 128-token experiment failed with repeated mid-phrase cutoffs. Do not merge PR #127 or make any production decision until a five-turn plus safety call proves complete caller-heard phrases without restoring the former 6-7 second response tail.

Workspace:
- Repo root: `/Volumes/Extreme Pro/myprojects/Kevin` (dirty and diverged; read-only)
- Clean validation worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-response-length-staging-validation`
- Branch: `codex/voice-response-length-staging-validation`, created from current `origin/main`
- Do not continue coding in `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-session-latency-eval`; it is the historical merged cap branch and is behind main.
- Important docs to read first:
  - `/Volumes/Extreme Pro/myprojects/Kevin/AGENTS.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-response-length-staging-validation/docs/handoffs/2026-07-19-voice-response-length-staging-validation-handoff.md`

Newest user request:
The user required the cutoff to be fixed. The 192-token candidate is ready for the user's repeat staging call. The separately requested Personal-mode fix is merged, and TestFlight `1.2.5 (25)` uploaded successfully and reached Apple status `VALID`.

Current state:
- PR #124 (`fix: cap live receptionist response length`) merged at `0b8de0be6ca07c67f18949ded7c26006bdf3aa75`.
- The failed 128-token baseline remains in merged main history. Draft PR #127 changes only the active cap and its test assertion to 192.
- Current `origin/main` is `6dc3013df78070cd60871febb1a541977ea4c3b3`, which includes merged Personal-mode PR #128 and build metadata PR #129.
- Staging health before and after the 2026-07-20 caller test: revision `kevin-api-staging-00095-bav`, deploy SHA `33e73cfea2b953057c0f320ef33d9afd6239c4bd`.
- That exact deployed SHA was inspected and contains the 128-token cap.
- Post-cap call `CAe19e3f` completed with five responses and one `urgency_detected` safety-path event.
- Playout was 3.165 s, 2.607 s, 3.310 s, 3.570 s, and 3.167 s. Non-greeting first audio was 942 ms, 911 ms, 805 ms, and 1,012 ms. Twilio first-media delay was 1-2 ms; inbound delivery mean/max was 1/110 ms.
- The user heard repeated mid-phrase cutoffs. The duration/latency criteria passed, but the required caller-heard completeness criterion failed.
- Generated output drained to Twilio with no logged backlog overflow or response interruption on turns 2-5. The cutoff is upstream at model generation/turn completion and is consistent with the 128-token experiment; an explicit provider finish reason was not logged.
- The validation worktree should have only two untracked handoff files; no implementation change is in progress.
- Exact 192 candidate: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-response-length-192-recovery`, branch `codex/voice-response-length-192-recovery`, SHA `2614dab2bfdf468ac78734f2877c52d3901104d7`, draft PR #127. It passed 76 focused tests, 842 unit tests, Ruff, diff integrity, secret scan, PR CI, and independent staff review.
- Staging is healthy on revision `kevin-api-staging-00097-tic`, deploy SHA `2614dab2bfdf468ac78734f2877c52d3901104d7`. Guarded run `29753133818` succeeded and production was skipped.
- Personal-mode PR #128 merged at `e653db39d0c2cf22c229c39e68bc214dc95ff184`. Build 24 still lacks the fix.
- TestFlight source: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/ios-build-25`, branch `codex/ios-build-25`, SHA `95edb21e2b7e49894576a35691243ee828f69b95`, version `1.2.5 (25)`. The exported IPA passed production/signing checks and Apple validation; upload succeeded, processing reached `VALID`, and PR #129 merged at `6dc3013df78070cd60871febb1a541977ea4c3b3`. No App Store release was submitted.

Critical constraints:
- Do not deploy production unless the user explicitly instructs it in the current session.
- Do not add more ad hoc prompt rules as the next fix.
- Do not change Gemini model, VAD, or output pacing from the existing evidence alone.
- Do not expose full phone numbers, raw transcripts, OAuth callback codes, admin bearer tokens, Jobber tokens, or secrets.
- Use payload-safe timing logs only; restrict queries to the active staging revision and `jsonPayload.message:"voice_timing"`.
- Preserve unrelated controller changes and do not clean the dirty root, the historical voice worktree, or any other worktree.
- Do not merge PR #127 or deploy the voice change to production before successful caller-heard validation.
- Do not alter the exact 192 candidate during the test window; if it fails, restore recorded staging revision `kevin-api-staging-00095-bav` / SHA `33e73cfea2b953057c0f320ef33d9afd6239c4bd` and investigate before another tuning change.

Facts and evidence:
- Before the cap, one five-turn call had provider first audio of 868, 922, 942, and 972 ms; Twilio first-media send followed provider audio by 1 ms; ingress max was 177 ms. No meaningful monotonic startup degradation was observed.
- The same call had output playout durations of 7.120 s and 6.828 s for two normal replies, then 3.044 s and 3.313 s. Long generated replies were the actionable issue.
- An independent staff review approved only a reversible 128-token staging experiment and required five-turn plus safety validation.
- Historical validation passed: 76 focused receptionist tests, 804 unit tests, Ruff, `git diff --check`, payload-safe added-line secret scan, PR CI, and the original staging workflow. No tests were rerun while preparing this handoff because no code changed.
- Current health was read from `https://kevin-api-staging-l63rergg7a-uc.a.run.app/health` before and after the call and remained stable. Reverify before any future staging work.
- Only payload-safe `voice_timing` logs were queried; no raw caller transcript was read or repeated.

Next recommended action:
1. Wait for the user to place the requested staging call and report whether any phrase cuts off and whether safety guidance is complete.
2. Query only payload-safe `voice_timing` events for revision `kevin-api-staging-00097-tic`, identify the newest redacted call label, and analyze the five response turns.
3. Re-read staging `/health` after the call. If the candidate fails, restore the recorded rollback revision; if it passes, mark PR #127 ready and merge only after confirming exact checks.
4. Ask the user to install TestFlight `1.2.5 (25)` when it appears, without uninstalling or resetting, then verify Personal selection persists after relaunch. Do not submit an App Store release or widen distribution.

Verification expected:
- Read `https://kevin-api-staging-l63rergg7a-uc.a.run.app/health` immediately before and after the caller test.
- Use `gcloud logging read` filters restricted to the call's active staging revision and `jsonPayload.message:"voice_timing"`; never query raw transcript content.
- Review `response_first_audio`, `response_first_twilio_media_sent`, `twilio_playback_mark_resolved`, `response_playout_drained`, `model_usage`, and `inbound_media_delivery_summary` for the chosen redacted call label.
- If code changes are warranted later: focused tests, existing receptionist tests, full unit suite, Ruff on touched files, diff integrity, payload-safe secret scan, PR CI, staging exact-SHA health verification, then caller validation.

Known risks:
- Staging can be superseded by another agent's deployment during a future test window.
- Raising the cap could restore long 6-7 second replies; keeping it too low can continue truncating safety guidance.
- `response_first_audio` begins at a transcript fragment, so it is not a precise caller-end-of-speech measurement.
- Do not mix the mode-switch agent's changes into the response-length candidate.

If anything conflicts, the newest user request wins. Start by running:

```bash
git status --short --branch
```
