# Gate 0A Current-Main Drift Reconciliation

## Decision

Gate 0A current-main closure is **blocked** at
`baf2fd9fee82e4a769550a556ebf308c3a5704d9` (tree
`3d9a977e2352a23e2c37c2126dfe3322bd1fdb21`). The provider-neutral assembly,
Gemini event adapter, evaluator, fixtures, ADR, and hard-disabled qualification
command have no demonstrated offline implementation gap. No implementation PR is
justified by this audit.

The earlier no-gap conclusion was still incomplete because the required
baseline-to-current live-pipeline no-diff assertion is false. The hard-pinned
qualification command also rejects current `main` with
`immutable_source_mismatch`, as designed. This audit does not authorize drafting a
Gate 0B successor plan. That requires this reconciliation PR to pass CI and review,
merge under separate authorization, and then receive separate user authorization
for Gate 0B planning.

This document does not authorize source-pin updates, runner implementation,
provider execution, credentials, purpose-recorded audio, real caller data,
observation extraction, live wiring, runtime flags, staging, production,
deployment, or release.

## Immutable identities

| Identity | Commit or digest | Meaning |
| --- | --- | --- |
| Qualification runner's supported source | `d7969acfd17018028c3aed86cc2733deffa9b1f7` | Commit that introduced the currently pinned live-source digests. |
| Historical Gate 0A audit baseline | `218822f2a2d1fa06d285de12d1ebeaecd26f6461` | Baseline named by the 2026-07-19 plan rebaseline. |
| Reconciled current `main` | `baf2fd9fee82e4a769550a556ebf308c3a5704d9` | Exact source reviewed by this audit. |
| Current tree | `3d9a977e2352a23e2c37c2126dfe3322bd1fdb21` | Landed tree for current-main verification. |
| Synthetic event fixture | `589f250a6f417606a80f5727a33225f608f6202e3f361fe2accfc499ebd537f7` | SHA-256 of `permutations.json`. |
| Current evaluator report | `50c2d576188ef53064d202685a76a0036635a81d675d642041f3cf6f50decb54` | SHA-256 of the external payload-free report generated for this audit. |

The plan itself changed after the historical baseline in commit `a9128c0`. It is
therefore excluded from claims that the Gate 0 implementation artifacts are
byte-unchanged from `218822f`.

## Source digest reconciliation

| File | Runner-supported digest at `d7969ac` | Digest at `218822f` | Digest at `baf2fd9` | Result |
| --- | --- | --- | --- | --- |
| `app/services/gemini_pipeline.py` | `33a0744b27e2c7e9ecfaeb8c15e276776cd7b22770e1783a63bdd0f5602ec3d4` | `8731a12d68b64a5216ed97b704784317dfa397f42a324b3164a0b8c490f09c8a` | `0c8326ba0a653360e192060a3795109885d5b383a8f48011599df6482d80ed37` | Pinned mismatch; additional post-baseline drift. |
| `app/services/voice_pipeline.py` | `9bdfea211568d1b8ca447677cb6b5dd807d81d099f6063053390114509249c8d` | `d032368ef26c937726e8f1ff6de11bc6ba87f163a91ce4aa7f346e29ccccce70` | `cffdb489a8acb8d7203ae0e62e35d0c86156e12299f092e1a73371e720fc257a` | Pinned mismatch; additional post-baseline drift. |
| `app/config.py` | `ae3e085976eb3409f79b18b9461e5957e4f768e3ce2e524f7da3e3dfb7f28018` | `a5e583c3cc9cb8ea14ad84ee59312ea4e35a5c64bd74a01ddfeba7c8cea8a55f` | `a5e583c3cc9cb8ea14ad84ee59312ea4e35a5c64bd74a01ddfeba7c8cea8a55f` | Pinned mismatch; no additional post-baseline drift. |

The runner was already intentionally incompatible with the historical audit
baseline. The current-main problem is separate: commits `53bb4cc` and `baf2fd9`
changed live pipeline code after `218822f`, so the plan's baseline-to-current
isolation check cannot pass even though static import isolation remains intact.

## Post-baseline pipeline drift

The exact `218822f..baf2fd9` live-pipeline delta is 115 additions and 18 deletions
in `gemini_pipeline.py`, plus three additions and one deletion in
`voice_pipeline.py`. It contains:

- a larger secondary audio-queue object bound while retaining the existing byte
  budget as the effective normal-frame limit;
- an explicit maximum response-token bound in Gemini generation configuration;
- bounded business-name handling and changed deterministic greeting construction;
- changed assistant-disclosure instructions in the legacy voice prompt;
- bounded voice-turn latency, first-media, cumulative-audio, and token-usage
  telemetry.

These changes do not import or wire `caller_turns`, `gemini_turn_events`,
`receptionist_observation`, or `gemini_observation_extractor`. They nevertheless
affect the live setup, prompt, or caller-facing behavior that a qualification
successor would need to bind. Greeting and disclosure semantics are reserved
product decisions; this audit records their existence without approving or
altering them.

## Corrected audit matrix

| Requirement | Current-main evidence | Result |
| --- | --- | --- |
| Provider-neutral assembly contract | 46 focused assembler, adapter, and evaluator tests passed. The event fixture contains ten authored synthetic cases. | No demonstrated offline code gap. |
| Hard-disabled qualification contract | 49 qualification tests passed, including zero-credential/zero-transport dry run, hard-disabled execute mode, immutable-source rejection, bounded caps, provenance, and mocked lifecycle handling. | No demonstrated offline code gap. |
| Payload-free offline evaluator | Report status is `pass`, failures are empty, and all validation/authorization booleans remain false. | Offline synthetic evidence only. |
| Gate 0 artifact stability | Gate 0 source, tests, evaluator, fixtures, ADR, and runbook have no diff from `218822f`. | Pass for those named artifacts only. |
| Static live-path isolation | Source search finds no Gate 0 import or wiring in either live pipeline. | Pass. |
| Immutable baseline-to-current live-pipeline diff | Both live pipeline files changed after `218822f`. | **Block.** |
| Runner source compatibility | Current-main source validation returns `immutable_source_mismatch`. | Safe fail-closed behavior; **block** any execution or preregistration claim. |
| Fixture provenance | Event permutations are `fixture_authored_synthetic`; the audio manifest is `pending` with zero cases and zero speakers and prohibits real-call and production audio. | Pass for offline audit; not execution-ready. |
| Security and privacy | Ruff and Bandit passed. Targeted private-key, credential, token, full-phone, address, and call-SID scans found no values. | Pass. |
| Full regression suite | 776 unit tests passed with 16 existing deprecation warnings on the exact current tree. | Pass. |

## Verification ledger

Commands were run from the clean current-main tree before this documentation-only
change:

```text
pytest test_caller_turns.py test_gemini_turn_events.py test_caller_turn_assembly_eval.py
  46 passed
pytest test_qualify_gemini_caller_turn_assembly.py
  49 passed
evaluate_caller_turn_assembly.py --source-sha baf2fd9...
  status=pass; failures={}; every authorization and validation claim=false
ruff check <Gate 0 source and tests>
  passed
bandit 1.8.6 -q -r <Gate 0 implementation source>
  passed
git diff --check
  passed
```

The documentation-only candidate ran all 776 unit tests with 16 existing warnings;
diff and targeted privacy checks also passed.

## Next allowed action

1. Obtain fresh staff/security review of this exact documentation candidate.
2. If approved, commit the exact reviewed tree, push this branch, and open a draft
   docs-only PR containing only this reconciliation and the plan status correction.
3. Keep that PR draft. Ready and merge transitions require fresh review, passing CI,
   and separate authorization.
4. Do not update the current runner's source pins or create a Gate 0A implementation
   PR.
5. Only after this reconciliation merges and the user separately authorizes Gate 0B
   planning may a successor plan start from fresh `main`. It must establish a new
   immutable setup projection for all current behavior-affecting fields and carry
   explicit reserved review for greeting, disclosure, prompt, and persona semantics.
6. Provider execution remains a separate approval after a reviewed successor
   implementation merges and binds its own exact source, corpus, credentials,
   endpoint, model, setup, time, attempt, and cost limits.

PR #109 remains an archived recoverable qualification asset by explicit repository
decision. This audit does not reopen it or authorize cherry-picking its code.

## Rollback

This candidate changes documentation only. Rollback is deletion of this audit and
restoration of the prior plan status text; no runtime, provider, data, flag,
workflow, dependency, or deployment state is affected.
