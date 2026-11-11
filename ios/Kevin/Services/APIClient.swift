import Foundation
import CoreFoundation

enum AppleLookupResult: Equatable, Sendable {
    case found(contractorId: String, apiToken: String)
    case notFound
    case authFailure
    case failure(String)
}

enum AppleLookupResponseParser {
    static func parse(data: Data, response: HTTPURLResponse) -> AppleLookupResult {
        if response.statusCode == 401 { return .authFailure }
        if response.statusCode == 404 { return .notFound }
        let failure = AppleLookupResult.failure(String(localized: "Could not reconnect your account. Please try again."))
        guard response.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: data) else { return failure }
        // The deployed FastAPI route serializes its not-found tuple as a 200
        // array. Only this exact legacy shape means absence, never an outage.
        if let array = object as? [Any], array.count == 2,
           let error = array[0] as? [String: String], error == ["error": "Not found"],
           let code = array[1] as? NSNumber,
           CFGetTypeID(code) != CFBooleanGetTypeID(), code == NSNumber(value: 404) {
            return .notFound
        }
        guard let json = object as? [String: Any],
              json["error"] == nil,
              let id = json["contractor_id"] as? String,
              let token = json["api_token"] as? String,
              !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return failure }
        return .found(contractorId: id, apiToken: token)
    }
}

enum ContactSyncUploadResult: Equatable, Sendable {
    case synced(synced: Int, removed: Int)
    case unchanged
    case rateLimited
    case failed
}

enum ContactSyncResponseParser {
    static func parse(data: Data, response: URLResponse) -> ContactSyncUploadResult {
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              json["error"] == nil else { return .failed }
        switch json["status"] as? String {
        case "ok":
            return .synced(synced: json["synced"] as? Int ?? 0, removed: json["removed"] as? Int ?? 0)
        case "skipped": return .unchanged
        case "rate_limited": return .rateLimited
        default: return .failed
        }
    }
}

enum DeviceRegistrationResponseParser {
    static func succeeded(data: Data, response: URLResponse) -> Bool {
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              json["status"] as? String == "ok", json["error"] == nil else { return false }
        return true
    }
}

/// Errors specific to the unauthenticated bootstrap calls (lookup-by-apple-id,
/// create contractor). These run before we have a contractor API token and
/// authenticate solely with the Apple identity token, which expires after
/// roughly 10 minutes. The backend returns 401 when the token is missing,
/// expired, or invalid.
enum BootstrapAuthError: Error, LocalizedError {
    /// The backend rejected the Apple identity token (HTTP 401).
    case unauthenticated

    var errorDescription: String? {
        switch self {
        case .unauthenticated:
            return String(localized: "Sign in expired. Please tap Sign in with Apple again to continue.")
        }
    }
}

/// The local account stores and the process-local epoch shared by AppState and API authorization.
final class AccountPersistence {
    static let live = AccountPersistence(defaults: .standard, keychain: .shared, epoch: .shared)

    let defaults: UserDefaults
    let keychain: KeychainManager
    let epoch: CallSessionEpoch
    private var migratedLegacyKeys: Set<String> = []

    init(defaults: UserDefaults, keychain: KeychainManager, epoch: CallSessionEpoch) {
        self.defaults = defaults
        self.keychain = keychain
        self.epoch = epoch
    }

    func readMigratingLegacy(_ key: String) -> KeychainReadResult {
        epoch.synchronized {
            let preRead = keychain.read(key)
            let result = keychain.readMigratingLegacy(key, defaults: defaults)
            if preRead == .missing, case .value = result {
                migratedLegacyKeys.insert(key)
            }
            return result
        }
    }

    func didMigrateLegacyKey(_ key: String) -> Bool {
        epoch.synchronized {
            migratedLegacyKeys.contains(key)
        }
    }

    func clearMigratedLegacyKey(_ key: String) {
        epoch.synchronized {
            _ = migratedLegacyKeys.remove(key)
        }
    }

    /// Per-contractor API token stored securely in Keychain
    /// Migrates from UserDefaults on first access for existing users
    var contractorToken: String {
        get {
            readMigratingLegacy("contractorApiToken").stringValue ?? ""
        }
        set {
            let changed = epoch.synchronized { () -> Bool in
                let current = keychain.read("contractorApiToken")
                if newValue.isEmpty {
                    let hadLegacy = KeychainManager.removeNonemptyLegacyValue(forKey: "contractorApiToken", in: defaults)
                    _ = migratedLegacyKeys.remove("contractorApiToken")
                    if current == .missing {
                        return hadLegacy
                    }
                    // Unavailable storage (.error) is not verified absence; always attempt deletion.
                    _ = keychain.delete("contractorApiToken")
                    return true
                }
                if current == .value(newValue) {
                    _ = KeychainManager.removeNonemptyLegacyValue(forKey: "contractorApiToken", in: defaults)
                    _ = migratedLegacyKeys.remove("contractorApiToken")
                    return false
                }
                if keychain.save("contractorApiToken", value: newValue) {
                    _ = KeychainManager.removeNonemptyLegacyValue(forKey: "contractorApiToken", in: defaults)
                    _ = migratedLegacyKeys.remove("contractorApiToken")
                }
                return true
            }
            if changed {
                epoch.advance()
            }
        }
    }
}

final class APIClient: @unchecked Sendable, JobberAPIClientProtocol {
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

    private let persistence: AccountPersistence

    var contractorToken: String {
        get { persistence.contractorToken }
        set { persistence.contractorToken = newValue }
    }

    private let customSession: URLSession?

    init(session: URLSession? = nil, persistence: AccountPersistence = .live) {
        self.persistence = persistence
        self.customSession = session
    }

    private lazy var defaultSession: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 10
        config.timeoutIntervalForResource = 15
        return URLSession(configuration: config)
    }()

    private lazy var ownerSMSConsentSession: URLSession = {
        // Enrollment alone can take 15 seconds; consent transactions need headroom.
        // Keep the longer budget separate from short profile and call requests.
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 90
        return URLSession(configuration: config)
    }()

    private lazy var contactSyncSession: URLSession = {
        // One upload carries up to 5000 numbers and the server writes them in
        // batches; the default 15-second resource budget is too short for that.
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 120
        return URLSession(configuration: config)
    }()

    var session: URLSession {
        customSession ?? defaultSession
    }

    var effectiveOwnerSMSConsentSession: URLSession {
        customSession ?? ownerSMSConsentSession
    }

    func debugLog(_ message: String) {
        #if DEBUG
        print(message)
        #endif
    }

    /// Add auth header using contractor token from Keychain
    func authorize(_ request: inout URLRequest) {
        let token = contractorToken
        guard !token.isEmpty else { return }
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
    }

    /// Retry wrapper — only retries on 5xx server errors, not network failures.
    /// On 401, signals AppState to show re-auth (token expired/invalid).
    func retryRequest(_ request: URLRequest, maxRetries: Int = 1, signalReauth: Bool = true) async throws -> (Data, URLResponse) {
        let requestGeneration = CallSessionEpoch.shared.generation
        var lastError: Error?
        for attempt in 0...maxRetries {
            do {
                let (data, response) = try await session.data(for: request)
                if let http = response as? HTTPURLResponse {
                    if http.statusCode == 401 {
                        if signalReauth {
                            await MainActor.run {
                                if CallSessionEpoch.shared.generation == requestGeneration,
                                   request.value(forHTTPHeaderField: "Authorization") == "Bearer \(self.contractorToken)" {
                                    AppState.shared.needsReauth = true
                                }
                            }
                        }
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

    // MARK: - Account Country

    /// Writes the account country through the validated settings endpoint and
    /// returns the server-confirmed code, or nil on any failure — including a
    /// 200 whose body carries `error`, which is how a failed save is reported.
    func updateCountryCode(contractorId: String, countryCode: String) async -> String? {
        guard !contractorToken.isEmpty else { return nil }

        do {
            var components = URLComponents(string: "\(baseURL)/api/settings")!
            components.queryItems = [URLQueryItem(name: "contractor_id", value: contractorId)]
            var request = URLRequest(url: components.url!)
            request.httpMethod = "PUT"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 10
            request.httpBody = try JSONSerialization.data(withJSONObject: ["country_code": countryCode])
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            return SettingsCountryParser.parse(response: response, data: data)
        } catch {
            debugLog("Update country failed: \(error.localizedDescription)")
        }
        return nil
    }

    // MARK: - Forwarding Instructions

    /// Per-country carrier dial codes. Returns nil on any failure — and on the
    /// backend's pre-fix response shape — so callers fall back to the built-in
    /// codes the app has always dialed.
    func getForwardingInstructions(countryCode: String) async -> ForwardingInstructions? {
        guard !contractorToken.isEmpty else { return nil }

        do {
            var components = URLComponents(string: "\(baseURL)/api/forwarding-instructions")!
            components.queryItems = [URLQueryItem(name: "country_code", value: countryCode)]
            let url = components.url!
            var request = URLRequest(url: url)
            request.timeoutInterval = 5
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            return ForwardingInstructionsParser.parse(response: response, data: data, expectedCountry: countryCode)
        } catch {
            debugLog("Forwarding instructions fetch failed: \(error.localizedDescription)")
        }
        return nil
    }

    // MARK: - Device Registration

    @discardableResult
    func registerDevice(pushToken: String, voipToken: String = "", authContext: CallAuthContext? = nil) async -> Bool {
        guard !pushToken.isEmpty || !voipToken.isEmpty else { return false }

        let currentAuth = await MainActor.run { AppState.shared.currentAuthContext() }
        let auth = authContext ?? currentAuth
        guard auth == currentAuth else { return false }
        let contractorId = auth.contractorId
        let token = auth.bearerToken
        guard auth.isValid, !contractorId.isEmpty, !token.isEmpty else {
            debugLog("Register device skipped: contractor auth unavailable")
            return false
        }

        do {
            let url = URL(string: "\(baseURL)/api/register-device")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 10
            let languageIdentifier: String = {
                let current = Locale.current
                if let langCode = current.language.languageCode?.identifier {
                    if langCode == "pt" {
                        if let region = current.region?.identifier.uppercased(), region == "BR" {
                            return "pt-BR"
                        }
                    }
                    return langCode
                }
                return "en"
            }()
            var body: [String: Any] = [
                "push_token": pushToken,
                "platform": "ios",
                "timezone": TimeZone.current.identifier,
                "language": languageIdentifier,
                "urgent_handoff_v1": true,
                "contractor_id": contractorId,
            ]
            if !voipToken.isEmpty {
                body["voip_token"] = voipToken
            }
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

            let (data, response) = try await session.data(for: request)
            return DeviceRegistrationResponseParser.succeeded(data: data, response: response)
        } catch {
            debugLog("Device registration failed: \(error.localizedDescription)")
            return false
        }
    }

    // MARK: - Account Lookup

    /// Look up an existing contractor by Apple User ID.
    ///
    /// The backend strictly verifies the Apple identity token. Throws
    /// ``BootstrapAuthError/unauthenticated`` on HTTP 401 so the caller can
    /// re-prompt for a fresh Sign-in with Apple credential and retry. Returns
    /// an explicit ``AppleLookupResult`` distinguishing found, not found, and failure.
    func findContractorByAppleId(appleUserId: String, appleIdentityToken: String = "") async throws -> AppleLookupResult {
        let url = URL(string: "\(baseURL)/api/contractors/lookup-by-apple-id")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "apple_user_id": appleUserId,
            "apple_identity_token": appleIdentityToken,
        ])
        authorize(&request)

        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                return .failure("Invalid response")
            }
            if http.statusCode == 401 {
                throw BootstrapAuthError.unauthenticated
            }
            return AppleLookupResponseParser.parse(data: data, response: http)
        } catch let error as BootstrapAuthError {
            throw error
        } catch {
            debugLog("Lookup by Apple ID failed: \(error.localizedDescription)")
            return .failure(error.localizedDescription)
        }
    }

    // MARK: - Markets

    func getMarkets() async -> MarketsFetchResult {
        do {
            let url = URL(string: "\(baseURL)/api/markets")!
            var request = URLRequest(url: url)
            request.timeoutInterval = 10
            let (data, response) = try await session.data(for: request)
            return MarketsParser.parseFetchResult(data: data, response: response)
        } catch {
            debugLog("Markets fetch failed: \(error.localizedDescription)")
            return .failed
        }
    }

    // MARK: - Contractor Onboarding

    /// Create (or restore-by-phone) a contractor account during onboarding.
    ///
    /// The backend strictly verifies the Apple identity token. Throws
    /// ``BootstrapAuthError/unauthenticated`` on HTTP 401 so the caller can
    /// re-prompt for a fresh Sign-in with Apple credential and retry. Returns
    /// `nil` on any other non-2xx response or transport failure.
    func createContractor(
        ownerName: String,
        businessName: String,
        serviceType: String,
        mode: String = "business",
        ownerPhone: String = "",
        countryCode: String? = nil,
        appleUserId: String = "",
        appleIdentityToken: String = "",
        businessAddress: String = "",
        businessCity: String = "",
        declaredOnboardingIntent: String? = nil,
        services: [[String: Any]]? = nil,
        knowledge: String? = nil
    ) async throws -> [String: Any]? {
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
            if let country = countryCode, !country.isEmpty {
                body["country_code"] = country
            }
            if let intent = declaredOnboardingIntent, !intent.isEmpty {
                body["declared_onboarding_intent"] = intent
            }
            if !appleUserId.isEmpty {
                body["apple_user_id"] = appleUserId
            }
            if !appleIdentityToken.isEmpty {
                body["apple_identity_token"] = appleIdentityToken
            }
            let trimmedAddress = businessAddress.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmedAddress.isEmpty {
                body["business_address"] = trimmedAddress
            }
            let trimmedCity = businessCity.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmedCity.isEmpty {
                body["business_city"] = trimmedCity
            }
            if let services, !services.isEmpty {
                body["services"] = services
            }
            if let knowledge {
                let trimmedKnowledge = knowledge.trimmingCharacters(in: .whitespacesAndNewlines)
                if !trimmedKnowledge.isEmpty {
                    body["knowledge"] = trimmedKnowledge
                }
            }
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            authorize(&request)

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse {
                if http.statusCode == 401 {
                    throw BootstrapAuthError.unauthenticated
                }
                if http.statusCode == 200 {
                    return try JSONSerialization.jsonObject(with: data) as? [String: Any]
                }
                if let errorDetail = APIErrorParser.parse(data: data, response: http) {
                    return [
                        "status": "error",
                        "error": errorDetail.localizedDescription,
                        "code": errorDetail.code,
                        "country_code": errorDetail.countryCode ?? "",
                    ]
                }
            }
        } catch let error as BootstrapAuthError {
            throw error
        } catch {
            debugLog("Create contractor failed: \(error.localizedDescription)")
        }
        return nil
    }

    func provisionNumber(contractorId: String, bearerToken: String? = nil) async -> [String: Any]? {
        guard !contractorId.isEmpty else { return nil }
        let token = bearerToken ?? contractorToken
        guard !token.isEmpty else { return nil }
        do {
            let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
            let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)/provision-number")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.timeoutInterval = 30  // Number provisioning can take time
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse {
                if http.statusCode == 200 {
                    return try JSONSerialization.jsonObject(with: data) as? [String: Any]
                }
                if let errorDetail = APIErrorParser.parse(data: data, response: http) {
                    return [
                        "status": "error",
                        "error": errorDetail.localizedDescription,
                        "code": errorDetail.code,
                        "country_code": errorDetail.countryCode ?? "",
                    ]
                }
            }
        } catch {
            debugLog("Provision number failed: \(error.localizedDescription)")
        }
        return nil
    }

    // MARK: - Contractor Profile

    func getContractorProfile(contractorId: String) async -> [String: Any]? {
        await getContractorProfile(
            contractorId: contractorId,
            bearerToken: contractorToken
        )
    }

    /// Account-bound profile fetch for billing reconciliation. The caller
    /// snapshots the auth context once so an account switch during suspension
    /// cannot authenticate a request for one account with another account's
    /// token.
    func getContractorProfile(
        contractorId: String,
        bearerToken: String
    ) async -> [String: Any]? {
        guard !contractorId.isEmpty, !bearerToken.isEmpty else { return nil }
        do {
            let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
            let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)")!
            var request = URLRequest(url: url)
            request.timeoutInterval = 10
            request.setValue(
                "Bearer \(bearerToken)",
                forHTTPHeaderField: "Authorization"
            )

            let (data, response) = try await session.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                      ContractorProfilePayload(profile: json, expectedContractorId: contractorId) != nil else {
                    return nil
                }
                return json
            }
        } catch {
            debugLog("Get contractor failed: \(error.localizedDescription)")
        }
        return nil
    }

    func updateKnowledge(knowledge: String, auth: CallAuthContext) async throws {
        guard auth.isValid else { throw URLError(.userAuthenticationRequired) }
        let encodedId = auth.contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? auth.contractorId
        let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)/knowledge")!
        var request = URLRequest(url: url)
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(auth.bearerToken)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 10
        request.httpBody = try JSONSerialization.data(withJSONObject: ["knowledge": knowledge])

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              body["status"] as? String == "ok" else {
            throw URLError(.badServerResponse)
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

    // MARK: - Contractor PATCH

    func patchContractor(_ contractorId: String, body: [String: Any], bearerToken: String? = nil) async throws -> Bool {
        let encodedId = contractorId.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? contractorId
        let url = URL(string: "\(baseURL)/api/contractors/\(encodedId)")!
        var request = URLRequest(url: url)
        request.httpMethod = "PATCH"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let token = bearerToken ?? contractorToken
        guard !token.isEmpty else { throw URLError(.userAuthenticationRequired) }
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await retryRequest(request, maxRetries: 0, signalReauth: false)
        guard (response as? HTTPURLResponse)?.statusCode == 200,
              let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return body["status"] as? String == "ok"
    }

    /// Saves the business street address and city regulatory countries
    /// require before Twilio number provisioning can succeed
    /// (`RegulatoryAddress.countries`). Thin wrapper over `patchContractor` —
    /// no new endpoint.
    func updateBusinessAddress(contractorId: String, address: String, city: String) async -> Bool {
        let body: [String: Any] = [
            "business_address": address.trimmingCharacters(in: .whitespacesAndNewlines),
            "business_city": city.trimmingCharacters(in: .whitespacesAndNewlines),
        ]
        do {
            return try await patchContractor(contractorId, body: body)
        } catch {
            debugLog("Update business address failed: \(error.localizedDescription)")
            return false
        }
    }

    // MARK: - Contractor Mode

    /// Outcome of a mode change, distinguishing "not entitled" from "it broke".
    ///
    /// The server is the authority on entitlement (the local subscription tier is
    /// only a UI cache), so a 403 here is a real answer — the account needs a
    /// Business subscription — not a failure to retry.
    enum ModeUpdateResult {
        case success
        case entitlementRequired
        case failed
    }

    func updateContractorMode(contractorId: String, mode: String) async -> ModeUpdateResult {
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
            switch (response as? HTTPURLResponse)?.statusCode {
            case 200: return .success
            case 403: return .entitlementRequired
            default: return .failed
            }
        } catch {
            debugLog("Update mode failed: \(error.localizedDescription)")
            return .failed
        }
    }

    // MARK: - Bulk Contact Sync

    func bulkSyncContacts(
        contractorId: String,
        contacts: [(name: String, phone: String)],
        contactsHash: String,
        bearerToken: String? = nil
    ) async -> ContactSyncUploadResult {
        if let token = bearerToken {
            guard !token.isEmpty else { return .failed }
        }
        do {
            let url = URL(string: "\(baseURL)/api/contacts/bulk-sync")!
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")

            let contactDicts = contacts.map { ["name": $0.name, "phone": $0.phone] }
            let body: [String: Any] = [
                "contacts": contactDicts,
                "contractor_id": contractorId,
                "contacts_hash": contactsHash,
            ]
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            if let token = bearerToken {
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            } else {
                authorize(&request)
            }

            let (data, response) = try await (customSession ?? contactSyncSession).data(for: request)
            return ContactSyncResponseParser.parse(data: data, response: response)
        } catch {
            debugLog("Bulk sync failed: \(error.localizedDescription)")
            return .failed
        }
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
