import XCTest
import Combine
@testable import Kevin

// MARK: - Mock URL Protocol for APIClient Tests

private final class JobberManagementURLProtocol: URLProtocol, @unchecked Sendable {
    static var requestHandler: ((URLRequest) throws -> (HTTPURLResponse, Data))?
    static var recordedRequests: [URLRequest] = []
    static let lock = NSLock()

    static func reset() {
        lock.lock()
        defer { lock.unlock() }
        requestHandler = nil
        recordedRequests = []
    }

    static func record(_ request: URLRequest) {
        lock.lock()
        defer { lock.unlock() }
        recordedRequests.append(request)
    }

    static func getRecordedRequests() -> [URLRequest] {
        lock.lock()
        defer { lock.unlock() }
        return recordedRequests
    }

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.record(request)
        guard let handler = Self.requestHandler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }

        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

// MARK: - Mock Jobber API Client for Model Tests

private final class MockJobberAPIClient: JobberAPIClientProtocol, @unchecked Sendable {
    var statusResult: Result<JobberStatusPayload, Error> = .success(
        JobberStatusPayload(connected: false, leadCaptureEnabled: false)
    )
    var connectResult: Result<URL, Error> = .success(
        URL(string: "https://api.getjobber.com/api/oauth/authorize?client_id=test")!
    )
    var disconnectResult: Result<JobberDisconnectPayload, Error> = .success(
        JobberDisconnectPayload(
            status: "disconnected",
            contractorId: "c-123",
            provider: "jobber",
            credentialDeletionStatus: "executed",
            revocationStatus: .providerConfirmed
        )
    )

    var statusCallCount: Int = 0
    var connectCallCount: Int = 0
    var disconnectCallCount: Int = 0

    var onStatusCall: (() async -> Void)?
    var onConnectCall: (() async -> Void)?
    var onDisconnectCall: (() async -> Void)?

    func getJobberStatus(auth: CallAuthContext) async throws -> JobberStatusPayload {
        statusCallCount += 1
        if let onStatusCall = onStatusCall {
            await onStatusCall()
        }
        return try statusResult.get()
    }

    func getJobberConnectURL(auth: CallAuthContext) async throws -> URL {
        connectCallCount += 1
        if let onConnectCall = onConnectCall {
            await onConnectCall()
        }
        return try connectResult.get()
    }

    func disconnectJobber(auth: CallAuthContext) async throws -> JobberDisconnectPayload {
        disconnectCallCount += 1
        if let onDisconnectCall = onDisconnectCall {
            await onDisconnectCall()
        }
        return try disconnectResult.get()
    }
}

// MARK: - Test Continuation Boxes

private final class TestVoidContinuationBox: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Never>?
    private var isResumed = false

    func setContinuation(_ cont: CheckedContinuation<Void, Never>) {
        lock.lock()
        if isResumed {
            lock.unlock()
            cont.resume()
        } else {
            self.continuation = cont
            lock.unlock()
        }
    }

    func resume() {
        lock.lock()
        if isResumed {
            lock.unlock()
            return
        }
        isResumed = true
        let cont = self.continuation
        self.continuation = nil
        lock.unlock()
        cont?.resume()
    }
}

private final class TestContinuationBox<T: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<T, Never>?
    private var pendingValue: T?
    private var isResumed = false

    func setContinuation(_ cont: CheckedContinuation<T, Never>) {
        lock.lock()
        if isResumed, let val = self.pendingValue {
            lock.unlock()
            cont.resume(returning: val)
        } else {
            self.continuation = cont
            lock.unlock()
        }
    }

    func resume(returning value: T) {
        lock.lock()
        if isResumed {
            lock.unlock()
            return
        }
        isResumed = true
        self.pendingValue = value
        let cont = self.continuation
        self.continuation = nil
        lock.unlock()
        cont?.resume(returning: value)
    }
}

// MARK: - Tests

@MainActor
final class JobberManagementTests: XCTestCase {

    override func setUp() {
        super.setUp()
        JobberManagementURLProtocol.reset()
    }

    override func tearDown() {
        JobberManagementURLProtocol.reset()
        super.tearDown()
    }

    private func makeAuth(
        contractorId: String = "c-123",
        token: String = "token-abc",
        generation: Int = 1
    ) -> CallAuthContext {
        CallAuthContext(contractorId: contractorId, bearerToken: token, generation: generation)
    }

    private func makeHTTPResponse(statusCode: Int = 200, urlString: String = "https://api.example.com") -> HTTPURLResponse {
        HTTPURLResponse(
            url: URL(string: urlString)!,
            statusCode: statusCode,
            httpVersion: nil,
            headerFields: nil
        )!
    }

    private func makeTestAPIClient() -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [JobberManagementURLProtocol.self]
        let session = URLSession(configuration: config)
        return APIClient(session: session)
    }

    // MARK: - Status Parser Tests

    func testStatusParserValidConnectedWithLeadCapture() throws {
        let jsonStr = """
        {"connected": true, "lead_capture_enabled": true, "connected_at": 1700000000.0}
        """
        let data = jsonStr.data(using: .utf8)!
        let payload = try JobberStatusParser.parse(data: data, response: makeHTTPResponse(statusCode: 200))

        XCTAssertTrue(payload.connected)
        XCTAssertTrue(payload.leadCaptureEnabled)
        XCTAssertEqual(payload.connectedAt, 1700000000.0)
    }

    func testStatusParserValidConnectedWithoutLeadCapture() throws {
        let jsonStr = """
        {"connected": true, "lead_capture_enabled": false}
        """
        let data = jsonStr.data(using: .utf8)!
        let payload = try JobberStatusParser.parse(data: data, response: makeHTTPResponse(statusCode: 200))

        XCTAssertTrue(payload.connected)
        XCTAssertFalse(payload.leadCaptureEnabled)
        XCTAssertNil(payload.connectedAt)
    }

    func testStatusParserValidDisconnected() throws {
        let jsonStr = """
        {"connected": false, "lead_capture_enabled": false}
        """
        let data = jsonStr.data(using: .utf8)!
        let payload = try JobberStatusParser.parse(data: data, response: makeHTTPResponse(statusCode: 200))

        XCTAssertFalse(payload.connected)
        XCTAssertFalse(payload.leadCaptureEnabled)
    }

    func testStatusParserRejectsInconsistentCaptureState() {
        let jsonStr = """
        {"connected": false, "lead_capture_enabled": true}
        """
        let data = jsonStr.data(using: .utf8)!

        XCTAssertThrowsError(try JobberStatusParser.parse(data: data, response: makeHTTPResponse(statusCode: 200))) { error in
            XCTAssertEqual(error as? JobberManagementError, .inconsistentCaptureState)
        }
    }

    func testStatusParserRejectsNumericBooleans() {
        // Integer 1/0 instead of true/false
        let numConnected = """
        {"connected": 1, "lead_capture_enabled": false}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try JobberStatusParser.parse(data: numConnected, response: makeHTTPResponse(statusCode: 200))) { error in
            XCTAssertEqual(error as? JobberManagementError, .malformedResponse)
        }

        let numLeadCapture = """
        {"connected": true, "lead_capture_enabled": 0}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try JobberStatusParser.parse(data: numLeadCapture, response: makeHTTPResponse(statusCode: 200))) { error in
            XCTAssertEqual(error as? JobberManagementError, .malformedResponse)
        }
    }

    func testStatusParserRejectsStringBooleans() {
        let strConnected = """
        {"connected": "true", "lead_capture_enabled": false}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try JobberStatusParser.parse(data: strConnected, response: makeHTTPResponse(statusCode: 200))) { error in
            XCTAssertEqual(error as? JobberManagementError, .malformedResponse)
        }
    }

    func testStatusParserRejectsMissingFields() {
        let missingLeadCapture = """
        {"connected": true}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try JobberStatusParser.parse(data: missingLeadCapture, response: makeHTTPResponse(statusCode: 200))) { error in
            XCTAssertEqual(error as? JobberManagementError, .malformedResponse)
        }
    }

    func testStatusParserRejectsNon200StatusCode() {
        let jsonStr = """
        {"connected": true, "lead_capture_enabled": true}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try JobberStatusParser.parse(data: jsonStr, response: makeHTTPResponse(statusCode: 500))) { error in
            XCTAssertEqual(error as? JobberManagementError, .httpError(statusCode: 500))
        }
    }

    // MARK: - Disconnect Parser Tests

    func testDisconnectParserValidConfirmedRevocation() throws {
        let jsonStr = """
        {
            "status": "disconnected",
            "contractor_id": "c-123",
            "provider": "jobber",
            "credential_deletion": {"status": "executed"},
            "provider_revocation": {"status": "provider_confirmed"}
        }
        """
        let data = jsonStr.data(using: .utf8)!
        let payload = try JobberDisconnectParser.parse(
            data: data,
            response: makeHTTPResponse(statusCode: 200),
            expectedContractorId: "c-123"
        )

        XCTAssertEqual(payload.status, "disconnected")
        XCTAssertEqual(payload.contractorId, "c-123")
        XCTAssertEqual(payload.provider, "jobber")
        XCTAssertEqual(payload.credentialDeletionStatus, "executed")
        XCTAssertEqual(payload.revocationStatus, .providerConfirmed)
        XCTAssertTrue(payload.isProviderConfirmed)
    }

    func testDisconnectParserValidUnconfirmedRevocations() throws {
        let unconfirmedStatuses = [
            ("provider_rejected", JobberRevocationStatus.providerRejected),
            ("transport_error_unknown", JobberRevocationStatus.transportErrorUnknown),
            ("not_attempted_unavailable_token", JobberRevocationStatus.notAttemptedUnavailableToken),
            ("something_new", JobberRevocationStatus.unknown)
        ]

        for (statusStr, expectedEnum) in unconfirmedStatuses {
            let jsonStr = """
            {
                "status": "disconnected",
                "contractor_id": "c-123",
                "provider": "jobber",
                "credential_deletion": {"status": "partial_reconciled"},
                "provider_revocation": {"status": "\(statusStr)"}
            }
            """
            let data = jsonStr.data(using: .utf8)!
            let payload = try JobberDisconnectParser.parse(
                data: data,
                response: makeHTTPResponse(statusCode: 200),
                expectedContractorId: "c-123"
            )
            XCTAssertEqual(payload.revocationStatus, expectedEnum)
            XCTAssertFalse(payload.isProviderConfirmed)
        }
    }

    func testDisconnectParserMissingRevocationNeverBecomesConfirmed() throws {
        let jsonStr = """
        {
            "status": "disconnected",
            "contractor_id": "c-123",
            "provider": "jobber",
            "credential_deletion": {"status": "legacy_reconciled"}
        }
        """
        let data = jsonStr.data(using: .utf8)!
        let payload = try JobberDisconnectParser.parse(
            data: data,
            response: makeHTTPResponse(statusCode: 200),
            expectedContractorId: "c-123"
        )
        XCTAssertEqual(payload.revocationStatus, .unknown)
        XCTAssertFalse(payload.isProviderConfirmed)
    }

    func testDisconnectParserTopLevelOnlyConfirmedRemainsUnknown() throws {
        let jsonStr = """
        {
            "status": "disconnected",
            "contractor_id": "c-123",
            "provider": "jobber",
            "credential_deletion": {"status": "executed"},
            "revocation_status": "provider_confirmed"
        }
        """
        let data = jsonStr.data(using: .utf8)!
        let payload = try JobberDisconnectParser.parse(
            data: data,
            response: makeHTTPResponse(statusCode: 200),
            expectedContractorId: "c-123"
        )
        XCTAssertEqual(payload.revocationStatus, .unknown)
        XCTAssertFalse(payload.isProviderConfirmed)
    }

    func testDisconnectParserMalformedOrMissingNestedRevocationRemainsUnknown() throws {
        let malformedCases = [
            // Missing provider_revocation
            """
            {
                "status": "disconnected",
                "contractor_id": "c-123",
                "provider": "jobber",
                "credential_deletion": {"status": "executed"}
            }
            """,
            // provider_revocation is not a dictionary (e.g. string or array)
            """
            {
                "status": "disconnected",
                "contractor_id": "c-123",
                "provider": "jobber",
                "credential_deletion": {"status": "executed"},
                "provider_revocation": "provider_confirmed"
            }
            """,
            // provider_revocation status is not a string
            """
            {
                "status": "disconnected",
                "contractor_id": "c-123",
                "provider": "jobber",
                "credential_deletion": {"status": "executed"},
                "provider_revocation": {"status": 123}
            }
            """
        ]

        for jsonStr in malformedCases {
            let data = jsonStr.data(using: .utf8)!
            let payload = try JobberDisconnectParser.parse(
                data: data,
                response: makeHTTPResponse(statusCode: 200),
                expectedContractorId: "c-123"
            )
            XCTAssertEqual(payload.revocationStatus, .unknown)
            XCTAssertFalse(payload.isProviderConfirmed)
        }
    }

    func testDisconnectParserRejectsRevocationDisagreement() {
        let jsonStr = """
        {
            "status": "disconnected",
            "contractor_id": "c-123",
            "provider": "jobber",
            "credential_deletion": {"status": "executed"},
            "provider_revocation": {"status": "provider_confirmed"},
            "revocation_status": "provider_rejected"
        }
        """
        let data = jsonStr.data(using: .utf8)!
        XCTAssertThrowsError(try JobberDisconnectParser.parse(
            data: data,
            response: makeHTTPResponse(statusCode: 200),
            expectedContractorId: "c-123"
        )) { error in
            XCTAssertEqual(error as? JobberManagementError, .malformedResponse)
        }
    }

    func testDisconnectParserRejectsInvalidCredentialDeletionStatus() {
        let jsonStr = """
        {
            "status": "disconnected",
            "contractor_id": "c-123",
            "provider": "jobber",
            "credential_deletion": {"status": "failed"}
        }
        """
        let data = jsonStr.data(using: .utf8)!
        XCTAssertThrowsError(try JobberDisconnectParser.parse(
            data: data,
            response: makeHTTPResponse(statusCode: 200),
            expectedContractorId: "c-123"
        )) { error in
            XCTAssertEqual(error as? JobberManagementError, .credentialDeletionUnacknowledged)
        }
    }

    func testDisconnectParserRejectsMismatchedContractorId() {
        let jsonStr = """
        {
            "status": "disconnected",
            "contractor_id": "c-victim",
            "provider": "jobber",
            "credential_deletion": {"status": "executed"}
        }
        """
        let data = jsonStr.data(using: .utf8)!
        XCTAssertThrowsError(try JobberDisconnectParser.parse(
            data: data,
            response: makeHTTPResponse(statusCode: 200),
            expectedContractorId: "c-123"
        )) { error in
            XCTAssertEqual(error as? JobberManagementError, .identityMismatch)
        }
    }

    func testDisconnectParserRejectsMismatchedProvider() {
        let jsonStr = """
        {
            "status": "disconnected",
            "contractor_id": "c-123",
            "provider": "google_calendar",
            "credential_deletion": {"status": "executed"}
        }
        """
        let data = jsonStr.data(using: .utf8)!
        XCTAssertThrowsError(try JobberDisconnectParser.parse(
            data: data,
            response: makeHTTPResponse(statusCode: 200),
            expectedContractorId: "c-123"
        )) { error in
            XCTAssertEqual(error as? JobberManagementError, .providerMismatch)
        }
    }

    // MARK: - Authorize URL Validator Tests

    func testAuthorizeURLValidatorValidCases() {
        let valid = "https://api.getjobber.com/api/oauth/authorize?client_id=cid&response_type=code&state=st&code_challenge=cc&code_challenge_method=S256"
        let parsed = JobberAuthorizeURLValidator.validate(valid)
        XCTAssertNotNil(parsed)
        XCTAssertEqual(parsed?.host, "api.getjobber.com")
        XCTAssertEqual(parsed?.path, "/api/oauth/authorize")
    }

    func testAuthorizeURLValidatorRejectsAdversarialCases() {
        // HTTP
        XCTAssertNil(JobberAuthorizeURLValidator.validate("http://api.getjobber.com/api/oauth/authorize?client_id=cid"))
        // Wrong host
        XCTAssertNil(JobberAuthorizeURLValidator.validate("https://evil.getjobber.com/api/oauth/authorize?client_id=cid"))
        XCTAssertNil(JobberAuthorizeURLValidator.validate("https://api.getjobber.com.attacker.com/api/oauth/authorize"))
        // Wrong path
        XCTAssertNil(JobberAuthorizeURLValidator.validate("https://api.getjobber.com/oauth/authorize?client_id=cid"))
        XCTAssertNil(JobberAuthorizeURLValidator.validate("https://api.getjobber.com/api/oauth/authorize/extra?client_id=cid"))
        // UserInfo
        XCTAssertNil(JobberAuthorizeURLValidator.validate("https://user:pass@api.getjobber.com/api/oauth/authorize?client_id=cid"))
        // Port
        XCTAssertNil(JobberAuthorizeURLValidator.validate("https://api.getjobber.com:8080/api/oauth/authorize?client_id=cid"))
        XCTAssertNil(JobberAuthorizeURLValidator.validate("https://api.getjobber.com:443/api/oauth/authorize?client_id=cid"))
        // Fragment
        XCTAssertNil(JobberAuthorizeURLValidator.validate("https://api.getjobber.com/api/oauth/authorize?client_id=cid#leak"))
    }

    // MARK: - Deep Link Validator Tests

    func testDeepLinkValidatorValidCanonicalURL() {
        let validURL = URL(string: "heykevin://integrations/jobber")!
        XCTAssertTrue(JobberDeepLinkValidator.isValidJobberDeepLink(validURL))
    }

    func testDeepLinkValidatorRejectsVariationsAndAttacks() {
        let invalidURLs = [
            "heykevin://integrations/jobber?code=123",
            "heykevin://integrations/jobber?",
            "heykevin://integrations/jobber/",
            "heykevin://integrations/jobber#fragment",
            "heykevin://integrations:8080/jobber",
            "heykevin://user:pass@integrations/jobber",
            "heykevin://integrations/jobber/extra",
            "heykevin://integrations/calendar",
            "https://integrations/jobber",
            "http://integrations/jobber",
            "heykevin://settings/jobber",
            "heykevin://integrations/%6aobber"
        ]

        for urlString in invalidURLs {
            if let url = URL(string: urlString) {
                XCTAssertFalse(
                    JobberDeepLinkValidator.isValidJobberDeepLink(url),
                    "Deep link validator must reject: \(urlString)"
                )
            }
        }
    }

    // MARK: - APIClient URLProtocol Tests

    func testAPIClientGetJobberConnectURLBundlesAuthAndParsesURL() async throws {
        let client = makeTestAPIClient()
        let auth = makeAuth(contractorId: "c-test-connect", token: "bearer-token-123")

        JobberManagementURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer bearer-token-123")
            XCTAssertTrue(request.url?.query?.contains("contractor_id=c-test-connect") ?? false)
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: nil,
                headerFields: nil
            )!
            let data = """
            {"authorize_url": "https://api.getjobber.com/api/oauth/authorize?client_id=123&state=abc"}
            """.data(using: .utf8)!
            return (response, data)
        }

        let url = try await client.getJobberConnectURL(auth: auth)
        XCTAssertEqual(url.host, "api.getjobber.com")
        XCTAssertEqual(url.path, "/api/oauth/authorize")

        let recorded = JobberManagementURLProtocol.getRecordedRequests()
        XCTAssertEqual(recorded.count, 1)
    }

    func testAPIClientGetJobberConnectURLHTTP500IssuesSingleRequest() async {
        let client = makeTestAPIClient()
        let auth = makeAuth(contractorId: "c-test-500", token: "tok-500")

        JobberManagementURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 500,
                httpVersion: nil,
                headerFields: nil
            )!
            return (response, Data())
        }

        do {
            _ = try await client.getJobberConnectURL(auth: auth)
            XCTFail("Expected HTTP error")
        } catch {
            XCTAssertEqual(error as? JobberManagementError, .httpError(statusCode: 500))
        }

        // maxRetries: 0 -> exactly 1 request issued
        let recorded = JobberManagementURLProtocol.getRecordedRequests()
        XCTAssertEqual(recorded.count, 1)
    }

    func testAPIClientGetJobberConnectURLRejectsInvalidURL() async {
        let client = makeTestAPIClient()
        let auth = makeAuth()

        JobberManagementURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            let data = """
            {"authorize_url": "https://evil.getjobber.com/api/oauth/authorize"}
            """.data(using: .utf8)!
            return (response, data)
        }

        do {
            _ = try await client.getJobberConnectURL(auth: auth)
            XCTFail("Expected invalid authorize URL error")
        } catch {
            XCTAssertEqual(error as? JobberManagementError, .invalidAuthorizeURL)
        }
    }

    func testAPIClientGetJobberStatusBundlesAuthAndParsesPayload() async throws {
        let client = makeTestAPIClient()
        let auth = makeAuth(contractorId: "c-status-1", token: "tok-status-1")

        JobberManagementURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer tok-status-1")
            XCTAssertTrue(request.url?.query?.contains("contractor_id=c-status-1") ?? false)
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            let data = """
            {"connected": true, "lead_capture_enabled": true, "connected_at": 1700000000.0}
            """.data(using: .utf8)!
            return (response, data)
        }

        let payload = try await client.getJobberStatus(auth: auth)
        XCTAssertTrue(payload.connected)
        XCTAssertTrue(payload.leadCaptureEnabled)
        XCTAssertEqual(payload.connectedAt, 1700000000.0)
    }

    func testAPIClientGetJobberStatusRejectsMalformedResponse() async {
        let client = makeTestAPIClient()
        let auth = makeAuth()

        JobberManagementURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            let data = """
            {"connected": "not-a-bool"}
            """.data(using: .utf8)!
            return (response, data)
        }

        do {
            _ = try await client.getJobberStatus(auth: auth)
            XCTFail("Expected malformedResponse")
        } catch {
            XCTAssertEqual(error as? JobberManagementError, .malformedResponse)
        }
    }

    func testAPIClientDisconnectJobberBundlesAuthAndParsesPayload() async throws {
        let client = makeTestAPIClient()
        let auth = makeAuth(contractorId: "c-disconnect-1", token: "tok-disconn-1")

        JobberManagementURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer tok-disconn-1")
            XCTAssertTrue(request.url?.query?.contains("contractor_id=c-disconnect-1") ?? false)
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            let data = """
            {
                "status": "disconnected",
                "contractor_id": "c-disconnect-1",
                "provider": "jobber",
                "credential_deletion": {"status": "executed"},
                "provider_revocation": {"status": "provider_confirmed"}
            }
            """.data(using: .utf8)!
            return (response, data)
        }

        let payload = try await client.disconnectJobber(auth: auth)
        XCTAssertEqual(payload.status, "disconnected")
        XCTAssertEqual(payload.contractorId, "c-disconnect-1")
        XCTAssertEqual(payload.revocationStatus, .providerConfirmed)
        XCTAssertTrue(payload.isProviderConfirmed)

        let recorded = JobberManagementURLProtocol.getRecordedRequests()
        XCTAssertEqual(recorded.count, 1)
    }

    func testAPIClientDisconnectJobberHTTP500IssuesSingleRequest() async {
        let client = makeTestAPIClient()
        let auth = makeAuth(contractorId: "c-test-500", token: "tok-500")

        JobberManagementURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 500, httpVersion: nil, headerFields: nil)!
            return (response, Data())
        }

        do {
            _ = try await client.disconnectJobber(auth: auth)
            XCTFail("Expected HTTP 500 error")
        } catch {
            XCTAssertEqual(error as? JobberManagementError, .httpError(statusCode: 500))
        }

        // maxRetries: 0 -> exactly 1 request issued
        let recorded = JobberManagementURLProtocol.getRecordedRequests()
        XCTAssertEqual(recorded.count, 1)
    }

    func testAPIClientDisconnectJobberRejectsIdentityMismatch() async {
        let client = makeTestAPIClient()
        let auth = makeAuth(contractorId: "c-my-account", token: "tok-my-account")

        JobberManagementURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            let data = """
            {
                "status": "disconnected",
                "contractor_id": "c-other-account",
                "provider": "jobber",
                "credential_deletion": {"status": "executed"}
            }
            """.data(using: .utf8)!
            return (response, data)
        }

        do {
            _ = try await client.disconnectJobber(auth: auth)
            XCTFail("Expected identityMismatch error")
        } catch {
            XCTAssertEqual(error as? JobberManagementError, .identityMismatch)
        }
    }

    // MARK: - JobberManagementModel Lifecycle & Single-Operation Fence

    func testModelInitialStateIsUnknown() {
        let model = JobberManagementModel(currentAuthProvider: { nil })
        XCTAssertEqual(model.connectionState, .unknown)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.activeOperation)
        XCTAssertNil(model.unconfirmedRevocationNotice)
        XCTAssertNil(model.statusErrorMessage)
        XCTAssertNil(model.browserPromptMessage)
        XCTAssertNil(model.lastConnectedAt)
    }

    func testModelRefreshStatusSuccessUpdatesState() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()
        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: true, connectedAt: 12345.0)
        )

        await model.refreshStatus(auth: auth, client: mockClient)

        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: true))
        XCTAssertEqual(model.lastConnectedAt, 12345.0)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.statusErrorMessage)
        XCTAssertEqual(mockClient.statusCallCount, 1)
    }

    func testModelRefreshStatusFailurePreservesLastStateAndSetsError() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        // 1. Initial success
        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: false, connectedAt: 100.0)
        )
        await model.refreshStatus(auth: auth, client: mockClient)
        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: false))

        // 2. Subsequent failure
        mockClient.statusResult = .failure(JobberManagementError.httpError(statusCode: 500))
        await model.refreshStatus(auth: auth, client: mockClient)

        // Must preserve connected state and set fixed error message
        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: false))
        XCTAssertEqual(model.statusErrorMessage, JobberManagementModel.fixedStatusErrorMessage)
        XCTAssertFalse(model.isBusy)
    }

    func testModelRefreshStatusPreservesUnconfirmedRevocationNotice() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        // Disconnect with unconfirmed notice
        mockClient.disconnectResult = .success(
            JobberDisconnectPayload(
                status: "disconnected",
                contractorId: "c-123",
                provider: "jobber",
                credentialDeletionStatus: "executed",
                revocationStatus: .transportErrorUnknown
            )
        )
        await model.disconnect(auth: auth, client: mockClient)

        XCTAssertEqual(model.connectionState, .notReady)
        XCTAssertEqual(model.unconfirmedRevocationNotice, JobberManagementModel.fixedUnconfirmedRevocationNotice)

        // Subsequent status refresh returns notReady
        mockClient.statusResult = .success(
            JobberStatusPayload(connected: false, leadCaptureEnabled: false)
        )
        await model.refreshStatus(auth: auth, client: mockClient)

        // Status refresh must NOT erase the unconfirmed revocation notice
        XCTAssertEqual(model.connectionState, .notReady)
        XCTAssertEqual(model.unconfirmedRevocationNotice, JobberManagementModel.fixedUnconfirmedRevocationNotice)
    }

    func testModelSingleOperationFenceRejectsConcurrentOperations() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        var didCallOpenURL = false

        // Script slow status call
        mockClient.onStatusCall = {
            // Attempt to trigger connect while status is still in flight
            await model.connect(auth: auth, client: mockClient) { _ in
                didCallOpenURL = true
                return true
            }
        }

        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: false)
        )

        await model.refreshStatus(auth: auth, client: mockClient)

        XCTAssertEqual(mockClient.statusCallCount, 1)
        XCTAssertEqual(mockClient.connectCallCount, 0, "Concurrent connect must be rejected by fence")
        XCTAssertFalse(didCallOpenURL)
        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: false))
    }

    func testModelStaleSuppliedAuthNotAdmitted() async {
        let liveAuth = makeAuth(contractorId: "c-live", token: "tok-live", generation: 1)
        let model = JobberManagementModel(currentAuthProvider: { liveAuth })
        let mockClient = MockJobberAPIClient()

        let staleAuth = makeAuth(contractorId: "c-stale", token: "tok-stale", generation: 1)

        await model.refreshStatus(auth: staleAuth, client: mockClient)

        XCTAssertEqual(mockClient.statusCallCount, 0, "Stale supplied auth must not be admitted")
        XCTAssertEqual(model.connectionState, .unknown)
        XCTAssertFalse(model.isBusy)
    }

    func testModelLiveAuthChangeWithoutOnChangeResetsBoundStateAndDropsInFlight() async {
        var currentLive: CallAuthContext? = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authA = currentLive!
        let model = JobberManagementModel(currentAuthProvider: { currentLive })
        let mockClient = MockJobberAPIClient()

        mockClient.onStatusCall = {
            // Live auth provider changes without handleAuthChange being explicitly called
            currentLive = self.makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        }

        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: true)
        )

        await model.refreshStatus(auth: authA, client: mockClient)

        // Completion for Auth A must be dropped and state reset
        XCTAssertEqual(model.connectionState, .unknown)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.activeOperation)
        XCTAssertNil(model.statusErrorMessage)
    }

    func testModelABARotationDropsStaleGenerationCompletion() async {
        var currentLive: CallAuthContext? = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authA1 = currentLive!
        let model = JobberManagementModel(currentAuthProvider: { currentLive })
        let mockClient = MockJobberAPIClient()

        mockClient.onStatusCall = {
            // Rotate A (gen 1) -> B (gen 2) -> A (gen 3)
            currentLive = self.makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
            model.handleAuthChange(newAuth: currentLive!)

            currentLive = self.makeAuth(contractorId: "c-A", token: "tok-A3", generation: 3)
            model.handleAuthChange(newAuth: currentLive!)
        }

        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: true)
        )

        await model.refreshStatus(auth: authA1, client: mockClient)

        // Stale lease with gen 1 must not mutate state of gen 3
        XCTAssertEqual(model.connectionState, .unknown)
        XCTAssertFalse(model.isBusy)
    }

    func testModelOverlappingDifferentAuthCallCannotInvalidateActiveLease() async {
        var currentLive: CallAuthContext? = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authA = currentLive!
        let model = JobberManagementModel(currentAuthProvider: { currentLive })
        let mockClient = MockJobberAPIClient()

        mockClient.onStatusCall = {
            // An overlapping call with different auth arrives while operation A is in-flight
            let otherAuth = self.makeAuth(contractorId: "c-OTHER", token: "tok-other", generation: 1)
            await model.refreshStatus(auth: otherAuth, client: mockClient)
        }

        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: true)
        )

        await model.refreshStatus(auth: authA, client: mockClient)

        // Operation A should complete cleanly
        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: true))
        XCTAssertFalse(model.isBusy)
        XCTAssertEqual(mockClient.statusCallCount, 1)
    }

    func testModelHandleAuthChangeDelayedNotificationDuringBusyOperationPreservesLease() async {
        let authA = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        var currentLive: CallAuthContext? = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        let authB = currentLive!

        let model = JobberManagementModel(currentAuthProvider: { currentLive })
        let mockClient = MockJobberAPIClient()

        mockClient.onStatusCall = {
            // Delayed notification for Auth A arrives while live Auth B operation is busy
            model.handleAuthChange(newAuth: authA)
            // Active lease for Auth B must still be intact
            XCTAssertTrue(model.isBusy)
            XCTAssertEqual(model.activeOperation, .refreshStatus)
        }

        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: true)
        )

        await model.refreshStatus(auth: authB, client: mockClient)

        // Live Auth B operation should complete successfully without being reset by delayed Auth A
        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: true))
        XCTAssertFalse(model.isBusy)
        XCTAssertEqual(mockClient.statusCallCount, 1)
    }

    func testModelHandleAuthChangeDuplicateNotificationPreservesActiveBusyLease() async {
        var currentLive: CallAuthContext? = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        let authB = currentLive!

        let model = JobberManagementModel(currentAuthProvider: { currentLive })
        let mockClient = MockJobberAPIClient()

        mockClient.onStatusCall = {
            // Duplicate notification for Auth B arrives while Auth B operation is busy
            model.handleAuthChange(newAuth: authB)
            XCTAssertTrue(model.isBusy)
            XCTAssertEqual(model.activeOperation, .refreshStatus)
        }

        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: true)
        )

        await model.refreshStatus(auth: authB, client: mockClient)

        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: true))
        XCTAssertFalse(model.isBusy)
        XCTAssertEqual(mockClient.statusCallCount, 1)
    }

    func testModelHandleAuthChangeNilLiveReadinessResetsBoundState() async {
        var currentLive: CallAuthContext? = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authA = currentLive!

        let model = JobberManagementModel(currentAuthProvider: { currentLive })
        let mockClient = MockJobberAPIClient()

        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: true)
        )
        await model.refreshStatus(auth: authA, client: mockClient)
        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: true))

        // Session unreadied / logged out -> currentAuthProvider returns nil
        currentLive = nil
        model.handleAuthChange(newAuth: authA) // Even if notification passes stale auth

        XCTAssertEqual(model.connectionState, .unknown)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.lastConnectedAt)
    }

    // MARK: - Browser Opener Tests

    func testModelConnectSuccessAsyncBrowserOpenerSetsPrompt() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        let expectedURL = URL(string: "https://api.getjobber.com/api/oauth/authorize?client_id=123")!
        mockClient.connectResult = .success(expectedURL)

        var openedURL: URL? = nil
        await model.connect(auth: auth, client: mockClient) { url in
            openedURL = url
            return true
        }

        XCTAssertEqual(openedURL, expectedURL)
        XCTAssertEqual(model.browserPromptMessage, JobberManagementModel.fixedBrowserPromptMessage)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.statusErrorMessage)
    }

    func testModelConnectFailureAsyncBrowserOpenerFalseSetsError() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        let expectedURL = URL(string: "https://api.getjobber.com/api/oauth/authorize?client_id=123")!
        mockClient.connectResult = .success(expectedURL)

        await model.connect(auth: auth, client: mockClient) { _ in
            return false // Browser failed to open
        }

        XCTAssertEqual(model.statusErrorMessage, JobberManagementModel.fixedConnectErrorMessage)
        XCTAssertNil(model.browserPromptMessage)
        XCTAssertFalse(model.isBusy)
    }

    func testModelConnectBrowserOpenerThrowSetsFixedErrorAndClearsLease() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        let expectedURL = URL(string: "https://api.getjobber.com/api/oauth/authorize?client_id=123")!
        mockClient.connectResult = .success(expectedURL)

        struct OpenerError: Error {}

        await model.connect(auth: auth, client: mockClient) { _ in
            throw OpenerError()
        }

        XCTAssertEqual(model.statusErrorMessage, JobberManagementModel.fixedConnectErrorMessage)
        XCTAssertNil(model.browserPromptMessage)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.activeLease)
    }

    func testModelConnectCancellationWithControlledContinuation() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        let expectedURL = URL(string: "https://api.getjobber.com/api/oauth/authorize?client_id=123")!
        mockClient.connectResult = .success(expectedURL)

        let browserEnteredBox = TestVoidContinuationBox()
        let openerResumeBox = TestContinuationBox<Bool>()
        let openerExitedBox = TestVoidContinuationBox()

        let connectTask = Task { @MainActor in
            await model.connect(auth: auth, client: mockClient) { _ in
                defer { openerExitedBox.resume() }
                return await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
                    openerResumeBox.setContinuation(continuation)
                    browserEnteredBox.resume()
                }
            }
        }

        // 1. Await browser entry
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            browserEnteredBox.setContinuation(continuation)
        }

        // 2. Cancel parent connect task and measure elapsed time to ensure prompt return (< 2.0s)
        let startTime = Date()
        connectTask.cancel()

        // 3. Verify bounded fixed-error completion
        await connectTask.value
        let elapsed = Date().timeIntervalSince(startTime)
        XCTAssertLessThan(elapsed, 2.0, "Cancellation must resolve promptly without waiting for the 10-second timeout")

        XCTAssertEqual(model.statusErrorMessage, JobberManagementModel.fixedConnectErrorMessage)
        XCTAssertNil(model.browserPromptMessage)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.activeLease)

        // 4. Resume suspended opener true, await its deterministic exit, and prove no success/state mutation
        openerResumeBox.resume(returning: true)
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            openerExitedBox.setContinuation(continuation)
        }

        XCTAssertEqual(model.statusErrorMessage, JobberManagementModel.fixedConnectErrorMessage)
        XCTAssertNil(model.browserPromptMessage)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.activeLease)
    }

    func testModelConnectCancelledBeforeBrowserOpenerStartSetsFixedErrorAndClearsLease() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        let expectedURL = URL(string: "https://api.getjobber.com/api/oauth/authorize?client_id=123")!
        mockClient.connectResult = .success(expectedURL)

        var openerWasCalled = false

        let connectTask = Task { @MainActor in
            await model.connect(auth: auth, client: mockClient) { _ in
                openerWasCalled = true
                return true
            }
        }
        connectTask.cancel()
        await connectTask.value

        XCTAssertEqual(model.statusErrorMessage, JobberManagementModel.fixedConnectErrorMessage)
        XCTAssertNil(model.browserPromptMessage)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.activeLease)
        XCTAssertFalse(openerWasCalled)
    }

    func testModelConnectBrowserOpenerTimeoutReturnsBoundedlyAndSetsFixedError() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        let expectedURL = URL(string: "https://api.getjobber.com/api/oauth/authorize?client_id=123")!
        mockClient.connectResult = .success(expectedURL)

        let openerEnteredBox = TestVoidContinuationBox()
        let openerResumeBox = TestContinuationBox<Bool>()
        let openerExitedBox = TestVoidContinuationBox()

        var timeoutCompletedFirst = false

        let connectTask = Task { @MainActor in
            await model.connect(auth: auth, client: mockClient, timeout: 0.05) { _ in
                // Truly non-cooperative opener: ignores cancellation by awaiting checked continuation
                defer { openerExitedBox.resume() }
                return await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
                    openerResumeBox.setContinuation(continuation)
                    openerEnteredBox.resume()
                }
            }
            timeoutCompletedFirst = true
        }

        // Wait until browser opener has entered
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            openerEnteredBox.setContinuation(continuation)
        }

        // Await timeout completion of connectTask
        await connectTask.value

        // Prove timeout completed first while opener is still suspended
        XCTAssertTrue(timeoutCompletedFirst)
        XCTAssertEqual(model.statusErrorMessage, JobberManagementModel.fixedConnectErrorMessage)
        XCTAssertNil(model.browserPromptMessage)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.activeLease)

        // Resume opener after timeout to clean up without leaving pending test tasks, awaiting its deterministic exit
        openerResumeBox.resume(returning: true)
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            openerExitedBox.setContinuation(continuation)
        }

        XCTAssertEqual(model.statusErrorMessage, JobberManagementModel.fixedConnectErrorMessage)
        XCTAssertNil(model.browserPromptMessage)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.activeLease)
    }

    func testModelConnectLateBrowserCompletionAfterNewerOperationStartsIsIgnored() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        let expectedURL = URL(string: "https://api.getjobber.com/api/oauth/authorize?client_id=123")!
        mockClient.connectResult = .success(expectedURL)

        let oldOpenerEnteredBox = TestVoidContinuationBox()
        let oldOpenerResumeBox = TestContinuationBox<Bool>()
        let oldOpenerExitedBox = TestVoidContinuationBox()

        // 1. First connect with short timeout and controlled suspended opener
        let connectTask = Task { @MainActor in
            await model.connect(auth: auth, client: mockClient, timeout: 0.05) { _ in
                defer { oldOpenerExitedBox.resume() }
                return await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
                    oldOpenerResumeBox.setContinuation(continuation)
                    oldOpenerEnteredBox.resume()
                }
            }
        }

        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            oldOpenerEnteredBox.setContinuation(continuation)
        }

        await connectTask.value

        XCTAssertEqual(model.statusErrorMessage, JobberManagementModel.fixedConnectErrorMessage)
        XCTAssertNil(model.browserPromptMessage)

        // 2. Start a newer operation (refreshStatus) held genuinely busy on a second controlled continuation
        let newerStatusEnteredBox = TestVoidContinuationBox()
        let newerStatusResumeBox = TestVoidContinuationBox()

        mockClient.onStatusCall = {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                newerStatusResumeBox.setContinuation(continuation)
                newerStatusEnteredBox.resume()
            }
        }

        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: false)
        )

        let refreshTask = Task { @MainActor in
            await model.refreshStatus(auth: auth, client: mockClient)
        }

        // Wait until newer operation has started and is suspended in onStatusCall
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            newerStatusEnteredBox.setContinuation(continuation)
        }

        // Assert newer operation is genuinely busy and capture its lease UUID
        XCTAssertTrue(model.isBusy)
        XCTAssertEqual(model.activeOperation, .refreshStatus)
        guard let newerLease = model.activeLease else {
            XCTFail("Newer operation must have an active lease")
            oldOpenerResumeBox.resume(returning: true)
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                oldOpenerExitedBox.setContinuation(continuation)
            }
            newerStatusResumeBox.resume()
            await refreshTask.value
            return
        }
        let newerLeaseId = newerLease.id
        XCTAssertEqual(newerLease.operation, .refreshStatus)

        // 3. Resume the OLD opener while newer operation is actively busy and await deterministic exit of old opener
        oldOpenerResumeBox.resume(returning: true)
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            oldOpenerExitedBox.setContinuation(continuation)
        }

        // Assert newer UUID and isBusy stay intact, and old completion did NOT set prompt
        XCTAssertTrue(model.isBusy, "Newer operation must remain busy after old opener resumes")
        XCTAssertEqual(model.activeOperation, .refreshStatus)
        XCTAssertEqual(model.activeLease?.id, newerLeaseId, "Newer lease UUID must remain intact")
        XCTAssertNil(model.browserPromptMessage, "Old opener completion must not set prompt")

        // 4. Finish the newer operation and await both to leave no pending test tasks
        newerStatusResumeBox.resume()
        await refreshTask.value

        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.activeLease)
        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: false))
        XCTAssertNil(model.browserPromptMessage)
    }

    func testModelConnectStaleBrowserCompletionDropped() async {
        var currentLive: CallAuthContext? = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authA = currentLive!
        let model = JobberManagementModel(currentAuthProvider: { currentLive })
        let mockClient = MockJobberAPIClient()

        let expectedURL = URL(string: "https://api.getjobber.com/api/oauth/authorize?client_id=123")!
        mockClient.connectResult = .success(expectedURL)

        await model.connect(auth: authA, client: mockClient) { _ in
            // Auth rotates while browser is opening
            currentLive = self.makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
            model.handleAuthChange(newAuth: currentLive!)
            return true
        }

        // Stale browser completion must not set prompt message or mutate Auth B state
        XCTAssertNil(model.browserPromptMessage)
        XCTAssertFalse(model.isBusy)
    }

    // MARK: - Deferred Status Read Tests

    func testModelDeferredStatusRefreshDrainedAfterOwningOperation() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        // 1. First status call succeeds
        mockClient.onStatusCall = {
            // Make one-shot so subsequent drained call doesn't recursively re-trigger
            mockClient.onStatusCall = nil
            // While first status is in-flight, a foreground status refresh arrives
            await model.refreshStatus(auth: auth, client: mockClient)
        }

        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: true, connectedAt: 500.0)
        )

        await model.refreshStatus(auth: auth, client: mockClient)

        // Allow async deferred drain task to execute
        try? await Task.sleep(nanoseconds: 50_000_000)

        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: true))
        XCTAssertEqual(mockClient.statusCallCount, 2, "Deferred status refresh must be drained once after operation")
    }

    func testModelDeferredStatusRefreshDroppedOnAuthChange() async {
        var currentLive: CallAuthContext? = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authA = currentLive!
        let model = JobberManagementModel(currentAuthProvider: { currentLive })
        let mockClient = MockJobberAPIClient()

        mockClient.onStatusCall = {
            // Foreground refresh arrives while in flight
            await model.refreshStatus(auth: authA, client: mockClient)
            // Then auth rotates before operation completes
            currentLive = self.makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
            model.handleAuthChange(newAuth: currentLive!)
        }

        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: true)
        )

        await model.refreshStatus(auth: authA, client: mockClient)
        try? await Task.sleep(nanoseconds: 50_000_000)

        // Deferred status for Auth A must be dropped
        XCTAssertEqual(mockClient.statusCallCount, 1)
        XCTAssertEqual(model.connectionState, .unknown)
    }

    // MARK: - Disconnect Tests

    func testModelDisconnectSuccessWithConfirmedRevocation() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        mockClient.disconnectResult = .success(
            JobberDisconnectPayload(
                status: "disconnected",
                contractorId: "c-123",
                provider: "jobber",
                credentialDeletionStatus: "executed",
                revocationStatus: .providerConfirmed
            )
        )

        await model.disconnect(auth: auth, client: mockClient)

        XCTAssertEqual(model.connectionState, .notReady)
        XCTAssertNil(model.unconfirmedRevocationNotice)
        XCTAssertFalse(model.isBusy)
        XCTAssertNil(model.statusErrorMessage)
    }

    func testModelDisconnectFailurePreservesStatusAndSetsError() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        // Initial connected state
        mockClient.statusResult = .success(
            JobberStatusPayload(connected: true, leadCaptureEnabled: true)
        )
        await model.refreshStatus(auth: auth, client: mockClient)
        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: true))

        // Disconnect fails
        mockClient.disconnectResult = .failure(JobberManagementError.httpError(statusCode: 500))
        await model.disconnect(auth: auth, client: mockClient)

        XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: true))
        XCTAssertEqual(model.statusErrorMessage, JobberManagementModel.fixedDisconnectErrorMessage)
        XCTAssertFalse(model.isBusy)
    }

    // MARK: - Connection Status & Disconnect State Tests

    func testStatusParserConnectionStatusVariants() throws {
        let response200 = HTTPURLResponse(
            url: URL(string: "https://api.example.com")!,
            statusCode: 200,
            httpVersion: nil,
            headerFields: nil
        )!

        // 1. Connected
        let connectedJson = """
        {"connected": true, "lead_capture_enabled": true, "connection_status": "connected", "connected_at": 1700000000.0}
        """.data(using: .utf8)!
        let connectedPayload = try JobberStatusParser.parse(data: connectedJson, response: response200)
        XCTAssertTrue(connectedPayload.connected)
        XCTAssertTrue(connectedPayload.leadCaptureEnabled)
        XCTAssertEqual(connectedPayload.connectionStatus, .connected)
        XCTAssertEqual(connectedPayload.connectedAt, 1700000000.0)

        // 2. Disconnected
        let disconnectedJson = """
        {"connected": false, "lead_capture_enabled": false, "connection_status": "disconnected"}
        """.data(using: .utf8)!
        let disconnectedPayload = try JobberStatusParser.parse(data: disconnectedJson, response: response200)
        XCTAssertFalse(disconnectedPayload.connected)
        XCTAssertFalse(disconnectedPayload.leadCaptureEnabled)
        XCTAssertEqual(disconnectedPayload.connectionStatus, .disconnected)
        XCTAssertNil(disconnectedPayload.connectedAt)

        // 3. Disconnect Pending
        let pendingJson = """
        {"connected": false, "lead_capture_enabled": false, "connection_status": "disconnect_pending"}
        """.data(using: .utf8)!
        let pendingPayload = try JobberStatusParser.parse(data: pendingJson, response: response200)
        XCTAssertFalse(pendingPayload.connected)
        XCTAssertFalse(pendingPayload.leadCaptureEnabled)
        XCTAssertEqual(pendingPayload.connectionStatus, .disconnectPending)
        XCTAssertNil(pendingPayload.connectedAt)

        // 4. Reauthorization Required
        let reauthJson = """
        {"connected": false, "lead_capture_enabled": false, "connection_status": "reauthorization_required"}
        """.data(using: .utf8)!
        let reauthPayload = try JobberStatusParser.parse(data: reauthJson, response: response200)
        XCTAssertFalse(reauthPayload.connected)
        XCTAssertFalse(reauthPayload.leadCaptureEnabled)
        XCTAssertEqual(reauthPayload.connectionStatus, .reauthorizationRequired)
        XCTAssertNil(reauthPayload.connectedAt)
    }

    func testStatusParserOldServerWithoutConnectionStatusFallback() throws {
        let response200 = HTTPURLResponse(
            url: URL(string: "https://api.example.com")!,
            statusCode: 200,
            httpVersion: nil,
            headerFields: nil
        )!

        let oldServerConnectedJson = """
        {"connected": true, "lead_capture_enabled": false}
        """.data(using: .utf8)!
        let payload1 = try JobberStatusParser.parse(data: oldServerConnectedJson, response: response200)
        XCTAssertTrue(payload1.connected)
        XCTAssertEqual(payload1.connectionStatus, .connected)

        let oldServerDisconnectedJson = """
        {"connected": false, "lead_capture_enabled": false}
        """.data(using: .utf8)!
        let payload2 = try JobberStatusParser.parse(data: oldServerDisconnectedJson, response: response200)
        XCTAssertFalse(payload2.connected)
        XCTAssertEqual(payload2.connectionStatus, .disconnected)
    }

    func testStatusParserInconsistentConnectionStatusRejection() {
        let response200 = HTTPURLResponse(
            url: URL(string: "https://api.example.com")!,
            statusCode: 200,
            httpVersion: nil,
            headerFields: nil
        )!

        // connected: false with connection_status: "connected"
        let badJson1 = """
        {"connected": false, "lead_capture_enabled": false, "connection_status": "connected"}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try JobberStatusParser.parse(data: badJson1, response: response200)) { error in
            XCTAssertEqual(error as? JobberManagementError, .malformedResponse)
        }

        // connected: true with connection_status: "reauthorization_required"
        let badJson2 = """
        {"connected": true, "lead_capture_enabled": false, "connection_status": "reauthorization_required"}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try JobberStatusParser.parse(data: badJson2, response: response200)) { error in
            XCTAssertEqual(error as? JobberManagementError, .malformedResponse)
        }

        // connected: false with connected_at present
        let badJson3 = """
        {"connected": false, "lead_capture_enabled": false, "connected_at": 1700000000.0}
        """.data(using: .utf8)!
        XCTAssertThrowsError(try JobberStatusParser.parse(data: badJson3, response: response200)) { error in
            XCTAssertEqual(error as? JobberManagementError, .malformedResponse)
        }
    }

    func testModelRefreshStatusDisconnectPendingAndReauth() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let mockClient = MockJobberAPIClient()

        mockClient.statusResult = .success(JobberStatusPayload(
            connected: true, leadCaptureEnabled: true, connectedAt: 12345.0))
        await model.refreshStatus(auth: auth, client: mockClient)
        XCTAssertEqual(model.lastConnectedAt, 12345.0)

        // 1. Pending status
        mockClient.statusResult = .success(
            JobberStatusPayload(
                connected: false,
                leadCaptureEnabled: false,
                connectionStatus: .disconnectPending
            )
        )
        await model.refreshStatus(auth: auth, client: mockClient)
        XCTAssertEqual(model.connectionState, .disconnectPending)
        XCTAssertNil(model.lastConnectedAt)

        // 2. Reauthorization required status
        mockClient.statusResult = .success(
            JobberStatusPayload(
                connected: false,
                leadCaptureEnabled: false,
                connectionStatus: .reauthorizationRequired
            )
        )
        await model.refreshStatus(auth: auth, client: mockClient)
        XCTAssertEqual(model.connectionState, .reauthorizationRequired)
        XCTAssertNil(model.lastConnectedAt)
    }
    func testPresentInvalidConnectionStatusNeverUsesOldServerFallback() throws {
        let response = HTTPURLResponse(url: URL(string: "https://example.test")!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        for value in [NSNull(), true, 0, 1.0, [:], [], "unknown", "future_value"] as [Any] {
            let json: [String: Any] = ["connected": false, "lead_capture_enabled": false, "connection_status": value]
            let body = try JSONSerialization.data(withJSONObject: json)
            XCTAssertThrowsError(try JobberStatusParser.parse(data: body, response: response))
        }
    }

    func testOmittedStatusInitializerDerivesExistingMockConnection() {
        XCTAssertEqual(JobberStatusPayload(connected: true, leadCaptureEnabled: false).connectionStatus, .connected)
        XCTAssertEqual(JobberStatusPayload(connected: false, leadCaptureEnabled: false).connectionStatus, .disconnected)
    }

    func testLocalOnlyRemovalParserRequiresNoProviderAttempt() throws {
        let response = HTTPURLResponse(url: URL(string: "https://example.test")!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        for attempted in [false, true, 0, NSNull()] as [Any] {
            let json: [String: Any] = ["status": "disconnected", "contractor_id": "c-123", "provider": "jobber",
                "credential_deletion": ["status": "executed"],
                "provider_revocation": ["status": "not_attempted_local_only", "attempted": attempted],
                "revocation_status": "not_attempted_local_only"]
            let body = try JSONSerialization.data(withJSONObject: json)
            if let number = attempted as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID(), !number.boolValue {
                let payload = try JobberDisconnectParser.parse(data: body, response: response, expectedContractorId: "c-123")
                XCTAssertEqual(payload.revocationStatus, .notAttemptedLocalOnly)
                XCTAssertFalse(payload.isProviderConfirmed)
            } else {
                XCTAssertThrowsError(try JobberDisconnectParser.parse(data: body, response: response, expectedContractorId: "c-123"))
            }
        }
    }

    func testLocalOnlyRemovalModelUsesTruthfulNotice() async {
        let auth = makeAuth()
        let model = JobberManagementModel(currentAuthProvider: { auth })
        let client = MockJobberAPIClient()
        client.disconnectResult = .success(JobberDisconnectPayload(status: "disconnected", contractorId: "c-123",
            provider: "jobber", credentialDeletionStatus: "executed", revocationStatus: .notAttemptedLocalOnly))
        await model.disconnect(auth: auth, client: client)
        XCTAssertEqual(model.connectionState, .notReady)
        XCTAssertEqual(model.unconfirmedRevocationNotice, JobberManagementModel.fixedLocalRemovalNotice)
        XCTAssertNil(model.lastConnectedAt)
    }

    func testLocalOnlyDeclarationCannotHideMissingOrContradictoryEvidence() throws {
        let response = HTTPURLResponse(url: URL(string: "https://example.test")!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        let base: [String: Any] = ["status": "disconnected", "contractor_id": "c-123", "provider": "jobber",
            "credential_deletion": ["status": "executed"], "revocation_status": "not_attempted_local_only"]
        var hostile: [[String: Any]] = [base]
        for nested in [NSNull(), 1, "provider_confirmed"] as [Any] {
            for attempted in [false, true] {
                var json = base
                json["provider_revocation"] = ["status": nested, "attempted": attempted]
                hostile.append(json)
            }
        }
        for attempted in [false, true] {
            var json = base
            json["provider_revocation"] = ["attempted": attempted]
            hostile.append(json)
        }
        for top in [NSNull(), 1, "provider_confirmed"] as [Any] {
            var json = base
            json["revocation_status"] = top
            json["provider_revocation"] = ["status": "not_attempted_local_only", "attempted": false]
            hostile.append(json)
        }
        var missingTop = base
        missingTop.removeValue(forKey: "revocation_status")
        missingTop["provider_revocation"] = ["status": "not_attempted_local_only", "attempted": false]
        hostile.append(missingTop)
        var unexecuted = base
        unexecuted["credential_deletion"] = ["status": "partial_reconciled"]
        unexecuted["provider_revocation"] = ["status": "not_attempted_local_only", "attempted": false]
        hostile.append(unexecuted)
        for json in hostile {
            let body = try JSONSerialization.data(withJSONObject: json)
            XCTAssertThrowsError(try JobberDisconnectParser.parse(data: body, response: response, expectedContractorId: "c-123"))
        }
    }

    func testDelayedLocalRemovalCannotOverwriteReplacementAuthOperation() async {
        for failOldRemoval in [false, true] {
            var liveAuth: CallAuthContext? = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
            let oldAuth = liveAuth!
            let model = JobberManagementModel(currentAuthProvider: { liveAuth })
            let oldClient = MockJobberAPIClient()
            oldClient.disconnectResult = failOldRemoval
                ? .failure(JobberManagementError.httpError(statusCode: 500))
                : .success(JobberDisconnectPayload(status: "disconnected", contractorId: "c-A", provider: "jobber",
                    credentialDeletionStatus: "executed", revocationStatus: .notAttemptedLocalOnly))
            let oldEntered = TestVoidContinuationBox()
            let oldResume = TestVoidContinuationBox()
            oldClient.onDisconnectCall = {
                await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
                    oldResume.setContinuation(cont)
                    oldEntered.resume()
                }
            }
            let oldTask = Task { @MainActor in await model.disconnect(auth: oldAuth, client: oldClient) }
            await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in oldEntered.setContinuation(cont) }
            liveAuth = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
            let newAuth = liveAuth!
            model.handleAuthChange(newAuth: newAuth)
            let newClient = MockJobberAPIClient()
            newClient.statusResult = .success(JobberStatusPayload(connected: true, leadCaptureEnabled: true, connectedAt: 54321.0))
            await model.refreshStatus(auth: newAuth, client: newClient)
            let newEntered = TestVoidContinuationBox()
            let newResume = TestVoidContinuationBox()
            newClient.onStatusCall = {
                await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
                    newResume.setContinuation(cont)
                    newEntered.resume()
                }
            }
            let newTask = Task { @MainActor in await model.refreshStatus(auth: newAuth, client: newClient) }
            await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in newEntered.setContinuation(cont) }
            let leaseID = model.activeLease?.id
            XCTAssertNotNil(leaseID)
            oldResume.resume()
            await oldTask.value
            XCTAssertTrue(model.isBusy)
            XCTAssertEqual(model.activeLease?.id, leaseID)
            XCTAssertEqual(model.activeOperation, .refreshStatus)
            XCTAssertEqual(model.connectionState, .connected(leadCaptureEnabled: true))
            XCTAssertEqual(model.lastConnectedAt, 54321.0)
            XCTAssertNil(model.statusErrorMessage)
            XCTAssertNil(model.unconfirmedRevocationNotice)
            newResume.resume()
            await newTask.value
            XCTAssertFalse(model.isBusy)
        }
    }

}
