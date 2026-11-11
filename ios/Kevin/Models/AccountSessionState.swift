import Foundation

/// Validated contractor profile payload decoded at the session/API boundary.
/// Enforces exact UTF-8 byte comparison for contractor identity so canonically
/// equivalent but byte-distinct Unicode strings are never conflated.
struct ContractorProfilePayload {
    let contractorId: String
    let rawProfile: [String: Any]
    let active: Bool?
    let subscriptionStatus: String?
    let subscriptionTier: String?
    let subscriptionUUIDString: String?
    let parsedSubscriptionUUID: UUID?
    let twilioNumber: String
    var nonemptyTwilioNumber: String? { twilioNumber.isEmpty ? nil : twilioNumber }
    let ownerName: String
    let businessName: String
    let businessAddress: String
    let businessCity: String
    let serviceType: String
    let ownerPhone: String?
    let knowledge: String?
    let autoReplySms: Bool
    let resolvedMode: String
    let accountCountry: String?
    let serviceBinding: ServiceBinding?

    init?(profile: [String: Any], expectedContractorId: String? = nil) {
        func nonemptyString(_ key: String) -> String? {
            guard let value = profile[key] as? String, !value.isEmpty else { return nil }
            return value
        }

        guard let cid = nonemptyString("contractor_id") else { return nil }
        if let expected = expectedContractorId {
            guard !expected.isEmpty, cid.utf8.elementsEqual(expected.utf8) else { return nil }
        }

        self.contractorId = cid
        self.rawProfile = profile
        self.active = profile["active"] as? Bool
        self.subscriptionStatus = nonemptyString("subscription_status")
        self.subscriptionTier = nonemptyString("subscription_tier")
        let rawUUID = nonemptyString("subscription_uuid")
        self.subscriptionUUIDString = rawUUID
        self.parsedSubscriptionUUID = rawUUID.flatMap(UUID.init(uuidString:))
        self.twilioNumber = profile["twilio_number"] as? String ?? ""
        self.ownerName = profile["owner_name"] as? String ?? ""
        self.businessName = profile["business_name"] as? String ?? ""
        self.businessAddress = profile["business_address"] as? String ?? ""
        self.businessCity = profile["business_city"] as? String ?? ""
        self.serviceType = profile["service_type"] as? String ?? ""
        self.ownerPhone = nonemptyString("owner_phone")
        self.knowledge = profile["knowledge"] as? String
        self.autoReplySms = profile["auto_reply_sms"] as? Bool ?? false
        let mode = profile["effective_mode"] as? String ?? profile["mode"] as? String ?? "personal"
        self.resolvedMode = mode == "personal" ? "personal" : "business"
        self.accountCountry = SettingsCountry.accountCountry(from: profile)
        self.serviceBinding = ServiceBindingParser.parse(from: profile)
    }

    static func matchesOptionalContractorId(
        in profile: [String: Any],
        expectedContractorId: String
    ) -> Bool {
        guard !expectedContractorId.isEmpty else { return false }
        if !profile.keys.contains("contractor_id") {
            return true
        }
        guard let cid = profile["contractor_id"] as? String,
              !cid.isEmpty,
              cid.utf8.elementsEqual(expectedContractorId.utf8) else {
            return false
        }
        return true
    }
}

/// The two independently supplied subscription fields in a contractor profile.
struct SubscriptionProfileUpdate {
    let contractorId: String
    let status: String?
    let tier: String?

    init?(profile: [String: Any]) {
        guard let payload = ContractorProfilePayload(profile: profile) else { return nil }
        self.contractorId = payload.contractorId
        self.status = payload.subscriptionStatus
        self.tier = payload.subscriptionTier
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
