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
    @StateObject private var historyModel: CallHistoryModel
    @StateObject private var frontendNav = FrontendNavigation()

    @State private var showForcedPaywall = false
    @State private var showWhatsNew = false
    @State private var presentedCallLease: CallPresentationLease?

    init() {
        _historyModel = StateObject(wrappedValue: {
            #if DEBUG
            if AppStoreScreenshotFixtures.isEnabled {
                return AppStoreScreenshotFixtures.makeHistoryModel()
            }
            #endif
            return CallHistoryModel()
        }())
    }

    var body: some View {
        SettingsHost(
            isAccountPresented: Binding(
                get: { frontendNav.isAccountPresented },
                set: { frontendNav.setAccountPresented($0) }
            ),
            shouldScrollToGoogleCalendar: $frontendNav.shouldScrollToGoogleCalendar,
            onDismissAccount: {
                let hasOpenSheets = frontendNav.presentedSheet != nil || showWhatsNew
                frontendNav.handleAccountDismissed(hasRemainingSheets: hasOpenSheets)
            },
            assistantIsSelected: frontendNav.selectedTab == .kevin,
            onOpenCall: { lease in
                frontendNav.openLive(lease: lease)
            }
        ) { assistantScreen in
            TabView(selection: $frontendNav.selectedTab) {
                CallHistoryView(
                    historyModel: historyModel,
                    navigation: frontendNav,
                    onOpenSettings: {
                        frontendNav.openAccount()
                    }
                )
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
        .sheet(item: $frontendNav.presentedSheet, onDismiss: {
            let hasOpenSheets = frontendNav.isAccountPresented || frontendNav.isAccountDismissalInProgress || showWhatsNew
            frontendNav.handleSheetDismissed(hasRemainingSheets: hasOpenSheets)
        }) { sheet in
            NavigationStack {
                switch sheet {
                case .historicalDetail(let lease):
                    CallDetailView(lease: lease, historyModel: historyModel)
                        .toolbar {
                            ToolbarItem(placement: .confirmationAction) {
                                Button(String(localized: "Done")) {
                                    frontendNav.dismissSheet()
                                }
                                .font(.headline)
                                .accessibilityIdentifier("calls.detailDone")
                            }
                        }
                case .liveCallDetail(let lease):
                    LiveCallDetailView(lease: lease, onDone: {
                        frontendNav.dismissSheet()
                    })
                case .unavailableNotification(_, let message, _):
                    ContentUnavailableView(
                        String(localized: "Call details unavailable"),
                        systemImage: "phone.badge.questionmark",
                        description: Text(message.isEmpty
                            ? String(localized: "This call is no longer in your history.")
                            : message)
                    )
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button(String(localized: "Done")) {
                                frontendNav.dismissSheet()
                            }
                            .font(.headline)
                            .accessibilityIdentifier("calls.unavailableDone")
                        }
                    }
                }
            }
        }
        .onChange(of: scenePhase) { _, phase in
            #if DEBUG
            if AppStoreScreenshotFixtures.isEnabled { return }
            #endif
            if phase == .active {
                if !callManager.isOnCall {
                    LiveCallObserver.shared.start()
                }
                Task {
                    await historyModel.loadCalls()
                }
            } else {
                LiveCallObserver.shared.stop()
            }
        }
        .onDisappear {
            #if DEBUG
            if AppStoreScreenshotFixtures.isEnabled { return }
            #endif
            LiveCallObserver.shared.stop()
        }
        .onChange(of: appState.hasActiveCall) { previousHadCall, currentHasCall in
            if previousHadCall && !currentHasCall {
                #if DEBUG
                if AppStoreScreenshotFixtures.isEnabled { return }
                #endif
                Task {
                    await historyModel.loadCalls()
                }
            }
        }
        .onReceive(appState.$selectedTab.dropFirst()) { legacyTab in
            frontendNav.handleLegacyTabCommand(legacyTab)
        }
        .onChange(of: appState.notificationCallSid) { _, sid in
            guard !sid.isEmpty else { return }
            let currentAuth = appState.currentAuthContext()
            frontendNav.queueNotificationTarget(
                callSid: sid,
                fallbackMessage: appState.notificationCallMessage,
                auth: currentAuth
            )
            Task {
                await historyModel.loadCalls()
                attemptNotificationResolution()
            }
        }
        .onChange(of: historyModel.isLoading) { _, loading in
            if !loading {
                attemptNotificationResolution()
            }
        }
        .onChange(of: historyModel.lastLoadedRevision) { _, _ in
            attemptNotificationResolution()
        }
        .onChange(of: callManager.isOnCall) { wasOnCall, isOnCall in
            if isOnCall {
                LiveCallObserver.shared.stop()
                let lease = callManager.presentationLease
                let hasOpen = frontendNav.isAccountPresented || frontendNav.isAccountDismissalInProgress || frontendNav.presentedSheet != nil || showWhatsNew
                if showWhatsNew {
                    showWhatsNew = false
                }
                frontendNav.handleCallConnectionStarted(lease: lease, hasOpenSheets: hasOpen)
            } else if wasOnCall && !isOnCall {
                if let lease = presentedCallLease ?? frontendNav.pendingCallPresentationLease {
                    if appState.activeCallSid == lease.scope.callSid,
                       let activeAuth = appState.activeCallAuth,
                       activeAuth == lease.auth {
                        appState.clearActiveCall()
                    }
                }
                presentedCallLease = nil
                frontendNav.handleInCallDismissed()
                #if DEBUG
                if !AppStoreScreenshotFixtures.isEnabled {
                    Task { await historyModel.loadCalls() }
                    if scenePhase == .active {
                        LiveCallObserver.shared.start()
                    }
                }
                #else
                Task { await historyModel.loadCalls() }
                if scenePhase == .active {
                    LiveCallObserver.shared.start()
                }
                #endif
            }
        }
        .fullScreenCover(isPresented: $frontendNav.shouldPresentInCall, onDismiss: {
            presentedCallLease = nil
            frontendNav.handleInCallDismissed()
            #if DEBUG
            if !AppStoreScreenshotFixtures.isEnabled {
                StoreReviewManager.shared.incrementScreenedCallCount()
                StoreReviewManager.shared.requestReviewIfEligible()
            }
            #else
            StoreReviewManager.shared.incrementScreenedCallCount()
            StoreReviewManager.shared.requestReviewIfEligible()
            #endif
        }) {
            if let lease = callManager.presentationLease,
               lease.isValid(auth: appState.currentAuthContext(), scope: appState.callLifecycleSnapshot) {
                InCallView(lease: lease)
                    .onAppear { presentedCallLease = lease }
            }
        }
        // Force paywall when trial expires — cannot be dismissed without subscribing
        .fullScreenCover(isPresented: $showForcedPaywall) {
            PaywallView(canDismiss: false)
                .environmentObject(appState)
        }
        // Feature announcement for 1.2.8
        .sheet(isPresented: $showWhatsNew, onDismiss: {
            let hasOpenSheets = frontendNav.isAccountPresented || frontendNav.isAccountDismissalInProgress || frontendNav.presentedSheet != nil
            frontendNav.handleSheetDismissed(hasRemainingSheets: hasOpenSheets)
        }) {
            WhatsNewSheet(needsCalendar: !appState.googleCalendarConnected)
                .environmentObject(appState)
        }
        .task {
            #if DEBUG
            if AppStoreScreenshotFixtures.isNativeUIReview,
               ProcessInfo.processInfo.environment["KEVIN_REVIEW_NOTIFICATION"] == "warm" {
                Task { @MainActor in
                    try? await Task.sleep(for: .seconds(2))
                    await AppStoreScreenshotFixtures.openReviewNotification(appState)
                }
            }
            #endif
            if !appState.notificationCallSid.isEmpty {
                let currentAuth = appState.currentAuthContext()
                frontendNav.queueNotificationTarget(
                    callSid: appState.notificationCallSid,
                    fallbackMessage: appState.notificationCallMessage,
                    auth: currentAuth
                )
            }
            await historyModel.loadCalls()
            attemptNotificationResolution()
        }
        .onAppear {
            #if DEBUG
            if let scenario = AppStoreScreenshotFixtures.scenario {
                if scenario == .accountSettings {
                    frontendNav.openAccount()
                } else if scenario == .businessDetail || scenario == .personalDetail {
                    frontendNav.openHistoricalDetail(call: AppStoreScreenshotFixtures.featuredCall)
                }
            }
            #endif
            showForcedPaywall = appState.subscriptionStatus == "expired"
            if !showForcedPaywall {
                showWhatsNew = WhatsNewSheet.shouldPresent(isOnboarded: appState.isOnboarded)
            }
            frontendNav.handleLegacyTabCommand(appState.selectedTab)
            if callManager.isOnCall,
               let lease = callManager.presentationLease,
               lease.isValid(auth: appState.currentAuthContext(), scope: appState.callLifecycleSnapshot) {
                let hasOpen = frontendNav.isAccountPresented || frontendNav.isAccountDismissalInProgress || frontendNav.presentedSheet != nil || showWhatsNew
                if showWhatsNew {
                    showWhatsNew = false
                }
                frontendNav.handleCallConnectionStarted(lease: lease, hasOpenSheets: hasOpen)
            } else {
                #if DEBUG
                if !AppStoreScreenshotFixtures.isEnabled {
                    if scenePhase == .active {
                        LiveCallObserver.shared.start()
                    }
                }
                #else
                if scenePhase == .active {
                    LiveCallObserver.shared.start()
                }
                #endif
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
            historyModel.invalidate()
            Task { await historyModel.loadCalls() }
        }
    }

    private func attemptNotificationResolution() {
        let loadFinishedOrFailed = !historyModel.isLoading && (historyModel.lastLoadedRevision > 0 || historyModel.errorMessage != nil)
        if let consumed = frontendNav.resolvePendingNotification(
            allCalls: historyModel.allCalls,
            loadedAuth: historyModel.activeAuthContext,
            isLoading: historyModel.isLoading,
            loadFinishedOrFailed: loadFinishedOrFailed
        ) {
            if appState.notificationCallSid == consumed.callSid && appState.currentAuthContext() == consumed.auth {
                appState.notificationCallSid = ""
                appState.notificationCallMessage = ""
            }
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
