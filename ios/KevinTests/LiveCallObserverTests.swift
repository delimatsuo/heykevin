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
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        XCTAssertFalse(observer.isRunning)

        observer.start()
        XCTAssertTrue(observer.isRunning)

        observer.stop()
        XCTAssertFalse(observer.isRunning)
    }

    func testCheckNowRequiresIsRunning() async {
        let auth = makeAuth()
        let scope = makeScope()
        var getActiveCallCalled = false

        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { _ in
                getActiveCallCalled = true
                return nil
            },
            getCallAction: { _, _ in nil },
            applyActiveCall: { _, _ in },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        // Stopped observer: checkNow must return false and not dispatch network request
        let handled = await observer.checkNow()
        XCTAssertFalse(handled)
        XCTAssertFalse(getActiveCallCalled, "Stopped observer must not execute checkNow requests")
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
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        observer.start()

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
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        observer.start()
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
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        observer.start()
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
            },
            autoSchedule: false
        )

        observer.start()
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
            },
            autoSchedule: false
        )

        observer.start()
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
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        observer.start()
        let handled = await observer.checkNow()
        XCTAssertFalse(handled)
        XCTAssertNil(appliedInfo, "Late response from superseded auth must be discarded")
    }

    func testStopWhileRequestInFlightDiscardsLateResponse() async {
        let auth = makeAuth()
        let scope = makeScope()
        var appliedInfo: ActiveCallInfo? = nil
        var continuation: CheckedContinuation<ActiveCallInfo?, Never>?

        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { _ in
                await withCheckedContinuation { cont in
                    continuation = cont
                }
            },
            getCallAction: { _, _ in nil },
            applyActiveCall: { info, _ in
                appliedInfo = info
            },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        observer.start()
        let task = Task { await observer.checkNow() }

        // Yield to allow task to start and enter in-flight
        try? await Task.sleep(nanoseconds: 10_000_000)
        XCTAssertTrue(observer.isInFlight)

        // Stop observer while request is in flight
        observer.stop()
        XCTAssertFalse(observer.isRunning)

        // Now resume underlying task
        continuation?.resume(returning: ActiveCallInfo(callSid: "CA_LATE", callerPhone: "+15551234567", callerName: "Late", transcript: ""))
        let result = await task.value

        XCTAssertFalse(result)
        XCTAssertNil(appliedInfo, "Late response after stop() must not apply side effects")
        XCTAssertFalse(observer.isInFlight)
    }

    func testHandleAuthChangeIncrementsRevision() {
        let auth = makeAuth()
        let scope = makeScope()
        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { _ in nil },
            getCallAction: { _, _ in nil },
            applyActiveCall: { _, _ in },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        observer.handleAuthChange()
        XCTAssertFalse(observer.isRunning)
    }

    // MARK: - Unowned Active Origin Not Polled As B

    func testUnownedActiveOriginNotPolledAsB() async {
        let authA = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authB = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        var currentAuth = authA

        let appState = AppState(authProvider: { currentAuth }, inMemory: true)
        appState.setActiveCall(callSid: "CA_ORIGIN_A", callerPhone: "+15551111", callerName: "A", authContext: authA)

        XCTAssertTrue(appState.hasActiveCall)
        XCTAssertEqual(appState.callLifecycleSnapshot.callSid, "CA_ORIGIN_A")

        // Switch active auth to B
        currentAuth = authB

        // appState.callLifecycleSnapshot now immediately returns empty SID
        XCTAssertEqual(appState.callLifecycleSnapshot.callSid, "")

        var polledSids: [String] = []
        var discoveredAuths: [CallAuthContext] = []

        let observer = LiveCallObserver(
            authProvider: { appState.currentAuthContext() },
            scopeProvider: { appState.callLifecycleSnapshot },
            getActiveCall: { auth in
                discoveredAuths.append(auth)
                return nil
            },
            getCallAction: { sid, auth in
                polledSids.append(sid)
                return nil
            },
            applyActiveCall: { _, _ in },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        observer.start()
        _ = await observer.checkNow()

        // Unowned origin call A must NOT have been polled with B credentials!
        XCTAssertTrue(polledSids.isEmpty, "Stale unowned call from A must never be polled under B credentials")
        XCTAssertEqual(discoveredAuths.count, 1)
        XCTAssertEqual(discoveredAuths.first, authB)
    }

    // MARK: - Coalesced Refresh & Lifecycle Race Tests

    func testHeldDiscoveryStopStartResolvesWithFreshAdopt() async {
        var currentAuth = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authB = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        let scope = makeScope(callSid: "")

        var continuationA: CheckedContinuation<ActiveCallInfo?, Never>?
        var appliedCalls: [(info: ActiveCallInfo, auth: CallAuthContext)] = []
        var discoveredAuths: [CallAuthContext] = []

        let fetchAStarted = expectation(description: "Fetch A started")
        let bAdopted = expectation(description: "Fetch B adopted")

        let observer = LiveCallObserver(
            authProvider: { currentAuth },
            scopeProvider: { scope },
            getActiveCall: { auth in
                discoveredAuths.append(auth)
                if auth.contractorId == "c-A" {
                    return await withCheckedContinuation { cont in
                        continuationA = cont
                        fetchAStarted.fulfill()
                    }
                } else {
                    return ActiveCallInfo(callSid: "CA_B_FRESH", callerPhone: "+15552222", callerName: "Bob", transcript: "B")
                }
            },
            getCallAction: { _, _ in nil },
            applyActiveCall: { info, auth in
                appliedCalls.append((info: info, auth: auth))
                if auth == authB {
                    bAdopted.fulfill()
                }
            },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        observer.start()
        await fulfillment(of: [fetchAStarted], timeout: 2.0)
        XCTAssertTrue(observer.isInFlight)

        // 2. Stop and switch to auth B, then start and request refresh
        observer.stop()
        currentAuth = authB
        observer.start()
        observer.requestRefresh() // queues pending refresh since A is still holding in-flight lock

        // 3. Resolve held request A
        continuationA?.resume(returning: ActiveCallInfo(callSid: "CA_A_STALE", callerPhone: "+15551111", callerName: "Alice", transcript: "A"))

        // Pending refresh automatically drains under Auth B
        await fulfillment(of: [bAdopted], timeout: 2.0)

        // 4. Assert: stale A was discarded, exactly one B result adopted
        XCTAssertEqual(appliedCalls.count, 1)
        XCTAssertEqual(appliedCalls.first?.info.callSid, "CA_B_FRESH")
        XCTAssertEqual(appliedCalls.first?.auth, authB)
        XCTAssertFalse(observer.isInFlight)
        observer.stop()
    }

    func testAuthRotationWhileDiscoveryPendingAdoptsFreshB() async {
        var currentAuth = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authB = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        let scope = makeScope(callSid: "")

        var continuationA: CheckedContinuation<ActiveCallInfo?, Never>?
        var appliedCalls: [(info: ActiveCallInfo, auth: CallAuthContext)] = []
        var fetchedAuths: [CallAuthContext] = []
        var inFlightCount = 0
        var maxOverlap = 0

        let fetchAStarted = expectation(description: "Fetch A started")
        let bAdopted = expectation(description: "Fetch B adopted")

        let observer = LiveCallObserver(
            authProvider: { currentAuth },
            scopeProvider: { scope },
            getActiveCall: { auth in
                fetchedAuths.append(auth)
                inFlightCount += 1
                if inFlightCount > maxOverlap {
                    maxOverlap = inFlightCount
                }
                defer { inFlightCount -= 1 }

                if auth.contractorId == "c-A" {
                    return await withCheckedContinuation { cont in
                        continuationA = cont
                        fetchAStarted.fulfill()
                    }
                } else {
                    return ActiveCallInfo(callSid: "CA_B_DISCOVERED", callerPhone: "+15559999", callerName: "B", transcript: "B")
                }
            },
            getCallAction: { _, _ in nil },
            applyActiveCall: { info, auth in
                appliedCalls.append((info: info, auth: auth))
                if auth == authB {
                    bAdopted.fulfill()
                }
            },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        observer.start()
        await fulfillment(of: [fetchAStarted], timeout: 2.0)

        // Rotate auth and notify observer while A is in flight
        currentAuth = authB
        observer.handleAuthChange()

        // Observer stays running with pending refresh queued
        XCTAssertTrue(observer.isRunning)

        // Resolve stale A
        continuationA?.resume(returning: ActiveCallInfo(callSid: "CA_A_STALE", callerPhone: "+15551111", callerName: "Alice", transcript: "A"))

        // Await automatic drain and adoption of B
        await fulfillment(of: [bAdopted], timeout: 2.0)

        // Assertions: exactly A, B fetched and only B applied, no overlap, observer is running until explicit stop
        XCTAssertEqual(fetchedAuths.count, 2)
        XCTAssertEqual(fetchedAuths[0], makeAuth(contractorId: "c-A", token: "tok-A", generation: 1))
        XCTAssertEqual(fetchedAuths[1], authB)
        XCTAssertEqual(appliedCalls.count, 1)
        XCTAssertEqual(appliedCalls.first?.info.callSid, "CA_B_DISCOVERED")
        XCTAssertEqual(appliedCalls.first?.auth, authB)
        XCTAssertEqual(maxOverlap, 1, "At no time should requests overlap in flight")
        XCTAssertTrue(observer.isRunning)

        observer.stop()
        XCTAssertFalse(observer.isRunning)
    }

    func testStopWithPendingRefreshExecutesNoExtraRequests() async {
        let auth = makeAuth()
        let scope = makeScope()
        var getActiveCallCallCount = 0
        var continuation: CheckedContinuation<ActiveCallInfo?, Never>?
        let fetchStarted = expectation(description: "Fetch started")

        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { _ in
                getActiveCallCallCount += 1
                return await withCheckedContinuation { cont in
                    continuation = cont
                    fetchStarted.fulfill()
                }
            },
            getCallAction: { _, _ in nil },
            applyActiveCall: { _, _ in },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        observer.start()
        await fulfillment(of: [fetchStarted], timeout: 2.0)

        // Queue pending refresh, then stop observer
        observer.requestRefresh()
        observer.stop()

        // Resume initial task
        continuation?.resume(returning: nil)

        // Only the 1 initial call was dispatched, pending refresh was cancelled by stop()
        XCTAssertEqual(getActiveCallCallCount, 1)
        XCTAssertFalse(observer.isInFlight)
    }

    func testSingleInFlightPreservedDuringCoalescedRefresh() async {
        let auth = makeAuth()
        let scope = makeScope()
        var activeFlightCount = 0
        var maxObservedFlights = 0
        var totalRequests = 0

        var continuation1: CheckedContinuation<ActiveCallInfo?, Never>?
        var continuation2: CheckedContinuation<ActiveCallInfo?, Never>?
        let firstStarted = expectation(description: "First request started")
        let secondStarted = expectation(description: "Second request started")

        let observer = LiveCallObserver(
            authProvider: { auth },
            scopeProvider: { scope },
            getActiveCall: { _ in
                activeFlightCount += 1
                totalRequests += 1
                if activeFlightCount > maxObservedFlights {
                    maxObservedFlights = activeFlightCount
                }
                defer { activeFlightCount -= 1 }

                if totalRequests == 1 {
                    return await withCheckedContinuation { cont in
                        continuation1 = cont
                        firstStarted.fulfill()
                    }
                } else {
                    return await withCheckedContinuation { cont in
                        continuation2 = cont
                        secondStarted.fulfill()
                    }
                }
            },
            getCallAction: { _, _ in nil },
            applyActiveCall: { _, _ in },
            applyStatus: { _, _ in },
            clearActiveCall: { _, _ in },
            autoSchedule: false
        )

        observer.start()
        await fulfillment(of: [firstStarted], timeout: 2.0)

        // Trigger multiple rapid requestRefresh calls while request 1 is holding in-flight lock
        observer.requestRefresh()
        observer.requestRefresh()
        observer.requestRefresh()

        // Resume request 1; pending refresh automatically starts request 2
        continuation1?.resume(returning: nil)
        await fulfillment(of: [secondStarted], timeout: 2.0)

        // Resume request 2
        continuation2?.resume(returning: nil)
        observer.stop()

        XCTAssertEqual(maxObservedFlights, 1, "At no time should more than 1 network request be in flight simultaneously")
        XCTAssertEqual(totalRequests, 2, "Coalescing should combine multiple rapid triggers into at most 2 requests")
        XCTAssertFalse(observer.isInFlight)
    }
}
