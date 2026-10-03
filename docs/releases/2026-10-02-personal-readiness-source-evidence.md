# Personal readiness source evidence — October 2, 2026

Base: `fa353fc996f0a0003ec3179d3a17077518c59c07`.
Candidate branch: `codex/personal-service-readiness-20261002`.
The PR records its exact final commit and hosted checks; this receipt records
local qualification and does not assert deployment or provider repair.

## Delivered source

- An injected-client, read-only SMS reconciliation collector and offline
  aggregate CLI. It checks account/project/service bindings, finite reads,
  canonical number joins, duplicate/orphan assignments, freshness and source
  completeness. It retains a minimal contractor projection and emits no
  customer or number identifiers. Candidates mean missing membership requiring
  review, not campaign eligibility, consent, delivery or permission to mutate.
- Signup-country snapshot and a protected first screening-conversation
  observation, using the durable completed screening transcript after persisted
  post-call handling. Exact tenant and temporal checks prevent misattribution.
  Collection uses the existing controls and bounded background queue.
- Aggregate country/download/redownload grouping, independent milestone counts
  and explicit limits on export completeness, uniqueness and payment semantics.
- [SMS repair procedure](2026-10-02-sms-repair-runbook.md) and
  [Brazil Personal pilot script/cost worksheet](2026-10-02-brazil-personal-pilot.md).

## Executed verification

Final integrated focused command: **192 passed** across
`test_sms_readiness_audit`, `test_acquisition_measurement`,
`test_acquisition_wiring`, `test_screening_measurement`,
`test_post_call_handoff`, `test_acquisition_api` and
`test_forwarding_evidence`. All runs used `KEVIN_DISABLE_DOTENV=1` and the
clone's existing virtual environment. Existing dependency deprecation warnings
remain. Fatal Ruff selectors, Python compilation and diff whitespace checks pass.

Twelve deliberate mutations were detected by the intended behavioral tests:

| Removed or weakened guard | Observed result |
| --- | --- |
| SMS expected-account binding | 1 test failed |
| SMS source completeness for missing assignments | 1 test failed |
| Duplicate tenant-number assignment | 1 test failed |
| Exact Firestore projection | 1 test failed |
| Canonical provider number, including whitespace | 2 parameter cases failed |
| Screening route | 1 test failed |
| Screening durable tenant match | 1 test failed |
| Call predating cohort creation | 1 test failed |
| Attribution as-of cutoff | 1 test failed |
| Global ambiguity blocks all SMS candidates | 7 mixed-snapshot cases failed |
| Exact empty-string unassigned sentinel | 1 test failed |
| Malformed contractor assignment blocks all candidates | 6 malformed-input cases failed |

Mutations ran in isolated builder checkouts and originals were restored with
byte/hash checks. The integration candidate was never published in a mutated
state. Fresh independent combined review passed after its canonical-number
finding was repaired. Hosted review then identified global ambiguity and the
empty-string unassigned sentinel, then malformed contractor assignments hiding
duplicates; all three were corrected, mutation-tested and passed independent
re-review. The earlier focused run with incomplete test
isolation was stopped; it is not counted as passing evidence.

The initial published commit `f2c22d8484b8ac2d374da38e5835c81976d5f753`
passed all seven hosted Python shards (8,466 tests), quality and required `Test`
in [run 37085105654](https://github.com/delimatsuo/heykevin/actions/runs/37085105654).
The next commit `77ad3da758e4d9815f41990aca0ba73fbf3bb85c` passed 8,474 tests,
quality and required `Test` in
[run 37086254851](https://github.com/delimatsuo/heykevin/actions/runs/37086254851).
Those results apply to their respective commits. The PR must record new
exact-HEAD CI and hosted review after the malformed-assignment correction;
they are not inferred from the earlier runs.

## Routing and audit accounting

Master model: GPT-6 (Codex; exact variant unavailable)

Builder model/tier: `gemini-3.7-flash-high`; `gemini-3.7-flash-low` for pinned mechanical corrections

Routing reason: isolated pinned implementations, independently audited and tested by the master; low tier limited to exact fixture/canonical-input edits

agy transport=headless: true

Input tokens: 1867048

Output tokens: 367867

Total tokens: 2234915

Retries: 7 bounded repair invocations after 2 initial builder invocations

Audit defects found: 17 grouped source/test issues: source-metadata privacy;
schema/row/cap validation; read deadline; pool conflicts/orphans; source counts;
capability coercion; canonical numbers; incomplete candidate counts; scheduler
tenant binding; handoff raw-identity binding; future observations; test
isolation/signatures; missing test import; partial-result fixture; global
ambiguity across mixed snapshots; empty-string unassigned sentinel; malformed
contractor assignments concealing duplicates.

Audit disposition: remediated; focused tests and independent review pass

## Evidence still missing

Cloud authentication was rechecked and still requires owner sign-in. No active
user/SMS-gap intersection has been read live, no registration changed, and no
delivery test passed. The only incoming response for Twilio case 29797359 is its
acknowledgement. A follow-up was sent through the existing case's reply address
on October 2 at 21:12 Eastern; Gmail readback confirmed it in Sent.
Brazil consumer eligibility, inventory quote, exact
carrier plan/DDD, pilot budget, provider registration and device acceptance
remain unresolved. No deployment, flag, ads, App Store, account, number purchase,
customer outreach or real-call action was performed in this source slice.
