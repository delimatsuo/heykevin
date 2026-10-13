import Foundation

enum SignupAdmissionStatus: Equatable, Sendable {
    case allowed
    case unavailable(message: String)
    case checkFailed(message: String)

    var isAllowed: Bool {
        if case .allowed = self { return true }
        return false
    }

    var errorMessage: String? {
        switch self {
        case .allowed:
            return nil
        case .unavailable(let message), .checkFailed(let message):
            return message
        }
    }
}

struct SignupAdmissionSnapshot: Equatable, Sendable {
    let countryCode: String
    let phone: String
    let authContext: CallAuthContext
    let appleUserId: String

    init(countryCode: String, phone: String, authContext: CallAuthContext, appleUserId: String) {
        self.countryCode = countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        self.phone = phone
        self.authContext = authContext
        self.appleUserId = appleUserId
    }

    func isCurrent(countryCode: String, phone: String, authContext: CallAuthContext, appleUserId: String) -> Bool {
        self.countryCode == countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
            && self.phone == phone
            && self.authContext == authContext
            && self.appleUserId == appleUserId
    }
}

enum SignupAdmission {
    private static let availabilityCheckFailed = String(localized: "Could not check availability. Try again.")

    private static func unavailableMessage(for country: String) -> String {
        if country == "BR" {
            return String(localized: "Kevin is not yet available in Brazil. We are preparing the service.")
        }
        if country == "GB" {
            return String(localized: "Kevin is not yet available in the United Kingdom. We are preparing the service.")
        }
        return String(localized: "Kevin is not yet available in this country. We are preparing the service.")
    }

    static func evaluate(countryCode: String, markets: MarketsResponse?) -> SignupAdmissionStatus {
        guard let markets else {
            return .checkFailed(message: availabilityCheckFailed)
        }
        let country = countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        guard let status = markets.status(for: country) else {
            return .checkFailed(message: availabilityCheckFailed)
        }

        switch status {
        case .available:
            return .allowed
        case .qualificationRequired, .unsupported:
            return .unavailable(message: unavailableMessage(for: country))
        }
    }

    static func evaluate(countryCode: String, result: MarketsFetchResult) -> SignupAdmissionStatus {
        switch result {
        case .catalog(let markets):
            return evaluate(countryCode: countryCode, markets: markets)
        case .legacyUSOnly:
            let country = countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
            // This preserves the only onboarding path supported by the backend
            // before market admission existed. It is deliberately not a generic
            // fallback for a failed availability check.
            return country == "US" ? .allowed : .unavailable(message: unavailableMessage(for: country))
        case .failed:
            return .checkFailed(message: availabilityCheckFailed)
        }
    }

    static func checkAvailability(
        countryCode: String,
        fetcher: () async -> MarketsFetchResult = { await APIClient.shared.getMarkets() }
    ) async -> SignupAdmissionStatus {
        let result = await fetcher()
        return evaluate(countryCode: countryCode, result: result)
    }

    @MainActor
    static func runIfAllowed<T>(
        status: SignupAdmissionStatus,
        snapshot: SignupAdmissionSnapshot,
        current: () -> SignupAdmissionSnapshot,
        operation: () async throws -> T?
    ) async rethrows -> T? {
        guard status.isAllowed, snapshot == current() else {
            return nil
        }
        let result = try await operation()
        guard snapshot == current() else {
            return nil
        }
        return result
    }
}
