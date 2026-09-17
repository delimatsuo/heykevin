import XCTest
import UserNotifications
@testable import Kevin

@MainActor
private final class NotificationHarness {
    var auth = CallAuthContext(contractorId: "fixture-owner", bearerToken: "fixture-token", generation: 1)
    lazy var state = AppState(authProvider: { self.auth }, inMemory: true)
    var gets = 0, posts = 0, connections = 0, messages = 0
    var duringGet: (() async -> Void)?
    var overrideStatus: CallActionResult?
    lazy var coordinator = CallActionCoordinator(
        sendAction: { auth, sid, action, operation, _ in
            self.posts += 1
            return self.result(sid: sid, owner: auth.contractorId, action: action, operation: operation)
        },
        getStatus: { auth, sid, operation in
            self.gets += 1
            XCTAssertEqual(operation, "")
            let reply = self.overrideStatus ?? self.result(sid: sid, owner: auth.contractorId)
            await self.duringGet?()
            return reply
        },
        connect: { _, _, _, _ in self.connections += 1; return true },
        directAnswer: { _, _ in nil },
        currentAuth: { self.auth }, currentCall: { self.state.callLifecycleSnapshot },
        onMessage: { _ in self.messages += 1 }, navigationState: state)

    func result(sid: String = "CA_A", owner: String? = nil, action: String = "", operation: String = "") -> CallActionResult {
        CallActionResult(callSid: sid, contractorId: owner ?? auth.contractorId, operationId: operation,
            action: action, actionStatus: action == "accept" ? "accepted" : (action == "decline" ? "taking_message" : "ready"),
            accessToken: action == "accept" ? "fixture-access" : nil, conferenceName: action == "accept" ? "fixture-conference" : nil,
            isActive: true, isUrgent: false, callerName: "Maria", callerPhone: "+16505550187",
            transcript: "Caller: My water heater is leaking.", statusCode: 200, rawStatus: "ok", errorDetail: nil,
            screeningReason: "Water heater repair")
    }
    func adopt(_ sid: String = "CA_A") {
        state.setActiveCall(callSid: sid, callerPhone: "+16505550187", callerName: "Maria", authContext: auth)
    }
    func handle(_ action: String, category: String = "SCREENING_CALL", sid: String = "CA_A", owner: String? = nil, capturedAuth: CallAuthContext? = nil) async {
        await AppDelegate.handleNotificationResponse(categoryIdentifier: category, actionIdentifier: action,
            userInfo: ["call_sid": sid, "contractor_id": owner ?? auth.contractorId], authContext: capturedAuth ?? auth,
            currentAuth: { self.auth }, coordinator: coordinator)
    }
}

@MainActor
final class NotificationEntryTests: XCTestCase {
    func testNotificationStillOpensWhenObserverDiscoversTheSameCallDuringValidation() async {
        let h = NotificationHarness()
        h.state.selectedTab = .recents
        h.duringGet = { h.adopt() }
        let navigated = await h.coordinator.validateAndNavigate(callSid: "CA_A", authContext: h.auth)
        XCTAssertTrue(navigated, "Discovering the exact requested call must not turn a live notification into history fallback")
        XCTAssertEqual(h.state.selectedTab, .live)
        XCTAssertTrue(h.state.notificationCallSid.isEmpty)
        XCTAssertEqual(h.state.activeCallReason, "Water heater repair")
        XCTAssertEqual(h.state.transcriptLines.map(\.text), ["Caller: My water heater is leaking."])
    }

    func testReplacedOrRecreatedCallCannotBeAdoptedByOldNotification() async {
        for transition in 0..<4 {
            let h = NotificationHarness()
            if transition == 2 { h.adopt() }
            if transition == 3 { h.adopt("CA_OTHER") }
            h.duringGet = {
                switch transition {
                case 0: h.adopt("CA_OTHER")
                case 1: h.adopt("CA_OTHER"); h.adopt()
                case 2: h.state.clearActiveCall(); h.adopt()
                default: break
                }
            }
            let accepted = await h.coordinator.validateAndNavigate(callSid: "CA_A", authContext: h.auth)
            XCTAssertFalse(accepted)
            XCTAssertNotEqual(h.state.selectedTab, .live)
            XCTAssertEqual(h.posts, 0)
        }
    }

    func testAuthRoundTripDuringValidationCannotNavigateOrAct() async {
        let h = NotificationHarness()
        h.duringGet = { h.auth = CallAuthContext(contractorId: h.auth.contractorId, bearerToken: h.auth.bearerToken, generation: h.auth.generation + 2) }
        await h.handle("TAKE_MESSAGE_ACTION")
        XCTAssertEqual(h.gets, 1)
        XCTAssertEqual(h.posts, 0)
        XCTAssertFalse(h.state.hasActiveCall)
    }

    func testReadActionsNavigateWithoutChangingTheCall() async {
        for action in [UNNotificationDefaultActionIdentifier, "READ_TRANSCRIPT_ACTION"] {
            let h = NotificationHarness()
            await h.handle(action)
            XCTAssertEqual(h.gets, 1)
            XCTAssertEqual(h.posts, 0)
            XCTAssertEqual(h.connections, 0)
            XCTAssertEqual(h.messages, 0)
            XCTAssertEqual(h.state.selectedTab, .live)
            XCTAssertEqual(h.state.activeCallSid, "CA_A")
        }
    }

    func testDuplicateActionDeliveryUsesOneOperationAndConnection() async {
        for action in ["PICK_UP_ACTION", "TAKE_MESSAGE_ACTION"] {
            let h = NotificationHarness()
            await h.handle(action)
            await h.handle(action)
            XCTAssertEqual(h.posts, 1)
            XCTAssertEqual(h.connections, action == "PICK_UP_ACTION" ? 1 : 0)
            XCTAssertEqual(h.messages, action == "TAKE_MESSAGE_ACTION" ? 1 : 0)
            XCTAssertEqual(h.state.selectedTab, .live)
        }
    }

    func testInvalidNotificationEntryHasNoEffects() async {
        for scenario in 0..<7 {
            let h = NotificationHarness()
            switch scenario {
            case 0: await h.handle(UNNotificationDismissActionIdentifier)
            case 1: await h.handle("READ_TRANSCRIPT_ACTION", category: "OTHER")
            case 2: await h.handle("UNKNOWN_ACTION")
            case 3: await h.handle("PICK_UP_ACTION", owner: "other")
            case 4: await h.handle("PICK_UP_ACTION", sid: "")
            case 5:
                h.auth = CallAuthContext(contractorId: "", bearerToken: "", generation: 1)
                await h.handle("PICK_UP_ACTION")
            default:
                let captured = h.auth
                h.auth = CallAuthContext(contractorId: captured.contractorId, bearerToken: captured.bearerToken, generation: captured.generation + 2)
                await h.handle("TAKE_MESSAGE_ACTION", capturedAuth: captured)
            }
            XCTAssertEqual(h.gets, 0)
            XCTAssertEqual(h.posts, 0)
            XCTAssertFalse(h.state.hasActiveCall)
        }
    }

    func testWrongIdentityOrCredentialsInPassiveStatusCannotAct() async {
        for status in 0..<3 {
            let h = NotificationHarness()
            h.overrideStatus = status == 0 ? h.result(sid: "CA_OTHER") : (status == 1 ? h.result(owner: "other") : h.result(action: "accept"))
            await h.handle("PICK_UP_ACTION")
            XCTAssertEqual(h.posts, 0)
            XCTAssertFalse(h.state.hasActiveCall)
            XCTAssertEqual(h.state.notificationCallSid, "CA_A")
        }
    }

    func testEndedAndUnavailableStatusRemainDifferentFallbacks() async {
        for ended in [true, false] {
            let h = NotificationHarness()
            h.adopt()
            h.overrideStatus = CallActionResult(callSid: "CA_A", contractorId: h.auth.contractorId, operationId: "",
                action: "", actionStatus: ended ? "ended" : "", accessToken: nil, conferenceName: nil,
                isActive: false, isUrgent: false, callerName: nil, callerPhone: nil, transcript: nil,
                statusCode: ended ? 200 : 404, rawStatus: ended ? "ok" : "error", errorDetail: nil)
            await h.handle("PICK_UP_ACTION")
            XCTAssertEqual(h.posts, 0)
            XCTAssertEqual(h.state.hasActiveCall, !ended)
            XCTAssertEqual(h.state.notificationCallMessage.isEmpty, ended)
            if !ended { XCTAssertTrue(h.state.notificationCallMessage.contains("may still be active")) }
        }
    }

    func testSupersededNotificationCannotReplaceNewerTarget() async {
        let h = NotificationHarness()
        var resume: CheckedContinuation<Void, Never>?
        h.duringGet = { await withCheckedContinuation { resume = $0 } }
        let first = Task { await h.coordinator.validateAndNavigate(callSid: "CA_A") }
        for _ in 0..<200 where resume == nil { await Task.yield() }
        XCTAssertNotNil(resume)
        h.duringGet = nil
        let second = await h.coordinator.validateAndNavigate(callSid: "CA_B")
        resume?.resume()
        let old = await first.value
        XCTAssertTrue(second)
        XCTAssertFalse(old)
        XCTAssertEqual(h.state.activeCallSid, "CA_B")
    }

    func testCategoryPreservesPrimaryActionsAndAddsPassiveRead() {
        let category = AppDelegate.makeScreeningNotificationCategory()
        XCTAssertEqual(category.identifier, "SCREENING_CALL")
        XCTAssertEqual(category.actions.map(\.identifier), ["PICK_UP_ACTION", "TAKE_MESSAGE_ACTION", "READ_TRANSCRIPT_ACTION"])
        XCTAssertTrue(category.actions.allSatisfy { $0.options.contains(.foreground) })
    }
}
