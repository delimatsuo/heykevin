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

    private var transactionListenerTask: Task<Void, Never>?

    private init() {}

    enum SubscriptionError: LocalizedError {
        case missingContractor
        case missingSubscriptionUUID
        case serverVerificationFailed

        var errorDescription: String? {
            switch self {
            case .missingContractor:
                return "Set up your Kevin account before subscribing."
            case .missingSubscriptionUUID:
                return "Kevin could not prepare your account for purchase. Please try again."
            case .serverVerificationFailed:
                return "Purchase completed, but Kevin could not verify it yet. Tap Restore Purchases to retry."
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
        guard let subscriptionUUID = await loadServerSubscriptionUUID() else {
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

        let result = try await product.purchase(options: purchaseOptions)

        switch result {
        case .success(let verificationResult):
            let transaction = try checkVerified(verificationResult)
            let verified = await verifyWithServer(transactionID: String(transaction.id), requireActiveStatus: true)
            if verified {
                await transaction.finish()
                return true
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
            return await verifyCurrentEntitlements()
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
    func verifyCurrentEntitlements() async -> Bool {
        var anyActive = false
        for await result in Transaction.currentEntitlements {
            guard case .verified(let transaction) = result else { continue }
            // requireActiveStatus=true so we only count this entitlement if
            // the backend says the contractor is currently active. A
            // user-cancelled-but-still-in-grace-period transaction can still
            // appear in currentEntitlements; we trust the server's view.
            let confirmed = await verifyWithServer(
                transactionID: String(transaction.id),
                requireActiveStatus: true
            )
            if confirmed { anyActive = true }
        }
        return anyActive
    }

    @discardableResult
    private func verifyWithServer(transactionID: String, requireActiveStatus: Bool = false) async -> Bool {
        let success = await APIClient.shared.verifySubscription(transactionId: transactionID)
        guard success else { return false }

        var serverStatus = ""
        if success {
            // Refresh subscription state from backend after successful verification
            let contractorId = AppState.shared.contractorId
            guard !contractorId.isEmpty else { return false }
            if let profile = await APIClient.shared.getContractorProfile(contractorId: contractorId) {
                let status = profile["subscription_status"] as? String ?? ""
                let tier = profile["subscription_tier"] as? String ?? ""
                if !status.isEmpty { AppState.shared.subscriptionStatus = status }
                if !tier.isEmpty { AppState.shared.subscriptionTier = tier }
                serverStatus = status
            }
        }

        if requireActiveStatus {
            return serverStatus == "active"
        }
        return true
    }

    // MARK: - Transaction Updates

    private func handleTransactionUpdate(_ verificationResult: VerificationResult<Transaction>) async {
        guard let transaction = try? checkVerified(verificationResult) else { return }
        let verified = await verifyWithServer(transactionID: String(transaction.id))
        if verified {
            await transaction.finish()
        }
    }

    private func loadServerSubscriptionUUID() async -> UUID? {
        let contractorId = AppState.shared.contractorId
        guard !contractorId.isEmpty else {
            purchaseError = SubscriptionError.missingContractor.localizedDescription
            return nil
        }

        // Always prefer Firestore's UUID. Older app builds could cache a local
        // temporary UUID, which would make Apple's appAccountToken fail ownership checks.
        guard let profile = await APIClient.shared.getContractorProfile(contractorId: contractorId),
              let subscriptionUUID = profile["subscription_uuid"] as? String,
              let uuid = UUID(uuidString: subscriptionUUID) else {
            return nil
        }

        AppState.shared.subscriptionUUID = subscriptionUUID
        return uuid
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
