# Returning Customer Continuity / PR #165 Handoff

Created: 2026-08-12 15:55 EDT

Prepared by: Codex

## Objective

Build returning-customer continuity as part of Hey Kevin's normal receptionist
service, not as a separate demo pipeline or fake demo memory. A confirmed returning
caller should receive a natural greeting such as "Hello, Jonathan. How can I help
you today?" and then use the normal product flow to cancel, reschedule, or add a
service to an existing appointment. The current branch contains the default-off
product architecture and safety work; production activation remains explicitly
blocked.

## Current State

- Repo root: `/Volumes/Extreme Pro/MYPROJECTS/Kevin`
- Feature worktree: `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory`
- Branch/upstream: `codex/customer-memory` / `origin/codex/customer-memory`
- Base and merge base at handoff creation: `origin/main` at
  `d2a2f003134a66b35cd76cabb8c2aaa43ca184f5`
- Latest local and remote commit:
  `4f99e8bbcdae77081786798990e1f6d922158c68` — `Block cross-tenant contact fallback`
- Previous feature commit:
  `5a61f206645ccabfc80b1ce4dd2126c14d3c5169` — `Add durable returning-customer continuity`
- Draft PR: [#165 — Add durable returning-customer continuity](https://github.com/delimatsuo/heykevin/pull/165)
- PR state at 2026-08-12 15:55 EDT: open, draft, mergeable, merge state
  `CLEAN`, head `4f99e8bbcdae77081786798990e1f6d922158c68`.
- Feature diff from `origin/main`: 49 files, 13,609 insertions, 410 deletions.
- Dirty state before this handoff: clean. The only intended dirty state after this
  handoff is the two untracked files under `docs/handoffs/` named in this document.
- The primary worktree `/Volumes/Extreme Pro/MYPROJECTS/Kevin` is on a different
  local `main` state, seven commits behind `origin/main`, with unrelated untracked
  visual-diagnosis documents. Do not modify, stage, delete, or move those files.
- All review subagents are complete. The final independent tenant-isolation review
  approved the exact `4f99e8b` diff for commit/push and default-off merge/deploy
  eligibility after CI; it did not approve production activation.

## Newest User Request

Create a durable handoff and paste-ready prompt so another agent can continue the
returning-customer feature without losing the product decisions, exact-tree
evidence, or activation blockers. If future instructions conflict, the newest user
request wins.

## Completed Work

- Replaced the proposed demo-only continuity with product-owned, tenant-scoped
  customer memory under
  `contractors/{contractor_id}/customer_memory/{sha256(normalized_e164)}`.
- Added a shared normal-product greeting context for Gemini, ConversationRelay,
  and the legacy voice path. A returning caller is not repeatedly told that the
  call is a demo.
- Added typed, durable service requests for cancel, reschedule, and add-service
  commands. Appointment continuity is independent from name personalization.
- Added explicit `local` versus `provider` execution provenance. Provider-backed
  requests require a provider binding and a finalized create receipt; missing or
  corrupt provenance fails closed instead of silently becoming a local mutation.
- Added prepare/provider/finalize sagas for Google Calendar create and mutation
  operations. Stable logical operation IDs, command receipts, revision checks, and
  replay handling prevent model/tool transport IDs from becoming product identity.
- Added bounded provider recovery with transactional leases, exponential backoff,
  CAS finalization, and retained `needs_review` state after eight attempts.
  Pending/uncertain provider records are not TTL-deleted.
- Changed Google add-service handling to update only Hey Kevin-owned private event
  metadata through GET/merge/ETag checks. It does not overwrite the event schedule,
  title, or description.
- Added four protected/default-closed rollout controls:
  - `customer_memory_capture_enabled`
  - `customer_memory_personalization_enabled`
  - `service_request_mutations_enabled`
  - environment gate `SERVICE_REQUEST_RECOVERY_ENABLED`
- Required provider create dispatch to have the global recovery gate literally
  true, the tenant mutation flag literally true, an approved integration, and a
  Google token. Missing, false, integer `1`, and string `"true"` remain closed.
- Removed both tenant-to-global contact read-throughs. Top-level legacy `contacts`
  and `caller_contacts` are quarantined from tenant runtime reads.
- Required existing tenant `caller_contacts` to carry exact provenance:
  `provenance_schema == 1`, `provenance_source == "tenant_post_call"`, and a
  matching `provenance_contractor_id`. An unproven record is replaced by a fresh
  tenant-bound post-call write rather than merged and accidentally legitimized.
- Converted `scripts/migrate_caller_contacts.py` to aggregate inventory only. Its
  former cross-tenant copy path is gone, `--apply` fails before Firestore access,
  and dry-run output contains no document IDs or caller PII.
- Added regression coverage for tenant isolation across the REST API, Twilio
  ingress, Relay greeting, Gemini/media setup, call/RTDB state, post-call writes,
  provider recovery, and calendar mutations.
- Pushed both feature commits and updated draft PR #165 with the exact head SHA,
  safety boundary, verification evidence, and activation blockers.

## In Progress

- No code edit is currently in progress.
- Draft PR #165 is not merged and must stay draft while activation blockers remain.
- The feature is not deployed, configured, or activated in staging or production.
- These two handoff files are intentionally uncommitted transfer artifacts unless
  the user explicitly authorizes publishing them.

## Important Decisions

- **Normal product, not demo fork:** customer identity and appointment continuity
  belong to the same receptionist service used by ordinary calls. Public-demo
  ingress may reuse only sandbox adapters and must not own a separate memory model
  or production credentials.
- **No repetitive demo copy:** after a caller returns, Kevin should greet and serve
  the caller normally. The caller already knows the number is a demo when that
  context applies.
- **Identity and appointment state are separate:** a service request can be found
  from tenant plus hashed normalized caller number without fabricating a
  customer-memory identity card.
- **Provider truth before spoken success:** Kevin may claim a mutation succeeded
  only after provider confirmation and canonical Firestore finalization.
- **Recovery is durable:** once a provider intent is admitted, disabling a feature
  flag must not strand it. Recovery continues with the persisted logical operation
  ID until finalization or explicit operator review.
- **Unknown provider state is retained:** `pending` and `needs_review` records omit
  top-level `expires_at`; TTL must not erase unresolved external side effects.
- **Activation gates are independent:** capture, personalization, tenant mutations,
  and global recovery are separate protected controls and are absent/false by
  default.
- **Legacy data is quarantined, not guessed:** phone-number equality does not prove
  tenant provenance. Global or unmarked records must never become spoken names,
  routing inputs, call-state fields, or API results.

## Files And Artifacts

- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/AGENTS.md`:
  repository rules, architecture, deployment paths, and multi-tenant invariants.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/docs/customer-memory-rollout.md`:
  canonical feature flags, Firestore index/TTL requirements, recovery contract,
  staging qualification, and privacy boundary.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/app/services/customer_memory.py`:
  immutable customer-memory domain model, source-ranked confirmation, revisioning,
  retention, and idempotent transitions.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/app/db/customer_memory.py`:
  tenant-scoped Firestore customer-memory adapter and durable command receipts.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/app/services/receptionist_context.py`:
  trusted greeting/name projection shared by voice engines.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/app/services/service_request.py`:
  typed service-request aggregate and mutation rules.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/app/services/service_request_repository.py`:
  repository contracts, provider prepare/finalize/recovery semantics, execution
  provenance, and command service.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/app/db/service_requests.py`:
  Firestore persistence, hidden pending envelopes, leases, receipts, and atomic
  finalization.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/app/services/google_calendar_request_provider.py`:
  Google provider adapter for create/cancel/reschedule/add-service.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/app/services/service_request_recovery.py`:
  bounded background replay worker.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/app/db/contacts.py`:
  tenant-only contact reads and caller-contact provenance enforcement.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/tests/unit/test_contact_tenant_voice_isolation.py`:
  end-to-end regression tests for global/unproven identity quarantine in voice
  paths.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/tests/unit/test_service_request_recovery.py`:
  recovery lease, crash-boundary, retry, and `needs_review` tests.
- `/Volumes/Extreme Pro/MYPROJECTS/Kevin/.worktrees/customer-memory/docs/handoffs/2026-08-12-customer-memory-pr165-new-session-prompt.md`:
  paste-ready prompt for the next session.

## Commands Run And Results

The following full-suite result was freshly reproduced in the exact feature
worktree during handoff preparation:

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

```bash
git rev-parse HEAD
git rev-parse origin/main
git merge-base HEAD origin/main
```

Result at handoff creation:

```text
4f99e8bbcdae77081786798990e1f6d922158c68
d2a2f003134a66b35cd76cabb8c2aaa43ca184f5
d2a2f003134a66b35cd76cabb8c2aaa43ca184f5
```

```bash
gh pr view 165 --json state,isDraft,mergeable,mergeStateStatus,headRefOid,baseRefOid,statusCheckRollup,url
```

Result: PR open/draft, `MERGEABLE`, merge state `CLEAN`, exact head
`4f99e8bbcdae77081786798990e1f6d922158c68`; the `Test` check succeeded.

```bash
gh run view 31632664524 --json status,conclusion,headSha,jobs,url
```

Result: workflow completed successfully on exact head `4f99e8b`; `Test` passed.
Staging and production deploy jobs were skipped, as expected for a PR.

```bash
git diff --check
```

Result: passed with no output before the handoff files were added.

Earlier exact-tree review evidence preserved from the completed review pass:

- Independent post-fix tenant-isolation review: **APPROVE**, no P0/P1 remains in
  the isolation diff.
- Reviewer's focused run: `83 passed`; Ruff, format, and `git diff --check` passed.
- Provider/repository/recovery focused suites passed during implementation; the
  current full-suite and CI results supersede those narrower pass counts for the
  exact branch head.

Read-only aggregate production inventory captured earlier on 2026-08-12, not
refreshed during this handoff and therefore subject to drift:

- 112 contractors; all lacked the three new contractor feature flags, so the
  absent-is-false contract kept them disabled.
- Voice engine distribution was `elevenlabs: 100`, `relay: 1`, missing: `11`.
- No `gated_actions` or `automation_approvals` were found.
- One contractor was Google-connected and one integration-approved, but no
  admitted automatic booking approval was found.
- Legacy global inventory was 9 `contacts` and 10 `caller_contacts`; tenant
  subcollections contained 10,262 contacts and 36 caller contacts.
- The audit was read-only. No production state was changed.

## Verification

- Passed freshly: full local Python suite — `2268 passed, 21 warnings`.
- Passed remotely: GitHub Actions run
  [31632664524](https://github.com/delimatsuo/heykevin/actions/runs/31632664524)
  on exact head `4f99e8b`.
- Passed before handoff creation: `git diff --check`.
- Passed by independent review: exact tenant-isolation diff with focused tests and
  lint/format checks.
- Failed: none in the current local or CI evidence.
- Not run: staging Firestore index/TTL provisioning, staging Calendar operations,
  restart/fault injection against real services, Cloud Run deployment, production
  feature flags, production provider mutations, and iOS/device qualification.

## Risks And Watchouts

- **P0 activation blocker — caller authorization:** destructive cancel,
  reschedule, and add-service commands currently rely on bare ANI/caller ID. Adopt
  an approved caller-verification/session-authorization policy before activation.
- **P0 activation blocker — reschedule concurrency:** Google reschedule currently
  uses a blind desired-state PATCH/replay path. It can overwrite a manual or
  concurrent provider change. Add provider inspection and an ETag/precondition or
  explicit conflict policy before activation.
- **P1 activation blocker — name confirmation:** post-call capture currently uses
  the weak `_caller_spoke_name` transcript heuristic. An incidental name utterance
  can be treated as confirmation. Adopt and test an approved confirmation policy.
- **P1 privacy blocker — deletion/disclosure:** account deletion currently
  deactivates a contractor but does not recursively purge customer memory, service
  requests, receipts, or retained `needs_review` proposals. Implement and test the
  purge path and approve user disclosure before enabling capture.
- **P1 infrastructure gate:** required Firestore composite indexes and TTL policies
  are documented but not provisioned/verified by this branch. Wait for `READY` and
  enabled TTL status in the intended isolated environment.
- **P1 qualification gate:** isolated staging must prove create/mutate/restart/
  retry/dual-worker/fault boundaries against a dedicated Google Calendar.
- **P1 operations gate:** retained `needs_review` proposals need an explicit
  operator resolution procedure or UI. They intentionally never auto-abandon.
- **P1 pre-existing tenant-routing issue:**
  `app/webhooks/twilio_incoming.py` calls `get_call_history(caller_phone)` while
  `app/db/calls.py` filters only by caller phone. Tenant A's outcomes can influence
  tenant B's trust score and route. The current contact fix does not expose a name,
  but do not claim fully tenant-isolated routing until this path is repaired.
- **P2 follow-up:** optional contractor-ID defaults in contact helpers and dormant
  unscoped users in `app/services/adaptive_trust.py` and
  `app/services/lookup.py` should be made tenant-explicit or retired.
- **Exact-tree boundary:** any source change invalidates the exact `4f99e8b` review
  and CI evidence. Rerun proportional tests and request fresh independent review.

## Do Not Do

- Do not enable any new feature flag, configure recovery, provision production
  Firestore, deploy, or mutate a real Calendar from this handoff.
- Do not mark PR #165 ready or merge it as an activation claim. Default-off source
  merge eligibility and production activation are separate decisions.
- Do not reintroduce a demo-specific pipeline, fake memory, or repeated "this is a
  demo" greeting for a known returning caller.
- Do not copy, migrate, or fall back from tenant data to top-level global contacts
  or caller contacts. Those records are quarantined because phone equality is not
  tenant provenance.
- Do not treat a provider timeout or local Firestore write as provider success.
- Do not let TTL remove pending or `needs_review` provider proposals.
- Do not run `git reset --hard`, `git clean`, broad staging, or destructive
  worktree commands. Preserve unrelated user and other-agent files.
- Do not work in `/Volumes/Extreme Pro/MYPROJECTS/Kevin` when changing this
  feature. Use the dedicated customer-memory worktree.
- Do not commit or push these handoff files unless the user explicitly authorizes
  their publication.

## Next Recommended Steps

1. Refresh exact state in the customer-memory worktree: compare `HEAD`, upstream,
   `origin/main`, PR #165, CI, and `git status` before touching code.
2. Take the bounded tenant-routing follow-up first. Inventory every
   `get_call_history(` call, make inbound trust-history lookup explicitly require
   `contractor_id`, update the Firestore query to filter tenant plus caller, and add
   negative tests proving tenant A outcomes cannot influence tenant B. Preserve a
   separately named global/admin query only if a verified call site requires it.
3. Run the focused call-routing tests and the full Python suite, then obtain a fresh
   independent staff review because the change affects multi-tenant routing. Do not
   silently append the result to the existing exact-tree approval.
4. Before implementing destructive-call activation, produce a staff-reviewed design
   for caller verification/session authorization and Google reschedule concurrency
   handling. Do not implement a prompt-only authorization rule or blind retry.
5. Keep all feature flags false until the deletion/disclosure, Firestore, staging,
   operator-resolution, and authorization gates in
   `docs/customer-memory-rollout.md` are satisfied with environment-bound evidence.

## Open Questions

- No user input is required to begin the bounded tenant-scoped call-history repair.
- User authority is required before committing/pushing these handoff artifacts,
  merging the draft PR, deploying, configuring cloud resources, or activating any
  contractor/provider behavior.
