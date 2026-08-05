# Voice Bakeoff Offline Gate Report — 2026-08

**Report date:** 2026-08-03
**Source SHA (HEAD):** `ead5d2b3252c03c9876fa8b93e30f4389fba85f0`
**Branch:** `worktree-phase0-hygiene-polish`, based on `origin/main` at `fb628eb` (`ead5d2b` is one commit on top of `fb628eb`, confirmed by `git log --oneline -2`)
**Master plan:** `docs/superpowers/plans/2026-07-22-voice-architecture-bakeoff-and-lifecycle-control.md`
**Addendum:** `docs/superpowers/plans/2026-07-30-voice-bakeoff-offline-composition-addendum.md`

**Authority and scope (master plan §14, "Verification Order"):**
> "Offline, mock, or synthetic success must be labeled as such. It is not live caller proof."

Everything in this report is **offline, static, and mocked-fixture evidence only**, gathered by
reading source, running the local test suite, running `ruff`, and inspecting checked-in
fixtures/docs on the commit named above. This report makes **no claim** about provider-connected
behavior, staging, production, or any caller-experience fact. It does not authorize, and is not
evidence toward authorizing, provider execution, a capability probe, a sealed bakeoff window, or
production (master plan §§2, 11, 17). Task 4.8 and later stages remain out of scope for this
report; the only Task-4.8-adjacent evidence cited below (Addendum increment 4) is itself an
offline, non-executable status projection (`execution_status: not_authorized`).

Payload-safety: no credentials, transcripts, phone-shaped values, or raw session/call/stream IDs
appear anywhere below. Test citations use file paths and test function names only.

---

## 1. Step 1 — Worktree state (verified, not re-synced)

Per the controller's amendment, Step 1's `git pull --ff-only` was already performed upstream; this
worktree is branched from the result. Verified instead of re-pulled:

```text
$ git log --oneline -5
ead5d2b docs: add voice-quality roadmap and multi-agent orchestration plan
fb628eb Merge pull request #135 from delimatsuo/chore/main-housekeeping-sync
efb5113 chore: gitignore .playwright-mcp/ tool-trace directory
efacb13 fix: reconcile local main with origin/main after iOS WIP fork
5681fb6 Merge pull request #134 from delimatsuo/codex/task-4-8-provider-approval-rebuild

$ git status --short --branch
## worktree-phase0-hygiene-polish
```

HEAD is `ead5d2b`, directly on top of `fb628eb`, matching the expected state. The worktree was
clean apart from this report's own addition (one untracked `uv.lock` appeared as a side effect of
`uv` dependency resolution during Step 2 — see §2.4; it is not staged or committed). No fetch,
pull, merge, or rebase was performed against any remote.

---

## 2. Step 2 — Verification sweep (real output)

### 2.1 Full pytest suite

**First attempt** (this sandbox's pre-existing `.venv`, unmodified):

```text
$ uv run --extra dev pytest -q
...
ERROR tests/unit/test_jobber_tool_gating.py
ERROR tests/unit/test_media_stream_ingress.py
ERROR tests/unit/test_phase0_voice_tool_gates.py
ERROR tests/unit/test_receptionist_intelligence.py
ERROR tests/unit/test_urgency.py
ERROR tests/unit/test_voice_turn_replay.py
!!!!!!!!!!!!!!!!!!! Interrupted: 6 errors during collection !!!!!!!!!!!!!!!!!!!!
7 warnings, 6 errors in 5.45s
```

Root cause (from the full traceback): `ModuleNotFoundError: No module named 'audioop'` in each of
the six files. `uv run python --version` showed **Python 3.13.13**, and `.venv/pyvenv.cfg`
confirmed the venv was built against `cpython-3.13`. The stdlib `audioop` module (used
transitively by these six files' Twilio/mulaw-audio code paths) was removed in Python 3.13. This
repo's declared toolchain is Python 3.12 (CLAUDE.md; `.github/workflows/deploy.yml` pins
`python-version: '3.12'`; `pyproject.toml`'s `[tool.ruff] target-version = "py312"`), but
`pyproject.toml`'s `requires-python = ">=3.11"` has **no upper bound**, so a fresh `uv run`/`uv
sync` on a machine with multiple interpreters installed can silently select an incompatible one.
Python 3.12.13 was available locally (`/opt/homebrew/bin/python3.12`, and a uv-managed
`~/.local/bin/python3.12`), so this is a local interpreter-resolution problem, not a source defect
in the six files.

**Remediation:** `uv run --python 3.12 python --version` recreated `.venv` against Python 3.12.13
(a locally available interpreter; no new packages beyond what `pyproject.toml` already declares).
This is an environment (untracked `.venv/`) action only — no repository file was changed.

**Re-run on the Python-3.12 venv:**

```text
$ uv run python --version
Python 3.12.13
$ uv run --extra dev pytest -q
........................................................................ [  3%]
... (28 dot-rows total) ...
...........                                                              [100%]
1883 passed, 19 warnings in 24.46s
```

**Result: 1883 passed, 0 failed, 0 errors, 0 skipped, exit code 0.** `pyproject.toml` has no
pytest `addopts`/marker filtering (`[tool.pytest.ini_options]` sets only `asyncio_mode = "auto"`),
so this is an unfiltered full-suite count. The 19 warnings are pre-existing deprecation notices
(FastAPI `on_event`, Pydantic v1 `.dict()`, `audioop` deprecation-in-3.13, gRPC/protobuf internals)
unrelated to this sweep.

**Recorded as a gap** (per Step 2's instruction to record any failure either way): the *initial*
run failed at collection in this sandbox due to the interpreter mismatch described above. This is
an environment-resolution finding worth a follow-up (pin `requires-python` to a `,<3.13` upper
bound or add a `.python-version` file), not a code defect — no source file touches `audioop` in a
way that fails under the CI-pinned Python 3.12, and CI (which always provisions exactly 3.12) was
never at risk. The clean 1883-pass result under Python 3.12 is the authoritative reading used for
every gate row below.

### 2.2 Ruff

```text
$ uv run --extra dev ruff check app scripts tests
...
Found 863 errors.
[*] 431 fixable with the `--fix` option (36 hidden fixes can be enabled with the `--unsafe-fixes` option).
```

**Recorded as a gap** against the brief's "ruff clean" expectation: the whole-tree sweep is **not**
clean. This was independently re-run after the Python-3.12 venv fix and produced the identical
count (ruff is a static analyzer; its output does not depend on the invoking interpreter's runtime
version). Rule-code breakdown (top codes, whole-tree):

| Rule | Count | Meaning |
| --- | ---: | --- |
| I001 | 157 | import block un-sorted |
| BLE001 | 155 | blind `except Exception` |
| UP045 | 153 | `Optional[X]` → `X \| None` (pyupgrade) |
| TRY004 | 84 | prefer `TypeError` for type checks |
| G201 | 73 | `logging.exception` vs `.error(..., exc_info=...)` |
| UP035 / UP006 / UP037 / UP041 / UP017 | 87 (combined) | further pyupgrade modernization |
| S110 | 26 | `try/except/pass` without logging |
| RUF100 | 14 | unused `noqa` |
| F401 | 13 | unused import |
| EXE001 | 13 | shebang/executable-bit mismatch |
| SIM102 | 12 | collapsible nested `if` |
| F541 | 12 | f-string without placeholders |
| (remainder) | ~30 | RUF059, B008, TRY002, RUF012 |

None of these are syntax errors or import-time failures; all 20 distinct rule codes are
style/modernization/hygiene diagnostics. `.github/workflows/deploy.yml`'s `Test` job runs only
`pytest --tb=short -q` — **ruff is not part of the CI gate today**, so this is pre-existing,
whole-repository lint debt, not a regression introduced by or scoped to the voice-bakeoff work.
Per-gate rows below cite each gate's own narrow ruff count so no row hides debt; most of the
bakeoff-specific files carry zero-to-a-handful of these diagnostics (see §3), and the three
addendum files (`voice_bakeoff_turn_composition.py`, `voice_bakeoff_session_driver.py`, and both
their test files) are fully clean (0 issues each).

### 2.3 `git diff --check`

```text
$ git diff --check
$ echo $?
0
```

Clean — no whitespace errors. (The worktree carries no working-tree changes to check against the
index, which is expected for a freshly synced worktree.)

### 2.4 Digests

```text
$ shasum -a 256 app/experiments/voice_bakeoff_app.py app/main.py app/webhooks/media_stream.py
082d82e73deff2db331ba120513327f6911f41f1c9f0e9e7279e8f711df13127  app/experiments/voice_bakeoff_app.py
e73f0cd47ad1e10358e47e7db1981c39f0e03e041996cb4d2fd50cc9c308b7e9  app/main.py
4dab265d4b82336d8b0239090ee4c751cff345da359d2b70b38aa4f5e48e850c  app/webhooks/media_stream.py
```

All three match the addendum's "Bound baseline" section exactly (see §4 digests table). These
three files were not modified in the course of producing this report.

One untracked `uv.lock` appeared in the worktree as a side effect of `uv`'s dependency resolution
during the venv rebuild in §2.1. It is not `.gitignore`d in this lineage but is also not currently
tracked on this branch (it does appear, tracked, on unrelated historical branches). It is left
untracked and is **not** staged or committed by this task.

---

## 3. Gate-by-gate table

Verdict legend: **green** = the cited automated evidence (test file(s) actually re-run on this
source, on this commit) passes and matches the master plan's stated requirement for that task.
**gap: `<description>`** = evidence is missing, thin, or does not fully substantiate the task's
requirement; the description says exactly what is and is not proven.

Stage 1 and Stage 2 tasks, and Task 3.1, have no single quotable "**Gate:**" line in the master
plan (only Tasks 3.4 and 4.0–4.7 do); for those rows the "gate statement" column paraphrases the
task's own `**Files:**`/"Test first for:" text instead of inventing a gate sentence.

| Task | Gate statement (paraphrase/quote, § ref) | Evidence | Verdict |
| --- | --- | --- | --- |
| **1.1** — provider-neutral lifecycle types | No explicit Gate line. §6 "Stage 1", Task 1.1: schema-version/unknown-event/command rejection, source/provenance validation, environment/call/stream mismatch, monotonic sequence/epoch handling, duplicate/stale/late/out-of-order events, bounded/oversized-payload rejection, semantic-act transitions, idempotent commands, raw-payload non-retention — all listed as "Test first for" requirements. | `tests/unit/test_voice_lifecycle.py` — **21 passed** (targeted re-run, 2026-08-03). Ruff: `app/services/voice_lifecycle.py` 6 issues (3×SIM102, 3×TRY004 — style only), test file 0 issues. | green |
| **1.2** — payload-safe telemetry projection | No explicit Gate line. §6, Task 1.2: log projection is "an immutable allowlist of bounded enums, booleans, counts, ordinals, monotonic durations, and mapped error classes"; must never emit transcript/audio/phone/raw-ID/credential fields. | `tests/unit/test_voice_telemetry.py` — **3 passed** (targeted re-run). The three tests assert: HMAC-pseudonym/allowlist-only projection, rejection of raw/unknown fields, and rejection of phone/identifier-shaped values. This is a thin file (3 tests) for the full allowlist described in the task — it exercises the allow/deny boundary but not every enumerated field in the master plan's emit/never-emit lists individually. Ruff: `app/services/voice_telemetry.py` 2 issues, test file 0. | gap: evidence thin — cited tests exercise the projection mechanism but not every enumerated allowlist/forbidden field (report's own note) |
| **1.4** — aggregate evaluator | No explicit Gate line. §6, Task 1.4: reads NDJSON from stdin only, rejects raw/unrecognized fields, verifies arm/revision/SHA/manifest digest, enforces correlation/terminal coverage, reports aggregates only, fails on incomplete cohorts/ceiling-hit/privacy canaries. | `tests/unit/test_evaluate_voice_architecture_bakeoff.py` — **8 passed** (targeted re-run). Ruff: `scripts/evaluate_voice_architecture_bakeoff.py` 5 issues, test file 1 issue (style only). | green |
| **2.1** — authenticate every telephony ingress before media/provider work | No explicit Gate line. §7 "Stage 2", Task 2.1: `UNTRUSTED_HANDSHAKE → SIGNATURE_VALIDATED → AUTH_PENDING → AUTHENTICATED\|REJECTED`; signature validation, one-time short-lived tokens, atomic active-execution record, replay/concurrent-consume rejection, fail-closed before `AUTHENTICATED`. | `tests/unit/test_voice_session_auth.py` — **9 passed** (targeted re-run): forged-envelope rejection, attested PSTN/approval binding fail-closed, setup-deadline/pre-auth-media rejection with token erasure, exactly-one-winner under concurrent setup, one-time bound/revoked callback capability, closed-schema/token-redaction, reconnect epoch rotation with fresh attestation, canonical-scheme rejection, cross-account/replay rejection. Ruff: `app/services/voice_session_auth.py` 5 issues, test 1 issue. Note: this dedicated file's 9 tests cover the core state machine; the broader adversarial/negative matrix for this same contract is additionally exercised by Task 2.3's files (72 tests, next row) and Task 4.7's isolation suite (52 tests, §4.7 row). | green |
| **2.2** — qualification environment contract | No explicit Gate line. §7, Task 2.2: names dedicated nonproduction identities/resources; "contains credential references only, never credentials"; tools/writes/terminal actions/logging/tracing/recording off. | `docs/voice-architecture-bakeoff-qualification.md` reviewed directly — states "secret-free template," names the isolation contract and mandatory controls exactly as required. `tests/fixtures/voice_architecture_bakeoff/manifest.json` inspected directly: every credential/telephony/provider/custody field is a `REPLACE_WITH_...` placeholder or all-zero SHA/digest — confirmed secret-free. This fixture is read and exercised by `tests/unit/test_run_voice_architecture_bakeoff.py` (**49 passed**, shared evidence with Task 3.4 below). | green |
| **2.3** — negative security verification | No `**Files:**` section in the master plan for this task (its requirements fold into existing security/isolation modules). §7, Task 2.3: missing/invalid/replayed/expired/cross-environment tokens; proxy-header spoofing; forged stream/call/tenant IDs; cross-tenant state; unexpected tools; log canaries; route/import enumeration. | `tests/unit/test_voice_bakeoff_security_contracts.py` — **51 passed** (targeted re-run). `tests/unit/test_voice_bakeoff_route_isolation.py` + `tests/unit/test_voice_candidate_isolation.py` + `tests/unit/test_voice_logging_privacy.py` — **21 passed** (targeted re-run, combined). Ruff: security-contracts source 47 issues (10×BLE001, 33×TRY004, 2×S110, 1×I001, 1×SIM114 — all style/exception-hygiene, no syntax errors), its test file 4 issues; the three isolation/privacy test files are ruff-clean (0 each). Master plan names no dedicated file for 2.3, so these are the best-matching existing carriers, not a task-specific file the plan itself points to. | green |
| **3.1-dev-tier** — development-tier corpus scaffold | No explicit Gate line. §8 "Stage 3", Task 3.1 lists ~35 required scenario-family categories; Task 3.2 clarifies "Development tier: Authored scenarios and deterministic mocks may be iterated. They are not selection evidence." | `tests/fixtures/voice_architecture_bakeoff/development_corpus_manifest.json` — `scenario_families` array enumerates all ~35 categories from Task 3.1's list (direct-answer-before-follow-up, corrections, silence, interruption positions, safety, repair taxonomy, etc.) — structurally matches. However `"development_cases": []` and `"qualified_languages": []` are both **intentionally empty** (self-documented in `docs/voice-architecture-bakeoff-development-corpus.md`: "an empty list here is intentional and cannot support a release claim"). No automated test loads, validates, or exercises this fixture — `grep` across `tests/`, `app/`, `scripts/` for `development_corpus` finds zero references. | gap: coverage-taxonomy scaffold exists and is honestly self-labeled unsealed/incomplete, but there are zero populated development cases and zero automated tests — this is structural evidence only, not functional dev-tier test evidence |
| **3.4** — seal and approve provider execution (mechanism) | §8, Task 3.4 (quoted, the only explicit Gate line before Stage 4): **"A successful dry run proves contract consistency only. It does not authorize provider execution. Connected execution additionally requires the signed one-use envelope, runtime identity attestation, technical production isolation, and the exact evidence-tier permission defined by Task 0.1."** | `tests/unit/test_run_voice_architecture_bakeoff.py` — **49 passed** (targeted re-run; also part of the 1883-pass full suite). Load-bearing node IDs actually present and passing: `test_forged_signature_is_rejected_before_credential_resolution`, `test_wrong_owner_key_is_rejected`, `test_unconfigured_trust_key_is_rejected`, `test_replayed_nonce_is_rejected_on_second_invocation`, `test_credential_swapped_dependency_is_rejected`, `test_destination_mismatched_dependency_is_rejected`, `test_execute_provider_is_rejected_before_inputs_or_subprocess`, `test_runner_contains_no_network_or_credential_imports`, `test_documented_step_order_technical_review_before_emit_produces_valid_signature`. Ruff: `scripts/run_voice_architecture_bakeoff.py` 4 issues, test file 0. | green — dry-run mechanism only; this is explicitly **not** evidence of provider-execution authorization (per the quoted gate text itself) |
| **4.0** — shared speech control | §11 "Stage 4" (quoted): **"Zero unauthorized acts reach the candidate adapters or TTS, B1/B2 parity is exact on the shared fixtures, every pending question is reserved before speech, and all semantic-act terminals are coherent."** | `tests/unit/test_voice_speech_control.py` — **15 passed** (targeted re-run). Ruff: `app/services/voice_speech_control.py` 4 issues, test 0. | green |
| **4.1** — typed observation extractor (B1/B2) | (quoted): **"Development fixtures produce zero invalid state mutations and no B1/B2 configuration divergence. Task 4.1 may not access, score, or receive labels from the external sealed correction/observation holdout. The 100% sealed correction and at-least-99% sealed field-accuracy gates run only after the exact source and configuration freeze in Stage 5."** | `tests/unit/test_caller_observation_extractor.py` — **7 passed** (targeted re-run). Ruff: `app/services/caller_observation_extractor.py` 2 issues, test 1. This report makes no claim about the deferred sealed-holdout gates (correctly out of scope here). | green (development-fixture scope only, as the gate text itself scopes it) |
| **4.2** — shared bakeoff coordinator and call lifecycle | (quoted): **"All candidates pass the identical coordinator/lifecycle contract; the silence and terminal matrices are 100% deterministic; no candidate adapter owns a timer or terminal action; and static isolation proves no live import."** | `tests/unit/test_voice_call_lifecycle.py` + `tests/unit/test_voice_bakeoff_coordinator.py` — **27 passed** (targeted re-run, combined per the master plan's own Commands section). Ruff: both source files 0 issues; `test_voice_bakeoff_coordinator.py` 1 issue. | green |
| **4.3** — Arm A native control | (quoted): **"Tools are absent from configuration, terminal actions are suppressed in mocks, all bounds fail closed, lifecycle mappings are total, and static isolation passes... Without a proven pre-response permit, Arm A remains a native-quality control and is not selectable for policy-controlled Business mode."** | `tests/unit/test_voice_candidate_native_gemini.py` — **32 passed** (targeted re-run). Ruff: source 0 issues, test 1. | green — control-arm-only status is part of the gate text itself, reproduced here, not weakened |
| **4.4** — Arm B1 streamed chained reference | (quoted): **"Mocks prove permit ordering, zero invalid state mutation, complete act/audio identity mapping, deterministic cancellation, and no legacy ownership leakage."** | `tests/unit/test_voice_candidate_chained_streaming.py` + `tests/unit/test_caller_observation_extractor.py` — **13 passed** (targeted re-run, combined exactly per the master plan's own Commands section for this task). Ruff: `chained_streaming.py` 0 issues, its test 0. | green |
| **4.5** — Arm B2 ConversationRelay challenger | (quoted): **"B2 passes the shared mocked semantic/security contract, disconnect recovery has zero duplicated or stale acts, its ingress is authenticated and statically absent from production routing... B2 remains control-only until Task 4.8 proves an authoritative real-time normal-playback receipt..."** | `tests/unit/test_voice_candidate_conversation_relay.py` — **8 passed** (targeted re-run). Ruff: source 0 issues, test 1. | green — control-only status explicitly preserved, not claimed resolved |
| **4.6** — Arm C manual-turn feasibility probe | (quoted): **"Mocks prove permit ordering, stale/late rejection, bounded generation timeout, and total lifecycle mapping."** | `tests/unit/test_voice_candidate_manual_native.py` — **23 passed** (targeted re-run). Ruff: source 0 issues, test 0. | green |
| **4.7** — isolated bakeoff runtime and caller-side harness | (quoted): **"Static import/route enumeration proves `app.main` cannot mount or import the bakeoff app; every candidate and callback has an isolated authenticated route; the dry-run harness reproduces its schedule/common clock; encrypted evidence teardown leaves zero residue; and no provider network connection occurs."** | `tests/unit/test_voice_bakeoff_app_isolation.py` + `tests/unit/test_voice_bakeoff_caller.py` — **52 passed** (targeted re-run). Load-bearing node IDs present and passing: `test_production_import_graph_cannot_discover_bakeoff_entrypoint`, `test_app_and_harness_have_no_provider_network_or_live_route_imports`, `test_dry_run_cannot_construct_routes_and_connected_table_is_isolated`, `test_every_websocket_ingress_uses_auth_pending_and_one_setup_only`, `test_callbacks_require_store_issued_bound_capability`, `test_normal_session_rotates_epoch_before_reconnect_capability`, `test_isolation_resolver_catches_relative_from_and_dynamic_imports`. Ruff: `app/experiments/voice_bakeoff_app.py` 4 issues (byte-identical to the addendum's pinned digest regardless — see §4), its isolation test 2 issues. | green |
| **Addendum increment 1** — implement and exact-review `TurnCompositionTransaction` | Addendum "Delivery sequence" item 1: "Implement and exact-review `TurnCompositionTransaction`." | `app/services/voice_bakeoff_turn_composition.py` exists (141,476 bytes). `tests/unit/test_voice_bakeoff_turn_composition.py` — **105 passed** (targeted re-run). Ruff: both files **0 issues** (fully clean). Implementing commit `b50482f` "Implement offline turn composition transaction" is present in history and is an ancestor of the `Merge pull request #132` merge commit (`13e105c`), i.e. it landed through a reviewed-PR workflow rather than a bare direct push. | green (implementation + tests); the "exact-review" claim itself is addressed separately in increment 2 below |
| **Addendum increment 2** — commit only after staff and security approve exact tree | Addendum "Delivery sequence" item 2: "Commit the composition only after staff and security approve its exact tree." | Commit `b50482f`'s message body is empty (`git log -1 --format=%B` shows only the subject line) — no reviewer trailer, sign-off reference, or review-record file is present anywhere in the repository for this specific commit. The commit did land via PR #132 (see increment 1), which implies *a* GitHub PR-merge process was used, but offline evidence cannot confirm that a **staff-and-security exact-tree review with no unresolved P1** specifically preceded it, per master plan §14's rule that offline/mock evidence must be labeled as such and cannot substitute for a review record. | gap: no repo-visible review-approval artifact for this commit; a PR-merge process is evidenced, a specific staff+security sign-off is not |
| **Addendum increment 3** — sealed offline session driver + synthetic journey fixtures | Addendum "Delivery sequence" item 3: "Implement and exact-review the sealed offline session driver and synthetic journey fixtures." | `app/services/voice_bakeoff_session_driver.py` exists (214,027 bytes). `tests/unit/test_voice_bakeoff_session_driver.py` — **147 passed** (targeted re-run). Ruff: both files **0 issues** (fully clean). Implementing commit `b46ea44` "Implement sealed offline session driver" plus 8 follow-on "Qualify offline ..." commits are present in history, ending at `e13e77e` "feat: qualify unsupported-language recovery offline" (all ancestors of `HEAD`). | green (implementation + tests); same review-record caveat as increment 2 applies to these commits |
| **Addendum increment 4** — re-run the full offline gate and report remaining external blockers | Addendum "Delivery sequence" item 4: "Re-run the full offline gate and report remaining external blockers." Required-verification list also requires: "gate report remains `execution_status: not_authorized` with all nine external blockers." | This report's own §2.1 full-suite re-run (**1883 passed**, exit 0) **is** the "re-run the full offline gate" action for this cycle. `tests/unit/test_voice_bakeoff_gate_report.py` — **11 passed** (targeted re-run). Direct invocation of the existing reporting CLI: `PYTHONPATH=. uv run python scripts/report_voice_bakeoff_gate.py` returned (payload-safe JSON, reproduced verbatim): `"execution_status": "not_authorized"`, `"owner_approval_status": "not_recorded"`, `"advisory_review_status": "advisory_only"`, `"package_status": "preparation_only"`, `"package_source_binding": "unbound_template"`, `"report_source_sha": "ead5d2b3252c03c9876fa8b93e30f4389fba85f0"` (matches HEAD), and exactly **9** `blocking_gates` entries (`sealed_owner_authorization`, `independent_technical_review`, `physically_separate_preauth_store`, `identity_and_credential_broker`, `durable_trust_and_revocation_store`, `provider_privacy_and_region_attestations`, `complete_production_denylist`, `immutable_custody_and_residue_routing`, `one_use_runtime_envelope`) — matching "all nine external blockers." | green |

---

## 4. Digests table

| File | Addendum pinned SHA-256 ("Bound baseline") | Observed SHA-256 (this report, HEAD `ead5d2b`) | Match |
| --- | --- | --- | --- |
| `app/experiments/voice_bakeoff_app.py` | `082d82e73deff2db331ba120513327f6911f41f1c9f0e9e7279e8f711df13127` | `082d82e73deff2db331ba120513327f6911f41f1c9f0e9e7279e8f711df13127` | yes |
| `app/main.py` | `e73f0cd47ad1e10358e47e7db1981c39f0e03e041996cb4d2fd50cc9c308b7e9` | `e73f0cd47ad1e10358e47e7db1981c39f0e03e041996cb4d2fd50cc9c308b7e9` | yes |
| `app/webhooks/media_stream.py` | `4dab265d4b82336d8b0239090ee4c751cff345da359d2b70b38aa4f5e48e850c` | `4dab265d4b82336d8b0239090ee4c751cff345da359d2b70b38aa4f5e48e850c` | yes |

The addendum's pre-implementation baseline SHA (`41599321bb8ab8d45162432fba2bce81a88f7daa`) is
confirmed (`git merge-base --is-ancestor`) to be an ancestor of current HEAD, so this comparison is
against a consistent, non-rebased history.

---

## 5. Full-suite pass count, date, source SHA (closing summary)

- **Full-suite pass count:** 1883 passed, 0 failed, 0 errors, 0 skipped (`uv run --extra dev pytest -q`, Python 3.12.13 venv, 24.46s) — see §2.1 for the initial-environment failure this superseded.
- **Ruff (whole tree):** 863 pre-existing style/modernization diagnostics, not a CI gate today, not a regression — see §2.2.
- **`git diff --check`:** clean.
- **Digests:** 3/3 match the addendum's pinned baseline — see §4.
- **Date:** 2026-08-03.
- **Source SHA:** `ead5d2b3252c03c9876fa8b93e30f4389fba85f0`.

## 6. Gaps recorded in this report (summary)

1. Initial-environment pytest collection failure (6 files, `ModuleNotFoundError: No module named
   'audioop'`) caused by a local Python-3.13 venv resolving against this repo's uncapped
   `requires-python = ">=3.11"`. Remediated locally by rebuilding the venv against Python 3.12;
   recommend a follow-up to cap `requires-python` or add a `.python-version` file so this cannot
   recur silently on another machine or CI runner image change. (addressed later on this same
   branch: commit 8ee2c81 adds `.python-version` pinning 3.12)
2. Whole-tree `ruff check app scripts tests` is not clean (863 diagnostics, all style/hygiene
   rules, not CI-gated). Pre-existing, repo-wide, not specific to the voice-bakeoff modules.
3. Task 1.2 (payload-safe telemetry projection): evidence is real but narrow — the cited tests
   exercise the allow/deny projection mechanism but do not individually verify every enumerated
   allowlist/forbidden field from the master plan's emit/never-emit lists (see the row's own note
   in §3).
4. Task 3.1 development-tier corpus is a coverage-taxonomy scaffold only — zero populated cases,
   zero automated tests.
5. Addendum increment 2 (staff-and-security exact-tree review before commit) has no repo-visible
   review-approval artifact; only a PR-merge workflow is evidenced.

Three gates in §3 above carry a recorded, outstanding gap verdict — Task 1.2, Task 3.1-dev-tier,
and Addendum increment 2; every other green verdict there is backed by an actual, reproducible
test run performed on 2026-08-03 against source SHA `ead5d2b`, as cited per row in §3.
