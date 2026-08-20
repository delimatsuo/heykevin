You are continuing Hey Kevin for Deli Matsuo (delimatsuo@gmail.com).

Current objective:
PR #165 rebase is done (`8713680`, CI green, draft, flags off). Standing
agent operating law is on PR #175 (`50d38aa`, CI green, **not merged**).
Merge #175 is blocked by **unresolved Codex review threads**
(`required_conversation_resolution` on `main`), not by failing CI and not
proven to need `--admin`. Next: fix the live P2, resolve both threads, merge
#175, then tenant-scoped `get_call_history` from a fresh worktree.

Workspace:
- Repo: `/Volumes/Extreme Pro/MYPROJECTS/Kevin` — **do not code here** (main
  `d3cd5c7`, 5 behind `origin/main`).
- Operating law: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/agent-operating`
  — `docs/agent-operating` @ `50d38aa`, PR #175. Work #175 here.
- Slice 2 memory: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory`
  — `codex/customer-memory` @ `8713680`, clean (+ untracked 2026-08-12 handoffs).
- Stale: `.worktrees/live-intake-controller` — do not touch.

Read first:
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/docs/handoffs/2026-08-17-pr175-conversation-block-handoff.md`
- After #175 merges: `docs/agent-operating.md` + `.cursor/rules/*.mdc`
- `git show origin/main:docs/superpowers/plans/2026-08-17-receptionist-wedge.md`
  (missing from stale primary checkout)
- `.worktrees/customer-memory/docs/handoffs/2026-08-12-customer-memory-pr165-handoff.md`
  (P0/P1 list; SHA rows stale)
- `.worktrees/customer-memory/docs/customer-memory-rollout.md` (if touching #165)
- `/Users/delimatsuo/.claude/projects/-Volumes-Extreme-Pro-myprojects-Kevin/memory/MEMORY.md`

Newest user request:
`/handoff` — preserve state; next session continues without re-asking.

Current state:
- **Done:** #165 rebased onto `9bc275c`; force-with-lease to
  `origin/codex/customer-memory`; focused 128 + full 2510 pytest passed;
  live intake + `build_greeting_text` both in tree.
- **Done:** Operating law adapted from ATS playbook; PR #175 opened;
  `50d38aa` review fixes; CI Test pass.
- **Diagnosed this session:** `gh pr merge 175 --merge` → “base branch
  policy prohibits the merge” because two Codex threads are unresolved:
  1. Live P2, `AGENTS.md:7` — portable worktree rule (do not hard-require
     `/Volumes/Extreme Pro/...`).
  2. Outdated P2, `docs/agent-operating.md` orig L144 — full
     `FORBIDDEN_ENV_NAMES` (partially fixed in `50d38aa`, still unresolved).
  Branch protection: `required_conversation_resolution=true`,
  `enforce_admins=true`, 0 required approvals, required check `Test`.
- **Not done:** merge #175 or #165; deploy; enable any memory flags.

Critical constraints:
- Work in worktrees only; `git fetch origin main`; branch from **origin/main**.
- Kopadze graph engineering (not LangGraph): fake-edge test, diamond, reduce
  in code, fresh-context reviewer, one report to parent. Skip the graph for
  this #175 docs loop.
- Autonomous push/PR/`gh pr merge --merge` when reviewed-clean **unless**
  owner-gated: Cloud Run deploy (staging or production), feature flags
  (`customer_memory_*`, `service_request_mutations_enabled`,
  `SERVICE_REQUEST_RECOVERY_ENABLED`, `jobber_lead_capture_enabled`,
  `PUBLIC_DEMO_ENABLED`), App Store/TestFlight, Twilio/A2P, credentials,
  real caller PII, opening `firestore.rules`, force-push main/staging.
- Merge to `main` does **not** deploy production.
- Do **not** lead with `gh pr merge --admin`. Resolve conversations first.
  If merge still fails after that, paste the exact command + error and stop.
- Do not enable memory flags on #165. Do not drop `_live_intake` from
  `gemini_pipeline.py`.
- Full pytest: `PATH="/Volumes/Extreme Pro/MYPROJECTS/Kevin/.venv/bin:$PATH"
  python -m pytest -q` — do **not** export `TWILIO_*` at process start.
- Stage explicit paths only; never `git add -A`, `--no-verify`, git config
  changes.
- Recommend, then execute. Do not ask Deli which slice.

Facts and evidence (refreshed 2026-08-17 19:19–19:25 EDT):
- `origin/main` = `9bc275c01071e5cf255237a533a6a40130558c09`
- PR #165 head `8713680`, draft, Test SUCCESS (run 32063044339)
- PR #175 head `50d38aa`, Test SUCCESS (run 32064385995), merge BLOCKED
- Production/staging health `deploy_sha=9bc275c`
- Public demo still `d3cd5c7` / `kevin-public-demo-00031-tgd` (separate GCP
  project `hk-public-demo-bos-260811`)

Next recommended action:
1. In `.worktrees/agent-operating`, make the worktree rule portable
   (`AGENTS.md`, `docs/agent-operating.md`, `.cursor/rules/agent-autonomy.mdc`).
   Keep the Mac path as a local example.
2. Push; wait for Test; resolve **both** PR #175 review threads.
3. `gh pr merge 175 --repo delimatsuo/heykevin --merge`
4. Then new worktree from `origin/main` for tenant-scoped `get_call_history`
   (P1; already on main; does not need #165). Leave #165 draft.

Verification expected:
```bash
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" worktree list
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" fetch origin main
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" log -1 --oneline origin/main
gh pr list --repo delimatsuo/heykevin --state open
gh api graphql -f query='query { repository(owner:"delimatsuo", name:"heykevin") { pullRequest(number:175) { mergeStateStatus reviewThreads(first:20) { nodes { isResolved isOutdated } } } } }'
```

Known risks:
- Accidental flag-true on #165 activates unready ANI-auth mutations (P0).
- Primary checkout stale; wedge plan is only on `origin/main`.
- `--admin` will not bypass `enforce_admins` + unresolved conversations.

If anything conflicts, the newest user request wins. Start by running:

```bash
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" worktree list
git -C "/Volumes/Extreme Pro/MYPROJECTS/Kevin" fetch origin main
gh pr list --repo delimatsuo/heykevin --state open
```
