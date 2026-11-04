import Foundation
import XCTest
@testable import Kevin

@MainActor
final class PaywallFlowTests: XCTestCase {

    private let validAuth = CallAuthContext(
        contractorId: "test-contractor-123",
        bearerToken: "test-token-abc",
        generation: 1
    )

    // MARK: - PaywallPolicy Tests

    func testPolicy_businessPurpose_allowsOnlyBusinessAndBusinessPro() {
        let purpose = PaywallPurpose.businessActivation
        XCTAssertTrue(PaywallPolicy.isEligible(productID: PaywallPolicy.businessProductID, for: purpose))
        XCTAssertTrue(PaywallPolicy.isEligible(productID: PaywallPolicy.businessProProductID, for: purpose))
        XCTAssertFalse(PaywallPolicy.isEligible(productID: PaywallPolicy.personalProductID, for: purpose))
        XCTAssertFalse(PaywallPolicy.isEligible(productID: "unknown.product.id", for: purpose))
    }

    func testPolicy_allPlansPurpose_allowsAllThreeKnownProducts() {
        let purpose = PaywallPurpose.allPlans
        XCTAssertTrue(PaywallPolicy.isEligible(productID: PaywallPolicy.personalProductID, for: purpose))
        XCTAssertTrue(PaywallPolicy.isEligible(productID: PaywallPolicy.businessProductID, for: purpose))
        XCTAssertTrue(PaywallPolicy.isEligible(productID: PaywallPolicy.businessProProductID, for: purpose))
        XCTAssertFalse(PaywallPolicy.isEligible(productID: "unknown.product.id", for: purpose))
    }

    func testPolicy_filterEligibleProductIDs() {
        let allProducts = [
            PaywallPolicy.personalProductID,
            PaywallPolicy.businessProductID,
            PaywallPolicy.businessProProductID,
            "com.kevin.callscreen.unknown",
        ]

        let businessEligible = PaywallPolicy.filterEligibleProductIDs(allProducts, for: .businessActivation)
        XCTAssertEqual(businessEligible, [
            PaywallPolicy.businessProductID,
            PaywallPolicy.businessProProductID,
        ])

        let allPlansEligible = PaywallPolicy.filterEligibleProductIDs(allProducts, for: .allPlans)
        XCTAssertEqual(allPlansEligible, [
            PaywallPolicy.personalProductID,
            PaywallPolicy.businessProductID,
            PaywallPolicy.businessProProductID,
        ])
    }

    func testPolicy_resolveSelectedProductID_preferredEligible() {
        let eligible = [PaywallPolicy.businessProductID, PaywallPolicy.businessProProductID]

        let resolvedBusiness = PaywallPolicy.resolveSelectedProductID(
            selectedID: nil,
            preferredID: PaywallPolicy.businessProductID,
            eligibleIDs: eligible
        )
        XCTAssertEqual(resolvedBusiness, PaywallPolicy.businessProductID)

        let resolvedBusinessPro = PaywallPolicy.resolveSelectedProductID(
            selectedID: nil,
            preferredID: PaywallPolicy.businessProProductID,
            eligibleIDs: eligible
        )
        XCTAssertEqual(resolvedBusinessPro, PaywallPolicy.businessProProductID)
    }

    func testPolicy_resolveSelectedProductID_wrongPreferredID() {
        let eligible = [PaywallPolicy.businessProductID, PaywallPolicy.businessProProductID]

        // Preferred is personal, but purpose is businessActivation (eligible only has business plans)
        let resolved = PaywallPolicy.resolveSelectedProductID(
            selectedID: nil,
            preferredID: PaywallPolicy.personalProductID,
            eligibleIDs: eligible
        )
        // Must resolve to the first eligible business product, NOT personal
        XCTAssertEqual(resolved, PaywallPolicy.businessProductID)
    }

    func testPolicy_resolveSelectedProductID_staleSelection() {
        let eligible = [PaywallPolicy.businessProductID, PaywallPolicy.businessProProductID]

        // User previously had personal selected in state, but now purpose is businessActivation
        let resolved = PaywallPolicy.resolveSelectedProductID(
            selectedID: PaywallPolicy.personalProductID,
            preferredID: PaywallPolicy.businessProductID,
            eligibleIDs: eligible
        )
        // Stale selection is not in eligible; resolves to preferred/first eligible business product
        XCTAssertEqual(resolved, PaywallPolicy.businessProductID)
    }

    func testPolicy_resolveSelectedProductID_missingOrUnknownProducts() {
        let emptyEligible: [String] = []
        let resolvedEmpty = PaywallPolicy.resolveSelectedProductID(
            selectedID: PaywallPolicy.businessProductID,
            preferredID: PaywallPolicy.businessProductID,
            eligibleIDs: emptyEligible
        )
        XCTAssertNil(resolvedEmpty)

        let unknownList = ["unknown.product"]
        let filtered = PaywallPolicy.filterEligibleProductIDs(unknownList, for: .businessActivation)
        let resolvedUnknown = PaywallPolicy.resolveSelectedProductID(
            selectedID: "unknown.product",
            preferredID: nil,
            eligibleIDs: filtered
        )
        XCTAssertNil(resolvedUnknown)
    }

    // MARK: - BusinessActivationOperation Tests

    func testBusinessActivation_success_commitsAndReturnsSuccess() async {
        var commitCount = 0
        var patchCalledWith: (String, [String: Any], String)?

        let result = await BusinessActivationOperation.activate(
            capturedAuth: validAuth,
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            patch: { contractorId, body, bearerToken in
                patchCalledWith = (contractorId, body, bearerToken)
                return true
            },
            commit: {
                commitCount += 1
            }
        )

        XCTAssertEqual(result, .success)
        XCTAssertEqual(commitCount, 1)
        XCTAssertEqual(patchCalledWith?.0, validAuth.contractorId)
        XCTAssertEqual(patchCalledWith?.1["mode"] as? String, "business")
        XCTAssertEqual(patchCalledWith?.2, validAuth.bearerToken)
    }

    func testBusinessActivation_falsePatch_returnsRestorePurchasesError() async {
        var commitCount = 0

        let result = await BusinessActivationOperation.activate(
            capturedAuth: validAuth,
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            patch: { _, _, _ in false },
            commit: { commitCount += 1 }
        )

        XCTAssertEqual(commitCount, 0)
        XCTAssertEqual(result, .failure("Failed to activate Business mode. Tap Restore Purchases to retry."))
    }

    func testBusinessActivation_patchThrows_returnsRestorePurchasesError() async {
        var commitCount = 0

        let result = await BusinessActivationOperation.activate(
            capturedAuth: validAuth,
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            patch: { _, _, _ in throw URLError(.badServerResponse) },
            commit: { commitCount += 1 }
        )

        XCTAssertEqual(commitCount, 0)
        XCTAssertEqual(result, .failure("Failed to activate Business mode. Tap Restore Purchases to retry."))
    }

    func testBusinessActivation_wrongEntitlement_returnsWrongTierError() async {
        var commitCount = 0
        var patchCalled = false

        let result = await BusinessActivationOperation.activate(
            capturedAuth: validAuth,
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { false },
            patch: { _, _, _ in
                patchCalled = true
                return true
            },
            commit: { commitCount += 1 }
        )

        XCTAssertFalse(patchCalled)
        XCTAssertEqual(commitCount, 0)
        XCTAssertEqual(result, .failure("The restored plan is Personal. Business mode requires a Business plan — upgrade below or choose Personal."))
    }

    func testBusinessActivation_invalidAuth_returnsAccountChanged() async {
        var commitCount = 0
        var patchCalled = false

        let invalid = CallAuthContext(contractorId: "", bearerToken: "", generation: 0)
        let result = await BusinessActivationOperation.activate(
            capturedAuth: invalid,
            currentAuth: { invalid },
            hasBusinessEntitlement: { true },
            patch: { _, _, _ in
                patchCalled = true
                return true
            },
            commit: { commitCount += 1 }
        )

        XCTAssertFalse(patchCalled)
        XCTAssertEqual(commitCount, 0)
        XCTAssertEqual(result, .failure("Your Kevin account changed during the purchase. Please try again."))
    }

    func testBusinessActivation_authRotationDuringPatch_abortsWithoutCommit() async {
        var commitCount = 0
        var currentGeneration = 1

        let result = await BusinessActivationOperation.activate(
            capturedAuth: validAuth,
            currentAuth: {
                CallAuthContext(
                    contractorId: self.validAuth.contractorId,
                    bearerToken: self.validAuth.bearerToken,
                    generation: currentGeneration
                )
            },
            hasBusinessEntitlement: { true },
            patch: { _, _, _ in
                // Rotate session generation during patch
                currentGeneration = 2
                return true
            },
            commit: { commitCount += 1 }
        )

        XCTAssertEqual(commitCount, 0)
        XCTAssertEqual(result, .failure("Your Kevin account changed during the purchase. Please try again."))
    }

    // MARK: - Purchase Flow Tests

    func testPurchase_businessSuccess_activatesAndCompletes() async {
        var activationCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { productID, _ in
                XCTAssertEqual(productID, PaywallPolicy.businessProductID)
                return true
            },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { auth in
                XCTAssertEqual(auth, self.validAuth)
                activationCount += 1
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.businessProductID)

        XCTAssertEqual(activationCount, 1)
        XCTAssertEqual(completionCount, 1)
        XCTAssertNil(coordinator.purchaseError)
    }

    func testPurchase_businessProSuccess_activatesAndCompletes() async {
        var activationCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { productID, _ in
                XCTAssertEqual(productID, PaywallPolicy.businessProProductID)
                return true
            },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { auth in
                XCTAssertEqual(auth, self.validAuth)
                activationCount += 1
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.businessProProductID)

        XCTAssertEqual(activationCount, 1)
        XCTAssertEqual(completionCount, 1)
        XCTAssertNil(coordinator.purchaseError)
    }

    func testPurchase_activePersonalMismatch_setsErrorAndDoesNotActivateOrComplete() async {
        var activationCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { _, _ in true },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { false },
            activationHandler: { _ in
                activationCount += 1
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.businessProductID)

        XCTAssertEqual(activationCount, 0)
        XCTAssertEqual(completionCount, 0)
        XCTAssertNotNil(coordinator.purchaseError)
        XCTAssertTrue(coordinator.purchaseError?.contains("Business plan") == true)
    }

    func testPurchase_falseVerification_setsManagerErrorAndDoesNotActivate() async {
        var activationCount = 0
        var completionCount = 0
        var purchaseOpCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { _, _ in
                purchaseOpCount += 1
                return false
            },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { false },
            managerError: { "Purchase completed, but Kevin could not verify it yet. Tap Restore Purchases to retry." },
            activationHandler: { _ in
                activationCount += 1
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.businessProductID)

        XCTAssertEqual(purchaseOpCount, 1)
        XCTAssertEqual(activationCount, 0)
        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.purchaseError, "Purchase completed, but Kevin could not verify it yet. Tap Restore Purchases to retry.")
    }

    func testPurchase_falseVerificationWithTrialOrNone_doesNotActivate() async {
        var activationCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .allPlans,
            purchaseByIDOperation: { _, _ in false },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { false },
            managerError: { nil },
            activationHandler: { _ in
                activationCount += 1
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.personalProductID)

        XCTAssertEqual(activationCount, 0)
        XCTAssertEqual(completionCount, 0)
    }

    func testPurchase_activationFailure_setsErrorLeavesOpen() async {
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { _, _ in true },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                return .failure("Failed to activate Business mode. Tap Restore Purchases to retry.")
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.businessProductID)

        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.purchaseError, "Failed to activate Business mode. Tap Restore Purchases to retry.")
    }

    func testPurchase_defaultPersonalSuccess_completesWithoutActivation() async {
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .allPlans,
            purchaseByIDOperation: { _, _ in true },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { false },
            activationHandler: nil,
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.personalProductID)

        XCTAssertEqual(completionCount, 1)
        XCTAssertNil(coordinator.purchaseError)
    }

    func testPurchase_authRotationDuringPurchaseOperation() async {
        var currentGeneration = 1
        var activationCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { _, _ in
                currentGeneration = 2
                return true
            },
            currentAuth: {
                CallAuthContext(
                    contractorId: "test-contractor-123",
                    bearerToken: "test-token-abc",
                    generation: currentGeneration
                )
            },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                activationCount += 1
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.businessProductID)

        XCTAssertEqual(activationCount, 0)
        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.purchaseError, SubscriptionManager.SubscriptionError.accountChanged.localizedDescription)
    }

    func testPurchase_authRotationDuringActivation() async {
        var currentGeneration = 1
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { _, _ in true },
            currentAuth: {
                CallAuthContext(
                    contractorId: "test-contractor-123",
                    bearerToken: "test-token-abc",
                    generation: currentGeneration
                )
            },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                currentGeneration = 2
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.businessProductID)

        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.purchaseError, SubscriptionManager.SubscriptionError.accountChanged.localizedDescription)
    }

    func testPurchase_ineligibleProductForPurpose_failsImmediately() async {
        var purchaseOpCalled = false
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { _, _ in
                purchaseOpCalled = true
                return true
            },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.personalProductID)

        XCTAssertFalse(purchaseOpCalled)
        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.purchaseError, "Selected plan is not eligible for this setup.")
    }

    // MARK: - Restore Flow Tests

    func testRestore_businessSuccess_activatesAndCompletes() async {
        var activationCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            restoreOperation: { true },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { auth in
                XCTAssertEqual(auth, self.validAuth)
                activationCount += 1
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.restorePurchases()

        XCTAssertEqual(activationCount, 1)
        XCTAssertEqual(completionCount, 1)
        XCTAssertNil(coordinator.purchaseError)
    }

    func testRestore_businessProSuccess_activatesAndCompletes() async {
        var activationCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            restoreOperation: { true },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { auth in
                XCTAssertEqual(auth, self.validAuth)
                activationCount += 1
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.restorePurchases()

        XCTAssertEqual(activationCount, 1)
        XCTAssertEqual(completionCount, 1)
        XCTAssertNil(coordinator.purchaseError)
    }

    func testRestore_activePersonalMismatch_setsErrorAndDoesNotActivate() async {
        var activationCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            restoreOperation: { true },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { false },
            activationHandler: { _ in
                activationCount += 1
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.restorePurchases()

        XCTAssertEqual(activationCount, 0)
        XCTAssertEqual(completionCount, 0)
        XCTAssertNotNil(coordinator.purchaseError)
        XCTAssertTrue(coordinator.purchaseError?.contains("Business plan") == true)
    }

    func testRestore_falseVerification_setsRestoreMessageAndDoesNotActivate() async {
        var activationCount = 0
        var completionCount = 0
        var restoreOpCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            restoreOperation: {
                restoreOpCount += 1
                return false
            },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { false },
            managerError: { nil },
            activationHandler: { _ in
                activationCount += 1
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.restorePurchases()

        XCTAssertEqual(restoreOpCount, 1)
        XCTAssertEqual(activationCount, 0)
        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.restoreMessage, "No active subscription found for this Apple ID.")
    }

    func testRestore_activationFailure_setsError() async {
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            restoreOperation: { true },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                return .failure("Failed to activate Business mode. Tap Restore Purchases to retry.")
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.restorePurchases()

        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.purchaseError, "Failed to activate Business mode. Tap Restore Purchases to retry.")
    }

    func testRetryUsingRestoreWithNoNewPurchase() async {
        var restoreShouldSucceed = false
        var activationShouldSucceed = false
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { _, _ in true },
            restoreOperation: { restoreShouldSucceed },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                if activationShouldSucceed {
                    return .success
                } else {
                    return .failure("Failed to activate Business mode. Tap Restore Purchases to retry.")
                }
            },
            onComplete: {
                completionCount += 1
            }
        )

        // Step 1: Purchase succeeds, but server activation fails
        await coordinator.purchase(productID: PaywallPolicy.businessProductID)
        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.purchaseError, "Failed to activate Business mode. Tap Restore Purchases to retry.")

        // Step 2: User taps Restore Purchases (no new purchase necessary)
        restoreShouldSucceed = true
        activationShouldSucceed = true
        await coordinator.restorePurchases()

        XCTAssertEqual(completionCount, 1)
        XCTAssertNil(coordinator.purchaseError)
    }

    func testRestore_defaultPersonalSuccess_completesWithoutActivation() async {
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .allPlans,
            restoreOperation: { true },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { false },
            activationHandler: nil,
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.restorePurchases()

        XCTAssertEqual(completionCount, 1)
        XCTAssertNil(coordinator.purchaseError)
    }

    func testRestore_authRotationDuringRestoreOperation() async {
        var currentGeneration = 1
        var activationCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            restoreOperation: {
                currentGeneration = 2
                return true
            },
            currentAuth: {
                CallAuthContext(
                    contractorId: "test-contractor-123",
                    bearerToken: "test-token-abc",
                    generation: currentGeneration
                )
            },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                activationCount += 1
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.restorePurchases()

        XCTAssertEqual(activationCount, 0)
        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.purchaseError, SubscriptionManager.SubscriptionError.accountChanged.localizedDescription)
    }

    func testRestore_authRotationDuringActivation() async {
        var currentGeneration = 1
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            restoreOperation: { true },
            currentAuth: {
                CallAuthContext(
                    contractorId: "test-contractor-123",
                    bearerToken: "test-token-abc",
                    generation: currentGeneration
                )
            },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                currentGeneration = 2
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.restorePurchases()

        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.purchaseError, SubscriptionManager.SubscriptionError.accountChanged.localizedDescription)
    }

    func testRestore_invalidInitialAuthContext_failsImmediately() async {
        var restoreOpCalled = false
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            restoreOperation: {
                restoreOpCalled = true
                return true
            },
            currentAuth: {
                CallAuthContext(contractorId: "", bearerToken: "", generation: 0)
            },
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.restorePurchases()

        XCTAssertFalse(restoreOpCalled)
        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.purchaseError, SubscriptionManager.SubscriptionError.missingContractor.localizedDescription)
    }

    // MARK: - Purpose and Configuration Boundary Tests (Fix 5)

    func testBusinessActivationPurposeWithNoActivationHandlerFailsClosed() async {
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { _, _ in true },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: nil,
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.businessProductID)

        XCTAssertEqual(completionCount, 0)
        XCTAssertEqual(coordinator.purchaseError, "Failed to activate Business mode. Tap Restore Purchases to retry.")
    }

    func testAllPlansPurposeWithNoActivationHandlerCompletes() async {
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .allPlans,
            purchaseByIDOperation: { _, _ in true },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { false },
            activationHandler: nil,
            onComplete: {
                completionCount += 1
            }
        )

        await coordinator.purchase(productID: PaywallPolicy.personalProductID)

        XCTAssertEqual(completionCount, 1)
        XCTAssertNil(coordinator.purchaseError)
    }

    func testConfigureRejectsReconfigurationWhileBusy() async {
        let gate = AsyncGate()
        defer { gate.open() }

        var initialActivationCalled = false
        var newActivationCalled = false
        var initialCompletionCalled = false

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { _, _ in true },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                initialActivationCalled = true
                await gate.signalAndPause()
                return .success
            },
            onComplete: {
                initialCompletionCalled = true
            }
        )

        let purchaseTask = Task {
            await coordinator.purchase(productID: PaywallPolicy.businessProductID)
        }

        let entered = await gate.waitForEntry()
        XCTAssertTrue(entered)
        XCTAssertTrue(coordinator.isBusy)

        // Attempt configure while busy — must be rejected and must not replace active purpose/handlers
        let configured = coordinator.configure(
            purpose: .allPlans,
            activationHandler: { _ in
                newActivationCalled = true
                return .success
            },
            onComplete: {}
        )
        XCTAssertFalse(configured)
        XCTAssertEqual(coordinator.purpose, .businessActivation)

        gate.open()
        await purchaseTask.value

        XCTAssertTrue(initialActivationCalled)
        XCTAssertFalse(newActivationCalled)
        XCTAssertTrue(initialCompletionCalled)
    }

    func testConfigureSucceedsWhenIdle() {
        let coordinator = PaywallFlowCoordinator(purpose: .allPlans)
        XCTAssertEqual(coordinator.purpose, .allPlans)

        let configured = coordinator.configure(
            purpose: .businessActivation,
            activationHandler: { _ in .success },
            onComplete: {}
        )
        XCTAssertTrue(configured)
        XCTAssertEqual(coordinator.purpose, .businessActivation)
    }

    // MARK: - Concurrency Tests (Fix 7)

    func testConcurrency_purchaseVsPurchase_secondPurchaseBlocked() async {
        let gate = AsyncGate()
        defer { gate.open() }

        var purchase1CallCount = 0
        var purchase2CallCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { productID, _ in
                if productID == PaywallPolicy.businessProductID {
                    purchase1CallCount += 1
                } else {
                    purchase2CallCount += 1
                }
                return true
            },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                await gate.signalAndPause()
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        let task1 = Task {
            await coordinator.purchase(productID: PaywallPolicy.businessProductID)
        }

        let entered = await gate.waitForEntry()
        XCTAssertTrue(entered)
        XCTAssertTrue(coordinator.isBusy)

        let task2Completed = expectation(description: "task2 completion")
        let task2 = Task {
            await coordinator.purchase(productID: PaywallPolicy.businessProProductID)
            task2Completed.fulfill()
        }
        await fulfillment(of: [task2Completed], timeout: 1.0)

        XCTAssertEqual(purchase1CallCount, 1)
        XCTAssertEqual(purchase2CallCount, 0)
        XCTAssertEqual(completionCount, 0)

        gate.open()
        _ = await task1.value
        _ = await task2.value

        XCTAssertEqual(completionCount, 1)
    }

    func testConcurrency_purchaseVsRestore_restoreBlocked() async {
        let gate = AsyncGate()
        defer { gate.open() }

        var purchaseCallCount = 0
        var restoreCallCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { _, _ in
                purchaseCallCount += 1
                return true
            },
            restoreOperation: {
                restoreCallCount += 1
                return true
            },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                await gate.signalAndPause()
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        let task1 = Task {
            await coordinator.purchase(productID: PaywallPolicy.businessProductID)
        }

        let entered = await gate.waitForEntry()
        XCTAssertTrue(entered)

        let task2Completed = expectation(description: "task2 completion")
        let task2 = Task {
            await coordinator.restorePurchases()
            task2Completed.fulfill()
        }
        await fulfillment(of: [task2Completed], timeout: 1.0)

        XCTAssertEqual(purchaseCallCount, 1)
        XCTAssertEqual(restoreCallCount, 0)
        XCTAssertEqual(completionCount, 0)

        gate.open()
        _ = await task1.value
        _ = await task2.value

        XCTAssertEqual(completionCount, 1)
    }

    func testConcurrency_restoreVsRestore_secondRestoreBlocked() async {
        let gate = AsyncGate()
        defer { gate.open() }

        var restoreCallCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            restoreOperation: {
                restoreCallCount += 1
                return true
            },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                await gate.signalAndPause()
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        let task1 = Task {
            await coordinator.restorePurchases()
        }

        let entered = await gate.waitForEntry()
        XCTAssertTrue(entered)

        let task2Completed = expectation(description: "task2 completion")
        let task2 = Task {
            await coordinator.restorePurchases()
            task2Completed.fulfill()
        }
        await fulfillment(of: [task2Completed], timeout: 1.0)

        XCTAssertEqual(restoreCallCount, 1)
        XCTAssertEqual(completionCount, 0)

        gate.open()
        _ = await task1.value
        _ = await task2.value

        XCTAssertEqual(completionCount, 1)
    }

    func testConcurrency_restoreVsPurchase_purchaseBlocked() async {
        let gate = AsyncGate()
        defer { gate.open() }

        var restoreCallCount = 0
        var purchaseCallCount = 0
        var completionCount = 0

        let coordinator = PaywallFlowCoordinator(
            purpose: .businessActivation,
            purchaseByIDOperation: { _, _ in
                purchaseCallCount += 1
                return true
            },
            restoreOperation: {
                restoreCallCount += 1
                return true
            },
            currentAuth: { self.validAuth },
            hasBusinessEntitlement: { true },
            activationHandler: { _ in
                await gate.signalAndPause()
                return .success
            },
            onComplete: {
                completionCount += 1
            }
        )

        let task1 = Task {
            await coordinator.restorePurchases()
        }

        let entered = await gate.waitForEntry()
        XCTAssertTrue(entered)

        let task2Completed = expectation(description: "task2 completion")
        let task2 = Task {
            await coordinator.purchase(productID: PaywallPolicy.businessProductID)
            task2Completed.fulfill()
        }
        await fulfillment(of: [task2Completed], timeout: 1.0)

        XCTAssertEqual(restoreCallCount, 1)
        XCTAssertEqual(purchaseCallCount, 0)
        XCTAssertEqual(completionCount, 0)

        gate.open()
        _ = await task1.value
        _ = await task2.value

        XCTAssertEqual(completionCount, 1)
    }

    // MARK: - Held-Continuation Business Activation Tests

    func testBusinessActivationOperation_heldPatch_generationChangesWhileSuspended_abortsWithoutCommit() async {
        let gate = AsyncGate()
        defer { gate.open() }

        let capturedAuth = CallAuthContext(
            contractorId: "test-contractor-123",
            bearerToken: "test-token-abc",
            generation: 1
        )
        var currentAuth = capturedAuth
        var commitCallCount = 0

        let task = Task {
            await BusinessActivationOperation.activate(
                capturedAuth: capturedAuth,
                currentAuth: { currentAuth },
                hasBusinessEntitlement: { true },
                patch: { _, _, _ in
                    await gate.signalAndPause()
                    return true
                },
                commit: {
                    commitCallCount += 1
                }
            )
        }

        let entered = await gate.waitForEntry()
        XCTAssertTrue(entered)
        XCTAssertEqual(commitCallCount, 0)

        // Externally change generation while held suspended in patch
        currentAuth = CallAuthContext(
            contractorId: "test-contractor-123",
            bearerToken: "test-token-abc",
            generation: 2
        )

        gate.open()
        let result = await task.value

        XCTAssertEqual(
            result,
            .failure(String(localized: "Your Kevin account changed during the purchase. Please try again."))
        )
        XCTAssertEqual(commitCallCount, 0)
    }

    func testBusinessActivationOperation_heldPatch_accountChangesWhileSuspended_abortsWithoutCommit() async {
        let gate = AsyncGate()
        defer { gate.open() }

        let capturedAuth = CallAuthContext(
            contractorId: "test-contractor-123",
            bearerToken: "test-token-abc",
            generation: 1
        )
        var currentAuth = capturedAuth
        var commitCallCount = 0

        let task = Task {
            await BusinessActivationOperation.activate(
                capturedAuth: capturedAuth,
                currentAuth: { currentAuth },
                hasBusinessEntitlement: { true },
                patch: { _, _, _ in
                    await gate.signalAndPause()
                    return true
                },
                commit: {
                    commitCallCount += 1
                }
            )
        }

        let entered = await gate.waitForEntry()
        XCTAssertTrue(entered)
        XCTAssertEqual(commitCallCount, 0)

        // Externally change account while held suspended in patch
        currentAuth = CallAuthContext(
            contractorId: "different-contractor-456",
            bearerToken: "different-token-xyz",
            generation: 1
        )

        gate.open()
        let result = await task.value

        XCTAssertEqual(
            result,
            .failure(String(localized: "Your Kevin account changed during the purchase. Please try again."))
        )
        XCTAssertEqual(commitCallCount, 0)
    }
}

// MARK: - AsyncGate Test Helper

final class AsyncGate: @unchecked Sendable {
    private var enterContinuations: [CheckedContinuation<Void, Never>] = []
    private var exitContinuations: [CheckedContinuation<Void, Never>] = []
    private let lock = NSLock()
    private var entryCount = 0
    private var isOpen = false

    var hasEntered: Bool {
        lock.lock()
        defer { lock.unlock() }
        return entryCount > 0
    }

    func signalAndPause() async {
        lock.lock()
        entryCount += 1
        let toResume = enterContinuations
        enterContinuations.removeAll()
        if isOpen {
            lock.unlock()
            for cont in toResume { cont.resume() }
            return
        }
        lock.unlock()
        for cont in toResume { cont.resume() }

        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            lock.lock()
            if isOpen {
                lock.unlock()
                cont.resume()
            } else {
                exitContinuations.append(cont)
                lock.unlock()
            }
        }
    }

    func waitForEntry(timeoutNanoseconds: UInt64 = 1_000_000_000) async -> Bool {
        lock.lock()
        if entryCount > 0 {
            lock.unlock()
            return true
        }
        lock.unlock()

        let enteredTask = Task {
            await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
                lock.lock()
                if entryCount > 0 {
                    lock.unlock()
                    cont.resume()
                } else {
                    enterContinuations.append(cont)
                    lock.unlock()
                }
            }
        }

        let timeoutTask = Task {
            try? await Task.sleep(nanoseconds: timeoutNanoseconds)
            lock.lock()
            let toResume = enterContinuations
            enterContinuations.removeAll()
            lock.unlock()
            for cont in toResume { cont.resume() }
        }

        await enteredTask.value
        timeoutTask.cancel()

        lock.lock()
        defer { lock.unlock() }
        return entryCount > 0
    }

    func open() {
        lock.lock()
        isOpen = true
        let currentEntries = enterContinuations
        enterContinuations.removeAll()
        let currentExits = exitContinuations
        exitContinuations.removeAll()
        lock.unlock()

        for cont in currentEntries { cont.resume() }
        for cont in currentExits { cont.resume() }
    }

    deinit {
        open()
    }
}
