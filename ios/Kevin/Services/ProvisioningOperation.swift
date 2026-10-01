import Foundation

@MainActor
enum ProvisioningOperation {
    struct Output: Equatable, Sendable {
        let number: String
        let country: String?
        let binding: ServiceBinding?
        let subscriptionUUID: String?
        let syncedContacts: Int?
    }

    enum Result: Equatable, Sendable {
        case completed
        case failed(message: String)
        case superseded
    }

    static func run(
        capturedAuth: CallAuthContext,
        isCurrent: () -> Bool,
        cachedNumber: String = "",
        cachedCountry: String? = nil,
        cachedBinding: ServiceBinding? = nil,
        patchProfile: (() async throws -> Bool)? = nil,
        patchFailureMessage: String = "",
        fetchProfile: () async -> [String: Any]?,
        provisionNumber: () async -> [String: Any]?,
        syncContacts: (() async -> Int?)? = nil,
        commit: (Output) -> Void
    ) async -> Result {
        guard capturedAuth.isValid, isCurrent() else {
            return .superseded
        }

        if let patchProfile {
            do {
                let updated = try await patchProfile()
                guard isCurrent() else { return .superseded }
                if !updated {
                    let fallback = String(localized: "Failed to update profile. Please try again.")
                    let message = patchFailureMessage.isEmpty ? fallback : patchFailureMessage
                    return .failed(message: message)
                }
            } catch {
                guard isCurrent() else { return .superseded }
                let fallback = String(localized: "Failed to update profile. Please try again.")
                let message = patchFailureMessage.isEmpty ? fallback : patchFailureMessage
                return .failed(message: message)
            }
        }

        let initialProfile = await fetchProfile()
        guard isCurrent() else { return .superseded }

        let resolvedNumber: String
        let resolvedCountry: String?
        let resolvedBinding: ServiceBinding?

        if let initialProfile,
           let existingNumber = initialProfile["twilio_number"] as? String,
           !existingNumber.isEmpty {
            resolvedNumber = existingNumber
            resolvedCountry = SettingsCountry.accountCountry(from: initialProfile)
            resolvedBinding = ServiceBindingParser.parse(from: initialProfile)
        } else if !cachedNumber.isEmpty {
            resolvedNumber = cachedNumber
            resolvedCountry = cachedCountry
            resolvedBinding = cachedBinding
        } else {
            guard initialProfile != nil else {
                return .failed(message: String(localized: "Could not reconnect your account. Please try again."))
            }

            let provResult = await provisionNumber()
            guard isCurrent() else { return .superseded }

            if provResult?["status"] as? String == "ok",
               let phoneNumber = provResult?["phone_number"] as? String,
               !phoneNumber.isEmpty {
                resolvedNumber = phoneNumber
                resolvedCountry = SettingsCountry.accountCountry(from: provResult ?? [:])
                resolvedBinding = ServiceBindingParser.parseBinding(provResult?["service_binding"])
            } else {
                let message = provResult?["error"] as? String ?? provResult?["message"] as? String
                let fallback = String(localized: "Failed to provision number. Please try again.")
                return .failed(message: message ?? fallback)
            }
        }

        let syncedContactsCount: Int?
        if let syncContacts {
            syncedContactsCount = await syncContacts()
            guard isCurrent() else { return .superseded }
        } else {
            syncedContactsCount = nil
        }

        var subscriptionUUID: String? = nil
        if let finalProfile = await fetchProfile() {
            guard isCurrent() else { return .superseded }
            if let uuid = finalProfile["subscription_uuid"] as? String, !uuid.isEmpty {
                subscriptionUUID = uuid
            }
        } else {
            guard isCurrent() else { return .superseded }
        }

        guard isCurrent() else { return .superseded }

        let output = Output(
            number: resolvedNumber,
            country: resolvedCountry,
            binding: resolvedBinding,
            subscriptionUUID: subscriptionUUID,
            syncedContacts: syncedContactsCount
        )
        commit(output)
        return .completed
    }
}
