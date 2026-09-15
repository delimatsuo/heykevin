import Foundation
import Combine

enum AppTab {
    case live, recents, settings
}

class AppState: ObservableObject {
    static let shared = AppState()

    private static let keychainBackedKeys = [
        "contractorId",
        "appleUserId",
        "subscriptionStatus",
        "subscriptionTier",
        "subscriptionUUID",
        "contractorApiToken",
    ]

    let backendURL: String = {
        guard let url = Bundle.main.infoDictionary?["BackendURL"] as? String,
              !url.isEmpty,
              !url.contains("$(") else {
            fatalError("BackendURL not set in Info.plist. Check the active build configuration.")
        }

        #if DEBUG || STAGING
        if url == "https://kevin-api-752910912062.us-central1.run.app" {
            fatalError("Non-production build is configured to use the production backend.")
        }
        #endif

        return url
    }()

    // When taking App Store screenshots, avoid persisting any local state changes.
    // This ensures fixture-only values do not leak into normal debug sessions.
    #if DEBUG
    private var inScreenshotFixture: Bool {
        ProcessInfo.processInfo.environment["APP_STORE_SCREENSHOT_SCENARIO"] != nil
    }
    #else
    private var inScreenshotFixture: Bool { false }
    #endif

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

    init() {
        let initialContractor = Self.migrateToKeychain("contractorId")
        self.contractorId = initialContractor
        self.readCallIds = Self.loadReadCallIds(for: initialContractor)
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handleAuthEpochChanged),
            name: CallSessionEpoch.didChangeNotification,
            object: nil
        )
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    @objc private func handleAuthEpochChanged() {
        if Thread.isMainThread {
            self.applyAuthChange()
        } else {
            DispatchQueue.main.async { [weak self] in
                self?.applyAuthChange()
            }
        }
    }

    private func applyAuthChange() {
        let current = contractorId
        readCallIds = Self.loadReadCallIds(for: current)
        unreadCallCount = 0
        notificationCallSid = ""
        notificationCallMessage = ""
    }

    // Session generation incremented on logout, contractor change, and credential change
    var sessionGeneration: Int { CallSessionEpoch.shared.generation }

    // Onboarding
    @Published var isOnboarded: Bool = UserDefaults.standard.bool(forKey: "isOnboarded") {
        didSet {
            let loggedOut = oldValue && !isOnboarded
            if !inScreenshotFixture {
                DispatchQueue.main.async { UserDefaults.standard.set(self.isOnboarded, forKey: "isOnboarded") }
            }
            if loggedOut {
                callLifecycleRevision += 1
                CallSessionEpoch.shared.advance()
            }
        }
    }
    @Published var contractorId: String = migrateToKeychain("contractorId") {
        didSet {
            let changed = oldValue != contractorId
            if !inScreenshotFixture {
                if contractorId.isEmpty {
                    KeychainManager.shared.delete("contractorId")
                } else {
                    KeychainManager.shared.save("contractorId", value: contractorId)
                }
            }
            if changed {
                callLifecycleRevision += 1
                readCallIds = Self.loadReadCallIds(for: contractorId)
                unreadCallCount = 0
                notificationCallSid = ""
                notificationCallMessage = ""
                CallSessionEpoch.shared.advance()
            }
        }
    }
    @Published var isRegistered: Bool = false

    // Subscription state (Keychain-backed — UI cache only, server is source of truth)
    @Published var subscriptionStatus: String = KeychainManager.shared.retrieve("subscriptionStatus") ?? "trial" {
        didSet {
            if inScreenshotFixture { return }
            if subscriptionStatus.isEmpty {
                KeychainManager.shared.delete("subscriptionStatus")
            } else {
                KeychainManager.shared.save("subscriptionStatus", value: subscriptionStatus)
            }
        }
    }
    @Published var subscriptionTier: String = KeychainManager.shared.retrieve("subscriptionTier") ?? "none" {
        didSet {
            if inScreenshotFixture { return }
            if subscriptionTier.isEmpty {
                KeychainManager.shared.delete("subscriptionTier")
            } else {
                KeychainManager.shared.save("subscriptionTier", value: subscriptionTier)
            }
        }
    }
    @Published var subscriptionUUID: String = KeychainManager.shared.retrieve("subscriptionUUID") ?? "" {
        didSet {
            if inScreenshotFixture { return }
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
            if inScreenshotFixture { return }
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
        didSet {
            if inScreenshotFixture { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.forwardingActivated, forKey: "forwardingActivated") }
        }
    }
    @Published var appleUserId: String = migrateToKeychain("appleUserId") {
        didSet {
            if inScreenshotFixture { return }
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
    @Published var voipToken: String = ""

    // Navigation
    @Published var selectedTab: AppTab = .recents

    // Active call
    private(set) var callLifecycleRevision = 0
    var callLifecycleSnapshot: CallLifecycleSnapshot { CallLifecycleSnapshot(callSid: activeCallSid, revision: callLifecycleRevision) }
    @Published var notificationCallSid = ""
    @Published var notificationCallMessage = ""
    @Published var activeCallSid: String = "" {
        didSet { if oldValue != activeCallSid { callLifecycleRevision += 1 } }
    }
    @Published var activeCallerPhone: String = ""
    @Published var activeCallerName: String = ""
    @Published var showActiveCall: Bool = false
    @Published var transcriptLines: [TranscriptLine] = []
    @Published var callStartTime: Date? = nil
    @Published var callIgnored: Bool = false
    @Published var isOnCall: Bool = false

    // Settings
    @Published var userName: String = UserDefaults.standard.string(forKey: "userName") ?? "" {
        didSet {
            if inScreenshotFixture { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.userName, forKey: "userName") }
        }
    }
    @Published var businessName: String = UserDefaults.standard.string(forKey: "businessName") ?? "" {
        didSet {
            if inScreenshotFixture { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.businessName, forKey: "businessName") }
        }
    }
    // Business street address and city required for Twilio number
    // provisioning in regulatory countries (RegulatoryAddress.countries).
    // Business contact details, not credentials, so UserDefaults matches
    // businessName rather than the Keychain.
    @Published var businessAddress: String = UserDefaults.standard.string(forKey: "businessAddress") ?? "" {
        didSet {
            if inScreenshotFixture { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.businessAddress, forKey: "businessAddress") }
        }
    }
    @Published var businessCity: String = UserDefaults.standard.string(forKey: "businessCity") ?? "" {
        didSet {
            if inScreenshotFixture { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.businessCity, forKey: "businessCity") }
        }
    }
    @Published var serviceType: String = UserDefaults.standard.string(forKey: "serviceType") ?? "" {
        didSet {
            if inScreenshotFixture { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.serviceType, forKey: "serviceType") }
        }
    }
    @Published var mode: String = UserDefaults.standard.string(forKey: "kevinMode") ?? "business" {
        didSet {
            if inScreenshotFixture { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.mode, forKey: "kevinMode") }
        }
    }

    var isPersonalMode: Bool { mode == "personal" }
    var hasBusinessEntitlement: Bool {
        (subscriptionStatus == "trial" || subscriptionStatus == "active")
            && (subscriptionTier == "business" || subscriptionTier == "businessPro")
    }
    var hasBusinessProEntitlement: Bool {
        (subscriptionStatus == "trial" || subscriptionStatus == "active")
            && subscriptionTier == "businessPro"
    }

    @Published var ringThroughContacts: Bool = UserDefaults.standard.object(forKey: "ringThroughContacts") as? Bool ?? true {
        didSet {
            if inScreenshotFixture { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.ringThroughContacts, forKey: "ringThroughContacts") }
        }
    }

    // Explicit consent to upload contacts to the server. Separate from iOS contacts permission.
    @Published var contactsUploadConsent: Bool = UserDefaults.standard.bool(forKey: "contactsUploadConsent") {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.contactsUploadConsent, forKey: "contactsUploadConsent") } }
    }

    // Carrier type for forwarding codes. Verizon uses *71/*73; everyone else uses GSM codes.
    // Set during onboarding forwarding step; editable from Settings.
    @Published var isVerizonCarrier: Bool = UserDefaults.standard.bool(forKey: "isVerizonCarrier") {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.isVerizonCarrier, forKey: "isVerizonCarrier") } }
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

    // Account country (ISO 3166-1 alpha-2), root-authoritative on the server;
    // mirrored here so forwarding codes key on it. Empty = not yet loaded.
    @Published var countryCode: String = UserDefaults.standard.string(forKey: "countryCode") ?? "" {
        didSet { DispatchQueue.main.async { UserDefaults.standard.set(self.countryCode, forKey: "countryCode") } }
    }

    private static func loadReadCallIds(for contractor: String) -> Set<String> {
        guard !contractor.isEmpty else { return [] }
        let arr = UserDefaults.standard.stringArray(forKey: "readCallIds_\(contractor)") ?? []
        return Set(arr)
    }

    // Unread calls — tracked locally by call ID, scoped per contractor with synchronous persistence
    @Published var readCallIds: Set<String> = [] {
        didSet {
            if inScreenshotFixture { return }
            let contractor = contractorId
            guard !contractor.isEmpty else { return }
            UserDefaults.standard.set(Array(self.readCallIds), forKey: "readCallIds_\(contractor)")
        }
    }

    func markCallAsRead(_ callId: String) {
        readCallIds.insert(callId)
    }

    func isCallUnread(_ call: CallRecord) -> Bool {
        if call.readOnServer { return false }
        return call.hasMessage && !readCallIds.contains(call.id)
    }

    @Published var unreadCallCount: Int = 0

    func updateUnreadCount(calls: [CallRecord]) {
        unreadCallCount = calls.filter { isCallUnread($0) }.count
    }

    func pruneReadCallIds(validIds: Set<String>) {
        readCallIds = readCallIds.intersection(validIds)
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


    /// Current snapshot of authorization context
    func currentAuthContext() -> CallAuthContext {
        CallAuthContext(
            contractorId: contractorId,
            bearerToken: APIClient.shared.contractorToken,
            generation: sessionGeneration
        )
    }

    /// Whether there's an active call (even if the full-screen view is dismissed)
    var hasActiveCall: Bool {
        !activeCallSid.isEmpty
    }

    /// Set active call state from any source (push notification, API check)
    func setActiveCall(callSid: String, callerPhone: String, callerName: String) {
        let isSameCall = activeCallSid == callSid
        activeCallSid = callSid
        activeCallerPhone = callerPhone
        activeCallerName = callerName
        if !isSameCall {
            callIgnored = false
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
        Task { @MainActor in
            self.refreshSecureStorageForActiveUse()
            _ = await LiveCallObserver.shared.checkNow()
        }
    }

    @MainActor
    func refreshSecureStorageForActiveUse() {
        KeychainManager.shared.migrateAccessibility(for: Self.keychainBackedKeys)

        if contractorId.isEmpty,
           let savedContractorId = KeychainManager.shared.retrieve("contractorId"),
           !savedContractorId.isEmpty {
            contractorId = savedContractorId
        }

        if appleUserId.isEmpty,
           let savedAppleUserId = KeychainManager.shared.retrieve("appleUserId"),
           !savedAppleUserId.isEmpty {
            appleUserId = savedAppleUserId
        }

        if subscriptionUUID.isEmpty,
           let savedSubscriptionUUID = KeychainManager.shared.retrieve("subscriptionUUID"),
           !savedSubscriptionUUID.isEmpty {
            subscriptionUUID = savedSubscriptionUUID
        }

        if let savedSubscriptionStatus = KeychainManager.shared.retrieve("subscriptionStatus"),
           !savedSubscriptionStatus.isEmpty,
           subscriptionStatus != savedSubscriptionStatus {
            subscriptionStatus = savedSubscriptionStatus
        }

        if let savedSubscriptionTier = KeychainManager.shared.retrieve("subscriptionTier"),
           !savedSubscriptionTier.isEmpty,
           subscriptionTier != savedSubscriptionTier {
            subscriptionTier = savedSubscriptionTier
        }
    }
}
