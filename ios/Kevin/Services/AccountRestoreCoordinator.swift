import Foundation

/// Exists only during one new-signup flow; restored defaults cannot create it.
struct SignupContinuation {
    let contractorId: String
    let appleUserId: String
    var explicitContactConsent = false
}

enum AccountRestoreOutcome: Equatable, Sendable {
    case success(contractorId: String, needsProvisioning: Bool)
    case newAccountNeeded
    case authFailed(String)
    case notFound(String)
    case failed(String)
    case superseded
}

/// Verified lookup and profile are staged before any account state is published.
/// The session epoch and attempt revision fence every suspension point.
@MainActor
final class AccountRestoreCoordinator {
    static let shared = AccountRestoreCoordinator()
    typealias LookupHandler = (String, String) async throws -> AppleLookupResult
    typealias ProfileFetcher = (String, String) async -> [String: Any]?
    private let state: AppState
    private let lookup: LookupHandler
    private let fetchProfile: ProfileFetcher
    private let saveCredentials: ([String: String], AccountRecoveryCheckpoint) -> Bool
    private let reconcileNotifications: () async -> Void
    private let syncContacts: (String) async -> Void
    private(set) var activeAttemptRevision = 0
    private(set) var isBusy = false

    init(state: AppState? = nil,
         lookupHandler: @escaping LookupHandler = { try await APIClient.shared.findContractorByAppleId(appleUserId: $0, appleIdentityToken: $1) },
         profileFetcher: @escaping ProfileFetcher = { await APIClient.shared.getContractorProfile(contractorId: $0, bearerToken: $1) },
         credentialSaver: (([String: String], AccountRecoveryCheckpoint) -> Bool)? = nil,
         notificationReconciler: @escaping () async -> Void = { await PushRegistrationCoordinator.shared.handleAccountReady() },
         contactSyncHandler: @escaping (String) async -> Void = { _ = await ContactSyncManager.shared.syncContacts(contractorId: $0, force: true) }) {
        let state = state ?? AppState.shared
        self.state = state
        self.lookup = lookupHandler
        self.fetchProfile = profileFetcher
        self.saveCredentials = credentialSaver ?? { state.persistRecoveredCredentials($0, checkpoint: $1) }
        self.reconcileNotifications = notificationReconciler
        self.syncContacts = contactSyncHandler
    }

    func beginAttempt() -> Int? {
        guard !isBusy else { return nil }
        isBusy = true
        activeAttemptRevision += 1
        return activeAttemptRevision
    }

    func cancelAttempt() {
        isBusy = false
        activeAttemptRevision += 1
    }

    /// The create endpoint can discover a returning account by phone. Its
    /// response is a hint to re-verify identity, never permission to open UI.
    func restoreBootstrapResponse(_ response: [String: Any]?, appleUserId: String,
                                  appleIdentityToken: String, retainedContractorId: String = "",
                                  retainedAppleUserId: String = "", retainedKevinNumber: String = "",
                                  signupContinuation: SignupContinuation? = nil,
                                  attemptRevision: Int) async -> AccountRestoreOutcome {
        defer { if activeAttemptRevision == attemptRevision { isBusy = false } }
        guard !Task.isCancelled, activeAttemptRevision == attemptRevision, isBusy else { return .superseded }
        guard let response, response["status"] as? String == "ok",
              let id = response["contractor_id"] as? String, !id.isEmpty,
              let token = response["api_token"] as? String, !token.isEmpty,
              response["existing"] == nil || response["existing"] is Bool else {
            return .failed(String(localized: "Could not reconnect your account. Please try again."))
        }
        guard response["existing"] as? Bool == true else {
            if state.pendingAccountRecovery != nil {
                return .failed(String(localized: "Could not reconnect your account. Please try again."))
            }
            return .newAccountNeeded
        }
        return await restoreAccount(appleUserId: appleUserId, appleIdentityToken: appleIdentityToken,
            isRecoveryMode: false, retainedContractorId: retainedContractorId,
            retainedAppleUserId: retainedAppleUserId, retainedKevinNumber: retainedKevinNumber,
            expectedContractorId: id, signupContinuation: signupContinuation, attemptRevision: attemptRevision)
    }

    func restoreAccount(appleUserId: String, appleIdentityToken: String,
                        isRecoveryMode: Bool, retainedContractorId: String = "",
                        retainedAppleUserId: String = "", retainedKevinNumber: String = "",
                        expectedContractorId: String = "",
                        signupContinuation: SignupContinuation? = nil,
                        attemptRevision: Int) async -> AccountRestoreOutcome {
        let originAuth = state.currentAuthContext()
        let originApple = state.appleUserId
        let originOnboarded = state.isOnboarded
        let originSession = state.sessionState
        let originCheckpoint = state.pendingAccountRecovery
        func isCurrent() -> Bool {
            !Task.isCancelled && activeAttemptRevision == attemptRevision
                && state.currentAuthContext() == originAuth && state.appleUserId == originApple
                && state.isOnboarded == originOnboarded && state.sessionState == originSession
                && state.pendingAccountRecovery == originCheckpoint
        }
        defer { if activeAttemptRevision == attemptRevision { isBusy = false } }
        let failure = AccountRestoreOutcome.failed(String(localized: "Could not reconnect your account. Please try again."))
        let wrongAccount = AccountRestoreOutcome.failed(String(localized: "Sign in with the Apple account originally used for Kevin."))
        guard isCurrent() else { return .superseded }
        guard !appleUserId.isEmpty, !appleIdentityToken.isEmpty else {
            return .authFailed(String(localized: "Sign in expired. Please tap Sign in with Apple again to continue."))
        }
        if isRecoveryMode, !retainedAppleUserId.isEmpty, retainedAppleUserId != appleUserId { return wrongAccount }
        if let originCheckpoint, !originCheckpoint.appleUserId.isEmpty,
           originCheckpoint.appleUserId != appleUserId { return wrongAccount }

        let result: AppleLookupResult
        do { result = try await lookup(appleUserId, appleIdentityToken) }
        catch BootstrapAuthError.unauthenticated {
            guard isCurrent() else { return .superseded }
            return .authFailed(String(localized: "Sign in expired. Please tap Sign in with Apple again to continue."))
        } catch {
            return isCurrent() ? failure : .superseded
        }
        guard isCurrent() else { return .superseded }
        let id: String
        let token: String
        switch result {
        case .notFound:
            return (isRecoveryMode || originCheckpoint != nil || !retainedContractorId.isEmpty || !retainedKevinNumber.isEmpty || !expectedContractorId.isEmpty)
                ? .notFound(String(localized: "Sign in with the Apple account originally used for Kevin."))
                : .newAccountNeeded
        case .authFailure:
            return .authFailed(String(localized: "Sign in expired. Please tap Sign in with Apple again to continue."))
        case .failure: return failure
        case .found(let account, let credential): id = account; token = credential
        }
        guard !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return failure }
        if isRecoveryMode, !retainedContractorId.isEmpty, id != retainedContractorId { return wrongAccount }
        if !expectedContractorId.isEmpty, id != expectedContractorId { return wrongAccount }
        if let originCheckpoint, !originCheckpoint.contractorId.isEmpty,
           originCheckpoint.contractorId != id { return wrongAccount }
        if let signupContinuation,
           signupContinuation.contractorId != id || signupContinuation.appleUserId != appleUserId { return wrongAccount }
        guard let profile = await fetchProfile(id, token) else { return isCurrent() ? failure : .superseded }
        guard isCurrent() else { return .superseded }
        guard profile["contractor_id"] as? String == id,
              profile["active"] as? Bool == true,
              let status = profile["subscription_status"] as? String,
              ["trial", "active", "expired", "cancelled"].contains(status),
              let tier = profile["subscription_tier"] as? String,
              ["none", "personal", "business", "businessPro"].contains(tier) else { return failure }
        let number = profile["twilio_number"] as? String ?? ""
        let requiresExistingNumber = isRecoveryMode || originCheckpoint?.allowsUnfinishedSetup == false || !retainedKevinNumber.isEmpty
        if requiresExistingNumber {
            guard !number.isEmpty else { return .failed(String(localized: "Your existing Kevin number could not be restored. Please try again or contact support.")) }
            if !retainedKevinNumber.isEmpty, number != retainedKevinNumber { return wrongAccount }
        }
        let values = ["contractorId": id, "contractorApiToken": token, "appleUserId": appleUserId,
                      "subscriptionStatus": status, "subscriptionTier": tier,
                      "subscriptionUUID": profile["subscription_uuid"] as? String ?? ""]
        // Clear unowned restored consent before the first credential write can
        // make a staged contractor ID look like a previously trusted identity.
        let keepConsent = signupContinuation?.explicitContactConsent
            ?? (!retainedContractorId.isEmpty && retainedContractorId == id && state.contactsUploadConsent)
        state.contactsUploadConsent = keepConsent
        let checkpoint = AccountRecoveryCheckpoint(contractorId: id, appleUserId: appleUserId,
            allowsUnfinishedSetup: !requiresExistingNumber && !state.isOnboarded && number.isEmpty)
        guard saveCredentials(values, checkpoint) else {
            state.beginAccountRecovery(checkpoint: checkpoint)
            return .failed(String(localized: "Could not save your sign-in securely. Please unlock your iPhone and try again."))
        }
        // No suspension between persistence and publication. Every credential
        // and its recovery marker was verified before authenticated UI can open.
        state.userName = profile["owner_name"] as? String ?? ""
        state.businessName = profile["business_name"] as? String ?? ""
        state.businessAddress = profile["business_address"] as? String ?? ""
        state.businessCity = profile["business_city"] as? String ?? ""
        state.serviceType = profile["service_type"] as? String ?? ""
        let mode = profile["effective_mode"] as? String ?? profile["mode"] as? String ?? "personal"
        state.mode = mode == "personal" ? "personal" : "business"
        state.countryCode = SettingsCountry.accountCountry(from: profile) ?? ""
        state.subscriptionStatus = status
        state.subscriptionTier = tier
        state.subscriptionUUID = values["subscriptionUUID"] ?? ""
        state.jobberConnected = false
        state.googleCalendarConnected = false
        state.contractorId = id
        state.appleUserId = appleUserId
        state.appleIdentityToken = ""
        state.kevinNumber = number
        state.readCallIds = []
        state.unreadCallCount = 0
        state.clearActiveCall()
        if !number.isEmpty { state.isOnboarded = true }
        guard state.finishAccountRecovery(bearerToken: token) else {
            state.beginAccountRecovery(checkpoint: checkpoint)
            return .failed(String(localized: "Could not save your sign-in securely. Please unlock your iPhone and try again."))
        }
        if number.isEmpty { return .success(contractorId: id, needsProvisioning: true) }
        let restoredAuth = state.currentAuthContext()
        await reconcileNotifications()
        guard activeAttemptRevision == attemptRevision, state.currentAuthContext() == restoredAuth,
              state.sessionState == .ready, !Task.isCancelled else { return .superseded }
        if keepConsent {
            await syncContacts(id)
            guard activeAttemptRevision == attemptRevision, state.currentAuthContext() == restoredAuth,
                  state.sessionState == .ready, !Task.isCancelled else { return .superseded }
        }
        return .success(contractorId: id, needsProvisioning: false)
    }
}
