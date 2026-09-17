import Foundation
import Combine

struct CapturedScreeningTranscript: Equatable, Sendable {
    let lease: CallPresentationLease
    let lines: [String]
    let reason: String
}

enum AppTab {
    case live, recents, settings
}

class AppState: ObservableObject {
    static let shared: AppState = {
        #if DEBUG
        if ProcessInfo.processInfo.environment["KEVIN_UNIT_TESTS"] == "1" {
            return AppState(inMemory: true)
        }
        #endif
        return AppState()
    }()

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

    let inMemory: Bool
    private let authProviderClosure: (() -> CallAuthContext)?

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

    init(authProvider: (() -> CallAuthContext)? = nil, inMemory: Bool = false) {
        self.inMemory = inMemory
        self.authProviderClosure = authProvider

        if !inMemory && !inScreenshotFixture {
            let initialContractor = Self.migrateToKeychain("contractorId")
            self.contractorId = initialContractor
            self.isOnboarded = UserDefaults.standard.bool(forKey: "isOnboarded")
            self.subscriptionStatus = KeychainManager.shared.retrieve("subscriptionStatus") ?? "trial"
            self.subscriptionTier = KeychainManager.shared.retrieve("subscriptionTier") ?? "none"
            self.subscriptionUUID = KeychainManager.shared.retrieve("subscriptionUUID") ?? ""
            self.kevinNumber = UserDefaults.standard.string(forKey: "kevinNumber") ?? ""
            self.forwardingActivated = UserDefaults.standard.bool(forKey: "forwardingActivated")
            self.appleUserId = Self.migrateToKeychain("appleUserId")
            self.userName = UserDefaults.standard.string(forKey: "userName") ?? ""
            self.businessName = UserDefaults.standard.string(forKey: "businessName") ?? ""
            self.businessAddress = UserDefaults.standard.string(forKey: "businessAddress") ?? ""
            self.businessCity = UserDefaults.standard.string(forKey: "businessCity") ?? ""
            self.serviceType = UserDefaults.standard.string(forKey: "serviceType") ?? ""
            self.mode = UserDefaults.standard.string(forKey: "kevinMode") ?? "business"
            self.ringThroughContacts = UserDefaults.standard.object(forKey: "ringThroughContacts") as? Bool ?? true
            self.contactsUploadConsent = UserDefaults.standard.bool(forKey: "contactsUploadConsent")
            self.isVerizonCarrier = UserDefaults.standard.bool(forKey: "isVerizonCarrier")
            self.sitToneEnabled = UserDefaults.standard.object(forKey: "sitToneEnabled") as? Bool ?? false
            self.autoReplySms = UserDefaults.standard.object(forKey: "autoReplySms") as? Bool ?? false
            self.smartInterruption = UserDefaults.standard.object(forKey: "smartInterruption") as? Bool ?? true
            self.kevinLanguage = UserDefaults.standard.string(forKey: "kevinLanguage") ?? "auto"
            self.countryCode = UserDefaults.standard.string(forKey: "countryCode") ?? ""
            self.readCallIds = Self.loadReadCallIds(for: initialContractor)
            self.jobberConnected = UserDefaults.standard.bool(forKey: "jobberConnected")
            self.googleCalendarConnected = UserDefaults.standard.bool(forKey: "googleCalendarConnected")
        }

        if !inMemory {
            NotificationCenter.default.addObserver(
                self,
                selector: #selector(handleAuthEpochChanged),
                name: CallSessionEpoch.didChangeNotification,
                object: nil
            )
        }
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

    func applyAuthChange() {
        let currentAuth = currentAuthContext()
        if let origin = activeCallAuth, origin != currentAuth || !currentAuth.isValid {
            clearActiveCall()
        }
        let current = contractorId
        if !inMemory && !inScreenshotFixture {
            readCallIds = Self.loadReadCallIds(for: current)
        } else {
            readCallIds = []
        }
        unreadCallCount = 0
        notificationCallSid = ""
        notificationCallMessage = ""
    }

    // Session generation incremented on logout, contractor change, and credential change
    var sessionGeneration: Int {
        if let custom = authProviderClosure?() {
            return custom.generation
        }
        return CallSessionEpoch.shared.generation
    }

    // Onboarding
    @Published var isOnboarded: Bool = false {
        didSet {
            let loggedOut = oldValue && !isOnboarded
            if !inScreenshotFixture && !inMemory {
                DispatchQueue.main.async { UserDefaults.standard.set(self.isOnboarded, forKey: "isOnboarded") }
            }
            if loggedOut {
                callLifecycleRevision += 1
                if !inMemory && !inScreenshotFixture {
                    CallSessionEpoch.shared.advance()
                }
            }
        }
    }
    @Published var contractorId: String = "" {
        didSet {
            let changed = oldValue != contractorId
            if !inScreenshotFixture && !inMemory {
                if contractorId.isEmpty {
                    KeychainManager.shared.delete("contractorId")
                } else {
                    KeychainManager.shared.save("contractorId", value: contractorId)
                }
            }
            if changed {
                callLifecycleRevision += 1
                if !inMemory && !inScreenshotFixture {
                    readCallIds = Self.loadReadCallIds(for: contractorId)
                } else {
                    readCallIds = []
                }
                unreadCallCount = 0
                notificationCallSid = ""
                notificationCallMessage = ""
                if !inMemory && !inScreenshotFixture {
                    CallSessionEpoch.shared.advance()
                }
            }
        }
    }
    @Published var isRegistered: Bool = false

    // Subscription state (Keychain-backed — UI cache only, server is source of truth)
    @Published var subscriptionStatus: String = "trial" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            if subscriptionStatus.isEmpty {
                KeychainManager.shared.delete("subscriptionStatus")
            } else {
                KeychainManager.shared.save("subscriptionStatus", value: subscriptionStatus)
            }
        }
    }
    @Published var subscriptionTier: String = "none" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            if subscriptionTier.isEmpty {
                KeychainManager.shared.delete("subscriptionTier")
            } else {
                KeychainManager.shared.save("subscriptionTier", value: subscriptionTier)
            }
        }
    }
    @Published var subscriptionUUID: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
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

    @Published var kevinNumber: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
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
    @Published var forwardingActivated: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.forwardingActivated, forKey: "forwardingActivated") }
        }
    }
    @Published var appleUserId: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
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

    /// Stored producing auth context for the currently active call
    private(set) var activeCallAuth: CallAuthContext? = nil

    /// Optional presentation lease built directly from stored origin auth + raw active SID/revision.
    /// Returns nil immediately when origin != currentAuthContext or invalid, even before queued auth cleanup.
    var ownedActiveCallLease: CallPresentationLease? {
        let currentAuth = currentAuthContext()
        guard let origin = activeCallAuth,
              origin == currentAuth,
              origin.isValid,
              !activeCallSid.isEmpty else {
            return nil
        }
        return CallPresentationLease(
            auth: origin,
            scope: CallLifecycleSnapshot(callSid: activeCallSid, revision: callLifecycleRevision)
        )
    }

    /// Snapshot of call lifecycle. Returns an empty SID with current revision when stale or unowned.
    var callLifecycleSnapshot: CallLifecycleSnapshot {
        if ownedActiveCallLease != nil {
            return CallLifecycleSnapshot(callSid: activeCallSid, revision: callLifecycleRevision)
        } else {
            return CallLifecycleSnapshot(callSid: "", revision: callLifecycleRevision)
        }
    }

    @Published var notificationCallSid = ""
    @Published var notificationCallMessage = ""
    @Published var activeCallSid: String = "" {
        didSet { if oldValue != activeCallSid { callLifecycleRevision += 1 } }
    }
    @Published var activeCallerPhone: String = ""
    @Published var activeCallerName: String = ""
    @Published var activeCallReason: String = ""
    @Published private var capturedScreeningTranscript: CapturedScreeningTranscript? = nil
    @Published var showActiveCall: Bool = false
    @Published var transcriptLines: [TranscriptLine] = []
    @Published var callStartTime: Date? = nil
    @Published var callIgnored: Bool = false
    @Published var isOnCall: Bool = false

    // Settings
    @Published var userName: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.userName, forKey: "userName") }
        }
    }
    @Published var businessName: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.businessName, forKey: "businessName") }
        }
    }
    // Business street address and city required for Twilio number
    // provisioning in regulatory countries (RegulatoryAddress.countries).
    // Business contact details, not credentials, so UserDefaults matches
    // businessName rather than the Keychain.
    @Published var businessAddress: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.businessAddress, forKey: "businessAddress") }
        }
    }
    @Published var businessCity: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.businessCity, forKey: "businessCity") }
        }
    }
    @Published var serviceType: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.serviceType, forKey: "serviceType") }
        }
    }
    @Published var mode: String = "business" {
        didSet {
            if inScreenshotFixture || inMemory { return }
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

    @Published var ringThroughContacts: Bool = true {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.ringThroughContacts, forKey: "ringThroughContacts") }
        }
    }

    // Explicit consent to upload contacts to the server. Separate from iOS contacts permission.
    @Published var contactsUploadConsent: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.contactsUploadConsent, forKey: "contactsUploadConsent") }
        }
    }

    // Carrier type for forwarding codes. Verizon uses *71/*73; everyone else uses GSM codes.
    // Set during onboarding forwarding step; editable from Settings.
    @Published var isVerizonCarrier: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.isVerizonCarrier, forKey: "isVerizonCarrier") }
        }
    }

    @Published var sitToneEnabled: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.sitToneEnabled, forKey: "sitToneEnabled") }
        }
    }
    @Published var autoReplySms: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.autoReplySms, forKey: "autoReplySms") }
        }
    }
    @Published var smartInterruption: Bool = true {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.smartInterruption, forKey: "smartInterruption") }
        }
    }
    @Published var kevinLanguage: String = "auto" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.kevinLanguage, forKey: "kevinLanguage") }
        }
    }

    // Account country (ISO 3166-1 alpha-2), root-authoritative on the server;
    // mirrored here so forwarding codes key on it. Empty = not yet loaded.
    @Published var countryCode: String = "" {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.countryCode, forKey: "countryCode") }
        }
    }

    private static func loadReadCallIds(for contractor: String) -> Set<String> {
        guard !contractor.isEmpty else { return [] }
        let arr = UserDefaults.standard.stringArray(forKey: "readCallIds_\(contractor)") ?? []
        return Set(arr)
    }

    // Unread calls — tracked locally by call ID, scoped per contractor with synchronous persistence
    @Published var readCallIds: Set<String> = [] {
        didSet {
            if inScreenshotFixture || inMemory { return }
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
    @Published var jobberConnected: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.jobberConnected, forKey: "jobberConnected") }
        }
    }
    @Published var googleCalendarConnected: Bool = false {
        didSet {
            if inScreenshotFixture || inMemory { return }
            DispatchQueue.main.async { UserDefaults.standard.set(self.googleCalendarConnected, forKey: "googleCalendarConnected") }
        }
    }

    /// Current snapshot of authorization context
    func currentAuthContext() -> CallAuthContext {
        if let custom = authProviderClosure?() {
            return custom
        }
        #if DEBUG
        if inScreenshotFixture {
            return CallAuthContext(
                contractorId: contractorId.isEmpty ? "app-store-screenshot-fixture" : contractorId,
                bearerToken: "fixture-token",
                generation: sessionGeneration
            )
        }
        #endif
        return CallAuthContext(
            contractorId: contractorId,
            bearerToken: APIClient.shared.contractorToken,
            generation: sessionGeneration
        )
    }

    /// Whether there's an active call owned by current auth
    var hasActiveCall: Bool {
        ownedActiveCallLease != nil
    }

    /// Set active call state from producing boundary with required producing auth context
    func setActiveCall(callSid: String, callerPhone: String, callerName: String, authContext: CallAuthContext) {
        guard authContext.isValid, authContext == currentAuthContext(), !callSid.isEmpty else { return }
        let isSameCall = (activeCallSid == callSid && activeCallAuth == authContext)
        if !isSameCall {
            activeCallSid = callSid
            activeCallAuth = authContext
            activeCallerPhone = callerPhone
            activeCallerName = callerName
            activeCallReason = ""
            capturedScreeningTranscript = nil
            transcriptLines = []
            callIgnored = false
            callStartTime = Date()
            isOnCall = false
        } else {
            activeCallerPhone = callerPhone
            activeCallerName = callerName
        }
    }

    /// Captures in-memory frozen screening transcript snapshot for the exact valid presentation lease
    func captureScreeningTranscript(for lease: CallPresentationLease) {
        let currentAuth = currentAuthContext()
        let currentScope = callLifecycleSnapshot
        guard lease.isValid(auth: currentAuth, scope: currentScope) else { return }
        if let existing = capturedScreeningTranscript, existing.lease == lease {
            return
        }
        capturedScreeningTranscript = CapturedScreeningTranscript(
            lease: lease,
            lines: transcriptLines.map(\.text),
            reason: activeCallReason
        )
    }

    /// Retrieves captured screening transcript only if matching a valid presentation lease
    func screeningTranscript(for lease: CallPresentationLease) -> CapturedScreeningTranscript? {
        guard let snapshot = capturedScreeningTranscript,
              snapshot.lease == lease,
              lease.isValid(auth: currentAuthContext(), scope: callLifecycleSnapshot) else {
            return nil
        }
        return snapshot
    }

    /// Clears captured screening transcript scoped to its own presentation lease
    func clearScreeningTranscript(for lease: CallPresentationLease) {
        if let current = capturedScreeningTranscript, current.lease == lease {
            capturedScreeningTranscript = nil
        }
    }

    /// Updates active call reason with origin auth and owned SID validation
    func updateActiveCallReason(reason: String, authContext: CallAuthContext, callSid: String) {
        guard let origin = activeCallAuth,
              origin == authContext,
              origin == currentAuthContext(),
              origin.isValid,
              activeCallSid == callSid,
              !callSid.isEmpty else {
            return
        }
        let trimmed = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        self.activeCallReason = trimmed == "Speaking with Kevin" ? "" : String(trimmed.prefix(160))
    }

    /// Updates active call reason using presentation lease
    func updateActiveCallReason(reason: String, lease: CallPresentationLease) {
        guard lease.isValid(auth: currentAuthContext(), scope: callLifecycleSnapshot) else { return }
        updateActiveCallReason(reason: reason, authContext: lease.auth, callSid: lease.scope.callSid)
    }

    /// Updates active call transcript with origin auth and owned SID validation
    func updateActiveCallTranscript(text: String, authContext: CallAuthContext, callSid: String) {
        guard let origin = activeCallAuth,
              origin == authContext,
              origin == currentAuthContext(),
              origin.isValid,
              activeCallSid == callSid,
              !callSid.isEmpty else {
            return
        }
        self.transcriptLines = text.components(separatedBy: "\n").filter { !$0.isEmpty }.map { TranscriptLine(text: $0) }
    }

    /// Updates active call transcript lines with origin auth and owned SID validation
    func updateActiveCallTranscript(lines: [TranscriptLine], authContext: CallAuthContext, callSid: String) {
        guard let origin = activeCallAuth,
              origin == authContext,
              origin == currentAuthContext(),
              origin.isValid,
              activeCallSid == callSid,
              !callSid.isEmpty else {
            return
        }
        self.transcriptLines = lines
    }

    /// Updates active call transcript using presentation lease
    func updateActiveCallTranscript(text: String, lease: CallPresentationLease) {
        updateActiveCallTranscript(text: text, authContext: lease.auth, callSid: lease.scope.callSid)
    }

    /// Updates active call transcript lines using presentation lease
    func updateActiveCallTranscript(lines: [TranscriptLine], lease: CallPresentationLease) {
        updateActiveCallTranscript(lines: lines, authContext: lease.auth, callSid: lease.scope.callSid)
    }

    /// Clear active call state
    func clearActiveCall() {
        activeCallAuth = nil
        activeCallSid = ""
        activeCallerPhone = ""
        activeCallerName = ""
        activeCallReason = ""
        capturedScreeningTranscript = nil
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
            LiveCallObserver.shared.requestRefresh()
        }
    }

    @MainActor
    func refreshSecureStorageForActiveUse() {
        #if DEBUG
        if inScreenshotFixture { return }
        #endif
        if inMemory { return }

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
