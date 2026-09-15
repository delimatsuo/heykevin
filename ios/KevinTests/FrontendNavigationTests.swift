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
        XCTAssertNil(nav.presentedSheet)
        XCTAssertNil(nav.presentedDetailLease)
        XCTAssertFalse(nav.shouldScrollToGoogleCalendar)
        XCTAssertNil(nav.pendingCallPresentationLease)
        XCTAssertFalse(nav.isCallConnectedPendingPresentation)
        XCTAssertFalse(nav.shouldPresentInCall)
    }

    // MARK: - Legacy Tab Commands

    func testHandleLegacyTabCommandRecents() {
        let nav = FrontendNavigation()
        nav.selectedTab = .kevin

        nav.handleLegacyTabCommand(.recents)
        XCTAssertEqual(nav.selectedTab, .calls)
    }

    func testHandleLegacyTabCommandLiveOpensLiveDetailWhenOwnedLeasePresent() {
        let auth = makeAuth(contractorId: "c-1", generation: 1)
        let scope = makeScope(callSid: "CA_LIVE_1", revision: 1)
        let lease = CallPresentationLease(auth: auth, scope: scope)
        let nav = FrontendNavigation(
            authProvider: { auth },
            scopeProvider: { scope },
            ownedLeaseProvider: { lease }
        )
        nav.selectedTab = .kevin

        nav.handleLegacyTabCommand(.live)
        XCTAssertEqual(nav.selectedTab, .calls)
        if case .liveCallDetail(let presented) = nav.presentedSheet {
            XCTAssertEqual(presented.scope.callSid, "CA_LIVE_1")
            XCTAssertEqual(presented.auth, auth)
        } else {
            XCTFail("Legacy .live command must present live call detail sheet when owned active call is present")
        }
    }

    func testHandleLegacyTabCommandLiveDoesNotOpenLiveDetailWhenOwnedLeaseNil() {
        let auth = makeAuth()
        let scope = makeScope(callSid: "")
        let nav = FrontendNavigation(
            authProvider: { auth },
            scopeProvider: { scope },
            ownedLeaseProvider: { nil }
        )

        nav.handleLegacyTabCommand(.live)
        XCTAssertEqual(nav.selectedTab, .calls)
        XCTAssertNil(nav.presentedSheet)
    }

    func testHandleLegacyTabCommandSettingsRoutesToKevinAndScrollsToCalendar() {
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
        let lease = CallPresentationLease(auth: auth, scope: scope)
        let nav = FrontendNavigation(
            authProvider: { auth },
            scopeProvider: { scope },
            ownedLeaseProvider: { lease }
        )

        nav.openAccount()
        XCTAssertTrue(nav.isAccountPresented)

        let captured = nav.requestReturnToCallFromAccount()
        XCTAssertNotNil(captured)
        XCTAssertEqual(captured?.auth, auth)
        XCTAssertEqual(captured?.scope, scope)
        XCTAssertEqual(nav.pendingCallPresentationLease, lease)
        XCTAssertFalse(nav.isAccountPresented)
    }

    func testRequestReturnToCallFromAccountReturnsNilWhenNoActiveCall() {
        let auth = makeAuth()
        let scope = makeScope(callSid: "", revision: 1)
        let nav = FrontendNavigation(
            authProvider: { auth },
            scopeProvider: { scope },
            ownedLeaseProvider: { nil }
        )

        nav.openAccount()
        let lease = nav.requestReturnToCallFromAccount()
        XCTAssertNil(lease)
        XCTAssertNil(nav.pendingCallPresentationLease)
    }

    func testRequestReturnToCallFromAccountReturnsNilWhenAuthInvalid() {
        let auth = CallAuthContext(contractorId: "", bearerToken: "", generation: 1)
        let scope = makeScope(callSid: "CA_LIVE_99")
        let nav = FrontendNavigation(
            authProvider: { auth },
            scopeProvider: { scope },
            ownedLeaseProvider: { nil }
        )

        let lease = nav.requestReturnToCallFromAccount()
        XCTAssertNil(lease)
    }

    // MARK: - Live Call Presentation

    func testOpenLiveDetailWithValidLease() {
        let auth = makeAuth(contractorId: "c-1", generation: 1)
        let scope = makeScope(callSid: "CA_LIVE_01", revision: 1)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })
        let lease = CallPresentationLease(auth: auth, scope: scope)

        nav.openLive(lease: lease)
        XCTAssertEqual(nav.presentedSheet, .liveCallDetail(lease))
    }

    func testOpenLiveDetailRejectsInvalidLease() {
        let auth = makeAuth(contractorId: "c-current", generation: 1)
        let scope = makeScope(callSid: "CA_CURRENT", revision: 1)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })

        let staleLease = CallPresentationLease(auth: makeAuth(contractorId: "c-stale", generation: 1), scope: scope)
        nav.openLive(lease: staleLease)
        XCTAssertNil(nav.presentedSheet)
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
        XCTAssertNil(nav.presentedSheet)
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

    func testResolveNotificationTargetPresentsUnavailableWhenNotFound() {
        let auth = makeAuth()
        let nav = FrontendNavigation(authProvider: { auth })
        let records = [makeRecord(id: "CA_01")]

        let match = nav.resolveNotificationTarget(callSid: "CA_MISSING", allCalls: records, fallbackMessage: "Call expired")
        XCTAssertNil(match)
        XCTAssertEqual(nav.presentedSheet, .unavailableNotification(callSid: "CA_MISSING", message: "Call expired", auth: auth))
    }

    func testResolveNotificationTargetReturnsNilWhenAuthInvalid() {
        let invalidAuth = CallAuthContext(contractorId: "", bearerToken: "", generation: 1)
        let nav = FrontendNavigation(authProvider: { invalidAuth })
        let records = [makeRecord(id: "CA_01")]

        let match = nav.resolveNotificationTarget(callSid: "CA_01", allCalls: records)
        XCTAssertNil(match)
        XCTAssertNil(nav.presentedSheet)
    }

    // MARK: - Connected Call Presentation Sequencing

    func testCallConnectionImmediateWhenNoSheetsOpen() {
        let auth = makeAuth(contractorId: "c-1")
        let scope = makeScope(callSid: "CA_CONN_1")
        let lease = CallPresentationLease(auth: auth, scope: scope)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })

        XCTAssertFalse(nav.shouldPresentInCall)
        XCTAssertFalse(nav.isCallConnectedPendingPresentation)

        nav.handleCallConnectionStarted(lease: lease, hasOpenSheets: false)
        XCTAssertTrue(nav.shouldPresentInCall)
        XCTAssertFalse(nav.isCallConnectedPendingPresentation)
    }

    func testCallConnectionSequencingWaitsForAccountSheetDismissal() {
        let auth = makeAuth(contractorId: "c-1")
        let scope = makeScope(callSid: "CA_CONN_1")
        let lease = CallPresentationLease(auth: auth, scope: scope)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })

        nav.openAccount()
        XCTAssertTrue(nav.isAccountPresented)

        nav.handleCallConnectionStarted(lease: lease, hasOpenSheets: true)
        XCTAssertTrue(nav.isCallConnectedPendingPresentation)
        XCTAssertFalse(nav.shouldPresentInCall)
        XCTAssertFalse(nav.isAccountPresented)

        // Real onDismiss callback of Account sheet fires with no remaining sheets
        nav.handleSheetDismissed(hasRemainingSheets: false)
        XCTAssertFalse(nav.isCallConnectedPendingPresentation)
        XCTAssertTrue(nav.shouldPresentInCall)
    }

    func testCallConnectionSequencingWaitsForRootSheetDismissal() {
        let auth = makeAuth(contractorId: "c-1")
        let scope = makeScope(callSid: "CA_CONN_1")
        let lease = CallPresentationLease(auth: auth, scope: scope)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })
        nav.openHistoricalDetail(call: makeRecord(id: "CA_1"))
        XCTAssertNotNil(nav.presentedSheet)

        nav.handleCallConnectionStarted(lease: lease, hasOpenSheets: true)
        XCTAssertTrue(nav.isCallConnectedPendingPresentation)
        XCTAssertFalse(nav.shouldPresentInCall)
        XCTAssertNil(nav.presentedSheet)

        // Real onDismiss callback fires
        nav.handleSheetDismissed(hasRemainingSheets: false)
        XCTAssertTrue(nav.shouldPresentInCall)
    }

    func testInCallDismissedCleansUpState() {
        let auth = makeAuth()
        let scope = makeScope(callSid: "CA_1")
        let lease = CallPresentationLease(auth: auth, scope: scope)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })

        nav.handleCallConnectionStarted(lease: lease, hasOpenSheets: false)
        XCTAssertTrue(nav.shouldPresentInCall)

        nav.handleInCallDismissed()
        XCTAssertFalse(nav.shouldPresentInCall)
        XCTAssertFalse(nav.isCallConnectedPendingPresentation)
    }

    func testConnectedPendingOldLeaseRejectedAfterAuthChange() {
        var currentAuth = makeAuth(contractorId: "c-A", token: "tok-A", generation: 1)
        let scope = makeScope(callSid: "CA_1")
        let leaseA = CallPresentationLease(auth: currentAuth, scope: scope)
        let nav = FrontendNavigation(authProvider: { currentAuth }, scopeProvider: { scope })

        nav.openAccount()
        nav.handleCallConnectionStarted(lease: leaseA, hasOpenSheets: true)
        XCTAssertTrue(nav.isCallConnectedPendingPresentation)

        // Switch to Auth B
        currentAuth = makeAuth(contractorId: "c-B", token: "tok-B", generation: 2)
        nav.handleAuthChange()

        // Sheet dismissal occurs
        nav.handleSheetDismissed(hasRemainingSheets: false)

        // Old connected call lease must NOT be presented under new auth
        XCTAssertFalse(nav.shouldPresentInCall)
        XCTAssertFalse(nav.isCallConnectedPendingPresentation)
        XCTAssertNil(nav.pendingCallPresentationLease)
    }

    func testPassiveFrontendDismissalNoClearOrHangup() {
        let auth = makeAuth()
        let scope = makeScope(callSid: "CA_ACTIVE_1")
        let lease = CallPresentationLease(auth: auth, scope: scope)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })

        nav.handleCallConnectionStarted(lease: lease, hasOpenSheets: false)
        XCTAssertTrue(nav.shouldPresentInCall)

        // Passive UI dismissal only cleans up presentation flags
        nav.handleInCallDismissed()
        XCTAssertFalse(nav.shouldPresentInCall)
        XCTAssertFalse(nav.isCallConnectedPendingPresentation)
    }

    // MARK: - Auth Lifecycle Invalidation

    func testHandleAuthChangeInvalidatesStalePresentedHistoricalDetail() {
        var currentAuth = makeAuth(contractorId: "c-1", generation: 1)
        let nav = FrontendNavigation(authProvider: { currentAuth })
        let record = makeRecord(id: "CA_10")

        nav.openHistoricalDetail(call: record)
        XCTAssertNotNil(nav.presentedDetailLease)

        // Advance auth generation (e.g. contractor rotation or logout)
        currentAuth = makeAuth(contractorId: "c-1", generation: 2)
        nav.handleAuthChange()

        XCTAssertNil(nav.presentedDetailLease)
        XCTAssertNil(nav.presentedSheet)
    }

    func testHandleAuthChangeResetsStateOnAnyAuthRotation() {
        var currentAuth = makeAuth(contractorId: "c-1", generation: 1)
        let nav = FrontendNavigation(authProvider: { currentAuth })
        nav.selectedTab = .kevin
        nav.openAccount()
        nav.shouldScrollToGoogleCalendar = true

        // Rotate A -> B
        currentAuth = makeAuth(contractorId: "c-2", generation: 1)
        nav.handleAuthChange()

        XCTAssertFalse(nav.isAccountPresented)
        XCTAssertFalse(nav.shouldScrollToGoogleCalendar)
        XCTAssertNil(nav.pendingCallPresentationLease)
        XCTAssertNil(nav.presentedSheet)
        XCTAssertNil(nav.pendingNotificationTarget)
    }

    // MARK: - Repeated Tab Commands

    func testRepeatedLiveCommandAfterDismissAndKevinSelection() {
        let auth = makeAuth(contractorId: "c-1", generation: 1)
        let scope = makeScope(callSid: "CA_LIVE_REP", revision: 1)
        let lease = CallPresentationLease(auth: auth, scope: scope)
        let nav = FrontendNavigation(
            authProvider: { auth },
            scopeProvider: { scope },
            ownedLeaseProvider: { lease }
        )

        // First .live command
        nav.handleLegacyTabCommand(.live)
        XCTAssertEqual(nav.selectedTab, .calls)
        XCTAssertEqual(nav.presentedSheet, .liveCallDetail(lease))

        // User dismisses sheet and switches to Kevin
        nav.dismissSheet()
        XCTAssertNil(nav.presentedSheet)
        nav.selectedTab = .kevin
        XCTAssertEqual(nav.selectedTab, .kevin)

        // Repeated .live command must reopen live detail and switch to calls
        nav.handleLegacyTabCommand(.live)
        XCTAssertEqual(nav.selectedTab, .calls)
        XCTAssertEqual(nav.presentedSheet, .liveCallDetail(lease))
    }

    func testRepeatedSettingsCommandAfterCallsSelection() {
        let nav = FrontendNavigation()

        // First .settings command
        nav.handleLegacyTabCommand(.settings)
        XCTAssertEqual(nav.selectedTab, .kevin)
        XCTAssertTrue(nav.shouldScrollToGoogleCalendar)

        // User navigates back to Calls tab
        nav.selectedTab = .calls
        nav.shouldScrollToGoogleCalendar = false

        // Repeated .settings command must switch back to Kevin and scroll to calendar
        nav.handleLegacyTabCommand(.settings)
        XCTAssertEqual(nav.selectedTab, .kevin)
        XCTAssertTrue(nav.shouldScrollToGoogleCalendar)
    }

    // MARK: - Cold Root & Settled Notification Target Resolution

    func testColdNotificationTargetQueuedWhileLoadingSettlesToDetail() {
        let auth = makeAuth(contractorId: "c-10", generation: 1)
        let nav = FrontendNavigation(authProvider: { auth })

        // Target SID for record 35 (beyond initial 20 page)
        let targetSid = "CA_RECORD_035"
        nav.queueNotificationTarget(callSid: targetSid, fallbackMessage: "Fallback", auth: auth)
        XCTAssertNotNil(nav.pendingNotificationTarget)
        XCTAssertEqual(nav.pendingNotificationTarget?.callSid, targetSid)

        // 1. Attempt resolution while loading: must do NOTHING (no sheet, returns nil)
        let unresolved = nav.resolvePendingNotification(
            allCalls: [],
            loadedAuth: auth,
            isLoading: true,
            loadFinishedOrFailed: false
        )
        XCTAssertNil(unresolved)
        XCTAssertNil(nav.presentedSheet)
        XCTAssertNotNil(nav.pendingNotificationTarget, "Target remains pending while loading")

        // 2. Build 101 records containing target outside page 1
        var records: [CallRecord] = []
        for i in 1...101 {
            records.append(makeRecord(id: String(format: "CA_RECORD_%03d", i), name: "Caller \(i)"))
        }

        // 3. Snapshot settles: resolution resolves exact ID from full snapshot to detail
        let consumed = nav.resolvePendingNotification(
            allCalls: records,
            loadedAuth: auth,
            isLoading: false,
            loadFinishedOrFailed: true
        )
        XCTAssertNotNil(consumed)
        XCTAssertEqual(consumed?.callSid, targetSid)
        XCTAssertEqual(consumed?.auth, auth)
        XCTAssertNil(nav.pendingNotificationTarget, "Target consumed and cleared")

        if case .historicalDetail(let lease) = nav.presentedSheet {
            XCTAssertEqual(lease.callId, targetSid)
            XCTAssertEqual(lease.auth, auth)
        } else {
            XCTFail("Expected historicalDetail sheet for resolved target")
        }

        // 4. Subsequent resolution attempts do nothing
        let subsequent = nav.resolvePendingNotification(
            allCalls: records,
            loadedAuth: auth,
            isLoading: false,
            loadFinishedOrFailed: true
        )
        XCTAssertNil(subsequent)
    }

    func testQueuedTargetAthenBCompletionResolvesBOnly() {
        let auth = makeAuth(contractorId: "c-1", generation: 1)
        let nav = FrontendNavigation(authProvider: { auth })

        // Queue target A then queue target B
        nav.queueNotificationTarget(callSid: "CA_A", fallbackMessage: "Msg A", auth: auth)
        XCTAssertEqual(nav.pendingNotificationTarget?.callSid, "CA_A")

        nav.queueNotificationTarget(callSid: "CA_B", fallbackMessage: "Msg B", auth: auth)
        XCTAssertEqual(nav.pendingNotificationTarget?.callSid, "CA_B", "New target B supersedes old target A")

        let records = [makeRecord(id: "CA_A"), makeRecord(id: "CA_B")]
        let consumed = nav.resolvePendingNotification(
            allCalls: records,
            loadedAuth: auth,
            isLoading: false,
            loadFinishedOrFailed: true
        )

        XCTAssertEqual(consumed?.callSid, "CA_B")
        XCTAssertEqual(nav.presentedDetailLease?.callId, "CA_B")
    }

    func testAuthRotationRejectsAndClearsQueuedPendingTarget() {
        var currentAuth = makeAuth(contractorId: "c-A", generation: 1)
        let nav = FrontendNavigation(authProvider: { currentAuth })

        // Queue target with Auth A
        nav.queueNotificationTarget(callSid: "CA_A", auth: currentAuth)
        XCTAssertNotNil(nav.pendingNotificationTarget)

        // Rotate auth to B
        currentAuth = makeAuth(contractorId: "c-B", generation: 2)
        nav.handleAuthChange()

        XCTAssertNil(nav.pendingNotificationTarget, "Auth rotation must clear pending notification target")

        let records = [makeRecord(id: "CA_A")]
        let consumed = nav.resolvePendingNotification(
            allCalls: records,
            loadedAuth: currentAuth,
            isLoading: false,
            loadFinishedOrFailed: true
        )
        XCTAssertNil(consumed)
        XCTAssertNil(nav.presentedSheet)
    }

    func testPendingTargetDoesNotReportUnavailableUntilOwnedLoadCompletedOrFailed() {
        let auth = makeAuth(contractorId: "c-1", generation: 1)
        let nav = FrontendNavigation(authProvider: { auth })

        nav.queueNotificationTarget(callSid: "CA_MISSING", fallbackMessage: "Call expired", auth: auth)

        // While loading: no sheet
        let r1 = nav.resolvePendingNotification(allCalls: [], loadedAuth: auth, isLoading: true, loadFinishedOrFailed: false)
        XCTAssertNil(r1)
        XCTAssertNil(nav.presentedSheet)

        // Unsettled (not loading, but loadFinishedOrFailed is false): no sheet
        let r2 = nav.resolvePendingNotification(allCalls: [], loadedAuth: auth, isLoading: false, loadFinishedOrFailed: false)
        XCTAssertNil(r2)
        XCTAssertNil(nav.presentedSheet)

        // Settled completed/failed: now reports honest unavailable
        let consumed = nav.resolvePendingNotification(allCalls: [], loadedAuth: auth, isLoading: false, loadFinishedOrFailed: true)
        XCTAssertNotNil(consumed)
        XCTAssertEqual(consumed?.callSid, "CA_MISSING")
        XCTAssertEqual(nav.presentedSheet, .unavailableNotification(callSid: "CA_MISSING", message: "Call expired", auth: auth))
    }

    // MARK: - Historical Selection Action Factory

    func testMakeHistoricalSelectionActionAuthGuards() {
        var currentAuth = makeAuth(contractorId: "c-100", token: "tok-100", generation: 1)
        let nav = FrontendNavigation(authProvider: { currentAuth })
        let record = makeRecord(id: "CA_HIST_SEL_1")

        // 1. Valid action executed while matching current auth
        let validAction = nav.makeHistoricalSelectionAction(call: record, expectedAuth: currentAuth)
        validAction()

        XCTAssertNotNil(nav.presentedDetailLease)
        XCTAssertEqual(nav.presentedDetailLease?.callId, "CA_HIST_SEL_1")
        if case .historicalDetail(let lease) = nav.presentedSheet {
            XCTAssertEqual(lease.callId, "CA_HIST_SEL_1")
            XCTAssertEqual(lease.auth, currentAuth)
        } else {
            XCTFail("Expected historicalDetail sheet")
        }

        // Dismiss sheet
        nav.dismissHistoricalDetail()
        XCTAssertNil(nav.presentedSheet)
        XCTAssertNil(nav.presentedDetailLease)

        // 2. Retained action A executed after auth switches to B
        currentAuth = makeAuth(contractorId: "c-200", token: "tok-200", generation: 2)
        nav.handleAuthChange()

        validAction() // execute retained action capturing old c-100/gen-1 auth
        XCTAssertNil(nav.presentedSheet, "Retained action with old auth must not open sheet after switching to contractor B")
        XCTAssertNil(nav.presentedDetailLease)

        // 3. Retained action A executed after ABA rotation (c-100 rotated to gen-3)
        currentAuth = makeAuth(contractorId: "c-100", token: "tok-100", generation: 3)
        nav.handleAuthChange()

        validAction() // execute retained action capturing gen-1 auth
        XCTAssertNil(nav.presentedSheet, "Retained action with generation 1 must not open sheet after ABA rotation to generation 3")
        XCTAssertNil(nav.presentedDetailLease)

        // 4. Action created with invalid auth context
        let invalidAuth = CallAuthContext(contractorId: "", bearerToken: "", generation: 0)
        let invalidAction = nav.makeHistoricalSelectionAction(call: record, expectedAuth: invalidAuth)
        invalidAction()
        XCTAssertNil(nav.presentedSheet)
        XCTAssertNil(nav.presentedDetailLease)
    }

    // MARK: - Account Dismissal Integration & Sequencing Tests

    func testAccountTrueOpenLiveQueuesUntilOnDismiss() {
        let auth = makeAuth(contractorId: "c-1", generation: 1)
        let scope = makeScope(callSid: "CA_LIVE_1", revision: 1)
        let lease = CallPresentationLease(auth: auth, scope: scope)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })

        nav.openAccount()
        XCTAssertTrue(nav.isAccountPresented)
        XCTAssertFalse(nav.isAccountDismissalInProgress)

        nav.openLive(lease: lease)
        XCTAssertFalse(nav.isAccountPresented)
        XCTAssertTrue(nav.isAccountDismissalInProgress)
        XCTAssertNil(nav.presentedSheet)
        XCTAssertEqual(nav.pendingAccountDestination, .liveCallDetail(lease))

        nav.handleAccountDismissed(hasRemainingSheets: false)
        XCTAssertFalse(nav.isAccountDismissalInProgress)
        XCTAssertNil(nav.pendingAccountDestination)
        XCTAssertEqual(nav.presentedSheet, .liveCallDetail(lease))
    }

    func testRepeatedHistoricalRouteWhileDismissingNewestWinsAfterDismissal() {
        let auth = makeAuth(contractorId: "c-1", generation: 1)
        let nav = FrontendNavigation(authProvider: { auth })
        let record1 = makeRecord(id: "CA_HIST_1")
        let record2 = makeRecord(id: "CA_HIST_2")

        nav.openAccount()
        XCTAssertTrue(nav.isAccountPresented)

        // First route while account is open
        nav.openHistoricalDetail(call: record1)
        XCTAssertFalse(nav.isAccountPresented)
        XCTAssertTrue(nav.isAccountDismissalInProgress)
        if case .historicalDetail(let lease1) = nav.pendingAccountDestination {
            XCTAssertEqual(lease1.callId, "CA_HIST_1")
        } else {
            XCTFail("Expected historicalDetail lease1 queued")
        }

        // Second route while account is still dismissing: newest route supersedes
        nav.openHistoricalDetail(call: record2)
        XCTAssertTrue(nav.isAccountDismissalInProgress)
        if case .historicalDetail(let lease2) = nav.pendingAccountDestination {
            XCTAssertEqual(lease2.callId, "CA_HIST_2")
        } else {
            XCTFail("Expected historicalDetail lease2 queued")
        }
        XCTAssertNil(nav.presentedSheet)

        // Real onDismiss completes
        nav.handleAccountDismissed(hasRemainingSheets: false)
        XCTAssertFalse(nav.isAccountDismissalInProgress)
        XCTAssertNil(nav.pendingAccountDestination)
        if case .historicalDetail(let presented) = nav.presentedSheet {
            XCTAssertEqual(presented.callId, "CA_HIST_2")
        } else {
            XCTFail("Expected newest historicalDetail CA_HIST_2 presented after dismissal")
        }
    }

    func testAuthRotationAndScopeReplacementRejectedAtDrain() {
        var currentAuth = makeAuth(contractorId: "c-A", generation: 1)
        var currentScope = makeScope(callSid: "CA_LIVE_1", revision: 1)
        let nav = FrontendNavigation(
            authProvider: { currentAuth },
            scopeProvider: { currentScope }
        )

        // Case A: Historical detail queued under Auth A, auth rotates to Auth B
        nav.openAccount()
        nav.openHistoricalDetail(call: makeRecord(id: "CA_HIST_1"))
        XCTAssertTrue(nav.isAccountDismissalInProgress)
        XCTAssertNotNil(nav.pendingAccountDestination)

        // Auth rotates to B
        currentAuth = makeAuth(contractorId: "c-B", generation: 2)
        nav.handleAuthChange()
        XCTAssertNil(nav.pendingAccountDestination)

        nav.handleAccountDismissed(hasRemainingSheets: false)
        XCTAssertNil(nav.presentedSheet)

        // Case B: Live detail queued with Scope revision 1, scope replaced to revision 2 before drain
        currentAuth = makeAuth(contractorId: "c-A", generation: 1)
        currentScope = makeScope(callSid: "CA_LIVE_1", revision: 1)
        nav.handleAuthChange()

        nav.openAccount()
        let liveLeaseRev1 = CallPresentationLease(auth: currentAuth, scope: currentScope)
        nav.openLive(lease: liveLeaseRev1)
        XCTAssertTrue(nav.isAccountDismissalInProgress)

        // Scope advances revision before drain callback
        currentScope = makeScope(callSid: "CA_LIVE_1", revision: 2)

        nav.handleAccountDismissed(hasRemainingSheets: false)
        XCTAssertNil(nav.presentedSheet, "Stale scope revision must be rejected at drain")
    }

    func testBeginAccountDismissWithSetterDeliverLiveDuringAnimationWaitsCallback() {
        let auth = makeAuth(contractorId: "c-1", generation: 1)
        let scope = makeScope(callSid: "CA_LIVE_1", revision: 1)
        let lease = CallPresentationLease(auth: auth, scope: scope)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })

        nav.openAccount()
        XCTAssertTrue(nav.isAccountPresented)

        // User triggers dismissal via binding setter
        nav.setAccountPresented(false)
        XCTAssertFalse(nav.isAccountPresented)
        XCTAssertTrue(nav.isAccountDismissalInProgress)

        // Live call connects during dismiss animation
        nav.handleCallConnectionStarted(lease: lease, hasOpenSheets: true)
        XCTAssertTrue(nav.isCallConnectedPendingPresentation)
        XCTAssertFalse(nav.shouldPresentInCall)
        XCTAssertNil(nav.presentedSheet)

        // Dismissal animation finishes
        nav.handleAccountDismissed(hasRemainingSheets: false)
        XCTAssertFalse(nav.isCallConnectedPendingPresentation)
        XCTAssertTrue(nav.shouldPresentInCall)
        XCTAssertNil(nav.presentedSheet)
    }

    func testStartConnectedWhileQueuePendingClearsQueueAndPresentsInCall() {
        let auth = makeAuth(contractorId: "c-1", generation: 1)
        let scope = makeScope(callSid: "CA_LIVE_1", revision: 1)
        let liveLease = CallPresentationLease(auth: auth, scope: scope)
        let nav = FrontendNavigation(authProvider: { auth }, scopeProvider: { scope })
        let record = makeRecord(id: "CA_HIST_1")

        nav.openAccount()
        nav.openHistoricalDetail(call: record)
        XCTAssertNotNil(nav.pendingAccountDestination)
        XCTAssertTrue(nav.isAccountDismissalInProgress)

        // Live call connects while historical detail was queued behind Account dismissal
        nav.handleCallConnectionStarted(lease: liveLease, hasOpenSheets: true)
        XCTAssertNil(nav.pendingAccountDestination, "Connected call priority clears queued pending destination")
        XCTAssertTrue(nav.isCallConnectedPendingPresentation)
        XCTAssertFalse(nav.shouldPresentInCall)

        // Account dismissal callback occurs
        nav.handleAccountDismissed(hasRemainingSheets: false)
        XCTAssertTrue(nav.shouldPresentInCall)
        XCTAssertNil(nav.presentedSheet)

        // Subsequent attempt to open historical detail while in-call is rejected
        nav.openHistoricalDetail(call: record)
        XCTAssertNil(nav.presentedSheet, "Historical detail presentation rejected while in-call")
    }
}
