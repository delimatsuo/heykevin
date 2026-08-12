import Foundation
import StoreKit

/// Singleton that manages StoreKit 2 subscriptions for Kevin AI.
///
/// Server is the single source of truth. This class handles:
/// - Product fetching from App Store
/// - Purchase flow with promotional offer support
/// - Transaction listener (started at app launch)
/// - Calling /api/subscription/verify after purchase and on each launch
@MainActor
class SubscriptionManager: ObservableObject {
    static let shared = SubscriptionManager()

    // MARK: - Product IDs

    static let productIDs: Set<String> = [
        "com.kevin.callscreen.personal.monthly",
        "com.kevin.callscreen.business.monthly",
        "com.kevin.callscreen.businesspro.monthly",
    ]

    // MARK: - Published State

    @Published var products: [Product] = []
    @Published var isLoading = false
    @Published var purchaseError: String? = nil
    @Published var fetchError: String? = nil

    // MARK: - Internal State

    private struct VerificationRetryTask {
        let id: UUID
        let context: SubscriptionVerificationContext
        let task: Task<Void, Never>
    }

    private var transactionListenerTask: Task<Void, Never>?
    private var verificationRetryTasks: [String: VerificationRetryTask] = [:]
    private var finishedVerificationKeys: Set<String> = []
    private let verificationCoordinator = SubscriptionVerificationCoordinator()

    private init() {}

    enum SubscriptionError: LocalizedError {
        case missingContractor
        case missingSubscriptionUUID
        case serverVerificationFailed
        case ownershipMismatch
        case accountChanged

        var errorDescription: String? {
            switch self {
            case .missingContractor:
                return "Set up your Kevin account before subscribing."
            case .missingSubscriptionUUID:
                return "Kevin could not prepare your account for purchase. Please try again."
            case .serverVerificationFailed:
                return "Purchase completed, but Kevin could not verify it yet. Tap Restore Purchases to retry."
            case .ownershipMismatch:
                return "This purchase could not be matched to this Kevin account. Contact support so we can safely restore it."
            case .accountChanged:
                return "Your Kevin account changed during the purchase. Please try again."
            }
        }
    }

    // MARK: - Lifecycle

    /// Start listening for transaction updates. Call once at app launch.
    func startTransactionListener() {
        guard transactionListenerTask == nil else { return }
        transactionListenerTask = Task {
            for await verificationResult in Transaction.updates {
                await handleTransactionUpdate(verificationResult)
            }
        }
    }

    // MARK: - Product Fetching

    func fetchProducts() async {
        isLoading = true
        fetchError = nil
        defer { isLoading = false }
        do {
            let fetched = try await Product.products(for: SubscriptionManager.productIDs)
            if fetched.isEmpty {
                fetchError = "No products returned. Check StoreKit configuration is enabled in scheme (Edit Scheme → Run → Options → StoreKit Configuration)."
            }
            products = fetched.sorted { $0.price < $1.price }
        } catch {
            fetchError = error.localizedDescription
            print("SubscriptionManager: fetchProducts failed: \(error)")
        }
    }

    // MARK: - Purchase

    /// Purchase a product. Optionally with a promotional offer.
    func purchase(_ product: Product, offerID: String? = nil) async throws -> Bool {
        purchaseError = nil

        var purchaseOptions: Set<Product.PurchaseOption> = []

        // Set the server-issued subscription UUID as appAccountToken for ownership verification.
        guard let context = currentVerificationContext() else {
            purchaseError = SubscriptionError.missingContractor.localizedDescription
            throw SubscriptionError.missingContractor
        }
        guard let subscriptionUUID = await loadServerSubscriptionUUID(context: context) else {
            purchaseError = SubscriptionError.missingSubscriptionUUID.localizedDescription
            throw SubscriptionError.missingSubscriptionUUID
        }
        purchaseOptions.insert(.appAccountToken(subscriptionUUID))

        // Attach promotional offer if provided
        if let offerID = offerID {
            let signedOffer = await APIClient.shared.signSubscriptionOffer(
                productId: product.id,
                offerId: offerID,
                applicationUsername: AppState.shared.contractorId
            )
            if let offer = signedOffer,
               let keyID = offer["keyIdentifier"] as? String,
               let nonceStr = offer["nonce"] as? String,
               let nonce = UUID(uuidString: nonceStr),
               let sig = offer["signature"] as? String,
               let sigData = Data(base64Encoded: sig),
               let timestamp = offer["timestamp"] as? Int {
                purchaseOptions.insert(
                    .promotionalOffer(
                        offerID: offerID,
                        keyID: keyID,
                        nonce: nonce,
                        signature: sigData,
                        timestamp: timestamp
                    )
                )
            }
        }

        guard isCurrent(context) else {
            purchaseError = SubscriptionError.accountChanged.localizedDescription
            throw SubscriptionError.accountChanged
        }

        let result = try await product.purchase(options: purchaseOptions)

        switch result {
        case .success(let verificationResult):
            let transaction = try checkVerified(verificationResult)
            let resolution = await verifyWithServer(
                transactionID: String(transaction.id),
                source: .purchase,
                context: context
            )
            let handled = await handleVerificationResolution(
                resolution,
                for: transaction,
                context: context
            )
            if case .active = resolution, handled { return true }

            if case .rejected(let reason) = resolution,
               reason.contains("app_account_token") ||
               reason.contains("ownership_mismatch") ||
               reason.contains("receipt_already_bound") {
                purchaseError = SubscriptionError.ownershipMismatch.localizedDescription
                return false
            }
            purchaseError = SubscriptionError.serverVerificationFailed.localizedDescription
            return false

        case .userCancelled:
            return false

        case .pending:
            return false

        @unknown default:
            return false
        }
    }

    // MARK: - Restore Purchases

    /// Restore purchases. Returns true only when AppStore.sync surfaces at
    /// least one verified entitlement that the backend also confirms as
    /// active.
    ///
    /// Audit F-1: callers used to gate "restore succeeded" on
    /// `appState.subscriptionStatus == "trial" || "active"` after this
    /// returned, but `subscriptionStatus` defaults to `"trial"` on a fresh
    /// install. With no Apple entitlement, the inner loop in
    /// `verifyCurrentEntitlements` runs zero times and never modifies the
    /// status, so the caller incorrectly treated the default-`"trial"` as
    /// a successful restore and let the user past the onboarding paywall.
    /// We now return an explicit Bool that reflects whether a real
    /// entitlement was found and confirmed by the backend.
    @discardableResult
    func restorePurchases() async -> Bool {
        do {
            purchaseError = nil
            try await AppStore.sync()
            return await verifyCurrentEntitlements(source: .restore)
        } catch {
            purchaseError = "Restore failed: \(error.localizedDescription)"
            return false
        }
    }

    // MARK: - Server Verification

    /// Verify all current entitlements with the server. Called on app launch
    /// and from `restorePurchases`. Returns true if at least one
    /// transaction was verified end-to-end (Apple-verified AND the backend
    /// confirmed it as `subscription_status == "active"`).
    @discardableResult
    func verifyCurrentEntitlements(
        source: SubscriptionVerificationSource = .launch
    ) async -> Bool {
        guard let context = currentVerificationContext() else { return false }
        var anyActive = false
        for await result in Transaction.currentEntitlements {
            guard case .verified(let transaction) = result else { continue }
            // Only count this entitlement if the refreshed backend profile
            // says the contractor is currently active. A
            // user-cancelled-but-still-in-grace-period transaction can still
            // appear in currentEntitlements; we trust the server's view.
            let resolution = await verifyWithServer(
                transactionID: String(transaction.id),
                source: source,
                context: context
            )
            let handled = await handleVerificationResolution(
                resolution,
                for: transaction,
                context: context
            )
            if case .active = resolution, handled {
                anyActive = true
            }
        }
        return anyActive
    }

    private func verifyWithServer(
        transactionID: String,
        source: SubscriptionVerificationSource,
        context: SubscriptionVerificationContext
    ) async -> SubscriptionServerResolution {
        guard isCurrent(context) else { return .rejected(reason: "account_changed") }

        let key = verificationKey(context: context, transactionID: transactionID)
        let outcome = await verificationCoordinator.result(for: key) {
            await APIClient.shared.verifySubscription(
                transactionId: transactionID,
                source: source,
                context: context
            )
        }
        guard isCurrent(context) else { return .rejected(reason: "account_changed") }

        switch outcome {
        case .active:
            // The verify endpoint updates Firestore before returning. Refresh
            // the server-authoritative profile before reporting purchase or
            // restore success. A duplicate old transaction may be processed
            // successfully while the current account is now inactive.
            guard let serverStatus = await refreshServerSubscriptionState(
                context: context
            ) else {
                if !isCurrent(context) {
                    return .rejected(reason: "account_changed")
                }
                return .retryable(after: 30)
            }
            return serverStatus == "active" ? .active : .inactive

        case .inactive:
            // Apple verified the receipt and the server acknowledged it as
            // terminal without granting access. Refresh if available, but the
            // acknowledgement itself is sufficient to drain StoreKit safely.
            _ = await refreshServerSubscriptionState(context: context)
            guard isCurrent(context) else {
                return .rejected(reason: "account_changed")
            }
            return .inactive

        case .retryable(let retryAfter):
            return .retryable(after: retryAfter)

        case .rejected(let reason):
            return .rejected(reason: reason)
        }
    }

    // MARK: - Transaction Updates

    private func handleTransactionUpdate(_ verificationResult: VerificationResult<Transaction>) async {
        guard let transaction = try? checkVerified(verificationResult) else { return }
        guard let context = currentVerificationContext() else { return }
        let resolution = await verifyWithServer(
            transactionID: String(transaction.id),
            source: .transactionUpdate,
            context: context
        )
        _ = await handleVerificationResolution(
            resolution,
            for: transaction,
            context: context
        )
    }

    private func refreshServerSubscriptionState(
        context: SubscriptionVerificationContext
    ) async -> String? {
        guard isCurrent(context),
              let profile = await APIClient.shared.getContractorProfile(
                  contractorId: context.contractorID,
                  bearerToken: context.bearerToken
              ) else {
            return nil
        }
        guard isCurrent(context) else { return nil }

        let status = profile["subscription_status"] as? String ?? ""
        let tier = profile["subscription_tier"] as? String ?? ""
        if !status.isEmpty { AppState.shared.subscriptionStatus = status }
        if !tier.isEmpty { AppState.shared.subscriptionTier = tier }
        return status.isEmpty ? nil : status
    }

    @discardableResult
    private func handleVerificationResolution(
        _ resolution: SubscriptionServerResolution,
        for transaction: Transaction,
        context: SubscriptionVerificationContext
    ) async -> Bool {
        let transactionID = String(transaction.id)
        let key = verificationKey(context: context, transactionID: transactionID)

        guard isCurrent(context) else {
            cancelVerificationRetry(for: key)
            return false
        }

        switch resolution {
        case .active, .inactive:
            cancelVerificationRetry(for: key)
            if finishedVerificationKeys.insert(key).inserted {
                await transaction.finish()
            }
            return true

        case .retryable(let retryAfter):
            scheduleVerificationRetry(
                for: transaction,
                context: context,
                after: retryAfter
            )
            return false

        case .rejected:
            cancelVerificationRetry(for: key)
            return false
        }
    }

    private func scheduleVerificationRetry(
        for transaction: Transaction,
        context: SubscriptionVerificationContext,
        after requestedDelay: TimeInterval
    ) {
        let transactionID = String(transaction.id)
        let retryKey = verificationKey(
            context: context,
            transactionID: transactionID
        )
        guard isCurrent(context) else { return }
        cancelRetriesNotMatching(context)
        guard verificationRetryTasks[retryKey] == nil else { return }

        let retryID = UUID()
        let task = Task { [weak self] in
            guard let self else { return }
            let terminal = await SubscriptionVerificationRetryRunner.run(
                initialDelay: requestedDelay
            ) {
                guard self.isCurrent(context) else {
                    return .rejected(reason: "account_changed")
                }
                return await self.verifyWithServer(
                    transactionID: transactionID,
                    source: .retry,
                    context: context
                )
            }

            guard self.verificationRetryTasks[retryKey]?.id == retryID else {
                return
            }
            self.verificationRetryTasks[retryKey] = nil
            guard let terminal else { return }
            _ = await self.handleVerificationResolution(
                terminal,
                for: transaction,
                context: context
            )
        }
        verificationRetryTasks[retryKey] = VerificationRetryTask(
            id: retryID,
            context: context,
            task: task
        )
    }

    private func cancelVerificationRetry(for key: String) {
        guard let retry = verificationRetryTasks.removeValue(forKey: key) else {
            return
        }
        retry.task.cancel()
    }

    private func cancelRetriesNotMatching(
        _ context: SubscriptionVerificationContext
    ) {
        let staleKeys = verificationRetryTasks.compactMap { key, retry in
            retry.context == context ? nil : key
        }
        for key in staleKeys {
            cancelVerificationRetry(for: key)
        }
    }

    private func loadServerSubscriptionUUID(
        context: SubscriptionVerificationContext
    ) async -> UUID? {
        guard isCurrent(context) else {
            purchaseError = SubscriptionError.accountChanged.localizedDescription
            return nil
        }

        // Always prefer Firestore's UUID. Older app builds could cache a local
        // temporary UUID, which would make Apple's appAccountToken fail ownership checks.
        guard let profile = await APIClient.shared.getContractorProfile(
                  contractorId: context.contractorID,
                  bearerToken: context.bearerToken
              ),
              isCurrent(context),
              let subscriptionUUID = profile["subscription_uuid"] as? String,
              let uuid = UUID(uuidString: subscriptionUUID) else {
            return nil
        }

        AppState.shared.subscriptionUUID = subscriptionUUID
        return uuid
    }

    private func currentVerificationContext() -> SubscriptionVerificationContext? {
        let contractorID = AppState.shared.contractorId
        let bearerToken = APIClient.shared.contractorToken
        guard !contractorID.isEmpty, !bearerToken.isEmpty else { return nil }

        let context = SubscriptionVerificationContext(
            contractorID: contractorID,
            bearerToken: bearerToken
        )
        cancelRetriesNotMatching(context)
        return context
    }

    private func isCurrent(_ context: SubscriptionVerificationContext) -> Bool {
        context.matches(
            contractorID: AppState.shared.contractorId,
            bearerToken: APIClient.shared.contractorToken
        )
    }

    private func verificationKey(
        context: SubscriptionVerificationContext,
        transactionID: String
    ) -> String {
        "\(context.cacheNamespace):\(transactionID)"
    }

    // MARK: - Helpers

    private func checkVerified<T>(_ result: VerificationResult<T>) throws -> T {
        switch result {
        case .unverified(_, let error):
            throw error
        case .verified(let value):
            return value
        }
    }
}
