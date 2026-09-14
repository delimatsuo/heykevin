import XCTest
@testable import Kevin

@MainActor
private final class ActionHarness {
    var auth = CallAuthContext(contractorId: "owner", bearerToken: "credential", generation: 1)
    var scope = CallLifecycleSnapshot(callSid: "call-A", revision: 1)
    var posts = 0
    var gets = 0
    var connections = 0
    var messages = 0
    var sid = ""
    var action = ""
    var op = ""
    var postWaiter: CheckedContinuation<CallActionResult, Error>?
    var getWaiter: CheckedContinuation<CallActionResult?, Error>?
    var suspendGet = false
    var getResult: CallActionResult?
    lazy var coordinator = CallActionCoordinator(
        sendAction: { [unowned self] _, sid, action, op, _ in
            self.posts += 1; self.sid = sid; self.action = action; self.op = op
            return try await withCheckedThrowingContinuation { self.postWaiter = $0 }
        },
        getStatus: { [unowned self] _, _, op in
            self.gets += 1
            XCTAssertEqual(op, self.op)
            if self.suspendGet { return try await withCheckedThrowingContinuation { self.getWaiter = $0 } }
            return self.getResult
        },
        connect: { [unowned self] _, sid, _, _ in
            XCTAssertEqual(sid, "call-A"); self.connections += 1; return true
        },
        directAnswer: { _, _ in nil },
        currentAuth: { [unowned self] in self.auth },
        currentCall: { [unowned self] in self.scope },
        onMessage: { [unowned self] _ in self.messages += 1 }, pollAttempts: 1)
    func result(status: String = "accepted", owner: String = "owner", sid: String? = nil,
                operation: String? = nil, action: String? = nil, http: Int = 200, active: Bool = true) -> CallActionResult {
        CallActionResult(callSid: sid ?? self.sid, contractorId: owner, operationId: operation ?? op,
            action: action ?? self.action, actionStatus: status,
            accessToken: status == "accepted" ? "token" : nil,
            conferenceName: status == "accepted" ? "conference" : nil,
            isActive: active, isUrgent: false, callerName: nil, callerPhone: nil, transcript: nil,
            statusCode: http, rawStatus: http == 202 ? "pending" : "ok", errorDetail: nil)
    }
    func reply(_ result: CallActionResult) { let c = postWaiter; postWaiter = nil; c?.resume(returning: result) }
    func waitForPost() async { for _ in 0..<200 where postWaiter == nil { await Task.yield() }; XCTAssertNotNil(postWaiter) }
    func waitForGet() async { for _ in 0..<200 where getWaiter == nil { await Task.yield() }; XCTAssertNotNil(getWaiter) }
}

final class CallActionTests: XCTestCase {
    @MainActor func testDuplicateEntryPointsShareOnePostAndConnection() async {
        let h = ActionHarness()
        let first = Task { await h.coordinator.pickUp(callSid: "call-A") }
        await h.waitForPost()
        let duplicate = Task { await h.coordinator.pickUp(callSid: "call-A") }
        await Task.yield()
        let opposite = await h.coordinator.takeMessage(callSid: "call-A")
        XCTAssertFalse(opposite)
        h.reply(h.result())
        let firstResult = await first.value, secondResult = await duplicate.value
        XCTAssertTrue(firstResult); XCTAssertTrue(secondResult)
        XCTAssertEqual(h.posts, 1); XCTAssertEqual(h.connections, 1)
    }
    @MainActor func testOldCallResponseCannotConnectAfterAnotherCallOrRevision() async {
        for replacement in [CallLifecycleSnapshot(callSid: "call-B", revision: 2), CallLifecycleSnapshot(callSid: "call-A", revision: 3)] {
            let h = ActionHarness()
            let pending = Task { await h.coordinator.pickUp(callSid: "call-A") }
            await h.waitForPost(); h.scope = replacement; h.reply(h.result())
            let result = await pending.value
            XCTAssertFalse(result); XCTAssertEqual(h.connections, 0)
        }
    }
    @MainActor func testSessionRoundTripInvalidatesSuspendedPost() async {
        let h = ActionHarness(), epoch = CallSessionEpoch()
        let initial = epoch.generation
        h.auth = CallAuthContext(contractorId: "owner", bearerToken: "credential", generation: initial)
        let pending = Task { await h.coordinator.pickUp(callSid: "call-A") }
        await h.waitForPost()
        epoch.credentialChanged(from: "credential", to: "other")
        epoch.credentialChanged(from: "other", to: "credential")
        XCTAssertEqual(epoch.generation, initial + 2)
        h.auth = CallAuthContext(contractorId: "owner", bearerToken: "credential", generation: epoch.generation)
        h.reply(h.result())
        let result = await pending.value
        XCTAssertFalse(result); XCTAssertEqual(h.connections, 0)
    }
    @MainActor func testUnknownDeclineRetainsOperationAndCheckUsesOnlyGet() async {
        let h = ActionHarness()
        let pending = Task { await h.coordinator.takeMessage(callSid: "call-A") }
        await h.waitForPost()
        h.getResult = h.result(status: "message_requested", http: 202)
        h.reply(h.result(status: "message_requested", http: 202))
        let initial = await pending.value
        XCTAssertFalse(initial); XCTAssertEqual(h.messages, 0)
        XCTAssertTrue(h.coordinator.canCheckStatus(for: "call-A"))
        XCTAssertTrue(h.coordinator.isActionPending(for: "call-A"))
        let opposite = await h.coordinator.pickUp(callSid: "call-A")
        XCTAssertFalse(opposite)
        h.getResult = h.result(status: "taking_message")
        let resolved = await h.coordinator.checkStatus(callSid: "call-A")
        XCTAssertTrue(resolved); XCTAssertEqual(h.posts, 1); XCTAssertEqual(h.gets, 2); XCTAssertEqual(h.messages, 1)
    }
    @MainActor func testReconciliationChecksSessionAfterSuspendedGet() async {
        let h = ActionHarness(); h.suspendGet = true
        let pending = Task { await h.coordinator.pickUp(callSid: "call-A") }
        await h.waitForPost(); h.reply(h.result(status: "accepting", http: 202)); await h.waitForGet()
        h.auth = CallAuthContext(contractorId: "owner", bearerToken: "changed", generation: 2)
        h.getWaiter?.resume(returning: h.result()); h.getWaiter = nil
        let result = await pending.value
        XCTAssertFalse(result); XCTAssertEqual(h.connections, 0); XCTAssertEqual(h.posts, 1)
    }
    @MainActor func testWrongIdentityActionHTTPAndInactiveCannotConnect() async {
        for invalid in 0..<6 {
            let h = ActionHarness()
            let task = Task { await h.coordinator.pickUp(callSid: "call-A") }
            await h.waitForPost()
            let result: CallActionResult
            switch invalid {
            case 0: result = h.result(owner: "wrong")
            case 1: result = h.result(sid: "call-B")
            case 2: result = h.result(operation: "wrong")
            case 3: result = h.result(action: "decline")
            case 4: result = h.result(http: 500)
            default: result = h.result(active: false)
            }
            h.reply(result)
            let accepted = await task.value
            XCTAssertFalse(accepted); XCTAssertEqual(h.connections, 0)
        }
    }
    func testIncomingOwnershipRejectsDuplicateExpiredMissingAndOldUUID() {
        let auth = CallAuthContext(contractorId: "owner", bearerToken: "token", generation: 1)
        let now = Date(timeIntervalSince1970: 1000)
        func context(_ sid: String, expiry: Double = 1030, preissued: Bool = false) -> IncomingCallContext {
            IncomingCallContext(uuid: UUID(), callSid: sid, auth: auth, deadline: Date(timeIntervalSince1970: expiry),
                accessToken: preissued ? "token" : "", conferenceName: preissued ? "conference" : "",
                callerName: "", callerPhone: "", isUrgent: !preissued)
        }
        var owner = IncomingCallOwnership()
        let a = context("call-A"), b = context("call-B", preissued: true)
        XCTAssertTrue(owner.adopt(a, currentAuth: auth, now: now))
        XCTAssertFalse(owner.adopt(context("call-A"), currentAuth: auth, now: now))
        XCTAssertNil(owner.usable(UUID(), auth: auth, now: now))
        XCTAssertNil(owner.usable(a.uuid, auth: auth, now: now.addingTimeInterval(30)))
        XCTAssertTrue(owner.remove(a.uuid))
        XCTAssertTrue(owner.adopt(b, currentAuth: auth, now: now))
        XCTAssertFalse(owner.remove(a.uuid))
        XCTAssertEqual(owner.activeUUID, b.uuid)
        XCTAssertTrue(owner.usable(b.uuid, auth: auth, now: now)!.hasPreissuedToken)
        XCTAssertTrue(owner.seenCallSids.contains("call-A"))
    }
    @MainActor func testExactEndedAndConflictReconciliationAreTerminal() async {
        for conflict in [false, true] {
            let h = ActionHarness()
            let task = Task { await h.coordinator.pickUp(callSid: "call-A") }
            await h.waitForPost()
            h.getResult = CallActionResult(callSid: h.sid, contractorId: "owner", operationId: h.op,
                action: "", actionStatus: conflict ? "action_conflict" : "ended", accessToken: nil, conferenceName: nil,
                isActive: conflict, isUrgent: false, callerName: nil, callerPhone: nil, transcript: nil,
                statusCode: conflict ? 409 : 200, rawStatus: conflict ? "error" : "ok", errorDetail: nil)
            h.reply(h.result(status: "accepting", http: 202))
            let result = await task.value
            XCTAssertFalse(result); XCTAssertEqual(h.connections, 0); XCTAssertFalse(h.coordinator.canCheckStatus(for: "call-A"))
            let retry = await h.coordinator.pickUp(callSid: "call-A")
            XCTAssertFalse(retry); XCTAssertEqual(h.posts, 1)
            if conflict { XCTAssertNotNil(h.coordinator.errorMessage(for: "call-A")) }
            else { XCTAssertEqual(h.coordinator.state(for: "call-A"), .ended) }
        }
    }
    @MainActor func testAuthoritativeTimeoutMessageProjectionDoesNotInventOperationOwnership() {
        let h = ActionHarness()
        let status = CallActionResult(callSid: "call-A", contractorId: "owner", operationId: "", action: "timeout",
            actionStatus: "taking_message", accessToken: nil, conferenceName: nil, isActive: true,
            isUrgent: true, callerName: nil, callerPhone: nil, transcript: nil, statusCode: 200, rawStatus: "ok", errorDetail: nil)
        h.coordinator.observeStatus(status, auth: h.auth)
        XCTAssertTrue(h.coordinator.isTakingMessage(for: "call-A"))
        XCTAssertTrue(h.coordinator.isActionPending(for: "call-A"))
        XCTAssertEqual(h.messages, 1); XCTAssertEqual(h.posts, 0)
        XCTAssertFalse(h.coordinator.canCheckStatus(for: "call-A"))
        h.scope = CallLifecycleSnapshot(callSid: "call-B", revision: 2)
        h.coordinator.observeStatus(status, auth: h.auth)
        XCTAssertEqual(h.messages, 1)
        XCTAssertFalse(h.coordinator.isTakingMessage(for: "call-B"))
    }
    @MainActor func testProvenPreparationFailureRequiresExplicitNewOperation() async {
        let h = ActionHarness()
        let first = Task { await h.coordinator.pickUp(callSid: "call-A") }
        await h.waitForPost()
        let originalOperation = h.op
        var failure = CallActionResult(callSid: h.sid, contractorId: "owner", operationId: h.op,
            action: "accept", actionStatus: "preparation_failed", accessToken: nil, conferenceName: nil,
            isActive: true, isUrgent: false, callerName: nil, callerPhone: nil, transcript: nil,
            statusCode: 503, rawStatus: "error", errorDetail: nil)
        failure.retryable = true
        h.reply(failure)
        let failed = await first.value
        XCTAssertFalse(failed); XCTAssertEqual(h.posts, 1); XCTAssertEqual(h.gets, 0)
        XCTAssertFalse(h.coordinator.isActionPending(for: "call-A"))
        let retry = Task { await h.coordinator.pickUp(callSid: "call-A") }
        await h.waitForPost()
        XCTAssertNotEqual(h.op, originalOperation)
        h.reply(h.result())
        let result = await retry.value
        XCTAssertTrue(result); XCTAssertEqual(h.posts, 2); XCTAssertEqual(h.connections, 1)
    }
    @MainActor func testUnproven503NeverPermitsNewPost() async {
        let h = ActionHarness()
        let first = Task { await h.coordinator.pickUp(callSid: "call-A") }
        await h.waitForPost()
        let failure = CallActionResult(callSid: h.sid, contractorId: "owner", operationId: h.op,
            action: "accept", actionStatus: "preparation_failed", accessToken: nil, conferenceName: nil,
            isActive: true, isUrgent: false, callerName: nil, callerPhone: nil, transcript: nil,
            statusCode: 503, rawStatus: "error", errorDetail: "Call preparation unavailable")
        h.reply(failure); _ = await first.value
        _ = await h.coordinator.pickUp(callSid: "call-A")
        XCTAssertEqual(h.posts, 1); XCTAssertEqual(h.connections, 0)
        XCTAssertTrue(h.coordinator.isActionPending(for: "call-A"))
    }
    @MainActor func testRemovedIncomingContextRejectsBeforePost() async {
        let auth = CallAuthContext(contractorId: "owner", bearerToken: "credential", generation: 1)
        let now = Date(), uuid = UUID()
        let context = IncomingCallContext(uuid: uuid, callSid: "call-A", auth: auth, deadline: now.addingTimeInterval(30),
            accessToken: "", conferenceName: "", callerName: "", callerPhone: "", isUrgent: true)
        var ownership = IncomingCallOwnership()
        XCTAssertTrue(ownership.adopt(context, currentAuth: auth, now: now)); ownership.remove(uuid)
        var posts = 0, connections = 0
        let coordinator = CallActionCoordinator(
            sendAction: { _, _, _, _, _ in posts += 1; throw URLError(.badServerResponse) },
            getStatus: { _, _, _ in XCTFail("Removed ring cannot reconcile a new action"); return nil },
            connect: { _, _, _, _ in connections += 1; return true },
            directAnswer: { auth, sid in
                switch ownership.pickupDisposition(callSid: sid, auth: auth, now: now) {
                case .unavailable: return false
                default: return nil
                }
            }, currentAuth: { auth }, currentCall: { CallLifecycleSnapshot(callSid: "call-A", revision: 1) }, onMessage: { _ in })
        let result = await coordinator.pickUp(callSid: "call-A")
        XCTAssertFalse(result); XCTAssertEqual(posts, 0); XCTAssertEqual(connections, 0)
    }
    @MainActor func testReadAccessorsHideChangedSessionWithoutMutation() async {
        let h = ActionHarness()
        let task = Task { await h.coordinator.takeMessage(callSid: "call-A") }
        await h.waitForPost(); h.reply(h.result(status: "taking_message")); _ = await task.value
        let oldStates = h.coordinator.currentStates
        h.auth = CallAuthContext(contractorId: "new-owner", bearerToken: "new-token", generation: 2)
        XCTAssertEqual(h.coordinator.state(for: "call-A"), .idle)
        XCTAssertFalse(h.coordinator.isTakingMessage(for: "call-A"))
        XCTAssertFalse(h.coordinator.isActionPending(for: "call-A"))
        XCTAssertEqual(h.coordinator.currentStates, oldStates)
    }
    func testStrictBooleanRejectsNumericOneAndZero() throws {
        let object = try JSONSerialization.jsonObject(with: Data(#"{"yes":true,"no":false,"one":1,"zero":0}"#.utf8)) as! [String: Any]
        XCTAssertEqual(CallActionResponseParser.boolean(object["yes"]), true)
        XCTAssertEqual(CallActionResponseParser.boolean(object["no"]), false)
        XCTAssertNil(CallActionResponseParser.boolean(object["one"]))
        XCTAssertNil(CallActionResponseParser.boolean(object["zero"]))
        XCTAssertNil(CallActionResponseParser.boolean("true"))
    }
    @MainActor func testKnownContactEntryPointsUseOriginalUUIDConferenceAndNoPost() async {
        let auth = CallAuthContext(contractorId: "owner", bearerToken: "credential", generation: 1)
        let uuid = UUID(), now = Date()
        let context = IncomingCallContext(uuid: uuid, callSid: "call-A", auth: auth, deadline: now.addingTimeInterval(30),
            accessToken: "original-token", conferenceName: "original-conference", callerName: "", callerPhone: "", isUrgent: false)
        var ownership = IncomingCallOwnership()
        XCTAssertTrue(ownership.adopt(context, currentAuth: auth, now: now))
        var posts = 0, commands = 0
        var connectedUUID: UUID?, token = "", conference = ""
        let coordinator = CallActionCoordinator(
            sendAction: { _, _, _, _, _ in posts += 1; throw URLError(.badServerResponse) },
            getStatus: { _, _, _ in XCTFail("Direct calls do not reconcile a fabricated operation"); return nil },
            connect: { _, _, _, _ in XCTFail("Direct call must not enter screened connector"); return false },
            directAnswer: { auth, sid in
                guard let route = ownership.preissuedContext(callSid: sid, auth: auth, now: now) else { return nil }
                commands += 1; connectedUUID = route.uuid; token = route.accessToken; conference = route.conferenceName
                return true
            }, currentAuth: { auth }, currentCall: { CallLifecycleSnapshot(callSid: "call-A", revision: 1) }, onMessage: { _ in })
        let first = await coordinator.pickUp(callSid: "call-A")
        let duplicate = await coordinator.pickUp(callSid: "call-A")
        let opposite = await coordinator.takeMessage(callSid: "call-A")
        XCTAssertTrue(first); XCTAssertTrue(duplicate); XCTAssertFalse(opposite)
        XCTAssertEqual(posts, 0); XCTAssertEqual(commands, 1); XCTAssertEqual(connectedUUID, uuid)
        XCTAssertEqual(token, "original-token"); XCTAssertEqual(conference, "original-conference")
    }
    @MainActor func testAcceptedWaitsForConnectedPresentationWithBothActionsDisabled() async {
        let h = ActionHarness()
        let task = Task { await h.coordinator.pickUp(callSid: "call-A") }
        await h.waitForPost(); h.reply(h.result())
        let accepted = await task.value
        XCTAssertTrue(accepted); XCTAssertEqual(h.connections, 1)
        XCTAssertTrue(h.coordinator.isAcceptPending(for: "call-A"))
        XCTAssertTrue(h.coordinator.isActionPending(for: "call-A"))
        let opposite = await h.coordinator.takeMessage(callSid: "call-A")
        XCTAssertFalse(opposite); XCTAssertEqual(h.posts, 1)
    }
    @MainActor func testDirectAcceptedAndFailedPresentationNeverOffersDeadActions() async {
        let auth = CallAuthContext(contractorId: "owner", bearerToken: "credential", generation: 1)
        var posts = 0
        let coordinator = CallActionCoordinator(
            sendAction: { _, _, _, _, _ in posts += 1; throw URLError(.badServerResponse) },
            getStatus: { _, _, _ in XCTFail("Terminal direct failure must not offer status reconciliation"); return nil },
            connect: { _, _, _, _ in false }, directAnswer: { _, _ in true },
            currentAuth: { auth }, currentCall: { CallLifecycleSnapshot(callSid: "call-A", revision: 1) }, onMessage: { _ in })
        let accepted = await coordinator.pickUp(callSid: "call-A")
        XCTAssertTrue(accepted); XCTAssertTrue(coordinator.isAcceptPending(for: "call-A"))
        XCTAssertTrue(coordinator.isActionPending(for: "call-A"))
        coordinator.reportConnectionFailure(callSid: "call-A", auth: auth)
        XCTAssertFalse(coordinator.isAcceptPending(for: "call-A"))
        XCTAssertTrue(coordinator.isActionPending(for: "call-A"))
        XCTAssertFalse(coordinator.canCheckStatus(for: "call-A"))
        XCTAssertFalse(coordinator.errorMessage(for: "call-A")?.contains("Check status") ?? true)
        let status = CallActionResult(callSid: "call-A", contractorId: "owner", operationId: "", action: "",
            actionStatus: "ready", accessToken: nil, conferenceName: nil, isActive: true, isUrgent: false,
            callerName: nil, callerPhone: nil, transcript: nil, statusCode: 200, rawStatus: "ok", errorDetail: nil)
        coordinator.observeStatus(status, auth: auth)
        XCTAssertTrue(coordinator.isActionPending(for: "call-A"))
        let opposite = await coordinator.takeMessage(callSid: "call-A")
        XCTAssertFalse(opposite); XCTAssertEqual(posts, 0)
    }
    @MainActor func testConnectionFailurePreservesOperationAndUsesGetOnly() async {
        let h = ActionHarness()
        let task = Task { await h.coordinator.pickUp(callSid: "call-A") }
        await h.waitForPost(); h.reply(h.result())
        let accepted = await task.value
        XCTAssertTrue(accepted); XCTAssertEqual(h.connections, 1)
        h.coordinator.reportConnectionFailure(callSid: "call-A", auth: h.auth)
        XCTAssertNotNil(h.coordinator.errorMessage(for: "call-A"))
        XCTAssertTrue(h.coordinator.canCheckStatus(for: "call-A"))
        let opposite = await h.coordinator.takeMessage(callSid: "call-A")
        XCTAssertFalse(opposite)
        h.getResult = h.result()
        let recovered = await h.coordinator.checkStatus(callSid: "call-A")
        XCTAssertTrue(recovered); XCTAssertEqual(h.posts, 1); XCTAssertEqual(h.gets, 1); XCTAssertEqual(h.connections, 2)
    }
    @MainActor func testOldConnectionFailureCannotChangeNewCall() async {
        let h = ActionHarness()
        let task = Task { await h.coordinator.pickUp(callSid: "call-A") }
        await h.waitForPost(); h.reply(h.result()); _ = await task.value
        h.scope = CallLifecycleSnapshot(callSid: "call-B", revision: 2)
        h.coordinator.reportConnectionFailure(callSid: "call-A", auth: h.auth)
        XCTAssertNil(h.coordinator.errorMessage(for: "call-A"))
        XCTAssertNil(h.coordinator.errorMessage(for: "call-B"))
    }
    func testDismissalLeaseNeverClearsNewCallOrRoundTrip() {
        let auth = CallAuthContext(contractorId: "owner", bearerToken: "credential", generation: 1)
        let scope = CallLifecycleSnapshot(callSid: "call-A", revision: 1)
        let lease = CallPresentationLease(auth: auth, scope: scope)
        XCTAssertTrue(lease.mayClear(auth: auth, scope: scope))
        XCTAssertFalse(lease.mayClear(auth: auth, scope: CallLifecycleSnapshot(callSid: "call-B", revision: 2)))
        XCTAssertFalse(lease.mayClear(auth: auth, scope: CallLifecycleSnapshot(callSid: "call-A", revision: 3)))
        XCTAssertFalse(lease.mayClear(auth: CallAuthContext(contractorId: "owner", bearerToken: "credential", generation: 3), scope: scope))
    }
    func testPreferenceSaveFenceRejectsStaleLoadAndOldCompletion() {
        var fence = PreferenceWriteFence()
        let readRevision = fence.revision
        let save = fence.beginSave()!
        XCTAssertFalse(fence.permitsLoad(readRevision)); XCTAssertNil(fence.beginSave())
        XCTAssertTrue(fence.finish(save)); XCTAssertFalse(fence.permitsLoad(readRevision))
        let next = fence.beginSave()!
        XCTAssertFalse(fence.finish(save)); XCTAssertEqual(fence.pending, next)
        XCTAssertTrue(fence.finish(next)); XCTAssertTrue(fence.permitsLoad(fence.revision))
    }
    func testParserNeverSynthesizesDeclineOrPartialV2OrGETCredentials() throws {
        func parse(_ json: String, action: String = "accept", get: Bool = false) -> CallActionResult {
            CallActionResponseParser.parse(data: Data(json.utf8), response: HTTPURLResponse(url: URL(string: "https://fixture.invalid")!, statusCode: 200, httpVersion: nil, headerFields: nil)!, requestedCallSid: "call-A", requestedContractorId: "owner", requestedOperationId: "operation", requestedAction: action, isGet: get)
        }
        let legacy = #"{"status":"ok","access_token":"token","conference_name":"conference"}"#
        XCTAssertTrue(parse(legacy).isAccepted)
        XCTAssertFalse(parse(legacy, get: true).isAccepted)
        XCTAssertFalse(parse(#"{"status":"ok"}"#, action: "decline").isTakingMessage)
        XCTAssertFalse(parse(#"{"status":"ok","call_sid":"call-A","access_token":"token","conference_name":"conference"}"#).isAccepted)
        XCTAssertFalse(parse(#"{"access_token":"token","conference_name":"conference"}"#).isAccepted)
        XCTAssertFalse(parse("<html>bad response</html>", action: "decline").isTakingMessage)
    }
}
