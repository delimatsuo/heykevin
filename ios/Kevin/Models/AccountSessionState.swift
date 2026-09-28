import Foundation

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
