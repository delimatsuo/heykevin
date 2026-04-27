import Foundation

private func debugLog(_ message: String) {
    #if DEBUG
    print(message)
    #endif
}

struct ActiveCallInfo {
    let callSid: String
    let callerPhone: String
    let callerName: String
    let transcript: String
}

class APIClient {
    static let shared = APIClient()

    let baseURL: String = {
        guard let url = Bundle.main.infoDictionary?["BackendURL"] as? String,
              !url.isEmpty,
              !url.contains("$(") else {
            fatalError("BackendURL not set in Info.plist. Check the active build configuration.")
        }

        #if DEBUG || STAGING
        if url == "https://kevin-api-752910912062.us-central1.run.app" {
            fatalError("Non-production build is configured to use the production backend.")
        }
        #endif

        return url
    }()

    /// Per-contractor API token stored securely in Keychain
    /// Migrates from UserDefaults on first access for existing users
    var contractorToken: String {
        get {
            if let existing = KeychainManager.shared.retrieve("contractorApiToken"), !existing.isEmpty {
                return existing
            }
            // Migrate from UserDefaults if present
            if let legacy = UserDefaults.standard.string(forKey: "contractorApiToken"), !legacy.isEmpty {
                KeychainManager.shared.save("contractorApiToken", value: legacy)
                UserDefaults.standard.removeObject(forKey: "contractorApiToken")
                return legacy
            }
            return ""
        }
        set {
            if newValue.isEmpty {
                KeychainManager.shared.delete("contractorApiToken")
            } else {
                KeychainManager.shared.save("contractorApiToken", value: newValue)
            }
        }
    }

    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 10
        config.timeoutIntervalForResource = 15
        return URLSession(configuration: config)
    }()

    /// Add auth header using contractor token from Keychain
    func authorize(_ request: inout URLRequest) {
        let token = contractorToken
        guard !token.isEmpty else { return }
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
    }

    /// Retry wrapper — only retries on 5xx server errors, not network failures.
    /// On 401, signals AppState to show re-auth (token expired/invalid).
    private func retryRequest(_ request: URLRequest, maxRetries: Int = 1) async throws -> (Data, URLResponse) {
        var lastError: Error?
        for attempt in 0...maxRetries {
            do {
                let (data, response) = try await session.data(for: request)
                if let http = response as? HTTPURLResponse {
                    if http.statusCode == 401 {
                        await MainActor.run { AppState.shared.needsReauth = true }
                        return (data, response)
                    }
                    if http.statusCode >= 500 && attempt < maxRetries {
                        try await Task.sleep(nanoseconds: UInt64(pow(2.0, Double(attempt))) * 1_000_000_000)
                        continue
                    }
                }
                return (data, response)
            } catch let error as URLError where error.code == .timedOut || error.code == .notConnectedToInternet || error.code == .networkConnectionLost {
                throw error
            } catch {
                lastError = error
                if attempt < maxRetries {
                    try await Task.sleep(nanoseconds: UInt64(pow(2.0, Double(attempt))) * 1_000_000_000)
                }
            }
        }
        throw lastError!
    }

    // MARK: - Active Call Check

    func getActiveCall() async -> ActiveCallInfo? {
        do {
            var components = URLComponents(string: "\(baseURL)/api/active-call")!
            components.queryItems = [URLQueryItem(name: "contractor_id", value: AppState.shared.contractorId)]
            let url = components.url!
            var request = URLRequest(url: url)
            request.timeoutInterval = 5
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               json["active"] as? Bool == true {
                return ActiveCallInfo(
                    callSid: json["call_sid"] as? String ?? "",
                    callerPhone: json["caller_phone"] as? String ?? "",
                    callerName: json["caller_name"] as? String ?? "",
                    transcript: json["transcript"] as? String ?? ""
                )
            }
        } catch {
            debugLog("Active call check failed: \(error.localizedDescription)")
        }
        return nil
    }

    // MARK: - Device Registration

    func registerDevice(pushToken: String, voipToken: String = "") async {
        do {
            let url = URL(string: "\(baseURL)/api/register-device")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 10
            var body: [String: Any] = [
                "push_token": pushToken,
                "platform": "ios",
                "timezone": TimeZone.current.identifier,
                "language": Locale.current.language.languageCode?.identifier ?? "en",
            ]
            if !voipToken.isEmpty {
                body["voip_token"] = voipToken
            }
            let contractorId = AppState.shared.contractorId
            if !contractorId.isEmpty {
                body["contractor_id"] = contractorId
            }
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            authorize(&request)

            let (_, response) = try await retryRequest(request)
            if let http = response as? HTTPURLResponse {
                debugLog("Register device: HTTP \(http.statusCode)")
                if http.statusCode == 200 {
                    debugLog("Device registered successfully")
                    await MainActor.run {
                        AppState.shared.isRegistered = true
                        AppState.shared.pushToken = pushToken
                    }
                }
            }
        } catch {
            debugLog("Device registration failed: \(error.localizedDescription)")
        }
    }

    // MARK: - Call Actions

    func sendCallAction(callSid: String, action: String, message: String = "") async -> [String: Any]? {
        do {
            var components = URLComponents(string: "\(baseURL)/api/call-action")!
            components.queryItems = [URLQueryItem(name: "contractor_id", value: AppState.shared.contractorId)]
            let url = components.url!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 10
            var body: [String: String] = [
                "call_sid": callSid,
                "action": action,
            ]
            if !message.isEmpty {
                body["message"] = message
            }
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            authorize(&request)

            let (data, _) = try await retryRequest(request)
            return try JSONSerialization.jsonObject(with: data) as? [String: Any]
        } catch {
            debugLog("Call action failed: \(error.localizedDescription)")
        }
        return nil
    }

    // MARK: - Transcript

    func getTranscript(callSid: String) async -> String? {
        do {
            let encodedCallSid = callSid.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? callSid
            var components = URLComponents(string: "\(baseURL)/api/transcript/\(encodedCallSid)")!
            components.queryItems = [URLQueryItem(name: "contractor_id", value: AppState.shared.contractorId)]
            let url = components.url!
            var request = URLRequest(url: url)
            request.timeoutInterval = 5
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                return json["transcript"] as? String
            }
        } catch {
            // Silent fail — polling will retry
        }
        return nil
    }

    // MARK: - Contacts

    func getContacts() async -> [VIPContact] {
        do {
            let url = URL(string: "\(baseURL)/api/contacts")!
            var request = URLRequest(url: url)
            request.timeoutInterval = 10
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let contacts = json["contacts"] as? [[String: Any]] {
                return contacts.compactMap { dict in
                    VIPContact(
                        id: dict["phone"] as? String ?? UUID().uuidString,
                        name: dict["name"] as? String ?? "",
                        phone: dict["phone"] as? String ?? ""
                    )
                }
            }
        } catch {
            debugLog("Contacts fetch failed: \(error.localizedDescription)")
        }
        return []
    }

    func addContact(name: String, phone: String) async {
        do {
            let url = URL(string: "\(baseURL)/api/contacts")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 10
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "name": name,
                "phone": phone,
                "is_whitelisted": true,
            ])
            authorize(&request)

            let (_, _) = try await session.data(for: request)
        } catch {
            debugLog("Add contact failed: \(error.localizedDescription)")
        }
    }

    // MARK: - Account Lookup

    func findContractorByAppleId(appleUserId: String, appleIdentityToken: String = "") async -> [String: Any]? {
        do {
            var components = URLComponents(string: "\(baseURL)/api/contractors/lookup-by-apple-id")!
            components.queryItems = [
                URLQueryItem(name: "apple_user_id", value: appleUserId),
            ]
            if !appleIdentityToken.isEmpty {
                components.queryItems?.append(URLQueryItem(name: "apple_identity_token", value: appleIdentityToken))
            }
            let url = components.url!
            var request = URLRequest(url: url)
            request.httpMethod = "GET"
            request.timeoutInterval = 10
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                return try JSONSerialization.jsonObject(with: data) as? [String: Any]
            }
        } catch {
            debugLog("Lookup by Apple ID failed: \(error.localizedDescription)")
        }
        return nil
    }

    // MARK: - Contractor Onboarding

    func createContractor(ownerName: String, businessName: String, serviceType: String, mode: String = "business", ownerPhone: String = "", appleUserId: String = "", appleIdentityToken: String = "") async -> [String: Any]? {
        do {
            let url = URL(string: "\(baseURL)/api/contractors")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 15
            var body: [String: Any] = [
                "business_name": businessName,
                "owner_name": ownerName,
                "service_type": serviceType,
                "mode": mode,
                "owner_phone": ownerPhone,
            ]
            if !appleUserId.isEmpty {
                body["apple_user_id"] = appleUserId
            }
            if !appleIdentityToken.isEmpty {
                body["apple_identity_token"] = appleIdentityToken
            }
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                return try JSONSerialization.jsonObject(with: data) as? [String: Any]
            }
        } catch {
            debugLog("Create contractor failed: \(error.localizedDescription)")
        }
        return nil
    }

    func provisionNumber(contractorId: String) async -> [String: Any]? {
        do {
            let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
            let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)/provision-number")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.timeoutInterval = 30  // Number provisioning can take time
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                return try JSONSerialization.jsonObject(with: data) as? [String: Any]
            }
        } catch {
            debugLog("Provision number failed: \(error.localizedDescription)")
        }
        return nil
    }

    // MARK: - Contractor Profile

    func getContractorProfile(contractorId: String) async -> [String: Any]? {
        do {
            let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
            let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)")!
            var request = URLRequest(url: url)
            request.timeoutInterval = 10
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                return try JSONSerialization.jsonObject(with: data) as? [String: Any]
            }
        } catch {
            debugLog("Get contractor failed: \(error.localizedDescription)")
        }
        return nil
    }

    func updateKnowledge(contractorId: String, knowledge: String) async {
        do {
            let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
            let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)/knowledge")!
            var request = URLRequest(url: url)
            request.httpMethod = "PUT"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 10
            request.httpBody = try JSONSerialization.data(withJSONObject: ["knowledge": knowledge])
            authorize(&request)

            let (_, _) = try await session.data(for: request)
        } catch {
            debugLog("Update knowledge failed: \(error.localizedDescription)")
        }
    }

    func importWebsite(contractorId: String, url: String) async -> [String: Any]? {
        do {
            let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
            let apiUrl = URL(string: "\(baseURL)/api/contractors/\(encodedId)/import-website")!
            var request = URLRequest(url: apiUrl)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 30
            request.httpBody = try JSONSerialization.data(withJSONObject: ["url": url])
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                return try JSONSerialization.jsonObject(with: data) as? [String: Any]
            }
        } catch {
            debugLog("Import website failed: \(error.localizedDescription)")
        }
        return nil
    }

    func structureKnowledge(contractorId: String, rawText: String, existingKnowledge: String = "", mode: String = "add") async -> String? {
        do {
            let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
            let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)/structure-knowledge")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 30
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "raw_text": rawText,
                "existing_knowledge": existingKnowledge,
                "mode": mode,
            ])
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                return json["knowledge"] as? String
            }
        } catch {
            debugLog("Structure knowledge failed: \(error.localizedDescription)")
        }
        return nil
    }

    // MARK: - Call History

    func getCallHistory() async throws -> [CallRecord] {
        let contractorId = await MainActor.run { AppState.shared.contractorId }
        guard !contractorId.isEmpty else {
            debugLog("Call history: no contractor ID")
            return []
        }
        var components = URLComponents(string: "\(baseURL)/api/calls")!
        components.queryItems = [URLQueryItem(name: "contractor_id", value: contractorId)]
        let url = components.url!
        var request = URLRequest(url: url)
        request.timeoutInterval = 10
        authorize(&request)

        let (data, _) = try await retryRequest(request)
        if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
           let callsArray = json["calls"] as? [[String: Any]] {
            let records = callsArray.compactMap { dict -> CallRecord? in
                let callSid = dict["call_sid"] as? String ?? "\(dict["timestamp"] as? Double ?? 0)-\(dict["caller_phone"] as? String ?? "")"
                let serverRead = dict["read"] as? Bool ?? false
                return CallRecord(
                    id: callSid,
                    callerPhone: dict["caller_phone"] as? String ?? "",
                    callerName: dict["caller_name"] as? String ?? "",
                    timestamp: Date(timeIntervalSince1970: dict["timestamp"] as? Double ?? 0),
                    trustScore: dict["trust_score"] as? Int ?? 0,
                    outcome: dict["outcome"] as? String ?? "unknown",
                    transcript: dict["transcript"] as? String ?? "",
                    voicemailURL: dict["voicemail_url"] as? String,
                    callbackNumber: dict["callback_number"] as? String,
                    readOnServer: serverRead
                )
            }
            // Seed local read state from server so unread badges are correct after reinstall
            await MainActor.run {
                for r in records where r.readOnServer {
                    AppState.shared.readCallIds.insert(r.id)
                }
            }
            return records
        }
        return []
    }

    func markCallsRead(_ callSids: [String]) async {
        guard !callSids.isEmpty else { return }
        let url = URL(string: "\(baseURL)/api/calls/mark-read")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        authorize(&request)
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["call_sids": callSids])
        _ = try? await retryRequest(request)
    }

    // MARK: - Contractor PATCH

    func patchContractor(_ contractorId: String, body: [String: Any]) async throws -> Bool {
        let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
        let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)")!
        var request = URLRequest(url: url)
        request.httpMethod = "PATCH"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        authorize(&request)

        let (_, response) = try await retryRequest(request)
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    // MARK: - Contractor Mode

    func updateContractorMode(contractorId: String, mode: String) async -> Bool {
        do {
            let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
            let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)")!
            var request = URLRequest(url: url)
            request.httpMethod = "PATCH"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 10
            request.httpBody = try JSONSerialization.data(withJSONObject: ["mode": mode])
            authorize(&request)

            let (_, response) = try await session.data(for: request)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            debugLog("Update mode failed: \(error.localizedDescription)")
            return false
        }
    }

    // MARK: - Bulk Contact Sync

    func bulkSyncContacts(contractorId: String, contacts: [(name: String, phone: String)], contactsHash: String = "") async -> (synced: Int, removed: Int) {
        do {
            let url = URL(string: "\(baseURL)/api/contacts/bulk-sync")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 30

            let contactDicts = contacts.map { ["name": $0.name, "phone": $0.phone] }
            var body: [String: Any] = [
                "contacts": contactDicts,
                "contractor_id": contractorId,
            ]
            if !contactsHash.isEmpty {
                body["contacts_hash"] = contactsHash
            }
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                let synced = json["synced"] as? Int ?? 0
                let removed = json["removed"] as? Int ?? 0
                return (synced, removed)
            }
        } catch {
            debugLog("Bulk sync failed: \(error.localizedDescription)")
        }
        return (0, 0)
    }

    // MARK: - Services

    func getServices(contractorId: String) async -> [[String: Any]] {
        do {
            let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
            let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)/services")!
            var request = URLRequest(url: url)
            request.timeoutInterval = 10
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let services = json["services"] as? [[String: Any]] {
                return services
            }
        } catch {
            debugLog("Get services failed: \(error.localizedDescription)")
        }
        return []
    }

    // MARK: - Integrations

    func getIntegrationConnectURL(_ service: String, contractorId: String) async throws -> String? {
        let encodedService = service.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? service
        var components = URLComponents(string: "\(baseURL)/api/integrations/\(encodedService)/connect")!
        components.queryItems = [URLQueryItem(name: "contractor_id", value: contractorId)]
        var request = URLRequest(url: components.url!)
        request.timeoutInterval = 10
        authorize(&request)

        let (data, _) = try await retryRequest(request)
        if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
            return json["authorize_url"] as? String
        }
        return nil
    }

    func disconnectIntegration(_ service: String, contractorId: String) async throws -> Bool {
        let encodedService = service.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? service
        var components = URLComponents(string: "\(baseURL)/api/integrations/\(encodedService)/disconnect")!
        components.queryItems = [URLQueryItem(name: "contractor_id", value: contractorId)]
        var request = URLRequest(url: components.url!)
        request.httpMethod = "POST"
        request.timeoutInterval = 10
        authorize(&request)

        let (_, response) = try await retryRequest(request)
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    func checkIntegrationStatus(_ service: String, contractorId: String) async throws -> Bool {
        let encodedService = service.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? service
        var components = URLComponents(string: "\(baseURL)/api/integrations/\(encodedService)/status")!
        components.queryItems = [URLQueryItem(name: "contractor_id", value: contractorId)]
        var request = URLRequest(url: components.url!)
        request.timeoutInterval = 10
        authorize(&request)

        let (data, _) = try await retryRequest(request)
        if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
           let connected = json["connected"] as? Bool {
            return connected
        }
        return false
    }

    // MARK: - Subscription

    @discardableResult
    func verifySubscription(transactionId: String) async -> Bool {
        let contractorId = await MainActor.run { AppState.shared.contractorId }
        guard !contractorId.isEmpty else { return false }
        do {
            let url = URL(string: "\(baseURL)/api/subscription/verify")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 15
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "transaction_id": transactionId,
                "contractor_id": contractorId,
            ])
            authorize(&request)
            let (data, response) = try await retryRequest(request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                debugLog("Verify subscription returned non-200")
                return false
            }
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  json["status"] as? String == "ok" else {
                debugLog("Verify subscription returned error response")
                return false
            }
            return true
        } catch {
            debugLog("Verify subscription failed: \(error.localizedDescription)")
            return false
        }
    }

    func signSubscriptionOffer(productId: String, offerId: String, applicationUsername: String) async -> [String: Any]? {
        let contractorId = await MainActor.run { AppState.shared.contractorId }
        guard !contractorId.isEmpty else { return nil }
        do {
            let url = URL(string: "\(baseURL)/api/subscription/sign-offer")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 15
            request.httpBody = try JSONSerialization.data(withJSONObject: [
                "contractor_id": contractorId,
                "product_id": productId,
                "offer_id": offerId,
                "application_username": applicationUsername,
            ])
            authorize(&request)
            let (data, response) = try await retryRequest(request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
               let sig = json["signature"] as? [String: Any] {
                return sig
            }
        } catch {
            debugLog("Sign subscription offer failed: \(error.localizedDescription)")
        }
        return nil
    }

    func checkPromoEligibility(contractorId: String) async -> Bool {
        guard !contractorId.isEmpty else { return false }
        do {
            var components = URLComponents(string: "\(baseURL)/api/subscription/promo-eligible")!
            components.queryItems = [URLQueryItem(name: "contractor_id", value: contractorId)]
            var request = URLRequest(url: components.url!)
            request.timeoutInterval = 10
            authorize(&request)
            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200,
               let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                return json["eligible"] as? Bool ?? false
            }
        } catch {
            debugLog("Promo eligibility check failed: \(error.localizedDescription)")
        }
        return false
    }

    // MARK: - Services (existing)

    func updateServices(contractorId: String, services: [[String: Any]]) async -> Bool {
        do {
            let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
            let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)/services")!
            var request = URLRequest(url: url)
            request.httpMethod = "PUT"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 10
            request.httpBody = try JSONSerialization.data(withJSONObject: ["services": services])
            authorize(&request)

            let (_, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                return true
            }
        } catch {
            debugLog("Update services failed: \(error.localizedDescription)")
        }
        return false
    }
}
