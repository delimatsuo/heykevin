You are continuing the Hey Kevin offline voice-architecture bakeoff.

Current objective:
Tasks 4.2–4.7 are complete. Preserve the source-only Task 4.8 preparation and
do not begin connected execution while the sealed gate remains
`execution_status: not_authorized`.

Workspace:
- Repo/worktree: `/Volumes/Extreme Pro/myprojects/Kevin/.worktrees/voice-architecture-bakeoff-plan`
- Branch: `codex/voice-architecture-bakeoff-plan`
- Important docs to read first:
  - `/Volumes/Extreme Pro/myprojects/Kevin/AGENTS.md`
  - `docs/handoffs/2026-07-28-voice-bakeoff-external-gate-handoff.md`
  - `docs/superpowers/plans/2026-07-22-voice-architecture-bakeoff-and-lifecycle-control.md`
  - `docs/security/voice-bakeoff-sealed-composition-and-iam-packet.md`

Newest user request:
Resume only after applying the newest user instruction. The prior user asked
for a handoff; newest user instruction always wins.

Current state:
- Latest commit: `2ed8ea7 feat: add blocked control admission projection`.
- Branch was 36 commits ahead of `origin/main` at handoff creation.
- Current tracked tree was clean; `docs/handoffs/` is untracked user material.
  Preserve it and do not stage it accidentally.
- Source-only seams are unmounted:
  - `app/services/voice_bakeoff_google_firestore_runner.py`
  - `app/services/voice_bakeoff_firestore_transaction_port.py`
  - `app/services/voice_bakeoff_control_store_assembly.py`
  - `app/services/voice_bakeoff_control_admission_projection.py`
- The assembly is injection-only and zero-I/O at construction. The admission
  projector can return only a payload-safe blocked diagnostic, never an
  admission, store, credential, or capability.
- `scripts/report_voice_bakeoff_gate.py` is clean-tree guarded and at `2ed8ea7`
  reports `execution_status: not_authorized`, `owner_approval_status:
  not_recorded`, and nine blockers.

Critical constraints:
- Use only this bakeoff worktree. Preserve unrelated worktrees and the
  untracked handoffs.
- Do not deploy, make provider/PSTN calls, recruit participants, query raw
  transcripts, modify production/staging, create workloads, add credentials,
  create IAM/service accounts, or lock retention.
- Do not add prompt rules or alter Gemini model, VAD, or pacing.
- Do not use `kevin-491315` or any production/staging path for bakeoff work.
- The user is the sole engineer. Use independent staff review for high-stakes
  security/architecture judgment, but do not ask for a separate human approver.

Facts and evidence:
- Full suite: `1242 passed, 19 warnings`.
- Focused latest gate/projection/isolation suite: `53 passed, 2 warnings`.
- Targeted Ruff and `git diff --check` passed.
- Advisory staff approved source-only work at `2ed8ea7`; it does not authorize
  Task 4.8.

Next recommended action:
1. Run the status and gate report below. If the gate is still
   `not_authorized`, do not create more code, IAM, credentials, workloads, or
   pre-auth functionality.
2. Only act if the user supplies a separately custodied, source-pinned,
   bounded one-use owner-authorization record for a separately reviewed future
   nonproduction runtime proposal. Review it against every gate blocker before
   any connected action.

Verification expected:
- `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python scripts/report_voice_bakeoff_gate.py`
- `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest -q`
- `git diff --check`

Known risks:
- Offline evidence does not prove live caller playback, provider behavior,
  latency, production isolation, or safety completeness.
- The IAM packet is reference-only. It requires fresh Policy Troubleshooter and
  synthetic isolated-identity evidence before any future apply.
- A current gate report or blocked diagnostic is not an authorization object.

If anything conflicts, the newest user request wins. Start by running:

```bash
git status --short --branch
```
