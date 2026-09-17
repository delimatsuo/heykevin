import XCTest
@testable import Kevin

@MainActor
final class AppStateOwnershipTests: XCTestCase {

    private func makeAuth(contractorId: String = "c-123", token: String = "token-abc", generation: Int = 1) -> CallAuthContext {
        CallAuthContext(contractorId: contractorId, bearerToken: token, generation: generation)
    }

    // MARK: - 1. Origin Auth Adoption and Owned Lease

    func testAdoptCallStoresOriginAuthAndExposesOwnedLease() {
        let authA = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        var currentAuth = authA
        let appState = AppState(authProvider: { currentAuth }, inMemory: true)

        XCTAssertFalse(appState.hasActiveCall)
        XCTAssertNil(appState.ownedActiveCallLease)
        XCTAssertEqual(appState.callLifecycleSnapshot.callSid, "")

        appState.setActiveCall(callSid: "CA_A1", callerPhone: "+15551111", callerName: "Alice", authContext: authA)
        appState.updateActiveCallTranscript(text: "Kevin: Hi\nCaller: Hello", authContext: authA, callSid: "CA_A1")

        XCTAssertTrue(appState.hasActiveCall)
        XCTAssertNotNil(appState.ownedActiveCallLease)
        XCTAssertEqual(appState.ownedActiveCallLease?.auth, authA)
        XCTAssertEqual(appState.ownedActiveCallLease?.scope.callSid, "CA_A1")
        XCTAssertEqual(appState.callLifecycleSnapshot.callSid, "CA_A1")
        XCTAssertEqual(appState.transcriptLines.count, 2)
        XCTAssertEqual(appState.activeCallerName, "Alice")
        XCTAssertEqual(appState.activeCallerPhone, "+15551111")
    }

    // MARK: - 2. Immediate Invalidation on Auth Switch to B and Sanitized Scope

    func testSwitchToBImmediatelyInvalidatesOwnedLeaseAndSanitizesScope() {
        let authA = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authB = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        var currentAuth = authA
        let appState = AppState(authProvider: { currentAuth }, inMemory: true)

        appState.setActiveCall(callSid: "CA_A1", callerPhone: "+15551111", callerName: "Alice", authContext: authA)
        appState.updateActiveCallTranscript(text: "Transcript A", authContext: authA, callSid: "CA_A1")
        XCTAssertTrue(appState.hasActiveCall)

        // Switch active auth to B before applyAuthChange is invoked
        currentAuth = authB

        // Must IMMEDIATELY report no owned active call and sanitized scope
        XCTAssertFalse(appState.hasActiveCall)
        XCTAssertNil(appState.ownedActiveCallLease)
        XCTAssertEqual(appState.callLifecycleSnapshot.callSid, "", "Scope must be sanitized to empty SID when unowned")
        XCTAssertEqual(appState.callLifecycleSnapshot.revision, appState.callLifecycleRevision)

        // applyAuthChange clears the stale metadata
        appState.applyAuthChange()
        XCTAssertEqual(appState.activeCallSid, "")
        XCTAssertTrue(appState.transcriptLines.isEmpty)
        XCTAssertNil(appState.activeCallAuth)
    }

    // MARK: - 3. Token Rotation, Logout, and ABA Generation Invalidation

    func testTokenRotationLogoutABA() {
        var currentAuth = makeAuth(contractorId: "c-A", token: "tok-1", generation: 1)
        let appState = AppState(authProvider: { currentAuth }, inMemory: true)

        appState.setActiveCall(callSid: "CA_A1", callerPhone: "+15551111", callerName: "Alice", authContext: currentAuth)
        XCTAssertTrue(appState.hasActiveCall)

        // Token rotation: same contractorId, new token, new generation
        currentAuth = makeAuth(contractorId: "c-A", token: "tok-2", generation: 2)
        XCTAssertFalse(appState.hasActiveCall, "Token rotation must invalidate active call from previous generation")
        XCTAssertNil(appState.ownedActiveCallLease)
        XCTAssertEqual(appState.callLifecycleSnapshot.callSid, "")

        // Logout: invalid auth
        currentAuth = makeAuth(contractorId: "", token: "", generation: 3)
        XCTAssertFalse(appState.hasActiveCall)
        XCTAssertNil(appState.ownedActiveCallLease)

        // Re-login as A with new generation (ABA)
        currentAuth = makeAuth(contractorId: "c-A", token: "tok-1", generation: 4)
        XCTAssertFalse(appState.hasActiveCall, "Old active call from generation 1 cannot be owned in generation 4")
        XCTAssertNil(appState.ownedActiveCallLease)
    }

    // MARK: - 4. Old A Adoption Cannot Overwrite B

    func testOldAAdoptionCannotOverwriteB() {
        let authA = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authB = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        var currentAuth = authB
        let appState = AppState(authProvider: { currentAuth }, inMemory: true)

        // Attempt adoption of stale call A under auth B
        appState.setActiveCall(callSid: "CA_STALE_A", callerPhone: "+15550000", callerName: "Old", authContext: authA)
        XCTAssertEqual(appState.activeCallSid, "")
        XCTAssertFalse(appState.hasActiveCall)

        // Adopt valid call B
        appState.setActiveCall(callSid: "CA_VALID_B", callerPhone: "+15559999", callerName: "Bob", authContext: authB)
        XCTAssertEqual(appState.activeCallSid, "CA_VALID_B")
        XCTAssertTrue(appState.hasActiveCall)

        // Stale adoption attempt for old call A arriving late
        appState.setActiveCall(callSid: "CA_STALE_A", callerPhone: "+15550000", callerName: "Old", authContext: authA)
        XCTAssertEqual(appState.activeCallSid, "CA_VALID_B", "Stale call A must not overwrite valid call B")
        XCTAssertEqual(appState.ownedActiveCallLease?.scope.callSid, "CA_VALID_B")
    }

    // MARK: - 5. New SID Clears Old Transcript, Ignored, and Start Time

    func testNewSIDClearsOldTranscriptIgnoredAndStartTime() {
        let authA = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let appState = AppState(authProvider: { authA }, inMemory: true)

        appState.setActiveCall(callSid: "CA_1", callerPhone: "+15551111", callerName: "Call 1", authContext: authA)
        appState.updateActiveCallTranscript(text: "Line 1\nLine 2", authContext: authA, callSid: "CA_1")
        appState.callIgnored = true
        let firstStartTime = appState.callStartTime

        XCTAssertEqual(appState.transcriptLines.count, 2)
        XCTAssertTrue(appState.callIgnored)

        // Small delay to ensure timestamp difference
        Thread.sleep(forTimeInterval: 0.01)

        // Adopt new call SID 2 under same auth
        appState.setActiveCall(callSid: "CA_2", callerPhone: "+15552222", callerName: "Call 2", authContext: authA)

        XCTAssertEqual(appState.activeCallSid, "CA_2")
        XCTAssertTrue(appState.transcriptLines.isEmpty, "New call SID must clear previous transcript lines")
        XCTAssertFalse(appState.callIgnored, "New call SID must reset callIgnored to false")
        XCTAssertNotEqual(appState.callStartTime, firstStartTime, "New call SID must reset callStartTime")
    }

    // MARK: - 6. Queued Old Auth Event Cannot Clear Valid New B Call

    func testQueuedOldAuthEventCannotClearValidNewBCall() {
        let authB = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        let appState = AppState(authProvider: { authB }, inMemory: true)

        // Adopt new B call
        appState.setActiveCall(callSid: "CA_B_VALID", callerPhone: "+15553333", callerName: "Barb", authContext: authB)
        appState.updateActiveCallTranscript(text: "Barb's conversation", authContext: authB, callSid: "CA_B_VALID")

        XCTAssertTrue(appState.hasActiveCall)
        XCTAssertEqual(appState.transcriptLines.count, 1)

        // A queued auth change event from the earlier transition arrives
        appState.applyAuthChange()

        // Valid call B must be preserved because activeCallAuth matches currentAuth
        XCTAssertTrue(appState.hasActiveCall)
        XCTAssertEqual(appState.activeCallSid, "CA_B_VALID")
        XCTAssertEqual(appState.ownedActiveCallLease?.scope.callSid, "CA_B_VALID")
        XCTAssertEqual(appState.transcriptLines.count, 1)
    }

    // MARK: - 7. updateActiveCallTranscript Validation

    func testUpdateActiveCallTranscriptValidation() {
        let authA = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authB = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        let appState = AppState(authProvider: { authA }, inMemory: true)

        appState.setActiveCall(callSid: "CA_1", callerPhone: "+15551111", callerName: "Alice", authContext: authA)

        // Mismatched auth context
        appState.updateActiveCallTranscript(text: "B transcript", authContext: authB, callSid: "CA_1")
        XCTAssertTrue(appState.transcriptLines.isEmpty)

        // Mismatched callSid
        appState.updateActiveCallTranscript(text: "Wrong SID transcript", authContext: authA, callSid: "CA_OTHER")
        XCTAssertTrue(appState.transcriptLines.isEmpty)

        // Valid update with string
        appState.updateActiveCallTranscript(text: "Line 1\nLine 2", authContext: authA, callSid: "CA_1")
        XCTAssertEqual(appState.transcriptLines.count, 2)

        // Valid update with lease
        let lease = appState.ownedActiveCallLease!
        appState.updateActiveCallTranscript(text: "Line 1\nLine 2\nLine 3", lease: lease)
        XCTAssertEqual(appState.transcriptLines.count, 3)
    }

    // MARK: - 8. In-Memory Mode Suppresses Persistence

    func testAppStateInMemorySuppressesPersistence() {
        let auth = makeAuth()
        let appState = AppState(authProvider: { auth }, inMemory: true)

        XCTAssertTrue(appState.inMemory)
        appState.isOnboarded = true
        appState.contractorId = "test-contractor"
        appState.subscriptionStatus = "active"
        appState.readCallIds = ["call-1", "call-2"]

        XCTAssertEqual(appState.contractorId, "test-contractor")
        XCTAssertEqual(appState.readCallIds.count, 2)
    }

    // MARK: - 9. activeCallReason Lifecycle, Trimming, and Resets

    func testActiveCallReasonLifecycleAndGuards() {
        let authA = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authB = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        var currentAuth = authA
        let appState = AppState(authProvider: { currentAuth }, inMemory: true)

        appState.setActiveCall(callSid: "CA_REASON_1", callerPhone: "+15551111", callerName: "Alice", authContext: authA)
        XCTAssertEqual(appState.activeCallReason, "")

        // 1. Valid update with whitespace trimming
        appState.updateActiveCallReason(reason: "  Water heater leaking  \n", authContext: authA, callSid: "CA_REASON_1")
        XCTAssertEqual(appState.activeCallReason, "Water heater leaking")

        // 2. 160-character capping
        let longReason = String(repeating: "A", count: 200)
        appState.updateActiveCallReason(reason: longReason, authContext: authA, callSid: "CA_REASON_1")
        XCTAssertEqual(appState.activeCallReason.count, 160)
        XCTAssertEqual(appState.activeCallReason, String(repeating: "A", count: 160))

        // 3. Mismatched auth context rejected
        appState.updateActiveCallReason(reason: "New reason from auth B", authContext: authB, callSid: "CA_REASON_1")
        XCTAssertEqual(appState.activeCallReason, String(repeating: "A", count: 160))

        // 4. Mismatched callSid rejected
        appState.updateActiveCallReason(reason: "Wrong SID reason", authContext: authA, callSid: "CA_OTHER")
        XCTAssertEqual(appState.activeCallReason, String(repeating: "A", count: 160))

        // 5. Update via lease
        let lease = appState.ownedActiveCallLease!
        appState.updateActiveCallReason(reason: "Lease updated reason", lease: lease)
        XCTAssertEqual(appState.activeCallReason, "Lease updated reason")

        // 6. Reset on new call adoption
        appState.setActiveCall(callSid: "CA_REASON_2", callerPhone: "+15552222", callerName: "Bob", authContext: authA)
        XCTAssertEqual(appState.activeCallReason, "", "Adopting a new call must reset activeCallReason")

        // 7. Reset on clearActiveCall
        appState.updateActiveCallReason(reason: "Some reason", authContext: authA, callSid: "CA_REASON_2")
        XCTAssertEqual(appState.activeCallReason, "Some reason")
        appState.clearActiveCall()
        XCTAssertEqual(appState.activeCallReason, "", "Clearing active call must reset activeCallReason")

        // 8. Reset on applyAuthChange
        appState.setActiveCall(callSid: "CA_REASON_3", callerPhone: "+15553333", callerName: "Charlie", authContext: authA)
        appState.updateActiveCallReason(reason: "Charlie's reason", authContext: authA, callSid: "CA_REASON_3")
        currentAuth = authB
        appState.applyAuthChange()
        XCTAssertEqual(appState.activeCallReason, "", "Auth change must reset activeCallReason")
    }

    // MARK: - 10. CapturedScreeningTranscript Snapshot Immutability and Lease Isolation

    func testCapturedScreeningTranscriptSnapshotAndIsolation() {
        let authA = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let authB = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        let appState = AppState(authProvider: { authA }, inMemory: true)

        appState.setActiveCall(callSid: "CA_SNAP_1", callerPhone: "+15551111", callerName: "Alice", authContext: authA)
        appState.updateActiveCallTranscript(text: "Kevin: Hi\nCaller: Need help", authContext: authA, callSid: "CA_SNAP_1")
        appState.updateActiveCallReason(reason: "Pipe bursting", authContext: authA, callSid: "CA_SNAP_1")

        guard let lease1 = appState.ownedActiveCallLease else {
            XCTFail("Expected valid active call lease")
            return
        }

        // 1. Capture screening transcript
        appState.captureScreeningTranscript(for: lease1)

        let snapshot = appState.screeningTranscript(for: lease1)
        XCTAssertNotNil(snapshot)
        XCTAssertEqual(snapshot?.lines, ["Kevin: Hi", "Caller: Need help"])
        XCTAssertEqual(snapshot?.reason, "Pipe bursting")
        XCTAssertEqual(snapshot?.lease, lease1)

        // 2. Immutability: subsequent mutations to activeCall transcript do not affect the captured snapshot
        appState.updateActiveCallTranscript(text: "Kevin: Hi\nCaller: Need help\nKevin: On the way", authContext: authA, callSid: "CA_SNAP_1")
        appState.updateActiveCallReason(reason: "Resolved", authContext: authA, callSid: "CA_SNAP_1")

        let snapshotAfterMutation = appState.screeningTranscript(for: lease1)
        XCTAssertEqual(snapshotAfterMutation?.lines, ["Kevin: Hi", "Caller: Need help"], "Captured transcript must be an immutable snapshot")
        XCTAssertEqual(snapshotAfterMutation?.reason, "Pipe bursting")

        // 3. Stale / mismatched lease returns nil
        let staleLease = CallPresentationLease(auth: authB, scope: lease1.scope)
        XCTAssertNil(appState.screeningTranscript(for: staleLease))

        let wrongSidLease = CallPresentationLease(auth: authA, scope: CallLifecycleSnapshot(callSid: "CA_WRONG", revision: 1))
        XCTAssertNil(appState.screeningTranscript(for: wrongSidLease))

        // 4. clearScreeningTranscript for matching lease removes snapshot
        appState.clearScreeningTranscript(for: lease1)
        XCTAssertNil(appState.screeningTranscript(for: lease1))

        // 5. clearActiveCall cleans up captured transcript
        appState.captureScreeningTranscript(for: lease1)
        XCTAssertNotNil(appState.screeningTranscript(for: lease1))
        appState.clearActiveCall()
        XCTAssertNil(appState.screeningTranscript(for: lease1))
    }
}
