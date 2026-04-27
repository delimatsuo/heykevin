import SwiftUI
import StoreKit

struct PaywallView: View {
    /// When false (trial expired), the paywall cannot be dismissed without subscribing.
    var canDismiss: Bool = true
    /// When true, shown as the final onboarding step — skip link says "Maybe later"
    var isOnboarding: Bool = false
    /// Optional plan to preselect when the paywall opens from an upgrade path.
    var preferredProductID: String? = nil
    /// Called after a purchase has been verified by the server.
    var onSubscribed: (() -> Void)? = nil
    /// Whether trial/grace users should see the non-purchase skip link.
    var showsTrialSkip: Bool = true

    @EnvironmentObject var appState: AppState
    @ObservedObject private var subscriptionManager = SubscriptionManager.shared
    @Environment(\.dismiss) var dismiss

    @State private var isPromoEligible = false
    @State private var isCheckingPromo = true
    @State private var isPurchasing = false
    @State private var isRestoring = false
    @State private var purchaseError: String?
    @State private var restoreMessage: String?
    @State private var selectedProductID: String?

    /// The founding-member 75% off promo is only shown to users who have already
    /// completed an Apple introductory offer (i.e. trial ended / subscription expired).
    /// Per Apple rules, promotional offers are not available to users who are still
    /// in their introductory offer period, and advertising them alongside the free
    /// trial was the source of the 2.1(b) rejection.
    private var canShowFoundingMemberPromo: Bool {
        guard isPromoEligible, !isCheckingPromo else { return false }
        return appState.subscriptionStatus == "expired" || appState.subscriptionStatus == "cancelled"
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 24) {
                    // Header
                    headerSection

                    // Trial status banner (if expired)
                    if appState.subscriptionStatus == "expired" {
                        expiredBanner
                    }

                    // Founding-member promo banner — only for expired/cancelled users
                    if canShowFoundingMemberPromo {
                        promoBadge
                    }

                    // Tier cards
                    if subscriptionManager.isLoading {
                        ProgressView(String(localized: "Loading plans..."))
                            .padding(.vertical, 40)
                    } else if subscriptionManager.products.isEmpty {
                        VStack(spacing: 12) {
                            Text(String(localized: "Could not load plans."))
                                .foregroundStyle(.secondary)
                            if let err = subscriptionManager.fetchError {
                                Text(err)
                                    .font(.caption)
                                    .foregroundStyle(.red)
                                    .multilineTextAlignment(.center)
                                    .padding(.horizontal)
                            }
                            Button(String(localized: "Try Again")) {
                                Task { await loadData() }
                            }
                        }
                        .padding(.vertical, 40)
                    } else {
                        ForEach(subscriptionManager.products, id: \.id) { product in
                            TierCard(
                                product: product,
                                showFoundingMemberPromo: canShowFoundingMemberPromo,
                                isSelected: selectedProductID == product.id,
                                onSelect: { selectedProductID = product.id }
                            )
                        }
                    }

                    // CTA Button
                    if !subscriptionManager.products.isEmpty {
                        purchaseButton
                    }

                    // Skip — only during active trial/grace period
                    if canDismiss && showsTrialSkip && appState.subscriptionStatus == "trial" {
                        Button(isOnboarding
                               ? String(localized: "Maybe later")
                               : String(localized: "Continue without subscribing")) {
                            if isOnboarding {
                                appState.isOnboarded = true
                            }
                            dismiss()
                        }
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                    }

                    // Cancel Forwarding
                    cancelForwardingButton

                    // Restore Purchases
                    Button {
                        Task {
                            isRestoring = true
                            restoreMessage = nil
                            purchaseError = nil
                            let statusBefore = appState.subscriptionStatus
                            await subscriptionManager.restorePurchases()
                            isRestoring = false
                            if appState.subscriptionStatus == "active" || appState.subscriptionStatus == "trial" {
                                if statusBefore != appState.subscriptionStatus {
                                    dismiss()
                                } else {
                                    restoreMessage = "Subscription restored."
                                }
                            } else if let err = subscriptionManager.purchaseError, !err.isEmpty {
                                purchaseError = err
                            } else {
                                restoreMessage = "No active subscription found for this Apple ID."
                            }
                        }
                    } label: {
                        if isRestoring {
                            ProgressView().scaleEffect(0.8)
                        } else {
                            Text("Restore Purchases")
                        }
                    }
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .disabled(isRestoring)

                    if let msg = restoreMessage {
                        Text(msg)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }

                    if let error = purchaseError {
                        Text(error)
                            .font(.caption)
                            .foregroundStyle(.red)
                            .multilineTextAlignment(.center)
                    }

                    // Legal footnote
                    VStack(spacing: 6) {
                        Text("All plans are auto-renewing monthly subscriptions. New subscribers receive a 2-week free trial — you will not be charged until the trial ends. Payment is charged to your Apple ID at the end of the free trial (or at confirmation of purchase if no trial applies). Subscriptions automatically renew unless cancelled at least 24 hours before the end of the current period. Manage or cancel in Settings > Apple ID > Subscriptions.")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .multilineTextAlignment(.center)

                        HStack(spacing: 16) {
                            Link("Privacy Policy", destination: URL(string: "https://heykevin.one/privacy")!)
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                            Link("Terms of Use", destination: URL(string: "https://heykevin.one/terms")!)
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                        }
                    }
                    .padding(.horizontal)
                }
                .padding()
            }
            .navigationTitle("Hey Kevin")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if canDismiss {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button(String(localized: "Done")) {
                            if isOnboarding {
                                appState.isOnboarded = true
                            }
                            dismiss()
                        }
                    }
                }
            }
            .task {
                await loadData()
            }
            .interactiveDismissDisabled(!canDismiss)
        }
    }

    // MARK: - Sections

    private var headerSection: some View {
        VStack(spacing: 12) {
            ZStack {
                Circle()
                    .fill(LinearGradient(
                        colors: [.blue, .purple, .pink],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    ))
                    .frame(width: 72, height: 72)
                Text("K")
                    .font(.system(size: 36, weight: .bold))
                    .foregroundStyle(.white)
            }

            Text(isOnboarding ? "Choose Your Plan" : "Hey Kevin")
                .font(.title2.bold())

            Text(isOnboarding
                 ? "Start with a 2-week free trial.\nCancel anytime before it ends — no charge."
                 : "AI call screening that learns your business.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
    }

    private var expiredBanner: some View {
        HStack {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Text("Your trial has expired. Subscribe to restore AI screening.")
                .font(.subheadline)
                .foregroundStyle(.primary)
        }
        .padding()
        .background(Color.orange.opacity(0.12))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private var promoBadge: some View {
        HStack {
            Image(systemName: "tag.fill")
                .foregroundStyle(.green)
            VStack(alignment: .leading, spacing: 2) {
                Text("Founding Member Offer")
                    .font(.subheadline.bold())
                Text("75% off for 3 months — first 1,000 users only")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding()
        .background(Color.green.opacity(0.1))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private var purchaseButton: some View {
        Button {
            guard let productID = selectedProductID ?? subscriptionManager.products.first?.id else { return }
            guard let product = subscriptionManager.products.first(where: { $0.id == productID }) else { return }
            isPurchasing = true
            purchaseError = nil
            Task {
                do {
                    // Only attach the promo offer for users who have already completed an
                    // intro offer (expired/cancelled). Users in trial or new users go
                    // through the standard StoreKit introductory offer (2-week free trial).
                    let offerID = canShowFoundingMemberPromo ? promoOfferID(for: product.id) : nil
                    let purchased = try await subscriptionManager.purchase(product, offerID: offerID)
                    if purchased {
                        isPurchasing = false
                        onSubscribed?()
                        if isOnboarding {
                            appState.isOnboarded = true
                        }
                        dismiss()
                        return
                    }
                    if let err = subscriptionManager.purchaseError, !err.isEmpty {
                        purchaseError = err
                    }
                } catch {
                    // If promo offer was rejected, fall back to regular price automatically
                    if canShowFoundingMemberPromo {
                        isPromoEligible = false
                        purchaseError = "Promo offer unavailable for your account — plans now shown at regular price. Tap the button again to subscribe."
                    } else {
                        purchaseError = error.localizedDescription
                    }
                }
                isPurchasing = false
            }
        } label: {
            if isPurchasing {
                ProgressView()
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            } else {
                let product = subscriptionManager.products.first(where: { $0.id == (selectedProductID ?? subscriptionManager.products.first?.id ?? "") })
                Text(purchaseButtonTitle(for: product))
                    .font(.headline)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
            }
        }
        .buttonStyle(.borderedProminent)
        .disabled(isPurchasing || subscriptionManager.products.isEmpty)
        .clipShape(RoundedRectangle(cornerRadius: 14))
    }

    private func purchaseButtonTitle(for product: Product?) -> String {
        guard let product else { return String(localized: "Subscribe") }
        let price = product.displayPrice

        // Expired/cancelled user eligible for founding-member promo: apply the
        // 75%-off-for-3-months promotional offer at purchase time.
        if canShowFoundingMemberPromo {
            let discounted = (product.price / 4).rounded(toPlaces: 2)
            let discountedStr = "$\(String(format: "%.2f", NSDecimalNumber(decimal: discounted).doubleValue))"
            return String(
                format: String(localized: "Subscribe — %1$@/mo for 3 months, then %2$@/mo"),
                discountedStr, price
            )
        }

        // Trial / new users: Apple's configured introductory offer (2-week free
        // trial) is presented by StoreKit at purchase confirmation.
        if appState.subscriptionStatus == "trial" || appState.subscriptionStatus.isEmpty || isOnboarding {
            return String(
                format: String(localized: "Try Free for 2 Weeks — then %@/mo"),
                price
            )
        }

        return String(format: String(localized: "Subscribe — %@/mo"), price)
    }

    private var cancelForwardingButton: some View {
        Button {
            // Deactivate — must match the activate code exactly.
            // Verizon: *73 (cancels forwarding). GSM: ##61# (cancels no-answer).
            let code = appState.isVerizonCarrier
                ? "tel:*73"
                : "tel:%23%2361%23"
            if let url = URL(string: code) {
                UIApplication.shared.open(url)
            }
        } label: {
            HStack {
                Image(systemName: "phone.slash")
                    .foregroundStyle(.red)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Cancel Forwarding")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.red)
                    Text("Stop routing calls through Kevin")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .foregroundStyle(.tertiary)
                    .font(.caption)
            }
            .padding()
            .background(Color(.systemGray6))
            .clipShape(RoundedRectangle(cornerRadius: 12))
        }
        .buttonStyle(.plain)
    }

    // MARK: - Helpers

    private func promoOfferID(for productID: String) -> String {
        switch productID {
        case "com.kevin.callscreen.personal.monthly":    return "founding_member_75off_personal"
        case "com.kevin.callscreen.business.monthly":   return "founding_member_75off_business"
        case "com.kevin.callscreen.businesspro.monthly": return "founding_member_75off"
        default: return "founding_member_75off"
        }
    }

    // MARK: - Load Data

    private func loadData() async {
        await subscriptionManager.fetchProducts()
        selectedProductID = subscriptionManager.products.first(where: { $0.id == preferredProductID })?.id
            ?? subscriptionManager.products.first?.id

        // Check promo eligibility in parallel with product fetch result
        if !appState.contractorId.isEmpty {
            isCheckingPromo = true
            let eligible = await APIClient.shared.checkPromoEligibility(contractorId: appState.contractorId)
            isPromoEligible = eligible
            isCheckingPromo = false
        } else {
            isCheckingPromo = false
        }
    }
}

// MARK: - Tier Card

private struct TierCard: View {
    let product: Product
    let showFoundingMemberPromo: Bool
    let isSelected: Bool
    let onSelect: () -> Void

    private var tierName: String {
        switch product.id {
        case "com.kevin.callscreen.personal.monthly": return "Personal"
        case "com.kevin.callscreen.business.monthly": return "Business"
        case "com.kevin.callscreen.businesspro.monthly": return "Business Pro"
        default: return product.displayName
        }
    }

    private var tierFeatures: [String] {
        switch product.id {
        case "com.kevin.callscreen.personal.monthly":
            return ["AI call screening", "Live transcript", "Post-call SMS", "Contact ring-through"]
        case "com.kevin.callscreen.business.monthly":
            return ["Everything in Personal", "Business hours / after-hours", "Business knowledge base"]
        case "com.kevin.callscreen.businesspro.monthly":
            return ["Everything in Business", "Jobber integration", "Google Calendar integration", "AI price estimates"]
        default:
            return []
        }
    }

    private var accentColor: Color {
        switch product.id {
        case "com.kevin.callscreen.personal.monthly": return .blue
        case "com.kevin.callscreen.business.monthly": return .purple
        case "com.kevin.callscreen.businesspro.monthly": return .orange
        default: return .blue
        }
    }

    // 75% off promotional price
    private var promoPrice: String {
        let price = product.price
        let discounted = (price / 4).rounded(toPlaces: 2)
        return "$\(String(format: "%.2f", NSDecimalNumber(decimal: discounted).doubleValue))"
    }

    var body: some View {
        Button(action: onSelect) {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(tierName)
                            .font(.headline)
                        HStack(spacing: 6) {
                            if showFoundingMemberPromo {
                                Text(product.displayPrice)
                                    .font(.subheadline)
                                    .strikethrough()
                                    .foregroundStyle(.secondary)
                                Text("\(promoPrice)/mo")
                                    .font(.subheadline.bold())
                                    .foregroundStyle(.green)
                                Text("for 3 months")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            } else {
                                Text("\(product.displayPrice)/mo")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    Spacer()
                    Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                        .foregroundStyle(isSelected ? accentColor : .secondary)
                        .font(.title3)
                }

                Divider()

                ForEach(tierFeatures, id: \.self) { feature in
                    HStack(spacing: 8) {
                        Image(systemName: "checkmark")
                            .foregroundStyle(accentColor)
                            .font(.caption.bold())
                        Text(feature)
                            .font(.subheadline)
                            .foregroundStyle(.primary)
                    }
                }
            }
            .padding()
            .background(isSelected ? accentColor.opacity(0.08) : Color(.systemGray6))
            .clipShape(RoundedRectangle(cornerRadius: 14))
            .overlay(
                RoundedRectangle(cornerRadius: 14)
                    .stroke(isSelected ? accentColor : Color.clear, lineWidth: 2)
            )
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Decimal rounding helper

private extension Decimal {
    func rounded(toPlaces places: Int) -> Decimal {
        var result = Decimal()
        var copy = self
        NSDecimalRound(&result, &copy, places, .plain)
        return result
    }
}
