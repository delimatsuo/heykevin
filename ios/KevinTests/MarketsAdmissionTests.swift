import Foundation
import XCTest
@testable import Kevin

// MARK: - Mock URLProtocol for APIClient Tests

final class MockURLProtocol: URLProtocol {
    static var requestHandler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        guard let handler = MockURLProtocol.requestHandler else {
            client?.urlProtocol(self, didFailWithError: NSError(domain: "MockURLProtocol", code: -1, userInfo: [NSLocalizedDescriptionKey: "No handler provided"]))
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

final class MarketsAdmissionTests: XCTestCase {
    private var customSession: URLSession!

    override func setUp() {
        super.setUp()
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        customSession = URLSession(configuration: config)
    }

    override func tearDown() {
        MockURLProtocol.requestHandler = nil
        customSession = nil
        super.tearDown()
    }

    private func makeClient() -> APIClient {
        APIClient(session: customSession)
    }

    private func httpResponse(status: Int, url: String = "https://example.com/api/markets") throws -> HTTPURLResponse {
        try XCTUnwrap(HTTPURLResponse(
            url: XCTUnwrap(URL(string: url)),
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        ))
    }

    private func json(_ object: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: object)
    }

    // MARK: - Markets Parser

    func testMarketsParserSuccess() throws {
        let payload: [String: Any] = [
            "markets": [
                ["country_code": "US", "status": "available"],
                ["country_code": "CA", "status": "available"],
                ["country_code": "BR", "status": "qualification_required"],
                ["country_code": "GB", "status": "qualification_required"],
                ["country_code": "DE", "status": "unsupported"]
            ],
            "qualification_order": ["BR", "CA", "GB"]
        ]

        let parsed = MarketsParser.parse(
            data: try json(payload),
            response: try httpResponse(status: 200)
        )

        XCTAssertNotNil(parsed)
        XCTAssertEqual(parsed?.markets.count, 5)
        XCTAssertEqual(parsed?.qualificationOrder, ["BR", "CA", "GB"])

        XCTAssertTrue(parsed?.isAvailable(countryCode: "US") ?? false)
        XCTAssertTrue(parsed?.isAvailable(countryCode: "CA") ?? false)
        XCTAssertFalse(parsed?.isAvailable(countryCode: "BR") ?? true)
        XCTAssertFalse(parsed?.isAvailable(countryCode: "GB") ?? true)
        XCTAssertFalse(parsed?.isAvailable(countryCode: "DE") ?? true)
        XCTAssertFalse(parsed?.isAvailable(countryCode: "FR") ?? true)

        XCTAssertEqual(parsed?.status(for: "BR"), .qualificationRequired)
        XCTAssertEqual(parsed?.status(for: "GB"), .qualificationRequired)
        XCTAssertEqual(parsed?.status(for: "DE"), .unsupported)
        XCTAssertNil(parsed?.status(for: "ZZ"))
    }

    func testMarketsParserRejectsDuplicateCountryEntries() throws {
        let payload: [String: Any] = [
            "markets": [
                ["country_code": "US", "status": "available"],
                ["country_code": "US", "status": "available"]
            ]
        ]

        let parsed = MarketsParser.parse(
            data: try json(payload),
            response: try httpResponse(status: 200)
        )
        XCTAssertNil(parsed, "Duplicate country codes must be rejected")
    }

    func testMarketsParserRejectsNonUppercaseCountryCodes() throws {
        let lowercasePayload: [String: Any] = [
            "markets": [
                ["country_code": "us", "status": "available"]
            ]
        ]
        XCTAssertNil(
            MarketsParser.parse(data: try json(lowercasePayload), response: try httpResponse(status: 200)),
            "Non-uppercase country codes must be rejected"
        )

        let mixedCasePayload: [String: Any] = [
            "markets": [
                ["country_code": "Us", "status": "available"]
            ]
        ]
        XCTAssertNil(
            MarketsParser.parse(data: try json(mixedCasePayload), response: try httpResponse(status: 200)),
            "Mixed-case country codes must be rejected"
        )

        let nonLetterPayload: [String: Any] = [
            "markets": [
                ["country_code": "1A", "status": "available"]
            ]
        ]
        XCTAssertNil(
            MarketsParser.parse(data: try json(nonLetterPayload), response: try httpResponse(status: 200)),
            "Non-letter country codes must be rejected"
        )
    }

    func testMarketsParserRejectsMalformedMarketEntries() throws {
        // Missing status
        let missingStatus: [String: Any] = [
            "markets": [
                ["country_code": "US"]
            ]
        ]
        XCTAssertNil(MarketsParser.parse(data: try json(missingStatus), response: try httpResponse(status: 200)))

        // Missing country_code
        let missingCountry: [String: Any] = [
            "markets": [
                ["status": "available"]
            ]
        ]
        XCTAssertNil(MarketsParser.parse(data: try json(missingCountry), response: try httpResponse(status: 200)))

        // Unknown / invalid status
        let invalidStatus: [String: Any] = [
            "markets": [
                ["country_code": "US", "status": "experimental"]
            ]
        ]
        XCTAssertNil(MarketsParser.parse(data: try json(invalidStatus), response: try httpResponse(status: 200)))
    }

    func testMarketsParserRejectsEmptyMarketsArray() throws {
        let payload: [String: Any] = [
            "markets": [] as [[String: Any]],
            "qualification_order": ["BR"]
        ]

        let parsed = MarketsParser.parse(
            data: try json(payload),
            response: try httpResponse(status: 200)
        )
        XCTAssertNil(parsed, "Empty markets array must be rejected as invalid data")
    }

    func testMarketsParserRejectsNon200() throws {
        let payload: [String: Any] = [
            "markets": [["country_code": "US", "status": "available"]]
        ]

        let parsed = MarketsParser.parse(
            data: try json(payload),
            response: try httpResponse(status: 500)
        )
        XCTAssertNil(parsed)
    }

    func testMarketsParserRejectsMalformedJSON() throws {
        let data = Data("<html>Error</html>".utf8)
        let parsed = MarketsParser.parse(
            data: data,
            response: try httpResponse(status: 200)
        )
        XCTAssertNil(parsed)
    }

    // MARK: - APIErrorParser & Localized Descriptions

    func testAPIErrorParserDirectAndNested() throws {
        // Direct detail dictionary
        let nestedDetail: [String: Any] = [
            "detail": [
                "code": "country_not_available",
                "country_code": "BR",
                "message": "Market not open"
            ]
        ]
        let parsedNested = APIErrorParser.parse(
            data: try json(nestedDetail),
            response: try httpResponse(status: 409)
        )
        XCTAssertEqual(parsedNested?.code, "country_not_available")
        XCTAssertEqual(parsedNested?.countryCode, "BR")
        XCTAssertEqual(
            parsedNested?.localizedDescription,
            "Kevin is not yet available in Brazil. We are preparing the service."
        )

        // Top level code
        let topLevel: [String: Any] = [
            "code": "country_not_available",
            "country_code": "GB"
        ]
        let parsedTop = APIErrorParser.parse(
            data: try json(topLevel),
            response: try httpResponse(status: 409)
        )
        XCTAssertEqual(parsedTop?.code, "country_not_available")
        XCTAssertEqual(parsedTop?.countryCode, "GB")
        XCTAssertEqual(
            parsedTop?.localizedDescription,
            "Kevin is not yet available in the United Kingdom. We are preparing the service."
        )

        // Country locked error
        let locked: [String: Any] = ["code": "country_locked_to_number"]
        let parsedLocked = APIErrorParser.parse(
            data: try json(locked),
            response: try httpResponse(status: 409)
        )
        XCTAssertEqual(
            parsedLocked?.localizedDescription,
            "Your account country is locked to your assigned Kevin number."
        )

        // Invalid owner phone error
        let invalidPhone: [String: Any] = ["code": "invalid_owner_phone"]
        let parsedPhone = APIErrorParser.parse(
            data: try json(invalidPhone),
            response: try httpResponse(status: 400)
        )
        XCTAssertEqual(
            parsedPhone?.localizedDescription,
            "Please enter a valid mobile phone number."
        )

        // Country phone mismatch error
        let mismatch: [String: Any] = ["code": "country_phone_mismatch"]
        let parsedMismatch = APIErrorParser.parse(
            data: try json(mismatch),
            response: try httpResponse(status: 400)
        )
        XCTAssertEqual(
            parsedMismatch?.localizedDescription,
            "The phone number does not match the selected country."
        )
    }

    // MARK: - SignupAdmission Evaluation Tests

    func testSignupAdmissionEvaluation() {
        let markets = MarketsResponse(
            markets: [
                MarketInfo(countryCode: "US", status: .available),
                MarketInfo(countryCode: "CA", status: .available),
                MarketInfo(countryCode: "BR", status: .qualificationRequired),
                MarketInfo(countryCode: "GB", status: .qualificationRequired),
                MarketInfo(countryCode: "DE", status: .unsupported)
            ],
            qualificationOrder: ["BR", "GB"]
        )

        // 1. Available country -> .allowed
        let resultUS = SignupAdmission.evaluate(countryCode: "US", markets: markets)
        XCTAssertEqual(resultUS, .allowed)
        XCTAssertTrue(resultUS.isAllowed)
        XCTAssertNil(resultUS.errorMessage)

        let resultCA = SignupAdmission.evaluate(countryCode: "CA", markets: markets)
        XCTAssertEqual(resultCA, .allowed)

        // 2. Closed / qualification required country -> .unavailable
        let resultBR = SignupAdmission.evaluate(countryCode: "BR", markets: markets)
        XCTAssertEqual(
            resultBR,
            .unavailable(message: "Kevin is not yet available in Brazil. We are preparing the service.")
        )
        XCTAssertFalse(resultBR.isAllowed)
        XCTAssertEqual(resultBR.errorMessage, "Kevin is not yet available in Brazil. We are preparing the service.")

        let resultGB = SignupAdmission.evaluate(countryCode: "GB", markets: markets)
        XCTAssertEqual(
            resultGB,
            .unavailable(message: "Kevin is not yet available in the United Kingdom. We are preparing the service.")
        )

        let resultDE = SignupAdmission.evaluate(countryCode: "DE", markets: markets)
        XCTAssertEqual(
            resultDE,
            .unavailable(message: "Kevin is not yet available in this country. We are preparing the service.")
        )

        // 3. Nil markets -> .checkFailed
        let resultNil = SignupAdmission.evaluate(countryCode: "US", markets: nil)
        XCTAssertEqual(resultNil, .checkFailed(message: "Could not check availability. Try again."))
        XCTAssertFalse(resultNil.isAllowed)
        XCTAssertEqual(resultNil.errorMessage, "Could not check availability. Try again.")

        // 4. Missing selected market from markets list -> .checkFailed (MUST be checkFailed, not unavailable)
        let resultMissing = SignupAdmission.evaluate(countryCode: "FR", markets: markets)
        XCTAssertEqual(
            resultMissing,
            .checkFailed(message: "Could not check availability. Try again."),
            "Missing market from catalog must evaluate to .checkFailed, not .unavailable"
        )
    }

    // MARK: - SignupAdmission Snapshot Guard Tests

    func testSignupAdmissionSnapshotGuard() {
        let auth = CallAuthContext(contractorId: "c_123", bearerToken: "tok_abc", generation: 1)
        let snapshot = SignupAdmissionSnapshot(
            countryCode: "US",
            phone: "+16505551234",
            authContext: auth,
            appleUserId: "apple_user_1"
        )

        // Matching state is current
        XCTAssertTrue(snapshot.isCurrent(
            countryCode: "US",
            phone: "+16505551234",
            authContext: auth,
            appleUserId: "apple_user_1"
        ))

        // Country changed
        XCTAssertFalse(snapshot.isCurrent(
            countryCode: "BR",
            phone: "+16505551234",
            authContext: auth,
            appleUserId: "apple_user_1"
        ))

        // Phone changed
        XCTAssertFalse(snapshot.isCurrent(
            countryCode: "US",
            phone: "+16505559999",
            authContext: auth,
            appleUserId: "apple_user_1"
        ))

        // Auth contractorId changed
        let authOther = CallAuthContext(contractorId: "c_other", bearerToken: "tok_abc", generation: 1)
        XCTAssertFalse(snapshot.isCurrent(
            countryCode: "US",
            phone: "+16505551234",
            authContext: authOther,
            appleUserId: "apple_user_1"
        ))

        // Auth token changed
        let authOtherToken = CallAuthContext(contractorId: "c_123", bearerToken: "tok_other", generation: 1)
        XCTAssertFalse(snapshot.isCurrent(
            countryCode: "US",
            phone: "+16505551234",
            authContext: authOtherToken,
            appleUserId: "apple_user_1"
        ))

        // Auth generation changed
        let authNewGen = CallAuthContext(contractorId: "c_123", bearerToken: "tok_abc", generation: 2)
        XCTAssertFalse(snapshot.isCurrent(
            countryCode: "US",
            phone: "+16505551234",
            authContext: authNewGen,
            appleUserId: "apple_user_1"
        ))

        // Apple user ID changed
        XCTAssertFalse(snapshot.isCurrent(
            countryCode: "US",
            phone: "+16505551234",
            authContext: auth,
            appleUserId: "apple_user_2"
        ))
    }

    // MARK: - Production Admission Effect Guard: runIfAllowed Tests

    @MainActor
    func testRunIfAllowedStatusNotAllowedRejectsWithoutCallingOperation() async {
        let auth = CallAuthContext(contractorId: "c_123", bearerToken: "tok_abc", generation: 1)
        let snapshot = SignupAdmissionSnapshot(
            countryCode: "BR",
            phone: "+5511987654321",
            authContext: auth,
            appleUserId: "apple_user_1"
        )

        var spyCallCount = 0
        let operation: () async throws -> [String: Any]? = {
            spyCallCount += 1
            return ["contractor_id": "c_created"]
        }

        // 1. Unavailable status
        let unavailableResult = await SignupAdmission.runIfAllowed(
            status: .unavailable(message: "Not available"),
            snapshot: snapshot,
            current: { snapshot },
            operation: operation
        )
        XCTAssertNil(unavailableResult)
        XCTAssertEqual(spyCallCount, 0, "Operation must not be called when admission status is unavailable")

        // 2. CheckFailed status
        let checkFailedResult = await SignupAdmission.runIfAllowed(
            status: .checkFailed(message: "Check failed"),
            snapshot: snapshot,
            current: { snapshot },
            operation: operation
        )
        XCTAssertNil(checkFailedResult)
        XCTAssertEqual(spyCallCount, 0, "Operation must not be called when admission status is checkFailed")
    }

    @MainActor
    func testRunIfAllowedStaleSnapshotBeforeOperationRejects() async {
        let auth = CallAuthContext(contractorId: "c_123", bearerToken: "tok_abc", generation: 1)
        let snapshot = SignupAdmissionSnapshot(
            countryCode: "US",
            phone: "+16505551234",
            authContext: auth,
            appleUserId: "apple_user_1"
        )

        var spyCallCount = 0
        let operation: () async throws -> [String: Any]? = {
            spyCallCount += 1
            return ["contractor_id": "c_created"]
        }

        // Stale country
        let staleCountry = SignupAdmissionSnapshot(countryCode: "BR", phone: "+16505551234", authContext: auth, appleUserId: "apple_user_1")
        let res1 = await SignupAdmission.runIfAllowed(status: .allowed, snapshot: snapshot, current: { staleCountry }, operation: operation)
        XCTAssertNil(res1)
        XCTAssertEqual(spyCallCount, 0)

        // Stale phone
        let stalePhone = SignupAdmissionSnapshot(countryCode: "US", phone: "+16505559999", authContext: auth, appleUserId: "apple_user_1")
        let res2 = await SignupAdmission.runIfAllowed(status: .allowed, snapshot: snapshot, current: { stalePhone }, operation: operation)
        XCTAssertNil(res2)
        XCTAssertEqual(spyCallCount, 0)

        // Stale contractor ID
        let staleContractor = SignupAdmissionSnapshot(countryCode: "US", phone: "+16505551234", authContext: CallAuthContext(contractorId: "c_diff", bearerToken: "tok_abc", generation: 1), appleUserId: "apple_user_1")
        let res3 = await SignupAdmission.runIfAllowed(status: .allowed, snapshot: snapshot, current: { staleContractor }, operation: operation)
        XCTAssertNil(res3)
        XCTAssertEqual(spyCallCount, 0)

        // Stale token
        let staleToken = SignupAdmissionSnapshot(countryCode: "US", phone: "+16505551234", authContext: CallAuthContext(contractorId: "c_123", bearerToken: "tok_diff", generation: 1), appleUserId: "apple_user_1")
        let res4 = await SignupAdmission.runIfAllowed(status: .allowed, snapshot: snapshot, current: { staleToken }, operation: operation)
        XCTAssertNil(res4)
        XCTAssertEqual(spyCallCount, 0)

        // Stale generation
        let staleGen = SignupAdmissionSnapshot(countryCode: "US", phone: "+16505551234", authContext: CallAuthContext(contractorId: "c_123", bearerToken: "tok_abc", generation: 2), appleUserId: "apple_user_1")
        let res5 = await SignupAdmission.runIfAllowed(status: .allowed, snapshot: snapshot, current: { staleGen }, operation: operation)
        XCTAssertNil(res5)
        XCTAssertEqual(spyCallCount, 0)

        // Stale Apple ID
        let staleApple = SignupAdmissionSnapshot(countryCode: "US", phone: "+16505551234", authContext: auth, appleUserId: "apple_user_2")
        let res6 = await SignupAdmission.runIfAllowed(status: .allowed, snapshot: snapshot, current: { staleApple }, operation: operation)
        XCTAssertNil(res6)
        XCTAssertEqual(spyCallCount, 0)
    }

    @MainActor
    func testRunIfAllowedFreshAvailableInvokesOperationOnce() async {
        let auth = CallAuthContext(contractorId: "c_123", bearerToken: "tok_abc", generation: 1)
        let snapshot = SignupAdmissionSnapshot(
            countryCode: "US",
            phone: "+16505551234",
            authContext: auth,
            appleUserId: "apple_user_1"
        )

        var spyCallCount = 0
        let result = await SignupAdmission.runIfAllowed(
            status: .allowed,
            snapshot: snapshot,
            current: { snapshot }
        ) {
            spyCallCount += 1
            return ["contractor_id": "c_new_123"]
        }

        XCTAssertEqual(spyCallCount, 1, "Operation must be called exactly once for fresh allowed admission")
        XCTAssertEqual(result?["contractor_id"] as? String, "c_new_123")
    }

    @MainActor
    func testRunIfAllowedChangedStateDuringSuspendedOperationReturnsNil() async {
        let auth = CallAuthContext(contractorId: "c_123", bearerToken: "tok_abc", generation: 1)
        let initialSnapshot = SignupAdmissionSnapshot(
            countryCode: "US",
            phone: "+16505551234",
            authContext: auth,
            appleUserId: "apple_user_1"
        )

        var liveSnapshot = initialSnapshot
        var spyCallCount = 0

        let result = await SignupAdmission.runIfAllowed(
            status: .allowed,
            snapshot: initialSnapshot,
            current: { liveSnapshot }
        ) {
            spyCallCount += 1
            // State mutation while operation is suspended
            liveSnapshot = SignupAdmissionSnapshot(
                countryCode: "BR",
                phone: "+5511987654321",
                authContext: auth,
                appleUserId: "apple_user_1"
            )
            return ["contractor_id": "c_mutated"]
        }

        XCTAssertEqual(spyCallCount, 1, "Operation was started when initial snapshot matched")
        XCTAssertNil(result, "Result must be nil because state changed during suspended operation")
    }

    // MARK: - APIClient URLProtocol Mock Tests

    func testAPIClientGetMarketsSuccess() async throws {
        let payload: [String: Any] = [
            "markets": [
                ["country_code": "US", "status": "available"],
                ["country_code": "CA", "status": "available"],
                ["country_code": "BR", "status": "qualification_required"]
            ]
        ]
        let data = try json(payload)

        MockURLProtocol.requestHandler = { request in
            XCTAssertTrue(request.url?.path.contains("/api/markets") ?? false)
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, data)
        }

        let client = makeClient()
        let markets = await client.getMarkets()
        XCTAssertNotNil(markets)
        XCTAssertTrue(markets?.isAvailable(countryCode: "US") ?? false)
        XCTAssertFalse(markets?.isAvailable(countryCode: "BR") ?? true)
    }

    func testAPIClientGetMarketsFailureCannotAuthorize() async {
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 500,
                httpVersion: "HTTP/1.1",
                headerFields: nil
            )!
            return (response, Data())
        }

        let client = makeClient()
        let markets = await client.getMarkets()
        XCTAssertNil(markets, "Failure must return nil, preventing accidental admission authorization")
    }

    func testAPIClientCreateContractorTransmitsCountryCode() async throws {
        var recordedBody: [String: Any]?

        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertTrue(request.url?.path.contains("/api/contractors") ?? false)

            if let stream = request.httpBodyStream {
                let data = Data(reading: stream)
                recordedBody = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            } else if let body = request.httpBody {
                recordedBody = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
            }

            let responseData = try! JSONSerialization.data(withJSONObject: [
                "contractor_id": "c_123",
                "status": "ok"
            ])
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: 200,
                httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json"]
            )!
            return (response, responseData)
        }

        let client = makeClient()
        _ = try? await client.createContractor(
            ownerName: "Deli Matsuo",
            businessName: "Kevin Tech",
            serviceType: "general",
            ownerPhone: "+5511987654321",
            countryCode: "BR"
        )

        XCTAssertNotNil(recordedBody)
        XCTAssertEqual(recordedBody?["owner_name"] as? String, "Deli Matsuo")
        XCTAssertEqual(recordedBody?["owner_phone"] as? String, "+5511987654321")
        XCTAssertEqual(recordedBody?["country_code"] as? String, "BR")
    }
}

private extension Data {
    init(reading stream: InputStream) {
        self.init()
        stream.open()
        let bufferSize = 1024
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
        defer {
            buffer.deallocate()
            stream.close()
        }
        while stream.hasBytesAvailable {
            let read = stream.read(buffer, maxLength: bufferSize)
            if read > 0 {
                self.append(buffer, count: read)
            } else {
                break
            }
        }
    }
}
