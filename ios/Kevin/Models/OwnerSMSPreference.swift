import Foundation
import CoreFoundation

/// Helper for strict boolean and integer type inspection on deserialized JSON objects.
enum OwnerSMSParsingHelper {
    /// Returns true/false only if the value is a true boolean (CFBoolean), not a numeric 0/1 or string.
    static func strictBool(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
            return nil
        }
        return number.boolValue
    }

    /// Returns an integer only if the value is a numeric NSNumber and NOT a CFBoolean.
    static func strictInt(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else {
            return nil
        }
        let numeric = number.doubleValue
        guard numeric.isFinite, numeric.rounded(.towardZero) == numeric,
              numeric >= Double(Int.min), numeric < Double(Int.max) else { return nil }
        return Int(numeric)
    }
}

/// Result of a typed PATCH update for owner SMS preferences.
enum OwnerSMSPatchResult: Equatable, Sendable {
    case success(enabled: Bool, optedOut: Bool, revision: Int, version: Int)
    case unsupported
    case failure(String)
}

/// Pure parser for profile GET and preference PATCH payloads.
enum OwnerSMSResponseParser {
    /// Parse PATCH response requiring version 1 and explicit strict boolean fields.
    static func parsePatchResponse(data: Data, response: HTTPURLResponse) -> OwnerSMSPatchResult {
        guard response.statusCode == 200 else {
            return .failure(String(localized: "Could not save SMS notifications. Please try again."))
        }

        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return .failure(String(localized: "Invalid response from server."))
        }

        guard json["status"] as? String == "ok" else {
            return .failure(String(localized: "Could not save SMS notifications. Please try again."))
        }

        // Require a confirmed response from a server supporting this setting.
        guard let version = OwnerSMSParsingHelper.strictInt(json["owner_sms_settings_version"]), version == 1 else {
            return .unsupported
        }

        guard let enabled = OwnerSMSParsingHelper.strictBool(json["owner_sms_enabled"]),
              let optedOut = OwnerSMSParsingHelper.strictBool(json["owner_sms_opted_out"]) else {
            return .unsupported
        }

        guard let revision = OwnerSMSParsingHelper.strictInt(json["owner_sms_opt_out_revision"]), revision >= 0 else {
            return .unsupported
        }
        return .success(enabled: enabled, optedOut: optedOut, revision: revision, version: version)
    }

    /// Consent requires explicit server confirmation; a generic preference ACK is insufficient.
    static func parseConsentResponse(data: Data, response: HTTPURLResponse) -> OwnerSMSPatchResult {
        guard let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              OwnerSMSParsingHelper.strictBool(json["owner_sms_consent_required"]) == false,
              OwnerSMSParsingHelper.strictBool(json["owner_sms_enabled"]) == true,
              OwnerSMSParsingHelper.strictBool(json["owner_sms_opted_out"]) == false else {
            return .failure(String(localized: "SMS consent was not confirmed. Refresh settings and try again."))
        }
        return parsePatchResponse(data: data, response: response)
    }

    /// Parse profile dictionary from GET /api/contractors/{id}.
    /// - owner_sms_enabled: absent defaults true; present non-bool fails closed (false).
    /// - owner_sms_opted_out: absent defaults false; present non-bool fails closed (true/blocked).
    /// - owner_sms_opt_out_revision: integer >= 0, default 0.
    /// - owner_sms_settings_version: integer == 1.
    static func parseProfile(dict: [String: Any]) -> (enabled: Bool, optedOut: Bool, revision: Int, version: Int, isVersioned: Bool) {
        let enabled: Bool
        if dict["owner_sms_enabled"] == nil {
            enabled = true
        } else if let b = OwnerSMSParsingHelper.strictBool(dict["owner_sms_enabled"]) {
            enabled = b
        } else {
            enabled = false // Malformed fails closed
        }

        let optedOut: Bool
        if dict["owner_sms_opted_out"] == nil {
            optedOut = false
        } else if let b = OwnerSMSParsingHelper.strictBool(dict["owner_sms_opted_out"]) {
            optedOut = b
        } else {
            optedOut = true // Malformed fails closed (carrier blocked)
        }

        let revision: Int
        if let rev = OwnerSMSParsingHelper.strictInt(dict["owner_sms_opt_out_revision"]), rev >= 0 {
            revision = rev
        } else {
            revision = 0
        }

        let version = OwnerSMSParsingHelper.strictInt(dict["owner_sms_settings_version"]) ?? 0
        let isVersioned = version == 1
            && OwnerSMSParsingHelper.strictBool(dict["owner_sms_enabled"]) != nil
            && OwnerSMSParsingHelper.strictBool(dict["owner_sms_opted_out"]) != nil
            && OwnerSMSParsingHelper.strictInt(dict["owner_sms_opt_out_revision"]).map { $0 >= 0 } == true

        return (enabled: enabled, optedOut: optedOut, revision: revision, version: version, isVersioned: isVersioned)
    }
}

/// Account-bound state container for owner SMS notifications toggle and carrier opt-out state.
struct OwnerSMSPreferenceState: Equatable, Sendable {
    var boundAuth: CallAuthContext
    var confirmedEnabled: Bool
    var carrierOptedOut: Bool
    var optOutRevision: Int
    var settingsVersion: Int
    var isLoaded: Bool
    var isSaving: Bool
    var saveError: String
    var loadError: String
    var isUnsupportedBackend: Bool
    var fenceRevision: Int
    private(set) var requestedEnabled: Bool?
    private(set) var consentRequired = false
    private(set) var consentCountry = ""
    private(set) var ownerPhoneE164 = ""
    private(set) var senderNumber = ""

    init(
        boundAuth: CallAuthContext = CallAuthContext(contractorId: "", bearerToken: "", generation: 0),
        confirmedEnabled: Bool = true,
        carrierOptedOut: Bool = false,
        optOutRevision: Int = 0,
        settingsVersion: Int = 1,
        isLoaded: Bool = false,
        isSaving: Bool = false,
        saveError: String = "",
        loadError: String = "",
        isUnsupportedBackend: Bool = false,
        fenceRevision: Int = 0
    ) {
        self.boundAuth = boundAuth
        self.confirmedEnabled = confirmedEnabled
        self.carrierOptedOut = carrierOptedOut
        self.optOutRevision = optOutRevision
        self.settingsVersion = settingsVersion
        self.isLoaded = isLoaded
        self.isSaving = isSaving
        self.saveError = saveError
        self.loadError = loadError
        self.isUnsupportedBackend = isUnsupportedBackend
        self.fenceRevision = fenceRevision
    }

    /// User-visible state of the SMS notifications switch.
    var displayedEnabled: Bool {
        consentRequired ? false : (isSaving ? (requestedEnabled ?? confirmedEnabled) : confirmedEnabled)
    }

    /// Whether texts are blocked at the carrier/provider level.
    var isCarrierBlocked: Bool {
        carrierOptedOut
    }

    /// Primary banner message when carrier/provider blocked.
    static let carrierBlockedNotice = String(localized: "Texts stopped. Reply START to your Kevin number to receive texts again.")

    /// Subtitle explanation for SMS notifications.
    static let summaryDescription = String(localized: "Receive call summaries and account updates by text. Summaries arrive after calls end.")

    /// Explanation that the app switch must also be on.
    static let appSwitchRequiredNotice = String(localized: "The switch above must also be turned on to receive texts.")

    /// Begin a save operation. Returns operation token (fenceRevision) if permitted, or nil if invalid/busy.
    mutating func beginSave(targetEnabled: Bool, auth: CallAuthContext) -> (operationToken: Int, previous: Bool)? {
        guard auth.isValid, auth == boundAuth, isLoaded, !isUnsupportedBackend, !isSaving else { return nil }
        guard !targetEnabled || !consentRequired else { return nil }
        requestedEnabled = targetEnabled
        fenceRevision += 1
        isSaving = true
        saveError = ""
        let previous = confirmedEnabled
        return (operationToken: fenceRevision, previous: previous)
    }

    /// Finish a save operation with the parsed result. Stale or cross-account operations are safely ignored.
    mutating func finishSave(result: OwnerSMSPatchResult, operationToken: Int, auth: CallAuthContext) {
        guard auth.isValid, auth == boundAuth, operationToken == fenceRevision, isSaving else {
            return
        }
        isSaving = false
        fenceRevision += 1

        switch result {
        case .success(let enabled, let optedOut, let revision, let version):
            guard version == 1, revision >= 0 else {
                isUnsupportedBackend = true
                isLoaded = false
                loadError = Self.unavailableNotice
                return
            }
            confirmedEnabled = enabled
            carrierOptedOut = optedOut
            optOutRevision = revision
            settingsVersion = version
            saveError = enabled == requestedEnabled ? "" : String(localized: "Could not save SMS notifications. Please try again.")
            if enabled == requestedEnabled { requestedEnabled = nil }
            isUnsupportedBackend = false
            isLoaded = true
            loadError = ""
        case .unsupported:
            saveError = Self.unavailableNotice
            loadError = Self.unavailableNotice
            isUnsupportedBackend = true
            isLoaded = false
        case .failure(let message):
            saveError = message
        }
    }

    /// Capture a load token before initiating an asynchronous profile fetch.
    mutating func captureLoadToken(auth: CallAuthContext) -> Int? {
        guard auth.isValid else { return nil }
        if auth != boundAuth { reset(auth: auth) }
        guard !isSaving else { return nil }
        fenceRevision += 1
        return fenceRevision
    }

    /// Apply profile GET hydration if the load token is still current and auth matches.
    mutating func applyHydration(dict: [String: Any], token: Int, auth: CallAuthContext) {
        guard auth.isValid, auth == boundAuth, token == fenceRevision, !isSaving else {
            return // Stale GET result or cross-account response arriving after newer toggle/auth change
        }
        let parsed = OwnerSMSResponseParser.parseProfile(dict: dict)
        guard parsed.isVersioned else {
            isLoaded = false
            isUnsupportedBackend = true
            loadError = Self.unavailableNotice
            return
        }
        consentCountry = ForwardingCountry.resolve(
            serviceBinding: ServiceBindingParser.parse(from: dict),
            accountCountry: dict["country_code"] as? String,
            assignedNumber: dict["twilio_number"] as? String,
            hasAssignedNumber: true
        )
        // Missing/malformed Canadian consent metadata must never inherit legacy true.
        consentRequired = consentCountry == "CA"
            ? (OwnerSMSParsingHelper.strictBool(dict["owner_sms_consent_required"]) ?? true)
            : (OwnerSMSParsingHelper.strictBool(dict["owner_sms_consent_required"]) ?? false)
        // Match server identity precedence: a present canonical companion is
        // authoritative, including when malformed. Never fall back around it.
        let ownerIdentity = dict.keys.contains("owner_phone_e164")
            ? dict["owner_phone_e164"] as? String
            : dict["owner_phone"] as? String
        ownerPhoneE164 = ownerIdentity.flatMap { Self.isE164($0) ? $0 : nil } ?? ""
        senderNumber = dict["twilio_number"] as? String ?? ""
        confirmedEnabled = parsed.enabled
        carrierOptedOut = parsed.optedOut
        optOutRevision = parsed.revision
        settingsVersion = parsed.version
        isLoaded = true
        loadError = ""
        isUnsupportedBackend = false
        if requestedEnabled == confirmedEnabled {
            requestedEnabled = nil
            saveError = ""
        }
    }

    /// The confirmation binds disclosure acceptance to one freshly hydrated identity.
    func consentSnapshot(auth: CallAuthContext) -> OwnerSMSConsentSnapshot? {
        guard auth.isValid, auth == boundAuth, isLoaded, !isSaving, !isUnsupportedBackend,
              consentCountry == "CA", consentRequired, !carrierOptedOut,
              Self.isE164(ownerPhoneE164), Self.isE164(senderNumber) else { return nil }
        return OwnerSMSConsentSnapshot(auth: auth, ownerPhoneE164: ownerPhoneE164,
                                       twilioNumber: senderNumber, expectedRevision: optOutRevision,
                                       fenceRevision: fenceRevision)
    }

    func matchesConsent(_ snapshot: OwnerSMSConsentSnapshot, auth: CallAuthContext) -> Bool {
        consentSnapshot(auth: auth) == snapshot
    }

    mutating func beginConsent(_ snapshot: OwnerSMSConsentSnapshot, auth: CallAuthContext) -> Int? {
        guard matchesConsent(snapshot, auth: auth) else { return nil }
        fenceRevision += 1
        isSaving = true
        requestedEnabled = true
        saveError = ""
        return fenceRevision
    }

    mutating func finishConsent(result: OwnerSMSPatchResult, snapshot: OwnerSMSConsentSnapshot,
                                operationToken: Int, auth: CallAuthContext) {
        guard auth == boundAuth, auth == snapshot.auth, operationToken == fenceRevision,
              isSaving, ownerPhoneE164 == snapshot.ownerPhoneE164,
              senderNumber == snapshot.twilioNumber, optOutRevision == snapshot.expectedRevision else { return }
        // The dedicated parser has already required consent_required=false and no STOP.
        if case .success(let enabled, let optedOut, let revision, let version) = result,
           enabled, !optedOut, revision == snapshot.expectedRevision, version == 1 {
            consentRequired = false
            finishSave(result: result, operationToken: operationToken, auth: auth)
        } else {
            finishSave(result: .failure(String(localized: "SMS consent was not confirmed. Refresh settings and try again.")),
                       operationToken: operationToken, auth: auth)
            requestedEnabled = nil
        }
    }

    private static func isE164(_ value: String) -> Bool {
        value.first == "+" && (9...16).contains(value.count)
            && value.dropFirst().first != "0" && value.dropFirst().allSatisfy { $0.isASCII && $0.isNumber }
    }

    static let unavailableNotice = String(localized: "SMS settings are unavailable right now. Please try again.")

    mutating func recordLoadFailure(token: Int, auth: CallAuthContext) {
        guard auth.isValid, auth == boundAuth, token == fenceRevision, !isSaving else { return }
        loadError = Self.unavailableNotice
    }

    /// Reset state when CallAuthContext changes.
    mutating func reset(auth: CallAuthContext) {
        boundAuth = auth
        requestedEnabled = nil
        consentRequired = false
        consentCountry = ""
        ownerPhoneE164 = ""
        senderNumber = ""
        confirmedEnabled = true
        carrierOptedOut = false
        optOutRevision = 0
        settingsVersion = 1
        isLoaded = false
        isSaving = false
        saveError = ""
        loadError = ""
        isUnsupportedBackend = false
        fenceRevision += 1
    }
}


struct OwnerSMSConsentSnapshot: Equatable, Sendable, Identifiable {
    var id: Int { fenceRevision }
    let auth: CallAuthContext
    let ownerPhoneE164: String
    let twilioNumber: String
    let expectedRevision: Int
    let fenceRevision: Int
    static let disclosureVersion = "ca-owner-sms-v1"
    var requestBody: [String: Any] {
        ["disclosure_version": Self.disclosureVersion,
         "owner_phone_e164": ownerPhoneE164,
         "twilio_number": twilioNumber,
         "expected_revision": expectedRevision]
    }
    var disclosure: String {
        "I agree to receive SMS from Hey Kevin at \(ownerPhoneE164), sent from my Kevin number \(twilioNumber), for call summaries, voicemail and account notices. Message frequency varies. Message and data rates may apply. Reply STOP to stop or HELP for help."
    }
}
