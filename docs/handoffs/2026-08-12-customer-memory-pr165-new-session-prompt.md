You are continuing Hey Kevin's returning-customer continuity work.

Current objective:
Keep returning-customer continuity in the normal receptionist product, not a demo
fork. A confirmed returning caller should be greeted naturally and then use normal
cancel/reschedule/add-service flows. Preserve the default-off safety boundary while
continuing the next tenant-isolation repair.

Workspace:

- Repo root: `/Volumes/Extreme Pro/MYPROJECTS/Kevin`
- Required worktree: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory`
- Branch/upstream: `codex/customer-memory` / `origin/codex/customer-memory`
- Draft PR: https://github.com/delimatsuo/heykevin/pull/165
- Important docs to read first:
  - `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/AGENTS.md`
  - `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/docs/handoffs/2026-08-12-customer-memory-pr165-handoff.md`
  - `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/docs/customer-memory-rollout.md`

Newest user request:
Continue from the durable handoff. If a newer user message changes scope, the newest
user request wins.

Current state:

- Exact feature head and upstream at handoff creation:
  `4f99e8bbcdae77081786798990e1f6d922158c68`.
- Base/merge base at handoff creation:
  `d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`.
- PR #165 is open, draft, mergeable, and `CLEAN` on exact head `4f99e8b`.
- GitHub Actions run 31632664524 passed the `Test` job on that exact head; staging
  and production jobs were skipped.
- Fresh local full suite: `2268 passed, 21 warnings in 36.10s`.
- Independent post-fix review approved the exact tenant-isolation diff for
  default-off merge/deploy eligibility after CI. Production activation was not
  approved.
- Product memory, shared greetings, typed service requests, provider create/
  mutation sagas, recovery leases, execution provenance, and protected feature
  gates are implemented.
- Legacy global contacts/caller contacts and unproven tenant caller contacts are
  quarantined from runtime reads.
- No code edit is in progress. The two `2026-08-12-customer-memory-pr165-*` handoff
  files are intentionally uncommitted unless the user authorizes publication.

Critical constraints:

- Do not revert user or other-agent changes.
- Do not work in the primary worktree; it contains unrelated untracked
  visual-diagnosis documents. Use only the customer-memory worktree.
- Do not reset, clean, broad-stage, or rewrite history.
- Do not deploy, configure Firestore, mutate a real provider, or enable flags.
- Keep PR #165 draft. Default-off source readiness is not activation readiness.
- Do not create a separate demo pipeline or fake memory. Do not repeatedly tell a
  known returning caller that the call is a demo.
- Never use top-level global contacts/caller contacts as tenant data.
- Provider success may be spoken only after provider confirmation and canonical
  finalization. Pending and `needs_review` proposals must remain durable.
- Any code change expires the exact-tree review and test claim; requalify it.

Facts and evidence:

- Fresh command:

  ```bash
  PATH="$PWD/.venv/bin:$PATH" \
  TWILIO_ACCOUNT_SID=test \
  TWILIO_AUTH_TOKEN=test \
  TWILIO_PHONE_NUMBER=+15555550100 \
  TELEGRAM_BOT_TOKEN=test \
  USER_PHONE=+15555550101 \
  python -m pytest -q
  ```

  Result: `2268 passed, 21 warnings in 36.10s`.

- `app/webhooks/twilio_incoming.py` currently calls
  `get_call_history(caller_phone, limit=10)`.
- `app/db/calls.py:get_call_history` filters only `caller_phone`, not
  `contractor_id`. Therefore tenant A's call outcomes can influence tenant B's trust
  score and route. This is the next bounded P1 repair.
- Activation remains blocked by caller verification for destructive actions,
  reschedule concurrency/preconditions, weak name-capture confirmation, recursive
  deletion/disclosure, Firestore indexes/TTL, isolated staging fault tests, and an
  operator procedure for retained `needs_review` proposals.

Next recommended action:

1. Inventory every `get_call_history(` call and its tests. Design the smallest
   tenant-explicit API: inbound routing must supply `contractor_id`, and the
   Firestore query must filter both tenant and caller. Retain a separately named
   global/admin lookup only if a verified call site requires it.
2. Add regression tests proving identical caller numbers cannot transfer history,
   trust score, or routing outcomes across tenants, and that unresolved tenant
   ingress performs no global history lookup.
3. Run focused routing tests and the full suite. Use an independent staff-engineer
   review for the multi-tenant routing change. Do not activate or deploy.
4. After that repair, prepare a staff-reviewed design for caller authorization and
   provider-safe reschedule preconditions before implementing destructive behavior.

Verification expected:

- Focused DB and Twilio routing tests for cross-tenant negative cases.
- Full local Python suite with the test environment shown above.
- Ruff/fatal/undefined checks for touched files, `git diff --check`, and fresh exact-
  tree staff review.
- Refresh PR/CI evidence if the branch is pushed.

Known risks:

- A naive signature change can break other history consumers; inventory call sites
  before editing.
- Adding a second Firestore equality filter plus ordering may require a composite
  index. Determine the exact query/index requirement and document it without
  provisioning production.
- Destructive provider actions are not safe to activate based on ANI alone.
- Blind reschedule replay can overwrite manual/concurrent Calendar changes.
- Account deactivation is not recursive data deletion.

If anything conflicts, the newest user request wins. Start by running:

```bash
cd "/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory"
git status --short --branch
git rev-parse HEAD
git rev-parse '@{u}'
git rev-parse origin/main
gh pr view 165 --json state,isDraft,mergeable,mergeStateStatus,headRefOid,baseRefOid,statusCheckRollup,url
rg -n "get_call_history\\(" app tests
```
