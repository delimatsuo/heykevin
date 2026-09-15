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

/// Production navigation model and adapter managing root tab routing, account sheet,
/// historical detail leases, and backward compatibility with internal legacy tab commands.
@MainActor
final class FrontendNavigation: ObservableObject {
    typealias AuthProvider = @MainActor () -> CallAuthContext
    typealias ScopeProvider = @MainActor () -> CallLifecycleSnapshot

    private let authProvider: AuthProvider
    private let scopeProvider: ScopeProvider

    @Published var selectedTab: FrontendTab = .calls
    @Published var isAccountPresented: Bool = false
    @Published var presentedDetailLease: HistoricalCallPresentationLease? = nil
    @Published var shouldScrollToGoogleCalendar: Bool = false
    @Published var pendingCallPresentationLease: CallPresentationLease? = nil

    init(
        authProvider: @escaping AuthProvider = { AppState.shared.currentAuthContext() },
        scopeProvider: @escaping ScopeProvider = { AppState.shared.callLifecycleSnapshot }
    ) {
        self.authProvider = authProvider
        self.scopeProvider = scopeProvider
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
        case .settings:
            // WhatsNewSheet or other announcement requesting Google Calendar connection:
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
        let auth = authProvider()
        let scope = scopeProvider()
        guard auth.isValid, !scope.callSid.isEmpty else { return nil }
        let lease = CallPresentationLease(auth: auth, scope: scope)
        pendingCallPresentationLease = lease
        isAccountPresented = false
        return lease
    }

    // MARK: - Historical Detail Presentation

    func openHistoricalDetail(call: CallRecord, expectedAuth: CallAuthContext? = nil) {
        let currentAuth = authProvider()
        guard currentAuth.isValid else { return }
        if let expected = expectedAuth, expected != currentAuth { return }

        presentedDetailLease = HistoricalCallPresentationLease(
            auth: currentAuth,
            callId: call.id,
            call: call
        )
    }

    func dismissHistoricalDetail() {
        presentedDetailLease = nil
    }

    /// Resolves an exact call ID from the complete fetched history snapshot,
    /// outside the current search query, filter tab, or visible prefix limit.
    /// Returns the matched CallRecord and presents the detail lease if found;
    /// returns nil if unavailable.
    @discardableResult
    func resolveNotificationTarget(
        callSid: String,
        allCalls: [CallRecord],
        expectedAuth: CallAuthContext? = nil
    ) -> CallRecord? {
        guard !callSid.isEmpty else { return nil }
        let currentAuth = authProvider()
        guard currentAuth.isValid else { return nil }
        if let expected = expectedAuth, expected != currentAuth { return nil }

        guard let match = allCalls.first(where: { $0.id == callSid }) else {
            return nil
        }

        presentedDetailLease = HistoricalCallPresentationLease(
            auth: currentAuth,
            callId: match.id,
            call: match
        )
        selectedTab = .calls
        return match
    }

    // MARK: - Auth Lifecycle Invalidation

    func handleAuthChange() {
        let currentAuth = authProvider()
        if let lease = presentedDetailLease, !lease.isValid(currentAuth: currentAuth) {
            presentedDetailLease = nil
        }
        if !currentAuth.isValid {
            isAccountPresented = false
            pendingCallPresentationLease = nil
            shouldScrollToGoogleCalendar = false
            selectedTab = .calls
        }
    }
}
