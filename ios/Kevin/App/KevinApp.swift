import SwiftUI

@main
struct KevinApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var appState = AppState.shared
    @Environment(\.scenePhase) var scenePhase
    @State private var lastSyncTime: Date = .distantPast
    @State private var isFirstLaunch = true
    @State private var listenerStarted = false

    var body: some Scene {
        WindowGroup {
            Group {
                if appState.isOnboarded {
                    ContentView()
                        .environmentObject(appState)
                } else {
                    OnboardingView()
                        .environmentObject(appState)
                }
            }
            .appVersionGate()
            .task {
                // Kick off a version check on cold launch so the gate can decide
                // whether to block, nudge, or stay silent.
                await AppVersionService.shared.check()
            }
        }
        .onChange(of: scenePhase) {
            if scenePhase == .active {
                // Start StoreKit transaction listener once
                if !listenerStarted {
                    listenerStarted = true
                    SubscriptionManager.shared.startTransactionListener()
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

                // Delay API calls on cold start — iOS networking needs time to initialize
                Task {
                    if isFirstLaunch {
                        isFirstLaunch = false
                        try? await Task.sleep(nanoseconds: 3_000_000_000)
                        // Retry device registration after network is warm
                        let pushToken = appState.pushToken
                        if !pushToken.isEmpty {
                            await APIClient.shared.registerDevice(pushToken: pushToken)
                        }
                        // Verify subscription entitlements once on cold launch.
                        //
                        // Audit F-5: verifyCurrentEntitlements only iterates
                        // `Transaction.currentEntitlements`. If Apple has no
                        // active entitlement to report (typical for an
                        // expired or never-subscribed user), the loop is a
                        // no-op and `appState.subscriptionStatus` stays at
                        // whatever was in Keychain — which can be a stale
                        // "trial" or "active" while the server already says
                        // "expired". Always fetch the contractor profile
                        // explicitly so the server view wins, then run the
                        // entitlement loop for any active StoreKit
                        // transactions.
                        if !appState.contractorId.isEmpty {
                            if let profile = await APIClient.shared.getContractorProfile(
                                contractorId: appState.contractorId
                            ) {
                                let status = profile["subscription_status"] as? String ?? ""
                                let tier = profile["subscription_tier"] as? String ?? ""
                                await MainActor.run {
                                    if !status.isEmpty { appState.subscriptionStatus = status }
                                    if !tier.isEmpty { appState.subscriptionTier = tier }
                                }
                            }
                            await SubscriptionManager.shared.verifyCurrentEntitlements()
                        }
                    }
                    appState.checkForActiveCall()
                }

                // Sync contacts at most once per hour — only if user has consented
                if !appState.contractorId.isEmpty,
                   appState.contactsUploadConsent,
                   Date().timeIntervalSince(lastSyncTime) > 3600 {
                    lastSyncTime = Date()
                    Task {
                        // Delay 2s to let other startup tasks finish first
                        try? await Task.sleep(nanoseconds: 2_000_000_000)
                        _ = await ContactSyncManager.shared.syncContacts(
                            contractorId: appState.contractorId
                        )
                    }
                }
            }
        }
    }
}
