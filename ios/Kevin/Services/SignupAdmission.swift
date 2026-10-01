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
    static func evaluate(countryCode: String, markets: MarketsResponse?) -> SignupAdmissionStatus {
        guard let markets else {
            return .checkFailed(message: String(localized: "Could not check availability. Try again."))
        }
        let country = countryCode.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        guard let status = markets.status(for: country) else {
            return .checkFailed(message: String(localized: "Could not check availability. Try again."))
        }

        switch status {
        case .available:
            return .allowed
        case .qualificationRequired, .unsupported:
            let message: String
            if country == "BR" {
                message = String(localized: "Kevin is not yet available in Brazil. We are preparing the service.")
            } else if country == "GB" {
                message = String(localized: "Kevin is not yet available in the United Kingdom. We are preparing the service.")
            } else {
                message = String(localized: "Kevin is not yet available in this country. We are preparing the service.")
            }
            return .unavailable(message: message)
        }
    }

    static func checkAvailability(
        countryCode: String,
        fetcher: () async -> MarketsResponse? = { await APIClient.shared.getMarkets() }
    ) async -> SignupAdmissionStatus {
        let markets = await fetcher()
        return evaluate(countryCode: countryCode, markets: markets)
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
