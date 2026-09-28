import Foundation

/// Observable lifecycle and authentication readiness state for an account session.
enum AccountSessionState: Equatable, Sendable {
    /// Initial unverified state before secure storage and keychain are checked.
    case uninitialized
    /// Clean install or logged out user who has not completed onboarding.
    case notOnboarded
    /// Authenticated session with valid, nonempty contractor ID and API token.
    case ready
    /// Onboarded installation lacking one or both credentials (e.g. after phone migration or session expiration).
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
