import Foundation
import SwiftUI
import XCTest
@testable import Kevin

private final class Slice5BURLProtocol: URLProtocol, @unchecked Sendable {
    static var requestHandler: ((URLRequest) throws -> (HTTPURLResponse, Data))?
    static var recordedRequests: [URLRequest] = []
    private static let lock = NSLock()

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

    static func snapshotRequests() -> [URLRequest] {
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

@MainActor
final class OnboardingForwardingStepTests: XCTestCase {
    private var testDefaults: UserDefaults!
    private var testSuiteName: String!
    private var mockSession: URLSession!

    override func setUp() {
        super.setUp()
        testSuiteName = "OnboardingForwardingStepTests.\(UUID().uuidString)"
        testDefaults = UserDefaults(suiteName: testSuiteName)!
        Slice5BURLProtocol.reset()
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [Slice5BURLProtocol.self]
        mockSession = URLSession(configuration: config)
    }

    override func tearDown() {
        Slice5BURLProtocol.reset()
        mockSession.invalidateAndCancel()
        mockSession = nil
        testDefaults.removePersistentDomain(forName: testSuiteName)
        testDefaults = nil
        testSuiteName = nil
        super.tearDown()
    }

    private func extractBodyData(from request: URLRequest) -> Data? {
        if let body = request.httpBody {
            return body
        }
        guard let stream = request.httpBodyStream else {
            return nil
        }
        stream.open()
        defer { stream.close() }
        var data = Data()
        let bufferSize = 4096
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
        defer { buffer.deallocate() }
        while stream.hasBytesAvailable {
            let count = stream.read(buffer, maxLength: bufferSize)
            if count < 0 {
                return nil
            }
            if count == 0 {
                break
            }
            data.append(buffer, count: count)
        }
        return data
    }

    private func makeHTTPResponse(for request: URLRequest, statusCode: Int) throws -> HTTPURLResponse {
        let url = try XCTUnwrap(request.url)
        return try XCTUnwrap(HTTPURLResponse(
            url: url,
            statusCode: statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        ))
    }

    // MARK: - OnboardingForwardingStepView Binding & Dial Code Resolution

    func testUSCarrierBindingPropagation_updatesDialCodesAndAppState() {
        let testAppState = AppState(inMemory: true)
        testAppState.isVerizonCarrier = false

        var boundIsVerizon = false
        var boundInstructions: ForwardingInstructions? = nil
        var completedDecisions: [Bool] = []

        let makeStepView = {
            OnboardingForwardingStepView(
                kevinNumber: "+1 (415) 555-1234",
                countryCode: "US",
                mode: "business",
                isVerizon: Binding(
                    get: { boundIsVerizon },
                    set: { boundIsVerizon = $0 }
                ),
                forwardingInstructions: Binding(
                    get: { boundInstructions },
                    set: { boundInstructions = $0 }
                ),
                onComplete: { didSetUp in
                    completedDecisions.append(didSetUp)
                }
            )
        }

        let initialView = makeStepView()
        XCTAssertEqual(
            initialView.forwardingCodes,
            ForwardingCodes(
                activate: "*61*14155551234#",
                deactivate: "##61#",
                clearExisting: "##21#",
                clearAll: "##002#",
                isServerDriven: false
            )
        )

        initialView.applyCarrierSelection(isVerizon: true, to: testAppState)
        XCTAssertTrue(boundIsVerizon)
        XCTAssertTrue(testAppState.isVerizonCarrier)

        let verizonView = makeStepView()
        XCTAssertEqual(
            verizonView.forwardingCodes,
            ForwardingCodes(
                activate: "*7114155551234",
                deactivate: "*73",
                clearExisting: "*73",
                clearAll: nil,
                isServerDriven: false
            )
        )

        verizonView.applyCarrierSelection(isVerizon: false, to: testAppState)
        XCTAssertFalse(boundIsVerizon)
        XCTAssertFalse(testAppState.isVerizonCarrier)

        let restoredGSMView = makeStepView()
        XCTAssertEqual(
            restoredGSMView.forwardingCodes,
            ForwardingCodes(
                activate: "*61*14155551234#",
                deactivate: "##61#",
                clearExisting: "##21#",
                clearAll: "##002#",
                isServerDriven: false
            )
        )

        restoredGSMView.completeStep(didSetUp: true)
        restoredGSMView.completeStep(didSetUp: false)
        XCTAssertEqual(completedDecisions, [true, false])
    }

    func testForwardingInstructionsBindingPropagation_fetchesOnlyForNonNANP() async {
        var boundIsVerizon = false
        var boundInstructions: ForwardingInstructions? = nil
        var fetchedCountries: [String] = []

        let expectedBrazilInstructions = ForwardingInstructions(
            countryCode: "BR",
            forwardUnansweredTemplate: "**61*{number}#",
            disableUnanswered: "##61#",
            disableAll: "##21#",
            disableEverything: "##002#"
        )

        for nanpCountry in ["US", "CA"] {
            let nanpView = OnboardingForwardingStepView(
                kevinNumber: "+14155551234",
                countryCode: nanpCountry,
                mode: "personal",
                isVerizon: Binding(get: { boundIsVerizon }, set: { boundIsVerizon = $0 }),
                forwardingInstructions: Binding(get: { boundInstructions }, set: { boundInstructions = $0 }),
                onComplete: { _ in }
            )
            await nanpView.loadForwardingInstructionsIfNeeded { country in
                fetchedCountries.append(country)
                return expectedBrazilInstructions
            }
            XCTAssertTrue(fetchedCountries.isEmpty)
            XCTAssertNil(boundInstructions)
            if nanpCountry == "CA" {
                XCTAssertNil(nanpView.forwardingCodes)
            } else {
                XCTAssertNotNil(nanpView.forwardingCodes)
            }
        }

        let brView = OnboardingForwardingStepView(
            kevinNumber: "+5511987654321",
            countryCode: "BR",
            mode: "business",
            isVerizon: Binding(get: { boundIsVerizon }, set: { boundIsVerizon = $0 }),
            forwardingInstructions: Binding(get: { boundInstructions }, set: { boundInstructions = $0 }),
            onComplete: { _ in }
        )

        await brView.loadForwardingInstructionsIfNeeded { country in
            fetchedCountries.append(country)
            return expectedBrazilInstructions
        }

        XCTAssertEqual(fetchedCountries, ["BR"])
        XCTAssertEqual(boundInstructions, expectedBrazilInstructions)
        XCTAssertNil(brView.forwardingCodes)
    }

    // MARK: - APIClient+CallActions 401 Session Fence

    func testSendAndGetCallAction_signalReauthOnlyWhenSessionMatchesOn401() async throws {
        let previousAuth = AppState.shared.currentAuthContext()
        let previousSessionState = AppState.shared.sessionState
        let previousNeedsReauth = AppState.shared.needsReauth
        defer {
            AppState.shared.contractorId = previousAuth.contractorId
            _ = AppState.shared.finishAccountRecovery(bearerToken: previousAuth.bearerToken)
            AppState.shared.sessionState = previousSessionState
            AppState.shared.needsReauth = previousNeedsReauth
        }

        AppState.shared.contractorId = "cnt_call_fence"
        XCTAssertTrue(AppState.shared.finishAccountRecovery(bearerToken: "tok_call_fence_live"))
        let liveAuth = AppState.shared.currentAuthContext()
        XCTAssertFalse(AppState.shared.needsReauth)

        let store = RecoveryTestKeychain()
        let persistence = AccountPersistence(defaults: testDefaults, keychain: store.client, epoch: .shared)
        let client = APIClient(session: mockSession, persistence: persistence)

        var responseStatusCode = 401
        Slice5BURLProtocol.requestHandler = { [self] request in
            let response = try makeHTTPResponse(for: request, statusCode: responseStatusCode)
            let payload: [String: Any] = responseStatusCode == 200
                ? [
                    "status": "ok",
                    "call_sid": "CA_fence_1",
                    "contractor_id": liveAuth.contractorId,
                    "operation_id": "op_1",
                    "action": "decline",
                    "action_status": "taking_message",
                    "active": true,
                ]
                : ["detail": "Unauthorized"]
            let data = try JSONSerialization.data(withJSONObject: payload)
            return (response, data)
        }

        // 1. Stale generation on 401 must NOT set needsReauth for sendCallAction or getCallAction.
        _ = try await client.sendCallAction(
            callSid: "CA_fence_1",
            action: "decline",
            operationId: "op_1",
            contractorId: liveAuth.contractorId,
            bearerToken: liveAuth.bearerToken,
            sessionGeneration: liveAuth.generation - 1
        )
        XCTAssertFalse(AppState.shared.needsReauth)

        _ = try await client.getCallAction(
            callSid: "CA_fence_1",
            operationId: "op_1",
            contractorId: liveAuth.contractorId,
            bearerToken: liveAuth.bearerToken,
            sessionGeneration: liveAuth.generation - 1
        )
        XCTAssertFalse(AppState.shared.needsReauth)

        // 2. Stale bearerToken on 401 must NOT set needsReauth.
        _ = try await client.sendCallAction(
            callSid: "CA_fence_1",
            action: "decline",
            operationId: "op_1",
            contractorId: liveAuth.contractorId,
            bearerToken: "tok_call_fence_stale",
            sessionGeneration: liveAuth.generation
        )
        XCTAssertFalse(AppState.shared.needsReauth)

        // 3. HTTP 200 with matching live auth must NOT set needsReauth.
        responseStatusCode = 200
        let okResult = try await client.sendCallAction(
            callSid: "CA_fence_1",
            action: "decline",
            operationId: "op_1",
            contractorId: liveAuth.contractorId,
            bearerToken: liveAuth.bearerToken,
            sessionGeneration: liveAuth.generation
        )
        XCTAssertTrue(okResult.isTakingMessage)
        XCTAssertFalse(AppState.shared.needsReauth)

        // 4. HTTP 401 with exact matching live auth sets needsReauth for sendCallAction.
        responseStatusCode = 401
        let unauthorizedSend = try await client.sendCallAction(
            callSid: "CA_fence_1",
            action: "decline",
            operationId: "op_1",
            contractorId: liveAuth.contractorId,
            bearerToken: liveAuth.bearerToken,
            sessionGeneration: liveAuth.generation
        )
        XCTAssertEqual(unauthorizedSend.statusCode, 401)
        XCTAssertTrue(AppState.shared.needsReauth)

        // 5. Reset needsReauth and verify getCallAction also sets needsReauth on matching 401.
        AppState.shared.needsReauth = false
        let unauthorizedGet = try await client.getCallAction(
            callSid: "CA_fence_1",
            operationId: "op_1",
            contractorId: liveAuth.contractorId,
            bearerToken: liveAuth.bearerToken,
            sessionGeneration: liveAuth.generation
        )
        XCTAssertEqual(unauthorizedGet?.statusCode, 401)
        XCTAssertTrue(AppState.shared.needsReauth)
    }

    // MARK: - APIClient+OwnerSMS Consent Snapshot & Session

    func testOwnerSMSConsentSession_usesDedicatedExtendedTimeoutConfiguration() {
        let store = RecoveryTestKeychain()
        let persistence = AccountPersistence(defaults: testDefaults, keychain: store.client, epoch: .shared)
        let defaultClient = APIClient(persistence: persistence)

        XCTAssertEqual(defaultClient.session.configuration.timeoutIntervalForRequest, 10)
        XCTAssertEqual(defaultClient.session.configuration.timeoutIntervalForResource, 15)
        XCTAssertEqual(defaultClient.effectiveOwnerSMSConsentSession.configuration.timeoutIntervalForRequest, 60)
        XCTAssertEqual(defaultClient.effectiveOwnerSMSConsentSession.configuration.timeoutIntervalForResource, 90)
        XCTAssertFalse(defaultClient.effectiveOwnerSMSConsentSession === defaultClient.session)
    }

    func testRecordOwnerSMSConsent_bindsSnapshotAuthAndSingleAttemptSemantics() async throws {
        let previousNeedsReauth = AppState.shared.needsReauth
        defer { AppState.shared.needsReauth = previousNeedsReauth }
        AppState.shared.needsReauth = false

        let store = RecoveryTestKeychain()
        store.values = ["contractorApiToken": "stale-persisted-token"]
        let persistence = AccountPersistence(defaults: testDefaults, keychain: store.client, epoch: .shared)
        let client = APIClient(session: mockSession, persistence: persistence)

        // 1. Invalid auth or negative expectedRevision fails before touching the network.
        let invalidSnapshot = OwnerSMSConsentSnapshot(
            auth: CallAuthContext(contractorId: "", bearerToken: "tok", generation: 1),
            ownerPhoneE164: "+14165550100",
            twilioNumber: "+14165550199",
            expectedRevision: 0,
            fenceRevision: 1
        )
        if case .failure = await client.recordOwnerSMSConsent(snapshot: invalidSnapshot) {
            XCTAssertTrue(Slice5BURLProtocol.snapshotRequests().isEmpty)
        } else {
            XCTFail("Expected invalid auth consent snapshot to fail locally")
        }

        let negativeRevisionSnapshot = OwnerSMSConsentSnapshot(
            auth: CallAuthContext(contractorId: "cnt_ca_1", bearerToken: "snapshot-bound-token", generation: 1),
            ownerPhoneE164: "+14165550100",
            twilioNumber: "+14165550199",
            expectedRevision: -1,
            fenceRevision: 1
        )
        if case .failure = await client.recordOwnerSMSConsent(snapshot: negativeRevisionSnapshot) {
            XCTAssertTrue(Slice5BURLProtocol.snapshotRequests().isEmpty)
        } else {
            XCTFail("Expected negative expectedRevision consent snapshot to fail locally")
        }

        // 2. Valid snapshot uses snapshot.auth.bearerToken (never persistence.contractorToken) and strict consent parser.
        let validSnapshot = OwnerSMSConsentSnapshot(
            auth: CallAuthContext(contractorId: "cnt_ca_1", bearerToken: "snapshot-bound-token", generation: 2),
            ownerPhoneE164: "+14165550100",
            twilioNumber: "+14165550199",
            expectedRevision: 3,
            fenceRevision: 7
        )

        Slice5BURLProtocol.requestHandler = { [self] request in
            let response = try makeHTTPResponse(for: request, statusCode: 200)
            let data = try JSONSerialization.data(withJSONObject: [
                "status": "ok",
                "owner_sms_consent_required": false,
                "owner_sms_enabled": true,
                "owner_sms_opted_out": false,
                "owner_sms_opt_out_revision": 3,
                "owner_sms_settings_version": 1,
            ])
            return (response, data)
        }

        let confirmed = await client.recordOwnerSMSConsent(snapshot: validSnapshot)
        XCTAssertEqual(confirmed, .success(enabled: true, optedOut: false, revision: 3, version: 1))

        let requests = Slice5BURLProtocol.snapshotRequests()
        XCTAssertEqual(requests.count, 1)
        let captured = try XCTUnwrap(requests.first)
        XCTAssertEqual(captured.httpMethod, "POST")
        XCTAssertEqual(captured.timeoutInterval, 60)
        XCTAssertTrue(captured.url?.path.hasSuffix("/api/contractors/cnt_ca_1/owner-sms-consent") == true)
        XCTAssertEqual(captured.value(forHTTPHeaderField: "Authorization"), "Bearer snapshot-bound-token")

        let bodyData = try XCTUnwrap(extractBodyData(from: captured))
        let bodyJSON = try XCTUnwrap(JSONSerialization.jsonObject(with: bodyData) as? [String: Any])
        XCTAssertEqual(bodyJSON["disclosure_version"] as? String, OwnerSMSConsentSnapshot.disclosureVersion)
        XCTAssertEqual(bodyJSON["owner_phone_e164"] as? String, "+14165550100")
        XCTAssertEqual(bodyJSON["twilio_number"] as? String, "+14165550199")
        XCTAssertEqual(bodyJSON["expected_revision"] as? Int, 3)

        // 3. HTTP 500 must NOT retry (single attempt only) and HTTP 401 must NOT trigger needsReauth.
        Slice5BURLProtocol.reset()
        Slice5BURLProtocol.requestHandler = { [self] request in
            let response = try makeHTTPResponse(for: request, statusCode: 500)
            return (response, Data("{\"error\":\"server\"}".utf8))
        }
        let serverFailure = await client.recordOwnerSMSConsent(snapshot: validSnapshot)
        if case .failure = serverFailure {
            XCTAssertEqual(Slice5BURLProtocol.snapshotRequests().count, 1)
        } else {
            XCTFail("Expected HTTP 500 consent attempt to fail without retry")
        }

        store.values = ["contractorApiToken": "snapshot-bound-token"]
        Slice5BURLProtocol.reset()
        Slice5BURLProtocol.requestHandler = { [self] request in
            let response = try makeHTTPResponse(for: request, statusCode: 401)
            return (response, Data("{\"detail\":\"unauthorized\"}".utf8))
        }
        let unauthorizedResult = await client.recordOwnerSMSConsent(snapshot: validSnapshot)
        if case .failure = unauthorizedResult {
            XCTAssertEqual(Slice5BURLProtocol.snapshotRequests().count, 1)
            XCTAssertFalse(AppState.shared.needsReauth)
        } else {
            XCTFail("Expected HTTP 401 consent attempt to fail")
        }
    }

    // MARK: - APIClient+Integrations getJobberStatus 401 Session Fence

    func testGetJobberStatus_signalsReauthOnlyForCurrentTokenOn401() async throws {
        let previousNeedsReauth = AppState.shared.needsReauth
        defer { AppState.shared.needsReauth = previousNeedsReauth }
        AppState.shared.needsReauth = false

        let store = RecoveryTestKeychain()
        store.values = ["contractorApiToken": "tok_jobber_current"]
        let persistence = AccountPersistence(defaults: testDefaults, keychain: store.client, epoch: .shared)
        let client = APIClient(session: mockSession, persistence: persistence)

        Slice5BURLProtocol.requestHandler = { [self] request in
            let response = try makeHTTPResponse(for: request, statusCode: 401)
            return (response, Data("{\"detail\":\"Unauthorized\"}".utf8))
        }

        // 1. Stale bearerToken (differs from persistence.contractorToken) must NOT set needsReauth on 401.
        let staleAuth = CallAuthContext(
            contractorId: "cnt_jobber_1",
            bearerToken: "tok_jobber_stale",
            generation: CallSessionEpoch.shared.generation
        )
        do {
            _ = try await client.getJobberStatus(auth: staleAuth)
            XCTFail("Expected 401 Jobber status to throw")
        } catch let error as JobberManagementError {
            XCTAssertEqual(error, .httpError(statusCode: 401))
        }
        XCTAssertFalse(AppState.shared.needsReauth)

        // 2. Matching current bearerToken + epoch sets needsReauth = true on 401 without retrying.
        Slice5BURLProtocol.reset()
        Slice5BURLProtocol.requestHandler = { [self] request in
            let response = try makeHTTPResponse(for: request, statusCode: 401)
            return (response, Data("{\"detail\":\"Unauthorized\"}".utf8))
        }
        let currentAuth = CallAuthContext(
            contractorId: "cnt_jobber_1",
            bearerToken: "tok_jobber_current",
            generation: CallSessionEpoch.shared.generation
        )
        do {
            _ = try await client.getJobberStatus(auth: currentAuth)
            XCTFail("Expected 401 Jobber status to throw")
        } catch let error as JobberManagementError {
            XCTAssertEqual(error, .httpError(statusCode: 401))
        }
        XCTAssertEqual(Slice5BURLProtocol.snapshotRequests().count, 1)
        XCTAssertTrue(AppState.shared.needsReauth)
    }
}
