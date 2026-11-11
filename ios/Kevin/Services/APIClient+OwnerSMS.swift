import Foundation

extension APIClient {
    // MARK: - Owner SMS Preferences

    func patchOwnerSMSPreference(
        contractorId: String,
        enabled: Bool,
        bearerToken: String
    ) async -> OwnerSMSPatchResult {
        guard !contractorId.isEmpty else {
            return .failure(String(localized: "Missing contractor ID."))
        }
        let token = bearerToken
        guard !token.isEmpty else {
            return .failure(String(localized: "Authentication required."))
        }

        let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
        guard let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)") else {
            return .failure(String(localized: "Invalid URL."))
        }

        var request = URLRequest(url: url)
        request.httpMethod = "PATCH"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "owner_sms_enabled": enabled
            ])
            let (data, response) = try await retryRequest(request, maxRetries: 0, signalReauth: false)
            guard let http = response as? HTTPURLResponse else {
                return .failure(String(localized: "Invalid server response."))
            }
            return OwnerSMSResponseParser.parsePatchResponse(data: data, response: http)
        } catch {
            debugLog("Patch owner SMS preference failed: \(error.localizedDescription)")
            return .failure(String(localized: "Failed to save setting. Please check your connection and try again."))
        }
    }

    func recordOwnerSMSConsent(snapshot: OwnerSMSConsentSnapshot) async -> OwnerSMSPatchResult {
        guard snapshot.auth.isValid, snapshot.expectedRevision >= 0 else {
            return .failure(String(localized: "Refresh SMS settings and try again."))
        }
        let encodedId = snapshot.auth.contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? snapshot.auth.contractorId
        guard let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)/owner-sms-consent") else {
            return .failure(String(localized: "Invalid URL."))
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 60
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(snapshot.auth.bearerToken)", forHTTPHeaderField: "Authorization")
        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: snapshot.requestBody)
            // Consent is a single attempt: an ambiguous response must not replay it.
            let (data, response) = try await effectiveOwnerSMSConsentSession.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                return .failure(String(localized: "Invalid server response."))
            }
            return OwnerSMSResponseParser.parseConsentResponse(data: data, response: http)
        } catch {
            return .failure(String(localized: "SMS consent was not confirmed. Refresh settings and try again."))
        }
    }
}
