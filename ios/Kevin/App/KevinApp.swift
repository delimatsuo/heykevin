import SwiftUI
import UserNotifications

@main
struct KevinApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var appState: AppState
    @Environment(\.scenePhase) var scenePhase
    @State private var lastSyncTime: Date = .distantPast
    @State private var isFirstLaunch = true
    @State private var listenerStarted = false

    init() {
        let state = AppState.shared
        AppStoreScreenshotFixtures.configure(state)
        _appState = StateObject(wrappedValue: state)
    }

    var body: some Scene {
        WindowGroup {
            if ProcessInfo.processInfo.environment["KEVIN_UNIT_TESTS"] == "1" {
                Color.clear
            } else if AppStoreScreenshotFixtures.isEnabled {
                AppStoreScreenshotRoot()
                    .environmentObject(appState)
            } else {
                Group {
                    switch appState.sessionState {
                    case .uninitialized:
                        ZStack {
                            Color.hkWarmCanvas.ignoresSafeArea()
                            ProgressView()
                        }
                    case .storageUnavailable(let reason):
                        storageUnavailableView(reason: reason)
                    case .needsRecovery:
                        OnboardingView(isRecoveryMode: true)
                            .environmentObject(appState)
                    case .notOnboarded:
                        OnboardingView(isRecoveryMode: false)
                            .environmentObject(appState)
                    case .ready:
                        if appState.isOnboarded {
                            ContentView()
                                .environmentObject(appState)
                        } else {
                            OnboardingView(isRecoveryMode: false)
                                .environmentObject(appState)
                        }
                    }
                }
                .onChange(of: appState.isOnboarded) {
                    appState.refreshSecureStorageForActiveUse()
                }
                .onChange(of: appState.sessionState) {
                    guard appState.isOnboarded, appState.sessionState == .ready else { return }
                    Task { @MainActor in await PushRegistrationCoordinator.shared.handleAccountReady() }
                }
                .onChange(of: appState.currentAuthContext()) {
                    PushRegistrationCoordinator.shared.handleAuthChange()
                    Task { @MainActor in
                        await AcquisitionCoordinator.shared.handleAuthChange()
                    }
                    guard appState.isOnboarded, appState.sessionState == .ready else { return }
                    Task { @MainActor in await PushRegistrationCoordinator.shared.handleAccountReady() }
                }
                .appVersionGate()
                .task {
                    await MainActor.run {
                        appState.refreshSecureStorageForActiveUse()
                    }
                    // Kick off a version check on cold launch so the gate can decide
                    // whether to block, nudge, or stay silent.
                    await AppVersionService.shared.check()
                }
            }
        }
        .onChange(of: scenePhase) {
            if ProcessInfo.processInfo.environment["KEVIN_UNIT_TESTS"] == "1" { return }
            if scenePhase == .active {
                guard !AppStoreScreenshotFixtures.isEnabled else { return }
                appState.refreshSecureStorageForActiveUse()

                // Start StoreKit transaction listener once
                if !listenerStarted {
                    listenerStarted = true
                    SubscriptionManager.shared.startTransactionListener()
                }

                // Push Registration and Permission Reconciliation via coordinator
                Task { @MainActor in
                    await PushRegistrationCoordinator.shared.handleSceneActive()
                }

                // Acquisition Attribution Coordinator
                Task { @MainActor in
                    await AcquisitionCoordinator.shared.handleSceneActive()
                }

                #if DEBUG
                // Network diagnostic — test both Google (known-good) and our backend
                Task {
                    // Test 1: Can we reach ANY server?
                    let g = Date()
                    do {
                        var r = URLRequest(url: URL(string: "https://www.google.com/generate_204")!)
                        r.timeoutInterval = 5
                        let (_, resp) = try await URLSession.shared.data(for: r)
                        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
                        print("🟢 GOOGLE: \(code) in \(Int(Date().timeIntervalSince(g)*1000))ms")
                    } catch {
                        print("🔴 GOOGLE FAILED in \(Int(Date().timeIntervalSince(g)*1000))ms: \(error.localizedDescription)")
                    }
                    // Test 2: Can we reach Cloud Run?
                    let s = Date()
                    do {
                        var r = URLRequest(url: URL(string: "\(appState.backendURL)/health")!)
                        r.timeoutInterval = 5
                        let (data, resp) = try await URLSession.shared.data(for: r)
                        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
                        let body = String(data: data, encoding: .utf8) ?? ""
                        print("🟢 HEALTH: \(code) in \(Int(Date().timeIntervalSince(s)*1000))ms — \(body)")
                    } catch {
                        print("🔴 HEALTH FAILED in \(Int(Date().timeIntervalSince(s)*1000))ms: \(error.localizedDescription)")
                    }
                }
                #endif

                let wasFirstLaunch = isFirstLaunch
                if wasFirstLaunch {
                    isFirstLaunch = false
                }

                // Delay API calls on cold start — iOS networking needs time to initialize
                Task {
                    if wasFirstLaunch {
                        try? await Task.sleep(nanoseconds: 3_000_000_000)
                    }

                    let auth = appState.currentAuthContext()
                    guard appState.isOnboarded, appState.sessionState == .ready, auth.isValid else { return }
                    if wasFirstLaunch {
                        // Keep the server subscription snapshot bound to this session.
                        if auth.isValid {
                            if let profile = await APIClient.shared.getContractorProfile(
                                contractorId: auth.contractorId, bearerToken: auth.bearerToken
                            ) {
                                guard appState.currentAuthContext() == auth, appState.sessionState == .ready else { return }
                                let status = profile["subscription_status"] as? String ?? ""
                                let tier = profile["subscription_tier"] as? String ?? ""
                                await MainActor.run {
                                    if !status.isEmpty { appState.subscriptionStatus = status }
                                    if !tier.isEmpty { appState.subscriptionTier = tier }
                                }
                            }
                            guard appState.currentAuthContext() == auth, appState.sessionState == .ready else { return }
                            await SubscriptionManager.shared.verifyCurrentEntitlements()
                        }
                    }
                    guard appState.currentAuthContext() == auth, appState.sessionState == .ready else { return }
                    appState.checkForActiveCall()
                }

                // Ask for an automatic contact sync at most once per foreground hour.
                // ContactSyncManager also persists a longer battery guard across launches.
                let syncAuth = appState.currentAuthContext()
                if appState.isOnboarded, appState.sessionState == .ready, syncAuth.isValid,
                   appState.contactsUploadConsent,
                   Date().timeIntervalSince(lastSyncTime) > 3600 {
                    lastSyncTime = Date()
                    Task {
                        // Delay 2s to let other startup tasks finish first
                        try? await Task.sleep(nanoseconds: 2_000_000_000)
                        guard appState.currentAuthContext() == syncAuth, appState.sessionState == .ready,
                              appState.contactsUploadConsent else { return }
                        _ = await ContactSyncManager.shared.syncContacts(contractorId: syncAuth.contractorId)
                    }
                }
            }
        }
    }

    private func storageUnavailableView(reason: String) -> some View {
        VStack(spacing: 16) {
            Image(systemName: "lock.shield")
                .font(.system(size: 48))
                .foregroundStyle(Color.hkOrange)

            Text(String(localized: "Storage Unavailable"))
                .font(.title2.bold())
                .multilineTextAlignment(.center)

            Text(String(localized: "Kevin cannot access secure storage on this device right now. Please unlock your device and retry."))
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)

            Button(String(localized: "Retry")) {
                appState.refreshSecureStorageForActiveUse()
            }
            .buttonStyle(.borderedProminent)
            .tint(Color.hkCobalt)
            .frame(minHeight: 44)
            .padding(.top, 8)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.hkWarmCanvas.ignoresSafeArea())
    }
}
