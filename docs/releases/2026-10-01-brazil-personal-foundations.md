# Brazil Personal foundations — source qualification

Observed October 1, 2026. Owner priority is Brazil Personal, then Canada, then
the United Kingdom. This change prepares country admission, account binding and
Portuguese product behavior. It does not launch Brazil or change live service.

## Implemented acceptance

The [implementation contract](../superpowers/plans/2026-10-01-brazil-personal-implementation.md)
requires:

> Add explicit Brazil/+55/DDD entry, normalized and validated numbers, and a clear
> unsupported-country response.

Both API and database admission validate country and phone before account/trial
creation or number purchase. `GET /api/markets` reports US/CA available, BR/GB
requiring qualification, and other recognized legacy countries unsupported.
Unknown international numbers never become US by default. Verified existing
Apple-linked accounts remain recoverable. The iPhone checks availability before
creation and preserves authentication/identity across every awaited provisioning
stage; late responses cannot publish state into another account.

> Bind the account's country, provider, number type and capabilities together;
> changing a profile country must not reinterpret an already assigned number.

Assignment metadata is server-owned. Missing legacy provider/capability evidence
remains unknown. Country changes conflicting with an assigned number fail before
writes. The iPhone uses the assignment binding for setup, and non-US/CA or invalid
destinations produce no activation/cancellation dial actions. Existing +1
destination digits remain intact in US/Canada carrier commands.

> Complete pt-BR signup, setup, paywall, notifications and errors; qualify
> greetings, conversation, transcript and summary with the actual voice engine.

The source now contains the scoped pt-BR UI, deterministic Personal greetings,
caller-language-following prompts, owner-language summaries and private generic
notifications. SMS consent controls and English machine keys are unchanged.
StoreKit supplies normal offer prices; this change invents no BRL price or
unlimited usage allowance. Actual speech, calls, delivery and purchases remain
provider/physical acceptance work under the linked country plan.

## Verification evidence

Behavior source: `b9edeb8fda9ef711ebe1def41064e7a5d98ff262`.
Content identities allow the checks from isolated worktrees to be compared with
the integrated candidate without confusing branch history with source content:

| Scope | Git tree |
| --- | --- |
| `app` | `3081f0890efa10f166b9a87c3af5ed062d8f0164` |
| `tests` | `fce3b61e1bb746fba19e5dd3690f9c526e8c3b15` |
| `ios` | `fb9bc7508671397e5c40095ef7ef90a2fe8f3cb5` |

- Focused integrated Python run: **833 passed**, 35 existing deprecation
  warnings. Outbound networking was denied and dotenv loading disabled.
- Guarded Xcode Debug build and complete unit suite: **493 passed**, zero
  failures, iPhone 16 simulator on iOS 26.0. Tested iOS tree equals the integrated
  tree above. The initial unknown-country label failure was corrected and the
  complete suite passed afterward.
- Portuguese UI fixture: `testBrazilPhoneUnavailableScenario` passed. It checks
  Brazil's +55 entry, the phone field, Portuguese unavailability copy and absence
  of purchase actions. This fixture does not simulate successful Brazil service.
- Catalog audit: 281 required scoped keys translated; no missing entries,
  placeholder mismatches or unintended existing English/Spanish value changes.
  SMS STOP/HELP keywords remain intact.
- Eight deliberate guard faults were detected by actual tests: backend
  admission, assigned-country lock, unknown capabilities, Portuguese hold
  negation, private push body, iOS admission, country dial guard and provisioning
  identity. All mutations were restored; subsequent normal suites passed.
- Fatal Ruff selectors and `git diff --check` passed.

The fresh independent review initially found two P2 defects: a stale identity
could continue provisioning, and NANP rendering dropped the leading 1. Both were
fixed; re-review of `6989115106c1786ed99574a427ec248eb824c933` found no remaining
P1/P2 issue. Subsequent source changes are the unknown-country label correction
and stronger forwarding guard tests. Final exact-candidate review and hosted
checks are recorded on the associated pull request.

Visual screenshot inspection was not completed: simulator capture returned an
OS permission error, the native app viewer was unavailable, and the successful
XCTest result retained no screenshot attachment. Automated UI evidence is
reported separately from visual, physical-device and live-provider evidence.

## Twilio account identity

The authenticated production Cloud Run console shows revision
`kevin-api-00277-neb` receiving **100%** of traffic, deploy SHA
`b62eea38d9ab5656fb8d809fb872f05d9c81147d`. Its actual `TWILIO_ACCOUNT_SID` and
`PRODUCTION_TWILIO_ACCOUNT_SID` match, ending **0494**. Kevin is using the expected
Twilio account. The existing startup validator rejects an account mismatch.

The Twilio browser login exposes only account ending **45a9**, whose numbers
reference other applications. That account must not inform Kevin inventory or
registration decisions. Access to **0494** is still needed to inspect Brazil
eligibility, number inventory and the actual provider configuration. No Twilio
account, number, webhook or credential was changed during this verification.

## Remaining launch gates

1. Qualify an individual-eligible Brazil number/provider route in Kevin's correct
   account, including documents, inventory, voice/SMS capabilities and costs.
2. Identify the owner's Brazil carrier, exact plan and DDD, then complete
   owner-controlled forwarding and cancellation tests on the selected route.
3. Qualify natural Portuguese speech, live takeover, locked-phone push, promised
   summaries, purchase/restore, privacy/registration handling and unit economics.
4. Prepare the resulting backend/iOS release and Brazil store availability under
   the repository's deployment, App Store, provider and spend gates.

Brazil and the UK remain unavailable for new service. Canada retains existing
availability while its carrier qualification follows Brazil. No deployment,
TestFlight upload, territory change, number purchase, live call or ad launch is
part of this source receipt.

## Routing and hosted-check admission

Three primary implementation outputs (backend, iOS, voice) were integrated and
reduced in code: **3 expected, 3 returned**. Including bounded repairs and catalog
work, **23 expected builder outputs, 23 returned**, with no partial synthesis.
The master independently ran checks and a separate reviewer audited the diff.

Master model: GPT-6 Codex (exact runtime variant unavailable)
Builder model/tier: headless agy gemini-3.7-flash-high; gemini-3.7-flash-low for pinned mechanical corrections
Routing reason: strong builders for implementation; low tier for exact compiler, fixture and table edits; independent strongest-tier source review
agy transport=headless: true
Input tokens: 8349099
Output tokens: 1043976
Total tokens: 9393075
Retries: 20 bounded repair/supplement nodes after the three primary outputs
Audit defects found: 2 independent-review P2 findings, plus compiler, fixture and guard-coverage defects found by local verification
Audit disposition: remediated; hosted results bound to the final PR head

Repository and billing owner: `delimatsuo`, public `delimatsuo/heykevin`.
Feature/main pushes do not trigger this workflow. A PR to main runs seven bounded
Ubuntu Python shards, Python quality and the required fail-closed `Test`
aggregator. No deployment or macOS job runs for the PR. Expected incremental paid
Actions charge is **$0** for these public standard runners. The exact clean HEAD,
current billing and active-run state must be refreshed immediately before the
push/PR; any unexpected paid work requires separate admission.
