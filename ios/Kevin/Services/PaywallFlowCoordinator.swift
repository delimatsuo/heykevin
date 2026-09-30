import Foundation
import StoreKit

enum PaywallCompletionResult: Equatable, Sendable {
    case success
    case failure(String)
}

@MainActor
enum BusinessActivationOperation {
    static func activate(
        capturedAuth: CallAuthContext,
        currentAuth: () -> CallAuthContext,
        hasBusinessEntitlement: () -> Bool,
        patch: (_ contractorId: String, _ body: [String: Any], _ bearerToken: String) async throws -> Bool,
        commit: @MainActor () -> Void
    ) async -> PaywallCompletionResult {
        guard capturedAuth.isValid, capturedAuth == currentAuth() else {
            return .failure(String(localized: "Your Kevin account changed during the purchase. Please try again."))
        }
        guard hasBusinessEntitlement() else {
            return .failure(String(localized: "The restored plan is Personal. Business mode requires a Business plan — upgrade below or choose Personal."))
        }
        guard !capturedAuth.contractorId.isEmpty else {
            return .failure(String(localized: "Set up your Kevin account before activating Business mode."))
        }

        let updateBody: [String: Any] = [
            "mode": "business"
        ]

        do {
            let updated = try await patch(capturedAuth.contractorId, updateBody, capturedAuth.bearerToken)
            guard capturedAuth.isValid, capturedAuth == currentAuth() else {
                return .failure(String(localized: "Your Kevin account changed during the purchase. Please try again."))
            }
            if updated {
                commit()
                return .success
            } else {
                return .failure(String(localized: "Failed to activate Business mode. Tap Restore Purchases to retry."))
            }
        } catch {
            return .failure(String(localized: "Failed to activate Business mode. Tap Restore Purchases to retry."))
        }
    }
}

@MainActor
final class PaywallFlowCoordinator: ObservableObject {
    @Published var isPurchasing: Bool = false
    @Published var isRestoring: Bool = false
    @Published var purchaseError: String? = nil
    @Published var restoreMessage: String? = nil

    var isBusy: Bool {
        isPurchasing || isRestoring
    }

    var purpose: PaywallPurpose = .allPlans
    var activationHandler: (@MainActor (CallAuthContext) async -> PaywallCompletionResult)?
    var onComplete: (@MainActor () -> Void)?

    private let purchaseByIDOperation: ((String, String?) async throws -> Bool)?
    private let purchaseOperation: ((Product, String?) async throws -> Bool)?
    private let restoreOperation: (() async -> Bool)?
    private let currentAuthContextClosure: (() -> CallAuthContext)?
    private let hasBusinessEntitlementClosure: (() -> Bool)?
    private let managerErrorClosure: (() -> String?)?

    init(
        purpose: PaywallPurpose = .allPlans,
        purchaseByIDOperation: ((String, String?) async throws -> Bool)? = nil,
        purchaseOperation: ((Product, String?) async throws -> Bool)? = nil,
        restoreOperation: (() async -> Bool)? = nil,
        currentAuth: (() -> CallAuthContext)? = nil,
        hasBusinessEntitlement: (() -> Bool)? = nil,
        managerError: (() -> String?)? = nil,
        activationHandler: (@MainActor (CallAuthContext) async -> PaywallCompletionResult)? = nil,
        onComplete: (@MainActor () -> Void)? = nil
    ) {
        self.purpose = purpose
        self.purchaseByIDOperation = purchaseByIDOperation
        self.purchaseOperation = purchaseOperation
        self.restoreOperation = restoreOperation
        self.currentAuthContextClosure = currentAuth
        self.hasBusinessEntitlementClosure = hasBusinessEntitlement
        self.managerErrorClosure = managerError
        self.activationHandler = activationHandler
        self.onComplete = onComplete
    }

    @discardableResult
    func configure(
        purpose: PaywallPurpose,
        activationHandler: (@MainActor (CallAuthContext) async -> PaywallCompletionResult)?,
        onComplete: (@MainActor () -> Void)?
    ) -> Bool {
        guard !isBusy else { return false }
        self.purpose = purpose
        self.activationHandler = activationHandler
        self.onComplete = onComplete
        return true
    }

    func purchase(product: Product, offerID: String? = nil) async {
        await executePurchase(productID: product.id, offerID: offerID, product: product)
    }

    func purchase(productID: String, offerID: String? = nil) async {
        await executePurchase(productID: productID, offerID: offerID, product: nil)
    }

    private func executePurchase(productID: String, offerID: String?, product: Product?) async {
        guard !isBusy else { return }
        isPurchasing = true
        purchaseError = nil
        restoreMessage = nil
        defer { isPurchasing = false }

        let initialAuth = currentAuth()
        let snapshotPurpose = self.purpose
        let snapshotActivation = self.activationHandler
        let snapshotOnComplete = self.onComplete

        guard initialAuth.isValid else {
            purchaseError = SubscriptionManager.SubscriptionError.missingContractor.localizedDescription
            return
        }

        guard PaywallPolicy.isEligible(productID: productID, for: snapshotPurpose) else {
            purchaseError = String(localized: "Selected plan is not eligible for this setup.")
            return
        }

        let purchased: Bool
        do {
            if let customByID = purchaseByIDOperation {
                purchased = try await customByID(productID, offerID)
            } else if let customProduct = purchaseOperation, let product = product {
                purchased = try await customProduct(product, offerID)
            } else if let product = product {
                purchased = try await SubscriptionManager.shared.purchase(product, offerID: offerID)
            } else {
                purchaseError = String(localized: "Could not complete purchase.")
                return
            }
        } catch {
            guard currentAuth() == initialAuth else {
                purchaseError = SubscriptionManager.SubscriptionError.accountChanged.localizedDescription
                return
            }
            purchaseError = error.localizedDescription
            return
        }

        guard currentAuth() == initialAuth else {
            purchaseError = SubscriptionManager.SubscriptionError.accountChanged.localizedDescription
            return
        }

        guard purchased else {
            if let err = getManagerError(), !err.isEmpty {
                purchaseError = err
            }
            return
        }

        await handleVerifiedSuccess(
            capturedAuth: initialAuth,
            purpose: snapshotPurpose,
            activationHandler: snapshotActivation,
            onComplete: snapshotOnComplete
        )
    }

    func restorePurchases() async {
        guard !isBusy else { return }
        isRestoring = true
        purchaseError = nil
        restoreMessage = nil
        defer { isRestoring = false }

        let initialAuth = currentAuth()
        let snapshotPurpose = self.purpose
        let snapshotActivation = self.activationHandler
        let snapshotOnComplete = self.onComplete

        guard initialAuth.isValid else {
            purchaseError = SubscriptionManager.SubscriptionError.missingContractor.localizedDescription
            return
        }

        let restoredOK: Bool
        if let customRestore = restoreOperation {
            restoredOK = await customRestore()
        } else {
            restoredOK = await SubscriptionManager.shared.restorePurchases()
        }

        guard currentAuth() == initialAuth else {
            purchaseError = SubscriptionManager.SubscriptionError.accountChanged.localizedDescription
            return
        }

        guard restoredOK else {
            if let err = getManagerError(), !err.isEmpty {
                purchaseError = err
            } else {
                restoreMessage = String(localized: "No active subscription found for this Apple ID.")
            }
            return
        }

        await handleVerifiedSuccess(
            capturedAuth: initialAuth,
            purpose: snapshotPurpose,
            activationHandler: snapshotActivation,
            onComplete: snapshotOnComplete
        )
    }

    private func handleVerifiedSuccess(
        capturedAuth: CallAuthContext,
        purpose: PaywallPurpose,
        activationHandler: (@MainActor (CallAuthContext) async -> PaywallCompletionResult)?,
        onComplete: (@MainActor () -> Void)?
    ) async {
        guard currentAuth() == capturedAuth else {
            purchaseError = SubscriptionManager.SubscriptionError.accountChanged.localizedDescription
            return
        }

        if purpose == .businessActivation {
            guard checkBusinessEntitlement() else {
                purchaseError = String(localized: "The restored plan is Personal. Business mode requires a Business plan — upgrade below or choose Personal.")
                return
            }

            guard let activationHandler = activationHandler else {
                purchaseError = String(localized: "Failed to activate Business mode. Tap Restore Purchases to retry.")
                return
            }

            guard currentAuth() == capturedAuth else {
                purchaseError = SubscriptionManager.SubscriptionError.accountChanged.localizedDescription
                return
            }

            let result = await activationHandler(capturedAuth)

            guard currentAuth() == capturedAuth else {
                purchaseError = SubscriptionManager.SubscriptionError.accountChanged.localizedDescription
                return
            }

            switch result {
            case .success:
                onComplete?()
            case .failure(let errorText):
                purchaseError = errorText
            }
        } else {
            if let activationHandler = activationHandler {
                guard currentAuth() == capturedAuth else {
                    purchaseError = SubscriptionManager.SubscriptionError.accountChanged.localizedDescription
                    return
                }

                let result = await activationHandler(capturedAuth)

                guard currentAuth() == capturedAuth else {
                    purchaseError = SubscriptionManager.SubscriptionError.accountChanged.localizedDescription
                    return
                }

                switch result {
                case .success:
                    onComplete?()
                case .failure(let errorText):
                    purchaseError = errorText
                }
            } else {
                onComplete?()
            }
        }
    }

    private func currentAuth() -> CallAuthContext {
        if let custom = currentAuthContextClosure {
            return custom()
        }
        return AppState.shared.currentAuthContext()
    }

    private func checkBusinessEntitlement() -> Bool {
        if let custom = hasBusinessEntitlementClosure {
            return custom()
        }
        return AppState.shared.hasBusinessEntitlement
    }

    private func getManagerError() -> String? {
        if let custom = managerErrorClosure {
            return custom()
        }
        return SubscriptionManager.shared.purchaseError
    }
}
