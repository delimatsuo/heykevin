import Foundation
import Combine
import SwiftUI

/// Visible root tabs for the native interface.
enum FrontendTab: String, CaseIterable, Identifiable, Sendable {
    case calls = "calls"
    case kevin = "kevin"

    var id: String { rawValue }

    var localizedTitle: String {
        switch self {
        case .calls: return String(localized: "Calls")
        case .kevin: return String(localized: "Kevin")
        }
    }
}

/// An immutable presentation lease identifying a displayed historical call detail.
struct HistoricalCallPresentationLease: Identifiable, Equatable, Sendable {
    var id: String { "\(auth.contractorId)_\(auth.generation)_\(callId)" }
    let auth: CallAuthContext
    let callId: String
    let call: CallRecord

    func isValid(currentAuth: CallAuthContext) -> Bool {
        auth == currentAuth && currentAuth.isValid
    }
}

/// Single root presentation sheet enum covering historical details, live transcript details,
/// and unavailable notification alerts.
enum RootNavigationSheet: Identifiable, Equatable, Sendable {
    case historicalDetail(HistoricalCallPresentationLease)
    case liveCallDetail(CallPresentationLease)
    case unavailableNotification(callSid: String, message: String, auth: CallAuthContext)

    var id: String {
        switch self {
        case .historicalDetail(let lease):
            return "hist_\(lease.id)"
        case .liveCallDetail(let lease):
            return "live_\(lease.auth.contractorId)_\(lease.auth.generation)_\(lease.scope.callSid)_\(lease.scope.revision)"
        case .unavailableNotification(let sid, _, let auth):
            return "unavail_\(auth.contractorId)_\(auth.generation)_\(sid)"
        }
    }
}

/// A pending historical notification target queued before or during history loading.
struct PendingNotificationTarget: Equatable, Sendable {
    let callSid: String
    let auth: CallAuthContext
    let fallbackMessage: String
    let revision: UInt64
}

/// Production navigation model and adapter managing root tab routing, account sheet,
/// historical detail leases, live transcript presentations, and connected-call sequencing.
@MainActor
final class FrontendNavigation: ObservableObject {
    typealias AuthProvider = @MainActor () -> CallAuthContext
    typealias ScopeProvider = @MainActor () -> CallLifecycleSnapshot
    typealias OwnedLeaseProvider = @MainActor () -> CallPresentationLease?

    private let authProvider: AuthProvider
    private let scopeProvider: ScopeProvider
    private let ownedLeaseProvider: OwnedLeaseProvider
    private var lastKnownAuth: CallAuthContext?
    private var nextNotificationRevision: UInt64 = 1

    @Published var selectedTab: FrontendTab = .calls
    @Published var isAccountPresented: Bool = false
    @Published var presentedSheet: RootNavigationSheet? = nil
    @Published var shouldScrollToGoogleCalendar: Bool = false
    @Published var pendingCallPresentationLease: CallPresentationLease? = nil
    @Published var isCallConnectedPendingPresentation: Bool = false
    @Published var shouldPresentInCall: Bool = false
    @Published private(set) var pendingNotificationTarget: PendingNotificationTarget? = nil

    /// Backwards compatibility accessor for historical detail lease
    var presentedDetailLease: HistoricalCallPresentationLease? {
        get {
            if case .historicalDetail(let lease) = presentedSheet {
                return lease
            }
            return nil
        }
        set {
            if let lease = newValue {
                presentedSheet = .historicalDetail(lease)
            } else if case .historicalDetail = presentedSheet {
                presentedSheet = nil
            }
        }
    }

    init(
        authProvider: @escaping AuthProvider = { AppState.shared.currentAuthContext() },
        scopeProvider: @escaping ScopeProvider = { AppState.shared.callLifecycleSnapshot },
        ownedLeaseProvider: @escaping OwnedLeaseProvider = { AppState.shared.ownedActiveCallLease }
    ) {
        self.authProvider = authProvider
        self.scopeProvider = scopeProvider
        self.ownedLeaseProvider = ownedLeaseProvider
        let initialAuth = authProvider()
        self.lastKnownAuth = initialAuth.isValid ? initialAuth : nil
    }

    // MARK: - Legacy Tab Command Adapter

    /// Routes legacy internal tab requests (e.g. from notifications, WhatsNew sheet, or deep links)
    /// to the native two-tab and sheet architecture.
    func handleLegacyTabCommand(_ tab: AppTab) {
        switch tab {
        case .recents:
            selectedTab = .calls
        case .live:
            selectedTab = .calls
            if let lease = ownedLeaseProvider() {
                openLive(lease: lease)
            }
        case .settings:
            // Route to Kevin tab and highlight Google Calendar integration section.
            selectedTab = .kevin
            shouldScrollToGoogleCalendar = true
        }
    }

    // MARK: - Account Sheet Navigation

    func openAccount() {
        isAccountPresented = true
    }

    func closeAccount() {
        isAccountPresented = false
    }

    func requestReturnToCallFromAccount() -> CallPresentationLease? {
        guard let lease = ownedLeaseProvider() else { return nil }
        pendingCallPresentationLease = lease
        isAccountPresented = false
        return lease
    }

    // MARK: - Live Call Presentation

    func openLive(lease: CallPresentationLease) {
        let currentAuth = authProvider()
        let currentScope = scopeProvider()
        guard currentAuth.isValid,
              !lease.scope.callSid.isEmpty,
              lease.isValid(auth: currentAuth, scope: currentScope) else {
            return
        }
        presentedSheet = .liveCallDetail(lease)
    }

    // MARK: - Historical Detail Presentation

    /// Factory creating a closure that captures the specific call and expected auth at view render time.
    /// When invoked, guards against absent or mismatched auth before presenting historical detail.
    func makeHistoricalSelectionAction(call: CallRecord, expectedAuth: CallAuthContext?) -> () -> Void {
        guard let expected = expectedAuth, expected.isValid else {
            return {}
        }
        return { [weak self] in
            self?.openHistoricalDetail(call: call, expectedAuth: expected)
        }
    }

    func openHistoricalDetail(call: CallRecord, expectedAuth: CallAuthContext? = nil) {
        let currentAuth = authProvider()
        guard currentAuth.isValid else { return }
        if let expected = expectedAuth, expected != currentAuth { return }

        let lease = HistoricalCallPresentationLease(
            auth: currentAuth,
            callId: call.id,
            call: call
        )
        presentedSheet = .historicalDetail(lease)
    }

    func dismissHistoricalDetail() {
        if case .historicalDetail = presentedSheet {
            presentedSheet = nil
        }
    }

    func dismissSheet() {
        presentedSheet = nil
    }

    // MARK: - Pending Notification Target Management

    /// Synchronously queues a pending notification target. New target supersedes old.
    func queueNotificationTarget(
        callSid: String,
        fallbackMessage: String = "",
        auth: CallAuthContext
    ) {
        guard !callSid.isEmpty, auth.isValid else { return }
        let currentAuth = authProvider()
        guard currentAuth.isValid, currentAuth == auth else { return }

        let revision = nextNotificationRevision
        nextNotificationRevision &+= 1

        pendingNotificationTarget = PendingNotificationTarget(
            callSid: callSid,
            auth: auth,
            fallbackMessage: fallbackMessage,
            revision: revision
        )
    }

    /// Resolves the pending notification target once history loading is settled for matching auth.
    /// Does NOTHING while loading or unsettled.
    /// Returns the consumed target if resolved (so root can clear AppState fields if still matching).
    @discardableResult
    func resolvePendingNotification(
        allCalls: [CallRecord],
        loadedAuth: CallAuthContext?,
        isLoading: Bool,
        loadFinishedOrFailed: Bool
    ) -> PendingNotificationTarget? {
        guard let pending = pendingNotificationTarget else { return nil }
        let currentAuth = authProvider()
        guard currentAuth.isValid else {
            pendingNotificationTarget = nil
            return nil
        }
        guard pending.auth == currentAuth else {
            pendingNotificationTarget = nil
            return nil
        }

        // Do NOTHING while loading or unsettled
        guard !isLoading, loadFinishedOrFailed else {
            return nil
        }

        // Must match current settled loaded auth
        guard let loaded = loadedAuth, loaded == currentAuth else {
            return nil
        }

        resolveNotificationTarget(
            callSid: pending.callSid,
            allCalls: allCalls,
            expectedAuth: currentAuth,
            fallbackMessage: pending.fallbackMessage
        )

        let consumed = pending
        pendingNotificationTarget = nil
        return consumed
    }

    /// Resolves an exact call ID from the complete fetched history snapshot,
    /// outside the current search query, filter tab, or visible prefix limit.
    /// Returns the matched CallRecord and presents the detail lease if found;
    /// if unavailable, presents the unavailable notification sheet and returns nil.
    @discardableResult
    func resolveNotificationTarget(
        callSid: String,
        allCalls: [CallRecord],
        expectedAuth: CallAuthContext? = nil,
        fallbackMessage: String = ""
    ) -> CallRecord? {
        guard !callSid.isEmpty else { return nil }
        let currentAuth = authProvider()
        guard currentAuth.isValid else { return nil }
        if let expected = expectedAuth, expected != currentAuth { return nil }

        selectedTab = .calls
        if let match = allCalls.first(where: { $0.id == callSid }) {
            let lease = HistoricalCallPresentationLease(
                auth: currentAuth,
                callId: match.id,
                call: match
            )
            presentedSheet = .historicalDetail(lease)
            return match
        } else {
            presentedSheet = .unavailableNotification(
                callSid: callSid,
                message: fallbackMessage,
                auth: currentAuth
            )
            return nil
        }
    }

    // MARK: - Connected Call Presentation Sequencing

    func handleCallConnectionStarted(lease: CallPresentationLease?, hasOpenSheets: Bool) {
        let currentAuth = authProvider()
        let currentScope = scopeProvider()
        guard let lease = lease, lease.isValid(auth: currentAuth, scope: currentScope) else {
            isCallConnectedPendingPresentation = false
            shouldPresentInCall = false
            pendingCallPresentationLease = nil
            return
        }
        pendingCallPresentationLease = lease
        if hasOpenSheets {
            isCallConnectedPendingPresentation = true
            isAccountPresented = false
            presentedSheet = nil
            shouldPresentInCall = false
        } else {
            isCallConnectedPendingPresentation = false
            shouldPresentInCall = true
        }
    }

    func handleSheetDismissed(hasRemainingSheets: Bool) {
        if isCallConnectedPendingPresentation && !hasRemainingSheets {
            let currentAuth = authProvider()
            let currentScope = scopeProvider()
            if let lease = pendingCallPresentationLease, lease.isValid(auth: currentAuth, scope: currentScope) {
                isCallConnectedPendingPresentation = false
                shouldPresentInCall = true
            } else {
                isCallConnectedPendingPresentation = false
                shouldPresentInCall = false
                pendingCallPresentationLease = nil
            }
        }
    }

    func handleInCallDismissed() {
        shouldPresentInCall = false
        isCallConnectedPendingPresentation = false
        pendingCallPresentationLease = nil
    }

    // MARK: - Auth Lifecycle Invalidation

    func handleAuthChange() {
        let currentAuth = authProvider()
        if lastKnownAuth != currentAuth {
            // Any context change (A->B, A->B->A, or invalidation) clears all stale sheets and state
            presentedSheet = nil
            isAccountPresented = false
            pendingCallPresentationLease = nil
            shouldScrollToGoogleCalendar = false
            isCallConnectedPendingPresentation = false
            shouldPresentInCall = false
            pendingNotificationTarget = nil
            if !currentAuth.isValid {
                selectedTab = .calls
            }
            lastKnownAuth = currentAuth.isValid ? currentAuth : nil
        } else {
            if let pending = pendingNotificationTarget, pending.auth != currentAuth || !currentAuth.isValid {
                pendingNotificationTarget = nil
            }
            if let sheet = presentedSheet {
                switch sheet {
                case .historicalDetail(let lease):
                    if !lease.isValid(currentAuth: currentAuth) {
                        presentedSheet = nil
                    }
                case .liveCallDetail(let lease):
                    if !lease.isValid(auth: currentAuth, scope: scopeProvider()) {
                        presentedSheet = nil
                    }
                case .unavailableNotification(_, _, let auth):
                    if auth != currentAuth || !currentAuth.isValid {
                        presentedSheet = nil
                    }
                }
            }
        }
    }
}
