import XCTest
@testable import Kevin

@MainActor
final class LiveCallObserverTests: XCTestCase {

    private func makeAuth(contractorId: String = "c-123", token: String = "tok-1", generation: Int = 1) -> CallAuthContext {
        CallAuthContext(contractorId: contractorId, bearerToken: token, generation: generation)
    }

    private func makeScope(callSid: String = "", revision: Int = 1) -> CallLifecycleSnapshot {
        CallLifecycleSnapshot(callSid: callSid, revision: revision)
    }

    // MARK: - Lifecycle

    func testStartAndStop() {
        let auth = makeAuth()
        let scope = makeScope()
        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { _ in nil },
            getCallAction: { _, _ in nil },
            applyActiveCall: { _, _ in },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in }
        )

        XCTAssertFalse(observer.isRunning)

        observer.start()
        XCTAssertTrue(observer.isRunning)

        observer.stop()
        XCTAssertFalse(observer.isRunning)
        XCTAssertFalse(observer.isInFlight)
    }

    // MARK: - Single In-Flight Request Lock

    func testCheckNowDeduplicatesConcurrentRequests() async {
        let auth = makeAuth()
        let scope = makeScope()
        var getActiveCallCallCount = 0

        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { _ in
                getActiveCallCallCount += 1
                try? await Task.sleep(nanoseconds: 50_000_000) // 50ms
                return ActiveCallInfo(callSid: "CA_1", callerPhone: "+15551234567", callerName: "Alice", transcript: "Hello")
            },
            getCallAction: { _, _ in nil },
            applyActiveCall: { _, _ in },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in }
        )

        // Launch two concurrent checkNow calls
        async let first = observer.checkNow()
        async let second = observer.checkNow()

        _ = await (first, second)

        // Only one network request should have been initiated
        XCTAssertEqual(getActiveCallCallCount, 1)
    }

    // MARK: - Active Call Discovery

    func testDiscoveryAppliesActiveCallWhenFound() async {
        let auth = makeAuth()
        let scope = makeScope(callSid: "") // No active call
        var appliedInfo: ActiveCallInfo? = nil
        var appliedAuth: CallAuthContext? = nil

        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { passedAuth in
                ActiveCallInfo(callSid: "CA_FOUND_123", callerPhone: "+15559876543", callerName: "Bob", transcript: "Kevin: Hi\nCaller: Hello")
            },
            getCallAction: { _, _ in nil },
            applyActiveCall: { info, authCtx in
                appliedInfo = info
                appliedAuth = authCtx
            },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in }
        )

        let handled = await observer.checkNow()
        XCTAssertTrue(handled)
        XCTAssertNotNil(appliedInfo)
        XCTAssertEqual(appliedInfo?.callSid, "CA_FOUND_123")
        XCTAssertEqual(appliedAuth, auth)
    }

    // MARK: - Status Polling

    func testPollingAppliesStatusForActiveCall() async {
        let auth = makeAuth(contractorId: "c-100")
        let scope = makeScope(callSid: "CA_ACTIVE_1")
        var appliedResult: CallActionResult? = nil

        let statusResult = CallActionResult(
            callSid: "CA_ACTIVE_1",
            contractorId: "c-100",
            operationId: "op-1",
            action: "accept",
            actionStatus: "ready",
            accessToken: nil,
            conferenceName: nil,
            isActive: true,
            isUrgent: false,
            callerName: "Bob",
            callerPhone: "+15559876543",
            transcript: "Kevin: Hi",
            statusCode: 200,
            rawStatus: "ok",
            errorDetail: nil
        )

        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { _ in nil },
            getCallAction: { sid, authCtx in
                return statusResult
            },
            applyActiveCall: { _, _ in },
            applyStatus: { result, _ in
                appliedResult = result
            },
            clearActiveCall: { _, _ in }
        )

        let handled = await observer.checkNow()
        XCTAssertTrue(handled)
        XCTAssertNotNil(appliedResult)
        XCTAssertEqual(appliedResult?.callSid, "CA_ACTIVE_1")
    }

    func testPollingClearsCallWhenEnded() async {
        let auth = makeAuth(contractorId: "c-100")
        let scope = makeScope(callSid: "CA_ACTIVE_1")
        var clearedSid: String? = nil

        let endedResult = CallActionResult(
            callSid: "CA_ACTIVE_1",
            contractorId: "c-100",
            operationId: "",
            action: "",
            actionStatus: "ended",
            accessToken: nil,
            conferenceName: nil,
            isActive: false,
            isUrgent: false,
            callerName: nil,
            callerPhone: nil,
            transcript: nil,
            statusCode: 200,
            rawStatus: "ok",
            errorDetail: nil,
            hasExplicitActive: true
        )

        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { _ in nil },
            getCallAction: { _, _ in endedResult },
            applyActiveCall: { _, _ in },
            applyStatus: { _, _ in },
            clearActiveCall: { sid, _ in
                clearedSid = sid
            }
        )

        let handled = await observer.checkNow()
        XCTAssertTrue(handled)
        XCTAssertEqual(clearedSid, "CA_ACTIVE_1")
    }

    // MARK: - Fail Safe on Unknown Status

    func testPollingRetainsCallOnNetworkError() async {
        let auth = makeAuth()
        let scope = makeScope(callSid: "CA_ACTIVE_1")
        var clearCalled = false

        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { _ in nil },
            getCallAction: { _, _ in
                throw URLError(.timedOut)
            },
            applyActiveCall: { _, _ in },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in
                clearCalled = true
            }
        )

        let handled = await observer.checkNow()
        XCTAssertFalse(handled)
        XCTAssertFalse(clearCalled, "Active call must be retained when status poll fails with network error")
    }

    // MARK: - Auth and Scope Change Guards

    func testDiscoveryDiscardsResponseIfAuthChangedDuringRequest() async {
        var currentAuth = makeAuth(contractorId: "c-A", generation: 1)
        let scope = makeScope(callSid: "")
        var appliedInfo: ActiveCallInfo? = nil

        let observer = LiveCallObserver(
            authProvider: { currentAuth },
            scopeProvider: { scope },
            getActiveCall: { _ in
                // Simulate auth change during request
                currentAuth = CallAuthContext(contractorId: "c-B", bearerToken: "tok-B", generation: 2)
                return ActiveCallInfo(callSid: "CA_DISCARD", callerPhone: "+15551234567", callerName: "A", transcript: "")
            },
            getCallAction: { _, _ in nil },
            applyActiveCall: { info, _ in
                appliedInfo = info
            },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in }
        )

        let handled = await observer.checkNow()
        XCTAssertFalse(handled)
        XCTAssertNil(appliedInfo, "Late response from superseded auth must be discarded")
    }

    func testHandleAuthChangeIncrementsRevisionAndClearsInFlight() {
        let auth = makeAuth()
        let scope = makeScope()
        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { _ in nil },
            getCallAction: { _, _ in nil },
            applyActiveCall: { _, _ in },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in }
        )

        observer.handleAuthChange()
        XCTAssertFalse(observer.isInFlight)
    }
}
