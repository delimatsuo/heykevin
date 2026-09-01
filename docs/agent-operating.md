# Hey Kevin — Agent operating law

Source prompt: Ella ATS operating instructions, adapted to this repo on 2026-08-17.
Do not copy ATS criteria, worktree paths, or merge targets from that project.

You are picking up **Hey Kevin** for Deli Matsuo (delimatsuo@gmail.com). Operate
autonomously. Do not bounce engineering, implementation-order, or “should I
push?” questions back. Push, open PRs, and `gh pr merge --merge` yourself when
a slice is reviewed-clean and is not owner-gated below. Come back only for
things you cannot do.

## Owner-gated (stop and ask)

- Cloud / spend / credentials (GCP `kevin-491315`, Secret Manager, Twilio,
  Gemini, Deepgram, ElevenLabs, APNs, App Store keys).
- Production deploy: `gh workflow run deploy.yml -f target=production --ref main`.
- Staging deploy: push to `staging` or `workflow_dispatch` target=staging
  (staging is live Cloud Run with telephony).
- Enabling flags: `customer_memory_capture_enabled`,
  `customer_memory_personalization_enabled`,
  `service_request_mutations_enabled`, `SERVICE_REQUEST_RECOVERY_ENABLED`,
  `jobber_lead_capture_enabled`, `PUBLIC_DEMO_ENABLED`.
- App Store / TestFlight upload.
- Twilio number purchase or release; A2P / caller-facing SMS.
- Force-push `main` or `staging`.
- Real caller PII, recordings, or live production calls on customer numbers
  (the owner may live-test; you do not).
- Opening `firestore.rules` or `database.rules.json` to client access.
- A GitHub permission deny on merge — paste the exact command, only then.

Merging a reviewed-clean, default-off PR to `main` is allowed: **push to `main`
does not deploy.** Activating behavior in production still needs an explicit
owner deploy plus any flag change.

## Workspace (standing trap)

Work in a git worktree of the **current clone**, never the primary checkout.
The primary checkout is often behind `origin/main`; do not trust files there.

Discover existing trees with `git worktree list` from the clone root, or
create one:

```bash
git fetch origin main
git worktree add -b <branch> .worktrees/<name> origin/main
```

Active trees belong under `<clone>/.worktrees/`. Pin the resolved absolute
worktree path in every subagent prompt once you have it. One git-mutation
owner per worktree. Never two agents commit on the same checkout.

On Deli's Mac this clone is `/Volumes/Extreme Pro/MYPROJECTS/Kevin` and
worktrees are `.worktrees/` under that path. Treat that as a local example,
not a gate. Cloud, CI, and other clones (for example `/workspace/heykevin`)
must use their own clone root and still follow the worktree rule.

Always `git fetch origin main` and cut branches from **origin/main**.

Read first: worktree `AGENTS.md`, this file, and the active plan under
`docs/superpowers/plans/`. Quote success criteria; don’t paraphrase. Evidence
ladder, never inflate: pytest that ran < owner live-test < production
`deploy_sha`. Fixture / mock LLM ≠ production Gemini Live behavior.
No public-demo iteration unless a live demo regression is reported.

`firestore.rules` is deny-all client access by design (SECURITY_AUDIT F-26).
No change without an explicit security reason and tests. Do not open client
reads/writes.

Repo: `https://github.com/delimatsuo/heykevin`.

## Graph engineering (Kopadze — this is the method, not LangGraph)

Source of truth: Anatoli Kopadze, “Graph Engineering explained”
https://x.com/AnatoliKopadze/status/2080668775796314331

A graph is a **plan of jobs**: which jobs exist, and which job waits on which.
It is not a supervisor chat, not LangGraph `StateGraph`, not shared `AgentState`.

- **Node** = one agent, one bounded job, one typed IN, one schema’d OUT.
  Free-text handoffs are a failed graph (a human has to sit in the middle).
- **Edge** exists only if data actually moves. Fake-edge test: “does this step
  *read* the previous result?” If no, delete the wait and run in parallel. If
  two nodes write the same file or share a rate-limited API, that *is* an
  edge — serialize or isolate with **separate worktrees**.
- **Diamond (default topology):** split → fan out independent workers →
  **reduce in code** (dedupe/flatten/filter, zero tokens) → **verifier on a
  fresh context** → one synthesizer. Stop asking “more steps.” Ask “where is
  the split, where is the merge.”
- **Worker never grades itself.** Verifier sees artifact + rubric, not the
  worker’s chat. Check a real signal (tests that *did* pass), not “the agent
  said done.”
- **A graph of agents sharing one parent chat is a loop in a costume.**
  Intermediates stay in the worker; you return **one report**. Parent context
  stays empty.
- **Anchors sit outside the graph:** tests that ran, frozen rules
  (`firestore.rules`, `AGENTS.md`, PROTECTED flag names). Topology among
  agreeing agents is not truth.
- **Cap.** First run is scoped. Count expected vs returned at every merge;
  never synthesize a partial set. Layer fan-in (batch → summarize batches →
  synthesize summaries). Discovery loops: two empty rounds *and* a hard agent
  cap, then stop.
- **Skip the graph** when the fake-edge test finds no independent jobs (one
  function, one bug, true chain). That is a **loop** (try / check / adjust).
  A loop is fine.
- **A graph costs more than a chat.** Coordination got cheaper; workers still
  burn tokens. Do not fan out for theater.
- Human (Deli) is the last yes only on irreversible/owner-gated items above.
  You are the last yes on everything else.

## Model routing (complexity of the *node*, not the session)

Use Cursor `Task` subagents with an explicit `model`. Do not default every
node to the parent model. If a model 429s / “Other Models usage limit”,
switch; do not stall.

| Node type | Model |
|---|---|
| Mechanical transcription, greps, “does this file exist”, fixture mapping | `composer-2.5-fast` or `cursor-grok-4.5-high-fast` |
| Standard implementation, plans, tests, most PRs | `cursor-grok-4.6-xhigh` or `claude-sonnet-5-thinking-high` |
| Whole-branch / defect-first review, security/PII, architectural knots | `claude-opus-5-thinking-high` (fallback `claude-opus-4-8-thinking-high`, then `cursor-grok-4.6-xhigh`) |
| Broad cheap research / web fetch | `claude-sonnet-5-thinking-high` or `cursor-grok-4.6-xhigh` — not opus |

Available slugs (do not invent others): `inherit`,
`claude-opus-4-8-thinking-high`, `claude-opus-5-thinking-high`,
`claude-sonnet-5-thinking-high`, `composer-2.5-fast`,
`cursor-grok-4.5-high-fast`, `cursor-grok-4.6-xhigh`,
`gemini-3.7-flash-high`, `gpt-5.5-medium`, `gpt-5.6-sol-medium`,
`gpt-5.6-terra-medium`.

Prefer `cursor-grok-4.6-xhigh` for implementation if opus/sonnet fail.

## How to run (every slice)

1. Draw the diamond on paper in the prompt: nodes, contracts (IN/OUT), edges.
   If zero independent jobs, run a loop yourself.
2. Fan out only file-disjoint worktrees. **One git-mutation owner per
   worktree.** Never two agents commit on the same checkout.
3. Reduce in code (you merge SHAs/test counts; do not spawn an LLM to
   “combine”).
4. Fresh-context reviewer on `origin/main...HEAD` before merge. Worker ≠
   reviewer.
5. `git push -u origin HEAD` → `gh pr create --repo delimatsuo/heykevin` →
   `gh pr merge --merge` when reviewed-clean and not owner-gated. If merge is
   permission-blocked, paste the exact command — only then.
6. Stage explicit paths. Never `git add -A`. Never `--no-verify`. Never update
   git config. No branch may be force-pushed; rebasing is allowed only before the
   first publication; after a branch is published, update it by merging the
   latest target branch into the feature branch and pushing normally; if history
   replacement is truly needed, create a new branch and replacement PR instead
   of rewriting the published branch.
7. Required checks before “done”. From the current clone or worktree, put
   that clone’s `.venv/bin` on PATH (Deli’s Mac example:
   `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin`):
   ```bash
   PATH="<clone>/.venv/bin:$PATH" python -m pytest -q
   ```
   Do **not** export any name in `FORBIDDEN_ENV_NAMES` from
   `tests/unit/test_visual_diagnosis_contracts.py` (same set in
   `tests/unit/test_visual_diagnosis_state.py`) at process start. That set
   includes `TWILIO_*`, `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `USER_PHONE`,
   and dozens of other credential/config keys. Those tests snapshot
   `os.environ` and fail collection if any listed name is present. Prefer a
   clean shell over listing every key. iOS: `cd ios && xcodegen generate` if
   `ios/project.yml` changed, then that app’s build if you touched Swift.
8. Mutation-check new guards (would the test still pass if the guard were
   deleted?).
9. Do not mark a wedge slice done unless the quoted plan row is satisfied.
   Default-off source on `main` ≠ activation. Owner live-test ≠ “unit tests
   proved live audio.”

## Product position (verify; don’t trust)

Snapshot 2026-08-17. Re-check `gh pr list --repo delimatsuo/heykevin` and
`git fetch origin main` before acting.

- Slice 1 live Gemini intake is on `origin/main` (PR #174) and in production.
  Do not reopen intake unless a live call skips the job and jumps to the form.
- Slice 2 returning-customer memory is draft PR #165, flags default-off. Merge
  eligibility ≠ activation. P0 blockers (bare ANI auth, Google reschedule
  concurrency) still block turning flags on.
- Slice 3 owner Confirm UI and Slice 4 hang-up caller SMS are later; SMS is
  A2P-blocked.
- Public demo (`kevin-public-demo`) is not the production receptionist path.
  Do not iterate demo greeting/legal/weekday UX unless a live demo regression
  is reported.
- Do not reintroduce global contact fallback.

## First action (new sessions)

Fetch `origin/main`, `gh pr list --repo delimatsuo/heykevin`, list
`.worktrees`. Continue the highest-leverage *unblocked* engineering node
(fake-edge test). Do not ask Deli which slice to pick.
