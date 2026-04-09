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

        // Set the subscription UUID as appAccountToken for server-side ownership verification
        let subscriptionUUID = AppState.shared.subscriptionUUID
        if let uuid = UUID(uuidString: subscriptionUUID) {
            purchaseOptions.insert(.appAccountToken(uuid))
        } else {
            // subscriptionUUID not yet loaded — generate and store a temporary one
            let tempUUID = UUID()
            AppState.shared.subscriptionUUID = tempUUID.uuidString
            purchaseOptions.insert(.appAccountToken(tempUUID))
        }

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
            await verifyWithServer(transactionID: String(transaction.id))
            await transaction.finish()
            return true

        case .userCancelled:
            return false

        case .pending:
            return false

        @unknown default:
            return false
        }
    }

    // MARK: - Restore Purchases

    func restorePurchases() async {
        do {
            try await AppStore.sync()
            await verifyCurrentEntitlements()
        } catch {
            purchaseError = "Restore failed: \(error.localizedDescription)"
        }
    }

    // MARK: - Server Verification

    /// Verify all current entitlements with the server. Called on app launch.
    func verifyCurrentEntitlements() async {
        for await result in Transaction.currentEntitlements {
            guard case .verified(let transaction) = result else { continue }
            await verifyWithServer(transactionID: String(transaction.id))
        }
    }

    private func verifyWithServer(transactionID: String) async {
        let success = await APIClient.shared.verifySubscription(transactionId: transactionID)
        if success {
            // Refresh subscription state from backend after successful verification
            let contractorId = AppState.shared.contractorId
            guard !contractorId.isEmpty else { return }
            if let profile = await APIClient.shared.getContractorProfile(contractorId: contractorId) {
                let status = profile["subscription_status"] as? String ?? ""
                let tier = profile["subscription_tier"] as? String ?? ""
                if !status.isEmpty { AppState.shared.subscriptionStatus = status }
                if !tier.isEmpty { AppState.shared.subscriptionTier = tier }
            }
        }
    }

    // MARK: - Transaction Updates

    private func handleTransactionUpdate(_ verificationResult: VerificationResult<Transaction>) async {
        guard let transaction = try? checkVerified(verificationResult) else { return }
        await verifyWithServer(transactionID: String(transaction.id))
        await transaction.finish()
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
