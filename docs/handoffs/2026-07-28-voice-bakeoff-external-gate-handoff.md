# Hey Kevin Voice Bakeoff External-Gate Handoff

Created: 2026-07-28 08:51 EDT
Prepared by: Codex

## Objective

Continue the Hey Kevin offline voice-architecture bakeoff only through safe,
nonproduction preparation. Tasks 4.2–4.7 are complete on the bakeoff branch.
Task 4.8 remains sealed: no connected execution may begin until the documented
external authority and evidence gates are satisfied. The user is the sole
engineer; independent staff review is advisory engineering review, not a
separate human approver.

## Current State

- Repo/workspace: `/Volumes/Extreme Pro/myprojects/Kevin`
- Implementation worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-architecture-bakeoff-plan`
- Branch: `codex/voice-architecture-bakeoff-plan`, 36 commits ahead of `origin/main`
- Latest commit: `2ed8ea7 feat: add blocked control admission projection`
- Dirty state: no tracked changes and no staged changes. `docs/handoffs/` is
  untracked; it contains prior user material plus the 2026-07-28 handoff pair. Do not
  stage, remove, or overwrite unrelated handoffs.
- Related agents: `/root/durable_adapter_staff_review` completed the advisory
  reviews for the latest source-only changes. No agent owns a pending edit.

## Newest User Request

The user requested a handoff. A newer user instruction supersedes this
document.

## Completed Work

- Offline Tasks 4.2–4.7 were completed and committed earlier on this branch.
  The completion is offline qualification only; it does not prove live caller
  playback, provider behavior, safety completeness, or latency.
- `ea470ab`: isolated Google Firestore transaction runner. It accepts a
  pre-attested client handle, rejects default/mismatched targets, and remains
  unmounted.
- `38d2eed`: sealed control composition and IAM packet. It is
  apply-prohibited and limits the future custom role to database get plus
  entity get/create/update.
- `976556f`: inert control-store assembly seam. It accepts already-attested
  injected dependencies, performs zero transactions at construction, exposes
  only control stores plus scope/binding, and has no public executor injection.
- `2ed8ea7`: deny-only control-admission projection and neutral gate report
  contracts. The projector can produce only a payload-safe
  `not_authorized` diagnostic; it cannot construct stores, clients,
  credentials, or a capability. Its local import closure excludes pre-auth,
  Firestore, runtime, and provider paths.
- Independent staff review approved each committed source-only step after P1
  corrections. The most recent staff verdict is that source/reference-only
  preparation is exhausted under the current prohibitions.

## Important Decisions

- The isolated control domain is separate from pre-auth. Do not implement a
  pre-auth runner, pre-auth identity, pre-auth IAM, or a cross-store saga in
  the control assembly seam.
- Do not create service accounts, IAM bindings, custom roles, workloads,
  provider credentials, provider/PSTN requests, deployments, retention locks,
  production access, or staging access. The IAM packet is a future reference
  only.
- The current Task 4.8 gate report is deny-only. It reports
  `execution_status: not_authorized`; it is not an authority object and cannot
  be extended into one by a boolean, enum, or polymorphic future status.
- Future execution requires a separately custodied, source-pinned, bounded,
  one-use owner-authorization record for a separately reviewed nonproduction
  runtime proposal. That record alone is insufficient: all nine gate
  evidences must also be current and independently reviewed without P1s.
- The user expects autonomous safe progress and does not want routine command
  permission questions. Respect non-negotiable external/irreversible gates and
  use independent staff review for high-stakes architecture or security
  judgment.

## Files And Artifacts

- `docs/superpowers/plans/2026-07-22-voice-architecture-bakeoff-and-lifecycle-control.md`:
  governing bakeoff plan.
- `docs/security/voice-bakeoff-sealed-composition-and-iam-packet.md`:
  control-only runtime contract, future IAM proposal, effective-access proof,
  and rollback requirements. It is explicitly `DO NOT APPLY`.
- `app/services/voice_bakeoff_control_store_assembly.py`:
  source-only, injection-only control-store assembly; unmounted.
- `app/services/voice_bakeoff_google_firestore_runner.py` and
  `app/services/voice_bakeoff_firestore_transaction_port.py`:
  closed transaction adapters; do not mount them in normal app paths.
- `app/services/voice_bakeoff_control_admission_projection.py`:
  source-only deny-only blocked diagnostic. It must never return an admission
  or capability.
- `app/services/voice_bakeoff_gate_contracts.py`:
  neutral `BlockingGate` and `Task48GateReport` value types; importing it must
  remain free of pre-auth/cloud/provider dependencies.
- `app/services/voice_bakeoff_gate_report.py` and
  `scripts/report_voice_bakeoff_gate.py`:
  clean-tree, payload-safe gate status. The script guards every report source,
  including `voice_bakeoff_gate_contracts.py`.
- `tests/unit/test_voice_bakeoff_sealed_composition_isolation.py`:
  AST/raw-text/runtime/deployment isolation guard for source-only seams.
- `tests/unit/test_voice_bakeoff_control_store_assembly.py` and
  `tests/unit/test_voice_bakeoff_control_admission_projection.py`:
  zero-I/O, type-boundary, and import-closure tests.
- `docs/handoffs/2026-07-24-voice-bakeoff-continuation-handoff.md`:
  historical handoff for the now-completed offline lifecycle/coordinator work.

## Commands Run And Results

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q
```

Result: `1242 passed, 19 warnings in 16.25s` after the latest gate-contract
and report-guard correction. Warnings are existing deprecation warnings.

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q \
  tests/unit/test_voice_bakeoff_gate_report.py \
  tests/unit/test_voice_bakeoff_control_admission_projection.py \
  tests/unit/test_voice_bakeoff_execution_firewall_contracts.py \
  tests/unit/test_voice_bakeoff_sealed_composition_isolation.py \
  tests/unit/test_voice_bakeoff_control_store_assembly.py
```

Result: `53 passed, 2 warnings`.

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/ruff check \
  scripts/report_voice_bakeoff_gate.py \
  app/services/voice_bakeoff_gate_contracts.py \
  app/services/voice_bakeoff_gate_report.py \
  app/services/voice_bakeoff_control_admission_projection.py \
  tests/unit/test_voice_bakeoff_gate_report.py \
  tests/unit/test_voice_bakeoff_control_admission_projection.py
git diff --check
```

Result: Ruff passed and `git diff --check` passed.

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python scripts/report_voice_bakeoff_gate.py
```

Result at committed `2ed8ea7`:

```json
{
  "execution_status": "not_authorized",
  "owner_approval_status": "not_recorded",
  "package_status": "preparation_only",
  "report_source_sha": "2ed8ea7d1d7f338e84ddf08d5a50a714835e1533"
}
```

The report lists nine blockers:

1. `sealed_owner_authorization`
2. `independent_technical_review`
3. `physically_separate_preauth_store`
4. `identity_and_credential_broker`
5. `durable_trust_and_revocation_store`
6. `provider_privacy_and_region_attestations`
7. `complete_production_denylist`
8. `immutable_custody_and_residue_routing`
9. `one_use_runtime_envelope`

## Verification

- Passed: focused tests, targeted Ruff, `git diff --check`, full test suite,
  clean-tree gate report, and advisory staff review of the latest source-only
  changes.
- Not run: provider/PSTN requests, caller testing, deployment, staging,
  production, workload execution, Firestore workload transactions, IAM
  changes, credentials, and retention locks. These are intentionally blocked.
- Unknown until future verification: live provider behavior, caller playback,
  latency, production/staging isolation proof, effective IAM access, and
  custody/retention behavior.

## Risks And Watchouts

- Critical: source-only tests do not authorize or prove connected execution.
- Critical: do not turn the current `not_authorized` gate report or blocked
  diagnostic into an authority path.
- Critical: do not use the production project `kevin-491315`, its Cloud Run
  services, staging, or existing production credentials for the bakeoff.
- High: the current reference-only IAM packet is deliberately incomplete for
  live use. Its custom role/condition must be revalidated with Policy
  Troubleshooter and a synthetic isolated identity before any apply.
- High: isolated control and pre-auth resources were historically prepared in
  separate projects, but their current cloud state is not freshly verified in
  the 2026-07-28 handoff. Re-read and validate before any future use.
- High: `docs/handoffs/` is untracked user material. Do not clean it up or
  stage it accidentally.

## Do Not Do

- Do not work in `/Volumes/Extreme Pro/myprojects/Kevin` root or a historical
  worktree; use only the bakeoff worktree.
- Do not deploy, push to `main`/`staging`, connect a provider, make PSTN calls,
  query raw transcripts, recruit participants, or modify production.
- Do not add prompt rules or change Gemini model, VAD, or pacing.
- Do not create a pre-auth implementation, service account, IAM binding,
  workload identity, secret, provider credential, or retention lock under the
  current gate.
- Do not stage or delete unrelated handoff files.

## Next Recommended Steps

1. Start with `git status --short --branch` and run the clean-tree gate report.
2. Do no further implementation while the gate remains `not_authorized` unless
   a newer user request changes scope.
3. If the user supplies a source-pinned, bounded one-use owner-authorization
   record for a separately reviewed nonproduction runtime proposal, first
   independently review that record against all nine blockers. Do not infer
   authority from prior conversation, code, or the current report.
4. Only after the external package is complete should an agent consider the
   separately reviewed pre-auth boundary, least-privilege IAM application, and
   isolated synthetic control workload verification. Provider/PSTN execution
   remains a later Task 4.8 step.

## Open Questions

- No safe source-only implementation work remains under the current
  prohibitions.
- The user must decide whether and when to provide the separately custodied,
  source-pinned one-use owner-authorization package. No other human approval
  is required by project staffing.
