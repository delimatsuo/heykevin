import Foundation
import StoreKit

enum ServerSubscriptionUUIDResolution: Equatable, Sendable {
    case resolved(UUID)
    case accountChanged
    case missingOrInvalid
}

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

    private let authProvider: @MainActor () -> CallAuthContext
    private var transactionListenerTask: Task<Void, Never>?
    private var verificationRetryTasks: [String: VerificationRetryTask] = [:]
    private var finishedVerificationKeys: Set<String> = []
    private let verificationCoordinator = SubscriptionVerificationCoordinator()

    init(authProvider: @escaping @MainActor () -> CallAuthContext = { AppState.shared.currentAuthContext() }) {
        self.authProvider = authProvider
    }

    var activeVerificationRetryCount: Int {
        verificationRetryTasks.count
    }

    func hasActiveVerificationRetry(
        transactionID: String,
        context: SubscriptionVerificationContext
    ) -> Bool {
        let key = verificationKey(context: context, transactionID: transactionID)
        guard let retry = verificationRetryTasks[key] else { return false }
        return !retry.task.isCancelled
    }

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

    func handleAuthChange(currentAuth: CallAuthContext) {
        guard currentAuth.isValid else {
            cancelAllVerificationRetries()
            return
        }
        let context = SubscriptionVerificationContext(
            contractorID: currentAuth.contractorId,
            bearerToken: currentAuth.bearerToken,
            generation: currentAuth.generation
        )
        cancelRetriesNotMatching(context)
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
        switch await Self.resolveServerSubscriptionUUID(
            context: context,
            appState: .shared,
            fetchProfile: APIClient.shared.getContractorProfile(contractorId:bearerToken:)
        ) {
        case .resolved(let uuid):
            purchaseOptions.insert(.appAccountToken(uuid))
        case .accountChanged:
            purchaseError = SubscriptionError.accountChanged.localizedDescription
            throw SubscriptionError.accountChanged
        case .missingOrInvalid:
            purchaseError = SubscriptionError.missingSubscriptionUUID.localizedDescription
            throw SubscriptionError.missingSubscriptionUUID
        }

        // Attach promotional offer if provided
        if let offerID = offerID {
            guard isCurrent(context) else {
                purchaseError = SubscriptionError.accountChanged.localizedDescription
                throw SubscriptionError.accountChanged
            }
            let signedOffer = await APIClient.shared.signSubscriptionOffer(
                productId: product.id,
                offerId: offerID,
                applicationUsername: context.contractorID,
                contractorId: context.contractorID,
                bearerToken: context.bearerToken
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
        guard let context = currentVerificationContext() else {
            purchaseError = SubscriptionError.missingContractor.localizedDescription
            return false
        }
        do {
            purchaseError = nil
            let restored = try await SubscriptionRestoreOperation.performRestore(
                context: context,
                isCurrent: { [weak self] in
                    guard let self else { return false }
                    return self.isCurrent(context)
                },
                sync: { try await AppStore.sync() },
                verify: { [weak self] capturedContext in
                    guard let self else { return false }
                    return await self.verifyCurrentEntitlements(source: .restore, context: capturedContext)
                }
            )
            if !restored && !isCurrent(context) {
                purchaseError = SubscriptionError.accountChanged.localizedDescription
            }
            return restored
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
        source: SubscriptionVerificationSource = .launch,
        context: SubscriptionVerificationContext? = nil
    ) async -> Bool {
        guard !Task.isCancelled else { return false }
        guard let context = context ?? currentVerificationContext() else { return false }
        var anyActive = false
        for await result in Transaction.currentEntitlements {
            guard !Task.isCancelled, isCurrent(context) else { return false }
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
        let outcome = await verificationCoordinator.result(for: key) { [weak self] in
            let isCurrent = await MainActor.run {
                guard let self else { return false }
                return self.isCurrent(context)
            }
            guard isCurrent else {
                return .rejected(reason: "account_changed")
            }
            return await APIClient.shared.verifySubscription(
                transactionId: transactionID,
                source: source,
                context: context
            )
        }
        return await Self.reconcileVerification(
            outcome, context: context, appState: .shared,
            fetchProfile: APIClient.shared.getContractorProfile(contractorId:bearerToken:)
        )
    }

    static func reconcileVerification(
        _ outcome: SubscriptionVerificationOutcome,
        context: SubscriptionVerificationContext,
        appState: AppState,
        fetchProfile: (_ contractorId: String, _ bearerToken: String) async -> [String: Any]?
    ) async -> SubscriptionServerResolution {
        let auth = CallAuthContext(
            contractorId: context.contractorID, bearerToken: context.bearerToken, generation: context.generation
        )
        guard auth.isValid, appState.currentAuthContext() == auth else {
            return .rejected(reason: "account_changed")
        }

        switch outcome {
        case .active, .inactive:
            let profile = await fetchProfile(auth.contractorId, auth.bearerToken)
            let serverStatus = profile.flatMap { appState.applySubscriptionProfile($0, for: auth) }
            guard appState.currentAuthContext() == auth else {
                return .rejected(reason: "account_changed")
            }

            // An inactive acknowledgement is terminal even if the profile is unavailable.
            // An active acknowledgement needs fresh status because an old receipt can be replayed.
            if outcome == .inactive { return .inactive }
            guard let serverStatus else { return .retryable(after: 30) }
            return serverStatus == "active" ? .active : .inactive

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
        scheduleVerificationRetryTask(
            transactionID: String(transaction.id),
            context: context,
            after: requestedDelay,
            onTerminal: { [weak self] terminal in
                guard let self else { return }
                _ = await self.handleVerificationResolution(
                    terminal,
                    for: transaction,
                    context: context
                )
            }
        )
    }

    func scheduleVerificationRetryTask(
        transactionID: String,
        context: SubscriptionVerificationContext,
        after requestedDelay: TimeInterval,
        attempt: (@MainActor () async -> SubscriptionServerResolution)? = nil,
        onTerminal: (@MainActor (SubscriptionServerResolution) async -> Void)? = nil
    ) {
        let retryKey = verificationKey(
            context: context,
            transactionID: transactionID
        )
        guard isCurrent(context) else { return }
        cancelRetriesNotMatching(context)
        guard verificationRetryTasks[retryKey] == nil else { return }

        let retryID = UUID()
        let task = Task { @MainActor [weak self] in
            guard let self else { return }
            let terminal = await SubscriptionVerificationRetryRunner.run(
                initialDelay: requestedDelay
            ) {
                guard self.isCurrent(context) else {
                    return .rejected(reason: "account_changed")
                }
                if let attempt {
                    return await attempt()
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
            await onTerminal?(terminal)
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

    private func cancelAllVerificationRetries() {
        let keys = Array(verificationRetryTasks.keys)
        for key in keys {
            cancelVerificationRetry(for: key)
        }
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

    @MainActor
    static func resolveServerSubscriptionUUID(
        context: SubscriptionVerificationContext,
        appState: AppState,
        fetchProfile: (_ contractorId: String, _ bearerToken: String) async -> [String: Any]?
    ) async -> ServerSubscriptionUUIDResolution {
        let auth = CallAuthContext(
            contractorId: context.contractorID,
            bearerToken: context.bearerToken,
            generation: context.generation
        )
        guard auth.isValid, appState.currentAuthContext() == auth else {
            return .accountChanged
        }

        // Always fetch from server; never short-circuit on cached appState.subscriptionUUID.
        let profile = await fetchProfile(auth.contractorId, auth.bearerToken)
        guard appState.currentAuthContext() == auth else {
            return .accountChanged
        }

        guard let profile,
              let uuid = appState.applySubscriptionUUID(
                  from: profile,
                  for: auth,
                  requiresReadySession: false
              ) else {
            return .missingOrInvalid
        }

        return .resolved(uuid)
    }

    private func loadServerSubscriptionUUID(
        context: SubscriptionVerificationContext
    ) async -> UUID? {
        switch await Self.resolveServerSubscriptionUUID(
            context: context,
            appState: .shared,
            fetchProfile: APIClient.shared.getContractorProfile(contractorId:bearerToken:)
        ) {
        case .resolved(let uuid):
            return uuid
        case .accountChanged:
            purchaseError = SubscriptionError.accountChanged.localizedDescription
            return nil
        case .missingOrInvalid:
            return nil
        }
    }

    private func currentVerificationContext() -> SubscriptionVerificationContext? {
        let auth = authProvider()
        guard auth.isValid else {
            cancelAllVerificationRetries()
            return nil
        }

        let context = SubscriptionVerificationContext(
            contractorID: auth.contractorId,
            bearerToken: auth.bearerToken,
            generation: auth.generation
        )
        cancelRetriesNotMatching(context)
        return context
    }

    private func isCurrent(_ context: SubscriptionVerificationContext) -> Bool {
        let auth = authProvider()
        return context.matches(
            contractorID: auth.contractorId,
            bearerToken: auth.bearerToken,
            generation: auth.generation
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
