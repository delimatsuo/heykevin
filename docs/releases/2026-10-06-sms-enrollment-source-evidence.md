# SMS enrollment source and Brazil preparation — October 6, 2026

Source base: `16c45688e0b36a58b30d4e691e1debcb4ac4ff18`. The existing SMS
sender repair remains dated October 3 evidence. This continuation prevents a
future source gap and clarifies Brazil qualification; it does not replay the
repair or initiate live provider work.

## Source scope and release boundary

The [enrollment contract](../superpowers/plans/2026-10-06-sms-sender-enrollment.md)
pins a default-off, production US-only membership operation for one assigned
number, with account/service/campaign scope, tenant, consent, lifecycle,
ownership, routing, freshness, timeout and no-retry guards. Future activation
requires owner authorization and a freshly calculated campaign scope digest.
No workflow, country admission, SMS sending policy or iOS change is included.

The [updated Brazil pilot package](2026-10-02-brazil-personal-pilot.md) retains
five consenting adults, seven days, five numbers, 90 calls and 270 connected
minutes. Complete money caps, the exact carrier/plan/DDD and explicit provider
consumer eligibility remain unresolved. Support case 29797359 was refreshed;
an informational follow-up was sent at 21:36 EDT and separately read back in
Sent. The private receipt stays in the existing evidence branch. No provider
order, registration or pilot execution was performed.

## Builder and verification record

Master model: Codex master (precise session model identifier unavailable)
Builder model/tier: gemini-3.7-flash-high
Routing reason: bounded implementation of master-pinned invariants; master owns design, audit and Git
agy transport=headless: true
Input tokens: 692574
Output tokens: 140122
Total tokens: 832696
Retries: 4 bounded repair passes; no transport reruns
Audit defects found: 13 implementation/test groups and two pilot documentation findings
Audit disposition: remediated; final review and mutation evidence recorded below

Builder owns only the five allowlisted Python paths in its isolated worktree.
Master owns this integration worktree and the private support evidence branch.
The builder ran no tests or Git operations. Its prose was not used as evidence.
The first independent audit exposed nine grouped defects, including actual SDK
membership shape, strict pins, routing/readback freshness, privacy and queued
operation guards. One bounded repair addressed those groups. The ensuing focused
run exposed two test-fixture defects: a nonadvancing clock and patching a
locally imported client at the wrong module path. A second repair corrected
only those fixtures and strengthened persist-before-enrollment assertions.

Fresh staff review then found that a null membership lookup could reach create
without the only accepted absence proof (HTTP 404/code 20404). The master
reproduced it with a sealed client; a one-line third repair rejects null
membership with zero writes. Re-review exposed a test-only ordering assertion
inside a helper whose exception is intentionally caught. The master demonstrated
that reversed persistence/enrollment still passed; a fourth test-only repair
captures state at helper entry and asserts it outside the catch. The original
order passes, while the reversed-order scratch mutation now fails. Production
ordering was already correct and its source did not change.

The two separate pilot findings covered consent
from callers at both ends/private retention and counting every attempt plus
reserving all rounded provider and carrier legs. Both are incorporated.

Observed local verification:

- 192 focused tests passed across the enrollment, independent SDK-shape audit,
  provisioning, regulatory, country admission and owner SMS preference/gate files.
- 18 existing runtime safety tests passed separately; 210 focused passes total.
- The two new enrollment test files contribute 77 cases, including 45 independent
  sealed-client probes. They do not authenticate or invoke live providers.
- Fatal Ruff (`E9,F63,F7,F82`) passed for all six changed Python files. All six
  compiled, and whitespace checks passed.
- All 31 deliberate guard/order mutations were caught by assertion failures. The
  mutation harness loaded scratch copies; the integration source retained
  SHA-256 `50e9a6d6c8f2a194849f9b1704d832d5853032a119a3a5d747862c6bfbd8ebd6`.
  The unmutated scratch control passed all 77 new cases; the final ordering-only
  test repair was independently rerun with its passing control and caught
  reversed-order mutation. Mutations cover the
  default-off gate, US region, assignment/consent/lifecycle/type/capabilities,
  campaign pins, fresh service/campaign/ownership/routing, postwrite freshness,
  membership absence/capabilities, executor budget/cancellation, transport
  privacy, no retries, transport timeout and persistence ordering. No integration
  file was mutated.
- Fresh staff re-review approved the integrated source, tests and pilot package
  with no remaining source findings after the external snapshot assertion fix.
  The reviewer inspected artifacts and observed evidence, and ran no tests.

## Read-only CI admission findings

Live repository and billing owner: `delimatsuo/heykevin`, owner `delimatsuo`
(User, GitHub Pro); visibility **public**. Main protection requires strict
`Test`, with administrator enforcement. Repository policy also requires all
seven Python shards and Python quality. No active main ruleset adds checks.

Ordinary feature-branch push has no Actions trigger until a PR exists. Opening
or updating a PR to main runs seven `ubuntu-latest` shards, one Ubuntu quality
job and the fail-closed Ubuntu `Test` aggregator. The two deploy jobs skip.
Push/merge to main does not deploy or run this workflow. Rollback is manual.
PR concurrency is scoped to the PR with cancellation enabled; shard/quality
timeouts are 15 minutes, aggregator 5. No matrix nesting or reusable workflow
adds fan-out. No workflow control was changed.

The last comparable exact-head run, 37088891678, used nine jobs: seven shards,
quality and Test. Shard durations were 38–125 seconds, quality 35 seconds and
aggregator 2 seconds: approximately 7.2 elapsed runner-minutes, or 11 minutes
after per-job rounding. At the observed $0.006 Linux reference rate this is
$0.066 gross equivalent. Existing timeout limits cap job execution at 125
runner-minutes ($0.75 gross equivalent); queued time is not execution.

GitHub's [billing documentation](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
confirms standard hosted runners in public repositories are free. Expected
incremental paid runner cost for this source PR is **$0**. It uploads no
artifacts and changes no dependency manifest. A branch-scoped pip cache may be
written within the included allowance. Kevin has
one 58,843,114-byte cache, below the per-repository included allowance, and no
stored artifacts. No new paid-CI allocation is inferred or consumed. Stop if
visibility, runner labels, workflow fan-out or paid storage assumptions change.

Read-only account monitoring showed Linux runner minutes fully discounted and
paid charges attributable to storage. Detailed billing amounts and projections
remain in private local evidence. This owner-specific observation does not
establish a total across other billing owners or grant new paid allocation.

The last 30 Kevin runs showed no active/queued runs or rerun attempts. The
candidate had not yet been published at this preparation snapshot. Immediately
before publication, refresh
clean exact HEAD, branch, owner/visibility, required checks, active runs, cache
and billing. Bind the final SHA in the PR rather than extrapolating this
preparation snapshot. Monitor externally through the GitHub API; do not add a
monitoring workflow, rerun stale checks or change billing/branch protection.

## Separate acceptance still required

Hosted CI and configured Codex review apply to the exact published HEAD.
Source verification does not establish deployment, live enrollment, A2P carrier
activation, handset delivery, Brazil qualification or paid subscribers.
The last serving source remains historical; this continuation has not refreshed
GCP, released code, enabled the flag, placed calls or changed advertisements.
