import Foundation
import Combine

enum AppTab {
    case live, recents, settings
}

class AppState: ObservableObject {
    static let shared = AppState()

    let backendURL: String = {
        if let url = Bundle.main.infoDictionary?["BackendURL"] as? String, !url.isEmpty {
            return url
        }
        #if DEBUG
        assertionFailure("BackendURL not set in Info.plist — check build configuration")
        #endif
        return "https://kevin-api-752910912062.us-central1.run.app"
    }()

    // One-time migration from UserDefaults to Keychain for existing users
    private static func migrateToKeychain(_ key: String, keychainKey: String? = nil) -> String {
        let kcKey = keychainKey ?? key
        // If already in Keychain, use that
        if let existing = KeychainManager.shared.retrieve(kcKey), !existing.isEmpty {
            return existing
        }
        // Migrate from UserDefaults if present
        if let legacy = UserDefaults.standard.string(forKey: key), !legacy.isEmpty {
            KeychainManager.shared.save(kcKey, value: legacy)
            UserDefaults.standard.removeObject(forKey: key)
            return legacy
        }
        return ""
    }

    // Onboarding
    @Published var isOnboarded: Bool = UserDefaults.standard.bool(forKey: "isOnboarded") {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.isOnboarded, forKey: "isOnboarded") } }
    }
    @Published var contractorId: String = migrateToKeychain("contractorId") {
        didSet {
            if contractorId.isEmpty {
                KeychainManager.shared.delete("contractorId")
            } else {
                KeychainManager.shared.save("contractorId", value: contractorId)
            }
        }
    }
    @Published var isRegistered: Bool = false

    // Subscription state (Keychain-backed — UI cache only, server is source of truth)
    @Published var subscriptionStatus: String = KeychainManager.shared.retrieve("subscriptionStatus") ?? "trial" {
        didSet {
            if subscriptionStatus.isEmpty {
                KeychainManager.shared.delete("subscriptionStatus")
            } else {
                KeychainManager.shared.save("subscriptionStatus", value: subscriptionStatus)
            }
        }
    }
    @Published var subscriptionTier: String = KeychainManager.shared.retrieve("subscriptionTier") ?? "none" {
        didSet {
            if subscriptionTier.isEmpty {
                KeychainManager.shared.delete("subscriptionTier")
            } else {
                KeychainManager.shared.save("subscriptionTier", value: subscriptionTier)
            }
        }
    }
    @Published var subscriptionUUID: String = KeychainManager.shared.retrieve("subscriptionUUID") ?? "" {
        didSet {
            if subscriptionUUID.isEmpty {
                KeychainManager.shared.delete("subscriptionUUID")
            } else {
                KeychainManager.shared.save("subscriptionUUID", value: subscriptionUUID)
            }
        }
    }

    var isSubscriptionActive: Bool {
        subscriptionStatus == "trial" || subscriptionStatus == "active"
    }

    @Published var kevinNumber: String = UserDefaults.standard.string(forKey: "kevinNumber") ?? "" {
        didSet {
            DispatchQueue.main.async { UserDefaults.standard.set(self.kevinNumber, forKey: "kevinNumber") }
            // New number means forwarding needs to be re-activated
            if !kevinNumber.isEmpty {
                let stored = UserDefaults.standard.string(forKey: "forwardingActivatedFor") ?? ""
                if stored != kevinNumber {
                    DispatchQueue.main.async { UserDefaults.standard.set(false, forKey: "forwardingActivated") }
                }
            }
        }
    }
    @Published var forwardingActivated: Bool = UserDefaults.standard.bool(forKey: "forwardingActivated") {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.forwardingActivated, forKey: "forwardingActivated") } }
    }
    @Published var appleUserId: String = migrateToKeychain("appleUserId") {
        didSet {
            if appleUserId.isEmpty {
                KeychainManager.shared.delete("appleUserId")
            } else {
                KeychainManager.shared.save("appleUserId", value: appleUserId)
            }
        }
    }
    @Published var appleIdentityToken: String = ""

    // Device
    @Published var pushToken: String = ""

    // Navigation
    @Published var selectedTab: AppTab = .recents

    // Active call
    @Published var activeCallSid: String = ""
    @Published var activeCallerPhone: String = ""
    @Published var activeCallerName: String = ""
    @Published var showActiveCall: Bool = false
    @Published var transcriptLines: [TranscriptLine] = []
    @Published var callStartTime: Date? = nil
    @Published var callIgnored: Bool = false
    @Published var isOnCall: Bool = false

    // Settings
    @Published var userName: String = UserDefaults.standard.string(forKey: "userName") ?? "" {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.userName, forKey: "userName") } }
    }
    @Published var businessName: String = UserDefaults.standard.string(forKey: "businessName") ?? "" {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.businessName, forKey: "businessName") } }
    }
    @Published var serviceType: String = UserDefaults.standard.string(forKey: "serviceType") ?? "" {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.serviceType, forKey: "serviceType") } }
    }
    @Published var mode: String = UserDefaults.standard.string(forKey: "kevinMode") ?? "business" {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.mode, forKey: "kevinMode") } }
    }

    var isPersonalMode: Bool { mode == "personal" }

    @Published var ringThroughContacts: Bool = UserDefaults.standard.object(forKey: "ringThroughContacts") as? Bool ?? true {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.ringThroughContacts, forKey: "ringThroughContacts") } }
    }

    @Published var sitToneEnabled: Bool = UserDefaults.standard.object(forKey: "sitToneEnabled") as? Bool ?? false {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.sitToneEnabled, forKey: "sitToneEnabled") } }
    }
    @Published var autoReplySms: Bool = UserDefaults.standard.object(forKey: "autoReplySms") as? Bool ?? false {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.autoReplySms, forKey: "autoReplySms") } }
    }
    @Published var smartInterruption: Bool = UserDefaults.standard.object(forKey: "smartInterruption") as? Bool ?? true {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.smartInterruption, forKey: "smartInterruption") } }
    }
    @Published var kevinLanguage: String = UserDefaults.standard.string(forKey: "kevinLanguage") ?? "auto" {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.kevinLanguage, forKey: "kevinLanguage") } }
    }

    // Mode change flag (skip restore, go straight to mode select)
    @Published var pendingModeChange: Bool = false

    // Unread calls — tracked locally by call ID
    @Published var readCallIds: Set<String> = {
        let arr = UserDefaults.standard.stringArray(forKey: "readCallIds") ?? []
        return Set(arr)
    }() {
        didSet {
            DispatchQueue.main.async {
                UserDefaults.standard.set(Array(self.readCallIds), forKey: "readCallIds")
            }
        }
    }

    func markCallAsRead(_ callId: String) {
        readCallIds.insert(callId)
    }

    func isCallUnread(_ call: CallRecord) -> Bool {
        call.hasMessage && !readCallIds.contains(call.id)
    }

    @Published var unreadCallCount: Int = 0

    func updateUnreadCount(calls: [CallRecord]) {
        unreadCallCount = calls.filter { isCallUnread($0) }.count
    }

    // Re-auth flag — set to true when server returns 401 (token expired/invalid)
    @Published var needsReauth: Bool = false

    // Integrations
    @Published var jobberConnected: Bool = UserDefaults.standard.bool(forKey: "jobberConnected") {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.jobberConnected, forKey: "jobberConnected") } }
    }
    @Published var googleCalendarConnected: Bool = UserDefaults.standard.bool(forKey: "googleCalendarConnected") {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.googleCalendarConnected, forKey: "googleCalendarConnected") } }
    }


    /// Whether there's an active call (even if the full-screen view is dismissed)
    var hasActiveCall: Bool {
        !activeCallSid.isEmpty
    }

    /// Set active call state from any source (push notification, API check)
    func setActiveCall(callSid: String, callerPhone: String, callerName: String) {
        activeCallSid = callSid
        activeCallerPhone = callerPhone
        activeCallerName = callerName
        callIgnored = false
        if callStartTime == nil {
            callStartTime = Date()
        }
    }

    /// Clear active call state
    func clearActiveCall() {
        activeCallSid = ""
        activeCallerPhone = ""
        activeCallerName = ""
        showActiveCall = false
        transcriptLines = []
        callStartTime = nil
        callIgnored = false
        isOnCall = false
    }

    /// Check backend for an active call (used on app foreground)
    func checkForActiveCall() {
        Task {
            if let call = await APIClient.shared.getActiveCall() {
                guard !call.callSid.isEmpty else { return }
                await MainActor.run {
                    if activeCallSid != call.callSid {
                        setActiveCall(
                            callSid: call.callSid,
                            callerPhone: call.callerPhone,
                            callerName: call.callerName
                        )
                    }
                    // Parse transcript if available
                    if !call.transcript.isEmpty {
                        transcriptLines = call.transcript
                            .components(separatedBy: "\n")
                            .filter { !$0.isEmpty }
                            .map { TranscriptLine(text: $0) }
                    }
                    showActiveCall = true
                }
            }
        }
    }
}
