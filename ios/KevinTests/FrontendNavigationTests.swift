import XCTest
@testable import Kevin

@MainActor
final class FrontendNavigationTests: XCTestCase {

    private func makeAuth(contractorId: String = "c-123", token: String = "token-abc", generation: Int = 1) -> CallAuthContext {
        CallAuthContext(contractorId: contractorId, bearerToken: token, generation: generation)
    }

    private func makeScope(callSid: String = "CA12345", revision: Int = 1) -> CallLifecycleSnapshot {
        CallLifecycleSnapshot(callSid: callSid, revision: revision)
    }

    private func makeRecord(id: String, name: String = "Caller", phone: String = "+15551234567") -> CallRecord {
        CallRecord(
            id: id,
            callerPhone: phone,
            callerName: name,
            timestamp: Date(),
            trustScore: 80,
            outcome: "screened",
            transcript: "Kevin: Hello\nCaller: Hi\nCaller: Call me",
            voicemailURL: nil,
            callbackNumber: nil,
            readOnServer: false
        )
    }

    // MARK: - Initial State

    func testInitialState() {
        let auth = makeAuth()
        let nav = FrontendNavigation(authProvider: { auth })

        XCTAssertEqual(nav.selectedTab, .calls)
        XCTAssertFalse(nav.isAccountPresented)
        XCTAssertNil(nav.presentedDetailLease)
        XCTAssertFalse(nav.shouldScrollToGoogleCalendar)
        XCTAssertNil(nav.pendingCallPresentationLease)
    }

    // MARK: - Legacy Tab Commands

    func testHandleLegacyTabCommandRecents() {
        let nav = FrontendNavigation()
        nav.selectedTab = .kevin

        nav.handleLegacyTabCommand(.recents)
        XCTAssertEqual(nav.selectedTab, .calls)
    }

    func testHandleLegacyTabCommandLive() {
        let nav = FrontendNavigation()
        nav.selectedTab = .kevin

        nav.handleLegacyTabCommand(.live)
        XCTAssertEqual(nav.selectedTab, .calls)
    }

    func testHandleLegacyTabCommandSettings() {
        let nav = FrontendNavigation()
        nav.selectedTab = .calls
        nav.shouldScrollToGoogleCalendar = false

        nav.handleLegacyTabCommand(.settings)
        XCTAssertEqual(nav.selectedTab, .kevin)
        XCTAssertTrue(nav.shouldScrollToGoogleCalendar)
    }

    // MARK: - Account Sheet Navigation

    func testOpenAndCloseAccount() {
        let nav = FrontendNavigation()
        XCTAssertFalse(nav.isAccountPresented)

        nav.openAccount()
        XCTAssertTrue(nav.isAccountPresented)

        nav.closeAccount()
        XCTAssertFalse(nav.isAccountPresented)
    }

    func testRequestReturnToCallFromAccountCapturesLease() {
        let auth = makeAuth(contractorId: "c-100", generation: 2)
        let scope = makeScope(callSid: "CA_LIVE_99", revision: 3)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })

        nav.openAccount()
        XCTAssertTrue(nav.isAccountPresented)

        let lease = nav.requestReturnToCallFromAccount()
        XCTAssertNotNil(lease)
        XCTAssertEqual(lease?.auth, auth)
        XCTAssertEqual(lease?.scope, scope)
        XCTAssertEqual(nav.pendingCallPresentationLease, lease)
        XCTAssertFalse(nav.isAccountPresented)
    }

    func testRequestReturnToCallFromAccountReturnsNilWhenNoActiveCall() {
        let auth = makeAuth()
        let scope = makeScope(callSid: "", revision: 1)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })

        nav.openAccount()
        let lease = nav.requestReturnToCallFromAccount()
        XCTAssertNil(lease)
        XCTAssertNil(nav.pendingCallPresentationLease)
    }

    func testRequestReturnToCallFromAccountReturnsNilWhenAuthInvalid() {
        let auth = CallAuthContext(contractorId: "", bearerToken: "", generation: 1)
        let scope = makeScope(callSid: "CA_LIVE_99")
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })

        let lease = nav.requestReturnToCallFromAccount()
        XCTAssertNil(lease)
    }

    // MARK: - Historical Detail Presentation

    func testOpenAndDismissHistoricalDetail() {
        let auth = makeAuth()
        let nav = FrontendNavigation(authProvider: { auth })
        let record = makeRecord(id: "CA_HIST_1")

        nav.openHistoricalDetail(call: record)
        XCTAssertNotNil(nav.presentedDetailLease)
        XCTAssertEqual(nav.presentedDetailLease?.callId, "CA_HIST_1")
        XCTAssertEqual(nav.presentedDetailLease?.auth, auth)

        nav.dismissHistoricalDetail()
        XCTAssertNil(nav.presentedDetailLease)
    }

    func testOpenHistoricalDetailRejectsMismatchedExpectedAuth() {
        let currentAuth = makeAuth(contractorId: "c-current", generation: 1)
        let staleAuth = makeAuth(contractorId: "c-stale", generation: 1)
        let nav = FrontendNavigation(authProvider: { currentAuth })
        let record = makeRecord(id: "CA_HIST_2")

        nav.openHistoricalDetail(call: record, expectedAuth: staleAuth)
        XCTAssertNil(nav.presentedDetailLease)
    }

    // MARK: - Target Resolution (Notification Deep Link)

    func testResolveNotificationTargetFindsCallFromFullSnapshot() {
        let auth = makeAuth()
        let nav = FrontendNavigation(authProvider: { auth })
        let records = [
            makeRecord(id: "CA_01"),
            makeRecord(id: "CA_02"),
            makeRecord(id: "CA_03")
        ]

        let match = nav.resolveNotificationTarget(callSid: "CA_02", allCalls: records)
        XCTAssertNotNil(match)
        XCTAssertEqual(match?.id, "CA_02")
        XCTAssertEqual(nav.presentedDetailLease?.callId, "CA_02")
        XCTAssertEqual(nav.selectedTab, .calls)
    }

    func testResolveNotificationTargetReturnsNilWhenCallNotFound() {
        let auth = makeAuth()
        let nav = FrontendNavigation(authProvider: { auth })
        let records = [makeRecord(id: "CA_01")]

        let match = nav.resolveNotificationTarget(callSid: "CA_MISSING", allCalls: records)
        XCTAssertNil(match)
        XCTAssertNil(nav.presentedDetailLease)
    }

    func testResolveNotificationTargetReturnsNilWhenAuthInvalid() {
        let invalidAuth = CallAuthContext(contractorId: "", bearerToken: "", generation: 1)
        let nav = FrontendNavigation(authProvider: { invalidAuth })
        let records = [makeRecord(id: "CA_01")]

        let match = nav.resolveNotificationTarget(callSid: "CA_01", allCalls: records)
        XCTAssertNil(match)
        XCTAssertNil(nav.presentedDetailLease)
    }

    // MARK: - Auth Lifecycle Invalidation

    func testHandleAuthChangeInvalidatesStalePresentedDetail() {
        var currentAuth = makeAuth(contractorId: "c-1", generation: 1)
        let nav = FrontendNavigation(authProvider: { currentAuth })
        let record = makeRecord(id: "CA_10")

        nav.openHistoricalDetail(call: record)
        XCTAssertNotNil(nav.presentedDetailLease)

        // Advance auth generation (e.g. contractor rotation or logout)
        currentAuth = makeAuth(contractorId: "c-1", generation: 2)
        nav.handleAuthChange()

        XCTAssertNil(nav.presentedDetailLease)
    }

    func testHandleAuthChangeResetsStateOnInvalidAuth() {
        var currentAuth = makeAuth(contractorId: "c-1", generation: 1)
        let nav = FrontendNavigation(authProvider: { currentAuth })
        nav.selectedTab = .kevin
        nav.openAccount()
        nav.shouldScrollToGoogleCalendar = true

        // Logout
        currentAuth = CallAuthContext(contractorId: "", bearerToken: "", generation: 2)
        nav.handleAuthChange()

        XCTAssertEqual(nav.selectedTab, .calls)
        XCTAssertFalse(nav.isAccountPresented)
        XCTAssertFalse(nav.shouldScrollToGoogleCalendar)
        XCTAssertNil(nav.pendingCallPresentationLease)
    }
}
