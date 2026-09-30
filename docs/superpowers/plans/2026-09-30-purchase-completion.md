# Purchase and restore completion

Base: `ba964d035325b8ba153a37e3ff2dc449130eeb60`; tree: `1b0ab87aa8c9da1ae43a75fbf7731a463ae15f87`.

Business onboarding currently preselects Business but permits buying Personal,
then dismisses before Business activation completes. Restore dismisses without
activating Business. This slice fixes those paths while preserving Personal and
Settings choices.

## Builder envelope

Builder: `agy gemini-3.7-flash-high`, headless. Master owns architecture, Git,
validation, and independent review. The builder implements only; no Git mutations,
tests, builds, provider calls, deployments, uploads, credential access, `.env`
reads, or changes outside the allowlist. Read the repository operating law.
Do not use `rm -rf`. Do not touch other worktrees or call other agents.

Allowed implementation files:

- `ios/Kevin/Views/PaywallView.swift`
- `ios/Kevin/Views/OnboardingView.swift`
- `ios/Kevin/Models/PaywallPolicy.swift` (new)
- `ios/Kevin/Services/PaywallFlowCoordinator.swift` (new)
- `ios/KevinTests/PaywallFlowTests.swift` (new)

Leave this plan unchanged. Do not change SubscriptionManager or APIClient.

## Pinned behavior

1. Add `PaywallPurpose` with default `allPlans` and `businessActivation`.
   Business accepts exactly `com.kevin.callscreen.business.monthly` and
   `com.kevin.callscreen.businesspro.monthly`. Default retains all three known
   plans. Use one eligible collection for cards, empty state, CTA, price/title,
   preferred selection and actual purchase resolution. Missing Business products
   yields retry feedback and no CTA. Stale/ineligible selection must resolve only
   inside that collection; never purchase a global fallback. A pure policy over
   product IDs should be directly testable without StoreKit transactions.
2. Both purchase and restore enter one @MainActor observable flow coordinator.
   Inject async operation, refreshed entitlement check, current auth context,
   awaited activation and final completion closures. A single busy guard prevents
   parallel purchase/restore/activation. A false operation (cancelled/pending or
   failed verification) never completes. Preserve useful manager error feedback.
3. The coordinator captures the full CallAuthContext (including generation) before
   awaiting any operation and rechecks after every suspension and before effects.
   Reject invalid or rotated context. Business completion requires actual refreshed
   `appState.hasBusinessEntitlement`; Personal/default completion preserves current
   verified-purchase behavior. Never manufacture subscription status or tier.
4. Replace synchronous `onSubscribed` with an awaited result, e.g.
   `(@MainActor (CallAuthContext) async -> PaywallCompletionResult)?`, whose failure
   carries visible text. Call activation only after successful verification and
   eligibility. Await its success before dismissing or setting isOnboarded.
   Purchase and restore must share this path. Keep the sheet open on failure.
5. Business Onboarding passes the purpose and awaited activation handler.
   `activateBusinessAfterPurchase` takes the captured auth; guard current context
   and entitlement, use captured contractorId and `bearerToken` for PATCH, recheck
   auth after PATCH, then update mode/business fields and advance to contacts only
   on success. Return visible failure for wrong tier or failed PATCH. No hidden
   fire-and-forget task, premature sheet dismissal or repeat-purchase advice.
6. Wrong-tier restore says the restored plan is Personal and Business needs a
   Business plan; offer choosing Personal/back or Business upgrade. Failed Business
   activation advises Restore Purchases/retry after the purchase was verified.
   Keep manual dismissal when allowed, disable interactive dismissal while busy,
   and disable both transaction actions while busy. Default Personal final step
   explicitly prefers Personal when selectedMode is personal; keep all plans
   available. Settings callers retain their existing defaults.
7. Avoid adding unrelated redesign, default mode changes, pricing changes,
   analytics or receipt-reconciliation behavior.

## Acceptance criteria and tests

- "Business onboarding cannot initiate a Personal purchase, including through missing preferred product, stale selection, or fallback resolution."
- "Server-verified Business and Business Pro purchase and restore both activate Business and advance only after the mode PATCH succeeds."
- "Wrong-tier restore and activation failure leave visible feedback and a usable recovery path."
- "Personal verified completion and Settings upgrade choices are preserved."

Add Swift XCTest tests against production policy/coordinator (not duplicate test
logic). Use injected closures/counters/held continuations. For both purchase and
restore cover Business and Business Pro success/order; active Personal mismatch;
false verification with trial/none; activation failure; retry using restore with
no new purchase; default Personal success; concurrency blocked during a suspended
activation; auth rotation during operation and during activation; missing/unknown
products, wrong preferred ID and stale selection. Completion must remain zero
while activation is suspended. Do not run tests yourself; the master executes them
through xcodebuild-external and mutation probes after reviewing the diff.

Output strict JSON: {node:"purchase_builder",base_sha,files_changed,summary,
tests_added,commands_executed,risks,usage:{input_tokens,output_tokens,total_tokens}}.
Use null for unknown token counts. Do not claim tests passed or production fixed.
