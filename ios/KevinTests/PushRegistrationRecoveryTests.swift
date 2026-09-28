import XCTest
import UserNotifications
@testable import Kevin

@MainActor
final class PushRegistrationRecoveryTests: XCTestCase {
    @MainActor final class Harness {
        var auth = CallAuthContext(contractorId: "owner", bearerToken: "token", generation: 1)
        var state: AccountSessionState = .ready
        var status: UNAuthorizationStatus = .authorized
        var permissionReads = 0
        var prompts = 0
        var apnsRequests = 0
        var submitted: [PushRegistrationSnapshot] = []
        var acknowledgements: [Bool] = []
        var callbackTokens = ("", "")
        var success = true
        lazy var coordinator = PushRegistrationCoordinator(
            permissionStatusProvider: { [unowned self] in permissionReads += 1; return status },
            requestAuthorizationHandler: { [unowned self] in prompts += 1; return true },
            remoteNotificationRegisterHandler: { [unowned self] in apnsRequests += 1 },
            submitRegistrationHandler: { [unowned self] push, voip, auth in
                submitted.append(PushRegistrationSnapshot(auth: auth, pushToken: push, voipToken: voip)); return success
            }, currentAuthProvider: { [unowned self] in auth }, sessionStateProvider: { [unowned self] in state },
            tokenUpdateEffect: { [unowned self] in callbackTokens = ($0, $1) },
            registrationSuccessEffect: { [unowned self] in acknowledgements.append($0) })
    }

    func testTokensBeforeSignInRegisterImmediatelyWhenAccountIsReady() async {
        let h = Harness(); h.state = .needsRecovery
        await h.coordinator.handlePushToken("push"); await h.coordinator.handleVoIPToken("voip")
        XCTAssertTrue(h.submitted.isEmpty)
        h.state = .ready
        await h.coordinator.handleAccountReady()
        XCTAssertEqual(h.submitted.count, 1)
        XCTAssertEqual(h.submitted[0].pushToken, "push"); XCTAssertEqual(h.submitted[0].voipToken, "voip")
        XCTAssertEqual(h.apnsRequests, 1)
    }

    func testBothCallbackOrdersPreserveCurrentTokens() async {
        for pushFirst in [true, false] {
            let h = Harness()
            if pushFirst { await h.coordinator.handlePushToken("push"); await h.coordinator.handleVoIPToken("voip") }
            else { await h.coordinator.handleVoIPToken("voip"); await h.coordinator.handlePushToken("push") }
            XCTAssertEqual(h.submitted.count, 2)
            XCTAssertEqual(h.submitted.last?.pushToken, "push"); XCTAssertEqual(h.submitted.last?.voipToken, "voip")
        }
    }

    func testPermissionReadsIndependentOfAuthAndRequestsRespectPermission() async {
        let h = Harness(); h.state = .needsRecovery
        await h.coordinator.handleSceneActive()
        XCTAssertEqual(h.permissionReads, 1); XCTAssertEqual(h.apnsRequests, 0)
        h.state = .ready
        for status in [UNAuthorizationStatus.authorized, .provisional, .denied, .notDetermined] {
            h.status = status; await h.coordinator.handleSceneActive()
        }
        XCTAssertEqual(h.prompts, 1); XCTAssertEqual(h.apnsRequests, 3)
    }

    func testFailedSubmissionStopsAndNextEventRetries() async {
        var calls = 0
        let h = Harness()
        let coordinator = PushRegistrationCoordinator(permissionStatusProvider: { .denied },
            remoteNotificationRegisterHandler: {}, submitRegistrationHandler: { _, _, _ in calls += 1; return calls > 1 },
            currentAuthProvider: { h.auth }, sessionStateProvider: { .ready }, tokenUpdateEffect: { _, _ in },
            registrationSuccessEffect: { h.acknowledgements.append($0) })
        await coordinator.handlePushToken("push")
        XCTAssertEqual(calls, 1, "A failed request must not immediately retry in a loop")
        XCTAssertFalse(h.acknowledgements.contains(true))
        await coordinator.handleSceneActive()
        XCTAssertEqual(calls, 2); XCTAssertEqual(h.acknowledgements.last, true)
    }

    func testLateResponseCannotAcknowledgeOldTokenOrReplaceCallbackCache() async {
        let entered = expectation(description: "old token submitted")
        var continuation: CheckedContinuation<Bool, Never>?
        let h = Harness()
        let coordinator = PushRegistrationCoordinator(submitRegistrationHandler: { push, voip, auth in
            h.submitted.append(PushRegistrationSnapshot(auth: auth, pushToken: push, voipToken: voip))
            if h.submitted.count == 1 { return await withCheckedContinuation { continuation = $0; entered.fulfill() } }
            return true
        }, currentAuthProvider: { h.auth }, sessionStateProvider: { h.state },
            tokenUpdateEffect: { h.callbackTokens = ($0, $1) }, registrationSuccessEffect: { h.acknowledgements.append($0) })
        let first = Task { await coordinator.handlePushToken("old") }
        await fulfillment(of: [entered], timeout: 2)
        await coordinator.handlePushToken("new")
        XCTAssertEqual(h.submitted.count, 1, "Submissions must be serial")
        continuation?.resume(returning: true); await first.value
        XCTAssertEqual(h.submitted.map(\.pushToken), ["old", "new"])
        XCTAssertEqual(h.callbackTokens.0, "new")
        XCTAssertEqual(h.acknowledgements.filter { $0 }.count, 1)
        XCTAssertEqual(coordinator.lastRegisteredSnapshot?.pushToken, "new")
    }

    func testLateResponseUsesFullAuthAndNewSessionGetsLatestRegistration() async {
        let entered = expectation(description: "old session submitted")
        var continuation: CheckedContinuation<Bool, Never>?
        let h = Harness()
        let coordinator = PushRegistrationCoordinator(submitRegistrationHandler: { push, voip, auth in
            h.submitted.append(PushRegistrationSnapshot(auth: auth, pushToken: push, voipToken: voip))
            if h.submitted.count == 1 { return await withCheckedContinuation { continuation = $0; entered.fulfill() } }
            return true
        }, currentAuthProvider: { h.auth }, sessionStateProvider: { h.state }, tokenUpdateEffect: { _, _ in },
            registrationSuccessEffect: { h.acknowledgements.append($0) })
        let first = Task { await coordinator.handlePushToken("push") }
        await fulfillment(of: [entered], timeout: 2)
        h.auth = CallAuthContext(contractorId: "owner", bearerToken: "rotated", generation: 1)
        coordinator.handleAuthChange()
        continuation?.resume(returning: true); await first.value
        XCTAssertEqual(h.submitted.count, 2)
        XCTAssertEqual(h.submitted.last?.auth.bearerToken, "rotated")
        XCTAssertEqual(h.acknowledgements.filter { $0 }.count, 1)
    }

    func testRegistrationAcknowledgementRequiresSuccessHTTPAndBody() {
        let url = URL(string: "https://synthetic.test/register-device")!
        for (status, body, expected) in [(200, #"{"status":"ok"}"#, true), (200, #"{"status":"error"}"#, false),
            (200, "broken", false), (200, "{}", false), (401, #"{"status":"ok"}"#, false),
            (500, #"{"status":"ok"}"#, false), (200, #"{"status":"ok","error":"failure"}"#, false)] {
            let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil)!
            XCTAssertEqual(DeviceRegistrationResponseParser.succeeded(data: Data(body.utf8), response: response), expected)
        }
    }

    func testSuccessfulRestoreInvokesRealNotificationCoordinatorWithEarlierCallbacks() async {
        let store = RecoveryTestKeychain(); let state = AppState(inMemory: true, secureStore: store.client)
        state.isOnboarded = true; state.sessionState = .needsRecovery; state.kevinNumber = "+15005550006"
        var submissions: [PushRegistrationSnapshot] = []
        let push = PushRegistrationCoordinator(permissionStatusProvider: { .authorized },
            remoteNotificationRegisterHandler: {}, submitRegistrationHandler: { p, v, a in
                submissions.append(PushRegistrationSnapshot(auth: a, pushToken: p, voipToken: v)); return true
            }, currentAuthProvider: { state.currentAuthContext() }, sessionStateProvider: { state.sessionState },
            tokenUpdateEffect: { _, _ in }, registrationSuccessEffect: { state.isRegistered = $0 })
        await push.handleVoIPToken("voip-before-login"); await push.handlePushToken("push-before-login")
        XCTAssertTrue(submissions.isEmpty)
        let restore = AccountRestoreCoordinator(state: state,
            lookupHandler: { _, _ in .found(contractorId: "owner", apiToken: "new-token") },
            profileFetcher: { _, _ in ["contractor_id": "owner", "active": true, "subscription_status": "active",
                "subscription_tier": "personal", "twilio_number": "+15005550006"] },
            notificationReconciler: { await push.handleAccountReady() }, contactSyncHandler: { _ in })
        let outcome = await restore.restoreAccount(appleUserId: "apple", appleIdentityToken: "fresh",
            isRecoveryMode: true, retainedKevinNumber: state.kevinNumber, attemptRevision: restore.beginAttempt()!)
        XCTAssertEqual(outcome, .success(contractorId: "owner", needsProvisioning: false))
        XCTAssertEqual(submissions.count, 1)
        XCTAssertEqual(submissions[0].auth.bearerToken, "new-token")
        XCTAssertEqual(submissions[0].pushToken, "push-before-login")
        XCTAssertEqual(submissions[0].voipToken, "voip-before-login")
        XCTAssertTrue(state.isRegistered)
    }
}
