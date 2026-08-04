You are continuing Hey Kevin's staging validation for the Gemini Live response-length cap.

Current objective:
Validate whether reducing the Live response output limit from 256 to 128 tokens shortens caller-heard normal replies without truncating safety guidance. Do not make a further tuning change until the current staging trace proves its need.

Workspace:
- Do new implementation work in a fresh worktree from `origin/main` in `/Volumes/Extreme Pro/myprojects/Kevin`.
- Do not work in the dirty repository root `/Volumes/Extreme Pro/myprojects/Kevin`.
- Do not continue coding on the historical worktree `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-session-latency-eval`; it is on merged branch `codex/voice-response-length-cap` and behind main.
- Important docs to read first:
  - `/Volumes/Extreme Pro/myprojects/Kevin/AGENTS.md`
  - `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-session-latency-eval/docs/handoffs/2026-07-19-voice-response-length-staging-handoff.md`

Newest user request:
Prepare a handoff. After resumption, newest user instructions supersede this prompt.

Current state:
- PR #124 (`fix: cap live receptionist response length`) merged at `0b8de0be6ca07c67f18949ded7c26006bdf3aa75`.
- The live cap is `MAX_RESPONSE_OUTPUT_TOKENS = 128` in `app/services/gemini_pipeline.py`.
- Original cap candidate `d61f67a464b7b3e6ad9eeda01d049fc0df6946c0` passed PR CI and was staged as revision `00093-rim`.
- Current main at handoff is `33e73cfea2b953057c0f320ef33d9afd6239c4bd`; it includes the cap plus later offline-controller PRs #125 and #126.
- Current staging health at handoff: revision `kevin-api-staging-00095-bav`, deploy SHA `33e73cfea2b953057c0f320ef33d9afd6239c4bd`.
- No post-cap caller test was completed. Verify live state again because staging may have changed.

Critical constraints:
- Do not deploy production unless the user explicitly instructs it in the current session.
- Do not add more ad hoc prompt rules as the next fix.
- Do not change Gemini model, VAD, or output pacing from the existing evidence alone.
- Do not expose full phone numbers, OAuth callback codes, admin bearer tokens, Jobber tokens, or secrets.
- Use payload-safe timing logs only; do not query or repeat raw caller transcripts.
- Preserve unrelated controller changes and do not clean other worktrees.

Facts and evidence:
- Before the cap, one five-turn call had provider first audio of 868, 922, 942, and 972 ms; Twilio first-media send was 1 ms; ingress max was 177 ms. No meaningful monotonic startup degradation was observed.
- That call had output playout durations of 7.120 s and 6.828 s for two normal replies, then 3.044 s and 3.313 s. Long generated replies were the actionable issue.
- An independent staff review approved only a reversible 128-token staging cap experiment and required five-turn plus safety validation.
- Validation already passed: `76` focused receptionist tests, `804` unit tests, Ruff, `git diff --check`, payload-safe added-line secret scan, PR CI, and the original staging workflow.

Next recommended action:
1. Create or use a clean fresh worktree from current `origin/main`, then run `git status --short --branch`, fetch, and read staging `/health`.
2. Confirm the deployed SHA contains `MAX_RESPONSE_OUTPUT_TOKENS = 128`; do not rely on the handoff revision name.
3. Ask the user to call the same staging number from Twilio, make five concise turns, then make one plumbing safety request. Do not print the full number.
4. Once the user says `done`, query Cloud Logging by the active revision for `response_first_audio`, find the newest call label, then read only the payload-safe timing events for that label.
5. Report whether normal response playout is roughly four seconds or less, first audio remains near one second, and safety guidance was complete. Do not claim a caller-experience success without both evidence and the user's feedback.

Verification expected:
- Read `https://kevin-api-staging-l63rergg7a-uc.a.run.app/health` before and after the test.
- Use `gcloud logging read` filters restricted to the active staging revision and `jsonPayload.message:"voice_timing"`.
- If code changes are warranted later: focused tests, existing receptionist tests, full unit suite, Ruff on touched files, diff integrity, payload-safe secret scan, PR CI, staging exact-SHA health verification, then caller validation.

Known risks:
- Staging can be superseded by another agent's deploy.
- The 128-token cap could fail to reduce audio duration or could truncate safety guidance.
- `response_first_audio` begins at a transcript fragment, so it is not a caller-end-of-speech measurement.

If anything conflicts, the newest user request wins. Start by running:

```bash
git status --short --branch
```
