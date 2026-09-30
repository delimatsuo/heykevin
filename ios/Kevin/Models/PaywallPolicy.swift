import Foundation
import StoreKit

enum PaywallPurpose: Hashable, Equatable, Sendable {
    case allPlans
    case businessActivation

    var allowedProductIDs: Set<String> {
        switch self {
        case .allPlans:
            return PaywallPolicy.allKnownProductIDs
        case .businessActivation:
            return PaywallPolicy.businessProductIDs
        }
    }
}

enum PaywallPolicy {
    static let personalProductID = "com.kevin.callscreen.personal.monthly"
    static let businessProductID = "com.kevin.callscreen.business.monthly"
    static let businessProProductID = "com.kevin.callscreen.businesspro.monthly"

    static let allKnownProductIDs: Set<String> = [
        personalProductID,
        businessProductID,
        businessProProductID,
    ]

    static let businessProductIDs: Set<String> = [
        businessProductID,
        businessProProductID,
    ]

    static func isEligible(productID: String, for purpose: PaywallPurpose) -> Bool {
        purpose.allowedProductIDs.contains(productID)
    }

    static func filterEligibleProductIDs(_ productIDs: [String], for purpose: PaywallPurpose) -> [String] {
        productIDs.filter { isEligible(productID: $0, for: purpose) }
    }

    static func filterEligibleProducts(_ products: [Product], for purpose: PaywallPurpose) -> [Product] {
        products.filter { isEligible(productID: $0.id, for: purpose) }
    }

    static func resolveSelectedProductID(
        selectedID: String?,
        preferredID: String?,
        eligibleIDs: [String]
    ) -> String? {
        if let selectedID, eligibleIDs.contains(selectedID) {
            return selectedID
        }
        if let preferredID, eligibleIDs.contains(preferredID) {
            return preferredID
        }
        return eligibleIDs.first
    }
}
