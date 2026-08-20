You are continuing Hey Kevin for Deli Matsuo (delimatsuo@gmail.com).

Current objective:
PR #165 rebase is **done** (`8713680`, CI green, draft, flags off). Standing
agent operating law is on **PR #175** (`50d38aa`, CI green, **not merged** —
`gh pr merge` blocked by branch policy). Next: merge #175 if owner/UI allows,
then pick the highest-leverage unblocked wedge slice.

Workspace:
- Repo: `/Volumes/Extreme Pro/MYPROJECTS/Kevin` — **do not code here** (main
  `d3cd5c7`, 5 behind `origin/main`).
- Slice 2 memory: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory`
  — `codex/customer-memory` @ `8713680`, clean (+ untracked 2026-08-12 handoffs).
- Operating law: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/agent-operating`
  — `docs/agent-operating` @ `50d38aa`, PR #175.
- Stale: `.worktrees/live-intake-controller` — do not touch.

Read first:
- `docs/handoffs/2026-08-17-pr165-rebase-and-operating-law-handoff.md`
- After #175 merges: `docs/agent-operating.md` + `.cursor/rules/*.mdc`
- `docs/superpowers/plans/2026-08-17-receptionist-wedge.md` (on origin/main)
- `.worktrees/customer-memory/docs/customer-memory-rollout.md` (if touching #165)

Newest user request:
`/handoff` — preserve state; next session continues without re-asking.

Current state:
- **Done:** #165 rebased onto `9bc275c`; force-with-lease to
  `origin/codex/customer-memory`; focused 128 + full 2510 pytest passed;
  live intake + `build_greeting_text` both in tree; prod/staging on `9bc275c`.
- **Done:** Operating law adapted from ATS playbook; PR #175 opened; review
  fixes pushed; CI Test pass.
- **Blocked:** `gh pr merge 175 --merge` → “base branch policy prohibits the
  merge”; `--auto` → auto-merge not enabled on repo.
- **Not done:** merge #175 or #165; deploy; enable any memory flags.

Critical constraints:
- Work in worktrees only; `git fetch origin main`; branch from **origin/main**.
- Kopadze graph engineering (not LangGraph): fake-edge test, diamond, reduce
  in code, fresh-context reviewer, one report to parent.
- Autonomous push/PR/`gh pr merge --merge` when reviewed-clean **unless**
  owner-gated: Cloud Run deploy (staging or production), feature flags
  (`customer_memory_*`, `service_request_mutations_enabled`,
  `SERVICE_REQUEST_RECOVERY_ENABLED`, `jobber_lead_capture_enabled`,
  `PUBLIC_DEMO_ENABLED`), App Store/TestFlight, Twilio/A2P, credentials,
  real caller PII, opening `firestore.rules`, force-push main/staging.
- Merge to `main` does **not** deploy production.
- Do not enable memory flags on #165. Do not drop `_live_intake` from
  `gemini_pipeline.py`.
- Full pytest: `PATH="/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin:$PATH"
  python -m pytest -q` — do **not** export `TWILIO_*` at process start.
- Stage explicit paths only; never `git add -A`, `--no-verify`, git config
  changes.

Facts and evidence:
- `origin/main` = `9bc275c01071e5cf255237a533a6a40130558c09`
- PR #165 head `8713680`, draft, Test SUCCESS (run 32063044339)
- PR #175 head `50d38aa`, Test SUCCESS (run 32064385995), merge BLOCKED
- Production/staging health `deploy_sha=9bc275c`

Next recommended action:
1. `gh pr view 175 --repo delimatsuo/heykevin` — try UI merge or
   `gh pr merge 175 --repo delimatsuo/heykevin --merge --admin` if authorized.
2. If #175 merges, `git fetch origin main` in a worktree; continue wedge work
   (fake-edge: tenant-scoped call history P1, or Slice 3 Confirm UI).
3. Leave #165 draft until owner asks to mark ready/merge.

Verification expected:
```bash
git worktree list
git fetch origin main && git log -1 --oneline origin/main
gh pr list --repo delimatsuo/heykevin --state open
```

Known risks:
- Accidental flag-true on #165 activates unready ANI-auth mutations (P0).
- Primary checkout stale; wrong tree loses live intake or operating law files.

If anything conflicts, the newest user request wins. Start by running:

```bash
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" worktree list
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" fetch origin main
gh pr list --repo delimatsuo/heykevin --state open
```
