import Foundation

/// The two independently supplied subscription fields in a contractor profile.
struct SubscriptionProfileUpdate {
    let contractorId: String
    let status: String?
    let tier: String?

    init?(profile: [String: Any]) {
        func nonemptyString(_ key: String) -> String? {
            guard let value = profile[key] as? String, !value.isEmpty else { return nil }
            return value
        }

        guard let contractorId = nonemptyString("contractor_id") else { return nil }
        self.contractorId = contractorId
        status = nonemptyString("subscription_status")
        tier = nonemptyString("subscription_tier")
    }
}

/// One atomic Keychain value preserves the verified recovery target even if
/// the process stops before writing the first individual credential.
struct AccountRecoveryCheckpoint: Codable, Equatable {
    let contractorId: String
    let appleUserId: String
    let allowsUnfinishedSetup: Bool
}

/// Observable lifecycle and authentication readiness state for an account session.
enum AccountSessionState: Equatable, Sendable {
    /// Initial unverified state before secure storage and keychain are checked.
    case uninitialized
    /// Clean install or logged out user who has not completed onboarding.
    case notOnboarded
    /// Authenticated session with valid, nonempty contractor ID and API token.
    case ready
    /// Missing credentials or an interrupted existing-account restore.
    case needsRecovery
    /// Secure storage is temporarily locked or inaccessible (e.g. before first device unlock).
    case storageUnavailable(reason: String)

    var isReady: Bool {
        self == .ready
    }

    var isUnavailable: Bool {
        if case .storageUnavailable = self { return true }
        return false
    }

    var needsRecovery: Bool {
        self == .needsRecovery
    }
}
