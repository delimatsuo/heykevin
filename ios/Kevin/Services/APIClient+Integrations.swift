import Foundation

extension APIClient {
    // MARK: - Native Jobber Integration

    func getJobberConnectURL(auth: CallAuthContext) async throws -> URL {
        guard auth.isValid else {
            throw JobberManagementError.invalidAuthContext
        }
        var components = URLComponents(string: "\(baseURL)/api/integrations/jobber/connect")!
        components.queryItems = [URLQueryItem(name: "contractor_id", value: auth.contractorId)]
        guard let url = components.url else {
            throw JobberManagementError.invalidAuthorizeURL
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 10
        request.setValue("Bearer \(auth.bearerToken)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await retryRequest(request, maxRetries: 0, signalReauth: true)
        guard let http = response as? HTTPURLResponse else {
            throw JobberManagementError.malformedResponse
        }
        return try JobberConnectResponseParser.parse(data: data, response: http)
    }

    func getJobberStatus(auth: CallAuthContext) async throws -> JobberStatusPayload {
        guard auth.isValid else {
            throw JobberManagementError.invalidAuthContext
        }
        var components = URLComponents(string: "\(baseURL)/api/integrations/jobber/status")!
        components.queryItems = [URLQueryItem(name: "contractor_id", value: auth.contractorId)]
        guard let url = components.url else {
            throw JobberManagementError.malformedResponse
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 10
        request.setValue("Bearer \(auth.bearerToken)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await retryRequest(request, maxRetries: 1, signalReauth: true)
        guard let http = response as? HTTPURLResponse else {
            throw JobberManagementError.malformedResponse
        }
        return try JobberStatusParser.parse(data: data, response: http)
    }

    func disconnectJobber(auth: CallAuthContext) async throws -> JobberDisconnectPayload {
        guard auth.isValid else {
            throw JobberManagementError.invalidAuthContext
        }
        var components = URLComponents(string: "\(baseURL)/api/integrations/jobber/disconnect")!
        components.queryItems = [URLQueryItem(name: "contractor_id", value: auth.contractorId)]
        guard let url = components.url else {
            throw JobberManagementError.malformedResponse
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 15
        request.setValue("Bearer \(auth.bearerToken)", forHTTPHeaderField: "Authorization")

        let (data, response) = try await retryRequest(request, maxRetries: 0, signalReauth: true)
        guard let http = response as? HTTPURLResponse else {
            throw JobberManagementError.malformedResponse
        }
        return try JobberDisconnectParser.parse(
            data: data,
            response: http,
            expectedContractorId: auth.contractorId
        )
    }

    // MARK: - Integrations

    func getIntegrationConnectURL(_ service: String, contractorId: String) async throws -> String? {
        let encodedService = service.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? service
        var components = URLComponents(string: "\(baseURL)/api/integrations/\(encodedService)/connect")!
        components.queryItems = [URLQueryItem(name: "contractor_id", value: contractorId)]
        var request = URLRequest(url: components.url!)
        request.timeoutInterval = 10
        authorize(&request)

        let (data, response) = try await retryRequest(request)
        if let http = response as? HTTPURLResponse,
           JobberConnectResponseParser.isBusinessProRequired(data: data, statusCode: http.statusCode) {
            throw JobberManagementError.businessProRequired
        }
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

    func checkIntegrationStatus(
        _ service: String,
        contractorId: String,
        bearerToken: String? = nil
    ) async throws -> Bool {
        let encodedService = service.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? service
        var components = URLComponents(string: "\(baseURL)/api/integrations/\(encodedService)/status")!
        components.queryItems = [URLQueryItem(name: "contractor_id", value: contractorId)]
        var request = URLRequest(url: components.url!)
        request.timeoutInterval = 10
        if let token = bearerToken {
            guard !token.isEmpty else {
                throw URLError(.userAuthenticationRequired)
            }
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        } else {
            authorize(&request)
        }

        let (data, response) = try await retryRequest(request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
           let connected = json["connected"] as? Bool {
            return connected
        }
        throw URLError(.cannotParseResponse)
    }
}
