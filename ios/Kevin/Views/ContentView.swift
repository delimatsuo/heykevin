import SwiftUI

/// Feature flag: hide "Text reply" until A2P 10DLC carrier registration is approved.
private let kTextReplyEnabled = false

struct TranscriptLine: Identifiable {
    let id = UUID()
    let text: String
}

/// Root content view hosting the native two-tab layout (Calls and Kevin)
/// wrapped in the single-owner SettingsHost.
struct ContentView: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var callManager = CallManager.shared
    @Environment(\.scenePhase) var scenePhase
    @StateObject private var frontendNav = FrontendNavigation()

    @State private var showForcedPaywall = false
    @State private var showWhatsNew = false
    @State private var presentedCallLease: CallPresentationLease?

    var body: some View {
        SettingsHost(
            isAccountPresented: $frontendNav.isAccountPresented,
            assistantIsSelected: frontendNav.selectedTab == .kevin,
            onOpenCall: { lease in
                frontendNav.selectedTab = .calls
            }
        ) { assistantScreen in
            TabView(selection: $frontendNav.selectedTab) {
                CallHistoryView(onOpenSettings: {
                    frontendNav.openAccount()
                })
                .tabItem {
                    Label(String(localized: "Calls"), systemImage: "phone.badge.waveform")
                }
                .tag(FrontendTab.calls)
                .badge(callsTabBadge)
                .accessibilityIdentifier("calls.tab")

                NavigationStack {
                    assistantScreen
                }
                .tabItem {
                    Label(String(localized: "Kevin"), systemImage: "person.crop.circle.badge.checkmark")
                }
                .tag(FrontendTab.kevin)
                .accessibilityIdentifier("kevin.tab")
            }
        }
        .onChange(of: scenePhase) { _, phase in
            #if DEBUG
            if AppStoreScreenshotFixtures.isEnabled { return }
            #endif
            if phase == .active {
                LiveCallObserver.shared.start()
            } else if phase == .background {
                LiveCallObserver.shared.stop()
            }
        }
        .onChange(of: appState.selectedTab) { _, legacyTab in
            frontendNav.handleLegacyTabCommand(legacyTab)
        }
        .onChange(of: appState.showActiveCall) { _, show in
            if show {
                frontendNav.selectedTab = .calls
            }
        }
        .fullScreenCover(isPresented: $callManager.isOnCall, onDismiss: {
            // A delayed dismissal of A must not clear a newer B or a new session.
            if presentedCallLease?.mayClear(auth: appState.currentAuthContext(), scope: appState.callLifecycleSnapshot) == true {
                appState.clearActiveCall()
                frontendNav.selectedTab = .calls
            }
            presentedCallLease = nil
            StoreReviewManager.shared.incrementScreenedCallCount()
            StoreReviewManager.shared.requestReviewIfEligible()
        }) {
            InCallView()
                .onAppear { presentedCallLease = callManager.presentationLease }
        }
        // Force paywall when trial expires — cannot be dismissed without subscribing
        .fullScreenCover(isPresented: $showForcedPaywall) {
            PaywallView(canDismiss: false)
                .environmentObject(appState)
        }
        // Feature announcement for 1.2.8
        .sheet(isPresented: $showWhatsNew) {
            WhatsNewSheet(needsCalendar: !appState.googleCalendarConnected)
                .environmentObject(appState)
        }
        .onAppear {
            #if DEBUG
            if AppStoreScreenshotFixtures.isEnabled { return }
            #endif
            showForcedPaywall = appState.subscriptionStatus == "expired"
            if !showForcedPaywall {
                showWhatsNew = WhatsNewSheet.shouldPresent(isOnboarded: appState.isOnboarded)
            }
            if scenePhase == .active {
                LiveCallObserver.shared.start()
            }
        }
        .onChange(of: appState.subscriptionStatus) { _, status in
            showForcedPaywall = (status == "expired")
        }
        // Re-auth alert when token is invalid
        .alert(String(localized: "Session Expired"), isPresented: $appState.needsReauth) {
            Button(String(localized: "Sign In Again")) {
                appState.needsReauth = false
                appState.isOnboarded = false
            }
            Button(String(localized: "Later"), role: .cancel) {
                appState.needsReauth = false
            }
        } message: {
            Text(String(localized: "Your session has expired. Sign in again to continue."))
        }
        .onReceive(NotificationCenter.default.publisher(for: CallSessionEpoch.didChangeNotification)) { _ in
            frontendNav.handleAuthChange()
            LiveCallObserver.shared.handleAuthChange()
        }
    }

    private var callsTabBadge: String? {
        if appState.unreadCallCount > 0 {
            return "\(appState.unreadCallCount)"
        }
        if appState.hasActiveCall {
            return "1"
        }
        return nil
    }
}
